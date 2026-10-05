"""Shared helpers for the "new" (plain-text, gcb-styled) read_file/write_file paths.

Both view_file_new_core.py and create_file_new_core.py need the same line/EOL
bookkeeping and the same comment-wrapping/symbol conventions
(Vision01__read.md / Vision01__write.md) - kept here once instead of
duplicated between the two.
"""

from __future__ import annotations

try:  # plugin mode
    from . import _vocab
except ImportError:  # console mode
    import _vocab  # type: ignore

from pathlib import Path


# ── Line / EOL bookkeeping (Vision01__write.md §2) ──────────────────────────

def to_lines_and_trailing_nl(content: str):
    """Split *content* into its real lines, plus whether it ends with a newline.

    Matches the line-counting formula from Vision01__write.md §2 (deliberately
    NOT ``wc -l``, which reports 0 for a file with no trailing newline - see
    core's B4 bug): "" -> 0 lines; otherwise the line count already comes out
    to count('\\n') + (0 if the content ends with '\\n' else 1) once the
    trailing-newline split artifact below is dropped.
    """
    if content == "":
        return [], False
    trailing = content.endswith("\n")
    lines = content.split("\n")
    if trailing:
        lines = lines[:-1]  # drop the "" artifact split() leaves after a trailing \n
    return lines, trailing


def join_lines(lines, trailing_nl: bool) -> str:
    if not lines:
        return ""
    body = "\n".join(lines)
    return body + "\n" if trailing_nl else body


def count_lines(content: str) -> int:
    lines, _ = to_lines_and_trailing_nl(content)
    return len(lines)


# ── Comment-wrapping (Vision01__read.md §3) ─────────────────────────────────

# (comment_start, comment_end) per extension. Paired-comment-only languages
# (markdown/html/xml) get a real closer; single-token-comment languages get
# "" - either way every marker line closes itself on one line, by construction
# (see make_commenter below) - never a dangling opener that could break real
# code if a line were copy-pasted out of context.
_COMMENT_DELIMS = {
    ".py": ("#", ""), ".pyw": ("#", ""),
    ".js": ("//", ""), ".jsx": ("//", ""), ".mjs": ("//", ""), ".cjs": ("//", ""),
    ".ts": ("//", ""), ".tsx": ("//", ""),
    ".cs": ("//", ""),
    ".cpp": ("//", ""), ".cc": ("//", ""), ".cxx": ("//", ""), ".c++": ("//", ""),
    ".hpp": ("//", ""), ".hh": ("//", ""), ".hxx": ("//", ""), ".h": ("//", ""), ".c": ("//", ""),
    ".scss": ("//", ""), ".sass": ("//", ""),
    ".css": ("/*", " */"),
    ".md": ("<!-- ", " -->"), ".markdown": ("<!-- ", " -->"),
    ".html": ("<!-- ", " -->"), ".htm": ("<!-- ", " -->"), ".xml": ("<!-- ", " -->"),
    ".yaml": ("#", ""), ".yml": ("#", ""),
    ".sh": ("#", ""), ".bash": ("#", ""),
    ".sql": ("--", ""),
}

# Files with no native comment syntax (json, csv, extensionless, ...) get this
# synthetic token instead - still self-closing (comment_end == "").
#
# CODE-LEVEL DEFAULT ONLY since 2026-09-23. The diamond used to be a module constant and
# nothing more - the one named symbol of our output a human could NOT change, while its three
# siblings had lived in `plugin.yaml` from the start. Found by the owner: "нет ромба в
# конфиге, а он у нас везде используется". Two things were wrong at once: the symbol was
# unreachable, and `_tool_text.py` already DOCUMENTED it as configurable - a doc that lied.
SYNTHETIC_COMMENT_START = "◆"  # ◆


def comment_delims(path: str, symbols: dict = None):
    """Comment framing for *path*: the language's own syntax, our diamond when it has none.

    `symbols` is the same dict every other named symbol travels in (`plugin.yaml`'s
    `formatting:`), so the fallback is overridable exactly like `block_symbol` and friends.
    Passing nothing keeps the code-level default - that is the safety net, not the source.
    """
    ext = Path(path).suffix.lower()
    fallback = (symbols or {}).get("comment_fallback") or SYNTHETIC_COMMENT_START
    return _COMMENT_DELIMS.get(ext, (fallback, ""))


def make_commenter(path: str, symbols: dict = None):
    """Return c(text) -> one self-closing comment-wrapped metadata line."""
    start, end = comment_delims(path, symbols)

    def c(text: str) -> str:
        return f"{start}{text}{end}"

    return c


# ── Named symbols (Vision01__read.md §2) ────────────────────────────────────
# Code-level safety net only - plugin.yaml's own `formatting:` section (and a
# profile's override of it) are the real, intended source; these are only used
# if neither declares a value.

DEFAULT_SYMBOLS = {
    "block_symbol": "■",      # ■
    "anchor_symbol": "⚓",     # ⚓ (sparse in-body orientation marks, see place_anchors)
    "separator_symbol": "○",  # ○
    # The fourth, added 2026-09-23 - see SYNTHETIC_COMMENT_START above for why it was missing.
    "comment_fallback": SYNTHETIC_COMMENT_START,  # ◆ when the file has no comment syntax
}


