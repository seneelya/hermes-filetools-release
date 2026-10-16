"""Компакция: выбранные сообщения → сводка модели → замена в базе (`Plan16`).

МЕХАНИЗМ ВСТАВКИ В ЛЕНТУ — В `_insert.py` (вынесен 2026-10-07, владелец: «механизм применения к ленте
одинаковый — это факт»). Здесь — только компакция: доля и автомат, опрос модели (`distill_summary`),
дистиллят, фоновый запуск и статус. Правила выбора ниже родились здесь и действуют в `_insert`.

ЧТО ЭТО ЗА ЦИКЛ. Пять шагов: выбрать сообщения → склеить в текст → позвать модель нашим
запросом → получить сводку → выпилить съеденное и положить полученное. Модель зовёт НАШЕ
ядро по нитям (`distill_summary`, `Vision08`) — их путь компакции (`_compact_prompt.py`)
снят 2026-09-22, см. `__HQ/attic/README.md`.

АДРЕСА ШЕСТНАДЦАТЕРИЧНЫЕ (`m003f`, `m00eb`) — так их выдаёт `_ledger.next_msg`. Диапазон
`m0001-m0041` это 65 сообщений, а не 41.

ЧЕТЫРЕ ПРАВИЛА, и два первых легко перепутать:

1. **Съедается ВЕСЬ пробег, вызовы инструментов и их ответы тоже.** Иначе контекст не
   уменьшается и компакции просто не происходит: в живой агентской сессии проза это единицы
   строк из сотен (замерено на сессии владельца 2026-09-16 — 2 из 235). Съеденное не
   исчезает совсем: `archive_dropped=True` оставляет строки в базе с `active = 0`.
2. **Предмет работы — СЛОВА**: реплики человека и речь модели. Ключи `prose`/`mute` пролёта
   делят выбор ровно по этому признаку, и пробег без единой реплики не трогается вовсе
   (правило 4). ВАЖНО: реплику с `tool_calls` это НЕ дисквалифицирует — «выкинуть тулколы»
   значит убрать вызовы, а не предложение, которое модель написала рядом с вызовом. Первая
   версия выбрасывала такую реплику целиком, и на живом прогоне из шестнадцати сообщений в
   модель уехало два (оба человеческих), а речь модели пропала вся.

   ЗДЕСЬ БЫЛО НЕВЕРНО СКАЗАНО, что вызовы и их ответы в модель не идут (правка 2026-09-22).
   Идут: `_distill.zoned_block` отдаёт рендереру ВЕСЬ кусок, и в блоке данных стоят и
   `TOOL-call`, и `TOOL-result`, обрезанные по категории (`_render`). Деление `prose`/`mute`
   решает, ЕСТЬ ЛИ что сжимать, а не что показывать. Разница не academic: на ней стоит
   правило владения (`_pairs`) — убивая вызов вместе с утонувшим результатом, мы ничего не
   теряем ровно потому, что содержимое обоих в дистилляте учтено.
3. **Дырка в ВЫБОРЕ режет пробег.** `m0001-m0010,m0012` — два несмежных куска, между ними
   живой `m0011`, который говорит своё. Одна общая сводка соврала бы о непрерывности: модель
   читала бы сводку, которой противоречит строка прямо под ней. Каждый пробег получает свою
   сводку, на месте своего первого съеденного.
4. **Выбор человека не обсуждается.** Что выбрано — то и сжимается: команда идёт и делает, а
   не решает за человека, осмысленна ли его компакция. Единственная достройка выбора —
   `_pairs.expand`: у выбранного результата его реплика-носитель считается выбранной, потому
   что своей ручки у вызова нет (владелец, 2026-09-22: «Есть только одно правило… Всех
   других правил нет»).

   ЗДЕСЬ СТОЯЛО ПРАВИЛО, КОТОРОГО НИКТО НЕ ВВОДИЛ (снято 2026-09-22). «Пробег без единой
   реплики не трогаем: без прозы это не компакция, а стирание». У соседних правил стоят даты
   и слова владельца, у этого не стояло ничего — оно было сочинено и трижды выдано за её
   норму. Стоило оно молча выпавшего куска работы на живом прогоне и, что хуже, подменяло
   решение человека мнением механизма. Сжать один большой файл ровно потому, что он большой,
   — законное и частое желание.

ПОЧЕМУ БЕЗ АРГУМЕНТОВ — ПРЕДПРОСМОТР, А НЕ «ВСЁ». Правка необратима, и та же причина, по
которой `POST /context-apply` отказывается применять пустой выбор: команда, отчитавшаяся
успехом без спроса, — единственный режим отказа, который стоит проектировать заранее. Сжать
всю сессию можно, но словом: `/ctx-compact all`.

ПОЗИЦИЯ СВОДКИ = ЕЁ ИНДЕКС В СПИСКЕ. Новые `id` раздаются по порядку вставки в
`replace_messages`, и ничего кроме порядка список не переносит. Поэтому сводка встаёт ровно
туда, где лежал первый съеденный ею — «на место того, что она заменила», а не в конец.

ДВЕ ИСТОРИИ, ОДИН НАБОР ПРАВОК. Как и у `erase`: строки базы (их отдаём в `replace_messages`)
и снимок памяти гейтвея (из него собирается следующий запрос). Адресация у нас по координате
сообщения, а у снимка гейтвея координаты может не быть вовсе — поэтому по нему правим тем же
ключом, каким сверяется предохранитель коммита (`_apply._row_key`), а не адресом.
"""

