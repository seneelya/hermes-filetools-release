"""Адресация сообщений и состав вагонов — `Plan14-Task03`.

Формы записей заморожены в `__HQ/CONTRACT__ledger-records.md` (§5 и ПОПРАВКА 1),
пишет их `_ledger`. Здесь — единственное место, которое решает, КОМУ выдать адрес
и КАК сложились вагоны.

ПОЧЕМУ ИНДЕКС РЕТРОСПЕКТИВНЫЙ. Мы не владеем моментом появления сообщения: наш код
исполняется внутри наших тулов, в `compress()` и в маршрутах дашборда — и ни одно
из этих мест не срабатывает, когда человек отправил реплику или когда модель
записала прозу. Значит адреса раздаются тогда, когда нас позвали, сверкой базы с
журналом. Побочная выгода: индекс догоняет и те сессии, что шли без нас.

ПРАВИЛО ВАГОНА: **вагон открывает реплика человека.** Не «по концу», хотя полный
turn в их смысле определяется именно концом — последним ответом модели без
тулколов: конец в момент индексации может ещё не наступить, а начало однозначно
всегда. Проверено на живой базе 2026-09-09 (`__HQ/probes/probe_layout.py --scan`):
455 сессий, 48976 строк, **82 места, где две реплики человека идут подряд** — по
правилу «конец» столько вагонов слиплось бы в один.

ЭТОТ МОДУЛЬ БАЗЫ НЕ ЗНАЕТ. Строки приходят аргументом — от `SessionDB.get_messages`
у вызывающего. Так индексатор проверяется без базы и не тащит их импорт в процесс,
где его может не быть.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys

logger = logging.getLogger(__name__)

# Два вызывающих загружают нас по-разному: движок обычным импортом пакета, дашборд
# — ПО ПУТИ (`spec_from_file_location`, вне пакета), потому что его самого так
# грузит их веб-сервер. Относительный импорт во втором случае невозможен, поэтому
# леджер разыскивается тем же именем модуля, под которым его кладёт дашборд, —
# так в процессе остаётся ОДИН экземпляр, а с ним один кеш счётчиков.
_LEDGER_MODULE = "hermes_filetools_ledger"


def _load_ledger():
    try:
        from . import _ledger as mod  # обычный путь: мы часть пакета
        return mod
    except Exception:  # noqa: BLE001 - загружены по пути, пакета вокруг нет
        pass
    cached = sys.modules.get(_LEDGER_MODULE)
    if cached is not None:
        return cached
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parent / "_ledger.py"
    spec = importlib.util.spec_from_file_location(_LEDGER_MODULE, path)
    if spec is None or spec.loader is None:
        raise ImportError("ledger module could not be located")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_LEDGER_MODULE] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(_LEDGER_MODULE, None)
        raise
    return mod


_ledger = _load_ledger()

# Поля, по которым считается дайджест. Не только `content`: пустая строка
# ассистента с вызовами тулов — обычное дело (живьём 15604 строк с одним вызовом),
# и по одному тексту такие строки слились бы в одну координату.
_DIGEST_FIELDS = ("tool_calls", "tool_call_id", "name")


def digest(row) -> str:
    """Полный sha256 содержимого строки — КАКАЯ версия этого места имеется в виду.

    Замерено на живой базе 2026-09-09: 878 координат из 48098 заняты больше чем
    одной строкой, и **все 878 разводятся дайджестом**. Неразличимых (роль, время
    и содержимое совпали) — ноль. Поэтому позиция как последний разделитель пока
    не нужна; открытый пункт контракта §9 закрыт замером, а не рассуждением.
    """
    parts = [str((row or {}).get("content") or "")]
    for key in _DIGEST_FIELDS:
        value = (row or {}).get(key)
        if not value:
            continue
        parts.append(value if isinstance(value, str)
                     else json.dumps(value, ensure_ascii=False, sort_keys=True))
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def coord_ts(value):
    """Координата во времени, приведённая к одному типу с обеих сторон сверки.

    Из базы `timestamp` приходит числом, из журнала — тем, во что его превратил
    JSON. Сравнивай мы их как есть, `1788527544.34` и `"1788527544.34"` были бы
    разными координатами, и каждый проход раздавал бы новые адреса.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value or "")


