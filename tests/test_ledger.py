"""Tests for the read ledger (`file_ops/_ledger.py`) — Plan02-Task01.

Two things are being defended here, and only one of them is the ledger:

  L1-L5  the store itself — round-trip, tolerance of a torn line, an absent
         session, filename safety, non-ascii and EOL stability.
  L6-L8  the CALLER — that a read_file whose ledger cannot be written returns
         the exact same bytes as one whose ledger can. The ledger is
         observability; `read_file` is the product, and a full disk must not
         turn a working read into an ERROR string.

Home is already hermetic: conftest points HERMES_HOME at an empty temp dir
before anything imports core, so `ledger_dir()` lands under it. Tests that want
their own home use `set_hermes_home_override` (context-local, same mechanism
the integration tests use) rather than mutating the env.
"""
from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest

from hermes_filetools.file_ops import _ledger
from hermes_filetools.file_ops import view_file_new_core

TESTS_DIR = Path(__file__).resolve().parent
EDGE_PY = TESTS_DIR / "Edge" / "Edge.py"


@pytest.fixture
def home(tmp_path):
    """A private hermes home for one test, restored afterwards."""
    import hermes_constants
    token = hermes_constants.set_hermes_home_override(tmp_path)
    try:
        yield tmp_path
    finally:
        hermes_constants.reset_hermes_home_override(token)


# --------------------------------------------------------------------------
# L1-L5 — the store
# --------------------------------------------------------------------------

def test_l1_round_trip(home):
    _ledger.note_read(text="hello", path="/a/b.py", session="sess-1",
                      task_id="t1", from_line=1, to_line=5, lines=5)
    entries = _ledger.read_ledger("sess-1")

    assert len(entries) == 1
    e = entries[0]
    assert e["session"] == "sess-1"
    assert e["task_id"] == "t1"
    assert e["path"] == "/a/b.py"
    assert (e["from"], e["to"], e["lines"]) == (1, 5, 5)
    # chars/sha8 describe the RESPONSE, which is what compaction rewrites -
    # that is the whole point of storing them.
    assert e["chars"] == len("hello")
    assert e["sha8"] == _ledger.sha8("hello")
    assert isinstance(e["ts"], float)


def test_l1b_appends_in_order(home):
    for i in range(3):
        _ledger.note_read(text="x" * i, path=f"/f{i}.py", session="s", lines=i)
    entries = _ledger.read_ledger("s")
    assert [e["path"] for e in entries] == ["/f0.py", "/f1.py", "/f2.py"]


def test_l2_torn_line_is_skipped_not_fatal(home):
    """A killed agent leaves a truncated last line. Losing the whole history
    over it would defeat the point of keeping one."""
    _ledger.note_read(text="one", path="/1.py", session="s")
    target = _ledger.ledger_path("s")
    with io.open(target, "a", encoding="utf-8", newline="") as fh:
        fh.write('{"ts": 1.0, "path": "/broken.py"\n')      # truncated mid-object
        fh.write("\n")                                       # blank line
        fh.write("not json at all\n")
    _ledger.note_read(text="two", path="/2.py", session="s")

    entries = _ledger.read_ledger("s")
    assert [e["path"] for e in entries] == ["/1.py", "/2.py"]


def test_l3_absent_session_is_empty_not_error(home):
    assert _ledger.read_ledger("never-existed") == []
    assert _ledger.read_ledger(None) == []
    assert _ledger.ledger_sessions() == []


def test_l4_session_id_cannot_escape_the_ledger_dir(home):
    """A session id becomes a file name. One "../" must not put the file
    outside hermes home."""
    for evil in ("../../evil", "..", "a/b\\c", "C:sess"):
        _ledger.note_read(text="x", path="/a.py", session=evil)
        target = _ledger.ledger_path(evil)
        # The invariant is containment, not the absence of any particular
        # character: the file lands directly in the ledger dir and nowhere else.
        assert target.parent == _ledger.ledger_dir()
        assert target.resolve().parent == _ledger.ledger_dir().resolve()
        assert target.exists(), evil
        assert _ledger.read_ledger(evil)[0]["path"] == "/a.py"


def test_l4b_empty_session_parks_under_one_visible_name(home):
    _ledger.note_read(text="x", path="/a.py", session="", task_id="")
    assert _ledger.ledger_sessions() == [_ledger.UNKNOWN_SESSION]


