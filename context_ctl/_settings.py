"""Наши настройки компакции — ДЕЙСТВУЮЩИЕ: умолчания `plugin.yaml` + настройки пользователя.

Панель правит настройки на лету (`/config`), и решения агента (порог автомата, отбор) не должны ждать
перезапуска клиента. Секция `context_ctl` приходит в `setup()` один раз при загрузке — её мало.

С 2026-09-30 два слоя (`_home.py`): `plugin.yaml` — УМОЛЧАНИЯ (едет релизом и деплоем, ими же
перезаписывается), `<HERMES_HOME>/filetools/settings.yaml` — то, что поменял человек (туда пишет
панель). Умолчания читаем регэкспом по mtime (ключи — плоские числа/флаги/строки своей секции),
настройки — поверх, их секция `context_ctl`. Значения по умолчанию — те же, что у
`dashboard/plugin_api.py::read_config`.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


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


DEFAULTS = {
    "compact_head_keep": 6,
    "compact_tail_keep": 20,
    "compact_cut_percent": 30,
    "compact_auto_percent": 70,
    "compact_auto_enabled": False,
    # После двух неудач автокомпакции подряд — пауза, минут (Plan16 п.1, задача C).
    "compact_auto_pause_minutes": 3,
    # Нет ни одного шага прогона дольше этого — строка хода в панели желтеет «возможно, завис».
    # Прогон НЕ убиваем: один вопрос на большой сессии идёт минуты (`deferred/10`, 2026-09-30).
    "compact_silence_minutes": 5,
    # Ручная компакция: запись отбита, потому что идёт ход, — ждём его конца не дольше стольких
    # минут и пишем по свежей ленте; 0 — не ждать (2026-09-30).
    "compact_wait_turn_minutes": 10,
    # С каким сервером говорит опрос: `auto` — по адресу, или вид из `_server_kind.KINDS`
    # (llamacpp / vllm / gemini / openai / openrouter / plain) — 2026-09-30.
    "compact_server": "auto",
    # Маршрут опроса (`Doc__interrogation-route.md`): `session` — модель живого агента сессии,
    # `own` — наш маршрут из трёх ключей ниже; ключ — ИМЯ переменной окружения, не сам ключ.
    "compact_route": "session",
    "compact_route_base_url": "",
    "compact_route_model": "",
    "compact_route_key_env": "",
}

_cache = {"key": None, "value": dict(DEFAULTS)}


def plugin_yaml() -> Path:
    return Path(__file__).resolve().parent.parent / "plugin.yaml"


def _mtime(path) -> float | None:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _coerce(default, value):
    """Значение из YAML — к типу умолчания; негодное — None (остаётся прежнее)."""
    if isinstance(default, bool):
        return value if isinstance(value, bool) else str(value).strip().lower() == "true"
    if isinstance(default, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return "" if value is None else str(value)


def _user_stamp():
    """mtime `settings.yaml` пользователя; None — файла нет (или нет дома Hermes)."""
    try:
        return _mtime(_home_module().settings_path())
    except Exception:  # noqa: BLE001 - голый python без Hermes — живём на умолчаниях
        return None


def _user_section() -> dict:
    """Секция `context_ctl` настроек пользователя; нет — `{}`."""
    if _user_stamp() is None:
        return {}
    section = _home_module().user_settings().get("context_ctl")
    return section if isinstance(section, dict) else {}


def compaction(path: Path | None = None) -> dict:
    """Ключи `compact_*`: умолчания `plugin.yaml`, поверх — `settings.yaml`; нет ключа — DEFAULTS."""
    path = path or plugin_yaml()
    stamp = _mtime(path)
    key = (str(path), stamp, _user_stamp())
    if _cache["key"] == key:
        return dict(_cache["value"])
    user = _user_section()
    out = dict(DEFAULTS)
    if stamp is not None:
        text = path.read_text(encoding="utf-8")
        for name, default in DEFAULTS.items():
            # `[ \t]*`, не `\s*`: пустое значение не должно дотянуться до следующей строки.
            m = re.search(rf"^\s+{name}:[ \t]*([^\s#]+)", text, re.M)
            if not m:
                continue
            raw = m.group(1)
            if isinstance(default, str):
                out[name] = raw.strip("\"'")  # регистр не трогаем: адрес, модель, имя переменной
            elif isinstance(default, bool):
                out[name] = raw.lower() == "true"
            else:
                try:
                    out[name] = int(raw)
                except ValueError:
                    pass
    for name, default in DEFAULTS.items():
        if name in user:
            value = _coerce(default, user[name])
            if value is not None:
                out[name] = value
    _cache.update(key=key, value=dict(out))
    return out


def export_root(path: Path | None = None) -> str:
    """`compact_export_root` — куда выгружать; пустая строка = дом плагина по умолчанию.
    Настройка пользователя сильнее умолчания `plugin.yaml`."""
    user = _user_section()
    if "compact_export_root" in user:
        return str(user.get("compact_export_root") or "").strip()
    path = path or plugin_yaml()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r'^\s+compact_export_root:\s*"?([^"#\n]*)"?', text, re.M)
    return m.group(1).strip() if m else ""
