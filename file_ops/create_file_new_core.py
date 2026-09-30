#!/usr/bin/env python3
"""write_file "new" path core — plain-text response, optional insert_line.

Implements Vision01__write.md: the response is plain descriptive text (never
JSON: "Overwritten (N lines)." / "Created new file (N lines)." / ...
/ "ERROR: ..."), and content can be spliced into an existing file via
insert_line instead of only ever overwriting it whole. See that document for
the full design rationale (line-counting formula, insert_line's numeric
semantics, EOL-preservation, the mtime-anchor).

CLI usage:
    python create_file_new_core.py PATH --content TEXT [--insert-line N] [--base-dir DIR]
"""

import base64
import sys

# Import mock infrastructure (no Hermes dependencies).
# Plugin mode: imported as package `hermes_plugins.<slug>` -> relative import.
# Console mode: run directly -> bare module.
try:  # plugin mode
    from .mocks import resolve_path_for_tool, file_ops
    from .file_backend import FileBackend
    from .create_file_core import _is_internal_file_tool_content
    from ._new_format import (to_lines_and_trailing_nl, join_lines, count_lines,
                              format_mtime, detect_eol, ui_envelope, minimal_diff)
    from .markers import strip_marker_lines
    from .flags import (has_flag, parse_flags_report, flag_int, unknown_flags,
                        FlagError, QUOTE_HINT)
    from ._tool_text import op_line
    from . import _ledger
except ImportError:  # console mode
    from mocks import resolve_path_for_tool, file_ops  # type: ignore
    from file_backend import FileBackend  # type: ignore
    from create_file_core import _is_internal_file_tool_content  # type: ignore
    from _new_format import (to_lines_and_trailing_nl, join_lines, count_lines,  # type: ignore
                             format_mtime, detect_eol, ui_envelope, minimal_diff)
    from markers import strip_marker_lines  # type: ignore
    from flags import (has_flag, parse_flags_report, flag_int, unknown_flags,  # type: ignore
                       FlagError, QUOTE_HINT)
    from _tool_text import op_line  # type: ignore
    import _ledger  # type: ignore


# write_file always writes UTF-8 today - be.ops.write_file() has no encoding
# parameter (mock: resolved.write_text(content, encoding='utf-8'); the real
# backend's write_file is str-based too). flags="--encoding NAME" to WRITE
# in a different encoding is a separate, not-yet-implemented feature (needs
# a bytes-write path on the real backend first, see Vision01__write.md) -
# this constant just names what's already true today, so the response can
# say so honestly and the model can see "I wrote UTF-8, not what I wanted."
WRITE_ENCODING = "utf-8"


# Same order-of-magnitude cap as view_file_core's document-extraction limit -
# insert_line needs the file's full existing bytes (not a paginated slice) to
# splice correctly, so there is no smaller "just enough" read here.
MAX_INSERT_READ_BYTES = 50 * 1024 * 1024


# Every flag this tool answers to; anything else is refused BY NAME (Plan08
# risk 1). Refusing rather than ignoring matters most here: a mistyped
# "--insert-line=5" that got dropped silently would OVERWRITE the file the
# model meant to splice into. `insert_line` is kept as an alias of
# `--insert-line` because that is the name the model knew as a parameter.
KNOWN_FLAGS = frozenset({"insert-line", "insert_line", "force", "help"})
_INSERT_LINE_FLAGS = ("insert-line", "insert_line")


def _command_text(path, content, insert_line, flags) -> str:
    """What the model asked for, as one readable body (Plan11-Task03).

    `content` is the unbounded part of a write and the reason this tool needs a
    sidecar. Kept as a labelled section rather than a repr: the sidecar is read
    by a HUMAN in the panel, and stage B will stub these very arguments out of
    the model's context, leaving this the only surviving copy of what was
    requested.
    """
    parts = [f"write_file {path or ''}".rstrip()]
    if insert_line is not None:
        parts.append(f"insert_line: {insert_line}")
    if flags:
        parts.append(f"flags: {flags}")
    if content:
        parts.append(f"--- content ---\n{content}")
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
            # Plan11-Task02 — filled by `_do_insert`/`_do_overwrite`, which are
            # where the arithmetic already is. Absent on an error path, and 0
            # is the right "unknown": `note_op` omits the key instead of
            # writing a line number nobody computed.
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


