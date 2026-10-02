"""`context_ctl` — наш движок контроля контекста (этап B, `Vision04`/`Vision04.1`).

Одна точка входа для корневого `register(ctx)`: `setup(ctx, plugin_version=...)`. Она сама
решает, включаться ли, и НИКОГДА не бросает — падение движка не имеет права уронить
регистрацию пяти файловых тулов.

ДВА ВЫКЛЮЧАТЕЛЯ, и они про разное:
- `context.engine` в конфиге Hermes — ИХ штатный выбор движка. Пока там не наше имя
  (`ctxctl`), их же `_select_context_engine` даже не спрашивает плагины, так что и
  регистрироваться, и — главное — ставить монкипатч в ядро нам незачем. Ноль следов.
- `context_ctl.enabled` в нашем `plugin.yaml` — наш собственный стоп-кран, чтобы отключить
  движок, НЕ трогая чужой конфиг (и не потеряв в нём наше имя). По умолчанию включён.
"""

from __future__ import annotations

from . import _bridge, _command, _compact, _distill, _llm, _select, _state
from .engine import ContextCtlEngine

handle_drop = _command.handle
handle_compact = _compact.handle
handle_distill = _distill.handle

__all__ = ["ContextCtlEngine", "setup", "format_status", "wanted_by_config",
           "handle_drop", "handle_compact", "handle_distill"]


def wanted_by_config() -> tuple[bool, str]:
    """Стоит ли в конфиге Hermes `context.engine: ctxctl`.

    Читаем ИХ конфиг, а не свой: смысл в том, чтобы наш след в ядре появлялся ровно тогда,
    когда владелец уже выбрала нас их же штатным ключом. Нечитаемый конфиг = «не выбраны»:
    для монкипатча единственный безопасный default — не ставить.
    """
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        section = cfg.get("context") or {}
        name = (section.get("engine") or "compressor") or "compressor"
    except Exception as exc:
        return False, f"конфиг Hermes не прочитан ({exc}) — движок не включаем"
    name = str(name).strip()
    if name != ContextCtlEngine.ENGINE_NAME:
        return False, f"context.engine = {name!r}, не {ContextCtlEngine.ENGINE_NAME!r}"
    return True, f"context.engine = {ContextCtlEngine.ENGINE_NAME!r}"


def setup(ctx, *, plugin_version: str = "", enabled: bool = True,
          config: dict | None = None) -> str:
    """Поставить мост и зарегистрировать движок, если он выбран. Возвращает строку отчёта.

    Строка ПЕЧАТАЕТСЯ в лог загрузки плагина осознанно: ни один отказ здесь не громкий сам
    по себе (не выбран в конфиге, не прошёл контракт швов, отвергнут как второй движок), а
    молчаливое «движка нет» — ровно тот класс поломки, из-за которого существует
    `ACHTUNG__foreign_seams.yaml`.
    """
    if not enabled:
        return "ctxctl: выключен нашим plugin.yaml (context_ctl.enabled: false)"

    wanted, why = wanted_by_config()
    if not wanted:
        return f"ctxctl: не активирован — {why}"

    ok, reason = _bridge.install()
    if not ok:
        # Движок без моста бесполезен НЕ полностью (механизмы 1/2 обходятся без агента),
        # но регистрировать его молча нельзя: сессия останется вообще без компакции.
        # Отказываемся целиком и говорим, почему.
        return (f"ctxctl: НЕ активирован — мост к живому agent не встал: {reason}. "
                "Встроенный компрессор Hermes остаётся на месте.")

    try:
        engine = ContextCtlEngine(plugin_version=plugin_version)
        handle = ctx.register_context_engine(engine)
    except Exception as exc:
        return f"ctxctl: register_context_engine упал: {exc}"
    if handle is None:
        return ("ctxctl: register_context_engine отказал (движок уже кем-то "
                "зарегистрирован — разрешён только один)")

    # Дверь к модели ставится ПОСЛЕ движка и её отказ движок не отменяет: без модели не
    # работает компакция, а выброс операций из контекста работает — менять рабочее на
    # нерабочее из-за недоступного слота нельзя. Поэтому отчёт приклеивается строкой.
    llm_why = _llm.register(ctx)

    # Наши собственные параметры. Кладутся ЗДЕСЬ, потому что секция конфига есть только у
    # корневого `register(ctx)`, а `_compact` зовётся из слеш-команды, где её уже нет.
    #
    # СЕКЦИЯ ПРИХОДИТ АРГУМЕНТОМ, а не читается из `ctx` (правка 2026-09-22). Первая версия
    # звала `ctx.get("compact_distillate_role")` — у плагинного контекста такого метода нет
    # вовсе, есть `get_config(секция, default)`. Исключение ловил соседний `except`, роль
    # молча оставалась `auto`, и живой прогон выдал дистиллят репликой ЧЕЛОВЕКА при
    # `compact_distillate_role: assistant` в конфиге. Настройка, которая молча не
    # применяется, хуже отсутствующей: она создаёт уверенность, что выбор сделан.
    from . import _compact, _settings
    role_why = _compact.set_distillate_role((config or {}).get("compact_distillate_role"))

    # Действующие пределы: plugin.yaml + settings.yaml. Секция `config` этого вызова — только
    # то, что Hermes отдал при загрузке, без правок панели.
    cfg = _settings.compaction()
    auto = "on" if cfg.get("compact_auto_enabled") else "off"
    limits = (f"auto-compaction {auto}, threshold {cfg['compact_auto_percent']}% full"
              f" | head keep {cfg['compact_head_keep']}, tail keep {cfg['compact_tail_keep']},"
              f" cut {cfg['compact_cut_percent']}%")
    return "ctxctl: ACTIVE | " + limits + " | " + llm_why + " | " + role_why


