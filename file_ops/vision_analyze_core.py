#!/usr/bin/env python3
"""vision_analyze — override of Hermes' own built-in `vision_analyze` tool.

Design: `__HQ/plans/Plan05__vision-analyze-path.md`. We do NOT reimplement
anything about vision: the provider calls, the native-vs-aux routing, the
resize/crop pipeline, the format normalisation all stay in hermes-agent's own
`tools.vision_tools._handle_vision_analyze()`, imported and called live. This
module fixes exactly ONE thing — that upstream cannot read a file out of the
sandbox on a Windows host — and reformats the string answer.

THE BUG WE ARE AROUND (root-caused and reproduced 2026-09-02)
-------------------------------------------------------------
`tools/image_source.py:123` parses the model's path with the HOST's pathlib:

    p = Path(os.path.expanduser(candidate))

On a Windows host `Path("/workspace/temp/x.jpg")` is a `WindowsPath`, and
`str(p)` flips every separator. That string is then shell-quoted into a command
run INSIDE the Linux container (`image_source.py:354`):

    head -c 52428801 < '\\workspace\\temp\\x.jpg' | base64

which of course finds nothing. Worse, the failure is MISREPORTED: `head` fails,
the backend still returns rc==0 with its error text in `output`, and
`base64.b64decode(output, validate=True)` rejects that text, so
`image_source.py:378` raises `NotAnImage("sandbox returned non-image data")`.
A path problem presented as a content-type problem — which is precisely what
drove the model into hand-rolling base64 data URLs instead of reporting a bad
path. Invisible on a Linux host; it only bites the Windows-host + Linux-
container combination, i.e. exactly this deployment.

WHY WE DO NOT JUST HAND THEM A NICER PATH
-----------------------------------------
There is no path string that survives. Every non-`data:`, non-`http(s)` source
in `resolve_image_source` goes through that one `Path()` call, and a
`WindowsPath` normalises `/` to `\\` unconditionally. Plan05's route (a) is
therefore not merely unattractive, it is impossible — established by reading
the branches, not guessed.

So we take route (b), but WITHOUT the cost Plan05 feared. We do not shell out
and base64 by hand: we read the bytes through the seam this plugin already
owns and tests, `file_ops/file_backend.py`. That backend resolves paths with
hermes-agent's OWN `_resolve_path_for_task`, which — unlike `image_source.py`
— checks `_uses_container_paths(task_id)` and returns a `PurePosixPath` for a
container env (`tools/file_tools.py:373-389`). That one call is the fix
upstream is missing, and it is already wired into every file tool we ship. The
bytes then come back via `read_file_bytes`, which execs inside the same
environment the built-in file tools use.

Upstream's own `data:` branch has no pathlib in it at all, so a data URL is the
one input shape that cannot be mangled. We hand them that.

WHAT WE DELIBERATELY DO NOT TOUCH
---------------------------------
- A `data:` or `http(s)://` source is passed straight through. Nothing is
  broken about those paths and there is nothing for us to fix.
- Any other explicit scheme is also passed through, so upstream's own
  `UnsupportedScheme` message is what the model sees.
- A DICT return is passed through UNTOUCHED. When the main model has native
  vision, `_handle_vision_analyze` returns a `_multimodal` envelope dict, not
  text — the agent loop unwraps it and provider adapters serialize it per
  backend. Rendering that would destroy the entire native-vision path. Only a
  STRING return is JSON we should reformat.
"""

import os

try:
    from ._tool_text import render_json_result
    from . import _ledger
    from .file_backend import FileBackend
except ImportError:  # console/dev mode - file_ops/ is on sys.path, not a package
    from _tool_text import render_json_result  # type: ignore
    import _ledger  # type: ignore
    from file_backend import FileBackend  # type: ignore

_LABELS = {
    "success": "Success",
    "error": "Error",
    "scale_note": "Scale note",
    "analysis": "Analysis",  # only ever a label if it is NOT the body
}

_HEAD_KEYS = ("success", "error")

# Extension -> declared mime. Only a HINT: upstream's `_finalize` verifies the
# real type from the magic bytes and that verification is what we want to keep
# intact - a genuinely non-image file must still fail as a content-type error,
# not be smuggled past by our declaration.
_MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".svg": "image/svg+xml", ".tif": "image/tiff", ".tiff": "image/tiff",
    ".ico": "image/x-icon", ".avif": "image/avif", ".heic": "image/heic",
}

# Fallback only. Reused from upstream when importable so the two cannot drift:
# a data URL we build is about to be handed straight to their ingest check.
_FALLBACK_MAX_BYTES = 50 * 1024 * 1024


def _max_bytes() -> int:
    try:
        from tools.image_source import _MAX_INGEST_BYTES
        return int(_MAX_INGEST_BYTES)
    except Exception:
        return _FALLBACK_MAX_BYTES


