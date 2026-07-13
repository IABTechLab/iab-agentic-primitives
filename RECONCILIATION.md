# Reconciliation record — EP-1.2: unifying the "Both" wire primitives

This document is the audit trail for how each shared wire primitive in
`src/iab_agentic_primitives/primitives/` was reconciled from the two agent
repos. Buyer sources are under `buyer-agent/src/ad_buyer/models/`; seller
sources are under `seller-agent/src/ad_seller/models/` (plus the inline
request models in `interfaces/api/main.py`). Flagged decisions (FD-n) refer
to the register in `IAB_Agents_Remediation_Plan_20260713.md` §7.2.

Acronyms used below, defined once: IAB = Interactive Advertising Bureau;
PG = Programmatic Guaranteed; PD = Preferred Deal; PA = Private Auction;
CPM = cost per mille (thousand impressions); CPP = cost per (gross rating)
point; GRP = gross rating point; DMA = designated market area; CTV =
connected television; DSP = demand-side platform; A2A = agent-to-agent
protocol; AAMP = the IAB Tech Lab agent discovery and trust registry;
OpenDirect = the IAB direct-buying API standard; OpenRTB = Open Real-Time
Bidding; AdCOM = Advertising Common Object Model; GPP = Global Privacy
Platform; TCF = Transparency & Consent Framework; UTC = Coordinated
Universal Time.

## Global decisions (apply to every primitive)

| # | Decision | Rationale |
|---|---|---|
| G1 | **Canonical wire encoding is snake_case field names, no aliases.** The buyer used camelCase aliases (`publisherId`, `basePrice`), the seller used squashed-lowercase aliases (`productid`, `sellerorganizationid`) — three encodings for the same fields. All aliases are dropped. | One schema means one field spelling; aliases were the main silent-divergence vector. |
| G2 | **All timestamps are timezone-aware UTC `datetime`.** Sources mixed naive `datetime.utcnow()` defaults (seller `session.py`, `negotiation.py`, `change_request.py`, `agent_registry.py`, buyer `opendirect.py`) and raw ISO strings (`expires_at: str`, `created_at: str` in both quote stacks). `default_factory=utc_now` from `primitives/_util.py`; `datetime.utcnow` is banned. | Naive/aware mixing caused comparison bugs; strings escaped validation entirely. |
| G3 | **Flight/effective dates are `date`, not `str`.** Buyer and seller both carried `flight_start: str  # YYYY-MM-DD`. | Typed dates validate on the wire; formatting stays at the edge. |
| G4 | **Every ID field documents WHO mints it.** Convention: the AAMP registry mints identity ids (`organization_id`, `agent_id`); the seller mints all transactional resource ids (`account_id`, `product_id`, `package_id`, `media_kit_id`, `rate_card_id`, `quote_id`, `proposal_id`, `negotiation_id`, `deal_id`, `order_id`, `line_id`, `change_request_id`, `session_id`, `creative_id`, `assignment_id`) — consistent with OpenDirect, where the publisher API mints resource identifiers. Buyers reference by id. | Neither repo documented minting; both occasionally minted the other side's ids in tests. |
| G5 | **Enums only; no transition rules here.** State machines (buyer `state_machine.py`, seller `order_state_machine.py` transition tables/guards) are EP-1.4 (`iab_agentic_primitives.state`). | Task scoping per the bead. |
| G6 | **`ext` is the single extension slot** (OpenDirect convention). Seller's `metadata: dict` extension fields on Organization/Proposal map to `ext`; `Session.metadata` is kept by that name because it is conversational metadata, not a schema extension point. | One extension convention. |
| G7 | **Money is exact integer micros — float is banned on the wire (FD-11).** One shared `Money` type (`amount_micros: int` strict, `currency`: ISO 4217 alpha-3): 1,000,000 micros = 1 currency unit, the ad-industry convention (Google Ad Manager prices in micros). Both source repos used IEEE float end-to-end for CPMs, rates, budgets, and offers; that defect is not fossilized into the spec. Converted fields: `QuotePricing.base_cpm/final_cpm/base_cpp/final_cpp`, `RateCardEntry.rate`, `Product.base_price`, `Package.base_price`, `CommercialTerms.minimum_deal_value`, `PricingTerms.price`, `NegotiationRound.buyer_price/seller_price`, `Order.budget`, `Line.rate/cost`, `OpenRTBParams.bidfloor`, `LinearTVParams.target_cpp`, `LinearTVQuoteDetails.cpp`. Standalone `currency: str` fields that sat next to a now-`Money` field are dropped as redundant (`QuotePricing.currency`, `PricingTerms.currency`, `RateCardEntry.currency`, `CommercialTerms.currency`, `Product.currency`, `Package.currency`, `Order.currency`, and `OpenRTBParams.bidfloorcur` — adapters translate `Money` back to the raw OpenRTB float `bidfloor`/`bidfloorcur` encoding at the DSP edge). `MediaKit.currency` is kept as the catalog-level default. Percentages/ratios (`tier_discount_pct`, `sov`, `weight`, fill rates, concession caps) stay float — they are not money. | Deterministic money math (P3: deterministic core); float `0.1 + 0.2 != 0.3`. |
| G8 | **Unknown-field policy is must-ignore (FD-13).** Every wire primitive inherits `WireModel` with a deliberate `model_config = ConfigDict(extra="ignore")`; the `x_` field-name prefix is reserved for vendor extensions and will never be claimed by the spec. | Forward compatibility: a newer counterparty can add fields without breaking an older one. |
| G9 | **Request/response DTOs (data transfer objects) are protocol messages, not primitives** — e.g. `QuoteRequest`, `DealBookingRequest`, `AvailsRequest/Response`, `MediaKitSearchRequest`, `CounterOfferRequest`, `SellerErrorResponse`. They land in `iab_agentic_primitives.protocol` (a separate bead). Consistent with plan §4.4 (Avails is a query, not a primitive). | Keeps primitives = persisted objects with identity. |