from __future__ import annotations

import re
import time

import threading

from . import (_insert, _addresses, _alternation, _apply, _compact_state, _cut, _llm, _pairs, _progress, _prompts, _settings, _signal, _stubs,
               _tape)


# КОГДА замок взят. Нужен не для красоты отчёта: прогон идёт минутами, и человек, глядящий
# на «ИДЁТ», должен отличать работу от зависания. Снимается вместе с замком.
_busy_since = 0.0

# Состояние боевого прогона для `/ctx-compact status`. Живёт в памяти АГЕНТСКОЙ половины —
# панель его отсюда не увидит, ей нужен файл и маршрут (`Plan16-Task02`, индикатор).
_state_now: dict = _progress.STATE



class Busy(RuntimeError):
    """Другой прогон уже идёт. Отказ по имени, а не тихое ожидание в очереди."""












def split_wish(raw: str) -> tuple:
    """`/ctx-compact m0003-m0010 выбрось отладку` → `("m0003-m0010", "выбрось отладку")`.

    Слева берутся слова, похожие на заказ: адрес с `m` или число, диапазон, доля, `all`, и их
    `,`-списки; с первого непохожего слова и до конца — пожелание (`Plan34`). Голое шестнадцатеричное
    слово без `m` (`add`, `face`) заказом НЕ считается — иначе английское пожелание съелось бы
    адресами."""
    words = str(raw or "").split()
    for k, word in enumerate(words):
        parts = [p for p in word.replace(";", ",").split(",") if p]
        if not parts or not all(_SPEC_WORD.match(p) or parse_share(p) is not None
                                or p.lower() in ("all", "всё", "все") for p in parts):
            return " ".join(words[:k]), " ".join(words[k:])
    return " ".join(words), ""


_SPEC_WORD = re.compile(r"^(m[0-9a-f]+|[0-9]+)(-(m[0-9a-f]+|[0-9]+))?$", re.I)


def parse_share(raw: str):
    """`0.5` или `50%` — ДОЛЯ сессии. Не доля — `None`, и аргумент читается адресами.

    Проценты принимаем наравне с дробью не из любви к формам: владелец, 2026-09-21 —
    «дробями писать неудобно». Одна строка кода против одной строки раздражения каждый раз.
    """
    text = str(raw or "").strip().replace(",", ".")
    percent = text.endswith("%")
    if percent:
        text = text[:-1].strip()
    try:
        value = float(text)
    except ValueError:
        return None
    if percent:
        value /= 100.0
    # Целое без процента — это адрес `7`, а не «семь долей». Дробь без нуля (`.5`) — доля.
    if not percent and value == int(value):
        return None
    return value
























def _wrap(summary_text: str, kept: bool = False) -> str:
    """Сводка в НАШЕЙ рамке (дистиллят, первое лицо — `Vision08`)."""
    return _prompts.wrap_distillate(summary_text, kept=kept)






