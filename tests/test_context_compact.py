"""Компакция — `Plan16`, механизм и его правила отбора.

Что защищается — не «функция что-то вернула», а те инварианты, нарушение которых портит
ЖИВУЮ сессию и делает это тихо:

  K1  адреса ШЕСТНАДЦАТЕРИЧНЫЕ; мусор НАЗВАН, а не проглочен
  K2  «проза» — СЛОВА: вызов реплику модели не дисквалифицирует, срезается при отправке
  K3  съедается весь пробег; дырка в ВЫБОРЕ режет пробег; выбор человека не обсуждается
  K4  роль сводки никогда не совпадает с ролью соседа — иначе провайдер не примет ход
  K5  сводка встаёт на место ПЕРВОГО съеденного, вход не мутируется
  K6  сводка уезжает в рамке: шапка «это справка» + маркер конца, обе половины
  K7  запись о поглощении — событие, видное своему читателю и невидимое чужим
  K8  без аргументов модель НЕ зовётся и база НЕ трогается (правка необратима)
"""
from __future__ import annotations

import pytest

from hermes_filetools.context_ctl import _compact, _prompts
from hermes_filetools.file_ops import _ledger


# ── K1 ───────────────────────────────────────────────────────────────────────

def test_k1_ranges_and_single_numbers_parse_to_the_same_number_set():
    wanted, bad = _compact.parse_spec("m0001-m0003, m0007, 12")
    assert wanted == {1, 2, 3, 7, 0x12}
    assert bad == []


def test_k1_addresses_are_hexadecimal_like_the_ledger_writes_them():
    """`_ledger.next_msg` форматирует `m{n:04x}` — рядом с `m0009` живёт `m000a`.

    Разбор десятичным был РЕАЛЬНЫМ багом (2026-09-16): он не падал, а молча выбрасывал
    каждый адрес с буквой — из 235 адресов сессии разбиралось 99, и человек получал бы
    сводку не по тому диапазону, который назвал.
    """
    wanted, bad = _compact.parse_spec("m000a, m00eb")
    assert wanted == {10, 235}
    assert bad == []
    # И диапазон считается по той же базе: m0001-m0041 это 65 сообщений, не 41.
    wanted, _bad = _compact.parse_spec("m0001-m0041")
    assert len(wanted) == 65
    assert _compact._addr(63) == "m003f"


def test_k1b_a_reversed_range_is_read_as_a_range_not_as_an_error():
    # `m0010-m0001` — описка, а не намерение; отказывать за неё значило бы заставить
    # человека печатать заново то, что и так однозначно.
    wanted, bad = _compact.parse_spec("m0010-m000e")
    assert wanted == {0x0e, 0x0f, 0x10}
    assert bad == []


def test_k1c_garbage_is_reported_and_does_not_cancel_the_rest():
    wanted, bad = _compact.parse_spec("m0002, ой, m0004")
    assert wanted == {2, 4}
    assert bad == ["ой"]


# ── K2 ───────────────────────────────────────────────────────────────────────

def test_k2_prose_is_the_words_of_both_sides():
    assert _compact.is_prose({"role": "user", "content": "вопрос"})
    assert _compact.is_prose({"role": "assistant", "content": "ответ"})


def test_k2_a_tool_call_does_not_disqualify_the_models_own_words():
    """РЕАЛЬНЫЙ баг, пойманный живьём 2026-09-16.

    Первая версия выбрасывала реплику ассистента целиком, если у неё есть `tool_calls`. В
    агентской сессии так устроена почти каждая реплика модели — и в суммаризатор из
    шестнадцати сообщений уехало ДВА, оба человеческих. Сводка получилась по двум строкам
    вместо всего разговора, и никакой ошибки при этом не возникло.
    """
    assert _compact.is_prose({"role": "assistant", "content": "сейчас прочитаю",
                              "tool_calls": [{"id": "c1"}]})


@pytest.mark.parametrize("row", [
    {"role": "tool", "tool_call_id": "c1", "content": "результат"},
    {"role": "assistant", "content": "   ", "tool_calls": [{"id": "c1"}]},
    {"role": "system", "content": "правила"},
])
def test_k2b_everything_else_is_not_prose(row):
    assert not _compact.is_prose(row)


