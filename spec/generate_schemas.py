"""Export the JSON-Schema for every wire primitive to spec/jsonschema/.

The exported schemas are the normative, language-neutral artifacts (see
spec/README.md — the spec is the standard, the Python package is its
reference implementation). The generated files are checked in;
tests/test_schema_drift.py fails if they ever diverge from the models.

Usage:

    uv run python spec/generate_schemas.py
"""

from __future__ import annotations

import json
from pathlib import Path

from iab_agentic_primitives.primitives import WIRE_PRIMITIVES

SCHEMA_DIR = Path(__file__).parent / "jsonschema"


def render_schema(model: type) -> str:
    """Render a model's JSON-Schema as stable, diff-friendly JSON text."""
    schema = model.model_json_schema()  # type: ignore[attr-defined]
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main() -> None:
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    for name, model in WIRE_PRIMITIVES.items():
        path = SCHEMA_DIR / f"{name}.json"
        path.write_text(render_schema(model))
        print(f"wrote {path.relative_to(SCHEMA_DIR.parent.parent)}")


if __name__ == "__main__":
    main()
