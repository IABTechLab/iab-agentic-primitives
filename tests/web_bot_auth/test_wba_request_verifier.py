"""RequestSigner and RequestVerifier: every profile rule and every refusal."""

from __future__ import annotations

import base64
import threading
from collections.abc import Callable
from dataclasses import replace
from typing import Literal, TypedDict, Unpack

import http_sf
import pytest

from iab_agentic_primitives.web_bot_auth import (
    CONFORMANCE_KEYS,
    DIRECTORY_PATH,
    Ed25519Key,
    HeaderLines,
    HTTPRequest,
    KeyPolicy,
    MemoryNonceStore,
    NonceStoreUnavailable,
    Origin,
    Refusal,
    RequestSigner,
    RequestVerifier,
    ResponseSigner,
    Seconds,
    ServerIdentity,
    SigningError,
    StaticKeyResolver,
    UnixSeconds,
    WebBotAuthError,
    accept_signature,
)
from iab_agentic_primitives.web_bot_auth.conformance import scenarios_document, vectors
from iab_agentic_primitives.web_bot_auth.conformance.models import NonceSpec, SignatureParameters
from iab_agentic_primitives.web_bot_auth.conformance.signing import (
    MessageDeparture,
    sign_request_scenario,
)
from iab_agentic_primitives.web_bot_auth.rules import CONTENT_DIGEST, signature_agent_component
from iab_agentic_primitives.web_bot_auth.sync import SyncRequestVerifier, SyncRuntime

from . import support as w
from .support import (
    BUYER,
    BUYER_ORIGIN,
    COMMIT_BODY,
    COMMIT_URL,
    JSON,
    OTHER,
    SELLER_IDENTITY,
    T0,
    at,
    b64url,
    b64url_bytes,
    craft,
    expect_refusal,
    verify,
)
from .support import digest as content_digest

KEYS_PATH = DIRECTORY_PATH

# Nonces the profile refuses: 64 bytes is the only length, in base64url or padded base64.
BAD_NONCES = [
    "short",
    "not base64!",
    b64url(bytes(32)),
    b64url(bytes(65)),
    base64.urlsafe_b64encode(b"\xfb" * 64).decode(),  # padded, with base64url's - and _
    base64.urlsafe_b64encode(bytes(range(64)) + b"\xfb").decode()[:-4],  # 66 bytes, cut
]

# ── signing ───────────────────────────────────────────────────────────────


def test_signer_adds_the_four_headers_and_covers_the_profile_components(
    buyer_key: Ed25519Key,
) -> None:
    added = w.request_added(
        RequestSigner.of(buyer_key, BUYER), "POST", COMMIT_URL, JSON, COMMIT_BODY, now=T0
    )
    assert list(added) == ["Content-Digest", "Signature-Agent", "Signature-Input", "Signature"]
    assert added["Signature-Agent"] == f'sig1="{BUYER}{KEYS_PATH}"'
    params = added["Signature-Input"]
    # The committed vector is the oracle: the generator signed it at the same components.
    golden = w.POSITIVE_001.headers["Signature-Input"]
    assert params.startswith(golden.split(";created")[0] + ";")
    for part in (
        f"created={T0}",
        f"expires={at(300)}",
        f'keyid="{buyer_key.thumbprint}"',
        'alg="ed25519"',
        'tag="web-bot-auth"',
        "nonce=",
    ):
        assert part in params


def test_signer_without_a_body_adds_no_digest(buyer_key: Ed25519Key) -> None:
    added = w.request_added(
        RequestSigner.of(buyer_key, BUYER), "GET", f"{w.SELLER}/proposals", now=T0
    )
    assert "Content-Digest" not in added
    for component in w.BODY_COMPONENTS:
        assert str(component) not in added["Signature-Input"]


def test_signer_is_deterministic_for_a_fixed_nonce(buyer_key: Ed25519Key) -> None:
    signer = RequestSigner.of(buyer_key, BUYER)
    nonce = "A" * 86
    one = w.request_added(signer, "POST", COMMIT_URL, JSON, COMMIT_BODY, now=T0, nonce=nonce)
    two = w.request_added(signer, "POST", COMMIT_URL, JSON, COMMIT_BODY, now=T0, nonce=nonce)
    assert one == two


