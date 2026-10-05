#!/usr/bin/env python3
"""search_files — "new" path (plain-text response, no JSON).

Design: __HQ/vision/Vision01__search.md. Old path (`target`/`output_mode` enums,
JSON) stays at sift_files_core.py, untouched - same split as
view_file_new_core.py/create_file_new_core.py vs their "old" siblings.

CLI usage:
    python sift_files_new_core.py <path> --pattern "regex" [--glob "*.py"] [--flags "--context 2"]
    python sift_files_new_core.py <path> --pattern ""              # filenames only, glob="*"
"""

import os
import sys
from pathlib import Path

try:  # plugin mode
    from .mocks import resolve_path_for_tool, file_ops, normalize_search_pagination
    from .file_backend import FileBackend
    from .flags import (parse_flags_report, has_flag, flag_int, unknown_flags,
                        FlagError, QUOTE_HINT, is_quotes_only)
    from ._tool_text import op_line
    from ._new_format import escape_controls, make_commenter
    from . import _ledger
    from . import _vocab
except ImportError:  # console mode
    from mocks import resolve_path_for_tool, file_ops, normalize_search_pagination  # type: ignore
    from file_backend import FileBackend  # type: ignore
    from flags import (parse_flags_report, has_flag, flag_int, unknown_flags,  # type: ignore
                       FlagError, QUOTE_HINT, is_quotes_only)
    from _tool_text import op_line  # type: ignore
    from _new_format import escape_controls, make_commenter  # type: ignore
    import _ledger  # type: ignore
    import _vocab  # type: ignore


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# Every flag this tool answers to; anything else is refused BY NAME (Plan08
# risk 1) - an ignored "--glob=*.py" would search every file instead of the
# ones asked for, and the response would look like a legitimate result.
# `--ignore-case` stays deliberately unadvertised (CONTRACT.md §15) but is
# recognised here, and `glob` moved in from being a schema property.
KNOWN_FLAGS = frozenset({
    "glob", "context", "limit", "offset",
    "files-only", "tally", "ignore-case", "help",
})


def _continue_flags(flags: str, next_offset: int) -> str:
    """The pagination hint, as a flags string the model can send back AS IS.

    It must carry the caller's OTHER flags along - since `glob` moved into the
    flag string (Plan08), a hint of just '--offset 50' would quietly widen the
    next search to every file, and the answer would look legitimate.
    """
    tokens = (flags or "").split()
    kept, i = [], 0
    while i < len(tokens):
        tok = tokens[i]
        takes_value = i + 1 < len(tokens) and not tokens[i + 1].startswith("--")
        if tok == "--offset":  # replaced by the new one below
            i += 2 if takes_value else 1
            continue
        kept.append(tok)
        if takes_value:
            kept.append(tokens[i + 1])
        i += 2 if takes_value else 1
    kept += ["--offset", str(next_offset)]
    return " ".join(kept)


def _to_relative(file_path: str, base: str) -> str:
    """Match paths are always shown relative to the resolved search `path`
    (Vision01__search.md §5) - unifies what used to be an absolute/relative
    split between target=files and output_mode=files_only in the old path.

    The mock backend (mocks.py::_parse_grep_output) already strips the search
    root prefix itself, so `file_path` arrives ALREADY relative there - only
    the real backend hands back an absolute path. Re-running relpath() on an
    already-relative name against an absolute `base` computes nonsense
    (e.g. "..\\..\\Edge.py") - only convert when `file_path` is actually
    absolute.

    Live-agent finding 2026-08-26: when `path` itself points AT a single
    file (not a directory), `file_path == base` and relpath() collapses to
    "." - a real file's group header shouldn't read ".", fall back to the
    basename in that case."""
    if not os.path.isabs(file_path):
        return file_path
    try:
        rel = os.path.relpath(file_path, base)
    except (ValueError, TypeError):
        return file_path
    return os.path.basename(file_path) if rel == "." else rel


