"""RegistryClient exercised against a faithful double of the REAL registry.

The real target of the ``LOCAL`` backend is the Node agent-registry run via
docker-compose (SANDBOX_REGISTRY.md). Where Docker is unavailable, these
tests drive the in-process real-API TEST DOUBLE
(:func:`create_registry_double`) over ``httpx.ASGITransport`` — same client,
same ``/api/agents`` wire shapes. The ``IAB_SANDBOX`` / ``IAB_PROD``
backends are skipped-with-reason until they are provisioned (URL + token —
pending Mayank); set ``AAMP_REGISTRY_URL`` + ``AAMP_REGISTRY_TOKEN`` to run
the identical suite against them.

Acronyms: JWT = JSON Web Token; SVG = Scalable Vector Graphics.
"""

import os
import uuid

import httpx
import pytest

from iab_agentic_primitives.primitives import (
    AccessTier,
    AgentProvider,
    AgentType,
    TrustStatus,
)
from iab_agentic_primitives.protocol import AgentCard, AgentTrustVerification
from iab_agentic_primitives.protocol.errors import ErrorCode
from iab_agentic_primitives.registry_client import (
    IAB_PROD_DEFAULT_URL,
    LOCAL_DEFAULT_URL,
    RegistryAgent,
    RegistryBackend,
    RegistryClient,
    RegistryError,
    TrustVerification,
    VerifiedTrust,
)
from iab_agentic_primitives.sandbox_registry import AgentStore, create_app, create_registry_double

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def make_registry_agent(name: str, domain: str = "example.com") -> RegistryAgent:
    return RegistryAgent(
        agent_name=name,
        primary_domain=domain,
        type="remote",
        protocol_type="a2a",
        endpoint_url=f"https://{domain}/a2a/{name}",
        description="Test agent",
    )


# -- REAL /api/agents surface, parameterized over backends ------------------


@pytest.fixture(
    params=[RegistryBackend.LOCAL, RegistryBackend.IAB_SANDBOX, RegistryBackend.IAB_PROD],
    ids=lambda b: b.value,
)
async def clients(request):
    """Yield (user_client, admin_client) for the backend under test.

    LOCAL drives the real-API test double in-process; the IAB backends are
    skipped until provisioned.
    """
    backend = request.param
    if backend is RegistryBackend.LOCAL:
        app = create_registry_double()
        transport = httpx.ASGITransport(app=app)
        user = RegistryClient(
            backend=backend, base_url="http://reg.local", auth_token="user-token",
            transport=transport,
        )
        admin = RegistryClient(
            backend=backend, base_url="http://reg.local", auth_token="admin-token",
            transport=transport,
        )
        async with user, admin:
            yield user, admin
    else:
        base_url = os.environ.get("AAMP_REGISTRY_URL")
        token = os.environ.get("AAMP_REGISTRY_TOKEN")
        if not (base_url and token):
            pytest.skip(f"{backend.value}: URL+token not provisioned — pending Mayank")
        client = RegistryClient(backend=backend, base_url=base_url, auth_token=token)
        async with client:
            yield client, client


async def test_register_list_get(clients) -> None:
    user, _admin = clients
    name = unique_name("seller")

    stored = await user.register_agent(make_registry_agent(name))
    assert isinstance(stored, RegistryAgent)
    assert stored.id is not None
    assert stored.agent_name == name
    # iab_member is forced false at registration.
    assert stored.iab_member is False

    listed = await user.list_agents(primary_domain="example.com")
    assert name in [a.agent_name for a in listed]

    fetched = await user.get_agent(stored.id)
    assert fetched.agent_name == name
    assert fetched.endpoint_url == f"https://example.com/a2a/{name}"


