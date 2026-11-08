"""The role core: keyword-only, immutable configuration, two owned credentials,
per-origin replay, the reject list on both roles, and the narrow nonce store."""

from __future__ import annotations

import asyncio
import dataclasses

import httpx
import pytest

from iab_agentic_primitives.web_bot_auth import (
    CONFORMANCE_KEYS,
    DirectoryResolver,
    Ed25519Key,
    FetchLimits,
    HTTPRequest,
    HTTPResponse,
    KeyPolicy,
    MemoryNonceStore,
    Origin,
    RequestSigner,
    RequestVerifier,
    ResolvedKey,
    ResponseSigner,
    ResponseVerifier,
    Seconds,
    ServerIdentity,
    StaticKeyResolver,
    UnixSeconds,
    VerifiedRequest,
    VerifiedResponse,
)
from iab_agentic_primitives.web_bot_auth.asgi import (
    Receive,
    RequireSignatureMiddleware,
    ResponseSigningMiddleware,
    Scope,
    Send,
    verified_request,
)
from iab_agentic_primitives.web_bot_auth.conformance import published_keys
from iab_agentic_primitives.web_bot_auth.errors import Refusal
from iab_agentic_primitives.web_bot_auth.httpx import WebBotAuth, verified_response
from iab_agentic_primitives.web_bot_auth.signer import SignerConfig
from iab_agentic_primitives.web_bot_auth.sync import SyncRequestVerifier, SyncRuntime
from iab_agentic_primitives.web_bot_auth.values import KeyId

from .support import (
    BUYER,
    BUYER_ORIGIN,
    COMMIT_RESULT_BODY,
    COMMIT_URL,
    JSON,
    OTHER,
    SELLER,
    SELLER_IDENTITY,
    T0,
    buyer_key,
    expect_refusal,
    other_key,
    seller_edge,
    seller_key,
    sign_request,
)


class RecordingStore(MemoryNonceStore):
    """A memory store that keeps every pair it was asked to record."""

    def __init__(self) -> None:
        super().__init__(clock=lambda: T0)
        self.asked: list[tuple[Origin, str]] = []

    async def check_and_record(self, origin: Origin, nonce: str, ttl: Seconds) -> bool:
        self.asked.append((origin, nonce))
        return await super().check_and_record(origin, nonce, ttl)


def _verifier(nonces: MemoryNonceStore, **keys: list[Ed25519Key]) -> RequestVerifier:
    return RequestVerifier(
        StaticKeyResolver({BUYER: keys.get("buyer", []), OTHER: keys.get("other", [])}),
        identity=SELLER_IDENTITY,
        nonces=nonces,
        clock=lambda: T0,
        keys=CONFORMANCE_KEYS,
    )


def _signed(key: Ed25519Key, origin: str, nonce: str) -> HTTPRequest:
    return RequestSigner.of(key, origin).sign(HTTPRequest("GET", COMMIT_URL), now=T0, nonce=nonce)


NONCE = "A" * 86


# ── per-origin replay ─────────────────────────────────────────────────────


def test_a_nonce_is_recorded_per_origin() -> None:
    store = RecordingStore()
    verifier = _verifier(store, buyer=[buyer_key(), other_key()], other=[buyer_key()])
    asyncio.run(verifier.verify(_signed(buyer_key(), BUYER, NONCE)))
    assert store.asked == [(BUYER_ORIGIN, NONCE)]
    # The same nonce from another origin, with the same key, is a new pair.
    asyncio.run(verifier.verify(_signed(buyer_key(), OTHER, NONCE)))
    # The same nonce from the same origin, with another of its keys, is a replay.
    replayed = _signed(other_key(), BUYER, NONCE)
    expect_refusal(lambda: verifier.verify(replayed), Refusal.NONCE_REPLAYED)


# ── the reject list on both roles ─────────────────────────────────────────


def test_a_response_signed_by_a_rejected_key_is_refused() -> None:
    request = RequestSigner.of(buyer_key(), BUYER).sign(
        HTTPRequest("POST", COMMIT_URL, JSON, b"{}")
    )
    response = ResponseSigner.of(seller_key(), SELLER).sign(
        HTTPResponse(200, JSON, COMMIT_RESULT_BODY, request), now=T0
    )
    verifier = ResponseVerifier(
        StaticKeyResolver({SELLER: [seller_key()]}),
        clock=lambda: T0,
        keys=KeyPolicy(reject_keyids={seller_key().thumbprint}, accept_test_keys=True),
    )
    expect_refusal(lambda: verifier.verify(response), Refusal.REJECTED_KEY)


def test_a_request_signed_by_a_rejected_key_is_refused() -> None:
    verifier = RequestVerifier(
        StaticKeyResolver({BUYER: [buyer_key()]}),
        identity=SELLER_IDENTITY,
        nonces=MemoryNonceStore(clock=lambda: T0),
        clock=lambda: T0,
        keys=KeyPolicy(reject_keyids={buyer_key().thumbprint}, accept_test_keys=True),
    )
    rejected = _signed(buyer_key(), BUYER, NONCE)
    expect_refusal(lambda: verifier.verify(rejected), Refusal.REJECTED_KEY)


