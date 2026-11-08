"""Test support for the Web Bot Auth suite: the keys, the clock, messages, directories, refusals.

The suite's fixed world is the conformance vectors' own: the clock, the
origins, the seeds, and the bodies come from
:mod:`~iab_agentic_primitives.web_bot_auth.conformance.fixtures`, so these
tests and the vectors sign the same messages with the same keys. Messages are
signed by the real signers; a departure from the profile is a
:class:`~iab_agentic_primitives.web_bot_auth.conformance.signing.MessageDeparture`,
signed raw by the conformance interpreter.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import hashlib
import inspect
import socket
import threading
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

import httpx
import uvicorn

from iab_agentic_primitives.web_bot_auth import (
    CONFORMANCE_KEYS,
    Answer,
    Clock,
    DirectoryPublisher,
    DirectoryResolver,
    Ed25519Key,
    HTTPRequest,
    HTTPResponse,
    KeyId,
    KeyPolicy,
    KeyResolver,
    MemoryNonceStore,
    NonceStore,
    Origin,
    Refusal,
    RequestConfirmer,
    RequestSigner,
    RequestVerifier,
    ResolvedKey,
    ResponseSigner,
    ResponseVerifier,
    ServerIdentity,
    UnixSeconds,
    WebBotAuthError,
    publish_directory,
)
from iab_agentic_primitives.web_bot_auth.asgi import (
    DEFAULT_SIGNING,
    ASGIApp,
    ResponseSigningMiddleware,
    ServerEdge,
    SigningPolicy,
)
from iab_agentic_primitives.web_bot_auth.conformance.fixtures import (
    BUYER,
    COMMIT_BODY,
    COMMIT_RESULT_BODY,
    COMMIT_URL,
    JSON,
    OTHER,
    SEED_PREFIX,
    SELLER,
    T0,
    at,
    key,
)
from iab_agentic_primitives.web_bot_auth.conformance.signing import MessageDeparture, RawSigner
from iab_agentic_primitives.web_bot_auth.testing import directory_transport, served_resolver

BUYER_ORIGIN = Origin.parse(BUYER)
SELLER_ORIGIN = Origin.parse(SELLER)
OTHER_ORIGIN = Origin.parse(OTHER)
#: The seller's own identity, as its middlewares and verifier are configured.
SELLER_IDENTITY = ServerIdentity.from_config(SELLER)


# ── keys ───────────────────────────────────────────────────────────────────


def buyer_key() -> Ed25519Key:
    return key("buyer-request")


def other_key() -> Ed25519Key:
    return key("other-request")


def seller_key() -> Ed25519Key:
    return key("seller-response")


def seller_edge(
    confirmer: RequestConfirmer | None = None, *, identity: ServerIdentity | None = None
) -> ServerEdge:
    """The seller's edge: its signer, its directory, and its identity.

    The identity is the confirmer's when one is given, and ``SELLER_IDENTITY`` otherwise.
    """
    if identity is None:
        identity = SELLER_IDENTITY if confirmer is None else confirmer.identity
    signer = seller_key()
    return ServerEdge(
        identity,
        signer=ResponseSigner.of(signer, identity.origin),
        publisher=DirectoryPublisher(identity.origin, [signer]),
        confirmer=confirmer,
    )


def seller_signing(
    app: ASGIApp,
    edge: ServerEdge | None = None,
    *,
    policy: SigningPolicy = DEFAULT_SIGNING,
) -> ResponseSigningMiddleware:
    """``app`` behind the seller's response signing, which serves the directory of its key."""
    return ResponseSigningMiddleware(app, seller_edge() if edge is None else edge, policy=policy)


# ── a live server ──────────────────────────────────────────────────────────


def free_port() -> int:
    """A TCP port on 127.0.0.1 that nothing listens on now."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@contextmanager
def serving(app: ASGIApp, port: int) -> Iterator[None]:
    """Serves ``app`` with uvicorn on 127.0.0.1:``port`` while the block runs."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "the server didn't start"
        time.sleep(0.01)
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ── digests and messages ───────────────────────────────────────────────────


