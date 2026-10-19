# SPDX-License-Identifier: MIT-0
"""Pack 4, `amc-dsp-reach`: DSP reach per campaign-day, loaded and covered (plan D6, ADR-109).

The pack and contract are held by the producer and contract gates; the planner gate by
`test_planning.py`, chat by `test_measurement_tool.py`, binding by `test_publication_selection.py`
and the fixture by `test_fixture_conformance.py`. This file runs the generated per-day
deliveries through the real ingestion path: a clean day publishes with its pack version on the
publication evidence, which is the coverage serving reads; the declared suppressed day is
refused whole under `reject_delivery`.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest
from dynamodb_reserved_words import guard_reserved_words
from test_semantic_sidecar import (
    MemoryCanonical,
    MemoryPublications,
    _case,  # pyright: ignore[reportPrivateUsage]
)

from adapters.amc_csv import dict_rows, write_rows
from adapters.jobs.contract_validation.loader import load_contracts, load_registry
from adapters.jobs.curation.manifests import (
    BuildManifestRequest,
    build_manifest,
    checksum_of,
    declared_columns_of,
)
from adapters.jobs.fixture_generation.catalog import load_synthetic_entity_catalog
from adapters.jobs.ingest_delivery.landing import LoadedDelivery
from adapters.jobs.ingest_delivery.semantic_ingestion import (
    SemanticIngestionRequest,
    ingest_semantic_delivery,
)
from adapters.jobs.ingest_delivery.semantic_sidecar import (
    SemanticSidecarOutcome,
    SemanticSidecarTooling,
)
from adapters.jobs.land_fixtures.bundle import LandedDelivery, plan_landings
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.dynamodb import semantic_state
from adapters.outbound.dynamodb.semantic_state import (
    EARLIEST_SCOPE_PAGE,
    SCOPE_CURRENT_INDEX,
    DynamoCurrentPublications,
)
from adapters.outbound.publication_selection import PublicationSelector
from adapters.outbound.source_pack import load_source_packs
from adapters.producer.packs import installed_governance, load_query_packs
from domain.curation.landing import GOVERNED_MANIFEST_LAYOUT, LandedObject
from domain.data_contracts.contract import ContractError, DataContract
from domain.measurement.pack_coverage import PackDayVersions, pack_version_from_evidence
from domain.semantics.publication import PublicationId, SuccessfulPublication
from domain.semantics.source_aligned import LandedDeliveryRef
from domain.shared_kernel.values import DateRange
from ports.outbound.canonical import CanonicalDestination
from ports.outbound.pack_coverage import PackCoverageReadRequest

if TYPE_CHECKING:
    from types_boto3_dynamodb.client import DynamoDBClient

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-109")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURE = "generated/amc-dsp-reach.csv"
CONTRACT = "amc-dsp-reach"
NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)
US = {"marketplace_key": "amazon_us"}


@pytest.fixture(scope="module")
def contract() -> DataContract:
    return next(item for _, item in load_contracts(ROOT) if item.contract_id == CONTRACT)


@pytest.fixture(scope="module")
def landed() -> tuple[LandedDelivery, ...]:
    return tuple(item for item in plan_landings(ROOT) if item.fixture.partition("#")[0] == FIXTURE)


def _loaded(planned: LandedDelivery) -> LoadedDelivery:
    manifest = planned.manifest
    return LoadedDelivery(
        manifest=manifest,
        manifest_key=manifest.manifest_key,
        text=planned.data[0].body.decode(),
        landed_ref=LandedDeliveryRef(
            manifest.manifest_key,
            "manifest-v1",
            "sha256:manifest",
            manifest.object_keys()[0],
            "object-v1",
            manifest.objects[0].checksum,
        ),
    )


@dataclass
class _Load:
    outcome: SemanticSidecarOutcome
    versions: tuple[int | None, ...]
    reach: tuple[object, ...]
    tooling: SemanticSidecarTooling


def _load(delivery: LoadedDelivery, contract: DataContract) -> _Load:
    _request, tooling, _order, _attempts = _case()
    (result,) = ingest_semantic_delivery(
        SemanticIngestionRequest(
            delivery=delivery,
            contracts=(contract,),
            columns=load_registry(ROOT),
            now=NOW,
            operation_id="dsp-reach-unit",
            execution_id="dsp-reach-execution",
            plan_digest="sha256:dsp-reach-plan",
            installed_governance=installed_governance(load_query_packs(ROOT)),
        ),
        tooling,
    )
    canonical = tooling.canonical_writer
    assert isinstance(canonical, MemoryCanonical)
    candidates = tuple(
        item
        for item in canonical.requests
        if item.destination is CanonicalDestination.CANDIDATE
        and item.publication.partition.scope.dataset.value == "fct_ad_exposure"
    )
    return _Load(
        outcome=result.outcome,
        versions=tuple(
            pack_version_from_evidence(item.publication.evidence, CONTRACT) for item in candidates
        ),
        reach=tuple(dict(row).get("reach_users") for item in candidates for row in item.rows),
        tooling=tooling,
    )


def test_the_bundle_lands_one_delivery_per_clean_day_and_never_the_refused_one(
    landed: tuple[LandedDelivery, ...],
) -> None:
    reach = load_synthetic_entity_catalog(ROOT).shape.dsp_reach
    clean = tuple(day for day in reach.days if day != reach.suppressed_day)
    assert tuple((item.manifest.period_start, item.manifest.period_end) for item in landed) == (
        tuple((day, day) for day in clean)
    )
    for item in landed:
        _header, rows = dict_rows(item.data[0].body.decode())
        assert {row["impression_date_utc"] for row in rows} == {
            item.manifest.period_start.isoformat()
        }
        assert {row["rows_suppressed"] for row in rows} == {"false"}


def test_a_clean_day_publishes_and_records_the_coverage_serving_reads(
    landed: tuple[LandedDelivery, ...], contract: DataContract
) -> None:
    first = landed[0]
    day = first.manifest.period_start
    loaded = _load(_loaded(first), contract)
    assert loaded.outcome is SemanticSidecarOutcome.PUBLISHED
    assert set(loaded.versions) == {1}
    _header, rows = dict_rows(first.data[0].body.decode())
    assert sorted(loaded.reach, key=str) == sorted(
        (int(row["reach_users"]) for row in rows), key=str
    )
    selector = PublicationSelector(
        loaded.tooling.publications, load_source_packs(ROOT), load_commerce_identity_bridge(ROOT)
    )
    (covered,) = selector.pack_coverage(
        PackCoverageReadRequest((contract,), US, (DateRange(day, day),))
    )
    assert covered == PackDayVersions(CONTRACT, {day: 1})
    # Control: a day before pack 4 has no publication, so it reads None and the gate refuses it,
    # naming the writer's first published day. A covered read (above) doesn't look it up.
    earlier = date(2026, 9, 14)
    (uncovered,) = selector.pack_coverage(
        PackCoverageReadRequest((contract,), US, (DateRange(earlier, earlier),))
    )
    assert uncovered == PackDayVersions(CONTRACT, {earlier: None}, first_published=day)


def test_the_declared_suppressed_day_is_refused_whole(
    landed: tuple[LandedDelivery, ...], contract: DataContract
) -> None:
    """The generated corpus's refused day, landed on purpose as the induced live probe would."""
    reach = load_synthetic_entity_catalog(ROOT).shape.dsp_reach
    header, parsed = dict_rows((ROOT / "tests/fixtures" / FIXTURE).read_text())
    rows = cast("tuple[dict[str, str], ...]", parsed)
    window = f"{reach.suppressed_day.isoformat()}T00:00:00.000Z"
    refused_rows = tuple(row for row in rows if row["time_window_start"] == window)
    assert sum(row["rows_suppressed"] == "true" for row in refused_rows) == 1
    text = write_rows(header or (), refused_rows)
    body = text.encode()
    declaration = json.loads(
        (ROOT / "tests/fixtures/generated/amc-dsp-reach.manifest.json").read_text()
    )
    day = reach.suppressed_day.isoformat()
    source_watermark = landed[0].manifest.source_watermark
    assert source_watermark is not None
    watermark = source_watermark.isoformat()
    manifest = build_manifest(
        BuildManifestRequest(
            declaration={
                **declaration,
                "period_start": day,
                "period_end": day,
                "source_watermark": watermark,
                "retrieved_at": "2026-10-06T00:00:00Z",
            },
            objects=(LandedObject("data-0001.csv", len(body), checksum_of(body)),),
            declared_columns=declared_columns_of(text, source_format="csv"),
        )
    )
    manifest = replace(
        manifest, governance=landed[0].manifest.governance, layout_version=GOVERNED_MANIFEST_LAYOUT
    )
    probe = LoadedDelivery(
        manifest=manifest,
        manifest_key=manifest.manifest_key,
        text=text,
        landed_ref=LandedDeliveryRef(
            manifest.manifest_key,
            "manifest-v1",
            "sha256:manifest",
            manifest.object_keys()[0],
            "object-v1",
            manifest.objects[0].checksum,
        ),
    )
    loaded = _load(probe, contract)
    assert loaded.outcome is SemanticSidecarOutcome.BLOCKED
    # Nothing is CURRENT for the day, so the planner's gate keeps refusing it.
    selector = PublicationSelector(
        loaded.tooling.publications, load_source_packs(ROOT), load_commerce_identity_bridge(ROOT)
    )
    refused_day = DateRange(reach.suppressed_day, reach.suppressed_day)
    assert selector.pack_coverage(PackCoverageReadRequest((contract,), US, (refused_day,))) == (
        PackDayVersions(CONTRACT, {reach.suppressed_day: None}, first_published=None),
    )
    # Positive control: the same day without its suppressed row publishes.
    kept = tuple(row for row in refused_rows if row["rows_suppressed"] == "false")
    kept_text = write_rows(header or (), kept)
    kept_body = kept_text.encode()
    kept_manifest = replace(
        build_manifest(
            BuildManifestRequest(
                declaration={
                    **declaration,
                    "period_start": day,
                    "period_end": day,
                    "source_watermark": watermark,
                    "retrieved_at": "2026-10-06T00:00:00Z",
                },
                objects=(LandedObject("data-0001.csv", len(kept_body), checksum_of(kept_body)),),
                declared_columns=declared_columns_of(kept_text, source_format="csv"),
            )
        ),
        governance=landed[0].manifest.governance,
        layout_version=GOVERNED_MANIFEST_LAYOUT,
    )
    published = _load(
        LoadedDelivery(
            manifest=kept_manifest,
            manifest_key=kept_manifest.manifest_key,
            text=kept_text,
            landed_ref=LandedDeliveryRef(
                kept_manifest.manifest_key,
                "manifest-v1",
                "sha256:manifest",
                kept_manifest.object_keys()[0],
                "object-v1",
                kept_manifest.objects[0].checksum,
            ),
        ),
        contract,
    )
    assert published.outcome is SemanticSidecarOutcome.PUBLISHED


