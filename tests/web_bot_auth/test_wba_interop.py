"""Interop with Cloudflare's web-bot-auth 0.2.0, through the Node harness in interop/.

The harness runs this library as an adapter (``conformance adapter --lines``)
and grades every message three ways: the Python verdict, Cloudflare's library
with the profile's rules (policy.mjs), and Cloudflare's library as shipped.
The tests carry the ``interop`` marker, which pytest deselects by default:
``make interop`` installs the harness's Node dependencies and runs them. Once
selected, they fail, rather than skip, when ``node`` or those dependencies are
absent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NotRequired, TypedDict

import pytest

from iab_agentic_primitives.web_bot_auth.conformance import VECTORS

pytestmark = pytest.mark.interop

INTEROP = Path(__file__).parent / "interop"
NODE = shutil.which("node")

_MISSING = (
    "node is not installed"
    if NODE is None
    else None
    if (INTEROP / "node_modules" / "web-bot-auth").is_dir()
    else "the harness's Node dependencies are not installed: run `make interop-deps`"
)


class Check(TypedDict):
    """One row of the harness's ``--json`` report."""

    suite: str
    id: str
    ok: bool
    expected: str
    detail: str
    tier: NotRequired[str | None]
    python: NotRequired[str | None]
    cloudflare: NotRequired[str | None]
    cloudflare_bare: NotRequired[str | None]


@pytest.fixture(autouse=True, scope="module")
def _harness_installed() -> None:
    if _MISSING is not None:
        pytest.fail(f"the interop harness can't run: {_MISSING}", pytrace=False)


def _harness(tmp_path_factory: pytest.TempPathFactory, *args: str) -> list[Check]:
    assert NODE is not None
    out = tmp_path_factory.mktemp("interop") / "checks.json"
    done = subprocess.run(
        [NODE, str(INTEROP / "run.mjs"), "--vectors", str(VECTORS), "--json", str(out), *args],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert out.exists(), done.stdout + done.stderr
    checks: list[Check] = json.loads(out.read_text())["checks"]
    return checks


@pytest.fixture(scope="module")
def checks(tmp_path_factory: pytest.TempPathFactory) -> list[Check]:
    adapter = (
        f'"{sys.executable}" -m iab_agentic_primitives.web_bot_auth.conformance adapter --lines'
    )
    return _harness(tmp_path_factory, "--persistent", "--adapter", adapter)


@pytest.fixture(scope="module")
def judge_checks(tmp_path_factory: pytest.TempPathFactory) -> list[Check]:
    return _harness(tmp_path_factory, "--self")


def _failures(checks: list[Check]) -> list[str]:
    return [
        f"{c['suite']} {c['id']}: expected {c['expected']}, {c['detail']}"
        for c in checks
        if not c["ok"]
    ]


def _pick(checks: list[Check], *prefixes: str) -> list[Check]:
    found = [c for c in checks if c["id"].startswith(prefixes)]
    assert found, prefixes
    return found


def test_every_check_passes(checks: list[Check]) -> None:
    assert _failures(checks) == []


def test_the_judge_passes_its_own_tier_check(judge_checks: list[Check]) -> None:
    assert _failures(judge_checks) == []


def test_python_signed_requests_verify_in_cloudflare(checks: list[Check]) -> None:
    for c in _pick(
        checks, "Python signs POST, Cloudflare verifies", "Python signs GET, Cloudflare verifies"
    ):
        assert (c["cloudflare"], c["cloudflare_bare"]) == ("pass", "pass"), c


def test_cloudflare_signed_requests_verify_in_python_in_both_forms(checks: list[Check]) -> None:
    for method in ("POST", "GET"):
        for form in ("the key URL", "the bare origin"):
            (c,) = _pick(checks, f"Cloudflare signs {method} with {form}, Python verifies")
            assert (c["python"], c["cloudflare"], c["cloudflare_bare"]) == (
                "pass",
                "pass",
                "pass",
            ), c
        (replay,) = _pick(checks, f"Cloudflare signs {method}, Python verifies it twice")
        assert replay["python"] == replay["cloudflare"] == "429 nonce-replayed"


def test_tampered_copies_fail_in_both(checks: list[Check]) -> None:
    tampered = [c for c in checks if "both refuse" in c["id"]]
    assert len(tampered) >= 8
    for c in tampered:
        assert c["python"] != "pass" and c["cloudflare"] != "pass", c
        if c["tier"] == "wba":
            assert c["cloudflare_bare"] != "pass", c


def test_directories_interoperate(checks: list[Check]) -> None:
    signed = _pick(checks, "Python signs the directory, Cloudflare verifies")[0]
    assert (signed["cloudflare"], signed["cloudflare_bare"]) == ("pass", "pass")
    assert _pick(checks, "Cloudflare signs the directory, Python verifies")[0]["python"] == "pass"


def test_the_authority_scenario_is_refused_by_both_for_its_rule(checks: list[Check]) -> None:
    (c,) = _pick(checks, "R26 ")
    assert (c["python"], c["cloudflare"], c["cloudflare_bare"]) == (
        "403 authority",
        "403 authority",
        "pass",
    )
    assert c["tier"] == "profile"


def test_responses_interoperate(checks: list[Check]) -> None:
    assert _pick(checks, "Python signs a response, Cloudflare verifies")[0]["cloudflare"] == "pass"
    assert _pick(checks, "Cloudflare signs a response, Python verifies")[0]["python"] == "pass"


def test_every_vector_and_scenario_gets_the_same_verdict_and_rule_in_both(
    checks: list[Check],
) -> None:
    graded = [c for c in checks if c["suite"] in ("vectors", "scenarios")]
    assert len(graded) >= 60
    for c in graded:
        # A refusal's verdict is its status and rule, so the two agree rule by rule.
        assert c["python"] == c["cloudflare"], c
        if c["tier"] == "wba":
            assert (c["cloudflare_bare"] == "pass") == (c["python"] == "pass"), c
        else:
            assert c["tier"] == "profile"
            assert (c["cloudflare_bare"] == "pass") != (c["python"] == "pass"), c


DOC = Path(__file__).resolve().parents[2] / "WEB_BOT_AUTH.md"


def _doc_table() -> list[tuple[str, ...]]:
    """The interop table in WEB_BOT_AUTH.md, one tuple of cells per check."""
    text = DOC.read_text()
    start = text.index("| Suite | Check | Tier |")
    table = text[start : text.index("\n\n", start)]
    rows = [line.strip("|").split(" | ") for line in table.strip().splitlines()[2:]]
    return [tuple(cell.strip() for cell in row) for row in rows]


def test_the_doc_table_is_the_harness_output(checks: list[Check]) -> None:
    def cell(value: str | None) -> str:
        return "-" if value is None else value

    harness = [
        (
            c["suite"],
            c["id"],
            c.get("tier") or "-",
            cell(c.get("python")),
            cell(c.get("cloudflare")),
            cell(c.get("cloudflare_bare")),
        )
        for c in checks
    ]
    assert _doc_table() == harness
    assert f"All {len(checks)} checks pass." in DOC.read_text()