def is_path_source(src) -> bool:
    """True when `src` is a filesystem path we should read ourselves.

    Mirrors upstream's own dispatch order (`resolve_image_source`): `data:` and
    `http(s)://` are theirs and work; any OTHER explicit scheme is theirs to
    refuse with its own message. `file://` counts as a path - it is stripped
    and parsed by the same broken `Path()` call.
    """
    if not isinstance(src, str) or not src.strip():
        return False
    s = src.strip()
    low = s.lower()
    if low.startswith(("data:", "http://", "https://")):
        return False
    if low.startswith("file://"):
        return True
    scheme, sep, _ = s.partition("://")
    if sep and scheme and scheme[0].isalpha() and all(
            c.isalnum() or c in "+.-" for c in scheme):
        return False  # some other scheme - upstream refuses it, and should
    return True


def path_to_data_url(src: str, task_id: str = "default", max_bytes: int = None,
                     backend=None):
    """`(data_url, error)` — exactly one of the two is not None.

    The error message is the OTHER half of this module's purpose. Upstream
    turned "cannot read that path" into "sandbox returned non-image data", and
    that lie cost the model several turns. Ours says which path, in whose
    filesystem view, and that it could not be READ - a content-type complaint
    stays reserved for a file we actually read and upstream then rejected.
    """
    path = src.strip()
    if path.lower().startswith("file://"):
        path = path[len("file://"):]

    if backend is None:
        try:
            from . import mocks  # type: ignore
        except ImportError:
            import mocks  # type: ignore
        backend = FileBackend(task_id, mock_ops=mocks.file_ops,
                              mock_resolve=mocks.resolve_path_for_tool)

    try:
        resolved = backend.resolve(path)
    except Exception as exc:
        return None, (f"ERROR: vision_analyze: could not resolve image path "
                      f"'{path}' ({exc}).")

    try:
        result = backend.ops.read_file_bytes(
            resolved, max_bytes=_max_bytes() if max_bytes is None else max_bytes)
    except Exception as exc:
        return None, (f"ERROR: vision_analyze: could not read image file "
                      f"'{resolved}' ({exc}).")

    error = getattr(result, "error", None)
    if error:
        return None, (f"ERROR: vision_analyze: could not read image file "
                      f"'{resolved}' — {error}. This is a PATH/READ problem, "
                      f"not an image-format problem: check the path exists in "
                      f"the environment your other file tools see. Do NOT "
                      f"build a base64 data URL by hand.")

    encoded = getattr(result, "base64_content", None)
    if not encoded:
        return None, (f"ERROR: vision_analyze: read '{resolved}' but got no "
                      f"content back from the file backend.")

    ext = os.path.splitext(str(resolved))[1].lower()
    mime = _MIME_BY_EXT.get(ext, "application/octet-stream")
    return f"data:{mime};base64,{encoded}", None


def render_vision_result(raw: str, symbols: dict = None, op: str = "") -> str:
    """JSON string (their contract) -> plain text (ours). The vision-shaped
    configuration of `render_json_result`; the mechanics are all its."""
    return render_json_result(
        raw,
        labels=_LABELS,
        body_keys=("analysis",),
        head_keys=_HEAD_KEYS,
        symbols=symbols,
        op=op,
    )


async def vision_analyze_tool_new(args: dict, symbols: dict = None, **kwargs):
    """Swap a filesystem path for a data URL, then call their live handler.

    Returns whatever they return when it is not a string (the `_multimodal`
    envelope on the native-vision fast path) — see the module docstring.
    """
    from tools.vision_tools import _handle_vision_analyze

    args = dict(args or {})
    src = args.get("image_url")
    if is_path_source(src):
        data_url, error = path_to_data_url(
            src, task_id=kwargs.get("task_id") or "default")
        if error:
            return error
        args["image_url"] = data_url

    # Asked for AFTER the path swap, so a call that fails on its own image
    # (`error` above) spends no address: an address is for an operation the
    # model can be pointed back at, and that one never reached the model as a
    # rendered answer at all.
    op = _ledger.next_op(session=kwargs.get("session_id"),
                         task_id=kwargs.get("task_id") or "default")

    raw = await _handle_vision_analyze(args, **kwargs)
    if not isinstance(raw, str):
        # Their native-vision fast path returns a `_multimodal` envelope, not
        # our text - nothing of ours to put a header line on. The address is
        # still recorded, so a gap in the sequence is explainable rather than
        # mysterious.
        _note("", op, kwargs)
        return raw
    out = render_vision_result(raw, symbols=symbols, op=op)
    _note(out, op, kwargs)
    return out


def _note(out: str, op: str, kwargs: dict) -> None:
    """Record the operation, and under no circumstances affect the answer.

    Second guard over `_ledger`'s own fail-open (same as the file cores keep):
    a ledger module that failed to import or changed shape must not be able to
    turn a working call into an error string.
    """
    try:
        _ledger.note_op(tool='vision_analyze', text=out,
                        session=kwargs.get("session_id") or "",
                        task_id=kwargs.get("task_id") or "default", op=op)
    except Exception:
        pass
