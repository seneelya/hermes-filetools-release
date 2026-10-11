"""Every CLI mode as DATA first, TEXT second (Plan30 of hermes-filetools, 2026-10-07).

The CLI used to compute and print in one pass inside `core._main_impl`, so the only way to
get its picture was its stdout, and the only way to get its numbers was to parse that text
back. Here each mode is a pair:

    view  = <mode>_view(src, ...)  -> dict      what the mode found; no printing, no text
    lines = render_<mode>(view, src)            the CLI's picture of that view

A renderer yields `(kind, text)`: `kind` is "meta" (one tool-written line, already wrapped
as a comment of the target language) or "body" (file text verbatim, its own newlines
included). `as_text()` joins them exactly as the CLI prints them; the CLI itself only adds
colour and the human-only hints on a terminal. A caller that frames file text its own way
(hermes-filetools `read_file`) takes `query_view(...)["runs"]` and never touches the body
lines of `render_query`.

`block_range` is the one view with no CLI mode of its own: a raw `[line, line+count-1]`
read with both ends snapped to blocks.
"""

from pathlib import Path

from get_codeblock.core import read_lines, resolve, make_comment_delims


class UnsupportedFormat(ValueError):
    """The extension has no reader profile — an honest refusal, never "the closest language"."""


class Source:
    """One opened file: its lines, its reader, and the comment wrapper for tool-written lines.

    `display` — the path the `File:` lines show; defaults to `path`. A caller that parses a
    temporary copy (hermes-filetools reads through its own backend and decodes first) names
    the real file here, so the picture never shows the copy's path."""

    def __init__(self, path, lines, language, handler, display=None):
        self.path = path
        self.display = display or path
        self.lines = lines
        self.language = language
        self.handler = handler
        self.ext = Path(path).suffix.lower()
        self._open, self._close = make_comment_delims(language)

    def c(self, s):
        """Wrap one metadata line as a comment valid in the target language."""
        return f"{self._open}{s}{self._close}"


def open_source(path, display=None):
    """Read and route `path` once for any number of views (`display`: see `Source`).

    Raises FileNotFoundError; `env_check.EnvError` when this language's tree-sitter packages
    are missing (its text is the exact pip command); `UnsupportedFormat` for an extension no
    reader profile handles."""
    try:
        lines = read_lines(path)
    except FileNotFoundError:
        raise FileNotFoundError(f"File not found: {path}")
    ext = Path(path).suffix.lower()
    from get_codeblock.reader.reader import language_for_ext, Reader
    language = language_for_ext(ext)
    from get_codeblock.env_check import ensure_language
    ensure_language(language)
    # `language_for_ext` falls back to 'python' for anything it doesn't know, so only the
    # registry can tell an unsupported extension (`.log`, `.rs`) from a real one.
    from get_codeblock.reader.registry import resolve as _resolve_format
    try:
        _resolve_format(ext)
    except ValueError:
        raise UnsupportedFormat(f"file format '{ext or '(no extension)'}' is not supported yet "
                                "(no reader profile registered for it).")
    return Source(path, lines, language, Reader.open(path, lines, language), display)


def as_text(rendered):
    """The CLI's exact stdout for one renderer's output."""
    return "".join(t + "\n" if kind == "meta" else t for kind, t in rendered)


def _hits(n):
    """'1 hit' vs 'N hits' — single --line is a 1-element batch, not a special case,
    so the header still needs to read right for it."""
    return f"{n} hit" if n == 1 else f"{n} hits"


def _file_header(file_path, lines):
    """The one place that renders 'File: path (N lines)' — every batch mode prints
    the file path, and every one of them should say how big the file is (useful
    orientation, e.g. judging how big a --level escalation would be) the same way.
    Fix it here once, not separately in survey/outline/query."""
    return f"File: {file_path} ({len(lines)} lines)"


def _check_line(src, line):
    if line < 1 or line > len(src.lines):
        raise ValueError(f"Line {line} out of range (1-{len(src.lines)})")


# -- outline (one map) ------------------------------------------------------------------