def sift_files_tool_new(path: str, pattern: str, glob: str = "*", flags: str = None,
                         base_dir: str = None, task_id: str = "default",
                         symbols: dict = None, session_id: str = "") -> str:
    """Search, then record the operation. The search itself is `_sift_impl`.

    Split for one reason: the implementation returns from half a dozen places
    (refusals, the filename-only path, three truncation branches), and an
    address has to be handed out ONCE per call while the record has to be
    written ONCE per answer. A wrapper does both at a single point instead of
    six.
    """
    op = _ledger.next_op(session=session_id, task_id=task_id)
    out = _sift_impl(path, pattern, glob, flags, base_dir, task_id, symbols, op)
    # Matching lines come out of the files themselves, so a C0 control byte
    # reaches the answer the same way it does in read_file - and kills the same
    # downstream log capture. Escaped at this single choke point because the
    # implementation returns from half a dozen places.
    out, controls = escape_controls(out)
    if controls:
        out += ("\n" + _vocab.header("search_files")["note"] + ": " + str(controls)
                + " control char(s) above are shown "
                "as pictures (␀-style); the files themselves are unchanged.")
    try:
        _ledger.note_op(tool="search_files", text=out, path=str(path or ""),
                        session=session_id, task_id=task_id, op=op)
    except Exception:
        pass          # observability never outranks the answer
    return out


