"""Log records: each refusal and each failed directory fetch, never a nonce or a body."""

from __future__ import annotations

import logging

import httpx
import pytest

from iab_agentic_primitives.web_bot_auth import (
    CONFORMANCE_KEYS,
    DirectoryResolver,
    MemoryNonceStore,
    Refusal,
    RequestVerifier,
)
from iab_agentic_primitives.web_bot_auth.fetcher import HttpxDirectoryFetcher
from iab_agentic_primitives.web_bot_auth.testing import directory_transport

from . import support as w
from .support import at, expect_refusal


def _verifier(transport: httpx.AsyncBaseTransport) -> RequestVerifier:
    return RequestVerifier(
        DirectoryResolver(HttpxDirectoryFetcher(transport=transport), clock=lambda: w.T0),
        identity=w.SELLER_IDENTITY,
        nonces=MemoryNonceStore(clock=lambda: w.T0),
        clock=lambda: w.T0,
        keys=CONFORMANCE_KEYS,
    )


def test_a_refusal_is_logged_with_its_status_reason_origin_and_keyid(
    caplog: pytest.LogCaptureFixture,
) -> None:
    key = w.buyer_key()
    verifier = _verifier(directory_transport({w.BUYER_ORIGIN: w.signed_directory(w.BUYER, [key])}))
    document = w.signed_commit(key)
    request = document
    w.run(verifier.verify(request))
    with caplog.at_level(logging.INFO, "iab_agentic_primitives.web_bot_auth"):
        # the same nonce again
        expect_refusal(lambda: verifier.verify(request), Refusal.NONCE_REPLAYED)
    (record,) = [r for r in caplog.records if "refused" in r.getMessage()]
    message = record.getMessage()
    assert "429 nonce-replayed" in message and w.BUYER in message and key.thumbprint in message
    nonce = (request.headers.get("signature-input") or "").split('nonce="')[1].split('"')[0]
    assert nonce not in message and w.COMMIT_BODY.decode() not in message


def test_a_failed_fetch_is_logged_with_the_origin_and_the_retry_time(
    caplog: pytest.LogCaptureFixture,
) -> None:
    verifier = _verifier(directory_transport({}))  # every directory is 404
    document = w.signed_commit(w.buyer_key())
    with caplog.at_level(logging.INFO, "iab_agentic_primitives.web_bot_auth"):
        request = document
        expect_refusal(lambda: verifier.verify(request), Refusal.DIRECTORY_STATUS)
    (fetch,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    message = fetch.getMessage()
    assert w.BUYER in message and str(at(60)) in message and "directory-status" in message
    assert any("403 directory-status" in r.getMessage() for r in caplog.records)
