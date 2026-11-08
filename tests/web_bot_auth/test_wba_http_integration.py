"""The httpx auth and the ASGI helpers, end to end in process.

A buyer client signs with :class:`WebBotAuth`; a FastAPI seller verifies the
request, resolving the buyer's key only through the buyer's own directory
route, and signs its response; the client verifies the response and its
binding through the seller's directory route. Neither side is configured with
the other's key.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import TypedDict, Unpack

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from iab_agentic_primitives.web_bot_auth import (
    CONFORMANCE_KEYS,
    DIRECTORY_MEDIA_TYPE,
    DIRECTORY_PATH,
    ConfigurationError,
    DirectoryPublisher,
    DirectoryResolver,
    Ed25519Key,
    HTTPRequest,
    HTTPResponse,
    MemoryNonceStore,
    NonceStoreUnavailable,
    Origin,
    PublishPolicy,
    Refusal,
    RequestSigner,
    RequestVerifier,
    ResponseSigner,
    ResponseVerifier,
    Seconds,
    ServerIdentity,
    StaticKeyResolver,
    WebBotAuthError,
    accept_signature,
    publish_directory,
    refusal_response,
    system_clock,
    verify_directory,
)
from iab_agentic_primitives.web_bot_auth.asgi import (
    ASGIApp,
    DirectoryMiddleware,
    OnUnsigned,
    Receive,
    RequirePolicy,
    RequireSignatureMiddleware,
    ResponseSigningMiddleware,
    Scope,
    Send,
    ServerEdge,
    request_url,
)
from iab_agentic_primitives.web_bot_auth.fetcher import HttpxDirectoryFetcher
from iab_agentic_primitives.web_bot_auth.httpx import (
    Expect,
    ResponseNotVerified,
    Unsigned,
    VerificationFailed,
    WebBotAuth,
    sent_request,
    verification,
    verified_response,
)
from iab_agentic_primitives.web_bot_auth.sync import SyncRequestVerifier, SyncRuntime
from iab_agentic_primitives.web_bot_auth.testing import directory_transport

from . import support as w
from .support import (
    BUYER,
    BUYER_ORIGIN,
    JSON,
    SELLER,
    SELLER_IDENTITY,
    SELLER_ORIGIN,
    T0,
    expect_refusal,
)

COMMIT = json.loads(w.COMMIT_BODY)
COMMIT_PATH = "/proposals/prop_7/commit"
JSON_RAW = [(k.lower().encode(), v.encode()) for k, v in JSON.items()]


class Router(httpx.AsyncBaseTransport):
    """Routes each request to the in-process app for its host."""

    def __init__(self, apps: Mapping[str, ASGIApp]) -> None:
        self.transports = {host: httpx.ASGITransport(app=app) for host, app in apps.items()}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self.transports[request.url.host].handle_async_request(request)


def seller_app(
    router_ref: httpx.AsyncBaseTransport,
    *,
    identity: ServerIdentity = SELLER_IDENTITY,
    on_unsigned: OnUnsigned = "refuse",
) -> ResponseSigningMiddleware:
    app = FastAPI()

    @app.post("/proposals/{proposal_id}/commit")
    async def commit(proposal_id: str, request: Request) -> JSONResponse:
        identity = getattr(request.state, "web_bot_auth", None)
        body = await request.json()
        return JSONResponse(
            {"entries": [body["entry"]], "signed_by": str(identity.origin) if identity else None}
        )

    @app.get("/proposals/{proposal_id}/events")
    async def events(proposal_id: str) -> StreamingResponse:
        return StreamingResponse(iter([b"data: 1\n\n"]), media_type="text/event-stream")

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    verifier = RequestVerifier(
        DirectoryResolver(HttpxDirectoryFetcher(transport=router_ref)),
        identity=identity,
        nonces=MemoryNonceStore(),
        keys=CONFORMANCE_KEYS,
    )
    edge = w.seller_edge(verifier)
    required = RequireSignatureMiddleware(
        app,
        edge,
        policy=RequirePolicy(prefixes=("/proposals",), on_unsigned=on_unsigned),
    )
    return w.seller_signing(required, edge)


def buyer_app() -> DirectoryMiddleware:
    app = FastAPI()
    return DirectoryMiddleware(app, DirectoryPublisher(BUYER_ORIGIN, [w.buyer_key()]))


class LateRouter(httpx.AsyncBaseTransport):
    """A router whose apps are set after construction, so the seller can resolve through it."""

    router: Router
    #: The seller's public base URL.
    base_url: str = SELLER

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self.router.handle_async_request(request)


#: The seller on a port other than 443: its identity is still https://seller.example.com.
PORTED = ServerIdentity.from_config("https://seller.example.com:8443")
#: The seller's identities, for the tests that run on each.
IDENTITIES = pytest.mark.parametrize(
    "network", [SELLER_IDENTITY, PORTED], ids=["default-port", "port-8443"], indirect=True
)


@pytest.fixture
def network(request: pytest.FixtureRequest) -> LateRouter:
    """The buyer and the seller, routed by host; the seller's identity is the parameter."""
    identity: ServerIdentity = getattr(request, "param", SELLER_IDENTITY)
    late = LateRouter()
    late.router = Router(
        {
            "seller.example.com": seller_app(late, identity=identity),
            "buyer.example.com": buyer_app(),
        }
    )
    late.base_url = identity.base_url
    return late


