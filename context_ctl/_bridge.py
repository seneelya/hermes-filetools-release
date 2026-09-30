"""Мост к живому объекту `agent` — единственный монкипатч этого плагина в ядро Hermes.

ЗАЧЕМ. Ни одна официальная дверь плагина не отдаёт живой `AIAgent`: у `ContextEngine`
все методы получают только `messages`/`usage`, у хуков (`pre_llm_call` и т.д.) в руках
копия истории, у `ctx.llm` — свой собственный холодный клиент (`Recon24`). А нам он нужен
ровно за одним: `agent._cached_system_prompt` + модель/провайдер, то есть за возможностью
задать модели вопрос НА ТОМ ЖЕ префиксе, что уже лежит в кеше провайдера (`Recon23`,
`Vision03`). Без этого весь экономический смысл опроса модели пропадает.

ЧТО ИМЕННО ПАТЧИМ И ПОЧЕМУ ЭТО. `run_agent.AIAgent.__init__` — тонкий форвардер, он
целиком состоит из `init_agent(self, **init_kwargs)`. Мы заворачиваем его, чтобы положить
`self` в `threading.local` ДО вызова оригинала, и снять после. Внутри оригинала, синхронно,
на том же потоке, вызывается наш собственный официальный хук:

    AIAgent.__init__ -> init_agent -> _inject_context_engine_tools(agent)
                                        -> agent.context_compressor.on_session_start(...)

то есть `ContextCtlEngine.on_session_start` читает thread-local и забирает `weakref` на
живого агента. Вся логика живёт в официальных методах движка; в патче — ноль бизнес-логики.

ПОЧЕМУ `__init__`, А НЕ ДИСПЕТЧЕР ТУЛОВ (как в `self_delegate`). Дрейф подписи здесь ломает
КОНСТРУИРОВАНИЕ агента: падает сразу, громко и на первой же сессии. Патч
`_execute_tool_calls` в той же ситуации устанавливается тихо и потом валится на каждом
батче тулов до конца сессии (`Recon25`, `Recon26`).

ПЕРЕЖИВАЕТ `/filetools-reload`. Перезагрузка плагина даёт НОВЫЙ объект модуля, а патч в
классе Hermes остаётся старый — новый `threading.local` никто бы не заполнял. Поэтому общее
состояние живёт не в модуле, а атрибутом `_ctxctl_state` на самой патч-функции: любая
следующая инкарнация этого модуля находит уже установленный патч и переиспользует его
состояние вместо повторной установки.

Ставится ТОЛЬКО когда владелец сам включил движок (`context.engine: ctxctl` в конфиге
Hermes) — см. `register()` в корневом `__init__.py`. Не включён => в ядре ничего не тронуто.
"""

from __future__ import annotations

import threading
import weakref

# Версия ФОРМЫ общего состояния, не плагина. Меняется только если меняется набор ключей в
# `_ctxctl_state`: старый, уже установленный патч останется в классе после перезагрузки
# плагина, и новый код должен уметь понять, что нашёл несовместимое состояние.
# 2 (Plan13 Task 4) — добавлен реестр живых движков: обработчик слеш-команды получает ТОЛЬКО
# строку аргументов (проверено по их `register_command`/`_hm_dispatch_quick_and_plugin_
# commands`), ни сессии, ни агента, поэтому найти живой движок он может лишь через реестр,
# живущий в процессе. Несовместимость с версией 1 означает «нужен перезапуск Hermes», и это
# правильный ответ: `/filetools-reload` не переставляет патч в классе.
STATE_VERSION = 2


def contract_ok() -> tuple[bool, str]:
    """Проверить швы ПЕРЕД установкой. Никогда не бросает — возвращает причину отказа.

    Три условия, каждое — отдельная причина отказаться, а не упасть:
    1. `run_agent.AIAgent.__init__` есть и вызываем;
    2. `agent.context_engine.ContextEngine` импортируется (иначе движок не собрать);
    3. `agent.agent_init._inject_context_engine_tools` существует — это ровно то звено,
       через которое `on_session_start` вызывается внутри `__init__`. Без него мост есть,
       а забирать агента некому.
    """
    try:
        import run_agent
    except Exception as exc:  # pragma: no cover - нет upstream => плагин и так урезан
        return False, f"run_agent не импортируется: {exc}"
    init = getattr(getattr(run_agent, "AIAgent", None), "__init__", None)
    if not callable(init):
        return False, "run_agent.AIAgent.__init__ не найден"
    try:
        from agent.context_engine import ContextEngine  # noqa: F401
    except Exception as exc:
        return False, f"agent.context_engine.ContextEngine не импортируется: {exc}"
    try:
        from agent import agent_init
    except Exception as exc:
        return False, f"agent.agent_init не импортируется: {exc}"
    if not callable(getattr(agent_init, "_inject_context_engine_tools", None)):
        return False, ("agent.agent_init._inject_context_engine_tools отсутствует — "
                       "звено, вызывающее on_session_start внутри __init__, пропало")
    return True, "ok"


