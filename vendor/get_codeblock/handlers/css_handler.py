"""CSS / SCSS / Sass handler for get_codeblock.

Navigation (outline / get_blocks / line_level) comes from the shared tree-sitter
block engine. A "block" is a rule set's brace body `selector { … }`; nested rules
(SCSS `&::before { … }`, `@media`/`@include` blocks) nest naturally, and the label
is the selector list.

Grammar note: we use `tree-sitter-css`. The dedicated `tree-sitter-scss` package
(1.0.0) ships an old-ABI `language()` that overflows on 64-bit Windows with
tree-sitter >= 0.26, so it is unusable here. The css grammar flags SCSS-only
syntax (`@mixin`, `@include`, `@if`, `&` interpolation) as ERROR nodes, but the
surrounding rule/selector/nesting structure still parses — which is exactly what
navigation needs. Good enough to work across a real SCSS codebase.

Requires: pip install tree-sitter tree-sitter-css
"""

from ._treesitter_blocks import LangSpec, TreeSitterBlockHandler


def _load_css_language():
    import tree_sitter_css
    from tree_sitter import Language
    return Language(tree_sitter_css.language())


def _mask_scss_top_level_vars(source: bytes) -> bytes:
    """cursor_feedback__gcb.md #5 — a top-level (brace-depth 0) SCSS variable declaration
    (`$name: value;`) isn't valid CSS, and unlike `@mixin`/`@include`/`&`-interpolation
    (which stay contained — the docstring above is right about those), tree-sitter-css's
    recovery from one at depth 0 can cascade into losing the parse of the REST OF THE
    FILE: `root.type` itself comes back 'ERROR' and everything downstream fragments into
    single-token ERROR/operator nodes, not one clean node per statement (verified on the
    reported fixture — `__dev/Requests/globals.scss`). A `$var:` NESTED inside a rule
    block is a separate, milder case (confirmed): the damage stays inside that one
    block's body, the next sibling rule parses fine — so it is intentionally left alone.

    Fix: rewrite each top-level `$name: value;` into a same-length, same-line-count real
    CSS COMMENT (`/*...*/`) before parsing — always valid, anywhere, so the surrounding
    real rules parse cleanly again. classify.py already renders a comment band's first
    line as its label, so the outline ends up showing the actual variable text instead of
    an opaque `~ERROR` — visible and honest, not swallowed.

    Best-effort, not a parser: found by a lightweight string/comment-aware brace-depth
    scan, not a real grammar, so a value containing a literal `*/` or a `$`-interpolation
    (`#{$var}`) starting its own line could still slip through uncaught — acceptable,
    the common case (bare variable declarations before any rule) is what actually
    happens in practice and is what this fixes.
    """
    text = source.decode('utf-8', 'replace')
    n = len(text)
    out = []
    i = 0
    depth = 0
    while i < n:
        ch = text[i]
        if ch == '/' and i + 1 < n and text[i + 1] == '*':
            end = text.find('*/', i + 2)
            j = (end + 2) if end != -1 else n
            out.append(text[i:j])
            i = j
            continue
        if ch in ('"', "'"):
            j = i + 1
            while j < n and text[j] != ch:
                j += 2 if text[j] == '\\' and j + 1 < n else 1
            j = min(j + 1, n)
            out.append(text[i:j])
            i = j
            continue
        if ch == '{':
            depth += 1
            out.append(ch)
            i += 1
            continue
        if ch == '}':
            depth = max(0, depth - 1)
            out.append(ch)
            i += 1
            continue
        line_start = text.rfind('\n', 0, i) + 1
        at_line_start = text[line_start:i].strip() == ''
        if depth == 0 and ch == '$' and at_line_start:
            j = i + 1
            local_depth = 0
            while j < n:
                cj = text[j]
                if cj in '([':
                    local_depth += 1
                elif cj in ')]':
                    local_depth = max(0, local_depth - 1)
                elif cj == ';' and local_depth == 0:
                    j += 1
                    break
                j += 1
            span = text[i:j]
            out.append(_as_comment(span))
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out).encode('utf-8')


def _is_masked_var(text: str) -> bool:
    """True for a comment produced by `_mask_scss_top_level_vars` — always `/*$name...`
    (real hand-written comments essentially never start with a literal `$` right after
    the opener). Used to keep a masked variable from gluing onto the NEXT rule as its
    preamble — it isn't a comment, it's a statement that happens to be disguised as one
    (cursor_feedback__gcb.md #5 residual finding, fixed 2026-09-13)."""
    return text.startswith('/*$')


def _as_comment(span: str) -> str:
    """Same LENGTH, embedded newlines kept as-is (byte offsets/line numbers stay valid) —
    the trailing `;`/last couple chars are dropped to make room for `/*`+`*/`, never the
    start (the variable name is what makes the resulting comment worth reading)."""
    if len(span) < 4:
        return ' ' * len(span)
    return '/*' + span[:len(span) - 4] + '*/'


CSS_SPEC = LangSpec(
    "CSS/SCSS",
    _load_css_language,
    body_types={'block'},
    transparent_parents=set(),
    named_def={'rule_set', 'keyframes_statement', 'at_rule',
               'media_statement', 'supports_statement'},
    container=set(),
    control=set(),
    scope_body='block',
    preprocess=_mask_scss_top_level_vars,
    is_synthetic_comment=_is_masked_var,
)


class CssHandler(TreeSitterBlockHandler):
    SPEC = CSS_SPEC
