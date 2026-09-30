"""Файл-выбор: что человек отметил выбросить. Одна форма, две половины.

КТО С КЕМ СХОДИТСЯ. Пишет — панель (чекбоксы → `POST /context-select`). Читает — движок,
внутри `compress()`, когда панель нажала «применить» (Plan13 Task 3/4). Разойтись эти две
половины могут МОЛЧА: движок просто не найдёт того, что панель считает отмеченным, и
компакция сделает не то. Поэтому форма описана здесь, в единственном нормативном месте, и
проверяется тестами. Когда клиентская половина встанет и форма перестанет меняться — её
место в `__HQ/CONTRACT__*` (правило из `Doc__naming-and-contracts.md`).

ФОРМА:

    {
      "version": 1,
      "session": "<id сессии, как его знает Hermes>",
      "updated_at": 1757356000.0,
      "items": [
        {"op": "0156", "tool_call_id": "KbB7...", "mode": "content"},
        {"op": "0157", "tool_call_id": "cpis...", "mode": "tool"}
      ]
    }

- `tool_call_id` — ЕДИНСТВЕННЫЙ адрес, по которому движок способен что-то найти: в истории
  сообщений наших `Operation ID` нет, они живут только в ТЕКСТЕ ответа тула и в леджере.
  Обязателен.
- `op` — наш `Operation ID` из леджера. Для человека и для журнала. Не адрес.
  НИКОГДА не подставляем сырой `tool_call_id` в текст, который увидит модель, — только
  `op` (их же код делает эту ошибку, `Vision04.1`).
- `mode` — ЧТО ИМЕННО уходит. Значение — ОБЪЕКТ действия, тем же словом, каким действие
  названо на кнопке в панели (`desktop/src/core.js::CONTEXT_ACTIONS`). Схема имён
  `(глагол)(объект)` — решение владельца 2026-09-09, и объект в ней несёт главное: сколько
  уходит. Одно слово на все слои (кнопка, галочка, отчёт, этот файл) — переводить между ними
  не надо:
  - `content` (`drop content`) — механизм 1: аргументы вызова и результат заменяются нашей
    заглушкой, сами сообщения остаются на месте, заголовок остаётся модели;
  - `tool` (`erase tool`) — механизм 2: пары `tool_calls[i]` + её tool-строка нет вовсе.
    Единица — ПАРА, а не батч: инвариант провайдера («у каждого вызова есть результат и
    наоборот») сохраняется, если переписать массив `tool_calls` у строки ассистента. Опустел
    массив — ключ убирается целиком (не `[]`); опустел и текста у ассистента нет — уходит и
    сама строка (их же правило из `agent/message_sanitization.py`: сообщение с `tool_calls`
    или tool-результат не удаляют никогда, иначе сирота даёт HTTP 400, а опустевшее в остальном
    сообщение удаляют);
  - `turn` (`erase turn`) — механизм 3: весь мини-ход (assistant + ВСЕ его результаты).

  ПРЕЖНИЕ ИМЕНА БЫЛИ `stub`/`turn` и описывали МЕХАНИКУ («подставляем заглушку»), а не то, что
  видит человек. Переименовано 2026-09-09, пока это бесплатно: формат версии 1, пишет его
  только наша панель, и ничего долгоживущего в нём не лежит. Файл со старым `stub` будет
  отвергнут поэлементно С ПРИЧИНОЙ (`normalize_items`), а не применён как попало.

  РЕАЛИЗОВАНЫ все три (`turn` — 2026-09-09, `Plan14-Task06`). Отметка на ОДНОЙ операции
  уносит весь шаг: элемент по-прежнему называет одну операцию, а расширение до состава шага
  делает механизм, и добавленные адреса он возвращает отдельным списком (`widened`) — человек
  обязан видеть до нажатия, что уйдёт больше названного.

ПОЧЕМУ ЭТОТ МОДУЛЬ БЕЗ ЗАВИСИМОСТЕЙ ОТ НАШЕГО ПАКЕТА. Его читают из двух разных миров:
движок — как часть пакета плагина, а `dashboard/plugin_api.py` — как модуль, загруженный
Хермесом ПО ПУТИ (`spec_from_file_location`, вне пакета), где относительный импорт
невозможен в принципе. Поэтому здесь только абсолютные импорты стандартной библиотеки и
`hermes_constants`, а санитайзер имени сессии продублирован из `file_ops/_ledger.py`
осознанно — с тестом, который сверяет обе копии на неудобных именах, чтобы расхождение
падало громко, а не приводило к двум разным файлам на одну сессию.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

VERSION = 1


def _home_module():
    """`<корень плагина>/_home.py` — ОДНО правило дома плагина. Пакетом — относительный импорт;
    панель грузит этот файл ПО ПУТИ (пакета нет) — тогда тот же `_home.py` по пути, один на процесс."""
    try:
        from .. import _home
        return _home
    except ImportError:
        import importlib.util
        import sys
        mod = sys.modules.get("hermes_filetools_home")
        if mod is None:
            path = Path(__file__).resolve().parents[1] / "_home.py"
            spec = importlib.util.spec_from_file_location("hermes_filetools_home", path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["hermes_filetools_home"] = mod
            spec.loader.exec_module(mod)
        return mod

# Значения `mode` — ОБЪЕКТЫ действия, теми же словами, что на кнопках панели. Подробно — в
# шапке файла. `MODE_CONTENT` первый ещё и потому, что он же default для элемента без `mode`:
# единственный реализованный механизм — единственный безопасный default.
MODE_CONTENT = "content"
MODE_TOOL = "tool"
MODE_TURN = "turn"
MODES = (MODE_CONTENT, MODE_TOOL, MODE_TURN)

# Что из этого УМЕЕТ применяться. Формат принимает все три, чтобы не меняться под каждый новый
# механизм, но применить пока можно только первый. Список отдельный, а не «всё, что в MODES»,
# именно для того, чтобы неподдержанный режим отказал ПО ИМЕНИ, а не выполнился как `content`:
# сделать вместо `erase tool` заглушку значило бы выполнить не ту операцию, которую выбрал
# человек, и притом необратимую.
MODES_IMPLEMENTED = (MODE_CONTENT, MODE_TOOL, MODE_TURN)

# Потолки на один файл выбора. Не защита от злоумышленника (маршрут за той же
# авторизацией, что и остальная приборная панель) — защита от опечатки в клиенте,
# которая иначе запишет на диск мегабайты и заставит движок их разбирать на каждом
# применении.
MAX_ITEMS = 2000
MAX_ID_CHARS = 256

# Копия правила из `file_ops/_ledger.py::_safe_session` — см. шапку модуля.
# `tests/test_context_ctl.py::test_session_sanitizer_matches_the_ledgers` держит их вместе.
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
UNKNOWN_SESSION = "_unknown"


def _safe_session(session: str | None) -> str:
    name = _SAFE_NAME.sub("_", (session or "").strip())
    name = name.lstrip(".")
    return name or UNKNOWN_SESSION


# ── Открытая сессия ────────────────────────────────────────────────────────────────────
#
# ЗАЧЕМ. Слеш-команда получает от Hermes ТОЛЬКО строку аргументов, поэтому «в каком чате её
# набрали» приходилось откуда-то доставать. Сначала мы угадывали по активности («где
# последней отвечала модель») — на локальном провайдере без `usage` это слепо. Потом я
# подменял их диспетчер, чтобы перехватить `params["session_id"]`, и владелец справедливо
# сказала, что патчить гейтвей ради номера сессии — бред: ПАНЕЛЬ его и так знает, она сама
# подставляет его в `/ops?session=...` и в файл выбора.
#
# Поэтому панель просто записывает, какой чат открыт, а команда это читает. Обмен «панель
# пишет — движок читает» здесь уже есть, направление то же, что у выбора, — новый модуль не
# нужен.
#
# ЧЕСТНАЯ ЦЕНА. Это «сессия, открытая в панели», а не «сессия, из которой пришёл вызов».
# В десктопе они совпадают по построению (панель следует за сфокусированным чатом), но это
# СВИДЕТЕЛЬСТВО, а не доказательство. Поэтому выбранная сессия по-прежнему НАЗЫВАЕТСЯ в
# ответе команды, а `--session` остаётся способом переспорить. Взамен из системы уходит
# монкипатч на чужой функции — обмен того стоит.
#
# Файл ОДИН, не по сессии: вопрос «какой чат открыт» global по своей природе.


def focus_path() -> Path:
    return select_dir() / "focus.json"


def read_focus() -> dict:
    """`{"session": str, "at": float}`. Файла нет или битый — пустая сессия, не исключение."""
    try:
        data = json.loads(focus_path().read_text(encoding="utf-8"))
    except Exception:
        return {"session": "", "at": 0.0}
    if not isinstance(data, dict):
        return {"session": "", "at": 0.0}
    return {"session": str(data.get("session") or ""),
            "at": float(data.get("at") or 0.0)}


def write_focus(session: str | None) -> dict:
    """Записать открытый чат. Пустая сессия — законный ввод: панель могла потерять фокус."""
    payload = {"version": VERSION, "session": str(session or ""), "at": time.time()}
    try:
        path = focus_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        return {"session": "", "at": 0.0}
    return payload


def select_dir() -> Path:
    """Папка файлов выбора. Отдельная от статусов движка, и это не косметика.

    Статус пишет ДВИЖОК и читают все; выбор пишет ПАНЕЛЬ и читает движок. Два разных
    направления записи в один файл — верный способ однажды затереть чужое.
    """
    return _home_module().plugin_home() / "ctxctl" / "select"


def select_path(session: str | None) -> Path:
    return select_dir() / f"{_safe_session(session)}.json"


def empty(session: str | None = "") -> dict:
    """Пустой выбор. ЛЕГАЛЬНОЕ состояние, а не ошибка: файла может не быть никогда."""
    return {"version": VERSION, "session": session or "", "updated_at": 0.0, "items": []}


def normalize_items(raw) -> tuple[list, list]:
    """`(принятые, отвергнутые с причиной)`. Ничего не бросает — маршруту нужен ответ.

    Отвергнутое НЕ отбрасывается молча: клиент, отправивший мусор, обязан узнать об этом
    из ответа, иначе человек будет смотреть на чекбокс, которого для сервера не существует.
    """
    accepted: list[dict] = []
    rejected: list[dict] = []
    if not isinstance(raw, list):
        return [], [{"item": None, "why": "items должен быть списком"}]

    seen: dict[str, int] = {}
    for entry in raw[:MAX_ITEMS]:
        if not isinstance(entry, dict):
            rejected.append({"item": entry, "why": "элемент должен быть объектом"})
            continue
        call_id = entry.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id.strip():
            rejected.append({"item": entry, "why": "нет tool_call_id — адресовать нечем"})
            continue
        call_id = call_id.strip()
        if len(call_id) > MAX_ID_CHARS:
            rejected.append({"item": entry, "why": "tool_call_id неправдоподобно длинный"})
            continue
        mode = entry.get("mode", MODE_CONTENT)
        if mode not in MODES:
            rejected.append({"item": entry, "why": f"mode должен быть одним из {MODES}"})
            continue
        if mode not in MODES_IMPLEMENTED:
            # Отказ ПО ИМЕНИ, а не тихая подмена на `content`: механизм ещё не написан, и
            # выполнить вместо него другую — необратимую — операцию было бы хуже отказа.
            rejected.append({"item": entry,
                             "why": f"mode {mode!r} формат принимает, но механизм ещё не "
                                    f"реализован; умеем пока {MODES_IMPLEMENTED}"})
            continue
        op = entry.get("op")
        op = op.strip() if isinstance(op, str) else ""
        item = {"tool_call_id": call_id, "mode": mode, "op": op[:MAX_ID_CHARS]}
        # Один и тот же вызов, отмеченный дважды, — не ошибка клиента, а обычное
        # перещёлкивание чекбокса. Побеждает ПОСЛЕДНЕЕ: это и есть то, что человек
        # видит на экране в момент отправки.
        if call_id in seen:
            accepted[seen[call_id]] = item
        else:
            seen[call_id] = len(accepted)
            accepted.append(item)

    if isinstance(raw, list) and len(raw) > MAX_ITEMS:
        rejected.append({"item": None,
                         "why": f"принято только первые {MAX_ITEMS} из {len(raw)}"})
    return accepted, rejected


def read_selection(session: str | None) -> dict:
    """Текущий выбор. Всегда корректная форма — нет файла или битый файл = пустой выбор.

    Битый файл не поднимает шума наверх осознанно: применение компакции не должно
    падать из-за недописанного JSON, а «ничего не выбрано» — безопасный ответ по
    умолчанию (не выбросим лишнего).
    """
    try:
        data = json.loads(select_path(session).read_text(encoding="utf-8"))
    except Exception:
        return empty(session)
    if not isinstance(data, dict):
        return empty(session)
    items, _rejected = normalize_items(data.get("items"))
    return {
        "version": VERSION,
        "session": str(data.get("session") or session or ""),
        "updated_at": float(data.get("updated_at") or 0.0),
        "items": items,
    }


def write_selection(session: str | None, raw_items) -> tuple[dict, list]:
    """Заменить выбор целиком. Возвращает `(что записано, что отвергнуто)`.

    ЗАМЕНА, не слияние: на экране у человека — полный набор чекбоксов, и он присылает
    именно его. Слияние означало бы, что снятый чекбокс не снимается.
    """
    items, rejected = normalize_items(raw_items)
    payload = {"version": VERSION, "session": session or "",
               "updated_at": time.time(), "items": items}
    path = select_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return payload, rejected


def clear_selection(session: str | None) -> dict:
    """Опустошить выбор. Именно записать пустой, а не удалить файл.

    Пустой файл и отсутствующий читаются одинаково, но записанный пустой несёт
    `updated_at` — то есть «здесь сознательно ничего не выбрано СЕЙЧАС», что после
    применения компакции отличимо от «панель ни разу не присылала выбор».
    """
    payload, _ = write_selection(session, [])
    return payload
