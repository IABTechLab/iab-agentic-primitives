# Protocol reconciliation record — EP-1.3: the ONE wire protocol

This document is the audit trail for how each protocol surface in
`src/iab_agentic_primitives/protocol/` was reconciled from the two agent
repos. Companion documents: RECONCILIATION.md (the primitives, EP-1.2) and
EVENTS_RECONCILIATION.md (the event vocabulary, EP-1.5). Buyer sources are
under `buyer-agent/src/ad_buyer/`; seller sources under
`seller-agent/src/ad_seller/`. Flagged decisions (FD-n) refer to the
register in `IAB_Agents_Remediation_Plan_20260713.md` §7.2.

Acronyms, defined once: IAB = Interactive Advertising Bureau; A2A =
Agent-to-Agent protocol; JSON-RPC = JSON (JavaScript Object Notation)
Remote Procedure Call; AAMP = the IAB Tech Lab agent discovery and trust
registry; OpenDirect = the IAB direct-buying API standard; DSP =
demand-side platform; SSP = supply-side platform; CPM = cost per mille
(thousand impressions); GRP = gross rating point; PG/PD/PA = Programmatic
Guaranteed / Preferred Deal / Private Auction; TTL = time to live; HTTP =
Hypertext Transfer Protocol; UUID = universally unique identifier; FD =
flagged decision.

## Global decisions (apply to every surface)

| # | Decision | Rationale |
|---|---|---|
| P1 | **Envelopes wrap primitives; they never redefine them.** `QuoteResponse.quote` is the shared `Quote`, `DealBookingResponse.deal` the shared `Deal`, `NegotiationRoundResponse.round` the shared `NegotiationRound`, `ChangeRequestResponse.change_request` the shared `ChangeRequest`. | One definition per exchanged object; the envelope adds only transport-level fields. |
| P2 | **Idempotency (FD-12): every money-mutating request inherits `IdempotentRequest`** — `QuoteRequest`, `DealBookingRequest`, `NegotiationMessage`, `ChangeRequestCreate` all carry a REQUIRED `idempotency_key`. Replay semantics: same key → same response, no duplicate side effects; same key + different body → `idempotency_conflict` error. Keys are requester-minted opaque strings (UUID recommended), scoped per buyer/seller pair. | A retried booking or counter must not create a duplicate deal or consume an extra round. Neither repo had any idempotency mechanism. |
| P3 | **ONE error envelope**: `{"detail": {"error": <ErrorCode>, "message": ..., "unsupported": [...]}}` — the FastAPI `HTTPException(detail=<dict>)` encoding the quote→book path already used on both sides. The buyer's flat fallback (`{"error", "detail"}`) is retired. Canonical `ErrorCode` enum replaces the seller's ad-hoc per-endpoint strings (mapping in `protocol/errors.py`). | The buyer's `DealsClient` had to speculatively parse two shapes; now there is one. |
| P4 | **Money is `Money` (FD-11)** on every protocol price field: `QuoteRequest.target_cpm`, `NegotiationMessage.buyer_price`. Float prices do not validate. | Same rule as the primitives; the negotiation floats were the worst offender. |
| P5 | **Unknown fields are ignored (FD-13)**; the `x_` prefix is reserved for vendor extensions. Protocol messages inherit `WireModel`. | Forward compatibility for third-party implementers. |
| P6 | **Typed enums at the edge**: `deal_type` is the shared `DealType` (`PG`/`PD`/`PA`), `media_type` the shared `MediaType`, `action` the shared `NegotiationAction`. Untyped `str` fields with comment-documented vocabularies are retired. | Comment-typed strings are how the two repos drifted. |
| P7 | **snake_case wire naming (G1 in RECONCILIATION.md), with ONE documented exception**: the A2A JSON-RPC envelope follows the external A2A specification's camelCase (`messageId`, `contextId`, `taskId`) via serialization aliases. | Conformance to a published external standard beats internal consistency. |

## Per-surface reconciliation

