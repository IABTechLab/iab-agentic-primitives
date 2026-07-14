# HARNESS.md — the in-process two-agent interop harness (EP-7.1)

## What it is

`iab_agentic_primitives.harness` is a **shared development tool** that runs
**both** sides of a buyer/seller transaction against the **one** shared
contract — the protocol envelopes (`protocol/`), the primitives
(`primitives/`), the canonical state machines (`state/`), and the real sandbox
AAMP registry (`sandbox_registry/`) — in a single process, with **no network
sockets**.

It exists because today the buyer repo and the seller repo each test their
agent against a **mock** of the counterparty. A mock always agrees with you,
so the cross-repo interop breaks stayed invisible until integration:

| Historical break | How the harness makes it loud |
| --- | --- |
| Negotiation **422 every round** (buyer sent `{price}`, seller wanted `buyer_price` + `action`) | Both sides speak the one `NegotiationMessage`; `assert_negotiation_terminates` fails on any stuck `active` |
| Dropped `agent_url` / `linear_tv` / `audience_plan` | `assert_no_field_dropped` checks these survive request → response |
| **405** on `POST /products/search` | The catalog is `list_products` only; no search route to 405 |
| Makegood **404s** | Change requests are one typed surface (contract-level) |
| Incompatible **agent cards** | Registration + discovery run against the real registry app |
| **Self-asserted** pricing tier | `assert_auth_tier_enforced`: the registry ceiling caps the claimed tier |

If the two sides disagree about a money path, a scenario turns red instead of
a production incident turning red.

**Acronyms.** AAMP = the IAB (Interactive Advertising Bureau) Tech Lab agent
discovery and trust registry. A2A = agent-to-agent protocol. CPM = cost per
mille (per 1000 impressions). PG/PD/PA = Programmatic Guaranteed / Preferred
Deal / Private Auction. FD = flagged decision (remediation plan §7.2). ASGI =
Asynchronous Server Gateway Interface. TV = television.

## The pieces

### `roles.py` — the interfaces

- **`SellerRole`** (passive service): `agent_card`, `list_products`,
  `price_quote`, `handle_negotiation`, `book_deal`. Every money-path method
  takes/returns a canonical envelope and returns **either** the success
  envelope **or** the one structured `ErrorEnvelope` — never a bare exception,
  never a silently mispriced result. Two harness-introspection hooks
  (`state_transitions`, `deal_status`) let the runner read the state-machine
  timeline and reconcile; real agents override them from their audit log.
- **`BuyerRole`** (active driver): `agent_card` and
  `transact(brief, sellers)`. Given a `CampaignBrief` and the discovered
  `SellerChannel`s, it requests quotes, optionally negotiates, enforces its
  **hard budget ceiling**, and books.
- **`SellerChannel`**: how a buyer reaches **one** seller over the protocol.
  In-process it wraps a `SellerRole` directly; in production it is an HTTP
  client. It carries the buyer's **registry-verified tier ceiling** and
  injects it into every seller call, so the seller caps the buyer's
  self-asserted tier server-side (trust is never self-asserted).
- **`CampaignBrief`**: the buyer's objective and guardrails (deal type,
  impressions, `max_cpm` ceiling, `budget` ceiling, self-asserted
  `buyer_identity`, negotiation knobs). Money fields are `Money` (exact
  micros) so ceiling checks never touch float.

### `reference_agents.py` — the known-good agents

Deterministic, **no LLM**, no wall-clock/random branching on the money path:

- **`ReferenceSeller`** — fixed catalog of `Product`s, a pricing engine that
  applies a per-tier discount but **never prices below the product's private
  floor**, a bounded concession rule for negotiation that is **guaranteed to
  terminate** (`MAX_SELLER_ROUNDS`), idempotent booking (same
  `idempotency_key` → one deal), a **seller-minted `deal_id`**, and it drives
  the canonical **Deal** and **Order** state machines. Linear TV is rejected
  **structurally** (`unsupported_capability` naming `linear_tv`), never
  mispriced.
- **`ReferenceBuyer`** — picks the **cheapest quote under `max_cpm`**, can send
  a counter and accept within budget, and enforces a **hard budget ceiling**
  (walks away cleanly, no exception, rather than overspend).

These are the fixed point real agents are swapped against: a real seller is
correct if it interoperates with `ReferenceBuyer` exactly as `ReferenceSeller`
does, and vice versa.

### `scenario.py` — the runner

`run_scenario(buyers, sellers)` (async) / `run_scenario_sync(...)`:

