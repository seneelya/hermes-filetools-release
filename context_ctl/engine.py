"""`ContextCtlEngine` — наш движок контроля контекста (Plan13, механизм 1 работает).

ЧТО ОН ДЕЛАЕТ:
- честно ведёт учёт токенов, который `run_agent.py` читает напрямую с движка;
- `compress()` — единственная точка, которой мы меняем историю. Ветвится по `focus_topic`:
  наша метка → механизм 1 (заглушки по файлу-выбору); что угодно другое → вход как есть;
- `should_compress()` — решение нашим счётом и порогом (`Plan24-Task07`); «да» только при
  `compact_auto_enabled: true` в нашем `plugin.yaml`, сжатие по «да» — `Plan16` п.1.

ПРО ОТСУТСТВИЕ АВТОМАТИКИ — ЭТО РЕШЕНИЕ, А НЕ НЕДОДЕЛКА. Владелец 2026-09-08
(`DECISIONS.md`, «Направления»): их компакцию НЕ делегируем, идём к своей — «пока не
подтверждено, что работают НАШИ ручные компакты, их компакт нам не нужен вообще». Позже их
механизм может стать фолбэком, отдельным решением. Практическое следствие, с которым живём
осознанно: пока `context.engine: ctxctl` включён, контекст растёт до предела окна, поэтому
на время работы над плагином в конфиге стоит `compressor`, а движок включается на проверки.

МОСТ К ЖИВОМУ АГЕНТУ подтверждён живьём 2026-09-08 на `platform: desktop` (`Recon26`,
`tests/scenarios/Scenario__ctxctl_bridge_live.md`): приложение строит каждую сессию в СВОЁМ
рабочем потоке, и три одновременно живых сессии дали три копии движка с тремя независимыми
захватами. Отсюда правило, которому подчинено всё остальное: наружу сессии состояние живёт
ТОЛЬКО в файлах (`_state.py`, `_select.py`) — у слеш-команды и у панели на руках другая
копия движка, спрашивать её бессмысленно.
"""

from __future__ import annotations

import copy
import threading
import time

from agent.context_engine import ContextEngine

from . import _bridge, _state, _tokcount
from . import _insert

# Как часто перезаписываем статус-файл на «скучных» событиях (учёт токенов). События,
# которые действительно что-то меняют (старт сессии, первый увиденный системный промпт,
# любой вызов compress), пишутся немедленно, без оглядки на этот интервал.
_STATUS_MIN_INTERVAL_S = 5.0

# Сколько последних вызовов compress() держим в статусе. Нужен не журнал, а доказательство
# того, что вызов дошёл и с какими аргументами.
_COMPRESS_LOG_KEEP = 10

# Сколько последних применений `/ctx-drop` держим в статусе — см. `note_drop`.
_DROP_LOG_KEEP = 10


def _settings_value(key: str):
    from . import _settings
    return _settings.compaction()[key]