def distill_summary(session_id, rows, run_info, by_msg, lines=None, wish: str = "", menu=None) -> dict:
    """Сводка НАШИМ ядром (`Vision08`). Форма ответа — словарь с `ok`/`text`/`error`.

    ДВА МНОЖЕСТВА, и путать их нельзя. В модель уезжает сессия С НАЧАЛА до конца пробега
    плюс запас за краем — ядру нужна целая картина, иначе теряется «зачем». Замещается при
    этом по-прежнему ТОЛЬКО сам пробег: то, что видели, и то, что съели, — разные вещи.

    В модель уходят ВСЕ строки, включая вызовы инструментов ярлыком и обрезком, а не только
    реплики. Решение владельца: файлы и команды — часто единственное место, где лежит суть
    сделанного.
    """
    from . import _distill  # лениво: `_distill` импортирует нас, на модульном уровне это цикл

    addr_by_row = {id(row): addr for addr, row in by_msg.items()}
    positions = _distill.positions_for(rows, max(run_info["positions"]))
    addr_of = {i: addr_by_row.get(id(rows[i]), "") for i in positions}
    eaten = run_info.get("eaten") or run_info["positions"]
    out = _distill.interrogate(session_id, rows, positions, addr_of, lines,
                               kept=run_info.get("kept") or [],
                               work=list(range(min(eaten), max(eaten) + 1)), wish=wish, menu=menu)
    return {"ok": bool(out.get("ok")), "text": out.get("text", ""),
            "error": "" if out.get("ok") else (out.get("error") or "холодный опрос не дал документа"),
            "provider": "distill", "model": f"{out.get('asked', 0)} вопросов",
            "tokens": 0, "prompt_chars": 0,
            "input_chars": out.get("data_chars", 0), "path": out.get("path", ""),
            "sets": out.get("sets") or [], "wish": out.get("wish", "")}


def run(session_id: str, engine, raw_args: str = "", owned: bool = False, deliver: dict | None = None,
        wish: str = "", menu=None, draft: bool = False) -> dict:
    """Сжать названные сообщения сессии. ОДНА реализация; сегодня зовёт слеш-команда.

    Ключ `ok` означает ровно одно: контекст ИЗМЕНЁН. Все прочие исходы (нечего сжимать,
    модель не ответила, коммит отказал) — `ok: False` и объяснение в `lines`. Так же
    устроен `_command.run`, и расходиться с ним в этом было бы хуже всего: два пути правки
    контекста, два разных смысла у слова «получилось».

    `deliver={"history": <лента хода>}` — РЕЖИМ АВТОМАТА (путь B, `Recon31` F11–F12): в базу НЕ
    пишем, новая лента кладётся в `facts["new_history"]`, её отдаёт Hermes `compress()`, и пишет
    её он сам, под своим замком хода. Записи о поглощении и карту — `_insert.finalize_delivered` после их
    записи. `facts["delivered"]` — лента собрана; `ok` по-прежнему значит «контекст изменён нами».

    `owned=True` — «замок уже мой, взят вызывающим». Так зовёт фоновый поток: решать,
    запускаться ли, и брать замок обязан ОДИН И ТОТ ЖЕ участок кода, иначе между «посмотрели,
    свободно» и «взяли» помещается второй вызов (`__HQ/deferred/03`, чинится этим). Прямой
    синхронный вызов по-прежнему берёт замок сам — default выбран так, чтобы забытый аргумент
    давал ЛИШНЮЮ защиту, а не её отсутствие.

    `wish` — пожелание пользователя к компакции (`Plan34`), проза; доходит до опроса
    (`_distill.interrogate`). Пустое — прогон прежний. `menu` — наборы опроса, выбранные человеком на
    этот прогон (Task 8); `None` — по настройке.

    `draft=True` — ПРЕВЬЮ (`Plan35`): всё до записи, но в ленту не пишем — дистиллят без рамки в
    `facts["draft"]` (`addrs`, `text`, `wrap`, `kept`, `sets`, `wish`), его открывает окно LLMOps.
    Имя не `preview`: так здесь зовётся предпросмотр реза (вызов без аргументов).
    """
    spec = str(raw_args or "").strip()
    facts = {"ok": False, "session": session_id, "spec": spec, "runs": 0,
             "absorbed": [], "summaries": [], "lines": []}
    lines = facts["lines"]

    preview = spec == ""
    # Предпросмотр замка не требует: он ничего не меняет и в модель не ходит.
    mine = not preview and not owned
    if mine and not _insert._busy.acquire(blocking=False):
        lines.append("ИТОГ: контекст не тронут — другой прогон компакции уже идёт. "
                     "Это не очередь: два прогона подряд съели бы сводку первого")
        return facts
    global _busy_since
    if mine:
        _busy_since = time.time()
    try:
        # Маршрут к модели сессии — на этот поток, на весь прогон (`_llm`, шапка).
        with _llm.use_route(engine.live_agent()):
            return _run_guarded(session_id, engine, raw_args, facts, lines, preview, deliver, wish, menu,
                                draft)
    finally:
        # Снятие в `finally` обязательно: прогон может упасть где угодно, а замок, повисший
        # после смерти прогона, заклинил бы компакцию до перезапуска клиента. Чужой замок не
        # снимаем: его снимет тот, кто взял (`_worker`), и тоже в `finally`.
        if mine:
            _busy_since = 0.0
            _insert._busy.release()