@pytest.mark.parametrize("kid", sorted(published_keys()))
def test_a_verifier_refuses_every_published_test_key_whatever_else_it_lists(kid: str) -> None:
    key, own = published_keys()[kid], Ed25519Key.generate()

    def verifier(keys: KeyPolicy) -> RequestVerifier:
        return RequestVerifier(
            StaticKeyResolver({BUYER: [key, own]}),
            identity=SELLER_IDENTITY,
            nonces=MemoryNonceStore(clock=lambda: T0),
            clock=lambda: T0,
            keys=keys,
        )

    def signed(by: Ed25519Key) -> HTTPRequest:
        return RequestSigner.of(by, BUYER).sign(HTTPRequest("GET", COMMIT_URL), now=T0)

    listing_own = verifier(KeyPolicy(reject_keyids={own.thumbprint}))
    expect_refusal(lambda: listing_own.verify(signed(key)), Refusal.REJECTED_KEY)
    expect_refusal(lambda: listing_own.verify(signed(own)), Refusal.REJECTED_KEY)
    default = RequestVerifier(
        StaticKeyResolver({BUYER: [key]}),
        identity=SELLER_IDENTITY,
        nonces=MemoryNonceStore(clock=lambda: T0),
        clock=lambda: T0,
    )
    expect_refusal(lambda: default.verify(signed(key)), Refusal.REJECTED_KEY)
    conformance = verifier(CONFORMANCE_KEYS)
    assert asyncio.run(conformance.verify(signed(key))).keyid == kid
    request = signed(buyer_key())
    response = ResponseSigner.of(key, SELLER).sign(
        HTTPResponse(200, JSON, COMMIT_RESULT_BODY, request), now=T0
    )
    responses = ResponseVerifier(
        StaticKeyResolver({SELLER: [key]}),
        clock=lambda: T0,
        keys=KeyPolicy(reject_keyids={own.thumbprint}),
    )
    expect_refusal(lambda: responses.verify(response), Refusal.REJECTED_KEY)


def test_a_default_seller_refuses_a_buyer_signing_with_a_test_key() -> None:
    verifier = RequestVerifier(
        StaticKeyResolver({BUYER: [buyer_key()]}),
        identity=SELLER_IDENTITY,
        nonces=MemoryNonceStore(),
    )
    edge = seller_edge(verifier)
    app = ResponseSigningMiddleware(RequireSignatureMiddleware(_ok, edge), edge)
    auth = WebBotAuth(RequestSigner.of(buyer_key(), BUYER))

    async def post() -> httpx.Response:
        async with auth.async_client(transport=httpx.ASGITransport(app=app)) as c:
            return await c.post(COMMIT_URL, json={})

    response = asyncio.run(post())
    assert response.status_code == 403 and response.json()["error"] == "rejected-key"


# ── the authority check ───────────────────────────────────────────────────


@pytest.mark.parametrize("url", ["/proposals/prop_7/commit", "seller.example.com/x", ""])
def test_a_request_url_that_is_not_absolute_is_refused(url: str) -> None:
    signed = _signed(buyer_key(), BUYER, NONCE)
    request = HTTPRequest("GET", url, signed.headers)
    verifier = _verifier(MemoryNonceStore(clock=lambda: T0), buyer=[buyer_key()])
    expect_refusal(lambda: verifier.verify(request), Refusal.AUTHORITY)


# ── keyword-only, immutable configuration ─────────────────────────────────


def test_verifier_arguments_after_the_resolver_are_keyword_only() -> None:
    resolver = StaticKeyResolver({})
    with pytest.raises(TypeError):
        RequestVerifier(resolver, lambda: T0, keys=CONFORMANCE_KEYS)  # type: ignore[arg-type, call-arg]
    with pytest.raises(TypeError):
        RequestVerifier(resolver, nonces=MemoryNonceStore(), keys=CONFORMANCE_KEYS)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        ResponseVerifier(resolver, MemoryNonceStore(), keys=CONFORMANCE_KEYS)  # type: ignore[arg-type, call-arg]


#: Setting a name that isn't a field of a slotted frozen dataclass raises
#: FrozenInstanceError from CPython 3.14 and TypeError before it.
NOT_A_FIELD = (dataclasses.FrozenInstanceError, TypeError)


def test_signers_their_config_and_keys_are_frozen() -> None:
    signer = RequestSigner.of(buyer_key(), BUYER)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ResponseSigner.of(seller_key(), SELLER).config = signer.config  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        signer.config.lifetime = 100_000  # type: ignore[misc]
    assert signer.config.lifetime == 300
    key = buyer_key()
    public = key.public_key
    with pytest.raises(NOT_A_FIELD):
        key.public_key = other_key().public_key  # type: ignore[misc]
    assert key.public_key is public
    assert not hasattr(key, "private_key") and not hasattr(key, "kid")


