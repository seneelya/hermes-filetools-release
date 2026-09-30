"""Tests for `GET /ops` (`dashboard/plugin_api.py`) — Plan02-Task03.

Most of this runs on hand-built rows, because the interesting failures are in
the JUDGEMENT (is this row ours? where do the arguments live? was it compacted?)
and not in SQLite. The last group runs against the real `state.db` when one is
present, since the task's whole point is reading somebody else's finished
session — and two of the rules below were only discovered by looking at real
rows.

Row shape is the real one (`SELECT *` from `messages`), verified 2026-08-27:
id, session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp,
active, compacted, ... — note there is NO `args` column, which is why
_args_index exists.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
API_FILE = PROJECT_ROOT / "dashboard" / "plugin_api.py"


@pytest.fixture(scope="module")
def api():
    name = "test_filetools_api_ops"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, API_FILE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    try:
        yield mod
    finally:
        sys.modules.pop(name, None)


# --- row builders ---------------------------------------------------------

READ_BODY = (
    "# File: /w/x.py\n"
    "# Size: 3 lines, 10 bytes, 10 chars, LF, max/avg line 4/3.0\n"
    "# FileTag: x\n"
    "# ○○○\n"
    "# ■BLOCK x : 1-3\n"
    "a=1\n"
    "# ■END x : 3\n"
)
WRITE_BODY = "Created new file (2 lines). Encoding: utf-8, LF. Modified: 2026-08-27 01:28."


def tool_row(rid, tool, content, call_id="c1", session="s", active=1, compacted=0):
    return {
        "id": rid, "session_id": session, "role": "tool", "content": content,
        "tool_call_id": call_id, "tool_calls": None, "tool_name": tool,
        "timestamp": 1000.0 + rid, "active": active, "compacted": compacted,
    }


def assistant_row(rid, call_id, name, args, session="s"):
    return {
        "id": rid, "session_id": session, "role": "assistant", "content": "",
        "tool_call_id": None, "tool_name": None, "active": 1, "compacted": 0,
        "timestamp": 1000.0 + rid,
        "tool_calls": json.dumps([
            {"id": call_id, "call_id": call_id, "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}
        ]),
    }


# --- O1: whose row is it --------------------------------------------------

def test_o1_ours_by_name_and_signature(api):
    assert api._is_ours(tool_row(1, "read_file", READ_BODY))


def test_o1b_unpatched_builtin_is_excluded(api):
    """The built-in returns JSON under the SAME tool name. Without the
    signature check the panel would present someone else's output as ours."""
    builtin = tool_row(1, "read_file", '{"content": "a=1", "lines": 3}')
    assert not api._is_ours(builtin)


def test_o1c_foreign_tool_is_excluded(api):
    assert not api._is_ours(tool_row(1, "terminal", READ_BODY))


def test_o1d_write_file_has_no_block_marks_and_must_still_count(api):
    """Regression on a rule that was wrong in the task: write_file's response
    has no file body, so it carries no ■/○○○ and the original
    "marks or nothing" rule dropped EVERY write we ever made."""
    assert api._is_ours(tool_row(1, "write_file", WRITE_BODY))


def test_o1e_compaction_stub_is_not_ours(api):
    """In-place compaction REPLACES a write_file result with its own one-liner
    (agent/context_compressor.py). That row is no longer our output."""
    stub = tool_row(1, "write_file", "[write_file] wrote to /w/x.py (40 lines)")
    assert not api._is_ours(stub)


# --- O2: arguments live on the assistant row ------------------------------

def test_o2_args_joined_from_assistant_tool_calls(api):
    rows = [
        assistant_row(1, "call-A", "read_file", {"path": "/w/x.py", "line": 5, "count": 20}),
        tool_row(2, "read_file", READ_BODY, call_id="call-A"),
    ]
    out = api.build_ops(rows)
    brick = out["ops"][0]
    assert brick["title"] == "read_file /w/x.py"
    assert "line 5" in brick["subtitle"] and "count 20" in brick["subtitle"]
    assert brick["meta"]["args_matched"] is True


def test_o2b_path_falls_back_to_our_own_header(api):
    """No tool_calls row (compaction dropped it, or the join failed): the path
    is recovered from OUR meta header. Reading our own format is allowed here;
    the body still is not."""
    out = api.build_ops([tool_row(2, "read_file", READ_BODY, call_id="orphan")])
    brick = out["ops"][0]
    assert brick["title"] == "read_file /w/x.py"
    assert brick["meta"]["args_matched"] is False


