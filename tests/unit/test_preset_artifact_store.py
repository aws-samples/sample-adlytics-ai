# SPDX-License-Identifier: MIT-0
"""The preset-artifact pointer store's item envelope (S6 §2.8, D28; carried item 6).

Only a grant-bearing pointer carries the graph fields (`grant_id`, `serving_generation`,
`written_at`), and it carries all three. A legacy (grant-less) pointer's item stays exactly
today's envelope, which the deployed reader rebuilds and compares, so a legacy bake after the
Publisher deploy is still served by an Api that has not been deployed yet.
"""

from __future__ import annotations

# The item renderer and reader are the store's private seams.
# pyright: reportPrivateUsage=false
from dataclasses import replace
from typing import Any, cast

import pytest
from dynamodb_reserved_words import guard_reserved_words

from adapters.outbound.dynamodb.preset_artifacts import (
    PRESET_ARTIFACT_POINTER_MALFORMED,
    DynamoPresetArtifactPointerStore,
    _pointer_from_item,
    _pointer_item,
    pointer_transaction_items,
)
from domain.operations.execution_graph_release import serving_generation
from domain.presentation.preset_artifacts import (
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactPointerAdvanceRequest,
    PresetArtifactRevision,
    PresetArtifactVariant,
    pointer_written_at,
)
from domain.shared_kernel.errors import InvariantError
from ports.outbound.preset_artifacts import LegacyPointerAfterCutoverError

pytestmark = [pytest.mark.unit]

PAGE = PresetArtifactPage("campaigns")
VARIANT = PresetArtifactVariant("7d")
GRANT = "a" * 64
LEGACY_ATTRIBUTES = frozenset(
    {
        "page_variant_key",
        "record_key",
        "record_type",
        "page",
        "variant",
        "revision",
        "body_key",
        "body_digest",
        "forward_token",
        "supersedes",
        "custody_digest",
    }
)
"""Every attribute today's envelope (`_pointer_item` before S6) can carry."""


def _legacy() -> PresetArtifactPointer:
    return PresetArtifactPointer(
        PAGE,
        VARIANT,
        PresetArtifactRevision.from_publication_identities(("publication-2",)),
        "b" * 64,
        2,
        PresetArtifactRevision.from_publication_identities(("publication-1",)),
        "c" * 64,
    )


def _granted() -> PresetArtifactPointer:
    return replace(
        _legacy(),
        grant_id=GRANT,
        serving_generation=serving_generation(GRANT),
        written_at="2026-10-07T09:00:00Z",
    )


@guard_reserved_words
class _Roster:
    """A `transact_get_items` client that returns one stored CURRENT item."""

    def __init__(self, item: dict[str, Any]) -> None:
        self.item = item

    def transact_get_items(self, **_request: Any) -> dict[str, Any]:
        return {"Responses": [{"Item": self.item}]}


def test_d28_a_grant_less_pointer_with_written_at_is_refused() -> None:
    with pytest.raises(ValueError, match="grant-less preset artifact pointer carries no graph"):
        replace(_legacy(), written_at="2026-10-07T09:00:00Z")


def test_d28_a_grant_bearing_pointer_without_serving_generation_is_refused() -> None:
    with pytest.raises(ValueError, match="carries its serving generation"):
        replace(_legacy(), grant_id=GRANT, written_at="2026-10-07T09:00:00Z")


@pytest.mark.parametrize(
    "written_at",
    ["2026-10-07T09:00:00.5Z", "2026-10-07T09:00:00+00:00", "2026-13-07T09:00:00Z"],
)
def test_d28_a_malformed_stored_written_at_reads_pointer_malformed(written_at: str) -> None:
    item: dict[str, Any] = dict(_pointer_item(_granted(), current=True))
    item["written_at"] = {"S": written_at}
    store = DynamoPresetArtifactPointerStore("fictional-pointers", cast("Any", _Roster(item)))

    with pytest.raises(InvariantError) as refused:
        store.current(PAGE, VARIANT)

    assert refused.value.code == PRESET_ARTIFACT_POINTER_MALFORMED


def test_d28_control_a_legacy_item_is_todays_envelope_and_round_trips_byte_equal() -> None:
    pointer = _legacy()
    item = _pointer_item(pointer, current=True)

    assert frozenset(item) <= LEGACY_ATTRIBUTES
    assert not frozenset(item) & {"grant_id", "serving_generation", "written_at"}
    assert _pointer_from_item(dict(item), current=True) == pointer
    assert _pointer_item(_pointer_from_item(dict(item), current=True), current=True) == item


