#!/usr/bin/env python3
"""patch — "new" path (plain-text response, no JSON).

Design: __HQ/vision/Vision01__patch.md. The old path (`replace_all` as a schema
boolean, JSON response) stays at modify_file_core.py, untouched - same split as
view_file_new_core.py/create_file_new_core.py/sift_files_new_core.py vs their
"old" siblings.

The whole point of this path (Vision §2): WE resolve the edit site, not the
model and not core's fuzzy matcher. We read the file ourselves, find the site
inside a window around the model's `line` hint, cut the text VERBATIM out of
the file, expand it with neighbouring text until it is unique file-wide, and
only then hand it to the backend's patch_replace as an exact string. The model
never has to hunt for a unique block, and core's own fuzzy matcher
(fuzzy_match.fuzzy_find_and_replace) has nothing left to guess.

CLI usage:
    python modify_file_new_core.py PATH --line N --old "text" --new "text" [--flags "--dry-run"]
"""

import base64
import re
import sys

try:  # plugin mode
    from .mocks import resolve_path_for_tool, file_ops
    from .file_backend import FileBackend, add_line_numbers
    from ._new_format import (make_commenter, DEFAULT_SYMBOLS, count_lines,
                              ui_envelope, minimal_diff, escape_controls)
    from .markers import resolve_file_tag, strip_marker_lines
    from .flags import (parse_flags_report, has_flag, flag_int, unknown_flags,
                        FlagError, QUOTE_HINT)
    from ._tool_text import op_line
    from . import _drift
    from . import _ledger
except ImportError:  # console mode
    from mocks import resolve_path_for_tool, file_ops  # type: ignore
    from file_backend import FileBackend, add_line_numbers  # type: ignore
    from _new_format import (make_commenter, DEFAULT_SYMBOLS, count_lines,  # type: ignore
                             ui_envelope, minimal_diff, escape_controls)
    from markers import resolve_file_tag, strip_marker_lines  # type: ignore
    from flags import (parse_flags_report, has_flag, flag_int, unknown_flags,  # type: ignore
                       FlagError, QUOTE_HINT)
    from _tool_text import op_line  # type: ignore
    import _drift  # type: ignore
    import _ledger  # type: ignore


MAX_READ_BYTES = 50 * 1024 * 1024

# Base half-width of the search window around `line`, in lines. Widened at
# call time by the accumulated line drift for this file (Vision §4.1). Sized
# generously on purpose: `line` is an ORIENTATION hint, not an address - its
# job is to disambiguate between several identical sites, not to be exact.
WINDOW_BASE = 60

# Every flag this tool answers to; anything else is refused BY NAME (Plan08
# risk 1). patch had no optional properties to move, but the rule has to hold
# for all four tools or it stops being a rule the model can rely on.
KNOWN_FLAGS = frozenset({
    "dry-run", "replace-all", "exact", "context", "numbered", "help",
})

# How many candidate sites we bother printing when the match is ambiguous.
MAX_CANDIDATES_SHOWN = 5

# A whole-gutter paste: EVERY line carries "<digits>|" (read_file/search_files
# numbered output). One code line that merely starts with a digit and a pipe
# is not enough - all lines must match, so ordinary content can't trip this.
_GUTTER_RE = re.compile(r"^\s*→?\d+\|")


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _looks_like_gutter(text: str) -> bool:
    lines = [ln for ln in (text or "").split("\n") if ln.strip()]
    return bool(lines) and all(_GUTTER_RE.match(ln) for ln in lines)


def _norm_ws(line: str) -> str:
    """Whitespace-insignificant form of one line: runs of spaces/tabs collapse
    to one space, leading/trailing whitespace (including a CR from a CRLF
    file) drops. This is the ONLY fuzziness this tool allows (Vision §5) -
    anything beyond whitespace is a non-match, not a near-match."""
    return re.sub(r"[ \t]+", " ", line).strip()


def _leading_ws(line: str) -> str:
    return line[:len(line) - len(line.lstrip(" \t"))]


