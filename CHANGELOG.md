# Changelog

All notable changes to this project are documented in this file. The
project follows semantic versioning (semver): a breaking schema change is a
major version bump.

## [Unreleased]

### Changed

- **Breaking:** name-length bounds conformed to the OpenDirect 2.1 spec
  (the IAB direct-buying API standard): `Product.name` max length 128 ->
  100; `Organization.name` and `Account.name` max length 128 -> 120.
  Bounds already matching spec (`Creative.name` 255, the lifecycle `name`
  fields at 200) are unchanged. Breaking for any integrator currently
  sending names in the 101-128 (Product) or 121-128 (Organization/
  Account) range.

### Added

- `NegotiationMessage.agent_url` (optional): the sender's registered A2A
  agent URL, for registry trust-tier verification. Optional for backward
  compatibility with callers that predate this field.
- `DealType.CUR`: curated package deal (floor-priced, bid-based, curated
  inventory bundled under one deal ID via an SSP-native curation
  product — e.g. Index Inventory/Auction Packages, PubMatic Auction
  Packages, Magnite Curate). Distinct from `PA` (private auction, the
  general private-marketplace tier); named `CUR` rather than "PMP"
  because bare "PMP" is ambiguous in industry usage and commonly refers
  to the `PA` tier instead. Seller-internal "PMP curated deals" map to
  `CUR` on the wire. See the `DealType` docstring for the full
  disambiguation.

## [0.5.1] - 2026-08-10

First tagged release published on GitHub.

### Fixed

- Version single-sourcing
  ([#2](https://github.com/IABTechLab/iab-agentic-primitives/issues/2)):
  `iab_agentic_primitives.__version__` was hardcoded to `0.1.0` while
  `pyproject.toml` declared `0.5.0`. The runtime version is now read from
  installed package metadata via `importlib.metadata`, so it always matches
  the version declared in `pyproject.toml`; a regression test pins the
  invariant.

## [0.5.0] - 2026-07-28

### Changed

- Known behavior note: invalid LEGACY avails requests now return 422
  details that include ProductAvailsSearch union errors with altered
  `loc` paths (success and 404 responses are byte-identical to v0.4.0).

### Added

- OpenDirect 2.1 dialect convergence for the avails surface (v0.5.0
  content): the published wire shapes now live in the contract alongside
  the legacy simplified profile — `protocol.ProductAvailsSearch` (the
  spec's multi-product request: `productids` array + required
  `accountid`/`advertiserbrandid`), `protocol.Avails` (the spec
  per-product response record) with `protocol.AvailsStatus` /
  `protocol.ProductTargeting` (spec `availsstatus` semantics: Available /
  Partially Available / Unavailable with the spec's enumerated reasons),
  and `protocol.AvailsCollection` (the `avails` collection envelope the
  spec's Collection Objects table requires for `POST /products/avails`
  responses). Servers accept BOTH request dialects
  (`parse_avails_request` discriminates on `productids` vs `productid`);
  the response dialect follows the request dialect, so every
  v2.1.0–v2.2.1 legacy payload round-trips unchanged (pinned by tests).
  Dialect bridge helpers ship with the contract:
  `AvailsRequest.to_spec()` emits a strictly spec-shaped request
  (extension fields travel as minted Investment `producttargeting`
  entries and the AdCOM Segment `targeting` array),
  `ProductAvailsSearch.to_simplified()` recovers the legacy queries, and
  `avails_from_simplified()` derives spec `availsstatus` from the
  honest-availability numbers. Ships with exported JSON Schemas, the
  dual-dialect OpenAPI path, and golden conformance vectors (valid +
  must-ignore + invalid) for all three new messages.

  **Breaking-change register (release planning):** none on the wire for
  existing traffic — the convergence is additive. (1) Legacy requests
  and responses are byte-for-byte unchanged. (2) A strictly spec-shaped
  `ProductAvailsSearch` request, previously rejected with a validation
  error, now succeeds and returns the spec envelope — a behavior
  addition, not a break. (3) Emitting the spec dialect requires
  `accountid`/`advertiserbrandid`, which the legacy profile never
  carried; buyers without account context must stay on the legacy
  dialect (documented in PROTOCOL_RECONCILIATION.md). The FD-11
  float-money exception on this surface is unchanged.

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
