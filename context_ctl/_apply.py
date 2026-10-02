"""Применение выбора ПО ТРЕБОВАНИЮ: найти живую сессию и переписать историю на месте.

ПОЧЕМУ ЭТО НЕ ТРИ СТРОЧКИ. Обработчик слеш-команды получает ТОЛЬКО строку аргументов —
проверено по их коду (`hermes_cli/plugins.py::register_command`: «handler `fn(raw_args:
str)`», и вызов в `gateway/run_inbound.py::_hm_dispatch_quick_and_plugin_commands`
передаёт ровно один аргумент). Ни сессии, ни агента, ни истории. Поэтому:

1. **Какая сессия — СПРАШИВАЕМ У ПАНЕЛИ.** Она единственная, кто знает это наверняка: она
   сама подставляет сессию в каждый маршрут, который зовёт (`/ops?session=...`, файл
   выбора). Поэтому панель записывает открытый чат (`POST /context-focus` →
   `_select.write_focus`), а команда его читает.

   Так было не всегда, и путь сюда стоит помнить. Сначала мы угадывали по активности («где
   последней отвечала модель») — на локальном провайдере без `usage` это слепо, потому что
   `update_from_response` там не вызывается вовсе. Потом я ПОДМЕНЯЛ их диспетчер
   (`methods_tools._dispatch_plugin`), чтобы перехватить `params["session_id"]`. Владелец
   на это сказала прямо: патчить гейтвей ради номера сессии — бред, панель его и так знает.
   Она права, патч удалён.

   Порядок источников: явный `--session` > открытый чат от панели > активность. Третий
   остался фолбэком на случай, когда панель ни разу не открывалась, и при неоднозначности
   ОТКАЗЫВАЕТ, а не угадывает.

   Честная цена: «открытый в панели чат» — свидетельство, а не доказательство того, что
   команда набрана именно там. В десктопе они совпадают по построению (панель следует за
   сфокусированным чатом). Выбранная сессия НАЗЫВАЕТСЯ в ответе в любом случае, а
   `--session` остаётся способом переспорить.

2. **Где живая история.** НЕ на агенте. Она в сессионном словаре гейтвея
   (`_sessions[sid]["history"]`) под своим локом и своей версией.

3. **Чем коммитим — `replace_messages`, а НЕ их конвейер компакции.** Это исправление
   2026-09-08, оплаченное живым прогоном владельца: применение через их
   `_compress_session_history` → `_compress_context` → `archive_and_compact` давало
   ВЕРНЫЙ промпт для модели и ПЕРЕПУТАННЫЙ разговор для человека. Замер на настоящей
   SQLite (`__HQ/recon/subjects/ctxdrop-row-order/probe_row_order.py`), одна и та же
   правка:

       archive_and_compact, без timestamp  → модель 7 строк верно, ИНТЕРФЕЙС 14 (всё дважды)
       archive_and_compact, с timestamp    → модель 7 строк верно, ИНТЕРФЕЙС 9 (пара в конце)
       replace_messages(archive_dropped)   → 7 и 7, верно в обоих видах

   Причина структурная, а не наша ошибка: их дисплейный дедуп поколений
   (`hermes_state_messages.py::_dedupe_display_generations`) ключуется по
   `(role, content, timestamp, tool_call_id, tool_calls, tool_name)` и сортирует по id
   ПЕРВОЙ строки с этим ключом. `archive_and_compact` архивирует старые строки (интерфейс
   их показывает) и вставляет список заново. Нетронутое совпадает ключом со своим архивным
   двойником и стоит на месте; переписанное нами имеет ДРУГОЙ ключ по определению, совпасть
   не с чем — и его «первая строка» это новая, с высоким id, то есть конец разговора.
   Внутри их пути это не лечится: любая правка содержимого создаёт новое поколение.
   `archive_and_compact` рассчитан на «схлопнуть диапазон в сводку», а не «переписать поле
   на месте» — для второго у них есть `replace_messages`, которым живут их же `/undo`,
   `/retry` и `/compress`: заменённые строки уходят в rewind-архив (`active=0,
   compacted=0` — интерфейс их не показывает, поиск и восстановление сохраняются), а новый
   набор остаётся ЕДИНСТВЕННЫМ поколением.

   Цена честно: `replace_messages` заменяет ВСЕ активные строки сессии, поэтому передавать
   обязательно полную историю. От одновременного хода защищаемся их же
   `reject_active_turn_lease=True` плюс локом и версией гейтвея.

4. **ЧТО ИМЕННО передаём в базу — строки БАЗЫ, а не память гейтвея.** Исправление
   2026-09-09 после потери данных: мы передавали снимок `session["history"]`, считая его
   полной историей. Он ей не является — чтение на 36 066 символов (`op 001f`) в базе было,
   а в снимке нет, и `replace_messages` увело его в rewind-архив. Второе, тихое: строки
   базы несут поля, которых в памяти гейтвея нет вовсе (`display_kind`,
   `display_metadata`, `api_content`, reasoning-колонки, `timestamp`), — запись из
   снимка обнуляла их у КАЖДОЙ строки сессии, а не только у тронутых.

   Поэтому правка идёт по базе, а память гейтвея после записи ПЕРЕЧИТЫВАЕТСЯ из неё:

       база     ← get_messages(session_id) → заглушки → replace_messages
       гейтвей  ← get_messages_as_conversation(..., include_row_ids=True)
                  → canonicalize_replay_history → на место (`history[:] = …`)

   `get_messages` (по умолчанию только активные, `ORDER BY id ASC`) отдаёт словари, чьи
   ключи `_insert_message_rows` читает обратно один в один, включая `timestamp` — то есть
   круговой оборот через базу ничего не теряет. Память гейтвея мы НЕ собираем сами
   (`Plan26`, 2026-10-02): у неё своя форма со служебными метками (`_row_id`,
   `_db_persisted`), и прежняя правка памяти «теми же сообщениями по тем же `tool_call_id`»
   оставляла у перевставленных строк старые `_row_id` — их сверка перед ходом дописывала
   наши строки второй раз (`Recon33`). Форму держит их загрузчик — тот же рецепт, что у
   их резюма (`_memory_loader`).

ДЕГРАДАЦИЯ, А НЕ ПАДЕНИЕ. Оба шва приватные. Если любой пропал после обновления Hermes,
команда честно скажет об этом и НЕ тронет контекст.
"""