def _line_starts(content: str) -> list:
    """Char offset of each line's first character."""
    starts = [0]
    for i, ch in enumerate(content):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _line_of(content: str, offset: int) -> int:
    """1-based line number containing *offset*."""
    return content.count("\n", 0, offset) + 1


def _reindent(new_text: str, model_prefix: str, file_prefix: str) -> str:
    """Re-anchor *new_text*'s indentation from what the model sent to what the
    file actually has (Vision §5).

    Fuzzy matching on whitespace fixes the SEARCH; without this it would break
    the WRITE - a model whose old_string had the wrong indentation almost
    certainly sent new_string with the same wrong indentation, and we would
    faithfully write it in."""
    if model_prefix == file_prefix:
        return new_text
    out = []
    for ln in new_text.split("\n"):
        if not ln.strip():
            out.append(ln)
        elif model_prefix and ln.startswith(model_prefix):
            out.append(file_prefix + ln[len(model_prefix):])
        elif not model_prefix:
            out.append(file_prefix + ln)
        else:
            out.append(ln)  # unexpected shape - leave it alone rather than guess
    return "\n".join(out)


def _expand_to_unique(content: str, off: int, old_core: str, new_core: str):
    """Grow the replaced span line by line until it occurs exactly once.

    This is the "uniqueness is the program's job" step (Vision §2): the model
    is no longer required to quote enough surrounding context to disambiguate.
    Works in character space so a partial-line edit expands correctly too.
    Returns (old_text, new_text).
    """
    start, end = off, off + len(old_core)
    while content.count(content[start:end]) > 1:
        moved = False
        if start > 0:
            # start-1, not start: when `start` already sits at the beginning of
            # a line, searching up to `start` finds the newline that ENDS the
            # previous line and lands us right back where we are - the span
            # would never grow leftwards and the loop would give up with a
            # still-ambiguous string, which core then rejects outright.
            prev_nl = content.rfind("\n", 0, start - 1)
            new_start = 0 if prev_nl == -1 else prev_nl + 1
            if new_start < start:
                start, moved = new_start, True
        if end < len(content):
            next_nl = content.find("\n", end)
            new_end = len(content) if next_nl == -1 else next_nl + 1
            if new_end > end:
                end, moved = new_end, True
        if not moved:
            break  # whole file already - can't disambiguate further
    prefix, suffix = content[start:off], content[off + len(old_core):end]
    return prefix + old_core + suffix, prefix + new_core + suffix


def _candidates(content: str, file_lines: list, starts: list, old_string: str,
                allow_fuzzy: bool):
    """All sites where *old_string* could be replaced.

    Exact substring matches first (these also cover partial-line edits). Only
    if there are NONE do we fall back to whitespace-insensitive matching, and
    that one is line-based - it needs whole lines to compare.

    Each candidate: {off, start_line, end_line, old_core, new_prefix_fix, fuzzy}
    """
    out = []
    if old_string:
        pos = content.find(old_string)
        while pos != -1:
            out.append({
                "off": pos,
                "start_line": _line_of(content, pos),
                "end_line": _line_of(content, pos + len(old_string)),
                "old_core": old_string,
                "indent": None,
                "fuzzy": False,
            })
            pos = content.find(old_string, pos + 1)
    if out or not allow_fuzzy:
        return out

    old_lines = old_string.strip("\n").split("\n")
    if not any(_norm_ws(ln) for ln in old_lines):
        return out  # blank/whitespace-only payload - refuse to fuzzy-match it
    k = len(old_lines)
    normalized_old = [_norm_ws(ln) for ln in old_lines]
    for i in range(0, len(file_lines) - k + 1):
        if [_norm_ws(ln) for ln in file_lines[i:i + k]] != normalized_old:
            continue
        off = starts[i]
        end_off = (starts[i + k] - 1) if (i + k) < len(starts) else len(content)
        out.append({
            "off": off,
            "start_line": i + 1,
            "end_line": i + k,
            "old_core": content[off:end_off],
            "indent": (_leading_ws(old_lines[0]), _leading_ws(file_lines[i])),
            "fuzzy": True,
        })
    return out


