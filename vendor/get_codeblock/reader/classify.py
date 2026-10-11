"""Backend-agnostic .0-классификатор (Vision02/03) на протоколе.

Ровно та же механика, что в handlers/dot_classify.py, но работает через `Spec`+
`RNode`, не зная про tree-sitter. Порт — чтобы доказать, что классификатор
отвязывается от backend'а (шаг к core2 docx/pdf).
"""

import os
import re

from .ir import Block, Role
from .profiles.presets import NAME_TYPES
from .registry import resolve


# Признаки лицензионного блока — язык-независимо (по содержимому коммента, не по языку).
_LICENSE_RE = re.compile(
    r'SPDX-License-Identifier'
    r'|Copyright\s*(\(c\)|©|\d{4})'
    r'|Licensed under\b'
    r'|Permission is hereby granted'
    r'|Redistribution and use'
    r'|All rights reserved'
    r'|GNU (General|Lesser) Public License'
    r'|Apache License|MIT License|BSD [0-9-]*Clause',
    re.IGNORECASE)


def _comment_band_label(nodes):
    """Метка comment-полосы: `license block` если распознан лицензионный блок, иначе
    первая СОДЕРЖАТЕЛЬНАЯ строка (декоративные разделители пропускаются). Язык-независимо."""
    text = "\n".join(n.text() for n in nodes)
    if _LICENSE_RE.search(text):
        return 'license block'
    return _first_comment_line(nodes)


def _first_comment_line(nodes, cap=60):
    """Первая СОДЕРЖАТЕЛЬНАЯ строка комментов полосы — для подписи блока (преамбула).
    Декоративные строки-разделители (`#====`, `----`, `____`, `####`, `***` …)
    пропускаем: язык-независимо — содержательная строка содержит хотя бы одну БУКВУ
    (буквы и есть текст, разделители — только пунктуация). Обрезаем длинное; '…' если
    в полосе есть ещё строки."""
    lines = []
    for n in nodes:
        lines.extend(n.text().splitlines())
    chosen = next((ln.strip() for ln in lines if any(ch.isalpha() for ch in ln)), None)
    if not chosen:
        return None
    multi = len(lines) > 1
    if len(chosen) > cap:
        return chosen[:cap].rstrip() + '…'
    return chosen + ' …' if multi else chosen