def _run_guarded(session_id, engine, raw_args, facts, lines, preview, deliver=None, wish="",
                 menu=None, draft=False) -> dict:
    """Тело `run()` под уже взятым замком — разнесено только ради `finally`."""
    spec = str(raw_args or "").strip()
    # ЧТО ИМЕННО ДОЕХАЛО, дословно и первой строкой. Панель и слеш-команда сходятся здесь, и
    # до 2026-09-22 отчёт не показывал заказ вовсе: когда живой прогон съел не то, что
    # человек наклацал, спорить было НЕ О ЧЕМ — ни в отчёте, ни в карте не было видно, какой
    # список адресов дошёл до движка. Эхо стоит строки и снимает весь этот класс разговоров.
    if spec:
        lines.append(f"запрошено: {spec}")
    take_all = spec.lower() in ("all", "всё", "все")
    # Доля разрешается ПОЗЖЕ, когда есть строки: правило реза смотрит на роли и на объём, а
    # не на адреса. До тех пор она ведёт себя как `all` — «адресов не задано» не про неё.
    share = None if (preview or take_all) else parse_share(spec)
    # Разбор адресов ДВАЖДЫ: здесь — только чтобы отказать на пустом выборе до похода в базу.
    # Настоящий выбор делает `_insert.positions_for_spec`, когда строки уже на руках: диапазон режет
    # ленту по ПОЛОЖЕНИЮ концов, а для этого нужны сами строки.
    wanted, bad = (set(), []) if (preview or take_all or share is not None) \
        else _insert.parse_spec(spec)
    if bad:
        lines.append(f"не разобрано и пропущено: {', '.join(bad)} — ждём `m0007`, `7` "
                     "или диапазон `m0001-m0010`")
    if not preview and not take_all and share is None and not wanted:
        lines.append("ИТОГ: контекст не тронут — ни одного адреса не задано")
        return facts

    ok, why = _llm.available()
    if not ok and not preview:
        lines.append(f"ИТОГ: контекст не тронут — {why}")
        return facts
    if ok:
        lines.append(f"модель опроса: {why}")

    agent = engine.live_agent()
    session, session_why = _apply.find_gateway_session(agent)
    # В режиме автомата сессия гейтвея не нужна: лента хода приходит аргументом, пишет Hermes.
    if session is None and deliver is None:
        lines.append(f"сжимать нечем — {session_why}")
        lines.append("ИТОГ: контекст не тронут")
        return facts

    rows, rows_why = _apply.db_history(agent)
    if rows_why:
        lines.append(rows_why)
        lines.append("ИТОГ: контекст не тронут")
        return facts

    # Адресация ДО правки — по той же причине, что и у `erase`: после замены строк не
    # будет, а с ними исчезнет координата, по которой адрес выдаётся.
    by_msg = _insert._index(session_id, rows)
    if not by_msg:
        lines.append("адресов сообщений нет вовсе — индексатор не смог разметить сессию; "
                     "без адресов выбирать нечем")
        lines.append("ИТОГ: контекст не тронут")
        return facts
    if preview or take_all:
        wanted = {_insert._addr_number(a) for a in by_msg} - {-1}
    elif share is not None:
        # ДОЛЯ. Правило целиком — `_cut`; здесь только перевод точки реза в адреса.
        report = _cut.select(rows, _settings.compaction(), share)
        lines.extend(_cut.explain(report))
        if report["index"] < 0:
            lines.append("ИТОГ: контекст не тронут — по этой доле резать нечего")
            return facts
        addr_by_row = {id(row): addr for addr, row in by_msg.items()}
        wanted = {_insert._addr_number(addr_by_row.get(id(rows[i]), ""))
                  for i in range(report["start"], report["index"] + 1)} - {-1}
        if not wanted:
            lines.append("ИТОГ: контекст не тронут — в отрезанном куске нет ни одного адреса")
            return facts
    else:
        # АДРЕСА. Диапазон — кусок ленты между концами, а не числа между ними: после
        # компакции номера идут не по возрастанию (владелец, 2026-09-22).
        places, _bad, gone = _insert.positions_for_spec(rows, by_msg, spec)
        addr_by_row = {id(row): addr for addr, row in by_msg.items()}
        wanted = {_insert._addr_number(addr_by_row.get(id(rows[i]), "")) for i in places} - {-1}
        if gone:
            lines.append("концы или адреса не найдены: "
                         + ", ".join(_insert._addr(n) for n in gone[:8])
                         + (" …" if len(gone) > 8 else ""))
        if not wanted:
            lines.append("ИТОГ: контекст не тронут — ни один названный адрес не найден")
            return facts

    # ВЫБОР ДОСТРАИВАЕТСЯ ДО ПАР (`_pairs.expand`, 2026-09-22). Выбранный результат тянет за
    # собой свою реплику-носителя: у вызова нет ни своей судьбы, ни своей ячейки в рельсе, и
    # показать на него пальцем человек может только через результат. Без этого выбор рвался
    # на односроч­ные пробеги, каждый из которых мы честно не трогали (правило 4) — и
    # заказанные адреса молча выпадали из работы.
    wanted = _insert._expand_to_pairs(rows, by_msg, wanted, lines)

    runs, skipped, missing, patch, replaced, hollow = _insert._plan(rows, by_msg, wanted, lines)
    lines.append(f"база: {len(rows)} активных строк, адресовано {len(by_msg)} | "
                 f"выбрано номеров {len(wanted)} | пробегов {len(runs)}")
    if missing:
        head = ", ".join(_insert._addr(n) for n in missing[:8])
        lines.append(f"адреса не найдены ({len(missing)}): {head}"
                     + (" …" if len(missing) > 8 else "")
                     + " — номера живут per-session, чужой сюда не подойдёт")
    if skipped:
        # Сюда больше ничего не попадает: `_insert.build_runs` ничего не пропускает (см. его
        # докстринг). Ветка оставлена сторожем — если список когда-нибудь снова начнёт
        # наполняться, человек об этом узнает, а не догадается по карте.
        head = ", ".join(addr for addr, _ in skipped[:8])
        lines.append(f"НЕ тронуто ({len(skipped)}): {head}"
                     + (" …" if len(skipped) > 8 else ""))
    if not runs:
        lines.append("ИТОГ: контекст не тронут — сжимать нечего")
        return facts
    # ПРОВЕРКИ «А ЕСТЬ ЛИ ТУТ ПРОЗА» БОЛЬШЕ НЕТ, и она тут стояла ровно один день
    # (2026-09-22, утром добавил, вечером убрал). Отказывать за то, что человек выбрал одни
    # результаты инструментов, — это подменять его решение своим мнением о том, что считать
    # осмысленной компакцией. Модель видит и вызовы, и результаты (`_render`), сводке есть из
    # чего взяться, а «сжать один большой файл потому, что он большой» — законное желание.
    total = sum(len(r["eaten"]) for r in runs)
    if total > _insert.MAX_MESSAGES:
        lines.append(f"ИТОГ: контекст не тронут — {total} сообщений за раз это больше "
                     f"предела {_insert.MAX_MESSAGES}. Раздели диапазон")
        return facts

    for run_info in runs:
        addresses = run_info["addresses"]
        lines.append(f"пробег {addresses[0]}…{addresses[-1]}: съедается {len(addresses)} "
                     f"строк, из них реплик {len(run_info['prose'])}, вызовов и ответов "
                     f"{len(run_info['mute'])}; съеденное уезжает в архив (active=0)")

    if preview:
        lines.append("ИТОГ: это ПРЕДПРОСМОТР — модель не звали, база не тронута. "
                     "Сжать: `/ctx-compact 0.5` (доля сессии, можно `50%`), "
                     "`/ctx-compact m0001-m0010` или `/ctx-compact all`")
        facts["runs"] = len(runs)
        return facts

    if draft and len(runs) > 1:
        # Окно LLMOps держит ОДИН кусок. Сторож: `_insert._plan` сегодня сводит пробеги в один
        # пролёт, сюда не попасть; если начнёт — отказ до модели, а не минуты опроса впустую.
        lines.append(f"ИТОГ: контекст не тронут — превью берёт один сплошной кусок, а пробегов "
                     f"{len(runs)}. Отметь кусок без дырок")
        return facts

    # ── Модель. Один вызов на пробег, до единой правки базы: упасть на третьем из пяти
    # и оставить контекст наполовину сжатым хуже, чем не начать вовсе.
    summaries = []
    for run_info in runs:
        _progress.progress(session_id, stage="prepare", step=0, total=0, label="", topic="",
                 span=f"{run_info['addresses'][0]}…{run_info['addresses'][-1]}")
        answer = distill_summary(session_id, rows, run_info, by_msg, lines, wish, menu)
        if not answer.get("ok"):
            lines.append(f"ИТОГ: контекст не тронут — модель не дала сводку для "
                         f"{run_info['addresses'][0]}…{run_info['addresses'][-1]}: "
                         f"{answer.get('error')}")
            return facts
        summaries.append(answer)
        lines.append(f"  сводка для {run_info['addresses'][0]}…"
                     f"{run_info['addresses'][-1]}: {len(answer['text'])} символов "
                     f"из {answer['input_chars']} "
                     f"({answer.get('provider')}/{answer.get('model')}, "
                     f"{answer.get('tokens')} токенов)")

    if draft:
        # ПРЕВЬЮ: запись не зовём. Текст — ровно тот, что `_insert._head_content` обернул бы
        # рамкой `compaction`; `kept` — тот же признак пузырей, что у записи.
        answer, run_info = summaries[0], runs[0]
        facts["draft"] = {"addrs": list(run_info["addresses"]), "text": answer["text"],
                          "wrap": "compaction", "kept": bool(run_info.get("kept")),
                          "sets": list(answer.get("sets") or []), "wish": answer.get("wish", "")}
        lines.append("превью: в ленту не записано — дистиллят в окне LLMOps")
        return facts

    return _insert._assemble_until_written(session_id, session, agent, rows, by_msg, runs, patch, hollow,
                                   replaced, summaries, deliver, spec, wanted, facts, lines)





