def test_o2c_broken_tool_calls_json_does_not_kill_the_response(api):
    bad = assistant_row(1, "call-A", "read_file", {"path": "/w/x.py"})
    bad["tool_calls"] = '[{"id": "call-A", "function": {"argum'
    out = api.build_ops([bad, tool_row(2, "read_file", READ_BODY, call_id="call-A")])
    assert len(out["ops"]) == 1
    assert out["ops"][0]["meta"]["args_matched"] is False


def test_o2d_title_never_comes_from_the_body(api):
    """A body that looks like metadata must not become the title."""
    liar = READ_BODY.replace("a=1", "File: /etc/passwd")
    out = api.build_ops([tool_row(2, "read_file", liar, call_id="none")])
    assert "/etc/passwd" not in out["ops"][0]["title"]


# --- O3: kind -------------------------------------------------------------

def test_o3_error_kind(api):
    out = api.build_ops([tool_row(1, "patch", "ERROR: could not find the anchor")])
    assert out["ops"] == [] or out["ops"][0]["kind"] == "error"


def test_o3b_no_change_comes_from_envelope_facts(api):
    """Seen live 2026-08-27: a patch with replacements: 1 was reported as no-change.
    The `Lines: N (unchanged)` sentinel means the line COUNT is unchanged, which a
    same-line replacement also satisfies — so the signal was simply wrong."""
    envelope = json.dumps({"files_modified": ["/w/a.txt"], "replacements": 1,
                           "added": 1, "removed": 1, "success": True})
    body = envelope + "\n" + "Patched: /w/a.txt\nLines: 1 (unchanged)\n"
    out = api.build_ops([tool_row(1, "patch", body)])
    brick = out["ops"][0]
    assert brick["kind"] == "ok", "a performed replacement is not a no-op"
    assert brick["meta"]["replacements"] == 1
    assert not brick["body"].startswith("{"), "the envelope must be stripped off the body"


def test_o3b2_real_no_change_is_reported(api):
    envelope = json.dumps({"files_modified": ["/w/a.txt"], "no_change": True,
                           "replacements": 0, "success": True})
    out = api.build_ops([tool_row(1, "patch", envelope + "\nLines: 1 (unchanged)\n")])
    assert out["ops"][0]["kind"] == "no-change"


def test_o3b3_builtin_json_is_still_not_ours(api):
    """The built-in tools answer with JSON too. Only OUR envelope keys count."""
    assert not api._is_ours(tool_row(1, "read_file", '{"content": "a=1", "lines": 3}'))
    assert api.split_envelope('{"content": "a=1"}\ntext')[0] is None


def test_o3c_sentinel_in_the_body_is_not_a_no_change(api):
    """The sentinel is only meaningful in the meta header. A file that happens
    to contain the phrase must not be reported as a no-op."""
    body = READ_BODY.replace("a=1", "Lines: 99 (unchanged)")
    out = api.build_ops([tool_row(1, "read_file", body)])
    assert out["ops"][0]["kind"] == "ok"


# --- O4: paging and compaction flag ---------------------------------------

def test_o4_limit_and_truncated(api):
    rows = [tool_row(i, "read_file", READ_BODY, call_id=f"c{i}") for i in range(1, 8)]
    out = api.build_ops(rows, limit=3)
    assert len(out["ops"]) == 3
    assert out["total"] == 7
    assert out["truncated"] is True

    whole = api.build_ops(rows, limit=100)
    assert whole["truncated"] is False and len(whole["ops"]) == 7


def test_o4b_chronological_order_preserved(api):
    rows = [tool_row(i, "read_file", READ_BODY, call_id=f"c{i}") for i in (1, 2, 3)]
    out = api.build_ops(rows)
    assert [b["meta"]["row_id"] for b in out["ops"]] == [1, 2, 3]


def test_o4c_compacted_row_is_flagged(api):
    """active=0 + compacted=1 means the text may already be a rewritten
    shortening. Unflagged, the panel would show a remnant as the full output."""
    rows = [
        tool_row(1, "read_file", READ_BODY, call_id="a", active=1, compacted=0),
        tool_row(2, "read_file", READ_BODY, call_id="b", active=0, compacted=1),
    ]
    out = api.build_ops(rows)
    assert out["ops"][0]["meta"]["compacted"] is False
    assert out["ops"][1]["meta"]["compacted"] is True


# --- O5: the route shell --------------------------------------------------

