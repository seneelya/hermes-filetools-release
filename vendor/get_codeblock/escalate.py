"""Vision05 — query-escalation: expand a too-small/uninformative `--query` result by
rewriting the CALL PARAMETERS (`--line`/`--level` arrays) before they reach the existing,
already-tested batch-query resolve/render path (`views._resolve_query_runs` /
`views.render_query`). Adds no new rendering path and no new engine — only decides
WHICH `--line`/`--level` combo to hand to the existing ones.

Design (see `__dev/vision/Vision05__get_codeblock.md` for the full rationale):
- Resolve-once for the common case: `views.query_view` resolves the ORIGINAL anchors exactly
  once (it would have anyway, escalation or not) and passes those `runs` in here — this
  module only pays for MORE resolves (probing neighbors) when that seed turns out too
  small, never re-derives what's already been computed.
- Horizontal by default: grab SIBLING blocks at the same (deepest-resolved) level, left and
  right in file order, instead of climbing to a parent — a vertical climb can jump into a
  giant enclosing block disproportionately (a `try` inside a 1000-line method).
- Applies to explicit multi-anchor batches too: every anchor first resolves to its own
  absolute level; the REFERENCE level for sibling-gathering is the MAX (deepest) among them.
  A single anchor is just the n=1 case of the same rule.
- Never touches the importable `get_codeblock()` API — this lives only in the CLI dispatch
  path (`views.query_view`), same boundary Vision04 draws between "core raises structure" and
  "policy decides what to show".

KNOWN, DEFERRED cost: every `handler.get_blocks()`/`line_level()` call re-reads and
re-parses the whole file from scratch (no shared parse-tree cache across calls — true
throughout `reader/address.py` and `reader/classify.py`, not just here). Measured:
~0.8ms/call on a 27-line file, ~75ms/call on a 4747-line file. This module's neighbor
probes each cost one such call, on top of whatever `_resolve_query_runs` already paid.
A proper fix (in-process parse-tree cache keyed by path, shared across all ~7 entry
points in `address.py`+`classify.py`) is a deliberate LATER, separate pass — not folded
into Vision05 to keep this change reviewable. Vision05 only removes the ONE redundant
resolve this module used to do on top of that pre-existing cost (see "Resolve-once"
above); it doesn't fix the underlying no-cache architecture.
"""


def _load_thresholds():
    """(FLOOR, TARGET, CEILING, K) from CONFIG__TOOLS.py, or hardcoded defaults if the
    file/names are missing — same optional-config pattern as `core.load_config()`."""
    try:
        from CONFIG__TOOLS import (ESCALATE_FLOOR, ESCALATE_TARGET,
                                    ESCALATE_CEILING, ESCALATE_K)
        return ESCALATE_FLOOR, ESCALATE_TARGET, ESCALATE_CEILING, ESCALATE_K
    except ImportError:
        return 12, 18, 40, 1.5


def _nonblank_count(lines, start, end):
    """Non-blank line count in `[start, end]` (1-based, inclusive) — the size measure
    the FLOOR/TARGET/CEILING thresholds are defined against (blank padding lines
    shouldn't count as "informative content")."""
    last = min(end, len(lines))
    return sum(1 for ln in lines[start - 1:last] if ln.strip())


def _cost(n, target, k):
    """Asymmetric quadratic: undershooting `target` costs `k`x as much as overshooting
    by the same amount — between two options equally far from target, prefer the one
    that reached/passed it over the one that fell short."""
    d = n - target
    return (d * d) if n >= target else (d * d * k)


