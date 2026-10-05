"""Test bootstrap for the hermes-filetools integration tests.

Run from anywhere: `python -m pytest /project/hermes-filetools/tests`

The project root IS the plugin directory (name ``hermes-filetools`` — hyphen, legal for
Hermes' path-based plugin loader, illegal as a Python identifier). For tests we bind it to
the module name ``hermes_filetools`` via importlib so package-relative imports inside it
(including into feature-group subpackages like ``file_ops/``) resolve.

Core lives at HERMES_SRC (default /workspace/SRC/hermes-agent-src); override via env.

HERMETICITY: core's model_tools.py runs discover_plugins() AT IMPORT TIME, and once this
plugin is enabled in the REAL ~/.hermes config every test process would auto-load a SECOND
module instance from /root/.hermes/plugins (two module objects → registry/identity confusion).
Live Hermes has exactly one instance; tests must too. So HERMES_HOME is pointed at an EMPTY
temp dir BEFORE anything imports core — user-plugin discovery finds nothing there. Tests that
need a real home monkeypatch pl.get_hermes_home themselves and are unaffected by this default.
"""
import importlib.util
import os
import sys
import tempfile

HERMES_SRC = os.environ.get("HERMES_SRC", "/workspace/SRC/hermes-agent-src")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))            # tests/
PROJECT_ROOT = os.path.dirname(TESTS_DIR)                          # hermes-filetools (project == plugin root)
PLUGIN_DIR = PROJECT_ROOT                                          # the plugin itself

# Throwaway files: `_home.scratch_dir` (RAM disk, else plugin scratch). Never AppData Temp.
# Must run before mkdtemp / pytest tmp_path.
sys.path.insert(0, PROJECT_ROOT)
from _home import use_scratch_as_temp  # noqa: E402
use_scratch_as_temp()

# --- hermetic home (must precede any core import) ---
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="filetools-test-home-")

# --- hermetic terminal backend (2026-09-30) ---
# The gateway bridges its `terminal.*` config into TERMINAL_* env vars (their
# gateway/run.py::_bridge_terminal_config_to_env). The panel's Full check runs these tests as a
# CHILD of the gateway, so they inherited e.g. TERMINAL_ENV=docker + TERMINAL_CWD=/workspace:
# the file backend then treated Windows absolute paths as relative and glued them onto the cwd
# ("File not found: Y:\...\hermes-filetools/Y:\...\x.py", 32 failures live). The tests assume
# the LOCAL backend - drop whatever the parent process configured.
for _key in [k for k in os.environ if k.startswith("TERMINAL_")]:
    del os.environ[_key]


def _load_plugin_package() -> None:
    """Bind the hyphenated plugin dir as module ``hermes_filetools``."""
    if "hermes_filetools" in sys.modules:
        return
    init_path = os.path.join(PLUGIN_DIR, "__init__.py")
    spec = importlib.util.spec_from_file_location(
        "hermes_filetools",
        init_path,
        submodule_search_locations=[PLUGIN_DIR],  # makes `from . import view_file_core` resolve
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hermes_filetools"] = mod
    spec.loader.exec_module(mod)


if os.path.isdir(HERMES_SRC):
    sys.path.insert(0, HERMES_SRC)

_load_plugin_package()


# --- their session loader (Plan26) ----------------------------------------
# After our write the gateway memory is re-read by THEIR loader
# (`get_messages_as_conversation(..., repair_alternation=True, include_row_ids=True)`), not built
# by us. Fake databases in the tests answer it with this: table-only columns dropped, empty columns
# dropped, and the two marks their loader stamps on every message - `_row_id` and `_db_persisted`.
_TABLE_ONLY = ("id", "session_id", "active", "compacted", "observed", "token_count",
               "display_identity", "display_order", "platform_message_id", "_row_id")


def their_loader(rows) -> list:
    """What `get_messages_as_conversation(..., include_row_ids=True)` would give for `rows`."""
    out = []
    for row in rows:
        msg = {k: v for k, v in dict(row).items() if k not in _TABLE_ONLY and v is not None}
        row_id = row.get("id", row.get("_row_id"))
        if row_id is not None:
            msg["_row_id"] = row_id
        msg["_db_persisted"] = True
        out.append(msg)
    return out


# --- operation addresses (Plan09) ---------------------------------------
# Every one of our tools now leads its answer with `Operation ID: <hex>[<FileTag>]`.
# Tests that assert on the WHOLE rendered text need to take that line off
# first, and they must not hardcode the number: the counter is per session and
# accumulates across tests sharing this process's temp home.
import re as _re

# The address is metadata, so it may wear a comment (`◆`, `#`, `<!-- … -->`): the wrapper is
# allowed and `split_op_line` hands back the bare `Operation ID: <hex>` for comparisons.
OP_LINE_RE = _re.compile(r"^[^\w\s]{0,4}\s?(Operation ID: [0-9a-f]{4,}(?:x[0-9a-f]{2,4})?)(?:\s?(?:-->|\*/))?$")


def split_op_line(text: str) -> tuple:
    """`(op_line, rest)` — and it ASSERTS the address is the first line.

    Deliberately strict about position: the whole point of the address is
    being readable without reading the rest of the answer, so "somewhere in
    the header" is not the contract.
    """
    first, _, rest = (text or "").partition("\n")
    found = OP_LINE_RE.match(first.strip())
    assert found, f"no Operation ID on the first line: {first!r}"
    return found.group(1), rest
