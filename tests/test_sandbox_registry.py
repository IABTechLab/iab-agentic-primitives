"""Sandbox registry store: registration semantics, seed data, persistence."""

import json

import pytest

from iab_agentic_primitives.primitives import (
    AccessTier,
    AgentProvider,
    AgentType,
    TrustStatus,
)
from iab_agentic_primitives.protocol import AgentCard
from iab_agentic_primitives.registry_client import TRUST_TIER_CEILING
from iab_agentic_primitives.sandbox_registry import (
    AgentStore,
    DuplicateAgentError,
    UnknownAgentError,
)


def make_card(
    agent_id: str,
    agent_type: AgentType = AgentType.SELLER,
    trust_status: TrustStatus = TrustStatus.UNKNOWN,
) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        name=f"Agent {agent_id}",
        description="Test agent",
        url=f"https://{agent_id}.example.com/a2a",
        agent_type=agent_type,
        provider=AgentProvider(name="Example Org"),
        supported_deal_types=["PG", "PD"],
        trust_status=trust_status,
    )


def test_register_forces_registered_status() -> None:
    store = AgentStore()
    # A self-asserted "preferred" must NOT survive registration.
    stored = store.register(make_card("a-1", trust_status=TrustStatus.PREFERRED))
    assert stored.trust_status is TrustStatus.REGISTERED
    assert store.get("a-1").max_access_tier is AccessTier.SEAT


def test_register_duplicate_rejected() -> None:
    store = AgentStore()
    store.register(make_card("a-1"))
    with pytest.raises(DuplicateAgentError):
        store.register(make_card("a-1"))


def test_discovery_filters_type_and_hides_blocked() -> None:
    store = AgentStore()
    store.register(make_card("seller-1", AgentType.SELLER))
    store.register(make_card("seller-2", AgentType.SELLER))
    store.register(make_card("buyer-1", AgentType.BUYER))
    store.set_trust("seller-2", TrustStatus.BLOCKED)

    sellers = store.list_cards(AgentType.SELLER)
    assert [card.agent_id for card in sellers] == ["seller-1"]
    assert [card.agent_id for card in store.list_cards(AgentType.BUYER)] == ["buyer-1"]
    # Blocked agents disappear from discovery but stay fetchable.
    assert store.get("seller-2").card.trust_status is TrustStatus.BLOCKED


def test_set_trust_applies_canonical_ceiling_and_explicit_override() -> None:
    store = AgentStore()
    store.register(make_card("a-1"))
    for status, ceiling in TRUST_TIER_CEILING.items():
        record = store.set_trust("a-1", status)
        assert record.card.trust_status is status
        assert record.max_access_tier is ceiling
    # An admin may pin a ceiling lower than the canonical one.
    record = store.set_trust("a-1", TrustStatus.PREFERRED, AccessTier.SEAT)
    assert record.max_access_tier is AccessTier.SEAT


def test_unknown_agent_raises() -> None:
    store = AgentStore()
    with pytest.raises(UnknownAgentError):
        store.get("nope")
    with pytest.raises(UnknownAgentError):
        store.set_trust("nope", TrustStatus.APPROVED)


def test_verification_shape() -> None:
    store = AgentStore()
    store.register(make_card("a-1"))
    verification = store.verification("a-1", "local_sandbox")
    assert verification.agent_id == "a-1"
    assert verification.agent_url == "https://a-1.example.com/a2a"
    assert verification.trust_status is TrustStatus.REGISTERED
    assert verification.max_access_tier is AccessTier.SEAT
    assert verification.registry_id == "local_sandbox"
    assert verification.verified_at is not None
    assert verification.verified_at.tzinfo is not None


def test_seed_loading(tmp_path) -> None:
    seed = {
        "agents": [
            {
                "card": make_card("seed-seller", AgentType.SELLER).model_dump(mode="json"),
                "trust_status": "preferred",
            },
            {
                "card": make_card("seed-buyer", AgentType.BUYER).model_dump(mode="json"),
                "trust_status": "approved",
                "max_access_tier": "seat",
            },
            # No trust fields at all: defaults to registered/seat.
            {"card": make_card("seed-plain").model_dump(mode="json")},
        ]
    }
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(json.dumps(seed))

    store = AgentStore()
    assert store.load_seed(seed_path) == 3
    assert store.get("seed-seller").card.trust_status is TrustStatus.PREFERRED
    assert store.get("seed-seller").max_access_tier is AccessTier.ADVERTISER
    assert store.get("seed-buyer").max_access_tier is AccessTier.SEAT
    assert store.get("seed-plain").card.trust_status is TrustStatus.REGISTERED
    assert store.get("seed-plain").max_access_tier is AccessTier.SEAT


def test_persistence_roundtrip(tmp_path) -> None:
    persist = tmp_path / "registry.json"
    store = AgentStore(persist_path=persist)
    store.register(make_card("a-1"))
    store.set_trust("a-1", TrustStatus.APPROVED)

    # A fresh store on the same path sees the same state (persist == seed shape).
    reloaded = AgentStore(persist_path=persist)
    record = reloaded.get("a-1")
    assert record.card.trust_status is TrustStatus.APPROVED
    assert record.max_access_tier is AccessTier.AGENCY

    # And the persisted file is directly usable as seed data elsewhere.
    other = AgentStore()
    assert other.load_seed(persist) == 1


def test_persist_path_from_env(tmp_path, monkeypatch) -> None:
    persist = tmp_path / "env-registry.json"
    monkeypatch.setenv(AgentStore.ENV_PERSIST_PATH, str(persist))
    store = AgentStore()
    store.register(make_card("a-env"))
    assert persist.exists()
    assert AgentStore().get("a-env").card.trust_status is TrustStatus.REGISTERED


def test_create_app_seeds_from_env(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastapi")
    from iab_agentic_primitives.sandbox_registry import create_app

    seed_path = tmp_path / "seed.json"
    seed_path.write_text(
        json.dumps({"agents": [{"card": make_card("seeded").model_dump(mode="json")}]})
    )
    monkeypatch.setenv("AAMP_SANDBOX_SEED_PATH", str(seed_path))
    app = create_app()
    assert app.state.store.get("seeded").card.trust_status is TrustStatus.REGISTERED
