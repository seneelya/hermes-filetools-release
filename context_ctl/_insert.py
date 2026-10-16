"""Вставка в ленту — ОДИН механизм применения к ленте сессии (вынесен из `_compact.py` 2026-10-07).

ЧТО ЗДЕСЬ. Выбор по адресам (`m0007`, диапазоны) и достройка до пар, план пролёта (пузырьки,
правило владения `_pairs`), сборка новой ленты (вставка на место пролёта, скобка, мостики
чередования), проверки, запись с ожиданием конца хода (путь A) или отдача ленты автомату (путь B),
журнал поглощения, карта ленты. Замок `_busy` — ОДИН на любую запись в ленту.

КТО ЗОВЁТ. Компакция (`_compact`: опрос модели даёт дистиллят), окно LLMOps (`Plan31`: готовый
текст человека), `erase` и правка (`_command`: `_rebuild` без вставок или с `patch`). Владелец,
2026-10-07: «механизм применения к ленте у них одинаковый — это факт»; называть его компакцией
неверно, это вставка данных в ленту.

ЧЕМ ВСТАВЛЯТЬ — решает вызывающий: ответ `{text, wrap?}` на пролёт. `wrap` — `compaction` (наш
дистиллят, по умолчанию) / `summarisation` / `none` (`_prompts.wrap_ready`).

Правила выбора (пролёт съедается целиком, дырка режет пробег, выбор человека не обсуждается) —
в шапке `_compact.py`, где они родились; действуют они здесь.
"""
from __future__ import annotations

import re
import threading
import time

from . import (_addresses, _alternation, _apply, _pairs, _progress, _prompts, _settings, _signal,
               _stubs, _tape)


# ЗАМОК ПОВТОРНОГО ВХОДА. Hermes обрывает слеш-команду по своему таймауту (30 с) и может
# отправить её ЗАНОВО — поймано живьём 2026-09-16: пристрелка шла 96 секунд, и её запустило
# дважды, два прогона одновременно заняли два слота сервера.
#
# Для пристрелки это просто мусор в логах. Для КОМПАКЦИИ это порча данных: первый прогон
# коммитит сводку, второй перечитывает базу заново, видит там уже сжатую сессию — и сжимает
# ЕЁ ЖЕ, съедая собственную сводку. Предохранитель коммита такое не ловит: с его точки
# зрения оба списка честные.
_busy = threading.Lock()
# АДРЕСА ШЕСТНАДЦАТЕРИЧНЫЕ. `_ledger.next_msg` форматирует `f"m{n:04x}"` — это записано у
# него прямо над `_MSG_WIDTH` («Hex, so the address stays short»), и в живой сессии рядом с
# `m0009` лежит `m000a`. Разбирать их десятичными — ровно та ошибка, которая НЕ падает:
# всё с буквой просто не разбирается, диапазон молча сжимается (поймано живьём 2026-09-16 —
# 99 адресов из 235), и человек получает сводку не по тому, что назвал.
_HEX = "[0-9a-f]+"
_ONE = re.compile(rf"^m?0*({_HEX})$", re.I)
_RANGE = re.compile(rf"^m?0*({_HEX})\s*-\s*m?0*({_HEX})$", re.I)
def _addr(number: int) -> str:
    """Число → адрес в том же виде, в каком его показывает зеркало (`m003f`)."""
    return f"m{number:04x}"
# Сколько сообщений максимум за один заход. Не «защита от дурака», а защита от того, что
# блок данных перестанет помещаться в окно вспомогательной модели.
#
# ПРЕЖНЯЯ ПРИЧИНА ЗДЕСЬ БЫЛА НЕВЕРНА (правка 2026-09-22). Стояло, что вход режет их
# `_bound_summary_input` по 160K символов. Проверено: `_SUMMARY_INPUT_MAX_CHARS = 160_000`
# живёт в `agent/context_compressor.py`, то есть на ИХ пути `/compress`, с которого мы ушли
# 2026-09-22 вместе с их промптом. Наша дверь (`context_ctl/_llm.py::ask_structured`) не
# режет вход нигде. Поэтому тихой обрезки хвоста у нас нет вовсе: переполнение окна даст
# отказ провайдера — громкий, но посреди уже оплаченного опроса. Отсюда порог и замер
# объёма, который печатается в отчёт прогона.
MAX_MESSAGES = 400
def parse_spec(raw: str) -> tuple[set, list]:
    """`"m0001-m0010,m0012"` → (`{1..10, 12}`, список непонятого).

    Непонятое НЕ игнорируется и не отменяет остальное: возвращается отдельным списком,
    чтобы отчёт мог сказать «взято столько, не разобрано вот это», а не выбирать между
    полным отказом и молчаливой потерей части выбора.
    """
    wanted: set = set()
    problems: list = []
    for chunk in str(raw or "").replace(";", ",").split(","):
        token = chunk.strip()
        if not token:
            continue
        m = _RANGE.match(token)
        if m:
            lo, hi = int(m.group(1), 16), int(m.group(2), 16)
            if lo > hi:
                lo, hi = hi, lo
            wanted.update(range(lo, hi + 1))
            continue
        m = _ONE.match(token)
        if m:
            wanted.add(int(m.group(1), 16))
            continue
        problems.append(token)
    return wanted, problems
def parse_spec_parts(raw: str) -> tuple:
    """`"m003b-m0036,m0012"` → (`[(0x3b, 0x36)]`, `{0x12}`, непонятое).

    Отличается от `parse_spec` тем, что НЕ разворачивает диапазон в числа: концы остаются
    парой. Кому это нужно — см. `positions_for_spec`.
    """
    ranges, singles, problems = [], set(), []
    for chunk in str(raw or "").replace(";", ",").split(","):
        token = chunk.strip()
        if not token:
            continue
        m = _RANGE.match(token)
        if m:
            ranges.append((int(m.group(1), 16), int(m.group(2), 16)))
            continue
        m = _ONE.match(token)
        if m:
            singles.add(int(m.group(1), 16))
            continue
        problems.append(token)
    return ranges, singles, problems
def positions_for_spec(rows, by_msg: dict, raw: str) -> tuple:
    """Спецификация → позиции строк. `(позиции, непонятое, ненайденное)`.

    ДИАПАЗОН РЕЖЕТ ЛЕНТУ ПО ПОЛОЖЕНИЮ КОНЦОВ, а не по числам между ними (владелец,
    2026-09-22). После компакции адреса идут не по возрастанию: новый дистиллят получает
    свежий номер, а встаёт на место первого съеденного. В ленте `3b, 09, 12, 45, 36, 22`
    числовой разворот `m003b-m0036` дал бы номера `0x36…0x3b` — из них в ленте есть только
    сами концы, остальные не существуют вовсе, — и ПРОПУСТИЛ бы `09`, `12` и `45`, которые
    между концами лежат. Человек же показывает пальцем на кусок ленты: от этой до вон той.

    Одиночные адреса берутся как есть: там двусмысленности нет.
    """
    order = {id(row): i for i, row in enumerate(rows)}
    pos_of_num = {}
    for addr, row in (by_msg or {}).items():
        num = _addr_number(addr)
        if num >= 0 and id(row) in order:
            pos_of_num[num] = order[id(row)]

    ranges, singles, problems = parse_spec_parts(raw)
    chosen, missing = set(), []
    for num in sorted(singles):
        place = pos_of_num.get(num)
        if place is None:
            missing.append(num)
        else:
            chosen.add(place)
    for lo, hi in ranges:
        a, b = pos_of_num.get(lo), pos_of_num.get(hi)
        if a is None:
            missing.append(lo)
        if b is None:
            missing.append(hi)
        if a is None or b is None:
            continue
        if a > b:
            a, b = b, a
        chosen.update(range(a, b + 1))
    return sorted(chosen), problems, sorted(set(missing))
