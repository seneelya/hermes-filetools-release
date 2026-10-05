"""hermes-filetools — top-level plugin entry point.

This is the plugin root: the whole repo folder IS the Hermes plugin (not a
subfolder of it). Tool operations live in feature-group subpackages, each a
self-contained folder; this file only wires groups into the registry.

Groups today:
    file_ops/   — registers as read_file / write_file / patch / search_files /
                  terminal, OVERRIDING the built-ins (decision 2026-08-25: core's
                  is_deferrable_tool_name() exempts tools by exact NAME
                  against a hardcoded _HERMES_CORE_TOOLS list in
                  toolsets.py, regardless of which plugin/toolset actually
                  registered them and regardless of schema size - only
                  literally being one of those names avoids Hermes'
                  progressive-disclosure tool_search/tool_describe/tool_call
                  bridge, which can drop a plugin tool to name-only or even
                  fully-invisible-until-searched under a tight context
                  budget. There is no config-level opt-out for a plugin's
                  own tools otherwise. Internal module/function names below
                  (view_file_core.py, view_file_tool(), etc.) are UNCHANGED -
                  only the externally-registered tool name changed.

Groups planned (not yet merged in — still standalone/untested elsewhere):
    self_delegate/, self_undo/

"Two-in-one" per group: every tool is the SAME core function callable two ways:
  1. as a Hermes plugin tool (this register(ctx), via ctx.register_tool);
  2. from the console without Hermes, e.g.:
         python file_ops/view_file_core.py myfile.txt --limit 50
         python file_ops/create_file_core.py out.txt --content "hi"
         python file_ops/modify_file_core.py f.txt -o old -n new
         python file_ops/sift_files_core.py "TODO" --target content

Enable in Hermes:
    hermes plugins enable hermes-filetools
Also requires (since this overrides built-ins):
    plugins:
      entries:
        hermes-filetools:
          allow_tool_override: true

Per-tool config (2026-08-25 foundation, 2026-08-26 moved into plugin.yaml) —
THREE layers, each overriding the one before:
  1. _FALLBACK_SLOT_DEFAULTS below (code) - only used for a slot plugin.yaml
     doesn't declare in its own `tools:` section (today: patch only -
     read_file/write_file/search_files are all declared there now).
  2. plugin.yaml's own `tools:` section (this repo's actual default values -
     today: read_file, write_file) - see _load_manifest_tools_defaults().
  3. the running profile's plugins.entries.hermes-filetools.settings.tools.
     <slot>, read via ctx.get_config("tools") - see plugin.yaml's
     config_schema for the documented shape.
Each slot supports: enabled (false = don't register at all), path (old|new -
"new" is a real, implemented alternate path for read_file/write_file/
search_files, see __HQ/vision/Vision01__read.md, Vision01__write.md,
Vision01__search.md; defaults to "old" for search_files until live-verified,
same phased rollout read/write went through; patch still falls back to "old",
no implementation yet), schema (which
file_ops/*.yaml to load - optional, auto-derived from `path` when omitted,
see _SCHEMA_BY_PATH; the REGISTERED tool name always comes from the loaded
file's own `name:` field, not the slot key, so an explicit schema pointing at
a differently-named file registers a second, parallel, non-overriding tool),
params (slot-specific extra parameters - currently just `mtime`, see
plugin.yaml's config_schema).
(standalone plugin, discovered from $HERMES_HOME/plugins/hermes-filetools/)
"""

import json

# file_ops group: core modules + schema loader + mock infrastructure.
# Plugin mode: imported as package `hermes_plugins.<slug>` -> relative imports.
# Console/dev mode (package dir on sys.path): bare module imports.
try:  # plugin mode
    from .file_ops import view_file_core, create_file_core, modify_file_core, sift_files_core
    from .file_ops import (view_file_new_core, create_file_new_core, sift_files_new_core,
                           modify_file_new_core, terminal_core, execute_code_core,
                           process_core, vision_analyze_core)
    from .file_ops.mocks import tool_error
    from .file_ops._schema_loader import load_schema
    from .file_ops import _vocab
    from .file_ops._new_format import DEFAULT_SYMBOLS
    from .file_ops.flags import has_flag
    from .file_ops._execute_code_bridge import patch_execute_code_bridge
    from .file_ops._tool_text import to_facade_dict
    from .file_ops.markers import strip_marker_lines, block_marker_lines
except ImportError:  # console/dev mode: put file_ops/ on sys.path
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "file_ops"))
    import view_file_core, create_file_core, modify_file_core, sift_files_core  # type: ignore
    import view_file_new_core, create_file_new_core, sift_files_new_core  # type: ignore
    import modify_file_new_core, terminal_core, execute_code_core  # type: ignore
    import process_core, vision_analyze_core  # type: ignore
    import mocks  # type: ignore
    from _schema_loader import load_schema  # type: ignore
    import _vocab  # type: ignore
    from _new_format import DEFAULT_SYMBOLS  # type: ignore
    from flags import has_flag  # type: ignore
    from _execute_code_bridge import patch_execute_code_bridge  # type: ignore
    from _tool_text import to_facade_dict  # type: ignore
    from markers import strip_marker_lines, block_marker_lines  # type: ignore

    tool_error = mocks.tool_error


# ---------------------------------------------------------------------------
# `flags` help text - the grammar itself is free-text (not enumerable via
# JSON schema), so each tool that accepts it exposes its own vocabulary this
# way: flags="--help" returns this instead of doing anything else.
# ---------------------------------------------------------------------------

_READ_FILE_FLAGS_HELP = """read_file `flags` (space-separated, values after a
space and never with "=", e.g. flags="--line 400 --count 200"):
  --line N           Start reading at line N (1-indexed, default 1).
  --count N          Read at most N lines from --line (default 2000, max
                     2000). --count 0 is its own mode: the header only (path,
                     size, encoding, FileTag) and no file content at all -
                     use it to check a file's size or encoding before reading.
  --numbered         Prefix every returned line with its absolute number
                     "N|". Off by default: the content then comes back
                     verbatim, and occasional "⚓ Line: N" signposts give you
                     numbers to work from without a number on every line.
  --force            Bypass binary/unknown-encoding refusal; read best-effort.
  --encoding NAME    Decode using exactly this codec (e.g. cp866, koi8-r) -
                     skips auto-detection and the binary/encoding refusal.
                     Use this when the auto-guess looks wrong.
  --help             Show this text instead of reading anything.
An unrecognised flag is refused by name - it is never silently ignored."""
# `--no-anchors` is deliberately NOT listed above, though it works (see
# view_file_new_core.py). Same treatment as search_files' --ignore-case
# (CONTRACT.md §15), different reason: ⚓ marks are a PASSIVE aid (§18), and a
# flag the model can toggle stops being passive - it turns into a decision to
# be made on every read. Live 2026-08-26: an agent saw the flag in the schema,
# applied it before the test even started, then had to reason about what the
# marks are and re-read. Operators still switch them off via
# plugin.yaml's params.anchors; the flag stays for the console CLI and tests.