class Classifier:
    def __init__(self, spec):
        self.spec = spec

    def classify(self, scope, level, depth, top_filler_only=False, skip_leaf_names=False):
        """Классифицировать прямых детей scope на уровне `level`. depth>0 —
        рекурсивно раскрыть тела landmark-ов на уровень глубже.

        top_filler_only=True (режим outline): filler-полосы показываем ТОЛЬКО на
        уровне файла (level==1). Присвоения/комменты внутри подобъектов малоинформативны
        фактом наличия — это данные для --query. Рамки не углубляют, поэтому filler в
        рамке уровня файла остаётся level==1 и показывается.

        ПРЕАМБУЛА: comment-полоса ПРЯМО над landmark'ом вливается в него — граница блока
        поднимается вверх над коммент, отдельной строкой он не показывается, а его первая
        строка дописывается в подпись блока (информативность для --query). Коммент НЕ над
        блоком (закомментированный текст) остаётся обычным filler на своём уровне."""
        out = []
        run = None       # текущая filler-полоса: [kind, start, end, nodes]
        pending = None   # удержанная comment-полоса (start, end, nodes) — вдруг ниже landmark

        def make_filler(kind, s, e, nodes):
            if top_filler_only and level > 1:              # outline: filler только на файле
                return None
            if kind == 'comment':                          # коммент-полоса: 1-я значимая строка / license
                label = _comment_band_label(nodes)
            else:
                labeler = getattr(self.spec, 'filler_label', None)   # опц. (Vision03)
                label = labeler(nodes) if labeler is not None else None
            return Block(Role.FILLER, kind, s, e, level, name=label, count=len(nodes))

        def release_pending():
            nonlocal pending
            if pending is not None:
                blk = make_filler('comment', *pending)     # ниже не блок — коммент как есть
                if blk is not None:
                    out.append(blk)
                pending = None

        def flush():
            nonlocal run, pending
            if run is None:
                return
            kind, s, e, nodes = run
            run = None
            if kind == 'comment':
                release_pending()                          # два коммент-бэнда подряд — прежний отдать
                pending = (s, e, nodes)                     # держим: вдруг ниже landmark
                return
            release_pending()                              # не-comment полоса рвёт склейку
            blk = make_filler(kind, s, e, nodes)
            if blk is not None:
                out.append(blk)

        for child in scope.children():
            # Рамка без тела рекурсит СВОИ дети, среди которых её токен-имя (guard
            # `#ifndef NAME` → identifier, C#-namespace → qualified_name). Это не контент —
            # отсеваем, иначе имя рамки утекает отдельной filler-строкой `~identifier`.
            if skip_leaf_names and child.type in NAME_TYPES:
                continue
            s = child.start_row + 1
            e = child.end_row + 1
            frame = self.spec.unwrap_frame(child)
            defn = None if frame is not None else self.spec.unwrap_def(child)

            if frame is None and defn is None:            # filler
                kind = self.spec.filler_kind(child)
                if kind == 'comment' and run is not None and s <= run[2]:
                    # Trailing companion comment on the SAME physical line as the run's
                    # last line (`x = 1  # note`) — glue it into the block (extend end)
                    # but do NOT add it to `nodes`: a trailing comment documents the
                    # statement, it doesn't rename/retype it. Real bug this fixes: a
                    # `# resolved once, cached` after an assignment used to spawn its OWN
                    # `~comment [N-N]` filler row, splitting one logical line into two and
                    # (via band_label's missing-name tally) risked a spurious +multiType
                    # on the run it interrupted.
                    run[2] = max(run[2], e)
                elif run is not None and run[0] == kind:
                    run[2] = e
                    run[3].append(child)
                else:
                    flush()
                    run = [kind, s, e, [child]]
                continue

            flush()

            if frame is not None:                         # рамка: дети всплывают (тот же level)
                release_pending()                         # к рамке не клеим — коммент как есть
                entry = Block(Role.FRAME, frame.type, s, e, level, name=self.spec.name(frame))
                body = self.spec.body(frame)
                scope2 = body if body is not None else frame
                # у рамки без тела дети = её собственные узлы (вкл. токен-имя) → отсеиваем его
                entry.children = self.classify(scope2, level, depth, top_filler_only,
                                               skip_leaf_names=(body is None))
                out.append(entry)
            else:                                         # landmark: имя от внешнего узла (с export/deco)
                entry = Block(Role.LANDMARK, defn.type, s, e, level, name=self.spec.name(child))
                if pending is not None:                   # ПРЕАМБУЛА: коммент над блоком вливается
                    entry.start = pending[0]
                    first = _first_comment_line(pending[2])
                    if first:
                        entry.name = f"{entry.name}  {first}"
                    pending = None
                if depth > 0:
                    body = self.spec.body(child)
                    if body is not None:
                        entry.children = self.classify(body, level + 1, depth - 1, top_filler_only)
                out.append(entry)

        flush()
        release_pending()
        return out


def classify_file(path, depth=0):
    ext = os.path.splitext(path)[1]
    backend, spec = resolve(ext)
    with open(path, 'rb') as f:
        src = f.read()
    root = backend.root(src)
    return Classifier(spec).classify(root, level=1, depth=depth)


_OUTLINE_FULL_DEPTH = 64   # раскрыть landmark'и до конца; отображение режет адаптив core.py


def _is_focus_block(spec, node):
    """Узел-цель для фокуса = именованный блок (landmark) или прозрачная рамка."""
    return spec.unwrap_def(node) is not None or spec.unwrap_frame(node) is not None