def place(row) -> tuple:
    """ЛИЧНОСТЬ сообщения: `(роль, координата)`. Дайджест сюда НЕ входит.

    Разделение оплачено живой проверкой 2026-09-09 (`Plan14-Task04`). Личностью
    была тройка с дайджестом, и это ломало ровно то, ради чего адресация нужна:
    наш же `erase` переписывает массив `tool_calls` у строки ассистента, а
    `drop content` — тело тул-ответа. Сообщение то же, содержимое другое,
    дайджест поехал — и адрес переставал разрешаться. Из 39 адресов сессии шесть
    указывали на строки, которые ЛЕЖАЛИ НА МЕСТЕ.

    Замер, разрешающий такой ключ: по активным строкам всей базы координата
    уникальна — 20240 мест на 20241 строку, единственное столкновение разводится
    дайджестом (см. `_pick`).
    """
    return (str((row or {}).get("role") or ""),
            coord_ts((row or {}).get("timestamp")))


def entry_place(entry) -> tuple:
    """То же место, снятое с записи журнала."""
    return (str((entry or {}).get("role") or ""),
            coord_ts((entry or {}).get("msg_ts")))


def coord(row) -> tuple:
    """Место плюс версия — полная ссылка, как в поправке 1 к контракту."""
    return place(row) + (digest(row),)


def entry_coord(entry) -> tuple:
    """Тот же ключ, но снятый с записи журнала."""
    return entry_place(entry) + (str((entry or {}).get("sha256") or ""),)


def is_opener(row) -> bool:
    """Открывает ли строка вагон.

    Тул-ответ у некоторых провайдеров тоже приезжает ролью `user` — отличает его
    `tool_call_id`, который есть у ВСЕХ 24237 тул-строк живой базы (замер
    2026-09-09). Без этой проверки каждый ответ тула начинал бы новый вагон.
    """
    row = row or {}
    return row.get("role") == "user" and not row.get("tool_call_id")


def carriages(rows) -> list:
    """Разбить строки на вагоны. `[{"opener": строка|None, "rows": [...]}, ...]`.

    Вагон 0 — всё, что стоит до первой реплики человека (системный промпт и
    прочее). У него `opener is None`, как и сказано в поправке 1.
    """
    out = []
    current = {"opener": None, "rows": []}
    for row in rows or []:
        if is_opener(row):
            if current["rows"] or current["opener"] is not None:
                out.append(current)
            current = {"opener": row, "rows": [row]}
        else:
            current["rows"].append(row)
    if current["rows"] or current["opener"] is not None:
        out.append(current)
    return out


def call_ids(row) -> list:
    """Идентификаторы вызовов, объявленных строкой ассистента.

    Форма `tool_calls` у провайдеров разная (плоский `id` или внутри `function`),
    поэтому разбор один и здесь, а не у каждого читателя.
    """
    out = []
    for call in ((row or {}).get("tool_calls") or []):
        if not isinstance(call, dict):
            continue
        cid = call.get("id") or (call.get("function") or {}).get("id")
        if cid:
            out.append(str(cid))
    return out


