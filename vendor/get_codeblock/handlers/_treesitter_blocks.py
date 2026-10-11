"""Shared tree-sitter block engine for brace languages (C/C++, C#).

The traversal + level logic is identical across brace languages; only the set of
node-type names differs. Each language supplies a `LangSpec` (grammar loader +
node-type sets) and gets outline / get_blocks / line_level for free by
subclassing `TSBlockHandler`.

Block model (see cpp_handler for the full rationale):
  * A block is any node with a brace-delimited body -> a foldable region.
  * TRANSPARENT containers (namespace / extern "C") are shown in --outline but
    do NOT add a nesting level to their members.
  * Preamble comments directly above a block are glued onto its start.
  * Preprocessor lines, usings, forward-decls and plain fields are leaves.

Level rule (matches editor-fold numbering):
  level(row) = 1 + count of NON-transparent block bodies STRICTLY containing the
  row (strict -> the `{` line and `}` line belong to the outer level).
"""


class LangSpec:
    """Node-type configuration for one brace language."""

    def __init__(self, name, load_language, *, body_types, transparent_parents,
                 named_def, container, control, scope_body, cut_extra=(), preprocess=None,
                 is_synthetic_comment=None):
        self.name = name
        self._load_language = load_language
        self.body_types = frozenset(body_types)
        self.transparent_parents = frozenset(transparent_parents)
        self.named_def = frozenset(named_def)
        self.container = frozenset(container)
        self.control = frozenset(control)
        self.scope_body = scope_body           # bare `{}` statement-block type
        self.cut_at = self.body_types | frozenset(cut_extra)
        self.body_owners = self.named_def | self.control
        self._parser = None
        # Optional bytes->bytes source rewrite run BEFORE parsing, same length in/out (byte
        # offsets/line numbers must stay valid) — a language-owned escape hatch for syntax
        # its grammar can't parse at all (see css_handler.py's SCSS top-level `$var:` mask).
        # None for every language that doesn't need one (default, zero behavior change).
        self.preprocess = preprocess
        # Opt-in: real `comment`-typed tree-sitter nodes that are actually a SYNTHETIC
        # stand-in for something `preprocess` masked (see css_handler.py's SCSS `$var:`
        # mask) — text -> bool. A real comment glues onto the block below it as preamble
        # (everywhere, by design); a masked one must NOT, or the thing it stood in for
        # (a variable declaration, not a comment) silently gets absorbed into the next
        # rule's range. None for every language without a preprocess mask (default).
        self.is_synthetic_comment = is_synthetic_comment

    def parser(self):
        if self._parser is None:
            try:
                from tree_sitter import Parser
                lang = self._load_language()
            except ImportError as e:
                raise ImportError(
                    f"{self.name} support needs tree-sitter: pip install "
                    f"tree-sitter <grammar> (present in the project venv/system python)"
                ) from e
            self._parser = Parser(lang)
        return self._parser


