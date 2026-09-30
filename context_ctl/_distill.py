"""Холодный опрос по нитям: маршрут → разбор каждой темы → документ, собранный КОДОМ.

Замысел целиком — `__HQ/vision/Vision08__thread-distillate.md`, согласован с владельцем
2026-09-21. Границы, оплаченные измерением, — `Vision06`. Порядок «сначала горячий,
потом холодный» — `Vision07`; ГОРЯЧЕГО ЗДЕСЬ ЕЩЁ НЕТ, это только холодная половина.

ЧЕМ ЭТО ОТЛИЧАЕТСЯ ОТ `/ctx-compact`. Тот делает ОДИН вызов на пробег и кладёт результат в
базу. Здесь вызовов много, они зависят друг от друга (ответ подставляется в следующий
вопрос), а в базу не пишется НИЧЕГО: первый контур проверяет качество опроса, и мешать это
с необратимой правкой нельзя — иначе каждый прогон стоил бы сессии.

ПОЧЕМУ ОДИН ПОТОК И ОДНА ОЧЕРЕДЬ. Владелец, 2026-09-21: основное время уходит не на сами
вопросы, а на старт и стоп модели. Значит весь опрос идёт непрерывно и модель между темами
не отпускается. Разбить это на команды значило бы платить за загрузку на каждом шаге.

ОТЧЁТ ПИШЕТСЯ ПОСЛЕ КАЖДОГО ОТВЕТА, а не в конце. Прогон длится минуты и может оборваться
(сервер, таймаут, перезапуск гейтвея); ответы, за которые уже заплачено, терять нельзя.
"""
from __future__ import annotations

import json
import threading
import time

from . import _apply, _compact, _cut, _llm, _md, _pairs, _progress, _prompts, _render, _settings, _state

NL_ = chr(10)

# ── Короткие строки, которыми правится весь прогон ──────────────────────────
# Сколько ходов базы захватывать ЗА концом выбранного куска. Vision08: дублирование
# дешевле потери целостности, а обрыв ровно по границе оставляет тему без развязки.
RESERVE_ROWS = 4

# Начало сессии в данные попадает ВСЕГДА, чем бы ни был выбран кусок: без первой реплики
# человека пропадает «зачем», а она почти всегда и есть постановка задачи.
ALWAYS_FROM_START = True

# Прогрев. Диагноз владельца 2026-09-17: сервер падает, когда наш первый запрос сам
# инициирует загрузку модели, а второй приходит, пока она встаёт.
WARMUP = True
SETTLE_S = 3.0

# Потолка по числу полных тем НЕТ — решение владельца 2026-09-21. Здесь только
# предохранитель от заведомо больного ответа (модель назвала сто тем).
MAX_TOPICS = 12

_thread_lock = threading.Lock()
_state_now: dict = {}


def _titles_block(titles) -> str:
    """Список названий для подстановки в вопрос. Нумерация для человека в отчёте; модель
    отвечает НАЗВАНИЯМИ, не номерами, — индексы она сдвигает молча."""
    return NL_.join(f"{i}. {t}" for i, t in enumerate(titles, 1))


def positions_for(rows, wanted_last: int) -> list:
    """Какие строки базы уезжают в модель.

    `wanted_last` — индекс последней выбранной строки. Берём с начала (см. константу) и на
    `RESERVE_ROWS` дальше: запас за зоной обрезки стоит токенов у суммаризатора и ничего не
    стоит в результате.
    """
    start = 0 if ALWAYS_FROM_START else max(0, wanted_last - 1)
    stop = min(len(rows), wanted_last + 1 + RESERVE_ROWS)
    return list(range(start, stop))


def resolve_split(titles: list, parsed: dict) -> tuple:
    """Ответ про главные/филер → `(полный разбор, филер)`. Отдельной функцией, потому что
    вырожденные случаи разрешает КОД, а не лишний вопрос модели.

    Тема не попала никуда — идёт в полный разбор: разобрать лишнее дешевле, чем потерять.
    Попала в оба списка — главная побеждает. Порядок сохраняется исходный, чтобы ответ на
    вопрос про классы (массив по порядку) ложился на те же темы.
    """
    parsed = parsed or {}
    filler = [t for t in parsed.get("filler") or [] if t in titles]
    main = [t for t in parsed.get("main") or [] if t in titles]
    full = [t for t in titles if t in main or t not in filler]
    return full, [t for t in titles if t in filler and t not in full]


def _ask_text(data: str, question: str, note: dict, system: str = "") -> str:
    """Свободный ответ прозой. Схемы нет НАРОЧНО: `{"type": "string"}` не ограничивает
    ничего, а JSON вокруг длинной прозы только добавляет экранирования (`Vision06`)."""
    started = time.monotonic()
    answer = _llm.ask(data + NL_ * 2 + question, system=system or _prompts.sys_cold())
    note.update({"ms": int((time.monotonic() - started) * 1000),
                 "tokens": answer.get("tokens", 0), "error": answer.get("error", ""),
                 "ok": bool(answer.get("ok"))})
    return str(answer.get("text") or "")