def test_signer_reuses_a_matching_digest_and_refuses_a_wrong_one(buyer_key: Ed25519Key) -> None:
    signer = RequestSigner.of(buyer_key, BUYER)
    digest = content_digest(COMMIT_BODY)
    added = w.request_added(
        signer, "POST", COMMIT_URL, {**JSON, "Content-Digest": digest}, COMMIT_BODY, now=T0
    )
    assert "Content-Digest" not in added
    with pytest.raises(ValueError, match="Content-Digest"):
        w.request_added(
            signer,
            "POST",
            COMMIT_URL,
            {**JSON, "Content-Digest": content_digest(b"x")},
            COMMIT_BODY,
        )


@pytest.mark.parametrize("existing", ["Signature", "Signature-Input", "Signature-Agent"])
def test_signer_signs_once_per_message(buyer_key: Ed25519Key, existing: str) -> None:
    with pytest.raises(ValueError, match="once per message"):
        w.request_added(
            RequestSigner.of(buyer_key, BUYER),
            "POST",
            COMMIT_URL,
            {**JSON, existing: "x"},
            COMMIT_BODY,
        )


def test_signer_needs_content_type_with_a_body(buyer_key: Ed25519Key) -> None:
    with pytest.raises(ValueError, match="Content-Type"):
        w.request_added(RequestSigner.of(buyer_key, BUYER), "POST", COMMIT_URL, {}, COMMIT_BODY)


@pytest.mark.parametrize("nonce", BAD_NONCES)
def test_signer_refuses_a_nonce_the_profile_does_not_allow(
    buyer_key: Ed25519Key, nonce: str
) -> None:
    with pytest.raises(SigningError, match="nonce"):
        w.request_added(RequestSigner.of(buyer_key, BUYER), "GET", COMMIT_URL, nonce=nonce)


def test_each_signing_draws_a_fresh_64_byte_nonce(
    verifier: RequestVerifier, buyer_key: Ed25519Key
) -> None:
    one, two = (verify(verifier, w.signed_commit(buyer_key)) for _ in range(2))
    assert one.nonce != two.nonce
    assert "=" not in one.nonce and len(b64url_bytes(one.nonce)) == 64


# ── verification: the happy path ──────────────────────────────────────────


def test_signed_commit_verifies(verifier: RequestVerifier, buyer_key: Ed25519Key) -> None:
    result = verify(verifier, w.signed_commit(buyer_key))
    assert result.origin == BUYER_ORIGIN
    assert result.keyid == buyer_key.thumbprint
    assert result.label == "sig1"
    assert (result.created, result.expires) == (T0, at(300))
    assert result.body_covered is True
    assert signature_agent_component("sig1") in result.components
    assert CONTENT_DIGEST in result.components


def test_signed_get_verifies_without_body_coverage(
    verifier: RequestVerifier, buyer_key: Ed25519Key
) -> None:
    url = f"{w.SELLER}/proposals/prop_7?view=a%7Eb&x=1"
    added = w.request_added(RequestSigner.of(buyer_key, BUYER), "GET", url, now=T0)
    result = verify(verifier, HTTPRequest("GET", url, added))
    assert result.body_covered is False


def test_verify_sync(verifier: RequestVerifier, buyer_key: Ed25519Key) -> None:
    r = w.signed_commit(buyer_key)
    with SyncRuntime() as runtime:
        assert SyncRequestVerifier(verifier, runtime=runtime).verify(r).origin == BUYER_ORIGIN


def test_header_names_are_case_insensitive_and_lines_combine(
    verifier: RequestVerifier, buyer_key: Ed25519Key
) -> None:
    r = w.signed_commit(buyer_key)
    lines = [(k.lower(), v) for k, v in r.headers]
    assert verify(verifier, replace(r, headers=HeaderLines(lines))).origin == BUYER_ORIGIN


@pytest.mark.parametrize("label", ["wba", "a1"])
def test_any_label(verifier: RequestVerifier, buyer_key: Ed25519Key, label: str) -> None:
    assert verify(verifier, craft(buyer_key, label=label)).label == label


def test_components_in_another_order(verifier: RequestVerifier, buyer_key: Ed25519Key) -> None:
    components = list(reversed(w.post_components()))
    assert verify(verifier, craft(buyer_key, components=components)).origin == BUYER_ORIGIN


@pytest.mark.parametrize("encoding", ["base64url", "base64"])
def test_both_nonce_encodings(
    verifier: RequestVerifier, buyer_key: Ed25519Key, encoding: Literal["base64url", "base64"]
) -> None:
    nonce = SignatureParameters(nonce=NonceSpec(bytes=64, encoding=encoding))
    assert verify(verifier, craft(buyer_key, params=nonce)).origin == BUYER_ORIGIN


