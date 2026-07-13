"""Round-trip tests: model -> JSON -> model for every wire primitive."""

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from iab_agentic_primitives.primitives import WIRE_PRIMITIVES

from conftest import PRIMITIVE_INSTANCES


def test_every_wire_primitive_has_a_fixture() -> None:
    assert set(PRIMITIVE_INSTANCES) == set(WIRE_PRIMITIVES)


@pytest.mark.parametrize("name", sorted(WIRE_PRIMITIVES))
def test_json_roundtrip(name: str) -> None:
    instance = PRIMITIVE_INSTANCES[name]
    model_cls = WIRE_PRIMITIVES[name]
    payload = instance.model_dump_json()
    restored = model_cls.model_validate_json(payload)
    assert restored == instance
    # A second pass must be byte-stable (no lossy round-trip).
    assert restored.model_dump_json() == payload


@pytest.mark.parametrize("name", sorted(WIRE_PRIMITIVES))
def test_timestamps_are_timezone_aware(name: str) -> None:
    """Every datetime on every primitive must be timezone-aware UTC."""
    instance = PRIMITIVE_INSTANCES[name]

    def check(value: object, path: str) -> None:
        if isinstance(value, datetime):
            assert value.tzinfo is not None, f"naive datetime at {path}"
            offset = value.utcoffset()
            assert offset is not None and offset.total_seconds() == 0, (
                f"non-UTC datetime at {path}"
            )
        elif isinstance(value, dict):
            for k, v in value.items():
                check(v, f"{path}.{k}")
        elif isinstance(value, list):
            for i, v in enumerate(value):
                check(v, f"{path}[{i}]")
        elif isinstance(value, BaseModel):
            for k, v in dict(value).items():
                check(v, f"{path}.{k}")

    check(instance, name)


def test_utc_now_is_aware() -> None:
    from iab_agentic_primitives.primitives import utc_now

    now = utc_now()
    assert now.tzinfo is UTC
