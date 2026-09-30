"""Отчёт о токенах живой сессии для мини-панели (`Plan24`): все числа, каждое со своим источником.

Собирается на стороне АГЕНТА (метод движка `token_report`), потому что только здесь видны
системный промпт (`agent._cached_system_prompt`), схемы тулов (`agent.tools`), модель и адрес
провайдера. Строки ленты — из базы тем же `_apply.db_history`, что пишет компакция.

Источники не смешиваются (решение владельца: «не путать их как они»):

- `anchor`   — реальный `usage` последнего ответа (`prompt_tokens` + справочно `completion_tokens`);
- `ours`     — наш счёт (`_tokens.count_ours`) × множитель токенизатора + обёртки шаблона;
- `hermes`   — их оценка (`estimate_request_tokens_rough`), чтобы расхождение было видно;
- `exact`    — сервер модели (`_tokserver`), только по запросу и только на загруженной модели;
- `window`   — окно модели: у Hermes (`engine.context_length`) и у сервера (`--ctx-size`).

`next` — сколько уйдёт следующим запросом: якорь + наш счёт строк, появившихся после него. Какие
строки «после» — по времени строки (`timestamp`) против времени якоря; граница приблизительная,
поэтому число строк по обе стороны тоже в отчёте.
"""
from __future__ import annotations

import json
import time

from . import _settings, _tokens, _tokserver

# Сервер спрашиваем о статусе модели не чаще этого: отчёт зовут на каждом конце хода.
_SERVER_TTL_S = 5.0

# Доля нового точного замера в множителе токенизатора. Не перезапись: на живой проверке
# 2026-09-29 множитель прыгал 0.86 ↔ 1.03 между двумя сессиями — содержимое у сессий разное,
# а множитель общий на токенизатор.
_MULT_WEIGHT = 0.3

# Сколько точных замеров постоянной части (система / схемы тулов) держим на токенизатор.
_FIXED_KEEP = 32


def auto_settings() -> tuple[int, bool]:
    """`(compact_auto_percent, compact_auto_enabled)` из ЖИВОГО `plugin.yaml` (`_settings`): правка в
    панели Config двигает и засечку, и решение движка — без перезапуска клиента."""
    cfg = _settings.compaction()
    return cfg["compact_auto_percent"], cfg["compact_auto_enabled"]


def decide(rep: dict) -> dict:
    """Сжимаемся или нет — по отчёту (`Plan24-Task07`).

    Число — самое надёжное из есть: якорь + прирост (`next`), иначе точный счёт, иначе наша
    оценка. Окно — сервера (`--ctx-size`), иначе Hermes. `fire` — число дошло до порога.
    `enabled` — включён ли автомат НАШИМ ключом `compact_auto_enabled`: пока нет, решение только
    видно, `should_compress` отвечает «нет».
    """
    percent, enabled = auto_settings()
    if rep.get("anchor") and isinstance(rep.get("next"), int):
        value, source = rep["next"], "next"
    elif isinstance((rep.get("exact") or {}).get("total"), int):
        value, source = rep["exact"]["total"], "exact"
    else:
        value, source = (rep.get("ours") or {}).get("total"), "ours"
    window = (rep.get("window") or {}).get("server") or (rep.get("window") or {}).get("hermes") or 0
    threshold = round(window * percent / 100) if window else None
    fire = bool(threshold and isinstance(value, int) and value >= threshold)
    return {"value": value, "source": source, "window": window, "percent": percent,
            "threshold": threshold, "fire": fire, "enabled": enabled, "at": time.strftime("%H:%M:%S")}


# ЗАЩИТА АВТОМАТА ОТ ПОВТОРОВ (Plan16 п.1, задача C; владелец, 2026-09-29: «да так делать»).
# Их автомат спрашивает нас несколько раз за ход (живьём: 3). Без защиты неудачная или
# недостаточная компакция запускалась бы на каждом вопросе. Правила:
#   - идёт ЛЮБАЯ наша компакция (ручная из панели или автоматическая) — «не сейчас»;
#   - один автоматический запуск на ХОД (ход = последняя реплика человека в ленте), удачный он или нет;
#   - две неудачи подряд — пауза `compact_auto_pause_minutes`.
AUTO_FAILS_BEFORE_PAUSE = 2