def _addr_number(addr: str) -> int:
    """Числовая часть адреса `m003f` → `63`; `-1` у того, что адресом не является."""
    m = _ONE.match(str(addr or "").strip())
    return int(m.group(1), 16) if m else -1
def is_prose(row) -> bool:
    """Есть ли в строке СЛОВА разговора: реплика человека или речь модели.

    ВЫЗОВ ИНСТРУМЕНТА СТРОКУ НЕ ДИСКВАЛИФИЦИРУЕТ. Первая версия выбрасывала реплику
    ассистента целиком, если у неё есть `tool_calls`, — и это оказалось грубой ошибкой,
    пойманной на живом прогоне 2026-09-16: в агентской сессии почти каждая реплика модели
    идёт вместе с вызовом, поэтому в суммаризатор из шестнадцати сообщений уехало ДВА, оба
    человеческих, а вся речь модели пропала. «Выкинуть тулколы» значит убрать ВЫЗОВЫ и
    РЕЗУЛЬТАТЫ, а не предложение, которое модель написала рядом с вызовом.

    Здесь решается только одно: есть ли тут слова вообще — сериализацию для модели
    делает `distill_summary`/`_distill.interrogate`, не эта функция.

    Строка-ответ тула не проза никогда: это вывод инструмента, не речь. Пустая реплика
    ассистента (одни вызовы, ни слова) — тоже, суммировать в ней нечего.
    """
    if not isinstance(row, dict):
        return False
    if str(row.get("role") or "") not in ("user", "assistant"):
        return False
    return bool(str(row.get("content") or "").strip())
# РОЛЬ ДИСТИЛЛЯТА. `auto` — прежнее поведение: роль выбирает сосед, лишь бы не слиплось.
# `user`/`assistant` — жёстко.
#
# ПОЧЕМУ НАСТРОЙКА, А НЕ РЕШЕНИЕ (владелец, 2026-09-22): «это нужно тестировать, как лучше
# будем, и сейчас сказать сложно». Дистиллят — справка о прошлом; чья это речь, меняет то,
# как модель к ней относится, и узнать это можно только живыми прогонами. У закрывающей
# скобки такого вопроса нет — она служебная и всегда `assistant` (см. `bracket_role`).
#
# ЗДЕСЬ СТОИТ `auto`, А В `plugin.yaml` — `assistant`, и расхождение НАМЕРЕННОЕ. Конфиг
# задаёт то, чем живёт бой: владелец выбрала `assistant` по тому же доводу, что и у скобки —
# наша вставка это речь механизма, а не человека. А код без конфига не имеет права ПРИБИТЬ
# роль: не прочитав настройку, мы не знаем выбора, и единственное честное поведение —
# прежнее правило «лишь бы не слиплось с соседом». Оно же держит юнит-тесты на `summary_role`
# честными: они проверяют само правило, а не значение чужого файла.
DISTILLATE_ROLE = "auto"
def set_distillate_role(value: str) -> str:
    """Принять роль из конфига. Возвращает строку отчёта — мусор в конфиге должен быть виден.

    ОТСУТСТВИЕ ЗНАЧЕНИЯ И СЛОВО `auto` РАЗВЕДЕНЫ (правка 2026-09-22). Раньше `None`
    превращался в `"auto"` и отчитывался как успешно принятая настройка — и когда параметр до
    нас вообще не доехал, строка загрузки бодро сообщала «роль дистиллята: auto», а конфиг в
    это время говорил `assistant`. Живой прогон выдал дистиллят репликой человека, и по
    отчёту понять это было нельзя. Настройка, которая молча не применяется, хуже
    отсутствующей: она создаёт уверенность, что выбор сделан.
    """
    global DISTILLATE_ROLE
    if value is None:
        return (f"distillate role: {DISTILLATE_ROLE} — config value MISSING "
                "(compact_distillate_role did not reach the engine)")
    name = str(value).strip().lower()
    if name not in ("auto", "user", "assistant"):
        return (f"compact_distillate_role = {value!r} not understood — kept "
                f"{DISTILLATE_ROLE!r}; expected auto/user/assistant")
    DISTILLATE_ROLE = name
    return f"distillate role: {name}"
def summary_role(prev_row, next_row) -> str:
    """Роль строки-сводки. `auto` — такая, чтобы не слипнуться с соседями.

    Правило их `_summary_placement`, сведённое к сути: два `user` подряд или два `assistant`
    подряд часть провайдеров не принимает вовсе, а у остальных это молча портит чередование.
    Сводка — новое место между двумя существующими, и выбрать ей роль может только сосед.

    Жёсткая роль из конфига этот довод СОЗНАТЕЛЬНО перевешивает: владелец хочет мерить, как
    модель относится к дистилляту от своего лица против лица человека, а плавающая роль
    сделала бы замеры несравнимыми. Риск слипания при этом реален и остаётся на человеке.
    """
    if DISTILLATE_ROLE in ("user", "assistant"):
        return DISTILLATE_ROLE
    before = str((prev_row or {}).get("role") or "")
    after = str((next_row or {}).get("role") or "")
    role = "user" if before in ("", "assistant", "tool") else "assistant"
    if role == after:
        role = "assistant" if role == "user" else "user"
    return role
def bracket_role(prev_row) -> str:
    """Роль ЗАКРЫВАЮЩЕЙ скобки. ВСЕГДА `assistant` (решение владельца, 2026-09-22).

    Прежде роль выбиралась по верхнему соседу и от него уступала. Живой прогон показал, чем
    это плохо на глаз: закрывающая строка встала репликой ЧЕЛОВЕКА, то есть в разговоре
    появилось сказанное владельцем, чего владелец не говорила. Служебная вставка — речь
    механизма, а механизм в этой ленте всегда на стороне агента.

    Цена решения названа честно: когда сверху лежит сохранённый дословно ход агента, скобка
    с ним слипнется (`assistant` после `assistant`). Владелец выбрала слипание вместо
    подделки чужой реплики. Аргумент параметром оставлен: слипание — вещь, которую, возможно,
    придётся лечить, и лечить её будут ЗДЕСЬ.
    """
    del prev_row   # см. докстринг: роль больше не зависит от соседа
    return "assistant"
