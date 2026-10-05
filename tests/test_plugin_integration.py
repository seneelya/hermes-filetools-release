"""Iteration-1 "wiring" tests for the hermes-filetools plugin.

Scope (per PLAN__hermes_filetools.md §3): we test that the plugin CONNECTS, not
that each tool's file logic is correct:

  E1  real loader path — PluginManager discovers + loads the plugin from a temp
      HERMES_HOME; register(ctx) ran; all four tools land in the global registry
      with the right names/toolset/schemas (and modify_file WITHOUT mode/patch, Q6);
      get_definitions returns OpenAI-format schemas under the NEW names.
  E2  dispatch — registry.dispatch(<tool>, test-args, task_id=…, session_id=…)
      for each of the four tools on safe fixtures → str result, no "error".
      ALL FOUR default to path: new as of 2026-08-26 (plain text, see
      Vision01__read.md/Vision01__write.md/Vision01__search.md/
      Vision01__patch.md). The OLD paths of search_files and patch are still
      tested too (test_e2_sift_files_dispatch / test_e2_modify_file_dispatch),
      each via an explicit path:old override - same files
      (sift_files_core.py / modify_file_core.py) as before, unaffected by the
      default flip, JSON-in/JSON-out.
  E3  error contract — dispatch with missing/absent path → an error surfaces
      as a str (a plain "ERROR: ..." line on every new path; JSON
      {"error": …} only on the old paths) - not a crash and not
      "unsupported result type".
  E4  console — `python <core>.py <test-args>` without Hermes → exit 0 + JSON stdout.

Torn down afterwards so nothing leaks into other tests (registry restore).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import split_op_line   # our tools lead every answer with `op: <id>`

HERMES_SRC = os.environ.get("HERMES_SRC", "/workspace/SRC/hermes-agent-src")
if HERMES_SRC not in sys.path:
    sys.path.insert(0, HERMES_SRC)

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent          # == plugin root (repo root IS the plugin)
FILE_OPS_DIR = PROJECT_ROOT / "file_ops"  # feature group: view/create/modify/sift
PLUGIN_MEMBERS = ["__init__.py", "_home.py", "plugin.yaml", "file_ops"]  # groups merged in so far
FIXTURES = TESTS_DIR / "Edge"
EDGE_PY = FIXTURES / "Edge.py"

TOOL_NAMES = ["read_file", "write_file", "patch", "search_files"]

EXPECTED_REQUIRED = {
    "read_file": ["path", "flags"],
    "write_file": ["path", "content", "flags"],
    # Q6: no mode/patch. `line`/`flags` arrived 2026-08-26 with the "new" path
    # (Vision01__patch.md §3) - required with no default on purpose.
    "patch": ["path", "line", "old_string", "new_string", "flags"],
    "search_files": ["path", "pattern", "flags"],
}


def _load_plugin(tmp_path, monkeypatch, settings_yaml: str = ""):
    """Copy the plugin members (not the whole repo — tests/, __HQ/, not-yet-merged
    groups like self_delegate/self_undo live outside the plugin) into a temp
    HERMES_HOME and run the REAL loader.

    settings_yaml: extra lines appended under
    plugins.entries.hermes-filetools.settings.tools (indented to match), for
    tests that need per-slot config (enabled/path/schema)."""
    from hermes_cli import plugins as pl

    home = tmp_path / "hermes-home"
    dest = home / "plugins" / "hermes-filetools"
    dest.mkdir(parents=True)
    for member in PLUGIN_MEMBERS:
        src = PROJECT_ROOT / member
        if src.is_dir():
            shutil.copytree(src, dest / member, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(src, dest / member)
    # 2026-08-25: our tools now register under the BUILT-IN names with
    # override=True (see __init__.py) - the loader requires an explicit
    # operator opt-in per plugin for that, normally set in the real
    # config.yaml. Write the same key into this temp home so the test
    # exercises the real override path instead of hitting PermissionError.
    config_text = (
        "plugins:\n"
        "  entries:\n"
        "    hermes-filetools:\n"
        "      allow_tool_override: true\n"
    )
    if settings_yaml:
        config_text += "      settings:\n        tools:\n" + settings_yaml
    (home / "config.yaml").write_text(config_text, encoding="utf-8")
    monkeypatch.setattr(pl, "get_hermes_home", lambda: home)
    monkeypatch.setattr(pl, "_get_enabled_plugins", lambda: {"hermes-filetools"})
    monkeypatch.delenv("HERMES_SAFE_MODE", raising=False)

    # PluginManager.scope_key resolves via hermes_home_key() -> the REAL
    # hermes_constants.get_hermes_home(), a separate name binding from the
    # `pl.get_hermes_home` patched above (plugins.py's own copy) - patching
    # that alone leaves scope_key (and therefore the override-permission
    # check AND the scoped-registry overlay our override=True tools land in)
    # pointed at the real ambient home. The override must stay active for
    # the WHOLE test body, not just the load step - registry.get_entry() /
    # dispatch() without an explicit scope resolve through whatever scope
    # is "current" (i.e. this override), so resetting it right after loading
    # made every later lookup fall through to the untouched global (built-in)
    # entry - caller (loaded_plugin fixture) owns the reset, after the test
    # body has run.
    import hermes_constants
    token = hermes_constants.set_hermes_home_override(home)

    manager = pl.PluginManager()
    manager.discover_and_load(force=True)
    return manager, token


@pytest.fixture
def loaded_plugin(tmp_path, monkeypatch):
    """E1 setup + registry teardown for the four tool names.

    Keeps the hermes_constants home override active for the WHOLE test body
    (not just the load step) - our tools register into a scope-keyed overlay
    (override=True shadows the global built-in entry only while that scope
    is "current"), so resetting the override before the test's own
    get_entry()/dispatch() calls would make them fall through to the
    untouched built-in tool instead of ours.
    """
    import hermes_constants
    from tools.registry import registry

    snapshots = {name: registry.snapshot_registration(name) for name in TOOL_NAMES}
    manager, token = _load_plugin(tmp_path, monkeypatch)
    try:
        yield manager
    finally:
        hermes_constants.reset_hermes_home_override(token)
        for name in TOOL_NAMES:
            cur = registry.snapshot_registration(name)
            prev = snapshots[name]
            if cur is not None and cur is not prev:
                registry.restore_registration(name, cur, prev)


# ---------------------------------------------------------------------------
# E0 — per-slot config (2026-08-25 foundation: enabled/path/schema per tool)
# ---------------------------------------------------------------------------

def test_e0_slot_disabled_via_config(tmp_path, monkeypatch):
    """settings.tools.search_files.enabled: false must actually skip registering
    it, while the other 3 slots still register normally (proves the config path
    is real, not just accepted-and-ignored)."""
    import hermes_constants
    from tools.registry import registry

    snapshots = {name: registry.snapshot_registration(name) for name in TOOL_NAMES}
    manager, token = _load_plugin(
        tmp_path, monkeypatch,
        settings_yaml="          search_files:\n            enabled: false\n",
    )
    try:
        # The core built-in "search_files" always exists globally - disabling
        # our override doesn't remove it, it just means OUR entry never lands.
        # So the real signal is "toolset is still the untouched core one",
        # not "get_entry returns None".
        e = registry.get_entry("search_files")
        assert e is None or e.toolset != "filetools", \
            f"search_files should NOT be our override when disabled via config (got toolset={e.toolset if e else None!r})"
        for name in ("read_file", "write_file", "patch"):
            e = registry.get_entry(name)
            assert e is not None and e.toolset == "filetools", \
                f"{name} should still be OUR registration when only search_files is disabled"
    finally:
        hermes_constants.reset_hermes_home_override(token)
        for name in TOOL_NAMES:
            cur = registry.snapshot_registration(name)
            prev = snapshots[name]
            if cur is not None and cur is not prev:
                registry.restore_registration(name, cur, prev)


# ---------------------------------------------------------------------------
# E1 — loader + registration
# ---------------------------------------------------------------------------

def test_e1_plugin_discovered_and_enabled(loaded_plugin):
    entry = loaded_plugin._plugins.get("hermes-filetools")
    assert entry is not None, "hermes-filetools was not discovered by the loader"
    assert getattr(entry, "enabled", False), f"plugin should be enabled (got {entry!r})"
    err = getattr(entry, "error", None)
    assert not err, f"load error: {err!r}"


def test_e1_all_four_tools_registered(loaded_plugin):
    from tools.registry import registry

    for name in TOOL_NAMES:
        e = registry.get_entry(name)
        assert e is not None, f"{name} missing from global registry after loader run"
        assert e.toolset == "filetools", f"{name}: wrong toolset {e.toolset!r}"


def test_e1_schemas_match_contract(loaded_plugin):
    from tools.registry import registry

    for name in TOOL_NAMES:
        e = registry.get_entry(name)
        params = e.schema["parameters"]
        assert params.get("required") == EXPECTED_REQUIRED[name], \
            f"{name}: schema.required wrong: {params.get('required')!r}"
        # Q6: patch must NOT expose the cut V4A/mode parameters
    m = registry.get_entry("patch").schema["parameters"]["properties"]
    assert "mode" not in m, "patch still exposes 'mode' (Q6 says CUT)"
    assert "patch_content" not in m, "patch still exposes 'patch_content' (Q6 says CUT)"


def test_e1_no_slot_has_an_optional_property(loaded_plugin):
    """Plan08's whole point as ONE assertion: every advertised property is
    required, so anything else lives in the `flags` string.

    An invariant rather than a per-tool list because that is what a weak model
    can hold - "everything I see, I must fill" instead of "read_file has three
    options, write_file one, search_files one, patch none". It also makes a new
    optional parameter impossible to add unnoticed.
    """
    from tools.registry import registry

    for name in TOOL_NAMES:
        params = registry.get_entry(name).schema["parameters"]
        optional = set(params.get("properties") or {}) - set(params.get("required") or [])
        assert not optional, (
            f"{name}: optional propert(ies) {sorted(optional)} - Plan08 says these "
            f"belong in the required `flags` string, not in the schema"
        )


def test_e1_every_flags_description_advertises_help(loaded_plugin):
    """`--help` IS our progressive schema loading: the schema stays short and
    the full list is one call away. A rewrite that drops the mention loses
    nothing loudly - no error anywhere, the model simply never learns which
    flags exist."""
    from tools.registry import registry

    for name in TOOL_NAMES:
        props = registry.get_entry(name).schema["parameters"]["properties"]
        assert "flags" in props, f"{name}: no `flags` property at all"
        assert "--help" in (props["flags"].get("description") or ""), \
            f"{name}: `flags` description no longer advertises --help"


def test_e1_get_definitions_new_names(loaded_plugin):
    from tools.registry import registry

    defs = registry.get_definitions(set(TOOL_NAMES))
    by_name = {d["function"]["name"]: d for d in defs}
    assert set(by_name) == set(TOOL_NAMES), f"get_definitions names: {sorted(by_name)}"
    for name, d in by_name.items():
        assert d["type"] == "function"
        assert d["function"].get("name") == name  # core overrides schema name with entry.name


# ---------------------------------------------------------------------------
# E2 — dispatch returns JSON strings (success path)
# ---------------------------------------------------------------------------

def _dispatch_ok(name, args):
    from tools.registry import registry

    result = registry.dispatch(name, args, task_id="t", session_id="s")
    assert isinstance(result, str), f"{name}: expected str result, got {type(result)}"
    assert "unsupported result type" not in result, \
        f"{name}: core rejected the handler result: {result[:200]}"
    data = json.loads(result)  # must be parseable JSON
    return data


def _split_envelope(text):
    """(payload | None, body) for a response that may carry a UI JSON block on
    its first line (CONTRACT.md §19). `min` is the shipped default for patch and
    write_file, so tests must not assume the text starts the response."""
    first, _, rest = text.partition(chr(10))
    if first.startswith("{"):
        try:
            return json.loads(first), rest
        except ValueError:
            pass
    return None, text


def _dispatch_text(name, args):
    """read_file/write_file's "new" path (default as of 2026-08-25): plain
    text, not JSON - see _dispatch_ok for the still-JSON tools (patch,
    search_files)."""
    from tools.registry import registry

    result = registry.dispatch(name, args, task_id="t", session_id="s")
    assert isinstance(result, str), f"{name}: expected str result, got {type(result)}"
    assert "unsupported result type" not in result, \
        f"{name}: core rejected the handler result: {result[:200]}"
    return result


def test_e2_view_file_dispatch(loaded_plugin):
    text = _dispatch_text("read_file", {"path": str(EDGE_PY), "line": 1, "count": 5})
    assert not text.startswith("ERROR"), f"unexpected error: {text}"
    assert "File:" in text and "BLOCK" in text, f"missing header/block framing: {text[:200]}"


def test_e2_view_file_dispatch_records_the_ledger(loaded_plugin, tmp_path):
    """Wiring for the read ledger (Plan02-Task01): the piece the unit tests
    cannot cover is that `session_id` survives the whole chain —
    registry.dispatch(session_id=…) → our handler's **kw → the core's
    parameter → the file name. A ledger filed under the wrong session is
    invisible to the panel, and the failure is silent by design (fail-open),
    so it has to be asserted here or not at all.

    `loaded_plugin` already points hermes home at tmp_path and holds the
    override open for the whole test body, so the ledger lands under it.
    """
    from hermes_filetools.file_ops import _ledger

    text = _dispatch_text("read_file", {"path": str(EDGE_PY), "line": 1, "count": 5})
    assert not text.startswith("ERROR"), f"unexpected error: {text}"

    entries = _ledger.read_ledger("s")   # _dispatch_text dispatches with session_id="s"
    assert entries, "read_file dispatched but nothing reached the ledger"
    e = entries[-1]
    assert e["session"] == "s"
    assert e["task_id"] == "t"
    assert e["path"].endswith("Edge.py")
    assert e["sha8"] == _ledger.sha8(text), "digest must cover what the model received"
    # Under the fixture's home, not the ambient one: a ledger written to the
    # real ~/.hermes during tests would be both a leak and a false positive.
    assert _ledger.ledger_path("s").parent == tmp_path / "hermes-home" / "filetools" / "ledger"


def _anchor_fixture(tmp_path, name="anchored.py", crlf=False):
    """80 lines, blank every 7th - long enough for several anchor slots.

    newline is pinned explicitly: the default would be the platform's, which
    silently made this a CRLF file on Windows and turned every LF assertion
    into an accidental CRLF test (that is how the anchor EOL bug surfaced -
    see test_e2_view_file_anchor_round_trip_crlf)."""
    target = tmp_path / name
    lines = ["" if i % 7 == 0 else f"value_{i} = {i}" for i in range(1, 81)]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8",
                      newline="\r\n" if crlf else "\n")
    return target


def _anchor_round_trip(target):
    """Read the file, lift a 3-line excerpt straight out of the response with
    its anchor line in the middle, and patch using it verbatim. Returns
    (patch_response, file_text_before, file_text_after)."""
    text = _dispatch_text("read_file", {"path": str(target), "flags": ""})
    lines = text.split("\n")
    start = next(i for i, ln in enumerate(lines) if "BLOCK" in ln)
    body = lines[start + 1:]
    at = next(i for i, ln in enumerate(body) if "⚓" in ln)

    excerpt = "\n".join(body[at - 1:at + 2])
    replacement = "\n".join([body[at - 1].rstrip("\r"), "",
                             body[at + 1].rstrip("\r") + "  # edited"])
    before = target.read_bytes().decode("utf-8")
    out = _dispatch_text("patch", {
        "path": str(target), "line": at, "old_string": excerpt,
        "new_string": replacement, "flags": "",
    })
    return out, before, target.read_bytes().decode("utf-8")


def test_e2_view_file_anchors_only_overwrite_blank_lines(loaded_plugin, tmp_path):
    """⚓ anchors must never INSERT a line (_new_format.place_anchors' hard
    invariant): the body has to stay exactly as long as the range in the
    ■BLOCK header, and every anchor has to sit where a blank line was. Break
    that and a copied excerpt carries a line the file doesn't have, so patch's
    line-based matching stops lining up (Vision01__patch.md §10)."""
    target = _anchor_fixture(tmp_path)
    text = _dispatch_text("read_file", {"path": str(target), "flags": ""})
    assert "⚓" in text, f"no anchors in an 80-line read: {text[:400]}"

    lines = text.split("\n")
    start = next(i for i, ln in enumerate(lines) if "BLOCK" in ln)
    end = next(i for i, ln in enumerate(lines) if "END" in ln)
    body = lines[start + 1:end]
    assert len(body) == 80, f"body is {len(body)} lines, file region is 80 - anchors shifted it"

    original = target.read_text(encoding="utf-8").split("\n")[:80]
    for i, ln in enumerate(body):
        if "⚓" in ln:
            assert original[i] == "", \
                f"anchor at line {i + 1} overwrote real content: {original[i]!r}"
            assert f"Line: {i + 1}" in ln, f"anchor states the wrong line number: {ln!r}"


def test_e2_view_file_anchor_survives_round_trip_into_patch(loaded_plugin, tmp_path):
    """The point of the blank-line invariant, end to end: copy an excerpt out
    of a read INCLUDING its anchor line, hand it straight to patch, and the
    match must still be found - the anchor line is restored to the blank line
    it took over, not simply deleted."""
    target = _anchor_fixture(tmp_path)
    out, before, after = _anchor_round_trip(target)

    assert not out.startswith("ERROR"), f"copied anchor broke the match: {out}"
    assert "# edited" in after
    assert after.count("\n") == before.count("\n"), \
        "line count changed - the anchor was dropped instead of blanked"
    assert "⚓" not in after, "anchor got written into the file!"


def test_e2_view_file_anchor_round_trip_crlf(loaded_plugin, tmp_path):
    """Same round trip on a CRLF file. This found two real bugs (2026-08-26):

    1. place_anchors built the anchor line from scratch, so it had no trailing
       "\\r" while every real line around it did - the body came back with
       mixed line endings on exactly the lines we touched, and an excerpt
       copied out of it no longer matched the file exactly.
    2. mocks.py::patch_replace read the file WITHOUT newline='', so universal
       newline translation flattened its CRLFs and an old_string legitimately
       containing "\\r\\n" could never be found - i.e. every patch of a CRLF
       file failed in console/mock mode, anchors or not. The real backend
       reads through `cat` and was never affected, so the mock was STRICTER
       than reality here (the mirror image of the replacement-count case,
       where it was more forgiving).

    Both directions of mock/real divergence are traps, hence this test pins
    the EOL style explicitly instead of taking the platform default.
    """
    target = _anchor_fixture(tmp_path, name="anchored_crlf.py", crlf=True)
    assert b"\r\n" in target.read_bytes(), "fixture is not actually CRLF"

    out, before, after = _anchor_round_trip(target)
    assert not out.startswith("ERROR"), f"CRLF round trip failed: {out}"
    assert "# edited" in after
    assert after.count("\n") == before.count("\n")
    assert "⚓" not in after
    assert b"\r\n" in target.read_bytes(), "the file's CRLF style was destroyed"


def test_e0_anchor_gap_from_config(tmp_path, monkeypatch):
    """Anchor density is operator-controlled (plugin.yaml params.anchor_gap),
    not a model-facing flag - the marks are passive, and a switch the model can
    flip stops being passive (CONTRACT.md §18). Proves the config value really
    reaches place_anchors instead of being accepted and ignored."""
    import hermes_constants
    from tools.registry import registry

    snapshots = {name: registry.snapshot_registration(name) for name in TOOL_NAMES}
    manager, token = _load_plugin(
        tmp_path, monkeypatch,
        settings_yaml="          read_file:\n            params:\n              anchor_gap: 10\n",
    )
    try:
        target = _anchor_fixture(tmp_path)  # 80 lines, blank every 7th
        text = _dispatch_text("read_file", {"path": str(target), "flags": ""})
        dense = text.count("⚓")
        assert dense >= 4, f"anchor_gap: 10 should give several anchors, got {dense}"
    finally:
        hermes_constants.reset_hermes_home_override(token)
        for name in TOOL_NAMES:
            cur = registry.snapshot_registration(name)
            if cur is not None and cur is not snapshots[name]:
                registry.restore_registration(name, cur, snapshots[name])

    # Same fixture at the default gap of 25 must be visibly sparser.
    manager, token = _load_plugin(tmp_path / "second", monkeypatch)
    try:
        target2 = _anchor_fixture(tmp_path / "second", name="again.py")
        text2 = _dispatch_text("read_file", {"path": str(target2), "flags": ""})
        assert text2.count("⚓") < dense, \
            "default gap produced as many anchors as anchor_gap: 10 - config had no effect"
    finally:
        hermes_constants.reset_hermes_home_override(token)
        for name in TOOL_NAMES:
            cur = registry.snapshot_registration(name)
            if cur is not None and cur is not snapshots[name]:
                registry.restore_registration(name, cur, snapshots[name])


def test_e2_view_file_numbered_has_no_anchors(loaded_plugin, tmp_path):
    """The gutter already numbers every line; anchors there would be noise."""
    target = _anchor_fixture(tmp_path)
    text = _dispatch_text("read_file", {"path": str(target), "numbered": True, "flags": ""})
    assert "⚓" not in text, f"anchors emitted alongside the gutter: {text[:300]}"


def test_e2_view_file_no_anchors_flag(loaded_plugin, tmp_path):
    target = _anchor_fixture(tmp_path)
    text = _dispatch_text("read_file", {"path": str(target), "flags": "--no-anchors"})
    assert "⚓" not in text, f"--no-anchors ignored: {text[:300]}"


# ---------------------------------------------------------------------------
# E2f — Plan08: the paging/insert/glob parameters now live in `flags`, the
# named ones stay accepted but unadvertised, and an unrecognised flag is
# reported BY NAME instead of being ignored.
# ---------------------------------------------------------------------------

def _five_lines(tmp_path):
    # NOT named "five.txt": the path is echoed in the header, so a filename
    # containing a line's text makes "line N is absent" unassertable.
    target = tmp_path / "counting.txt"
    target.write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")
    return target


def test_e2f_read_paging_via_flags(loaded_plugin, tmp_path):
    target = _five_lines(tmp_path)
    text = _dispatch_text("read_file", {"path": str(target), "flags": "--line 3 --count 2"})
    assert not text.startswith("ERROR"), text
    assert "3-4" in text, f"block range should be 3-4: {text}"
    assert "three" in text and "four" in text and "five" not in text, text


def test_e2f_read_numbered_via_flags(loaded_plugin, tmp_path):
    target = _five_lines(tmp_path)
    text = _dispatch_text("read_file", {"path": str(target), "flags": "--numbered"})
    assert "1|one" in text, f"--numbered did not reach the gutter: {text}"


def test_e2f_read_flag_beats_the_named_parameter(loaded_plugin, tmp_path):
    """Both ways in are live, so the precedence has to be stated somewhere it
    can break: the FLAG wins, as upstream's unadvertised `notify` aliases do."""
    target = _five_lines(tmp_path)
    text = _dispatch_text(
        "read_file", {"path": str(target), "count": 5, "flags": "--count 1"})
    assert "1-1" in text, f"flag lost to the named parameter: {text}"


