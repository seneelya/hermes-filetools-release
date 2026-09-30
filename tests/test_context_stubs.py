"""Механизм 1 (заглушка) и команда `/ctx-drop` — Plan13 Task 3/4.

Что защищается — не «функция что-то вернула», а те инварианты, нарушение которых ломает
ЖИВУЮ сессию и делает это тихо:

  M1  сообщения остаются на месте: пары вызов/результат не рвутся никогда
  M2  вход не мутируется — живую историю гейтвея держит его лок, правка in-place обошла бы
      его проверку версии
  M3  в текст заглушки не попадает сырой `tool_call_id`, свой `op` — попадает
  M4  `arguments` остаётся валидным JSON (спорный пункт `Vision04`, выбран безопасный)
  M5  разрыв пары, найденный линтом, НЕ коммитится — он не наш, поверх чужой поломки нельзя
  M6  адрес не нашёлся — это ответ, а не исключение
  C1  команда без аргументов ничего не меняет
  C2  команда честно называет, какую сессию выбрала
  C3  пропавшие приватные швы дают обходной путь, а не падение
"""
from __future__ import annotations

import json

import pytest

from hermes_filetools.context_ctl import _alternation, _apply, _command, _select, _stubs
from hermes_filetools.context_ctl.engine import ContextCtlEngine


# Тело реалистичного размера. Фикстуры были игрушечными (три десятка символов), и после
# порог `worth_dropping` они честно перестали проходить — а потом владелец отменила и сам
# порог («названа операция — удаляется»). Фикстура осталась реалистичной по размеру, потому
# что механизм сделан для больших ответов, но отказ по размеру больше не существует.
# И ОБЯЗАТЕЛЬНО С КОНВЕРТОМ. Прежняя фикстура была голым текстом без заголовка и
# разделителя, то есть не похожа на настоящий ответ тула ничем. Механизм удаления работает
# ровно с конвертом — заголовок оставить, тело выбросить, — и на фикстуре без конверта он
# честно не работал бы, а тест бы этого не показал.
NL_ = chr(10)
HEAD = NL_.join([
    "◆Operation ID: 0156",
    "◆File: /x/foo.py",
    "◆Size: 40 lines, 2560 bytes, 2560 chars, LF, max/avg line 64/64.0",
    "◆Modified: 2026-09-09 01:48",
    "◆Encoding: utf-8",
    "◆FileTag: x42",
    "◆○○○",
])
BODY = HEAD + NL_ + ("Line: длинная строка содержимого файла, которых в ответе много" + NL_) * 40


def history(call_id="call_1", content=None):
    content = BODY if content is None else content
    return [
        {"role": "user", "content": "прочитай файл"},
        {"role": "assistant", "content": "читаю", "tool_calls": [
            {"id": call_id, "type": "function",
             "function": {"name": "read_file",
                          "arguments": json.dumps({"path": "/x/foo.py"})}}]},
        {"role": "tool", "tool_call_id": call_id, "content": content},
        {"role": "assistant", "content": "прочитал"},
    ]


LEDGER = {"call_1": {"tool": "read_file", "path": "/x/foo.py", "from": 12, "to": 20,
                     "op": "0156", "tool_call_id": "call_1"}}


# --- M1..M6: механизм ----------------------------------------------------------------


def test_m1_messages_stay_in_place_and_pairs_hold():
    msgs = history()
    out, report = _stubs.apply_stubs(msgs, [{"tool_call_id": "call_1", "mode": "content"}],
                                     LEDGER)
    assert len(out) == len(msgs), "заглушка не удаляет сообщений — иначе рвётся пара"
    assert [m["role"] for m in out] == [m["role"] for m in msgs]
    assert _stubs.lint_pairs(out) == []
    assert report["problems"] == []
    assert len(report["stubbed"]) == 1
    # `committed` ставит ДВИЖОК, а не эта функция: apply_stubs считает, что произошло,
    # решение «отдавать ли это в историю» принимается уровнем выше, после линта.
    assert "committed" not in report


def test_m2_the_input_list_is_not_mutated():
    msgs = history()
    before = json.dumps(msgs, ensure_ascii=False)
    _stubs.apply_stubs(msgs, [{"tool_call_id": "call_1"}], LEDGER)
    assert json.dumps(msgs, ensure_ascii=False) == before, (
        "живую историю гейтвея нельзя править на месте: он держит её под своим локом и "
        "проверяет версию — правка in-place обошла бы обе защиты")


def test_m3_stub_text_carries_our_op_and_never_the_raw_call_id():
    out, _ = _stubs.apply_stubs(history(), [{"tool_call_id": "call_1"}], LEDGER)
    text = out[2]["content"]
    assert "0156" in text, "свой номер — разметка, он обязан быть (Vision04)"
    assert "call_1" not in text, (
        "сырой tool_call_id в тексте, который видит модель, — их ошибка в промпте "
        "суммаризации, не наследуем")
    # Путь и размер остаются потому, что они БЫЛИ в заголовке ответа, а не потому, что мы
    # их дописали. Они описывают файл, а не то, чем владеет модель, и над ними стоит явное
    # состояние — врать им нечем (решение владельца 2026-09-09: полезный след оставляем).
    assert "◆State: removed_from_context" in text
    assert "/x/foo.py" in text and "Size: 40 lines" in text
    assert text.rstrip().endswith("◆○○○"), "тело выброшено целиком, разделитель остаётся"
    args = out[1]["tool_calls"][0]["function"]["arguments"]
    assert "call_1" not in args and "0156" in args


def test_m3b_the_stub_never_invents_a_fact_the_answer_did_not_carry():
    """Леджера нет вовсе — текст обязан быть тем же: мы ничего не сочиняем.

    Раньше заглушка ДОПИСЫВАЛА путь и диапазон из записи леджера, и без записи выходило
    «[выброшено]» без единого факта. Теперь источник один — сам заголовок, поэтому наличие
    или отсутствие леджера на текст не влияет никак.
    """
    with_ledger, _ = _stubs.apply_stubs(history(), [{"tool_call_id": "call_1"}], LEDGER)
    without, _ = _stubs.apply_stubs(history(), [{"tool_call_id": "call_1"}], {})
    assert with_ledger[2]["content"] == without[2]["content"]
    assert "0156" in without[2]["content"]


def test_m4_arguments_stay_valid_json():
    out, _ = _stubs.apply_stubs(history(), [{"tool_call_id": "call_1"}], LEDGER)
    args = out[1]["tool_calls"][0]["function"]["arguments"]
    assert isinstance(json.loads(args), dict), (
        "Vision04 допускает простую строку, но Recon18 нашёл у них усечение С СОХРАНЕНИЕМ "
        "валидного JSON, привязанное к живому 400 провайдера — выбран безопасный вариант")


def test_m4b_sdk_object_tool_calls_are_handled_not_skipped():
    """В истории элемент `tool_calls` может быть объектом SDK, а не словарём.

    Их собственный сериализатор на этом спотыкается и печатает `name(...)` вместо
    аргументов (`Vision04.1`). Нам спотыкаться нельзя: не узнав `id`, мы не найдём пару.
    """
    class _Fn:
        name = "read_file"
        arguments = '{"path":"/x/foo.py"}'

    class _Call:
        id = "call_1"
        type = "function"
        function = _Fn()

    msgs = history()
    msgs[1] = {**msgs[1], "tool_calls": [_Call()]}
    out, report = _stubs.apply_stubs(msgs, [{"tool_call_id": "call_1"}], LEDGER)
    call = out[1]["tool_calls"][0]
    assert isinstance(call, dict) and call["id"] == "call_1"
    assert json.loads(call["function"]["arguments"])["__dropped__"]
    assert len(report["stubbed"]) == 1


def test_m5_a_pre_existing_hole_is_reported_and_not_committed():
    """Разрыв пары мы создать не можем — значит он был до нас, и коммитить нельзя."""
    broken = history()
    del broken[2]                      # вызов есть, результата в истории нет
    out, report = _stubs.apply_stubs(broken, [{"tool_call_id": "call_1"}], LEDGER)
    assert report["problems"], "линт обязан увидеть вызов без результата"
    assert [p["kind"] for p in report["problems"]] == ["call_without_result"]


def test_m6_a_missing_address_is_an_answer_not_an_exception():
    out, report = _stubs.apply_stubs(history(), [{"tool_call_id": "нет такого"}], LEDGER)
    assert out is not None and report["not_found"] == ["нет такого"]
    assert report["stubbed"] == []


def test_m6b_ops_resolve_from_the_ledger(monkeypatch):
    """Леджер — второй источник адреса, для случая «в истории номер уже не читается»."""
    monkeypatch.setattr(_stubs, "_entries", lambda session: [LEDGER["call_1"]])
    items, missing, no_address = _stubs.ops_to_call_ids("s", ["0156", "ffff"])
    assert items == [{"tool_call_id": "call_1", "op": "0156", "mode": "content",
                      "src": "леджер"}]
    assert missing == ["ffff"] and no_address == []


