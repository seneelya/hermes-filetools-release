#!/usr/bin/env python3
"""terminal — override of Hermes' own built-in `terminal` tool.

Design: `__HQ/plans/Plan03__terminal-passthrough.md`. We do NOT reimplement
command execution — approval/sudo/docker/sandbox logic all stays in
hermes-agent's own `tools.terminal_tool.terminal_tool()`, imported and
called live, unmodified (no fork, no monkeypatch). This module's only job
is turning that function's JSON return string into plain text, the same
contract read_file/write_file/patch/search_files already give the model.

No console `main()` here unlike the other `*_core.py` files: the whole
point of this tool is running inside a live Hermes process (real terminal
env, approval flow, process registry) — there is no meaningful offline mode
to drive from the command line. `render_terminal_result()` below is the
pure, unit-testable half; `terminal_tool_new()` is the live-only half.

Since Plan04 task 3 the formatting itself lives in `file_ops/_tool_text.py`
and is shared with the other tools we wrap; what stays here is only what is
genuinely terminal's — its label map, its field order, and the
`(raw: "exit_code": N)` tail that two other files regex out of our text.
"""

try:
    from ._tool_text import render_json_result
    from . import _ledger
except ImportError:  # console/dev mode - file_ops/ is on sys.path, not a package
    from _tool_text import render_json_result  # type: ignore
    import _ledger  # type: ignore

# Friendly labels for known result keys (collected from hermes-agent's
# tools/terminal_tool.py return sites, session of 2026-09-01: 17 distinct
# `json.dumps(...)` returns across success/background/degraded/exception
# paths). Any key NOT in this map still gets rendered — title-cased from its
# snake_case name — so a future upstream field never silently disappears;
# they hand the model raw JSON today with zero unpacking on their side, so
# the model already has to find fields by name either way (confirmed with
# the user 2026-09-01).
_LABELS = {
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
}

# The trailing "(raw: ...)" is not decoration and this is the only place it is
# produced: agent/context_compressor.py's own compaction summary line regexes
# the stored text for a literal `"exit_code": N` (any tool_name == "terminal"
# result, JSON or not). Without this fragment the post-compaction summary
# degrades to "exit ?" - one cheap line keeps it working (Plan03 decision). It
# is ALSO what dashboard/plugin_api.py's _TOOL_SIGNALS["terminal"] matches to
# recognise a history row as ours, so it is load bearing twice over.
_VALUE_FORMATTERS = {
    "exit_code": lambda v: f'{v}  (raw: "exit_code": {v})',
}

# exit_code and error lead; everything else keeps upstream's own order.
_HEAD_KEYS = ("exit_code", "error")


def render_terminal_result(raw: str, symbols: dict = None, op: str = "") -> str:
    """JSON string (their contract) -> plain text (ours): header lines, the
    ○○○ separator, then the command's own output verbatim.

    Everything mechanical (fail-open on non-JSON, never dropping an unknown
    key, no separator when there is no body) is `render_json_result`'s; this
    function is the terminal-shaped configuration of it. The separator is the
    SAME boundary read_file puts between its header and the file body, so the
    model reads one convention instead of two and the panel needs no
    terminal-only branch. Deliberately NOT comment-wrapped the way read_file's
    header is: this is not a file and there is no language to wrap for.
    """
    return render_json_result(
        raw,
        labels=_LABELS,
        body_keys=("output",),
        head_keys=_HEAD_KEYS,
        value_formatters=_VALUE_FORMATTERS,
        symbols=symbols,
        op=op,
    )


def terminal_tool_new(command: str, background: bool = False, timeout=None,
                       task_id: str = None, session_id: str = None,
                       workdir: str = None, pty: bool = False,
                       notify_on_complete: bool = False,
                       watch_patterns=None, symbols: dict = None) -> str:
    from tools.terminal_tool import terminal_tool as _live_terminal_tool

    # Address FIRST, before the command runs: a long build must not be able to
    # renumber itself behind a call that started later (Plan09). No tag - there
    # is no file here, and the address is the number.
    op = _ledger.next_op(session=session_id, task_id=task_id or "default")

    raw = _live_terminal_tool(
        command=command,
        background=background,
        timeout=timeout,
        task_id=task_id,
        session_id=session_id,
        workdir=workdir,
        pty=pty,
        notify_on_complete=notify_on_complete,
        watch_patterns=watch_patterns,
    )
    out = render_terminal_result(raw, symbols=symbols, op=op)
    _note(out, op, session_id, task_id, command=command)
    return out


def _note(out: str, op: str, session_id, task_id, command: str = "") -> None:
    """Record the operation, and under no circumstances affect the answer.

    Second guard over `_ledger`'s own fail-open, same as the file cores keep:
    a ledger module that failed to import or changed shape must not be able to
    turn a working command into an error string.
    """
    try:
        # Plan11-Task03: the command verbatim. `terminal` is THE case the
        # sidecar exists for — a long script is exactly what stage B will stub
        # out of the model's context, and after that this is the only copy.
        _ledger.note_op(tool="terminal", text=out, session=session_id or "",
                        task_id=task_id or "default", op=op, command=command)
    except Exception:
        pass
