"""hermes-filetools dashboard backend — the panel's data door.

Mounted by Hermes at ``/api/plugins/hermes-filetools/``
(``hermes_cli/web_server.py::_mount_plugin_api_routes``).

Why this file exists at all: the desktop panel's REST helper is locked to
``/api/plugins/<pluginId>`` by construction, and the gateway — the other half
of the desktop's backend — is JSON-RPC with a fixed method table and no
extension point for plugin code. So this namespace is the ONLY place our own
data can reach the panel. See ``__HQ/plans/Plan02__panel-data.md``.

Two constraints shape everything here:

**Imported at server startup, by file path.** Hermes loads this module as
``hermes_dashboard_plugin_hermes-filetools`` via
``spec_from_file_location`` — NOT as part of a package. Relative imports are
therefore impossible, and the plugin root is not on ``sys.path``. Anything we
need from the plugin has to be loaded deliberately (see ``_load_ledger``).
And because the mount happens at import time, an exception raised here breaks
the SERVER's startup, not just our route — hence the FastAPI stub below and
the refusal to do any real work at import.

**Read-only, with ONE named exception.** This half never touches ``state.db``
except to read and never talks to the model. It reported and nothing else until
2026-09-08, when ``POST /context-select`` (Plan13-Task02, removed 2026-10-06) became the first
writing route: a human's decision about what leaves the context has to be
recorded somewhere the engine can read it, and the panel has no other way to
reach it. That route writes exactly one file of our own under
``<hermes home>/filetools/ctxctl/select/`` and nothing else — see the block
comment above it for why that is not a slide down a slope.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

try:
    # `Request` is imported for its ANNOTATION, not for use, and that annotation is
    # load-bearing: FastAPI injects the request object only when the parameter is annotated
    # with it. An UN-annotated `request` parameter is read as a QUERY parameter instead, so
    # the route answers 422 ("field required") and the body is unreachable. Paid for on
    # 2026-09-09: `POST /context-focus` silently 422'd, no file was ever written, and
    # `POST /context-select` had carried the same latent defect since it was added — nothing
    # had called it yet, because the panel had never written anything.
    from fastapi import APIRouter, Request
except Exception:  # noqa: BLE001 - unit tests run without dashboard deps
    # Keeps the module importable by a bare python (and by pytest) with no
    # FastAPI installed: the decorators become no-ops and `router` is inert.
    # Same shape as hermes-achievements' own api file.
    class Request:  # type: ignore[no-redef]
        """Stub for the no-FastAPI path: only ever used as an annotation."""

    class APIRouter:  # type: ignore[no-redef]
        def get(self, *_args, **_kwargs):
            return lambda fn: fn

        def post(self, *_args, **_kwargs):
            return lambda fn: fn

        def websocket(self, *_args, **_kwargs):
            return lambda fn: fn


router = APIRouter()

PLUGIN_ID = "hermes-filetools"

# The slots we override. A chat can also have run UNPATCHED built-ins under
# these same names (an older session, a profile with the plugin off), so the
# name alone is not proof the row is ours — see _is_ours.
#
# `terminal` joined on 2026-09-02, and it must be listed here as well as in the
# desktop half's own OUR_TOOLS (desktop/src/core.js): the two lists are read by
# different halves — this one gates `_is_ours` (whose row is ours: `/messages` names an unrecorded
# row of ours apart from a foreign one), that one gates the live event stream.
OUR_TOOLS = frozenset({
    "read_file", "write_file", "patch", "search_files", "terminal",
    # 2026-09-02: the two remaining tools that answer in OUR text and were
    # missing from the panel.
    "execute_code", "vision_analyze",
    # 2026-10-06 (`deferred/20`): it sits behind the tool_search bridge, but a call
    # through their `tool_call` stores an ordinary row named `process_manage` — and
    # without it here that row was never found and its chip read `unknown`.
    # Moves together with OUR_TOOLS in `desktop/src/core.js`.
    "process_manage",
})

# Signals that identify OUR response, chosen to avoid configurable symbols.
#
# The framing marks (block/anchor/separator) come from plugin.yaml's `formatting:` section and a
# profile can override any of them, so matching on "■"/"○○○" silently stops working the moment
# someone changes them — and silently is the operative word: our rows would stop being recognised.
# Meta FIELD names are literals in our own code and are not configurable, so they are the stable
# signal. Per tool, because the four responses share no single field.
# Keys that only OUR envelope carries (ui_envelope passes facts through from the tool cores).
# Needed because the BUILT-IN tools also answer with JSON: without this, `{"content": ..., "lines":
# ...}` from an unpatched read_file parsed as "an envelope, therefore ours" — a false positive that
# would have let the panel claim someone else's output. Presence of any one is enough.
_ENVELOPE_KEYS = frozenset({
    "files_modified", "replacements", "no_change", "dry_run", "added", "removed", "success",
})

# The only tools whose answer can carry our `ui_json` envelope: `ui_envelope`
# (file_ops/_new_format.py) is called from create_file_new_core and
# modify_file_new_core and nowhere else. Everything else we wrap answers as
# plain text, so a JSON-looking first line on one of THOSE rows is upstream's,
# not ours — see the note in `_is_ours`.
_ENVELOPE_TOOLS = frozenset({"write_file", "patch"})

# The operation address as our tools print it: hex, at least four digits. The
# FileTag tail is no longer EMITTED (2026-09-03) but is still matched, so rows
# stored before that keep resolving. Not anchored to the line start - read_file
# wraps its whole header in the opened file's comment syntax.
#
# The words (`Operation ID`, `File`) come from the tool dictionary (Plan28,
# `file_ops/_vocab.py`), so the regexes are built on first use: `_header_re`.
_HEADER_RES: dict = {}


def _header_re(key: str):
    """`op` — the address in a header line; `file` — the path after the `File` word."""
    if not _HEADER_RES:
        vocab = _load_file_ops(_VOCAB_MODULE, "_vocab.py")
        op = vocab.COMMON["op"] if vocab else "Operation ID"
        file_word = vocab.header("read_file")["file"] if vocab else "File"
        _HEADER_RES["op"] = re.compile(
            re.escape(op) + r":\s*([0-9a-fA-F]{4,}(?:x[0-9a-fA-F]{2,4})?)")
        _HEADER_RES["file"] = re.compile(re.escape(file_word) + r":\s*(\S.*)")
        _HEADER_RES["file_word"] = file_word + ":"
    return _HEADER_RES[key]


def _unframe(value: str, content: str, op_m) -> str:
    """A header value without the comment closer (`<!-- File: a.md -->` → `a.md`).

    The closer is taken from the op line of the same row, never guessed from the
    path — same rule as `file_ops/_tool_text.py::frame_of`.
    """
    value = value.strip()
    if op_m:
        end = content.find("\n", op_m.end())
        closer = content[op_m.end(): end if end != -1 else len(content)].strip()
        if closer and value.endswith(closer):
            value = value[: -len(closer)].rstrip()
    return value


# Signals are header ROLES (Plan28): the word is the dictionary's, `role:` is searched.
# A literal (not a role) is marked by a leading `=`.
_TOOL_SIGNALS = {
    "read_file": ("file_tag", "size"),
    "write_file": ("encoding",),
    "patch": ("patched", "replacements", "lines"),
    "search_files": ("found",),
    # terminal — the built-in answers with raw JSON, ours with labeled plain
    # text, so `_is_ours`'s "still JSON → not ours" check already carries most
    # of the weight here. These two are the positive proof on top of it:
    #   - `(raw: "exit_code"` is emitted by render_terminal_result and by
    #     nothing else (terminal_core.py adds it so the compaction summary can
    #     still regex an exit code out of our plain text).
    #   - `Session id:` covers a BACKGROUND start, which returns a session id
    #     and no exit code at all — without it those rows would be dropped.
    # Both are literals in our own code, not configurable symbols, same rule
    # as the four above.
    "terminal": ('=(raw: "exit_code"', "session_id"),
    # execute_code — same reasoning as terminal: the built-in answers with raw
    # JSON, ours with labeled text, so `_is_ours`'s "still JSON → not ours"
    # carries most of it. `Stdout bytes captured:` is the strongest positive:
    # it is our _LABELS wording for their `stdout_bytes_captured` key, and it
    # appeared in every live sample. `Tool calls:` is the same kind of label
    # and covers a script that captured no stdout at all.
    "execute_code": ("stdout_bytes_captured", "tool_calls_made", "status"),
    # vision_analyze — `Success:` is our label for their `success` field and is
    # a head key, so it is present whenever the answer is TEXT at all. Note
    # what this correctly does NOT match: on the native-vision fast path their
    # handler returns a `_multimodal` DICT that our wrapper passes through
    # untouched, so that row is not our text and must not be claimed as ours.
    "vision_analyze": ("success", "scale_note"),
}


def split_envelope(content):
    """`(facts, rest)` — strip our `ui_json` envelope off the front of a response.

    `CONTRACT.md` §19: with `ui_json` set to anything but "off", `patch` and `write_file` put a
    short JSON facts block on the FIRST line, above the plain text. Two consequences the panel
    learned the hard way:

    * a response can legitimately START with "{", so rejecting those as "not ours" drops every
      enveloped operation — which is what happened the moment ui_json went to "min";
    * the facts are the honest source for `kind`. The old signal, the `Lines: N (unchanged)`
      sentinel, means the line COUNT is unchanged, NOT that nothing changed: a replacement inside
      one line keeps the count, so a real edit was reported as no-change (seen live 2026-08-27).
    """
    text = content or ""
    if not text.startswith("{"):
        return None, text
    head, sep, rest = text.partition("\n")
    try:
        facts = json.loads(head)
    except Exception:
        return None, text
    if not isinstance(facts, dict):
        return None, text
    if not (_ENVELOPE_KEYS & set(facts)):
        return None, text          # someone else's JSON, not our envelope
    return facts, (rest if sep else "")


# dashboard/ -> plugin root. Resolved once; the file's own location is the only
# thing we can trust here, since cwd at server startup is arbitrary.
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent

# Private module name: `_ledger` is loaded BY PATH rather than by putting the
# plugin root on sys.path. Two reasons — a bare top-level `file_ops` in
# sys.modules is a name collision waiting to happen inside a long-lived server
# process, and importing the package would run file_ops/__init__.py for no
# reason. This mirrors exactly what Hermes does to us.
_LEDGER_MODULE = "hermes_filetools_ledger"


def _load_ledger():
    """Return the ledger module, or None if it cannot be loaded.

    Lazy and cached: called per request, never at import — a missing or broken
    ledger must degrade this route to an honest error field, not take down the
    server it is mounted in.
    """
    cached = sys.modules.get(_LEDGER_MODULE)
    if cached is not None:
        return cached
    path = _PLUGIN_ROOT / "file_ops" / "_ledger.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(_LEDGER_MODULE, path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_LEDGER_MODULE] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(_LEDGER_MODULE, None)
        return None
    return mod


_MSGINDEX_MODULE = "hermes_filetools_msgindex"


def _load_msgindex():
    """Return `file_ops/_msgindex.py`, or None. Same lazy, fail-closed shape.

    It resolves the ledger itself, under the SAME module name this file uses —
    so the counter cache stays single-instance in this process.
    """
    cached = sys.modules.get(_MSGINDEX_MODULE)
    if cached is not None:
        return cached
    path = _PLUGIN_ROOT / "file_ops" / "_msgindex.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(_MSGINDEX_MODULE, path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MSGINDEX_MODULE] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(_MSGINDEX_MODULE, None)
        return None
    return mod


_TOOL_TEXT_MODULE = "hermes_filetools_dashboard_tool_text"


def _load_tool_text():
    """Return `file_ops/_tool_text.py`, or None. Same lazy, fail-closed shape as the ledger.

    Loaded BY PATH for the same reason the ledger is: this file is imported by the
    desktop's plugin loader, not as part of a package, so a normal import would
    depend on how the process happened to be started.

    WHY LOAD IT AT ALL instead of copying one regex. The envelope format was
    already written out three times — the tools build it, the drop mechanism
    rewrote it, this file parses it — and every copy drifted. The address regex
    below (`_OP_IN_HEADER`) is the surviving duplicate and it is documented as
    such; a second one for the state would be the same mistake made knowingly.
    """
    cached = sys.modules.get(_TOOL_TEXT_MODULE)
    if cached is not None:
        return cached
    path = _PLUGIN_ROOT / "file_ops" / "_tool_text.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(_TOOL_TEXT_MODULE, path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_TOOL_TEXT_MODULE] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(_TOOL_TEXT_MODULE, None)
        return None
    return mod


# The state a human put on an operation by removing it from the context. Kept as
# a literal so a failed module load degrades to "we cannot read states" rather
# than to a crash; the string itself is owned by _tool_text.STATE_REMOVED and the
# test below pins the two together.
STATE_REMOVED = "removed_from_context"


def _declared_state(content: str) -> str:
    """The state written in the row's own header, or `""` when it declares none."""
    tool_text = _load_tool_text()
    if tool_text is None:
        return ""
    try:
        return tool_text.state_of(content or "")
    except Exception:  # noqa: BLE001 - report nothing, never raise
        return ""


# ---------------------------------------------------------------------------
# Row recognition — pure logic (whose row is ours, what it ended with). Kept as module-level
# functions with no DB and no FastAPI in them, so the tests can drive them on hand-built rows.
# The `/ops` route they once served is gone (Plan23, 2026-10-06); `/messages` uses them now.
# ---------------------------------------------------------------------------

def _tool_signals(tool: str) -> tuple:
    """`_TOOL_SIGNALS[tool]` as strings to search: a role → `<its word>:`, `=literal` → literal."""
    vocab = _load_file_ops(_VOCAB_MODULE, "_vocab.py")
    words = vocab.header(tool) if vocab is not None else {}
    out = []
    for sig in _TOOL_SIGNALS.get(tool, ()):
        if sig.startswith("="):
            out.append(sig[1:])
        elif sig in words:
            out.append(words[sig] + ":")
    return tuple(out)


def _is_ours(row) -> bool:
    """True when this row is a result from OUR handler.

    Two conditions, both required. The tool name must be one of the four we
    override, AND the text must carry one of our framing marks. The name alone
    is not enough: the same four names exist as built-ins, so a session that
    ran with the plugin disabled would otherwise show up as ours and the panel
    would claim credit for JSON it never produced.

    Note on what this necessarily EXCLUDES: in-place compaction does not just
    shorten a write_file result, it REPLACES it with its own one-liner
    ("[write_file] wrote to <path> (N lines)", agent/context_compressor.py).
    Such a row is genuinely not our output any more, so dropping it is
    correct — and it is also the sharpest argument for the ledger, since for
    writes the original text is not merely truncated but substituted.
    """
    tool = (row.get("tool_name") or "")
    if tool not in OUR_TOOLS:
        return False

    content = row.get("content") or ""

    # OUR OWN STATE STAMP IS PROOF BY ITSELF. `State: removed_from_context` is written by
    # `_tool_text.as_removed` and by nothing else in the world, in our own comment framing —
    # a row declaring it is ours by construction.
    #
    # This is not belt-and-braces, it repairs a real collision found 2026-09-09 (owner
    # noticed the visible half: `erase tool` could not be selected on a dropped card).
    # `write_file`'s ONLY ownership signature is `Encoding:` — and `drop content` deliberately
    # removes that field (`_tool_text._DROP_ON_REMOVAL`, it is useful only on a re-read). So a
    # dropped write stopped being recognised as ours: its card vanished from the stack on any
    # history reload, and erasing it became impossible for a second, deeper reason than the
    # checkbox rule. Measured, not reasoned: `_is_ours` went True -> False across `as_removed`
    # for write_file and stayed True for read_file (which keeps `Size:`).
    #
    # Checking the state instead of restoring `Encoding:` fixes the whole class: whatever
    # header fields we decide to drop later, a row that carries our stamp stays ours.
    if _declared_state(content):
        return True

    facts, body = split_envelope(content)

    # An envelope is proof by itself, but ONLY for the two tools that can emit
    # one (`ui_envelope` is called from create_file_new_core and
    # modify_file_new_core, nowhere else — CONTRACT.md §19).
    #
    # Found by a cross-half test 2026-09-02, the moment vision_analyze joined
    # OUR_TOOLS: upstream's own vision answer is `{"success": true, "analysis":
    # ...}`, and `success` is in _ENVELOPE_KEYS, so the shortcut read THEIR
    # JSON as our envelope and the panel claimed output it never produced.
    # That is the exact false positive _ENVELOPE_KEYS was introduced to
    # prevent, reappearing from the other side — the keys cannot tell whose
    # JSON it is, only the tool can.
    if facts is not None and tool in _ENVELOPE_TOOLS:
        return True

    # Still JSON after the envelope check → the built-in tool, or our own OLD path.
    if body.lstrip().startswith("{"):
        return False

    return any(sig in body for sig in _tool_signals(tool))


