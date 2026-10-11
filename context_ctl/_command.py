"""`/ctx-drop <номера через запятую>` — ручное выбрасывание, тестовая форма (Plan13 Task 4).

ЗАЧЕМ КОМАНДА, ЕСЛИ ПЛАНИРУЕТСЯ КНОПКА. Решение владельца 2026-09-08: тестовая версия —
именно команда с номерами операций, потому что она проверяема БЕЗ клиентской половины.
Кнопка, шторки с галочками и цифры у галочек — рабочая версия, следующим шагом; команда
останется как опция.

ИМЕНА — `Operation ID` (`0015`, `op0015`) и m-адреса (`m0042`). Номер — то, что видят
человек и МОДЕЛЬ в ответах тулов (m-адресов модель не видит). Транспорт ОДИН (Plan23,
владелец 2026-10-06): номер переводится в m-адрес (номер → `tool_call_id` → строка → адрес;
номер → `tool_call_id` двумя источниками — сначала САМА ИСТОРИЯ, затем леджер) и дальше
идёт той же дверью, что и панель, — `run_addresses`.

БЕЗ АРГУМЕНТОВ — НИЧЕГО НЕ ДЕЛАЕТ, только подсказывает формат. Команда меняет живой
контекст, и пустой ввод не должен означать «сделай что-нибудь».

ОТВЕТ ОБЯЗАТЕЛЬНО НЕПУСТ И ЗАКАНЧИВАЕТСЯ СТРОКОЙ «ИТОГ:». Требование владельца после
живого прогона 2026-09-08, где случилось худшее из возможного: команда сработала МОЛЧА, и
о том, что контекст изменён, человек узнал только по уменьшившемуся промпту. Тронут
контекст или нет — должно читаться с одной строки, без догадок.
"""

from __future__ import annotations

from . import _apply, _signal, _stubs
from . import _insert

_MARK = "── ctx-drop ──"
NL = "\n"


def _split_ops(raw: str) -> list:
    """Имена из строки. Принимаем и запятую, и пробел: человек напишет как придётся."""
    out, seen = [], set()
    for chunk in (raw or "").replace(",", " ").split():
        token = chunk.strip().lstrip("#")
        if token and token.lower() not in seen:
            seen.add(token.lower())
            out.append(token)
    return out


def _split_names(names) -> tuple:
    """`(m-адреса, номера операций)`. `m` не шестнадцатеричная цифра — адрес от номера
    отличается без догадок; `op` перед номером снимается (`op0015` = `0015`)."""
    addrs, ops = [], []
    for name in names or []:
        low = str(name).strip().lower()
        if low.startswith("m") and low[1:].isalnum() and low[1:]:
            addrs.append(low)
        else:
            ops.append(low[2:] if low.startswith("op") and len(low) > 2 else low)
    return addrs, ops


def handle(raw_args: str = "") -> str:
    """Обработчик слеш-команды. Возвращает текст человеку; исключений не выпускает.

    Порядок сознательный: сначала находим сессию, потом переводим номера, потом считаем
    новую историю и линтуем, и только потом коммитим. Если что-то не сошлось на любом
    шаге — отвечаем и НЕ трогаем контекст: цена ошибочного применения (порванная живая
    сессия) несравнима с ценой лишнего вопроса.
    """
    try:
        return _handle(raw_args or "")
    except Exception as exc:  # noqa: BLE001 - команда не имеет права уронить чат
        return f"{_MARK}{NL}ИТОГ: не сработало — {type(exc).__name__}: {exc}"


def _note(engine, verdict: str, **facts) -> None:
    """Записать исход в статус движка — доказательство, не зависящее от эха в чате."""
    try:
        engine.note_drop({"verdict": verdict, **facts})
    except Exception:  # noqa: BLE001 - диагностика не имеет права ломать команду
        pass


def _handle(raw_args: str) -> str:
    args = raw_args.strip()
    session_hint = ""
    # `--session <id>` — способ переспорить угадывание «кто говорил последним».
    if "--session" in args:
        head, _, tail = args.partition("--session")
        session_hint = tail.strip().split()[0] if tail.strip() else ""
        args = head.strip()

    session_id, engine, why = _apply.pick_engine(session_hint)
    if engine is None:
        return NL.join([_MARK, why, "ИТОГ: контекст не тронут"])

    names = _split_ops(args)
    if not names:
        return NL.join([_MARK, why, _FORMAT, "ИТОГ: ничего не задано, контекст не тронут"])

    addrs, ops = _split_names(names)
    return NL.join([_MARK, why]
                   + run_addresses(session_id, engine, addrs, OP_CONTENT, ops=ops)["lines"])