async def test_admin_update_delete_and_verify_trust(clients) -> None:
    user, admin = clients
    stored = await user.register_agent(make_registry_agent(unique_name("seller")))

    # Fresh registration: not yet active, no badge.
    trust = await user.verified_trust(stored.id)
    assert isinstance(trust, VerifiedTrust)
    assert trust.verified is False
    assert trust.badge_available is False

    # Admin promotes to active + IAB member.
    updated = await admin.update_agent(
        stored.id, verification_status="active", domain_verified=True, iab_member=True
    )
    assert updated.verification_status == "active"

    trust = await user.verified_trust(stored.id)
    assert trust.verified is True
    assert trust.iab_member is True
    assert trust.badge_available is True

    # The badge endpoint returns SVG bytes for members.
    badge = await user.verification_badge(stored.id)
    assert badge is not None and b"svg" in badge

    # Admin can delete; afterwards it is gone.
    await admin.delete_agent(stored.id)
    with pytest.raises(RegistryError) as exc:
        await user.get_agent(stored.id)
    assert exc.value.status_code == 404
    assert exc.value.code is ErrorCode.NOT_FOUND


async def test_non_admin_forbidden_on_admin_routes(clients) -> None:
    user, _admin = clients
    stored = await user.register_agent(make_registry_agent(unique_name("seller")))
    with pytest.raises(RegistryError) as exc:
        await user.update_agent(stored.id, verification_status="active")
    assert exc.value.status_code == 403
    assert exc.value.code is ErrorCode.FORBIDDEN


async def test_validate_endpoints(clients) -> None:
    user, _admin = clients
    name = unique_name("seller")
    await user.register_agent(make_registry_agent(name))

    domain_result = await user.validate_domain("example.com")
    assert domain_result.registered is True

    auth_result = await user.validate_agent("example.com", f"https://example.com/a2a/{name}")
    assert auth_result.agent_in_registry is True
    assert auth_result.authorized is False  # no authorized-agents.txt to check


# -- Behaviors only the in-process double can assert deterministically ------


async def test_missing_auth_is_unauthorized() -> None:
    app = create_registry_double()
    async with RegistryClient(
        base_url="http://reg.local", transport=httpx.ASGITransport(app=app)
    ) as anon:
        with pytest.raises(RegistryError) as exc:
            await anon.list_agents()
    assert exc.value.status_code == 401
    assert exc.value.code is ErrorCode.UNAUTHORIZED


async def test_domain_mismatch_forbidden() -> None:
    app = create_registry_double()
    async with RegistryClient(
        base_url="http://reg.local", auth_token="user-token",
        transport=httpx.ASGITransport(app=app),
    ) as user:
        with pytest.raises(RegistryError) as exc:
            await user.register_agent(make_registry_agent(unique_name("x"), domain="other.com"))
    assert exc.value.status_code == 403


async def test_x_new_token_refresh_updates_stored_token() -> None:
    app = create_registry_double()
    async with RegistryClient(
        base_url="http://reg.local", auth_token="expired-token",
        transport=httpx.ASGITransport(app=app),
    ) as client:
        assert client.token == "expired-token"
        # Any request against an expired-but-valid token gets refreshed.
        await client.list_agents()
        assert client.token == "user-token"
        assert client._http.headers["Authorization"] == "Bearer user-token"


# -- backend/config resolution (no server needed) ---------------------------


async def test_backend_resolves_from_env(monkeypatch) -> None:
    monkeypatch.setenv("AAMP_REGISTRY_BACKEND", "IAB_SANDBOX")
    monkeypatch.setenv("AAMP_REGISTRY_URL", "https://sandbox.iabtechlab.example/api")
    monkeypatch.setenv("AAMP_REGISTRY_TOKEN", "sekrit")
    async with RegistryClient() as client:
        assert client.backend is RegistryBackend.IAB_SANDBOX
        assert client.base_url == "https://sandbox.iabtechlab.example/api"
        assert client._http.headers["Authorization"] == "Bearer sekrit"


async def test_legacy_token_env_still_honored(monkeypatch) -> None:
    monkeypatch.delenv("AAMP_REGISTRY_TOKEN", raising=False)
    monkeypatch.setenv("AAMP_REGISTRY_AUTH_TOKEN", "legacy-secret")
    async with RegistryClient(backend=RegistryBackend.LOCAL) as client:
        assert client._http.headers["Authorization"] == "Bearer legacy-secret"