def _read_text(be, resolved_str: str):
    """(text, error). Reads raw bytes, not backend.read_file - the latter bakes
    the "N|content" gutter into .content on BOTH backends (see the same note in
    view_file_new_core.py), which would poison every match we try to make."""
    result = be.ops.read_file_bytes(resolved_str, max_bytes=MAX_READ_BYTES)
    if getattr(result, "error", None):
        return None, result.error
    raw = base64.b64decode(result.base64_content, validate=True) if result.base64_content else b""
    return raw.decode("utf-8", errors="replace"), None


def _echo_block(c, block_symbol: str, tag: str, file_lines: list, first: int, last: int,
                changed: range = None, numbered: bool = False, total: int = None) -> list:
    """One ■BLOCK/■END-framed excerpt. Body is raw file text by default -
    copy-safe, so the model can lift it straight into its next old_string
    (Vision §6). --numbered turns the →N| gutter back on.

    `total` is the file's real line count (count_lines): *file_lines* comes
    from a bare split("\\n"), which leaves a phantom trailing "" for any file
    ending in a newline - echoing that as a real line would invent one."""
    first = max(1, first)
    last = min(total if total is not None else len(file_lines), last)
    if last < first:
        return []
    body = file_lines[first - 1:last]
    if numbered:
        numbered_body = add_line_numbers("\n".join(body), first).split("\n")
        body = [
            ("→" + ln.lstrip()) if (changed and (first + i) in changed) else ln  # → = "→"
            for i, ln in enumerate(numbered_body)
        ]
    return ([c(f"{block_symbol}BLOCK {tag} : {first}-{last}")] + body
            + [c(f"{block_symbol}END {tag} : {last}")])


def _command_text(path, line, old_string, new_string, flags) -> str:
    """What the model asked for, as one readable body (Plan11-Task03).

    `old_string`/`new_string` are the unbounded part of a patch call and the
    reason this tool needs a sidecar at all. Rendered as labelled sections
    rather than as a dict repr: the sidecar is read by a HUMAN in the panel,
    and stage B will stub these very arguments out of the model's context, so
    this becomes the only surviving copy of what was requested.
    """
    parts = [f"patch {path or ''}".rstrip()]
    if line:
        parts.append(f"line: {line}")
    if flags:
        parts.append(f"flags: {flags}")
    if old_string:
        parts.append(f"--- old ---\n{old_string}")
    if new_string:
        parts.append(f"--- new ---\n{new_string}")
    return "\n".join(parts)


def _note_op(tool, out, path, session_id, task_id, facts, op="", command=""):
    """Record the operation in the ledger. Swallows EVERYTHING.

    A second guard on top of `_ledger`'s own fail-open: this one covers the
    import/attribute layer, so a module that failed to load or changed shape
    cannot turn a successful operation into an ERROR. Observability is never
    more important than the product.
    """
    try:
        _ledger.note_op(
            tool=tool, text=out, path=str(path or ""),
            session=session_id, task_id=task_id, op=op,
            lines=int(facts.get("added") or 0),
            # Plan11-Task02. `facts` is already the channel between
            # `_patch_impl` and here, so the position travels the same way the
            # counters do — no signature change along the chain. Absent when
            # the path never reached a located edit (an error before matching),
            # and 0 is exactly the right "unknown" there: `note_op` omits the
            # key rather than writing a fake line number.
            from_line=int(facts.get("from_line") or 0),
            to_line=int(facts.get("to_line") or 0),
            total=int(facts.get("total") or 0),
            command=command,
            counts={k: facts[k] for k in
                    ("added", "removed", "replacements", "no_change", "dry_run")
                    if k in facts},
        )
    except Exception:
        pass