class AuthOptions(TypedDict, total=False):
    """The :class:`WebBotAuth` keyword options a test passes through :func:`client`."""

    expect: Expect
    require_signed_response: bool


def client(network: LateRouter, **auth_options: Unpack[AuthOptions]) -> httpx.AsyncClient:
    auth = WebBotAuth(
        RequestSigner.of(w.buyer_key(), BUYER),
        ResponseVerifier(
            DirectoryResolver(HttpxDirectoryFetcher(transport=network)), keys=CONFORMANCE_KEYS
        ),
        **auth_options,
    )
    return auth.async_client(transport=network)


async def _commit(network: LateRouter, **auth_options: Unpack[AuthOptions]) -> httpx.Response:
    async with client(network, **auth_options) as c:
        return await c.post(network.base_url + COMMIT_PATH, json=COMMIT)


def test_a_url_with_an_empty_query_is_sent_signed_and_answered_as_one(
    network: LateRouter,
) -> None:
    async def post() -> httpx.Response:
        async with client(network) as c:
            return await c.post(SELLER + COMMIT_PATH + "?", json=COMMIT)

    response = w.run(post())
    assert response.status_code == 200 and response.json()["signed_by"] == BUYER
    assert verified_response(response) is not None
    sent = sent_request(response)
    assert sent is not None and sent.url == SELLER + COMMIT_PATH  # sent as signed


@pytest.mark.parametrize("suffix", ["#terms", "?#terms"], ids=["fragment", "empty-query-fragment"])
def test_a_url_with_a_fragment_is_signed_and_answered_without_it(
    network: LateRouter, suffix: str
) -> None:
    async def post() -> httpx.Response:
        async with client(network) as c:
            return await c.post(SELLER + COMMIT_PATH + suffix, json=COMMIT)

    response = w.run(post())
    assert response.status_code == 200 and response.json()["signed_by"] == BUYER
    assert verified_response(response) is not None
    sent = sent_request(response)
    assert sent is not None and sent.url == SELLER + COMMIT_PATH