# ── K3 ───────────────────────────────────────────────────────────────────────

def _rows_and_index():
    """Четыре реплики, между ними живая пара вызов/ответ. Адреса как у индексатора."""
    rows = [
        {"role": "user", "content": "раз", "timestamp": 1.0},              # m0001
        {"role": "assistant", "content": "два", "timestamp": 2.0},          # m0002
        {"role": "assistant", "content": "зову", "timestamp": 3.0,
         "tool_calls": [{"id": "c1", "function": {"name": "read_file",
                                                  "arguments": "{}"}}]},    # m0003
        {"role": "tool", "tool_call_id": "c1", "content": "итог",
         "timestamp": 4.0},                                                 # m0004
        {"role": "assistant", "content": "три", "timestamp": 5.0},          # m0005
        {"role": "user", "content": "нетронутое", "timestamp": 6.0},        # m0006
        {"role": "assistant", "content": "четыре", "timestamp": 7.0},       # m0007
    ]
    by_msg = {f"m{i + 1:04d}": row for i, row in enumerate(rows)}
    return rows, by_msg


def test_k3_a_gap_in_the_selection_splits_the_run():
    rows, by_msg = _rows_and_index()
    # m0006 НЕ выбран — значит m0007 отдельный пробег, а не хвост первого.
    runs, skipped, missing = _compact.build_runs(rows, by_msg, {1, 2, 5, 7})
    assert [r["addresses"] for r in runs] == [["m0001", "m0002"], ["m0005"], ["m0007"]]
    assert missing == []


def test_k3b_the_whole_run_is_eaten_but_only_prose_reaches_the_model():
    """Главное правило, и оно ДВОЙНОЕ — их легко перепутать.

    Съедается весь пробег (иначе контекст не уменьшается: в живой сессии проза это
    единицы строк из сотен), а в модель уходит только проза (вызовы и ответы
    суммаризатору не показываем).
    """
    rows, by_msg = _rows_and_index()
    runs, skipped, _missing = _compact.build_runs(rows, by_msg, {1, 2, 3, 4, 5})
    assert len(runs) == 1
    run = runs[0]
    assert run["addresses"] == ["m0001", "m0002", "m0003", "m0004", "m0005"]
    assert run["eaten"] == [0, 1, 2, 3, 4]          # съедается ВСЁ
    # В модель — все СЛОВА, включая реплику модели, которая несёт вызов (позиция 2).
    assert run["prose"] == [0, 1, 2, 4]
    assert run["mute"] == [3]                       # только строка-ответ инструмента
    assert skipped == []


def test_k3c_an_address_that_does_not_exist_is_named_not_ignored():
    rows, by_msg = _rows_and_index()
    runs, _skipped, missing = _compact.build_runs(rows, by_msg, {1, 99})
    assert missing == [99]
    assert [r["addresses"] for r in runs] == [["m0001"]]


def test_k3d_a_selection_without_prose_is_compacted_like_any_other():
    """ПЕРЕВЁРНУТ 2026-09-22. Здесь стоял отказ: пробег без единой реплики не трогался.

    Правила такого никто не вводил — оно появилось в шапке модуля без чьего-либо решения, в
    отличие от соседних, у которых стоят даты и слова владельца, и трижды выдавалось за её
    норму. Её слова, когда это вскрылось: «пользователь нажал компакцию на 1 элемент? команда
    пошла и выполнила эту задачу, а не выдумала, что она не может это делать».

    Довод по существу, а не только по полномочиям: одинокий результат — это, например,
    чтение большого файла, и сжать его ровно потому, что он большой, — самое частое желание
    из всех. Модель видит и вызовы, и результаты (`_render`), так что сводке есть из чего
    взяться.
    """
    rows, by_msg = _rows_and_index()
    runs, skipped, _missing = _compact.build_runs(rows, by_msg, {4})
    assert skipped == [], "выбор человека не обсуждается"
    assert [r["addresses"] for r in runs] == [["m0004"]]
    assert runs[0]["prose"] == [] and runs[0]["mute"] == [3]