def modify_file_tool_new(path: str = None, line: int = None, old_string: str = None,
                         new_string: str = None, flags: str = None,
                         base_dir: str = None, task_id: str = "default",
                         session_id: str = "",
                         symbols: dict = None, ui_json: str = "off") -> str:
    """Public entry point. The reference plain-text response is produced by
    _patch_impl and left untouched; `ui_json` only decides whether a short
    machine-readable block is placed ABOVE it for a UI to pick up
    (CONTRACT.md §19). Facts are collected by the impl as it goes, so the
    envelope never has to re-parse our own output."""
    facts: dict = {}
    # Taken here rather than inside the impl: the impl returns from a dozen
    # places (refusals, ambiguity, dry-run, the no-change outcome), and an
    # address is handed out once per CALL, not once per outcome (Plan09).
    op = _ledger.next_op(session=session_id, task_id=task_id)
    text = _patch_impl(path, line, old_string, new_string, flags, base_dir,
                       task_id, symbols, facts)
    # The echoed region is file content, so it can carry C0 controls straight
    # out of the file - same hazard, same single choke point as search_files.
    text, controls = escape_controls(text)
    if controls:
        text += ("\nNote: " + str(controls) + " control char(s) above are shown "
                 "as pictures (␀-style); the file itself is unchanged.")
    if text.lstrip().startswith("ERROR"):
        facts["success"] = False
        facts.setdefault("error", text.strip().splitlines()[0])
    elif facts:
        facts.setdefault("success", True)
    # Only on SUCCESS — see the same note in create_file_new_core: three
    # consumers read "did this fail" off the answer STARTING with "ERROR:",
    # so an address above an error line would report failures as successes.
    first = op_line(op)
    if first and facts.get("success"):
        text = first + "\n" + text
    out = ui_envelope(ui_json, facts, text)
    if facts.get("success"):
        _note_op("patch", out, path, session_id, task_id, facts, op,
                 command=_command_text(path, line, old_string, new_string, flags))
    return out


