"""Shell profile (bash / sh) — Vision03 plugin.

Brace model fits: a function body is `{ … }` (compound_statement), a loop body is `do … done`
(do_group); both start on the header's row, so body-strict levels hold. `if` / `case` have NO body
node in this grammar (their statements are direct children) — they are not rungs; their lines are
addressed by the filler band they form inside the enclosing function (invariant #9).
"""

from ...handlers._treesitter_blocks import LangSpec
from .base import TSProfile


def _load_bash():
    import tree_sitter_bash
    from tree_sitter import Language
    return Language(tree_sitter_bash.language())


BASH = TSProfile(LangSpec(
    "Shell", _load_bash,
    body_types={'compound_statement', 'do_group'},
    transparent_parents=set(),
    named_def={'function_definition'},
    container=set(),
    control={'for_statement', 'c_style_for_statement', 'while_statement', 'select_statement'},
    scope_body='compound_statement'))