async def _proposal(scope: Scope, receive: Receive, send: Send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": [*JSON_RAW]})
    await send({"type": "http.response.body", "body": b'{"ok":true}'})


@pytest.mark.parametrize("suffix", ["?", "#terms", "?#terms"])
def test_a_verify_only_client_takes_the_signed_answer_to_a_url_it_wrote_loosely(
    suffix: str,
) -> None:
    keys = StaticKeyResolver({SELLER_ORIGIN: [w.seller_key()]})
    auth = WebBotAuth.verify_only(ResponseVerifier(keys, keys=CONFORMANCE_KEYS))
    transport = httpx.ASGITransport(app=w.seller_signing(_proposal))

    async def get() -> httpx.Response:
        async with auth.async_client(transport=transport) as c:
            return await c.get(SELLER + "/proposals/prop_7" + suffix)

    verified = verified_response(w.run(get()))
    assert verified is not None and verified.origin == SELLER_ORIGIN


@pytest.mark.parametrize("suffix", ["?", "#terms", "?#terms"])
def test_a_response_verifier_reads_the_url_a_caller_wrote_as_the_server_rebuilt_it(
    suffix: str,
) -> None:
    # The server rebuilt the request without the fragment or the empty query.
    rebuilt = HTTPRequest("GET", SELLER + "/proposals/prop_7")
    signer = ResponseSigner.of(w.seller_key(), SELLER)
    answered = signer.sign(HTTPResponse(200, JSON, b'{"ok":true}', rebuilt))
    as_written = HTTPRequest("GET", SELLER + "/proposals/prop_7" + suffix)
    keys = StaticKeyResolver({SELLER_ORIGIN: [w.seller_key()]})
    verifier = ResponseVerifier(keys, keys=CONFORMANCE_KEYS)
    received = HTTPResponse(200, answered.headers, answered.body, as_written)
    assert w.run(verifier.verify(received)).origin == SELLER_ORIGIN


@IDENTITIES
def test_buyer_and_seller_agree_through_discovery_only(network: LateRouter) -> None:
    response = w.run(_commit(network))
    assert response.status_code == 200
    assert (
        response.json()["signed_by"] == BUYER
    )  # the seller attributed the request to the buyer's origin
    verified = verified_response(response)
    assert verified is not None
    assert verified.origin == SELLER_ORIGIN and verified.bound and verified.body_covered


@IDENTITIES
def test_directory_route_serves_a_signed_directory(network: LateRouter) -> None:
    async def fetch() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        async with httpx.AsyncClient(transport=network) as c:
            return (
                await c.get(SELLER + DIRECTORY_PATH),
                await c.head(SELLER + DIRECTORY_PATH),
                await c.post(SELLER + DIRECTORY_PATH),
            )

    get, head, post = w.run(fetch())
    assert get.status_code == 200 and get.headers["content-type"] == DIRECTORY_MEDIA_TYPE
    now = system_clock()
    # A verifier fetches the identity's own URL, on 443, whatever port the server listens on.
    directory = verify_directory(w.fetched_from(get, now), SELLER_ORIGIN, now)
    assert list(directory.keys) == [w.seller_key().thumbprint]
    assert head.status_code == 200 and head.content == b""
    assert post.status_code == 405


def test_unsigned_request_is_refused_with_accept_signature(network: LateRouter) -> None:
    async def send() -> httpx.Response:
        async with httpx.AsyncClient(transport=network) as c:
            return await c.post(SELLER + COMMIT_PATH, json=COMMIT)

    response = w.run(send())
    assert response.status_code == 403
    assert response.json()["error"] == "signature-missing"
    assert response.headers["accept-signature"] == accept_signature(has_body=True)


def test_refusal_statuses_pass_through(network: LateRouter) -> None:
    async def replay() -> tuple[httpx.Response, httpx.Response]:
        async with client(network) as c:
            first = await c.post(SELLER + COMMIT_PATH, json=COMMIT)
        sent = sent_request(first)
        assert sent is not None
        # Resend the same signed bytes on a plain client, which doesn't sign again.
        async with httpx.AsyncClient(transport=network) as plain:
            again = await plain.post(sent.url, headers=sent.headers.items(), content=sent.body)
        return first, again

    first, again = w.run(replay())
    assert first.status_code == 200
    assert again.status_code == 429 and again.json()["error"] == "nonce-replayed"
    assert again.headers["accept-signature"] == accept_signature(has_body=True)
    assert "retry-after" not in again.headers


def test_a_nonce_store_outage_is_503_with_retry_after(buyer_key: Ed25519Key) -> None:
    class Down:
        async def check_and_record(self, origin: Origin, nonce: str, ttl: Seconds) -> bool:
            raise NonceStoreUnavailable("the store is down")

    verifier = RequestVerifier(
        StaticKeyResolver({BUYER: [buyer_key]}),
        identity=SELLER_IDENTITY,
        nonces=Down(),
        keys=CONFORMANCE_KEYS,
    )
    app = RequireSignatureMiddleware(FastAPI(), w.seller_edge(verifier))
    signed = w.signed_commit(buyer_key, now=system_clock())

    async def send() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as c:
            return await c.post(
                signed.url, headers=list(signed.headers.items()), content=signed.body
            )

    response = w.run(send())
    assert response.status_code == 503
    assert response.json()["error"] == "nonce-store-unavailable"
    assert response.headers["retry-after"] == "1"
    assert "accept-signature" not in response.headers


def test_paths_outside_the_prefixes_need_no_signature(network: LateRouter) -> None:
    async def get() -> httpx.Response:
        async with httpx.AsyncClient(transport=network) as c:
            return await c.get(SELLER + "/health")

    response = w.run(get())
    assert response.status_code == 200
    assert "signature-input" in response.headers  # the response is still signed, without a binding


def test_optional_mode_passes_unsigned_requests_without_identity() -> None:
    late = LateRouter()
    late.router = Router(
        {
            "seller.example.com": seller_app(late, on_unsigned="pass"),
            "buyer.example.com": buyer_app(),
        }
    )

    async def send() -> httpx.Response:
        async with httpx.AsyncClient(transport=late) as c:
            return await c.post(SELLER + COMMIT_PATH, json=COMMIT)

    response = w.run(send())
    assert response.status_code == 200 and response.json()["signed_by"] is None


@pytest.mark.parametrize("strict", [True, False], ids=["required", "optional"])
def test_streaming_responses_pass_through_unsigned(network: LateRouter, strict: bool) -> None:
    async def stream() -> httpx.Response:
        async with client(network, require_signed_response=strict) as c:
            return await c.get(SELLER + "/proposals/prop_7/events")

    response = w.run(stream())
    assert response.status_code == 200 and response.content == b"data: 1\n\n"
    assert "signature" not in response.headers
    assert verified_response(response) is None
    assert isinstance(verification(response), Unsigned)


def not_verified(call: Callable[[], Awaitable[httpx.Response]]) -> ResponseNotVerified:
    """The :class:`ResponseNotVerified` that ``call`` raises."""
    with pytest.raises(ResponseNotVerified) as e:
        w.run(call())
    return e.value


def failure(found: object, reason: Refusal) -> None:
    """Asserts that ``found`` is a failed verification for ``reason``."""
    assert isinstance(found, VerificationFailed) and found.reason is reason, found


def test_an_unsigned_success_is_refused_when_required_and_carries_the_answer() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "committed"})

    async def post(**options: Unpack[AuthOptions]) -> httpx.Response:
        auth = WebBotAuth(
            RequestSigner.of(w.buyer_key(), BUYER),
            ResponseVerifier(StaticKeyResolver({}), keys=CONFORMANCE_KEYS),
            **options,
        )
        async with auth.async_client(transport=httpx.MockTransport(answer)) as c:
            return await c.post(SELLER + COMMIT_PATH, json=COMMIT)

    refused = not_verified(post)
    assert isinstance(refused.verification, Unsigned)
    assert refused.response.json() == {"status": "committed"}
    optional = w.run(post(require_signed_response=False))
    assert optional.json() == {"status": "committed"}
    assert isinstance(verification(optional), Unsigned)