def _patch_impl(path, line, old_string, new_string, flags, base_dir, task_id,
                symbols, facts: dict) -> str:
    symbols = symbols or DEFAULT_SYMBOLS
    block_symbol = symbols.get("block_symbol", DEFAULT_SYMBOLS["block_symbol"])
    separator = symbols.get("separator_symbol", DEFAULT_SYMBOLS["separator_symbol"]) * 3

    if not path or not isinstance(path, str):
        return "ERROR: patch: missing required field 'path'."
    if old_string is None:
        return "ERROR: patch: missing required field 'old_string'."
    if new_string is None:
        return "ERROR: patch: missing required field 'new_string'."
    if line is None:
        return ("ERROR: patch: missing required field 'line' "
                "(pass 0 if you don't know where in the file this is).")

    parsed, quoted = parse_flags_report(flags)
    unknown = unknown_flags(parsed, KNOWN_FLAGS)
    if unknown:
        listed = ", ".join(f"--{name}" for name in unknown)
        return (f"ERROR: patch: unknown flag(s): {listed}. Values are written "
                'after a space ("--context 2", not "--context=2"). Nothing '
                'was changed. Call flags="--help" for the full list.')
    replace_all = bool(parsed.get("replace-all"))
    exact_only = bool(parsed.get("exact"))
    dry_run = bool(parsed.get("dry-run"))
    numbered = bool(parsed.get("numbered"))
    try:
        context = flag_int(parsed, "context", 3)
    except FlagError as e:
        return f"ERROR: patch: {e} Nothing was changed."
    line = max(0, _as_int(line, 0))

    if old_string == new_string:
        return ("ERROR: patch: old_string and new_string are identical - "
                "nothing to change.")

    # A pasted gutter is NOT quietly stripped (Vision §10): unlike our marker
    # lines, "47|" is a corruption of the code itself, and silently "fixing"
    # it would be exactly the guess-the-intent behaviour this tool refuses.
    for field, value in (("old_string", old_string), ("new_string", new_string)):
        if _looks_like_gutter(value):
            return (f"ERROR: patch: '{field}' looks like numbered output "
                    f"(every line starts with a \"N|\" gutter) rather than file "
                    f"content. Send the code itself, without the line-number "
                    f"column.")

    try:
        be = FileBackend(task_id, mock_ops=file_ops, mock_resolve=resolve_path_for_tool)
        resolved_str = be.resolve(path, base_dir)
        facts["files_modified"] = [resolved_str]

        # Our own marker lines (■BLOCK/■END/File:/FileTag:/⚓...) ARE stripped
        # silently - a line carrying a sentinel is provably not code.
        old_string, removed_old = strip_marker_lines(old_string, resolved_str, symbols)
        new_string, removed_new = strip_marker_lines(new_string, resolved_str, symbols)
        removed = removed_old + removed_new

        content, err = _read_text(be, resolved_str)
        if err:
            return f"ERROR: {err}"

        file_lines = content.split("\n")
        starts = _line_starts(content)
        old_total = count_lines(content)
        c = make_commenter(resolved_str, symbols)

        notes = []
        if removed:
            notes.append(f"Removed {removed} marker-like line(s) from the payload.")

        # ── locate ──────────────────────────────────────────────────────
        if replace_all:
            # File-wide by construction (Vision §7): "replace every occurrence,
            # but only in this window" isn't a real scenario - adjacent edits
            # get rewritten as one ordinary patch instead.
            occurrences = content.count(old_string) if old_string else 0
            if occurrences == 0:
                return (f"ERROR: patch: old_string not found in {resolved_str}. "
                        "Re-read the file - with --replace-all the match must be exact.")
            if line:
                notes.append("--replace-all applies file-wide; the line hint was ignored.")
            core_old, core_new = old_string, new_string
            send_old, send_new = old_string, new_string
            first_off = content.find(old_string)
        else:
            cands = _candidates(content, file_lines, starts, old_string, allow_fuzzy=not exact_only)
            if not cands:
                return (f"ERROR: patch: old_string not found in {resolved_str}. "
                        "Re-read the file to see its current content"
                        + ("." if exact_only else
                           " (whitespace differences are already tolerated, so this is a real "
                           "content mismatch)."))

            drift = _drift.drift_for(task_id, resolved_str)
            window = WINDOW_BASE + drift

            if not line:
                # No hint given: nothing to rank by, so more than one site is a
                # genuine ambiguity - refuse rather than pick.
                if len(cands) > 1:
                    return _ambiguous(c, block_symbol, separator, content, file_lines,
                                      cands, line, window, numbered)
                chosen = cands[0]
            else:
                # The hint RANKS, it doesn't filter. A flat "everything within
                # ±window" test would report an ambiguity even when the model
                # named the exact line of the site it meant and the decoy is
                # four lines away - which defeats the entire point of `line`.
                # Nearest-to-the-hint wins; a site that CONTAINS the hint beats
                # a merely nearby one; only a true tie is ambiguous.
                in_window = [x for x in cands if abs(x["start_line"] - line) <= window]
                containing = [x for x in in_window
                              if x["start_line"] <= line <= x["end_line"]]
                pool = containing or in_window or cands

                def _dist(x):
                    return abs(x["start_line"] - line)

                best_dist = min(_dist(x) for x in pool)
                best = [x for x in pool if _dist(x) == best_dist]
                if len(best) > 1:
                    return _ambiguous(c, block_symbol, separator, content, file_lines,
                                      best, line, window, numbered, tie=True)
                chosen = best[0]

                if not in_window:
                    notes.append(
                        f"No match within {window} lines of {line} - used the one at line "
                        f"{chosen['start_line']} (hint was off by {best_dist}). Your line "
                        f"numbers for this file are stale; re-read it before the next edit.")
                elif len(cands) > 1:
                    others = ", ".join(str(x["start_line"]) for x in cands
                                       if x is not chosen)
                    notes.append(f"{len(cands)} sites matched; used line "
                                 f"{chosen['start_line']} as the nearest to your hint "
                                 f"({line}). Others: {others}.")

            new_core = new_string
            if chosen["fuzzy"]:
                model_prefix, file_prefix = chosen["indent"]
                new_core = _reindent(new_string, model_prefix, file_prefix)
                notes.append("Matched ignoring whitespace differences"
                             + ("; new_string re-indented to match the file."
                                if model_prefix != file_prefix else "."))
            # core_* = the span actually being replaced (what the response
            # reports and echoes); send_* = that span padded with neighbouring
            # text until it is file-unique (what the backend needs, and what
            # the model no longer has to construct itself).
            core_old, core_new = chosen["old_core"], new_core
            send_old, send_new = _expand_to_unique(content, chosen["off"],
                                                   core_old, core_new)
            first_off = chosen["off"]
            occurrences = 1

        change_start = _line_of(content, first_off)

        # ── dry run: everything except the write ────────────────────────
        if dry_run:
            end_line = change_start + core_old.count("\n")
            head = [c(f"Dry run: {resolved_str} - no write performed."),
                    c(f"Would replace: {occurrences} occurrence(s), "
                      f"line {change_start}"
                      + (f"-{end_line}" if end_line != change_start else "")),
                    c(f"Lines: {old_total} (unchanged)")]
            head += [c(f"Note: {n}") for n in notes]
            head.append(c(separator))
            # Plan11-Task02: position and length ARE recorded for a dry run,
            # deliberately. The model looked HERE, and that is worth showing on
            # the bar. What must NOT happen is a coordinate shift — and it
            # cannot: `added`/`removed` stay unset on this path, so replaying
            # the deltas moves nothing.
            facts.update(dry_run=True, replacements=0,
                         diff=minimal_diff(core_old, core_new),
                         from_line=change_start, to_line=end_line,
                         total=old_total)
            return "\n".join(head + _echo_block(
                c, block_symbol, resolve_file_tag(content, block_symbol), file_lines,
                change_start - context, end_line + context,
                range(change_start, end_line + 1), numbered, total=old_total))

        # ── write ───────────────────────────────────────────────────────
        result = be.ops.patch_replace(resolved_str, send_old, send_new,
                                      replace_all=replace_all)
        if result.error:
            return f"ERROR: {result.error}"

        # Core detects a re-sent, already-landed edit and returns success with
        # no write (file_operations.py:2244). Reporting "Patched" here would be
        # a lie the model then does line arithmetic on - see Vision §8.
        if getattr(result, "no_change", False):
            head = [c(f"Unchanged: {resolved_str}"),
                    c("Already applied - the file already contains the target text. "
                      "No write performed; do not re-send this patch."),
                    c(f"Lines: {old_total}")]
            # Plan11-Task02: same reasoning as the dry run — the target place is
            # a fact worth placing on the bar, and with no `added`/`removed`
            # there is no shift. The end is derived the way dry-run derives it,
            # from the target text's own line count.
            facts.update(no_change=True, replacements=0,
                         from_line=change_start,
                         to_line=change_start + core_old.count("\n"),
                         total=old_total)
            return "\n".join(head + [c(f"Note: {n}") for n in notes])

        # ── echo: a real read AFTER the write, not a preview ─────────────
        new_content, err = _read_text(be, resolved_str)
        if err:
            return (f"Patched: {resolved_str}\n"
                    f"ERROR: the write succeeded but the file could not be re-read "
                    f"for confirmation: {err}")

        new_lines = new_content.split("\n")
        new_total = count_lines(new_content)
        delta = new_total - old_total
        if delta:
            _drift.note_edit(task_id, resolved_str, delta)

        change_end = change_start + core_new.count("\n")
        tag = resolve_file_tag(new_content, block_symbol)

        header = [c(f"Patched: {resolved_str}")]
        if delta:
            header.append(c(f"Lines: {old_total} -> {new_total} "
                            f"({delta:+d} from line {change_start})"))
        else:
            header.append(c(f"Lines: {new_total} (unchanged)"))
        # OUR count, never the backend's. The real backend's PatchResult has no
        # occurrence count at all - only files_modified, a list of PATHS, which
        # is length 1 for any single-file edit, so FileBackend can only ever
        # report 1 (the old path hit the same thing live on 2026-08-24 and
        # works around it with its own pre-count). We already hold the file
        # content, so we know the true number exactly - and the mock backend
        # DOES count correctly, which is why only a live run exposes this.
        header.append(c(f"Replacements: {occurrences}"))
        if quoted:
            notes.append(QUOTE_HINT)
        header += [c(f"Note: {n}") for n in notes]
        header.append(c(separator))

        facts.update(replacements=occurrences,
                     removed=len(core_old.split("\n")) if core_old else 0,
                     added=len(core_new.split("\n")) if core_new else 0,
                     diff=minimal_diff(core_old, core_new),
                     # Plan11-Task02: WHERE it landed and how long the file
                     # ended up. Both are already computed right here for the
                     # header two lines above; the ledger simply never got
                     # them, which left the panel unable to place a patch on
                     # the bar at all.
                     from_line=change_start, to_line=change_end,
                     total=new_total)
        return "\n".join(header + _echo_block(
            c, block_symbol, tag, new_lines,
            change_start - context, change_end + context,
            range(change_start, change_end + 1), numbered, total=new_total))

    except Exception as e:
        return f"ERROR: {e}"


