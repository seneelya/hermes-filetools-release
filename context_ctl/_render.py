"""Блок данных для холодного опроса: реплики словами, вызовы инструментов — ярлыком и обрезком.

ЗАЧЕМ СВОЙ СЕРИАЛИЗАТОР, А НЕ ИХ. `_compact_prompt.py` заморожен (решение владельца,
2026-09-21): оттуда мы больше ничего не берём. Их `_serialize_for_summary` вдобавок режет
тела по единым порогам, ничего не зная о том, ЧТО это за инструмент, — а нам нужно ровно
обратное.

ПОЧЕМУ ВЫЗОВЫ ВООБЩЕ ВХОДЯТ В ДАННЫЕ (правка 2026-09-21, владелец). Раньше вызовы и их
ответы в модель не уходили вовсе. Но человек мог передать задачу или данные ФАЙЛОМ — и
тогда холодная модель видит «они что-то читали», а чем занимались, непонятно. Имя файла
часто и есть половина смысла. Тела при этом по-прежнему не едут целиком: едет обрезок.

ЧЕГО ЭТО НЕ ЛЕЧИТ: всё, что агент понял из файлов и не пересказал словами. Достать это
может только горячий опрос живой модели — других источников нет (`Vision07`).

РЕДАКТУРА СЕКРЕТОВ — ИХ, ИМПОРТОМ, И ЭТО НЕ ЛЕНЬ. Копия их правил редактуры молча отстала
бы от оригинала, и цена такого отставания — утёкший ключ. Импорт не удался — говорим
громко и по имени, а не подставляем тихий фолбэк.
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import _prompts

# Номер операции лежит в ТЕЛЕ ответа инструмента (`Operation ID: 0001`) — это шапка наших
# же тулов. Вытаскиваем его, чтобы он встал в ЯРЛЫК обеих половин пары, и вырезаем из тела:
# в ярлыке он полезен (по нему адресуется вся механика теней), в теле он дубль.
#
# ОБЁРТКА У ШАПКИ РАЗНАЯ, и первая версия регулярки на этом промахнулась молча (поймано
# владельцем на живых данных 2026-09-21). В бою встречаются как минимум две формы:
#     ◆Operation ID: 0002
#     <!-- Operation ID: 0003 -->
# Поэтому якоримся не на начало строки, а на сами слова, и режем строку ЦЕЛИКОМ вместе с
# обёрткой. Ограничение слева держит нас от совпадения с прозой, где номер лишь упомянут.
_OP_RE = re.compile(r"^[^\n]{0,40}?Operation ID:\s*([0-9a-fA-F]+)[^\n]{0,20}$", re.M)

IMPORT_PROBLEM = ""
# Срезание `<think>`-блоков импортируется ЛЕНИВО (их модуль тяжёлый), поэтому отказ
# обнаруживается не при загрузке, а на первой реплике. След остаётся здесь.
THINK_PROBLEM = ""
try:
    from agent.context_compressor import _redact_compaction_text
except Exception as _exc:  # noqa: BLE001
    IMPORT_PROBLEM = f"agent.context_compressor: {_exc}"

    def _redact_compaction_text(text: Any) -> str:  # type: ignore[misc]
        raise RuntimeError(IMPORT_PROBLEM)


# ── Бюджеты обрезки. Одна правка здесь меняет весь блок данных ───────────────
# Порядок величин выбран «на глаз» и подлежит правке по живым прогонам: сотня символов
# головы отвечает на вопрос «чем занимались», не отвечая на вопрос «что там было».
CLIP_HEAD = 400            # символов с начала тела результата
CLIP_TAIL = 400            # символов с конца — только у категорий, где смысл в хвосте
CLIP_ARGS = 600            # аргументы вызова: путь и команда, не содержимое
# ПОЧЕМУ У АРГУМЕНТОВ БЮДЖЕТ БОЛЬШЕ, ЧЕМ У ТЕЛА (правка по живому прогону 2026-09-21).
# На двухстах символах обрезалась сама КОМАНДА: `cd /workspace/cTest && ls -la && …`
# превращалась в огрызок, и модель дописала за него вывод («ls -la показал только 3
# файла») — выдумка, которую поймала самопроверка. Команда почти всегда короткая, дорого
# это не стоит, а информативнее неё в строке вызова нет ничего.
#
# АРИФМЕТИКА, о которой надо помнить на больших сессиях: двести вызовов по
# (600 + 400 + 400) символов — это под триста килобайт блока данных. Пороги здесь и стоят
# затем, чтобы крутить их одной правкой, когда мы дойдём до настоящих сессий.
#
# РЕПЛИКУ НЕ РЕЖЕМ ВОВСЕ (решение владельца, 2026-09-22). Стояло 6000 — то есть обрезка без
# спроса ровно того, что и есть предмет работы: в реплику человек вставляет куски файлов,
# логи и чужие ответы, а агент кладёт разбор, и всё это уезжало молча. Сначала отдаём
# целиком; фильтровать содержимое будем осознанно и отдельно.
#
# Тихой обрезки за этим числом нет. Их `_bound_summary_input` (160K символов) живёт в
# `agent/context_compressor.py`, то есть на пути `/compress`, с которого мы ушли 2026-09-22;
# наша дверь `_llm.ask_structured` вход не режет нигде. Единственный потолок — окно
# вспомогательной модели, и оно отвечает отказом, а не молчанием.
CLIP_PROSE = 100_000_000   # реплика человека или агента: НЕ режем, это и есть предмет

# МЕТКА ОБРЕЗКИ. Три требования владельца (2026-09-21), и каждое оплачено живым файлом:
#   — с переносами строк: в середине строки метка ломала структуру того, что резала;
#   — по-английски и НАЗЫВАЯ ВИНОВНИКА: модель должна знать, что сократили МЫ при компакции,
#     а не что текст таким был;
#   — «сократил», а не «вырезал»: вырезанное звучит как потерянное, сокращённое — как то,
#     чего в этом месте просто нет целиком.
#   — и, с 2026-09-21, в НАШИХ скобках `<[ … ]>`: метка служебная, а стояла в обычных
#     квадратных, то есть ровно в тех, от которых мы ушли в ярлыках, потому что они
#     встречаются в тексте постоянно (`__HQ/CONTRACT__markup.md`, правило 1).
ELLIPSIS_PLAIN = (chr(10) * 2) + "[ … shortened here by the compaction pass … ]" + (chr(10) * 2)
ELLIPSIS = (chr(10) * 2) + "<[ … shortened here by the compaction pass … ]>" + (chr(10) * 2)

# СКОБКИ ЯРЛЫКА. `<[ … ]>` вместо `[ … ]` (владелец, 2026-09-21): обычная квадратная скобка
# в тексте сессии встречается постоянно, и разметка переставала быть отличимой от данных.
LABEL_OPEN = "<["
LABEL_CLOSE = "]>"

# Категория инструмента решает ФОРМУ обрезки. Имена взяты из живой базы (2026-09-21),
# а не придуманы: `read_file` 1691, `terminal` 1381, `search_files` 648, `patch` 513,
# `write_file` 377, `execute_code` 62, `process` 95.
#
# `head_tail` — там, где голова пустая (баннер, шапка таблицы), а смысл в хвосте:
# «628 passed, 2 skipped» стоит в последней строке, и без хвоста результат бесполезен.
_CATEGORY = {
    "terminal": "head_tail",
    "execute_code": "head_tail",
    "process": "head_tail",
}


def category(tool_name: str) -> str:
    """Как резать результат этого инструмента: `head_tail` или `head`.

    Неизвестное имя — `head`. Список нарочно короткий и дописывается по мере встреч:
    угадывать заранее незачем, а одно место правки есть.
    """
    return _CATEGORY.get(str(tool_name or "").strip().lower(), "head")


def clip(text: str, mode: str = "head", *, head: int = CLIP_HEAD,
         tail: int = CLIP_TAIL) -> str:
    """Обрезка тела под категорию. Короткое возвращается как есть, без пометки."""
    body = str(text or "")
    if mode == "head_tail":
        if len(body) <= head + tail:
            return body
        return body[:head] + ELLIPSIS + body[-tail:]
    if len(body) <= head:
        return body
    return body[:head] + ELLIPSIS


def tool_names(row) -> list:
    """Имена инструментов, объявленных этой репликой. Пусто — вызовов нет."""
    out = []
    for tc in (row or {}).get("tool_calls") or []:
        fn = tc.get("function") if isinstance(tc, dict) else getattr(tc, "function", None)
        name = (fn or {}).get("name") if isinstance(fn, dict) else getattr(fn, "name", "")
        out.append(str(name or "?"))
    return out


def _content(row) -> str:
    """Текст реплики: список частей сводится в строку, секреты редактируются."""
    content = (row or {}).get("content")
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(str(part.get("text") or ""))
        content = "\n".join(p for p in parts if p)
    return _redact_compaction_text(content or "")


def _label(addr: str, head: str) -> str:
    """Ярлык блока. Адрес свой у каждой строки базы — у вызова и у его ответа они РАЗНЫЕ."""
    return f"{LABEL_OPEN}{addr + ' ' if addr else ''}{head}{LABEL_CLOSE}: "


def _pair_head(kind: str, name: str, op: str) -> str:
    """Шапка половины пары: `TOOL-call (terminal operation ID 0001)`.

    Форма владельца, 2026-09-21: у вызова и у ответа она ОДИНАКОВАЯ с точностью до слова
    `call`/`result`. Раньше вызов прятался внутри реплики блоком `[Tool calls: …]`, а ответ
    подписывался иначе — пара не читалась как пара, хотя механика вся построена на парах.
    """
    inner = " ".join(x for x in (name, f"operation ID {op}" if op else "") if x)
    return f"TOOL-{kind}" + (f" ({inner})" if inner else "")


def _tool_addr(addr: str, op: str) -> str:
    """Адрес у тул-строки НЕ ставится, когда известен номер операции (владелец, 2026-09-21).

    Настоящая личность тула — `operation ID`: на нём стоит вся механика теней и им работает
    `erase tool`. Адрес сообщения нужен ПРОЗЕ, про неё мы и спрашиваем. Два конкурирующих
    номера в одной строке модель только путают.

    Исключение: номера операции нет (чужой тул без нашей шапки) — тогда адрес остаётся,
    иначе строка станет безымянной и сослаться на неё будет нечем.
    """
    return "" if op else addr


def strip_past_frames(text: str) -> str:
    """Вырезать НАШИ рамки из прежнего дистиллята, попавшего обратно в данные.

    Шапка рамки — чистая директива на двести слов, адресованная другому читателю: горячему
    агенту в живом промпте. Холодной модели она не только бесполезна, но и вредна — читается
    как указание ей. Содержимое же дистиллята ценно и остаётся целиком.

    ТОЛЬКО ТОЧНОЕ СОВПАДЕНИЕ (владелец, 2026-09-21). Не нашли строку дословно — не трогаем
    ничего: догадка на чужом тексте стоит дороже непочищенной шапки. Поэтому формы рамок
    ведутся списком и не чистятся — в живых базах лежат прогоны всех прошлых версий.

    Вместо вырезанной шапки встаёт короткая пометка: молчаливый вырез отнял бы у модели
    знание, что кусок уже сжат, и она взялась бы сжимать его заново.
    """
    body = str(text or "")
    if not body:
        return body
    heads, ends = _prompts.known_frame_lines()
    found = False
    for head in heads:
        if head in body:
            body = body.replace(head, _prompts.past_distillate_tag())
            found = True
    if not found:
        return str(text or "")
    for end in ends:
        body = body.replace(end, "")
    return body.strip()


def render_row(row, addr: str = "", *, name_of_call: dict | None = None,
               op_of_call: dict | None = None) -> str:
    """Одна строка базы → один блок текста для модели (у реплики с вызовами — несколько).

    Формы:
      реплика             `[m0007 USER]: …текст…`
      реплика с вызовами  текст, затем по блоку `[m0007 TOOL-call (terminal …)]: аргументы`
      ответ инструмента   `[m0008 TOOL-result (terminal operation ID 0001)]: …обрезок…`

    Подписываемся ИМЕНЕМ инструмента и номером операции, а не идентификатором вызова:
    идентификатор модели ничего не говорит, имя и номер говорят всё, и по номеру работает
    вся наша механика теней.
    """
    role = str((row or {}).get("role") or "unknown")
    text = strip_past_frames(_content(row))

    if role == "assistant" and text:
        # Ленивый импорт: их модуль тянет тяжёлые транзитивные зависимости. Отказ НЕ
        # молчит — след остаётся в `THINK_PROBLEM`: если `<think>`-блоки поедут в данные,
        # модель примет чужие черновые рассуждения за установленный факт, а искать причину
        # будет негде.
        global THINK_PROBLEM
        try:
            from agent.agent_runtime_helpers import strip_think_blocks
            text = strip_think_blocks(None, text)
        except Exception as exc:  # noqa: BLE001
            THINK_PROBLEM = f"agent.agent_runtime_helpers: {exc}"

    if role == "tool":
        call_id = str((row or {}).get("tool_call_id") or "")
        name = (name_of_call or {}).get(call_id, "")
        op = (op_of_call or {}).get(call_id, "") or (_OP_RE.search(text).group(1)
                                                     if _OP_RE.search(text) else "")
        # Номер уже стоит в ярлыке — в теле он дубль, и первая строка тела уходит на него.
        body = clip(_OP_RE.sub("", text, count=1).strip(), category(name))
        return _label(_tool_addr(addr, op), _pair_head("result", name, op)) + body

    parts = [_label(addr, role.upper())
             + clip(text, "head_tail", head=CLIP_PROSE, tail=CLIP_TAIL)]
    if role == "assistant":
        for tc in (row or {}).get("tool_calls") or []:
            call_id = str(tc.get("id") or "") if isinstance(tc, dict) else ""
            fn = tc.get("function") or {} if isinstance(tc, dict) else {}
            name = str(fn.get("name") or "?")
            args = _redact_compaction_text(fn.get("arguments") or "")
            op = (op_of_call or {}).get(call_id, "")
            parts.append(_label(_tool_addr(addr, op), _pair_head("call", name, op))
                         + clip(args, "head", head=CLIP_ARGS))
    return (chr(10) * 2).join(parts)


def render_block(rows, positions, addresses=None, kept=None) -> str:
    """Весь блок данных одним текстом, в порядке строк базы.

    Карта `id вызова → имя инструмента` строится ОДНИМ проходом вперёд: ответ инструмента
    всегда идёт после своего вызова, и без этой карты его пришлось бы искать назад по всему
    списку на каждой строке — квадрат на сессии в сотни сообщений.

    `kept` — позиции ПУЗЫРЬКОВ: ходов, которые останутся в контексте дословно. Над каждым
    таким встаёт ярлык той же формы `<[ … ]>`, что и вся наша разметка. Без ярлыка модель
    честно перескажет то, что и так уцелеет, и в контексте окажутся два экземпляра одного
    текста, причём второй хуже.
    """
    addresses = addresses or {}
    kept = set(kept or ())
    name_of_call: dict = {}
    op_of_call: dict = {}
    for row in rows:
        for tc in (row or {}).get("tool_calls") or []:
            if isinstance(tc, dict):
                fn = tc.get("function") or {}
                name_of_call[str(tc.get("id") or "")] = str(fn.get("name") or "")
        # Номер операции известен только из ОТВЕТА, а поставить его надо и в ярлык вызова —
        # значит карта строится наперёд, до сериализации, одним и тем же проходом.
        if str((row or {}).get("role") or "") == "tool":
            found = _OP_RE.search(str((row or {}).get("content") or ""))
            if found:
                op_of_call[str((row or {}).get("tool_call_id") or "")] = found.group(1)
    parts = []
    for i in positions:
        # ЗАКРЫВАЮЩАЯ СТРОКА ПРОШЛОЙ КОМПАКЦИИ В ДАННЫЕ НЕ ИДЁТ ВОВСЕ (2026-09-22).
        # Это не содержание, а наш структурный знак границы, адресованный ГОРЯЧЕМУ агенту в
        # живом промпте. Холодная модель читает его как указание себе — та же беда, ради
        # которой существует `strip_past_frames`, только этой функции он не по зубам: она
        # чистит концевые метки, лишь когда в ТОЙ ЖЕ строке нашлась шапка, а закрывающая
        # живёт отдельным сообщением и шапки не несёт. Вырезать текст мало — останется
        # пустое сообщение; поэтому строка не рендерится совсем.
        # МОСТИК ЧЕРЕДОВАНИЯ — ТОЖЕ (владелец, 2026-09-30: сброс служебных строк обязателен): это
        # реплика «[CONTEXT COMPACTION ENGINE: …]», вставленная ради правила ролей, а не разговор.
        # Узнаём одной функцией `_prompts.is_bridge` (метка `◆FT-DISTILL-BRIDGE_v1◆` и старая шапка).
        text = str((rows[i] or {}).get("content") or "")
        if _prompts.is_distill_end(text) or _prompts.is_bridge(text):
            continue
        body = render_row(rows[i], addresses.get(i, ""), name_of_call=name_of_call,
                          op_of_call=op_of_call)
        # Ярлык стоит НАД строкой: он объясняет то, что идёт следом, а не то, что позади.
        if i in kept:
            body = _prompts.kept_tag(addresses.get(i, "")) + chr(10) + body
        parts.append(body)
    return (chr(10) * 2).join(parts)


def stats(rows, positions) -> dict:
    """Из чего состоит блок. Единственное, что мы про него знаем ПРОГРАММНО."""
    out = {"rows": len(positions), "prose": 0, "tool_results": 0, "calls": 0}
    for i in positions:
        row = rows[i]
        role = str((row or {}).get("role") or "")
        if role == "tool":
            out["tool_results"] += 1
        elif str((row or {}).get("content") or "").strip():
            out["prose"] += 1
        out["calls"] += len((row or {}).get("tool_calls") or [])
    return out
