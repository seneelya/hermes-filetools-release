"""Operation ledger — the on-disk relative of `_drift.py`, and since Plan09
also the SERVICE that hands out operation addresses.

`_drift.py` keeps one number per file in memory and answers "how far could a
line hint have drifted". This module keeps one LINE per read on disk and
answers a question nothing else can answer afterwards: *what exactly did we
hand the model, and when*.

Why it must be written at call time. Hermes stores the tool result in the
`messages` row, but compaction REWRITES that row's `content` in place (see
`__HQ/recon/` on compaction, and `Plan02__panel-data.md`). Once it has run,
the row no longer says what the model originally saw, and no amount of later
analysis recovers it. So the ledger is not a cache of their data — it is the
only surviving reference copy, and the diff between it and the row is exactly
what the panel reports (`verbatim | compacted | evicted | unknown`).

Format: one JSON object per line, `<hermes home>/filetools/ledger/<session>.jsonl`,
append-only, never rewritten. `chars`/`sha8` cover the FULL text we returned
(the same string that lands in `content`), so a shortened row is detectable by
length alone; `lines`/`from`/`to` describe the slice of the FILE that text
showed.

Two files per session, both under that directory: `<session>.jsonl` (the
entries) and `<session>.op` (that session's counter — one decimal number).
The counter is the service's business and nothing outside this module may
compute one: tools ASK for the next address (`next_op`) and hand back their
record (`note_op`). Storage shape, file names and format are internal here on
purpose — the day this becomes a real database, tools do not change.

**Fail-open is a hard rule.** Every public function swallows its own errors.
A read_file that used to touch one file now also touches a log directory, and
a full disk, a locked file or a missing home must not change the model-facing
answer by a single byte — the ledger is observability, `read_file` is the
product.
"""

from __future__ import annotations

