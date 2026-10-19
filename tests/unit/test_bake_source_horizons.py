# SPDX-License-Identifier: MIT-0
"""Plan A5.1 I9: every whole-scope CURRENT read a bake pins is horizon-bounded.

Covers the bounded read (the `scope-current` index query), the bake reader's RECORD refusal and
horizon-keyed replay, the run-pinned row horizon, the pin's pre-write horizon and byte guard,
and Retention's first quarterly vintage read from the bounded earliest read. Each passing check
has a paired control. Every identity is fictional.
"""

from __future__ import annotations

# The store's pin guard and the fake's publication items are private seams under test.
# pyright: reportPrivateUsage=false
import gzip
import hashlib
import json
import pathlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from dynamodb_reserved_words import guard_reserved_words
from execution_graph_release_support import release_store
from execution_graph_support import FakeDynamo
from publication_support import _PublicationSpec, successful_publication

from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.preset_artifact_publisher.graph_bake import row_horizon
from adapters.jobs.preset_artifact_publisher.retention_source import (
    STANDARD_CONTRACT,
    quarterly_vintage_reader,
)
from adapters.outbound.bake_sources import (
    BakeReadMode,
    BakeRowHorizon,
    BakeSourceReads,
    PinnedRead,
    encode_reads,
    reads_digest,
)
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.dynamodb import semantic_state
from adapters.outbound.dynamodb.semantic_state import (
    EARLIEST_SCOPE_PAGE,
    SCOPE_CURRENT_INDEX,
    DynamoCurrentPublications,
)
from adapters.outbound.entity_catalog import LATEST_ONLY, publication_order
from adapters.outbound.publication_selection import (
    PublicationScopeRequest,
    publication_digest,
    publication_scopes_for,
)
from adapters.outbound.source_pack import load_source_packs
from domain.operations.execution_graph_release import (
    BakeRosterRow,
    BakeTaskSource,
    DurableBakeSourceManifest,
    DurableBakeSourceManifestInvalidError,
)
from domain.semantics.calendar_periods import vintage_ending
from domain.semantics.time import TimeGrain
from domain.shared_kernel.errors import ErrorCode, InvariantError
from ports.outbound.publication_selection import PinnedPublication
from ports.outbound.semantic_state import ScopeHorizon

if TYPE_CHECKING:
    from types_boto3_dynamodb import DynamoDBClient

    from domain.semantics.publication import PublicationScope, SuccessfulPublication
    from ports.outbound.execution_graph_release import BakeSourcePinRequest

pytestmark = [pytest.mark.unit]

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
FIRST_DAY = date(2026, 9, 1)
ROW = BakeRowHorizon(date(2026, 9, 10), 3)
FAN_OUT_FAILED = ErrorCode("GRAPH_BAKE_V3", 10)


# --- the bounded read: the scope-current index query ----------------------------------------


@guard_reserved_words
@dataclass
class _Index:
    """A DynamoDB client over CURRENT and immutable items, answering `scope-current`.

    As the service does: `bucket_start` order, its key condition and `Limit`.
    """

    items: dict[tuple[str, str], dict[str, Any]]
    queries: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def query(self, **request: Any) -> dict[str, Any]:
        self.queries.append(request)
        assert request["IndexName"] == SCOPE_CURRENT_INDEX
        values = request["ExpressionAttributeValues"]
        condition = str(request["KeyConditionExpression"])
        lower = values[":from"]["S"] if "bucket_start >= :from" in condition else ""
        rows = sorted(
            (
                item
                for item in self.items.values()
                if item.get("coverage_scope", {}).get("S") == values[":scope"]["S"]
                and item["bucket_start"]["S"] >= lower
            ),
            key=lambda item: (item["bucket_start"]["S"], item["partition_key"]["S"]),
            reverse=request.get("ScanIndexForward") is False,
        )
        limit = int(request.get("Limit", len(rows)))
        return {"Items": [{"partition_key": row["partition_key"]} for row in rows[:limit]]}

    def transact_get_items(self, **request: Any) -> dict[str, Any]:
        keys = tuple(
            (member["Get"]["Key"]["partition_key"]["S"], member["Get"]["Key"]["record_key"]["S"])
            for member in request["TransactItems"]
        )
        return {
            "Responses": [{"Item": self.items[key]} if key in self.items else {} for key in keys]
        }


