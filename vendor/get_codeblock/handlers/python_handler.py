"""Indentation-based Python handler for get_codeblock."""

KEYWORDS = {
    'def', 'class', 'if', 'elif', 'else',
    'for', 'while',
    'try', 'except', 'finally',
    'with',
    'match', 'case',
}

SIBLING_GROUPS = {
    'if': {'elif', 'else'},
    'try': {'except', 'finally'},
}


def is_keyword(kw):
    return kw in KEYWORDS or kw == 'async'


def compute_in_string_mask(lines):
    """Return list of bools: mask[i] is True if line i BEGINS inside a
    triple-quoted string that opened on an earlier line.

    Used so is_block_header never mistakes prose inside a docstring (e.g. a
    wrapped line starting with the word "class" or "for") for a real header.
    A best-effort single-pass scanner: tracks the active triple-quote
    delimiter across lines, skips `#` comments and single-line quoted strings.
    """
    mask = [False] * len(lines)
    delim = None  # active triple-quote delimiter carried across lines
    for i, line in enumerate(lines):
        if delim is not None:
            mask[i] = True  # this line starts inside a multi-line string
        j, n = 0, len(line)
        while j < n:
            if delim is not None:
                if line.startswith(delim, j):
                    delim = None
                    j += 3
                else:
                    j += 1
                continue
            c = line[j]
            if c == '#':
                break  # rest of line is a comment
            if line.startswith('"""', j) or line.startswith("'''", j):
                delim = line[j:j + 3]
                j += 3
                continue
            if c == '"' or c == "'":
                j += 1  # single-line quoted string
                while j < n:
                    if line[j] == '\\':
                        j += 2
                        continue
                    if line[j] == c:
                        j += 1
                        break
                    j += 1
                continue
            j += 1
    return mask


_MASK_CACHE = {'lines': None, 'mask': None}


def _in_string_mask(lines):
    """Cached accessor for compute_in_string_mask, keyed on the lines object
    identity (one file is loaded once per tool run and passed around)."""
    if _MASK_CACHE['lines'] is lines:
        return _MASK_CACHE['mask']
    mask = compute_in_string_mask(lines)
    _MASK_CACHE['lines'] = lines
    _MASK_CACHE['mask'] = mask
    return mask


def compute_bracket_depths(lines):
    """Return list: depths[i] = number of unclosed ([{ brackets at the START of line i.

    String/comment-aware (same delimiter tracking as compute_in_string_mask). Used to reject
    comprehension/generator/ternary keywords as block headers: a compound-statement header
    (`for`/`if`/`while`/`with`/…) can NEVER begin inside an open bracket, so a `for`/`if` seen
    there is a comprehension clause (`{x for x in ...}`) or a ternary (`(a if c else b)`),
    NOT a block. Without this the heuristic read such a `for` as a loop and fabricated a body
    running far past the bracket (a real ballooning bug the sweep caught on Hermes source).
    """
    depths = [0] * len(lines)
    depth = 0
    delim = None
    for i, line in enumerate(lines):
        depths[i] = depth
        j, n = 0, len(line)
        while j < n:
            if delim is not None:
                if line.startswith(delim, j):
                    delim = None
                    j += 3
                else:
                    j += 1
                continue
            c = line[j]
            if c == '#':
                break
            if line.startswith('"""', j) or line.startswith("'''", j):
                delim = line[j:j + 3]
                j += 3
                continue
            if c == '"' or c == "'":
                j += 1
                while j < n:
                    if line[j] == '\\':
                        j += 2
                        continue
                    if line[j] == c:
                        j += 1
                        break
                    j += 1
                continue
            if c in '([{':
                depth += 1
            elif c in ')]}':
                depth = max(0, depth - 1)
            j += 1
    return depths


_DEPTH_CACHE = {'lines': None, 'depths': None}