import contextvars
import hashlib
import io
import json
import logging
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _home_module():
    """`<корень плагина>/_home.py` — ОДНО правило дома плагина. Пакетом — относительный импорт;
    панель грузит этот файл ПО ПУТИ (пакета нет) — тогда тот же `_home.py` по пути, один на процесс."""
    try:
        from .. import _home
        return _home
    except ImportError:
        import importlib.util
        import sys
        mod = sys.modules.get("hermes_filetools_home")
        if mod is None:
            path = Path(__file__).resolve().parents[1] / "_home.py"
            spec = importlib.util.spec_from_file_location("hermes_filetools_home", path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["hermes_filetools_home"] = mod
            spec.loader.exec_module(mod)
        return mod

# Session ids reach us as opaque strings and become a FILE NAME, so anything
# outside this set is replaced rather than trusted: one "../" would put the
# ledger outside hermes home.
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

# A read we could not attribute to a session still happened; parking it under
# one visible name beats dropping it silently or inventing an id.
UNKNOWN_SESSION = "_unknown"


def _safe_session(session: str | None) -> str:
    name = _SAFE_NAME.sub("_", (session or "").strip())
    # Leading dots are dropped separately: `.` is legal INSIDE a file name but
    # a name that is only dots addresses a directory, not a file.
    name = name.lstrip(".")
    return name or UNKNOWN_SESSION


def ledger_dir() -> Path:
    """Directory holding the per-session JSONL files.

    Resolved on every call, never cached: `hermes_constants.get_hermes_home()`
    is context-local and overridable (tests and profile-aware callers both do
    it), so a module-level constant would pin the first home ever seen.
    """
    return _home_module().plugin_home() / "ledger"


def ledger_path(session: str | None) -> Path:
    return ledger_dir() / f"{_safe_session(session)}.jsonl"


# ---------------------------------------------------------------------------
# Command sidecar (Plan11-Task03). The ledger always carries a TRUNCATED
# command; a body longer than the threshold also gets a file of its own.
#
# Why the full body is worth keeping at all: stage B of Vision04 will REPLACE
# `tool_call.arguments` in the model's context with a stub ("a terminal was
# called here"). Once that happens the ledger is the only place the original
# survives, and the panel can show a human the full script the model no longer
# sees. That is what makes aggressive stubbing acceptable — the information is
# not lost, it moves out of the context and into the panel.
#
# Truncation and the threshold live HERE, not in the callers: eight tools would
# otherwise grow eight slightly different opinions about what "long" means.
# ---------------------------------------------------------------------------

# Enough to be meaningful in a card without the entry becoming the body.
_CMD_HEAD_CHARS_DEFAULT = 200
# Above this, the body also goes to a file. Deliberately larger than the head:
# between the two sits the range where the head IS the whole command and a file
# would be pure overhead.
_CMD_SIDECAR_MIN_CHARS_DEFAULT = 400
_SIDECAR_KEEP_DAYS_DEFAULT = 14
_SIDECAR_MAX_MB_DEFAULT = 200

_config = {
    "cmd_head_chars": _CMD_HEAD_CHARS_DEFAULT,
    "cmd_sidecar_min_chars": _CMD_SIDECAR_MIN_CHARS_DEFAULT,
    "sidecar_keep_days": _SIDECAR_KEEP_DAYS_DEFAULT,
    "sidecar_max_mb": _SIDECAR_MAX_MB_DEFAULT,
}


def configure(**values) -> None:
    """Set service-level options, once per process, from the plugin's entry point.

    `register()` is the only place that holds `ctx.get_config`, and the cores
    that call `note_op` never see it — threading one threshold through eight
    core signatures to reach this module would be the wrong trade.

    Unlike :func:`ledger_dir`, caching IS correct here: hermes home is
    context-local and must be re-resolved on every call, but a threshold is
    operator config and is one per process. Unknown keys are ignored rather
    than raising: a newer config against an older plugin must not break the
    ledger, which is observability.
    """
    for key, value in (values or {}).items():
        if key in _config and value is not None:
            _config[key] = value


def reset_config() -> None:
    """Restore the built-in defaults. For tests, and for a plugin reload that
    wants a clean slate rather than whatever the previous load left behind."""
    _config.update({
        "cmd_head_chars": _CMD_HEAD_CHARS_DEFAULT,
        "cmd_sidecar_min_chars": _CMD_SIDECAR_MIN_CHARS_DEFAULT,
        "sidecar_keep_days": _SIDECAR_KEEP_DAYS_DEFAULT,
        "sidecar_max_mb": _SIDECAR_MAX_MB_DEFAULT,
    })


def sidecar_dir(session: str | None) -> Path:
    """Where one session's full command bodies live.

    Grouped BY SESSION rather than flat, so retiring a whole session is one
    `rmtree` instead of a walk that stats every file. Inside `ledger/` because
    it is one tree that lives and dies with the ledger.
    """
    return ledger_dir() / "cmd" / _safe_session(session)


def sidecar_path(session: str | None, op: str) -> Path:
    return sidecar_dir(session) / f"{_SAFE_NAME.sub('_', op)}.txt"


def read_sidecar(session: str | None, op: str) -> str | None:
    """The full command body, or None if there is none.

    None is a legal, expected answer — the file may never have been written
    (short command, or no operation address) or may have been swept by age. The
    contract for every consumer is the same: no file, no command block at all.
    Not "loading", not "data lost".
    """
    try:
        return sidecar_path(session, op).read_text(encoding="utf-8")
    except Exception:
        return None


def _write_sidecar(session: str | None, op: str, body: str) -> str | None:
    """Store `body`; return the file name to record, or None if we did not.

    No `op` means no sidecar: the file name IS the operation address, and
    without one there is nothing to name the file. Same principle as
    `tool_call_id` — an absence is a fact, an invented name is worse.
    """
    if not op:
        return None
    try:
        target = sidecar_path(session, op)
        target.parent.mkdir(parents=True, exist_ok=True)
        # newline="" for the same reason as the ledger itself: the body must
        # read back byte-identical to what the model actually sent.
        with io.open(target, "w", encoding="utf-8", newline="") as fh:
            fh.write(body)
        return target.name
    except Exception as e:
        logger.debug("filetools ledger: sidecar for %r not written: %s", op, e)
        return None


def sweep_sidecars() -> int:
    """Delete command bodies past the age or size budget, oldest first.

    Returns how many files were removed. Never raises — a missing directory is
    the normal state on a fresh home, not an error.

    Called from `register()` ONLY, i.e. once per process. Not on every write
    (a stat-walk per operation is absurd) and not on a timer (adding one to a
    plugin for the sake of text files is out of proportion). The honest
    consequence: a very long-lived Hermes will not sweep until it restarts.
    That is acceptable because the cost is disk, not correctness — nothing in
    the system depends on whether a sidecar was swept, since "no file" is a
    legal state by contract.
    """
    removed = 0
    try:
        root = ledger_dir() / "cmd"
        if not root.is_dir():
            return 0

        files = []
        for path in root.rglob("*.txt"):
            try:
                stat = path.stat()
            except Exception:
                continue
            files.append((stat.st_mtime, stat.st_size, path))
        files.sort()  # oldest first

        cutoff = time.time() - _config["sidecar_keep_days"] * 86400
        budget = _config["sidecar_max_mb"] * 1024 * 1024
        total = sum(size for _mtime, size, _path in files)

        for mtime, size, path in files:
            too_old = mtime < cutoff
            over_budget = total > budget
            if not (too_old or over_budget):
                break  # sorted oldest first, so nothing later qualifies either
            try:
                path.unlink()
            except Exception:
                continue
            total -= size
            removed += 1

        # Leave no empty session directories behind: they are noise for anyone
        # listing the tree, and an empty dir is not evidence of anything.
        for child in root.iterdir():
            try:
                if child.is_dir() and not any(child.iterdir()):
                    child.rmdir()
            except Exception:
                continue
    except Exception as e:
        logger.debug("filetools ledger: sidecar sweep skipped: %s", e)
    return removed


def sha8(text: str) -> str:
    """Short digest of the text we returned — the cheap "is this still the
    same bytes" check against the stored row."""
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()[:8]


# Are we running INSIDE the execute_code sandbox right now? (Plan11-Task02)
#
# A contextvar rather than a module global: concurrent tool calls each get their
# own view, and the token-based reset restores the previous state exactly even
# if the sandbox call raises.
#
# The marker in `_execute_code_bridge` cannot answer this — it decides the
# FACADE's return shape, and `_make_facade` calls `inner` regardless of it
# (`__HQ/DECISIONS.md`). Neither can `tool_call_id`: every nested call inherits
# the OUTER execute_code's id (see `_observability_context` below), which is
# precisely why nested reads cannot be told apart from their parent after the
# fact and end up `unknown` in `/reads`.
_in_sandbox: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "filetools_in_sandbox", default=False
)


