"""RegistryClient driven against the sandbox registry, parameterized by backend.

The SAME suite runs against every backend — that parameterization is the
acceptance criterion for the config-swap story (owner decision FD-3):

- ``LOCAL_SANDBOX``: the sandbox FastAPI app spun up in-process via
  ``httpx.ASGITransport`` (no socket).
- ``IAB_SANDBOX``: skipped-with-reason until the real IAB sandbox URL
  exists; set ``AAMP_REGISTRY_URL`` to run the identical suite against it.
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
from iab_agentic_primitives.registry_client import (
    LOCAL_SANDBOX_DEFAULT_URL,
    RegistryBackend,
    RegistryClient,
    RegistryError,
    TrustVerification,
)
from iab_agentic_primitives.protocol.errors import ErrorCode
from iab_agentic_primitives.sandbox_registry import AgentStore, create_app

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


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


def unique_id(prefix: str) -> str:
    """Registry-unique agent ids so the suite can also run against a shared
    remote registry once IAB_SANDBOX access lands."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@pytest.fixture(
    params=[RegistryBackend.LOCAL_SANDBOX, RegistryBackend.IAB_SANDBOX],
    ids=lambda backend: backend.value,
)
async def client(request):
    backend = request.param
    if backend is RegistryBackend.LOCAL_SANDBOX:
        app = create_app(store=AgentStore())
        registry_client = RegistryClient(
            backend=backend,
            base_url="http://sandbox.local",
            transport=httpx.ASGITransport(app=app),
        )
    else:
        base_url = os.environ.get("AAMP_REGISTRY_URL")
        if not base_url:
            pytest.skip(
                "IAB sandbox registry is not yet available; set AAMP_REGISTRY_URL "
                "(and AAMP_REGISTRY_AUTH_TOKEN) once IAB access lands to run this "
                "same suite against it"
            )
        registry_client = RegistryClient(backend=backend, base_url=base_url)
    async with registry_client:
        yield registry_client


async def test_full_lifecycle(client: RegistryClient) -> None:
    """register -> discover -> fetch -> verify trust -> admin tier change."""
    seller_id = unique_id("seller")
    buyer_id = unique_id("buyer")

    # Register: trust_status is registry-assigned, never self-asserted.
    stored = await client.register(make_card(seller_id, AgentType.SELLER))
    assert stored.agent_id == seller_id
    assert stored.trust_status is TrustStatus.REGISTERED
    await client.register(make_card(buyer_id, AgentType.BUYER))

    # Discover by type.
    sellers = await client.discover(AgentType.SELLER)
    assert seller_id in [card.agent_id for card in sellers]
    assert buyer_id not in [card.agent_id for card in sellers]
    buyers = await client.discover("buyer")  # plain-string agent_type also accepted
    assert buyer_id in [card.agent_id for card in buyers]

    # Fetch a single card.
    card = await client.fetch_card(seller_id)
    assert card.agent_id == seller_id
    assert card.url == f"https://{seller_id}.example.com/a2a"

    # Verify trust: fresh registration => registered, seat-tier ceiling.
    verification = await client.verify_trust(seller_id)
    assert isinstance(verification, TrustVerification)
    assert isinstance(verification, AgentTrustVerification)
    assert verification.trust_status is TrustStatus.REGISTERED
    assert verification.max_access_tier is AccessTier.SEAT
    assert verification.verified_at is not None

    # Admin trust change is reflected by verify_trust.
    await client.set_trust(seller_id, TrustStatus.PREFERRED)
    verification = await client.verify_trust(seller_id)
    assert verification.trust_status is TrustStatus.PREFERRED
    assert verification.max_access_tier is AccessTier.ADVERTISER

    # Explicit ceiling override sticks.
    await client.set_trust(seller_id, TrustStatus.APPROVED, AccessTier.SEAT)
    verification = await client.verify_trust(seller_id)
    assert verification.trust_status is TrustStatus.APPROVED
    assert verification.max_access_tier is AccessTier.SEAT


async def test_blocked_agent_hidden_from_discovery(client: RegistryClient) -> None:
    agent_id = unique_id("seller")
    await client.register(make_card(agent_id, AgentType.SELLER))
    await client.set_trust(agent_id, TrustStatus.BLOCKED)

    listed = [card.agent_id for card in await client.discover(AgentType.SELLER)]
    assert agent_id not in listed
    # ... but still fetchable and honest about being blocked.
    card = await client.fetch_card(agent_id)
    assert card.trust_status is TrustStatus.BLOCKED
    verification = await client.verify_trust(agent_id)
    assert verification.trust_status is TrustStatus.BLOCKED
    assert verification.max_access_tier is AccessTier.PUBLIC


async def test_structured_errors(client: RegistryClient) -> None:
    missing_id = unique_id("ghost")
    with pytest.raises(RegistryError) as excinfo:
        await client.fetch_card(missing_id)
    assert excinfo.value.status_code == 404
    assert excinfo.value.code is ErrorCode.NOT_FOUND

    with pytest.raises(RegistryError) as excinfo:
        await client.verify_trust(missing_id)
    assert excinfo.value.code is ErrorCode.NOT_FOUND

    agent_id = unique_id("dup")
    await client.register(make_card(agent_id))
    with pytest.raises(RegistryError) as excinfo:
        await client.register(make_card(agent_id))
    assert excinfo.value.status_code == 409
    assert excinfo.value.code is ErrorCode.CONTENTION


# -- backend/config resolution (no server needed) ---------------------------


async def test_backend_resolves_from_env(monkeypatch) -> None:
    monkeypatch.setenv("AAMP_REGISTRY_BACKEND", "IAB_SANDBOX")
    monkeypatch.setenv("AAMP_REGISTRY_URL", "https://sandbox.iabtechlab.example/aamp")
    monkeypatch.setenv("AAMP_REGISTRY_AUTH_TOKEN", "sekrit")
    async with RegistryClient() as client:
        assert client.backend is RegistryBackend.IAB_SANDBOX
        assert client.base_url == "https://sandbox.iabtechlab.example/aamp"
        # The auth slot for when IAB access lands.
        assert client._http.headers["Authorization"] == "Bearer sekrit"


async def test_local_sandbox_default_url(monkeypatch) -> None:
    monkeypatch.delenv("AAMP_REGISTRY_BACKEND", raising=False)
    monkeypatch.delenv("AAMP_REGISTRY_URL", raising=False)
    monkeypatch.delenv("AAMP_REGISTRY_AUTH_TOKEN", raising=False)
    async with RegistryClient() as client:
        assert client.backend is RegistryBackend.LOCAL_SANDBOX
        assert client.base_url == LOCAL_SANDBOX_DEFAULT_URL
        assert "Authorization" not in client._http.headers


async def test_iab_backends_require_url(monkeypatch) -> None:
    monkeypatch.delenv("AAMP_REGISTRY_URL", raising=False)
    for backend in (RegistryBackend.IAB_SANDBOX, RegistryBackend.IAB_PROD):
        with pytest.raises(ValueError, match="AAMP_REGISTRY_URL"):
            RegistryClient(backend=backend)


async def test_unknown_backend_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown AAMP registry backend"):
        RegistryClient(backend="STAGING")
