"""What the httpx auth does with each answer a seller, a verifier, or a proxy sends.

A buyer's :class:`WebBotAuth` always hands back the response, with what it
found out about it. It verifies a signed response, passes an unsigned refusal
or stream through with its real status, and raises
:class:`ResponseNotVerified`, carrying the response, only for an unverified
``2xx`` when it requires signed responses. It signs each wire hop for its own
URL, so a redirect never carries a signature to another URI. Each test drives
a real httpx client against a real ASGI app or a mock transport.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Awaitable, Callable

import httpx
import pytest
from fastapi import FastAPI

from iab_agentic_primitives.web_bot_auth import (
    CONFORMANCE_KEYS,
    ConfigurationError,
    DirectoryResolver,
    HTTPRequest,
    HTTPResponse,
    MemoryNonceStore,
    NonceStoreUnavailable,
    Origin,
    Refusal,
    RequestSigner,
    RequestVerifier,
    ResponseSigner,
    ResponseVerifier,
    Seconds,
    ServerIdentity,
    StaticKeyResolver,
)
from iab_agentic_primitives.web_bot_auth.asgi import (
    ASGIApp,
    Receive,
    RequirePolicy,
    RequireSignatureMiddleware,
    Scope,
    Send,
    SigningPolicy,
)
from iab_agentic_primitives.web_bot_auth.fetcher import HttpxDirectoryFetcher
from iab_agentic_primitives.web_bot_auth.httpx import (
    ResponseNotVerified,
    Unsigned,
    Verification,
    VerificationFailed,
    WebBotAuth,
    sent_request,
    verification,
    verified_response,
)
from iab_agentic_primitives.web_bot_auth.rules import Streaming

from . import support as w
from .support import BUYER, SELLER, SELLER_IDENTITY, SELLER_ORIGIN

COMMIT_PATH = "/proposals/prop_7/commit"
COMMITTED = {"status": "committed", "id": "c_1"}


def _auth(
    resolver: StaticKeyResolver | DirectoryResolver | None = None, *, strict: bool = True
) -> WebBotAuth:
    """The buyer's auth: it signs, and verifies with ``resolver`` (the seller's key by default).

    ``strict`` requires signed responses, as the auth does by default.
    """
    keys = StaticKeyResolver({SELLER_ORIGIN: [w.seller_key()]}) if resolver is None else resolver
    signer = RequestSigner.of(w.buyer_key(), BUYER)
    return WebBotAuth(
        signer, ResponseVerifier(keys, keys=CONFORMANCE_KEYS), require_signed_response=strict
    )


def _mock(answer: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(answer)


def _received(request: httpx.Request) -> HTTPRequest:
    return HTTPRequest(
        request.method, str(request.url), request.headers.multi_items(), request.content
    )


async def _post(auth: WebBotAuth, transport: httpx.AsyncBaseTransport, url: str) -> httpx.Response:
    async with auth.async_client(transport=transport) as c:
        return await c.post(url, json={"entry": 1})


def _not_verified(call: Awaitable[httpx.Response]) -> ResponseNotVerified:
    with pytest.raises(ResponseNotVerified) as e:
        w.run(call)
    return e.value


def _failed(found: Verification | None, reason: Refusal) -> None:
    assert isinstance(found, VerificationFailed) and found.reason is reason, found


async def _committing(scope: Scope, receive: Receive, send: Send) -> None:
    headers = [(b"content-type", b"application/json")]
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    await send({"type": "http.response.body", "body": json.dumps(COMMITTED).encode()})


# ── one test of whether a response is signed ──────────────────────────────


CAVAGE = 'keyId="k",algorithm="hs2019",headers="date",signature="abc"'


def test_a_response_with_another_schemes_signature_is_unsigned() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=COMMITTED, headers={"Signature": CAVAGE})

    optional = w.run(_post(_auth(strict=False), _mock(answer), SELLER))
    assert optional.json() == COMMITTED and isinstance(verification(optional), Unsigned)
    refused = _not_verified(_post(_auth(), _mock(answer), SELLER))
    assert isinstance(refused.verification, Unsigned)


def test_a_signed_response_missing_its_signature_member_fails_and_is_never_unsigned() -> None:
    # Signature-Input and Signature-Agent for a web-bot-auth sig9, with the Signature
    # dropped on the way: a broken signature, not an unsigned answer.
    agent = f'sig9="{SELLER_ORIGIN.directory_url}"'
    signature_input = (
        'sig9=("@status" "signature-agent";key="sig9");created=1;expires=2;'
        'keyid="k";alg="ed25519";tag="web-bot-auth"'
    )

    def answer(request: httpx.Request) -> httpx.Response:
        headers = {"Signature-Input": signature_input, "Signature-Agent": agent}
        return httpx.Response(200, json=COMMITTED, headers=headers)

    optional = w.run(_post(_auth(strict=False), _mock(answer), SELLER))
    _failed(verification(optional), Refusal.SIGNATURE_MISSING)
    _failed(
        _not_verified(_post(_auth(), _mock(answer), SELLER)).verification, Refusal.SIGNATURE_MISSING
    )


# ── the responder's own origin ────────────────────────────────────────────


def test_a_seller_on_an_idn_host_verifies_as_its_own_origin() -> None:
    seller = Origin.parse("https://xn--bcher-kva.example")
    signer = ResponseSigner.of(w.seller_key(), seller)

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.dumps(COMMITTED).encode()
        signed = signer.sign(HTTPResponse(200, w.JSON, body, _received(request)))
        return httpx.Response(200, headers=signed.headers.items(), content=body)

    auth = _auth(StaticKeyResolver({seller: [w.seller_key()]}))
    # httpx reads the host as bücher.example; the identity is its A-label form.
    response = w.run(_post(auth, _mock(answer), "https://bücher.example/commit"))
    verified = verified_response(response)
    assert verified is not None and verified.origin == seller and verified.bound


def test_a_response_verifier_holds_a_responder_to_the_host_the_request_went_to() -> None:
    request = w.sign_request()  # to the seller
    response = w.sign_response(request, ResponseSigner.of(w.other_key(), w.OTHER))
    verifier = ResponseVerifier(
        StaticKeyResolver({w.OTHER_ORIGIN: [w.other_key()]}),
        clock=lambda: w.T0,
        keys=CONFORMANCE_KEYS,
    )
    w.expect_refusal(lambda: verifier.verify(response), Refusal.ORIGIN_MISMATCH)
    assert w.run(verifier.verify(response, expected_origin=None)).origin == w.OTHER_ORIGIN


# ── the seller's answer is never lost ─────────────────────────────────────


def test_a_commit_the_buyer_cant_verify_still_reaches_the_buyer() -> None:
    commits: list[int] = []

    async def api(scope: Scope, receive: Receive, send: Send) -> None:
        commits.append(1)
        await _committing(scope, receive, send)

    seller = httpx.ASGITransport(app=w.seller_signing(api))

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("the directory host is unreachable")

    def unreachable() -> DirectoryResolver:
        return DirectoryResolver(HttpxDirectoryFetcher(transport=_mock(down)))

    refused = _not_verified(_post(_auth(unreachable()), seller, SELLER + COMMIT_PATH))
    _failed(refused.verification, Refusal.DIRECTORY_UNAVAILABLE)
    assert refused.response.status_code == 200 and refused.response.json() == COMMITTED
    optional = w.run(_post(_auth(unreachable(), strict=False), seller, SELLER + COMMIT_PATH))
    assert optional.json() == COMMITTED
    _failed(verification(optional), Refusal.DIRECTORY_UNAVAILABLE)
    assert len(commits) == 2


class _Down:
    async def check_and_record(self, origin: Origin, nonce: str, ttl: Seconds) -> bool:
        raise NonceStoreUnavailable("the store is down")


def test_an_unsigned_refusal_reaches_the_buyer_with_its_own_status() -> None:
    # The seller verifies outside its response signing, so the refusal answers unsigned.
    verifier = RequestVerifier(
        StaticKeyResolver({BUYER: [w.buyer_key()]}),
        identity=SELLER_IDENTITY,
        nonces=_Down(),
        keys=CONFORMANCE_KEYS,
    )
    edge = w.seller_edge(verifier)
    app = RequireSignatureMiddleware(w.seller_signing(_committing, edge), edge)
    response = w.run(_post(_auth(), httpx.ASGITransport(app=app), SELLER + COMMIT_PATH))
    assert response.status_code == 503 and response.headers["retry-after"] == "1"
    assert response.json()["error"] == "nonce-store-unavailable"
    assert isinstance(verification(response), Unsigned)


def test_the_servers_own_unsignable_500_and_a_proxys_502_pass_through() -> None:
    async def no_content_type(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"hello"})

    unsignable = httpx.ASGITransport(app=w.seller_signing(no_content_type))
    response = w.run(_post(_auth(), unsignable, SELLER + COMMIT_PATH))
    assert response.status_code == 500 and response.json()["error"] == "response-unsignable"
    assert isinstance(verification(response), Unsigned)
    proxy = _mock(lambda request: httpx.Response(502, text="bad gateway"))
    response = w.run(_post(_auth(), proxy, SELLER + COMMIT_PATH))
    assert response.status_code == 502 and response.text == "bad gateway"


def test_a_sync_client_without_a_runtime_is_refused_before_it_sends() -> None:
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={})

    with pytest.raises(ConfigurationError, match="SyncRuntime"):
        _auth().client(transport=_mock(answer))
    assert sent == []


def test_the_configuration_is_not_an_httpx_auth_a_client_could_bypass() -> None:
    # The library builds the client, so the signing transport is its one transport.
    with pytest.raises(TypeError):
        httpx.AsyncClient(auth=_auth())  # type: ignore[arg-type]


@pytest.mark.parametrize("option", ["transport_factory", "mounts", "proxy", "trust_env", "app"])
def test_a_setting_that_routes_around_the_signing_transport_is_refused(option: str) -> None:
    options: dict[str, object] = {option: None}
    with pytest.raises(ConfigurationError, match=option):
        _auth().async_client(**options)  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError, match=option):
        WebBotAuth(RequestSigner.of(w.buyer_key(), BUYER)).client(**options)  # type: ignore[arg-type]


# ── a live event stream ───────────────────────────────────────────────────


def test_an_event_stream_arrives_as_it_is_sent() -> None:
    port = w.free_port()
    identity = ServerIdentity.from_config(f"http://127.0.0.1:{port}")
    released, finished = threading.Event(), threading.Event()

    async def events(scope: Scope, receive: Receive, send: Send) -> None:
        headers = [(b"content-type", b"text/event-stream")]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": b"data: 1\n\n", "more_body": True})
        await asyncio.to_thread(released.wait, 5)  # the next event comes later
        finished.set()
        await send({"type": "http.response.body", "body": b"data: 2\n\n"})

    async def read() -> tuple[bytes, bool, bytes, Verification | None]:
        async with _auth().async_client() as c:
            async with c.stream("GET", identity.base_url + "/proposals/prop_7/events") as r:
                chunks = r.aiter_bytes()
                first = await anext(chunks)
                live = not finished.is_set()
                released.set()
                rest = b"".join([chunk async for chunk in chunks])
                return first, live, rest, verification(r)

    with w.serving(w.seller_signing(events, w.seller_edge(identity=identity)), port):
        first, live, rest, found = w.run(read())
    assert first == b"data: 1\n\n" and live, "the first event waited for the stream to end"
    assert rest == b"data: 2\n\n" and isinstance(found, Unsigned)


#: A seller that also streams NDJSON, and the rule its buyers configure to match.
NDJSON = Streaming(("text/event-stream", "application/x-ndjson"))


async def _ndjson(scope: Scope, receive: Receive, send: Send) -> None:
    headers = [(b"content-type", b"application/x-ndjson")]
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    await send({"type": "http.response.body", "body": b'{"n":1}\n{"n":2}\n'})


def test_a_client_takes_unsigned_the_streams_its_seller_names_in_the_same_rule() -> None:
    seller = w.seller_signing(_ndjson, policy=SigningPolicy(streaming=NDJSON))
    signer = RequestSigner.of(w.buyer_key(), BUYER)
    keys = StaticKeyResolver({SELLER_ORIGIN: [w.seller_key()]})
    matched = WebBotAuth(signer, ResponseVerifier(keys, keys=CONFORMANCE_KEYS), streaming=NDJSON)

    async def get(auth: WebBotAuth) -> httpx.Response:
        async with auth.async_client(transport=httpx.ASGITransport(app=seller)) as c:
            return await c.get(SELLER + "/proposals/prop_7/lines")

    response = w.run(get(matched))
    assert response.status_code == 200 and response.content == b'{"n":1}\n{"n":2}\n'
    assert isinstance(verification(response), Unsigned)
    # A client on the default rule doesn't know NDJSON streams, so it refuses the unsigned 200.
    refused = _not_verified(get(_auth()))
    assert isinstance(refused.verification, Unsigned)


def test_a_signed_head_response_verifies_without_the_body_the_server_drops() -> None:
    port = w.free_port()
    identity = ServerIdentity.from_config(f"http://127.0.0.1:{port}")
    api = FastAPI()

    @api.api_route("/proposals/prop_7", methods=["GET", "HEAD"])
    async def proposal() -> dict[str, str]:
        return COMMITTED

    async def head_and_get() -> tuple[httpx.Response, httpx.Response]:
        keys = StaticKeyResolver({identity.origin: [w.seller_key()]})
        async with _auth(keys).async_client() as c:
            url = identity.base_url + "/proposals/prop_7"
            return await c.head(url), await c.get(url)

    with w.serving(w.seller_signing(api, w.seller_edge(identity=identity)), port):
        head, get = w.run(head_and_get())
    assert head.status_code == 200 and head.content == b""  # uvicorn sent no body
    verified = verified_response(head)
    assert verified is not None and verified.bound and not verified.body_covered
    got = verified_response(get)
    assert got is not None and got.body_covered and get.json() == COMMITTED


# ── the redirect boundary ─────────────────────────────────────────────────


def _redirecting_seller() -> tuple[ASGIApp, list[str]]:
    """The seller, verifying every request; a route without its slash redirects to it."""
    api = FastAPI()
    seen: list[str] = []

    @api.post(COMMIT_PATH)
    async def commit() -> dict[str, str]:
        return COMMITTED

    verifier = RequestVerifier(
        StaticKeyResolver({BUYER: [w.buyer_key()]}),
        identity=SELLER_IDENTITY,
        nonces=MemoryNonceStore(),
        keys=CONFORMANCE_KEYS,
    )
    edge = w.seller_edge(verifier)
    required = RequireSignatureMiddleware(api, edge, policy=RequirePolicy(("/proposals",)))

    async def logged(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(str(scope.get("path")))
        await required(scope, receive, send)

    return w.seller_signing(logged, edge), seen


def test_a_redirect_is_followed_by_signing_the_next_request_afresh() -> None:
    app, seen = _redirecting_seller()

    async def follow() -> tuple[httpx.Response, httpx.Response]:
        async with _auth().async_client(transport=httpx.ASGITransport(app=app)) as c:
            moved = await c.post(SELLER + COMMIT_PATH + "/", json={"entry": 1})
            assert moved.next_request is not None
            return moved, await c.send(moved.next_request)

    moved, final = w.run(follow())
    assert moved.status_code == 307 and verified_response(moved) is not None
    assert final.json() == COMMITTED
    verified = verified_response(final)
    assert verified is not None and verified.bound
    assert seen == [COMMIT_PATH + "/", COMMIT_PATH]


def test_a_followed_redirect_is_signed_afresh_for_its_own_url() -> None:
    # The seller verifies every hop: a signature resent to another URL fails there.
    app, seen = _redirecting_seller()

    async def follow() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with _auth().async_client(transport=transport, follow_redirects=True) as c:
            return await c.post(SELLER + COMMIT_PATH + "/", json={"entry": 1})

    final = w.run(follow())
    assert final.json() == COMMITTED and [r.status_code for r in final.history] == [307]
    verified = verified_response(final)
    assert verified is not None and verified.bound
    assert verified_response(final.history[0]) is not None
    assert seen == [COMMIT_PATH + "/", COMMIT_PATH]


def test_a_redirect_to_another_host_never_carries_the_sellers_signature() -> None:
    received: list[HTTPRequest] = []
    elsewhere = "https://elsewhere.example"

    def answer(request: httpx.Request) -> httpx.Response:
        received.append(_received(request))
        if request.url.host == "seller.example.com":
            return httpx.Response(307, headers={"Location": elsewhere + "/collect"})
        return httpx.Response(200, json={})

    async def send() -> httpx.Response:
        auth = _auth(strict=False)
        async with auth.async_client(transport=_mock(answer), follow_redirects=True) as c:
            return await c.post(SELLER + COMMIT_PATH, json={"entry": 1})

    final = w.run(send())
    to_seller, to_elsewhere = received
    assert to_elsewhere.url == elsewhere + "/collect"
    assert to_seller.headers["signature"] != to_elsewhere.headers["signature"]
    # What reached the other host is a signature for its own URL, and no other.
    there = RequestVerifier(
        StaticKeyResolver({BUYER: [w.buyer_key()]}),
        identity=ServerIdentity.from_config(elsewhere),
        nonces=MemoryNonceStore(),
        keys=CONFORMANCE_KEYS,
    )
    assert w.run(there.verify(to_elsewhere)).origin == Origin.parse(BUYER)
    moved = HTTPRequest(to_seller.method, to_elsewhere.url, to_seller.headers, to_seller.body)
    w.expect_refusal(lambda: there.verify(moved), Refusal.SIGNATURE_INVALID)
    sent = sent_request(final)
    assert sent is not None and sent.url == to_elsewhere.url
    assert sent.headers["signature"] == to_elsewhere.headers["signature"]


def test_a_redirect_elsewhere_carries_none_of_the_signature() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"Location": "https://elsewhere.example/commit"})

    async def send() -> httpx.Response:
        async with _auth().async_client(transport=_mock(answer)) as c:
            return await c.post(SELLER + COMMIT_PATH, json={"entry": 1})

    moved = w.run(send())
    assert moved.status_code == 307 and isinstance(verification(moved), Unsigned)
    sent = sent_request(moved)
    assert sent is not None and sent.headers["signature-input"]  # the hop itself was signed
    follow = moved.next_request
    assert follow is not None and follow.url.host == "elsewhere.example"
    for name in ("signature", "signature-input", "signature-agent", "content-digest"):
        assert name not in follow.headers, name
