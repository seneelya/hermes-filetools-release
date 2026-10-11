#!/usr/bin/env python3
"""read_file "new" path core — plain-text response modeled on gcb's own output.

Implements Vision01__read.md: a comment-framed header (path/size, optional
mtime, a "○○○" separator) followed by ■BLOCK/■END-wrapped file content,
instead of JSON. `numbered=False` (default) returns raw content; `numbered=
True` prefixes every returned line with its absolute line number (the
existing plugin-wide "N|content" gutter, see file_backend.add_line_numbers).
See that document for the symbol/comment-wrapping rationale.

CLI usage:
    python view_file_new_core.py PATH [--line N] [--count N] [--numbered] [--base-dir DIR]
"""

import base64
import sys
from pathlib import Path

try:  # plugin mode
    from .mocks import resolve_path_for_tool, file_ops, is_extractable_document, extract_document_bytes
    from .file_backend import FileBackend, add_line_numbers
    from ._new_format import (
        to_lines_and_trailing_nl, make_commenter, DEFAULT_SYMBOLS, format_mtime,
        detect_eol, line_length_stats, place_anchors, resolve_anchor_gap,
        escape_controls,
    )
    from .markers import resolve_file_tag
    from .read_dispatch import dispatch as dispatch_read
    from .flags import (parse_flags_report, flag_int, flag_int_list, flag_bool, unknown_flags,
                        FlagError, QUOTE_HINT)
    from . import _blocks
    from ._tool_text import op_line
    from . import _drift
    from . import _ledger
    from . import _vocab
except ImportError:  # console mode
    from mocks import resolve_path_for_tool, file_ops, is_extractable_document, extract_document_bytes  # type: ignore
    from file_backend import FileBackend, add_line_numbers  # type: ignore
    from _new_format import (  # type: ignore
        to_lines_and_trailing_nl, make_commenter, DEFAULT_SYMBOLS, format_mtime,
        detect_eol, line_length_stats, place_anchors, resolve_anchor_gap,
        escape_controls,
    )
    from markers import resolve_file_tag  # type: ignore
    from read_dispatch import dispatch as dispatch_read  # type: ignore
    from flags import (parse_flags_report, flag_int, flag_int_list, flag_bool, unknown_flags,  # type: ignore
                       FlagError, QUOTE_HINT)
    import _blocks  # type: ignore
    from _tool_text import op_line  # type: ignore
    import _drift  # type: ignore
    import _ledger  # type: ignore
    import _vocab  # type: ignore


# Read the file's full raw bytes rather than paging through the backend's own
# read_file() (which bakes N|content numbering into .content unconditionally
# on both backends) - we need genuinely raw text for numbered=False, and do
# our own line-slicing/numbering here instead.
MAX_READ_BYTES = 50 * 1024 * 1024

# Every flag this tool answers to. Anything else is reported by name rather
# than ignored (Plan08 risk 1). `line`/`count`/`numbered` live here because
# they are no longer schema properties - they are read out of `flags`, and the
# same-named keyword arguments below stay accepted but unadvertised.
# The second row is the block mode (Plan30): get_codeblock's own CLI words, same meaning.
KNOWN_FLAGS = frozenset({
    "line", "count", "numbered",
    "force", "encoding", "no-anchors", "help",
    "get-codeblock", "query", "outline", "level", "ancestor-level", "name",
})

# Words that only mean something inside `--get-codeblock`: without it they are refused by
# name, not ignored - a `--query` silently read as a line range is the confusion Plan08 removes.
BLOCK_ONLY_FLAGS = ("query", "outline", "level", "ancestor-level", "name")

# The answer-size limit when the caller passes none (plugin.yaml read_file.params.max_tokens).
DEFAULT_MAX_TOKENS = 10000