from __future__ import annotations

from . import _bridge, _select, _stubs


def is_child_agent(agent) -> bool:
    """Сабагент/форк, а не чат человека.

    Наш патч `__init__` ловит КАЖДОГО сконструированного агента, включая делегатов
    (`delegate_task`) и форки. У сабагента своя сессия, своя копия движка и своя метка
    активности — и пока он работает, он самый активный из всех. Значит без этого фильтра
    слеш-команда, набранная во время работы сабагента, выбрала бы ЕГО.

    Признак — `parent_session_id`: его выставляет `init_agent` только детям.
    """
    return bool(str(getattr(agent, "parent_session_id", "") or "").strip())


def human_sessions() -> list:
    """`[(session_id, движок)]` — только сессии, в которые человек может писать.

    Источник истины — реестр гейтвея: в нём лежат ровно верхнеуровневые чаты, а сабагенты
    и форки в него не попадают вообще. Наш собственный реестр движков остаётся ФОЛБЭКОМ на
    случай, когда реестр гейтвея недоступен (их приватный шов, плюс при
    `dashboard.turn_isolation` ходы вообще уезжают в дочерний процесс) — там фильтруем
    детей вручную.
    """
    out = []
    try:
        from tui_gateway import server as gw
        registry = getattr(gw, "_sessions", None)
        if isinstance(registry, dict):
            for session in list(registry.values()):
                if not isinstance(session, dict):
                    continue
                agent = session.get("agent")
                engine = getattr(agent, "context_compressor", None)
                if engine is not None and getattr(engine, "name", "") == "ctxctl":
                    out.append((getattr(engine, "_session_id", "") or "", engine))
            if out:
                return out
    except Exception:  # noqa: BLE001 - шов приватный, отсутствие не повод падать
        pass
    return [(sid, engine) for sid, engine in _bridge.live_engines()
            if not is_child_agent(engine.live_agent())]


