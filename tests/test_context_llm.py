"""Дверь к модели — маршрут сессии (`_llm`, 2026-09-30).

  L1  без маршрута и с плохим маршрутом модель не зовётся: отказ словами, не догадка
  L2  запрос уходит ровно на адрес агента, с его моделью и ключом; ключа нет в тексте ошибки
  L3  структурный вопрос собран как у Hermes (`_build_structured_messages`) и разобран по схеме
  L4  ответ вне схемы — `ok: False` с причиной, не исключение
  L5  прогрев не прошёл — данные сессии в модель не уходят
"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

from hermes_filetools.context_ctl import _llm


class _Agent:
    base_url = "http://srv:5000/v1/"
    model = "M-1"
    api_key = "sekret"
    api_mode = "chat_completions"


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_server(monkeypatch, content, sent):
    def urlopen(req, timeout=None):
        sent.append({"url": req.full_url, "headers": dict(req.header_items()),
                     "body": json.loads(req.data.decode("utf-8"))})
        return _Resp(json.dumps({"model": "M-1", "usage": {"total_tokens": 7},
                                 "choices": [{"message": {"content": content}}]}).encode("utf-8"))
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)


def test_l1_no_route_no_model():
    ok, why = _llm.available()
    assert not ok and "маршрут" in why
    with _llm.use_route(None):
        ok, why = _llm.available()
        assert not ok and "агента" in why
    bad = _Agent()
    bad.api_mode = "anthropic_messages"
    with _llm.use_route(bad):
        ok, why = _llm.available()
        assert not ok and "chat_completions" in why
    assert _llm.available()[0] is False, "маршрут снят после прогона"


def test_l2_request_goes_to_the_agent_address(monkeypatch):
    sent = []
    _fake_server(monkeypatch, "привет", sent)
    with _llm.use_route(_Agent()):
        assert _llm.available()[0]
        out = _llm.ask("ok", system="S")
    assert out["ok"] and out["text"] == "привет" and out["tokens"] == 7
    assert sent[0]["url"] == "http://srv:5000/v1/chat/completions"
    assert sent[0]["headers"].get("Authorization") == "Bearer sekret"
    assert sent[0]["body"]["model"] == "M-1"
    assert sent[0]["body"]["messages"][0] == {"role": "system", "content": "S"}
    assert sent[0]["body"]["chat_template_kwargs"] == {"enable_thinking": False}

    def refuse(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b"nope"))
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with _llm.use_route(_Agent()):
        out = _llm.ask("ok")
    assert not out["ok"] and "HTTP 400" in out["error"] and "sekret" not in out["error"]


def test_l3_structured_like_hermes(monkeypatch):
    sent = []
    _fake_server(monkeypatch, '```json\n{"count": 3}\n```', sent)
    schema = {"type": "object", "properties": {"count": {"type": "integer"}},
              "required": ["count"]}
    with _llm.use_route(_Agent()):
        out = _llm.ask_structured("ДАННЫЕ\n", "вопрос?", schema, schema_name="s1", system="SYS")
    assert out["ok"] and out["parsed"] == {"count": 3}
    body = sent[0]["body"]
    assert body["messages"][0]["content"].startswith("SYS\n\nRespond with a single JSON object")
    parts = body["messages"][1]["content"]
    assert parts[0]["text"].startswith("ДАННЫЕ\n\nSchema name: s1\n\nJSON schema:\n")
    assert parts[1]["text"] == "вопрос?", "вопрос — в хвосте, префикс общий"
    assert body["response_format"]["json_schema"]["schema"] == schema


def test_l4_answer_outside_schema(monkeypatch):
    _fake_server(monkeypatch, "просто проза", [])
    with _llm.use_route(_Agent()):
        out = _llm.ask_structured("D", "q", {"type": "object"})
    assert not out["ok"] and out["parsed"] is None and out["error"]


def test_l5_failed_warmup_sends_no_data(monkeypatch, tmp_path):
    from hermes_filetools.context_ctl import _distill
    asked = []

    def ask(prompt, **kw):
        asked.append(prompt)
        return {"ok": False, "text": "", "error": "HTTP 400"}
    monkeypatch.setattr(_distill._llm, "ask", ask)
    monkeypatch.setattr(_distill._llm, "ask_structured",
                        lambda *a, **kw: asked.append("DATA") or {"ok": False})
    monkeypatch.setattr(_distill, "dump_dir", lambda session: tmp_path)
    monkeypatch.setattr(_distill, "SETTLE_S", 0)
    rows = [{"role": "user", "content": "раз"}, {"role": "assistant", "content": "два"}]
    out = _distill.interrogate("s", rows, [0, 1], {0: "m0001", 1: "m0002"})
    assert asked == ["ok"], "после неудачного прогрева данные не уходили"
    assert not out["ok"] and "прогрев" in out["error"]