def steps(rows) -> list:
    """Разбить строки на ШАГИ: `[{"lead": строка, "rows": [...]}, ...]`.

    Шаг — реплика ассистента плюс ответы на ЕЁ вызовы. Именно это владелец имеет
    в виду, говоря «удалить ход»: полный turn в их смысле кончается возвратом
    управления человеку, а стирать надо шаг (поправка 1 к контракту).

    Каждая строка принадлежит РОВНО одному шагу: реплика человека и проза модели
    без вызовов — шаги из одной строки. Тотальность важнее изящества: `erase turn`
    получит шаг для любой карточки, на которую ткнули, и не придётся заводить
    отдельную ветку «а это ни к чему не относится».

    Связь по `tool_call_id`, а НЕ по соседству, хотя замер живой базы 2026-09-09
    говорит, что 6956 шагов из 6957 идут сплошняком: соседство — свойство
    сегодняшней записи, а идентификатор вызова это сама связь. Единственный
    вызов без ответа в базе тоже нашёлся — по позиции он утащил бы в шаг чужую
    строку.
    """
    rows = list(rows or [])
    answers = {}
    for row in rows:
        cid = str(row.get("tool_call_id") or "")
        if row.get("role") == "tool" and cid:
            answers.setdefault(cid, []).append(row)

    taken = set()
    out = []
    for row in rows:
        if id(row) in taken:
            continue
        group = [row]
        taken.add(id(row))
        if row.get("role") == "assistant":
            for cid in call_ids(row):
                for answer in answers.get(cid, []):
                    if id(answer) not in taken:
                        group.append(answer)
                        taken.add(id(answer))
        out.append({"lead": row, "rows": group})
    return out


def foreign_call_ids(rows, entries) -> set:
    """`tool_call_id` тул-строк, которых НЕТ в нашем журнале операций.

    Чужой вызов внутри шага — не ошибка и не помеха: по живой базе это около
    десятой части тул-строк (`delegate_task`, `web_search`, `todo`, `memory` и
    прочие 26 имён). Но действие над ходом уносит и их, а сказать о них нам
    нечего — ни адреса операции, ни записи. Значит их надо СЧИТАТЬ и НАЗЫВАТЬ,
    а не молча прихватывать.
    """
    ours = {str(e.get("tool_call_id") or "")
            for e in (entries or [])
            if isinstance(e, dict) and not _ledger.is_event(e)}
    ours.discard("")
    out = set()
    for row in rows or []:
        cid = str((row or {}).get("tool_call_id") or "")
        if (row or {}).get("role") == "tool" and cid and cid not in ours:
            out.add(cid)
    return out


def recorded_steps(entries) -> list:
    """Шаги сессии ПО ПОСЛЕДНЕЙ записи каждого вагона — то есть состав СЕЙЧАС.

    Запись состава отвечает на вопрос «что в сессии сейчас» (поправка 1), и
    поэтому она сжимается вслед за стиранием. Что там было раньше, помнит
    `step_history`: журнал append-only, прошлые записи никуда не делись.
    """
    out = []
    for layout in _ledger.layouts(entries).values():
        for step in (layout.get("steps") or []):
            names = [str(m) for m in (step or []) if str(m or "").strip()]
            if names:
                out.append(names)
    return out


def step_history(entries, address) -> list:
    """Всё, что КОГДА-ЛИБО записывалось в состав шага с этим адресом.

    Объединение по всем записям состава, в порядке первого появления. Отсюда
    берётся ответ «этот шаг надкусан»: последняя запись показывает нынешний
    состав, а разница с историей — то, чего уже нет. Без этого стирание молча
    переписывало бы память о самом себе.
    """
    address = str(address or "").strip()
    if not address:
        return []
    out = []
    for entry in entries or []:
        if not (isinstance(entry, dict) and entry.get("event") == _ledger.EVENT_LAYOUT):
            continue
        for step in (entry.get("steps") or []):
            names = [str(m) for m in (step or []) if str(m or "").strip()]
            if address not in names:
                continue
            for name in names:
                if name not in out:
                    out.append(name)
    return out