def _ask_json(data: str, question: str, schema_and_name: tuple, note: dict,
              system: str = ""):
    """Ответ, ограниченный схемой. Возвращает разобранный объект либо `None`."""
    schema, name = schema_and_name
    started = time.monotonic()
    answer = _llm.ask_structured(data, question, schema, schema_name=name,
                                 system=system or _prompts.sys_cold())
    note.update({"ms": int((time.monotonic() - started) * 1000),
                 "tokens": answer.get("tokens", 0), "error": answer.get("error", ""),
                 "ok": bool(answer.get("ok")), "raw": answer.get("text", "")})
    return answer.get("parsed")


def section_parts(topic: dict) -> list:
    """Разделы одной темы — `(заголовок, текст)`, в порядке документа.

    Пустые не попадают вовсе: «не содержатся» через следующую компакцию читается как
    содержание (`Vision06`).
    """
    pairs = [(_prompts.doc_talk_head(), topic.get("talk")),
             (_prompts.doc_pending_head(), topic.get("pending")),
             (_prompts.doc_settled_head(), topic.get("settled")),
             (_prompts.doc_open_head(), topic.get("open")),
             (_prompts.doc_errors_head(), topic.get("errors")),
             (_prompts.doc_values_head(), topic.get("values")),
             (_prompts.doc_pack_head(topic.get("class")), topic.get("pack"))]
    # Разделы темы лежат под нашим `###`, значит заголовкам модели остаётся `####` и глубже.
    return [(head, _md.normalize(_strip_heading(body), _md.FLOOR))
            for head, body in pairs if str(body or "").strip()]


def section_text(topic: dict) -> str:
    """Собранный раздел темы одним текстом — им же идёт самопроверка."""
    return (NL_ * 2).join(f"{head}{NL_}{NL_}{body}" for head, body in section_parts(topic))


def _strip_heading(text: str) -> str:
    """Снять заголовок, который модель написала себе сама, — но ТОЛЬКО если он один.

    Живой прогон 2026-09-21: клей начинался с `# Тема: …`, а заголовок темы уже стоит над
    ним от нас — получалось два подряд, и уровни разъезжались. Просить «не пиши заголовок»
    тоже надо, но код дешевле и надёжнее просьбы.

    СУЖЕНО 2026-09-22, когда появилась нормализация уровней. Прежнее правило снимало первый
    заголовок всегда, и в ответе вида `## Проблема … ## Итог` исчезала «Проблема», а «Итог»
    оставался: один из братьев пропадал без причины. Единственный заголовок — это подпись к
    ответу, и её мы снимаем; два и больше — это структура, и она принадлежит ответу.
    """
    body = str(text or "").strip()
    if len(_md.headings(body)) > 1:
        return body
    lines = body.split(NL_)
    while lines and (not lines[0].strip() or lines[0].lstrip().startswith("#")):
        lines.pop(0)
    return NL_.join(lines).strip()


def build_document(topics: list, filler_text: str, additions: str,
                   constraints: str = "") -> str:
    """Документ собирается КОДОМ из отдельных ответов — в этом весь смысл затеи.

    Секции, на которые ответа нет, в документ не попадают ВОВСЕ: «не содержится» через
    следующую компакцию читается как содержание (`Vision06`).

    ДОКУМЕНТ — ЭТО САМИ ОТВЕТЫ, А НЕ ПЕРЕСКАЗ. Правка владельца 2026-09-21, и она отменяет
    прежнее устройство: был шаг «склей свои ответы в один текст», то есть ВТОРАЯ
    суммаризация — модель получала три подробных ответа и усредняла их в один. Замер того
    же прогона: клей 5 КБ против 9 КБ сырых ответов, то есть две трети выброшены. Ровно тот
    механизм, от которого мы уходили, только дороже.

    Теперь склейка — дело КОДА: ответы ложатся разделами, дословно. Модель пишет поверх
    собранного ровно один раз — хвостом добавлений (Q12).

    Цена названа честно: на малых данных разделы будут повторять друг друга. Владелец,
    2026-09-21: «на малых данных это повторы по делу, на больших — грани того же самого с
    разных сторон». Повтор не потеря.
    """
    out = [_prompts.doc_title(), ""]
    for t in topics:
        out += [_prompts.doc_topic_head(t["title"]), ""]
        for head, body in section_parts(t):
            out += [head, "", body, ""]
        if t.get("suspect"):
            out += [_prompts.doc_suspect(t["suspect"]), ""]
    # ДОКУМЕНТНЫЕ разделы висят прямо под `##`, своей `###` над ними нет — значит верхний
    # уровень, доступный заголовкам модели, здесь третий, а не четвёртый (владелец,
    # 2026-09-22): углублять там, где ступеньки нет, значит рисовать пустую ступеньку.
    top = _md.FLOOR - 1
    if str(constraints or "").strip():
        out += [_prompts.doc_constraints_head(), "",
                _md.normalize(constraints.strip(), top), ""]
    if str(filler_text or "").strip():
        out += [_prompts.doc_filler_head(), "",
                _md.normalize(filler_text.strip(), top), ""]
    if str(additions or "").strip():
        out += [_prompts.doc_additions_head(), "",
                _md.normalize(additions.strip(), top), ""]
    return NL_.join(out).rstrip() + NL_