def test_o5_missing_session_is_an_error_field_not_an_exception(api):
    out = api.ops(session="")
    assert out["ops"] == [] and "error" in out


def test_o5b_unknown_session_is_empty_with_a_note(api):
    out = api.ops(session="definitely-not-a-session-xyz")
    assert out["ops"] == []
    assert "error" in out or "note" in out


# --- O6: the real database ------------------------------------------------

REAL_DB = os.environ.get("FILETOOLS_TEST_STATE_DB") or r"Y:\Hermess\body\state.db"


@pytest.mark.skipif(not Path(REAL_DB).exists(), reason="no real state.db available")
def test_o6_real_db_rows_pass_the_same_rules(api):
    """The task's point is reading a FINISHED session someone else produced.
    Hand-built rows cannot prove the rules match reality — two of them were
    wrong until real rows were looked at."""
    conn = sqlite3.connect(f"file:{Path(REAL_DB).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM messages WHERE tool_name IN "
        "('read_file','write_file','patch','search_files') LIMIT 20000"
    )]
    conn.close()
    assert rows, "real db has no rows under our tool names"

    ours = [r for r in rows if api._is_ours(r)]
    assert ours, "no row in the real db passed the ours-check — the signature rule is stale"

    # Built-in JSON results vastly outnumber ours in a long-lived db; if the
    # filter ever stops discriminating, this is where it shows.
    assert len(ours) < len(rows), "the filter accepted everything — signature check is dead"

    # No compaction stub may survive the filter.
    assert not [r for r in ours
                if (r["content"] or "").startswith("[write_file] wrote to")]

    # Every accepted row must produce a brick with a usable title.
    built = api.build_ops(ours, limit=50)
    assert built["ops"], "accepted rows produced no bricks"
    assert all(b["title"] for b in built["ops"])
    assert all(b["kind"] in ("ok", "error", "no-change") for b in built["ops"])


# --- O7: the operation address (Plan09) -----------------------------------

def test_o7_address_reaches_the_card_meta(api):
    """History half of a two-half feature: this reads the address out of the
    STORED row, the desktop half reads it out of the live event's body
    (`desktop/src/core.js::opFromHeader`). Both must find the same thing in
    the same text, or a fresh card carries an address while its history twin
    does not - the "new calls show up, older ones never load" shape of bug."""
    body = "#Operation ID: 0156xab\n" + READ_BODY
    ops = api.build_ops([tool_row(1, "read_file", body)])["ops"]
    assert ops[0]["meta"]["op"] == "0156xab"


def test_o7b_bare_address_from_a_wrapped_tool(api):
    # The `(raw: "exit_code"` tail is terminal's own ownership signal — without
    # it the row is not ours at all, and then there is no card to carry an
    # address in the first place.
    body = ('Operation ID: 0007\n'
            'Exit code: 0  (raw: "exit_code": 0)\n'
            '○○○\nfiles')
    ops = api.build_ops([tool_row(1, "terminal", body)])["ops"]
    assert ops[0]["meta"]["op"] == "0007"


def test_o7c_a_row_without_an_address_reports_none_not_a_guess(api):
    """Rows predating Plan09, and refusals, carry no address at all. The panel
    must show nothing there rather than invent a number - a card claiming an
    address the model never saw is worse than a card with none."""
    ops = api.build_ops([tool_row(1, "read_file", READ_BODY)])["ops"]
    assert ops[0]["meta"]["op"] is None


def test_o7d_an_address_inside_the_body_is_not_the_header(api):
    """the label below the header is file CONTENT. Same narrow scan as the path
    fallback, for the same reason."""
    body = READ_BODY + "\nOperation ID: 0999\n"
    ops = api.build_ops([tool_row(1, "read_file", body)])["ops"]
    assert ops[0]["meta"]["op"] is None


# --- O8: what an operation WEIGHS ----------------------------------------
# Owner, 2026-09-09: the number next to a checkbox is the operation's own
# weight in tokens, NOT what removing it would save. Choosing without a price
# is meaningless - a 300-char read and a 36 000-char read look identical in
# the stack.


def test_o8_a_brick_carries_its_token_weight(api):
    ops = api.build_ops([tool_row(1, "read_file", READ_BODY)])["ops"]
    tokens = ops[0]["meta"]["tokens"]
    assert isinstance(tokens, int) and tokens > 0
    # Sanity, not calibration: their estimator is ~bytes/4 and the body is
    # ~180 ASCII bytes, so anything outside this range means we measured the
    # wrong string.
    assert 20 <= tokens <= 120, tokens


