"""Механизм 1 `Vision04.1`: точечная заглушка вместо операции.

ЧТО ДЕЛАЕМ. Для каждого выбранного `tool_call_id`:
- в `assistant.tool_calls[i]` заменяем `arguments` короткой строкой;
- в парном `tool`-сообщении заменяем `content` короткой строкой.

Сами сообщения ОСТАЮТСЯ НА МЕСТЕ, и это не оптимизация, а безопасность: провайдер требует
результат на КАЖДЫЙ `tool_call`, поэтому пока мы только переписываем содержимое, пары
рваться нечем и дыре взяться неоткуда (`Vision04`, «Дизайн без дыр»). Физическое удаление
допустимо только целым батчем — это механизм 2, здесь его нет.

ТЕКСТ ЗАГЛУШКИ — по правилам `Vision04`, раздел «Текст заглушки»:
- описываем КОНЕЧНОЕ СОСТОЯНИЕ, не хронологию: «foo.py, 12-20: прочитан»;
- **свой** номер операции ставится — это разметка, координата, которой человек уже
  пользуется в панели; **чужие** номера не ставятся никогда — они выглядят как
  адресуемая ссылка, а для модели это мёртвый адрес;
- сырой `tool_call_id` в текст не попадает НИКОГДА (их код делает эту ошибку в промпте
  суммаризации, `Vision04.1` — не наследуем).

ПРО JSON В `arguments`. Позиция владельца — не заботиться о валидности специально. Но
`Recon18` нашёл в их компакторе усечение значений С СОХРАНЕНИЕМ валидного JSON, привязанное
к живому issue провайдера (`400 invalid function arguments json string`). Разночтение не
разрешено, поэтому здесь выбран безопасный вариант из `Plan13`: валидный JSON с одним полем.
Цена нулевая, а падение провайдера на длинной сессии — нет.
"""

from __future__ import annotations

import json
import re

# Формат конверта знает ОДИН модуль — тот, который его и строит. Двойная попытка импорта
# повторяет то, как это делают сами `file_ops`: плагин грузится и пакетом, и плоско.
try:
    from ..file_ops import _tool_text
except ImportError:  # pragma: no cover - плоская загрузка
    from file_ops import _tool_text  # type: ignore

# Имена режимов живут в `_select` — там же, где формат файла выбора. Здесь только пользуемся,
# чтобы отчёт назывался тем же словом, что кнопка в панели и элемент в файле.
from . import _select

# Метка, по которой движок узнаёт «это ручное применение выбора, а не запрос на
# суммаризацию». Ездит в `focus_topic` — том самом поле, куда их `/compress` кладёт свой
# сырой аргумент. Слово выбрано так, чтобы его не тронул их же разборщик границ
# (`parse_partial_compress_args` ищет `here [N]`, `up to here`, `--keep N`).
FOCUS_MARKER = "ctxdrop"

def is_manual_apply(focus_topic) -> bool:
    """Наша ли это метка. Ничего не парсим сверх этого: адреса лежат в файле-выборе."""
    return isinstance(focus_topic, str) and focus_topic.strip().startswith(FOCUS_MARKER)


NL = chr(10)

def result_stub(entry, op: str = "", original: str = "", tool: str = "") -> str:
    """Текст вместо `content` тул-результата: заголовок как есть, состояние, пустое тело.

    ФОРМАТ МЫ БОЛЬШЕ НЕ СОБИРАЕМ САМИ. Перерисовку делает `file_ops/_tool_text.py`, который
    формат и так строит для всех тулов. Здесь была вторая рукописная копия того же знания, и
    она дважды разошлась с оригиналом: сначала стёрла опознавательные признаки нашего ответа
    (панель перестала узнавать свои же строки, и выброшенные операции ИСЧЕЗАЛИ из `/ops`),
    потом писала строку без оформления — а заголовок `read_file` завёрнут в синтаксис
    комментариев открытого файла (`#`, `//`, `<!-- -->`, `◆` когда своего нет).

    `entry` — запись леджера, сегодня не нужна: состояние читается из самого заголовка, а не
    из нашей записи. Параметр оставлен, потому что механизм 2 (удаление целого мини-хода)
    будет отвечать по-другому и запись ему понадобится.

    Строку «выброшено человеком» убрали по решению владельца 2026-09-09: это была отсебятина,
    которую она не подтверждала, и с явным `State:` она вдобавок дублирует смысл.
    """
    del entry   # см. докстринг: состояние живёт в заголовке, а не в нашей записи
    # `op` прокидываем: у ответа БЕЗ нашего конверта заголовка нет вовсе, и адрес взять
    # больше негде — иначе строка после удаления перестала бы находиться по номеру.
    return _tool_text.as_removed(original, op=(op or "").strip(), tool=(tool or "").strip())


