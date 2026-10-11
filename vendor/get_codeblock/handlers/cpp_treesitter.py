"""Tree-sitter backend for the C/C++ declared surface (Plan08 step 3).

Same role as ts_treesitter / cs_treesitter: make_interface_card's C++ module (`stamp_langs/cpp.py`)
asks it for the declarations of a header or an implementation. Export/attribute macros must be
stripped by the caller first (`find_code_usage.cpp_includes.strip_macros`) — `GGML_API void f();`
otherwise parses as a declaration of `GGML_API`.

Returns, via analyze(src_text): {"decls": [...], "opaque": [(first_line, last_line), ...]}
  decl = {name, kind, signature, methods, line, static, definition}
  kind: function | struct | class | union | enum | type | var | macro | namespace
  opaque: top-level ranges tree-sitter could NOT parse (ERROR nodes) — usually macro-generated
          code; the card says "read the code" there instead of guessing.
No C++ grammar at all -> available() is False; there is no regex fallback for C/C++ (its
declarations are too irregular for one to be honest). Install: pip install tree-sitter tree-sitter-cpp
"""

_TRANSPARENT = {"preproc_if", "preproc_ifdef", "preproc_else", "preproc_elif", "preproc_elifdef",
                "linkage_specification", "declaration_list"}
_BODY = {"field_declaration_list", "enumerator_list", "compound_statement", "declaration_list"}
_RECORD = {"struct_specifier": "struct", "class_specifier": "class", "union_specifier": "union",
           "enum_specifier": "enum"}
_NAME_TYPES = {"identifier", "field_identifier", "type_identifier", "qualified_identifier",
               "operator_name", "destructor_name", "namespace_identifier", "primitive_type"}


def available():
    try:
        import tree_sitter  # noqa: F401
        import tree_sitter_cpp  # noqa: F401
        return True
    except Exception:
        return False


def _parser():
    import tree_sitter_cpp
    from tree_sitter import Language, Parser
    lang = Language(tree_sitter_cpp.language())
    try:
        return Parser(lang)
    except TypeError:
        p = Parser()
        p.language = lang
        return p


class _Ctx:
    def __init__(self, src):
        self.src = src

    def text(self, node):
        return self.src[node.start_byte:node.end_byte].decode("utf-8", "replace")

    def sig(self, node, stop_at_body=False):
        """Node text with every body replaced by `{…}` (or cut before the body), collapsed."""
        parts, pos = [], node.start_byte
        for b in _bodies(node):
            parts.append(self.src[pos:b.start_byte].decode("utf-8", "replace"))
            if stop_at_body:
                pos = None
                break
            parts.append("{…}")
            pos = b.end_byte
        if pos is not None:
            parts.append(self.src[pos:node.end_byte].decode("utf-8", "replace"))
        return " ".join("".join(parts).split()).rstrip(";").strip()


def _bodies(node):
    """Top-most body nodes inside `node` (not descending into a found body), in order."""
    out, stack = [], list(reversed(node.children))
    while stack:
        n = stack.pop()
        if n.type in _BODY:
            out.append(n)
            continue
        stack.extend(reversed(n.children))
    return out


def _declarator_name(node):
    """(name, is_function) for a declarator chain."""
    n, is_fn = node, False
    while n is not None:
        if n.type in _NAME_TYPES:
            return n.text.decode("utf-8", "replace") if hasattr(n, "text") and n.text else None, is_fn
        if n.type == "function_declarator":
            inner = n.child_by_field_name("declarator")
            probe = inner
            while probe is not None and probe.type == "parenthesized_declarator":
                probe = probe.named_children[0] if probe.named_children else None
            # `void (*fp)(int)` — a pointer TO a function is a variable, not a function
            is_fn = not (probe is not None and probe.type in ("pointer_declarator", "reference_declarator"))
            n = inner
            continue
        nxt = n.child_by_field_name("declarator")
        if nxt is None:
            named = [c for c in n.named_children if c.type not in ("type_qualifier", "attribute_specifier",
                                                                   "ms_call_modifier", "ms_pointer_modifier")]
            nxt = named[0] if named else None
        n = nxt
    return None, is_fn


