"""The tool dictionary: one place that says what our tools print and take (`deferred/08`).

The panel used to guess what it parses: header words as a constant (`HEADER_WORDS`), icons
as a constant (`TOOL_ICON`), body arguments as a list of names (`BODY_KEYS`). Every guess was
a second copy of a fact this side already holds, and copies drift silently — a renamed
header line just stops being coloured, no error anywhere. Now the dictionary goes to the
panel over `/config` (`dashboard/plugin_api.py::_tool_dictionary`), and the panel keeps its
constants only for the first frame, before `/config` answers.

What lives here and what does not:

- `HEADER` — per tool, `role -> word` of every header line the tool prints (`Size`, `Exit
  code`). A role is how the panel asks ("the exit-code word of terminal"), the word is what is
  printed. For the four JSON-wrapping tools it IS their label table (`LABELS`): their cores
  take it from here, so word and print cannot part. The four text tools still spell their
  words in f-strings; `tests/test_vocab.py` holds the two together until those are moved too.
  A JSON tool may print a key nobody listed (their answer grows) — then the word is the key,
  capitalised (`_tool_text._label_for`). Those cannot be known in advance.
- `CALL_BODY` — which call arguments are TEXT to read rather than a value to glance at
  (`content`, `old_string`). Not in the schema: a schema says `string`, not "a body".
- `SCHEMA_BY_PATH` — which `file_ops/*.yaml` a slot loads; `__init__.py` registers by it,
  the panel reads parameters from the same file.
- Icons are NOT here: an icon is a setting, it lives in the tool's block of `plugin.yaml`
  (`tools.<name>.icon`, owner 2026-10-05).

Loaded by path from the dashboard (no package there), so this module imports nothing of ours.

Adding or changing a tool: `__HQ/HowTo__Write_a_tool.md` (where every fact of a tool lives).
"""

from __future__ import annotations

# Lines every tool's answer may carry: the address (`_tool_text.OP_LABEL`), and the stamp a
# removed operation leaves (`_tool_text.drop_content`: tool, size, state).
COMMON = {
    "op": "Operation ID",
    "tool": "Tool",
    "size": "Size",
    "state": "State",
    # `flags.QUOTE_HINT` and the tools' own hints.
    "hint": "Hint",
}

# Label tables of the tools that wrap THEIR JSON (`_tool_text.render_json_result`).
# Moved here from the cores 2026-10-05; the comments on WHY a key gets a line stayed there.
LABELS = {
    "terminal": {
        "cwd": "Cwd",
        "session_id": "Session id",
        "pid": "Pid",
        "watch_patterns": "Watching",
        "approval": "Approval",
        "exit_code_meaning": "Note",
        "hint": "Hint",
        "sudo_auth_failed": "Sudo auth failed",
        "sudo_cache_cleared": "Sudo cache cleared",
        "status": "Status",
        "reason": "Reason",
        "retry_hint": "Retry hint",
        "traceback": "Traceback",
        "pty_note": "Pty note",
        "output_total_chars": "Output total chars",
        "full_output_path": "Full output path",
        "truncation_note": "Truncation note",
        "verification_evidence": "Verification evidence",
        "exit_code": "Exit code",
        "error": "Error",
    },
    "execute_code": {
        "status": "Status",
        "exit_code": "Exit code",
        "error": "Error",
        "tool_calls_made": "Tool calls",
        "duration_seconds": "Duration, s",
        "stdout_truncated": "Stdout truncated",
        "stdout_bytes_captured": "Stdout bytes captured",
        "stdout_bytes_total": "Stdout bytes total",
        "stdout_bytes_omitted": "Stdout bytes omitted",
        "warning": "Warning",
    },
    "process": {
        "status": "Status",
        "error": "Error",
        "session_id": "Session id",
        # Unlike terminal, `command` EARNS its header line here: the model sent a
        # session_id, not a command, so this is information it does not already
        # have - and acting on the wrong background process is a real mistake.
        "command": "Command",
        "cwd": "Cwd",
        "pid": "Pid",
        "exit_code": "Exit code",
        "uptime_seconds": "Uptime, s",
        "started_at": "Started at",
        "completion_reason": "Completion reason",
        "termination_source": "Termination source",
        "total_lines": "Total lines",
        "showing": "Showing",
        "timeout_note": "Note",
        "note": "Note",
        "process_running": "Process running",
        "bytes_written": "Bytes written",
        "message": "Message",
        "detached": "Detached",
        "closed": "Closed",
        "watch_patterns": "Watching",
        "watch_hit": "Watch hit",
        "notify_on_complete": "Notify on complete",
        "session_scoped": "Session scoped",
        "output_preview": "Output preview",  # only as a LIST-row label; see process_core
    },
    "vision_analyze": {
        "success": "Success",
        "error": "Error",
        "scale_note": "Scale note",
        "analysis": "Analysis",  # only ever a label if it is NOT the body
    },
}