def test_l4c_task_id_rescues_a_read_with_no_session(home):
    """Live-data gap (2026-08-27): a read issued through the execute_code
    sandbox bridge carries neither the session_id kwarg nor the approval
    contextvars — only task_id. Four reads landed in _unknown.jsonl before
    this fallback existed."""
    _ledger.note_read(text="x", path="/a.py", session="", task_id="sess-from-task")

    assert _ledger.ledger_sessions() == ["sess-from-task"]
    entry = _ledger.read_ledger("sess-from-task")[0]
    assert entry["session"] == "sess-from-task"
    # Marked as INFERRED: /reads must not treat it as authoritative.
    assert entry["session_src"] == "task"


def test_l4d_explicit_session_wins_over_task_id(home):
    _ledger.note_read(text="x", path="/a.py", session="real", task_id="other")
    entry = _ledger.read_ledger("real")[0]
    assert entry["session"] == "real" and entry["session_src"] == "arg"


def test_l5_utf8_and_eol_survive_the_round_trip(home):
    """Written on Windows, read by the same code: the file must come back
    byte-identical. This project has already been bitten by CRLF."""
    text = "первая строка\nвторая\ttab\n"
    _ledger.note_read(text=text, path="C:/путь/файл.py", session="s", lines=2)

    raw = _ledger.ledger_path("s").read_bytes()
    assert b"\r\n" not in raw          # we asked for "\n" and must get "\n"
    assert raw.endswith(b"\n")

    e = _ledger.read_ledger("s")[0]
    assert e["path"] == "C:/путь/файл.py"
    assert e["chars"] == len(text)
    assert e["sha8"] == _ledger.sha8(text)


def test_l5b_sessions_are_listed_and_isolated(home):
    _ledger.note_read(text="a", path="/a.py", session="alpha")
    _ledger.note_read(text="b", path="/b.py", session="beta")

    assert _ledger.ledger_sessions() == ["alpha", "beta"]
    assert [e["path"] for e in _ledger.read_ledger("alpha")] == ["/a.py"]
    assert [e["path"] for e in _ledger.read_ledger("beta")] == ["/b.py"]


def test_l5c_missing_correlation_ids_are_absent_not_null(home):
    """Task04 keys `state: unknown` off a MISSING tool_call_id. A null would
    read as "we know it has none"."""
    _ledger.note_read(text="x", path="/a.py", session="s")
    e = _ledger.read_ledger("s")[0]
    assert "tool_call_id" not in e
    assert "turn" not in e


# --------------------------------------------------------------------------
# L6-L8 — the caller: the ledger may never change the answer
# --------------------------------------------------------------------------

def _read(**kw) -> str:
    return view_file_new_core.view_file_tool_new(
        path=str(EDGE_PY), line=1, count=5, mtime_enabled=False, **kw
    )


def test_l6_read_file_is_recorded(home):
    out = _read(session_id="live-session")
    assert not out.startswith("ERROR")

    entries = _ledger.read_ledger("live-session")
    assert len(entries) == 1
    e = entries[0]
    assert e["path"].endswith("Edge.py")
    assert e["from"] == 1 and e["to"] == 5 and e["lines"] == 5
    # The digest must cover what the model actually received, byte for byte -
    # Task04 compares it against the stored `content`.
    assert e["sha8"] == _ledger.sha8(out)
    assert e["chars"] == len(out)


def test_l7_unwritable_ledger_does_not_change_the_answer(home, monkeypatch):
    """The real failure, not a mocked one: mkdir fails because the ledger's
    own path is occupied by a FILE. Output must be identical to the healthy
    run."""
    import shutil
    good = _read(session_id="s1")

    shutil.rmtree(home / "filetools")
    (home / "filetools").write_text("I am a file, not a directory", encoding="utf-8")
    bad = _read(session_id="s2")

    assert bad == good
    assert not bad.startswith("ERROR")


def test_l7b_broken_ledger_module_does_not_change_the_answer(home, monkeypatch):
    """Second guard, one layer up: even a ledger that raises on ATTRIBUTE
    access (a rename upstream, a half-loaded module) must not surface."""
    good = _read(session_id="s1")

    def boom(*a, **kw):
        raise RuntimeError("ledger exploded")

    monkeypatch.setattr(_ledger, "note_read", boom)
    assert _read(session_id="s2") == good


def test_l8_metadata_only_read_is_still_an_entry(home):
    """count=0 showed no content. "It only ever looked at the size" is an
    answer the panel needs, not a gap."""
    out = view_file_new_core.view_file_tool_new(
        path=str(EDGE_PY), count=0, mtime_enabled=False, session_id="s"
    )
    assert not out.startswith("ERROR")

    e = _ledger.read_ledger("s")[0]
    assert (e["from"], e["to"], e["lines"]) == (0, 0, 0)
    assert e["chars"] == len(out)