@guard_reserved_words
@dataclass
class _Dynamo:
    """A DynamoDB client holding CURRENT and immutable publication items, counting every read.

    It answers the `scope-current` index in `bucket_start` order with `Limit` and
    `ExclusiveStartKey`, as the service does, and transactional gets by exact key.
    """

    items: dict[tuple[str, str], dict[str, object]]
    queries: list[dict[str, object]]
    read_items: int = 0

    def query(self, **request: object) -> dict[str, object]:
        self.queries.append(request)
        assert request["IndexName"] == SCOPE_CURRENT_INDEX
        values = cast("dict[str, dict[str, str]]", request["ExpressionAttributeValues"])

        def order(item: dict[str, object]) -> tuple[str, str]:
            text = cast("dict[str, dict[str, str]]", item)
            return text["bucket_start"]["S"], text["partition_key"]["S"]

        rows = sorted(
            (
                item
                for item in self.items.values()
                if cast("dict[str, dict[str, str]]", item).get("coverage_scope", {}).get("S")
                == values[":scope"]["S"]
            ),
            key=order,
            reverse=request.get("ScanIndexForward") is False,
        )
        start = cast("dict[str, object] | None", request.get("ExclusiveStartKey"))
        if start is not None:
            rows = [row for row in rows if order(row) > order(start)]
        limit = cast("int", request.get("Limit", len(rows)))
        page = rows[:limit]
        response: dict[str, object] = {
            "Items": [{"partition_key": row["partition_key"]} for row in page]
        }
        if len(rows) > limit:
            response["LastEvaluatedKey"] = page[-1]
        return response

    def transact_get_items(self, **request: object) -> dict[str, object]:
        members = cast(
            "list[dict[str, dict[str, dict[str, dict[str, str]]]]]", request["TransactItems"]
        )
        self.read_items += len(members)
        keys = tuple(
            (member["Get"]["Key"]["partition_key"]["S"], member["Get"]["Key"]["record_key"]["S"])
            for member in members
        )
        return {
            "Responses": [{"Item": self.items[key]} if key in self.items else {} for key in keys]
        }


