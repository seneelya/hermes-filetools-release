# -*- coding: utf-8 -*-
"""Работа с АДРЕСАМИ выбора: во что превращается то, что человек отметил.

ЗАЧЕМ ОТДЕЛЬНЫЙ ФАЙЛ (владелец, 2026-09-28). Правило владения (`_pairs.py`, шапка — там весь
довод) решает, что достраивается к выбору человека. Жило оно внутри компакции, и выгрузке
его было не достать: чуть не завелась вторая копия того же правила. Теперь ОДНА главная
функция, её зовёт каждая операция и называет себя (`operation`), чтобы разное поведение,
если оно понадобится, было веткой здесь, а не новой копией где-то ещё. Сейчас поведение
у всех одинаковое.

ДВА УСЛОВИЯ, на которых держится файл:

- **никаких относительных импортов.** Дашбордная половина (`dashboard/plugin_api.py`) грузит
  наши модули ПО ПУТИ, вне пакета — относительный импорт там не работает. Агентская
  половина берёт этот же файл обычным импортом. Один файл, два способа загрузки;
- **никакого состояния.** Только функции над лентой: на входе строки и адреса, на выходе
  решение.

ДВЕ ОПЕРАЦИИ НАД ВЫБОРОМ:

    expand      выбранный РЕЗУЛЬТАТ тянет свою БЕЗМОЛВНУЮ реплику-носителя
    live_calls  у реплики остаются только те вызовы, чьи результаты тоже выбраны

Обе — одно правило с двух сторон: вызова без его результата не бывает, и результата без
вызова тоже. У вызова нет ни своей судьбы, ни своей ячейки в рельсе — показать на него
пальцем человек может только через результат.
"""
from __future__ import annotations

import json


def calls_of(row) -> list:
    """Список вызовов строки, чем бы поле ни оказалось.

    В базе `tool_calls` приходит списком словарей, но через снимок памяти гейтвея — иногда
    строкой JSON, а из SDK — объектами. Не разобрав поле, мы приняли бы реплику с вызовами за
    пустую и убили бы её как оболочку без содержимого.
    """
    calls = (row or {}).get("tool_calls") or []
    if isinstance(calls, str):
        try:
            calls = json.loads(calls)
        except (ValueError, TypeError):
            return []
    return list(calls) if isinstance(calls, list) else []


def call_id(call) -> str:
    """`id` элемента `tool_calls`: словарь или объект SDK. У словаря запасной — `function.id`."""
    if isinstance(call, dict):
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        return str(call.get("id") or fn.get("id") or "")
    return str(getattr(call, "id", "") or "")


def owners(rows) -> dict:
    """`id вызова` → позиция строки-РЕЗУЛЬТАТА. Хозяин каждого вызова, по всей ленте.

    Считается по строкам `tool` и их `tool_call_id` — единственной связи, которая есть
    всегда: `op id` наш, он штампуется в конверт результата и у вызова его нет никогда, а
    у чужого результата (упавший тул, `foreign`) нет и конверта.
    """
    out: dict = {}
    for index, row in enumerate(rows or []):
        if not isinstance(row, dict) or str(row.get("role") or "") != "tool":
            continue
        cid = str(row.get("tool_call_id") or "")
        if cid:
            out.setdefault(cid, index)
    return out


def carriers(rows) -> dict:
    """`id вызова` → позиция строки-РЕПЛИКИ, которая его несёт. Обратная к `owners`."""
    out: dict = {}
    for index, row in enumerate(rows or []):
        if not isinstance(row, dict) or str(row.get("role") or "") != "assistant":
            continue
        for call in calls_of(row):
            cid = call_id(call)
            if cid:
                out.setdefault(cid, index)
    return out


