# SPDX-License-Identifier: MIT-0
"""Semantic V2 orchestration with immutable, fenced publication order."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final, cast

from aws_lambda_powertools import Logger

from adapters.jobs.ingest_delivery.row_validation import validated_rows
from adapters.jobs.schema_generation.merge import (
    RESIDUAL_BUCKET_SCHEME,
    RESIDUAL_ROW_KIND,
    WITHHELD_SUFFIX,
)
from adapters.outbound.telemetry.reporting import LogSeverity, OperationLog, report_operation
from adapters.outbound.telemetry.tracing import (
    INGEST_DELIVERY,
    INGEST_PARTITION,
    span,
    trace_boundary,
)
from domain.catalog.capability import Catalog, MetricKind, references_in_definition
from domain.curation.assertions import (
    AssertionName,
    LoadOutcome,
    Violation,
    control_total_measures,
    measure_columns,
)
from domain.curation.ingestion import (
    ContractAssessment,
    DiscardFoldRequest,
    WithheldResidual,
    fold_discarded,
    split_discarded,
    split_withheld,
)
from domain.curation.landing import Manifest
from domain.curation.mapping import SourceDocument, suppression_flag
from domain.data_contracts.contract import (
    CELL_STATE_COLUMN,
    ContractError,
    DataContract,
    ExpectedCellSet,
    NullMeaning,
)
from domain.data_contracts.schema import (
    CONFORMED_PROVENANCE_COLUMNS,
    conformed_provenance_values,
)
from domain.data_contracts.validation import ColumnRegistry
from domain.measurement.pack_coverage import pack_version_evidence
from domain.measurement.periodic import AS_OF_MONTH_COLUMN
from domain.semantics.audience_segments import SegmentProviderClassification
from domain.semantics.canonical_compiler import (
    CanonicalCompileRequest,
    CanonicalCompileResult,
    CanonicalRow,
    IdentityMappingEvidence,
    canonical_rows_checksum,
    canonical_scalar,
    compile_canonical_rows,
)
from domain.semantics.commerce_identity import (
    CommerceIdentityBridge,
    ResolveCommerceIdentityRequest,
    resolve_commerce_identity,
)
from domain.semantics.curation import (
    CanonicalCurationPlan,
    CanonicalCurationRequest,
    plan_curation,
)
from domain.semantics.dimension_history import (
    DimensionHistoryRequest,
    DimensionHistoryResult,
    build_dimension_history,
)
from domain.semantics.identity import IdentityResolutionError, PlatformKey, SourceIdentifier
from domain.semantics.identity_evidence import (
    IDENTITY_EVIDENCE_KEY_CONFLICT,
    IDENTITY_EVIDENCE_UNPREPARED,
    IdentityMappingsClaim,
    IdentityMappingsDocument,
    identity_mappings_document,
    parse_identity_claim,
)
from domain.semantics.media_taxonomy import MediaTaxonomy, ResolveExposureSourceSetRequest
from domain.semantics.prepublication import (
    CandidateMeasureTotal,
    ControlComparisonKey,
    ControlComparisonScope,
    ControlTotalRequest,
    PrepublicationRequest,
    ReportedMeasureTotal,
    SourcePackConformanceRequest,
    SourcePackConformanceResult,
    SuppressionCoverageEvidence,
    WithheldResidualTotal,
    conform_source_pack,
    evaluate_control_total,
    evaluate_prepublication,
    evaluate_suppression_coverage,
)
from domain.semantics.product_dimension import OPEN_ENDED
from domain.semantics.publication import (
    PACK_VARIANT_OPTION,
    AttemptEvent,
    AttemptEventKind,
    AttemptId,
    CanonicalDatasetKey,
    CanonicalPublisherKey,
    LeaseGrant,
    LeaseHolderKey,
    LeaseRequest,
    LeaseState,
    PartitionIdentity,
    PublicationCandidate,
    PublicationGraph,
    PublicationId,
    PublicationScope,
    PublishCandidateRequest,
    lens_source_options,
    publication_lens_for,
    publish_candidate,
    successful_matches_candidate,
)
from domain.semantics.source_aligned import LandedDeliveryRef, SourceAlignedPublication
from domain.semantics.source_compiler import (
    CompileSourceRequest,
    compile_source_publication,
    periodic_manifest_bucket,
)
from domain.semantics.source_pack import (
    PackReadiness,
    SourcePack,
    SourcePackControlRole,
    SourcePackRole,
)
from domain.semantics.table_dq import RuntimeTableRules, evaluate_table_dq
from domain.semantics.time import (
    FactTimeClass,
    MeasurementBucket,
    ObservationTimeBasis,
    TimeGrain,
)
from domain.semantics.validation import (
    NO_CELL_RETURNED_RULE_ID,
    WITHHELD_ONLY_RULE_ID,
    PrepublicationRuleFamily,
    PrepublicationRuleResult,
    PublicationEvidence,
    PublicationEvidenceSubject,
    RuleOutcome,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError
from domain.shared_kernel.provenance import MeasurementBasis, withheld_money_counts
from domain.shopper_aggregates.cells import DeclaredCellsRequest, fill_declared_cells
from domain.shopper_aggregates.findings import (
    ShopperAggregateContractError,
    ShopperAggregateFinding,
)
from domain.shopper_aggregates.loss import (
    DEGRADE_THRESHOLD,
    GrainSetLoss,
    LossRequest,
    LossScope,
    cell_loss_rule_id,
    grade_loss,
)
from ports.outbound.canonical import (
    CanonicalDatasetReadPort,
    CanonicalDatasetWritePort,
    CanonicalDestination,
    CanonicalMaterializeRequest,
)
from ports.outbound.identity_evidence import IdentityEvidencePort
from ports.outbound.semantic_state import (
    MAX_COMMIT_GROUP_MEMBERS,
    AttemptStorePort,
    CommitPublicationGroupRequest,
    CommitPublicationRequest,
    LeaseStorePort,
    PublicationStorePort,
)
from ports.outbound.source_aligned import SourceAlignedReadPort, SourceAlignedWritePort

_LOGGER = Logger(service="adlytics-semantic-sidecar")
"""Structured operation events for the load's diagnostics, beside the typed error reports."""

__all__ = [
    "PreparedProjectionPartition",
    "PreparedProjectionPlan",
    "SemanticSidecarOutcome",
    "SemanticSidecarReceipt",
    "SemanticSidecarRequest",
    "SemanticSidecarResult",
    "SemanticSidecarTooling",
    "run_semantic_sidecar",
]

SIDECAR_INVARIANT = ErrorCode("SEMANTIC_SIDECAR", 1)
SIDECAR_BUSY = ErrorCode("SEMANTIC_SIDECAR", 2)
RULE_SET_VERSION = "semantic-v2.1"
PARTITION_LEASE_DURATION: Final = timedelta(minutes=10)
"""How long one canonical-partition publication lease lasts from its acquisition.

A holder releases it when its publication group ends; a holder that stops without releasing
(a crash or timeout) keeps every other publisher of the partition refused, `SIDECAR_BUSY`, for
at most this long. The delivery handler's busy-retry schedule is bounded against it.
"""


class SemanticSidecarOutcome(StrEnum):
    """Decided sidecar outcome; dependency failures raise for retry instead.

    ``SUPERSEDED`` is a contract whose every candidate bucket was skipped as older than its
    ``CURRENT`` (ADR-047: the newest source revision wins). It is a product signal, never a
    block: a bulk load of older data supersedes by design, and counting it as ``BLOCKED`` would
    hold the blocked-delivery alarm in ``ALARM`` and hide the next real block. A contract that
    published some buckets and skipped the rest is ``PUBLISHED``; ``BLOCKED`` is every other
    refusal.
    """

    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    BLOCKED = "blocked"
    SOURCE_ONLY = "source_only"


@dataclass(frozen=True, slots=True)
class SemanticSidecarReceipt:
    """Exact artifacts observed or created for one decided semantic partition."""

    landed_delivery: LandedDeliveryRef
    source_publications: tuple[SourceAlignedPublication, ...]
    candidate: CanonicalMaterializeRequest | None = None
    successful: CanonicalMaterializeRequest | None = None
    attempt_id: AttemptId | None = None
    predecessor_attempt_id: AttemptId | None = None
    terminal_event_kind: AttemptEventKind | None = None
    write_plan_key: str | None = None

    def __post_init__(self) -> None:
        if self.write_plan_key is not None and not self.write_plan_key.strip():
            raise ValueError("semantic sidecar receipt write plan key cannot be blank.")
        if not self.source_publications:
            raise ValueError("semantic sidecar receipt requires exact source publications.")
        if any(
            publication.landed_delivery != self.landed_delivery
            for publication in self.source_publications
        ):
            raise ValueError("semantic sidecar receipt sources must name the landed delivery.")


@dataclass(frozen=True, slots=True)
class SemanticSidecarResult:
    """One source pack's decided Semantic V2 outcome."""

    contract_id: str
    outcome: SemanticSidecarOutcome
    detail: str
    publications: tuple[PublicationId, ...] = ()
    receipts: tuple[SemanticSidecarReceipt, ...] = ()
    expected_partition_count: int = 0

    def __post_init__(self) -> None:
        if self.expected_partition_count < 0:
            raise ValueError("semantic sidecar expected partition count cannot be negative.")


def _ignore_sidecar_progress(_progress: SemanticSidecarProgress) -> None:
    """Default progress sink for callers that do not project operator telemetry."""


@dataclass(frozen=True, slots=True)
class PreparedProjectionPartition:
    """One prepared canonical partition as the feasibility callback sees it before any lease.

    Only facts settled by prepare/bind are carried: the exact dataset, partition identity,
    bound source publications, and the compiled canonical rows with their checksum. The
    publication identity, attempt, and fencing token are minted only after lease acquisition,
    so the callback substitutes conservative bounds for them (write-discipline §2.1).
    """

    contract_id: str
    dataset: str
    partition_identity: str
    source_publications: tuple[SourceAlignedPublication, ...]
    canonical_rows: tuple[CanonicalRow, ...]
    content_checksum: str


@dataclass(frozen=True, slots=True)
class PreparedProjectionPlan:
    """The side-effect-free prepared roster handed to ``validate_projection_feasibility``.

    ``source_publications`` is every READY source object of the delivery in ascending contract
    order; ``partitions`` is the complete controlled roster; ``groups`` is the exact
    group/member roster as ``(contract_id, partition_identity)`` keys.
    """

    source_publications: tuple[SourceAlignedPublication, ...]
    partitions: tuple[PreparedProjectionPartition, ...]
    groups: tuple[tuple[tuple[str, str], ...], ...]


def _accept_prepared_plan(_plan: PreparedProjectionPlan) -> None:
    """Default feasibility callback for callers that carry no plan-capacity bound."""


@dataclass(frozen=True, slots=True)
class SemanticSidecarProgress:
    """Safe stage progress inside one delivery; no partition identity or source values."""

    stage: str
    contract_ids: tuple[str, ...]
    completed: int
    total: int

    def __post_init__(self) -> None:
        if not self.stage.strip() or not self.contract_ids:
            raise ValueError("semantic sidecar progress requires a stage and contracts.")
        if self.completed < 0 or self.total < 1 or self.completed > self.total:
            raise ValueError("semantic sidecar progress counts are invalid.")


@dataclass(frozen=True, slots=True)
class SemanticSidecarRequest:
    """One parsed delivery and source assessment, with stable operation correlation."""

    manifest: Manifest
    document: SourceDocument
    landed_delivery: LandedDeliveryRef
    assessments: tuple[ContractAssessment, ...]
    contracts: tuple[DataContract, ...]
    now: datetime
    operation_id: str
    execution_id: str
    predecessor_execution_id: str | None = None
    plan_digest: str = ""
    telemetry_operation_id: str = ""
    progress: Callable[[SemanticSidecarProgress], None] = _ignore_sidecar_progress
    feasibility: Callable[[PreparedProjectionPlan], None] = _accept_prepared_plan
    """Runs after prepare/bind/group and before the first lease, attempt, canonical object,
    publication, or ``CURRENT`` mutation; raising refuses the delivery before authority."""

    def __post_init__(self) -> None:
        if self.now.tzinfo is None:
            raise ValueError("semantic sidecar instant must be timezone-aware.")
        if not self.operation_id.strip():
            raise ValueError("semantic sidecar requires an operation id.")
        if not self.execution_id.strip():
            raise ValueError("semantic sidecar requires an execution id.")
        if self.predecessor_execution_id is not None:
            if not self.predecessor_execution_id.strip():
                raise ValueError("semantic sidecar predecessor execution id cannot be blank.")
            if self.predecessor_execution_id == self.execution_id:
                raise ValueError("semantic sidecar execution cannot be its own predecessor.")


