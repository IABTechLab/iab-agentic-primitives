# IAB Agentic Primitives — Standards Gap Report

> Generated from `gap_report.json` by `python -m iab_agentic_primitives.conformance --render-md STANDARDS_GAP_REPORT.md`. **Do not edit by hand** — a drift-guard test asserts this file matches a fresh render.

IAB = Interactive Advertising Bureau. This report answers a single question honestly: for each standard the shared contract claims alignment with, have we actually *verified* it, and if not, exactly what is missing and how to close the gap. "We model a field named after a standard" is never treated as "we verified conformance against the published standard".

## Executive summary

- **12 standards tracked** (library version `0.1.0`).
- **4 fully self-conformant** — the contract's own guarantees (primitives, protocol, state, events, money, idempotency, forward-compat), verified by executed checks against our spec artifacts.
- **0 partial** — an implemented claim whose mapped checks failed or did not execute.
- **8 unverified (pending external spec)** — claims that reference a published external standard with no fidelity check against that publication yet.

Conformance run: **396 checks**, 303 passed, 0 failed, 93 skipped across 147 vectors / 50 targets. Self-conformance verdict: **CONFORMANT**.

**Highest-priority gap: IAB OpenDirect 2.1** — field-level fidelity for the core commercial booking surface (Orders / Lines / Products) is the most consequential unverified claim. Obtain the published IAB OpenDirect 2.1 field specification (Order / Line / Product JSON schemas and canonical example payloads) and add field-level golden vectors derived from those examples; assert our reconciled Order/Line/Product shapes and the OpenDirect-cased LineStatus vocabulary round-trip against them.

## What is verified vs. what is not

Our **self-conformance is strong**: every wire primitive, protocol message, state machine, and event round-trips through the shared models; FD-11 (flagged decision) integer-micros money rejects float-typed money on the wire; FD-13 must-ignore forward-compat is executable; and the canonical Deal / Order / ChangeRequest lifecycle machines replay identically through their Python and JSON exports.

What is **not** verified is external-spec *fidelity*. The contract borrows names and shapes from published IAB and IAB Tech Lab standards, but this kit does not yet validate our payloads against those publications' own schemas and examples. Those claims are listed below as UNVERIFIED, each with a concrete note on how to close it. No external-spec detail is fabricated into a vector.

## Standards

### Self-conformant (verified against our spec artifacts)

#### IAB agentic-primitives shared contract (this spec) 0.1

- **Status:** CONFORMANT
- **Registry id:** `contract-self-conformance`
- **Checks:** `validate` (109 pass); `roundtrip` (93 pass); `schema` (93 skip); `schema_sync` (43 pass)

#### FD-11 Money as integer micros (float rejected on the wire) 0.1

- **Status:** CONFORMANT
- **Registry id:** `fd11-money-integer-micros`
- **Checks:** `expected_invalid(money-float)` (20 pass)

#### FD-13 must-ignore unknown fields + x_ extension prefix 0.1

- **Status:** CONFORMANT
- **Registry id:** `fd13-forward-compat`
- **Checks:** `must_ignore` (16 pass)

#### Canonical Deal/Order/ChangeRequest lifecycle machines (EP-1.4) 0.1

- **Status:** CONFORMANT
- **Registry id:** `state-lifecycles`
- **Checks:** `state_sequence` (18 pass); `state_spec_sync` (4 pass)

### Unverified — pending external published spec

#### IAB OpenDirect 2.1

- **Status:** UNVERIFIED
- **Registry id:** `opendirect-2.1`
- **Unverified / missing:** The avails surface now carries the published wire shapes transcribed from the OpenDirect 2.1 normative attribute tables (ProductAvailsSearch / Avails / AvailsStatus / ProductTargeting and the 'avails' collection envelope), with golden vectors, alongside the legacy simplified profile. Still missing: validation against a hash-pinned copy of the publication itself (the transcription is authored here), and any fidelity check for the remaining surfaces — the Order/Line/Product object tables, the required endpoint surface (URI Summary Table), and URI versioning.
- **To close this gap:** Obtain the published IAB OpenDirect 2.1 field specification (Order / Line / Product JSON schemas and canonical example payloads) and add field-level golden vectors derived from those examples; assert our reconciled Order/Line/Product shapes and the OpenDirect-cased LineStatus vocabulary round-trip against them.