def _bracket_depths(lines):
    """Cached accessor for compute_bracket_depths, keyed on lines object identity."""
    if _DEPTH_CACHE['lines'] is lines:
        return _DEPTH_CACHE['depths']
    depths = compute_bracket_depths(lines)
    _DEPTH_CACHE['lines'] = lines
    _DEPTH_CACHE['depths'] = depths
    return depths


def _is_continuation(lines, i):
    """Line i continues the statement above — it begins INSIDE a multi-line string or an open
    bracket, or follows a `\\`-ended line. Its indentation means nothing (a dedented JSON literal
    inside a triple-quoted string, a hanging argument, `\\` + `else (...)` of a conditional
    expression) — it can neither end a block nor start one."""
    if _in_string_mask(lines)[i] or _bracket_depths(lines)[i] > 0:
        return True
    return i > 0 and lines[i - 1].rstrip('\r\n').endswith('\\')


def _statement_start(lines, i):
    """First line of the logical statement holding line i (walks up over continuations)."""
    while i > 0 and _is_continuation(lines, i):
        i -= 1
    return i


def get_indent(line):
    """Return (indent_spaces, is_blank). Tabs count as 4."""
    stripped = line.lstrip()
    if not stripped:
        return len(line) - len(stripped), True
    indent_str = line[:len(line) - len(stripped)]
    indent = indent_str.replace('\t', '    ')
    return len(indent), False


def is_block_header(lines, idx):
    """Check if line idx starts a block (def/class/if/for/etc).

    Lines that begin inside a multi-line string (docstrings) are never
    headers, even if their first word happens to be a keyword.
    """
    if idx < 0 or idx >= len(lines):
        return False
    if _is_continuation(lines, idx):
        return False  # inside a string / open ([{ / after `\`: a clause, not a statement
    line = lines[idx]
    stripped = line.strip()
    if not stripped or stripped.startswith('#'):
        return False
    
    parts = stripped.split(None, 1)
    if not parts:
        return False
    
    first = parts[0].rstrip(':')
    if first in ('match', 'case'):
        # SOFT keywords: `match`/`case` double as ordinary identifiers. Only a header in
        # statement form — `match SUBJECT:` / `case PATTERN:` — i.e. the logical line ends
        # with ':'. Reject `match = re.search(...)`, `case = 5`, `match(x)` (assignment/
        # call/expression), which otherwise fabricated a bogus block (sweep-caught).
        code = stripped.split('#', 1)[0].rstrip()
        return code.endswith(':')
    if is_keyword(first):
        return True

    # Handle lines like "elif:", "else:", etc.
    if stripped.endswith(':') and any(stripped.startswith(kw + ':') or stripped == kw + ':' 
                                       for kw in ['elif', 'else', 'finally', 'except', 'case']):
        return True
    
    return False


def get_keyword(line):
    """Extract the primary keyword from a header line."""
    stripped = line.strip()
    parts = stripped.split(None, 1)
    if not parts:
        return None
    kw = parts[0].rstrip(':')
    
    # Handle "async def", "async with"
    if len(parts) > 1 and kw == 'async':
        second = parts[1].split()[0] if parts[1] else ''
        if second in ('def', 'with'):
            return kw + '_' + second
    
    return kw


