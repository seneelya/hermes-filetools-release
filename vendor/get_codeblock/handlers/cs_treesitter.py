"""Tree-sitter backend for the C# declared surface (OPTIONAL, high-fidelity).

Same role/shape as ts_treesitter: used by make_interface_card when CONFIG__TOOLS.DECL_BACKEND selects
it, else the regex heuristic in csharp_handler.declarations() is the zero-dependency
fallback. Returns {name, kind, exported, reexport_from, signature, methods, start, end}.

A real parse removes the regex glitches (phantom `that`/`class`, `= new()` misread as a
method, `record class`, string-literal parens). Nested types get correct direct-member
attribution for free from the tree. Install:  pip install tree-sitter tree-sitter-c-sharp
"""

import re

_TYPE_KINDS = {
    "class_declaration": "class", "interface_declaration": "interface",
    "struct_declaration": "struct", "enum_declaration": "enum",
    "record_declaration": "record", "record_struct_declaration": "record",
}
_MEMBER_KINDS = {
    "method_declaration", "property_declaration", "field_declaration",
    "constructor_declaration", "event_field_declaration", "indexer_declaration",
    "delegate_declaration", "operator_declaration",
}
_CUT_BODY = {"block", "accessor_list", "arrow_expression_clause"}


def available():
    try:
        import tree_sitter  # noqa: F401
        import tree_sitter_c_sharp  # noqa: F401
        return True
    except Exception:
        return False


def _parser():
    import tree_sitter_c_sharp as cs
    from tree_sitter import Language, Parser
    lang = Language(cs.language())
    try:
        return Parser(lang)
    except TypeError:
        p = Parser()
        p.language = lang
        return p


def _text(src, node):
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _collapse(s):
    return " ".join(s.split())


def _public(src, node):
    return any(c.type == "modifier" and _text(src, c) == "public" for c in node.children)


def _member_sig(src, node):
    for c in node.children:
        if c.type in _CUT_BODY:
            return _collapse(src[node.start_byte:c.start_byte].decode("utf-8", "replace")).rstrip().rstrip("{").rstrip()
    txt = _collapse(_text(src, node)).rstrip(";")
    return re.split(r"=(?!=)", txt, 1)[0].strip()   # drop a field initializer


def _member_entries(src, node):
    """(name, signature) pairs for a member node (a field may declare several names)."""
    sig = _member_sig(src, node)
    if node.type in ("field_declaration", "event_field_declaration"):
        names = []
        for vd in node.named_children:
            if vd.type == "variable_declaration":
                for d in vd.named_children:
                    if d.type == "variable_declarator":
                        nm = d.child_by_field_name("name") or (d.named_children[0] if d.named_children else None)
                        if nm:
                            names.append(_text(src, nm))
        return [(n, sig) for n in (names or ["?"])]
    nm = node.child_by_field_name("name")
    return [(_text(src, nm) if nm else "?", sig)]


def declarations(source):
    """source: str -> C# type declarations with direct public members."""
    src = source.encode("utf-8")
    root = _parser().parse(src).root_node

    types = []

    def visit(n):
        for c in n.named_children:
            if c.type in _TYPE_KINDS:
                types.append(c)
            visit(c)

    visit(root)

    out = []
    for t in types:
        nm = t.child_by_field_name("name")
        body = t.child_by_field_name("body")
        sig = (_collapse(src[t.start_byte:body.start_byte].decode("utf-8", "replace")).rstrip().rstrip("{").rstrip()
               if body else _collapse(_text(src, t)).rstrip(";"))
        methods = []
        seen = set()
        if body:
            for m in body.named_children:
                if m.type in _MEMBER_KINDS and _public(src, m):
                    for name, msig in _member_entries(src, m):
                        if name not in seen:
                            seen.add(name)
                            methods.append({"name": name, "signature": msig})
        out.append({
            "name": _text(src, nm) if nm else "?",
            "kind": _TYPE_KINDS[t.type],
            "exported": _public(src, t),
            "reexport_from": None,
            "signature": sig,
            "methods": methods,
            "start": t.start_point[0] + 1,
            "end": t.end_point[0] + 1,
        })
    return out
