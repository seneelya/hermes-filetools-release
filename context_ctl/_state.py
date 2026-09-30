"""Файл-состояние движка: куда пишем и как читаем.

ЗАЧЕМ ФАЙЛ, А НЕ АТРИБУТЫ ДВИЖКА. Зарегистрированный движок — синглтон, но каждому агенту
Hermes отдаёт его `copy.deepcopy` (`agent_init.py::_select_context_engine`, «Deep-copy the
shared singleton so a child's update_model() can't mutate the parent's»). Значит атрибут,
записанный в живой сессии, не виден ни другой сессии, ни маршруту панели, ни слеш-команде:
у них на руках другая копия. Всё, что должно быть видно СНАРУЖИ сессии, обязано лежать в
файле — так же, как леджер (`file_ops/_ledger.py`), и в той же папке-хозяйстве.

Пишем атомарно (tmp + replace) и никогда не роняем вызывающего: статус — наблюдаемость, а
не работа. Пустой/битый файл — легальное состояние, читатель обязан это выдержать.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ..file_ops._ledger import _safe_session


def when(ts=None) -> str:
    """Время в виде, который читает ЧЕЛОВЕК: `2026-09-09 03:34:30`.

    Файл существует ради того, чтобы человек мог узнать исход необратимой правки после
    того, как эхо в чате исчезло. Эпоха-float (`1788914070.4533167`) для этого негодна:
    её нельзя прочитать глазами, а машинам этот файл не нужен — панель читает счётчик
    ревизии из `_signal.py`, а не отсюда.
    """
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(
            time.time() if ts is None else float(ts)))
    except Exception:
        return ""


def state_dir() -> Path:
    """Папка состояния движка. Резолвится на каждом вызове — как `_ledger.ledger_dir()`.

    `get_hermes_home()` контекстно-локален и переопределяем (тесты и профили это делают),
    поэтому модульная константа запомнила бы первый увиденный дом.
    """
    from .. import _home
    return _home.plugin_home() / "ctxctl"


def status_path(session: str | None) -> Path:
    return state_dir() / f"{_safe_session(session)}.json"


def write_status(session: str | None, payload: dict) -> Path | None:
    """Записать статус сессии. Возвращает путь либо None, если не получилось."""
    try:
        path = status_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        # sort_keys НЕ ставим: алфавит рассыпает осмысленный порядок, который собрал
        # `engine.status()` (кто мы → какая сессия → мост → токены → журнал правок), и
        # файл начинает читаться как выгрузка, а не как ответ на вопрос.
        body = json.dumps({**payload, "written_at": when()},
                          ensure_ascii=False, indent=2)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)
        return path
    except Exception:
        return None


def read_status(session: str | None) -> dict | None:
    try:
        return json.loads(status_path(session).read_text(encoding="utf-8"))
    except Exception:
        return None


def latest_status() -> tuple[str, dict] | None:
    """Самый свежий статус-файл: `(имя файла, содержимое)`.

    Нужен слеш-команде: обработчик команды получает только строку аргументов, без
    `session_id`, поэтому «текущую» сессию он может опознать лишь по времени записи.
    """
    try:
        files = sorted(state_dir().glob("*.json"), key=lambda p: p.stat().st_mtime,
                       reverse=True)
    except Exception:
        return None
    for path in files:
        try:
            return path.name, json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
    return None