def enter_sandbox():
    """Mark the current context as "inside execute_code"; returns the reset token.

    Caller MUST pass the token to :func:`exit_sandbox` in a `finally`, or the
    flag leaks into whatever runs next in this context.
    """
    return _in_sandbox.set(True)


def exit_sandbox(token) -> None:
    """Undo :func:`enter_sandbox`. Never raises — observability must not be able
    to turn a working tool call into an error."""
    try:
        _in_sandbox.reset(token)
    except Exception:  # noqa: BLE001 - a stale token is not worth an exception
        pass


def _observability_context() -> tuple[str, str, str]:
    """Best-effort `(session_id, tool_call_id, turn_id)` for the running call.

    Reads three PRIVATE contextvars — they have setters but no getters, so
    there is no public way to ask. Deliberately probed instead of copied:
    `model_tools` binds them immediately before dispatch, which is the only
    place the tool_call_id exists at all, and without it a ledger entry cannot
    be matched to its row (it degrades to `state: unknown`).

    TWO import points, the current one first. Upstream moved these out of
    `tools/approval.py` into `tools/approval_context.py` at 6e2b8e070 and did
    NOT re-export them, so our old read started raising AttributeError and this
    function degraded to empty strings — exactly as designed, and exactly as
    invisibly. Found 2026-09-08 from LIVE DATA, not from code: the same day's
    05:58 session had 25 of 52 entries with a tool_call_id, the 18:02 one had
    0 of 30. The fallback keeps an older Hermes working; the order says which
    is current.

    THE LESSON, because the registry did NOT save us: ACHTUNG had already
    recorded the move (`dispatch.observability_contextvars` names the new
    file) and the checker went green — it greps THEIR side, where the symbol
    exists. Nobody re-pointed OURS. A seam entry is only half a seam, so
    `tests/test_ledger.py::test_observability_contextvars_are_reachable` now
    asserts the import point we actually use.

    A rename upstream must degrade to empty strings, never raise.
    """
    import importlib
    for module_name in ("tools.approval_context", "tools.approval"):
        try:
            mod = importlib.import_module(module_name)
            return (
                mod._approval_session_id.get() or "",
                mod._approval_tool_call_id.get() or "",
                mod._approval_turn_id.get() or "",
            )
        except Exception:
            continue
    return ("", "", "")


# ---------------------------------------------------------------------------
# Operation ids (Plan09). The counter belongs to the SERVICE, not to a tool:
# an int incremented inside each tool would have read_file and terminal both
# hand out 0001, and two operations sharing one address is the one thing an
# address may not do.
# ---------------------------------------------------------------------------

# Hex, so the address stays short while staying a plain sequence: four digits
# cover 65k operations in a session and the format simply widens past that.
_OP_WIDTH = 4
_MSG_WIDTH = 4

# session -> last number handed out. A cache in front of the file, not the
# authority: seeded from disk on first use so a reloaded plugin (or a restarted
# process returning to the same session) continues instead of restarting at 1
# and colliding with numbers the model is still holding.
_counters: dict = {}


def _counter_path(session: str | None, kind: str) -> Path:
    """Where one session's counter of `kind` lives. Next to its ledger,
    deliberately: same directory, same lifetime, same fail-open rules."""
    return ledger_dir() / f"{_safe_session(session)}.{kind}"


def op_path(session: str | None) -> Path:
    return _counter_path(session, "op")


def msg_path(session: str | None) -> Path:
    """Счётчик адресов сообщений (`Plan14`, поправка 1 к контракту). Отдельный
    файл, а не общий с операциями: общий счётчик дал бы дыры в нумерации
    операций, а дыра ТАМ уже значит другое — «операция стёрта»."""
    return _counter_path(session, "msg")