_FORMAT = ("Формат: `/ctx-drop 0156,0157` — номера операций (`Operation ID`) из ответов наших "
           "тулов; можно `op0156` и m-адреса панели (`m0042`).")


def _facts(facts: dict, lines: list, summary: str = "") -> dict:
    """Закрыть отчёт. `lines`/`text` — человеку в ЧАТ, `summary` — панели.

    ДВА ГОЛОСА, И ЭТО ИСПРАВЛЕНИЕ 2026-09-09. Панель раньше выскабливала себе подпись из
    строк команды — искала ту, что начинается с «ИТОГ:». Так её язык оказался привязан к языку
    чата, и на экран уехало русское «ИТОГ: применено — применено · освободилось ~567 токенов»:
    и дубль, и русский там, где всё должно быть по-английски.

    Теперь у отчёта есть отдельная короткая английская сводка, собранная из ФАКТОВ. Проза
    команды остаётся русской и своей: это её собственный голос в чате, а префикс «ИТОГ:» —
    её конвенция, по которой человек читает исход с одной строки.
    """
    facts["lines"] = lines
    facts["text"] = NL.join(lines)
    facts["summary"] = summary or ("applied" if facts.get("ok") else "not applied")
    return facts


def _summary_of(applied, tokens_dropped: int) -> str:
    """Короткая английская фраза для панели. Из ФАКТОВ, а не из прозы команды."""
    freed = f" · freed ~{tokens_dropped} tk" if tokens_dropped > 0 else ""
    return f"dropped {len(applied)} ops{freed}"


