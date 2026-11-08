"""The signed key directory: building, signing, and every verification rule."""

from __future__ import annotations

import base64
import json
from typing import TypedDict, Unpack

import pytest

from iab_agentic_primitives.web_bot_auth import (
    DIRECTORY_MEDIA_TYPE,
    ConfigurationError,
    DirectoryPublisher,
    Ed25519Key,
    FetchLimits,
    Headers,
    PublishPolicy,
    Refusal,
    RequestSigner,
    UnixSeconds,
    VerifiedDirectory,
    publish_directory,
    verify_directory,
)
from iab_agentic_primitives.web_bot_auth.conformance.fixtures import p256_public_jwk
from iab_agentic_primitives.web_bot_auth.conformance.signing import (
    DirectoryDeparture,
    sign_directory_body,
)
from iab_agentic_primitives.web_bot_auth.directory import build_directory
from iab_agentic_primitives.web_bot_auth.rules import DIRECTORY
from iab_agentic_primitives.web_bot_auth.values import OMIT

from . import support as w
from .support import BUYER, BUYER_ORIGIN, T0, at, expect_refusal
from .support import digest as content_digest


class Limits(TypedDict, total=False):
    """The :class:`FetchLimits` limits a test tightens."""

    max_bytes: int
    max_keys: int


def parse(
    status: int,
    headers: Headers,
    body: bytes,
    now: UnixSeconds = T0,
    **limits: Unpack[Limits],
) -> VerifiedDirectory:
    """Verifies ``(status, headers, body)`` as the buyer's directory, fetched at ``now``."""
    response = w.fetched(status, headers, body, now=now)
    return verify_directory(response, BUYER_ORIGIN, now, FetchLimits(**limits))


def raw(
    body: bytes,
    keys: list[Ed25519Key],
    now: UnixSeconds = T0,
    departure: DirectoryDeparture | None = None,
) -> dict[str, str]:
    """The headers of ``body`` signed by ``keys`` as the buyer's directory, raw."""
    signed = sign_directory_body(body, keys, BUYER_ORIGIN, now, departure or DirectoryDeparture())
    return dict(signed.headers.items())


def published(keys: list[Ed25519Key], **policy: int) -> tuple[dict[str, str], bytes]:
    """The buyer's directory as the profile publishes it: its headers and body."""
    answer = publish_directory(BUYER_ORIGIN, keys, now=T0, policy=PublishPolicy(**policy))
    return dict(answer.headers.items()), answer.body


def test_build_directory_publishes_public_jwks(buyer_key: Ed25519Key) -> None:
    document = json.loads(build_directory([buyer_key, p256_public_jwk()]))
    assert document["keys"][0] == buyer_key.public_jwk()
    assert "d" not in document["keys"][0]
    assert document["keys"][1]["kty"] == "EC"


# RFC 8037 Appendix A: the Ed25519 key of RFC 8032 test 1, its public x, and its thumbprint.
RFC_8032_SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
RFC_8037_X = "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"
RFC_8037_KID = "kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k"  # gitleaks:allow


def test_a_key_is_published_and_signs_under_its_rfc_8037_thumbprint() -> None:
    key = Ed25519Key.from_seed(RFC_8032_SEED)
    status, lines, body = w.signed_directory(BUYER, [key])
    headers = dict(lines.items())
    (jwk,) = json.loads(body)["keys"]
    assert jwk == {
        "kty": "OKP",
        "crv": "Ed25519",
        "kid": RFC_8037_KID,
        "x": RFC_8037_X,
        "use": "sig",
    }
    assert list(parse(status, headers, body).keys) == [RFC_8037_KID]
    signed = RequestSigner.of(key, BUYER).sign(w.commit(), now=T0)
    assert f'keyid="{RFC_8037_KID}"' in (signed.headers.get("signature-input") or "")
    verifier = w.request_verifier(resolver=w.directory_resolver({BUYER: [key]}))
    assert w.run(verifier.verify(signed)).keyid == RFC_8037_KID


