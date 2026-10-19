# SPDX-License-Identifier: MIT-0
"""New customers: pack v2, its pack-v1 policy, the coverage gate and NewCustomersArtifact v2.

The halo-inclusive gateway column, how pack-1 deliveries load, the planner's pack-v2 coverage gate
and the `NewCustomersArtifact` v2 domain contract (docs/ui/new-customers), including new buyers
per channel (manifest revision 2026-10-09.2).

Every figure is fictional and synthetic. The load tests run the real ingestion path (assessment,
then the sidecar) on in-memory tooling; the gate tests run the real planner and measurement tool.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
from publication_support import StubPublications
from test_reprocess_semantic_v2 import MemoryRaw
from test_semantic_sidecar import MemoryCanonical, _case  # pyright: ignore[reportPrivateUsage]

from adapters.inbound.tool_handlers.measurement import (
    MeasurementTool,
    MeasurementTooling,
    MeasurementToolRequest,
    attach_pack_coverage,
    handle,
)
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.jobs.contract_validation.loader import load_contracts, load_registry
from adapters.jobs.curation.manifests import (
    BuildManifestRequest,
    build_manifest,
    checksum_of,
    declared_columns_of,
    write_manifest,
)
from adapters.jobs.ingest_delivery.landing import LoadedDelivery
from adapters.jobs.ingest_delivery.semantic_ingestion import (
    SemanticIngestionRequest,
    ingest_semantic_delivery,
)
from adapters.jobs.ingest_delivery.semantic_sidecar import (
    SemanticSidecarOutcome,
    SemanticSidecarTooling,
)
from adapters.jobs.reprocess_semantic_v2 import __main__ as reprocess_cli
from adapters.jobs.reprocess_semantic_v2.job import ApplyReprocessRequest, apply_reprocess
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.publication_selection import PublicationSelector
from adapters.outbound.source_pack import load_source_packs
from adapters.producer.packs import installed_governance, load_query_packs, pack_by_id
from domain.catalog.capability import Catalog
from domain.curation.landing import (
    GOVERNED_MANIFEST_LAYOUT,
    LandedObject,
    LandingError,
    dataset_prefix,
)
from domain.data_contracts.contract import DataContract
from domain.measurement.pack_coverage import (
    PACK_COVERAGE_REFUSED,
    PackDayVersions,
    pack_coverage_verdict,
    pack_version_from_evidence,
)
from domain.measurement.planning import FactShape, PlanRequest, QueryPlan, plan
from domain.measurement.rosters import (
    Roster,
    RosterEntry,
    RosterKind,
    apply_roster,
    coarser_grain_set,
)
from domain.presentation.compared_metrics import new_customers_change_semantics
from domain.presentation.insights import (
    DETERMINISTIC_SLOT_IDS,
    DeterministicFinding,
    InsightAbsenceClass,
    InsightSlotId,
    InsightSlotState,
)
from domain.presentation.new_customers import (
    GATEWAY_CAPTIONS,
    GATEWAY_LIMIT,
    NO_CALENDAR_COMPARISON_TEXT,
    GatewayState,
    NewBuyers,
    NewBuyersAwaitingRequest,
    NewBuyersFiguresRequest,
    NewBuyersState,
    NewCustomersAbsenceReason,
    NewCustomersArtifact,
    NewCustomersFigure,
    NewCustomersLabels,
    NewCustomersMarket,
    NewCustomersOutcome,
    NtbShareMover,
    PerChannel,
    PerChannelAwaitingRequest,
    PerChannelFiguresRequest,
    PerChannelInput,
    PerChannelState,
    PortfolioAcquisitionSlotRequest,
    ShareChange,
    StepOneInput,
    gateway_entry,
    gateway_not_covered,
    new_buyers_awaiting,
    new_buyers_figures,
    new_buyers_outside_declared_periods,
    new_customers_artifact_payload,
    ntb_share_movers,
    per_channel_awaiting,
    per_channel_figures,
    portfolio_acquisition_cards,
    portfolio_acquisition_slot,
    resolve_new_buyers,
    step_one_row,
)
from domain.presentation.page_schemas import TemporalPresetId
from domain.semantics.calendar_periods import CalendarPeriod
from domain.semantics.reprocessing import (
    ContractArtifact,
    CreateReprocessPlanRequest,
    RawDeliveryBinding,
    RawObjectBinding,
    ReprocessOperationId,
    ReprocessPlan,
    ReprocessRequest,
)
from domain.semantics.source_aligned import LandedDeliveryRef
from domain.semantics.time import TimeGrain
from domain.semantics.validation import PrepublicationRuleFamily
from domain.shared_kernel.errors import InvariantError, ValidationError
from domain.shared_kernel.provenance import ProducerGovernance, Suppression
from domain.shared_kernel.values import DateRange
from domain.shopper_aggregates.loss import DEGRADED_NOTE, LossGrade
from ports.outbound.canonical import CanonicalDestination
from ports.outbound.pack_coverage import PackCoverageReadRequest
from ports.outbound.query import QueryRequest, QueryResult

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-100", "ADR-106")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/generated"
PACK_ID = "amc-attributed-conversion"
FACT = "fct_attributed_conversion"
COLUMN = "purchases_new_to_brand_incl_halo_12mo"
GATEWAY_METRIC = COLUMN
NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)
DAY = date(2026, 9, 14)
_V1_HEADER = (
    "conversion_event_date_utc,ad_product_type,channel_group_key,campaign_id_string,tracked_asin,"
    "conversion_event_marketplace_name,conversions,purchases,total_purchases,product_sales,"
    "total_product_sales,new_to_brand_product_sales,purchase_currency,units_sold,total_units_sold,"
    "new_to_brand_purchases,time_window_start,time_window_end,distinct_user_count,"
    "suppression_reason,rows_suppressed"
)
_V2_HEADER = _V1_HEADER.replace(
    "new_to_brand_purchases,", "new_to_brand_purchases,new_to_brand_total_purchases,"
)
_WINDOW = "2026-09-14T00:00:00.000Z,2026-09-15T00:00:00.000Z"
_KEY = "2026-09-14,sponsored_products,search,910000000000000001,B0FIXTUR00,AMAZON.COM"
_MEASURES = "38,19,23,584.3807,730.4759,292.1904,USD,38,57,9"
V1_ROW = f"{_KEY},{_MEASURES},{_WINDOW},2,,false"
V2_ROW = f"{_KEY},{_MEASURES},11,{_WINDOW},2,,false"
V2_NULL_ROW = f"{_KEY},{_MEASURES},,{_WINDOW},2,,false"


@pytest.fixture(scope="module")
def contracts() -> tuple[DataContract, ...]:
    return tuple(contract for _, contract in load_contracts(ROOT))


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return load_catalog(ROOT)


def _v1_governance() -> ProducerGovernance:
    (retained,) = pack_by_id(ROOT, PACK_ID).retained_governance
    return retained


def _delivery(
    lines: tuple[str, ...],
    governance: ProducerGovernance,
    retrieved_at: datetime = datetime(2026, 9, 16, tzinfo=UTC),
) -> LoadedDelivery:
    """Land one one-day attributed-conversion delivery under the given pack binding."""
    text = "".join(f"{line}\n" for line in lines)
    body = text.encode()
    declaration = json.loads((FIXTURES / f"{PACK_ID}.manifest.json").read_text())
    manifest = build_manifest(
        BuildManifestRequest(
            declaration={
                **declaration,
                "period_start": DAY.isoformat(),
                "period_end": DAY.isoformat(),
                "source_watermark": (DAY + timedelta(days=2)).isoformat(),
                "retrieved_at": retrieved_at.isoformat(),
            },
            objects=(LandedObject("data-0001.csv", len(body), checksum_of(body)),),
            declared_columns=declared_columns_of(text, source_format="csv"),
        )
    )
    manifest = replace(manifest, governance=governance, layout_version=GOVERNED_MANIFEST_LAYOUT)
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


@dataclass
class _Loaded:
    outcome: SemanticSidecarOutcome
    values: tuple[object, ...]
    """The column's value on every candidate fact row."""
    versions: tuple[int | None, ...]
    """The query pack version each candidate's publication evidence records."""
    contract_rule: str
    tooling: SemanticSidecarTooling | None = None