def _expand_to_pairs(rows, by_msg: dict, wanted: set, lines=None) -> set:
    """Достроить набор НОМЕРОВ до целых пар. Правило и его причина — `_addresses.expand_positions`.

    Работает через позиции, а не через номера: пара «вызов/результат» связана
    `tool_call_id`, и соседство номеров тут ни при чём — после прошлой компакции номера
    вокруг идут вразнобой.
    """
    pos_of_num, num_of_pos = {}, {}
    order = {id(row): i for i, row in enumerate(rows or [])}
    for addr, row in (by_msg or {}).items():
        num = _addr_number(addr)
        place = order.get(id(row))
        if num >= 0 and place is not None:
            pos_of_num[num] = place
            num_of_pos[place] = num

    chosen = {pos_of_num[n] for n in wanted if n in pos_of_num}
    grown, notes = _addresses.expand_positions("compact", rows, chosen)
    if notes and lines is not None:
        named = ", ".join(f"{_addr(num_of_pos.get(p, -1))}←{_addr(num_of_pos.get(r, -1))}"
                          for p, r in notes)
        lines.append(f"выбор достроен до пар ({len(notes)}): {named} — у вызова нет ни своей "
                     "судьбы, ни своей ячейки в рельсе; показать на него можно только "
                     "через его результат")
    return {num_of_pos[p] for p in grown if p in num_of_pos}
def build_runs(rows, by_msg: dict, wanted: set) -> tuple[list, list, list]:
    """Разложить выбор на пробеги. `(пробеги, пропущенные, не найденные)`.

    Пробег — `{"positions": [...], "eaten": [...]}`, где позиции это индексы в `rows`. Режет
    пробег ТОЛЬКО дырка в выборе: два выбранных индекса подряд — один пробег, разрыв — новый.

    `skipped` ВСЕГДА ПУСТ и оставлен только формой ответа (2026-09-22). Здесь стоял отказ:
    пробег без единой реплики не трогался вовсе — «без прозы это не компакция, а стирание».
    Правила такого НЕТ и не было: оно появилось в шапке модуля без чьего-либо решения, в
    отличие от соседних, у которых стоят даты и слова владельца, и трижды выдавалось за её
    норму, пока она не потребовала его убрать.

    Вреда от него было два. Прямой: человек отмечает кусок с дырками, один из обломков
    оказывается сплошь из тул-строк — и молча выпадает из работы (живой прогон 2026-09-22,
    сессия `20260916_235239_e7fa17`: `m0007`, `m0008`, `m0009` уцелели, хотя их выбрали).
    Косвенный, но хуже: он подменял решение человека собственным мнением о том, что считать
    осмысленной компакцией. Нажал на один элемент — команда идёт и делает, а не выдумывает,
    что так нельзя. Сжать один большой файл ровно потому, что он большой, — это законное и
    частое желание.
    """
    pos_of = {id(row): i for i, row in enumerate(rows)}
    chosen: dict = {}          # индекс в rows -> адрес
    for addr, row in (by_msg or {}).items():
        num = _addr_number(addr)
        if num in wanted and id(row) in pos_of:
            chosen[pos_of[id(row)]] = addr

    found = {_addr_number(a) for a in chosen.values()}
    missing = sorted(wanted - found)

    runs: list = []
    skipped: list = []
    current: list = []
    for index in sorted(chosen):
        if current and index != current[-1] + 1:
            runs.append(current)
            current = []
        current.append(index)
    if current:
        runs.append(current)

    out = []
    for positions in runs:
        prose = [i for i in positions if is_prose(rows[i])]
        mute = [i for i in positions if not is_prose(rows[i])]
        out.append({"positions": positions,
                    # СЪЕДАЕТСЯ ВЕСЬ пробег, вызовы и ответы тоже: иначе контекст не
                    # уменьшается и смысла в компакции нет вовсе (владелец, 2026-09-16 —
                    # в её сессии проза это 2 строки из 235).
                    "eaten": list(positions),
                    # `prose`/`mute` — ЕСТЬ ЛИ что сжимать, а НЕ что показывать модели:
                    # блок данных собирает `_distill`/`_render`, и вызовы с ответами в нём
                    # есть (правка комментария 2026-09-22, прежний утверждал обратное).
                    "prose": prose,
                    "mute": mute,
                    "addresses": [chosen[i] for i in positions]})
    return out, skipped, missing
def merge_runs(rows, runs) -> list:
    """Пробеги — в ОДИН пролёт, дырки в выборе становятся ПУЗЫРЬКАМИ.

    ЗАЧЕМ. `build_runs` режет выбор на пробеги по каждой дырке, и на старом мозге это было
    дёшево: пробег стоил одного вызова модели. Наше ядро стоит целого опроса в минуты, и
    пять пробегов означали бы пять дистиллятов подряд — на одной и той же сессии, каждый со
    своей неполной картиной.

    Поэтому дистиллят ОДИН на весь пролёт, а невыбранные строки внутри него остаются на
    своих местах живыми ходами (`Vision08`: пузырьки — полноценные сообщения, мы их не
    трогаем ни на байт). Их адреса возвращаются в ключе `kept` — по ним ставится
    закрывающая скобка.
    """
    if not runs:
        return []
    addr_of: dict = {}
    for run_info in runs:
        addr_of.update(dict(zip(run_info["positions"], run_info["addresses"])))
    return run_over(rows, {i for run_info in runs for i in run_info["eaten"]}, addr_of)
def run_over(rows, eaten, addr_of: dict, hollow=()) -> list:
    """Пролёт по ГОТОВОМУ набору съедаемых позиций. Список из одного, либо пустой.

    Отдельно от `merge_runs` потому, что набор меняется и после неё: правило владения
    (`_pairs.settle`) выводит из съедаемого оболочки и склеенные блоки, и пересобрать
    пролёт после этого надо тем же кодом, каким он собирался, — иначе `kept` и `prose`
    разойдутся с `eaten` молча.

    ПУЗЫРЁК — ЭТО ТО, ЧТО ОСТАНЕТСЯ ЖИТЬ СО СВОИМИ СЛОВАМИ. Дырка в выборе сама по себе
    пузырьком не делает, и это не придирка: ярлык `STAYS VERBATIM` обещает модели «этот ход
    остаётся у тебя дословно, опирайся на него и не пересказывай». Обещать так про строку,
    которой сейчас не станет, — прямая ложь в данных, а модель мы за такое же и ругаем.

    Из дырок вычитается `hollow` — позиции, которые правило владения уже разобрало:

      * строка УБИРАЕТСЯ целиком (реплика ассистента осталась без прозы и без вызовов) —
        обещать дословность нечему. Случай владельца 2026-09-22: отметить в ленте одни
        результаты значит оставить дырками их пустые реплики-носители, а те после смерти
        своих результатов исчезают все до одной;
      * строка становится ОБОЛОЧКОЙ (её проза уходит в дистиллят, вызовы остаются) — она
        останется, но её слова как раз пересказываются, и они предмет работы, а не фон.
    """
    eaten = sorted(eaten or [])
    if not eaten:
        return []
    inside = set(eaten) | set(hollow or ())
    kept = [i for i in range(eaten[0], eaten[-1] + 1) if i not in inside]
    return [{"positions": eaten, "eaten": eaten, "kept": kept,
             "prose": [i for i in eaten if is_prose(rows[i])],
             "mute": [i for i in eaten if not is_prose(rows[i])],
             "addresses": [addr_of.get(i, "") for i in eaten]}]
def is_summary_row(content: str) -> bool:
    """Наша ли это вставка-сводка в собранной ленте. Узнаём по ТОЙ рамке, которой писали.

    Ловушка, найденная 2026-09-21: узнавание было прибито к их префиксу, и с нашей рамкой
    `_note_absorptions` не находил НИ ОДНОЙ сводки — запись о поглощении просто не писалась
    бы в леджер, молча и без отказа. Свидетельство исчезает тише всего.

    Закрывающая скобка сюда не попадает: у неё своя метка, и поглощать ей нечего.
    """
    text = str(content or "")
    return _prompts.is_distillate(text)