def test_o8b_both_halves_are_counted_not_just_the_answer(api):
    """A write answers in ONE line and carries the whole file in its arguments.

    Counting only the answer would show the heaviest operations in the session
    as the cheapest - the exact defect the owner hit on 2026-09-09 when a write
    refused to be dropped because its answer had nothing in it.
    """
    call = "c-big"
    big = "x" * 8000
    rows = [assistant_row(1, call, "write_file", {"path": "/w/x.py", "content": big}),
            tool_row(2, "write_file", WRITE_BODY, call_id=call)]
    heavy = api.build_ops(rows)["ops"][0]["meta"]["tokens"]

    light = api.build_ops([tool_row(2, "write_file", WRITE_BODY, call_id="c-none")]
                          )["ops"][0]["meta"]["tokens"]
    assert heavy > light + 1500, (heavy, light)


def test_o8c_the_estimate_falls_back_when_upstream_is_missing(api, monkeypatch):
    """No upstream (bare python) must still produce a number, not a zero.

    Zero would read as "this operation is free" - the one answer that would
    make the human choose wrong.
    """
    import builtins
    real_import = builtins.__import__

    def no_metadata(name, *a, **kw):
        if name == "agent.model_metadata":
            raise ImportError("no upstream")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_metadata)
    # Cyrillic on purpose: their documented rule is UTF-8 bytes/4, and the
    # fallback has to agree with it rather than count characters.
    assert api._tokens_of("привет" * 100) > 250


def test_o8d_an_empty_operation_weighs_zero_without_raising(api):
    assert api._tokens_of("", None, 0) == 0


# --- O9: состояние строки в кирпичике ------------------------------------
# `/reads` отвечает только про read-тулы, а панели состояние нужно у КАЖДОЙ
# карточки: выброшенной операции нельзя предлагать `drop content` второй раз,
# а `erase tool` по ней — наоборот, самый полезный случай (заголовок остался и
# тратит токены). Читается из заголовка строки, не выводится из размеров.


def test_o9_a_dropped_row_declares_its_state(api):
    body = ("#Operation ID: 0007\n"
            "#State: removed_from_context\n"
            "#File: /w/x.py\n"
            "#○○○\n")
    ops = api.build_ops([tool_row(1, "read_file", body)])["ops"]
    assert ops[0]["meta"]["state"] == "removed_from_context"


def test_o9b_a_live_row_declares_nothing_rather_than_a_guess(api):
    ops = api.build_ops([tool_row(1, "read_file", READ_BODY)])["ops"]
    assert ops[0]["meta"]["state"] is None, "состояние не выводим из размеров"


def test_o9c_a_write_carries_the_state_too(api):
    """Именно на записи прежнее правило и разъезжалось: /reads её не знает вовсе."""
    body = "#Operation ID: 0008\n#State: removed_from_context\n"
    ops = api.build_ops([tool_row(1, "write_file", body)])["ops"]
    assert ops[0]["meta"]["state"] == "removed_from_context"


# --- O10: НАДГРОБИЯ (Plan13, поправка 8) ----------------------------------
# У стёртой операции строки в базе нет ВОВСЕ, поэтому из `rows` карточка не
# соберётся никак. Единственный, кто помнит, что операция была, — леджер: запись
# об операции осталась (журнал append-only), а рядом легло надгробие.


def op_entry(op="0014", tool="read_file", path="/w/x.py", ts=900.0):
    return {"ts": ts, "op": op, "tool": tool, "path": path, "session": "s"}


def tomb(op="0014", ts=2000.0, by="erase tool"):
    return {"ts": ts, "event": "erased", "op": op, "session": "s", "by": by}


def test_o10_a_tombstone_becomes_a_card_of_its_own(api):
    ops = api.build_ops([], entries=[op_entry(), tomb()])["ops"]
    assert len(ops) == 1
    brick = ops[0]
    assert brick["meta"]["op"] == "0014"
    assert brick["meta"]["state"] == "erased"
    assert brick["meta"]["erased_by"] == "erase tool"
    assert brick["title"] == "read_file /w/x.py", "имя и путь из записи об операции"
    assert brick["body"] == "", "показывать нечего, и придумать нечего честно"
    assert brick["tool_id"] == "" and brick["meta"]["row_id"] is None


