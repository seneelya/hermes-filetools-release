"""Which lines of a nested read actually reached the model (`deferred/17`).

A read made by a script inside `execute_code` hands its text to the SCRIPT, not to the model.
What reaches the model is only the script's output — sometimes the file printed through,
often one number (`12` dots counted). Treating every nested read as "the model saw lines
from..to" made a later dot count shadow an earlier explicit `read_file` as 100% stale.

This module answers the question from the two texts we hold at the end of `execute_code`:
the lines each nested read returned, and the answer the model received. A file line counts
as seen when an output line ends with it (so `12: text` and `enumerate` prints match too),
and only inside a run of at least `MIN_RUN` non-blank lines in the same order. Shorter runs
are what a search prints (`grep`-like hits), and counting them would bring the false
shadowing back through another door (owner, 2026-10-05: «меньше чем 10 строк отрывки не
считать»).

Blank file lines neither break a run nor count towards it: a script that skips them while
printing still printed the file.
"""

from __future__ import annotations

MIN_RUN = 10

# A line repeated this often in one page (`}`, `end`, `return None`) gives that many start
# candidates per output line; past the cap we stop trying starts for it. A real run starting
# on such a line is still found from its second line on.
_MAX_STARTS = 64


def seen_ranges(start: int, lines, out: str, min_run: int = MIN_RUN) -> list:
    """`[[from, to], …]` — file line numbers of `lines` (first one is `start`) found in `out`.

    Never raises: a failure here must not cost the ledger its entry, so the caller gets
    `[]` (nothing proven seen) instead.
    """
    try:
        return _seen_ranges(int(start), list(lines or []), str(out or ""), int(min_run))
    except Exception:  # noqa: BLE001 - evidence, not the operation
        return []


def _seen_ranges(start: int, lines: list, out: str, min_run: int) -> list:
    if start < 1 or not lines or not out:
        return []
    file_n = [str(s).strip() for s in lines]
    out_n = [s.strip() for s in out.split("\n")]

    index: dict = {}
    for i, s in enumerate(file_n):
        if s:
            index.setdefault(s, []).append(i)

    covered = [False] * len(file_n)
    ranges = []
    for j, o in enumerate(out_n):
        if not o:
            continue
        for i in _candidates(o, index):
            if covered[i]:
                continue
            first, last, count = _extend(file_n, out_n, i, j)
            if count >= min_run:
                for k in range(first, last + 1):
                    covered[k] = True
                ranges.append([start + first, start + last])
    return _merge(ranges)


def _candidates(o: str, index: dict) -> list:
    """File indices whose line `o` could be: exact, or `o` ends with it (a printed prefix)."""
    hits = list(index.get(o, ()))
    for s, idx in index.items():
        if s != o and o.endswith(s) and len(hits) < _MAX_STARTS:
            hits.extend(idx)
    return sorted(set(hits))[:_MAX_STARTS]


def _extend(file_n: list, out_n: list, i: int, j: int) -> tuple:
    """Walk forward from the matched pair (i, j). Returns (first, last, non-blank matched)."""
    first = last = i
    count = 0
    while i < len(file_n) and j < len(out_n):
        f, o = file_n[i], out_n[j]
        if not f:
            # Blank file line: consumes a blank output line if there is one, never counts.
            i += 1
            if not o:
                j += 1
            continue
        if not o:
            j += 1
            continue
        if o != f and not o.endswith(f):
            break
        last = i
        count += 1
        i += 1
        j += 1
    return first, last, count


def _merge(ranges: list) -> list:
    out = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out
