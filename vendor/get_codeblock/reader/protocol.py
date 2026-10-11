"""Протоколы общения ридера (Vision03) — два шва между слоями.

Классификатор (`classify.py`) backend-agnostic: он работает с `RNode` через `Spec`,
не зная, tree-sitter это, .docx или что-то ещё.

Три роли в контракте:
  * Backend — превращает файл/байты в корневой `RNode` (парсинг).
  * RNode   — нормализованный узел: минимум, нужный классификатору.
  * Spec    — декоратор формата/языка: роль/имя/тело/рамка узла (весь язык-нюанс).

Реестр (`registry.py`) связывает расширение с парой (Backend, Spec).
"""

from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class RNode(Protocol):
    """Нормализованный узел. tree-sitter-узел ему почти соответствует (тонкая
    обёртка в backends/treesitter.py); .docx-backend строит такие сам."""

    @property
    def type(self) -> str: ...

    @property
    def start_row(self) -> int: ...      # 0-based, как у tree-sitter

    @property
    def end_row(self) -> int: ...        # 0-based

    def children(self) -> "list[RNode]": ...          # значимые (named) дети по порядку

    def text(self) -> str: ...                         # исходный текст узла

    def field(self, name: str) -> "Optional[RNode]": ...  # именованное поле (name/body), или None


class Backend(Protocol):
    """Парсер формата: файл/байты → корневой RNode."""

    def root(self, source: bytes) -> RNode: ...


class Spec(Protocol):
    """Декоратор формата/языка — ВЕСЬ языкозависимый нюанс тут (Vision02/03).

    Классификатор спрашивает только это; всё остальное у него общее."""

    def role(self, node: RNode) -> str: ...            # 'landmark' | 'filler' | 'frame' (Role.value)

    def name(self, node: RNode) -> str: ...            # заголовок landmark/frame

    def body(self, node: RNode) -> Optional[RNode]: ...  # scope-тело для рекурсии/всплытия

    def unwrap_frame(self, node: RNode) -> Optional[RNode]: ...  # развернуть обёртку до рамки, или None

    def unwrap_def(self, node: RNode) -> Optional[RNode]: ...    # развернуть обёртку до определения, или None

    def filler_kind(self, node: RNode) -> str: ...     # ключ группировки filler-полосы


class Analyzer(Protocol):
    """Опциональный СЕМАНТИЧЕСКИЙ пост-проход над готовым IR (Vision03, пласт 2).

    Backend даёт СТРУКТУРУ («comment-полоса [1-15]»); Analyzer по косвенным
    признакам ставит СМЫСЛ в block.description («license-блок», «docstring»).
    Реализации — от простых эвристик (regex «Copyright/SPDX») до embedder-rerank.

    ИНВАРИАНТ: аналайзер ОПЦИОНАЛЕН и ЧИСТ. Его отсутствие/падение НЕ ломает
    пайплайн — блок рендерится со структурным лейблом. Никогда не меняет
    структуру (role/range/children) — только обогащает description."""

    def describe(self, block, source: str) -> "Optional[str]": ...  # текст или None