async def test_local_default_url(monkeypatch) -> None:
    for var in ("AAMP_REGISTRY_BACKEND", "AAMP_REGISTRY_URL", "AAMP_REGISTRY_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    async with RegistryClient() as client:
        assert client.backend is RegistryBackend.LOCAL
        assert client.base_url == LOCAL_DEFAULT_URL
        assert "Authorization" not in client._http.headers


async def test_iab_prod_default_url(monkeypatch) -> None:
    monkeypatch.delenv("AAMP_REGISTRY_URL", raising=False)
    async with RegistryClient(backend=RegistryBackend.IAB_PROD) as client:
        assert client.base_url == IAB_PROD_DEFAULT_URL


async def test_iab_sandbox_requires_url(monkeypatch) -> None:
    monkeypatch.delenv("AAMP_REGISTRY_URL", raising=False)
    with pytest.raises(ValueError, match="AAMP_REGISTRY_URL"):
        RegistryClient(backend=RegistryBackend.IAB_SANDBOX)


async def test_legacy_backend_alias_maps_to_local(monkeypatch) -> None:
    for var in ("AAMP_REGISTRY_URL", "AAMP_REGISTRY_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    async with RegistryClient(backend="LOCAL_SANDBOX") as client:
        assert client.backend is RegistryBackend.LOCAL
        assert client.base_url == LOCAL_DEFAULT_URL


async def test_unknown_backend_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown AAMP registry backend"):
        RegistryClient(backend="STAGING")


# -- LEGACY AAMP trust-tier surface (retained for the EP-7.1 harness) --------


@pytest.fixture
async def legacy_client():
    """RegistryClient over the legacy trust-tier sandbox app (in-process)."""
    app = create_app(store=AgentStore())
    client = RegistryClient(
        backend=RegistryBackend.LOCAL, base_url="http://sandbox.local",
        transport=httpx.ASGITransport(app=app),
    )
    async with client:
        yield client


def make_card(agent_id: str, agent_type: AgentType = AgentType.SELLER) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        name=f"Agent {agent_id}",
        description="Test agent",
        url=f"https://{agent_id}.example.com/a2a",
        agent_type=agent_type,
        provider=AgentProvider(name="Example Org"),
        supported_deal_types=["PG", "PD", "PA"],
    )


async def test_legacy_trust_tier_lifecycle(legacy_client: RegistryClient) -> None:
    """register -> discover -> fetch -> verify_trust -> admin tier change."""
    seller_id = unique_name("seller")
    stored = await legacy_client.register(make_card(seller_id, AgentType.SELLER))
    assert stored.trust_status is TrustStatus.REGISTERED

    sellers = await legacy_client.discover(AgentType.SELLER)
    assert seller_id in [c.agent_id for c in sellers]

    card = await legacy_client.fetch_card(seller_id)
    assert card.agent_id == seller_id

    verification = await legacy_client.verify_trust(seller_id)
    assert isinstance(verification, TrustVerification)
    assert isinstance(verification, AgentTrustVerification)
    assert verification.trust_status is TrustStatus.REGISTERED
    assert verification.max_access_tier is AccessTier.SEAT

    await legacy_client.set_trust(seller_id, TrustStatus.PREFERRED)
    verification = await legacy_client.verify_trust(seller_id)
    assert verification.max_access_tier is AccessTier.ADVERTISER


async def test_legacy_structured_errors(legacy_client: RegistryClient) -> None:
    with pytest.raises(RegistryError) as exc:
        await legacy_client.fetch_card(unique_name("ghost"))
    assert exc.value.status_code == 404
    assert exc.value.code is ErrorCode.NOT_FOUND

    agent_id = unique_name("dup")
    await legacy_client.register(make_card(agent_id))
    with pytest.raises(RegistryError) as exc:
        await legacy_client.register(make_card(agent_id))
    assert exc.value.status_code == 409
    assert exc.value.code is ErrorCode.CONTENTION