def describe_step(address, rows, entries, by_msg=None) -> dict:
    """Что представляет собой шаг, которому принадлежит адрес `address`.

    `{"lead", "members", "foreign", "source", "missing"}`.

    АВТОРИТЕТ. Пока строки живы — авторитет у СТРУКТУРЫ: `tool_calls` и есть то,
    из чего провайдер собирает запрос, и разойтись с ней значило бы действовать
    не над тем, что видит модель. Как только строки нет — отвечает ЗАПИСЬ, и
    другого источника уже не существует. То есть это не «кто главнее», а два
    непересекающихся случая; путаница возникала, пока запись считали дублем
    структуры, а она — её единственный переживший след.

    `missing` — адреса шага, которым строки уже нет: по ним видно, что шаг
    тронут, и `erase turn` не станет второй раз сносить снесённое.
    """
    out = {"lead": "", "members": [], "foreign": [], "source": "",
           "missing": []}
    address = str(address or "").strip()
    if not address:
        return out
    by_msg = by_msg or {}
    live = by_msg.get(address)
    if live is not None:
        for step in steps(rows):
            if any(r is live for r in step["rows"]):
                names = []
                back = {id(r): a for a, r in by_msg.items()}
                for r in step["rows"]:
                    got = back.get(id(r))
                    if got:
                        names.append(got)
                out["lead"] = back.get(id(step["lead"]), "")
                out["members"] = names
                out["source"] = "structure"
                foreign = foreign_call_ids(step["rows"], entries)
                out["foreign"] = [back.get(id(r), "") for r in step["rows"]
                                  if str(r.get("tool_call_id") or "") in foreign]
                # Журнал помнит и то, чего в структуре уже нет. Шаг, у которого
                # часть снесена, обязан выглядеть надкусанным, а не целым.
                out["missing"] = [n for n in step_history(entries, address)
                                  if n not in by_msg]
                return out
    seen = step_history(entries, address)
    if seen:
        out["lead"] = seen[0]
        out["members"] = list(seen)
        out["source"] = "record"
        out["missing"] = [n for n in seen if n not in by_msg]
    return out


def _pick(past, sha, taken):
    """Какая из записей этого места описывает ЭТУ строку. `None` — ни одна.

    Обычный случай: место занято одним сообщением, и подходит любая его запись —
    берём первую, у неё исходный адрес. Редкий: место занято ДВУМЯ строками
    (одно столкновение на 20241 активную строку живой базы) — тогда разводит
    дайджест, а `taken` не даёт второй строке забрать адрес первой.
    """
    exact = [e for e in past
             if str(e.get("sha256") or "") == sha
             and str(e.get("msg") or "") not in taken]
    if exact:
        return exact[0]
    free = [e for e in past if str(e.get("msg") or "") not in taken]
    return free[0] if free else None