def _history(days: int, *, unserved_newest: int = 0) -> tuple[_Index, PublicationScope]:
    """`days` daily CURRENT publications in one scope; the newest `unserved_newest` unprojected."""
    items: dict[tuple[str, str], dict[str, Any]] = {}
    scope: PublicationScope | None = None
    for offset in range(days):
        publication = successful_publication(
            "fct_ad_exposure",
            FIRST_DAY + timedelta(days=offset),
            _PublicationSpec(f"publication-{offset:03d}", NOW),
        )
        scope = publication.partition.scope
        for current in (True, False):
            item = cast(
                "dict[str, Any]", semantic_state._publication_item(publication, current=current)
            )
            if current:
                served = offset < days - unserved_newest
                item |= {
                    "projected_publication_id": {
                        "S": publication.publication_id.value
                        if served
                        else semantic_state.SERVABLE_NONE
                    },
                    "projected_fencing_token": {
                        "N": str(publication.fencing_token if served else 0)
                    },
                    "projected_at": {"S": NOW.isoformat()},
                }
            items[(publication.partition.identity, item["record_key"]["S"])] = item
    assert scope is not None
    return _Index(items), scope


def _reader(index: _Index) -> DynamoCurrentPublications:
    return DynamoCurrentPublications("publications", cast("DynamoDBClient", cast("object", index)))


def _starts(found: tuple[SuccessfulPublication, ...]) -> list[date]:
    return [item.partition.bucket.window.start for item in found]


def test_newest_one_is_the_newest_wins_reduction_in_one_page_at_any_history() -> None:
    """T3: `newest=1` equals `max(current, key=publication_order)`, the reduction F3-F5 make."""
    for days in (2, 85, 400):
        index, scope = _history(days)
        whole = _reader(index).current_for_scope(scope)
        bounded = _reader(index).current_for_scope(scope, LATEST_ONLY)
        assert bounded == (max(whole, key=publication_order),)
        strong = _reader(index)._store.current_for_scope(scope, LATEST_ONLY)
        assert strong == bounded
        # One newest-first page and never a second, whatever the history.
        newest = [query for query in index.queries if query.get("ScanIndexForward") is False]
        assert [query["Limit"] for query in newest] == [EARLIEST_SCOPE_PAGE, 1]
    # Control: an ascending page answers the earliest bucket, which the reduction never picks.
    index, scope = _history(85)
    earliest = _reader(index).earliest_current_for_scope(scope)
    assert earliest is not None
    assert (earliest,) != _reader(index).current_for_scope(scope, LATEST_ONLY)


def test_newest_one_skips_a_short_projection_backlog_like_the_whole_read() -> None:
    """A servable newest-first read answers the latest projected bucket, as `max` over all does."""
    index, scope = _history(20, unserved_newest=3)
    whole = _reader(index).current_for_scope(scope)
    assert _reader(index).current_for_scope(scope, LATEST_ONLY) == (
        max(whole, key=publication_order),
    )
    assert _starts(_reader(index).current_for_scope(scope, LATEST_ONLY)) == [
        FIRST_DAY + timedelta(days=16)
    ]


def test_a_windowed_horizon_reads_from_its_bound_and_never_grows_with_history() -> None:
    """T1 at the read: the bounded answer is identical at 30 and 400 days of history."""
    horizon = ScopeHorizon(from_bucket_start=FIRST_DAY + timedelta(days=20))
    answers: list[list[date]] = []
    for days in (30, 400):
        index, scope = _history(days)
        found = _reader(index).current_for_scope(scope, horizon)
        assert min(_starts(found)) == FIRST_DAY + timedelta(days=20)
        assert all(
            "bucket_start >= :from" in query["KeyConditionExpression"] for query in index.queries
        )
        answers.append(_starts(found)[:10])
    assert answers[0] == answers[1]
    # Control: the unbounded read grows with history.
    assert len(_reader(_history(30)[0]).current_for_scope(_history(30)[1])) == 30
    assert len(_reader(_history(400)[0]).current_for_scope(_history(400)[1])) == 400


