"""Drift guard: checked-in protocol JSON Schemas must match the live models.

If this test fails, a protocol message changed without regenerating the
spec: run ``uv run python spec/jsonschema/protocol/generate.py`` and
commit the diff (remember: a wire-shape change is a breaking change —
see the semver policy in README.md).
"""

import json
import sys
from pathlib import Path

import pytest

from iab_agentic_primitives.protocol import PROTOCOL_MESSAGES

REPO_ROOT = Path(__file__).parent.parent
SCHEMA_DIR = REPO_ROOT / "spec" / "jsonschema" / "protocol"

sys.path.insert(0, str(SCHEMA_DIR))
from generate import render_schema  # noqa: E402


def test_schema_files_exactly_cover_protocol_messages() -> None:
    on_disk = {p.stem for p in SCHEMA_DIR.glob("*.json")}
    assert on_disk == set(PROTOCOL_MESSAGES), (
        "spec/jsonschema/protocol/ contents must exactly match PROTOCOL_MESSAGES; "
        "run `uv run python spec/jsonschema/protocol/generate.py`"
    )


@pytest.mark.parametrize("name", sorted(PROTOCOL_MESSAGES))
def test_checked_in_schema_matches_model(name: str) -> None:
    path = SCHEMA_DIR / f"{name}.json"
    assert path.exists(), (
        f"missing {path}; run `uv run python spec/jsonschema/protocol/generate.py`"
    )
    on_disk = json.loads(path.read_text())
    fresh = PROTOCOL_MESSAGES[name].model_json_schema()
    assert on_disk == fresh, (
        f"{name} schema drifted from the model; "
        "run `uv run python spec/jsonschema/protocol/generate.py` and review the diff"
    )
    # Text-level stability too (formatting is part of the checked-in artifact).
    assert path.read_text() == render_schema(PROTOCOL_MESSAGES[name])
