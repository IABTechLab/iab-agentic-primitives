# Conformance Kit (EP-6.1)

The conformance kit is the executable "are we standards-aligned?" check
for the shared contract. Both agent repos run it in CI (continuous
integration); a red check IS the gap list. It lives in
`src/iab_agentic_primitives/conformance/`, its golden vectors live in
`spec/fixtures/`, and its output is the machine-readable
`gap_report.json` plus a human-readable table (the EP-6.2 gap-report
generator).

Acronyms used below (defined on first use): IAB = Interactive Advertising
Bureau; JSON = JavaScript Object Notation; FD = flagged decision (the
remediation plan's decision register); PG / PD / PA = Programmatic
Guaranteed / Preferred Deal / Private Auction; GRP = gross rating point;
CPM / CPP = cost per mille / cost per point; A2A = Agent-to-Agent
protocol; JSON-RPC = JSON Remote Procedure Call; AAMP = the IAB Tech Lab
agent discovery and trust registry; AdCOM = Advertising Common Object
Model; OpenRTB = Open Real-Time Bidding; GPP = Global Privacy Platform;
TCF = Transparency & Consent Framework; UTC = Coordinated Universal Time.

## How to run

From a source checkout of this repo (or with `--fixtures` pointing at
one — the golden fixtures are spec artifacts, not wheel payload):

```bash
uv run python -m iab_agentic_primitives.conformance
```

This runs every check, writes `gap_report.json` to the current
directory, and prints the human table. Exit codes: `0` = conformant,
`1` = gaps found (read `gap_report.json`), `2` = runner could not start
(missing fixtures directory).

Options:

```text
--fixtures PATH   golden-fixture directory (default: <repo>/spec/fixtures)
--schemas PATH    checked-in JSON Schema directory (default: <repo>/spec/jsonschema)
--output PATH     where to write gap_report.json (default: ./gap_report.json)
--quiet           suppress the human table
--regenerate      rewrite the checked-in fixtures from the builders, then exit
```

Programmatic use (what a downstream test suite calls):

```python
from iab_agentic_primitives.conformance import run_conformance

report = run_conformance()
assert report.conformant, report.render_table()
```

Optional dependency: if the third-party `jsonschema` package is
installed, every valid vector is additionally validated directly against
the checked-in JSON Schema (`schema` check). Without it, that check is
recorded as SKIP — never silently dropped — and the same property is
still pinned indirectly: vectors validate against the models
(`validate`), and the models' generated schemas must byte-match the
checked-in schema files (`schema_sync`).

## What the runner checks

Every fixture vector under `spec/fixtures/<area>/<Target>.golden.json`
(`area` is `primitives`, `protocol`, `events`, or `state`) is replayed:

| Check | Assertion |
| --- | --- |
| `validate` | the vector's `data` validates against the shared model |
| `roundtrip` | re-serializing byte-matches the vector under canonical JSON (sorted keys, compact separators); failures carry a field-level diff |
| `must_ignore` | FD-13: unknown fields and `x_`-prefixed extension fields parse and do NOT survive re-serialization (byte-compare against the vector's `expected`) |
| `expected_invalid` | mode-`invalid` vectors MUST fail validation with an error containing `expected_error` — this is how FD-11 (float-typed money is rejected) is executable |
| `schema` | direct validation against the checked-in JSON Schema (needs the optional `jsonschema` package; SKIP otherwise) |
| `schema_sync` | the model's generated JSON Schema byte-matches `spec/jsonschema/` |
| `state_sequence` | legal/illegal transition sequences replay identically through the checked-in JSON transition exports (`spec/jsonschema/state/`) AND the Python machines; any disagreement is a gap |
| `state_spec_sync` | the machines' spec exports byte-match the checked-in state artifacts |
| `fixture` | every wire primitive, protocol message, and event has a checked-in fixture (a missing fixture is itself a gap) |

Vector coverage highlights: PG/PD/PA deal types, the FD-6 linear TV
quote and structured `unsupported_capability` rejection, Money boundary
values (1 micro and 2^53−1 micros — the largest integer an IEEE 754
double can represent exactly), terminal statuses, the makegood
ChangeRequest subtype, and FD-12 idempotency keys on every
money-mutating request.

## How to add a vector

1. Edit `src/iab_agentic_primitives/conformance/vectors.py`. Vectors are
   built as validated model instances (helpers `_valid`, `_must_ignore`,
   `_invalid`), so an unrepresentable vector cannot be checked in by
   accident. Pin every timestamp/identifier — regeneration must be
   byte-stable (use the module's `T0`/`T1`/`T_EXPIRES` constants).
2. Regenerate the checked-in fixtures:
   `uv run python -m iab_agentic_primitives.conformance --regenerate`
3. Run `uv run pytest` — `tests/test_conformance.py` fails if the files
   and builders drift, if a target drops below 2 valid vectors, or if a
   major envelope loses its FD-13 must-ignore variant.

Non-Python implementations replay the fixture files directly; the JSON
in `data` is the wire truth (camelCase for the A2A envelope, snake_case
everywhere else), and `mode` tells the harness what to assert.

State sequences live in `spec/fixtures/state/*.golden.json` with
`expect: legal | illegal`; illegal sequences carry `fails_at`, the
zero-based index of the transition that must be rejected (steps before
it must be legal).

## The UNVERIFIED policy

`conformance/standards.py` is the registry of every standard the
contract claims alignment with. Each entry is either:

- **implemented** — mapped to the concrete check kinds the runner
  executes today (the contract's own guarantees: self-conformance,
  FD-11 integer-micros money, FD-13 forward compatibility, the EP-1.4
  lifecycle machines); or
- **UNVERIFIED** — an explicit marker with a `missing` description of
  exactly what has not been checked.

Every gap report prints the full registry, so there are no silent
coverage claims. "We model a field named after the standard" is not
"we verified conformance against the published standard" — the following
remain UNVERIFIED until a later bead adds fidelity checks against the
publications themselves: OpenDirect 2.1, IAB Deals API v1.0, AAMP,
A2A/JSON-RPC (published-spec fidelity), AdCOM/OpenRTB supply chain,
sellers.json/ads.txt, GPP, and TCF. External-spec details are never
fabricated into vectors; this kit asserts vectors derivable from our own
spec artifacts.

## Report format

`gap_report.json` (the EP-6.2 machine-readable shape):

```json
{
  "generated_at": "...",
  "library_version": "0.1.0",
  "summary": {
    "conformant": true,
    "checks": 310, "passed": 237, "failed": 0, "skipped": 73,
    "targets": 41, "vectors": 115, "gaps": 0,
    "standards_unverified": 8,
    "by_area": {"primitives": {"pass": 109, "fail": 0, "skip": 39}, "...": {}}
  },
  "gaps": [
    {
      "area": "primitives", "target": "Quote", "vector": "pg_digital_guaranteed",
      "check": "roundtrip", "message": "re-serialized bytes differ ...",
      "diff": [{"path": "$.pricing.base_cpm.currency", "expected": "<absent>", "actual": "\"USD\""}]
    }
  ],
  "standards": [
    {"id": "opendirect-2.1", "name": "IAB OpenDirect", "version": "2.1",
     "status": "unverified", "checks": [], "missing": "No fidelity check against ..."}
  ],
  "checks": [{"area": "...", "target": "...", "vector": "...", "check": "...", "status": "pass", "detail": ""}]
}
```

Skips are reported (count + per-check detail), never hidden; the
`conformant` flag refers only to executed checks.