@pytest.mark.parametrize("value", [f"{BUYER}{KEYS_PATH}", BUYER])
def test_key_url_and_bare_origin_forms_name_the_same_identity(
    verifier: RequestVerifier, buyer_key: Ed25519Key, value: str
) -> None:
    result = verify(verifier, craft(buyer_key, agent=f'sig1="{value}"'))
    assert (result.origin, result.origin.host) == (BUYER_ORIGIN, "buyer.example.com")


def test_a_second_signature_with_another_tag_is_not_read(
    verifier: RequestVerifier, buyer_key: Ed25519Key, other_key: Ed25519Key
) -> None:
    scenarios = scenarios_document()
    s = next(s for s in scenarios.request_scenarios if s.id == "R20")
    keys: dict[str, Ed25519Key] = {buyer_key.thumbprint: buyer_key, other_key.thumbprint: other_key}
    request = sign_request_scenario(scenarios, s, keys, T0)
    assert w.run(verifier.verify(request)).origin == BUYER_ORIGIN


# ── the window and the clock skew ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("created", "expires", "refusal"),
    [
        (60, 300, None),
        (61, 300, Refusal.CREATED_IN_FUTURE),
        (-360, -60, None),
        (-361, -61, Refusal.EXPIRED),
        (-5, 295, None),
        (0, 301, Refusal.LIFETIME),
        (10, 5, Refusal.WINDOW),
    ],
)
def test_window(
    verifier: RequestVerifier,
    buyer_key: Ed25519Key,
    created: int,
    expires: int,
    refusal: Refusal | None,
) -> None:
    request = craft(buyer_key, params=SignatureParameters(created=created, expires=expires))
    if refusal is None:
        assert verify(verifier, request).origin == BUYER_ORIGIN
    else:
        expect_refusal(lambda: w.verify(verifier, request), refusal)


def test_created_is_required(verifier: RequestVerifier, buyer_key: Ed25519Key) -> None:
    request = w.signed_commit(buyer_key)
    signature_input = request.headers["Signature-Input"].replace(f";created={T0}", "")
    request = w.edited(request, headers={"Signature-Input": signature_input})
    expect_refusal(lambda: w.verify(verifier, request), Refusal.WINDOW)


def test_expires_is_required(verifier: RequestVerifier, buyer_key: Ed25519Key) -> None:
    expect_refusal(
        lambda: w.verify(verifier, craft(buyer_key, params=SignatureParameters(expires=None))),
        Refusal.WINDOW,
    )


# ── refusals: 400 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("Signature-Agent", f'sig1="{BUYER}'),
        ("Signature-Agent", f'"{BUYER}"'),
        ("Signature-Input", "sig1=("),
        ("Signature", "sig1=:not base64:"),
        ("Signature", "sig1=token"),
        ("Signature-Input", 'sig1="string"'),
        ("Signature-Agent", 'sig1="hé"'),
    ],
)
def test_unparseable_fields_get_400(
    verifier: RequestVerifier, buyer_key: Ed25519Key, field: str, value: str
) -> None:
    request = w.edited(w.signed_commit(buyer_key), headers={field: value})
    expect_refusal(lambda: w.verify(verifier, request), Refusal.PARSE)


def test_a_malformed_member_of_any_tag_is_a_400(
    verifier: RequestVerifier, buyer_key: Ed25519Key
) -> None:
    """RFC 9421 sections 4.1 and 4.2 make every member's shape normative, whatever its tag.

    Members with other tags aren't read, but a Signature member that isn't a
    Byte Sequence makes the field malformed, so the request gets 400.
    """
    request = w.signed_commit(buyer_key)
    cdn = f', cdn={w.inner_list([])};tag="cdn-edge"'
    request = w.edited(
        request,
        headers={
            "Signature-Input": request.headers["Signature-Input"] + cdn,
            "Signature": request.headers["Signature"] + ", cdn=edge-token",
        },
    )
    expect_refusal(lambda: w.verify(verifier, request), Refusal.PARSE)


# ── refusals: 403 ─────────────────────────────────────────────────────────


def test_unsigned_request(verifier: RequestVerifier) -> None:
    expect_refusal(
        lambda: w.verify(verifier, HTTPRequest("GET", COMMIT_URL)),
        Refusal.SIGNATURE_MISSING,
    )


def test_no_web_bot_auth_tag(verifier: RequestVerifier, buyer_key: Ed25519Key) -> None:
    expect_refusal(
        lambda: w.verify(
            verifier, craft(buyer_key, params=SignatureParameters(tag="other-profile/v1"))
        ),
        Refusal.SIGNATURE_MISSING,
    )