def test_signed_directory_verifies(buyer_key: Ed25519Key) -> None:
    status, lines, body = w.signed_directory(BUYER, [buyer_key])
    headers = dict(lines.items())
    assert headers["Content-Type"] == DIRECTORY_MEDIA_TYPE
    assert headers["Content-Digest"] == content_digest(body)
    assert headers["Signature-Input"].startswith(f"key1={w.inner_list(DIRECTORY.components())};")
    assert 'tag="http-message-signatures-directory"' in headers["Signature-Input"]
    directory = parse(status, headers, body)
    assert list(directory.keys) == [buyer_key.thumbprint]
    assert directory.expires == at(86400)


def test_every_key_signs_and_the_earliest_expires_wins(
    buyer_key: Ed25519Key, other_key: Ed25519Key
) -> None:
    body = build_directory([buyer_key, other_key])
    headers = raw(body, [buyer_key, other_key])
    directory = parse(200, headers, body)
    assert set(directory.keys) == {buyer_key.thumbprint, other_key.thumbprint}
    later = raw(body, [other_key], at(100), DirectoryDeparture(expires=1000))
    mixed = dict(headers)
    mixed["Signature-Input"] = (
        headers["Signature-Input"].split(", ")[0]
        + ", "
        + later["Signature-Input"].replace("key1=", "key2=")
    )
    mixed["Signature"] = (
        headers["Signature"].split(", ")[0] + ", " + later["Signature"].replace("key1=", "key2=")
    )
    assert parse(200, mixed, body, now=at(100)).expires == at(1100)


def test_media_type_parameters_are_allowed(buyer_key: Ed25519Key) -> None:
    status, lines, body = w.signed_directory(BUYER, [buyer_key])
    headers = dict(lines.items())
    headers["Content-Type"] = DIRECTORY_MEDIA_TYPE + "; charset=utf-8"
    assert parse(status, headers, body).origin == BUYER_ORIGIN


def test_keys_of_other_types_are_ignored(buyer_key: Ed25519Key) -> None:
    body = build_directory([buyer_key, p256_public_jwk()])
    headers = raw(body, [buyer_key])
    assert list(parse(200, headers, body).keys) == [buyer_key.thumbprint]


@pytest.mark.parametrize("status", [201, 204, 301, 302, 404, 500])
def test_status_must_be_200(buyer_key: Ed25519Key, status: int) -> None:
    _, lines, body = w.signed_directory(BUYER, [buyer_key])
    headers = dict(lines.items())
    expect_refusal(lambda: parse(status, headers, body), Refusal.DIRECTORY_STATUS)


@pytest.mark.parametrize("media_type", ["application/json", "application/jwk-set+json", None])
def test_media_type(buyer_key: Ed25519Key, media_type: str | None) -> None:
    _, lines, body = w.signed_directory(BUYER, [buyer_key])
    headers = dict(lines.items())
    if media_type is None:
        del headers["Content-Type"]
    else:
        headers["Content-Type"] = media_type
    expect_refusal(lambda: parse(200, headers, body), Refusal.DIRECTORY_MEDIA_TYPE)


def test_body_cap(buyer_key: Ed25519Key) -> None:
    _, lines, body = w.signed_directory(BUYER, [buyer_key])
    headers = dict(lines.items())
    expect_refusal(
        lambda: parse(200, headers, body, max_bytes=len(body) - 1), Refusal.DIRECTORY_TOO_LARGE
    )


def test_key_count_cap(buyer_key: Ed25519Key, other_key: Ed25519Key) -> None:
    _, lines, body = w.signed_directory(BUYER, [buyer_key, other_key])
    headers = dict(lines.items())
    expect_refusal(lambda: parse(200, headers, body, max_keys=1), Refusal.DIRECTORY_TOO_MANY_KEYS)
    padded = build_directory([buyer_key] + [p256_public_jwk()] * 16)
    signed = raw(padded, [buyer_key])
    expect_refusal(lambda: parse(200, signed, padded), Refusal.DIRECTORY_TOO_MANY_KEYS)