def _owning_block(spec, children, line, want_glue=False):
    """Focus-блок среди `children`, которому принадлежит `line` — С УЧЁТОМ ПРЕАМБУЛЫ:
    если строка попала на doc/коммент прямо над блоком (только комменты/пусто между),
    она принадлежит этому блоку (как в склейке). Иначе — блок, чей диапазон её содержит.

    want_glue=True → вернуть (node, glued_start) где glued_start (1-based) поднят над
    коммент-преамбулой блока (как старый `_bounds` через `_preamble_start`). Иначе — node.

    `spec.filler_kind(ch) == 'comment'`, NOT raw `ch.type == 'comment'`: a `preprocess`-
    masked node (SCSS `$var:` mask, `LangSpec.is_synthetic_comment`) is `type=='comment'`
    at the tree level but `filler_kind` relabels it — it's a statement in disguise, must
    NOT glue onto the block below as preamble (cursor_feedback__gcb.md #5 residual, fixed
    2026-09-13; same distinction the Classifier's own filler-run grouping already makes —
    this function just re-derives raw node types instead of asking `filler_kind`, so it
    had drifted out of sync)."""
    pre = None   # начало непрерывного коммент-рана перед текущим focus-блоком
    for ch in children:
        s, e = ch.start_row + 1, ch.end_row + 1
        if _is_focus_block(spec, ch):
            lo = pre if pre is not None else s     # диапазон + его преамбула
            if lo <= line <= e:
                return (ch, lo) if want_glue else ch
            pre = None
        elif spec.filler_kind(ch) == 'comment':
            if pre is None:
                pre = s                            # старт коммент-рана (преамбула следующего блока)
        else:
            pre = None                             # код рвёт преамбулу
    return (None, None) if want_glue else None


def _filler_at(spec, scope, level, line):
    """Filler-полоса (imports/comments/assign-run/docstring) среди ПРЯМЫХ детей `scope` на
    глубине `level`, содержащая `line`, или None. Гоняет настоящий `Classifier.classify`
    (тот же run-группировщик + склейка хвостовых комментов, что и `--outline`/`--dot`) —
    НЕ повторяет правила группировки отдельно, диапазоны/лейблы гарантированно те же,
    что в карте."""
    for b in Classifier(spec).classify(scope, level, depth=0):
        if b.role is Role.FILLER and b.start <= line <= b.end:
            return b
    return None


def _containing_chain(root, spec, line):
    """Цепочка объемлющих блоков строки `line`, внешний→внутренний: landmark/frame (спуск по
    reader-дереву, как раньше), а когда НИ ОДИН из них не содержит строку на каком-то скоупе —
    filler-полоса ТОГО ЖЕ скоупа (`_filler_at`) как ПОСЛЕДНЕЕ звено (у filler нет тела, вглубь
    не спускаемся). Раньше filler не считался «содержащим блоком» вообще — строка внутри
    top-level импортов/докстринга/комментария не находила НИЧЕГО (`chain=[]`); теперь она
    получает свою полосу, тот же принцип, что уже работает для адресации `get_blocks`
    (invariant #7) — filler = легитимный контейнер на своём уровне («.», «.2», …), не только
    на уровне файла. Backend-agnostic.

    Возвращает `(chain, levels)`: `chain` — RNode (landmark/frame) и, не более одного, `Block`
    (filler) в конце; `levels[i]` — РЕАЛЬНАЯ глубина `chain[i]` (прозрачные рамки — namespace/
    `extern "C"` — level НЕ увеличивают, как везде в тулзе: `address._level_of_row`, CONTRACT
    инвариант про прозрачные рамки). Раньше глубину цели пересчитывали снаружи как «позиция в
    chain + 1» (`outline_rows`) — тот же баг: рамка в цепочке засчитывалась как +1 уровень,
    депф врал на файлах с namespace (`ValueError: max() arg is an empty sequence`, когда цель
    оказывалась filler на неверно завышенном уровне — сама себя отфильтровывала в core.py)."""
    chain, levels, cur, guard, level = [], [], root, 0, 1
    while guard < 512:
        guard += 1
        nxt = _owning_block(spec, cur.children(), line)
        if nxt is None:
            filler = _filler_at(spec, cur, level, line)
            if filler is not None:
                chain.append(filler)
                levels.append(level)
            break
        chain.append(nxt)
        levels.append(level)
        if spec.unwrap_def(nxt) is not None:            # landmark — углубляет; frame — нет
            level += 1
        # Same descent as the Classifier (map): a frame WITHOUT a body node (`#ifdef X` —
        # preproc_ifdef keeps its statements as direct children) is itself the scope. Before,
        # the chain stopped here and every line inside `#ifdef __cplusplus` of a C header got
        # `<file>` while the map showed its real block (invariants #6/#9).
        frame = spec.unwrap_frame(nxt)
        body = spec.body(frame) if frame is not None else spec.body(nxt)
        if body is None:
            if frame is None:
                break
            body = frame
        cur = body
    return chain, levels