def test_signer_config_checks_its_values_once() -> None:
    config = SignerConfig(buyer_key(), BUYER_ORIGIN, label="b1", lifetime=60)
    assert (config.origin, config.label, config.lifetime) == (BUYER_ORIGIN, "b1", 60)
    with pytest.raises(ValueError):
        SignerConfig(buyer_key(), BUYER)  # type: ignore[arg-type]


# ── two owned credentials and their accessors ─────────────────────────────


def _needs_request(verified: VerifiedRequest) -> str:
    return verified.nonce


def test_the_two_credentials_are_distinct_types() -> None:
    verified = asyncio.run(
        _verifier(MemoryNonceStore(clock=lambda: T0), buyer=[buyer_key()]).verify(
            _signed(buyer_key(), BUYER, NONCE)
        )
    )
    assert _needs_request(verified) == NONCE
    request = RequestSigner.of(buyer_key(), BUYER).sign(HTTPRequest("GET", COMMIT_URL))
    response = asyncio.run(
        ResponseVerifier(
            StaticKeyResolver({SELLER: [seller_key()]}), clock=lambda: T0, keys=CONFORMANCE_KEYS
        ).verify(
            ResponseSigner.of(seller_key(), SELLER).sign(
                HTTPResponse(200, {}, b"", request), now=T0
            )
        )
    )
    assert isinstance(response, VerifiedResponse) and not isinstance(response, VerifiedRequest)
    with pytest.raises(AttributeError):
        _needs_request(response)  # type: ignore[arg-type]


def test_the_accessors_read_only_their_own_credential() -> None:
    verified = asyncio.run(
        _verifier(MemoryNonceStore(clock=lambda: T0), buyer=[buyer_key()]).verify(
            _signed(buyer_key(), BUYER, NONCE)
        )
    )
    assert verified.origin == BUYER_ORIGIN
    assert verified_request({}) is None
    assert verified_response(httpx.Response(200)) is None


def test_a_static_resolver_returns_a_resolved_key_without_a_directory() -> None:
    resolved = asyncio.run(
        StaticKeyResolver({BUYER: [buyer_key()]}).resolve(
            BUYER_ORIGIN, KeyId(buyer_key().thumbprint), now=T0
        )
    )
    assert isinstance(resolved, ResolvedKey) and resolved.directory is None
    assert resolved.public_key == buyer_key().public_key


# ── the role ports, and holders fixed when they are made ─────────────────


class AllowlistedVerifier:
    """An application's own confirmer: the profile's verifier, then its own origin list."""

    def __init__(self, verifier: RequestVerifier, allowed: frozenset[Origin]) -> None:
        self._verifier = verifier
        self._allowed = allowed

    @property
    def identity(self) -> ServerIdentity:
        return self._verifier.identity

    async def verify(
        self, request: HTTPRequest, *, now: UnixSeconds | None = None
    ) -> VerifiedRequest:
        verified = await self._verifier.verify(request, now=now)
        if verified.origin not in self._allowed:
            raise PermissionError(f"{verified.origin} is not on the list")
        return verified


def test_an_application_confirmer_serves_every_edge() -> None:
    confirmer = AllowlistedVerifier(
        _verifier(MemoryNonceStore(clock=lambda: T0), buyer=[buyer_key()]),
        frozenset({BUYER_ORIGIN}),
    )
    middleware = RequireSignatureMiddleware(_ok, seller_edge(confirmer))
    assert middleware.confirmer is confirmer
    with SyncRuntime() as runtime:
        sync = SyncRequestVerifier(confirmer, runtime=runtime)
        assert sync.verify(sign_request(now=T0)).origin == BUYER_ORIGIN


async def _ok(scope: Scope, receive: Receive, send: Send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b""})


def test_the_roles_resolver_and_edges_are_fixed_when_made() -> None:
    verifier = _verifier(MemoryNonceStore(clock=lambda: T0), buyer=[buyer_key()])
    other = ServerIdentity.from_config("https://evil.example.com")
    with pytest.raises(dataclasses.FrozenInstanceError):
        verifier.identity = other  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        verifier.nonces = MemoryNonceStore()  # type: ignore[misc]
    resolver = DirectoryResolver()
    with pytest.raises(AttributeError):
        resolver.limits = FetchLimits(max_keys=1)  # type: ignore[misc]
    edge = seller_edge(verifier)
    middleware = RequireSignatureMiddleware(_ok, edge)
    with pytest.raises(dataclasses.FrozenInstanceError):
        middleware.confirmer = verifier  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        edge.identity = other  # type: ignore[misc]
    auth = WebBotAuth(RequestSigner.of(buyer_key(), BUYER))
    with pytest.raises(AttributeError):
        auth.signer = None  # type: ignore[misc]
