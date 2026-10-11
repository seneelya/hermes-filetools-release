"""C# language handler for get_codeblock.

Navigation (outline/get_blocks/line_level) now runs on the shared tree-sitter
engine (`_treesitter_blocks` + CS_SPEC below) -- a real parse, so multi-line
signatures, file-scoped namespaces and `record` types are handled correctly.

The brace-heuristic functions in this module are LEGACY. `declarations()` (the
zero-dependency regex fallback for interface cards, superseded by cs_treesitter
when its grammar is installed) still uses `find_body_end` / `_csharp_member` /
`_TYPE_RE`. The OLD brace-based navigation (get_blocks/line_level and helpers)
is preserved, unused, at the bottom of this file as `_LegacyBraceNav` for
reference -- it is NOT registered anywhere.
"""

import re

from ._treesitter_blocks import LangSpec, TreeSitterBlockHandler


def _load_csharp_language():
    import tree_sitter_c_sharp
    from tree_sitter import Language
    return Language(tree_sitter_c_sharp.language())


CS_SPEC = LangSpec(
    "C#",
    _load_csharp_language,
    body_types={
        'block', 'declaration_list', 'enum_member_declaration_list', 'accessor_list',
    },
    # namespaces are TRANSPARENT (consistent with C++): shown in --outline but
    # they do NOT add a nesting level, so a top-level type sits at level 1
    # whether the file uses a braced or a file-scoped namespace.
    transparent_parents={
        'namespace_declaration', 'file_scoped_namespace_declaration',
    },
    named_def={
        'class_declaration', 'struct_declaration', 'interface_declaration',
        'enum_declaration', 'record_declaration', 'record_struct_declaration',
        'method_declaration', 'constructor_declaration', 'destructor_declaration',
        'operator_declaration', 'conversion_operator_declaration',
        'indexer_declaration', 'local_function_statement',
    },
    container={'namespace_declaration', 'file_scoped_namespace_declaration'},
    control={
        'if_statement', 'else_clause',
        'for_statement', 'for_each_statement', 'foreach_statement',
        'while_statement', 'do_statement',
        'switch_statement', 'try_statement', 'catch_clause', 'finally_clause',
        'using_statement', 'lock_statement', 'fixed_statement', 'unsafe_statement',
        'lambda_expression', 'anonymous_method_expression', 'property_declaration',
    },
    scope_body='block',
    cut_extra={'constructor_initializer', 'base_list'},
)

_MODS = r"(?:public|internal|protected|private|abstract|sealed|static|partial|readonly|virtual|override|async|new|extern|unsafe|const)"

# A type declaration at line start (after attributes/modifiers); `record class`/`record struct` handled.
_TYPE_RE = re.compile(
    r"^[ \t]*(?:\[[^\]]*\][ \t]*)*"
    r"(?P<mods>(?:" + _MODS + r"[ \t]+)*)"
    r"(?P<kind>class|struct|interface|enum|record)(?:[ \t]+(?:class|struct))?[ \t]+(?P<name>@?\w+)"
)


def _split_top_assign(head):
    """Return `head` truncated at the first top-level assignment `=` (paren/bracket depth 0,
    not `==`/`=>`/`>=`/`<=`/`!=`) — drops an initializer like `= new()` / `= "(x)"` so it
    isn't mistaken for a method's parameter list."""
    depth = 0
    for idx, ch in enumerate(head):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "=" and depth == 0:
            nxt = head[idx + 1] if idx + 1 < len(head) else ""
            prv = head[idx - 1] if idx else ""
            if nxt not in (">", "=") and prv not in ("=", "<", ">", "!"):
                return head[:idx].strip()
    return head


def _csharp_member(line):
    """A public type member on this line -> (name, signature, kind) or None.

    Needs modifiers incl. `public`. The initializer is dropped first (so `= new()` /
    `= "(x)"` don't read as a param list); then a `(` on the declaration side means a
    method (name = token before `(`), else a property/field (name = last identifier).
    Nested type declarations are handled separately, not here.
    """
    s = line.strip()
    m = re.match(r"^(?:\[[^\]]*\][ \t]*)*(?P<mods>(?:" + _MODS + r"[ \t]+)+)", s)
    if not m or "public" not in m.group("mods"):
        return None
    if re.match(r"^(?:" + _MODS + r"[ \t]+)*(?:class|struct|interface|enum|record)\b", s):
        return None  # nested type — declarations() records it on its own
    head = re.split(r"\{|=>|;", s, 1)[0].strip()
    lhs = _split_top_assign(head).strip()
    if "(" in lhs:
        name = lhs.split("(")[0].strip().split()[-1]
        return name, head, "method"
    toks = re.findall(r"\w+", lhs)
    if not toks:
        return None
    return toks[-1], lhs, "member"


