"""TypeScript / JavaScript handler for get_codeblock.

Navigation (outline / get_blocks / line_level) is provided by the shared
tree-sitter block engine in `_treesitter_blocks` — the same engine C/C++ and C#
use. Two grammars back it: `typescript` for `.ts`/`.js`, `tsx` for `.tsx`/`.jsx`
(the tsx grammar understands JSX). A real parse handles multi-line signatures,
generics `<T>`, template literals, object-type literals and comments correctly —
the class of edge-cases the old brace heuristic kept tripping on.

Block model / level rule: see `_treesitter_blocks`. For TS specifically:
  * function/class/interface/enum declarations, methods, and control blocks
    (if/for/while/switch/try/…) are blocks;
  * `namespace`/`module` are TRANSPARENT (shown in outline, add no depth);
  * imports, `type X = …`, and value bindings (`const`/`let`/`var`) are leaves.

FIRST PASS: bare arrow/function EXPRESSIONS (React `const Foo = () => {…}`,
inline callbacks) are not yet named in the outline — their bodies still resolve
as standalone scopes, so get_blocks/levels inside them are correct; only their
outline LABEL is pending (needs a walk-up to the binding name). See TODO below.

The regex `declarations()` (declared surface for make_interface_card) is kept
below unchanged; the old brace-based navigation is preserved, disconnected, in
`_LegacyBraceNav` for reference.

Requires: pip install tree-sitter tree-sitter-typescript
"""

import re

from ._treesitter_blocks import LangSpec, TreeSitterBlockHandler, _walk, _source_bytes


def _load_typescript():
    import tree_sitter_typescript as tsts
    from tree_sitter import Language
    return Language(tsts.language_typescript())


def _load_tsx():
    import tree_sitter_typescript as tsts
    from tree_sitter import Language
    return Language(tsts.language_tsx())


# Node-type config shared by both grammars (tsx == typescript + JSX nodes).
# `object` is included so a multi-line object literal counts as a foldable block
# (TS/JS routinely hide methods in them: `{ value: (x) => {…} }`). Single-line
# objects are filtered out by the multi-line gate in the engine.
_TS_BODY = {'statement_block', 'class_body', 'interface_body', 'enum_body',
            'switch_body', 'object'}
_TS_NAMED = {
    'function_declaration', 'generator_function_declaration',
    'class_declaration', 'abstract_class_declaration',
    'interface_declaration', 'enum_declaration',
    'method_definition',
}
_TS_CONTROL = {
    'if_statement', 'else_clause',
    'for_statement', 'for_in_statement',
    'while_statement', 'do_statement',
    'switch_statement',
    'try_statement', 'catch_clause', 'finally_clause',
}
_TS_TRANSPARENT = {'internal_module', 'module'}   # `namespace X {}` / `module X {}`


def _make_ts_spec(name, loader):
    return LangSpec(
        name, loader,
        body_types=_TS_BODY,
        transparent_parents=_TS_TRANSPARENT,
        named_def=_TS_NAMED,
        container=_TS_TRANSPARENT,
        control=_TS_CONTROL,
        scope_body='statement_block',
    )


TS_SPEC = _make_ts_spec("TypeScript", _load_typescript)
TSX_SPEC = _make_ts_spec("TypeScript (TSX)", _load_tsx)


# TODO(next pass): name arrow/function EXPRESSIONS bound to an identifier
# (`const Foo = () => {…}`, class fields, object props) in the outline — the
# React-component pattern. Needs an _outline_nodes/_label override that walks
# arrow_function.parent -> variable_declarator/public_field_definition/pair to
# recover the name, while NOT surfacing anonymous inline callbacks as noise.