# Header words of the four text tools, as their cores print them today.
HEADER = {
    "read_file": {
        "file": "File",
        "size": "Size",
        "modified": "Modified",
        "encoding": "Encoding",
        "control_chars": "Control chars",
        "file_tag": "FileTag",
    },
    # A write answers in one line of prose under its address; no header words of its own.
    "write_file": {
        # Inline in its one line of prose, not header lines of their own.
        "encoding": "Encoding",
        "modified": "Modified",
        "resolved_to": "Resolved to",
    },
    "patch": {
        "patched": "Patched",
        "unchanged": "Unchanged",
        "dry_run": "Dry run",
        "would_replace": "Would replace",
        "lines": "Lines",
        "replacements": "Replacements",
        "note": "Note",
    },
    "search_files": {
        "search": "Search",
        "found": "Found",
        "note": "Note",
    },
    **LABELS,
}

# Call arguments shown as text to read, not as a cell (panel `call-render.js`). The same
# names the panel's guess list held (owner, 2026-09-23), now per tool. `execute_code` takes
# THEIR schema; its body is still ours to name.
CALL_BODY = {
    "read_file": [],
    "write_file": ["content"],
    "patch": ["old_string", "new_string"],
    "search_files": ["pattern"],
    "terminal": [],
    "execute_code": ["code"],
    "process": [],
    "vision_analyze": [],
}

# Which schema file a slot registers with. `old`/`new` follow the slot's `path`; terminal,
# process and vision_analyze have only ours; execute_code registers THEIR schema (no file).
SCHEMA_BY_PATH = {
    "read_file": {"old": "view_file.yaml", "new": "view_file_new.yaml"},
    "write_file": {"old": "create_file.yaml", "new": "create_file_new.yaml"},
    "patch": {"old": "modify_file.yaml", "new": "modify_file_new.yaml"},
    "search_files": {"old": "sift_files.yaml", "new": "sift_files_new.yaml"},
    # terminal has only one implementation (calls hermes-agent's live tool and
    # reformats its result), so it has only the "new" key: `path: old` for this
    # slot means "don't register at all, leave upstream's own terminal", and
    # the loop returns before it ever needs a schema name.
    "terminal": {"new": "terminal.yaml"},
    "process": {"new": "process.yaml"},
    "vision_analyze": {"new": "vision_analyze.yaml"},
}


# Words of the body marks: `■BLOCK x1b : 1-20`, `■END x1b : 20`, `⚓x1b Line: 26`. The symbol in
# front is a setting (`formatting`); the word after it is ours.
MARKS = {
    "block_word": "BLOCK",
    "end_word": "END",
    "anchor_word": "Line",
}


def format_elements(symbols: dict) -> dict:
    """The four symbols plus the ELEMENTS made of them — the ONE place that knows a header
    separator is the separator symbol three times and a block opens with symbol + `BLOCK`.
    `symbols` must be complete (defaults merged by the caller: `_new_format.DEFAULT_SYMBOLS`)."""
    sym = {k: str(v) for k, v in (symbols or {}).items()}
    return {
        **sym,
        "header_separator": sym.get("separator_symbol", "") * 3,
        "block_open": sym.get("block_symbol", "") + MARKS["block_word"],
        "block_close": sym.get("block_symbol", "") + MARKS["end_word"],
    }


def placeholders(symbols: dict) -> dict:
    """Values for `{name}` placeholders in schema descriptions (`_schema_loader.fill`):
    formatting elements (`{block_open}`), mark words (`{anchor_word}`) and header words as
    `{tool.role}` (`{read_file.file_tag}`)."""
    out = {**format_elements(symbols), **MARKS}
    for tool in HEADER:
        for role, word in header(tool).items():
            out[f"{tool}.{role}"] = word
    return out


def header(tool: str) -> dict:
    """`role -> word` for one tool, the common lines included. Unknown tool: common only."""
    return {**COMMON, **HEADER.get(tool, {})}


def schema_file(tool: str, path: str = "new") -> str:
    """The yaml a slot loads for `path`, or `""` when it has none (execute_code, unknown)."""
    return SCHEMA_BY_PATH.get(tool, {}).get(path or "new", "")


def call_params(schema: dict) -> list:
    """The panel's view of a loaded schema: `[{name, type, required, enum?}]` in schema order.

    No descriptions on purpose: they are prompt text for the model, long, and the panel has
    no use for them.
    """
    params = (schema or {}).get("parameters") or {}
    required = set(params.get("required") or [])
    out = []
    for name, spec in (params.get("properties") or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        entry = {"name": str(name), "type": str(spec.get("type") or ""),
                 "required": name in required}
        if isinstance(spec.get("enum"), list):
            entry["enum"] = [str(v) for v in spec["enum"]]
        out.append(entry)
    return out