def filler_container_at(path, line):
    """Самая ВНУТРЕННЯЯ filler-полоса (imports/comments/assign-run/docstring/поле класса),
    содержащая `line` — переиспользует обход focus-цепочки (`_containing_chain`): landmark/
    frame спускаемся как обычно, а когда НИ ОДИН из них строку дальше не сужает — берём
    filler ТОГО скоупа. Landmark ЗАКОННО стоит выше по цепочке (класс/функция, внутри
    которых сидит эта filler-полоса) — это НЕ повод отказаться, это и есть наш случай.

    Возвращает {'level','start','end','label'} (форма рунга `get_blocks`) или None — если
    вообще ничего не нашлось (честный file-scope/охватывающий landmark остаётся на
    вызывающей стороне).

    Два способа применения (оба в `address.get_blocks`/`python_handler._orphan`):
    1. Инвариант #7 fallback — `containing` вообще пуст (top-level строка без адресуемого
       блока) — единственный кандидат.
    2. Инвариант #9 расширение — `containing` НЕ пуст (строка внутри def/class), но внутри
       ТЕЛА самого внутреннего адресуемого рунга есть более узкая filler-полоса (поле класса,
       не заводящее свой control/named_def-рунг) — добавляется как ЕЩЁ ОДИН, более внутренний
       рунг (см. вызывающий код: сравнение диапазонов решает, короче ли она).

    ОДИН источник правды с `--outline`/`--dot`, не дублирует группировку."""
    backend, spec = resolve(os.path.splitext(path)[1])
    with open(path, 'rb') as f:
        root = backend.root(f.read())
    chain, _levels = _containing_chain(root, spec, line)
    if not chain:
        return None
    last = chain[-1]
    if not isinstance(last, Block) or last.role is not Role.FILLER:
        return None                                      # цепочка кончилась на landmark/frame
    label = last.name or ('~' + last.kind + (f' x{last.count}' if last.count > 1 else ''))
    return {'level': last.level, 'start': last.start, 'end': last.end, 'label': label}


def ladder_at(path, line):
    """Generic backend-agnostic `get_blocks`: `_containing_chain` reshaped into the rung
    format `{level,start,end,label}` (внешний→внутренний), с той же преамбула-склейкой у
    landmark/frame (`_owning_block(want_glue=True)`, как в `outline_rows`'s focus-ветке) и
    filler-терминалом (инвариант #9). НЕ содержит brace-специфики (control-блоки,
    standalone-тела) — тех рунгов, что даёт `address.py`, тут не будет, потому что у
    non-code форматов (markdown, будущие docx/pdf) им нет аналога в LangSpec.

    Это ДЕФОЛТНАЯ адресация для любого backend'а, который НЕ на `address._BRACE_EXTS`
    (богаче) и НЕ Python (свой отступной хендлер, сохранён осознанно — см.
    CONTEXT_RESTORE_TOOLS.md). Новый формат/язык получает работающий `--line` СРАЗУ,
    без единой строки адресного кода — только Spec (Vision03: «новый язык = данные»).
    Инвариант #7: ничего не нашлось → честный file-scope `[1, N]`."""
    chain, rungs, n_lines = _chain_rungs(path, line)
    if not chain:
        return [{'level': 1, 'start': 1, 'end': n_lines, 'label': '<file>'}]
    return rungs


def _chain_rungs(path, line):
    """(chain, rungs, n_lines): `_containing_chain` + its rung form — shared by `ladder_at`
    and `leaf_landmark_at` (one derivation of starts/labels, the map's own)."""
    backend, spec = resolve(os.path.splitext(path)[1])
    with open(path, 'rb') as f:
        src = f.read()
    root = backend.root(src)
    n_lines = max(src.count(b'\n') + (0 if src.endswith(b'\n') else 1), 1)
    chain, levels = _containing_chain(root, spec, line)
    rungs = []
    parent_scope = root
    for node, lvl in zip(chain, levels):
        if isinstance(node, Block):                          # filler-терминал (инвариант #9)
            label = node.name or ('~' + node.kind + (f' x{node.count}' if node.count > 1 else ''))
            rungs.append({'level': lvl, 'start': node.start, 'end': node.end, 'label': label})
            break
        _n, glued = _owning_block(spec, parent_scope.children(), node.start_row + 1, want_glue=True)
        head = _focus_head(spec, node, lvl, start_override=glued)
        rungs.append({'level': head.level, 'start': head.start, 'end': head.end, 'label': head.name})
        parent_scope = _scope_inside(spec, node, parent_scope)
    return chain, rungs, n_lines


