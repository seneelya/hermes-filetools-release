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

from pathlib import Path

from hermes_yaml import safe_load

_SCHEMA_DIR = Path(__file__).resolve().parent


def load_schema(filename: str) -> dict:
    """Read `<filename>` (relative to this file's directory) and shape it into
    `{"name", "description", "parameters": {"type": "object", "properties", "required"}}`.
    """
    data = safe_load((_SCHEMA_DIR / filename).read_text(encoding="utf-8"))
    return {
        "name": data["name"],
        "description": data["description"].strip(),
        "parameters": {
            "type": "object",
            "properties": data.get("properties", {}),
            "required": data.get("required", []),
        },
    }
