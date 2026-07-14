"""Tests for the conformance kit (EP-6.1): runner, vectors, gap report."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from iab_agentic_primitives.conformance import (
    DEFAULT_FIXTURES_DIR,
    GapReport,
    conformance_targets,
    json_diff,
    render_markdown,
    run_conformance,
)
from iab_agentic_primitives.conformance.gap_report_render import (
    HIGHEST_PRIORITY_GAP_ID,
    REMEDIATION,
)
from iab_agentic_primitives.conformance.vectors import (
    fixture_documents,
    render_fixture,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "spec" / "fixtures"
GAP_REPORT_MD = REPO_ROOT / "STANDARDS_GAP_REPORT.md"


@pytest.fixture(scope="module")
def green_report() -> GapReport:
    """One shared full run against the checked-in fixtures."""
    return run_conformance()


@pytest.fixture()
def fixtures_copy(tmp_path: Path) -> Path:
    """A mutable copy of the checked-in fixtures for corruption tests."""
    dest = tmp_path / "fixtures"
    shutil.copytree(FIXTURES_DIR, dest)
    return dest


def _mutate(path: Path, mutate) -> None:
    doc = json.loads(path.read_text())
    mutate(doc)
    path.write_text(json.dumps(doc))


def _vector(doc: dict, name: str) -> dict:
    return next(v for v in doc["vectors"] if v["name"] == name)


# ---------------------------------------------------------------------------
# The checked-in fixtures themselves
# ---------------------------------------------------------------------------


def test_checked_in_fixtures_match_generators() -> None:
    """Drift guard: fixture files must match the vector builders exactly."""
    for rel_path, doc in fixture_documents().items():
        path = FIXTURES_DIR / rel_path
        assert path.is_file(), f"missing fixture {rel_path}"
        assert path.read_text() == render_fixture(doc), (
            f"{rel_path} has drifted; regenerate with "
            "`uv run python -m iab_agentic_primitives.conformance --regenerate`"
        )
    on_disk = {
        str(p.relative_to(FIXTURES_DIR)) for p in FIXTURES_DIR.rglob("*.golden.json")
    }
    assert on_disk == set(fixture_documents()), "stray or missing fixture files"


def test_every_target_has_at_least_two_realistic_vectors() -> None:
    docs = fixture_documents()
    for area, models in conformance_targets().items():
        for target in models:
            doc = docs[f"{area}/{target}.golden.json"]
            valid = [v for v in doc["vectors"] if v["mode"] == "valid"]
            assert len(valid) >= 2, f"{area}/{target} needs >=2 valid golden vectors"


def test_major_envelopes_have_forward_compat_vectors() -> None:
    """FD-13: one must-ignore variant per major envelope."""
    expected = {
        ("primitives", "Agent"),
        ("primitives", "Product"),
        ("primitives", "Quote"),
        ("primitives", "Deal"),
        ("protocol", "QuoteRequest"),
        ("protocol", "QuoteResponse"),
        ("protocol", "DealBookingRequest"),
        ("protocol", "NegotiationMessage"),
        ("protocol", "JsonRpcRequest"),
        ("protocol", "ErrorEnvelope"),
        ("events", "Event"),
    }
    docs = fixture_documents()
    for area, target in expected:
        doc = docs[f"{area}/{target}.golden.json"]
        modes = {v["mode"] for v in doc["vectors"]}
        assert "must_ignore" in modes, f"{area}/{target} lacks a must_ignore vector"


def test_money_rejection_vectors_exist() -> None:
    """FD-11: float-typed money must be covered by expected-invalid vectors."""
    float_vectors = [
        (rel, v["name"])
        for rel, doc in fixture_documents().items()
        for v in doc.get("vectors", [])
        if v.get("mode") == "invalid" and "float" in v["name"]
    ]
    assert len(float_vectors) >= 5, float_vectors


# ---------------------------------------------------------------------------
# The runner: green path
# ---------------------------------------------------------------------------


def test_full_run_is_conformant(green_report: GapReport) -> None:
    assert green_report.conformant, [g.model_dump() for g in green_report.gaps]
    summary = green_report.summary()
    assert summary["failed"] == 0
    assert summary["vectors"] >= 100
    assert set(summary["by_area"]) == {"primitives", "protocol", "events", "state"}


def test_all_checks_have_valid_shape(green_report: GapReport) -> None:
    kinds = {record.check for record in green_report.checks}
    assert {
        "validate",
        "roundtrip",
        "must_ignore",
        "expected_invalid",
        "schema_sync",
        "state_sequence",
        "state_spec_sync",
    } <= kinds


def test_gap_report_structure(green_report: GapReport) -> None:
    """The EP-6.2 machine-readable report shape."""
    payload = green_report.to_json_dict()
    assert set(payload) == {
        "generated_at",
        "library_version",
        "summary",
        "gaps",
        "standards",
        "checks",
    }
    assert payload["summary"]["conformant"] is True
    statuses = {entry["status"] for entry in payload["standards"]}
    assert statuses == {"implemented", "unverified"}
    for entry in payload["standards"]:
        if entry["status"] == "unverified":
            assert entry["missing"], f"UNVERIFIED {entry['id']} must say what's missing"
        else:
            assert entry["checks"], f"implemented {entry['id']} must list its checks"
    # round-trips as JSON
    assert json.loads(json.dumps(payload)) == payload


def test_unverified_standards_are_listed_explicitly(green_report: GapReport) -> None:
    ids = {entry.id for entry in green_report.standards if entry.status == "unverified"}
    assert {
        "opendirect-2.1",
        "iab-deals-api-1.0",
        "aamp",
        "adcom-openrtb-supply-chain",
        "sellers-json-ads-txt",
        "gpp",
        "tcf",
    } <= ids
    table = green_report.render_table()
    assert "UNVERIFIED" in table
    assert "CONFORMANT" in table


# ---------------------------------------------------------------------------
# The runner: red paths
# ---------------------------------------------------------------------------


def test_corrupted_vector_fails_with_useful_diff(fixtures_copy: Path) -> None:
    """Dropping a defaulted field breaks the canonical byte-compare, and the
    gap names the exact path that drifted."""

    def corrupt(doc: dict) -> None:
        vector = _vector(doc, "pg_digital_guaranteed")
        del vector["data"]["pricing"]["base_cpm"]["currency"]

    _mutate(fixtures_copy / "primitives" / "Quote.golden.json", corrupt)
    report = run_conformance(fixtures_dir=fixtures_copy)
    assert not report.conformant
    gap = next(g for g in report.gaps if g.check == "roundtrip")
    assert gap.target == "Quote"
    assert gap.vector == "pg_digital_guaranteed"
    assert gap.diff, "roundtrip gap must carry a field-level diff"
    assert gap.diff[0].path == "$.pricing.base_cpm.currency"
    assert gap.diff[0].actual == '"USD"'


def test_invalid_golden_vector_is_a_validation_gap(fixtures_copy: Path) -> None:
    def corrupt(doc: dict) -> None:
        vector = _vector(doc, "pg_digital_guaranteed")
        vector["data"]["deal_type"] = "programmaticguaranteed"  # retired long form

    _mutate(fixtures_copy / "primitives" / "Quote.golden.json", corrupt)
    report = run_conformance(fixtures_dir=fixtures_copy)
    gap = next(g for g in report.gaps if g.check == "validate")
    assert gap.target == "Quote"
    assert "failed validation" in gap.message


def test_expected_invalid_that_parses_is_a_gap(fixtures_copy: Path) -> None:
    """If the contract ever ACCEPTS float money, the FD-11 vector goes red."""

    def corrupt(doc: dict) -> None:
        vector = _vector(doc, "float_buyer_price_rejected")
        vector["data"]["buyer_price"]["amount_micros"] = 20_000_000  # now valid

    _mutate(fixtures_copy / "protocol" / "NegotiationMessage.golden.json", corrupt)
    report = run_conformance(fixtures_dir=fixtures_copy)
    gap = next(g for g in report.gaps if g.check == "expected_invalid")
    assert gap.target == "NegotiationMessage"
    assert "PARSED successfully" in gap.message


def test_expected_invalid_wrong_reason_is_a_gap(fixtures_copy: Path) -> None:
    def corrupt(doc: dict) -> None:
        vector = _vector(doc, "float_target_cpm_rejected")
        vector["expected_error"] = "some entirely different error"

    _mutate(fixtures_copy / "protocol" / "QuoteRequest.golden.json", corrupt)
    report = run_conformance(fixtures_dir=fixtures_copy)
    gap = next(g for g in report.gaps if g.check == "expected_invalid")
    assert "wrong reason" in gap.message


def test_unknown_field_vectors_pass_and_extras_are_dropped(
    green_report: GapReport,
) -> None:
    """FD-13 must-ignore vectors pass, and the extras really are ignored."""
    must_ignore = [r for r in green_report.checks if r.check == "must_ignore"]
    assert len(must_ignore) >= 10
    assert all(r.status.value == "pass" for r in must_ignore)

    from iab_agentic_primitives.primitives import Quote

    doc = json.loads((FIXTURES_DIR / "primitives" / "Quote.golden.json").read_text())
    vector = _vector(doc, "quote_with_unknown_fields")
    assert "x_vendor_ext" in vector["data"]
    parsed = Quote.model_validate(vector["data"])
    dumped = parsed.model_dump(mode="json")
    assert "x_vendor_ext" not in dumped
    assert "field_from_a_newer_spec_version" not in dumped


def test_missing_fixture_is_a_gap(fixtures_copy: Path) -> None:
    (fixtures_copy / "primitives" / "Deal.golden.json").unlink()
    report = run_conformance(fixtures_dir=fixtures_copy)
    gap = next(g for g in report.gaps if g.check == "fixture")
    assert gap.target == "Deal"
    assert "no golden fixture" in gap.message


# ---------------------------------------------------------------------------
# State-machine sequences
# ---------------------------------------------------------------------------


def test_state_sequences_all_pass(green_report: GapReport) -> None:
    records = [r for r in green_report.checks if r.check == "state_sequence"]
    assert len(records) >= 15
    assert all(r.status.value == "pass" for r in records)
    assert {r.target for r in records} == {"deal", "order", "change_request"}


def test_illegal_transition_accepted_is_a_gap(fixtures_copy: Path) -> None:
    """A sequence marked illegal that the machine allows must go red."""

    def corrupt(doc: dict) -> None:
        seq = next(s for s in doc["sequences"] if s["name"] == "skip_acceptance")
        seq["states"] = ["proposed", "negotiating"]  # actually legal

    _mutate(fixtures_copy / "state" / "DealLifecycle.golden.json", corrupt)
    report = run_conformance(fixtures_dir=fixtures_copy)
    gap = next(g for g in report.gaps if g.check == "state_sequence")
    assert gap.target == "deal"
    assert "ACCEPTED" in gap.message


def test_legal_sequence_rejected_is_a_gap(fixtures_copy: Path) -> None:
    def corrupt(doc: dict) -> None:
        seq = next(s for s in doc["sequences"] if s["name"] == "happy_path")
        seq["states"] = ["proposed", "booked"]  # actually illegal

    _mutate(fixtures_copy / "state" / "DealLifecycle.golden.json", corrupt)
    report = run_conformance(fixtures_dir=fixtures_copy)
    gap = next(g for g in report.gaps if g.check == "state_sequence")
    assert "legal sequence rejected" in gap.message


# ---------------------------------------------------------------------------
# Diff helper
# ---------------------------------------------------------------------------


def test_json_diff_reports_paths_and_wire_types() -> None:
    expected = {"a": 1, "b": [1, 2], "c": {"d": "x"}}
    actual = {"a": 1.0, "b": [1], "c": {"d": "y"}, "e": 5}
    paths = {entry["path"] for entry in json_diff(expected, actual)}
    assert paths == {"$.a", "$.b.length", "$.c.d", "$.e"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_writes_gap_report_and_exits_zero(tmp_path: Path) -> None:
    out = tmp_path / "gap_report.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "iab_agentic_primitives.conformance",
            "--output",
            str(out),
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "STANDARDS" in result.stdout
    payload = json.loads(out.read_text())
    assert payload["summary"]["conformant"] is True
    assert payload["summary"]["failed"] == 0


def test_cli_exits_one_on_gaps(tmp_path: Path, fixtures_copy: Path) -> None:
    (fixtures_copy / "primitives" / "Deal.golden.json").unlink()
    out = tmp_path / "gap_report.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "iab_agentic_primitives.conformance",
            "--fixtures",
            str(fixtures_copy),
            "--output",
            str(out),
            "--quiet",
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.returncode == 1
    payload = json.loads(out.read_text())
    assert payload["summary"]["conformant"] is False


def test_cli_regenerate_writes_fixtures(tmp_path: Path) -> None:
    target = tmp_path / "regen"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "iab_agentic_primitives.conformance",
            "--regenerate",
            "--fixtures",
            str(target),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    written = {str(p.relative_to(target)) for p in target.rglob("*.golden.json")}
    assert written == set(fixture_documents())


def test_default_fixtures_dir_points_at_repo() -> None:
    assert DEFAULT_FIXTURES_DIR == FIXTURES_DIR


# ---------------------------------------------------------------------------
# EP-6.2 Markdown gap report (STANDARDS_GAP_REPORT.md)
# ---------------------------------------------------------------------------


def test_checked_in_gap_report_matches_fresh_render(green_report: GapReport) -> None:
    """Drift guard: the checked-in Markdown must match a fresh render exactly.

    Regenerate with
    `uv run python -m iab_agentic_primitives.conformance --render-md STANDARDS_GAP_REPORT.md`.
    """
    assert GAP_REPORT_MD.is_file(), "STANDARDS_GAP_REPORT.md is missing"
    fresh = render_markdown(green_report.to_json_dict())
    assert GAP_REPORT_MD.read_text() == fresh, (
        "STANDARDS_GAP_REPORT.md has drifted; regenerate with "
        "`uv run python -m iab_agentic_primitives.conformance "
        "--render-md STANDARDS_GAP_REPORT.md`"
    )


def test_gap_report_render_is_deterministic_and_timeless(
    green_report: GapReport,
) -> None:
    """The render omits the volatile timestamp so the drift guard is stable."""
    payload = green_report.to_json_dict()
    first = render_markdown(payload)
    second = render_markdown(payload)
    assert first == second
    assert payload["generated_at"] not in first


def test_render_markdown_on_synthetic_report() -> None:
    """The renderer derives CONFORMANT / PARTIAL / UNVERIFIED from the data."""
    report = {
        "library_version": "9.9.9",
        "summary": {
            "conformant": False,
            "checks": 6,
            "passed": 3,
            "failed": 1,
            "skipped": 2,
            "vectors": 4,
            "targets": 3,
        },
        "standards": [
            {
                "id": "opendirect-2.1",
                "name": "IAB OpenDirect",
                "version": "2.1",
                "status": "unverified",
                "checks": [],
                "missing": "No fidelity check against the published spec.",
            },
            {
                "id": "good-impl",
                "name": "Fully checked claim",
                "version": "1.0",
                "status": "implemented",
                "checks": ["validate", "roundtrip"],
                "missing": "",
            },
            {
                "id": "broken-impl",
                "name": "Failing claim",
                "version": "1.0",
                "status": "implemented",
                "checks": ["expected_invalid"],
                "missing": "",
            },
            {
                "id": "never-ran",
                "name": "Unexecuted claim",
                "version": "1.0",
                "status": "implemented",
                "checks": ["ghost_check"],
                "missing": "",
            },
        ],
        "checks": [
            {"area": "p", "target": "T", "check": "validate", "status": "pass"},
            {"area": "p", "target": "T", "check": "roundtrip", "status": "pass"},
            {"area": "p", "target": "U", "check": "expected_invalid", "status": "fail"},
        ],
        "gaps": [],
    }
    md = render_markdown(report)

    # Executive summary counts: 1 conformant, 2 partial, 1 unverified.
    assert "**1 fully self-conformant**" in md
    assert "**2 partial**" in md
    assert "**1 unverified (pending external spec)**" in md
    assert "library version `9.9.9`" in md

    # Highest-priority gap is called out with its remediation note.
    assert "Highest-priority gap: IAB OpenDirect 2.1" in md
    assert REMEDIATION[HIGHEST_PRIORITY_GAP_ID][:30] in md

    # Statuses appear per standard.
    assert "**Status:** CONFORMANT" in md
    assert "**Status:** PARTIAL" in md
    assert "**Status:** UNVERIFIED" in md

    # A failing implemented claim is PARTIAL, not CONFORMANT.
    broken = md.split("#### Failing claim")[1].split("####")[0]
    assert "PARTIAL" in broken
    # An implemented claim whose mapped check never executed is PARTIAL.
    never = md.split("#### Unexecuted claim")[1].split("####")[0]
    assert "PARTIAL" in never
    # UNVERIFIED entries carry a concrete to-close note.
    assert "To close this gap:" in md


def test_every_unverified_standard_has_remediation(green_report: GapReport) -> None:
    """No UNVERIFIED standard may ship without an actionable to-close note."""
    md = render_markdown(green_report.to_json_dict())
    for entry in green_report.standards:
        if entry.status == "unverified":
            block = md.split(f"`{entry.id}`")[1].split("####")[0]
            assert "To close this gap:" in block, entry.id


def test_cli_render_md_flag_writes_report(tmp_path: Path) -> None:
    out_json = tmp_path / "gap_report.json"
    out_md = tmp_path / "STANDARDS_GAP_REPORT.md"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "iab_agentic_primitives.conformance",
            "--output",
            str(out_json),
            "--render-md",
            str(out_md),
            "--quiet",
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    text = out_md.read_text()
    assert text.startswith("# IAB Agentic Primitives — Standards Gap Report")
    assert "## Executive summary" in text
