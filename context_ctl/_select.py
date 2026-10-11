"""Фокус: какой чат сейчас открыт в панели. Пишет панель, читает слеш-команда.

Прежде здесь жил ещё ФАЙЛ ВЫБОРА — что человек отметил галочками в стакане операций
(`POST /context-select`, `{op, tool_call_id, mode}`), и по нему работали `POST /context-apply`
и пустой `/ctx-drop`. Снят 2026-10-06 (Plan23, владелец: «транспорт с сервером один»): операции
над контекстом идут только m-адресами (`POST /context-operation` → `_command.run_addresses`),
выбор живёт в панели (рельса) и уезжает в теле запроса, а не в файле.

ПОЧЕМУ ЭТОТ МОДУЛЬ БЕЗ ЗАВИСИМОСТЕЙ ОТ НАШЕГО ПАКЕТА. Его читают из двух разных миров:
движок — как часть пакета плагина, а `dashboard/plugin_api.py` — как модуль, загруженный
Хермесом ПО ПУТИ (`spec_from_file_location`, вне пакета), где относительный импорт
невозможен в принципе. Поэтому здесь только стандартная библиотека и `_home.py` по пути.
"""

from __future__ import annotations

import json
import os
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


# ── Открытая сессия ────────────────────────────────────────────────────────────────────
#
# ЗАЧЕМ. Слеш-команда получает от Hermes ТОЛЬКО строку аргументов, поэтому «в каком чате её
# набрали» приходилось откуда-то доставать. Сначала мы угадывали по активности («где
# последней отвечала модель») — на локальном провайдере без `usage` это слепо. Потом я
# подменял их диспетчер, чтобы перехватить `params["session_id"]`, и владелец справедливо
# сказала, что патчить гейтвей ради номера сессии — бред: ПАНЕЛЬ его и так знает, она сама
# подставляет его в каждый свой запрос.
#
# Поэтому панель просто записывает, какой чат открыт, а команда это читает.
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
    """Папка файла фокуса (имя `select` историческое — путь не меняем, файл уже лежит у
    пользователей). Отдельная от статусов движка: статус пишет ДВИЖОК, фокус — ПАНЕЛЬ; два
    направления записи в одну папку — верный способ однажды затереть чужое."""
    return _home_module().plugin_home() / "ctxctl" / "select"