def create_file_tool_new(path: str, content: str, insert_line: int = None,
                         base_dir: str = None, task_id: str = "default",
                         session_id: str = "",
                         mtime_enabled: bool = True, flags: str = None,
                         ui_json: str = "off") -> str:
    """Write or insert *content*, returning a plain-text description of what happened.

    `ui_json` works exactly as it does for patch (CONTRACT.md §19): the
    plain-text answer is the reference form and stays untouched; the optional
    JSON block goes ABOVE it. write_file needs this for the same reason patch
    does - the desktop app lists it in FILE_EDIT_TOOL_NAMES too, so it also
    renders an empty diff card and shows nothing.
    """
    facts: dict = {}
    # Taken BEFORE the write, so an operation that then fails still has an
    # address: "0157 tried to write and errored" is worth being able to point
    # at. No FileTag tail — this tool resolves no tag of its own, and hashing a
    # file just to decorate the address would be a read we do not otherwise
    # need (Plan09: the number is the address, the tail only supplements).
    op = _ledger.next_op(session=session_id, task_id=task_id)
    text = _write_impl(path, content, insert_line, base_dir, task_id,
                       mtime_enabled, flags, facts)
    if text.lstrip().startswith("ERROR"):
        facts["success"] = False
        facts.setdefault("error", text.strip().splitlines()[0])
    elif facts:
        facts.setdefault("success", True)
    # Emitted only on SUCCESS, and that is a contract rather than a nicety:
    # three different consumers decide "did this fail" by the answer STARTING
    # with "ERROR:" (the check just above, `_tool_text._facade_exit_code`, the
    # panel's `_kind`), so an address printed above an error would report every
    # failure as a success. A refusal is also the one answer an address buys
    # nothing on — one line, and nobody needs to ask whether it is still worth
    # keeping. The number is spent either way, so the sequence has gaps:
    # monotonic, not contiguous.
    first = op_line(op)
    if first and facts.get("success"):
        text = first + "\n" + text
    out = ui_envelope(ui_json, facts, text)
    # Записываем ТОЛЬКО успех и ТОЛЬКО финальный текст (после конверта) — именно он ложится в
    # строку messages, и именно его компакция потом подменяет своей заглушкой.
    if facts.get("success"):
        _note_op("write_file", out, path, session_id, task_id, facts, op,
                 command=_command_text(path, content, insert_line, flags))
    return out


