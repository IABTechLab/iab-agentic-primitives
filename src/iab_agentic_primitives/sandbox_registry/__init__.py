"""Local sandbox AAMP registry — a config-swappable development stand-in.

AAMP (Agentic Advertising Marketplace Protocol — the IAB Tech Lab agent
discovery and trust registry protocol) is the registry the agents use for
counterparty discovery and trust verification. The real IAB sandbox is
not available yet, so this package stands up OUR OWN representative
registry (owner decision FD-3). Agents talk to it through
:class:`iab_agentic_primitives.registry_client.RegistryClient`; moving to
the real IAB registry later is a config change, never a code change.

The FastAPI app lives in :mod:`.app` (requires the ``sandbox`` extra);
the dependency-free in-memory store lives in :mod:`.store`. Importing
this package does not require fastapi — ``create_app`` is loaded lazily.
"""

from typing import Any

from .store import (
    AgentRecord,
    AgentStore,
    DuplicateAgentError,
    SeedEntry,
    SeedFile,
    UnknownAgentError,
)

__all__ = [
    "AgentRecord",
    "AgentStore",
    "DuplicateAgentError",
    "SeedEntry",
    "SeedFile",
    "UnknownAgentError",
    "create_app",
]


def __getattr__(name: str) -> Any:
    if name == "create_app":  # lazy: keeps `import ...sandbox_registry` fastapi-free
        from .app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
