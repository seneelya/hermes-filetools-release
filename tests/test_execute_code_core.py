"""Unit tests for file_ops/execute_code_core.py (Plan04 task 4).

render_execute_code_result() is pure - no Hermes, no HERMES_SRC - and the
fixtures below are shaped after the actual `json.dumps(...)` return sites in
hermes-agent's tools/code_execution_tool.py (local path ~1700, remote path
~1240, approval-guard refusal ~1336, the two exception paths ~1195/~1752, and
the stdout-truncation metadata from _assemble_stdout_result ~107).

execute_code_tool_new() calls THEIR live handler and pipes the result through
the renderer; it is tested with a fake stand-in module injected into
sys.modules, so no real hermes-agent checkout is needed to verify the
plumbing - in particular that `args` reaches them as the dict they expect,
which is the whole reason we call their handler instead of execute_code().
"""
from __future__ import annotations

import json
import sys
import types

import pytest

from conftest import split_op_line   # our tools lead every answer with `op: <id>`

sys.path.insert(0, "file_ops")
import execute_code_core  # type: ignore


def render(raw, **kw):
    kw.setdefault("max_body_chars", 0)  # 0 = no cap; the cap has its own tests
    return execute_code_core.render_execute_code_result(raw, **kw)


def test_success_renders_status_output_and_separator():
    raw = json.dumps({
        "status": "success", "output": "42\n", "exit_code": 0,
        "tool_calls_made": 3, "duration_seconds": 1.25,
    })
    result = render(raw)
    head, sep, body = result.partition("\n◆○○○\n")
    assert sep
    assert head.splitlines() == [
        "◆Status: success",
        "◆Exit code: 0",
        "◆Tool calls: 3",
        "◆Duration, s: 1.25",
    ]
    assert body == "42\n"


def test_no_json_blob_for_the_model():
    """The point of the whole wrapper: the model must not receive a JSON
    payload to unpack by hand."""
    raw = json.dumps({"status": "success", "output": "hi", "exit_code": 0})
    result = render(raw)
    assert "{" not in result and '":' not in result


def test_error_path_leads_with_status_and_error():
    raw = json.dumps({
        "status": "error", "output": "partial\nTraceback...\n", "exit_code": 1,
        "error": "Script exited with code 1", "tool_calls_made": 0,
        "duration_seconds": 0.4,
    })
    result = render(raw)
    lines = result.splitlines()
    assert lines[0] == "◆Status: error"
    assert lines[1] == "◆Exit code: 1"
    assert lines[2] == "Error: Script exited with code 1"


def test_approval_refusal_has_header_only_no_dangling_separator():
    raw = json.dumps({
        "status": "error", "error": "execute_code blocked by approval guard.",
        "tool_calls_made": 0, "duration_seconds": 0,
    })
    result = render(raw)
    assert "○○○" not in result
    assert "Error: execute_code blocked by approval guard." in result
    assert "Duration, s: 0" in result


def test_upstream_stdout_truncation_metadata_surfaces_as_header_lines():
    """Their own 50KB stdout cap reports itself in the payload. Those keys are
    the honest 'you are missing output' signal and must reach the model."""
    raw = json.dumps({
        "status": "success", "output": "head...tail", "exit_code": 0,
        "stdout_truncated": True, "stdout_bytes_captured": 50000,
        "stdout_bytes_total": 120000, "stdout_bytes_omitted": 70000,
        "warning": "execute_code stdout was truncated; the script did run",
    })
    result = render(raw)
    assert "Stdout truncated: True" in result
    assert "Stdout bytes omitted: 70000" in result
    assert "Warning: execute_code stdout was truncated; the script did run" in result


def test_stdout_truncated_false_is_kept_not_dropped():
    """`False` is information here - it says the output IS complete. Only None
    and "" carry nothing."""
    raw = json.dumps({"status": "success", "output": "hi", "exit_code": 0,
                       "stdout_truncated": False})
    assert "Stdout truncated: False" in render(raw)


def test_unknown_key_still_rendered_not_dropped():
    raw = json.dumps({"status": "success", "output": "hi",
                       "totally_new_future_field": "surprise"})
    assert "Totally new future field: surprise" in render(raw)


def test_tool_error_shape_renders_as_an_error_line():
    """Their handler's misdirected-argument recovery answers with
    tools.registry.tool_error(), i.e. `{"error": ...}` - which flows through
    the same renderer and comes out as one readable line."""
    raw = json.dumps({"error": "execute_code received a 'command' parameter"})
    assert render(raw) == "Error: execute_code received a 'command' parameter"


def test_non_json_passthrough_unchanged():
    raw = "not json at all"
    assert render(raw) == raw


