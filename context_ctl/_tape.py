# -*- coding: utf-8 -*-
"""Карта ленты: что лежит в сессии, строка за строкой, до правки и после.

ЗАЧЕМ ОТДЕЛЬНЫМ ФАЙЛОМ ОТЧЁТА. Владелец, 2026-09-22: после компакции хочется сразу видеть,
«какая была конфигурация до и какая стала после», не открывая базу зондом. Разбор отказа по
парам в тот же день занял бы минуту вместо получаса, будь такая карта под рукой: из отчётов
прогона не было видно ни того, что пузырьков четыре, ни того, который из них — половина пары.

ЧТО ЗДЕСЬ ВИДНО, чего не видно больше нигде:

  — порядок строк и адреса РЯДОМ: после компакции номера идут не по возрастанию, дистиллят
    получает свежий номер и встаёт на место первого съеденного, и глазами это иначе не
    поймать;
  — вызовы и их результаты СВЯЗАНЫ: `id` вызова в одной колонке, `tool_call_id` ответа в
    соседней — разрыв пары виден строкой, а не вычислением;
  — реплика ассистента с одним вызовом и пустой прозой помечена `вызов`, а не показана
    пустотой (просьба владельца: «помечать просто как вызов»). Таких строк в живой базе 44%,
    и в зеркале они выглядят обрубками, из-за чего однажды были приняты за следы удаления.
"""

from __future__ import annotations

from . import _pairs, _stubs

NL = chr(10)

# Что строка значит в ЭТОЙ правке. Слова короткие: колонка узкая, а читается она глазом
# сверху вниз, и длинные подписи мешают увидеть форму.
MARK_DISTILL = "ДИСТИЛЛЯТ"
MARK_CLOSE = "закрывающая"
MARK_BUBBLE = "пузырёк"
MARK_HULL = "оболочка"
# Вытащенный обратно результат — НЕ оболочка, хотя в съедаемом его тоже больше нет. Разведены
# 2026-09-22 по живой карте: `m0005` стоял с пометкой «оболочка», будучи обычным результатом,
# которому склейка блока сохранила жизнь ради его же вызова. Одно слово на два разных события
# — это отчёт, который надо расшифровывать.
MARK_RESCUED = "оставлен ради пары"
MARK_GONE = "убрана"
MARK_EATEN = "съедается"


def role_of(row) -> str:
    """Роль строки словами, с пометкой про пустую реплику-носителя вызова."""
    role = str((row or {}).get("role") or "?")
    if role != "assistant":
        return role
    calls = _pairs.calls_of(row)
    if not calls:
        return role
    if str((row or {}).get("content") or "").strip():
        return f"{role}+вызов"
    # Просьба владельца: не показывать её пустотой. Пустая реплика с вызовом — это НЕ след
    # удаления и не обрубок, это обычная форма хода агента.
    return "вызов" if len(calls) == 1 else f"вызовов {len(calls)}"


def _calls(row) -> str:
    out = []
    for call in _pairs.calls_of(row):
        name = _stubs._fn_name(call) or "?"
        call_id = (_stubs._call_id(call) or "?")[:8]
        out.append(f"{name}#{call_id}")
    return ";".join(out)


def render(rows, addr_of=None, marks=None, title: str = "") -> str:
    """Лента таблицей. `addr_of` — `позиция → адрес`, `marks` — `позиция → пометка`."""
    addr_of = addr_of or {}
    marks = marks or {}
    head = (f"| поз | адрес | роль | пометка | вызовы | ответ на | символов |{NL}"
            f"|---:|---|---|---|---|---|---:|")
    out = [f"### {title}", "", head] if title else [head]
    for index, row in enumerate(rows or []):
        body = str((row or {}).get("content") or "")
        out.append(
            f"| {index} | {addr_of.get(index, '—')} | {role_of(row)} | "
            f"{marks.get(index, '')} | {_calls(row) or ''} | "
            f"{str((row or {}).get('tool_call_id') or '')[:8]} | {len(body)} |")
    return NL.join(out)


def marks_for(rows, run_info, patch=None, hollow=()) -> dict:
    """Пометки ДО правки: что с какой строкой собираются сделать.

    Считается из того же решения, которым правка и делается, — иначе карта показывала бы
    намерение, а лента получала другое.
    """
    patch = patch or {}
    marks = {}
    eaten = list(run_info.get("eaten") or [])
    for index in eaten:
        marks[index] = MARK_EATEN
    if eaten:
        marks[eaten[0]] = f"{MARK_EATEN} → {MARK_DISTILL} встанет сюда"
    for index in run_info.get("kept") or []:
        marks[index] = MARK_BUBBLE
    for index in hollow or ():
        row = (rows or [])[index] if 0 <= index < len(rows or []) else None
        # Оболочка бывает только у реплики ассистента: у неё выпотрошили прозу, а вызовы
        # остались. Строка `tool`, вышедшая из съедаемого, — совсем другое событие: её
        # оставила в живых склейка блока, ради её же вызова.
        marks[index] = MARK_HULL if str((row or {}).get("role") or "") == "assistant" \
            else MARK_RESCUED
    for index, fresh in patch.items():
        if fresh is None:
            marks[index] = MARK_GONE
        elif index not in marks:
            marks[index] = "правится: убраны вызовы"
    return marks


def marks_after(rows, is_distillate, is_close) -> dict:
    """Пометки ПОСЛЕ правки: узнаём свои вставки по их же рамке, а не по позиции."""
    marks = {}
    for index, row in enumerate(rows or []):
        text = str((row or {}).get("content") or "")
        if is_distillate(text):
            marks[index] = MARK_DISTILL
        elif is_close(text):
            marks[index] = MARK_CLOSE
    return marks