## Enum reconciliation

### DealType — the wire encoding wins (ratified)

| Source | Values |
|---|---|
| Buyer `buyer_identity.DealType` | `"PG"`, `"PD"`, `"PA"` |
| Seller `core.DealType` | `"programmaticguaranteed"`, `"preferreddeal"`, `"privateauction"` |
| Both quote stacks | untyped `deal_type: str` |

**Decision:** ONE `DealType` with wire values `"PG"` (Programmatic
Guaranteed), `"PD"` (Preferred Deal), `"PA"` (Private Auction). Mapping from
the retired seller encoding: `programmaticguaranteed → PG`,
`preferreddeal → PD`, `privateauction → PA`. The long-form strings are not
valid wire values (tested). All `deal_type: str` fields are now typed.

### DealStatus — union of four competing vocabularies

| Source | Values |
|---|---|
| Buyer `deals.DealResponse.status` (comment-typed str) | proposed, active, rejected, expired, completed |
| Seller `quotes.DealBookingStatus` | proposed, active, expired, cancelled |
| Buyer `state_machine.BuyerDealStatus` | quoted, negotiating, accepted, booking, booked, delivering, completed, failed, cancelled, expired, makegood_pending, partially_canceled |
| Seller `freewheel.FWDealStatus` | ad-server adapter vocabulary — **seller-local, excluded** |

**Decision:** ONE `DealStatus`: `proposed, negotiating, accepted, booked,
active, makegood_pending, partially_cancelled, completed, rejected, failed,
cancelled, expired`. Recorded aliases (retired, rejected on the wire):

- `delivering` → `active` (same state: deal live and delivering)
- `booking` → `booked` (transient buyer-local step, not a wire state)
- `quoted` → not a deal state; that phase is `QuoteStatus`
- `partially_canceled` → `partially_cancelled` (spelling unified on double-l,
  matching `cancelled` which both repos already used elsewhere)