def test_e2f_read_count_zero_is_still_metadata_only(loaded_plugin, tmp_path):
    """--count 0 has to survive the move: 0 is a MODE, and `flag_int` must not
    treat it as "absent" and fall back to the 2000-line default."""
    target = _five_lines(tmp_path)
    text = _dispatch_text("read_file", {"path": str(target), "flags": "--count 0"})
    assert "BLOCK" not in text, f"--count 0 returned content: {text}"
    assert "FileTag:" in text, f"--count 0 lost the header: {text}"


def test_e2f_unknown_flag_is_named_not_ignored(loaded_plugin, tmp_path):
    """The "=" form is the real case: parse_flags reads "--count=2" as a flag
    literally NAMED "count=2", so ignoring it would read the whole file while
    the model believes it paged - exactly the confusion Plan08 removes."""
    target = _five_lines(tmp_path)
    text = _dispatch_text("read_file", {"path": str(target), "flags": "--count=2"})
    assert text.startswith("ERROR"), f"unknown flag was accepted: {text}"
    assert "--count=2" in text, f"the answer must name the flag: {text}"


def test_e2f_non_numeric_flag_value_says_so(loaded_plugin, tmp_path):
    target = _five_lines(tmp_path)
    text = _dispatch_text("read_file", {"path": str(target), "flags": "--count abc"})
    assert text.startswith("ERROR") and "number" in text, \
        f"a junk number must be named, not silently defaulted: {text}"


