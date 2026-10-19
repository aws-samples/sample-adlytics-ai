# SPDX-License-Identifier: MIT-0
"""Semantic V2 publication ordering, blocking, isolated ingestion, and immutable replay."""

from __future__ import annotations

import json
import pathlib
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast
from unittest.mock import MagicMock

import pytest
from scale_envelope import assert_within_bound

from adapters.amc_csv import dict_rows, write_rows
from adapters.jobs.contract_validation.loader import load_contracts, load_registry
from adapters.jobs.curation.manifests import (
    BuildManifestRequest,
    build_manifest,
    checksum_of,
    declared_columns_of,
    write_manifest,
)
from adapters.jobs.curation.readers import DeliveredFile, read
from adapters.jobs.fixture_generation.catalog import (
    BULK_CAMPAIGN_COUNT,
    load_synthetic_entity_catalog,
)
from adapters.jobs.ingest_delivery import semantic_ingestion as semantic_ingestion_module
from adapters.jobs.ingest_delivery import semantic_sidecar as semantic_sidecar_module
from adapters.jobs.ingest_delivery.landing import LoadedDelivery
from adapters.jobs.ingest_delivery.semantic_ingestion import (
    SemanticIngestionRequest,
    ingest_semantic_delivery,
)
from adapters.jobs.ingest_delivery.semantic_sidecar import (
    PreparedProjectionPlan,
    SemanticSidecarOutcome,
    SemanticSidecarProgress,
    SemanticSidecarRequest,
    SemanticSidecarTooling,
    run_semantic_sidecar,
)
from adapters.jobs.reprocess_semantic_v2.acceptance import (
    ReprocessAcceptanceRequest,
    ReprocessAcceptanceResult,
    attest_reprocess,
)
from adapters.jobs.schema_generation.publisher_graph import load_publication_graph
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.dynamodb.semantic_state import (
    MAX_PUBLICATION_ITEM_BYTES,
    publication_record_bytes,
)
from adapters.outbound.media_taxonomy import load_media_taxonomy
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.s3.semantic_storage import canonical_object_bytes
from adapters.outbound.semantic_storage import (
    ReplicatedCanonicalStorage,
    ReplicatedSemanticStorage,
    ReplicatedSourceAlignedStorage,
)
from adapters.outbound.source_pack import load_source_packs
from adapters.outbound.table_dq import load_runtime_table_rules
from adapters.producer.packs import load_query_packs
from domain.curation.assertions import LoadOutcome
from domain.curation.ingestion import (
    AssessmentRequest,
    ContractAssessment,
    ContractLoad,
    assess_source_delivery,
)
from domain.curation.landing import (
    GOVERNED_MANIFEST_LAYOUT,
    LandedObject,
    LandingError,
    Manifest,
    partition_for,
)
from domain.curation.mapping import MappingRequest, map_rows
from domain.semantics.canonical_compiler import (
    CanonicalCompileRequest,
    CanonicalCompileResult,
    CanonicalRow,
    IdentityMappingEvidence,
    canonical_checksum_digest,
    canonical_rows_checksum,
    compile_canonical_rows,
)
from domain.semantics.curation import (
    CanonicalCurationPlan,
    CanonicalCurationRequest,
    plan_curation,
)
from domain.semantics.identity_evidence import (
    IdentityMappingsClaim,
    IdentityMappingsDocument,
    identity_mappings_document,
    parse_identity_claim,
    verify_identity_mappings,
)
from domain.semantics.publication import (
    AttemptEvent,
    AttemptEventKind,
    AttemptId,
    CanonicalDatasetKey,
    CanonicalPublisherKey,
    LeaseGrant,
    LeaseRequest,
    PartitionIdentity,
    PublicationId,
    PublicationScope,
    SourcePublicationRef,
    SuccessfulPublication,
    acquire_lease,
)
from domain.semantics.source_aligned import LandedDeliveryRef, SourceAlignedPublication
from domain.semantics.source_pack import IdentityField
from domain.semantics.validation import PrepublicationRuleFamily, RuleOutcome
from domain.shared_kernel.errors import InvariantError
from domain.shared_kernel.provenance import ProducerGovernance
from ports.outbound.canonical import (
    CanonicalDestination,
    CanonicalMaterializeRequest,
    CanonicalWriteResult,
)
from ports.outbound.identity_evidence import IdentityEvidenceWriteResult
from ports.outbound.semantic_state import (
    MAX_COMMIT_GROUP_MEMBERS,
    CommitPublicationGroupRequest,
    CommitPublicationRequest,
    InspectExactResult,
    LeaseAcquireOutcome,
    StateWriteOutcome,
)
from ports.outbound.source_aligned import MaterializeSourceRowsetResult

pytestmark = [pytest.mark.unit]
ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/generated"
TRAPS = ROOT / "tests/fixtures/traps"


def _set_attribute(value: object, name: str, replacement: object) -> None:
    setattr(value, name, replacement)


def _manifest(stem: str) -> Manifest:
    text = (FIXTURES / f"{stem}.csv").read_text()
    body = text.encode()
    return build_manifest(
        BuildManifestRequest(
            declaration=json.loads((FIXTURES / f"{stem}.manifest.json").read_text()),
            objects=(LandedObject("data-0001.csv", len(body), checksum_of(body)),),
            declared_columns=declared_columns_of(text, source_format="csv"),
        )
    )


class MemorySources:
    def __init__(self) -> None:
        self.items: dict[str, SourceAlignedPublication] = {}
        self.materializations = 0

    def materialize(self, publication: SourceAlignedPublication) -> MaterializeSourceRowsetResult:
        self.materializations += 1
        existing = self.items.get(publication.reference.value)
        if existing is not None and existing != publication:
            return MaterializeSourceRowsetResult(False, False, "conflicting source replay")
        self.items[publication.reference.value] = publication
        return MaterializeSourceRowsetResult(True, existing is None)

    def publication(self, reference: SourcePublicationRef) -> SourceAlignedPublication | None:
        return self.items.get(reference.value)

    def attest(self, publication: SourceAlignedPublication) -> MaterializeSourceRowsetResult:
        accepted = self.items.get(publication.reference.value) == publication
        return MaterializeSourceRowsetResult(
            accepted,
            False,
            "" if accepted else "source authority differs",
        )


class MemoryCanonical:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.requests: list[CanonicalMaterializeRequest] = []

    def rows(self, publication: SuccessfulPublication) -> tuple[CanonicalRow, ...]:
        matches = tuple(
            item.rows
            for item in self.requests
            if item.destination is CanonicalDestination.SUCCESSFUL
            and item.publication.publication_id == publication.publication_id
        )
        if len(matches) != 1:
            raise ValueError("memory canonical store lacks one exact successful publication.")
        return matches[0]

    def materialize(self, request: CanonicalMaterializeRequest) -> CanonicalWriteResult:
        self.order.append(request.destination.value)
        self.requests.append(request)
        return CanonicalWriteResult(
            True,
            True,
            len(request.rows),
            request.publication.content_checksum,
        )

    def attest(self, request: CanonicalMaterializeRequest) -> CanonicalWriteResult:
        accepted = request in self.requests
        return CanonicalWriteResult(
            accepted,
            False,
            len(request.rows),
            request.publication.content_checksum,
            "" if accepted else "canonical authority differs",
        )


class MemoryIdentityEvidence:
    """Write-once claim-check store; `read` verifies through the domain, like the S3 adapter."""

    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.items: dict[str, bytes] = {}

    def put(self, document: IdentityMappingsDocument) -> IdentityEvidenceWriteResult:
        self.order.append("identity_evidence")
        existing = self.items.get(document.claim.sha256)
        if existing is not None:
            accepted = existing == document.body
            return IdentityEvidenceWriteResult(accepted, False, "" if accepted else "differs")
        self.items[document.claim.sha256] = document.body
        return IdentityEvidenceWriteResult(True, True)

    def read(self, claim: IdentityMappingsClaim) -> tuple[dict[str, object], ...]:
        return verify_identity_mappings(claim, self.items.get(claim.sha256))


class MemoryAttempts:
    def __init__(self) -> None:
        self.events: list[AttemptEvent] = []
        self._slots: dict[tuple[AttemptId, str], AttemptEvent] = {}

    def append(self, event: AttemptEvent) -> StateWriteOutcome:
        key = (event.attempt_id, event.event_key)
        existing = self._slots.get(key)
        if existing is not None:
            return StateWriteOutcome(
                existing == event,
                "" if existing == event else "different evidence at immutable attempt slot",
            )
        if event.kind is not AttemptEventKind.STARTED:
            started = self._slots.get((event.attempt_id, "00000000"))
            if started is None:
                return StateWriteOutcome(False, "terminal event requires STARTED first")
            if (
                started.partition != event.partition
                or started.predecessor != event.predecessor
                or event.occurred_at < started.occurred_at
            ):
                return StateWriteOutcome(False, "terminal event contradicts STARTED")
        self._slots[key] = event
        self.events.append(event)
        return StateWriteOutcome(True)

    def events_for(self, attempt_id: AttemptId) -> tuple[AttemptEvent, ...]:
        return tuple(event for event in self.events if event.attempt_id == attempt_id)

    def commit_successes(self, events: tuple[AttemptEvent, ...]) -> StateWriteOutcome:
        """Atomically append exact successes after every STARTED slot is attested."""
        for event in events:
            started = self._slots.get((event.attempt_id, "00000000"))
            terminal = self._slots.get((event.attempt_id, event.event_key))
            expected_started = AttemptEvent(
                event.attempt_id,
                event.partition,
                AttemptEventKind.STARTED,
                event.occurred_at,
                0,
                predecessor=event.predecessor,
            )
            if started != expected_started or (terminal is not None and terminal != event):
                return StateWriteOutcome(False, "atomic success evidence differs")
        for event in events:
            key = (event.attempt_id, event.event_key)
            if key not in self._slots:
                self._slots[key] = event
                self.events.append(event)
        return StateWriteOutcome(True)


class MemoryLeases:
    def __init__(self) -> None:
        self.grants: list[LeaseGrant] = []
        self.releases = 0

    def acquire(self, request: LeaseRequest) -> LeaseAcquireOutcome:
        grant = acquire_lease(None, request)
        self.grants.append(grant)
        return LeaseAcquireOutcome(grant)

    def release(self, grant: LeaseGrant, *, at: datetime) -> StateWriteOutcome:  # noqa: ARG002
        self.releases += 1
        return StateWriteOutcome(True)


class MemoryPublications:
    def __init__(self, order: list[str], attempts: MemoryAttempts | None = None) -> None:
        self.order = order
        self.attempts = attempts or MemoryAttempts()
        self.current_items: dict[str, SuccessfulPublication] = {}

    def current(self, partition: PartitionIdentity) -> SuccessfulPublication | None:
        return self.current_items.get(partition.identity)

    def inspect_exact(
        self,
        partitions: tuple[PartitionIdentity, ...],
    ) -> InspectExactResult:
        return InspectExactResult(
            tuple(self.current_items.get(partition.identity) for partition in partitions)
        )

    def current_exact(
        self,
        partitions: tuple[PartitionIdentity, ...],
    ) -> tuple[SuccessfulPublication, ...] | None:
        inspected = self.inspect_exact(partitions)
        if any(publication is None for publication in inspected.publications):
            return None
        return tuple(
            publication for publication in inspected.publications if publication is not None
        )

    def current_for_scope(
        self,
        scope: PublicationScope,  # noqa: ARG002
        horizon: object = None,  # noqa: ARG002
    ) -> tuple[SuccessfulPublication, ...]:
        return tuple(self.current_items.values())

    def earliest_current_for_scope(self, scope: PublicationScope) -> SuccessfulPublication | None:
        return min(
            (item for item in self.current_items.values() if item.partition.scope == scope),
            key=lambda item: item.partition.bucket.window.start,
            default=None,
        )

    def validate_group(self, request: CommitPublicationGroupRequest) -> None:
        del request

    def commit(self, request: CommitPublicationRequest) -> StateWriteOutcome:
        self.order.append("commit")
        self.current_items[request.publication.partition.identity] = request.publication
        return StateWriteOutcome(True)

    def commit_group(self, request: CommitPublicationGroupRequest) -> StateWriteOutcome:
        successes = tuple(cast("AttemptEvent", member.success_event) for member in request.members)
        attempted = self.attempts.commit_successes(successes)
        if not attempted.accepted:
            return attempted
        self.order.append("commit_group")
        for member in request.members:
            self.current_items[member.publication.partition.identity] = member.publication
        return StateWriteOutcome(True)


