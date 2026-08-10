"""Scaffold sanity tests: every subpackage imports and the version is set."""

import tomllib
from pathlib import Path

import iab_agentic_primitives
import iab_agentic_primitives.conformance
import iab_agentic_primitives.events
import iab_agentic_primitives.primitives
import iab_agentic_primitives.protocol
import iab_agentic_primitives.state


def test_version_matches_pyproject() -> None:
    """__version__ is single-sourced from package metadata (pyproject.toml).

    Regression test for the 0.1.0/0.5.0 mismatch reported in GitHub issue #2:
    the runtime version must always equal the version declared in pyproject.
    """
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    with pyproject.open("rb") as f:
        declared = tomllib.load(f)["project"]["version"]
    assert iab_agentic_primitives.__version__ == declared


def test_subpackages_importable_and_documented() -> None:
    subpackages = [
        iab_agentic_primitives.primitives,
        iab_agentic_primitives.protocol,
        iab_agentic_primitives.state,
        iab_agentic_primitives.events,
        iab_agentic_primitives.conformance,
    ]
    for module in subpackages:
        assert module.__doc__, f"{module.__name__} is missing its module docstring"