def _apply_items(session_id, engine, agent, session, history, rows, items,
                 facts, lines, missing, no_address, tool_msgs, ops) -> dict:
    """`drop content` по готовому списку целей `{op, tool_call_id}`: заглушки, линт, коммит,
    сигнал, отчёт. Зовёт только `run_addresses` (Plan23: путь номеров снят, номера команды
    переводятся в m-адреса до этого места).
    """
    mechanism = _stubs.apply_stubs
    index = _stubs.ledger_index(session_id)
    new_rows, report = mechanism(rows, items, index)
    new_rows = _apply.drop_stale_api_content(rows, new_rows)
    # Тот же набор адресов по памяти гейтвея. Отчёт берём от базы: она источник, и
    # расхождение между двумя списками мы называем ниже отдельной строкой, а не смешиваем.
    new_history, gw_report = mechanism(history, items, index)

    problems = report.get("problems") or []
    if problems:
        lines.append("ИТОГ: контекст НЕ тронут — линт нашёл разрыв пары вызов/результат "
                     f"({len(problems)}). Ни один наш механизм такого не создаёт (заглушка "
                     "оставляет оба сообщения на месте, а удаление снимает обе половины "
                     "пары), значит разрыв был до нас, и коммитить поверх чужой поломки "
                     "нельзя")
        _note(engine, "не применено: разрыв пары", problems=problems)
        return _facts(facts, lines,
                      f"refused: {len(problems)} broken call/result pair(s) found")

    # Строки «пропущено как бессмысленное» больше нет: отказ по размеру отменён владельцем
    # (2026-09-09), названа операция — удаляется. `skipped` остаётся в отчёте пустым списком
    # ради формы, но печатать нечего.

    stubbed = report.get("applied") or []
    if not stubbed:
        not_found = report.get("not_found") or []
        if not_found:
            lines.append(
                f"адрес есть, а активной строки в базе НЕТ ({len(not_found)}). Значит "
                "набор, который мы правим, эту операцию не содержит: либо адрес взят из "
                "леджера прошлой сессии, либо строку уже сняли с активных. Смотри цифры "
                "выше — если тул-ответов в базе 0, дело во втором")
        lines.append("ИТОГ: контекст не тронут — выбрасывать нечего")
        _note(engine, "не применено: выбрасывать нечего", not_found=not_found,
              rows=len(rows), history_len=len(history), tool_msgs=tool_msgs)
        # Контекст не тронут, а КАРТОЧКЕ здесь делать нечего: строки под ней нет. Панель
        # снимет её по этому списку, и это не «сделали вид, что применили» — `ok` остаётся
        # False, отчёт говорит правду, убирается только то, что и так ни на чём не основано.
        facts["absent"] = report.get("absent") or []
        return _facts(facts, lines, "nothing to remove: no such row in the context")

    # Токены считаем по памяти гейтвея: именно из неё собирается следующий запрос.
    before = _apply._estimate(agent, history)
    after = _apply._estimate(agent, new_history)

    gw_stubbed = gw_report.get("applied") or []
    if len(gw_stubbed) != len(stubbed):
        lines.append(
            f"расхождение: в базе заглушено {len(stubbed)}, в памяти гейтвея "
            f"{len(gw_stubbed)} — снимок памяти неполон. База будет верна, а промпт "
            "следующего запроса может ещё нести выброшенное; перезапуск сессии сравняет")

    # `removed` — то, что механизм удалил нарочно. Берём из отчёта ПО БАЗЕ: именно её
    # строки сверяет предохранитель, и отчёт памяти гейтвея тут не при чём.
    ok, applied_why = _apply.commit_replacement(session, agent, new_rows,
                                                removed=report.get("removed") or [])
    if not ok:
        lines.append(f"ИТОГ: НЕ применено — {applied_why}")
        _note(engine, f"не применено: {applied_why}", ops=ops)
        return _facts(facts, lines, "refused at commit, context untouched")

    # Надгробий здесь нет: у `drop content` операция в контексте осталась, у неё живой
    # заголовок, и надгробие соврало бы. Стирание пишет их само (`_erase_places`).

    # Панель узнаёт об изменении только отсюда: их события `compacted` у нашего пути нет,
    # а процессы разные. Бампаем ПОСЛЕ успешного коммита — сигнал обещает факт, не намерение.
    _signal.bump(session_id, [row.get("op") for row in stubbed])

    # По ОПЕРАЦИИ, а не по одной строке: у записи всё содержимое лежит в аргументах вызова,
    # и отчёт только по ответу показывал бы «освободилось 0» при удалённом файле.
    lines.append("удалено: " + ", ".join(
        f"{row.get('op') or '?'} (освободилось {row.get('saved', 0)} симв.: "
        f"ответ {row.get('saved_result', 0)}, аргументы {row.get('saved_args', 0)})"
        for row in stubbed))
    # ЕДИНСТВЕННОЕ, чего человек не выбирал, а получил: вместе с последней операцией хода
    # ушла и строка ассистента (у неё не осталось ни вызовов, ни текста). Отдельной строкой,
    # потому что это не деталь реализации, а изменение разговора, которое он не заказывал.
    shells_removed = report.get("shells_removed") or []
    if shells_removed:
        lines.append(f"вместе с ними ушли строки ассистента ({len(shells_removed)}): у них не "
                     "осталось ни вызовов, ни текста — пустой ход провайдеру не отправить")
    shells_emptied = report.get("shells_emptied") or []
    if shells_emptied:
        lines.append(f"строк ассистента с сохранённым текстом: {len(shells_emptied)} — вызовы "
                     "сняты, слова модели остались, их человек не выбирал")
    lines.append(f"токены: было ~{before}, стало ~{after} "
                 f"(освободилось ~{max(0, before - after)}, "
                 f"символов {report.get('chars_saved', 0)})")
    lines.append("следующий запрос — полный холодный пересчёт: префикс изменился, кеш "
                 "провайдера на этой сессии больше не подходит")
    # Причину дописываем только если она ДОБАВЛЯЕТ что-то к слову «применено»: коммит
    # в обычном случае возвращает ровно его, и строка читалась как «применено — применено».
    lines.append("ИТОГ: применено"
                 + ("" if applied_why.strip() == "применено" else f" — {applied_why}"))
    _note(engine, applied_why,
          stubbed=[{"op": r.get("op"), "chars_before": r["chars_before"],
                    "chars_after": r["chars_after"]} for r in stubbed],
          tokens_before=before, tokens_after=after,
          missing=missing, no_address=no_address)
    facts.update({"ok": True, "applied": [row.get("op") for row in stubbed],
                  # Операции, чьей строки в базе нет ВОВСЕ. Панель снимает по ним карточку:
                  # она показывала бы операцию, которой в контексте не существует.
                  "absent": report.get("absent") or [],
                  "tokens_before": before, "tokens_after": after,
                  "tokens_dropped": max(0, before - after),
                  "chars_saved": report.get("chars_saved", 0),
                  "revision": _signal.read_signal(session_id).get("revision", 0)})
    return _facts(facts, lines, _summary_of(facts["applied"], facts["tokens_dropped"]))