def test_a_scope_horizon_declares_exactly_one_bound_and_a_canonical_token() -> None:
    assert ScopeHorizon(newest=1).token == "newest=1"
    assert ScopeHorizon(from_bucket_start=date(2026, 1, 2)).token == "from=2026-01-02"
    for bad in (
        {},
        {"newest": 1, "from_bucket_start": date(2026, 1, 2)},
        {"newest": 0},
        {"newest": True},
    ):
        with pytest.raises(ValueError, match="horizon"):
            ScopeHorizon(**cast("dict[str, Any]", bad))


# --- the bake reader: RECORD refusal and horizon-keyed replay -------------------------------


@dataclass
class _Current:
    """A CURRENT with immutable records; it also serves `select_exact`."""

    publications: tuple[SuccessfulPublication, ...]
    calls: list[ScopeHorizon | None] = field(default_factory=list[ScopeHorizon | None])

    def current_for_scope(
        self, scope: PublicationScope, horizon: ScopeHorizon | None = None
    ) -> tuple[SuccessfulPublication, ...]:
        del scope
        self.calls.append(horizon)
        return self.publications

    def select_exact(self, request: Any) -> tuple[SuccessfulPublication, ...]:
        by_id = {item.publication_id.value: item for item in self.publications}
        return tuple(by_id[pin.publication_id] for pin in request.pins)


def _bake_reader() -> tuple[BakeSourceReads, _Current, PublicationScope]:
    publications = tuple(
        successful_publication(
            "fct_ad_exposure", FIRST_DAY + timedelta(days=day), _PublicationSpec(f"p-{day}", NOW)
        )
        for day in range(3)
    )
    current = _Current(publications)
    return BakeSourceReads(current, cast("Any", current)), current, publications[0].partition.scope


def test_recording_an_unbounded_scope_read_is_refused_before_current() -> None:
    """T6: under RECORD a whole-scope read with no horizon is fatal, and reads nothing."""
    reads, current, scope = _bake_reader()
    with reads.snapshot(), reads.recording(ROW) as recorded:
        with pytest.raises(InvariantError) as refused:
            reads.current_for_scope(scope)
        assert refused.value.code == FAN_OUT_FAILED
        assert current.calls == []
        # Paired: the row's horizon records, and its token is in the pinned key.
        reads.current_for_scope(scope, reads.window_horizon(1))
    (read,) = recorded.reads
    assert read.key == (scope.identity, "from=2026-09-06")
    assert current.calls == [ScopeHorizon(from_bucket_start=date(2026, 9, 6))]
    # Control: outside a bake the same unbounded read is today's answer, unchanged.
    assert reads.mode is BakeReadMode.PASSTHROUGH
    assert reads.window_horizon(1) is None
    assert reads.current_for_scope(scope) == current.publications
    assert current.calls[-1] is None


def test_a_replay_at_another_horizon_is_the_manifest_refusal_and_a_stage1_key_replays() -> None:
    """T9: the horizon token is part of the key; a stage-1 one-element key replays unbounded."""
    reads, current, scope = _bake_reader()
    with reads.snapshot(), reads.recording(ROW) as handle:
        pinned = reads.current_for_scope(scope, ROW.window(1))
    recorded = handle.reads
    with reads.replaying(recorded, ROW):
        assert reads.current_for_scope(scope, ROW.window(1)) == pinned
    later = BakeRowHorizon(ROW.anchor + timedelta(days=1), ROW.days)
    with (
        reads.replaying(recorded, later),
        pytest.raises(DurableBakeSourceManifestInvalidError),
    ):
        reads.current_for_scope(scope, later.window(1))
    # A stage-1 pin: one-element key, read with no horizon in its replay.
    legacy = (
        PinnedRead(
            "current_for_scope",
            (scope.identity,),
            tuple(
                PinnedPublication(
                    item.partition.identity,
                    item.publication_id.value,
                    item.fencing_token,
                    publication_digest(item),
                )
                for item in current.publications
            ),
        ),
    )
    with reads.replaying(legacy, ROW):
        assert reads.current_for_scope(scope) == current.publications
    # Control: a stage-1 key read at a horizon is refused, not answered from a guess.
    with reads.replaying(legacy, ROW), pytest.raises(DurableBakeSourceManifestInvalidError):
        reads.current_for_scope(scope, ROW.window(1))