def outline_view(src, line=None, level=0, deep=False, depth=0):
    """The map of the file, or of ONE block when `line` is given.

    Without `line`, `level > 0` caps the depth (`--level N --outline`); `deep` is the `.0`
    map (`--dot`, filler on every level) and `depth` its cap. With `line`, `level` picks the
    block whose map it is — 0 the innermost, -K K ancestors up (`--ancestor-level K`), +N the
    N-th from the top — and the depth is adaptive.

    Returns {rows, depth, per_level, base_level, shown, focus_line, mode_word, jsx_note};
    `rows` are only the shown ones ({level, start, end, text, frame, filler}). An empty map
    is `rows == []` with `depth is None`. Raises ValueError for a line out of range."""
    if line is not None:
        _check_line(src, line)
    focus_level = level or 0
    mode_word = '.0' if deep else 'outline'
    rows_all = src.handler.outline(src.lines, max_level=None, deep=deep,
                                   focus_line=line, focus_level=focus_level)
    if not rows_all:
        return {'rows': [], 'depth': None, 'per_level': {}, 'base_level': None, 'shown': None,
                'focus_line': line, 'mode_word': mode_word, 'jsx_note': None}

    per_level = {}                      # frames/filler ('.') excluded from the tally
    for r in rows_all:
        if not r.get('frame') and not r.get('filler'):
            per_level[r['level']] = per_level.get(r['level'], 0) + 1
    # Базовый уровень = РЕАЛЬНАЯ глубина корня карты: 1 для файла, глубже — в фокусе
    # (напр. метод на глубине 2). Адаптив/хедер считаем ОТ него, а не от жёсткого 1.
    # per_level пуст, когда фокус-цель сама — filler (инвариант #9: строка без landmark
    # рядом, например топ-левел assign/comment) — единственная строка НЕ landmark, тэлли
    # её не считает. Тогда база — реальный level этой самой строки, а не «1» (иначе
    # `rows` ниже отфильтровывает её же и остаётся пустым — падение на max() пустой
    # последовательности).
    base_level = min(per_level) if per_level else rows_all[0]['level']
    max_depth = max((r['level'] for r in rows_all if not r.get('filler')), default=base_level)
    nb = per_level.get(base_level, 0)          # «вершины» на базовом уровне
    nb1 = per_level.get(base_level + 1, 0)     # следующий уровень
    total_lines = len(src.lines)

    # Явный потолок: --level N (outline) или --depth N (dot). Иначе адаптив.
    # В фокус-режиме --level = предок цели (не потолок глубины) → адаптив.
    explicit = None if line else (
        (level if level and level > 0 else None)
        or (depth if deep and depth and depth > 0 else None))
    if explicit:
        shown = explicit
    else:
        # Overview depth from the map's own size. Expand ONE level past base only when
        # the map stays a small fraction of the file (<=PCT) AND fits a row budget
        # (<=MAX_ROWS). With very few tops (<=TINY_TOPS) the tops say almost nothing —
        # expand anyway (the members ARE the map). Relative to base_level, not hard 1/2.
        OUTLINE_PCT, OUTLINE_MAX_ROWS, OUTLINE_TINY_TOPS = 0.15, 40, 2
        fits = (nb + nb1) <= OUTLINE_PCT * total_lines and (nb + nb1) <= OUTLINE_MAX_ROWS
        shown = (base_level + 1) if (nb1 > 0 and (fits or nb <= OUTLINE_TINY_TOPS)) else base_level

    from get_codeblock.jsx_note import flat_block_note
    jsx_start, jsx_end = (rows_all[0]['start'], rows_all[0]['end']) if line else (1, total_lines)
    jsx_note = flat_block_note(src.language, src.ext, src.lines, jsx_start, jsx_end,
                               depth=max_depth, base_level=base_level)
    return {'rows': [r for r in rows_all if r['level'] <= shown], 'depth': max_depth,
            'per_level': per_level, 'base_level': base_level, 'shown': shown,
            'focus_line': line, 'mode_word': mode_word, 'jsx_note': jsx_note}


def render_outline(view, src):
    c = src.c
    if not view['rows']:
        yield 'meta', c("(no block found at that line)" if view['focus_line'] else "(no structure found)")
        return
    # Header: total depth + per-level tally + what is shown. This is METADATA
    # (the overview signal) — emitted for every caller, including the API.
    per_level = view['per_level']
    tally = " ".join(f"L{lvl}={per_level[lvl]}" for lvl in sorted(per_level))
    focus_tag = f"focus line {view['focus_line']} " if view['focus_line'] else ""
    yield 'meta', c(f"{view['mode_word']} — {focus_tag}max depth {view['depth']}"
                    + (f", {tally}" if tally else "")
                    + f", showing levels {view['base_level']}..{min(view['shown'], view['depth'])}")
    if view['jsx_note']:
        yield 'meta', c(view['jsx_note'])

    # Pad each "<indent><marker>" so ranges line up. Named block = bare level number;
    # unnamed (frame/filler) = '.'+level ('.3' = «уровень 3, без имени»), чтобы глубина
    # была видна, но было ясно: имени тут нет, в оглавление не тащим. На уровне 1 номер
    # не пишем (он очевиден по нулевому отступу) — голая '.', чтобы оглавление не шумело.
    def _mark(r):
        if not (r.get('frame') or r.get('filler')):
            return str(r['level'])
        return '.' + (str(r['level']) if r['level'] > 1 else '')
    rows = view['rows']
    labels = ["  " * (r['level'] - 1) + _mark(r) for r in rows]
    width = max(len(s) for s in labels)
    for r, label in zip(rows, labels):
        yield 'meta', c(f"{label.ljust(width)} [{r['start']}-{r['end']}] {r['text']}")


