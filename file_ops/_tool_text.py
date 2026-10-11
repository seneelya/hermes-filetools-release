#!/usr/bin/env python3
"""Shared JSON-result → plain-text renderer for the tools we wrap.

Design: `__HQ/plans/Plan04__wrap-exec-and-process.md`, "Shared renderer".

Several of Hermes' own tools (`terminal`, `execute_code`, `process`) answer the
model with a `json.dumps(...)` blob. We do not reimplement any of them — we call
their live function and reformat only its return string. The reformatting turned
out to be the SAME operation every time, so it lives here once:

    scalars, one `Label: value` per line
    ○○○
    the one big text field, verbatim

The per-tool difference is DATA, not code: a labels map, which key holds the
body, which keys come first, and (for `terminal`'s `(raw: "exit_code": N)` tail)
a formatter for one value. Nothing else.

Two rules that are load-bearing rather than stylistic:

- **Nothing is dropped.** A key absent from `labels` still gets rendered, with
  its label title-cased from its own snake_case name. Upstream hands the model
  raw JSON today, so the model already has to find fields by name; dropping one
  a future upstream adds would be a strict regression.
- **Fail-open.** Input that is not valid JSON, or is not a dict once parsed, is
  passed through unchanged — same principle as `file_ops/_ledger.py`. A
  formatter must never be able to destroy a tool's answer.
"""

import json
import os
import re
import tempfile


