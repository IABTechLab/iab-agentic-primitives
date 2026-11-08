"""The contracts a caller relies on: the refusal table as sent, configuration
errors, canonical origins, the server's authorities, and key selection.

Each test goes through a public constructor, a signer and a verifier, or an
edge. The ``type: ignore`` lines below are live: strict mypy checks this module
in blocking mode, so if one of those calls ever type-checks,
``warn_unused_ignores`` fails the gate.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import pickle
import random
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from typing import Literal

import http_sf
import httpx
import pytest
from fastapi import FastAPI

from iab_agentic_primitives.web_bot_auth import (
    CONFORMANCE_KEYS,
    DIRECTORY_PATH,
    CachePolicy,
    ConfigurationError,
    DirectoryPublisher,
    DirectoryResolver,
    Ed25519Key,
    FetchLimits,
    HeaderLines,
    HTTPRequest,
    HTTPResponse,
    KeyPolicy,
    MemoryDirectoryStore,
    MemoryNonceStore,
    Origin,
    PublishPolicy,
    Refusal,
    RequestSigner,
    RequestVerifier,
    ResponseSigner,
    ResponseVerifier,
    ServerIdentity,
    StaticKeyResolver,
    WebBotAuthError,
    accept_signature,
    publish_directory,
    refusal_response,
    verify_directory,
)
from iab_agentic_primitives.web_bot_auth.asgi import (
    DirectoryMiddleware,
    RequirePolicy,
    RequireSignatureMiddleware,
    ResponseSigningMiddleware,
    ServerEdge,
    SigningPolicy,
)
from iab_agentic_primitives.web_bot_auth.fetcher import HttpxDirectoryFetcher
from iab_agentic_primitives.web_bot_auth.httpx import WebBotAuth, verified_response
from iab_agentic_primitives.web_bot_auth.rules import CONTENT_DIGEST, Streaming
from iab_agentic_primitives.web_bot_auth.signer import SignerConfig
from iab_agentic_primitives.web_bot_auth.sync import SyncRuntime
from iab_agentic_primitives.web_bot_auth.testing import directory_transport

from . import support as w
from .support import expect_refusal

STATUSES = {400, 403, 429, 503}

# ── the refusal table ─────────────────────────────────────────────────────

# Every refusal, with its code, status, Accept-Signature, and Retry-After.
REFUSALS: dict[Refusal, tuple[str, int, bool, int | None]] = {
    Refusal.PARSE: ("parse", 400, False, None),
    Refusal.SIGNATURE_MISSING: ("signature-missing", 403, True, None),
    Refusal.MULTIPLE_SIGNATURES: ("multiple-signatures", 403, True, None),
    Refusal.SIGNATURE_AGENT_MISSING: ("signature-agent-missing", 403, True, None),
    Refusal.MULTIPLE_SIGNATURE_AGENTS: ("multiple-signature-agents", 403, True, None),
    Refusal.SIGNATURE_AGENT_PARAMETERS: ("signature-agent-parameters", 403, True, None),
    Refusal.SIGNATURE_AGENT_NOT_COVERED: ("signature-agent-not-covered", 403, True, None),
    Refusal.SIGNATURE_AGENT_VALUE: ("signature-agent-value", 403, True, None),
    Refusal.KEYID: ("keyid", 403, True, None),
    Refusal.ALG: ("alg", 403, True, None),
    Refusal.WINDOW: ("window", 403, True, None),
    Refusal.LIFETIME: ("lifetime", 403, True, None),
    Refusal.CREATED_IN_FUTURE: ("created-in-future", 403, True, None),
    Refusal.EXPIRED: ("expired", 403, True, None),
    Refusal.NONCE_MISSING: ("nonce-missing", 403, True, None),
    Refusal.NONCE_INVALID: ("nonce-invalid", 403, True, None),
    Refusal.COMPONENTS: ("components", 403, True, None),
    Refusal.CONTENT_DIGEST: ("content-digest", 403, True, None),
    Refusal.AUTHORITY: ("authority", 403, True, None),
    Refusal.REJECTED_KEY: ("rejected-key", 403, True, None),
    Refusal.KEY_NOT_FOUND: ("key-not-found", 403, True, None),
    Refusal.SIGNATURE_INVALID: ("signature-invalid", 403, True, None),
    Refusal.RESPONSE_NOT_BOUND: ("response-not-bound", 403, True, None),
    Refusal.ORIGIN_MISMATCH: ("origin-mismatch", 403, True, None),
    Refusal.DIRECTORY_UNAVAILABLE: ("directory-unavailable", 403, True, None),
    Refusal.DIRECTORY_STATUS: ("directory-status", 403, True, None),
    Refusal.DIRECTORY_ENCODING: ("directory-encoding", 403, True, None),
    Refusal.DIRECTORY_MEDIA_TYPE: ("directory-media-type", 403, True, None),
    Refusal.DIRECTORY_TOO_LARGE: ("directory-too-large", 403, True, None),
    Refusal.DIRECTORY_CONTENT_DIGEST: ("directory-content-digest", 403, True, None),
    Refusal.DIRECTORY_FORMAT: ("directory-format", 403, True, None),
    Refusal.DIRECTORY_TOO_MANY_KEYS: ("directory-too-many-keys", 403, True, None),
    Refusal.DIRECTORY_KID: ("directory-kid", 403, True, None),
    Refusal.DIRECTORY_NO_USABLE_KEY: ("directory-no-usable-key", 403, True, None),
    Refusal.DIRECTORY_SIGNATURE_PARSE: ("directory-signature-parse", 403, True, None),
    Refusal.DIRECTORY_ONE_SIGNATURE_PER_KEY: ("directory-one-signature-per-key", 403, True, None),
    Refusal.DIRECTORY_TAG: ("directory-tag", 403, True, None),
    Refusal.DIRECTORY_COMPONENTS: ("directory-components", 403, True, None),
    Refusal.DIRECTORY_WINDOW: ("directory-window", 403, True, None),
    Refusal.DIRECTORY_SIGNATURE: ("directory-signature", 403, True, None),
    Refusal.NONCE_REPLAYED: ("nonce-replayed", 429, True, None),
    Refusal.NONCE_STORE_UNAVAILABLE: ("nonce-store-unavailable", 503, False, 1),
}


def test_the_refusal_table_lists_every_member() -> None:
    assert set(REFUSALS) == set(Refusal)
    assert len({code for code, *_ in REFUSALS.values()}) == len(Refusal)


@pytest.mark.parametrize("has_body", [True, False], ids=["body", "no-body"])
@pytest.mark.parametrize("refusal", list(Refusal), ids=lambda r: r.code)
def test_each_refusal_is_sent_with_its_code_status_and_headers(
    refusal: Refusal, has_body: bool
) -> None:
    code, status, sends_accept_signature, retry_after = REFUSALS[refusal]
    error = WebBotAuthError(refusal, "why it failed")
    assert (error.refusal, error.status, error.reason, str(error)) == (
        refusal,
        status,
        code,
        "why it failed",
    )
    sent_status, headers, body = refusal_response(error, has_body=has_body)
    assert sent_status == status and status in STATUSES
    assert headers.get("content-type") == "application/json"
    assert json.loads(body) == {"error": code, "status": status}
    expected_accept = accept_signature(has_body=has_body) if sends_accept_signature else None
    assert headers.get("accept-signature") == expected_accept
    assert headers.get("retry-after") == (None if retry_after is None else str(retry_after))


def test_a_status_outside_the_table_cannot_be_expressed() -> None:
    # The error takes a Refusal, and only a Refusal has a status.
    with pytest.raises(TypeError):
        WebBotAuthError(500, "made-up-reason")  # type: ignore[arg-type]


def test_a_credential_is_made_only_by_verifying() -> None:
    verified = w.run(w.request_verifier().verify(w.sign_request()))
    assert verified.origin == w.BUYER_ORIGIN
    forgeries: list[Callable[[], object]] = [
        lambda: dataclasses.replace(verified, origin=Origin.parse("https://victim.example.com")),
        lambda: copy.copy(verified),
        lambda: copy.deepcopy(verified),
        lambda: pickle.dumps(verified),
        lambda: type(verified)(),  # type: ignore[call-arg]
    ]
    for forge in forgeries:
        with pytest.raises(TypeError):
            forge()


def test_the_covered_components_are_component_ids() -> None:
    verified = w.run(w.request_verifier().verify(w.sign_request()))
    assert CONTENT_DIGEST in verified.components


def test_a_refusal_survives_pickle_and_copy() -> None:
    error = WebBotAuthError(Refusal.NONCE_REPLAYED, "seen before")
    for copied in (pickle.loads(pickle.dumps(error)), copy.copy(error), copy.deepcopy(error)):
        assert isinstance(copied, WebBotAuthError)
        assert (copied.refusal, copied.status, str(copied)) == (
            Refusal.NONCE_REPLAYED,
            429,
            "seen before",
        )


# ── configuration errors ──────────────────────────────────────────────────

BAD_ORIGIN = "https://Buyer.example.com"
#: One key more than a directory may hold.
SEVENTEEN_KEYS = [w.key(f"directory-{i}") for i in range(17)]


def _app() -> FastAPI:
    return FastAPI()


CONSTRUCTORS: dict[str, Callable[[], object]] = {
    "RequestSigner origin": lambda: RequestSigner.of(w.buyer_key(), BAD_ORIGIN),
    "RequestSigner lifetime": lambda: RequestSigner(
        SignerConfig(w.buyer_key(), Origin.parse(w.BUYER), lifetime=0)
    ),
    "ResponseSigner origin": lambda: ResponseSigner.of(w.seller_key(), "http://seller.example.com"),
    "ResponseSigner lifetime": lambda: ResponseSigner(
        SignerConfig(w.seller_key(), Origin.parse(w.SELLER), lifetime=301)
    ),
    "StaticKeyResolver": lambda: StaticKeyResolver({BAD_ORIGIN: [w.buyer_key()]}),
    "Origin.parse": lambda: Origin.parse("https://buyer.example.com:443"),
    "ServerIdentity.from_config": lambda: ServerIdentity.from_config("https://Seller.example.com"),
    "ServerIdentity userinfo": lambda: ServerIdentity.from_config(
        "https://user@seller.example.com"
    ),
    "ServerIdentity non-ASCII port": lambda: ServerIdentity.from_config(
        "https://seller.example.com:\u00b2"
    ),
    "ServerIdentity direct, another scheme": lambda: ServerIdentity(
        Origin.parse("https://a.example.com"), "ftp://b.example.com/x?y"
    ),
    "ServerIdentity direct, another host": lambda: ServerIdentity(
        Origin.parse("https://a.example.com"), "https://b.example.com"
    ),
    "CachePolicy max_cache_age str": lambda: CachePolicy(max_cache_age="300"),  # type: ignore[arg-type]
    "CachePolicy max_cache_age -1": lambda: CachePolicy(max_cache_age=-1),
    "CachePolicy negative_ttl -60": lambda: CachePolicy(negative_ttl=-60),
    "MemoryDirectoryStore max_entries": lambda: MemoryDirectoryStore(max_entries=0),
    "FetchLimits max_bytes": lambda: FetchLimits(max_bytes=-1),
    "FetchLimits max_keys": lambda: FetchLimits(max_keys=0),
    "FetchLimits total_timeout": lambda: FetchLimits(total_timeout=-1),
    "FetchLimits connect_timeout bool": lambda: FetchLimits(connect_timeout=True),
    "DirectoryResolver limits": lambda: DirectoryResolver(
        limits="profile"  # type: ignore[arg-type]
    ),
    "MemoryNonceStore max_entries": lambda: MemoryNonceStore(max_entries=0),
    "MemoryNonceStore max_entries type": lambda: MemoryNonceStore(max_entries="1"),  # type: ignore[arg-type]
    "DirectoryMiddleware": lambda: DirectoryMiddleware(
        _app(),
        [w.seller_key()],  # type: ignore[arg-type]
    ),
    "DirectoryPublisher origin": lambda: DirectoryPublisher(
        w.SELLER,  # type: ignore[arg-type]
        [w.seller_key()],
    ),
    "DirectoryPublisher no keys": lambda: DirectoryPublisher(w.SELLER_ORIGIN, []),
    "DirectoryPublisher a key twice": lambda: DirectoryPublisher(
        w.SELLER_ORIGIN, [w.seller_key(), w.seller_key()]
    ),
    "DirectoryPublisher 17 keys": lambda: DirectoryPublisher(w.SELLER_ORIGIN, SEVENTEEN_KEYS),
    "PublishPolicy lifetime -1": lambda: PublishPolicy(lifetime=-1),
    "PublishPolicy lifetime 0": lambda: PublishPolicy(lifetime=0),
    "PublishPolicy lifetime True": lambda: PublishPolicy(lifetime=True),
    "publish_directory no keys": lambda: publish_directory(w.SELLER_ORIGIN, [], now=w.T0),
    "publish_directory a key twice": lambda: publish_directory(
        w.SELLER_ORIGIN, [w.seller_key(), w.seller_key()], now=w.T0
    ),
    "publish_directory 17 keys": lambda: publish_directory(
        w.SELLER_ORIGIN, SEVENTEEN_KEYS, now=w.T0
    ),
    "ServerEdge identity": lambda: ServerEdge(
        w.SELLER,  # type: ignore[arg-type]
        signer=ResponseSigner.of(w.seller_key(), w.SELLER),
        publisher=DirectoryPublisher(w.SELLER_ORIGIN, [w.seller_key()]),
    ),
    "ServerEdge another directory": lambda: ServerEdge(
        w.SELLER_IDENTITY,
        signer=ResponseSigner.of(w.seller_key(), w.SELLER),
        publisher=DirectoryPublisher(w.OTHER_ORIGIN, [w.seller_key()]),
    ),
    "ServerEdge unpublished key": lambda: ServerEdge(
        w.SELLER_IDENTITY,
        signer=ResponseSigner.of(w.seller_key(), w.SELLER),
        publisher=DirectoryPublisher(w.SELLER_ORIGIN, [w.other_key()]),
    ),
    "ResponseSigningMiddleware edge": lambda: ResponseSigningMiddleware(
        _app(),
        w.SELLER_IDENTITY,  # type: ignore[arg-type]
    ),
    "RequireSignatureMiddleware edge without a confirmer": lambda: RequireSignatureMiddleware(
        _app(), w.seller_edge()
    ),
    "SigningPolicy max_body_bytes": lambda: SigningPolicy(max_body_bytes=-1),
    "Streaming media_types": lambda: Streaming("text/event-stream"),  # type: ignore[arg-type]
    "SigningPolicy streaming": lambda: SigningPolicy(
        streaming=("text/event-stream",)  # type: ignore[arg-type]
    ),
    "RequirePolicy max_body_bytes": lambda: RequirePolicy(max_body_bytes=True),
    "WebBotAuth expect": lambda: WebBotAuth(
        None,
        ResponseVerifier(StaticKeyResolver({}), keys=CONFORMANCE_KEYS),
        expect=w.SELLER,  # type: ignore[arg-type]
    ),
    "WebBotAuth streaming": lambda: WebBotAuth(
        None,
        ResponseVerifier(StaticKeyResolver({}), keys=CONFORMANCE_KEYS),
        streaming=("application/x-ndjson",),  # type: ignore[arg-type]
    ),
}


@pytest.mark.parametrize("name", list(CONSTRUCTORS))
def test_bad_configuration_raises_configuration_error(name: str) -> None:
    # A misconfiguration fails at construction, as a ValueError, and is never a refusal.
    with pytest.raises(ConfigurationError) as e:
        CONSTRUCTORS[name]()
    assert isinstance(e.value, ValueError) and not isinstance(e.value, WebBotAuthError)


def test_a_key_seed_is_32_bytes() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        Ed25519Key.from_seed(b"short")


def test_an_identifier_set_is_not_a_bare_string() -> None:
    kid = w.buyer_key().thumbprint
    with pytest.raises(ConfigurationError):
        KeyPolicy(reject_keyids=kid)
    with pytest.raises(ConfigurationError):
        KeyPolicy(accept_test_keys="yes")  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError):
        ResponseVerifier(StaticKeyResolver({}), keys={kid})  # type: ignore[arg-type]


# ── canonical origins ─────────────────────────────────────────────────────

BAD_HOSTS = [
    "http://buyer.example.com",
    "https://BUYER.example.com",
    "HTTPS://buyer.example.com",
    "https://buyer.example.com:443",
    "https://buyer.example.com:8443",
    "https://user@buyer.example.com",
    "https://b\u00fccher.example.com",
    "https://2130706433",
    "https://127.1",
    "https://1.2.3.04",
    "https://0x7f.0.0.1",
    "https://seller.0x10",
    "https://[fe80::1%en0]",
    "https://",
    "buyer.example.com",
]


@pytest.mark.parametrize(
    "value", ["https://buyer.example.com", "https://[::1]", "https://127.0.0.1", "https://a"]
)
def test_an_origin_is_exactly_https_and_a_canonical_host(value: str) -> None:
    origin = Origin.parse(value)
    assert str(origin) == value
    assert origin.host == origin.authority == value[len("https://") :]
    assert origin.directory_url == value + DIRECTORY_PATH


@pytest.mark.parametrize(
    "value",
    [
        *BAD_HOSTS,
        "https://buyer.example.com/",
        "https://buyer.example.com/agents/a",
        "https://buyer.example.com?x=1",
        "https://buyer.example.com#f",
        "https://buyer.example.com" + DIRECTORY_PATH,
        "",
        42,
    ],
)
def test_a_non_canonical_origin_is_a_configuration_error(value: object) -> None:
    with pytest.raises(ConfigurationError):
        Origin.parse(value)


# ── the server's authority ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "refusal"),
    [
        ("https://seller.example.com:8443/proposals", None),
        ("https://Seller.Example.COM:8443/proposals?x=1", None),
        ("https://seller.example.com/proposals", Refusal.AUTHORITY),
        ("https://seller.example.com:9443/proposals", Refusal.AUTHORITY),
        ("ftp://seller.example.com:8443/proposals", Refusal.AUTHORITY),
    ],
)
def test_a_server_on_a_port_answers_for_its_base_urls_authority_alone(
    url: str, refusal: Refusal | None
) -> None:
    # The authority normalizes as @authority does: a lowercase host, no default port.
    identity = ServerIdentity.from_config("https://seller.example.com:8443")
    verifier = RequestVerifier(
        StaticKeyResolver({w.BUYER: [w.buyer_key()]}),
        identity=identity,
        nonces=MemoryNonceStore(clock=lambda: w.T0),
        clock=lambda: w.T0,
        keys=CONFORMANCE_KEYS,
    )
    # A signer signs only http and https URLs, so the ftp URL is presented, not signed.
    signable = url.replace("ftp://", "https://")
    signer = RequestSigner.of(w.buyer_key(), w.BUYER)
    signed = replace(signer.sign(HTTPRequest("GET", signable), now=w.T0), url=url)
    if refusal is None:
        assert w.run(verifier.verify(signed)).origin == w.BUYER_ORIGIN
    else:
        expect_refusal(lambda: verifier.verify(signed), refusal)


# ── key selection by (origin, kid) on a warm cache ────────────────────────


def _verify(verifier: RequestVerifier, request: HTTPRequest) -> Origin:
    verified = w.run(verifier.verify(request, now=w.T0))
    return verified.origin


def _verifier(resolver: DirectoryResolver) -> RequestVerifier:
    return RequestVerifier(
        resolver,
        identity=w.SELLER_IDENTITY,
        nonces=MemoryNonceStore(clock=lambda: w.T0),
        clock=lambda: w.T0,
        keys=CONFORMANCE_KEYS,
    )


def _directories(buyer: Ed25519Key, other: Ed25519Key) -> httpx.MockTransport:
    return directory_transport(
        {
            w.BUYER_ORIGIN: w.signed_directory(w.BUYER, [buyer]),
            w.OTHER_ORIGIN: w.signed_directory(w.OTHER, [other]),
        }
    )


def _claims_other(buyer: Ed25519Key) -> HTTPRequest:
    """A commit signed with the buyer's key whose Signature-Agent names OTHER."""
    return w.signed_commit(buyer, w.OTHER)


