"""Reading C/C++ source RIGHT: macros tree-sitter cannot know, cut before parsing.

One implementation for every tool (get_codeblock reader, the card stamp, find_code_usage):
  - export / attribute macros (`GGML_API`, `GGML_ATTRIBUTE_FORMAT(1, 2)`) -> blanked;
  - wrapper macros (`GGML_DEPRECATED(decl, "hint")`) -> unwrapped to `decl`;
  - CUDA/HIP qualifiers (`__device__`, `__launch_bounds__(…)`) -> blanked, always.
Every change replaces characters with spaces, so line AND column numbers of the original file
hold (addressing reads rows; `--query` prints the ORIGINAL lines, never this text).

Which macros: the caller's list (a project's `CPP_STRIP_MACROS` / `CPP_WRAPPER_MACROS`) plus
`detect_macros` — what the file's own `#define`s say (`#define DEPRECATED(func, hint) func
__attribute__((deprecated(hint)))` is a wrapper, `#define LLAMA_API __declspec(dllexport)` is an
attribute). A macro is touched only when EVERY definition of it agrees (no real code in any).
Without this, `ggml.h` (`GGML_DEPRECATED(...)`) and `llama.h` (`DEPRECATED(...)`) parse into
one bogus node spanning a thousand lines.
"""
import hashlib
import re
from typing import Dict, Iterable, List, Tuple

# CUDA/HIP declaration qualifiers tree-sitter-cpp does not know — always cut (harmless in plain
# C/C++: nobody else names things like this). `__launch_bounds__(...)` goes with its arguments.
BUILTIN_STRIP = ["__device__", "__host__", "__global__", "__forceinline__", "__noinline__",
                 "__shared__", "__constant__", "__managed__", "__restrict__", "__launch_bounds__"]

# words a declaration decorator may consist of (after attributes are removed)
_DECOR_WORDS = {"extern", "static", "inline", "__inline", "__inline__", "__forceinline",
                "restrict", "__restrict", "__restrict__", "_Noreturn", "noexcept", "constexpr",
                "__cdecl", "__stdcall", "__fastcall", "__vectorcall", "WINAPI", "APIENTRY"}
_ATTR_RE = re.compile(r"__attribute__\s*\(\(|__declspec\s*\(|\[\[")
_DEFINE_RE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)(\([^)]*\))?(.*)$", re.M)
_TOKEN_RE = re.compile(r"[A-Za-z_]\w*|\S")


def _drop_attributes(body: str) -> str:
    """Remove `__attribute__((…))`, `__declspec(…)`, `[[…]]` (balanced)."""
    out, pos = [], 0
    while True:
        m = _ATTR_RE.search(body, pos)
        if not m:
            out.append(body[pos:])
            return "".join(out)
        out.append(body[pos:m.start()])
        i, depth = m.end(), m.group(0).count("(") + m.group(0).count("[")
        while i < len(body) and depth:
            depth += body[i] in "(["
            depth -= body[i] in ")]"
            i += 1
        pos = i


def detect_macros(src: str) -> Tuple[List[str], List[str]]:
    """(strip, wrappers) from the file's own `#define`s. Conservative: a macro counts only if
    every one of its definitions (per `#if` branch) classifies the same way."""
    text = re.sub(r"\\\r?\n", " ", src)                 # continuation lines -> one logical line
    kinds: Dict[str, set] = {}
    defs = []
    for m in _DEFINE_RE.finditer(text):
        name, params, body = m.group(1), m.group(2), m.group(3)
        body = re.sub(r"/\*.*?\*/|//.*$", " ", body)
        defs.append((name, [p.strip() for p in params[1:-1].split(",")] if params else None,
                     _drop_attributes(body)))
    strip = set()
    for _ in range(3):                                   # macros made of other stripped macros
        kinds.clear()
        for name, params, body in defs:
            toks = [t for t in _TOKEN_RE.findall(body) if t not in _DECOR_WORDS and t not in strip]
            if params is None:
                kind = "strip" if not toks else "code"
            elif not toks:
                kind = "strip"                           # attribute-only, args dropped with it
            elif params and toks == [params[0]]:
                kind = "wrap"
            else:
                kind = "code"
            kinds.setdefault(name, set()).add(kind)
        new = {n for n, k in kinds.items() if k == {"strip"}}
        if new == strip:
            break
        strip = new
    wrappers = sorted(n for n, k in kinds.items() if k == {"wrap"})
    return sorted(strip), wrappers


