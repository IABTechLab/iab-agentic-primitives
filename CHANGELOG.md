# Changelog

All notable changes to this project are documented in this file. The
project follows semantic versioning (semver): a breaking schema change is a
major version bump.

## [Unreleased]

### Added

- `harness` subpackage (EP-7.1): an in-process two-agent interop test rig
  that runs BOTH sides of a transaction against the shared contract instead
  of mocking the counterparty. Ships the `BuyerRole`/`SellerRole`/
  `SellerChannel` interfaces (canonical envelopes only — no free text on the
  money path), deterministic LLM-free `ReferenceBuyer`/`ReferenceSeller`, an
  in-process `run_scenario` runner that wires N buyers x M sellers through the
  real sandbox AAMP registry (httpx ASGITransport), and reusable interop
  assertions (quote round-trip, no dropped `agent_url`/`linear_tv`/
  `audience_plan`, negotiation termination, seller-minted deal id, trust-tier
  enforcement, idempotent booking, state reconciliation). Golden scenarios in
  `tests/test_harness.py`; consumption guide in `HARNESS.md`. New `harness`
  optional extra (httpx + fastapi).

## [0.1.0] - 2026-07-13

### Added

- Initial package scaffold: `iab_agentic_primitives` with empty
  `primitives`, `protocol`, `state`, `events`, and `conformance`
  subpackages (module docstrings only; domain models land in later work
  items).
- `spec/` directory structure (`openapi/`, `jsonschema/`, `fixtures/`) for
  the normative language-neutral contract artifacts, with README
  establishing the spec-is-normative / package-is-reference-implementation
  relationship.
- CI (continuous integration) workflow running pytest and ruff on push and
  pull request.
- Packaging: hatchling build backend, Python >= 3.11, `pydantic>=2` as the
  sole runtime dependency.