def _resolve_session(session: str | None, task_id: str) -> tuple:
    """`(session, source)` — three sources in order of authority.

    The third exists because of a real gap found on live data (2026-08-27): a
    read issued through the execute_code sandbox bridge arrives with NEITHER
    the session_id kwarg NOR the approval contextvars — only task_id survives,
    and in every observed case it equalled the session id. `source` is recorded
    so a consumer cannot mistake an inferred session for an authoritative one.
    """
    ctx_session, _tool_call_id, _turn_id = _observability_context()
    resolved = (session or "").strip()
    if resolved:
        return resolved, "arg"
    if ctx_session:
        return ctx_session, "ctx"
    return (task_id or "").strip(), "task"


def next_op(session: str | None = None, task_id: str = "default") -> str:
    """The next operation address for this session, e.g. `"0156"`.

    Uniqueness is guaranteed, ORDER is not: tools running in parallel inside
    one turn draw from this counter interleaved, so a higher number does not
    mean "later" (live 2026-09-03: four `read_file` calls of one message took
    0005, 0007, 0006, 0008). The number is an address, not a position.

    NO tail. `read_file` used to append its FileTag (`0156xbc`) as a kinship
    hint; dropped 2026-09-03 for two reasons that compound. Measured: asked to
    list its own addresses, the model reproduced 29 of 29 bare numbers and only
    5 of 10 tailed ones — and mangled one of those into a DIFFERENT address
    (`0017xf6` named as `0017`). And it bought nothing: nothing ever read the
    tail, while the same tag is already a header line of its own (`FileTag:`),
    which is where the panel takes it from. A duplicate that halved the recall
    of the thing it decorated.

    Returns `""` when the store cannot be reached, and callers then emit no
    `op:` line at all. Deliberately not a fallback number: a missing address is
    a gap the panel can see, while a duplicate or restarted one silently points
    at the wrong operation.
    """
    try:
        key = _safe_session(_resolve_session(session, task_id)[0])
        return f"{_bump(key, 'op'):0{_OP_WIDTH}x}"
    except Exception as e:
        logger.debug("filetools ledger: no op id for session %r: %s", session, e)
        return ""


def next_msg(session: str | None = None) -> str:
    """Следующий адрес сообщения этой сессии, например `"m0007"`.

    Один плоский накопительный счётчик на сессию — поправка 1 к контракту. Роль
    в адрес НЕ входит: наша будущая сводка ляжет строкой ассистента, репликой
    модели не являясь, и решать её род ради выдачи номера значило бы положить в
    личность то, что можно переосмыслить позже.

    Возвращает `""`, когда счётчик недостижим, — как `next_op`. Пустой адрес
    виден, а повторно выданный указывает на чужое сообщение молча.
    """
    try:
        return f"m{_bump(_safe_session(session), 'msg', reread=True):0{_MSG_WIDTH}x}"
    except Exception as e:
        logger.debug("filetools ledger: no msg id for session %r: %s", session, e)
        return ""


def _bump(key: str, kind: str, reread: bool = False) -> int:
    """Выдать следующий номер счётчика `kind` сессии `key`. Кеш в памяти перед
    файлом, засев с диска при первом обращении — общий механизм для обоих
    родов адресов, чтобы они не разъехались поведением.

    `reread` — СВЕРЯТЬСЯ С ФАЙЛОМ НА КАЖДОМ ШАГЕ, и это не осторожность впрок.
    Этот модуль живёт в процессе гейтвея В ДВУХ ЭКЗЕМПЛЯРАХ: движок импортирует
    его пакетом, дашборд грузит ПО ПУТИ (иначе не умеет — его самого так грузят).
    Кеши у них разные, файл один. Операциям это не вредило: номера выдаёт только
    движок. Адреса сообщений раздают ОБА — маршрут `/messages` и `_command.run`, —
    и экземпляр с отставшим кешем выдал бы номер, уже названный другим. Поймано
    тестом `test_r3b` 2026-09-09: второй проход маршрута выдал `m000a` там, где
    в файле стояла единица.
    """
    number = _counters.get((kind, key))
    if number is None or reread:
        number = max(number or 0, _read_counter(key, kind))
    number += 1
    _counters[(kind, key)] = number
    _write_counter(key, number, kind)
    return number


def _read_counter(session: str, kind: str = "op") -> int:
    """Where this session's numbering stands. 0 when it has not started.

    Deliberately does NOT swallow a read failure, unlike everything else here:
    a counter file that EXISTS but cannot be read means the session is already
    under way at a position we cannot see, and guessing 0 there would hand out
    an address the model is still holding. That propagates to `next_op`, which
    then emits no address at all — a visible gap instead of a wrong pointer.
    A missing file is not that case: it means nobody has been numbered yet.
    """
    target = _counter_path(session, kind)
    if not target.exists():
        return 0
    return int(target.read_text(encoding="utf-8").strip() or 0)


