"""The profile rules: every signer configuration round-trips, every bad one is
refused at construction, and the verifiers require exactly the rules' components.

The windows are graded where they apply: the message window by the request and
response verifiers, the directory window by the directory tests.
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from iab_agentic_primitives.web_bot_auth import (
    ConfigurationError,
    HTTPRequest,
    HTTPResponse,
    Origin,
    RequestSigner,
    RequestVerifier,
    ResponseSigner,
    ResponseVerifier,
    StaticKeyResolver,
    VerifiedRequest,
    VerifiedResponse,
    WebBotAuthError,
)
from iab_agentic_primitives.web_bot_auth.conformance.signing import (
    MessageDeparture,
    RawSigner,
    components_of,
)
from iab_agentic_primitives.web_bot_auth.errors import Refusal
from iab_agentic_primitives.web_bot_auth.rules import (
    MAX_LIFETIME,
    REQUEST,
    RESPONSE,
)
from iab_agentic_primitives.web_bot_auth.signer import SignerConfig

from .support import (
    BUYER,
    COMMIT_BODY,
    COMMIT_RESULT_BODY,
    COMMIT_URL,
    JSON,
    SELLER,
    T0,
    UnixSeconds,
    at,
    buyer_key,
    request_verifier,
    response_verifier,
    seller_key,
    spec,
)

LABELS = ["sig1", "a", "x-1.*"]
LIFETIMES = [1, 150, MAX_LIFETIME]
NONCE_BYTES = bytes(range(64))
NONCES = {
    "base64url": base64.urlsafe_b64encode(NONCE_BYTES).rstrip(b"=").decode(),
    "base64": base64.b64encode(NONCE_BYTES).decode(),
}


def _request_verifier(now: UnixSeconds) -> RequestVerifier:
    return request_verifier(resolver=StaticKeyResolver({BUYER: [buyer_key()]}), now=now)


def _response_verifier(now: UnixSeconds) -> ResponseVerifier:
    return response_verifier(resolver=StaticKeyResolver({SELLER: [seller_key()]}), now=now)


def _request(has_body: bool) -> HTTPRequest:
    if has_body:
        return HTTPRequest("POST", COMMIT_URL, JSON, COMMIT_BODY)
    return HTTPRequest("GET", COMMIT_URL)


# ── round trips over the signer's options ────────────────────────────────


@pytest.mark.parametrize("encoding", sorted(NONCES))
@pytest.mark.parametrize("has_body", [True, False], ids=["body", "no-body"])
@pytest.mark.parametrize("lifetime", LIFETIMES)
@pytest.mark.parametrize("label", LABELS)
def test_every_request_configuration_signs_and_verifies(
    label: str, lifetime: int, has_body: bool, encoding: str
) -> None:
    signer = RequestSigner(
        SignerConfig(buyer_key(), Origin.parse(BUYER), label=label, lifetime=lifetime)
    )
    signed = signer.sign(_request(has_body), now=T0, nonce=NONCES[encoding])
    verified = asyncio.run(_request_verifier(at(lifetime)).verify(signed))
    assert (verified.label, verified.expires, verified.nonce) == (
        label,
        at(lifetime),
        NONCES[encoding],
    )
    assert verified.components == REQUEST.components(label, has_body=has_body)
    assert verified.body_covered is has_body


@pytest.mark.parametrize("bound", [True, False], ids=["bound", "unbound"])
@pytest.mark.parametrize("has_body", [True, False], ids=["body", "no-body"])
@pytest.mark.parametrize("lifetime", LIFETIMES)
@pytest.mark.parametrize("label", LABELS)
def test_every_response_configuration_signs_and_verifies(
    label: str, lifetime: int, has_body: bool, bound: bool
) -> None:
    request = _request(True)
    if bound:
        request = RequestSigner.of(buyer_key(), BUYER).sign(request, now=T0)
    body = COMMIT_RESULT_BODY if has_body else b""
    response = HTTPResponse(200, JSON if has_body else {}, body, request)
    signer = ResponseSigner(
        SignerConfig(seller_key(), Origin.parse(SELLER), label=label, lifetime=lifetime)
    )
    signed = signer.sign(response, now=T0)
    verified = asyncio.run(_response_verifier(at(lifetime)).verify(signed))
    request_label = "sig1" if bound else None
    expected = RESPONSE.components(label, has_body=has_body, request_label=request_label)
    assert verified.components == expected
    assert (verified.label, verified.bound) == (label, bound)


# ── configurations the signers refuse ─────────────────────────────────────


@pytest.mark.parametrize("signer", [RequestSigner, ResponseSigner])
@pytest.mark.parametrize(
    "options",
    [
        {"lifetime": 0},
        {"lifetime": MAX_LIFETIME + 1},
        {"lifetime": -1},
        {"lifetime": True},
        {"lifetime": "300"},
        {"label": "Sig1"},
        {"label": "1sig"},
        {"label": ""},
        {"label": "sig 1"},
        {"label": 1},
    ],
    ids=[
        "lifetime-0",
        "lifetime-301",
        "lifetime-negative",
        "lifetime-bool",
        "lifetime-str",
        "label-uppercase",
        "label-digit-first",
        "label-empty",
        "label-space",
        "label-int",
    ],
)
def test_a_signer_refuses_an_out_of_range_configuration(
    signer: type[RequestSigner] | type[ResponseSigner], options: dict[str, object]
) -> None:
    with pytest.raises(ConfigurationError):
        signer(SignerConfig(buyer_key(), Origin.parse(BUYER), **options))  # type: ignore[arg-type]


# ── the verifiers require exactly the rules' components ──────────────────


def _crafted_request(label: str, has_body: bool, components: list[str]) -> HTTPRequest:
    covered = MessageDeparture(components=components_of(components, label))
    config = SignerConfig(buyer_key(), Origin.parse(BUYER), label=label)
    return RawSigner(config, covered).request(_request(has_body), T0)


def _request_outcome(request: HTTPRequest) -> VerifiedRequest | Refusal:
    try:
        return asyncio.run(_request_verifier(T0).verify(request))
    except WebBotAuthError as e:
        return e.refusal


@pytest.mark.parametrize("has_body", [True, False], ids=["body", "no-body"])
@pytest.mark.parametrize("label", ["sig1", "agent7"])
def test_the_request_verifier_requires_exactly_the_rules_components(
    label: str, has_body: bool
) -> None:
    components = [spec(c) for c in REQUEST.components(label, has_body=has_body)]
    assert isinstance(
        _request_outcome(_crafted_request(label, has_body, components)), VerifiedRequest
    )
    for dropped in components:
        kept = [c for c in components if c != dropped]
        outcome = _request_outcome(_crafted_request(label, has_body, kept))
        assert outcome in (Refusal.COMPONENTS, Refusal.SIGNATURE_AGENT_NOT_COVERED), dropped


def _crafted_response(
    label: str, has_body: bool, bound: bool, components: list[str]
) -> HTTPResponse:
    request = _request(True)
    if bound:
        request = RequestSigner.of(buyer_key(), BUYER).sign(request, now=T0)
    body = COMMIT_RESULT_BODY if has_body else b""
    covered = MessageDeparture(components=components_of(components, label))
    config = SignerConfig(seller_key(), Origin.parse(SELLER), label=label)
    return RawSigner(config, covered).response(
        HTTPResponse(200, JSON if has_body else {}, body, request), T0
    )


def _response_outcome(response: HTTPResponse) -> VerifiedResponse | Refusal:
    try:
        return asyncio.run(_response_verifier(T0).verify(response))
    except WebBotAuthError as e:
        return e.refusal


@pytest.mark.parametrize("bound", [True, False], ids=["bound", "unbound"])
@pytest.mark.parametrize("has_body", [True, False], ids=["body", "no-body"])
@pytest.mark.parametrize("label", ["sig1", "agent7"])
def test_the_response_verifier_requires_exactly_the_rules_components(
    label: str, has_body: bool, bound: bool
) -> None:
    request_label = "sig1" if bound else None
    rules = RESPONSE.components(label, has_body=has_body, request_label=request_label)
    components = [spec(c) for c in rules]
    signed = _crafted_response(label, has_body, bound, components)
    assert isinstance(_response_outcome(signed), VerifiedResponse)
    refusals = (Refusal.COMPONENTS, Refusal.SIGNATURE_AGENT_NOT_COVERED, Refusal.RESPONSE_NOT_BOUND)
    for dropped in components:
        kept = [c for c in components if c != dropped]
        outcome = _response_outcome(_crafted_response(label, has_body, bound, kept))
        assert outcome in refusals, dropped