def find_colon_line(lines, start_idx):
    """Find the line carrying the header's closing ':' (handles multi-line signatures).

    The end of the signature is where all brackets opened by the `(`/`[`/`{` in the header
    are balanced again AND a ':' appears — tracked by bracket depth. We do NOT require that
    line to sit at the header's own indent: with a hanging indent the closing
    `) -> T:` stays at the continuation column, e.g.

        def key(self, keys: str, *,
                bring_to_front: bool = False) -> ActionResult:   # <- ':' here, deep indent

    The old `ind == header_indent` guard missed that and ran on to the next dedented ':'
    lines away, ballooning the block and swallowing sibling defs (a real bug the sweep
    caught). Bracket balance alone is the correct discriminator.

    STRING/COMMENT-AWARE: brackets and ':' inside string literals or comments do NOT count.
    A naive `stripped.count('(')` breaks on headers like `if ch == ")":` — the ')' lives in
    a string, drove the depth negative, hid the real colon, and ran the search away (another
    ballooning bug the sweep caught). We scan characters, skipping strings/comments.
    """
    depth = 0
    delim = None  # active triple-quote delimiter carried across lines

    for i in range(start_idx, len(lines)):
        line = lines[i]
        j, n = 0, len(line)
        colon_at_top = False
        while j < n:
            if delim is not None:
                if line.startswith(delim, j):
                    delim = None
                    j += 3
                else:
                    j += 1
                continue
            c = line[j]
            if c == '#':
                break  # rest of line is a comment
            if line.startswith('"""', j) or line.startswith("'''", j):
                delim = line[j:j + 3]
                j += 3
                continue
            if c == '"' or c == "'":
                j += 1
                while j < n:
                    if line[j] == '\\':
                        j += 2
                        continue
                    if line[j] == c:
                        j += 1
                        break
                    j += 1
                continue
            if c in '([{':
                depth += 1
            elif c in ')]}':
                depth = max(0, depth - 1)
            elif c == ':' and depth == 0:
                colon_at_top = True  # header colon, not a slice/dict/annotation in brackets
            j += 1

        # Header colon = first line where, outside strings, brackets are balanced and a
        # top-level ':' appears (end of a possibly multi-line signature).
        if delim is None and depth == 0 and colon_at_top:
            return i

    return start_idx


def find_body_end(lines, header_idx, respect_siblings=True):
    """Find last line of block body starting at header_idx.

    Handles multi-line headers and sibling branches (try/except/finally, if/elif/else).
    Comments/docstrings at header_indent level are allowed inside the body.
    Returns inclusive index.

    respect_siblings=True: the returned range spans the WHOLE construct — a `try`
    reaches over its `except`/`finally`, an `if` over its `elif`/`else` (used when
    landing directly on the group's opening header, and for section spans).
    respect_siblings=False: BRANCH end only — stop at the first sibling. A line
    inside `except` is contained by `except`, not by the `try` above it, so
    containment/depth must use this to avoid counting a sibling as a deeper block.
    """
    colon_idx = find_colon_line(lines, header_idx)
    header_indent = get_indent(lines[header_idx])[0]

    if colon_idx + 1 >= len(lines):
        return colon_idx

    first_kw = get_keyword(lines[header_idx])
    group = None
    if respect_siblings:
        for parent, siblings in SIBLING_GROUPS.items():
            if first_kw == parent or first_kw in siblings:
                group = {parent} | siblings
                break
    
    last_line = colon_idx
    
    for i in range(colon_idx + 1, len(lines)):
        ind, blank = get_indent(lines[i])
        
        if blank:
            last_line = i
            continue

        if _is_continuation(lines, i):          # inside a string / bracket: part of the body
            last_line = i
            continue

        stripped = lines[i].strip()
        
        # Comments/docstrings at ANY indent never end the parent block — they're semantically attached to something inside it
        if stripped.startswith('#') or stripped.startswith('"""') or stripped.startswith("'''"):
            last_line = i
            continue
        
        # Lines with indent less than header → we might have left this block
        if ind < header_indent:
            # For compound blocks (try/except, if/elif), peek ahead to see if a sibling branch follows
            found_sibling = False
            if group is not None:
                peek = i + 1
                while peek < len(lines):
                    pi, pblank = get_indent(lines[peek])
                    pstripped = lines[peek].strip()
                    if pblank or pstripped.startswith('#') or pstripped.startswith('"""') or pstripped.startswith("'''"):
                        peek += 1
                        continue
                    if pi == header_indent and is_block_header(lines, peek):
                        kw = get_keyword(lines[peek])
                        if kw in group:
                            last_line = peek
                            found_sibling = True
                            break
                    break
            
            if not found_sibling:
                break
            continue
        
        # At same indent level as header
        if ind == header_indent:
            # Non-block-header code at same indent → something else started, our block ended
            if not is_block_header(lines, i):
                break
            
            # Block header at same indent: check if it's a sibling in our compound group
            kw = get_keyword(lines[i])
            
            if group is not None:
                if kw in group:
                    last_line = i
                    continue
                else:
                    break
            
            # Different block header at same indent → sibling, our block ended
            break
        
        # ind > header_indent → inside the body
        last_line = i

    # A block ends at its last CONTENT line — trailing blank/comment lines belong to
    # the enclosing scope or (by the comment-glues-below rule) to the next sibling's
    # preamble, NOT to this block. Trimming them makes indentation ranges agree with
    # the brace/tree-sitter convention (a block ends at `}` / its last statement), so
    # addressing and the .0-outline report the same [start-end] for the same block.
    while last_line > colon_idx:
        s = lines[last_line].strip()
        if _is_continuation(lines, last_line):   # a `#` / blank inside a string is content
            break
        if not s or s.startswith('#'):
            last_line -= 1
        else:
            break

    return last_line