def maybe_escalate(handler, resolve_fn, file_path, lines, line_nums, levels, runs):
    """Returns (new_line_nums, new_levels, note). `note` is None when nothing changed
    (already informative, or no neighbor could help). Caller (`views.query_view`) is expected
    to skip calling this entirely when `--force` was given — this function doesn't
    know about the flag, it only ever escalates or doesn't based on `runs`' size.

    `runs` — the ALREADY-RESOLVED merged ranges from a prior `_resolve_query_runs`
    pass over `line_nums`/`levels` (each `{start, end, parts: [block, ...]}`, per
    `views._resolve_query_runs`). Reusing them means the common (non-escalating) call
    pays for exactly the ONE resolve it would have paid for anyway — this function
    only does NEW work (probing neighbors) when the seed is actually too small,
    instead of re-resolving the original anchors from scratch just to measure them.

    `handler`/`resolve_fn` are the SAME `get_blocks`/`resolve` primitives `views.py`
    already uses for query batches — no new addressing engine, just reuse."""
    FLOOR, TARGET, CEILING, K = _load_thresholds()

    if not runs:
        # nothing resolved (every requested line was out of range / errored) -> nothing to
        # grow; the caller reports the per-line errors. Was: max() of an empty sequence.
        return line_nums, levels, None

    seed_total = sum(_nonblank_count(lines, r['start'], r['end']) for r in runs)
    if seed_total >= FLOOR:
        return line_nums, levels, None

    ref_level = max(p['level'] for r in runs for p in r['parts'])
    cur_lo = min(r['start'] for r in runs)
    cur_hi = max(r['end'] for r in runs)
    cur_total = seed_total

    def _next_nonblank(idx_1based, step):
        """First non-blank line at/after (step=+1) or at/before (step=-1)
        `idx_1based`, or None — probing a blank line falls to the honest
        file-scope fallback (invariant #7), never the real next/prev sibling."""
        i = idx_1based
        while 1 <= i <= len(lines):
            if lines[i - 1].strip():
                return i
            i += step
        return None

    def _next_right(hi):
        probe = _next_nonblank(hi + 1, +1)
        if probe is None:
            return None
        blocks = handler.get_blocks(file_path, probe)
        if not blocks:
            return None
        cand = resolve_fn(blocks, ref_level)
        if not cand or cand['level'] != ref_level or cand['start'] <= hi:
            return None
        return cand

    def _next_left(lo):
        probe = _next_nonblank(lo - 1, -1)
        if probe is None:
            return None
        blocks = handler.get_blocks(file_path, probe)
        if not blocks:
            return None
        cand = resolve_fn(blocks, ref_level)
        if not cand or cand['level'] != ref_level or cand['end'] >= lo:
            return None
        return cand

    best_cost = _cost(cur_total, TARGET, K)
    best = {'lo': cur_lo, 'hi': cur_hi, 'left': [], 'right': []}
    added_left, added_right = [], []

    while True:
        right = _next_right(cur_hi)
        left = _next_left(cur_lo)
        candidates = [(s, c) for s, c in (('right', right), ('left', left)) if c is not None]
        if not candidates:
            break
        # Smaller neighbor first — gentler growth, less likely to overshoot in one jump.
        side, cand = min(candidates, key=lambda sc: _nonblank_count(lines, sc[1]['start'], sc[1]['end']))
        add_n = _nonblank_count(lines, cand['start'], cand['end'])
        new_total = cur_total + add_n
        if new_total > CEILING:
            break
        if side == 'right':
            cur_hi = cand['end']
            added_right.append(cand)
        else:
            cur_lo = cand['start']
            added_left.append(cand)
        cur_total = new_total
        cost = _cost(cur_total, TARGET, K)
        if cost <= best_cost:
            best_cost = cost
            best = {'lo': cur_lo, 'hi': cur_hi, 'left': list(added_left), 'right': list(added_right)}

    if not best['left'] and not best['right']:
        return line_nums, levels, None

    new_lines = list(line_nums)
    new_levels = list(levels)
    for cand in best['left'] + best['right']:
        new_lines.append(cand['start'])
        new_levels.append(cand['level'])

    old_desc = ",".join(str(x) for x in line_nums)
    new_desc = ",".join(str(x) for x in new_lines)
    note = (f"parameters escalated: --line {old_desc} -> --line {new_desc} "
            f"(result was {seed_total} non-blank line(s), below the informative floor)")
    return new_lines, new_levels, note