def _is_static(ctx, node):
    return any(c.type == "storage_class_specifier" and ctx.text(c) == "static" for c in node.children)


def _members(ctx, rec):
    """Public members of a struct/class/union (fields, methods) or enumerators of an enum."""
    body = rec.child_by_field_name("body")
    if body is None:
        return []
    out = []
    if rec.type == "enum_specifier":
        for e in body.named_children:
            if e.type == "enumerator":
                out.append({"name": ctx.text(e.child_by_field_name("name") or e),
                            "signature": " ".join(ctx.text(e).split())})
        return out
    public = rec.type != "class_specifier"
    for m in body.named_children:
        if m.type == "access_specifier":
            public = ctx.text(m).strip().rstrip(":").strip() == "public"
            continue
        if not public:
            continue
        if m.type in _MEMBER_TYPES:
            sig = ctx.sig(m, stop_at_body=(m.type == "function_definition"))
            d = m.child_by_field_name("declarator")
            name = _declarator_name(d)[0] if d is not None else None
            out.append({"name": name or sig, "signature": sig})
    return out


_MEMBER_TYPES = ("field_declaration", "declaration", "function_definition", "template_declaration")


def _all_member_names(ctx, rec):
    """Names of ALL members, private and protected included: a .cpp defining `Class::method` for
    a PRIVATE method declared in the class still implements the header (it is not new API)."""
    body = rec.child_by_field_name("body")
    if body is None or rec.type == "enum_specifier":
        return []
    out = []
    for m in body.named_children:
        if m.type in _MEMBER_TYPES:
            d = m.child_by_field_name("declarator")
            name = _declarator_name(d)[0] if d is not None else None
            if name:
                out.append(name)
    return out


def _record(ctx, rec, line, out, prefix=""):
    name_node = rec.child_by_field_name("name")
    if name_node is None:
        return None
    name = ctx.text(name_node)
    has_body = rec.child_by_field_name("body") is not None
    out.append({"name": name, "kind": _RECORD[rec.type], "line": line,
                "signature": prefix + ctx.sig(rec) if has_body else prefix + " ".join(ctx.text(rec).split()),
                "methods": _members(ctx, rec), "member_names": _all_member_names(ctx, rec),
                "static": False, "definition": has_body})
    return name


def _walk(ctx, node, out, anon_ns=False, prefix=""):
    for c in node.named_children:
        t = c.type
        line = c.start_point[0] + 1
        if t in _TRANSPARENT:
            _walk(ctx, c, out, anon_ns, prefix)
        elif t == "namespace_definition":
            nm = c.child_by_field_name("name")
            if nm is not None:
                out.append({"name": ctx.text(nm), "kind": "namespace", "line": line,
                            "signature": f"namespace {ctx.text(nm)}", "methods": [], "static": False,
                            "definition": True})
            body = c.child_by_field_name("body")
            if body is not None:
                _walk(ctx, body, out, anon_ns or nm is None, prefix)
        elif t == "template_declaration":
            params = c.child_by_field_name("parameters")
            pre = f"template {ctx.text(params)} " if params is not None else "template "
            _walk(ctx, c, out, anon_ns, pre)
        elif t in _RECORD:
            _record(ctx, c, line, out, prefix)
        elif t == "preproc_def" or t == "preproc_function_def":
            nm = c.child_by_field_name("name")
            if nm is None:
                continue
            params = c.child_by_field_name("parameters")
            value = c.child_by_field_name("value")
            head = ctx.text(nm) + (ctx.text(params) if params is not None else "")
            val = " ".join(ctx.text(value).split()) if value is not None else ""
            if len(val) > 100:
                val = val[:97] + "…"
            out.append({"name": ctx.text(nm), "kind": "macro", "line": line,
                        "signature": f"#define {head} {val}".rstrip(), "methods": [],
                        "static": False, "definition": True})
        elif t in ("type_definition", "alias_declaration"):
            typ = c.child_by_field_name("type")
            methods = _members(ctx, typ) if typ is not None and typ.type in _RECORD else []
            names = []
            if t == "alias_declaration":
                nm = c.child_by_field_name("name")
                names = [ctx.text(nm)] if nm is not None else []
            else:
                for d in c.children_by_field_name("declarator"):
                    nm, _fn = _declarator_name(d)
                    if nm:
                        names.append(nm)
            for nm in names:
                out.append({"name": nm, "kind": "type", "line": line, "signature": prefix + ctx.sig(c),
                            "methods": methods, "static": False, "definition": True})
        elif t in ("declaration", "field_declaration", "function_definition"):
            typ = c.child_by_field_name("type")
            if typ is not None and typ.type in _RECORD and typ.child_by_field_name("body") is not None:
                _record(ctx, typ, line, out, prefix)
            static = anon_ns or _is_static(ctx, c)
            is_def = t == "function_definition"
            sig = prefix + ctx.sig(c, stop_at_body=is_def)
            for d in c.children_by_field_name("declarator"):
                nm, is_fn = _declarator_name(d)
                if not nm:
                    continue
                out.append({"name": nm, "kind": "function" if is_fn else "var", "line": line,
                            "signature": sig, "methods": [], "static": static,
                            "definition": is_def or (not is_fn and "extern" not in sig.split())})