def test_l8b_failed_read_is_not_recorded(home):
    out = view_file_new_core.view_file_tool_new(
        path=str(TESTS_DIR / "does-not-exist-xyz.py"), session_id="s"
    )
    assert out.startswith("ERROR")
    assert _ledger.read_ledger("s") == []


# --------------------------------------------------------------------------
# L9 — writes and patches, added 2026-08-27
#
# Writes are the stronger case for a ledger than reads: in-place compaction
# does not shorten a write_file result, it REPLACES it with
# "[write_file] wrote to <path> (N lines)" (67 such rows in the live db). For
# a read the row is at worst truncated; for a write the original text is gone.
# --------------------------------------------------------------------------

def test_l9_write_and_patch_are_recorded(home, tmp_path):
    from hermes_filetools.file_ops import create_file_new_core, modify_file_new_core

    target = tmp_path / "x.py"
    wrote = create_file_new_core.create_file_tool_new(
        path=str(target), content="a=1\nb=2\nc=3\n", base_dir=str(tmp_path),
        session_id="s", mtime_enabled=False)
    assert not wrote.startswith("ERROR")

    patched = modify_file_new_core.modify_file_tool_new(
        path=str(target), line=1, old_string="a=1", new_string="a=9",
        base_dir=str(tmp_path), session_id="s")
    assert not patched.lstrip().startswith("ERROR")

    entries = _ledger.read_ledger("s")
    assert [e["tool"] for e in entries] == ["write_file", "patch"]

    # sha8/chars must cover the FINAL text, envelope included — that is the
    # string that lands in the row and that compaction later replaces.
    assert entries[0]["sha8"] == _ledger.sha8(wrote)
    assert entries[1]["sha8"] == _ledger.sha8(patched)
    assert entries[1]["counts"]["replacements"] == 1


def test_l9b_failed_write_is_not_recorded(home, tmp_path):
    from hermes_filetools.file_ops import modify_file_new_core

    target = tmp_path / "y.py"
    target.write_text("a=1\n", encoding="utf-8")
    out = modify_file_new_core.modify_file_tool_new(
        path=str(target), line=1, old_string="NOT THERE", new_string="x",
        base_dir=str(tmp_path), session_id="s")
    assert "ERROR" in out or "not found" in out.lower()
    assert _ledger.read_ledger("s") == []


def test_l9c_counts_are_scalars_only(home):
    """The caller's facts dict sits next to `diff`, whose size is unbounded.
    An append-only log must not quietly become a second copy of the files."""
    _ledger.note_op(tool="patch", text="ok", path="/a.py", session="s",
                    counts={"added": 2, "no_change": False, "ok": 1.5,
                            "diff": "-" * 10000, "files": ["/a.py"],
                            "nested": {"a": 1}})
    counts = _ledger.read_ledger("s")[0]["counts"]
    assert counts == {"added": 2, "no_change": False, "ok": 1.5}
    assert "diff" not in counts and "files" not in counts


def test_l9d_old_entries_without_tool_read_as_reads(home):
    """Entries written before the `tool` field existed must not become
    unclassified: note_read still defaults it."""
    _ledger.note_read(text="x", path="/a.py", session="s")
    assert _ledger.read_ledger("s")[0]["tool"] == "read_file"


# ── Plan11-Task02: the facts the panel cannot derive ────────────────────────
#
# L10-L14 defend the four new keys. The theme of the group: every one of them
# is a fact the CLIENT has no way to compute, which is why it must be recorded
# at call time or lost. `total` in particular is NOT a history of lengths —
# nobody reads the old ones; it is the bar's denominator (newest entry per
# file) and a desync detector against replayed deltas.


def test_l10_read_records_the_file_length_not_the_slice(home):
    """`lines` and `total` are different numbers, and conflating them was the
    bug: a read of 12-20 says nothing about how long the file is, so without
    `total` the bar had no denominator at all."""
    out = view_file_new_core.view_file_tool_new(
        path=str(EDGE_PY), flags="--line 2 --count 3", session_id="s")
    assert not out.startswith("ERROR")

    entry = _ledger.read_ledger("s")[0]
    total_lines = len(EDGE_PY.read_text(encoding="utf-8").splitlines())
    assert entry["lines"] == 3, "slice size"
    assert entry["total"] == total_lines, "file size"
    assert entry["lines"] != entry["total"], (
        "a fixture whose file is exactly 3 lines would make this test pass "
        "for the wrong reason")


