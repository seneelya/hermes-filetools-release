"""profiles/ — плагины языков для tree-sitter-бэкенда (Vision03). Один файл на язык.

`ts_profile_for_ext(ext)` — резолвер расширения в профиль (tree-sitter-ветвь реестра).
Добавить язык = положить рядом файл-профиль и одну строку сюда. Python-профиль
конструируется лениво (`profiles.python.make_python_profile`) — у него фолбек на ast.
"""


def ts_profile_for_ext(ext):
    """TSProfile под расширение, или None (тогда registry решает: markdown/py/ошибка)."""
    ext = ext.lower()
    if ext in ('.ts', '.js', '.mjs'):
        from .typescript import TS
        return TS
    if ext in ('.tsx', '.jsx'):
        from .typescript import TSX
        return TSX
    if ext in ('.cpp', '.cc', '.cxx', '.c++', '.hpp', '.h', '.hh', '.hxx', '.c', '.cu', '.cuh'):
        from .cpp import CPP
        return CPP
    if ext == '.cs':
        from .csharp import CS
        return CS
    if ext in ('.scss', '.sass', '.css'):
        from .css import CSS
        return CSS
    if ext in ('.sh', '.bash'):
        from .bash import BASH
        return BASH
    return None