def new_auto_state() -> dict:
    return {"turn": None, "fails": 0, "pause_until": 0.0, "last": None, "pending": False}


def auto_gate(state: dict, turn_key, busy: bool, now: float) -> tuple[bool, str]:
    """Можно ли запускать автокомпакцию сейчас: `(да/нет, причина словами)`."""
    if busy:
        return False, "компакция уже идёт"
    if now < state.get("pause_until", 0.0):
        left = int((state["pause_until"] - now) / 60) + 1
        return False, f"пауза после {state.get('fails', 0)} неудач подряд — ещё ~{left} мин"
    if turn_key is not None and state.get("turn") == turn_key:
        return False, "в этом ходе автокомпакция уже была"
    return True, "можно"


def auto_started(state: dict, turn_key) -> None:
    """«Да» ушло их автомату: запоминаем ход и ждём их `compress()` (`pending`)."""
    state["turn"] = turn_key
    state["pending"] = True


def auto_finished(state: dict, ok: bool, why: str, now: float, pause_minutes: int) -> None:
    """Итог автозапуска: удача обнуляет счёт неудач, вторая неудача подряд ставит паузу."""
    state["last"] = {"ok": bool(ok), "why": why, "at": time.strftime("%H:%M:%S")}
    if ok:
        state["fails"] = 0
        return
    state["fails"] = state.get("fails", 0) + 1
    if state["fails"] >= AUTO_FAILS_BEFORE_PAUSE:
        state["pause_until"] = now + max(0, pause_minutes) * 60


def turn_key_of(rows: list):
    """Ключ хода — последняя реплика человека (её время, иначе номер строки)."""
    for i in range(len(rows) - 1, -1, -1):
        row = rows[i]
        if isinstance(row, dict) and row.get("role") == "user":
            return str(row.get("timestamp") or f"row{i}")
    return None


def fixed_key(kind: str, text: str) -> str:
    """Ключ точного числа постоянной части: `sys:a1b2c3` / `tools:…` — короткий хеш текста."""
    import hashlib
    return f"{kind}:" + hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:6]


def db_system_prompt(session_id: str) -> str:
    """Системный промпт сессии из базы (`sessions.system_prompt_hash` → `system_prompts`).

    Нужен, пока агент в этом процессе ещё не делал запроса: `_cached_system_prompt` появляется
    только с первым запросом, и без базы отчёт показал бы `sys 0` (живая проверка 2026-09-29).
    Только чтение.
    """
    import sqlite3
    db = _tokens.tokens_dir().parent.parent / "state.db"
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            row = con.execute("select p.prompt from system_prompts p join sessions s "
                              "on s.system_prompt_hash = p.hash where s.id = ?", (session_id,)).fetchone()
        finally:
            con.close()
    except Exception:  # noqa: BLE001 - нет таблицы / базы — промпт неизвестен, не ошибка отчёта
        return ""
    return (row[0] or "") if row else ""


# Построчный счёт живёт в `_tokens` — его грузит по пути и `/messages` (числа зеркала).
_wire_text = _tokens.wire_text
_calls = _tokens.row_calls
_args = _tokens.call_args
_row_ours = _tokens.row_units


def to_wire(rows: list, system_prompt: str) -> list:
    """Строки базы → `messages` для `/apply-template`: так, как их шлёт Hermes (без рассуждений, T3)."""
    out = [{"role": "system", "content": system_prompt}] if system_prompt else []
    for row in rows:
        role = row.get("role")
        if role not in ("user", "assistant", "tool", "system"):
            continue
        msg = {"role": role, "content": _wire_text(row)}
        calls = _calls(row)
        if role == "assistant" and calls:
            msg["tool_calls"] = [{"id": c.get("id") or f"c{i}", "type": "function",
                                  "function": {"name": (c.get("function") or {}).get("name") or "",
                                               "arguments": json.dumps(_args(c), ensure_ascii=False)}}
                                 for i, c in enumerate(calls)]
        if role == "tool":
            msg["tool_call_id"] = row.get("tool_call_id") or ""
        out.append(msg)
    return out