# ── K4 ───────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("before,after", [
    ("assistant", "user"), ("user", "assistant"), ("tool", "user"),
    ("assistant", "assistant"), ("user", "user"), ("", ""),
])
def test_k4_the_summary_role_never_equals_a_neighbour(before, after):
    role = _compact.summary_role({"role": before} if before else None,
                                 {"role": after} if after else None)
    assert role in ("user", "assistant")
    assert role != after


# ── K5 / K6 ──────────────────────────────────────────────────────────────────

def test_k5_the_summary_lands_where_the_first_eaten_row_was():
    rows, by_msg = _rows_and_index()
    runs, _skipped, _missing = _compact.build_runs(rows, by_msg, {1, 2, 3, 4, 5})
    answers = [{"text": "СВОДКА"}]
    new_rows, new_history, eaten = _compact._rebuild(rows, list(rows), runs, answers)

    # Сводка ПЕРВАЯ: её пробег начинался с m0001, то есть с самого начала списка.
    assert _prompts.is_distillate(new_rows[0]["content"])
    # Съеден ВЕСЬ пробег, включая пару вызов/ответ; за сводкой остался только хвост.
    assert [r.get("content") for r in new_rows[1:]] == ["нетронутое", "четыре"]
    assert len(eaten) == 5


def test_k5b_the_input_lists_are_not_mutated():
    rows, by_msg = _rows_and_index()
    before = [dict(r) for r in rows]
    runs, _s, _m = _compact.build_runs(rows, by_msg, {1, 2})
    _compact._rebuild(rows, list(rows), runs, [{"text": "СВОДКА"}])
    assert rows == before


def test_k5c_two_runs_get_two_summaries_with_distinct_coordinates():
    rows, by_msg = _rows_and_index()
    runs, _s, _m = _compact.build_runs(rows, by_msg, {1, 2, 5, 7})
    answers = [{"text": "A"}, {"text": "B"}, {"text": "C"}]
    new_rows, _hist, _eaten = _compact._rebuild(rows, list(rows), runs, answers)
    stamps = [r["timestamp"] for r in new_rows
              if _prompts.is_distillate(str(r.get("content", "")))]
    assert len(stamps) == 3
    # Координата `(роль, время)` — личность сообщения: совпадение стоило бы адреса.
    assert len(set(stamps)) == 3


def test_k6_the_summary_travels_inside_both_halves_of_a_frame():
    """Рамка одного голоса с текстом: наш дистиллят написан первым лицом, их сводку пишет
    посторонний. Поэтому рамку выбирает тот же переключатель, что и мозг."""
    wrapped = _compact._wrap("тело сводки")
    assert _prompts.is_distillate(wrapped)
    assert wrapped.rstrip().endswith(_prompts.distill_end(False))
    assert "тело сводки" in wrapped


# ── K7 ───────────────────────────────────────────────────────────────────────