def _load(
    delivery: LoadedDelivery, governance: tuple[ProducerGovernance, ...] | None = None
) -> _Loaded:
    """Run the real ingestion path under the installed governance set (current and retained)."""
    _request, tooling, _order, _attempts = _case()
    results = ingest_semantic_delivery(
        SemanticIngestionRequest(
            delivery=delivery,
            contracts=tuple(
                contract for _, contract in load_contracts(ROOT) if contract.contract_id == PACK_ID
            ),
            columns=load_registry(ROOT),
            now=NOW,
            operation_id="new-customers-unit",
            execution_id="new-customers-execution",
            plan_digest="sha256:new-customers-plan",
            installed_governance=(
                installed_governance(load_query_packs(ROOT)) if governance is None else governance
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
    return _Loaded(
        outcome=results[0].outcome,
        values=tuple(dict(row).get(COLUMN, "absent") for item in candidates for row in item.rows),
        versions=tuple(
            pack_version_from_evidence(item.publication.evidence, PACK_ID) for item in candidates
        ),
        contract_rule=next(
            (
                result.detail
                for item in candidates
                for result in item.publication.evidence.results
                if result.rule_id == f"contract.{PACK_ID}.v1"
            ),
            "",
        ),
        tooling=tooling,
    )


# --- Pack v2 and its pack-v1 policy -------------------------------------------------------------


def test_pack_2_outputs_the_column_and_retains_its_version_1_binding() -> None:
    pack = pack_by_id(ROOT, PACK_ID)
    assert pack.pack_version == 2
    assert "new_to_brand_total_purchases" in pack.expected_columns
    assert pack.sql.count("SUM(new_to_brand_total_purchases) AS new_to_brand_total_purchases") == 1
    (retained,) = pack.retained_governance
    # The exact version-1 binding, derived by `load_query_packs` on the contracts at commit
    # 52126129 (the last commit at pack version 1).
    # The other pack-v1 tests read the retained entry itself, so only this pin catches a typo.
    assert retained == ProducerGovernance(
        query_pack_id=PACK_ID,
        query_pack_version=1,
        query_pack_digest="sha256:96097d501dd02cdd6f0dbba0675f0c0d2debfa5d4fb7bfd5bc396b193580d0a3",
        query_digest="sha256:6ca5453d9371377016352d6e0003046525ed20debece6937fba89c0889ea54b9",
        data_contract_id=PACK_ID,
        data_contract_digest=(
            "sha256:5ae9db31286085e0a49fcb71c13b88a3a7e4612435249d8d5d0906bf83589486"
        ),
        source_pack_id=PACK_ID,
        source_pack_digest="sha256:a567202154db952359866b98c39c35276be4bcea9b28d12bde82a5e744749219",
    )
    installed = installed_governance(load_query_packs(ROOT))
    assert installed.count(pack.governance) == 1
    assert installed.count(retained) == 1
    assert retained != pack.governance


def test_a_pack_1_delivery_loads_with_the_column_null_and_passes_dq() -> None:
    loaded = _load(_delivery((_V1_HEADER, V1_ROW), _v1_governance()))
    assert loaded.outcome is SemanticSidecarOutcome.PUBLISHED
    assert loaded.values == (None,)
    assert loaded.versions == (1,)
    assert loaded.contract_rule == "Source contract assertions passed."
    # Positive control: the same row from pack 2 carries the column and records version 2.
    current = _load(_delivery((_V2_HEADER, V2_ROW), pack_by_id(ROOT, PACK_ID).governance))
    assert current.outcome is SemanticSidecarOutcome.PUBLISHED
    assert current.values == (11,)
    assert current.versions == (2,)


def test_a_pack_2_kept_row_with_the_column_null_is_rejected() -> None:
    loaded = _load(_delivery((_V2_HEADER, V2_NULL_ROW), pack_by_id(ROOT, PACK_ID).governance))
    assert loaded.outcome is SemanticSidecarOutcome.BLOCKED
    assert loaded.contract_rule == (
        f"{PACK_ID}: 1 row(s) carry no {COLUMN}, but query pack version 2 outputs it (present "
        "from version 2). Only a delivery from an earlier pack version may leave it NULL."
    )
    # Positive control: the same NULL from pack 1 is its declared null meaning and publishes.
    v1 = _load(_delivery((_V1_HEADER, V1_ROW), _v1_governance()))
    assert v1.outcome is SemanticSidecarOutcome.PUBLISHED


def test_a_re_land_of_a_pack_1_window_loads_with_the_column_null() -> None:
    first = _load(_delivery((_V1_HEADER, V1_ROW), _v1_governance()))
    again = _load(
        _delivery((_V1_HEADER, V1_ROW), _v1_governance(), datetime(2026, 9, 18, tzinfo=UTC))
    )
    assert (first.outcome, again.outcome) == (SemanticSidecarOutcome.PUBLISHED,) * 2
    assert (first.values, again.values) == ((None,), (None,))
    assert again.versions == (1,)


def _reprocess(governance: tuple[ProducerGovernance, ...]) -> _Loaded:
    """Apply an exact reprocess of one landed pack-1 delivery through the real sidecar."""
    delivery = _delivery((_V1_HEADER, V1_ROW), _v1_governance())
    manifest = delivery.manifest
    manifest_body = write_manifest(manifest).encode()
    body = delivery.text.encode()
    loaded_contracts = load_contracts(ROOT)
    artifacts = tuple(
        ContractArtifact(contract.contract_id, checksum_of(path.read_bytes()))
        for path, contract in loaded_contracts
    )
    contract = next(item for _, item in loaded_contracts if item.contract_id == PACK_ID)
    binding = RawDeliveryBinding(
        contract_artifacts=tuple(item for item in artifacts if item.contract_id == PACK_ID),
        manifest_key=manifest.manifest_key,
        manifest_version_id="manifest-v1",
        manifest_checksum=checksum_of(manifest_body),
        first_observed_at=manifest.retrieved_at,
        object=RawObjectBinding(
            key=manifest.object_keys()[0],
            version_id="object-v1",
            name="data-0001.csv",
            declared_byte_count=len(body),
            actual_byte_count=len(body),
            declared_checksum=checksum_of(body),
            actual_checksum=checksum_of(body),
        ),
        period=DateRange(DAY, DAY),
    )
    authority = reprocess_cli._semantic_authority_digest()  # pyright: ignore[reportPrivateUsage]
    plan = ReprocessPlan.create(
        CreateReprocessPlanRequest(
            request=ReprocessRequest(
                environment="dev",
                stack_name="Adlytics-Foundation-dev",
                bucket="explicit-versioned-raw",
                contracts=(PACK_ID,),
                window=DateRange(DAY, DAY),
                operation_id=ReprocessOperationId("new-customers-v1-replay"),
            ),
            prefixes=(dataset_prefix(contract),),
            contract_artifacts=binding.contract_artifacts,
            semantic_authority_digest=authority,
            deliveries=(binding,),
        )
    )
    _request, tooling, _order, _attempts = _case()
    apply_reprocess(
        ApplyReprocessRequest(
            plan=plan,
            contracts=tuple(item for _, item in loaded_contracts),
            registry=load_registry(ROOT),
            raw=MemoryRaw(
                (),
                {
                    (manifest.manifest_key, "manifest-v1"): manifest_body,
                    (manifest.object_keys()[0], "object-v1"): body,
                },
            ),
            semantic=tooling,
            execution_id="new-customers-reprocess",
            applied_at=NOW,
            installed_governance=governance,
            contract_artifacts=artifacts,
            semantic_authority_digest=authority,
        )
    )
    canonical = tooling.canonical_writer
    assert isinstance(canonical, MemoryCanonical)
    candidates = tuple(
        item
        for item in canonical.requests
        if item.destination is CanonicalDestination.CANDIDATE
        and item.publication.partition.scope.dataset.value == FACT
    )
    return _Loaded(
        outcome=SemanticSidecarOutcome.PUBLISHED,
        values=tuple(dict(row).get(COLUMN, "absent") for item in candidates for row in item.rows),
        versions=tuple(
            pack_version_from_evidence(item.publication.evidence, PACK_ID) for item in candidates
        ),
        contract_rule=next(
            (
                result.outcome.value
                for item in candidates
                for result in item.publication.evidence.results
                if result.family is PrepublicationRuleFamily.TABLE_DQ
            ),
            "",
        ),
    )


def test_a_reprocess_of_pack_1_evidence_loads_with_the_column_null_and_passes_dq() -> None:
    reprocessed = _reprocess(installed_governance(load_query_packs(ROOT)))
    assert reprocessed.values == (None,)
    assert reprocessed.versions == (1,)
    assert reprocessed.contract_rule == "pass"
    # Control: without the retained version-1 binding the reprocess refuses before any write.
    with pytest.raises(LandingError, match="governed manifest artifacts differ"):
        _reprocess((pack_by_id(ROOT, PACK_ID).governance,))


def test_a_pack_1_delivery_is_refused_without_the_retained_binding() -> None:
    """The retained binding is what admits pack-1 evidence; without it the load refuses."""
    current_only = (pack_by_id(ROOT, PACK_ID).governance,)
    with pytest.raises(LandingError, match="governed manifest artifacts differ"):
        _load(_delivery((_V1_HEADER, V1_ROW), _v1_governance()), current_only)
    # Positive control: the installed set (current and retained) admits it.
    assert _load(_delivery((_V1_HEADER, V1_ROW), _v1_governance())).outcome is (
        SemanticSidecarOutcome.PUBLISHED
    )


# --- The pack-v2 coverage gate, in the planner ---------------------------------------------------

WINDOW = DateRange(date(2026, 9, 14), date(2026, 9, 20))
GATEWAY_LABEL = "New-to-brand purchases including brand halo (12 month lookback)"
_TAIL = "They appear once every day of a window is measured."


def _days(window: DateRange, version: int) -> dict[date, int | None]:
    return {window.start + timedelta(days=offset): version for offset in range(window.days)}


def _coverage(versions: dict[date, int | None]) -> tuple[PackDayVersions, ...]:
    return (PackDayVersions(PACK_ID, versions),)


def _plan_gateway(  # noqa: PLR0913, PLR0917 — the catalog, shapes, evidence and metric
    catalog: Catalog,
    contracts: tuple[DataContract, ...],
    coverage: tuple[PackDayVersions, ...] | None,
    metric: str = GATEWAY_METRIC,
) -> QueryPlan:
    return plan(
        catalog,
        PlanRequest(
            metric,
            ("channel", "ad_product", "product"),
            WINDOW,
            {"marketplace_key": "amazon_us"},
            pack_coverage=coverage,
        ),
        FactShape.for_fact(FACT, contracts),
    )


def _refusal(call: object) -> ValidationError:
    assert isinstance(call, ValidationError)
    return call


def test_a_window_with_a_pack_1_day_is_refused_naming_the_first_covered_date(
    catalog: Catalog, contracts: tuple[DataContract, ...]
) -> None:
    versions = {**_days(WINDOW, 2), WINDOW.start: 1}
    with pytest.raises(ValidationError) as refused:
        _plan_gateway(catalog, contracts, _coverage(versions))
    error = _refusal(refused.value)
    assert error.code == PACK_COVERAGE_REFUSED
    assert error.description == (
        f"{GATEWAY_LABEL} are measured from 15 Sep 2026. This window starts earlier, so they "
        f"aren't shown. {_TAIL}"
    )
    assert error.context["first_covered_date"] == "2026-09-15"
    # Positive control: the same window with every day at pack version 2 plans.
    planned = _plan_gateway(catalog, contracts, _coverage(_days(WINDOW, 2)))
    assert planned.group_by == ("channel_key", "ad_product_key", "amazon_asin")


def test_a_pack_1_last_day_or_no_covered_day_reads_its_own_text(
    catalog: Catalog, contracts: tuple[DataContract, ...]
) -> None:
    last = {**_days(WINDOW, 2), WINDOW.end: 1}
    with pytest.raises(ValidationError) as refused:
        _plan_gateway(catalog, contracts, _coverage(last))
    assert _refusal(refused.value).description == (
        f"{GATEWAY_LABEL} aren't measured for every day of this window, so they aren't shown. "
        f"{_TAIL}"
    )
    with pytest.raises(ValidationError) as none:
        _plan_gateway(catalog, contracts, _coverage(_days(WINDOW, 1)))
    assert _refusal(none.value).description == f"{GATEWAY_LABEL} aren't measured yet. {_TAIL}"
    # A day with no current publication isn't covered either; its context says so.
    missing = {key: value for key, value in _days(WINDOW, 2).items() if key != WINDOW.end}
    with pytest.raises(ValidationError) as absent:
        _plan_gateway(catalog, contracts, _coverage(missing))
    assert _refusal(absent.value).context["first_covered_date"] is None
    assert _refusal(absent.value).context["any_day_covered"] is True


def test_the_gate_refuses_a_runtime_without_coverage_and_leaves_step_1_untouched(
    catalog: Catalog, contracts: tuple[DataContract, ...]
) -> None:
    with pytest.raises(InvariantError, match="supplied no pack coverage"):
        _plan_gateway(catalog, contracts, None)
    # Positive control: step 1's advertised-only count reads no versioned column and plans.
    planned = _plan_gateway(catalog, contracts, None, metric="purchases_new_to_brand_12mo")
    assert planned.measures == ("purchases_new_to_brand_12mo",)


def test_a_covered_day_is_decided_by_its_pack_version_never_by_rows() -> None:
    """Row presence plays no part: the verdict reads only each day's published version."""
    covered = pack_coverage_verdict(WINDOW, 2, _days(WINDOW, 2))
    assert (covered.covered, covered.first_covered_date) == (True, WINDOW.start)
    later = pack_coverage_verdict(WINDOW, 2, {**_days(WINDOW, 2), WINDOW.start: 1})
    assert (later.covered, later.first_covered_date, later.any_day_covered) == (
        False,
        WINDOW.start + timedelta(days=1),
        True,
    )
    # A pack-1 re-run landing on an early day after version 2 keeps the text true: the first
    # covered date is the earliest day from which every later day is covered.
    rerun = pack_coverage_verdict(
        WINDOW, 2, {**_days(WINDOW, 2), WINDOW.start + timedelta(days=2): 1}
    )
    assert rerun.first_covered_date == WINDOW.start + timedelta(days=3)


def test_pack_coverage_is_read_from_publication_state(
    contracts: tuple[DataContract, ...],
) -> None:
    """The selector reads the version the load recorded on the day's CURRENT publication."""
    day = DateRange(DAY, DAY)
    contract = next(item for item in contracts if item.contract_id == PACK_ID)
    for lines, governance, expected in (
        ((_V2_HEADER, V2_ROW), pack_by_id(ROOT, PACK_ID).governance, 2),
        ((_V1_HEADER, V1_ROW), _v1_governance(), 1),
    ):
        loaded = _load(_delivery(lines, governance))
        assert loaded.tooling is not None
        selector = PublicationSelector(
            loaded.tooling.publications,
            load_source_packs(ROOT),
            load_commerce_identity_bridge(ROOT),
        )
        (coverage,) = selector.pack_coverage(
            PackCoverageReadRequest((contract,), {"marketplace_key": "amazon_us"}, (day,))
        )
        assert coverage == PackDayVersions(PACK_ID, {DAY: expected})
    # Control: a day with no CURRENT publication reads None, never a version.
    later = DAY + timedelta(days=1)
    (empty,) = selector.pack_coverage(
        PackCoverageReadRequest(
            (contract,), {"marketplace_key": "amazon_us"}, (DateRange(later, later),)
        )
    )
    assert empty == PackDayVersions(PACK_ID, {DAY + timedelta(days=1): None})


@dataclass
class _Coverage:
    versions: dict[date, int | None]
    seen: list[PackCoverageReadRequest]

    def pack_coverage(self, request: PackCoverageReadRequest) -> tuple[PackDayVersions, ...]:
        self.seen.append(request)
        return tuple(PackDayVersions(item.contract_id, self.versions) for item in request.contracts)


@dataclass
class _NoRows:
    def run(self, request: QueryRequest) -> QueryResult:
        del request
        return QueryResult(
            rows=(), bytes_scanned=1, truncated=False, suppression=Suppression(False)
        )


def _chat(
    catalog: Catalog, contracts: tuple[DataContract, ...], versions: dict[date, int | None]
) -> tuple[object, _Coverage]:
    coverage = _Coverage(versions, [])
    request = MeasurementToolRequest(
        tool=MeasurementTool.METRIC_BY_GRAIN,
        catalog=catalog,
        contracts=contracts,
        metric_name=GATEWAY_METRIC,
        grains=("channel", "ad_product"),
        window=WINDOW,
        computed_at=NOW,
        correlation_id="new-customers-chat",
        filters={"marketplace_key": "amazon_us"},
    )
    tooling = MeasurementTooling(
        queries=_NoRows(), publications=StubPublications(), pack_coverage=coverage
    )
    try:
        return handle(request, tooling), coverage
    except ValidationError as error:
        return error, coverage


def test_chat_answers_an_uncovered_window_with_the_designed_absence_text(
    catalog: Catalog, contracts: tuple[DataContract, ...]
) -> None:
    refused, coverage = _chat(catalog, contracts, {**_days(WINDOW, 2), WINDOW.start: 1})
    assert isinstance(refused, ValidationError)
    assert refused.description == (
        f"{GATEWAY_LABEL} are measured from 15 Sep 2026. This window starts earlier, so they "
        f"aren't shown. {_TAIL}"
    )
    (read,) = coverage.seen
    assert (read.windows, dict(read.filters)) == ((WINDOW,), {"marketplace_key": "amazon_us"})
    # Positive control: a covered window answers.
    answered, _ = _chat(catalog, contracts, _days(WINDOW, 2))
    assert not isinstance(answered, ValidationError)
    # A daily metric reading no versioned column never consults the reader.
    step_one = MeasurementToolRequest(
        tool=MeasurementTool.METRIC_BY_GRAIN,
        catalog=catalog,
        contracts=contracts,
        metric_name="purchases_new_to_brand_12mo",
        grains=("channel",),
        window=WINDOW,
        computed_at=NOW,
        correlation_id="new-customers-chat",
    )
    unread = _Coverage({}, [])
    assert attach_pack_coverage(step_one, unread) is step_one
    assert unread.seen == []


# --- NewCustomersArtifact v1 -----------------------------------------------------------------

USD = "USD"
_LABELS = NewCustomersLabels("Amazon", 14, 12, False, True)
SHARE_SEMANTICS = new_customers_change_semantics(load_catalog(ROOT)).for_metric("ntb_revenue_share")


def _input(**changes: object) -> StepOneInput:
    base = StepOneInput(
        ad_product_key="sponsored_products",
        ad_product_label="Sponsored Products",
        channel_group_label="Search",
        currency_code=USD,
        ntb_purchases=Decimal(40),
        ntb_revenue=Decimal("1200.50"),
        attributed_revenue=Decimal("3000"),
        share=Decimal("0.4"),
        cost_per_ntb=Decimal("12.5"),
        compared=True,
        prior_share=Decimal("0.35"),
        window_text="14 Sep 2026 \u2013 20 Sep 2026",
        prior_text="7 Sep 2026 \u2013 13 Sep 2026",
        share_semantics=SHARE_SEMANTICS,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def test_a_step_1_row_carries_every_figure_and_the_share_change() -> None:
    row = step_one_row(_input())
    assert (row.ntb_purchases.value, row.ntb_revenue.value) == (Decimal(40), Decimal("1200.50"))
    assert row.ntb_revenue.display_value == "1200.50"
    assert row.ntb_revenue_share.display_text == "40.00%"
    change = row.ntb_revenue_share_change
    assert isinstance(change, ShareChange)
    assert (change.points, change.direction.value, change.display_text) == (
        Decimal("5.00"),
        "up",
        "+5.0 pts",
    )
    assert row.cost_per_ntb_purchase.value == Decimal("12.5")


def test_the_share_change_is_adr_121s_block_toned_by_the_dictionary() -> None:
    # The dictionary declares more new-to-brand share favourable (plan D4).
    rise = step_one_row(_input()).ntb_revenue_share_change
    fall = step_one_row(_input(prior_share=Decimal("0.45"))).ntb_revenue_share_change
    flat = step_one_row(_input(prior_share=Decimal("0.4"))).ntb_revenue_share_change
    assert isinstance(rise, ShareChange) and isinstance(fall, ShareChange)
    assert isinstance(flat, ShareChange)
    assert [
        (item.change.direction.value, item.change.sentiment.value, item.change.display_text)
        for item in (rise, fall, flat)
    ] == [
        ("up", "favourable", "+5.0 pts"),
        ("down", "unfavourable", "\u22125.0 pts"),
        ("flat", "neutral", "No change"),
    ]
    # Refusal: a compared share without its governed semantics, beside the calendar row that
    # needs none.
    with pytest.raises(ValueError, match="declares its change semantics"):
        step_one_row(_input(share_semantics=None))
    calendar = step_one_row(_input(compared=False, prior_share=None, share_semantics=None))
    assert not isinstance(calendar.ntb_revenue_share_change, ShareChange)


def test_each_step_1_absence_gives_exactly_its_text_beside_a_row_with_the_figure() -> None:
    no_revenue = step_one_row(_input(attributed_revenue=Decimal(0), share=None))
    assert no_revenue.ntb_revenue_share.absence is not None
    assert no_revenue.ntb_revenue_share.absence.text == (
        "No attributed revenue in this window, so there's no share."
    )
    no_prior = step_one_row(_input(prior_share=None))
    absent = no_prior.ntb_revenue_share_change
    assert not isinstance(absent, ShareChange)
    assert (absent.reason, absent.text) == (
        NewCustomersAbsenceReason.NO_SHARE_TO_COMPARE,
        "No new-to-brand revenue share for 7 Sep 2026 \u2013 13 Sep 2026, so there's no change.",
    )
    no_ntb = step_one_row(_input(ntb_purchases=Decimal(0), cost_per_ntb=None))
    assert no_ntb.cost_per_ntb_purchase.absence is not None
    assert no_ntb.cost_per_ntb_purchase.absence.text == "No new-to-brand purchases in this window."
    # Positive control: the full row has every figure.
    full = step_one_row(_input())
    assert all(
        figure.absence is None for figure in (full.ntb_revenue_share, full.cost_per_ntb_purchase)
    )


def test_a_covered_ad_product_with_no_conversion_row_reads_true_zeros() -> None:
    row = step_one_row(
        _input(
            ntb_purchases=None,
            ntb_revenue=None,
            attributed_revenue=None,
            share=None,
            cost_per_ntb=None,
            prior_share=None,
        )
    )
    assert (row.ntb_purchases.value, row.ntb_revenue.value) == (Decimal(0), Decimal(0))
    assert row.ntb_revenue_share.absence is not None
    assert row.ntb_revenue_share.absence.reason is NewCustomersAbsenceReason.NO_ATTRIBUTED_REVENUE
    assert row.cost_per_ntb_purchase.absence is not None
    assert row.cost_per_ntb_purchase.absence.reason is NewCustomersAbsenceReason.NO_NTB_PURCHASES
    # Refusal: a planned share beside zero attributed revenue contradicts its operands.
    with pytest.raises(ValueError, match="planned exactly when attributed revenue"):
        step_one_row(_input(attributed_revenue=Decimal(0)))


def test_a_calendar_preset_shows_no_change_column() -> None:
    row = step_one_row(_input(compared=False, prior_share=None))
    change = row.ntb_revenue_share_change
    assert not isinstance(change, ShareChange)
    assert change.reason is NewCustomersAbsenceReason.CALENDAR_PERIOD_SELECTED


WEEK = CalendarPeriod(date(2026, 9, 14), date(2026, 9, 20), TimeGrain.WEEK)


def _channel(key: str, label: str, **changes: object) -> PerChannelInput:
    base = PerChannelInput(
        ad_product_key=key,
        ad_product_label=label,
        channel_group_label="Search",
        buyers=Decimal(300),
        cost=Decimal(10),
        on_roster=True,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


SP_ROW = _channel("sponsored_products", "Sponsored Products")
SB_ROW = _channel("sponsored_brands", "Sponsored Brands", buyers=Decimal(120))


def _table(*rows: PerChannelInput, note: str | None = None) -> PerChannel:
    return per_channel_figures(PerChannelFiguresRequest(WEEK, USD, rows, note, ""))


PER_CHANNEL = _table(SP_ROW)


def test_a_rolling_preset_gives_the_new_buyers_callout_and_its_action() -> None:
    section = new_buyers_outside_declared_periods((TimeGrain.WEEK, TimeGrain.MONTH))
    assert section.state is NewBuyersState.OUTSIDE_DECLARED_PERIODS
    assert section.text == (
        "New buyers are measured for complete weeks and months. Choose Latest measured week to "
        "see them."
    )
    assert section.action is not None
    assert (section.action.label, section.action.preset) == (
        "Show Latest measured week",
        TemporalPresetId.LAST_COMPLETE_WEEK,
    )
    assert (section.new_buyers, section.cost_per_new_buyer) == (None, None)


def test_a_calendar_preset_gives_both_tiles_and_not_returned_reads_too_few_shoppers() -> None:
    section = new_buyers_figures(
        NewBuyersFiguresRequest(
            WEEK,
            buyers=Decimal(4400),
            cost=Decimal("1.25"),
            spend=Decimal(5500),
            currency_code=USD,
            caveat="",
            per_channel=PER_CHANNEL,
        )
    )
    assert section.state is NewBuyersState.FIGURES
    assert section.new_buyers is not None and section.cost_per_new_buyer is not None
    assert (section.new_buyers.value, section.cost_per_new_buyer.value) == (
        Decimal(4400),
        Decimal("1.25"),
    )
    assert section.cost_basis == (
        "Ad spend ÷ new buyers · Week of 14\u201320 Sep 2026 · 5500.00 USD ÷ 4400"
    )
    missing = new_buyers_figures(
        NewBuyersFiguresRequest(
            WEEK,
            buyers=None,
            cost=None,
            spend=None,
            currency_code=USD,
            caveat="",
            per_channel=PER_CHANNEL,
        )
    )
    assert missing.new_buyers is not None and missing.cost_per_new_buyer is not None
    for tile in (missing.new_buyers, missing.cost_per_new_buyer):
        assert tile.absence is not None
        assert (tile.absence.reason, tile.absence.text) == (
            NewCustomersAbsenceReason.NOT_RETURNED,
            "too few shoppers, or none",
        )


# --- Per-channel new buyers (v2) ---------------------------------------------------------------

_NOT_RETURNED = ("not_returned", "too few shoppers, or none")


def _absences(table: PerChannel) -> list[tuple[str, object, object]]:
    def cell(figure: NewCustomersFigure) -> object:
        absence = figure.absence
        return figure.value if absence is None else (absence.reason.value, absence.text)

    return [
        (row.ad_product_key, cell(row.new_buyers), cell(row.cost_per_new_buyer))
        for row in table.rows
    ]


def test_a_rostered_ad_product_with_no_row_reads_too_few_shoppers_in_both_cells() -> None:
    table = _table(SP_ROW, replace(SB_ROW, buyers=None, cost=None))
    assert table.state is PerChannelState.ROWS
    assert _absences(table) == [
        ("sponsored_products", Decimal(300), Decimal(10)),
        ("sponsored_brands", _NOT_RETURNED, _NOT_RETURNED),
    ]
    # Paired: the same ad product with its row gives both figures.
    assert _absences(_table(SP_ROW, SB_ROW))[1] == ("sponsored_brands", Decimal(120), Decimal(10))


def test_one_of_four_roster_entries_missing_is_graded_degraded_and_never_withdrawn() -> None:
    entries = tuple(
        RosterEntry(key, key.replace("_", " "))
        for key in ("amazon_dsp", "sponsored_products", "sponsored_brands", "sponsored_display")
    )
    roster = Roster(RosterKind.AD_PRODUCT, WINDOW, "amazon_us", "amazon_ads", entries)
    coarser = coarser_grain_set("fct_new_to_brand_buyers", "channel")
    outcome = apply_roster(
        roster, ("amazon_dsp", "sponsored_products", "sponsored_brands"), coarser
    )
    assert (outcome.loss.grade, outcome.loss.pointer) == (LossGrade.DEGRADED, None)
    assert [entry.key for entry in outcome.absent] == ["sponsored_display"]
    table = _table(SP_ROW, note=outcome.note)
    assert table.note == DEGRADED_NOTE == "Some breakdowns had too few shoppers and aren't shown."
    # Control: every roster entry returned grades not degraded, so no note shows.
    whole = apply_roster(roster, tuple(entry.key for entry in entries), coarser)
    assert (whole.loss.grade, whole.note) == (LossGrade.NOT_DEGRADED, None)


def test_a_returned_ad_product_off_the_roster_reads_the_no_spend_text() -> None:
    table = _table(SP_ROW, replace(SB_ROW, cost=None, on_roster=False))
    assert _absences(table)[1] == (
        "sponsored_brands",
        Decimal(120),
        ("no_ad_spend", "No ad spend for Sponsored Brands in Week of 14\u201320 Sep 2026."),
    )
    # Paired: on the roster, its cost renders.
    assert _absences(_table(SP_ROW, SB_ROW))[1][2] == Decimal(10)


def test_a_returned_ad_product_with_spend_and_0_new_buyers_reads_no_new_buyers() -> None:
    """A row above the bar can hold 0 new buyers; its cost divides by zero and is absent typed."""
    zero = replace(SB_ROW, buyers=Decimal(0), cost=None)
    assert _absences(_table(SP_ROW, zero)) == [
        # Paired: a row with new buyers keeps its numeric cost beside it.
        ("sponsored_products", Decimal(300), Decimal(10)),
        (
            "sponsored_brands",
            Decimal(0),
            ("no_new_buyers", "No new buyers for Sponsored Brands in Week of 14\u201320 Sep 2026."),
        ),
    ]
    # Paired: a rostered ad product that genuinely didn't come back still reads not_returned.
    assert _absences(_table(SP_ROW, replace(SB_ROW, buyers=None, cost=None)))[1] == (
        "sponsored_brands",
        _NOT_RETURNED,
        _NOT_RETURNED,
    )
    # A cost beside 0 new buyers would be a fabricated division: refused.
    with pytest.raises(ValueError, match="no new buyers have no cost per new buyer"):
        replace(SB_ROW, buyers=Decimal(0))
    # Off the roster, the no-spend reason still wins over the zero.
    off = replace(SB_ROW, buyers=Decimal(0), cost=None, on_roster=False)
    assert _absences(_table(off))[0][2][0] == "no_ad_spend"  # type: ignore[index]


def test_the_tiles_with_spend_and_0_new_buyers_read_no_new_buyers() -> None:
    def tiles(buyers: Decimal | None, cost: Decimal | None) -> NewBuyers:
        return new_buyers_figures(
            NewBuyersFiguresRequest(
                WEEK,
                buyers=buyers,
                cost=cost,
                spend=None if buyers is None else Decimal(5500),
                currency_code=USD,
                caveat="",
                per_channel=PER_CHANNEL,
            )
        )

    zero = tiles(Decimal(0), None)
    assert zero.new_buyers is not None and zero.cost_per_new_buyer is not None
    assert zero.new_buyers.value == Decimal(0)
    assert zero.cost_per_new_buyer.absence is not None
    assert (zero.cost_per_new_buyer.absence.reason, zero.cost_per_new_buyer.absence.text) == (
        NewCustomersAbsenceReason.NO_NEW_BUYERS,
        "No new buyers in Week of 14\u201320 Sep 2026.",
    )
    assert zero.per_channel == PER_CHANNEL
    # Paired: new buyers above 0 give a numeric cost.
    some = tiles(Decimal(4400), Decimal("1.25"))
    assert some.cost_per_new_buyer is not None
    assert some.cost_per_new_buyer.value == Decimal("1.25")
    # Paired: a cell that didn't come back still reads not_returned on both tiles.
    missing = tiles(None, None)
    assert missing.cost_per_new_buyer is not None and missing.cost_per_new_buyer.absence
    assert missing.cost_per_new_buyer.absence.reason is NewCustomersAbsenceReason.NOT_RETURNED


def test_an_empty_roster_with_no_row_gives_no_channels_and_one_returned_row_gives_rows() -> None:
    empty = _table()
    assert (empty.state, empty.text, empty.rows, empty.note) == (
        PerChannelState.NO_CHANNELS,
        "No channel had ad spend in Week of 14\u201320 Sep 2026.",
        (),
        None,
    )
    one = _table(replace(SB_ROW, cost=None, on_roster=False))
    assert (one.state, [row.ad_product_key for row in one.rows]) == (
        PerChannelState.ROWS,
        ["sponsored_brands"],
    )


def test_a_pending_and_a_missing_per_channel_delivery_give_their_texts_while_the_tiles_render() -> (
    None
):
    expected = WEEK.end + timedelta(days=2)
    pending = per_channel_awaiting(PerChannelAwaitingRequest(WEEK, expected, None, expected))
    assert (pending.state, pending.text) == (
        PerChannelState.PENDING,
        "Figures per channel for Week of 14\u201320 Sep 2026 are expected after 22 Sep 2026.",
    )
    section = new_buyers_figures(
        NewBuyersFiguresRequest(
            WEEK,
            buyers=Decimal(4400),
            cost=Decimal("1.25"),
            spend=Decimal(5500),
            currency_code=USD,
            caveat="",
            per_channel=pending,
        )
    )
    assert section.new_buyers is not None and section.new_buyers.value == Decimal(4400)
    late = resolve_new_buyers(section, expected + timedelta(days=1))
    assert late.per_channel is not None
    assert (late.per_channel.state, late.per_channel.text) == (
        PerChannelState.MISSING,
        "Figures per channel for Week of 14\u201320 Sep 2026 didn't arrive.",
    )
    assert late.new_buyers == section.new_buyers
    # Control: on its expected day it's still pending, and a table with rows is left as baked.
    assert resolve_new_buyers(section, expected) == section
    listed = replace(section, per_channel=PER_CHANNEL)
    assert resolve_new_buyers(listed, expected + timedelta(days=30)) == listed


def test_the_per_channel_table_nests_under_the_tiles_and_shows_their_period() -> None:
    figures = new_buyers_figures(
        NewBuyersFiguresRequest(
            WEEK,
            buyers=Decimal(4400),
            cost=Decimal("1.25"),
            spend=Decimal(5500),
            currency_code=USD,
            caveat="",
            per_channel=PER_CHANNEL,
        )
    )
    assert figures.per_channel == PER_CHANNEL
    with pytest.raises(ValueError, match="nests under the tiles"):
        replace(figures, per_channel=None)
    other = CalendarPeriod(date(2026, 9, 7), date(2026, 9, 13), TimeGrain.WEEK)
    with pytest.raises(ValueError, match="shows the tiles' period"):
        replace(figures, per_channel=replace(PER_CHANNEL, period=other))
    # A callout carries no table.
    with pytest.raises(ValueError, match="nests under the tiles"):
        replace(new_buyers_outside_declared_periods((TimeGrain.WEEK,)), per_channel=PER_CHANNEL)


def _calendar_input(**changes: object) -> StepOneInput:
    return _input(compared=False, prior_share=None, **changes)


_SB_INPUT: dict[str, object] = {
    "ad_product_key": "sponsored_brands",
    "ad_product_label": "Sponsored Brands",
}


def _week_market(steps: tuple[StepOneInput, ...], *rows: PerChannelInput) -> NewCustomersMarket:
    return replace(
        _market(steps),
        new_buyers=new_buyers_figures(
            NewBuyersFiguresRequest(
                WEEK,
                buyers=Decimal(4400),
                cost=Decimal("1.25"),
                spend=Decimal(5500),
                currency_code=USD,
                caveat="",
                per_channel=_table(*rows),
            )
        ),
    )


def test_a_per_channel_row_is_a_step_1_ad_product_or_one_with_no_ad_spend() -> None:
    """NC-GATE-103: the bake refuses the row the browser's decoder refuses, so it alarms there."""
    sp, sb = _calendar_input(), _calendar_input(**_SB_INPUT)
    no_spend = replace(SB_ROW, cost=None, on_roster=False)
    # Accepted: each rostered row is in step 1, and a row with no ad spend is not.
    assert _week_market((sp, sb), SP_ROW, SB_ROW).new_buyers.per_channel is not None
    assert _week_market((sp,), SP_ROW, no_spend).new_buyers.per_channel is not None
    with pytest.raises(ValueError, match="a step-1 ad product, or one with no ad spend"):
        _week_market((sp,), SP_ROW, SB_ROW)
    with pytest.raises(ValueError, match="a step-1 ad product, or one with no ad spend"):
        _week_market((sp, sb), SP_ROW, no_spend)


def test_the_v2_payload_carries_the_per_channel_table_and_no_cross_channel_total() -> None:
    calendar_market = replace(
        _market((_calendar_input(), _calendar_input(**_SB_INPUT))),
        new_buyers=new_buyers_figures(
            NewBuyersFiguresRequest(
                WEEK,
                buyers=Decimal(4400),
                cost=Decimal("1.25"),
                spend=Decimal(5500),
                currency_code=USD,
                caveat="",
                per_channel=_table(SP_ROW, SB_ROW),
            )
        ),
    )
    artifact = replace(
        _artifact((_market(),)),
        preset=TemporalPresetId.LAST_COMPLETE_WEEK,
        prior_period=None,
        markets=(calendar_market,),
    )
    payload = new_customers_artifact_payload(artifact)
    (market,) = cast("list[dict[str, object]]", payload["markets"])
    table = cast(
        "dict[str, object]", cast("dict[str, object]", market["new_buyers"])["per_channel"]
    )
    assert sorted(table) == [
        "caption",
        "caption_detail",
        "cost_basis",
        "cost_header",
        "expected_after",
        "foot",
        "latest_available_period",
        "note",
        "rows",
        "state",
        "text",
    ]
    assert (table["state"], table["caption"], table["foot"]) == (
        "rows",
        "New buyers per channel",
        "New buyers aren't added across channels, because one shopper can buy after ads on more "
        "than one channel.",
    )
    assert table["cost_basis"] == "Ad spend ÷ new buyers · Week of 14\u201320 Sep 2026"
    rows = cast("list[dict[str, object]]", table["rows"])
    assert [sorted(row) for row in rows] == [
        [
            "ad_product_key",
            "ad_product_label",
            "channel_group_label",
            "cost_per_new_buyer",
            "new_buyers",
        ]
    ] * 2
    assert [cast("dict[str, object]", row["new_buyers"])["value"] for row in rows] == ["300", "120"]
    # No key anywhere in the section names a total or subtotal row.
    assert "total" not in json.dumps(market["new_buyers"])


def test_a_pending_period_reads_expected_after_and_turns_missing_at_serve_time() -> None:
    expected = WEEK.end + timedelta(days=2)
    pending = new_buyers_awaiting(
        NewBuyersAwaitingRequest(
            WEEK, expected_after=expected, latest_available_period=None, as_of=expected
        )
    )
    assert (pending.state, pending.text) == (
        NewBuyersState.PENDING,
        "Figures for Week of 14\u201320 Sep 2026 are expected after 22 Sep 2026.",
    )
    late = resolve_new_buyers(pending, expected + timedelta(days=1))
    assert (late.state, late.text) == (
        NewBuyersState.MISSING,
        "Figures for Week of 14\u201320 Sep 2026 didn't arrive.",
    )
    # Positive control: on its expected day it's still pending.
    assert resolve_new_buyers(pending, expected) == pending


def test_gateway_rows_are_bounded_ordered_and_shared_of_their_own_channel_only() -> None:
    products = {
        f"B0GATE{index:04d}": (f"Fictional product {index}", 20 - index) for index in range(12)
    }
    entry = gateway_entry(("sponsored_products", "Sponsored Products"), products, 300)
    assert entry.state is GatewayState.ROWS
    assert len(entry.rows) == GATEWAY_LIMIT
    assert [row.ntb_purchases for row in entry.rows] == list(range(20, 10, -1))
    assert entry.rows[0].share_of_channel == Decimal(20) / Decimal(300)
    assert entry.more_text == "And 2 more products."
    assert entry.caption_title == "Products new-to-brand shoppers bought first, Sponsored Products"


def test_gateway_no_purchases_is_decided_on_the_halo_inclusive_total_never_step_1() -> None:
    halo_only = gateway_entry(
        ("sponsored_display", "Sponsored Display"), {"B0HALO0001": ("Halo product", 3)}, 3
    )
    assert halo_only.state is GatewayState.ROWS
    none = gateway_entry(("sponsored_display", "Sponsored Display"), {}, 0)
    assert (none.state, none.text) == (
        GatewayState.NO_NTB_PURCHASES,
        "No new-to-brand purchases were credited to Sponsored Display in this window.",
    )
    no_row = gateway_entry(("sponsored_display", "Sponsored Display"), {}, None)
    assert no_row.state is GatewayState.NO_NTB_PURCHASES


def test_an_uncovered_window_reads_the_coverage_text_with_its_first_covered_date() -> None:
    verdict = pack_coverage_verdict(WINDOW, 2, {**_days(WINDOW, 2), WINDOW.start: 1})
    entry = gateway_not_covered(("sponsored_products", "Sponsored Products"), verdict)
    assert (entry.state, entry.first_covered_date) == (
        GatewayState.NOT_COVERED,
        date(2026, 9, 15),
    )
    assert entry.text == (
        "Gateway products are measured from 15 Sep 2026. This window starts earlier, so they "
        f"aren't shown. {_TAIL}"
    )
    assert entry.rows == ()


def _market(rows: tuple[StepOneInput, ...] = (_input(),)) -> NewCustomersMarket:
    built = tuple(step_one_row(item) for item in rows)
    return NewCustomersMarket(
        marketplace_key="amazon_us",
        currency_code=USD,
        new_to_brand=built,
        new_buyers=new_buyers_outside_declared_periods((TimeGrain.WEEK, TimeGrain.MONTH)),
        gateway=tuple(
            gateway_entry(
                (row.ad_product_key, row.ad_product_label), {"B0GATE0001": ("Fictional", 5)}, 5
            )
            for row in built
        ),
    )


def _artifact(markets: tuple[NewCustomersMarket, ...]) -> NewCustomersArtifact:
    return NewCustomersArtifact(
        outcome=NewCustomersOutcome.COMPLETE,
        preset=TemporalPresetId.SEVEN_DAYS,
        period=WINDOW,
        prior_period=DateRange(date(2026, 9, 7), date(2026, 9, 13)),
        period_label="14 Sep 2026 \u2013 20 Sep 2026",
        labels=_LABELS,
        markets=markets,
        insights=(
            portfolio_acquisition_slot(
                PortfolioAcquisitionSlotRequest(
                    (), artifact_revision="rev-1", silence_reason="quiet", windows=None
                )
            ),
        ),
    )


def test_the_artifact_never_totals_across_channels() -> None:
    sb = _input(ad_product_key="sponsored_brands", ad_product_label="Sponsored Brands")
    artifact = _artifact((_market((_input(), sb)),))
    payload = new_customers_artifact_payload(artifact)
    # The closed field sets: no level of the artifact has a slot for an all-channel figure.
    assert sorted(payload) == [
        "boundary",
        "evidence_status",
        "insight_cards",
        "insights",
        "markets",
        "outcome",
        "outcome_text",
        "page_context_expires_at",
        "page_context_label",
        "page_text",
        "period",
        "period_label",
        "pointers",
        "preset",
        "prior_period",
        "scope_text",
        "starter_questions",
        "type",
        "version",
    ]
    (market,) = cast("list[dict[str, object]]", payload["markets"])
    assert sorted(market) == [
        "currency_code",
        "gateway",
        "marketplace",
        "marketplace_text",
        "new_buyers",
        "new_to_brand",
    ]
    table = cast("dict[str, object]", market["new_to_brand"])
    assert sorted(table) == [
        "caption",
        "caption_detail",
        "columns",
        "foot",
        "heading",
        "rows",
        "share_basis",
    ]
    rows = cast("list[dict[str, object]]", table["rows"])
    assert [sorted(row) for row in rows] == [
        [
            "ad_product_key",
            "ad_product_label",
            "channel_group_label",
            "cost_per_ntb_purchase",
            "ntb_purchases",
            "ntb_revenue",
            "ntb_revenue_share",
            "ntb_revenue_share_change",
        ]
    ] * 2
    assert [
        entry["ad_product_key"] for entry in cast("list[dict[str, object]]", market["gateway"])
    ] == [
        "sponsored_products",
        "sponsored_brands",
    ]
    assert payload["starter_questions"] == [
        "Which channel had the highest new-to-brand share in 14 Sep 2026 \u2013 20 Sep 2026?"
    ]
    # Refusal: a gateway roster that doesn't follow step 1 one-for-one is refused.
    with pytest.raises(ValueError, match="one entry per step-1 ad product"):
        replace(_market((_input(), sb)), gateway=_market().gateway)


def test_the_step_1_caption_reads_its_halo_scope_from_dictionary_metadata(
    catalog: Catalog,
) -> None:
    step_one, gateway = catalog.metric("purchases_new_to_brand_12mo"), catalog.metric(COLUMN)
    assert step_one is not None and gateway is not None
    assert (step_one.includes_brand_halo, gateway.includes_brand_halo) == (False, True)
    assert _LABELS.step_one_caption("Week of 14\u201320 Sep 2026") == (
        "Amazon-attributed · 14-day · conversion date · new to brand in the last 12 months · "
        "excludes brand halo · Week of 14\u201320 Sep 2026"
    )
    # Refusal: halo scopes the wrong way round are refused rather than mislabelled.
    with pytest.raises(ValueError, match="advertised products only"):
        NewCustomersLabels("Amazon", 14, 12, True, True)
    payload = new_customers_artifact_payload(_artifact((_market(),)))
    (market,) = cast("list[dict[str, object]]", payload["markets"])
    (entry,) = cast("list[dict[str, object]]", market["gateway"])
    assert entry["captions"] == list(GATEWAY_CAPTIONS)
    assert entry["column_header"] == "New-to-brand purchases, brand halo included"


def test_new_buyers_show_only_on_a_calendar_preset() -> None:
    calendar_market = replace(
        _market((_input(compared=False, prior_share=None),)),
        new_buyers=new_buyers_figures(
            NewBuyersFiguresRequest(
                WEEK,
                buyers=None,
                cost=None,
                spend=None,
                currency_code=USD,
                caveat="",
                per_channel=PER_CHANNEL,
            )
        ),
    )
    with pytest.raises(ValueError, match="new buyers show only on a calendar preset"):
        _artifact((calendar_market,))
    # Positive control: the same market on its calendar preset.
    calendar = replace(
        _artifact((_market(),)),
        preset=TemporalPresetId.LAST_COMPLETE_WEEK,
        prior_period=None,
        markets=(calendar_market,),
    )
    assert calendar.scope_text == (
        "Showing 14 Sep 2026 \u2013 20 Sep 2026 (Latest measured week). No comparison for calendar "
        "periods."
    )


# --- portfolio-acquisition v1 ---------------------------------------------------------------


def test_portfolio_acquisition_is_a_deterministic_perspective_contract() -> None:
    contracts = {contract.id: contract for _, contract in load_insight_contracts(ROOT)}
    contract = contracts[InsightSlotId.PORTFOLIO_ACQUISITION]
    assert InsightSlotId.PORTFOLIO_ACQUISITION in DETERMINISTIC_SLOT_IDS
    assert contract.output_class.value == "perspective"
    assert (contract.initial_fictional_prompt, contract.deep) == (None, None)
    assert [kind.value for kind in contract.deterministic_findings] == ["absolute-change"]
    kinds = {case.kind.value: case for case in contract.calibration_cases}
    assert list(kinds) == [
        "known-correct",
        "exploratory",
        "contradiction",
        "fluent-unsupported",
        "derived-figure",
        "miscomputed-derivation",
        "incompatible-basis",
    ]
    # The two cross-channel rejections: adding two channels' shares, and summing buyer counts.
    assert kinds["miscomputed-derivation"].input_summary == (
        "A fictional renderer adds two channels' new-to-brand revenue shares, each taken of its "
        "own channel's attributed revenue, and states the sum as one share."
    )
    assert kinds["incompatible-basis"].input_summary == (
        "A fictional draft sums two channels' new-to-brand buyer counts, though one shopper can "
        "be credited to both channels."
    )
    assert kinds["known-correct"].findings[0].name == (
        "Sponsored Products new-to-brand revenue share change"
    )


def test_a_material_mover_fills_the_slot_with_one_channel_and_a_calendar_preset_is_silent() -> None:
    mover = NtbShareMover(
        "amazon_us",
        "sponsored_products",
        "Sponsored Products",
        DeterministicFinding(
            "Sponsored Products new-to-brand revenue share change", Decimal("0.01")
        ),
        Decimal("0.35"),
    )
    slot = portfolio_acquisition_slot(
        PortfolioAcquisitionSlotRequest(
            (mover,),
            artifact_revision="rev-1",
            silence_reason="quiet",
            windows=("14 Sep 2026 \u2013 20 Sep 2026", "7 Sep 2026 \u2013 13 Sep 2026"),
        )
    )
    assert slot.state is InsightSlotState.FILLED
    assert slot.text == (
        "Sponsored Products new-to-brand share rose 1 point. Inside Sponsored Products only, "
        "14 Sep 2026 \u2013 20 Sep 2026 compared with 7 Sep 2026 \u2013 13 Sep 2026."
    )
    silent = portfolio_acquisition_slot(
        PortfolioAcquisitionSlotRequest(
            (mover,), artifact_revision="rev-1", silence_reason="quiet", windows=None
        )
    )
    # NC-GATE-101: a calendar preset compared nothing, so it never serves the prior-window
    # silence condition; it reads the page's no-comparison sentence, quiet for that reason.
    assert (silent.state, silent.absence_class, silent.absence_reason) == (
        InsightSlotState.SILENT,
        InsightAbsenceClass.EVALUATED_QUIET,
        NO_CALENDAR_COMPARISON_TEXT,
    )
    # Paired: a rolling preset where nothing moved still serves the silence condition.
    quiet = portfolio_acquisition_slot(
        PortfolioAcquisitionSlotRequest(
            (),
            artifact_revision="rev-1",
            silence_reason="quiet",
            windows=("14 Sep 2026 \u2013 20 Sep 2026", "7 Sep 2026 \u2013 13 Sep 2026"),
        )
    )
    assert (quiet.state, quiet.absence_class, quiet.absence_reason) == (
        InsightSlotState.SILENT,
        InsightAbsenceClass.EVALUATED_QUIET,
        "quiet",
    )


def test_the_payload_is_json_ready_and_typed() -> None:
    payload = new_customers_artifact_payload(_artifact((_market(),)))
    assert json.loads(json.dumps(payload)) == payload
    assert (payload["type"], payload["version"]) == ("NewCustomersArtifact", 2)
    assert [
        pointer["tab_id"] for pointer in cast("list[dict[str, object]]", payload["pointers"])
    ] == [
        "audiences",
        "retention",
    ]
    # ADR-119: one coverage block; a covered rolling window lists step 1 for both windows and
    # gateway products, all complete, and no new buyers (the callout isn't a gap).
    status = cast("dict[str, object]", payload["evidence_status"])
    assert (status["version"], status["window"]) == (
        2,
        {"start": "2026-09-14", "end": "2026-09-20"},
    )
    assert status["summary"] == "Every figure is complete for this window."
    assert [
        (entry["slot"], entry["role"], entry["label"], entry["role_label"], entry["state"])
        for entry in cast("list[dict[str, object]]", status["entries"])
    ] == [
        (
            "new-to-brand:amazon_us",
            "current",
            "New-to-brand purchases per channel",
            "This window",
            "complete",
        ),
        (
            "new-to-brand:amazon_us",
            "prior",
            "New-to-brand purchases per channel",
            "Prior window",
            "complete",
        ),
        ("gateway-products:amazon_us", "current", "Gateway products", "This window", "complete"),
    ]
    # ADR-121 change block, toned by the dictionary's favourable direction (plan D4).
    (market,) = cast("list[dict[str, object]]", payload["markets"])
    (row,) = cast(
        "list[dict[str, object]]", cast("dict[str, object]", market["new_to_brand"])["rows"]
    )
    assert row["ntb_revenue_share_change"] == {
        "change": {
            "direction": "up",
            "sentiment": "favourable",
            "unit": "percentage_points",
            "display_text": "+5.0 pts",
            "not_meaningful": None,
        },
        "absence": None,
    }
    # One marketplace names none, as the approved mockups read.
    assert market["marketplace_text"] is None


def _calendar_artifact(markets: tuple[NewCustomersMarket, ...]) -> NewCustomersArtifact:
    rolling = _artifact((_market(),))
    return replace(
        rolling,
        preset=TemporalPresetId.LAST_COMPLETE_WEEK,
        period=WEEK.window,
        prior_period=None,
        period_label=WEEK.label,
        markets=markets,
    )


def _calendar_market(new_buyers: NewBuyers, **changes: object) -> NewCustomersMarket:
    row = _input(compared=False, prior_share=None)
    return replace(_market((row,)), new_buyers=new_buyers, **changes)  # type: ignore[arg-type]


def _entries(artifact: NewCustomersArtifact) -> list[tuple[object, ...]]:
    status = cast("dict[str, object]", new_customers_artifact_payload(artifact)["evidence_status"])
    return [
        (entry["slot"], entry["state"], entry["missing_text"], entry["reason"], entry["recovery"])
        for entry in cast("list[dict[str, object]]", status["entries"])
        if entry["slot"] != "new-to-brand:amazon_us"
    ]


def test_coverage_names_each_section_shown_short_of_full_with_its_server_text() -> None:
    figures = new_buyers_figures(
        NewBuyersFiguresRequest(
            WEEK,
            buyers=Decimal(4400),
            cost=Decimal("1.25"),
            spend=Decimal(5500),
            currency_code=USD,
            caveat="",
            per_channel=PER_CHANNEL,
        )
    )
    complete = ("complete", "None", "Every day of this window has arrived.", "No action needed.")
    # Positive control: tiles, per-channel rows and gateway rows are complete.
    assert _entries(_calendar_artifact((_calendar_market(figures),))) == [
        ("new-buyers:amazon_us", *complete),
        ("new-buyers-per-channel:amazon_us", *complete),
        ("gateway-products:amazon_us", *complete),
    ]
    recovery = "It shows by itself when the data arrives."
    pending = new_buyers_awaiting(
        NewBuyersAwaitingRequest(
            WEEK, expected_after=date(2026, 9, 23), latest_available_period=None, as_of=WEEK.end
        )
    )
    assert pending.state is NewBuyersState.PENDING
    assert _entries(_calendar_artifact((_calendar_market(pending),))) == [
        ("new-buyers:amazon_us", "unavailable", "Not sent", pending.text, recovery),
        ("gateway-products:amazon_us", *complete),
    ]
    late = per_channel_awaiting(
        PerChannelAwaitingRequest(
            WEEK, date(2026, 9, 23), latest_available_period=None, as_of=date(2026, 9, 24)
        )
    )
    with_late = replace(figures, per_channel=late)
    assert _entries(_calendar_artifact((_calendar_market(with_late),))) == [
        ("new-buyers:amazon_us", *complete),
        ("new-buyers-per-channel:amazon_us", "unavailable", "Not sent", late.text, recovery),
        ("gateway-products:amazon_us", *complete),
    ]
    uncovered = gateway_not_covered(
        ("sponsored_products", "Sponsored Products"),
        pack_coverage_verdict(WINDOW, 2, {**_days(WINDOW, 2), WINDOW.start: 1}),
    )
    shut = _calendar_market(figures, gateway=(uncovered,))
    assert _entries(_calendar_artifact((shut,)))[-1] == (
        "gateway-products:amazon_us",
        "unavailable",
        "Not sent",
        "Gateway products are measured from 15 Sep 2026. This window starts earlier, so they "
        "aren't shown.",
        _TAIL,
    )


def test_two_marketplaces_name_each_market_and_its_coverage_entries() -> None:
    uk = replace(_market(), marketplace_key="amazon_uk", marketplace_label="UK")
    us = replace(_market(), marketplace_label="US")
    payload = new_customers_artifact_payload(_artifact((uk, us)))
    assert [
        market["marketplace_text"] for market in cast("list[dict[str, object]]", payload["markets"])
    ] == ["UK marketplace \u00b7 USD", "US marketplace \u00b7 USD"]
    status = cast("dict[str, object]", payload["evidence_status"])
    labels = {entry["label"] for entry in cast("list[dict[str, object]]", status["entries"])}
    assert "Gateway products \u00b7 UK marketplace \u00b7 USD" in labels
    # Refusal: two marketplaces with one unnamed, beside the named pair above.
    with pytest.raises(ValueError, match="each market names its marketplace"):
        _artifact((replace(uk, marketplace_label=None), us))


def test_each_request_refuses_its_inconsistent_inputs_beside_a_consistent_one() -> None:
    with pytest.raises(ValueError, match="returned new buyers carry their planned cost"):
        NewBuyersFiguresRequest(
            WEEK,
            buyers=Decimal(4400),
            cost=None,
            spend=Decimal(5500),
            currency_code=USD,
            caveat="",
            per_channel=PER_CHANNEL,
        )
    assert (
        NewBuyersFiguresRequest(
            WEEK,
            buyers=None,
            cost=None,
            spend=None,
            currency_code=USD,
            caveat="",
            per_channel=PER_CHANNEL,
        ).buyers
        is None
    )
    with pytest.raises(ValueError, match="expected no earlier than its last day"):
        NewBuyersAwaitingRequest(
            WEEK,
            expected_after=WEEK.end - timedelta(days=1),
            latest_available_period=None,
            as_of=WEEK.end,
        )
    assert (
        NewBuyersAwaitingRequest(
            WEEK, expected_after=WEEK.end, latest_available_period=None, as_of=WEEK.end
        ).expected_after
        == WEEK.end
    )
    with pytest.raises(ValueError, match="names its revision and silence reason"):
        PortfolioAcquisitionSlotRequest(
            (), artifact_revision=" ", silence_reason="quiet", windows=None
        )
    assert (
        PortfolioAcquisitionSlotRequest(
            (), artifact_revision="rev-1", silence_reason="quiet", windows=None
        ).material
        == ()
    )


def test_two_marketplaces_give_one_mover_each_and_never_one_across_them() -> None:
    """Latent while one marketplace is governed: the card text names no marketplace (open item)."""
    us = _market()
    uk = replace(_market((_input(prior_share=Decimal("0.30")),)), marketplace_key="amazon_uk")
    movers = ntb_share_movers((us, uk))
    assert [(item.marketplace_key, item.ad_product_key, item.finding.value) for item in movers] == [
        ("amazon_us", "sponsored_products", Decimal("0.05")),
        ("amazon_uk", "sponsored_products", Decimal("0.10")),
    ]
    slot = portfolio_acquisition_slot(
        PortfolioAcquisitionSlotRequest(
            movers,
            artifact_revision="rev-1",
            silence_reason="quiet",
            windows=("14 Sep 2026 \u2013 20 Sep 2026", "7 Sep 2026 \u2013 13 Sep 2026"),
        )
    )
    # One sentence per marketplace's move, largest first; the two moves are never added.
    assert slot.text == (
        "Sponsored Products new-to-brand share rose 10 points. Inside Sponsored Products only, "
        "14 Sep 2026 \u2013 20 Sep 2026 compared with 7 Sep 2026 \u2013 13 Sep 2026. "
        "Sponsored Products new-to-brand share rose 5 points. Inside Sponsored Products only, "
        "14 Sep 2026 \u2013 20 Sep 2026 compared with 7 Sep 2026 \u2013 13 Sep 2026."
    )
    # Control: one marketplace gives exactly its own mover.
    assert [item.marketplace_key for item in ntb_share_movers((us,))] == ["amazon_us"]


def test_each_material_mover_is_one_card_whose_title_and_body_are_the_slot_s_narration() -> None:
    mover = NtbShareMover(
        "amazon_us",
        "sponsored_products",
        "Sponsored Products",
        DeterministicFinding(
            "Sponsored Products new-to-brand revenue share change", Decimal("0.03")
        ),
        Decimal("0.35"),
    )
    request = PortfolioAcquisitionSlotRequest(
        (mover,),
        artifact_revision="rev-1",
        silence_reason="quiet",
        windows=("Sep 14\u201320", "Sep 7\u201313"),
    )
    (card,) = portfolio_acquisition_cards(request)
    assert (card.headline, card.body) == (
        "Sponsored Products new-to-brand share rose 3 points",
        "Inside Sponsored Products only, Sep 14\u201320 compared with Sep 7\u201313.",
    )
    slot = portfolio_acquisition_slot(request)
    assert slot.text == f"{card.headline}. {card.body}"
    artifact = replace(_artifact((_market(),)), insights=(slot,), insight_cards=(card,))
    (served,) = cast(
        "list[dict[str, object]]", new_customers_artifact_payload(artifact)["insight_cards"]
    )
    assert (served["kind_label"], served["title"], served["marketplace_text"]) == (
        "New-to-brand share mover",
        "Sponsored Products new-to-brand share rose 3 points",
        None,
    )
    # A silent slot has no card; a card beside a silent slot is refused (paired).
    silent = replace(request, windows=None)
    assert portfolio_acquisition_cards(silent) == ()
    with pytest.raises(ValueError, match="exactly the filled slot's narration"):
        replace(artifact, insights=(portfolio_acquisition_slot(silent),))