# -- boxed rows: ladder and outline batch -------------------------------------------------

def _outline_label_index(handler, lines, deep=False):
    """Full-file outline computed ONCE, indexed by (start, end). Ladder/survey rows
    read their label from here instead of the raw truncated header text — one source
    of truth for block labels, shared with outline batch (CONTRACT.md: 'label lines
    are taken exactly as outline gives them'). Missing entries (no outline support
    for this language) fall back to the raw ladder label at the call site."""
    if not hasattr(handler, 'outline'):
        return {}
    rows = handler.outline(lines, max_level=None, deep=deep) or []
    return {(r['start'], r['end']): r for r in rows}


def _render_boxed_rows(rows, c):
    """CONTRACT.md boxed ladder/tree format — the ONE renderer shared by survey and
    outline batch, so both look identical (columns AND indentation computed once
    across ALL rows passed in, never per-hit/per-group). Each row is one of:
      {'kind': 'hit',   'line': N, 'text': <source line>,   'indent': K}
      {'kind': 'error', 'line': N, 'msg': <error text>,     'indent': K}
      {'kind': 'block', 'level': L, 'start': S, 'end': E, 'text': <label>, 'indent': K,
       'frame': bool, 'filler': bool}
    `indent` = nesting depth WITHIN this printout (0 = outermost shown), independent
    of the block's real file-depth — real depth can start anywhere (a hit ten levels
    deep still reads as a 3-row staircase, not ten). Block rows encode it twice: the
    level marker is staircased across the mark column (deeper sits closer to `|`,
    like nested braces) AND the label gets a synthetic `  `-per-level indent, so the
    block reads almost like the source's own nesting. Hit/error rows use a single
    arrow glyph instead of a level number — it means "here's the grep hit", not a
    depth, so it always sits flush against `|` regardless of that row's own indent
    (only its LABEL gets the indent) — a depth-shaped position on a non-depth marker
    would be a lie. The RANGE column gets the same arrow again (`→ N`, not `A-B`) —
    a single number preceded by the same glyph reads as "one exact line", never
    confusable with a start-end span; no index either (which --line position this
    came from is not information anyone needs once it's resolved — the line number
    already is), and no `>` (looks like a false numeric comparison, "1 > 41").
    """
    ARROW = '→'  # →

    def _mark(r):
        if r['kind'] in ('hit', 'error'):
            return ARROW
        if not (r.get('frame') or r.get('filler')):
            return str(r['level'])
        return '.' + (str(r['level']) if r['level'] > 1 else '')

    def _cell(r):
        if r['kind'] in ('hit', 'error'):
            return f"{ARROW} {r['line']}"
        return f"{r['start']}-{r['end']}"

    if not rows:
        return
    marks = [_mark(r) for r in rows]
    cells = [_cell(r) for r in rows]
    cw = max(len(x) for x in cells)
    max_indent = max(r.get('indent', 0) for r in rows)
    mark_w = max_indent + 2  # staircase field: leading (indent+1) spaces + 1-char marker
    for r, m, cell in zip(rows, marks, cells):
        text = f"ERROR: {r['msg']}" if r['kind'] == 'error' else r['text']
        indent = r.get('indent', 0)
        mark_field = m.rjust(mark_w) if r['kind'] in ('hit', 'error') \
            else (' ' * (indent + 1) + m).ljust(mark_w)
        yield 'meta', c(f"{mark_field}| {cell.rjust(cw)}| {'  ' * indent}{text}")