def pick_engine(session_hint: str = "") -> tuple:
    """`(session_id, движок, пояснение)`. Движок None — значит не нашли, пояснение скажет.

    `session_hint` — явное указание сессии, если человек его дал: угадывание по «кто
    говорил последним» надёжно, но не безошибочно, и должен быть способ его переспорить.
    """
    live = human_sessions()
    if not live:
        if not _bridge.installed():
            return "", None, ("мост к живому agent не установлен — движок не выбран в "
                              "конфиге Hermes (`context.engine: ctxctl`) или сессия "
                              "стартовала до загрузки плагина")
        if _bridge.live_engines():
            return "", None, ("живые сессии есть, но все они сабагенты/форки — в такую "
                              "человек не пишет, применять там нечего")
        return "", None, ("ни одной живой сессии с нашим движком — перезапусти Hermes "
                          "после включения `context.engine: ctxctl`")

    # СЕССИЯ ВЫЗОВА — не догадка, а факт. Человек нажимает кнопку или пишет команду В
    # СВОЁМ чате, и их диспетчер эту сессию знает (`methods_tools._dispatch_plugin` получает
    # `params["session_id"]`), просто обработчику её не передаёт. Мост её перехватывает.
    #
    # Порядок источников: явный `--session` > сессия вызова > активность. Третий остаётся
    # только фолбэком для путей, где патча нет (CLI без гейтвея), и он при неоднозначности
    # ОТКАЗЫВАЕТ, а не угадывает.
    if not session_hint:
        open_now = str((_select.read_focus() or {}).get("session") or "")
        if open_now:
            for session_id, engine in live:
                if session_id == open_now:
                    return session_id, engine, f"открытый чат: {session_id}"
            engine, why = _engine_of_gateway_key(open_now)
            if engine is not None:
                return getattr(engine, "_session_id", "") or open_now, engine, why
            # Панель сообщила чат, а движка в нём нет. Не перебираем остальные: правка ушла
            # бы туда, где человек не смотрит.
            return "", None, (f"панель сообщила открытый чат {open_now}, но движка ctxctl в "
                              "нём нет — сессия стартовала до включения "
                              "`context.engine: ctxctl` или до загрузки плагина. Живые с "
                              "движком: " + ", ".join(sid for sid, _ in live))

    if session_hint:
        for session_id, engine in live:
            if session_id == session_hint:
                return session_id, engine, f"сессия задана явно: {session_id}"
        return "", None, (f"сессия {session_hint!r} среди живых не найдена; живые: "
                          + ", ".join(sid for sid, _ in live))

    live.sort(key=lambda pair: getattr(pair[1], "_last_active", 0.0), reverse=True)
    session_id, engine = live[0]
    if len(live) == 1:
        return session_id, engine, f"единственная живая сессия: {session_id}"
    if not getattr(engine, "_last_active", 0.0):
        return "", None, ("живых сессий несколько и ни в одной ещё не отвечала модель — "
                          "какую из них ты имеешь в виду, определить нечем; скажи сессию "
                          "явно. Живые: " + ", ".join(sid for sid, _ in live))
    return session_id, engine, (f"выбрана самая активная из {len(live)} живых: {session_id}")


