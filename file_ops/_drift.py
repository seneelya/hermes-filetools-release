"""Line-drift ledger — a cheap predecessor of the block bookkeeper.

See __HQ/vision/Vision01__patch.md §4.1. NOT the staleness bookkeeper from
BRAINSTORM__read_patch.md §7: that one stores block CONTENT and answers "did
this block change since the model saw it" (needs a snapshot store). This one
stores a single number per file and answers a much narrower question: "how far
could a line hint have drifted since the model last saw real line numbers?"

Kept module-level (process-wide, per (task_id, resolved_path)) because that is
exactly the lifetime it needs: one running Hermes process editing files inside
one task. Losing it on a plugin reload is harmless by construction - the drift
budget just falls back to 0 and the search window returns to its base size, so
a patch that would have worked still works, it just gets a narrower window.
Contrast the real bookkeeper, where a lost snapshot means the question can't
be answered at all.
"""

from __future__ import annotations

# Cap on how much accumulated drift may widen the search window. Without it a
# long chain of edits degenerates into a whole-file search, which is exactly
# the ambiguity `line` exists to remove.
MAX_DRIFT = 400

_LEDGER: dict = {}


def _key(task_id: str, resolved_path: str) -> tuple:
    return (task_id or "default", resolved_path or "")


def note_edit(task_id: str, resolved_path: str, delta: int) -> None:
    """Record that an edit changed this file's line count by *delta*.

    Accumulates the ABSOLUTE value: a +3 followed by a -3 does not mean line
    hints are trustworthy again. Both edits moved things around, and the model
    may hold an orientation from before either of them.
    """
    k = _key(task_id, resolved_path)
    _LEDGER[k] = min(_LEDGER.get(k, 0) + abs(int(delta or 0)), MAX_DRIFT)


def drift_for(task_id: str, resolved_path: str) -> int:
    """Accumulated drift since this file was last read."""
    return _LEDGER.get(_key(task_id, resolved_path), 0)


def reset(task_id: str, resolved_path: str) -> None:
    """A successful read_file of this path: the model just saw the truth, so
    every line number it now holds is current. Budget goes back to zero."""
    _LEDGER.pop(_key(task_id, resolved_path), None)