def _op_from_header(content: str):
    """This operation's address (`Operation ID: 0156xab`) off the FIRST lines of our own
    response — Plan09.

    Read from the text rather than joined from the ledger on purpose: the panel
    must show exactly the address the MODEL was shown, and the two can differ
    (a row rewritten by compaction, a ledger file from another home). Same
    narrow scan as `_path_from_header`, and for the same reason: the header is
    at the top by construction, and an `op:` further down is file CONTENT.
    Tolerates read_file's comment wrapper (`#Operation ID: ...`), which is why the match
    is not anchored to the start of the line.
    """
    for line in (content or "").split("\n", 4)[:4]:
        found = _header_re("op").search(line)
        if found:
            return found.group(1)
    return None


def _kind_from(facts, body: str) -> str:
    """Ядро решения о `kind`. Принимает УЖЕ разобранные факты и тело без конверта."""
    if facts is not None:
        if facts.get("success") is False or facts.get("error"):
            return "error"
        if facts.get("no_change") is True:
            return "no-change"
        counts = [facts.get(k) for k in ("replacements", "added", "removed")]
        known = [c for c in counts if isinstance(c, int)]
        if known and all(c == 0 for c in known):
            return "no-change"
        return "ok"
    text = (body or "").lstrip()
    if text.startswith("ERROR:") or text.startswith('{"error"'):
        return "error"
    return "ok"


def _kind(content: str) -> str:
    """Обёртка для СЫРОГО ответа (тесты, внешние вызовы): снимает конверт и делегирует."""
    facts, body = split_envelope(content or "")
    return _kind_from(facts, body)


# Код выхода в НАШЕЙ шапке (`terminal`) — та же строка, что читает `_tool_text._facade_exit_code`.
_EXIT_LINE_RE = re.compile(r"^\W*Exit code:\s*(-?\d+)", re.MULTILINE)


def _row_kind(content: str) -> str:
    """Исход ЛЮБОЙ тул-строки ленты, нашей и чужой (Plan23-Task03, чип зеркала).

    Наше решает `_kind` (наш конверт и наше `ERROR`). Сверх него две вещи, которых `_kind` не
    видит: код выхода в шапке (`Exit code: 1`) и чужой
    JSON-ответ (`{"success": false}`, `{"error": "..."}`, `{"exit_code": 2}` — так отвечают
    `terminal`, `memory`, `web_*` Hermes). Ничего не угадываем по словам в тексте: только поля.
    """
    kind = _kind(content)
    if kind != "ok":
        return kind
    text = str(content or "")
    exit_m = _EXIT_LINE_RE.search(text[:2000])
    if exit_m and int(exit_m.group(1)) != 0:
        return "error"
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except (TypeError, ValueError):
            return kind
        if isinstance(data, dict):
            code = data.get("exit_code")
            if data.get("success") is False or data.get("error") \
                    or (isinstance(code, int) and not isinstance(code, bool) and code != 0):
                return "error"
    return kind


_TOKENS_MODULE = "hermes_filetools_dashboard_tokens"


def _load_tokens():
    """`context_ctl/_tokens.py` (our estimator, Plan24) by path, or None. Stdlib only, so safe here."""
    cached = sys.modules.get(_TOKENS_MODULE)
    if cached is not None:
        return cached
    path = _PLUGIN_ROOT / "context_ctl" / "_tokens.py"
    spec = importlib.util.spec_from_file_location(_TOKENS_MODULE, path) if path.exists() else None
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_TOKENS_MODULE] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:  # noqa: BLE001 - no estimator is a degraded answer, not a 500
        sys.modules.pop(_TOKENS_MODULE, None)
        return None
    return mod


_ENVELOPE_MODULE = "hermes_filetools_dashboard_envelope"


def _load_envelope():
    """`context_ctl/_prompts/_envelope.py` by path, or None: the ONE place that knows our compaction
    marks (`service_kind`). No imports inside it, so it loads bare like `_tokens.py`."""
    cached = sys.modules.get(_ENVELOPE_MODULE)
    if cached is not None:
        return cached
    path = _PLUGIN_ROOT / "context_ctl" / "_prompts" / "_envelope.py"
    spec = importlib.util.spec_from_file_location(_ENVELOPE_MODULE, path) if path.exists() else None
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_ENVELOPE_MODULE] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:  # noqa: BLE001 - no marks means no tint, not a 500
        sys.modules.pop(_ENVELOPE_MODULE, None)
        return None
    return mod


def _session_estimator(tk, session: str) -> dict:
    """Multiplier and template markup for THIS session's model (Plan24): the Mirror's numbers per
    message come from the same estimator and the same coefficients as the token strip's `Ours`.

    The model: the live agent's, else the one the engine's status file remembers. Unknown model →
    the latest measured tokenizer (`exact_model: False`), unknown everything → multiplier 1.
    """
    model = ""
    engine, _reason = _engine_for(session)
    agent = engine.live_agent() if engine is not None and callable(getattr(engine, "live_agent", None)) else None
    if agent is not None:
        model = getattr(agent, "model", "") or ""
    if not model:
        try:
            status = json.loads((tk.tokens_dir().parent / "ctxctl" / f"{session}.json").read_text(encoding="utf-8"))
            model = ((status.get("session_kwargs") or {}).get("model")
                     or (status.get("bridge") or {}).get("agent_model") or "")
        except Exception:  # noqa: BLE001 - no status file yet
            model = ""
    fp, entry, exact_model = tk.entry_for(tk.load_models(), model)
    return {"model": model, "fp": fp, "exact_model": exact_model,
            "mult": (entry or {}).get("mult") or 1.0, "markup": (entry or {}).get("markup") or {}}


# ---------------------------------------------------------------------------
# Ledger against rows — our ledger is the REFERENCE (what we handed the model); their row is
# the OBSERVATION (what survived). The difference is the answer, and neither source can produce
# it alone. Served `/reads` until Plan23 (2026-10-06); `/context-state` is its one reader now.
# ---------------------------------------------------------------------------

def _rows_by_call_id(rows) -> dict:
    out: dict = {}
    for row in rows:
        call_id = row.get("tool_call_id")
        if call_id and (row.get("tool_name") or "") in OUR_TOOLS:
            out.setdefault(str(call_id), row)
    return out


def _rows_by_op(rows) -> dict:
    """`Operation ID -> row`, read out of the row's own TEXT. The second join key.

    WHY A SECOND KEY, and why it is not redundant: the primary join is the ledger
    entry's `tool_call_id`, and that field can simply be absent. It was absent for a
    whole day on 2026-09-08 — upstream moved the private contextvars our ledger reads
    and every entry lost its id — and with no id there is no row, so every operation
    reported `state: unknown` and the panel showed nothing changing. It is also
    legitimately absent for operations issued inside the `execute_code` sandbox.

    The number in the row's text is the SAME address the model was shown (Plan09), so
    this key is available exactly when the other one is not. Our own drop stubs print
    the canonical `Operation ID: <hex>` line for this reason: a dropped operation must
    stay identifiable, or the panel loses the card at the moment its status changes.
    """
    out: dict = {}
    for row in rows:
        if (row.get("tool_name") or "") not in OUR_TOOLS:
            continue
        op = _op_from_header(row.get("content") or "")
        if op:
            out.setdefault(str(op).lower(), row)
    return out


def _row_for(entry, by_call_id, by_op):
    """The row for one ledger entry: by `tool_call_id`, else by operation number."""
    call_id = entry.get("tool_call_id")
    row = by_call_id.get(str(call_id)) if call_id else None
    if row is None:
        op = str(entry.get("op") or "").strip().lower()
        if op:
            row = by_op.get(op)
    return row


def _state_for(entry, row) -> tuple:
    """`(state, reason)` for one ledger entry against its row — or its absence.

    The vocabulary is deliberately small and every branch is evidence-based:

    * `verbatim`  — the row still holds the exact bytes we returned (sha8).
    * `removed_from_context` — the row's own header DECLARES it (`State:`): a
      human took this operation out of the model's context. Not shortened, not
      summarised: the body is gone and the count is zero. Checked first, before
      any size comparison, because comparing sizes already answers a different
      question than the one asked.
    * `compacted` — the row is still active but its text no longer matches and
      is shorter: in-place compaction rewrote `content` under it. THEIRS, not
      ours — ours says so in the header.
    * `evicted`   — the row is not active. Two different causes, kept apart in
      the reason because they mean different things to a human: compaction
      moved it out of the live window (`compacted=1`), or undo/rewind
      soft-deleted it (`compacted=0`).
    * `unknown`   — we could not match it to a row at all. NEVER a guess.
    """
    if row is None:
        return "unknown", "no matching row"

    content = row.get("content") or ""
    active = bool(row.get("active"))
    was_compacted = bool(row.get("compacted"))

    # A row that DECLARES its state is not guessed about. This is the whole
    # design change of 2026-09-09: the answer carries `State:` in its own header,
    # so removal is a fact we read, not a conclusion we draw from lengths.
    #
    # Before this, a removed operation fell through to the length comparison
    # below and came out as `compacted`, "row shortened 746 -> 258 chars" — which
    # the owner rightly called absurd: nothing was shortened, the content is GONE.
    # The check comes first for the same reason: the moment we compare sizes we
    # are already answering the wrong question.
    if active and _declared_state(content) == STATE_REMOVED:
        # Empty reason, exactly like `verbatim`. A reason explains a CONCLUSION we
        # drew; this state was read off the row, so there is nothing to explain —
        # and English prose glued to a Russian label is how the panel ended up
        # saying "удалено из контекста — removed from the context by a human".
        return STATE_REMOVED, ""

    if not active:
        if was_compacted:
            return "evicted", "compaction moved it out of the active window"
        return "evicted", "soft-deleted by undo/rewind"

    ledger = _load_ledger()
    if ledger is None:
        return "unknown", "ledger module unavailable, cannot compare"

    expected = entry.get("sha8")
    if ledger.sha8(content) == expected:
        return "verbatim", ""

    ours = int(entry.get("chars") or 0)

    # Hermes APPENDS to a stored tool result. Observed on live data: a
    # "[Tool loop warning: idempotent_no_progress_warning; ...]" line glued to
    # the end, 194 chars, which made a perfectly intact row look shortened-in-
    # reverse and got 12 of 47 reads mislabelled `compacted`.
    #
    # A PREFIX check settles it without us ever storing the text: hash the
    # first `chars` characters of the row and compare to the digest we kept.
    # Our text is intact, so the model IS still holding what we sent.
    if len(content) > ours and ledger.sha8(content[:ours]) == expected:
        return "verbatim", (
            f"our text intact; Hermes appended {len(content) - ours} chars "
            "(e.g. a tool-loop warning)"
        )

    if len(content) < ours:
        return "compacted", f"row shortened {ours} -> {len(content)} chars"

    # Same length or longer, and not our text even as a prefix. Calling this
    # verbatim would be a lie; calling it compacted would name a cause we did
    # not establish.
    # Формулировка переписана 2026-09-09 по замечанию владельца: прежняя («row
    # neither matches nor is shorter (0 -> N chars)») описывала ХОД НАШЕЙ МЫСЛИ —
    # какие ветки не сработали, — а человеку нужен факт и что с ним делать.
    return "unknown", (
        f"text changed, not shortened: we sent {ours} chars, the row has "
        f"{len(content)}"
    )


@router.get("/ping")
def ping():
    """Health probe: is this namespace mounted, and can it see the ledger?

    Deliberately more than `{ok: true}`. The two failures this route exists to
    tell apart are both invisible from a bare status code:

    * mounted but the ledger store is unreachable -> `ledger_sessions: null`
      plus `error`, rather than a 500 that looks like a routing problem;
    * mounted against a DIFFERENT hermes home than the agent writes to (the
      web server resolves its own) -> `ledger_dir` shows which one, which is
      the only way to catch a silent split-brain between the two processes.

    Exposes no file contents and no session data — a count and a path.
    """
    ledger = _load_ledger()
    if ledger is None:
        return {
            "ok": True,
            "plugin": PLUGIN_ID,
            "ledger_sessions": None,
            "ledger_dir": None,
            "error": "ledger module could not be loaded from "
                     f"{_PLUGIN_ROOT / 'file_ops' / '_ledger.py'}",
        }

    # ledger_sessions() is itself fail-open (returns [] on any error), so a
    # missing directory reads as "no sessions yet", which is the truth before
    # the first read_file lands.
    try:
        sessions = ledger.ledger_sessions()
        ledger_dir = str(ledger.ledger_dir())
    except Exception as exc:  # noqa: BLE001 - report, never raise
        return {
            "ok": True,
            "plugin": PLUGIN_ID,
            "ledger_sessions": None,
            "ledger_dir": None,
            "error": f"{type(exc).__name__}: {exc}",
        }

    return {
        "ok": True,
        "plugin": PLUGIN_ID,
        "ledger_sessions": len(sessions),
        "ledger_dir": ledger_dir,
    }


# ---------------------------------------------------------------------------
# /context-state (Plan11-Task04) — FACTS ONLY.
#
# Named after the subsystem, not the payload (owner, 2026-09-07): the panel this
# feeds is about the state of the CONTEXT, and later about controlling it
# (compaction, summarisation). Files are how it shows that, not what it is.
#
# The hard rule, frozen in `CONTRACT__panel-seams`: the server ships facts, the client
# computes everything derived. Only two things here cannot be computed client
# side, and both are facts — the physical state of a row (needs THEIR state.db
# plus our sha8) and a file's length (only the ledger or the disk knows it).
# The fold into current coordinates, recency rank and overlap percentage all
# live in `core.js::buildContextView`, because the panel must redraw on EVERY
# operation: a server-side fold would mean a network round trip per operation.
# ---------------------------------------------------------------------------

# Ledger keys copied through verbatim. Listed rather than passed as `**entry`
# so a new ledger field cannot leak into a frozen contract by accident: the
# contract is the promise, and the ledger is free to grow behind it.
_FACT_KEYS = ("op", "tool", "path", "from", "to", "total", "ts",
              "nested", "seen", "out_lines", "cmd_head", "cmd_sidecar",
              # several shown ranges of one read (Plan30, ledger amendment 8, panel 22)
              "parts")

