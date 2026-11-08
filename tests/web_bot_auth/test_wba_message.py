"""The message model as callers use it: header input, signing that leaves the caller's
message alone, and the lossless conformance documents.

This module type-checks under strict mypy in blocking mode.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Iterator

import pytest

from iab_agentic_primitives.web_bot_auth import (
    HeaderLines,
    HTTPRequest,
    HTTPResponse,
    RequestSigner,
    ResponseSigner,
)
from iab_agentic_primitives.web_bot_auth.conformance import LibraryAdapter
from iab_agentic_primitives.web_bot_auth.conformance.models import RequestDoc, dump, read
from iab_agentic_primitives.web_bot_auth.conformance.signing import sign_directory

from . import support as w

# ── header input ──────────────────────────────────────────────────────────


def test_a_message_takes_only_str_header_lines() -> None:
    # Raw ASGI header bytes must be decoded by the caller, never signed as they are.
    with pytest.raises(TypeError):
        HTTPRequest("GET", w.COMMIT_URL, [(b"a", b"b")])  # type: ignore[list-item]
    with pytest.raises(TypeError):
        HTTPRequest("GET", w.COMMIT_URL, {"a": 1})  # type: ignore[arg-type]


def _lines() -> Iterator[tuple[str, str]]:
    yield ("Content-Type", "application/json")
    yield ("X-Trace", "t-1")


def test_a_generator_of_headers_signs_the_same_as_a_list() -> None:
    signer = RequestSigner.of(w.buyer_key(), w.BUYER)
    nonce = "A" * 86
    from_list = HTTPRequest("POST", w.COMMIT_URL, list(_lines()), w.COMMIT_BODY)
    from_generator = HTTPRequest("POST", w.COMMIT_URL, HeaderLines(_lines()), w.COMMIT_BODY)
    one = signer.sign(from_list, now=w.T0, nonce=nonce)
    two = signer.sign(from_generator, now=w.T0, nonce=nonce)
    assert one == two
    assert one.headers.get_all("x-trace") == ("t-1",)
    verifier = w.request_verifier()
    assert asyncio.run(verifier.verify(two)).origin == w.BUYER_ORIGIN


def test_a_response_to_a_request_with_generated_headers_binds() -> None:
    request = w.signed_commit(w.buyer_key())
    generated = HTTPRequest(
        request.method, request.url, HeaderLines(iter(request.headers.items())), request.body
    )
    signer = ResponseSigner.of(w.seller_key(), w.SELLER)
    one = signer.sign(HTTPResponse(200, w.JSON, w.COMMIT_RESULT_BODY, request), now=w.T0)
    two = signer.sign(HTTPResponse(200, w.JSON, w.COMMIT_RESULT_BODY, generated), now=w.T0)
    assert one.headers == two.headers
    verified = asyncio.run(w.response_verifier().verify(two))
    assert verified.bound


def test_signing_returns_a_new_message_and_keeps_the_callers() -> None:
    request = HTTPRequest("POST", w.COMMIT_URL, w.JSON, w.COMMIT_BODY)
    signed = RequestSigner.of(w.buyer_key(), w.BUYER).sign(request, now=w.T0)
    assert request.headers == HeaderLines(w.JSON)
    assert signed.headers.items()[:1] == request.headers.items()
    assert [n for n, _ in signed.headers.items()[1:]] == [
        "Content-Digest",
        "Signature-Agent",
        "Signature-Input",
        "Signature",
    ]


# ── the conformance documents ─────────────────────────────────────────────

_BINARY = b"\xff\xfe\x00binary"
_REPEATED = [["Content-Type", "application/octet-stream"], ["X-Trace", "a"], ["X-Trace", "b"]]


def _op(op: str, **fields: object) -> str:
    return json.dumps({"op": op, **fields})


def test_a_binary_body_and_repeated_lines_round_trip_through_the_adapter() -> None:
    adapter = LibraryAdapter()
    request = {
        "method": "POST",
        "url": w.COMMIT_URL,
        "headers": _REPEATED,
        "body": {"base64": base64.b64encode(_BINARY).decode()},
    }
    jwk = w.buyer_key().private_jwk()
    key = {name: jwk[name] for name in ("kty", "crv", "x", "d")}
    signed = json.loads(
        adapter.serve(_op("sign_request", key=key, origin=w.BUYER, request=request, now=w.T0))
    )
    assert signed["headers"][:3] == _REPEATED and signed["body"] == request["body"]
    directory = sign_directory([w.buyer_key()], w.BUYER, w.T0)
    verify = _op(
        "verify_request",
        request=signed,
        authority="seller.example.com",
        directories={w.BUYER: dump(directory)},
        now=w.T0,
    )
    assert json.loads(adapter.serve(verify)) == {
        "ok": True,
        "identity": w.BUYER,
        "keyid": w.buyer_key().thumbprint,
        "body_covered": True,
    }


def test_a_document_is_frozen_hashable_and_lossless() -> None:
    signed = RequestSigner.of(w.buyer_key(), w.BUYER).sign(
        HTTPRequest("POST", w.COMMIT_URL, [(name, value) for name, value in _REPEATED], _BINARY),
        now=w.T0,
    )
    document = RequestDoc.of(signed)
    assert read(RequestDoc, json.dumps(dump(document))) == document
    assert document.message() == signed
    assert hash(document) == hash(RequestDoc.of(signed))
    with pytest.raises(TypeError):
        document.headers["X-Trace"] = "c"  # type: ignore[index]


def test_a_document_stores_a_utf_8_body_as_text() -> None:
    document = dump(RequestDoc.of(w.signed_commit(w.buyer_key())))
    assert isinstance(document, dict) and document["body"] == w.COMMIT_BODY.decode()