def _write_impl(path, content, insert_line, base_dir, task_id, mtime_enabled,
                flags, facts: dict) -> str:
    parsed_flags, quoted = parse_flags_report(flags)
    unknown = unknown_flags(parsed_flags, KNOWN_FLAGS)
    if unknown:
        listed = ", ".join(f"--{name}" for name in unknown)
        return (f"ERROR: write_file: unknown flag(s): {listed}. Values are "
                'written after a space ("--insert-line 20", not '
                '"--insert-line=20"). Nothing was written. Call '
                'flags="--help" for the full list.')
    # The flag is the advertised way in (Plan08); `insert_line=` is still
    # accepted unadvertised, and the flag wins on conflict.
    try:
        for name in _INSERT_LINE_FLAGS:
            if name in parsed_flags:
                insert_line = flag_int(parsed_flags, name, insert_line)
                break
    except FlagError as e:
        return f"ERROR: write_file: {e} Nothing was written."

    try:
        be = FileBackend(task_id, mock_ops=file_ops, mock_resolve=resolve_path_for_tool)
        resolved_str = be.resolve(path, base_dir)
        facts["files_modified"] = [resolved_str]

        if _is_internal_file_tool_content(content):
            return ("ERROR: Refusing to write internal read_file display text as file "
                    "content. Strip line-number prefixes or reconstruct the intended "
                    "file contents.")

        removed_count = 0
        if not has_flag(flags, "force"):
            content, removed_count = strip_marker_lines(content, resolved_str)

        # `facts` goes IN so the position/length land where the numbers already
        # live (Plan11-Task02). Recomputing them out here would be a second,
        # divergable copy of the insert arithmetic — the one thing an insert
        # must not have, since the whole point of recording the position is
        # that the panel can shift everything below it correctly.
        if insert_line is None:
            outcome = _do_overwrite(be, resolved_str, content, facts)
        else:
            outcome = _do_insert(be, resolved_str, content, insert_line, facts)

        if isinstance(outcome, str):  # error path returns the "ERROR: ..." line directly
            return outcome

        message, final_content = outcome
        write_result = be.ops.write_file(resolved_str, final_content)
        if write_result.error:
            return f"ERROR: {write_result.error}"

        suffix = ""
        if removed_count:
            suffix += f" Removed {removed_count} marker-like line(s) before writing."
        if quoted:
            suffix += " " + QUOTE_HINT
        suffix += f" Encoding: {WRITE_ENCODING}, {detect_eol(final_content)}."
        if resolved_str != path:
            suffix += f" Resolved to: {resolved_str}."
        if mtime_enabled:
            mtime = be.stat_mtime(resolved_str)
            if mtime is not None:
                suffix += f" Modified: {format_mtime(mtime)}."

        # `added` counts what this call actually put in. A full overwrite has no
        # "removed" side we could show without dumping the whole previous file,
        # and dumping code is exactly what the envelope must not do - so only an
        # insert contributes a diff (the inserted lines, prefixed "+").
        facts["added"] = count_lines(content)
        if insert_line is not None:
            facts["diff"] = minimal_diff("", content)
        return message + suffix

    except Exception as e:
        return f"ERROR: {e}"


def _peek_existing(be, resolved_str: str):
    """Best-effort read of the file's current raw content.

    Returns (existed, content). Both "doesn't exist yet" AND "can't be read"
    (permission error, too large for MAX_INSERT_READ_BYTES, ...) come back as
    (False, "") - there is nothing safe to splice into either way, and the
    write_file call right after this surfaces any genuine I/O error on its
    own, so nothing gets silently swallowed.
    """
    result = be.ops.read_file_bytes(resolved_str, max_bytes=MAX_INSERT_READ_BYTES)
    if result.error:
        return False, ""
    if not result.base64_content:
        return True, ""  # exists, empty file
    try:
        return True, base64.b64decode(result.base64_content, validate=True).decode(
            "utf-8", errors="replace"
        )
    except Exception:
        return True, ""


def _do_overwrite(be, resolved_str: str, content: str, facts: dict = None):
    existed, previous = _peek_existing(be, resolved_str)
    total = count_lines(content)
    message = (f"Overwritten ({total} lines)." if existed
               else f"Created new file ({total} lines).")
    # An overwrite touches the WHOLE file, so the range is the whole file
    # (Plan11-Task02). Saying 1-total rather than leaving it blank is what lets
    # the panel show "everything under here is new", which is the truth.
    if facts is not None:
        facts.update(from_line=1, to_line=total, total=total)
        # `removed` — the lines this overwrite DESTROYED. Recorded 2026-09-08,
        # after the panel proved it needs the other half of the sum: with only
        # `added`, an overwrite of a 4-line file with 4 lines reads as "+4", so
        # the consumer shifted every earlier range past the end of the file and
        # raised a false out-of-sync warning (verified on a live ledger).
        #
        # Only when the previous content was actually SEEN. `_peek_existing`
        # reports (False, "") both for "absent" and for "could not be read", and
        # a `removed: 0` we did not observe would be a claim, not a fact — the
        # exact failure this field exists to prevent.
        if existed:
            facts["removed"] = count_lines(previous)
    return message, content