def _pick(raw_args: str, mark: str) -> tuple:
    """`(session_id, движок, аргументы, строки отказа)`. Общий вход обеих наших команд.

    Тот же выбор сессии, что у `/ctx-drop` (`_apply.pick_engine`), и это не единообразие
    ради единообразия: он смотрит, КАКОЙ ЧАТ ОТКРЫТ В ПАНЕЛИ, понимает явное `--session`, а
    при неоднозначности ОТКАЗЫВАЕТ вместо угадывания. Своя первая версия брала
    `_bridge.live_engines()[0]` — а у него в докстринге прямо написано «порядок не
    определён», то есть команда могла уйти в чужой чат и не сказать об этом.
    """
    args = str(raw_args or "").strip()
    session_hint = ""
    if "--session" in args:
        head, _, tail = args.partition("--session")
        session_hint = tail.strip().split()[0] if tail.strip() else ""
        args = head.strip()
    session_id, engine, why = _apply.pick_engine(session_hint)
    if engine is None:
        return "", None, args, [mark, why, "ИТОГ: ничего не сделано"]
    return session_id, engine, args, [mark, why]


def status_lines() -> list:
    """Что с боевым прогоном сейчас. Читается `/ctx-compact status`."""
    state = dict(_state_now)
    if not state:
        return ["боевых прогонов ещё не было"]
    head = ("ИДЁТ" if state.get("running") else "закончен") \
        + f": сессия {state.get('session', '?')}"
    if state.get("started"):
        head += f", начат {state['started']}"
    if state.get("running") and _busy_since:
        head += f", идёт {int(time.time() - _busy_since)} с"
    out = [head]
    if state.get("error"):
        out.append(f"ОШИБКА: {state['error']}")
    for line in state.get("lines") or []:
        out.append("  " + line)
    return out


