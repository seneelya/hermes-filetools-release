"""Unit tests for file_ops/process_core.py (Plan04 task 5).

render_process_result() is pure - no Hermes, no HERMES_SRC. Every fixture below
is shaped after a REAL return site in hermes-agent's tools/process_registry.py
(read 2026-09-02): list_sessions ~2552, poll ~2099, read_log ~2142, wait ~2184
(exited / interrupted / timeout), kill_process ~2303, write_stdin ~2430,
close_stdin ~2516. There are eight actions and they do NOT share a body key -
that is the one thing worth testing exhaustively here, because getting it wrong
puts a chunk of output in the header as an unlabeled line.

process_tool_new() calls THEIR live handler and pipes the result through the
renderer; tested with a fake stand-in module injected into sys.modules.
"""
from __future__ import annotations

import json
import sys
import types

import pytest

from conftest import split_op_line   # our tools lead every answer with `op: <id>`

sys.path.insert(0, "file_ops")
import process_core  # type: ignore

render = process_core.render_process_result


# ── the body key, per action ────────────────────────────────────────────────

def test_poll_body_is_output_preview():
    raw = json.dumps({
        "session_id": "proc_ab12", "command": "npm run dev", "status": "running",
        "pid": 4567, "uptime_seconds": 12, "output_preview": "listening on 3000\n",
    })
    head, sep, body = render(raw).partition("\n○○○\n")
    assert sep
    assert body == "listening on 3000\n"
    assert head.splitlines() == [
        "Status: running",
        "Session id: proc_ab12",
        "Command: npm run dev",
        "Pid: 4567",
        "Uptime, s: 12",
    ]


def test_log_body_is_output_with_pagination_scalars():
    raw = json.dumps({
        "session_id": "proc_ab12", "command": "pytest", "status": "exited",
        "output": "line1\nline2", "total_lines": 900, "showing": "2 lines",
    })
    head, _, body = render(raw).partition("\n○○○\n")
    assert body == "line1\nline2"
    assert "Total lines: 900" in head
    assert "Showing: 2 lines" in head


def test_wait_exited_leads_with_status_and_exit_code():
    raw = json.dumps({
        "status": "exited", "command": "make", "exit_code": 0,
        "completion_reason": "exited", "termination_source": None,
        "output": "done\n",
    })
    result = render(raw)
    assert result.splitlines()[:3] == [
        "Status: exited", "Command: make", "Exit code: 0",
    ]
    assert "Termination source" not in result  # None is dropped
    assert result.endswith("○○○\ndone\n")


def test_wait_timeout_note_reaches_the_model_intact():
    """Upstream went out of its way to say a timeout is NOT an error (511
    duplicate calls in production came from models misreading it). That
    sentence must survive our reformatting."""
    note = ("Wait window of 180s elapsed - the process is still running. "
            "This is not an error. Uptime: 42s.")
    raw = json.dumps({"status": "timeout", "command": "serve", "output": "...",
                       "process_running": True, "timeout_note": note})
    result = render(raw)
    assert f"Note: {note}" in result
    assert "Process running: True" in result


def test_kill_body_is_output():
    raw = json.dumps({
        "status": "killed", "session_id": "proc_ab12",
        "completion_reason": "killed", "termination_source": "process.kill",
        "output": "tail of the log\n",
    })
    head, _, body = render(raw).partition("\n○○○\n")
    assert body == "tail of the log\n"
    assert "Completion reason: killed" in head
    assert "Termination source: process.kill" in head


def test_write_has_no_body_and_no_dangling_separator():
    raw = json.dumps({"status": "ok", "bytes_written": 12})
    result = render(raw)
    assert "○○○" not in result
    assert result == "Status: ok\nBytes written: 12"


def test_close_message_renders():
    assert render(json.dumps({"status": "ok", "message": "stdin closed"})) == \
        "Status: ok\nMessage: stdin closed"


def test_not_found_error_leads_the_header():
    raw = json.dumps({"status": "not_found", "error": "No process with ID x"})
    assert render(raw) == "Status: not_found\nError: No process with ID x"


def test_tool_error_shape_renders_as_an_error_line():
    """Their handler answers a missing session_id / unknown action with
    tools.registry.tool_error(), i.e. `{"error": ...}`."""
    raw = json.dumps({"error": "session_id is required for poll"})
    assert render(raw) == "Error: session_id is required for poll"