def test_two_web_bot_auth_signatures(verifier: RequestVerifier) -> None:
    request = w.vector_request(dict(vectors())["negative/006-two-signatures"]).message()
    expect_refusal(lambda: w.verify(verifier, request), Refusal.MULTIPLE_SIGNATURES)


def test_signature_member_missing_for_the_label(
    verifier: RequestVerifier, buyer_key: Ed25519Key
) -> None:
    request = w.signed_commit(buyer_key)
    signature = request.headers["Signature"].replace("sig1=", "other=")
    request = w.edited(request, headers={"Signature": signature})
    expect_refusal(lambda: w.verify(verifier, request), Refusal.SIGNATURE_MISSING)


def test_no_signature_agent(verifier: RequestVerifier, buyer_key: Ed25519Key) -> None:
    request = w.edited(w.signed_commit(buyer_key), headers={"Signature-Agent": None})
    expect_refusal(lambda: w.verify(verifier, request), Refusal.SIGNATURE_AGENT_MISSING)


def test_two_signature_agent_members(verifier: RequestVerifier, buyer_key: Ed25519Key) -> None:
    request = craft(buyer_key, agent=f'sig1="{BUYER}", sig2="{OTHER}"')
    expect_refusal(lambda: w.verify(verifier, request), Refusal.MULTIPLE_SIGNATURE_AGENTS)


def test_member_keyed_differently_from_the_label(
    verifier: RequestVerifier, buyer_key: Ed25519Key
) -> None:
    components = w.post_components("agent1")
    request = craft(buyer_key, agent=f'agent1="{BUYER}"', components=components)
    expect_refusal(lambda: w.verify(verifier, request), Refusal.SIGNATURE_AGENT_MISSING)


def test_member_not_covered(verifier: RequestVerifier, buyer_key: Ed25519Key) -> None:
    components = w.post_components()[:-1]
    expect_refusal(
        lambda: w.verify(verifier, craft(buyer_key, components=components)),
        Refusal.SIGNATURE_AGENT_NOT_COVERED,
    )


def test_whole_field_covered_instead_of_the_member(
    verifier: RequestVerifier, buyer_key: Ed25519Key
) -> None:
    agent = signature_agent_component("sig1")
    components = [*w.post_components()[:-1], agent.name]
    expect_refusal(
        lambda: w.verify(verifier, craft(buyer_key, components=components)),
        Refusal.SIGNATURE_AGENT_NOT_COVERED,
    )


@pytest.mark.parametrize(
    "member",
    [
        f'sig1="{BUYER}/jwks.json";type=jwks_uri',
        f'sig1="{BUYER}";type=directory',
        f'sig1="{BUYER}{KEYS_PATH}";type="directory"',
        f'sig1="{BUYER}";type=cimd',
        f'sig1="{BUYER}";x',
    ],
)
def test_a_member_with_parameters_is_ignored(
    verifier: RequestVerifier, buyer_key: Ed25519Key, member: str
) -> None:
    expect_refusal(
        lambda: w.verify(verifier, craft(buyer_key, agent=member)),
        Refusal.SIGNATURE_AGENT_PARAMETERS,
    )


def test_an_ignored_member_does_not_count(verifier: RequestVerifier, buyer_key: Ed25519Key) -> None:
    agent = f'sig1="{BUYER}", other="{OTHER}";type=directory'
    assert verify(verifier, craft(buyer_key, agent=agent)).origin == BUYER_ORIGIN


@pytest.mark.parametrize(
    "value",
    [
        f"http://buyer.example.com{KEYS_PATH}",
        "http://buyer.example.com",
        f"{BUYER}/",
        f"{BUYER}/jwks.json",
        f"{BUYER}{KEYS_PATH}/",
        f"{BUYER}{KEYS_PATH}?v=1",
        f"{BUYER}{KEYS_PATH}#k",
        f"{BUYER}{KEYS_PATH.upper()}",
        f"https://BUYER.example.com{KEYS_PATH}",
        f"{BUYER}:443{KEYS_PATH}",
        f"{BUYER}:8443",
        f"https://agent@buyer.example.com{KEYS_PATH}",
        "https://agent@buyer.example.com",
        "HTTPS://buyer.example.com",
        f"HTTPS://buyer.example.com{KEYS_PATH}",
        "https://",
        "buyer.example.com",
        f"{BUYER}?v=1",
        f"{BUYER}//.well-known/http-message-signatures-directory",
        f"{BUYER}{KEYS_PATH}.json",
    ],
)
def test_signature_agent_value_rule(
    verifier: RequestVerifier, buyer_key: Ed25519Key, value: str
) -> None:
    expect_refusal(
        lambda: w.verify(verifier, craft(buyer_key, agent=f'sig1="{value}"')),
        Refusal.SIGNATURE_AGENT_VALUE,
    )


