"""The URL every signature covers, ``@target-uri``, as it travels on the wire.

The signer signs the URL an HTTP client sends, the server edge rebuilds the
URL it received, and the verifier recomputes ``@target-uri`` and
``@authority`` from it, all under one rule (:func:`target_uri`). So a URL
with the characters clients send raw (``[ ] | ^ { }`` in a query), an
escape in the path, a default port, a mixed-case host, or dot segments is
signed, verified, and answered like any other, in process and on uvicorn.
"""

from __future__ import annotations

import json

import httpx
import pytest

from iab_agentic_primitives.web_bot_auth import (
    CONFORMANCE_KEYS,
    HTTPRequest,
    MemoryNonceStore,
    RequestSigner,
    RequestVerifier,
    ResponseVerifier,
    ServerIdentity,
    StaticKeyResolver,
    TargetURI,
    target_uri,
)
from iab_agentic_primitives.web_bot_auth.asgi import (
    ASGIApp,
    Receive,
    RequirePolicy,
    RequireSignatureMiddleware,
    Scope,
    Send,
    verified_request,
)
from iab_agentic_primitives.web_bot_auth.httpx import (
    WebBotAuth,
    sent_request,
    verified_response,
)
from iab_agentic_primitives.web_bot_auth.values import authority_of

from . import support as w
from .support import BUYER, BUYER_ORIGIN, SELLER, SELLER_IDENTITY

#: Each URL as a caller writes it, and as it travels on the wire: the WHATWG
#: serialization httpx and fetch send, normalized per RFC 3986 § 6.2.2.
WIRE = [
    (f"{SELLER}/p?ids[]=1", f"{SELLER}/p?ids[]=1"),
    (f"{SELLER}/p?f=a|b", f"{SELLER}/p?f=a|b"),
    (f"{SELLER}/p?q={{json}}", f"{SELLER}/p?q={{json}}"),
    (f"{SELLER}/p?q=a^b", f"{SELLER}/p?q=a^b"),
    (f"{SELLER}/proposals/a%20b", f"{SELLER}/proposals/a%20b"),
    ("https://seller.example.com:443/proposals/p1", f"{SELLER}/proposals/p1"),
    ("HTTPS://Seller.Example.COM/Proposals/p1", f"{SELLER}/Proposals/p1"),
    (f"{SELLER}/x/../proposals/./p1", f"{SELLER}/proposals/p1"),
    (f"{SELLER}/x/%2E%2e/p1", f"{SELLER}/p1"),
    (f"{SELLER}/proposals/a b?q=x y", f"{SELLER}/proposals/a%20b?q=x%20y"),
    (f"{SELLER}/a{{b}}`c?q='\"", f"{SELLER}/a%7Bb%7D%60c?q=%27%22"),
    (f"{SELLER}/p%zz?q=%zz", f"{SELLER}/p%zz?q=%zz"),
    (f"{SELLER}/a\\b?q=a\\b", f"{SELLER}/a/b?q=a\\b"),
    (f"{SELLER}/café?q=é", f"{SELLER}/caf%C3%A9?q=%C3%A9"),
    ("https://user:pw@seller.example.com/p?#terms", f"{SELLER}/p"),
    ("https://seller.example.com", f"{SELLER}/"),
    ("https://Bücher.example:8443/p", "https://xn--bcher-kva.example:8443/p"),
    ("http://[2001:DB8::1]:80/p", "http://[2001:db8::1]/p"),
]

#: The URLs the review found unsignable, unverifiable, or answered 500, as written.
REVIEWED = [written for written, _ in WIRE[:8]]


@pytest.mark.parametrize(("written", "wire"), WIRE)
def test_target_uri_is_the_url_as_it_travels_on_the_wire(written: str, wire: str) -> None:
    assert target_uri(written) == wire
    assert target_uri(wire) == wire
    # httpx sends the path and query exactly as the rule writes them.
    assert wire.endswith(httpx.URL(wire).raw_path.decode())
    # @authority follows the same rule.
    assert authority_of(written) == TargetURI.parse(wire).authority


@pytest.mark.parametrize("url", ["/p", "seller.example.com/p", "ftp://seller.example.com/p", ""])
def test_a_url_that_is_not_absolute_http_has_no_target_uri(url: str) -> None:
    with pytest.raises(ValueError, match="not an absolute http or https URL"):
        target_uri(url)


# ── signed, verified, and answered ────────────────────────────────────────