def _engine_of_gateway_key(key: str) -> tuple:
    """Движок сессии по КЛЮЧУ РЕЕСТРА гейтвея. `(движок, пояснение)`.

    Ищем ровно тем же способом, что их собственный диспетчер —
    `_sessions.get(params["session_id"])` (`methods_tools.py`, стадии слеш-команд). Это
    важнее, чем кажется: ключ реестра НЕ обязан совпадать с `agent.session_id` (их
    `_sync_session_key_after_compress` этот ключ меняет), поэтому сравнивать строки было бы
    ошибкой. Берём словарь сессии по ключу и спрашиваем его агента.
    """
    try:
        from tui_gateway import server as gw
    except Exception as exc:  # noqa: BLE001
        return None, f"tui_gateway.server не импортируется ({exc})"
    registry = getattr(gw, "_sessions", None)
    if not isinstance(registry, dict):
        return None, "реестр сессий гейтвея (_sessions) пропал"
    session = registry.get(key)
    if not isinstance(session, dict):
        return None, f"сессии {key} нет в реестре гейтвея"
    engine = getattr(session.get("agent"), "context_compressor", None)
    if engine is None or getattr(engine, "name", "") != "ctxctl":
        return None, (f"команда вызвана в сессии {key}, но движка ctxctl в ней нет — она "
                      "стартовала до включения `context.engine: ctxctl` или до загрузки "
                      "плагина")
    return engine, f"сессия вызова: {key}"


def find_gateway_session(agent) -> tuple:
    """Сессионный словарь гейтвея для ЭТОГО агента. `(session, пояснение)`.

    Сопоставляем ПО ТОЖДЕСТВУ объекта агента, а не по идентификатору сессии: ключ в их
    реестре — это gateway session key, который не обязан совпадать с `agent.session_id`
    (их же `_sync_session_key_after_compress` этот ключ ещё и меняет). Тождество агента
    сомнений не оставляет.
    """
    try:
        from tui_gateway import server as gw
    except Exception as exc:  # noqa: BLE001
        return None, f"tui_gateway.server не импортируется ({exc})"
    registry = getattr(gw, "_sessions", None)
    if not isinstance(registry, dict):
        return None, "tui_gateway.server._sessions пропал или сменил тип"
    for sid, session in list(registry.items()):
        if isinstance(session, dict) and session.get("agent") is agent:
            return session, f"сессия гейтвея: {sid}"
    return None, ("этот агент не найден среди сессий гейтвея — команда пришла не из "
                  "десктопа/гейтвея, либо сессия уже закрыта")


def _estimate(agent, messages) -> int:
    """Оценка давления запроса — их же функцией и с теми же слагаемыми.

    Считаем ВМЕСТЕ с системным промптом и схемами тулов, как это делает их
    `_compress_live_with_feedback`: транскрипт сам по себе занижает реальную цену запроса,
    и цифра «останется M» без промпта и схем была бы неправдой в нашу пользу.
    """
    try:
        from agent.model_metadata import estimate_request_tokens_rough
    except Exception:  # noqa: BLE001
        return 0
    try:
        return int(estimate_request_tokens_rough(
            messages,
            system_prompt=getattr(agent, "_cached_system_prompt", "") or "",
            tools=getattr(agent, "tools", None) or None,
        ))
    except Exception:  # noqa: BLE001
        return 0


def preview(agent, messages, items, session_id) -> dict:
    """Сухой прогон: что будет, если применить. Ничего не меняет.

    Именно эти две цифры владелец хочет видеть у галочек: сколько выбросим и сколько
    ОСТАНЕТСЯ — второе и есть фактическая цена холодного пересчёта после применения.
    """
    index = _stubs.ledger_index(session_id)
    after_messages, report = _stubs.apply_stubs(messages, items, index)
    before = _estimate(agent, messages)
    after = _estimate(agent, after_messages)
    return {
        "tokens_before": before,
        "tokens_after": after,
        "tokens_dropped": max(0, before - after),
        "chars_saved": report.get("chars_saved", 0),
        "would_stub": len(report.get("stubbed") or []),
        "not_found": report.get("not_found") or [],
        "problems": report.get("problems") or [],
    }