# ── Операции над m-АДРЕСАМИ (Plan16-Task08) — ЕДИНСТВЕННЫЙ транспорт (Plan23) ─────────────
#
# Одна дверь на все операции, чей предмет — сообщения (владелец, 2026-09-29). С 2026-10-06
# другой нет: путь номеров операций (`/context-apply`, выбор панели, механизмы стирания по
# номерам) снят, номера команды `/ctx-drop` переводятся в адреса здесь же (`ops=`).
#
#   content  тело результатов тулов — НАШИХ И ЧУЖИХ. Цели собираются по адресу: строка
#            результата → её `tool_call_id`, дальше `apply_stubs` и коммит.
#   erase    любое сообщение, по правилу владения (`_pairs.settle`, как у компакции):
#            результат уносит свой вызов, реплика без прозы и вызовов уходит, реплика с
#            прозой теряет прозу, но живёт, пока в ней есть вызов. Это компакция без
#            дистиллята. Надгробия пишутся нашим стёртым операциям (решение владельца).
OP_CONTENT, OP_ERASE = "content", "erase"
OPERATIONS = (OP_CONTENT, OP_ERASE)


def _addresses_of_ops(session_id, ops, rows, by_msg) -> tuple:
    """Номера операций → m-адреса их строк-результатов. `(адреса, строки отчёта, нет нигде,
    нет адреса)`. Номер → `tool_call_id` — `_stubs.ops_to_call_ids` (история, потом леджер);
    `tool_call_id` → адрес — по той же ленте, что и всё остальное в двери."""
    addr_of_call = {}
    for addr, row in by_msg.items():
        if isinstance(row, dict) and row.get("role") == "tool" and row.get("tool_call_id"):
            addr_of_call[str(row["tool_call_id"])] = addr
    items, missing, no_address = _stubs.ops_to_call_ids(session_id, ops, rows)
    addrs, found = [], []
    for item in items:
        addr = addr_of_call.get(str(item.get("tool_call_id") or ""))
        if addr:
            addrs.append(addr)
            found.append(f"{item.get('op')} → {addr} (из {item.get('src')})")
        else:
            # Вызов известен, а строки в активной ленте нет — той же природы, что «нет адреса».
            no_address.append(str(item.get("op") or ""))
    lines = []
    if found:
        lines.append("номера найдены: " + ", ".join(found))
    if missing:
        lines.append(f"нет ни в истории, ни в леджере: {', '.join(missing)} — номера живут "
                     "per-session, номер из другого чата тут не найдётся")
    if no_address:
        # «Нечем НАЙТИ», а не «нечего удалять»: номера в тексте ответа нет (его переписала
        # их компакция) и строки с его `tool_call_id` в активной ленте тоже нет.
        lines.append(f"операция была, но НАЙТИ её строку нечем: {', '.join(no_address)} — "
                     "номера в тексте ответа нет, а строки с её tool_call_id в активной "
                     "ленте нет")
    return addrs, lines, missing, no_address