def test_a_valid_response_signed_by_another_origin_is_an_origin_mismatch(
    other_key: Ed25519Key,
) -> None:
    """Another identity answers for seller.example.com with a valid, bound signature."""
    now = system_clock()
    impostor = ResponseSigner.of(other_key, w.OTHER)

    def answer(request: httpx.Request) -> httpx.Response:
        incoming = HTTPRequest(
            request.method, str(request.url), request.headers.multi_items(), request.content
        )
        body = b'{"ok":true}'
        signed = impostor.sign(HTTPResponse(200, w.JSON, body, incoming))
        return httpx.Response(200, headers=signed.headers.items(), content=body)

    directories = directory_transport(
        {w.OTHER_ORIGIN: w.signed_directory(w.OTHER, [other_key], now)}
    )

    def auth(**options: Unpack[AuthOptions]) -> WebBotAuth:
        resolver = DirectoryResolver(HttpxDirectoryFetcher(transport=directories))
        signer = RequestSigner.of(w.buyer_key(), BUYER)
        return WebBotAuth(signer, ResponseVerifier(resolver, keys=CONFORMANCE_KEYS), **options)

    def send(auth: WebBotAuth) -> Callable[[], Awaitable[httpx.Response]]:
        async def post() -> httpx.Response:
            async with auth.async_client(transport=httpx.MockTransport(answer)) as c:
                return await c.post(SELLER + COMMIT_PATH, json=COMMIT)

        return post

    failure(not_verified(send(auth())).verification, Refusal.ORIGIN_MISMATCH)
    # Accepting unsigned responses accepts no failed verification.
    optional = w.run(send(auth(require_signed_response=False))())
    assert verified_response(optional) is None
    failure(verification(optional), Refusal.ORIGIN_MISMATCH)
    # The same response verifies when any identity may answer.
    verified = verified_response(w.run(send(auth(expect=None))()))
    assert verified is not None
    assert str(verified.origin) == w.OTHER


# ── the sync client ───────────────────────────────────────────────────────


def test_sync_client_signs_and_verifies(buyer_key: Ed25519Key, seller_key: Ed25519Key) -> None:
    sellers_resolver = DirectoryResolver(
        HttpxDirectoryFetcher(
            transport=directory_transport(
                {BUYER_ORIGIN: w.signed_directory(BUYER, [buyer_key], now=system_clock())}
            )
        )
    )
    verifier = RequestVerifier(
        sellers_resolver,
        identity=SELLER_IDENTITY,
        nonces=MemoryNonceStore(),
        keys=CONFORMANCE_KEYS,
    )
    signer = ResponseSigner.of(seller_key, SELLER)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        incoming = HTTPRequest(
            request.method, str(request.url), request.headers.multi_items(), request.content
        )
        verified = sync.verify(incoming)
        body = json.dumps({"by": str(verified.origin)}).encode()
        headers = {**JSON, **w.response_added(signer, 200, JSON, body, incoming)}
        return httpx.Response(200, headers=headers, content=body)

    seller_dir = directory_transport(
        {SELLER_ORIGIN: w.signed_directory(SELLER, [seller_key], now=system_clock())}
    )
    buyers = DirectoryResolver(HttpxDirectoryFetcher(transport=seller_dir))
    with SyncRuntime(buyers) as runtime, SyncRuntime(sellers_resolver) as sellers:
        sync = SyncRequestVerifier(verifier, runtime=sellers)
        auth = WebBotAuth(
            RequestSigner.of(buyer_key, BUYER),
            ResponseVerifier(buyers, keys=CONFORMANCE_KEYS),
            runtime=runtime,
        )
        with auth.client(transport=httpx.MockTransport(handler)) as c:
            response = c.post(SELLER + COMMIT_PATH, json=COMMIT)
    assert response.json() == {"by": BUYER}
    verified = verified_response(response)
    assert verified is not None
    assert verified.bound


