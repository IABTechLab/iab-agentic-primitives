# Registry client + local registry (EP-5.4: realigned to the REAL registry)

This library's `RegistryClient` speaks the **actual** IAB Tech Lab
agent-registry API (github.com/IABTechLab/agent-registry, a Node.js
service; production `https://registry.iabtechlab.com`). EP-5.3 had built the
client and a Python "sandbox" against *assumed* API shapes; EP-5.4 realigns
both to what the real registry does.

**Acronyms.** AAMP = Agentic Advertising Marketplace Protocol (this
project's umbrella name for the IAB agent discovery + trust flow). JWT =
JSON Web Token. OAuth = Open Authorization. IAB = Interactive Advertising
Bureau. A2A = Agent-to-Agent protocol. MCP = Model Context Protocol. SVG =
Scalable Vector Graphics. GPP = Global Privacy Platform.

## The realigned client (endpoints + auth)

`iab_agentic_primitives.registry_client.RegistryClient` — one async
(`httpx`) client, backend chosen by config. Requires the `client` extra.

**Base path** is `/api/agents` (the EP-5.3 client used `/agents`).

**Auth** is a JWT bearer token: `Authorization: Bearer <token>` (HS256,
issuer `IAB`). You supply an already-issued token
(`AAMP_REGISTRY_TOKEN` / `auth_token=`); the client does not log in for you.
The registry proxies login to IAB OAuth
(`POST /api/auth/login` → `api.iabtechlab.com/oauth/authmobile`, username +
password → token). When the registry returns a refreshed token in the
**`X-New-Token`** response header, the client absorbs it automatically and
uses it for subsequent requests (`client.token` reflects the current one).

Success bodies are enveloped `{"success": true, "data": ...}` (a list is
`{"data": {"agents": [...], "count": N, ...}}`); the client unwraps `data`.
Errors are `{"success": false, "error": "<message>"}`, mapped by HTTP status
onto the shared `ErrorCode` vocabulary and raised as structured
`RegistryError` (carrying an `ErrorDetail`).

| Client method | Real endpoint | Auth |
| --- | --- | --- |
| `register_agent(agent)` | `POST /api/agents` | authenticated; JWT company domain must match `primary_domain` |
| `list_agents(**filters)` | `GET /api/agents` | authenticated |
| `get_agent(id)` | `GET /api/agents/:id` | authenticated |
| `update_agent(id, **fields)` | `PUT /api/agents/:id` | **admin** |
| `delete_agent(id)` | `DELETE /api/agents/:id` | **admin** |
| `verify_health(id)` | `POST /api/agents/:id/verify-health` | **admin** |
| `validate_domain(domain)` | `POST /api/agents/validate/domain` | public |
| `validate_agent(domain, url)` | `POST /api/agents/validate/agent` | public |
| `verification_badge(id)` | `GET /api/agents/:id/verification-badge` | public (SVG) |
| `verified_trust(id)` | `get_agent` + `verification-badge` | authenticated |

`verified_trust(id)` is the **real trust check** (what the bead calls
"verify_trust"): it maps the registry's real signals —
`verification_status` (`active`/`pending`), `domain_verified`, `iab_member`,
and whether the public verification badge is served — into a single
`VerifiedTrust` verdict (`verified == verification_status == "active"`).

> **Deviation — method name `verify_trust`.** The bead asked for the real
> method to be named `verify_trust`. That name is occupied by the **legacy
> AAMP trust-tier** method the EP-7.1 interop harness still calls (see
> below), which this bead's scope does not permit editing. The real verdict
> is therefore exposed as **`verified_trust`**; `verify_trust` remains the
> legacy trust-tier call. When the harness migrates off the trust-tier
> model, `verified_trust` should be renamed to `verify_trust`.

The registry has **no self-asserted access-tier / trust-tier enum** — trust
is the `verification_status` + `domain_verified` + `iab_member` triplet.

```python
from iab_agentic_primitives.registry_client import RegistryClient, RegistryAgent

async with RegistryClient() as reg:                    # backend from env
    agent = await reg.register_agent(RegistryAgent(
        agent_name="Acme Seller",
        primary_domain="acme.example",                 # must match your JWT domain
        type="remote", protocol_type="a2a",
        endpoint_url="https://acme.example/a2a",
    ))
    trust = await reg.verified_trust(agent.id)
    if trust.verified:
        ...                                            # active in the registry
```

## How `LOCAL` runs the REAL registry (vs the old Python fake)

**Before (EP-5.3):** `LOCAL_SANDBOX` pointed at a Python FastAPI
*reimplementation* of an assumed registry — a divergent fake.

**Now (EP-5.4):** the `LOCAL` backend points at the **real Node
agent-registry** run locally via
`sandbox_registry/docker-compose.yml`, which builds the actual upstream repo
and binds it on `127.0.0.1:3001` (the `LOCAL` default URL):

```bash
docker compose -f src/iab_agentic_primitives/sandbox_registry/docker-compose.yml up --build
export AAMP_REGISTRY_BACKEND=LOCAL           # default; URL defaults to http://127.0.0.1:3001
export AAMP_REGISTRY_TOKEN=<JWT from the IAB Tools portal>
```

> **Upstream gap.** The agent-registry repo does not ship a DB schema
> migration for a fresh database (its `db:init` references an absent
> `database/schema-sqlite.sql`; there is no Postgres `CREATE TABLE`). The
> compose file wires a Postgres service and an `./initdb` mount for the
> `agents` + `verification_logs` schema, which must be supplied out of band
> (or point `DB_*` at an already-provisioned Postgres). This is why CI does
> not hard-depend on the dockerized registry.

**In-process test double (offline / no Docker).** For fast, deterministic
unit tests, `sandbox_registry.create_registry_double()` is a **minimal test
double faithful to the `/api/agents` shape** — enveloped responses, bearer
auth (401), admin-gated `PUT`/`DELETE` (403), domain-scoped registration
(403 on mismatch), the `X-New-Token` refresh, the public validate endpoints,
and the SVG badge (members only, else 404). It is explicitly a **test
double, not "the sandbox"**; the real registry is the LOCAL target.

## Agent-card model alignment

The real registry's agent record diverges sharply from this library's Agent
Card (`primitives.Agent`, exported as `protocol.AgentCard`). Rather than
break that card (heavily consumed across the lib), EP-5.4 adds a new,
additive `RegistryAgent` wire model (in `registry_client`) that mirrors the
real record; the Agent Card is unchanged. Key divergences:

| Concept | This lib's `AgentCard` (`Agent`) | Real registry (`RegistryAgent`) |
| --- | --- | --- |
| Identifier | `agent_id` (string, registry-issued) | `id` (integer, auto-increment) |
| Name | `name` | `agent_name` |
| Endpoint | `url` (A2A) | `endpoint_url` (remote/private) or `repository_url` (local) |
| Domain | — (via `provider`) | `primary_domain` (required; JWT-scoped) |
| Kind | `agent_type` (buyer/seller/…) | `type` (local/remote/private) + `protocol_type` (a2a/mcp) |
| Capabilities | typed `AgentCapabilities` + `skills[]` | flat `capabilities[]` (+ `iab_capabilities[]`, taxonomy) |
| Provider | `provider` (`AgentProvider`) | `legal_name` / `contact_email` / `contact_website` |
| Trust | `trust_status` enum + access-tier ceiling | `verification_status` + `domain_verified` + `iab_member` |
| Privacy | — | `gpp_id` / `gpp_verified` |

`RegistryAgent` requires only `agent_name` + `primary_domain`; every other
field is optional and unknown fields are ignored (the `WireModel`
forward-compat rule), so partial records round-trip.

## Backend config (the swap is config, not code)

| Variable | Values | Notes |
| --- | --- | --- |
| `AAMP_REGISTRY_BACKEND` | `LOCAL` (default), `IAB_SANDBOX`, `IAB_PROD` | `LOCAL_SANDBOX` still accepted as an alias for `LOCAL` |
| `AAMP_REGISTRY_URL` | base URL | Defaults: `http://127.0.0.1:3001` (LOCAL), `https://registry.iabtechlab.com` (IAB_PROD); required for `IAB_SANDBOX` |
| `AAMP_REGISTRY_TOKEN` | bearer JWT | `Authorization: Bearer <token>`; legacy name `AAMP_REGISTRY_AUTH_TOKEN` still honored |

```bash
# local dev against the real registry (docker)
export AAMP_REGISTRY_BACKEND=LOCAL

# when the IAB sandbox is provisioned — no code change
export AAMP_REGISTRY_BACKEND=IAB_SANDBOX
export AAMP_REGISTRY_URL=https://<iab-sandbox-url>
export AAMP_REGISTRY_TOKEN=<issued-jwt>
```

## Tests

`tests/test_registry_client.py` runs the **client's real method set**
parameterized over backends:

- `LOCAL` — drives `create_registry_double()` in-process via
  `httpx.ASGITransport` (register → list → get → admin update/delete →
  `verified_trust` → badge → validate), plus deterministic checks for
  missing-auth (401), domain-mismatch (403), admin-gating (403), and the
  `X-New-Token` refresh.
- `IAB_SANDBOX` / `IAB_PROD` — **skipped with reason** "URL+token not
  provisioned — pending Mayank"; set `AAMP_REGISTRY_URL` +
  `AAMP_REGISTRY_TOKEN` to run the identical suite against them.

Config resolution (backend/URL/token defaults, aliases, unknown-backend
rejection) is covered without a server.

## Legacy AAMP trust-tier surface (retained for the EP-7.1 harness)

The EP-7.1 in-process interop harness (`harness/scenario.py`, out of this
bead's editable scope) models an AAMP **trust-tier** registry that the real
registry does not implement: register an `AgentCard`, `set_trust(status)`,
`discover(agent_type)`, and read an `AccessTier` ceiling from
`verify_trust(id)`. To keep the full suite green without editing the
harness, that surface is **retained as-is**:

- `RegistryClient.register / discover / fetch_card / verify_trust /
  set_trust` (the `/agents` trust-tier paths), returning `TrustVerification`
  (status + `max_access_tier`, capped by `TRUST_TIER_CEILING`).
- `sandbox_registry.create_app` + `AgentStore` — the legacy trust-tier app
  (now clearly labeled legacy in its docstrings), still exercised by
  `tests/test_sandbox_registry.py` and the harness.

This is a compatibility layer, not part of the realigned real API; it should
be removed once the harness migrates to the real trust signals.