def test_m6c_ops_resolve_from_the_history_without_any_ledger(monkeypatch):
    """ГЛАВНЫЙ путь адресации: номер напечатан в тексте ответа, id есть у сообщения.

    Найдено по живому прогону 2026-09-08: их contextvars переехали, леджер остался без
    `tool_call_id` — и адресация по леджеру умерла целиком. История же содержит ОБЕ
    половины связи сама, поэтому этот путь в тот же момент продолжал работать.
    """
    monkeypatch.setattr(_stubs, "_entries", lambda session: [])
    msgs = history(content="Operation ID: 001e" + chr(10) + "FileTag: ab" + chr(10) + BODY)
    items, missing, no_address = _stubs.ops_to_call_ids("s", ["001e"], msgs)
    assert items == [{"tool_call_id": "call_1", "op": "001e", "mode": "content",
                      "src": "история"}]
    assert missing == [] and no_address == []


def test_m6d_an_op_deeper_in_the_body_is_not_taken_as_an_address(monkeypatch):
    """`Operation ID` ниже заголовка — это СОДЕРЖИМОЕ прочитанного файла, чужой номер."""
    monkeypatch.setattr(_stubs, "_entries", lambda session: [])
    msgs = history(content=chr(10).join(
        ["FileTag: ab", "Size: 10", "x", "y", "Operation ID: 00ff", BODY]))
    items, missing, _ = _stubs.ops_to_call_ids("s", ["00ff"], msgs)
    assert items == [] and missing == ["00ff"]


def test_m6e_was_recorded_but_has_no_address_is_its_own_answer(monkeypatch):
    """Третий исход, оплаченный живым прогоном: «была, но адресовать нечем».

    Владелец набрала `/ctx-drop 001e`, получила «номер не найден» и решила, что дело в
    шестнадцатеричной записи. Дело было в отсутствии `tool_call_id` — и это ДРУГАЯ
    поломка с другой починкой, поэтому она обязана называться отдельно.
    """
    entry = {**LEDGER["call_1"], "op": "001e"}
    entry.pop("tool_call_id")
    monkeypatch.setattr(_stubs, "_entries", lambda session: [entry])
    items, missing, no_address = _stubs.ops_to_call_ids("s", ["001e"], [])
    assert items == [] and missing == [] and no_address == ["001e"]


def test_m7_the_engine_never_touches_history(monkeypatch):
    """`compress()` — identity ВСЕГДА, включая нашу прежнюю метку.

    До 2026-09-08 ручные механизмы применялись здесь, и живой прогон показал, почему так
    нельзя: их конвейер коммитит результат `compress()` через `archive_and_compact`, а тот
    архивирует старые строки и вставляет список заново — каждое переписанное сообщение
    становится новым поколением и в ИНТЕРФЕЙСЕ уезжает в конец разговора. Модель видела
    верный промпт, человек — перепутанную переписку. Хирургия ушла на `replace_messages`,
    а этот метод обязан остаться безвредным: если он снова начнёт что-то менять, дефект
    вернётся молча и только в интерфейсе.
    """
    engine = ContextCtlEngine()
    engine._session_id = "s-engine-identity"
    _select.write_selection("s-engine-identity", [{"tool_call_id": "call_1", "op": "0156"}])
    msgs = history()
    for focus in (None, "важная тема", _stubs.FOCUS_MARKER, "ctxdrop:0156"):
        assert engine.compress(msgs, focus_topic=focus, force=True) is msgs, focus


# --- C1..C3: команда ------------------------------------------------------------------


class _FakeAgent:
    _cached_system_prompt = "system"
    tools = None
    model = "m"
    provider = "p"
    parent_session_id = ""      # пусто = чат человека; непусто = сабагент/форк


class _FakeEngine:
    name = "ctxctl"

    def __init__(self, session_id, last_active=1.0, parent=""):
        self.session_id = session_id
        self._session_id = session_id
        self._last_active = last_active
        self._agent = _FakeAgent()
        self._agent.parent_session_id = parent

    def live_agent(self):
        return self._agent


class _FakeDB:
    """Минимальная база: активные строки плюс запись. Команда читает ИМЕННО её.

    Строки базы несут поля, которых в памяти гейтвея нет (`id`, `timestamp`,
    `display_kind`), — фикстура их проставляет, чтобы тесты видели то же расхождение
    форматов, что и живая сессия.
    """

    def __init__(self, rows):
        self.rows = [dict(row) for row in rows]
        self.written = None

    def get_messages(self, session_id, **kw):
        return [dict(row) for row in self.rows]

    def replace_messages(self, session_id, messages, **kw):
        self.written = [dict(m) for m in messages]
        self.rows = [dict(m) for m in messages]


def db_rows(call_id="call_1", content=None):
    """`history()` в форме строк базы: те же сообщения плюс колонки, которых в памяти нет."""
    rows = []
    for index, msg in enumerate(history(call_id, content), start=1):
        rows.append({**msg, "id": 6300 + index, "timestamp": 1000.0 + index,
                     "display_kind": "chat"})
    return rows


@pytest.fixture
def one_live_session(monkeypatch):
    engine = _FakeEngine("s-cmd")
    engine._agent.session_id = "s-cmd"
    engine._agent._session_db = _FakeDB(db_rows())
    session = {"agent": engine._agent, "history": history(), "history_lock": None,
               "history_version": 0}
    monkeypatch.setattr(_apply, "human_sessions", lambda: [("s-cmd", engine)])
    monkeypatch.setattr(_apply, "find_gateway_session",
                        lambda agent: (session, "сессия гейтвея: s-cmd"))
    monkeypatch.setattr(_stubs, "_entries", lambda s: [LEDGER["call_1"]])
    monkeypatch.setattr(_stubs, "ledger_index", lambda s: LEDGER)
    return engine, session


def test_c1_no_arguments_changes_nothing(one_live_session):
    _select.write_selection("s-cmd", [])
    out = _command.handle("")
    assert "ничего не отмечено" in out
    assert "отмечено:" not in out, "пустой ввод не должен рапортовать о выборе"
    assert _select.read_selection("s-cmd")["items"] == []


def test_c1b_no_arguments_shows_the_price_of_applying(one_live_session):
    _select.write_selection("s-cmd", [{"tool_call_id": "call_1", "op": "0156"}])
    out = _command.handle("")
    assert "отмечено: 1" in out and "0156" in out
    assert "останется" in out


def test_c2_the_command_names_the_session_it_chose(one_live_session):
    assert "s-cmd" in _command.handle("")


def test_c2b_several_live_sessions_pick_the_most_recently_active(monkeypatch):
    quiet, loud = _FakeEngine("s-quiet", 10.0), _FakeEngine("s-loud", 99.0)
    monkeypatch.setattr(_apply, "human_sessions",
                        lambda: [("s-quiet", quiet), ("s-loud", loud)])
    session_id, engine, why = _apply.pick_engine()
    assert session_id == "s-loud" and engine is loud
    assert "s-loud" in why and "2" in why


def test_c2c_ambiguous_sessions_refuse_rather_than_guess(monkeypatch):
    a, b = _FakeEngine("s-a", 0.0), _FakeEngine("s-b", 0.0)
    monkeypatch.setattr(_apply, "human_sessions",
                        lambda: [("s-a", a), ("s-b", b)])
    session_id, engine, why = _apply.pick_engine()
    assert engine is None, "ни в одной не отвечала модель — угадывать нечем"
    assert "s-a" in why and "s-b" in why


def test_c2d_an_explicit_session_overrides_the_guess(monkeypatch):
    a, b = _FakeEngine("s-a", 1.0), _FakeEngine("s-b", 99.0)
    monkeypatch.setattr(_apply, "human_sessions",
                        lambda: [("s-a", a), ("s-b", b)])
    session_id, engine, _ = _apply.pick_engine("s-a")
    assert session_id == "s-a" and engine is a


def test_c3_a_failed_commit_says_so_and_ends_with_a_verdict(monkeypatch, one_live_session):
    """Отказ примитива замены — это ответ с приговором, а не тишина и не исключение."""
    monkeypatch.setattr(_apply, "commit_replacement",
                        lambda session, agent, rows, memory=None, **kw: (False, "идёт ход, повтори позже"))
    out = _command.handle("0156")
    assert "ИТОГ: НЕ применено" in out and "идёт ход" in out


def test_c3b_a_missing_session_registry_is_reported_not_raised(monkeypatch):
    engine = _FakeEngine("s-cmd")
    monkeypatch.setattr(_apply, "human_sessions", lambda: [("s-cmd", engine)])
    monkeypatch.setattr(_apply, "find_gateway_session",
                        lambda agent: (None, "_sessions пропал"))
    out = _command.handle("0156")
    assert "_sessions пропал" in out
    assert "ИТОГ: контекст не тронут" in out


def test_c4_unknown_numbers_say_so_and_touch_nothing(one_live_session, monkeypatch):
    calls = []
    monkeypatch.setattr(_apply, "commit_replacement",
                        lambda session, agent, rows, memory=None, **kw: calls.append(1) or (True, "ok"))
    out = _command.handle("ffff,eeee")
    assert calls == [], "неизвестные номера не повод трогать контекст"
    assert "нет ни в истории, ни в леджере" in out and "ffff" in out


def test_c5_numbers_parse_from_commas_and_spaces_alike():
    assert _command._split_ops("0156, 0157 0158,,#0159") == ["0156", "0157", "0158", "0159"]
    assert _command._split_ops("0156,0156") == ["0156"]


