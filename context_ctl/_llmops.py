"""Сервер окна LLMOps (`Plan31` Таск 2): данные окна, разговор, модели, пресеты.

ДАННЫЕ — ТЕМ ЖЕ РАЗРЕЗОМ, ЧТО У КОМПАКЦИИ. Выбор достраивается до пар и планируется её же кодом
(`_insert._expand_to_pairs`, `_insert._plan`): пролёт один, дырки в нём — пузырьки (живые
ходы, которые замена оставит). Префикс — с начала сессии, суффикс — `_distill.RESERVE_ROWS` за
концом, как у опроса. Окно по этому же выбору потом пишет замену (`/llmops-replace`), поэтому
адреса для замены (`addrs`) — ровно съедаемое, со служебными строками (скобка, мостик), которые
в чипы не попадают.

РАМКИ. С отмеченного снимаются ДИРЕКТИВНЫЕ рамки наших дистиллятов — точным совпадением, тем же
списком форм, что у опроса (`_prompts.known_frame_lines`), — и возвращаются в `frames` (для
«как было»). Нейтральная рамка суммаризации — часть содержания, не снимается. В префиксе и
суффиксе рамки режутся так же, как в данных опроса (`_render.strip_past_frames`: на месте шапки
— короткая пометка, что кусок уже сжат).

РАЗГОВОР. Весь разговор окна — каждый раз, списком сообщений, на маршрут опроса компакции
(`_llm.route_of`: сессии или наш); модель — из выбора окна, «думать» — снимает поля «не думай».
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request

from . import _apply, _llm, _progress, _prompts, _render, _tokens
from . import _insert
from ._prompts import _llmops as _presets

ROLES = ("system", "user", "assistant")
# Обёртка «как было» — не текст из словаря, а та рамка, что стояла на заменяемом (`replace`).
AS_IT_WAS = "as-it-was"
# Пауза после прогрева перед разговором — как у опроса компакции (`_distill.SETTLE_S`). 15 с пробовали
# 2026-10-07: падение llama.cpp (CUDA `allreduce.cu`) оказалось багом сервера на этом промпте —
# воспроизведено в его собственном чате без плагина; пауза не влияет. Владелец: «оставь 3 секунды».
WARM_SETTLE_S = 3.0
# Сколько ждём ответа на разговор окна: большие сессии считаются минутами (владелец, 2026-10-14 —
# 30 с UI и 180 с маршрута обрывали). Окно ждёт столько же (`adapter.js::LLMOPS_CHAT_TIMEOUT_MS`).
CHAT_TIMEOUT_S = 30 * 60

# Идущий разговор окна (одно окно — один разговор): его соединения и флаг «остановлен» (`stop`).
_run_lock = threading.Lock()
_run: dict | None = None


def _sink(run: dict):
    """Приёмник соединений `_llm.conn_sink` для разговора `run`; после стопа новых не открываем."""
    def take(conn):
        with _run_lock:
            if run["stopped"]:
                raise RuntimeError("остановлено")
            # Сам сокет, не соединение: после `getresponse` http.client отвязывает его (`conn.sock = None`),
            # а ответ читает дальше через тот же сокет.
            run["socks"].append(conn.sock)
    return take


def stop() -> dict:
    """■ Stop окна: гасим сокеты идущего разговора — `urlopen` в его потоке падает, сервер модели
    видит обрыв. `{ok, cut}` — сколько соединений оборвали (0 — нечего было или ещё не открыто)."""
    with _run_lock:
        run = _run
        if run is None:
            return {"ok": True, "cut": 0}
        run["stopped"] = True
        socks = list(run["socks"])
    cut = 0
    for sock in socks:
        if sock is None:
            continue
        try:
            import socket
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        cut += 1
    return {"ok": True, "cut": cut}


def _strip_target_frames(text: str) -> tuple:
    """Снять с текста ДИРЕКТИВНУЮ рамку целиком. `(текст, рамка | None)`. Только точное совпадение."""
    body = str(text or "")
    if not _prompts.is_distillate(body) and not any(h in body for h in _prompts.known_frame_lines()[0]):
        return body, None
    heads, ends = _prompts.known_frame_lines()
    head = next((h for h in heads if h in body), "")
    if not head:
        return body, None
    body = body.replace(head, "")
    tail = next((e for e in ends if e in body), "")
    if tail:
        body = body.replace(tail, "")
    return body.strip(), {"kind": "compaction", "head": head, "tail": tail}


def _is_service(text: str) -> bool:
    """Служебная строка компакции без содержания: отдельная скобка или мостик."""
    t = str(text or "")
    return _prompts.is_bridge(t) or (_prompts.is_distill_end(t) and not _prompts.is_distillate(t))


def _tool_names(rows) -> dict:
    """`tool_call_id → имя инструмента` по репликам-носителям."""
    out = {}
    for row in rows or []:
        for tc in row.get("tool_calls") or []:
            fn = tc.get("function") if isinstance(tc, dict) else None
            if isinstance(tc, dict) and tc.get("id"):
                out[tc["id"]] = str((fn or {}).get("name") or "?")
    return out


def _message(row, addr: str, names: dict, text: str) -> dict:
    out = {"addr": addr, "role": str(row.get("role") or ""), "text": text,
           "tokens": _tokens.count_ours(text)}
    if out["role"] == "tool":
        out["tool"] = names.get(row.get("tool_call_id"), "?")
    calls = _render.tool_names(row)
    if calls:
        out["calls"] = calls
    return out


def window_data(session_id: str, rows: list, by_msg: dict, addrs: list) -> dict:
    """Данные окна: `{ok, prefix, target, bubbles, suffix, frames, addrs, lines}` или `{ok: False, error, lines}`."""
    from . import _distill  # лениво: `_distill` импортирует `_compact`, на модульном уровне цикл
    lines: list = []
    wanted = {_insert._addr_number(str(a).strip()) for a in addrs or []} - {-1}
    if not wanted:
        return {"ok": False, "error": "ни одного адреса", "lines": lines}
    wanted = _insert._expand_to_pairs(rows, by_msg, wanted, lines)
    runs, _skipped, missing, _patch, _replaced, _hollow = _insert._plan(rows, by_msg, wanted, lines)
    if missing:
        lines.append("адреса не найдены: " + ", ".join(_insert._addr(n) for n in missing[:8]))
    if not runs:
        return {"ok": False, "error": "по этим адресам в ленте нечего взять", "lines": lines}
    run = runs[0]
    eaten, kept = sorted(run["eaten"]), sorted(run.get("kept") or [])
    order = {id(row): i for i, row in enumerate(rows)}
    addr_at = {order[id(row)]: addr for addr, row in by_msg.items() if id(row) in order}
    names = _tool_names(rows)
    positions = _distill.positions_for(rows, eaten[-1])

    def ctx(i):
        return _message(rows[i], addr_at.get(i, ""), names,
                        _render.strip_past_frames(_render._content(rows[i])))

    target, frames = [], []
    for i in eaten:
        text = _render._content(rows[i])
        if _is_service(text):
            continue
        text, frame = _strip_target_frames(text)
        if frame and not any(f["head"] == frame["head"] for f in frames):
            frames.append(frame)
        target.append(_message(rows[i], addr_at.get(i, ""), names, text))
    return {
        "ok": True,
        "prefix": [ctx(i) for i in positions if i < eaten[0]],
        "target": target,
        "bubbles": [ctx(i) for i in kept],
        "suffix": [ctx(i) for i in positions if i > eaten[-1]],
        "frames": frames,
        "addrs": [addr_at[i] for i in eaten if i in addr_at],
        "lines": lines,
    }


def chat(messages: list, model: str = "", think: bool = False, timeout: float | None = None) -> dict:
    """Весь разговор окна одним запросом на маршрут опроса. `{ok, text, model, tokens}` / `{ok: False, error}`.

    Маршрут ставит вызывающий (`_llm.use_route`). `model` — из выбора окна, пусто — модель маршрута.
    Ждёт до `CHAT_TIMEOUT_S`; `stop()` обрывает его (ответ `{ok: False, stopped: True}`)."""
    global _run
    run = {"socks": [], "stopped": False}
    with _run_lock:
        _run = run
    token = _llm.conn_sink.set(_sink(run))
    try:
        out = _chat(messages, model, think, timeout or CHAT_TIMEOUT_S)
    finally:
        _llm.conn_sink.reset(token)
        with _run_lock:
            if _run is run:
                _run = None
    if run["stopped"]:
        return {"ok": False, "stopped": True, "error": "остановлено"}
    return out


def _chat(messages: list, model: str, think: bool, timeout: float) -> dict:
    """Тело `chat`: прогрев, запрос, разбор."""
    ok, why = _llm.available()
    if not ok:
        return {"ok": False, "error": why}
    msgs = [{"role": str(m.get("role")), "content": str(m.get("content") or "")}
            for m in messages or [] if isinstance(m, dict) and m.get("role") in ROLES]
    if not msgs or msgs[-1]["role"] != "user":
        return {"ok": False, "error": "разговор должен кончаться репликой user"}
    route = dict(_llm._route.get())
    if str(model or "").strip():
        route["model"] = str(model).strip()
    warm = _warm_up(route, timeout)
    if warm.get("error"):
        # Прогрев — голое «ok» без данных. Не ответил — разговор никуда не шлём.
        return {"ok": False, "error": f"модель не ответила на прогрев: {warm['error']}"}
    try:
        result = _llm._complete(route, msgs, timeout, think=bool(think), stream=True)
    except Exception as exc:  # noqa: BLE001 — отчёт, не падение
        return {"ok": False, "error": str(exc)}
    text = result["text"].strip()
    if not text:
        return {"ok": False, "error": f"модель вернула пустой ответ ({result['model']})"}
    out = {"ok": True, "text": text, "model": result["model"], "tokens": result["tokens"]}
    if warm.get("ms") is not None:
        out["warmed_ms"] = warm["ms"]
    return out


def _server_root(base_url: str) -> str:
    """Корень сервера для `/v1/models`: адрес маршрута обычно уже кончается на `/v1`."""
    base = str(base_url or "").rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def _warm_up(route: dict, timeout) -> dict:
    """ПРОГРЕВ, как у опроса компакции (`_distill.WARMUP`, диагноз владельца 2026-09-17): первый
    запрос в НЕзагруженную модель роутер llama.cpp встречает загрузкой, и большой промпт сразу за ней
    валит сервер (живьём снова 2026-10-07: окно LLMOps, CUDA `allreduce.cu`, процесс модели умер).
    Статус спрашиваем у роутера (`/v1/models` модель не грузит); `loaded` или сервер без статусов —
    греть нечего. Не загружена или статус не узнать — голое «ok», пауза `SETTLE_S`, потом разговор.
    `{}` — прогрев не нужен; `{ms}` — прогрет; `{error}` — не ответил."""
    from . import _tokserver  # лениво: `_distill` тянет `_insert`/`_compact`
    try:
        status = _tokserver.model_state(_server_root(route.get("base_url")), route["model"])["status"]
    except Exception:  # noqa: BLE001 — не узнали: греем, это дёшево и безопасно
        status = "unknown"
    if status in ("loaded", None):
        return {}
    started = time.monotonic()
    try:
        answer = _llm._complete(route, [{"role": "user", "content": "ok"}], timeout, think=False)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    if not str(answer.get("text") or "").strip():
        return {"error": "пустой ответ"}
    time.sleep(WARM_SETTLE_S)
    return {"ms": int((time.monotonic() - started) * 1000)}


def models(timeout: float = 10.0) -> dict:
    """Модели сервера маршрута (`GET <base>/models` — сам список модель не грузит). `{ok, session, list}`."""
    ok, why = _llm.available()
    if not ok:
        return {"ok": False, "error": why}
    route = _llm._route.get()
    headers = {}
    key = _llm._key_value(route.get("api_key"))
    if key:
        headers["Authorization"] = f"Bearer {key}"
    found = []
    try:
        req = urllib.request.Request(route["base_url"] + "/models", headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        found = [str(m.get("id")) for m in (data.get("data") or []) if isinstance(m, dict) and m.get("id")]
    except Exception as exc:  # noqa: BLE001 — нет списка: окно покажет одну модель маршрута
        return {"ok": True, "session": route["model"], "list": [route["model"]],
                "note": f"список моделей не получен: {exc}"}
    if route["model"] not in found:
        found.insert(0, route["model"])
    return {"ok": True, "session": route["model"], "list": found}


def presets() -> list:
    """Пресеты окна (`_prompts/_llmops.py`)."""
    return _presets.presets()


def frame_texts() -> dict:
    """Тексты обёрток, которые поставит сервер (`_prompts.wrap_ready`) — окно рисует их вокруг тела.
    `*_kept` — с пузырьками: у `compaction` своя шапка, а скобка уезжает отдельным сообщением."""
    return {
        "compaction": {"head": _prompts.distill_frame(False), "head_kept": _prompts.distill_frame(True),
                       "tail": _prompts.distill_end(False), "tail_kept": _prompts.distill_end(True)},
        "summarisation": {"head": _prompts.summary_frame(), "tail": _prompts.summary_end()},
    }


# ── Замена: операция `llm ops`, НЕ компакция (владелец, 2026-10-07) ─────────────
# Модель не зовём, текст написан в окне. Встаёт ВСТАВКОЙ В ЛЕНТУ (`_insert`) — тем же механизмом,
# что у компакции и `erase`: пролёт, пузыри, мостики, чередование, ожидание конца хода, журнал
# поглощения. Замок записи — общий (`_insert._busy`): две записи в ленту разом нельзя, кто бы ни
# писал. Ход и итог — в общем файле состояния операции (`_progress`, `who: "llmops"`).


def replace(session_id: str, engine, addrs: list, text: str, wrap: str, preset: str = "") -> dict:
    """Поставить готовый текст на место адресов. Замок уже взят вызывающим. `{ok, lines, …}`;
    `ok` — лента изменена. `preset` — ключ пресета окна, только для журнала (поправка 10)."""
    spec = ",".join(str(a).strip() for a in addrs or [] if str(a or "").strip())
    facts = {"ok": False, "session": session_id, "spec": spec, "runs": 0,
             "absorbed": [], "summaries": [], "lines": []}
    lines = facts["lines"]
    lines.append(f"llm ops: заменить {spec}")
    if wrap not in _prompts.WRAPS and wrap != AS_IT_WAS:
        lines.append(f"ИТОГ: лента не тронута — обёртка `{wrap}` неизвестна, "
                     f"ждём одну из {', '.join(_prompts.WRAPS)}")
        return facts
    body = str(text or "").strip()
    if not body:
        lines.append("ИТОГ: лента не тронута — текст замены пуст")
        return facts
    if not spec:
        lines.append("ИТОГ: лента не тронута — ни одного адреса")
        return facts
    agent = engine.live_agent()
    session, why = _apply.find_gateway_session(agent)
    if session is None:
        lines.append(f"ИТОГ: лента не тронута — {why}")
        return facts
    rows, why = _apply.db_history(agent)
    if why:
        lines.append(f"ИТОГ: лента не тронута — {why}")
        return facts
    by_msg = _insert._index(session_id, rows)
    if not by_msg:
        lines.append("ИТОГ: лента не тронута — адресов сообщений нет (индексатор не разметил сессию)")
        return facts
    places, _bad, gone = _insert.positions_for_spec(rows, by_msg, spec)
    addr_by_row = {id(row): addr for addr, row in by_msg.items()}
    wanted = {_insert._addr_number(addr_by_row.get(id(rows[i]), "")) for i in places} - {-1}
    if gone:
        lines.append("адреса не найдены: " + ", ".join(_insert._addr(n) for n in gone[:8]))
    if not wanted:
        lines.append("ИТОГ: лента не тронута — ни один адрес не найден")
        return facts
    wanted = _insert._expand_to_pairs(rows, by_msg, wanted, lines)
    runs, _skipped, _missing, patch, replaced, hollow = _insert._plan(rows, by_msg, wanted, lines)
    if len(runs) != 1:
        lines.append(f"ИТОГ: лента не тронута — выбор лёг {len(runs)} пролётами, а текст один")
        return facts
    eaten = len(runs[0]["eaten"])
    if eaten > _insert.MAX_MESSAGES:
        lines.append(f"ИТОГ: лента не тронута — {eaten} сообщений больше предела "
                     f"{_insert.MAX_MESSAGES}")
        return facts
    chosen = wrap
    if wrap == AS_IT_WAS:
        # БУКВАЛЬНО ТА РАМКА, ЧТО БЫЛА (владелец, 2026-10-07): снимаем её с заменяемых строк тем же
        # точным совпадением, что при чтении данных окна, и надеваем ровно её. Шапка дистиллята с
        # сохранёнными ходами остаётся такой же, его мостик и отдельная скобка — на своих местах.
        frame = next((f for f in (_strip_target_frames(_render._content(rows[i]))[1]
                                  for i in runs[0]["eaten"]) if f), None)
        if frame:
            body = frame["head"] + _prompts._GAP + body + (_prompts._GAP + frame["tail"] if frame["tail"] else "")
            lines.append("рамка — как была (снята с заменяемого, надета без изменений)")
        else:
            lines.append("рамки на заменяемом не было — текст как есть")
        wrap = "none"
    first, last = runs[0]["addresses"][0], runs[0]["addresses"][-1]
    lines.append(f"{first}…{last}: {eaten} сообщений → 1, пузырей {len(runs[0].get('kept') or [])}, "
                 f"обёртка `{wrap}`, {len(body)} символов")
    summaries = [{"ok": True, "text": body, "wrap": wrap, "wrap_chosen": chosen,
                  "preset": str(preset or ""), "provider": "llmops", "model": "window",
                  "tokens": 0, "prompt_chars": 0, "input_chars": 0, "path": ""}]
    return _insert._assemble_until_written(session_id, session, agent, rows, by_msg, runs, patch,
                                           hollow, replaced, summaries, None, spec, wanted, facts,
                                           lines)


def _worker(session_id, engine, addrs, text, wrap, preset="") -> None:
    facts, error = {}, ""
    try:
        facts = replace(session_id, engine, addrs, text, wrap, preset)
    except Exception as exc:  # noqa: BLE001 — поток не роняем: человек увидит «ошибка», а не вечное «идёт»
        error = f"{type(exc).__name__}: {exc}"
    finally:
        _progress.finish(session_id, facts, error)
        _insert._busy.release()


def start_replace(session_id: str, engine, addrs: list, text: str, wrap: str,
                  preset: str = "") -> dict:
    """Запустить замену в фоне: запись может ждать конца хода. `{ok: True}` — замок наш, поток
    пошёл; итог — файл состояния (`GET /context-compact-status`, `who: "llmops"`)."""
    if not [a for a in addrs or [] if str(a or "").strip()]:
        return {"ok": False, "error": "nothing to replace: no message address"}
    if not _insert._busy.acquire(blocking=False):
        return {"ok": False, "error": "лента уже переписывается (компакция или другая вставка) — "
                                      "вторая запись не ставится в очередь"}
    _progress.begin(session_id, "llmops")
    try:
        threading.Thread(target=_worker, args=(session_id, engine, list(addrs), text, wrap, preset),
                         daemon=True, name="llmops-replace").start()
    except Exception as exc:  # noqa: BLE001
        _insert._busy.release()
        error = f"поток замены не запущен: {type(exc).__name__}: {exc}"
        _progress.finish(session_id, None, error)
        return {"ok": False, "error": error}
    return {"ok": True, "session": session_id}
