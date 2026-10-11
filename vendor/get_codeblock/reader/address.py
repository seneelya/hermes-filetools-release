"""Адресация на reader-движке (Vision04) — brace-языки.

ОДИН структурный движок «какой блок на строке N» для tree-sitter brace-языков
(ts/tsx/cs/cpp/css): `get_blocks` (лестница объемлющих блоков) и `line_level`
(глубина строки). Порт handlers/_treesitter_blocks на протокол RNode+Spec —
тот же grammar, те же границы, что и у `.0`-outline (склейка преамбулы общая),
но набор рунгов адресации БОГАЧЕ карты: сюда входят braced-control (`for`/`if`)
и standalone-тела (arrow/`{…}`/object/array), а transparent-рамки (namespace/
extern "C") прозрачны для уровня — как в старом `_level_of_row`.

Python/Markdown идут своим (отступным/бesparser) путём — Reader делегирует их
существующим хендлерам, пока они не обёрнуты в backend (см. CONTEXT_RESTORE ⭐).
"""

import os

from .registry import resolve


# brace-семейство: reader-нативная адресация. Python (отступной) и Markdown
# (беsparser) НАМЕРЕННО исключены — идут своим хендлером до обёртки в backend.
_BRACE_EXTS = frozenset({
    '.ts', '.js', '.mjs', '.tsx', '.jsx', '.cs',
    '.cpp', '.cc', '.cxx', '.c++', '.hpp', '.hh', '.hxx', '.h', '.c', '.cu', '.cuh',
    '.scss', '.sass', '.css',
    '.sh', '.bash',
})


