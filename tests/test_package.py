"""Scaffold sanity tests: every subpackage imports and the version is set."""

import iab_agentic_primitives
import iab_agentic_primitives.conformance
import iab_agentic_primitives.events
import iab_agentic_primitives.primitives
import iab_agentic_primitives.protocol
import iab_agentic_primitives.state


def test_version() -> None:
    assert iab_agentic_primitives.__version__ == "0.1.0"


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
