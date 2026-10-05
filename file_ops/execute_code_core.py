#!/usr/bin/env python3
"""execute_code — override of Hermes' own built-in `execute_code` tool.

Design: `__HQ/plans/Plan04__wrap-exec-and-process.md` task 4. Same discipline
as `terminal_core.py` (Plan03): we do NOT reimplement anything. The sandbox,
the RPC loop, the approval guard, the local/remote dispatch, the misdirected-
argument recovery — all of it stays in hermes-agent's own
`tools.code_execution_tool._execute_code_handler()`, imported and called live.
This module's only job is turning that function's JSON return string into the
plain text the model already gets from read_file/write_file/patch/search_files.

Their handler, not their `execute_code()`: the handler is the layer that
recovers a model sending terminal's `command` instead of `code`, or a
non-string `code`. Calling `execute_code()` directly would silently drop those
recoveries, and re-implementing them here would be a copy to keep in sync.

No console `main()`, for the same reason as terminal_core.py: this tool only
means anything inside a live Hermes process.

TWO FACTS ESTABLISHED IN THEIR SOURCE, both of which shaped this file:

1. **The model-facing description is NOT ours.** `model_tools.py:522-529`
   replaces the `execute_code` schema in the tool list handed to the model with
   `build_execute_code_schema(...)`, keyed on nothing but the NAME being
   present — it does not care who registered it. So a YAML copy of the schema
   (the pattern used for our four file tools and `terminal`) would be dead
   weight that drifts: our wording could never reach the model. We therefore
   register with THEIR live-built schema and let the output speak for itself,
   which it can — labeled lines above a ○○○ separator are self-evident in a
   way a JSON blob is not. Their rebuild also keeps something a static copy
   would lose: the description lists only the sandbox tools actually enabled
   this session, and the working-directory sentence follows the configured
   execution mode.
2. **stdout is already capped upstream, honestly.** `MAX_STDOUT_BYTES = 50_000`
   with a 40/60 head-tail split, plus `stdout_truncated` /
   `stdout_bytes_captured` / `stdout_bytes_total` / `stdout_bytes_omitted` /
   `warning` in the payload (`_truncate_stdout_text`, ~line 122). Those become
   header lines here for free. The `max_result_size_chars=100_000` we lose by
   overriding was a backstop ABOVE that cap, i.e. above a body that cannot
   reach 100k in the first place — so the regression Plan04's risk 1 feared is
   much smaller than it looked. We still apply `get_max_bytes()` (default
   50_000, one `tool_output.max_bytes` key in config.yaml) so an operator who
   sets it LOWER than 50k actually gets that, and so the number stays a config
   change rather than a code change.
"""

try:
    from ._tool_text import render_json_result
    from . import _ledger
    from . import _vocab
except ImportError:  # console/dev mode - file_ops/ is on sys.path, not a package
    from _tool_text import render_json_result  # type: ignore
    import _ledger  # type: ignore
    import _vocab  # type: ignore

# Friendly labels for the keys their result dicts carry (collected from the
# `json.dumps(...)` return sites in tools/code_execution_tool.py: the local
# path ~1700, the remote path ~1240, the approval-guard refusal ~1336, the
# two exception paths ~1195/~1752, and the stdout metadata from
# _assemble_stdout_result ~107). Any key NOT here is still rendered, with its
# label title-cased from its own snake_case name - a future upstream field must
# never silently disappear.
# The table lives in `_vocab.py` since 2026-10-05 (`deferred/08`): the panel reads the same
# words over `/config`, and a word printed here but unknown there stops being coloured.
_LABELS = _vocab.LABELS["execute_code"]

# What the model needs first when a script fails: did it run, what came back,
# what went wrong. Everything else keeps upstream's own order.
_HEAD_KEYS = ("status", "exit_code", "error")

# No `(raw: ...)` tail here, unlike terminal. That fragment exists because
# agent/context_compressor.py regexes an exit code out of any `tool_name ==
# "terminal"` result; its execute_code branch (context_compressor.py:1943)
# summarises from the ARGS and a line count instead and never looks at our
# text, so there is nothing to keep alive.


def _max_body_chars() -> int:
    """The operator's own `tool_output.max_bytes` (config.yaml), or their
    default. Read live, not at import: their reader caches it itself, and
    reading at import would freeze whatever the config said when the plugin
    loaded. Fail-open to 0 (= no cap of ours) rather than to an invented
    number: upstream's own 50k stdout cap is still in force either way, so
    "we could not read the config" must not mean "we cut it somewhere else".
    """
    try:
        from tools.tool_output_limits import get_max_bytes
        return int(get_max_bytes())
    except Exception:
        return 0