def _case() -> tuple[SemanticSidecarRequest, SemanticSidecarTooling, list[str], MemoryAttempts]:
    contract_id = "amc-retail-purchases"
    contract = next(
        contract for _, contract in load_contracts(ROOT) if contract.contract_id == contract_id
    )
    manifest = _manifest(contract_id)
    document = read(
        DeliveredFile(
            text=(FIXTURES / f"{contract_id}.csv").read_text(),
            source_format=contract.source_format,
            manifest_text=(FIXTURES / f"{contract_id}.manifest.json").read_text(),
        )
    )
    document = replace(
        document,
        rows=tuple(
            row | {"marketplace_name": "AMAZON.COM"}
            for row in document.rows
            if row.get("rows_suppressed") == "false"
        )[:1],
    )
    assessment = ContractAssessment(
        ContractLoad(
            contract,
            partition_for(manifest, contract),
            map_rows(MappingRequest(contract, document)),
        ),
        LoadOutcome(),
    )
    rules = load_runtime_table_rules(ROOT / "contracts/generated/dq-rulesets.json")
    order: list[str] = []
    attempts = MemoryAttempts()
    sources = MemorySources()
    canonical = MemoryCanonical(order)
    tooling = SemanticSidecarTooling(
        source_writer=sources,
        source_reader=sources,
        canonical_writer=canonical,
        attempts=attempts,
        leases=MemoryLeases(),
        publications=MemoryPublications(order, attempts),
        packs=load_source_packs(ROOT),
        bridge=load_commerce_identity_bridge(ROOT),
        graph=load_publication_graph(
            ROOT,
            frozenset(CanonicalDatasetKey(name) for name in rules),
        ),
        table_rules=rules,
        columns=load_registry(ROOT),
        catalog=load_catalog(ROOT),
        identity_evidence=MemoryIdentityEvidence([]),
        taxonomy=load_media_taxonomy(ROOT),
        canonical_reader=canonical,
    )
    request = SemanticSidecarRequest(
        manifest=manifest,
        document=document,
        landed_delivery=LandedDeliveryRef(
            manifest.manifest_key,
            "manifest-v1",
            "sha256:manifest",
            manifest.object_keys()[0],
            "object-v1",
            manifest.objects[0].checksum,
        ),
        assessments=(assessment,),
        contracts=(contract,),
        now=datetime(2026, 8, 24, tzinfo=UTC),
        operation_id="sidecar-unit-001",
        execution_id="execution-unit-001",
    )
    return request, tooling, order, attempts


def _vendor_request(
    fixture: str,
    tooling: SemanticSidecarTooling,
) -> SemanticSidecarRequest:
    payload = json.loads((TRAPS / f"{fixture}.json").read_text())
    for index, row in enumerate(payload["salesByAsin"]):
        row["asin"] = f"B0TRAP{index:04d}"
    text = json.dumps(payload)
    manifest_text = (TRAPS / f"{fixture}.manifest.json").read_text()
    contracts = tuple(
        contract
        for _, contract in load_contracts(ROOT)
        if contract.contract_id in {"vendor-sales-by-asin", "vendor-sales-reported-total"}
    )
    source_format = contracts[0].source_format
    manifest = build_manifest(
        BuildManifestRequest(
            declaration=json.loads(manifest_text),
            objects=(
                LandedObject("data-0001.json", len(text.encode()), checksum_of(text.encode())),
            ),
            declared_columns=declared_columns_of(text, source_format=source_format.value),
        )
    )
    document = read(DeliveredFile(text, source_format, manifest_text))
    loads = tuple(
        ContractLoad(
            contract,
            partition_for(manifest, contract),
            map_rows(MappingRequest(contract, document)),
        )
        for contract in contracts
    )
    assessment = assess_source_delivery(
        AssessmentRequest(
            manifest=manifest,
            document=document,
            loads=loads,
            registry=tooling.columns,
        )
    )
    return SemanticSidecarRequest(
        manifest=manifest,
        document=document,
        landed_delivery=LandedDeliveryRef(
            manifest.manifest_key,
            f"manifest-{fixture}",
            f"sha256:manifest-{fixture}",
            manifest.object_keys()[0],
            f"object-{fixture}",
            manifest.objects[0].checksum,
        ),
        assessments=assessment.assessments,
        contracts=contracts,
        now=datetime(2026, 8, 24, tzinfo=UTC),
        operation_id=f"sidecar-{fixture}",
        execution_id=f"execution-{fixture}",
    )


def _vendor_case(
    fixture: str,
) -> tuple[SemanticSidecarRequest, SemanticSidecarTooling, list[str], MemoryAttempts]:
    _request, tooling, order, attempts = _case()
    return _vendor_request(fixture, tooling), tooling, order, attempts


def _trap_case(
    fixture: str, contract_id: str
) -> tuple[SemanticSidecarRequest, SemanticSidecarTooling, list[str]]:
    """One AMC trap delivery, read as landed, through the sidecar's own tooling."""
    _request, tooling, order, _attempts = _case()
    contract = next(
        contract for _, contract in load_contracts(ROOT) if contract.contract_id == contract_id
    )
    text = (TRAPS / f"{fixture}.csv").read_text()
    manifest_text = (TRAPS / f"{fixture}.manifest.json").read_text()
    manifest = build_manifest(
        BuildManifestRequest(
            declaration=json.loads(manifest_text),
            objects=(
                LandedObject("data-0001.csv", len(text.encode()), checksum_of(text.encode())),
            ),
            declared_columns=declared_columns_of(text, source_format="csv"),
        )
    )
    document = read(DeliveredFile(text, contract.source_format, manifest_text))
    load = ContractLoad(
        contract,
        partition_for(manifest, contract),
        map_rows(MappingRequest(contract, document)),
    )
    assessment = assess_source_delivery(
        AssessmentRequest(
            manifest=manifest,
            document=document,
            loads=(load,),
            registry=tooling.columns,
        )
    )
    request = SemanticSidecarRequest(
        manifest=manifest,
        document=document,
        landed_delivery=LandedDeliveryRef(
            manifest.manifest_key,
            f"manifest-{fixture}",
            f"sha256:manifest-{fixture}",
            manifest.object_keys()[0],
            f"object-{fixture}",
            manifest.objects[0].checksum,
        ),
        assessments=assessment.assessments,
        contracts=(contract,),
        now=datetime(2026, 8, 29, tzinfo=UTC),
        operation_id=f"sidecar-{fixture}",
        execution_id=f"execution-{fixture}",
    )
    return request, tooling, order


def _suppression_case(  # noqa: PLR0913
    *,
    multi_day: bool = False,
    all_withheld: bool = False,
    manifest_marketplace: str | None = None,
    withheld_currency_null: bool = False,
) -> tuple[
    SemanticSidecarRequest,
    SemanticSidecarTooling,
    list[str],
]:
    _request, tooling, order, _attempts = _case()
    contract = next(
        contract
        for _, contract in load_contracts(ROOT)
        if contract.contract_id == "amc-retail-purchases"
    )
    text = (FIXTURES / "amc-retail-purchases.csv").read_text()
    declaration = json.loads((FIXTURES / "amc-retail-purchases.manifest.json").read_text())
    # The case reads the fixture's second row, so its period is that row's day (and the next
    # day for the multi-day case), read from the regenerated fixture rather than pinned.
    second_day = date.fromisoformat(text.splitlines()[2].split(",", 1)[0])
    declaration["period_start"] = second_day.isoformat()
    declaration["period_end"] = (
        second_day + timedelta(days=1) if multi_day else second_day
    ).isoformat()
    if manifest_marketplace is not None:
        declaration["marketplace"] = manifest_marketplace
    manifest_text = json.dumps(declaration)
    manifest = build_manifest(
        BuildManifestRequest(
            declaration=declaration,
            objects=(
                LandedObject("data-0001.csv", len(text.encode()), checksum_of(text.encode())),
            ),
            declared_columns=declared_columns_of(text, source_format="csv"),
        )
    )
    parsed = read(DeliveredFile(text, contract.source_format, manifest_text))
    attributable = dict(parsed.rows[1]) | {
        "marketplace_name": "AMAZON.COM",
        "rows_suppressed": "false",
    }
    withheld = dict(attributable) | {
        "purchase_date_utc": None,
        "asin": None,
        "marketplace_name": None,
        "rows_suppressed": "true",
        "purchase_units_sold": "3",
        "unit_price": "2.0000",
        "revenue_purchased": "6.0000",
    }
    if withheld_currency_null:
        # The live multi-source shape: AMC returns the withheld row's currency NULL (AMC-V
        # decision 5), so its revenue has no unit.
        withheld["currency_code"] = None
    document = replace(
        parsed,
        rows=(withheld,) if all_withheld else (attributable, withheld),
    )
    load = ContractLoad(
        contract,
        partition_for(manifest, contract),
        map_rows(MappingRequest(contract, document)),
    )
    assessment = assess_source_delivery(
        AssessmentRequest(
            manifest=manifest,
            document=document,
            loads=(load,),
            registry=tooling.columns,
        )
    )
    request = SemanticSidecarRequest(
        manifest=manifest,
        document=document,
        landed_delivery=LandedDeliveryRef(
            manifest.manifest_key,
            "manifest-suppression-v1",
            "sha256:manifest-suppression",
            manifest.object_keys()[0],
            "object-suppression-v1",
            manifest.objects[0].checksum,
        ),
        assessments=assessment.assessments,
        contracts=(contract,),
        now=datetime(2026, 8, 24, tzinfo=UTC),
        operation_id="sidecar-suppression-exact-day",
        execution_id="execution-suppression-exact-day",
    )
    return request, tooling, order


@pytest.mark.covers("ADR-044", "ADR-047", "ADR-049", "ADR-053")
def test_adopted_source_authority_is_replayed_through_the_projection_writer() -> None:
    request, tooling, _, _ = _case()
    sources = tooling.source_writer
    assert isinstance(sources, MemorySources)

    first = run_semantic_sidecar(request, tooling)
    second = run_semantic_sidecar(
        replace(
            request,
            now=request.now + timedelta(seconds=1),
            operation_id="sidecar-unit-002",
        ),
        tooling,
    )

    assert first[0].outcome is SemanticSidecarOutcome.PUBLISHED
    assert second[0].outcome is SemanticSidecarOutcome.PUBLISHED
    publication = next(iter(sources.items.values()))
    assert publication.rowset.delivery_scope.source_scope == request.manifest.source_scope
    assert publication.rowset.delivery_scope.source_scope.value != request.manifest.marketplace
    assert sources.materializations == 3


def test_source_only_receipt_retains_exact_source_without_attempt_or_candidate() -> None:
    request, tooling, order, attempts = _case()
    contract = request.contracts[0]
    empty_document = replace(request.document, rows=())
    assessment = ContractAssessment(
        ContractLoad(
            contract,
            partition_for(request.manifest, contract),
            map_rows(MappingRequest(contract, empty_document)),
        ),
        LoadOutcome(),
    )

    (result,) = run_semantic_sidecar(
        replace(request, document=empty_document, assessments=(assessment,)),
        tooling,
    )

    sources = tooling.source_reader
    assert isinstance(sources, MemorySources)
    assert result.outcome is SemanticSidecarOutcome.SOURCE_ONLY
    assert result.publications == ()
    assert result.receipts[0].source_publications == tuple(sources.items.values())
    assert result.receipts[0].candidate is None
    assert result.receipts[0].successful is None
    assert result.receipts[0].attempt_id is None
    assert result.receipts[0].predecessor_attempt_id is None
    assert result.receipts[0].terminal_event_kind is None
    assert order == []
    assert attempts.events == []
    canonical = tooling.canonical_writer
    publications = tooling.publications
    assert isinstance(canonical, MemoryCanonical)
    assert isinstance(publications, MemoryPublications)
    acceptance = attest_reprocess(
        ReprocessAcceptanceRequest(
            outcomes=(result,),
            storage=ReplicatedSemanticStorage(
                source=cast("ReplicatedSourceAlignedStorage", sources),
                canonical=cast("ReplicatedCanonicalStorage", canonical),
                identity_evidence=tooling.identity_evidence,
            ),
            attempts=attempts,
            publications=publications,
        )
    )
    assert acceptance.accepted
    assert acceptance.counts.sources == 1
    assert acceptance.counts.candidates == 0
    assert acceptance.counts.successes == 0
    assert acceptance.counts.publications == 0
    assert acceptance.counts.attempts == 0


