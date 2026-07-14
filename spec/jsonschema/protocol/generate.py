"""Export the JSON Schema for every protocol message to this directory.

Same conventions as the primitives export (``spec/generate_schemas.py``):
the checked-in files are the normative, language-neutral artifacts, and
``tests/test_protocol_schema_drift.py`` fails if they ever diverge from
the models.

Usage (from the repo root):

    uv run python spec/jsonschema/protocol/generate.py
"""

from __future__ import annotations

import json
from pathlib import Path

from iab_agentic_primitives.protocol import PROTOCOL_MESSAGES

HERE = Path(__file__).parent


def render_schema(model: type) -> str:
    """Render a model's JSON Schema as stable, diff-friendly JSON text."""
    schema = model.model_json_schema()  # type: ignore[attr-defined]
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main() -> None:
    for name, model in PROTOCOL_MESSAGES.items():
        path = HERE / f"{name}.json"
        path.write_text(render_schema(model))
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
