"""Windows batch backend (.bat / .cmd) — zero dependencies, own parser (Recipe B, like markdown.py).

A batch file has no nested definitions: its structure is LABELS (`:name`, the targets of `goto` /
`call :name`) — each owns the statements down to the next label, like a markdown heading owns its
section. Statements before the first label are the file's preamble. One statement = one logical
line: a `( … )` block spanning lines (`if … (` / `) else (` / `)`) and `^`-continued lines are
joined. Statement kinds: `comment` (`REM`, `::`), `set` (`SET X=…` -> `set: X`), `if` / `for`
(multi-line blocks), `command`. Comments glue to the label below them as preamble (Classifier).
Addressing takes the generic path (`classify.ladder_at`), the same tree as the map.
"""

import re

_LABEL = re.compile(r'^\s*:(?!:)([^\s:+=,;]+)')
_COMMENT = re.compile(r'^\s*@?(rem\b|::)', re.I)
_SET = re.compile(r'^\s*@?set\s+(?:/[ap]\s+)?"?([^=\s"]+)\s*=', re.I)
_KEYWORD = re.compile(r'^\s*@?(if|for)\b', re.I)


class BatNode:
    __slots__ = ('type', '_s', '_e', '_label', '_kids')

    def __init__(self, node_type, start_row, end_row, label='', kids=None):
        self.type = node_type
        self._s = start_row
        self._e = end_row
        self._label = label
        self._kids = kids if kids is not None else []

    @property
    def start_row(self):
        return self._s

    @property
    def end_row(self):
        return self._e

    def children(self):
        return self._kids

    def text(self):
        return self._label

    def field(self, name):
        # a label declares its name without the colon (`goto usage` / `call :usage`)
        if name == 'name' and self.type == 'label':
            return BatNode('name', self._s, self._s, label=self._label.lstrip(':'))
        return None

    @property
    def label(self):
        return self._label


def _paren_delta(line):
    """Net `(` minus `)` outside quotes and `^`-escapes; a comment line counts 0."""
    if _COMMENT.match(line):
        return 0
    d, quoted, i = 0, False, 0
    while i < len(line):
        ch = line[i]
        if ch == '^':
            i += 2
            continue
        if ch == '"':
            quoted = not quoted
        elif not quoted:
            if ch == '(':
                d += 1
            elif ch == ')':
                d -= 1
        i += 1
    return d


def _statements(lines):
    """[(start, end, kind, name)] — logical statements, blank lines skipped; labels kind='label'."""
    out = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i].rstrip('\r\n')
        if not line.strip():
            i += 1
            continue
        m = _LABEL.match(line)
        if m:
            out.append((i, i, 'label', ':' + m.group(1)))
            i += 1
            continue
        start, depth = i, _paren_delta(line)
        while i + 1 < n and (depth > 0 or line.rstrip().endswith('^')):
            i += 1
            line = lines[i].rstrip('\r\n')
            depth += _paren_delta(line)
        first = lines[start]
        if _COMMENT.match(first):
            kind, name = 'comment', ''
        elif _SET.match(first):
            kind, name = 'set', _SET.match(first).group(1)
        elif i > start and _KEYWORD.match(first):
            kind, name = _KEYWORD.match(first).group(1).lower(), ''
        else:
            kind, name = 'command', ''
        out.append((start, i, kind, name))
        i += 1
    return out


class BatchBackend:
    def root(self, source):
        lines = source.decode('utf-8', 'replace').splitlines()
        top, section = [], None
        for s, e, kind, name in _statements(lines):
            if kind == 'label':
                section = BatNode('label', s, s, label=name)
                top.append(section)
                continue
            node = BatNode(kind, s, e, label=name)
            if section is None:
                top.append(node)
            else:
                section._kids.append(node)
                section._e = e
        return BatNode('document', 0, max(0, len(lines) - 1), kids=top)


class BatchSpec:
    """Label = landmark, statements = filler. No frames."""

    def unwrap_frame(self, node):
        return None

    def unwrap_def(self, node):
        return node if node.type == 'label' else None

    def role(self, node):
        return 'landmark' if node.type == 'label' else 'filler'

    def body(self, node):
        return node if node.type == 'label' and node.children() else None

    def name(self, node):
        return node.label

    def filler_kind(self, node):
        return node.type

    def filler_label(self, nodes):
        if nodes[0].type != 'set':
            return None
        names = []
        for n in nodes:
            if n.label and n.label not in names:
                names.append(n.label)
        return "set: " + ", ".join(names[:8]) + (", …" if len(names) > 8 else "")
