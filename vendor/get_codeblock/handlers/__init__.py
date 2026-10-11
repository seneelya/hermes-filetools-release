"""Language handlers registry for get_codeblock."""


def get_handler(language: str):
    """Get a language handler by name.

    Raises ValueError if the language is not supported.
    """
    handlers = {
        "python": _make_python_handler,
        "py": _make_python_handler,
        "typescript": _make_ts_handler,
        "ts": _make_ts_handler,
        "js": _make_ts_handler,
        "tsx": _make_tsx_handler,
        "jsx": _make_tsx_handler,
        "csharp": _make_csharp_handler,
        "cs": _make_csharp_handler,
        "cpp": _make_cpp_handler,
        "c++": _make_cpp_handler,
        "c": _make_cpp_handler,
        "css": _make_css_handler,
        "scss": _make_css_handler,
        "sass": _make_css_handler,
        "markdown": _make_markdown_handler,
        "md": _make_markdown_handler,
        "text": _make_text_handler,
        "txt": _make_text_handler,
        "yaml": _make_text_handler,
        "yml": _make_text_handler,
        "batch": _make_text_handler,
        "powershell": _make_text_handler,
        "shell": _make_text_handler,   # no old handler: reader-native only (stub for declarations)
    }

    factory = handlers.get(language.lower())
    if not factory:
        supported = ", ".join(sorted(handlers.keys()))
        raise ValueError(
            f"Language '{language}' not supported yet (supported: {supported})"
        )

    return factory()


def _make_python_handler():
    from .python_handler import PythonHandler
    return PythonHandler()


def _make_ts_handler():
    from .typescript_handler import TypeScriptHandler
    return TypeScriptHandler()


def _make_tsx_handler():
    from .typescript_handler import TsxHandler
    return TsxHandler()


def _make_csharp_handler():
    from .csharp_handler import CSharpHandler
    return CSharpHandler()


def _make_cpp_handler():
    from .cpp_handler import CppHandler
    return CppHandler()


def _make_css_handler():
    from .css_handler import CssHandler
    return CssHandler()


def _make_markdown_handler():
    from .markdown_handler import MarkdownHandler
    return MarkdownHandler()


def _make_text_handler():
    """Plain-text has no declared-API concept (outline/get_blocks/line_level go through
    the reader-native generic path, see reader.py) — this stub exists only so
    `Reader.open`'s eager `get_handler(language)` has something to construct."""
    class _TextHandler:
        def declarations(self, lines):
            return []
    return _TextHandler()