def args_stub(tool_name: str, op: str = "") -> str:
    """Строка вместо `arguments` вызова. Валидный JSON с одним полем — см. шапку модуля.

    ТЕКСТ — ТО ЖЕ СЛОВО, ЧТО В ЗАГОЛОВКЕ ОТВЕТА. Прежняя русская фраза «вызов read_file
    выброшен из контекста человеком» была моей отсебятиной и вдобавок мешала языки: модель
    видит английский токен в ответе и русское объяснение в аргументах одного и того же
    вызова.

    Двух формулировок владельца я не взял, и обе по её же логике. «by compact» приписало бы
    правку компакции — ровно то различие, на котором она настаивала («это не их компакция
    кстати а всё наше»). «Old Operation ID» назвало бы номер устаревшим, а он остаётся
    действующим адресом: по нему панель сшивает строку, и по нему человек спрашивает, что
    здесь было.
    """
    op = (op or "").strip()
    note = _tool_text.STATE_REMOVED
    if op:
        note += f". {_tool_text.OP_LABEL}: {op}"
    # Ключ нарочно не похож на настоящий параметр. Шаблон локальной модели рендерит ключи
    # JSON как `<parameter=имя>`, и живой прогон 2026-09-08 показал наш прежний `note`
    # ровно в этом виде — то есть у `search_files` появлялся аргумент, которого нет в схеме,
    # и модель могла принять его за настоящий. Двойное подчёркивание читается как «это не
    # аргумент» на любом шаблоне.
    return json.dumps({"__dropped__": note}, ensure_ascii=False)


def lint_pairs(messages) -> list:
    """Свой минимальный линт целостности: у каждого `tool_call` есть результат и обратно.

    Свой, а не их приватный `_classify_tool_call_orphans`: `Recon24` нашёл, что host всё
    равно гоняет generic-санитайзер после ЛЮБОГО движка перед каждым запросом, значит их
    приватная функция — не единственная защита, и seam-риск ради неё не оправдан. Зато свой
    линт даёт нам контроль над ТЕКСТОМ на месте дыры, а не только над фактом дыры.

    Возвращает список проблем; пустой список = пары целы.
    """
    called: dict[str, int] = {}
    answered: set[str] = set()
    for idx, msg in enumerate(messages or []):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            for call in msg.get("tool_calls") or []:
                call_id = _call_id(call)
                if call_id:
                    called.setdefault(call_id, idx)
        elif role == "tool":
            call_id = msg.get("tool_call_id")
            if call_id:
                answered.add(str(call_id))

    problems = []
    for call_id, idx in called.items():
        if call_id not in answered:
            problems.append({"kind": "call_without_result", "tool_call_id": call_id,
                             "at": idx})
    for msg_idx, msg in enumerate(messages or []):
        if isinstance(msg, dict) and msg.get("role") == "tool":
            call_id = str(msg.get("tool_call_id") or "")
            if call_id and call_id not in called:
                problems.append({"kind": "result_without_call", "tool_call_id": call_id,
                                 "at": msg_idx})
    return problems


def _call_id(call) -> str:
    """`id` элемента `tool_calls`, чем бы этот элемент ни оказался.

    В истории он может быть и словарём, и объектом SDK — их же сериализатор для
    суммаризации на этом спотыкается и печатает `name(...)` вместо аргументов
    (`Vision04.1`). Нам спотыкаться нельзя: не узнав `id`, мы не найдём пару.
    """
    if isinstance(call, dict):
        return str(call.get("id") or "")
    return str(getattr(call, "id", "") or "")


def _fn_name(call) -> str:
    if isinstance(call, dict):
        fn = call.get("function")
        if isinstance(fn, dict):
            return str(fn.get("name") or "")
        return str(getattr(fn, "name", "") or "")
    fn = getattr(call, "function", None)
    return str(getattr(fn, "name", "") or "")