def test_c6_the_answer_names_the_cold_recount_cost(one_live_session, monkeypatch):
    monkeypatch.setattr(_apply, "commit_replacement",
                        lambda session, agent, rows, memory=None, **kw: (True, "применено"))
    out = _command.handle("0156")
    assert "холодный пересчёт" in out, (
        "цена применения — полный пересчёт префикса, и человек должен видеть это сразу, "
        "а не узнать из счёта за следующий запрос")


def test_c7_a_running_subagent_is_never_picked(monkeypatch):
    """Сабагент работает — значит он самый активный. Выбрать его нельзя.

    Наш патч `__init__` ловит КАЖДОГО агента, включая делегатов. Без фильтра команда,
    набранная во время работы сабагента, ушла бы в его сессию: у него свежее
    `_last_active`, чем у чата, в котором человек только что печатал.
    """
    chat = _FakeEngine("s-chat", last_active=10.0)
    child = _FakeEngine("s-child", last_active=99.0, parent="s-chat")
    monkeypatch.setattr(_apply._bridge, "live_engines",
                        lambda: [("s-chat", chat), ("s-child", child)])
    # Реестр гейтвея недоступен -> идём фолбэком по нашему реестру, где детей отсекаем сами.
    monkeypatch.setattr(_apply, "human_sessions",
                        lambda: [(sid, e) for sid, e in _apply._bridge.live_engines()
                                 if not _apply.is_child_agent(e.live_agent())])
    session_id, engine, _why = _apply.pick_engine()
    assert session_id == "s-chat" and engine is chat


def test_c7b_only_subagents_alive_refuses_instead_of_applying(monkeypatch):
    child = _FakeEngine("s-child", last_active=99.0, parent="s-chat")
    monkeypatch.setattr(_apply._bridge, "live_engines", lambda: [("s-child", child)])
    monkeypatch.setattr(_apply, "human_sessions", lambda: [])
    monkeypatch.setattr(_apply._bridge, "installed", lambda: True)
    _sid, engine, why = _apply.pick_engine()
    assert engine is None and "сабагент" in why


def test_c7c_gateway_registry_is_the_source_of_truth_for_human_sessions(monkeypatch):
    """Реестр гейтвея содержит только верхнеуровневые чаты — сабагентов в нём нет."""
    chat = _FakeEngine("s-chat")
    fake_gw = type("GW", (), {"_sessions": {"gwkey": {"agent": chat.live_agent()}}})
    chat.live_agent().context_compressor = chat
    # И в sys.modules, И атрибутом на пакете: `from tui_gateway import server` смотрит
    # сначала атрибут пакета и только потом sys.modules, а что из двух сработает —
    # зависит от того, импортировал ли кто-то этот подмодуль раньше в этом процессе.
    import sys
    monkeypatch.setitem(sys.modules, "tui_gateway.server", fake_gw)
    pkg = sys.modules.get("tui_gateway")
    if pkg is not None:
        monkeypatch.setattr(pkg, "server", fake_gw, raising=False)
    monkeypatch.setattr(_apply._bridge, "live_engines", lambda: [])
    found = _apply.human_sessions()
    assert found == [("s-chat", chat)]


def test_m8_the_stub_prints_the_canonical_operation_id_header():
    """Заглушка обязана нести номер В КАНОНИЧЕСКОМ виде, иначе панель теряет операцию.

    Панель сшивает запись леджера со строкой сообщения по `tool_call_id`, а когда его нет —
    по номеру, вычитанному из ТЕКСТА шаблоном `Operation ID: <hex>`. Подмена стирает
    исходный заголовок; если заглушка напишет номер вольно (`[op 001e]`), операция после
    выбрасывания станет для панели безымянной — статус `unknown` вместо `compacted`, и
    карточка не изменится. Ровно это и увидела владелец живьём 2026-09-08.
    """
    import re as _re
    canon = _re.compile(r"Operation ID:\s*([0-9a-fA-F]{4,})")

    out, _ = _stubs.apply_stubs(history(), [{"tool_call_id": "call_1"}], LEDGER)
    first_lines = out[2]["content"].split(chr(10), 4)[:4]
    found = [canon.search(line) for line in first_lines]
    assert any(found), "в первых строках заглушки нет канонического Operation ID"
    assert next(m for m in found if m).group(1).lower() == "0156"


def test_m8b_the_stub_is_still_matchable_by_the_panel_after_the_drop():
    """Сквозная проверка того же: панельный парсер должен найти номер в НАШЕЙ заглушке."""
    import importlib.util
    import sys
    from pathlib import Path

    api_file = Path(__file__).resolve().parent.parent / "dashboard" / "plugin_api.py"
    name = "test_filetools_api_stub_join"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, api_file)
    api = importlib.util.module_from_spec(spec)
    sys.modules[name] = api
    try:
        spec.loader.exec_module(api)
        out, _ = _stubs.apply_stubs(history(), [{"tool_call_id": "call_1"}], LEDGER)
        assert api._op_from_header(out[2]["content"]) == "0156"
        # И обратный join: строка с нашей заглушкой находится по номеру.
        rows = [{"tool_name": "read_file", "content": out[2]["content"], "active": 1}]
        assert api._rows_by_op(rows).get("0156") is rows[0]
    finally:
        sys.modules.pop(name, None)


def test_m8c_an_answer_without_an_envelope_is_replaced_not_refused(monkeypatch):
    """Нет конверта — сохранять нечего, но и отказывать нельзя: заменяем целиком.

    Владелец 2026-09-09: «я не хочу встретить подобный отказ ни у одной операции». Строка
    без нашего заголовка тул-признаков не несла и раньше, поэтому терять панели тут нечего,
    а адрес мы дописываем — иначе строка после удаления не найдётся по номеру.
    """
    monkeypatch.setattr(_stubs, "_entries", lambda session: [])
    naked = "просто текст без заголовка и разделителя" + NL_ + "x" * 3000
    out = _stubs.result_stub(None, "0156", naked)
    assert out != naked, "отказа быть не должно"
    assert "State: removed_from_context" in out
    assert "0156" in out, "адрес обязан остаться — по нему сшивается панель"
    assert len(out) < len(naked)


def test_c8_every_answer_ends_with_a_verdict_line(monkeypatch, one_live_session):
    """Требование владельца: тронут контекст или нет — читается с одной строки.

    Живой прогон 2026-09-08 дал худший из возможных исходов: команда сработала МОЛЧА, и о
    правке человек узнал только по уменьшившемуся промпту. Пустой или бессловесный ответ
    здесь — дефект, а не мелочь, поэтому проверяются ВСЕ ветки.
    """
    monkeypatch.setattr(_apply, "commit_replacement",
                        lambda session, agent, rows, memory=None, **kw: (True, "применено"))
    _select.write_selection("s-cmd", [])
    for args in ("", "0156", "ffff", "0156,ffff"):
        out = _command.handle(args)
        assert out.startswith("── ctx-drop ──"), args
        assert any(line.startswith("ИТОГ:") for line in out.split(chr(10))), (args, out)


def test_c9_commit_writes_the_db_before_the_gateway_memory(monkeypatch, one_live_session):
    """Порядок записи: сначала база, потом память гейтвея.

    База — то, из чего интерфейс и следующая сессия читают историю; память гейтвея — то, из
    чего собирается следующий запрос. Упасть между ними можно, но если первой падает база,
    человек видит ровно то, что и было. В обратном порядке он получил бы правку,
    исчезающую при перезапуске.
    """
    _engine, session = one_live_session
    order = []

    class _DB:
        rows = [{"role": "user", "content": "новое", "tool_call_id": None}]

        def get_messages(self, session_id, **kw):
            return list(self.rows)

        def replace_messages(self, session_id, messages, **kw):
            order.append(("db", kw.get("archive_dropped"), kw.get("reject_active_turn_lease")))

    agent = session["agent"]
    agent._session_db = _DB()
    agent.session_id = "s-cmd"
    original = list(session["history"])

    held = session["history"]          # чужая ссылка на тот же список
    ok, why = _apply.commit_replacement(session, agent, [{"role": "user", "content": "новое"}])
    assert ok and order == [("db", True, True)], (ok, why, order)
    assert session["history"] != original, "память гейтвея обязана обновиться после базы"
    assert session["history_version"] == 1
    assert session["history"] is held, (
        "историю надо менять НА МЕСТЕ: переприсваивание ключа оставляет держателей старого "
        "объекта (активный ход, коллбэки гейтвея) дописывать в него, и наша правка "
        "превращается в замороженный снимок — живьём 2026-09-08 после первого применения "
        "команда перестала находить операции, видимые в стакане")
    assert held == [{"role": "user", "content": "новое"}]


def test_c9b_commit_refuses_without_a_session_db(one_live_session):
    _engine, session = one_live_session
    agent = session["agent"]
    agent._session_db = None
    ok, why = _apply.commit_replacement(session, agent, [{"role": "user", "content": "x"}])
    assert ok is False and "session_db" in why


# --- пересечение половин: панель обязана УЗНАВАТЬ строку после выбрасывания ---------