def test_e2f_write_insert_via_flag(loaded_plugin, tmp_path):
    target = _five_lines(tmp_path)
    text = _dispatch_text(
        "write_file",
        {"path": str(target), "content": "inserted\n", "flags": "--insert-line 1"},
    )
    _payload, body = _split_envelope(text)
    assert not body.startswith("ERROR"), body
    assert target.read_text(encoding="utf-8").startswith("inserted\none\n"), \
        target.read_text(encoding="utf-8")


def test_e2f_write_unknown_flag_writes_nothing(loaded_plugin, tmp_path):
    """Worst case of a silently-dropped flag in the whole plugin: a mistyped
    "--insert-line=1" that got ignored would OVERWRITE the file the model
    meant to splice into."""
    target = _five_lines(tmp_path)
    before = target.read_bytes()
    text = _dispatch_text(
        "write_file",
        {"path": str(target), "content": "inserted\n", "flags": "--insert-line=1"},
    )
    _payload, body = _split_envelope(text)
    assert body.startswith("ERROR") and "--insert-line=1" in body, f"accepted: {body}"
    assert target.read_bytes() == before, "the file was written despite the refusal"


def test_e2f_search_glob_via_flag(loaded_plugin):
    text = _dispatch_text(
        "search_files",
        {"path": str(FIXTURES), "pattern": "def ", "flags": "--glob *.py"},
    )
    assert not text.startswith("ERROR"), text
    assert "glob=*.py" in text, f"the glob flag never reached the search: {text[:300]}"


