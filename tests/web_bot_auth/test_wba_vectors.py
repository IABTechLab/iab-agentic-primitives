"""The committed conformance vectors, the generator, the runner, and the adapters."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import replace
from pathlib import Path

import http_sf
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import ValidationError

from iab_agentic_primitives.web_bot_auth import HeaderLines, Refusal, thumbprint
from iab_agentic_primitives.web_bot_auth.conformance import (
    PROTOCOL,
    VECTORS,
    CommandAdapter,
    LibraryAdapter,
    generate,
    keys_document,
    meets,
    published_key_ids,
    published_keys,
    run_conformance,
    scenarios_document,
    vector_operation,
    vectors,
)
from iab_agentic_primitives.web_bot_auth.conformance.adapter import Adapter
from iab_agentic_primitives.web_bot_auth.conformance.fixtures import DOC_URL
from iab_agentic_primitives.web_bot_auth.conformance.models import (
    KeyEntry,
    Operation,
    RefusalsDocument,
    Refused,
    Refuses,
    RequestDoc,
    Result,
    Vector,
    Verified,
    VerifyDirectory,
    VerifyRequest,
    VerifyResponse,
    read,
)
from iab_agentic_primitives.web_bot_auth.conformance.signing import sign_directory

from .support import BUYER, OTHER, T0, at, key, vector_message, vector_request
from .support import b64url_bytes as b64url_decode

ALL = dict(vectors())
POSITIVE = {vid: v for vid, v in ALL.items() if vid.startswith("positive/")}
NEGATIVE = {vid: v for vid, v in ALL.items() if vid.startswith("negative/")}
KEYS = keys_document().keys
ROOT = Path(__file__).resolve().parents[2]


def _library(op: Operation) -> Result:
    return LibraryAdapter()(op)


# ── the files ─────────────────────────────────────────────────────────────


def test_committed_files_match_the_generator() -> None:
    assert not generate.check()


def test_a_stale_vector_file_fails_the_check_and_regenerating_deletes_it(tmp_path: Path) -> None:
    copy = tmp_path / "vectors"
    shutil.copytree(generate.SOURCE, copy)
    (copy / "negative" / "099-stale.json").write_text("{}")
    assert generate.check(copy).unknown == ("negative/099-stale.json",)
    generate.write(copy)
    assert not (copy / "negative" / "099-stale.json").exists()
    assert not generate.check(copy)


def test_regenerating_touches_only_the_generators_own_files(tmp_path: Path) -> None:
    copy = tmp_path / "vectors"
    shutil.copytree(generate.SOURCE, copy)
    (copy / "package.json").write_text("{}")
    (copy / "notes").mkdir()
    (copy / "notes" / "settings.json").write_text("{}")
    (copy / "scenarios.json").unlink()
    assert generate.check(copy).missing == ("scenarios.json",)
    generate.write(copy)
    assert (copy / "package.json").exists() and (copy / "notes" / "settings.json").exists()
    assert not generate.check(copy)


def test_regenerating_refuses_a_directory_that_is_not_a_vector_directory(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}")
    with pytest.raises(ValueError, match="not a vector directory"):
        generate.write(tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == ["package.json"]


def test_an_edited_vector_file_fails_the_check(tmp_path: Path) -> None:
    copy = tmp_path / "vectors"
    shutil.copytree(generate.SOURCE, copy)
    edited = copy / "positive" / "001-single-signer-request.json"
    edited.write_text(edited.read_text().replace('"tier": "wba"', '"tier": "profile"'))
    assert generate.check(copy).differs == ("positive/001-single-signer-request.json",)


def test_a_vector_whose_operation_signs_fails_to_load() -> None:
    # A vector is graded by a verification; its operation names which.
    document = json.loads((VECTORS / "positive" / "001-single-signer-request.json").read_text())
    document["operation"]["op"] = "sign_request"
    with pytest.raises(ValidationError, match="op"):
        read(Vector, json.dumps(document))


@pytest.mark.parametrize(
    ("member", "value"),
    [("ok", 1), ("ok", "yes"), ("status", 403.0), ("status", 500), ("status", 429)],
)
def test_an_outcome_is_read_strictly(member: str, value: object) -> None:
    # JSON booleans and integers, and the rule's own status.
    document = json.loads((VECTORS / "negative" / "001-missing-nonce.json").read_text())
    document["expected"][member] = value
    with pytest.raises(ValidationError):
        read(Vector, json.dumps(document))


def test_a_refusal_without_its_rule_fails_to_load() -> None:
    document = json.loads((VECTORS / "negative" / "001-missing-nonce.json").read_text())
    del document["expected"]["rule"]
    with pytest.raises(ValidationError, match="rule"):
        read(Vector, json.dumps(document))


def test_the_refusal_vocabulary_is_the_refusal_enum() -> None:
    vocabulary = read(RefusalsDocument, (VECTORS / "refusals.json").read_text())
    assert [(r.rule, r.status) for r in vocabulary.refusals] == [
        (r.code, r.status) for r in Refusal
    ]


def test_vectors_and_the_protocol_are_package_data() -> None:
    assert {p.name for p in VECTORS.iterdir()} >= {
        "keys.json",
        "scenarios.json",
        "refusals.json",
        "positive",
        "negative",
        "README.md",
    }
    protocol = PROTOCOL.read_text()
    assert "refusals.json" in protocol
    assert all(str(status) in protocol for status in (400, 403, 429, 503))


def test_the_wheel_ships_the_protocol_and_the_vectors(tmp_path: Path) -> None:
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path), str(ROOT)],
        capture_output=True,
        check=True,
    )
    (wheel,) = tmp_path.glob("*.whl")
    names = set(zipfile.ZipFile(wheel).namelist())
    package = "iab_agentic_primitives/web_bot_auth"
    assert f"{package}/conformance/PROTOCOL.md" in names
    assert f"{package}/vectors/refusals.json" in names
    assert f"{package}/vectors/negative/001-missing-nonce.json" in names


@pytest.mark.parametrize("entry", KEYS, ids=lambda k: k.seed_name)
def test_each_key_derives_from_its_seed_and_kid_is_its_thumbprint(entry: KeyEntry) -> None:
    seed = hashlib.sha256(f"aamp-wba-test/{entry.seed_name}".encode()).digest()
    assert b64url_decode(entry.private_d) == seed
    # The suite's own keys are these keys: support derives them from the same seed names.
    assert key(entry.seed_name).thumbprint == entry.kid
    assert entry.kid == thumbprint({"kty": entry.kty, "crv": entry.crv, "x": entry.x})
    assert published_keys()[entry.kid].public_jwk()["x"] == entry.x
    assert entry.identity.host.endswith(".example.com")


def test_published_key_ids() -> None:
    assert published_key_ids() == {k.kid for k in KEYS}


def _signatures(vector: Vector) -> dict[str, bytes]:
    value = vector_message(vector).headers["Signature"]
    members = http_sf.parse(value.encode(), tltype="dictionary")
    assert isinstance(members, dict)
    out = {}
    for label, member in members.items():
        assert isinstance(member, tuple) and isinstance(member[0], bytes)
        out[label] = member[0]
    return out


def _keyids(vector: Vector) -> dict[str, str]:
    value = vector_message(vector).headers["Signature-Input"]
    members = http_sf.parse(value.encode(), tltype="dictionary")
    assert isinstance(members, dict)
    out = {}
    for label, member in members.items():
        assert isinstance(member, tuple) and isinstance(member[1], dict)
        out[label] = str(member[1]["keyid"])
    return out


@pytest.mark.parametrize("vid", [v for v, d in ALL.items() if d.expected_signature_base])
def test_each_committed_signature_signs_its_expected_base(vid: str) -> None:
    vector = ALL[vid]
    assert vector.expected_signature_base is not None
    by_kid = {k.kid: k for k in KEYS}
    signatures, keyids = _signatures(vector), _keyids(vector)
    for label, base in vector.expected_signature_base.items():
        public = Ed25519PublicKey.from_public_bytes(b64url_decode(by_kid[keyids[label]].x))
        public.verify(signatures[label], base.encode())


def _headings() -> set[str]:
    def slug(heading: str) -> str:
        return re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", heading.strip().lower()))

    doc = (ROOT / "WEB_BOT_AUTH.md").read_text()
    return {slug(line.lstrip("#")) for line in doc.splitlines() if line.startswith("#")}


def test_every_spec_reference_resolves_to_a_heading_of_the_doc() -> None:
    references = [v.spec_reference for v in ALL.values()]
    references.append(scenarios_document().spec_reference)
    for reference in references:
        url, anchor = reference.split("#", 1)
        assert url == DOC_URL
        assert anchor in _headings(), reference


@pytest.mark.parametrize("vid", sorted(ALL))
def test_each_vector_declares_its_kind_tier_and_spec(vid: str) -> None:
    vector = ALL[vid]
    entry = next(e for e in generate.VECTORS_REGISTRY if e.path == f"{vid}.json")
    assert (vector.tier, vector.spec) == (entry.tier, "OpenProposal 3.0 § 2.8")
    assert isinstance(vector.operation, (VerifyRequest, VerifyResponse, VerifyDirectory))


def test_vector_hosts_are_under_example_com() -> None:
    for path in generate.SOURCE.rglob("*.json"):
        text = path.read_text()
        assert ".test" not in text.replace("_test_", "").replace("test_harness", ""), path.name


# ── the library on the vectors ────────────────────────────────────────────


@pytest.mark.parametrize("vid", sorted(POSITIVE))
def test_positive_vectors_verify_and_name_their_identity(vid: str) -> None:
    vector = POSITIVE[vid]
    got = _library(vector_operation(vector))
    assert meets(vector.expected, got), got


@pytest.mark.parametrize("vid", sorted(NEGATIVE))
def test_negative_vectors_are_refused_for_their_rule(vid: str) -> None:
    vector = NEGATIVE[vid]
    expected = vector.expected
    assert isinstance(expected, Refuses)
    got = _library(vector_operation(vector))
    assert isinstance(got, Refused), got
    assert (got.status, got.rule) == (expected.status, expected.rule.code)


def test_004_verifies_when_the_named_origin_publishes_the_key() -> None:
    vector = NEGATIVE["negative/004-key-not-in-named-directory"]
    op = vector_operation(vector)
    assert isinstance(op, VerifyRequest)
    buyer_kid = next(k.kid for k in KEYS if str(k.identity) == BUYER)
    other = {OTHER: sign_directory([published_keys()[buyer_kid]], OTHER, vector.reference_now)}
    got = _library(replace(op, directories=other))
    assert got == Verified(identity=OTHER, keyid=buyer_kid, body_covered=True)


def _only_member(value: str, label: str) -> str:
    members = http_sf.parse(value.encode(), tltype="dictionary")
    assert isinstance(members, dict)
    return http_sf.ser({label: members[label]})


def test_006_each_signature_verifies_on_its_own() -> None:
    vector = NEGATIVE["negative/006-two-signatures"]
    first = POSITIVE["positive/001-single-signer-request"]
    assert vector.expected_signature_base and first.expected_signature_base
    assert vector.expected_signature_base["sig1"] == first.expected_signature_base["sig1"]
    op = vector_operation(vector)
    assert isinstance(op, VerifyRequest)
    headers = op.request.headers
    for label, origin in (("sig1", BUYER), ("sig2", OTHER)):
        only = dict(headers.items())
        for name in ("Signature-Input", "Signature", "Signature-Agent"):
            only[name] = _only_member(headers[name], label)
        got = _library(replace(op, request=replace(op.request, headers=HeaderLines(only))))
        assert isinstance(got, Verified) and got.identity == origin, got


def test_002_is_positive_001_sent_again_after_001() -> None:
    replayed = NEGATIVE["negative/002-replayed-nonce"].operation
    assert isinstance(replayed, VerifyRequest)
    assert replayed.request == vector_request(POSITIVE["positive/001-single-signer-request"])
    assert replayed.prior == (replayed.request,)


def test_positive_001_body_is_an_openproposal_commit() -> None:
    request = vector_request(POSITIVE["positive/001-single-signer-request"])
    body = json.loads(request.body)
    assert request.url == "https://seller.example.com/proposals/prop_7/commit"
    assert body["entry"]["role"] == "commitment"
    assert set(body["writes"]) == set(body["entry"]["line_item_ids"])
    digest = base64.b64encode(hashlib.sha256(request.body).digest()).decode()
    assert request.headers["Content-Digest"] == f"sha-256=:{digest}:"
    response = vector_message(POSITIVE["positive/003-signed-response"])
    assert [e["role"] for e in json.loads(response.body)["entries"]] == ["commitment", "acceptance"]


def test_reference_now_is_the_fixed_clock() -> None:
    assert {v.reference_now for v in ALL.values()} <= {at(10), at(15)}


# ── the runner grades by rule ─────────────────────────────────────────────


def test_the_runner_grades_this_library_at_the_vector_clock_and_now() -> None:
    reports = [run_conformance(now=now) for now in (T0, None)]
    for graded in reports:
        assert graded.ok, graded.render()
    report = reports[-1]
    assert {c.suite for c in report.checks} == {"vectors", "live", "scenarios"}
    doc = scenarios_document()
    count = len(doc.request_scenarios) + len(doc.directory_scenarios) + len(doc.response_scenarios)
    assert sum(c.suite == "scenarios" for c in report.checks) == count
    (authority,) = [c for c in report.checks if c.id.startswith("R26 ")]
    assert (authority.ok, authority.expected) == (True, "403 authority")


def _vector_failures(adapter: Adapter) -> set[str]:
    report = run_conformance(adapter, now=T0, suites=("vectors",))
    return {c.id for c in report.failures}


def test_an_adapter_that_accepts_everything_fails_every_refusal_and_the_wrong_identity() -> None:
    def accepts_everything(op: Operation) -> Result:
        return Verified(identity=BUYER, keyid="k")

    # Every refusal fails, and so does the response vector, which names the seller.
    assert _vector_failures(accepts_everything) == {*NEGATIVE, "positive/003-signed-response"}


def test_an_adapter_that_crashes_fails_every_vector() -> None:
    def crashes(op: Operation) -> Result:
        return Refused.adapter_failure("adapter-crash", "exit 1")

    assert _vector_failures(crashes) == set(ALL)


def test_an_adapter_with_the_right_rule_and_the_wrong_status_fails() -> None:
    def wrong_status(op: Operation) -> Result:
        got = _library(op)
        return (
            replace(got, status=400 if got.status != 400 else 403)
            if isinstance(got, Refused)
            else got
        )

    assert _vector_failures(wrong_status) == set(NEGATIVE)


def test_an_adapter_with_the_right_status_and_the_wrong_rule_fails() -> None:
    def wrong_rule(op: Operation) -> Result:
        got = _library(op)
        return replace(got, rule="signature-invalid") if isinstance(got, Refused) else got

    assert _vector_failures(wrong_rule) == set(NEGATIVE)


# ── the command adapter contains every failure ────────────────────────────


def _op() -> VerifyRequest:
    op = vector_operation(POSITIVE["positive/001-single-signer-request"])
    assert isinstance(op, VerifyRequest)
    return op


def _python(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


def test_a_command_adapter_that_hangs_times_out() -> None:
    got = CommandAdapter(_python("import time; time.sleep(5)"), timeout=0.5)(_op())
    assert isinstance(got, Refused) and (got.status, got.rule) == (None, "adapter-timeout")


def test_a_command_adapter_that_exits_reports_its_exit_code() -> None:
    got = CommandAdapter(_python("import sys; sys.exit(3)"))(_op())
    assert isinstance(got, Refused) and got.rule == "adapter-crash"
    assert got.error.startswith("exit 3")


def test_a_command_adapter_that_answers_garbage_is_invalid() -> None:
    got = CommandAdapter(_python("print('not json')"))(_op())
    assert isinstance(got, Refused) and (got.status, got.rule) == (None, "adapter-invalid")
    status_zero = _python('print(\'{\\"ok\\": false, \\"status\\": 0, \\"rule\\": \\"x\\"}\')')
    got = CommandAdapter(status_zero)(_op())
    assert isinstance(got, Refused) and got.rule == "adapter-invalid"


@pytest.mark.parametrize(
    "answer",
    [
        {"ok": 1, "identity": BUYER, "keyid": "k"},
        {"ok": True, "identity": BUYER, "keyid": "k", "body_covered": "on"},
        {"ok": True, "identity": BUYER, "keyid": "k", "bound": "yes"},
        {"ok": False, "status": 403.0, "rule": "signature-invalid"},
    ],
    ids=["ok-1", "body-covered-on", "bound-yes", "status-float"],
)
def test_an_answer_that_is_not_strict_json_is_adapter_invalid(
    tmp_path: Path, answer: dict[str, object]
) -> None:
    # The protocol's booleans are JSON booleans and its statuses JSON integers.
    script = tmp_path / "adapter.py"
    script.write_text(f"print({json.dumps(json.dumps(answer))})")
    got = CommandAdapter(f'"{sys.executable}" "{script}"')(_op())
    assert isinstance(got, Refused) and (got.status, got.rule) == (None, "adapter-invalid")


def test_a_command_adapter_that_cannot_start_crashes() -> None:
    got = CommandAdapter("/nonexistent/adapter")(_op())
    assert isinstance(got, Refused) and got.rule == "adapter-crash"


# ── the command line ──────────────────────────────────────────────────────


def _cli(*args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "iab_agentic_primitives.web_bot_auth.conformance", *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_adapter_runs_one_operation_and_line_mode_runs_many() -> None:
    from iab_agentic_primitives.web_bot_auth.conformance.models import dump_json

    op = dump_json(_op())
    one = _cli("adapter", stdin=op)
    assert json.loads(one.stdout)["identity"] == BUYER
    many = _cli("adapter", "--lines", stdin=op + "\n" + json.dumps({"op": "nope"}) + "\n")
    first, second = [json.loads(line) for line in many.stdout.splitlines()]
    assert first["ok"] and (second["ok"], second["rule"]) == (False, "adapter-invalid")
    assert "status" not in second


def test_cli_check_and_run_through_a_command_adapter() -> None:
    assert _cli("--check").returncode == 0
    command = f'"{sys.executable}" -m iab_agentic_primitives.web_bot_auth.conformance adapter'
    report = run_conformance(command, now=T0, suites=("vectors",))
    assert report.ok, report.render()


def test_a_signed_request_document_round_trips() -> None:
    request = vector_request(POSITIVE["positive/001-single-signer-request"])
    assert RequestDoc.of(request.message()) == request