def test_a_warm_cache_keys_on_origin_and_kid() -> None:
    buyer, other = w.buyer_key(), w.other_key()
    verifier = _verifier(
        DirectoryResolver(
            HttpxDirectoryFetcher(transport=_directories(buyer, other)), clock=lambda: w.T0
        )
    )
    assert _verify(verifier, w.signed_commit(buyer)) == w.BUYER_ORIGIN
    expect_refusal(lambda: _verify(verifier, _claims_other(buyer)), Refusal.KEY_NOT_FOUND)


def test_a_shared_cache_keys_on_origin_and_kid() -> None:
    buyer, other = w.buyer_key(), w.other_key()
    store = MemoryDirectoryStore()
    first, second = (
        _verifier(
            DirectoryResolver(
                HttpxDirectoryFetcher(transport=_directories(buyer, other)),
                store=store,
                clock=lambda: w.T0,
            )
        )
        for _ in range(2)
    )
    assert _verify(first, w.signed_commit(buyer)) == w.BUYER_ORIGIN
    assert asyncio.run(store.get(w.BUYER_ORIGIN)) is not None
    assert asyncio.run(store.get(w.OTHER_ORIGIN)) is None
    expect_refusal(lambda: _verify(second, _claims_other(buyer)), Refusal.KEY_NOT_FOUND)


