"""load_or_create: one key file, written once and atomically, even on concurrent first boots,
and read back only as the Ed25519 private key it holds."""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
from pathlib import Path

import pytest

from iab_agentic_primitives.web_bot_auth import (
    CONFORMANCE_KEYS,
    MemoryNonceStore,
    RequestSigner,
    RequestVerifier,
    StaticKeyResolver,
)
from iab_agentic_primitives.web_bot_auth.keyfile import load_or_create

from . import support as w


def test_load_or_create_writes_a_private_file_once(tmp_path: Path) -> None:
    path = tmp_path / "keys" / "agent.jwk"
    first = load_or_create(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(path.read_text())["kid"] == first.thumbprint
    again = load_or_create(path)
    assert again.thumbprint == first.thumbprint
    assert [p.name for p in path.parent.iterdir()] == ["agent.jwk"]
    # The key read back signs requests that verify under the key first published.
    signed = RequestSigner.of(again, w.BUYER).sign(w.commit(), now=w.T0)
    verifier = RequestVerifier(
        StaticKeyResolver({w.BUYER: [first.public_jwk()]}),
        identity=w.SELLER_IDENTITY,
        nonces=MemoryNonceStore(clock=lambda: w.T0),
        clock=lambda: w.T0,
        keys=CONFORMANCE_KEYS,
    )
    assert w.run(verifier.verify(signed)).keyid == first.thumbprint


def _mismatched() -> dict[str, object]:
    jwk: dict[str, object] = dict(w.buyer_key().private_jwk())
    jwk["x"] = w.other_key().public_jwk()["x"]
    return jwk


@pytest.mark.parametrize(
    "jwk",
    [
        {"kty": "OKP", "crv": "Ed448", "d": "AA"},
        {"kty": "EC", "crv": "P-256", "d": "AA"},
        {"kty": "OKP", "crv": "Ed25519"},
        _mismatched(),
    ],
    ids=["ed448", "p-256", "public-only", "x-of-another-key"],
)
def test_a_key_file_that_holds_anything_but_its_ed25519_private_key_is_refused(
    tmp_path: Path, jwk: dict[str, str]
) -> None:
    path = tmp_path / "agent.jwk"
    path.write_text(json.dumps(jwk))
    with pytest.raises(ValueError, match="Ed25519|does not match"):
        load_or_create(path)
    assert json.loads(path.read_text()) == jwk  # never overwritten


def _boot(
    path: str,
    barrier: multiprocessing.synchronize.Barrier,
    out: multiprocessing.Queue[tuple[str, str]],
) -> None:
    barrier.wait()
    try:
        out.put(("ok", load_or_create(path).thumbprint))
    except Exception as e:  # reported to the parent, which fails the test
        out.put(("error", repr(e)))


def test_eight_concurrent_first_boots_make_one_key(tmp_path: Path) -> None:
    path = tmp_path / "fresh" / "wba-key.jwk"
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(8)
    out = context.Queue()
    workers = [context.Process(target=_boot, args=(str(path), barrier, out)) for _ in range(8)]
    for worker in workers:
        worker.start()
    results = [out.get(timeout=60) for _ in workers]
    for worker in workers:
        worker.join(timeout=60)
    assert [status for status, _ in results] == ["ok"] * 8, results
    assert len({kid for _, kid in results}) == 1
    assert [p.name for p in path.parent.iterdir()] == ["wba-key.jwk"]
    assert json.loads(path.read_text())["kid"] == results[0][1]