# ── ⚓ anchors: sparse in-body line-number orientation ──────────────────────
# Vision01__read.md / BRAINSTORM__read_patch.md §5.1, and a precondition for
# patch's `line` hint (Vision01__patch.md §10): a plain read returns raw
# content, so without these the model has nowhere to get a line number from
# except search_files.

# Don't anchor more often than this. Also why short files get none at all:
# the first anchor can only land this far into the returned body, and the
# ■BLOCK header's own range already orients anything smaller.
# Only the code-level default - plugin.yaml's params.anchor_gap overrides it
# (and params.anchors turns placement off entirely), see CONTRACT.md §18.
ANCHOR_MIN_GAP = 25


# ── UI envelope (CONTRACT.md §19) ───────────────────────────────────────────
# Our plain-text response is the reference form and stays byte-for-byte intact:
# the JSON, when enabled, is a short block ABOVE it, not a wrapper around it.
# That ordering is core's own existing convention - acp_adapter/tools.py's
# _json_loads_maybe() decodes the FIRST JSON value and tolerates whatever
# follows, with a comment saying some Hermes tools already append a human hint
# after a JSON payload. Cost to the model is one short line and NOT a single
# escaped character, which is why this can be on by default one day.
#
# The desktop app is the exception: all three of its parse paths
# (lib/chat-messages/tool-parts.ts, tool/fallback-model/format.ts,
# tool/fallback.tsx) run a strict JSON.parse over the WHOLE string, so a
# trailing text tail makes them see nothing at all. Mode "wrap" exists only for
# that client - it puts the text inside `message` and pays the escaping.

UI_JSON_MODES = ("off", "min", "diff", "wrap")

# Cap on the duplicated changed lines - a copy for a human to glance at, never
# a second copy of a large edit.
UI_DIFF_MAX_LINES = 40

_UI_HINT = ("fields above are a copy for the UI, not new information - "
            "read the text below this JSON")


def resolve_ui_json_mode(value) -> str:
    """Config value -> a valid mode. Anything unrecognised becomes "off": the
    reference text is what we protect, so a typo must never silently turn the
    response into something else."""
    if isinstance(value, str) and value.strip().lower() in UI_JSON_MODES:
        return value.strip().lower()
    if value is True:  # `ui_json: true` in YAML - read as the cheapest useful mode
        return "min"
    return "off"


def minimal_diff(old_text: str, new_text: str, max_lines: int = UI_DIFF_MAX_LINES) -> str:
    """Just the lines that left and the lines that arrived - no context, no
    hunk headers. Enough for a UI card to show "this went, that came"."""
    # to_lines_and_trailing_nl's rule, not a bare split: a trailing newline is a
    # terminator, not an extra empty line. Without it an inserted "foo\n" came
    # out as "+foo" followed by a bare "+".
    removed, _ = to_lines_and_trailing_nl(old_text or "")
    added, _ = to_lines_and_trailing_nl(new_text or "")
    out = ["-" + ln for ln in removed[:max_lines]] + ["+" + ln for ln in added[:max_lines]]
    if len(removed) > max_lines or len(added) > max_lines:
        out.append(f"... truncated to {max_lines} lines per side")
    return "\n".join(out)


def ui_envelope(mode: str, facts: dict, text: str) -> str:
    """Combine the reference *text* with the UI-facing *facts* per *mode*.

    off  -> text alone (unchanged reference form)
    min  -> short JSON block, then the text below it
    diff -> same plus a copy of the changed lines and a note saying it IS a copy
    wrap -> everything inside the JSON (`message`), for strict-parsing clients
    """
    import json

    mode = resolve_ui_json_mode(mode)
    if mode == "off" or not facts:
        return text

    payload = {k: v for k, v in facts.items() if v is not None and k != "diff"}
    # On failure nothing was written, so a files_modified list would tell the UI
    # a file changed when it didn't - drop it rather than let a client draw a
    # phantom edit.
    if payload.get("success") is False:
        payload.pop("files_modified", None)
        payload.pop("added", None)
        payload.pop("removed", None)
    # `wrap` carries the diff too, not just `diff` mode. Found by testing rather
    # than by reading: the desktop's gate is the diff and nothing else -
    # thread/changed-files.ts does `if (!inlineDiffFromResult(result)) continue`,
    # and the tool card's own body is `defaultOpen = Boolean(inlineDiff)`. So a
    # payload without one renders NOTHING anywhere, however well-formed it is
    # (min and wrap were both tried live and both showed nothing). And `wrap`
    # exists specifically for that client, which also needs the whole payload to
    # be valid JSON - so wrap without a diff could never do its one job.
    if mode in ("diff", "wrap") and facts.get("diff"):
        payload["diff"] = facts["diff"]
        payload["ui_hint"] = _UI_HINT
    if mode == "wrap":
        payload["message"] = text
        return json.dumps(payload, ensure_ascii=False)
    return json.dumps(payload, ensure_ascii=False) + "\n" + text