def test_d28_control_a_valid_grant_bearing_pointer_round_trips() -> None:
    pointer = _granted()
    item: dict[str, Any] = dict(_pointer_item(pointer, current=True))
    store = DynamoPresetArtifactPointerStore("fictional-pointers", cast("Any", _Roster(item)))

    assert {item["grant_id"]["S"], item["written_at"]["S"]} == {GRANT, "2026-10-07T09:00:00Z"}
    assert store.current(PAGE, VARIANT) == pointer


def test_the_write_instant_renders_utc_whole_seconds() -> None:
    from datetime import UTC, datetime, timedelta, timezone  # noqa: PLC0415

    instant = datetime(2026, 10, 7, 11, 0, 0, 900_000, tzinfo=timezone(timedelta(hours=2)))
    assert pointer_written_at(instant) == "2026-10-07T09:00:00Z"
    with pytest.raises(ValueError, match="timezone-aware"):
        pointer_written_at(datetime(2026, 10, 7, 9, 0, tzinfo=UTC).replace(tzinfo=None))


# --- S6 §2.9 (AGG3-6): the legacy pointer's graph mode ConditionCheck across the CAS ----------


class _Tables:
    """The pointer table and the graph table's mode item in one transactional fake."""

    def __init__(self) -> None:
        from execution_graph_support import FakeDynamo  # noqa: PLC0415

        class _Fake(FakeDynamo):
            def _key(self, key: dict[str, Any]) -> tuple[str, str]:
                if "page_variant_key" in key:
                    return (f"P|{key['page_variant_key']['S']}", str(key["record_key"]["S"]))
                return super()._key(key)

        self.fake = _Fake()

    def set_mode(self, mode: str) -> None:
        self.fake.items[("MODE#ingestion", "MODE")] = {
            "pk": {"S": "MODE#ingestion"},
            "sk": {"S": "MODE"},
            "mode": {"S": mode},
            "revision": {"N": "3"},
        }

    def store(self) -> DynamoPresetArtifactPointerStore:
        return DynamoPresetArtifactPointerStore(
            "fictional-pointers", cast("Any", self.fake), graph_table="fictional-graph"
        )


def _first_legacy() -> PresetArtifactPointerAdvanceRequest:
    return PresetArtifactPointerAdvanceRequest(replace(_legacy(), supersedes=None, forward_token=1))


def test_agg3_6_a_legacy_write_after_the_cas_is_refused_with_zero_effect() -> None:
    tables = _Tables()
    tables.set_mode("LEGACY_DRAINING")  # the baker's batch read LEGACY_DRAINING
    request = _first_legacy()

    def cas(_fake: object) -> None:  # the GRAPH CAS commits between the read and the write
        tables.set_mode("GRAPH")

    tables.fake.before_transaction = cas

    with pytest.raises(LegacyPointerAfterCutoverError):
        tables.store().advance(request)

    assert not any(key[0].startswith("P|") for key in tables.fake.items)


def test_agg3_6_control_without_the_cas_the_legacy_write_commits_with_the_check() -> None:
    tables = _Tables()
    tables.set_mode("LEGACY_DRAINING")

    result = tables.store().advance(_first_legacy())

    assert result.outcome.value == "advanced"
    (members,) = tables.fake.transactions
    check = members[-1]["ConditionCheck"]
    assert (check["TableName"], check["Key"]["pk"]["S"]) == ("fictional-graph", "MODE#ingestion")
    assert check["ConditionExpression"] == "attribute_not_exists(pk) OR #mode <> :graph"


def test_agg3_6_a_grant_bearing_write_carries_no_mode_check() -> None:
    granted = PresetArtifactPointerAdvanceRequest(
        replace(_granted(), supersedes=None, forward_token=1)
    )
    items = pointer_transaction_items("fictional-pointers", granted, "fictional-graph")
    assert not any("ConditionCheck" in item for item in items)
    # Control: the same legacy write carries it; without a graph table, today's two members.
    assert (
        "ConditionCheck"
        in pointer_transaction_items("fictional-pointers", _first_legacy(), "fictional-graph")[-1]
    )
    assert len(pointer_transaction_items("fictional-pointers", _first_legacy())) == 2