# ГОТОВЫЙ ТЕКСТ (LLMOps, Plan31 Таск 1). Поставщик текста замены — подставляемый: опрос ядра
# (`_compact.distill_summary`) или окно LLMOps, где текст уже написан и обёртку выбрал человек. У ответа
# окна есть `wrap` (`compaction` / `summarisation` / `none`, `_prompts.WRAPS`); у ответа опроса его
# нет — это дистиллят. Всё прочее (выбор, пары, пролёты, пузыри, мостики, чередование, коммит,
# журнал поглощения, замок) — общее, не копируется.
def _head_content(answer: dict, kept: bool = False) -> str:
    """Текст вставки на месте пробега: дистиллят в рамке или готовый текст в своей обёртке."""
    return _prompts.wrap_ready(answer["text"], answer.get("wrap") or "compaction", kept=kept)
def _bridge_kind(summaries) -> str:
    """Чьими словами мостики — по обёртке вставки: `compact` — дистиллят (опрос компакции, `llm ops`
    с рамкой `compaction` или «как было» на дистилляте); `summarisation` — нейтральная рамка (и «как
    было» на ней); `mute` — без рамки: заглушка ничего не говорит о содержании."""
    kinds = []
    for s in summaries or []:
        wrap, text = s.get("wrap"), str(s.get("text") or "")
        if wrap in (None, "compaction") or (wrap == "none" and _prompts.is_distillate(text)):
            kinds.append("compact")
        elif wrap == "summarisation" or (wrap == "none" and _prompts.is_summary(text)):
            kinds.append("summarisation")
        else:
            kinds.append("mute")
    for kind in ("mute", "summarisation"):
        if kind in kinds:
            return kind
    return "compact"


def _insert_ids(new: list, old: list, heads: set) -> set:
    """`id` строк-вставок этого прогона: новых (их не было в `old`) с текстом вставки. Правленые
    правилом владения оболочки — тоже новые объекты, но не вставки: их отличает текст."""
    old_ids = {id(r) for r in old}
    return {id(r) for r in new if id(r) not in old_ids and r.get("content") in heads}


def _has_close(answer: dict) -> bool:
    """Отдельная закрывающая скобка за пузырьками — только у директивной рамки дистиллята."""
    return answer.get("wrap") in (None, "compaction")
def _plan(rows, by_msg, wanted, lines):
    """ВЫБОР на готовых строках: пробеги, пузырьки, правило владения. Без модели и без базы —
    поэтому его можно повторить на СВЕЖИХ строках, когда запись ждёт конца хода.
    `(runs, skipped, missing, patch, replaced, hollow)`."""
    runs, skipped, missing = build_runs(rows, by_msg, wanted)
    # НАШ мозг стоит целого опроса в минуты, и пять пробегов означали бы пять дистиллятов
    # подряд на одной сессии. Сводим в один пролёт: дырки становятся пузырьками.
    # Зовём ВСЕГДА, а не только на нескольких пробегах: `merge_runs` заодно приводит пролёт
    # к форме с ключом `kept`, на которую опирается всё ниже.
    before = len(runs)
    runs = merge_runs(rows, runs)
    if before > 1:
        lines.append(f"пробегов было {before} — сведены в один пролёт; "
                     f"невыбранное внутри ({len(runs[0]['kept'])} строк) остаётся "
                     "живыми ходами между дистиллятом и закрывающей строкой")

    # ПРАВИЛО ВЛАДЕНИЯ (`_pairs`): судьбу вызова решает судьба его результата. Правим
    # выбор ЗДЕСЬ, до похода в модель, — тогда блок данных, ярлыки `STAYS VERBATIM` и
    # отчёт говорят одно и то же. Лечить после оплаченного опроса значило бы показать
    # модели один кусок, а заменить другой.
    patch: dict = {}
    replaced: list = []
    hollow: set = set()
    if runs:
        addr_of = dict(zip(runs[0]["positions"], runs[0]["addresses"]))
        decision = _pairs.settle(rows, runs[0]["eaten"])
        patch = decision["patch"]
        replaced = [decision["was"][i] for i in sorted(decision["was"])]
        for note in decision["notes"]:
            lines.append("пары: " + note)
        # Оболочки (вышли из съедаемого) и убранные строки пузырьками НЕ считаются —
        # см. `run_over`. Пересобираем пролёт ВСЕГДА: даже когда набор съедаемого не
        # менялся, `patch` мог убрать строку, которая иначе получила бы ярлык дословности.
        hollow = (set(runs[0]["eaten"]) - decision["eaten"]) | {
            i for i, fresh in patch.items() if fresh is None}
        runs = run_over(rows, decision["eaten"], addr_of, hollow)
    return runs, skipped, missing, patch, replaced, hollow