def _dedupe(decls):
    """One entry per (name, kind): a definition with a body beats a forward declaration; for
    functions the FIRST occurrence (usually the prototype) keeps its place and signature."""
    seen, out = {}, []
    for d in decls:
        key = (d["name"], "function" if d["kind"] == "function" else d["kind"])
        if key not in seen:
            seen[key] = len(out)
            out.append(d)
            continue
        old = out[seen[key]]
        if d["kind"] in _RECORD.values() and d["definition"] and not old["definition"]:
            out[seen[key]] = d
        elif d["kind"] == "macro":
            # conditional variants (`#ifdef HIP #define NAME "ROCm" #else #define NAME "CUDA"`) are
            # kept together on the first entry — the caller labels each with its condition
            old.setdefault("variants", []).append(d)
        elif d["kind"] == "function":
            old["definition"] = old["definition"] or d["definition"]
            old["static"] = old["static"] and d["static"]
    return out


def _opaque(root):
    ranges = []
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "ERROR":
            ranges.append((n.start_point[0] + 1, n.end_point[0] + 1))
            continue
        if n.type in _TRANSPARENT or n.type in ("translation_unit", "namespace_definition"):
            stack.extend(n.children)
    return sorted(ranges)


def _parse(parser, src_text):
    src = src_text.encode("utf-8", "replace")
    tree = parser.parse(src)
    decls = []
    _walk(_Ctx(src), tree.root_node, decls)
    return decls, _opaque(tree.root_node)


def _inside(line, ranges):
    return any(a <= line <= b for a, b in ranges)


def analyze(src_text, second_pass=None):
    """Full parse; where it failed (ERROR ranges) and `second_pass` is given — a function that
    rewrites the source (first_branch_only: one branch per #if, braces balance again) — parse that
    too and take its declarations for the failed ranges. What STILL fails stays opaque."""
    parser = _parser()
    decls, opaque = _parse(parser, src_text)
    if opaque and second_pass is not None:
        decls2, opaque2 = _parse(parser, second_pass(src_text))
        decls = [d for d in decls if not _inside(d["line"], opaque)]
        decls += [d for d in decls2 if _inside(d["line"], opaque)]
        decls.sort(key=lambda d: d["line"])
        opaque = [(a, b) for a, b in opaque2 if any(a <= y and x <= b for x, y in opaque)]
    return {"decls": _dedupe(decls), "opaque": opaque}


def declarations(src_text):
    return analyze(src_text)["decls"]