def db_history(agent) -> tuple:
    """Активные строки базы как список сообщений: `(строки, пояснение)`.

    ЭТО источник для записи, а не память гейтвея — см. пункт 4 в докстринге модуля. Их
    `get_messages` по умолчанию отдаёт ровно активные строки в порядке вставки (`id ASC`,
    «never timestamp: clocks regress») и декодирует `content`/`tool_calls`, то есть
    возвращает то же, что мы потом отдадим назад.

    Пустой список — законный исход (свежая сессия), поэтому «не смогли прочитать»
    отличается от «прочитали, там пусто» через пояснение, а не через пустоту.
    """
    session_db = getattr(agent, "_session_db", None)
    session_id = getattr(agent, "session_id", "") or ""
    if session_db is None or not session_id:
        return [], ("у агента нет session_db — читать нечего; сессия без базы "
                    "(форк/изолированный прогон)")
    if not callable(getattr(session_db, "get_messages", None)):
        return [], "session_db.get_messages пропал — прочитать активные строки нечем"
    try:
        rows = [dict(row) for row in session_db.get_messages(session_id)]
    except Exception as exc:  # noqa: BLE001 - шов приватный, деградируем с причиной
        return [], f"базу прочитать не удалось — {type(exc).__name__}: {exc}"
    return rows, ""


def drop_stale_api_content(old_rows, new_rows) -> list:
    """Снять `api_content` с тех строк, чей `content` мы переписали.

    `api_content` — их байтовый сайдкар «что ушло в API, когда отличалось от content»
    (`hermes_state_messages.py::add_message`). Сегодня он ставится только на последнюю
    строку пользователя (`set_latest_user_api_content`), то есть на тул-ответы не попадает
    и снимать нечего. Делаем всё равно и заранее: если сайдкар когда-нибудь появится на
    строке, которую мы заглушили, воспроизведение возьмёт ИМЕННО его — и выброшенное
    человеком вернётся в промпт молча, мимо всей нашей механики.

    СВЕРКА ПО АДРЕСУ, А НЕ ПОЗИЦИОННАЯ. Была позиционная, и держалось это на том, что
    `apply_stubs` сохраняет длину списка. Механизм 2 (`erase tool`) её не сохраняет: после
    первой удалённой строки позиции съезжают на единицу, и сравнивались бы РАЗНЫЕ строки.
    Поэтому сопоставляем по `id` строки базы, а без него — по `tool_call_id`.

    Строку без `id` и без адреса сопоставляем по самому тексту: если ровно такой текст был в
    прежнем наборе, значит эту строку не трогали, и сайдкар остаётся. Такие строки бывают —
    снимок памяти гейтвея id не несёт вовсе, — и обеднять нетронутое было бы неправдой.

    Не нашли ни по адресу, ни по тексту — ЧИСТИМ. Из двух ошибок выбрана дешёвая: лишний раз
    снятый сайдкар означает воспроизведение из `content` (тот же текст, но, возможно, не байт
    в байт — то есть промах кеша провайдера, о котором мы и так предупреждаем отдельной
    строкой). Оставленный по ошибке сайдкар означает, что выброшенное человеком ВЕРНЁТСЯ в
    промпт молча.
    """
    def key(row):
        if not isinstance(row, dict):
            return None
        if row.get("id") is not None:
            return ("id", row.get("id"))
        call_id = str(row.get("tool_call_id") or "")
        return ("call", call_id) if call_id else None

    old_by_key = {}
    old_texts = set()
    for row in old_rows or []:
        if not isinstance(row, dict):
            continue
        k = key(row)
        if k is not None:
            old_by_key.setdefault(k, row)
        old_texts.add((row.get("role"), row.get("content")))

    out = []
    for row in new_rows or []:
        if isinstance(row, dict) and row.get("api_content"):
            old = old_by_key.get(key(row))
            unchanged = (old.get("content") == row.get("content")) if old is not None \
                else ((row.get("role"), row.get("content")) in old_texts)
            if not unchanged:
                row = {**row}
                row.pop("api_content", None)
        out.append(row)
    return out


