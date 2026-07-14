# State Reconciliation (EP-1.4)

This document is the audit trail for the canonical lifecycle state machines in
`src/iab_agentic_primitives/state/` of this shared contract library for IAB
(Interactive Advertising Bureau) agentic advertising. It maps every transition
defined in the two agent repos onto the ONE canonical machine per lifecycle,
and records the design rulings (the `syncing` decision, the FD-8 — flagged
decision 8 — authority rule, and the rework-edge precedence rule).

Status **vocabularies** (the enums) were reconciled earlier in
`RECONCILIATION.md`; this document reconciles the **transition tables**.

Sources reconciled:

- **Buyer**: `buyer-agent/src/ad_buyer/models/state_machine.py` —
  `DealStateMachine` over `BuyerDealStatus`, 27 transition rules
  (`_build_deal_rules`).
- **Seller**: `seller-agent/src/ad_seller/models/order_state_machine.py` —
  `OrderStateMachine` over its unified `OrderStatus`, 21 transition rules
  (`_DEFAULT_TRANSITIONS`), plus the lossy `ExecutionStatus`/
  `ExecutionOrderStatus` mapping helpers.
- **Both**: `docs/state-machines/change-request-flow.md` (identical 7-status
  flow, documented in both repos, enforced only by ad-hoc if-statements in the
  seller's API — Application Programming Interface — handlers).

Result: **deal 27, order 19, change request 8 canonical transitions**, all
driven by one generic engine (`state/machine.py`) and exported as
language-neutral JSON (JavaScript Object Notation) under
`spec/jsonschema/state/`.

Neither repo's machine governed anything in production (the seller's was wired
only to standalone endpoints no flow calls; the buyer's silently skipped
validation for the status strings its main flow passes). The canonical
machines are therefore free to fix modeling errors — every deviation from a
source table is listed below.

## The FD-8 authority rule

Both agents track lifecycle state **independently**; there is no distributed
transaction. When their records disagree:

- The **seller's record is authoritative** for order/fulfillment state — and,
  because deals and change requests are seller-issued and seller-operated
  lifecycles, for the deal and change-request lifecycles too.
- The **buyer's record is authoritative** for its own campaign/budget state.
  Those machines (`BuyerCampaignStatus`, `CampaignStatus`,
  `CampaignAutomationStateMachine` — 14 + 18 more transition rules in the
  buyer repo) are buyer-internal, never cross the wire, and are deliberately
  **not** canonicalized here.

Reconciliation (`state/reconciliation.py`) does not force agreement — it makes
disagreement visible. It is a pure comparison helper (no input/output) that
agents call from their sync jobs; it classifies each (buyer-side, seller-side)
status pair as `consistent`, `buyer_behind`, `seller_behind`, or `divergent`,
and a divergence yields a structured `DisagreementReport` carrying both
states, both observation timestamps, and the authoritative side. The full
classification matrices are exported to
`spec/jsonschema/state/Reconciliation.json`.

### Precedence and rework edges

"Behind" means: the other side's state is reachable from yours via
**forward-progress** transitions. Transitions that return an entity to an
earlier state — counter-offer re-propose, reset-to-draft recovery,
resume-after-makegood — are marked `rework: true` in the canonical tables.
They are fully legal moves, but they are excluded from the precedence
computation: with the recovery loops included, almost every pair of order
states is mutually reachable and every ordinary lag would misclassify as
divergent. The forward-progress subgraph is required to be acyclic (validated
at machine construction), so precedence is a strict partial order.
Trade-off: if one side has genuinely looped back (seller reset a failed order
to `draft` while the buyer still shows `failed`), the looped-back side
classifies as behind rather than divergent — acceptable, since either way the
sync job re-fetches from the authoritative side.

## The `syncing` decision

The seller's internal `syncing` state (in-flight ad-server synchronization
between `in_progress` and `booked`) is **not a wire state**. `RECONCILIATION.md`
already dropped it from the canonical `OrderStatus` vocabulary; this bead
decides how the machine represents it:

- The seller's `in_progress -> syncing -> booked` chain collapses to
  `in_progress -> booked`, and `syncing -> failed` folds into the existing
  `in_progress -> failed`.