def _dynamo_history(published: SuccessfulPublication, days: int, unserved: int = 0) -> _Dynamo:
    """Store the published day plus `days - 1` later CURRENT days as DynamoDB items.

    The first `unserved` days' CURRENT pointers carry the servable NONE sentinel, as a
    projection backlog leaves them; every later CURRENT pointer serves its own publication,
    since a serving read refuses a CURRENT without a servable projection pointer.
    """
    items: dict[tuple[str, str], dict[str, object]] = {}
    for offset in range(days):
        day = published.partition.bucket.window.start + timedelta(days=offset)
        bucket = replace(published.partition.bucket, window=DateRange(day, day))
        partition = replace(published.partition, bucket=bucket)
        subject = published.evidence.subject
        assert subject is not None
        publication = replace(
            published,
            partition=partition,
            publication_id=PublicationId(f"{published.publication_id.value}-{offset}"),
            evidence=replace(
                published.evidence, subject=replace(subject, partition=partition.identity)
            ),
        )
        for current in (True, False):
            item = cast(
                "dict[str, object]",
                semantic_state._publication_item(  # pyright: ignore[reportPrivateUsage]
                    publication, current=current
                ),
            )
            if current:
                served = offset >= unserved
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
            record = cast("dict[str, dict[str, str]]", item)["record_key"]["S"]
            items[(publication.partition.identity, record)] = item
    return _Dynamo(items, [])