def _real_utc_now() -> datetime:
    """Real wall-clock instant for lease expiry, distinct from the logical request now."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SemanticSidecarTooling:
    """Separated physical and state authorities used by the canonical publisher."""

    source_writer: SourceAlignedWritePort
    source_reader: SourceAlignedReadPort
    canonical_writer: CanonicalDatasetWritePort
    attempts: AttemptStorePort
    leases: LeaseStorePort
    publications: PublicationStorePort
    packs: tuple[SourcePack, ...]
    bridge: CommerceIdentityBridge
    graph: PublicationGraph
    table_rules: dict[str, RuntimeTableRules]
    columns: ColumnRegistry
    catalog: Catalog
    identity_evidence: IdentityEvidencePort
    """Write-once store of identity-mapping claim-check documents (write-discipline §2.6)."""
    taxonomy: MediaTaxonomy | None = None
    canonical_reader: CanonicalDatasetReadPort | None = None
    clock: Callable[[], datetime] = _real_utc_now
    segment_providers: SegmentProviderClassification | None = None
    """The governed provider classification `dim_audience_segment.provider` is stamped from."""


def _id(prefix: str, *parts: str) -> str:
    rendered = "|".join(parts)
    return f"{prefix}-{hashlib.sha256(rendered.encode()).hexdigest()[:32]}"


@dataclass(frozen=True, slots=True)
class _RuleSpec:
    family: PrepublicationRuleFamily
    rule_id: str
    passed: bool
    detail: str


def _rule(spec: _RuleSpec) -> PrepublicationRuleResult:
    return PrepublicationRuleResult(
        family=spec.family,
        rule_id=spec.rule_id,
        outcome=RuleOutcome.PASSED if spec.passed else RuleOutcome.FAIL,
        detail=spec.detail,
    )


@dataclass(frozen=True, slots=True)
class _PartitionRequest:
    publication: SourceAlignedPublication
    pack: SourcePack
    contract: DataContract
    bridge: CommerceIdentityBridge
    bucket: MeasurementBucket
    manifest_marketplace: str


def _suppressed_partition_rows(request: _PartitionRequest) -> bool:
    suppression = request.contract.suppression
    rows = tuple(
        row for row in request.publication.rowset.rows if row.measurement == request.bucket
    )
    if suppression is None or not rows:
        return False
    for row in rows:
        values = tuple(
            field.value for field in row.fields if field.name == suppression.discriminator_column
        )
        if len(values) != 1 or not suppression_flag(values[0]):
            return False
    return True


def _partition(request: _PartitionRequest) -> PartitionIdentity:
    publication = request.publication
    pack = request.pack
    bridge = request.bridge
    bucket = request.bucket
    rows = tuple(row for row in publication.rowset.rows if row.measurement == bucket)
    if not rows and not request.contract.records_cell_state:
        # Only a closed enumeration has a partition with no row: its expected cell, which the
        # load writes not_returned in the manifest's marketplace (ADR-099 point 8).
        raise ValueError("semantic partition has no source-aligned rows.")
    marketplace_ids = tuple(
        item
        for row in rows
        for item in row.source_identifiers
        if item.namespace == pack.marketplace.namespace
    )
    if marketplace_ids:
        resolved = tuple(
            resolve_commerce_identity(
                bridge,
                ResolveCommerceIdentityRequest(
                    marketplace=item,
                    business_date=bucket.window.start,
                ),
            ).marketplace.target
            for item in marketplace_ids
        )
    elif not rows or _suppressed_partition_rows(request):
        resolved = tuple(
            {
                mapping.target
                for mapping in bridge.bare_value_marketplace_mappings()
                if mapping.source.value == request.manifest_marketplace
                and mapping.applies(mapping.source, on=bucket.window.start)
            }
        )
        if len(resolved) != 1:
            raise IdentityResolutionError(
                "suppressed source partition has no single governed manifest marketplace."
            )
    else:
        raise IdentityResolutionError(
            "source partition carries no governed marketplace identifier."
        )
    if len(set(resolved)) != 1:
        raise IdentityResolutionError(
            "one source bucket resolves to more than one marketplace population."
        )
    # The family-naming options only, so serving rebuilds this exact lens (`lens_source_options`).
    options = lens_source_options(
        publication.rowset.delivery_scope.source_system.value,
        tuple((item.name, item.value) for item in publication.rowset.source_options),
    )
    scope = PublicationScope(
        dataset=pack.dataset,
        commerce_scope=bridge.commerce_scope,
        population_key=resolved[0].value,
        lens=publication_lens_for(
            publication.rowset.interpreter.value,
            options,
            publication.rowset.interpreter.version,
        ),
        time_class=pack.time.time_class,
        business_calendar=bucket.calendar,
        source_timezone=bucket.source_timezone,
        observation_basis=bucket.observation_basis,
    )
    return PartitionIdentity(scope=scope, bucket=bucket)


def _append(tooling: SemanticSidecarTooling, event: AttemptEvent) -> None:
    outcome = tooling.attempts.append(event)
    if not outcome.accepted:
        raise InvariantError(
            SIDECAR_INVARIANT,
            "Semantic attempt history refused an immutable event.",
            context={"attempt": event.attempt_id.value, "reason": outcome.reason},
        )


def _source_conformance(
    pack: SourcePack,
    publication: SourceAlignedPublication,
    mappings: tuple[IdentityMappingEvidence, ...],
) -> tuple[SourcePackConformanceResult, IdentityMappingsDocument]:
    """Source rules, with the identity result as a fixed-size claim on the full mapping list.

    The document is returned beside the conformance; it is written before any candidate that
    names it (`_write_identity_evidence`), never during preparation.
    """
    document = identity_mappings_document(mappings)
    passed = (
        _rule(
            _RuleSpec(
                PrepublicationRuleFamily.PROVENANCE,
                f"source.{pack.contract_id}.provenance.v1",
                True,
                "Exact manifest and object VersionIds and checksums are bound.",
            )
        ),
        _rule(
            _RuleSpec(
                PrepublicationRuleFamily.IDENTITY,
                f"source.{pack.contract_id}.identity.v1",
                True,
                document.claim.detail,
            )
        ),
        _rule(
            _RuleSpec(
                PrepublicationRuleFamily.TIME,
                f"source.{pack.contract_id}.time.v1",
                True,
                "Row measurement buckets are contained by extraction coverage.",
            )
        ),
    )
    conformance = conform_source_pack(
        SourcePackConformanceRequest(pack, publication.reference, passed)
    )
    return conformance, document


@dataclass(frozen=True, slots=True)
class _CandidateRulesRequest:
    item: ContractAssessment
    pack: SourcePack
    partition: PartitionIdentity
    rows: tuple[CanonicalRow, ...]
    rules: RuntimeTableRules
    catalog: Catalog
    control_results: tuple[PrepublicationRuleResult, ...]
    evidence_only: bool = False
    withheld_only: bool = False
    suppression_proven: bool = False
    suppression_unavailable: bool = False
    cell_loss: CellLoss | None = None
    query_pack_version: int | None = None
    no_cell_returned: bool = False
    """A roster-graded grain set's delivery returned no cell (`NO_CELL_RETURNED_RULE_ID`)."""
    """The delivering pack's version, recorded for a contract with pack-versioned columns."""


def _suppression_result(request: _CandidateRulesRequest) -> PrepublicationRuleResult:
    item = request.item
    partition = request.partition
    rows = request.rows
    if request.withheld_only:
        return _rule(
            _RuleSpec(
                PrepublicationRuleFamily.SUPPRESSION_COVERAGE,
                WITHHELD_ONLY_RULE_ID,
                True,
                "Every source row in this exact bucket was withheld; no attributable fact row "
                "was invented, and the grouped residual retains the withheld quantities.",
            )
        )
    if request.evidence_only or request.suppression_proven:
        return evaluate_suppression_coverage(
            partition,
            SuppressionCoverageEvidence(requested_buckets=(partition.bucket,)),
        )
    if request.suppression_unavailable:
        return evaluate_suppression_coverage(
            partition,
            SuppressionCoverageEvidence(
                requested_buckets=(partition.bucket,),
                unavailable_buckets=(partition.bucket,),
            ),
        )
    if item.load.contract.suppression is None:
        return evaluate_suppression_coverage(
            partition,
            SuppressionCoverageEvidence(requested_buckets=(partition.bucket,)),
        )
    observed = tuple(dict(row).get("rows_suppressed") for row in rows)
    if not observed or any(not isinstance(value, bool) for value in observed):
        return _rule(
            _RuleSpec(
                PrepublicationRuleFamily.SUPPRESSION_COVERAGE,
                "suppression:exact-bucket",
                False,
                "Suppression-bearing candidate has no typed discriminator evidence.",
            )
        )
    unavailable = (partition.bucket,) if any(observed) else ()
    return evaluate_suppression_coverage(
        partition,
        SuppressionCoverageEvidence(
            requested_buckets=(partition.bucket,),
            unavailable_buckets=unavailable,
        ),
    )


def _metric_semantics_result(request: _CandidateRulesRequest) -> PrepublicationRuleResult:
    contract = request.item.load.contract
    source_dataset = contract.source_dataset
    written = set(contract.target_columns)
    # A metric binds the contracts that write one of its columns. Two contracts now read
    # dsp_impressions into fct_ad_exposure (amc-dsp-impressions, amc-dsp-reach), and each writes
    # only its own measures; before pack 4 every (dataset, fact) pair had one writer, so this
    # selects exactly the metrics it always did for every other contract.
    metrics = tuple(
        metric
        for metric in request.catalog.metrics
        if metric.kind is MetricKind.SIMPLE
        and source_dataset in metric.source_datasets
        and request.pack.dataset.value in request.catalog.facts_read(metric)
        and any(
            table == request.pack.dataset.value and column in written
            for table, column in references_in_definition(metric.definition)
        )
    )
    if not metrics:
        expected = (
            request.pack.role is SourcePackRole.FACT
            and request.pack.control is None
            and not request.evidence_only
        )
        return _rule(
            _RuleSpec(
                PrepublicationRuleFamily.METRIC_SEMANTICS,
                f"metric.{request.pack.contract_id}.v2",
                not expected,
                (
                    "This evidence-only dataset declares no user-facing metric."
                    if not expected
                    else (
                        "No metric dictionary entry binds this fact source to its "
                        "canonical dataset."
                    )
                ),
            )
        )
    columns = (
        set(request.item.load.contract.target_columns)
        if request.withheld_only or request.no_cell_returned
        else {name for row in request.rows for name, _ in row}
    )
    missing = sorted(
        {
            column
            for metric in metrics
            for table, column in references_in_definition(metric.definition)
            if table == request.pack.dataset.value and column not in columns
        }
    )
    return _rule(
        _RuleSpec(
            PrepublicationRuleFamily.METRIC_SEMANTICS,
            f"metric.{request.pack.contract_id}.v2",
            not missing,
            (
                "Metric dictionary source, dataset, and stored measure columns are candidate-bound."
                if not missing
                else "Metric dictionary references absent candidate columns: " + ", ".join(missing)
            ),
        )
    )


def _candidate_results(
    request: _CandidateRulesRequest,
) -> tuple[PrepublicationRuleResult, ...]:
    item = request.item
    pack = request.pack
    contract_passed = not item.quarantines
    return (
        _metric_semantics_result(request),
        _rule(
            _RuleSpec(
                PrepublicationRuleFamily.CONTRACT_ASSERTIONS,
                f"contract.{pack.contract_id}.v1",
                contract_passed,
                "Source contract assertions passed."
                if contract_passed
                else "; ".join(violation.detail for violation in item.outcome.violations),
            )
        ),
        evaluate_table_dq(request.rules, request.rows),
        *request.control_results,
        _suppression_result(request),
        *(
            ()
            if request.cell_loss is None
            else (
                _rule(
                    _RuleSpec(
                        PrepublicationRuleFamily.SUPPRESSION_COVERAGE,
                        cell_loss_rule_id(pack.contract_id),
                        True,
                        request.cell_loss.detail,
                    )
                ),
            )
        ),
        *(
            ()
            if not request.no_cell_returned
            else (
                _rule(
                    _RuleSpec(
                        PrepublicationRuleFamily.SUPPRESSION_COVERAGE,
                        NO_CELL_RETURNED_RULE_ID,
                        True,
                        "No cell came back for this grain set, which has no closed enumeration "
                        "at load; serving reads every rostered key not available (design §4.5).",
                    )
                ),
            )
        ),
        *(
            ()
            if request.query_pack_version is None
            else (pack_version_evidence(pack.contract_id, request.query_pack_version),)
        ),
    )


