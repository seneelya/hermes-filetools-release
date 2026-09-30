#!/usr/bin/env python3
"""Standalone read_file tool - extracted from Hermes Agent file_tools.py.

Reads files with pagination and line numbers, without requiring Hermes infrastructure.

CLI usage:
    python read_file_tool.py path/to/file [--offset 1] [--limit 2000] [--base-dir .]

Output: JSON with content (line-numbered), total_lines, file_size, truncated flag."""

import json
import os
import sys
from pathlib import Path

# Import mock infrastructure (no Hermes dependencies).
# Plugin mode: imported as package `hermes_plugins.<slug>` -> relative import.
# Console mode: run directly (`python view_file_core.py ...`) -> bare module.
try:  # plugin mode
    from .mocks import (
        tool_error,
        resolve_path_for_tool,
        file_ops,
        has_binary_extension,
        get_read_block_error,
        redact_sensitive_text,
        normalize_read_pagination,
        truncate_to_char_budget,
        get_max_read_chars,
        is_extractable_document,
        extract_document_bytes,
    )
    from .file_backend import FileBackend, add_line_numbers
except ImportError:  # console mode
    from mocks import (  # type: ignore
        tool_error,
        resolve_path_for_tool,
        file_ops,
        has_binary_extension,
        get_read_block_error,
        redact_sensitive_text,
        normalize_read_pagination,
        truncate_to_char_budget,
        get_max_read_chars,
        is_extractable_document,
        extract_document_bytes,
    )
    from file_backend import FileBackend, add_line_numbers  # type: ignore


# Constants from original Hermes tool (kept for compatibility)
_LARGE_FILE_HINT_BYTES = 1024 * 1024  # 1MB

# Document extraction size limit
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024  # 50MB