def _write_counter(session: str, number: int, kind: str = "op") -> None:
    """Write-through on every increment. One tiny write per operation, the same
    order of cost as the ledger append we already do, and it is what makes the
    number survive a `/filetools-reload` mid-session.

    A failure here does NOT cancel the address: the in-memory counter has
    already advanced, so numbers stay unique for as long as this process
    lives, which is the span the model's context covers. What is lost is
    survival across a restart — the plan's accepted risk 2, not a new one.
    """
    try:
        target = _counter_path(session, kind)
        target.parent.mkdir(parents=True, exist_ok=True)
        with io.open(target, "w", encoding="utf-8", newline="") as fh:
            fh.write(str(number))
    except Exception as e:
        logger.debug("filetools ledger: could not persist the op counter: %s", e)


def note_read(**kwargs) -> None:
    """Append one entry for a successful read.

    Thin wrapper over :func:`note_op` kept because reads were here first and
    are still the majority of entries; `tool` defaults to `read_file` so old
    entries (written before the field existed) and new ones read alike.
    """
    kwargs.setdefault("tool", "read_file")
    note_op(**kwargs)


# ── Надгробия: запись о том, что операции больше НЕТ (Plan13) ────────────────
#
# ЗАЧЕМ ОТДЕЛЬНАЯ ЗАПИСЬ, ЕСЛИ ЗАПИСЬ ОБ ОПЕРАЦИИ И ТАК ОСТАЁТСЯ. Леджер append-only, и
# после `erase tool` он единственный, кто помнит, что операция была: строки в базе нет
# вовсе. Но «записи есть, строки нет» — это ДВА разных факта с одним симптомом: либо мы
# стёрли операцию нарочно, либо строку потеряли (компакция переписала текст, сессия из
# окна без `tool_call_id`, чужая правка). Панель обязана показывать их по-разному:
# нарочно стёртая получает надгробие — серую некликабельную карточку `#L7`, — а потерянная
# просто уходит с экрана, потому что сказать о ней нечего.
#
# Различить их можно ТОЛЬКО записью. Владелец 2026-09-09: «операция 0014 стёрта, тогда-то»
# — у нас для этого даже формат был заведён. Отсюда и `event`: обычные записи этого ключа
# не несут, поэтому старый читатель их не спутает, а новый разделит одной проверкой.
#
# ПРАВИЛО: неудачная запись надгробия НЕ отменяет применения. Надгробие — свидетельство, а
# не сама операция; строки в контексте уже нет, и падать здесь значило бы соврать вызывающему
# об исходе необратимой правки.
EVENT_ERASED = "erased"


def note_erased(session: str | None, op: str, by: str = "erase tool") -> None:
    """Записать, что операция `op` этой сессии стёрта. Никогда не бросает."""
    try:
        if not str(op or "").strip():
            return
        entry = {
            "ts": time.time(),
            "event": EVENT_ERASED,
            # СЫРОЙ идентификатор, как его пишет `note_op`, а НЕ имя файла. Санитайзинг —
            # дело `ledger_path`; положи мы сюда безопасное имя, поле `session` у надгробия
            # разошлось бы с полем `session` у записи об операции в том же файле, и grep по
            # журналам начал бы находить одну сессию под двумя именами.
            "session": str(session or "").strip(),
            "op": str(op).strip(),
            # Каким действием стёрта. Сегодня одно (`erase tool`), завтра будет `erase turn`
            # — и надгробию придётся сказать, какого масштаба была правка.
            "by": str(by or "erase tool"),
        }
        _append(entry, session)
    except Exception as e:
        logger.debug("filetools ledger: skipped a tombstone for %r: %s", op, e)


def is_event(entry) -> bool:
    """Запись СОБЫТИЯ (надгробие), а не операции.

    Единственное место, где знание «событие отличается ключом `event`» превращается в код:
    читателей у леджера много (`/ops`, `/reads`, `/context-state`, движок), и каждый из них,
    решая это самостоятельно, однажды решил бы иначе.
    """
    return isinstance(entry, dict) and bool(entry.get("event"))


def erased_ops(entries) -> dict:
    """`op -> надгробие` по записям сессии. Последнее надгробие на операцию побеждает."""
    out = {}
    for entry in entries or []:
        if not is_event(entry) or entry.get("event") != EVENT_ERASED:
            continue
        op = str(entry.get("op") or "").strip()
        if op:
            out[op] = entry
    return out