# The state of an operation that was DELIBERATELY erased, and we have the record to prove it
# (`_ledger.EVENT_ERASED`). Not the same as "the ledger remembers it and the database has no
# row": that is `missing_row` and it covers everything, including losses we cannot explain.
STATE_ERASED = "erased"


def _msg_by_call_id(entries) -> dict:
    """`tool_call_id -> m-адрес` по записям о сообщениях журнала (их пишет индексация
    `/messages`). Последняя запись побеждает — то же правило, что у `/messages`."""
    ledger = _load_ledger()
    if ledger is None or not hasattr(ledger, "message_records"):
        return {}
    out = {}
    for rec in ledger.message_records(entries):
        call_id, msg = str(rec.get("tool_call_id") or ""), str(rec.get("msg") or "")
        if call_id and msg:
            out[call_id] = msg
    return out


def _ledger_split(entries):
    """`(operation entries, op -> tombstone)`. The one place events are told from operations.

    THE VERDICT ORGAN, and that is the point (owner, 2026-09-09: "maybe we need a central
    organ of verdicts, rather than everyone deciding for themselves"). Whether an operation
    is still in the context is answered HERE, once, and the surfaces obey without an opinion
    of their own — the stack by removing the card, the file zone by dropping the segment.

    Degrades to today's behaviour when the ledger module cannot be loaded: every entry is an
    operation, no tombstones. An unreadable journal must cost us the gravestones, not the
    panel.
    """
    ledger = _load_ledger()
    if ledger is None or not hasattr(ledger, "erased_ops"):
        return [e for e in entries or [] if isinstance(e, dict)], {}
    ops = [e for e in entries or []
           if isinstance(e, dict) and not ledger.is_event(e)]
    return ops, ledger.erased_ops(entries)


def _parent_of(entry, entries):
    """`(parent execute_code entry or None, matched_by_position)` for a nested operation.

    Its own sha8 can never match: the row it correlates to holds the sandbox's
    output, not this read's text, because every nested call inherits the OUTER
    tool_call_id (`_ledger.py::_observability_context`). So the questions about
    it are asked of the PARENT — the `execute_code` entry it ran inside.

    With a correlation id: the `execute_code` entry sharing it.

    Without one — and measurement says that is the NORMAL case, not the exception:
    across the whole live ledger store, all 50 nested entries lack `tool_call_id`,
    every one of them (checked 2026-09-08). The sandbox runs the inner tool in a
    context where `tools.approval`'s contextvars are unset, so there is nothing for
    `_ledger` to record. The parent can still be identified, by ledger POSITION
    rather than by id: a child is recorded WHILE execute_code runs, the parent right
    AFTER it returns, so the parent is the first `execute_code` entry below this one.
    Same 50 entries, same check: all 50 have such an entry, none is missing.

    Deliberately NOT the nearest execute_code in either direction: "before"
    would attach a child to the PREVIOUS sandbox call whenever the current one
    is the session's first, which is exactly the case with no evidence at all.
    """
    call_id = entry.get("tool_call_id")
    if call_id:
        for other in entries:
            if other is entry:
                continue
            if (other.get("tool") == "execute_code"
                    and other.get("tool_call_id") == call_id):
                return other, False
        return None, False
    for other in entries[entries.index(entry) + 1:]:
        if other.get("tool") == "execute_code":
            return other, True
    return None, True


def _parent_state(entry, entries, index):
    """`(state, reason)` for an operation performed INSIDE execute_code.

    The nested operation's visibility is exactly its parent's (`_parent_of`):
    parent evicted, it is gone with it; parent alive, its text is inside the
    parent's.
    """
    parent, by_position = _parent_of(entry, entries)
    if parent is None:
        if not by_position:
            return "unknown", ("nested operation whose execute_code parent is not "
                               "in the ledger")
        return "unknown", ("nested operation with no correlation id and no "
                           "execute_code entry after it — its parent cannot be "
                           "identified")
    parent_id = parent.get("tool_call_id")
    state, reason = _state_for(parent, index.get(str(parent_id)) if parent_id else None)
    if by_position:
        suffix = ("inherited from the execute_code operation it ran inside "
                  "(matched by ledger position — the sandbox records no "
                  "correlation id for nested calls)")
    else:
        suffix = "inherited from the execute_code operation it ran inside"
    return state, f"{reason}; {suffix}" if reason else suffix


def build_context_state(entries, rows) -> dict:
    """Join the ledger against the stored rows. Pure: no DB, no FastAPI.

    Returns a FLAT list in ledger order — not a tree keyed by file. Grouping is
    a presentation decision and belongs to the client; the server does not get
    to decide how its data is shown.

    Evicted operations are ABSENT, not flagged. The bar shows what is in the
    context, and on the bar grey already means "not read" — so an evicted
    operation must not occupy space there at all (`Vision02__fileInfo_panel.md`,
    goal #0).
    """
    index = _rows_by_call_id(rows)
    by_op = _rows_by_op(rows)
    msg_of = _msg_by_call_id(entries)
    # Надгробия отделяются ЗДЕСЬ, и `seq` считается по операциям: события в леджере лежат
    # вперемешку с ними, а `seq` — это порядок ОПЕРАЦИЙ, по которому клиент сортирует всё.
    # Считай мы его по сырому списку, номера поехали бы у всех операций после первого
    # стирания, а `total` начал бы считать надгробия операциями.
    entries, tombs = _ledger_split(entries)
    ops = []
    unmatched = evicted = erased = 0

    for seq, entry in enumerate(entries):
        call_id = entry.get("tool_call_id")
        row = _row_for(entry, index, by_op)

        if entry.get("nested"):
            state, reason = _parent_state(entry, entries, index)
        else:
            state, reason = _state_for(entry, row)
            if row is None:
                unmatched += 1
                if not call_id:
                    reason = ("no tool_call_id recorded, and the operation "
                              "number could not be found in any row's text "
                              "either (e.g. an operation issued through the "
                              "execute_code sandbox, which Hermes never stores "
                              "as a row of its own)")

        if state == "evicted":
            evicted += 1
            continue

        fact = {key: entry.get(key) for key in _FACT_KEYS}
        # `seq` is the ORDER, and the operation number is NOT: `next_op`
        # guarantees uniqueness, never sequence — parallel tools inside one
        # turn draw interleaved (live: 0005, 0007, 0006, 0008). Anything that
        # sorts operations must use this, and `op` only as a display address.
        fact["seq"] = seq
        # Kept apart rather than pre-subtracted: "-1 +100" and "-100 +199" are
        # different events with the same delta, and the fold wants the events.
        counts = entry.get("counts") or {}
        fact["added"] = int(counts.get("added") or 0)
        fact["removed"] = int(counts.get("removed") or 0)
        fact["state"] = state
        fact["reason"] = reason
        # m-АДРЕС строки-результата (Plan23, поправка 20): зеркало находит факт по адресу, а не
        # по номеру операции. У вложенной операции своей строки нет — адреса нет.
        msg = msg_of.get(str(call_id or "")) if not entry.get("nested") else None
        if msg:
            fact["msg"] = msg
        # Адрес строки `execute_code`, ВНУТРИ которого шла вложенная операция (2026-10-14) —
        # только для перехода из списка операций файла к чипу скрипта. Нарочно НЕ `msg`: по
        # `msg` чип берёт диф и состояние операции, и скрипт присвоил бы себе чужие.
        if entry.get("nested"):
            parent, _ = _parent_of(entry, entries)
            host = msg_of.get(str(parent.get("tool_call_id") or "")) if parent else None
            if host:
                fact["host_msg"] = host
        # THE LEDGER REMEMBERS THE OPERATION AND THE DATABASE HAS NO ROW FOR IT. A flag, not
        # a phrase to be re-parsed on the client: the panel acts on this (it removes the
        # card), and hanging an action off matching the wording of `reason` would break the
        # moment that wording is improved.
        #
        # Added 2026-09-09 for `erase tool`. The card of an erased operation survived in the
        # panel's live store and turned into a ghost — visible, addressable, backed by
        # nothing. Owner's rule: "orphaned cards should just be removed, the operation
        # physically no longer exists."
        #
        # Why not "absent from /ops" as the criterion: that answers a DIFFERENT question.
        # `/ops` also drops rows whose text stopped looking like ours — and in-place
        # compaction REPLACES a write_file result with its own one-liner, so such a row is
        # missing from `/ops` while still sitting in the model's context and costing tokens.
        # Deleting that card would hide something real. `missing_row` is about the row's
        # existence and nothing else.
        fact["missing_row"] = row is None and not entry.get("nested")
        # СТЁРТА, И ЭТО ЗАПИСАНО — состояние из надгробия, а не выведенное из отсутствия
        # строки. Разница ровно в том, что можно сказать человеку: у записанного стирания
        # есть время и действие, поэтому панель рисует надгробие (`#L7`); у необъяснимой
        # потери нет ничего, поэтому карточка просто уходит. Ставим ПОСЛЕ `_state_for`
        # намеренно: физический вопрос по строке уже задан, и его ответ («строки нет»)
        # надгробие не отменяет, а объясняет.
        tomb = tombs.get(str(entry.get("op") or "").strip())
        if tomb is not None:
            erased += 1
            fact["state"] = STATE_ERASED
            fact["erased_at"] = tomb.get("ts")
            fact["erased_by"] = tomb.get("by") or ""
            fact["reason"] = f"erased by {fact['erased_by'] or 'us'}"
            # Из `unmatched` вычитаем: это счётчик НЕОБЪЯСНЁННЫХ несовпадений, по нему
            # смотрят, много ли операций панель не смогла сопоставить. Стёртая операция
            # сопоставлению не подлежит вовсе, и держать её в этом числе значило бы
            # показывать проблему там, где всё сработало как задумано.
            if row is None and not entry.get("nested"):
                unmatched = max(0, unmatched - 1)
        ops.append(fact)

    return {"ops": ops, "unmatched": unmatched, "evicted": evicted,
            "erased": erased, "total": len(entries)}


@router.get("/context-state")
def context_state(session: str = ""):
    """Every operation of ours still visible to the model, as plain facts.

    Honesty rule: a `note`/`error` string rather than a bare
    empty list, because "there is no ledger for this session" and "nothing was
    ever read" are different answers and a caller cannot tell them apart from
    `[]`.
    """
    empty = {"ops": [], "unmatched": 0, "evicted": 0, "total": 0}
    if not session:
        return {**empty, "error": "session is required"}

    ledger = _load_ledger()
    if ledger is None:
        return {**empty, "error": "ledger module could not be loaded"}

    entries = ledger.read_ledger(session)
    if not entries:
        return {**empty,
                "note": ("no ledger for this session — it ran before the "
                         "ledger existed, or no operation was recorded. This "
                         "is NOT the same as 'nothing happened'.")}

    try:
        from hermes_state import SessionDB
        rows = [dict(r) for r in
                SessionDB().get_messages(session, include_compacted=True)]
    except Exception as exc:  # noqa: BLE001 - report, never raise
        return {**empty, "total": len(entries),
                "error": f"{type(exc).__name__}: {exc}"}

    return {"session": session, **build_context_state(entries, rows)}


# ---------------------------------------------------------------------------
# /messages (Plan14-Task03) — the prompt as OUR addresses, carriage by carriage.
#
# THE THIRD WRITING ROUTE, and unlike the other two it writes on a GET. Said out
# loud because this file's header promises read-only with named exceptions, and
# this is one: the indexer hands out addresses, and an address is only useful if
# it is recorded. It is bounded the same way the others are — it appends to our
# own ledger under `<hermes home>/filetools/ledger/`, never to `state.db`.
#
# WHY A GET WRITES AT ALL. We do not own the moment a message appears (see the
# module docstring of `_msgindex`), so the index catches up whenever we are
# called. Idempotency is what makes that safe: a second pass over an unchanged
# session writes NOTHING — verified live 2026-09-09, and covered by tests.
#
# `?index=0` turns the writing off for a caller that only wants to look.
# ---------------------------------------------------------------------------

_MSG_KEYS = ("msg", "role", "msg_ts", "tool_call_id")


def _split_call_tokens(fact: dict, msg: dict, tk, estimator: dict, carriers: dict) -> None:
    """Вес для ПОКАЗА на чипе (владелец, 2026-10-06): тул — вызов + ответ, реплика — только проза.

    `tokens` строки не трогаем: итоги (полоса, «отмечено», вагон) честно считаются по строкам, и
    стирание ответа всё равно уносит его вызов. Здесь только две добавки:
    - строке `tool` — `call_tokens`, цена её вызова в строке-носителе (у `write_file` ответ в одну
      строку, а весь файл — в аргументах вызова);
    - строке `assistant` с вызовами — `prose_tokens`, её вес без вызовов.
    Носителя или вызова не нашли — поля нет, чип показывает строку как есть."""
    mult, markup = estimator["mult"], estimator["markup"]
    role = str(msg.get("role") or "")
    if role == "tool":
        call_id = str(msg.get("tool_call_id") or "")
        carrier = carriers.get(call_id) if call_id else None
        for call in (tk.row_calls(carrier) if isinstance(carrier, dict) else []):
            if isinstance(call, dict) and str(call.get("id") or (call.get("function") or {}).get("id") or "") == call_id:
                fact["call_tokens"] = tk.call_tokens(call, mult, markup)
                break
    elif role == "assistant":
        calls = [c for c in tk.row_calls(msg) if isinstance(c, dict)]
        if calls:
            spent = sum(tk.call_tokens(c, mult, markup) for c in calls)
            fact["prose_tokens"] = max(0, int(fact.get("tokens") or 0) - spent)


def _their_repairs(msgindex, rows) -> dict:
    """`id(строки) -> почему ОНИ её на загрузке поправят`.

    ЧТО МЫ НЕ ДЕЛАЕМ И ПОЧЕМУ. Модельный список они строят своим загрузчиком
    (`get_messages_as_conversation`), и он не равен активным строкам: чинит
    подряд идущие реплики человека, выбрасывает харнесс фонового ревью и голые
    маркеры тул-вызовов, гоняет текст через `sanitize_context`. Позвать его
    значило бы завести ВТОРОЙ шов в их состояние, и жирный — внутри него четыре
    чужих правила, которые меняются без предупреждения. Решение владельца
    2026-09-10: держим один шов (`get_messages`, уже в реестре ACHTUNG) и читаем
    базу сами, потому что схему базы им ломать дорого, а внутренности — нет.

    Цена решения названа честно: их правки мы не воспроизводим, а ПОМЕЧАЕМ то
    место, где они вмешаются. Дешёвый и устойчивый признак ровно один — две
    реплики человека подряд (на живой базе 82 таких места). Гадать за остальные
    их проходы не станем: сказать «здесь они что-то поправят» честнее, чем
    угадать неверно.
    """
    out = {}
    previous = None
    for row in rows or []:
        if msgindex.is_opener(row) and previous is not None and msgindex.is_opener(previous):
            out[id(row)] = "their loader folds consecutive human turns"
        previous = row
    return out


def _call_names(row) -> list:
    """Имена тулов, объявленных строкой ассистента.

    Форма `tool_calls` у провайдеров разная (плоский `name` или внутри
    `function`) — тот же разбор, что и у `_msgindex.call_ids`, но по имени.
    """
    out = []
    for call in ((row or {}).get("tool_calls") or []):
        if not isinstance(call, dict):
            continue
        name = (call.get("function") or {}).get("name") or call.get("name")
        if name:
            out.append(str(name))
    return out


