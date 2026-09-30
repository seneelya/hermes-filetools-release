#!/usr/bin/env python3
"""Standalone search_tool - extracted from Hermes Agent file_tools.py.

Search files using grep (ripgrep preferred if available):
- Content search: find text/regex inside files
- File search: find files by name pattern

CLI usage (content):
    python search_tool.py "pattern" --target content [--file-glob "*.py"] [--limit 50]

CLI usage (files):
    python search_tool.py "*.json" --target files

Output: JSON with matches, total_count, truncated flag."""

import json
import os
import sys
from pathlib import Path

# Import mock infrastructure (no Hermes dependencies).
# Plugin mode: imported as package `hermes_plugins.<slug>` -> relative import.
# Console mode: run directly (`python sift_files_core.py ...`) -> bare module.
try:  # plugin mode
    from .mocks import (
        tool_error,
        resolve_path_for_tool,
        file_ops,
        normalize_search_pagination,
    )
    from .file_backend import FileBackend
except ImportError:  # console mode
    from mocks import (  # type: ignore
        tool_error,
        resolve_path_for_tool,
        file_ops,
        normalize_search_pagination,
    )
    from file_backend import FileBackend  # type: ignore


def sift_files_tool(pattern: str, target: str = "content", path: str = ".",
                    file_glob: str = None, limit: int = 50, offset: int = 0,
                    output_mode: str = "content", context: int = 0,
                    base_dir: str = None, task_id: str = "default") -> dict:
    """Search for content or files.
    
    Args:
        pattern: Regex pattern to search for (content mode) or glob pattern (files mode)
        target: "content" (search inside files) or "files" (find by filename)
        path: Directory to search in
        file_glob: Filter files by extension/glob pattern (e.g. "*.py")
        limit: Maximum results to return (default: 50, max: 500)
        offset: Skip first N results for pagination
        output_mode: "content" (matches with lines), "files_only", or "count"
        context: Number of context lines before/after each match
        base_dir: Base directory for resolving relative paths
    
    Returns:
        dict with matches, total_count, truncated flag
    """
    try:
        # Normalize pagination
        offset, limit = normalize_search_pagination(offset, limit)

        # Route through the active backend (real Hermes env in plugin mode, mock in console).
        be = FileBackend(task_id, mock_ops=file_ops, mock_resolve=resolve_path_for_tool)
        resolved_str = be.resolve(path, base_dir)

        if not be.is_real and not Path(resolved_str).exists():
            return {"error": f"Path not found: {path}"}

        # Perform the search via the active backend.
        result = be.ops.search(
            pattern=pattern,
            path=resolved_str,
            target=target,
            file_glob=file_glob,
            limit=limit,
            offset=offset,
            output_mode=output_mode,
            context=context,
        )

        if result.error:
            return {"error": result.error}

        # Build response based on output mode
        matches = getattr(result, 'matches', []) or []
        total_count = getattr(result, 'total_count', len(matches))
        
        resp = {
            "matches": matches,
            "total_count": total_count,
            "truncated": bool(getattr(result, 'truncated', False)),
        }

        # Add pagination hint when truncated
        if resp["truncated"]:
            next_offset = offset + limit
            resp["_hint"] = (
                f"Results truncated. Use offset={next_offset} to see more, "
                "or narrow with a more specific pattern or file_glob."
            )

        return resp

    except Exception as e:
        return {"error": str(e)}


def main():
    """CLI entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Search files (Hermes-style) - content search via grep or file name search",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:

Content search:
  python search_tool.py "def main" --target content --path ./src
  
  python search_tool.py "TODO|FIXME" --file-glob "*.py" --context 2
  
File name search:
  python search_tool.py "*.json" --target files --path /project/configs

Files only (no match text):
  python search_tool.py "import os" --output-mode files_only

Count matches per file:
  python search_tool.py "error|warn" --output-mode count"""
    )
    
    parser.add_argument("pattern", help="Pattern to search for")
    parser.add_argument("--target", "-t", type=str, default="content",
                        choices=["content", "files"],
                        help="Search target: content (inside files) or files (by name)")
    parser.add_argument("--path", "-p", type=str, default=".",
                        help="Directory to search in (default: current dir)")
    parser.add_argument("--file-glob", "-g", type=str, default=None,
                        help="Filter by file pattern (e.g. '*.py')")
    parser.add_argument("--limit", "-l", type=int, default=50,
                        help="Maximum results to return (default: 50, max: 500)")
    parser.add_argument("--offset", "-o", type=int, default=0,
                        help="Skip first N results")
    parser.add_argument("--output-mode", "-m", type=str, default="content",
                        choices=["content", "files_only", "count"],
                        help="Output format (default: content)")
    parser.add_argument("--context", "-c", type=int, default=0,
                        help="Context lines before/after each match")
    parser.add_argument("--base-dir", type=str, default=None,
                        help="Base directory for resolving relative paths (default: current dir)")
    
    args = parser.parse_args()

    result = sift_files_tool(
        pattern=args.pattern,
        target=args.target,
        path=args.path,
        file_glob=args.file_glob,
        limit=args.limit,
        offset=args.offset,
        output_mode=args.output_mode,
        context=args.context,
        base_dir=args.base_dir,
    )

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
