"""Core CLI logic for get_codeblock.

KNOWN PERFORMANCE ISSUE (not fixed, deliberately deferred — see escalate.py's module
docstring for measured numbers): every `handler.get_blocks()`/`line_level()` call
(and every `reader/classify.py` entry point: `filler_container_at`, `ladder_at`,
`line_level_at`, `outline_rows`, `classify_file`) re-reads the file from disk and
re-runs a full tree-sitter parse — nothing is cached across calls within one process.
A batch `--line a,b,c` already paid for this once per anchor before Vision05 existed;
Vision05's query-escalation (`escalate.py`) adds more such calls when it probes
neighbors. Measured: ~0.8ms/call on a 27-line file, ~75ms/call on a 4747-line file —
noticeable on large files with several escalation steps. The real fix (an in-process
parse-tree cache keyed by file path, shared across all ~7 entry points in
`address.py`+`classify.py`) is a separate, later global-optimization pass — out of
scope here so this change stays reviewable on its own.
"""

import os
import sys
from pathlib import Path

# Bump on any change that affects OUTPUT FORMAT (columns, labels, flag semantics) —
# gcb now has real external consumers (Hermes agents), so the CLI contract is no
# longer a private implementation detail. See CONTRACT.md for what's covered.
VERSION = "0.8.0"


def normalize_path(p):
    """Normalize path separators to forward slashes."""
    if not p:
        return p
    return p.replace('\\', '/')


def is_absolute_path(p):
    """Check if path is absolute (Unix or Windows style)."""
    if Path(p).is_absolute():
        return True
    # Handle Windows drive letters like C:/ or Y:/ on non-Windows systems
    return bool(p and len(p) >= 2 and p[1] == ':')


def _same_file(a, b):
    try:
        return a.samefile(b)
    except OSError:
        return False


def load_config():
    """Load CONFIG__TOOLS.py if available."""
    try:
        from CONFIG__TOOLS import PROJECT_ROOT
        return {'PROJECT_ROOT': PROJECT_ROOT}
    except ImportError:
        return None


