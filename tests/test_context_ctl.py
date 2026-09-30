"""Движок контроля контекста (`context_ctl/`, Plan13 Task 1).

Средняя ступень лестницы проверок этого моста, и она здесь не для красоты. Изолированный
скрипт (`__HQ/recon/subjects/context-engine-bridge/`, Task 0) доказал механику один раз,
руками; живой прогон в панели доказывает последнее звено — но ни один из них не побежит
снова при обновлении Hermes. Эти тесты бегут.

ЧТО ИМЕННО ОНИ СТЕРЕГУТ (и почему нельзя проверять «ничего не упало»): весь мост держится
на том, что `AIAgent.__init__` синхронно, на том же потоке, доходит до
`on_session_start` нашего движка. Если upstream уведёт этот вызов в поток или отложит его,
патч встанет как обычно, `capture()` вернёт `None` — и тёплый канал опроса модели просто
тихо перестанет существовать. Поэтому тест утверждает НАЛИЧИЕ пойманного агента, а не
отсутствие исключения.
"""

import copy
import threading

import pytest

from hermes_filetools import context_ctl
from hermes_filetools.context_ctl import _bridge, _signal, _state
from hermes_filetools.context_ctl.engine import ContextCtlEngine

run_agent = pytest.importorskip("run_agent")
agent_init = pytest.importorskip("agent.agent_init")


@pytest.fixture
def bridge():
    """Мост, поставленный на время теста и снятый после."""
    installed_here = not _bridge.installed()
    ok, reason = _bridge.install()
    assert ok, f"мост не встал: {reason}"
    yield _bridge
    if installed_here:
        _bridge.uninstall()


# --- контракт швов ------------------------------------------------------------------


def test_seam_contract_holds():
    """Три звена, без которых моста нет вовсе — проверяются ПО ОТДЕЛЬНОСТИ в contract_ok."""
    ok, reason = _bridge.contract_ok()
    assert ok, reason


def test_inject_context_engine_tools_calls_on_session_start_synchronously():
    """Именно ЭТО звено делает мост возможным: наш хук вызывается внутри `__init__`.

    Проверяем текстом их функции, а не поведением: поведенческий тест ниже поймал бы
    уход в поток по пустому `capture()`, но не сказал бы, ЧТО изменилось.
    """
    import inspect
    src = inspect.getsource(agent_init._inject_context_engine_tools)
    assert "on_session_start" in src, (
        "_inject_context_engine_tools больше не вызывает on_session_start — "
        "мост к живому agent разорван, см. Recon26")
    for hop in ("Thread(", "asyncio", ".submit(", "run_in_executor"):
        assert hop not in src, (
            f"в _inject_context_engine_tools появился {hop} — вызов может уйти с нашего "
            "потока, и threading.local моста окажется пустым")


# --- сам мост ----------------------------------------------------------------------


def test_install_is_idempotent(bridge):
    ok, reason = bridge.install()
    assert ok and "уже установлен" in reason
    assert bridge.installed()


# --- контракт швов ------------------------------------------------------------------


def test_seam_contract_holds():
    """Три звена, без которых моста нет вовсе — проверяются ПО ОТДЕЛЬНОСТИ в contract_ok."""
    ok, reason = _bridge.contract_ok()
    assert ok, reason


def test_inject_context_engine_tools_calls_on_session_start_synchronously():
    """Именно ЭТО звено делает мост возможным: наш хук вызывается внутри `__init__`.

    Проверяем текстом их функции, а не поведением: поведенческий тест ниже поймал бы
    уход в поток по пустому `capture()`, но не сказал бы, ЧТО изменилось.
    """
    import inspect
    src = inspect.getsource(agent_init._inject_context_engine_tools)
    assert "on_session_start" in src, (
        "_inject_context_engine_tools больше не вызывает on_session_start — "
        "мост к живому agent разорван, см. Recon26")
    for hop in ("Thread(", "asyncio", ".submit(", "run_in_executor"):
        assert hop not in src, (
            f"в _inject_context_engine_tools появился {hop} — вызов может уйти с нашего "
            "потока, и threading.local моста окажется пустым")


# --- сам мост ----------------------------------------------------------------------