def find_containing_blocks(lines, target_idx):
    """Find all blocks whose body contains target_idx.
    
    Returns list sorted outermost-first: [(header_idx, body_end), ...].
    """
    # a continuation line (inside a string / bracket) is indented like its statement's first line
    start = _statement_start(lines, target_idx)
    target_indent = get_indent(lines[start])[0]

    # ...and when that statement is a block header (a wrapped `def f(a,\n b):`), the line is part
    # of that block's own header, so the block holds it
    candidates = [start] if start != target_idx and is_block_header(lines, start) else []
    for i in range(target_idx - 1, -1, -1):
        if i == start and candidates:
            continue
        ind, blank = get_indent(lines[i])
        
        if blank or not lines[i].strip():
            continue
        
        if ind >= target_indent:
            continue
        
        if is_block_header(lines, i):
            candidates.append(i)
    
    containing = []
    for h in candidates:
        # BRANCH end (respect_siblings=False): a `try`/`if` must NOT be reported as
        # containing a line that actually sits in its `except`/`elif`/`else` sibling —
        # that sibling is at the same depth, not one deeper.
        end = find_body_end(lines, h, respect_siblings=False)
        if target_idx >= h and target_idx <= end:
            containing.append((h, end))
    
    # Sort by indent ascending (outermost first)
    containing.sort(key=lambda x: get_indent(lines[x[0]])[0])

    return containing


def enclosing_bracket_spans(lines, idx):
    """All multi-line bracket groups ``([{ … }])`` that contain line `idx`: opener on an
    EARLIER line, closer on/after `idx`. Returns ``[(open_row, close_row), …]`` 0-based,
    OUTERMOST→innermost (empty if none). String/comment-aware (same delimiter tracking as
    the docstring scanner), so a bracket inside a string or ``#`` comment is ignored.

    WHY THIS EXISTS: the indentation heuristic only models colon-blocks (def/class/if/…).
    A line inside a top-level list/dict/call literal (e.g. a ``REGISTRY = [ … ]`` table)
    has no colon-block container, so get_blocks used to fall through to the NEAREST def —
    a block that does not contain the line (invariant violation). This recovers the real
    enclosing constructs so the ladder spans the hit line and nests like the brace engine.
    """
    stack = []      # open rows of currently-open brackets
    delim = None    # active triple-quote delimiter carried across lines
    found = []      # (open_row, close_row) for every group that contains idx
    for i, line in enumerate(lines):
        j, n = 0, len(line)
        while j < n:
            if delim is not None:
                if line.startswith(delim, j):
                    delim = None
                    j += 3
                else:
                    j += 1
                continue
            c = line[j]
            if c == '#':
                break  # rest of line is a comment
            if line.startswith('"""', j) or line.startswith("'''", j):
                delim = line[j:j + 3]
                j += 3
                continue
            if c == '"' or c == "'":
                j += 1
                while j < n:
                    if line[j] == '\\':
                        j += 2
                        continue
                    if line[j] == c:
                        j += 1
                        break
                    j += 1
                continue
            if c in '([{':
                stack.append(i)
            elif c in ')]}':
                if stack:
                    o = stack.pop()
                    if o < idx <= i:
                        found.append((o, i))  # opened before the hit line, closes at/after
            j += 1
    found.sort(key=lambda s: s[0])  # outermost (smallest open row) first
    return found