def test_content_digest_missing_or_wrong(buyer_key: Ed25519Key) -> None:
    _, lines, body = w.signed_directory(BUYER, [buyer_key])
    headers = dict(lines.items())
    expect_refusal(lambda: parse(200, headers, body + b"\n"), Refusal.DIRECTORY_CONTENT_DIGEST)
    del headers["Content-Digest"]
    expect_refusal(lambda: parse(200, headers, body), Refusal.DIRECTORY_CONTENT_DIGEST)


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"keys": {}}'])
def test_body_must_be_a_jwk_set(body: bytes) -> None:
    headers = {"Content-Type": DIRECTORY_MEDIA_TYPE, "Content-Digest": content_digest(body)}
    expect_refusal(lambda: parse(200, headers, body), Refusal.DIRECTORY_FORMAT)


def test_an_ed25519_key_that_is_not_32_bytes_is_a_format_refusal(buyer_key: Ed25519Key) -> None:
    body = build_directory([buyer_key, {"kty": "OKP", "crv": "Ed25519", "x": "AAAA"}])
    headers = raw(body, [buyer_key])
    expect_refusal(lambda: parse(200, headers, body), Refusal.DIRECTORY_FORMAT)


def test_no_ed25519_key() -> None:
    body = build_directory([p256_public_jwk()])
    headers = {"Content-Type": DIRECTORY_MEDIA_TYPE, "Content-Digest": content_digest(body)}
    expect_refusal(lambda: parse(200, headers, body), Refusal.DIRECTORY_NO_USABLE_KEY)


def test_kid_must_be_the_thumbprint(buyer_key: Ed25519Key) -> None:
    jwk = {**buyer_key.public_jwk(), "kid": "operator-chosen"}
    body = build_directory([jwk])
    headers = raw(body, [buyer_key])
    expect_refusal(lambda: parse(200, headers, body), Refusal.DIRECTORY_KID)


def test_an_ed25519_key_that_does_not_sign(buyer_key: Ed25519Key, other_key: Ed25519Key) -> None:
    body = build_directory([buyer_key, other_key])
    headers = raw(body, [buyer_key])
    expect_refusal(lambda: parse(200, headers, body), Refusal.DIRECTORY_ONE_SIGNATURE_PER_KEY)


def test_unsigned_directory(buyer_key: Ed25519Key) -> None:
    body = build_directory([buyer_key])
    headers = {"Content-Type": DIRECTORY_MEDIA_TYPE, "Content-Digest": content_digest(body)}
    expect_refusal(lambda: parse(200, headers, body), Refusal.DIRECTORY_ONE_SIGNATURE_PER_KEY)


def test_two_signatures_by_one_key(buyer_key: Ed25519Key) -> None:
    body = build_directory([buyer_key])
    headers = raw(body, [buyer_key, buyer_key])
    expect_refusal(lambda: parse(200, headers, body), Refusal.DIRECTORY_ONE_SIGNATURE_PER_KEY)


def test_signed_for_another_authority(buyer_key: Ed25519Key) -> None:
    _, lines, body = w.signed_directory(BUYER, [buyer_key])
    headers = dict(lines.items())
    headers = raw(body, [buyer_key], departure=DirectoryDeparture(authority="other.example.com"))
    expect_refusal(lambda: parse(200, headers, body), Refusal.DIRECTORY_SIGNATURE)


