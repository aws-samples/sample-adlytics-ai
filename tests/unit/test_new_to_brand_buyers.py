# SPDX-License-Identifier: MIT-0
"""Pack 1: new-to-brand buyers at platform grain, for complete weeks and months (FEAT-003).

`amc-new-to-brand-buyers-platform` is the first shopper-aggregate pack (ADR-099, ADR-100): exact
distinct counts per complete ISO week or calendar month, loaded under `discard_suppressed_rows`,
with one expected platform cell per delivery. Built against the real pack, contract, source pack,
dictionary and generated fixture; every delivery is the generated synthetic corpus or a stated
edit of it. Each refusal asserts its exact code and the layer that raised it, beside a positive
control. Nothing here is live: every row of `FEAT-003-live-acceptance.md` stays PENDING.
"""

from __future__ import annotations

import csv
import inspect
import io
import json
import pathlib
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast
from unittest.mock import patch

import pytest
from publication_support import StubPublications
from test_catalog_tool import _context  # pyright: ignore[reportPrivateUsage]
from test_ingestion_handler import (
    _configure as _configure_ingestion,  # pyright: ignore[reportPrivateUsage]
)
from test_producer_finalizer import BUCKET as FINALIZER_BUCKET
from test_producer_finalizer import (
    _event as _finalizer_event,  # pyright: ignore[reportPrivateUsage]
)
from test_producer_finalizer import (
    _finalizer,  # pyright: ignore[reportPrivateUsage]
)
from test_producer_finalizer import (
    _Landing as _FinalizerLanding,  # pyright: ignore[reportPrivateUsage]
)
from test_producer_finalizer import (
    _Statuses as _FinalizerStatuses,  # pyright: ignore[reportPrivateUsage]
)
from test_publication_selection import (
    RecordingCurrent,
    _success,  # pyright: ignore[reportPrivateUsage]
)
from test_semantic_sidecar import (
    MemoryCanonical,
    MemoryPublications,
    _case,  # pyright: ignore[reportPrivateUsage]
)

from adapters.inbound.tool_handlers.catalog import ListMetricsRequest, list_metrics
from adapters.inbound.tool_handlers.measurement import (
    MeasurementTool,
    MeasurementTooling,
    MeasurementToolRequest,
    handle,
)
from adapters.jobs.contract_validation.loader import load_contracts, load_registry
from adapters.jobs.curation.manifests import (
    BuildManifestRequest,
    build_manifest,
    checksum_of,
    declared_columns_of,
    write_manifest,
)
from adapters.jobs.curation.readers import DeliveredFile, read
from adapters.jobs.ingest_delivery import semantic_sidecar as sidecar_module
from adapters.jobs.ingest_delivery.landing import LoadedDelivery
from adapters.jobs.ingest_delivery.semantic_ingestion import (
    SemanticIngestionRequest,
    ingest_semantic_delivery,
)
from adapters.jobs.ingest_delivery.semantic_sidecar import SemanticSidecarOutcome
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.publication_selection import (
    PublicationScopeRequest,
    PublicationSelector,
    publication_scopes_for,
)
from adapters.outbound.source_pack import load_source_packs
from adapters.outbound.telemetry.reporting import Report
from adapters.producer.packs import discard_policies, load_query_packs, pack_by_id
from adapters.producer.registrations import write_registration
from adapters.producer.results import parse_csv_result
from domain.catalog.capability import GRAIN_NOT_SUPPORTED_CODE, Catalog
from domain.curation.assertions import AssertionName, LoadOutcome
from domain.curation.ingestion import (
    AssessmentRequest,
    ContractLoad,
    DiscardPolicy,
    assess_source_delivery,
)
from domain.curation.landing import GOVERNED_MANIFEST_LAYOUT, LandedObject, partition_for
from domain.curation.mapping import MappingRequest, map_rows
from domain.data_contracts.contract import DataContract
from domain.measurement.planning import FactShape, FactShapes, PlanRequest, measure_alias, plan
from domain.producer.models import (
    GovernedQueryPack,
    PeriodGrain,
    ScheduleKind,
    SourceRegistration,
    VariantTrigger,
)
from domain.producer.parameters import CommerceIdentityRequest, resolve_commerce_identities
from domain.producer.planning import (
    PlanLandingRequest,
    PlanManifestRequest,
    plan_landing,
    plan_manifest,
)
from domain.producer.validation import (
    WINDOW_END_OPTION,
    WINDOW_START_OPTION,
    validate_registration,
)
from domain.producer.variants import DeclaredVariant
from domain.semantics.calendar_periods import CalendarPeriod
from domain.semantics.identity import SourceIdentifier
from domain.semantics.publication import (
    AMC_LENS_OPTIONS,
    PartitionIdentity,
    PublicationScope,
    SuccessfulPublication,
    lens_source_options,
    publication_lens_for,
)
from domain.semantics.source_aligned import LandedDeliveryRef
from domain.semantics.time import MeasurementBucket, TimeGrain
from domain.shared_kernel.errors import AdlyticsError, DomainError, InvariantError
from domain.shared_kernel.provenance import Suppression
from domain.shared_kernel.values import DateRange
from ports.outbound.canonical import CanonicalDestination
from ports.outbound.publication_selection import PublicationSelectionRequest
from ports.outbound.query import QueryRequest, QueryResult
from ports.outbound.semantic_state import InspectExactResult
from ports.outbound.served_periods import ServedPeriodsRequest

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-099", "ADR-100", "ADR-110")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/generated"
PACK_ID = "amc-new-to-brand-buyers-platform"
FACT = "fct_new_to_brand_buyers"
US = "ATVPDKIKX0DER"
NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)
WEEK = DateRange(date(2026, 9, 14), date(2026, 9, 20))
SEPTEMBER = DateRange(date(2026, 9, 1), date(2026, 9, 30))
NOT_RETURNED_WEEK = DateRange(date(2026, 8, 24), date(2026, 8, 30))
"""The catalog's declared suppressed-only week (six weeks before the shape_anchor week)."""
HEADER = (
    "conversion_event_marketplace_name,buyers_new_to_brand_12mo,buyers_attributed,"
    "time_window_start,time_window_end,distinct_user_count,suppression_reason,rows_suppressed"
)
WINDOW = "2026-09-14T00:00:00.000Z,2026-09-21T00:00:00.000Z"
KEPT = f"AMAZON.COM,4400,9000,{WINDOW},2,,false"
WITHHELD = f",1,1,{WINDOW},1,1 amazon_attributed_events_by_conversion_time users < 2 required,true"
"""A suppressed row as AMC delivered one live: source keys NULL, counts kept, count = reason."""


def _csv(*lines: str) -> str:
    return "".join(f"{line}\n" for line in (HEADER, *lines))


@pytest.fixture(scope="module")
def contracts() -> tuple[DataContract, ...]:
    return tuple(contract for _, contract in load_contracts(ROOT))


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return load_catalog(ROOT)


def _contract(contracts: tuple[DataContract, ...], contract_id: str = PACK_ID) -> DataContract:
    return next(contract for contract in contracts if contract.contract_id == contract_id)


