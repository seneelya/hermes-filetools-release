"""Unit tests for file_ops/terminal_core.py (Plan03).

render_terminal_result() is pure - no Hermes, no HERMES_SRC needed - and is
the bulk of what's worth testing: it's the JSON-in/text-out contract. The
fixture strings below are shaped after the actual `json.dumps(...)` return
sites found in hermes-agent's tools/terminal_tool.py this session (success,
background, error+hint, degraded, exception+traceback, sudo-redacted).

terminal_tool_new() itself just calls the live tools.terminal_tool.terminal_tool
and pipes the result through render_terminal_result - tested here with a
fake stand-in module injected into sys.modules, so no real hermes-agent
checkout is required to verify the plumbing (kwargs forwarded correctly).
"""
from __future__ import annotations

import json
import re
import sys
import types

import pytest

sys.path.insert(0, "file_ops")
import terminal_core  # type: ignore


def test_success_renders_output_and_exit_code():
    raw = json.dumps({"output": "hello\nworld\n", "exit_code": 0, "error": None})
    result = terminal_core.render_terminal_result(raw)
    assert "Exit code: 0" in result
    assert re.search(r'"exit_code"\s*:\s*0', result)  # compaction regex hook
    assert "hello\nworld\n" in result
    assert "Error:" not in result  # error is None -> no line


def test_error_line_only_when_truthy():
    raw = json.dumps({"output": "", "exit_code": 1, "error": "boom"})
    result = terminal_core.render_terminal_result(raw)
    assert "Exit code: 1" in result
    assert "Error: boom" in result


def test_background_start_shows_session_and_pid():
    raw = json.dumps({
        "output": "Background process started",
        "session_id": "abc123",
        "pid": 4567,
        "exit_code": 0,
        "error": None,
        "watch_patterns": ["ready"],
    })
    result = terminal_core.render_terminal_result(raw)
    assert "Session id: abc123" in result
    assert "Pid: 4567" in result
    assert "Watching:" in result and "ready" in result


def test_degraded_backend_shows_status_reason_hint():
    raw = json.dumps({
        "output": "",
        "exit_code": -1,
        "status": "degraded",
        "reason": "SSH host down",
        "retry_hint": "check network",
        "error": "Terminal backend degraded: SSH host down",
    })
    result = terminal_core.render_terminal_result(raw)
    assert "Status: degraded" in result
    assert "Reason: SSH host down" in result
    assert "Retry hint: check network" in result
    assert "Error: Terminal backend degraded: SSH host down" in result


def test_exception_traceback_surfaces():
    raw = json.dumps({
        "output": "",
        "exit_code": -1,
        "error": "Failed to execute command: boom",
        "traceback": "Traceback (most recent call last):\n  ...",
        "status": "error",
    })
    result = terminal_core.render_terminal_result(raw)
    assert "Error: Failed to execute command: boom" in result
    assert "Traceback:" in result
    assert "Status: error" in result


def test_sudo_flags_render():
    raw = json.dumps({
        "output": "",
        "exit_code": -1,
        "error": "[REDACTED] sudo auth failed",
        "sudo_auth_failed": True,
        "sudo_cache_cleared": True,
    })
    result = terminal_core.render_terminal_result(raw)
    assert "Sudo auth failed: True" in result
    assert "Sudo cache cleared: True" in result


def test_unknown_key_still_rendered_not_dropped():
    raw = json.dumps({"output": "ok", "exit_code": 0, "error": None,
                       "totally_new_future_field": "surprise"})
    result = terminal_core.render_terminal_result(raw)
    assert "Totally new future field: surprise" in result


def test_nested_dict_value_rendered_compact():
    raw = json.dumps({"output": "ok", "exit_code": 0, "error": None,
                       "verification_evidence": {"status": "verified", "kind": "test"}})
    result = terminal_core.render_terminal_result(raw)
    assert "Verification evidence:" in result
    assert '"status": "verified"' in result


