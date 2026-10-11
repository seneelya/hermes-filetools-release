"""YAML backend for the reader — a real grammar (tree-sitter-yaml), unlike the
plain-text backend's blank-line heuristics.

Model: a `key: value` line (`block_mapping_pair`) and a `- item` line
(`block_sequence_item`) are both landmarks. One whose value is itself a nested mapping
or sequence has a body (its own pairs/items, one level deeper); a scalar-valued one is a
leaf. Comments are ordinary siblings of these at whatever depth they appear — the
existing generic comment-preamble gluing (classify.py) applies with no extra code.

The grammar wraps every value in structural nodes (`document`, `block_node`) that carry
no name of their own — showing them as "frame" rows (like a transparent `namespace`)
would just be empty-looking noise, so `root()` skips through them directly instead of
exposing them as frames at all; multiple `---`-separated documents in one file (rare,
real) have their top-level pairs/items flattened together.
"""

from .treesitter import TSNode

_LABEL_CAP = 60
_WRAPPER_TYPES = ('document', 'block_node')


def _short(text, cap=_LABEL_CAP):
    t = " ".join(text.split())
    if len(t) <= cap:
        return t
    cut = t[:cap]
    sp = cut.rfind(' ')
    if sp > cap // 2:            # don't chop a word unless the trim would be tiny
        cut = cut[:sp]
    return cut.rstrip() + '…'


def _unwrap(node):
    """Skip transparent grammar wrappers down to the real content node (a mapping, a
    sequence, or a scalar) — `document`/`block_node` carry no info of their own."""
    while node is not None and node.type in _WRAPPER_TYPES:
        kids = node.children()
        if not kids:
            return node
        node = kids[0]
    return node


def _value_of(node):
    """The value-side content of a landmark, unwrapped — or None (nothing to recurse
    into: a scalar leaf, or an empty sequence item like a bare `-`)."""
    if node.type == 'block_mapping_pair':
        value = node.field('value')
    elif node.type == 'block_sequence_item':
        kids = node.children()
        value = kids[0] if kids else None
    else:
        return None
    return _unwrap(value) if value is not None else None


class _RootNode:
    """Synthetic root exposing a FLAT top-level children() list regardless of how many
    `---`-documents or wrapper layers the grammar put in between."""
    __slots__ = ('_s', '_e', '_kids')
    type = 'document_root'

    def __init__(self, s, e, kids):
        self._s, self._e, self._kids = s, e, kids

    @property
    def start_row(self):
        return self._s

    @property
    def end_row(self):
        return self._e

    def children(self):
        return self._kids

    def text(self):
        return ''

    def field(self, name):
        return None


class YamlBackend:
    def root(self, source):
        # Грамматика опциональна (requirements.txt). Без гарда голый ImportError уходил
        # трейсбеком мимо штатного сообщения с готовой командой установки — тем самым,
        # что CLI печатает через core.py. Остальные языки прикрыты LangSpec.parser().
        from ...env_check import ensure_language
        ensure_language("yaml")
        import tree_sitter_yaml
        from tree_sitter import Language, Parser
        lang = Language(tree_sitter_yaml.language())
        parser = Parser(lang)
        root = TSNode(parser.parse(source).root_node, source)
        kids = []
        for c in root.children():
            if c.type == 'document':
                content = _unwrap(c)
                if content is not None:
                    kids.extend(content.children())
            else:                          # a leading/standalone comment, etc.
                kids.append(c)
        end = root.end_row if root.end_row >= root.start_row else root.start_row
        return _RootNode(root.start_row, end, kids)


class YamlSpec:
    def unwrap_frame(self, node):
        return None

    def unwrap_def(self, node):
        return node if node.type in ('block_mapping_pair', 'block_sequence_item') else None

    def role(self, node):
        return 'landmark' if node.type in ('block_mapping_pair', 'block_sequence_item') else 'filler'

    def body(self, node):
        content = _value_of(node)
        return content if content is not None and content.type in ('block_mapping', 'block_sequence') else None

    def name(self, node):
        if node.type == 'block_sequence_item':
            return _short(node.first_line().strip())
        if node.type != 'block_mapping_pair':
            return ''
        key = node.field('key')
        key_text = (key.text().strip() if key is not None else '?').splitlines()[0]
        content = _value_of(node)
        if content is not None and content.type in ('block_mapping', 'block_sequence'):
            return f"{key_text}:"
        value = node.field('value')
        val_text = value.first_line().strip() if value is not None else ''
        return _short(f"{key_text}: {val_text}") if val_text else f"{key_text}:"

    def filler_kind(self, node):
        return node.type