def index_session(session, rows, entries, *, session_src: str = "arg",
                  parent_session: str = "", write: bool = True) -> dict:
    """Догнать индекс этой сессии и вернуть сводку. НИКОГДА не бросает.

    Идемпотентно: адрес выдаётся МЕСТУ один раз. Индексатор зовут часто — на
    каждый запрос панели, — и без сверки с уже записанным сессия получала бы
    новый комплект адресов при каждом обходе.

    Изменилось содержимое места — адрес остаётся, дописывается запись НОВОЙ
    ВЕРСИИ (тот же `msg`, другой `sha256`). Это поправка 3: личность не кодирует
    то, что меняется, а наши же правки содержимое как раз и меняют.

    `write=False` — сухой проход: считает то же самое, но в журнал не пишет.

    Возвращает `{"addressed", "reused", "versioned", "skipped", "layouts",
    "carriages", "by_msg"}`, где `by_msg` — `адрес -> строка базы`, готовая
    карта для читателя, которому нужен join к промпту.
    """
    out = {"addressed": 0, "reused": 0, "versioned": 0, "skipped": 0,
           "layouts": 0, "carriages": 0, "by_msg": {}}
    try:
        # Место -> список записей о нём, в порядке журнала. Адрес берётся у
        # ПЕРВОЙ (он выдан один раз и не перевыдаётся), текущая версия — у
        # ПОСЛЕДНЕЙ с этим адресом.
        history = {}
        for e in _ledger.message_records(entries):
            history.setdefault(entry_place(e), []).append(e)

        address_of = {}
        row_of = {}
        seen_places = {}
        for row in rows or []:
            spot = place(row)
            sha = digest(row)
            past = history.get(spot) or []
            taken = seen_places.setdefault(spot, [])
            found = _pick(past, sha, taken)
            if found is not None:
                address = str(found.get("msg") or "")
                taken.append(address)
                address_of[coord(row)] = address
                row_of[address] = row
                out["reused"] += 1
                # Место то же, содержимое другое — это НОВАЯ ВЕРСИЯ, а не новое
                # сообщение. Дописываем только если последняя записанная версия
                # этого адреса отличается: иначе журнал рос бы на каждый проход.
                versions = [e for e in past if str(e.get("msg") or "") == address]
                if versions and str(versions[-1].get("sha256") or "") != sha:
                    if not write:
                        # Сухой проход считает НАМЕРЕНИЕ: сказать «версий нет»,
                        # когда они есть, значило бы соврать смотрящему.
                        out["versioned"] += 1
                    elif _ledger.note_message(
                            session=session, msg=address,
                            role=str(row.get("role") or ""),
                            msg_ts=row.get("timestamp"), sha256=sha,
                            session_src=session_src,
                            tool_call_id=str(row.get("tool_call_id") or ""),
                            parent_session=parent_session):
                        past.append({"msg": address, "sha256": sha,
                                     "role": row.get("role"),
                                     "msg_ts": row.get("timestamp")})
                        out["versioned"] += 1
                continue
            address = _ledger.next_msg(session) if write else ""
            if not address:
                # Счётчик недостижим (или сухой проход) — строка остаётся без
                # адреса. Пустой адрес в записи был бы хуже отсутствия записи.
                out["skipped"] += 1
                continue
            ok = _ledger.note_message(
                session=session, msg=address, role=str(row.get("role") or ""),
                msg_ts=row.get("timestamp"), sha256=sha,
                session_src=session_src,
                tool_call_id=str(row.get("tool_call_id") or ""),
                parent_session=parent_session)
            if not ok:
                out["skipped"] += 1
                continue
            taken.append(address)
            address_of[coord(row)] = address
            row_of[address] = row
            history.setdefault(spot, []).append(
                {"msg": address, "sha256": sha, "role": row.get("role"),
                 "msg_ts": row.get("timestamp")})
            out["addressed"] += 1

        out["by_msg"] = row_of

        recorded = _ledger.layouts(entries)
        cars = carriages(rows)
        out["carriages"] = len(cars)
        for car in cars:
            opener = address_of.get(coord(car["opener"])) if car["opener"] else None
            members = [address_of[coord(r)] for r in car["rows"]
                       if coord(r) in address_of]
            if car["opener"] is not None and not opener:
                # Вагон без адресованного открывателя записать нечем: ключ
                # записи — именно он.
                continue
            # Состав ШАГОВ внутри вагона — тоже запись, а не вывод. Сегодня его
            # можно вывести из `tool_calls`, но `erase turn` сносит именно те
            # строки, из которых вывод и делается: после него сказать, что было
            # в шаге, сможет только записанное.
            grouped = []
            for step in steps(car["rows"]):
                names = [address_of[coord(r)] for r in step["rows"]
                         if coord(r) in address_of]
                if names:
                    grouped.append(names)

            was = recorded.get(opener)
            if (was is not None
                    and list(was.get("members") or []) == members
                    and [list(s) for s in (was.get("steps") or [])] == grouped):
                continue  # состав не изменился — писать нечего
            if write and _ledger.note_layout(session=session, opener=opener,
                                             members=members, steps=grouped):
                out["layouts"] += 1
    except Exception as e:  # noqa: BLE001 - свидетельство не отменяет работы
        logger.debug("filetools msgindex: session %r not indexed: %s", session, e)
    return out