def test_a_cache_warmed_in_the_other_order_keys_on_origin_and_kid() -> None:
    # OTHER's directory is cached first, then BUYER's; BUYER's key still names only BUYER.
    buyer, other = w.buyer_key(), w.other_key()
    resolver = DirectoryResolver(
        HttpxDirectoryFetcher(transport=_directories(buyer, other)), clock=lambda: w.T0
    )
    verifier = _verifier(resolver)
    assert _verify(verifier, w.signed_commit(other, w.OTHER)) == w.OTHER_ORIGIN
    assert _verify(verifier, w.signed_commit(buyer)) == w.BUYER_ORIGIN
    expect_refusal(lambda: _verify(verifier, _claims_other(buyer)), Refusal.KEY_NOT_FOUND)


# ── the httpx edge's expected origin ──────────────────────────────────────


def _seller_on(url_port: int, seller: Ed25519Key) -> httpx.MockTransport:
    signer = ResponseSigner(SignerConfig(seller, w.SELLER_ORIGIN, clock=lambda: w.T0))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.port == url_port
        body = b'{"ok":true}'
        incoming = HTTPRequest(request.method, str(request.url), request.headers.multi_items())
        signed = signer.sign(HTTPResponse(200, w.JSON, body, incoming))
        return httpx.Response(200, headers=signed.headers.items(), content=body)

    return httpx.MockTransport(handler)