_REAL_READ_ANSWER = chr(10).join([
    "◆Operation ID: 000b",
    "◆File: /workspace/temp4/patch_test.txt",
    "◆Size: 7 lines, 235 bytes, 235 chars, LF, max/avg line 42/25.1",
    "◆Modified: 2026-09-07 23:20",
    "◆Encoding: utf-8",
    "◆FileTag: xce",
    "◆○○○",
    "◆■BLOCK xce : 1-7",
    "Line 1: Hello World",
]) + chr(10) + BODY


def _api():
    import importlib.util
    import sys
    from pathlib import Path

    name = "test_filetools_api_is_ours"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parent.parent / "dashboard" / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_p1_a_stubbed_row_is_still_recognised_as_ours():
    """Заглушка ОБЯЗАНА сохранять заголовок ответа — иначе операция исчезает из стакана.

    Стоимость этого теста уже заплачена живьём 2026-09-08. Первая версия заглушки писала
    только свою строку, стирая опознавательные признаки нашего ответа (`_TOOL_SIGNALS`:
    для `read_file` это `FileTag:`/`Size:`). Последствие было не «карточка не сменила
    цвет», а `/ops` отдавал 20 карточек из 30: девять выброшенных операций пропадали из
    стакана при перезагрузке — то есть выбрасывание содержимого выглядело как исчезновение
    самой операции. Это ровно тот класс дефекта, ради которого в проекте вообще есть тесты
    на пересечении половин.
    """
    api = _api()
    msgs = [
        {"role": "assistant", "content": "читаю", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "tool_name": "read_file",
         "content": _REAL_READ_ANSWER},
    ]
    entry = {"tool": "read_file", "path": "/workspace/temp4/patch_test.txt",
             "from": 1, "to": 7, "op": "000b", "tool_call_id": "call_1"}
    out, report = _stubs.apply_stubs(msgs, [{"tool_call_id": "call_1", "op": "000b"}],
                                     {"call_1": entry})
    stubbed_row = {"tool_name": "read_file", "content": out[1]["content"], "active": 1}

    assert api._is_ours(stubbed_row), (
        "панель не узнала нашу строку после выбрасывания — операция выпадет из /ops")
    assert api._op_from_header(stubbed_row["content"]) == "000b", "адрес потерян"
    assert "Size: 7 lines" in stubbed_row["content"], "факты о файле обязаны остаться"
    # А `FileTag` УХОДИТ, и это не противоречие: он адресует блоки тела (`■BLOCK xce`),
    # которого больше нет. Признак «наша строка» держится на `Size:` — в `_TOOL_SIGNALS`
    # для `read_file` хватает любого из двух, и это ровно та страховка, ради которой их два.
    assert "FileTag" not in stubbed_row["content"]
    assert "Line 1: Hello World" not in stubbed_row["content"], "тело обязано уйти"
    assert report["chars_saved"] > 0


def test_p2_the_saving_is_the_body_not_the_header():
    """Сохранённый заголовок стоит немного, а выбрасывается именно дорогое."""
    big = _REAL_READ_ANSWER + chr(10) + (chr(10).join(f"Line {n}: x" * 20
                                                      for n in range(400)))
    entry = {"tool": "read_file", "path": "/x/foo.py", "from": 1, "to": 400,
             "op": "000b", "tool_call_id": "call_1"}
    text = _stubs.result_stub(entry, "000b", big)
    assert len(text) < len(big) / 20, "экономия должна быть кратной, а не косметической"
    assert "◆File: /workspace/temp4/patch_test.txt" in text and "◆Size: 7 lines" in text


def test_p3_a_tiny_answer_is_removed_too_and_does_not_grow():
    """Названа операция — она удаляется. Без порогов и без нотаций."""
    tiny = chr(10).join([
        "◆Operation ID: 0009",
        "◆File: /workspace/temp4/cp866.txt",
        "◆Size: 1 lines, 6 bytes, 3 chars",
        "◆Encoding: big5hkscs (guessed)",
        "◆FileTag: xb7",
        "◆○○○",
        "три",
    ])
    msgs = [
        {"role": "assistant", "content": "читаю", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path":"/x"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": tiny},
    ]
    out, report = _stubs.apply_stubs(msgs, [{"tool_call_id": "call_1", "op": "0009"}], {})
    assert report["skipped"] == [], "нотаций быть не должно — номер назван, значит делаем"
    assert len(report["stubbed"]) == 1
    assert len(out[1]["content"]) < len(tiny), "правка не имеет права УВЕЛИЧИТЬ строку"
    assert "три" not in out[1]["content"], "тело ушло"
    assert "0009" in out[1]["content"], "адрес остался"


def test_p3b_a_write_is_removed_through_its_ARGUMENTS():
    """У записи дорогое — аргументы вызова, а ответ одна строка подтверждения.

    Дефект, найденный владельцем живьём 2026-09-09: судьбу аргументов решал результат, и
    запись с файлом на десять килобайт в `arguments` получала «удалять нечего», потому что
    её ОТВЕТ переписывать было не во что. Две половины операции решаются независимо.
    """
    payload = "line of a file being written" + chr(10)
    args = json.dumps({"path": "/w/big.txt", "content": payload * 400}, ensure_ascii=False)
    confirmation = chr(10).join([
        "◆Operation ID: 0009",
        "◆File: /w/big.txt",
        "◆Patched: 1 file, 400 lines added",
        "◆Encoding: utf-8",
    ])
    msgs = [
        {"role": "assistant", "content": "пишу", "tool_calls": [
            {"id": "call_w", "type": "function",
             "function": {"name": "write_file", "arguments": args}}]},
        {"role": "tool", "tool_call_id": "call_w", "content": confirmation},
    ]
    out, report = _stubs.apply_stubs(msgs, [{"tool_call_id": "call_w", "op": "0009"}], {})

    assert report["skipped"] == [], "отказа быть не должно"
    row = report["stubbed"][0]
    assert row["saved_args"] > 10000, ("экономия аргументов не учтена: "
                                       f"{row['saved_args']}")
    # По ответу экономия слегка ОТРИЦАТЕЛЬНАЯ: тела у него нет, а строка состояния
    # добавилась. Так и должно быть, и цифра печатается как есть — это честнее, чем
    # прятать её или отказываться от удаления из-за неё.
    assert row["saved_result"] < 0
    assert row["saved"] == row["saved_args"] + row["saved_result"]
    # Аргументы выпотрошены...
    new_args = out[0]["tool_calls"][0]["function"]["arguments"]
    assert "line of a file being written" not in new_args
    assert json.loads(new_args)["__dropped__"].startswith("removed_from_context")
    # ...а у ответа без тела остался штамп состояния, иначе панель сочтёт его дословным.
    assert "State: removed_from_context" in out[1]["content"]
    assert "◆File: /w/big.txt" in out[1]["content"], "полезный след сохраняется"
    assert _stubs.lint_pairs(out) == [], "пара вызов/результат не рассогласована"


def test_p4_the_stub_always_carries_exactly_one_address():
    """Адрес в заглушке — инвариант: ни нуля, ни двух.

    Ноль означает, что операция потеряна и для панели (сшивка по номеру), и для человека
    (спросить модель «что было в 000b» будет нечем). Два — что одна операция читается как
    две. Проверяются оба граничных случая: ответ С заголовком (адрес уже внутри) и ответ
    БЕЗ заголовка вовсе (адрес надо дописать).
    """
    entry = {"tool": "read_file", "path": "/x/foo.py", "from": 1, "to": 9, "op": "000b"}
    # `_REAL_READ_ANSWER` несёт `000b` в своём заголовке — тот же номер, что и запись
    # леджера: адрес мы и берём из истории, так что расходиться им негде.
    found = _stubs._OP_IN_HEADER.findall(_stubs.result_stub(entry, "000b", _REAL_READ_ANSWER))
    assert found == ["000b"], found

    # У ответа без конверта заголовка нет, поэтому он заменяется целиком — и адрес там
    # тоже ровно один: без него строка перестала бы находиться по номеру.
    naked = "строка содержимого" + NL_ + "ещё одна" + NL_ + "и третья" + NL_ + "x" * 3000
    assert _stubs._OP_IN_HEADER.findall(_stubs.result_stub(entry, "000b", naked)) == ["000b"]


def test_c10_a_list_that_would_lose_active_rows_is_refused(one_live_session):
    """Список без части активных строк базы НЕ коммитим — иначе они исчезают из разговора.

    Это самый дорогой урок дня (2026-09-08). `replace_messages(archive_dropped=True)`
    архивирует ВСЕ активные строки и вставляет ровно переданное. Мы брали список из
    `session["history"]`, считая его полной историей, — и он ею не был: чтение на 36 066
    символов (`op 001f`) лежало в базе, но не в снимке гейтвея, и наш коммит отправил его в
    rewind-архив. Владелец не выбирала эту операцию; она просто исчезла из контекста.
    Поэтому теперь сверяемся с БАЗОЙ и при расхождении отказываемся.
    """
    _engine, session = one_live_session
    written = []

    class _DB:
        def get_messages(self, session_id, **kw):
            return [
                {"role": "user", "content": "первое", "tool_call_id": None},
                {"role": "tool", "content": "большой ответ", "tool_call_id": "call_lost"},
            ]

        def replace_messages(self, session_id, messages, **kw):
            written.append(messages)

    agent = session["agent"]
    agent._session_db = _DB()
    agent.session_id = "s-cmd"

    # В списке нет строки `call_lost` — применять нельзя.
    ok, why = _apply.commit_replacement(
        session, agent, [{"role": "user", "content": "первое", "tool_call_id": None}])
    assert ok is False and written == [], "коммит обязан быть отменён"
    assert "ОТКАЗ" in why and "потерять" in why