def run_addresses(session_id: str, engine, addrs: list, operation: str, ops=()) -> dict:
    """Применить операцию к названным m-адресам сессии. `ops` — номера операций (команда
    `/ctx-drop`): переводятся в адреса и идут тем же путём."""
    addrs = [str(a).strip().lower() for a in (addrs or []) if str(a).strip()]
    ops = [str(o).strip() for o in (ops or []) if str(o).strip()]
    facts = {"ok": False, "session": session_id, "msgs": list(addrs), "operation": operation,
             "mode": operation, "applied": [], "tokens_before": 0, "tokens_after": 0,
             "chars_saved": 0}
    if ops:
        facts["ops"] = list(ops)
    if operation not in OPERATIONS:
        return _facts(facts, [f"операция {operation!r} не реализована — умеем {OPERATIONS}",
                              "ИТОГ: контекст не тронут"],
                      f"operation {operation!r} is not implemented")

    agent = engine.live_agent()
    session, session_why = _apply.find_gateway_session(agent)
    if session is None:
        return _facts(facts, [f"применить нечем — {session_why}", "ИТОГ: контекст не тронут"],
                      "no live session to apply to")
    history = _apply.history_of(session)
    rows, rows_why = _apply.db_history(agent)
    if rows_why:
        return _facts(facts, [rows_why, "ИТОГ: контекст не тронут"],
                      "the session database is unreachable")
    facts["indexed"] = _stubs.index_messages(session_id, rows)

    # Адрес → позиция строки. Тот же индекс, что у компакции: адрес — личность сообщения.
    from . import _compact
    by_msg = _insert._index(session_id, rows)
    tool_msgs = sum(1 for m in rows if isinstance(m, dict) and m.get("role") == "tool")
    lines = [f"задано: адресов {len(addrs)}, номеров {len(ops)} | база: {len(rows)} активных "
             f"строк, из них тул-ответов {tool_msgs} | память гейтвея: {len(history)} сообщений"]
    ops_missing, no_address = [], []
    if ops:
        from_ops, op_lines, ops_missing, no_address = _addresses_of_ops(session_id, ops, rows,
                                                                        by_msg)
        lines += op_lines
        addrs += [a for a in from_ops if a not in addrs]
        facts["msgs"] = list(addrs)

    order = {id(row): i for i, row in enumerate(rows)}
    places, missing = {}, []
    for addr in addrs:
        row = by_msg.get(addr)
        place = order.get(id(row)) if row is not None else None
        if place is None:
            missing.append(addr)
        else:
            places[addr] = place
    if missing:
        lines.append(f"адреса не найдены: {', '.join(missing)} — адреса живут per-session")
    if not places:
        lines.append("ИТОГ: контекст не тронут — ни один адрес не найден")
        _note(engine, "не применено: адресов нет", msgs=addrs, ops=ops,
              missing=missing + ops_missing, no_address=no_address)
        return _facts(facts, lines, "no message could be addressed")

    if operation == OP_CONTENT:
        items, not_results = [], []
        for addr, place in places.items():
            row = rows[place]
            call_id = str((row or {}).get("tool_call_id") or "") if isinstance(row, dict) else ""
            if row.get("role") == "tool" and call_id:
                items.append({"op": "", "tool_call_id": call_id})
            else:
                not_results.append(addr)
        if not_results:
            lines.append(f"не результаты тулов, `drop content` к ним неприменим: "
                         f"{', '.join(not_results)}")
        if not items:
            lines.append("ИТОГ: контекст не тронут — ни одного результата тула")
            return _facts(facts, lines, "no tool result among the ticked messages")
        return _apply_items(session_id, engine, agent, session, history, rows, items,
                            facts, lines, missing + ops_missing, no_address, tool_msgs,
                            list(addrs))

    return _erase_places(session_id, engine, agent, session, history, rows, places, facts,
                         lines, _compact)