def test_l11_patch_records_where_it_landed_and_the_new_length(home, tmp_path):
    """Without a position a patch cannot be placed on the bar at all, and
    without the resulting length the fold cannot shift what lies below it."""
    from hermes_filetools.file_ops import modify_file_new_core

    target = tmp_path / "x.py"
    target.write_text("a=1\nb=2\nc=3\n", encoding="utf-8")
    out = modify_file_new_core.modify_file_tool_new(
        path=str(target), line=2, old_string="b=2", new_string="b=9\nb2=9",
        base_dir=str(tmp_path), session_id="s")
    assert not out.lstrip().startswith("ERROR")

    entry = _ledger.read_ledger("s")[0]
    assert entry["from"] == 2 and entry["to"] == 3, "the changed range"
    assert entry["total"] == 4, "one line became two"
    # added != removed is exactly what shifts everything below — the fold reads
    # these two, never a pre-computed delta, because "-1 +100" and "-100 +199"
    # are different events with the same delta.
    assert entry["counts"]["added"] == 2
    assert entry["counts"]["removed"] == 1


def test_l12_dry_run_records_the_place_but_causes_no_shift(home, tmp_path):
    """The model looked HERE — worth showing. But a dry run writes nothing, so
    it must not move a single coordinate: no `added`/`removed`, no shift."""
    from hermes_filetools.file_ops import modify_file_new_core

    target = tmp_path / "x.py"
    target.write_text("a=1\nb=2\nc=3\n", encoding="utf-8")
    out = modify_file_new_core.modify_file_tool_new(
        path=str(target), line=2, old_string="b=2", new_string="b=9",
        flags="--dry-run", base_dir=str(tmp_path), session_id="s")
    assert not out.lstrip().startswith("ERROR")

    entry = _ledger.read_ledger("s")[0]
    assert entry["from"] == 2 and entry["to"] == 2
    assert entry["total"] == 3, "unchanged: nothing was written"
    counts = entry.get("counts", {})
    assert counts.get("dry_run") is True
    assert not counts.get("added") and not counts.get("removed")


def test_l13_insert_records_the_inserted_block_in_new_coordinates(home, tmp_path):
    """The entry that makes the fold possible: an insert shifts every line
    below it, and without knowing WHERE, earlier reads would be drawn at stale
    addresses. The range is the inserted block's own, in the NEW numbering."""
    from hermes_filetools.file_ops import create_file_new_core

    target = tmp_path / "x.py"
    target.write_text("a=1\nb=2\nc=3\n", encoding="utf-8")
    out = create_file_new_core.create_file_tool_new(
        path=str(target), content="new1\nnew2\n", insert_line=2,
        base_dir=str(tmp_path), session_id="s", mtime_enabled=False)
    assert not out.startswith("ERROR")

    entry = _ledger.read_ledger("s")[0]
    assert entry["from"] == 2 and entry["to"] == 3, "the two inserted lines"
    assert entry["total"] == 5, "3 + 2"


def test_l14_nested_flag_is_absent_unless_inside_the_sandbox(home):
    """`nested` cannot be recovered afterwards: every nested call inherits the
    OUTER execute_code's tool_call_id, which is why such reads read as
    `unknown`. So it is set while the script runs, or never.

    Absent rather than `false`, like every other optional key here — a `false`
    on every ordinary read would be noise in a log read by grep as often as by
    code."""
    _ledger.note_read(text="plain", path="/a.py", session="s")

    token = _ledger.enter_sandbox()
    try:
        _ledger.note_read(text="from a script", path="/b.py", session="s")
    finally:
        _ledger.exit_sandbox(token)

    _ledger.note_read(text="after", path="/c.py", session="s")

    plain, inside, after = _ledger.read_ledger("s")
    assert "nested" not in plain
    assert inside["nested"] is True
    assert "nested" not in after, "the flag must not leak past exit_sandbox"


def test_l14b_sandbox_flag_is_reset_even_if_the_body_raises(home):
    """A leaked flag would mislabel every later operation as nested, so the
    reset lives in a `finally` — checked here rather than trusted."""
    token = _ledger.enter_sandbox()
    try:
        raise RuntimeError("their handler blew up")
    except RuntimeError:
        pass
    finally:
        _ledger.exit_sandbox(token)

    _ledger.note_read(text="after the blow-up", path="/a.py", session="s")
    assert "nested" not in _ledger.read_ledger("s")[0]


# ── Plan11-Task03: the command sidecar ──────────────────────────────────────
#
# The theme: the ledger must hold what was CALLED, not only what came back —
# because stage B will stub `tool_call.arguments` out of the model's context,
# after which this is the only surviving copy. Hence L15-L20 care as much about
# the ABSENCE cases (no pointer, no file, swept file) as about the happy path:
# every consumer's contract is "no file, no command block at all".


@pytest.fixture
def clean_ledger_config():
    """Defaults restored around a test that changes thresholds."""
    _ledger.reset_config()
    try:
        yield
    finally:
        _ledger.reset_config()