def collect_preamble(lines, header_idx):
    # Find earliest #-comment/decorator line attached above header.
    #
    # Returns index of first preamble line, or header_idx if none.
    #
    # Only #-comments and @-decorators glue -- NOT triple-quoted strings. A Python
    # docstring is a real STATEMENT (the def/class's own first body line, __doc__), not a
    # comment; the map (reader/backends/python_ast.py) already keeps it as its own filler,
    # never gluing it into whatever follows (filler_kind only holds #-comment bands as
    # pending). Gluing docstring TEXT here as well used to steal a function's own docstring
    # into the preamble of its first inner if/for (walking upward hits the bare closing
    # triple-quote and mistakes it for a fresh one-line comment) -- a real bug the fuzzer's
    # level column caught. Matching the map's rule (comments/decorators only) fixes it and
    # keeps the two engines consistent (invariant #6).
    if header_idx == 0:
        return header_idx

    start = header_idx
    i = header_idx - 1

    while i >= 0:
        content = lines[i].strip()
        blank = not content

        if blank:
            i -= 1
            continue

        if is_block_header(lines, i):
            break

        # Decorators (`@deco`) are part of the definition (the tree-sitter map folds them
        # into the def node's span), so addressing must glue them too or the two engines
        # report different starts for the same block. NOTE: a multi-line decorator's
        # continuation lines aren't caught here, only the `@`-leading line — acceptable;
        # single-line decorators are the norm.
        if content.startswith('#') or content.startswith('@'):
            start = i
            i -= 1
        else:
            break

    return start


def find_attached_block(lines, comment_idx):
    """For a standalone comment line, find block it's attached to below.
    
    Returns header index of the compound block containing that attachment target,
    or None if no attachment found.
    """
    # Skip blank AND comment lines: a multi-line comment block above a def means the
    # first comment line must still reach the def below it (not stop at the 2nd comment).
    i = comment_idx + 1
    while i < len(lines) and (not lines[i].strip() or lines[i].strip().startswith('#')):
        i += 1

    if i >= len(lines):
        return None
    
    if is_block_header(lines, i):
        attached_kw = get_keyword(lines[i])
        
        # If this is a sibling keyword (except/elif), find the parent compound header above
        for parent, siblings in SIBLING_GROUPS.items():
            if attached_kw in siblings:
                # Search upward from comment_idx for the parent keyword at same indent
                target_indent = get_indent(lines[i])[0]
                j = comment_idx - 1
                while j >= 0:
                    ind, _ = get_indent(lines[j])
                    if ind < target_indent:
                        break
                    if is_block_header(lines, j) and get_keyword(lines[j]) == parent:
                        return j
                    j -= 1
                break
        
        return i
    
    return None


