# Changelog

All notable changes to this project are documented in this file. The
project follows semantic versioning (semver): a breaking schema change is a
major version bump.

## [Unreleased]

### Added

- Avails surface (`POST /products/avails`): `protocol.AvailsRequest` /
  `protocol.AvailsResponse`, the canonical home of the availability +
  pricing query the seller shipped in v2.1.0 and the buyer's OpenDirect
  client already speaks. The served OpenDirect 2.1 dialect is preserved
  byte-for-byte (spec-lowercase `productid`/`startdate`/`enddate`,
  camelCase extension fields) so existing payloads round-trip identically.
  Settled policy encoded: `availableImpressions` REQUIRED (uncapped
  products report requested-as-available); `deliveryConfidence` OPTIONAL
  and omitted entirely when there is no forecast data source (never
  fabricated or null-padded; readers tolerate legacy `null`);
  `guaranteedImpressions` present ONLY for PG-capable products. Money
  fields on this surface remain floats — a documented FD-11 exception to
  keep the live wire compatible; `Money` micros migration is reserved for
  the next major version. Ships with exported JSON Schemas
  (`spec/jsonschema/protocol/Avails*.json`), the OpenAPI path, golden
  conformance vectors, and reconciliation notes
  (PROTOCOL_RECONCILIATION.md).

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
- `ReferenceBuyer` negotiation band (internal tracking):
  `negotiation_band_per_mille` constructor knob (default
  `DEFAULT_NEGOTIATION_BAND_PER_MILLE = 1250`, i.e. 1.25x). Quotes above
  `max_cpm` but within the band are NEGOTIABLE instead of discarded: the
  buyer opens a real `NegotiationMessage` at its true ceiling, never bids
  above it, accepts iff the seller's counter is <= `max_cpm`, holds its
  standing price while the seller spends its bounded rounds, and walks
  honestly otherwise. `1000` restores the strict legacy filter. Beyond the
  band remains filtered without negotiation.

### Changed

- `ReferenceBuyer` guardrails are now checked at the EFFECTIVE price: after
  an accepted negotiation, the budget ceiling (and a new explicit `max_cpm`
  ceiling guard) apply to the agreed price (final round's seller price), not
  the pre-negotiation quote price. The buyer still NEVER books above
  `max_cpm`.
- `ReferenceSeller.book_deal` books at the NEGOTIATED price when the quote
  has an accepted negotiation (the deal's `final_cpm` is the agreed price,
  rationale annotated), instead of silently booking the stale quote price.
- `ReferenceSeller` terminal negotiation round (`MAX_SELLER_ROUNDS`): if the
  buyer's standing price is at/above the private floor the seller ACCEPTS it
  rather than walking away from a profitable deal; below the floor it still
  rejects. Termination is unchanged (bounded rounds, never stuck 'active').

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