#### IAB Deals API 1.0

- **Status:** UNVERIFIED
- **Registry id:** `iab-deals-api-1.0`
- **Unverified / missing:** No vectors derived from the published Deals API v1.0 examples/schemas. Internal vectors cover our reconciled quote->book envelopes only.
- **To close this gap:** Obtain the published IAB Deals API v1.0 schemas and example payloads and add vectors derived from them; assert our reconciled quote->book envelopes validate against the published request/response shapes.

#### IAB Tech Lab AAMP agent discovery and trust registry

- **Status:** UNVERIFIED
- **Registry id:** `aamp`
- **Unverified / missing:** No check against the AAMP registry specification. Agent card and trust-verification vectors cover the reconciled internal shape only; trust_status semantics are asserted by our own spec, not against AAMP publications.
- **To close this gap:** Obtain the IAB Tech Lab AAMP (agent discovery and trust registry) specification and add vectors that validate agent-card and trust-verification payloads against its published schema and trust_status vocabulary, rather than against our own reconciled shape.

#### A2A protocol / JSON-RPC 2.0 envelope

- **Status:** UNVERIFIED
- **Registry id:** `a2a-jsonrpc`
- **Unverified / missing:** No conformance run against the published A2A specification or test suite. Internal vectors cover the reconciled message/send envelope subset (camelCase aliases, -32601 for retired methods) as defined by this contract.
- **To close this gap:** Run the published A2A (Agent-to-Agent) / JSON-RPC 2.0 conformance suite, or derive vectors from its message/send examples, and assert our envelope subset (camelCase aliases, -32601 for retired methods) conforms to the publication rather than to this contract alone.

#### AdCOM / OpenRTB supply chain

- **Status:** UNVERIFIED
- **Registry id:** `adcom-openrtb-supply-chain`
- **Unverified / missing:** SupplyChain (schain) is now modeled (EP-10.3: SupplyChain / SupplyChainNode with OpenRTB asi/sid/hp/rid field names) and round-trip/schema checked, but NO fidelity check against the AdCOM/OpenRTB publications exists: complete/hp 0-1 semantics and node ordering are asserted by our own spec only.
- **To close this gap:** Model the SupplyChain (schain) object per AdCOM (Advertising Common Object Model) / OpenRTB (Open Real-Time Bidding) and add vectors that validate schain nodes, the `complete` flag, and AdCOM enum integers against the published specifications.

#### sellers.json / ads.txt

- **Status:** UNVERIFIED
- **Registry id:** `sellers-json-ads-txt`
- **Unverified / missing:** A sellers.json entry is now modeled (EP-10.3: SellersJsonEntry with seller_type PUBLISHER/INTERMEDIARY/BOTH + is_confidential) and round-trip/schema checked, but no ads.txt/sellers.json file parsing or cross-file crawl/validation exists.
- **To close this gap:** Model sellers.json and ads.txt records and add parser/validation checks against the published IAB Tech Lab formats (record grammar, seller_id/domain relationships, and the DIRECT/RESELLER distinction).

#### Global Privacy Platform (GPP)

- **Status:** UNVERIFIED
- **Registry id:** `gpp`
- **Unverified / missing:** gpp_string / gpp_section_ids are carried as opaque values on the EP-10.4 ConsentContext (alongside us_privacy); no GPP string decoding or section validation exists.
- **To close this gap:** Integrate a GPP (Global Privacy Platform) string decoder and add vectors that decode gpp_string / gpp_section_ids and validate section structure against the GPP specification, replacing the FD-10 (flagged decision) opaque placeholder.

#### Transparency & Consent Framework (TCF)

- **Status:** UNVERIFIED
- **Registry id:** `tcf`
- **Unverified / missing:** tcf_string / gdpr_applies are carried opaque on the EP-10.4 ConsentContext; no TC string decoding or vendor-list checks exist.
- **To close this gap:** Integrate a TCF (Transparency & Consent Framework) TC-string decoder and add vectors that decode tcf_string and validate it against a pinned GVL (Global Vendor List), replacing the FD-10 opaque placeholder.