def test_body_cap_truncates_and_says_so(tmp_path):
    body = "x" * 5000
    raw = json.dumps({"status": "success", "output": body, "exit_code": 0})
    result = execute_code_core.render_execute_code_result(
        raw, max_body_chars=1000)
    assert "OUTPUT TRUNCATED" in result
    assert "chars omitted out of 5000 total" in result
    # The HEADER is never cut - an exit code the model cannot see is worse than
    # output it knows was cut.
    assert result.startswith("◆Status: success\n◆Exit code: 0\n◆○○○\n")


def test_body_cap_leaves_a_fitting_body_alone():
    raw = json.dumps({"status": "success", "output": "short", "exit_code": 0})
    result = execute_code_core.render_execute_code_result(raw, max_body_chars=1000)
    assert result.endswith("◆○○○\nshort")
    assert "TRUNCATED" not in result


def test_execute_code_tool_new_forwards_args_dict_and_renders(monkeypatch):
    captured = {}

    def _fake_handler(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return json.dumps({"status": "success", "output": "fake ran",
                            "exit_code": 0})

    fake_mod = types.ModuleType("tools.code_execution_tool")
    fake_mod._execute_code_handler = _fake_handler
    monkeypatch.setitem(sys.modules, "tools.code_execution_tool", fake_mod)
    if "tools" not in sys.modules:
        monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))

    result = execute_code_core.execute_code_tool_new(
        {"code": "print(1)"}, task_id="t1", enabled_tools=["read_file"])

    assert captured["args"] == {"code": "print(1)"}
    assert captured["kwargs"] == {"task_id": "t1", "enabled_tools": ["read_file"]}
    assert "fake ran" in result
    assert "Status: success" in result


def test_execute_code_tool_new_passes_misdirected_args_through_untouched(monkeypatch):
    """We must NOT validate `code` ourselves: their handler owns the
    command-instead-of-code and non-string-code redirects, and a check of our
    own here would either duplicate or shadow theirs."""
    seen = {}

    def _fake_handler(args, **kwargs):
        seen.update(args)
        return json.dumps({"error": "execute_code received a 'command' parameter"})

    fake_mod = types.ModuleType("tools.code_execution_tool")
    fake_mod._execute_code_handler = _fake_handler
    monkeypatch.setitem(sys.modules, "tools.code_execution_tool", fake_mod)
    if "tools" not in sys.modules:
        monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))

    result = execute_code_core.execute_code_tool_new({"command": "ls"})
    assert seen == {"command": "ls"}
    _op, rest = split_op_line(result)   # the address leads the answer (Plan09)
    assert rest.startswith("Error: ")


def test_max_body_chars_reads_their_config_reader(monkeypatch):
    fake_mod = types.ModuleType("tools.tool_output_limits")
    fake_mod.get_max_bytes = lambda: 12_345
    monkeypatch.setitem(sys.modules, "tools.tool_output_limits", fake_mod)
    if "tools" not in sys.modules:
        monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
    assert execute_code_core._max_body_chars() == 12_345


def test_max_body_chars_falls_open_to_no_cap_of_ours(monkeypatch):
    """Their reader unreachable -> 0, i.e. no cap of OURS. Deliberately not an
    invented number: upstream's own 50KB stdout cap is still in force, so
    "could not read the config" must never silently mean "cut somewhere else".
    """
    def _boom():
        raise RuntimeError("no config")

    fake_mod = types.ModuleType("tools.tool_output_limits")
    fake_mod.get_max_bytes = _boom
    monkeypatch.setitem(sys.modules, "tools.tool_output_limits", fake_mod)
    if "tools" not in sys.modules:
        monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
    assert execute_code_core._max_body_chars() == 0


def test_is_ours_recognises_our_execute_code_text():
    """CROSS-HALF test, the one that catches the regression hit live on
    2026-09-02: /ops (dashboard/plugin_api.py) gates HISTORY by matching
    literal fragments of OUR rendered text, while desktop/src/core.js gates
    the LIVE stream by tool name. Both lists were missing execute_code
    entirely, so no card appeared at all; now that they include it, a
    rendering change must keep matching or history silently stops loading
    while new calls still show.
    """
    import sys
    sys.path.insert(0, "dashboard")
    import plugin_api  # type: ignore

    ours = execute_code_core.render_execute_code_result(json.dumps({
        "status": "success", "exit_code": 0, "output": "hi",
        "tool_calls_made": 0, "duration_seconds": 0.8,
        "stdout_truncated": False, "stdout_bytes_captured": 2,
    }))
    # A script that printed nothing at all: no stdout counters in the answer,
    # so the FIRST signal is absent and the fallback ones have to carry it.
    quiet = execute_code_core.render_execute_code_result(json.dumps({
        "status": "success", "exit_code": 0, "tool_calls_made": 3,
    }))
    builtin_json = json.dumps({"status": "success", "exit_code": 0, "output": "hi"})

    def row(content):
        return {"tool_name": "execute_code", "content": content}

    assert plugin_api._is_ours(row(ours)) is True
    assert plugin_api._is_ours(row(quiet)) is True
    assert plugin_api._is_ours(row(builtin_json)) is False