def view_file_tool_new(path: str, line: int = 1, count: int = 2000, numbered: bool = False,
                       base_dir: str = None, task_id: str = "default",
                       session_id: str = "",
                       mtime_enabled: bool = True, symbols: dict = None,
                       flags: str = None, anchors_enabled: bool = True,
                       anchor_gap=None, align_blocks: bool = True, cut_note: bool = True,
                       gcb_hint: bool = True, max_tokens: int = DEFAULT_MAX_TOKENS,
                       gcb_escalate: bool = True) -> str:
    """`align_blocks` .. `gcb_escalate` are read_file's Plan30 settings (plugin.yaml
    `read_file.params`): snap a line range to blocks, mark a range cut inside a block on its
    closer, hint `--get-codeblock --outline` on a partial read, the answer limit in tokens,
    and get_codeblock's own widening of a too-small `--query` block."""
    symbols = symbols or DEFAULT_SYMBOLS
    parsed_flags, quoted = parse_flags_report(flags)
    unknown = unknown_flags(parsed_flags, KNOWN_FLAGS)
    if unknown:
        listed = ", ".join(f"--{name}" for name in unknown)
        return (f"ERROR: read_file: unknown flag(s): {listed}. "
                'Values are written after a space ("--count 20", not '
                '"--count=20"). Call flags="--help" for the full list.')
    force = bool(parsed_flags.get("force"))
    no_anchors = bool(parsed_flags.get("no-anchors"))
    encoding_override = parsed_flags.get("encoding")
    if isinstance(encoding_override, bool):  # bare "--encoding" with no value
        return 'ERROR: --encoding needs a value, e.g. flags="--encoding cp866".'
    encoding_label = None

    # The flag string is the ADVERTISED way in (Plan08); the keyword arguments
    # are still accepted for callers that predate it (a session in flight, a
    # saved skill, our own CLI/tests), and the flag wins on conflict - the same
    # precedence upstream gave its own unadvertised `notify` aliases.
    try:
        starts = flag_int_list(parsed_flags, "line", line)
        count = flag_int(parsed_flags, "count", count)
        numbered = flag_bool(parsed_flags, "numbered", numbered)
        block_mode = flag_bool(parsed_flags, "get-codeblock", False)
        want = {
            "query": flag_bool(parsed_flags, "query", False),
            "outline": flag_bool(parsed_flags, "outline", False),
            "level": flag_int(parsed_flags, "level", 0),
            "ancestor": (flag_int(parsed_flags, "ancestor-level", 0)
                         if "ancestor-level" in parsed_flags else None),
            "line_given": "line" in parsed_flags,
        }
    except FlagError as e:
        return f"ERROR: read_file: {e}"
    stray = [f"--{n}" for n in BLOCK_ONLY_FLAGS if n in parsed_flags]
    if stray and not block_mode:
        return (f"ERROR: read_file: {', '.join(stray)} only works together with --get-codeblock "
                '(e.g. flags="--get-codeblock --line 120 --query"). Call flags="--help" for the guide.')
    want["name"] = parsed_flags.get("name")
    if want["name"] is True:
        return 'ERROR: read_file: --name needs a value, e.g. flags="--get-codeblock --name main".'
    if want["ancestor"] is not None and want["ancestor"] < 0:
        return "ERROR: read_file: --ancestor-level must be 0 or more (0 = this block, 1 = its parent)."

    try:
        be = FileBackend(task_id, mock_ops=file_ops, mock_resolve=resolve_path_for_tool)
        resolved_str = be.resolve(path, base_dir)

        # Checked BEFORE the binary guard below - these extensions ARE opaque
        # zip/binary containers, but extractable ones; has_binary_extension's
        # own blocklist also lists .docx/.xlsx/.pptx/.pdf, so checking it
        # first would reject every one of them before ever reaching here.
        if is_extractable_document(resolved_str):
            result = be.ops.read_file_bytes(resolved_str, max_bytes=MAX_READ_BYTES)
            if result.error:
                return f"ERROR: {result.error}"
            if not result.base64_content:
                return f"ERROR: could not read '{path}' for extraction (empty read)."
            try:
                document_bytes = base64.b64decode(result.base64_content, validate=True)
                extracted = extract_document_bytes(document_bytes, resolved_str)
            except Exception as e:
                return f"ERROR: failed to extract '{path}': {e}"
            if extracted is None:
                ext = Path(resolved_str).suffix.lower()
                return (f"ERROR: '{path}' ({ext}) extraction is not supported yet. "
                        "Not falling through to a raw read - this is a binary container, not text.")
            text = extracted
            file_size = result.file_size
        else:
            result = be.ops.read_file_bytes(resolved_str, max_bytes=MAX_READ_BYTES)
            if result.error:
                return f"ERROR: {result.error}"
            raw = base64.b64decode(result.base64_content, validate=True) if result.base64_content else b""
            decision = dispatch_read(raw, force=force, encoding=encoding_override)
            if decision.kind == "refuse":
                return f"ERROR: '{path}': {decision.reason}"
            text = raw.decode(decision.encoding, errors="replace")
            file_size = result.file_size
            encoding_label = decision.encoding + ("" if decision.encoding_verified else " (guessed)")

        lines, _trailing = to_lines_and_trailing_nl(text)
        total_lines = len(lines)

        # The model has just been shown this file's real line numbers, so every
        # hint it now holds is current: patch's drift budget for this path goes
        # back to zero (Vision01__patch.md §4.1). read_file is the ONLY thing
        # that resets it - patching a file is what accumulates it.
        _drift.reset(task_id, resolved_str)

        c = make_commenter(resolved_str, symbols)
        block_symbol = symbols.get("block_symbol", DEFAULT_SYMBOLS["block_symbol"])
        tag = resolve_file_tag(text, block_symbol)
        # Words and elements come from the tool dictionary (Plan28): the panel and the schema
        # description read the same ones.
        els = _vocab.format_elements({**DEFAULT_SYMBOLS, **symbols})
        w = _vocab.header("read_file")

        max_len, avg_len = line_length_stats(lines)
        eol = detect_eol(text)

        # The address goes FIRST so it can be read without reading the rest,
        # and comment-wrapped like the rest of this header - which is also what
        # lets write_file/patch strip it back out if the model echoes our
        # header into `content` (markers._marker_line_pattern).
        # No FileTag tail since 2026-09-03: it halved the model's recall of
        # the address and only duplicated the `FileTag:` line three rows below.
        op = _ledger.next_op(session=session_id, task_id=task_id)
        head = [c(op_line(op))] if op else []
        head.append(c(f"{w['file']}: {resolved_str}"))
        head.append(c(
            f"{w['size']}: {total_lines} lines, {file_size} bytes, {len(text)} chars, "
            f"{eol}, max/avg line {max_len}/{avg_len:.1f}"
        ))

        if mtime_enabled:
            mtime = be.stat_mtime(resolved_str)
            if mtime is not None:
                head.append(c(f"{w['modified']}: {format_mtime(mtime)}"))

        if encoding_label:
            head.append(c(f"{w['encoding']}: {encoding_label}"))

        if encoding_label and encoding_label.endswith("(guessed)"):
            head.append(c(f"{w['hint']}: encoding was guessed - retry with "
                          'flags="--encoding NAME" if this looks wrong.'))

        # Accepting the quotes is not enough: unless the answer SAYS they were
        # removed, the model keeps writing them and the next tool that has no
        # such tolerance eats them silently. Same channel that taught it
        # `--encoding` (Plan08: flags are advertised in ANSWERS).
        if quoted:
            head.append(c(QUOTE_HINT))

        page = _Page(lines=lines, c=c, els=els, w=w, tag=tag, symbols=symbols,
                     numbered=numbered, anchors=anchors_enabled and not no_anchors,
                     anchor_gap=anchor_gap, cut_note=cut_note,
                     budget=None if force else max_tokens)

        if block_mode:
            return _block_mode(page, head, resolved_str, starts, want, force, gcb_escalate,
                               max_tokens, op, session_id, task_id, total_lines)

        # count=0 is its own mode ("just tell me about the file") - distinct
        # from an empty file's 0-0 block: no lines were even requested, so
        # there is no block to bound, and we skip straight to the header.
        if count == 0:
            head.append(c(f"{w['file_tag']}: {tag}"))
            head.append(c(els["header_separator"]))
            out = "\n".join(head)
            # count=0 showed the model no content at all. Still an entry: the
            # panel's job is to say what the model can lean on, and "it only
            # ever looked at the size" is an answer, not a gap.
            _note_read(out, session_id, task_id, resolved_str, 0, 0, 0, op)
            return out
        if count < 1:
            count = 1
        starts = [max(1, s) for s in starts]

        if total_lines == 0:
            head.append(c(f"{w['file_tag']}: {tag}"))
            head.append(c(els["header_separator"]))
            out = "\n".join(head + [c(f"{els['block_open']} {tag} : 0-0"),
                                    c(f"{els['block_close']} {tag} : 0")])
            _note_read(out, session_id, task_id, resolved_str, 0, 0, 0, op, total=0, body=[])
            return out
        beyond = [s for s in starts if s > total_lines]
        if beyond:
            return f"ERROR: line {beyond[0]} is beyond the end of the file ({total_lines} lines)."

        # Plan30: the range's ends snap to blocks where the format has them. The whole file
        # needs no snapping, and anything that keeps the block reader from answering - an
        # unknown format, a missing grammar - leaves the plain range, said nowhere: there was
        # nothing to align, so there is nothing to report.
        ranges, by_blocks = None, False
        whole = len(starts) == 1 and starts[0] == 1 and count >= total_lines
        if align_blocks and not whole:
            try:
                with _blocks.parsed(lines, resolved_str) as (views, src):
                    ranges = _blocks.aligned_ranges(views, src, starts, count)
                    by_blocks = True
            except Exception:
                ranges = None
        if ranges is None:
            ranges = _blocks.plain_ranges(starts, count, total_lines)
        ranges = _blocks.merge(ranges)

        body, shown, limited = page.render(ranges)
        asked = [(starts[0], min(total_lines, starts[0] + count - 1))]
        if len(shown) > 1 or [(r["from"], r["to"]) for r in shown] != asked:
            head.append(c(f"{w['shown']}: " + ", ".join(f"{r['from']}-{r['to']}" for r in shown)))
        if page.controls:
            head.append(c(f"{w['control_chars']}: {page.controls} shown as pictures (␀-style) - "
                          "this body is NOT a byte copy of the file."))
        if limited:
            head.append(c(_limit_line(w, max_tokens, limited)))
        if gcb_hint and by_blocks and sum(r["to"] - r["from"] + 1 for r in shown) < total_lines:
            head.append(c(f"{w['hint']}: use flags=\"--get-codeblock --outline\" to get this "
                          "file's table of contents."))
        head.append(c(f"{w['file_tag']}: {tag}"))
        head.append(c(els["header_separator"]))

        out = "\n".join(head + body)
        _note_shown(out, session_id, task_id, resolved_str, shown, op, total_lines, page)
        return out

    except Exception as e:
        return f"ERROR: {e}"