def ladder_view(src, line_nums):
    """Survey (`--line N[,N…]` alone): ONE merged map of the file, like outline batch —
    every hit's ladder gets folded into it by (start, end), so hits sharing an
    ancestor (even a distant one, even non-adjacent in the --line list) show that
    ancestor exactly ONCE, not once per hit. Each hit's own exact source line (the
    grep-hit proof, never enriched/reformatted) is inserted right after the block it
    actually landed in, nested one level deeper than it.

    Returns {n, rows}: rows in the boxed-row shape of `_render_boxed_rows`."""
    handler, lines, file_path = src.handler, src.lines, src.path
    outline_index = _outline_label_index(handler, lines)
    merged = {}          # (start, end) -> block row
    order = []
    hits_by_block = {}   # (start, end) of a hit's OWN innermost block -> [hit info]
    error_entries = []   # hits that never resolved to any block

    for ln in line_nums:
        if ln < 1 or ln > len(lines):
            error_entries.append((ln, f"Line out of range (1-{len(lines)})"))
            continue

        blocks = handler.get_blocks(file_path, ln)  # outermost -> innermost
        if not blocks:
            error_entries.append((ln, "No blocks found"))
            continue

        for b in blocks:
            key = (b['start'], b['end'])
            if key not in merged:
                o = outline_index.get(key)
                merged[key] = {'level': b['level'], 'start': b['start'], 'end': b['end'],
                                'text': o['text'] if o else (b.get('label') or ''),
                                'frame': bool(o and o.get('frame')), 'filler': bool(o and o.get('filler'))}
                order.append(key)

        innermost = blocks[-1]
        # Same indent as the innermost rung (not +1) whenever the hit line ISN'T
        # actually inside that rung's body — covers two different real cases:
        # a single-line pinpoint filler (`~return_statement`, start==end==this line)
        # AND a hit landing on the block's own header/preamble (e.g. `async def
        # foo():` itself, or an attached comment glued above it) rather than inside
        # it — both have line_level(ln) == the rung's own level, never deeper; a
        # true body line gets a strictly higher line_level (there'd be one more rung
        # in `blocks` for it otherwise). Only actually-nested content gets +1.
        same_level = handler.line_level(lines, ln - 1) <= innermost['level']
        ikey = (innermost['start'], innermost['end'])
        hits_by_block.setdefault(ikey, []).append(
            {'line': ln, 'same_level': same_level, 'text': lines[ln - 1].strip()})

    all_rows = [{'kind': 'error', 'line': ln, 'indent': 0, 'msg': msg}
                for ln, msg in error_entries]

    block_list = sorted((merged[k] for k in order), key=lambda r: (r['start'], -r['end']))
    base_level = min((r['level'] for r in block_list), default=0)
    for r in block_list:
        indent = r['level'] - base_level
        all_rows.append({'kind': 'block', 'level': r['level'], 'start': r['start'], 'end': r['end'],
                          'text': r['text'], 'indent': indent,
                          'frame': r['frame'], 'filler': r['filler']})
        for h in hits_by_block.get((r['start'], r['end']), []):
            all_rows.append({'kind': 'hit', 'line': h['line'],
                              'indent': indent if h['same_level'] else indent + 1,
                              'text': h['text']})
    return {'n': len(line_nums), 'rows': all_rows}


def render_ladder(view, src):
    yield 'meta', src.c(f"{_file_header(src.display, src.lines)} · {_hits(view['n'])}")
    yield from _render_boxed_rows(view['rows'], src.c)


def outline_batch_view(src, line_nums, levels, deep=False):
    """Batch outline (CONTRACT.md): ONE merged, deduped tree across all hits, each
    escalated to its own broadcast --level/--ancestor-level. Same boxed rows as the
    ladder — same columns, same staircase indent, same arrow glyph for error rows.

    Returns {n, errors, rows, mode_word}."""
    lines = src.lines
    errors = []
    merged = {}   # (start, end) -> row (first hit to surface it wins)
    order = []

    for ln, lvl in zip(line_nums, levels):
        if ln < 1 or ln > len(lines):
            errors.append((ln, f"Line out of range (1-{len(lines)})"))
            continue
        rows = src.handler.outline(lines, max_level=None, deep=deep, focus_line=ln, focus_level=lvl)
        if not rows:
            errors.append((ln, "no block found at that line"))
            continue
        for r in rows:
            key = (r['start'], r['end'])
            if key not in merged:
                merged[key] = r
                order.append(key)

    error_rows = [{'kind': 'error', 'line': ln, 'indent': 0, 'msg': err}
                  for ln, err in errors]
    block_list = sorted((merged[k] for k in order), key=lambda r: (r['start'], -r['end']))
    base_level = min((r['level'] for r in block_list), default=0)
    block_rows = [{'kind': 'block', 'level': r['level'], 'start': r['start'], 'end': r['end'],
                   'text': r['text'], 'indent': r['level'] - base_level,
                   'frame': r.get('frame'), 'filler': r.get('filler')}
                  for r in block_list]
    return {'n': len(line_nums), 'errors': errors, 'rows': error_rows + block_rows,
            'mode_word': '.0' if deep else 'outline'}


