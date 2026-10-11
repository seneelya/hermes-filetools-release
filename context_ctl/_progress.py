"""Ход прогона компакции для панели (`deferred/10`, владелец 2026-09-30).

Кнопка (фоновый поток) и автомат (внутри хода) пишут ОДИН файл состояния (`_compact_state`) на
каждом этапе и на каждом вопросе опроса; панель читает его маршрутом `/context-compact-status` и
показывает строкой хода с ползунком в строке состояния под кнопками команд.

Поля сверх прежних (`running/ok/started/error/lines`):
  who     button | auto
  span    что сжимаем, `m0034…m0043`
  stage   [export (автомат)] → prepare → warmup → questions → assemble → checks → [wait_turn] → write (кнопка) | handover (автомат)
  step    номер текущего вопроса; total — сколько всего по плану (0 — ещё не ясно: темы не названы)
  label   о чём вопрос, словами; topic — «тема k/K» у вопросов по теме
  beat    время последнего шага (epoch): по нему панель видит тишину

Пишет только ИДУЩИЙ прогон (`running`): сухой `/ctx-distill` зовёт то же ядро опроса, и его шаги
в файл боевого прогона не попадают.
"""
from __future__ import annotations

import re
import time

from . import _compact_state

# Состояние прогона. ОДИН объект на процесс: его же `_compact._state_now` (слеш-команда
# `/ctx-compact status`) — чтобы два адресата видели одно событие, а не две копии.
STATE: dict = {}

# О чём вопрос — словами для строки хода. Ключ шага — имя вопроса без номера темы.
LABELS = {
    "q1_count": "counting topics",
    "q2_titles": "naming topics",
    "q2_recount": "checking the topic list",
    "q2_merge": "merging topics",
    "q3_split": "main and minor topics",
    "q_which_set": "set of questions for each topic",
    "q_talk": "the conversation",
    "q_gate": "which sections to write",
    "q5_settled": "settled decisions",
    "q_pending_task": "pending task",
    "q6_open": "open items",
    "q_errors_fixes": "errors and fixes",
    "q_critical_values": "critical values",
    "q7_pack": "details",
    "q10_check": "self-check",
    "q11_filler": "minor topics",
    "q_constraints": "constraints, verbatim",
    "q12_additions": "additions",
}

# Финальные вопросы: запреты и добавления. Вопросы темы считает её опросник
# (`_scenario.Questionnaire.max_steps`, ворота вычитают закрытое — `Plan32.1`).
FINAL_STEPS = 2

_KEY_RE = re.compile(r"^([^\[]+)(?:\[(\d+)\])?$")


def label_of(key: str) -> tuple[str, int]:
    """`q5_settled[2]` → («settled decisions», 2). Неизвестный ключ — сам ключ."""
    m = _KEY_RE.match(str(key or ""))
    name, n = (m.group(1), int(m.group(2) or 0)) if m else (str(key or ""), 0)
    return LABELS.get(name, name), n


def progress(session_id: str, **fields) -> None:
    """Отметить этап/шаг идущего прогона в файл. Ошибка записи прогон не роняет."""
    if not STATE.get("running"):
        return
    STATE.update(fields)
    STATE["beat"] = time.time()
    _write(session_id)


def begin(session_id: str, who: str) -> None:
    """Начало прогона: чистое состояние, `running`."""
    STATE.clear()
    now = time.time()
    STATE.update({"running": True, "session": session_id, "ok": False, "who": who,
                  "started": time.strftime("%H:%M:%S"), "started_at": now, "beat": now,
                  "stage": "prepare", "step": 0, "total": 0, "label": "", "topic": "",
                  "span": "", "error": "", "lines": []})
    _write(session_id)


def finish(session_id: str, facts: dict | None = None, error: str = "") -> None:
    """Итог прогона: `running: False`, строки отчёта; `ok` — лента изменена или собрана."""
    facts = facts or {}
    STATE.update({"running": False, "lines": list(facts.get("lines") or []), "beat": time.time(),
                  "ok": bool(facts.get("ok") or facts.get("delivered"))})
    if error:
        STATE["error"] = error
    _write(session_id)


def _write(session_id: str) -> None:
    try:
        _compact_state.write_state(session_id, dict(STATE))
    except Exception:  # noqa: BLE001 - ход — для глаз, не для дела
        pass
