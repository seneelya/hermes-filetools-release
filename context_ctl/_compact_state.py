"""Ход ФОНОВОГО прогона компакции, из процесса агента в процесс панели (Plan16-Task03).

ЗАЧЕМ ОТДЕЛЬНЫЙ ФАЙЛ, А НЕ `_state_now` ИЗ `_compact.py`. Компакция с панели идёт минутами
в отдельном потоке, и панель опрашивает её ход маршрутом `GET /context-compact-status`. Но
`_compact._state_now` — обычный модульный глобал ВНУТРИ пакета: агентская половина видит его
(команда `/ctx-compact status` уже так и работает), а дашбордная половина `_compact.py`
обычным импортом не достаёт вовсе (`dashboard/plugin_api.py` грузится ПО ПУТИ, вне пакета —
тот же барьер, что решают `_select.py`/`_signal.py`). Даже импорт `_compact.py` по пути дал
бы ВТОРОЙ, независимый экземпляр модуля со своим собственным `_state_now` — не тот объект,
который правит поток агентской половины. Файл — единственное, что видно обеим половинам.

ПОЧЕМУ НЕ `_signal.py`. Тот пишет ревизию ПОСЛЕ успешного применения (одна запись на удачный
`/ctx-drop`), и его смысл — «что-то поменялось, перечитай». Этот файл пишется НА КАЖДОМ шаге
фонового прогона (старт / конец / ошибка), и его смысл другой — «прогон ещё идёт или уже
закончен, и чем». Путать направления — верный способ однажды затереть чужую запись не туда.

Зависимостей, кроме `hermes_constants`, нет НАРОЧНО — как и у `_signal.py`/`_select.py`,
относительные импорты здесь невозможны в принципе.
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

# Та же копия правила, что в `_signal.py`/`_select.py` — держатся вместе тем же тестом.
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
UNKNOWN_SESSION = "_unknown"


def _safe_session(session: str | None) -> str:
    name = _SAFE_NAME.sub("_", (session or "").strip())
    name = name.lstrip(".")
    return name or UNKNOWN_SESSION


def state_dir() -> Path:
    """Своя папка — не `applied` (`_signal.py`) и не `select` (`_select.py`): третье
    направление записи, третье имя."""
    return _home_module().plugin_home() / "ctxctl" / "compact_state"


def state_path(session: str | None) -> Path:
    return state_dir() / f"{_safe_session(session)}.json"


def empty(session: str | None = "") -> dict:
    """Прогонов ещё не было. ЛЕГАЛЬНОЕ состояние: файла может не быть никогда."""
    return {"version": VERSION, "session": session or "", "running": False,
            "ok": False, "started": "", "error": "", "lines": [],
            # Ход прогона (`_progress.py`, 2026-09-30): кто, что, этап, вопрос k/N, пульс.
            "who": "", "span": "", "stage": "", "step": 0, "total": 0, "label": "",
            "topic": "", "started_at": 0.0, "beat": 0.0}


def read_state(session: str | None) -> dict:
    """Ход прогона. Битый или отсутствующий файл — это `empty()`, не исключение и не 500."""
    try:
        data = json.loads(state_path(session).read_text(encoding="utf-8"))
    except Exception:
        return empty(session)
    if not isinstance(data, dict):
        return empty(session)
    out = empty(session)
    out.update({k: v for k, v in data.items() if k in out})
    if isinstance(data.get("draft"), dict):
        out["draft"] = data["draft"]  # превью (`Plan35`) — только когда есть
    out["running"] = bool(out["running"])
    out["ok"] = bool(out["ok"])
    out["lines"] = [str(x) for x in (out.get("lines") or [])]
    return out


def write_state(session: str | None, payload: dict) -> dict:
    """Записать ход прогона. Ошибка записи не роняет вызывающий поток — потерянная запись
    о ходе прогона хуже, чем упавший фоновый прогон компакции."""
    data = empty(session)
    data.update({k: v for k, v in (payload or {}).items() if k in data})
    if isinstance((payload or {}).get("draft"), dict):
        data["draft"] = payload["draft"]  # превью (`Plan35`): нет — ключа нет, файл прежний
    data["version"] = VERSION
    data["session"] = session or ""
    try:
        path = state_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
                       encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass
    return data