def _ambiguous(c, block_symbol: str, separator: str, content: str, file_lines: list,
               cands: list, line: int, window: int, numbered: bool,
               tie: bool = False) -> str:
    """Several possible sites: refuse and SHOW them (Vision §5) - never pick
    the first one, and never make the model re-guess blind.

    `tie` distinguishes the two ways this happens, because the fix differs: no
    hint at all (give one) vs. a hint that lands exactly between two equally
    distant sites (give a closer one, or narrow old_string)."""
    tag = resolve_file_tag(content, block_symbol)
    shown = cands[:MAX_CANDIDATES_SHOWN]
    where = (f"equally close to line {line}" if tie
             else "in the file (no line hint was given)")
    fix = ("Fix: your hint sits exactly between them - re-send with `line` on the "
           "one you want, or include more surrounding text in old_string."
           if tie else
           "Fix: re-send with `line` set to the site you want (you passed 0 = anywhere).")
    out = [c(f"ERROR: patch: {len(cands)} possible matches {where} - refusing to "
             f"guess which one you meant."),
           c(fix)]
    if len(shown) < len(cands):
        out.append(c(f"Showing the first {len(shown)}."))
    out.append(c(separator))
    for cand in shown:
        out += _echo_block(c, block_symbol, tag, file_lines,
                           cand["start_line"], cand["end_line"],
                           range(cand["start_line"], cand["end_line"] + 1), numbered)
    return "\n".join(out)