def _assemble(session_id, session, agent, rows, by_msg, runs, patch, hollow, replaced, summaries,
              deliver, spec, facts, lines) -> dict:
    """СБОРКА И ЗАПИСЬ по готовым сводкам: лента, чередование, пары, сшивка, запись (путь A) или
    отдача ленты (путь B). Отказ записи из-за живой сессии — `facts["_retry"]` с причиной."""
    _progress.progress(session_id, stage="assemble", label="")
    history = list(deliver["history"]) if deliver is not None else _apply.history_of(session)
    new_rows, new_history, eaten_rows = _rebuild(rows, history, runs, summaries, patch)
    new_rows = _apply.drop_stale_api_content(rows, new_rows)

    # ЧЕРЕДОВАНИЕ ПОД ИХ ПРАВИЛО (`_alternation`, владелец 2026-09-29): на новых стыках одной роли
    # — скобка меняет роль или встаёт мостик. Иначе Hermes склеит дистиллят с соседом (живьём:
    # дистиллят стал частью `m0002`). Проверка — их же функцией: нарушений не больше, чем было.
    # Слова мостиков — операции (владелец, 2026-10-07): дистиллят компакции — голос компакции;
    # готовый текст `llm ops` в нейтральной рамке или без рамки — голос суммаризации. Места `before`/
    # `after` — по НАШИМ вставкам этого прогона (`inserts`), а не угадыванием по метке.
    words = _prompts.bridge_texts(_bridge_kind(summaries))
    heads = {_head_content(s, bool(r.get("kept"))) for r, s in zip(runs, summaries)}
    new_rows, bridged_rows = _alternation.fit(new_rows, rows, texts=words,
                                              inserts=_insert_ids(new_rows, rows, heads))
    new_history, bridged = _alternation.fit(new_history, history, texts=words,
                                            inserts=_insert_ids(new_history, history, heads))
    if bridged or bridged_rows:
        lines.append(f"чередование: вставлено мостиков {bridged} (в базе {bridged_rows}) — "
                     "их правило склеило бы соседние реплики одной роли")
    was_bad, now_bad = _alternation.their_repairs(history), _alternation.their_repairs(new_history)
    if was_bad is not None and now_bad is not None and now_bad > was_bad:
        lines.append(f"ИТОГ: контекст НЕ тронут — их проверка чередования нашла {now_bad} "
                     f"нарушений против {was_bad} до сборки: такую ленту Hermes разрежет")
        return facts

    # ЛОВУШКА 1 ПЛАНА, и теперь она достижима: съедая пробег целиком, мы можем разрезать
    # пару вызов/ответ по ГРАНИЦЕ диапазона — вызов внутри, ответ снаружи (или наоборот).
    # Провайдер такой ход не принимает вовсе. Линт тот же, которым пользуется `erase`.
    problems = _stubs.lint_pairs(new_rows)
    if problems:
        lines.append(f"ИТОГ: контекст НЕ тронут — осталось {len(problems)} "
                     f"рассогласованных пар вызов/ответ: одна половина внутри, другая "
                     "снаружи, а провайдеру нужны обе. Правило владения (`_pairs`) такие "
                     "разрывы лечит само — значит этот пришёл из ленты, а не из выбора: "
                     "смотри карту в отчёте прогона")
        _write_tape(summaries, session_id, rows, by_msg, runs, patch, hollow, None, {},
                    spec)
        return facts

    # ЛЕНТА ДЛЯ ПАМЯТИ (хода или гейтвея) — СШИВАЕТСЯ, в обоих путях: голова, дистиллят,
    # мостики — из сборки по БАЗЕ; хвост — из памяти (там самое свежее, возможно ещё не
    # записанное). Прежняя версия искала в памяти место дистиллята по строке базы; живьём
    # 2026-09-29 Hermes заранее склеил эту строку с соседом в памяти, место не нашлось, и
    # дистиллят молча выпал (`splice_for_delivery`, проверки там же).
    _progress.progress(session_id, stage="checks")
    spliced, why_not = splice_for_delivery(rows, new_rows, history)
    if spliced is None:
        lines.append("ИТОГ: контекст НЕ тронут — " + why_not)
        return facts
    new_history = spliced

    if deliver is not None:
        _progress.progress(session_id, stage="handover")
        # ПУТЬ B: запись — их. Отдаём сшитую ленту и всё, что понадобится после их записи.
        facts.update({"delivered": True, "new_history": new_history, "runs": len(runs),
                      "summaries": [s["text"] for s in summaries],
                      "rows_before": len(rows), "rows_after": len(new_rows),
                      "finalize": {"runs": runs, "new_rows": new_rows, "summaries": summaries,
                                   "patch": patch, "hollow": hollow, "rows": rows,
                                   "by_msg": by_msg, "spec": spec}})
        lines.append(f"лента собрана: строк было {len(rows)}, станет {len(new_rows)} — "
                     "записывает Hermes (автокомпакция, путь B)")
        return facts

    # `replaced` — ИСХОДНЫЕ версии строк, которые правило владения переписало. Предохранитель
    # коммита ключует реплику ассистента ПО ТЕКСТУ (`_apply._row_key`), поэтому выпотрошенная
    # оболочка для него — другая строка, а исходная выглядит непосчитанной, и он отказал бы
    # целиком. Тот же законный вход, которым живёт `erase`: «удалённое нарочно отсутствует».
    _progress.progress(session_id, stage="write")
    ok, applied_why = _apply.commit_replacement(session, agent, new_rows,
                                                removed=eaten_rows + replaced)
    if not ok:
        if _apply.should_retry(applied_why):
            facts["_retry"] = applied_why     # сессия живёт — ждём конца хода и пересобираем
            return facts
        lines.append(f"ИТОГ: НЕ применено — {applied_why}")
        return facts

    # ── После коммита. Переиндексация даёт сводкам их собственные адреса, и только теперь
    # запись о поглощении может назвать ключ («m0015 поглотил m0002…m0008»).
    fresh, _ = _apply.db_history(agent)
    fresh_by_msg = _index(session_id, fresh) if fresh else {}
    absorbed = _note_absorptions(session_id, fresh_by_msg, runs, new_rows, summaries)
    facts["absorbed"] = absorbed
    for record in absorbed:
        lines.append(f"поглощение: {record['summary']} ← "
                     f"{record['members'][0]}…{record['members'][-1]} "
                     f"({len(record['members'])} сообщений)")
    if len(absorbed) < len(runs):
        lines.append(f"записей о поглощении {len(absorbed)} из {len(runs)} — сводке не "
                     "нашёлся адрес после переиндексации. Контекст изменён, свидетельство "
                     "неполно: смотри журнал сессии")

    _write_tape(summaries, session_id, rows, by_msg, runs, patch, hollow,
                fresh, fresh_by_msg, spec)

    _signal.bump(session_id, [])
    facts.update({"ok": True, "runs": len(runs),
                  "summaries": [s["text"] for s in summaries],
                  "rows_before": len(rows), "rows_after": len(new_rows)})
    lines.append(f"строк в базе: было {len(rows)}, стало {len(new_rows)} "
                 f"(съедено {len(eaten_rows)}, добавлено сводок {len(runs)})")

    # СКОЛЬКО РЕАЛЬНО ВЫИГРАНО, символами. Живой прогон 2026-09-22 на сессии
    # `20260917_013139_ae86e7`: съедено 744 символа прозы, дистиллят вышел 6328 — контекст
    # вырос почти вдвое, и по отчёту это было не видно вовсе, он бодро говорил «применено».
    # Причина была в выборе (взяли только реплики, а весь объём — ответы инструментов —
    # остался пузырьками), но узнать об этом человек мог лишь пересчитав карту руками.
    # Компакция, сделавшая контекст больше, — не ошибка механизма, но и не то, за чем её
    # звали, и молчать об этом нельзя (правило 4 шапки модуля).
    ate = sum(len(str(r.get("content") or "")) for r in eaten_rows)
    made = sum(len(_head_content(s)) for s in summaries)
    lines.append(f"символов: съедено {ate}, дистиллят {made} — "
                 + (f"выигрыш {ate - made}" if made < ate else
                    f"КОНТЕКСТ ВЫРОС на {made - ate}. Объём сессии лежит не в том, что "
                    "выбрано: смотри карту `5_лента.md` — что осталось пузырьками"))
    lines.append("следующий запрос — полный холодный пересчёт: префикс изменился, кеш "
                 "провайдера на этой сессии больше не подходит")
    lines.append("ИТОГ: применено"
                 + ("" if applied_why.strip() == "применено" else f" — {applied_why}"))
    return facts
def _same_selection(old_rows, old_runs, new_rows, new_runs) -> str:
    """Та же ли середина на свежих строках: те же адреса съедаемого и пузырьков, и съедаемое не
    переписано. Пустая строка — да; иначе — что изменилось."""
    def shape(runs):
        return [(tuple(r["addresses"]), tuple(r.get("kept") or ())) for r in runs]
    if len(old_runs) != len(new_runs):
        return "пробегов стало другое число"
    for a, b in zip(old_runs, new_runs):
        if list(a["addresses"]) != list(b["addresses"]):
            return f"другие строки в пробеге {a['addresses'][0]}…{a['addresses'][-1]}"
        if len(a.get("kept") or ()) != len(b.get("kept") or ()):
            return "другие пузырьки"
        before = [_key(old_rows[i]) + (str(old_rows[i].get("content") or ""),) for i in a["eaten"]]
        after = [_key(new_rows[i]) + (str(new_rows[i].get("content") or ""),) for i in b["eaten"]]
        if before != after:
            return "съедаемые строки переписаны за время ожидания"
    return ""