def resolve_anchor_gap(value, default: int = ANCHOR_MIN_GAP) -> int:
    """Config-supplied anchor spacing -> a usable int.

    Anything that isn't a whole number >= 1 falls back to *default* rather than
    disabling anchors or crashing: "off" has its own switch (params.anchors),
    so a junk gap is a typo, not an intent."""
    try:
        gap = int(value)
    except (TypeError, ValueError):
        return default
    return gap if gap >= 1 else default


def place_anchors(page_lines, start_line: int, commenter, anchor_symbol: str,
                  tag: str, min_gap: int = ANCHOR_MIN_GAP):
    """Replace SOME already-blank lines with a line-number anchor.

    Hard invariant: anchors only ever OVERWRITE an existing blank line, never
    insert a new one. So the returned body always has exactly as many lines as
    the file region it came from, and undoing an anchor means putting a blank
    line back (markers.strip_marker_lines does exactly that). Inserting lines
    instead - e.g. force-anchoring every N lines through a dense stretch with
    no blank lines, the alternative sketched in CONTRACT.md §10 - would break
    that invariant: a copied excerpt would carry a line the file doesn't have,
    and patch's line-based matching would then fail to line up.

    Consequence, accepted deliberately (BRAINSTORM §5.1 calls it best-effort):
    a stretch of code with no blank line at all gets no anchors. That is the
    cheap trade - such a stretch is exactly where counting from the ■BLOCK
    header still works, because there is nothing structural to lose track of.
    """
    out = []
    last = start_line  # first anchor lands no earlier than min_gap lines in
    for i, line in enumerate(page_lines):
        n = start_line + i
        if not line.strip() and (n - last) >= min_gap:
            # Keep the replaced line's own EOL. The caller split the file on
            # "\n", so in a CRLF file every line (a blank one included) still
            # carries its trailing "\r" - an anchor built from scratch would
            # not, leaving the body with mixed line endings on exactly the
            # lines we touched. Invisible on screen, but it makes an excerpt
            # copied back out fail an exact match.
            eol = "\r" if line.endswith("\r") else ""
            # "Line: N", with the colon, to match every other metadata line we
            # emit (File:/Size:/FileTag:/Modified:). Without it this was the
            # only one phrased as prose - "Line 33" reads as a claim to be
            # checked, "Line: 33" reads as a field.
            out.append(commenter(f"{anchor_symbol}{tag} {_vocab.MARKS['anchor_word']}: {n}") + eol)
            last = n
        else:
            out.append(line)
    return out


# ── mtime anchor (Vision01__write.md §4 / Vision01__read.md §6) ────────────

def format_mtime(epoch_seconds: float) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(epoch_seconds).strftime("%Y-%m-%d %H:%M")


# ── Size-shape facts (Vision01__read.md, "Size:" line) ──────────────────────
# Grouped together on purpose - one "shape of the file" fact-group, unlike
# Encoding/FileTag/Hint which are separate, conditional axes and stay on
# their own lines.

def detect_eol(content: str) -> str:
    """Best-effort EOL style label for display - "n/a" for content with no
    line break at all (empty, or a single line with no trailing newline)."""
    if "\r\n" in content:
        return "CRLF"
    if "\n" in content:
        return "LF"
    return "n/a"


def line_length_stats(lines: list):
    """(max_len, avg_len) over *lines* - (0, 0.0) for an empty list."""
    if not lines:
        return 0, 0.0
    lengths = [len(ln) for ln in lines]
    return max(lengths), sum(lengths) / len(lengths)


# C0 control characters are VALID codepoints, so `errors="replace"` does not
# touch them: a NUL or a SUB read out of a binary file travels intact into the
# model's context, into their `messages.content`, and into whatever writes the
# transcript. Owner caught a log capture dying on exactly this three times
# before we found the cause (2026-09-03). Tab/newline/CR are excluded because
# they are the text itself, not noise inside it.
_KEEP_CONTROLS = (9, 10, 13)

# Unicode "Control Pictures" (U+2400…U+241F, plus U+2421 for DEL): one visible
# glyph per byte, so line lengths, ranges and line numbers stay exactly as they
# were - a substitution that changed widths would break the ■BLOCK ranges the
# same answer prints.
_CONTROL_PICTURES = {
    chr(c): chr(0x2400 + c) for c in range(0x20) if c not in _KEEP_CONTROLS
}
_CONTROL_PICTURES[chr(0x7F)] = chr(0x2421)


def escape_controls(text: str):
    """`(escaped_text, count)` — C0 controls shown as ␀-style pictures.

    Returns the text UNCHANGED (and 0) when there is nothing to escape, which
    is the case for every ordinary text file: a file that does contain these
    bytes is not text, and our read-dispatch already refuses it without
    `--force`. The caller must SAY how many were escaped — a body that is no
    longer a byte copy of the file has to admit it.
    """
    if not text:
        return text, 0
    count = 0
    out = []
    for ch in text:
        replacement = _CONTROL_PICTURES.get(ch)
        if replacement is None:
            out.append(ch)
        else:
            out.append(replacement)
            count += 1
    return ("".join(out), count) if count else (text, 0)
