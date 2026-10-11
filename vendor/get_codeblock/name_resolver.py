"""`--name` — a declared name -> line numbers. NOT a mode: the result feeds the existing
addressing (`--line N --query` for one exact hit, `--line N1,N2,… --outline` otherwise), so
the only new output is ONE header line (`header_line`). Everything below that is rendered by
code that already exists and is already tested.

Where names come from: the same reader tree the map (`--outline`) is built from
(`registry.resolve` -> Backend -> RNode, Spec.unwrap_frame/unwrap_def/body) — every landmark,
every declaration inside a filler band (C/C++ prototypes, `#define`, `typedef`, fields,
assignments) and named frames (C++ `namespace`, which qualify what they hold).
The declared name of a node = the end of its `declaration`/`definition`/`name`/`declarator`/
`left` field chain (tree-sitter grammars agree on these across C/C++, Python, TS/JS, C#);
`qualified_identifier` (`Foo::bar`) splits into qualifier + name. No per-language code.

Matching (`resolve_name`):
  exact    — the name (after `normalize`) equals the declared name; a qualified query
             (`Foo::bar`, `Foo.bar`) must also match the trailing qualifiers.
  glob     — `*`/`?` in the query: every declared name that matches (a family, `ggml_rope_*`).
  closest  — only when nothing is exact, ranked: case-insensitive > substring > all words
             (`_`/camelCase/space) > typo (similarity >= TYPO_MIN). Never rendered as a block.
"""
import difflib
import fnmatch
import os
import re
from dataclasses import dataclass, field
from typing import List

from .reader.registry import resolve

TOP = 10              # candidates shown; the rest is counted in the header
TYPO_MIN = 0.8
_FIELDS = ("declaration", "definition", "name", "declarator", "left")
_HOW_ORDER = {"exact": 0, "glob": 0, "case": 1, "substring": 2, "words": 3, "typo": 4}
_FUNC_WORDS = ("function", "method", "constructor", "lambda", "arrow")
_NAME_RE = re.compile(r"^(~?[A-Za-z_$][\w$-]*|operator\S+)$")   # `-`: PowerShell `Get-Thing`


@dataclass
class Decl:
    name: str
    quals: List[str]
    line: int
    end: int = 0          # last line of the declaring node (body / initializer included)


@dataclass
class Hit:
    line: int
    name: str
    how: str


@dataclass
class Result:
    query: str
    hits: List[Hit] = field(default_factory=list)
    exact: bool = False
    more: int = 0


# --------------------------------------------------------------------------- query form

def _strip_templates(s):
    """`Foo<T, U<V>>::bar` -> `Foo::bar`; operator names keep their brackets (`operator<<`)."""
    out, depth, i = [], 0, 0
    while i < len(s):
        if s.startswith("operator", i):
            out.append(s[i:])               # the rest is the operator token — keep verbatim
            break
        ch = s[i]
        if ch == "<":
            depth += 1
        elif ch == ">" and depth:
            depth -= 1
        elif depth == 0:
            out.append(ch)
        i += 1
    return "".join(out)


def normalize(name):
    """-> [components]: whitespace removed, template arguments dropped, `::` and `.` both
    separate components (one spelling for C++, Python, TS, C#)."""
    s = "".join((name or "").split())
    s = _strip_templates(s)
    s = s.replace("::", ".")
    parts = [p for p in re.split(r"\.(?!\.)", s) if p]
    # `operator.` never occurs; a leading `~` (destructor) stays on its component
    return parts or [s]


# --------------------------------------------------------------------------- declared names

def decl_name(node):
    """(name, [qualifiers]) a node declares, or (None, []) — by the field chain."""
    cur, quals = node, []
    for _ in range(10):
        nxt = None
        if cur.type == "qualified_identifier" and "::" in cur.text():
            # only a real `A::b`: a macro prefix (`GGML_API T name(...)`) makes tree-sitter
            # read `T name` as a qualified_identifier WITHOUT `::` — T is a type, not a scope
            scope = cur.field("scope")
            if scope is not None:
                quals.extend(normalize(scope.text()))
        for f in _FIELDS:
            v = cur.field(f)
            if v is not None:
                nxt = v
                break
        if nxt is None:
            # wrapper without the fields (expression_statement -> assignment,
            # lexical_declaration -> variable_declarator, template_declaration -> definition)
            for c in cur.children():
                if any(c.field(f) is not None for f in _FIELDS):
                    nxt = c
                    break
        if nxt is None:
            break
        cur = nxt
    if cur is node:
        return None, []
    text = "".join(cur.text().split())
    if not text:
        return None, []
    comps = normalize(text)
    if not _NAME_RE.match(comps[-1]):          # `defined(_WIN32)`, `a[0]` — not a declared name
        return None, []
    return comps[-1], quals + comps[:-1]


def container_at(decls, line):
    """Innermost multi-line declaration holding `line` (a function, a struct, a table
    initializer), or None. `decls` = declarations(path)."""
    best = None
    for d in decls:
        if d.line <= line <= d.end and d.end > d.line:
            if best is None or (d.end - d.line) < (best.end - best.line):
                best = d
    return best


