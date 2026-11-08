"""Requests and documents the tests craft, each signed by the real signers.

:func:`craft` signs a request with any components and parameters, including
ones the profile refuses, raw, through the conformance interpreter. The rest give
the field lines a signer adds, the components the rules require in the
``scenarios.json`` spelling, and :func:`edited`, the one way a test tampers
with a signed request.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import TypedDict, Unpack

import httpx

from iab_agentic_primitives.web_bot_auth import (
    DIRECTORY_PATH,
    DirectoryResponse,
    Ed25519Key,
    HeaderLines,
    Headers,
    HTTPRequest,
    HTTPResponse,
    Origin,
    RequestSigner,
    RequestVerifier,
    ResponseSigner,
    UnixSeconds,
    VerifiedRequest,
)
from iab_agentic_primitives.web_bot_auth.conformance import vectors
from iab_agentic_primitives.web_bot_auth.conformance.models import (
    DirectoryDoc,
    RequestDoc,
    ResponseDoc,
    SignatureParameters,
    Vector,
    VerifyDirectory,
    VerifyRequest,
)
from iab_agentic_primitives.web_bot_auth.conformance.signing import (
    MessageDeparture,
    RawSigner,
    components_of,
)
from iab_agentic_primitives.web_bot_auth.rules import REQUEST, ComponentId
from iab_agentic_primitives.web_bot_auth.signer import SignerConfig

from . import (
    BUYER,
    COMMIT_BODY,
    COMMIT_RESULT_BODY,
    COMMIT_URL,
    JSON,
    SELLER,
    T0,
    run,
)


def edited(
    request: HTTPRequest,
    *,
    headers: Mapping[str, str | None] | None = None,
    body: bytes | None = None,
    url: str | None = None,
) -> HTTPRequest:
    """``request`` tampered with after signing: the one way a test changes a signed message.

    Each field in ``headers`` keeps its place with the new value, or goes when
    the value is ``None``; a field the request lacks is added at the end.
    ``body`` and ``url`` replace the request's own.
    """
    changes = {name.lower(): value for name, value in (headers or {}).items()}
    lines: list[tuple[str, str]] = []
    for name, value in request.headers:
        new = changes.pop(name.lower(), value)
        if new is not None:
            lines.append((name, new))
    lines.extend(
        (name, value)
        for name, value in (headers or {}).items()
        if name.lower() in changes and value is not None
    )
    return replace(
        request,
        headers=HeaderLines(lines),
        body=request.body if body is None else body,
        url=request.url if url is None else url,
    )


def spec(component: ComponentId) -> str:
    """The ``scenarios.json`` spelling of ``component``, such as ``signature-agent;key=sig1``."""
    params = "".join(f";{k}" if v is True else f";{k}={v}" for k, v in component.params)
    return component.name + params


def inner_list(components: Iterable[ComponentId]) -> str:
    """The serialized Inner List of ``components``, as ``Signature-Input`` carries it."""
    return "(" + " ".join(str(c) for c in components) + ")"


def post_components(label: str = "sig1") -> list[str]:
    """The components a request with a body covers, from the rules."""
    return [spec(c) for c in REQUEST.components(label, has_body=True)]


def get_components(label: str = "sig1") -> list[str]:
    """The components a request without a body covers, from the rules."""
    return [spec(c) for c in REQUEST.components(label, has_body=False)]


#: The components a body adds to a request signature.
BODY_COMPONENTS = tuple(
    c
    for c in REQUEST.components("sig1", has_body=True)
    if c not in REQUEST.components("sig1", has_body=False)
)
#: The rules' request components, with ``{label}`` for the signature label.
POST_COMPONENTS = post_components("{label}")
GET_COMPONENTS = get_components("{label}")


def vector_request(vector: Vector) -> RequestDoc:
    """The request a request or response vector's operation sends."""
    operation = vector.operation
    if isinstance(operation, VerifyDirectory):
        raise TypeError(f"{vector.name} sends a directory, not a request")
    return operation.request


def vector_message(vector: Vector) -> RequestDoc | ResponseDoc | DirectoryDoc:
    """The message a vector's operation is graded on: its response, or else its request."""
    operation = vector.operation
    return operation.request if isinstance(operation, VerifyRequest) else operation.response


_VECTORS = dict(vectors())
#: The signed commit request of the committed positive vector 001.
POSITIVE_001 = vector_request(_VECTORS["positive/001-single-signer-request"])
#: The seller's response of the committed positive vector 003, bound to 001's request.
POSITIVE_003 = vector_message(_VECTORS["positive/003-signed-response"])