class PythonHandler:

    def __init__(self):
        pass

    # ⚠ SUSPECT-DEAD (Vision03/04) — .py outline теперь идёт через reader/classify (.0 на
    # tree-sitter-python / ast-фолбек), НЕ сюда. Единственный вызыватель — golden_capture.py.
    # ВНИМАНИЕ: get_blocks/line_level НИЖЕ — ЖИВЫЕ (Reader делегирует .py-адресацию им). Не путать.
    def outline(self, lines, max_level=None):
        """Named-definition skeleton: def/class only (control blocks excluded),
        hierarchical (class=1, its methods=2, ...). Label = the header line.

        Section end = line before the next definition at the same-or-shallower indent
        (like Markdown headings) — deliberately NOT find_body_end, which mis-ranges
        multi-line signatures. For a TOC this is accurate; precise extraction uses --query.
        """
        defs = []  # (idx, indent, label)
        for i, line in enumerate(lines):
            if is_block_header(lines, i) and get_keyword(line) in ('def', 'class', 'async_def'):
                defs.append((i, get_indent(line)[0], line.strip()))

        out = []
        stack = []  # indents of enclosing definitions
        for k, (i, indent, label) in enumerate(defs):
            while stack and stack[-1] >= indent:
                stack.pop()
            level = len(stack) + 1
            stack.append(indent)
            if max_level and level > max_level:
                continue
            end = len(lines)
            for j_idx, j_indent, _l in defs[k + 1:]:
                if j_indent <= indent:
                    end = j_idx  # 1-based line before the next same/shallower def
                    break
            out.append({'level': level, 'text': label, 'start': i + 1, 'end': end})
        return out

    def line_level(self, lines, idx):
        """Logical nesting level of ONE line (0-based idx), 1-based.

        Rule: level = 1 + number of enclosing block BODIES. A block header sits at
        its parent's level; the body is one deeper. Multi-line headers (wrapped
        signatures) count as one logical line at the header's level.
        Root / not inside any block body = 1. (0 is never a real depth — it is
        reserved for --level addressing.)
        """
        if idx < 0 or idx >= len(lines):
            return 1
        # A wrapped-signature continuation line belongs to its header → use the header.
        if not is_block_header(lines, idx):
            j = idx - 1
            while j >= 0:
                if not lines[j].strip():
                    break
                if is_block_header(lines, j):
                    if find_colon_line(lines, j) >= idx:
                        idx = j  # part of this header's multi-line signature
                    break
                j -= 1
        return len(find_containing_blocks(lines, idx)) + 1

    def get_blocks(self, file_path, target_line):
        """Get blocks containing target line.
        
        Returns list sorted outermost-first:
        [{'level': N, 'start': X, 'end': Y}, ...]
        Level = depth from root (1=top-level block).
        start/end = 1-indexed inclusive line numbers.
        """
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        if not lines or target_line < 1 or target_line > len(lines):
            return []

        idx = target_line - 1
        result = self._ladder(lines, idx)

        # INVARIANT (Vision04): every rung MUST contain the hit line. The indentation
        # heuristic doesn't model multi-line bracket constructs (list/dict/call), so a
        # stray path could otherwise report a block that doesn't span target_line (the
        # old nearest-def fallback did exactly that for a line inside a top-level list).
        # Drop any non-containing rung; if nothing remains, describe the real container.
        result = [b for b in result if b['start'] <= target_line <= b['end']]
        if not result:
            return self._orphan(file_path, lines, idx)

        # ИНВАРИАНТ #9 (расширение): даже когда есть охватывающий адресуемый рунг, внутри его
        # тела может сидеть более узкая filler-полоса (атрибут класса, обычный стейтмент —
        # `--dot` её уже показывает на уровень глубже родителя). Строго уже последнего
        # найденного рунга → честный ещё более внутренний контейнер. См. `address.get_blocks`
        # (тот же приём, brace-сторона) — единая логика через `filler_container_at`.
        from ..reader.classify import filler_container_at
        inner = result[-1]
        filler = filler_container_at(file_path, target_line)
        if (filler is not None and filler['start'] >= inner['start'] and filler['end'] <= inner['end']
                and (filler['start'], filler['end']) != (inner['start'], inner['end'])):
            result.append({**filler, 'level': inner['level'] + 1})
        return result

    def _ladder(self, lines, idx):
        """Enclosing colon-block ladder (outermost→innermost) for line idx. May be empty
        when the line sits outside every def/class/control block (handled by _orphan)."""
        # If comment attached to block below, resolve effective target
        content = lines[idx].strip()
        if content.startswith('#'):
            attached = find_attached_block(lines, idx)
            if attached is not None:
                blocks = self._build_hierarchy_for_header(lines, attached)
                # Include the comment itself in the innermost block's start
                if blocks:
                    preamble_start = collect_preamble(lines, attached)
                    # Use min of comment position and preamble
                    blocks[-1]['start'] = min(idx + 1, preamble_start + 1)
                return blocks

        # A block-header line belongs to the block it OPENS: land on `def ws_reader`
        # (or `if ...`, `for ...`) and the innermost block is that block, not its parent.
        # find_containing_blocks only returns strict ancestors, so route headers through
        # the ancestors+own-block builder (same one the comment path uses).
        if is_block_header(lines, idx):
            return self._build_hierarchy_for_header(lines, idx)

        containing = find_containing_blocks(lines, idx)

        result = []
        for i, (h, end) in enumerate(containing):
            # Glue the preamble onto EVERY rung (not just the innermost) so a block
            # reports the same range as an ancestor rung of a deep query as it does
            # when it's the block landed on — the tree-sitter engine's single
            # canonical range (_bounds), now matched here.
            result.append({
                'level': i + 1,
                'start': collect_preamble(lines, h) + 1,
                'end': end + 1,
                'label': lines[h].strip()[:80],
            })

        return result

    def _orphan(self, file_path, lines, idx):
        """Line outside every colon-block. First try the enclosing multi-line bracket
        construct (list/dict/call) so we still return a block that CONTAINS the line;
        then the filler band (imports/comments/assign-run/docstring) the map already shows
        for this line (invariant #9); failing that, report honest module scope. Never a
        non-containing 'nearest' guess."""
        spans = enclosing_bracket_spans(lines, idx)
        if spans:
            # colon-block ancestors of the outermost construct + the bracket groups
            # themselves (outer→inner), so the ladder nests like the brace engine does.
            rungs = list(find_containing_blocks(lines, spans[0][0])) + spans
            return [{
                'level': i + 1,
                'start': collect_preamble(lines, h) + 1,
                'end': e + 1,
                'label': lines[h].strip()[:80],
            } for i, (h, e) in enumerate(rungs)]
        # Genuinely top-level line (module gap / bare statement): the filler band
        # `--outline`/`--dot` already show for it (`imports: …`, `~docstring`, `assign: …`)
        # is the real container here — same concept as named/control blocks, just unnamed.
        # Uses tree-sitter-python (or the ast fallback) via classify.py — an independent
        # parse from this indentation engine, but the two are already required to agree
        # on bounds (CONTRACT invariant #6), so this doesn't introduce a new discrepancy.
        from ..reader.classify import filler_container_at
        filler = filler_container_at(file_path, idx + 1)
        if filler is not None:
            return [filler]
        # Module scope always contains it, and stays truthful — no phantom def invented.
        return [{'level': 1, 'start': 1, 'end': len(lines), 'label': '<module>'}]


    def _build_hierarchy_for_header(self, lines, header_idx):
        """Build full hierarchy of blocks containing the given header, plus the header's own block."""
        # find_containing_blocks already returns ALL strict ancestors (outermost-first).
        # The old climb (target = h - 1) stepped onto the line ABOVE each ancestor header —
        # which is usually the previous SIBLING's last line — and wrongly pulled that
        # sibling (and its predecessors) in as ancestors. On a flat module with many
        # top-level defs this ballooned the ladder (e.g. `def emit` inside `def main`
        # reported all 10 preceding top-level funcs as containers). One direct call is
        # correct and cheap.
        ancestors = list(find_containing_blocks(lines, header_idx))

        # Add the header's own block as innermost level (branch end: a `try`/`if`
        # header's block is its own branch, matching how it reads as an ancestor).
        header_end = find_body_end(lines, header_idx, respect_siblings=False)
        ancestors.append((header_idx, header_end))
        
        result = []
        for i, (h, end) in enumerate(ancestors):
            # Every rung glued (see get_blocks) — consistent ranges across modes.
            result.append({
                'level': i + 1,
                'start': collect_preamble(lines, h) + 1,
                'end': end + 1,
                'label': lines[h].strip()[:80],
            })

        return result