@pytest.mark.parametrize("expect", [w.SELLER_ORIGIN, "same-origin"])
def test_a_response_from_another_port_verifies_as_the_hosts_identity(
    expect: Origin | Literal["same-origin"],
) -> None:
    seller = w.seller_key()
    resolver = DirectoryResolver(
        HttpxDirectoryFetcher(
            transport=directory_transport({w.SELLER_ORIGIN: w.signed_directory(w.SELLER, [seller])})
        ),
        clock=lambda: w.T0,
    )
    with SyncRuntime(resolver) as runtime:
        verifier = ResponseVerifier(resolver, clock=lambda: w.T0, keys=CONFORMANCE_KEYS)
        auth = WebBotAuth.verify_only(verifier, runtime=runtime, expect=expect)
        with auth.client(transport=_seller_on(8443, seller)) as client:
            response = client.get("https://seller.example.com:8443/proposals/prop_7")
    verified = verified_response(response)
    assert verified is not None and verified.origin == w.SELLER_ORIGIN


# ── only typed refusals escape ────────────────────────────────────────────

_ALPHABET = '!"#$%(),-./:;<=>?@[]_{}=;"0123456789abcdefABCDEF sig1key\té'


def _mutate(value: str, rng: random.Random) -> str:
    """One random edit: delete, insert, duplicate, or truncate."""
    if not value:
        return rng.choice(_ALPHABET)
    i = rng.randrange(len(value))
    edit = rng.randrange(4)
    if edit == 0:
        return value[:i] + value[i + 1 :]
    if edit == 1:
        return value[:i] + rng.choice(_ALPHABET) + value[i:]
    if edit == 2:
        j = rng.randrange(i, len(value) + 1)
        return value[:j] + value[i:j] + value[j:]
    return value[:i]