def test_c10b_a_complete_list_still_commits(one_live_session):
    """Защита не должна мешать нормальному применению: полный список проходит."""
    _engine, session = one_live_session
    written = []
    full = [
        {"role": "user", "content": "первое", "tool_call_id": None},
        {"role": "tool", "content": "заглушка", "tool_call_id": "call_1"},
    ]

    class _DB:
        def get_messages(self, session_id, **kw):
            # В базе тот же набор, но у тул-строки ещё ДОСЛОВНОЕ содержимое: сверка идёт по
            # адресу, поэтому изменение текста не должно выглядеть как потеря строки.
            return [
                {"role": "user", "content": "первое", "tool_call_id": None},
                {"role": "tool", "content": "большой дословный ответ",
                 "tool_call_id": "call_1"},
            ]

        def replace_messages(self, session_id, messages, **kw):
            written.append(messages)

    agent = session["agent"]
    agent._session_db = _DB()
    agent.session_id = "s-cmd"
    ok, why = _apply.commit_replacement(session, agent, full)
    assert ok is True and written == [full], why

def test_c10c_a_deliberately_erased_row_is_not_a_loss(one_live_session):
    """`erase tool` УДАЛЯЕТ строки — и без этого предохранитель не давал ему работать вовсе.

    Живой прогон владельца 2026-09-09: отказ приходил на те самые строки, которые она и
    стирала («в базе есть 3 активных строк, которых нет у нас» — и это были 000a/000b/000c).
    Предохранитель писался под механизм 1, где число строк не меняется никогда.
    """
    _engine, session = one_live_session
    written = []
    erased = {"role": "tool", "content": "большой ответ", "tool_call_id": "call_1"}
    stays = {"role": "user", "content": "первое", "tool_call_id": None}

    class _DB:
        def get_messages(self, session_id, **kw):
            return [dict(stays), dict(erased)]

        def replace_messages(self, session_id, messages, **kw):
            written.append(messages)

    agent = session["agent"]
    agent._session_db = _DB()
    agent.session_id = "s-cmd"

    # Без объявления удалённого — отказ, как и было.
    ok, why = _apply.commit_replacement(session, agent, [stays])
    assert ok is False and written == [] and "ОТКАЗ" in why

    # С объявлением — проходит, и в базу уходит именно укороченный список.
    ok, why = _apply.commit_replacement(session, agent, [stays], removed=[erased])
    assert ok is True, why
    assert written == [[stays]]


def test_c10d_the_guard_does_not_go_blind_when_something_is_erased(one_live_session):
    """Пропускаем РОВНО названное. Одно удаление не должно амнистировать всё остальное."""
    _engine, session = one_live_session
    written = []
    erased = {"role": "tool", "content": "ответ 1", "tool_call_id": "call_1"}
    lost = {"role": "tool", "content": "ответ 2", "tool_call_id": "call_2"}

    class _DB:
        def get_messages(self, session_id, **kw):
            return [dict(erased), dict(lost)]

        def replace_messages(self, session_id, messages, **kw):
            written.append(messages)

    agent = session["agent"]
    agent._session_db = _DB()
    agent.session_id = "s-cmd"

    ok, why = _apply.commit_replacement(session, agent, [], removed=[erased])
    assert ok is False, "call_2 никто не удалял — это потеря, и она обязана отказать"
    assert written == []
    assert "call_2" in why or "ответ 2" in why, why


def test_c10e_erase_reports_the_rows_it_removed(one_live_session):
    """Отчёт механизма обязан НАЗЫВАТЬ удалённое — иначе предохранителю нечего пропускать."""
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "tool_name": "read_file",
         "content": "тело"},
    ]
    _out, report = _stubs.erase_tools(msgs, [{"tool_call_id": "call_1", "op": "0001"}])
    removed = report["removed"]
    # Обе половины пары: и tool-строка, и опустевшая оболочка хода.
    assert {m.get("role") for m in removed} == {"assistant", "tool"}
    assert any(m.get("tool_call_id") == "call_1" for m in removed)


def test_c10f_drop_content_declares_nothing_removed(one_live_session):
    """Механизм 1 строк не удаляет — значит и амнистии просить не должен."""
    body = chr(10).join(["# Size: 1 lines", "# ○○○", "тело"])
    msgs = [{"role": "tool", "tool_call_id": "call_1", "tool_name": "read_file",
             "content": body}]
    _out, report = _stubs.apply_stubs(msgs, [{"tool_call_id": "call_1", "op": "0001"}])
    assert not report.get("removed"), "заглушка не имеет права объявлять удаления"


# --- C11..C14: источник для записи — БАЗА, а не память гейтвея ------------------------
# Исправление 2026-09-09 после потери данных. Раньше в `replace_messages` уходил снимок
# `session["history"]`; он неполон и беднее строк базы, а примитив вставляет РОВНО
# переданное. Тесты ниже фиксируют оба следствия: ничего не теряется и ничего не беднеет.


def test_c11_the_command_writes_database_rows_not_the_memory_snapshot(one_live_session):
    """Записанное должно нести колонки базы: снимок памяти их не содержит вовсе."""
    _engine, session = one_live_session
    db = session["agent"]._session_db
    out = _command.handle("0156")
    assert "ИТОГ: применено" in out, out
    assert db.written is not None, "в базу не записали"
    assert len(db.written) == 4, "записан не полный активный набор"
    assert all(row.get("id") for row in db.written), (
        "у строк пропали id/timestamp/display_kind — значит писали снимок памяти гейтвея, "
        "и запись обнулила бы эти поля у КАЖДОЙ строки сессии, а не только у тронутых")
    tool_row = [r for r in db.written if r["role"] == "tool"][0]
    assert "State: removed_from_context" in tool_row["content"]


def test_c12_a_row_absent_from_gateway_memory_is_not_lost(one_live_session):
    """Воспроизведение реальной потери: строка есть в базе и нет в снимке памяти.

    2026-09-08 так ушло в rewind-архив чтение на 36 066 символов (`op 001f`): человек его
    не выбирал, а команда его не передала, потому что в снимке его не было.
    """
    _engine, session = one_live_session
    db = session["agent"]._session_db
    # Пара целиком, как было в жизни: вызов и его большой результат. Половина пары дала бы
    # честный отказ линта — он и должен так работать, но проверяем мы здесь другое.
    orphan = {"role": "tool", "tool_call_id": "call_orphan", "content": "x" * 3000,
              "id": 63006, "timestamp": 1500.0}
    db.rows.append({"role": "assistant", "content": "читаю большой", "id": 63005,
                    "timestamp": 1499.0, "tool_calls": [
                        {"id": "call_orphan", "type": "function",
                         "function": {"name": "read_file", "arguments": "{}"}}]})
    db.rows.append(orphan)
    session["history"].append({"role": "assistant", "content": "после"})

    out = _command.handle("0156")
    assert "ИТОГ: применено" in out, out
    written = {str(row.get("tool_call_id") or "") for row in db.written}
    assert "call_orphan" in written, (
        "строка, которой не было в памяти гейтвея, обязана уйти в запись как есть — "
        "иначе её архивирует `archive_dropped=True`, и человек потеряет невыбранное")
    kept = [r for r in db.written if r.get("tool_call_id") == "call_orphan"][0]
    assert kept["content"] == orphan["content"], "нетронутое не переписываем"


def test_c13_a_rewritten_row_loses_its_api_content_sidecar():
    """`api_content` — байтовая копия того, что ушло в API. У переписанной строки её быть
    не должно: иначе воспроизведение вернёт выброшенное в промпт мимо всей механики."""
    old = [{"role": "tool", "content": "дословно", "api_content": "дословно"},
           {"role": "user", "content": "не тронуто", "api_content": "не тронуто"}]
    new = [{"role": "tool", "content": "заглушка", "api_content": "дословно"},
           {"role": "user", "content": "не тронуто", "api_content": "не тронуто"}]
    out = _apply.drop_stale_api_content(old, new)
    assert "api_content" not in out[0]
    assert out[1]["api_content"] == "не тронуто", "нетронутую строку не обедняем"
    assert new[0]["api_content"] == "дословно", "вход не мутируем"


def test_c14_a_divergence_between_database_and_memory_is_named(one_live_session):
    """Промпт собирается из памяти. Если в ней операции нет — об этом надо сказать."""
    _engine, session = one_live_session
    session["history"][:] = [{"role": "user", "content": "только это"}]
    out = _command.handle("0156")
    assert "ИТОГ: применено" in out, out
    assert "расхождение" in out and "снимок памяти неполон" in out


