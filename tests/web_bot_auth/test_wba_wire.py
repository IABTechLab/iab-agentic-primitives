"""The wire format, through the public verifiers and signers.

What the signature fields must look like, how a covered ``Content-Digest`` is
checked, what the signers refuse, and which parameters the credential carries.
Every case goes through :class:`RequestVerifier`, :class:`ResponseVerifier`,
or a signer, so a restructure of the seams behind them doesn't touch it.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from http_message_signatures.exceptions import HTTPMessageSignaturesException

from iab_agentic_primitives.web_bot_auth import (
    HTTPRequest,
    HTTPResponse,
    Refusal,
    RequestSigner,
    ResponseSigner,
    SigningError,
    VerifiedRequest,
)
from iab_agentic_primitives.web_bot_auth.conformance.models import SignatureParameters
from iab_agentic_primitives.web_bot_auth.conformance.signing import MessageDeparture
from iab_agentic_primitives.web_bot_auth.rules import (
    CONTENT_DIGEST,
    REQUEST,
    RESPONSE,
    component_id,
)
from iab_agentic_primitives.web_bot_auth.signer import generate_nonce

from .support import (
    BUYER,
    COMMIT_BODY,
    COMMIT_URL,
    JSON,
    SELLER,
    T0,
    at,
    buyer_key,
    digest,
    digest_without_sha256,
    expect_refusal,
    request_verifier,
    response_verifier,
    run,
    seller_key,
    sign_request,
    sign_response,
)

AGENT = f'sig1="{BUYER}/.well-known/http-message-signatures-directory"'
COVERED = '("@method" "signature-agent";key="sig1")'
TAGGED = f'sig1={COVERED};created={T0};expires={at(300)};keyid="k";tag="web-bot-auth"'


def _fields(**fields: str) -> HTTPRequest:
    """A GET to the seller carrying these raw signature fields."""
    names = {"input": "Signature-Input", "signature": "Signature", "agent": "Signature-Agent"}
    return HTTPRequest("GET", COMMIT_URL, [(names[k], v) for k, v in fields.items()])


def _verify(request: HTTPRequest, *, offset: int = 5) -> VerifiedRequest:
    return run(request_verifier(now=at(offset)).verify(request))


# ── the signature fields ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("message", "refusal"),
    [
        (HTTPRequest("GET", COMMIT_URL, {"Signature-Agent": AGENT}), Refusal.SIGNATURE_MISSING),
        (_fields(input="sig1=("), Refusal.PARSE),
        (_fields(input='sig1=("@méthod")'), Refusal.PARSE),
        (_fields(input='sig1=("@method");tag="a";tag="b"'), Refusal.PARSE),
        (_fields(input='sig1="@method"'), Refusal.PARSE),
        (_fields(input=TAGGED, signature="sig1=token"), Refusal.PARSE),
        (_fields(input="sig1=(method)"), Refusal.PARSE),
        (_fields(input='sig1=("@method";req=1)'), Refusal.PARSE),
        (_fields(input='sig1=("@method";q=1;q=2)'), Refusal.PARSE),
        (_fields(input='sig1=();created="1"'), Refusal.PARSE),
        (_fields(input="sig1=();keyid=1"), Refusal.PARSE),
        (_fields(input="sig1=();nonce=:AAAA:"), Refusal.PARSE),
        (_fields(input=TAGGED, agent='sig1="a";x;x'), Refusal.PARSE),
        (_fields(input=f"{TAGGED}, {TAGGED}"), Refusal.MULTIPLE_SIGNATURES),
        (_fields(input=TAGGED, signature="sig1=:AA==:, sig1=:AA==:"), Refusal.MULTIPLE_SIGNATURES),
        (
            _fields(input=TAGGED, signature="sig1=:AA==:", agent=f"{AGENT}, {AGENT}"),
            Refusal.MULTIPLE_SIGNATURE_AGENTS,
        ),
        # Step 1 runs whole before step 2: a malformed member is 400, whatever repeats.
        (_fields(input=f"{TAGGED}, {TAGGED}, sig2=tok"), Refusal.PARSE),
        # A Signature with no Signature-Input, such as another scheme's, is not parsed.
        (
            _fields(signature='keyId="k",algorithm="hs2019",headers="date",signature="abc"'),
            Refusal.SIGNATURE_MISSING,
        ),
        (_fields(signature="sig1=:AAAA:"), Refusal.SIGNATURE_MISSING),
        (_fields(input=f"sig1={COVERED}", signature="sig1=:AA==:"), Refusal.SIGNATURE_MISSING),
        (
            _fields(input=f"{TAGGED}, {TAGGED.replace('sig1=', 'sig2=')}", signature="sig1=:AA==:"),
            Refusal.MULTIPLE_SIGNATURES,
        ),
        (_fields(input=TAGGED, signature="sig2=:AA==:"), Refusal.SIGNATURE_MISSING),
    ],
    ids=[
        "unsigned",
        "not-a-dictionary",
        "not-ascii",
        "repeated-parameter",
        "member-not-an-inner-list",
        "signature-not-a-byte-sequence",
        "component-not-a-string",
        "component-parameter-not-a-string-or-boolean",
        "repeated-component-parameter",
        "created-not-an-integer",
        "keyid-not-a-string",
        "nonce-not-a-string",
        "signature-agent-repeated-parameter",
        "repeated-member",
        "repeated-signature",
        "repeated-signature-agent",
        "malformed-member-before-repeated-label",
        "cavage-signature-alone",
        "signature-alone",
        "no-web-bot-auth-member",
        "two-web-bot-auth-members",
        "no-signature-member",
    ],
)
def test_the_signature_fields_are_refused(message: HTTPRequest, refusal: Refusal) -> None:
    expect_refusal(lambda: _verify(message), refusal)


@pytest.mark.parametrize(
    ("agent", "refusal"),
    [
        (f'{AGENT}, sig2="{BUYER}"', Refusal.MULTIPLE_SIGNATURE_AGENTS),
        (f"{AGENT};type=directory", Refusal.SIGNATURE_AGENT_PARAMETERS),
        (f'sig1="http://{BUYER[len("https://") :]}"', Refusal.SIGNATURE_AGENT_VALUE),
        ("sig1=:AAAA:", Refusal.SIGNATURE_AGENT_VALUE),
    ],
    ids=["two-members", "parameters", "http", "not-a-string"],
)
def test_the_signature_agent_member_is_refused(agent: str, refusal: Refusal) -> None:
    signed = sign_request(departure=MessageDeparture(signature_agent=agent))
    expect_refusal(lambda: _verify(signed), refusal)


@pytest.mark.parametrize("agent", [None, AGENT.replace("sig1=", "sig2=")], ids=["absent", "other"])
def test_no_signature_agent_member_for_the_label_is_refused(agent: str | None) -> None:
    fields = {"input": TAGGED, "signature": "sig1=:AA==:"} | ({"agent": agent} if agent else {})
    expect_refusal(lambda: _verify(_fields(**fields)), Refusal.SIGNATURE_AGENT_MISSING)


def test_an_uncovered_signature_agent_member_is_refused() -> None:
    uncovered = REQUEST.components("sig1", has_body=True)[:-1]
    signed = sign_request(departure=MessageDeparture(components=uncovered))
    expect_refusal(lambda: _verify(signed), Refusal.SIGNATURE_AGENT_NOT_COVERED)


def test_other_signature_agent_members_beside_the_label_are_ignored() -> None:
    signed = sign_request(departure=MessageDeparture(signature_agent=f'{AGENT}, x="y";p'))
    assert _verify(signed).origin.host == "buyer.example.com"


def test_a_repeated_label_on_another_signers_member_leaves_the_signature_valid() -> None:
    # Only the web-bot-auth member must be unique (OpenProposal § 2.8.2).
    signed = sign_request()
    other = 'other=("@method");tag="x"'
    lines = signed.headers.with_replaced(
        "Signature-Input", f"{signed.headers['Signature-Input']}, {other}, {other}"
    ).with_replaced("Signature", f"{signed.headers['Signature']}, other=:AA==:, other=:AA==:")
    assert _verify(HTTPRequest(signed.method, signed.url, lines, signed.body)).origin.host == (
        "buyer.example.com"
    )


def test_a_binding_with_its_parameters_in_another_order_still_binds() -> None:
    swapped = component_id("signature", {"key": "sig1", "req": True})
    assert swapped == RESPONSE.binding("sig1")
    assert str(swapped) != str(RESPONSE.binding("sig1"))
    components = (*RESPONSE.components("sig1", has_body=True), swapped)
    response = sign_response(sign_request(), departure=MessageDeparture(components=components))
    assert run(response_verifier().verify(response)).bound is True


# ── a covered Content-Digest ──────────────────────────────────────────────


def _with_digest(value: str) -> HTTPRequest:
    """The commit, signed, with ``value`` as its Content-Digest from the start when it
    parses, and put in after signing when the library can't sign over it."""
    try:
        unchecked = MessageDeparture(content_digest=value)
        return sign_request(HTTPRequest("POST", COMMIT_URL, JSON, COMMIT_BODY), departure=unchecked)
    except SigningError:
        signed = sign_request()
        return HTTPRequest(
            "POST", COMMIT_URL, signed.headers.with_replaced("Content-Digest", value), COMMIT_BODY
        )


