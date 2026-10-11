"""Reader — единая точка входа приложения (Vision03).

Проверенные режимы (outline / get_blocks / line_level / declarations) ДЕЛЕГИРУЮТСЯ
существующему языковому хендлеру — поведение и паритет (оракул 88/88) сохраняются
бесплатно. Новый режим `.0` (IR-классификация) идёт через reader-backend.

Смысл: `core.py` ходит СЮДА, а не в `get_handler` напрямую. Это фронт-дверь, за
которой позже консолидируются registry/лестница/анализаторы — без правки CLI.
Интерфейс намеренно совпадает с хендлером, поэтому Reader — drop-in замена.
"""

from pathlib import Path

# `.py` — единственное расширение с собственным (отступным) адресным хендлером; всё
# остальное non-brace идёт через generic `classify.ladder_at`. Проверяем РАСШИРЕНИЕ
# напрямую (не `self.language`/`_LANG_MAP` — та карта на новый формат может не знать и
# молча упадёт на 'python' по умолчанию, что увело бы адресацию НОВОГО формата на
# python_handler вместо generic-пути; расширение — единственный надёжный сигнал).
_PY_EXTS = {'.py'}

# Один маппинг ext -> language (позже вытеснит 3 копии lang_map в core.py).
_LANG_MAP = {
    '.py': 'python',
    '.ts': 'typescript', '.js': 'typescript', '.mjs': 'typescript',
    '.tsx': 'tsx', '.jsx': 'tsx',
    '.cs': 'csharp',
    '.cpp': 'cpp', '.cc': 'cpp', '.cxx': 'cpp', '.c++': 'cpp',
    '.hpp': 'cpp', '.hh': 'cpp', '.hxx': 'cpp', '.h': 'cpp', '.c': 'cpp', '.cu': 'cpp', '.cuh': 'cpp',
    '.scss': 'css', '.sass': 'css', '.css': 'css',
    '.md': 'markdown', '.markdown': 'markdown',
    '.txt': 'text',
    '.yaml': 'yaml', '.yml': 'yaml',
    '.sh': 'shell', '.bash': 'shell',
    '.ps1': 'powershell', '.psm1': 'powershell',
    '.bat': 'batch', '.cmd': 'batch',
}


def language_for_ext(ext):
    return _LANG_MAP.get(ext.lower(), 'python')


class Reader:
    def __init__(self, path, lines, language, handler):
        self.path = path
        self.lines = lines
        self.language = language
        self.handler = handler

    @classmethod
    def open(cls, path, lines, language=None):
        if language is None:
            language = language_for_ext(Path(path).suffix)
        from ..handlers import get_handler
        return cls(path, lines, language, get_handler(language))

    # -- проверенные режимы: делегация хендлеру (drop-in-сигнатуры) --------

    def outline(self, lines, max_level=None, deep=False, focus_line=None, focus_level=0):
        """Unified-outline (Vision03): `.0`-карта (landmark'и вглубь), а НЕ делегация
        старому хендлеру. Адаптивную глубину режет core.py. deep=True (диагностический
        `--dot`) — filler на ВСЕХ раскрытых уровнях; deep=False (`--outline`) — только
        на уровне файла. focus_line задан — карта только блока-цели (K=focus_level предок)."""
        from .classify import outline_rows
        rows = outline_rows(self.path, deep=deep, focus_line=focus_line, focus_level=focus_level)
        if max_level:
            rows = [r for r in rows if r['level'] <= max_level]
        return rows

    def get_blocks(self, file_path, line):
        """Адресация через reader (Vision04). Три пути:
        1. brace-языки (`address._BRACE_EXTS`) — богатый reader-движок (control-блоки,
           standalone-тела), границы совпадают с outline.
        2. `.py` — отступной хендлер, сохранён осознанно (без парсера, CONTEXT_RESTORE ⭐).
        3. ЛЮБОЙ другой формат (markdown, будущие docx/pdf) — generic `classify.ladder_at`:
           landmark/frame + filler-терминал (инвариант #9), без brace-специфики. Работает
           «из коробки» для нового формата — нужен только Spec, ни строчки адресного кода
           (Vision03: «новый язык/формат = данные»)."""
        from . import address
        if address.supports(file_path):
            return address.get_blocks(file_path, line)
        if Path(file_path).suffix.lower() in _PY_EXTS:
            return self.handler.get_blocks(file_path, line)
        from .classify import ladder_at
        return ladder_at(file_path, line)

    def line_level(self, lines, idx):
        from . import address
        if address.supports(self.path):
            return address.line_level(self.path, idx)
        if Path(self.path).suffix.lower() in _PY_EXTS:
            return self.handler.line_level(lines, idx)
        from .classify import line_level_at
        return line_level_at(self.path, idx)

    def declarations(self, lines):
        return self.handler.declarations(lines)

    # -- новый режим: .0 (IR через reader-backend) ------------------------

    def classify(self, depth=0):
        from .classify import classify_file
        return classify_file(self.path, depth=depth)