def dump_dir(session_id: str):
    """Папка прогона. ОДИН файл на всё оказался нечитаемым: двадцать тысяч символов
    данных и столько же вопросов хоронили под собой то единственное, ради чего всё
    затевалось (владелец, живьём 2026-09-21). Поэтому четыре файла, и результат первым.
    """
    stamp = time.strftime("%Y%m%d_%H%M%S")
    folder = _state.state_dir() / "distill" / f"{_safe(session_id)}_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(name or "none"))[:60]


# Имена нумерованы, чтобы в проводнике результат лежал сверху, а данные внизу.
# Забор для ответов модели. Четыре кавычки, потому что внутри ответа законно встречаются
# тройные — см. `write_dump`.
FENCE = "`" * 4
# Забор для JSON: в разобранном ответе тройных кавычек нет по построению, а четвёрка
# мешает человеку обернуть кусок отчёта в свой забор, когда он его кому-то показывает.
FENCE3 = "`" * 3

FILE_RESULT = "1_РЕЗУЛЬТАТ.md"
FILE_RUN = "2_прогон.md"
FILE_STEPS = "3_вопросы.md"
FILE_DATA = "4_данные.md"


def write_dump(folder, session_id: str, data: str, steps: list, document: str,
               lines=None) -> None:
    """Четыре файла: результат, ход прогона, вопросы с ответами, блок данных.

    Пишется после КАЖДОГО шага целиком, а не дописывается: перезапись файла в сотню
    килобайт стоит миллисекунды, а дописывание требовало бы помнить, что уже на диске, —
    и первый же оборванный прогон оставил бы файл в непонятном состоянии.
    """
    (folder / FILE_RESULT).write_text(
        document or "# Дистиллят\n\n(ещё не собран — прогон не дошёл до разбора тем)\n",
        encoding="utf-8")

    run = [f"# Прогон — сессия {session_id}", "",
           f"Данных: {len(data)} символов. Шагов: {len(steps)}. "
           f"Документ: {len(document or '')} символов.", "",
           f"Результат — `{FILE_RESULT}`, вопросы — `{FILE_STEPS}`, данные — `{FILE_DATA}`.",
           "", "| # | шаг | разобран | мс | токены |", "|---|---|---|---|---|"]
    for i, s in enumerate(steps, 1):
        run.append(f"| {i} | {s['key']} | {'да' if s.get('ok') else 'НЕТ'} | "
                   f"{s.get('ms', 0)} | {s.get('tokens', 0)} |")
    if lines:
        run += ["", "## Ход", ""] + [f"- {ln}" for ln in lines]
    (folder / FILE_RUN).write_text(NL_.join(run) + NL_, encoding="utf-8")

    out = [f"# Вопросы и ответы — сессия {session_id}", "",
           f"Перед каждым вопросом уезжает один и тот же блок данных (`{FILE_DATA}`) "
           "и системный промт; здесь только хвост.", ""]
    # ЗАБОР ИЗ ЧЕТЫРЁХ КАВЫЧЕК, а не из трёх (владелец, 2026-09-21 — оба раза по живому
    # файлу). Ответ надо огородить, иначе он сливается с разметкой отчёта и теряется на
    # глаз. Но тройной забор рвётся о тройной забор ВНУТРИ ответа, а клей цитирует код
    # сплошь и рядом. Четыре кавычки markdown разрешает, и они держат тройные внутри.
    #
    # Вопрос — тем же забором. Полоска не годилась: `---` сразу после текста markdown
    # считает подчёркиванием ЗАГОЛОВКА, и разделителя фактически не было — в отчёте
    # не разобрать, где кончился вопрос и начался ответ (владелец, 2026-09-21).
    for i, s in enumerate(steps, 1):
        out += [f"## {i}. {s['key']}", "", "**Вопрос:**", "",
                FENCE, s.get("q", ""), FENCE, ""]
        if s.get("error"):
            out += [f"**ОШИБКА:** {s['error']}", ""]
        if s.get("parsed") is not None:
            # JSON — ТРОЙНЫМ забором (владелец, 2026-09-21). Четвёрка нужна там, где внутри
            # законно встречается тройка: в прозе модель цитирует код сплошь и рядом. В
            # разобранном ответе кавычек нет по построению, а четвёрка мешает человеку —
            # кусок отчёта нельзя обернуть в свой забор, чтобы кому-то его показать.
            out += ["**Разобрано:**", "", FENCE3 + "json",
                    json.dumps(s["parsed"], ensure_ascii=False, indent=2), FENCE3, ""]
        elif s.get("answer"):
            out += ["**Ответ:**", "", FENCE, s["answer"], FENCE, ""]
        elif s.get("raw"):
            out += ["**Сырой ответ (схема не сработала):**", "", FENCE, s["raw"], FENCE, ""]
    (folder / FILE_STEPS).write_text(NL_.join(out) + NL_, encoding="utf-8")

    (folder / FILE_DATA).write_text(
        f"# Блок данных — сессия {session_id}{NL_}{NL_}"
        f"{len(data)} символов. Общий префикс ВСЕХ вопросов прогона.{NL_}{NL_}"
        f"---{NL_}{NL_}{data}{NL_}", encoding="utf-8")


