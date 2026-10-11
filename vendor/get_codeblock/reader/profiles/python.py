"""Профиль Python (tree-sitter-python) — плагин Vision03.

Фолбек на stdlib `ast` (при отсутствии грамматики) — это ДРУГОЙ backend/spec,
живёт в registry, не тут (тут только tree-sitter-ветвь)."""

from .base import TSProfile


def _load_python_language():
    import tree_sitter_python
    from tree_sitter import Language
    return Language(tree_sitter_python.language())


def make_python_profile():
    from ...handlers._treesitter_blocks import LangSpec
    ls = LangSpec(
        "Python(ts)", _load_python_language,
        body_types={'block'},
        transparent_parents=set(),
        named_def={'function_definition', 'class_definition'},
        container=set(),
        control={'if_statement', 'for_statement', 'while_statement',
                 'with_statement', 'try_statement', 'match_statement'},
        scope_body='block',
    )
    return TSProfile(ls)