def test_separator_between_header_and_output():
    raw = json.dumps({"output": "hello\n", "exit_code": 0, "error": None,
                       "cwd": "/srv/app"})
    result = terminal_core.render_terminal_result(raw)
    assert "○○○" in result
    head, _, body = result.partition("\n○○○\n")
    assert "Exit code: 0" in head and "Cwd: /srv/app" in head
    assert body == "hello\n"  # body is verbatim, nothing appended below it


def test_no_separator_when_output_empty():
    """A ○○○ with nothing under it tells the model there IS a body. There
    isn't - a command can legitimately print nothing."""
    raw = json.dumps({"output": "", "exit_code": 0, "error": None})
    result = terminal_core.render_terminal_result(raw)
    assert "○○○" not in result
    assert result == 'Exit code: 0  (raw: "exit_code": 0)'


def test_separator_symbol_honours_configured_symbols():
    raw = json.dumps({"output": "hi", "exit_code": 0, "error": None})
    result = terminal_core.render_terminal_result(raw, symbols={"separator_symbol": "#"})
    assert "\n###\n" in result
    assert "○" not in result


def test_no_command_echo_in_header():
    """Settled with the user: the model just SENT the command, echoing it back
    costs tokens on every call for zero information."""
    raw = json.dumps({"output": "hi", "exit_code": 0, "error": None,
                       "command": "echo hi"})
    result = terminal_core.render_terminal_result(raw)
    # An upstream `command` key is never dropped (nothing is), but we don't
    # invent a Command: line of our own.
    assert result.count("echo hi") <= 1


def test_is_ours_still_recognises_the_new_text():
    """CROSS-HALF test, and the one that would actually catch the regression
    hit live on 2026-09-02: /ops (dashboard/plugin_api.py) gates history by
    matching literal fragments of OUR rendered text. Changing the rendering
    without checking that map makes new calls appear as cards while older ones
    never load. Both the foreground and the background shape must match.
    """
    sys.path.insert(0, "dashboard")
    import plugin_api  # type: ignore

    foreground = terminal_core.render_terminal_result(
        json.dumps({"output": "hi", "exit_code": 0, "error": None}))
    background = terminal_core.render_terminal_result(json.dumps({
        "output": "Background process started", "session_id": "abc123",
        "pid": 4567, "error": None}))
    builtin_json = json.dumps({"output": "hi", "exit_code": 0, "error": None})

    def row(content):
        return {"tool_name": "terminal", "content": content}

    assert plugin_api._is_ours(row(foreground)) is True
    assert plugin_api._is_ours(row(background)) is True
    assert plugin_api._is_ours(row(builtin_json)) is False


def test_non_json_passthrough_unchanged():
    raw = "plain stderr, not json at all"
    assert terminal_core.render_terminal_result(raw) == raw


def test_non_dict_json_passthrough_unchanged():
    raw = json.dumps([1, 2, 3])
    assert terminal_core.render_terminal_result(raw) == raw


def test_terminal_tool_new_forwards_kwargs_and_renders(monkeypatch):
    captured = {}

    def _fake_terminal_tool(**kwargs):
        captured.update(kwargs)
        return json.dumps({"output": "fake ran", "exit_code": 0, "error": None})

    fake_mod = types.ModuleType("tools.terminal_tool")
    fake_mod.terminal_tool = _fake_terminal_tool
    monkeypatch.setitem(sys.modules, "tools.terminal_tool", fake_mod)
    if "tools" not in sys.modules:
        monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))

    result = terminal_core.terminal_tool_new(
        command="echo hi", background=True, timeout=30, task_id="t1",
        session_id="s1", workdir="/tmp", pty=True, notify_on_complete=True,
        watch_patterns=["done"],
    )
    assert "fake ran" in result
    assert "Exit code: 0" in result
    assert captured == {
        "command": "echo hi", "background": True, "timeout": 30,
        "task_id": "t1", "session_id": "s1", "workdir": "/tmp", "pty": True,
        "notify_on_complete": True, "watch_patterns": ["done"],
    }
