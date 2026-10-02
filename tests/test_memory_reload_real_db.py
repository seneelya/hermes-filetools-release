"""Plan26 on THEIR real SessionDB: after our write the gateway memory is their loader's answer.

`Recon33`: our edit re-inserted the tail with new ids while gateway memory kept the old `_row_id`s;
their pre-turn check (`tui_gateway/prompt_turn.py::_adopt_out_of_band_turns`: rows newer than the
highest `_row_id` in memory are foreign) appended our own rows a second time. Fakes cannot show this -
the ids, the kept prefix and the marks come from their `replace_messages` and their loader.

  M1  edit in the middle: memory `_row_id`s = active ids in order, nothing newer than memory
  M2  every memory message is marked durable (`_db_persisted`), no table columns, no null tool_call_id
"""
import threading

import pytest

from hermes_filetools.context_ctl import _apply

hermes_state = pytest.importorskip("hermes_state")

SID = "plan26-real"


def _call(call_id, name="read_file"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}


@pytest.fixture
def live(tmp_path):
    db = hermes_state.SessionDB(db_path=tmp_path / "state.db")
    db.create_session(SID, "cli")
    db.append_message(SID, "user", "прочитай оба файла")
    db.append_message(SID, "assistant", "Начинаю с чтения.", tool_calls=[_call("c1"), _call("c2")])
    db.append_message(SID, "tool", "файл 1", tool_call_id="c1", tool_name="read_file")
    db.append_message(SID, "tool", "файл 2", tool_call_id="c2", tool_name="read_file")
    db.append_message(SID, "assistant", "Теперь патч.", tool_calls=[_call("c3", "patch")])
    db.append_message(SID, "tool", "ok", tool_call_id="c3", tool_name="patch")
    db.append_message(SID, "assistant", "Готово.")

    class Agent:
        session_id = SID
        _session_db = db
    # Memory as their desktop resume makes it - the starting point of a real session.
    history = db.get_messages_as_conversation(SID, repair_alternation=True, include_row_ids=True)
    session = {"history": history, "history_lock": threading.RLock(), "history_version": 0}
    yield db, Agent(), session
    db.close()


def _edit_second_message(db):
    """What `run_edit` hands the commit: active rows, the edited one without its id."""
    rows = [dict(r) for r in db.get_messages(SID)]
    old = rows[1]
    new = {k: v for k, v in old.items() if k not in ("id", "api_content", "_row_id")}
    new["content"] = "Начинаю с чтения"            # the owner's edit: trailing dot removed
    rows[1] = new
    return rows, old


def test_m1_memory_row_ids_follow_the_database_after_an_edit_in_the_middle(live):
    db, agent, session = live
    stale_seen = max(m["_row_id"] for m in session["history"])
    rows, old = _edit_second_message(db)
    ok, why = _apply.commit_replacement(session, agent, rows, removed=[old])
    assert ok and why == "применено", why
    # The bug this guards: memory left with the OLD ids sees our re-inserted tail as foreign.
    assert len(db.get_messages(SID, after_id=stale_seen)) == 6, "their replace re-inserts the tail"

    active = [r["id"] for r in db.get_messages(SID)]
    memory = session["history"]
    assert [m.get("_row_id") for m in memory] == active
    assert memory[1]["content"] == "Начинаю с чтения"
    # Their pre-turn check asks the store for rows after the highest id in memory.
    assert db.get_messages(SID, after_id=max(m["_row_id"] for m in memory)) == [], (
        "rows newer than memory would be adopted as foreign - the tail doubles")
    assert session["history_version"] == 1


def test_m2_memory_is_their_shape_marked_durable(live):
    db, agent, session = live
    rows, old = _edit_second_message(db)
    ok, why = _apply.commit_replacement(session, agent, rows, removed=[old])
    assert ok, why
    for msg in session["history"]:
        assert msg.get("_db_persisted") is True, msg
        for column in ("id", "session_id", "active", "compacted", "token_count"):
            assert column not in msg, (column, msg)
        if msg["role"] != "tool":
            assert "tool_call_id" not in msg, msg
    tool_ids = [m["tool_call_id"] for m in session["history"] if m["role"] == "tool"]
    assert tool_ids == ["c1", "c2", "c3"]