# ── Поглощение: какие сообщения уехали в какую сводку (Plan16) ───────────────
#
# ЗАЧЕМ. Компакция — единственная наша правка, после которой источника не остаётся ВОВСЕ:
# `erase` убирает одну пару и оставляет соседей, а здесь десяток сообщений заменяется одним.
# Через минуту сказать, ЧТО именно уехало, сможет только эта запись: строк нет, их адреса
# больше ни на что не указывают, а текст сводки про свои источники не помнит.
#
# ПОЧЕМУ ПИШЕТСЯ ПОСЛЕ КОММИТА. Ключ записи — адрес СВОДКИ, а его не существует, пока
# сводка не легла в базу и индексатор не выдал ей место. Записывать раньше значило бы
# записывать намерение, а `event`-записи у нас описывают только состоявшееся.
#
# ПРОВЕРЕНО ПРОТИВ ЧИТАТЕЛЕЙ (2026-09-16, `grep -rn "is_event"`): все шестеро либо
# пропускают события целиком, либо сверяют конкретное значение `event` — новый род им
# невидим. Это ровно та проверка, которой не было, когда мы обожглись на надгробиях.
EVENT_ABSORBED = "absorbed"


def note_absorbed(session: str | None, summary: str, members,
                  by: str = "compact") -> bool:
    """Записать, что сводка `summary` поглотила сообщения `members`. Никогда не бросает.

    `summary` и `members` — АДРЕСА сообщений (`m0015`, `["m0002", …]`), не `id` базы: `id`
    пересоздаётся при каждой правке истории, адрес её переживает — тот же довод, по которому
    вся адресация построена на координате.
    """
    try:
        summary = str(summary or "").strip()
        members = [str(m).strip() for m in (members or []) if str(m).strip()]
        if not summary or not members:
            return False
        return _append({
            "ts": time.time(),
            "event": EVENT_ABSORBED,
            "session": str(session or "").strip(),
            "summary": summary,
            "members": members,
            # Каким действием. Сегодня одно (`compact`), но у надгробий это поле уже
            # пригодилось, когда действий стало три.
            "by": str(by or "compact"),
        }, session)
    except Exception as e:  # noqa: BLE001 — свидетельство не отменяет работы
        logger.debug("filetools ledger: skipped an absorption record for %r: %s",
                     summary, e)
        return False


def absorptions(entries) -> dict:
    """`адрес сводки -> запись` по журналу сессии. Последняя запись на адрес побеждает."""
    out = {}
    for entry in entries or []:
        if not is_event(entry) or entry.get("event") != EVENT_ABSORBED:
            continue
        key = str(entry.get("summary") or "").strip()
        if key:
            out[key] = entry
    return out


# ── Правка: сообщение заменено текстом из буфера (`paste`, Vision09) ─────────
#
# Связь один-к-одному: было → стало. Старого текста в записи НЕТ — заархивированная строка
# лежит в базе целиком и сама свидетельство (Vision09, «Запись в леджер»). Пишется ПОСЛЕ
# коммита: адрес новой строки выдаёт переиндексация. Читатели журнала новый род не видят —
# все они либо пропускают события, либо сверяют конкретное `event` (та же проверка, что у
# `absorbed`).
EVENT_EDITED = "edited"


def note_edited(session: str | None, was: str, now: str, by: str = "human") -> bool:
    """Записать, что сообщение `was` заменено сообщением `now`. Никогда не бросает."""
    try:
        was = str(was or "").strip()
        now = str(now or "").strip()
        if not was or not now:
            return False
        return _append({"ts": time.time(), "event": EVENT_EDITED,
                        "session": str(session or "").strip(),
                        "was": was, "now": now, "by": str(by or "human")}, session)
    except Exception as e:  # noqa: BLE001 — свидетельство не отменяет работы
        logger.debug("filetools ledger: skipped an edit record for %r: %s", was, e)
        return False


# ── Сообщения и состав вагона (Plan14-Task03) ───────────────────────────────
#
# Формы заморожены в `__HQ/CONTRACT__ledger-records.md` §5 и поправке 1. Здесь только
# запись; кто и когда её порождает — `file_ops/_msgindex.py`.
#
# ПОЧЕМУ КООРДИНАТА, А НЕ ИХ `id`. `id` пересоздаётся при каждой правке истории —
# замерено, одно сообщение под семью `id` в одной сессии. `msg_ts` пересоздание
# переживает, а дайджест разводит две редакции одного места.

EVENT_MESSAGE = "message"
EVENT_LAYOUT = "layout"


def _append(entry: dict, session: str | None) -> bool:
    """Дописать одну строку в журнал сессии. Никогда не бросает.

    Общий хвост всех писателей: три записи подряд копировали одни и те же пять
    строк, и любая правка (например смена кодировки) неминуемо разошлась бы.
    """
    try:
        target = ledger_path(session)
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False)
        with io.open(target, "a", encoding="utf-8", newline="") as fh:
            fh.write(line + "\n")
        return True
    except Exception as e:
        logger.debug("filetools ledger: skipped a %r entry: %s",
                     entry.get("event"), e)
        return False