def supports(path):
    """Есть ли reader-нативная адресация для расширения (только brace-язык с langspec)."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in _BRACE_EXTS:
        return False
    try:
        _, spec = resolve(ext)
    except Exception:
        return False
    ls = getattr(spec, 'ls', None)
    return ls is not None and hasattr(ls, 'control') and hasattr(ls, 'body_types')


# -- обход дерева -----------------------------------------------------------

def _has_body(node, ls):
    return any(c.type in ls.body_types for c in node.children())


def _is_standalone_body(node, parent, ls):
    """brace-тело без своего заголовка (arrow-тело, голый `{}`, многострочный
    object/array-литерал) — foldable-регион без имени. ТОЛЬКО многострочный."""
    if node.type not in ls.body_types:
        return False
    if node.end_row <= node.start_row:
        return False
    if parent is None:
        return False
    return (parent.type not in ls.body_owners
            and parent.type not in ls.transparent_parents)


def _collect(root, ls):
    """Один обход: рунги адресации (node, parent) + все brace-тела (sr,er,transparent)."""
    blocks, bodies = [], []

    def walk(node, parent):
        t = node.type
        if t in ls.body_types:
            transparent = parent is not None and parent.type in ls.transparent_parents
            bodies.append((node.start_row, node.end_row, transparent))
        if (t in ls.named_def or t in ls.control) and _has_body(node, ls):
            blocks.append((node, parent))
        elif _is_standalone_body(node, parent, ls):
            blocks.append((node, parent))
        for c in node.children():
            walk(c, node)

    walk(root, None)
    return blocks, bodies


def _level_of_row(row, bodies):
    """Как старый: 1 + число НЕпрозрачных тел, СТРОГО содержащих строку (0-based)."""
    return 1 + sum(1 for sr, er, tr in bodies if not tr and sr < row < er)


# -- преамбула-склейка (общая семантика с outline) --------------------------

def _comment_rows(root, spec):
    """Строки, покрытые ИСКЛЮЧИТЕЛЬНО комментами (лист-узлы). Код на строке рвёт склейку.

    A `preprocess`-masked comment (opt-in, `LangSpec.is_synthetic_comment` — SCSS's `$var:`
    mask) counts as CODE here, not comment: it's a statement in disguise, not a real
    preamble — must not glue onto the block below it (cursor_feedback__gcb.md #5 residual,
    fixed 2026-09-13). Same distinction `filler_kind()` makes for the outline/map side —
    both engines must agree, or map and address diverge (CONTRACT.md invariant #6)."""
    comment_rows, code_rows = set(), set()
    is_synthetic = getattr(spec.ls, 'is_synthetic_comment', None)

    def walk(n):
        ch = n.children()
        if not ch:                                    # лист
            rows = range(n.start_row, n.end_row + 1)
            is_masked = n.type == 'comment' and is_synthetic is not None and is_synthetic(n.text())
            (code_rows if (n.type != 'comment' or is_masked) else comment_rows).update(rows)
        for c in ch:
            walk(c)

    walk(root)
    return comment_rows - code_rows


def _closed_before(row, target_start, bodies):
    """Строка ВНУТРИ тела, которое УЖЕ ЗАКРЫЛОСЬ к моменту начала target (sr < row < er
    и er <= target_start): комментарий принадлежит ЗАКРЫТОМУ sibling'у (`if (x) { …
    // note\n } else {`), а не преамбуле target. Тело-ПРЕДОК (ещё открыто на старте target,
    er > target_start — напр. `namespace { … }` вокруг всего файла) не мешает: комментарий
    внутри него, но ВЫШЕ target, — легитимная преамбула на его собственном уровне."""
    return any(sr < row < er and er <= target_start for sr, er, _ in bodies)


def _preamble_start(start_row, comment_rows, bodies, lines):
    """Поднять начало над коммент-преамбулой (комменты/пустые прямо над блоком). 0-based.

    Склейка идёт ЧИСТО по физическим строкам (без учёта AST-вложенности), поэтому обязана
    сама остановиться на границе ЗАКРЫТОГО sibling-тела (`_closed_before`) — иначе комментарий
    на строке `} else {` (последняя строка ПРЕДЫДУЩЕГО sibling-блока) утекает в преамбулу
    следующего, перепрыгивая закрывающую скобку соседа и раздувая начало вглубь его тела —
    RANGE-баг, пойманный sweep на else-if цепочках (`if/else if/else`, реальный код:
    json-schema-processors.ts, to-json-schema.ts, schemas.ts)."""
    first = start_row
    r = start_row - 1
    while r >= 0:
        if not lines[r].strip():
            r -= 1
            continue
        if r in comment_rows and not _closed_before(r, start_row, bodies):
            first = r
            r -= 1
            continue
        break
    return first


def _preamble_owner(blocks, row, comment_rows, bodies, lines):
    owners = [n for n, _ in blocks
              if _preamble_start(n.start_row, comment_rows, bodies, lines) <= row < n.start_row]
    if not owners:
        return None
    return min(owners, key=lambda n: n.start_row)


# -- label ------------------------------------------------------------------

def _same_node(a, b):
    """Identity by (type, exact row-span) rather than Python `is` — `spec.unwrap_def()`
    re-wraps the underlying tree-sitter node into a FRESH TSNode on every call (via
    `.children()`/`.field()`), so two TSNode instances pointing at the identical node are
    never `is`-equal even though they address the same source range."""
    return (a is not None and b is not None
            and a.type == b.type and a.start_row == b.start_row and a.end_row == b.end_row)


def _bound_ancestor_label(value_node, spec):
    """cursor_feedback__gcb.md #1 — a standalone function/arrow body (`const foo = () =>
    {...}`, a class field, an object property) is addressed as a ladder rung by its BODY
    node, whose immediate parent is the anonymous value (`arrow_function`/`function`) —
    that parent carries no name of its own, only its BINDER ancestor does (`const foo =`).
    Outline already resolves this via `unwrap_def`'s downward binder search (Vision03
    const-promotion); this walks the SAME relationship from the value side, upward through
    up to 3 wrapper levels (`variable_declarator` -> `lexical_declaration` -> `export_statement`,
    the deepest realistic nesting), and reuses `spec.name()` so ladder and outline render the
    IDENTICAL label for the same block. None if no such binder is found (a bare unnamed
    arrow passed as a callback argument, for instance — nothing to recover)."""
    anc = value_node.parent()
    best = None   # OUTERMOST ancestor that still resolves to value_node, not the nearest —
                  # `const foo = ...` under `export` must show "export const foo =", not just
                  # "foo =" from stopping at the innermost `variable_declarator`.
    for _ in range(3):
        if anc is None:
            break
        if _same_node(spec.unwrap_def(anc), value_node):
            best = anc
        anc = anc.parent()
    return spec.name(best) if best is not None else None


def _block_label(node, parent, spec):
    ls = spec.ls
    if node.type in ls.named_def or node.type in ls.control:
        return spec.name(node)
    ptype = parent.type if parent is not None else ''
    if node.type in ('object', 'object_pattern'):
        return "{…} object"
    if node.type in ('array', 'array_pattern'):
        return "[…] array"
    if parent is not None and parent.type in spec.profile.value_types:
        bound = _bound_ancestor_label(parent, spec)
        if bound is not None:
            return bound
    if 'arrow' in ptype:
        return "() => {…}"
    return "{…} block"


def _own_end_row(node, ls):
    """Конец рунга ПО ЕГО СОБСТВЕННОМУ телу, а не по всему AST-узлу. Грамматика прячет
    хвостовые sibling-клозы (`else_clause`/`catch_clause`/`finally_clause`) ВНУТРЬ
    родительского узла (`if_statement` кончается на конце `else`, `try_statement` — на
    конце `finally`), а мы адресуем эти клозы ОТДЕЛЬНЫМИ рунгами (`_collect` кладёт их
    в `blocks` по своему типу в `ls.control`). Без этой отсечки `if (p)` раздувался бы
    до конца чужого `else` — RANGE-баг, пойманный sweep на реальном коде.

    Правило: у control/named-узла берём конец его СОБСТВЕННОГО прямого body_types-
    ребёнка (там, где он есть) — хвостовые клозы туда не входят, они не body_types.
    Без такого ребёнка (например, `else if …` без своих скобок) — честный node.end_row."""
    if node.type not in ls.named_def and node.type not in ls.control:
        return node.end_row
    body_ends = [c.end_row for c in node.children() if c.type in ls.body_types]
    return max(body_ends) if body_ends else node.end_row


def _bounds(node, parent, bodies, comment_rows, lines, spec):
    """ЕДИНЫЙ калькулятор диапазона рунга (порт `_treesitter_blocks._bounds`): один и
    тот же [start-end] в любом режиме (ladder/query/nearest). level = глубина ЗАГОЛОВКА
    (строгое вложение тел); start поднят над коммент-преамблой; end = конец СОБСТВЕННОГО
    тела узла, не хвостового sibling-клоза (`_own_end_row`). 1-based, end inclusive."""
    return {
        'level': _level_of_row(node.start_row, bodies),
        'start': _preamble_start(node.start_row, comment_rows, bodies, lines) + 1,
        'end': _own_end_row(node, spec.ls) + 1,
        'label': _block_label(node, parent, spec),
    }


def _read_lines(path):
    with open(path, 'r', encoding='utf-8') as f:
        return f.readlines()


# -- публичный API (зеркалит хендлер) ---------------------------------------

def get_blocks(path, target_line):
    """Лестница объемлющих блоков строки `target_line` (1-based), внешний→внутренний,
    каждый — {level,start,end,label}. Порт `_treesitter_blocks.get_blocks` на RNode:
    рунги = все addressable-блоки (def/class + braced-control + standalone-тела),
    содержащие строку; тычок в коммент-преамблу → её блок (`_preamble_owner`); если ни
    в один не попали — честный file-scope `[1,N]` (инвариант #7). Контракт наружу совпадает с хендлером
    (его ест `core.resolve`/query/staircase). Внутренний вход: `Reader.get_blocks`."""
    from .classify import filler_container_at, leaf_landmark_at
    lines = _read_lines(path)
    if not lines or target_line < 1 or target_line > len(lines):
        return []
    backend, spec = resolve(os.path.splitext(path)[1])
    root = backend.root("".join(lines).encode("utf-8"))
    blocks, bodies = _collect(root, spec.ls)
    comment_rows = _comment_rows(root, spec)
    row = target_line - 1

    containing = [(n, p) for n, p in blocks
                  if n.start_row <= row <= _own_end_row(n, spec.ls)]

    if row in comment_rows:                            # тычок в преамбулу → её блок
        owner = _preamble_owner(blocks, row, comment_rows, bodies, lines)
        if owner is not None and all(owner is not n for n, _ in containing):
            containing.append(next((n, p) for n, p in blocks if n is owner))

    if not containing:
        # ИНВАРИАНТ #7 (CONTRACT): строка вне всех addressable-блоков (top-level import/
        # декларация или гэп) → честный контейнер, который ГАРАНТИРОВАННО её содержит. НЕ
        # «ближайший» блок — он строку не покрывает (в TS/CSS таких строк много: импорты,
        # top-level type/const). Старый `_nearest` (порт `_treesitter_blocks._nearest`) снят:
        # он нарушал инвариант.
        #
        # ИНВАРИАНТ #9: сначала пробуем filler-полосу (`--outline`/`--dot` её УЖЕ показывают
        # как `imports: …`/`~docstring`/…) — тот же контейнер-концепт, что и у named/control
        # блоков, просто безымянный. Она ЕСТЬ на любом скоупе (файл, внутри transparent-рамки),
        # не только на уровне файла. Только если её тоже нет (совсем пустая строка вне всего) —
        # честный file-scope `[1,N]` как последний рубеж.
        filler = filler_container_at(path, target_line)
        if filler is not None:
            return [filler]
        leaf = leaf_landmark_at(path, target_line)     # bodyless landmark the map shows (#6)
        if leaf is not None:
            return [leaf]
        return [{'level': 1, 'start': 1, 'end': len(lines), 'label': '<file>'}]

    containing.sort(key=lambda np: (np[0].start_row, -np[0].end_row))
    result = [_bounds(n, p, bodies, comment_rows, lines, spec) for n, p in containing]

    # ИНВАРИАНТ #9 (расширение): даже когда есть охватывающий адресуемый рунг, ВНУТРИ его
    # тела может сидеть более узкая filler-полоса — поле класса, топ-левел-подобный член,
    # который не заводит свой control/named_def-рунг (`--dot` её УЖЕ показывает, `.` на
    # уровень глубже родителя). Если она строго уже последнего найденного рунга — это
    # честный ЕЩЁ БОЛЕЕ внутренний контейнер, добавляем его. Тот же путь (`filler_container_at`)
    # сам себя ограничивает: если внутри innermost сидит ещё один control/named_def-блок,
    # он там его найдёт как landmark/frame и остановится, не долетев до filler глубже —
    # не задвоит уже найденный рунг.
    inner = result[-1]
    filler = filler_container_at(path, target_line)
    if (filler is not None and filler['start'] >= inner['start'] and filler['end'] <= inner['end']
            and (filler['start'], filler['end']) != (inner['start'], inner['end'])):
        result.append({**filler, 'level': inner['level'] + 1})
    return result


def line_level(path, idx):
    """Глубина строки `idx` (0-based) = `_level_of_row`: 1 + число НЕпрозрачных тел, строго
    её содержащих. Порт `_treesitter_blocks.line_level`. Вход: `Reader.line_level`."""
    lines = _read_lines(path)
    if idx < 0 or idx >= len(lines):
        return 1
    backend, spec = resolve(os.path.splitext(path)[1])
    root = backend.root("".join(lines).encode("utf-8"))
    _, bodies = _collect(root, spec.ls)
    return _level_of_row(idx, bodies)