def test_sign_only_auth_leaves_responses_alone(buyer_key: Ed25519Key) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={})

    auth = WebBotAuth(RequestSigner.of(buyer_key, BUYER))
    with auth.client(transport=httpx.MockTransport(handler)) as c:
        response = c.get(SELLER + "/proposals")
    assert "signature-input" in seen and seen["accept-encoding"] != "identity"
    assert verified_response(response) is None


# ── helpers ───────────────────────────────────────────────────────────────


def test_request_url_rebuilds_the_target() -> None:
    scope: Scope = {
        "scheme": "http",
        "path": "/a b",
        "raw_path": b"/a%20b",
        "query_string": b"x=1",
        "headers": [(b"host", b"seller.example.com:8080")],
    }

    # The Host header is the client's claim; the server's own base URL wins.
    def on(base_url: str) -> ServerIdentity:
        return ServerIdentity.from_config(base_url)

    assert request_url(on("https://seller.example.com"), scope) == (
        "https://seller.example.com/a%20b?x=1"
    )
    assert w.seller_edge().request_url(scope) == request_url(SELLER_IDENTITY, scope)
    assert request_url(on("https://seller.example.com/"), {"path": "/"}) == (
        "https://seller.example.com/"
    )
    assert request_url(on("http://127.0.0.1"), {"path": "/"}) == "http://127.0.0.1/"
    port = on("https://seller.example.com:8443")
    assert request_url(port, {"path": "/"}) == "https://seller.example.com:8443/"
    assert str(port.origin) == "https://seller.example.com"
    assert port.request_authority == "seller.example.com:8443"


@pytest.mark.parametrize(
    ("scope", "url"),
    [
        ({"raw_path": b"/p", "query_string": b"ids[]=1&f=a|b"}, f"{SELLER}/p?ids[]=1&f=a|b"),
        ({"raw_path": b"/caf\xc3\xa9", "query_string": b"q=\xe9"}, f"{SELLER}/caf%C3%A9?q=%E9"),
        ({"raw_path": b"/x/../p%zz", "query_string": b"a?b"}, f"{SELLER}/p%zz?a?b"),
        ({"path": "/a b?c#d"}, f"{SELLER}/a%20b%3Fc%23d"),
        ({"raw_path": b"*"}, f"{SELLER}/*"),
    ],
    ids=["raw-characters", "non-ascii-bytes", "dots-and-escapes", "decoded-path", "asterisk"],
)
def test_request_url_reads_the_bytes_the_client_sent(scope: Scope, url: str) -> None:
    assert request_url(SELLER_IDENTITY, scope) == url


def test_a_server_that_only_verifies_rebuilds_the_url_from_its_identity_alone() -> None:
    verifier = RequestVerifier(
        StaticKeyResolver({BUYER_ORIGIN: [w.buyer_key()]}),
        identity=SELLER_IDENTITY,
        nonces=MemoryNonceStore(),
        keys=CONFORMANCE_KEYS,
    )

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        # No ServerEdge, signer, or key: the identity is all the rebuild needs.
        url = request_url(SELLER_IDENTITY, scope)
        lines = scope["headers"]
        assert isinstance(lines, list)
        headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in lines]
        try:
            verified = await verifier.verify(HTTPRequest(str(scope["method"]), url, headers))
            status, body = 200, str(verified.origin).encode()
        except WebBotAuthError as e:
            status, body = e.status, e.reason.encode()
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": body})

    auth = WebBotAuth(RequestSigner.of(w.buyer_key(), BUYER))

    async def get(path: str) -> httpx.Response:
        async with auth.async_client(transport=httpx.ASGITransport(app=app)) as c:
            return await c.get(SELLER + path)

    for path in ("/proposals/a%20b", "/proposals/a b", "/proposals?ids[]=1&f=a|b"):
        response = w.run(get(path))
        assert (response.status_code, response.text) == (200, BUYER), path


@pytest.mark.parametrize(
    "base_url",
    [
        "seller.example.com",
        "https://seller.example.com/api",
        "https://",
        "https://seller.example.com:443",
        "http://seller.example.com:80",
        "https://seller.example.com:08443",
        "https://Seller.example.com",
        "HTTPS://seller.example.com",
        "https://user@seller.example.com",
        "https://seller.example.com?x=1",
        "https://seller.example.com#f",
        "ftp://seller.example.com",
    ],
)
def test_base_url_must_be_an_origin(base_url: str) -> None:
    with pytest.raises(ConfigurationError):
        ServerIdentity.from_config(base_url)


