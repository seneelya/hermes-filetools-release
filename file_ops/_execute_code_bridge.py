"""Patches tools.code_execution_tool's execute_code sandbox bridge so it
tells the model the TRUTH about our "new"-path tools (read_file/write_file/
search_files) instead of core's own stale/JSON-only assumptions.

Background: __HQ/recon/Recon10__execute_code_json_only_bridge.md. Two
independent problems in core, both patched here:

1. `_TOOL_STUBS`/`_TOOL_DOC_LINES` (code_execution_tool.py) are core's own
   hardcoded tables describing each sandboxed tool's Python signature - both
   what actually gets sent over RPC AND what the model reads in
   execute_code's own system-prompt description (`build_execute_code_schema`,
   called fresh per session by model_tools.py - not a frozen import-time
   snapshot, so patching before that runs is enough). Neither knows about
   our plugin's schema at all, so a model calling e.g. search_files() via
   execute_code sees (and can rely on) a stale, wrong signature.
2. The generated sandbox stub's `_call()` does `json.loads(raw)`
   unconditionally - our "new" paths return plain text, not JSON, by
   design (CONTRACT.md §1) - crashes on the very first call, before the
   model's own script code even runs.

Since Plan06 the marked sandbox path answers with `json.dumps(...)` of three
keys, so (2) is no longer the everyday case - but the patch stays, because the
dict assembly is fail-open and falls back to plain text, which would then hit
that same unguarded `json.loads`.

Encoding the response also closes a latent framing bug on this transport that
plain text was exposed to: the protocol is newline-delimited (`_call` reads
until `buf.endswith(b"\\n")`, code_execution_tool.py:564) while a plain-text
answer is full of real newlines, so a recv boundary landing just after an
internal one ends the read early - a truncated answer and a desynced socket.
`json.dumps` escapes every newline, so the payload is a single line by
construction. Incidental to Plan06's purpose, but it is why the marked path is
the safer one on this transport, not merely the more convenient one.

Anchor point: core's OWN regression test for exactly this class of drift
(tests/tools/test_code_execution.py::TestStubSchemaDrift) reads
`registry._tools[tool_name].schema` as ground truth for what a stub SHOULD
cover - so do we: instead of hand-writing a second copy of our schema here
(which would just go stale again the next time we touch the yaml), the stub
signature/doc line are BUILT FROM the schema we already registered via
ctx.register_tool - exactly one source of truth, no drift possible by
construction.

Best-effort, defensive throughout: if core's internals don't match what we
expect (renamed module/attribute, restructured template - a future Hermes
version), every step is wrapped so a mismatch just skips that one patch
instead of breaking plugin registration.

`schema` here is whatever `_schema_loader.py::load_schema()` returns -
`{"name", "description", "parameters": {"type", "properties", "required"}}`
(OpenAI function-calling shape, same shape `registry._tools[name].schema`
itself holds, and what core's own TestStubSchemaDrift reads via
`entry.schema.get("parameters", {}).get("properties", {})`) - NOT the
flatter top-level `properties`/`required` the raw YAML file has before that
loader wraps it.
"""

import logging

logger = logging.getLogger(__name__)

_PY_TYPE = {"string": "str", "integer": "int", "boolean": "bool"}

# Kept in sync with __init__.py's _FACADE_MARKER by the test that asserts they
# are equal - two files have to agree on this string and only one of them can
# own it (this module cannot import the plugin root, which imports this
# module). The stub sends it as a literal in its args expression, so the model
# never sees it: it reads the SIGNATURE, and the marker is not a parameter.
_FACADE_MARKER = "_filetools_dict"

# Same three keys for every tool (Plan06). Written once, here; it reaches the
# system prompt once per tool because core's _TOOL_DOC_LINES is per-tool by
# construction and its shared preamble is core's text, not ours.
# Where a FAILURE lands is stated explicitly, because the shape makes the wrong
# guess natural: on an error the whole message is in `header` and `body` is
# empty, so a caller that prints `body` sees nothing and reads it as a silent
# no-op. Observed live 2026-09-03 - an agent called `patch --exact` with no
# match, looked at `body`, and reported "silent refusal, exit 0" for an answer
# that was in fact exit_code 1 with the reason spelled out in `header`.
_RETURNS_DICT = ('Returns {"exit_code": int, "header": str, "body": str} - '
                 'the tool\'s technical header as text, its output as text. '
                 'On failure exit_code is non-zero, the reason is in header, '
                 'and body is empty - check exit_code, not body.')

# `patch` only. Its `line` is an orientation hint resolved against
# `old_string`, which makes independent patches immune to each other's line
# shifts - but NOT to this: text inserted by an earlier patch can contain the
# very anchor a later patch targets, becoming a SECOND candidate that is
# closer to the later patch's `line`. Uniqueness is not formally violated, so
# nothing errors - the wrong site is patched silently. In a single tool call
# the model would see the echo and notice; in a chain it never reads the
# intermediate answers. Warning, not a prohibition: chaining independent
# patches is legitimate and useful.
_CHAIN_CAUTION = {
    "patch": (" In a chain use very carefully - a patch's own inserted text "
              "can become a closer match for a later patch's anchor. Prefer "
              "one call per patch."),
}