@dataclass(frozen=True, slots=True)
class CellLoss:
    """What a `discard_suppressed_rows` partition dropped and which expected cells it lacks.

    Recorded on the partition's publication evidence (the per-delivery record the freshness
    readers already have), never in a new table: the loss ratio is `not_returned` cells over
    expected cells, and the discarded-row count is a diagnostic beside it (design §4.4).
    """

    discarded_rows: int
    loss: GrainSetLoss
    slices: tuple[tuple[tuple[tuple[str, str], ...], GrainSetLoss], ...] = ()
    """Each graded slice's own loss, where the contract grades per slice (packs 5 to 7, per
    `identity_space`; design §4.4). Graded only, never withdrawn. Empty otherwise, so a contract
    without slices keeps its record unchanged."""
    repeated_totals: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()
    """Each repeated total's one value per cohort (`YYYY-MM-DD|acquisition channel group`), on a
    cohort contract (packs 14-15). Pack 15's `cohort_buyers_account_ids_check` is read from here
    by the Retention bake, which compares it with pack 14's cohort size (design §6.6, DS-13): an
    integrity count kept in the delivery's own evidence, never a dictionary metric or a figure.
    Empty otherwise, so every other record is unchanged."""

    @property
    def detail(self) -> str:
        """Render the record as canonical JSON, so a reader parses it without prose."""
        record: dict[str, object] = {
            "discarded_rows": self.discarded_rows,
            "expected_cells": self.loss.expected,
            "grade": self.loss.grade.value,
            "loss_ratio": str(self.loss.ratio),
            "not_returned_cells": self.loss.missing,
        }
        if self.repeated_totals:
            record["repeated_totals"] = {
                column: dict(values) for column, values in self.repeated_totals
            }
        if self.slices:
            record["slices"] = [
                {
                    "expected_cells": loss.expected,
                    "grade": loss.grade.value,
                    "loss_ratio": str(loss.ratio),
                    "not_returned_cells": loss.missing,
                    "slice": dict(key),
                }
                for key, loss in self.slices
            ]
        return json.dumps(record, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class _RunPartitionRequest:
    request: SemanticSidecarRequest
    tooling: SemanticSidecarTooling
    pack: SourcePack
    item: ContractAssessment
    publication: SourceAlignedPublication
    partition: PartitionIdentity


@dataclass(frozen=True, slots=True)
class _PreparedPartition:
    pack: SourcePack
    item: ContractAssessment
    publication: SourceAlignedPublication
    partition: PartitionIdentity
    compiled: CanonicalCompileResult
    rules: RuntimeTableRules
    conformance: SourcePackConformanceResult
    identity_documents: tuple[IdentityMappingsDocument, ...]
    """Every claim-check document the `conformances` name, ordered by digest."""
    control_results: tuple[PrepublicationRuleResult, ...] = ()
    sources: tuple[SourceAlignedPublication, ...] = ()
    conformances: tuple[SourcePackConformanceResult, ...] = ()
    residual: WithheldResidual | None = None
    evidence_only: bool = False
    withheld_only: bool = False
    suppression_proven: bool = False
    suppression_unavailable: bool = False
    result_contract_id: str = ""
    publication_group: str = ""
    cell_loss: CellLoss | None = None
    no_cell_returned: bool = False
    """A roster-graded grain set's delivery returned no cell: published with zero rows."""
    """A closed-enumeration partition's discarded rows and graded cell loss (ADR-099 point 9)."""
    withheld_ordinals: frozenset[int] | None = None
    """A discard partition's dropped row ordinals; None where the rows carry the discriminator."""

    @property
    def reporting_contract_id(self) -> str:
        """Original source contract represented by a base or derived residual candidate."""
        return self.result_contract_id or self.pack.contract_id

    @property
    def key(self) -> tuple[str, str]:
        return self.pack.contract_id, self.partition.identity


@dataclass(frozen=True, slots=True)
class _PreparedControlRequest:
    detail: _PreparedPartition
    reported: _PreparedPartition
    measure: str


@dataclass(frozen=True, slots=True)
class _PreparedGrant:
    prepared: _PreparedPartition
    attempt: AttemptId
    predecessor: AttemptId | None
    holder: LeaseHolderKey
    grant: LeaseGrant
    plan: CanonicalCurationPlan


@dataclass(frozen=True, slots=True)
class _CandidateStage:
    granted: _PreparedGrant
    candidate: PublicationCandidate
    decision_publishable: bool


@dataclass(frozen=True, slots=True)
class _PartitionOutcome:
    publication_id: PublicationId | None
    receipt: SemanticSidecarReceipt


def _targets_changing_dimension(contract: DataContract) -> bool:
    """Return whether a contract enters the write-discipline §2.1 single-emission path."""
    return not contract.targets_a_fact and "valid_from" in contract.target_columns


def _previous_history_rows(
    operation: _RunPartitionRequest,
    first_bucket: MeasurementBucket,
) -> tuple[CanonicalRow, ...]:
    """Read the scope's latest published history at or before the FIRST folded bucket.

    The fold base must predate the first boundary it will apply: under the restatement
    cadence the previous run has already published a partition at this delivery's latest
    bucket end, and a base read there would carry version transitions the fold is about
    to re-derive from re-observed earlier buckets — rejecting the re-observation as an
    out-of-order change. Reading at the first folded bucket's end keeps every base
    transition at or before the first boundary, and the fold re-derives the later
    transitions byte-identically from the delivery's own rows.
    """
    contract = operation.item.load.contract
    current = operation.tooling.publications.current_for_scope(operation.partition.scope)
    eligible = tuple(
        item for item in current if item.partition.bucket.window.end <= first_bucket.window.end
    )
    if not eligible:
        return ()
    selected = max(eligible, key=lambda item: item.partition.bucket.window.end)
    reader = operation.tooling.canonical_reader
    if reader is None:
        raise InvariantError(
            SIDECAR_INVARIANT,
            "Dimension history requires exact successful canonical read authority.",
            context={"contract": contract.contract_id},
        )
    return reader.rows(selected)


def _folded_dimension_history(
    operation: _RunPartitionRequest,
    compiles: tuple[tuple[MeasurementBucket, CanonicalCompileResult], ...],
) -> CanonicalCompileResult:
    """Fold the delivery's dimension observations ascending into one complete candidate.

    Write-discipline §2.1 (2026-09-22 amendment) rule 1: the scope's prior published
    history plus every bucket's observations fold in ascending bucket order, each bucket
    applying its own ``valid_from`` boundary; only the latest bucket's partition emits.
    ``operation`` addresses that latest partition.
    """
    contract = operation.item.load.contract
    business_key = tuple(column for column in contract.grain if column != "valid_from")
    tracked = tuple(
        column
        for column in contract.target_columns
        if column not in {*business_key, "valid_from", "valid_to", "is_current", "rows_suppressed"}
        and not column.startswith("source_")
    )
    rows = _previous_history_rows(operation, compiles[0][0])
    history: DimensionHistoryResult | None = None
    for bucket, compiled in compiles:
        history = build_dimension_history(
            DimensionHistoryRequest(
                previous=rows,
                observations=compiled.rows,
                business_key=business_key,
                tracked_attributes=tracked,
                boundary=datetime.combine(bucket.window.start, time.min, tzinfo=UTC),
            )
        )
        rows = history.rows
    if history is None:
        raise InvariantError(
            SIDECAR_INVARIANT,
            "Dimension history fold requires at least one compiled bucket.",
            context={"contract": contract.contract_id},
        )
    seen: set[IdentityMappingEvidence] = set()
    evidence: list[IdentityMappingEvidence] = []
    for _, compiled in compiles:
        for item in compiled.identity_evidence:
            if item not in seen:
                seen.add(item)
                evidence.append(item)
    latest = compiles[-1][1]
    return replace(
        latest,
        rows=history.rows,
        content_checksum=history.content_checksum,
        identity_evidence=tuple(evidence),
    )


def _validated_canonical(
    contract: DataContract,
    compiled: CanonicalCompileResult,
) -> CanonicalCompileResult:
    validated = validated_rows(contract, tuple(dict(row) for row in compiled.rows))
    rows: tuple[CanonicalRow, ...] = tuple(
        tuple(
            sorted(
                ((name, canonical_scalar(value, column=name)) for name, value in row.items()),
                key=lambda item: item[0],
            )
        )
        for row in validated
    )
    return replace(
        compiled,
        rows=rows,
        content_checksum=canonical_rows_checksum(rows),
    )


@dataclass(frozen=True, slots=True)
class _CompiledPartition:
    """One bucket's compiled canonical rows before validation and finalization."""

    compiled: CanonicalCompileResult
    residual: WithheldResidual | None
    item: ContractAssessment | None = None
    """A replacement assessment when compilation found a contract violation to quarantine."""
    cell_loss: CellLoss | None = None
    withheld_ordinals: frozenset[int] | None = None


def _with_period(row: dict[str, object], bucket: MeasurementBucket) -> dict[str, object]:
    """Stamp a periodic row's exact period from its measurement bucket (ADR-100).

    A cohort vintage (`custom`) also carries its `as_of_month`, the first day of its last month,
    so the vintage an answer reads is a column a figure can name (packs 14-15).
    """
    stamped: dict[str, object] = {
        **row,
        "period_start": bucket.window.start,
        "period_end": bucket.window.end,
        "time_grain": bucket.grain.value,
    }
    if bucket.grain is TimeGrain.CUSTOM:
        stamped[AS_OF_MONTH_COLUMN] = bucket.window.end.replace(day=1)
    return stamped


def _gap_template(operation: _RunPartitionRequest) -> dict[str, object]:
    """Build the delivery-owned columns of a `not_returned` row: everything but the cell.

    Every value comes from the contract, the source pack's media literal through the governed
    taxonomy, or the partition — never from a delivered row, because no row came back.
    """
    contract = operation.item.load.contract
    bucket = operation.partition.bucket
    row: dict[str, object] = {
        field.target_column: field.literal_value
        for field in contract.fields
        if field.literal_value is not None
    }
    row.update(conformed_provenance_values(contract))
    row.update(
        {
            field.target_column: None
            for field in contract.fields
            if field.null_meaning is NullMeaning.NOT_APPLICABLE_UNDER_OPTIONS
        }
    )
    if "marketplace_key" in contract.target_columns:
        # The partition's population: a cell keyed below the marketplace (pack 5's exposure
        # groups) still belongs to the delivery's one marketplace.
        row["marketplace_key"] = operation.partition.scope.population_key
    media = operation.pack.media
    taxonomy = operation.tooling.taxonomy
    if media is not None and media.literal_value is not None and taxonomy is not None:
        resolved = taxonomy.resolve(
            SourceIdentifier(media.namespace, media.literal_value), bucket.window.start
        )
        row["ecosystem_key"] = resolved.ecosystem.value
        row["platform_key"] = resolved.platform.value
    return _with_period(row, bucket)


_SOURCE_SET_LABELS = (
    ("exposure_source_set", "exposure_group"),
    ("acquisition_source_set", "acquisition_channel_group"),
)
"""A pack's source-set label and the canonical axis the taxonomy resolves it to (ADR-101):
pack 5's exposure source set, and packs 14-15's acquisition source set (design §6.6)."""


def _with_source_sets(operation: _RunPartitionRequest, row: dict[str, object]) -> dict[str, object]:
    """Resolve each source-set label on a row to its canonical key through the taxonomy."""
    contract = operation.item.load.contract
    written = set(contract.target_columns)
    resolved = dict(row)
    for label, axis in _SOURCE_SET_LABELS:
        if label not in written or axis not in written:
            continue
        taxonomy = operation.tooling.taxonomy
        if taxonomy is None:
            raise ValueError(f"{contract.contract_id}: a source-set label needs the taxonomy.")
        mapping = taxonomy.resolve_exposure_source_set(
            ResolveExposureSourceSetRequest(
                platform=PlatformKey(contract.platform_key),
                source_set=str(row[label]),
                on=operation.partition.bucket.window.start,
            )
        )
        resolved[axis] = (
            mapping.exposure_group.value
            if axis == "exposure_group"
            else mapping.acquisition_channel_group.value
        )
    return resolved


def _labelled(operation: _RunPartitionRequest, row: dict[str, object]) -> dict[str, object]:
    """Mark a refused delivery's row returned, resolving the source-set labels it can.

    The rows go forward only to be assessed and quarantined, so a label the taxonomy can't
    resolve (the undeclared value that refused the delivery) keeps its source text.
    """
    marked = {**row, CELL_STATE_COLUMN: "returned"}
    try:
        return _with_source_sets(operation, marked)
    except ValueError:
        for label, axis in _SOURCE_SET_LABELS:
            if axis in operation.item.load.contract.target_columns:
                marked[axis] = str(marked.get(label))
        return marked


_SLICE_MEDIA_COLUMNS = (
    "ecosystem_key",
    "platform_key",
    "channel_group_key",
    "channel_key",
    "ad_product_key",
    "campaign_key",
)
"""The media columns the canonical owner resolves for a delivered row (ADR-099 point 2)."""


def _open_columns(sets: tuple[ExpectedCellSet, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(column for item in sets for column in item.open_slices))


def _open_slice_media(
    sets: tuple[ExpectedCellSet, ...], rows: tuple[dict[str, object], ...]
) -> dict[tuple[object, ...], dict[str, object]]:
    """Each open slice's resolved media columns, read from a row that came back in it.

    Pack 7 fills its buckets per ad product that returned a row, so a gap's ad product, channel
    and channel group are the ones the canonical owner resolved for its returned siblings; a gap
    has no delivered row of its own to resolve them from.
    """
    columns = _open_columns(sets)
    if not columns:
        return {}
    found: dict[tuple[object, ...], dict[str, object]] = {}
    for row in rows:
        if row[CELL_STATE_COLUMN] == "not_returned":
            continue
        key = tuple(row.get(column) for column in columns)
        found.setdefault(
            key, {column: row[column] for column in _SLICE_MEDIA_COLUMNS if column in row}
        )
    return found


def _sibling_media(
    sets: tuple[ExpectedCellSet, ...],
    siblings: dict[tuple[object, ...], dict[str, object]],
    row: dict[str, object],
) -> dict[str, object]:
    columns = _open_columns(sets)
    if not columns:
        return {}
    return siblings.get(tuple(row.get(column) for column in columns), {})


def _filled_cells(
    operation: _RunPartitionRequest,
    compiled: CanonicalCompileResult,
    discarded: int,
) -> tuple[CanonicalCompileResult, CellLoss]:
    """Write every declared expected cell, `not_returned` when no row came back (plan D8).

    The contract declares its cells (`expected_cells`): pack 1 its delivery's one marketplace cell;
    packs 2 and 3 that cell with the returned ad product or campaign as an open slice, graded
    against the roster at serve (design §4.5), so nothing is written `not_returned` for them; pack 5
    three exposure groups by two identity spaces, graded per identity space. A declared variant
    (the registration's `pack_variant`) fills its own sets. A missing cell becomes a row with NULL
    measures, never 0. Raises `ShopperAggregateContractError` for a repeated or undeclared cell.
    """
    contract = operation.item.load.contract
    declared = contract.expected_cells
    if declared is None:  # pragma: no cover — DataContract requires it on a cell-state contract
        raise ValueError(f"{contract.contract_id}: records cell_state with no expected_cells.")
    named = operation.request.manifest.option_set.get(PACK_VARIANT_OPTION)
    try:
        sets = declared.for_variant(None if named is None else str(named))
    except ContractError as error:
        raise ShopperAggregateContractError(
            (ShopperAggregateFinding("CELL_VALUE_UNDECLARED", str(error)),)
        ) from None
    distinct = tuple(field.target_column for field in contract.fields if field.non_additive)
    nulls = tuple(
        field.target_column
        for field in contract.fields
        if field.nullable and not field.non_additive and not field.has_no_delivered_value
    )
    outcome = fill_declared_cells(
        DeclaredCellsRequest(
            sets=sets,
            rows=tuple(dict(row) for row in compiled.rows),
            delivery_marketplace=operation.partition.scope.population_key,
            measures=distinct,
            nulls=nulls,
            period=(
                operation.partition.bucket.window.start,
                operation.partition.bucket.window.end,
            ),
        )
    )
    template = _gap_template(operation)
    siblings = _open_slice_media(sets, outcome.rows)
    rows: tuple[CanonicalRow, ...] = tuple(
        tuple(
            sorted(
                (
                    (name, canonical_scalar(value, column=name))
                    for name, value in _with_source_sets(
                        operation,
                        row
                        if row[CELL_STATE_COLUMN] != "not_returned"
                        else {**template, **_sibling_media(sets, siblings, row), **row},
                    ).items()
                ),
                key=lambda item: item[0],
            )
        )
        for row in outcome.rows
    )
    expected = sum(item.expected for item in outcome.slices)
    missing = sum(item.missing for item in outcome.slices)
    loss = grade_loss(LossRequest(expected=expected, missing=missing, scope=LossScope.DELIVERY))
    graded = any(cell_set.slices for cell_set in sets)
    return (
        replace(compiled, rows=rows, content_checksum=canonical_rows_checksum(rows)),
        CellLoss(
            discarded_rows=discarded,
            loss=loss,
            repeated_totals=_cohort_totals(contract, rows),
            slices=tuple(
                (
                    item.key,
                    grade_loss(
                        LossRequest(
                            expected=item.expected, missing=item.missing, scope=LossScope.SLICE
                        )
                    ),
                )
                for item in outcome.slices
            )
            if graded
            else (),
        ),
    )


COHORT_KEY_COLUMNS = ("cohort_month", "acquisition_channel_group")
"""The key a cohort contract's repeated totals are recorded under (one value per cohort)."""


def _cohort_totals(
    contract: DataContract, rows: tuple[CanonicalRow, ...]
) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """Each repeated total's value per cohort and group, from the returned cells (packs 14-15).

    The repeated-total check already refused a delivery whose copies differ, so every returned
    row of a cohort carries the same value. Only a contract keyed by cohort records them.
    """
    if not contract.repeated_totals or not set(COHORT_KEY_COLUMNS) <= set(contract.target_columns):
        return ()
    recorded: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    for column, _keyed_by in contract.repeated_totals:
        values: dict[str, str] = {}
        for row in rows:
            mapped = dict(row)
            value = mapped.get(column)
            if mapped.get(CELL_STATE_COLUMN) != "returned" or value is None:
                continue
            key = "|".join(str(mapped[name]) for name in COHORT_KEY_COLUMNS)
            values.setdefault(key, str(value))
        recorded.append((column, tuple(sorted(values.items()))))
    return tuple(recorded)


def _cell_violation(
    item: ContractAssessment, error: ShopperAggregateContractError
) -> ContractAssessment:
    """Quarantine a delivery whose rows break the declared cells, naming each finding."""
    contract = item.load.contract
    violations = tuple(
        Violation(
            assertion=AssertionName.CLOSED_ENUMERATION,
            detail=f"{contract.contract_id}: {finding.code}: {finding.detail}",
            context=(("contract_id", contract.contract_id), ("finding_code", finding.code)),
        )
        for finding in error.findings
    )
    return replace(item, outcome=item.outcome.combined(LoadOutcome(violations=violations)))


def _report_cell_loss(operation: _RunPartitionRequest, cell_loss: CellLoss) -> None:
    """Log a structured WARN when a delivery's cell loss passes ADR-019's degrade threshold."""
    if cell_loss.loss.ratio <= DEGRADE_THRESHOLD:
        return
    report_operation(
        OperationLog(
            operation="ingest.shopper_aggregate_loss",
            component="semantic_sidecar",
            phase="prepare",
            outcome=cell_loss.loss.grade.value,
            severity=LogSeverity.WARNING,
            description=(
                "Expected shopper-aggregate cells didn't come back and are written not_returned."
            ),
            details={
                "contract_id": operation.item.load.contract.contract_id,
                "partition": operation.partition.identity,
                "loss_ratio": str(cell_loss.loss.ratio),
                "not_returned_cells": cell_loss.loss.missing,
                "expected_cells": cell_loss.loss.expected,
                "discarded_rows": cell_loss.discarded_rows,
                "degrade_threshold": str(DEGRADE_THRESHOLD),
            },
        ),
        _LOGGER,
    )


def _compile_discarding(operation: _RunPartitionRequest) -> _CompiledPartition:
    """Compile a `discard_suppressed_rows` partition: drop withheld rows, fill the cells.

    Suppressed rows never reach the fact. Their additive measures, where the contract maps any
    (pack 5's impressions, window spend, purchases and revenue), fold into the `_withheld`
    residual; their distinct counts never do (ADR-099 point 12). The bar check already ran in the
    assessment.
    """
    tooling = operation.tooling
    contract = operation.item.load.contract
    plan = plan_curation(
        CanonicalCurationRequest(
            partition=operation.partition,
            publisher=CanonicalPublisherKey("canonical-curation"),
            source_references=(operation.publication.reference,),
            available_sources=(operation.publication,),
            graph=tooling.graph,
        )
    )
    measures = measure_columns(contract, tooling.columns)
    split = split_discarded(contract, operation.request.document.rows, measures)
    residual = fold_discarded(
        DiscardFoldRequest(
            contract=contract,
            manifest=operation.request.manifest,
            rows=tuple(operation.item.load.rows[index] for index in split.withheld_ordinals),
            measures=measures,
        )
    )
    compiled = compile_canonical_rows(
        CanonicalCompileRequest(
            plan=plan,
            pack=operation.pack,
            contract=contract,
            mapped_rows=tuple(
                _with_period(dict(row), operation.partition.bucket)
                for row in operation.item.load.rows
            ),
            bridge=tooling.bridge,
            included_ordinals=frozenset(split.attributable_ordinals),
            taxonomy=tooling.taxonomy,
        )
    )
    try:
        filled, cell_loss = _filled_cells(operation, compiled, split.discarded)
    except ShopperAggregateContractError as error:
        # The rows that came back go forward unfilled, so the candidate is assessed, fails its
        # contract assertions and is quarantined like any other refused delivery.
        returned: tuple[CanonicalRow, ...] = tuple(
            tuple(
                sorted(
                    (
                        (name, canonical_scalar(value, column=name))
                        for name, value in _labelled(operation, dict(row)).items()
                    ),
                    key=lambda item: item[0],
                )
            )
            for row in compiled.rows
        )
        return _CompiledPartition(
            compiled=replace(
                compiled, rows=returned, content_checksum=canonical_rows_checksum(returned)
            ),
            residual=None,
            item=_cell_violation(operation.item, error),
        )
    _report_cell_loss(operation, cell_loss)
    return _CompiledPartition(
        compiled=filled,
        residual=residual,
        cell_loss=cell_loss,
        withheld_ordinals=frozenset(split.withheld_ordinals),
    )


def _compile_partition(operation: _RunPartitionRequest) -> _CompiledPartition:
    """Compile one partition's bucket-scoped canonical rows without history or validation."""
    if operation.item.load.contract.records_cell_state:
        return _compile_discarding(operation)
    tooling = operation.tooling
    plan = plan_curation(
        CanonicalCurationRequest(
            partition=operation.partition,
            publisher=CanonicalPublisherKey("canonical-curation"),
            source_references=(operation.publication.reference,),
            available_sources=(operation.publication,),
            graph=tooling.graph,
        )
    )
    contract = operation.item.load.contract
    source_rows = operation.item.load.rows
    if operation.pack.time.observation_basis is ObservationTimeBasis.SYSTEM_FIRST_OBSERVED:
        first_observed = operation.publication.landed_delivery.first_observed_at
        if first_observed is None:
            raise InvariantError(
                SIDECAR_INVARIANT,
                "System-observed snapshot lacks immutable first-observed evidence.",
                context={"contract": contract.contract_id},
            )
        source_rows = tuple(
            {
                **row,
                "valid_from": first_observed,
                "valid_to": OPEN_ENDED,
            }
            for row in source_rows
        )
    residual: WithheldResidual | None = None
    included_ordinals: frozenset[int] | None = None
    if contract.suppression is None:
        mapped_rows = source_rows
    else:
        split = split_withheld(contract, operation.request.manifest, source_rows)
        typed_attributable = split.attributable_rows
        aligned = [dict(row) for row in operation.item.load.rows]
        for ordinal, row in zip(
            split.attributable_ordinals,
            typed_attributable,
            strict=True,
        ):
            aligned[ordinal] = row
        mapped_rows = tuple(aligned)
        included_ordinals = frozenset(split.attributable_ordinals)
        # A discarding dimension (packs 9 and 13) drops its withheld rows: it has no measure to
        # keep in a residual, and its history keeps each omitted key's earlier version.
        residual = None if contract.discards_into_history else split.residual
    compiled = compile_canonical_rows(
        CanonicalCompileRequest(
            plan=plan,
            pack=operation.pack,
            contract=contract,
            mapped_rows=mapped_rows,
            bridge=tooling.bridge,
            included_ordinals=included_ordinals,
            taxonomy=tooling.taxonomy,
        )
    )
    if PROVIDER_COLUMN in contract.target_columns:
        compiled = _with_providers(operation, compiled)
    return _CompiledPartition(compiled=compiled, residual=residual)


PROVIDER_COLUMN = "provider"
"""`dim_audience_segment.provider`, assigned at load from the governed classification."""


def _with_providers(
    operation: _RunPartitionRequest, compiled: CanonicalCompileResult
) -> CanonicalCompileResult:
    """Stamp each segment's provider from the governed classification (ADR-103 point 7).

    By exact (namespace, name) entry, then the namespace rule; NULL where neither covers the
    segment, so it reads "Provider not recorded" and never "Amazon". A runtime without the
    classification fails closed rather than writing every provider NULL.
    """
    classification = operation.tooling.segment_providers
    contract = operation.item.load.contract
    if classification is None:
        raise InvariantError(
            SIDECAR_INVARIANT,
            "Segment providers require the governed provider classification.",
            context={"contract": contract.contract_id},
        )
    rows: tuple[CanonicalRow, ...] = tuple(
        tuple(
            sorted(
                (
                    *(item for item in row if item[0] != PROVIDER_COLUMN),
                    (
                        PROVIDER_COLUMN,
                        _provider_of(classification, dict(row)),
                    ),
                ),
                key=lambda item: item[0],
            )
        )
        for row in compiled.rows
    )
    return replace(compiled, rows=rows, content_checksum=canonical_rows_checksum(rows))


def _provider_of(
    classification: SegmentProviderClassification, row: dict[str, object]
) -> str | None:
    name = row.get("segment_name")
    provider = classification.classify(
        str(row["segment_namespace"]), name if isinstance(name, str) else None
    )
    return None if provider is None else provider.value


def _finalize_partition(
    operation: _RunPartitionRequest,
    compiled: CanonicalCompileResult,
    residual: WithheldResidual | None,
) -> _PreparedPartition:
    """Attach rules and conformance to one validated compiled partition."""
    tooling = operation.tooling
    rules = tooling.table_rules.get(operation.pack.dataset.value)
    if rules is None:
        raise InvariantError(
            SIDECAR_INVARIANT,
            "Generated runtime table rules are missing.",
            context={"dataset": operation.pack.dataset.value},
        )
    conformance, document = _source_conformance(
        operation.pack,
        operation.publication,
        compiled.identity_evidence,
    )
    return _PreparedPartition(
        pack=operation.pack,
        item=operation.item,
        publication=operation.publication,
        partition=operation.partition,
        compiled=compiled,
        rules=rules,
        conformance=conformance,
        identity_documents=(document,),
        sources=(operation.publication,),
        conformances=(conformance,),
        residual=residual,
        result_contract_id=operation.pack.contract_id,
    )


def _prepare_partition(operation: _RunPartitionRequest) -> _PreparedPartition:
    """Prepare one fact bucket's partition; dimensions take the single-emission path."""
    contract = operation.item.load.contract
    compiled_partition = _compile_partition(operation)
    if compiled_partition.item is not None:
        operation = replace(operation, item=compiled_partition.item)
    # A partition whose declared cells were broken carries its rows only to be quarantined with
    # the typed finding, never written; a NULL in a grain column (FREQUENCY_BUCKET_NULL) can't
    # pass the row model, so those rows aren't re-validated (they never reach a table).
    compiled = (
        compiled_partition.compiled
        if compiled_partition.item is not None
        else _validated_canonical(contract, compiled_partition.compiled)
    )
    prepared = _finalize_partition(operation, compiled, compiled_partition.residual)
    if compiled_partition.cell_loss is None:
        return prepared
    # A discard partition accounts for every withheld row itself: dropped, counted, and its
    # expected cell written not_returned (ADR-099 point 7). A roster-graded grain set writes no
    # not_returned cell, so a delivery with no cell publishes zero rows (design §4.5).
    return replace(
        prepared,
        suppression_proven=True,
        cell_loss=compiled_partition.cell_loss,
        no_cell_returned=not compiled.rows,
        withheld_ordinals=compiled_partition.withheld_ordinals,
    )


def _delivery_identity(publication: SourceAlignedPublication) -> str:
    landed = publication.landed_delivery
    return "|".join(
        (
            landed.manifest_key,
            landed.manifest_version,
            landed.manifest_checksum,
            landed.object_key,
            landed.object_version,
            landed.object_checksum,
        )
    )


def _control_scope(prepared: _PreparedPartition) -> ControlComparisonScope:
    control = prepared.pack.control
    if control is None:
        raise ValueError("uncontrolled source pack has no comparison scope.")
    scope = prepared.partition.scope
    options = tuple(
        (option.name, option.value) for option in prepared.publication.rowset.source_options
    )
    return ControlComparisonScope(
        group=control.group,
        commerce_scope=scope.commerce_scope,
        population_key=scope.population_key,
        # Detail and reported-total packs have distinct publisher profiles, by design. Controls
        # compare their shared source-option population, so use one group-owned profile while
        # preserving the exact encoded options. Using either publication lens would split a valid
        # reciprocal pair after profile became part of the immutable serving identity.
        lens=publication_lens_for(f"control_group.{control.group.replace('-', '_')}", options, 1),
        time_class=scope.time_class,
        bucket=prepared.partition.bucket,
        delivery_identity=_delivery_identity(prepared.publication),
    )


def _measure_total(rows: tuple[CanonicalRow, ...], measure: str) -> Decimal | None:
    present = [dict(row).get(measure) for row in rows if dict(row).get(measure) is not None]
    if not present:
        return None
    try:
        return sum((Decimal(str(value)) for value in present), start=Decimal(0))
    except ArithmeticError, ValueError:
        raise ValueError(f"control measure {measure!r} contains a non-numeric value.") from None


def _single_text(rows: tuple[CanonicalRow, ...], column: str) -> str | None:
    values = {str(dict(row)[column]) for row in rows if dict(row).get(column) is not None}
    return next(iter(values)) if len(values) == 1 else None


def _mapped_measure_total(rows: tuple[dict[str, object], ...], measure: str) -> Decimal | None:
    present = [row.get(measure) for row in rows if row.get(measure) is not None]
    if not present:
        return None
    try:
        return sum((Decimal(str(value)) for value in present), start=Decimal(0))
    except ArithmeticError, ValueError:
        raise ValueError(f"control measure {measure!r} contains a non-numeric value.") from None


def _suppression_scope(prepared: _PreparedPartition) -> ControlComparisonScope:
    scope = prepared.partition.scope
    return ControlComparisonScope(
        group=f"suppression-{prepared.item.load.contract.contract_id}",
        commerce_scope=scope.commerce_scope,
        population_key=scope.population_key,
        lens=scope.lens,
        time_class=scope.time_class,
        bucket=prepared.partition.bucket,
        delivery_identity=_delivery_identity(prepared.publication),
    )


def _suppression_control_results(
    prepared: _PreparedPartition,
    residual: WithheldResidual,
    columns: ColumnRegistry,
) -> tuple[PrepublicationRuleResult, ...]:
    contract = prepared.item.load.contract
    measures = control_total_measures(contract, columns)
    if not measures:
        return _control_failure(prepared.pack, "Suppression split has no measures.")
    basis = residual.provenance.get("basis_key")
    if basis is None:
        return _control_failure(prepared.pack, "Suppression residual has no measurement basis.")
    results: list[PrepublicationRuleResult] = []
    key_scope = _suppression_scope(prepared)
    # A discard delivery's mapped rows don't carry the discriminator (it isn't a fact column), so
    # its withheld rows are the ordinals the split dropped; every other contract maps it.
    withheld_ordinals = prepared.withheld_ordinals
    flagged = tuple(
        (index in withheld_ordinals)
        if withheld_ordinals is not None
        else row.get("rows_suppressed") is True
        for index, row in enumerate(prepared.item.load.rows)
    )
    for measure in sorted(measures):
        field = contract.field_for(measure)
        unit = "count"
        kept = tuple(zip(flagged, prepared.item.load.rows, strict=True))
        if field is not None and field.monetary:
            currency_column = field.currency_column or ""
            # A withheld row whose currency came back NULL holds an amount with no unit, so it
            # is in neither side of the money control: the residual never sums it
            # (`domain.curation.ingestion._folded`, AMC-V decision 5) and the reported side
            # leaves it out too. Attributable rows are all kept, so an attributable amount with
            # no currency is still refused below.
            kept = tuple(
                (withheld, row)
                for withheld, row in kept
                if not (withheld and not withheld_money_counts(row.get(currency_column)))
            )
        reported_rows = tuple(row for _, row in kept)
        # Suppression is observed for a measure only over the withheld rows that carry it: on a
        # multi-source withheld row AMC returns purchase-side sums NULL, so the residual holds
        # nothing for them and the reported side, which skips NULL, holds nothing either.
        observed = any(withheld and row.get(measure) is not None for withheld, row in kept)
        if field is not None and field.monetary:
            currency_column = field.currency_column or ""
            currencies = {
                str(row[currency_column])
                for row in reported_rows
                if row.get(currency_column) is not None
            }
            if not currencies and all(row.get(measure) is None for row in reported_rows):
                results.append(
                    _rule(
                        _RuleSpec(
                            PrepublicationRuleFamily.CONTROL_TOTALS,
                            f"control-total:{measure}",
                            True,
                            f"Every {measure} amount was withheld without its currency, so "
                            "neither side holds one to compare.",
                        )
                    )
                )
                continue
            if len(currencies) != 1:
                results.extend(
                    _control_failure(
                        prepared.pack,
                        f"Suppression measure {measure} has no single exact currency.",
                    )
                )
                continue
            unit = f"currency:{next(iter(currencies))}"
        key = ControlComparisonKey(
            scope=key_scope,
            source_revision=prepared.publication.source_revision,
            measurement_basis=MeasurementBasis(basis),
            unit=unit,
        )
        residual_value = residual.measures.get(measure)
        withheld = (
            None
            if residual_value is None
            else WithheldResidualTotal(key, Decimal(str(residual_value)))
        )
        results.append(
            evaluate_control_total(
                ControlTotalRequest(
                    candidate=CandidateMeasureTotal(
                        measure=measure,
                        key=key,
                        attributable=_measure_total(prepared.compiled.rows, measure),
                        withheld_residual=withheld,
                        suppression_observed=observed,
                    ),
                    reported=ReportedMeasureTotal(
                        measure=measure,
                        key=key,
                        value=_mapped_measure_total(reported_rows, measure),
                    ),
                )
            )
        )
    return tuple(results)


def _residual_prepared(
    prepared: _PreparedPartition,
    tooling: SemanticSidecarTooling,
    controls: tuple[PrepublicationRuleResult, ...],
) -> _PreparedPartition:
    residual = prepared.residual
    if residual is None:
        raise ValueError("residual candidate requires withheld evidence.")
    contract = prepared.item.load.contract
    dataset = CanonicalDatasetKey(f"{contract.target_table}{WITHHELD_SUFFIX}")
    allowed = {
        field.target_column
        for field in contract.fields
        if field.target_column not in contract.grain
        and field.target_column not in CONFORMED_PROVENANCE_COLUMNS
        and field.target_column != "rows_suppressed"
    }
    measures = measure_columns(contract, tooling.columns)
    key: dict[str, object] = {
        "contract_id": contract.contract_id,
        "marketplace_key": prepared.partition.scope.population_key,
        "period_start": residual.period_start,
        "period_end": residual.period_end,
        "option_set_hash": residual.option_set_hash,
        **dict(residual.provenance),
        # A frequency fact's residual names the scheme its withheld buckets were counted under.
        **(
            {}
            if residual.bucket_scheme is None
            else {RESIDUAL_BUCKET_SCHEME: residual.bucket_scheme}
        ),
        # A cohort fact's residual names its vintage, path and cohort grain (plan D11).
        **dict(residual.cohort_keys),
    }
    # A fact with a variant row kind keeps each row kind's withheld figures on its own residual
    # row (plan D11); any other fact's residual is the one delivery-keyed row.
    parts = residual.by_row_kind or ((None, residual.measures),)
    canonical_rows = tuple(
        cast(
            "CanonicalRow",
            tuple(
                sorted(
                    {
                        **key,
                        **({} if kind is None else {RESIDUAL_ROW_KIND: kind}),
                        **{
                            name: Decimal(str(value)) if name in measures else value
                            for name, value in values.items()
                            if name in allowed
                        },
                    }.items(),
                    key=lambda item: item[0],
                )
            ),
        )
        for kind, values in parts
    )
    partition = PartitionIdentity(
        scope=replace(prepared.partition.scope, dataset=dataset),
        bucket=prepared.partition.bucket,
    )
    rules = tooling.table_rules.get(dataset.value)
    if rules is None:
        raise InvariantError(
            SIDECAR_INVARIANT,
            "Generated residual runtime table rules are missing.",
            context={"dataset": dataset.value},
        )
    pack = replace(
        prepared.pack,
        contract_id=f"{prepared.pack.contract_id}-withheld",
        dataset=dataset,
        role=SourcePackRole.FACT,
        product=None,
        control=None,
    )
    group = f"suppression|{contract.contract_id}|{prepared.partition.identity}"
    return _PreparedPartition(
        pack=pack,
        item=prepared.item,
        publication=prepared.publication,
        partition=partition,
        compiled=CanonicalCompileResult(
            rows=canonical_rows,
            content_checksum=canonical_rows_checksum(canonical_rows),
            identity_evidence=prepared.compiled.identity_evidence,
        ),
        rules=rules,
        conformance=prepared.conformance,
        identity_documents=prepared.identity_documents,
        control_results=controls,
        sources=prepared.sources,
        conformances=prepared.conformances,
        evidence_only=True,
        suppression_proven=True,
        result_contract_id=contract.contract_id,
        publication_group=group,
    )


def _with_suppression_artifacts(
    prepared: tuple[_PreparedPartition, ...],
    tooling: SemanticSidecarTooling,
    manifest: Manifest,
) -> tuple[_PreparedPartition, ...]:
    residuals = tuple(item.residual for item in prepared if item.residual is not None)
    if not residuals:
        return prepared
    residual = residuals[0]
    # The residual is keyed on the delivery, so it is attributable only when the delivery is one
    # bucket: one day of a daily fact, or one complete period of a periodic one (pack 5's week).
    window = prepared[0].partition.bucket.window if len(prepared) == 1 else None
    if window is None or (window.start, window.end) != (manifest.period_start, manifest.period_end):
        return tuple(replace(item, suppression_unavailable=True) for item in prepared)
    base = prepared[0]
    controls = _suppression_control_results(base, residual, tooling.columns)
    group = f"suppression|{base.item.load.contract.contract_id}|{base.partition.identity}"
    conformed_base = replace(
        base,
        control_results=controls,
        withheld_only=not base.compiled.rows,
        suppression_proven=True,
        publication_group=group,
    )
    return conformed_base, _residual_prepared(conformed_base, tooling, controls)


def _control_measure_result(request: _PreparedControlRequest) -> PrepublicationRuleResult:
    detail = request.detail
    reported = request.reported
    measure = request.measure
    detail_field = detail.item.load.contract.field_for(measure)
    reported_field = reported.item.load.contract.field_for(measure)
    detail_monetary = detail_field is not None and detail_field.monetary
    reported_monetary = reported_field is not None and reported_field.monetary
    detail_basis = _single_text(detail.compiled.rows, "basis_key")
    reported_basis = _single_text(reported.compiled.rows, "basis_key")
    if detail_basis is None or reported_basis is None or detail_monetary != reported_monetary:
        return _rule(
            _RuleSpec(
                PrepublicationRuleFamily.CONTROL_TOTALS,
                f"control-total:{measure}",
                False,
                "Control candidates have ambiguous basis or disagree on monetary semantics.",
            )
        )
    detail_unit = "count"
    reported_unit = "count"
    if detail_monetary:
        detail_currency = _single_text(detail.compiled.rows, "currency_code")
        reported_currency = _single_text(reported.compiled.rows, "currency_code")
        if detail_currency is None or reported_currency is None:
            return _rule(
                _RuleSpec(
                    PrepublicationRuleFamily.CONTROL_TOTALS,
                    f"control-total:{measure}",
                    False,
                    "Monetary control candidates do not carry one exact currency each.",
                )
            )
        detail_unit = f"currency:{detail_currency}"
        reported_unit = f"currency:{reported_currency}"
    detail_key = ControlComparisonKey(
        scope=_control_scope(detail),
        source_revision=detail.publication.source_revision,
        measurement_basis=MeasurementBasis(detail_basis),
        unit=detail_unit,
    )
    reported_key = ControlComparisonKey(
        scope=_control_scope(reported),
        source_revision=reported.publication.source_revision,
        measurement_basis=MeasurementBasis(reported_basis),
        unit=reported_unit,
    )
    return evaluate_control_total(
        ControlTotalRequest(
            candidate=CandidateMeasureTotal(
                measure=measure,
                key=detail_key,
                attributable=_measure_total(detail.compiled.rows, measure),
                withheld_residual=None,
                suppression_observed=False,
            ),
            reported=ReportedMeasureTotal(
                measure=measure,
                key=reported_key,
                value=_measure_total(reported.compiled.rows, measure),
            ),
        )
    )


def _control_failure(pack: SourcePack, detail: str) -> tuple[PrepublicationRuleResult, ...]:
    return (
        _rule(
            _RuleSpec(
                PrepublicationRuleFamily.CONTROL_TOTALS,
                f"control.{pack.contract_id}.v2",
                False,
                detail,
            )
        ),
    )


def _bind_controls(
    prepared: tuple[_PreparedPartition, ...],
    columns: ColumnRegistry,
) -> tuple[_PreparedPartition, ...]:
    bound = {item.key: item for item in prepared}
    grouped: dict[tuple[str, ControlComparisonScope], list[_PreparedPartition]] = {}
    for item in prepared:
        control = item.pack.control
        if control is None:
            if item.control_results:
                continue
            bound[item.key] = replace(
                item,
                control_results=(
                    _rule(
                        _RuleSpec(
                            PrepublicationRuleFamily.CONTROL_TOTALS,
                            f"control.{item.pack.contract_id}.not-applicable.v2",
                            True,
                            "No external control relationship is declared for this source pack.",
                        )
                    ),
                ),
            )
            continue
        grouped.setdefault((control.group, _control_scope(item)), []).append(item)

    for members in grouped.values():
        details = tuple(
            item
            for item in members
            if item.pack.control is not None
            and item.pack.control.role is SourcePackControlRole.DETAIL
        )
        reported = tuple(
            item
            for item in members
            if item.pack.control is not None
            and item.pack.control.role is SourcePackControlRole.REPORTED
        )
        if len(details) != 1 or len(reported) != 1:
            for item in members:
                bound[item.key] = replace(
                    item,
                    control_results=_control_failure(
                        item.pack,
                        (
                            "Exact control group does not contain one detail and one "
                            "reported candidate."
                        ),
                    ),
                )
            continue
        detail = details[0]
        total = reported[0]
        measures = control_total_measures(
            detail.item.load.contract, columns
        ) & control_total_measures(total.item.load.contract, columns)
        if not measures:
            results = _control_failure(detail.pack, "Control group declares no common measures.")
        else:
            results = tuple(
                _control_measure_result(_PreparedControlRequest(detail, total, measure))
                for measure in sorted(measures)
            )
        by_reference = {
            detail.publication.reference: (detail.publication, detail.conformance),
            total.publication.reference: (total.publication, total.conformance),
        }
        ordered = tuple(
            by_reference[key] for key in sorted(by_reference, key=lambda item: item.value)
        )
        sources = tuple(item[0] for item in ordered)
        conformances = tuple(item[1] for item in ordered)
        documents = _merged_documents(detail.identity_documents, total.identity_documents)
        bound[detail.key] = replace(
            detail,
            control_results=results,
            sources=sources,
            conformances=conformances,
            identity_documents=documents,
        )
        bound[total.key] = replace(
            total,
            control_results=results,
            sources=sources,
            conformances=conformances,
            identity_documents=documents,
        )
    return tuple(bound[item.key] for item in prepared)


def _prepared_groups(
    prepared: tuple[_PreparedPartition, ...],
) -> tuple[tuple[_PreparedPartition, ...], ...]:
    groups: list[tuple[_PreparedPartition, ...]] = []
    visited: set[tuple[str, str]] = set()
    for item in sorted(prepared, key=lambda value: value.partition.identity):
        if item.key in visited:
            continue
        control = item.pack.control
        if item.publication_group:
            selected = tuple(
                candidate
                for candidate in prepared
                if candidate.publication_group == item.publication_group
            )
        elif control is None:
            selected = (item,)
        else:
            scope = _control_scope(item)
            selected = tuple(
                candidate
                for candidate in prepared
                if candidate.pack.control is not None
                and candidate.pack.control.group == control.group
                and _control_scope(candidate) == scope
            )
        selected = tuple(sorted(selected, key=lambda value: value.partition.identity))
        groups.append(selected)
        visited.update(member.key for member in selected)
    return tuple(groups)


def _recorded_pack_version(
    request: SemanticSidecarRequest, prepared: _PreparedPartition
) -> int | None:
    """Return the delivering pack version when the contract has pack-versioned or gated columns.

    Recorded on every partition of such a contract, so serving reads a day's coverage from
    publication state alone (`domain.measurement.pack_coverage`). A `served_where_published`
    column is covered by any recorded version, so the record is what says the day was published.
    """
    governance = request.manifest.governance
    contract = prepared.item.load.contract
    if governance is None or not (
        contract.pack_versioned_fields or contract.publication_gated_fields
    ):
        return None
    return governance.query_pack_version


def _candidate_stage(
    request: SemanticSidecarRequest,
    tooling: SemanticSidecarTooling,
    granted: _PreparedGrant,
) -> _CandidateStage:
    prepared = granted.prepared
    source_inputs = tuple(source.reference for source in prepared.sources)
    publication_id = PublicationId(
        _id(
            "publication",
            request.operation_id,
            *(source.value for source in source_inputs),
            prepared.partition.identity,
            prepared.compiled.content_checksum,
            str(granted.grant.state.fencing_token),
        )
    )
    revision = prepared.publication.source_revision
    subject = PublicationEvidenceSubject(
        publisher=granted.plan.publisher.value,
        partition=prepared.partition.identity,
        source_publications=tuple(source.value for source in source_inputs),
        source_revision="|".join(
            (
                revision.scheme.value,
                revision.precision.value,
                revision.observed_at.isoformat(),
                revision.source_id,
            )
        ),
        content_checksum=prepared.compiled.content_checksum,
    )
    decision = evaluate_prepublication(
        PrepublicationRequest(
            rule_set_version=RULE_SET_VERSION,
            subject=subject,
            source_conformance=prepared.conformances,
            candidate_results=_candidate_results(
                _CandidateRulesRequest(
                    item=prepared.item,
                    pack=prepared.pack,
                    partition=prepared.partition,
                    rows=prepared.compiled.rows,
                    rules=prepared.rules,
                    catalog=tooling.catalog,
                    control_results=prepared.control_results,
                    evidence_only=prepared.evidence_only,
                    withheld_only=prepared.withheld_only,
                    suppression_proven=prepared.suppression_proven,
                    suppression_unavailable=prepared.suppression_unavailable,
                    cell_loss=prepared.cell_loss,
                    query_pack_version=_recorded_pack_version(request, prepared),
                    no_cell_returned=prepared.no_cell_returned,
                )
            ),
        )
    )
    candidate = PublicationCandidate(
        publication_id=publication_id,
        publisher=granted.plan.publisher,
        partition=prepared.partition,
        attempt_id=granted.attempt,
        holder=granted.holder,
        fencing_token=granted.grant.state.fencing_token,
        source_inputs=source_inputs,
        source_revision=revision,
        content_checksum=prepared.compiled.content_checksum,
        evidence=decision.evidence,
        published_at=request.now,
    )
    return _CandidateStage(granted, candidate, decision.publishable)


@dataclass(frozen=True, slots=True)
class _PublishPreparedRequest:
    request: SemanticSidecarRequest
    tooling: SemanticSidecarTooling
    members: tuple[_PreparedPartition, ...]

    def __post_init__(self) -> None:
        if len(self.members) > MAX_COMMIT_GROUP_MEMBERS:
            raise ValueError(
                f"prepared publication group cannot exceed {MAX_COMMIT_GROUP_MEMBERS} members."
            )


@dataclass(frozen=True, slots=True)
class _PlannedPartition:
    prepared: _PreparedPartition
    attempt: AttemptId
    predecessor: AttemptId | None
    holder: LeaseHolderKey
    plan: CanonicalCurationPlan


def _attempt_id(
    request: SemanticSidecarRequest,
    prepared: _PreparedPartition,
    execution_id: str,
) -> AttemptId:
    return AttemptId(
        _id(
            "attempt",
            request.operation_id,
            execution_id,
            prepared.pack.contract_id,
            prepared.partition.identity,
        )
    )


def _plan_group(group: _PublishPreparedRequest) -> tuple[_PlannedPartition, ...]:
    """Plan every member before attempts or leases mutate durable state."""
    planned: list[_PlannedPartition] = []
    for prepared in group.members:
        source_references = tuple(source.reference for source in prepared.sources)
        plan = plan_curation(
            CanonicalCurationRequest(
                partition=prepared.partition,
                publisher=CanonicalPublisherKey("canonical-curation"),
                source_references=source_references,
                available_sources=prepared.sources,
                graph=group.tooling.graph,
            )
        )
        planned.append(
            _PlannedPartition(
                prepared=prepared,
                attempt=_attempt_id(group.request, prepared, group.request.execution_id),
                predecessor=(
                    None
                    if group.request.predecessor_execution_id is None
                    else _attempt_id(
                        group.request,
                        prepared,
                        group.request.predecessor_execution_id,
                    )
                ),
                holder=LeaseHolderKey(
                    _id(
                        "holder",
                        group.request.operation_id,
                        group.request.execution_id,
                        prepared.pack.contract_id,
                        prepared.partition.identity,
                    )
                ),
                plan=plan,
            )
        )
    return tuple(planned)


class _GroupAcquisition:
    """Own all attempt and lease cleanup from the first durable mutation onward."""

    def __init__(
        self,
        group: _PublishPreparedRequest,
        planned: tuple[_PlannedPartition, ...],
    ) -> None:
        self._group = group
        self._planned = planned
        self._started: list[_PlannedPartition] = []
        self._granted: list[_PreparedGrant] = []
        self._terminalized = False
        self._closed = False

    def __enter__(self) -> tuple[_PreparedGrant, ...]:
        try:
            for item in self._planned:
                _append(
                    self._group.tooling,
                    AttemptEvent(
                        item.attempt,
                        item.prepared.partition,
                        AttemptEventKind.STARTED,
                        self._group.request.now,
                        0,
                        predecessor=item.predecessor,
                    ),
                )
                self._started.append(item)
                # The lease is a real-time mutex: its expiry is compared against the
                # store's wall clock at commit. Anchoring it to the request's logical
                # publication instant made every lease in a long batch (reprocessing
                # many retained deliveries in one process) expire ten real minutes
                # after process start, refusing every commit after that point.
                lease = self._group.tooling.leases.acquire(
                    LeaseRequest(
                        item.prepared.partition,
                        item.holder,
                        self._group.tooling.clock(),
                        PARTITION_LEASE_DURATION,
                    )
                )
                if lease.grant is None:
                    raise DependencyError(
                        SIDECAR_BUSY,
                        "The semantic publication group is temporarily busy.",
                        context={
                            "partition": item.prepared.partition.identity,
                            "reason": lease.reason,
                        },
                    )
                self._granted.append(
                    _PreparedGrant(
                        item.prepared,
                        item.attempt,
                        item.predecessor,
                        item.holder,
                        lease.grant,
                        item.plan,
                    )
                )
        except BaseException as error:
            self._cleanup(error)
            raise
        return tuple(self._granted)

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: object,
    ) -> None:
        del error_type, traceback
        self._cleanup(error)

    def mark_terminal(self) -> None:
        """Record that every attempt reached an immutable terminal state."""
        self._terminalized = True

    def _cleanup(self, error: BaseException | None) -> None:
        if self._closed:
            return
        self._closed = True
        failures: list[BaseException] = []
        if error is not None and not self._terminalized:
            for item in self._started:
                try:
                    _append(
                        self._group.tooling,
                        AttemptEvent(
                            item.attempt,
                            item.prepared.partition,
                            AttemptEventKind.FAILED,
                            self._group.request.now,
                            1,
                            predecessor=item.predecessor,
                            reason=type(error).__name__,
                        ),
                    )
                except BaseException as cleanup_error:
                    failures.append(cleanup_error)
        for item in reversed(self._granted):
            try:
                released = self._group.tooling.leases.release(
                    item.grant,
                    at=self._group.request.now,
                )
                if not released.accepted:
                    failures.append(
                        InvariantError(
                            SIDECAR_INVARIANT,
                            "Semantic publication lease release was refused.",
                            context={
                                "partition": item.grant.state.partition.identity,
                                "reason": released.reason,
                            },
                        )
                    )
            except BaseException as cleanup_error:
                failures.append(cleanup_error)
        if not failures:
            return
        if error is None and len(failures) == 1:
            raise failures[0]
        grouped = ([error] if error is not None else []) + failures
        raise BaseExceptionGroup("Semantic publication cleanup failed.", grouped)


def _candidate_stages(
    group: _PublishPreparedRequest,
    granted: tuple[_PreparedGrant, ...],
) -> tuple[_CandidateStage, ...]:
    return tuple(_candidate_stage(group.request, group.tooling, item) for item in granted)


def _merged_documents(
    *groups: tuple[IdentityMappingsDocument, ...],
) -> tuple[IdentityMappingsDocument, ...]:
    """Union claim-check documents by digest, in digest order (one per distinct document)."""
    by_digest = {document.claim.sha256: document for group in groups for document in group}
    return tuple(by_digest[digest] for digest in sorted(by_digest))


def _identity_claims(evidence: PublicationEvidence) -> tuple[IdentityMappingsClaim, ...]:
    """Every identity claim an evidence set names; any other identity-result form refuses."""
    return tuple(
        parse_identity_claim(result.detail)
        for result in evidence.results
        if result.family is PrepublicationRuleFamily.IDENTITY
    )


def _write_identity_evidence(tooling: SemanticSidecarTooling, stage: _CandidateStage) -> None:
    """Write every claim-check document a candidate names, before any object or record does.

    The documents to write are derived from the candidate's own evidence, so a claim with no
    prepared document can never be published with a dangling reference.
    """
    documents = {
        document.claim.sha256: document for document in stage.granted.prepared.identity_documents
    }
    for claim in _identity_claims(stage.candidate.evidence):
        document = documents.get(claim.sha256)
        if document is None or document.claim != claim:
            raise InvariantError(
                IDENTITY_EVIDENCE_UNPREPARED,
                "Candidate evidence names an identity claim this delivery did not prepare.",
                context={"publication": stage.candidate.publication_id.value},
            )
        written = tooling.identity_evidence.put(document)
        if not written.accepted:
            raise InvariantError(
                IDENTITY_EVIDENCE_KEY_CONFLICT,
                "Identity claim-check key already contains different bytes.",
                context={"sha256": claim.sha256, "reason": written.reason},
            )


def _materialize_candidate_stages(
    group: _PublishPreparedRequest,
    stages: tuple[_CandidateStage, ...],
) -> dict[tuple[str, str], CanonicalMaterializeRequest]:
    requests: dict[tuple[str, str], CanonicalMaterializeRequest] = {}
    for stage in stages:
        # Claim check first: the candidate object and, later, the DynamoDB record name it.
        _write_identity_evidence(group.tooling, stage)
        request = CanonicalMaterializeRequest(
            CanonicalDestination.CANDIDATE,
            stage.candidate,
            stage.granted.prepared.compiled.rows,
            RULE_SET_VERSION,
        )
        write = group.tooling.canonical_writer.materialize(request)
        if not write.accepted:
            raise InvariantError(
                SIDECAR_INVARIANT,
                "Canonical candidate identity already contains different bytes.",
                context={
                    "publication": stage.candidate.publication_id.value,
                    "reason": write.reason,
                },
            )
        requests[stage.granted.prepared.key] = request
    return requests


@dataclass(frozen=True, slots=True)
class _ReceiptRequest:
    group: _PublishPreparedRequest
    prepared: _PreparedPartition
    candidate: CanonicalMaterializeRequest | None
    successful: CanonicalMaterializeRequest | None
    attempt_id: AttemptId
    predecessor_attempt_id: AttemptId | None
    terminal_event_kind: AttemptEventKind


def _receipt(request: _ReceiptRequest) -> SemanticSidecarReceipt:
    return SemanticSidecarReceipt(
        landed_delivery=request.group.request.landed_delivery,
        source_publications=request.prepared.sources,
        candidate=request.candidate,
        successful=request.successful,
        attempt_id=request.attempt_id,
        predecessor_attempt_id=request.predecessor_attempt_id,
        terminal_event_kind=request.terminal_event_kind,
    )


def _quarantine_stages(
    group: _PublishPreparedRequest,
    stages: tuple[_CandidateStage, ...],
    candidates: dict[tuple[str, str], CanonicalMaterializeRequest],
) -> dict[tuple[str, str], _PartitionOutcome]:
    outcomes: dict[tuple[str, str], _PartitionOutcome] = {}
    for stage in stages:
        event = AttemptEvent(
            stage.granted.attempt,
            stage.candidate.partition,
            AttemptEventKind.QUARANTINED,
            group.request.now,
            1,
            predecessor=stage.granted.predecessor,
            reason=", ".join(stage.candidate.evidence.failed_rules)
            or "a sibling candidate failed grouped publication",
        )
        _append(group.tooling, event)
        prepared = stage.granted.prepared
        outcomes[prepared.key] = _PartitionOutcome(
            publication_id=None,
            receipt=_receipt(
                _ReceiptRequest(
                    group=group,
                    prepared=prepared,
                    candidate=candidates[prepared.key],
                    successful=None,
                    attempt_id=stage.granted.attempt,
                    predecessor_attempt_id=stage.granted.predecessor,
                    terminal_event_kind=event.kind,
                )
            ),
        )
    return outcomes


def _split_stale_stages(
    group: _PublishPreparedRequest,
    stages: tuple[_CandidateStage, ...],
) -> tuple[tuple[_CandidateStage, ...], tuple[_CandidateStage, ...]]:
    """Divide stages by ADR-047's source-revision ordering against each incumbent CURRENT.

    A candidate strictly older than its incumbent never publishes: fencing orders competing
    writers, but only this comparison orders the source's own timeline, and without it a
    redelivered pre-restatement document moves CURRENT backwards. Equal revisions still
    publish — batch reprocessing republishes the same source state under changed rules.
    """
    fresh: list[_CandidateStage] = []
    stale: list[_CandidateStage] = []
    for stage in stages:
        current = group.tooling.publications.current(stage.candidate.partition)
        if (
            current is not None
            and current.publication_id != stage.candidate.publication_id
            and current.source_revision.is_newer_than(stage.candidate.source_revision)
        ):
            stale.append(stage)
        else:
            fresh.append(stage)
    return tuple(fresh), tuple(stale)


def _skip_stale_stages(
    group: _PublishPreparedRequest,
    stages: tuple[_CandidateStage, ...],
) -> dict[tuple[str, str], _PartitionOutcome]:
    """Terminate out-of-order candidates as SKIPPED attempts that publish nothing."""
    outcomes: dict[tuple[str, str], _PartitionOutcome] = {}
    for stage in stages:
        event = AttemptEvent(
            stage.granted.attempt,
            stage.candidate.partition,
            AttemptEventKind.SKIPPED,
            group.request.now,
            1,
            predecessor=stage.granted.predecessor,
            reason="source revision is older than the current publication",
        )
        _append(group.tooling, event)
        prepared = stage.granted.prepared
        outcomes[prepared.key] = _PartitionOutcome(
            publication_id=None,
            receipt=_receipt(
                _ReceiptRequest(
                    group=group,
                    prepared=prepared,
                    candidate=None,
                    successful=None,
                    attempt_id=stage.granted.attempt,
                    predecessor_attempt_id=stage.granted.predecessor,
                    terminal_event_kind=event.kind,
                )
            ),
        )
    return outcomes


def _successful_commits(
    group: _PublishPreparedRequest,
    stages: tuple[_CandidateStage, ...],
) -> tuple[CommitPublicationRequest, ...]:
    commits: list[CommitPublicationRequest] = []
    for stage in stages:
        current = group.tooling.publications.current(stage.candidate.partition)
        candidate = stage.candidate
        if current is not None and current.publication_id == candidate.publication_id:
            if not successful_matches_candidate(candidate, current):
                raise InvariantError(
                    SIDECAR_INVARIANT,
                    "Current publication id names different immutable candidate evidence.",
                    context={"publication": candidate.publication_id.value},
                )
            success = current
        else:
            success = publish_candidate(
                PublishCandidateRequest(
                    candidate,
                    stage.granted.plan,
                    stage.granted.grant,
                    current,
                )
            )
        commits.append(
            CommitPublicationRequest(
                success,
                stage.granted.grant,
                stage.granted.plan.authority,
                AttemptEvent(
                    stage.granted.attempt,
                    stage.candidate.partition,
                    AttemptEventKind.SUCCEEDED,
                    group.request.now,
                    1,
                    predecessor=stage.granted.predecessor,
                ),
            )
        )
    return tuple(commits)


def _materialize_successful_commits(
    group: _PublishPreparedRequest,
    stages: tuple[_CandidateStage, ...],
    commits: tuple[CommitPublicationRequest, ...],
) -> dict[tuple[str, str], CanonicalMaterializeRequest]:
    requests: dict[tuple[str, str], CanonicalMaterializeRequest] = {}
    for stage, commit in zip(stages, commits, strict=True):
        request = CanonicalMaterializeRequest(
            CanonicalDestination.SUCCESSFUL,
            commit.publication,
            stage.granted.prepared.compiled.rows,
            RULE_SET_VERSION,
        )
        write = group.tooling.canonical_writer.materialize(request)
        if not write.accepted:
            raise InvariantError(
                SIDECAR_INVARIANT,
                "Canonical successful identity already contains different bytes.",
                context={
                    "publication": commit.publication.publication_id.value,
                    "reason": write.reason,
                },
            )
        requests[stage.granted.prepared.key] = request
    return requests


def _completed_group(
    group: _PublishPreparedRequest,
    planned: tuple[_PlannedPartition, ...],
) -> dict[tuple[str, str], _PartitionOutcome] | None:
    completed: dict[tuple[str, str], _PartitionOutcome] = {}
    for item in planned:
        events = group.tooling.attempts.events_for(item.attempt)
        succeeded = tuple(event for event in events if event.kind is AttemptEventKind.SUCCEEDED)
        if not succeeded:
            return None
        current = group.tooling.publications.current(item.prepared.partition)
        if current is None or current.attempt_id != item.attempt:
            raise InvariantError(
                SIDECAR_INVARIANT,
                "Succeeded attempt has no exact current publication.",
                context={"attempt": item.attempt.value},
            )
        expected_events = (
            AttemptEvent(
                item.attempt,
                item.prepared.partition,
                AttemptEventKind.STARTED,
                current.published_at,
                0,
                predecessor=item.predecessor,
            ),
            AttemptEvent(
                item.attempt,
                item.prepared.partition,
                AttemptEventKind.SUCCEEDED,
                current.published_at,
                1,
                predecessor=item.predecessor,
            ),
        )
        if events != expected_events:
            raise InvariantError(
                SIDECAR_INVARIANT,
                "Completed attempt history differs from exact STARTED then SUCCEEDED evidence.",
                context={"attempt": item.attempt.value},
            )
        replay_grant = LeaseGrant(
            LeaseState(
                partition=item.prepared.partition,
                holder=item.holder,
                fencing_token=current.fencing_token,
                expires_at=current.published_at + PARTITION_LEASE_DURATION,
            )
        )
        stage = _candidate_stage(
            replace(group.request, now=current.published_at),
            group.tooling,
            _PreparedGrant(
                item.prepared,
                item.attempt,
                item.predecessor,
                item.holder,
                replay_grant,
                item.plan,
            ),
        )
        if not stage.decision_publishable or not successful_matches_candidate(
            stage.candidate,
            current,
        ):
            raise InvariantError(
                SIDECAR_INVARIANT,
                "Completed publication differs from freshly prepared semantic evidence.",
                context={"attempt": item.attempt.value},
            )
        candidate = CanonicalMaterializeRequest(
            CanonicalDestination.CANDIDATE,
            stage.candidate,
            item.prepared.compiled.rows,
            RULE_SET_VERSION,
        )
        successful = CanonicalMaterializeRequest(
            CanonicalDestination.SUCCESSFUL,
            current,
            item.prepared.compiled.rows,
            RULE_SET_VERSION,
        )
        for physical in (candidate, successful):
            attested = group.tooling.canonical_writer.attest(physical)
            if not attested.accepted or attested.created:
                raise InvariantError(
                    SIDECAR_INVARIANT,
                    "Completed publication lacks exact immutable physical attestation.",
                    context={"attempt": item.attempt.value},
                )
        # The claim-check documents are part of the physical evidence: read through the
        # digest-verifying port, which raises on absence or mismatch (never repairs).
        for claim in _identity_claims(current.evidence):
            group.tooling.identity_evidence.read(claim)
        completed[item.prepared.key] = _PartitionOutcome(
            publication_id=current.publication_id,
            receipt=_receipt(
                _ReceiptRequest(
                    group=group,
                    prepared=item.prepared,
                    candidate=candidate,
                    successful=successful,
                    attempt_id=item.attempt,
                    predecessor_attempt_id=item.predecessor,
                    terminal_event_kind=AttemptEventKind.SUCCEEDED,
                )
            ),
        )
    return completed


def _publish_prepared_group(
    group: _PublishPreparedRequest,
) -> dict[tuple[str, str], _PartitionOutcome]:
    planned = _plan_group(group)
    completed = _completed_group(group, planned)
    if completed is not None:
        return completed
    acquisition = _GroupAcquisition(group, planned)
    with acquisition as granted:
        stages = _candidate_stages(group, granted)
        if not all(stage.decision_publishable for stage in stages):
            candidates = _materialize_candidate_stages(group, stages)
            outcomes = _quarantine_stages(group, stages, candidates)
            acquisition.mark_terminal()
            return outcomes
        stages, stale = _split_stale_stages(group, stages)
        skipped = _skip_stale_stages(group, stale)
        if not stages:
            acquisition.mark_terminal()
            return skipped
        commits = _successful_commits(group, stages)
        commit_request = CommitPublicationGroupRequest(commits)
        group.tooling.publications.validate_group(commit_request)
        candidates = _materialize_candidate_stages(group, stages)
        successes = _materialize_successful_commits(group, stages, commits)
        committed = group.tooling.publications.commit_group(commit_request)
        if not committed.accepted:
            raise InvariantError(
                SIDECAR_INVARIANT,
                "Canonical grouped current pointers refused fenced successful publications.",
                context={"reason": committed.reason},
            )
        acquisition.mark_terminal()
        return {
            **skipped,
            **{
                stage.granted.prepared.key: _PartitionOutcome(
                    publication_id=stage.candidate.publication_id,
                    receipt=_receipt(
                        _ReceiptRequest(
                            group=group,
                            prepared=stage.granted.prepared,
                            candidate=candidates[stage.granted.prepared.key],
                            successful=successes[stage.granted.prepared.key],
                            attempt_id=stage.granted.attempt,
                            predecessor_attempt_id=stage.granted.predecessor,
                            terminal_event_kind=AttemptEventKind.SUCCEEDED,
                        )
                    ),
                )
                for stage in stages
            },
        }


@dataclass(frozen=True, slots=True)
class _PreparedDelivery:
    assessments: dict[str, ContractAssessment]
    blocked: dict[str, str]
    source_only: frozenset[str]
    identity_failures: dict[str, str]
    sources: dict[str, SourceAlignedPublication]
    partitions: tuple[_PreparedPartition, ...]


@dataclass(frozen=True, slots=True)
class _SourceMaterializeRequest:
    request: SemanticSidecarRequest
    tooling: SemanticSidecarTooling
    pack: SourcePack
    contract: DataContract


def _materialize_source(request: _SourceMaterializeRequest) -> SourceAlignedPublication:
    publication = compile_source_publication(
        CompileSourceRequest(
            pack=request.pack,
            contract=request.contract,
            manifest=request.request.manifest,
            document=request.request.document,
            landed_delivery=request.request.landed_delivery,
            published_at=request.request.now,
        )
    )
    result = request.tooling.source_writer.materialize(publication)
    if result.accepted:
        stored = request.tooling.source_reader.publication(publication.reference)
        if stored != publication:
            raise InvariantError(
                SIDECAR_INVARIANT,
                "Materialized source publication could not be read back exactly.",
                context={"source": publication.reference.value},
            )
        return publication
    existing = request.tooling.source_reader.publication(publication.reference)
    if existing is None or replace(existing, published_at=publication.published_at) != publication:
        raise InvariantError(
            SIDECAR_INVARIANT,
            "Existing source publication has conflicting immutable semantics.",
            context={"source": publication.reference.value, "reason": result.reason},
        )
    reconciled = request.tooling.source_writer.materialize(existing)
    if not reconciled.accepted:
        raise InvariantError(
            SIDECAR_INVARIANT,
            "Adopted source authority could not be projected exactly.",
            context={"source": existing.reference.value, "reason": reconciled.reason},
        )
    return existing


def _residual_only(pack: SourcePack, bucket: MeasurementBucket) -> bool:
    """Whether a bucket only carries a daily pack's withheld rows over a multi-day manifest.

    A withheld daily row with blank bounds is bucketed over the whole manifest period as `custom`
    and publishes nothing of its own. A periodic pack's `custom` bucket is a cohort vintage
    (packs 14-15, ADR-104), which publishes like any declared period.
    """
    return bucket.grain is TimeGrain.CUSTOM and pack.time.time_class is not FactTimeClass.PERIODIC


@dataclass(frozen=True, slots=True)
class _RunContractRequest:
    """One dimension contract's delivery-wide preparation input across all its buckets."""

    request: SemanticSidecarRequest
    tooling: SemanticSidecarTooling
    pack: SourcePack
    item: ContractAssessment
    publication: SourceAlignedPublication
    contract: DataContract
    buckets: tuple[MeasurementBucket, ...]


def _prepare_dimension_contract(
    operation: _RunContractRequest,
) -> tuple[_PreparedPartition, ...]:
    """Prepare exactly one folded dimension partition per scope at its latest bucket.

    Write-discipline §2.1 (2026-09-22 amendment) rule 1: earlier buckets of the same
    delivery emit no dimension partition; their observations fold ascending into the
    latest bucket's single complete candidate.
    """
    per_scope: dict[PublicationScope, list[_RunPartitionRequest]] = {}
    for bucket in operation.buckets:
        if operation.contract.suppression is not None and _residual_only(operation.pack, bucket):
            continue
        partition = _partition(
            _PartitionRequest(
                publication=operation.publication,
                pack=operation.pack,
                contract=operation.contract,
                bridge=operation.tooling.bridge,
                bucket=bucket,
                manifest_marketplace=operation.request.manifest.marketplace,
            )
        )
        per_scope.setdefault(partition.scope, []).append(
            _RunPartitionRequest(
                operation.request,
                operation.tooling,
                operation.pack,
                operation.item,
                operation.publication,
                partition,
            )
        )
    prepared: list[_PreparedPartition] = []
    for members in per_scope.values():
        # Buckets arrive ascending from the delivery enumeration, so the last member
        # is the scope's latest bucket — the one partition this delivery emits.
        latest = members[-1]
        compiled_members = tuple(
            (member.partition.bucket, _compile_partition(member)) for member in members
        )
        compiles = tuple((bucket, member.compiled) for bucket, member in compiled_members)
        folded = _folded_dimension_history(latest, compiles)
        finalized = _finalize_partition(
            latest,
            _validated_canonical(operation.contract, folded),
            # The withheld split runs over the whole delivery's mapped rows before
            # bucket scoping, so every member carries the same residual evidence.
            residual=compiled_members[-1][1].residual,
        )
        # A discarding dimension accounts for its withheld rows itself: dropped before the
        # fold, which keeps the omitted keys' earlier versions (packs 9 and 13, design §5.4).
        prepared.append(
            replace(finalized, suppression_proven=True)
            if operation.contract.discards_into_history
            else finalized
        )
    return tuple(prepared)


def _prepare_delivery(
    request: SemanticSidecarRequest,
    tooling: SemanticSidecarTooling,
) -> _PreparedDelivery:
    packs = {pack.contract_id: pack for pack in tooling.packs}
    assessments = {item.contract_id: item for item in request.assessments}
    contracts = {contract.contract_id: contract for contract in request.contracts}
    blocked: dict[str, str] = {}
    source_only: set[str] = set()
    identity_failures: dict[str, str] = {}
    sources: dict[str, SourceAlignedPublication] = {}
    prepared: list[_PreparedPartition] = []
    for contract_id in sorted(assessments):
        pack = packs[contract_id]
        if pack.readiness is not PackReadiness.READY:
            blocker = pack.blocker
            blocked[contract_id] = "blocked source pack" if blocker is None else blocker.detail
            continue
        publication = _materialize_source(
            _SourceMaterializeRequest(request, tooling, pack, contracts[contract_id])
        )
        sources[contract_id] = publication
        buckets = tuple(
            sorted(
                {row.measurement for row in publication.rowset.rows},
                key=lambda item: (item.window.start, item.window.end, item.grain.value),
            )
        )
        if not buckets and contracts[contract_id].records_cell_state:
            # A shopper aggregate expects its cell even when nothing came back: an empty and a
            # suppressed cell look the same (ADR-099 point 8), so the manifest's one period is
            # the bucket and the cell is written not_returned.
            try:
                buckets = (
                    periodic_manifest_bucket(pack, contracts[contract_id], request.manifest),
                )
            except ValueError as error:
                blocked[contract_id] = str(error)
                continue
        if not buckets:
            source_only.add(contract_id)
            continue
        contract_prepared: list[_PreparedPartition] = []
        if _targets_changing_dimension(contracts[contract_id]):
            # Write-discipline §2.1 rule 1: one dimension-history emission per delivery
            # and scope — the per-bucket emission path is retired for dimensions.
            try:
                contract_prepared.extend(
                    _prepare_dimension_contract(
                        _RunContractRequest(
                            request=request,
                            tooling=tooling,
                            pack=pack,
                            item=assessments[contract_id],
                            publication=publication,
                            contract=contracts[contract_id],
                            buckets=buckets,
                        )
                    )
                )
            except IdentityResolutionError as error:
                identity_failures[contract_id] = str(error)
        else:
            for bucket in buckets:
                if contracts[contract_id].suppression is not None and _residual_only(pack, bucket):
                    continue
                try:
                    partition = _partition(
                        _PartitionRequest(
                            publication=publication,
                            pack=pack,
                            contract=contracts[contract_id],
                            bridge=tooling.bridge,
                            bucket=bucket,
                            manifest_marketplace=request.manifest.marketplace,
                        )
                    )
                    contract_prepared.append(
                        _prepare_partition(
                            _RunPartitionRequest(
                                request,
                                tooling,
                                pack,
                                assessments[contract_id],
                                publication,
                                partition,
                            )
                        )
                    )
                except IdentityResolutionError as error:
                    identity_failures[contract_id] = str(error)
                    break
        if contract_id not in identity_failures:
            prepared.extend(
                _with_suppression_artifacts(
                    tuple(contract_prepared),
                    tooling,
                    request.manifest,
                )
            )
    return _PreparedDelivery(
        assessments=assessments,
        blocked=blocked,
        source_only=frozenset(source_only),
        identity_failures=identity_failures,
        sources=sources,
        partitions=tuple(prepared),
    )


@dataclass(frozen=True, slots=True)
class _ResultRequest:
    contract_id: str
    delivery: _PreparedDelivery
    controlled: tuple[_PreparedPartition, ...]
    outcomes: dict[tuple[str, str], _PartitionOutcome]


def _source_receipt(publication: SourceAlignedPublication) -> SemanticSidecarReceipt:
    return SemanticSidecarReceipt(
        landed_delivery=publication.landed_delivery,
        source_publications=(publication,),
    )


def _bucket_outcome(*, published: int, skipped: int, blocked: int) -> SemanticSidecarOutcome:
    """Classify a contract's candidate buckets by what applied.

    Any bucket that failed prepublication evidence blocks the contract. Otherwise a contract
    that published at least one bucket published, whatever it skipped as older than
    ``CURRENT``; one that skipped every bucket was superseded. No bucket at all is a block.
    """
    if blocked > 0:
        return SemanticSidecarOutcome.BLOCKED
    if published > 0:
        return SemanticSidecarOutcome.PUBLISHED
    if skipped > 0:
        return SemanticSidecarOutcome.SUPERSEDED
    return SemanticSidecarOutcome.BLOCKED


def _contract_result(request: _ResultRequest) -> SemanticSidecarResult:
    contract_id = request.contract_id
    if contract_id in request.delivery.blocked:
        return SemanticSidecarResult(
            contract_id,
            SemanticSidecarOutcome.BLOCKED,
            request.delivery.blocked[contract_id],
        )
    if contract_id in request.delivery.source_only:
        return SemanticSidecarResult(
            contract_id,
            SemanticSidecarOutcome.SOURCE_ONLY,
            "Empty source publication retained; no canonical bucket was invented.",
            receipts=(_source_receipt(request.delivery.sources[contract_id]),),
        )
    if contract_id in request.delivery.identity_failures:
        return SemanticSidecarResult(
            contract_id,
            SemanticSidecarOutcome.BLOCKED,
            request.delivery.identity_failures[contract_id],
            receipts=(_source_receipt(request.delivery.sources[contract_id]),),
        )
    candidates = tuple(
        item for item in request.controlled if item.reporting_contract_id == contract_id
    )
    selected = tuple(
        outcome for item in candidates if (outcome := request.outcomes.get(item.key)) is not None
    )
    publications = tuple(
        outcome.publication_id for outcome in selected if outcome.publication_id is not None
    )
    skipped_buckets = sum(
        1 for outcome in selected if outcome.receipt.terminal_event_kind is AttemptEventKind.SKIPPED
    )
    blocked_buckets = len(candidates) - len(publications) - skipped_buckets
    published_all = bool(publications) and blocked_buckets == 0 and skipped_buckets == 0
    return SemanticSidecarResult(
        contract_id,
        _bucket_outcome(
            published=len(publications), skipped=skipped_buckets, blocked=blocked_buckets
        ),
        (
            "Published every exact candidate bucket."
            if published_all
            else (
                f"Published {len(publications)} bucket(s); "
                f"{skipped_buckets} skipped as older than the current publication; "
                f"{blocked_buckets} failed grouped prepublication evidence."
            )
        ),
        publications,
        tuple(outcome.receipt for outcome in selected),
        expected_partition_count=len(candidates),
    )


def _prepared_projection_plan(
    delivery: _PreparedDelivery,
    controlled: tuple[_PreparedPartition, ...],
    groups: tuple[tuple[_PreparedPartition, ...], ...],
) -> PreparedProjectionPlan:
    """Project the private prepared roster into the public pre-authority callback payload."""
    return PreparedProjectionPlan(
        source_publications=tuple(
            delivery.sources[contract_id] for contract_id in sorted(delivery.sources)
        ),
        partitions=tuple(
            PreparedProjectionPartition(
                contract_id=item.pack.contract_id,
                dataset=item.partition.scope.dataset.value,
                partition_identity=item.partition.identity,
                source_publications=item.sources,
                canonical_rows=item.compiled.rows,
                content_checksum=item.compiled.content_checksum,
            )
            for item in controlled
        ),
        groups=tuple(tuple(member.key for member in group) for group in groups),
    )


@trace_boundary(INGEST_DELIVERY, INGEST_PARTITION)
def run_semantic_sidecar(
    request: SemanticSidecarRequest,
    tooling: SemanticSidecarTooling,
) -> tuple[SemanticSidecarResult, ...]:
    """Prepare every delivery candidate before any grouped fenced pointer transition."""
    contract_ids = tuple(sorted(contract.contract_id for contract in request.contracts))
    plan_digest = request.plan_digest or (
        f"sha256:{hashlib.sha256(request.operation_id.encode()).hexdigest()}"
    )
    telemetry_operation_id = request.telemetry_operation_id or _id(
        "operation", request.operation_id
    )
    with span(
        INGEST_DELIVERY,
        operation_id=telemetry_operation_id,
        execution_id=request.execution_id,
        predecessor_execution_id=request.predecessor_execution_id or "none",
        plan_digest=plan_digest,
        stage="prepare",
        completed=0,
        total=0,
        contract_ids=contract_ids,
    ) as delivery_span:
        delivery = _prepare_delivery(request, tooling)
        controlled = _bind_controls(delivery.partitions, tooling.columns)
        groups = _prepared_groups(controlled)
        # Write-discipline §2.1: the feasibility callback sees the complete prepared roster
        # here, after grouping and before the first group's attempt/lease mutation.
        request.feasibility(_prepared_projection_plan(delivery, controlled, groups))
        total = max(1, len(groups))
        outcomes: dict[tuple[str, str], _PartitionOutcome] = {}
        if not groups:
            request.progress(SemanticSidecarProgress("publish", contract_ids, 1, 1))
        for ordinal, group in enumerate(groups, start=1):
            group_contracts = tuple(sorted({item.reporting_contract_id for item in group}))
            bucket_starts = tuple(
                sorted({item.partition.bucket.window.start.isoformat() for item in group})
            )
            with span(
                INGEST_PARTITION,
                operation_id=telemetry_operation_id,
                execution_id=request.execution_id,
                predecessor_execution_id=request.predecessor_execution_id or "none",
                plan_digest=plan_digest,
                stage="publish",
                contract_ids=group_contracts,
                bucket_starts=bucket_starts,
                completed=ordinal - 1,
                total=total,
            ) as partition_span:
                outcomes.update(
                    _publish_prepared_group(_PublishPreparedRequest(request, tooling, group))
                )
                partition_span.record(completed=ordinal)
            request.progress(SemanticSidecarProgress("publish", group_contracts, ordinal, total))
        results = tuple(
            _contract_result(_ResultRequest(contract_id, delivery, controlled, outcomes))
            for contract_id in sorted(delivery.assessments)
        )
        delivery_span.record(stage="complete", completed=total, total=total)
        return results