def is_block_header(line):
    """Check if line starts a named block (class/method/namespace/etc)."""
    stripped = line.strip()

    # Skip pure comments or empty lines
    if not stripped or stripped.startswith('//') or stripped.startswith('/*'):
        return False

    # Block keywords that introduce braces: namespace, class, struct, interface, enum, method-like
    block_starts = {
        'namespace', 'class', 'struct', 'interface', 'enum',
        'delegate', 'record',
    }

    parts = stripped.split(None, 1)
    if not parts:
        return False

    # Handle access modifiers / keywords before the actual keyword
    first = parts[0]
    second_word = None
    if len(parts) > 1:
        second_word = parts[1].split()[0] if parts[1] else None

    # Direct block start (no modifier prefix)
    if first in block_starts or first.endswith('{'):
        return True

    # Modifier + keyword pattern: public class, static void Method(...), etc.
    modifiers_and_types = {
        'public', 'private', 'protected', 'internal', 'static', 'sealed',
        'abstract', 'partial', 'virtual', 'override', 'async', 'readonly',
        'new', 'extern', 'unsafe', 'fixed', 'volatile',
    }

    if first in modifiers_and_types and second_word:
        # Second word could be a block keyword, type+method name, etc.
        if second_word in block_starts:
            return True
        # Type + method/property pattern: "void Method(" or "int Property {" 
        rest = stripped.split(None, 2)
        if len(rest) >= 3 and '(' in rest[2]:
            return True

    # Lambda / anonymous methods: () => {
    if '=>' in stripped and '{' in stripped:
        return True

    # Control flow blocks: if/for/while/switch/catch/finally/using/lock with braces on same line or next
    control_keywords = {'if', 'else', 'for', 'foreach', 'while', 'do', 'switch', 'case', 'catch', 'finally', 'using', 'lock'}
    if first in control_keywords:
        return True

    # Method-like pattern without explicit modifier but with parentheses and braces nearby
    if '(' in stripped and any(c in stripped for c in ['{']):
        return True

    return False


def get_block_keyword(line):
    """Extract the block keyword from a header line."""
    stripped = line.strip()
    parts = stripped.split(None, 1)
    if not parts:
        return None

    # Skip modifiers to find the real keyword
    skip_modifiers = {
        'public', 'private', 'protected', 'internal', 'static', 'sealed',
        'abstract', 'partial', 'virtual', 'override', 'async', 'readonly',
        'new', 'extern', 'unsafe', 'fixed', 'volatile',
    }

    first = parts[0]
    if first in skip_modifiers and len(parts) > 1:
        second_part = parts[1].split(None, 1)[0]
        return second_part.lower()
    
    # Check control keywords directly
    control_keywords = {'if', 'else', 'for', 'foreach', 'while', 'do', 'switch', 'case', 'catch', 'finally', 'using', 'lock'}
    if first.lower() in control_keywords:
        return first.lower()

    # Direct block keywords
    block_keywords = {'namespace', 'class', 'struct', 'interface', 'enum', 'delegate', 'record'}
    if first.lower() in block_keywords:
        return first.lower()

    return None


def get_indent(line):
    """Get indent level (spaces count) and whether line is blank."""
    stripped = line.lstrip(' \t')
    if not stripped or stripped.startswith('//') or stripped.startswith('/*'):
        return len(line) - len(stripped), True  # treat comments as "blank-like" for structure purposes
    return len(line) - len(stripped), False