def render_outline_batch(view, src):
    yield 'meta', src.c(f"{_file_header(src.display, src.lines)} · {view['mode_word']} batch · {_hits(view['n'])}"
                        + (f", {len(view['errors'])} error(s)" if view['errors'] else ""))
    yield from _render_boxed_rows(view['rows'], src.c)


# -- query ------------------------------------------------------------------------------

def _resolve_query_runs(handler, file_path, lines, line_nums, levels):
    """Pure resolve step of batch query (no printing): every hit -> block, sorted by
    file position, merged on touch/overlap into `runs` (see `render_query` for
    why merge-on-touch, not just containment). Split out from the old single
    `_run_query_batch` (Vision05) so escalation can inspect the resolved SIZE before
    committing to render — the common (non-escalating) call now resolves exactly
    once, same cost as before Vision05 existed; only an actually-undersized result
    pays for a second resolve (with the escalated `--line`/`--level` arrays)."""
    errors = []
    resolved = {}  # (start, end) -> block; exact duplicates dedupe for free here

    for ln, lvl in zip(line_nums, levels):
        if ln < 1 or ln > len(lines):
            errors.append(f"line {ln} out of range (1-{len(lines)})")
            continue
        blocks = handler.get_blocks(file_path, ln)
        if not blocks:
            errors.append(f"no blocks found at line {ln}")
            continue
        block = resolve(blocks, lvl)
        if not block:
            errors.append(f"level out of range at line {ln}")
            continue
        resolved[(block['start'], block['end'])] = block

    # Each run keeps the full list of ORIGINAL resolved blocks it absorbed — a merged
    # run never claims a single 'Block level' for its whole span (that would be a lie
    # once it's spliced from more than one real block at different depths); instead
    # its header lists every real constituent, each stating its own true level/range.
    by_position = sorted(resolved.values(), key=lambda b: (b['start'], -b['end']))
    runs = []
    for b in by_position:
        if runs and b['start'] <= runs[-1]['end'] + 1:
            runs[-1]['end'] = max(runs[-1]['end'], b['end'])
            runs[-1]['parts'].append(b)
        else:
            runs.append({'start': b['start'], 'end': b['end'], 'parts': [b]})

    return runs, errors


def query_view(src, line_nums, levels, force=False):
    """`--line N[,N…] --query`: which file ranges to return.

    Returns {runs, errors, note, jsx_note}. `runs` — [{start, end, parts: [block…]}],
    sorted, merged on touch/overlap (a real gap keeps two runs apart); `errors` — one
    text per line that resolved to nothing; `note` — the escalation note when a too-small
    result was grown (Vision05), None otherwise or with `force`."""
    handler, lines, file_path = src.handler, src.lines, src.path
    # Resolve ONCE with the original params first (Vision05) — same cost as before
    # escalation existed. Only inspect+retry (escalate.maybe_escalate) if THAT result
    # turns out undersized; the common/already-informative case never pays for a
    # second resolve. See escalate.py's module docstring for the known, deferred
    # re-parse-per-call cost this reordering does NOT fix (separate later pass).
    runs, errors = _resolve_query_runs(handler, file_path, lines, line_nums, levels)
    note = None
    if not force:
        from get_codeblock.escalate import maybe_escalate
        new_lines, new_levels, note = maybe_escalate(
            handler, resolve, file_path, lines, line_nums, levels, runs)
        if note:
            runs, errors = _resolve_query_runs(handler, file_path, lines, new_lines, new_levels)
    jsx_note = None
    if runs:
        from get_codeblock.jsx_note import flat_block_note
        jsx_note = flat_block_note(src.language, src.ext, lines,
                                   min(r['start'] for r in runs),
                                   max(r['end'] for r in runs),
                                   handler=handler)
    return {'runs': runs, 'errors': errors, 'note': note, 'jsx_note': jsx_note}