| Surface | Buyer-sent shape | Seller-served shape | Canonical decision | Breaking notes |
|---|---|---|---|---|
| **Catalog list** | `GET /products?$skip&$top` (OpenDirect casing, `opendirect_client.py`) and A2A natural-language listing | `GET /products` → `{"products": [...]}` unpaginated (`api/main.py:684`) | `GET /products?limit&offset` → `ProductListResponse` (`products`, `total_count`, `limit`, `offset` echo), items = the shared `Product` primitive | Seller adds pagination + `total_count`; buyer drops `$skip`/`$top` casing. |
| **Catalog search** | `POST /products/search` with filter dict (`opendirect_client.py:100`) | **no route → 405** | **NO `/products/search` — deleted from the contract** (plan §7 amendment 3). The list response carries every filterable field (`ad_formats`, `delivery_type`, `pricing_model`, `pricing_type`, `base_price`, `available_impressions`, taxonomy blocks); the buyer filters client-side. Free-text discovery stays on the media-kit search surface. | Buyer's `search_products` re-implemented client-side; no server work. |
| **Catalog detail** | `GET /products/{id}` | `GET /products/{id}` (ad-hoc `_serialize_product` dict) | `GET /products/{product_id}` → the `Product` primitive, no wrapper | Seller serializes the shared primitive instead of a hand-rolled dict. |
| **Quote create** | `QuoteRequest` in `models/deals.py`: sends `agent_url`, `media_type`, `linear_tv`, `audience_plan`; float `target_cpm` | Inline `QuoteRequestModel` (`api/main.py:304`): **silently drops** `agent_url`/`media_type`/`linear_tv`/`audience_plan`; float `target_cpm`; ad-hoc error strings | `protocol.QuoteRequest`: buyer's field set wins and becomes REQUIRED-to-carry (`media_type`, `linear_tv` slot, `audience_plan` slot, `agent_url`, plus `rate_card_id` per FD-9); typed `DealType`/`MediaType`; `target_cpm: Money`; `idempotency_key` required; cross-field validator ties `linear_tv` params to `media_type=linear_tv`. Response = `QuoteResponse{quote: Quote}`. | Seller must implement or structurally reject each newly-carried field — silent dropping is banned (FD-6): unsupported `media_type` → `unsupported_capability` + `unsupported: [{"capability": "linear_tv"}]`. Buyer switches `target_cpm` to micros. |
| **Quote fetch** | `GET /api/v1/quotes/{id}` → flat quote dict | same, plus `410` `quote_expired` | Same path; response is `QuoteResponse` envelope; `410`/`quote_expired` kept as canonical | Response gains the `quote` wrapper key. |
| **Deal booking** | `DealBookingRequest` (`quote_id`, `buyer_identity`, `notes`, `audience_plan`) | `DealBookingRequestModel` (same fields; `audience_plan: dict`) — the one surface that already agreed | `protocol.DealBookingRequest` = that shape + required `idempotency_key` + `consent_context` slot (FD-10). Response = `DealBookingResponse{deal: Deal, audience_plan_snapshot, audience_match_summary}` | Response gains the `deal` wrapper key; `openrtb_params.bidfloor` becomes `Money` via the Deal primitive. |
| **Deal booking errors** | `DealsClient._build_error_from_response` (`deals_client.py:437-492`) parses `{"detail": {"error", "message"/"detail", "unsupported": [...]}}` | raises `HTTPException(detail={"error": ..., "unsupported": [...]})` (`api/main.py:2480-2497`) | This symmetric wrapped shape IS the canonical `ErrorEnvelope`; `unsupported` items typed as `{capability, path?, reason}` with a normalizing validator accepting the legacy `{path, reason}` entries and bare strings | `audience_plan_unsupported` code string → `unsupported_capability`; other legacy codes remapped (see `errors.py`). |
| **Makegood** | `POST /api/v1/deals/{id}/makegoods` (`MakegoodRequest`, `linear_tv.py`) | **no route** | Typed `ChangeRequest` subtype (FD-6): `POST /api/v1/change-requests` with `change_type=makegood` + `MakegoodDetails` (same fields as the buyer's `MakegoodRequest`). Sub-route retired. | Buyer's dead `request_makegood` client call must be re-pointed (or stay disabled until the seller implements change requests). |
| **Cancellation** | `POST /api/v1/deals/{id}/cancel` (`CancellationRequest`) | **no route** | `POST /api/v1/change-requests` with `change_type=cancellation`; `cancel_pct`/`reason`/`effective_date` ride `proposed_values`. Sub-route retired. | Same as makegood. |
| **Negotiation** | `negotiation/client.py` POSTs `{"price": <float>}` (and `{"action": "accept"/"decline"}` ad hoc) to `/proposals/{id}/counter` | `CounterOfferRequest` requires `buyer_price: float`, has **no `action` field** (`api/main.py:286`) → **422 every round**; response is an ad-hoc dict | `POST /api/v1/negotiations/messages` with `NegotiationMessage` (FD-5): REQUIRED `action: NegotiationAction` (`accept`/`counter`/`reject`/`final_offer`), `buyer_price: Money` (required for counter/final_offer, forbidden on reject), `negotiation_id` (None opens; seller mints) or `proposal_id`/`quote_id`, optional `round_number` for optimistic concurrency, required `idempotency_key`. Response = `NegotiationRoundResponse{negotiation_id, status, round: NegotiationRound, rounds_remaining}`. `GET /api/v1/negotiations/{id}` returns the `Negotiation` primitive. Buyer verb mapping: offer→`counter` (no id), decline→`reject`; accept/reject are TERMINAL (walk-away recorded as a round); further messages → `negotiation_closed`. | `/proposals/{id}/counter` retired; both sides validate the SAME model, so the bare-`price` payload is unrepresentable — tested in `tests/test_protocol.py`. |
| **A2A messaging** | JSON-RPC `method: "message/send"`, `params.message{messageId, role, parts[{kind,text/data}]}` — the A2A-spec dialect (`clients/a2a_client.py`) | JSON-RPC `method: "call"`, `params.request: <str>` — homegrown (`clients/a2a_client.py:153`) | **`message/send` wins** (spec-conformant). `JsonRpcRequest.method` is a Literal, so `"call"` does not validate; servers answer it with JSON-RPC error `-32601` (method not found). camelCase aliases per the A2A spec (the one G1 exception). | Seller's A2A client/server must migrate to `message/send`. |
| **Agent Card** | `registry/models.AgentCard`: `agent_id`, `url`, `protocols: list[str]`, `capabilities: list[AgentCapability]`, `trust_level` (with `verified`) | `models/agent_registry.AgentCard`: no `agent_id`, typed `AgentCapabilities` object, `skills`, `provider`, `authentication`, `audience_capabilities`; separate `RegisteredAgent` wrapper; lowercase deal types (`pg`, `pmp`, ...) | ONE card = the shared **`Agent` primitive** (re-exported as `protocol.AgentCard`), served at `GET /.well-known/agent.json`. Seller's structure wins (capabilities stays a TYPED object); buyer's capability list maps to `skills`; AAMP trust kept on the record: `trust_status` is registry-verified, never self-asserted (self-served card = `unknown`; `verified` → `approved`); `supported_deal_types` uses `PG`/`PD`/`PA`. `AgentTrustVerification` replaces the buyer's `AgentTrustInfo` (`is_registered` derivable). | Buyer's card model and `TrustLevel.VERIFIED` retired; seller's lowercase deal-type strings retired. |
| **Sessions** | `sessions/session_manager.py` POSTs `{"buyer_identity": {...}}` to `/sessions` | `CreateSessionRequest` takes **flat** `seat_id`/`agency_id`/... (`api/main.py:939`) | Out of EP-1.3 scope — noted here so the mismatch is on the record: the canonical shape should take the nested `BuyerIdentity` primitive. The `Session` primitive itself landed in EP-1.2. | Deferred to the session-surface bead. |

## FD-7 assumption (published Deals API v1.0)

The published overview at `iabtechlab.com/standards/dealsapi/` (fetched
2026-07-13) describes v1.0 as a **one-way deal PUSH from the origin system
to receiving systems (e.g. SSP → DSP) plus a status query**, and states it
explicitly excludes "proposals, revisions, or negotiations". The full
spec text (field names, entity definitions for seller/packager/curator,
error shapes) was not reachable from this environment, so per the bead
instruction the repos' quote→book path — the one conformant surface, and
a do-not-touch item — is followed for the request/booking wire semantics
defined here, and entity naming aligns with the public overview where it
defines terms (`seller` and `curator` are already canonical
`OrganizationRole` values; `packager` maps to the existing `curator`
role — a party that packages inventory without owning or executing it —
until the full spec text says otherwise). **Open reconciliation item:**
when the published v1.0 spec text is obtained, diff `QuoteRequest`/
`DealBookingRequest`/`Deal` field naming and the push/query surface
against it; where they differ the published standard wins (FD-7).

## Idempotency replay semantics (FD-12) — normative summary

1. Every `POST` that can move money (`/api/v1/quotes`, `/api/v1/deals`,
   `/api/v1/negotiations/messages`, `/api/v1/change-requests`) requires
   `idempotency_key` in the body; requests without it fail validation.
2. First use of a key: process normally, persist `(key → response)` for at
   least the resource's TTL.
3. Replay with the same key and same body: return the stored response
   verbatim; do NOT re-execute the side effect (no second quote/deal/
   round/change request).
4. Same key, different body: reject with `error=idempotency_conflict`.
5. Keys are scoped per buyer/seller pair; they are opaque and never
   interpreted.

## Explicit non-goals (stated so their absence is a decision, not an accident)

- **Settlement / invoicing**: out of scope for the reference contract
  (FD-6); no protocol surface represents money owed or paid.
- **Negotiation strategies**: only the message schema and the basic
  offer→counter→accept/decline loop are contractual (FD-5); multi-round
  strategy logic stays agent-local.
- **Typed audience plan / match summary**: carried as open objects
  (`audience_plan`, `audience_match_summary`); the typed models land with
  the audience-plan bead, and the slots exist now so the wire shape does
  not break when they do.
- **State transition rules**: the enums ride the primitives; lifecycles
  are EP-1.4 (`iab_agentic_primitives.state`).
