"""Drift guard: checked-in spec/jsonschema/state/ files must match the machines.

If this test fails, a lifecycle table changed without regenerating the
spec: run ``uv run python -m iab_agentic_primitives.state.spec_export``
and commit the diff (a transition-table change is a contract change —
see the semver policy in README.md).
"""

import json

import pytest

from iab_agentic_primitives.state.spec_export import (
    STATE_SPEC_DIR,
    render_document,
    state_spec_documents,
)

DOCUMENTS = state_spec_documents()


def test_spec_files_exactly_cover_state_exports() -> None:
    on_disk = {path.stem for path in STATE_SPEC_DIR.glob("*.json")}
    assert on_disk == set(DOCUMENTS), (
        "spec/jsonschema/state/ contents must exactly match the state exports; "
        "run `uv run python -m iab_agentic_primitives.state.spec_export`"
    )


@pytest.mark.parametrize("stem", sorted(DOCUMENTS))
def test_checked_in_spec_matches_machines(stem: str) -> None:
    path = STATE_SPEC_DIR / f"{stem}.json"
    assert path.exists(), (
        f"missing {path}; run `uv run python -m iab_agentic_primitives.state.spec_export`"
    )
    assert json.loads(path.read_text()) == DOCUMENTS[stem], (
        f"{stem} spec drifted from the machine; "
        "run `uv run python -m iab_agentic_primitives.state.spec_export` and review the diff"
    )
    # Text-level stability too (formatting is part of the checked-in artifact).
    assert path.read_text() == render_document(DOCUMENTS[stem])
