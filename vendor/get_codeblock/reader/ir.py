"""IR — промежуточное представление ридера (Vision03).

Единый «формат запросов утилиты»: что бы ни было под капотом (tree-sitter-код,
.docx, .pdf), backend+декоратор выдают дерево `Block`, а рендеры (outline/query/
.0/ладдер) потребляют ТОЛЬКО его. Renderer'ы про backend ничего не знают.

`Block` — обобщение прежнего `dot_classify.DotEntry`.
"""

from enum import Enum


class Role(str, Enum):
    """Роль элемента в scope (единственное, что решает декоратор языка/формата)."""
    LANDMARK = "landmark"   # именованное определение — раскрываем поимённо
    FILLER = "filler"       # imports/comments/абзацы — сливаем в полосу по типу
    FRAME = "frame"         # прозрачная рамка (namespace/раздел) — дети всплывают


class Block:
    """Узел IR. Диапазон 1-based, end включительно (как во всех режимах утилиты)."""
    __slots__ = ('role', 'kind', 'name', 'start', 'end', 'level', 'count',
                 'children', 'description')

    def __init__(self, role, kind, start, end, level, name=None, count=1,
                 children=None, description=None):
        self.role = role          # Role
        self.kind = kind          # str — тип узла backend'а (node_type / 'heading' / ...)
        self.name = name          # str | None — заголовок landmark/frame
        self.start = start        # int, 1-based
        self.end = end            # int, 1-based inclusive
        self.level = level        # int — файловый уровень = 1 (рамка не углубляет)
        self.count = count        # int — сколько узлов слито в filler-полосу
        self.children = children if children is not None else []
        # Опциональное описание от Analyzer (см. protocol.Analyzer): «license-блок»,
        # «docstring» и т.п. Backend его НЕ ставит — это пост-проход. None = не анализировано.
        self.description = description

    def __repr__(self):
        who = self.name if self.name is not None else f"{self.kind}×{self.count}"
        return f"Block({self.role.value} [{self.start}-{self.end}] L{self.level} {who!r})"
