"""The ASGI edges, driven as an ASGI server drives them: pathsend, the body cap, optional mode."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from starlette.responses import FileResponse

from iab_agentic_primitives.web_bot_auth import (
    CONFORMANCE_KEYS,
    DIRECTORY_PATH,
    DirectoryPublisher,
    DirectoryResolver,
    Ed25519Key,
    HTTPRequest,
    MemoryNonceStore,
    NonceStoreUnavailable,
    Origin,
    RequestSigner,
    RequestVerifier,
    Seconds,
    StaticKeyResolver,
    VerifiedRequest,
    accept_signature,
    system_clock,
)
from iab_agentic_primitives.web_bot_auth.asgi import (
    MAX_BODY_BYTES,
    STATE_KEY,
    ASGIApp,
    DirectoryMiddleware,
    Message,
    OnUnsigned,
    Receive,
    RequirePolicy,
    RequireSignatureMiddleware,
    ResponseSigningMiddleware,
    Scope,
    Send,
    SigningPolicy,
    verified_request,
)
from iab_agentic_primitives.web_bot_auth.fetcher import HttpxDirectoryFetcher
from iab_agentic_primitives.web_bot_auth.httpx import EXTENSION, verified_response

from . import support as w

SELLER_URL = "https://seller.example.com"


def _scope(
    path: str,
    *,
    method: str = "GET",
    headers: Iterable[tuple[str, str]] = (),
    extensions: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
        "server": ("seller.example.com", 443),
        "extensions": extensions or {},
    }


async def _drive(app: ASGIApp, scope: Scope, receive: Receive) -> list[Message]:
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


def _start(sent: list[Message]) -> tuple[int, dict[str, str]]:
    start = sent[0]
    assert start["type"] == "http.response.start"
    status, lines = start["status"], start["headers"]
    assert isinstance(status, int) and isinstance(lines, list)
    return status, {k.decode(): v.decode() for k, v in lines}


def _body(message: Message) -> bytes:
    body = message["body"]
    assert isinstance(body, bytes)
    return body


# ── pathsend ──────────────────────────────────────────────────────────────


def test_a_file_response_under_pathsend_is_sent_as_a_signed_body(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_bytes(b'{"ok": true}')
    api = FastAPI()

    @api.get("/report")
    async def get_report() -> FileResponse:
        return FileResponse(report, media_type="application/json")

    app = w.seller_signing(api)
    scope = _scope("/report", extensions={"http.response.pathsend": {}})

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    sent = w.run(_drive(app, scope, receive))
    status, headers = _start(sent)
    assert status == 200 and "signature" in headers and "signature-input" in headers
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[1]["body"] == b'{"ok": true}'


def test_an_unexpected_message_after_the_start_is_unsignable() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.zerocopy", "file": 3})

    signing = w.seller_signing(app)

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    sent = w.run(_drive(signing, _scope("/x"), receive))
    status, _ = _start(sent)
    assert status == 500 and json.loads(_body(sent[1]))["error"] == "response-unsignable"


# ── the body cap and the header check ────────────────────────────────────


def _guarded(
    on_unsigned: OnUnsigned = "refuse", max_body_bytes: int = 1024 * 1024
) -> tuple[RequireSignatureMiddleware, list[VerifiedRequest | None]]:
    seen: list[VerifiedRequest | None] = []
    api = FastAPI()

    @api.post("/proposals/commit")
    async def commit(request: Request) -> dict[str, bool]:
        seen.append(verified_request(request.scope))
        await request.body()
        return {"ok": True}

    verifier = RequestVerifier(
        StaticKeyResolver({w.BUYER: [w.buyer_key()]}),
        identity=w.SELLER_IDENTITY,
        nonces=MemoryNonceStore(),
        keys=CONFORMANCE_KEYS,
    )
    app = RequireSignatureMiddleware(
        api,
        w.seller_edge(verifier),
        policy=RequirePolicy(on_unsigned=on_unsigned, max_body_bytes=max_body_bytes),
    )
    return app, seen


def test_an_unsigned_10_mb_post_is_refused_before_its_body_is_read() -> None:
    app, seen = _guarded()
    reads: list[int] = []

    async def receive() -> Message:
        reads.append(1)
        raise AssertionError("the body was read")

    size = 10 * 1024 * 1024
    scope = _scope(
        "/proposals/commit",
        method="POST",
        headers=[("content-type", "application/json"), ("content-length", str(size))],
    )
    sent = w.run(_drive(app, scope, receive))
    status, headers = _start(sent)
    assert status == 403 and json.loads(_body(sent[1]))["error"] == "signature-missing"
    assert headers["accept-signature"] == accept_signature(has_body=True)
    assert reads == [] and seen == []


def _signed(body: bytes = b'{"entry": 1}') -> HTTPRequest:
    request = HTTPRequest("POST", SELLER_URL + "/proposals/commit", w.JSON, body)
    return RequestSigner.of(w.buyer_key(), w.BUYER).sign(request, now=system_clock())


def _post(app: ASGIApp, request: HTTPRequest) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport) as client:
            return await client.post(
                request.url, headers=list(request.headers.items()), content=request.body
            )

    return w.run(send())


def test_a_signed_body_over_the_limit_is_413() -> None:
    app, seen = _guarded(max_body_bytes=8)
    response = _post(app, _signed(b'{"entry": 12345}'))
    assert response.status_code == 413 and response.json()["error"] == "body-too-large"
    assert seen == []


class _DownStore:
    async def check_and_record(self, origin: Origin, nonce: str, ttl: Seconds) -> bool:
        raise NonceStoreUnavailable("redis at 10.0.0.5:6379 refused the connection")


def _unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("[Errno 8] nodename nor servname known: billing.corp.internal")


def test_a_refusal_body_carries_the_reason_and_status_and_no_detail() -> None:
    # A peer signs with a key of its own, naming a host the verifier can't reach.
    resolver = DirectoryResolver(HttpxDirectoryFetcher(transport=httpx.MockTransport(_unreachable)))
    verifier = RequestVerifier(resolver, identity=w.SELLER_IDENTITY, nonces=MemoryNonceStore())
    app = RequireSignatureMiddleware(_not_found, w.seller_edge(verifier))
    probe = RequestSigner.of(Ed25519Key.generate(), "https://billing.corp.internal")
    request = HTTPRequest("POST", SELLER_URL + "/proposals/commit", w.JSON, b'{"entry": 1}')
    response = _post(app, probe.sign(request))
    assert response.status_code == 403
    assert response.json() == {"error": "directory-unavailable", "status": 403}


def test_a_nonce_store_failure_is_answered_without_the_stores_message() -> None:
    verifier = RequestVerifier(
        StaticKeyResolver({w.BUYER: [w.buyer_key()]}),
        identity=w.SELLER_IDENTITY,
        nonces=_DownStore(),
        keys=CONFORMANCE_KEYS,
    )
    app = RequireSignatureMiddleware(_not_found, w.seller_edge(verifier))
    response = _post(app, _signed())
    assert response.status_code == 503
    assert response.json() == {"error": "nonce-store-unavailable", "status": 503}


@pytest.mark.parametrize("on_unsigned", ["refuse", "pass"])
def test_a_forged_signature_is_refused_in_either_mode(on_unsigned: OnUnsigned) -> None:
    app, seen = _guarded(on_unsigned)
    forged = _signed()
    forged = HTTPRequest(forged.method, forged.url, forged.headers, b'{"entry": 2}')
    response = _post(app, forged)
    assert response.status_code == 403 and response.json()["error"] == "content-digest"
    assert seen == []


def test_optional_mode_verifies_a_signed_request_and_hands_over_the_credential() -> None:
    app, seen = _guarded("pass")
    assert _post(app, _signed()).status_code == 200
    assert len(seen) == 1
    credential = seen[0]
    assert credential is not None and str(credential.origin) == w.BUYER


def test_optional_mode_passes_an_unsigned_request_unread() -> None:
    app, seen = _guarded("pass")
    unsigned = HTTPRequest("POST", SELLER_URL + "/proposals/commit", w.JSON, b"{}")
    assert _post(app, unsigned).status_code == 200
    assert seen == [None]


#: Signature fields of no web-bot-auth signature: another scheme's, and a lone Signature.
NOT_SIGNED_UNDER_THE_PROFILE = [
    ("Signature", 'keyId="k",algorithm="hs2019",headers="date",signature="abc"'),
    ("Signature", "sig1=:AAAA:"),
]


@pytest.mark.parametrize("line", NOT_SIGNED_UNDER_THE_PROFILE, ids=["cavage", "signature-alone"])
def test_a_message_without_signature_input_is_unsigned(line: tuple[str, str]) -> None:
    # Optional mode passes it on unsigned; required mode asks it for a signature.
    request = HTTPRequest("POST", SELLER_URL + "/proposals/commit", [*w.JSON.items(), line], b"{}")
    app, seen = _guarded("pass")
    assert _post(app, request).status_code == 200 and seen == [None]
    app, seen = _guarded("refuse")
    refused = _post(app, request)
    assert refused.status_code == 403 and refused.json()["error"] == "signature-missing"
    assert refused.headers["accept-signature"] == accept_signature(has_body=True)
    assert seen == []


def test_on_unsigned_takes_refuse_or_pass() -> None:
    from iab_agentic_primitives.web_bot_auth import ConfigurationError

    with pytest.raises(ConfigurationError):
        _guarded("maybe")  # type: ignore[arg-type]


def _signed_scope(request: HTTPRequest, *, declare_length: bool) -> dict[str, object]:
    lines = [(k, v) for k, v in request.headers.items() if k.lower() != "content-length"]
    if declare_length:
        lines.append(("content-length", str(len(request.body))))
    return _scope("/proposals/commit", method="POST", headers=lines)


def test_a_declared_length_over_the_limit_is_413_before_any_read() -> None:
    app, seen = _guarded(max_body_bytes=8)
    reads: list[int] = []

    async def receive() -> Message:
        reads.append(1)
        raise AssertionError("the body was read")

    request = _signed(b'{"entry": 12345}')
    sent = w.run(_drive(app, _signed_scope(request, declare_length=True), receive))
    assert _start(sent)[0] == 413 and reads == [] and seen == []


def test_a_streamed_body_stops_being_read_past_the_limit() -> None:
    app, seen = _guarded(max_body_bytes=8)
    chunks = [b'{"entry"', b": 12345", b"}"]
    reads: list[int] = []

    async def receive() -> Message:
        reads.append(1)
        return {"type": "http.request", "body": chunks.pop(0), "more_body": bool(chunks)}

    request = _signed(b'{"entry": 12345}')
    sent = w.run(_drive(app, _signed_scope(request, declare_length=False), receive))
    assert _start(sent)[0] == 413 and len(reads) == 2 and seen == []


# ── the answer serializer and the buffered response ──────────────────────


async def _no_body() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}


def _directory_app() -> DirectoryMiddleware:
    return DirectoryMiddleware(
        _not_found, DirectoryPublisher(w.SELLER_ORIGIN, [w.seller_key()]), clock=lambda: w.T0
    )


async def _not_found(scope: Scope, receive: Receive, send: Send) -> None:
    await send({"type": "http.response.start", "status": 404, "headers": []})
    await send({"type": "http.response.body", "body": b""})


def test_head_on_the_directory_sends_the_get_headers_and_no_body() -> None:
    app = _directory_app()
    get = w.run(_drive(app, _scope(DIRECTORY_PATH), _no_body))
    head = w.run(_drive(app, _scope(DIRECTORY_PATH, method="HEAD"), _no_body))
    assert _start(head) == _start(get)
    assert int(_start(get)[1]["content-length"]) == len(_body(get[1])) > 0
    assert _body(head[1]) == b""


def _signing(app: ASGIApp, max_body_bytes: int = MAX_BODY_BYTES) -> ResponseSigningMiddleware:
    return w.seller_signing(app, policy=SigningPolicy(max_body_bytes=max_body_bytes))


def test_a_second_start_answers_500() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    sent = w.run(_drive(_signing(app), _scope("/x"), _no_body))
    assert _start(sent)[0] == 500
    assert json.loads(_body(sent[1]))["error"] == "response-unsignable"
    assert len(sent) == 2


def _json_app(chunks: list[bytes], declared: int | None = None) -> ASGIApp:
    headers = [(b"content-type", b"application/json")]
    if declared is not None:
        headers.append((b"content-length", str(declared).encode()))

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        for i, chunk in enumerate(chunks):
            more = i < len(chunks) - 1
            await send({"type": "http.response.body", "body": chunk, "more_body": more})

    return app


def test_a_response_body_over_the_cap_answers_500(tmp_path: Path) -> None:
    # Streamed past the cap, declared past it, and a file past it.
    big = tmp_path / "big.json"
    big.write_bytes(b"[" + b"0," * 600 + b"0]")
    files = FastAPI()

    @files.get("/big")
    async def get_big() -> FileResponse:
        return FileResponse(big, media_type="application/json")

    apps: list[tuple[ASGIApp, str]] = [
        (_json_app([b"[" + b"0," * 300, b"0" * 400 + b"]"]), "/x"),
        (_json_app([b"[]"], declared=2048), "/x"),
        (files, "/big"),
    ]
    for app, path in apps:
        sent = w.run(_drive(_signing(app, max_body_bytes=1000), _scope(path), _no_body))
        assert _start(sent)[0] == 500, path
        assert json.loads(_body(sent[1]))["error"] == "response-unsignable"
    within = w.run(
        _drive(_signing(_json_app([b"[1]"]), max_body_bytes=1000), _scope("/x"), _no_body)
    )
    assert _start(within)[0] == 200 and "signature" in _start(within)[1]


def test_the_directory_is_fetched_unsigned_when_signatures_are_required_outside_it() -> None:
    verifier = RequestVerifier(
        StaticKeyResolver({}),
        identity=w.SELLER_IDENTITY,
        nonces=MemoryNonceStore(),
        keys=CONFORMANCE_KEYS,
    )
    app = RequireSignatureMiddleware(_directory_app(), w.seller_edge(verifier))
    sent = w.run(_drive(app, _scope(DIRECTORY_PATH), _no_body))
    assert _start(sent)[0] == 200 and "signature" in _start(sent)[1]
    refused = w.run(_drive(app, _scope("/proposals"), _no_body))
    assert _start(refused)[0] == 403


def test_an_unsigned_get_is_asked_for_the_components_of_a_request_without_a_body() -> None:
    verifier = RequestVerifier(
        StaticKeyResolver({}),
        identity=w.SELLER_IDENTITY,
        nonces=MemoryNonceStore(),
        keys=CONFORMANCE_KEYS,
    )
    app = RequireSignatureMiddleware(_not_found, w.seller_edge(verifier))
    sent = w.run(_drive(app, _scope("/proposals/prop_7"), _no_body))
    status, headers = _start(sent)
    assert status == 403
    assert headers["accept-signature"] == accept_signature(has_body=False)


def test_each_accessor_reads_only_its_own_credential() -> None:
    request = w.run(w.request_verifier().verify(w.sign_request()))
    response_verifier = w.response_verifier()
    response = w.run(response_verifier.verify(w.sign_response(w.sign_request())))
    assert verified_request({"state": {STATE_KEY: response}}) is None
    assert verified_request({"state": {STATE_KEY: request}}) is request
    assert verified_response(httpx.Response(200, extensions={EXTENSION: request})) is None
    assert verified_response(httpx.Response(200, extensions={EXTENSION: response})) is response