def _blocked_params(cet, tool_name: str) -> frozenset:
    """Params this tool's schema declares but the sandbox bridge refuses to
    forward, so the generated stub must not advertise them.

    Read from CORE's own constant, never a copy of ours: `_rpc_server_loop`
    pops exactly these from `args` before dispatch
    (`code_execution_tool.py:739`), so a stub built from our full schema would
    hand the model four parameters that silently do nothing. Only `terminal`
    has any today; the lookup is by tool name so a future blocked set for
    another tool needs no change here.
    """
    if tool_name != "terminal":
        return frozenset()
    blocked = getattr(cet, "_TERMINAL_BLOCKED_PARAMS", None)
    if not isinstance(blocked, (set, frozenset, tuple, list)):
        # Moved to its own module at some upstream revision (was
        # code_execution_tool.py, now code_execution_rpc.py) - same
        # RPC-arg-stripping constant, new home. Try there before giving up:
        # a module move isn't a rename/removal, and code_execution_rpc is
        # exactly where `_rpc_server_loop` (the thing that pops these params)
        # lives now, so the constant travelling with it is the expected case,
        # not a drift.
        try:
            from tools.code_execution_rpc import _TERMINAL_BLOCKED_PARAMS as blocked
        except ImportError:
            blocked = None
    if not isinstance(blocked, (set, frozenset, tuple, list)):
        # Renamed or gone entirely: advertise everything rather than guess a
        # set. An over-wide signature is a lesser wrong than one silently
        # missing params the bridge would actually have accepted.
        logger.warning(
            "execute_code bridge: _TERMINAL_BLOCKED_PARAMS not found, terminal's "
            "stub will advertise every schema param (see Doc__execute_code_bridge.md)"
        )
        return frozenset()
    return frozenset(blocked)


def _build_stub(schema: dict, exclude: frozenset = frozenset()):
    """(sig, args_expr) - two of the three _TOOL_STUBS fields (doc is built
    by the caller) - derived from our own registered schema.

    `exclude` drops params the RPC server strips anyway (see
    `_blocked_params`). They leave BOTH the signature and args_expr: keeping
    them in args_expr would send fields the server pops, which is just noise
    on the wire.

    Required params (no default) MUST come before optional ones in a Python
    signature, and a schema need not declare them in that order - sorting
    required-first here, rather than copying declaration order, is what keeps
    the generated `def tool(...)` syntactically valid at all. Found via a
    direct unit-test run 2026-08-26 before this ever reached a live session:
    a naive declaration-order copy produced a SyntaxError ("non-default
    argument follows default argument").

    Since Plan08 (2026-09-02) the four file tools have no optional properties
    left, so their stubs come out ALL-required - `read_file(path: str,
    flags: str)`, with the paging parameters living inside the flag string.
    The sort still matters: `terminal`'s schema is upstream's shape and keeps
    its optional params."""
    parameters = schema.get("parameters", {})
    props = parameters.get("properties", {})
    required_names = set(parameters.get("required", []))

    required_parts = []
    optional_parts = []
    args_parts = []
    for name, spec in props.items():
        if name in exclude:
            continue
        pytype = _PY_TYPE.get(spec.get("type", "string"), "str")
        if name in required_names:
            required_parts.append(f"{name}: {pytype}")
        else:
            default = spec.get("default", None)
            optional_parts.append(f"{name}: {pytype} = {default!r}")
        args_parts.append(f'"{name}": {name}')

    sig = ", ".join(required_parts + optional_parts)
    # The marker rides along as a LITERAL, not a parameter: it is what tells
    # our handler this call came from a Python script rather than the model,
    # which is the only place the two are distinguishable (the RPC server
    # dispatches both through the same handle_function_call). It survives the
    # trip because _rpc_server_loop forwards args verbatim and
    # coerce_tool_args skips keys absent from the schema.
    args_parts.append(f'"{_FACADE_MARKER}": True')
    args_expr = "{" + ", ".join(args_parts) + "}"
    return sig, args_expr


def _summary(schema: dict) -> str:
    return (schema.get("description") or "").strip().split(". ")[0].strip().rstrip(".")


# Shared tail text patched in step (3) below - also the "seam" a Hermes
# update could move without renaming anything, so keep it as one named
# constant both the patch and any drift-check can compare against.
_JSON_LOADS_OLD_TAIL = (
    "    result = json.loads(raw)\n"
    "    if isinstance(result, str):\n"
)
_JSON_LOADS_NEW_TAIL = (
    "    try:\n"
    "        result = json.loads(raw)\n"
    "    except json.JSONDecodeError:\n"
    "        return raw\n"
    "    if isinstance(result, str):\n"
)


