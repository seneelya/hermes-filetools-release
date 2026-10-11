"""Tree-sitter based C/C++ handler for get_codeblock.

Navigation (outline/get_blocks/line_level) comes from the shared tree-sitter
block engine in `_ts_blocks`; this module only declares the C++ node-type sets.
A real parse handles multi-line signatures, `template<...>`, `Class::method`,
macros and raw string literals correctly -- things a brace heuristic trips on.

Block model / level rule: see `_ts_blocks`. For C++ specifically:
  * `namespace` and `extern "C"` (linkage_specification) are TRANSPARENT.
  * function/method definitions, class/struct/union/enum, and control blocks
    (if/for/while/switch/try/lambda/bare {}) are blocks.
  * `#include`, `using`/alias, forward decls and fields are leaves.

Requires: pip install tree-sitter tree-sitter-cpp
"""

from ._treesitter_blocks import LangSpec, TreeSitterBlockHandler


def _load_cpp_language():
    import tree_sitter_cpp
    from tree_sitter import Language
    return Language(tree_sitter_cpp.language())


CPP_SPEC = LangSpec(
    "C/C++",
    _load_cpp_language,
    body_types={
        'compound_statement', 'declaration_list',
        'field_declaration_list', 'enumerator_list',
    },
    transparent_parents={'namespace_definition', 'linkage_specification'},
    named_def={
        'function_definition',
        'class_specifier', 'struct_specifier', 'union_specifier', 'enum_specifier',
    },
    container={'namespace_definition', 'linkage_specification'},
    control={
        'if_statement', 'else_clause',
        'for_statement', 'for_range_loop',
        'while_statement', 'do_statement',
        'switch_statement', 'case_statement',
        'try_statement', 'catch_clause', 'seh_try_statement',
        'lambda_expression',
    },
    scope_body='compound_statement',
    cut_extra={'field_initializer_list'},   # drop ctor `: a(a), b(b)` from labels
)


class CppHandler(TreeSitterBlockHandler):
    SPEC = CPP_SPEC