# Ожидание конца хода (2026-09-30): пауза между попытками записи и предел ожидания.
WAIT_TURN_POLL_S = 3.0
def _assemble_until_written(session_id, session, agent, rows, by_msg, runs, patch, hollow, replaced,
                            summaries, deliver, spec, wanted, facts, lines) -> dict:
    """Сборка и запись; идёт ход — ЖДЁМ его конца, а не выбрасываем оплаченный опрос (владелец,
    2026-09-30: задел под параллельный сервер — опрос на одном, разговор на другом).

    Каждая следующая попытка — на СВЕЖИХ строках базы и свежей памяти гейтвея: ход дописал хвост,
    а сводки те же. Середина обязана остаться той же (`_same_selection`) — иначе отказ словами,
    сводка остаётся в отчёте прогона. Замок компакции держим всё ожидание: автомат поверх не
    стартует. Предел — `compact_wait_turn_minutes`; путь автомата (B) не ждёт — ход и есть он."""
    scratch: list = []
    out = _assemble(session_id, session, agent, rows, by_msg, runs, patch, hollow, replaced,
                    summaries, deliver, spec, facts, scratch)
    if not facts.get("_retry") or deliver is not None:
        lines.extend(scratch)
        facts.pop("_retry", None)
        return out
    limit = max(0, int(_settings.compaction().get("compact_wait_turn_minutes") or 0)) * 60
    started = time.time()
    first_why = facts["_retry"]
    lines.extend(scratch)
    lines.append(f"запись ждёт конца хода: {first_why.split(':', 1)[0]}")
    while True:
        waited = time.time() - started
        if waited >= limit:
            facts.pop("_retry", None)
            lines.append(f"ИТОГ: НЕ применено — ход не кончился за {int(waited // 60)} мин "
                         f"(`compact_wait_turn_minutes`). Сводка — в отчёте прогона")
            return facts
        _progress.progress(session_id, stage="wait_turn", label="")
        time.sleep(WAIT_TURN_POLL_S)
        fresh, why = _apply.db_history(agent)
        if why:
            facts.pop("_retry", None)
            lines.append(f"ИТОГ: НЕ применено — {why}")
            return facts
        fresh_by_msg = _index(session_id, fresh)
        plan_lines: list = []
        f_runs, _s, _m, f_patch, f_replaced, f_hollow = _plan(fresh, fresh_by_msg, wanted, plan_lines)
        changed = _same_selection(rows, runs, fresh, f_runs)
        if changed:
            facts.pop("_retry", None)
            lines.append(f"ИТОГ: НЕ применено — за время ожидания изменилась середина ({changed}). "
                         "Сводка — в отчёте прогона")
            return facts
        facts.pop("_retry", None)
        scratch = []
        out = _assemble(session_id, session, agent, fresh, fresh_by_msg, f_runs, f_patch, f_hollow,
                        f_replaced, summaries, deliver, spec, facts, scratch)
        if not facts.get("_retry"):
            lines.append(f"ход кончился — запись через {int(time.time() - started)} с ожидания")
            lines.extend(scratch)
            return out
# Поля строки базы, которые — сообщение; остальное (`id`, `session_id`, `active`, …) — её учёт.
_MESSAGE_KEYS = ("role", "content", "api_content", "tool_calls", "tool_call_id", "tool_name", "name",
                 "timestamp", "reasoning", "reasoning_content", "reasoning_details", "finish_reason",
                 "display_kind")
def _as_message(row: dict) -> dict:
    return {k: row[k] for k in _MESSAGE_KEYS if k in row and row[k] is not None}
def _key(row) -> tuple:
    call_id = _first_call_id(row)
    return ("call", call_id) if call_id else _apply._row_key(
        row.get("role"), row.get("content"), row.get("tool_call_id"))
def splice_for_delivery(rows: list, new_rows: list, history: list) -> tuple:
    """Лента для их записи: `(лента, "")` или `(None, причина)`.

    Блок до хвоста — из `new_rows` (сборка по базе: голова, дистиллят, мостики, скобка, пузырьки)
    как сообщения; хвост — из `history` (лента хода), начиная с сообщения, соответствующего первой
    нетронутой строке базы после блока. Проверки перед отдачей: каждый новый дистиллят на месте,
    пары целы, их чередование не хуже, чем было.
    """
    old_ids = {id(r) for r in rows}
    # Хвост в базе — нетронутые строки ПОСЛЕ последней новой (дистиллят/мостик/скобка).
    last_new = max((i for i, r in enumerate(new_rows) if id(r) not in old_ids), default=-1)
    tail_db = new_rows[last_new + 1:]
    block = new_rows[:last_new + 1]
    if tail_db:
        # Якорь хвоста — ПЕРВАЯ строка базы после блока, которая есть в ленте хода. Не обязательно
        # самая первая: их загрузчик склеивает в памяти старые пары одной роли, а мы такой стык
        # чиним мостиком (`_alternation.fit`, всё после места вставки) — и строка за мостиком в
        # памяти живёт только внутри склейки. Живьём 2026-10-07 (сессия `d39f0f`): `[System: The
        # active model … changed]` + «понятно», два `user`; вставка `llm ops` упала «хвост не
        # найден». Строки до якоря — из базы (она источник правды; в памяти они лишь склеены).
        pos, skip = None, 0
        for skip, row in enumerate(tail_db):
            want = _key(row)
            pos = next((i for i, m in enumerate(history) if _key(m) == want), None)
            if pos is not None:
                break
        if pos is None:
            return None, ("хвост не найден в ленте хода (ни одна строка после реза не совпала ни с "
                          "одним сообщением хода) — сшить ленту не из чего")
        block = block + tail_db[:skip]
        tail = history[pos:]
    else:
        # Резали до самого конца базы: из ленты хода берём только то, чего в базе ещё нет.
        last_key = _key(rows[-1]) if rows else None
        pos = next((i for i in range(len(history) - 1, -1, -1) if _key(history[i]) == last_key), None)
        tail = history[pos + 1:] if pos is not None else []
    # Строки базы уходят сообщениями (без учётных полей базы); новые — как собраны.
    out = [_as_message(r) if id(r) in old_ids else r for r in block] + list(tail)
    # Новая ВСТАВКА — всё новое, кроме мостиков и скобки: дистиллят или готовый текст LLMOps
    # (тот может быть без рамки, по метке его не узнать — Plan31).
    def _service(r):
        text = str(r.get("content") or "")
        # Дистиллят без пузырей несёт метку конца у себя в хвосте — скобка лишь та, где нет шапки.
        return _prompts.is_bridge(text) or (_prompts.is_distill_end(text)
                                            and not _prompts.is_distillate(text))
    fresh = [r for r in block if id(r) not in old_ids and not _service(r)]
    if not fresh or any(all(r is not m for m in out) for r in fresh):
        return None, "в сшитой ленте нет новой вставки (дистиллята) — отдавать такое нельзя"
    problems = _stubs.lint_pairs(out)
    if problems:
        return None, f"в сшитой ленте {len(problems)} рассогласованных пар вызов/ответ"
    # ПОДОБНОЕ С ПОДОБНЫМ (2026-10-03): голова сшитой ленты — из строк БАЗЫ, а их загрузчик в
    # памяти хода старые пары одной роли склеивает. Сравнение только с памятью давало «1 против 0»
    # на паре, которую мы не вносили (сессия `4fb201`, голова под `head_keep`). База сравнения —
    # худшее из двух: память хода и та же лента до правки в виде строк базы.
    was_turn = _alternation.their_repairs(history)
    was_rows = _alternation.their_repairs([_as_message(r) for r in rows])
    was_bad = max((n for n in (was_turn, was_rows) if n is not None), default=None)
    now_bad = _alternation.their_repairs(out)
    if was_bad is not None and now_bad is not None and now_bad > was_bad:
        return None, (f"их проверка чередования нашла в сшитой ленте {now_bad} нарушений "
                      f"против {was_bad} до правки")
    return out, ""