def test_l15_short_command_stays_inline(home, clean_ledger_config):
    """Below the sidecar threshold the head IS the whole command, and a file
    would be pure overhead."""
    _ledger.note_op(tool="terminal", text="ok", session="s", op="0001",
                    command="ls -la")
    entry = _ledger.read_ledger("s")[0]
    assert entry["cmd_head"] == "ls -la"
    assert "cmd_sidecar" not in entry
    assert not _ledger.sidecar_dir("s").exists()


def test_l16_long_command_goes_to_a_sidecar_byte_for_byte(home, clean_ledger_config):
    """The head is truncated for the entry; the FILE must hold the original
    exactly — it is the copy the model will no longer have."""
    body = "#!/bin/sh\n" + "echo line\n" * 500
    _ledger.note_op(tool="terminal", text="ok", session="s", op="00ab",
                    command=body)

    entry = _ledger.read_ledger("s")[0]
    assert len(entry["cmd_head"]) == 200
    assert entry["cmd_head"] == body[:200]
    assert entry["cmd_sidecar"] == "00ab.txt"
    assert _ledger.read_sidecar("s", "00ab") == body


def test_l17_no_op_means_no_sidecar_but_still_a_head(home, clean_ledger_config):
    """The file name IS the operation address. With no address there is nothing
    to name the file after, and an invented name is worse than an absence —
    the same rule `tool_call_id` follows."""
    _ledger.note_op(tool="terminal", text="ok", session="s", op="",
                    command="x" * 5000)
    entry = _ledger.read_ledger("s")[0]
    assert entry["cmd_head"] == "x" * 200
    assert "cmd_sidecar" not in entry


def test_l18_absent_sidecar_reads_as_none_not_an_error(home, clean_ledger_config):
    """"No file" is a legal, expected state, not a failure: consumers must be
    able to ask without guarding."""
    assert _ledger.read_sidecar("s", "dead") is None


def test_l19_sweep_removes_by_age_oldest_first(home, clean_ledger_config):
    import os

    _ledger.note_op(tool="terminal", text="ok", session="s", op="0001",
                    command="a" * 1000)
    _ledger.note_op(tool="terminal", text="ok", session="s", op="0002",
                    command="b" * 1000)

    old = _ledger.sidecar_path("s", "0001")
    ancient = time.time() - 99 * 86400
    os.utime(old, (ancient, ancient))

    assert _ledger.sweep_sidecars() == 1
    assert _ledger.read_sidecar("s", "0001") is None
    assert _ledger.read_sidecar("s", "0002") == "b" * 1000


def test_l19b_sweep_respects_the_size_budget(home, clean_ledger_config):
    """Age is not the only bound: a burst inside the retention window must not
    be able to fill the disk."""
    _ledger.configure(sidecar_max_mb=0)  # everything is over budget
    _ledger.note_op(tool="terminal", text="ok", session="s", op="0001",
                    command="a" * 1000)
    assert _ledger.sweep_sidecars() == 1
    assert _ledger.read_sidecar("s", "0001") is None


def test_l19c_sweep_on_an_empty_home_is_not_an_error(home, clean_ledger_config):
    """A fresh home has no `cmd/` directory at all, and that is the normal
    state — the sweep runs at plugin registration, before anything was written."""
    assert _ledger.sweep_sidecars() == 0


def test_l20_a_failed_sidecar_write_does_not_lose_the_entry(home, clean_ledger_config,
                                                            monkeypatch):
    """Observability degrades in layers: losing the full body must not cost us
    the entry, and neither may ever cost the tool its answer."""
    monkeypatch.setattr(_ledger, "_write_sidecar", lambda *a, **k: None)
    _ledger.note_op(tool="terminal", text="ok", session="s", op="0001",
                    command="z" * 5000)
    entry = _ledger.read_ledger("s")[0]
    assert entry["cmd_head"] == "z" * 200
    assert "cmd_sidecar" not in entry


def test_l21_patch_and_write_record_their_arguments(home, clean_ledger_config, tmp_path):
    """`old_string`/`new_string` and `content` are the unbounded part of these
    calls and the reason they need a sidecar at all."""
    from hermes_filetools.file_ops import create_file_new_core, modify_file_new_core

    target = tmp_path / "x.py"
    body = "\n".join(f"line{i}" for i in range(400)) + "\n"
    wrote = create_file_new_core.create_file_tool_new(
        path=str(target), content=body, base_dir=str(tmp_path),
        session_id="s", mtime_enabled=False)
    assert not wrote.startswith("ERROR")

    patched = modify_file_new_core.modify_file_tool_new(
        path=str(target), line=1, old_string="line0", new_string="line0-edited",
        base_dir=str(tmp_path), session_id="s")
    assert not patched.lstrip().startswith("ERROR")

    write_entry, patch_entry = _ledger.read_ledger("s")
    assert write_entry["cmd_head"].startswith("write_file ")
    # The content is long, so the full body must have gone to a file.
    assert body in _ledger.read_sidecar("s", write_entry["op"])
    # The patch call is short — head only, and the head must still say WHAT was
    # asked for, not just which tool ran.
    assert "line0-edited" in patch_entry["cmd_head"]