def test_the_asgi_helpers_take_a_server_identity_not_a_string() -> None:
    with pytest.raises(ConfigurationError):
        DirectoryMiddleware(FastAPI(), [w.seller_key()])  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError):
        DirectoryPublisher(SELLER, [w.seller_key()])  # type: ignore[arg-type]


def test_the_response_signer_must_sign_as_the_servers_identity() -> None:
    with pytest.raises(ConfigurationError):
        ServerEdge(
            SELLER_IDENTITY,
            signer=ResponseSigner.of(w.seller_key(), w.OTHER),
            publisher=DirectoryPublisher(w.OTHER_ORIGIN, [w.seller_key()]),
        )


def test_the_middlewares_take_the_servers_edge() -> None:
    verifier = RequestVerifier(
        StaticKeyResolver({BUYER: [w.buyer_key()]}),
        identity=SELLER_IDENTITY,
        nonces=MemoryNonceStore(),
        keys=CONFORMANCE_KEYS,
    )
    with pytest.raises(ConfigurationError):
        ResponseSigningMiddleware(FastAPI(), ResponseSigner.of(w.seller_key(), SELLER))  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError):
        RequireSignatureMiddleware(FastAPI(), verifier)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "base_url",
    [
        "https://seller.example.com",
        "http://seller.example.com:8443",
        "https://seller.example.com:9443",
    ],
)
def test_an_edge_refuses_a_confirmer_on_another_base_url(base_url: str) -> None:
    verifier = RequestVerifier(
        StaticKeyResolver({BUYER: [w.buyer_key()]}),
        identity=ServerIdentity.from_config(base_url),
        nonces=MemoryNonceStore(),
        keys=CONFORMANCE_KEYS,
    )
    with pytest.raises(ConfigurationError, match="base URL is https://seller.example.com:8443"):
        w.seller_edge(verifier, identity=PORTED)


@pytest.mark.parametrize(
    ("verifies_for", "status"), [(SELLER_IDENTITY, 200), (PORTED, 403)], ids=["same", "other"]
)
def test_a_boundary_verifier_on_another_base_url_refuses_the_request(
    verifies_for: ServerIdentity, status: int
) -> None:
    """The application verifies at its own boundary, on the URL its edge rebuilds."""
    edge = w.seller_edge()
    verifier = RequestVerifier(
        StaticKeyResolver({BUYER: [w.buyer_key()]}),
        identity=verifies_for,
        nonces=MemoryNonceStore(),
        keys=CONFORMANCE_KEYS,
    )
    api = FastAPI()

    @api.post(COMMIT_PATH)
    async def commit(request: Request) -> Response:
        body = await request.body()
        received = HTTPRequest(
            request.method, edge.request_url(request.scope), request.headers.items(), body
        )
        try:
            await verifier.verify(received)
        except WebBotAuthError as e:
            refused = refusal_response(e, has_body=bool(body))
            return Response(refused.body, refused.status, dict(refused.headers.items()))
        return JSONResponse({"ok": True})

    auth = WebBotAuth(
        RequestSigner.of(w.buyer_key(), BUYER),
        ResponseVerifier(StaticKeyResolver({SELLER: [w.seller_key()]}), keys=CONFORMANCE_KEYS),
    )

    async def post() -> httpx.Response:
        transport = httpx.ASGITransport(app=ResponseSigningMiddleware(api, edge))
        async with auth.async_client(transport=transport) as c:
            return await c.post(SELLER + COMMIT_PATH, json=COMMIT)

    response = w.run(post())
    assert response.status_code == status
    if status == 200:
        assert verified_response(response) is not None  # the response binds to the request
    else:
        assert response.json()["error"] == "authority"


def test_the_directory_is_signed_for_the_configured_authority_whatever_the_host() -> None:
    app = DirectoryMiddleware(
        FastAPI(), DirectoryPublisher(SELLER_IDENTITY.origin, [w.seller_key()])
    )

    async def fetch() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as c:
            return await c.get("https://evil.example.com" + DIRECTORY_PATH)

    response = w.run(fetch())
    now = system_clock()
    fetched = w.fetched_from(response, now)
    rehosted = replace(fetched, url=SELLER_ORIGIN.directory_url)
    assert verify_directory(rehosted, SELLER_ORIGIN, now).origin == SELLER_ORIGIN
    # Served again from another domain, its signatures name the seller's authority.
    evil = Origin.parse("https://evil.example.com")
    expect_refusal(lambda: verify_directory(fetched, evil, now), Refusal.DIRECTORY_SIGNATURE)