def run_import(session_id: str, engine, folder: str, mode: str = "replace") -> dict:
    """Влить строки из папки выгрузки в живую ленту сессии (владелец, 2026-09-29; режимы — `Plan27`).

    `mode`: `replace` — заменить ВСЮ ленту (прежние строки уходят в архив их же
    `replace_messages(archive_dropped=True)`, тот же путь, что у `erase`); `tail` — дописать в
    хвост; `head` — вставить в голову. Разбор и сборка пар — `_import.load`, порядок —
    `_import.assemble`; здесь проверка и коммит. Валидатор без исправления: любой разрыв пары в
    папке — отказ целиком, контекст не тронут. Стыки одной роли (на входе в импорт, на выходе,
    внутри) — мостики `_alternation.fit(kind="import")`, проверка их функцией: не хуже, чем было.
    """
    from . import _alternation, _import
    facts = {"ok": False, "session": session_id, "operation": "import", "mode": "import",
             "import_mode": str(mode or ""), "folder": str(folder or ""), "applied": [],
             "tokens_before": 0, "tokens_after": 0, "chars_saved": 0}
    if mode not in _import.MODES:
        return _facts(facts, [f"режим {mode!r} не знаем, есть: {', '.join(_import.MODES)}",
                              "ИТОГ: контекст не тронут"], f"unknown import mode {mode!r}")
    loaded = _import.load(folder)
    new_rows = loaded["rows"]
    problems = list(loaded["problems"])
    problems += [f"пара: {p.get('kind')} {p.get('tool_call_id') or ''}".rstrip()
                 for p in _stubs.lint_pairs(new_rows)]
    lines = [f"папка: {folder} | файлов: {loaded['files']} | сообщений собрано: {len(new_rows)}"]
    facts.update({"files": loaded["files"], "messages": len(new_rows), "problems": problems})
    if problems:
        lines += [f"проблема: {p}" for p in problems[:20]]
        lines.append(f"ИТОГ: контекст НЕ тронут — проблем {len(problems)}, импорт без исправлений")
        return _facts(facts, lines, f"refused: {len(problems)} problem(s) in the folder")
    if not new_rows:
        lines.append("ИТОГ: контекст не тронут — импортировать нечего")
        return _facts(facts, lines, "nothing to import")

    agent = engine.live_agent()
    session, session_why = _apply.find_gateway_session(agent)
    if session is None:
        return _facts(facts, lines + [f"применить нечем — {session_why}",
                                      "ИТОГ: контекст не тронут"], "no live session to apply to")
    history = _apply.history_of(session)
    rows, rows_why = _apply.db_history(agent)
    if rows_why:
        return _facts(facts, lines + [rows_why, "ИТОГ: контекст не тронут"],
                      "the session database is unreachable")

    # `removed` — при `replace` ВСЯ прежняя лента: её уход и есть операция, предохранитель
    # коммита иначе принял бы его за потерю данных. При `tail`/`head` не уходит ничего.
    tape, removed = _import.assemble(rows, new_rows, mode)
    tape, bridged = _alternation.fit(tape, rows, kind="import")
    lines.append(f"режим: {mode}")
    if bridged:
        lines.append(f"чередование: вставлено мостиков {bridged} — их правило склеило бы соседние "
                     "реплики одной роли")
    was_bad, now_bad = _alternation.their_repairs(rows), _alternation.their_repairs(tape)
    if was_bad is not None and now_bad is not None and now_bad > was_bad:
        lines.append(f"ИТОГ: контекст НЕ тронут — их проверка чередования нашла {now_bad} "
                     f"нарушений против {was_bad} до импорта: такую ленту Hermes склеит")
        _note(engine, "импорт не применён: чередование", was=was_bad, now=now_bad)
        return _facts(facts, lines, f"refused: role alternation ({now_bad} vs {was_bad})")
    # Пары по ВСЕЙ новой ленте: разрыв, которого не было в живой ленте, — наш, отказ.
    pairs_before = len(_stubs.lint_pairs(rows)) if mode != "replace" else 0
    pairs_after = _stubs.lint_pairs(tape)
    if len(pairs_after) > pairs_before:
        lines.append(f"ИТОГ: контекст НЕ тронут — на стыке разорвано пар вызов/ответ: "
                     f"{len(pairs_after) - pairs_before}")
        _note(engine, "импорт не применён: разрыв пары", problems=pairs_after)
        return _facts(facts, lines, f"refused: {len(pairs_after)} broken call/result pair(s)")
    facts["bridges"] = bridged

    # ЖУРНАЛ (Plan27-Task02): операции из спутника папки получают номера ЦЕЛЕВОЙ сессии, и
    # номер в тексте результата меняется до записи ленты. Записи в журнал — только после
    # успешного коммита: отказ иначе оставил бы записи о том, чего в ленте нет.
    from . import _relink
    entries, relink_notes = _relink.relink(new_rows, loaded.get("ledger") or [], session_id)
    lines += [f"журнал: {n}" for n in relink_notes]
    if loaded.get("ledger_bad"):
        lines.append(f"журнал: строк спутника не разобрано {loaded['ledger_bad']} — те операции без привязки")

    before = _apply._estimate(agent, history)
    after = _apply._estimate(agent, tape)
    ok, applied_why = _apply.commit_replacement(session, agent, tape, removed=removed)
    if not ok:
        lines.append(f"ИТОГ: НЕ применено — {applied_why}")
        _note(engine, f"импорт не применён: {applied_why}", folder=str(folder), mode=mode)
        return _facts(facts, lines, "refused at commit, context untouched")
    from ..file_ops import _ledger
    written = sum(1 for e in entries if _ledger.note_imported(session_id, e))
    edited = sum(1 for e in entries if e.get("edited"))
    facts.update({"relinked": written, "relinked_edited": edited})
    if entries or loaded.get("ledger"):
        lines.append(f"журнал: привязано операций {written} из {len(loaded.get('ledger') or [])} "
                     f"записей спутника" + (f", правленых руками {edited}" if edited else ""))
    _signal.bump(session_id, [])
    lines.append(f"было строк: {len(rows)}, стало: {len(tape)}")
    lines.append(f"токены: было ~{before}, стало ~{after}")
    lines.append("ИТОГ: применено"
                 + ("" if applied_why.strip() == "применено" else f" — {applied_why}"))
    _note(engine, applied_why, imported=len(new_rows), folder=str(folder), mode=mode)
    facts.update({"ok": True, "rows_before": len(rows), "rows_after": len(tape),
                  "tokens_before": before, "tokens_after": after,
                  "revision": _signal.read_signal(session_id).get("revision", 0)})
    return _facts(facts, lines, f"imported {len(new_rows)} messages ({mode}) · ~{after} tk")


EDITABLE_ROLES = ("user", "assistant", "system")