def _sift_impl(path: str, pattern: str, glob: str = "*", flags: str = None,
               base_dir: str = None, task_id: str = "default",
               symbols: dict = None, op: str = "") -> str:
    if not path or not isinstance(path, str):
        return "ERROR: search_files: missing required field 'path'."
    if pattern is None:
        return ("ERROR: search_files: missing required field 'pattern' "
                "(pass \"\" to search by filename only, no content search).")

    # A pattern that is NOTHING BUT a pair of quotes means "empty" - and we
    # are the ones who asked for it: our own schema said «pass "" (empty
    # string)», and in a template with no quoting layer the model copies those
    # two characters literally. Seen live 2026-09-03: `pattern='""'` searched
    # for two quote marks, answered `Found: 0`, and the agent concluded our
    # tool needs `--files-only` to filter by name. The `Search:` echo below
    # still shows what was actually searched, so this normalisation is visible
    # rather than silent. Only the quotes-only case: a pattern that CONTAINS
    # quotes is a legitimate search and is left alone.
    quoted_pattern = is_quotes_only(pattern)
    if quoted_pattern:
        pattern = ""

    sep = _vocab.format_elements(
        {"separator_symbol": (symbols or {}).get("separator_symbol", "○")})["header_separator"]
    # Header words from the tool dictionary (Plan28).
    w = _vocab.header("search_files")
    # The answer has no file, so no native comment syntax: every METADATA line (address, echo,
    # counts, hints, the divider) wears the fallback symbol; the body below the divider does not.
    c = make_commenter("", symbols)

    parsed, quoted = parse_flags_report(flags)
    unknown = unknown_flags(parsed, KNOWN_FLAGS)
    if unknown:
        listed = ", ".join(f"--{name}" for name in unknown)
        return (f"ERROR: search_files: unknown flag(s): {listed}. Values are "
                'written after a space ("--context 2", not "--context=2"). '
                'Call flags="--help" for the full list.')
    try:
        context = flag_int(parsed, "context", 0)
        limit = flag_int(parsed, "limit", 50)
        offset = flag_int(parsed, "offset", 0)
        # The flag is the advertised way in (Plan08); `glob=` is still
        # accepted unadvertised, and the flag wins on conflict.
        raw_glob = parsed.get("glob")
        if raw_glob is True:
            raise FlagError('--glob expects a filename pattern, e.g. "--glob *.py".')
        if raw_glob:
            glob = str(raw_glob)
    except FlagError as e:
        return f"ERROR: search_files: {e}"
    files_only = has_flag(flags, "files-only")
    tally = has_flag(flags, "tally")
    ignore_case = has_flag(flags, "ignore-case")

    offset, limit = normalize_search_pagination(offset, limit)

    be = FileBackend(task_id, mock_ops=file_ops, mock_resolve=resolve_path_for_tool)
    try:
        resolved_str = be.resolve(path, base_dir)
    except Exception as e:
        return f"ERROR: {e}"

    if not be.is_real and not Path(resolved_str).exists():
        return f"ERROR: Path not found: {path}"

    glob = glob if glob else "*"
    search_glob = f"pattern={pattern!r}  path={resolved_str}  glob={glob}"
    if ignore_case:
        search_glob += "  (ignore-case)"
    for flag_name, flag_val in (("context", context), ("limit", limit if limit != 50 else None),
                                 ("offset", offset if offset else None)):
        if flag_val:
            search_glob += f"  --{flag_name} {flag_val}"
    if files_only:
        search_glob += "  --files-only"
    if tally:
        search_glob += "  --tally"

    # The address leads the answer, above the Search: echo (Plan09). No FileTag
    # tail: this answer spans MANY files, so a tag taken from one of them would
    # be a supplement that misleads rather than helps.
    first = op_line(op)
    header_prefix = (c(first) + "\n" if first else "") + c(f"{w['search']}: {search_glob}") + "\n"
    if quoted or quoted_pattern:
        header_prefix += c(QUOTE_HINT) + "\n"

    # Real backend's own `truncated`/`total_count` are unreliable when
    # context=0: core fetches EXACTLY `limit` rows through a shell `head`
    # with no over-fetch margin (Recon09/_search_with_rg's fetch_limit),
    # so it has no way to tell "got exactly N" from "got cut off at N" -
    # live-agent finding 2026-08-26 (F1: no truncated/Hint when it should
    # have appeared). Fix ourselves: always ask for one extra result and
    # decide truncation from whether we actually got it back, instead of
    # trusting the backend's own flag (still OR'd in as a second signal -
    # the mock backend's flag IS reliable, no harm in keeping it).
    FETCH_PAD = 1

    # `(?i)` inline regex flag: rg (the real backend's primary engine,
    # Recon09) understands it - the real adapter has no ignore_case kwarg
    # at all (core's search_tool doesn't expose one), so this is the only
    # way to reach it there. Only applied for the real backend - the mock
    # backend gets `ignore_case` as its own kwarg instead (mocks.py adds
    # `-i` to its grep -E call, which doesn't understand `(?i)` inline -
    # prefixing it there would search for the literal text "(?i)").
    # Narrow known gap (CONTRACT.md §15): won't help if core itself falls
    # back to plain grep instead of rg (POSIX ERE, same restriction) - rare,
    # rg is the default/preferred engine.
    backend_pattern = f"(?i){pattern}" if (ignore_case and be.is_real) else pattern

    # ── target=files (pattern == "") — filename-only search, glob is the mask ──
    if not pattern:
        result = be.ops.search(
            pattern=glob, path=resolved_str, target="files",
            limit=limit + FETCH_PAD, offset=offset, output_mode="content", context=0,
        )
        if result.error:
            return f"ERROR: {result.error}"
        raw_files = result.matches or []
        truncated = len(raw_files) > limit or bool(getattr(result, "truncated", False))
        files = [_to_relative(m["path"], resolved_str) for m in raw_files[:limit]]
        total = getattr(result, "total_count", len(files))
        lines = [header_prefix.rstrip("\n")]
        # "at least" because the count is capped by OUR fetch window: we ask
        # for limit+FETCH_PAD rows and never look past them, so a truncated
        # answer knows a LOWER BOUND, not a total. Seen live 2026-09-03:
        # "Found: 4 matches (showing 3)" followed by a continuation that
        # returned 2 more - a model that trusted the 4 would have stopped.
        found = (f"{w['found']}: at least {total} file(s)" if truncated
                 else f"{w['found']}: {total} file(s)")
        if truncated:
            found += f", truncated (showing {len(files)})"
        lines.append(c(found))
        if truncated:
            next_offset = offset + limit
            lines.append(c(f'{w["hint"]}: truncated — flags="{_continue_flags(flags, next_offset)}" to continue.'))
        lines.append(c(sep))
        lines.extend(files)
        return "\n".join(lines)

    # ── content search ──────────────────────────────────────────────────────
    file_glob = None if glob == "*" else glob
    base_result = be.ops.search(
        pattern=backend_pattern, path=resolved_str, target="content", file_glob=file_glob,
        limit=limit + FETCH_PAD, offset=offset, output_mode="content", context=0,
        ignore_case=ignore_case,
    )
    if base_result.error:
        return f"ERROR: {base_result.error}"

    raw_matches = base_result.matches or []
    truncated = len(raw_matches) > limit or bool(getattr(base_result, "truncated", False))
    true_matches = raw_matches[:limit]
    match_keys = {(m["file"], m["line_number"]) for m in true_matches}
    total_count = getattr(base_result, "total_count", len(true_matches))

    display_source = true_matches
    if context > 0 and true_matches:
        ctx_result = be.ops.search(
            pattern=backend_pattern, path=resolved_str, target="content", file_glob=file_glob,
            limit=limit, offset=offset, output_mode="content", context=context,
            ignore_case=ignore_case,
        )
        # Known limitation (Vision01__search.md §8, open question - mock
        # console engine): the SHARED mocks.py::_parse_grep_output only
        # understands `file:line:content` (colon) - grep -C's context lines
        # come back as `file-line-content` (dash) and are silently dropped,
        # so in mock/console mode this call currently returns the SAME
        # lines as the context=0 call above (no visible context). The real
        # Hermes backend parses all three formats correctly (Recon09) - this
        # only affects our own console testing, not the live tool. Also:
        # context-fetch's own limit/offset accounting counts context lines
        # toward the same budget, so its page boundary can drift slightly
        # from `true_matches` on heavily-truncated results even once fixed.
        # match_keys (built from the context=0 call above) still correctly
        # marks which lines are real matches vs context either way.
        if not ctx_result.error and ctx_result.matches:
            display_source = ctx_result.matches

    # Group by file, preserving first-seen order.
    groups = {}
    for m in display_source:
        groups.setdefault(m["file"], []).append(m)

    file_counts = {}
    for m in true_matches:
        file_counts[m["file"]] = file_counts.get(m["file"], 0) + 1

    lines = [header_prefix.rstrip("\n")]

    if files_only:
        found = f"{w['found']}: {len(groups)} file(s) with matches"
        if truncated:
            found += f" (of at least {total_count} matches)"
        lines.append(c(found))
        if truncated:
            next_offset = offset + limit
            lines.append(c(f'{w["hint"]}: truncated — flags="{_continue_flags(flags, next_offset)}" to continue.'))
        lines.append(c(sep))
        lines.extend(_to_relative(f, resolved_str) for f in groups.keys())
        return "\n".join(lines)

    n_files = len(groups)
    at_least = "at least " if truncated else ""
    found = (f"{w['found']}: {at_least}{total_count} matches in {n_files} "
             f"file{'s' if n_files != 1 else ''}")
    if truncated:
        found += f" (showing {len(true_matches)})"
    lines.append(c(found))
    if truncated:
        next_offset = offset + limit
        lines.append(c(f'{w["hint"]}: truncated — flags="{_continue_flags(flags, next_offset)}" to continue.'))
    lines.append(c(sep))

    if tally:
        for f in groups.keys():
            lines.append(f"{_to_relative(f, resolved_str)}: {file_counts.get(f, 0)}")
        return "\n".join(lines)

    for f, group_matches in groups.items():
        rel = _to_relative(f, resolved_str)
        lines.append(f"{rel}: {file_counts.get(f, 0)}")
        prev_line = None
        for m in group_matches:
            ln = m["line_number"]
            if prev_line is not None and ln > prev_line + 1:
                lines.append("--")
            marker = "→" if (f, ln) in match_keys else ""  # → = "→" ("→")
            lines.append(f"{marker}{ln}|{m['content']}")
            prev_line = ln

    return "\n".join(lines)