def b64url(data: bytes) -> str:
    """``data`` as unpadded base64url, as JWKs and nonces carry it."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_bytes(text: str) -> bytes:
    """The bytes of unpadded or padded base64url ``text``."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def digest(body: bytes) -> str:
    """The ``Content-Digest`` of ``body`` with its ``sha-256`` member (RFC 9530)."""
    return f"sha-256=:{base64.b64encode(hashlib.sha256(body).digest()).decode()}:"


def digest_without_sha256(body: bytes) -> str:
    """A correct ``Content-Digest`` of ``body`` that carries only ``sha-512``."""
    return f"sha-512=:{base64.b64encode(hashlib.sha512(body).digest()).decode()}:"


def commit(body: bytes = COMMIT_BODY) -> HTTPRequest:
    """The unsigned commit request."""
    return HTTPRequest("POST", COMMIT_URL, JSON, body)


def sign_request(
    request: HTTPRequest | None = None,
    signer: RequestSigner | None = None,
    *,
    now: UnixSeconds = T0,
    departure: MessageDeparture | None = None,
) -> HTTPRequest:
    """``request`` (the commit by default) signed by ``signer`` (the buyer by default).

    With a ``departure``, it is signed raw, as ``signer`` would but departing.
    """
    signer = RequestSigner.of(buyer_key(), BUYER) if signer is None else signer
    request = commit() if request is None else request
    if departure is None:
        return signer.sign(request, now=now)
    return RawSigner(signer.config, departure).request(request, now)


def sign_response(
    request: HTTPRequest,
    signer: ResponseSigner | None = None,
    *,
    now: UnixSeconds = T0,
    departure: MessageDeparture | None = None,
) -> HTTPResponse:
    """The seller's CommitResult to ``request``, signed by ``signer`` (the seller by default).

    With a ``departure``, it is signed raw, as ``signer`` would but departing.
    """
    signer = ResponseSigner.of(seller_key(), SELLER) if signer is None else signer
    response = HTTPResponse(200, JSON, COMMIT_RESULT_BODY, request)
    if departure is None:
        return signer.sign(response, now=now)
    return RawSigner(signer.config, departure).response(response, now)


# ── directories and verifiers ──────────────────────────────────────────────


def origin_of(origin: Origin | str) -> Origin:
    """``origin`` as an :class:`Origin`; a string is read with :meth:`Origin.parse`."""
    return origin if isinstance(origin, Origin) else Origin.parse(origin)


def signed_directory(
    origin: Origin | str, keys: Sequence[Ed25519Key], now: UnixSeconds = T0
) -> Answer:
    """The directory of ``origin``, published with ``keys`` and signed at ``now``."""
    return publish_directory(origin_of(origin), keys, now=now)


def directory_transport_for(
    directories: Mapping[str, Sequence[Ed25519Key]], now: UnixSeconds = T0
) -> httpx.MockTransport:
    """A transport that serves each origin's signed directory, signed at ``now``."""
    served = {
        origin_of(origin): signed_directory(origin, keys, now)
        for origin, keys in directories.items()
    }
    return directory_transport(served)


def directory_resolver(
    directories: Mapping[str, Sequence[Ed25519Key]], *, now: UnixSeconds | None = None
) -> DirectoryResolver:
    """A resolver over signed directories served from data, signed and fetched at ``now``.

    ``now`` is ``T0`` by default.
    """
    at = T0 if now is None else now
    return served_resolver(
        {origin_of(o): signed_directory(o, keys, at) for o, keys in directories.items()},
        clock=lambda: at,
    )


@dataclass(frozen=True, slots=True)
class Clocked:
    """A resolver and the clock its resolves read, as a verifier passes ``now``."""

    resolver: KeyResolver
    clock: Clock

    async def resolve(self, origin: Origin, keyid: KeyId) -> ResolvedKey:
        return await self.resolver.resolve(origin, keyid, now=self.clock())