def patch_execute_code_bridge(new_path_schemas: dict) -> dict:
    """new_path_schemas: {tool_name: schema_dict} for whichever of
    read_file/write_file/search_files are ACTUALLY on `path: new` this
    registration (register() in __init__.py only passes those) - a slot
    left on `path: old` keeps core's original stub untouched, since its
    response really is the JSON dict core promises there.

    Returns a {check_name: bool} status report - True per patch point that
    actually applied. NOT just a side-effecting void function on purpose:
    every entry landing True is exactly the "did this Hermes version's
    internals still match what we assumed" seam-validation signal
    (CONTRACT.md §16) - a Hermes update that renames/restructures any of
    these silently degrades to a no-op patch (by design - never breaks
    plugin registration) UNLESS something actually asserts on this return
    value, which is what test_e2b/test_e2c in
    tests/test_plugin_integration.py do."""
    status = {}
    if not new_path_schemas:
        return status
    try:
        import tools.code_execution_tool as cet
    except Exception as exc:
        logger.warning("execute_code bridge patch skipped (core module not importable): %s", exc)
        status["module_importable"] = False
        return status
    status["module_importable"] = True

    for tool_name, schema in new_path_schemas.items():
        try:
            excluded = _blocked_params(cet, tool_name)
            sig, args_expr = _build_stub(schema, exclude=excluded)
            summary = _summary(schema)
            # Core's own terminal doc line said "Foreground only (no
            # background/pty)". Dropping the excluded params without saying so
            # would lose that warning, so it is restated from the same set
            # that produced the exclusion - no second list to keep in sync.
            unavailable = (
                f" Not available here: {', '.join(sorted(excluded))} "
                f"(the sandbox bridge strips them)."
                if excluded else ""
            )
            caution = _CHAIN_CAUTION.get(tool_name, "")
            doc = f'"""{summary}. {_RETURNS_DICT}{unavailable}{caution}"""'
            if hasattr(cet, "_TOOL_STUBS") and isinstance(cet._TOOL_STUBS, dict):
                # Value shape isn't ours to assume - core dropped the
                # redundant leading name from the tuple at some upstream
                # revision (was `(name, sig, doc, args_expr)`, is now
                # `(sig, doc, args_expr)` with the name only as the dict
                # key). Detect from a core-owned entry we never touch
                # (web_search is never one of our tools) instead of
                # hardcoding either shape - same trick as
                # `_upstream_name` for the process/process_manage rename:
                # infer from what's actually there.
                sample = cet._TOOL_STUBS.get("web_search") or next(
                    iter(cet._TOOL_STUBS.values()), None)
                if sample is not None and len(sample) == 3:
                    cet._TOOL_STUBS[tool_name] = (sig, doc, args_expr)
                else:
                    cet._TOOL_STUBS[tool_name] = (tool_name, sig, doc, args_expr)
                status[f"stub:{tool_name}"] = True
            else:
                status[f"stub:{tool_name}"] = False

            if hasattr(cet, "_TOOL_DOC_LINES") and isinstance(cet._TOOL_DOC_LINES, list):
                doc_line = (
                    f"  {tool_name}({sig}) -> dict\n"
                    f"    {summary}. {_RETURNS_DICT}{unavailable}{caution}"
                )
                cet._TOOL_DOC_LINES = [
                    (n, doc_line) if n == tool_name else (n, d)
                    for n, d in cet._TOOL_DOC_LINES
                ]
                status[f"doc_line:{tool_name}"] = True
            else:
                status[f"doc_line:{tool_name}"] = False
        except Exception as exc:
            logger.warning("execute_code bridge: skipped patching stub for %s (%s)", tool_name, exc)
            status[f"stub:{tool_name}"] = False
            status[f"doc_line:{tool_name}"] = False

    # (3) _call()'s unconditional json.loads() - identical tail in both
    # transport header templates (verified via gcb 2026-08-26, Recon10).
    # Only makes the RESPONSE parse gracefully fall back to the raw string
    # on non-JSON input - doesn't touch the REQUEST side at all.
    for header_name in ("_UDS_TRANSPORT_HEADER", "_FILE_TRANSPORT_HEADER"):
        status_key = f"json_fallback:{header_name}"
        try:
            current = getattr(cet, header_name, None)
            if isinstance(current, str) and _JSON_LOADS_NEW_TAIL in current:
                # Idempotency: register() can run more than once in the same
                # process (the plugin's own `filetools-reload` slash command
                # forces a full re-discovery) - already-patched is success,
                # not "shape changed", and must NOT be re-replaced (the old
                # tail is gone by definition, a naive re-check would
                # falsely report drift on the SECOND call every time).
                status[status_key] = True
            elif isinstance(current, str) and _JSON_LOADS_OLD_TAIL in current:
                setattr(cet, header_name, current.replace(_JSON_LOADS_OLD_TAIL, _JSON_LOADS_NEW_TAIL, 1))
                status[status_key] = True
            else:
                logger.warning(
                    "execute_code bridge: %s doesn't match the expected shape, skipped "
                    "(Hermes internals may have changed - see CONTRACT.md §16)",
                    header_name,
                )
                status[status_key] = False
        except Exception as exc:
            logger.warning("execute_code bridge: skipped patching %s (%s)", header_name, exc)
            status[status_key] = False

    return status