def apply_stubs(messages, items, ledger_index=None) -> tuple[list, dict]:
    """Применить механизм 1. Возвращает `(новый список, отчёт)`.

    Новый список — ВСЕГДА новый объект с новыми словарями для тронутых сообщений: сообщения
    из живой истории мутировать на месте нельзя, их держит гейтвей под своим локом, и
    правка in-place обошла бы его проверку версии.

    ДВЕ ПОЛОВИНЫ ОПЕРАЦИИ РЕШАЮТСЯ НЕЗАВИСИМО. Это исправление 2026-09-09 по живому прогону:
    раньше судьбу АРГУМЕНТОВ определял результат — если результат переписывать было нечего,
    аргументы оставались как есть. Для чтения это верно (дорогое лежит в ответе), а для
    ЗАПИСИ ровно наоборот: `write_file`/`patch` отвечают одной строкой подтверждения, а всё
    содержимое файла лежит в `arguments` вызова. Владелец: «оно отказывается удалять запись,
    но её нужно удалять, потому что в её вызове много строк». Поэтому:

        аргументы  → заглушка всегда, когда операция выбрана
        результат  → заглушка, если у ответа есть тело; иначе только штамп состояния

    Пара вызов/результат при этом не рассогласуется: оба сообщения остаются на месте, у
    результата в заголовке стоит `State: removed_from_context`, а в аргументах — тот же
    токен. Модель видит «вызов был, его содержимое убрано человеком, вот подтверждение».

    ЭКОНОМИЯ СЧИТАЕТСЯ ПО ОБЕИМ ПОЛОВИНАМ. До этой правки в отчёт шёл только результат, то
    есть у записи человек видел «освободилось 0» при удалённом файле на десять килобайт.

    Отчёт разделяет три исхода, потому что для человека это три разных факта: `stubbed`
    (нашли и заменили), `not_found` (адреса в истории нет — не та сессия или уже переписано),
    `problems` (линт нашёл дыру: мы её НЕ создавали, но молчать нельзя).
    """
    wanted = {}
    for item in items or []:
        call_id = str((item or {}).get("tool_call_id") or "")
        if call_id:
            wanted[call_id] = item
    index = ledger_index or {}
    # Имя тула по `id` вызова — для заглушки ЧУЖОГО ответа, у которого своей шапки нет.
    tool_of = {}
    for msg in messages or []:
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            for call in list(msg.get("tool_calls") or []):
                tool_of.setdefault(_call_id(call), _fn_name(call))

    # Первый проход — ТОЛЬКО подсчёт: сколько освободит каждая половина. Решение
    # «трогаем или нет» принимается по СУММЕ, поэтому его нельзя принять, пока не увидены
    # оба сообщения пары, а лежат они в списке порознь.
    plan = {}
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "tool":
            call_id = str(msg.get("tool_call_id") or "")
            if call_id not in wanted:
                continue
            op = _op_of(wanted[call_id], index, call_id)
            before = str(msg.get("content") or "")
            after = result_stub(None, op, before, tool_of.get(call_id, ""))
            plan.setdefault(call_id, {"op": op, "result": 0, "args": 0})
            plan[call_id]["result"] = len(before) - len(after)
        elif role == "assistant" and msg.get("tool_calls"):
            for call in list(msg.get("tool_calls") or []):
                call_id = _call_id(call)
                if call_id not in wanted:
                    continue
                op = _op_of(wanted[call_id], index, call_id)
                before = str(_fn_args(call) or "")
                after = args_stub(_fn_name(call), op)
                plan.setdefault(call_id, {"op": op, "result": 0, "args": 0})
                plan[call_id]["args"] = len(before) - len(after)

    # НАЗВАНА — УДАЛЯЕТСЯ. Ни порога, ни оценки «а стоит ли», ни отказа (владелец,
    # 2026-09-09: «я не хочу встретить подобный отказ ни у одной операции — команда должна
    # работать; мне всё равно если там нечего удалять, оно должно удалить это нечего»).
    # Осталась ровно одна причина ничего не сделать, и она не оценочная: адреса нет в списке
    # вовсе (`not_found`) — тогда трогать просто нечего.
    #
    # Цифры экономии при этом считаются и печатаются как есть, включая ноль и минус: человек
    # видит, что освободилось, а не наше мнение о том, стоило ли.
    touch, skipped = set(plan), []

    out, stubbed = [], []
    for msg in messages or []:
        if not isinstance(msg, dict):
            out.append(msg)
            continue

        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            touched, new_calls = False, []
            for call in list(msg.get("tool_calls") or []):
                call_id = _call_id(call)
                if call_id in touch:
                    op = _op_of(wanted[call_id], index, call_id)
                    new_calls.append(_with_args(call, args_stub(_fn_name(call), op)))
                    touched = True
                else:
                    new_calls.append(call)
            out.append({**msg, "tool_calls": new_calls} if touched else msg)
            continue

        if role == "tool":
            call_id = str(msg.get("tool_call_id") or "")
            if call_id in touch:
                p = plan[call_id]
                before = str(msg.get("content") or "")
                text = result_stub(None, p["op"], before, tool_of.get(call_id, ""))
                out.append({**msg, "content": text} if text != before else msg)
                # `chars_before/after` — по ОПЕРАЦИИ целиком, обе половины: человек считает
                # освободившееся место, а не строки в базе.
                stubbed.append({"tool_call_id": call_id, "op": p["op"],
                                "chars_before": len(before),
                                "chars_after": len(text),
                                # `saved` — по ОПЕРАЦИИ целиком: у записи всё лежит в
                                # аргументах, и отчёт только по ответу показывал бы ноль.
                                "saved": p["result"] + p["args"],
                                "saved_result": p["result"], "saved_args": p["args"]})
                continue

        out.append(msg)

    found = {row["tool_call_id"] for row in stubbed} | {row["tool_call_id"] for row in skipped}
    report = {
        "mechanism": _select.MODE_CONTENT,
        # `applied` — канонический список тронутого, одинаковый у всех механизмов: по нему
        # отчитывается команда и маршрут, и им не надо знать, какой ключ у какого механизма.
        # `stubbed` остаётся своим именем этого механизма.
        "applied": stubbed,
        "stubbed": stubbed,
        "skipped": skipped,
        "not_found": [call_id for call_id in wanted if call_id not in found],
        "problems": lint_pairs(out),
        "chars_saved": sum(row["saved"] for row in stubbed),
    }
    return out, report