def run_edit(session_id: str, engine, addr: str, content: str) -> dict:
    """`paste` (Vision09): заменить ТЕКСТ одного сообщения, всё прочее — байт в байт.

    Два правила из Vision09, иначе сломается тихо:
      1. из буфера приходит ТОЛЬКО `content` — `role`, `tool_calls`, `tool_call_id` и прочие
         поля строки переносятся не глядя; тул-строки не правятся вовсе;
      2. новая строка встаёт НА МЕСТО старой (`_insert._rebuild` с `patch`), старая уходит в
         `removed` — ровно путь коммита дистиллята.
    `api_content` не переносится (видно в панели = видно модели). Время — новое: адрес правленого
    сообщения меняется НАРОЧНО, и правка видна в ленте. После коммита — запись `edited`.
    """
    import time
    from . import _compact
    from ..file_ops import _ledger, _msgindex
    addr = str(addr or "").strip()
    text = str(content if content is not None else "")
    facts = {"ok": False, "session": session_id, "operation": "edit", "mode": "edit",
             "msgs": [addr], "applied": [], "tokens_before": 0, "tokens_after": 0,
             "chars_saved": 0}
    if not text.strip():
        return _facts(facts, ["буфер пуст — заменять нечем", "ИТОГ: контекст не тронут"],
                      "the clipboard is empty")

    agent = engine.live_agent()
    session, session_why = _apply.find_gateway_session(agent)
    if session is None:
        return _facts(facts, [f"применить нечем — {session_why}", "ИТОГ: контекст не тронут"],
                      "no live session to apply to")
    history = _apply.history_of(session)
    rows, rows_why = _apply.db_history(agent)
    if rows_why:
        return _facts(facts, [rows_why, "ИТОГ: контекст не тронут"],
                      "the session database is unreachable")
    facts["indexed"] = _stubs.index_messages(session_id, rows)
    by_msg = _insert._index(session_id, rows)
    row = by_msg.get(addr)
    place = next((i for i, r in enumerate(rows) if r is row), None) if row is not None else None
    if place is None:
        return _facts(facts, [f"адрес {addr} не найден", "ИТОГ: контекст не тронут"],
                      f"{addr} not found")
    role = str(row.get("role") or "")
    if role not in EDITABLE_ROLES:
        return _facts(facts, [f"{addr}: роль {role!r} не правится — вызовы и результаты тулов "
                              "священны", "ИТОГ: контекст не тронут"],
                      f"{role} messages are not editable")
    old_text = str(row.get("api_content") or row.get("content") or "")
    if old_text == text:
        return _facts(facts, [f"{addr}: текст тот же", "ИТОГ: контекст не тронут"],
                      "the clipboard holds the same text")

    new_row = {k: v for k, v in row.items() if k not in ("id", "api_content", "_row_id")}
    new_row["content"] = text
    new_row["timestamp"] = time.time()
    new_rows, new_history, _eaten = _insert._rebuild(rows, history, [], [], {place: new_row}, ())
    new_rows = _apply.drop_stale_api_content(rows, new_rows)

    before = _apply._estimate(agent, history)
    after = _apply._estimate(agent, new_history)
    ok, applied_why = _apply.commit_replacement(session, agent, new_rows,
                                                removed=[row])
    lines = [f"{addr}: {len(old_text)} → {len(text)} символов"]
    if not ok:
        lines.append(f"ИТОГ: НЕ применено — {applied_why}")
        _note(engine, f"правка не применена: {applied_why}", msg=addr)
        return _facts(facts, lines, "refused at commit, context untouched")

    fresh, _ = _apply.db_history(agent)
    fresh_by_msg = _insert._index(session_id, fresh) if fresh else {}
    target = _msgindex.place(new_row)
    now = next((a for a, r in fresh_by_msg.items() if _msgindex.place(r) == target), "")
    facts["edited"] = bool(now and _ledger.note_edited(session_id, addr, now))
    _signal.bump(session_id, [])
    lines.append(f"новый адрес: {now or '—'}")
    lines.append(f"токены: было ~{before}, стало ~{after}")
    lines.append("ИТОГ: применено"
                 + ("" if applied_why.strip() == "применено" else f" — {applied_why}"))
    _note(engine, applied_why, was=addr, now=now)
    facts.update({"ok": True, "applied": [addr], "now": now,
                  "tokens_before": before, "tokens_after": after,
                  "revision": _signal.read_signal(session_id).get("revision", 0)})
    return _facts(facts, lines, f"{addr} edited" + (f" → {now}" if now else ""))


