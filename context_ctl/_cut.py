"""Где резать сессию: доля → точка реза по ЛОГИЧЕСКОЙ границе.

Замысел — `__HQ/vision/Vision08__thread-distillate.md`, раздел ПРИМЕНЕНИЕ. Здесь только
арифметика и правило границы; кто и что потом замещает — не наше дело.

ТРИ ПРАВИЛА, И ВСЕ ТРИ ОПЛАЧЕНЫ РАЗГОВОРОМ С ВЛАДЕЛЬЦЕМ 2026-09-21.

1. **Доля считается двумя мерами, побеждает БОЛЬШАЯ.** Половина сообщений и половина объёма
   на живой сессии расходятся сильно: один цикл инструментов весит как десять реплик. Берём
   ту, что уводит дальше, и честно говорим в отчёте, какая победила.

2. **Рез ложится на реплику ЧЕЛОВЕКА.** Логические зоны кончаются там, а не посреди цикла
   обработки, где агент ещё не договорил сам с собой.

3. **Тянуться за этой границей разрешено недалеко.** Случай владельца: `USER`, затем цикл на
   полтораста сообщений. Если дотягивать до следующего человека, «половинная» компакция
   съедает сессию целиком. Не дотянулись в допуске — режем ВНУТРИ цикла, но только по
   границе целой пары «вызов + его результат»: вызов от результата не отрывается никогда.

ЧЕМ СЧИТАЕМ ОБЪЁМ. Символами. Своего счётчика токенов пока нет, а для ДОЛИ он и не нужен:
отношение символов к токенам по ленте примерно постоянно, и ошибка сокращается. Абсолютное
число токенов (порог автокомпакции) — другая задача, `Plan16-Task02`.
"""
from __future__ import annotations

from . import _stubs

# Насколько дальше цели разрешено тянуться в поисках реплики человека — ДОЛЯ ОТ ДОЛИ.
# При 0.5 доедем самое большее до 0.75 сессии: владелец назвала именно эту величину.
STRETCH = 0.5

# Хвост не трогаем никогда: последние ходы — это то, о чём разговор идёт ПРЯМО СЕЙЧАС, и
# заменять их сводкой бессмысленно. Считается в строках, а не второй дробью: две дроби на
# одной оси — два колеса, которые надо крутить согласованно.
TAIL_RESERVE_ROWS = 6


def weigh(row) -> int:
    """Вес одной строки в символах. Вызовы инструментов весят вместе с аргументами."""
    if not isinstance(row, dict):
        return 0
    total = len(str(row.get("api_content") or row.get("content") or ""))
    for call in row.get("tool_calls") or []:
        total += len(str(call))
    return total


def index_at_share(rows, share: float) -> int:
    """Последняя строка, на которой накопленный объём дотягивает до доли.

    `-1` значит «ни одной»: доля меньше первой же строки.
    """
    weights = [weigh(r) for r in rows]
    total = sum(weights)
    if total <= 0 or share <= 0:
        return -1
    want = total * share
    seen = 0
    for i, w in enumerate(weights):
        seen += w
        if seen >= want:
            return i
    return len(rows) - 1


def index_by_rows(rows, share: float) -> int:
    """То же по счёту строк. Округление ВВЕРХ: просили половину — половина должна уехать."""
    if not rows or share <= 0:
        return -1
    take = int(len(rows) * share)
    if take < len(rows) * share:
        take += 1
    return min(take, len(rows)) - 1


def _is_user(row) -> bool:
    return isinstance(row, dict) and str(row.get("role") or "") == "user"


def _pairs_whole(rows, last: int) -> bool:
    """Целы ли пары, если оборвать ленту после `last`."""
    return not _stubs.lint_pairs(rows[:last + 1])


def head_start(rows, head_keep: int) -> int:
    """Первая строка, которую можно сжимать: после `head_keep` строк головы, и не посреди пары.

    Голова — начало сессии, её собственная просьба (владелец: «первые сообщения из настроек»).
    Если граница головы разрывает пару вызов/результат, голова растёт до конца пары — сжатый
    кусок не может начинаться с результата без своего вызова.
    """
    start = max(0, min(int(head_keep or 0), len(rows)))
    while 0 < start < len(rows) and not _pairs_whole(rows, start - 1):
        start += 1
    return start