# --- E: механизм 2, `erase tool` ------------------------------------------------------
#
# Единица — ПАРА (вызов + результат), а не весь мини-ход. Это исправление владельца
# 2026-09-09: я утверждал, что из «провайдер требует результат на каждый tool_call» следует
# «удалять можно только батч целиком». Не следует — следует лишь то, что удалять надо ОБЕ
# половины пары, переписав массив `tool_calls` у строки ассистента.


def _turn(call_ids, content="", args_by_id=None):
    """Мини-ход: одна строка ассистента с N вызовами и по одной tool-строке на каждый."""
    args_by_id = args_by_id or {}
    calls = [{"id": cid, "type": "function",
              "function": {"name": "read_file",
                           "arguments": args_by_id.get(cid, '{"path": "/w/x.py"}')}}
             for cid in call_ids]
    msgs = [{"role": "assistant", "content": content, "tool_calls": calls}]
    for cid in call_ids:
        msgs.append({"role": "tool", "tool_call_id": cid, "tool_name": "read_file",
                     "content": f"# Operation ID: {cid}\n# ○○○\nbody of {cid}"})
    return msgs


def test_e1_a_pair_goes_and_the_siblings_stay():
    """Удаляется ровно названная пара. Соседи по ходу остаются — батч тут не единица."""
    msgs = _turn(["c1", "c2", "c3"])
    out, report = _stubs.erase_tools(msgs, [{"tool_call_id": "c2", "op": "0002"}])

    assert [m["role"] for m in out] == ["assistant", "tool", "tool"]
    assert [c["id"] for c in out[0]["tool_calls"]] == ["c1", "c3"], "переписан не тот массив"
    assert [m["tool_call_id"] for m in out if m["role"] == "tool"] == ["c1", "c3"]
    assert report["problems"] == [], "пара не должна рассогласоваться"
    assert [r["op"] for r in report["applied"]] == ["0002"]


def test_e2_the_pair_is_gone_from_both_halves_not_stubbed():
    """След не остаётся ВОВСЕ — ни заголовка, ни штампа состояния (решение владельца)."""
    msgs = _turn(["c1"])
    out, _ = _stubs.erase_tools(msgs, [{"tool_call_id": "c1", "op": "0001"}])
    text = json.dumps(out, ensure_ascii=False)
    assert "c1" not in text and "Operation ID" not in text
    assert "removed_from_context" not in text, "это не заглушка, состояние ставить некуда"


def test_e3_an_emptied_shell_with_text_survives_without_the_key():
    """Слова модели — её собственные, человек выбирал операцию, а не реплику.

    И ключ убирается ЦЕЛИКОМ, не в `[]`: на половине памяти гейтвея нет writer'а, который
    превратил бы пустой список в NULL, и `tool_calls: []` уехал бы к провайдеру как есть.
    """
    msgs = _turn(["c1"], content="Сейчас прочитаю файл.")
    out, report = _stubs.erase_tools(msgs, [{"tool_call_id": "c1", "op": "0001"}])
    assert len(out) == 1 and out[0]["role"] == "assistant"
    assert out[0]["content"] == "Сейчас прочитаю файл."
    assert "tool_calls" not in out[0], "пустой массив вместо снятого ключа"
    assert len(report["shells_emptied"]) == 1
    assert report["shells_removed"] == []


def test_e4_an_emptied_shell_without_text_goes_too():
    """Ни вызовов, ни текста — это их же «otherwise-empty message», такой ход не отправить."""
    msgs = _turn(["c1"], content="")
    out, report = _stubs.erase_tools(msgs, [{"tool_call_id": "c1", "op": "0001"}])
    assert out == []
    assert len(report["shells_removed"]) == 1
    assert report["shells_emptied"] == []


def test_e4b_whitespace_only_text_is_not_text():
    msgs = _turn(["c1"], content="   \n  ")
    out, report = _stubs.erase_tools(msgs, [{"tool_call_id": "c1", "op": "0001"}])
    assert out == [] and len(report["shells_removed"]) == 1


def test_e5_savings_count_both_halves():
    """У записи всё содержимое лежит в аргументах — отчёт по одному ответу показал бы мало."""
    big = '{"path": "/w/x.py", "content": "' + "x" * 5000 + '"}'
    msgs = _turn(["c1"], args_by_id={"c1": big})
    _out, report = _stubs.erase_tools(msgs, [{"tool_call_id": "c1", "op": "0001"}])
    row = report["applied"][0]
    assert row["saved_args"] == len(big)
    assert row["saved_result"] > 0
    assert row["saved"] == row["saved_args"] + row["saved_result"]
    assert report["chars_saved"] == row["saved"]
    assert row["chars_after"] == 0, "после удаления не остаётся ничего"


def test_e6_an_unknown_address_is_reported_not_guessed():
    msgs = _turn(["c1"])
    out, report = _stubs.erase_tools(msgs, [{"tool_call_id": "нет-такого", "op": "0009"}])
    assert out == msgs, "ничего не тронуто"
    assert report["not_found"] == ["нет-такого"]
    assert report["applied"] == []


def test_e7_a_half_pair_is_finished_off_not_left_orphaned():
    """Результат без вызова (разрыв был ДО нас) — удаление его только починит."""
    msgs = [{"role": "tool", "tool_call_id": "c9", "tool_name": "read_file", "content": "тело"}]
    out, report = _stubs.erase_tools(msgs, [{"tool_call_id": "c9", "op": "0009"}])
    assert out == []
    assert report["problems"] == []
    assert [r["op"] for r in report["applied"]] == ["0009"]


def test_e8_a_lint_hole_that_predates_us_is_reported():
    """Мы дыр не создаём; если линт что-то нашёл — это было до нас, и молчать нельзя."""
    msgs = _turn(["c1", "c2"])
    del msgs[2]  # результат c2 пропал сам, до нас
    _out, report = _stubs.erase_tools(msgs, [{"tool_call_id": "c1", "op": "0001"}])
    assert report["problems"], "разрыв обязан быть назван"
    assert report["problems"][0]["tool_call_id"] == "c2"


def test_e9_erasing_every_call_of_a_multi_tool_turn_takes_the_shell():
    msgs = _turn(["c1", "c2"], content="")
    out, report = _stubs.erase_tools(
        msgs, [{"tool_call_id": "c1", "op": "0001"}, {"tool_call_id": "c2", "op": "0002"}])
    assert out == []
    assert sorted(report["shells_removed"][0]["calls"]) == ["c1", "c2"]
    assert len(report["applied"]) == 2


def test_e10_the_mechanism_is_named_in_the_report():
    _out, report = _stubs.erase_tools(_turn(["c1"]), [{"tool_call_id": "c1"}])
    assert report["mechanism"] == "tool"


def test_c15_the_api_content_check_survives_a_shortened_list():
    """Сверка сайдкара по АДРЕСУ, а не по позиции: `erase tool` укорачивает список.

    Была позиционная и держалась на том, что механизм 1 сохраняет длину. С удалением строк
    позиции съезжают, и сравнивались бы разные строки — то есть сайдкар мог остаться на
    переписанной строке, а это тихий возврат выброшенного в промпт.
    """
    old = [
        {"id": 1, "role": "tool", "tool_call_id": "c1", "content": "первый ответ"},
        {"id": 2, "role": "tool", "tool_call_id": "c2", "content": "второй ответ",
         "api_content": "байты второго"},
    ]
    # `c1` удалён целиком, `c2` не тронут: сайдкар обязан ОСТАТЬСЯ.
    new = [dict(old[1])]
    out = _apply.drop_stale_api_content(old, new)
    assert out[0].get("api_content") == "байты второго", (
        "нетронутая строка потеряла сайдкар — сверка съехала")

    # А если её же content переписан — сайдкар обязан УЙТИ, несмотря на смену позиции.
    rewritten = [{**old[1], "content": "заглушка"}]
    out = _apply.drop_stale_api_content(old, rewritten)
    assert "api_content" not in out[0], "переписанная строка сохранила байты прежнего текста"


def test_c15b_a_row_without_an_address_is_matched_by_its_text():
    """Снимок памяти гейтвея `id` не несёт вовсе — нетронутое там обеднять нельзя."""
    old = [{"role": "user", "content": "текст", "api_content": "байты"}]
    same = _apply.drop_stale_api_content(old, [dict(old[0])])
    assert same[0]["api_content"] == "байты", "нетронутая строка потеряла сайдкар"


def test_c15c_an_unmatchable_row_loses_its_sidecar():
    """Ни адреса, ни такого текста в прежнем наборе — чистим.

    Дешёвая ошибка (промах кеша) вместо дорогой (выброшенное молча вернулось в промпт).
    """
    out = _apply.drop_stale_api_content(
        [], [{"role": "user", "content": "текст", "api_content": "байты"}])
    assert "api_content" not in out[0]


def test_e11_erase_reports_an_address_with_no_row_as_absent():
    """Адрес нашёлся, строки нет — ДОКАЗАННОЕ отсутствие, и панель снимает по нему карточку."""
    msgs = [{"role": "tool", "tool_call_id": "c1", "tool_name": "read_file", "content": "тело"}]
    _out, report = _stubs.erase_tools(
        msgs, [{"tool_call_id": "c1", "op": "0001"},
               {"tool_call_id": "c-gone", "op": "0009"}])
    assert report["absent"] == ["0009"], report
    assert [r["op"] for r in report["applied"]] == ["0001"]