def run(session_id: str, engine, raw_args: str = "") -> dict:
    """Сухой прогон на маршруте модели сессии (`_llm.use_route`)."""
    with _llm.use_route(engine.live_agent()):
        return _run(session_id, engine, raw_args)


def _run(session_id: str, engine, raw_args: str = "") -> dict:
    """СУХОЙ ПРОГОН: выбрать кусок сессии и спросить. Ни базы, ни памяти не трогает.

    Вся работа с моделью живёт в `interrogate` — здесь только выбор данных. Так `/ctx-compact`
    получает ТО ЖЕ ЯДРО, а не свою копию: разойдись они, сухой прогон начал бы врать про то,
    что сделает боевой, и доверять ему стало бы нельзя (`Vision08`, раздел ПРИМЕНЕНИЕ).
    """
    facts = {"ok": False, "session": session_id, "asked": 0, "topics": 0, "lines": []}
    lines = facts["lines"]

    ok, why = _llm.available()
    if not ok:
        lines.append(f"ИТОГ: не спрошено — {why}")
        return facts
    lines.append(f"модель опроса: {why}")

    agent = engine.live_agent()
    session, session_why = _apply.find_gateway_session(agent)
    if session is None:
        lines.append(f"спрашивать не о чем — {session_why}")
        return facts
    rows, rows_why = _apply.db_history(agent)
    if rows_why:
        lines.append(rows_why)
        return facts

    by_msg = _compact._index(session_id, rows)
    if not by_msg:
        lines.append("адресов сообщений нет — индексатор не разметил сессию")
        return facts

    spec = str(raw_args or "").strip()
    take_all = spec == "" or spec.lower() in ("all", u"всё", u"все")
    addr_by_row = {id(row): addr for addr, row in by_msg.items()}
    # ДОЛЯ — тот же аргумент и то же правило, что у боевой команды: сухой прогон обязан
    # показывать ИМЕННО то, что сделает боевой, иначе он врёт (`Vision08`).
    share = None if take_all else _compact.parse_share(spec)
    if share is not None:
        report = _cut.select(rows, _settings.compaction(), share)
        lines.extend(_cut.explain(report))
        if report["index"] < 0:
            lines.append("ИТОГ: не спрошено — по этой доле резать нечего")
            return facts
        chosen = list(range(report["start"], report["index"] + 1))
    elif take_all:
        chosen = list(range(len(rows)))
    else:
        # Диапазон режет ленту по ПОЛОЖЕНИЮ концов — то же правило, что у боевой команды.
        chosen, _bad, gone = _compact.positions_for_spec(rows, by_msg, spec)
        if gone:
            lines.append("концы или адреса не найдены: "
                         + ", ".join(_compact._addr(n) for n in gone[:8]))
    if not chosen:
        lines.append("ИТОГ: не спрошено — в выбранном нет ни одной строки")
        return facts

    positions = positions_for(rows, chosen[-1])
    addr_of = {i: addr_by_row.get(id(rows[i]), "") for i in positions}
    # ПУЗЫРЬКИ сухого прогона — дырки внутри выбора. Так их можно проверить прямо сейчас,
    # списком адресов (`/ctx-distill m0001,m0003`), не дожидаясь галочек в панели.
    #
    # ПРАВИЛО ВЛАДЕНИЯ ГОНИМ И ЗДЕСЬ, хотя писать нам нечего. Пузырёк — это то, что
    # ОСТАНЕТСЯ жить со своими словами, и после разбора пар дырок обычно меньше: реплики,
    # чьи результаты утонули, исчезают, а те, чью прозу съели, становятся оболочками. Не
    # спроси мы здесь то же самое, сухой прогон показал бы модели другой набор ярлыков
    # `STAYS VERBATIM`, чем боевой, — то есть соврал бы ровно о том, ради чего он есть.
    decision = _pairs.settle(rows, chosen)
    hollow = (set(chosen) - decision["eaten"]) | {
        i for i, fresh in decision["patch"].items() if fresh is None}
    skip = set(chosen) | hollow
    kept = [i for i in range(chosen[0], chosen[-1] + 1) if i not in skip]
    for note in decision["notes"]:
        lines.append("пары: " + note)
    # Зона работ — сам выбор вместе с пузырьками внутри него. Остальное, что уехало в
    # модель, это префикс и суффикс: связность, а не предмет (`Vision08`, три зоны).
    out = interrogate(session_id, rows, positions, addr_of, lines, kept=kept,
                      work=list(range(chosen[0], chosen[-1] + 1)))
    facts.update({"ok": bool(out.get("ok")), "asked": out.get("asked", 0),
                  "topics": out.get("topics", 0), "text": out.get("text", "")})
    lines.append("база НЕ тронута: это сухой прогон")
    return facts