def _delivery(text: str, window: DateRange = WEEK) -> LoadedDelivery:
    """Land one pack-1 delivery: the generated declaration over `window`, governed by pack 1."""
    body = text.encode()
    declaration = json.loads((FIXTURES / f"{PACK_ID}.manifest.json").read_text())
    manifest = build_manifest(
        BuildManifestRequest(
            declaration={
                **declaration,
                "period_start": window.start.isoformat(),
                "period_end": window.end.isoformat(),
                "source_watermark": (window.end + timedelta(days=2)).isoformat(),
            },
            objects=(LandedObject("data-0001.csv", len(body), checksum_of(body)),),
            declared_columns=declared_columns_of(text, source_format="csv"),
        )
    )
    manifest = replace(
        manifest,
        governance=pack_by_id(ROOT, PACK_ID).governance,
        layout_version=GOVERNED_MANIFEST_LAYOUT,
    )
    return LoadedDelivery(
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


def _assess(
    delivery: LoadedDelivery, contract: DataContract, policies: tuple[DiscardPolicy, ...]
) -> LoadOutcome:
    manifest = delivery.manifest
    document = read(DeliveredFile(delivery.text, contract.source_format, write_manifest(manifest)))
    assessed = assess_source_delivery(
        AssessmentRequest(
            manifest=manifest,
            document=document,
            loads=(
                ContractLoad(
                    contract,
                    partition_for(manifest, contract),
                    map_rows(MappingRequest(contract, document)),
                ),
            ),
            registry=load_registry(ROOT),
            discard_policies=policies,
        )
    )
    return assessed.assessments[0].outcome


@dataclass
class _Loaded:
    outcome: SemanticSidecarOutcome
    rows: tuple[dict[str, object], ...]
    loss: str | None
    results: tuple[str, ...]
    publications: tuple[str, ...] = ()
    contract_rule: tuple[str, str] = ("", "")
    """The candidate's contract-assertion outcome and detail, as its evidence records them."""
    datasets: tuple[str, ...] = ()
    """Every canonical dataset any write request named, sorted and distinct."""


def _load(delivery: LoadedDelivery, policies: tuple[DiscardPolicy, ...] | None = None) -> _Loaded:
    """Run the real ingestion path (assessment, then the sidecar) on in-memory tooling.

    `policies` defaults to the installed packs' discard policies, as the composition roots build
    them (`ingestion_handlers/load.py`, `ingest_delivery/__main__.py`).
    """
    _request, tooling, _order, _attempts = _case()
    results = ingest_semantic_delivery(
        SemanticIngestionRequest(
            delivery=delivery,
            contracts=tuple(
                contract for _, contract in load_contracts(ROOT) if contract.contract_id == PACK_ID
            ),
            columns=load_registry(ROOT),
            now=NOW,
            operation_id="ntb-buyers-unit",
            execution_id="ntb-buyers-execution",
            plan_digest="sha256:ntb-buyers-plan",
            installed_governance=(pack_by_id(ROOT, PACK_ID).governance,),
            discard_policies=(
                discard_policies(load_query_packs(ROOT)) if policies is None else policies
            ),
        ),
        tooling,
    )
    canonical = tooling.canonical_writer
    assert isinstance(canonical, MemoryCanonical)
    candidates = tuple(
        item
        for item in canonical.requests
        if item.destination is CanonicalDestination.CANDIDATE
        and item.publication.partition.scope.dataset.value == FACT
    )
    rows: tuple[dict[str, object], ...] = tuple(
        dict(row) for item in candidates for row in item.rows
    )
    loss = next(
        (
            result.detail
            for item in candidates
            for result in item.publication.evidence.results
            if result.rule_id == f"suppression:{PACK_ID}.cell-loss.v1"
        ),
        None,
    )
    return _Loaded(
        outcome=results[0].outcome,
        rows=rows,
        loss=loss,
        results=tuple(result.outcome.value for result in results),
        publications=tuple(item.value for result in results for item in result.publications),
        contract_rule=next(
            (
                (result.outcome.value, result.detail)
                for item in candidates
                for result in item.publication.evidence.results
                if result.rule_id == f"contract.{PACK_ID}.v1"
            ),
            ("", ""),
        ),
        datasets=tuple(
            sorted({item.publication.partition.scope.dataset.value for item in canonical.requests})
        ),
    )


_PLATFORM_ROW = {
    "ad_product_key": None,
    "basis_key": "ad_attributed",
    "campaign_key": None,
    "channel_group_key": None,
    "channel_key": None,
    "ecosystem_key": "amazon",
    "grain_set": "platform",
    "identity_unit": "amazon_user_id",
    "marketplace_key": "amazon_us",
    "platform_key": "amazon_ads",
    "source_timezone": "UTC",
}


# --- Pack validation: the Finalizer refuses a misaligned period or a wrong parameter --------------


def _registration(window: DateRange, grain: PeriodGrain) -> SourceRegistration:
    return _pack_registration(pack_by_id(ROOT, PACK_ID), window, grain)


def _pack_registration(
    pack: GovernedQueryPack, window: DateRange, grain: PeriodGrain | None
) -> SourceRegistration:
    bridge = load_commerce_identity_bridge(ROOT)
    period = (window.start, window.end)
    return plan_landing(
        PlanLandingRequest(
            pack=pack,
            execution_id="synthetic_ntb_execution_001",
            workflow_id="adlytics_synthetic_v1",
            schedule_kind=ScheduleKind.ADHOC,
            schedule_id=None,
            source_scope=SourceIdentifier("amc.entity", "fictional-producer-entity"),
            marketplace="US",
            period_start=window.start,
            period_end=window.end,
            time_window_start=datetime.combine(window.start, datetime.min.time(), tzinfo=UTC),
            time_window_end=datetime.combine(window.end, datetime.min.time(), tzinfo=UTC)
            + timedelta(days=1),
            source_watermark=window.end + timedelta(days=2),
            retrieved_at=datetime.combine(
                window.end + timedelta(days=2), datetime.min.time(), tzinfo=UTC
            ),
            object_version_id="incoming-version-001",
            period_grain=grain,
            commerce_identities=resolve_commerce_identities(
                bridge, CommerceIdentityRequest(pack=pack, marketplace="US", period=period)
            ),
        )
    )


def _codes(registration: SourceRegistration) -> set[str]:
    pack = pack_by_id(ROOT, PACK_ID)
    report = validate_registration(pack, registration, load_commerce_identity_bridge(ROOT))
    return {finding.code for finding in report.findings}


def test_the_finalizer_refuses_a_window_misaligned_to_the_declared_grain() -> None:
    week = _registration(WEEK, PeriodGrain.WEEK)
    month = _registration(SEPTEMBER, PeriodGrain.MONTH)
    # Positive controls: a complete ISO week and a calendar month finalize.
    assert _codes(week) == set()
    assert _codes(month) == set()
    assert week.option_set["period_grain"] == "week"
    assert week.option_set["sponsored_marketplace_name"] == "AMAZON.COM"
    shifted = DateRange(WEEK.start + timedelta(days=1), WEEK.end + timedelta(days=1))
    misaligned = replace(
        week,
        period_start=shifted.start,
        period_end=shifted.end,
        option_set={
            **week.option_set,
            "timeWindowStart": f"{shifted.start.isoformat()}T00:00:00",
            "timeWindowEnd": f"{(shifted.end + timedelta(days=1)).isoformat()}T00:00:00",
        },
    )
    assert _codes(misaligned) == {"REGISTRATION_PERIOD_ALIGNMENT"}


def test_the_finalizer_refuses_a_marketplace_parameter_the_bridge_does_not_resolve() -> None:
    week = _registration(WEEK, PeriodGrain.WEEK)
    assert _codes(week) == set()
    wrong = replace(
        week, option_set={**week.option_set, "sponsored_marketplace_name": "AMAZON.CO.UK"}
    )
    assert _codes(wrong) == {"REGISTRATION_PARAMETER"}


def test_the_pack_declares_discard_bar_lag_parameters_and_week_and_month() -> None:
    pack = pack_by_id(ROOT, PACK_ID)
    assert [grain.value for grain in pack.period_policy.grains] == ["week", "month"]  # type: ignore[union-attr]
    assert (pack.expected_bar, pack.effective_cadence_lag_days) == (2, 2)
    assert [item.name for item in pack.input_parameters] == ["sponsored_marketplace_name"]
    # Packs 2 and 3 (the channel and campaign grain sets) take the same discard policy and bar.
    channel = "amc-new-to-brand-buyers-channel"
    campaign = "amc-new-to-brand-buyers-campaign"
    exposure = "amc-exposure-groups"
    coarse = (DeclaredVariant("coarse", 2, VariantTrigger.DEGRADE_NEXT_PERIOD),)
    frequency_channel = "amc-exposure-frequency-channel"
    frequency_platform = "amc-exposure-frequency-platform"
    performance = "amc-audience-performance"
    segment_names = "amc-dsp-segment-names"
    segment_metadata = "amc-segment-metadata"
    cohorts, cohorts_arp = "amc-acquisition-cohorts", "amc-acquisition-cohorts-arp"
    quarterly = (DeclaredVariant("quarterly", 2, VariantTrigger.DEGRADE_NEXT_PERIOD),)
    assert discard_policies(load_query_packs(ROOT)) == (
        # Packs 15 and 14 declare their automatic calendar-quarter variant (plan D9, D10).
        DiscardPolicy(cohorts_arp, cohorts_arp, 2, quarterly),
        DiscardPolicy(cohorts, cohorts, 2, quarterly),
        # Packs 8, 9 and 13 (segment delivery and the segment dimension) discard at bar 2.
        DiscardPolicy(performance, performance, 2),
        DiscardPolicy(segment_names, segment_names, 2),
        # Packs 6 and 7 declare their automatic coarse-bucket variant (plan D9, D10).
        DiscardPolicy(frequency_channel, frequency_channel, 2, coarse),
        DiscardPolicy(frequency_platform, frequency_platform, 2, coarse),
        # Pack 5 declares its touch-only variant (plan D9), so the load knows its pack version.
        DiscardPolicy(
            query_pack_id=exposure,
            contract_id=exposure,
            expected_bar=2,
            variants=(DeclaredVariant("touch_only", 2, VariantTrigger.MANUAL),),
        ),
        DiscardPolicy(query_pack_id=campaign, contract_id=campaign, expected_bar=2),
        DiscardPolicy(query_pack_id=channel, contract_id=channel, expected_bar=2),
        DiscardPolicy(query_pack_id=PACK_ID, contract_id=PACK_ID, expected_bar=2),
        # Packs 12, 10 and 11 (the buyer segment profile) discard at bar 2.
        *(
            DiscardPolicy(pack, pack, 2)
            for pack in (
                "amc-segment-buyer-profile-dsp-exposed",
                "amc-segment-buyer-profile-inmarket",
                "amc-segment-buyer-profile-lifestyle",
            )
        ),
        DiscardPolicy(segment_metadata, segment_metadata, 2),
    )


# --- Load: discard, bar, closed enumeration, loss -------------------------------------------------


def test_a_suppressed_row_is_discarded_and_the_kept_row_publishes_with_its_loss(
    contracts: tuple[DataContract, ...],
) -> None:
    loaded = _load(_delivery(_csv(KEPT, WITHHELD)))
    assert loaded.outcome is SemanticSidecarOutcome.PUBLISHED
    assert loaded.rows == (
        {
            **_PLATFORM_ROW,
            "buyers_attributed": 9000,
            "buyers_new_to_brand_12mo": 4400,
            "cell_state": "returned",
            "period_end": WEEK.end,
            "period_start": WEEK.start,
            "time_grain": "week",
        },
    )
    assert loaded.loss == (
        '{"discarded_rows":1,"expected_cells":1,"grade":"not_degraded",'
        '"loss_ratio":"0","not_returned_cells":0}'
    )
    # The discarded row goes nowhere else: no residual table exists for this fact, and no write
    # request names one.
    assert loaded.datasets == (FACT,)
    # Paired: the same delivery, not under its pack's discard policy, is refused.
    refused = _assess(_delivery(_csv(KEPT, WITHHELD)), _contract(contracts), ())
    assert [item.assertion for item in refused.violations] == [AssertionName.CLOSED_ENUMERATION]
    assert refused.violations[0].detail.startswith(
        f"{PACK_ID}: records cell_state, so its delivery must come from a governed pack that "
        "declares discard_suppressed_rows"
    )
    assert _load(_delivery(_csv(KEPT, WITHHELD)), ()).outcome is SemanticSidecarOutcome.BLOCKED


def test_a_missing_platform_cell_is_written_not_returned_with_null_measures() -> None:
    with patch.object(sidecar_module, "report_operation") as warned:
        loaded = _load(_delivery(_csv(WITHHELD)))
    assert loaded.outcome is SemanticSidecarOutcome.PUBLISHED
    assert loaded.rows == (
        {
            **_PLATFORM_ROW,
            "buyers_attributed": None,
            "buyers_new_to_brand_12mo": None,
            "cell_state": "not_returned",
            "period_end": WEEK.end,
            "period_start": WEEK.start,
            "time_grain": "week",
        },
    )
    assert loaded.loss == (
        '{"discarded_rows":1,"expected_cells":1,"grade":"degraded",'
        '"loss_ratio":"1","not_returned_cells":1}'
    )
    # One structured WARN, above ADR-019's degrade threshold.
    assert warned.call_count == 1
    event = warned.call_args.args[0]
    assert (event.operation, event.outcome, event.severity.value) == (
        "ingest.shopper_aggregate_loss",
        "degraded",
        "warning",
    )
    assert event.details["loss_ratio"] == "1"
    # Positive control: a returned cell warns nothing.
    with patch.object(sidecar_module, "report_operation") as quiet:
        _load(_delivery(_csv(KEPT)))
    assert quiet.call_count == 0


def test_a_delivery_with_no_rows_writes_its_period_cell_not_returned() -> None:
    """A header-only delivery: empty and suppressed look the same (ADR-099 point 8)."""
    loaded = _load(_delivery(_csv()))
    assert loaded.outcome is SemanticSidecarOutcome.PUBLISHED
    assert loaded.rows == (
        {
            **_PLATFORM_ROW,
            "buyers_attributed": None,
            "buyers_new_to_brand_12mo": None,
            "cell_state": "not_returned",
            "period_end": WEEK.end,
            "period_start": WEEK.start,
            "time_grain": "week",
        },
    )
    assert loaded.loss == (
        '{"discarded_rows":0,"expected_cells":1,"grade":"degraded",'
        '"loss_ratio":"1","not_returned_cells":1}'
    )
    # The month form takes the manifest's month.
    month = _load(_delivery(_csv(), SEPTEMBER))
    assert [
        (row["period_start"], row["period_end"], row["time_grain"], row["cell_state"])
        for row in month.rows
    ] == [(SEPTEMBER.start, SEPTEMBER.end, "month", "not_returned")]
    # Refused: an empty delivery whose manifest period isn't one complete week or month.
    shifted = DateRange(WEEK.start + timedelta(days=1), WEEK.end + timedelta(days=1))
    refused = _load(_delivery(_csv(), shifted))
    assert (refused.outcome, refused.rows) == (SemanticSidecarOutcome.BLOCKED, ())
    # Positive control: a returned row over the same week publishes returned.
    assert [row["cell_state"] for row in _load(_delivery(_csv(KEPT))).rows] == ["returned"]


def test_a_row_window_other_than_the_manifest_period_is_refused() -> None:
    """A delivery publishes only the period its manifest names (row September, manifest week)."""
    september = "2026-09-01T00:00:00.000Z,2026-10-01T00:00:00.000Z"
    with pytest.raises(ValueError, match="but the manifest declares") as raised:
        _load(_delivery(_csv(KEPT.replace(WINDOW, september))))
    assert str(raised.value) == (
        "time_window_start..time_window_end measure 2026-09-01..2026-09-30, but the manifest "
        "declares 2026-09-14..2026-09-20."
    )
    # Positive control: the same row under a September manifest publishes the month.
    month = _load(_delivery(_csv(KEPT.replace(WINDOW, september)), SEPTEMBER))
    assert [(row["time_grain"], row["cell_state"]) for row in month.rows] == [("month", "returned")]


def test_the_bar_accepts_two_and_rejects_a_kept_row_at_three(
    contracts: tuple[DataContract, ...],
) -> None:
    policies = discard_policies(load_query_packs(ROOT))
    contract = _contract(contracts)
    assert _assess(_delivery(_csv(KEPT)), contract, policies).violations == ()
    above = KEPT.replace(f"{WINDOW},2,", f"{WINDOW},3,")
    refused = _assess(_delivery(_csv(above)), contract, policies)
    assert [item.assertion for item in refused.violations] == [AssertionName.PRIVACY_BAR]
    assert refused.violations[0].detail == (
        f"{PACK_ID}: BAR_EXCEEDED: 1 unsuppressed row(s) read distinct_user_count above "
        "expectedBar 2 (highest 3)."
    )
    assert dict(refused.violations[0].context)["bar_verdict"] == "rejected"
    # The rejection quarantines: the delivery is blocked. Its alarm route is asserted below.
    assert _load(_delivery(_csv(above))).outcome is SemanticSidecarOutcome.BLOCKED


# --- Alarm routing: what the deployed alarms count ------------------------------------------------


def _partition_actions(monkeypatch: pytest.MonkeyPatch, text: str) -> list[list[tuple[str, str]]]:
    """Run a delivery and report it through the load handler's own seam (`_report_v2`).

    Returns each emitted `IngestPartitions` metric's dimensions. The BlockedPartitions alarm
    (infra/lib/stacks/ingestion-stack.ts) counts the `Action=blocked` series of that metric.
    """
    _configure_ingestion(monkeypatch)
    from adapters.inbound.ingestion_handlers import load  # noqa: PLC0415

    emitted: list[list[tuple[str, str]]] = []

    class _Metric:
        def __init__(self) -> None:
            self.dimensions: list[tuple[str, str]] = []

        def add_dimension(self, *, name: str, value: str) -> None:
            self.dimensions.append((name, value))

    @contextmanager
    def metric(**kwargs: object) -> Generator[_Metric]:
        assert kwargs["name"] == load.PARTITION_METRIC == "IngestPartitions"
        item = _Metric()
        emitted.append(item.dimensions)
        yield item

    monkeypatch.setattr(load, "single_metric", metric)

    def ignore_operation(_event: object, _logger: object) -> None:
        return None

    monkeypatch.setattr(load, "report_operation", ignore_operation)
    # The composition root passes the installed discard policies, built from the same packs.
    installed = discard_policies(load_query_packs(ROOT))
    assert installed == load.INSTALLED_DISCARD_POLICIES
    _request, tooling, _order, _attempts = _case()
    results = ingest_semantic_delivery(
        SemanticIngestionRequest(
            delivery=_delivery(text),
            contracts=tuple(
                contract for _, contract in load_contracts(ROOT) if contract.contract_id == PACK_ID
            ),
            columns=load_registry(ROOT),
            now=NOW,
            operation_id="ntb-buyers-alarm",
            execution_id="ntb-buyers-alarm-execution",
            plan_digest="sha256:ntb-buyers-alarm-plan",
            installed_governance=load.INSTALLED_GOVERNANCE,
            discard_policies=load.INSTALLED_DISCARD_POLICIES,
        ),
        tooling,
    )
    load._report_v2(  # pyright: ignore[reportPrivateUsage]
        "manifest.json",
        load._V2Outcome(  # pyright: ignore[reportPrivateUsage]
            "ntb-buyers-alarm", "ntb-buyers-alarm", "sha256:plan", results, write_plan_key="none"
        ),
        elapsed_ms=1.0,
        # The SQS receive that completed the terminal record (S6-8): the first one.
        receive_count=1,
    )
    return emitted


def test_a_bar_exceeded_delivery_counts_in_the_blocked_partitions_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BAR_EXCEEDED quarantines the delivery; the handler counts it as IngestPartitions blocked.

    Not the typed-error alarm: a quarantine is a decided outcome, not an invariant or dependency
    error, so it routes to BlockedPartitions.
    """
    above = KEPT.replace(f"{WINDOW},2,", f"{WINDOW},3,")
    assert _partition_actions(monkeypatch, _csv(above)) == [[("Action", "blocked")]]
    # Positive control: the delivery at the bar publishes and isn't in the blocked series.
    assert _partition_actions(monkeypatch, _csv(KEPT)) == [[("Action", "published")]]


def test_a_misaligned_registration_is_rejected_as_adly_producer_0001_on_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Finalizer refuses a period misaligned to the pack's grain with a typed rejection.

    Reported as ADLY-PRODUCER-0001 on `finalize_registration` and counted as
    ProducerRegistrations Action=rejected, the series the RejectedRegistrations alarm reads.
    """
    module = _finalizer(monkeypatch)
    pack = pack_by_id(ROOT, PACK_ID)

    def run(registration: SourceRegistration) -> tuple[list[tuple[str, str, str]], list[str]]:
        version = "registration-version-ntb"
        landing = _FinalizerLanding(registration.registration_key, version)
        window = (
            f"{registration.option_set['timeWindowStart']}Z,"
            f"{registration.option_set['timeWindowEnd']}Z"
        )
        landing.objects = {
            (registration.registration_key, version): write_registration(registration).encode(),
            (registration.object.key, registration.object.version_id): _csv(
                KEPT.replace(WINDOW, window)
            ).encode(),
        }
        monkeypatch.setattr(
            module,
            "RUNTIME",
            module.FinalizerRuntime(
                FINALIZER_BUCKET,
                {pack.pack_id: pack},
                landing,
                _FinalizerStatuses(),
                module.COMMERCE_IDENTITIES,
            ),
        )
        errors: list[tuple[str, str, str]] = []
        actions: list[str] = []

        def capture_error(error: AdlyticsError, report: Report) -> None:
            errors.append((str(error.code), report.operation, str(error.context["code"])))

        def capture_action(action: str, _message: object) -> None:
            actions.append(action)

        monkeypatch.setattr(module, "report_error", capture_error)
        monkeypatch.setattr(module, "_report", capture_action)
        inspect.unwrap(module.handler)(
            _finalizer_event(registration.registration_key, version), object()
        )
        return errors, actions

    week = _registration(WEEK, PeriodGrain.WEEK)
    shifted = DateRange(WEEK.start + timedelta(days=1), WEEK.end + timedelta(days=1))
    misaligned = replace(
        week,
        period_start=shifted.start,
        period_end=shifted.end,
        option_set={
            **week.option_set,
            "timeWindowStart": f"{shifted.start.isoformat()}T00:00:00",
            "timeWindowEnd": f"{(shifted.end + timedelta(days=1)).isoformat()}T00:00:00",
        },
    )
    assert run(misaligned) == (
        [("ADLY-PRODUCER-0001", "finalize_registration", "REGISTRATION_PERIOD_ALIGNMENT")],
        ["rejected"],
    )
    # Positive control: the aligned week finalizes, with no error reported.
    assert run(week) == ([], ["finalized"])


def test_every_kept_row_at_100_under_a_bar_of_2_is_recorded_as_an_escalation(
    contracts: tuple[DataContract, ...],
) -> None:
    policies = discard_policies(load_query_packs(ROOT))
    escalated = KEPT.replace(f"{WINDOW},2,", f"{WINDOW},100,")
    refused = _assess(_delivery(_csv(escalated, WITHHELD)), _contract(contracts), policies)
    assert [item.assertion for item in refused.violations] == [AssertionName.PRIVACY_BAR]
    assert dict(refused.violations[0].context) == {
        "bar_verdict": "escalation",
        "contract_id": PACK_ID,
        "expected_bar": "2",
        "finding_code": "BAR_ESCALATION",
        "highest_user_count": "100",
    }


def test_a_repeated_platform_cell_rejects_the_delivery() -> None:
    loaded = _load(_delivery(_csv(KEPT, KEPT)))
    assert loaded.outcome is SemanticSidecarOutcome.BLOCKED
    assert loaded.publications == ()
    assert loaded.contract_rule == (
        "fail",
        f"{PACK_ID}: CELL_DUPLICATE: row 1 repeats cell marketplace_key=amazon_us.",
    )
    # Positive control: the one cell loads.
    assert _load(_delivery(_csv(KEPT))).outcome is SemanticSidecarOutcome.PUBLISHED


def test_a_returned_row_with_a_null_count_rejects_the_delivery() -> None:
    """A NULL means not_returned here, so a returned row can't carry one (A1-3)."""
    blank = KEPT.replace("AMAZON.COM,4400,9000,", "AMAZON.COM,,9000,")
    loaded = _load(_delivery(_csv(blank)))
    assert (loaded.outcome, loaded.publications) == (SemanticSidecarOutcome.BLOCKED, ())
    assert loaded.contract_rule == (
        "fail",
        f"{PACK_ID}: CELL_VALUE_NULL: row 0: buyers_new_to_brand_12mo is NULL on an "
        "unsuppressed row.",
    )
    # Positive control: the same row with its count publishes.
    assert _load(_delivery(_csv(KEPT))).outcome is SemanticSidecarOutcome.PUBLISHED


def test_a_reject_delivery_pack_keeps_its_refusal_beside_the_discard_policy(
    contracts: tuple[DataContract, ...],
) -> None:
    """amc-reach-frequency (reject_delivery) is assessed exactly as before the discard path."""
    reach = _contract(contracts, "amc-reach-frequency")
    text = (FIXTURES / "amc-reach-frequency.csv").read_text()
    lines = text.splitlines()
    header, first = lines[0], lines[1]
    columns = header.split(",")
    values = first.split(",")
    suppressed = dict(zip(columns, values, strict=True)) | {
        "rows_suppressed": "true",
        "distinct_user_count": "1",
        "suppression_reason": "1 sponsored_ads_traffic users < 2 required",
    }
    body = "\n".join((header, first, ",".join(suppressed[name] for name in columns))) + "\n"
    manifest = replace(
        _delivery(body).manifest,
        governance=pack_by_id(ROOT, "amc-reach-frequency").governance,
        declared_columns=tuple(columns),
    )
    document = read(DeliveredFile(body, reach.source_format, write_manifest(manifest)))
    policies = discard_policies(load_query_packs(ROOT))

    def assessed(rows_text: str) -> LoadOutcome:
        doc = read(DeliveredFile(rows_text, reach.source_format, write_manifest(manifest)))
        return (
            assess_source_delivery(
                AssessmentRequest(
                    manifest=manifest,
                    document=doc,
                    loads=(
                        ContractLoad(
                            reach,
                            partition_for(manifest, reach),
                            map_rows(MappingRequest(reach, doc)),
                        ),
                    ),
                    registry=load_registry(ROOT),
                    discard_policies=policies,
                )
            )
            .assessments[0]
            .outcome
        )

    assert document.rows
    refused = assessed(body)
    assert [item.assertion for item in refused.violations] == [
        AssertionName.NON_ADDITIVE_SUPPRESSION
    ]
    assert refused.violations[0].detail == (
        "amc-reach-frequency: 1 suppressed row(s) carry non-additive reach_users. AMC can "
        "preserve duplicate nulled grain rows for distinct counts, so this delivery cannot be "
        "aggregated or published safely."
    )
    # Positive control: the clean delivery passes.
    assert assessed("\n".join((header, first)) + "\n").violations == ()


# --- Planner: exact periods, D-1, D-6, D-7 --------------------------------------------------------


def _shape(contracts: tuple[DataContract, ...]) -> FactShape:
    return FactShape.for_fact(FACT, contracts)


def _filters(grain: str) -> dict[str, str]:
    return {"time_grain": grain, "grain_set": "platform", "marketplace_key": US}


def _refused(call: object) -> DomainError:
    with pytest.raises(DomainError) as refused:
        call()  # type: ignore[operator]
    return refused.value


def test_an_exact_month_plans_and_a_seven_day_window_names_the_nearest_month(
    catalog: Catalog, contracts: tuple[DataContract, ...]
) -> None:
    planned = plan(
        catalog,
        PlanRequest("buyers_new_to_brand_12mo", ("marketplace",), SEPTEMBER, _filters("month")),
        _shape(contracts),
    )
    assert planned.period == CalendarPeriod(SEPTEMBER.start, SEPTEMBER.end, TimeGrain.MONTH)
    assert dict(planned.filters) == _filters("month")
    rolling = DateRange(date(2026, 9, 24), date(2026, 9, 30))
    error = _refused(
        lambda: plan(
            catalog,
            PlanRequest("buyers_new_to_brand_12mo", ("marketplace",), rolling, _filters("month")),
            _shape(contracts),
        )
    )
    assert error.code == GRAIN_NOT_SUPPORTED_CODE
    # The nearest complete month ending on or before the window's end.
    assert dict(error.context) == {
        "requested": "buyers_new_to_brand_12mo",
        "reason": "period_not_complete",
        "nearest_period_start": "2026-09-01",
        "nearest_period_end": "2026-09-30",
        "nearest_time_grain": "month",
        "nearest_period_label": "September 2026",
    }


def test_two_weeks_are_never_summed(catalog: Catalog, contracts: tuple[DataContract, ...]) -> None:
    two_weeks = DateRange(WEEK.start, WEEK.end + timedelta(days=7))
    error = _refused(
        lambda: plan(
            catalog,
            PlanRequest("buyers_attributed", ("marketplace",), two_weeks, _filters("week")),
            _shape(contracts),
        )
    )
    assert error.code == GRAIN_NOT_SUPPORTED_CODE
    assert dict(error.context)["reason"] == "period_not_complete"
    # Positive control: one week plans.
    assert plan(
        catalog,
        PlanRequest("buyers_attributed", ("marketplace",), WEEK, _filters("week")),
        _shape(contracts),
    ).period == CalendarPeriod(WEEK.start, WEEK.end, TimeGrain.WEEK)


def test_the_period_grain_must_be_pinned(
    catalog: Catalog, contracts: tuple[DataContract, ...]
) -> None:
    unpinned = {"grain_set": "platform", "marketplace_key": US}
    error = _refused(
        lambda: plan(
            catalog,
            PlanRequest("buyers_new_to_brand_12mo", ("marketplace",), SEPTEMBER, unpinned),
            _shape(contracts),
        )
    )
    assert dict(error.context)["reason"] == "requires_source_option"


@dataclass
class _Current(RecordingCurrent):
    """CURRENT state with chosen partitions absent, for D-6 coverage and served periods."""

    absent: frozenset[str] = frozenset()
    periodic: dict[str, tuple[SuccessfulPublication, ...]] = field(
        default_factory=dict[str, tuple[SuccessfulPublication, ...]]
    )

    def inspect_exact(self, partitions: tuple[PartitionIdentity, ...]) -> InspectExactResult:
        present = super().inspect_exact(partitions)
        return InspectExactResult(
            tuple(
                None if partition.identity in self.absent else publication
                for partition, publication in zip(partitions, present.publications, strict=True)
            )
        )

    def current_for_scope(
        self, scope: PublicationScope, horizon: object = None
    ) -> tuple[SuccessfulPublication, ...]:
        del horizon
        self.scope_calls.append(scope)
        return self.periodic.get(scope.identity, ())


def _cost_plan(catalog: Catalog, contracts: tuple[DataContract, ...]) -> object:
    return plan(
        catalog,
        PlanRequest(
            "cost_per_new_to_brand_buyer_12mo", ("marketplace",), SEPTEMBER, _filters("month")
        ),
        FactShapes(
            numerator=FactShape.for_fact("fct_ad_exposure", contracts),
            denominator=_shape(contracts),
        ),
    )


def test_d6_cost_per_new_buyer_needs_every_spend_day_of_the_month(
    catalog: Catalog, contracts: tuple[DataContract, ...]
) -> None:
    logical = _cost_plan(catalog, contracts)
    selector = PublicationSelector(
        _Current(), load_source_packs(ROOT), load_commerce_identity_bridge(ROOT)
    )
    bound = selector.select(PublicationSelectionRequest((logical,), contracts, NOW))  # type: ignore[arg-type]
    spend_days = {
        item.partition.bucket.window.start
        for item in bound.plans[0].primary
        if item.partition.scope.dataset.value == "fct_ad_exposure"
    }
    assert spend_days == {SEPTEMBER.start + timedelta(days=offset) for offset in range(30)}
    assert {item.partition.bucket.window for item in bound.plans[0].joined} == {SEPTEMBER}
    missing = next(
        item.partition.identity
        for item in bound.plans[0].primary
        if item.partition.bucket.window.start == date(2026, 9, 15)
    )
    gap = PublicationSelector(
        _Current(absent=frozenset({missing})),
        load_source_packs(ROOT),
        load_commerce_identity_bridge(ROOT),
    )
    with pytest.raises(DomainError, match="No published data covers the requested window"):
        gap.select(PublicationSelectionRequest((logical,), contracts, NOW))  # type: ignore[arg-type]


# --- Chat: the period a periodic answer reads -----------------------------------------------------


def _served(contracts: tuple[DataContract, ...], *periods: CalendarPeriod) -> PublicationSelector:
    """Build a selector whose CURRENT state holds pack 1's US publications for `periods`."""
    packs = load_source_packs(ROOT)
    bridge = load_commerce_identity_bridge(ROOT)
    pack = next(item for item in packs if item.contract_id == PACK_ID)
    current = _Current()
    for period in periods:
        # Each grain is its own publication family: the registration's period_grain is in the
        # lens, so a week lands under the week scope and a month under the month scope.
        (scope,) = publication_scopes_for(
            PublicationScopeRequest(
                pack=pack,
                contract=_contract(contracts),
                bridge=bridge,
                filters={"marketplace_key": US},
                period_grain=period.grain,
            )
        )
        current.periodic[scope.identity] = (
            *current.periodic.get(scope.identity, ()),
            _success(
                PartitionIdentity(
                    scope,
                    MeasurementBucket(
                        period.window,
                        period.grain,
                        scope.business_calendar,
                        scope.source_timezone,
                        scope.observation_basis,
                    ),
                ),
                published_at=NOW - timedelta(days=1),
            ),
        )
    return PublicationSelector(current, packs, bridge, cadence_lag_days={PACK_ID: 2})


@dataclass
class _Rows:
    rows: tuple[dict[str, object], ...]
    seen: list[QueryRequest] = field(default_factory=list[QueryRequest])

    def run(self, request: QueryRequest) -> QueryResult:
        self.seen.append(request)
        return QueryResult(
            rows=self.rows,  # type: ignore[arg-type]
            bytes_scanned=1,
            truncated=False,
            suppression=Suppression(rows_suppressed=False),
        )


AUGUST_PERIOD = CalendarPeriod(date(2026, 8, 1), date(2026, 8, 31), TimeGrain.MONTH)
SEPTEMBER_PERIOD = CalendarPeriod(SEPTEMBER.start, SEPTEMBER.end, TimeGrain.MONTH)


def _chat(  # noqa: PLR0913, PLR0917 -- the question, what is published, and what comes back
    contracts: tuple[DataContract, ...],
    window: DateRange,
    periods: tuple[CalendarPeriod, ...],
    rows: tuple[dict[str, object], ...],
) -> tuple[object, _Rows]:
    queries = _Rows(rows)
    response = handle(
        MeasurementToolRequest(
            tool=MeasurementTool.METRIC_BY_GRAIN,
            catalog=load_catalog(ROOT),
            contracts=contracts,
            metric_name="buyers_new_to_brand_12mo",
            grains=("marketplace",),
            window=window,
            computed_at=NOW,
            correlation_id="corr-ntb",
            filters={"marketplace_key": US},
        ),
        MeasurementTooling(
            queries=queries,
            publications=StubPublications(),
            periods=_served(contracts, *periods),
        ),
    )
    return response, queries


def test_new_buyers_last_month_reads_the_exact_month_and_names_it(
    contracts: tuple[DataContract, ...],
) -> None:
    response, queries = _chat(
        contracts,
        SEPTEMBER,
        (AUGUST_PERIOD, SEPTEMBER_PERIOD),
        ({"marketplace_key": "amazon_us", measure_alias(0): 36419},),
    )
    planned = queries.seen[0].plan.logical
    assert planned.period == SEPTEMBER_PERIOD
    # The period grain and the one served grain set are pinned with the caller's marketplace.
    assert dict(planned.filters) == {
        "grain_set": "platform",
        "marketplace_key": US,
        "time_grain": "month",
    }
    narrative = response.narrative  # type: ignore[attr-defined]
    assert narrative[1] == "Figures are for September 2026."
    assert (
        "This metric is measured for one complete month, September 2026, so it must not be "
        "read as a daily additive figure."
    ) in narrative
    assert response.data.values == ({"marketplace_key": "amazon_us", "value": 36419},)  # type: ignore[attr-defined]


def test_last_month_not_landed_uses_the_latest_measured_month_and_says_so(
    contracts: tuple[DataContract, ...],
) -> None:
    response, queries = _chat(
        contracts,
        SEPTEMBER,
        (AUGUST_PERIOD,),
        ({"marketplace_key": "amazon_us", measure_alias(0): 30000},),
    )
    assert queries.seen[0].plan.logical.period == AUGUST_PERIOD
    assert response.narrative[1] == (  # type: ignore[attr-defined]
        "September 2026 hasn't been measured yet, so this answer uses the latest measured "
        "month, August 2026."
    )


def test_seven_days_are_refused_with_the_latest_measured_week(
    contracts: tuple[DataContract, ...],
) -> None:
    measured = CalendarPeriod(WEEK.start, WEEK.end, TimeGrain.WEEK)
    rolling = DateRange(date(2026, 9, 29), date(2026, 10, 5))
    with pytest.raises(DomainError) as refused:
        _chat(contracts, rolling, (measured,), ())
    assert refused.value.code == GRAIN_NOT_SUPPORTED_CODE
    assert dict(refused.value.context)["reason"] == "period_not_complete"
    assert dict(refused.value.context)["nearest_period_label"] == "Week of 14\u201320 Sep 2026"
    assert refused.value.description.endswith(
        "The latest measured week is Week of 14\u201320 Sep 2026."
    )


def test_a_not_returned_cell_answers_not_available_never_zero(
    contracts: tuple[DataContract, ...],
) -> None:
    response, _queries = _chat(
        contracts,
        SEPTEMBER,
        (SEPTEMBER_PERIOD,),
        ({"marketplace_key": "amazon_us", measure_alias(0): None},),
    )
    assert response.data.values == ()  # type: ignore[attr-defined]
    lines = cast("tuple[str, ...]", response.narrative)  # type: ignore[attr-defined]
    # A distinct count is never summed over rows, so the sentence names no short count.
    assert (
        "1 row(s) are not shown because they're not available: too few shoppers, or none, "
        "which leaves the figure undefined rather than zero."
    ) in lines
    assert not any("A count over the rows above" in line for line in lines)


def test_a_periodic_metric_without_the_period_reader_is_refused(
    contracts: tuple[DataContract, ...],
) -> None:
    request = MeasurementToolRequest(
        tool=MeasurementTool.METRIC_BY_GRAIN,
        catalog=load_catalog(ROOT),
        contracts=contracts,
        metric_name="buyers_new_to_brand_12mo",
        grains=("marketplace",),
        window=SEPTEMBER,
        computed_at=NOW,
        correlation_id="corr-ntb",
        filters={"marketplace_key": US},
    )
    with pytest.raises(InvariantError) as refused:
        handle(request, MeasurementTooling(queries=_Rows(()), publications=StubPublications()))
    assert str(refused.value.code) == "ADLY-MEASUREMENT-0005"
    # Positive control: a daily metric never reads it.
    daily = replace(request, metric_name="ad_spend", window=WEEK, grains=("marketplace",))
    handle(
        daily,
        MeasurementTooling(
            queries=_Rows(
                (
                    {
                        "marketplace_key": "amazon_us",
                        "currency_code": "USD",
                        measure_alias(0): Decimal("1.0000"),
                    },
                )
            ),
            publications=StubPublications(),
        ),
    )


def test_served_periods_read_publication_state_only(contracts: tuple[DataContract, ...]) -> None:
    selector = _served(
        contracts, SEPTEMBER_PERIOD, CalendarPeriod(WEEK.start, WEEK.end, TimeGrain.WEEK)
    )
    served = selector.served_periods(
        ServedPeriodsRequest(
            contracts=(_contract(contracts),), filters={"marketplace_key": US}, now=NOW
        )
    )
    assert served.publications == frozenset(
        {
            (PACK_ID, SEPTEMBER_PERIOD),
            (PACK_ID, CalendarPeriod(WEEK.start, WEEK.end, TimeGrain.WEEK)),
        }
    )
    assert dict(served.cadence_lag_days) == {PACK_ID: 2}
    with pytest.raises(ValueError, match="no periodic source pack"):
        selector.served_periods(
            ServedPeriodsRequest(
                contracts=(_contract(contracts, "amc-reach-frequency"),),
                filters={"marketplace_key": US},
                now=NOW,
            )
        )


def test_a_producer_landed_week_is_served_and_a_lens_without_its_grain_finds_nothing(
    contracts: tuple[DataContract, ...],
) -> None:
    """Wave-2 risk A1-7: serving rebuilds the lens the publisher hashed from the registration.

    The manifest is the Finalizer's own (`plan_manifest`), so its option set is the full
    registration: execution options, window bounds, `period_grain` and the marketplace parameter.
    The lens keeps `period_grain` only (`lens_source_options`), and serving adds it per grain.
    """
    registration = _registration(WEEK, PeriodGrain.WEEK)
    text = _csv(KEPT)
    _summary, evidence = parse_csv_result(text.encode())
    manifest = plan_manifest(
        PlanManifestRequest(registration, pack_by_id(ROOT, PACK_ID), evidence, "result-v1")
    ).manifest
    assert {"timeWindowStart", "sponsored_marketplace_name", "period_grain"} <= set(
        manifest.option_set
    )
    delivery = LoadedDelivery(
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
    _request, tooling, _order, _attempts = _case()
    results = ingest_semantic_delivery(
        SemanticIngestionRequest(
            delivery=delivery,
            contracts=(_contract(contracts),),
            columns=load_registry(ROOT),
            now=NOW,
            operation_id="ntb-producer-landed",
            execution_id="ntb-producer-landed-execution",
            plan_digest="sha256:ntb-producer-landed-plan",
            installed_governance=(pack_by_id(ROOT, PACK_ID).governance,),
            discard_policies=discard_policies(load_query_packs(ROOT)),
        ),
        tooling,
    )
    assert [result.outcome for result in results] == [SemanticSidecarOutcome.PUBLISHED]
    publications = cast("MemoryPublications", tooling.publications)
    landed = tuple(
        item
        for item in publications.current_items.values()
        if item.partition.scope.dataset.value == FACT
    )
    current = _Current()
    for publication in landed:
        scope = publication.partition.scope
        current.periodic[scope.identity] = (*current.periodic.get(scope.identity, ()), publication)
    selector = PublicationSelector(
        current, load_source_packs(ROOT), load_commerce_identity_bridge(ROOT)
    )
    served = selector.served_periods(
        ServedPeriodsRequest(
            contracts=(_contract(contracts),), filters={"marketplace_key": US}, now=NOW
        )
    )
    assert served.publications == frozenset(
        {(PACK_ID, CalendarPeriod(WEEK.start, WEEK.end, TimeGrain.WEEK))}
    )
    # Control: the lens without period_grain, the one serving built before (the contract's
    # reportOptions only, none on AMC), finds nothing the producer landed.
    (published,) = landed
    pack = next(item for item in load_source_packs(ROOT) if item.contract_id == PACK_ID)
    profile = f"source_pack.{PACK_ID.replace('-', '_')}"
    without_grain = publication_lens_for(profile, (), pack.registry_version)
    assert published.partition.scope.lens == publication_lens_for(
        profile, (("period_grain", '"week"'),), pack.registry_version
    )
    assert current.current_for_scope(replace(published.partition.scope, lens=without_grain)) == ()


_GRAIN_WINDOWS = {
    PeriodGrain.WEEK: WEEK,
    PeriodGrain.MONTH: SEPTEMBER,
    PeriodGrain.QUARTER: DateRange(date(2026, 7, 1), date(2026, 9, 30)),
    # A cohort vintage: the 13 calendar months ending with its as_of_month (ADR-104).
    PeriodGrain.CUSTOM: DateRange(date(2025, 9, 1), date(2026, 9, 30)),
}


def _unclassified_options(pack: GovernedQueryPack, names: set[str]) -> set[str]:
    """Registration options that neither name a family (the lens) nor are declared non-family.

    Non-family, as `AMC_LENS_OPTIONS` documents: the pack's fixed execution options, the window
    bounds and the pack's input parameters. Anything else is a new kind of option whose lens
    decision nobody made.
    """
    non_family = {
        *(name for name, _value in pack.execution_options),
        WINDOW_START_OPTION,
        WINDOW_END_OPTION,
        *(parameter.name for parameter in pack.input_parameters),
    }
    return names - AMC_LENS_OPTIONS - non_family


def test_every_amc_registration_option_has_a_lens_decision() -> None:
    """Review B1-1: the AMC lens allowlist fails closed on a registration option nobody classed.

    Builds every governed pack's registration (`plan_landing`, the same option set the Finalizer
    expects) for each declared grain. A new option, such as a pack variant (plan D9), fails here
    until it is added to `AMC_LENS_OPTIONS` or named non-family, so it can't silently drop out of
    the lens and merge two series into one family.
    """
    checked = 0
    installed = load_query_packs(ROOT)
    bindings = (
        *installed,
        *(pack.variant_binding(item.variant) for pack in installed for item in pack.variants),
    )
    for pack in bindings:
        grains = (None,) if pack.period_policy is None else pack.period_policy.grains
        for grain in grains:
            window = DateRange(date(2026, 9, 14), date(2026, 9, 14))
            if grain is not None:
                window = _GRAIN_WINDOWS[grain]
            registration = _pack_registration(pack, window, grain)
            names = set(registration.option_set)
            assert _unclassified_options(pack, names) == set(), pack.pack_id
            kept = lens_source_options("amc", tuple((name, "x") for name in sorted(names)))
            # A declared variant's registration names it (plan D9), so its lens does too.
            expected: set[str] = (set[str]() if grain is None else {"period_grain"}) | (
                set[str]() if pack.variant_id is None else {"pack_variant"}
            )
            assert {name for name, _value in kept} == expected, pack.pack_id
            checked += 1
    assert checked >= len(bindings)
    assert any(pack.variant_id == "touch_only" for pack in bindings)
    # Control: an option of a new kind is caught, not dropped silently.
    pack = pack_by_id(ROOT, PACK_ID)
    names = set(_registration(WEEK, PeriodGrain.WEEK).option_set)
    assert _unclassified_options(pack, {*names, "bucket_scheme"}) == {"bucket_scheme"}


# --- Catalog --------------------------------------------------------------------------------------


def test_both_buyer_counts_are_plannable_in_the_shoppers_and_reach_category(
    catalog: Catalog,
) -> None:
    category = next(
        item for item in catalog.inventory_categories if item.category_id == "shoppers_and_reach"
    )
    assert category.metric_ids[:3] == (
        "buyers_new_to_brand_12mo",
        "buyers_attributed",
        "cost_per_new_to_brand_buyer_12mo",
    )
    for name in ("buyers_new_to_brand_12mo", "buyers_attributed"):
        metric = catalog.metric(name)
        assert metric is not None
        assert metric.period_grains == (TimeGrain.WEEK, TimeGrain.MONTH)
        assert metric.required_selectors == (("time_grain", ("month", "week")),)
        assert catalog.refusal_for(name, grain="marketplace", filters=_filters("week")) is None
    cost = catalog.metric("cost_per_new_to_brand_buyer_12mo")
    assert cost is not None
    assert cost.required_selectors == (("time_grain", ("month", "week")),)
    listed = list_metrics(ListMetricsRequest(catalog=catalog, context=_context()))
    assert {
        "collection": "catalog:shoppers_and_reach",
        "label": "Shoppers and reach",
        "status": "available",
    } in listed.data.values
    grounding = listed.metadata.grounding
    assert grounding is not None
    collection = next(
        item for item in grounding.collections if item.collection_id.endswith("shoppers_and_reach")
    )
    assert tuple(collection.items)[:3] == (
        "New-to-brand ad-attributed buyers (12 month lookback) · 14-day attribution · "
        "conversion date · advertised products",
        "Ad-attributed buyers · 14-day attribution · conversion date · advertised products",
        "Cost per new-to-brand buyer (12 month lookback)",
    )
    # No surface reads the share, so it isn't declared (FEAT-003).
    assert catalog.metric("ntb_buyer_share_12mo") is None


# --- Fixtures -------------------------------------------------------------------------------------


def test_the_fixture_is_one_delivery_per_complete_week_and_month() -> None:
    rows = tuple(csv.DictReader(io.StringIO((FIXTURES / f"{PACK_ID}.csv").read_text())))
    windows = [(row["time_window_start"], row["time_window_end"]) for row in rows]
    assert len(windows) == len(set(windows)) == 80
    suppressed = [row for row in rows if row["rows_suppressed"] == "true"]
    assert suppressed == [
        {
            "conversion_event_marketplace_name": "",
            "buyers_new_to_brand_12mo": "1",
            "buyers_attributed": "1",
            "time_window_start": "2026-08-24T00:00:00.000Z",
            "time_window_end": "2026-08-31T00:00:00.000Z",
            "distinct_user_count": "1",
            "suppression_reason": (
                "1 amazon_attributed_events_by_conversion_time users < 2 required"
            ),
            "rows_suppressed": "true",
        }
    ]
    assert {row["distinct_user_count"] for row in rows if row["rows_suppressed"] == "false"} == {
        "2"
    }
    assert NOT_RETURNED_WEEK.start.isoformat() == suppressed[0]["time_window_start"][:10]
