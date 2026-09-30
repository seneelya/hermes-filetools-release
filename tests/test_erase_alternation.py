"""Мостики стирания (`erase`) и служебные строки вне данных для модели (2026-09-30).

  E1  ответ ассистента между двумя репликами человека стёрт — мостик от ассистента между ними
  E2  реплика человека между двумя ответами стёрта — мостик от человека
  E3  цепочка «вызов + результат» между двумя ответами стёрта (правило владения) — мостик
  E4  стёрто начало, лента начинается с ответа ассистента — мостик «conversation start removed»
  E5  стирание без нового стыка одной роли — мостиков нет; старое нарушение не лечим
  E6  тексты erase — свои, короткие, с меткой мостика; не похожи на дистиллят
  E7  мостик (старый и новый) не идёт в данные для модели, как и закрывающая скобка
  E8  их проверка чередования: лента с мостиками — не хуже исходной (шов
      `ctxctl.alternation_repair_count`)
"""
from __future__ import annotations

import pytest

from hermes_filetools.context_ctl import _alternation, _compact, _pairs, _prompts


def _m(role, text, t):
    return {"role": role, "content": text, "timestamp": float(t)}


def _call(call_id):
    return {"id": call_id, "type": "function", "function": {"name": "terminal", "arguments": "{}"}}


def _erase(rows, picked):
    """Путь `_command._erase_places` до коммита: владение, пересборка, чередование."""
    decision = _pairs.settle(rows, set(picked))
    new_rows, _h, _e = _compact._rebuild(rows, [], [], [], decision["patch"], decision["eaten"])
    return _alternation.fit(new_rows, rows, kind="erase")


def _shape(tape):
    return [r["role"] + ("*" if _alternation.is_bridge(r) else "") for r in tape]


def test_e1_reply_between_two_user_turns():
    rows = [_m("user", "a", 1), _m("assistant", "b", 2), _m("user", "c", 3)]
    out, n = _erase(rows, {1})
    assert _shape(out) == ["user", "assistant*", "user"] and n == 1
    assert out[1]["content"] == _alternation.ERASE_TEXT["gap"]


def test_e2_user_turn_between_two_replies():
    rows = [_m("user", "a", 1), _m("assistant", "b", 2), _m("user", "c", 3), _m("assistant", "d", 4)]
    out, n = _erase(rows, {2})
    assert _shape(out) == ["user", "assistant", "user*", "assistant"] and n == 1


def test_e3_call_chain_between_two_replies():
    rows = [_m("user", "a", 1), _m("assistant", "b", 2),
            {"role": "assistant", "content": "", "timestamp": 3.0, "tool_calls": [_call("c1")]},
            {"role": "tool", "tool_call_id": "c1", "content": "out", "timestamp": 4.0},
            _m("assistant", "d", 5)]
    out, n = _erase(rows, {3})        # стёрт результат — правило владения уносит и вызов
    assert _shape(out) == ["user", "assistant", "user*", "assistant"] and n == 1


def test_e4_start_erased_tape_starts_with_a_reply():
    rows = [_m("user", "a", 1), _m("assistant", "b", 2), _m("user", "c", 3)]
    out, n = _erase(rows, {0})
    assert _shape(out) == ["user*", "assistant", "user"] and n == 1
    assert out[0]["content"] == _alternation.ERASE_TEXT["start"]


def test_e5_no_new_seam_no_bridge():
    rows = [_m("user", "a", 1), _m("assistant", "b", 2), _m("user", "c", 3), _m("assistant", "d", 4)]
    out, n = _erase(rows, {2, 3})     # ушла пара целиком — стык user|конец
    assert n == 0 and _shape(out) == ["user", "assistant"]
    old_bad = [_m("user", "a", 1), _m("user", "b", 2), _m("assistant", "c", 3), _m("user", "d", 4)]
    out, n = _erase(old_bad, {3})
    assert n == 0 and _shape(out) == ["user", "user", "assistant"], "старое нарушение — их дело"


def test_e6_erase_texts():
    for text in _alternation.ERASE_TEXT.values():
        assert text.startswith("[" + _prompts.engine_voice()) and text.endswith(
            _prompts.bridge_mark() + "]")
        assert _prompts.is_bridge(text) and not _prompts.is_distillate(text)
        assert len(text) < 90, "коротко (владелец)"


def test_e7_bridges_never_reach_the_model_as_data():
    from hermes_filetools.context_ctl import _render
    old_bridge = "[system — context compaction: the conversation continues]"
    rows = [_m("user", "вопрос", 1), _m("assistant", _alternation.ERASE_TEXT["gap"], 2),
            _m("user", "ещё", 3), _m("assistant", _alternation.BRIDGE_TEXT["continue"], 4),
            _m("user", old_bridge, 5), _m("assistant", "ответ", 6)]
    block = _render.render_block(rows, list(range(6)), {i: f"m000{i + 1}" for i in range(6)})
    assert "вопрос" in block and "ещё" in block and "ответ" in block
    assert _prompts.bridge_mark() not in block and "context compaction:" not in block
    for addr in ("m0002", "m0004", "m0005"):
        assert addr not in block, "и пустого сообщения от мостика не остаётся"


def test_e8_their_check_is_not_worse_with_bridges():
    rows = [_m("user", "a", 1), _m("assistant", "b", 2), _m("user", "c", 3)]
    decision = _pairs.settle(rows, {1})
    plain, _h, _e = _compact._rebuild(rows, [], [], [], decision["patch"], decision["eaten"])
    was = _alternation.their_repairs(rows)
    if was is None:
        pytest.skip("их repair_message_sequence не импортируется — проверять нечем")
    assert _alternation.their_repairs(plain) > was, "без мостика их правило склеило бы"
    bridged, _n = _alternation.fit(plain, rows, kind="erase")
    assert _alternation.their_repairs(bridged) <= was
