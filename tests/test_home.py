"""Дом плагина и слой настроек пользователя — `_home.py` (2026-09-30).

  H1  дом — `<HERMES_HOME>/filetools` по их `get_hermes_home()`; без Hermes — `HERMES_HOME`
  H2  все семь путей данных — под домом (журнал, служебные файлы движка, токены, выгрузка)
  H3  слияние: словари вглубь, списки и значения — заменяются; `diff` — только отличия
  H4  `settings.yaml` пишется читаемым и читается обратно; битый файл — живём на умолчаниях
  H5  длинный путь с обратными слешами переживает запись (их эмиттер не складывает строку)
  H6  зашитых путей (`Y:\\Hermess`) в коде плагина нет
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from hermes_filetools import _home

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    fake = types.ModuleType("hermes_constants")
    fake.get_hermes_home = lambda: str(tmp_path / "hh")
    monkeypatch.setitem(sys.modules, "hermes_constants", fake)
    return tmp_path / "hh"


def test_h1_home(fake_home, monkeypatch):
    assert _home.plugin_home() == fake_home / "filetools"
    assert _home.settings_path() == fake_home / "filetools" / "settings.yaml"
    monkeypatch.setitem(sys.modules, "hermes_constants", None)   # import -> ImportError
    monkeypatch.setenv("HERMES_HOME", str(fake_home / "env"))
    assert _home.plugin_home() == fake_home / "env" / "filetools"


def test_h2_every_data_dir_is_under_home(fake_home):
    from hermes_filetools.context_ctl import (_compact_state, _export, _select, _signal, _state,
                                              _tokens)
    from hermes_filetools.file_ops import _ledger
    base = fake_home / "filetools"
    assert _ledger.ledger_dir() == base / "ledger"
    assert _state.state_dir() == base / "ctxctl"
    assert _select.select_dir() == base / "ctxctl" / "select"
    assert _signal.signal_dir() == base / "ctxctl" / "applied"
    assert _compact_state.state_dir() == base / "ctxctl" / "compact_state"
    assert _tokens.tokens_dir() == base / "tokens"
    assert _export.export_dir("") == base / "export"


def test_h3_merge_and_diff():
    base = {"tools": {"a": {"enabled": True, "params": {"x": 1, "y": 2}}},
            "disabled_tools": ["p", "q"], "context_ctl": {"k": 1}}
    over = {"tools": {"a": {"params": {"y": 5}}}, "disabled_tools": ["r"]}
    eff = _home.merged(base, over)
    assert eff["tools"]["a"] == {"enabled": True, "params": {"x": 1, "y": 5}}
    assert eff["disabled_tools"] == ["r"] and eff["context_ctl"] == {"k": 1}
    assert _home.diff(eff, base) == over


def test_h4_settings_round_trip_and_broken_file(fake_home):
    _home.save_settings({"context_ctl": {"compact_head_keep": 9}, "disabled_tools": ["x"]})
    text = _home.settings_path().read_text(encoding="utf-8")
    assert text.startswith("# hermes-filetools - YOUR settings")
    assert _home.user_settings() == {"context_ctl": {"compact_head_keep": 9}, "disabled_tools": ["x"]}
    _home.settings_path().write_text("a: [1, 2\nb: {", encoding="utf-8")
    assert _home.user_settings() == {}, "битый файл — умолчания, не падение"


def test_h5_long_windows_path_survives(fake_home):
    long_path = "D:\\" + "\\".join(["очень_длинная_папка_" + str(i) for i in range(12)])
    _home.save_settings({"context_ctl": {"compact_export_root": long_path}})
    assert _home.user_settings()["context_ctl"]["compact_export_root"] == long_path


def test_h6_no_hardcoded_paths():
    bad = []
    for path in list(ROOT.glob("*.py")) + list((ROOT / "context_ctl").rglob("*.py")) \
            + list((ROOT / "file_ops").rglob("*.py")) + list((ROOT / "dashboard").glob("*.py")) \
            + list((ROOT / "seams").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        if 'r"Y:' in code or "r'Y:" in code or '"Y:\\\\' in code:
            bad.append(path.name)
    assert bad == [], f"зашитый путь Y: в {bad}"