def note_message(
    *,
    session: str | None,
    msg: str,
    role: str,
    msg_ts,
    sha256: str,
    session_src: str = "arg",
    tool_call_id: str = "",
    parent_session: str = "",
) -> bool:
    """Записать, что сообщению с такой координатой выдан наш адрес.

    Запись — ИНДЕКС, а не наблюдение: она означает «в базе этой сессии есть или
    было сообщение с такой координатой, и оно зовётся так». Она НЕ означает
    «сообщение сейчас в промпте» — на это отвечает вердикт.

    Необязательные ключи ОТСУТСТВУЮТ, а не приходят пустыми (контракт §1).
    """
    try:
        if not str(msg or "").strip() or not str(role or "").strip():
            return False
        entry = {
            "ts": time.time(),
            "event": EVENT_MESSAGE,
            "session": str(session or "").strip(),
            "session_src": str(session_src or "arg"),
            "msg": str(msg).strip(),
            "role": str(role).strip(),
            "msg_ts": msg_ts,
            "sha256": str(sha256 or ""),
        }
        if str(tool_call_id or "").strip():
            entry["tool_call_id"] = str(tool_call_id).strip()
        if str(parent_session or "").strip():
            entry["parent_session"] = str(parent_session).strip()
        return _append(entry, session)
    except Exception as e:  # noqa: BLE001 - свидетельство не отменяет работы
        logger.debug("filetools ledger: skipped a message record: %s", e)
        return False


def note_layout(
    *,
    session: str | None,
    opener: str | None,
    members,
    steps=None,
    note: str = "",
) -> bool:
    """Записать состав одного вагона: `opener` — адрес открывающей реплики
    человека (`None` у вагона 0), `members` — упорядоченные адреса, `steps` —
    разбиение тех же адресов на шаги (поправка 4).

    Состав ЗАПИСАН, а не выводится: вывести его из структуры базы можно ровно
    до первой нашей правки, а после стирания или сводки группировка поедет.
    """
    try:
        entry = {
            "ts": time.time(),
            "event": EVENT_LAYOUT,
            "session": str(session or "").strip(),
            "opener": str(opener).strip() if opener else None,
            "members": [str(m).strip() for m in (members or []) if str(m).strip()],
        }
        grouped = [[str(m).strip() for m in (step or []) if str(m).strip()]
                   for step in (steps or [])]
        grouped = [s for s in grouped if s]
        if grouped:
            entry["steps"] = grouped
        if str(note or "").strip():
            entry["note"] = str(note).strip()
        return _append(entry, session)
    except Exception as e:  # noqa: BLE001
        logger.debug("filetools ledger: skipped a layout record: %s", e)
        return False


def message_records(entries) -> list:
    """Все записи о сообщениях, в порядке журнала."""
    return [e for e in (entries or [])
            if is_event(e) and e.get("event") == EVENT_MESSAGE]


# Ключ идемпотентности `(role, msg_ts, sha256)` СОБИРАЕТСЯ НЕ ЗДЕСЬ, а в
# `_msgindex.entry_coord`: координату надо приводить к одному типу с обеих сторон
# сверки (из базы приходит число, из JSON — что придётся), и второй способ строить
# тот же ключ разошёлся бы с первым молча.


def layouts(entries) -> dict:
    """`opener -> запись состава`. Последняя запись на вагон побеждает — то же
    правило, что у надгробий. Вагон 0 живёт под ключом `None`."""
    out = {}
    for entry in entries or []:
        if not is_event(entry) or entry.get("event") != EVENT_LAYOUT:
            continue
        out[entry.get("opener") or None] = entry
    return out