def _limit_line(w, max_tokens, next_line):
    return (f"{w['limit']}: this answer stops at {max_tokens} tokens (a setting) - continue with "
            f'flags="--line {next_line}", or add --force to get everything at once.')


class _Page:
    """Renders file ranges as `■BLOCK`/`■END`-framed text — the ONE renderer for a plain read
    and for `--get-codeblock --query`, so both look identical and both feed the ledger alike.

    `budget` — tokens the bodies may take (None = no limit, `--force`). A range that does not
    fit is cut at the last whole line that does; nothing after it is shown, and `render`
    returns the line to continue from."""

    def __init__(self, *, lines, c, els, w, tag, symbols, numbered, anchors, anchor_gap,
                 cut_note, budget):
        self.lines, self.c, self.els, self.w, self.tag = lines, c, els, w, tag
        self.symbols, self.numbered, self.anchors = symbols, numbered, anchors
        self.anchor_gap, self.cut_note, self.budget = anchor_gap, cut_note, budget
        self.controls = 0
        self.page_lines = []     # every shown line, in order (the ledger's `body` for one range)

    def render(self, ranges):
        """-> (body lines, shown ranges, next line when the budget stopped it, else 0)."""
        c, els, tag = self.c, self.els, self.tag
        body, shown = [], []
        spent = 0
        for r in ranges:
            start, end = r["from"], r["to"]
            chunk = []
            for n in range(start, end + 1):
                one, k = escape_controls(self.lines[n - 1])
                if self.budget is not None:
                    cost = _blocks.count_tokens(one + "\n")
                    # Always at least one line: an answer that shows nothing teaches nothing.
                    if spent + cost > self.budget and (chunk or shown):
                        if chunk:
                            self._emit(body, shown, start, start + len(chunk) - 1, chunk, None)
                        return body, shown, n
                    spent += cost
                self.controls += k
                chunk.append(one)
            self._emit(body, shown, start, end, chunk, r.get("cut"))
        return body, shown, 0

    def _emit(self, body, shown, start, end, chunk, cut):
        c, els, tag = self.c, self.els, self.tag
        body.append(c(f"{els['block_open']} {tag} : {start}-{end}"))
        if self.numbered and chunk:
            # The gutter already numbers every line - anchors would be pure
            # noise here. They exist precisely FOR the un-numbered default.
            body.extend(add_line_numbers("\n".join(chunk), start).split("\n"))
        elif chunk and self.anchors:
            body.extend(place_anchors(
                chunk, start, c,
                self.symbols.get("anchor_symbol", DEFAULT_SYMBOLS["anchor_symbol"]), tag,
                min_gap=resolve_anchor_gap(self.anchor_gap),
            ))
        else:
            body.extend(chunk)
        # Plan30: a range whose end could not snap to a block says so ON its closer - the tail
        # travels with the mark line, and the markers grammar (`:.*`) still recognises it.
        tail = (f"   | {_vocab.MARKS['cut_word']} {cut[0]}-{cut[1]} |"
                if cut and self.cut_note else "")
        body.append(c(f"{els['block_close']} {tag} : {end}{tail}"))
        shown.append({"from": start, "to": end})
        self.page_lines.extend(chunk)