def _worker(session_id, engine, args, wish: str = "", menu=None, draft: bool = False) -> None:
    """Тело боевого прогона в отдельном потоке. Исключений наружу не выпускает: поток,
    умерший молча, оставил бы человека с вечным «идёт».

    Пишет ход И в `_state_now` (слеш-команда `/ctx-compact status`, тот же процесс), И в
    файл через `_compact_state` (панель, другая половина — см. его докстринг про то, почему
    модульный глобал ей не виден). Одно и то же событие, два адресата.

    ЗАМОК УЖЕ ВЗЯТ `start_background`, поэтому `owned=True`, а снимается он ЗДЕСЬ: взявший
    вернуть его сам не может — он ответил человеку и ушёл, пока поток только начинается.
    """
    global _busy_since
    facts, error = {}, ""
    try:
        # `draft` — только когда есть: без превью вызов прежний, байт в байт (`Plan35`).
        extra = {"draft": True} if draft else {}
        facts = run(session_id, engine, args, owned=True, wish=wish, menu=menu, **extra)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    finally:
        _progress.finish(session_id, facts, error)
        _busy_since = 0.0
        _insert._busy.release()


def start_background(session_id: str, engine, args: str, wish: str = "", menu=None,
                     draft: bool = False) -> dict:
    """Запустить компакцию в фоне. Общее тело для слэш-команды и маршрута панели
    (`POST /context-compact-start`) — ОДНА реализация замка+потока, а не две разные копии
    одного и того же риска (двойной запуск, поток без `finally`).

    Отвечает СРАЗУ: `{"ok": True, "session": ...}` до конца прогона, сам прогон и его итог —
    в `_state_now`/файле состояния, не в этом возврате. `{"ok": False, "error": ...}`, если
    запускать нечего (нет адресов) или замок уже занят.

    ЗАМОК БЕРЁТСЯ ЗДЕСЬ, а не внутри потока (`__HQ/deferred/03`, починено 2026-09-22).
    Прежде здесь СПРАШИВАЛИ `_insert._busy.locked()`, а брали замок позже и в другом месте — внутри
    `run()`, уже в потоке. Между «посмотрели» и «взяли» помещался второй вызов: оба видели
    свободно, оба заводили поток. Данные при этом не страдали (второй прогон честно
    отказывался), а вот СТАТУС врал дважды — очистка `_state_now` затирала прогресс первого,
    ЖИВОГО прогона состоянием второго, мертворождённого, и вызывающий получал `ok: True` на
    то, что через миллисекунды откажется. Второе хуже: сабагент в закрытой сессии не увидел
    бы ни отказа, ни отчёта — он досидел бы до таймаута.

    Теперь `{"ok": True}` значит «замок мой, поток пошёл», а не «на момент вопроса было
    свободно». Снимает замок `_worker` в своём `finally`; если поток не завёлся вовсе —
    снимаем здесь, иначе компакция заклинила бы до перезапуска клиента.
    """
    spec = str(args or "").strip()
    if not spec:
        return {"ok": False, "error": "nothing to compact: no message is ticked"}
    if not _insert._busy.acquire(blocking=False):
        return {"ok": False, "error": "компакция уже идёт — второй запуск не ставится в очередь"}
    global _busy_since
    _busy_since = time.time()
    # Состояние чистим ТОЛЬКО после успешного захвата: до него прогон не наш, и затирать им
    # чужой прогресс значит отнимать у человека вид на работу, которая идёт.
    _progress.begin(session_id, "button")
    try:
        threading.Thread(target=_worker, args=(session_id, engine, spec, wish, menu, draft),
                         daemon=True, name="ctx-compact").start()
    except Exception as exc:  # noqa: BLE001 — поток не завёлся: замок наш, вернуть некому
        _busy_since = 0.0
        _insert._busy.release()
        error = f"поток компакции не запущен: {type(exc).__name__}: {exc}"
        _progress.finish(session_id, None, error)
        return {"ok": False, "error": error}
    return {"ok": True, "session": session_id}