@pytest.mark.parametrize(
    ("departure", "expected"),
    [
        (DirectoryDeparture(components=DIRECTORY.components()[:1]), Refusal.DIRECTORY_COMPONENTS),
        (DirectoryDeparture(components=DIRECTORY.components()[1:]), Refusal.DIRECTORY_COMPONENTS),
        (DirectoryDeparture(created=3600, expires=90000), Refusal.DIRECTORY_WINDOW),
        (DirectoryDeparture(created=-7200, expires=-3600), Refusal.DIRECTORY_WINDOW),
        # Valid at now on both ends, but expires comes before created.
        (DirectoryDeparture(created=60, expires=30), Refusal.DIRECTORY_WINDOW),
    ],
)
def test_signature_rules(
    buyer_key: Ed25519Key, departure: DirectoryDeparture, expected: Refusal
) -> None:
    body = build_directory([buyer_key])
    expect_refusal(lambda: parse(200, raw(body, [buyer_key], departure=departure), body), expected)


def test_directory_clock_skew(buyer_key: Ed25519Key) -> None:
    body = build_directory([buyer_key])
    headers = raw(body, [buyer_key], departure=DirectoryDeparture(created=60))
    assert parse(200, headers, body).origin == BUYER_ORIGIN


def test_wrong_tag(buyer_key: Ed25519Key) -> None:
    _, lines, body = w.signed_directory(BUYER, [buyer_key])
    headers = dict(lines.items())
    headers["Signature-Input"] = headers["Signature-Input"].replace(
        "http-message-signatures-directory", "web-bot-auth"
    )
    expect_refusal(lambda: parse(200, headers, body), Refusal.DIRECTORY_TAG)


def test_tampered_signature(buyer_key: Ed25519Key) -> None:
    _, lines, body = w.signed_directory(BUYER, [buyer_key])
    headers = dict(lines.items())
    headers["Signature"] = "key1=:" + "A" * 86 + "==:"
    expect_refusal(lambda: parse(200, headers, body), Refusal.DIRECTORY_SIGNATURE)


def test_unparseable_signature_fields(buyer_key: Ed25519Key) -> None:
    _, lines, body = w.signed_directory(BUYER, [buyer_key])
    headers = dict(lines.items())
    headers["Signature-Input"] = "key1=("
    expect_refusal(lambda: parse(200, headers, body), Refusal.DIRECTORY_SIGNATURE_PARSE)


def test_publishing_needs_a_key() -> None:
    with pytest.raises(ConfigurationError):
        publish_directory(BUYER_ORIGIN, [], now=T0)


@pytest.mark.parametrize(
    "keys",
    [[w.buyer_key(), w.buyer_key()], [w.key(f"directory-{i}") for i in range(17)]],
    ids=["a-key-twice", "seventeen-keys"],
)
def test_a_live_key_source_never_publishes_what_verifiers_refuse(keys: list[Ed25519Key]) -> None:
    # One keyfile in both rotation slots, or one key too many: every verifier refuses
    # the directory, so the publisher refuses it on the answer instead of serving it.
    publisher = DirectoryPublisher(BUYER_ORIGIN, lambda: keys)
    with pytest.raises(ConfigurationError):
        w.run(publisher.answer(T0))
    assert w.run(DirectoryPublisher(BUYER_ORIGIN, lambda: keys[:1]).answer(T0)).status == 200


# ── review regressions ────────────────────────────────────────────────────


def _resign(
    buyer_key: Ed25519Key, body: bytes, *, digest: str | None = None, alg: str | None = "ed25519"
) -> dict[str, str]:
    """Signs ``body`` as the buyer's directory, with a chosen Content-Digest and alg."""
    departure = DirectoryDeparture(
        content_digest=digest, alg=OMIT if alg is None else alg, expires=600
    )
    return raw(body, [buyer_key], departure=departure)


def test_a_directory_digest_without_sha_256_is_refused(buyer_key: Ed25519Key) -> None:
    body = build_directory([buyer_key])
    sha512 = w.digest_without_sha256(body)
    expect_refusal(
        lambda: parse(200, _resign(buyer_key, body, digest=sha512), body),
        Refusal.DIRECTORY_CONTENT_DIGEST,
    )