def test_e2f_search_pagination_hint_keeps_the_other_flags(loaded_plugin):
    """Now that `glob` lives in the flag string, a hint of just "--offset N"
    would quietly widen the next search to every file - and the answer would
    look perfectly legitimate."""
    text = _dispatch_text(
        "search_files",
        {"path": str(FIXTURES), "pattern": "e", "flags": "--glob *.py --limit 1"},
    )
    hint = [ln for ln in text.split("\n") if ln.lstrip("◆").startswith("Hint:")]
    assert hint, f"expected a truncation hint: {text[:400]}"
    assert "--glob *.py" in hint[0] and "--offset" in hint[0], \
        f"the hint dropped the caller's own flags: {hint[0]}"


def test_e2f_patch_unknown_flag_changes_nothing(loaded_plugin, tmp_path):
    target = _five_lines(tmp_path)
    before = target.read_bytes()
    _payload, body = _split_envelope(_dispatch_text(
        "patch",
        {"path": str(target), "line": 1, "old_string": "one",
         "new_string": "ONE", "flags": "--context=2"},
    ))
    assert body.startswith("ERROR") and "--context=2" in body, f"accepted: {body}"
    assert target.read_bytes() == before, "the file changed despite the refusal"


def test_e2_create_file_dispatch(loaded_plugin, tmp_path):
    dest = tmp_path / "created.txt"
    text = _dispatch_text("write_file", {"path": str(dest), "content": "hello filetools\n"})
    assert "Created new file" in text, f"unexpected response: {text}"
    assert dest.read_text() == "hello filetools\n"


def test_e2_modify_file_dispatch(tmp_path, monkeypatch):
    """patch OLD path (modify_file_core.py, JSON) - the default flipped to
    "new" 2026-08-26 (Vision01__patch.md), so this opts INTO "old" explicitly
    to keep exercising it, same as test_e2_sift_files_dispatch does."""
    manager, token = _load_plugin(
        tmp_path, monkeypatch,
        settings_yaml="          patch:\n            path: old\n",
    )
    try:
        target = tmp_path / "modify-me.txt"
        target.write_text("alpha beta gamma\n", encoding="utf-8")
        data = _dispatch_ok(
            "patch",
            {"path": str(target), "old_string": "beta", "new_string": "BETA"},
        )
        assert data.get("success") is True, f"modify failed: {data}"
        assert target.read_text() == "alpha BETA gamma\n"
    finally:
        import hermes_constants
        hermes_constants.reset_hermes_home_override(token)


def test_e2_modify_file_new_dispatch(loaded_plugin, tmp_path):
    """patch "new" path (Vision01__patch.md) - the default since 2026-08-26."""
    target = tmp_path / "patch-me.py"
    target.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    text = _dispatch_text(
        "patch",
        {"path": str(target), "line": 2, "old_string": "b = 2",
         "new_string": "b = 22", "flags": ""},
    )
    assert not text.startswith("ERROR"), f"unexpected error: {text}"
    assert "Patched:" in text and "Replacements:" in text, f"missing header: {text[:200]}"
    assert "BLOCK" in text, f"missing echo block: {text[:300]}"
    assert target.read_text() == "a = 1\nb = 22\nc = 3\n"


def test_e2_modify_file_new_line_hint_disambiguates(loaded_plugin, tmp_path):
    """The whole point of `line` (Vision01__patch.md §4): two identical sites,
    no extra context quoted by the model, and the hint alone decides. A flat
    "is it inside the window" filter would call this ambiguous - the hint
    RANKS, it doesn't filter."""
    target = tmp_path / "twice.py"
    target.write_text("x = 1\ny = 0\nx = 1\n", encoding="utf-8")
    text = _dispatch_text(
        "patch",
        {"path": str(target), "line": 3, "old_string": "x = 1",
         "new_string": "x = 9", "flags": ""},
    )
    assert not text.startswith("ERROR"), f"hint failed to disambiguate: {text}"
    assert target.read_text() == "x = 1\ny = 0\nx = 9\n"


def test_e2_modify_file_new_ambiguous_without_hint(loaded_plugin, tmp_path):
    """line=0 means "I don't know" - with nothing to rank by, several matches
    must refuse and SHOW the candidates, never silently take the first."""
    target = tmp_path / "twice2.py"
    target.write_text("x = 1\ny = 0\nx = 1\n", encoding="utf-8")
    text = _dispatch_text(
        "patch",
        {"path": str(target), "line": 0, "old_string": "x = 1",
         "new_string": "x = 9", "flags": ""},
    )
    assert "ERROR" in text and "possible matches" in text, f"expected refusal: {text[:300]}"
    assert text.count("BLOCK") >= 2, f"candidates not shown: {text[:400]}"
    assert target.read_text() == "x = 1\ny = 0\nx = 1\n", "refused patch still wrote!"


def test_e2_modify_file_new_dry_run_writes_nothing(loaded_plugin, tmp_path):
    target = tmp_path / "dry.py"
    target.write_text("a = 1\n", encoding="utf-8")
    text = _dispatch_text(
        "patch",
        {"path": str(target), "line": 1, "old_string": "a = 1",
         "new_string": "a = 2", "flags": "--dry-run"},
    )
    assert "Dry run:" in text, f"unexpected response: {text[:200]}"
    assert target.read_text() == "a = 1\n", "--dry-run wrote to the file!"