def expand_positions(operation, rows, chosen) -> tuple:
    """Ядро достройки на ПОЗИЦИЯХ ленты. `(позиции, пояснения)`, пояснение — `(носитель, результат)`.

    Выбранный РЕЗУЛЬТАТ тянет за собой свою БЕЗМОЛВНУЮ реплику-носителя. Слово «безмолвную»
    несущее: носитель с прозой не трогаем никогда — у него есть своя ячейка в рельсе, и её
    состояние это осознанный выбор человека. Достраиваем ровно то, чего человек физически
    не мог выбрать: у реплики без прозы нет своего брика в зеркале, значит и ячейки.

    Найдено живьём дважды. 2026-09-22, компакция: владелец отметила `m0001…m0010`, до движка
    доехало без реплик-носителей, и выбор рвался на пробеги, которые молча выпадали из работы.
    2026-09-28, выгрузка: снимок по «выбрать все» дал 198 файлов против 230 при взводе — без
    носителей, а выгрузка задумана и как будущий импорт сессии, где нужны честные вызовы.

    ТОЛЬКО В ЭТУ СТОРОНУ. Выбранная реплика результатов не тянет: у неё есть собственный
    предмет, её проза, и взять прозу без результатов — законное желание. Лишние вызовы такой
    реплики снимает `live_calls`, а не достройка.

    `operation` — кто зовёт (`"compact"`, `"export"`). Сейчас поведение одно на всех.
    """
    del operation  # одинаковое поведение; параметр — место под различие, а не забытая переменная
    rows = list(rows or [])
    chosen = set(chosen or [])
    carrier_at = carriers(rows)
    notes = []
    for index in sorted(chosen):
        row = rows[index] if 0 <= index < len(rows) else None
        if not isinstance(row, dict) or str(row.get("role") or "") != "tool":
            continue
        place = carrier_at.get(str(row.get("tool_call_id") or ""))
        if place is None or place in chosen:
            continue
        if str((rows[place] or {}).get("content") or "").strip():
            continue
        chosen.add(place)
        notes.append((place, index))
    return chosen, notes


def expand(operation, rows, addresses, wanted) -> tuple:
    """ГЛАВНАЯ ФУНКЦИЯ: массив m-адресов → он же, дополненный недостающими половинами пар.

    `rows` и `addresses` идут параллельно (адрес строки `i` — `addresses[i]`, пустой — строки
    без адреса). `wanted` — то, что отметил человек. Возвращает `(достроенный список,
    добавленные адреса)`: порядок исходного сохранён, добавленные в хвосте в порядке ленты.
    Ненайденные адреса НЕ выбрасываются — что с ними делать, решает операция.
    """
    rows = list(rows or [])
    addresses = list(addresses or [])
    wanted = [str(a or "").strip() for a in (wanted or []) if str(a or "").strip()]
    place_of = {}
    for index, addr in enumerate(addresses):
        addr = str(addr or "")
        if addr and index < len(rows):
            place_of.setdefault(addr, index)
    chosen = {place_of[a] for a in wanted if a in place_of}
    grown, _notes = expand_positions(operation, rows, chosen)
    have = set(wanted)
    added = []
    for index in sorted(grown - chosen):
        addr = str(addresses[index] or "") if index < len(addresses) else ""
        if addr and addr not in have:
            have.add(addr)
            added.append(addr)
    return wanted + added, added


def live_calls(operation, rows, index, kept) -> list:
    """Вызовы реплики `index`, у которых результат тоже в `kept` (множество ПОЗИЦИЙ).

    Вторая сторона правила (владелец, 2026-09-28): человек может снять галочку с результата,
    но не может поставить её на вызов — значит вызов, чей результат не выбран, не должен
    висеть в реплике без пары. Реплика с прозой, потерявшая все вызовы, остаётся прозой.

    Вызов, у которого результата в ленте нет ВОВСЕ (обрыв, чужая дверь), тоже уходит: пары у
    него нет не по выбору, а по факту, и пустить его дальше — значит собрать ленту, которую
    провайдер не примет.

    `operation` — кто зовёт. Сейчас поведение одно на всех.
    """
    del operation
    rows = list(rows or [])
    row = rows[index] if 0 <= index < len(rows) else None
    if not isinstance(row, dict):
        return []
    owner_at = owners(rows)
    kept = set(kept or [])
    return [call for call in calls_of(row) if owner_at.get(call_id(call)) in kept]