class ContextCtlEngine(ContextEngine):
    """Движок, регистрируемый через `ctx.register_context_engine`.

    Выбирается конфигом Hermes: `context.engine: ctxctl` (значение сравнивается с `name`).
    """

    ENGINE_NAME = "ctxctl"

    def __init__(self, *, plugin_version: str = ""):
        # Учёт токенов — контракт ABC: «engines MUST maintain these; run_agent.py reads
        # them directly».
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.threshold_tokens = 0
        self.context_length = 0
        self.compression_count = 0

        self.plugin_version = plugin_version
        # Слабая ссылка на живого агента — заполняется мостом в `on_session_start`.
        self._agent_ref = None
        self._session_id = ""
        self._session_kwargs = {}
        self._construction_thread = ""
        self._prompt_seen = None      # {"chars": N, "sha8": "..."} — первый увиденный промпт
        self._compress_log = []
        self._drops = []
        self._last_active = 0.0
        # Провайдер не сообщает `usage` (локальные серверы почти всегда). Ставится их
        # хуком `note_usage_less_response`; без него нули в статусе читаются как «ничего
        # не происходило», хотя разговор шёл.
        self._provider_omits_usage = False
        self._calls = {"on_session_start": 0, "update_from_response": 0,
                       "compress": 0, "on_session_end": 0}
        self._last_status_write = 0.0
        self._lock = threading.Lock()
        # Свой счёт токенов (`Plan24`): якорь из `usage`, наш счёт, точный счёт сервером.
        self._tok = _tokcount.Counter()

    # ---- то, чего требует ABC -------------------------------------------------------

    @property
    def name(self) -> str:
        return self.ENGINE_NAME

    def update_from_response(self, usage) -> None:
        """Учёт токенов после каждого вызова LLM.

        Здесь же — единственное место, где имеет смысл заглядывать за системным промптом:
        на момент `on_session_start` он ещё `None` (его кладёт
        `chat_completion_helpers.py` при сборке первого запроса), а нам важно знать, видим
        ли мы его ВООБЩЕ в живой сессии — от этого зависит вся идея cache-parity опроса.
        """
        try:
            self.last_prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
            self.last_completion_tokens = int(usage.get("completion_tokens", 0) or 0)
            self.last_total_tokens = int(usage.get("total_tokens", 0) or 0)
        except Exception:
            pass
        self._finalize_pending()
        self._tok.note_usage(self.last_prompt_tokens, self.last_completion_tokens)
        self._calls["update_from_response"] += 1
        # Единственный надёжный признак «в этой сессии только что говорили»: обработчик
        # слеш-команды не знает, из какого чата его позвали, и различить несколько живых
        # сессий может лишь по этой метке (десктоп поднимает восстановленные сессии при
        # старте — их бывает сразу несколько, и все они «живые»).
        self._last_active = time.time()
        fresh_prompt = self._note_system_prompt()
        self._write_status(force=fresh_prompt)

    def note_usage_less_response(self) -> None:
        """Ответ модели пришёл, но БЕЗ `usage`. Их хук, и он нам нужнее, чем кажется.

        `update_from_response` зовётся только когда у ответа есть `response.usage`
        (`agent/turn_usage.py::record_response_usage`). Локальные OpenAI-совместимые
        серверы его обычно не отдают — у владельца `custom`/Qwen, и у нас поэтому стояли
        нули и `last_active: никогда` при полном разговоре. Их собственный компрессор на
        этот случай ставит флаг «провайдер не сообщает usage», а нас про ответ вообще
        никто не извещал.

        Что здесь важно: время активности. Оно нужно ФОЛБЭКУ выбора сессии — на путях без
        гейтвея (CLI) сессию вызова узнать нечем, и тогда «где последней отвечала модель»
        остаётся единственным признаком. Токены при этом честно остаются нулями: соврать
        оценкой было бы хуже, чем сказать, что провайдер их не сообщает.
        """
        self._provider_omits_usage = True
        self._last_active = time.time()
        self._calls["usage_less_response"] = self._calls.get("usage_less_response", 0) + 1
        self._write_status()

    def _coerce_threshold_tokens_cap(self, value=None):
        """ОБРАТНЫЙ шов (ACHTUNG `ctxctl.live_config_threshold_cap`): зовут ОНИ, не мы.

        Их `tui_gateway/session_compression.py::_apply_live_compression_config` на старте хода
        переносит `compression.*` в `agent.context_compressor` — у нас это этот движок — и зовёт
        приватный метод своего `ContextCompressor`. Без него — предупреждение в `gui.log` на
        каждую сессию. Потолок порога мы не ведём (решение — `should_compress`, наш порог из
        `plugin.yaml`), поэтому `None` = «потолка нет». Узко, одним именем: ловушка на все чужие
        приватные имена сломала бы их проверки `getattr(cc, "_x", None) is not None`.
        """
        return None

    def should_compress(self, prompt_tokens: int = None) -> bool:
        """Сжимаемся или нет — НАШИМ счётом и НАШИМ порогом (`Plan24-Task07`), не их числом.

        Их автомат зовёт это перед запросом и после результатов тулов (`Recon31` F1), передавая
        СВОЮ оценку (`prompt_tokens`) — её только записываем для сравнения. Решение — по отчёту
        без сети (`_tokcount.decide`): якорь + прирост, окно, `compact_auto_percent`. Пока наш
        ключ `compact_auto_enabled` не включён, ответ «нет»: решение видно (полоса, статус), но
        их автомат ничего не запускает. Сжатие по «да» — `Plan16` п.1.
        """
        fire, _reason = self._decide(prompt_tokens, commit=True)
        return fire

    def should_compress_info(self, prompt_tokens: int = None):
        """`(сжимаемся?, причина БЛОКИРОВКИ)` — БЕЗ побочных действий.

        Их код зовёт это отдельно, чтобы спросить «почему нет», и любую непустую причину
        печатает как «компакция заблокирована … the model may stop responding»
        (`turn_context_compaction._blocked_compress_reason`). Поэтому причина — только у
        настоящей блокировки: пауза после неудач, ошибка счёта. «Рано», «автомат выключен»,
        «в этом ходе уже было» — не блокировка, причины нет. Взводит автозапуск только
        `should_compress` (живьём 2026-09-29: вопрос «почему» не должен менять ход).
        """
        return self._decide(prompt_tokens, commit=False)

    def _decide(self, prompt_tokens, commit: bool):
        self._calls["should_compress"] = self._calls.get("should_compress", 0) + 1
        self._finalize_pending()
        try:
            rep = self.token_report(offline=True)
            d = rep.get("decision") or {}
            if not d:
                return False, f"ctxctl: решения нет — {rep.get('error') or 'отчёт без решения'}"
        except Exception as exc:  # noqa: BLE001 - их конвейер не должен падать из-за счёта
            return False, f"ctxctl: решение не посчитано ({type(exc).__name__}: {exc})"
        gate_ok, gate_why = True, ""
        if d.get("fire") and d.get("enabled"):
            from . import _compact
            gate_ok, gate_why = _tokcount.auto_gate(self._tok.auto, rep.get("turn_key"),
                                                    _insert._busy.locked(), time.time())
        fire = bool(d.get("fire") and d.get("enabled") and gate_ok)
        if fire and commit:
            _tokcount.auto_started(self._tok.auto, rep.get("turn_key"))
        why = ("сжимаемся" if fire else
               ("не сейчас: " + gate_why) if d.get("fire") and d.get("enabled") else
               "сработало бы, но автомат выключен (compact_auto_enabled)" if d.get("fire") else "рано")
        if commit:
            self._tok.last_ask = dict(d, their_tokens=prompt_tokens, answer=fire, why=why)
            self._write_status()
        blocked = (not fire and d.get("fire") and d.get("enabled") and gate_why.startswith("пауза"))
        return fire, (f"ctxctl: {why}" if blocked else None)

    def compress(self, messages, current_tokens=None, focus_topic=None,
                 force=False, memory_context="") -> list:
        """Identity. Мы НЕ меняем историю через этот метод — и это решение, не заглушка.

        Так было не всегда: до 2026-09-08 ручные механизмы 1/2 применялись именно здесь,
        через форсированный `agent._compress_context(...)`. Живой прогон владельца показал,
        почему так нельзя: их конвейер коммитит результат `compress()` через
        `archive_and_compact`, а он архивирует старые строки и вставляет список заново —
        то есть КАЖДОЕ переписанное нами сообщение становится новым «поколением» и в
        интерфейсе уезжает в конец разговора, тогда как его предшественник остаётся на
        месте. Модель видела правильный промпт, человек — перепутанную переписку. Замер и
        разбор — `__HQ/recon/subjects/ctxdrop-row-order/`, механика — в шапке `_apply.py`.

        Поэтому хирургия ушла на `replace_messages` (их же примитив для `/undo`/`/retry`),
        а этот метод остался identity и НЕ трогает вход. Автоматической компакции у нас нет
        по решению владельца (`DECISIONS.md`, 2026-09-08 — замена, не делегирование), а
        суммаризация это механизм 4, отдельный план. Молча вернуть вход — честнее, чем
        сделать с живой историей что-то неожидаемое.
        """
        self._calls["compress"] += 1
        applied = "identity — историю меняет только /ctx-drop через replace_messages, не этот метод"
        out = messages
        # АВТОМАТ (Plan16 п.1, задача E; путь B — `DECISIONS.md`): «да» нашего `should_compress`
        # взвело `pending`. Сжимаем ЗДЕСЬ, синхронно: ход стоит, сервер занят только компакцией;
        # новую ленту отдаём, пишет её Hermes. Ручной `/compress` (`force`) — по-прежнему identity.
        if not force and self._tok.auto.get("pending") and messages:
            self._tok.auto["pending"] = False
            out, applied = self._auto_compact(messages)
        with self._lock:
            self._compress_log.append({
                "at": time.time(),
                "focus_topic": focus_topic,
                "force": bool(force),
                "n_messages": len(messages) if messages is not None else None,
                "n_out": len(out) if out is not None else None,
                "current_tokens": current_tokens,
                "applied": applied,
            })
            del self._compress_log[:-_COMPRESS_LOG_KEEP]
        self._write_status(force=True)
        return out

    def _say(self, text: str) -> None:
        """Строка прогресса: консоль Hermes + событие статуса в десктоп (`Recon31` F9)."""
        agent = self.live_agent()
        emit = getattr(agent, "_emit_status", None) if agent is not None else None
        if callable(emit):
            try:
                emit(f"ctxctl: {text}")
            except Exception:  # noqa: BLE001 - строка статуса не вправе уронить компакцию
                pass

    def _auto_compact(self, messages) -> tuple:
        """Автокомпакция внутри хода: выгрузка → отбор (`_cut.select`) → наша компакция в режиме
        «отдать ленту» → `(новая лента, пояснение)`. Неудача — `(messages, причина)`: их конвейер
        увидит «без изменений» и ничего не запишет (`Recon31` F3).

        Ход — в файл состояния (`_progress`): панель видит, что автомат идёт и на каком он шаге."""
        from . import _progress
        _progress.begin(self._session_id, "auto")
        self._auto_lines = []
        result = (messages, "auto: не выполнено — упало")
        try:
            result = self._auto_compact_run(messages)
            return result
        finally:
            done = result[0] is not messages
            _progress.finish(self._session_id, {"ok": done, "lines": self._auto_lines},
                             "" if done else result[1])

    def _auto_compact_run(self, messages) -> tuple:
        from . import _compact, _progress, _tokcount
        started = time.time()
        pause = _settings_value("compact_auto_pause_minutes")
        self._say("автокомпакция: начинаю")
        _progress.progress(self._session_id, stage="export")
        self._say("выгрузка сессии: " + self._export_before_compaction())
        pick = self.compact_pick()
        if not pick.get("ok"):
            why = "; ".join(pick.get("lines") or []) or "нечего сжимать"
            _tokcount.auto_finished(self._tok.auto, False, why, time.time(), pause)
            self._say("автокомпакция не выполнена — " + why)
            return messages, "auto: не выполнено — " + why
        spec = ",".join(pick["picked"])
        self._say(f"автокомпакция: сжимаю {len(pick['picked'])} сообщений")
        facts = _compact.run(self._session_id, self, spec, deliver={"history": list(messages)})
        self._auto_lines = list(facts.get("lines") or [])
        if not facts.get("delivered"):
            why = (facts.get("lines") or ["без причины"])[-1]
            _tokcount.auto_finished(self._tok.auto, False, why, time.time(), pause)
            self._say("автокомпакция не выполнена — " + why)
            return messages, "auto: не выполнено — " + why
        self._pending_finalize = facts["finalize"]
        self.note_rewrite()
        took = int(time.time() - started)
        why = f"строк {facts['rows_before']} → {facts['rows_after']}, {took} с"
        _tokcount.auto_finished(self._tok.auto, True, why, time.time(), pause)
        self._say("автокомпакция готова — " + why + "; ленту записывает Hermes")
        return facts["new_history"], "auto: " + why

    def _export_before_compaction(self) -> str:
        """Выгрузка всей сессии снимком перед автокомпакцией (задача D): сессия не теряется."""
        try:
            from . import _apply, _compact, _export, _settings
            from ..file_ops._ledger import is_event, read_ledger
            agent = self.live_agent()
            rows, why = _apply.db_history(agent)
            if why:
                return "не сделана — " + why
            by_msg = _insert._index(self._session_id, rows)
            addr_of = {id(r): a for a, r in by_msg.items()}
            addresses = [addr_of.get(id(r), "") for r in rows]
            # Спутник `ledger.jsonl` — записи операций (Plan27-Task01).
            operations = [e for e in (read_ledger(self._session_id) or []) if not is_event(e)]
            report = _export.export(self._session_id, rows, addresses, [a for a in addresses if a],
                                    root=_settings.export_root(), snapshot=True,
                                    operations=operations)
            if report.get("errors"):
                return f"с ошибками ({len(report['errors'])}) — {report.get('dir') or ''}"
            return f"{len(report.get('written') or [])} файлов → {report.get('dir') or '?'}"
        except Exception as exc:  # noqa: BLE001 - без выгрузки сжимать можно, молчать нельзя
            return f"не сделана — {type(exc).__name__}: {exc}"

    def _finalize_pending(self) -> None:
        """Их запись ленты автомата уже прошла — дописываем журнал поглощений и карту."""
        fin, self._pending_finalize = getattr(self, "_pending_finalize", None), None
        if not fin:
            return
        try:
            from . import _compact
            _insert.finalize_delivered(self._session_id, self, fin)
        except Exception:  # noqa: BLE001 - свидетельство неполно, но лента уже записана
            pass

    # ---- официальные необязательные хуки, которые нам нужны ------------------------

    def on_session_start(self, session_id: str, **kwargs) -> None:
        """Старт сессии — и ЕДИНСТВЕННЫЙ момент, когда мост отдаёт живого агента.

        Вызывается синхронно изнутри `AIAgent.__init__`
        (`agent_init._inject_context_engine_tools`), на том же потоке, поэтому
        `_bridge.capture()` читает thread-local, положенный патчем несколькими кадрами
        стека выше. Ничего не бросаем: сорванный мост означает «нет тёплого канала», а не
        «сессия не запустилась».
        """
        self._calls["on_session_start"] += 1
        try:
            captured = _bridge.capture()
        except Exception:
            captured = None
        # ЧУЖОЙ СТАРТ (живьём 2026-09-29, после автокомпакции): наш же движок получил второй
        # `on_session_start` от ДРУГОГО агента, построенного в том же потоке (временный агент
        # вызова модели во время нашей компакции), перехватил ссылку на него — и когда тот умер,
        # движок остался «без живого агента», хотя его собственный агент работал дальше. Живого
        # агента на другой объект не меняем.
        old = self._raw_agent()
        new = captured() if captured is not None else None
        if old is not None and new is not None and new is not old:
            self._calls["foreign_session_start"] = self._calls.get("foreign_session_start", 0) + 1
            self._write_status(force=True)
            return
        self._session_id = session_id or ""
        try:
            self._session_kwargs = {k: str(v) for k, v in kwargs.items()
                                    if k in ("platform", "model", "hermes_home",
                                             "context_length", "conversation_id")}
        except Exception:
            self._session_kwargs = {}
        self._construction_thread = threading.current_thread().name
        self._agent_ref = captured
        # В процессный реестр — иначе слеш-команда нас не найдёт: её обработчик получает
        # только строку аргументов, а движок каждой сессии свой (deepcopy).
        _bridge.remember_engine(self._session_id, self)
        self._adopt_recorded_drops()
        self._write_status(force=True)

    def _adopt_recorded_drops(self) -> None:
        """Поднять прежний журнал применений из файла — иначе перезапуск его СТИРАЕТ.

        `__init__` начинает с пустого списка, а статус пишется полным снимком, поэтому
        восстановление сессии затирало историю правок нулём. Владелец увидела это 2026-09-09:
        применение сработало, а в статусе стояло `"drops": []` — то есть единственное
        долговечное доказательство необратимой правки исчезало при первом же перезапуске.

        Поднимаем ТОЛЬКО если своих записей ещё нет: живая сессия важнее файла.
        """
        if self._drops:
            return
        try:
            recorded = (_state.read_status(self._session_id) or {}).get("drops")
            if isinstance(recorded, list):
                with self._lock:
                    self._drops = [d for d in recorded if isinstance(d, dict)][-_DROP_LOG_KEEP:]
        except Exception:  # noqa: BLE001 - наблюдаемость не имеет права ломать старт
            pass

    def on_session_end(self, session_id: str, messages) -> None:
        self._calls["on_session_end"] += 1
        self._write_status(force=True)

    def __deepcopy__(self, memo):
        """Каждому агенту Hermes отдаёт `deepcopy` синглтона — копируем ТОЛЬКО состояние.

        Их код прямо просит об этом: «Plugin engines that hold uncopyable state (locks, DB
        connections) should implement __deepcopy__ to copy only mutable budget state».
        У нас такое состояние есть (`threading.Lock`, `weakref` на чужого агента), и копия
        обязана начать с чистой ссылкой: агент у неё будет СВОЙ, его принесёт собственный
        `on_session_start`.
        """
        fresh = ContextCtlEngine(plugin_version=self.plugin_version)
        fresh.last_prompt_tokens = self.last_prompt_tokens
        fresh.last_completion_tokens = self.last_completion_tokens
        fresh.last_total_tokens = self.last_total_tokens
        fresh.threshold_tokens = self.threshold_tokens
        fresh.context_length = self.context_length
        fresh.threshold_percent = self.threshold_percent
        fresh.model_thresholds = copy.deepcopy(getattr(self, "model_thresholds", {}), memo)
        memo[id(self)] = fresh
        return fresh

    # ---- наблюдаемость -------------------------------------------------------------

    def note_drop(self, report: dict) -> None:
        """Записать отчёт о применении в статус движка. Зовёт команда, а не движок.

        Нужно потому, что ответ команды ЖИВЁТ НЕДОЛГО: Hermes показывает его как
        временное уведомление и убирает через несколько секунд (уточнение владельца
        2026-09-08 — моя первая версия про «ответ съеден» была неверной). А правка
        контекста необратима, поэтому её исход обязан читаться и ПОСЛЕ того, как
        уведомление исчезло — отдельной командой `/filetools-context-status`, а не только
        в тот момент, когда его успел прочитать человек.
        """
        with self._lock:
            self._drops.append({"at": _state.when(), **(report or {})})
            del self._drops[:-_DROP_LOG_KEEP]
        self._write_status(force=True)

    def apply_addresses(self, operation: str, addrs) -> dict:
        """Операция над m-адресами этой сессии (Plan16-Task08): `content` или `erase`.

        ЕДИНСТВЕННАЯ дверь панели для операций над контекстом (Plan23, 2026-10-06: путь номеров
        операций `apply_drop` / `POST /context-apply` снят). Методом на движке, а не импортом,
        по конкретной причине: дашбордная половина плагина загружается Хермесом ПО ПУТИ, вне
        пакета, — относительный импорт `_command` там невозможен в принципе, а угадывать имя
        нашего пакета в `sys.modules` значило бы завести ещё один приватный шов их
        плагин-лоадера. Живой движок маршрут и так достаёт из реестра гейтвея
        (`_sessions[key]["agent"].context_compressor`), а внутри движка импорты обычные.

        Сессию не выбираем и не угадываем: применяем к той, чей это движок.
        """
        from . import _command
        return _command.run_addresses(self._session_id, self, list(addrs or []), operation)

    def apply_edit(self, addr: str, content: str) -> dict:
        """`paste` (Vision09): заменить текст одного сообщения этой сессии. Та же причина метода."""
        from . import _command
        return _command.run_edit(self._session_id, self, str(addr or ""), content)

    def apply_import(self, folder: str, mode: str = "replace") -> dict:
        """Заменить живую ленту этой сессии строками из папки выгрузки (импорт, 2026-09-29).

        Метод на движке по той же причине, что `apply_addresses`: дашборд грузится по пути.
        """
        from . import _command
        return _command.run_import(self._session_id, self, str(folder or ""),
                                   str(mode or "replace"))

    def start_compact(self, addrs) -> dict:
        """Запустить компакцию В ФОНЕ для НАЗВАННЫХ m-адресов этой сессии (Plan16-Task03).

        Рельса компакции отдаёт голые m-адреса (`m0001`, `m0007`, …) как есть — для неё, в
        отличие от прежнего `apply_drop`, предметом работы являются сами сообщения, а не операции
        (`ops`), и разрешать их во что-то ещё не нужно. `,`-список — та же форма, что уже
        понимает `/ctx-compact m0001,m0007` (`_insert.parse_spec`), никакой новой не заводим.

        Та же причина метода-на-движке, что у `apply_addresses`: дашбордная половина не может
        импортировать `_compact` напрямую (по пути, вне пакета), а живой движок уже достаётся
        из реестра гейтвея тем же маршрутом.

        Отвечает СРАЗУ (`{"ok": True}` — запущено, не «применено»): прогон идёт в фоне
        минутами, его итог смотри в файле состояния (`_compact_state.py`,
        `GET /context-compact-status`), не в этом возврате.
        """
        from . import _compact
        spec = ",".join(str(a).strip() for a in (addrs or []) if str(a or "").strip())
        return _compact.start_background(self._session_id, self, spec)

    def start_ready(self, addrs, text: str, wrap: str, preset: str = "") -> dict:
        """LLMOps (Plan31): поставить ГОТОВЫЙ текст на место m-адресов ВСТАВКОЙ В ЛЕНТУ (`_insert`) —
        операция `llm ops`, не компакция. В фоне, общим замком записи; итог — файл состояния
        (`GET /context-compact-status`, `who: "llmops"`). `wrap` — `compaction` / `summarisation` / `none`;
        `preset` — ключ пресета окна, только для журнала."""
        from . import _llmops
        return _llmops.start_replace(self._session_id, self, list(addrs or []),
                                     str(text or ""), str(wrap or ""), str(preset or ""))

    # ── LLMOps (Plan31 Таск 2): окно зовёт движок тем же приёмом, что `start_compact` —
    # дашбордная половина наш пакет не импортирует. Маршрут к модели — маршрут опроса компакции.
    def llmops_data(self, addrs) -> dict:
        """Данные окна по m-адресам (`_llmops.window_data`)."""
        from . import _apply, _llmops
        agent = self.live_agent()
        if agent is None:
            return {"ok": False, "error": "живого агента нет — сессия не открыта"}
        rows, why = _apply.db_history(agent)
        if why:
            return {"ok": False, "error": why}
        from . import _compact
        by_msg = _insert._index(self._session_id, rows)
        if not by_msg:
            return {"ok": False, "error": "адресов сообщений нет — индексатор не разметил сессию"}
        return _llmops.window_data(self._session_id, rows, by_msg, list(addrs or []))

    def llmops_chat(self, messages, model: str = "", think: bool = False) -> dict:
        """Разговор окна одним запросом (`_llmops.chat`)."""
        from . import _llm, _llmops
        with _llm.use_route(self.live_agent()):
            return _llmops.chat(messages, model, think)

    def llmops_stop(self) -> dict:
        """■ Stop окна: оборвать идущий разговор (`_llmops.stop`)."""
        from . import _llmops
        return _llmops.stop()

    def llmops_models(self) -> dict:
        """Модели сервера маршрута (`_llmops.models`)."""
        from . import _llm, _llmops
        with _llm.use_route(self.live_agent()):
            return _llmops.models()

    def llmops_presets(self) -> list:
        """Пресеты окна (`_prompts/_llmops.py`)."""
        from . import _llmops
        return _llmops.presets()

    def llmops_frames(self) -> dict:
        """Тексты обёрток результата для показа в окне (`_llmops.frame_texts`)."""
        from . import _llmops
        return _llmops.frame_texts()

    def note_rewrite(self) -> None:
        """Наша запись переписала ленту (`_apply.commit_replacement`): якорь и точный замер —
        про СТАРУЮ ленту. Сбрасываем оба, статус пишем сразу (иначе якорь вернулся бы из файла),
        и решение до следующего ответа сервера идёт по оценке всей ленты. Точное число новой
        длины — счётчик посчитает сам, если модель загружена (`exact: 'auto'` на перечитывании)."""
        self._tok.anchor = None
        self._tok.exact = None
        self._tok.rewrites += 1
        self._write_status(force=True)

    def compact_pick(self, share=None) -> dict:
        """Какие сообщения сжимать — ОДИН механизм для всех (владелец, 2026-09-29).

        `_cut.select` с НАШИМИ настройками (`compact_head_keep` / `compact_tail_keep` /
        `compact_cut_percent`, живьём из `plugin.yaml`); `share` — разовая доля кнопки поверх
        настройки. Кнопка кладёт результат галочками на рельсу (человек правит как хочет),
        автомат отдаёт его сразу на выполнение. Ответ: `{"ok", "picked": [m-адреса], "lines"}`.
        """
        from . import _apply, _compact, _cut, _settings
        agent = self.live_agent()
        if agent is None:
            return {"ok": False, "picked": [], "lines": ["живого агента нет — сессия не открыта"]}
        rows, why = _apply.db_history(agent)
        if why:
            return {"ok": False, "picked": [], "lines": [why]}
        by_msg = _insert._index(self._session_id, rows)
        report = _cut.select(rows, _settings.compaction(), share)
        lines = _cut.explain(report)
        if report["index"] < 0:
            return {"ok": False, "picked": [], "lines": lines, "report": report}
        addr_of = {id(row): addr for addr, row in by_msg.items()}
        picked = [addr_of[id(rows[i])] for i in range(report["start"], report["index"] + 1)
                  if id(rows[i]) in addr_of]
        return {"ok": bool(picked), "picked": picked, "lines": lines, "report": report}

    def token_report(self, exact: bool = False, allow_load: bool = False, offline: bool = False) -> dict:
        """Все числа о токенах этой сессии, каждое со своим источником (`_tokcount`).

        `exact` — ещё и точный счёт сервером модели; `allow_load` — можно загрузить модель
        (кнопка `load`). Без `allow_load` к незагруженной модели запросов нет.
        """
        agent = self.live_agent()
        if agent is None:
            return {"error": "живого агента нет — сессия не открыта в этом процессе"}
        if self._tok.anchor is None and not self._tok.rewrites:
            # Якорь переживает перезапуск клиента через статус-файл (живая проверка 2026-09-29:
            # в сессии, где после перезапуска не говорили, `ServerPrompt` был пуст).
            saved = ((_state.read_status(self._session_id) or {}).get("tokens") or {}).get("anchor")
            if isinstance(saved, dict) and saved.get("prompt") and saved.get("at"):
                self._tok.anchor = dict(saved)
        from . import _apply
        rows, note = _apply.db_history(agent)
        return self._tok.report(agent, self, rows, note, exact=exact, allow_load=allow_load,
                                offline=offline)

    def _raw_agent(self):
        ref = self._agent_ref
        return ref() if ref is not None else None

    def live_agent(self):
        """Живой агент этой сессии или None (умер / мост не сработал).

        Ссылка умерла — ищем своего агента в реестре гейтвея ПО ТОЖДЕСТВУ: чей
        `context_compressor` — это мы. Так движок возвращается к агенту, даже если ссылку
        когда-то перехватил чужой старт.
        """
        agent = self._raw_agent()
        if agent is None:
            agent = self._rebind_agent()
        return agent

    def _rebind_agent(self):
        try:
            from tui_gateway import server as gw
            registry = getattr(gw, "_sessions", None) or {}
            for entry in list(registry.values()):
                agent = entry.get("agent") if isinstance(entry, dict) else None
                if agent is not None and getattr(agent, "context_compressor", None) is self:
                    import weakref
                    self._agent_ref = weakref.ref(agent)
                    self._calls["agent_rebound"] = self._calls.get("agent_rebound", 0) + 1
                    return agent
        except Exception:  # noqa: BLE001 - нет гейтвея (CLI, тесты) — агента нет
            return None
        return None

    def _note_system_prompt(self) -> bool:
        """Запомнить факт видимости системного промпта. True — увидели впервые."""
        if self._prompt_seen is not None:
            return False
        agent = self.live_agent()
        prompt = getattr(agent, "_cached_system_prompt", None) if agent is not None else None
        if not prompt:
            return False
        try:
            from ..file_ops._ledger import sha8
            self._prompt_seen = {"chars": len(prompt), "sha8": sha8(prompt)}
        except Exception:
            self._prompt_seen = {"chars": len(prompt), "sha8": ""}
        return True

    def status(self) -> dict:
        """Факты, по которым живой прогон либо доказывает путь, либо нет.

        Нарочно без производных суждений: «мост стоит», «агент виден», «промпт виден» —
        три отдельных факта, потому что провалиться они могут независимо.
        """
        agent = self.live_agent()
        return {
            "engine": self.name,
            "plugin_version": self.plugin_version,
            "session_id": self._session_id,
            "session_kwargs": self._session_kwargs,
            "bridge": {
                "installed": _bridge.installed(),
                "agents_constructed": _bridge.constructed_count(),
                "agent_captured": self._agent_ref is not None,
                "agent_alive": agent is not None,
                "construction_thread": self._construction_thread,
                "agent_model": getattr(agent, "model", "") if agent is not None else "",
                "agent_provider": getattr(agent, "provider", "") if agent is not None else "",
                "agent_platform": getattr(agent, "platform", "") if agent is not None else "",
                "agent_session_id": getattr(agent, "session_id", "") if agent is not None else "",
                "session_id_matches": (
                    bool(self._session_id)
                    and getattr(agent, "session_id", None) == self._session_id
                ) if agent is not None else False,
            },
            "system_prompt": self._prompt_seen or {"seen": False},
            "tokens": {
                "provider_reports_usage": not self._provider_omits_usage,
                "last_prompt": self.last_prompt_tokens,
                "last_completion": self.last_completion_tokens,
                "last_total": self.last_total_tokens,
                "context_length": self.context_length,
                "threshold_tokens": self.threshold_tokens,
                "anchor": self._tok.anchor,
                "decision": self._tok.last_decision,
                "last_ask": self._tok.last_ask,
            },
            "calls": dict(self._calls),
            "last_active": _state.when(self._last_active) if self._last_active else "никогда",
            "compress_log": list(self._compress_log),
            "drops": list(self._drops),
        }

    def _write_status(self, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self._last_status_write) < _STATUS_MIN_INTERVAL_S:
            return
        self._last_status_write = now
        _state.write_status(self._session_id, self.status())
