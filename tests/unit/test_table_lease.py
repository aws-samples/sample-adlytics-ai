# SPDX-License-Identifier: MIT-0
"""ADR-075 §5 closed table-lease record form, canonical key, and expiry conversion."""

from __future__ import annotations

import pytest

from domain.operations.table_lease import (
    RELEASED_LEASE_EXPIRY,
    TableLeaseError,
    TableLeaseRecord,
    canonical_lease_instant,
    is_canonical_lease_expiry,
    lease_expiry_epoch_micros,
    table_lease_partition_key,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

# Every identity below is fictional fixture data; none is copied from a live system.

TABLE_ID = "db.alpha"
EXPIRES = "2026-09-19T13:00:00+00:00"


def _lease(**overrides: object) -> TableLeaseRecord:
    fields: dict[str, object] = {
        "physical_table_id": TABLE_ID,
        "holder": "holder-fixture-0001",
        "fencing_token": 7,
        "expires_at": EXPIRES,
    }
    fields.update(overrides)
    return TableLeaseRecord(**fields)  # type: ignore[arg-type]


def test_partition_key_is_the_canonical_single_attribute_table_prefix() -> None:
    assert table_lease_partition_key(TABLE_ID) == "TABLE#db.alpha"
    assert _lease().partition_key == "TABLE#db.alpha"


def test_partition_key_rejects_a_noncanonical_table_id() -> None:
    with pytest.raises(TableLeaseError, match="canonical"):
        table_lease_partition_key("no-database-qualifier")


def test_canonical_lease_expiry_accepts_only_the_stored_utc_isoformat_bytes() -> None:
    assert is_canonical_lease_expiry(EXPIRES)
    assert is_canonical_lease_expiry(RELEASED_LEASE_EXPIRY)
    # Z-suffix, naive, and free text differ from the store's byte-canonical rendering.
    assert not is_canonical_lease_expiry("2026-09-19T13:00:00Z")
    assert not is_canonical_lease_expiry("2026-09-19T13:00:00")
    assert not is_canonical_lease_expiry("soon")


def test_expiry_conversion_is_exact_utc_epoch_microseconds() -> None:
    # Grain: integer microseconds since the Unix epoch, by integer arithmetic only.
    assert lease_expiry_epoch_micros("1970-01-01T00:00:01+00:00") == 1_000_000
    assert lease_expiry_epoch_micros("1970-01-01T00:00:00.000001+00:00") == 1
    assert lease_expiry_epoch_micros(RELEASED_LEASE_EXPIRY) == 0


def test_expiry_conversion_rejects_noncanonical_text() -> None:
    with pytest.raises(TableLeaseError, match="canonical"):
        lease_expiry_epoch_micros("2026-09-19T13:00:00Z")


def test_canonical_lease_instant_is_the_exact_inverse_of_expiry_conversion() -> None:
    # Grain: a 20-digit epoch-microseconds instant renders as the store's expiry bytes.
    assert canonical_lease_instant("00000000000000000000") == RELEASED_LEASE_EXPIRY
    assert canonical_lease_instant("00000000000001000000") == "1970-01-01T00:00:01+00:00"
    assert canonical_lease_instant("00000000000000000001") == "1970-01-01T00:00:00.000001+00:00"
    for instant in ("00001789821296789012", "00001789822800000000", "00000000000000000000"):
        rendered = canonical_lease_instant(instant)
        assert is_canonical_lease_expiry(rendered)
        assert f"{lease_expiry_epoch_micros(rendered):020d}" == instant


def test_canonical_lease_instant_orders_lexically_as_the_underlying_moments() -> None:
    """The reclaim condition compares ``expires_at <= :instant`` as text in the store."""
    whole = canonical_lease_instant("00001789822800000000")  # ...T13:00:00+00:00
    later = canonical_lease_instant("00001789822800000001")  # ...T13:00:00.000001+00:00
    earlier = canonical_lease_instant("00001789822799999999")  # ...T12:59:59.999999+00:00
    assert earlier < whole < later
    assert earlier > RELEASED_LEASE_EXPIRY


def test_canonical_lease_instant_rejects_a_noncanonical_instant() -> None:
    with pytest.raises(TableLeaseError, match="20-digit"):
        canonical_lease_instant("1789821296789012")


def test_lease_record_accepts_the_closed_form_with_an_optional_park_instant() -> None:
    parked = _lease(parked_at="0" * 19 + "1")
    assert parked.parked_at == "0" * 19 + "1"
    assert parked.expiry_epoch_micros() == lease_expiry_epoch_micros(EXPIRES)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("physical_table_id", "unqualified", "canonical"),
        ("holder", "   ", "nonblank"),
        ("fencing_token", 0, "positive"),
        ("expires_at", "2026-09-19T13:00:00", "canonical UTC"),
        ("parked_at", "not-a-canonical-instant", "20-digit"),
    ],
)
def test_lease_record_rejects_each_field_outside_the_closed_form(
    field: str, value: object, message: str
) -> None:
    with pytest.raises(TableLeaseError, match=message):
        _lease(**{field: value})