def cut_point(rows, fraction: float, head_keep: int = 0, tail_keep: int = TAIL_RESERVE_ROWS) -> dict:
    """Точка реза по доле. Возвращает отчёт, а не одно число: предпросмотр обязан объяснить.

    Сжимаемый кусок — строки `start..index` включительно. `start` — после головы (`head_keep`,
    `head_start`), хвост (`tail_keep`) не трогается никогда; доля считается от строк МЕЖДУ ними.
    `index = -1` значит «резать нечего», и это нормальный исход на короткой сессии, а не ошибка.

    ОДИН механизм на всё (владелец, 2026-09-29): галочки кнопки компакции на рельсе, автомат,
    слеш-команда с долей — все берут кусок отсюда (`select`); кнопка лишь отдаёт его человеку.
    """
    out = {"index": -1, "start": 0, "rule": "none", "measure": "", "fraction": float(fraction or 0),
           "by_rows": -1, "by_chars": -1, "target": -1, "limit": -1, "why": "",
           "head_keep": int(head_keep or 0), "tail_keep": int(tail_keep or 0)}
    if not rows:
        out["why"] = "строк нет вовсе"
        return out
    share = float(fraction or 0)
    if share <= 0 or share > 1:
        out["why"] = "доля должна быть больше 0 и не больше 1"
        return out

    start = head_start(rows, head_keep)
    end = len(rows) - 1 - max(0, int(tail_keep or 0))   # последняя строка, которую можно сжать
    out["start"] = start
    if end < start:
        out["why"] = (f"между головой ({start} строк) и хвостом ({tail_keep} строк) ничего "
                      "не осталось — резать нечего")
        return out
    middle = rows[start:end + 1]

    out["by_rows"] = by_rows = start + index_by_rows(middle, share)
    out["by_chars"] = by_chars = start + index_at_share(middle, share)
    # «Что больше» — то и берём: мера, уводящая дальше, и есть честная доля.
    out["target"] = target = max(by_rows, by_chars)
    out["measure"] = "строки" if by_rows >= by_chars else "объём"

    stretched = min(share * (1 + STRETCH), 1.0)
    limit = start + max(index_by_rows(middle, stretched), index_at_share(middle, stretched))
    limit = min(limit, end)
    out["limit"] = limit
    if target < start:
        out["why"] = "доля меньше первой же строки — резать нечего"
        return out
    if target > limit:
        target = limit
        out["target"] = target

    # 1. Ближайшая реплика человека ЗА целью, но в пределах допуска. Режем ПЕРЕД ней:
    #    новая зона начинается с человека, а не с его обрывка.
    for p in range(target, limit + 1):
        if p + 1 < len(rows) and _is_user(rows[p + 1]) and _pairs_whole(rows, p):
            out.update({"index": p, "rule": "user",
                        "why": "рез перед репликой человека"})
            return out

    # 2. Не дотянулись — режем внутри цикла по целой паре. Сначала ближе к цели и дальше к
    #    допуску, потом назад: лучше съесть меньше обещанного, чем разорвать пару.
    for p in list(range(target, limit + 1)) + list(range(target - 1, start - 1, -1)):
        if _pairs_whole(rows, p):
            out.update({"index": p, "rule": "pair",
                        "why": ("реплики человека в допуске нет — рез по границе целой "
                                "пары вызов/результат внутри цикла")})
            return out

    out["why"] = "ни одной целой границы не нашлось — резать нечем"
    return out


def select(rows, cfg: dict, fraction: float | None = None) -> dict:
    """Кусок для компакции по НАШИМ настройкам: `cut_point` с `compact_head_keep`,
    `compact_tail_keep` и долей `compact_cut_percent` (или `fraction`, если дана явно)."""
    share = fraction if fraction is not None else (cfg.get("compact_cut_percent") or 0) / 100.0
    return cut_point(rows, share, head_keep=cfg.get("compact_head_keep", 0),
                     tail_keep=cfg.get("compact_tail_keep", TAIL_RESERVE_ROWS))


def explain(report: dict) -> list:
    """Отчёт строками для предпросмотра. Человек должен видеть, какая мера победила."""
    if report.get("index", -1) < 0:
        return [f"рез не выбран: {report.get('why') or 'причина не названа'}"]
    return [
        f"доля {report['fraction']:g}: по строкам до {report['by_rows']}, "
        f"по объёму до {report['by_chars']} — победила мера «{report['measure']}»",
        f"цель {report['target']}, допуск до {report['limit']} "
        f"(голова {report.get('start', 0)} строк, хвост {report.get('tail_keep', TAIL_RESERVE_ROWS)} строк)",
        f"сжимаются строки {report.get('start', 0)}..{report['index']}: {report['why']}",
    ]
