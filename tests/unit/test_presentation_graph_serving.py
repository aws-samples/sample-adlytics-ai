# SPDX-License-Identifier: MIT-0
"""Presentation's graph serving rule and its read-only graph-table adapter (S6 §2.9, B4)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError
from dynamodb_reserved_words import guard_reserved_words

from adapters.outbound.dynamodb.presentation_graph import DynamoPresentationGraphReader
from domain.operations.execution_graph_release import serving_generation
from domain.presentation.graph_serving import PageGeneration, normalize_graph_entered_at
from domain.shared_kernel.errors import (
    GRAPH_SERVING_ENTRY_MALFORMED,
    DependencyError,
    InvariantError,
)

pytestmark = [pytest.mark.unit]

GRANT = "1" * 64
TABLE = "adlytics-dev-execution-graph"


@guard_reserved_words
class _Client:
    """A DynamoDB client double: `GetItem` by partition key, recording every read."""

    def __init__(self, items: dict[str, dict[str, Any]], *, fail: bool = False) -> None:
        self.items = items
        self.fail = fail
        self.reads: list[str] = []

    def get_item(self, **arguments: Any) -> dict[str, Any]:
        assert arguments["TableName"] == TABLE
        assert arguments["ConsistentRead"] is True
        pk = arguments["Key"]["pk"]["S"]
        self.reads.append(pk)
        if self.fail:
            raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetItem")
        item = self.items.get(pk)
        return {} if item is None else {"Item": item}


def _mode(mode: str, entered_at: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"mode": {"S": mode}, "revision": {"N": "3"}}
    if entered_at is not None:
        item["graph_entered_at"] = {"S": entered_at}
    return item


def _reader(client: _Client) -> DynamoPresentationGraphReader:
    return DynamoPresentationGraphReader(TABLE, cast("Any", client))


@pytest.mark.parametrize(
    ("stored", "normalized"),
    [
        ("2026-10-09T21:06:30.123456+00:00", "2026-10-09T21:06:31Z"),
        ("2026-10-09T21:06:30.000001+00:00", "2026-10-09T21:06:31Z"),
        ("2026-10-09T23:06:30.5+02:00", "2026-10-09T21:06:31Z"),
        ("2026-12-31T23:59:59.9+00:00", "2027-01-01T00:00:00Z"),
        ("2026-10-09T21:06:30+00:00", "2026-10-09T21:06:30Z"),
        ("2026-10-09T21:06:30Z", "2026-10-09T21:06:30Z"),
    ],
)
def test_graph_entered_at_normalizes_to_whole_seconds_rounding_up(
    stored: str, normalized: str
) -> None:
    assert normalize_graph_entered_at(stored) == normalized


@pytest.mark.parametrize("stored", ["2026-10-09T21:06:30", "not-an-instant", "", None, 1760044000])
def test_an_unnormalizable_graph_entered_at_is_an_invariant_error(stored: object) -> None:
    with pytest.raises(InvariantError) as refused:
        normalize_graph_entered_at(stored)

    assert refused.value.code == GRAPH_SERVING_ENTRY_MALFORMED
    assert str(refused.value.code) == "ADLY-GRAPH_SERVING-0002"
    # Control: the same instant with its offset normalizes.
    assert normalize_graph_entered_at("2026-10-09T21:06:30+00:00") == "2026-10-09T21:06:30Z"


@pytest.mark.parametrize(
    "fields",
    [
        {"built_at": "2026-08-20T06:12:04Z", "built_before": "2026-08-20T06:12:04Z"},
        {},
        {"built_at": "2026-08-20T06:12:04.5Z"},
        {"built_before": "2026-08-20"},
    ],
)
def test_a_page_generation_is_exactly_one_whole_second_instant(fields: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="page generation"):
        PageGeneration(**fields)
    # Control: each single valid form is accepted.
    assert PageGeneration(built_at="2026-08-20T06:12:04Z").built_at == "2026-08-20T06:12:04Z"
    assert PageGeneration(built_before="2026-08-20T06:12:04Z").built_at is None


def test_the_graph_entry_is_read_once_per_container_once_it_is_set() -> None:
    client = _Client({"MODE#ingestion": _mode("GRAPH", "2026-10-09T21:06:30.2+00:00")})
    reader = _reader(client)

    assert reader.graph_entered_at() == "2026-10-09T21:06:31Z"
    assert reader.graph_entered_at() == "2026-10-09T21:06:31Z"
    assert client.reads == ["MODE#ingestion"]


def test_control_outside_graph_the_mode_item_is_read_again_until_the_cas() -> None:
    client = _Client({"MODE#ingestion": _mode("LEGACY_DRAINING")})
    reader = _reader(client)

    assert reader.graph_entered_at() is None
    client.items["MODE#ingestion"] = _mode("GRAPH", "2026-10-09T21:06:30+00:00")
    assert reader.graph_entered_at() == "2026-10-09T21:06:30Z"
    assert client.reads == ["MODE#ingestion", "MODE#ingestion"]


def test_an_absent_mode_item_reads_legacy_with_no_entry() -> None:
    assert _reader(_Client({})).graph_entered_at() is None


@pytest.mark.parametrize(
    "item",
    [_mode("GRAPH"), _mode("GRAPH", "yesterday"), _mode("NOT_A_MODE")],
)
def test_a_graph_mode_item_without_a_normalizable_entry_is_an_invariant_error(
    item: dict[str, Any],
) -> None:
    with pytest.raises(InvariantError) as refused:
        _reader(_Client({"MODE#ingestion": item})).graph_entered_at()

    assert refused.value.code == GRAPH_SERVING_ENTRY_MALFORMED
    # Control: a GRAPH item with its entry instant reads.
    control = _Client({"MODE#ingestion": _mode("GRAPH", "2026-10-09T21:06:30+00:00")})
    assert _reader(control).graph_entered_at() == "2026-10-09T21:06:30Z"


def test_a_graph_read_failure_is_a_dependency_error() -> None:
    reader = _reader(_Client({}, fail=True))

    with pytest.raises(DependencyError) as failed:
        reader.serving_binding(GRANT)
    assert str(failed.value.code) == "ADLY-EXEC_GRAPH-0020"
    with pytest.raises(DependencyError):
        reader.graph_entered_at()
    # Control: the same reads succeed against a reachable table.
    reachable = _reader(_Client({}))
    assert reachable.serving_binding(GRANT) is None
    assert reachable.graph_entered_at() is None


def test_serving_binding_reads_the_stored_binding_by_grant() -> None:
    bound_at = datetime(2026, 8, 20, 6, 12, 9, tzinfo=UTC)
    client = _Client(
        {
            f"SERVING#{GRANT}": {
                "pk": {"S": f"SERVING#{GRANT}"},
                "sk": {"S": "BINDING"},
                "item_kind": {"S": "serving_binding"},
                "binding_id": {"S": "b" * 64},
                "conflict_unit": {"S": "u" * 64},
                "served_revision": {"S": bound_at.isoformat()},
                "contributor_roots": {"L": [{"S": "root-flow"}]},
                "bound_at": {"S": bound_at.isoformat()},
                "artifact_revision": {"S": serving_generation(GRANT)},
                "unavailable": {"L": []},
            }
        }
    )

    binding = _reader(client).serving_binding(GRANT)

    assert binding is not None
    assert binding.grant_id == GRANT
    assert binding.artifact_revision == serving_generation(GRANT)
    assert binding.contributor_roots == ("root-flow",)
    assert client.reads == [f"SERVING#{GRANT}"]
    # Control: another grant's cohort is still open, so it has no binding.
    assert _reader(client).serving_binding("2" * 64) is None
