"""Backend-agnostic лейблер filler-полосы (Vision03): band → строка ИМЁН.

Заголовок полосы — это ОГЛАВЛЕНИЕ (список имён), а не содержимое. Полное всегда
через `--query` по диапазону. Движок общий; «как достать имя из узла» задаёт
профиль (`name_of`), поэтому одинаково годен для tree-sitter и ast.

Правила синхронны между Spec'ами (полный tree-sitter ≥ degraded ast), чтобы фолбек
не показывал больше полного (Vision02).
"""

from .profiles.presets import (HUMAN_KIND, IMPORT_KINDS, ASSIGN_WRAPPERS,
                               BINDER_TYPES, NAME_TYPES, CAP)


def _clean(text):
    return " ".join(text.split()).strip() if text else None


def _unquote(text):
    t = _clean(text)
    return t.strip('\'"') if t else t


def ts_name_of(node):
    """Извлекатель имени для tree-sitter-узла (RNode). Консервативен: имя отдаём только
    для импортов и явных привязок; для вызовов/управляющих — None (полоса остаётся ~kind)."""
    t = node.type
    if t in IMPORT_KINDS:
        m = node.field('module_name') or node.field('name')
        if m is not None:
            return _clean(m.text())
        src = node.field('source')                      # JS/TS: from "module"
        if src is not None:
            return _unquote(src.text())
        for c in node.children():                       # первый модуль/идентификатор/строка
            if c.type in NAME_TYPES:
                return _clean(c.text())
            if c.type in ('string', 'string_literal'):
                return _unquote(c.text())
        return None
    if t in ASSIGN_WRAPPERS:
        tgt = _binding_target(node)
        return _clean(tgt.text()) if tgt is not None else None
    return None


def _binding_target(node):
    """Цель привязки (имя слева от `=`) внутри обёртки, поиск вширь ≤2."""
    stack = [(node, 0)]
    while stack:
        n, d = stack.pop(0)
        if n.type in BINDER_TYPES:
            return n.field('left') or n.field('name') or n.field('declarator')
        if d <= 2:
            for c in n.children():
                stack.append((c, d + 1))
    return None


def band_label(nodes, name_of):
    """Заголовок полосы из её узлов, или None (тогда рендер оставит `~kind xN`).

    Однотипная полоса → `word: a, b, c`. Часть узлов без имени (напр. вызовы среди
    присваиваний) — не усложняем: если разобрали меньшинство, ставим `word +multiType`."""
    word = HUMAN_KIND.get(nodes[0].type, nodes[0].type)
    names, missing, seen = [], 0, set()
    for n in nodes:
        nm = name_of(n)
        if nm and nm not in seen:
            seen.add(nm)
            names.append(nm)
        elif not nm:
            missing += 1
    if not names:
        return None
    head = ", ".join(names[:CAP]) + (", …" if len(names) > CAP else "")
    tail = " +multiType" if missing else ""            # часть узлов иного рода — флаг
    return f"{word}: {head}{tail}"
