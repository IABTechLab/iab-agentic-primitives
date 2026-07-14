"""Shared test fixtures: one representative instance of every wire primitive."""

from datetime import date

import pytest

from iab_agentic_primitives.primitives import (
    Account,
    Agent,
    AgentProvider,
    Assignment,
    BuyerIdentity,
    ActorKind,
    ChangeRequest,
    ChangeType,
    ConsentContext,
    Creative,
    CreativeApproval,
    CreativeAsset,
    CreativeManifest,
    Deal,
    DealType,
    DecisionActor,
    DecisionInputRef,
    DecisionRationale,
    DecisionRecord,
    DecisionType,
    DeliveryGoal,
    DiligenceStatus,
    Line,
    LinearTVQuoteDetails,
    MakegoodDetails,
    MediaKit,
    MediaType,
    Money,
    Negotiation,
    NegotiationAction,
    NegotiationRound,
    OpenRTBParams,
    Order,
    Organization,
    OrganizationRole,
    Package,
    PackagePlacement,
    PricingTerms,
    Product,
    ProductRef,
    Proposal,
    ProposalLine,
    Quote,
    QuoteAvailability,
    QuotePricing,
    QuoteTerms,
    RateCard,
    RateCardEntry,
    SellersJsonEntry,
    SellerType,
    Session,
    SessionMessage,
    SupplyChain,
    SupplyChainNode,
)


def build_consent_context() -> ConsentContext:
    return ConsentContext(
        applicable_regimes=["GDPR", "CCPA"],
        gpp_string="DBABMA~CPXxRfAPXxRfAAfKABENB",
        gpp_section_ids=[2, 6],
        tcf_string="CPXxRfAPXxRfAAfKABENB",
        diligence_status=DiligenceStatus.PASSED,
    )


def build_product() -> Product:
    return Product(
        product_id="prod-001",
        seller_organization_id="org-seller-1",
        name="Premium Homepage Takeover",
        description="Above-the-fold homepage display",
        base_price=Money.from_decimal_str("25.00"),
        ad_formats=["banner", "video"],
        audience_targeting={"segment_ids": ["1001"]},
        available_impressions=5_000_000,
    )


def build_package() -> Package:
    return Package(
        package_id="pkg-001",
        name="Sports Premium",
        placements=[
            PackagePlacement(
                product_id="prod-001",
                product_name="Premium Homepage Takeover",
                ad_formats=["banner"],
                device_types=[2, 3],
            )
        ],
        cat=["IAB19"],
        geo_targets=["US", "US-NY"],
        base_price=Money.from_decimal_str("32.00"),
    )


def build_quote() -> Quote:
    return Quote(
        quote_id="q-123",
        deal_type=DealType.PROGRAMMATIC_GUARANTEED,
        product=ProductRef(product_id="prod-001", name="Premium Homepage Takeover"),
        pricing=QuotePricing(
            base_cpm=Money.from_decimal_str("25.00"),
            tier_discount_pct=10.0,
            final_cpm=Money.from_decimal_str("22.50"),
        ),
        terms=QuoteTerms(
            impressions=1_000_000,
            flight_start=date(2026, 8, 1),
            flight_end=date(2026, 8, 31),
            guaranteed=True,
        ),
        availability=QuoteAvailability(estimated_fill_rate=0.95),
        rate_card_id="rc-001",
        seller_id="agent-seller-1",
        media_type=MediaType.LINEAR_TV,
        linear_tv=LinearTVQuoteDetails(
            target_demo="A18-49",
            estimated_grps=120.0,
            estimated_rating=2.4,
            cpp=Money.from_decimal_str("450.00"),
            dayparts=["primetime"],
            networks=["NBC"],
            spots_per_week=12,
            total_spots=48,
            spot_length=30,
            measurement_currency="nielsen",
        ),
        consent_context=build_consent_context(),
    )