def _scope_inside(spec, node, outer):
    """The scope a chain link opens for the next link: its body, a frame's body, or a bodyless
    frame (`#ifdef X`, `#else`) itself; a link without any keeps `outer`. One rule for the
    ladder (`_chain_rungs`) and the focus map (`outline_rows`)."""
    frame = spec.unwrap_frame(node)
    body = spec.body(frame) if frame is not None else spec.body(node)
    if body is None and frame is not None:
        body = frame
    return body if body is not None else outer


def leaf_landmark_at(path, line):
    """A landmark WITHOUT a body that owns `line` (C/C++: `typedef struct x * x_t;`, a forward
    `struct x;`) — the map shows it as a numbered block, but the brace engine only knows rungs
    with bodies, so `--line` on it fell back to `<file>` (invariant #6: one question, one
    answer). -> rung {'level','start','end','label'} or None. Same chain as the map."""
    chain, rungs, _n = _chain_rungs(path, line)
    if not chain or isinstance(chain[-1], Block) or not rungs:
        return None
    backend, spec = resolve(os.path.splitext(path)[1])
    last = chain[-1]
    if spec.unwrap_frame(last) is not None or spec.body(last) is not None:
        return None
    return rungs[-1]


def line_level_at(path, idx):
    """Generic `line_level` (0-based `idx`), пара к `ladder_at`: глубина = уровень
    последнего звена цепочки (`_containing_chain`) — landmark/frame level или, если
    цепочка кончилась filler'ом, его level. Пустая строка вне всего — 1 (file-scope)."""
    backend, spec = resolve(os.path.splitext(path)[1])
    with open(path, 'rb') as f:
        root = backend.root(f.read())
    chain, levels = _containing_chain(root, spec, idx + 1)
    if not chain:
        return 1
    last = chain[-1]
    return last.level if isinstance(last, Block) else levels[-1]


def _pick_focus(chain, level):
    """Выбор цели из цепочки по --level (как core.resolve): 0=внутренний, +1=верхний (от файла),
    -N=на N вверх от внутреннего."""
    if not chain:
        return None
    if level == 0:
        return chain[-1]
    if level < 0:
        idx = len(chain) - 1 + level
        return chain[0] if idx < 0 else chain[idx]
    return chain[min(level - 1, len(chain) - 1)]


def _focus_head(spec, node, level, start_override=None):
    """Block-заголовок цели на её РЕАЛЬНОЙ файловой глубине `level` (не ре-базируем в 1 —
    иначе хедер/отступ врут о глубине). Тело раскрывается с level+1.

    start_override (1-based) — начало, поднятое над коммент-преамблой блока, чтобы focus-
    карта давала тот же [start-end], что полный outline и адресация (иначе focus показывал
    сырой node.start без склеенного док-коммента → три движка расходились)."""
    frame = spec.unwrap_frame(node)
    s = start_override if start_override is not None else node.start_row + 1
    e = node.end_row + 1
    if frame is not None:
        return Block(Role.FRAME, frame.type, s, e, level, name=spec.name(frame))
    return Block(Role.LANDMARK, spec.unwrap_def(node).type, s, e, level, name=spec.name(node))