def handle(raw_args: str = "") -> str:
    """Слеш-команда `/ctx-compact`. Предпросмотр отвечает сразу, правка уходит в ФОН.

    ПОЧЕМУ В ФОН. Наш мозг — не один вызов, а весь холодный опрос, и это минуты. Hermes
    рвёт слеш-команду на тридцати секундах и отправляет её ЗАНОВО (поймано живьём
    2026-09-16), а второй прогон компакции перечитал бы уже сжатую сессию и съел собственный
    дистиллят первого. Поэтому отвечаем немедленно, работаем в потоке, а от повторного входа
    защищает замок внутри `run`.

    Предпросмотр остаётся СИНХРОННЫМ: он в модель не ходит и считается мгновенно.
    """
    mark = "── ctx-compact ──"
    if str(raw_args or "").strip().lower() in ("status", "статус", "?"):
        return "\n".join([mark] + status_lines())

    session_id, engine, args, lines = _pick(raw_args, mark)
    if engine is None:
        return "\n".join(lines)
    # Проза после адресов/доли — пожелание к компакции (`Plan34` Task 5).
    args, wish = split_wish(args)
    if wish and not args:
        return "\n".join(lines + [f"пожелание: «{wish}»", "ИТОГ: ничего не сделано — перед "
                                  "пожеланием нужен заказ: адреса, доля (`50%`) или `all`"])
    if not args:
        return "\n".join(lines + list(run(session_id, engine, args).get("lines") or []))

    # ЗАПУСК ИДЁТ ПЕРВЫМ, предпросмотр реза — после него (перестановка 2026-09-22).
    #
    # Отдельной проверки `_insert._busy.locked()` здесь БОЛЬШЕ НЕТ: она была вторым местом, где
    # решают «свободно ли», при том что берёт замок третье, — той самой гонкой, из-за которой
    # врал статус (`__HQ/deferred/03`). Единственные ворота — `start_background`, он же и
    # захватывает.
    #
    # Но убрать её мало: рез читает БАЗУ, и стоял он ВЫШЕ ворот. Пока ранний выход по
    # `locked()` существовал, на занятом замке до этого чтения просто не доходило; без него
    # отказ стал стоить лишнего похода в базу — а на фальшивом движке в тестах чтение и вовсе
    # подвисло. Поэтому сначала решаем и захватываем, и только потом объясняем рез: в ответе
    # человеку он всё равно окажется выше, порядок строк мы задаём сами.
    started = start_background(session_id, engine, args, wish)
    if not started.get("ok"):
        return "\n".join(lines + [started.get("error") or "не запущено"] + status_lines())

    # Рез показываем СРАЗУ, как и в сухом прогоне: иначе человек видит только «запущено» и
    # не знает даже, понят ли аргумент. Считать дёшево и ничего не меняет.
    share = parse_share(args)
    if share is not None:
        try:
            rows, why = _apply.db_history(engine.live_agent())
            lines.extend(_cut.explain(_cut.select(rows, _settings.compaction(), share)) if rows
                         else [why or "строк сессии не видно"])
        except Exception as exc:  # noqa: BLE001
            lines.append(f"рез сейчас не показать ({type(exc).__name__}) — он будет в отчёте")
    return "\n".join(lines + [
        "ЗАПУЩЕНО В ФОНЕ: холодный опрос идёт минутами, и только после него правится база.",
        "как идёт — `/ctx-compact status`; там же будет итог и что именно поглощено"])