def test_the_row_horizon_is_the_run_pinned_compute_start_and_never_a_wall_clock() -> None:
    """T10: every attempt derives the same token from `compute_started_at`."""
    row = BakeRosterRow("campaigns", "7d", 240, 16.6, 123, 26)
    tokens = {
        row_horizon(cast("Any", SimpleNamespace(compute_started_at=started)), row).window(1).token
        for started in ("2026-10-14T23:59:59+00:00", "2026-10-14T19:59:59-04:00")
    }
    assert tokens == {"from=2026-06-12"}
    # Control: a run with no (or a naive) compute start is refused, never read from a clock.
    for started in (None, "2026-10-14T23:59:59", "not an instant"):
        with pytest.raises(InvariantError) as refused:
            row_horizon(cast("Any", SimpleNamespace(compute_started_at=started)), row)
        assert refused.value.code == FAN_OUT_FAILED


def test_a_roster_row_without_a_positive_integer_horizon_is_refused() -> None:
    for days in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="BAKE_SOURCE_HORIZON_DAYS_INVALID"):
            BakeRosterRow("campaigns", "7d", 240, 16.6, cast("int", days), 26)
    assert BakeRosterRow("campaigns", "7d", 240, 16.6, 1, 26).source_horizon_days == 1


# --- the pin's pre-write guard ---------------------------------------------------------------

_RUN = hashlib.sha256(b"fictional-horizon-guard-run").hexdigest()


def _blob(key: tuple[str, ...], pins: int = 1, *, padding: str = "") -> bytes:
    read = PinnedRead(
        "current_for_scope",
        key,
        tuple(
            PinnedPublication(
                f"scope|day|{index}{padding}",
                f"publication-{hashlib.sha256(f'{index}{padding}'.encode()).hexdigest()[:32]}",
                1,
                hashlib.sha256(f"{index}{padding}".encode()).hexdigest(),
            )
            for index in range(pins)
        ),
    )
    return encode_reads((read,))


def _pin_request(blobs: tuple[bytes, ...]) -> BakeSourcePinRequest:
    tasks = tuple(
        BakeTaskSource(
            "campaigns",
            f"{index + 1}d",
            reads_digest(
                tuple(
                    PinnedRead.from_json(item)
                    for item in cast("list[object]", json.loads(gzip.decompress(blob)))
                )
            ),
            (hashlib.sha256(f"q{index}".encode()).hexdigest(),),
            1,
        )
        for index, blob in enumerate(blobs)
    )
    manifest = DurableBakeSourceManifest(_RUN, "grant-fictional", f"sha256:{'0' * 64}", tasks)
    return cast(
        "BakeSourcePinRequest",
        SimpleNamespace(
            run=SimpleNamespace(run_id=_RUN),
            claim=SimpleNamespace(node_id=f"{_RUN}:bake", lease_version=1),
            head=SimpleNamespace(
                domain="presentation-serving", version=1, serving_input_revision=""
            ),
            manifest=manifest,
            reads=blobs,
            recorded_at=NOW.isoformat(),
        ),
    )


def _refusal(blobs: tuple[bytes, ...]) -> InvariantError:
    tables = FakeDynamo()
    with pytest.raises(InvariantError) as refused:
        release_store(tables).pin_durable_bake_sources(_pin_request(blobs))
    assert tables.transactions == []
    assert refused.value.code == FAN_OUT_FAILED
    return refused.value