def test_e2_modify_file_new_replacement_count_is_ours(tmp_path):
    """Regression, live-agent finding F1 (2026-08-26): with --replace-all the
    response must report the REAL number of replacements.

    The real backend cannot supply it - its PatchResult carries no occurrence
    count, only files_modified, a list of PATHS, which is length 1 for any
    single-file edit, so FileBackend can only ever derive 1. The count has to
    come from our own pre-write read instead (we hold the content anyway).

    The MOCK backend counts correctly, which is exactly why 32 green pytest
    runs missed this and one live run caught it - so this test strips the
    count the way the real backend lacks it, instead of trusting the mock.
    """
    if str(FILE_OPS_DIR) not in sys.path:
        sys.path.insert(0, str(FILE_OPS_DIR))
    import modify_file_new_core as mod
    import mocks

    target = tmp_path / "many.py"
    target.write_text("a = 0\nb = 0\nc = 0\n", encoding="utf-8")

    original = mocks.MockFileOperations.patch_replace

    def _without_count(self, path, old_string, new_string, replace_all=False):
        r = original(self, path, old_string, new_string, replace_all=replace_all)
        if getattr(r, "replacements", None) is not None:
            r.replacements = 1  # all the real backend is ever able to say
        return r

    mocks.MockFileOperations.patch_replace = _without_count
    try:
        text = mod.modify_file_tool_new(
            path=str(target), line=0, old_string="= 0", new_string="= 1",
            flags="--replace-all",
        )
    finally:
        mocks.MockFileOperations.patch_replace = original

    assert "Replacements: 3" in text, f"count not taken from our own read: {text}"
    assert target.read_text() == "a = 1\nb = 1\nc = 1\n"


def test_e0_ui_json_prefix_keeps_text_intact(tmp_path, monkeypatch):
    """params.ui_json = min: a short JSON block goes ABOVE the answer, and the
    plain-text answer below it stays byte-for-byte what `off` produces
    (CONTRACT.md §19). The reference form is the thing being protected here -
    the JSON must be an addition, never a wrapper that escapes the text."""
    import hermes_constants
    from tools.registry import registry

    snapshots = {name: registry.snapshot_registration(name) for name in TOOL_NAMES}
    manager, token = _load_plugin(
        tmp_path, monkeypatch,
        settings_yaml='          patch:\n            params:\n              ui_json: min\n',
    )
    try:
        target = tmp_path / "env.py"
        target.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8", newline="\n")
        text = _dispatch_text("patch", {
            "path": str(target), "line": 2, "old_string": "b = 2",
            "new_string": "b = 22", "flags": "",
        })
    finally:
        hermes_constants.reset_hermes_home_override(token)
        for name in TOOL_NAMES:
            cur = registry.snapshot_registration(name)
            if cur is not None and cur is not snapshots[name]:
                registry.restore_registration(name, cur, snapshots[name])

    first, _, rest = text.partition("\n")
    payload = json.loads(first)  # must be valid JSON on its own first line
    assert payload["success"] is True and payload["replacements"] == 1
    assert payload["files_modified"] == [str(target)]
    assert "diff" not in payload, "min mode must not carry code"
    # The text below is the untouched reference form: real newlines, no escapes.
    # The envelope's own line is followed by the operation address (Plan09),
    # then the reference text.
    _op, rest = split_op_line(rest)
    assert rest.startswith("#Patched:") and "\\n" not in rest
    assert "■BLOCK" in rest and "b = 22" in rest


# `ui_json: min` PINNED for the two failure tests below: the machine-readable failure block they
# check exists only in that mode, and the manifest default is `off` since 2026-10-02 (owner: with
# `min` the desktop app rendered a failed call's error several times). The mode itself stays.
_PATCH_UI_JSON_MIN = "          patch:\n            params:\n              ui_json: min\n"


def _dispatch_patch_min(tmp_path, monkeypatch, args):
    _manager, token = _load_plugin(tmp_path, monkeypatch, settings_yaml=_PATCH_UI_JSON_MIN)
    try:
        return _dispatch_text("patch", args)
    finally:
        import hermes_constants
        hermes_constants.reset_hermes_home_override(token)


def test_e2_modify_file_new_rejects_pasted_gutter(tmp_path, monkeypatch):
    """A pasted "N|" gutter is a corruption of the code, not one of our marker
    lines - it must be a distinguishable ERROR, never a silent repair
    (Vision01__patch.md §10)."""
    target = tmp_path / "gut.py"
    target.write_text("a = 1\n", encoding="utf-8")
    text = _dispatch_patch_min(
        tmp_path, monkeypatch,
        {"path": str(target), "line": 1, "old_string": "1|a = 1",
         "new_string": "a = 2", "flags": ""},
    )
    payload, body = _split_envelope(text)
    assert body.startswith("ERROR") and "gutter" in body, f"expected gutter refusal: {text}"
    # A failure must be machine-readable too: clients otherwise guess from prose
    # and can render a failed edit as a successful call (CONTRACT.md §19).
    assert payload is not None and payload["success"] is False
    assert "files_modified" not in payload, "nothing was written - don't claim a changed file"
    assert target.read_text() == "a = 1\n"


def test_e2_sift_files_dispatch(tmp_path, monkeypatch):
    """search_files OLD path (sift_files_core.py, JSON, target/output_mode
    enums) - the default flipped to "new" 2026-08-26 (Vision01__search.md),
    so this now opts INTO "old" explicitly to keep exercising it, same file/
    contract as before, unaffected by the flip."""
    manager, token = _load_plugin(
        tmp_path, monkeypatch,
        settings_yaml="          search_files:\n            path: old\n",
    )
    try:
        data = _dispatch_ok(
            "search_files",
            {"pattern": "def ", "target": "content", "path": str(FIXTURES),
             "file_glob": "*.py", "limit": 3},
        )
        assert data.get("total_count", 0) > 0, f"no matches found: {data}"
    finally:
        import hermes_constants
        hermes_constants.reset_hermes_home_override(token)


def test_e2_sift_files_new_dispatch(loaded_plugin):
    """search_files "new" path (Vision01__search.md) - the default since
    2026-08-26 (plugin.yaml), live-agent confirmation still pending."""
    text = _dispatch_text(
        "search_files",
        {"path": str(FIXTURES), "pattern": "def ", "glob": "*.py", "flags": ""},
    )
    assert not text.startswith("ERROR"), f"unexpected error: {text}"
    assert "Search:" in text and "Found:" in text, f"missing header: {text[:200]}"
    assert "|" in text, f"missing N|content gutter: {text[:200]}"


def test_e2_sift_files_new_filenames_only(loaded_plugin):
    """pattern="" - filenames only, no content search (Vision01__search.md §4)."""
    text = _dispatch_text(
        "search_files",
        {"path": str(FIXTURES), "pattern": "", "glob": "*.py", "flags": ""},
    )
    assert not text.startswith("ERROR"), f"unexpected error: {text}"
    assert "Edge.py" in text, f"expected Edge.py in filename listing: {text}"


# ---------------------------------------------------------------------------
# E2b — execute_code sandbox bridge (Recon10/_execute_code_bridge.py):
# register() patches code_execution_tool's own stub tables so the sandbox
# stub matches our real schema and its RPC client tolerates our plain-text
# response instead of crashing on json.loads(). Runs as a side effect of
# loading the plugin (same fixture as everything else) - no separate setup.
# ---------------------------------------------------------------------------

