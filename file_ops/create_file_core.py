#!/usr/bin/env python3
"""Standalone write_file tool - extracted from Hermes Agent file_tools.py.

Writes content to a file (creates parent directories), without requiring Hermes infrastructure.

CLI usage:
    python write_file_tool.py path/to/file --content "file contents" [--base-dir .]
    
Or via stdin:
    echo "hello world" | python write_file_tool.py path/to/file --stdin

Output: JSON with success status, file_size, resolved_path."""

import json
import os
import sys
from pathlib import Path

# Import mock infrastructure (no Hermes dependencies).
# Plugin mode: imported as package `hermes_plugins.<slug>` -> relative import.
# Console mode: run directly (`python create_file_core.py ...`) -> bare module.
try:  # plugin mode
    from .mocks import (
        tool_error,
        resolve_path_for_tool,
        file_ops,
    )
    from .file_backend import FileBackend
except ImportError:  # console mode
    from mocks import (  # type: ignore
        tool_error,
        resolve_path_for_tool,
        file_ops,
    )
    from file_backend import FileBackend  # type: ignore


def create_file_tool(path: str, content: str, base_dir: str = None,
                     task_id: str = "default") -> dict:
    """Write content to a file.
    
    Creates parent directories if they don't exist. Overwrites existing files completely.
    
    Args:
        path: File path to write (absolute or relative to base_dir)
        content: Content to write
        base_dir: Base directory for resolving relative paths
    
    Returns:
        dict with success status, file_size, resolved_path
    """
    try:
        # Route through the active backend (real Hermes env in plugin mode, mock in console).
        be = FileBackend(task_id, mock_ops=file_ops, mock_resolve=resolve_path_for_tool)
        resolved_str = be.resolve(path, base_dir)

        # ── Guard: reject read_file output format as content ───────────
        if _is_internal_file_tool_content(content):
            return tool_error(
                "Refusing to write internal read_file display text as file content. "
                "Strip line-number prefixes or reconstruct the intended file contents."
            )

        # Perform the write via the active backend (creates parent directories).
        result = be.ops.write_file(resolved_str, content)

        if result.error:
            return {"error": result.error}

        return {
            "success": True,
            "error": None,
            "file_size": result.file_size,
            "resolved_path": resolved_str,
        }

    except Exception as e:
        return {"error": str(e)}


def _is_internal_file_tool_content(content: str) -> bool:
    """Check if content looks like read_file output with line numbers.

    Prevents accidentally writing Hermes-style formatted output back to files.

    Real core's numbering format (tools/file_operations.py::_add_line_numbers)
    is the compact ``N|content`` gutter, NOT zero/space-padded to a fixed
    width - that padded format was replaced for token-efficiency reasons.
    This guard used to require a >=4-digit numeric prefix, which meant it
    could only ever fire on a file with 1000+ lines already numbered - i.e.
    never, in practice. Bug found via live-agent testing 2026-08-24 (a
    2-line "1|foo\n2|bar" sample wasn't rejected). Now matches the real
    (unpadded) format instead.
    """
    lines = content.split('\n')
    if not lines or len(lines) < 2:
        return False

    # Check for pattern: <digits>|content (compact line-number gutter)
    numbered_lines = 0
    for line in lines[:min(10, len(lines))]:  # Sample first 10 lines
        if '|' not in line:
            continue

        prefix = line.split('|', 1)[0]
        if prefix.isdigit():
            numbered_lines += 1

    # If most sampled lines have line-number prefixes, it's read_file output
    return numbered_lines >= 3


def main():
    """CLI entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Write content to a file (Hermes-style)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python write_file_tool.py myfile.txt --content "hello world"
  python write_file_tool.py ./src/main.py --file input.py    # Read from file
  cat content.txt | python write_file_tool.py dest.txt --stdin
  python write_file_tool.py file.txt --base-dir /project"""
    )
    
    parser.add_argument("path", help="Path to file to write")
    parser.add_argument("--content", "-c", type=str, default=None,
                        help="Content to write directly")
    parser.add_argument("--file", "-f", type=str, default=None,
                        help="Read content from this file instead of --content/--stdin")
    parser.add_argument("--base-dir", type=str, default=None,
                        help="Base directory for resolving relative paths (default: current dir)")
    
    args = parser.parse_args()

    # Determine content source
    if args.content is not None:
        content = args.content
    elif args.file is not None:
        try:
            input_path = resolve_path_for_tool(args.file, args.base_dir)
            content = input_path.read_text(encoding='utf-8')
        except Exception as e:
            print(json.dumps({"error": f"Failed to read source file {args.file}: {e}"}))
            sys.exit(1)
    else:
        # Read from stdin if neither --content nor --file provided
        content = sys.stdin.read()

    result = create_file_tool(args.path, content, args.base_dir)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