def test_install_is_idempotent(bridge):
    ok, reason = bridge.install()
    assert ok and "уже установлен" in reason
    assert bridge.installed()


def test_capture_is_none_outside_construction(bridge):
    assert bridge.capture() is None


def _live_agent():
    """Настоящий AIAgent на OpenAI-проводе.

    ``provider="anthropic"`` при инициализации тянет optional-extra ``anthropic``.
    Эти тесты проверяют мост движка, не провайдера.
    """
    return run_agent.AIAgent(
        model="test-model", provider="openrouter",
        api_key="test", base_url="http://127.0.0.1:9/v1",
        quiet_mode=True, skip_memory=True, max_iterations=1,
    )


def test_bridge_hands_live_agent_to_on_session_start(bridge, monkeypatch):
    """Главный тест: движок получает ЖИВОГО агента, того самого, синхронно.

    Подменяем их выбор движка (нам не нужен настоящий конфиг), строим агента и смотрим,
    что движок увидел изнутри `on_session_start`.
    """
    engine = ContextCtlEngine(plugin_version="test")
    monkeypatch.setattr(agent_init, "_select_context_engine", lambda cfg: engine)

    agent = _live_agent()

    live = agent.context_compressor
    assert isinstance(live, ContextCtlEngine)

    st = live.status()
    assert st["bridge"]["agent_captured"], "живой agent не пойман изнутри on_session_start"
    assert live.live_agent() is agent, "поймали не тот объект"
    assert st["bridge"]["session_id_matches"], "session_id хука не совпал с agent.session_id"
    assert st["bridge"]["construction_thread"] == threading.current_thread().name
    assert st["calls"]["on_session_start"] == 1


def test_status_file_written_for_live_session(bridge, monkeypatch):
    """Статус обязан быть в ФАЙЛЕ: панель и слеш-команда держат другую копию движка."""
    engine = ContextCtlEngine(plugin_version="test")
    monkeypatch.setattr(agent_init, "_select_context_engine", lambda cfg: engine)
    agent = _live_agent()

    on_disk = _state.read_status(agent.context_compressor._session_id)
    assert on_disk is not None, "статус-файл не записан"
    assert on_disk["bridge"]["agent_captured"] is True
    assert on_disk["engine"] == "ctxctl"


def test_real_selection_path_deepcopies_us_and_the_copy_still_catches_the_agent(
        bridge, monkeypatch):
    """Продакшн-путь целиком: их конфиг → их `_select_context_engine` → deepcopy → мост.

    Тест выше подменяет выбор движка и потому НЕ проходит через `copy.deepcopy`, которым
    Hermes отдаёт каждому агенту свою копию синглтона. А копия — это и есть живой движок
    сессии, так что ловить агента обязана именно она. Здесь ничего не подменяется, кроме
    их конфига и реестра плагинов: остальное — их настоящий код.
    """
    import hermes_cli.config as hermes_config
    import hermes_cli.plugins as hermes_plugins

    singleton = ContextCtlEngine(plugin_version="test")
    monkeypatch.setattr(hermes_config, "load_config_readonly",
                        lambda: {"context": {"engine": ContextCtlEngine.ENGINE_NAME}})
    monkeypatch.setattr(hermes_plugins, "get_plugin_context_engine", lambda: singleton)

    agent = _live_agent()

    live = agent.context_compressor
    assert isinstance(live, ContextCtlEngine), (
        "их `_select_context_engine` не выбрал нас — либо ключ `context.engine` читается "
        "иначе, либо сравнение с `engine.name` изменилось")
    assert live is not singleton, "ожидали deepcopy синглтона, получили тот же объект"
    assert live.live_agent() is agent, (
        "КОПИЯ движка не поймала живого агента — deepcopy рвёт мост")
    assert singleton.live_agent() is None, "синглтон не должен держать чужого агента"


