"""get_codeblock for the plugin: the vendored copy in `vendor/get_codeblock/` (Plan30).

The tool is ours but lives in its own repo; the plugin carries a copy of its package
(`vendor/sync_get_codeblock.py` refreshes it, never edit it here). Its modules import each
other absolutely (`from get_codeblock.core import ...`), so the copy is reached as the
top-level package `get_codeblock`: its parent dir goes on `sys.path` once, at the first
call, not at plugin load — a read that never asks for blocks never pays for the import.

Grammars (tree-sitter-*) come from `pip_dependencies` in plugin.yaml, installed by Hermes'
plugin installer. Without them `.py`, `.md` and `.txt` still work; other formats raise
`EnvError` from `open_source`, and the caller falls back to a plain read.
"""
import sys
from pathlib import Path

VENDOR = Path(__file__).resolve().parent.parent / "vendor"


def views():
    """The vendored `get_codeblock.views` module.

    `append`, not `insert`: the copy must never shadow anything of the gateway's. If some
    OTHER `get_codeblock` got imported first (a dev checkout on the path), refuse instead of
    silently running a different version of the tool than the one the plugin ships."""
    v = str(VENDOR)
    if v not in sys.path:
        sys.path.append(v)
    from get_codeblock import views as mod
    here = Path(mod.__file__).resolve()
    if VENDOR not in here.parents:
        raise ImportError(f"get_codeblock imported from {here}, not from the plugin's vendor copy {VENDOR}")
    return mod