class TypeScriptHandler(TreeSitterBlockHandler):
    """.ts / .js — tree-sitter (typescript grammar) navigation + regex declared surface."""

    SPEC = TS_SPEC

    # node types that bind an arrow/function EXPRESSION to a name we can show.
    _ARROW_BINDINGS = {'variable_declarator', 'pair', 'public_field_definition',
                       'field_definition', 'assignment_expression'}

    # -- outline: named blocks + name-bound arrow/function expressions --------------------

    # ⚠ SUSPECT-DEAD (Vision03/04) — TS/JS outline теперь reader/classify (.0) c промоушеном
    # name-bound arrows в профиле typescript.py; сюда приложение не ходит. Вызыватель — golden_capture.py.
    def outline(self, lines, max_level=None):
        """Base named outline PLUS the JS/TS idiom the base can't see: an arrow or
        function EXPRESSION bound to a name — `const Foo = () => {…}` (React
        components), `value: () => {…}`, class field `foo = () => {…}`. Only
        block-bodied ones (a real `{…}` to fold); expression-bodied arrows and inline
        anonymous callbacks stay out of the map. Each is labelled by its binding, so
        `const App = () => {…}` reads as `const App = () =>`, not just its params.
        """
        rows = super().outline(lines, max_level)
        root = self._root(_source_bytes(lines))
        bodies = self._bodies(root)
        comment_rows = self._comment_rows(root)
        source = _source_bytes(lines)

        for n in _walk(root):
            if n.type not in ('arrow_function', 'function', 'function_expression'):
                continue
            body = next((c for c in n.children if c.type == 'statement_block'), None)
            if body is None or body.end_point[0] <= body.start_point[0]:
                continue                                   # no multi-line {…} to fold
            binding = n.parent
            if binding is None or binding.type not in self._ARROW_BINDINGS:
                continue                                   # anonymous inline callback
            # Anchor the entry at the whole binding statement (include const/export).
            stmt = binding
            if binding.type == 'variable_declarator' and binding.parent is not None:
                stmt = binding.parent
                if stmt.parent is not None and stmt.parent.type == 'export_statement':
                    stmt = stmt.parent
            level = self._level_of_row(stmt.start_point[0], bodies)
            if max_level and level > max_level:
                continue
            start = self._preamble_start(stmt.start_point[0], comment_rows, lines)
            label = source[stmt.start_byte:body.start_byte].decode('utf-8', 'replace')
            label = " ".join(label.split()).rstrip('{').rstrip()
            rows.append({'level': level, 'text': label,
                         'start': start + 1, 'end': body.end_point[0] + 1, 'frame': False})

        rows.sort(key=lambda r: (r['start'], -r['end']))
        return rows

    # -- declared surface (for make_interface_card) --------------------------------------

    def declarations(self, lines):
        """Top-level declarations (structural TS/JS heuristics) — the declared surface.

        Returns a list of dicts, in file order:
          {name, kind, exported, reexport_from, signature, start, end}
        kind ∈ function|class|interface|enum|type|const|let|var|namespace|reexport.
        `exported` = the line carries `export`. `reexport_from` set for
        `export … from "x"` (barrel). `signature` = the header, multi-line joined and
        cut at `{`/`;`. start/end are 1-indexed. Depth-0 only (nested members excluded).
        """
        out = []
        stack = []
        for i in range(len(lines)):
            depth_before = len(stack)
            s = lines[i].strip()
            if depth_before == 0 and s and not s.startswith(('//', '*', '/*')):
                d = self._parse_decl(lines, i)
                if d:
                    out.append(d)
            self._scan_line_for_braces(lines[i], stack, i)
        return out

    def _parse_decl(self, lines, i):
        s = lines[i].strip()
        # re-export: `export {a, b} from '...'`  |  `export * (as X)? from '...'`
        m = re.match(r'export\s+(?:type\s+)?(\*(?:\s+as\s+[\w$]+)?|\{[^}]*\})\s*from\s*["\']([^"\']+)["\']', s)
        if m:
            return {'name': m.group(1).strip(), 'kind': 'reexport', 'exported': True,
                    'reexport_from': m.group(2), 'signature': s.rstrip(';'),
                    'start': i + 1, 'end': i + 1}
        exported = bool(re.match(r'export\b', s))
        core = re.sub(r'^(?:export\s+|default\s+|declare\s+|abstract\s+|async\s+)+', '', s)
        m = re.match(r'(function|class|interface|enum|namespace|module|type|const|let|var)\b\s*\*?\s*([A-Za-z_$][\w$]*)', core)
        if not m:
            return None
        sig, end = self._decl_signature(lines, i, m.group(1))
        return {'name': m.group(2), 'kind': m.group(1), 'exported': exported,
                'reexport_from': None, 'signature': sig, 'start': i + 1, 'end': end}

    # kinds whose first depth-0 `{` opens a BODY (cut the signature there);
    # the rest (type/const/let/var) keep braces as part of the value/type up to `;`.
    _BLOCK_BODIED = {'function', 'class', 'interface', 'enum', 'namespace', 'module'}

    def _decl_signature(self, lines, i, kind):
        """Signature of a declaration starting at line i, scanning char-by-char across
        lines with bracket-depth + string/comment awareness. Returns (signature, end_line).

        - block-bodied kinds → stop at the first depth-0 `{` (the body opener);
        - value/type kinds    → run to the depth-0 `;` (type/object braces are part of the
          signature and are NOT cut); an arrow body `=> {` still stops the scan.
        """
        block = kind in self._BLOCK_BODIED
        value_binding = kind in ('const', 'let', 'var')
        out, depth, j = [], 0, i
        seen_colon = False   # a depth-0 `:` = a type annotation on a value binding
        stop = False
        while j < len(lines) and j < i + 40 and not stop:
            line = lines[j]
            k, n = 0, len(line)
            while k < n:
                ch = line[k]
                if ch in ('"', "'", '`'):                       # string / template literal
                    q = ch; out.append(ch); k += 1
                    while k < n and line[k] != q:
                        out.append(line[k]); k += 2 if line[k] == '\\' else 1
                    if k < n:
                        out.append(line[k]); k += 1
                    continue
                if ch == '/' and k + 1 < n and line[k + 1] == '/':   # line comment
                    break
                if ch == '/' and k + 1 < n and line[k + 1] == '*':   # block comment
                    k += 2
                    while k < n - 1 and not (line[k] == '*' and line[k + 1] == '/'):
                        k += 1
                    k += 2
                    continue
                if ch in '([':
                    depth += 1
                elif ch in ')]':
                    depth -= 1
                elif ch == '{':
                    if depth == 0:
                        tail = ''.join(out).rstrip()
                        if block or tail.endswith('=>'):        # body opener → stop
                            stop = True
                            break
                    depth += 1                                   # type/object literal brace
                elif ch == '}':
                    depth -= 1
                elif ch == ';' and depth == 0:
                    stop = True
                    break
                elif ch == ':' and depth == 0 and value_binding:
                    seen_colon = True                            # entering the type annotation
                elif ch == '=' and depth == 0 and value_binding and seen_colon:
                    # A typed binding `const x: T = impl` — the declared TYPE is the signature;
                    # drop the `= impl` initializer. Skip `=>`, `==`, `>=`, `<=`, `!=`.
                    nxt = line[k + 1] if k + 1 < n else ''
                    prev = out[-1] if out else ''
                    if nxt not in ('>', '=') and prev not in ('=', '<', '>', '!'):
                        stop = True
                        break
                out.append(ch)
                k += 1
            out.append(' ')
            j += 1
        sig = ' '.join(''.join(out).split()).rstrip(';').strip()
        return sig, j

    @staticmethod
    def _scan_line_for_braces(line, stack, line_idx):
        """Scan a single line for '{'/'}' (ignoring strings, template literals, comments),
        pushing/popping LINE indices. Used by declarations() to track depth-0."""
        i, n = 0, len(line)
        while i < n:
            ch = line[i]
            if ch in ('"', "'"):
                quote = ch
                i += 1
                while i < n and line[i] != quote:
                    i += 2 if line[i] == '\\' else 1
                i += 1
                continue
            if ch == '`':
                i += 1
                while i < n and line[i] != '`':
                    i += 2 if line[i] == '\\' else 1
                i += 1
                continue
            if i + 1 < n and ch == '/' and line[i + 1] == '/':
                break
            if i + 1 < n and ch == '/' and line[i + 1] == '*':
                i += 2
                while i < n - 1:
                    if line[i] == '*' and line[i + 1] == '/':
                        i += 2
                        break
                    i += 1
                else:
                    i = n
                continue
            if ch == '{':
                stack.append(line_idx)
            elif ch == '}':
                if stack:
                    stack.pop()
            i += 1