def _sibling_vocab():
    """`_vocab.py` next to this file: package, bare (console), or by path (this module is
    itself loaded by path from the dashboard, where neither import works)."""
    try:
        from . import _vocab
        return _vocab
    except ImportError:
        pass
    try:
        import _vocab  # type: ignore
        return _vocab
    except ImportError:
        pass
    import importlib.util
    import sys
    from pathlib import Path as _P
    name = "hermes_filetools_vocab"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(name, _P(__file__).with_name("_vocab.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


_vocab = _sibling_vocab()

# The default header/body divider. Callers pass the resolved `symbols` dict
# (plugin.yaml's `formatting:` section, overridable by a profile), so this is
# only the code-level safety net — same arrangement as _new_format's
# DEFAULT_SYMBOLS, kept as a literal here so this module stays dependency-free.
_DEFAULT_SEPARATOR = "○"

# The symbol that opens every METADATA line of an answer that has no file (so no native
# comment syntax): address, fields, divider. Same arrangement as the separator: `symbols`
# carries `comment_fallback`, this literal is `_new_format.SYNTHETIC_COMMENT_START` kept here
# so the module stays dependency-free. The body below the divider and an `error` line are
# NOT metadata and never get it.
_DEFAULT_COMMENT = "◆"
_NOT_METADATA_KEYS = ("error",)


# Head/tail split when truncating, copied from hermes-agent's own
# terminal_tool.py (~line 3689) rather than chosen: error messages tend to
# appear EARLY and the relevant result LATE, so the middle is what to drop. Same
# split as their execute_code stdout cap (_truncate_stdout_text).
_HEAD_FRACTION = 0.4


def _scratch_dir() -> str:
    ram = os.path.join("c:\\RAM_Cache", "Temp")
    path = os.path.join(ram, "hermes-filetools", "spill") if os.path.isdir(ram) else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scratch", "spill")
    os.makedirs(path, exist_ok=True)
    return path


def truncate_for_model(text: str, max_chars: int, *, spill_dir: str = None,
                       spill_prefix: str = "hermes_tool_output_") -> str:
    """Cap `text` at `max_chars`, keeping head and tail and saying so.

    Two rules, both from being burned by the alternative:

    - **Never silently.** The omitted-character count stays in the text. A
      model that cannot tell it is missing output will confidently reason from
      half of it.
    - **The full text is not lost.** It is written to a file and the PATH goes
      into the notice, so the model can go read the part we dropped
      (`read_file` with a `line` range). This is the user's own
      instruction - "write it all to a file and tell it: the file is over
      there in temp" - and hermes-agent already reports full output the same
      way (`full_output_path` on their background terminal path).

    Returns `text` unchanged when it fits. Spilling is fail-open: if the file
    cannot be written the notice simply carries no path, because a truncation
    that raises would destroy an answer we already have in hand.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text

    head_chars = int(max_chars * _HEAD_FRACTION)
    tail_chars = max_chars - head_chars
    omitted = len(text) - head_chars - tail_chars

    path = None
    try:
        fd, path = tempfile.mkstemp(prefix=spill_prefix, suffix=".txt",
                                     dir=spill_dir or str(_scratch_dir()))
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(text)
    except OSError:
        path = None

    where = f" Full output saved to: {path}" if path else ""
    notice = (f"\n\n... [OUTPUT TRUNCATED - {omitted} chars omitted out of "
              f"{len(text)} total] ...{where}\n\n")
    return text[:head_chars] + notice + text[-tail_chars:]


def _label_for(key: str, labels: dict) -> str:
    return labels.get(key, key.replace("_", " ").capitalize())


def _scalar(value) -> str:
    """One value → one line's worth of text. A dict/list that lands ABOVE the
    separator is a small structured field (terminal's `verification_evidence`,
    say), not the body — compact JSON keeps it on one line, which is what a
    header line has to be."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _skip(value) -> bool:
    """`None` and `""` carry no information and cost a line each. Note `0` and
    `False` are NOT skipped: `exit_code: 0` is the most informative value the
    field ever has."""
    return value is None or value == ""


def _one_line(text: str) -> str:
    """Force a value onto one line, visibly. `process` action="list" puts a
    200-char `output_preview` in every row, and a raw newline in there would
    silently break the one-row-per-item promise the format is FOR. ⏎ is the
    same stand-in the desktop panel already uses when it flattens a
    multi-line command (desktop/src/core.js).
    """
    if "\n" not in text and "\r" not in text:
        return text
    return " ⏎ ".join(part for part in text.replace("\r\n", "\n")
                            .replace("\r", "\n").split("\n") if part != "")


def _render_items(items: list, labels: dict) -> str:
    """A list BELOW the separator, i.e. a collection is the answer (`process`
    with action="list"). An array of dicts flattened as `key: value` lines is
    unreadable, and a JSON blob is what we are removing — so one line per item,
    compact `k=v` pairs, key order stable across rows (first-seen order over the
    whole array, so a row missing a key doesn't shuffle the rest).
    """
    dicts = [i for i in items if isinstance(i, dict)]
    if not dicts:
        return "\n".join(_scalar(i) for i in items)

    order = []
    for item in dicts:
        for key in item:
            if key not in order:
                order.append(key)

    lines = []
    for item in items:
        if not isinstance(item, dict):
            lines.append(_scalar(item))
            continue
        pairs = [f"{_label_for(k, labels)}={_one_line(_scalar(item[k]))}"
                 for k in order if k in item and not _skip(item[k])]
        lines.append("  ".join(pairs))
    return "\n".join(lines)


def _render_body(value, labels: dict) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value  # verbatim, always - this is the tool's own output
    if isinstance(value, list):
        return _render_items(value, labels)
    if isinstance(value, dict):
        # Not expected from any tool we wrap today, but a dict body rendered as
        # a JSON blob would defeat the entire point of this module.
        return "\n".join(f"{_label_for(k, labels)}: {_scalar(v)}"
                         for k, v in value.items() if not _skip(v))
    return _scalar(value)


# The label the MODEL reads. Spelled out rather than `op:` (owner, 2026-09-03):
# a two-letter abbreviation is a guess for the model, "Operation ID" is not.
# Changing it here changes it everywhere it is emitted - but the three
# RECOGNISERS have to move together, or an echoed header stops being stripped
# and the panel stops finding the address: `markers._marker_line_pattern`,
# `dashboard/plugin_api.py::_OP_IN_HEADER`, `desktop/src/core.js::OP_RE`.
OP_LABEL = _vocab.COMMON["op"]


def op_line(op: str) -> str:
    """The `Operation ID: 0156xAB` header line, or `""` when there is no address.

    One function so the wording exists once: this string is what a human or the
    model later NAMES to point at an operation, and two spellings of it would
    make the address unusable for exactly the questions it exists to answer
    (Plan09 / `__HQ/vision/Vision03__context-surgery.md`).
    """
    return f"{OP_LABEL}: {op}" if op else ""


# --------------------------------------------------------------------------
# Operation STATE: re-rendering a stored answer in a different state.
# Plan13, owner's design 2026-09-09.
# --------------------------------------------------------------------------
#
# WHY THIS LIVES HERE. Removing an operation from the context rewrites a stored
# answer, and a rewrite has to know where the header ends, how a header line is
# framed and how a field is spelled. That is knowledge about the FORMAT, and the
# format already lives in this module. It was written a second time by hand in
# context_ctl/_stubs.py and a third time in dashboard/plugin_api.py, and both
# copies drifted: the hand-built stub stripped the tool signals, so the panel
# stopped recognising its own rows and dropped operations VANISHED from /ops.
#
# WHY NOT A METHOD ON THE OPERATION (owner's question, and the right one). By the
# time we remove something there IS no operation object - only a string in a DB
# row, read by a route in another process that never loads tool code. But the
# question turned out not to need an answer: every tool already puts everything
# valuable ABOVE the separator (read_file its File/Size/FileTag, search_files its
# `Found:`, terminal its exit code via _HEAD_KEYS) and only bulk below it. So the
# removal rule is the same for all of them - keep the header, empty the body -
# and nothing here needs to know which tool it is looking at.

NL_JOIN = chr(10)

STATE_LABEL = _vocab.COMMON["state"]

# Header fields that die WITH the body, because their only use is to point into
# it or to act on it. Dropped when an operation is removed (owner, 2026-09-09:
# "хорошо бы всё же экономить токены... файл таг точно мусор").
#
#   FileTag  — the tag in `■BLOCK xe8 : 1-21`. It addresses body blocks. With no
#              body it addresses nothing.
#   Encoding — only actionable when re-reading the file, and a re-read reports it
#              again anyway. Nothing in the transcript can use it.
#
# Kept, deliberately: File, Size, Modified. They say WHAT WAS HERE — path, how
# big, how old — which is the trace worth leaving, and none of them pretend the
# content is still available now that the state line says otherwise.
_DROP_ON_REMOVAL = ("FileTag", "Encoding")

# The one state that exists today. A human removed this operation from the
# model's context: the content is not shortened, summarised or moved - it is
# GONE, and the wording says so to the model in its own terms (owner, 2026-09-09:
# "removed_from_context ... специально так написала чтобы ллм понимала что
# произошло, dropped будет плохо пониматься").
STATE_REMOVED = "removed_from_context"

# Unanchored on purpose, exactly like dashboard/plugin_api.py::_OP_IN_HEADER: the
# header of a read_file answer is wrapped in the comment syntax of whichever file
# was opened, so the line can start with `#`, `//`, `<!--` or `◆`, and can end
# with a closer. Same reason the value is a bare token match rather than
# "the rest of the line".
_STATE_IN_HEADER = re.compile(re.escape(STATE_LABEL) + r":\s*([A-Za-z_][A-Za-z0-9_]*)")

# The op line as a TEMPLATE for framing. Its value is a single token of a KNOWN
# shape, which is what makes it usable: `post` is then unambiguously the closer
# and not part of the value. `Size: 21 lines, 524 bytes` could not be taken apart
# this way.
#
# The shape is spelled out rather than `\S+` because a closer need not be
# preceded by a space: `<!-- Operation ID: 001f -->` survives a greedy `\S+`,
# `%%~Operation ID: 001f~%%` does not - the token swallows the closer and the
# state line comes out unclosed. Found by the test, not by reading.
#
# Same pattern as the other two recognisers named at OP_LABEL
# (`dashboard/plugin_api.py::_OP_IN_HEADER`, `desktop/src/core.js::OP_RE`): they
# move together or the address stops resolving.
_OP_FRAME = re.compile(
    r"^(?P<pre>.*?)" + re.escape(OP_LABEL)
    + r":\s*(?P<op>[0-9a-fA-F]{4,}(?:x[0-9a-fA-F]{2,4})?)(?P<post>.*)$")


_BODY_FIELD = re.compile(r"(" + "|".join(_DROP_ON_REMOVAL) + r"):")


def _addresses_the_body(line: str) -> bool:
    """True for a header field whose only use dies with the body. Unanchored, as always."""
    return bool(_BODY_FIELD.search(line or ""))


def _split_at_separator(text: str, sep: str):
    """`(header_lines, separator_line, body_lines)`. Separator line is `None` if absent.

    The splitting rule is the PANEL's, shared with `to_facade_dict`: the first
    line CONTAINING the separator, never a line EQUAL to it - `read_file`'s
    divider is `◆○○○`, wrapped like every other header line.
    """
    lines = text.split("\n")
    at = next((i for i, line in enumerate(lines) if sep in line), -1)
    if at == -1:
        return lines, None, []
    return lines[:at], lines[at], lines[at + 1:]


def frame_of(line: str):
    """`(prefix, suffix)` of a header line, taken from the op line as a template.

    We CLONE the framing of a line that is already there instead of recomputing
    it from the file path with `comment_delims`. Recomputing would be a guess
    about how the answer was framed BACK THEN: the comment symbol for a plain
    file (`◆`) is configurable through plugin.yaml's `formatting:` section, the
    answer may have been written by an older version of the plugin, and the path
    in the header need not be the path the framing was computed from. The
    neighbouring line is not a guess - it is evidence from the row itself.
    """
    match = _OP_FRAME.match(line or "")
    if not match:
        return "", ""
    return match.group("pre"), match.group("post")


def state_of(text: str) -> str:
    """The operation state written in the header, or `""`. Header only, never the body."""
    header, _sep, _body = _split_at_separator(str(text or ""), _DEFAULT_SEPARATOR * 3)
    for line in header:
        match = _STATE_IN_HEADER.search(line)
        if match:
            return match.group(1)
    return ""


def as_removed(text: str, *, symbols: dict = None, op: str = "", tool: str = "") -> str:
    """A stored answer re-rendered as `removed_from_context`: header kept, body gone.

    THE HEADER IS KEPT WHOLE. Those lines describe the FILE, not what the model
    is holding, so with an explicit state above them they do not lie - and they
    are a useful trace to leave (owner, 2026-09-09). Keeping is also the
    reversible choice: trimming fields later is easy, restoring fields we threw
    away is impossible.

    THE SEPARATOR STAYS even though the body is now empty, which is the opposite
    of `render_json_result`'s rule ("no separator with nothing under it"). That
    rule is about composing a FRESH answer for a tool that produced no output.
    Here the answer existed and had a body; every recogniser downstream splits on
    that line, and taking it away would make the whole header parse as a body.

    Fail-open like the rest of the module: text we cannot make sense of comes
    back unchanged rather than being destroyed.
    """
    text = str(text or "")
    if not text.strip():
        return text
    sep = (symbols or {}).get("separator_symbol", _DEFAULT_SEPARATOR) * 3
    header, sep_line, _body = _split_at_separator(text, sep)

    # ASKED TO REMOVE MEANS REMOVED. No thresholds, no worthiness test, no refusal
    # (owner, 2026-09-09: "я не хочу встретить подобный отказ ни у одной операции -
    # команда должна работать; мне всё равно если там нечего удалять, оно должно
    # удалить это нечего"). The three shapes an answer can have are handled, and
    # none of them is a "no":
    #
    #   header + body     keep the header, stamp, drop the body    (a read)
    #   header, no body   stamp only                               (a write: the
    #                     expensive half is the call's ARGUMENTS, not this line)
    #   no header at all  replace everything with the stamp        (not our
    #                     envelope: there is no header to preserve, and the tool
    #                     signals a panel looks for were never there either)
    if sep_line is None and not any(_OP_FRAME.match(line) for line in header):
        stamped = [op_line(op)] if op else []
        # ЧУЖОЙ ответ (владелец, 2026-09-29): от него не остаётся ничего, поэтому след —
        # имя тула и сколько было. Без него модель видит «что-то убрано» и не знает что.
        if tool:
            stamped.append(f"{_vocab.COMMON['tool']}: {tool}")
        stamped.append(f"{_vocab.COMMON['size']}: {len(text)} chars")
        stamped.append(f"{STATE_LABEL}: {STATE_REMOVED}")
        return NL_JOIN.join(stamped)

    op_at = next((i for i, line in enumerate(header) if _OP_FRAME.match(line)), -1)
    pre, post = frame_of(header[op_at]) if op_at >= 0 else ("", "")
    state_line = f"{pre}{STATE_LABEL}: {STATE_REMOVED}{post}"

    out = [line for line in header if not _addresses_the_body(line)]
    # The address may have been one of the lines we just dropped only if the
    # answer had no op line at all, in which case op_at was -1 anyway.
    op_at = next((i for i, line in enumerate(out) if _OP_FRAME.match(line)), -1)
    at = next((i for i, line in enumerate(out) if _STATE_IN_HEADER.search(line)), -1)
    if at >= 0:
        out[at] = state_line          # already removed once: restate, never stack
    else:
        out.insert(op_at + 1, state_line)   # right under the address; -1 puts it first

    if sep_line is not None:
        out.append(sep_line)
    return "\n".join(out)


def render_json_result(raw: str, *, labels: dict = None, body_keys=("output",),
                       head_keys=(), value_formatters: dict = None,
                       symbols: dict = None, body_max_chars: int = None,
                       spill_dir: str = None, op: str = "",
                       count_body: bool = False) -> str:
    """JSON string (their contract) → plain text (ours).

    `count_body` — a LIST body whose items are things (processes), not lines of
    output: its count goes in the header, `Processes: 0` included.

    `body_keys` — candidate keys holding the big field that goes BELOW the
    separator; the first one PRESENT wins (present, not truthy: an empty body
    still has to be taken out of the header). Every candidate is removed either
    way, so a tool that returns two of them cannot leak one into the header.

    `head_keys` — keys rendered first, in the order given, ahead of whatever
    else upstream returned. Everything remaining keeps upstream's own order:
    stable-but-arbitrary is easier to diff when they add a field than sorted.

    `value_formatters` — per-key `value -> str` for the rare field that needs
    more than `str()`. This is how `terminal`'s `(raw: "exit_code": N)` tail
    stays terminal's business instead of being baked in here.

    `op` — this operation's address (Plan09), emitted as the FIRST header line
    so it is visible without reading the rest. Empty means no line at all; the
    number is obtained from `_ledger.next_op` by the caller, because this
    module stays dependency-free on purpose.

    `body_max_chars` — cap applied to the BODY only, via
    `truncate_for_model`. Only the body, deliberately: the header is a handful
    of short lines the model needs in full (an exit code it cannot see is worse
    than output it knows was cut), and cutting the assembled text could take
    the `○○○` line itself with it.
    """
    labels = labels or {}
    value_formatters = value_formatters or {}
    sep = (symbols or {}).get("separator_symbol", _DEFAULT_SEPARATOR) * 3
    mark = (symbols or {}).get("comment_fallback") or _DEFAULT_COMMENT

    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if not isinstance(data, dict):
        return raw

    body_value = None
    body_key = ""
    for key in body_keys:
        found = data.pop(key, None)
        if body_value is None and found is not None:
            body_value, body_key = found, key

    lines = []
    first = op_line(op)
    if first:
        lines.append(mark + first)

    def field(key, value):
        render = value_formatters.get(key, _scalar)
        text = f"{_label_for(key, labels)}: {render(value)}"
        return text if key in _NOT_METADATA_KEYS else mark + text

    for key in head_keys:
        if key not in data:
            continue
        value = data.pop(key)
        if _skip(value):
            continue
        lines.append(field(key, value))

    for key, value in data.items():
        if _skip(value):
            continue
        lines.append(field(key, value))

    # A collection body is COUNTED in the header, empty or not, the way search says
    # `Found: 0`: `process list` with nothing running used to come back as the bare op
    # line, while their JSON said `"processes": []` (live 2026-10-06, `deferred/20`).
    # Opt-in: a list of OUTPUT lines counted would be noise. Empty text stays silent.
    if count_body and isinstance(body_value, list):
        lines.append(field(body_key, len(body_value)))

    header = "\n".join(lines)
    body = _render_body(body_value, labels)
    if body_max_chars:
        body = truncate_for_model(body, body_max_chars, spill_dir=spill_dir)
    if header and body:
        return f"{header}\n{mark}{sep}\n{body}"
    # No separator with nothing under it: ○○○ promises a body, and a tool that
    # produced no output has none.
    return header or body


# --------------------------------------------------------------------------
# The second facade: a dict for a Python script inside execute_code.
# Plan06, __HQ/docs/Doc__execute_code_bridge.md.
# --------------------------------------------------------------------------

# Every error path in the four "new" cores returns a string starting with this
# (verified across view_file_new_core / create_file_new_core /
# sift_files_new_core / modify_file_new_core, 2026-09-02). It is the ONLY
# signal those four have for failure - upstream has no exit-code notion for
# them - so it is a contract of theirs, not an observation about them: a new
# error path that forgets the prefix reports success through this facade.
_ERROR_PREFIX = "ERROR:"

# `terminal`'s real code, off its own header line (_LABELS["exit_code"] plus
# terminal_core's `(raw: ...)` tail, which exists precisely so an exit code can
# be machine-read out of our text - agent/context_compressor.py already does
# the same thing).
_EXIT_CODE_LINE = re.compile(r"^\W*Exit code:\s*(-?\d+)")


def _facade_exit_code(text: str, header: str) -> int:
    """0/1 for the file tools, the real code for `terminal`.

    Never invents a 0: "succeeded" and "has no such notion" are different
    facts, and a script branching on `== 0` would read a fabricated zero as
    confirmation. A tool with no exit code and no error genuinely did succeed,
    which is what the 0 at the end means.
    """
    if text.lstrip().startswith(_ERROR_PREFIX):
        return 1
    for line in header.split("\n"):
        found = _EXIT_CODE_LINE.match(line.strip())
        if found:
            return int(found.group(1))
    return 0


def to_facade_dict(text: str, *, symbols: dict = None) -> dict:
    """Our own plain text -> the three keys a chained Python script wants.

    `{"exit_code": int, "header": str, "body": str}` - the same three for every
    tool, on purpose. Nothing is lost: every header field stays in `header` as
    text, just not broken out into keys of its own. That is the whole design
    win - with no per-tool keys there is no per-tool schema to document, and
    nothing that can drift out of date the way core's own `_TOOL_DOC_LINES`
    does.

    Splitting rule is the PANEL's, not a second convention: the first line
    CONTAINING the separator (desktop/src/operation-card.js::headerEndIndex).
    That matters because `read_file`'s divider is `◆○○○` - our header is
    wrapped in the comment syntax of whichever file was opened (`//`, `#`, `◆`
    when there is none) - so a rule looking for a line EQUAL to `○○○` would
    quietly hand back the whole answer as `header` and an empty `body`.

    No separator at all means the tool produced no body (render_json_result
    only emits one when both halves exist), so everything is header.
    """
    sep = (symbols or {}).get("separator_symbol", _DEFAULT_SEPARATOR) * 3
    lines = text.split("\n")
    at = next((i for i, line in enumerate(lines) if sep in line), -1)
    if at == -1:
        header, body = text, ""
    else:
        header = "\n".join(lines[:at])
        body = "\n".join(lines[at + 1:])
    return {
        "exit_code": _facade_exit_code(text, header),
        "header": header,
        "body": body,
    }
