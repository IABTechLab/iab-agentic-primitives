"""The ONE AAMP registry client — swapping registries is config, not code.

AAMP (Agentic Advertising Marketplace Protocol — the IAB Tech Lab agent
discovery and trust registry protocol) is how agents find counterparties
and verify their trust status. Per owner decision FD-3, the real IAB
sandbox registry is not available yet, so this library ships its own
representative sandbox (:mod:`iab_agentic_primitives.sandbox_registry`)
and this single client class that both the buyer and seller agents adopt.

All backends speak the same HTTP protocol; the backend is resolved from
config/env, so pointing an agent at the real IAB registry when access
lands is a configuration change only:

- ``AAMP_REGISTRY_BACKEND`` — one of ``LOCAL_SANDBOX`` (default),
  ``IAB_SANDBOX``, ``IAB_PROD``
- ``AAMP_REGISTRY_URL`` — registry base URL (required for the ``IAB_*``
  backends; defaults to ``http://127.0.0.1:8020`` for ``LOCAL_SANDBOX``)
- ``AAMP_REGISTRY_AUTH_TOKEN`` — optional bearer token, sent as
  ``Authorization: Bearer <token>`` (the auth slot for when IAB access
  lands; the local sandbox ignores it)

Requires ``httpx`` — install the ``client`` extra:
``pip install 'iab-agentic-primitives[client]'``.
"""

from __future__ import annotations

import os
from enum import Enum
from typing import Any

from pydantic import Field

from .primitives import AccessTier, AgentType, TrustStatus
from .protocol import AgentCard, AgentTrustVerification
from .protocol.errors import ErrorCode, ErrorDetail, ErrorEnvelope

try:  # httpx is the 'client' optional extra; models above stay importable without it.
    import httpx
except ImportError:  # pragma: no cover - exercised only without the extra
    httpx = None  # type: ignore[assignment]

ENV_BACKEND = "AAMP_REGISTRY_BACKEND"
ENV_URL = "AAMP_REGISTRY_URL"
ENV_AUTH_TOKEN = "AAMP_REGISTRY_AUTH_TOKEN"

#: Default base URL for the LOCAL_SANDBOX backend when AAMP_REGISTRY_URL is unset.
LOCAL_SANDBOX_DEFAULT_URL = "http://127.0.0.1:8020"

#: Maximum AccessTier an agent may claim per registry-verified TrustStatus.
#: Trust caps the effective tier server-side; it is never self-asserted.
TRUST_TIER_CEILING: dict[TrustStatus, AccessTier] = {
    TrustStatus.UNKNOWN: AccessTier.PUBLIC,
    TrustStatus.REGISTERED: AccessTier.SEAT,
    TrustStatus.APPROVED: AccessTier.AGENCY,
    TrustStatus.PREFERRED: AccessTier.ADVERTISER,
    TrustStatus.BLOCKED: AccessTier.PUBLIC,
}


class RegistryBackend(str, Enum):
    """Which AAMP registry deployment the client talks to.

    All three speak the same HTTP protocol; only the base URL (and, for
    the ``IAB_*`` backends, the auth token) differs — the swap is config.
    """

    LOCAL_SANDBOX = "LOCAL_SANDBOX"
    IAB_SANDBOX = "IAB_SANDBOX"
    IAB_PROD = "IAB_PROD"


class TrustVerification(AgentTrustVerification):
    """:class:`AgentTrustVerification` plus the derived access-tier ceiling.

    ``max_access_tier`` is the highest :class:`AccessTier` the agent may be
    granted given its registry-verified trust status (see
    :data:`TRUST_TIER_CEILING`); registry admins may lower it further.
    """

    max_access_tier: AccessTier = Field(
        default=AccessTier.PUBLIC,
        description="Highest AccessTier the agent may be granted; registry-enforced.",
    )


class RegistryError(Exception):
    """A structured protocol error returned by the registry.

    Carries the canonical :class:`ErrorDetail` parsed from the wire
    :class:`ErrorEnvelope`, plus the HTTP status code.
    """

    def __init__(self, status_code: int, detail: ErrorDetail):
        super().__init__(f"[{status_code}] {detail.error.value}: {detail.message}")
        self.status_code = status_code
        self.detail = detail

    @property
    def code(self) -> ErrorCode:
        return self.detail.error


_FALLBACK_CODES: dict[int, ErrorCode] = {
    401: ErrorCode.UNAUTHORIZED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    409: ErrorCode.CONTENTION,
    410: ErrorCode.QUOTE_EXPIRED,
    422: ErrorCode.VALIDATION,
}


