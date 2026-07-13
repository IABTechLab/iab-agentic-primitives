"""Money (FD-11) and unknown-field policy (FD-13) tests.

FD = flagged decision (see the remediation plan §7.2 register and
RECONCILIATION.md).
"""

import pytest
from pydantic import ValidationError

from iab_agentic_primitives.primitives import Money, Quote


def test_money_roundtrip() -> None:
    m = Money(amount_micros=22_500_000, currency="EUR")
    restored = Money.model_validate_json(m.model_dump_json())
    assert restored == m
    assert restored.amount_micros == 22_500_000
    assert restored.currency == "EUR"


def test_money_from_decimal_str() -> None:
    m = Money.from_decimal_str("12.50")
    assert m.amount_micros == 12_500_000
    assert m.currency == "USD"
    assert Money.from_decimal_str("0.000001").amount_micros == 1
    assert Money.from_decimal_str("450", "GBP") == Money(amount_micros=450_000_000, currency="GBP")


def test_money_from_decimal_str_rejects_sub_micro_precision() -> None:
    with pytest.raises(ValueError):
        Money.from_decimal_str("0.0000001")


@pytest.mark.parametrize("bad_amount", [12.5, 12.0, "12.5"])
def test_money_rejects_non_integer_amounts(bad_amount: object) -> None:
    """amount_micros is a strict integer: float (and string) inputs are rejected."""
    with pytest.raises(ValidationError):
        Money(amount_micros=bad_amount)


@pytest.mark.parametrize("bad_currency", ["usd", "US", "USDX", "us$", ""])
def test_money_rejects_invalid_currency_codes(bad_currency: str) -> None:
    with pytest.raises(ValidationError):
        Money(amount_micros=1_000_000, currency=bad_currency)


def test_no_float_typed_money_fields_anywhere() -> None:
    """No wire primitive may declare a float-typed money-looking field (FD-11)."""
    import typing

    from iab_agentic_primitives.primitives import WIRE_PRIMITIVES

    money_words = ("price", "cpm", "cpp", "budget", "rate", "cost", "bidfloor", "value")
    # Fields that merely *sound* like money but are ratios/percentages.
    allowed_ratios = {
        "tier_discount_pct",
        "volume_discount_pct",
        "estimated_fill_rate",
        "estimated_rating",
        "concession_pct",
        "cumulative_concession_pct",
        "cancellable_pct",
        "delivery_rate",
    }

    def walk(model: type, seen: set[type]) -> None:
        if model in seen:
            return
        seen.add(model)
        for name, field in model.model_fields.items():
            anno = field.annotation
            args = typing.get_args(anno) or (anno,)
            is_floaty = float in args or anno is float
            if is_floaty and name not in allowed_ratios:
                assert not any(w in name for w in money_words), (
                    f"{model.__name__}.{name} is float-typed but looks like money; use Money"
                )
            for arg in args:
                if isinstance(arg, type) and hasattr(arg, "model_fields"):
                    walk(arg, seen)
            if isinstance(anno, type) and hasattr(anno, "model_fields"):
                walk(anno, seen)

    seen: set[type] = set()
    for model in WIRE_PRIMITIVES.values():
        walk(model, seen)


def test_unknown_fields_are_ignored_fd13() -> None:
    """Forward compatibility: implementations must ignore unknown fields."""
    from conftest import PRIMITIVE_INSTANCES

    quote = PRIMITIVE_INSTANCES["Quote"]
    payload = quote.model_dump(mode="json")
    payload["x_vendor_extension"] = {"anything": True}
    payload["field_from_the_future"] = "v2"
    restored = Quote.model_validate(payload)
    assert restored == quote
    assert not hasattr(restored, "field_from_the_future")
