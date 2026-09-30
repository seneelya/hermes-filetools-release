"""Read-time file classification core ("read dispatch engine") - decides
WHETHER and HOW read_file should read a given file's bytes, before
view_file_new_core.py builds any response text.

Extension-routed known document types (.docx/.xlsx/.pptx/.ipynb) stay
handled by view_file_new_core.py itself, unchanged - this module only
covers everything else: is it actually binary regardless of what its
extension/lack of extension claims, and if not, what encoding is it.

Order (cheapest/most certain first, matching Vision01__read.md): our own
magic-byte signature table (tier 1, zero dependency, always runs) ->
`filetype` library (tier 2, optional, broader coverage) -> text encoding
detection (BOM -> strict UTF-8 -> `charset-normalizer` guess -> unknown).
A hard binary signature match always refuses (needs flags="--force" to
bypass); an unconfirmed/unknown encoding also refuses unless forced -
nothing here silently guesses past the model's back.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ReadDecision:
    kind: str                       # "text" | "refuse"
    encoding: str = "utf-8"
    encoding_verified: bool = True  # False = guessed (charset-normalizer), not confirmed
    reason: str = None              # set when kind == "refuse"


# ── Tier 1: our own magic-byte table (zero dependency, always runs) ────────
# (name, offset, signature bytes) - just the common "renamed binary file"
# cases; broader coverage is `filetype`'s job (tier 2, optional).
_SIGNATURES = [
    ("JPEG", 0, b"\xff\xd8\xff"),
    ("PNG", 0, b"\x89PNG\r\n\x1a\n"),
    ("GIF", 0, b"GIF8"),
    ("PDF", 0, b"%PDF-"),
    ("ZIP", 0, b"PK\x03\x04"),    # also docx/xlsx/pptx/jar - extension already
                                  # routes those to extraction before this runs
    ("GZIP", 0, b"\x1f\x8b"),
    ("ELF", 0, b"\x7fELF"),
    ("PE/EXE", 0, b"MZ"),
    ("RAR", 0, b"Rar!\x1a\x07"),
    ("7Z", 0, b"7z\xbc\xaf\x27\x1c"),
]


def _sniff_signature(raw: bytes) -> str:
    for name, offset, sig in _SIGNATURES:
        if raw[offset:offset + len(sig)] == sig:
            return name
    return None


def _sniff_filetype(raw: bytes) -> str:
    try:
        import filetype
    except ImportError:
        return None
    kind = filetype.guess(raw)
    return kind.mime if kind else None


# ── Encoding detection ──────────────────────────────────────────────────────

# Longer/more specific BOMs first - utf-32-le's BOM is a prefix-superset of
# utf-16-le's, so it must be checked first or it'd never match. Names are the
# GENERIC codecs ("utf-16"/"utf-32", not "-le"/"-be") on purpose: only those
# auto-detect AND strip the BOM on decode - the explicit-endianness variants
# just decode the BOM bytes as a literal U+FEFF character in the output.
_BOMS = [
    ("utf-8-sig", b"\xef\xbb\xbf"),
    ("utf-32", b"\xff\xfe\x00\x00"),
    ("utf-32", b"\x00\x00\xfe\xff"),
    ("utf-16", b"\xff\xfe"),
    ("utf-16", b"\xfe\xff"),
]

_ENCODING_SAMPLE_BYTES = 128 * 1024  # bound charset-normalizer's cost on huge files


def _detect_bom(raw: bytes) -> str:
    for name, bom in _BOMS:
        if raw.startswith(bom):
            return name
    return None


def _guess_encoding(raw: bytes):
    """Returns (encoding_or_None, verified). verified=True only for a BOM or
    a strict-UTF-8 pass - both are facts, not guesses."""
    bom = _detect_bom(raw)
    if bom:
        return bom, True
    try:
        raw.decode("utf-8", errors="strict")
        return "utf-8", True
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes
    except ImportError:
        return None, False
    best = from_bytes(raw[:_ENCODING_SAMPLE_BYTES]).best()
    if best is None:
        return None, False
    return best.encoding, False


def dispatch(raw: bytes, force: bool = False, encoding: str = None) -> ReadDecision:
    """Classify *raw* bytes (already known NOT to be a routed extractable
    document - see module docstring). *encoding*, if given, is an explicit
    Python codec name (e.g. "cp866") that skips auto-detection AND the
    binary/encoding refusal entirely - the caller (model) is asserting it
    knows better, most useful when an auto-guess came back wrong (see
    Vision01__read.md - charset-normalizer can and does mis-guess close
    Cyrillic codepages like mac_cyrillic vs cp1251)."""
    if encoding:
        import codecs
        try:
            codecs.lookup(encoding)
        except LookupError:
            return ReadDecision(
                kind="refuse",
                reason=f"'{encoding}' is not a known codec name (Python encoding).",
            )
        return ReadDecision(kind="text", encoding=encoding, encoding_verified=True)

    sig = _sniff_signature(raw) or _sniff_filetype(raw)
    if sig is not None and not force:
        return ReadDecision(
            kind="refuse",
            reason=(f"file appears to be {sig} (binary signature), not text. "
                     'Pass flags="--force" to read anyway.'),
        )

    encoding, verified = _guess_encoding(raw)
    if encoding is None:
        if force:
            return ReadDecision(kind="text", encoding="utf-8", encoding_verified=False)
        try:
            import charset_normalizer  # noqa: F401
            hint = "encoding could not be determined"
        except ImportError:
            hint = "encoding unknown - pip install charset-normalizer for detection"
        return ReadDecision(
            kind="refuse",
            reason=f'{hint}. Pass flags="--force" to read as UTF-8 best-effort.',
        )

    return ReadDecision(kind="text", encoding=encoding, encoding_verified=verified)
