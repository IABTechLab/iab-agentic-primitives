"""Contract tests for the library behavior the module builds on.

The module keeps only the profile's rules and relies on these libraries for
the rest: http-message-signatures (RFC 9421 and RFC 9530), http-sf (Structured
Fields), joserfc (JWK thumbprints), and idna (host canonicalization). The
library behaviors it depends on are pinned here, through the module's own
signers and verifiers where it can, so an upgrade that changes one fails a
test instead of changing a verdict.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import http_sf
import idna
import pytest
from http_message_signatures import ContentDigest
from http_message_signatures.exceptions import InvalidSignature
from joserfc.jwk import OKPKey

from iab_agentic_primitives.web_bot_auth import DIRECTORY_PATH, EXTRA_MODULES, UnixSeconds
from iab_agentic_primitives.web_bot_auth._unchecked import signature_base
from iab_agentic_primitives.web_bot_auth.conformance.generate import VECTORS_REGISTRY

from .support import SELLER, at, request_verifier, run, seller_key, sign_request, sign_response


def test_the_window_is_left_to_the_profile() -> None:
    # Signed and verified a million seconds before the wall clock: the library
    # doesn't check the time, and the verifier's injected clock decides.
    then = at(-(10**6))
    signed = sign_request(now=then)
    assert run(request_verifier(now=UnixSeconds(then + 5)).verify(signed)).expires == then + 300


def test_req_and_key_component_parameters_resolve() -> None:
    request = sign_request()
    response = sign_response(request)
    base = signature_base(response, "sig1", seller_key()).decode()
    request_signature = (request.headers.get("signature") or "").split("=", 1)[1]
    assert f'"signature";req;key="sig1": {request_signature}' in base
    assert f'"signature-agent";key="sig1": "{SELLER}{DIRECTORY_PATH}"' in base


def test_content_digest_is_rfc_9530_sha_256() -> None:
    assert ContentDigest.create(b"") == "sha-256=:47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=:"
    ContentDigest.verify(ContentDigest.create(b"x"), b"x")
    with pytest.raises(InvalidSignature):
        ContentDigest.verify(ContentDigest.create(b"x"), b"y")


def test_structured_fields_refuse_duplicates_on_request() -> None:
    def refuse(key: str, context: str) -> None:
        raise ValueError(key)

    with pytest.raises(ValueError):
        http_sf.parse(b"a=1, a=2", tltype="dictionary", on_duplicate_key=refuse)
    members = http_sf.parse(b'a="x";type=directory', tltype="dictionary")
    assert isinstance(members, dict)
    member = members["a"]
    assert isinstance(member, tuple) and member[0] == "x"
    assert isinstance(member[1], dict) and isinstance(member[1]["type"], http_sf.Token)


def test_joserfc_thumbprint_is_rfc_7638_for_okp() -> None:
    x = "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"
    jwk: dict[str, str | list[str]] = {"kty": "OKP", "crv": "Ed25519", "x": x}
    assert (
        OKPKey.import_key(jwk).thumbprint() == "kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k"
    )  # gitleaks:allow


def test_idna_2008_non_transitional() -> None:
    assert idna.encode("straße.de", uts46=True, transitional=False) == b"xn--strae-oqa.de"
    assert (
        idna.encode("Seller.Example.COM", uts46=True, transitional=False) == b"seller.example.com"
    )


@pytest.mark.parametrize("host", ["xn--a.example", "xn--bcher-2pa.example"])
def test_idna_refuses_invalid_a_labels(host: str) -> None:
    with pytest.raises(idna.IDNAError):
        idna.encode(host, uts46=True, transitional=False)
    from iab_agentic_primitives.web_bot_auth.values import is_canonical_host

    assert is_canonical_host(host) is False
    assert is_canonical_host("xn--bcher-kva.example") is True


RFC_8037_X = "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"


@pytest.mark.parametrize(
    "x",
    [
        RFC_8037_X + "=",  # padded
        RFC_8037_X.replace("_", "/").replace("-", "+"),  # standard alphabet
        RFC_8037_X[:-1] + "p",  # a trailing bit altered
    ],
    ids=["padded", "standard-alphabet", "trailing-bit"],
)
def test_joserfc_refuses_a_non_canonical_x(x: str) -> None:
    with pytest.raises(ValueError):
        OKPKey.import_key({"kty": "OKP", "crv": "Ed25519", "x": x})
    from iab_agentic_primitives.web_bot_auth import thumbprint

    with pytest.raises(ValueError):
        thumbprint({"kty": "OKP", "crv": "Ed25519", "x": x})


# ── the module's boundary ─────────────────────────────────────────────────

_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE = _ROOT / "src" / "iab_agentic_primitives" / "web_bot_auth"


def test_the_extra_lists_the_libraries_pyproject_installs() -> None:
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"]
    requirements = project["optional-dependencies"]["wba"]
    names = sorted(re.split(r"[^A-Za-z0-9._-]", r, maxsplit=1)[0] for r in requirements)
    assert tuple(name.replace("-", "_") for name in names) == EXTRA_MODULES


def test_a_vendored_copy_imports_and_reads_its_own_vectors(tmp_path: Path) -> None:
    shutil.copytree(_PACKAGE, tmp_path / "vendored", ignore=shutil.ignore_patterns("__pycache__"))
    code = (
        "import vendored.conformance as c, vendored.extra as e; "
        "print(len(list(c.vectors())), len(e.EXTRA_MODULES))"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == [str(len(VECTORS_REGISTRY)), str(len(EXTRA_MODULES))]