def _state():
    """Общее состояние моста, если патч уже стоит (в т.ч. от прошлой инкарнации модуля)."""
    try:
        import run_agent
        st = getattr(run_agent.AIAgent.__init__, "_ctxctl_state", None)
    except Exception:
        return None
    if isinstance(st, dict) and st.get("version") == STATE_VERSION:
        return st
    return None


def install() -> tuple[bool, str]:
    """Идемпотентно поставить патч. Возвращает `(активен, человекочитаемая причина)`.

    Fail-open по принципу `self_delegate::_contract_ok`: непройденный контракт означает
    «моста нет», а не исключение — плагин обязан загрузиться и без него.
    """
    st = _state()
    if st is not None:
        return True, "мост уже установлен (переиспользую состояние)"

    ok, reason = contract_ok()
    if not ok:
        return False, reason

    import run_agent

    orig = run_agent.AIAgent.__init__
    if getattr(orig, "_ctxctl_state", None) is not None:  # pragma: no cover
        # Состояние есть, но другой версии — не наслаиваем патч на патч.
        return False, "в классе стоит патч моста НЕСОВМЕСТИМОЙ версии; нужен перезапуск Hermes"

    shared = {
        "version": STATE_VERSION,
        "local": threading.local(),
        "orig": orig,
        # Счётчик сконструированных агентов — единственная диагностика, которую можно
        # снять, ничего не удерживая в памяти.
        "constructed": 0,
        # session_id -> живой движок этой сессии. СЛАБЫЕ значения: движок жив ровно пока
        # жив агент (он его атрибут), и сильная ссылка здесь удерживала бы мёртвые сессии
        # в процессе, который живёт неделями.
        "engines": weakref.WeakValueDictionary(),
    }

    def _patched_init(self, *a, **kw):
        local = shared["local"]
        prev = getattr(local, "agent", None)  # вложенное конструирование (форк) — не теряем
        local.agent = self
        shared["constructed"] += 1
        try:
            return shared["orig"](self, *a, **kw)
        finally:
            local.agent = prev

    _patched_init._ctxctl_state = shared
    _patched_init._ctxctl_orig = orig
    run_agent.AIAgent.__init__ = _patched_init

    return True, "мост установлен"



def uninstall() -> bool:
    """Снять патч, если он наш. Для тестов; в живом процессе не используется."""
    try:
        import run_agent
        cur = run_agent.AIAgent.__init__
        orig = getattr(cur, "_ctxctl_orig", None)
        if orig is None:
            return False
        run_agent.AIAgent.__init__ = orig
        return True
    except Exception:
        return False


def installed() -> bool:
    return _state() is not None


def constructed_count() -> int:
    st = _state()
    return int(st["constructed"]) if st else 0


def remember_engine(session_id: str, engine) -> None:
    """Записать движок сессии в процессный реестр. Тихо ничего не делает без моста."""
    st = _state()
    if st is None or not session_id:
        return
    try:
        st["engines"][str(session_id)] = engine
    except Exception:  # pragma: no cover - реестр не имеет права мешать старту сессии
        pass


def live_engines() -> list:
    """`[(session_id, движок)]` для сессий, чей агент ЖИВ. Порядок не определён.

    Живость проверяется по агенту, а не по движку: движок держит агента слабой ссылкой, и
    после закрытия чата он ещё какое-то время существует, но опрашивать по нему уже нечего.
    """
    st = _state()
    if st is None:
        return []
    out = []
    try:
        for session_id, engine in list(st["engines"].items()):
            if engine is not None and engine.live_agent() is not None:
                out.append((session_id, engine))
    except Exception:  # pragma: no cover
        return out
    return out


def capture() -> "weakref.ref | None":
    """Слабая ссылка на агента, который КОНСТРУИРУЕТСЯ на этом потоке прямо сейчас.

    Слабая осознанно: движок живёт столько же, сколько агент (он его атрибут), и сильная
    ссылка замкнула бы цикл, удерживающий мёртвую сессию. `None` = мы не внутри `__init__`
    либо мост не установлен.
    """
    st = _state()
    if st is None:
        return None
    agent = getattr(st["local"], "agent", None)
    if agent is None:
        return None
    try:
        return weakref.ref(agent)
    except TypeError:  # pragma: no cover - AIAgent слабых ссылок не запрещает
        return None