### OrderStatus — union of three competing vocabularies

| Source | Values |
|---|---|
| Buyer `opendirect.OrderStatus` | `PENDING`, `APPROVED`, `REJECTED` (uppercase) |
| Seller `core.ExecutionOrderStatus` | draft, proposed, booked, unbooked, canceled |
| Seller `order_state_machine.OrderStatus` | draft, submitted, pending_approval, approved, rejected, in_progress, syncing, completed, failed, cancelled, booked, unbooked |

**Decision:** adopt the seller's already-unified `order_state_machine`
vocabulary (it had merged the other seller set), minus `syncing`:
`draft, submitted, pending_approval, approved, rejected, in_progress,
booked, unbooked, completed, failed, cancelled`. Recorded aliases:
`PENDING → pending_approval`, `APPROVED/REJECTED → approved/rejected`
(lowercase), `proposed → submitted`, `canceled → cancelled`. `syncing` is a
seller-internal ad-server sync detail and is dropped from the wire.

### Other enums

| Enum | Sources | Decision |
|---|---|---|
| `QuoteStatus` | buyer comment string / seller `quotes.QuoteStatus` — identical (available, booked, expired, declined) | Adopted as-is, now typed on both sides. |
| `LineStatus` | buyer `opendirect.LineBookingStatus` (OpenDirect PascalCase: Draft…Expired); seller `core.PlacementStatus` (created/active/inactive/canceled) | OpenDirect vocabulary and casing win (published standard wins, FD-7 spirit). Seller `Placement` is internal; its status does not cross the wire. Field renamed `booking_status` → `status`. |
| `PricingModel` | buyer `opendirect.RateType` (CPM, CPMV, CPC, CPD, FlatRate); seller `core.PricingModel` (cpm, cpv, cpc, cpcv, flat_fee); linear TV strings ("cpm", "cpp", "unit_rate", "hybrid") | ONE lowercase enum: `cpm, cpmv, cpv, cpc, cpcv, cpd, cpp, flat_fee, unit_rate, hybrid`. Alias: `FlatRate → flat_fee`; uppercase values retired. |
| `PricingType` | seller `pricing_type.PricingType` only (fixed, floor, on_request) | Adopted verbatim; buyer previously had no signal and could fabricate prices for on_request inventory. |
| `MediaType` | buyer `deals.py` strings ("digital", "ctv", "linear_tv") | Typed enum (FD-6). |
| `AccessTier` | identical in both repos (public, seat, agency, advertiser) | Adopted verbatim. |
| `TrustStatus`, `AgentType` | seller `agent_registry.py` only | Adopted verbatim; the `TRUST_TO_TIER_MAP` policy table stays out of the contract (EP-5 verified-trust bead). |
| `GoalType`, `BillableEvent` | seller `core.py` (`viewableimpressions`, `viewableimpression`) | Adopted with snake_case wire values (`viewable_impressions`, `viewable_impression`) per G1. |
| `AdProfile` | seller `core.py` (`metadataonly`, `fulladcom`) | snake_case wire values (`metadata_only`, `full_adcom`) per G1. |
| `ProposalStatus` | seller `core.ProposalStatus` (lowercase) and `RevisionStatus` (uppercase twin) | Lowercase set adopted; the uppercase revision twin retires with the revision machinery (see Proposal). |
| `ChangeRequestStatus`, `ChangeType`, `ChangeSeverity` | seller `change_request.py` only | Adopted; `ChangeType` gains `makegood` (FD-6). |
| `NegotiationAction` | seller `negotiation.py` | Adopted verbatim. `NegotiationStatus` newly typed (was raw `status: str = "active"`). |
| `SessionStatus` | seller `session.py` | Adopted verbatim. |

## Primitive-by-primitive record

