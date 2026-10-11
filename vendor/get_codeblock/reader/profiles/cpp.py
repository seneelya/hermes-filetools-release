"""Профиль C/C++ — плагин Vision03."""

import copy

from ...handlers.cpp_handler import CPP_SPEC
from ...cpp_source import preprocess_bytes
from .base import TSProfile

# Macros tree-sitter cannot know (`GGML_DEPRECATED(decl, "hint")`, `GGML_API`, `__device__`) are
# cut before parsing (get_codeblock/cpp_source.py; line/column-preserving). On a COPY of the
# shared LangSpec — the shared one feeds the old handlers and stays as is (invariant 5).
_CPP_READER_SPEC = copy.copy(CPP_SPEC)
_CPP_READER_SPEC.preprocess = preprocess_bytes

# #ifndef-guard оборачивает весь файл (preproc_ifdef/preproc_if) — прозрачная рамка.
CPP = TSProfile(_CPP_READER_SPEC, extra_frames={'preproc_ifdef', 'preproc_if'})