def format_status(_raw_args: str = "") -> str:
    """Человекочитаемый статус для слеш-команды — то, что владелец видит В ИНТЕРФЕЙСЕ.

    Читаем файл, а не движок: обработчик команды и живая сессия держат РАЗНЫЕ копии
    движка (Hermes deepcopy-ит синглтон на каждого агента), поэтому спрашивать движок
    здесь было бы спрашиванием не того объекта.
    """
    wanted, why = wanted_by_config()
    lines = [
        "── ctxctl (движок контроля контекста, Plan13 Task 1) ──",
        f"выбран в конфиге Hermes: {'да' if wanted else 'НЕТ'} — {why}",
        f"мост AIAgent.__init__:   {'стоит' if _bridge.installed() else 'НЕ СТОИТ'}"
        f" (агентов сконструировано: {_bridge.constructed_count()})",
        # Какой чат панель считает открытым. Раньше здесь стоял признак патча на их
        # диспетчере — патча больше нет: панель сессию и так знает, ей достаточно её
        # записать (`POST /context-focus`). Пустая строка означает, что панель ещё ни разу
        # не отчиталась, и тогда выбор сессии падает на угадывание по активности.
        f"открытый чат (от панели): "
        + ((_select.read_focus() or {}).get("session") or
           "не сообщён — панель не открывалась; выбор сессии упадёт на угадывание"),
    ]

    latest = _state.latest_status()
    if latest is None:
        lines.append("статус-файл:             нет ни одного — движок ни разу не стартовал")
        lines.append(f"папка состояния:         {_state.state_dir()}")
        return "\n".join(lines)

    fname, st = latest
    bridge = st.get("bridge") or {}
    prompt = st.get("system_prompt") or {}
    calls = st.get("calls") or {}
    tokens = st.get("tokens") or {}
    lines += [
        f"статус-файл:             {fname}",
        f"сессия:                  {st.get('session_id') or '—'}"
        f" (платформа: {(st.get('session_kwargs') or {}).get('platform', '—')})",
        f"живой agent пойман:      {'ДА' if bridge.get('agent_captured') else 'нет'}"
        f", жив сейчас: {'да' if bridge.get('agent_alive') else 'нет'}"
        f", session_id сходится: {'да' if bridge.get('session_id_matches') else 'нет'}",
        f"поток конструирования:   {bridge.get('construction_thread') or '—'}",
        f"модель/провайдер:        {bridge.get('agent_model') or '—'}"
        f" / {bridge.get('agent_provider') or '—'}",
        f"системный промпт виден:  "
        + (f"ДА, {prompt.get('chars')} символов (sha8 {prompt.get('sha8')})"
           if prompt.get("chars") else "нет (ещё ни одного ответа модели в этой сессии?)"),
        f"вызовы:                  on_session_start={calls.get('on_session_start', 0)},"
        f" update_from_response={calls.get('update_from_response', 0)},"
        f" compress={calls.get('compress', 0)}",
        f"токены:                  prompt={tokens.get('last_prompt')},"
        f" окно={tokens.get('context_length')}",
    ]
    drops = st.get("drops") or []
    if drops:
        lines.append("последние применения /ctx-drop (ответ команды Hermes убирает через "
                     "несколько секунд — здесь он остаётся):")
        for entry in drops[-3:]:
            head = f"  {entry.get('verdict')}"
            stubbed = entry.get("stubbed") or []
            if stubbed:
                head += " | " + ", ".join(
                    f"{row.get('op')} ({row.get('chars_before')}→{row.get('chars_after')})"
                    for row in stubbed)
            if entry.get("tokens_before") is not None:
                head += (f" | токены {entry.get('tokens_before')}"
                         f"→{entry.get('tokens_after')}")
            lines.append(head)
    else:
        lines.append("применений /ctx-drop не было")

    log = st.get("compress_log") or []
    if log:
        lines.append("последние вызовы compress():")
        for entry in log[-3:]:
            lines.append(f"  focus_topic={entry.get('focus_topic')!r}"
                         f" force={entry.get('force')}"
                         f" сообщений={entry.get('n_messages')}"
                         f" → {entry.get('applied')}")
    else:
        lines.append("вызовов compress() пока не было")
    return "\n".join(lines)