def test_member_value_that_is_not_a_string(
    verifier: RequestVerifier, buyer_key: Ed25519Key
) -> None:
    expect_refusal(
        lambda: w.verify(verifier, craft(buyer_key, agent="sig1=buyer")),
        Refusal.SIGNATURE_AGENT_VALUE,
    )


@pytest.mark.parametrize("alg", [None, "ecdsa-p256-sha256", "rsa-pss-sha512"])
def test_alg(verifier: RequestVerifier, buyer_key: Ed25519Key, alg: str | None) -> None:
    expect_refusal(
        lambda: w.verify(verifier, craft(buyer_key, params=SignatureParameters(alg=alg))),
        Refusal.ALG,
    )


def test_keyid_missing(verifier: RequestVerifier, buyer_key: Ed25519Key) -> None:
    expect_refusal(
        lambda: w.verify(verifier, craft(buyer_key, params=SignatureParameters(keyid=""))),
        Refusal.KEYID,
    )


def test_a_request_without_a_nonce_is_refused(
    verifier: RequestVerifier, buyer_key: Ed25519Key
) -> None:
    signed = craft(buyer_key, params=SignatureParameters(nonce=None))
    expect_refusal(lambda: w.verify(verifier, signed), Refusal.NONCE_MISSING)


@pytest.mark.parametrize(
    "nonce",
    [
        NonceSpec(bytes=32, encoding="base64url"),
        NonceSpec(bytes=64, encoding="base64url-padded"),
        *BAD_NONCES,
    ],
)
def test_a_nonce_the_profile_does_not_allow_is_refused(
    verifier: RequestVerifier, buyer_key: Ed25519Key, nonce: NonceSpec | str
) -> None:
    signed = craft(buyer_key, params=SignatureParameters(nonce=nonce))
    expect_refusal(lambda: w.verify(verifier, signed), Refusal.NONCE_INVALID)


@pytest.mark.parametrize("dropped", w.post_components()[:-1])
def test_each_required_component(
    verifier: RequestVerifier, buyer_key: Ed25519Key, dropped: str
) -> None:
    components = [c for c in w.POST_COMPONENTS if c != dropped]
    expect_refusal(
        lambda: w.verify(verifier, craft(buyer_key, components=components)), Refusal.COMPONENTS
    )


def test_get_needs_method_and_target_uri(verifier: RequestVerifier, buyer_key: Ed25519Key) -> None:
    request = craft(buyer_key, method="GET", body=b"", components=w.get_components()[2:])
    expect_refusal(lambda: w.verify(verifier, request), Refusal.COMPONENTS)


def test_authority_with_parameters_does_not_count(
    verifier: RequestVerifier, buyer_key: Ed25519Key
) -> None:
    authority = w.get_components()[2]
    components = ["@query-param;name=x" if c == authority else c for c in w.get_components()]
    url = f"{COMMIT_URL}?x=1"
    request = craft(buyer_key, method="GET", url=url, body=b"", components=components)
    expect_refusal(lambda: w.verify(verifier, request), Refusal.COMPONENTS)


def test_body_changed_after_signing(verifier: RequestVerifier, buyer_key: Ed25519Key) -> None:
    request = w.signed_commit(buyer_key)
    request = w.edited(request, body=request.body + b"\n")
    expect_refusal(lambda: w.verify(verifier, request), Refusal.CONTENT_DIGEST)


