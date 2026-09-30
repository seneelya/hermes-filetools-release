"""Точный счёт токенов сервером модели (`Plan24`, `Recon32` T9) — и наш зонд одновременно.

Что умеет llama.cpp (и его роутер, с полем `model`):
- `POST /tokenize {content}` — номера токенов токенизатором модели;
- `POST /apply-template {messages, tools}` — промпт, собранный ШАБЛОНОМ модели, со всей разметкой
  вызовов тулов, без генерации;
- `GET /v1/models` — у роутера статус каждой модели (`loaded` / `loading` / `unloaded`) и аргументы
  запуска (оттуда окно модели `--ctx-size`).

ГЛАВНОЕ ПРАВИЛО. Роутер на запрос к НЕзагруженной модели начинает её ЗАГРУЖАТЬ (проверено
2026-09-29 дважды — оба раза нечаянно). Поэтому каждый запрос к модели проходит `_guard`: модель
не `loaded` → отказ `NotLoaded`, если вызывающий явно не сказал `allow_load=True` (это кнопка
`load` в панели, и только она).

ЗОНД. Модуль стоит на стандартной библиотеке и запускается сам, вне Hermes:

    python context_ctl/_tokserver.py status  --base http://192.168.1.40:5000/v1
    python context_ctl/_tokserver.py probe   --base … --model <имя> [--load] [--save]
    python context_ctl/_tokserver.py text    --base … --model <имя> "любой текст"
    python context_ctl/_tokserver.py session --base … --model <имя> <session_id> [--limit N]

`session` — наш счёт против сервера на сообщениях живой сессии (чтение базы только на чтение):
на нём подбираются параметры `_tokens.count_ours`.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

try:
    from . import _tokens
except ImportError:  # запущен как скрипт / загружен по пути — соседа берём по пути
    import importlib.util as _ilu
    from pathlib import Path as _P
    _spec = _ilu.spec_from_file_location("hermes_filetools_ctx_tokens",
                                         _P(__file__).with_name("_tokens.py"))
    _tokens = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_tokens)

TIMEOUT = 60


class NotLoaded(RuntimeError):
    """Модель не загружена, а загружать её не просили."""


class NoServer(RuntimeError):
    """По этому адресу нет llama.cpp-счёта (облако, выключенный сервер)."""


def server_root(base_url: str) -> str:
    """`http://host:5000/v1` → `http://host:5000`: `/tokenize` живёт в корне, не под `/v1`."""
    root = (base_url or "").strip().rstrip("/")
    return root[:-3] if root.endswith("/v1") else root


def _http(root: str, path: str, body: dict | None = None, timeout: float = TIMEOUT):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(root + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise NoServer(f"{path}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise NoServer(f"{path}: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise NoServer(f"{path}: не JSON ({raw[:80]!r})") from exc


_CTX_RE = re.compile(r"(?:^|\n)ctx-size\s*=\s*(\d+)")


def _ctx_of(status: dict) -> int | None:
    args = status.get("args") or []
    for i, arg in enumerate(args):
        if arg in ("--ctx-size", "-c") and i + 1 < len(args):
            try:
                return int(args[i + 1])
            except ValueError:
                return None
    m = _CTX_RE.search(status.get("preset") or "")
    return int(m.group(1)) if m else None


def models(root: str, timeout: float = 10) -> list[dict]:
    """Модели сервера: `{id, status, ctx, template}`. `status` — `None`, если сервер статусов не
    даёт (одиночный llama-server: его модель загружена всегда; облако — счёта нет вовсе)."""
    out = []
    for m in (_http(root, "/v1/models", timeout=timeout).get("data") or []):
        st = m.get("status") if isinstance(m.get("status"), dict) else {}
        args = st.get("args") or []
        tpl = args[args.index("--chat-template-file") + 1] if "--chat-template-file" in args[:-1] else None
        out.append({"id": m.get("id"), "status": st.get("value"), "ctx": _ctx_of(st), "template": tpl})
    return out


def model_state(root: str, model: str) -> dict:
    """`{status, ctx, template}` одной модели; `status: "absent"` — такой модели на сервере нет."""
    for m in models(root):
        if m["id"] == model:
            return m
    return {"id": model, "status": "absent", "ctx": None, "template": None}


def _guard(root: str, model: str, allow_load: bool) -> None:
    if allow_load:
        return
    st = model_state(root, model)["status"]
    # `None` — сервер без статусов: там модель одна и она загружена.
    if st not in ("loaded", None):
        raise NotLoaded(f"{model}: {st}")


def token_ids(root: str, model: str, text: str, allow_load: bool = False) -> list[int]:
    _guard(root, model, allow_load)
    res = _http(root, "/tokenize", {"model": model, "content": text},
                timeout=600 if allow_load else TIMEOUT)
    toks = res.get("tokens")
    if not isinstance(toks, list):
        raise NoServer(f"/tokenize: нет tokens ({str(res)[:80]})")
    return [t["id"] if isinstance(t, dict) else t for t in toks]


def count_text(root: str, model: str, text: str, allow_load: bool = False) -> int:
    return len(token_ids(root, model, text, allow_load))


def render(root: str, model: str, messages: list, tools: list | None = None,
           allow_load: bool = False) -> str:
    """Промпт, собранный шаблоном модели. С хвостом «ответ ассистента» — как в настоящем запросе."""
    _guard(root, model, allow_load)
    body = {"model": model, "messages": messages}
    if tools:
        body["tools"] = tools
    res = _http(root, "/apply-template", body, timeout=600 if allow_load else TIMEOUT)
    if "prompt" not in res:
        raise NoServer(f"/apply-template: нет prompt ({str(res)[:80]})")
    return res["prompt"]


def count_prompt(root: str, model: str, messages: list, tools: list | None = None,
                 allow_load: bool = False) -> int:
    """Точное число токенов запроса целиком. Модель уже проверена `render` — второй раз не спрашиваем."""
    prompt = render(root, model, messages, tools, allow_load)
    return count_text(root, model, prompt, allow_load=True)


# --------------------------------------------------------------------------- обёртки

def _call(cid: str, name: str, args: dict) -> dict:
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def measure_markup(root: str, model: str, allow_load: bool = False) -> dict:
    """Цена разметки шаблона разницами промптов: что добавляет реплика, вызов, параметр.

    `user`      — реплика пользователя сверх её текста;
    `assistant` — реплика ассистента сверх текста (с пустым `<think>`, как Hermes шлёт старые);
    `call`      — вызов тула с одним параметром + его результат, сверх их значений и сверх `assistant`;
    `param`     — каждый следующий параметр сверх его значения.
    """
    c = lambda msgs: count_prompt(root, model, msgs, allow_load=allow_load)  # noqa: E731
    t = lambda s: count_text(root, model, s, allow_load=True)  # noqa: E731 - модель уже проверена `c`
    ua = {"role": "user", "content": "alpha"}
    uc = {"role": "user", "content": "gamma"}
    a = c([ua])
    b = c([ua, uc])
    user = b - a - t("gamma")
    cc = c([ua, {"role": "assistant", "content": "beta"}, uc])
    assistant = cc - b - t("beta")
    one = [ua, {"role": "assistant", "content": "", "tool_calls": [_call("c1", "read_file", {"path": "x.py"})]},
           {"role": "tool", "tool_call_id": "c1", "content": "result"}, uc]
    d = c(one)
    call = d - b - assistant - t("x.py") - t("result")
    two = [ua, {"role": "assistant", "content": "",
                "tool_calls": [_call("c1", "read_file", {"path": "x.py", "limit": "20"})]},
           {"role": "tool", "tool_call_id": "c1", "content": "result"}, uc]
    e = c(two)
    param = e - d - t("20")
    return {"user": user, "assistant": assistant, "call": call, "param": param}


def probe(root: str, model: str, allow_load: bool = False) -> dict:
    """Тестовая строка → отпечаток и стартовый множитель; обёртки шаблона. Для `remember_probe`."""
    ids = token_ids(root, model, _tokens.PROBE_TEXT, allow_load)
    markup = measure_markup(root, model)
    ours = _tokens.count_ours(_tokens.PROBE_TEXT)
    return {"model": model, "fingerprint": _tokens.fingerprint(ids), "probe_tokens": len(ids),
            "probe_ours": ours, "mult": round(len(ids) / ours, 4) if ours else None, "markup": markup}


# --------------------------------------------------------------------------- зонд

def _session_rows(session: str, limit: int) -> list[tuple[str, str]]:
    """`(роль, текст)` активных строк сессии по их правилу: `ORDER BY id`, `api_content` поверх
    `content`. Только чтение."""
    import sqlite3
    db = _tokens.tokens_dir().parent.parent / "state.db"
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        cols = {r[1] for r in con.execute("pragma table_info(messages)")}
        body = "coalesce(api_content, content)" if "api_content" in cols else "content"
        active = " and active = 1" if "active" in cols else ""
        rows = con.execute(f"select role, {body}, tool_calls from messages where session_id = ?"
                           f"{active} order by id", (session,)).fetchall()
    finally:
        con.close()
    out = []
    for role, text, calls in rows[-limit:] if limit else rows:
        out.append((role, (text or "") + _calls_text(calls)))
    return out


def _calls_text(calls) -> str:
    """Вызовы так, как их видит модель: имя, ключи и РАСКОДИРОВАННЫЕ значения аргументов.

    В базе `tool_calls` — JSON с экранированной кириллицей (`\\u041e…`); на проводе шаблон выводит
    значения обычным текстом. Считать экранированное — мерить не то (2026-09-29: на этом
    подобранные параметры оценщика завышали русский текст).
    """
    if not calls:
        return ""
    try:
        items = json.loads(calls) if isinstance(calls, str) else calls
    except ValueError:
        return "\n" + str(calls)
    parts = []
    for c in items if isinstance(items, list) else []:
        fn = (c or {}).get("function") or {}
        parts.append(fn.get("name") or "")
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {"_raw": args}
        for key, val in (args or {}).items() if isinstance(args, dict) else []:
            parts.append(key)
            parts.append(val if isinstance(val, str) else json.dumps(val, ensure_ascii=False))
    return "\n" + "\n".join(parts) if parts else ""


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("cmd", choices=["status", "probe", "text", "session"])
    ap.add_argument("rest", nargs="*")
    ap.add_argument("--base", required=True)
    ap.add_argument("--model")
    ap.add_argument("--load", action="store_true", help="разрешить загрузку модели")
    ap.add_argument("--save", action="store_true", help="probe: записать в файл моделей")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_intermixed_args(argv)
    root = server_root(a.base)
    if a.cmd == "status":
        for m in models(root):
            if m["status"] != "unloaded":
                print(json.dumps(m, ensure_ascii=False))
        return 0
    if not a.model:
        ap.error("нужен --model")
    try:
        if a.cmd == "probe":
            res = probe(root, a.model, a.load)
            print(json.dumps(res, ensure_ascii=False, indent=1))
            if a.save:
                data = _tokens.load_models()
                _tokens.remember_probe(data, a.model, res["fingerprint"], res["probe_tokens"], res["markup"])
                print("saved", _tokens.save_models(data))
        elif a.cmd == "text":
            text = " ".join(a.rest)
            n = count_text(root, a.model, text, a.load)
            print(json.dumps({"server": n, "ours": _tokens.count_ours(text),
                              "bytes4": (len(text.encode("utf-8")) + 3) // 4}))
        elif a.cmd == "session":
            rows = _session_rows(a.rest[0], a.limit)
            tot = {"server": 0, "ours": 0, "bytes4": 0}
            for role, text in rows:
                s = count_text(root, a.model, text, a.load) if text else 0
                o = _tokens.count_ours(text)
                b = (len(text.encode("utf-8")) + 3) // 4
                tot["server"] += s; tot["ours"] += o; tot["bytes4"] += b
                print(f"{role:9} server {s:6}  ours {o:6}  ratio {s / o if o else 0:5.2f}  bytes4 {b:6}")
            k = tot["server"] / tot["ours"] if tot["ours"] else 0
            print(json.dumps({**tot, "mult": round(k, 4), "rows": len(rows)}))
    except NotLoaded as exc:
        print(f"NOT LOADED: {exc} — добавь --load, если грузить можно")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