def _do_insert(be, resolved_str: str, content: str, insert_line: int,
               facts: dict = None):
    _existed, existing = _peek_existing(be, resolved_str)
    existing_lines, existing_trailing = to_lines_and_trailing_nl(existing)
    total_before = len(existing_lines)

    # Doesn't exist, or exists but empty (0 lines either way) - insert_line=0
    # (or any negative) is valid: the whole content just becomes the file,
    # verbatim. A positive insert_line references a line that can't exist yet.
    if total_before == 0:
        if insert_line > 0:
            return (f"ERROR: insert_line={insert_line} but the file is empty (0 lines) - "
                    f"there is no line {insert_line} to insert at. Use insert_line=0 "
                    "(or a negative number) to create the file from this content.")
        total = count_lines(content)
        # Empty/absent file: the insert degenerates into "the content IS the
        # file", so the range is the whole file, same as an overwrite.
        if facts is not None:
            facts.update(from_line=1, to_line=total, total=total)
        return f"Created new file ({total} lines).", content

    content_lines, _content_trailing = to_lines_and_trailing_nl(content)

    # EOL normalization: only the INSERTED content's line endings adapt to the
    # existing file's style (CRLF vs LF) - never a restructure of the existing
    # file's own bytes, which are carried through untouched in existing_lines.
    eol = "\r\n" if "\r\n" in existing else "\n"
    if eol == "\r\n":
        content_lines = [ln + "\r" for ln in content_lines]

    inserted_n = len(content_lines)

    if insert_line == 0:
        position = 0
    elif insert_line > 0:
        if insert_line > total_before:
            return (f"ERROR: insert_line={insert_line} but the file only has "
                    f"{total_before} lines - there is no line {insert_line} to insert at.")
        position = insert_line - 1
    else:
        position = total_before  # append

    new_lines = existing_lines[:position] + content_lines + existing_lines[position:]
    final_content = join_lines(new_lines, existing_trailing)
    final_total = len(new_lines)

    if insert_line == 0:
        message = f"Inserted {inserted_n} lines at start (file now {final_total} lines total)."
    elif insert_line > 0:
        message = (f"Inserted {inserted_n} lines at line {insert_line} "
                   f"(file now {final_total} lines total).")
    else:
        message = f"Appended {inserted_n} lines (file now {final_total} lines total)."

    # The inserted block's OWN range in the NEW numbering (Plan11-Task02).
    # `position` is a 0-based index into the line list, so the first inserted
    # line is `position + 1` in 1-based file coordinates — the same coordinate
    # system every other ledger entry uses.
    #
    # This is the entry that makes the panel's fold possible at all: an insert
    # shifts every line below it, and without knowing WHERE it happened, every
    # earlier read of this file would be drawn at stale addresses.
    if facts is not None:
        facts.update(from_line=position + 1,
                     to_line=position + inserted_n,
                     total=final_total)

    return message, final_content


def main():
    """CLI entry point."""
    import argparse

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    parser = argparse.ArgumentParser(
        description="Write or insert content into a file (new path — plain-text response)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python create_file_new_core.py myfile.txt --content "hello world"
  python create_file_new_core.py myfile.txt --content "new line\\n" --insert-line 5
  python create_file_new_core.py myfile.txt --content "appended\\n" --insert-line -1""",
    )
    parser.add_argument("path", help="Path to file to write")
    parser.add_argument("--content", "-c", type=str, default=None, help="Content, directly")
    parser.add_argument("--file", "-f", type=str, default=None,
                        help="Read content from this file instead of --content/stdin")
    parser.add_argument("--insert-line", type=int, default=None,
                        help="0=start, 1..N=at existing line N, negative=append; omit to overwrite")
    parser.add_argument("--base-dir", type=str, default=None)
    parser.add_argument("--tool-flags", type=str, default=None,
                        help='Modifier string, e.g. --tool-flags "--force"')
    args = parser.parse_args()

    if args.content is not None:
        content = args.content
    elif args.file is not None:
        content = resolve_path_for_tool(args.file, args.base_dir).read_text(encoding="utf-8")
    else:
        content = sys.stdin.read()

    print(create_file_tool_new(args.path, content, args.insert_line, args.base_dir,
                               flags=args.tool_flags))


if __name__ == "__main__":
    main()