def commit_replacement(session, agent, db_messages, **kwargs) -> tuple:
    """Записать новую ленту — ЕДИНСТВЕННЫЙ путь наших правок базы (compact / drop / erase / edit /
    import). После успешной записи сообщаем движку сессии: реальное число последнего ответа
    сервера (якорь счётчика токенов) описывает СТАРУЮ ленту и больше не годится
    (`Plan16` п.1, задача B: иначе автомат видит прежнюю длину и сжимает снова и снова).
    """
    ok, why = _commit_replacement(session, agent, db_messages, **kwargs)
    if ok:
        engine = getattr(agent, "context_compressor", None)
        note = getattr(engine, "note_rewrite", None)
        if callable(note):
            try:
                note()
            except Exception:  # noqa: BLE001 - запись уже прошла; счётчик не вправе её отменить
                pass
    return ok, why


# ОТКАЗЫ, КОТОРЫЕ ПРОХОДЯТ САМИ (2026-09-30): запись отбита, потому что сессия живёт — идёт ход
# (их аренда хода проверяется В ТОЙ ЖЕ транзакции, что и запись: гонки нет) или ход уже дописал в
# базу строки, которых нет в нашей ленте. Ручная компакция на такой отказ не выбрасывает
# результат, а ждёт конца хода и пересобирает ленту (`_compact._assemble_until_written`).
# Причина начинается с одного из этих слов — по ним вызывающий и узнаёт.
BUSY_TURN = "идёт ход"
BUSY_MOVED = "база ушла вперёд"
_TURN_BUSY_ERRORS = ("SessionTurnLeaseLostError", "SessionCompressionInProgressError")


def should_retry(why: str) -> bool:
    """Отказ записи из-за живой сессии (ход идёт / база ушла вперёд), а не из-за ошибки."""
    return str(why or "").startswith((BUSY_TURN, BUSY_MOVED))


def _memory_loader(session_db):
    """`(читатель, "")` или `(None, причина)`: память гейтвея из базы — ИХ загрузчиком (`Plan26`).

    Ровно так память наполняет их резюм десктопа (`tui_gateway/server.py::_load_resume_transcript`):
    `get_messages_as_conversation(..., repair_alternation=True, include_row_ids=True)`, затем
    `canonicalize_replay_history`. Загрузчик ставит на каждое сообщение `_row_id` и `_db_persisted`;
    по первой метке их сверка перед ходом решает, что в базе новее памяти, по второй — их сохранение
    решает, что не писать. Наша прежняя сборка памяти из старых словарей оставляла у перевставленных
    строк СТАРЫЕ `_row_id`, и следующий ход дописывал наши же строки второй раз (`Recon33`, сессия
    `20261001_225039_9d612f`). Форму сообщения держит Hermes — мы её больше не угадываем.
    """
    load = getattr(session_db, "get_messages_as_conversation", None)
    if not callable(load):
        return None, "session_db.get_messages_as_conversation пропал — перечитать память из базы нечем"
    try:
        from agent.replay_cleanup import canonicalize_replay_history
    except Exception as exc:  # noqa: BLE001 - их модуль переехал/переименован
        return None, ("agent.replay_cleanup.canonicalize_replay_history недоступен "
                      f"({type(exc).__name__}: {exc})")

    def read(session_id):
        rows = load(session_id, repair_alternation=True, include_row_ids=True)
        if not isinstance(rows, list):
            raise TypeError(f"загрузчик вернул {type(rows).__name__}, а не список")
        return canonicalize_replay_history(rows)
    return read, ""


