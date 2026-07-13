"""Regenerate the checked-in event schema artifacts in this directory.

Usage (from the repo root):

    uv run python spec/jsonschema/events/generate.py
"""

import json
from pathlib import Path

from iab_agentic_primitives.events.schema import build_event_schema, build_event_type_values

HERE = Path(__file__).parent

ARTIFACTS = {
    "event.schema.json": build_event_schema,
    "event_type.values.json": build_event_type_values,
}


def main() -> None:
    for filename, builder in ARTIFACTS.items():
        path = HERE / filename
        path.write_text(json.dumps(builder(), indent=2) + "\n")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
