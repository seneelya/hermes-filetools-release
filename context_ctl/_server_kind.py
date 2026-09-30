"""С кем говорит наш клиент модели и какими полями сказать ему «не думай» (2026-09-30).

ЗАЧЕМ. Прямой клиент (`_llm`) слал всем серверам поля llama.cpp (`chat_template_kwargs`) и
Hermes (`reasoning`). Локальному серверу это нужно, а облако может ответить на незнакомое
поле HTTP 400 — и опрос падает на первом же вопросе. Здесь — ОСНОВА выбора: вид сервера по
конфигу или по адресу и поля «без рассуждения» для каждого вида. Облако живьём НЕ проверено;
первая проверка — Gemini (OpenAI-вход). Страховка от ошибки в этой таблице — ступени отката
в `_llm._complete` (повтор без наших добавок), не эта таблица.

Вид — `compact_server` в `plugin.yaml`: `auto` (по адресу) или имя из `KINDS`.
"""

from __future__ import annotations

import copy
import ipaddress
import urllib.parse

KINDS = ("llamacpp", "vllm", "gemini", "openai", "openrouter", "plain")

# Облака по имени хоста. Всё, чего здесь нет и что не локально, — `plain`: без добавок.
_HOSTS = {
    "generativelanguage.googleapis.com": "gemini",
    "api.openai.com": "openai",
    "openrouter.ai": "openrouter",
}

# Поля «не думай» по виду. Меньше — безопаснее: незнакомое поле облако вправе отвергнуть.
_NO_THINK = {
    # Как было до 2026-09-30, байт в байт: `reasoning` — форма Hermes, `chat_template_kwargs` —
    # переменная шаблона (Qwen читает `enable_thinking` оттуда), см. шапку `_llm`.
    "llamacpp": {"reasoning": {"enabled": False, "effort": "none"},
                 "chat_template_kwargs": {"enable_thinking": False}},
    # vLLM `chat_template_kwargs` знает; `reasoning` не шлём — у него такого поля нет.
    "vllm": {"chat_template_kwargs": {"enable_thinking": False}},
    # OpenAI-вход Gemini. `none` выключает рассуждение у моделей, где оно выключаемо; где нет —
    # 400, и `_llm` повторит без добавок. НЕ ПРОВЕРЕНО ЖИВЬЁМ.
    "gemini": {"reasoning_effort": "none"},
    # Единая форма OpenRouter для всех их моделей. НЕ ПРОВЕРЕНО ЖИВЬЁМ.
    "openrouter": {"reasoning": {"enabled": False}},
    # `reasoning_effort` у моделей без рассуждения — 400; не шлём ничего.
    "openai": {},
    "plain": {},
}


def _is_local(host: str) -> bool:
    host = host.strip("[]").lower()
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".lan")):
        return True
    if "." not in host and ":" not in host:
        return True  # имя машины в сети без домена
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def kind_of(base_url: str, setting: str = "auto") -> tuple[str, str]:
    """Вид сервера и откуда он взят: `(kind, how)`; `how` — «конфиг» или «по адресу»."""
    setting = str(setting or "auto").strip().lower()
    if setting in KINDS:
        return setting, "конфиг"
    host = urllib.parse.urlsplit(str(base_url or "")).hostname or ""
    if host in _HOSTS:
        return _HOSTS[host], "по адресу"
    if _is_local(host):
        return "llamacpp", "по адресу"
    return "plain", "по адресу"


def no_think(kind: str) -> dict:
    """Поля запроса, гасящие рассуждение у этого вида. Копия — вызывающий вправе менять."""
    return copy.deepcopy(_NO_THINK.get(kind, {}))