def test_body_added_to_a_signed_get(verifier: RequestVerifier, buyer_key: Ed25519Key) -> None:
    url = f"{w.SELLER}/proposals"
    added = w.request_added(RequestSigner.of(buyer_key, BUYER), "GET", url, now=T0)
    expect_refusal(
        lambda: w.verify(verifier, HTTPRequest("GET", url, added, b"{}")),
        Refusal.COMPONENTS,
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda r: w.edited(r, headers={"Content-Type": "text/plain"}),
        lambda r: w.edited(r, url=COMMIT_URL + "?x=1"),
        lambda r: w.edited(r, url=COMMIT_URL.replace("seller", "other")),
        lambda r: replace(r, method="PUT"),
    ],
    ids=["content-type", "target-uri", "authority", "method"],
)
def test_covered_value_changed_after_signing(
    buyer_key: Ed25519Key, change: Callable[[HTTPRequest], HTTPRequest]
) -> None:
    # The verifier is the server the changed request names, so a changed @authority
    # passes the authority check and reaches the signature check.
    request = change(w.signed_commit(buyer_key))
    verifier = RequestVerifier(
        StaticKeyResolver({BUYER: [buyer_key]}),
        identity=ServerIdentity.from_config(str(Origin.of_url(request.url))),
        nonces=MemoryNonceStore(clock=lambda: T0),
        clock=lambda: T0,
        keys=CONFORMANCE_KEYS,
    )
    expect_refusal(lambda: w.verify(verifier, request), Refusal.SIGNATURE_INVALID)


def test_a_parameter_appended_after_signing_is_tampering(
    verifier: RequestVerifier, buyer_key: Ed25519Key
) -> None:
    request = w.signed_commit(buyer_key)
    extra = request.headers["Signature-Input"] + ";extra=1"
    request = w.edited(request, headers={"Signature-Input": extra})
    expect_refusal(lambda: w.verify(verifier, request), Refusal.SIGNATURE_INVALID)


def test_key_not_in_the_named_directory(verifier: RequestVerifier, buyer_key: Ed25519Key) -> None:
    request = w.signed_commit(buyer_key, origin=OTHER)
    expect_refusal(lambda: w.verify(verifier, request), Refusal.KEY_NOT_FOUND)


def test_key_lookup_is_by_origin_and_keyid(buyer_key: Ed25519Key) -> None:
    # The key is known, but only under another origin: a lookup by keyid alone would accept.
    resolver = StaticKeyResolver({OTHER: [buyer_key], BUYER: []})
    verifier = RequestVerifier(
        resolver,
        identity=SELLER_IDENTITY,
        nonces=MemoryNonceStore(clock=lambda: T0),
        clock=lambda: T0,
        keys=CONFORMANCE_KEYS,
    )
    expect_refusal(lambda: w.verify(verifier, w.signed_commit(buyer_key)), Refusal.KEY_NOT_FOUND)


def test_rejected_keyids(buyer_key: Ed25519Key) -> None:
    resolver = StaticKeyResolver({BUYER: [buyer_key]})
    verifier = RequestVerifier(
        resolver,
        identity=SELLER_IDENTITY,
        nonces=MemoryNonceStore(clock=lambda: T0),
        clock=lambda: T0,
        keys=KeyPolicy(reject_keyids={buyer_key.thumbprint}),
    )
    expect_refusal(lambda: w.verify(verifier, w.signed_commit(buyer_key)), Refusal.REJECTED_KEY)


# ── refusals: 429 ─────────────────────────────────────────────────────────


def test_replayed_nonce_gets_429(verifier: RequestVerifier, buyer_key: Ed25519Key) -> None:
    request = w.signed_commit(buyer_key)
    verify(verifier, request)
    expect_refusal(lambda: w.verify(verifier, request), Refusal.NONCE_REPLAYED)


def test_replay_is_per_origin_and_the_ttl_covers_the_window(buyer_key: Ed25519Key) -> None:
    stored: dict[tuple[Origin, str], Seconds] = {}

    class Recording(MemoryNonceStore):
        async def check_and_record(self, origin: Origin, nonce: str, ttl: Seconds) -> bool:
            stored[(origin, nonce)] = ttl
            return await super().check_and_record(origin, nonce, ttl)

    verifier = RequestVerifier(
        StaticKeyResolver({BUYER: [buyer_key]}),
        identity=SELLER_IDENTITY,
        nonces=Recording(clock=lambda: at(10)),
        keys=CONFORMANCE_KEYS,
    )
    verify(verifier, w.signed_commit(buyer_key), now=at(10))
    (ttl,) = stored.values()
    assert ttl == 300 - 10 + 60


def test_a_failed_verification_records_no_nonce(buyer_key: Ed25519Key) -> None:
    store = MemoryNonceStore(clock=lambda: T0)
    verifier = RequestVerifier(
        StaticKeyResolver({BUYER: [buyer_key]}),
        identity=SELLER_IDENTITY,
        nonces=store,
        clock=lambda: T0,
        keys=CONFORMANCE_KEYS,
    )
    request = w.signed_commit(buyer_key)
    tampered = w.edited(request, body=request.body + b" ")
    expect_refusal(lambda: w.verify(verifier, tampered), Refusal.CONTENT_DIGEST)
    assert len(store) == 0
    assert verify(verifier, request).origin == BUYER_ORIGIN


