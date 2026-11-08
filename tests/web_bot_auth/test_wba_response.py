"""ResponseSigner and ResponseVerifier: components, the request binding, and refusals."""

from __future__ import annotations

import pytest

from iab_agentic_primitives.web_bot_auth import (
    Ed25519Key,
    HTTPRequest,
    HTTPResponse,
    Origin,
    Refusal,
    RequestSigner,
    ResponseSigner,
    ResponseVerifier,
    VerifiedResponse,
)
from iab_agentic_primitives.web_bot_auth.conformance import scenarios_document
from iab_agentic_primitives.web_bot_auth.conformance.models import Refuses, ResponseScenario
from iab_agentic_primitives.web_bot_auth.conformance.signing import (
    MessageDeparture,
    sign_response_scenario,
)
from iab_agentic_primitives.web_bot_auth.signer import SignerConfig
from iab_agentic_primitives.web_bot_auth.sync import SyncResponseVerifier, SyncRuntime
from iab_agentic_primitives.web_bot_auth.values import OMIT

from . import support as w
from .support import (
    COMMIT_RESULT_BODY,
    JSON,
    SELLER,
    SELLER_ORIGIN,
    T0,
    expect_refusal,
    run,
    signed_commit,
    signed_result,
)


def verify(
    response_verifier: ResponseVerifier,
    response: HTTPResponse,
    *,
    expected_origin: Origin | None = None,
) -> VerifiedResponse:
    return run(response_verifier.verify(response, expected_origin=expected_origin))


def test_signed_response_is_bound_to_the_signed_request(
    response_verifier: ResponseVerifier, buyer_key: Ed25519Key, seller_key: Ed25519Key
) -> None:
    request, headers = signed_result(seller_key, signed_commit(buyer_key))
    # The committed vector is the oracle: the generator signed it at the same components.
    golden = w.POSITIVE_003.headers["Signature-Input"]
    assert headers["Signature-Input"].startswith(golden.split(";created")[0] + ";")
    assert "nonce=" not in headers["Signature-Input"]
    result = verify(
        response_verifier,
        HTTPResponse(200, headers, COMMIT_RESULT_BODY, request),
        expected_origin=SELLER_ORIGIN,
    )
    assert result.origin == SELLER_ORIGIN and result.bound and result.body_covered


def test_response_to_an_unsigned_request_has_no_binding(
    response_verifier: ResponseVerifier, seller_key: Ed25519Key
) -> None:
    request = HTTPRequest("GET", f"{SELLER}/proposals/prop_7", {})
    headers = w.response_added(
        ResponseSigner.of(seller_key, SELLER), 200, JSON, COMMIT_RESULT_BODY, request, now=T0
    )
    assert '"signature";req' not in headers["Signature-Input"]
    result = verify(
        response_verifier, HTTPResponse(200, {**JSON, **headers}, COMMIT_RESULT_BODY, request)
    )
    assert result.bound is False


def test_response_binds_to_the_request_label(
    response_verifier: ResponseVerifier, seller_key: Ed25519Key, buyer_key: Ed25519Key
) -> None:
    request = w.craft(buyer_key, label="wba")
    req, headers = signed_result(seller_key, request)
    assert '"signature";req;key="wba"' in headers["Signature-Input"]
    assert verify(response_verifier, HTTPResponse(200, headers, COMMIT_RESULT_BODY, req)).bound


def test_response_without_a_body(
    response_verifier: ResponseVerifier, seller_key: Ed25519Key, buyer_key: Ed25519Key
) -> None:
    request = signed_commit(buyer_key)
    req = request
    headers = w.response_added(ResponseSigner.of(seller_key, SELLER), 204, {}, b"", req, now=T0)
    assert "Content-Digest" not in headers
    assert verify(response_verifier, HTTPResponse(204, headers, b"", req)).body_covered is False


def test_unbound_response_to_a_signed_request(
    response_verifier: ResponseVerifier, buyer_key: Ed25519Key, seller_key: Ed25519Key
) -> None:
    request = signed_commit(buyer_key)
    signed = request
    unsigned = HTTPRequest(signed.method, signed.url, JSON, signed.body)
    headers = w.response_added(
        ResponseSigner.of(seller_key, SELLER), 200, JSON, COMMIT_RESULT_BODY, unsigned, now=T0
    )
    expect_refusal(
        lambda: verify(
            response_verifier, HTTPResponse(200, {**JSON, **headers}, COMMIT_RESULT_BODY, signed)
        ),
        Refusal.RESPONSE_NOT_BOUND,
    )