def outline_rows(path, deep=False, focus_line=None, focus_level=0):
    """Unified-outline (Vision03): `.0`-строки для адаптивного outline в core.py.
    Landmark'и раскрыты вглубь. deep=False (`--outline`) — filler только на уровне файла;
    deep=True (диагностический `--dot`) — filler на ВСЕХ раскрытых уровнях. Строки несут
    флаги frame/filler (обе рисуются точкой; в подсчёт глубины/тэлли идут только landmark'и).
    Формат строки совпадает со старым handler.outline: {level,start,end,text,frame}.

    focus_line задан → карта ТОЛЬКО блока-цели (K-предок строки, K=focus_level): цель на
    уровне 1, её тело — вглубь. Решает монстро-классы: развернуть один блок как отдельный файл."""
    backend, spec = resolve(os.path.splitext(path)[1])
    with open(path, 'rb') as f:
        root = backend.root(f.read())
    clf = Classifier(spec)
    if focus_line is not None:
        chain, levels = _containing_chain(root, spec, focus_line)
        target = _pick_focus(chain, focus_level)
        if target is None:
            tree = []
        elif isinstance(target, Block):
            # Filler-terminated chain (imports/comments/assign-run/docstring — see
            # `_containing_chain`): `_filler_at` already built this via the real
            # Classifier at the right depth/range (transparent frames already excluded —
            # see `levels`). It's a leaf (no body to descend into, nothing to glue —
            # Classifier's own run-grouping already settled its bounds).
            tree = [target]
        else:
            idx = chain.index(target)
            base = levels[idx]                 # РЕАЛЬНАЯ глубина цели (рамки не углубляют)
            # Склеить коммент-преамблу цели: спрашиваем want_glue у её РОДИТЕЛЬСКОГО скоупа
            # (root для верхнего уровня, тело родителя-по-цепочке иначе). Тот же расчёт, что
            # в полном outline (pending) и в адресации (_preamble_start) — один [start-end].
            # Scope by the ladder's rule (`_scope_inside`): a bodyless parent (`#else` around
            # a definition, REQ-015) is a scope itself, never None.
            parent_scope = root
            for link in chain[:idx]:
                parent_scope = _scope_inside(spec, link, parent_scope)
            _n, glued = _owning_block(spec, parent_scope.children(), target.start_row + 1,
                                      want_glue=True)
            head = _focus_head(spec, target, base, start_override=glued)
            body = _scope_inside(spec, target, None)
            if body is not None:
                head.children = clf.classify(body, level=base + 1, depth=_OUTLINE_FULL_DEPTH,
                                             top_filler_only=not deep)
            tree = [head]
    else:
        tree = clf.classify(root, level=1, depth=_OUTLINE_FULL_DEPTH, top_filler_only=not deep)
    rows = []

    def walk(items):
        for b in items:
            text = b.name if b.name else '~' + b.kind + (f' x{b.count}' if b.count > 1 else '')
            rows.append({'level': b.level, 'start': b.start, 'end': b.end, 'text': text,
                         'frame': b.role is Role.FRAME, 'filler': b.role is Role.FILLER})
            if b.children:
                walk(b.children)

    walk(tree)
    return rows


def render(blocks, marker='#'):
    """Метка = обозначение УРОВНЯ. Отступ = глубина уровня (` ` * level). Символ:
    landmark — номер уровня (`1`/`2`), frame и filler — точка (`.`, «сам уровень/скоуп»,
    а не именованное определение). Точки и числа в одной колонке — консистентно.
    Диапазоны выровнены."""
    flat = []

    def walk(items):
        for b in items:
            indent = ' ' * b.level
            dot = '.' + (str(b.level) if b.level > 1 else '')   # '.N' (N≥2); на уровне 1 голая '.'
            if b.role is Role.LANDMARK:
                sym, content = indent + str(b.level), b.name
            elif b.role is Role.FRAME:
                sym, content = indent + dot, b.name
            else:
                sym = indent + dot                      # '.N' — уровень без имени
                if b.name:                              # лейбл-оглавление полосы (Vision03)
                    content = b.name
                else:
                    content = '~' + b.kind + (f' x{b.count}' if b.count > 1 else '')
            flat.append((sym, b.start, b.end, content, b.description))
            if b.children:
                walk(b.children)

    walk(blocks)
    symw = max((len(r[0]) for r in flat), default=0)
    out = []
    for sym, s, e, content, desc in flat:
        rng = f"[{s}-{e}]"
        line = f"{marker}{sym.ljust(symw)}  {rng:>10}  {content}"
        if desc:                                    # обогащение от Analyzer, если было
            line += f"  -- {desc}"
        out.append(line)
    return "\n".join(out)


def main():
    import sys
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    args = sys.argv[1:]
    if not args:
        print("usage: python -m get_codeblock.reader.classify FILE [--depth N]")
        return
    path = args[0]
    depth = int(args[args.index('--depth') + 1]) if '--depth' in args else 0
    print(f"//.0 (reader): {path}  (depth={depth})")
    print(render(classify_file(path, depth=depth)))


if __name__ == '__main__':
    main()