def _without_tool_calls(msg: dict) -> dict:
    """Копия сообщения БЕЗ ключа `tool_calls`, а не с пустым списком.

    Разница не косметическая, и половины расходятся именно здесь. На половине БАЗЫ пустой
    список безопасен сам собой: их `_message_row_params` биндит
    `json.dumps(tool_calls) if tool_calls else None`, то есть `[]` уезжает в колонку как NULL.
    А на половине ПАМЯТИ ГЕЙТВЕЯ между нами и следующим запросом нет ни одного writer'а —
    словарь уйдёт к провайдеру как есть, и `tool_calls: []` это форма, которую ни их
    конвертер, ни провайдеры принимать не обязаны. Поэтому ключ убираем сами и одинаково для
    обеих половин: меньше поводов помнить, какая из них сейчас в руках.
    """
    return {k: v for k, v in msg.items() if k != "tool_calls"}


def erase_tools(messages, items, ledger_index=None, *,
                drop_prose: bool = False) -> tuple[list, dict]:
    """Применить механизм 2 (`erase tool`). Возвращает `(новый список, отчёт)`.

    `drop_prose` — уносить ли текст опустевшей строки ассистента. По умолчанию
    НЕТ: человек выбрал операцию, а не слова модели. Механизм 3 (`erase turn`)
    ставит его, потому что там человек выбрал ход, а проза шага — его часть.
    Флаг, а не второй механизм: инвариант провайдера («у каждого вызова есть
    результат и наоборот») чинится одним кодом, и второй его копии, которая
    однажды починит его иначе, не появляется.

    ЕДИНИЦА — ПАРА, А НЕ БАТЧ. Это исправление владельца 2026-09-09, и оно того стоило: я
    утверждал, что удалять можно только весь мини-ход целиком, потому что «провайдер требует
    результат на каждый `tool_call`». Из этого требования следует совсем другое — что удалять
    надо ОБЕ половины пары. Владелец: «не вижу, почему нужно удалять весь батч, ход не
    рвётся». Так и есть: инвариант («у каждого вызова есть результат и наоборот») сохраняется,
    если вместе с tool-строкой переписать массив `tool_calls` у строки ассистента. Массив
    общий на все тулы хода — вот единственное, что за «батчем» на самом деле стояло.

    ЧТО БЫВАЕТ С ОПУСТЕВШЕЙ СТРОКОЙ АССИСТЕНТА — вопрос владельца («или может получится
    пустой тул-ход?»), и ответ у Hermes уже есть, в их же `agent/message_sanitization.py`:
    сообщение, несущее `tool_calls`, или tool-результат они НЕ удаляют никогда («deleting
    orphans the paired ``tool_call_id`` → HTTP 400»), а опустевшее в остальном сообщение —
    удаляют. Отсюда наши три случая:

        остались другие вызовы          → строка живёт, массив переписан
        вызовов не осталось, текст есть → строка живёт как обычный ответ ассистента,
                                          ключ `tool_calls` убран целиком (не `[]`)
        вызовов не осталось, текста нет → строка уходит: это ровно их «otherwise-empty»,
                                          и держать её значило бы послать пустой ход

    СЛЕДА НЕ ОСТАЁТСЯ ВОВСЕ, и это замысел, а не упущение (владелец: «удалили = пусто, нету,
    чисто»). Единственный след — дырка в нумерации операций: номера последовательные, и
    пропуск честно означает «здесь что-то было».
    """
    wanted = {}
    for item in items or []:
        call_id = str((item or {}).get("tool_call_id") or "")
        if call_id:
            wanted[call_id] = item
    index = ledger_index or {}

    # Первый проход — только подсчёт: сколько освободит каждая половина пары. Считаем ПОЛНЫЙ
    # размер, а не разницу с заглушкой: здесь не остаётся ни заголовка, ни штампа.
    plan = {}
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "tool":
            call_id = str(msg.get("tool_call_id") or "")
            if call_id not in wanted:
                continue
            plan.setdefault(call_id, {"op": _op_of(wanted[call_id], index, call_id),
                                      "result": 0, "args": 0})
            plan[call_id]["result"] = len(str(msg.get("content") or ""))
        elif role == "assistant" and msg.get("tool_calls"):
            for call in list(msg.get("tool_calls") or []):
                call_id = _call_id(call)
                if call_id not in wanted:
                    continue
                plan.setdefault(call_id, {"op": _op_of(wanted[call_id], index, call_id),
                                          "result": 0, "args": 0})
                plan[call_id]["args"] = len(str(_fn_args(call) or ""))

    touch = set(plan)
    out, erased = [], []
    shells_removed, shells_emptied = [], []
    # Проза унесённых оболочек. Список из одного числа, чтобы считать её там же,
    # где решается судьба строки, а не вторым проходом по уже изменённому списку.
    prose_saved = [0]
    # САМИ удалённые сообщения, а не только их адреса. Нужны предохранителю коммита: он
    # сверяет наш список с активными строками базы и отказывает, если чего-то не хватает
    # (потеря данных 2026-09-08). Удаление по замыслу тоже создаёт нехватку, и отличить его
    # от потери можно ровно одним способом — назвать удалённое явно.
    removed_msgs = []

    for msg in messages or []:
        if not isinstance(msg, dict):
            out.append(msg)
            continue

        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            calls = list(msg.get("tool_calls") or [])
            kept = [call for call in calls if _call_id(call) not in touch]
            if len(kept) == len(calls):
                out.append(msg)
                continue
            gone = [_call_id(call) for call in calls if _call_id(call) in touch]
            if kept:
                out.append({**msg, "tool_calls": kept})
                continue
            if str(msg.get("content") or "").strip() and not drop_prose:
                # Текст ассистента — его собственные слова, а не содержимое операции. Человек
                # выбрал операцию, а не реплику, поэтому реплика остаётся.
                out.append(_without_tool_calls(msg))
                shells_emptied.append({"calls": gone})
            else:
                # Ни вызовов, ни текста — держать нечего и незачем. При
                # `drop_prose` сюда же попадает и оболочка с текстом: её слова
                # входят в ход, который человек выбрал целиком.
                prose = len(str(msg.get("content") or ""))
                shells_removed.append({"calls": gone, "prose_chars": prose}
                                      if prose else {"calls": gone})
                removed_msgs.append(msg)
                prose_saved[0] += prose
            continue

        if role == "tool":
            call_id = str(msg.get("tool_call_id") or "")
            if call_id in touch:
                p = plan[call_id]
                removed_msgs.append(msg)
                erased.append({"tool_call_id": call_id, "op": p["op"],
                               "chars_before": p["result"] + p["args"],
                               "chars_after": 0,
                               "saved": p["result"] + p["args"],
                               "saved_result": p["result"], "saved_args": p["args"]})
                continue

        out.append(msg)

    # Адрес, у которого не нашлось НИ tool-строки, НИ записи в массиве вызовов. Пара, у
    # которой нашлась только одна половина, к отчёту об удалении относится: вторая половина
    # уже отсутствовала до нас, и её удаление только починило бы пару.
    found = {row["tool_call_id"] for row in erased}
    found |= {cid for shell in shells_removed + shells_emptied for cid in shell["calls"]}
    # Адрес нашёлся, а активной строки под ним НЕТ. Это ДОКАЗАННОЕ отсутствие, и оно отличается
    # от «номер не удалось адресовать вовсе»: там мы просто не знаем, есть строка или нет.
    # Панель по этому списку снимает карточку с экрана (решение владельца 2026-09-09: «если на
    # карточке нажат erase, даже если нечего удалять из контекста, удалить саму карточку, чтобы
    # она не портила вид»), поэтому список именно номеров операций, а не адресов.
    absent = [str((wanted[call_id] or {}).get("op") or "")
              for call_id in wanted if call_id not in found]
    report = {
        "mechanism": _select.MODE_TOOL,
        "applied": erased,
        "erased": erased,
        "absent": [op for op in absent if op],
        # Оболочки хода, которых больше нет, и оболочки, потерявшие вызовы, но сохранившие
        # текст. Отдельными списками, потому что первое — ЕДИНСТВЕННОЕ, чего человек не
        # выбирал, а получил: он назвал операцию, а ушла ещё и строка ассистента. Молчать об
        # этом нельзя, отговариваться «так надо» — тоже.
        "shells_removed": shells_removed,
        "shells_emptied": shells_emptied,
        # Для предохранителя коммита — см. `removed_msgs` выше.
        "removed": removed_msgs,
        "skipped": [],
        "not_found": [call_id for call_id in wanted if call_id not in found],
        "problems": lint_pairs(out),
        "chars_saved": sum(row["saved"] for row in erased) + prose_saved[0],
        # Символы прозы, унесённой вместе с ходом. Ноль у `erase tool` всегда:
        # там проза остаётся.
        "prose_chars": prose_saved[0],
    }
    return out, report