- In-flight sync is represented as an **audit note**, not a state: when sync
  begins, the seller writes an `AuditEntry`-shaped record on the `in_progress`
  state with `reason=AD_SERVER_SYNC_STARTED` (a shared constant,
  `"syncing to ad server"`, exported from `state/order_lifecycle.py` so both
  agents' audit trails stay grep-able for one string). The eventual
  `in_progress -> booked` or `in_progress -> failed` transition closes it out.
- A seller **may** additionally keep a local boolean substate flag; neither
  the flag nor the audit note ever crosses the wire.

Rationale: in-flight sync is seller-internal mechanics — there is no action a
buyer can take on it, and exposing it forces every other implementation to
model a state it can never enter.

## Deal lifecycle

Canonical machine: `state/deal_lifecycle.py` — 12 states (`DealStatus`),
27 transitions, initial `proposed`, terminal `completed`, `rejected`,
`failed`, `cancelled`, `expired`.

Source: the buyer's 27 `_build_deal_rules` transitions over
`BuyerDealStatus`. The seller had no formal deal machine (its
`DealBookingStatus` enum was assigned ad hoc), so the buyer table is the
baseline. State renames applied throughout (from `RECONCILIATION.md`):
`quoted -> proposed` (the deal entry state is the seller's proposed deal;
"quoted" belongs to the Quote lifecycle), `delivering -> active`,
`partially_canceled -> partially_cancelled`, and `booking` (in-flight
booking) dropped as a wire state.

| # | Buyer transition | Canonical | Disposition |
|---|---|---|---|
| 1 | `quoted -> negotiating` | `proposed -> negotiating` | Renamed (quoted -> proposed) |
| 2 | `quoted -> accepted` | `proposed -> accepted` | Renamed |
| 3 | `negotiating -> accepted` | `negotiating -> accepted` | Kept |
| 4 | `negotiating -> quoted` | `negotiating -> proposed` | Renamed; marked `rework` (counter-offer re-propose) |
| 5 | `accepted -> booking` | `accepted -> booked` | Collapsed with #6: `booking` is not a wire state; in-flight booking is the request/response gap, not a lifecycle fact |
| 6 | `booking -> booked` | — | Collapsed into #5 |
| 7 | `booked -> delivering` | `booked -> active` | Renamed (delivering -> active) |
| 8 | `delivering -> completed` | `active -> completed` | Renamed |
| 9 | `quoted -> failed` | `proposed -> failed` | Renamed |
| 10 | `negotiating -> failed` | `negotiating -> failed` | Kept |
| 11 | `booking -> failed` | `accepted -> failed` | Re-anchored: with `booking` gone, a booking failure happens while `accepted` |
| 12 | `delivering -> failed` | `active -> failed` | Renamed |
| 13 | `quoted -> cancelled` | `proposed -> cancelled` | Renamed |
| 14 | `negotiating -> cancelled` | `negotiating -> cancelled` | Kept |
| 15 | `accepted -> cancelled` | `accepted -> cancelled` | Kept |
| 16 | `booking -> cancelled` | — | Dropped as duplicate: re-anchoring onto `accepted` makes it identical to #15 |
| 17 | `booked -> cancelled` | `booked -> cancelled` | Kept |
| 18 | `delivering -> cancelled` | `active -> cancelled` | Renamed |
| 19 | `quoted -> expired` | `proposed -> expired` | Renamed |
| 20 | `negotiating -> expired` | `negotiating -> expired` | Kept |
| 21 | `delivering -> makegood_pending` | `active -> makegood_pending` | Renamed |
| 22 | `makegood_pending -> delivering` | `makegood_pending -> active` | Renamed; marked `rework` (resume delivery) |
| 23 | `makegood_pending -> completed` | `makegood_pending -> completed` | Kept |
| 24 | `makegood_pending -> failed` | `makegood_pending -> failed` | Kept |
| 25 | `booked -> partially_canceled` | `booked -> partially_cancelled` | Renamed (spelling) |
| 26 | `partially_canceled -> delivering` | `partially_cancelled -> active` | Renamed |
| 27 | `partially_canceled -> cancelled` | `partially_cancelled -> cancelled` | Kept |
| — | (none) | `proposed -> rejected` | **Added**: canonical `DealStatus` includes `rejected` (both repos' deal response models used it) but the buyer machine had no path into it |
| — | (none) | `negotiating -> rejected` | **Added**: negotiation walk-away |

Net: 27 buyer rules -> 25 canonical (two collapses around `booking`),
plus 2 added rejection paths = **27 canonical transitions**.

## Order lifecycle

Canonical machine: `state/order_lifecycle.py` — 11 states (`OrderStatus`),
19 transitions, initial `draft`, terminal `completed` and `cancelled` **only**
(`rejected`, `failed`, `unbooked` are recoverable via rework edges to `draft`,
so they are deliberately not terminal).

Source: the seller's 21 `_DEFAULT_TRANSITIONS`. The buyer's OpenDirect (the
IAB direct-buying API standard) order statuses carried no transition table and
were already folded into this vocabulary by `RECONCILIATION.md`
(`PENDING -> pending_approval`, uppercase `APPROVED`/`REJECTED` -> lowercase).

| # | Seller transition | Canonical | Disposition |
|---|---|---|---|
| 1 | `draft -> submitted` | `draft -> submitted` | Kept |
| 2 | `submitted -> pending_approval` | `submitted -> pending_approval` | Kept |
| 3 | `submitted -> approved` | `submitted -> approved` | Kept (auto-approval, no gate) |
| 4 | `pending_approval -> approved` | `pending_approval -> approved` | Kept |
| 5 | `pending_approval -> rejected` | `pending_approval -> rejected` | Kept |
| 6 | `approved -> in_progress` | `approved -> in_progress` | Kept |
| 7 | `in_progress -> syncing` | `in_progress -> booked` | Collapsed with #8 per the `syncing` decision above |
| 8 | `syncing -> booked` | — | Collapsed into #7 |
| 9 | `booked -> completed` | `booked -> completed` | Kept |
| 10 | `booked -> unbooked` | `booked -> unbooked` | Kept |
| 11 | `draft -> cancelled` | `draft -> cancelled` | Kept |
| 12 | `submitted -> cancelled` | `submitted -> cancelled` | Kept |
| 13 | `submitted -> failed` | `submitted -> failed` | Kept |
| 14 | `pending_approval -> cancelled` | `pending_approval -> cancelled` | Kept |
| 15 | `approved -> cancelled` | `approved -> cancelled` | Kept |
| 16 | `in_progress -> failed` | `in_progress -> failed` | Kept; description widened to cover ad-server sync failure |
| 17 | `in_progress -> cancelled` | `in_progress -> cancelled` | Kept |
| 18 | `syncing -> failed` | — | Dropped as duplicate: folds into #16 once `syncing` collapses into `in_progress` |
| 19 | `rejected -> draft` | `rejected -> draft` | Kept; marked `rework` |
| 20 | `failed -> draft` | `failed -> draft` | Kept; marked `rework` |
| 21 | `unbooked -> draft` | `unbooked -> draft` | Kept; marked `rework` |

Net: 21 seller rules -> **19 canonical transitions** (one collapse, one
duplicate absorbed). Nothing added.

The seller's lossy legacy mapping helpers (`from_execution_status`,
`from_execution_order_status` — which silently defaulted unknown strings to
`draft`) are NOT carried into the shared library: the canonical vocabulary is
the only wire vocabulary, and unknown values must be rejected, not coerced.

## Change-request lifecycle

Canonical machine: `state/change_request_lifecycle.py` — 7 states
(`ChangeRequestStatus`), 8 transitions, initial `pending`, terminal `applied`,
`rejected`, `failed`.

Source: the identical flow documented in both repos'
`docs/state-machines/change-request-flow.md`, previously enforced only by
ad-hoc if-statements in the seller's API handlers.

| # | Documented flow | Canonical | Disposition |
|---|---|---|---|
| 1 | `pending -> validating` | `pending -> validating` | Kept |
| 2 | `validating -> failed` | `validating -> failed` | Kept (validation errors) |
| 3 | `validating -> approved` | `validating -> approved` | Kept (minor severity auto-approval) |
| 4 | `validating -> pending_approval` | `validating -> pending_approval` | Kept (material/critical severity) |
| 5 | `pending_approval -> approved` | `pending_approval -> approved` | Kept |
| 6 | `pending_approval -> rejected` | `pending_approval -> rejected` | Kept |
| 7 | `approved -> applied` | `approved -> applied` | Kept |
| — | (none) | `approved -> failed` | **Added**: applying an approved change can fail; the ad-hoc code had no legal state for that outcome |

Severity routing (which of #3/#4 the seller picks out of `validating`) is
seller policy, not machine structure — the machine defines what moves are
legal, not which one to choose. Makegoods (flagged decision FD-6) are a typed
`ChangeRequest` subtype (`change_type == "makegood"`) and follow this same
machine with no extra states. The change-request lifecycle is seller-owned
(the seller mints the identifier and runs validation, approval, and
application), so the seller is authoritative per FD-8; the buyer submits and
polls.

## Engine notes

`state/machine.py` is one small generic engine used by all three lifecycles:

- explicit transition table `{(from, to): TransitionRule}` with optional pure
  guard predicates, validated at construction (unknown states, duplicates,
  outgoing edges from terminal states, unreachable states, and unflagged
  forward-progress cycles are construction-time errors);
- `transition(current, to) -> to` is pure, with **no storage coupling** —
  persistence and audit-log storage are the agents' concern (both source
  machines carried mutable in-memory state plus an audit log that production
  flows bypassed; the canonical engine cannot be bypassed that way because it
  holds nothing);
- `InvalidTransitionError` carries `{machine, current, attempted, allowed[]}`
  and renders via `as_dict()` — the seller's HTTP (Hypertext Transfer
  Protocol) 409-with-allowed-transitions response pattern, made portable;
- `AuditEntry` standardizes the audit-record shape both agents persist:
  `(machine, from_state, to_state, actor, occurred_at, reason)` with
  `occurred_at` required to be timezone-aware and normalized to UTC
  (Coordinated Universal Time).

The transition tables and reconciliation matrices are exported to
`spec/jsonschema/state/` by `uv run python -m
iab_agentic_primitives.state.spec_export`; `tests/test_state_spec_drift.py`
fails if the checked-in JSON ever diverges from the machines.