def find_body_end(lines, header_idx):
    """Find last line of block body starting at header_idx.
    
    Handles:
    - multi-line headers (method signatures spanning lines)
    - sibling branches (if/else, try/catch/finally)
    
    Returns inclusive end line index.
    """
    if not lines or header_idx < 0 or header_idx >= len(lines):
        return header_idx

    # Find the opening brace position
    open_brace_line = None
    depth = 0
    
    for i in range(header_idx, min(header_idx + 15, len(lines))):
        stripped = lines[i].strip()
        
        # Skip string literals and comments
        if not stripped or stripped.startswith('//'):
            continue
        
        # Count braces on this line (simplified: ignore strings/comments inside signature)
        brace_count = stripped.count('{') - stripped.count('}')
        depth += brace_count
        
        if brace_count > 0 or (stripped.endswith('{')):
            open_brace_line = i
            break
    
    if open_brace_line is None:
        return header_idx

    header_indent = get_indent(lines[header_idx])[0]
    keyword = get_block_keyword(lines[header_idx])
    
    # Determine compound group for sibling merging (try/catch/finally, if/else)
    group = None
    if keyword in ('if', 'else'):
        group = {'if', 'else'}
    elif keyword in ('try', 'catch', 'finally'):
        group = {'try', 'catch', 'finally'}

    last_line = open_brace_line
    
    for i in range(open_brace_line + 1, len(lines)):
        stripped = lines[i].strip()
        
        if not stripped or stripped.startswith('//') or stripped.startswith('/*'):
            # Comments don't end the block — they belong to it or a sibling
            last_line = i
            continue
        
        ind = get_indent(lines[i])[0]
        
        # Count braces on this line
        brace_count = stripped.count('{') - stripped.count('}')
        
        if brace_count < 0:
            # Closing brace — check if it closes our block (depth becomes 0)
            depth += brace_count
            if depth == 0:
                return i
            elif depth < 0:
                # Block ended before we found matching close — stop here
                return last_line
        
        if brace_count > 0 and ind > header_indent:
            depth += brace_count
            last_line = i
            continue
        
        # Lines with indent less than header → might have left this block
        if ind < header_indent:
            # For compound blocks (if/else, try/catch), peek ahead past comments/blanks
            # to see if a sibling branch follows at our indent level
            found_sibling = False
            if group is not None:
                peek = i + 1
                while peek < len(lines):
                    peek_stripped = lines[peek].strip()
                    if not peek_stripped or peek_stripped.startswith('//'):
                        last_line = peek
                        peek += 1
                        continue
                    
                    pi = get_indent(lines[peek])[0]
                    
                    if pi == header_indent and is_block_header(lines[peek]):
                        kw = get_block_keyword(lines[peek])
                        if kw in group:
                            # Sibling branch follows — include this region and continue scanning
                            last_line = peek
                            found_sibling = True
                            break
                    # Next real content doesn't match our compound → we're done
                    break
            
            if not found_sibling:
                break
            continue
        
        # At same indent level: only stop on actual block headers, not comments/docstrings
        if ind == header_indent:
            if is_block_header(lines[i]):
                kw = get_block_keyword(lines[i])
                if group is not None and kw in group:
                    # Sibling of our compound block — include it
                    last_line = i
                    continue
                else:
                    # Different block at same level — end here
                    break
            last_line = i
            continue
        
        # Indent > header_indent: body line
        last_line = i
    
    return last_line


def find_containing_braces(lines, target_idx):
    """Find all brace blocks containing the target line.
    
    Returns list sorted outermost-first (by increasing depth).
    Each item: {'header_idx': int, 'body_end': int}
    """
    stack = []  # List of indices where '{' was seen
    
    for i in range(target_idx + 1):
        line = lines[i]
        
        # Skip pure comment lines (but still track braces inside them)
        stripped = line.strip()
        if stripped.startswith('//'):
            continue
        
        # Track braces character by character, ignoring strings/comments
        j = 0
        in_string = False
        string_char = None
        n = len(line)
        
        while j < n:
            ch = line[j]
            
            # Handle escape sequences in strings
            if in_string and ch == '\\\\' and j + 1 < n:
                j += 2
                continue
            
            # Handle character literals (@"" for verbatim strings)
            if j > 0 and line[j - 1] == '@':
                j += 1
                continue
            
            # String handling
            if (ch == '"' or ch == "'") and not in_string:
                in_string = True
                string_char = ch
                j += 1
                continue
            
            if in_string and ch == string_char:
                in_string = False
                string_char = None
                j += 1
                continue
            
            if in_string:
                j += 1
                continue
            
            # Line comment
            if ch == '/' and j + 1 < n and line[j + 1] == '/':
                break
            
            # Multi-line comment start
            if ch == '/' and j + 1 < n and line[j + 1] == '*':
                j += 2
                while j < n - 1:
                    if line[j] == '*' and line[j + 1] == '/':
                        break
                    j += 1
                j += 2
                continue
            
            # Braces
            if ch == '{':
                stack.append(i)
            
            elif ch == '}':
                if stack:
                    stack.pop()
            
            j += 1
    
    # Now find matching '}' for each brace in stack (outermost first = bottom of stack)
    containing = []
    
    for brace_idx in stack:
        depth = 0
        
        for i in range(brace_idx, len(lines)):
            line = lines[i]
            
            stripped = line.strip()
            if stripped.startswith('//'):
                continue
            
            # Count braces on this line (ignore strings/comments)
            j = 0
            n = len(line)
            found_close = False
            
            while j < n and not found_close:
                ch = line[j]
                
                if ch == '/' and j + 1 < n and line[j + 1] == '/':
                    break
                
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        containing.append({'header_idx': brace_idx, 'body_end': i})
                        found_close = True
                
                j += 1
            
            if found_close:
                break
    
    return containing