def request_verifier(
    *,
    resolver: KeyResolver | None = None,
    now: UnixSeconds = T0,
    nonces: NonceStore | None = None,
    identity: ServerIdentity = SELLER_IDENTITY,
    keys: KeyPolicy = CONFORMANCE_KEYS,
) -> RequestVerifier:
    """The suite's one request verifier: the seller's, with its clock at ``now``.

    By default it resolves the buyer's and the other origin's directories and
    records nonces in memory on the same clock.
    """
    if resolver is None:
        resolver = directory_resolver({BUYER: [buyer_key()], OTHER: [other_key()]}, now=now)
    return RequestVerifier(
        resolver,
        identity=identity,
        nonces=MemoryNonceStore(clock=lambda: now) if nonces is None else nonces,
        clock=lambda: now,
        keys=keys,
    )


def response_verifier(
    *, resolver: KeyResolver | None = None, now: UnixSeconds = T0
) -> ResponseVerifier:
    """The suite's one response verifier: the buyer's, over the seller's directory by default."""
    if resolver is None:
        resolver = directory_resolver({SELLER: [seller_key()]}, now=now)
    return ResponseVerifier(resolver, clock=lambda: now, keys=CONFORMANCE_KEYS)


# ── calls and refusals ─────────────────────────────────────────────────────


#: The suite's one event loop: a resolver belongs to the loop it first resolves on, and
#: a test that resolves more than once runs as one program does, on one loop.
_RUNNER = asyncio.Runner()
atexit.register(_RUNNER.close)


def run[T](coroutine: Awaitable[T]) -> T:
    """Runs ``coroutine`` to completion on the suite's one event loop."""

    async def wait() -> T:
        return await coroutine

    return _RUNNER.run(wait())


def expect_refusal(call: Callable[[], object], refusal: Refusal) -> WebBotAuthError:
    """Calls ``call`` (awaiting what it returns, if it's awaitable) and asserts ``refusal``.

    Returns the :class:`WebBotAuthError`, whose ``status`` is the refusal's.
    """
    try:
        result = call()
        if inspect.isawaitable(result):
            run(result)
    except WebBotAuthError as e:
        assert e.refusal is refusal, f"refused with {e.refusal}, not {refusal}: {e}"
        assert e.status == refusal.status
        return e
    raise AssertionError(f"accepted; expected the refusal {refusal}")


# The crafting helpers are part of the one support namespace; they import from above.
# isort: split
from .crafting import (  # noqa: E402
    BODY_COMPONENTS,
    GET_COMPONENTS,
    POSITIVE_001,
    POSITIVE_003,
    POST_COMPONENTS,
    CraftOptions,
    SignOptions,
    added_lines,
    craft,
    edited,
    fetched,
    fetched_from,
    get_components,
    inner_list,
    post_components,
    request_added,
    response_added,
    signed_commit,
    signed_result,
    spec,
    vector_message,
    vector_request,
    verify,
)

__all__ = [
    "BODY_COMPONENTS",
    "BUYER",
    "BUYER_ORIGIN",
    "COMMIT_BODY",
    "COMMIT_RESULT_BODY",
    "COMMIT_URL",
    "GET_COMPONENTS",
    "JSON",
    "OTHER",
    "OTHER_ORIGIN",
    "POSITIVE_001",
    "POSITIVE_003",
    "POST_COMPONENTS",
    "SEED_PREFIX",
    "SELLER",
    "SELLER_IDENTITY",
    "SELLER_ORIGIN",
    "T0",
    "Clocked",
    "CraftOptions",
    "SignOptions",
    "UnixSeconds",
    "added_lines",
    "at",
    "b64url",
    "b64url_bytes",
    "buyer_key",
    "commit",
    "craft",
    "digest",
    "digest_without_sha256",
    "directory_resolver",
    "directory_transport_for",
    "edited",
    "expect_refusal",
    "fetched",
    "fetched_from",
    "get_components",
    "inner_list",
    "key",
    "origin_of",
    "other_key",
    "post_components",
    "request_added",
    "request_verifier",
    "response_added",
    "response_verifier",
    "run",
    "seller_edge",
    "seller_key",
    "seller_signing",
    "sign_request",
    "sign_response",
    "signed_commit",
    "signed_directory",
    "signed_result",
    "spec",
    "vector_message",
    "vector_request",
    "verify",
]