def finalize_delivered(session_id: str, engine, fin: dict) -> list:
    """После ИХ записи ленты автомата (путь B): записи о поглощении по свежим адресам, карта,
    сигнал панели. Зовёт движок на следующем своём событии — к этому моменту база уже их."""
    agent = engine.live_agent()
    fresh, _ = _apply.db_history(agent) if agent is not None else ([], "")
    fresh_by_msg = _index(session_id, fresh) if fresh else {}
    absorbed = _note_absorptions(session_id, fresh_by_msg, fin["runs"], fin["new_rows"],
                                 fin.get("summaries"))
    _write_tape(fin["summaries"], session_id, fin["rows"], fin["by_msg"], fin["runs"],
                fin["patch"], fin["hollow"], fresh, fresh_by_msg, fin["spec"])
    _signal.bump(session_id, [])
    return absorbed
FILE_TAPE = "5_лента.md"
def _addr_map(rows, by_msg: dict) -> dict:
    """`позиция → адрес` из индекса. Обратная сторона `by_msg`, которая всем и нужна."""
    addr_by_row = {id(row): addr for addr, row in (by_msg or {}).items()}
    return {i: addr_by_row.get(id(row), "") for i, row in enumerate(rows or [])}
def _write_tape(summaries, session_id, rows, by_msg, runs, patch, hollow,
                fresh, fresh_by_msg, spec: str = "") -> str:
    """Карта ленты ДО и ПОСЛЕ — пятым файлом в папку прогона. Никогда не роняет команду.

    Кладётся рядом с остальным отчётом, а не отдельной папкой: человек смотрит сюда сразу
    после прогона, и заставлять его искать карту в другом месте значит не сделать ничего.
    Папку берём из пути результата, который вернуло ядро, — второго источника у нас нет и
    заводить его незачем.

    ПИШЕТСЯ И НА ОТКАЗЕ. Отчёт об удавшейся правке полезен, отчёт о НЕудавшейся — нужнее:
    отказ по парам 2026-09-22 разбирался полчаса зондом ровно потому, что карты не было.
    """
    try:
        from pathlib import Path
        path = str((summaries or [{}])[0].get("path") or "")
        if not path:
            return ""
        folder = Path(path).parent
        run_info = (runs or [{}])[0]
        asked = sorted(run_info.get("addresses") or [])
        parts = [f"# Карта ленты — сессия {session_id}", "",
                 "Адреса и порядок РЯДОМ: после компакции номера идут не по возрастанию — "
                 "дистиллят получает свежий номер, а встаёт на место первого съеденного.", "",
                 "**Запрошено** (что доехало до движка, дословно):", "",
                 "```", spec or "(пусто)", "```", "",
                 f"**Съедается после разбора пар** ({len(asked)}): "
                 + (", ".join(asked) if asked else "ничего"), ""]
        parts.append(_tape.render(rows, _addr_map(rows, by_msg),
                                  _tape.marks_for(rows, run_info, patch, hollow),
                                  title="ДО правки"))
        parts += ["", ""]
        if fresh:
            parts.append(_tape.render(fresh, _addr_map(fresh, fresh_by_msg),
                                      _tape.marks_after(fresh, is_summary_row,
                                                        _prompts.is_distill_end),
                                      title="ПОСЛЕ правки"))
        else:
            parts.append("### ПОСЛЕ правки" + chr(10) + chr(10)
                         + "Правки не было — контекст не тронут.")
        (folder / FILE_TAPE).write_text(chr(10).join(parts) + chr(10), encoding="utf-8")
        return str(folder / FILE_TAPE)
    except Exception:  # noqa: BLE001 — отчёт не смеет ронять правку, которая уже случилась
        return ""
def _index(session_id: str, rows) -> dict:
    """`адрес -> строка базы`. Пустой словарь вместо исключения: индекс это свидетельство."""
    try:
        from ..file_ops import _msgindex
        from ..file_ops._ledger import read_ledger
        out = _msgindex.index_session(session_id, rows, read_ledger(session_id) or [])
        return out.get("by_msg") or {}
    except Exception:  # noqa: BLE001
        return {}
def _first_call_id(row) -> str:
    """`id` первого вызова строки — ключ, которым правленая реплика ищется в памяти гейтвея.

    Не `_apply._row_key`: у реплики ассистента он считается ПО ТЕКСТУ, а у реплики с одними
    вызовами текст пустой — ключ `("txt","assistant",0,"")` одинаков у всех таких строк, а их
    в живой базе 3341. Искать по нему значило бы поправить первую попавшуюся. `id` вызова
    уникален глобально, и у всякой строки, которую трогает `_pairs`, он есть по определению.
    """
    for call in _pairs.calls_of(row):
        call_id = _stubs._call_id(call)
        if call_id:
            return call_id
    return ""