def _block_mode(page, head, resolved_str, starts, want, force, gcb_escalate, max_tokens,
                op, session_id, task_id, total_lines):
    """`--get-codeblock`: get_codeblock itself, its own semantics (Plan30).

    Maps (table of contents, the chain of blocks around a line, `--name` candidates) come as
    get_codeblock draws them, under a `Map:` line - they are NOT file text, so no `○○○` and
    no `■BLOCK`. `--query` returns blocks as file text in read_file's own frame, so patch,
    the write shield and the file panel know them like any read. A block over the answer
    limit is not torn: its map comes instead, unless `--force`."""
    c, w, els, tag = page.c, page.w, page.els, page.tag
    lv = -want["ancestor"] if want["ancestor"] is not None else want["level"]
    try:
        with _blocks.parsed(page.lines, resolved_str) as (views, src):
            notes, rendered, ranges = [], None, None
            starts_in = starts
            query, outline = want["query"], want["outline"]
            line_given = want["line_given"]
            if want["name"]:
                from get_codeblock.name_resolver import resolve_name, header_line
                res = resolve_name(src.path, str(want["name"]))
                notes.append(src.c(header_line(res)))
                found = [h.line for h in res.hits]
                if not found:
                    rendered = []
                elif res.exact and len(found) == 1:
                    starts_in, line_given = found, True
                    query = not outline
                else:
                    rendered = views.render_outline_batch(
                        views.outline_batch_view(src, found, [lv] * len(found)), src)
            if rendered is None and query:
                if not line_given:
                    return ('ERROR: read_file: --query needs the line of the block, e.g. '
                            'flags="--get-codeblock --line 120 --query" (or --name NAME).')
                qv = views.query_view(src, starts_in, [lv] * len(starts_in),
                                      force=force or not gcb_escalate)
                if qv["note"]:
                    notes.append(c(f"{w['hint']}: {qv['note']}"))
                notes.extend(c(f"ERROR: {e}") for e in qv["errors"])
                if not qv["runs"]:
                    return "\n".join(head + notes)
                ranges = []
                for run in qv["runs"]:
                    text = "\n".join(page.lines[run["start"] - 1:run["end"]])
                    if page.budget is not None and _blocks.count_tokens(text) > max_tokens:
                        notes.append(c(
                            f"{w['limit']}: block {run['start']}-{run['end']} is about "
                            f"{_blocks.count_tokens(text)} tokens, over this answer's "
                            f"{max_tokens} (a setting) - here is its map: read it in parts by "
                            "--line, or add --force to get the whole block."))
                        notes.extend(t for _k, t in views.render_outline(
                            views.outline_view(src, line=run["start"]), src))
                    else:
                        ranges.append({"from": run["start"], "to": run["end"], "cut": None})
            elif rendered is None:
                if outline or not line_given:
                    if line_given and len(starts_in) > 1:
                        rendered = views.render_outline_batch(views.outline_batch_view(
                            src, starts_in, [lv] * len(starts_in)), src)
                    else:
                        rendered = views.render_outline(views.outline_view(
                            src, line=starts_in[0] if line_given else None, level=lv), src)
                else:
                    rendered = views.render_ladder(views.ladder_view(src, starts_in), src)
            map_lines = [t for _k, t in rendered] if rendered is not None else []
    except _blocks.Unavailable as e:
        return (f"ERROR: read_file --get-codeblock: {e}. Read this file with "
                'flags="--line N --count N" instead.')
    except ValueError as e:
        return f"ERROR: read_file --get-codeblock: {e}"

    if ranges is None:
        # A map: what the model may lean on is structure, not text - recorded like `--count 0`.
        head.append(c(f"{w['map']}: the structure of this file's blocks, not file text - "
                      'get a block\'s text with flags="--get-codeblock --line N --query".'))
        bare = not want["line_given"] and not want["name"] and not want["level"]
        if bare:
            head.append(c(f"{w['hint']}: --level N limits how deep this map goes (1 = top level "
                          "only); --line N --outline = the map of the block at line N; "
                          "--ancestor-level K with it = the block K levels up."))
        out = "\n".join(head + notes + map_lines)
        _note_read(out, session_id, task_id, resolved_str, 0, 0, 0, op)
        return out

    body, shown, limited = page.render(_blocks.merge(ranges)) if ranges else ([], [], 0)
    if len(shown) > 1:
        head.append(c(f"{w['shown']}: " + ", ".join(f"{r['from']}-{r['to']}" for r in shown)))
    if page.controls:
        head.append(c(f"{w['control_chars']}: {page.controls} shown as pictures (␀-style) - "
                      "this body is NOT a byte copy of the file."))
    if limited:
        head.append(c(_limit_line(w, max_tokens, limited)))
    head.extend(notes)
    head.append(c(f"{w['file_tag']}: {tag}"))
    head.append(c(els["header_separator"]))
    out = "\n".join(head + body)
    _note_shown(out, session_id, task_id, resolved_str, shown, op, total_lines, page)
    return out


