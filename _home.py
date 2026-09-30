"""Дом плагина — `<HERMES_HOME>/filetools` — ОДНО место, где это правило записано (2026-09-30).

Раньше путь собирали семь модулей сами (`get_hermes_home() / "filetools" / …`), а счётчик токенов
ещё и падал в зашитый `Y:\\Hermess\\body` — у клиента такого диска нет. Теперь все спрашивают здесь.

Разрешается на КАЖДОМ вызове, не кэшируется: `get_hermes_home()` контекстно-локален и
переопределяем (тесты, профили). Без внутренних импортов плагина: модули, которые панель грузит
ПО ПУТИ (вне пакета), подключают этот файл тоже по пути (свой короткий `_home_module()`: сначала
относительный импорт, не вышло — `<корень плагина>/_home.py` под именем `hermes_filetools_home`).

Что лежит в доме: `ledger/` (журнал), `ctxctl/` (служебные файлы движка), `tokens/` (файл
моделей), `export/` (выгрузка по умолчанию), `settings.yaml` (НАСТРОЙКИ ПОЛЬЗОВАТЕЛЯ — поверх
умолчаний `plugin.yaml`; обновление плагина их не трогает).
"""
from __future__ import annotations

import os
from pathlib import Path

SETTINGS_FILE = "settings.yaml"


def hermes_home() -> Path:
    """Дом Hermes: их `get_hermes_home()`; вне Hermes (зонд, голый python) — `HERMES_HOME`."""
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except ImportError:
        env = os.environ.get("HERMES_HOME", "").strip()
        if not env:
            raise RuntimeError("Hermes home unknown: no hermes_constants and no HERMES_HOME") from None
        return Path(env).expanduser()


def plugin_home() -> Path:
    """`<HERMES_HOME>/filetools` — все наши данные живут под ним."""
    return hermes_home() / "filetools"


def settings_path() -> Path:
    """Файл настроек пользователя — то, что панель пишет вместо `plugin.yaml`."""
    return plugin_home() / SETTINGS_FILE


# ------------------------------------------------------------- настройки пользователя поверх умолчаний
#
# `plugin.yaml` едет релизом и деплоем и потому — УМОЛЧАНИЯ: `hermes plugins update` (git pull) и
# наш деплой его перезаписывают. Всё, что человек поменял в панели, живёт в `settings.yaml` дома
# плагина — только отличия от умолчаний. Читатель видит «действующее»: умолчания, поверх них
# настройки. Словари сливаются вглубь; списки и значения — заменяются целиком.

def load_yaml(path) -> dict:
    """YAML-файл словарём (их `hermes_yaml`); нет файла — `{}`."""
    path = Path(path)
    if not path.is_file():
        return {}
    from hermes_yaml import safe_load
    with open(path, "r", encoding="utf-8") as fh:
        data = safe_load(fh) or {}
    return data if isinstance(data, dict) else {}


def user_settings() -> dict:
    """`settings.yaml` пользователя; нет или не читается — `{}` (живём на умолчаниях)."""
    try:
        return load_yaml(settings_path())
    except Exception:  # noqa: BLE001 - битый файл настроек не должен ронять плагин
        return {}


def merged(base, over):
    """Настройки поверх умолчаний: словари — вглубь, остальное — заменяется."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for key, value in over.items():
            out[key] = merged(base.get(key), value)
        return out
    return over


def effective(manifest_path) -> dict:
    """Действующая конфигурация: `plugin.yaml` + `settings.yaml`."""
    return merged(load_yaml(manifest_path), user_settings())


def diff(current, base) -> dict:
    """Чем `current` отличается от `base` — ровно то, что пишется в `settings.yaml`."""
    out = {}
    for key, value in (current or {}).items():
        was = base.get(key) if isinstance(base, dict) else None
        if isinstance(value, dict) and isinstance(was, dict):
            sub = diff(value, was)
            if sub:
                out[key] = sub
        elif value != was:
            out[key] = value
    return out


def save_settings(data: dict) -> Path:
    """Записать `settings.yaml` целиком (атомарно, через временный файл рядом)."""
    from hermes_yaml import safe_dump
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write("# hermes-filetools - YOUR settings (the Config panel writes this file).\n"
                 "# Only differences from the plugin's defaults (plugin.yaml). Delete a key to\n"
                 "# go back to the default; plugin updates never touch this file.\n")
        # Ширина без переносов: их эмиттер, складывая длинную строку после экранированного
        # обратного слеша, меняет значение (их комментарий у `ROUNDTRIP_YAML_WIDTH`) — а путь
        # выгрузки на Windows ровно такой.
        safe_dump(data, fh, allow_unicode=True, sort_keys=False, width=2**31 - 1)
    tmp.replace(path)
    return path
