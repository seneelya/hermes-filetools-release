"""Сигнал «контекст изменён» из процесса агента в процесс панели.

ЗАЧЕМ ОТДЕЛЬНЫЙ КАНАЛ. Панель узнаёт об изменениях двумя триггерами: наша операция (по
`tool.complete`) и ИХ событие `status.update` с `kind: "compacted"`. Мы сознательно ушли из
их конвейера компакции — значит их события нет, и после `/ctx-drop` панель показывала
устаревший снимок до перезапуска Hermes. Владелец увидела это живьём 2026-09-09: содержимое
ушло из промпта модели, а стакан остался зелёным.

Подделывать их событие нельзя (мы не компакция), а прямого пути нет: команда исполняется в
процессе АГЕНТА, а маршруты панели смонтированы в веб-сервере дашборда — у них разные
процессы, что и подтверждает существование `_ledger_dir_or_none()` (он ловит расхождение
домов). Общая между процессами у нас одна вещь — файловое хозяйство, та самая
«псевдобаза данных», и через неё уже ходит выбор (`_select.py`).

ПОЧЕМУ НЕ В СТАТУС-ФАЙЛ ДВИЖКА. Статус пишется на КАЖДЫЙ ответ модели, поэтому его mtime
меняется постоянно — панель перечитывала бы всё непрерывно. Здесь пишется только успешное
применение, поэтому смена ревизии означает ровно то, что означает.

Зависимостей, кроме `hermes_constants`, нет НАРОЧНО: этот модуль грузится ПО ПУТИ из
`dashboard/plugin_api.py`, где он не является частью пакета. Относительных импортов быть не
может.
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

# Копия правила из `file_ops/_ledger.py::_safe_session`, как и в `_select.py`, и по той же
# причине — этот модуль обязан грузиться в одиночку. Держатся вместе тестом
# `tests/test_context_ctl.py::test_session_sanitizer_matches_the_ledgers`.
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
UNKNOWN_SESSION = "_unknown"


def _safe_session(session: str | None) -> str:
    name = _SAFE_NAME.sub("_", (session or "").strip())
    name = name.lstrip(".")
    return name or UNKNOWN_SESSION


def signal_dir() -> Path:
    """Папка сигналов. Своя, потому что направление записи третье.

    Статус пишет движок для человека, выбор пишет панель для движка, сигнал пишет движок
    для панели. Три направления в одном файле — верный способ однажды затереть чужое.
    """
    return _home_module().plugin_home() / "ctxctl" / "applied"


def signal_path(session: str | None) -> Path:
    return signal_dir() / f"{_safe_session(session)}.json"


def empty(session: str | None = "") -> dict:
    """Ничего не применяли. ЛЕГАЛЬНОЕ состояние: файла может не быть никогда."""
    return {"version": VERSION, "session": session or "", "revision": 0,
            "applied_at": 0.0, "ops": []}


def read_signal(session: str | None) -> dict:
    """Последнее применение. Битый или отсутствующий файл — это `empty()`, не исключение."""
    try:
        data = json.loads(signal_path(session).read_text(encoding="utf-8"))
    except Exception:
        return empty(session)
    if not isinstance(data, dict):
        return empty(session)
    out = empty(session)
    out.update({k: v for k, v in data.items() if k in out})
    try:
        out["revision"] = int(out["revision"])
    except Exception:
        out["revision"] = 0
    return out


def bump(session: str | None, ops) -> dict:
    """Отметить успешное применение. Ревизия строго растёт; ошибка записи не роняет вызов.

    Ревизия, а не только время: панель сравнивает ЧИСЛО и потому не зависит ни от часов
    («clocks regress» — их же формулировка про порядок строк), ни от разрешения mtime.
    """
    current = read_signal(session)
    payload = {
        "version": VERSION,
        "session": session or "",
        "revision": int(current.get("revision") or 0) + 1,
        "applied_at": time.time(),
        "ops": [str(op) for op in (ops or []) if str(op or "").strip()],
    }
    try:
        path = signal_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                       encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        return current
    return payload