class RegistryClient:
    """Async HTTP client for the AAMP registry surface.

    One class for every backend: construct it with no arguments and it
    resolves ``AAMP_REGISTRY_BACKEND`` / ``AAMP_REGISTRY_URL`` /
    ``AAMP_REGISTRY_AUTH_TOKEN`` from the environment. Explicit keyword
    arguments override the environment.

    ``transport`` accepts any ``httpx`` transport — pass
    ``httpx.ASGITransport(app=create_app(...))`` to drive the local
    sandbox in-process (no socket), exactly as the test suite does.
    """

    def __init__(
        self,
        backend: RegistryBackend | str | None = None,
        base_url: str | None = None,
        auth_token: str | None = None,
        *,
        transport: Any = None,
        timeout: float = 10.0,
    ):
        if httpx is None:  # pragma: no cover - exercised only without the extra
            raise ImportError(
                "RegistryClient requires httpx; install the 'client' extra: "
                "pip install 'iab-agentic-primitives[client]'"
            )
        raw_backend = backend or os.environ.get(ENV_BACKEND) or RegistryBackend.LOCAL_SANDBOX
        if isinstance(raw_backend, RegistryBackend):
            self.backend = raw_backend
        else:
            try:
                self.backend = RegistryBackend(raw_backend.upper())
            except ValueError:
                valid = ", ".join(b.value for b in RegistryBackend)
                raise ValueError(
                    f"Unknown AAMP registry backend {raw_backend!r}; expected one of: {valid}"
                ) from None

        base_url = base_url or os.environ.get(ENV_URL)
        if not base_url:
            if self.backend is RegistryBackend.LOCAL_SANDBOX:
                base_url = LOCAL_SANDBOX_DEFAULT_URL
            else:
                raise ValueError(
                    f"{ENV_URL} (or base_url=) must be set for backend "
                    f"{self.backend.value}: the IAB registry has no default URL yet"
                )
        self.base_url = base_url.rstrip("/")

        auth_token = auth_token if auth_token is not None else os.environ.get(ENV_AUTH_TOKEN)
        headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
        self._http = httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=timeout, transport=transport
        )

    # -- protocol surface --------------------------------------------------

    async def register(self, card: AgentCard) -> AgentCard:
        """Register an agent card; returns the stored card.

        The registry never trusts a self-asserted ``trust_status``: a
        fresh registration always comes back ``registered``.
        """
        response = await self._http.post("/agents", json=card.model_dump(mode="json"))
        self._raise_for_error(response)
        return AgentCard.model_validate(response.json())

    async def discover(self, agent_type: AgentType | str | None = None) -> list[AgentCard]:
        """List registered agents, optionally filtered by type.

        Blocked agents are excluded from discovery listings.
        """
        params: dict[str, str] = {}
        if agent_type is not None:
            value = agent_type.value if isinstance(agent_type, AgentType) else str(agent_type)
            params["agent_type"] = value
        response = await self._http.get("/agents", params=params)
        self._raise_for_error(response)
        return [AgentCard.model_validate(item) for item in response.json()]

    async def fetch_card(self, agent_id: str) -> AgentCard:
        """Fetch a single agent card by registry-issued id."""
        response = await self._http.get(f"/agents/{agent_id}")
        self._raise_for_error(response)
        return AgentCard.model_validate(response.json())

    async def verify_trust(self, agent_id: str) -> TrustVerification:
        """Registry-verified trust status + access-tier ceiling for an agent."""
        response = await self._http.get(f"/agents/{agent_id}/trust")
        self._raise_for_error(response)
        return TrustVerification.model_validate(response.json())

    async def set_trust(
        self,
        agent_id: str,
        trust_status: TrustStatus,
        max_access_tier: AccessTier | None = None,
    ) -> TrustVerification:
        """Admin convenience (sandbox testing): set an agent's trust status.

        Omitting ``max_access_tier`` applies the canonical ceiling for the
        new status (:data:`TRUST_TIER_CEILING`).
        """
        body: dict[str, Any] = {"trust_status": trust_status.value}
        if max_access_tier is not None:
            body["max_access_tier"] = max_access_tier.value
        response = await self._http.put(f"/agents/{agent_id}/trust", json=body)
        self._raise_for_error(response)
        return TrustVerification.model_validate(response.json())

    # -- plumbing -----------------------------------------------------------

    def _raise_for_error(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        try:
            detail = ErrorEnvelope.model_validate(response.json()).detail
        except Exception:
            code = _FALLBACK_CODES.get(response.status_code, ErrorCode.INTERNAL)
            detail = ErrorDetail(
                error=code, message=response.text[:500] or f"HTTP {response.status_code}"
            )
        raise RegistryError(response.status_code, detail)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "RegistryClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


__all__ = [
    "ENV_AUTH_TOKEN",
    "ENV_BACKEND",
    "ENV_URL",
    "LOCAL_SANDBOX_DEFAULT_URL",
    "TRUST_TIER_CEILING",
    "RegistryBackend",
    "RegistryClient",
    "RegistryError",
    "TrustVerification",
]