def _span(addr_of, chunk) -> str:
    """Адреса куска ПЕРЕЧИСЛЕНИЕМ, в порядке ленты. Подряд идущие сворачиваются.

    ПОЧЕМУ НЕ ДИАПАЗОН «первый…последний» (владелец, 2026-09-22). После компакции адреса в
    ленте идут не по возрастанию: новый дистиллят получает свежий номер, а встаёт на место
    первого съеденного — и большой номер оказывается впереди маленьких. Диапазон в такой
    ленте печатался бы как `m003b…m0036`, то есть как перевёрнутый или пустой интервал.

    Перечисление же само несёт смысл: видно, где номера идут ровно (исходная лента), а где
    выбивается чужой — значит вставлен позже. Это подпорка логической цепочке за нулевую
    цену; сотня адресов в префиксе никого не разорит.
    """
    names = [(addr_of.get(i, ""), _compact._addr_number(addr_of.get(i, "")))
             for i in chunk if addr_of.get(i, "")]
    if not names:
        return ""
    out, run = [], [names[0]]
    for name, num in names[1:]:
        if num >= 0 and run[-1][1] >= 0 and num == run[-1][1] + 1:
            run.append((name, num))
            continue
        out.append(run)
        run = [(name, num)]
    out.append(run)
    # Свёртка только от трёх подряд: на двух `a…b` длиннее, чем `a, b`, и ничего не экономит.
    return ", ".join(f"{g[0][0]}…{g[-1][0]}" if len(g) >= 3
                     else ", ".join(n for n, _ in g) for g in out)


def zoned_block(rows, positions, addr_of, kept, work) -> tuple:
    """Блок данных ТРЕМЯ ЗОНАМИ: префикс, зона работ, суффикс. `(текст, карта)`.

    Найдено живьём 2026-09-21: без этого деления модель разбирает всю ленту, включая уже
    свёрнутое прежним дистиллятом, — и второй дистиллят повторяет первый. Два множества у нас
    были разведены в коде с первого шага, но модели о них не сообщалось ни слова.

    Зона закрывается ПАРНЫМ тегом, строки — нет. Конец строки выводится из ярлыка следующей;
    конец зоны вывести неоткуда, а закрывающая строка вдобавок заявляет, что эта граница
    сильнее всех, что были внутри (довод владельца).
    """
    work = set(work or ())
    lo = min(work) if work else (positions[0] if positions else 0)
    hi = max(work) if work else (positions[-1] if positions else 0)
    parts = {"prefix": [i for i in positions if i < lo],
             "work": [i for i in positions if lo <= i <= hi],
             "suffix": [i for i in positions if i > hi]}

    def render(chunk):
        return _render.render_block(rows, chunk, addr_of, kept=kept)

    spans = {name: _span(addr_of, chunk) for name, chunk in parts.items()}
    blocks, number = [], 0
    for name, title, note in (("prefix", _prompts.zone_context_title(),
                               _prompts.zone_prefix_note()),
                              ("work", _prompts.zone_work_title(),
                               _prompts.zone_work_note()),
                              ("suffix", _prompts.zone_context_title(),
                               _prompts.zone_suffix_note())):
        number += 1
        if not parts[name]:
            continue
        blocks.append(_prompts.zone_open(number, 3, title, spans[name], note)
                      + NL_ * 2 + render(parts[name]) + NL_ * 2
                      + _prompts.zone_close(number, title,
                                            "this boundary is stronger than any inside it"))
    return (NL_ * 2).join(blocks), _prompts.zone_map(spans["prefix"], spans["work"],
                                                     spans["suffix"])