def step_calls(messages, items) -> tuple[list, list]:
    """Расширить выбор до ЦЕЛЫХ шагов. `(items, добавленные tool_call_id)`.

    Шаг — реплика ассистента и ответы на ЕЁ вызовы; связь по `tool_call_id`, а не
    по соседству (замер живой базы 2026-09-09: 6956 шагов из 6957 сплошные, но
    единственный вызов без ответа по позиции утащил бы чужую строку).

    Добавленные адреса возвращаются ОТДЕЛЬНО: человек отметил одну операцию, а
    уйдёт несколько, и об этом надо сказать до применения, а не после.
    """
    wanted = {str((i or {}).get("tool_call_id") or ""): i for i in (items or [])}
    wanted.pop("", None)
    extra = []
    out = list(items or [])
    for msg in messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        calls = [_call_id(c) for c in (msg.get("tool_calls") or [])]
        calls = [c for c in calls if c]
        if not any(c in wanted for c in calls):
            continue
        for call_id in calls:
            if call_id in wanted:
                continue
            wanted[call_id] = {"tool_call_id": call_id}
            out.append(wanted[call_id])
            extra.append(call_id)
    return out, extra


def erase_turn(messages, items, ledger_index=None) -> tuple[list, dict]:
    """Применить механизм 3 (`erase turn`). Возвращает `(новый список, отчёт)`.

    ШАГ, А НЕ ПОЛНЫЙ TURN. Полный turn в их смысле кончается возвратом управления
    человеку; здесь уходит реплика ассистента со ВСЕМИ её вызовами и ответами —
    именно это владелец имеет в виду, говоря «удалить ход» (поправка 1 к
    контракту записей).

    Механизм тот же, что у `erase tool`, — разница только в ШИРИНЕ выбора, и это
    не экономия строк, а свойство: инвариант провайдера («у каждого вызова есть
    результат и наоборот») чинится одним и тем же кодом, и второй его копии,
    которая однажды починит его иначе, не появляется.

    ОТЛИЧИЕ ОДНО: реплика ассистента уходит ВМЕСТЕ С ТЕКСТОМ. У `erase tool`
    текст остаётся — человек выбрал операцию, а не слова модели. Здесь он выбрал
    ход, а проза шага — его часть; владелец 2026-09-09: «кроме карточек есть ещё
    реплики модели, которые стоят вместе с вызовами тула, их нужно тоже удалять».
    """
    expanded, extra = step_calls(messages, items)
    out, report = erase_tools(messages, expanded, ledger_index, drop_prose=True)
    report["mechanism"] = _select.MODE_TURN
    report["widened"] = extra
    return out, report