def test_e2b_execute_code_stubs_patched(loaded_plugin):
    from tools.code_execution_tool import generate_hermes_tools_module, SANDBOX_ALLOWED_TOOLS
    import tools.code_execution_tool as cet

    from _execute_code_bridge import _FACADE_MARKER  # console-mode bare import

    for name in ("read_file", "write_file", "search_files", "patch"):
        # `*_` swallows the leading name element on the pre-refactor 4-tuple
        # shape (name, sig, doc, args_expr) and nothing on the current
        # 3-tuple shape (sig, doc, args_expr) - shape isn't ours to assume,
        # see _execute_code_bridge.py::patch_execute_code_bridge.
        *_, sig, doc, args_expr = cet._TOOL_STUBS[name]
        assert "flags" in sig, f"{name}: patched stub signature missing 'flags': {sig}"
        # Since Plan06 the sandbox facade DOES answer with a dict - three
        # fixed keys, same for every tool. What must never come back is
        # core's own promise of per-tool keys to index into.
        assert '"exit_code"' in doc and '"header"' in doc and '"body"' in doc, \
            f"{name}: stub doc doesn't state the three facade keys: {doc}"
        assert _FACADE_MARKER in args_expr, \
            f"{name}: stub sends no facade marker, so it will get plain text: {args_expr}"
        assert _FACADE_MARKER not in sig, \
            f"{name}: the marker leaked into the signature the model reads: {sig}"
        # Required-before-optional or this is a SyntaxError once generated
        # (caught manually 2026-08-26 before this test existed - naive
        # schema-declaration-order copy put `flags` after defaulted params).
        compile(f"def _check({sig}):\n    pass\n", "<stub-check>", "exec")

    doc_lines = dict(cet._TOOL_DOC_LINES)
    for name in ("read_file", "write_file", "search_files", "patch"):
        assert "-> dict" in doc_lines[name], \
            f"{name}: doc line not patched: {doc_lines[name]}"
        assert '"exit_code"' in doc_lines[name], \
            f"{name}: doc line doesn't state the facade keys: {doc_lines[name]}"

    # patch is the one tool whose chaining is quietly unsafe: its own inserted
    # text can become a closer match for a later patch's anchor, and nothing
    # errors. Our tools must not nudge the model into that.
    assert "In a chain use very carefully" in doc_lines["patch"], \
        f"patch's doc line lost the chaining caution: {doc_lines['patch']}"
    for name in ("read_file", "write_file", "search_files"):
        assert "In a chain" not in doc_lines[name], \
            f"{name} carries patch's caution, which does not apply to it"

    src = generate_hermes_tools_module(list(SANDBOX_ALLOWED_TOOLS))
    compile(src, "hermes_tools.py", "exec")  # full generated sandbox module still valid Python

    for header_name in ("_UDS_TRANSPORT_HEADER", "_FILE_TRANSPORT_HEADER"):
        header = getattr(cet, header_name)
        assert "except json.JSONDecodeError:\n        return raw" in header, \
            f"{header_name}: json.loads() fallback patch missing"


def test_e2b_terminal_stub_patched_and_blocked_params_excluded(loaded_plugin):
    """`terminal` joined the bridge list on 2026-09-02 (path: new), which fixed
    a live lie: its sandbox answer has been plain text since Plan03 while
    core's doc line still promised `-> dict` with "output"/"exit_code" keys to
    index into.

    The second half is the part that is easy to get wrong. Our terminal.yaml
    declares background/pty/notify_on_complete/watch_patterns, but
    `_rpc_server_loop` POPS exactly those before dispatch
    (`_TERMINAL_BLOCKED_PARAMS`, code_execution_tool.py:739). A stub built from
    the full schema would advertise four parameters that silently do nothing -
    worse than not offering them at all, because the script gets no error back.
    """
    import tools.code_execution_tool as cet

    *_, sig, doc, args_expr = cet._TOOL_STUBS["terminal"]
    assert "command" in sig, f"terminal stub lost its command param: {sig}"
    assert "-> dict" not in doc, f"terminal stub doc still promises a dict: {doc}"
    compile(f"def _check({sig}):\n    pass\n", "<stub-check>", "exec")

    # Constant travels between modules across core versions (was
    # code_execution_tool.py, moved to code_execution_rpc.py alongside
    # `_rpc_server_loop`, the thing that actually pops these params) - same
    # resolution order as _execute_code_bridge.py::_blocked_params.
    blocked_params = getattr(cet, "_TERMINAL_BLOCKED_PARAMS", None)
    if blocked_params is None:
        from tools.code_execution_rpc import _TERMINAL_BLOCKED_PARAMS as blocked_params
    assert blocked_params, \
        "core declares no blocked terminal params - this test's premise is gone"
    for blocked in blocked_params:
        assert blocked not in sig, \
            f"terminal stub advertises {blocked!r}, which the bridge strips: {sig}"
        assert blocked not in args_expr, \
            f"terminal stub still sends {blocked!r} over RPC: {args_expr}"

    # The exclusion must be STATED, not silent - core's own doc line said
    # "Foreground only (no background/pty)" and that warning has to survive.
    doc_lines = dict(cet._TOOL_DOC_LINES)
    assert "-> dict" in doc_lines["terminal"], \
        f"terminal doc line not patched: {doc_lines['terminal']}"
    assert "Not available here" in doc_lines["terminal"], \
        f"terminal doc line drops the blocked params silently: {doc_lines['terminal']}"


def test_e2b_facade_marker_round_trips_through_dispatch(loaded_plugin, tmp_path):
    """The marker lives in two modules and only one can own the string: the
    bridge writes it into the stub's args expression, the plugin root pops it
    off the incoming args. `_execute_code_bridge` cannot import the plugin root
    (the root imports it), so there is no shared constant to point both at.

    Asserting the two literals are equal would test the strings; this tests the
    seam - the marker the STUB would really send, dispatched through the real
    registry, has to come back as a dict. Drift between the two files does not
    raise anything: every sandbox call would silently get plain text again,
    which is exactly the regression this facade exists to prevent.
    """
    from tools.registry import registry
    from _execute_code_bridge import _FACADE_MARKER  # what the stub sends

    target = tmp_path / "facade.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    args = {"path": str(target), "flags": ""}

    plain = registry.dispatch("read_file", dict(args), task_id="t", session_id="s")
    assert isinstance(plain, str) and not plain.lstrip().startswith("{"), \
        f"unmarked call must stay plain text for the model: {plain[:120]!r}"

    marked = registry.dispatch("read_file", {**args, _FACADE_MARKER: True},
                               task_id="t", session_id="s")
    data = json.loads(marked)
    assert set(data) == {"exit_code", "header", "body"}, \
        f"facade must be exactly the three keys, got {sorted(data)}"
    assert data["exit_code"] == 0, f"a successful read is not an error: {data}"
    # read_file's divider is `◆○○○` - comment-wrapped per our own convention -
    # so a splitter matching a line EQUAL to ○○○ would put everything in the
    # header and leave body empty. That is the regression this line catches.
    assert "alpha" in data["body"] and "beta" in data["body"], \
        f"body lost the file's own content: {data}"
    assert "alpha" not in data["header"], \
        f"header swallowed the body - separator not recognised: {data}"