def test_response_presented_with_another_request(
    response_verifier: ResponseVerifier, buyer_key: Ed25519Key, seller_key: Ed25519Key
) -> None:
    first, headers = signed_result(seller_key, signed_commit(buyer_key))
    other = signed_commit(buyer_key)
    other_req = other
    expect_refusal(
        lambda: verify(
            response_verifier, HTTPResponse(200, headers, COMMIT_RESULT_BODY, other_req)
        ),
        Refusal.SIGNATURE_INVALID,
    )


@pytest.mark.parametrize(
    ("status", "body", "refusal"),
    [
        (201, COMMIT_RESULT_BODY, Refusal.SIGNATURE_INVALID),
        (200, COMMIT_RESULT_BODY + b" ", Refusal.CONTENT_DIGEST),
    ],
)
def test_tampered_response(
    response_verifier: ResponseVerifier,
    seller_key: Ed25519Key,
    status: int,
    body: bytes,
    refusal: Refusal,
) -> None:
    request, headers = signed_result(seller_key, signed_commit(w.buyer_key()))
    expect_refusal(
        lambda: verify(response_verifier, HTTPResponse(status, headers, body, request)), refusal
    )


def test_expected_origin(
    response_verifier: ResponseVerifier, buyer_key: Ed25519Key, seller_key: Ed25519Key
) -> None:
    request, headers = signed_result(seller_key, signed_commit(buyer_key))
    expect_refusal(
        lambda: verify(
            response_verifier,
            HTTPResponse(200, headers, COMMIT_RESULT_BODY, request),
            expected_origin=w.OTHER_ORIGIN,
        ),
        Refusal.ORIGIN_MISMATCH,
    )


def test_key_must_be_in_the_responders_directory(
    response_verifier: ResponseVerifier, buyer_key: Ed25519Key
) -> None:
    request, headers = signed_result(buyer_key, signed_commit(buyer_key))
    expect_refusal(
        lambda: verify(response_verifier, HTTPResponse(200, headers, COMMIT_RESULT_BODY, request)),
        Refusal.KEY_NOT_FOUND,
    )


_REFUSED_SCENARIOS = [
    s for s in scenarios_document().response_scenarios if isinstance(s.expected, Refuses)
]


@pytest.mark.parametrize("scenario", _REFUSED_SCENARIOS, ids=lambda s: s.id)
def test_each_refused_response_scenario_is_refused_for_its_rule(
    response_verifier: ResponseVerifier,
    buyer_key: Ed25519Key,
    seller_key: Ed25519Key,
    scenario: ResponseScenario,
) -> None:
    expected = scenario.expected
    assert isinstance(expected, Refuses)
    keys: dict[str, Ed25519Key] = {k.thumbprint: k for k in (buyer_key, seller_key)}
    request, signed = sign_response_scenario(scenarios_document(), scenario, keys, T0)
    response = signed.message(request.message())
    expect_refusal(lambda: w.run(response_verifier.verify(response)), expected.rule)


def test_response_verify_sync(
    response_verifier: ResponseVerifier, buyer_key: Ed25519Key, seller_key: Ed25519Key
) -> None:
    request, headers = signed_result(seller_key, signed_commit(buyer_key))
    with SyncRuntime() as runtime:
        sync = SyncResponseVerifier(response_verifier, runtime=runtime)
        verified = sync.verify(HTTPResponse(200, headers, COMMIT_RESULT_BODY, request))
    assert verified.origin == SELLER_ORIGIN


def test_a_request_with_two_web_bot_auth_signatures_gets_an_unbound_response(
    response_verifier: ResponseVerifier, buyer_key: Ed25519Key, seller_key: Ed25519Key
) -> None:
    # A request whose web-bot-auth signature is ambiguous has no signature to bind to.
    agents = f'sig1="{w.BUYER_ORIGIN.directory_url}", sig2="{w.OTHER_ORIGIN.directory_url}"'
    first = w.sign_request(now=T0, departure=MessageDeparture(signature_agent=agents))
    second = w.sign_request(
        first,
        RequestSigner(SignerConfig(w.other_key(), Origin.parse(w.OTHER), label="sig2")),
        departure=MessageDeparture(signature_agent=OMIT, keep_signatures=True),
    )
    response = ResponseSigner.of(seller_key, SELLER).sign(
        HTTPResponse(200, JSON, COMMIT_RESULT_BODY, second), now=T0
    )
    result = verify(response_verifier, response, expected_origin=SELLER_ORIGIN)
    assert result.bound is False
    assert all(c.name != "signature" for c in result.components)