def _commit_replacement(session, agent, db_messages, removed=()) -> tuple:
    """Записать переписанную историю: сначала в базу, потом перечитать память гейтвея ИЗ БАЗЫ.

    `(ok, пояснение)`. `db_messages` — переписанные строки БАЗЫ (полный активный набор). Память
    (`session["history"]`) мы больше не собираем: после записи её наполняет их загрузчик
    (`_memory_loader`, `Plan26`). Его наличие проверяется ДО записи: без него отказ, база не тронута.

    Порядок именно такой: база — источник, из которого интерфейс и следующая сессия читают
    историю, а память гейтвея — то, из чего собирается следующий запрос. Упасть между ними
    всё равно можно, но если первой падает база, то в памяти ничего не меняется и человек
    видит ровно то, что и было; в обратном порядке он получил бы правку, исчезающую при
    перезапуске.

    `removed` — сообщения, которые механизм удалил НАРОЧНО (механизм 2, `erase tool`). Без
    этого списка предохранитель ниже видит их отсутствие как потерю данных и отказывает — и
    именно так `erase tool` не работал вовсе при живом прогоне 2026-09-09: отказ приходил на
    те самые строки, которые человек и стирал. Предохранитель при этом НЕ ослабляется:
    пропускаются ровно названные сообщения, любое другое расхождение по-прежнему отказ.
    """
    version = int(session.get("history_version", 0))
    session_db = getattr(agent, "_session_db", None)
    session_id = getattr(agent, "session_id", "") or ""
    if session_db is None or not session_id:
        return False, ("у агента нет session_db — писать некуда; сессия без базы "
                       "(форк/изолированный прогон)")
    if not callable(getattr(session_db, "replace_messages", None)):
        return False, ("session_db.replace_messages пропал — примитив замены строк "
                       "недоступен, применять нечем")
    read_memory, no_loader = _memory_loader(session_db)
    if read_memory is None:
        return False, (f"{no_loader}. Без него память после записи пришлось бы собирать самим, "
                       "а это и задваивало хвост (`Recon33`). Контекст не тронут")

    unaccounted = _unaccounted_active_rows(session_db, session_id, db_messages, removed)
    if unaccounted:
        return False, (
            f"{BUSY_MOVED}: ОТКАЗ: в базе есть {len(unaccounted)} активных строк, которых нет ни в нашем "
            "списке, ни среди удаляемых нарочно — применить значило бы их потерять. "
            "`replace_messages` снимает ВСЕ активные строки и вставляет ровно переданное, "
            "поэтому список обязан быть полным. Контекст не тронут. Первые: "
            + "; ".join(f"id={r[0]} {r[1]} {r[2]}" for r in unaccounted[:3]))

    try:
        session_db.replace_messages(session_id, db_messages, archive_dropped=True,
                                    reject_active_turn_lease=True)
    except Exception as exc:  # noqa: BLE001 - в т.ч. их отказ при активном ходе
        if type(exc).__name__ in _TURN_BUSY_ERRORS:
            return False, (f"{BUSY_TURN}: {type(exc).__name__}: {exc} — дождись ответа модели. "
                           "Контекст не тронут")
        return False, (f"{type(exc).__name__}: {exc} — вероятнее всего в сессии идёт ход; "
                       "дождись ответа модели и повтори. Контекст не тронут")

    try:
        memory = read_memory(session_id)
    except Exception as exc:  # noqa: BLE001 - база уже записана, откатывать нечем
        # Прежнюю память НЕ оставляем молча: в ней старые `_row_id`, на следующем ходу их сверка
        # задвоит хвост. Собрать её сами тоже не можем — значит, только громко.
        return True, (f"применено в БАЗЕ, но память гейтвея НЕ перечитана ({type(exc).__name__}: "
                      f"{exc}) — переоткрой сессию (или перезапусти шлюз) ДО следующего сообщения, "
                      "иначе следующий ход может задвоить хвост")
    lock = session.get("history_lock")
    if lock is None:
        _write_history(session, memory, version)
        return True, "применено (без лока — сессия без history_lock)"
    with lock:
        if int(session.get("history_version", 0)) != version:
            # База уже переписана, а память гейтвея ушла вперёд. Не затираем чужую правку:
            # честнее сказать про расхождение, чем молча выкинуть чей-то ход.
            return True, ("применено в БАЗЕ, но история в памяти изменилась во время "
                          "записи и НЕ обновлена — перезапусти сессию, чтобы вид совпал")
        _write_history(session, memory, version)
    return True, "применено"


