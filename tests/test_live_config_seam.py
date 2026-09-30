"""Обратный шов: их живое применение `compression.*` к нашему движку (ACHTUNG
`ctxctl.live_config_threshold_cap`, 2026-09-30).

  W1  их `_apply_live_compression_config` проходит на нашем движке до конца, без исключения;
      потолок порога — `None` («потолка нет»)
  W2  без нашего `_coerce_threshold_tokens_cap` их функция падает — значит, они его ещё зовут
      (перестанут — тест покраснеет, и заглушку можно убрать)
"""
from __future__ import annotations

import types

import pytest

from hermes_filetools.context_ctl.engine import ContextCtlEngine

session_compression = pytest.importorskip("tui_gateway.session_compression")

CFG = {"compression": {"threshold": 0.7, "protect_last_n": 12}}


def _agent():
    engine = ContextCtlEngine(plugin_version="test")
    agent = types.SimpleNamespace(model="M-1", provider="custom", base_url="http://srv:5000/v1",
                                  context_compressor=engine)
    return agent, engine


def test_w1_their_live_apply_runs_through(monkeypatch):
    monkeypatch.setattr("agent.agent_init.config_context_length_for_runtime", lambda a, c: None,
                        raising=False)
    agent, engine = _agent()
    session_compression._apply_live_compression_config(agent, CFG)
    assert engine.threshold_tokens_cap is None
    assert engine._threshold_tokens is None, "их последняя строка дошла"


def test_w2_they_still_call_it(monkeypatch):
    monkeypatch.setattr("agent.agent_init.config_context_length_for_runtime", lambda a, c: None,
                        raising=False)
    monkeypatch.delattr(ContextCtlEngine, "_coerce_threshold_tokens_cap")
    agent, _ = _agent()
    with pytest.raises(AttributeError, match="_coerce_threshold_tokens_cap"):
        session_compression._apply_live_compression_config(agent, CFG)