def _note_shown(out, session_id, task_id, resolved_str, shown, op, total, page):
    """The ledger entry of a read that showed `shown`: one range as before; several - their
    span in `from`/`to` plus `parts` (ledger amendment 8). `body` only for one range: inside
    execute_code it is compared from `from` on, and a body with gaps would compare wrong."""
    if not shown:
        _note_read(out, session_id, task_id, resolved_str, 0, 0, 0, op, total=total)
        return
    n = sum(r["to"] - r["from"] + 1 for r in shown)
    _note_read(out, session_id, task_id, resolved_str, shown[0]["from"], shown[-1]["to"], n, op,
               total=total, body=page.page_lines if len(shown) == 1 else None,
               parts=[[r["from"], r["to"]] for r in shown])


def _note_read(out: str, session_id: str, task_id: str, resolved_str: str,
               start: int, end: int, lines: int, op: str = "",
               total: int = 0, body=None, parts=None) -> None:
    """Record the read, and under no circumstances affect the return value.

    `body` — the page lines handed over; inside execute_code the ledger compares
    them with the script's answer (`deferred/17`), and never stores them.
    `parts` — several shown ranges of one read (`_ledger.note_op`).

    `_ledger.note_read` already swallows its own errors; this second guard is
    for the import/attribute layer, so a ledger module that failed to load or
    changed shape still cannot turn a working read_file into an ERROR string.

    `lines` and `total` are NOT the same number and confusing them is the bug
    Plan11-Task02 fixed: `lines` is how many lines we HANDED OVER (the slice),
    `total` is how long the FILE is. Only the latter can serve as the bar's
    denominator, and until this parameter existed the ledger had no way to
    know it — a read of 12-20 says nothing about the file's size.
    """
    try:
        _ledger.note_read(
            text=out, path=resolved_str, session=session_id, task_id=task_id,
            from_line=start, to_line=end, lines=lines, op=op, total=total,
            body=body, parts=parts,
        )
    except Exception:
        pass