def _op_of(item, index, call_id: str) -> str:
    """Наш номер операции: из выбора, а если там нет — из записи леджера."""
    op = str((item or {}).get("op") or "")
    return op or str((_entry_for(index, call_id, op) or {}).get("op") or "")


def _fn_args(call):
    """`arguments` вызова, хоть у словаря, хоть у объекта SDK. См. `_fn_name`."""
    fn = call.get("function") if isinstance(call, dict) else getattr(call, "function", None)
    if isinstance(fn, dict):
        return fn.get("arguments")
    return getattr(fn, "arguments", None)


def _entry_for(index, call_id: str, op: str = ""):
    """Запись леджера по адресу ИЛИ по номеру операции. None — легально.

    Два ключа, потому что ни один не гарантирован: `tool_call_id` пропадает, когда их
    contextvars переезжают (случилось 2026-09-08), а `op` — когда счётчик был недоступен
    в момент вызова. Заглушка должна быть содержательной в обоих случаях.
    """
    entry = index.get(call_id) if call_id else None
    if entry is None and op:
        entry = index.get(f"op:{op.strip().lower()}")
    return entry


def _with_args(call, arguments: str):
    """Копия элемента `tool_calls` с подменёнными `arguments`.

    Объект SDK копируем в словарь: транспорт принимает словарь, а мутировать чужой объект,
    который может быть общим с историей гейтвея, нельзя.
    """
    if isinstance(call, dict):
        fn = call.get("function")
        new_fn = ({**fn, "arguments": arguments} if isinstance(fn, dict)
                  else {"name": _fn_name(call), "arguments": arguments})
        return {**call, "function": new_fn}
    return {
        "id": _call_id(call),
        "type": getattr(call, "type", "function") or "function",
        "function": {"name": _fn_name(call), "arguments": arguments},
    }