_WRITE_FILE_FLAGS_HELP = """write_file `flags` (space-separated, values after a
space and never with "=", e.g. flags="--insert-line 40"):
  --insert-line N    Splice `content` INTO the file at this position instead
                     of overwriting the whole file - the way to add to a file
                     without reading it first. 0 = start of file (valid even
                     for a new/empty one), 1..N = insert AT that existing line
                     number, pushing it and everything below it down, -1 =
                     append at the end. Leave it out for a full overwrite.
  --force            By default, lines that exactly match read_file's own
                     tool-marker format for THIS file's type (its comment
                     prefix + File:/FileTag:/BLOCK/END/etc - e.g. "#" for
                     .py, the synthetic "◆" for .txt with no native
                     comment syntax - possibly copy-pasted by mistake from
                     a read_file response) are removed from `content`
                     before writing, and the response says how many. A
                     "⚓ Line N" signpost is handled slightly differently:
                     it becomes the blank line it was standing in for,
                     rather than vanishing, since that is what the file
                     actually had there. A
                     line with the right words but the WRONG comment
                     prefix for this file type is NOT recognized and is
                     written as plain content. --force skips this removal
                     entirely and writes `content` exactly as given - use
                     it to intentionally write text that documents or
                     quotes these markers.
  --help             Show this text instead of writing anything.
An unrecognised flag is refused by name and nothing is written."""

_SEARCH_FILES_FLAGS_HELP = """search_files `flags` (space-separated, values
after a space and never with "=", e.g. flags="--glob *.py --context 2"):
  --glob PATTERN     Filter by FILENAME (fnmatch: "*.py", "test_*") - not a
                     regex, unlike `pattern`. "." is a literal dot, so
                     "--glob .*" means hidden files, not "everything".
                     Without this flag every file is searched.
  --context N        Show N lines of context before/after each match.
  --limit N          Max matches in the response (default 50) - applies the
                     same way whether showing full matches, --files-only, or
                     --tally.
  --offset N         Skip the first N matches (pagination past a truncated
                     response - the response's own Hint: line gives you the
                     ready-to-use flags string).
  --files-only       List only the files that matched, no lines/content.
  --tally            List only each file with its match count, no lines.
  --help             Show this text instead of searching anything.
An unrecognised flag is refused by name - it is never silently ignored."""

_PATCH_FLAGS_HELP = """patch `flags` (space-separated, e.g. flags="--dry-run"):
  --dry-run          Resolve the edit and show exactly what would change -
                     which lines, and the text there right now - without
                     writing anything. Use it when you aren't sure `line` is
                     right, instead of spending a real edit to find out.
  --replace-all      Replace every occurrence in the file instead of the one
                     `line` points at. The match must be exact, and the line
                     hint is ignored (the response says so).
  --exact            Turn OFF the default tolerance for indentation/spacing
                     differences - old_string must match byte for byte.
  --context N        Lines of surrounding context in the echoed region
                     (default 3).
  --numbered         Echo the region with "N|" line numbers and mark changed
                     lines with an arrow. Off by default so the echoed text
                     can be copied straight back into another old_string.
  --help             Show this text instead of changing anything.
An unrecognised flag is refused by name and nothing is changed."""

TOOLSET = "filetools"


# ---------------------------------------------------------------------------
# Handlers — thin wrappers: validate, call core (returns dict or str),
# serialize to a JSON STRING (core contract: registry accepts str only).
# ---------------------------------------------------------------------------

