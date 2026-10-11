"""Plain-text backend for the reader (experimental — no code, no markup at all).

No headings to key off (unlike markdown), so the structure comes purely from
whitespace: a PARAGRAPH is a run of non-blank lines; a run of 2+ blank lines, or a
horizontal-rule-only line (`---`/`===`/`***`, 3+ repeats of one punctuation char),
marks a SECTION break — a landmark grouping the paragraphs between two such breaks.
Maps onto "TOC - blocks - paragraphs": the outline itself is the TOC, SECTION is a
block, PARAGRAPH is a paragraph. A rule line is shown as its own top-level filler item
(kind='rule'), not swallowed.

Naming: no title text exists to read (there is no heading), so a paragraph/section's
name is its own first ~60 chars, trimmed to a word boundary with a trailing `…` if cut
— a cheap proxy for "the first half of its first sentence" without real sentence-
boundary detection (which is locale/abbreviation-fragile). A section's name is its
first paragraph's name.

A paragraph that turns out to be a LIST (2+ of its lines start with a `1.`/`1)` or
`-`/`*`/`•` marker) is split one level deeper: each marker line (through the lines
before the next marker) becomes its own ITEM landmark; a lead-in line before the first
marker (`Consider:` before a bullet list) becomes an unmarked ITEM too. An ordinary
prose paragraph (no marker lines, or just one) is untouched — still one flat leaf.
"""

import re

_RULE_RE = re.compile(r'^[ \t]*([-=_*])\1{2,}[ \t]*$')
_ITEM_RE = re.compile(r'^\s*(?:\d+[.)]|[-*•])\s+')
_LABEL_CAP = 60


def _short_label(text, cap=_LABEL_CAP):
    t = " ".join(text.split())
    if len(t) <= cap:
        return t
    cut = t[:cap]
    sp = cut.rfind(' ')
    if sp > cap // 2:            # don't chop a word unless the trim would be tiny
        cut = cut[:sp]
    return cut.rstrip() + '…'


class TxtNode:
    """Node under the RNode protocol, built by this backend (not tree-sitter)."""
    __slots__ = ('type', '_s', '_e', '_label', '_kids')

    def __init__(self, node_type, start_row, end_row, label='', kids=None):
        self.type = node_type          # 'document' | 'section' | 'paragraph' | 'rule'
        self._s = start_row            # 0-based
        self._e = end_row              # 0-based inclusive
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
        return None

    @property
    def label(self):
        return self._label


def _runs(lines):
    """lines -> [('blank'|'rule'|'para', start_row, end_row), ...], 0-based inclusive."""
    runs = []
    i, n = 0, len(lines)
    while i < n:
        raw = lines[i].rstrip('\r\n')
        if raw.strip() == '':
            j = i
            while j < n and lines[j].rstrip('\r\n').strip() == '':
                j += 1
            runs.append(('blank', i, j - 1))
            i = j
            continue
        if _RULE_RE.match(raw):
            runs.append(('rule', i, i))
            i += 1
            continue
        j = i
        while j < n:
            r = lines[j].rstrip('\r\n')
            if r.strip() == '' or _RULE_RE.match(r):
                break
            j += 1
        runs.append(('para', i, j - 1))
        i = j
    return runs


def _list_item_ranges(lines, s, e):
    """Split paragraph [s,e] into list items if 2+ of its lines start with a marker.
    Returns None (not a list — keep the paragraph flat) or [(start,end), ...] ranges
    covering [s,e] exactly: an optional unmarked lead-in range, then one range per
    marker line running up to (not including) the next marker line."""
    marker_rows = [i for i in range(s, e + 1) if _ITEM_RE.match(lines[i].rstrip('\r\n'))]
    if len(marker_rows) < 2:
        return None
    ranges = []
    if marker_rows[0] > s:
        ranges.append((s, marker_rows[0] - 1))
    for idx, row in enumerate(marker_rows):
        end = (marker_rows[idx + 1] - 1) if idx + 1 < len(marker_rows) else e
        ranges.append((row, end))
    return ranges


def _paragraph_node(lines, s, e):
    text = "".join(lines[s:e + 1])
    label = _short_label(text)
    items = _list_item_ranges(lines, s, e)
    if items is None:
        return TxtNode('paragraph', s, e, label=label)
    kids = [TxtNode('item', a, b, label=_short_label("".join(lines[a:b + 1])))
            for a, b in items]
    return TxtNode('paragraph', s, e, label=label, kids=kids)


def _build(lines):
    nodes = []
    section_paras = []
    section_start = [None]

    def flush_section():
        if not section_paras:
            return
        s = section_start[0]
        e = section_paras[-1].end_row
        label = section_paras[0].label
        nodes.append(TxtNode('section', s, e, label=label, kids=list(section_paras)))
        section_paras.clear()
        section_start[0] = None

    for kind, s, e in _runs(lines):
        if kind == 'blank':
            if e - s + 1 >= 2:                       # 2+ blank lines -> section break
                flush_section()
            continue
        if kind == 'rule':
            flush_section()
            nodes.append(TxtNode('rule', s, e, label=lines[s].rstrip()))
            continue
        para = _paragraph_node(lines, s, e)
        if section_start[0] is None:
            section_start[0] = s
        section_paras.append(para)

    flush_section()
    return nodes


class PlainTextBackend:
    def root(self, source):
        lines = source.decode('utf-8', 'replace').splitlines(keepends=True)
        kids = _build(lines)
        return TxtNode('document', 0, max(0, len(lines) - 1), kids=kids)


class PlainTextSpec:
    """SECTION/PARAGRAPH/ITEM are landmarks; RULE is filler; no frames (there is nothing
    structural to be transparent about in plain prose)."""

    def unwrap_frame(self, node):
        return None

    def unwrap_def(self, node):
        return node if node.type in ('section', 'paragraph', 'item') else None

    def role(self, node):
        return 'landmark' if node.type in ('section', 'paragraph', 'item') else 'filler'

    def body(self, node):
        return node if node.type in ('section', 'paragraph') and node.children() else None

    def name(self, node):
        return node.label

    def filler_kind(self, node):
        return node.type