def ledger_index(session: str | None) -> dict:
    """`tool_call_id -> запись леджера` для этой сессии. Пустой словарь — легально.

    Нужен ровно для ТЕКСТА заглушки (путь, диапазон, глагол) и для перевода `Operation ID`
    в адрес. Само применение от леджера не зависит: без него заглушка просто короче.
    """
    entries = _entries(session)
    index = {}
    for entry in entries:
        # СОБЫТИЯ СЮДА НЕ ПУСКАЕМ. Запись о сообщении несёт `tool_call_id` — тот же,
        # что у операции, — и по правилу «последняя запись на вызов побеждает» она
        # затёрла бы операцию, а с ней путь и диапазон в тексте заглушки. Тот же
        # промах в `/reads` 2026-09-09 покрасил серым весь стакан.
        if _is_event(entry):
            continue
        call_id = str(entry.get("tool_call_id") or "")
        if call_id:
            # Последняя запись на один вызов побеждает: у вложенных операций
            # (`execute_code`) `tool_call_id` общий с родителем, и заглушка описывает
            # именно родительский вызов — он и есть то, что физически лежит в контексте.
            index[call_id] = entry
        op = str(entry.get("op") or "").strip().lower()
        if op:
            # ВТОРОЙ ключ, по номеру операции. Без него заглушка теряет путь и диапазон
            # ровно в том случае, который и случился живьём: `tool_call_id` в записях
            # отсутствует (их contextvars переехали), адрес мы взяли из истории, а текст
            # обогатить нечем — и человек получает «[выброшено]» без единого факта о том,
            # что именно выбросили.
            index.setdefault(f"op:{op}", entry)
    return index


# The label is the dictionary's (`_tool_text.OP_LABEL` = `_vocab.COMMON["op"]`, Plan28).
_OP_IN_HEADER = re.compile(re.escape(_tool_text.OP_LABEL)
                           + r":\s*([0-9a-fA-F]{4,}(?:x[0-9a-fA-F]{2,4})?)")


def ops_from_history(messages) -> dict:
    """`op -> tool_call_id`, прочитанное ИЗ САМОЙ ИСТОРИИ. Основной способ адресации.

    Работает потому, что наш номер напечатан В ТЕКСТЕ ответа тула (Plan09), а у
    tool-сообщения есть свой `tool_call_id`. То есть история сама содержит обе половины
    связи, и ни леджер, ни их contextvars для перевода номера в адрес не нужны вовсе.

    Почему это ЛУЧШЕ леджера именно для нашей задачи, а не просто «тоже работает»:
    - выбрасывать можно только то, что физически лежит в истории. Если номера в ней нет,
      то и выбрасывать нечего — «не нашли» и «нечего делать» здесь один и тот же факт;
    - не зависит от `_observability_context`. Он молча сломался при обновлении Hermes
      (переезд их contextvars, найдено живьём 2026-09-08), и записи леджера остались без
      `tool_call_id` — а этот путь в тот же момент продолжал работать;
    - показывает РОВНО тот адрес, который видела модель, а не тот, который мы записали.

    Ограничение честное: если содержимое уже переписано (их компакцией или нашей же
    заглушкой), заголовка в нём нет и номер отсюда не восстановить. Но тогда данных в
    контексте тоже нет — выбрасывать нечего.

    Скан узкий, первые четыре строки — как в панели (`plugin_api.py::_op_from_header`):
    заголовок стоит сверху по построению, а `Operation ID` где-то ниже это уже СОДЕРЖИМОЕ
    прочитанного файла, чужой номер, и принять его за адрес было бы ошибкой.
    """
    out = {}
    for msg in messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        call_id = str(msg.get("tool_call_id") or "")
        if not call_id:
            continue
        for line in str(msg.get("content") or "").split("\n", 4)[:4]:
            found = _OP_IN_HEADER.search(line)
            if found:
                out.setdefault(found.group(1).lower(), call_id)
                break
    return out