def parse_args():
    """Manually parse command line arguments from sys.argv."""
    config = load_config()
    default_root = config['PROJECT_ROOT'] if config else None

    tokens = sys.argv[1:]

    file_path = None
    line_list = None       # --line, always a list once given (len 1 == the old scalar)
    level_list_raw = None  # --level, as given (before array-broadcast against --line)
    ancestor_list_raw = None  # --ancestor-level, as given (before broadcast; wins over --level)
    name = None    # --name NAME: resolved to --line(s), then the existing render (name_resolver)
    query = False  # flag, no value needed
    outline = False  # flag: print the file's structural outline (no --line needed)
    numbered = False  # flag: prefix --query code lines with absolute line numbers
    dot = False  # flag: reader .0 universal map (IR: landmarks + filler-полосы + frames)
    depth = 0    # for --dot: how many landmark levels to expand
    force = False  # flag (Vision05): skip query-escalation, exact requested range only
    # --project-root omitted -> None here; main() then falls back to CONFIG__TOOLS.PROJECT_ROOT as
    # the FIRST base for a relative --file, cwd second (Vision08 §5 — the agent often starts
    # OUTSIDE the project and must not type long absolute paths). The REQ-002-A risk (a file that
    # coincidentally exists under the root silently outranking the one meant relative to cwd) is
    # handled there: a hit in BOTH places -> the root wins but a one-line warning names both.
    project_root = None

    i = 0
    while i < len(tokens):
        token = tokens[i]

        if token == '--file' and i + 1 < len(tokens):
            file_path = tokens[i + 1]
            i += 2
        elif token == '--line' and i + 1 < len(tokens):
            # Batch coordinates: --line 1827,606,867 resolves all of them in one file
            # parse (grep hands you many at once). A single number is just a 1-element
            # array — no behavior change for the existing single-line callers.
            try:
                line_list = [int(t) for t in tokens[i + 1].split(',')]
            except ValueError:
                print(f"Error: --line requires an integer or comma-separated integers", file=sys.stderr)
                sys.exit(1)
            i += 2
        elif token == '--name' and i + 1 < len(tokens):
            name = tokens[i + 1]
            i += 2
        elif token == '--level' and i + 1 < len(tokens):
            try:
                level_list_raw = [int(t) for t in tokens[i + 1].split(',')]
            except ValueError:
                print(f"Error: --level requires an integer or comma-separated integers", file=sys.stderr)
                sys.exit(1)
            i += 2
        elif token == '--query':
            query = True
            i += 1
        elif token == '--outline':
            outline = True
            i += 1
        elif token == '--dot':
            dot = True
            i += 1
        elif token == '--depth' and i + 1 < len(tokens):
            try:
                depth = int(tokens[i + 1])
            except ValueError:
                print("Error: --depth requires an integer value", file=sys.stderr)
                sys.exit(1)
            i += 2
        elif token == '--numbered':
            numbered = True
            i += 1
        elif token == '--force':
            force = True
            i += 1
        elif token in ('--ancestor-level', '--ancestor_level') and i + 1 < len(tokens):
            # Self-documenting relative address: N ancestors up from the block at
            # --line (0 = that block itself, 1 = parent, ...). Sugar for --level -N;
            # the underlying mechanic (negative level) is unchanged. Array-capable in
            # lockstep with --line (see _broadcast below).
            try:
                ancestor_list_raw = [int(t) for t in tokens[i + 1].split(',')]
            except ValueError:
                print("Error: --ancestor-level requires an integer or comma-separated integers >= 0", file=sys.stderr)
                sys.exit(1)
            if any(a < 0 for a in ancestor_list_raw):
                print("Error: --ancestor-level must be >= 0 (0=self, 1=parent, ...)", file=sys.stderr)
                sys.exit(1)
            i += 2
        elif token in ('--project-root', '--project_root') and i + 1 < len(tokens):
            value = tokens[i + 1]
            if ' --' in value or '--line' in value or '--file' in value or '--level' in value or '--query' in value:
                print(f"Error: --project-root incorrect: {value}", file=sys.stderr)
                sys.exit(1)
            if value == "@":
                # Explicit alias -> CONFIG__TOOLS.PROJECT_ROOT (Vision01__path-and-flag-conventions.md).
                # Everything else about this tool's resolution is untouched on purpose.
                if not default_root:
                    print("Error: --project-root @ requires CONFIG__TOOLS.PROJECT_ROOT, but it isn't set.", file=sys.stderr)
                    sys.exit(1)
                value = default_root
            project_root = value
            i += 2
        elif token == '--help':
            print(f"get_codeblock (gcb) v{VERSION}")
            print("Search or query an exact code block from a given line, at a given depth (--level).")
            print("")
            print("Usage:")
            print("  get_codeblock.py --file PATH                                   (defaults to --outline)")
            print("  get_codeblock.py --file PATH [--level MAXDEPTH] --outline")
            print("  get_codeblock.py --file PATH --line N --level K --outline     (focus: one block's own map)")
            print("  get_codeblock.py --file PATH --line N[,N,...] [--ancestor-level N | --level N] [--query]")
            print("  get_codeblock.py --file PATH --name NAME [--outline]           (a declared name -> its --line)")
            print("")
            print("Arguments:")
            print("  --project-root PATH Base to try first for a relative --file, before falling back to")
            print("                      cwd. Not given -> CONFIG__TOOLS.PROJECT_ROOT (if set), then cwd;")
            print("                      found in both (different files) -> root wins + a warning.")
            print("                      '@' -> explicitly CONFIG__TOOLS.PROJECT_ROOT.")
            print("  --file PATH         Path to file (absolute or relative). Code + Markdown (.md).")
            print("  --line N[,N,...]    Target line number(s), 1-based. One file parse resolves them")
            print("                      all. >1 line switches to batch mode: bare = survey (one merged")
            print("                      map); with --outline = one merged tree; with --query = one")
            print("                      BLOCK per resolved range, merging touching/nested ones.")
            print("  --name NAME         Find a declared name in the file and use its line(s) as --line:")
            print("                      one exact hit -> --query (with --outline: the map inside it);")
            print("                      several, or only close ones -> --outline per candidate, so the")
            print("                      next call is an exact --line. Quote C++ names: \"Foo<T>::operator<<\".")
            print("                      `::`/`.` both qualify (Foo::bar = Foo.bar); `*`/`?` = a family")
            print("                      (\"ggml_rope_*\"). Not exact -> case / substring / words / typo,")
            print("                      never a block. The first output line says which. Exit 2 = nothing.")
            print("  --ancestor-level N[,N,...]  Which block at --line: N ancestors up. 0 = the innermost")
            print("                      block itself (default), 1 = its parent, 2 = grandparent, ...")
            print("  --level N[,N,...]   Absolute depth address instead: 1 = file top, 2 = one level in.")
            print("                      (With --outline, --level N caps the depth shown — not an address.)")
            print("                      Arrays broadcast against --line: one value replicates to all;")
            print("                      shorter repeats its last value; longer has its excess ignored.")
            print("                      Combined with --line and --outline (FOCUS mode): the table of")
            print("                      contents of just the named block K ancestors up from line N")
            print("                      (anonymous try/if/for wrappers don't count as a step). Real use:")
            print("                      you grepped/landed on a line deep inside some control flow and")
            print("                      don't know what function/class it's even in —")
            print("                      `--line 49 --ancestor-level 1 --outline` shows the outline of")
            print("                      that containing named function. Works with an array of hits too:")
            print("                      `--line 44,62 --ancestor-level 2 --outline` escalates EACH hit the same")
            print("                      way and merges shared ancestors into one tree.")
            print("  --outline           Print the structural map (named blocks only). No --line")
            print("                      needed. Default mode when --file is given alone. Bare = an")
            print("                      overview sized to the file; --level N caps depth (high N = all).")
            print("  --query             Return the block TEXT (framed by anchors) instead of the ladder.")
            print("  --numbered          With --query: prefix each code line with its absolute line")
            print("                      number ('  92 | ...'). Off by default — raw text stays")
            print("                      copy/diff-safe; the range header already gives the numbers.")
            print("  --force             Guarantees the exact requested range, ignoring context length —")
            print("                      small (Vision05: --query skips escalating a too-small result)")
            print("                      or large (Vision04: --outline skips shrinking a huge one).")
            print("")
            print("Two ways to pick a block at --line (don't mix):")
            print("  --ancestor-level N  relative — walk N blocks up from where the line lands (0=here).")
            print("  --level N           absolute — jump to depth N counted from the file top.")
            print("  The ladder's level number is the block's real depth (1 = file top, deeper = higher);")
            print("  --query's own ■BLOCK : A-B never states one (a merged run lists each real level")
            print("  in a ' = ranges : Level L  A-B, ...' tail instead of one number for the whole span).")
            print("")
            if default_root:
                print(f'CONFIG__TOOLS.PROJECT_ROOT="{default_root}" (use --project-root @ to apply it)')
            sys.exit(0)
        else:
            # A silently-skipped stray token used to hide a real bug: PowerShell
            # expands an unquoted `a,b, c` (comma THEN space) into separate argv
            # entries (`a`, `b`, `c`) instead of one string — `--line` then only
            # sees its immediate next token as a 1-element array, and the rest
            # landed here and vanished, quietly falling back to single-line mode
            # instead of erroring. Fail loud instead: this token belongs to nothing.
            hint = (" (PowerShell splits an unquoted 'a,b, c' on the space after the "
                    "comma into separate arguments — quote the whole list: "
                    f"--line \"{token}\")") if token[0].isdigit() or token[0] == '-' and token[1:].isdigit() else ""
            print(f"Error: unrecognized argument: '{token}'{hint}", file=sys.stderr)
            sys.exit(1)

    # Normalize paths
    if file_path:
        file_path = normalize_path(file_path)
    if project_root:
        project_root = normalize_path(project_root)

    # --file alone (no --line, no --outline) defaults to --outline: it's the primary
    # discovery mode, and requiring the flag explicitly here would be pure friction.
    # The flag itself still works and stays documented for explicit use.
    if file_path and line_list is None and name is None and not outline and not dot:
        outline = True

    # No arguments or missing required ones: show usage hint
    if not file_path and line_list is None:
        _y = "\033[93m" if sys.stdout.isatty() else ""
        _r = "\033[0m" if sys.stdout.isatty() else ""
        print("Search or query an exact code block from a given line, at a given depth (--level).")
        print(f"{_y}Usage:")
        print("  get_codeblock.py --file PATH                          (defaults to --outline)")
        print("  get_codeblock.py --file PATH [--level MAXDEPTH] --outline")
        print(f"  get_codeblock.py --file PATH --line N[,N,...] [--level LEVEL] [--query]{_r}")
        print("Run with --help for full options, including --level addressing.")
        print("")
        if default_root:
            print(f"CONFIG__TOOLS.PROJECT_ROOT={default_root} (use --project-root @ to apply it)")
        sys.exit(0)

    # --outline / --dot need only --file; every other mode needs --file and --line
    if not file_path or (line_list is None and name is None and not outline and not dot):
        need = "--file" if (outline or dot) else "--file, --line (or --name)"
        print(f"Error: the following arguments are required: {need}", file=sys.stderr)
        sys.exit(1)

    # Array broadcast (--line 1827,606,867 with --level/--ancestor-level): one number
    # replicates across the whole array; a shorter array replicates its last value for
    # the remainder; a longer array has its excess ignored. --ancestor-level wins over
    # --level when both are given (same precedence as the old scalar code: whichever
    # was parsed decides `level`; here that's whichever list is non-None).
    n_lines = len(line_list) if line_list else 1

    def _broadcast(raw, n):
        if not raw:
            return None
        if len(raw) >= n:
            return raw[:n]
        return raw + [raw[-1]] * (n - len(raw))

    if ancestor_list_raw is not None:
        levels = [-a for a in _broadcast(ancestor_list_raw, n_lines)]
    elif level_list_raw is not None:
        levels = _broadcast(level_list_raw, n_lines)
    else:
        levels = [0] * n_lines

    return {
        'file': file_path,
        'line': line_list[0] if line_list else None,   # back-compat single-value view
        'lines': line_list,                              # full array (len 1 for scalar calls)
        'level': levels[0],                               # back-compat single-value view
        'levels': levels,                                 # full array, broadcast to len(lines)
        'query': query,
        'outline': outline,
        'numbered': numbered,
        'dot': dot,
        'depth': depth,
        'force': force,
        'name': name,
        'project_root': project_root
    }, config


