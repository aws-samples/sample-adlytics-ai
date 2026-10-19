# SPDX-License-Identifier: MIT-0
"""V2-only ingestion: parse once, run source-derived checks, and publish semantically."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from adapters.amc_csv import AmcCsvEscapeError
from adapters.jobs.curation.manifests import write_manifest
from adapters.jobs.curation.readers import DeliveredFile, read
from adapters.jobs.ingest_delivery.semantic_sidecar import (
    PreparedProjectionPlan,
    SemanticSidecarOutcome,
    SemanticSidecarProgress,
    SemanticSidecarRequest,
    SemanticSidecarResult,
    SemanticSidecarTooling,
    run_semantic_sidecar,
)
from adapters.jobs.ingest_delivery.variant_evidence import (
    VariantEvidenceRequest,
    read_variant_evidence,
)
from adapters.outbound.telemetry.tracing import INGEST_PARSE, span, trace_boundary
from domain.curation.ingestion import (
    AssessmentRequest,
    ContractLoad,
    DiscardPolicy,
    assess_source_delivery,
)
from domain.curation.landing import (
    contracts_for,
    partition_for,
    require_installed_governance,
)
from domain.curation.mapping import MappingRequest, map_rows

if TYPE_CHECKING:  # pragma: no cover
    from datetime import datetime

    from adapters.jobs.ingest_delivery.landing import LoadedDelivery
    from domain.data_contracts.contract import DataContract
    from domain.data_contracts.validation import ColumnRegistry
    from domain.shared_kernel.provenance import ProducerGovernance

__all__ = ["SemanticIngestionRequest", "ingest_semantic_delivery"]


def _ignore_progress(_event: SemanticSidecarProgress) -> None:
    """Default progress sink for callers that do not project lifecycle state."""


def _accept_prepared_plan(_plan: PreparedProjectionPlan) -> None:
    """Default feasibility callback for callers that carry no plan-capacity bound."""


@dataclass(frozen=True, slots=True)
class SemanticIngestionRequest:
    """One exact landed delivery and the source-neutral inputs needed to publish V2."""

    delivery: LoadedDelivery
    contracts: tuple[DataContract, ...]
    columns: ColumnRegistry
    now: datetime
    operation_id: str
    execution_id: str
    plan_digest: str
    predecessor_execution_id: str | None = None
    telemetry_operation_id: str = ""
    installed_governance: tuple[ProducerGovernance, ...] = ()
    discard_policies: tuple[DiscardPolicy, ...] = ()
    """The installed packs declaring `discard_suppressed_rows` (ADR-099 point 7).

    Built by the composition root from the same governed query packs as `installed_governance`,
    so a delivery is assessed under exactly the pack its manifest names.
    """
    progress: Callable[[SemanticSidecarProgress], None] = _ignore_progress
    feasibility: Callable[[PreparedProjectionPlan], None] = _accept_prepared_plan

    def __post_init__(self) -> None:
        if self.now.tzinfo is None:
            raise ValueError("semantic ingestion instant must be timezone-aware.")
        if not self.operation_id.strip():
            raise ValueError("semantic ingestion requires a stable operation id.")
        if not self.execution_id.strip():
            raise ValueError("semantic ingestion requires an execution id.")
        if not self.plan_digest.strip():
            raise ValueError("semantic ingestion requires a plan digest.")
        if self.predecessor_execution_id is not None:
            if not self.predecessor_execution_id.strip():
                raise ValueError("semantic ingestion predecessor execution id cannot be blank.")
            if self.predecessor_execution_id == self.execution_id:
                raise ValueError("semantic ingestion execution cannot be its own predecessor.")


@trace_boundary(INGEST_PARSE)
def ingest_semantic_delivery(
    request: SemanticIngestionRequest,
    tooling: SemanticSidecarTooling,
) -> tuple[SemanticSidecarResult, ...]:
    """Publish one source-assessed delivery through Semantic V2."""
    delivery = request.delivery
    require_installed_governance(delivery.manifest, request.installed_governance)
    resolved = contracts_for(delivery.manifest, request.contracts)
    telemetry_operation_id = request.telemetry_operation_id or request.operation_id
    with span(
        INGEST_PARSE,
        operation_id=telemetry_operation_id,
        execution_id=request.execution_id,
        predecessor_execution_id=request.predecessor_execution_id or "none",
        plan_digest=request.plan_digest,
        stage="parse",
        completed=0,
        total=1,
        contract_count=len(resolved.contracts),
    ) as parse_span:
        try:
            document = read(
                DeliveredFile(
                    text=delivery.text,
                    source_format=resolved.source_format,
                    manifest_text=write_manifest(delivery.manifest),
                )
            )
        except AmcCsvEscapeError as error:
            # A decided refusal, not a retryable failure: the bytes are immutable, so a retry
            # reads the same escape. Each contract is blocked with the finding, which counts in
            # IngestPartitions Action=blocked (the BlockedPartitions alarm), and nothing publishes.
            parse_span.record(assessment_count=0, completed=1, outcome="blocked")
            return tuple(
                SemanticSidecarResult(
                    contract.contract_id, SemanticSidecarOutcome.BLOCKED, str(error.finding)
                )
                for contract in resolved.contracts
            )
        loads = tuple(
            ContractLoad(
                contract=contract,
                partition=partition_for(delivery.manifest, contract),
                rows=map_rows(MappingRequest(contract=contract, source=document)),
            )
            for contract in resolved.contracts
        )
        assessment = assess_source_delivery(
            AssessmentRequest(
                manifest=delivery.manifest,
                document=document,
                loads=loads,
                registry=request.columns,
                discard_policies=request.discard_policies,
                variant_evidence=read_variant_evidence(
                    VariantEvidenceRequest(
                        manifest=delivery.manifest,
                        contracts=resolved.contracts,
                        discard_policies=request.discard_policies,
                        packs=tooling.packs,
                        bridge=tooling.bridge,
                        publications=tooling.publications,
                        governed=request.contracts,
                    )
                ),
            )
        )
        parse_span.record(
            assessment_count=len(assessment.assessments),
            completed=1,
            outcome="completed",
        )
    return run_semantic_sidecar(
        SemanticSidecarRequest(
            manifest=delivery.manifest,
            document=document,
            landed_delivery=delivery.landed_ref,
            assessments=assessment.assessments,
            contracts=resolved.contracts,
            now=request.now,
            operation_id=request.operation_id,
            execution_id=request.execution_id,
            predecessor_execution_id=request.predecessor_execution_id,
            plan_digest=request.plan_digest,
            telemetry_operation_id=telemetry_operation_id,
            progress=request.progress,
            feasibility=request.feasibility,
        ),
        tooling,
    )
