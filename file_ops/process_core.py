#!/usr/bin/env python3
"""process — override of Hermes' own built-in `process` tool.

Design: `__HQ/plans/Plan04__wrap-exec-and-process.md` task 5. Same discipline
as `terminal_core.py` and `execute_code_core.py`: we do NOT reimplement
anything. Session lookup, stdin plumbing, the kill/systemd escalation, the
completion-consumed bookkeeping and the secret redaction all stay in
hermes-agent's own `tools.process_registry._handle_process()`, imported and
called live. This module turns its JSON return string into plain text.

Their HANDLER, not the individual registry methods: the handler owns the
action dispatch, the `session_id` coercion (models send integers), the
required-argument errors and the unknown-action message. Calling the methods
directly would mean copying all of that.

INVENTORY of the 8 actions (read out of tools/process_registry.py, 2026-09-02),
because "which key holds the body" is the only thing the shared renderer needs
and it is NOT the same key for all of them:

    action    body key          notable scalars
    ------    ----------------  ------------------------------------------
    list      processes (LIST)  - (the collection IS the answer)
    poll      output_preview    session_id command status pid uptime_seconds
                                 [exit_code completion_reason
                                  termination_source detached note]
    log       output            session_id command status total_lines showing
    wait      output            status command [exit_code completion_reason
                                 termination_source | note | process_running]
                                 timeout_note
    kill      output            status [session_id completion_reason
                                 termination_source | exit_code]
    write     -                 status bytes_written | error
    submit    -                 status bytes_written | error
    close     -                 status message | error

`list` is the one genuine complication: an array of dicts is unreadable
flattened to `key: value` lines and a JSON blob is exactly what we are
removing, so `render_json_result` puts it below the separator as one line per
process with compact `Label=value` pairs (and forces each value onto one line -
every row carries a 200-char `output_preview`).

No console `main()`, same reason as the other two: this tool only means
anything inside a live Hermes process with a real process registry.
"""

try:
    from ._tool_text import render_json_result
    from . import _ledger
    from . import _vocab
except ImportError:  # console/dev mode - file_ops/ is on sys.path, not a package
    from _tool_text import render_json_result  # type: ignore
    import _ledger  # type: ignore
    import _vocab  # type: ignore

# Friendly labels across all 8 actions. Any key NOT here is still rendered,
# title-cased from its own snake_case name - a future upstream field must never
# silently disappear.
# The table lives in `_vocab.py` since 2026-10-05 (`deferred/08`): the panel reads the same
# words over `/config`, and a word printed here but unknown there stops being coloured.
_LABELS = _vocab.LABELS["process"]

# What identifies the process and what happened to it, before the details.
_HEAD_KEYS = ("status", "error", "session_id", "command", "exit_code")

# One key per action, first PRESENT wins (they never overlap - see the
# inventory in the module docstring).
_BODY_KEYS = ("output", "output_preview", "processes")


def render_process_result(raw: str, symbols: dict = None, op: str = "") -> str:
    """JSON string (their contract) -> plain text (ours): header lines, the
    ○○○ separator, then whichever field this action's answer carries.

    Everything mechanical (fail-open on non-JSON, never dropping an unknown
    key, no separator when there is no body, the array-of-dicts rule) belongs
    to `render_json_result`; this is the process-shaped configuration of it.

    No body cap here, deliberately, unlike execute_code: every body this tool
    can return is already bounded upstream by construction - `output_preview`
    at 200-1000 chars, `wait`/`kill` at the last 2000, and `log` by the
    `limit` argument the model itself passes (default 200 lines).
    """
    return render_json_result(
        raw,
        labels=_LABELS,
        body_keys=_BODY_KEYS,
        head_keys=_HEAD_KEYS,
        symbols=symbols,
        op=op,
        count_body=True,   # `list`: `Processes: N` in the header, `0` included
    )


def process_tool_new(args: dict, symbols: dict = None, **kwargs) -> str:
    """Call their live handler with the model's arguments untouched, render its
    answer. `args` goes through as the dict they expect precisely so their
    action dispatch and argument validation keep working (see module docstring).
    """
    from tools.process_registry import _handle_process

    op = _ledger.next_op(session=kwargs.get("session_id"),
                         task_id=kwargs.get("task_id") or "default")

    raw = _handle_process(args, **kwargs)
    out = render_process_result(raw, symbols=symbols, op=op)
    _note(out, op, kwargs)
    return out


def _note(out: str, op: str, kwargs: dict) -> None:
    """Record the operation, and under no circumstances affect the answer.

    Second guard over `_ledger`'s own fail-open (same as the file cores keep):
    a ledger module that failed to import or changed shape must not be able to
    turn a working call into an error string.
    """
    try:
        _ledger.note_op(tool='process', text=out,
                        session=kwargs.get("session_id") or "",
                        task_id=kwargs.get("task_id") or "default", op=op)
    except Exception:
        pass