class CraftOptions(TypedDict, total=False):
    """What :func:`craft` changes from the signed commit."""

    method: str
    url: str
    body: bytes
    headers: Mapping[str, str]
    agent: str
    label: str
    components: list[str]
    params: SignatureParameters
    now: UnixSeconds


def craft(key: Ed25519Key, **options: Unpack[CraftOptions]) -> HTTPRequest:
    """Signs a request with any components and parameters, including ones the profile refuses.

    ``params`` moves the signature's parameters (offsets for ``created`` and
    ``expires``; ``None`` omits one). ``agent`` is the ``Signature-Agent``
    value. It is signed raw, as the buyer's signer would but departing.
    """
    body = options.get("body", COMMIT_BODY)
    label = options.get("label", "sig1")
    defaults = POST_COMPONENTS if body else GET_COMPONENTS
    departure = MessageDeparture(
        components=components_of(options.get("components") or defaults, label),
        parameters=options.get("params", SignatureParameters()),
        signature_agent=options.get("agent"),
    )
    headers = options.get("headers", JSON if body else {})
    request = HTTPRequest(
        options.get("method", "POST"), options.get("url", COMMIT_URL), headers, body
    )
    config = SignerConfig(key, Origin.parse(BUYER), label=label)
    return RawSigner(config, departure).request(request, options.get("now", T0))


def signed_commit(
    key: Ed25519Key, origin: str = BUYER, *, now: UnixSeconds = T0, body: bytes = COMMIT_BODY
) -> HTTPRequest:
    """The commit request, signed."""
    return RequestSigner.of(key, origin).sign(HTTPRequest("POST", COMMIT_URL, JSON, body), now=now)


def signed_result(
    key: Ed25519Key, request: HTTPRequest, *, now: UnixSeconds = T0, status: int = 200
) -> tuple[HTTPRequest, HeaderLines]:
    """The seller's signed CommitResult to ``request``: the request and the response headers."""
    response = HTTPResponse(status, JSON, COMMIT_RESULT_BODY, request)
    return request, ResponseSigner.of(key, SELLER).sign(response, now=now).headers


def added_lines(before: HeaderLines, after: HeaderLines) -> dict[str, str]:
    """The field lines signing appended to ``before``."""
    assert after.items()[: len(before)] == before.items()
    return dict(after.items()[len(before) :])


class SignOptions(TypedDict, total=False):
    """The signing options :func:`request_added` and :func:`response_added` pass on."""

    now: UnixSeconds
    nonce: str


def request_added(
    signer: RequestSigner,
    method: str,
    url: str,
    headers: Mapping[str, str] | None = None,
    body: bytes = b"",
    **sign: Unpack[SignOptions],
) -> dict[str, str]:
    """The field lines ``signer`` adds to the request."""
    request = HTTPRequest(method, url, headers or {}, body)
    signed = signer.sign(request, now=sign.get("now"), nonce=sign.get("nonce"))
    return added_lines(request.headers, signed.headers)


def response_added(
    signer: ResponseSigner,
    status: int,
    headers: Mapping[str, str] | None,
    body: bytes,
    request: HTTPRequest,
    **sign: Unpack[SignOptions],
) -> dict[str, str]:
    """The field lines ``signer`` adds to the response."""
    response = HTTPResponse(status, headers or {}, body, request)
    return added_lines(response.headers, signer.sign(response, now=sign.get("now")).headers)


def fetched_from(response: httpx.Response, now: UnixSeconds) -> DirectoryResponse:
    """An ``httpx.Response`` from a directory route, as a fetcher returns it."""
    lines = response.headers.multi_items()
    return DirectoryResponse(str(response.url), response.status_code, lines, response.content, now)


def verify(
    verifier: RequestVerifier, request: HTTPRequest, *, now: UnixSeconds | None = None
) -> VerifiedRequest:
    """Verifies ``request``, at ``now`` when given."""
    return run(verifier.verify(request, now=now))


def fetched(
    status: int,
    headers: Headers,
    body: bytes,
    *,
    origin: Origin | str = BUYER,
    now: UnixSeconds = T0,
) -> DirectoryResponse:
    """The directory response ``(status, headers, body)`` as fetched from ``origin`` at ``now``."""
    return DirectoryResponse(f"{origin}{DIRECTORY_PATH}", status, headers, body, now)


__all__ = [
    "BODY_COMPONENTS",
    "GET_COMPONENTS",
    "POSITIVE_001",
    "POSITIVE_003",
    "POST_COMPONENTS",
    "CraftOptions",
    "SignOptions",
    "added_lines",
    "craft",
    "edited",
    "fetched",
    "fetched_from",
    "get_components",
    "inner_list",
    "post_components",
    "request_added",
    "response_added",
    "signed_commit",
    "signed_result",
    "spec",
    "vector_message",
    "vector_request",
    "verify",
]
