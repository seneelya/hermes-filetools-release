"""Дверь к модели — единственное место, откуда наш код зовёт LLM.

ПОЧЕМУ ОФИЦИАЛЬНАЯ ДВЕРЬ, А НЕ `call_llm`. Их компактор зовёт модуль-функцию
`agent.auxiliary_client.call_llm(...)` — двадцать с лишним аргументов приватной сигнатуры,
шов уровня 1 по нашей же шкале (`ACHTUNG`): ломается молча, без исключения, просто перестав
попадать туда, куда мы просили. У Hermes при этом есть ПУБЛИЧНАЯ дверь ровно под нашу задачу
(`Recon24`, найдено 2026-09-16 по `hermes_cli/plugins.py::PluginContext`):

    ctx.register_auxiliary_task(key, ...)   # свой слот роутинга: `auxiliary.<key>` в конфиге
    ctx.llm.complete(messages=[...], task=key)

ДВЕ ДВЕРИ ОДНИМ API — и это не совпадение, а ровно то, что нужно циклу компакции:

- `task=TASK_KEY` → наш зарегистрированный слот. Туда владелец конфигом сажает ЛЮБУЮ модель
  (суммаризатор не обязан быть главной моделью: ему уходит текст без вызовов инструментов,
  и системный промпт агента ему не нужен).
- `task=None` → активная ГЛАВНАЯ модель, та самая, что сидит агентом. Это канал БУДУЩЕГО
  опроса («какие из этих вызовов тебе ещё нужны?»): ответ имеет смысл только от того, кто
  этим контекстом живёт, а не от постороннего суммаризатора.

ЧЕГО ЭТА ДВЕРЬ НЕ ДАЁТ. `ctx.llm` — ХОЛОДНЫЙ клиент: ни истории сессии, ни системного
промпта агента (`Recon24`, и это же причина существования `_bridge.py`). Для суммаризации это
не потеря — их собственный `_call_summary_llm` шлёт такой же одноразовый промпт `[{"role":
"user"}]`, никак не связанный с префиксом сессии. А вот для ГОРЯЧЕГО опроса модели о её же
истории холодного клиента не хватит, и это работа `Recon28`, не этого цикла.

ПОЧЕМУ КЛЮЧ С ПРЕФИКСОМ. Их регистратор не даст взять встроенный ключ (`compression` и
прочие зарезервированы) и не даст перехватить чужой плагинный — сверяет по `plugin_id`.
Это и есть ответ на «нам нужно отличать наш вызов от их»: маршрут именной, в конфиге он
виден отдельной секцией, и их правка их же `auxiliary.compression` нас не задевает.

МАРШРУТ СЕССИИ — НАША ТРУБКА (2026-09-30). Их `auto` вне хода разрешается без живого маршрута
агента, и после их обновления 2026-09-29 `custom:<имя>` терял запись `custom_providers` и уходил
к первому провайдеру с ключом: кнопка компакции слала сессию в Gemini (`Doc__compaction.md`,
«Модель опроса»). Поэтому
вопросы опроса идут ПРЯМО на сервер модели сессии: адрес, модель, ключ, протокол — у живого
агента (`agent.base_url/model/api_key/api_mode`, те же поля кладёт в маршрут их `turn_context`).
Прогон ставит маршрут на свой поток (`use_route`), без маршрута модель не зовём — отказ словами,
не догадка. Протокол — OpenAI `chat/completions`; другие (`anthropic_messages`, codex) — отказ.
Вид сервера и поля «не думай» — `_server_kind` (`compact_server`), при 400 — откат ступенями.
Сообщения собираются ТОЧНО как у их `_build_structured_messages`: промпт и кеш не меняются.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import urllib.error
import urllib.request

from . import _server_kind, _settings

# Ключ нашего auxiliary-слота. Читается конфигом Hermes как `auxiliary.filetools_compact.*`
# (provider/model/base_url/api_key/timeout/extra_body) — то есть владелец может посадить сюда
# другую модель, не трогая ни главную, ни их компактор.
TASK_KEY = "filetools_compact"

# Reasoning гасим НА УРОВНЕ СЛОТА, а не на каждом вызове: суммаризатору думать вслух незачем,
# а забыть флаг на одном из вызовов — вопрос времени. Форма взята у их `CompressionFastLane`
# (`auxiliary_client.py::resolve_compression_fast_lane`), она же лежит в их дефолтах.
_DEFAULTS = {
    "provider": "auto",
    "model": "",
    "timeout": 180,
    "extra_body": {
        # Форма Hermes/OpenAI — её они сами шлют своему компактору.
        "reasoning": {"enabled": False, "effort": "none"},
        # ФОРМА llama.cpp, и она НЕ ДУБЛЬ предыдущей. В отрисованном промпте живого прогона
        # (2026-09-16) хвост выглядит так:
        #
        #     <|im_start|>assistant
        #     <think>
        #
        # То есть ШАБЛОН МОДЕЛИ сам открывает блок размышления, и поле `reasoning` выше его
        # не закрыло. На Ornith обошлось пустым `reasoning_content`, а на Qwen3 обернётся
        # тем, что модель будет думать над каждым вопросом как над откровением — при
        # пошаговом опросе это умножается на число вопросов.
        #
        # `chat_template_kwargs` — переменные, которые llama.cpp прокидывает В САМ шаблон,
        # и `enable_thinking` шаблоны Qwen читают именно оттуда. Шаблон, который такой
        # переменной не знает, просто её не использует.
        #
        # НЕ ПРОВЕРЕНО ЖИВЬЁМ. Проверять по отрисованному промпту: если `<think>` в хвосте
        # пропал — сработало. Верить ответу модели, а не этому комментарию.
        "chat_template_kwargs": {"enable_thinking": False},
    },
}

# Живой `ctx.llm`. Модульная переменная, а не поле движка, по той же причине, что и у
# `_select`: движок Hermes deepcopy-ит на каждого агента, а клиент модели один на процесс.
# Переживать `/filetools-reload` ей не нужно — перезагрузка даёт новый модуль И заново зовёт
# `setup(ctx)`, который её и кладёт.
_llm = None
_registered = ""

# Маршрут к модели сессии на ЭТОМ потоке (contextvar: у фонового прогона и у хода свои).
_route: contextvars.ContextVar = contextvars.ContextVar("filetools_llm_route", default=None)

# Ступень отката, на которой сервер ответил: (адрес, модель) → номер (`_complete`). Живёт до
# перезапуска процесса: сервер, отвергший поле, отвергнет его и завтра.
_step_of: dict = {}


class _HttpError(RuntimeError):
    def __init__(self, code: int, text: str):
        super().__init__(text)
        self.code = code

_PROTOCOLS = ("", "chat_completions")
_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL | re.IGNORECASE)
_JSON_ONLY = ("Respond with a single JSON object that matches the requested shape. "
              "Do not include prose or markdown fences.")


def route_of(agent) -> dict:
    """Маршрут опроса: наш (`compact_route: own`) или живого агента сессии; `{error}` словами.

    Ключ живёт только в этом словаре, на время прогона: ни в отчёт, ни в лог, ни в файлы.
    Наш маршрут задан, но негоден — ОТКАЗ, не откат на маршрут сессии: иначе проверка чужого
    сервера молча проверила бы свой (`Doc__interrogation-route.md`)."""
    cfg = _settings.compaction()
    if str(cfg.get("compact_route", "session")).strip().lower() == "own":
        return own_route(cfg)
    if agent is None:
        return {"error": "живого агента сессии нет — откройте сессию в клиенте"}
    base = str(getattr(agent, "base_url", "") or "").strip().rstrip("/")
    model = str(getattr(agent, "model", "") or "").strip()
    mode = str(getattr(agent, "api_mode", "") or "").strip().lower()
    if not base:
        return {"error": "у агента сессии нет base_url"}
    if not model:
        return {"error": "у агента сессии нет модели"}
    if mode not in _PROTOCOLS:
        return {"error": f"протокол модели сессии {mode!r} — наш клиент говорит только "
                         "chat_completions"}
    kind, how = _server_kind.kind_of(base, cfg.get("compact_server", "auto"))
    return {"base_url": base, "model": model, "api_key": getattr(agent, "api_key", "") or "",
            "api_mode": mode or "chat_completions", "server": kind, "server_how": how,
            "source": "session"}


def own_route(cfg: dict) -> dict:
    """Наш маршрут из `plugin.yaml`: адрес, модель, ИМЯ переменной окружения с ключом.

    Ключ читается из окружения процесса (Hermes при старте грузит в него `.env` своего дома).
    Имя переменной пустое — ключа нет (локальный сервер); имя есть, а переменной нет — отказ."""
    base = str(cfg.get("compact_route_base_url") or "").strip().rstrip("/")
    model = str(cfg.get("compact_route_model") or "").strip()
    env = str(cfg.get("compact_route_key_env") or "").strip()
    if not base:
        return {"error": "наш маршрут: не задан compact_route_base_url"}
    if not model:
        return {"error": "наш маршрут: не задан compact_route_model"}
    key = ""
    if env:
        key = os.environ.get(env, "").strip()
        if not key:
            return {"error": f"наш маршрут: переменной окружения {env} нет или она пуста "
                             "(ключ ищется в окружении шлюза, `.env` Hermes)"}
    kind, how = _server_kind.kind_of(base, cfg.get("compact_server", "auto"))
    return {"base_url": base, "model": model, "api_key": key, "api_mode": "chat_completions",
            "server": kind, "server_how": how, "source": "own"}


@contextlib.contextmanager
def use_route(agent):
    """Поставить маршрут опроса (наш или сессии, `route_of`) на время прогона. Ошибка маршрута тоже ставится —
    `available()` отдаст её словами, и прогон откажет, не спросив никого другого."""
    token = _route.set(route_of(agent))
    try:
        yield
    finally:
        _route.reset(token)


def _key_value(key) -> str:
    if callable(key) and not isinstance(key, str):
        key = key()
    return str(key or "").strip()


def _post(route: dict, body: dict, timeout: float) -> dict:
    """Один запрос `chat/completions`. Исключения — словами, без ключа в тексте."""
    headers = {"Content-Type": "application/json"}
    try:
        key = _key_value(route.get("api_key"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"ключ модели сессии не получен: {type(exc).__name__}") from None
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(route["base_url"] + "/chat/completions",
                                 data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        with contextlib.suppress(Exception):
            detail = exc.read().decode("utf-8", "replace")[:300]
        raise _HttpError(exc.code, f"HTTP {exc.code} от {route['base_url']}: {detail}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"сервер {route['base_url']} недоступен: {exc.reason}") from None


def _complete(route: dict, messages: list, timeout: float | None, response_format=None) -> dict:
    """Запрос и разбор ответа: `{text, model, tokens, sent}`.

    СТУПЕНИ ОТКАТА. Поля «не думай» (`_server_kind`) и `response_format` — наши добавки, и
    сервер вправе отвергнуть незнакомое поле (400/422). Тогда повторяем: без полей «не думай»,
    затем и без схемы (ответ всё равно проверяем сами — `_parse_json`). Ступень, на которой
    сервер ответил, запоминается на (адрес, модель): следующие вопросы опроса не бьются в ту
    же стену и не рвут префикс кеша разным телом. `sent` — какая ступень ушла, для отчёта."""
    key = (route["base_url"], route["model"])
    extras = _server_kind.no_think(route.get("server", "llamacpp"))
    steps = [("full", extras, response_format), ("no-think-off", {}, response_format),
             ("bare", {}, None)]
    start = _step_of.get(key, 0)
    last = None
    for i, (sent, add, fmt) in enumerate(steps[start:], start):
        if i > start and add == steps[i - 1][1] and fmt == steps[i - 1][2]:
            continue  # ступень ничем не отличается от предыдущей — повторять нечего
        body = {"model": route["model"], "messages": messages, "stream": False, **add}
        if fmt:
            body["response_format"] = fmt
        try:
            data = _post(route, body, float(timeout or _DEFAULTS["timeout"]))
        except _HttpError as exc:
            if exc.code not in (400, 422):
                raise
            last = exc
            continue
        _step_of[key] = i
        break
    else:
        raise last
    return _answer(data, route, sent)


def _answer(data: dict, route: dict, sent: str) -> dict:
    choice = (data.get("choices") or [{}])[0]
    text = str((choice.get("message") or {}).get("content") or "")
    usage = data.get("usage") or {}
    return {"text": text, "model": str(data.get("model") or route["model"]),
            "tokens": int(usage.get("total_tokens") or 0), "sent": sent}


def _parse_json(text: str, schema: dict):
    """Разбор как у них: первый блок в тройных кавычках или весь текст, затем схема."""
    match = _FENCE_RE.search(text)
    raw = match.group(1).strip() if match else text.strip()
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None, ""
    try:
        import jsonschema  # type: ignore[import-untyped]
    except ImportError:
        return parsed, ""
    try:
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as exc:
        return None, f"ответ не совпал со схемой: {exc.message}"
    return parsed, ""


def register(ctx) -> str:
    """Зарегистрировать наш auxiliary-слот и запомнить `ctx.llm`. Строка отчёта, не исключение.

    Отказ здесь НЕ отменяет движок: компакция без модели не работает, а выброс операций из
    контекста (`drop content` / `erase tool`) — вполне, и ронять из-за этого весь `ctxctl`
    было бы обменом рабочего на нерабочее.
    """
    global _llm, _registered
    try:
        ctx.register_auxiliary_task(
            TASK_KEY,
            display_name="filetools: сводка компакции",
            description="Модель, которой уходит текст выбранного диапазона сообщений, "
                        "чтобы вернуть сводку. Не обязана быть главной моделью агента.",
            defaults=dict(_DEFAULTS),
        )
    except Exception as exc:  # noqa: BLE001 — отчёт, не падение
        return f"auxiliary-слот {TASK_KEY!r} НЕ зарегистрирован: {exc}"
    try:
        _llm = ctx.llm
    except Exception as exc:  # noqa: BLE001
        return f"слот {TASK_KEY!r} зарегистрирован, но ctx.llm недоступен: {exc}"
    _registered = TASK_KEY
    return f"auxiliary-слот {TASK_KEY!r} зарегистрирован, ctx.llm на месте"


def available() -> tuple[bool, str]:
    """Можно ли звать модель. Причина словами — она уходит человеку в ответ команды.

    Только по маршруту сессии (`use_route`). Слот `ctx.llm` опрос больше не зовёт: вне хода их
    `auto` угадывает провайдера, и угадал чужое облако (шапка модуля)."""
    route = _route.get()
    if route is None:
        return False, "маршрут к модели сессии не задан — прогон не поставил `use_route`"
    if route.get("error"):
        return False, route["error"]
    who = "наш маршрут" if route.get("source") == "own" else "модель сессии"
    return True, (f"{who} {route['model']} @ {route['base_url']} "
                  f"(сервер {route.get('server', '?')}, {route.get('server_how', '?')})")


def ask_structured(data: str, question: str, schema: dict, *,
                   schema_name: str = "filetools_answer", system: str = "",
                   task: str | None = TASK_KEY, timeout: float | None = None) -> dict:
    """Вопрос К ТЕМ ЖЕ ДАННЫМ, с ответом, ограниченным схемой. Ответ словарём.

    `system` — системный промт, ОДИН на весь прогон (Vision08). Холодный клиент истории не
    имеет: модель не помнит ни прошлого вопроса, ни своего ответа, ни того, что это за
    простыня сверху, — роль и адресата результата ей задаёт только он. Уезжает отдельным
    сообщением ПЕРЕД пользовательским (их `_build_structured_messages`), то есть встаёт в
    самую голову префикса; постоянный — значит кеш от него не страдает.

    ПОЧЕМУ ДАННЫЕ В `instructions`, А ВОПРОС В `input` — наоборот именам, и это нарочно.
    Их `_build_structured_messages` собирает пользовательское сообщение так: сначала
    `instructions`, затем ТЕКСТ СХЕМЫ, затем блоки `input`. То есть в голове оказывается
    то, что передано `instructions`. Нам нужно, чтобы в голове стояло ПОСТОЯННОЕ (данные и
    схема), а менялся только хвост — иначе префикс запроса меняется с первого же символа и
    кеш провайдера не переиспользуется ни разу. Смысл всей схемы «один блок данных, много
    вопросов» именно в этом: десять вопросов подряд должны стоить один префилл.

    ОГРАНИЧЕНИЕ НАСТОЯЩЕЕ, НЕ ПРОСЬБА. `json_schema` уезжает в `extra_body` как
    `response_format: {type: json_schema, ...}`; llama.cpp с `--jinja` превращает её в
    GBNF-грамматику, и декодер не может выдать токен вне схемы. Плюс их сторона валидирует
    возврат через `jsonschema` и БРОСАЕТ при несоответствии — поэтому мы ловим.

    ЧЕГО ЭТА ФУНКЦИЯ НЕ ЗНАЕТ: совпал ли префикс НА САМОМ ДЕЛЕ. Порядок в массиве
    `messages` — не порядок токенов: шаблон модели (jinja) склеивает их по-своему и вправе
    переставить. Проверяется это только отрисованным промптом со стороны сервера, а не
    отсюда. Не считать совпадение доказанным, пока не сверены два дампа.
    """
    ok, why = available()
    if not ok:
        return {"ok": False, "parsed": None, "text": "", "error": why, "task": task}

    route = _route.get()
    # Сборка — КАК У НИХ (`plugin_llm._build_structured_messages`), до символа.
    sys_parts = ([system.strip()] if system else []) + [_JSON_ONLY]
    try:
        schema_text = json.dumps(schema, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        schema_text = str(schema)
    header = f"{data.strip()}\n\nSchema name: {schema_name}\n\nJSON schema:\n{schema_text}"
    messages = [{"role": "system", "content": "\n\n".join(sys_parts)},
                {"role": "user", "content": [{"type": "text", "text": header},
                                             {"type": "text", "text": question}]}]
    response_format = {"type": "json_schema", "json_schema": {
        "name": "plugin_structured_output", "schema": schema, "strict": False}}
    try:
        result = _complete(route, messages, timeout, response_format)
    except Exception as exc:  # noqa: BLE001 — отчёт, не падение
        return {"ok": False, "parsed": None, "text": "", "error": str(exc), "task": task}

    text = result["text"]
    meta = {"provider": route.get("source", "session"), "model": result["model"], "tokens": result["tokens"],
            "server": route.get("server", ""), "sent": result["sent"]}
    parsed, why = _parse_json(text, schema) if text else (None, "")
    if parsed is None:
        # Схема не сработала: модель вернула прозу, а грамматику сервер не применил.
        # Это ФАКТ о сервере, а не сбой нашего кода, и в отчёт он должен попасть как факт.
        return {"ok": False, "parsed": None, "text": text, "task": task,
                "error": why or "ответ не разобран как JSON — схема не была применена", **meta}
    return {"ok": True, "parsed": parsed, "text": text, "error": "", "task": task,
            "content_type": "json", **meta}


def ask(prompt: str, *, system: str = "",
        task: str | None = TASK_KEY, timeout: float | None = None) -> dict:
    """ОДИН вопрос модели одним сообщением. Ответ — словарём, исключений наружу нет.

    `task=TASK_KEY` (default) — наш слот суммаризации. `task=None` — АКТИВНАЯ ГЛАВНАЯ модель
    (см. шапку модуля): это будущий канал опроса, и он тут не для симметрии, а чтобы дверь
    с самого начала была одна. Разводить два почти одинаковых вызова по двум функциям
    значило бы получить два места, где чинить одно и то же.

    `[{"role": "user", "content": prompt}]` — ОДНОРАЗОВАЯ сборка, не история разговора; так
    же устроен и их собственный вызов сводки. `max_tokens` не ставим НИКОГДА: жёсткий cap
    усекает сводку на середине (их комментарий в `_call_summary_llm`, и он оплачен их
    инцидентом — thinking-модели выжигают лимит на рассуждении).
    """
    ok, why = available()
    if not ok:
        return {"ok": False, "text": "", "error": why, "task": task}

    # Системное сообщение здесь собираем САМИ: `complete` принимает готовый список, а
    # `system_prompt` есть только у структурного вызова. Роль `system` первым элементом —
    # то же место в промпте, что у них.
    route = _route.get()
    messages = ([{"role": "system", "content": system}] if system else []) \
        + [{"role": "user", "content": prompt}]
    try:
        result = _complete(route, messages, timeout)
    except Exception as exc:  # noqa: BLE001 — отчёт, не падение
        return {"ok": False, "text": "", "error": str(exc), "task": task}

    text = result["text"].strip()
    meta = {"provider": route.get("source", "session"), "model": result["model"], "tokens": result["tokens"],
            "server": route.get("server", ""), "sent": result["sent"]}
    if not text:
        # Пустой ответ при HTTP 200 — их известный класс беды (прокси отдают 200 с пустым
        # content). Молча записать такое сводкой значит стереть диапазон в никуда.
        return {"ok": False, "text": "", "task": task,
                "error": f"модель вернула пустой ответ ({meta['provider']}/{meta['model']})",
                **meta}
    return {"ok": True, "text": text, "error": "", "task": task, **meta}