def _mutations(
    headers: dict[str, str], names: tuple[str, ...], seed: int, count: int
) -> Iterator[dict[str, str]]:
    rng = random.Random(seed)
    for _ in range(count):
        mutated = dict(headers)
        for _ in range(rng.randrange(1, 4)):
            name = rng.choice([n for n in mutated if n.lower() in names] or [names[0]])
            mutated[name] = _mutate(mutated.get(name, ""), rng)
        yield mutated


def _fields(headers: Mapping[str, str], names: tuple[str, ...]) -> dict[str, object]:
    """The fields ``names`` of ``headers`` as Structured Fields parse them; ``None`` if not."""
    parsed: dict[str, object] = {}
    for name, value in headers.items():
        if name.lower() in names:
            try:
                parsed[name.lower()] = http_sf.parse(value.encode(), tltype="dictionary")
            except (http_sf.StructuredFieldError, UnicodeEncodeError):
                parsed[name.lower()] = None
    return parsed


@contextmanager
def _only_refusals(
    original: Mapping[str, str],
    mutated: Mapping[str, str],
    names: tuple[str, ...] = ("signature", "signature-input", "signature-agent"),
) -> Iterator[None]:
    """Requires a typed refusal with a status from the table, unless the mutation left the
    signature fields as they were: a changed message never verifies, and nothing else escapes."""
    try:
        yield
    except WebBotAuthError as e:
        assert e.status in STATUSES, e
    else:
        assert _fields(mutated, names) == _fields(original, names), "a mutated message verified"