def test_e2b_facade_read_body_is_the_file_itself(loaded_plugin, tmp_path):
    """The facade's `body` for read_file must be the FILE, not a marked-up
    view of it. The test is a round-trip because that is the actual use: a
    script reads a file and writes it back, and it has to get a copy. Before
    2026-09-02 the body carried `◆■BLOCK`/`◆■END` lines, so that script wrote
    a corrupted file (found by printing r2["body"] in a live session).
    """
    from tools.registry import registry
    from _execute_code_bridge import _FACADE_MARKER

    # newline="" so the bytes on disk are exactly these - the assertion below
    # compares BYTES, since read_text() would hide a newline change by
    # translating it on the way back and "a copy" has to mean a copy.
    # The blank line matters too: an ⚓ anchor overwrites a blank line, so
    # undoing one means restoring the blank, not dropping the line.
    original = "first\nsecond\n\nfourth after a blank\n"
    source = tmp_path / "round.txt"
    with open(source, "w", encoding="utf-8", newline="") as fh:
        fh.write(original)

    marked = registry.dispatch(
        "read_file", {"path": str(source), "flags": "", _FACADE_MARKER: True},
        task_id="t", session_id="s")
    facade = json.loads(marked)
    body = facade["body"]
    for token in ("BLOCK", "END", "FileTag"):
        assert token not in body, f"marker vocabulary left in the body: {body!r}"

    # ...but the range must not be LOST, only moved: ■BLOCK is the only place
    # that says which lines of the file the body covers, and a ranged read
    # whose range was deleted reads exactly like a whole-file read.
    assert "BLOCK" in facade["header"], \
        f"the ■BLOCK range vanished instead of moving to the header: {facade}"

    copy = tmp_path / "copy.txt"
    written = registry.dispatch(
        "write_file", {"path": str(copy), "content": body, "flags": ""},
        task_id="t", session_id="s")
    assert "ERROR" not in written, written

    # Everything except the FINAL newline, and the exception is not laziness -
    # it is a limit of read_file's marker format itself, above this facade.
    # `◆■END` is emitted directly after the last content line, so the file's
    # trailing newline IS that line's separator: a file ending "x\n" and one
    # ending "x" produce byte-identical bodies, and no amount of stripping can
    # tell them apart afterwards. The model's own text path has always had the
    # same gap. Asserting full byte equality here would mean asserting
    # something the format cannot deliver; asserting nothing about bytes would
    # let a real newline bug through. So: bytes must match modulo one trailing
    # newline, and the gap is reported rather than hidden.
    wrote, origin = copy.read_bytes(), source.read_bytes()
    assert wrote == origin.rstrip(b"\r\n"), (
        f"read -> write differs by more than the trailing newline:\n"
        f"  wrote:    {wrote!r}\n"
        f"  original: {origin!r}"
    )


# ---------------------------------------------------------------------------
# E2d — OUR execute_code slot (Plan04 task 4). Distinct from E2b above, which
# is about the SANDBOX's stubs for our file tools; this is about the
# execute_code tool itself being ours and staying disableable.
# ---------------------------------------------------------------------------

def test_e2d_execute_code_is_ours_under_their_toolset(loaded_plugin):
    """Three things at once, and each was a way to get this wrong:

    - our handler is in place (the whole point);
    - `toolset == "code_execution"`, THEIRS, so the operator's existing switch
      still turns the tool off. Under "filetools" it would become
      un-disableable - the mirror image of the desktop_ui problem;
    - `check_fn` is upstream's own `check_sandbox_requirements`, forwarded not
      re-implemented, so when there is no sandbox our entry is invisible for
      exactly the same reason theirs is.
    """
    from tools.registry import registry
    from tools.code_execution_tool import check_sandbox_requirements

    e = registry.get_entry("execute_code")
    assert e is not None, "execute_code missing from the registry entirely"
    assert e.toolset == "code_execution", \
        f"execute_code must stay in THEIR toolset to remain disableable (got {e.toolset!r})"
    assert e.check_fn is check_sandbox_requirements, \
        "execute_code's availability check must be upstream's own, forwarded"
    assert "_make_handle_execute_code" in getattr(e.handler, "__qualname__", ""), \
        f"execute_code handler is not our wrapper closure: {e.handler!r}"


def test_e2d_execute_code_slot_disabled_via_config(tmp_path, monkeypatch):
    """`settings.tools.execute_code.enabled: false` must leave upstream's own
    JSON-returning execute_code in place - the escape hatch every new slot
    ships with (Plan04 risk 2: interception is only confirmable live)."""
    import hermes_constants
    from tools.registry import registry

    names = TOOL_NAMES + ["execute_code"]
    snapshots = {name: registry.snapshot_registration(name) for name in names}
    manager, token = _load_plugin(
        tmp_path, monkeypatch,
        settings_yaml="          execute_code:\n            enabled: false\n",
    )
    try:
        e = registry.get_entry("execute_code")
        # Their registration uses their handler; ours is a closure from
        # _make_handle_execute_code. "Not ours" is the assertion, not "absent":
        # the built-in always exists.
        assert e is None or "_make_handle_execute_code" not in getattr(
            e.handler, "__qualname__", ""), \
            "execute_code should NOT be our wrapper when disabled via config"
    finally:
        hermes_constants.reset_hermes_home_override(token)
        for name in names:
            cur = registry.snapshot_registration(name)
            prev = snapshots[name]
            if cur is not None and cur is not prev:
                registry.restore_registration(name, cur, prev)


def _process_name() -> str:
    """The spelling THIS Hermes registers for the background-process tool.

    Mirrors `__init__.py::_upstream_name` on purpose, candidates in the same
    order: upstream renamed `process` -> `process_manage` at afc3d9d34, so a
    test hardcoding either one fails on the other version for a reason
    unrelated to what it checks. Newest first, because on a reload our own
    previous registration is still in the registry under last run's name.
    """
    from tools.registry import registry

    for name in ("process_manage", "process"):
        if name in registry._tools:
            return name
    return "process_manage"


def test_e2d_process_is_ours_under_their_toolset(loaded_plugin):
    """Same three checks as execute_code above, minus check_fn: upstream
    declares none for `process` (tools/process_registry.py:3357), so ours must
    not invent one either - inventing a gate the original never had is how a
    working tool quietly disappears.

    Looks the name up the way register() does instead of hardcoding it:
    upstream renamed this tool `process` -> `process_manage` at afc3d9d34, and
    a test pinned to one spelling would fail on the other Hermes for a reason
    that has nothing to do with what it is checking.
    """
    from tools.registry import registry

    name = _process_name()
    e = registry.get_entry(name)
    assert e is not None, f"{name} missing from the registry entirely"
    assert e.toolset == "terminal", \
        f"process must stay in THEIR toolset to remain disableable (got {e.toolset!r})"
    assert e.check_fn is None, \
        "upstream declares no availability check for process; ours must not add one"
    assert "_make_handle_process" in getattr(e.handler, "__qualname__", ""), \
        f"process handler is not our wrapper closure: {e.handler!r}"
    assert e.schema["parameters"]["required"] == ["action"]


def test_e2d_vision_analyze_is_ours_and_async(loaded_plugin):
    """`is_async` is the one that bites late: their handler is a coroutine, so
    getting the flag wrong does not fail at registration - it fails on the
    first call, in a live session (Plan05)."""
    from tools.registry import registry
    from tools.vision_tools import check_vision_requirements

    e = registry.get_entry("vision_analyze")
    assert e is not None, "vision_analyze missing from the registry entirely"
    assert e.toolset == "vision", \
        f"vision_analyze must stay in THEIR toolset to remain disableable (got {e.toolset!r})"
    assert e.check_fn is check_vision_requirements, \
        "vision_analyze's availability check must be upstream's own, forwarded"
    assert e.is_async is True, "their handler is a coroutine; ours must be registered async"
    assert "_make_handle_vision_analyze" in getattr(e.handler, "__qualname__", ""), \
        f"vision_analyze handler is not our wrapper closure: {e.handler!r}"
    assert e.schema["parameters"]["required"] == ["image_url", "question"]


# CONFIG slot key -> our handler factory. The slot key is OUR identifier and
# never changes; the name a slot registers UNDER can (upstream renamed
# process -> process_manage), so registry lookups go through
# `_registry_name` rather than reusing the key.
_WRAPPED_SLOT_CLOSURES = {
    "terminal": "_make_handle_terminal",
    "execute_code": "_make_handle_execute_code",
    "process": "_make_handle_process",
    "vision_analyze": "_make_handle_vision_analyze",
}


def _registry_name(slot: str) -> str:
    return _process_name() if slot == "process" else slot