class TsxHandler(TypeScriptHandler):
    """.tsx / .jsx — same as TypeScriptHandler but with the JSX-aware tsx grammar."""

    SPEC = TSX_SPEC


# ============================================================================
# LEGACY — superseded by the tree-sitter engine above. Kept for reference only;
# NOT wired into any handler. The brace-matching navigation below was the
# original TS/JS implementation before the migration onto TreeSitterBlockHandler.
# ============================================================================

def is_block_header(line):
    """(legacy) Check if a line starts a named/control block by keyword."""
    stripped = line.strip()
    if not stripped or stripped.startswith('//'):
        return False
    if stripped.startswith('/*') or stripped.startswith('*'):
        return False
    check_part = stripped.split('{')[0].strip().lower()
    keywords = {
        'function', 'class', 'interface', 'enum', 'type',
        'namespace', 'module', 'const', 'let', 'var',
        'if', 'for', 'while', 'do', 'switch', 'try', 'catch', 'finally',
    }
    return any(check_part.startswith(kw) for kw in keywords)


def get_indent(line):
    """(legacy) Indentation width, tabs = 4."""
    stripped = line.lstrip()
    indent_str = line[:len(line) - len(stripped)]
    return len(indent_str.replace('\t', '    '))


# ⚠ DEAD (grep: 0 references in whole tree) — pre-migration brace nav, kept only for reference.
class _LegacyBraceNav:
    """(legacy, disconnected) Original brace-matching navigation for TS/JS.

    Replaced by TreeSitterBlockHandler; retained so the pre-migration heuristics
    remain visible. Do not wire this into the handler registry.
    """

    _BRACELESS_KW = ('if', 'else', 'for', 'while', 'do', 'foreach')

    def _is_braceless_control(self, stripped):
        if '{' in stripped or stripped.endswith(';') or stripped.endswith(','):
            return False
        for kw in self._BRACELESS_KW:
            if stripped == kw or stripped.startswith(kw + ' ') or stripped.startswith(kw + '('):
                return True
        return False

    def line_level(self, lines, idx):
        if idx < 0 or idx >= len(lines):
            return 1
        stack = []
        for i in range(idx):
            TypeScriptHandler._scan_line_for_braces(lines[i], stack, i)
        depth = len(stack)
        content = lines[idx].lstrip()
        if content.startswith('}'):
            depth = max(depth - 1, 0)
        return depth + 1 + self._braceless_bonus(lines, idx)

    def _braceless_bonus(self, lines, idx):
        bonus = 0
        cur_indent = get_indent(lines[idx])
        j = idx - 1
        while j >= 0:
            s = lines[j].strip()
            if not s or s.startswith('//') or s.startswith('*') or s.startswith('/*'):
                j -= 1
                continue
            ind = get_indent(lines[j])
            if ind < cur_indent and self._is_braceless_control(s):
                bonus += 1
                cur_indent = ind
                j -= 1
                continue
            break
        return bonus