def used_in_code(src: str, names: Iterable[str]) -> set:
    """Which of `names` occur on a CODE line (not inside a directive) — a decorator in use, as
    opposed to a build flag that only ever appears in `#if` (`#define FP16_AVAILABLE`)."""
    names = set(names)
    if not names:
        return set()
    rx = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\b")
    found, in_directive = set(), False
    for ln in src.split("\n"):
        directive = in_directive or ln.lstrip().startswith("#")
        in_directive = directive and ln.rstrip("\r").endswith("\\")
        if not directive:
            found.update(rx.findall(ln))
    return found


def strip_macros(src: str, names: Iterable[str], wrappers: Iterable[str] = ()) -> str:
    """Blank out export/attribute macros (`GGML_API`, and with its arguments when called on the
    same line: `GGML_ATTRIBUTE_FORMAT(1, 2)`) and unwrap function-like wrapper macros
    (`GGML_DEPRECATED(decl, "hint")` -> `decl`), keeping every newline so line numbers hold."""
    names = list(names)
    if names:
        # Only CODE lines: inside a directive (`#define GGML_API extern`) the macro is being
        # DEFINED — blanking it there would turn the line into `#define extern`.
        rx = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\b"
                        r"(?:\s*\([^()]*(?:\([^()]*\)[^()]*)*\))?")
        lines, in_directive = src.split("\n"), False
        for i, ln in enumerate(lines):
            directive = in_directive or ln.lstrip().startswith("#")
            in_directive = directive and ln.rstrip("\r").endswith("\\")
            if not directive:
                lines[i] = rx.sub(lambda m: " " * len(m.group(0)), ln)
        src = "\n".join(lines)
    for w in wrappers:
        src = _unwrap(src, w)
    return src


def _unwrap(src: str, name: str) -> str:
    rx = re.compile(r"\b" + re.escape(name) + r"\s*\(")
    out, pos = [], 0
    for m in rx.finditer(src):
        if m.start() < pos:
            continue
        line_start = src.rfind("\n", 0, m.start()) + 1
        if src[line_start:m.start()].lstrip().startswith("#"):
            continue                                     # the macro's own #define
        i, depth, comma = m.end(), 1, None
        while i < len(src) and depth:
            ch = src[i]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif ch == "," and depth == 1 and comma is None:
                comma = i
            i += 1
        if depth:
            break
        close = i - 1
        arg_end = comma if comma is not None else close
        blank = lambda s: re.sub(r"[^\n]", " ", s)
        out.append(src[pos:m.start()])
        out.append(blank(src[m.start():m.end()]))
        out.append(src[m.end():arg_end])
        out.append(blank(src[arg_end:close + 1]))
        pos = close + 1
    out.append(src[pos:])
    return "".join(out)


def prepare(src: str, strip: Iterable[str] = (), wrappers: Iterable[str] = ()) -> str:
    """The one C/C++ source preparation: builtin + the caller's macros + the file's own."""
    auto_strip, auto_wrap = detect_macros(src)
    names = list(dict.fromkeys(BUILTIN_STRIP + list(strip) + auto_strip))
    wraps = list(dict.fromkeys(list(wrappers) + auto_wrap))
    return strip_macros(src, names, wraps)


_BODY_OPENER = re.compile(
    r"\)\s*(?:(?:const|noexcept|override|final|volatile|mutable|&&|&)\s*)*"
    r"(?:noexcept\s*\([^()]*\)\s*)?(?:->\s*[\w:<>,\s*&]+?)?\s*$")