# ── Plan11-Task02: the sandbox window and out_lines ─────────────────────────
#
# `_ledger` is imported the same BARE way `execute_code_core` imports it, and
# the home is redirected through `set_hermes_home_override` rather than by
# patching `ledger_dir`: this file imports its subject bare, so a patched
# module attribute would land on a DIFFERENT module object than the one under
# test. The override is process-wide, so module identity stops mattering.
import _ledger  # type: ignore  # noqa: E402


@pytest.fixture
def home(tmp_path):
    """A private hermes home for one test, restored afterwards."""
    import hermes_constants
    token = hermes_constants.set_hermes_home_override(tmp_path)
    try:
        yield tmp_path
    finally:
        hermes_constants.reset_hermes_home_override(token)


def _install_fake_handler(monkeypatch, handler):
    fake_mod = types.ModuleType("tools.code_execution_tool")
    fake_mod._execute_code_handler = handler
    monkeypatch.setitem(sys.modules, "tools.code_execution_tool", fake_mod)
    if "tools" not in sys.modules:
        monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))


def test_nested_ops_are_flagged_while_the_script_runs(home, monkeypatch):
    """The flag has to be set WHILE the script runs or never: every nested call
    inherits the outer execute_code's tool_call_id, which is exactly why such
    reads read as `unknown` in /reads and cannot be reclassified afterwards."""

    def _fake_handler(args, **kwargs):
        # Stands in for the model's script reaching our tools through the facade.
        _ledger.note_read(text="inner", path="/inside.py", session="s")
        return json.dumps({"status": "success", "output": "ok", "exit_code": 0})

    _install_fake_handler(monkeypatch, _fake_handler)

    _ledger.note_read(text="before", path="/before.py", session="s")
    execute_code_core.execute_code_tool_new({"code": "x"}, session_id="s")
    _ledger.note_read(text="after", path="/after.py", session="s")

    by_path = {e.get("path"): e for e in _ledger.read_ledger("s")}
    assert "nested" not in by_path["/before.py"]
    assert by_path["/inside.py"]["nested"] is True
    assert "nested" not in by_path["/after.py"], "the window must close"


@pytest.mark.parametrize("printed, seen", [
    ("12", None),                                   # counted dots: nothing reached the model
    ("\n".join(f"text {k}" for k in range(1, 16)), [[1, 15]]),
])
def test_nested_read_records_what_reached_the_model(home, monkeypatch, printed, seen):
    """`deferred/17`: `seen` comes from the answer the model receives, written at the
    end of execute_code, before the execute_code entry itself."""
    body = [f"text {k}" for k in range(1, 21)]

    def _fake_handler(args, **kwargs):
        _ledger.note_read(text="inner", path="/f.txt", session="s", from_line=1,
                          to_line=20, lines=20, total=20, body=body)
        return json.dumps({"status": "success", "output": printed, "exit_code": 0})

    _install_fake_handler(monkeypatch, _fake_handler)
    execute_code_core.execute_code_tool_new({"code": "x"}, session_id="s")

    nested, outer = _ledger.read_ledger("s")
    assert nested["path"] == "/f.txt" and outer["tool"] == "execute_code"
    assert nested.get("seen") == seen


def test_sandbox_window_closes_even_if_their_handler_raises(home, monkeypatch):
    """A leaked flag would mislabel every later operation as nested — worse
    than a missing flag, because it looks like data rather than absence."""

    def _boom(args, **kwargs):
        raise RuntimeError("their handler blew up")

    _install_fake_handler(monkeypatch, _boom)

    with pytest.raises(RuntimeError):
        execute_code_core.execute_code_tool_new({"code": "x"}, session_id="s")

    _ledger.note_read(text="after", path="/after.py", session="s")
    assert "nested" not in _ledger.read_ledger("s")[0]


def test_out_lines_counts_our_rendered_answer(home, monkeypatch):
    """Counted on what the MODEL receives, not on their raw result: the teal
    heuristic is about what reached the context, not about sandbox internals."""

    def _fake_handler(args, **kwargs):
        return json.dumps({"status": "success",
                           "output": "\n".join(["one", "two", "three"]),
                           "exit_code": 0})

    _install_fake_handler(monkeypatch, _fake_handler)

    out = execute_code_core.execute_code_tool_new({"code": "x"}, session_id="s")
    entry = _ledger.read_ledger("s")[0]
    assert entry["out_lines"] == out.count("\n") + 1
    assert entry["out_lines"] > 3, "the envelope's own lines count too"