# Шапка НАШЕГО рендера тул-ответа (`file_ops/*_core.py`, Plan09) — `Operation ID:`/`File:` в
# первых строках, иногда за комментарием (`read_file` оборачивает шапку видом открытого
# файла: `#Operation ID: ...`). ТА ЖЕ пара, что `core.js::OP_RE` тянет из тела кирпичика в
# стакане Operations — здесь читаем из ПОЛНОГО `content`, не из обрезанного `preview`
# (владелец, 2026-09-15: «на поверхности каждой операции должен выводиться её номер и файл»).
# Чужой тул или нефайловая операция (`terminal`, `search_files`) честно даёт "" — не мок.
# The pair is `_header_re("op")` / `_header_re("file")` — words from the dictionary.


# Ключи аргументов, которые стоит показать как короткий хинт вызова (Plan19-Task5:
# слитый кирпичик "имя тула | файл : адрес"). Список короткий и намеренно НЕ растёт под
# каждый тул — незнакомый набор аргументов честно даёт пустую строку, а не догадку.
_TARGET_ARG_KEYS = ("path", "file", "file_path", "filename", "target")


def _call_info(row) -> list:
    """По вызову — ОДИН объект `{name, target, id}`, одним проходом по `tool_calls`.

    НЕ параллельный массив к `_call_names`: там, где что-то отсутствует у вызова (нет
    имени, нет `id`), тот список молча пропускает запись и индексы съезжают — это и
    была реальная причина, почему пары вызов+ответ не находились в Задаче 5 живьём
    (владелец поймала на экране, 2026-09-15). Здесь каждый вызов — ровно одна запись,
    что бы у него ни было заполнено, поэтому индекс всегда значит «этот же вызов».
    """
    out = []
    for call in ((row or {}).get("tool_calls") or []):
        if not isinstance(call, dict):
            continue
        fn = call.get("function") or {}
        name = fn.get("name") or call.get("name")
        cid = call.get("id") or fn.get("id")
        raw_args = fn.get("arguments")
        parsed = None
        if isinstance(raw_args, str):
            try:
                parsed = json.loads(raw_args)
            except (TypeError, ValueError):
                parsed = None
        target = ""
        if isinstance(parsed, dict):
            for key in _TARGET_ARG_KEYS:
                val = parsed.get(key)
                if isinstance(val, str) and val:
                    target = val.replace("\\", "/").rsplit("/", 1)[-1]
                    break
        out.append({"name": str(name) if name else "", "target": target,
                     "id": str(cid) if cid else ""})
    return out


# ---------------------------------------------------------------------------
# ТЕКСТ ВЫЗОВА ДЛЯ ЭКРАНА (Plan22-Task01). Самый старый долг панели: команду терминала и код
# `execute_code`, отправленные агентом, посмотреть было НЕГДЕ. Аргументы на сервере
# разбирались дважды (`_args_index` здесь, `buildBrick` на клиенте) и оба раза выбрасывались
# после того, как из них слепили заголовок.
#
# ФОРМА: `{name, id, args, raw, cut}`.
#
# `args` — СТРУКТУРА, а не готовый текст: рендерер в панели строит из неё таблицу
# «ключ — значение», и получить её обратно разбором строки было бы вторым разбором того, что
# уже разобрано.
#
# `raw` — тот же вызов, напечатанный `context_ctl/_export.py::render_call`, то есть РОВНО то,
# что уходит в файл выгрузки. Сюда он едет готовым намеренно: глазик в панели обещает
# «покажи как есть», и собирать это «как есть» вторым кодом на клиенте значит однажды
# разойтись с файлом молча (ловушка 2 `Plan22`).
#
# `cut` — правду про обрезку говорим ВСЛУХ. В ленте значения режутся, иначе аргументы
# `execute_code` (десятки килобайт кода) поехали бы на каждый опрос; по `full=<адрес>`
# приходит целое. Без этого флага человек не отличил бы короткую команду от обрезанной.
#
# Режем ЗНАЧЕНИЯ, а не структуру: ключи остаются все. Обрезанная таблица — всё ещё таблица,
# и вид окна не прыгает при переходе от превью к полному.
_CALL_VALUE_CHARS = 400


#: Знак обрезки. Без него человек не отличает «текст кончился» от «текст обрезан» — и это не
#: теория: владелец 2026-09-23 считала символы в блокноте, чтобы понять, всё ли ей показали.
CUT_MARK = " …"


def _cut_value(value, limit: int):
    """Строку — подрезать, всё остальное отдать как есть (числа и флаги и так короткие)."""
    if limit <= 0 or not isinstance(value, str) or len(value) <= limit:
        return value
    return value[:limit] + CUT_MARK


def _call_payload(call, limit: int) -> dict:
    """Один вызов в форме для экрана. `limit<=0` — не резать.

    Неразобранные аргументы НЕ прячем: битый JSON это факт разговора, и показать его строкой
    честнее, чем сделать вид, что вызов был пустым.
    """
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = fn.get("name") or call.get("name") or ""
    cid = call.get("id") or (fn.get("id") if fn else None)
    raw_args = fn.get("arguments") if fn else call.get("arguments")
    args = raw_args
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args)
        except (TypeError, ValueError):
            args = raw_args

    if isinstance(args, dict):
        shown = {str(k): _cut_value(v, limit) for k, v in args.items()}
    else:
        shown = _cut_value(args, limit)
    cut = shown != args

    # `raw` печатается из УЖЕ ОБРЕЗАННЫХ аргументов, а не из исходного вызова. Так обе половины
    # глазика показывают ровно одно и то же по объёму — по построению, а не по совпадению
    # порогов. Раньше здесь стоял свой предел (`limit * 4`), и сырьё показывало вчетверо больше
    # рендера: переключение меняло не ВИД, а КОЛИЧЕСТВО текста (владелец, 2026-09-23, посчитала
    # символы). При `limit <= 0` (запрос `full=<адрес>`) резать нечего, и `raw` снова байт в байт
    # совпадает с файлом выгрузки — там, где это обещание и нужно.
    exporter = _load_export()
    raw = ""
    if exporter is not None:
        try:
            raw = exporter.render_call({"function": {"name": name, "arguments": shown}})
        except Exception:  # noqa: BLE001 - печать вызова не смеет ронять весь ответ
            raw = ""

    return {"name": str(name) if name else "", "id": str(cid) if cid else "",
            "args": shown, "raw": raw,
            "cut": bool(cut)}


def _call_of_tool_row(row, carriers, limit: int):
    """Вызов, ответом на который является ЭТА строка, или None.

    Берётся ТОЛЬКО свой вызов, по `tool_call_id`: у носителя их может быть несколько, и
    соседние — не его дело. То же правило владения, по которому живёт выгрузка
    (`context_ctl/_pairs.py`): у вызова нет своего адреса, приехать он может лишь с
    результатом, но и приезжает при этом РОВНО он.
    """
    call_id = str((row or {}).get("tool_call_id") or "")
    carrier = carriers.get(call_id) if call_id else None
    if carrier is None:
        return None
    exporter = _load_export()
    calls = exporter.calls_of(carrier) if exporter is not None else (carrier.get("tool_calls") or [])
    for call in calls:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        cid = call.get("id") or (fn.get("id") if fn else None)
        if str(cid or "") == call_id:
            return _call_payload(call, limit)
    return None


@router.get("/messages")
def messages(session: str = "", index: int = 1, preview: int = 0, full: str = ""):
    """Our message addresses for this session, grouped into carriages.

    A carriage is opened by a human's message — never closed by the model's last
    word: at index time the end may not have happened yet, while the beginning is
    unambiguous always. Live measurement behind that rule: 82 places in the base
    where two human messages run back to back, which the "by the end" rule would
    have glued into one carriage each.

    `preview=N` adds `text` — the first N characters of the row, cut bluntly at
    the character. Off by default, so the plain answer stays what it was: pure
    addressing. The "no texts" rule is about the LEDGER — nothing of this reaches
    a record; the route already reads the base, and the old `/ops` had always shown
    bodies. `chars` stays the full length, so the cut is visible.

    WE READ THE BASE BY THEIR RULE, WE DO NOT CALL THEIR ASSEMBLER. The rule is
    small and stable: active rows of the session, `ORDER BY id` (never timestamp
    — clocks regress), and `api_content` is what actually goes to the provider
    where the column is filled. All three are things we read ourselves through
    the ONE seam we already own, `get_messages`.

    Their loader (`get_messages_as_conversation`) additionally folds consecutive
    human turns, strips a background-review harness and bare tool-call markers,
    and sanitizes text. Calling it would buy exactness at the price of a second,
    much fatter seam into their internals — four foreign rules that change with
    no notice. Owner's call, 2026-09-10: schema they cannot break cheaply (every
    client has a big base and a migration would be needed), internals they can.
    So we stay on the schema, and the residual difference is NAMED instead of
    guessed — `their_repair` marks the place where they will intervene.

    Measured 2026-09-10 on the live base, so the size of that residue is known:
    session ...3b2787 has 180 active rows against 178 sent, and 10 rows whose
    sent bytes differ from `content` (one user turn: 28 stored, 133 sent). The
    second number is ours now; the first is the residue.
    """
    empty = {"carriages": [], "messages": [], "addressed": 0, "reused": 0}
    if not session:
        return {**empty, "error": "session is required"}

    ledger = _load_ledger()
    msgindex = _load_msgindex()
    if ledger is None or msgindex is None:
        return {**empty, "error": "ledger module could not be loaded"}

    try:
        from hermes_state import SessionDB
        db = SessionDB()
        rows = [dict(r) for r in db.get_messages(session)]
    except Exception as exc:  # noqa: BLE001 - report, never raise
        return {**empty, "error": f"{type(exc).__name__}: {exc}"}

    entries = ledger.read_ledger(session) or []
    summary = msgindex.index_session(session, rows, entries,
                                     write=bool(index))
    address_of = {}
    for address, row in (summary.get("by_msg") or {}).items():
        address_of[id(row)] = address

    # Read back so the answer describes what is RECORDED, not what we just
    # computed: those differ exactly when a write failed, and that is the case
    # worth seeing.
    fresh = ledger.read_ledger(session) or [] if index else entries
    recorded = {str(e.get("msg") or ""): e for e in ledger.message_records(fresh)}
    # Операции, привезённые импортом (Plan27): `edited` и `imported_from` записи — на чип по
    # `tool_call_id` (поправка 17). Род различает `is_event`, как везде.
    imported_ops = {str(e.get("tool_call_id")): e for e in fresh
                    if not ledger.is_event(e) and e.get("session_src") == "import"
                    and e.get("tool_call_id")}

    repairs = _their_repairs(msgindex, rows)
    # Токены строки — НАШ оценщик (Plan24, владелец 2026-09-29: «на наш оценщик завязать оценку
    # всех сообщений в поезде и его вагонах»), с разметкой шаблона: сколько строка стоит в промпте.
    tk = _load_tokens()
    envelope = _load_envelope()
    estimator = _session_estimator(tk, session) if tk is not None else None
    # Носители вызовов — ОДИН проход по ленте на весь ответ, а не поиск на каждую тул-строку.
    exporter = _load_export()
    carriers = exporter.carriers_by_call(rows) if exporter is not None else {}

    out_msgs = []
    for msg in rows:
        address = address_of.get(id(msg), "")
        entry = recorded.get(address) or {}
        fact = {k: entry.get(k) for k in _MSG_KEYS if entry.get(k) is not None}
        fact.setdefault("msg", address)
        fact.setdefault("role", msg.get("role"))
        # Из СЫРОЙ строки, не только из леджера: пары для слитого кирпичика (Plan19-Task5)
        # должны находиться и там, где записи в журнале нет вовсе (`unrecorded`/чужой тул) —
        # у самой строки-ответа `tool_call_id` есть всегда, это форма провайдера.
        fact.setdefault("tool_call_id", msg.get("tool_call_id"))
        # ЧТО РЕАЛЬНО УЕЗЖАЕТ: `api_content` вербатим, если он есть. Показывать
        # `content` там, где провайдер получает другое, значит показывать не тот
        # разговор — ровно та ошибка, ради которой это зеркало и строится.
        body = msg.get("api_content")
        content = str(body if body else (msg.get("content") or ""))
        fact["chars"] = len(content)
        if estimator is not None:
            fact["tokens"] = tk.row_tokens(msg, estimator["mult"], estimator["markup"])
            _split_call_tokens(fact, msg, tk, estimator, carriers)
        if body:
            fact["sent_as"] = "api_content"
            stored = len(str(msg.get("content") or ""))
            if stored != len(content):
                fact["db_chars"] = stored
        if str(fact.get("role") or "") == "tool":
            imported = imported_ops.get(str(msg.get("tool_call_id") or ""))
            if imported is not None:
                fact["imported_from"] = imported.get("imported_from")
                if imported.get("edited"):
                    fact["edited"] = True
            op_m = _header_re("op").search(content)
            if op_m:
                fact["op"] = op_m.group(1)
            file_m = _header_re("file").search(content)
            if file_m:
                full_path = _unframe(file_m.group(1), content, op_m).replace("\\", "/")
                fact["file"] = full_path.rsplit("/", 1)[-1]
                fact["path"] = full_path
            # Статусы чипа (Plan23-Task03): исход и объявленное состояние — у КАЖДОЙ тул-строки,
            # не только у наших операций. Пустое — не кладём.
            kind = _row_kind(content)
            if kind != "ok":
                fact["kind"] = kind
            state = _declared_state(content)
            if state:
                fact["state"] = state
        if preview > 0:
            # `full` — РОВНО ОДИН адрес, чей текст едет целиком. Так окно по клику
            # читает своё сообщение, не заставляя весь список тащить полные тела:
            # поднимать `preview` для всех ради одной карточки — это платить за
            # 235 строк, чтобы прочитать одну.
            fact["text"] = content if (full and address == full) else content[:preview]
            # Служебная строка компакции — по ПОЛНОМУ тексту: метка стоит в конце первой скобки,
            # и в обрезок `preview` она не попадает (живьём 2026-09-30: мостик и скобка не красились).
            service = envelope.service_kind(content) if envelope is not None else ""
            if service:
                fact["distill"] = service
            # Строка ассистента, которая ТОЛЬКО зовёт тулы, пуста по содержимому:
            # без имён вызовов её плитка выглядела бы потерянной, хотя всё цело.
            names = _call_names(msg)
            if names:
                fact["calls"] = names
            info = _call_info(msg)
            if info:
                fact["call_info"] = info
            name = msg.get("tool_name") or msg.get("name")
            if name:
                fact["name"] = str(name)
            # ВЫЗОВ — только у тул-строк (решение владельца 2026-09-23): у реплики её вызовы
            # уже показаны своими вагончиками, и повторять их в её окне значит показать одно и
            # то же дважды. Заодно это держит ответ маленьким.
            if str(fact.get("role") or "") == "tool":
                whole = bool(full and address == full)
                call = _call_of_tool_row(msg, carriers,
                                         0 if whole else _CALL_VALUE_CHARS)
                if call is not None:
                    fact["call"] = call
        if id(msg) in repairs:
            fact["their_repair"] = repairs[id(msg)]
        out_msgs.append(fact)

    # Чужие вызовы внутри хода НАЗЫВАЮТСЯ, а не проглатываются: около десятой
    # части тул-строк живой базы не наши (`web_search`, `delegate_task`, `todo` и
    # ещё 26 имён), а действие над ходом уносит и их.
    # «Журнал про этот вызов молчит» — это ДВА РАЗНЫХ факта, и звать их одним
    # словом значит прятать второй. Чужой тул (`web_search`, `delegate_task`, …)
    # молчания заслуживает: записи о нём и быть не должно. А вот НАША строка без
    # записи — дырка в журнале, и найдена она живьём: на сессии ...7221ba две
    # тул-строки из десяти несут наш заголовок (`Operation ID: 0016`, `001c`), а
    # записи о них нет. Различает их `_is_ours` — тот же проверенный признак, по
    # которому стакан операций решает, чья строка.
    unknown = msgindex.foreign_call_ids(rows, fresh)
    cars = []
    for car in msgindex.carriages(rows):
        opener = address_of.get(id(car["opener"]), "") if car["opener"] else None
        silent = [r for r in car["rows"]
                  if str(r.get("tool_call_id") or "") in unknown]
        cars.append({
            "opener": opener or None,
            "members": [address_of.get(id(r), "") for r in car["rows"]],
            "steps": [[address_of.get(id(r), "") for r in step["rows"]]
                      for step in msgindex.steps(car["rows"])],
            "foreign": [address_of.get(id(r), "") for r in silent
                        if not _is_ours(r)],
            "unrecorded": [address_of.get(id(r), "") for r in silent
                           if _is_ours(r)],
        })

    return {"session": session, "messages": out_msgs, "carriages": cars,
            "addressed": summary.get("addressed", 0),
            "reused": summary.get("reused", 0),
            "skipped": summary.get("skipped", 0),
            "layouts": summary.get("layouts", 0),
            "rows": len(rows), "repairs": len(repairs),
            "indexed": bool(index),
            "estimator": ({k: estimator[k] for k in ("model", "fp", "exact_model", "mult")}
                          if estimator is not None else None)}