def test_e11b_an_item_without_an_op_is_not_reported_as_absent():
    """Снимать карточку нечем: без номера её не назвать. Пустых строк в списке не держим."""
    _out, report = _stubs.erase_tools([], [{"tool_call_id": "c-gone", "op": ""}])
    assert report["absent"] == []


def test_e11c_drop_content_never_reports_absent():
    """Механизм 1 карточек не снимает — значит и списка на снятие у него быть не должно."""
    _out, report = _stubs.apply_stubs([], [{"tool_call_id": "c-gone", "op": "0009"}])
    assert not report.get("absent")


# --- C16: два голоса — проза в чат, английская сводка в панель -------------------------
# Владелец, 2026-09-09, увидела на экране «ИТОГ применено - применено. освободилось ~567
# токенов»: и дубль, и русский там, где всё должно быть по-английски. Панель выскабливала себе
# подпись из строк команды, то есть её язык был привязан к языку чата.


def test_c16_the_verdict_line_does_not_repeat_itself(one_live_session, monkeypatch):
    """Коммит возвращает ровно слово «применено» — дописывать его второй раз незачем."""
    _engine, session = one_live_session
    monkeypatch.setattr(_apply, "commit_replacement",
                        lambda *a, **kw: (True, "применено"))
    out = _command.handle("0156")
    assert "ИТОГ: применено" in out, out
    assert "применено — применено" not in out


def test_c16b_a_verdict_with_a_real_reason_keeps_it(one_live_session, monkeypatch):
    """Причина, которая ДОБАВЛЯЕТ смысл, остаётся: без неё исход читался бы неверно."""
    _engine, session = one_live_session
    monkeypatch.setattr(_apply, "commit_replacement",
                        lambda *a, **kw: (True, "применено в БАЗЕ, но история изменилась"))
    out = _command.handle("0156")
    assert "но история изменилась" in out


def test_c16c_the_report_carries_an_english_summary_for_the_panel(one_live_session,
                                                                 monkeypatch):
    """Панели — короткая английская фраза ИЗ ФАКТОВ, а не пересказ русской прозы."""
    engine, _session = one_live_session
    monkeypatch.setattr(_apply, "commit_replacement", lambda *a, **kw: (True, "применено"))
    report = _command.run(engine._session_id, engine, ["0156"])
    assert report["ok"] is True
    summary = report["summary"]
    assert summary.startswith("dropped "), summary
    assert not any("а" <= ch.lower() <= "я" for ch in summary), (
        "в подписи для панели не должно быть ни одного русского слова")


def test_c16d_every_outcome_has_a_summary_and_it_is_english(one_live_session, monkeypatch):
    """Сводка обязана быть НА КАЖДОМ исходе — иначе панель опять начнёт что-то выдумывать."""
    engine, _session = one_live_session
    monkeypatch.setattr(_apply, "commit_replacement",
                        lambda *a, **kw: (False, "их линт отказал"))
    for ops, mode in ((["0156"], "content"), (["нет-такого"], "content"),
                      (["0156"], "tool"), (["0156"], "turn")):
        report = _command.run(engine._session_id, engine, ops, mode)
        summary = report.get("summary") or ""
        assert summary, (ops, mode)
        assert not any("а" <= ch.lower() <= "я" for ch in summary), (mode, summary)


# --- C17: надгробия пишутся при стирании (Plan13) --------------------------------------
# Стёртая операция не оставляет следа НИГДЕ, кроме журнала: строки в базе нет, карточка
# снята. Надгробие — единственное, чем панель может сказать «операция 0014 стёрта,
# тогда-то» вместо того, чтобы молча показать дырку в нумерации.


def test_c17_erase_writes_a_tombstone(one_live_session, monkeypatch):
    engine, _session = one_live_session
    monkeypatch.setattr(_apply, "commit_replacement", lambda *a, **kw: (True, "применено"))
    written = []
    monkeypatch.setattr(_stubs, "note_erased",
                        lambda session, ops, by="erase tool": written.append((session, list(ops), by))
                        or len(list(ops)))
    report = _command.run(engine._session_id, engine, ["0156"], "tool")
    assert report["ok"] is True
    assert written == [("s-cmd", ["0156"], "erase tool")], written


def test_c17b_drop_content_leaves_no_tombstone(one_live_session, monkeypatch):
    """У `drop content` операция в контексте ОСТАЛАСЬ — надгробие над ней соврало бы."""
    engine, _session = one_live_session
    monkeypatch.setattr(_apply, "commit_replacement", lambda *a, **kw: (True, "применено"))
    calls = []
    monkeypatch.setattr(_stubs, "note_erased",
                        lambda *a, **kw: calls.append(a) or 0)
    _command.run(engine._session_id, engine, ["0156"], "content")
    assert calls == []


def test_c17c_a_refused_commit_writes_no_tombstone(one_live_session, monkeypatch):
    """Свидетельство о стирании, которого не было, — хуже отсутствия свидетельства."""
    engine, _session = one_live_session
    monkeypatch.setattr(_apply, "commit_replacement", lambda *a, **kw: (False, "их линт отказал"))
    calls = []
    monkeypatch.setattr(_stubs, "note_erased", lambda *a, **kw: calls.append(a) or 0)
    report = _command.run(engine._session_id, engine, ["0156"], "tool")
    assert report["ok"] is False
    assert calls == []


def test_c17d_an_unwritable_ledger_does_not_undo_the_erase(one_live_session, monkeypatch):
    """ПРАВИЛО: применение уже состоялось и необратимо. Журнал ему не судья."""
    engine, _session = one_live_session
    monkeypatch.setattr(_apply, "commit_replacement", lambda *a, **kw: (True, "применено"))

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr("hermes_filetools.file_ops._ledger.note_erased", boom)
    report = _command.run(engine._session_id, engine, ["0156"], "tool")
    assert report["ok"] is True, "надгробие — свидетельство, а не сама операция"
    assert report["tombstones"] == 0


def test_c18_a_message_record_does_not_shadow_the_operation_in_the_index(tmp_path, monkeypatch):
    """`ledger_index` берёт последнюю запись на `tool_call_id`, а запись о
    сообщении несёт ТОТ ЖЕ идентификатор. Не отсеки мы её — заглушка потеряла бы
    путь и диапазон, ровно как `/reads` потерял состояния (2026-09-09)."""
    import hermes_constants
    from hermes_filetools.context_ctl import _stubs
    from hermes_filetools.file_ops import _ledger
    token = hermes_constants.set_hermes_home_override(tmp_path)
    try:
        monkeypatch.setattr(_ledger, "_observability_context",
                            lambda: ("s18", "c1", ""))
        _ledger.note_read(text="a=1", path="/w/x.py", session="s18", op="0001")
        _ledger.note_message(session="s18", msg="m0001", role="tool", msg_ts=1.0,
                             sha256="a" * 64, tool_call_id="c1")
        index = _stubs.ledger_index("s18")
    finally:
        hermes_constants.reset_hermes_home_override(token)
    assert index["c1"].get("path") == "/w/x.py"
    assert index["c1"].get("event") is None
    assert index["op:0001"].get("path") == "/w/x.py"


# --------------------------------------------------------------------------
# T1-T4 — механизм 3, `erase turn` (Plan14-Task06)
#
# Шаг, а не полный turn: реплика ассистента со ВСЕМИ её вызовами и ответами.
# Полный turn в их смысле кончается возвратом управления человеку — это другое,
# и владелец имеет в виду именно шаг (поправка 1 к контракту записей).
# --------------------------------------------------------------------------

def step_history(*call_ids, prose="думаю", tail=True):
    calls = [{"id": cid, "type": "function",
              "function": {"name": "read_file",
                           "arguments": json.dumps({"path": f"/x/{cid}.py"})}}
             for cid in call_ids]
    out = [{"role": "user", "content": "сделай"},
           {"role": "assistant", "content": prose, "tool_calls": calls}]
    out += [{"role": "tool", "tool_call_id": cid, "content": f"ответ {cid}"}
            for cid in call_ids]
    if tail:
        out.append({"role": "assistant", "content": "готово"})
    return out


def test_t1_one_named_call_takes_the_whole_step():
    """Отметка на ОДНОЙ операции уносит весь шаг — решение владельца."""
    msgs = step_history("c1", "c2", "c3")
    out, report = _stubs.erase_turn(msgs, [{"tool_call_id": "c2", "op": "0002"}])
    assert [m["role"] for m in out] == ["user", "assistant"]
    assert out[-1]["content"] == "готово", "соседний шаг не тронут"
    assert sorted(report["widened"]) == ["c1", "c3"]
    assert report["mechanism"] == "turn"
    assert report["problems"] == []


def test_t2_the_prose_of_the_step_goes_with_it():
    """У `erase tool` слова модели остаются: человек выбрал операцию. Здесь он
    выбрал ход, а проза шага — его часть."""
    msgs = step_history("c1", prose="сейчас посмотрю файл")
    out, report = _stubs.erase_turn(msgs, [{"tool_call_id": "c1"}])
    assert not any(m.get("content") == "сейчас посмотрю файл" for m in out)
    assert report["prose_chars"] == len("сейчас посмотрю файл")
    assert report["shells_emptied"] == []
    assert report["shells_removed"] and report["shells_removed"][0]["prose_chars"] > 0