# ── action="list": the collection IS the answer ────────────────────────────

def test_list_renders_one_line_per_process():
    raw = json.dumps({"processes": [
        {"session_id": "proc_a", "command": "npm run dev", "cwd": "/srv",
         "pid": 1, "uptime_seconds": 30, "status": "running",
         "output_preview": "up\n"},
        {"session_id": "proc_b", "command": "pytest", "cwd": "/srv",
         "pid": 2, "uptime_seconds": 5, "status": "exited",
         "output_preview": "", "exit_code": 1},
    ]})
    body = render(raw)
    lines = body.splitlines()
    assert len(lines) == 2, f"one line per process, got:\n{body}"
    assert lines[0].startswith("Session id=proc_a  Command=npm run dev  Cwd=/srv")
    assert "Exit code=1" in lines[1]
    # An empty output_preview costs a pair for no information -> dropped.
    assert "Output preview=" not in lines[1]


def test_list_row_newlines_are_flattened_not_leaked():
    """Every row carries a 200-char output_preview straight out of the process
    buffer. A raw newline in there silently breaks the one-row-per-process
    promise the whole format exists for."""
    raw = json.dumps({"processes": [
        {"session_id": "proc_a", "output_preview": "first\nsecond\nthird"},
        {"session_id": "proc_b", "output_preview": "only"},
    ]})
    lines = render(raw).splitlines()
    assert len(lines) == 2
    assert lines[0] == "Session id=proc_a  Output preview=first ⏎ second ⏎ third"


def test_list_key_order_stable_when_a_row_lacks_a_key():
    raw = json.dumps({"processes": [
        {"session_id": "a", "pid": 1, "status": "running"},
        {"session_id": "b", "status": "exited", "exit_code": 2},
    ]})
    lines = render(raw).splitlines()
    assert lines[0] == "Session id=a  Pid=1  Status=running"
    assert lines[1] == "Session id=b  Status=exited  Exit code=2"


def test_list_empty_renders_nothing_rather_than_an_empty_blob():
    assert render(json.dumps({"processes": []})) == ""


# ── plumbing ───────────────────────────────────────────────────────────────

def test_unknown_key_still_rendered_not_dropped():
    raw = json.dumps({"status": "ok", "totally_new_future_field": "surprise"})
    assert "Totally new future field: surprise" in render(raw)


def test_non_json_passthrough_unchanged():
    raw = "not json at all"
    assert render(raw) == raw


def test_process_tool_new_forwards_args_dict_and_renders(monkeypatch):
    captured = {}

    def _fake_handler(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return json.dumps({"status": "ok", "bytes_written": 3})

    fake_mod = types.ModuleType("tools.process_registry")
    fake_mod._handle_process = _fake_handler
    monkeypatch.setitem(sys.modules, "tools.process_registry", fake_mod)
    if "tools" not in sys.modules:
        monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))

    result = process_core.process_tool_new(
        {"action": "write", "session_id": "proc_a", "data": "yes"}, task_id="t1")

    assert captured["args"] == {"action": "write", "session_id": "proc_a",
                                 "data": "yes"}
    assert captured["kwargs"] == {"task_id": "t1"}
    _op, rest = split_op_line(result)   # the address leads the answer (Plan09)
    assert rest == "Status: ok\nBytes written: 3"


def test_process_tool_new_does_not_validate_args_itself(monkeypatch):
    """Their handler owns the action dispatch, the session_id coercion and the
    required-argument errors. A check of ours here would duplicate or shadow
    theirs."""
    seen = {}

    def _fake_handler(args, **kwargs):
        seen.update(args)
        return json.dumps({"error": "Unknown process action: fly"})

    fake_mod = types.ModuleType("tools.process_registry")
    fake_mod._handle_process = _fake_handler
    monkeypatch.setitem(sys.modules, "tools.process_registry", fake_mod)
    if "tools" not in sys.modules:
        monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))

    result = process_core.process_tool_new({"action": "fly"})
    assert seen == {"action": "fly"}
    _op, rest = split_op_line(result)
    assert rest == "Error: Unknown process action: fly"
