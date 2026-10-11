"""Единый реестр ридера: расширение → (Backend, Spec). ОДИН вход (Vision03).

Заменяет собой размазанную диспетчеризацию (lang_map ×3 в core.py, get_handler,
_spec_for_ext). Пока только tree-sitter-языки; новый язык = одна запись здесь,
новый backend (docx/pdf) = ветка на другой Backend-класс.
"""

from .backends.treesitter import TSBackend, TreeSitterSpec
from . import profiles


def _python_backend_spec():
    """Python: tree-sitter-python если есть; иначе ГРОМКИЙ фолбек на stdlib ast."""
    try:
        import tree_sitter_python  # noqa: F401
    except ImportError:
        import sys
        print("[fallback mode] Full analysis is unavailable: tree_sitter_python is not "
              "installed, so this Python file is parsed with the stdlib `ast` backend. "
              "In this reduced mode, comments and file-level blocks are not tracked. "
              "To enable full analysis, install the tree-sitter engine and its Python "
              "grammar:  pip install tree-sitter tree_sitter_python",
              file=sys.stderr)
        from .backends.python_ast import PythonAstBackend, PythonAstSpec
        return PythonAstBackend(), PythonAstSpec()
    from .profiles.python import make_python_profile
    prof = make_python_profile()
    return TSBackend(prof.langspec), TreeSitterSpec(prof)


def resolve(ext):
    """(Backend, Spec) под расширение, или ValueError. Единый вход: ветка на нужный
    backend по формату (tree-sitter-код / markdown / позже docx/pdf). Язык tree-sitter
    приходит ПРОФИЛЕМ-плагином из profiles/."""
    ext = ext.lower()
    if ext in ('.md', '.markdown'):                    # core2 — НЕ tree-sitter
        from .backends.markdown import MarkdownBackend, MarkdownSpec
        return MarkdownBackend(), MarkdownSpec()
    if ext == '.txt':                                   # core2 — plain prose, no markup
        from .backends.plaintext import PlainTextBackend, PlainTextSpec
        return PlainTextBackend(), PlainTextSpec()
    if ext in ('.yaml', '.yml'):                        # core2 — real grammar, own Spec
        from .backends.yaml_backend import YamlBackend, YamlSpec
        return YamlBackend(), YamlSpec()
    if ext in ('.bat', '.cmd'):                         # core2 — labels as sections, no parser
        from .backends.batch import BatchBackend, BatchSpec
        return BatchBackend(), BatchSpec()
    if ext in ('.ps1', '.psm1'):                        # core2 — real grammar, own Spec (wrappers)
        from .backends.powershell import PowerShellBackend, PowerShellSpec
        return PowerShellBackend(), PowerShellSpec()
    if ext == '.py':                                   # tree-sitter-python или ast-фолбек
        return _python_backend_spec()
    prof = profiles.ts_profile_for_ext(ext)            # core1 — tree-sitter, плагин языка
    if prof is None:
        raise ValueError(f"reader: формат {ext} пока не поддержан")
    return TSBackend(prof.langspec), TreeSitterSpec(prof)