def _walk(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def _source_bytes(lines):
    return "".join(lines).encode("utf-8")


class TreeSitterBlockHandler:
    """Mixin providing outline/get_blocks/line_level from a LangSpec.

    Subclasses set the class attribute SPEC = <LangSpec>.
    """

    SPEC = None

    # -- tree helpers -----------------------------------------------------

    def _root(self, source_bytes):
        if self.SPEC.preprocess is not None:
            source_bytes = self.SPEC.preprocess(source_bytes)
        return self.SPEC.parser().parse(source_bytes).root_node

    def _bodies(self, root):
        """(start_row, end_row, transparent) for every brace body."""
        sp = self.SPEC
        out = []
        for n in _walk(root):
            if n.type in sp.body_types:
                transparent = (n.parent is not None
                               and n.parent.type in sp.transparent_parents)
                out.append((n.start_point[0], n.end_point[0], transparent))
        return out

    @staticmethod
    def _level_of_row(row, bodies):
        return 1 + sum(1 for sr, er, tr in bodies if not tr and sr < row < er)

    def _has_body(self, node):
        return any(c.type in self.SPEC.body_types for c in node.children)

    def _is_standalone_body(self, node):
        """A brace body with no declaring header of its own — an arrow-function body,
        a bare `{}` block, or a multi-line object/array literal. These are foldable
        regions you'd want to pull when the cursor lands inside, but they carry no
        name. MULTI-LINE only: a single-line `{…}` is nothing to fold, so it is not a
        level (this also drops brace-less one-line control, which has no body node).

        Excluded: bodies owned by a named/control node (the owner is the block) and
        bodies of a transparent container (namespace/extern "C")."""
        sp = self.SPEC
        if node.type not in sp.body_types:
            return False
        if node.end_point[0] <= node.start_point[0]:
            return False
        parent = node.parent
        return (parent is not None
                and parent.type not in sp.body_owners
                and parent.type not in sp.transparent_parents)

    def _label(self, node, source_bytes):
        """Header text: everything up to the body / init-list."""
        cut = node.end_byte
        for c in node.children:
            if c.type in self.SPEC.cut_at:
                cut = c.start_byte
                break
        text = source_bytes[node.start_byte:cut].decode("utf-8", "replace")
        return " ".join(text.split()).rstrip('{').rstrip().rstrip(':').rstrip()

    def _block_label(self, node, source_bytes):
        """Label for a ladder rung: the header for a named/control block; a short
        kind tag for an anonymous brace region (arrow body, object literal, block)."""
        sp = self.SPEC
        if node.type in sp.named_def or node.type in sp.control:
            return self._label(node, source_bytes)
        parent = node.parent.type if node.parent is not None else ''
        if node.type in ('object', 'object_pattern'):
            return "{…} object"
        if node.type in ('array', 'array_pattern'):
            return "[…] array"
        if 'arrow' in parent:
            return "() => {…}"
        return "{…} block"

    @staticmethod
    def _comment_rows(root):
        """Rows covered EXCLUSIVELY by comment nodes.

        A row that also carries a non-comment (code) node — e.g. a trailing
        `// note` after `if (x) {...}` — is a code row, not a comment row, so it
        must NOT let the preamble glue climb over it. Subtracting code_rows kills
        BUG #1 (hanging comments dragging a block's start up over foreign code)."""
        comment_rows, code_rows = set(), set()
        for n in _walk(root):
            if n.child_count:          # only LEAF nodes carry a real token on a row;
                continue               # container nodes span comment rows and would
            rows = range(n.start_point[0], n.end_point[0] + 1)  # falsely mark them code
            if n.type == 'comment':
                comment_rows.update(rows)
            else:
                code_rows.update(rows)
        return comment_rows - code_rows

    @staticmethod
    def _preamble_start(start_row, comment_rows, lines):
        first = start_row
        r = start_row - 1
        while r >= 0:
            if not lines[r].strip():
                r -= 1
                continue
            if r in comment_rows:
                first = r
                r -= 1
                continue
            break
        return first

    def _preamble_owner(self, nodes, row, comment_rows, lines):
        """The ladder node whose preamble (comments directly above it) includes
        `row`, i.e. preamble_start(node) <= row < node.start. None if the row is
        a comment that documents nothing (trailing/detached). If several nodes
        qualify (a comment above a block that is itself the first line of an outer
        block), pick the nearest one below the row = the block it actually
        annotates."""
        owners = [n for n in nodes
                  if self._preamble_start(n.start_point[0], comment_rows, lines)
                  <= row < n.start_point[0]]
        if not owners:
            return None
        return min(owners, key=lambda n: n.start_point[0])

    def _ladder_nodes(self, root):
        sp = self.SPEC
        out = []
        for n in _walk(root):
            t = n.type
            if t in sp.named_def and self._has_body(n):
                out.append(n)
            elif t in sp.control and self._has_body(n):   # braced control only — a
                out.append(n)                             # brace-less one-liner is not a block
            elif self._is_standalone_body(n):
                out.append(n)
        return out

    def _outline_nodes(self, root):
        sp = self.SPEC
        out = []
        for n in _walk(root):
            t = n.type
            if (t in sp.container or t in sp.named_def) and self._has_body(n):
                out.append(n)
        return out

    # -- one canonical range calculator -----------------------------------

    def _bounds(self, node, bodies, comment_rows, lines):
        """THE range of a block node — identical in every mode (outline, ladder,
        query, nearest). Level = structural depth of the header row; `start` is
        pulled up over the block's preamble comments (doc/line/block comment
        directly above it); `end` = the node's last row. 1-based, end inclusive.

        Every code path goes through here, so a block reports the same
        [start-end] whether it is the innermost hit or an outer rung of the
        ladder — no more outline-vs-ladder drift."""
        return {
            'level': self._level_of_row(node.start_point[0], bodies),
            'start': self._preamble_start(node.start_point[0], comment_rows, lines) + 1,
            'end': node.end_point[0] + 1,
        }

    # -- public API (mirrors PythonHandler) -------------------------------
    #
    # ⚠ SUSPECT-DEAD (Vision03/04 migration) — весь этот public API + его хелперы
    # (_outline_nodes/_ladder_nodes/_bounds/_nearest) вытеснены reader-слоем:
    # outline → reader/classify.py (.0), get_blocks/line_level (brace) → reader/address.py.
    # Приложение (core.py) ходит через `Reader`, НЕ сюда. Единственный оставшийся вызыватель —
    # test/parity/golden_capture.py (снимок старого поведения для паритета). НЕ РЕЗАТЬ пока —
    # сперва решить судьбу golden_capture. LangSpec/SPEC-константы ниже/выше — ЖИВЫЕ (импортят профили).

    def outline(self, lines, max_level=None):   # ⚠ SUSPECT-DEAD → reader/classify.outline_rows
        source = _source_bytes(lines)
        root = self._root(source)
        bodies = self._bodies(root)
        comment_rows = self._comment_rows(root)

        nodes = self._outline_nodes(root)
        nodes.sort(key=lambda n: (n.start_point[0], -n.end_point[0]))

        out = []
        for n in nodes:
            b = self._bounds(n, bodies, comment_rows, lines)
            if max_level and b['level'] > max_level:
                continue
            b['text'] = self._label(n, source)
            # Transparent containers (namespace / extern "C") are frames, not depth:
            # rendered with a "." marker instead of a level number.
            b['frame'] = n.type in self.SPEC.transparent_parents
            out.append(b)
        return out

    def line_level(self, lines, idx):   # ⚠ SUSPECT-DEAD → reader/address.line_level (brace)
        if idx < 0 or idx >= len(lines):
            return 1
        root = self._root(_source_bytes(lines))
        return self._level_of_row(idx, self._bodies(root))

    def get_blocks(self, file_path, target_line):   # ⚠ SUSPECT-DEAD → reader/address.get_blocks (brace)
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        if not lines or target_line < 1 or target_line > len(lines):
            return []

        root = self._root(_source_bytes(lines))
        bodies = self._bodies(root)
        comment_rows = self._comment_rows(root)
        row = target_line - 1

        nodes = self._ladder_nodes(root)
        containing = [n for n in nodes
                      if n.start_point[0] <= row <= n.end_point[0]]

        # Landing on a preamble comment: it documents the block directly below it,
        # so it belongs to THAT block, not to whatever encloses the gap between
        # blocks (a class, a body). Mirror outline's gluing: if the row sits in a
        # node's preamble region [preamble_start .. node.start), treat that node as
        # the innermost containing block. An inner comment can't document the class.
        if row in comment_rows:
            owner = self._preamble_owner(nodes, row, comment_rows, lines)
            if owner is not None and owner not in containing:
                containing.append(owner)

        if not containing:
            return self._nearest(nodes, row, bodies, comment_rows, lines)

        containing.sort(key=lambda n: (n.start_point[0], -n.end_point[0]))

        # EVERY rung glued the same way (was: innermost-only) -> a block's range
        # matches its outline range exactly. Each rung carries a label (what it is).
        source = _source_bytes(lines)
        result = []
        for n in containing:
            b = self._bounds(n, bodies, comment_rows, lines)
            b['label'] = self._block_label(n, source)
            result.append(b)
        return result

    def _nearest(self, nodes, row, bodies, comment_rows, lines):
        below = [n for n in nodes if n.start_point[0] >= row]
        above = [n for n in nodes if n.end_point[0] <= row]
        chosen = None
        if below:
            chosen = min(below, key=lambda n: n.start_point[0] - row)
        if above:
            a = min(above, key=lambda n: row - n.end_point[0])
            if chosen is None or (row - a.end_point[0]) < (chosen.start_point[0] - row):
                chosen = a
        if chosen is None:
            return []
        b = self._bounds(chosen, bodies, comment_rows, lines)
        b['label'] = self._block_label(chosen, _source_bytes(lines))
        return [b]