def interrogate(session_id: str, rows, positions, addr_of, lines=None, kept=None,
                work=None) -> dict:
    """ЯДРО холодного опроса: из готовых строк и позиций — готовый документ.

    ОДНО НА ДВЕ ДВЕРИ. Сюда приходит и сухой `/ctx-distill`, и боевой `/ctx-compact`; всё,
    что их различает, — записывает ли вызывающий результат. Решение владельца 2026-09-21:
    пути не «объединяются» отдельной работой, у боевого просто меняется мозг.

    ЧТО СЮДА ПЕРЕДАВАТЬ. `positions` — то, что модель УВИДИТ, и оно ШИРЕ того, что будет
    замещено: с начала сессии плюс запас за краем. Множество замещаемого ядру неизвестно
    вовсе и остаётся заботой вызывающего — путать их нельзя (`Vision08`, два множества).

    В базу здесь не пишется ничего и никогда. Отчёт на диск — пишется, после каждого ответа.
    """
    facts = {"ok": False, "session": session_id, "asked": 0, "topics": 0,
             "text": "", "path": ""}
    lines = lines if lines is not None else []
    kept = sorted(set(kept or ()))
    body, map_text = zoned_block(rows, positions, addr_of, kept, work or positions)
    system = _prompts.sys_cold(map_text)
    # Список пузырьков ПОВТОРЯЕТСЯ в конце данных: ярлык над строкой остаётся далеко наверху
    # длинной ленты, а побеждает ближнее к ответу. Стоит в общем префиксе — платим один раз.
    notice = _prompts.kept_notice([addr_of.get(i, "") for i in kept])
    # Громкие границы с обеих сторон: без закрывающей вопрос на длинной ленте читается как
    # продолжение разговора (владелец, 2026-09-21, по их же приёму).
    data = (_prompts.data_header() + NL_ * 2 + body + NL_ * 2
            + (notice + NL_ * 2 if notice else "") + _prompts.data_footer())
    if kept:
        lines.append(f"пузырьков: {len(kept)} — помечены ярлыком и перечислены в конце "
                     "данных; их содержимое пересказывать не просим")
    facts["data_chars"] = len(data)
    shape = _render.stats(rows, positions)
    lines.append(f"данных: {len(data)} символов | строк {shape['rows']} "
                 f"(реплик {shape['prose']}, ответов тулов {shape['tool_results']}, "
                 f"вызовов {shape['calls']})")

    steps: list = []
    folder = dump_dir(session_id)
    document = ""

    def save():
        """Отчёт на диск прямо сейчас. Падение записи прогон не отменяет."""
        try:
            write_dump(folder, session_id, data, steps, document, lines)
            _state_now["path"] = str(folder / FILE_RESULT)
        except Exception as exc:  # noqa: BLE001
            _state_now["error"] = f"отчёт не пишется: {exc}"

    # Ход для панели (`_progress`): всего вопросов по плану и число тем — уточняются по ходу.
    plan = {"total": 0, "topics": 0}

    def step(key: str, question: str, schema_and_name=None):
        """Один вопрос: задать, записать, сохранить. Единственная точка вызова модели."""
        label, n = _progress.label_of(key)
        _progress.progress(session_id, stage="questions", step=len(steps) + 1,
                           total=max(plan["total"], len(steps) + 1) if plan["total"] else 0,
                           label=label, topic=f"{n}/{plan['topics']}" if n else "")
        note = {"key": key, "q": question}
        if schema_and_name is None:
            note["answer"] = _ask_text(data, question, note, system)
            note["parsed"] = None
        else:
            note["parsed"] = _ask_json(data, question, schema_and_name, note, system)
        steps.append(note)
        _state_now["done"] = len(steps)
        mark = "✓" if note.get("ok") else "✗"
        lines.append(f"  {mark} {key}: {note.get('ms', 0)} мс"
                     + (f" — {note['error']}" if note.get("error") else ""))
        save()
        return note

    if WARMUP:
        _progress.progress(session_id, stage="warmup")
        started = time.monotonic()
        warm = _llm.ask("ok")
        took = int((time.monotonic() - started) * 1000)
        lines.append(f"прогрев: {took} мс"
                     + (" (похоже, модель грузилась)" if took > 5000 else "")
                     + (f" — {warm.get('error')}" if warm.get("error") else ""))
        if not warm.get("ok"):
            # Прогрев — голое «ok» без данных. Не ответил — данные сессии никуда не шлём.
            facts["error"] = f"модель не ответила на прогрев: {warm.get('error') or 'пустой ответ'}"
            save()
            return facts
        time.sleep(SETTLE_S)

    # ── Маршрут ──────────────────────────────────────────────────────────────
    zone = _prompts.zone_note()
    parsed = step("q1_count", _prompts.q1_count() + zone, _prompts.s1_count())["parsed"]
    count = int((parsed or {}).get("count") or 0) or 1

    parsed = step("q2_titles", _prompts.q2_titles(count) + zone,
                  _prompts.s2_titles())["parsed"]
    titles = [str(t).strip() for t in (parsed or {}).get("titles") or [] if str(t).strip()]
    if not titles:
        lines.append("ИТОГ: тем не названо — дальше идти не с чем")
        failed = next((s["error"] for s in reversed(steps) if s.get("error")), "")
        facts["error"] = f"тем не названо — {failed}" if failed else "модель не назвала ни одной темы"
        facts["asked"] = len(steps)
        return facts
    # РАСХОЖДЕНИЕ ЧИСЛА И СПИСКА. Прежде принимали массив как есть, и живой прогон
    # 2026-09-21 показал цену: на тех же данных модель насчитала одну тему и нарезала много,
    # раздув отчёт. Спрашиваем только когда список ДЛИННЕЕ — в эту сторону тянет форма
    # вопроса; когда он короче, модель просто успокоилась, и это не подозрительно.
    if len(titles) > count >= 1:
        lines.append(f"  тем названо {len(titles)}, а насчитано {count} — разбираем")
        verdict = step("q2_recount", _prompts.q2_recount(count, _titles_block(titles)),
                       _prompts.s2_recount())["parsed"] or {}
        if _prompts.recount_says_merge(verdict.get("verdict", "")):
            parsed = step("q2_merge", _prompts.q2_titles_merge(count, _titles_block(titles)),
                          _prompts.s2_titles())["parsed"]
            merged = [str(t).strip() for t in (parsed or {}).get("titles") or []
                      if str(t).strip()]
            # Пустой ответ на слияние — беда слияния, а не повод остаться без тем.
            if merged:
                lines.append(f"  сведено: {len(titles)} → {len(merged)}")
                titles = merged
        else:
            lines.append("  вердикт: темы и правда разные — список оставлен")
    elif len(titles) != count:
        lines.append(f"  тем названо {len(titles)}, а насчитано {count} — берём названные")
    if len(titles) > MAX_TOPICS:
        lines.append(f"  тем {len(titles)} — разбираем первые {MAX_TOPICS}")
        titles = titles[:MAX_TOPICS]

    filler: list = []
    if len(titles) == 1:
        # Тема одна — вопроса про склейку нет вовсе, филера не существует.
        full = list(titles)
    else:
        parsed = step("q3_split", _prompts.q3_split(_titles_block(titles)) + zone,
                      _prompts.s3_split(titles))["parsed"]
        full, filler = resolve_split(titles, parsed)
    facts["topics"] = len(full)
    lines.append(f"тем: {len(titles)} | полный разбор: {len(full)} | филер: {len(filler)}")

    # ВКУС каждой темы. Заменил прежний `q4_classes` (владелец, 2026-09-21): тот выбирал
    # «какую пачку добавить», а выбирать надо, НА КАКОМ ЯЗЫКЕ спрашивать вообще. Вопрос про
    # ошибки и файлы в разговоре про жизнь не просто пустой — технический словарь
    # разворачивает модель в техническую рамку и портит ответ.
    #
    # Ключи схемы — САМИ НАЗВАНИЯ тем: грамматика обязывает дать ровно по одной записи на
    # тему. Массив пар этого не давал, и длина уплывала живьём.
    parsed = step("q_kinds", _prompts.q_kinds(_titles_block(full)) + zone,
                  _prompts.s_kinds(full))["parsed"] or {}
    kinds = {t: _prompts.kind_of(parsed.get(t, "")) for t in full}
    plan["topics"] = len(full)
    plan["total"] = _progress.planned(len(steps), [kinds[t] for t in full], bool(filler))

    # ── Полный разбор каждой темы ────────────────────────────────────────────
    topics: list = []
    for n, title in enumerate(full):
        scope = _prompts.topic_scope(title, n + 1, len(full))
        kind = kinds.get(title, "other")
        # РАЗГОВОР НЕ ДОПРАШИВАЕМ. Технические вопросы здесь вредны: модель начнёт
        # пересказывать болтовню как отладку (замысел владельца 2026-09-21).
        if kind == "talk":
            topics.append({"title": title, "class": kind, "suspect": "",
                           "talk": step(f"q_talk[{n + 1}]",
                                        scope + _prompts.q_talk())["answer"]})
            document = build_document(topics, "", "")
            save()
            continue
        # ВОРОТА. Дешёвые булевы решают, какие дорогие вопросы задавать вовсе: секция, на
        # которую пришло `false`, не создаётся и не спрашивается (владелец, 2026-09-21).
        gate = step(f"q_gate[{n + 1}]", _prompts.q_gate(title, n + 1, len(full)),
                    _prompts.s_gate())["parsed"] or {}
        plan["total"] -= _progress.skipped_by_gate(gate)

        def maybe(field, key, question, _n=n, _scope=scope, _gate=gate):
            """Вопрос — только если ворота его пропустили. Ворот нет (сбой схемы) —
            спрашиваем: потерять секцию дороже, чем задать лишний вопрос."""
            if field in _gate and not _gate.get(field):
                return ""
            return step(f"{key}[{_n + 1}]", _scope + question)["answer"]

        # «Пришли» не за воротами: тема без единого итога — не тема.
        settled = step(f"q5_settled[{n + 1}]",
                       scope + _prompts.q5_settled())["answer"]
        pending = maybe("pending_task", "q_pending_task", _prompts.q_pending_task())
        open_text = maybe("open_items", "q6_open", _prompts.q6_open())
        errors = maybe("errors_fixes", "q_errors_fixes", _prompts.q_errors_fixes())
        values = maybe("critical_values", "q_critical_values", _prompts.q_critical_values())
        pack = maybe("class_details", "q7_pack",
                     _prompts.q7_pack(kind, settled, open_text))
        # Шага «склей свои ответы» больше нет — склеивает КОД (см. `build_document`).
        topic = {"title": title, "class": kind, "settled": settled, "pending": pending,
                 "open": open_text, "errors": errors, "values": values, "pack": pack,
                 "suspect": ""}
        # Проверяем ТО, ЧТО ПОЕДЕТ ДАЛЬШЕ: собранный раздел, а не отдельный пересказ.
        check = step(f"q10_check[{n + 1}]", _prompts.q10_selfcheck(section_text(topic)),
                     _prompts.s10_selfcheck())["parsed"] or {}
        # Ключ вердикта — целая фраза, и его имя берётся из одного места: имя ключа тут не
        # оформление, а те токены, которыми модель приходит к суждению (`_prompts`).
        if check.get(_prompts.selfcheck_key()):
            topic["suspect"] = str(check.get("what_exactly")
                                   or check.get("what") or "").strip()
        topics.append(topic)
        document = build_document(topics, "", "")
        save()

    # ── Филер и добавления ───────────────────────────────────────────────────
    filler_text = ""
    if filler:
        filler_text = step("q11_filler", _prompts.q11_filler(_titles_block(filler)))["answer"]

    # Запреты и ограничения — ОДИН раз на сессию, а не по темам: правило, сказанное в одной
    # теме, действует во всех. Дословно, потому что пересказ запрета запретом не является.
    parsed = step("q_constraints", _prompts.q_constraints_verbatim(),
                  _prompts.s_constraints_verbatim())["parsed"] or {}
    constraints = str(parsed.get("text") or "").strip() \
        if parsed.get(_prompts.CONSTRAINTS_KEY) else ""

    document = build_document(topics, filler_text, "", constraints)
    # Найденное самопроверкой отдаём вопросу о добавлениях: иначе он честно перескажет его
    # своими словами, и половина абзаца уйдёт на уже сказанное (живой прогон 2026-09-21).
    known = NL_.join(f"- {t['title']}: {t['suspect']}" for t in topics if t.get("suspect"))
    parsed = step("q12_additions", _prompts.q12_additions(document, known),
                  _prompts.s12_additions())["parsed"] or {}
    # НЕЧЕГО ДОБАВИТЬ — НЕ ПИШЕМ НИЧЕГО (владелец, 2026-09-21). Раньше код дописывал сюда
    # свою строку про «дополнений нет», и она была неверна дважды: захардкожена по-русски,
    # хотя язык документа задаёт сессия, — и пустой раздел через следующую компакцию читается
    # как содержание. Признак полноты остаётся булевым, просто `false` означает молчание.
    additions = str(parsed.get("text") or "").strip() if parsed.get("has_additions") else ""
    document = build_document(topics, filler_text, additions, constraints)
    save()

    facts.update({"ok": True, "asked": len(steps), "text": document,
                  "path": str(folder / FILE_RESULT)})
    lines.append(f"ИТОГ опроса: шагов {len(steps)}, тем разобрано {len(topics)}. "
                 f"Документ {len(document)} символов")
    lines.append(f"результат: {folder / FILE_RESULT}")
    save()
    return facts