# ---------------------------------------------------------------------------
# /events (Plan11-Task06) — the channel, deliberately before the cargo.
#
# Built now on the owner's explicit decision (2026-09-07): "the socket first,
# even if it carries an empty payload — the channel matters, not the cargo."
# Reason: without a server->client push the panel can only learn that the
# context changed by being told to look, and the one moment we cannot cover
# that way is an AUTOMATIC compaction nobody clicked a button for.
#
# The door is Hermes' own (`Recon19`): `@router.websocket` on a plugin's router
# mounts like any other route, against `ctx.socket('/events', cb)` in
# `@hermes/plugin-sdk`. Working precedent: `plugins/kanban/dashboard/plugin_api.py`.
#
# NOTE on what this does NOT do: the compaction signal itself already reaches
# the desktop as Hermes' own `status.update{kind:"compacted"}` event
# (`Recon19`), and subscribing to THAT is Task07. This socket exists for what
# only WE can know — future pushes about our own ledger — and right now it
# proves the pipe end to end and nothing more. An honest empty channel beats a
# channel faked with invented traffic.
# ---------------------------------------------------------------------------


def _ws_upgrade_authorized(ws) -> bool:
    """Authorize the upgrade by DELEGATING to the dashboard's canonical gate.

    Delegating rather than checking a token ourselves is a lesson already paid
    for next door: kanban's own bespoke check understood only `_SESSION_TOKEN`
    and therefore rejected its live-events socket on every OAuth-gated
    deployment, while the rest of the dashboard worked
    (`plugins/kanban/dashboard/plugin_api.py:65`, their comment). Routing
    through the shared gate means this can never drift from core auth.

    Browsers cannot set `Authorization` on a WS upgrade, so the credential
    rides in the query string; the SDK's own `buildWsUrl` assembles it.

    Imported lazily, and an import failure ACCEPTS: without a dashboard there
    is no auth to enforce and no browser to protect — that is the bare-FastAPI
    test harness, and refusing there would make the endpoint untestable while
    protecting nothing.
    """
    try:
        from hermes_cli import web_server as _ws
    except Exception:  # noqa: BLE001 - no dashboard context (tests)
        return True
    try:
        return bool(_ws._ws_auth_ok(ws))
    except Exception:  # noqa: BLE001 - a gate that errors must not 500 the upgrade
        return False


# How often an open panel looks for a new revision. A second is far below what a
# human notices and far above what one integer costs; nothing else is read.
_POLL_SECONDS = 1.0


@router.websocket("/events")
async def events(ws):
    """Hold a socket open per panel so the server can speak first later.

    Sends a `hello` frame on connect — enough for the client to confirm the pipe
    end to end — then holds, and now also SPEAKS: a `context-changed` frame
    whenever a human removes an operation from this session's context.

    WHY THIS POLLS, having said before that a heartbeat we do not need would be
    invented traffic. That was true while nothing had anything to say. Now
    something does, and it cannot reach us any other way: `/ctx-drop` runs in the
    AGENT process, these routes are mounted in the dashboard's web server, and
    the only thing the two share is the file tree — the same door the focus
    file already goes through. The panel's other two triggers cannot cover it either:
    our own is `tool.complete` (a slash command is not a tool call) and theirs is
    `status.update` with `kind: "compacted"`, which their compaction pipeline
    emits and we deliberately no longer go through. Without this the panel showed
    a stale snapshot until Hermes was restarted — seen live 2026-09-09.

    What is polled is ONE integer in one small file, only while a panel is open,
    and only the CHANGE is sent. The revision is a counter rather than an mtime
    so the comparison does not depend on clock behaviour or filesystem
    resolution.
    """
    import asyncio
    if not _ws_upgrade_authorized(ws):
        try:
            from starlette.status import WS_1008_POLICY_VIOLATION
        except Exception:  # noqa: BLE001
            WS_1008_POLICY_VIOLATION = 1008
        await ws.close(code=WS_1008_POLICY_VIOLATION)
        return

    await ws.accept()

    # Pinned at the handshake, like kanban pins its board: a subscription that
    # changed session mid-stream would need two cursors reconciled, and the
    # panel can simply open a new socket when the chat changes.
    session = ""
    try:
        session = ws.query_params.get("session", "") or ""
    except Exception:  # noqa: BLE001 - a stubbed ws in tests may lack query_params
        session = ""

    try:
        await ws.send_json({
            "kind": "hello",
            "plugin": PLUGIN_ID,
            "session": session,
            # Which hermes home the WEB SERVER resolved. Same reason `/ping`
            # reports it: the web server resolves its own home, and a silent
            # split-brain against the home the agent writes to is invisible
            # from anything else.
            "ledger_dir": _ledger_dir_or_none(),
        })
        # The revision AS OF the handshake. A drop that happened before the panel
        # connected is already in the snapshot it is about to fetch, so firing on
        # it would be a pointless extra round trip on every chat switch.
        seen = context_revision(session)

        pending = asyncio.ensure_future(ws.receive())
        try:
            while True:
                # Any frame the client sends is still ignored on purpose — there
                # is no client→server protocol, and inventing one before its
                # first user would be guessing. The read is here to notice a
                # disconnect, which raises out of it.
                done, _ = await asyncio.wait({pending}, timeout=_POLL_SECONDS)
                if pending in done:
                    message = pending.result()
                    # Starlette hands a disconnect back as a MESSAGE, it does not
                    # always raise. Re-arming the read on a dead socket would leave
                    # this loop polling a file for a panel that is gone.
                    if isinstance(message, dict) and                             message.get("type") == "websocket.disconnect":
                        return
                    pending = asyncio.ensure_future(ws.receive())
                    continue
                current = context_revision(session)
                if current != seen:
                    seen = current
                    await ws.send_json({
                        "kind": "context-changed",
                        "plugin": PLUGIN_ID,
                        "session": session,
                        "revision": current,
                    })
        finally:
            pending.cancel()
    except Exception:  # noqa: BLE001 - disconnect is the normal exit
        return


def _ledger_dir_or_none():
    ledger = _load_ledger()
    if ledger is None:
        return None
    try:
        return str(ledger.ledger_dir())
    except Exception:  # noqa: BLE001 - report absence, never raise
        return None


# ---------------------------------------------------------------------------
# /context-focus — which chat the panel has open (`context_ctl/_select.py`).
#
# Writes ONE file of OUR OWN under `<hermes home>/filetools/ctxctl/select/`, never
# touches `state.db`, never rewrites the ledger, never talks to the model. The
# slash command reads it to know which chat it was typed in.
#
# The panel's PENDING SELECTION (`/context-select`, Plan13-Task02) lived here too
# and is gone (Plan23, 2026-10-06): operations travel by m-address in the body of
# `POST /context-operation`, one transport, no selection file.
#
# Auth: none of its own, on purpose. Every route here relies on the dashboard's
# own gate at the mount point, and a bespoke check beside it is the mistake
# `_ws_upgrade_authorized` documents (kanban's own token check rejected valid
# OAuth deployments). A writing route must not invent a second, weaker gate.
# ---------------------------------------------------------------------------

_SELECT_MODULE = "hermes_filetools_ctxctl_select"


def _load_select():
    """Return `context_ctl/_select.py`, or None if it cannot be loaded.

    Same by-path loading as `_load_ledger`, for the same two reasons — and this
    is exactly why that module is written with no relative imports: here it is
    not part of any package.
    """
    cached = sys.modules.get(_SELECT_MODULE)
    if cached is not None:
        return cached
    path = _PLUGIN_ROOT / "context_ctl" / "_select.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(_SELECT_MODULE, path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_SELECT_MODULE] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(_SELECT_MODULE, None)
        return None
    return mod


_SIGNAL_MODULE = "hermes_filetools_dashboard_signal"


def _load_signal():
    """Return `context_ctl/_signal.py`, or None. By path, same as the ledger and select."""
    cached = sys.modules.get(_SIGNAL_MODULE)
    if cached is not None:
        return cached
    path = _PLUGIN_ROOT / "context_ctl" / "_signal.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(_SIGNAL_MODULE, path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_SIGNAL_MODULE] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(_SIGNAL_MODULE, None)
        return None
    return mod


def context_revision(session: str) -> int:
    """How many times a human has applied a removal in this session. 0 = never, or unknown.

    Pure and boring on purpose: it is polled, so it must never raise and never
    block. An unreadable signal reads as 0, which simply means "no frame to
    send" — the panel keeps the snapshot it has instead of thrashing.
    """
    signal = _load_signal()
    if signal is None or not session:
        return 0
    try:
        return int(signal.read_signal(session).get("revision") or 0)
    except Exception:  # noqa: BLE001 - polled: report nothing, never raise
        return 0


@router.get("/context-revision")
def context_revision_route(session: str = ""):
    """How many removals this session has had. The panel polls this to know it must refetch.

    WHY A POLLED ROUTE, when `/events` already pushes a `context-changed` frame. Because
    the push did not arrive — twice, in the field, for two different reasons (a gateway
    still serving the previous version of the websocket, and once more with everything
    deployed). REST to these routes is PROVEN to work in the same panel at the same moment:
    /context-state and /messages answer. A channel that works beats a channel that
    is architecturally nicer, and the human should not have to restart the client to see
    what she just did.

    One integer, no joins, no DB. The socket frame stays as the fast path when it works.
    """
    if not session:
        return {"revision": 0, "error": "session is required"}
    return {"revision": context_revision(session), "session": session}


@router.post("/context-focus")
async def context_focus_post(request: Request):
    """Which chat the panel currently has open. Written by the panel, read by /ctx-drop.

    This route exists so nothing has to PATCH Hermes to learn the session of a slash
    command. The command API hands its handler only the argument string, so the session had
    to come from somewhere; the first two answers were a guess (which chat answered last)
    and a monkeypatch on their dispatcher. The owner's objection was exactly right: the
    panel already knows the session — it supplies it to every route it calls — so it can
    simply write it down.
    """
    select = _load_select()
    if select is None:
        return {"session": "", "error": "select module could not be loaded"}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body is a client error, not a 500
        return {"session": "", "error": "body must be JSON"}
    if not isinstance(body, dict):
        return {"session": "", "error": "body must be a JSON object"}
    try:
        return select.write_focus(str(body.get("session") or ""))
    except Exception as exc:  # noqa: BLE001 - report, never raise
        return {"session": "", "error": f"{type(exc).__name__}: {exc}"}


@router.get("/context-focus")
def context_focus_get():
    """What the panel last reported. Diagnostics for `/filetools-context-status`."""
    select = _load_select()
    if select is None:
        return {"session": "", "error": "select module could not be loaded"}
    return select.read_focus()


# ---------------------------------------------------------------------------
# Apply. The routes that change the model's context rather than a file of our own.
#
# Why routes and not only the slash command (owner, 2026-09-09): "the panel is
# where context work is done; commands are hyper-inconvenient". The command
# stays, and since Plan23 (2026-10-06) it goes through the SAME door: one
# transport by m-address (`POST /context-operation` -> `apply_addresses`); the
# operation-number route `POST /context-apply` is gone.
#
# Why this does not import our engine: this file is loaded BY PATH, outside the
# package, so a relative import is impossible and guessing our package name in
# `sys.modules` would add yet another private seam of their plugin loader. The
# live engine, however, is already reachable the way THEIR own dispatcher
# reaches it -- `_sessions[key]["agent"]` -- and the engine object is part of
# our package, where imports are ordinary. So we look the engine up and call one
# duck-typed method on it: `apply_addresses(operation, msgs)`.
# ---------------------------------------------------------------------------

def _engine_for(session: str):
    """`(engine, reason)` for a session id as the PANEL knows it.

    Two lookups, in this order, because the gateway's registry key is not
    required to equal `agent.session_id` -- their own
    `_sync_session_key_after_compress` changes that key. So: by key first (the
    normal case, and what their dispatcher does), then by asking each live
    session's engine for its own id. Anything else refuses by name: applying to
    a session the human is not looking at is worse than not applying at all.
    """
    try:
        from tui_gateway import server as gw
    except Exception as exc:  # noqa: BLE001 - private seam; absence is a reason, not a 500
        return None, f"tui_gateway.server is not importable ({exc})"
    registry = getattr(gw, "_sessions", None)
    if not isinstance(registry, dict):
        return None, "the gateway session registry (_sessions) is gone or changed type"

    def engine_of(entry):
        if not isinstance(entry, dict):
            return None
        engine = getattr(entry.get("agent"), "context_compressor", None)
        if engine is None or getattr(engine, "name", "") != "ctxctl":
            return None
        return engine if callable(getattr(engine, "apply_addresses", None)) else None

    entry = registry.get(session)
    if entry is not None:
        engine = engine_of(entry)
        if engine is not None:
            return engine, f"session {session}"
        return None, (f"session {session} exists but carries no ctxctl engine — it started "
                      "before `context.engine: ctxctl` was on, or before the plugin loaded")

    live = []
    for other in list(registry.values()):
        engine = engine_of(other)
        if engine is None:
            continue
        own = str(getattr(engine, "_session_id", "") or "")
        if own == session:
            return engine, f"session {session} (matched by engine id; the registry key differs)"
        live.append(own or "?")
    return None, (f"session {session} is not among the live ones. Live with an engine: "
                  + (", ".join(live) if live else "none"))


def _engine_from_bridge(session: str):
    """`(engine, reason)` from OUR process registry of engines — for READING only.

    A subagent's agent is built by the agent itself (`delegate_task`), never enters the
    gateway's `_sessions`, so `_engine_for` cannot see it — yet its engine runs and decides on
    compaction like any other (live 2026-10-11, `platform: subagent`). Every engine registers
    itself at session start (`context_ctl/_bridge.py::remember_engine`) in the bridge state kept
    on `run_agent.AIAgent.__init__._ctxctl_state` (the bridge's own seam). Used by `/tokens`
    only: the writing routes keep `_engine_for` — a manual compaction or LLMOps needs the
    gateway session that a subagent does not have.
    """
    try:
        import run_agent
        state = getattr(run_agent.AIAgent.__init__, "_ctxctl_state", None)
        engine = (state or {}).get("engines", {}).get(session) if isinstance(state, dict) else None
    except Exception:  # noqa: BLE001 - no bridge is a reason, not a 500
        return None, "the engine registry is not reachable"
    if engine is None or getattr(engine, "name", "") != "ctxctl":
        return None, f"session {session} has no engine in the registry"
    try:
        alive = engine.live_agent() is not None
    except Exception:  # noqa: BLE001
        alive = False
    if not alive:
        return None, f"session {session}: its agent is gone"
    return engine, f"session {session} (not in the gateway — a subagent; read from our engine registry)"