def ops_to_call_ids(session: str | None, ops, messages=None) -> tuple[list, list, list]:
    """`Operation ID` → элементы выбора. `(найденные, нет нигде, нет адреса)`.

    Два источника, и порядок важен. СНАЧАЛА история (`ops_from_history`) — она и есть
    правда о том, что лежит в контексте. Леджер — ВТОРОЙ, для случая, когда номер в
    истории уже не читается: он подтверждает, что операция вообще была, и тогда мы можем
    честно сказать «была, но её содержимого в контексте уже нет».

    ТРИ исхода, а не два, и это оплачено живым прогоном 2026-09-08. Владелец набрала
    `/ctx-drop 001e`, получила «номер не найден» и предположила, что дело в
    шестнадцатеричной записи. Дело было в другом: номер нашёлся, а `tool_call_id` у записи
    леджера отсутствовал — их contextvars переехали, наш `_observability_context` читал
    старое место и молча отдавал пустоту. «Не найден» и «найден, но адресовать нечем» —
    разные факты и разные починки, поэтому теперь они разделены на всём пути до ответа.
    """
    from_history = ops_from_history(messages)

    by_op = {}
    for entry in (_entries(session) or []):
        op = str(entry.get("op") or "").strip()
        if op:
            by_op.setdefault(op.lower(), entry)

    items, missing, no_address = [], [], []
    for raw in ops or []:
        key = str(raw).strip().lower()
        entry = by_op.get(key)
        call_id = from_history.get(key) or str((entry or {}).get("tool_call_id") or "")
        if call_id:
            items.append({"tool_call_id": call_id,
                          "op": str((entry or {}).get("op") or raw).strip(),
                          "mode": _select.MODE_CONTENT,
                          # ОТКУДА взят адрес. Диагностика, а не украшение: «нашли в
                          # истории» и «нашли только в леджере» ведут к разным исходам, и
                          # если применение потом не сработало, первым делом надо знать
                          # именно это (живьём 2026-09-08 ответ «выбрасывать нечего» не
                          # объяснял ничего, потому что источник не назывался).
                          "src": "история" if from_history.get(key) else "леджер"})
        elif entry is not None:
            no_address.append(str(raw).strip())
        else:
            missing.append(str(raw).strip())
    return items, missing, no_address


def _is_event(entry) -> bool:
    """Событие (надгробие, сообщение, состав), а не операция. Одна проверка на всех.

    Своей копии условия здесь нет намеренно: различение родов живёт в `_ledger`, и
    второй способ различать — это ровно то, чем читатели расходятся молча.
    """
    try:
        from ..file_ops._ledger import is_event
        return is_event(entry)
    except Exception:  # noqa: BLE001 - леджер недоступен: тогда событий и не бывает
        return isinstance(entry, dict) and bool(entry.get("event"))


def _entries(session: str | None):
    try:
        from ..file_ops._ledger import read_ledger
        return read_ledger(session) or []
    except Exception:
        return []


def index_messages(session: str | None, rows) -> dict:
    """Догнать адресацию сообщений этой сессии. Ошибки наружу не выходят.

    Зовут ДО правки, и это не мелочь: `erase` строки удаляет, а неадресованная
    строка после удаления адреса уже не получит — координаты не останется. Так
    ссылка на стёртое сообщение существует и после стирания, ровно как надгробие
    существует после стирания операции.

    Индексируем АКТИВНЫЙ набор: он и есть то, из чего собирается промпт, а стакан
    по `Vision05` — зеркало промпта, не архива.
    """
    try:
        from ..file_ops import _msgindex
        out = _msgindex.index_session(session, rows, _entries(session))
        # `by_msg` несёт СТРОКИ БАЗЫ целиком — в отчёт, который уедет в JSON маршрута
        # и в журнал применений, им дороги нет: контракт §7, полей неограниченного
        # размера в записи не бывает.
        return {k: v for k, v in out.items() if k != "by_msg"}
    except Exception:  # noqa: BLE001 - индекс это свидетельство, а не сама работа
        return {}


def note_erased(session: str | None, ops, by: str = "erase tool") -> int:
    """Поставить надгробия названным операциям. Возвращает, сколько записано.

    ОТКАЗОУСТОЙЧИВО ПО ЗАМЫСЛУ: ни одна ошибка отсюда не выходит. Зовут это ПОСЛЕ успешного
    коммита — строк в контексте уже нет, — и падение здесь превратило бы состоявшуюся
    необратимую правку в «не сработало». Надгробие это свидетельство, а не сама операция:
    без него панель просто снимет карточку молча, как снимала до сегодня.
    """
    written = 0
    try:
        from ..file_ops._ledger import note_erased as _note
    except Exception:  # noqa: BLE001 - леджер недоступен: применение всё равно состоялось
        return 0
    for op in ops or []:
        op = str(op or "").strip()
        if not op:
            continue
        try:
            _note(session, op, by)
            written += 1
        except Exception:  # noqa: BLE001
            continue
    return written