def _row_key(role, content, tool_call_id) -> tuple:
    """Ключ сообщения для сверки списка с базой: адрес, иначе роль плюс отпечаток текста."""
    call_id = str(tool_call_id or "")
    if call_id:
        return ("id", call_id)
    text = str(content or "")
    return ("txt", str(role or ""), len(text), text[:120])


def _unaccounted_active_rows(session_db, session_id, new_messages, removed=()) -> list:
    """Активные строки базы, которых НЕТ в нашем списке. Пустой список = писать безопасно.

    ЗАЧЕМ. `replace_messages(archive_dropped=True)` архивирует ВСЕ активные строки и
    вставляет ровно то, что ей передали. Значит любая строка, отсутствующая в нашем списке,
    молча уходит из живого разговора — и это уже случилось: 2026-09-08 чтение на 36 066
    символов (`op 001f`) оказалось в rewind-архиве, потому что снимок `session["history"]`
    его не содержал, хотя в базе он был. Допущение «память гейтвея = полная история»
    неверно, и цена ошибки — потерянные сообщения, а не неудачная правка.

    Поэтому перед записью сверяемся с БАЗОЙ. Сверка по адресу (`tool_call_id`), а для
    сообщений без него — по роли и отпечатку текста: сравнивать полные тексты дорого, а
    длина плюс начало отличают разные сообщения надёжно для этой цели.

    Ошибка чтения базы трактуется как «сверить не смогли» и возвращает пустой список: иначе
    любой сбой чтения заблокировал бы применение навсегда. Риск принят осознанно — запись
    всё равно делает их атомарная транзакция, а альтернатива (отказывать всегда) сделала бы
    механизм неработоспособным при любой временной проблеме с базой.
    """
    try:
        rows = [dict(r) for r in session_db.get_messages(session_id)]
    except Exception:  # noqa: BLE001 - см. докстринг: не смогли сверить != не писать
        return []
    have = {_row_key(m.get("role"), m.get("content"), m.get("tool_call_id"))
            for m in (new_messages or []) if isinstance(m, dict)}
    # Удалённое НАРОЧНО отсутствует законно. Ключи считаем тем же `_row_key`, что и всё
    # остальное: удалённое сообщение — это та же строка базы, просто её больше нет в списке.
    have |= {_row_key(m.get("role"), m.get("content"), m.get("tool_call_id"))
             for m in (removed or []) if isinstance(m, dict)}
    missing = []
    for row in rows:
        key = _row_key(row.get("role"), row.get("content"), row.get("tool_call_id"))
        if key not in have:
            missing.append((row.get("id"), row.get("role"),
                            str(row.get("content") or "")[:40]))
    return missing


def _write_history(session, new_messages, version) -> None:
    """Обновить историю НА МЕСТЕ, а не переприсвоить ключ.

    Разница не стилистическая. Присваивание `session["history"] = [...]` меняет только то,
    на что смотрит словарь сессии; любой, кто держит ССЫЛКУ на прежний объект списка
    (активный ход, коллбэки гейтвея), продолжит дописывать в старый — и наша правка
    окажется замороженным снимком, в который новые сообщения больше не попадают. Симптом,
    оплаченный живьём 2026-09-08: после первого успешного применения `/ctx-drop` перестал
    находить в «живой истории» операции, которые в стакане прекрасно видны.

    `[:]` меняет содержимое ТОГО ЖЕ объекта, поэтому все держатели видят одно и то же.
    """
    history = session.get("history")
    if isinstance(history, list):
        history[:] = list(new_messages)
    else:
        session["history"] = list(new_messages)
    session["history_version"] = version + 1


def history_of(session) -> list:
    """Копия живой истории под их локом. Читать без лока нельзя — её пишет активный ход."""
    lock = session.get("history_lock")
    if lock is None:
        return list(session.get("history") or [])
    with lock:
        return list(session.get("history") or [])


def selection_for(session_id: str) -> list:
    """Отмеченное в режиме `drop content` — единственном, который умеет применяться."""
    return [item for item in (_select.read_selection(session_id).get("items") or [])
            if item.get("mode") == _select.MODE_CONTENT]