def view_file_tool(path: str, offset: int = 1, limit: int = 2000, base_dir: str = None,
                   task_id: str = "default") -> dict:
    """Read a file with pagination and line numbers.

    Args:
        path: File path to read (absolute or relative)
        offset: Starting line number (1-indexed)
        limit: Maximum lines to return
        base_dir: Base directory for resolving relative paths (console/mock mode only;
                  in plugin mode the task's real working dir is used instead)
        task_id: Hermes task id — selects the live terminal environment (docker/host)
                 that built-in tools operate on. Ignored in console mode.

    Returns:
        dict with content, total_lines, file_size, truncated flag
    """
    try:
        # Normalize pagination
        offset, limit = normalize_read_pagination(offset, limit)

        # Route through the active backend: real Hermes env (plugin mode) or mock (console).
        be = FileBackend(task_id, mock_ops=file_ops, mock_resolve=resolve_path_for_tool)
        resolved_str = be.resolve(path, base_dir)

        # ── Structured-document extraction (checked BEFORE the binary guard
        # below - these extensions ARE opaque zip/binary containers, but
        # extractable ones; has_binary_extension's own blocklist also lists
        # .docx/.xlsx/.pptx/.pdf, so checking it first would reject every one
        # of them before ever reaching this branch - that was a real, silent
        # bug (found 2026-08-25): only .ipynb isn't in that blocklist, so it
        # was the only format that ever actually reached extraction) ───────
        if is_extractable_document(resolved_str):
            result = be.ops.read_file_bytes(resolved_str, max_bytes=MAX_DOCUMENT_BYTES)

            if result.error:
                return {"error": result.error}
            if not result.base64_content:
                return tool_error(f"Could not read '{path}' for extraction (empty read).")

            import base64
            try:
                document_bytes = base64.b64decode(result.base64_content, validate=True)
                extracted_text = extract_document_bytes(document_bytes, resolved_str)
            except Exception as e:
                return tool_error(f"Failed to extract '{path}': {e}")

            if extracted_text is None:
                ext = Path(resolved_str).suffix.lower()
                return tool_error(
                    f"'{path}' ({ext}) extraction is not supported yet. "
                    "Not falling through to a raw read - this is a binary container, "
                    "not text."
                )

            lines = extracted_text.splitlines()
            total_lines = len(lines)
            end_line = offset + limit - 1
            page_text = "\n".join(lines[offset - 1:end_line])

            result_dict = {
                "content": add_line_numbers(page_text, offset) if page_text else "",
                "total_lines": total_lines,
                "file_size": result.file_size,
                "truncated": total_lines > end_line,
                "extracted_document": True,
            }

            # Handle truncation with pagination hint
            if result_dict["truncated"]:
                result_dict["hint"] = (
                    f"Use offset={end_line + 1} to continue reading "
                    f"(showing {offset}-{min(end_line, total_lines)} of {total_lines} lines)"
                )

            # Character budget truncation
            content_len = len(result_dict["content"])
            max_chars = get_max_read_chars()

            if content_len > max_chars:
                trimmed, lines_kept, first_line_exceeded = truncate_to_char_budget(
                    result_dict["content"], max_chars
                )
                next_offset = offset + lines_kept
                shown_end = offset + lines_kept - 1

                result_dict["content"] = trimmed
                result_dict["truncated"] = True
                result_dict["truncated_by"] = "chars"
                result_dict["next_offset"] = next_offset
                result_dict["hint"] = (
                    f"Output truncated at the {max_chars:,}-char read budget "
                    f"after {lines_kept} line(s) (showing lines {offset}-"
                    f"{shown_end} of {total_lines}). Use offset={next_offset} to continue."
                )

                if first_line_exceeded:
                    result_dict["hint"] += (
                        " Note: the first line alone exceeded the budget and was clamped mid-line."
                    )

            return result_dict

        # ── Binary extension guard (genuinely opaque binaries only, now that
        # extractable documents are handled above) ────────────────────────
        if has_binary_extension(resolved_str):
            ext = Path(resolved_str).suffix.lower()
            return tool_error(
                f"Cannot read binary file '{path}' ({ext}). "
                "Use a different tool for images or terminal to inspect binary files."
            )

        # ── Perform the read via the active backend ───────────────────
        result = be.ops.read_file(resolved_str, offset, limit)
        
        if result.error:
            return {"error": result.error}
        
        result_dict = {
            "content": result.content or "",
            "total_lines": result.total_lines,
            "file_size": result.file_size,
            "truncated": bool(result.truncated),
        }

        # ── Character budget truncation (for long lines) ──────────────
        content_len = len(result_dict["content"])
        max_chars = get_max_read_chars()
        
        if content_len > max_chars:
            trimmed, lines_kept, first_line_exceeded = truncate_to_char_budget(
                result_dict["content"], max_chars
            )
            next_offset = offset + lines_kept
            shown_end = offset + lines_kept - 1
            
            result_dict["content"] = trimmed
            result_dict["truncated"] = True
            result_dict["truncated_by"] = "chars"
            result_dict["next_offset"] = next_offset
            total_lines = result_dict.get("total_lines", "unknown")
            
            result_dict["hint"] = (
                f"Output truncated at the {max_chars:,}-char read budget after "
                f"{lines_kept} line(s) (showing lines {offset}-{shown_end} of "
                f"{total_lines}). Use offset={next_offset} to continue."
            )
            
            if first_line_exceeded:
                result_dict["hint"] += (
                    " Note: the first line alone exceeded the budget and was clamped mid-line."
                )

        # ── Large file hint ───────────────────────────────────────────
        file_size = result_dict.get("file_size", 0)
        if (file_size > _LARGE_FILE_HINT_BYTES 
            and limit > 200 
            and result_dict.get("truncated")):
            result_dict["_hint"] = (
                f"This file is large ({file_size:,} bytes). "
                "Consider reading only the section you need with offset and limit."
            )

        return result_dict
        
    except Exception as e:
        return {"error": str(e)}


def main():
    """CLI entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Read a file with pagination and line numbers (Hermes-style)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python read_file_tool.py myfile.txt                    # Read first 2000 lines
  python read_file_tool.py myfile.txt --offset 100       # Start from line 100  
  python read_file_tool.py myfile.txt --limit 50         # Only 50 lines
  python read_file_tool.py ./src/main.py --base-dir /project  # Custom base dir"""
    )
    
    parser.add_argument("path", help="Path to file to read")
    parser.add_argument("--offset", type=int, default=1, 
                        help="Starting line number (1-indexed, default: 1)")
    parser.add_argument("--limit", type=int, default=2000,
                        help="Maximum lines to return (default: 2000, max: 2000)")
    parser.add_argument("--base-dir", type=str, default=None,
                        help="Base directory for resolving relative paths (default: current dir)")
    
    args = parser.parse_args()
    
    result = view_file_tool(args.path, args.offset, args.limit, args.base_dir)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
