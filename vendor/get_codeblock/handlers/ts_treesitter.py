"""Tree-sitter backend for the TS/JS declared surface (OPTIONAL, high-fidelity).

Used by make_interface_card when CONFIG__TOOLS.DECL_BACKEND selects it; otherwise the zero-dependency
regex heuristic in typescript_handler.declarations() is used. Returns the SAME dict shape:
  {name, kind, exported, reexport_from, signature, start, end}

A real parse (not regex) means signatures/blocks come out right regardless of nested
braces, template literals, comments, multi-line headers or object-type literals — the
class of edge-cases the heuristic keeps hitting. Install:
    pip install tree-sitter tree-sitter-typescript
"""

_KIND = {
    "function_declaration": "function", "generator_function_declaration": "function",
    "function_signature": "function",
    "class_declaration": "class", "abstract_class_declaration": "class",
    "interface_declaration": "interface", "enum_declaration": "enum",
    "type_alias_declaration": "type",
    "internal_module": "namespace", "module": "namespace",
}


def available():
    try:
        import tree_sitter  # noqa: F401
        import tree_sitter_typescript  # noqa: F401
        return True
    except Exception:
        return False


def _parser():
    import tree_sitter_typescript as tsts
    from tree_sitter import Language, Parser
    lang = Language(tsts.language_typescript())
    try:
        return Parser(lang)
    except TypeError:                      # older API
        p = Parser()
        p.language = lang
        return p


def _text(src, node):
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _collapse(s):
    return " ".join(s.split())


def _declarator(node):
    for c in node.named_children:
        if c.type == "variable_declarator":
            return c
    return None


def _kind_of(src, decl):
    if decl.type in _KIND:
        return _KIND[decl.type]
    if decl.type == "lexical_declaration":
        return "let" if _text(src, decl).lstrip().startswith("let") else "const"
    if decl.type == "variable_declaration":
        return "var"
    return None


def _name_of(src, decl, kind):
    if kind in ("const", "let", "var"):
        d = _declarator(decl)
        n = d.child_by_field_name("name") if d else None
    else:
        n = decl.child_by_field_name("name")
    return _text(src, n) if n else ""


def _signature(src, decl, kind):
    if kind in ("const", "let", "var"):
        d = _declarator(decl)
        typ = d.child_by_field_name("type") if d else None
        if typ is not None:                       # typed binding → the type annotation IS the sig
            return _collapse(src[decl.start_byte:typ.end_byte].decode("utf-8", "replace"))
        return _collapse(_text(src, decl)).rstrip(";")   # untyped → keep the initializer/value
    body = decl.child_by_field_name("body")
    if body is not None:                          # function/class/etc — header up to the body `{`
        return _collapse(src[decl.start_byte:body.start_byte].decode("utf-8", "replace")).rstrip().rstrip("{").rstrip()
    return _collapse(_text(src, decl)).rstrip(";")  # type alias etc — the whole `type X = …`


def _export_names(src, node):
    for c in node.named_children:
        if c.type == "export_clause":
            return [_text(src, sp.child_by_field_name("name"))
                    for sp in c.named_children
                    if sp.type == "export_specifier" and sp.child_by_field_name("name")]
    return []


def declarations(source):
    """source: str -> top-level declarations (same shape as the regex handler)."""
    src = source.encode("utf-8")
    root = _parser().parse(src).root_node
    out = []
    for node in root.named_children:
        exported = node.type == "export_statement"
        decl = node
        if exported:
            d = node.child_by_field_name("declaration")
            if d is None:                          # re-export: `export {…}/* from "x"`
                s = node.child_by_field_name("source")
                if s is None:
                    continue
                source_mod = _text(src, s).strip("'\"")
                for nm in (_export_names(src, node) or ["*"]):
                    out.append({"name": nm, "kind": "reexport", "exported": True,
                                "reexport_from": source_mod,
                                "signature": _collapse(_text(src, node)).rstrip(";"),
                                "start": node.start_point[0] + 1, "end": node.end_point[0] + 1})
                continue
            decl = d
        kind = _kind_of(src, decl)
        if kind is None:
            continue
        out.append({"name": _name_of(src, decl, kind), "kind": kind, "exported": exported,
                    "reexport_from": None, "signature": _signature(src, decl, kind),
                    "start": decl.start_point[0] + 1, "end": decl.end_point[0] + 1})
    return out