def outermost_in(decls, a, b):
    """Declarations starting inside lines a..b that no other such declaration contains."""
    inside = [d for d in decls if a <= d.line <= b]
    return [d for d in inside
            if not any(o is not d and o.line <= d.line and d.end <= o.end and (o.line, o.end) != (d.line, d.end)
                       for o in inside)]


def _is_func(node):
    return any(w in node.type for w in _FUNC_WORDS)


def declarations(path):
    """Every declared name in the file with its line (1-based), in file order."""
    backend, spec = resolve(os.path.splitext(path)[1])
    with open(path, "rb") as fh:
        root = backend.root(fh.read())
    out: List[Decl] = []
    # frames that QUALIFY what they hold (C++ namespace); preprocessor frames (`#ifdef X`) do not
    scoping = set(getattr(getattr(spec, "ls", None), "transparent_parents", ()) or ())

    def add(node, parents):
        nm, q = decl_name(node)
        if nm:
            out.append(Decl(nm, parents + q, node.start_row + 1, node.end_row + 1))
            return nm, q
        return None, []

    def walk(scope, parents, in_func):
        for ch in scope.children():
            frame = spec.unwrap_frame(ch)
            if frame is not None:
                nm, _q = decl_name(frame) if frame.type in scoping else (None, [])
                body = spec.body(frame)
                walk(body if body is not None else frame, parents + ([nm] if nm else []), in_func)
                continue
            d = spec.unwrap_def(ch)
            if d is not None:
                nm, q = add(ch, parents)
                if not nm:
                    nm, q = add(d, parents)
                if not nm:                                  # markdown heading etc.: its label
                    label = spec.name(ch)
                    if label:
                        out.append(Decl(label, list(parents), ch.start_row + 1, ch.end_row + 1))
                body = spec.body(ch)
                if body is not None:
                    walk(body, parents + q + ([nm] if nm else []), in_func or _is_func(d))
                continue
            # locals / imports / shell command calls (`build "$@"` names the called command) declare nothing
            if not in_func and spec.filler_kind(ch) not in ("import", "command"):
                add(ch, parents)

    walk(root, [], False)
    out.sort(key=lambda d: d.line)
    return out


# --------------------------------------------------------------------------- matching

def _words(s):
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    return [w for w in re.split(r"[\s_\-.:]+", s.lower()) if w]


def _closest(q, name, qw):
    ql, nl = q.lower(), name.lower()
    if ql == nl:
        return "case"
    if ql in nl:
        return "substring"
    if qw and all(any(w in nw for nw in _words(name)) for w in qw):
        return "words"
    if difflib.SequenceMatcher(None, ql, nl).ratio() >= TYPO_MIN:
        return "typo"
    return None


def resolve_name(path, query):
    comps = normalize(query)
    decls = declarations(path)
    res = Result(query=query)
    want, quals = comps[-1], comps[:-1]

    if any(ch in query for ch in "*?"):
        pat = ".".join(comps)
        hits = [Hit(d.line, ".".join(d.quals + [d.name]), "glob") for d in decls
                if fnmatch.fnmatchcase(d.name, want) or fnmatch.fnmatchcase(".".join(d.quals + [d.name]), pat)]
    else:
        hits = [Hit(d.line, ".".join(d.quals + [d.name]), "exact") for d in decls
                if d.name == want and (not quals or d.quals[-len(quals):] == quals)]
        res.exact = bool(hits)
        if not hits:
            qw = _words(query.replace("::", " ").replace(".", " "))   # words BEFORE spaces go
            for d in decls:
                how = _closest(want, d.name, qw)
                if how:
                    hits.append(Hit(d.line, ".".join(d.quals + [d.name]), how))
            wl = want.lower()                  # same category -> the most similar first
            hits.sort(key=lambda h: (_HOW_ORDER[h.how],
                                     -difflib.SequenceMatcher(None, wl, h.name.rsplit(".", 1)[-1].lower()).ratio(),
                                     h.line))
    seen, uniq = set(), []
    for h in hits:                                           # one line, one candidate
        if h.line not in seen:
            seen.add(h.line)
            uniq.append(h)
    res.more = max(0, len(uniq) - TOP)
    res.hits = uniq[:TOP]        # exact/glob: file order; closest: best first
    return res


def header_line(res):
    """The ONE line --name adds on top of the existing render."""
    q = f'name: "{res.query}"'
    more = f" (+{res.more} more — refine the name)" if res.more else ""
    if not res.hits:
        return f"{q} — nothing similar; try --outline"
    if res.exact:
        if len(res.hits) == 1:
            return f"{q} — exact, line {res.hits[0].line}"
        return f"{q} — ambiguous: {len(res.hits)} exact{more}; pick one with --line N --query"
    if all(h.how == "glob" for h in res.hits):
        return f"{q} — {len(res.hits)} match{more}; pick one with --line N --query"
    hows = ", ".join(dict.fromkeys(h.how for h in res.hits))
    return f"{q} — no exact; closest {len(res.hits)} ({hows}){more}; pick one with --line N --query"