def test_the_first_published_day_is_read_without_walking_the_history(
    landed: tuple[LandedDelivery, ...], contract: DataContract
) -> None:
    """Review B2-1: an uncovered day costs the same reads over 10 or 400 published days."""
    loaded = _load(_loaded(landed[0]), contract)
    publications = loaded.tooling.publications
    assert isinstance(publications, MemoryPublications)
    (published,) = (
        item
        for item in publications.current_items.values()
        if item.partition.scope.dataset.value == "fct_ad_exposure"
    )
    first = published.partition.bucket.window.start
    earlier = date(2026, 9, 14)
    reads: list[tuple[int, int]] = []
    for days in (10, 400):
        dynamo = _dynamo_history(published, days)
        selector = PublicationSelector(
            DynamoCurrentPublications(
                "publications", cast("DynamoDBClient", cast("object", dynamo))
            ),
            load_source_packs(ROOT),
            load_commerce_identity_bridge(ROOT),
        )
        assert selector.pack_coverage(
            PackCoverageReadRequest((contract,), US, (DateRange(earlier, earlier),))
        ) == (PackDayVersions(CONTRACT, {earlier: None}, first_published=first),)
        assert [(query["Limit"], query["ScanIndexForward"]) for query in dynamo.queries] == [
            (EARLIEST_SCOPE_PAGE, True)
        ]
        reads.append((len(dynamo.queries), dynamo.read_items))
        # Positive control: a covered day plans and never looks the first day up.
        covered = _Dynamo(dynamo.items, [])
        control = PublicationSelector(
            DynamoCurrentPublications(
                "publications", cast("DynamoDBClient", cast("object", covered))
            ),
            load_source_packs(ROOT),
            load_commerce_identity_bridge(ROOT),
        )
        assert control.pack_coverage(
            PackCoverageReadRequest((contract,), US, (DateRange(first, first),))
        ) == (PackDayVersions(CONTRACT, {first: 1}),)
        assert covered.queries == []
    assert reads[0] == reads[1]