def test_l22_overwrite_records_what_it_destroyed(home, tmp_path):
    """`removed` on a full overwrite (2026-09-08).

    Without it an overwrite of a 4-line file with 4 lines reads as `+4`, and a
    consumer that folds `added - removed` shifts every earlier range past the
    end of the file — verified live before this was recorded.
    """
    from hermes_filetools.file_ops import create_file_new_core

    target = tmp_path / "b.txt"
    first = create_file_new_core.create_file_tool_new(
        path=str(target), content="B1\nB2\nB3\nB4\n", base_dir=str(tmp_path),
        session_id="s22", mtime_enabled=False)
    assert not first.startswith("ERROR"), first
    second = create_file_new_core.create_file_tool_new(
        path=str(target), content="C1\nC2\nC3\nC4\n", base_dir=str(tmp_path),
        session_id="s22", mtime_enabled=False)
    assert not second.startswith("ERROR"), second

    entries = _ledger.read_ledger("s22")
    assert len(entries) == 2, entries
    # Creation destroyed nothing: the file did not exist, and a `removed` we
    # never observed would be a claim rather than a fact.
    assert "removed" not in (entries[0].get("counts") or {})
    assert (entries[1].get("counts") or {}).get("removed") == 4, entries[1]


def test_observability_contextvars_are_reachable():
    """Наш ИМПОРТ трёх приватных contextvars жив — не их символ, а наша дверь к нему.

    Тест написан 2026-09-08 по факту тихой поломки, которую реестр швов не поймал.
    Апстрим перенёс переменные из `tools/approval.py` в `tools/approval_context.py` и
    не оставил реэкспорта; `ACHTUNG__foreign_seams.yaml` переезд ЗАПИСАЛ, чекер грепал
    ИХ файл и зеленел, а наш `_observability_context` продолжал читать старое место,
    ловил AttributeError и молча писал записи без `tool_call_id`. Итог: панель месяцами
    показывала бы `state: unknown`, а адресация `/ctx-drop` не работала бы вовсе.

    Поэтому проверяется именно ДОСТИЖИМОСТЬ атрибутов оттуда, откуда читаем мы. Значение
    вне живого dispatch законно пустое, так что утверждать про значение нельзя — только
    про то, что дверь есть.
    """
    import importlib

    from hermes_filetools.file_ops import _ledger as led

    found = None
    for name in ("tools.approval_context", "tools.approval"):
        try:
            mod = importlib.import_module(name)
        except Exception:
            continue
        if all(hasattr(mod, attr) for attr in
               ("_approval_session_id", "_approval_tool_call_id", "_approval_turn_id")):
            found = name
            break
    assert found is not None, (
        "ни `tools.approval_context`, ни `tools.approval` не отдают три contextvars — "
        "адресация операций мертва: ledger пишет записи без tool_call_id, панель "
        "показывает `unknown`, /ctx-drop не находит адреса. Искать новое место "
        "`_approval_tool_call_id` в чекауте Hermes и поправить `_observability_context`")

    # И наш собственный вызов не должен падать ни при каком расположении.
    session, call_id, turn = led._observability_context()
    assert isinstance(session, str) and isinstance(call_id, str) and isinstance(turn, str)


# --------------------------------------------------------------------------
# L9 — надгробия: запись о том, что операции больше НЕТ (Plan13)
#
# Журнал append-only, и после `erase tool` он единственный, кто помнит, что
# операция была. Но «запись есть, строки нет» — два разных факта с одним
# симптомом: наше стирание и потеря, которую мы объяснить не можем. Различить
# их можно только записью, поэтому она отдельная и с ключом `event`.
# --------------------------------------------------------------------------

def test_l9_a_tombstone_round_trips(home):
    _ledger.note_erased("s9", "0014")
    entries = _ledger.read_ledger("s9")
    assert len(entries) == 1
    tomb = entries[0]
    assert tomb["event"] == "erased"
    assert tomb["op"] == "0014"
    assert tomb["by"] == "erase tool"
    assert isinstance(tomb["ts"], float)