def test_k7_an_absorption_record_is_an_event_its_reader_sees(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_FILETOOLS_LEDGER_DIR", str(tmp_path))
    monkeypatch.setattr(_ledger, "ledger_dir", lambda: tmp_path)
    assert _ledger.note_absorbed("s-1", "m0015", ["m0002", "m0003"]) is True

    entries = _ledger.read_ledger("s-1")
    assert [_ledger.is_event(e) for e in entries] == [True]
    assert _ledger.absorptions(entries)["m0015"]["members"] == ["m0002", "m0003"]
    # ...и невидима читателю надгробий: роды записей не путаются между собой.
    assert _ledger.erased_ops(entries) == {}


def test_k7b_an_absorption_without_members_is_not_written(tmp_path, monkeypatch):
    monkeypatch.setattr(_ledger, "ledger_dir", lambda: tmp_path)
    # «Сводка не поглотила ничего» — не факт, а пустая запись; писать её значило бы
    # засорить журнал свидетельством ни о чём.
    assert _ledger.note_absorbed("s-2", "m0015", []) is False
    assert _ledger.read_ledger("s-2") == []


# ── K8 ───────────────────────────────────────────────────────────────────────

def test_k8_no_arguments_means_preview_the_model_is_not_called(monkeypatch):
    """Правка необратима, поэтому «без аргументов» обязано быть безопасным.

    Сторожим ОБА следствия сразу: модель не позвали и коммит не трогали. Проверять только
    `ok is False` было бы недостаточно — отчёт об отказе тоже даёт `ok: False`, уже потратив
    вызов модели.
    """
    from hermes_filetools.context_ctl import _apply, _llm

    called = {"model": 0, "commit": 0}
    monkeypatch.setattr(_llm, "available", lambda: (True, "тест"))
    monkeypatch.setattr(_llm, "ask", lambda *a, **kw: called.__setitem__("model", 1))
    monkeypatch.setattr(_apply, "commit_replacement",
                        lambda *a, **kw: called.__setitem__("commit", 1) or (True, ""))

    rows, by_msg = _rows_and_index()
    monkeypatch.setattr(_apply, "find_gateway_session", lambda agent: ({}, "тестовая"))
    monkeypatch.setattr(_apply, "db_history", lambda agent: (rows, ""))
    monkeypatch.setattr(_compact, "_index", lambda session, rows: by_msg)

    class _Engine:
        def live_agent(self):
            return object()

    facts = _compact.run("s-1", _Engine(), "")
    assert facts["ok"] is False
    assert called == {"model": 0, "commit": 0}
    assert any("ПРЕДПРОСМОТР" in line for line in facts["lines"])
    # Предпросмотр обязан ПОКАЗАТЬ работу, а не просто отказаться: пробеги посчитаны.
    assert facts["runs"] > 0


# ── B: `start_background` (Plan16-Task03, панель) ────────────────────────────────────

class _FakeThreadModule:
    """Заменяет `threading` целиком ВНУТРИ `_compact`'s собственного пространства имён
    (не сам модуль `threading` — тот общий на весь процесс) — поток становится синхронным,
    тест не гоняется за таймингом фонового прогона."""

    class Thread:
        def __init__(self, target=None, args=(), daemon=None, name=None):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)


def test_b1_start_background_refuses_with_nothing_to_compact():
    out = _compact.start_background("s-1", object(), "")
    assert out["ok"] is False and "no message is ticked" in out["error"]


def test_b2_start_background_refuses_a_second_run_while_one_is_locked(monkeypatch):
    # Замок ЗАНЯТ значит «`acquire` не даётся», а не «`locked()` вернул True»: с 2026-09-22
    # `start_background` сам захватывает, а не спрашивает (`__HQ/deferred/03`).
    monkeypatch.setattr(_compact, "_busy",
                        type("L", (), {"acquire": lambda self, blocking=True: False})())
    out = _compact.start_background("s-1", object(), "m0001")
    assert out["ok"] is False and "уже идёт" in out["error"]


def test_b2b_the_lock_is_taken_where_the_decision_is_made_not_later_in_the_thread(monkeypatch):
    """`__HQ/deferred/03`: проверка замка и его захват были РАЗНЕСЕНЫ.

    `start_background` спрашивал `_busy.locked()`, а брал замок `run()` — уже в потоке.
    Между «посмотрели» и «взяли» помещался второй вызов: оба видели свободно, оба заводили
    поток. Данные не страдали (второй прогон честно отказывался), но статус врал дважды:
    очистка `_state_now` затирала прогресс ПЕРВОГО, живого прогона, а вызывающий получал
    `ok: True` на то, что через миллисекунды откажется. Для сабагента в закрытой сессии это
    значило досидеть до таймаута, не увидев ни отказа, ни отчёта.

    Здесь поток НЕ запускается вовсе — значит замок, если он взят, взят именно тем кодом,
    который решает запускаться.
    """
    started = []
    monkeypatch.setattr(_compact, "threading", type("T", (), {
        "Thread": lambda **kw: type("H", (), {"start": lambda self: started.append(1)})()}))

    first = _compact.start_background("s-1", object(), "m0001")
    try:
        assert first["ok"] is True, "первый обязан пройти"
        second = _compact.start_background("s-1", object(), "m0002")
        assert second["ok"] is False, "второй получил бы «запущено» на то, что откажется"
        assert "уже идёт" in second["error"]
        assert _compact._state_now["session"] == "s-1", "прогресс первого не затёрт вторым"
    finally:
        _compact._busy_since = 0.0
        _compact._busy.release()