def test_a_request_signed_for_another_server_is_refused(network: LateRouter) -> None:
    """The buyer's request to seller.example.com, forwarded to another server with its Host."""
    seller_b = ServerIdentity.from_config("https://seller-b.example.com")
    verifier = RequestVerifier(
        DirectoryResolver(HttpxDirectoryFetcher(transport=network)),
        identity=seller_b,
        nonces=MemoryNonceStore(),
        keys=CONFORMANCE_KEYS,
    )
    app = RequireSignatureMiddleware(FastAPI(), w.seller_edge(verifier))

    async def forward() -> httpx.Response:
        added = w.request_added(
            RequestSigner.of(w.buyer_key(), BUYER),
            "POST",
            SELLER + COMMIT_PATH,
            {"Content-Type": "application/json"},
            json.dumps(COMMIT).encode(),
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as c:
            return await c.post(
                SELLER + COMMIT_PATH,  # Host: seller.example.com
                content=json.dumps(COMMIT).encode(),
                headers={"Content-Type": "application/json", **added},
            )

    response = w.run(forward())
    assert response.status_code == 403 and response.json()["error"] == "signature-invalid"


def test_the_verifier_refuses_a_url_for_another_authority(buyer_key: Ed25519Key) -> None:
    request = w.signed_commit(buyer_key)
    verifier = RequestVerifier(
        w.directory_resolver({BUYER: [buyer_key]}),
        nonces=MemoryNonceStore(clock=lambda: T0),
        clock=lambda: T0,
        identity=ServerIdentity.from_config("https://seller-b.example.com"),
        keys=CONFORMANCE_KEYS,
    )
    expect_refusal(lambda: w.verify(verifier, request), Refusal.AUTHORITY)
    allowed = RequestVerifier(
        w.directory_resolver({BUYER: [buyer_key]}),
        nonces=MemoryNonceStore(clock=lambda: T0),
        clock=lambda: T0,
        identity=ServerIdentity.from_config("https://seller.example.com"),
        keys=CONFORMANCE_KEYS,
    )
    assert w.verify(allowed, request).origin == BUYER_ORIGIN


def test_a_framework_route_answers_with_the_publisher() -> None:
    publisher = DirectoryPublisher(SELLER_ORIGIN, [w.seller_key()])
    status, headers, body = w.run(publisher.answer(T0))
    assert status == 200 and json.loads(body)["keys"][0]["kid"] == w.seller_key().thumbprint
    assert "Signature" in headers


def test_a_response_the_profile_cannot_sign_is_refused_loudly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"raw"})

    wrapped = w.seller_signing(app)

    async def send() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped)) as c:
            return await c.get("https://seller.example.com/x")

    response = w.run(send())
    assert response.status_code == 500 and response.json()["error"] == "response-unsignable"
    assert "cannot sign the response" in caplog.text


def test_a_refusal_to_an_incomplete_signature_is_signed_unbound(network: LateRouter) -> None:
    """Signature-Input tagged web-bot-auth, but no Signature: a 403, signed, not a 500."""
    request = w.signed_commit(w.buyer_key(), origin=BUYER)
    headers = {k: v for k, v in request.headers if k != "Signature"}

    async def send() -> httpx.Response:
        async with httpx.AsyncClient(transport=network) as c:
            return await c.post(SELLER + COMMIT_PATH, content=request.body, headers=headers)

    response = w.run(send())
    assert response.status_code == 403 and response.json()["error"] == "signature-missing"
    assert '"signature";req' not in response.headers["signature-input"]
    assert "accept-signature" in response.headers


# ── a live key source for the directory ───────────────────────────────────


async def _directory(app: ASGIApp) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as c:
        return await c.get(SELLER + DIRECTORY_PATH)


def _kids(response: httpx.Response) -> list[str]:
    return [k["kid"] for k in json.loads(response.content)["keys"]]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_directory_reads_a_key_source_on_every_request(asynchronous: bool) -> None:
    first, second = w.seller_key(), w.other_key()
    current = [first]

    def sync_source() -> list[Ed25519Key]:
        return list(current)

    async def async_source() -> list[Ed25519Key]:
        return list(current)

    app = DirectoryMiddleware(
        FastAPI(),
        DirectoryPublisher(SELLER_IDENTITY.origin, async_source if asynchronous else sync_source),
    )
    assert _kids(w.run(_directory(app))) == [first.thumbprint]
    current.append(second)  # rotation: publish the new key next to the old one
    response = w.run(_directory(app))
    assert _kids(response) == [first.thumbprint, second.thumbprint]
    now = system_clock()
    directory = verify_directory(w.fetched_from(response, now), SELLER_ORIGIN, now)
    assert set(directory.keys) == {first.thumbprint, second.thumbprint}
    current.remove(first)
    assert _kids(w.run(_directory(app))) == [second.thumbprint]
    current.clear()
    assert w.run(_directory(app)).status_code == 503


