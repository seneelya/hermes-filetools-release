"""Shared loader: turn a schema YAML file (e.g. `view_file.yaml`) into the
JSON-Schema dict `ctx.register_tool()` expects.

Why YAML, not a dict literal inline in Python (2026-08-25 decision): editing a
long prose `description` as one escaped Python string was uncomfortable
(nested quotes, no line wrapping). YAML block scalars (`>`) let a description
be written as plain wrapped text, no escaping, and the repeated
`"parameters": {"type": "object", "properties": ..., "required": ...}` wrapper
moves into this one loader instead of being copy-pasted into every file.

One file = one schema (2026-08-25 decision): each tool slot has its own YAML
file (e.g. `view_file.yaml`, `view_file_new.yaml` for a future alternate
implementation) rather than several `name:` blocks packed into one file -
`register(ctx)` addresses schemas by exact filename (`settings.tools.<slot>.
schema`), so there's nothing to gain from scanning/parsing a whole directory
to find one by its internal `name:` field.

Schemas are read with Hermes' own loader (`hermes_yaml.safe_load`, ruamel).
The console `main()` entry points in each `*_core.py` never import this module,
so they still run without it.
"""

import re
from pathlib import Path

from hermes_yaml import safe_load

_SCHEMA_DIR = Path(__file__).resolve().parent

# `{block_open}`, `{anchor_word}`, `{read_file.file_tag}` (Plan28). Descriptions are prompt text
# for the MODEL, and they used to spell symbols the human can change (`■BLOCK`) and words the
# tools print — so a changed `block_symbol` reached the tool's output and never its description.
# Values come from `_vocab.placeholders(symbols)`; the yaml holds only the names.
PLACEHOLDER = re.compile(r"\{([a-z_]+(?:\.[a-z_]+)?)\}")


def fill(text: str, values: dict) -> str:
    """Replace known placeholders; an unknown one is left as is (a test fails on it)."""
    return PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), m.group(0))), text)


def _fill_tree(node, values: dict):
    """Every `description` string, however deep (parameters, nested items)."""
    if isinstance(node, dict):
        return {k: (fill(v, values) if k == "description" and isinstance(v, str)
                    else _fill_tree(v, values)) for k, v in node.items()}
    if isinstance(node, list):
        return [_fill_tree(v, values) for v in node]
    return node


def load_schema(filename: str, values: dict | None = None) -> dict:
    """Read `<filename>` (relative to this file's directory) and shape it into
    `{"name", "description", "parameters": {"type": "object", "properties", "required"}}`.

    `values` — placeholder values (`_vocab.placeholders(symbols)`); registration always passes
    them. Without them the text keeps its `{names}` — fine for the panel, which drops
    descriptions anyway.
    """
    data = safe_load((_SCHEMA_DIR / filename).read_text(encoding="utf-8"))
    values = values or {}
    return {
        "name": data["name"],
        "description": fill(data["description"].strip(), values),
        "parameters": {
            "type": "object",
            "properties": _fill_tree(dict(data.get("properties", {})), values),
            "required": data.get("required", []),
        },
    }