def test_b3_start_background_answers_immediately_and_persists_the_outcome(tmp_path, monkeypatch):
    """`start_background` отвечает `{"ok": True}` ДО конца прогона — сам итог обязан лечь
    и в `_state_now` (слеш-команда, тот же процесс), и в файл (панель, другой процесс)."""
    from hermes_filetools.context_ctl import _compact_state

    monkeypatch.setattr(_compact_state, "state_dir", lambda: tmp_path / "compact_state")
    monkeypatch.setattr(_compact, "threading", _FakeThreadModule)
    monkeypatch.setattr(_compact, "run",
                        lambda sid, engine, args, owned=False:
                            {"ok": True, "lines": ["ИТОГ: применено"]})

    out = _compact.start_background("s-1", object(), "m0001,m0002")
    assert out == {"ok": True, "session": "s-1"}

    assert _compact._state_now["ok"] is True
    assert _compact._state_now["running"] is False

    state = _compact_state.read_state("s-1")
    assert state["running"] is False and state["ok"] is True
    assert state["lines"] == ["ИТОГ: применено"]


def test_b4_a_worker_that_raises_still_ends_running_and_reports_the_error(tmp_path, monkeypatch):
    from hermes_filetools.context_ctl import _compact_state

    monkeypatch.setattr(_compact_state, "state_dir", lambda: tmp_path / "compact_state")
    monkeypatch.setattr(_compact, "threading", _FakeThreadModule)

    def _boom(sid, engine, args, owned=False):
        raise RuntimeError("шов пропал")
    monkeypatch.setattr(_compact, "run", _boom)

    _compact.start_background("s-2", object(), "m0001")
    state = _compact_state.read_state("s-2")
    assert state["running"] is False
    assert "шов пропал" in state.get("error", "") or "шов пропал" in _compact._state_now.get("error", "")





# ── E: режим автомата, путь B (Plan16 п.1) ───────────────────────────────────────────

def test_e1_deliver_mode_hands_the_tape_back_and_never_writes(monkeypatch):
    """Автомат: лента собирается из ленты ХОДА, в базу не пишем — её запишет Hermes."""
    from hermes_filetools.context_ctl import _apply, _llm

    called = {"commit": 0}
    monkeypatch.setattr(_llm, "available", lambda: (True, "тест"))
    monkeypatch.setattr(_apply, "commit_replacement",
                        lambda *a, **kw: called.__setitem__("commit", 1) or (True, ""))
    rows, by_msg = _rows_and_index()
    # Сессии гейтвея нет — в режиме автомата она и не нужна.
    monkeypatch.setattr(_apply, "find_gateway_session", lambda agent: (None, "нет гейтвея"))
    monkeypatch.setattr(_apply, "db_history", lambda agent: (rows, ""))
    monkeypatch.setattr(_compact, "_index", lambda session, rows: by_msg)
    monkeypatch.setattr(_compact, "distill_summary",
                        lambda *a, **kw: {"ok": True, "text": "СВОДКА", "input_chars": 10,
                                          "provider": "p", "model": "m", "tokens": 1})

    class _Engine:
        def live_agent(self):
            return object()

    turn = [dict(r) for r in rows]
    facts = _compact.run("s-1", _Engine(), "m0001-m0002", deliver={"history": turn})
    assert called["commit"] == 0, "в режиме автомата мы не пишем"
    assert facts.get("delivered") is True and facts["ok"] is False
    new = facts["new_history"]
    assert len(new) < len(turn)
    assert any("СВОДКА" in str(m.get("content")) for m in new)
    assert facts["finalize"]["spec"] == "m0001-m0002"