def test_l9a2_the_tombstone_names_the_session_the_way_operations_do(home):
    """Одна сессия под двумя именами в одном файле — и grep по журналам врёт.

    В поле идёт СЫРОЙ идентификатор (так пишет `note_op`), а безопасное имя — дело
    `ledger_path`, то есть только имени файла.
    """
    raw = "s9:вот такая"
    _ledger.note_read(text="a=1", path="/w/x.py", session=raw, op="0001")
    _ledger.note_erased(raw, "0001")
    entries = _ledger.read_ledger(raw)
    assert len(entries) == 2, "надгробие легло в ТОТ ЖЕ файл"
    assert {e["session"] for e in entries} == {raw}


def test_l9b_events_are_told_from_operations_by_one_function(home):
    """Читателей у леджера много, и решай каждый сам — однажды решил бы иначе."""
    _ledger.note_read(text="a=1", path="/w/x.py", session="s9", op="0001")
    _ledger.note_erased("s9", "0001")
    entries = _ledger.read_ledger("s9")
    assert [_ledger.is_event(e) for e in entries] == [False, True]
    assert list(_ledger.erased_ops(entries)) == ["0001"]


def test_l9c_the_last_tombstone_per_op_wins(home):
    _ledger.note_erased("s9", "0001", by="erase tool")
    _ledger.note_erased("s9", "0001", by="erase turn")
    tombs = _ledger.erased_ops(_ledger.read_ledger("s9"))
    assert tombs["0001"]["by"] == "erase turn"


def test_l9d_an_empty_address_writes_nothing(home):
    """Надгробие без адреса не про что: показать его было бы нечем, а строку оно занимает."""
    _ledger.note_erased("s9", "")
    _ledger.note_erased("s9", "   ")
    assert _ledger.read_ledger("s9") == []


