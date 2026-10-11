"""Markdown backend для ридера (core2 — первый НЕ-tree-sitter кор).

Доказывает, что интерфейс Backend/RNode/Spec держит источник без tree-sitter.
Разбор заголовков переиспользуем из handlers.markdown_handler (не дублируем).

Модель (как в markdown_handler): заголовок `## X` владеет всем до следующего
заголовка того же/высшего уровня; уровень = глубина заголовка. Заголовки =
landmark (разделы/главы книги), остальной текст = filler-полосы. Рамок нет.
"""

from ..protocol import RNode  # noqa: F401  (документируем, чему соответствуем)


class MdNode:
    """Узел markdown-дерева под протокол RNode. Строится backend'ом (не tree-sitter)."""
    __slots__ = ('type', '_s', '_e', '_label', '_kids')

    def __init__(self, node_type, start_row, end_row, label='', kids=None):
        self.type = node_type              # 'document' | 'heading' | 'content'
        self._s = start_row                # 0-based
        self._e = end_row                  # 0-based inclusive
        self._label = label                # текст заголовка
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
        return None

    # helper для Spec
    @property
    def label(self):
        return self._label


def _build(hs, i, parent_level, gap_start, region_end, lines, section_end):
    """Рекурсивно: узлы для [gap_start, region_end) из заголовков hs[i:] глубже
    parent_level. Возвращает (nodes, next_i)."""
    nodes = []
    cursor = gap_start
    while i < len(hs) and hs[i][1] > parent_level:
        idx, level, text = hs[i]
        if idx > cursor:                                   # текст-пробел перед заголовком
            nodes.append(MdNode('content', cursor, idx - 1))
        sec_end = section_end(hs, i, len(lines))           # 0-based: строка следующего >= заголовка / nlines
        child_nodes, i = _build(hs, i + 1, level, idx + 1, sec_end, lines, section_end)
        nodes.append(MdNode('heading', idx, sec_end - 1, label=text, kids=child_nodes))
        cursor = sec_end
    if cursor < region_end:
        nodes.append(MdNode('content', cursor, region_end - 1))
    return nodes, i


class MarkdownBackend:
    def root(self, source):
        from ...handlers.markdown_handler import _headings, _section_end, _frontmatter
        lines = source.decode('utf-8', 'replace').splitlines(keepends=True)
        hs = _headings(lines)
        fm = _frontmatter(lines)
        start = fm[0] + 1 if fm else 0
        kids, _ = _build(hs, 0, 0, start, len(lines), lines, _section_end)
        if fm:
            # the document's passport: its own block before the first heading, owns no sections
            label = 'meta: ' + ', '.join(fm[1]) if fm[1] else 'meta'
            kids.insert(0, MdNode('meta', 0, fm[0], label=label))
        return MdNode('document', 0, max(0, len(lines) - 1), kids=kids)


class MarkdownSpec:
    """Декоратор markdown: заголовок = landmark, прочее = filler. Рамок нет."""

    def unwrap_frame(self, node):
        return None

    def unwrap_def(self, node):
        return node if node.type in ('heading', 'meta') else None

    def role(self, node):
        return 'landmark' if node.type in ('heading', 'meta') else 'filler'

    def body(self, node):
        # тело раздела = сам узел (его children() — под-контент и под-заголовки)
        return node if node.type == 'heading' and node.children() else None

    def name(self, node):
        return node.label

    def filler_kind(self, node):
        return node.type