def _erase_places(session_id, engine, agent, session, history, rows, places, facts, lines,
                  _compact) -> dict:
    """`erase` по позициям: правило владения, пересборка без дистиллята, коммит."""
    from . import _alternation, _pairs
    decision = _pairs.settle(rows, set(places.values()))
    patch = decision["patch"]
    eaten = decision["eaten"]
    replaced = [decision["was"][i] for i in sorted(decision["was"])]
    for note in decision["notes"]:
        lines.append("пары: " + note)

    new_rows, new_history, eaten_rows = _insert._rebuild(rows, history, [], [], patch, eaten)
    new_rows = _apply.drop_stale_api_content(rows, new_rows)

    # ЧЕРЕДОВАНИЕ (2026-09-30): стирание рождает стыки одной роли — ответ между двумя репликами
    # человека, реплика между двумя ответами, лента с ответа ассистента. Их загрузчик такие пары
    # СКЛЕИТ (и запишет склейку в базу) — адреса поедут. Тот же орган, что у компакции:
    # мостик на новом стыке, проверка их же функцией «нарушений не больше, чем было».
    new_rows, bridged_rows = _alternation.fit(new_rows, rows, kind="erase")
    new_history, bridged = _alternation.fit(new_history, history, kind="erase")
    if bridged or bridged_rows:
        lines.append(f"чередование: вставлено мостиков {bridged} (в базе {bridged_rows}) — "
                     "их правило склеило бы соседние реплики одной роли")
    was_bad, now_bad = _alternation.their_repairs(history), _alternation.their_repairs(new_history)
    if was_bad is not None and now_bad is not None and now_bad > was_bad:
        lines.append(f"ИТОГ: контекст НЕ тронут — их проверка чередования нашла {now_bad} "
                     f"нарушений против {was_bad} до стирания: такую ленту Hermes склеит")
        _note(engine, "не применено: чередование", was=was_bad, now=now_bad)
        return _facts(facts, lines, f"refused: role alternation ({now_bad} vs {was_bad})")
    facts["bridges"] = bridged_rows

    problems = _stubs.lint_pairs(new_rows)
    if problems:
        lines.append(f"ИТОГ: контекст НЕ тронут — после стирания осталось {len(problems)} "
                     "рассогласованных пар вызов/ответ. Правило владения таких не создаёт, "
                     "значит разрыв был в ленте до нас")
        _note(engine, "не применено: разрыв пары", problems=problems)
        return _facts(facts, lines, f"refused: {len(problems)} broken call/result pair(s)")

    before = _apply._estimate(agent, history)
    after = _apply._estimate(agent, new_history)
    ok, applied_why = _apply.commit_replacement(session, agent, new_rows,
                                                removed=eaten_rows + replaced)
    if not ok:
        lines.append(f"ИТОГ: НЕ применено — {applied_why}")
        _note(engine, f"не применено: {applied_why}", msgs=list(places))
        return _facts(facts, lines, "refused at commit, context untouched")

    # Надгробия нашим стёртым операциям (владелец, 2026-09-29: «пусть будет»). ПОСЛЕ коммита
    # и ДО сигнала — панель перечитывает данные по бампу. Исход записи на `ok` не влияет.
    index = _stubs.ledger_index(session_id)
    ops = []
    for row in eaten_rows:
        if isinstance(row, dict) and row.get("role") == "tool":
            op = _stubs._op_of({}, index, str(row.get("tool_call_id") or ""))
            if op:
                ops.append(op)
    facts["tombstones"] = _stubs.note_erased(session_id, ops, "erase")
    _signal.bump(session_id, ops)

    erased = [addr for addr, place in places.items() if place in eaten]
    lines.append(f"стёрто сообщений: {len(eaten_rows)} (из них названо {len(erased)}), "
                 f"правлено строк: {len(replaced)}")
    lines.append(f"токены: было ~{before}, стало ~{after} (освободилось ~{max(0, before - after)})")
    lines.append("ИТОГ: применено"
                 + ("" if applied_why.strip() == "применено" else f" — {applied_why}"))
    _note(engine, applied_why, erased=erased, tokens_before=before, tokens_after=after)
    facts.update({"ok": True, "applied": erased, "ops": ops,
                  "rows_before": len(rows), "rows_after": len(new_rows),
                  "tokens_before": before, "tokens_after": after,
                  "tokens_dropped": max(0, before - after),
                  "revision": _signal.read_signal(session_id).get("revision", 0)})
    freed = f" · freed ~{facts['tokens_dropped']} tk" if facts["tokens_dropped"] > 0 else ""
    return _facts(facts, lines, f"erased {len(eaten_rows)} messages{freed}")