class Counter:
    """Состояние счёта одной сессии: живёт в движке, переживает отчёты."""

    def __init__(self):
        self.anchor = None          # {"prompt", "completion", "at"}
        self.exact = None           # последний точный замер сервером
        self._server = {}           # root → (время, models())
        self._probed = set()        # имена, для которых отпечаток сняли в этом процессе
        self._last_server = None    # последнее состояние сервера — для отчёта без сети
        self.last_decision = None   # последнее решение «сжимаемся или нет» (по отчёту)
        self.last_ask = None        # последний вопрос ИХ автомата (`should_compress`) и наш ответ
        self.rewrites = 0           # сколько раз НАША запись сбросила якорь в этом процессе
        self.auto = new_auto_state()  # защита автомата от повторов (задача C)

    def note_usage(self, prompt: int, completion: int) -> None:
        if prompt:
            self.anchor = {"prompt": int(prompt), "completion": int(completion or 0), "at": time.time()}

    # ------------------------------------------------------------------ сервер

    def _state(self, root: str, model: str) -> dict:
        now = time.time()
        cached = self._server.get(root)
        if cached is None or now - cached[0] > _SERVER_TTL_S:
            try:
                cached = (now, _tokserver.models(root, timeout=3), "")
            except _tokserver.NoServer as exc:
                cached = (now, None, str(exc))
            self._server[root] = cached
        _at, models, err = cached
        if models is None:
            return {"kind": "none", "status": None, "ctx": None, "template": None, "error": err}
        # Роутер llama.cpp даёт статус каждой модели; одиночный сервер (llama-server с одной
        # моделью, vLLM, облако) — нет. У одиночного `/tokenize` поле `model` НЕ СМОТРИТ и считает
        # своей моделью, поэтому модель сессии, которой там нет, — `absent`, а не «загружена»:
        # иначе чужой токенизатор записался бы под именем модели сессии.
        kind = "router" if any(m.get("status") is not None for m in models) else "single"
        for m in models:
            if m["id"] == model:
                return {"kind": kind, **{k: m[k] for k in ("status", "ctx", "template")}, "error": ""}
        return {"kind": kind, "status": "absent", "ctx": None, "template": None, "error": ""}

    def _ensure_probe(self, data: dict, root: str, model: str, allow_load: bool) -> str:
        """Отпечаток модели, если его ещё нет: сама — только на загруженной, иначе по кнопке."""
        if model in data["names"] or model in self._probed:
            return ""
        try:
            res = _tokserver.probe(root, model, allow_load)
        except (_tokserver.NotLoaded, _tokserver.NoServer) as exc:
            return str(exc)
        self._probed.add(model)
        _tokens.remember_probe(data, model, res["fingerprint"], res["probe_tokens"], res["markup"])
        _tokens.save_models(data)
        return ""

    def _sibling(self, root: str, data: dict, fp: str) -> str:
        """Загруженная модель с тем же отпечатком токенизатора, если есть."""
        cached = self._server.get(root)
        for m in (cached[1] if cached and cached[1] else []):
            if m.get("status") == "loaded" and data["names"].get(m.get("id")) == fp:
                return m["id"]
        return ""

    # ------------------------------------------------------------------ отчёт

    def report(self, agent, engine, rows: list, rows_note: str, exact: bool = False,
               allow_load: bool = False, offline: bool = False) -> dict:
        """Отчёт по источникам. `offline` — без единого запроса к серверу (для `should_compress`,
        который их автомат зовёт перед каждым запросом): состояние сервера — последнее известное."""
        model = getattr(agent, "model", "") or ""
        base = getattr(agent, "base_url", "") or ""
        system_prompt = getattr(agent, "_cached_system_prompt", "") or ""
        system_source = "live" if system_prompt else ""
        if not system_prompt:
            system_prompt = db_system_prompt(getattr(agent, "session_id", "") or "")
            system_source = "db" if system_prompt else "none"
        tools = list(getattr(agent, "tools", None) or [])
        root = _tokserver.server_root(base)
        if offline:
            server = dict(self._last_server or {"kind": "unknown", "status": None, "ctx": None})
        else:
            server = self._state(root, model) if root else {"kind": "none", "error": "нет base_url"}
            self._last_server = server

        data = _tokens.load_models()
        probe_err = ""
        present = server.get("kind") != "none" and server.get("status") != "absent"
        if present and not offline and (server.get("status") in ("loaded", None) or allow_load):
            probe_err = self._ensure_probe(data, root, model, allow_load)
        fp, entry, exact_model = _tokens.entry_for(data, model)
        mult = (entry or {}).get("mult") or 1.0
        markup = (entry or {}).get("markup") or {}
        # Модель сессии не загружена, но загружена ДРУГАЯ с тем же отпечатком токенизатора
        # (Ornith-1.0 и 1.5 — один токенизатор, проверено 2026-09-29) — точно считаем ею, ничего
        # не загружая.
        via = model
        if exact_model and server.get("status") not in ("loaded", None):
            via = self._sibling(root, data, fp) or model
            if via != model:
                server = {**server, "via": via}

        sys_units = _tokens.count_ours(system_prompt)
        tools_text = "\n".join(json.dumps(t, ensure_ascii=False) for t in tools)
        tools_units = _tokens.count_ours(tools_text)
        msgs_units = msgs_markup = 0
        after_units = after_markup = 0
        at = (self.anchor or {}).get("at")
        before = after = 0
        for row in rows:
            u, w = _row_ours(row, markup)
            msgs_units += u
            msgs_markup += w
            ts = row.get("timestamp")
            try:
                ts = float(ts)
            except (TypeError, ValueError):
                ts = None
            if at is not None and ts is not None and ts > at:
                after += 1
                after_units += u
                after_markup += w
            else:
                before += 1

        est = lambda units, w=0: round(units * mult) + w  # noqa: E731
        # Постоянная часть — ТОЧНЫМ числом, если этот текст уже мерили сервером (владелец,
        # 2026-09-29): ошибка оценки остаётся только на сообщениях. Хеш — от текста, поэтому
        # сменился промпт или набор тулов — снова оценка до следующего точного замера.
        fixed = ((entry or {}).get("fixed") or {}) if exact_model else {}
        sys_key, tools_key = fixed_key("sys", system_prompt), fixed_key("tools", tools_text)
        sys_tok = fixed.get(sys_key) if system_prompt and sys_key in fixed else est(sys_units)
        tools_tok = fixed.get(tools_key) if tools and tools_key in fixed else est(tools_units)
        fixed_exact = {"system": bool(system_prompt) and sys_key in fixed,
                       "tools": bool(tools) and tools_key in fixed}
        ours_total = sys_tok + tools_tok + est(msgs_units, msgs_markup)
        rep = {
            "at": time.strftime("%H:%M:%S"),
            "session": getattr(agent, "session_id", "") or "",
            "model": model, "provider": getattr(agent, "provider", "") or "", "base_url": base,
            "server": server,
            "window": {"hermes": int(getattr(engine, "context_length", 0) or 0),
                       "server": server.get("ctx"),
                       "hermes_threshold": int(getattr(engine, "threshold_tokens", 0) or 0)},
            "tokenizer": {"fp": fp, "exact_model": exact_model, "mult": mult, "markup": markup,
                          "probe_error": probe_err},
            "rows": {"total": len(rows), "before_anchor": before, "after_anchor": after,
                     "note": rows_note},
            "anchor": dict(self.anchor) if self.anchor else None,
            "ours": {"system": sys_tok, "tools": tools_tok, "tools_count": len(tools), "fixed_exact": fixed_exact,
                     "msgs": est(msgs_units, msgs_markup), "markup": msgs_markup, "total": ours_total},
            "next": (self.anchor["prompt"] + est(after_units, after_markup)) if self.anchor else None,
            "hermes": _their_estimate(rows, system_prompt, tools),
            "chars": {"system": len(system_prompt), "tools": len(tools_text)},
            "system_source": system_source,
            "exact": self.exact,
            "last_ask": self.last_ask,
            "rewrites": self.rewrites,
            "turn_key": turn_key_of(rows),
            "auto": {k: self.auto[k] for k in ("fails", "pause_until", "last")},
        }
        rep["decision"] = self.last_decision = decide(rep)
        if offline:
            return rep
        if exact == "auto":
            # Сам — только когда модель уже загружена: за точным числом сервер не грузим.
            exact = server.get("kind") != "none" and (server.get("status") in ("loaded", None) or via != model)
        if exact:
            rep["exact"] = self.exact = self._exact(root, via, rows, system_prompt, tools,
                                                    allow_load and via == model, server)
            # Точный замер уточняет множитель токенизатора — только если отпечаток этой модели.
            fp, entry, exact_model = _tokens.entry_for(_tokens.load_models(), model)
            # Без системного промпта «лента» точного замера — это лента + система: не калибруем.
            if "msgs" in self.exact and msgs_units and exact_model and system_prompt:
                data = _tokens.load_models()
                measured = (self.exact["msgs"] - msgs_markup) / msgs_units
                old = data["tok"][fp].get("mult") or measured
                data["tok"][fp]["mult"] = round(old * (1 - _MULT_WEIGHT) + measured * _MULT_WEIGHT, 4)
                rep["tokenizer"]["mult_measured"] = round(measured, 4)
                data["tok"][fp]["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                _tokens.save_models(data)
                rep["tokenizer"]["mult_after_exact"] = data["tok"][fp]["mult"]
            if "system" in self.exact and exact_model and fp:
                self._remember_fixed(fp, sys_key if system_prompt else "", tools_key if tools else "", rep)
            rep["decision"] = self.last_decision = decide(rep)  # точный счёт мог сменить число
        return rep

    def _remember_fixed(self, fp: str, sys_key: str, tools_key: str, rep: dict) -> None:
        """Запомнить точные числа системы и тулов по хешу их текста; подставить их в отчёт."""
        data = _tokens.load_models()
        entry = data["tok"].get(fp)
        if entry is None:
            return
        fixed = entry.setdefault("fixed", {})
        ours = rep["ours"]
        for key, part in ((sys_key, "system"), (tools_key, "tools")):
            if not key:
                continue
            fixed.pop(key, None)  # свежий — в конец, старые уходят первыми
            fixed[key] = int(self.exact[part])
            ours["total"] += fixed[key] - ours[part]
            ours[part] = fixed[key]
            ours["fixed_exact"][part] = True
        while len(fixed) > _FIXED_KEEP:
            fixed.pop(next(iter(fixed)))
        _tokens.save_models(data)

    def _exact(self, root, model, rows, system_prompt, tools, allow_load, server) -> dict:
        if server.get("kind") == "none":
            return {"error": "у провайдера нет точного счёта (" + (server.get("error") or "не llama.cpp") + ")"}
        try:
            u = [{"role": "user", "content": "."}]
            total = _tokserver.count_prompt(root, model, to_wire(rows, system_prompt), tools, allow_load)
            bare = _tokserver.count_prompt(root, model, u, None, True)
            with_tools = _tokserver.count_prompt(root, model, u, tools, True) if tools else bare
            with_sys = _tokserver.count_prompt(root, model, [{"role": "system", "content": system_prompt}] + u,
                                               tools, True) if system_prompt else with_tools
        except (_tokserver.NotLoaded, _tokserver.NoServer) as exc:
            return {"error": str(exc)}
        return {"at": time.strftime("%H:%M:%S"), "total": total, "tools": with_tools - bare,
                "system": with_sys - with_tools, "system_block": with_sys - bare,
                "msgs": total - (with_sys - bare)}


def _their_estimate(rows, system_prompt, tools):
    try:
        from agent.model_metadata import estimate_request_tokens_rough
    except Exception:  # noqa: BLE001 - нет их кода (тест / голый python)
        return None
    try:
        msgs = to_wire(rows, "")
        return int(estimate_request_tokens_rough(msgs, system_prompt=system_prompt, tools=tools or None))
    except Exception:  # noqa: BLE001 - их сигнатура поменялась — число просто не показываем
        return None
