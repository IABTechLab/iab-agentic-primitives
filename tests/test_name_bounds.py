"""Name-length bounds conformed to the OpenDirect 2.1 spec.

OpenDirect = the IAB (Interactive Advertising Bureau) direct-buying API
standard. ``Product.name`` (and the equivalent identity-adjacent names on
``Organization``/``Account``) previously used a uniform 128-char bound
inherited from neither source repo's actual spec grounding; this brings
them in line with OpenDirect 2.1: Product name 100, Organization/Account
name 120. Bounds that already matched spec (``Creative.name`` 255,
the lifecycle ``name`` fields at 200) are deliberately untouched.
"""

from datetime import date

import pytest
from pydantic import ValidationError

from iab_agentic_primitives.primitives import (
    Account,
    Creative,
    Money,
    Organization,
    OrganizationRole,
    Product,
)
from iab_agentic_primitives.primitives.lifecycle import Line, Order

# ---------------------------------------------------------------------------
# Product.name: 128 -> 100
# ---------------------------------------------------------------------------


def _product(name: str) -> Product:
    return Product(product_id="p-1", seller_organization_id="org-1", name=name)


def test_product_name_100_chars_accepted() -> None:
    assert _product("x" * 100).name == "x" * 100


def test_product_name_101_chars_rejected() -> None:
    with pytest.raises(ValidationError):
        _product("x" * 101)


# ---------------------------------------------------------------------------
# Organization.name: 128 -> 120
# ---------------------------------------------------------------------------


def _organization(name: str) -> Organization:
    return Organization(organization_id="org-1", name=name, role=OrganizationRole.SELLER)


def test_organization_name_120_chars_accepted() -> None:
    assert _organization("x" * 120).name == "x" * 120


def test_organization_name_121_chars_rejected() -> None:
    with pytest.raises(ValidationError):
        _organization("x" * 121)


# ---------------------------------------------------------------------------
# Account.name: 128 -> 120
# ---------------------------------------------------------------------------


def _account(name: str) -> Account:
    return Account(
        account_id="acct-1",
        buyer_organization_id="org-buyer",
        seller_organization_id="org-seller",
        name=name,
    )


def test_account_name_120_chars_accepted() -> None:
    assert _account("x" * 120).name == "x" * 120


def test_account_name_121_chars_rejected() -> None:
    with pytest.raises(ValidationError):
        _account("x" * 121)


# ---------------------------------------------------------------------------
# Untouched bounds: creative 255, lifecycle 200 — regression guards only
# ---------------------------------------------------------------------------


def test_creative_name_255_chars_still_accepted() -> None:
    assert Creative(creative_id="cr-1", name="x" * 255).name == "x" * 255
    with pytest.raises(ValidationError):
        Creative(creative_id="cr-2", name="x" * 256)


def test_lifecycle_name_200_chars_still_accepted() -> None:
    order_kwargs = dict(
        account_id="acct-1",
        budget=Money.from_decimal_str("1000.00"),
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
    )
    assert Order(order_id="o-1", name="x" * 200, **order_kwargs).name == "x" * 200
    with pytest.raises(ValidationError):
        Order(order_id="o-2", name="x" * 201, **order_kwargs)

    line_kwargs = dict(
        order_id="o-1",
        product_id="p-1",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        rate=Money.from_decimal_str("10.00"),
        quantity=1000,
    )
    assert Line(line_id="l-1", name="x" * 200, **line_kwargs).name == "x" * 200
    with pytest.raises(ValidationError):
        Line(line_id="l-2", name="x" * 201, **line_kwargs)