PRIMITIVE_INSTANCES = {
    "Organization": Organization(
        organization_id="org-buyer-1",
        name="Acme Media Buying",
        role=OrganizationRole.BUYER,
    ),
    "Account": Account(
        account_id="acct-001",
        buyer_organization_id="org-buyer-1",
        seller_organization_id="org-seller-1",
        advertiser_id="adv-coca-cola",
        name="Acme x PremiumPub",
    ),
    "Agent": Agent(
        agent_id="agent-seller-1",
        name="PremiumPub Seller Agent",
        description="Seller agent for PremiumPub inventory",
        url="https://seller.example.com/a2a",
        provider=AgentProvider(name="PremiumPub"),
        supported_deal_types=["PG", "PD", "PA"],
    ),
    "Product": build_product(),
    "MediaKit": MediaKit(
        media_kit_id="mk-001",
        seller_organization_id="org-seller-1",
        name="PremiumPub 2026 Media Kit",
        packages=[build_package()],
    ),
    "Package": build_package(),
    "RateCard": RateCard(
        rate_card_id="rc-001",
        buyer_organization_id="org-buyer-1",
        seller_organization_id="org-seller-1",
        account_id="acct-001",
        entries=[RateCardEntry(product_id="prod-001", rate=Money.from_decimal_str("21.00"))],
        effective_from=date(2026, 7, 1),
        effective_to=date(2026, 12, 31),
    ),
    "Quote": build_quote(),
    "Proposal": Proposal(
        proposal_id="prop-001",
        account_id="acct-001",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
        lines=[
            ProposalLine(
                proposal_line_id="pl-001",
                proposal_id="prop-001",
                product_id="prod-001",
                deal_type=DealType.PREFERRED_DEAL,
                delivery_goal=DeliveryGoal(
                    goal_type="impressions",
                    goal_amount=1_000_000,
                    billable_event="impression",
                ),
                pricing=PricingTerms(price=Money.from_decimal_str("22.50")),
            )
        ],
    ),
    "Negotiation": Negotiation(
        negotiation_id="neg-001",
        quote_id="q-123",
        product_id="prod-001",
        buyer_tier="advertiser",
        rounds=[
            NegotiationRound(
                round_number=1,
                buyer_price=Money.from_decimal_str("20.00"),
                seller_price=Money.from_decimal_str("23.00"),
                action=NegotiationAction.COUNTER,
                concession_pct=0.04,
                cumulative_concession_pct=0.04,
            )
        ],
    ),
    "Deal": Deal(
        deal_id="deal-001",
        deal_type=DealType.PROGRAMMATIC_GUARANTEED,
        quote_id="q-123",
        rate_card_id="rc-001",
        product=ProductRef(product_id="prod-001", name="Premium Homepage Takeover"),
        pricing=QuotePricing(
            base_cpm=Money.from_decimal_str("25.00"),
            final_cpm=Money.from_decimal_str("22.50"),
        ),
        terms=QuoteTerms(impressions=1_000_000, guaranteed=True),
        openrtb_params=OpenRTBParams(id="deal-001", bidfloor=Money.from_decimal_str("22.50")),
        consent_context=build_consent_context(),
    ),
    "Order": Order(
        order_id="ord-001",
        account_id="acct-001",
        deal_id="deal-001",
        name="Q3 Awareness Push",
        budget=Money.from_decimal_str("50000"),
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
        consent_context=build_consent_context(),
    ),
    "Line": Line(
        line_id="line-001",
        order_id="ord-001",
        product_id="prod-001",
        name="Homepage Takeover Aug",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
        rate=Money.from_decimal_str("22.50"),
        quantity=1_000_000,
    ),
    "ChangeRequest": ChangeRequest(
        change_request_id="CR-001",
        deal_id="deal-001",
        change_type=ChangeType.MAKEGOOD,
        requested_by="agent:agent-buyer-1",
        reason="GRP shortfall in week 2",
        makegood=MakegoodDetails(
            shortfall_grps=8.5,
            original_daypart="primetime",
            target_demo="A18-49",
        ),
    ),
    "Session": Session(
        session_id="sess-001",
        buyer_identity=BuyerIdentity(seat_id="ttd-seat-123", agency_id="omnicom-456"),
        messages=[SessionMessage(role="user", content="What CTV inventory do you have?")],
        active_deal_ids=["deal-001"],
    ),
    "Creative": Creative(
        creative_id="cr-001",
        account_id="acct-001",
        name="Summer Video 30s",
        creative_manifest=CreativeManifest(
            assets=[
                CreativeAsset(
                    asset_id="a-1",
                    asset_url="https://cdn.example.com/summer.mp4",
                    mime_type="video/mp4",
                    width=1920,
                    height=1080,
                    role="main",
                )
            ],
            declared_advertiser_domains=["example.com"],
            duration_ms=30_000,
        ),
    ),
    "CreativeApproval": CreativeApproval(
        approval_id="capp-001",
        creative_id="cr-001",
        status="approved",
        reviewer="human:ops-42",
        reason="Meets brand-safety and format requirements",
    ),
    "Assignment": Assignment(
        assignment_id="asg-001",
        creative_id="cr-001",
        line_id="line-001",
        sov=0.5,
        effective_start_date=date(2026, 8, 1),
        effective_end_date=date(2026, 8, 31),
    ),
    "ConsentContext": build_consent_context(),
    "SupplyChain": SupplyChain(
        complete=1,
        nodes=[
            SupplyChainNode(
                asi="exchange.example.com",
                sid="seller-001",
                hp=1,
                name="PremiumPub",
                domain="premiumpub.example.com",
            )
        ],
    ),
    "SellersJsonEntry": SellersJsonEntry(
        seller_id="seller-001",
        name="PremiumPub",
        domain="premiumpub.example.com",
        seller_type=SellerType.PUBLISHER,
    ),
    "DecisionRecord": DecisionRecord(
        decision_id="dr-001",
        subject_type="deal",
        subject_id="deal-001",
        decision_type=DecisionType.BOOKING,
        actor=DecisionActor(
            agent_id="agent-seller-1",
            kind=ActorKind.MACHINE,
            on_behalf_of="org-seller-1",
        ),
        inputs=[
            DecisionInputRef(
                kind="model_output",
                digest="sha256:fcde2b2edba56bf408601fb721fe9b5c",
                description="Pricing model recommendation",
            )
        ],
        rationale=DecisionRationale(
            summary="Booked at the advertiser-tier rate card price",
            factors=["advertiser_tier", "rate_card_match"],
        ),
        money_effect=Money.from_decimal_str("22500"),
        correlation_id="corr-001",
    ),
}


@pytest.fixture
def primitive_instances() -> dict[str, object]:
    return PRIMITIVE_INSTANCES