def _blank_bodies_impl(text: str, only=None) -> str:
    """Empty the inside of every function body `) {...}` (newlines kept). The declared surface
    never needs a body, and bodies are where macro-generated code (X-macros without `;`) makes
    tree-sitter give up on the WHOLE function. Comments, strings, char literals, raw strings and
    preprocessor lines are skipped so their braces don't count."""
    out = list(text)
    n, i = len(text), 0
    line_start = True

    def skip_ws_comment_string(j):
        """Advance over one non-code unit starting at j; return new j or None if code char."""
        c = text[j]
        if text.startswith("//", j):
            k = text.find("\n", j)
            return n if k == -1 else k
        if text.startswith("/*", j):
            k = text.find("*/", j + 2)
            return n if k == -1 else k + 2
        if c == "R" and j + 1 < n and text[j + 1] == '"':
            m = re.match(r'R"([^()\\\s]{0,16})\(', text[j:j + 20])
            if m:
                end = text.find(")" + m.group(1) + '"', j)
                return n if end == -1 else end + len(m.group(1)) + 2
        if c in "\"'":
            k = j + 1
            while k < n and text[k] != c:
                if text[k] == "\\":
                    k += 1
                elif text[k] == "\n":
                    break
                k += 1
            return k + 1
        return None

    def skip_preproc(j):
        while j < n:
            k = text.find("\n", j)
            if k == -1:
                return n
            if text[k - 1] == "\\" or (k >= 2 and text[k - 2:k] == "\\\r"):
                j = k + 1
                continue
            return k
        return n

    while i < n:
        c = text[i]
        if line_start and c in " \t":
            i += 1
            continue
        if line_start and c == "#":
            i = skip_preproc(i)
            continue
        line_start = c == "\n"
        j = skip_ws_comment_string(i)
        if j is not None:
            i = j
            continue
        if c == "{" and _BODY_OPENER.search(text[max(0, i - 300):i]) and (only is None or i in only):
            depth, k, ls = 1, i + 1, False
            while k < n and depth:
                ch = text[k]
                if ls and ch in " \t":
                    k += 1
                    continue
                if ls and ch == "#":
                    k = skip_preproc(k)
                    continue
                ls = ch == "\n"
                jj = skip_ws_comment_string(k)
                if jj is not None:
                    k = jj
                    continue
                depth += ch == "{"
                depth -= ch == "}"
                k += 1
            for p in range(i + 1, k - 1):
                if out[p] != "\n":
                    out[p] = " "
            i = k
            continue
        i += 1
    return "".join(out)




def blank_function_bodies(text: str) -> str:
    """Empty the inside of every function body (newlines kept) — see _blank_bodies_impl."""
    return _blank_bodies_impl(text)


def _body_openers(text: str):
    """Offsets of every function-body `{` (the same rule the blanker uses), in file order."""
    out = []
    n, i, line_start = len(text), 0, True
    while i < n:
        c = text[i]
        if line_start and c in " \t":
            i += 1
            continue
        if line_start and c == "#":
            k = text.find("\n", i)
            while k != -1 and text[k - 1] == "\\":
                k = text.find("\n", k + 1)
            i = n if k == -1 else k
            continue
        line_start = c == "\n"
        if c == "{" and _BODY_OPENER.search(text[max(0, i - 300):i]):
            out.append(i)
        i += 1
    return out


def blank_body_from_line(text: str, row: int) -> str:
    """Blank ONLY the first function body whose `{` is on line `row` (0-based) or later.
    Returns the text unchanged when there is none."""
    line_off = 0
    for _ in range(row):
        k = text.find("\n", line_off)
        if k == -1:
            return text
        line_off = k + 1
    for o in _body_openers(text):
        if o >= line_off:
            return _blank_bodies_impl(text, only={o})
    return text


# --------------------------------------------------------------------------- reader hook

_PRE_CACHE: Dict[str, bytes] = {}


def _config_macros():
    """`CPP_STRIP_MACROS` / `CPP_WRAPPER_MACROS` of the HQ config next to the tools, if any."""
    try:
        import CONFIG__TOOLS as cfg
    except Exception:
        return [], []
    return (list(getattr(cfg, "CPP_STRIP_MACROS", []) or []),
            list(getattr(cfg, "CPP_WRAPPER_MACROS", []) or []))


RECOVER_ROUNDS = 20


def _recover(text: str) -> str:
    """A function whose body tree-sitter cannot read (X-macros without `;` — ggml-vulkan.cpp
    `ggml_vk_load_shaders`) turns the REST of the file into one top-level ERROR node: the map shows
    nothing after it. Blank that function's body (lines/columns kept; the function itself stays a
    block) and parse again, until no top-level ERROR is left or nothing more can be blanked."""
    try:
        from .handlers.cpp_handler import CPP_SPEC
        parser = CPP_SPEC.parser()
    except Exception:
        return text
    for _ in range(RECOVER_ROUNDS):
        root = parser.parse(text.encode("utf-8")).root_node
        err = next((c for c in root.children if c.type == "ERROR"), None)
        if err is None:
            return text
        new = blank_body_from_line(text, err.start_point[0])
        if new == text:
            return text
        text = new
    return text


def preprocess_bytes(source: bytes) -> bytes:
    """`LangSpec.preprocess` for the C/C++ reader profile (tree-sitter parses the result)."""
    key = hashlib.sha1(source).hexdigest()
    hit = _PRE_CACHE.get(key)
    if hit is not None:
        return hit
    text = source.decode("utf-8", "replace")
    strip, wraps = _config_macros()
    out = _recover(prepare(text, strip, wraps)).encode("utf-8")
    if len(_PRE_CACHE) > 32:
        _PRE_CACHE.clear()
    _PRE_CACHE[key] = out
    return out