def note_op(
    *,
    text: str,
    path: str = "",
    tool: str = "read_file",
    session: str | None = None,
    task_id: str = "default",
    from_line: int = 0,
    to_line: int = 0,
    lines: int = 0,
    total: int = 0,
    out_lines: int = 0,
    command: str = "",
    counts: dict | None = None,
    op: str = "",
) -> None:
    """Append one entry for a successful operation. Never raises, never returns
    anything the caller could be tempted to put in the model's answer.

    Writes matter here even more than reads. In-place compaction does not
    shorten a `write_file` result, it REPLACES it with a one-liner of its own
    (`[write_file] wrote to <path> (N lines)`,
    `agent/context_compressor.py`) — 67 such rows in the live database. For a
    read the stored row is at worst truncated; for a write the original text
    is simply gone, and this entry is the only place it was ever described.
    """
    try:
        _ctx_session, tool_call_id, turn_id = _observability_context()

        # Session resolution lives in `_resolve_session` — shared with
        # `next_op`, so an entry and the address printed in the very same
        # answer can never end up filed under two different sessions.
        # `session_src` records WHICH source answered: an inferred session
        # (`task`) must not be treated as authoritative.
        resolved, session_src = _resolve_session(session, task_id)

        entry = {
            "ts": time.time(),
            "tool": tool or "read_file",
            "session": resolved,
            "session_src": session_src,
            "task_id": task_id or "default",
            "path": path or "",
            "from": int(from_line or 0),
            "to": int(to_line or 0),
            "lines": int(lines or 0),
            "chars": len(text or ""),
            "sha8": sha8(text or ""),
        }
        # The address the model saw in this very answer (Plan09). Absent when
        # the counter was unreachable, for the same reason as below: "no id"
        # is a fact, and an empty string that looks like an id is worse.
        if op:
            entry["op"] = op

        # ── Plan11-Task02: the three facts the panel cannot derive ──────────
        #
        # `total` — the file's length AFTER this operation. NOT a history of
        # lengths: nobody reads the old ones. It is (a) the denominator for the
        # bar, taken from the newest entry per file, and (b) a cheap desync
        # detector — recorded length vs length computed by replaying the
        # deltas; a mismatch means the file was edited outside our tools, which
        # the panel may then say honestly instead of drawing a wrong picture.
        if total:
            entry["total"] = int(total)
        # Only execute_code sets this: how many lines its own output had. The
        # panel's teal heuristic needs it — output lines >= the sum of lines
        # claimed by the nested reads means the script very likely printed the
        # reads through unfiltered, so the data can be trusted as a real read.
        if out_lines:
            entry["out_lines"] = int(out_lines)
        # Absent unless true, like every other optional key here: `nested` is a
        # claim, and a `false` on every ordinary read would be noise in a log
        # that is read by grep as often as by code.
        if _in_sandbox.get():
            entry["nested"] = True

        # ── Plan11-Task03: what was CALLED, not just what came back ─────────
        #
        # The head is always recorded, so a card can say something meaningful
        # even with no sidecar on disk. The full body goes to a file only past
        # the threshold — between head length and that threshold the head IS
        # the whole command, and a file would be pure overhead.
        if command:
            head_chars = _config["cmd_head_chars"]
            entry["cmd_head"] = command[:head_chars]
            if len(command) > _config["cmd_sidecar_min_chars"]:
                name = _write_sidecar(entry["session"], op, command)
                # Absent, not null, when the write failed or there was no `op`
                # to name the file after. Consumers treat a missing pointer and
                # a missing file identically: no command block at all.
                if name:
                    entry["cmd_sidecar"] = name
        # Absent rather than null: a missing correlation id is a fact about the
        # entry, and Task04 keys `unknown` off exactly this.
        if tool_call_id:
            entry["tool_call_id"] = tool_call_id
        if turn_id:
            entry["turn"] = turn_id

        # Small SCALAR counters from the caller (added/removed/replacements/...).
        # Whitelisted BY TYPE rather than by name, because the caller's `facts`
        # dict sits right next to `diff`, whose size is unbounded. An entry has
        # to stay small — otherwise an append-only log quietly becomes a second
        # copy of the files themselves.
        if isinstance(counts, dict):
            small = {k: v for k, v in counts.items()
                     if isinstance(v, (int, float, bool)) and not isinstance(v, bytes)}
            if small:
                entry["counts"] = small

        target = ledger_path(entry["session"])
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False)
        # newline="" keeps Windows from turning our "\n" into "\r\n": the file
        # must read back byte-identical wherever it was written.
        with io.open(target, "a", encoding="utf-8", newline="") as fh:
            fh.write(line + "\n")
    except Exception as e:
        logger.debug("filetools ledger: skipped an entry for %r: %s", path, e)


def read_ledger(session: str | None) -> list:
    """Entries for one session, oldest first.

    A single unparseable line is skipped, not fatal: the writer appends under
    a live agent that can be killed mid-write, so a truncated last line is an
    expected state, and losing the whole history over it would defeat the
    point of keeping one.
    """
    entries: list = []
    try:
        target = ledger_path(session)
        if not target.exists():
            return entries
        with io.open(target, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    entries.append(obj)
    except Exception as e:
        logger.debug("filetools ledger: could not read session %r: %s", session, e)
    return entries


def ledger_sessions() -> list:
    """Session ids that have a ledger file, sorted. Used by the API's health
    route to prove the store is reachable without exposing any content."""
    try:
        d = ledger_dir()
        if not d.exists():
            return []
        return sorted(
            p.name[: -len(".jsonl")]
            for p in d.iterdir()
            if p.is_file() and p.name.endswith(".jsonl")
        )
    except Exception as e:
        logger.debug("filetools ledger: could not list sessions: %s", e)
        return []