def _selector(dynamo: _Dynamo) -> PublicationSelector:
    return PublicationSelector(
        DynamoCurrentPublications("publications", cast("DynamoDBClient", cast("object", dynamo))),
        load_source_packs(ROOT),
        load_commerce_identity_bridge(ROOT),
    )


def test_the_first_day_lookup_has_a_hard_bound_over_unserved_buckets(
    landed: tuple[LandedDelivery, ...], contract: DataContract
) -> None:
    """Review B3-1: leading servable-NONE pointers never page the lookup into the history.

    A servable bucket costs four item reads (the CURRENT pointer, then the strong CURRENT and
    immutable pair after a discovery read); a NONE bucket costs one.
    """
    loaded = _load(_loaded(landed[0]), contract)
    publications = loaded.tooling.publications
    assert isinstance(publications, MemoryPublications)
    (published,) = (
        item
        for item in publications.current_items.values()
        if item.partition.scope.dataset.value == "fct_ad_exposure"
    )
    first = published.partition.bucket.window.start
    earlier = date(2026, 9, 14)
    uncovered = PackCoverageReadRequest((contract,), US, (DateRange(earlier, earlier),))
    one_page = [(EARLIEST_SCOPE_PAGE, True)]
    # The day's own exact inspection, read over an empty table where the lookup finds no key.
    empty = _Dynamo({}, [])
    assert _selector(empty).pack_coverage(uncovered) == (
        PackDayVersions(CONTRACT, {earlier: None}, first_published=None),
    )
    inspection = empty.read_items

    # Three unserved buckets inside the page: the fourth bucket is named, still one query.
    within = _dynamo_history(published, 40, unserved=3)
    assert _selector(within).pack_coverage(uncovered) == (
        PackDayVersions(CONTRACT, {earlier: None}, first_published=first + timedelta(days=3)),
    )
    assert [(query["Limit"], query["ScanIndexForward"]) for query in within.queries] == one_page
    assert "ExclusiveStartKey" not in within.queries[0]
    assert within.read_items == inspection + 3 + 4

    # More than two pages of unserved buckets: one query, one read per examined bucket, and
    # no day is named (fail closed) although day 21 is servable.
    backlog = _dynamo_history(published, 40, unserved=2 * EARLIEST_SCOPE_PAGE + 4)
    assert _selector(backlog).pack_coverage(uncovered) == (
        PackDayVersions(CONTRACT, {earlier: None}, first_published=None),
    )
    assert [(query["Limit"], query["ScanIndexForward"]) for query in backlog.queries] == one_page
    assert "ExclusiveStartKey" not in backlog.queries[0]
    assert backlog.read_items == inspection + EARLIEST_SCOPE_PAGE

    # Positive control: a covered day over the same backlog plans and never looks the day up.
    served = first + timedelta(days=2 * EARLIEST_SCOPE_PAGE + 4)
    control = _Dynamo(backlog.items, [])
    assert _selector(control).pack_coverage(
        PackCoverageReadRequest((contract,), US, (DateRange(served, served),))
    ) == (PackDayVersions(CONTRACT, {served: 1}),)
    assert control.queries == []


def test_served_where_published_needs_a_non_additive_column_and_a_bounded_grain(
    contract: DataContract,
) -> None:
    reach = contract.field_for("reach_users")
    assert reach is not None and reach.served_where_published
    assert contract.publication_gated_fields == (reach,)
    assert not contract.contributes_to_coverage
    with pytest.raises(ContractError, match="only a non_additive column"):
        replace(reach, non_additive=False)
    unbounded = tuple(
        replace(field, literal_value=None, source_path="campaign_country")
        if field.target_column in {"channel_key", "ad_product_key"}
        else field
        for field in contract.fields
    )
    with pytest.raises(ContractError, match="bounds"):
        replace(contract, fields=unbounded)
    # Control: every other writer of the fact contributes to its coverage.
    others = tuple(
        item
        for _, item in load_contracts(ROOT)
        if item.target_table == "fct_ad_exposure" and item.contract_id != CONTRACT
    )
    assert len(others) == 4
    assert all(item.contributes_to_coverage for item in others)