def test_t2b_erase_tool_still_keeps_the_prose():
    """Проверка, что флаг не поменял поведение механизма 2 заодно."""
    msgs = step_history("c1", prose="сейчас посмотрю файл")
    out, report = _stubs.erase_tools(msgs, [{"tool_call_id": "c1"}])
    assert any(m.get("content") == "сейчас посмотрю файл" for m in out)
    assert report["prose_chars"] == 0
    assert report["shells_emptied"] and not report["shells_removed"]


def test_t3_a_neighbouring_step_is_not_widened_into():
    """Расширение идёт по `tool_calls` СВОЕЙ строки, а не по соседству."""
    msgs = step_history("c1", tail=False) + step_history("c9", tail=False)[1:]
    out, report = _stubs.erase_turn(msgs, [{"tool_call_id": "c1"}])
    assert report["widened"] == []
    assert any(m.get("tool_call_id") == "c9" for m in out)
    assert report["problems"] == []


def test_t4_the_removed_rows_are_named_for_the_commit_guard():
    """Предохранитель коммита сверяет наш список с активными строками и отказывает
    при нехватке. Удаление СОЗДАЁТ нехватку, и отличить его от потери можно ровно
    одним способом — назвать удалённое явно."""
    msgs = step_history("c1", "c2")
    out, report = _stubs.erase_turn(msgs, [{"tool_call_id": "c1"}])
    removed = report["removed"]
    assert len(removed) == 3, "две тул-строки и оболочка шага"
    assert all(m not in out for m in removed)


def test_t4b_nothing_named_nothing_erased():
    msgs = step_history("c1")
    out, report = _stubs.erase_turn(msgs, [])
    assert out == msgs and report["applied"] == [] and report["widened"] == []


# ── O1..O7: операции над m-АДРЕСАМИ (Plan16-Task08, `_command.run_addresses`) ──────────────
#
# Лента фикстуры: m0001 user · m0002 assistant «читаю» + call_1 · m0003 tool call_1 ·
# m0004 assistant «прочитал». Адреса подставляются индексом по порядку строк.

@pytest.fixture
def by_address(monkeypatch):
    from hermes_filetools.context_ctl import _compact
    monkeypatch.setattr(_compact, "_index",
                        lambda session_id, rows: {f"m{i + 1:04d}": row for i, row in enumerate(rows)})
    monkeypatch.setattr(_stubs, "index_messages", lambda session_id, rows: {})


def test_o1_drop_content_by_address_reaches_our_result(one_live_session, by_address):
    engine, _session = one_live_session
    report = _command.run_addresses(engine._session_id, engine, ["m0003"], "content")
    assert report["ok"] is True, report["text"]
    written = engine._agent._session_db.written
    tool = [m for m in written if m["role"] == "tool"][0]
    assert "removed_from_context" in tool["content"]


def test_o2_drop_content_reaches_a_FOREIGN_result_and_names_the_tool(one_live_session, by_address,
                                                                    monkeypatch):
    """Чужой результат: номера операции нет, конверта нет — заглушка с именем тула и размером."""
    engine, _session = one_live_session
    monkeypatch.setattr(_stubs, "ledger_index", lambda s: {})
    engine._agent._session_db = _FakeDB(db_rows(content="голый чужой ответ без конверта"))
    report = _command.run_addresses(engine._session_id, engine, ["m0003"], "content")
    assert report["ok"] is True, report["text"]
    tool = [m for m in engine._agent._session_db.written if m["role"] == "tool"][0]
    assert "Tool: read_file" in tool["content"]
    assert "Size: " in tool["content"]
    assert "removed_from_context" in tool["content"]


def test_o3_drop_content_on_a_non_result_touches_nothing(one_live_session, by_address):
    engine, _session = one_live_session
    report = _command.run_addresses(engine._session_id, engine, ["m0001"], "content")
    assert report["ok"] is False
    assert engine._agent._session_db.written is None


def test_o4_erase_a_result_takes_its_call_and_writes_a_tombstone(one_live_session, by_address,
                                                                monkeypatch):
    engine, _session = one_live_session
    written = []
    monkeypatch.setattr(_stubs, "note_erased",
                        lambda session, ops, by="erase tool": written.append((list(ops), by)) or 0)
    report = _command.run_addresses(engine._session_id, engine, ["m0003"], "erase")
    assert report["ok"] is True, report["text"]
    rows = engine._agent._session_db.written
    # С 2026-09-30 на новом стыке двух ответов ассистента — мостик от человека (`_alternation`).
    assert [m["role"] for m in rows] == ["user", "assistant", "user", "assistant"]
    assert _alternation.is_bridge(rows[2]) and "мостиков 1" in report["text"]
    carrier = rows[1]
    assert carrier["content"] == "читаю", "проза реплики — её судьба, она не выбрана"
    assert not carrier.get("tool_calls"), "вызов ушёл вместе со своим результатом"
    assert written == [(["0156"], "erase")]


def test_o5_erase_a_user_message(one_live_session, by_address):
    engine, _session = one_live_session
    report = _command.run_addresses(engine._session_id, engine, ["m0001"], "erase")
    assert report["ok"] is True, report["text"]
    rows = engine._agent._session_db.written
    # Лента больше не начинается с ответа ассистента: мостик «conversation start removed».
    assert [m["role"] for m in rows] == ["user", "assistant", "tool", "assistant"]
    assert rows[0]["content"] == _alternation.ERASE_TEXT["start"]


def test_o6_erase_prose_keeps_a_hull_while_its_call_lives(one_live_session, by_address):
    """Правило владения: проза уходит, вызов живёт, пока жив его результат."""
    engine, _session = one_live_session
    report = _command.run_addresses(engine._session_id, engine, ["m0002"], "erase")
    assert report["ok"] is True, report["text"]
    rows = engine._agent._session_db.written
    assert [m["role"] for m in rows] == ["user", "assistant", "tool", "assistant"]
    assert not str(rows[1].get("content") or "").strip()
    assert rows[1].get("tool_calls"), "вызов остался — его результат не выбран"


def test_o7_an_unknown_operation_is_refused_by_name(one_live_session, by_address):
    engine, _session = one_live_session
    report = _command.run_addresses(engine._session_id, engine, ["m0001"], "shred")
    assert report["ok"] is False
    assert "shred" in report["summary"]


# ── W1..W5: `paste` — замена текста сообщения из буфера (Vision09, `_command.run_edit`) ─────

def test_w1_paste_replaces_only_the_text_in_place(one_live_session, by_address):
    """Правило 1: из буфера только `content`; правило 2: строка встаёт НА МЕСТО старой."""
    engine, _session = one_live_session
    report = _command.run_edit(engine._session_id, engine, "m0002", "читаю внимательно")
    assert report["ok"] is True, report["text"]
    rows = engine._agent._session_db.written
    assert [m["role"] for m in rows] == ["user", "assistant", "tool", "assistant"]
    edited = rows[1]
    assert edited["content"] == "читаю внимательно"
    assert edited.get("tool_calls"), "вызовы реплики переносятся байт в байт"
    assert "api_content" not in edited and "id" not in edited


def test_w1b_paste_gives_the_row_a_new_time_so_its_address_changes(one_live_session, by_address):
    engine, _session = one_live_session
    old = engine._agent._session_db.get_messages(engine._session_id)[1]
    _command.run_edit(engine._session_id, engine, "m0002", "другое")
    edited = engine._agent._session_db.written[1]
    assert edited["timestamp"] != old.get("timestamp")


def test_w2_tool_rows_are_not_editable(one_live_session, by_address):
    engine, _session = one_live_session
    report = _command.run_edit(engine._session_id, engine, "m0003", "подделка")
    assert report["ok"] is False
    assert engine._agent._session_db.written is None


def test_w3_same_text_and_empty_clipboard_touch_nothing(one_live_session, by_address):
    engine, _session = one_live_session
    assert _command.run_edit(engine._session_id, engine, "m0002", "читаю")["ok"] is False
    assert _command.run_edit(engine._session_id, engine, "m0002", "   ")["ok"] is False
    assert engine._agent._session_db.written is None


def test_w4_unknown_address_is_named(one_live_session, by_address):
    engine, _session = one_live_session
    report = _command.run_edit(engine._session_id, engine, "m0099", "x")
    assert report["ok"] is False
    assert "m0099" in report["summary"]


def test_w5_ledger_edited_record_shape(tmp_path, monkeypatch):
    from hermes_filetools.file_ops import _ledger
    monkeypatch.setattr(_ledger, "ledger_path", lambda s: tmp_path / "ledger.jsonl")
    assert _ledger.note_edited("s1", "m0002", "m0021") is True
    import json as _json
    rec = _json.loads((tmp_path / "ledger.jsonl").read_text(encoding="utf-8").strip())
    assert {k: rec[k] for k in ("event", "session", "was", "now", "by")} == {
        "event": "edited", "session": "s1", "was": "m0002", "now": "m0021", "by": "human"}
    assert _ledger.note_edited("s1", "", "m0021") is False
