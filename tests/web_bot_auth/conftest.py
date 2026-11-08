"""Fixtures for the Web Bot Auth tests: the published keys and the two verifiers.

The dev group carries the ``wba`` libraries, so the suite always runs.
"""

import pytest

from iab_agentic_primitives.web_bot_auth import Ed25519Key, RequestVerifier, ResponseVerifier

from . import support
from .support import buyer_key as _buyer
from .support import other_key as _other
from .support import request_verifier
from .support import seller_key as _seller


@pytest.fixture
def buyer_key() -> Ed25519Key:
    return _buyer()


@pytest.fixture
def other_key() -> Ed25519Key:
    return _other()


@pytest.fixture
def seller_key() -> Ed25519Key:
    return _seller()


@pytest.fixture
def verifier(buyer_key: Ed25519Key, other_key: Ed25519Key) -> RequestVerifier:
    """The seller's verifier, over the directories of the buyer and the other origin."""
    return request_verifier()


@pytest.fixture
def response_verifier() -> ResponseVerifier:
    """The buyer's verifier of the seller's responses."""
    return support.response_verifier()