@pytest.mark.parametrize("alg", [None, "ed25519"])
def test_a_directory_signature_may_omit_alg(buyer_key: Ed25519Key, alg: str | None) -> None:
    body = build_directory([buyer_key])
    assert parse(200, _resign(buyer_key, body, alg=alg), body).origin == BUYER_ORIGIN


def test_a_directory_signature_with_another_alg_is_refused(buyer_key: Ed25519Key) -> None:
    body = build_directory([buyer_key])
    expect_refusal(
        lambda: parse(200, _resign(buyer_key, body, alg="rsa-pss-sha512"), body),
        Refusal.DIRECTORY_SIGNATURE,
    )


@pytest.mark.parametrize(("created", "ok"), [(60, True), (61, False)])
def test_a_directory_signature_gets_the_same_60_second_skew(
    buyer_key: Ed25519Key, created: int, ok: bool
) -> None:
    body = build_directory([buyer_key])
    args = (200, raw(body, [buyer_key], departure=DirectoryDeparture(created=created)), body)
    if ok:
        assert parse(*args).origin == BUYER_ORIGIN
    else:
        expect_refusal(lambda: parse(*args), Refusal.DIRECTORY_WINDOW)


def test_a_deeply_nested_body_is_a_format_refusal_not_a_crash(buyer_key: Ed25519Key) -> None:
    body = b"[" * 100_000 + b"]" * 100_000
    headers = {"Content-Type": DIRECTORY_MEDIA_TYPE, "Content-Digest": content_digest(body)}
    expect_refusal(lambda: parse(200, headers, body, max_bytes=len(body)), Refusal.DIRECTORY_FORMAT)


def test_a_body_that_is_not_utf_8_is_refused(buyer_key: Ed25519Key) -> None:
    # UTF-16 with a byte order mark: json.loads on bytes would detect and accept it.
    body = json.dumps({"keys": [buyer_key.public_jwk()]}).encode("utf-16")
    assert json.loads(body)["keys"]
    expect_refusal(lambda: parse(200, _resign(buyer_key, body), body), Refusal.DIRECTORY_FORMAT)


def test_evidence_keeps_the_exact_body_bytes(buyer_key: Ed25519Key) -> None:
    _, lines, body = w.signed_directory(BUYER, [buyer_key])
    headers = dict(lines.items())
    evidence = parse(200, headers, body).evidence.document()
    body_base64 = evidence["body_base64"]
    assert isinstance(body_base64, str)
    assert base64.b64decode(body_base64) == body


def test_a_directory_verifies_until_its_expires_and_not_one_second_after(
    buyer_key: Ed25519Key,
) -> None:
    body = build_directory([buyer_key])
    departure = DirectoryDeparture(created=-600, expires=0)
    args = (200, raw(body, [buyer_key], departure=departure), body)
    assert parse(*args, now=T0).expires == T0  # the boundary second
    expect_refusal(lambda: parse(*args, now=at(1)), Refusal.DIRECTORY_WINDOW)  # no skew on expires


@pytest.mark.parametrize("max_age", [0, 120, 300])
def test_max_age_sets_cache_control(buyer_key: Ed25519Key, max_age: int) -> None:
    headers, body = published([buyer_key], max_age=max_age)
    assert headers["Cache-Control"] == f"max-age={max_age}"
    assert parse(200, headers, body).origin == BUYER_ORIGIN


def test_the_default_max_age_is_300(buyer_key: Ed25519Key) -> None:
    assert published([buyer_key])[0]["Cache-Control"] == "max-age=300"


@pytest.mark.parametrize("max_age", [-1, 1.5, "300", True])
def test_max_age_must_be_whole_seconds_and_not_negative(max_age: object) -> None:
    with pytest.raises(ConfigurationError):
        PublishPolicy(max_age=max_age)  # type: ignore[arg-type]
