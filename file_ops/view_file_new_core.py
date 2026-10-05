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
    from .flags import (parse_flags_report, flag_int, flag_bool, unknown_flags,
                        FlagError, QUOTE_HINT)
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
    from flags import (parse_flags_report, flag_int, flag_bool, unknown_flags,  # type: ignore
                       FlagError, QUOTE_HINT)
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
KNOWN_FLAGS = frozenset({
    "line", "count", "numbered",
    "force", "encoding", "no-anchors", "help",
})


def view_file_tool_new(path: str, line: int = 1, count: int = 2000, numbered: bool = False,
                       base_dir: str = None, task_id: str = "default",
                       session_id: str = "",
                       mtime_enabled: bool = True, symbols: dict = None,
                       flags: str = None, anchors_enabled: bool = True,
                       anchor_gap=None) -> str:
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
        line = flag_int(parsed_flags, "line", line)
        count = flag_int(parsed_flags, "count", count)
        numbered = flag_bool(parsed_flags, "numbered", numbered)
    except FlagError as e:
        return f"ERROR: read_file: {e}"

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

        # count=0 is its own mode ("just tell me about the file") - distinct
        # from an empty file's 0-0 block: no lines were even requested, so
        # there is no block to bound, and we skip straight to the header.
        metadata_only = (count == 0)

        if line < 1:
            line = 1
        if not metadata_only and count < 1:
            count = 1

        if not metadata_only:
            if total_lines == 0:
                page_lines = []
                actual_start, actual_end = 0, 0
            else:
                if line > total_lines:
                    return f"ERROR: line {line} is beyond the end of the file ({total_lines} lines)."
                start_idx = line - 1
                end_idx = min(start_idx + count, total_lines)
                page_lines = lines[start_idx:end_idx]
                actual_start = start_idx + 1
                actual_end = end_idx

        # C0 controls are valid codepoints, so the decode above leaves them
        # intact: a NUL or SUB out of a binary file would travel verbatim into
        # the model's context, into their `messages` row, and into whatever
        # writes the transcript - the owner lost a log capture to exactly this
        # three times before the cause was found (2026-09-03). Escaped per
        # LINE, so ranges and numbering are untouched.
        controls = 0
        if not metadata_only and page_lines:
            escaped = []
            for one in page_lines:
                one, n = escape_controls(one)
                controls += n
                escaped.append(one)
            page_lines = escaped

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
        header = [c(op_line(op))] if op else []
        header.append(c(f"{w['file']}: {resolved_str}"))
        header.append(c(
            f"{w['size']}: {total_lines} lines, {file_size} bytes, {len(text)} chars, "
            f"{eol}, max/avg line {max_len}/{avg_len:.1f}"
        ))

        if mtime_enabled:
            mtime = be.stat_mtime(resolved_str)
            if mtime is not None:
                header.append(c(f"{w['modified']}: {format_mtime(mtime)}"))

        if encoding_label:
            header.append(c(f"{w['encoding']}: {encoding_label}"))

        if encoding_label and encoding_label.endswith("(guessed)"):
            header.append(c(f"{w['hint']}: encoding was guessed - retry with "
                             'flags="--encoding NAME" if this looks wrong.'))

        # Accepting the quotes is not enough: unless the answer SAYS they were
        # removed, the model keeps writing them and the next tool that has no
        # such tolerance eats them silently. Same channel that taught it
        # `--encoding` (Plan08: flags are advertised in ANSWERS).
        if quoted:
            header.append(c(QUOTE_HINT))

        if controls:
            header.append(c(
                f"{w['control_chars']}: {controls} shown as pictures (␀-style) - "
                "this body is NOT a byte copy of the file."
            ))

        header.append(c(f"{w['file_tag']}: {tag}"))
        header.append(c(els["header_separator"]))

        if metadata_only:
            out = "\n".join(header)
            # count=0 showed the model no content at all. Still an entry: the
            # panel's job is to say what the model can lean on, and "it only
            # ever looked at the size" is an answer, not a gap.
            _note_read(out, session_id, task_id, resolved_str, 0, 0, 0, op)
            return out

        header.append(c(f"{els['block_open']} {tag} : {actual_start}-{actual_end}"))

        if numbered and page_lines:
            # The gutter already numbers every line - anchors would be pure
            # noise here. They exist precisely FOR the un-numbered default.
            body = add_line_numbers("\n".join(page_lines), actual_start).split("\n")
        elif page_lines and anchors_enabled and not no_anchors:
            body = place_anchors(
                page_lines, actual_start, c,
                symbols.get("anchor_symbol", DEFAULT_SYMBOLS["anchor_symbol"]), tag,
                min_gap=resolve_anchor_gap(anchor_gap),
            )
        else:
            body = page_lines

        footer = [c(f"{els['block_close']} {tag} : {actual_end}")]

        out = "\n".join(header + body + footer)
        _note_read(out, session_id, task_id, resolved_str,
                   actual_start, actual_end, len(page_lines), op,
                   total=total_lines, body=page_lines)
        return out

    except Exception as e:
        return f"ERROR: {e}"


def _note_read(out: str, session_id: str, task_id: str, resolved_str: str,
               start: int, end: int, lines: int, op: str = "",
               total: int = 0, body=None) -> None:
    """Record the read, and under no circumstances affect the return value.

    `body` — the page lines handed over; inside execute_code the ledger compares
    them with the script's answer (`deferred/17`), and never stores them.

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
            body=body,
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
