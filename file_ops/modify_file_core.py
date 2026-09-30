#!/usr/bin/env python3
"""Standalone modify_file tool - extracted from Hermes Agent file_tools.py.

Patches a SINGLE file with exact find/replace (old_string -> new_string).

NOTE: the multi-file "V4A patch" mode of the original `patch` tool was REMOVED
in iteration 1 (project decision Q6, 2026-08-23): batch editing of several
files in one call is considered meaningless by nature. The mock methods
MockFileOperations.patch_v4a / _apply_diff_block remain in mocks.py as an
unused archive; this tool no longer references them.

CLI usage:
    python modify_file_core.py path/to/file --old "text to find" --new "replacement" [--replace-all] [--base-dir .]

Output: JSON with success status, resolved_path, files_modified."""

import json

# Import mock infrastructure (no Hermes dependencies).
# Plugin mode: imported as package `hermes_plugins.<slug>` -> relative import.
# Console mode: run directly (`python modify_file_core.py ...`) -> bare module.
try:  # plugin mode
    from .mocks import tool_error, resolve_path_for_tool, file_ops
    from .file_backend import FileBackend
except ImportError:  # console mode
    from mocks import tool_error, resolve_path_for_tool, file_ops  # type: ignore
    from file_backend import FileBackend  # type: ignore


def modify_file_tool(path: str = None, old_string: str = None,
                     new_string: str = None, replace_all: bool = False,
                     base_dir: str = None, task_id: str = "default") -> dict:
    """Patch a single file with exact find/replace.

    Args:
        path: File to patch (required)
        old_string: Text to find and replace (required)
        new_string: Replacement text (required)
        replace_all: Replace all occurrences instead of just the first
        base_dir: Base directory for resolving relative paths

    Returns:
        dict with success status, resolved_path, files_modified
    """
    try:
        # ── Validate parameters ────────────────────────────────────────
        if not path:
            return tool_error("path required")
        if old_string is None or new_string is None:
            return tool_error("old_string and new_string required")

        # Route through the active backend (real Hermes env in plugin mode, mock in console).
        be = FileBackend(task_id, mock_ops=file_ops, mock_resolve=resolve_path_for_tool)
        resolved_str = be.resolve(path, base_dir)

        # Count occurrences BEFORE the edit. The real backend's PatchResult
        # carries no occurrence count at all (only files_modified, a list of
        # PATHS - always length 1 for a single-file op, so it can never tell
        # apart "replaced once" from "replaced five times"). Bug found via
        # live-agent testing 2026-08-24: replace_all=True with 2 real matches
        # reported replacements:1. Best-effort pre-count instead of a fixed
        # guess; None (falls back to 1) if the read itself fails for any reason.
        pre_count = None
        pre_truncated = False
        if replace_all:
            try:
                pre = be.ops.read_file(resolved_str, offset=1, limit=1_000_000)
                if not getattr(pre, "error", None):
                    pre_count = (pre.content or "").count(old_string)
                    pre_truncated = bool(getattr(pre, "truncated", False))
            except Exception:
                pass

        result = be.ops.patch_replace(resolved_str, old_string, new_string, replace_all)

        if result.error:
            return {
                "error": result.error,
                "_hint": _generate_patch_hint(result.error),
            }

        resp = {
            "success": True,
            "error": None,
            "resolved_path": resolved_str,
            "files_modified": [resolved_str],
            "replacements": pre_count if pre_count is not None else 1,
        }
        if replace_all and pre_truncated:
            resp["_hint"] = (
                "replacements count may be a lower bound - the file exceeded "
                "the window used to pre-count occurrences."
            )
        return resp

    except Exception as e:
        return {"error": str(e)}


def _generate_patch_hint(error_msg: str) -> str | None:
    """Generate helpful hint for patch failures."""
    if "Could not find" in error_msg or "not found" in error_msg.lower():
        return (
            "old_string not found. Use view_file to verify the current content, "
            "or ensure old_string matches exactly including whitespace and indentation."
        )
    return None


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Modify a file with exact find/replace (Hermes-style)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python modify_file_core.py myfile.py \\
      --old "def old_func(): pass" \\
      --new "def new_func(): return True"

  python modify_file_core.py file.txt -o "text to find" -n "replacement" --replace-all""",
    )

    parser.add_argument("path", help="File to patch")
    parser.add_argument("--old", "-o", type=str, default=None,
                        help="Text to find and replace (required)")
    parser.add_argument("--new", "-n", type=str, default=None,
                        help="Replacement text (required)")
    parser.add_argument("--replace-all", dest="replace_all", action="store_true",
                        help="Replace all occurrences instead of first match only")
    parser.add_argument("--base-dir", type=str, default=None,
                        help="Base directory for resolving relative paths (default: current dir)")

    args = parser.parse_args()

    result = modify_file_tool(
        path=args.path,
        old_string=args.old,
        new_string=args.new,
        replace_all=args.replace_all,
        base_dir=args.base_dir,
    )

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