def test_directory_still_takes_a_list() -> None:
    with pytest.raises(ConfigurationError):
        DirectoryPublisher(SELLER_IDENTITY.origin, [])
    assert _kids(
        w.run(
            _directory(
                DirectoryMiddleware(
                    FastAPI(), DirectoryPublisher(SELLER_IDENTITY.origin, [w.seller_key()])
                )
            )
        )
    ) == [w.seller_key().thumbprint]


# ── verify-only: an unsigned request, a signed response ───────────────────


def _bearer_seller(seller_key: Ed25519Key, *, origin: str = SELLER) -> httpx.MockTransport:
    signer = ResponseSigner.of(seller_key, origin)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "signature" not in request.headers and "signature-agent" not in request.headers
        assert request.headers["authorization"] == "Bearer t0k3n"
        body = b'{"ok":true}'
        incoming = HTTPRequest(
            request.method, str(request.url), request.headers.multi_items(), request.content
        )
        headers = {**JSON, **w.response_added(signer, 200, JSON, body, incoming)}
        return httpx.Response(200, headers=headers, content=body)

    return httpx.MockTransport(handler)


def _seller_directory(seller_key: Ed25519Key) -> DirectoryResolver:
    now = system_clock()
    return DirectoryResolver(
        HttpxDirectoryFetcher(
            transport=directory_transport(
                {SELLER_ORIGIN: w.signed_directory(SELLER, [seller_key], now=now)}
            )
        )
    )


def test_verify_only_leaves_the_request_unsigned_and_verifies_the_response(
    seller_key: Ed25519Key,
) -> None:
    resolver = _seller_directory(seller_key)
    headers = {"Authorization": "Bearer t0k3n"}
    with SyncRuntime(resolver) as runtime:
        auth = WebBotAuth.verify_only(
            ResponseVerifier(resolver, keys=CONFORMANCE_KEYS), runtime=runtime
        )
        with auth.client(transport=_bearer_seller(seller_key), headers=headers) as c:
            response = c.post(SELLER + COMMIT_PATH, json=COMMIT)
    verified = verified_response(response)
    assert verified is not None
    assert verified.origin == SELLER_ORIGIN and verified.bound is False and verified.body_covered


def test_verify_only_works_with_the_async_client(seller_key: Ed25519Key) -> None:
    auth = WebBotAuth.verify_only(
        ResponseVerifier(_seller_directory(seller_key), keys=CONFORMANCE_KEYS)
    )

    async def send() -> httpx.Response:
        async with auth.async_client(
            transport=_bearer_seller(seller_key), headers={"Authorization": "Bearer t0k3n"}
        ) as c:
            return await c.get(SELLER + "/proposals/prop_7")

    verified = verified_response(w.run(send()))
    assert verified is not None
    assert verified.origin == SELLER_ORIGIN


def test_verify_only_checks_the_expected_origin(seller_key: Ed25519Key) -> None:
    resolver = _seller_directory(seller_key)
    headers = {"Authorization": "Bearer t0k3n"}
    with SyncRuntime(resolver) as runtime:
        verifier = ResponseVerifier(resolver, keys=CONFORMANCE_KEYS)
        auth = WebBotAuth.verify_only(verifier, runtime=runtime, expect=w.OTHER_ORIGIN)
        with auth.client(transport=_bearer_seller(seller_key), headers=headers) as c:
            with pytest.raises(ResponseNotVerified) as e:
                c.get(SELLER + "/proposals/prop_7")
    failure(e.value.verification, Refusal.ORIGIN_MISMATCH)
    assert e.value.response.json() == {"ok": True}


def test_verify_only_refuses_an_unsigned_response(seller_key: Ed25519Key) -> None:
    resolver = _seller_directory(seller_key)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    with SyncRuntime(resolver) as runtime:
        auth = WebBotAuth.verify_only(
            ResponseVerifier(resolver, keys=CONFORMANCE_KEYS), runtime=runtime
        )
        with auth.client(transport=transport) as c:
            with pytest.raises(ResponseNotVerified) as e:
                c.get(SELLER + "/proposals/prop_7")
    assert isinstance(e.value.verification, Unsigned) and e.value.response.json() == {}


def test_an_auth_must_sign_or_verify() -> None:
    with pytest.raises(ValueError):
        WebBotAuth(None)


def test_directory_middleware_and_publishing_take_max_age() -> None:
    zero = PublishPolicy(max_age=0)
    _, headers, _ = publish_directory(SELLER_ORIGIN, [w.seller_key()], now=T0, policy=zero)
    assert headers["Cache-Control"] == "max-age=0"
    publish = PublishPolicy(max_age=60)
    app = DirectoryMiddleware(
        FastAPI(), DirectoryPublisher(SELLER_IDENTITY.origin, [w.seller_key()], policy=publish)
    )
    assert w.run(_directory(app)).headers["cache-control"] == "max-age=60"
    with pytest.raises(ConfigurationError):
        PublishPolicy(max_age=-5)