def _as_int(value, default):
    """Coerce LLM-supplied ints that may arrive as strings; fall back on junk."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_json(result) -> str:
    """dict -> JSON string; strings pass through (tool_error() already returns one)."""
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False)


def _handle_view_file(args, **kw) -> str:
    path = args.get("path")
    if not path or not isinstance(path, str):
        return tool_error(
            "read_file: missing required field 'path'. Re-emit the tool call with a file path."
        )
    result = view_file_core.view_file_tool(
        path=path,
        offset=_as_int(args.get("offset"), 1),
        limit=_as_int(args.get("limit"), 2000),
        # base_dir=None -> in plugin mode the task's real env cwd is used (see FileBackend);
        # only a console --base-dir would anchor relative paths.
        task_id=kw.get("task_id", "default"),
    )
    return _to_json(result)


def _handle_create_file(args, **kw) -> str:
    path = args.get("path")
    if not path or not isinstance(path, str):
        return tool_error(
            "write_file: missing required field 'path'. Re-emit the tool call with "
            "both 'path' and 'content' set."
        )
    content = args.get("content")
    if content is None:
        return tool_error(
            "write_file: missing required field 'content'. The tool call included a "
            "path but no content argument — re-emit the tool call with the full content payload."
        )
    if not isinstance(content, str):
        return tool_error(
            f"write_file: 'content' must be a string, got {type(content).__name__}."
        )
    result = create_file_core.create_file_tool(path=path, content=content,
                                               task_id=kw.get("task_id", "default"))
    return _to_json(result)


def _handle_modify_file(args, **kw) -> str:
    path = args.get("path")
    old_string = args.get("old_string")
    new_string = args.get("new_string")
    if not path or not isinstance(path, str):
        return tool_error(
            "patch: missing required field 'path'. Re-emit the tool call with "
            "'path', 'old_string' and 'new_string' set."
        )
    if old_string is None or new_string is None:
        return tool_error(
            "patch: both 'old_string' and 'new_string' are required. "
            "Re-emit the tool call with all three fields set."
        )
    result = modify_file_core.modify_file_tool(
        path=path,
        old_string=old_string,
        new_string=new_string,
        replace_all=bool(args.get("replace_all", False)),
        task_id=kw.get("task_id", "default"),
    )
    return _to_json(result)


def _handle_sift_files(args, **kw) -> str:
    pattern = args.get("pattern")
    if not pattern or not isinstance(pattern, str):
        return tool_error(
            "search_files: missing required field 'pattern'. Re-emit the tool call with a search pattern."
        )
    target_map = {"grep": "content", "find": "files"}  # legacy aliases (core convention)
    raw_target = args.get("target", "content")
    target = target_map.get(raw_target, raw_target)
    result = sift_files_core.sift_files_tool(
        pattern=pattern,
        target=target,
        path=args.get("path", "."),
        file_glob=args.get("file_glob"),
        limit=_as_int(args.get("limit"), 50),
        offset=_as_int(args.get("offset"), 0),
        output_mode=args.get("output_mode", "content"),
        context=_as_int(args.get("context"), 0),
        task_id=kw.get("task_id", "default"),
    )
    return _to_json(result)


# ---------------------------------------------------------------------------
# "new"-path handlers (Vision01__read.md / Vision01__write.md) — plain-text
# responses, never JSON. Built as factories (not module-level functions like
# the _handle_* ones above) because their behavior depends on per-slot config
# (mtime toggle, shared formatting symbols) resolved once in register(), not
# on anything in the tool call itself.
# ---------------------------------------------------------------------------

def _make_handle_view_file_new(mtime_enabled: bool, symbols: dict, anchors_enabled: bool = True,
                                anchor_gap=None):
    def _handler(args, **kw) -> str:
        if has_flag(args.get("flags"), "help"):
            return _READ_FILE_FLAGS_HELP
        path = args.get("path")
        if not path or not isinstance(path, str):
            return "ERROR: read_file: missing required field 'path'."
        return view_file_new_core.view_file_tool_new(
            path=path,
            # Unadvertised since Plan08: these three are no longer schema
            # properties (they live in `flags`), but a call that still sends
            # them - a session in flight, a saved skill, a cron prompt - keeps
            # working. The core lets an equally-named flag win.
            line=_as_int(args.get("line"), 1),
            count=_as_int(args.get("count"), 2000),
            numbered=bool(args.get("numbered", False)),
            task_id=kw.get("task_id", "default"),
            # Passed explicitly rather than probed: model_tools forwards it to
            # every dispatch, and an explicit argument is what the tests can
            # set. _ledger falls back to the approval contextvar when it's
            # empty (console mode, older call paths).
            session_id=kw.get("session_id") or "",
            mtime_enabled=mtime_enabled,
            symbols=symbols,
            flags=args.get("flags"),
            anchors_enabled=anchors_enabled,
            anchor_gap=anchor_gap,
        )
    return _handler


def _make_handle_create_file_new(mtime_enabled: bool, ui_json: str = "off", symbols: dict = None):
    def _handler(args, **kw) -> str:
        if has_flag(args.get("flags"), "help"):
            return _WRITE_FILE_FLAGS_HELP
        path = args.get("path")
        if not path or not isinstance(path, str):
            return "ERROR: write_file: missing required field 'path'."
        content = args.get("content")
        if content is None:
            return "ERROR: write_file: missing required field 'content'."
        if not isinstance(content, str):
            return f"ERROR: write_file: 'content' must be a string, got {type(content).__name__}."
        raw_insert_line = args.get("insert_line")
        insert_line = None if raw_insert_line is None else _as_int(raw_insert_line, None)
        return create_file_new_core.create_file_tool_new(
            path=path,
            content=content,
            insert_line=insert_line,
            task_id=kw.get("task_id", "default"),
            session_id=kw.get("session_id") or "",
            mtime_enabled=mtime_enabled,
            flags=args.get("flags"),
            ui_json=ui_json,
            symbols=symbols,
        )
    return _handler


def _make_handle_sift_files_new(symbols: dict):
    def _handler(args, **kw) -> str:
        if has_flag(args.get("flags"), "help"):
            return _SEARCH_FILES_FLAGS_HELP
        path = args.get("path")
        if not path or not isinstance(path, str):
            return "ERROR: search_files: missing required field 'path'."
        pattern = args.get("pattern")
        if pattern is None or not isinstance(pattern, str):
            return "ERROR: search_files: missing required field 'pattern' (pass \"\" for filenames only)."
        return sift_files_new_core.sift_files_tool_new(
            path=path,
            pattern=pattern,
            glob=args.get("glob") or "*",
            flags=args.get("flags"),
            task_id=kw.get("task_id", "default"),
            session_id=kw.get("session_id") or "",
            symbols=symbols,
        )
    return _handler


# ---------------------------------------------------------------------------
# Dev-only slash command: force a live plugin reload in THIS process.
# ---------------------------------------------------------------------------

def _make_handle_modify_file_new(symbols: dict, ui_json: str = "off"):
    def _handler(args, **kw) -> str:
        if has_flag(args.get("flags"), "help"):
            return _PATCH_FLAGS_HELP
        path = args.get("path")
        if not path or not isinstance(path, str):
            return "ERROR: patch: missing required field 'path'."
        old_string = args.get("old_string")
        new_string = args.get("new_string")
        if old_string is None:
            return ("ERROR: patch: missing required field 'old_string'. "
                    "Re-emit the tool call with all of path/line/old_string/new_string/flags.")
        if new_string is None:
            return ("ERROR: patch: missing required field 'new_string'. "
                    "Re-emit the tool call with all of path/line/old_string/new_string/flags.")
        # `line` is required-with-no-default in the schema, but a dropped one
        # must not become a crash: 0 is its own honest value ("I don't know
        # where this is"), so a missing field degrades to a whole-file search
        # rather than an error the model can't act on.
        return modify_file_new_core.modify_file_tool_new(
            path=path,
            line=_as_int(args.get("line"), 0),
            old_string=old_string,
            new_string=new_string,
            flags=args.get("flags"),
            task_id=kw.get("task_id", "default"),
            session_id=kw.get("session_id") or "",
            symbols=symbols,
            ui_json=ui_json,
        )
    return _handler


def _handle_terminal(args, symbols: dict = None, **kw) -> str:
    # Mirror of hermes-agent's own _handle_terminal misplaced-arg recovery
    # (their tools/terminal_tool.py): models sometimes send execute_code's
    # `code` argument here instead of `command`.
    if "command" not in args and "code" in args:
        return ("ERROR: terminal received a 'code' parameter, but it requires a shell "
                "command in 'command'. Use execute_code(code=...) for Python; "
                "for shell, retry as terminal(command=...).")
    command = args.get("command")
    if not command or not isinstance(command, str):
        return "ERROR: terminal: missing required field 'command'."
    raw_timeout = args.get("timeout")
    timeout = _as_int(raw_timeout, None) if raw_timeout is not None else None
    return terminal_core.terminal_tool_new(
        command=command,
        background=bool(args.get("background", False)),
        timeout=timeout,
        task_id=kw.get("task_id", "default"),
        session_id=kw.get("session_id") or "",
        workdir=args.get("workdir"),
        pty=bool(args.get("pty", False)),
        notify_on_complete=bool(args.get("notify_on_complete", False)),
        watch_patterns=args.get("watch_patterns"),
        symbols=symbols,
    )


def _make_handle_terminal(symbols: dict):
    """Same shape as the other _make_handle_* factories: the resolved
    formatting symbols are closed over once in register(), not re-read per
    call. terminal only needs the separator, but it must be the SAME one the
    file tools use - a profile that overrides separator_symbol has to move
    every tool together or the panel's parser sees two conventions.
    """
    def _handler(args, **kw) -> str:
        return _handle_terminal(args, symbols=symbols, **kw)

    return _handler


def _handle_reload(raw_args: str):
    """Not model-facing (no schema, never in the LLM's tool list) - typed
    directly in chat as a command. Plugins import once into sys.modules and
    stay cached for the whole process (PluginManager is a process-wide
    singleton); editing a core/schema file on disk does nothing to an
    already-running session on its own (confirmed empirically 2026-08-25:
    exiting and re-entering the chat picks up edits, staying in the same
    chat does not). discover_and_load(force=True) is core's own sanctioned
    fix for exactly this - its docstring: "so config changes ... become
    visible in long-lived sessions without requiring a full agent restart"
    - but it only affects the process that calls it, so it must run FROM
    INSIDE that process. A slash-command handler runs in-process (unlike
    execute_code, which per core's own config docs runs as a separate
    python process/interpreter and so can't reach this same in-memory
    PluginManager) - an external OS-terminal command can't reach it either,
    for the same reason (no shared memory across processes).
    """
    from hermes_cli.plugins import discover_plugins

    discover_plugins(force=True)
    return "hermes-filetools: forced a full plugin discovery reload (force=True) in this process."


# ---------------------------------------------------------------------------
# Plugin entry point (Hermes loader: getattr(module, "register"))
# ---------------------------------------------------------------------------

# Slot = the logical tool identity (fixed); config can rename what it's
# REGISTERED as (via which schema file's own `name:` gets loaded), disable
# it, or (once a "new" implementation exists) pick which handler runs it.
# handler/emoji are keyed by slot, not by registered name, since the slot's
# underlying logic doesn't change based on what name it's exposed under.
#
# Code-level safety net only - used when plugin.yaml's own `tools:` section
# (the real, intended default source, see module docstring) doesn't declare
# a slot. Keeps patch/search_files working today; migrate them into
# plugin.yaml's `tools:` the same way read_file/write_file already are, and
# their entry here stops mattering.
_FALLBACK_SLOT_DEFAULTS = {
    "read_file": {"enabled": True, "path": "old", "params": {}},
    "write_file": {"enabled": True, "path": "old", "params": {}},
    "patch": {"enabled": True, "path": "old", "params": {}},
    "search_files": {"enabled": True, "path": "old", "params": {}},
    # "new" here, unlike the four above: terminal has no "old" implementation
    # of OURS to fall back to (there never was one - _handle_terminal has
    # called terminal_core since Plan03), so "old" means "don't register,
    # leave upstream's JSON-returning terminal in place". Defaulting a missing
    # manifest to that would silently turn the wrapper off.
    "terminal": {"enabled": True, "path": "new", "params": {}},
}


def _effective_manifest() -> dict:
    """This plugin's config as it ACTS: `plugin.yaml` (defaults - a release/deploy overwrites
    it) with the user's `<HERMES_HOME>/filetools/settings.yaml` on top (what the Config panel
    writes since 2026-09-30). One rule for both files - `_home.py`."""
    import os
    from . import _home
    manifest_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugin.yaml")
    return _home.effective(manifest_path)


def _load_manifest_tools_defaults() -> dict:
    """Read the `tools:` defaults section straight out of this plugin's own
    plugin.yaml (repo root, next to this file) - NOT via Hermes' own
    manifest parser, which doesn't recognize "tools" as a known field and
    would silently drop it (see module docstring). We own this file, so we
    read it ourselves instead of going through core.
    """
    try:
        data = _effective_manifest()
    except Exception:
        return {}
    tools = data.get("tools")
    return tools if isinstance(tools, dict) else {}


def _load_manifest_formatting_defaults() -> dict:
    """Read this plugin's own `formatting:` section straight out of plugin.yaml
    (same rationale/mechanism as _load_manifest_tools_defaults above - core's
    manifest parser doesn't know this field either).
    """
    try:
        data = _effective_manifest()
    except Exception:
        return {}
    formatting = data.get("formatting")
    return formatting if isinstance(formatting, dict) else {}


def _load_manifest_ledger_defaults() -> dict:
    """Read this plugin's own `ledger:` section out of plugin.yaml (same
    rationale/mechanism as the two loaders above).

    Service-level, not per-slot: the ledger is ONE store shared by every tool,
    so its thresholds have no business under any single slot's `params`.
    """
    try:
        data = _effective_manifest()
    except Exception:
        return {}
    ledger = data.get("ledger")
    return ledger if isinstance(ledger, dict) else {}


def _load_manifest_context_ctl_defaults() -> dict:
    """Read this plugin's own `context_ctl:` section out of plugin.yaml (same
    rationale/mechanism as the loaders above).

    Only a kill switch lives here. WHICH engine runs is Hermes' own
    `context.engine` key, and that split is deliberate: the engine's one
    monkeypatch into core must never be installed by a setting of OURS alone -
    it appears only once the owner has already selected us through their own
    config. This key exists to turn the engine off WITHOUT editing their
    config (and so without losing our name from it).
    """
    try:
        data = _effective_manifest()
    except Exception:
        return {}
    section = data.get("context_ctl")
    return section if isinstance(section, dict) else {}


def _load_manifest_version() -> str:
    """This plugin's own `version:` - recorded in the engine's status file so a
    live status can be traced back to the code that wrote it.
    """
    import os

    manifest_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugin.yaml")
    try:
        from hermes_yaml import safe_load
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = safe_load(f) or {}
        return str(data.get("version") or "")
    except Exception:
        return ""


def _load_manifest_disabled_tools() -> list:
    """Read this plugin's own `disabled_tools:` section out of plugin.yaml (same
    rationale/mechanism as the two loaders above).
    """
    try:
        data = _effective_manifest()
    except Exception:
        return []
    names = data.get("disabled_tools")
    return [str(n) for n in names if str(n).strip()] if isinstance(names, list) else []


def _never_available() -> bool:
    """check_fn for a suppressed tool: the tool is never offered to the model.

    The registry hands out schemas ONLY for tools whose check_fn passes
    (model_tools.py:509), so this is the sanctioned "this tool is unavailable"
    switch - the same mechanism ``execute_code`` uses to hide itself when no
    sandbox is configured. One shared function object on purpose: check_fn
    results are cached per function (tools/registry.py::_check_fn_cached).
    """
    return False


def _suppressed_schema(name: str) -> dict:
    """Minimal schema for a suppressed tool. Never reaches the model (its
    check_fn always fails), so the description is for humans reading the
    registry, not for the model."""
    return {
        "name": name,
        "description": (
            f"Disabled by the hermes-filetools plugin (disabled_tools in plugin.yaml). "
            f"Never offered to the model."
        ),
        "required": [],
        "properties": {},
    }


def _suppressed_handler(name: str):
    def _handler(**_kwargs) -> str:
        # Unreachable in practice: a tool whose check_fn fails is not in the
        # model's tool list at all. Kept honest rather than raising, so a future
        # core change that calls it anyway degrades to a message, not a crash.
        return (
            f"ERROR: {name} is disabled by the hermes-filetools plugin "
            f"(disabled_tools in plugin.yaml)."
        )
    return _handler


def _make_handle_execute_code(symbols: dict):
    def _handler(args, **kw) -> str:
        return execute_code_core.execute_code_tool_new(args, symbols=symbols, **kw)

    return _handler


# The marker a sandbox stub adds to its own RPC args to ask for a dict instead
# of our plain text (Plan06). Leading underscore is not decoration: no schema
# in file_ops/*.yaml declares a parameter starting with one (checked), so the
# model cannot collide with it by guessing a plausible field name - and it
# never learns the marker exists, because _build_stub puts it in the stub's
# args expression, not in the signature the model reads.
_FACADE_MARKER = "_filetools_dict"

# The five slots that HAVE a Python facade inside execute_code, i.e. appear in
# core's own _TOOL_STUBS. execute_code/process/vision_analyze are absent from
# that table, so nothing can ever ask them for a dict.
_FACADE_SLOTS = ("read_file", "write_file", "search_files", "patch", "terminal")

# Slots whose facade `body` must be the FILE'S OWN text and nothing else, so
# our marker lines come off it. Only read_file qualifies, and the narrowness is
# the point: a script that reads a file and writes it back has to get a copy,
# and `◆■BLOCK`/`◆■END` lines in between mean it gets a corrupted file instead
# (found live 2026-09-02 by printing r2["body"]).
#
# NOT the other three, deliberately. `search_files` marks up its body with the
# same vocabulary, but there those lines say WHICH FILE each match came from -
# stripping them would delete the answer, not clean it. `patch`'s body is an
# echo of the changed region, a view rather than content, and nothing round-
# trips it into a write. `terminal` never emits markers at all.
#
# Two limits worth knowing, neither fixable here: a RANGED read returns that
# range, so writing it back is not a whole-file copy (inherent to a partial
# read); and `--numbered` prefixes every line with `N|`, which we do not undo
# because the caller explicitly asked for it.
_FACADE_STRIP_MARKERS = ("read_file",)


def _make_facade(inner, symbols: dict, slot: str):
    """Wrap a slot's handler so a marked call gets the dict facade.

    One wrapper here instead of the same branch in five handlers, and the
    branch is deliberately thin: BOTH facades come from the same single call to
    `inner`, so there is no second implementation of any tool - only a
    different shape on the way out.

    `functools.wraps` matters beyond tidiness: registration tests (and anything
    else introspecting) identify a slot by its handler's `__qualname__`, and
    this wrapper must not make our own closure look like someone else's.

    `args` is copied before the marker is popped - `handle_function_call` hands
    us the caller's own dict, and upstream's handler has no business seeing a
    key we invented.

    Fail-open, like everywhere else here: if the dict cannot be assembled the
    text goes out unchanged. A script that gets a string can still print it; a
    script that gets an exception has lost the answer.
    """
    import functools

    @functools.wraps(inner)
    def _handler(args, **kw):
        args = dict(args or {})
        wants_dict = bool(args.pop(_FACADE_MARKER, False))
        result = inner(args, **kw)
        if not wants_dict or not isinstance(result, str):
            return result
        try:
            facade = to_facade_dict(result, symbols=symbols)
            if slot in _FACADE_STRIP_MARKERS and facade["body"]:
                # `path`'s comment style is what makes the recognition
                # structural instead of a substring hunt, and the same call
                # restores a ⚓ anchor as the blank line it took over rather
                # than deleting the line - which is why this reuses
                # write_file/patch's own guard instead of a fresh regex.
                where = args.get("path") or ""
                # ■BLOCK/■END say WHICH lines of the file the body covers, and
                # that fact survives nowhere else - a ranged read whose range
                # was deleted reads exactly like a whole-file read. So they
                # move UP into the header, verbatim, instead of being dropped
                # with the rest of the markup.
                blocks = block_marker_lines(facade["body"], where, symbols)
                facade["body"], _ = strip_marker_lines(
                    facade["body"], where, symbols)
                if blocks:
                    facade["header"] = "\n".join(
                        ([facade["header"]] if facade["header"] else []) + blocks)
            return json.dumps(facade, ensure_ascii=False)
        except Exception:
            return result

    return _handler


def _upstream_name(candidates: tuple, default: str) -> str:
    """Which of `candidates` upstream ACTUALLY registered this run.

    Exists because of a real, silent break: at upstream afc3d9d34 the tool we
    wrap as `process` was renamed to `process_manage` (commit e16ad33a9). A
    rename is worse for us than a removal - `override=True` is a no-op when no
    name collides, so our slot registers as a plain PLUGIN tool, and per
    `tools/tool_search.py::is_deferrable_tool_name` a plugin tool outside
    `_HERMES_CORE_TOOLS` is deferrable, i.e. hidden behind the tool_search
    bridge until searched for. Nothing raises; the tool simply stops being
    visible, and their renamed one is deferred too. So the model ends up with
    no eager process tool at all and two catalog stubs.

    Hardcoding the new name would just move the same trap one rename along, so
    the name is READ from their registry instead. Candidates are tried in
    order, newest upstream name first: on a `/filetools-reload` our own
    previous registration is still in `registry._tools` under whichever name
    we chose last time, and order is what keeps that from pinning us to it.

    `discover_builtin_tools()` has to run first or the registry holds only
    plugin tools - the same ordering trap documented in
    `_suppress_disabled_tools`. It is their own function, it caches, and its
    only effect is importing tool modules the app imports anyway.

    Fail-open to `default`, which must be upstream's CURRENT name: if we cannot
    look, the version we are being installed against is the new one.
    """
    try:
        from tools.registry import registry, discover_builtin_tools
        discover_builtin_tools()
        for name in candidates:
            if name in registry._tools:
                return name
    except Exception as exc:
        print(f"[filetools] could not read upstream's name for {default!r} "
              f"({exc}); using it as-is")
    return default


# Upstream's current name first. `process` stays as a candidate so the plugin
# still overrides correctly on a Hermes older than afc3d9d34 - the config slot
# key stays `process` either way, it is our own identifier, not theirs.
_PROCESS_NAMES = ("process_manage", "process")


def _wrapper_disabled(slot: str, cfg: dict) -> bool:
    """True when this wrapped slot must not be registered at all.

    Two keys say it, and they mean the same thing for these three slots (and
    for `terminal` in the loop above): `enabled: false`, or `path` set to
    anything but "new". They aren't redundant - `path` is the key register()
    collects the taken-over tools by, so it has to be readable per slot even
    where the tool has no Python facade for that list to reach.

    Default "new" rather than "old", deliberately: for the four file slots the
    code-level default is the conservative one because both implementations
    are ours. Here "old" means "we register nothing", so a missing manifest
    entry would silently switch the wrapper off instead of falling back to
    something.
    """
    if not cfg.get("enabled", True):
        return True
    path = cfg.get("path", "new")
    if path != "new":
        print(f"[filetools] {slot}: path != new, not registered "
              f"(Hermes' own {slot} stays)")
        return True
    return False


def _register_execute_code(ctx, symbols: dict, cfg: dict) -> None:
    """Register OUR execute_code, or nothing at all.

    Unlike the five slots above, this one is CONDITIONAL, and every condition
    is there because the alternative would resurrect a tool the operator turned
    off (Plan04, scope):

    - **Upstream must be importable.** No `tools.code_execution_tool` means no
      sandbox in this build; registering our name would announce a tool that
      cannot run.
    - **Their own `check_sandbox_requirements` is forwarded as our check_fn**,
      not re-implemented. The registry hands out schemas only for tools whose
      check_fn passes (model_tools.py:509), so when the sandbox is unavailable
      our entry is invisible for exactly the same reason theirs is.
    - **Their toolset, not ours.** `toolset="code_execution"` keeps the
      operator's existing switch working: disable that toolset and our version
      goes with it. Registering under `filetools` would quietly make
      `execute_code` un-disableable - the mirror image of the `desktop_ui`
      problem that `_suppress_disabled_tools` exists to fix.

    The SCHEMA is theirs, built live, and that is not laziness - see
    execute_code_core.py's docstring: `model_tools.py:522-529` overwrites the
    model-facing execute_code schema by name regardless of who registered it,
    so our own copy could never reach the model. Their build also carries
    information a static copy would lose (which sandbox tools are enabled this
    session, which execution mode is configured).

    Fail-open and LOUD: any failure here leaves upstream's own execute_code in
    place, which is a working tool with JSON output - a worse response format,
    not a broken one. Silence would be the bug, so each outcome prints.
    """
    if _wrapper_disabled("execute_code", cfg):
        return

    try:
        from tools.code_execution_tool import check_sandbox_requirements
        from tools.code_execution_tool import build_execute_code_schema
    except Exception as exc:
        print(f"[filetools] execute_code: upstream not importable, not registered ({exc})")
        return

    try:
        schema = build_execute_code_schema()
    except Exception as exc:
        print(f"[filetools] execute_code: schema build failed, not registered ({exc})")
        return

    try:
        ctx.register_tool(
            name="execute_code",
            toolset="code_execution",
            schema=schema,
            handler=_make_handle_execute_code(symbols),
            check_fn=check_sandbox_requirements,
            emoji="\U0001F40D",  # 🐍 - upstream's own, kept so the UI doesn't change
            override=True,
        )
    except Exception as exc:
        print(f"[filetools] execute_code: REFUSED, upstream's JSON version stays ({exc})")
        return
    print("[filetools] execute_code: wrapped (plain text instead of JSON)")


def _make_handle_process(symbols: dict):
    def _handler(args, **kw) -> str:
        return process_core.process_tool_new(args, symbols=symbols, **kw)

    return _handler


def _register_process(ctx, symbols: dict, cfg: dict) -> None:
    """Register OUR process, or nothing at all.

    Simpler than `_register_execute_code` in two ways, both because upstream's
    own registration is simpler (`tools/process_registry.py:3357`): there is no
    `check_fn` to forward (they declare none - the tool is always available),
    and nothing rebuilds its schema per session, so our OWN YAML copy really
    does reach the model and can say the output is plain text.

    Same as execute_code otherwise: conditional on upstream being importable,
    filed under THEIR toolset (`terminal`) so the operator's existing switch
    still turns it off, and fail-open+loud - leaving their JSON-returning
    version in place is a worse response format, not a broken tool.
    """
    if _wrapper_disabled("process", cfg):
        return

    try:
        import tools.process_registry  # noqa: F401  - presence check only
    except Exception as exc:
        print(f"[filetools] process: upstream not importable, not registered ({exc})")
        return

    try:
        schema = load_schema("process.yaml", _vocab.placeholders(symbols))
        # THEIR name, resolved at registration, not the one our YAML happens to
        # carry: this is the only slot upstream has renamed so far
        # (process -> process_manage), and shadowing the wrong name is silent
        # (see _upstream_name). The schema copy keeps its own WORDING - only
        # the name is theirs to dictate.
        name = _upstream_name(_PROCESS_NAMES, _PROCESS_NAMES[0])
        schema = {**schema, "name": name}
        ctx.register_tool(
            name=name,
            toolset="terminal",
            schema=schema,
            handler=_make_handle_process(symbols),
            emoji="⚙",  # ⚙ - upstream's own (minus the variation selector)
            override=True,
        )
    except Exception as exc:
        print(f"[filetools] process: REFUSED, upstream's JSON version stays ({exc})")
        return
    print(f"[filetools] process: wrapped as {name!r} (plain text instead of JSON)")


def _make_handle_vision_analyze(symbols: dict):
    async def _handler(args, **kw):
        return await vision_analyze_core.vision_analyze_tool_new(
            args, symbols=symbols, **kw)

    return _handler


def _register_vision_analyze(ctx, symbols: dict, cfg: dict) -> None:
    """Register OUR vision_analyze, or nothing at all (Plan05).

    Same terms as the other two wrapped slots, with two differences that come
    from upstream's own registration (`tools/vision_tools.py:1810`):

    - **`is_async=True`.** Their handler is a coroutine and so is ours. Getting
      this wrong does not fail at registration - it fails at the first call.
    - **`check_fn` is their `check_vision_requirements`, forwarded.** Vision
      needs a configured provider; when there is none our entry must be
      invisible for exactly the same reason theirs is.

    Toolset stays THEIRS (`vision`) so the operator's existing switch still
    turns the tool off. Fail-open and loud: leaving upstream's version in place
    means the container-path bug is back, which is bad, but a silent
    half-registration would be worse.
    """
    if _wrapper_disabled("vision_analyze", cfg):
        return

    try:
        from tools.vision_tools import check_vision_requirements
    except Exception as exc:
        print(f"[filetools] vision_analyze: upstream not importable, not registered ({exc})")
        return

    try:
        schema = load_schema("vision_analyze.yaml", _vocab.placeholders(symbols))
        ctx.register_tool(
            name=schema["name"],
            toolset="vision",
            schema=schema,
            handler=_make_handle_vision_analyze(symbols),
            check_fn=check_vision_requirements,
            is_async=True,
            emoji="\U0001F441",  # 👁 - upstream's own (minus the variation selector)
            override=True,
        )
    except Exception as exc:
        print(f"[filetools] vision_analyze: REFUSED, upstream's version stays ({exc})")
        return
    print("[filetools] vision_analyze: wrapped (plain path in, plain text out)")


def _suppress_disabled_tools(ctx) -> None:
    """Hide the tools named in `disabled_tools:` from the model.

    WHY THIS EXISTS HERE, of all places. Two of Hermes' toolsets - ``desktop_ui``
    and ``project`` - are deliberately kept OFF ``_HERMES_CORE_TOOLS`` and are
    injected by exactly one function, ``_gui_surface_toolsets()``
    (``tui_gateway/server.py``), keyed on the session's source being the desktop
    app. That injection is a UNION applied AFTER every config-derived
    subtraction, on both of the resolver's return paths::

        tui_gateway/server.py:5281   return sorted({*selection, *_gui_surface_toolsets(...)})
        tui_gateway/server.py:5398   return sorted(enabled | _gui_surface_toolsets(...))

    So no config key can switch them off for a desktop session - not
    ``platform_toolsets``, not ``agent.disabled_toolsets`` (which
    ``_get_platform_tools`` does honour, only for the union to add the toolset
    straight back), and not the desktop app's own Tools UI, whose
    ``setToolsetEnabled()`` writes that same losing ``platform_toolsets`` key.
    Verified live 2026-09-02 on the operator's config: ``project`` had been in
    ``agent.disabled_toolsets`` for a while and its tools were still offered.

    HOW, and why not the obvious way. ``registry.deregister()`` looks like the
    answer and is NOT available to us: a plugin's registry scope is its profile,
    so ``caller_scope`` is never ``None``, and the API refuses outright -
    "Scoped plugin module 'hermes_plugins.hermes_filetools' cannot deregister
    process-global tool 'open_preview'; register a scoped override instead"
    (tools/registry.py::deregister). That is structural, not a missing
    permission: no config flag grants it, and ``allow_tool_override`` is checked
    further down, past the refusal. Tried live 2026-09-02, all 16 names refused.

    So we take the route that refusal names. A SCOPED OVERRIDE with a ``check_fn``
    that always fails: the registry hands out schemas only for tools whose
    check_fn passes (model_tools.py:509), which is the same mechanism
    ``execute_code`` uses to hide itself when no sandbox exists. The tool stays in
    the registry, our entry shadows theirs for this profile, and the model's tool
    list never contains it. This is not a stub the model can call and get an error
    from - it never learns the tool exists.

    Scope of the damage, deliberately: these are the tools by which the MODEL
    drives the desktop's panes (they call ``desktop_ui.emit``). The panes
    themselves are drawn by the client and keep working; what goes away is
    "the model can open a preview", not "previews are broken".

    Fail-open per name, and that is the whole error policy: a name that isn't
    registered (tool absent, toolset already off, upstream renamed it) is a
    no-op, and one refused override must not take our own five slots down with
    it.
    """
    try:
        profile_names = ctx.get_config("disabled_tools", None)
    except Exception:
        profile_names = None
    # Profile REPLACES the manifest list rather than merging: this is a denylist,
    # and a merge would make a name impossible to take back out from the profile.
    if isinstance(profile_names, list):
        names = [str(n) for n in profile_names if str(n).strip()]
    else:
        names = _load_manifest_disabled_tools()
    if not names:
        return

    try:
        from tools.registry import registry
    except Exception as exc:
        print(f"[filetools] disabled_tools: registry unavailable, nothing suppressed ({exc})")
        return

    # Force builtin discovery FIRST, or there is nothing here to shadow yet.
    # discover_builtin_tools() runs as an import-time side effect of
    # model_tools.py (model_tools.py:230), and plugins load before anything
    # imports that module - so at this point the registry holds only plugin
    # tools and every name below looks absent. Verified exactly that way on the
    # first live load, 2026-09-02.
    #
    # Calling it here is their own function, it caches its verdicts, and its only
    # effect is importing the tool modules - a state the app reaches
    # unconditionally anyway, just slightly later. Cheaper than the alternative,
    # which is a feature that silently does nothing.
    try:
        from tools.registry import discover_builtin_tools
        discover_builtin_tools()
    except Exception as exc:
        print(f"[filetools] disabled_tools: builtin discovery failed, suppression may be partial ({exc})")

    hidden, missing, refused = [], [], []
    for name in names:
        try:
            if name not in registry._tools:
                # Not registered at all: nothing to shadow, and registering our
                # own placeholder would ADD a name that wasn't there.
                missing.append(name)
                continue
            ctx.register_tool(
                name=name,
                toolset=TOOLSET,
                schema=_suppressed_schema(name),
                handler=_suppressed_handler(name),
                check_fn=_never_available,
                emoji="🚫",
                override=True,
            )
            hidden.append(name)
        except Exception as exc:
            refused.append(f"{name} ({exc})")

    if hidden:
        print(f"[filetools] disabled_tools: hidden {len(hidden)} — {', '.join(sorted(hidden))}")
    if missing:
        print(f"[filetools] disabled_tools: not registered, skipped — {', '.join(sorted(missing))}")
    if refused:
        # Loud on purpose: a refusal means the tool IS still offered to the model,
        # i.e. the operator asked for something that silently did not happen.
        print(f"[filetools] disabled_tools: REFUSED — {'; '.join(refused)}")


# Schema auto-derivation: which file_ops/*.yaml a slot loads when its config
# doesn't explicitly set `schema:` (see plugin.yaml's config_schema docs for
# the explicit-override escape hatch). `patch` has only one implementation
# (old) so it just has that one entry.
# The table itself lives in `file_ops/_vocab.py` (`deferred/08`): the panel reads tool
# parameters from the same files, and two copies of this map would drift.
_SCHEMA_BY_PATH = _vocab.SCHEMA_BY_PATH

# "old"-path handlers only - "new"-path handlers are built per-call by the
# factories above, since they close over per-slot config (mtime/symbols).
_SLOT_HANDLERS_OLD = {
    "read_file": _handle_view_file,
    "write_file": _handle_create_file,
    "patch": _handle_modify_file,
    "search_files": _handle_sift_files,
    # No "terminal" entry on purpose: there is no old terminal of ours. The
    # one that used to sit here called terminal_core all the same, so
    # `path: old` quietly registered the wrapper minus its `symbols`.
}

# 2026-08-25: swapped for symbols without a variation selector (U+FE0F) -
# some consoles don't render VS16'd glyphs (✍️/🖋️/🕵️) or very recent
# Unicode additions (🧵, added in Unicode 11.0/2018) at all. Old set kept
# below as comments, not deleted, in case this doesn't actually help or
# needs reverting.
_SLOT_EMOJI = {
    "read_file": "\U0001F4DA",      # 📚 (was: \U0001F4D6 📖)
    "write_file": "\U0001F4DD",     # 📝 (was: ✍️)
    "patch": "\U0001F529",          # 🔩 (was: \U0001F527 🔧)
    "search_files": "\U0001F50D",   # 🔍 (was: \U0001F50E 🔎)
    "terminal": "\U0001F4BB",       # 💻
}


def register(ctx) -> None:
    """Register the 5 filetools slots, per-slot config permitting.

    Registered names default to the BUILT-IN names with override=True
    (2026-08-25 decision, see module docstring for why: it's the only way
    to escape Hermes' tool_search deferral, core exempts by exact name
    only). override=True is harmless when the configured schema's name
    doesn't collide with an existing global tool (registry.register()'s
    shadow check only engages when the name actually matches one) - so a
    slot pointed at an alternate schema (e.g. name: read_file2) registers
    as a normal, non-overriding, parallel tool with no extra config needed.
    """
    manifest_tools = _load_manifest_tools_defaults()
    manifest_formatting = _load_manifest_formatting_defaults()
    try:
        profile_tools = ctx.get_config("tools", {}) or {}
    except Exception:
        profile_tools = {}
    try:
        profile_formatting = ctx.get_config("formatting", {}) or {}
    except Exception:
        profile_formatting = {}
    symbols = {**DEFAULT_SYMBOLS, **manifest_formatting, **profile_formatting}

    # Ledger service config + sidecar housekeeping (Plan11-Task03/04).
    #
    # Here and nowhere else, for two reasons. This is the only place holding
    # `ctx.get_config`, and the cores that write ledger entries never see it —
    # threading a threshold through eight core signatures to reach one module
    # would be the wrong trade. And registration is the one cheap, predictable
    # moment for the sweep: once per process, with the config already in hand.
    # Not per write (a stat-walk per operation is absurd) and not on a timer
    # (adding one to a plugin for the sake of text files is out of proportion).
    #
    # Fail-open as a whole: the ledger is observability, and a config it could
    # not read must leave the plugin registering normally on its defaults.
    try:
        from .file_ops import _ledger as _ledger_service
        try:
            profile_ledger = ctx.get_config("ledger", {}) or {}
        except Exception:
            profile_ledger = {}
        _ledger_service.configure(**{**_load_manifest_ledger_defaults(),
                                     **profile_ledger})
        _ledger_service.sweep_sidecars()
    except Exception:
        pass

    # Collected below, only for slots that end up on path:new this run - fed
    # to patch_execute_code_bridge() after the loop so execute_code's own
    # Python-API bridge (Recon10) tells the model the truth about these
    # specific tools instead of core's stale/JSON-only assumptions. A slot
    # left on path:old is deliberately NOT included - its response really
    # is the JSON dict core's own stub already promises.
    new_path_schemas = {}

    for slot, fallback in _FALLBACK_SLOT_DEFAULTS.items():
        manifest_cfg = manifest_tools.get(slot) or {}
        profile_cfg = profile_tools.get(slot) or {}
        cfg = {**fallback, **manifest_cfg, **profile_cfg}
        if not cfg.get("enabled", True):
            continue
        path = cfg.get("path", "old")

        # Explicit `schema:` (manifest or profile) always wins - that's the
        # escape hatch for pointing a slot at a second, differently-named
        # tool. Otherwise auto-derive from `path` so switching path: old->new
        # doesn't leave a slot's schema silently mismatched with its handler.
        explicit_schema = profile_cfg.get("schema") or manifest_cfg.get("schema")
        schema_by_path = _SCHEMA_BY_PATH.get(slot, {})
        schema_name = explicit_schema or schema_by_path.get(path) or schema_by_path.get("old")

        params = cfg.get("params") or {}
        mtime_enabled = bool(params.get("mtime", True))

        if path == "new" and slot == "read_file":
            handler = _make_handle_view_file_new(
                mtime_enabled, symbols, bool(params.get("anchors", True)),
                params.get("anchor_gap"))
        elif path == "new" and slot == "write_file":
            handler = _make_handle_create_file_new(mtime_enabled,
                                                    params.get("ui_json", "off"), symbols)
        elif path == "new" and slot == "search_files":
            handler = _make_handle_sift_files_new(symbols)
        elif path == "new" and slot == "patch":
            handler = _make_handle_modify_file_new(symbols, params.get("ui_json", "off"))
        elif slot == "terminal":
            # One implementation only, so `path` is a switch, not a choice:
            # anything but "new" leaves upstream's own JSON-returning terminal
            # registered and us out of the way. Printed, not silent - the
            # whole point of this key is that you can tell from the load log
            # which tools we took over.
            if path != "new":
                print("[filetools] terminal: path != new, not registered "
                      "(Hermes' own terminal stays)")
                continue
            handler = _make_handle_terminal(symbols)
        else:
            # "old" was requested (every slot has a "new" implementation as of
            # 2026-08-26) - fall back to "old" rather than silently
            # registering nothing if a slot ever loses one again.
            handler = _SLOT_HANDLERS_OLD[slot]

        # Placeholders in the description (`{block_open}`, Plan28) get the symbols this run uses.
        schema = load_schema(schema_name, _vocab.placeholders(symbols))
        # Only path:new slots get the dict facade, and the condition is the
        # same one that puts a slot in new_path_schemas below - a slot whose
        # stub we did NOT patch can never receive the marker, so wrapping it
        # would add a branch that is unreachable by construction.
        if path == "new" and slot in _FACADE_SLOTS:
            handler = _make_facade(handler, symbols, slot)
        ctx.register_tool(
            name=schema["name"],
            toolset=TOOLSET,
            schema=schema,
            handler=handler,
            emoji=_SLOT_EMOJI[slot],
            override=True,
        )
        # `terminal` joined this list 2026-09-02 and it fixed a live lie: its
        # answer through the sandbox facade has been plain text since Plan03,
        # while core's own doc line kept promising `-> dict` with "output" and
        # "exit_code" keys to index into.
        if path == "new" and slot in ("read_file", "write_file", "search_files",
                                      "patch", "terminal"):
            new_path_schemas[schema["name"]] = schema

    patch_execute_code_bridge(new_path_schemas)

    _register_execute_code(ctx, symbols, {
        **(manifest_tools.get("execute_code") or {}),
        **(profile_tools.get("execute_code") or {}),
    })
    _register_process(ctx, symbols, {
        **(manifest_tools.get("process") or {}),
        **(profile_tools.get("process") or {}),
    })
    _register_vision_analyze(ctx, symbols, {
        **(manifest_tools.get("vision_analyze") or {}),
        **(profile_tools.get("vision_analyze") or {}),
    })

    # AFTER our own slots, not before: a refused/failed suppression must never
    # be able to take the five filetools slots down with it. Re-runs on every
    # plugin load, so /filetools-reload re-applies it.
    _suppress_disabled_tools(ctx)

    # Context engine (Plan13, этап B). LAST, and fail-open as a whole: the five
    # filetools slots above are the product, the engine is an experiment on top
    # of it, and an experiment must never be able to take the product down.
    #
    # It stays completely inert - no registration, and above all NO monkeypatch
    # in core - until Hermes' own `context.engine` names it; see
    # context_ctl/__init__.py::setup for both switches. The one-line report is
    # printed on purpose: every failure mode here is silent by nature ("not
    # selected", "seam contract failed", "another engine won"), and a silently
    # missing engine is exactly the class of break ACHTUNG__foreign_seams.yaml
    # exists for.
    try:
        from . import context_ctl
        _ctxctl_cfg = _load_manifest_context_ctl_defaults()
        try:
            _ctxctl_cfg = {**_ctxctl_cfg, **(ctx.get_config("context_ctl", {}) or {})}
        except Exception:
            pass
        print("[filetools] " + context_ctl.setup(
            ctx,
            plugin_version=_load_manifest_version(),
            enabled=bool(_ctxctl_cfg.get("enabled", True)),
            # ВСЯ секция, а не только `enabled`: своего доступа к конфигу у `setup` нет, и
            # попытка достать параметр из `ctx` уже стоила молча неприменённой настройки —
            # роль дистиллята, живой прогон 2026-09-22.
            config=_ctxctl_cfg,
        ))
        ctx.register_command(
            "filetools-context-status",
            handler=context_ctl.format_status,
            description="Состояние движка контроля контекста (ctxctl): выбран ли конфигом, "
                        "встал ли мост к живому agent, виден ли системный промпт, "
                        "доходили ли вызовы compress().",
        )
        # `args_hint` вернулся 2026-09-08 после уточнения владельца: ответ команды НЕ
        # съедается, он приходит и Hermes убирает его через несколько секунд как временное
        # уведомление. Прежняя версия («его глотает `contextlib.suppress` в
        # tui_gateway/methods_tools.py::_dispatch_plugin») была ошибочной, и снятие hint
        # ничего не лечило. Настоящее следствие другое: раз уведомление исчезает, исход
        # применения обязан читаться и ПОТОМ — поэтому команда пишет отчёт в статус движка,
        # а `/filetools-context-status` его показывает.
        ctx.register_command(
            "ctx-drop",
            handler=context_ctl.handle_drop,
            args_hint="<Operation ID через запятую>",
            description="Выбросить операции из контекста заглушкой: /ctx-drop 0156,0157 "
                        "(номера — Operation ID из ответов тулов). Без аргументов — "
                        "показать, что отмечено и сколько токенов освободится, ничего "
                        "не меняя.",
        )
        # Компакция (Plan16). Отдельная команда, а не режим `/ctx-drop`: у неё другой вход
        # (адреса СООБЩЕНИЙ, не номера операций), другой отбор и вызов модели посередине.
        # Без аргументов — предпросмотр, ничего не меняющий: правка необратима.
        ctx.register_command(
            "ctx-compact",
            handler=context_ctl.handle_compact,
            args_hint="<доля: 0.5 | 50% — или адреса: m0001-m0010,m0012 | all | status>",
            description="Сжать часть сессии в дистиллят: /ctx-compact 0.5 — половина "
                        "сессии, считая по сообщениям ИЛИ по объёму (что больше), рез "
                        "ложится на реплику человека и никогда не рвёт пару "
                        "вызов/результат. Можно и адресами: /ctx-compact m0001-m0010 "
                        "(те же адреса, что показывает зеркало промпта), `all` — вся "
                        "сессия. Без аргументов — показать, что было бы сжато, не трогая "
                        "ни модель, ни базу. Сама правка идёт В ФОНЕ и занимает минуты: "
                        "`/ctx-compact status` покажет, как идёт и что поглощено.",
        )
        # Холодный опрос по нитям (Plan16, `Vision08`). В базу НЕ пишет: первый контур
        # проверяет КАЧЕСТВО опроса, и мешать это с необратимой правкой нельзя — иначе
        # каждый прогон стоил бы сессии. Лимиты и запись — следующим шагом.
        ctx.register_command(
            "ctx-distill",
            handler=context_ctl.handle_distill,
            args_hint="<адреса сообщений или пусто = вся сессия>",
            description="Разобрать сессию по нитям: темы, главные и филер, по каждой теме "
                        "«пришли / открыто / клей», документ собирается кодом и ложится "
                        "файлом. Идёт минуты, отчёт пишется после каждого ответа. Ничего "
                        "не меняет ни в базе, ни в контексте.",
        )
    except Exception as _ctxctl_err:
        print(f"[filetools] ctxctl: не загрузился ({_ctxctl_err}); "
              "файловые тулы это не затрагивает")

    ctx.register_command(
        "filetools-reload",
        handler=_handle_reload,
        description="Dev-only: force Hermes to re-scan/reload ALL plugins in this "
                     "process, so on-disk edits to hermes-filetools take effect "
                     "without restarting/re-entering the chat.",
    )