def status_lines() -> list:
    st = dict(_state_now)
    if not st:
        return ["прогонов ещё не было"]
    head = ("ИДЁТ" if st.get("running") else "закончен") + f": сессия {st.get('session', '?')}"
    if st.get("started"):
        head += f", начат {st['started']}"
    out = [head, f"шагов сделано: {st.get('done', 0)}"]
    if st.get("error"):
        out.append(f"ОШИБКА: {st['error']}")
    if st.get("path"):
        out.append(f"результат: {st['path']}")
    for line in st.get("lines") or []:
        out.append("  " + line)
    return out


def _worker(session_id, engine, args) -> None:
    try:
        facts = run(session_id, engine, args)
        _state_now["lines"] = list(facts.get("lines") or [])
    except Exception as exc:  # noqa: BLE001
        _state_now["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        _state_now["running"] = False
        _thread_lock.release()


def handle(raw_args: str = "") -> str:
    """Слеш-команда `/ctx-distill`. Запускает опрос и отвечает СРАЗУ.

    Ответ мгновенный не для красоты: Hermes рвёт команду своим таймаутом на тридцати
    секундах и отправляет её ЗАНОВО, а опрос идёт минуты (`Vision06`).
    """
    from ._compact import _pick

    mark = "── ctx-distill (в базу не пишет) ──"
    if str(raw_args or "").strip().lower() in ("status", "статус", "?"):
        return NL_.join([mark] + status_lines())

    session_id, engine, args, lines = _pick(raw_args, mark)
    if engine is None:
        return NL_.join(lines)

    # РЕЗ ПОКАЗЫВАЕМ СРАЗУ. Он считается в фоне, а мгновенный ответ уходит раньше — и
    # человек видел только «запущено», не зная даже, понята ли доля (владелец, 2026-09-21).
    # Считать дёшево и безопасно: правило детерминированное, в модель не ходит, ничего не
    # меняет. Упасть здесь нельзя ничем: не показали рез — прогон всё равно идёт.
    share = _compact.parse_share(args)
    if share is not None:
        try:
            rows, _why = _apply.db_history(engine.live_agent())
            lines.extend(_cut.explain(_cut.select(rows, _settings.compaction(), share)) if rows
                         else [_why or "строк сессии не видно — рез покажет отчёт"])
        except Exception as exc:  # noqa: BLE001
            lines.append(f"рез сейчас не показать ({type(exc).__name__}) — он будет в отчёте")

    if not _thread_lock.acquire(blocking=False):
        return NL_.join(lines + ["опрос уже идёт — второй запуск не ставится в очередь"]
                        + status_lines())

    _state_now.clear()
    _state_now.update({"running": True, "session": session_id, "done": 0,
                       "started": time.strftime("%H:%M:%S"), "path": "", "error": "",
                       "lines": []})
    threading.Thread(target=_worker, args=(session_id, engine, args),
                     daemon=True, name="ctx-distill").start()
    return NL_.join(lines + [
        "запущено в фоне: маршрут, затем по пять вопросов на каждую тему — это минуты.",
        f"папка прогона появится в {_state.state_dir() / 'distill'}; "
        f"результат отдельным файлом `{FILE_RESULT}`, обновляется после каждого ответа",
        "как идёт — `/ctx-distill status`"])
