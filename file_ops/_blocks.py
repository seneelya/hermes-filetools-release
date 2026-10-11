"""read_file by blocks (Plan30): the bridge between `read_file` and the vendored get_codeblock.

`read_file` reads through ITS backend (local or docker) and decodes the bytes itself (cp1251,
guessed encodings...), while get_codeblock parses a FILE ON DISK as utf-8. So the tool never
sees the user's file: `parsed()` writes the text `read_file` already holds into a scratch copy
with the same extension, and every view runs on that copy — the same lines, numbered the same,
whatever the backend or the encoding. `\\r` is dropped from the copy: get_codeblock reads text
in universal-newline mode, where a lone `\\r` would split a line and shift every number below.

Nothing here is required for a read to work: `parsed()` yields None when the format has no
reader, a grammar is missing, or anything at all goes wrong, and the caller reads plain lines.
"""
import contextlib
import os
import threading
import uuid
from pathlib import Path

try:  # plugin mode
    from . import _gcb
    from . import _ledger
except ImportError:  # console mode
    import _gcb  # type: ignore
    import _ledger  # type: ignore


class Unavailable(Exception):
    """Why a file cannot be read by blocks: its text is what `--get-codeblock` refuses with."""


@contextlib.contextmanager
def parsed(lines, path):
    """Yield `(views, src)` for `lines` as the file `path`; raise `Unavailable` if it can't be.

    `src.display` is the real path, so a map names the user's file, not the copy."""
    try:
        views = _gcb.views()
    except Exception as e:
        raise Unavailable(f"the block reader could not be loaded ({e})") from None
    ext = Path(path).suffix.lower()
    copy = _ledger._home_module().scratch_dir("blocks") / (
        f"{os.getpid()}_{threading.get_ident()}_{uuid.uuid4().hex[:8]}{ext}")
    try:
        copy.write_text("".join(l.replace("\r", "") + "\n" for l in lines),
                        encoding="utf-8", newline="\n")
        try:
            src = views.open_source(str(copy), display=path)
        except views.UnsupportedFormat:
            raise Unavailable(f"'{ext or '(no extension)'}' is not a block format") from None
        except Exception as e:
            if type(e).__name__ == "EnvError":
                raise Unavailable(f"the grammar for '{ext}' files is not installed") from None
            raise Unavailable(str(e)) from None
        yield views, src
    finally:
        with contextlib.suppress(OSError):
            copy.unlink()


def plain_ranges(starts, count, total):
    """`--line a,b --count N` without alignment: N lines from each start, clipped to the file."""
    return [{"from": s, "to": min(total, s + count - 1), "cut": None} for s in starts]


def aligned_ranges(views, src, starts, count):
    """The same ranges with both ends snapped to blocks (`views.block_range`)."""
    return [views.block_range(src, s, count) for s in starts]


def merge(ranges):
    """Sort and merge ranges that overlap or touch: one range of the file is shown once, and
    two adjacent ones as one — a seam between them would be a seam the file does not have.
    `cut` stays with whichever range supplies the merged end."""
    out = []
    for r in sorted(ranges, key=lambda r: (r["from"], -r["to"])):
        if out and r["from"] <= out[-1]["to"] + 1:
            if r["to"] > out[-1]["to"]:
                out[-1]["to"], out[-1]["cut"] = r["to"], r["cut"]
        else:
            out.append(dict(r))
    return out


def count_tokens(text):
    """Our token estimate (`context_ctl/_tokens.count_ours`, the one the panel weighs chips
    with); a crude chars/3 if that module can't be reached — the limit must never break a read."""
    try:
        try:
            from ..context_ctl import _tokens
        except ImportError:
            import importlib.util
            import sys
            _tokens = sys.modules.get("hermes_filetools_tokens")
            if _tokens is None:
                p = Path(__file__).resolve().parents[1] / "context_ctl" / "_tokens.py"
                spec = importlib.util.spec_from_file_location("hermes_filetools_tokens", p)
                _tokens = importlib.util.module_from_spec(spec)
                sys.modules["hermes_filetools_tokens"] = _tokens
                spec.loader.exec_module(_tokens)
        return _tokens.count_ours(text)
    except Exception:
        return len(text) // 3 + 1