def test_a_pin_holding_an_unbounded_scope_read_is_refused_with_zero_writes() -> None:
    """T7: a one-element `current_for_scope` key never reaches DynamoDB."""
    refused = _refusal((_blob(("scope-a", "newest=1")), _blob(("scope-b",))))
    assert refused.context["signal"] == "bake_source_pin_unbounded_scope_read"
    assert (refused.context["variant"], refused.context["scope"]) == ("2d", "scope-b")
    for token in ("newest=0", "from=2026-1-2", "window=7"):
        assert _refusal((_blob(("scope-a", token)),)).context["signal"] == (
            "bake_source_pin_unbounded_scope_read"
        )


def test_a_bounded_pin_passes_the_guard_and_the_guard_is_what_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paired accepted case and control: removing the guard lets the unbounded pin commit."""
    committed: list[int] = []
    store = release_store(FakeDynamo())

    def commit(members: list[object]) -> None:
        committed.append(len(members))

    monkeypatch.setattr(store, "_commit", commit)
    store.pin_durable_bake_sources(
        _pin_request((_blob(("scope-a", "newest=1")), _blob(("scope-b", "from=2026-06-13"))))
    )
    assert committed == [6]
    monkeypatch.setattr(type(store), "_guard_bake_pin", staticmethod(lambda *_args: None))
    store.pin_durable_bake_sources(_pin_request((_blob(("scope-b",)),)))
    assert committed == [6, 5]


def test_an_over_envelope_pin_is_refused_with_zero_writes() -> None:
    """A5.1 Test 8a: an item over 400,000 or an aggregate over 4,000,000 bytes is refused."""
    # One row: high-entropy pins push its `BAKE_SOURCE#` item over the item ceiling.
    item = _refusal((_blob(("scope-a", "newest=1"), pins=7_000, padding="x"),))
    assert item.context["signal"] == "bake_source_pin_over_byte_envelope"
    assert cast("int", item.context["largest_item_bytes"]) > 400_000
    assert item.context["largest_item_sk"] == "BAKE_SOURCE#campaigns#1d"
    # Twelve rows each under the item ceiling but over the aggregate together.
    rows = tuple(
        _blob(("scope-a", "newest=1"), pins=6_000, padding=f"r{index}") for index in range(12)
    )
    aggregate = _refusal(rows)
    assert cast("int", aggregate.context["largest_item_bytes"]) <= 400_000
    assert cast("int", aggregate.context["aggregate_bytes"]) > 4_000_000


# --- Retention's first quarterly vintage -----------------------------------------------------


def test_the_first_quarterly_vintage_is_the_quarterly_series_earliest_publication() -> None:
    """T5: read from the quarterly series' own scope by the bounded earliest read."""
    contracts = {contract.contract_id: contract for _, contract in load_contracts(_ROOT)}
    packs, bridge = load_source_packs(_ROOT), load_commerce_identity_bridge(_ROOT)
    contract = contracts[STANDARD_CONTRACT]
    pack = next(item for item in packs if item.contract_id == STANDARD_CONTRACT)
    quarterly, base = (
        publication_scopes_for(
            PublicationScopeRequest(
                pack,
                contract,
                bridge,
                {"marketplace_key": "amazon_us"},
                period_grain=TimeGrain.CUSTOM,
                variant=variant,
            )
        )[0]
        for variant in ("quarterly", None)
    )
    earliest_vintage = vintage_ending(date(2025, 8, 1)).window
    asked: list[str] = []

    def earliest(scope: PublicationScope) -> SuccessfulPublication | None:
        asked.append(scope.identity)
        if scope.identity != quarterly.identity:
            return None
        # Only the bucket window is read: the vintage's 13 months ending in August 2025.
        bucket = SimpleNamespace(window=earliest_vintage)
        return cast(
            "SuccessfulPublication", SimpleNamespace(partition=SimpleNamespace(bucket=bucket))
        )

    reader = quarterly_vintage_reader(
        cast("Any", SimpleNamespace(earliest_current_for_scope=earliest)), packs, bridge, contract
    )
    assert reader("amazon_us") == date(2025, 8, 1)
    # Control: the base (monthly) series is never consulted, so it can't name the vintage.
    assert asked == [quarterly.identity]
    assert base.identity not in asked


_ROOT = pathlib.Path(__file__).resolve().parents[2]