def render_execute_code_result(raw: str, symbols: dict = None,
                               max_body_chars: int = None, op: str = "") -> str:
    """JSON string (their contract) -> plain text (ours): header lines, the
    ○○○ separator, then the script's own stdout verbatim.

    Everything mechanical (fail-open on non-JSON, never dropping an unknown
    key, no separator when there is no body, truncate-and-say-so) belongs to
    `render_json_result`; this is the execute_code-shaped configuration of it.
    """
    return render_json_result(
        raw,
        labels=_LABELS,
        body_keys=("output",),
        head_keys=_HEAD_KEYS,
        symbols=symbols,
        body_max_chars=_max_body_chars() if max_body_chars is None else max_body_chars,
        op=op,
    )


def execute_code_tool_new(args: dict, symbols: dict = None, **kwargs) -> str:
    """Call their live handler with the model's arguments untouched, render its
    answer. `args` is passed as the dict they expect precisely so their
    misdirected-argument recovery keeps working (see the module docstring).
    """
    from tools.code_execution_tool import _execute_code_handler

    # Address FIRST, before the script runs: a script that takes a minute must
    # not renumber itself behind a call that started later (Plan09).
    op = _ledger.next_op(session=kwargs.get("session_id"),
                         task_id=kwargs.get("task_id") or "default")

    # The model's script runs INSIDE this handler, and whatever it calls through
    # our facade lands in the ledger as an operation of its own. Mark the window
    # so those entries can carry `nested: true` (Plan11-Task02).
    #
    # It cannot be recovered afterwards: every nested call inherits the OUTER
    # tool_call_id (`_ledger.py::_observability_context`), which is exactly why
    # nested reads show up as `unknown` in `/reads` today. The flag has to be
    # set while the script runs, or never.
    #
    # try/finally rather than a context manager because the reset MUST happen
    # even if their handler raises: a leaked flag would mislabel every later
    # operation in this context as nested.
    #
    # The nested entries were held while the script ran (`deferred/17`): only now,
    # with the answer the model receives in hand, can a nested read say which of
    # its lines reached the model (`seen`). Flushed even if anything below raises —
    # with an empty answer, i.e. nothing proven seen.
    pending, out = [], ""
    token = _ledger.enter_sandbox()
    try:
        try:
            raw = _execute_code_handler(args, **kwargs)
        finally:
            pending = _ledger.exit_sandbox(token)
        out = render_execute_code_result(raw, symbols=symbols, op=op)
    finally:
        _ledger.flush_sandbox(pending, out)
    # Read out of `args` rather than taken as a named parameter, because this
    # file deliberately does NOT validate their argument shape: their own
    # misdirected-argument recovery accepts `command` where `code` was meant
    # (see the module docstring), and we must not disagree with it about what
    # was actually called.
    _note(out, op, kwargs,
          command=str(args.get("code") or args.get("command") or ""))
    return out


def _note(out: str, op: str, kwargs: dict, command: str = "") -> None:
    """Record the operation, and under no circumstances affect the answer.

    Second guard over `_ledger`'s own fail-open (same as the file cores keep):
    a ledger module that failed to import or changed shape must not be able to
    turn a working call into an error string.
    """
    try:
        # `out_lines` — how many lines OUR rendered answer had (Plan11-Task02).
        # The panel's teal heuristic compares it against the sum of lines the
        # nested reads claimed: output at least that big means the script very
        # likely printed the reads straight through, so the data can be trusted
        # as a genuine read rather than "something passed through a filter".
        #
        # Counted on the rendered `out`, not their raw result, because `out` is
        # what the model actually receives — the heuristic is about the model's
        # context, not about the sandbox's internals.
        _ledger.note_op(tool='execute_code', text=out,
                        session=kwargs.get("session_id") or "",
                        task_id=kwargs.get("task_id") or "default", op=op,
                        out_lines=out.count("\n") + 1 if out else 0,
                        # Plan11-Task03: the script itself. Alongside `terminal`
                        # this is the main case the sidecar exists for — a long
                        # script is exactly what stage B stubs out of the
                        # model's context, and after that this is the only copy.
                        command=command)
    except Exception:
        pass
