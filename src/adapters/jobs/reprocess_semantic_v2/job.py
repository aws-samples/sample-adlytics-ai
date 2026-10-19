# SPDX-License-Identifier: MIT-0
"""Plan exact-version, append-only Semantic V2 reprocessing from retained raw evidence."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import cast

from adapters.jobs.curation.manifests import checksum_of, read_manifest
from adapters.jobs.curation.readers import DeliveredFile, read
from adapters.jobs.ingest_delivery.semantic_sidecar import (
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
from adapters.outbound.telemetry.tracing import REPROCESS_DELIVERY, span, trace_boundary
from domain.curation.ingestion import (
    AssessmentRequest,
    ContractLoad,
    DiscardPolicy,
    assess_source_delivery,
)
from domain.curation.landing import (
    Manifest,
    contracts_for,
    dataset_prefix,
    misplacement,
    partition_for,
    require_installed_governance,
)
from domain.curation.mapping import MappingRequest, map_rows
from domain.data_contracts.contract import DataContract
from domain.data_contracts.validation import ColumnRegistry
from domain.semantics.reprocessing import (
    ContractArtifact,
    CreateReprocessPlanRequest,
    RawDeliveryBinding,
    RawObjectBinding,
    ReprocessPlan,
    ReprocessRequest,
)
from domain.semantics.source_aligned import LandedDeliveryRef
from domain.semantics.source_pack import PackReadiness, SourcePack
from domain.shared_kernel.provenance import ProducerGovernance
from domain.shared_kernel.values import DateRange
from ports.outbound.versioned_raw import RawVersion, VersionedRawReadPort

__all__ = [
    "ApplyReprocessRequest",
    "PlanReprocessRequest",
    "ReprocessCancelledError",
    "ReprocessProgress",
    "apply_reprocess",
    "plan_reprocess",
]


@dataclass(frozen=True, slots=True)
class PlanReprocessRequest:
    """Operator scope plus loaded contracts/packs and exact-version read authority."""

    request: ReprocessRequest
    contracts: tuple[DataContract, ...]
    packs: tuple[SourcePack, ...]
    raw: VersionedRawReadPort
    installed_governance: tuple[ProducerGovernance, ...] = ()
    contract_artifacts: tuple[ContractArtifact, ...] = ()
    semantic_authority_digest: str = ""

    def __post_init__(self) -> None:
        if not self.contract_artifacts:
            raise ValueError("reprocess planning requires installed contract artifacts.")
        if not self.semantic_authority_digest.strip():
            raise ValueError("reprocess planning requires its semantic authority digest.")


def _overlaps(left: DateRange, right: DateRange) -> bool:
    return left.start <= right.end and right.start <= left.end


def _declared_period(body: bytes, manifest_key: str) -> DateRange:
    """Read only the declared window needed to exclude unrelated retained evidence."""
    try:
        raw: object = json.loads(body)
        if not isinstance(raw, dict):
            raise TypeError("manifest root is not an object")
        manifest = cast("dict[object, object]", raw)
        start = manifest.get("period_start")
        end = manifest.get("period_end")
        if not isinstance(start, str) or not isinstance(end, str):
            raise TypeError("manifest period is not text")
        return DateRange(date.fromisoformat(start), date.fromisoformat(end))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(
            f"{manifest_key}: retained manifest period cannot be safely scoped."
        ) from error


def _one_current_version(versions: tuple[RawVersion, ...], key: str) -> RawVersion:
    matches = tuple(item for item in versions if item.key == key)
    concrete = tuple(item for item in matches if not item.delete_marker)
    if len(concrete) != 1 or any(item.delete_marker for item in matches):
        raise ValueError(f"{key}: retained evidence is missing, overwritten, or delete-marked.")
    return concrete[0]


def _declared_version(
    versions: tuple[RawVersion, ...],
    key: str,
    version_id: str | None,
) -> RawVersion:
    if version_id is None:
        raise ValueError(f"{key}: manifest does not declare an object VersionId.")
    matches = tuple(
        item
        for item in versions
        if item.key == key and item.version_id == version_id and not item.delete_marker
    )
    if len(matches) != 1:
        raise ValueError(f"{key}: declared retained VersionId {version_id!r} is missing.")
    return matches[0]


def _selected(request: PlanReprocessRequest) -> tuple[DataContract, ...]:
    contracts = {contract.contract_id: contract for contract in request.contracts}
    packs = {pack.contract_id: pack for pack in request.packs}
    selected: list[DataContract] = []
    for contract_id in request.request.contracts:
        contract = contracts.get(contract_id)
        pack = packs.get(contract_id)
        if contract is None or pack is None:
            raise ValueError(f"unknown Semantic V2 contract {contract_id!r}.")
        if pack.readiness is not PackReadiness.READY:
            raise ValueError(f"{contract_id}: blocked source pack cannot be reprocessed.")
        selected.append(contract)
    return tuple(selected)


def _artifacts_for(
    contract_ids: tuple[str, ...],
    installed: tuple[ContractArtifact, ...],
) -> tuple[ContractArtifact, ...]:
    by_id = {artifact.contract_id: artifact for artifact in installed}
    if len(by_id) != len(installed):
        raise ValueError("installed reprocess contract artifacts repeat an identity.")
    missing = tuple(contract_id for contract_id in contract_ids if contract_id not in by_id)
    if missing:
        raise ValueError(f"reprocess contract artifacts are missing {missing!r}.")
    return tuple(by_id[contract_id] for contract_id in contract_ids)


@dataclass(frozen=True, slots=True)
class _BindingRequest:
    manifest: Manifest
    manifest_version: RawVersion
    versions: tuple[RawVersion, ...]
    manifest_bytes: bytes
    contract_artifacts: tuple[ContractArtifact, ...]
    raw: VersionedRawReadPort


def _binding(request: _BindingRequest) -> RawDeliveryBinding:
    manifest = request.manifest
    if len(manifest.objects) != 1:
        raise ValueError(f"{manifest.manifest_key}: multipart reprocessing is not governed.")
    declared = manifest.objects[0]
    object_key = f"{manifest.prefix}{declared.name}"
    selected = _declared_version(request.versions, object_key, declared.version_id)
    object_body = request.raw.body(object_key, selected.version_id)
    actual_checksum = checksum_of(object_body.body)
    return RawDeliveryBinding(
        contract_artifacts=request.contract_artifacts,
        manifest_key=manifest.manifest_key,
        manifest_version_id=request.manifest_version.version_id,
        manifest_checksum=checksum_of(request.manifest_bytes),
        first_observed_at=request.manifest_version.modified_at,
        object=RawObjectBinding(
            key=object_key,
            version_id=selected.version_id,
            name=declared.name,
            declared_byte_count=declared.byte_count,
            actual_byte_count=len(object_body.body),
            declared_checksum=declared.checksum,
            actual_checksum=actual_checksum,
        ),
        period=DateRange(manifest.period_start, manifest.period_end),
    )


def plan_reprocess(request: PlanReprocessRequest) -> ReprocessPlan:
    """Discover only selected prefixes and bind every accepted delivery to exact versions."""
    selected = _selected(request)
    by_prefix: dict[str, list[DataContract]] = {}
    for contract in selected:
        by_prefix.setdefault(dataset_prefix(contract), []).append(contract)
    deliveries: list[RawDeliveryBinding] = []
    for prefix in sorted(by_prefix):
        versions = request.raw.versions(prefix)
        manifest_keys = sorted(
            {
                item.key
                for item in versions
                if item.key.endswith("/manifest.json")
                and not item.delete_marker
                and (
                    not request.request.retrieved_tokens
                    or any(
                        f"/retrieved={token}/" in item.key
                        for token in request.request.retrieved_tokens
                    )
                )
            }
        )
        for manifest_key in manifest_keys:
            manifest_version = _one_current_version(versions, manifest_key)
            versioned = request.raw.body(manifest_key, manifest_version.version_id)
            period = _declared_period(versioned.body, manifest_key)
            if not _overlaps(request.request.window, period):
                continue
            manifest = read_manifest(versioned.body.decode())
            wrong_place = misplacement(manifest, manifest_key)
            if wrong_place is not None:
                raise ValueError(wrong_place)
            if DateRange(manifest.period_start, manifest.period_end) != period:
                raise ValueError(f"{manifest_key}: parsed manifest period changed after scoping.")
            if not request.request.window.covers(period):
                raise ValueError(
                    f"{manifest_key}: delivery only partially overlaps the approved "
                    "reprocess window."
                )
            require_installed_governance(manifest, request.installed_governance)
            resolved = contracts_for(manifest, request.contracts)
            selected_ids = frozenset(contract.contract_id for contract in by_prefix[prefix])
            matching = tuple(
                contract.contract_id
                for contract in resolved.contracts
                if contract.contract_id in selected_ids
            )
            if not matching:
                continue
            deliveries.append(
                _binding(
                    _BindingRequest(
                        manifest=manifest,
                        manifest_version=manifest_version,
                        versions=versions,
                        manifest_bytes=versioned.body,
                        contract_artifacts=_artifacts_for(
                            matching,
                            request.contract_artifacts,
                        ),
                        raw=request.raw,
                    )
                )
            )
    return ReprocessPlan.create(
        CreateReprocessPlanRequest(
            request=request.request,
            prefixes=tuple(by_prefix),
            contract_artifacts=_artifacts_for(
                request.request.contracts,
                request.contract_artifacts,
            ),
            semantic_authority_digest=request.semantic_authority_digest,
            deliveries=tuple(deliveries),
        )
    )


def _ignore_progress(_progress: ReprocessProgress) -> None:
    """Default progress sink for callers that do not project operator telemetry."""


def _not_cancelled() -> bool:
    return False


@dataclass(frozen=True, slots=True)
class ReprocessProgress:
    """Bounded lineage and monotonic stage completion for one apply invocation."""

    operation_id: str
    execution_id: str
    predecessor_execution_id: str | None
    plan_digest: str
    stage: str
    completed: int
    total: int
    contract_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not all(
            (self.operation_id.strip(), self.execution_id.strip(), self.plan_digest.strip())
        ):
            raise ValueError("reprocess progress requires operation, execution, and plan digest.")
        if not self.stage.strip():
            raise ValueError("reprocess progress stage cannot be blank.")
        if self.completed < 0 or self.total < 0 or self.completed > self.total:
            raise ValueError("reprocess progress counts must be bounded and nonnegative.")


class ReprocessCancelledError(Exception):
    """Typed cooperative cancellation raised only at safe adapter checkpoints."""


@dataclass(frozen=True, slots=True)
class ApplyReprocessRequest:
    """Digest-bound exact plan and composed V2 authorities; no legacy mutation authority exists."""

    plan: ReprocessPlan
    contracts: tuple[DataContract, ...]
    registry: ColumnRegistry
    raw: VersionedRawReadPort
    semantic: SemanticSidecarTooling
    execution_id: str
    applied_at: datetime
    predecessor_execution_id: str | None = None
    installed_governance: tuple[ProducerGovernance, ...] = ()
    discard_policies: tuple[DiscardPolicy, ...] = ()
    """The installed `discard_suppressed_rows` packs, as the delivery handler passes them, so a
    reprocessed `records_cell_state` delivery is assessed under its own pack (ADR-099 point 7)."""
    contract_artifacts: tuple[ContractArtifact, ...] = ()
    semantic_authority_digest: str = ""
    progress: Callable[[ReprocessProgress], None] = _ignore_progress
    cancelled: Callable[[], bool] = _not_cancelled

    def __post_init__(self) -> None:
        if not self.contract_artifacts:
            raise ValueError("reprocess apply requires installed contract artifacts.")
        if not self.semantic_authority_digest.strip():
            raise ValueError("reprocess apply requires its semantic authority digest.")
        if not self.execution_id.strip():
            raise ValueError("reprocess apply requires a fresh execution id.")
        if self.predecessor_execution_id is not None:
            if not self.predecessor_execution_id.strip():
                raise ValueError("reprocess predecessor execution id cannot be blank.")
            if self.predecessor_execution_id == self.execution_id:
                raise ValueError("reprocess execution cannot be its own predecessor.")
        if self.applied_at.tzinfo is None:
            raise ValueError("reprocess apply instant must be timezone-aware.")


def _prepare_reprocess_delivery(
    request: ApplyReprocessRequest,
    delivery: RawDeliveryBinding,
    contracts: dict[str, DataContract],
) -> SemanticSidecarRequest:
    """Fully validate one exact delivery before any delivery in the plan may mutate state."""
    manifest_body = request.raw.body(
        delivery.manifest_key,
        delivery.manifest_version_id,
    )
    object_body = request.raw.body(delivery.object.key, delivery.object.version_id)
    if checksum_of(manifest_body.body) != delivery.manifest_checksum:
        raise ValueError("apply-time manifest bytes differ from the digest-bound plan.")
    if checksum_of(object_body.body) != delivery.object.actual_checksum:
        raise ValueError("apply-time object bytes differ from the digest-bound plan.")
    manifest_text = manifest_body.body.decode()
    manifest = read_manifest(manifest_text)
    require_installed_governance(manifest, request.installed_governance)
    expected_artifacts = _artifacts_for(
        delivery.contract_ids,
        request.contract_artifacts,
    )
    if delivery.contract_artifacts != expected_artifacts:
        raise ValueError("apply-time contract artifacts differ from the digest-bound plan.")
    unknown = tuple(
        contract_id for contract_id in delivery.contract_ids if contract_id not in contracts
    )
    if unknown:
        raise ValueError(f"reprocess plan names unknown contracts {unknown!r}.")
    resolved_all = contracts_for(manifest, tuple(contracts.values()))
    selected_ids = frozenset(delivery.contract_ids)
    selected = tuple(
        contract for contract in resolved_all.contracts if contract.contract_id in selected_ids
    )
    if frozenset(contract.contract_id for contract in selected) != selected_ids:
        raise ValueError("reprocess plan contract scope differs from full-registry resolution.")
    document = read(
        DeliveredFile(
            text=object_body.body.decode(),
            source_format=resolved_all.source_format,
            manifest_text=manifest_text,
        )
    )
    loads = tuple(
        ContractLoad(
            contract,
            partition_for(manifest, contract),
            map_rows(MappingRequest(contract, document)),
        )
        for contract in selected
    )
    assessment = assess_source_delivery(
        AssessmentRequest(
            manifest=manifest,
            document=document,
            loads=loads,
            registry=request.registry,
            discard_policies=request.discard_policies,
            # Read exactly as the delivery handler does; with no policy there is no automatic
            # variant, so `read_variant_evidence` would return {} without reading the tooling.
            variant_evidence=(
                read_variant_evidence(
                    VariantEvidenceRequest(
                        manifest=manifest,
                        contracts=selected,
                        discard_policies=request.discard_policies,
                        packs=request.semantic.packs,
                        bridge=request.semantic.bridge,
                        publications=request.semantic.publications,
                        governed=request.contracts,
                    )
                )
                if request.discard_policies
                else {}
            ),
        )
    )
    return SemanticSidecarRequest(
        manifest=manifest,
        document=document,
        landed_delivery=LandedDeliveryRef(
            manifest_key=delivery.manifest_key,
            manifest_version=delivery.manifest_version_id,
            manifest_checksum=delivery.manifest_checksum,
            object_key=delivery.object.key,
            object_version=delivery.object.version_id,
            object_checksum=delivery.object.actual_checksum,
            first_observed_at=delivery.first_observed_at,
        ),
        assessments=assessment.assessments,
        contracts=selected,
        now=request.applied_at,
        operation_id="|".join(
            (
                request.plan.request.operation_id.value,
                delivery.identity,
            )
        ),
        execution_id=request.execution_id,
        predecessor_execution_id=request.predecessor_execution_id,
        plan_digest=request.plan.digest,
    )


def _checkpoint(request: ApplyReprocessRequest) -> None:
    if request.cancelled():
        raise ReprocessCancelledError("reprocess cancellation requested")


@trace_boundary(REPROCESS_DELIVERY)
def apply_reprocess(request: ApplyReprocessRequest) -> tuple[SemanticSidecarResult, ...]:
    """Apply one fully preflighted exact plan without reset, truncation, or deletion."""
    _checkpoint(request)
    if request.plan.semantic_authority_digest != request.semantic_authority_digest:
        raise ValueError("apply-time semantic authority differs from the reviewed plan.")
    current_artifacts = _artifacts_for(
        request.plan.request.contracts,
        request.contract_artifacts,
    )
    if request.plan.contract_artifacts != current_artifacts:
        raise ValueError("apply-time selected contract artifacts differ from the reviewed plan.")
    by_id = {contract.contract_id: contract for contract in request.contracts}
    delivery_count = len(request.plan.deliveries)
    total = delivery_count * 2
    prepared: list[SemanticSidecarRequest] = []
    for completed, delivery in enumerate(request.plan.deliveries, start=1):
        _checkpoint(request)
        prepared.append(_prepare_reprocess_delivery(request, delivery, by_id))
        request.progress(
            ReprocessProgress(
                operation_id=request.plan.request.operation_id.value,
                execution_id=request.execution_id,
                predecessor_execution_id=request.predecessor_execution_id,
                plan_digest=request.plan.digest,
                stage="preflight",
                completed=completed,
                total=total,
            )
        )

    outcomes: list[SemanticSidecarResult] = []
    for ordinal, delivery_request in enumerate(prepared, start=1):
        _checkpoint(request)
        completed = delivery_count + ordinal - 1
        contract_ids = tuple(contract.contract_id for contract in delivery_request.contracts)

        def sidecar_progress(
            event: SemanticSidecarProgress,
            *,
            completed_before_delivery: int = completed,
        ) -> None:
            request.progress(
                ReprocessProgress(
                    operation_id=request.plan.request.operation_id.value,
                    execution_id=request.execution_id,
                    predecessor_execution_id=request.predecessor_execution_id,
                    plan_digest=request.plan.digest,
                    stage=f"apply.{event.stage}",
                    completed=completed_before_delivery,
                    total=total,
                    contract_ids=event.contract_ids,
                )
            )

        observed_request = replace(delivery_request, progress=sidecar_progress)
        with span(
            REPROCESS_DELIVERY,
            operation_id=request.plan.request.operation_id.value,
            execution_id=request.execution_id,
            predecessor_execution_id=request.predecessor_execution_id or "none",
            plan_digest=request.plan.digest,
            stage="apply",
            completed=completed,
            total=total,
            contract_ids=contract_ids,
        ) as delivery_span:
            outcomes.extend(run_semantic_sidecar(observed_request, request.semantic))
            completed += 1
            delivery_span.record(completed=completed)
        request.progress(
            ReprocessProgress(
                operation_id=request.plan.request.operation_id.value,
                execution_id=request.execution_id,
                predecessor_execution_id=request.predecessor_execution_id,
                plan_digest=request.plan.digest,
                stage="apply",
                completed=completed,
                total=total,
            )
        )
    _checkpoint(request)
    return tuple(outcomes)
