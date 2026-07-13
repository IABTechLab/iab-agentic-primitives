"""Shared wire primitives exchanged between the buyer and seller agents.

Every primitive the two agents exchange is defined exactly once here --
Product, MediaKit, Package, RateCard, Quote, Proposal, Negotiation, Deal,
Order, Line, ChangeRequest, Session, Creative, Assignment, and the identity
objects (Agent, Organization, Account). Agent-local primitives (a buyer's
planning internals, a seller's pricing configuration) stay in their own
repos; only the "both" wire primitives live in this package, which kills
the copy-fork divergence and incompatible-schema problems by making sure
there is exactly one schema for anything that crosses the wire.

Modules:

- ``identity``  -- Organization, Account, Agent, BuyerIdentity, ConsentContext
- ``inventory`` -- Product, MediaKit, Package
- ``pricing``   -- DealType, pricing enums, linear TV params, RateCard, Quote
- ``lifecycle`` -- Proposal, Negotiation, Deal, Order, Line, ChangeRequest, Session
- ``creative``  -- Creative, Assignment

Reconciliation decisions (which repo's shape won, and why) are recorded in
RECONCILIATION.md at the repository root.
"""

from ._util import Money, WireModel, utc_now
from .creative import (
    AdProfile,
    Assignment,
    ContentPolicy,
    Creative,
    CreativeAsset,
    CreativeManifest,
    ReviewStatus,
    RotationMode,
)
from .identity import (
    AccessTier,
    Account,
    AccountStatus,
    Agent,
    AgentAuthentication,
    AgentCapabilities,
    AgentProvider,
    AgentSkill,
    AgentType,
    BuyerIdentity,
    ConsentContext,
    DiligenceStatus,
    Organization,
    OrganizationRole,
    OrganizationStatus,
    TrustStatus,
)
from .inventory import (
    CommercialTerms,
    DeliveryType,
    MediaKit,
    Package,
    PackageLayer,
    PackagePlacement,
    PackageStatus,
    Product,
)
from .lifecycle import (
    BillableEvent,
    ChangeRequest,
    ChangeRequestStatus,
    ChangeSeverity,
    ChangeType,
    Deal,
    DealStatus,
    DeliveryGoal,
    FieldDiff,
    GoalType,
    Line,
    LineStatus,
    MakegoodDetails,
    Negotiation,
    NegotiationAction,
    NegotiationRound,
    NegotiationStatus,
    OpenRTBParams,
    Order,
    OrderStatus,
    PricingTerms,
    Proposal,
    ProposalLine,
    ProposalStatus,
    Session,
    SessionMessage,
    SessionStatus,
)
from .pricing import (
    CancellationTerms,
    DealType,
    LinearTVParams,
    LinearTVQuoteDetails,
    MediaType,
    PricingModel,
    PricingType,
    ProductRef,
    Quote,
    QuoteAvailability,
    QuotePricing,
    QuoteStatus,
    QuoteTerms,
    RateCard,
    RateCardEntry,
    RateCardStatus,
)

# The top-level exchanged primitives, in spec order. This mapping drives the
# JSON-Schema export (spec/generate_schemas.py) and the drift-guard test.
WIRE_PRIMITIVES: dict[str, type] = {
    "Organization": Organization,
    "Account": Account,
    "Agent": Agent,
    "Product": Product,
    "MediaKit": MediaKit,
    "Package": Package,
    "RateCard": RateCard,
    "Quote": Quote,
    "Proposal": Proposal,
    "Negotiation": Negotiation,
    "Deal": Deal,
    "Order": Order,
    "Line": Line,
    "ChangeRequest": ChangeRequest,
    "Session": Session,
    "Creative": Creative,
    "Assignment": Assignment,
    "ConsentContext": ConsentContext,
}

__all__ = [
    "WIRE_PRIMITIVES",
    "Money",
    "WireModel",
    "utc_now",
    # identity
    "AccessTier",
    "Account",
    "AccountStatus",
    "Agent",
    "AgentAuthentication",
    "AgentCapabilities",
    "AgentProvider",
    "AgentSkill",
    "AgentType",
    "BuyerIdentity",
    "ConsentContext",
    "DiligenceStatus",
    "Organization",
    "OrganizationRole",
    "OrganizationStatus",
    "TrustStatus",
    # inventory
    "CommercialTerms",
    "DeliveryType",
    "MediaKit",
    "Package",
    "PackageLayer",
    "PackagePlacement",
    "PackageStatus",
    "Product",
    # pricing
    "CancellationTerms",
    "DealType",
    "LinearTVParams",
    "LinearTVQuoteDetails",
    "MediaType",
    "PricingModel",
    "PricingType",
    "ProductRef",
    "Quote",
    "QuoteAvailability",
    "QuotePricing",
    "QuoteStatus",
    "QuoteTerms",
    "RateCard",
    "RateCardEntry",
    "RateCardStatus",
    # lifecycle
    "BillableEvent",
    "ChangeRequest",
    "ChangeRequestStatus",
    "ChangeSeverity",
    "ChangeType",
    "Deal",
    "DealStatus",
    "DeliveryGoal",
    "FieldDiff",
    "GoalType",
    "Line",
    "LineStatus",
    "MakegoodDetails",
    "Negotiation",
    "NegotiationAction",
    "NegotiationRound",
    "NegotiationStatus",
    "OpenRTBParams",
    "Order",
    "OrderStatus",
    "PricingTerms",
    "Proposal",
    "ProposalLine",
    "ProposalStatus",
    "Session",
    "SessionMessage",
    "SessionStatus",
    # creative
    "AdProfile",
    "Assignment",
    "ContentPolicy",
    "Creative",
    "CreativeAsset",
    "CreativeManifest",
    "ReviewStatus",
    "RotationMode",
]