def render_query(view, src, numbered=False):
    """The query picture. MERGE any two
    resolved ranges that touch or overlap — zero-gap adjacency included, not just
    strict containment. We're returning FILE TEXT: if range A ends at line 46 and
    range B starts at line 47, there is no real gap in the source between them, so
    printing them as two separately-framed blocks would insert a fake seam right
    where the file has none — and if this output is ever pasted back into code,
    an actively wrong one. Only a REAL gap (an uncovered line in between) keeps
    two ranges as separate printed blocks. Containment (one fully inside another)
    is the same merge with nothing new to extend — level escalation routinely
    sends several hits into the same ancestor, or into an ancestor that already
    engulfs an earlier hit's smaller block; printing that body again on top of
    itself used to duplicate real file content, not cosmetic on a real file
    (5 hits escalating into one 6571-line function would be 30000+ lines from
    one call).

    `■BLOCK : A-B` / `■END : B` are the framing numbers — ALWAYS true, exactly the
    slice printed below, NEVER a claim about depth (a merged run's true constituent
    levels can differ across its span; one number for the whole thing would lie).
    `■` marks a line as tool-written framing, never file content — same reasoning
    as CONTRACT.md's comment-wrapping of TTY hints, just a stronger, single-glyph
    version of it. When a BLOCK absorbed more than one original resolved range, the
    same line carries a ` = ranges : ...` tail listing every real constituent
    (`Level L  A-B`, comma-separated) — auxiliary, always exactly one line so it's
    trivially deletable, never its own multi-line block. No hit numbers, no [i/n]
    counter, no repeated block label: this isn't survey (no grep-hit to prove), a
    counter is redundant with just counting BLOCK lines, and a label would only
    restate the body's own first line one row down."""
    c, lines = src.c, src.lines
    # Unconditional metadata, NOT TTY-gated like the CLI's legend/outline hints:
    # this changes what range is actually returned, so a piped/programmatic caller
    # needs it exactly as much as a human does (Vision05 — checked empirically that
    # this session's own subprocess stdout is not a tty, so TTY-only would hide it
    # from exactly the audience it matters most to).
    if view['note']:
        yield 'meta', c(view['note'])
    if view['jsx_note']:
        yield 'meta', c(view['jsx_note'])
    yield 'meta', c(_file_header(src.display, lines))
    for msg in view['errors']:
        yield 'meta', c(f"ERROR: {msg}")

    for run in view['runs']:
        start, end = run['start'], run['end']
        tail = ""
        if len(run['parts']) > 1:
            ranges = ",  ".join(f"Level {p['level']}  {p['start']}-{p['end']}" for p in run['parts'])
            tail = f"  = ranges :  {ranges}"
        yield 'meta', c(f"■BLOCK : {start}-{end}{tail}")
        last = min(end, len(lines))
        numw = len(str(last)) if numbered else 0
        for j in range(start - 1, last):
            yield 'body', (f"{j + 1:>{numw}} | " if numw else "") + lines[j]
        if last >= 1 and not lines[last - 1].endswith("\n"):
            yield 'body', "\n"
        yield 'meta', c(f"■END : {end}")


# -- block_range ------------------------------------------------------------------------

def block_range(src, line, count):
    """`[line, line+count-1]` with BOTH ENDS snapped to blocks (Plan30, owner's rule).

    Start: the OUTERMOST block containing `line` whose head is at most `tol` lines back
    (a comment glued above a block is part of it). End: the OUTERMOST block containing
    the last line whose end is at most `tol` lines ahead; none — the end stays where
    `count` put it and `cut` names the innermost block it falls inside. `tol = count // 2`,
    so `count` means "about this many" (`count 1` is exactly one line).

    Two ladder lookups, no growth loop: a line is never read twice by a reader paging
    forward, because the next page starts after this `to`.

    Returns {from, to, cut}: 1-based inclusive; `cut` is [start, end] or None.
    Raises ValueError for a line out of range or count < 1."""
    n = len(src.lines)
    _check_line(src, line)
    if count < 1:
        raise ValueError(f"count must be >= 1, got {count}")
    a, b = line, min(n, line + count - 1)
    tol = count // 2

    heads = [r['start'] for r in src.handler.get_blocks(src.path, a) if a - r['start'] <= tol]
    start = min(heads, default=a)

    rungs = src.handler.get_blocks(src.path, b)
    ends = [r['end'] for r in rungs if r['end'] - b <= tol]
    if ends:
        return {'from': start, 'to': max(max(ends), b), 'cut': None}
    inner = rungs[-1] if rungs else None
    return {'from': start, 'to': b, 'cut': [inner['start'], inner['end']] if inner else None}
