"""Stopgap notice for large, structurally flat TSX/JSX blocks (a big `return (...)` whose
JSX children aren't promoted to landmarks — see the internal planning doc for the real fix,
not referenced in the printed message itself: the message is for whatever tool/agent
consumes get_codeblock's output, which has no use for our roadmap file names).

Doesn't fix the underlying gap — JSX children still aren't addressable landmarks. Just flags
it so an agent doesn't mistake "outline/query found nothing to break down here" for "there's
nothing here." Self-obsoleting: once JSX promotion ships, these blocks stop being flat and
this stops firing on its own — no flag to remove later.
"""

_JSX_TYPES = ('jsx_element', 'jsx_self_closing_element', 'jsx_fragment')


def _contains_jsx(root, start, end):
    """Any node of a JSX type overlapping [start, end] (1-based, inclusive)?"""
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type in _JSX_TYPES:
            s, e = n.start_row + 1, n.end_row + 1
            if s <= end and e >= start:
                return True
        stack.extend(n.children())
    return False


def flat_block_note(language, ext, lines, start, end, *, handler=None, depth=None, base_level=None):
    """None, or a one-line note: this TSX/JSX range is large, FLAT (no sub-landmarks
    anywhere in it), and actually contains JSX — almost certainly a big `return (...)`.

    `depth`/`base_level` — pass them in if the caller already computed them (the
    `--outline` branch in core.py always has); otherwise pass `handler` and this
    derives them itself via a scoped `handler.outline(...)` call (the `--query`/ladder
    path, which doesn't otherwise compute a depth tally)."""
    if language != 'tsx':  # core.py's lang_map sends BOTH .tsx and .jsx here as 'tsx'
        return None

    if depth is None or base_level is None:
        if handler is None or not hasattr(handler, 'outline'):
            return None
        rows = handler.outline(lines, max_level=None, deep=False, focus_line=start, focus_level=0)
        if not rows:
            return None
        levels = [r['level'] for r in rows if not r.get('filler')]
        if not levels:
            return None
        base_level, depth = min(levels), max(levels)

    if depth > base_level:
        return None  # real sub-structure exists somewhere in range — not flat, no note

    nonblank = sum(1 for ln in lines[start - 1:min(end, len(lines))] if ln.strip())
    from .escalate import _load_thresholds
    _, _, ceiling, _ = _load_thresholds()
    if nonblank <= ceiling:
        return None

    from .reader.registry import resolve
    backend, spec = resolve(ext)
    root = backend.root("".join(lines).encode("utf-8"))
    if not _contains_jsx(root, start, end):
        return None

    return (f"known limitation: JSX inside this block isn't broken into landmarks "
            f"(flat, {nonblank} non-blank line(s)) — use Read for exact content")