def test_e2_the_tape_is_spliced_from_the_db_and_the_turn_tail(monkeypatch):
    """Живьём 2026-09-29: Hermes заранее склеил в ленте хода голову со старым дистиллятом, место
    для нового дистиллята по строке базы не нашлось — и он молча выпал. Теперь блок до хвоста —
    из сборки по БАЗЕ, хвост — из ленты хода (с несохранённым), дистиллят проверяется."""
    from hermes_filetools.context_ctl import _alternation, _apply, _llm

    rows = [
        {"role": "user", "content": "раз", "timestamp": 1.0},                    # m0001
        {"role": "assistant", "content": "голова", "timestamp": 2.0},            # m0002
        {"role": "assistant", "content": "старый дистиллят", "timestamp": 3.0},  # m0003
        {"role": "user", "content": "вопрос", "timestamp": 4.0},                 # m0004
        {"role": "assistant", "content": "ответ", "timestamp": 5.0},             # m0005
        {"role": "user", "content": "хвост", "timestamp": 6.0},                  # m0006
        {"role": "assistant", "content": "хвост-ответ", "timestamp": 7.0},       # m0007
    ]
    by_msg = {f"m{i + 1:04d}": row for i, row in enumerate(rows)}
    turn = [dict(rows[0]), {"role": "assistant", "content": "голова\nстарый дистиллят"},
            dict(rows[3]), dict(rows[4]), dict(rows[5]), dict(rows[6]),
            {"role": "user", "content": "несохранённое"}]
    monkeypatch.setattr(_llm, "available", lambda: (True, "тест"))
    monkeypatch.setattr(_apply, "find_gateway_session", lambda agent: (None, "нет гейтвея"))
    monkeypatch.setattr(_apply, "db_history", lambda agent: (rows, ""))
    monkeypatch.setattr(_compact, "_index", lambda session, rows: by_msg)
    monkeypatch.setattr(_compact, "distill_summary",
                        lambda *a, **kw: {"ok": True, "text": "СВОДКА", "input_chars": 10,
                                          "provider": "p", "model": "m", "tokens": 1})

    class _Engine:
        def live_agent(self):
            return object()

    facts = _compact.run("s-1", _Engine(), "m0003-m0005", deliver={"history": turn})
    assert facts.get("delivered") is True, facts["lines"][-3:]
    out = facts["new_history"]
    texts = [str(m.get("content")) for m in out]
    assert any("СВОДКА" in x for x in texts), "новый дистиллят на месте"
    assert "голова" in texts, "голова — отдельной строкой из базы, не склеенная"
    assert not any("старый дистиллят" in x for x in texts), "съеденное не вернулось через ленту хода"
    assert texts[-1] == "несохранённое", "свежий хвост хода не потерян"
    found = _alternation.their_repairs(out)
    assert found in (None, 0), f"их правило нашло бы {found} нарушений"



def test_e3_eaten_empty_call_does_not_drag_the_tail_along():
    """Живьём 2026-09-30 (`5cb486`): съеденная реплика-вызов с пустым текстом выкидывалась из
    памяти по ключу `("assistant", "", None)` — и с ней КАЖДАЯ такая реплика хвоста; их
    результаты повисали, их проверка нашла 9 нарушений. Съеденное узнаётся по `id` вызова."""
    def call(cid):
        return {"role": "assistant", "content": "",
                "tool_calls": [{"id": cid, "type": "function",
                                "function": {"name": "read_file", "arguments": "{}"}}]}

    def result(cid):
        return {"role": "tool", "content": f"ответ {cid}", "tool_call_id": cid}

    rows = [{"role": "user", "content": "раз"}, call("c1"), result("c1"),
            {"role": "assistant", "content": "готово"},
            {"role": "user", "content": "ещё"}, call("c2"), result("c2"),
            {"role": "assistant", "content": "снова готово"}]
    history = [dict(r) for r in rows]
    runs = [{"eaten": [0, 1, 2, 3], "kept": []}]
    _, new_history, _ = _compact._rebuild(rows, history, runs, [{"text": "СВОДКА"}])
    tail = [m for m in new_history if m.get("tool_calls")]
    assert [m["tool_calls"][0]["id"] for m in tail] == ["c2"], "вызов хвоста на месте"
    assert any(m.get("tool_call_id") == "c2" for m in new_history)
    assert not any(m.get("tool_call_id") == "c1" for m in new_history), "съеденное съедено"