def test_forced_compress_from_the_host_reaches_our_compress(bridge, monkeypatch):
    """Триггер, которым панель применит выбор (Task 4), — уже сейчас доходит до нас.

    `agent._compress_context(..., force=True)` — тот же вызов, которым пользуется их
    `/compress` (`Recon23`). Живой тест Task 0 показал важное: он идёт ПРЯМО в `compress()`
    и `prune_tool_results_only()` не задевает вовсе — поэтому механизмы 1/2 живут в
    `compress()`. Этот тест стережёт именно это, чтобы находка не потерялась.
    """
    engine = ContextCtlEngine(plugin_version="test")
    monkeypatch.setattr(agent_init, "_select_context_engine", lambda cfg: engine)
    agent = _live_agent()
    live = agent.context_compressor

    pruned = []
    monkeypatch.setattr(type(live), "prune_tool_results_only",
                        lambda self, messages, current_tokens=None: (
                            pruned.append(1) or (messages, 0)))

    messages = [
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "смотрю файл", "tool_calls": [
            {"id": "tc_0012", "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "tc_0012", "content": "тело файла"},
    ]
    out, _system_prompt = agent._compress_context(
        list(messages), None, approx_tokens=1, focus_topic="ctxctl:manual", force=True)

    log = live.status()["compress_log"]
    assert log, "форсированный вызов не дошёл до нашего compress()"
    assert log[-1]["focus_topic"] == "ctxctl:manual" and log[-1]["force"] is True
    assert out == messages, "Task 1 — identity: история обязана вернуться неизменной"
    assert not pruned, ("форсированный путь заглянул в prune_tool_results_only — находка "
                        "Task 0 больше не верна, Plan13 надо перечитать")


# --- движок ------------------------------------------------------------------------


def test_engine_name_is_the_config_key():
    """Имя движка — то самое значение, которое владелец пишет в `context.engine`."""
    assert ContextCtlEngine().name == "ctxctl"
    assert ContextCtlEngine.ENGINE_NAME == "ctxctl"


def test_compress_is_identity_and_records_the_call():
    """Task 1: НИЧЕГО не меняем, но факт вызова с аргументами обязан быть виден.

    Именно этим живой прогон докажет, что кнопка панели (форсированный
    `agent._compress_context(..., force=True)`) доезжает до нашего кода.
    """
    engine = ContextCtlEngine()
    messages = [
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "tc_0012", "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "tc_0012", "content": "тело файла"},
    ]
    out = engine.compress(messages, current_tokens=123, focus_topic="ctxctl:manual",
                          force=True)
    assert out == messages, "Task 1 обязан вернуть вход без изменений"

    entry = engine.status()["compress_log"][-1]
    assert entry["focus_topic"] == "ctxctl:manual"
    assert entry["force"] is True
    assert entry["n_messages"] == 3


def test_no_automatic_compaction_and_it_says_why():
    """Без живого агента решения нет — «нет» с причиной (Plan24-Task07). Их число и их порог
    (`last_prompt_tokens`, `threshold_tokens`) решение не двигают: считаем своим."""
    engine = ContextCtlEngine()
    engine.context_length = 100_000
    engine.threshold_tokens = 1
    engine.last_prompt_tokens = 99_000
    assert engine.should_compress() is False
    fires, reason = engine.should_compress_info()
    assert fires is False and reason and "живого агента нет" in reason  # ошибка — это блокировка


def test_update_from_response_maintains_the_fields_run_agent_reads():
    engine = ContextCtlEngine()
    engine.update_from_response({"prompt_tokens": 10, "completion_tokens": 3,
                                 "total_tokens": 13})
    assert (engine.last_prompt_tokens, engine.last_completion_tokens,
            engine.last_total_tokens) == (10, 3, 13)
    # Мусор в usage не имеет права уронить учёт (их же контракт допускает
    # отсутствие канонических полей на старых хостах).
    engine.update_from_response({})
    assert engine.last_prompt_tokens == 0


def test_deepcopy_keeps_budget_state_and_drops_the_foreign_agent_ref():
    """Копия обязана начать с ЧИСТОЙ ссылкой: агент у неё будет свой.

    Их `_select_context_engine` копирует синглтон именно для этого — чтобы состояние одной
    сессии не протекло в другую.
    """
    engine = ContextCtlEngine(plugin_version="test")
    engine.context_length = 200_000
    engine.threshold_tokens = 150_000
    engine.last_prompt_tokens = 42
    engine._agent_ref = lambda: "чужой агент"
    engine._session_id = "session-A"

    twin = copy.deepcopy(engine)
    assert isinstance(twin, ContextCtlEngine)
    assert (twin.context_length, twin.threshold_tokens, twin.last_prompt_tokens) == (
        200_000, 150_000, 42)
    assert twin.live_agent() is None
    assert twin._session_id == ""


def test_update_model_recomputes_threshold_like_the_abc_promises():
    engine = ContextCtlEngine()
    engine.update_model(model="test-model", context_length=200_000)
    assert engine.context_length == 200_000
    assert engine.threshold_tokens == int(200_000 * engine.threshold_percent)


def test_get_status_shape_survives_the_abc_default():
    """`run_agent.py` читает `get_status()` — базовая реализация должна работать на нас."""
    engine = ContextCtlEngine()
    engine.context_length = 1000
    engine.last_prompt_tokens = 250
    st = engine.get_status()
    assert st["usage_percent"] == 25
    assert set(st) == {"last_prompt_tokens", "threshold_tokens", "context_length",
                       "usage_percent", "compression_count"}


def test_session_sanitizer_matches_the_ledgers():
    """Две копии одного правила обязаны совпадать — иначе два файла на одну сессию.

    `context_ctl/_select.py` и `_signal.py` дублируют `_ledger._safe_session` осознанно: их
    грузят по пути из `dashboard/plugin_api.py`, где относительный импорт невозможен. Этот
    тест — единственное, что держит копии вместе, поэтому имена здесь неудобные нарочно.

    Расхождение уже случалось: в первой копии не было `+` в квантификаторе, и `сессия`
    давала `______` вместо `_` — то есть два файла на одну сессию.
    """
    from hermes_filetools.context_ctl import _select, _signal
    from hermes_filetools.file_ops._ledger import _safe_session as ledger_safe

    for name in ("", "   ", None, "..", "...hidden", "a/b\\c", "20260908_180224_d5f698",
                 "сессия", "a:b*c?d", "..\\..\\etc\\passwd", "x" * 300, ".dot"):
        assert _select._safe_session(name) == ledger_safe(name), name
        assert _signal._safe_session(name) == ledger_safe(name), name


# --- проводка -----------------------------------------------------------------------


class _FakeCtx:
    def __init__(self):
        self.engines = []
        self.config = {}

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_context_engine(self, engine):
        self.engines.append(engine)
        return object()


def test_setup_stays_inert_when_their_config_does_not_select_us(monkeypatch):
    """Ни регистрации, ни — главное — монкипатча, пока владелец не выбрала нас ИХ ключом."""
    monkeypatch.setattr(context_ctl, "wanted_by_config",
                        lambda: (False, "context.engine = 'compressor'"))
    ctx = _FakeCtx()
    report = context_ctl.setup(ctx, plugin_version="test")
    assert ctx.engines == []
    assert "не активирован" in report


def test_setup_respects_our_own_kill_switch():
    ctx = _FakeCtx()
    report = context_ctl.setup(ctx, plugin_version="test", enabled=False)
    assert ctx.engines == []
    assert "выключен" in report


def test_setup_registers_when_selected(monkeypatch, bridge):
    monkeypatch.setattr(context_ctl, "wanted_by_config",
                        lambda: (True, "context.engine = 'ctxctl'"))
    ctx = _FakeCtx()
    report = context_ctl.setup(ctx, plugin_version="test")
    assert len(ctx.engines) == 1
    assert isinstance(ctx.engines[0], ContextCtlEngine)
    assert "АКТИВЕН" in report
    # Отчёт обязан называть цену включения: движок заменяет их компрессор и не сжимает.
    assert "автоматической компакции" in report


def test_setup_refuses_when_the_bridge_cannot_be_installed(monkeypatch):
    """Движок без моста не регистрируем: иначе сессия молча остаётся без компакции вовсе."""
    monkeypatch.setattr(context_ctl, "wanted_by_config",
                        lambda: (True, "context.engine = 'ctxctl'"))
    monkeypatch.setattr(_bridge, "install", lambda: (False, "шов пропал"))
    ctx = _FakeCtx()
    report = context_ctl.setup(ctx, plugin_version="test")
    assert ctx.engines == []
    assert "шов пропал" in report and "остаётся на месте" in report


def test_status_command_reads_the_file_not_the_engine(bridge, monkeypatch):
    """Слеш-команда обязана отвечать по файлу — своей копии живого движка у неё нет."""
    engine = ContextCtlEngine(plugin_version="test")
    monkeypatch.setattr(agent_init, "_select_context_engine", lambda cfg: engine)
    _live_agent()

    text = context_ctl.format_status("")
    assert "живой agent пойман:      ДА" in text
    assert "мост AIAgent.__init__:   стоит" in text


# --- сигнал панели и живучесть журнала ------------------------------------------------


def test_the_revision_only_grows_and_a_missing_file_is_zero(tmp_path, monkeypatch):
    """Ревизия — счётчик, а не время: панель сравнивает число, а не показания часов."""
    monkeypatch.setattr(_signal, "signal_dir", lambda: tmp_path / "applied")
    assert _signal.read_signal("s")["revision"] == 0, "файла нет — это ноль, не исключение"
    assert _signal.bump("s", ["001f"])["revision"] == 1
    assert _signal.bump("s", ["0009", "000b"])["revision"] == 2
    stored = _signal.read_signal("s")
    assert stored["revision"] == 2 and stored["ops"] == ["0009", "000b"]


def test_a_corrupt_signal_reads_as_never_applied(tmp_path, monkeypatch):
    monkeypatch.setattr(_signal, "signal_dir", lambda: tmp_path / "applied")
    path = _signal.signal_path("s")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{битый", encoding="utf-8")
    assert _signal.read_signal("s")["revision"] == 0
    # И следующее применение обязано пройти, а не упереться в мусор.
    assert _signal.bump("s", ["001f"])["revision"] == 1


def test_the_drop_log_survives_a_session_restart(tmp_path, monkeypatch):
    """Перезапуск НЕ имеет права стирать журнал необратимых правок.

    Владелец увидела ровно это 2026-09-09: применение сработало, а в статус-файле стояло
    `"drops": []` — новая копия движка начинала с пустого списка и писала полный снимок
    поверх. Единственное долговечное доказательство правки исчезало.
    """
    monkeypatch.setattr(_state, "state_dir", lambda: tmp_path / "ctxctl")

    first = ContextCtlEngine(plugin_version="test")
    first.on_session_start("s-restart")
    first.note_drop({"verdict": "применено", "stubbed": [{"op": "001f"}]})
    assert (_state.read_status("s-restart") or {}).get("drops")

    second = ContextCtlEngine(plugin_version="test")
    second.on_session_start("s-restart")
    kept = (_state.read_status("s-restart") or {}).get("drops") or []
    assert [d.get("verdict") for d in kept] == ["применено"], kept

    # А своё поверх поднятого дописывается, не затирая.
    second.note_drop({"verdict": "не применено: адресов нет"})
    kept = (_state.read_status("s-restart") or {}).get("drops") or []
    assert len(kept) == 2


# --- открытый чат: панель пишет, команда читает ---------------------------------------
# Владелец 2026-09-09: «патчить гейтвей ради номера сессии — бред, мы же заполняем стакан
# операциями, в которых есть айди сессии». Именно так: панель сессию знает и сама подставляет
# её в каждый маршрут, значит достаточно её записать. Патч диспетчера удалён.


class _Eng:
    name = "ctxctl"

    def __init__(self, sid, last_active=0.0):
        self._session_id = sid
        self._last_active = last_active


def test_the_open_chat_beats_the_activity_guess(tmp_path, monkeypatch):
    """Открытый чат важнее активности — даже если модель громче отвечала в другом."""
    from hermes_filetools.context_ctl import _apply, _select
    monkeypatch.setattr(_select, "select_dir", lambda: tmp_path / "select")
    quiet, loud = _Eng("s-quiet"), _Eng("s-loud", 99.0)
    monkeypatch.setattr(_apply, "human_sessions",
                        lambda: [("s-quiet", quiet), ("s-loud", loud)])

    _select.write_focus("s-quiet")
    session_id, engine, why = _apply.pick_engine()
    assert engine is quiet, "выбрана самая активная вместо открытой в панели"
    assert session_id == "s-quiet" and "открытый чат" in why


def test_an_open_chat_without_our_engine_refuses(tmp_path, monkeypatch):
    """Правка ушла бы туда, где человек не смотрит — значит отказ, а не перебор."""
    from hermes_filetools.context_ctl import _apply, _select
    monkeypatch.setattr(_select, "select_dir", lambda: tmp_path / "select")
    other = _Eng("s-other", 5.0)
    monkeypatch.setattr(_apply, "human_sessions", lambda: [("s-other", other)])
    monkeypatch.setattr(_apply, "_engine_of_gateway_key", lambda key: (None, "нет движка"))

    _select.write_focus("s-fresh")
    _sid, engine, why = _apply.pick_engine()
    assert engine is None and "s-fresh" in why
    assert "s-other" in why, "живые всё равно надо назвать — человеку решать"


def test_no_focus_falls_back_to_the_activity_guess(tmp_path, monkeypatch):
    """Панель не открывалась — старое поведение в силе, и оно при неоднозначности ОТКАЗЫВАЕТ."""
    from hermes_filetools.context_ctl import _apply, _select
    monkeypatch.setattr(_select, "select_dir", lambda: tmp_path / "select")
    a, b = _Eng("s-a"), _Eng("s-b")
    monkeypatch.setattr(_apply, "human_sessions", lambda: [("s-a", a), ("s-b", b)])

    assert _select.read_focus()["session"] == "", "файла ещё нет — это пустая строка"
    _sid, engine, why = _apply.pick_engine()
    assert engine is None and "определить нечем" in why


def test_an_explicit_session_still_wins(tmp_path, monkeypatch):
    from hermes_filetools.context_ctl import _apply, _select
    monkeypatch.setattr(_select, "select_dir", lambda: tmp_path / "select")
    a, b = _Eng("s-a"), _Eng("s-b")
    monkeypatch.setattr(_apply, "human_sessions", lambda: [("s-a", a), ("s-b", b)])
    _select.write_focus("s-b")

    session_id, engine, _why = _apply.pick_engine("s-a")
    assert engine is a and session_id == "s-a"


def test_a_corrupt_focus_file_is_no_focus(tmp_path, monkeypatch):
    from hermes_filetools.context_ctl import _select
    monkeypatch.setattr(_select, "select_dir", lambda: tmp_path / "select")
    path = _select.focus_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{битый", encoding="utf-8")
    assert _select.read_focus()["session"] == ""
    # И следующая запись обязана пройти, а не упереться в мусор.
    assert _select.write_focus("s-ok")["session"] == "s-ok"


def test_nothing_patches_their_dispatcher_any_more():
    """Патч на их диспетчере УДАЛЁН, и это проверяется, а не подразумевается.

    Он существовал ровно для того, чтобы узнать номер сессии, который панель и так знает.
    Если он когда-нибудь вернётся — пусть тест об этом скажет громко.
    """
    from tui_gateway import methods_tools
    assert not getattr(methods_tools._dispatch_plugin, "_ctxctl_dispatch", False)
    from hermes_filetools.context_ctl import _bridge as br
    assert not hasattr(br, "dispatch_session")
    assert not hasattr(br, "_install_dispatch_capture")


def test_a_response_without_usage_still_marks_the_session_alive(tmp_path, monkeypatch):
    """Локальный провайдер не сообщает `usage` — но ответ БЫЛ, и это надо знать.

    `update_from_response` в таком случае не зовётся вовсе
    (`agent/turn_usage.py::record_response_usage`), поэтому у владельца стояли нули и
    `last_active: никогда` при полном разговоре. Время активности нужно фолбэку выбора
    сессии на путях без гейтвея.
    """
    monkeypatch.setattr(_state, "state_dir", lambda: tmp_path / "ctxctl")
    engine = ContextCtlEngine(plugin_version="test")
    engine.on_session_start("s-nousage")
    assert engine.status()["tokens"]["provider_reports_usage"] is True

    engine.note_usage_less_response()
    status = engine.status()
    assert status["tokens"]["provider_reports_usage"] is False
    assert status["last_active"] != "никогда", "ответ был — активность обязана быть"
    assert engine._last_active > 0
    # Токены остаются нулями НАМЕРЕННО: соврать оценкой хуже, чем сказать, что их не дали.
    assert status["tokens"]["last_prompt"] == 0