def _rebuild(rows, history, runs, summaries, patch=None, eaten=()) -> tuple[list, list, list]:
    """Собрать новые списки базы и памяти гейтвея. `(строки базы, память, съеденное)`.

    Второй список — ПРОГНОЗ памяти: по нему считаются токены «после» и проверка чередования до
    записи. В гейтвей он не кладётся: после записи память перечитывает их загрузчик (`Plan26`,
    `_apply._memory_loader`), поэтому правленая строка входит сюда как есть.

    Сводка встаёт на индекс ПЕРВОГО съеденного своего пробега. Память гейтвея правится тем
    же набором, но ключом `_apply._row_key`: у её строк координаты сообщения может не быть
    вовсе, а этот ключ — ровно тот, каким сверяется предохранитель коммита.

    `patch` — решение правила владения (`_pairs.settle`): `позиция → новая строка`, а `None`
    значит, что строки не будет вовсе. Правленые строки НЕ попадают в съеденное: они не
    утонули, у них сменилось содержимое, и `role`/`timestamp` у них те же — значит адрес за
    ними держится (`_msgindex.place`).

    `eaten` — позиции, которые уходят БЕЗ дистиллята (`erase` на m-адресах, Plan16-Task08):
    та же пересборка обеих историй, только вставок нет.
    """
    patch = patch or {}
    eaten_at = {index: None for index in (eaten or ())}
    # КУДА ВСТАЮТ ВСТАВКИ. По умолчанию дистиллят — на место первого съеденного, закрывающая
    # строка — за последним. Но если такое место оказывается ВНУТРИ живого блока «реплика с
    # вызовом + её выживший результат», вставка съезжает: дистиллят влево, на первое валидное
    # место перед этой репликой; закрывающая — вправо, за последний выживший результат блока
    # (`_pairs.insert_left`/`insert_right`, решение владельца 2026-09-22).
    #
    # Двигаем ВСТАВКУ, а не отменяем выбор. Прежняя версия в этом случае выводила съеденный
    # результат обратно в живые — и на разборе примера оказалось, что от заказа человека не
    # оставалось ничего: вытащенный результат оживлял вызов, вызов расформировывал оболочку.
    open_at, close_at, kept_at = {}, {}, {}
    for run_info, answer in zip(runs, summaries):
        for index in run_info["eaten"]:
            eaten_at[index] = None
        inside = set(run_info["eaten"])
        head = _pairs.insert_left(rows, inside, run_info["eaten"][0])
        open_at[head] = answer
        kept_at[head] = bool(run_info.get("kept"))
        if run_info.get("kept") and _has_close(answer):
            close_at[_pairs.insert_right(rows, inside, run_info["eaten"][-1])] = True

    # Уникальное и стабильное время: координата `(роль, время)` — личность сообщения, и
    # коллизия здесь стоит адреса. Порядок от него НЕ зависит (`ORDER BY id`, часы врут),
    # поэтому «время позже соседей справа» ничего не ломает.
    stamp = time.time()
    new_rows, eaten_rows = [], []
    for index, row in enumerate(rows):
        if index in open_at:
            prev_row = new_rows[-1] if new_rows else None
            next_row = next((rows[j] for j in range(index, len(rows))
                             if j not in eaten_at), None)
            new_rows.append({"role": summary_role(prev_row, next_row),
                             "content": _head_content(open_at[index],
                                                      kept=kept_at.get(index, False)),
                             "timestamp": stamp + len(eaten_rows) * 1e-3})
        if index in eaten_at:
            eaten_rows.append(row)
        elif index in patch:
            if patch[index] is not None:
                new_rows.append(patch[index])
        else:
            new_rows.append(row)
        if index in close_at:
            new_rows.append({"role": bracket_role(new_rows[-1] if new_rows else None),
                             "content": _prompts.distill_end(True),
                             "timestamp": stamp + len(eaten_rows) * 1e-3})

    # Ключ СЪЕДЕННОГО — как у вставок: реплика с вызовами узнаётся по `id` вызова. Текстовый
    # ключ пустой реплики-вызова `("assistant", "", None)` совпадал с КАЖДОЙ такой репликой
    # хвоста, и они вылетали из памяти вместе со съеденной; их результаты повисали (живьём
    # 2026-09-30, сессия `5cb486`: «9 нарушений против 0»).
    drop_keys = {_key(r) for r in eaten_rows}
    # Те же две вставки и те же сдвиги, что выше, но по памяти гейтвея. Якорем служит
    # СТРОКА, перед которой (или после которой) вставка встаёт: в снимке гейтвея позиций у
    # нас нет, а строка находится по ключу.
    def _anchor(index):
        row = rows[index]
        call_id = _first_call_id(row)
        return ("call", call_id) if call_id else _apply._row_key(
            row.get("role"), row.get("content"), row.get("tool_call_id"))

    def _row_anchor(row):
        call_id = _first_call_id(row)
        return ("call", call_id) if call_id else _apply._row_key(
            row.get("role"), row.get("content"), row.get("tool_call_id"))

    inserts, closes = {}, set()
    for run_info, answer in zip(runs, summaries):
        inside = set(run_info["eaten"])
        head = _pairs.insert_left(rows, inside, run_info["eaten"][0])
        inserts[_anchor(head)] = (answer, bool(run_info.get("kept")))
        if run_info.get("kept") and _has_close(answer):
            closes.add(_anchor(_pairs.insert_right(rows, inside,
                                                   run_info["eaten"][-1])))

    patch_by_call = {}
    for index, fresh in patch.items():
        call_id = _first_call_id(rows[index])
        if call_id:
            patch_by_call[call_id] = fresh

    new_history, added = [], 0
    for row in history:
        anchor = _row_anchor(row)
        if anchor in inserts:
            answer, kept = inserts[anchor]
            prev_row = new_history[-1] if new_history else None
            new_history.append({"role": summary_role(prev_row, None),
                                "content": _head_content(answer, kept=kept),
                                "timestamp": stamp + added * 1e-3})
            added += 1
        # Правка по `id` вызова, а не по `_row_key`: см. `_first_call_id`. Проверяется
        # РАНЬШЕ выбрасывания — иначе оболочка с пустой прозой совпала бы по текстовому
        # ключу с любой съеденной пустой репликой и вылетела бы из памяти вместе с ней.
        fixed = patch_by_call.get(_first_call_id(row), False)
        if fixed is not False:
            if fixed is not None:
                new_history.append(fixed)
        elif _key(row) in drop_keys:
            pass                                   # съедено, в новую историю не попадает
        else:
            new_history.append(row)
        if anchor in closes:
            new_history.append({"role": bracket_role(
                                    new_history[-1] if new_history else None),
                                "content": _prompts.distill_end(True),
                                "timestamp": stamp + added * 1e-3})
            added += 1
    return new_rows, new_history, eaten_rows
def _note_absorptions(session_id: str, fresh_by_msg: dict, runs, new_rows, summaries=None) -> list:
    """Записать в леджер, какая сводка что поглотила. Отчёт списком записанного.

    Адрес сводки берётся ПЕРЕИНДЕКСАЦИЕЙ, а не угадыванием: сводка — новое место, и кто
    ей выдал адрес, знает только индексатор. Не нашёлся — запись не пишется и об этом
    сказано вслух; врать адресом хуже, чем признать пробел в свидетельстве.
    """
    from ..file_ops import _ledger, _msgindex

    place_to_addr = {}
    for addr, row in (fresh_by_msg or {}).items():
        place_to_addr[_msgindex.place(row)] = addr

    written = []
    ready = [s for s in (summaries or []) if s.get("wrap") is not None]
    if ready:
        # Готовый текст окна может быть без рамки (`none`) — по метке его не найти. Ищем ровно
        # ту вставку, что сделали: её текст известен (`_head_content`).
        heads = [_head_content(s, bool(r.get("kept"))) for r, s in zip(runs, summaries)]
        summary_rows = [r for r in new_rows if "timestamp" in r and r.get("content") in heads]
    else:
        summary_rows = [r for r in new_rows
                        if "timestamp" in r and is_summary_row(r.get("content", ""))]
    for k, (run_info, row) in enumerate(zip(runs, summary_rows)):
        addr = place_to_addr.get(_msgindex.place(row))
        if not addr:
            continue
        # Классификация опроса (какой набор у какой темы) — у сводки компакции; у вставки окна
        # LLMOps её нет. Сводки идут в порядке пробегов.
        mine = (summaries or [])[k] if k < len(summaries or []) else {}
        sets = mine.get("sets")
        # Кто поглотил — по поставщику вставки: `llm ops` — своим словом, иначе `compact`.
        by = "llmops" if any(x.get("provider") == "llmops" for x in (summaries or [])) else "compact"
        # Вставка окна: каким пресетом и в какой обёртке (поправка 10) — у компакции их нет.
        extra = ({"preset": mine.get("preset"), "wrap": mine.get("wrap_chosen")}
                 if by == "llmops" else {})
        # Пожелание пользователя к компакции (поправка 11, `Plan34`) — у сводки компакции.
        if mine.get("wish"):
            extra["wish"] = mine["wish"]
        if _ledger.note_absorbed(session_id, addr, run_info["addresses"], by=by, sets=sets,
                                 **extra):
            written.append({"summary": addr, "members": run_info["addresses"]})
    return written