| Primitive | Buyer source | Seller source | Divergences found | Decision taken |
|---|---|---|---|---|
| **Organization** | `opendirect.Organization` (`type: str`, `address`, `contacts`, `ext`, camelCase) | `core.Organization` (`role: OrganizationRole`, `status: str`, `metadata`, squashed aliases) | Untyped `type` vs typed `role`; buyer had no status; different extension fields; `id` optional in buyer | Seller's typed `role` wins; buyer's `address`/`contacts` kept; new `OrganizationStatus` enum (active/suspended); `organization_id` required, registry-issued; `ext` per G6. |
| **Account** | `opendirect.Account` (`advertiser_id`, `buyer_id`, `name` max 36) | `core.Account` (`buyer_organization_id`, `seller_organization_id`, `status`) | Buyer modeled buyer↔advertiser; seller modeled buyer-org↔seller-org; no shared shape at all | Seller's org-pair shape wins (matches plan: Account = commercial relationship between the pair); buyer's `advertiser_id` and `name` kept as optional; `AccountStatus` from seller. |
| **Agent** | *(buyer shipped an incompatible agent-card schema in its clients — plan §5: "Card schemas incompatible across repos")* | `agent_registry.AgentCard` + `RegisteredAgent` | Two incompatible cards; seller card had `supported_deal_types: list[str]`, self-asserted tiers; `RegisteredAgent.registered_at` used naive `datetime.utcnow` | Single `Agent` = A2A card + registry identity. `agent_id` registry-issued; `trust_status` documented as registry-verified, never self-asserted. `audience_capabilities` kept as open `dict` (typed model is a later bead). Local-registry bookkeeping (`interaction_count`, `last_seen`, `registry_sources`, `notes`) stays seller-local. `supported_deal_types` documented as DealType wire values. |
| **Product** | `opendirect.Product` (publisherId, basePrice required, RateType, DeliveryType, flat `targeting` dict, camelCase) | `core.Product` (seller_organization_id, inventory_segments, IAB taxonomy targeting triple, CommercialTerms, no price) | Price required vs absent; flat targeting vs taxonomy triple; buyer `domain`/`ad_unit`; seller execution-level `inventory_segments` | Merged: seller's taxonomy triple + `CommercialTerms` win over the flat dict; `base_price` optional (public list signal only — private rates live on RateCard per FD-9) with `pricing_type`; buyer's `delivery_type`, `domain`, `available_impressions` kept; `ad_unit` generalized to `ad_formats`; `inventory_segments` dropped as seller-internal execution detail (maps via seller's own storage). `CommercialTerms.supported_deal_types` now `list[DealType]`. |
| **MediaKit** | — (buyer consumed it as untyped JSON) | `media_kit.py` (no MediaKit model — only Package + tier views) | Neither repo had an actual MediaKit object despite both exchanging one | New model: seller-issued catalog of `Package`s + contact/currency. Tier-gated views (`PublicPackageView`, `AuthenticatedPackageView`) are seller-derived presentations, not wire primitives. |
| **Package** | — | `media_kit.Package` | Seller-only; carried a legacy `audience_segment_ids` migration shim and typed `AudienceCapabilities`; naive `created_at` | Adopted minus the legacy-migration validator (seller-local concern) and minus ad-server curation internals (`ad_server_source`); `audience_capabilities` is an open `dict` until the audience bead; `rate_type` renamed `pricing_model`; `floor_price` dropped — a seller's floor is internal and must not cross the wire. |
| **RateCard** (FD-9) | — (buyer had no model) | — (seller's `pricing_tiers.py` is list/default pricing config, NOT a rate card) | Neither repo modeled the actual industry object: the pair's private negotiated rates | New model per ratified FD-9: pair identities (`buyer_organization_id`, `seller_organization_id`, optional `account_id`), `RateCardEntry[]` (product/package/format → agreed rate + `PricingModel`), `effective_from/to`, `RateCardStatus` (proposed/active/expired/terminated). NEVER public; `Quote.rate_card_id` / `Deal.rate_card_id` reference it — it is never embedded. Seller `PricingRule`/`PricingTier`/`TieredPricingConfig` stay seller-local. |
| **Quote** | `deals.QuoteResponse` (media_type + linear_tv, `final_cpm` optional, str timestamps) | `quotes.QuoteResponse` (typed `QuoteStatus`, `pricing_type`, `final_cpm` **required**, `inventory_type` required, hardcoded `seller_id` default, no linear TV) | Seller could not represent on_request pricing; seller had **no linear TV fields at all** (the silent-mispricing risk behind FD-6); availability defaults differed (buyer None vs seller 0.95/"moderate") | Merged: buyer's optional `base_cpm`/`final_cpm` win (supports `pricing_type=on_request`); `media_type` + `linear_tv: LinearTVQuoteDetails` on the shared schema per FD-6 so non-linear sellers reject structurally; seller's typed `QuoteStatus`/`pricing_type` win; `buyer_tier` typed as `AccessTier` (was str in both); availability optional with no fabricated defaults; timestamps typed per G2; + `rate_card_id` (FD-9) and `consent_context` (FD-10). |
| **Proposal** | — (buyer had no proposal model) | `core.Proposal` + `ProposalThread` + `ProposalRevision` + `ProposalLine` | Buyer missing entirely; seller carried RFC 6902 JSON Patch revision machinery with hashes | Shared `Proposal` = current negotiated state + `ProposalLine[]` (with `DeliveryGoal`, `PricingTerms`); `proposal_thread_id` kept as an optional threading id; the JSON Patch revision/hash machinery (`ProposalRevision`, `RevisionCreator`, `RevisionType`, `ChangeClassification`) stays seller-local — multi-turn offer history crosses the wire as `Negotiation` instead. Dates typed per G3. |
| **Negotiation** | — (buyer negotiated via untyped counter-offer calls — the 422 bug surface) | `negotiation.NegotiationHistory` + `NegotiationRound` | Buyer missing; seller history embedded **seller secrets**: `floor_price`, `base_price`, `strategy`, `limits` (concession caps); naive timestamps; raw `status: str` | Shared `Negotiation` = multi-turn offer history container: ids (`proposal_id`/`quote_id`/`product_id`/`package_id`), `buyer_tier`, `NegotiationRound[]`, typed `NegotiationStatus`. **`floor_price`, `base_price`, `strategy`, and `NegotiationLimits` are deliberately excluded** — they are the seller's internal guardrails (do-not-touch NegotiationEngine) and must never cross the wire. |
| **Deal** | `deals.DealResponse` (typed `OpenRTBParams`, audience snapshot fields, str status) | `quotes.DealBookingResponse` (typed `DealBookingStatus`, untyped `openrtb_params: dict`) | Status vocabularies differed (see DealStatus); buyer's `openrtb_params` typed vs seller's dict; `quote_id` optional (buyer) vs required (seller) | Merged: unified `DealStatus`; buyer's typed `OpenRTBParams` wins; `quote_id` optional (deals may arrive via the import-first deal-library path, amendment §7.4); + `rate_card_id` (FD-9), `media_type`/`linear_tv` carried from the booked quote (FD-6), `consent_context` (FD-10). `audience_plan_snapshot`/`audience_match_summary` deferred to the audience bead with the typed audience models. |
| **Order** | `opendirect.Order` (account_id, budget, datetime flights, 3-state uppercase status) | `core.ExecutionOrder` (proposal_id, external_ids, actions, 5-state status) | Different identity fields, different status sets, buyer camelCase | Merged: OpenDirect commercial fields (account, name, currency, budget, flight dates as `date`) + seller's `proposal_id`/`external_ids`; unified `OrderStatus`; `actions` dropped (seller-internal workflow); + `deal_id` link and `consent_context` (FD-10). Seller's record is authoritative for order state (FD-8). |
| **Line** | `opendirect.Line` | `core.Placement` (execution-level) | Buyer's Line is the OpenDirect booking unit; seller's Placement is an ad-server mapping with a different status set | Buyer's OpenDirect `Line` wins as the wire unit; `rate_type` → `pricing_model` (unified enum), `booking_status` → `status` (`LineStatus`, OpenDirect casing). Seller `Placement`/`PlacementStatus` stay seller-local; sellers map Line ↔ placement internally. |
| **ChangeRequest** | `linear_tv.MakegoodRequest` + `CancellationRequest` (dead endpoints — seller served no route) | `change_request.py` (full model) | Buyer's makegood/cancel were separate one-off DTOs hitting non-existent routes (interop finding F3); seller model had naive timestamps, `""` sentinel strings, internal `rollback_snapshot` | Seller's `ChangeRequest` wins as the one post-booking modification object; `ChangeType` gains `makegood` and the typed `MakegoodDetails` payload per FD-6 (makegood = typed ChangeRequest subtype, not a primitive); `order_id`/`deal_id` both optional with an at-least-one validator; `""` sentinels → `None`; `rollback_snapshot` dropped (seller-internal); severity-classification and validation *functions* stay seller-local (EP-1.4/EP-3 behavior). |
| **Session** | — | `session.py` | Seller-only; embedded `BuyerContext` (buyer-local type), internal flow threading (`linked_flow_ids`), funnel-state `NegotiationState`, naive timestamps | Shared `Session` = conversation container: `buyer_identity`, `SessionMessage[]`, `active_negotiation_ids`/`active_deal_ids` links. `BuyerContext`, `linked_flow_ids`, and the `NegotiationState` funnel internals stay seller-local; `SessionMessage.flow_id` dropped for the same reason. |
| **Creative** | `opendirect.Creative` (account_id, name, click_url, untyped `creative_asset`/`creative_approvals` dicts) | `core.Creative` (typed `CreativeManifest`, `AdProfile`, `ReviewStatus`, placeholder support) | Buyer untyped asset blob vs seller typed manifest; approval modeled as untyped list (buyer) vs `review_status` (seller); plan §4.4 flagged Creative as wrongly buyer-only | Shared wire object per plan §4.4: seller's typed `CreativeManifest` (metadata only, never executable markup) + `review_status` as the seller-side approval state win; buyer's `account_id`, `name`, `language`, `click_url` kept; `mimetype` → `mime_type` per G1. |
| **Assignment** | `opendirect.Assignment` (creative → **line**, untyped status) | `core.Assignment` (creative → **placement**, rotation, sov, required effective dates) | Bound to different execution units | Buyer's `line_id` binding wins (Line is the wire execution unit; placement is seller-internal); seller's `rotation_mode`/`sov` kept; effective dates optional typed `date`; status left free-form until EP-1.4. |
| **ConsentContext** (FD-10) | `sgp.py` diligence gate (buyer-local, not exchanged) | — (`consent` mentioned 44×, no type) | 67 buyer / 44 seller mentions of consent with **no exchanged type** (plan §4.3) | New minimal placeholder per ratified FD-10: `applicable_regimes`, `gpp_string`/`gpp_section_ids` (GPP), `tcf_string` (TCF), `DiligenceStatus` + `verified_at` (IAB Diligence Platform). Slots exist now on `Quote`, `Deal`, and `Order`; full build-out is a later bead. |

## Explicitly out of scope (stated, not accidental)

- **Settlement / Invoice** — out of scope for the reference implementation
  per ratified FD-6; recorded here so the absence is explicit.
- **Avails** — a request/response query, not a persisted primitive
  (plan §4.4); lands in `protocol/`.
- **Audience plan / audience capabilities typed models** — a dedicated
  bead; the fields exist as open objects (`Agent.audience_capabilities`,
  `Package.audience_capabilities`) so the slots are stable.
- **State machines / transition rules** — EP-1.4 (`state/`); this bead
  ships the canonical enums only.
- **Makegood as a standalone primitive** — deliberately NOT added; it is a
  typed `ChangeRequest` subtype per ratified FD-6.
