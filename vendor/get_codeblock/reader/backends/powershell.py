"""PowerShell backend for the reader — a real grammar (tree-sitter-powershell) with its own Spec,
like `yaml_backend.py` (Recipe B, variant 3: reuses only `TSNode`).

Why not a brace profile (LangSpec): the grammar wraps every statement run in nodes with no syntax
of their own (`statement_list`, `script_block_body`), and a function body (`script_block`) starts
on the row AFTER `{` and ends before `}` — body-strict levels of `address.py` would put the first
and last body lines on the header's level, and the rung would end before the function's `}` (map
and addressing would disagree, invariant #6). So: wrappers are spliced away here, and addressing
takes the generic path (`classify.ladder_at`) — the same tree as the map.

Model: `function` / `filter`, `class`, class methods and `enum` are landmarks; everything else is
filler — control blocks (`if`, `foreach`, `try`, `switch`) form bands inside the function that holds
them; `$x = …` runs are `assign: $x, …`; comments (`#`, `<# #>`) glue as preamble as usual.
"""

from .treesitter import TSNode

_SPLICE = frozenset({'statement_list', 'script_block_body'})
_DEFS = frozenset({'function_statement', 'class_statement', 'class_method_definition',
                   'enum_statement'})
_MEMBERS = frozenset({'class_property_definition', 'class_method_definition', 'comment',
                      'enum_member'})
_LABEL_CAP = 60
_CAP = 8


class PsNode:
    """TSNode view whose children() splices the grammar's syntax-less wrappers. `kids`
    overrides the children (a class body = its members only, not its name token)."""
    __slots__ = ('_t', '_kids')

    def __init__(self, tsnode, kids=None):
        self._t = tsnode
        self._kids = kids

    @property
    def type(self):
        return self._t.type

    @property
    def start_row(self):
        return self._t.start_row

    @property
    def end_row(self):
        return self._t.end_row

    def children(self):
        if self._kids is not None:
            return self._kids
        out = []
        stack = list(reversed(self._t.children()))
        while stack:
            c = stack.pop()
            if c.type in _SPLICE:
                stack.extend(reversed(c.children()))
            else:
                out.append(PsNode(c))
        return out

    def text(self):
        return self._t.text()

    def field(self, name):
        f = self._t.field(name)
        if f is None and name == 'name' and self.type in _DEFS:
            # the grammar has no `name` field on definitions: the name is a typed child
            # (`function_name` / `simple_name`) — exposed as the field so `--name` (and any
            # field-chain reader) finds `Get-Thing` / `Area` exactly
            f = self.child_of_type('function_name') or self.child_of_type('simple_name')
        return PsNode(f) if f is not None else None

    def child_of_type(self, t):
        for c in self._t.children():
            if c.type == t:
                return c
        return None

    def head_before(self, child):
        return self._t.head_before(child)

    def first_line(self):
        return self._t.first_line()


class PowerShellBackend:
    def root(self, source):
        # Optional grammar (requirements.txt): the standard install message, not a traceback.
        from ...env_check import ensure_language
        ensure_language("powershell")
        import tree_sitter_powershell
        from tree_sitter import Language, Parser
        parser = Parser(Language(tree_sitter_powershell.language()))
        return PsNode(TSNode(parser.parse(source).root_node, source))


def _short(text, cap=_LABEL_CAP):
    t = " ".join(text.split())
    return t if len(t) <= cap else t[:cap].rstrip() + ' …'


def _assign_target(node):
    """`$x = …` statement (a pipeline holding an assignment) -> `$x`, else None."""
    if node.type != 'pipeline':
        return None
    for c in node.children():
        if c.type == 'assignment_expression':
            left = c.child_of_type('left_assignment_expression')
            return _short(left.text(), 40) if left is not None else None
        return None
    return None


class PowerShellSpec:
    def unwrap_frame(self, node):
        return None

    def unwrap_def(self, node):
        return node if node.type in _DEFS else None

    def role(self, node):
        return 'landmark' if node.type in _DEFS else 'filler'

    def body(self, node):
        if node.type in ('function_statement', 'class_method_definition'):
            sb = node.child_of_type('script_block')
            return PsNode(sb) if sb is not None else None
        if node.type in ('class_statement', 'enum_statement'):
            members = [c for c in node.children() if c.type in _MEMBERS]
            return PsNode(node._t, kids=members) if members else None
        return None

    def name(self, node):
        sb = node.child_of_type('script_block') if node.type != 'class_statement' else None
        raw = node.head_before(sb) if sb is not None else node.first_line()
        return _short(raw.strip().rstrip('{').rstrip())

    def filler_kind(self, node):
        if node.type == 'pipeline':
            return 'assign' if _assign_target(node) else 'command'
        if node.type == 'param_block':
            return 'param'
        return node.type

    def filler_label(self, nodes):
        if nodes[0].type != 'pipeline' or not _assign_target(nodes[0]):
            return None
        names = []
        for n in nodes:
            t = _assign_target(n)
            if t and t not in names:
                names.append(t)
        return "assign: " + ", ".join(names[:_CAP]) + (", …" if len(names) > _CAP else "")