def main():
    """CLI entry point."""
    import argparse

    # Console output can contain non-ASCII (■/○/⚓, box-drawing in fixtures, ...) - a
    # Windows cp1251/1252 console crashes print() on it otherwise (same fix as gcb).
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    parser = argparse.ArgumentParser(
        description="Read a file as plain framed text (new path, gcb-styled)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python view_file_new_core.py myfile.txt
  python view_file_new_core.py myfile.txt --line 100 --count 50
  python view_file_new_core.py myfile.txt --numbered""",
    )
    parser.add_argument("path", help="Path to file to read")
    parser.add_argument("--line", type=int, default=1, help="Starting line (1-indexed, default: 1)")
    parser.add_argument("--count", type=int, default=2000, help="Max lines to return (default: 2000)")
    parser.add_argument("--numbered", action="store_true", help="Prefix lines with absolute line numbers")
    parser.add_argument("--no-anchors", dest="no_anchors", action="store_true",
                        help="Don't place ⚓ line-number anchors in blank lines")
    parser.add_argument("--anchor-gap", dest="anchor_gap", type=int, default=None,
                        help="Min lines between ⚓ anchors (plugin.yaml: params.anchor_gap)")
    parser.add_argument("--base-dir", type=str, default=None)
    parser.add_argument("--flags", type=str, default=None,
                        help='Modifier string, e.g. --flags "--force"')
    args = parser.parse_args()

    print(view_file_tool_new(args.path, args.line, args.count, args.numbered, args.base_dir,
                             flags=args.flags, anchors_enabled=not args.no_anchors,
                             anchor_gap=args.anchor_gap))


if __name__ == "__main__":
    main()
