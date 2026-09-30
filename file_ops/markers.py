"""FileTag resolver + marker-line recognizer, shared by read_file/write_file/
future tools that emit or consume the block_symbol/anchor_symbol/
separator_symbol marker vocabulary. See __HQ/vision/Vision01__unique_tokens.md
for the full design contract - this module is only its implementation.
"""

from __future__ import annotations

import hashlib
import re

try:  # plugin mode
    from ._new_format import DEFAULT_SYMBOLS, comment_delims
except ImportError:  # console mode
    from _new_format import DEFAULT_SYMBOLS, comment_delims  # type: ignore

# Vision01__unique_tokens.md §2 - cheapest (fewest tokens) first, escalate
# only on a real, verified collision against the actual file content.
TAG_BIT_LADDER = (8, 12, 16)

_TAG_RE = r"x[0-9a-fA-F]{2,4}"  # covers every rung of TAG_BIT_LADDER (2-4 hex digits)


def _normalize_for_tag(content: str) -> str:
    """EOL + trailing-whitespace normalization before hashing (OMP's method,
    Vision01__unique_tokens.md §2) - a file that only differs by CRLF/LF or
    trailing spaces must not get a different FileTag."""
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip(" \t") for line in normalized.split("\n"))


def _tag_for_bits(content: str, bits: int) -> str:
    digest = hashlib.sha1(_normalize_for_tag(content).encode("utf-8", errors="ignore")).hexdigest()
    nibbles = (bits + 3) // 4  # hex digits needed to cover `bits` bits
    return "x" + digest[:nibbles]


def resolve_file_tag(content: str, block_symbol: str = None) -> str:
    """Pick the FileTag for one read_file response over *content* (the whole
    file, not just the returned page - keeps the tag stable across pages of
    the same call/file).

    Tries TAG_BIT_LADDER from the cheapest (8 bit) up, verifying the resulting
    marker strings against *content* each time. Falls back to the widest rung
    (16 bit) if every rung still collides - open question, not a hard
    guarantee (Vision01__unique_tokens.md §2).
    """
    block_symbol = block_symbol or DEFAULT_SYMBOLS["block_symbol"]
    tag = None
    for bits in TAG_BIT_LADDER:
        tag = _tag_for_bits(content, bits)
        probes = (f"{block_symbol}BLOCK {tag}", f"{block_symbol}END {tag}")
        if not any(p in content for p in probes):
            return tag
    return tag


def _marker_line_pattern(symbols: dict) -> re.Pattern:
    block = re.escape(symbols["block_symbol"])
    anchor = re.escape(symbols["anchor_symbol"])
    sep = re.escape(symbols["separator_symbol"])
    return re.compile(
        rf"^(?:"
        rf"{block}(?:BLOCK|END)\s*(?:{_TAG_RE})?\s*:.*"
        # `Line:` with the colon is what place_anchors emits now; the
        # colon-less `Line N` form stays accepted on purpose - a model may
        # still be holding a response produced before the format changed, and
        # failing to recognise it would mean writing an anchor into a file.
        rf"|{anchor}(?:{_TAG_RE})?\s*Line\s*:?\s*\d+.*"
        rf"|{sep}{{3}}"
        rf"|File:.*"
        rf"|Modified:.*"
        rf"|FileTag:\s*{_TAG_RE}"
        # The operation address (Plan09, label spelled out 2026-09-03 - see
        # `_tool_text.OP_LABEL`). It lives only in a response header
        # and must never reach a file, but the model can copy our header into
        # `content` - so it has to be part of THIS grammar, or it becomes the
        # one header line the write-side shield lets through. The FileTag tail
        # is no longer EMITTED (2026-09-03) but stays matched here: a session
        # in flight, or a model still holding an older response, can echo
        # `0156xbc` back into content.
        rf"|Operation ID:\s*[0-9a-fA-F]{{4,}}(?:{_TAG_RE})?"
        rf")$"
    )


def block_marker_lines(content: str, path: str, symbols: dict = None) -> list:
    """The ■BLOCK/■END lines in *content*, verbatim, comment wrapper included.

    Exists so a caller that is about to remove our markers can keep the one
    piece of information they carry that is NOT recoverable from anywhere
    else: WHICH lines of the file the body actually covers (`■BLOCK x1b :
    1-1`). read_file's facade moves them up into the header for exactly that
    reason - a ranged read whose range is gone reads as a whole-file read.

    Recognition is this module's own (same comment-wrapping and grammar as
    `strip_marker_lines`, one grammar for the vocabulary), narrowed to the
    block symbol: ⚓ anchors and File:/Modified:/FileTag: lines are either not
    positional or already in the header.
    """
    symbols = symbols or DEFAULT_SYMBOLS
    comment_start, comment_end = comment_delims(path, symbols)
    pattern = _marker_line_pattern(symbols)
    block_symbol = symbols.get("block_symbol", DEFAULT_SYMBOLS["block_symbol"])

    found = []
    for line in content.split("\n"):
        inner = None
        if comment_start and line.startswith(comment_start):
            if comment_end:
                if line.endswith(comment_end):
                    inner = line[len(comment_start):len(line) - len(comment_end)]
            else:
                inner = line[len(comment_start):]
        if inner is None:
            continue
        stripped = inner.strip()
        if stripped.startswith(block_symbol) and pattern.match(stripped):
            found.append(line)
    return found


def strip_marker_lines(content: str, path: str, symbols: dict = None) -> tuple:
    """Recognize and remove any line in *content* that looks like OUR marker
    vocabulary (any FileTag width, not just the current one) - a defense
    against the model echoing a copy-pasted read_file line back into content
    bound for write_file/patch. Uses *path*'s comment style to find candidate
    lines structurally (comment-wrapped + matches the marker grammar), not by
    bare substring search.

    Returns (cleaned_content, removed_count).

    Two different removals, and the difference matters (it was left open here
    on purpose until the anchor design existed - Vision01__unique_tokens.md §5
    called it "a patch-tool concern"):

    - An **⚓ anchor** OVERWRITES a line that was blank in the file
      (_new_format.place_anchors keeps that as a hard invariant), so undoing
      one means putting the blank line BACK, not deleting the line. Deleting
      it would leave an excerpt one line shorter than the file it came from,
      and patch's line-based matching would no longer line up.
    - Every other marker (■BLOCK/■END/File:/Modified:/FileTag:/○○○) is a line
      the file never had at all, so it is removed outright.
    """
    symbols = symbols or DEFAULT_SYMBOLS
    comment_start, comment_end = comment_delims(path, symbols)
    pattern = _marker_line_pattern(symbols)
    anchor_symbol = symbols.get("anchor_symbol", DEFAULT_SYMBOLS["anchor_symbol"])

    kept = []
    removed = 0
    for line in content.split("\n"):
        inner = None
        if comment_start and line.startswith(comment_start):
            if comment_end:
                if line.endswith(comment_end):
                    inner = line[len(comment_start):len(line) - len(comment_end)]
            else:
                inner = line[len(comment_start):]

        if inner is not None and pattern.match(inner.strip()):
            removed += 1
            if inner.strip().startswith(anchor_symbol):
                # Restore the blank line the anchor took over - carrying back
                # its "\r" in a CRLF file, so the restored line is byte-for-byte
                # what the file has there (place_anchors keeps the same rule).
                kept.append("\r" if line.endswith("\r") else "")
            continue
        kept.append(line)

    return "\n".join(kept), removed