def test_e2d_path_old_leaves_upstream_for_wrapped_slots(tmp_path, monkeypatch):
    """`path: old` on a WRAPPED slot means "register nothing" - upstream's own
    tool stays. Distinct from the `enabled: false` test above on purpose: the
    two keys reach the same outcome through two different code paths (the
    register loop's own branch for `terminal`, `_wrapper_disabled` for the
    other three), so one passing tells you nothing about the other.

    None of these four has an old implementation of OURS. Before 2026-09-02
    `terminal` was declared `path: old` and registered the wrapper anyway,
    just without `symbols` - the config said one thing, the code did another.

    Loads TWICE, and the control load is the point: with default settings
    every slot must come out ours. Asserting only the negative half passes
    vacuously the moment the isolated load stops installing anything at all -
    which is exactly what happened when this test was first written.
    """
    import hermes_constants
    from tools.registry import registry

    names = TOOL_NAMES + [_registry_name(n) for n in _WRAPPED_SLOT_CLOSURES
                          if _registry_name(n) not in TOOL_NAMES]

    def _which_are_ours(sub_dir, settings_yaml):
        snapshots = {name: registry.snapshot_registration(name) for name in names}
        _, token = _load_plugin(sub_dir, monkeypatch, settings_yaml=settings_yaml)
        try:
            ours = {}
            for slot, qualname in _WRAPPED_SLOT_CLOSURES.items():
                e = registry.get_entry(_registry_name(slot))
                ours[slot] = e is not None and qualname in getattr(
                    e.handler, "__qualname__", "")
            return ours
        finally:
            hermes_constants.reset_hermes_home_override(token)
            for name in names:
                cur = registry.snapshot_registration(name)
                prev = snapshots[name]
                if cur is not None and cur is not prev:
                    registry.restore_registration(name, cur, prev)

    control = _which_are_ours(tmp_path / "control", "")
    assert all(control.values()), \
        f"control load installed no wrapper, so the negative half proves nothing: {control}"

    settings = "".join(
        f"          {slot}:\n            path: old\n"
        for slot in _WRAPPED_SLOT_CLOSURES
    )
    switched_off = _which_are_ours(tmp_path / "pathold", settings)
    assert not any(switched_off.values()), \
        f"path: old still installed our wrapper: {switched_off}"


def test_e2c_execute_code_bridge_seam_status(loaded_plugin):
    """Explicit "did Hermes's internals still match our assumptions" check
    (CONTRACT.md §16) - run this (or the whole suite) after any Hermes
    core update. patch_execute_code_bridge() returns a {check: bool} status
    report specifically so this can assert on it directly, instead of only
    inferring health from downstream values (test_e2b) - a clearer failure
    here points straight at which of the 3 patch points (_TOOL_STUBS,
    _TOOL_DOC_LINES, the _call() json.loads() tail) code_execution_tool.py
    restructured, rather than a generic "stub missing 'flags'" failure.

    Calls patch_execute_code_bridge() a SECOND time on purpose (loaded_plugin
    already triggered it once via register()) - also exercises the
    idempotency fix (a naive re-check would report the json_fallback keys
    as False on re-application, since the "old" tail is gone by definition
    once patched - see the function's own docstring)."""
    from tools.registry import registry

    if str(FILE_OPS_DIR) not in sys.path:
        sys.path.insert(0, str(FILE_OPS_DIR))
    from _execute_code_bridge import patch_execute_code_bridge  # console-mode bare import

    schemas = {}
    for name in ("read_file", "write_file", "search_files", "patch"):
        entry = registry.get_entry(name)  # scope-aware - registry._tools is NOT (returns core's own unscoped entry)
        assert entry is not None and entry.toolset == "filetools", \
            f"{name}: not our registration - can't build a seam check from it"
        schemas[name] = entry.schema

    status = patch_execute_code_bridge(schemas)
    failed = {k: v for k, v in status.items() if not v}
    assert not failed, (
        f"execute_code bridge seam check failed: {sorted(failed)} - "
        "tools.code_execution_tool's internals likely changed in this Hermes "
        "version, see file_ops/_execute_code_bridge.py and CONTRACT.md §16"
    )


# ---------------------------------------------------------------------------
# E3 — error contract (JSON {"error": …}, no crash)
# ---------------------------------------------------------------------------

def test_e3_view_file_missing_path_field(loaded_plugin):
    text = _dispatch_text("read_file", {})
    assert text.startswith("ERROR"), f"expected an ERROR line, got: {text}"


def test_e3_view_file_nonexistent_file(loaded_plugin):
    text = _dispatch_text("read_file", {"path": "/nonexistent/filetools/none.txt"})
    assert text.startswith("ERROR"), f"expected an ERROR line, got: {text}"


def test_e3_modify_file_missing_fields(loaded_plugin):
    """A dropped required field names exactly which one (Vision01__patch.md §8) -
    the 2.7%-of-real-calls failure mode from Recon06."""
    _payload, body = _split_envelope(_dispatch_text("patch", {"path": "/tmp/x.txt"}))
    assert body.startswith("ERROR") and "old_string" in body, f"unclear error: {body}"


def test_e3_modify_file_missing_line_degrades(loaded_plugin, tmp_path):
    """`line` is required in the schema, but a dropped one must not crash: 0
    ("I don't know") is an honest value, so it degrades to a whole-file search
    instead of an error the model can't act on."""
    target = tmp_path / "noline.txt"
    target.write_text("alpha beta\n", encoding="utf-8")
    text = _dispatch_text(
        "patch",
        {"path": str(target), "old_string": "beta", "new_string": "BETA", "flags": ""},
    )
    assert not text.startswith("ERROR"), f"missing 'line' should degrade, not fail: {text}"
    assert target.read_text() == "alpha BETA\n"


def test_e3_modify_file_old_string_not_found(tmp_path, monkeypatch):
    target = tmp_path / "nomatch.txt"
    target.write_text("some content\n", encoding="utf-8")
    text = _dispatch_patch_min(
        tmp_path, monkeypatch,
        {"path": str(target), "line": 1, "old_string": "absent-string",
         "new_string": "x", "flags": ""},
    )
    payload, body = _split_envelope(text)
    assert body.startswith("ERROR") and "not found" in body, f"unclear error: {text}"
    assert payload is not None and payload["success"] is False


# ---------------------------------------------------------------------------
# E4 — console: each core callable WITHOUT Hermes (subprocess)
# ---------------------------------------------------------------------------

def _run_console(script, *args):
    proc = subprocess.run(
        [sys.executable, str(FILE_OPS_DIR / script), *map(str, args)],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=60,
    )
    assert proc.returncode == 0, f"{script} exited {proc.returncode}: {proc.stderr[:400]}"
    return json.loads(proc.stdout)


def test_e4_console_view_file():
    data = _run_console("view_file_core.py", EDGE_PY, "--limit", "5")
    assert data["total_lines"] > 0 and "content" in data


def test_e4_console_view_file_base_dir():
    """Regression (prototype bug): file_ops re-resolved the path against cwd, ignoring --base-dir."""
    data = _run_console("view_file_core.py", "Edge.py", "--base-dir", str(FIXTURES))
    assert data["total_lines"] > 0 and "content" in data, f"--base-dir ignored: {data}"


def test_e4_console_create_and_modify(tmp_path):
    dest = tmp_path / "console-out.txt"
    created = _run_console("create_file_core.py", dest, "--content", "one two three\n")
    assert created.get("success") is True
    modified = _run_console("modify_file_core.py", dest, "-o", "two", "-n", "TWO")
    assert modified.get("success") is True
    assert dest.read_text() == "one TWO three\n"


def test_e4_console_sift_files():
    data = _run_console("sift_files_core.py", "def ", "--target", "content",
                        "--path", str(FIXTURES), "--file-glob", "*.py", "--limit", "3")
    assert data.get("total_count", 0) > 0


def test_e4_console_sift_files_only_and_count():
    """Regression (prototype bug): grep -l/-c output isn't `file:line:text`, so the old
    parser dropped every match and both modes returned empty results."""
    fo = _run_console("sift_files_core.py", "import logging", "--output-mode", "files_only",
                      "--path", str(FIXTURES))
    assert fo.get("total_count", 0) > 0, f"files_only empty: {fo}"

    cnt = _run_console("sift_files_core.py", "def ", "--output-mode", "count",
                       "--path", str(FIXTURES), "--file-glob", "*.py")
    assert cnt.get("total_count", 0) > 0, f"count empty: {cnt}"