@pytest.mark.parametrize("has_body", [True, False], ids=["post", "get"])
def test_a_request_signed_as_accept_signature_asks_verifies(
    verifier: RequestVerifier, buyer_key: Ed25519Key, has_body: bool
) -> None:
    parsed = http_sf.parse(accept_signature(has_body=has_body).encode(), tltype="dictionary")
    assert isinstance(parsed, dict)
    ((label, member),) = parsed.items()
    assert isinstance(member, tuple)
    inner, parameters = member
    assert isinstance(inner, list)
    assert parameters == {
        "created": True,
        "expires": True,
        "nonce": True,
        "keyid": True,
        "alg": "ed25519",
        "tag": "web-bot-auth",
    }
    components = []
    for item in inner:
        assert isinstance(item, tuple)
        name, ps = item
        assert isinstance(name, str)
        components.append(name + "".join(f";{k}={v!s}" for k, v in ps.items()))
    body = COMMIT_BODY if has_body else b""
    method = "POST" if has_body else "GET"
    request = craft(buyer_key, method=method, body=body, label=label, components=components)
    assert verify(verifier, request).body_covered is has_body


# ── review regressions ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("field", "refusal"),
    [
        ("Signature-Agent", Refusal.MULTIPLE_SIGNATURE_AGENTS),
        ("Signature-Input", Refusal.MULTIPLE_SIGNATURES),
        ("Signature", Refusal.MULTIPLE_SIGNATURES),
    ],
)
def test_a_repeated_member_gets_403_not_400(
    verifier: RequestVerifier, buyer_key: Ed25519Key, field: str, refusal: Refusal
) -> None:
    request = w.signed_commit(buyer_key)
    value = request.headers[field]
    request = w.edited(request, headers={field: f"{value}, {value}"})
    expect_refusal(lambda: w.verify(verifier, request), refusal)


def test_a_duplicate_parameter_is_still_a_parse_failure(
    verifier: RequestVerifier, buyer_key: Ed25519Key
) -> None:
    request = w.signed_commit(buyer_key)
    tagged = request.headers["Signature-Input"] + ';tag="web-bot-auth"'
    request = w.edited(request, headers={"Signature-Input": tagged})
    expect_refusal(lambda: w.verify(verifier, request), Refusal.PARSE)


def test_a_request_digest_without_sha_256_is_refused(
    verifier: RequestVerifier, buyer_key: Ed25519Key
) -> None:
    unchecked = MessageDeparture(
        content_digest=w.digest_without_sha256(COMMIT_BODY), signature_agent=f'sig1="{BUYER}"'
    )
    signed = w.sign_request(
        HTTPRequest("POST", COMMIT_URL, JSON, COMMIT_BODY), now=T0, departure=unchecked
    )
    expect_refusal(lambda: w.verify(verifier, signed), Refusal.CONTENT_DIGEST)


def test_signers_refuse_a_supplied_digest_without_sha_256(
    buyer_key: Ed25519Key, seller_key: Ed25519Key
) -> None:
    headers = {**JSON, "Content-Digest": w.digest_without_sha256(COMMIT_BODY)}
    with pytest.raises(ValueError, match="sha-256"):
        w.request_added(
            RequestSigner.of(buyer_key, BUYER), "POST", COMMIT_URL, headers, COMMIT_BODY, now=T0
        )
    with pytest.raises(ValueError, match="sha-256"):
        w.response_added(
            ResponseSigner.of(seller_key, w.SELLER),
            200,
            headers,
            COMMIT_BODY,
            HTTPRequest("GET", COMMIT_URL, {}),
            now=T0,
        )
    both = {
        **JSON,
        "Content-Digest": f"{w.digest_without_sha256(COMMIT_BODY)}, {content_digest(COMMIT_BODY)}",
    }
    assert "Content-Digest" not in w.request_added(
        RequestSigner.of(buyer_key, BUYER), "POST", COMMIT_URL, both, COMMIT_BODY
    )


class StoreOptions(TypedDict, total=False):
    """The :class:`MemoryNonceStore` options a test sets."""

    max_entries: int