def test_o10b_the_tombstone_stands_where_the_operation_stood(api):
    """`ts` — МЕСТО в разговоре, не факт о времени. Факт уходит в `meta.erased_at`."""
    ops = api.build_ops([], entries=[op_entry(ts=900.0), tomb(ts=5000.0)])["ops"]
    assert ops[0]["ts"] == 900.0, "соседей нет — только тогда время из журнала"
    assert ops[0]["meta"]["erased_at"] == 5000.0


def op_row(rid, op, ts, tool="read_file"):
    """Строка с адресом в шапке и СВОИМ временем базы."""
    row = tool_row(rid, tool, f"#Operation ID: {op}\n" + READ_BODY, call_id=f"c{rid}")
    row["timestamp"] = ts
    return row


def test_o10b2_inside_a_parallel_batch_the_place_beats_the_clock(api):
    """Замерено живьём 2026-09-09: журнал внутри батча пишет время ГРУБЕЕ базы.

    Девять операций одного хода легли в журнал на `...535.2`, а строки их соседей — на
    `...535.28`–`...535.30`. Часы сходятся, но доли секунды у двух источников свои, поэтому
    отсортированное по журналу надгробие уезжает на пару карточек ВВЕРХ. Владелец увидела
    это первым же прогоном. Якорь — строка предыдущей операции, а не записанное время.
    """
    entries = [op_entry(op="0011", ts=535.2), op_entry(op="0012", ts=535.2),
               op_entry(op="0013", ts=535.2), tomb(op="0012", ts=9000.0)]
    rows = [op_row(1, "0011", 535.28), op_row(2, "0013", 535.30)]
    ops = api.build_ops(rows, entries=entries)["ops"]
    assert [o["meta"]["op"] for o in ops] == ["0011", "0012", "0013"], (
        "надгробие обязано встать МЕЖДУ соседями, а не перед батчем")


def test_o10b3_the_first_operation_erased_stands_before_the_next_one(api):
    """Предыдущей нет вовсе — встаём ПЕРЕД следующей, а не в начало всей истории."""
    entries = [op_entry(op="0001", ts=200.0), op_entry(op="0002", ts=200.0),
               tomb(op="0001", ts=9000.0)]
    ops = api.build_ops([op_row(1, "0002", 200.1)], entries=entries)["ops"]
    assert [o["meta"]["op"] for o in ops] == ["0001", "0002"]
    assert ops[0]["ts"] < 200.1


def test_o10b3b_a_neighbour_from_another_turn_is_not_a_neighbour(api):
    """Живая находка 2026-09-09, второй случай: якорь чинит ДОЛИ СЕКУНДЫ, не переносит по ходам.

    Операцию `0014` стёрли; её сосед по журналу лежал ЧАСОМ раньше, и надгробие уехало в
    середину старого хода. Между ходами записанное время точное — ему и верим, иначе мы
    меняем маленькую неправду (пара карточек) на большую (не тот ход).
    """
    entries = [op_entry(op="0012", ts=535.0), op_entry(op="0014", ts=4135.0),
               tomb(op="0014", ts=9000.0)]
    ops = api.build_ops([op_row(1, "0012", 535.29)], entries=entries)["ops"]
    assert [o["meta"]["op"] for o in ops] == ["0012", "0014"]
    assert ops[1]["ts"] == 4135.0, "своё время операции, а не время далёкого соседа"


def test_o10b4_several_erasures_in_a_row_keep_their_order(api):
    """Одно время на две могилы — и порядок между ними становится делом случая."""
    # Один ход: журнал грубее строк на доли секунды — так и есть на живых данных.
    entries = [op_entry(op="0001", ts=535.2), op_entry(op="0002", ts=535.2),
               op_entry(op="0003", ts=535.2), op_entry(op="0004", ts=535.2),
               tomb(op="0002", ts=9000.0), tomb(op="0003", ts=9001.0)]
    rows = [op_row(1, "0001", 535.28), op_row(2, "0004", 535.30)]
    ops = api.build_ops(rows, entries=entries)["ops"]
    assert [o["meta"]["op"] for o in ops] == ["0001", "0002", "0003", "0004"]


def test_o10c_tombstones_do_not_inflate_the_count(api):
    """`total` — «сколько наших операций в контексте». Стёртой там нет."""
    res = api.build_ops([tool_row(1, "read_file", READ_BODY)],
                        entries=[op_entry(), tomb()])
    assert res["total"] == 1, "надгробие не операция"
    assert len(res["ops"]) == 2