def test_l9e_a_failing_write_never_raises(home, monkeypatch):
    """ПРАВИЛО: неудачная запись надгробия НЕ отменяет применения.

    Строки в контексте уже нет — правка необратима. Падение здесь превратило бы
    состоявшееся стирание в «не сработало», то есть соврало бы вызывающему об
    исходе того, что откатить нельзя.
    """
    def boom(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(_ledger.io, "open", boom)
    _ledger.note_erased("s9", "0014")  # молча


# --------------------------------------------------------------------------
# L10 — сообщения и состав вагона (Plan14-Task03)
#
# Проверяется по пунктам КОНТРАКТА (`__HQ/CONTRACT__ledger-records.md` §5 и
# поправка 1), а не по строчкам кода: род различается одной проверкой, текстов
# в записи нет, адрес не перевыдаётся, состав пишется только при изменении.
# --------------------------------------------------------------------------

def test_l10a_the_message_counter_is_separate_from_operations(home):
    """Общий счётчик дал бы дыры в нумерации операций, а дыра ТАМ уже значит
    другое — «операция стёрта»."""
    _ledger._counters.clear()   # кеш счётчиков переживает подмену home
    assert _ledger.next_op("s10") == "0001"
    assert _ledger.next_msg("s10") == "m0001"
    assert _ledger.next_op("s10") == "0002", "адрес операции не сдвинулся сообщением"
    assert _ledger.next_msg("s10") == "m0002"
    assert _ledger.msg_path("s10").read_text(encoding="utf-8") == "2"
    assert _ledger.op_path("s10").read_text(encoding="utf-8") == "2"


def test_l10a2_the_counter_survives_a_reload(home):
    """Тот же случай, что у операций: перезапуск не имеет права выдать адрес,
    который уже назван."""
    _ledger._counters.clear()   # кеш счётчиков переживает подмену home
    _ledger.next_msg("s10")
    _ledger.next_msg("s10")
    _ledger._counters.clear()          # что делает с этим процессом /filetools-reload
    assert _ledger.next_msg("s10") == "m0003"


def test_l10b_one_counter_for_all_roles(home):
    """Поправка 1: счётчик ОДИН, роль — поле записи, а не часть адреса."""
    _ledger._counters.clear()   # кеш счётчиков переживает подмену home
    for role in ("user", "assistant", "tool", "system"):
        _ledger.note_message(session="s10", msg=_ledger.next_msg("s10"), role=role,
                             msg_ts=1.0 + len(role), sha256="d" * 64)
    records = _ledger.message_records(_ledger.read_ledger("s10"))
    assert [r["msg"] for r in records] == ["m0001", "m0002", "m0003", "m0004"]
    assert [r["role"] for r in records] == ["user", "assistant", "tool", "system"]


def test_l10c_the_record_carries_no_text(home):
    """Контракт §1 и §7: журнал — индекс, а не вторая копия истории."""
    _ledger.note_message(session="s10", msg="m0001", role="user", msg_ts=1.0,
                         sha256="a" * 64)
    raw = _ledger.ledger_path("s10").read_text(encoding="utf-8")
    entry = json.loads(raw.strip())
    assert set(entry) == {"ts", "event", "session", "session_src", "msg", "role",
                          "msg_ts", "sha256"}, "необязательные ключи ОТСУТСТВУЮТ, а не пусты"
    assert entry["event"] == "message"


def test_l10d_optional_keys_are_absent_not_empty(home):
    _ledger.note_message(session="s10", msg="m0001", role="tool", msg_ts=1.0,
                         sha256="a" * 64, tool_call_id="call-7",
                         parent_session="parent-1")
    entry = _ledger.message_records(_ledger.read_ledger("s10"))[0]
    assert entry["tool_call_id"] == "call-7"
    assert entry["parent_session"] == "parent-1"


def test_l10e_events_are_told_apart_by_one_key(home):
    """Дискриминатор один на все роды — контракт §2."""
    _ledger.note_read(text="a=1", path="/w/x.py", session="s10", op="0001")
    _ledger.note_erased("s10", "0001")
    _ledger.note_message(session="s10", msg="m0001", role="user", msg_ts=1.0,
                         sha256="a" * 64)
    _ledger.note_layout(session="s10", opener="m0001", members=["m0001"])
    entries = _ledger.read_ledger("s10")
    assert [_ledger.is_event(e) for e in entries] == [False, True, True, True]
    assert list(_ledger.erased_ops(entries)) == ["0001"]
    assert [e["msg"] for e in _ledger.message_records(entries)] == ["m0001"]
    assert list(_ledger.layouts(entries)) == ["m0001"]


def test_l10f_the_last_layout_per_carriage_wins(home):
    """То же правило, что у надгробий: состав выражается последней записью."""
    _ledger.note_layout(session="s10", opener="m0001", members=["m0001"])
    _ledger.note_layout(session="s10", opener="m0001", members=["m0001", "m0002"])
    lay = _ledger.layouts(_ledger.read_ledger("s10"))
    assert lay["m0001"]["members"] == ["m0001", "m0002"]


def test_l10g_carriage_zero_lives_under_none(home):
    """У вагона 0 нет открывающей реплики человека, и это не ошибка."""
    _ledger.note_layout(session="s10", opener=None, members=["m0001"])
    assert list(_ledger.layouts(_ledger.read_ledger("s10"))) == [None]


def test_l10h_a_nameless_message_writes_nothing(home):
    """Запись без адреса не про что — как надгробие без адреса."""
    assert _ledger.note_message(session="s10", msg="", role="user", msg_ts=1.0,
                                sha256="a" * 64) is False
    assert _ledger.note_message(session="s10", msg="m0001", role="  ", msg_ts=1.0,
                                sha256="a" * 64) is False
    assert _ledger.read_ledger("s10") == []


def test_l10i_a_failing_write_never_raises(home, monkeypatch):
    """Свидетельство не отменяет работы — то же правило, что у надгробий."""
    def boom(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(_ledger.io, "open", boom)
    assert _ledger.note_message(session="s10", msg="m0001", role="user", msg_ts=1.0,
                                sha256="a" * 64) is False
    assert _ledger.note_layout(session="s10", opener=None, members=[]) is False


def test_l10j_two_instances_over_one_file_never_repeat_an_address(home):
    """ДВА ЭКЗЕМПЛЯРА ЭТОГО МОДУЛЯ ЖИВУТ В ОДНОМ ПРОЦЕССЕ, и это не гипотеза:
    движок импортирует его пакетом, дашборд грузит по пути (иначе не умеет).
    Кеши разные, файл один. Операциям это не вредило — номера выдаёт только
    движок; адреса сообщений раздают ОБА, и экземпляр с отставшим кешем выдал бы
    номер, который другой уже назвал.
    """
    import importlib.util
    import sys as _sys
    _ledger._counters.clear()   # кеш счётчиков переживает подмену home
    name = "test_second_ledger_instance"
    spec = importlib.util.spec_from_file_location(
        name, Path(_ledger.__file__))
    twin = importlib.util.module_from_spec(spec)
    _sys.modules[name] = twin
    try:
        spec.loader.exec_module(twin)
        handed = [_ledger.next_msg("s10"), twin.next_msg("s10"),
                  _ledger.next_msg("s10"), twin.next_msg("s10")]
    finally:
        _sys.modules.pop(name, None)
    assert handed == ["m0001", "m0002", "m0003", "m0004"]
    assert len(set(handed)) == 4