def _clocked_verifier(
    buyer_key: Ed25519Key, now: list[UnixSeconds], **store: Unpack[StoreOptions]
) -> RequestVerifier:
    """A verifier whose clock and nonce store read ``now[0]``."""
    return RequestVerifier(
        StaticKeyResolver({BUYER: [buyer_key]}),
        identity=SELLER_IDENTITY,
        nonces=MemoryNonceStore(clock=lambda: now[0], **store),
        clock=lambda: now[0],
        keys=CONFORMANCE_KEYS,
    )


def test_a_full_nonce_store_answers_503_not_429_until_its_nonces_expire(
    buyer_key: Ed25519Key,
) -> None:
    now = [T0]
    verifier = _clocked_verifier(buyer_key, now, max_entries=1)
    first = w.signed_commit(buyer_key)
    verify(verifier, first)
    again = w.signed_commit(buyer_key)
    expect_refusal(lambda: w.verify(verifier, again), Refusal.NONCE_STORE_UNAVAILABLE)
    expect_refusal(lambda: w.verify(verifier, first), Refusal.NONCE_REPLAYED)
    # Once the first nonce's window and skew have passed, its space is free again.
    now[0] = at(300 + 60 + 1)
    assert verify(verifier, w.signed_commit(buyer_key, now=now[0])).origin == BUYER_ORIGIN


def test_a_replay_in_the_last_second_the_window_allows_is_still_a_replay(
    buyer_key: Ed25519Key,
) -> None:
    now = [T0]
    verifier = _clocked_verifier(buyer_key, now)
    request = w.signed_commit(buyer_key)
    verify(verifier, request)
    now[0] = at(300 + 60)  # expires, plus the skew: the last second the request is good
    expect_refusal(lambda: w.verify(verifier, request), Refusal.NONCE_REPLAYED)
    now[0] = UnixSeconds(now[0] + 1)
    expect_refusal(lambda: w.verify(verifier, request), Refusal.EXPIRED)


def test_concurrent_verifications_of_one_request_accept_it_once(buyer_key: Ed25519Key) -> None:
    # Sixteen threads verify through one runtime, as threaded code shares a resolver.
    verifier = _clocked_verifier(buyer_key, [T0])
    request = w.signed_commit(buyer_key)
    barrier = threading.Barrier(16)
    outcomes: list[Origin | Refusal] = []
    with SyncRuntime() as runtime:
        sync = SyncRequestVerifier(verifier, runtime=runtime)

        def attempt() -> None:
            barrier.wait()
            try:
                outcomes.append(sync.verify(request).origin)
            except WebBotAuthError as e:
                outcomes.append(e.refusal)

        threads = [threading.Thread(target=attempt) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert outcomes.count(BUYER_ORIGIN) == 1
    assert outcomes.count(Refusal.NONCE_REPLAYED) == 15


def test_a_failing_nonce_store_answers_503(buyer_key: Ed25519Key) -> None:
    class Down:
        async def check_and_record(self, origin: Origin, nonce: str, ttl: Seconds) -> bool:
            raise NonceStoreUnavailable("redis is down")

    verifier = RequestVerifier(
        StaticKeyResolver({BUYER: [buyer_key]}),
        identity=SELLER_IDENTITY,
        nonces=Down(),
        clock=lambda: T0,
        keys=CONFORMANCE_KEYS,
    )
    request = w.signed_commit(buyer_key)
    expect_refusal(lambda: w.verify(verifier, request), Refusal.NONCE_STORE_UNAVAILABLE)


@pytest.mark.parametrize(
    "host", ["2130706433", "127.1", "1.2.3.04", "0x7f.0.0.1", "a.0x10", "[fe80::1%25en0]"]
)
def test_number_shaped_hosts_are_not_identities(
    verifier: RequestVerifier, buyer_key: Ed25519Key, host: str
) -> None:
    request = craft(buyer_key, agent=f'sig1="https://{host}{KEYS_PATH}"')
    expect_refusal(lambda: w.verify(verifier, request), Refusal.SIGNATURE_AGENT_VALUE)


def test_a_nonce_store_bug_propagates_instead_of_answering_503(buyer_key: Ed25519Key) -> None:
    class Broken:
        async def check_and_record(self, origin: Origin, nonce: str, ttl: Seconds) -> bool:
            raise ConnectionError("not a NonceStoreUnavailable")

    verifier = RequestVerifier(
        StaticKeyResolver({BUYER: [buyer_key]}),
        identity=SELLER_IDENTITY,
        nonces=Broken(),
        clock=lambda: T0,
        keys=CONFORMANCE_KEYS,
    )
    with pytest.raises(ConnectionError):
        verify(verifier, w.signed_commit(buyer_key))