def test_a_digest_with_sha_256_and_another_algorithm_verifies() -> None:
    both = f"{digest_without_sha256(COMMIT_BODY)}, {digest(COMMIT_BODY)}"
    assert _verify(_with_digest(both)).body_covered is True


@pytest.mark.parametrize(
    "value",
    [
        digest_without_sha256(COMMIT_BODY),
        digest(b"y"),
        f"{digest(COMMIT_BODY)}, {digest(COMMIT_BODY)}",
        "sha-256=(",
    ],
    ids=["sha-512-only", "mismatch", "repeated-algorithm", "malformed"],
)
def test_a_bad_digest_is_refused(value: str) -> None:
    expect_refusal(lambda: _verify(_with_digest(value)), Refusal.CONTENT_DIGEST)


def test_a_bodyless_request_with_an_uncovered_digest_verifies_as_signed() -> None:
    # The signer covers Content-Digest only with a body, so the verifier gives
    # the library no body to check for a message whose signature doesn't cover it.
    request = HTTPRequest("GET", COMMIT_URL, {"Content-Digest": digest(b"")})
    verified = _verify(sign_request(request))
    assert verified.body_covered is False
    assert CONTENT_DIGEST not in verified.components


# ── signing: every failure is a SigningError ──────────────────────────────


def _buyer() -> RequestSigner:
    return RequestSigner.of(buyer_key(), BUYER)


