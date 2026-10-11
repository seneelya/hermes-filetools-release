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
from . import _insert, _questionnaires, _scenario
# Названия списком и «главные/филер» живут в реестре вместе с вопросами сессии (`Plan32.1`).
from ._questionnaires import resolve_split, titles_block as _titles_block  # noqa: F401

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


def positions_for(rows, wanted_last: int) -> list:
    """Какие строки базы уезжают в модель.

    `wanted_last` — индекс последней выбранной строки. Берём с начала (см. константу) и на
    `RESERVE_ROWS` дальше: запас за зоной обрезки стоит токенов у суммаризатора и ничего не
    стоит в результате.
    """
    start = 0 if ALWAYS_FROM_START else max(0, wanted_last - 1)
    stop = min(len(rows), wanted_last + 1 + RESERVE_ROWS)
    return list(range(start, stop))


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
    # СВОИ РАЗДЕЛЫ НАБОРА (`Plan32.1`): поле, которого документ не знает, встаёт после известных,
    # в порядке опросника и под заголовком из его описания (`topic["heads"]`, пишет
    # `Questionnaire.run`). Иначе новый набор — «персонажи», «мир» — спрашивал бы впустую.
    known = {"talk", "pending", "settled", "open", "errors", "values", "pack"}
    pairs += [(head, topic.get(field)) for field, head in topic.get("heads") or ()
              if field not in known]
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

    by_msg = _insert._index(session_id, rows)
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
        chosen, _bad, gone = _insert.positions_for_spec(rows, by_msg, spec)
        if gone:
            lines.append("концы или адреса не найдены: "
                         + ", ".join(_insert._addr(n) for n in gone[:8]))
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
    names = [(addr_of.get(i, ""), _insert._addr_number(addr_of.get(i, "")))
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
    Сам опрос — сценарий `_compaction` на примитивах `_scenario` (`Plan32.1`).
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

    folder = dump_dir(session_id)
    doc = {"text": ""}

    def save():
        """Отчёт на диск прямо сейчас. Падение записи прогон не отменяет."""
        try:
            write_dump(folder, session_id, data, run.steps, doc["text"], lines)
            _state_now["path"] = str(folder / FILE_RESULT)
        except Exception as exc:  # noqa: BLE001
            _state_now["error"] = f"отчёт не пишется: {exc}"

    run = _scenario.Run(session_id, data, system, lines, _state_now, save)

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

    span = set(work or positions)
    work_rows = sum(1 for i in positions if min(span) <= i <= max(span)) if span else 0
    _compaction(_scenario.View(run), run, facts, lines, doc, save, work_rows)
    if facts["ok"]:
        facts["path"] = str(folder / FILE_RESULT)
        lines.append(f"результат: {folder / FILE_RESULT}")
        save()
    return facts


def _compaction(s, run, facts: dict, lines: list, doc: dict, save, work_rows: int = 0) -> None:
    """СЦЕНАРИЙ компакции: темы → фильтры → выбор опросника каждой теме → опросники → филер,
    запреты, добавления. Пишет итог в `facts`, документ — в `doc["text"]`."""
    count = s.ask(_questionnaires.Q_COUNT) or 1
    titles = s.ask(_questionnaires.Q_TITLES, count=count)
    if not titles:
        lines.append("ИТОГ: тем не названо — дальше идти не с чем")
        failed = next((st["error"] for st in reversed(run.steps) if st.get("error")), "")
        facts["error"] = f"тем не названо — {failed}" if failed else "модель не назвала ни одной темы"
        facts["asked"] = len(run.steps)
        return
    # РАСХОЖДЕНИЕ ЧИСЛА И СПИСКА. Прежде принимали массив как есть, и живой прогон
    # 2026-09-21 показал цену: на тех же данных модель насчитала одну тему и нарезала много,
    # раздув отчёт. Спрашиваем только когда список ДЛИННЕЕ — в эту сторону тянет форма
    # вопроса; когда он короче, модель просто успокоилась, и это не подозрительно.
    if len(titles) > count >= 1:
        lines.append(f"  тем названо {len(titles)}, а насчитано {count} — разбираем")
        verdict = s.ask(_questionnaires.Q_RECOUNT, count=count, names=titles)
        if _prompts.recount_says_merge(verdict):
            merged = s.ask(_questionnaires.Q_MERGE, count=count, names=titles)
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

    # Тема одна — вопроса про склейку нет вовсе, филера не существует.
    full, filler = (list(titles), []) if len(titles) == 1 else s.ask(
        _questionnaires.Q_SPLIT, names=titles)
    facts["topics"] = len(full)
    lines.append(f"тем: {len(titles)} | полный разбор: {len(full)} | филер: {len(filler)}")

    # УЗЕЛ ВЫБОРА (`Plan32.1`, замысел владельца 2026-10-11): фильтры убирают неуместные наборы,
    # модели показываем оставшиеся — для чего каждый и что он спросит, — и она выбирает набор
    # каждой теме. Вопрос про ошибки и файлы в разговоре про жизнь не просто пустой: технический
    # словарь разворачивает модель в техническую рамку и портит ответ (владелец, 2026-09-21).
    sets = _questionnaires.offered({"work_rows": work_rows, "topics": full})
    picked = s.ask(_questionnaires.which_set(sets), names=full)
    # КЛАССИФИКАЦИЯ — в итог прогона и дальше в запись о поглощении (владелец, 2026-10-11: «на
    # всякий случай, вдруг пригодится»): какой набор модель выбрала каждой теме.
    facts["sets"] = [{"topic": t, "set": picked[t].name} for t in full]
    lines.append("наборы: " + "; ".join(f"{x['topic']} → {x['set']}" for x in facts["sets"]))
    run.plan["topics"] = len(full)
    run.plan["total"] = (len(run.steps) + sum(picked[t].max_steps for t in full)
                         + (1 if filler else 0) + _progress.FINAL_STEPS)

    # ── Каждая тема — своим опросником ──────────────────────────────────────
    topics: list = []
    for n, title in enumerate(full):
        topics.append(picked[title].run(s.about(title, n + 1, len(full)), section_text))
        doc["text"] = build_document(topics, "", "")
        save()

    # ── Филер, запреты, добавления ──────────────────────────────────────────
    filler_text = s.ask(_questionnaires.Q_FILLER, names=filler) if filler else ""
    constraints = s.ask(_questionnaires.Q_CONSTRAINTS).text
    doc["text"] = build_document(topics, filler_text, "", constraints)
    # Найденное самопроверкой отдаём вопросу о добавлениях: иначе он честно перескажет его
    # своими словами, и половина абзаца уйдёт на уже сказанное (живой прогон 2026-09-21).
    known = NL_.join(f"- {t['title']}: {t['suspect']}" for t in topics if t.get("suspect"))
    additions = s.ask(_questionnaires.Q_ADDITIONS, document=doc["text"], known=known).text
    doc["text"] = build_document(topics, filler_text, additions, constraints)
    save()

    facts.update({"ok": True, "asked": len(run.steps), "text": doc["text"]})
    lines.append(f"ИТОГ опроса: шагов {len(run.steps)}, тем разобрано {len(topics)}. "
                 f"Документ {len(doc['text'])} символов")


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
