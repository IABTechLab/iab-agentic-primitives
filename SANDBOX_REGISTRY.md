# Sandbox AAMP Registry (EP-5.3, owner decision FD-3)

**This is a development stand-in for the IAB AAMP registry.** AAMP (Agentic
Advertising Marketplace Protocol — the IAB Tech Lab agent discovery and trust
registry protocol, as used throughout this project's plan) is the registry the
buyer and seller agents use to find counterparties and verify their trust
status. The real IAB sandbox registry is not yet available, so per owner
decision FD-3 this library ships its own representative registry — faithful
enough to develop against — plus one shared client whose backend is **pure
configuration**, so moving to the real IAB registry later is a config change,
never a code change.

Two pieces:

- `iab_agentic_primitives.sandbox_registry` — a small FastAPI app + in-memory
  store implementing the registry surface (requires the `sandbox` extra)
- `iab_agentic_primitives.registry_client.RegistryClient` — the ONE client
  class both agents adopt (requires the `client` extra: httpx)

## Running the sandbox

```bash
uv sync --extra sandbox   # or: pip install 'iab-agentic-primitives[sandbox]'
uvicorn --factory iab_agentic_primitives.sandbox_registry.app:create_app --port 8020
```

Environment:

| Variable | Effect |
| --- | --- |
| `AAMP_SANDBOX_SEED_PATH` | Seed JSON loaded at startup so a demo env comes up populated |
| `AAMP_SANDBOX_PERSIST_PATH` | JSON file the in-memory store rewrites after every mutation and reloads at startup (no database) |

The persistence file uses the same shape as the seed file, so a persisted
store can be reused directly as seed data.

### Endpoints

All bodies are typed against the shared `protocol`/`primitives` models; all
errors use the canonical `ErrorEnvelope` (`{"detail": {"error": ..., "message": ...}}`).

| Method & path | Purpose |
| --- | --- |
| `POST /agents` | Register an `AgentCard` (201; 409 `contention` on duplicate id). The registry never trusts a self-asserted `trust_status` — fresh registrations always come back `registered`. |
| `GET /agents?agent_type=seller\|buyer` | Discovery listing (blocked agents are excluded) |
| `GET /agents/{agent_id}` | Card fetch (404 `not_found`) |
| `GET /agents/{agent_id}/trust` | `AgentTrustVerification` + `max_access_tier` (the access-tier ceiling the trust status allows) |
| `PUT /agents/{agent_id}/trust` | **Admin convenience for testing**: set trust status and optionally an explicit tier ceiling |
| `GET /healthz` | Liveness + registry id |

Trust status caps the access tier an agent can be granted (never
self-asserted): `unknown`/`blocked` → `public`, `registered` → `seat`,
`approved` → `agency`, `preferred` → `advertiser`
(`registry_client.TRUST_TIER_CEILING`). `PUT /agents/{id}/trust` may pin a
lower ceiling explicitly.

### Seed format

```json
{
  "agents": [
    {
      "card": {
        "agent_id": "demo-seller-1",
        "name": "Demo Seller",
        "description": "Demo seller agent",
        "url": "https://seller.demo.example/a2a",
        "agent_type": "seller",
        "provider": {"name": "DemoPub"},
        "supported_deal_types": ["PG", "PD"]
      },
      "trust_status": "approved",
      "max_access_tier": "agency"
    }
  ]
}
```

`trust_status` and `max_access_tier` are optional per entry: an agent present
in a seed is at least `registered`, and the tier ceiling defaults to the
canonical one for its status.

## The config-swap story

Agents construct `RegistryClient()` with no arguments; everything resolves
from config/env:

| Variable | Values | Notes |
| --- | --- | --- |
| `AAMP_REGISTRY_BACKEND` | `LOCAL_SANDBOX` (default), `IAB_SANDBOX`, `IAB_PROD` | Which registry deployment to talk to |
| `AAMP_REGISTRY_URL` | base URL | Required for `IAB_*`; defaults to `http://127.0.0.1:8020` for `LOCAL_SANDBOX` |
| `AAMP_REGISTRY_AUTH_TOKEN` | bearer token | Sent as `Authorization: Bearer <token>` — the auth slot for when IAB access lands; the local sandbox ignores it |

All three backends speak the **same HTTP protocol** (the sandbox implements
it; the `IAB_*` backends just point at different base URLs plus the auth
header), so the swap is literally:

```bash
# today
export AAMP_REGISTRY_BACKEND=LOCAL_SANDBOX

# when IAB sandbox access lands — no code change
export AAMP_REGISTRY_BACKEND=IAB_SANDBOX
export AAMP_REGISTRY_URL=https://<iab-sandbox-url>
export AAMP_REGISTRY_AUTH_TOKEN=<issued-token>
```

Client methods (async, httpx-based, structured `RegistryError`s carrying the
canonical `ErrorDetail`): `register(card)`, `discover(agent_type)`,
`fetch_card(agent_id)`, `verify_trust(agent_id) -> TrustVerification`, plus
the admin convenience `set_trust(...)` for sandbox testing.

```python
from iab_agentic_primitives.registry_client import RegistryClient

async with RegistryClient() as registry:          # backend from env
    sellers = await registry.discover("seller")
    trust = await registry.verify_trust(sellers[0].agent_id)
    if trust.trust_status != "blocked":
        tier_ceiling = trust.max_access_tier      # cap pricing tier here
```

## Tests as the acceptance criterion

`tests/test_registry_client.py` runs the **same suite parameterized over
backends**: `LOCAL_SANDBOX` drives the sandbox app in-process via
`httpx.ASGITransport` (register → discover → fetch → verify trust → admin
tier change reflected); `IAB_SANDBOX` is skipped-with-reason until the real
URL exists — set `AAMP_REGISTRY_URL` and the identical suite runs against it.
That parameterization is the proof that swapping registries is config, not
code.