async def _echo(scope: Scope, receive: Receive, send: Send) -> None:
    verified = verified_request(scope)
    signed_by = None if verified is None else str(verified.origin)
    headers = [(b"content-type", b"application/json")]
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    await send(
        {"type": "http.response.body", "body": json.dumps({"signed_by": signed_by}).encode()}
    )


def _seller(identity: ServerIdentity = SELLER_IDENTITY) -> ASGIApp:
    """The seller: it verifies a signed request, passes an unsigned one, and signs every answer."""
    keys = StaticKeyResolver({BUYER_ORIGIN: [w.buyer_key()]})
    verifier = RequestVerifier(
        keys, identity=identity, nonces=MemoryNonceStore(), keys=CONFORMANCE_KEYS
    )
    edge = w.seller_edge(verifier)
    required = RequireSignatureMiddleware(_echo, edge, policy=RequirePolicy(on_unsigned="pass"))
    return w.seller_signing(required, edge)


def _response_verifier(identity: ServerIdentity = SELLER_IDENTITY) -> ResponseVerifier:
    return ResponseVerifier(
        StaticKeyResolver({identity.origin: [w.seller_key()]}), keys=CONFORMANCE_KEYS
    )


def _buyer(identity: ServerIdentity = SELLER_IDENTITY) -> WebBotAuth:
    """The buyer: it signs every request and verifies every answer and its binding."""
    return WebBotAuth(RequestSigner.of(w.buyer_key(), BUYER), _response_verifier(identity))


def _browser(identity: ServerIdentity = SELLER_IDENTITY) -> WebBotAuth:
    """A client that signs nothing and still verifies the seller's signed answer."""
    return WebBotAuth.verify_only(_response_verifier(identity))


@pytest.mark.parametrize("written", REVIEWED)
def test_the_buyers_client_signs_and_verifies_each_url_through_the_edge(written: str) -> None:
    async def get() -> httpx.Response:
        transport = httpx.ASGITransport(app=_seller())
        async with _buyer().async_client(transport=transport) as c:
            return await c.get(written)

    response = w.run(get())
    assert response.status_code == 200 and response.json() == {"signed_by": BUYER}
    verified = verified_response(response)
    assert verified is not None and verified.bound
    sent = sent_request(response)
    assert sent is not None and sent.url == target_uri(written)


@pytest.mark.parametrize("written", REVIEWED)
def test_a_request_signed_for_any_client_verifies_at_the_url_it_returns(written: str) -> None:
    signed = RequestSigner.of(w.buyer_key(), BUYER).sign(HTTPRequest("GET", written))
    assert signed.url == target_uri(written)

    async def get() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_seller())) as c:
            return await c.get(signed.url, headers=signed.headers.items())

    response = w.run(get())
    assert response.status_code == 200 and response.json() == {"signed_by": BUYER}


@pytest.mark.parametrize("written", REVIEWED)
def test_an_unsigned_request_to_each_url_gets_a_signed_answer(written: str) -> None:
    async def get() -> httpx.Response:
        transport = httpx.ASGITransport(app=_seller())
        async with _browser().async_client(transport=transport) as c:
            return await c.get(written)

    response = w.run(get())
    assert response.status_code == 200 and response.json() == {"signed_by": None}
    verified = verified_response(response)
    assert verified is not None and not verified.bound


# ── on a real server ──────────────────────────────────────────────────────

#: The paths and queries a real HTTP server receives raw, the review's 500s among them.
ON_THE_WIRE = [
    "/p?ids[]=1",
    "/p?f=a|b",
    "/p?q={json}",
    "/p?q=a^b",
    "/proposals/a%20b",
    "/p%zz?q=%zz",
    "/x/../proposals/./p1",
]


def test_behind_response_signing_on_uvicorn_every_url_is_answered_and_verified() -> None:
    port = w.free_port()
    identity = ServerIdentity.from_config(f"http://127.0.0.1:{port}")

    async def each(auth: WebBotAuth) -> list[httpx.Response]:
        async with auth.async_client() as c:
            return [await c.get(identity.base_url + path) for path in ON_THE_WIRE]

    with w.serving(_seller(identity), port):
        signed = w.run(each(_buyer(identity)))
        unsigned = w.run(each(_browser(identity)))
    for path, response in zip(ON_THE_WIRE, signed, strict=True):
        assert response.status_code == 200, (path, response.text)
        assert response.json() == {"signed_by": BUYER}, path
        verified = verified_response(response)
        assert verified is not None and verified.bound, path
    for path, response in zip(ON_THE_WIRE, unsigned, strict=True):
        assert response.status_code == 200, (path, response.text)
        assert response.json() == {"signed_by": None}, path
        assert verified_response(response) is not None, path
