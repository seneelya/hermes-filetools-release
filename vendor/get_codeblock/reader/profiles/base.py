"""Профиль языка — плагин (Vision03): structural LangSpec + reader-надстройки.

Профиль — ЕДИНСТВЕННОЕ язык-специфичное место ридера для tree-sitter-бэкенда.
Несёт: (1) LangSpec — структурные наборы node-типов, общие с рабочим outline/query
(их НЕ трогаем, оракул 88/88); (2) reader-надстройки поверх: прозрачные рамки,
которых нет в LangSpec, и правила промоушена `NAME = value` → landmark.

`TreeSitterSpec` — тонкий ДВИЖОК над профилем (вся логика там, тут только данные).
Новый tree-sitter-язык = новый файл-профиль рядом (Vision03 «язык = запись данных»).
"""


class TSProfile:
    """Декларации плагина языка для tree-sitter-бэкенда. Только данные, без логики."""
    __slots__ = ('langspec', 'extra_frames', 'binders', 'value_types')

    def __init__(self, langspec, extra_frames=(), binders=(), value_types=()):
        self.langspec = langspec
        self.extra_frames = frozenset(extra_frames)   # рамки вне LangSpec.transparent_parents
        self.binders = tuple(binders)                 # узлы-привязки NAME = value (промоушен)
        self.value_types = tuple(value_types)         # типы значения, промотируемого в landmark