_BRACELESS_KW = ('if', 'else', 'for', 'foreach', 'while', 'do')


def _is_braceless_control(stripped):
    """A control header whose body is the next statement (no '{' on this line)."""
    if '{' in stripped or stripped.endswith(';') or stripped.endswith(','):
        return False
    for kw in _BRACELESS_KW:
        if stripped == kw or stripped.startswith(kw + ' ') or stripped.startswith(kw + '('):
            return True
    return False


def _scan_line_braces(line, stack, line_idx):
    """Update `stack` (list of open-brace line indices) for one C# line.

    Ignores braces inside strings, char/verbatim literals and comments.
    """
    j = 0
    n = len(line)
    while j < n:
        ch = line[j]
        if ch in ('"', "'"):
            quote = ch
            j += 1
            while j < n and line[j] != quote:
                j += 2 if line[j] == '\\' else 1
            j += 1
            continue
        if j + 1 < n and ch == '/' and line[j + 1] == '/':
            break
        if j + 1 < n and ch == '/' and line[j + 1] == '*':
            j += 2
            while j < n - 1:
                if line[j] == '*' and line[j + 1] == '/':
                    j += 2
                    break
                j += 1
            else:
                j = n
            continue
        if ch == '{':
            stack.append(line_idx)
        elif ch == '}':
            if stack:
                stack.pop()
        j += 1


class CSharpHandler(TreeSitterBlockHandler):
    """C# handler: tree-sitter navigation (outline/get_blocks/line_level via
    CS_SPEC) plus the regex `declarations()` used by interface cards."""

    SPEC = CS_SPEC

    def declarations(self, lines):
        """Declared surface (regex heuristic): public types + their public members.

        Returns dicts {name, kind, exported, reexport_from, signature, methods, start, end}.
        kind ∈ class|struct|interface|enum|record. exported = has `public` (C# defaults to
        internal). `methods` = public methods/properties/fields inside the type body.
        C# has no re-exports (namespace, not path) → reexport_from always None.
        """
        # 1. all type declarations + their body ranges.
        types = []
        for i, line in enumerate(lines):
            tm = _TYPE_RE.match(line)
            if not tm:
                continue
            types.append({
                "i": i, "end": find_body_end(lines, i),
                "name": tm.group("name"), "kind": tm.group("kind"),
                "exported": "public" in tm.group("mods"),
                "signature": re.split(r"\{", line.strip(), 1)[0].strip(),
                "methods": [], "seen": set(),
            })
        # 2. attribute each public member to its INNERMOST enclosing type (so a nested
        #    type's members are not double-counted onto the parent).
        for j, line in enumerate(lines):
            mem = _csharp_member(line)
            if not mem:
                continue
            best = None
            for t in types:
                if t["i"] < j <= t["end"] and (best is None or t["i"] > best["i"]):
                    best = t
            if best is not None and mem[0] not in best["seen"]:
                best["seen"].add(mem[0])
                best["methods"].append({"name": mem[0], "signature": mem[1]})
        # 3. emit
        return [{"name": t["name"], "kind": t["kind"], "exported": t["exported"],
                 "reexport_from": None, "signature": t["signature"],
                 "methods": t["methods"], "start": t["i"] + 1, "end": t["end"] + 1}
                for t in types]