def test_o10d_no_grave_over_a_live_operation(api):
    """Строка на месте — надгробия быть не должно ни при каком надгробии в журнале.

    Худшая из возможных неправд: карточка есть, операция работает, а панель
    показывает, что её стёрли.
    """
    body = "#Operation ID: 0014\n" + READ_BODY
    ops = api.build_ops([tool_row(1, "read_file", body)],
                        entries=[op_entry(), tomb()])["ops"]
    assert len(ops) == 1
    assert ops[0]["meta"]["state"] is None


def test_o10d2_no_grave_over_a_row_whose_text_stopped_looking_like_ours(api):
    """Их компакция переписывает результат записи своей сводкой — карточки нет, а строка есть.

    Токены она тратит по-прежнему, поэтому надгробие над ней было бы вдвойне неправдой:
    и операция жива, и человек решил бы, что место в контексте освободилось.
    """
    rewritten = "#Operation ID: 0014\n[write_file] wrote to /w/x.py (2 lines)"
    res = api.build_ops([tool_row(1, "write_file", rewritten)],
                        entries=[op_entry(tool="write_file"), tomb()])
    assert res["ops"] == [], "ни карточки операции, ни надгробия"


def test_o10e_without_a_ledger_the_answer_is_exactly_the_old_one(api):
    before = api.build_ops([tool_row(1, "read_file", READ_BODY)])
    after = api.build_ops([tool_row(1, "read_file", READ_BODY)], entries=[])
    assert before == after


def test_o10f_a_tombstone_without_its_operation_entry_still_shows(api):
    """Журнал мог быть обрезан. Тогда надгробие всё, что есть, — и его хватает."""
    ops = api.build_ops([], entries=[tomb(op="0099", ts=3000.0)])["ops"]
    assert len(ops) == 1
    assert ops[0]["meta"]["op"] == "0099"
    assert ops[0]["ts"] == 3000.0, "времени операции нет — берём время стирания"
    assert ops[0]["tool"] == "operation"


def test_o10g_the_last_tombstone_per_op_wins(api):
    ops = api.build_ops([], entries=[op_entry(), tomb(by="erase tool"),
                                     tomb(by="erase turn")])["ops"]
    assert len(ops) == 1
    assert ops[0]["meta"]["erased_by"] == "erase turn"


def test_o10h_events_are_never_mistaken_for_operations(api):
    """Событие не имеет ни тула, ни пути; попади оно в общий поток — стало бы карточкой."""
    op_entries, tombs = api._ledger_split([op_entry(), tomb()])
    assert len(op_entries) == 1 and list(tombs) == ["0014"]


# --------------------------------------------------------------------------
# O11 — к какому ШАГУ принадлежит карточка (Plan14-Task06)
#
# Клиенту это нужно, чтобы отметка на одной карточке разъехалась на весь ход ДО
# нажатия: расширение делает сервер, но увидеть его человек обязан заранее.
# --------------------------------------------------------------------------

def test_o11_cards_of_one_step_share_a_key(api):
    rows = [
        {"id": 1, "role": "assistant", "content": "", "tool_calls": [
            {"id": "c1"}, {"id": "c2"}]},
        {"id": 2, "role": "tool", "tool_call_id": "c1", "tool_name": "read_file",
         "content": "Operation ID: 0001\nState: verbatim\n"},
        {"id": 3, "role": "tool", "tool_call_id": "c2", "tool_name": "read_file",
         "content": "Operation ID: 0002\nState: verbatim\n"},
        {"id": 4, "role": "assistant", "content": "", "tool_calls": [{"id": "c9"}]},
        {"id": 5, "role": "tool", "tool_call_id": "c9", "tool_name": "read_file",
         "content": "Operation ID: 0009\nState: verbatim\n"},
    ]
    steps = {b["meta"]["op"]: b["meta"].get("step")
             for b in api.build_ops(rows)["ops"]}
    assert steps["0001"] == steps["0002"], "две операции одного шага"
    assert steps["0009"] != steps["0001"], "соседний шаг — другой ключ"


def test_o11b_a_card_without_a_known_step_says_nothing(api):
    """Ключа нет — клиент отметит только саму карточку. Догадка по соседству была
    бы хуже: соседство свойство записи, а связь — идентификатор вызова."""
    rows = [{"id": 1, "role": "tool", "tool_call_id": "c1", "tool_name": "read_file",
             "content": "Operation ID: 0001\nState: verbatim\n"}]
    assert api.build_ops(rows)["ops"][0]["meta"].get("step") is None