def _wait_setup(monkeypatch, commits):
    """Путь A с живой сессией: база растёт, запись отвечает по очереди из `commits`."""
    from hermes_filetools.context_ctl import _apply, _llm
    rows = [
        {"role": "user", "content": "раз", "timestamp": 1.0},
        {"role": "assistant", "content": "два", "timestamp": 2.0},
        {"role": "user", "content": "три", "timestamp": 3.0},
        {"role": "assistant", "content": "четыре", "timestamp": 4.0},
        {"role": "user", "content": "пять", "timestamp": 5.0},
        {"role": "assistant", "content": "шесть", "timestamp": 6.0},
    ]
    db = {"rows": rows}
    session = {"history": [dict(r) for r in rows], "history_version": 0}
    written = []
    monkeypatch.setattr(_llm, "available", lambda: (True, "тест"))
    monkeypatch.setattr(_apply, "find_gateway_session", lambda agent: (session, ""))
    monkeypatch.setattr(_apply, "history_of", lambda s: [dict(r) for r in db["rows"]])
    monkeypatch.setattr(_apply, "db_history", lambda agent: (list(db["rows"]), ""))
    monkeypatch.setattr(_compact, "_index",
                        lambda session_id, rs: {f"m{i + 1:04d}": r for i, r in enumerate(rs)})
    monkeypatch.setattr(_compact, "distill_summary",
                        lambda *a, **kw: {"ok": True, "text": "СВОДКА", "input_chars": 10,
                                          "provider": "p", "model": "m", "tokens": 1})
    monkeypatch.setattr(_compact, "WAIT_TURN_POLL_S", 0)

    def commit(session_, agent, new_rows, **kw):
        written.append([str(r.get("content")) for r in new_rows])
        answer = commits.pop(0)
        if callable(answer):
            answer = answer(db)
        return answer
    monkeypatch.setattr(_apply, "commit_replacement", commit)

    class _Engine:
        def live_agent(self):
            return object()
    return db, written, _Engine()


def test_e4_write_waits_for_the_turn_and_takes_the_fresh_tail(monkeypatch):
    """2026-09-30: запись отбита «идёт ход» — ждём и пишем по свежей базе, сводку не теряем."""
    from hermes_filetools.context_ctl import _apply

    def busy_and_turn_writes(db):
        db["rows"] = db["rows"] + [{"role": "user", "content": "новое", "timestamp": 7.0}]
        return False, _apply.BUSY_TURN + ": SessionTurnLeaseLostError: active turn lease"
    db, written, engine = _wait_setup(monkeypatch, [busy_and_turn_writes, (True, "применено")])
    facts = _compact.run("s-1", engine, "m0002-m0003")
    assert facts["ok"], facts["lines"][-3:]
    assert len(written) == 2
    assert written[1][-1] == "новое", "вторая попытка — по свежей базе, с дописанным ходом"
    assert any("СВОДКА" in x for x in written[1])
    assert any("запись ждёт конца хода" in x for x in facts["lines"])


def test_e5_middle_changed_while_waiting_is_a_refusal(monkeypatch):
    from hermes_filetools.context_ctl import _apply

    def busy_and_middle_edited(db):
        rows = [dict(r) for r in db["rows"]]
        rows[2]["content"] = "три (правка на рельсе)"
        db["rows"] = rows
        return False, _apply.BUSY_MOVED + ": ОТКАЗ: в базе есть 1 активных строк"
    db, written, engine = _wait_setup(monkeypatch, [busy_and_middle_edited])
    facts = _compact.run("s-1", engine, "m0002-m0003")
    assert not facts["ok"]
    assert len(written) == 1, "с изменённой серединой вторая запись не делается"
    assert "изменилась середина" in facts["lines"][-1]