@router.post("/context-operation")
async def context_operation_post(request: Request):
    """Apply an operation to MESSAGE ADDRESSES (Plan16-Task08): `content` or `erase`.

    One door for every operation whose subject is messages (`m0007`) rather than our operation
    numbers (owner, 2026-09-29). `content` drops the body of tool results — ours AND foreign,
    which have no operation number. The ONLY door since Plan23 (2026-10-06). `erase` removes
    any message by the ownership rule (`_pairs.settle`): a result takes its call with it.

    Body: `{session, operation, msgs}`. IRREVERSIBLE; an unknown operation is refused by name.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body is a client error, not a 500
        return {"ok": False, "error": "body must be JSON"}
    if not isinstance(body, dict):
        return {"ok": False, "error": "body must be a JSON object"}
    session = str(body.get("session") or "").strip()
    if not session:
        return {"ok": False, "error": "session is required"}
    operation = str(body.get("operation") or "").strip()
    msgs = body.get("msgs")
    if not isinstance(msgs, list):
        return {"ok": False, "error": "msgs must be a list of message addresses"}
    msgs = [str(m).strip() for m in msgs if str(m).strip()]
    if not msgs:
        return {"ok": False, "session": session, "error": "nothing to apply: no message is ticked"}
    engine, reason = _engine_for(session)
    if engine is None:
        return {"ok": False, "session": session, "error": reason}
    try:
        report = engine.apply_addresses(operation, msgs)
    except Exception as exc:  # noqa: BLE001 - report, never raise
        return {"ok": False, "session": session, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(report, dict):
        return {"ok": False, "session": session,
                "error": "the engine returned something other than a report"}
    return {**report, "engine": reason}


_IMPORT_MODULE = "hermes_filetools_ctxctl_import"


def _load_import():
    """Return `context_ctl/_import.py`, or None. By path, same as `_load_export`."""
    cached = sys.modules.get(_IMPORT_MODULE)
    if cached is not None:
        return cached
    path = _PLUGIN_ROOT / "context_ctl" / "_import.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(_IMPORT_MODULE, path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_IMPORT_MODULE] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(_IMPORT_MODULE, None)
        return None
    return mod


def import_check(folder: str) -> dict:
    """Проверка папки без записи: сколько сообщений соберётся и какие проблемы у пар.

    Тот же разбор (`_import.load`) и тот же линт пар, что у настоящего импорта — иначе
    «проверено» и «применено» могли бы разойтись. Линт — копия правила `_stubs.lint_pairs`
    не нужна: пары собирает сам `_import`, и всё, что он не смог связать, уже в `problems`;
    вызов без результата ловим здесь по тем же `tool_call_id`.
    """
    importer = _load_import()
    if importer is None:
        return {"ok": False, "error": "import module could not be loaded"}
    loaded = importer.load(folder)
    rows = loaded["rows"]
    answered = {str(r.get("tool_call_id")) for r in rows
                if r.get("role") == "tool" and r.get("tool_call_id")}
    problems = list(loaded["problems"])
    for r in rows:
        for call in r.get("tool_calls") or []:
            if str(call.get("id")) not in answered:
                name = (call.get("function") or {}).get("name") or "?"
                problems.append(f"вызов {name} без результата (call_without_result)")
    roles = {}
    for r in rows:
        roles[r["role"]] = roles.get(r["role"], 0) + 1
    return {"ok": not problems and bool(rows), "folder": str(folder), "files": loaded["files"],
            "messages": len(rows), "roles": roles, "problems": problems}


# Режимы импорта — та же тройка, что `context_ctl/_import.MODES` (Plan27). Здесь своя копия:
# модуль импорта грузится лениво, а маршрут проверяет тело до него.
IMPORT_MODES = ("replace", "tail", "head")


@router.post("/context-import")
async def context_import_post(request: Request):
    """Импорт сессии из папки выгрузки С ПЕРЕЗАПИСЬЮ текущей ленты (владелец, 2026-09-29).

    Body: `{session, folder, apply, mode?}`. Без `apply` — только проверка (`import_check`),
    ничего не пишет и движка не требует. С `apply: true` — `engine.apply_import(folder, mode)`:
    `mode` `replace` (по умолчанию — вся живая лента заменяется собранной из файлов), `tail`
    (дописать в хвост), `head` (вставить в голову) — `Plan27`. IRREVERSIBLE для контекста
    (при `replace` прежние строки — в их архиве).
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body is a client error, not a 500
        return {"ok": False, "error": "body must be JSON"}
    if not isinstance(body, dict):
        return {"ok": False, "error": "body must be a JSON object"}
    session = str(body.get("session") or "").strip()
    folder = str(body.get("folder") or "").strip()
    if not folder:
        return {"ok": False, "session": session, "error": "folder is required"}
    if not body.get("apply"):
        return {"session": session, **import_check(folder)}
    if not session:
        return {"ok": False, "error": "session is required"}
    mode = str(body.get("mode") or "replace").strip()
    if mode not in IMPORT_MODES:
        return {"ok": False, "session": session,
                "error": f"mode must be one of {', '.join(IMPORT_MODES)}"}
    engine, reason = _engine_for(session)
    if engine is None:
        return {"ok": False, "session": session, "error": reason}
    if not callable(getattr(engine, "apply_import", None)):
        return {"ok": False, "session": session,
                "error": "the live engine has no import yet — restart the Hermes client"}
    try:
        # Старый движок (до `Plan27`) режима не знает: только `replace`, иначе — внятный отказ.
        report = (engine.apply_import(folder) if mode == "replace"
                  else engine.apply_import(folder, mode))
    except Exception as exc:  # noqa: BLE001 - report, never raise
        return {"ok": False, "session": session, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(report, dict):
        return {"ok": False, "session": session,
                "error": "the engine returned something other than a report"}
    return {**report, "engine": reason}


@router.post("/context-edit")
async def context_edit_post(request: Request):
    """`paste` (Vision09): заменить ТЕКСТ одного сообщения человека или прозы ассистента.

    Body: `{session, msg, content}`. Роль, вызовы и прочие поля строки переносятся байт в байт,
    тул-строки не правятся; адрес правленого сообщения меняется. IRREVERSIBLE для контекста
    (прежняя строка — в их архиве, связь было → стало — запись `edited` в журнале).
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body is a client error, not a 500
        return {"ok": False, "error": "body must be JSON"}
    if not isinstance(body, dict):
        return {"ok": False, "error": "body must be a JSON object"}
    session = str(body.get("session") or "").strip()
    msg = str(body.get("msg") or "").strip()
    content = body.get("content")
    if not session or not msg:
        return {"ok": False, "session": session, "error": "session and msg are required"}
    if not isinstance(content, str):
        return {"ok": False, "session": session, "error": "content must be a string"}
    engine, reason = _engine_for(session)
    if engine is None:
        return {"ok": False, "session": session, "error": reason}
    if not callable(getattr(engine, "apply_edit", None)):
        return {"ok": False, "session": session,
                "error": "the live engine has no edit yet — restart the Hermes client"}
    try:
        report = engine.apply_edit(msg, content)
    except Exception as exc:  # noqa: BLE001 - report, never raise
        return {"ok": False, "session": session, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(report, dict):
        return {"ok": False, "session": session,
                "error": "the engine returned something other than a report"}
    return {**report, "engine": reason}


@router.post("/context-compact-pick")
async def context_compact_pick(request: Request):
    """Галочки кнопки компакции — от СЕРВЕРА, тем же отбором, что у автомата (`engine.compact_pick`,
    `context_ctl/_cut.py::select`). Body: `{session, share?}` — `share` в процентах, разовое поле
    кнопки. Один механизм на кнопку, автомат и слеш-команду (владелец, 2026-09-29)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body is a client error, not a 500
        return {"ok": False, "error": "body must be JSON"}
    session = str((body or {}).get("session") or "").strip() if isinstance(body, dict) else ""
    if not session:
        return {"ok": False, "error": "session is required"}
    engine, reason = _engine_for(session)
    if engine is None:
        return {"ok": False, "session": session, "error": reason}
    if not callable(getattr(engine, "compact_pick", None)):
        return {"ok": False, "session": session,
                "error": "the live engine has no compact_pick yet — restart the Hermes client"}
    share = body.get("share")
    try:
        share = None if share in (None, "") else float(share) / 100.0
    except (TypeError, ValueError):
        return {"ok": False, "session": session, "error": "share must be a percent number"}
    try:
        out = engine.compact_pick(share)
    except Exception as exc:  # noqa: BLE001 - report, never raise
        return {"ok": False, "session": session, "error": f"{type(exc).__name__}: {exc}"}
    out = dict(out)
    out.pop("report", None)
    return {**out, "session": session, "engine": reason}


@router.post("/tokens")
async def tokens_post(request: Request):
    """Все числа о токенах сессии, каждое со своим источником (`Plan24`, `context_ctl/_tokcount.py`).

    Body: `{session, exact?, load?}`. `exact` — ещё и точный счёт сервером модели; `load` —
    разрешить загрузку незагруженной модели (кнопка `load`). Без `load` к модели, которая не
    `loaded`, запросов нет: роутер llama.cpp на такой запрос начинает её грузить.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body is a client error, not a 500
        return {"ok": False, "error": "body must be JSON"}
    if not isinstance(body, dict):
        return {"ok": False, "error": "body must be a JSON object"}
    session = str(body.get("session") or "").strip()
    if not session:
        return {"ok": False, "error": "session is required"}
    engine, reason = _engine_for(session)
    if engine is None:
        # A subagent is not in the gateway; its engine is in ours (reading only).
        engine, why = _engine_from_bridge(session)
        if engine is None:
            return {"ok": False, "session": session, "error": f"{reason}; {why}"}
        reason = why
    if not callable(getattr(engine, "token_report", None)):
        return {"ok": False, "session": session,
                "error": "the live engine has no token counter yet — restart the Hermes client"}
    try:
        report = engine.token_report(exact="auto" if body.get("exact") == "auto" else bool(body.get("exact")),
                                     allow_load=bool(body.get("load")))
    except Exception as exc:  # noqa: BLE001 - report, never raise
        return {"ok": False, "session": session, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(report, dict):
        return {"ok": False, "session": session, "error": "the engine returned something other than a report"}
    return {"ok": "error" not in report, **report, "engine": reason}


# ---------------------------------------------------------------------------
# /context-compact-start, /context-compact-status (Plan16-Task03) — the Mirror's fourth
# rail, compaction. The subject is MESSAGE addresses (`m0007`), not `ops`, and the rail
# already speaks in those — no resolution step needed.
#
# Compaction runs minutes, in the background, on the AGENT half's own thread
# (`context_ctl._compact._worker`). This route only STARTS it (`engine.start_compact`,
# same "method on the live engine" trick `apply_addresses` uses, for the same reason — this
# module cannot import our package). The SECOND route reads the run's progress from a
# FILE (`context_ctl/_compact_state.py`), because the agent half's own in-memory status
# (`_compact._state_now`) lives in a module this half cannot see: two Python halves of the
# same running server, but this file is loaded BY PATH outside the package, so a second
# import of `_compact.py` here would only give a second, empty module instance — not the
# one the background thread is actually mutating.
# ---------------------------------------------------------------------------

_COMPACT_STATE_MODULE = "hermes_filetools_dashboard_compact_state"


def _load_compact_state():
    """Return `context_ctl/_compact_state.py`, or None. By path, same as select/signal."""
    cached = sys.modules.get(_COMPACT_STATE_MODULE)
    if cached is not None:
        return cached
    path = _PLUGIN_ROOT / "context_ctl" / "_compact_state.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(_COMPACT_STATE_MODULE, path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_COMPACT_STATE_MODULE] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(_COMPACT_STATE_MODULE, None)
        return None
    return mod


@router.post("/context-compact-start")
async def context_compact_start_post(request: Request):
    """`POST /context-compact-start` — the compaction button's second click, confirmed.

    Body: `{session, addrs}` — `addrs` is the rail's ticked m-addresses AS-IS, no `ops`
    resolution. Answers immediately (`{"ok": True}` = started, not "applied") — the run
    itself is minutes long; its outcome is `GET /context-compact-status`.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body is a client error, not a 500
        return {"ok": False, "error": "body must be JSON"}
    if not isinstance(body, dict):
        return {"ok": False, "error": "body must be a JSON object"}

    session = str(body.get("session") or "").strip()
    if not session:
        return {"ok": False, "error": "session is required"}

    addrs = body.get("addrs")
    if not isinstance(addrs, list):
        return {"ok": False, "error": "addrs must be a list of message addresses"}
    addrs = [str(a).strip() for a in addrs if str(a).strip()]
    if not addrs:
        return {"ok": False, "error": "nothing to compact: no message is ticked"}

    engine, reason = _engine_for(session)
    if engine is None:
        return {"ok": False, "session": session, "error": reason}
    try:
        report = engine.start_compact(addrs)
    except Exception as exc:  # noqa: BLE001 - report, never raise
        return {"ok": False, "session": session, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(report, dict):
        return {"ok": False, "session": session,
                "error": "the engine returned something other than a report"}
    return {**report, "engine": reason}


# ---------------------------------------------------------------------------
# LLMOps window (Plan31 Task 2): data, chat, models, presets. Same "method on the live engine"
# trick as compaction — this half cannot import our package. The model route is compaction's
# interrogation route (session's or our own).
# ---------------------------------------------------------------------------


def _llmops_engine(session: str, method: str):
    """`(engine, None)` or `(None, error answer)`."""
    session = str(session or "").strip()
    if not session:
        return None, {"ok": False, "error": "session is required"}
    engine, reason = _engine_for(session)
    if engine is None:
        return None, {"ok": False, "session": session, "error": reason}
    if not callable(getattr(engine, method, None)):
        return None, {"ok": False, "session": session,
                      "error": f"the live engine has no {method} yet — restart the Hermes client"}
    return engine, None


@router.get("/llmops-data")
def llmops_data(session: str = "", addrs: str = ""):
    """`GET /llmops-data?session&addrs=m0034,m0035` — the window's input, cut as compaction cuts it:
    `{ok, prefix, target, bubbles, suffix, frames, addrs, lines}`. A message: `{addr, role, text,
    tokens, tool?, calls?}`. `frames` — directive frames stripped from the target; `addrs` — what a
    replace will eat (service rows included)."""
    engine, err = _llmops_engine(session, "llmops_data")
    if err:
        return err
    wanted = [a.strip() for a in str(addrs or "").split(",") if a.strip()]
    if not wanted:
        return {"ok": False, "error": "addrs is required"}
    try:
        return engine.llmops_data(wanted)
    except Exception as exc:  # noqa: BLE001 - report, never raise
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.post("/llmops-chat")
async def llmops_chat(request: Request):
    """`POST /llmops-chat {session, messages, model?, think?}` — the WHOLE window conversation in one
    request: `{ok, text, model, tokens}` or `{ok: False, error}`."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "body must be JSON"}
    if not isinstance(body, dict):
        return {"ok": False, "error": "body must be a JSON object"}
    engine, err = _llmops_engine(body.get("session"), "llmops_chat")
    if err:
        return err
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return {"ok": False, "error": "messages must be a non-empty list"}
    import asyncio
    try:
        # The model answers in minutes — off the event loop.
        return await asyncio.to_thread(engine.llmops_chat, messages,
                                       str(body.get("model") or ""), bool(body.get("think")))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.post("/llmops-stop")
async def llmops_stop(request: Request):
    """`POST /llmops-stop {session}` — ■ Stop окна: оборвать идущий `/llmops-chat` (его сокет к
    модели гасится, тот запрос отвечает `{ok: False, stopped: True}`). `{ok, cut}`."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    engine, err = _llmops_engine((body or {}).get("session") if isinstance(body, dict) else "", "llmops_stop")
    if err:
        return err
    try:
        return engine.llmops_stop()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/llmops-models")
def llmops_models(session: str = ""):
    """`GET /llmops-models?session` — `{ok, session: <route model>, list}`; no list from the server —
    the route model alone plus `note`."""
    engine, err = _llmops_engine(session, "llmops_models")
    if err:
        return err
    try:
        return engine.llmops_models()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/llmops-presets")
def llmops_presets(session: str = ""):
    """`GET /llmops-presets?session` — `{ok, presets: [{key, label, system, user, wrap, settings}], frames}`;
    `frames` — wrap texts the server puts around the body (`compaction` head/tail + `*_kept`,
    `summarisation` head/tail), shown around the window's result field;
    `user` holds `{{data}}`, `wrap: null` = «as it was». Texts: `context_ctl/_prompts/_llmops.py`."""
    engine, err = _llmops_engine(session, "llmops_presets")
    if err:
        return err
    try:
        frames = engine.llmops_frames() if callable(getattr(engine, "llmops_frames", None)) else {}
        return {"ok": True, "presets": engine.llmops_presets(), "frames": frames}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.post("/llmops-replace")
async def llmops_replace_post(request: Request):
    """`POST /llmops-replace` — LLMOps window's `Replace` (Plan31 Task 1).

    Body: `{session, addrs, text, wrap, preset?}` — `addrs` the window's target m-addresses, `text` the
    BODY from the result field, `wrap` one of `compaction` / `summarisation` / `none` (the window
    resolves «as it was» before sending). The server wraps: with bubbles the compaction frame's
    closing line goes as a separate message after them, which only the compaction plan knows.
    Runs as a compaction run (same lock, same state file): `{"ok": True}` = started, the outcome
    is `GET /context-compact-status`. `preset` — the window's preset key, recorded in the
    ledger's absorption record only (ledger contract amendment 10).
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body is a client error, not a 500
        return {"ok": False, "error": "body must be JSON"}
    if not isinstance(body, dict):
        return {"ok": False, "error": "body must be a JSON object"}
    session = str(body.get("session") or "").strip()
    if not session:
        return {"ok": False, "error": "session is required"}
    addrs = body.get("addrs")
    if not isinstance(addrs, list):
        return {"ok": False, "error": "addrs must be a list of message addresses"}
    addrs = [str(a).strip() for a in addrs if str(a).strip()]
    if not addrs:
        return {"ok": False, "error": "nothing to replace: no message address"}
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        return {"ok": False, "error": "text is empty"}
    wrap = str(body.get("wrap") or "").strip()
    if wrap not in ("compaction", "summarisation", "none", "as-it-was"):
        return {"ok": False, "error": "wrap must be compaction, summarisation, none or as-it-was"}
    preset = str(body.get("preset") or "").strip()[:64]

    engine, reason = _engine_for(session)
    if engine is None:
        return {"ok": False, "session": session, "error": reason}
    if not callable(getattr(engine, "start_ready", None)):
        return {"ok": False, "session": session,
                "error": "the live engine has no start_ready yet — restart the Hermes client"}
    try:
        report = engine.start_ready(addrs, text, wrap, preset)
    except Exception as exc:  # noqa: BLE001 - report, never raise
        return {"ok": False, "session": session, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(report, dict):
        return {"ok": False, "session": session,
                "error": "the engine returned something other than a report"}
    return {**report, "engine": reason}


@router.get("/context-compact-status")
def context_compact_status(session: str = ""):
    """`GET /context-compact-status?session=<id>` — how the background run is doing.

    Reads a FILE, not the live engine — deliberately: the run outlives any single request,
    and a poll must still answer after the agent process reconnects/restarts. No record at
    all is `running: False` (never started), not an error — same honesty convention as
    `/context-focus`/`/context-revision`.
    """
    mod = _load_compact_state()
    if mod is None:
        return {"error": "_compact_state.py is not loadable"}
    out = mod.read_state(session)
    # Для строки хода (`context_ctl/_progress.py`, 2026-09-30): время СЕРВЕРА — `beat` написан его
    # часами, и тишину честно мерить ими же; порог — `compact_silence_minutes` из plugin.yaml.
    import time as _time
    out["now"] = _time.time()
    settings = _load_settings()
    minutes = (settings.compaction().get("compact_silence_minutes") if settings is not None else None) or 5
    out["silence_s"] = int(minutes) * 60
    return out


_SEAMS_MODULE = "hermes_filetools_seams_check"


def _load_seams_check():
    """`seams/check.py` by path, or None — the ACHTUNG core (no plugin-internal imports).
    Loaded FRESH on every call, not cached: the registry and the check are data we want read as
    they are on disk now, and a check runs on a button press, not on a poll."""
    path = _PLUGIN_ROOT / "seams" / "check.py"
    spec = importlib.util.spec_from_file_location(_SEAMS_MODULE, path) if path.exists() else None
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:  # noqa: BLE001 - reported as an error answer, not a 500
        return None
    return mod


@router.get("/seams-check")
def seams_check(tests: int = 0):
    """`GET /seams-check[?tests=1]` — the ACHTUNG check against the Hermes checkout THIS process runs.

    Without `tests`: read-only, counts occurrences in their files by our registry (`seams/`),
    imports and runs nothing of theirs. `tests=1` — FULL check (2026-09-30): also runs every
    `asserted_by` test shipped with the plugin (`tests/`, pytest in a child process of this
    Python, minutes; `tests/conftest.py` points HERMES_HOME at a temp dir, user data untouched).
    A failed test turns its seam FAIL, a passed one turns TEST-ONLY into TEST-OK. Same core as
    `__HQ/ACHTUNG/check_seams.py`. Answer: `ok` (no FAIL, and tests ran when asked), `root`,
    `how`, `head`, `seams`, `baseline`, `rows` [{id, level, verdict, observed, note, breaks,
    test?}], `tally`, `failed`, `tests?` {ran, error, seconds, summary}, `text`, or `error`.
    """
    core = _load_seams_check()
    if core is None:
        return {"ok": False, "error": "seams/check.py is not loadable"}
    try:
        registry = core.load()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"seam registry not readable: {type(exc).__name__}: {exc}"}
    root, how = core.running_root()
    if not root:
        return {"ok": False, "error": how}
    ran = None
    if tests:
        ran = core.run_tests(str(_PLUGIN_ROOT), core.asserted_ids(registry))
    result = core.run(root, registry, ran)
    meta = registry.get("meta") or {}
    # A root inside an environment generation (their venv's editable copy, no .git) takes the
    # commit from the source checkout, and the line says so. The live gateway imports from the
    # checkout itself (2026-09-30).
    head, head_from = core.source_head(root)
    at = f"  @ {head}" + (f" ({head_from})" if head_from else "") if head else ""
    lines = [f"ACHTUNG — {len(registry.get('seams') or [])} seams · baseline "
             f"{meta.get('baseline_head')} taken {meta.get('baseline_taken')}",
             f"root: {root}  [{how}]{at}"] + core.render(result)
    out = {"ok": not result["failed"] and (ran is None or bool(ran.get("ran"))),
           "root": root, "how": how, "head": head, "head_from": head_from,
           "seams": len(registry.get("seams") or []),
           "baseline": {"head": meta.get("baseline_head"), "taken": meta.get("baseline_taken")},
           "rows": result["rows"], "tally": result["tally"], "failed": result["failed"],
           "text": "\n".join(lines)}
    if "tests" in result:
        out["tests"] = result["tests"]
    return out


_SETTINGS_MODULE = "hermes_filetools_ctxctl_settings"


def _load_settings():
    """`context_ctl/_settings.py` by path, or None (stdlib only, reads plugin.yaml next to it)."""
    cached = sys.modules.get(_SETTINGS_MODULE)
    if cached is not None:
        return cached
    path = _PLUGIN_ROOT / "context_ctl" / "_settings.py"
    spec = importlib.util.spec_from_file_location(_SETTINGS_MODULE, path) if path.exists() else None
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_SETTINGS_MODULE] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:  # noqa: BLE001 - default threshold then
        sys.modules.pop(_SETTINGS_MODULE, None)
        return None
    return mod


# ---------------------------------------------------------------------------
# /context-export (Plan16-Task05) — the FIRST route that ADDS something instead of
# taking it away. Everything this panel could do to a context so far subtracted:
# `drop content`, `erase tool`, `erase turn`, compaction. Export only reads the base
# and writes markdown of our own, so the worst it can cost is a wrong file on disk —
# which is exactly why the new rail is being built on it first (`Vision09`).
#
# THE SERVER WRITES, NOT THE CLIENT. The panel sends an intent (`session` + ticked
# addresses); files are laid out by our half, under a path the human owns in the
# config (`context_ctl.compact_export_root`, empty = the plugin's home). Same split
# as everywhere else here — the renderer has no business knowing about the disk.
#
# It reads the base by the same rule as `/messages` (active rows, `ORDER BY id`,
# `api_content` where filled) and indexes DRY (`write=False`): exporting must not
# mint addresses. A message with no address yet simply cannot be asked for — the
# panel only ever shows addressed rows.
# ---------------------------------------------------------------------------

_EXPORT_MODULE = "hermes_filetools_ctxctl_export"


def _load_export():
    """Return `context_ctl/_export.py`, or None. By path, same as select/signal/state."""
    cached = sys.modules.get(_EXPORT_MODULE)
    if cached is not None:
        return cached
    path = _PLUGIN_ROOT / "context_ctl" / "_export.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(_EXPORT_MODULE, path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_EXPORT_MODULE] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(_EXPORT_MODULE, None)
        return None
    return mod


def _export_root() -> str:
    """The configured export path, or "" for "the plugin's home".

    Read from the manifest on every call rather than cached: the Config panel writes
    `plugin.yaml` live, and a human who has just changed the path expects the next
    save to land there — not after a restart.
    """
    try:
        return str((read_config().get("compaction") or {}).get("export_root") or "")
    except Exception:  # noqa: BLE001 - an unreadable manifest means "the default"
        return ""


def _rows_and_addresses(session: str):
    """`(rows, addresses, error)` — the tape and our address per row, parallel lists.

    Same three reads `/messages` does, minus the writing: ledger, base, dry index.
    """
    ledger = _load_ledger()
    msgindex = _load_msgindex()
    if ledger is None or msgindex is None:
        return [], [], "ledger module could not be loaded"
    try:
        from hermes_state import SessionDB
        rows = [dict(r) for r in SessionDB().get_messages(session)]
    except Exception as exc:  # noqa: BLE001 - report, never raise
        return [], [], f"{type(exc).__name__}: {exc}"

    entries = ledger.read_ledger(session) or []
    summary = msgindex.index_session(session, rows, entries, write=False)
    address_of = {}
    for address, row in (summary.get("by_msg") or {}).items():
        address_of[id(row)] = address
    return rows, [address_of.get(id(r), "") for r in rows], ""


@router.post("/context-export")
async def context_export_post(request: Request):
    """`POST /context-export` — save the ticked messages to markdown files.

    Body: `{session, msgs, snapshot?}` — `msgs` is a list of our m-addresses, as-is, same
    subject the compaction rail already speaks in. Answers with what was written and what
    was NOT found: "saved nothing" and "no such message" are different facts, and hiding
    the second leaves a human staring at an empty folder with no reason.

    `snapshot: true` is the SELECTION rail's mode: a dated folder inside the session's,
    each file prefixed with its seat in the tape. One field rather than a second route —
    the reading half (base, addresses, render) is identical and only the layout on disk
    differs; a twin route would duplicate all of it for one `if`.

    Changes NOTHING about the conversation: no `state.db`, no ledger, no model.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body is a client error, not a 500
        return {"ok": False, "error": "body must be JSON"}
    if not isinstance(body, dict):
        return {"ok": False, "error": "body must be a JSON object"}

    session = str(body.get("session") or "").strip()
    if not session:
        return {"ok": False, "error": "session is required"}

    msgs = body.get("msgs")
    if not isinstance(msgs, list):
        return {"ok": False, "error": "msgs must be a list of message addresses"}
    msgs = [str(m).strip() for m in msgs if str(m).strip()]
    if not msgs:
        return {"ok": False, "session": session,
                "error": "nothing to export: no message is ticked"}

    exporter = _load_export()
    if exporter is None:
        return {"ok": False, "session": session, "error": "_export.py is not loadable"}

    rows, addresses, error = _rows_and_addresses(session)
    if error:
        return {"ok": False, "session": session, "error": error}

    # Снимок везёт спутник `ledger.jsonl` (Plan27-Task01): записи ОПЕРАЦИЙ сессии, род
    # различает `_ledger.is_event` — единственное место этого знания.
    snapshot = bool(body.get("snapshot"))
    operations = None
    if snapshot:
        ledger = _load_ledger()
        if ledger is not None:
            operations = [e for e in (ledger.read_ledger(session) or []) if not ledger.is_event(e)]
    report = exporter.export(session, rows, addresses, msgs, root=_export_root(),
                             snapshot=snapshot, operations=operations)
    ok = bool(report.get("written")) and not report.get("errors")
    return {"ok": ok, **report}


# ---------------------------------------------------------------------------
# /config (Plan17) — the Config panel's data door.
#
# TWO LAYERS since 2026-09-30 (`_home.py`): `plugin.yaml` is the DEFAULTS — a
# release (`hermes plugins update`, a git pull) and our deploy overwrite it, so a
# human's edit there would be lost or would block the pull. What the human changes
# in the panel lives in `<HERMES_HOME>/filetools/settings.yaml` — ONLY the
# differences from the defaults. Reading shows the EFFECTIVE config (defaults with
# the user's settings on top); writing applies the patch onto it and saves the
# difference. `plugin.yaml` is never written from here any more.
#
# Loaded fresh on every call, nothing cached across requests: both files can be
# read by more than one process, so an in-memory copy would go stale the moment
# anything else writes them.
# ---------------------------------------------------------------------------

_MANIFEST_PATH = _PLUGIN_ROOT / "plugin.yaml"
_HOME_MODULE = "hermes_filetools_home"


def _load_home():
    """`_home.py` by path, or None: the ONE place for the plugin home and the settings layer.
    Shared by name with the by-path engine modules (`_settings`, `_ledger`, …)."""
    cached = sys.modules.get(_HOME_MODULE)
    if cached is not None:
        return cached
    path = _PLUGIN_ROOT / "_home.py"
    spec = importlib.util.spec_from_file_location(_HOME_MODULE, path) if path.exists() else None
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_HOME_MODULE] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:  # noqa: BLE001 - reported by the route, not a 500
        sys.modules.pop(_HOME_MODULE, None)
        return None
    return mod


_VOCAB_MODULE = "hermes_filetools_dashboard_vocab"
_SCHEMA_LOADER_MODULE = "hermes_filetools_dashboard_schema_loader"


def _load_file_ops(name: str, filename: str):
    """`file_ops/<filename>` loaded by path under `name`, or None. Same lazy, fail-closed
    shape as the other loaders here."""
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    path = _PLUGIN_ROOT / "file_ops" / filename
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:  # noqa: BLE001 - the route degrades, never 500s
        sys.modules.pop(name, None)
        return None
    return mod


def _tool_dictionary(slot: str, cfg: dict) -> dict:
    """The tool dictionary for one slot (`deferred/08`, panel amendment 19): `icon` (a
    setting, `tools.<slot>.icon`), `header` (`role -> word` of its answer's header lines) and
    `call` (`params` from the slot's schema file, `body` — arguments shown as text).

    Every part degrades on its own to empty: a panel with no dictionary falls back to its
    first-frame defaults, which is a cosmetic loss, while a 500 on `/config` is not.
    """
    vocab = _load_file_ops(_VOCAB_MODULE, "_vocab.py")
    header, body, params = {}, [], []
    if vocab is not None:
        try:
            header = dict(vocab.header(slot))
            body = list(vocab.CALL_BODY.get(slot, []))
            # An explicit `schema:` in the slot wins, as it does at registration (`__init__.py`).
            schema_name = (str(cfg.get("schema") or "")
                           or vocab.schema_file(slot, str(cfg.get("path") or "new")))
            loader = _load_file_ops(_SCHEMA_LOADER_MODULE, "_schema_loader.py") if schema_name else None
            if loader is not None:
                params = vocab.call_params(loader.load_schema(schema_name))
        except Exception:  # noqa: BLE001
            pass
    return {
        "icon": str(cfg.get("icon") or ""),
        "header": header,
        "call": {"params": params, "body": body},
    }


def _param_type(value, has_options: bool) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "number"
    return "enum" if has_options else "text"


# Код-уровневый запас на случай манифеста без секции — ТА ЖЕ четвёрка, что в
# `file_ops/_new_format.py::DEFAULT_SYMBOLS`. Копия осознанная: этот модуль грузится Хермесом
# ПО ПУТИ, вне пакета, и относительного импорта у него нет. Расхождение копий ловит тест.
_FORMAT_DEFAULTS = {"block_symbol": "■", "anchor_symbol": "⚓",
                    "separator_symbol": "○", "comment_fallback": "◆"}


def _formatting(data) -> dict:
    """Словарь формата для панели: СИМВОЛЫ и собранные из них ЭЛЕМЕНТЫ.

    Почему не только символы (заявка владельца 2026-09-23: «не просто три символа, а записать
    тип объекта — хедер_сепаратор»): разделитель шапки это не `separator_symbol`, а он же
    ТРИЖДЫ, а открывающий маркер блока — символ ПЛЮС слово `BLOCK`. Отдай мы одни атомы,
    панели пришлось бы знать правила сборки, то есть держать вторую копию формата — ровно то,
    от чего мы уходим. Поэтому в манифесте человек правит АТОМЫ, а потребитель получает
    готовые ЭЛЕМЕНТЫ, собранные здесь, в одном месте.
    """
    src = (data or {}).get("formatting") or {}
    sym = {}
    for key, fallback in _FORMAT_DEFAULTS.items():
        value = src.get(key)
        sym[key] = str(value) if value else fallback
    # Элементы. Имена — по РОЛИ в тексте, а не по символу: роль переживает смену символа.
    # Правило сборки — ОДНО, `_vocab.format_elements` (Plan28): его же зовут тулы и загрузчик
    # схем. Запасная копия здесь — на случай, если модуль не загрузился: `/config` не падает.
    vocab = _load_file_ops(_VOCAB_MODULE, "_vocab.py")
    if vocab is not None:
        try:
            return vocab.format_elements(sym)
        except Exception:  # noqa: BLE001
            pass
    return {
        **sym,
        "header_separator": sym["separator_symbol"] * 3,
        "block_open": sym["block_symbol"] + "BLOCK",
        "block_close": sym["block_symbol"] + "END",
    }


def _compact_int(value, default: int) -> int:
    """A missing/unparseable compaction knob falls back to its own documented default,
    never to 0 — a silent 0 would mean "keep nothing" / "cut nothing", the opposite of
    a sane default."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _hermes_verified() -> dict:
    """`{"commit", "date"}` of the Hermes this plugin version was last checked against — the seam
    registry's `meta`. Empty strings when the registry is missing or unreadable (shown as a dash)."""
    core = _load_seams_check()
    try:
        meta = (core.load() if core is not None else {}).get("meta") or {}
    except Exception:  # noqa: BLE001 - a broken registry must not break the Config panel
        meta = {}
    return {"commit": str(meta.get("baseline_head") or ""), "date": str(meta.get("baseline_taken") or "")}


def read_config() -> dict:
    """The Config panel's read side: the EFFECTIVE settings (defaults + the user's
    `settings.yaml`) plus the `help` / `params_help` / `disabled_tools_help` text the
    panel's info-popups show (Plan17 — these used to be a hardcoded `INFO_PLACEHOLDER`).

    A missing file, or a key missing inside it, is not an error — same convention as
    `_load_manifest_disabled_tools` and its siblings in `__init__.py`: an empty/default
    projection, never a 500.
    """
    home = _load_home()
    if home is None:
        return {"error": "_home.py is not loadable"}
    try:
        data = home.effective(_MANIFEST_PATH)
    except Exception as exc:  # noqa: BLE001 - report, never raise
        return {"error": f"{type(exc).__name__}: {exc}"}

    tools = {}
    for slot, cfg in (data.get("tools") or {}).items():
        if not isinstance(cfg, dict):
            continue
        params_raw = cfg.get("params") or {}
        help_raw = cfg.get("params_help") or {}
        options_raw = cfg.get("params_options") or {}
        params = []
        for key, value in params_raw.items():
            has_options = key in options_raw
            entry = {
                "key": str(key),
                "type": _param_type(value, has_options),
                "value": value,
                "help": str(help_raw.get(key) or ""),
            }
            if has_options:
                entry["options"] = [str(o) for o in (options_raw.get(key) or [])]
            params.append(entry)
        tools[slot] = {
            "enabled": bool(cfg.get("enabled", True)),
            "path": str(cfg.get("path") or "new"),
            "help": str(cfg.get("help") or ""),
            "params": params,
            # Справочник тула (`deferred/08`): иконка, слова шапки, параметры вызова.
            **_tool_dictionary(str(slot), cfg),
        }

    disabled_help = data.get("disabled_tools_help") or {}
    ctx = data.get("context_ctl") or {}
    try:
        data_root, settings_file = str(home.plugin_home()), str(home.settings_path())
    except Exception:  # noqa: BLE001 - no Hermes home known: show nothing rather than a guess
        data_root, settings_file = "", ""
    return {
        # ВЕРСИЯ ПЛАГИНА — чтобы её было видно НА ЭКРАНЕ, а не выясняли по логу загрузки
        # (владелец, 2026-09-23). Пустая строка означает «манифест прочитан, а поля в нём нет»;
        # панель покажет прочерк, а не выдуманное число.
        "version": str(data.get("version") or ""),
        # НА КАКОМ HERMES ЭТА ВЕРСИЯ ПРОВЕРЕНА (владелец, 2026-10-02): многое у нас держится за
        # непубличный код Hermes, и человек должен видеть, с каким их коммитом всё сходилось.
        # Источник — реестр швов, который едет с плагином (`meta.baseline_head` /
        # `baseline_taken`); его же подставляет в README сборка релиза.
        "hermes_verified": _hermes_verified(),
        # DEV-РЕЖИМ (владелец, 2026-10-02): вкладки разработки (Operations, Palette) панель
        # показывает только при `dev: true` в settings.yaml пользователя. Строго `True`: строка
        # «true» из руками правленого файла YAML и так разберёт как bool.
        "dev": data.get("dev") is True,
        # Дом плагина и файл настроек пользователя (2026-09-30) — панель показывает их как есть.
        "data_root": data_root,
        "settings_file": settings_file,
        "disabled_tools": [str(n) for n in (data.get("disabled_tools") or [])],
        "disabled_tools_zone_help": str(data.get("disabled_tools_zone_help") or ""),
        "disabled_tools_help": {str(k): str(v) for k, v in disabled_help.items()},
        "tools": tools,
        # СЛОВАРЬ НАШЕГО ФОРМАТА — чтобы панель перестала зашивать символы у себя
        # (владелец, 2026-09-23).
        "formatting": _formatting(data),
        "context_ctl_enabled": bool(ctx.get("enabled", True)),
        # Knobs for the Compaction zone (Plan16-Task03) — what the button pre-ticks the rail
        # with. Defaults here MUST match the engine's (`context_ctl/_settings.py::DEFAULTS`).
        "compaction": {
            "head_keep": _compact_int(ctx.get("compact_head_keep"), 6),
            "auto_percent": _compact_int(ctx.get("compact_auto_percent"), 70),
            "cut_percent": _compact_int(ctx.get("compact_cut_percent"), 30),
            "tail_keep": _compact_int(ctx.get("compact_tail_keep"), 20),
            "auto_enabled": str(ctx.get("compact_auto_enabled", False)).strip().lower() == "true",
            "pause_minutes": _compact_int(ctx.get("compact_auto_pause_minutes"), 3),
            "silence_minutes": _compact_int(ctx.get("compact_silence_minutes"), 5),
            "wait_turn_minutes": _compact_int(ctx.get("compact_wait_turn_minutes"), 10),
            # Куда панель сохраняет сообщения и сессии в `.md` (`Vision09`). Строка, и ПУСТАЯ
            # строка законна: она значит «дом плагина по умолчанию», а не «не задано».
            "export_root": str(ctx.get("compact_export_root") or ""),
        },
        # "Temporary parameters" (owner, 2026-09-22) — empty since 2026-09-29 (Plan24).
        "temp_params": {},
    }


def _apply_tools(data, tools_patch) -> None:
    """Assign into each slot's EXISTING mapping — a slot the defaults do not declare is
    ignored, and a param keeps the defaults' own type."""
    tools = data.get("tools")
    if not isinstance(tools, dict):
        return
    for slot, slot_patch in (tools_patch or {}).items():
        node = tools.get(slot)
        if not isinstance(node, dict) or not isinstance(slot_patch, dict):
            continue
        if "enabled" in slot_patch:
            node["enabled"] = bool(slot_patch["enabled"])
        if slot_patch.get("path") in ("old", "new"):
            node["path"] = slot_patch["path"]
        params_node = node.get("params")
        if not isinstance(params_node, dict):
            continue
        for p in (slot_patch.get("params") or []):
            key = p.get("key") if isinstance(p, dict) else None
            if key not in params_node or "value" not in p:
                continue
            old, new = params_node[key], p["value"]
            # Keep the file's own type for this key rather than trusting whatever the
            # client sent — a bool written as the string "true" would come back as a
            # YAML 1.1 landmine (Plan17's ловушка 3), and a stray string into a number
            # field would corrupt a param the tool core parses as int.
            if isinstance(old, bool):
                params_node[key] = bool(new)
            elif isinstance(old, int):
                try:
                    params_node[key] = int(new)
                except (TypeError, ValueError):
                    continue
            else:
                params_node[key] = str(new)


_COMPACT_KEYS = {
    "head_keep": "compact_head_keep", "auto_percent": "compact_auto_percent",
    "cut_percent": "compact_cut_percent", "tail_keep": "compact_tail_keep",
    "pause_minutes": "compact_auto_pause_minutes",
    "silence_minutes": "compact_silence_minutes",
    "wait_turn_minutes": "compact_wait_turn_minutes",
}


def write_config(patch: dict) -> dict:
    """Apply `patch` (same shape `read_config()` returns) onto the EFFECTIVE config and save
    what now differs from the defaults into the user's `settings.yaml`. `plugin.yaml` is not
    touched. Unknown/unwritable keys in `patch` are ignored rather than trusted blindly.
    """
    import copy
    home = _load_home()
    if home is None:
        return {"error": "_home.py is not loadable"}
    try:
        base = home.load_yaml(_MANIFEST_PATH)
        # deepcopy: `merged` shares untouched sub-dicts with `base`, and the patch below must
        # not leak into the defaults it is then compared against.
        data = copy.deepcopy(home.merged(base, home.user_settings()))
    except Exception as exc:  # noqa: BLE001 - report, never raise
        return {"error": f"{type(exc).__name__}: {exc}"}

    if isinstance(patch.get("disabled_tools"), list):
        data["disabled_tools"] = [str(n).strip() for n in patch["disabled_tools"] if str(n).strip()]
    if isinstance(patch.get("tools"), dict):
        _apply_tools(data, patch["tools"])
    ctx = data.get("context_ctl")
    if isinstance(ctx, dict):
        if "context_ctl_enabled" in patch:
            ctx["enabled"] = bool(patch["context_ctl_enabled"])
        comp = patch.get("compaction")
        if isinstance(comp, dict):
            for panel_key, yaml_key in _COMPACT_KEYS.items():
                if panel_key not in comp:
                    continue
                try:
                    ctx[yaml_key] = int(comp[panel_key])
                except (TypeError, ValueError):
                    continue
            # Выключатель автомата — булево, не число.
            if "auto_enabled" in comp:
                ctx["compact_auto_enabled"] = bool(comp["auto_enabled"])
            # Путь — любая строка законна, включая пустую («дом плагина»).
            if "export_root" in comp:
                ctx["compact_export_root"] = str(comp["export_root"] or "")

    try:
        home.save_settings(home.diff(data, base))
    except Exception as exc:  # noqa: BLE001 - report, never raise
        return {"error": f"{type(exc).__name__}: {exc}"}
    return read_config()


@router.get("/config")
def config_get():
    """The Config panel's read side — this plugin's EFFECTIVE config (`plugin.yaml` defaults +
    the user's `settings.yaml`), not Hermes' `config.yaml` (that one stays read-only reference, shown by the
    panel from a different source — not this route)."""
    return read_config()


@router.post("/config")
async def config_post(request: Request):
    """Apply an edit from the Config panel. See `write_config()` for exactly
    which keys are writable and why nothing else in the body is trusted."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body is a client error, not a 500
        return {"error": "body must be JSON"}
    if not isinstance(body, dict):
        return {"error": "body must be a JSON object"}
    return write_config(body)


def reload_plugin() -> dict:
    """Same mechanism as the `/filetools-reload` slash command
    (`__init__.py::_handle_reload`) — forces `discover_plugins(force=True)`
    in THIS process, so that a config edit (which register() only reads at
    plugin load) takes effect without a full Hermes restart.

    Works from here for the reason `_handle_reload`'s own docstring spells
    out: a slash-command handler and this dashboard route both run inside the
    ONE long-lived Hermes process that owns the process-wide `PluginManager`
    singleton — `_engine_for` above reaches a live agent's engine from this
    same process for the identical reason. An external OS process
    (`execute_code`'s sandbox, a terminal command) could not do this; this
    route can, because it is not one.
    """
    try:
        from hermes_cli.plugins import discover_plugins
        discover_plugins(force=True)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001 - report, never raise
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.post("/config-reload")
def config_reload_post(request: Request):
    """The Config panel's Reload button — same effect as typing
    `/filetools-reload` in chat, triggered from the UI instead. Takes no
    body, but `request: Request` stays annotated anyway — every `*_post`
    handler in this module is swept by
    `test_post_routes_annotate_their_request_parameter`, on purpose, so a
    future route that DOES need a body cannot forget the annotation
    (`request` param unused here is fine and expected).
    """
    return reload_plugin()