def read_lines(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        return f.readlines()


def _innermost_unambiguous_idx(blocks):
    """Index of the innermost rung whose level is held by exactly one entry.

    Sibling control clauses that share one physical brace line (`} else {`,
    `} catch (e) {`) land at the SAME level — both genuinely touch the hit line,
    and neither is "more inner" than the other. A duplicate level means "this
    line sits exactly on the shared boundary of two siblings": there is no single
    innermost block to name, so climb past the whole tied run to the nearest
    level held by one rung (their common parent)."""
    idx = len(blocks) - 1
    while idx > 0 and sum(1 for b in blocks if b['level'] == blocks[idx]['level']) > 1:
        idx -= 1
    return idx


def resolve(blocks, level):
    """Resolve block by level address.

    blocks: list sorted outermost-first [0=outermost/top-level, N-1=innermost]

    Level addressing (Vision contract):
      0   = current block (innermost UNAMBIGUOUS block containing the line;
            see `_innermost_unambiguous_idx` for the shared-brace-line case)
     -N   = N steps up from there to parent blocks
      +N  = N-th level from top of hierarchy (1=topmost, 2=next inner...)
    """
    if not blocks:
        return None

    n = level
    base = _innermost_unambiguous_idx(blocks)

    if n == 0:
        return blocks[base]

    elif n < 0:
        # Negative: relative to the unambiguous base, going up (-1=parent)
        idx = base + n
        return blocks[0] if idx < 0 else blocks[idx]

    else:
        # Positive: from top of hierarchy (1=topmost containing block)
        idx = n - 1
        return blocks[min(idx, len(blocks) - 1)]


def make_comment_delims(language):
    """(open, close) comment delimiters for the tool's own metadata lines, so those
    lines are valid comments in the target language and don't collide with its syntax.

    Markdown needs a CLOSED HTML comment `<!-- … -->` — a leading `#` would render as
    an H1 heading. The line-comment languages just get a prefix and an empty closer.
    """
    return {"python": ("#", ""), "typescript": ("//", ""), "tsx": ("//", ""),
            "csharp": ("//", ""), "cpp": ("//", ""), "css": ("//", ""),
            "markdown": ("<!-- ", " -->"), "text": ("#", ""),
            "yaml": ("#", ""), "shell": ("#", ""), "powershell": ("#", ""), "batch": ("REM ", "")}.get(language, ("#", ""))


def get_codeblock(file_path: str, line_num: int = 1, level: int = 0, query: bool = False) -> dict:
    """Importable function to get code block metadata (and optionally text).

    Args:
        file_path: Path to source file (absolute or relative)
        line_num: Target line number (1-based)
        level: Block address level (0=current, -N=parents, +N=from top)
        query: If True, also return block text

    Returns dict with keys:
        level   : int — real depth level of returned block
        start   : int — start line number (1-based)
        end     : int — end line number (1-based, inclusive)
        text    : str — block content byte-for-byte (only if query=True)

    Raises:
        FileNotFoundError: file doesn't exist
        ValueError: line out of range or no blocks found
    """
    # Read file
    try:
        lines = read_lines(file_path)
    except FileNotFoundError:
        raise FileNotFoundError(f"File not found: {file_path}")

    if line_num < 1 or line_num > len(lines):
        raise ValueError(f"Line {line_num} out of range (1-{len(lines)})")

    # Detect language by extension
    ext = Path(file_path).suffix.lower()
    from get_codeblock.reader.reader import language_for_ext
    language = language_for_ext(ext)

    # Get blocks via handler
    from get_codeblock.env_check import ensure_language
    ensure_language(language)  # raises EnvError with install instructions if deps missing
    from get_codeblock.reader.reader import Reader
    handler = Reader.open(file_path, lines, language)
    blocks = handler.get_blocks(file_path, line_num)

    if not blocks:
        raise ValueError("No blocks found")

    # Resolve block by level address
    block = resolve(blocks, level)
    if not block:
        raise ValueError("Level out of range")

    result = {
        "level": block["level"],
        "start": block["start"],
        "end": block["end"],  # inclusive
    }

    # Optionally return text byte-for-byte from file
    if query:
        start_idx = block["start"] - 1  # to 0-based
        end_idx = min(block["end"], len(lines))
        result["text"] = "".join(lines[start_idx:end_idx])

    return result


def get_line_levels(file_path: str, line_nums: list) -> dict:
    """Efficiently get block levels for multiple lines in ONE file parse.

    Designed for callers like find_code_usage that need levels for many
    usage lines in the same file — avoids re-parsing file N times.

    Args:
        file_path: Path to source file (absolute or relative)
        line_nums: List of target line numbers (1-based, can be unsorted/duplicates)

    Returns dict mapping each line_num -> level int (1-based; real levels are 1,2,3,...).
    A line inside no block sits at the file root = level 1 (never 0 — 0 is reserved for
    --level addressing, not a real depth):
        {18: 1, 45: 3, ...}

    Raises:
        FileNotFoundError: file doesn't exist
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except FileNotFoundError:
        raise FileNotFoundError(f"File not found: {file_path}")

    if not line_nums or not lines:
        return {}

    # Detect language by extension
    ext = Path(file_path).suffix.lower()
    from get_codeblock.reader.reader import language_for_ext
    language = language_for_ext(ext)

    # Per-line logical level: level = 1 + enclosing block BODIES (a block header sits
    # at its parent's level). Every handler implements line_level.
    from get_codeblock.env_check import ensure_language
    ensure_language(language)  # raises EnvError with install instructions if deps missing
    from get_codeblock.reader.reader import Reader
    handler = Reader.open(file_path, lines, language)
    return {
        ln: (handler.line_level(lines, ln - 1) if 1 <= ln <= len(lines) else 1)
        for ln in line_nums
    }


TOOL_NAME = "get_codeblock"


def _load_logging_config():
    """Best-effort read of the opt-in call-logging config from CONFIG__TOOLS.py.

    Returns (enabled, log_dir, base). `base` anchors a relative LOG_DIR: config
    schema >= 2 -> this tool's own __HQ (Vision08: our paths hang off the HQ,
    which may live outside the sources); older configs -> PROJECT_ROOT, as
    before. enabled is False whenever CONFIG__TOOLS.py is missing, doesn't list
    this tool, or anything else about reading it goes wrong — logging must never
    be why the tool fails to run."""
    try:
        import CONFIG__TOOLS as c
        schema = getattr(c, "CONFIG_SCHEMA_VERSION", 1) or 1
        # this file is <HQ>/tools/get_codeblock/core.py
        base = Path(__file__).resolve().parents[2] if schema >= 2 else c.PROJECT_ROOT
        return TOOL_NAME in (c.LOG_ENABLED_TOOLS or []), c.LOG_DIR, base
    except Exception:
        return False, None, None


def _log_call(record):
    """Append one JSONL diagnostic line for this invocation (argv/exit_code/
    duration/error — never the code text a call returned). No-op unless this
    tool is listed in CONFIG__TOOLS.LOG_ENABLED_TOOLS. Swallows every error:
    a logging failure must never affect the tool's real behavior or exit code."""
    try:
        enabled, log_dir, project_root = _load_logging_config()
        if not enabled:
            return
        import json
        import time as _time
        log_dir = log_dir or "."
        # A relative LOG_DIR is anchored to the HQ (schema >= 2) or PROJECT_ROOT
        # (older configs), NOT to the process's cwd — this tool is routinely
        # invoked from arbitrary directories, and a cwd-relative log dir would
        # scatter/duplicate log files depending on where the caller stood.
        base = Path(project_root) if project_root and not is_absolute_path(log_dir) else None
        log_path = (base / log_dir if base else Path(log_dir)) / f"{TOOL_NAME}.log.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record.setdefault("ts", _time.strftime("%Y-%m-%dT%H:%M:%S"))
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def main():
    """Thin logging wrapper around `_main_impl` (the actual CLI, unchanged below).
    Kept separate on purpose: logging must never touch/risk the real logic, only
    observe argv in and exit_code/error/duration out."""
    import time as _time
    t0 = _time.time()
    record = {"tool": TOOL_NAME, "version": VERSION, "argv": sys.argv[1:]}
    exit_code = 0
    try:
        _main_impl()
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        raise
    except BaseException as e:
        exit_code = 1
        record["error"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        record["exit_code"] = exit_code
        record["duration_ms"] = round((_time.time() - t0) * 1000, 2)
        _log_call(record)


def _main_impl():
    # Windows-консоль (cp1251/1252) роняет print на не-ASCII (×, кириллица, emoji).
    # utf-8 + replace: не падаем; на не-utf8 консоли максимум косметический мохито.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

    args, config = parse_args()

    # Resolve file path (Vision08 §5): absolute -> as is; relative -> first the root (explicit
    # --project-root, else CONFIG__TOOLS.PROJECT_ROOT), then cwd. Resolved from the root -> the
    # path becomes absolute, so the 'File:' header shows exactly what was opened.
    file_path = args['file']
    base = args.get('project_root') or (config or {}).get('PROJECT_ROOT')
    if not is_absolute_path(file_path) and base:
        resolved = Path(base) / file_path
        if resolved.is_file():
            here = Path(file_path)
            if here.is_file() and not _same_file(here, resolved):
                print(f"Warning: '{file_path}' exists both under the project root and under cwd — "
                      f"using {resolved}; pass an absolute path for the other one.", file=sys.stderr)
            file_path = str(resolved)

    try:
        lines = read_lines(file_path)
    except FileNotFoundError:
        print(f"Error: File not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    ext = Path(file_path).suffix.lower()
    from get_codeblock.reader.reader import language_for_ext
    language = language_for_ext(ext)

    # Preflight: if this language needs tree-sitter packages that aren't installed,
    # print exactly what to install (from requirements.txt) instead of a traceback.
    from get_codeblock.env_check import ensure_language, EnvError
    try:
        ensure_language(language)
    except EnvError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    # Preflight: is THIS extension registered at all (any profile/backend)? `language`
    # above is core.py's own best-guess mapping (defaults to 'python' for anything it
    # doesn't recognize), so it can't catch this — only `registry.resolve(ext)` knows.
    # Without this check, an unsupported extension (e.g. `.rs`, `.go`, `.yaml`) reached
    # `registry.resolve` deep inside outline/get_blocks and raised a raw `ValueError`
    # traceback instead of a clean message.
    from get_codeblock.reader.registry import resolve as _resolve_format
    try:
        _resolve_format(ext)
    except ValueError:
        print(f"Error: file format '{ext or '(no extension)'}' is not supported yet "
              "(no reader profile registered for it).", file=sys.stderr)
        sys.exit(1)

    # Единая точка входа приложения (Vision03): проверенные режимы делегируются
    # хендлеру внутри Reader (паритет), новый .0 — там же. core.py в get_handler
    # напрямую больше не ходит.
    from get_codeblock.reader.reader import Reader
    handler = Reader.open(file_path, lines, language)
    from get_codeblock import views
    src = views.Source(file_path, lines, language, handler)
    c = src.c
    is_tty = sys.stdout.isatty()

    def emit(s):
        print(f"\033[93m{s}\033[0m" if is_tty else s)

    def out(rendered):
        """Print one view's picture: tool lines through `emit`, file text verbatim."""
        for kind, text in rendered:
            if kind == 'meta':
                emit(text)
            else:
                sys.stdout.write(text)

    # One-line reminder of the two addressing scales (real depth vs --level). Console-only
    # (is_tty) — external/programmatic callers get bare block lines, nothing extra to parse.
    # Still wrapped in c() like every other line: a human may paste this straight into a
    # code file along with the block below it, and an uncommented line would break there.
    def emit_legend():
        if is_tty:
            legend_text = ("Ladder's level number = real depth (1=file top, deeper=higher). Pick a block: "
                            "--ancestor-level N = N up from here (0=this block, 1=parent) · "
                            "--level N = absolute depth from top · --query = its text.")
            print(f"\033[92m{c(legend_text)}\033[0m")

    # --name: NOT a mode — the name becomes --line(s) and the existing render does the rest:
    # one exact hit -> `--line N --query` (or `--outline` inside it when --outline is given);
    # several / only close ones -> `--line N1,N2,… --outline` (a ladder per candidate, so the
    # next call is an exact --line). The only new output is the header line.
    if args.get('name'):
        from get_codeblock.name_resolver import resolve_name, header_line
        res = resolve_name(file_path, args['name'])
        emit(c(header_line(res)))
        if not res.hits:
            sys.exit(2)
        found = [h.line for h in res.hits]
        lvl = (args.get('levels') or [0])[0]
        args['lines'], args['line'] = found, found[0]
        args['levels'], args['level'] = [lvl] * len(found), lvl
        if not (res.exact and len(found) == 1):
            args['outline'], args['query'] = True, False
        elif not args.get('outline'):
            args['query'] = True

    # --outline / --dot: единый адаптивный рендер поверх `.0` IR (Vision03).
    #   --outline — чистая карта: landmark'и вглубь, filler только на уровне файла.
    #   --dot     — тот же рендер, но filler на ВСЕХ раскрытых уровнях (диагностика).
    # Глубина адаптивная (по размеру файла); --level N / --depth N — явный потолок.
    lines_batch = args.get('lines') or []
    levels_batch = args.get('levels') or []
    is_batch = len(lines_batch) > 1

    if args.get('outline') or args.get('dot'):
        deep = bool(args.get('dot'))
        if not hasattr(handler, 'outline'):
            print(f"Error: outline not supported for {language} yet", file=sys.stderr)
            sys.exit(1)
        if is_batch:
            out(views.render_outline_batch(
                views.outline_batch_view(src, lines_batch, levels_batch, deep), src))
            return
        # Map mode does not take --line. If both are given the user meant to inspect a
        # line — карта ТОЛЬКО блока-цели: K-предок строки (K=--level, по умолч. внутренний).
        # Решает монстро-файлы/классы: развернуть один блок как отдельный файл, рекурсивно.
        try:
            view = views.outline_view(src, line=args.get('line'), level=args.get('level') or 0,
                                      deep=deep, depth=args.get('depth') or 0)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        # Help line ALWAYS on top. Actionable hints are HUMAN guidance, not part of
        # the tool's output contract: console-only (is_tty), so programmatic callers
        # get clean block lines. Teach the mode map explicitly: here --level = depth
        # cap; --line/--query are OTHER modes (never combined with --outline).
        if is_tty and view['rows']:
            g, r = "\033[92m", "\033[0m"
            capflag = "--depth N" if deep else "--level N"
            cap = f"{capflag} caps depth (raise N for the full tree)" if view['shown'] < view['depth'] \
                  else f"{capflag} caps depth"
            kind = ".0 map (all levels: named + filler)" if deep else "outline (map) mode"
            hint2 = ("to read code, drop the map flag: `--line N` = block bounds at a line "
                     "· `--line N --query` = that block's text")
            print(f"{g}{c(f'{kind} · {cap}')}{r}")
            print(f"{g}{c(hint2)}{r}")
        out(views.render_outline(view, src))
        return

    # Ladder/query: ALWAYS the batch renderer, even for a single --line. Two paths
    # that render "the same thing" slightly differently is exactly the kind of
    # inconsistency that reads as a bug to anyone gluing this output downstream —
    # one --line is just a 1-element array, not a different mode.
    emit_legend()
    if args['query']:
        view = views.query_view(src, lines_batch, levels_batch, force=bool(args.get('force')))
        out(views.render_query(view, src, args.get('numbered')))
        if view['errors']:
            sys.exit(1)
    else:
        out(views.render_ladder(views.ladder_view(src, lines_batch), src))

if __name__ == "__main__":
    main()