@pytest.mark.covers("ADR-044", "ADR-047", "ADR-049")
def test_success_bytes_exist_before_fenced_pointer_commit() -> None:
    request, tooling, order, attempts = _case()
    results = run_semantic_sidecar(request, tooling)

    assert results[0].outcome is SemanticSidecarOutcome.PUBLISHED
    assert results[0].publications and isinstance(results[0].publications[0], PublicationId)
    receipt = results[0].receipts[0]
    sources = tooling.source_reader
    canonical = tooling.canonical_writer
    assert isinstance(sources, MemorySources)
    assert isinstance(canonical, MemoryCanonical)
    assert receipt.landed_delivery == request.landed_delivery
    assert receipt.source_publications == tuple(sources.items.values())
    assert receipt.candidate is canonical.requests[0]
    assert receipt.successful is canonical.requests[1]
    assert receipt.attempt_id == attempts.events[0].attempt_id
    assert receipt.predecessor_attempt_id is None
    assert receipt.terminal_event_kind is AttemptEventKind.SUCCEEDED
    assert len(order) % 3 == 0
    assert all(
        order[index : index + 3] == ["candidate", "successful", "commit_group"]
        for index in range(0, len(order), 3)
    )
    assert [event.kind.value for event in attempts.events] == [
        value for _ in range(len(order) // 3) for value in ("started", "succeeded")
    ]
    publications = tooling.publications
    assert isinstance(publications, MemoryPublications)
    storage = ReplicatedSemanticStorage(
        source=cast("ReplicatedSourceAlignedStorage", sources),
        canonical=cast("ReplicatedCanonicalStorage", canonical),
        identity_evidence=tooling.identity_evidence,
    )
    acceptance = attest_reprocess(
        ReprocessAcceptanceRequest(
            outcomes=results,
            storage=storage,
            attempts=attempts,
            publications=publications,
        )
    )
    assert acceptance.accepted
    assert acceptance.counts.sources == 1
    assert acceptance.counts.candidates == 1
    assert acceptance.counts.successes == 1
    assert acceptance.counts.publications == 1
    assert acceptance.counts.attempts == 1


def test_reprocess_acceptance_refuses_substituted_success_under_candidate_id() -> None:
    request, tooling, _order, attempts = _case()
    (result,) = run_semantic_sidecar(request, tooling)
    receipt = result.receipts[0]
    successful = receipt.successful
    sources = tooling.source_reader
    canonical = tooling.canonical_writer
    publications = tooling.publications
    assert successful is not None
    assert isinstance(successful.publication, SuccessfulPublication)
    assert isinstance(sources, MemorySources)
    assert isinstance(canonical, MemoryCanonical)
    assert isinstance(publications, MemoryPublications)
    subject = successful.publication.evidence.subject
    assert subject is not None
    substituted_publication = replace(
        successful.publication,
        publisher=CanonicalPublisherKey("substituted-publisher"),
        evidence=replace(
            successful.publication.evidence,
            subject=replace(subject, publisher="substituted-publisher"),
        ),
    )
    substituted_success = CanonicalMaterializeRequest(
        CanonicalDestination.SUCCESSFUL,
        substituted_publication,
        successful.rows,
        successful.rule_set_version,
    )
    canonical.requests[1] = substituted_success
    publications.current_items[substituted_publication.partition.identity] = substituted_publication
    substituted_result = replace(
        result,
        receipts=(replace(receipt, successful=substituted_success),),
    )

    acceptance = attest_reprocess(
        ReprocessAcceptanceRequest(
            outcomes=(substituted_result,),
            storage=ReplicatedSemanticStorage(
                source=cast("ReplicatedSourceAlignedStorage", sources),
                canonical=cast("ReplicatedCanonicalStorage", canonical),
                identity_evidence=tooling.identity_evidence,
            ),
            attempts=attempts,
            publications=publications,
        )
    )

    assert not acceptance.accepted
    assert "malformed lifecycle" in acceptance.reason


@pytest.mark.covers("ADR-031", "ADR-044", "ADR-047", "ADR-049")
def test_matching_vendor_control_pair_advances_both_pointers_in_one_group() -> None:
    request, tooling, order, _attempts = _vendor_case("vendor-control-total-consistent")
    results = run_semantic_sidecar(request, tooling)

    assert {result.outcome for result in results} == {SemanticSidecarOutcome.PUBLISHED}
    assert order == [
        "candidate",
        "candidate",
        "successful",
        "successful",
        "commit_group",
    ]
    canonical = tooling.canonical_writer
    publications = tooling.publications
    assert isinstance(canonical, MemoryCanonical)
    assert isinstance(publications, MemoryPublications)
    candidates = tuple(
        item for item in canonical.requests if item.destination is CanonicalDestination.CANDIDATE
    )
    assert len(candidates) == 2
    assert len(publications.current_items) == 2
    for candidate in candidates:
        assert len(candidate.publication.source_inputs) == 2
        controls = tuple(
            result
            for result in candidate.publication.evidence.results
            if result.family is PrepublicationRuleFamily.CONTROL_TOTALS
        )
        assert controls
        assert all(result.outcome is RuleOutcome.PASSED for result in controls)


@pytest.mark.covers("ADR-047")
def test_redelivered_older_revision_is_skipped_and_never_reverts_current() -> None:
    """A stale redelivery must never move CURRENT backwards.

    The live 2026-09-16 reproduction: a raw-zone retry redelivered the pre-restatement
    document after its restatement had published, and CURRENT moved backwards to the
    superseded figure. Fencing orders competing writers, not the source's own timeline.
    """
    later, tooling, _order, attempts = _vendor_case("vendor-restatement-later")
    first = run_semantic_sidecar(later, tooling)
    assert {result.outcome for result in first} == {SemanticSidecarOutcome.PUBLISHED}
    publications = tooling.publications
    assert isinstance(publications, MemoryPublications)
    current_before = dict(publications.current_items)

    stale = run_semantic_sidecar(_vendor_request("vendor-restatement-earlier", tooling), tooling)

    # Every bucket older than CURRENT: superseded, a product signal, never blocked.
    assert {result.outcome for result in stale} == {SemanticSidecarOutcome.SUPERSEDED}
    assert all(result.publications == () for result in stale)
    assert publications.current_items == current_before
    skipped_events = [event for event in attempts.events if event.kind is AttemptEventKind.SKIPPED]
    assert skipped_events
    assert all(
        "older than the current publication" in (event.reason or "") for event in skipped_events
    )
    assert all(
        receipt.terminal_event_kind is AttemptEventKind.SKIPPED
        for result in stale
        for receipt in result.receipts
    )
    sources = tooling.source_reader
    canonical = tooling.canonical_writer
    assert isinstance(sources, MemorySources)
    assert isinstance(canonical, MemoryCanonical)
    acceptance = attest_reprocess(
        ReprocessAcceptanceRequest(
            outcomes=stale,
            storage=ReplicatedSemanticStorage(
                source=cast("ReplicatedSourceAlignedStorage", sources),
                canonical=cast("ReplicatedCanonicalStorage", canonical),
                identity_evidence=tooling.identity_evidence,
            ),
            attempts=attempts,
            publications=publications,
        )
    )
    assert acceptance.accepted
    assert acceptance.counts.attempts == sum(len(result.receipts) for result in stale)
    assert acceptance.counts.publications == 0

    incomplete = replace(
        stale[0],
        expected_partition_count=stale[0].expected_partition_count + 1,
    )
    refused = attest_reprocess(
        ReprocessAcceptanceRequest(
            outcomes=(incomplete, *stale[1:]),
            storage=ReplicatedSemanticStorage(
                source=cast("ReplicatedSourceAlignedStorage", sources),
                canonical=cast("ReplicatedCanonicalStorage", canonical),
                identity_evidence=tooling.identity_evidence,
            ),
            attempts=attempts,
            publications=publications,
        )
    )
    assert not refused.accepted
    assert "malformed lifecycle" in refused.reason


@pytest.mark.parametrize(
    ("buckets", "expected"),
    [
        # (published, skipped, blocked). Every bucket older than CURRENT: superseded, not blocked.
        ((0, 3, 0), SemanticSidecarOutcome.SUPERSEDED),
        # Paired control: one bucket that failed evidence beside the skips still blocks.
        ((0, 2, 1), SemanticSidecarOutcome.BLOCKED),
        # Mixed: classified by what applied, so neither superseded nor blocked.
        ((2, 1, 0), SemanticSidecarOutcome.PUBLISHED),
        # Paired control: a mix with a failed bucket blocks.
        ((2, 1, 1), SemanticSidecarOutcome.BLOCKED),
        ((3, 0, 0), SemanticSidecarOutcome.PUBLISHED),
        ((0, 0, 2), SemanticSidecarOutcome.BLOCKED),
        # No candidate bucket at all is not a supersession.
        ((0, 0, 0), SemanticSidecarOutcome.BLOCKED),
    ],
)
def test_bucket_outcome_classifies_by_what_applied(
    buckets: tuple[int, int, int], expected: SemanticSidecarOutcome
) -> None:
    published, skipped, blocked = buckets
    outcome = semantic_sidecar_module._bucket_outcome(  # pyright: ignore[reportPrivateUsage]
        published=published, skipped=skipped, blocked=blocked
    )
    assert outcome is expected


@pytest.mark.covers("ADR-047")
def test_a_delivery_that_publishes_some_buckets_and_skips_the_rest_is_published() -> None:
    """Mixed skipped and applied buckets: classified by what applied, never blocked."""
    request, tooling, _order = _suppression_case()
    first = run_semantic_sidecar(request, tooling)
    assert first[0].outcome is SemanticSidecarOutcome.PUBLISHED
    publications = tooling.publications
    attempts = tooling.attempts
    assert isinstance(publications, MemoryPublications)
    assert isinstance(attempts, MemoryAttempts)
    # A newer revision is CURRENT for exactly one of the two buckets: the stale-split reads only
    # the incumbent's publication id and source revision.
    newer_key = sorted(publications.current_items)[0]
    incumbent = publications.current_items[newer_key]
    newer = MagicMock(
        publication_id=PublicationId("publication-newer-incumbent"),
        source_revision=replace(
            incumbent.source_revision,
            observed_at=incumbent.source_revision.observed_at + timedelta(days=1),
        ),
    )
    current = publications.current

    def newer_for_one(partition: PartitionIdentity) -> SuccessfulPublication | None:
        return (
            cast("SuccessfulPublication", newer)
            if partition.identity == newer_key
            else (current(partition))
        )

    publications.current = newer_for_one  # type: ignore[method-assign]

    mixed = run_semantic_sidecar(
        replace(
            request,
            now=datetime(2026, 8, 24, 1, tzinfo=UTC),
            operation_id="sidecar-suppression-mixed",
            execution_id="execution-suppression-mixed",
        ),
        tooling,
    )

    assert [result.outcome for result in mixed] == [SemanticSidecarOutcome.PUBLISHED]
    kinds = sorted(str(receipt.terminal_event_kind) for receipt in mixed[0].receipts)
    assert kinds == sorted([str(AttemptEventKind.SKIPPED), str(AttemptEventKind.SUCCEEDED)])
    assert len(mixed[0].publications) == 1
    # The skipped bucket's CURRENT is untouched: no change to which data is written.
    assert publications.current_items[newer_key] == incumbent
    sources = tooling.source_reader
    canonical = tooling.canonical_writer
    assert isinstance(sources, MemorySources)
    assert isinstance(canonical, MemoryCanonical)
    # The reprocess acceptance accepts the mixed lifecycle it accepted while it was labelled
    # blocked (paired with the quarantined control-total case, which it refuses as blocked).
    acceptance = attest_reprocess(
        ReprocessAcceptanceRequest(
            outcomes=mixed,
            storage=ReplicatedSemanticStorage(
                source=cast("ReplicatedSourceAlignedStorage", sources),
                canonical=cast("ReplicatedCanonicalStorage", canonical),
                identity_evidence=tooling.identity_evidence,
            ),
            attempts=attempts,
            publications=publications,
        )
    )
    assert acceptance.accepted, acceptance.reason


@pytest.mark.covers("ADR-047")
def test_equal_revision_republication_still_supersedes_for_reprocessing() -> None:
    """An equal source revision supersedes rather than skips.

    Batch reprocessing republishes the same source state under changed rules; skipping on
    equality would make every reprocess a silent no-op.
    """
    later, tooling, _order, _attempts = _vendor_case("vendor-restatement-later")
    first = run_semantic_sidecar(later, tooling)
    assert {result.outcome for result in first} == {SemanticSidecarOutcome.PUBLISHED}
    publications = tooling.publications
    assert isinstance(publications, MemoryPublications)
    before = {key: item.publication_id for key, item in publications.current_items.items()}

    replay = replace(
        _vendor_request("vendor-restatement-later", tooling),
        now=datetime(2026, 8, 24, 1, tzinfo=UTC),
        operation_id="sidecar-replay",
        execution_id="execution-replay",
    )
    results = run_semantic_sidecar(replay, tooling)

    assert {result.outcome for result in results} == {SemanticSidecarOutcome.PUBLISHED}
    after = {key: item.publication_id for key, item in publications.current_items.items()}
    assert set(after) == set(before)
    assert all(after[key] != before[key] for key in after)


@pytest.mark.covers("ADR-031", "ADR-044", "ADR-049")
def test_mismatching_vendor_control_pair_retains_both_candidates_and_moves_no_pointer() -> None:
    request, tooling, order, attempts = _vendor_case("vendor-control-total-short")
    results = run_semantic_sidecar(request, tooling)

    assert {result.outcome for result in results} == {SemanticSidecarOutcome.BLOCKED}
    assert order == ["candidate", "candidate"]
    publications = tooling.publications
    canonical = tooling.canonical_writer
    assert isinstance(publications, MemoryPublications)
    assert isinstance(canonical, MemoryCanonical)
    assert len(publications.current_items) == 0
    receipts = tuple(receipt for result in results for receipt in result.receipts)
    assert {id(receipt.candidate) for receipt in receipts} == {
        id(candidate) for candidate in canonical.requests
    }
    assert all(receipt.successful is None for receipt in receipts)
    assert all(receipt.attempt_id is not None for receipt in receipts)
    assert all(receipt.terminal_event_kind is AttemptEventKind.QUARANTINED for receipt in receipts)
    assert {receipt.attempt_id for receipt in receipts} == {
        event.attempt_id for event in attempts.events if event.kind is AttemptEventKind.QUARANTINED
    }
    sources = tooling.source_reader
    assert isinstance(sources, MemorySources)
    acceptance = attest_reprocess(
        ReprocessAcceptanceRequest(
            outcomes=results,
            storage=ReplicatedSemanticStorage(
                source=cast("ReplicatedSourceAlignedStorage", sources),
                canonical=cast("ReplicatedCanonicalStorage", canonical),
                identity_evidence=tooling.identity_evidence,
            ),
            attempts=attempts,
            publications=publications,
        )
    )
    assert not acceptance.accepted
    assert "blocked" in acceptance.reason
    for candidate in canonical.requests:
        controls = tuple(
            result
            for result in candidate.publication.evidence.results
            if result.family is PrepublicationRuleFamily.CONTROL_TOTALS
        )
        assert controls
        assert any(result.outcome is RuleOutcome.FAIL for result in controls)


@pytest.mark.covers("ADR-034", "ADR-044", "ADR-049")
def test_single_day_suppression_publishes_attributable_and_residual_atomically() -> None:
    request, tooling, order = _suppression_case()
    results = run_semantic_sidecar(request, tooling)

    assert results[0].outcome is SemanticSidecarOutcome.PUBLISHED
    assert len(results[0].publications) == 2
    assert order == [
        "candidate",
        "candidate",
        "successful",
        "successful",
        "commit_group",
    ]
    canonical = tooling.canonical_writer
    assert isinstance(canonical, MemoryCanonical)
    candidates = tuple(
        item for item in canonical.requests if item.destination is CanonicalDestination.CANDIDATE
    )
    datasets = {item.publication.partition.scope.dataset.value for item in candidates}
    assert datasets == {"fct_retail_purchase_amc", "fct_retail_purchase_amc_withheld"}
    residual = next(
        item
        for item in candidates
        if item.publication.partition.scope.dataset.value.endswith("_withheld")
    )
    residual_row = dict(residual.rows[0])
    assert residual_row["units_purchased"] == Decimal("3")
    assert residual_row["revenue_purchased"] == Decimal("6.0000")
    for candidate in candidates:
        controls = tuple(
            result
            for result in candidate.publication.evidence.results
            if result.family is PrepublicationRuleFamily.CONTROL_TOTALS
        )
        assert controls
        assert all(result.outcome is RuleOutcome.PASSED for result in controls)


def test_all_withheld_bucket_publishes_empty_base_and_residual_atomically() -> None:
    request, tooling, order = _suppression_case(all_withheld=True)
    results = run_semantic_sidecar(request, tooling)

    assert results[0].outcome is SemanticSidecarOutcome.PUBLISHED
    assert len(results[0].publications) == 2
    assert order == [
        "candidate",
        "candidate",
        "successful",
        "successful",
        "commit_group",
    ]
    canonical = tooling.canonical_writer
    publications = tooling.publications
    attempts = tooling.attempts
    sources = tooling.source_reader
    assert isinstance(canonical, MemoryCanonical)
    assert isinstance(publications, MemoryPublications)
    assert isinstance(attempts, MemoryAttempts)
    assert isinstance(sources, MemorySources)
    candidates = tuple(
        item for item in canonical.requests if item.destination is CanonicalDestination.CANDIDATE
    )
    base = next(
        item
        for item in candidates
        if not item.publication.partition.scope.dataset.value.endswith("_withheld")
    )
    residual = next(
        item
        for item in candidates
        if item.publication.partition.scope.dataset.value.endswith("_withheld")
    )
    assert base.rows == ()
    assert base.publication.content_checksum == canonical_rows_checksum(())
    assert base.publication.evidence.withheld_only
    assert dict(residual.rows[0])["revenue_purchased"] == Decimal("6.0000")
    assert len(publications.current_items) == 2
    acceptance = attest_reprocess(
        ReprocessAcceptanceRequest(
            outcomes=results,
            storage=ReplicatedSemanticStorage(
                source=cast("ReplicatedSourceAlignedStorage", sources),
                canonical=cast("ReplicatedCanonicalStorage", canonical),
                identity_evidence=tooling.identity_evidence,
            ),
            attempts=attempts,
            publications=publications,
        )
    )
    assert acceptance.accepted
    assert acceptance.counts.candidates == 2
    assert acceptance.counts.successes == 2
    assert acceptance.counts.publications == 2
    assert acceptance.counts.attempts == 2


def _candidates(
    tooling: SemanticSidecarTooling,
) -> tuple[tuple[CanonicalRow, ...], tuple[CanonicalRow, ...]]:
    """Return the base and residual candidate rows one suppression delivery wrote."""
    canonical = tooling.canonical_writer
    assert isinstance(canonical, MemoryCanonical)
    candidates = tuple(
        item for item in canonical.requests if item.destination is CanonicalDestination.CANDIDATE
    )
    base = next(
        item.rows
        for item in candidates
        if not item.publication.partition.scope.dataset.value.endswith("_withheld")
    )
    residual = next(
        item.rows
        for item in candidates
        if item.publication.partition.scope.dataset.value.endswith("_withheld")
    )
    return base, residual


def _control_outcomes(tooling: SemanticSidecarTooling) -> set[tuple[str, RuleOutcome]]:
    canonical = tooling.canonical_writer
    assert isinstance(canonical, MemoryCanonical)
    return {
        (result.rule_id, result.outcome)
        for item in canonical.requests
        if item.destination is CanonicalDestination.CANDIDATE
        for result in item.publication.evidence.results
        if result.family is PrepublicationRuleFamily.CONTROL_TOTALS
    }


class TestAWithheldAmountWithoutItsCurrency:
    """Review A1-1: a currency-less withheld amount stays out of both sides of the control.

    AMC returns a multi-source withheld row's purchase-side sums and currency NULL (AMC-V
    decision 5). Its counts still publish in the residual; its amount is NULL there, which reads
    "withheld". The paired control is `test_single_day_suppression_publishes_attributable_and_
    residual_atomically`, whose withheld row keeps its currency and whose revenue is summed.
    """

    def test_the_live_shape_withheld_residual_trap_publishes(self) -> None:
        request, tooling, _order = _trap_case("amc-withheld-residual", "amc-dsp-impressions")

        results = run_semantic_sidecar(request, tooling)

        assert results[0].outcome is SemanticSidecarOutcome.PUBLISHED, results[0].detail
        assert len(results[0].publications) == 2
        _base, residual = _candidates(tooling)
        row = dict(residual[0])
        assert row["impressions"] == Decimal("457")
        assert row.get("spend") is None
        assert row.get("currency_code") is None
        assert _control_outcomes(tooling) == {
            ("control-total:impressions", RuleOutcome.PASSED),
            ("control-total:spend", RuleOutcome.PASSED),
        }

    def test_a_mixed_delivery_publishes_its_withheld_units_and_leaves_revenue_null(self) -> None:
        request, tooling, _order = _suppression_case(withheld_currency_null=True)

        results = run_semantic_sidecar(request, tooling)

        assert results[0].outcome is SemanticSidecarOutcome.PUBLISHED, results[0].detail
        _base, residual = _candidates(tooling)
        row = dict(residual[0])
        assert row["units_purchased"] == Decimal("3")
        assert row.get("revenue_purchased") is None
        assert {outcome for _rule_id, outcome in _control_outcomes(tooling)} == {RuleOutcome.PASSED}

    def test_an_all_withheld_delivery_publishes_its_withheld_only_base_and_residual(
        self,
    ) -> None:
        request, tooling, _order = _suppression_case(all_withheld=True, withheld_currency_null=True)

        results = run_semantic_sidecar(request, tooling)

        assert results[0].outcome is SemanticSidecarOutcome.PUBLISHED, results[0].detail
        base, residual = _candidates(tooling)
        assert base == ()
        row = dict(residual[0])
        assert row["units_purchased"] == Decimal("3")
        assert row.get("revenue_purchased") is None
        assert {outcome for _rule_id, outcome in _control_outcomes(tooling)} == {RuleOutcome.PASSED}


def test_a_bare_asi_segment_marketplace_manifest_value_never_scopes_a_suppressed_bucket() -> None:
    """Review B-1: the suppressed-partition fallback ignores query-parameter namespaces.

    `'1'` is the `amc.segment_marketplace_id` alias for the US. As a manifest marketplace it
    names no governed marketplace, so the sidecar blocks an all-withheld bucket (the
    `IdentityResolutionError` from `_partition`, reported as the result detail) rather than
    resolving it to `amazon_us`. The paired control is the same bucket under `ATVPDKIKX0DER`.
    """
    request, tooling, _order = _suppression_case(all_withheld=True, manifest_marketplace="1")

    refused = run_semantic_sidecar(request, tooling)

    assert refused[0].outcome is SemanticSidecarOutcome.BLOCKED
    assert refused[0].detail == (
        "suppressed source partition has no single governed manifest marketplace."
    )
    assert refused[0].publications == ()
    control_request, control_tooling, _ = _suppression_case(all_withheld=True)
    control = run_semantic_sidecar(control_request, control_tooling)
    assert control[0].outcome is SemanticSidecarOutcome.PUBLISHED
    canonical = control_tooling.canonical_writer
    assert isinstance(canonical, MemoryCanonical)
    assert {item.publication.partition.scope.population_key for item in canonical.requests} == {
        "amazon_us"
    }


@pytest.mark.covers("ADR-034", "ADR-046", "ADR-049")
def test_multi_day_unassignable_residual_blocks_exact_bucket_and_moves_no_pointer() -> None:
    request, tooling, order = _suppression_case(multi_day=True)
    results = run_semantic_sidecar(request, tooling)

    assert results[0].outcome is SemanticSidecarOutcome.BLOCKED
    assert results[0].publications == ()
    assert order == ["candidate"]
    publications = tooling.publications
    canonical = tooling.canonical_writer
    assert isinstance(publications, MemoryPublications)
    assert isinstance(canonical, MemoryCanonical)
    assert publications.current_items == {}
    candidate = canonical.requests[0]
    suppression = next(
        result
        for result in candidate.publication.evidence.results
        if result.family is PrepublicationRuleFamily.SUPPRESSION_COVERAGE
    )
    assert suppression.outcome is RuleOutcome.FAIL
    assert "exactly" in suppression.detail


@pytest.mark.covers("ADR-044", "ADR-047", "ADR-049")
def test_uncontrolled_fact_uses_explicit_not_applicable_control_evidence() -> None:
    request, tooling, order, attempts = _case()
    pack = next(pack for pack in tooling.packs if pack.contract_id == "amc-attributed-conversion")
    tooling = replace(tooling, packs=(pack,))
    contract = next(
        contract
        for _, contract in load_contracts(ROOT)
        if contract.contract_id == "amc-attributed-conversion"
    )
    manifest = _manifest("amc-attributed-conversion")
    document = read(
        DeliveredFile(
            (FIXTURES / "amc-attributed-conversion.csv").read_text(),
            contract.source_format,
            (FIXTURES / "amc-attributed-conversion.manifest.json").read_text(),
        )
    )
    document = replace(
        document,
        rows=tuple(
            row | {"conversion_event_marketplace_name": "AMAZON.COM"} for row in document.rows
        ),
    )
    assessment = ContractAssessment(
        ContractLoad(
            contract,
            partition_for(manifest, contract),
            map_rows(MappingRequest(contract, document)),
        ),
        LoadOutcome(),
    )
    changed = SemanticSidecarRequest(
        manifest,
        document,
        LandedDeliveryRef(
            manifest.manifest_key,
            "manifest-v1",
            "sha256:manifest",
            manifest.object_keys()[0],
            "object-v1",
            manifest.objects[0].checksum,
        ),
        (assessment,),
        (contract,),
        request.now,
        "sidecar-unit-002",
        "execution-unit-002",
    )

    results = run_semantic_sidecar(changed, tooling)
    assert results[0].outcome is SemanticSidecarOutcome.PUBLISHED
    assert results[0].publications
    assert order and set(order) == {"candidate", "successful", "commit_group"}
    canonical = tooling.canonical_writer
    assert isinstance(canonical, MemoryCanonical)
    candidate = next(
        request
        for request in canonical.requests
        if request.destination is CanonicalDestination.CANDIDATE
    )
    control = next(
        result
        for result in candidate.publication.evidence.results
        if result.family is PrepublicationRuleFamily.CONTROL_TOTALS
    )
    assert control.outcome is RuleOutcome.PASSED
    assert "No external control relationship" in control.detail
    assert {event.kind.value for event in attempts.events} == {
        "started",
        "succeeded",
    }


@pytest.mark.covers("ADR-044", "ADR-045", "ADR-046")
def test_canonical_candidates_are_bucket_scoped_and_identity_conformed() -> None:
    request, tooling, _order, _attempts = _case()
    results = run_semantic_sidecar(request, tooling)
    assert results[0].outcome is SemanticSidecarOutcome.PUBLISHED
    canonical = tooling.canonical_writer
    assert isinstance(canonical, MemoryCanonical)
    candidates = [item for item in canonical.requests if item.destination.value == "candidate"]
    assert candidates
    for candidate in candidates:
        assert len(candidate.rows) == 1
        row = dict(candidate.rows[0])
        assert row["marketplace_key"] == "amazon_us"
        assert str(row["amazon_asin"]).startswith("amazon_asin:b0fixtur")
        assert isinstance(row["date_key"], date)
        assert isinstance(row["units_purchased"], int)
        assert isinstance(row["rows_suppressed"], bool)
        assert isinstance(row["revenue_purchased"], Decimal)
        identity_result = next(
            result
            for result in candidate.publication.evidence.results
            if result.family.value == "identity"
        )
        mappings = tooling.identity_evidence.read(parse_identity_claim(identity_result.detail))
        assert 2 in {item["mapping_version"] for item in mappings}
        assert candidate.publication.content_checksum.startswith("sha256:")


def _compiler_request() -> CanonicalCompileRequest:
    request, tooling, _order, _attempts = _case()
    run_semantic_sidecar(request, tooling)
    sources = tooling.source_writer
    canonical = tooling.canonical_writer
    assert isinstance(sources, MemorySources)
    assert isinstance(canonical, MemoryCanonical)
    publication = next(iter(sources.items.values()))
    candidate = next(
        item for item in canonical.requests if item.destination.value == "candidate"
    ).publication
    pack = next(
        pack for pack in tooling.packs if pack.contract_id == request.contracts[0].contract_id
    )
    plan = plan_curation(
        CanonicalCurationRequest(
            partition=candidate.partition,
            publisher=candidate.publisher,
            source_references=(publication.reference,),
            available_sources=(publication,),
            graph=tooling.graph,
        )
    )
    return CanonicalCompileRequest(
        plan,
        pack,
        request.contracts[0],
        request.assessments[0].load.rows,
        tooling.bridge,
        taxonomy=tooling.taxonomy,
    )


@pytest.mark.covers("ADR-044", "ADR-045", "ADR-046")
def test_canonical_compiler_result_is_deterministic_frozen_and_slotted() -> None:
    compiled = compile_canonical_rows(_compiler_request())
    assert compiled.row_count == 1
    assert compiled.content_checksum == (
        "sha256:b9ec9cc424d58f8fabadf6420060d6fa5af09b1f9190995a81b1ad888169c03c"
    )
    assert dict(compiled.rows[0])["marketplace_key"] == "amazon_us"
    assert {
        (item.source_namespace, item.mapping_version) for item in compiled.identity_evidence
    } == {
        ("adlytics.media_profile", 1),
        ("amc.marketplace_name", 2),
        ("amc.retail_purchase_asin", 1),
    }
    with pytest.raises(FrozenInstanceError):
        _set_attribute(compiled, "content_checksum", "changed")
    assert not hasattr(compiled, "__dict__")


@pytest.mark.covers("ADR-044", "ADR-045")
def test_canonical_compiler_refuses_pack_contract_and_dataset_substitution() -> None:
    request = _compiler_request()
    for contract_id in ("aaa-contract", "zzz-contract"):
        with pytest.raises(ValueError, match="pack and contract identities"):
            replace(request, pack=replace(request.pack, contract_id=contract_id))
    for dataset in ("aaa_fact", "zzz_fact"):
        with pytest.raises(ValueError, match="another canonical dataset"):
            replace(request, pack=replace(request.pack, dataset=CanonicalDatasetKey(dataset)))


@pytest.mark.covers("ADR-044")
def test_canonical_compiler_requires_one_source_and_matching_full_row_count() -> None:
    request = _compiler_request()
    source = request.plan.source_publications[0]
    with pytest.raises(ValueError, match="exactly one source"):
        replace(request, plan=replace(request.plan, source_publications=(source, source)))
    with pytest.raises(ValueError, match="row counts differ"):
        replace(request, mapped_rows=())
    with pytest.raises(ValueError, match="row counts differ"):
        replace(request, mapped_rows=(*request.mapped_rows, *request.mapped_rows))


@pytest.mark.covers("ADR-044")
def test_canonical_compiler_refuses_unsupported_values_and_naive_datetimes() -> None:
    request = _compiler_request()
    unsupported_value: list[object] = []
    unsupported: dict[str, object] = dict(request.mapped_rows[0])
    unsupported["unsupported"] = unsupported_value
    with pytest.raises(ValueError, match="unsupported value type"):
        compile_canonical_rows(
            replace(request, mapped_rows=(unsupported, *request.mapped_rows[1:]))
        )
    naive = dict(request.mapped_rows[0]) | {
        "naive_time": datetime(2026, 8, 25)  # noqa: DTZ001 — deliberate invalid input
    }
    with pytest.raises(ValueError, match="timezone-aware"):
        compile_canonical_rows(replace(request, mapped_rows=(naive, *request.mapped_rows[1:])))


@pytest.mark.covers("ADR-044")
def test_canonical_compile_request_is_frozen_and_slotted() -> None:
    request = _compiler_request()
    with pytest.raises(FrozenInstanceError):
        _set_attribute(request, "mapped_rows", ())
    assert not hasattr(request, "__dict__")
    assert isinstance(CanonicalCompileResult((), "sha256:empty", ()).row_count, int)


@pytest.mark.covers("ADR-044", "ADR-045")
def test_canonical_compiler_requires_exact_mapping_and_source_identifier() -> None:
    request = _compiler_request()
    missing_path = replace(
        request.pack,
        marketplace=IdentityField(request.pack.marketplace.namespace, "missing.source.path"),
    )
    with pytest.raises(ValueError, match="does not map to exactly one target"):
        compile_canonical_rows(replace(request, pack=missing_path))
    missing_identifier = replace(
        request.pack,
        marketplace=IdentityField("missing.namespace", request.pack.marketplace.source_path),
    )
    with pytest.raises(ValueError, match=r"exactly one 'missing.namespace' identifier"):
        compile_canonical_rows(replace(request, pack=missing_identifier))


@pytest.mark.covers("ADR-044")
def test_canonical_compiler_preserves_null_and_normalizes_aware_datetime_in_checksum() -> None:
    request = _compiler_request()
    enriched = dict(request.mapped_rows[0]) | {
        "optional_value": None,
        "observed_at": datetime(2026, 8, 25, 3, tzinfo=UTC),
    }
    compiled = compile_canonical_rows(
        replace(request, mapped_rows=(enriched, *request.mapped_rows[1:]))
    )
    row = dict(compiled.rows[0])
    assert row["optional_value"] is None
    assert row["observed_at"] == datetime(2026, 8, 25, 3, tzinfo=UTC)
    assert compiled.content_checksum != compile_canonical_rows(request).content_checksum


@pytest.mark.covers("ADR-044", "ADR-047")
def test_retry_reuses_immutable_source_semantics_without_rewriting_publication_time() -> None:
    request, tooling, _order, _attempts = _case()
    first = run_semantic_sidecar(request, tooling)
    retried = run_semantic_sidecar(
        replace(
            request,
            now=request.now + timedelta(minutes=1),
            operation_id="sidecar-unit-retry-002",
        ),
        tooling,
    )
    sources = tooling.source_writer
    assert isinstance(sources, MemorySources)
    assert sources.materializations == 3
    assert len(sources.items) == 1
    assert first[0].outcome is SemanticSidecarOutcome.PUBLISHED
    assert retried[0].outcome is SemanticSidecarOutcome.PUBLISHED
    assert first[0].publications != retried[0].publications


@pytest.mark.covers("ADR-045", "ADR-047")
def test_unresolved_product_is_a_decided_quarantine_not_a_retryable_failure() -> None:
    request, tooling, order, attempts = _case()
    contract = request.contracts[0]
    invalid_document = replace(
        request.document,
        rows=tuple(row | {"asin": "INVALID00000"} for row in request.document.rows),
    )
    assessment = ContractAssessment(
        ContractLoad(
            contract,
            partition_for(request.manifest, contract),
            map_rows(MappingRequest(contract, invalid_document)),
        ),
        LoadOutcome(),
    )
    result = run_semantic_sidecar(
        replace(request, document=invalid_document, assessments=(assessment,)),
        tooling,
    )
    assert result[0].outcome is SemanticSidecarOutcome.BLOCKED
    assert order == []
    assert [event.kind.value for event in attempts.events] == [
        value for _ in range(len(attempts.events) // 2) for value in ("started", "quarantined")
    ]


@pytest.mark.covers("ADR-047")
def test_commit_dependency_failure_releases_the_live_lease_for_retry() -> None:
    request, tooling, order, _attempts = _case()

    class FailingPublications(MemoryPublications):
        def commit_group(
            self,
            request: CommitPublicationGroupRequest,  # noqa: ARG002
        ) -> StateWriteOutcome:
            raise RuntimeError("dependency failed")

    leases = tooling.leases
    assert isinstance(leases, MemoryLeases)
    failing = replace(tooling, publications=FailingPublications(order))
    with pytest.raises(RuntimeError, match="dependency failed"):
        run_semantic_sidecar(request, failing)
    assert leases.releases == 1


@pytest.mark.covers("ADR-047", "ADR-049")
def test_publication_state_size_refusal_precedes_canonical_materialization() -> None:
    request, tooling, order, _attempts = _case()

    class OversizedPublications(MemoryPublications):
        def validate_group(self, request: CommitPublicationGroupRequest) -> None:
            del request
            raise ValueError("publication state record exceeds the safe bound")

    leases = tooling.leases
    assert isinstance(leases, MemoryLeases)
    publications = OversizedPublications(order)

    with pytest.raises(ValueError, match="safe bound"):
        run_semantic_sidecar(request, replace(tooling, publications=publications))

    assert order == []
    assert publications.current_items == {}
    assert leases.releases == 1


@pytest.mark.covers("ADR-047", "ADR-049")
def test_cleanup_continues_and_surfaces_every_failure() -> None:
    request, tooling, order, _attempts = _vendor_case("vendor-control-total-consistent")

    class FailingPublications(MemoryPublications):
        def commit_group(
            self,
            request: CommitPublicationGroupRequest,  # noqa: ARG002
        ) -> StateWriteOutcome:
            raise RuntimeError("publication failed")

    class FailingAttempts(MemoryAttempts):
        def __init__(self) -> None:
            super().__init__()
            self.failed_appends = 0

        def append(self, event: AttemptEvent) -> StateWriteOutcome:
            if event.kind is AttemptEventKind.FAILED:
                self.failed_appends += 1
                if self.failed_appends == 1:
                    raise RuntimeError("FAILED append failed")
            return super().append(event)

    class FailingReleases(MemoryLeases):
        def release(self, grant: LeaseGrant, *, at: datetime) -> StateWriteOutcome:
            del grant, at
            self.releases += 1
            if self.releases == 1:
                raise RuntimeError("first release failed")
            return StateWriteOutcome(False, "second release refused")

    failing_attempts = FailingAttempts()
    failing_leases = FailingReleases()
    failing = replace(
        tooling,
        attempts=failing_attempts,
        leases=failing_leases,
        publications=FailingPublications(order),
    )

    with pytest.raises(BaseExceptionGroup, match="cleanup failed") as raised:
        run_semantic_sidecar(request, failing)

    assert [str(error) for error in raised.value.exceptions] == [
        "publication failed",
        "FAILED append failed",
        "first release failed",
        ("ADLY-SEMANTIC_SIDECAR-0001: Semantic publication lease release was refused."),
    ]
    assert failing_attempts.failed_appends == 2
    assert failing_leases.releases == 2
    assert [event.kind for event in failing_attempts.events] == [
        AttemptEventKind.STARTED,
        AttemptEventKind.STARTED,
        AttemptEventKind.FAILED,
    ]


@pytest.mark.covers("ADR-047")
def test_same_execution_replay_reuses_exact_attempt_event_slots() -> None:
    request, tooling, _order, attempts = _case()

    first = run_semantic_sidecar(request, tooling)
    event_snapshot = tuple(attempts.events)
    second = run_semantic_sidecar(request, tooling)

    assert first[0].outcome is SemanticSidecarOutcome.PUBLISHED
    assert second[0].outcome is SemanticSidecarOutcome.PUBLISHED
    assert tuple(attempts.events) == event_snapshot
    assert second[0].receipts[0].candidate is not None
    assert second[0].receipts[0].successful is not None
    assert (
        second[0].receipts[0].candidate.publication.publication_id
        == second[0].receipts[0].successful.publication.publication_id
    )
    assert second[0].receipts[0].attempt_id == event_snapshot[0].attempt_id
    assert second[0].receipts[0].terminal_event_kind is AttemptEventKind.SUCCEEDED
    assert {event.kind for event in event_snapshot} == {
        AttemptEventKind.STARTED,
        AttemptEventKind.SUCCEEDED,
    }


def test_completed_replay_refuses_malformed_attempt_history() -> None:
    request, tooling, _order, attempts = _case()
    run_semantic_sidecar(request, tooling)
    attempts.events.append(attempts.events[-1])

    with pytest.raises(InvariantError, match="history differs"):
        run_semantic_sidecar(request, tooling)


def test_completed_replay_requires_exact_candidate_and_successful_artifacts() -> None:
    request, tooling, _order, _attempts = _case()
    run_semantic_sidecar(request, tooling)
    canonical = tooling.canonical_writer
    assert isinstance(canonical, MemoryCanonical)
    canonical.requests.pop(0)

    with pytest.raises(InvariantError, match="physical attestation"):
        run_semantic_sidecar(request, tooling)


@pytest.mark.covers("ADR-047")
def test_redelivery_after_started_uses_new_attempt_with_predecessor_lineage() -> None:
    request, tooling, order, attempts = _case()

    class FailingPublications(MemoryPublications):
        def commit_group(
            self,
            request: CommitPublicationGroupRequest,  # noqa: ARG002
        ) -> StateWriteOutcome:
            raise RuntimeError("first delivery failed after STARTED")

    with pytest.raises(RuntimeError, match="after STARTED"):
        run_semantic_sidecar(
            request,
            replace(tooling, publications=FailingPublications(order)),
        )
    first_attempt = attempts.events[0].attempt_id
    retry = replace(
        request,
        now=request.now + timedelta(seconds=1),
        execution_id="execution-unit-002",
        predecessor_execution_id=request.execution_id,
    )

    result = run_semantic_sidecar(retry, tooling)

    assert result[0].outcome is SemanticSidecarOutcome.PUBLISHED
    retry_started = next(
        event
        for event in attempts.events
        if event.kind is AttemptEventKind.STARTED and event.attempt_id != first_attempt
    )
    retry_terminal = next(
        event
        for event in attempts.events
        if event.attempt_id == retry_started.attempt_id and event.sequence == 1
    )
    assert retry_started.predecessor == first_attempt
    assert retry_terminal.predecessor == first_attempt


@pytest.mark.covers("ADR-047", "ADR-049")
def test_second_member_busy_fails_started_attempts_and_releases_first_grant() -> None:
    request, tooling, _order, attempts = _vendor_case("vendor-control-total-consistent")

    class BusySecondLease(MemoryLeases):
        def __init__(self) -> None:
            super().__init__()
            self.acquisitions = 0

        def acquire(self, request: LeaseRequest) -> LeaseAcquireOutcome:
            self.acquisitions += 1
            if self.acquisitions == 2:
                return LeaseAcquireOutcome(None, "second member busy")
            return super().acquire(request)

    leases = BusySecondLease()
    with pytest.raises(Exception, match="temporarily busy"):
        run_semantic_sidecar(request, replace(tooling, leases=leases))

    assert leases.releases == 1
    assert [event.kind for event in attempts.events] == [
        AttemptEventKind.STARTED,
        AttemptEventKind.STARTED,
        AttemptEventKind.FAILED,
        AttemptEventKind.FAILED,
    ]


@pytest.mark.covers("ADR-047", "ADR-049")
def test_second_member_planning_failure_occurs_before_attempt_or_lease_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, tooling, _order, attempts = _vendor_case("vendor-control-total-consistent")
    leases = tooling.leases
    assert isinstance(leases, MemoryLeases)
    actual = semantic_sidecar_module.plan_curation
    planned_group_members = 0

    def fail_second_group_member(
        planning: CanonicalCurationRequest,
    ) -> CanonicalCurationPlan:
        nonlocal planned_group_members
        if len(planning.available_sources) == 2:
            planned_group_members += 1
            if planned_group_members == 2:
                raise RuntimeError("second group plan failed")
        return actual(planning)

    monkeypatch.setattr(semantic_sidecar_module, "plan_curation", fail_second_group_member)

    with pytest.raises(RuntimeError, match="second group plan failed"):
        run_semantic_sidecar(request, tooling)

    assert attempts.events == []
    assert leases.grants == []
    assert leases.releases == 0


@pytest.mark.covers("ADR-047", "ADR-049")
def test_oversized_prepared_group_is_refused_before_publication_calls() -> None:
    request, tooling, order, attempts = _case()
    prepared = semantic_sidecar_module._prepare_delivery(  # pyright: ignore[reportPrivateUsage]
        request,
        tooling,
    ).partitions[0]
    leases = tooling.leases
    publications = tooling.publications
    assert isinstance(leases, MemoryLeases)
    assert isinstance(publications, MemoryPublications)

    with pytest.raises(ValueError, match=str(MAX_COMMIT_GROUP_MEMBERS)):
        semantic_sidecar_module._PublishPreparedRequest(  # pyright: ignore[reportPrivateUsage]
            request,
            tooling,
            (prepared,) * (MAX_COMMIT_GROUP_MEMBERS + 1),
        )

    assert attempts.events == []
    assert leases.grants == []
    assert leases.releases == 0
    assert order == []
    assert publications.current_items == {}


@pytest.mark.covers("ADR-047", "ADR-049")
def test_atomic_terminal_refusal_moves_no_current_and_cleanup_records_failures() -> None:
    request, tooling, order, _attempts = _vendor_case("vendor-control-total-consistent")
    attempts = MemoryAttempts()

    class RefuseAtomicCommit(MemoryPublications):
        def commit_group(
            self,
            request: CommitPublicationGroupRequest,  # noqa: ARG002
        ) -> StateWriteOutcome:
            return StateWriteOutcome(False, "injected atomic refusal")

    leases = tooling.leases
    publications = RefuseAtomicCommit(order, attempts)
    assert isinstance(leases, MemoryLeases)

    with pytest.raises(InvariantError, match="refused fenced"):
        run_semantic_sidecar(
            request,
            replace(tooling, attempts=attempts, publications=publications),
        )

    assert publications.current_items == {}
    assert leases.releases == 2
    assert [event.kind for event in attempts.events].count(AttemptEventKind.SUCCEEDED) == 0
    assert [event.kind for event in attempts.events].count(AttemptEventKind.FAILED) == 2


@pytest.mark.covers("ADR-049")
def test_observed_suppression_marks_exact_bucket_unavailable_and_blocks_success() -> None:
    request, tooling, order, attempts = _case()
    contract = request.contracts[0]
    suppressed_document = replace(
        request.document,
        rows=tuple(row | {"rows_suppressed": "true"} for row in request.document.rows),
    )
    assessment = ContractAssessment(
        ContractLoad(
            contract,
            partition_for(request.manifest, contract),
            map_rows(MappingRequest(contract, suppressed_document)),
        ),
        LoadOutcome(),
    )
    result = run_semantic_sidecar(
        replace(request, document=suppressed_document, assessments=(assessment,)),
        tooling,
    )
    assert result[0].outcome is SemanticSidecarOutcome.BLOCKED
    assert order == ["candidate"]
    canonical = tooling.canonical_writer
    assert isinstance(canonical, MemoryCanonical)
    assert "suppression:exact-bucket" in canonical.requests[0].publication.evidence.failed_rules
    assert [event.kind.value for event in attempts.events] == ["started", "quarantined"]


@pytest.mark.covers("ADR-045", "ADR-047")
def test_unresolved_marketplace_is_blocked_before_partition_without_retry() -> None:
    request, tooling, order, attempts = _case()
    contract = request.contracts[0]
    unknown_document = replace(
        request.document,
        rows=tuple(row | {"marketplace_name": "UNKNOWN"} for row in request.document.rows),
    )
    assessment = ContractAssessment(
        ContractLoad(
            contract,
            partition_for(request.manifest, contract),
            map_rows(MappingRequest(contract, unknown_document)),
        ),
        LoadOutcome(),
    )
    result = run_semantic_sidecar(
        replace(request, document=unknown_document, assessments=(assessment,)),
        tooling,
    )
    assert result[0].outcome is SemanticSidecarOutcome.BLOCKED
    assert "no canonical mapping" in result[0].detail
    assert order == []
    assert attempts.events == []


@pytest.mark.covers("ADR-044", "ADR-045")
def test_candidate_object_serializes_candidate_bound_mapping_evidence() -> None:
    request, tooling, _order, _attempts = _case()
    run_semantic_sidecar(request, tooling)
    canonical = tooling.canonical_writer
    assert isinstance(canonical, MemoryCanonical)
    candidate = next(item for item in canonical.requests if item.destination.value == "candidate")
    payload = json.loads(canonical_object_bytes(candidate))
    evidence = payload["evidence"]
    assert evidence["subject"]["content_checksum"] == payload["content_checksum"]
    identity = next(item for item in evidence["results"] if item["family"] == "identity")
    # The object carries the claim; the full candidate-bound list is behind its digest.
    mappings = tooling.identity_evidence.read(parse_identity_claim(identity["detail"]))
    assert 2 in {item["mapping_version"] for item in mappings}
    assert identity["outcome"] == "pass"


@pytest.mark.covers("ADR-044", "ADR-053")
def test_semantic_ingestion_refuses_governance_not_installed_with_the_runtime() -> None:
    sidecar_request, tooling, _order, _attempts = _case()
    contract_id = sidecar_request.contracts[0].contract_id
    digest = "sha256:" + "a" * 64
    governance = ProducerGovernance(
        query_pack_id=contract_id,
        query_pack_version=1,
        query_pack_digest=digest,
        query_digest=digest,
        data_contract_id=contract_id,
        data_contract_digest=digest,
        source_pack_id=contract_id,
        source_pack_digest=digest,
    )
    delivery = LoadedDelivery(
        manifest=replace(sidecar_request.manifest, governance=governance),
        manifest_key=sidecar_request.manifest.manifest_key,
        text="not read before governance validation",
        landed_ref=sidecar_request.landed_delivery,
    )
    request = SemanticIngestionRequest(
        delivery=delivery,
        contracts=sidecar_request.contracts,
        columns=load_registry(ROOT),
        now=sidecar_request.now,
        operation_id="governance-refusal-unit",
        execution_id="governance-refusal-execution",
        plan_digest="sha256:governance-refusal-plan",
        installed_governance=(replace(governance, query_digest="sha256:" + "b" * 64),),
    )

    with pytest.raises(LandingError, match="artifacts differ"):
        ingest_semantic_delivery(request, tooling)


@pytest.mark.covers("ADR-044", "ADR-045", "ADR-047", "ADR-049")
def test_v2_only_ingestion_publishes_without_v1_state_or_athena_collaborators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sidecar_request, tooling, order, _attempts = _case()
    text = (
        (FIXTURES / "amc-retail-purchases.csv").read_text().replace("ATVPDKIKX0DER", "AMAZON.COM")
    )
    pack = next(
        item
        for item in load_query_packs(ROOT)
        if item.contract_id == sidecar_request.contracts[0].contract_id
    )
    manifest = replace(
        sidecar_request.manifest,
        governance=pack.governance,
        layout_version=GOVERNED_MANIFEST_LAYOUT,
    )
    delivery = LoadedDelivery(
        manifest=manifest,
        manifest_key=manifest.manifest_key,
        text=text,
        landed_ref=replace(
            sidecar_request.landed_delivery,
            manifest_key=manifest.manifest_key,
            object_key=manifest.object_keys()[0],
        ),
    )
    progress_events: list[tuple[SemanticSidecarProgress, int]] = []
    traced = MagicMock()
    parse_span = MagicMock()
    traced.return_value.__enter__.return_value = parse_span
    monkeypatch.setattr(semantic_ingestion_module, "span", traced)
    request = SemanticIngestionRequest(
        delivery=delivery,
        contracts=sidecar_request.contracts,
        columns=load_registry(ROOT),
        now=sidecar_request.now,
        operation_id="v2-only-unit-001",
        execution_id="v2-only-execution-001",
        plan_digest="sha256:v2-only-plan",
        installed_governance=(pack.governance,),
        progress=lambda event: progress_events.append((event, order.count("commit_group"))),
    )
    results = ingest_semantic_delivery(request, tooling)
    sources = tooling.source_writer
    assert isinstance(sources, MemorySources)
    assert sources.items
    assert results[0].outcome is SemanticSidecarOutcome.PUBLISHED
    traced.assert_called_once_with(
        semantic_ingestion_module.INGEST_PARSE,
        operation_id=request.operation_id,
        execution_id=request.execution_id,
        predecessor_execution_id="none",
        plan_digest=request.plan_digest,
        stage="parse",
        completed=0,
        total=1,
        contract_count=len(request.contracts),
    )
    parse_span.record.assert_called_once_with(
        assessment_count=len(request.contracts),
        completed=1,
        outcome="completed",
    )
    assert len(progress_events) > 1
    events = [event for event, _commits in progress_events]
    assert {event.stage for event in events} == {"publish"}
    assert {event.total for event in events} == {len(events)}
    assert [event.completed for event in events] == list(range(1, len(events) + 1))
    assert [commits for _event, commits in progress_events] == list(range(1, len(events) + 1))
    assert set(SemanticIngestionRequest.__dataclass_fields__) == {
        "delivery",
        "contracts",
        "columns",
        "now",
        "operation_id",
        "execution_id",
        "plan_digest",
        "predecessor_execution_id",
        "telemetry_operation_id",
        "installed_governance",
        "discard_policies",
        "progress",
        "feasibility",
    }


def _fixture_sidecar_request(contract_id: str, operation_id: str) -> SemanticSidecarRequest:
    contract = next(
        contract for _, contract in load_contracts(ROOT) if contract.contract_id == contract_id
    )
    manifest = _manifest(contract_id)
    document = read(
        DeliveredFile(
            (FIXTURES / f"{contract_id}.csv").read_text(),
            contract.source_format,
            (FIXTURES / f"{contract_id}.manifest.json").read_text(),
        )
    )
    assessment = ContractAssessment(
        ContractLoad(
            contract,
            partition_for(manifest, contract),
            map_rows(MappingRequest(contract, document)),
        ),
        LoadOutcome(),
    )
    return SemanticSidecarRequest(
        manifest=manifest,
        document=document,
        landed_delivery=LandedDeliveryRef(
            manifest.manifest_key,
            f"manifest-{contract_id}-v1",
            f"sha256:manifest-{contract_id}",
            manifest.object_keys()[0],
            f"object-{contract_id}-v1",
            manifest.objects[0].checksum,
        ),
        assessments=(assessment,),
        contracts=(contract,),
        now=datetime(2026, 8, 24, tzinfo=UTC),
        operation_id=operation_id,
        execution_id=f"{operation_id}-execution",
    )


@pytest.mark.covers("ADR-052", "ADR-053")
def test_campaign_dimension_publishes_canonical_identity_and_media_context() -> None:
    _base, tooling, _order, _attempts = _case()
    request = _fixture_sidecar_request(
        "amc-campaign-dsp",
        "campaign-dimension-unit-001",
    )

    results = run_semantic_sidecar(request, tooling)

    assert results[0].outcome is SemanticSidecarOutcome.PUBLISHED
    canonical = tooling.canonical_writer
    assert isinstance(canonical, MemoryCanonical)
    candidate = next(
        item
        for item in canonical.requests
        if item.destination is CanonicalDestination.CANDIDATE
        and item.publication.partition.scope.dataset.value == "dim_campaign"
    )
    rows = tuple(dict(row) for row in candidate.rows)
    assert rows
    assert all(str(row["campaign_key"]).startswith("campaign:") for row in rows)
    assert all(str(row["advertiser_key"]).startswith("advertiser:") for row in rows)
    assert {row["source_system_key"] for row in rows} == {"amc"}
    assert {row["source_scope_namespace"] for row in rows} == {"amc.entity"}
    assert {row["source_scope_value"] for row in rows} == {"fictional-fixture-amc-entity"}
    assert {row["source_campaign_namespace"] for row in rows} == {"amc.dsp.campaign_id_string"}
    assert {row["source_campaign_identifier_kind"] for row in rows} == {"campaign_id_string"}
    catalog = load_synthetic_entity_catalog(ROOT)
    expected_dsp_ids = {
        campaign.source_aliases["amc.dsp.campaign_id_string"]
        for campaign in catalog.campaigns
        if campaign.channel == "amazon_dsp"
    }
    assert {row["source_campaign_id"] for row in rows} == expected_dsp_ids
    assert {row["ecosystem_key"] for row in rows} == {"amazon"}
    assert {row["platform_key"] for row in rows} == {"amazon_ads"}
    assert {row["channel_group_key"] for row in rows} == {"display"}
    assert {row["channel_key"] for row in rows} == {"amazon_dsp"}
    # Write-discipline §2.1 rule 1: the delivery emits exactly one dim_campaign candidate —
    # the latest bucket's folded complete history — never one candidate per bucket.
    dimension_candidates = tuple(
        item
        for item in canonical.requests
        if item.destination is CanonicalDestination.CANDIDATE
        and item.publication.partition.scope.dataset.value == "dim_campaign"
    )
    assert len(dimension_candidates) == 1
    current_rows = tuple(row for row in rows if row["is_current"] is True)
    assert {row["campaign_key"] for row in current_rows} == {row["campaign_key"] for row in rows}
    assert all(row["valid_to"] == datetime(2999, 12, 31, tzinfo=UTC) for row in current_rows)
    assert all(
        row["valid_to"] != datetime(2999, 12, 31, tzinfo=UTC)
        for row in rows
        if row["is_current"] is not True
    )
    identity = next(
        result
        for result in candidate.publication.evidence.results
        if result.family is PrepublicationRuleFamily.IDENTITY
    )
    # Write-discipline §2.6: the record carries a claim; the mappings are read back verified.
    mappings = tooling.identity_evidence.read(parse_identity_claim(identity.detail))
    assert {item["source_system_key"] for item in mappings} >= {"amc"}
    assert {
        "namespace": "amc.entity",
        "value": "fictional-fixture-amc-entity",
    } in [item["source_scope"] for item in mappings]
    assert "campaign_id_string" in {item["source_identifier_kind"] for item in mappings}


@pytest.mark.covers("ADR-053")
@pytest.mark.parametrize(
    ("dimension_id", "fact_id"),
    (
        ("amc-campaign-dsp", "amc-dsp-impressions"),
        ("amc-campaign-sponsored", "amc-reach-frequency"),
    ),
)
def test_matching_campaign_fact_and_dimension_rows_share_canonical_keys(
    dimension_id: str,
    fact_id: str,
) -> None:
    _base, tooling, _order, _attempts = _case()
    dimension_results = run_semantic_sidecar(
        _fixture_sidecar_request(dimension_id, f"{dimension_id}-identity-unit"),
        tooling,
    )
    fact_results = run_semantic_sidecar(
        _fixture_sidecar_request(fact_id, f"{fact_id}-identity-unit"),
        tooling,
    )
    assert all(result.outcome is SemanticSidecarOutcome.PUBLISHED for result in dimension_results)
    assert fact_results
    assert all(result.outcome is SemanticSidecarOutcome.PUBLISHED for result in fact_results), (
        fact_results
    )
    canonical = tooling.canonical_writer
    assert isinstance(canonical, MemoryCanonical)
    dimension_keys = {
        str(dict(row)["campaign_key"])
        for item in canonical.requests
        if item.destination is CanonicalDestination.CANDIDATE
        and item.publication.partition.scope.dataset.value == "dim_campaign"
        for row in item.rows
    }
    fact_keys = {
        str(dict(row)["campaign_key"])
        for item in canonical.requests
        if item.destination is CanonicalDestination.CANDIDATE
        and item.publication.partition.scope.dataset.value == "fct_ad_exposure"
        for row in item.rows
    }

    assert dimension_keys
    assert fact_keys == dimension_keys


@pytest.mark.covers("ADR-044")
def test_publication_leases_anchor_to_the_real_clock_not_the_logical_request_instant() -> None:
    """A lease is a real-time mutex checked against the store's wall clock at commit.

    Anchoring it to the request's logical publication instant made every lease in a
    long batch expire ten real minutes after process start, refusing every commit
    after that point.
    """
    request, tooling, _order, _attempts = _vendor_case("vendor-control-total-consistent")

    class RequestCapturingLeases(MemoryLeases):
        def __init__(self) -> None:
            super().__init__()
            self.requested_at: list[datetime] = []

        def acquire(self, request: LeaseRequest) -> LeaseAcquireOutcome:
            self.requested_at.append(request.requested_at)
            return super().acquire(request)

    leases = RequestCapturingLeases()
    real_instant = request.now + timedelta(hours=3)
    results = run_semantic_sidecar(
        request,
        replace(tooling, leases=leases, clock=lambda: real_instant),
    )

    assert {result.outcome for result in results} == {SemanticSidecarOutcome.PUBLISHED}
    assert leases.requested_at, "the publication group acquired no lease."
    assert all(item == real_instant for item in leases.requested_at)


def test_canonical_checksum_digest_strips_the_sha256_prefix_to_the_64_hex_digest() -> None:
    """The Stage-1 roster keeps digest bytes only, so both renderings must compare equal."""
    rendered = canonical_rows_checksum(())
    digest = canonical_checksum_digest(rendered)
    assert rendered == f"sha256:{digest}"
    assert len(digest) == 64 and digest == digest.lower()
    assert canonical_checksum_digest(digest) == digest
    for malformed in ("sha256:", "md5:" + "0" * 32, "sha256:" + "0" * 63, "sha256:" + "G" * 64):
        with pytest.raises(ValueError, match="content checksum"):
            canonical_checksum_digest(malformed)


def _chunked_dimension_request(  # noqa: PLR0913 -- one landing-shaped chunk per call
    operation_id: str, first: date, last: date, *, retrieval_second: int
) -> SemanticSidecarRequest:
    """Build one amc-campaign-dsp chunk delivery with its own landed identity.

    Mirrors the landing seam: each chunk is its own delivery — own bytes, own manifest,
    own retrieval instant — holding only the chunk's populated days.
    """
    base = _fixture_sidecar_request("amc-campaign-dsp", operation_id)
    text = (FIXTURES / "amc-campaign-dsp.csv").read_text()
    # AMC's dialect both ways, as the landing seam does: a campaign name holds an escaped quote.
    fieldnames, rows = dict_rows(text)
    assert fieldnames is not None
    selected = [
        row for row in rows if first <= date.fromisoformat(str(row["source_observed_on"])) <= last
    ]
    chunk_text = write_rows(fieldnames, cast("list[dict[str, object]]", selected))
    chunk_body = chunk_text.encode()
    declaration = json.loads((FIXTURES / "amc-campaign-dsp.manifest.json").read_text())
    declaration["period_start"] = first.isoformat()
    declaration["period_end"] = last.isoformat()
    declaration["source_watermark"] = last.isoformat()
    declaration["retrieved_at"] = f"2026-09-22T12:00:{retrieval_second:02d}Z"
    manifest = build_manifest(
        BuildManifestRequest(
            declaration=declaration,
            objects=(LandedObject("data-0001.csv", len(chunk_body), checksum_of(chunk_body)),),
            declared_columns=declared_columns_of(chunk_text, source_format="csv"),
        )
    )
    contract = base.contracts[0]
    document = read(DeliveredFile(chunk_text, contract.source_format, write_manifest(manifest)))
    assessment = ContractAssessment(
        ContractLoad(
            contract,
            partition_for(manifest, contract),
            map_rows(MappingRequest(contract, document)),
        ),
        LoadOutcome(),
    )
    return replace(
        base,
        manifest=manifest,
        document=document,
        assessments=(assessment,),
        landed_delivery=LandedDeliveryRef(
            manifest.manifest_key,
            f"manifest-{operation_id}-v1",
            f"sha256:manifest-{operation_id}",
            manifest.object_keys()[0],
            f"object-{operation_id}-v1",
            manifest.objects[0].checksum,
        ),
    )


def _prepared_dimension_partitions(
    request: SemanticSidecarRequest,
    tooling: SemanticSidecarTooling,
) -> tuple[tuple[str, tuple[tuple[tuple[str, object], ...], ...]], ...]:
    """Run the sidecar capturing the prepared dim_campaign roster the callback sees."""
    captured: list[tuple[str, tuple[tuple[tuple[str, object], ...], ...]]] = []

    def capture(plan: PreparedProjectionPlan) -> None:
        captured.extend(
            (item.content_checksum, item.canonical_rows)
            for item in plan.partitions
            if item.dataset == "dim_campaign"
        )

    results = run_semantic_sidecar(replace(request, feasibility=capture), tooling)
    assert results, "the sidecar must return a contract result"
    return tuple(captured)


@pytest.mark.covers("ADR-075", "ADR-025")
def test_chunked_dimension_deliveries_fold_sequentially_and_reobservation_converges() -> None:
    """Write-discipline §2.1 rules 1+3: the order-dependent fold across chunk deliveries.

    The first chunk carries a tracked-attribute change; the next chunk folds over the
    published history without raising, and a re-observed chunk re-derives the identical
    complete candidate from a base read at its own first bucket — the restatement-overlap
    case that a base read at the latest bucket end rejects as an out-of-order change.
    """
    _base, tooling, _order, _attempts = _case()
    first_chunk = _chunked_dimension_request(
        "chunk-dim-a", date(2026, 8, 10), date(2026, 8, 12), retrieval_second=1
    )
    prepared_a = _prepared_dimension_partitions(first_chunk, tooling)
    assert len(prepared_a) == 1
    rows_a = tuple(dict(row) for row in prepared_a[0][1])
    assert any(row["is_current"] is not True for row in rows_a), (
        "the fixture window must contain a tracked-attribute change for this test to bite"
    )

    next_chunk = _chunked_dimension_request(
        "chunk-dim-b", date(2026, 8, 13), date(2026, 8, 15), retrieval_second=2
    )
    prepared_b = _prepared_dimension_partitions(next_chunk, tooling)
    assert len(prepared_b) == 1
    rows_b = tuple(dict(row) for row in prepared_b[0][1])
    # The fold over the published base retains the first chunk's version transition.
    closed_a = {
        (row["campaign_key"], str(row["valid_from"]))
        for row in rows_a
        if row["is_current"] is not True
    }
    closed_b = {
        (row["campaign_key"], str(row["valid_from"]))
        for row in rows_b
        if row["is_current"] is not True
    }
    assert closed_a <= closed_b

    reobserved = _chunked_dimension_request(
        "chunk-dim-a-replay", date(2026, 8, 10), date(2026, 8, 12), retrieval_second=3
    )
    prepared_replay = _prepared_dimension_partitions(reobserved, tooling)
    assert len(prepared_replay) == 1
    # Byte-identical convergence: the re-fold from a base at the chunk's own first bucket
    # re-derives exactly the candidate the original chunk emitted.
    assert prepared_replay[0][0] == prepared_a[0][0]
    assert prepared_replay[0][1] == prepared_a[0][1]


# --- Identity-mapping claim check (write-discipline §2.6) -----------------------------------


def _one_day_request(contract_id: str, day: str, operation_id: str) -> SemanticSidecarRequest:
    """Narrow the committed generated fixture to one day: one bucket at its real width."""
    full = _fixture_sidecar_request(contract_id, operation_id)
    contract = full.contracts[0]
    date_column = next(
        item.source_path.rsplit(".", 1)[-1]
        for item in contract.fields
        if item.target_column == "date_key"
    )
    rows = tuple(row for row in full.document.rows if row.get(date_column) == day)
    assert rows, "the fixture day must carry rows"
    document = replace(full.document, rows=rows)
    assessment = ContractAssessment(
        ContractLoad(
            contract,
            partition_for(full.manifest, contract),
            map_rows(MappingRequest(contract, document)),
        ),
        LoadOutcome(),
    )
    return replace(full, document=document, assessments=(assessment,))


def _widest_conversion_day() -> str:
    _fieldnames, rows = dict_rows((FIXTURES / "amc-attributed-conversion.csv").read_text())
    by_day: dict[str, set[str]] = {}
    for row in rows:
        by_day.setdefault(row["conversion_event_date_utc"], set()).add(row["campaign_id_string"])
    return max(sorted(by_day), key=lambda day: len(by_day[day]))


def _record_bytes(publication: SuccessfulPublication) -> int:
    return publication_record_bytes(publication)


def _campaign_estate_record_bytes(campaigns: int) -> int:
    """Publish one conversion bucket carrying `campaigns` distinct campaigns; return its bytes.

    The widest committed fixture day supplies the row shape; each campaign is a fictional
    `campaign_id_string`, which the compiler resolves deterministically, so the sidecar seam
    selects one campaign identity mapping per campaign without any I/O.
    """
    _base, tooling, _order, _attempts = _case()
    day = _widest_conversion_day()
    request = _one_day_request("amc-attributed-conversion", day, f"campaign-estate-{campaigns}")
    template = dict(request.document.rows[0])
    rows = tuple(
        template | {"campaign_id_string": f"9190{ordinal:014d}"} for ordinal in range(campaigns)
    )
    document = replace(request.document, rows=rows)
    (assessment,) = request.assessments
    contract = assessment.load.contract
    load = replace(assessment.load, rows=map_rows(MappingRequest(contract, document)))
    request = replace(request, document=document, assessments=(replace(assessment, load=load),))

    results = run_semantic_sidecar(request, tooling)

    assert results[0].outcome is SemanticSidecarOutcome.PUBLISHED, results[0].detail
    publications = tooling.publications
    assert isinstance(publications, MemoryPublications)
    (publication,) = publications.current_items.values()
    (claim,) = (
        parse_identity_claim(result.detail)
        for result in publication.evidence.results
        if result.family is PrepublicationRuleFamily.IDENTITY
    )
    mappings = tooling.identity_evidence.read(claim)
    selected = {item["source"] for item in mappings if "campaign_id_string" in str(item["source"])}
    # The grain under test: the record's identity evidence covers every campaign of the estate.
    assert len(selected) == campaigns
    return _record_bytes(publication)


@pytest.mark.covers("ADR-134")
def test_publication_record_stays_within_the_item_bound_at_the_governed_campaign_estate() -> None:
    measured = assert_within_bound(
        measure=_campaign_estate_record_bytes,
        declared_maximum=BULK_CAMPAIGN_COUNT,
        bound=MAX_PUBLICATION_ITEM_BYTES,
        unit="bytes",
        grain="publication record",
    )

    assert BULK_CAMPAIGN_COUNT == 1_000
    assert MAX_PUBLICATION_ITEM_BYTES == 32_000
    assert 0 < measured <= MAX_PUBLICATION_ITEM_BYTES


def test_identity_claim_document_is_written_before_candidate_success_and_commit() -> None:
    request, tooling, order, _attempts = _case()
    tooling = replace(tooling, identity_evidence=MemoryIdentityEvidence(order))

    results = run_semantic_sidecar(request, tooling)

    assert results[0].outcome is SemanticSidecarOutcome.PUBLISHED
    assert order[:4] == ["identity_evidence", "candidate", "successful", "commit_group"]
    candidate = results[0].receipts[0].candidate
    assert candidate is not None
    (claim,) = (
        parse_identity_claim(result.detail)
        for result in candidate.publication.evidence.results
        if result.family is PrepublicationRuleFamily.IDENTITY
    )
    assert tooling.identity_evidence.read(claim)


@pytest.mark.parametrize("day", ["2026-07-16", _widest_conversion_day()])
def test_conversion_bucket_record_stays_well_under_the_transaction_item_bound(day: str) -> None:
    _base, tooling, _order, _attempts = _case()
    request = _one_day_request("amc-attributed-conversion", day, f"claim-size-{day}")

    results = run_semantic_sidecar(request, tooling)

    assert results[0].outcome is SemanticSidecarOutcome.PUBLISHED, results[0].detail
    publications = tooling.publications
    assert isinstance(publications, MemoryPublications)
    (publication,) = publications.current_items.values()
    (claim,) = (
        parse_identity_claim(result.detail)
        for result in publication.evidence.results
        if result.family is PrepublicationRuleFamily.IDENTITY
    )
    mappings = tooling.identity_evidence.read(claim)
    campaigns = {item["source"] for item in mappings if "campaign_id_string" in str(item["source"])}
    # The dev refusal of 2026-10-09: 79 mappings, 57 campaigns, 33,762 B inline.
    assert claim.count == len(mappings) >= 79
    assert len(campaigns) >= 57
    assert MAX_PUBLICATION_ITEM_BYTES == 32_000
    assert _record_bytes(publication) < MAX_PUBLICATION_ITEM_BYTES // 2
    # Negative control: the superseded inline detail on the same record exceeds the bound.
    inline = "Governed identity mappings selected: " + json.dumps(
        list(mappings), sort_keys=True, separators=(",", ":")
    )
    superseded = replace(
        publication,
        evidence=replace(
            publication.evidence,
            results=tuple(
                replace(result, detail=inline)
                if result.family is PrepublicationRuleFamily.IDENTITY
                else result
                for result in publication.evidence.results
            ),
        ),
    )
    assert _record_bytes(superseded) > MAX_PUBLICATION_ITEM_BYTES


def test_record_size_does_not_grow_with_a_much_larger_synthetic_mapping_list() -> None:
    _base, tooling, _order, _attempts = _case()
    request = _one_day_request("amc-attributed-conversion", "2026-07-16", "claim-size-large")
    run_semantic_sidecar(request, tooling)
    publications = tooling.publications
    assert isinstance(publications, MemoryPublications)
    (publication,) = publications.current_items.values()
    large = identity_mappings_document(
        tuple(
            IdentityMappingEvidence(
                source_namespace="amc.campaign_id_string",
                source_value=f"91{index:016d}",
                target_value=f"campaign:synthetic-{index:05d}",
                mapping_version=1,
                valid_from=date(2000, 1, 1),
                valid_to=None,
            )
            for index in range(5_000)
        )
    )
    swapped = replace(
        publication,
        evidence=replace(
            publication.evidence,
            results=tuple(
                replace(result, detail=large.claim.detail)
                if result.family is PrepublicationRuleFamily.IDENTITY
                else result
                for result in publication.evidence.results
            ),
        ),
    )

    assert large.claim.count == 5_000
    assert len(large.body) > 1_000_000
    # Only the count's digits grow (79 -> 5000); the mapping list never enters the record.
    base = _record_bytes(publication)
    assert base < _record_bytes(swapped) <= base + len(str(large.claim.count)) < 16_000


def test_completed_replay_fails_closed_when_the_claim_document_is_absent() -> None:
    request, tooling, _order, _attempts = _case()
    store = tooling.identity_evidence
    assert isinstance(store, MemoryIdentityEvidence)
    run_semantic_sidecar(request, tooling)

    # Positive control: an intact document replays.
    assert run_semantic_sidecar(request, tooling)[0].outcome is SemanticSidecarOutcome.PUBLISHED
    store.items.clear()
    with pytest.raises(InvariantError) as refused:
        run_semantic_sidecar(request, tooling)

    assert str(refused.value.code) == "ADLY-IDENTITY_EVIDENCE-0002"


def test_completed_replay_fails_closed_when_the_claim_document_bytes_differ() -> None:
    request, tooling, _order, _attempts = _case()
    store = tooling.identity_evidence
    assert isinstance(store, MemoryIdentityEvidence)
    run_semantic_sidecar(request, tooling)
    # Positive control: an intact document replays.
    assert run_semantic_sidecar(request, tooling)[0].outcome is SemanticSidecarOutcome.PUBLISHED
    (digest,) = store.items
    store.items[digest] = store.items[digest].replace(b'"mapping_version":', b'"mapping_version": ')

    with pytest.raises(InvariantError) as refused:
        run_semantic_sidecar(request, tooling)

    assert str(refused.value.code) == "ADLY-IDENTITY_EVIDENCE-0003"


def test_a_claim_key_holding_other_bytes_refuses_before_any_candidate_object() -> None:
    # Positive control: an independent run of the same delivery publishes and names its digests.
    probe_request, probe_tooling, _probe_order, _probe_attempts = _case()
    assert run_semantic_sidecar(probe_request, probe_tooling)[0].outcome is (
        SemanticSidecarOutcome.PUBLISHED
    )
    probe_store = probe_tooling.identity_evidence
    assert isinstance(probe_store, MemoryIdentityEvidence)
    assert probe_store.items
    # Pre-seed every digest this delivery will name with other bytes.
    request, tooling, order, _attempts = _case()
    store = MemoryIdentityEvidence(order)
    store.items = dict.fromkeys(probe_store.items, b"{}")
    tooling = replace(tooling, identity_evidence=store)

    with pytest.raises(InvariantError) as refused:
        run_semantic_sidecar(request, tooling)

    # The claim-check write refuses, before any candidate or successful object or commit.
    assert str(refused.value.code) == "ADLY-IDENTITY_EVIDENCE-0004"
    assert order == ["identity_evidence"]
    assert set(store.items.values()) == {b"{}"}


def test_a_claim_without_its_prepared_document_refuses_before_any_candidate_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Positive control: the intact vendor control pair writes both documents, then publishes.
    request, tooling, order, _attempts = _vendor_case("vendor-control-total-consistent")
    tooling = replace(tooling, identity_evidence=MemoryIdentityEvidence(order))
    results = run_semantic_sidecar(request, tooling)
    assert {result.outcome for result in results} == {SemanticSidecarOutcome.PUBLISHED}
    assert order.count("identity_evidence") >= 2
    assert "candidate" in order

    # The pair's merged documents lose one: its claim is still named by both candidates.
    original = semantic_sidecar_module._merged_documents  # pyright: ignore[reportPrivateUsage]

    def drop_last(
        *groups: tuple[IdentityMappingsDocument, ...],
    ) -> tuple[IdentityMappingsDocument, ...]:
        return original(*groups)[:-1]

    monkeypatch.setattr(semantic_sidecar_module, "_merged_documents", drop_last)
    request, tooling, order, _attempts = _vendor_case("vendor-control-total-consistent")
    store = MemoryIdentityEvidence(order)
    tooling = replace(tooling, identity_evidence=store)

    with pytest.raises(InvariantError) as refused:
        run_semantic_sidecar(request, tooling)

    assert str(refused.value.code) == "ADLY-IDENTITY_EVIDENCE-0005"
    assert "candidate" not in order
    assert "commit_group" not in order


def test_reprocess_acceptance_refuses_a_candidate_whose_claim_document_is_absent() -> None:
    request, tooling, _order, attempts = _case()
    results = run_semantic_sidecar(request, tooling)
    sources = tooling.source_reader
    canonical = tooling.canonical_writer
    publications = tooling.publications
    store = tooling.identity_evidence
    assert isinstance(sources, MemorySources)
    assert isinstance(canonical, MemoryCanonical)
    assert isinstance(publications, MemoryPublications)
    assert isinstance(store, MemoryIdentityEvidence)
    storage = ReplicatedSemanticStorage(
        source=cast("ReplicatedSourceAlignedStorage", sources),
        canonical=cast("ReplicatedCanonicalStorage", canonical),
        identity_evidence=store,
    )

    def attest() -> ReprocessAcceptanceResult:
        return attest_reprocess(
            ReprocessAcceptanceRequest(
                outcomes=results, storage=storage, attempts=attempts, publications=publications
            )
        )

    assert attest().accepted  # positive control
    store.items.clear()
    refused = attest()

    assert not refused.accepted
    assert refused.reason == ("identity claim-check evidence is absent or differs from its claim.")


def test_replay_of_a_pre_amendment_inline_record_refuses_before_any_identity_read() -> None:
    request, tooling, _order, _attempts = _case()
    store = tooling.identity_evidence
    publications = tooling.publications
    assert isinstance(store, MemoryIdentityEvidence)
    assert isinstance(publications, MemoryPublications)
    run_semantic_sidecar(request, tooling)
    reads: list[IdentityMappingsClaim] = []
    original_read = store.read

    def counting_read(claim: IdentityMappingsClaim) -> tuple[dict[str, object], ...]:
        reads.append(claim)
        return original_read(claim)

    store.read = counting_read  # type: ignore[method-assign]
    # Positive control: the claim-form record replays and its document is read once.
    assert run_semantic_sidecar(request, tooling)[0].outcome is SemanticSidecarOutcome.PUBLISHED
    assert len(reads) == 1
    reads.clear()
    # The same CURRENT as a pre-amendment record: the identity detail is the inline list.
    ((key, current),) = publications.current_items.items()
    publications.current_items[key] = replace(
        current,
        evidence=replace(
            current.evidence,
            results=tuple(
                replace(
                    result, detail='Governed identity mappings selected: [{"mapping_version":2}]'
                )
                if result.family is PrepublicationRuleFamily.IDENTITY
                else result
                for result in current.evidence.results
            ),
        ),
    )

    with pytest.raises(InvariantError) as refused:
        run_semantic_sidecar(request, tooling)

    assert str(refused.value.code) == "ADLY-SEMANTIC_SIDECAR-0001"
    assert reads == []