SIGNED = {"Signature": "sig1=:AA==:", "Signature-Input": "sig1=()"}
MISMATCHED = {"Content-Type": "text/plain", "Content-Digest": digest(b"y")}

SIGNINGS: list[Callable[[], object]] = [
    lambda: ResponseSigner.of(seller_key(), SELLER).sign(
        HTTPResponse(200, {}, b"", HTTPRequest("GET", "/proposals/prop_7")), now=T0
    ),
    lambda: _buyer().sign(HTTPRequest("GET", "seller.example.com/x"), now=T0),
    lambda: _buyer().sign(HTTPRequest("POST", COMMIT_URL, {}, b"x"), now=T0),
    lambda: _buyer().sign(HTTPRequest("GET", COMMIT_URL, SIGNED), now=T0),
    lambda: _buyer().sign(HTTPRequest("GET", COMMIT_URL), now=T0, nonce="short"),
    lambda: _buyer().sign(HTTPRequest("POST", COMMIT_URL, MISMATCHED, b"x"), now=T0),
]


@pytest.mark.parametrize(
    "sign",
    SIGNINGS,
    ids=[
        "relative-response-request-url",
        "relative-request-url",
        "body-without-content-type",
        "already-signed",
        "invalid-nonce",
        "mismatched-digest",
    ],
)
def test_a_signer_raises_only_signing_error(sign: Callable[[], object]) -> None:
    with pytest.raises(SigningError) as raised:
        sign()
    assert isinstance(raised.value, ValueError)
    assert not isinstance(raised.value, HTTPMessageSignaturesException)


def test_a_library_error_is_translated_with_its_cause() -> None:
    # The library's URL grammar refuses a host with a space in it.
    with pytest.raises(SigningError) as raised:
        _buyer().sign(HTTPRequest("GET", "https://seller example.com/x"), now=T0)
    assert isinstance(raised.value.__cause__, HTTPMessageSignaturesException)


@pytest.mark.parametrize("url", ["/x", "seller.example.com/x", "ftp://seller.example.com/x"])
def test_a_url_that_is_not_absolute_http_is_not_signed(url: str) -> None:
    with pytest.raises(SigningError, match="not an absolute http or https URL"):
        _buyer().sign(HTTPRequest("GET", url), now=T0)


def test_a_claimed_alg_is_signed_as_claimed_and_refused() -> None:
    signed = sign_request(departure=MessageDeparture(parameters=SignatureParameters(alg="x")))
    assert ';alg="x";' in (signed.headers.get("signature-input") or "")
    expect_refusal(lambda: _verify(signed), Refusal.ALG)


# ── the credential comes from the signed parameters ───────────────────────


def test_the_credential_carries_the_signed_parameters() -> None:
    nonce = generate_nonce()
    signed = _buyer().sign(HTTPRequest("POST", COMMIT_URL, JSON, COMMIT_BODY), now=T0, nonce=nonce)
    verified = _verify(signed)
    assert (verified.created, verified.expires, verified.nonce) == (T0, at(300), nonce)
    assert verified.body_covered is True


def test_a_second_unsigned_member_does_not_change_the_credential() -> None:
    nonce = generate_nonce()
    signed = _buyer().sign(HTTPRequest("GET", COMMIT_URL), now=T0, nonce=nonce)
    other = f'cdn=("@method");created={at(-50)};expires={at(9)};nonce="other";tag="cdn"'
    inputs = f"{signed.headers.get('signature-input')}, {other}"
    signatures = f"{signed.headers.get('signature')}, cdn=:AAAA:"
    lines = signed.headers.with_replaced("Signature-Input", inputs)
    verified = _verify(HTTPRequest("GET", COMMIT_URL, lines.with_replaced("Signature", signatures)))
    assert (verified.created, verified.expires, verified.nonce) == (T0, at(300), nonce)
    assert verified.body_covered is False


@pytest.mark.parametrize(
    ("departure", "refusal"),
    [
        (MessageDeparture(parameters=SignatureParameters(expires=None)), Refusal.WINDOW),
        (MessageDeparture(parameters=SignatureParameters(tag=None)), Refusal.SIGNATURE_MISSING),
        (MessageDeparture(parameters=SignatureParameters(alg=None)), Refusal.ALG),
    ],
    ids=["no-expires", "no-tag", "no-alg"],
)
def test_a_signature_without_a_required_parameter_is_refused(
    departure: MessageDeparture, refusal: Refusal
) -> None:
    expect_refusal(lambda: _verify(sign_request(departure=departure)), refusal)