# ⚠ DEAD (grep: 0 references in whole tree) — pre-migration brace nav, kept only for reference.
class _LegacyBraceNav:
    """LEGACY brace-heuristic navigation -- SUPERSEDED by the tree-sitter engine
    (CSharpHandler above, via CS_SPEC). Kept unused for reference / possible
    zero-dependency fallback; NOT registered anywhere. This was the previous
    get_blocks/line_level implementation before the tree-sitter port."""

    def line_level(self, lines, idx):
        """Logical nesting level of ONE line (0-based idx), 1-based.

        level = 1 + enclosing block BODIES (region inside matching {...}); the header
        up to '{' sits at the parent level, so a leading '}' counts against the parent.
        Brace-less control bodies (if/for/while/else with no '{') recovered via indent.
        Root = 1 (0 is reserved for --level addressing, never a real depth).
        """
        if idx < 0 or idx >= len(lines):
            return 1
        stack = []
        for i in range(idx):
            _scan_line_braces(lines[i], stack, i)
        depth = len(stack)
        content = lines[idx].lstrip()
        if content.startswith('}'):
            depth = max(depth - 1, 0)
        return depth + 1 + self._braceless_bonus(lines, idx)

    def _braceless_bonus(self, lines, idx):
        """Extra levels from brace-less control headers governing this line by indent."""
        bonus = 0
        cur_indent = get_indent(lines[idx])[0]
        j = idx - 1
        while j >= 0:
            s = lines[j].strip()
            if not s or s.startswith('//') or s.startswith('*') or s.startswith('/*'):
                j -= 1
                continue
            ind = get_indent(lines[j])[0]
            if ind < cur_indent and _is_braceless_control(s):
                bonus += 1
                cur_indent = ind
                j -= 1
                continue
            break
        return bonus

    def get_blocks(self, file_path, line_num):
        """Get blocks hierarchy containing the given line.
        
        Args:
            file_path: Path to source file
            line_num: 1-based target line number
            
        Returns:
            List of dicts sorted outermost-first:
            [{'level': N, 'start': X, 'end': Y}, ...]
            
            level=1 means top-level block in the file (not inside any other)
        """
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        target_idx = line_num - 1
        
        if not lines or target_idx < 0 or target_idx >= len(lines):
            return []

        # Check if target is a comment — attach to nearest block header above it
        stripped = lines[target_idx].strip()
        
        if stripped.startswith('//') or stripped.startswith('/*'):
            # Comment belongs to block above it
            attached = self._find_attached_block(lines, target_idx)
            if attached is not None:
                return self._build_hierarchy_for_header(lines, attached)
            else:
                return []

        # Find containing brace blocks
        containing_braces = find_containing_braces(lines, target_idx)
        
        if not containing_braces:
            return []

        # Build hierarchy from brace blocks
        result = []
        for i, block_info in enumerate(containing_braces):
            header_idx = block_info['header_idx']
            body_end = block_info['body_end']
            
            # Find actual semantic header line (may be before '{' on previous lines)
            actual_header = self._find_semantic_header(lines, header_idx)
            
            result.append({
                'level': i + 1,
                'start': actual_header + 1,  # Convert to 1-based
                'end': body_end + 1,
            })

        return result

    def _find_attached_block(self, lines, target_idx):
        """For a comment line, find the nearest block header above it."""
        for i in range(target_idx - 1, -1, -1):
            stripped = lines[i].strip()
            if not stripped or stripped.startswith('//'):
                continue
            
            if is_block_header(lines[i]):
                return i
        
        return None

    def _find_semantic_header(self, lines, brace_line_idx):
        """Find the semantic header line for a '{' position.
        
        In C#, headers can span multiple lines before the opening brace.
        We look backwards from the '{' to find where the declaration starts.
        """
        if brace_line_idx < 0 or brace_line_idx >= len(lines):
            return brace_line_idx
        
        brace_indent = get_indent(lines[brace_line_idx])[0]
        
        # If '{' is on its own line, look backwards for the header
        stripped = lines[brace_line_idx].strip()
        if stripped == '{':
            # Search backwards from previous non-blank/non-comment line
            i = brace_line_idx - 1
            
            while i >= 0:
                s = lines[i].strip()
                
                # Skip blank lines and comments immediately before header
                if not s or s.startswith('//'):
                    i -= 1
                    continue
                
                # Check if this line is part of a multi-line declaration
                ind = get_indent(lines[i])[0]
                
                # If indent matches brace level but it's not '{', we've gone too far
                if ind <= brace_indent and s != '{' and i < brace_line_idx - 1:
                    break
                
                return i
            
            return brace_line_idx
        
        return brace_line_idx

    def _build_hierarchy_for_header(self, lines, header_idx):
        """Build full hierarchy of blocks containing the given header."""
        target = header_idx
        ancestors = []
        
        while target >= 0:
            # Find nearest block header above current position
            found = False
            for h in range(target - 1, -1, -1):
                if is_block_header(lines[h]):
                    body_end = find_body_end(lines, h)
                    
                    # Check if this block contains our target line
                    if target >= h and target <= body_end:
                        ancestors.append((h, body_end))
                        found = True
                        target = h - 1
                        break
            
            if not found:
                break
        
        ancestors.reverse()
        
        result = []
        for i, (h, end) in enumerate(ancestors):
            result.append({
                'level': i + 1,
                'start': h + 1,  # Convert to 1-based
                'end': end + 1,
            })
        
        return result