FIELDS = ("signature", "signature-input", "signature-agent")


def test_mutated_request_headers_raise_only_refusals() -> None:
    buyer, other = w.buyer_key(), w.other_key()
    verifier = _verifier(
        DirectoryResolver(
            HttpxDirectoryFetcher(transport=_directories(buyer, other)), clock=lambda: w.T0
        )
    )
    request = w.signed_commit(buyer)
    original = dict(request.headers)
    for mutated in _mutations(original, FIELDS, seed=1, count=600):
        with _only_refusals(original, mutated):
            w.run(verifier.verify(replace(request, headers=HeaderLines(mutated)), now=w.T0))


def test_mutated_response_headers_raise_only_refusals() -> None:
    seller = w.seller_key()
    resolver = DirectoryResolver(
        HttpxDirectoryFetcher(
            transport=directory_transport({w.SELLER_ORIGIN: w.signed_directory(w.SELLER, [seller])})
        ),
        clock=lambda: w.T0,
    )
    verifier = ResponseVerifier(resolver, clock=lambda: w.T0, keys=CONFORMANCE_KEYS)
    request = w.signed_commit(w.buyer_key())
    sent, headers = w.signed_result(seller, request)
    original = dict(headers.items())
    for mutated in _mutations(original, FIELDS, seed=2, count=600):
        with _only_refusals(original, mutated):
            response = HTTPResponse(200, mutated, w.COMMIT_RESULT_BODY, sent)
            w.run(verifier.verify(response, now=w.T0))


def test_mutated_directory_headers_raise_only_refusals() -> None:
    status, lines, body = w.signed_directory(w.BUYER, [w.buyer_key(), w.other_key()])
    headers = dict(lines.items())
    names = ("signature", "signature-input", "content-digest")
    for mutated in _mutations(headers, names, seed=3, count=600):
        with _only_refusals(headers, mutated, names):
            verify_directory(w.fetched(status, mutated, body), w.BUYER_ORIGIN, w.T0)