def main():
    """CLI entry point."""
    import argparse

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    parser = argparse.ArgumentParser(
        description="patch 'new' path - plain-text response, see "
                     "__HQ/vision/Vision01__patch.md.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:

  python modify_file_new_core.py f.py --line 47 --old "x = 1" --new "x = 2"
  python modify_file_new_core.py f.py --line 0  --old "x = 1" --new "x = 2"
  python modify_file_new_core.py f.py --line 47 --old "x = 1" --new "x = 2" --flags="--dry-run"
  python modify_file_new_core.py f.py --line 0  --old "old" --new "new" --flags="--replace-all"
""")
    parser.add_argument("path", help="File to patch")
    parser.add_argument("--line", type=int, required=True,
                        help="Line hint; 0 = unknown, search the whole file")
    parser.add_argument("--old", "-o", type=str, required=True, help="Text to replace")
    parser.add_argument("--new", "-n", type=str, required=True, help="Replacement text")
    parser.add_argument("--flags", type=str, default=None,
                        help='Modifiers, e.g. --flags="--dry-run --numbered"')
    parser.add_argument("--base-dir", type=str, default=None)
    parser.add_argument("--ui-json", dest="ui_json", default="off",
                        choices=["off", "min", "diff", "wrap"],
                        help="UI envelope mode (plugin.yaml: params.ui_json)")

    args = parser.parse_args()
    print(modify_file_tool_new(path=args.path, line=args.line, old_string=args.old,
                               new_string=args.new, flags=args.flags,
                               base_dir=args.base_dir, ui_json=args.ui_json))


if __name__ == "__main__":
    main()