1. Stands up the sandbox registry app over an in-memory store and an
   in-process `RegistryClient` (httpx **ASGITransport**, no socket).
2. Registers every buyer/seller `AgentCard`, then sets each participant's
   trust status (which caps the buyer's grantable tier and hides blocked
   sellers from discovery).
3. Per buyer: **discovers** trusted sellers, **verifies** the buyer's own tier
   ceiling, builds recording `SellerChannel`s, and runs `transact`.
4. **Reconciles** the buyer-side and seller-side deal status (FD-8) and returns
   a `TransactionResult` per buyer: the full protocol **timeline**
   (`Exchange` records), the final `Deal`, the **state-machine transitions**
   the seller drove, and any `DisagreementReport`.

Registration/discovery/trust is infrastructure the **runner** owns (it holds
the ASGITransport); agent business logic lives entirely in the roles. In-process
calls are synchronous, so transactions run independently — isolation is
structural (deals and negotiations are keyed by seller-minted id and by
idempotency key), which is exactly what scenario (f) verifies.

### `assertions.py` — reusable interop checks

Drop-in pytest assertions over a `TransactionResult`, callable by both agent
repos' CI:

- `assert_quote_roundtrips` — a quote request produced a coherent quote.
- `assert_no_field_dropped` — `agent_url` / `linear_tv` / `audience_plan`
  survive (or, for an unsupported capability, are rejected structurally).
- `assert_negotiation_terminates` — no infinite `active`.
- `assert_booking_has_seller_deal_id` — booked deal carries a seller-minted id.
- `assert_auth_tier_enforced` — the granted tier equals the registry grant,
  never a self-asserted higher one.
- `assert_idempotent_booking` — same key → one deal.
- `assert_state_consistent` — buyer vs seller reconciliation == `consistent`.
- `assert_structural_rejection` — a surface produced a typed `ErrorEnvelope`
  with the expected code (FD-6).

## How the two agent repos consume it (EP-2.x / EP-3.x)

Once a repo imports the shared contract, it implements the role with its real
agent and runs the **same golden scenarios**:

```python
from iab_agentic_primitives.harness import (
    BuyerRole, SellerParticipant, BuyerParticipant, CampaignBrief,
    ReferenceSeller, run_scenario_sync,
    assert_quote_roundtrips, assert_booking_has_seller_deal_id,
    assert_state_consistent,
)

class RealBuyer(BuyerRole):
    def agent_card(self): ...          # your real card
    def transact(self, brief, sellers):
        # drive your real planning/quoting/negotiation/booking logic
        ...

def test_real_buyer_against_reference_seller():
    brief = CampaignBrief(...)
    result = run_scenario_sync(
        buyers=[BuyerParticipant(RealBuyer(), brief)],
        sellers=[SellerParticipant(ReferenceSeller())],   # known-good counterparty
    )
    tx = result.transactions[0]
    assert_quote_roundtrips(tx)
    assert_booking_has_seller_deal_id(tx)
    assert_state_consistent(tx)
```

The seller repo does the mirror: implement `SellerRole` with the real seller
and run it against `ReferenceBuyer`. When **both** repos are refactored onto
the contract, the harness can run the two **real** agents against each other —
the same scenarios, no mocks.

## How the rig (EP-11) consumes it

The rig imports the same `run_scenario` runner and the reference agents as its
baseline, then swaps in real agents (and, later, LLM-backed ones) as
participants. Because the runner returns a structured `TransactionResult`
(timeline + deal + transitions + reconciliation), the rig can score and diff
runs without re-implementing any protocol plumbing.

## Version-skew smoke stub (EP-7.2 entry point)

`tests/test_harness.py::test_version_skew_smoke` is parametrized over
`_VERSION_SKEW_MATRIX`. Today both reference agents are built from the **same**
installed contract, so the only working cell is `(N, N)`, which the stub
asserts. The real **buyer@N vs seller@N±1** matrix is EP-7.2: add
`(buyer_version, seller_version)` rows and pin each side to a different
installed contract wheel. The `TODO(EP-7.2)` in that list is the documented
extension point.

## Install / run

The role interfaces, reference agents, and assertions import only `pydantic`.
The scenario **runner** drives the registry in-process and needs httpx +
fastapi — the `harness` optional extra (already covered by the repo's `dev`
dependency group):

```bash
uv run pytest tests/test_harness.py     # golden scenarios
uv run pytest                            # full suite
uv run ruff check .
```