def main():
    """CLI entry point."""
    import argparse

    # Console output can contain non-ASCII (○/→, ...) - a Windows cp1251/1252
    # console crashes print() on it otherwise (same fix as view_file_new_core.py/gcb).
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    parser = argparse.ArgumentParser(
        description="search_files 'new' path - plain-text response, see "
                     "__HQ/vision/Vision01__search.md.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:

  python sift_files_new_core.py tests/Edge --pattern "async def"
  python sift_files_new_core.py tests/Edge --pattern "async def" --flags "--context 2"
  python sift_files_new_core.py tests/Edge --pattern "" --glob "*.py"
  python sift_files_new_core.py tests/Edge --pattern "async" --flags "--files-only"
  python sift_files_new_core.py tests/Edge --pattern "async" --flags "--tally --ignore-case" """
    )

    parser.add_argument("path", help="Where to search")
    parser.add_argument("--pattern", required=True,
                        help="Regex to search inside file contents; \"\" = filenames only")
    parser.add_argument("--glob", default="*", help="Filename filter (fnmatch, default: *)")
    parser.add_argument("--flags", default=None,
                        help="Space-separated modifiers, e.g. \"--context 2 --ignore-case\"")
    parser.add_argument("--base-dir", default=None)

    args = parser.parse_args()

    print(sift_files_tool_new(
        path=args.path, pattern=args.pattern, glob=args.glob, flags=args.flags,
        base_dir=args.base_dir,
    ))


if __name__ == "__main__":
    main()
