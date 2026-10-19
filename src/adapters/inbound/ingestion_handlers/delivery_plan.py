# SPDX-License-Identifier: MIT-0
"""Phase-1 delivery planning: composition, canonical roster, feasibility, acknowledgment.

Pure adapter logic behind ``load.py`` (write-discipline §§2.1, 2.5, 3, 5.3, 10). Nothing here
talks to AWS or reads configuration: the handler resolves its settings and passes them in, and
every function here computes over typed inputs only.

* ``projection_composition`` maps the §2.5 handoff tag to ``inline`` or ``queued``.
* ``prepared_roster`` builds the pre-authority roster the §2.1 feasibility callback proves,
  substituting the constructor-published maximum encoded length for every identity minted only
  after lease acquisition; ``published_roster`` builds the exact roster from published receipts.
  Both measure ``M(u)`` through the one §6 packer, never a preflight-only count.
* ``require_feasible`` applies the clean-attempt formula and the §11 bounds before mutation.

The closed §5.3 acknowledgment classifier lives in ``domain.operations.source_delivery``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from adapters.inbound.ingestion_handlers.inline_run import ServablePointerTarget
from adapters.jobs.ingest_delivery.semantic_sidecar import (
    RULE_SET_VERSION,
    PreparedProjectionPartition,
    PreparedProjectionPlan,
    SemanticSidecarResult,
)
from adapters.outbound.athena.semantic_storage import (
    PhysicalRecord,
    UnitWorkload,
    canonical_unit_records,
    source_records,
)
from adapters.outbound.athena.table_attempt_statements import (
    ComposeUnitStatementsRequest,
    UnitStatementComposition,
    compose_unit_statements,
)
from adapters.outbound.s3.semantic_storage import (
    ProjectionUnitSource,
    canonical_object_bytes,
    canonical_object_key,
    projection_unit_source,
)
from domain.operations.execution_graph_dispatch import (
    PROVIDER_CEILING_BYTES,
    provider_accounted_bytes,
)
from domain.operations.projection_feasibility import (
    FeasibleProjection,
    ProjectionFeasibilityRefusal,
    ProjectionFeasibilityRequest,
    UnitMergeMeasurement,
    validate_projection_feasibility,
)
from domain.operations.projection_handoff import RESOLVER_CONTROLS
from domain.operations.statement_protocol import MAX_STATEMENT_BYTES
from domain.operations.write_plan import (
    CanonicalObjectEntry,
    CanonicalUnitEntry,
)
from domain.semantics.canonical_compiler import canonical_checksum_digest
from domain.shared_kernel.errors import ErrorCode, ValidationError
from ports.outbound.canonical import CanonicalDestination

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime

    from adapters.config import ProjectionHandoffState, Stage1DeliverySettings
    from adapters.jobs.ingest_delivery.semantic_sidecar import SemanticSidecarReceipt
    from adapters.outbound.athena.semantic_storage import AthenaSemanticTarget
    from adapters.outbound.v2_storage_catalog import V2StorageCatalog, V2Table
    from domain.semantics.source_aligned import SourceAlignedPublication
    from ports.outbound.canonical import CanonicalMaterializeRequest

__all__ = [
    "FEASIBILITY_REFUSED",
    "POST_AUTHORITY_INFEASIBLE",
    "SOURCE_OBJECT_FENCING_TOKEN",
    "TABLE_KIND_CANDIDATE",
    "TABLE_KIND_CURRENT",
    "TABLE_KIND_SOURCE",
    "DeliveryRoster",
    "FeasibilityCheck",
    "PlannedUnit",
    "PreparedRosterRequest",
    "ProjectionComposition",
    "PublishedRosterRequest",
    "prepared_roster",
    "projection_composition",
    "published_roster",
    "require_feasible",
]

FEASIBILITY_REFUSED: Final = ErrorCode("STAGE1_DELIVERY", 1)
"""The deterministic pre-authority refusal (§2.1): nothing has been mutated."""
POST_AUTHORITY_INFEASIBLE: Final = ErrorCode("STAGE1_DELIVERY", 5)
"""The published roster encodes outside the pre-authority feasibility proof (an invariant)."""

# The §5.3 acknowledgment classifier and its error code live in
# ``domain.operations.source_delivery`` (pure classification over domain forms).

# The table writer's closed table-kind vocabulary (§3 canonical unit entry, table_writer.py).
TABLE_KIND_SOURCE: Final = "source_aligned"
TABLE_KIND_CANDIDATE: Final = "candidate"
TABLE_KIND_CURRENT: Final = "current"

SOURCE_OBJECT_FENCING_TOKEN: Final = 1
"""A source object is fenced by no partition lease; the roster member is the minimum token."""

# Conservative substitutes for identities minted only after lease acquisition (§2.1). The
# sidecar mints ``<prefix>-<32 hex>`` identities, so a same-length deterministic stand-in
# reproduces the exact encoded bytes the packer will see; the fencing token substitutes the
# widest decimal rendering a positive 64-bit token can take.
_MINTED_HEX_DIGITS: Final = 32
_PUBLICATION_ID_PREFIX: Final = "publication-"
_ATTEMPT_ID_PREFIX: Final = "attempt-"
_CONSERVATIVE_FENCING_TOKEN: Final = 2**63 - 1
_CONSERVATIVE_PUBLISHER: Final = "canonical-curation"

_UNPROVED_ORDINAL: Final = 0


# --- Composition ----------------------------------------------------------------------------------


class ProjectionComposition(StrEnum):
    """The §2.5 resolver's projection composition: inline writer or queued S3-only authority."""

    INLINE = "inline"
    QUEUED = "queued"


def projection_composition(state: ProjectionHandoffState) -> ProjectionComposition:
    """Derive the projection composition from the one typed handoff tag (§2.5 resolver)."""
    return ProjectionComposition(RESOLVER_CONTROLS[state.value].projection_composition)


# --- Roster ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannedUnit:
    """One physical-table unit: its canonical entry, exact workload, and packed statements."""

    entry: CanonicalUnitEntry
    workload: UnitWorkload
    composition: UnitStatementComposition
    servable: tuple[ServablePointerTarget, ...]

    @property
    def measurement(self) -> UnitMergeMeasurement:
        """The packer's ``M(u)`` for this unit."""
        return UnitMergeMeasurement(self.entry.unit_key, self.composition.merge_count)


@dataclass(frozen=True, slots=True)
class DeliveryRoster:
    """The complete unit roster in canonical (ascending ``unit_key`` byte) order."""

    units: tuple[PlannedUnit, ...]

    @property
    def entries(self) -> tuple[CanonicalUnitEntry, ...]:
        """The canonical unit entries the intent persists."""
        return tuple(unit.entry for unit in self.units)

    @property
    def measurements(self) -> tuple[UnitMergeMeasurement, ...]:
        """The packer measurements beside every entry, in roster order."""
        return tuple(unit.measurement for unit in self.units)


@dataclass(frozen=True, slots=True)
class _RosterObject:
    """One immutable object joining one unit, with the records the packer measures."""

    unit_key: str
    table_kind: str
    table: V2Table
    database: str
    entry: CanonicalObjectEntry
    records: tuple[PhysicalRecord, ...]
    servable: ServablePointerTarget | None = None


@dataclass(frozen=True, slots=True)
class PreparedRosterRequest:
    """Pre-authority roster inputs: the prepared plan beside the physical target.

    ``max_statement_bytes`` is the §11 ``athenaMaxStatementBytes`` the §6 packer measures
    ``M(u)`` against; the delivery entry point passes its resolved setting.
    """

    plan: PreparedProjectionPlan
    target: AthenaSemanticTarget
    catalog: V2StorageCatalog
    now: datetime
    max_statement_bytes: int = MAX_STATEMENT_BYTES


@dataclass(frozen=True, slots=True)
class PublishedRosterRequest:
    """Post-authority roster inputs: the published receipts beside the physical target.

    ``max_statement_bytes`` must be the value the pre-authority roster used.
    """

    results: tuple[SemanticSidecarResult, ...]
    target: AthenaSemanticTarget
    catalog: V2StorageCatalog
    max_statement_bytes: int = MAX_STATEMENT_BYTES


def _placeholder(prefix: str, partition_identity: str, destination: str) -> str:
    digest = hashlib.sha256(f"{destination}|{partition_identity}".encode()).hexdigest()
    return f"{prefix}{digest[:_MINTED_HEX_DIGITS]}"


def _source_object(
    publication: SourceAlignedPublication,
    target: AthenaSemanticTarget,
    catalog: V2StorageCatalog,
) -> _RosterObject:
    table = catalog.source_for_interpreter(publication.rowset.interpreter.value)
    reference = publication.reference.value
    return _RosterObject(
        unit_key=f"{target.source_database}.{table.name}",
        table_kind=TABLE_KIND_SOURCE,
        table=table,
        database=target.source_database,
        entry=CanonicalObjectEntry(
            kind="source",
            reference=reference,
            content_checksum=canonical_checksum_digest(publication.content_checksum),
            publication_id=reference,
            partition_key=None,
            fencing_token=SOURCE_OBJECT_FENCING_TOKEN,
        ),
        records=source_records(publication, table),
    )


@dataclass(frozen=True, slots=True)
class _CanonicalShape:
    """The physical placement one canonical destination resolves to."""

    destination: CanonicalDestination
    table_kind: str
    database: str
    current: bool


def _canonical_shape(
    destination: CanonicalDestination, target: AthenaSemanticTarget
) -> _CanonicalShape:
    if destination is CanonicalDestination.SUCCESSFUL:
        return _CanonicalShape(destination, TABLE_KIND_CURRENT, target.current_database, True)
    return _CanonicalShape(destination, TABLE_KIND_CANDIDATE, target.candidate_database, False)


def _canonical_object(
    source: ProjectionUnitSource,
    dataset: str,
    request: PreparedRosterRequest | PublishedRosterRequest,
) -> _RosterObject:
    shape = _canonical_shape(CanonicalDestination(source.destination), request.target)
    table = request.catalog.canonical(dataset, successful=shape.current)
    return _RosterObject(
        unit_key=f"{shape.database}.{table.name}",
        table_kind=shape.table_kind,
        table=table,
        database=shape.database,
        entry=CanonicalObjectEntry(
            kind="canonical",
            reference=canonical_object_key(source.destination, dataset, source.publication_id),
            content_checksum=canonical_checksum_digest(source.content_checksum),
            publication_id=source.publication_id,
            partition_key=source.partition_key if shape.current else None,
            fencing_token=source.fencing_token,
        ),
        records=canonical_unit_records(source, table),
        servable=(
            ServablePointerTarget(source.partition_key, source.publication_id, source.fencing_token)
            if shape.current
            else None
        ),
    )


def _conservative_source(
    partition: PreparedProjectionPartition,
    destination: CanonicalDestination,
    now: datetime,
) -> ProjectionUnitSource:
    """Stand in for a canonical object whose identities are minted only after the lease."""
    return ProjectionUnitSource(
        destination=destination.value,
        publication_id=_placeholder(
            _PUBLICATION_ID_PREFIX, partition.partition_identity, destination.value
        ),
        attempt_id=_placeholder(
            _ATTEMPT_ID_PREFIX, partition.partition_identity, destination.value
        ),
        partition_key=partition.partition_identity,
        publisher=_CONSERVATIVE_PUBLISHER,
        fencing_token=_CONSERVATIVE_FENCING_TOKEN,
        source_publications=tuple(
            publication.reference.value for publication in partition.source_publications
        ),
        source_revision="",
        content_checksum=partition.content_checksum,
        rule_set_version=RULE_SET_VERSION,
        created_at=now,
        rows=partition.canonical_rows,
    )


def prepared_roster(request: PreparedRosterRequest) -> DeliveryRoster:
    """Build the §2.1 pre-authority roster the feasibility callback proves.

    Every source object joins a source unit (rule 3); a canonical partition with no business
    rows joins no candidate or current unit. Publication, attempt, and fencing identities are
    conservative same-length substitutes because the lease that mints them is not yet held.
    """
    objects = [
        _source_object(publication, request.target, request.catalog)
        for publication in request.plan.source_publications
    ]
    for partition in request.plan.partitions:
        if not partition.canonical_rows:
            continue
        for destination in (CanonicalDestination.CANDIDATE, CanonicalDestination.SUCCESSFUL):
            objects.append(
                _canonical_object(
                    _conservative_source(partition, destination, request.now),
                    partition.dataset,
                    request,
                )
            )
    return _shape(objects, request.target.batch_workgroup, request.max_statement_bytes)


def _published_canonical(
    materialization: CanonicalMaterializeRequest, request: PublishedRosterRequest
) -> _RosterObject:
    # The same decode the table writer applies to the stored object, so the roster's merge
    # count and checksum are exactly what phase 2 will recompute from S3.
    source = projection_unit_source(canonical_object_bytes(materialization))
    dataset = materialization.publication.partition.scope.dataset.value
    return _canonical_object(source, dataset, request)


def _receipt_objects(
    receipt: SemanticSidecarReceipt, request: PublishedRosterRequest
) -> list[_RosterObject]:
    objects = [
        _source_object(publication, request.target, request.catalog)
        for publication in receipt.source_publications
    ]
    for materialization in (receipt.candidate, receipt.successful):
        if materialization is None or not materialization.rows:
            continue
        objects.append(_published_canonical(materialization, request))
    return objects


def published_roster(request: PublishedRosterRequest) -> DeliveryRoster:
    """Build the exact canonical roster from published receipts (§2.1 rules 3 and 6)."""
    objects = [
        item
        for result in request.results
        for receipt in result.receipts
        for item in _receipt_objects(receipt, request)
    ]
    return _shape(objects, request.target.batch_workgroup, request.max_statement_bytes)


def _shape(
    objects: list[_RosterObject], workgroup: str, max_statement_bytes: int
) -> DeliveryRoster:
    """Coalesce objects into canonical units and measure each through the §6 packer."""
    by_unit: dict[str, dict[tuple[bytes, bytes], _RosterObject]] = {}
    for item in objects:
        members = by_unit.setdefault(item.unit_key, {})
        retained = members.get(item.entry.object_key)
        if retained is None:
            members[item.entry.object_key] = item
            continue
        if retained.entry != item.entry:
            raise ValueError("one immutable projection identity names conflicting evidence.")
    units: list[PlannedUnit] = []
    for unit_key in sorted(by_unit, key=lambda key: key.encode("utf-8")):
        members = tuple(by_unit[unit_key][key] for key in sorted(by_unit[unit_key]))
        kinds = {member.table_kind for member in members}
        tables = {member.table.name for member in members}
        if len(kinds) != 1 or len(tables) != 1:
            raise ValueError("one physical write unit cannot mix table kinds or schemas.")
        workload = UnitWorkload(
            table=members[0].table,
            database=members[0].database,
            records_by_identity=tuple(
                (member.entry.publication_id, member.records) for member in members
            ),
        )
        composition = compose_unit_statements(
            ComposeUnitStatementsRequest(
                workload=workload, workgroup=workgroup, max_statement_bytes=max_statement_bytes
            )
        )
        units.append(
            PlannedUnit(
                entry=CanonicalUnitEntry(
                    unit_key=unit_key,
                    table_kind=members[0].table_kind,
                    merge_count=composition.merge_count,
                    objects=tuple(member.entry for member in members),
                ),
                workload=workload,
                composition=composition,
                servable=tuple(
                    member.servable for member in members if member.servable is not None
                ),
            )
        )
    return DeliveryRoster(tuple(units))


# --- Feasibility ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeasibilityCheck:
    """The complete roster, the exact group roster, and the §11 bounds it must fit."""

    roster: DeliveryRoster
    groups: tuple[tuple[tuple[str, str], ...], ...]
    settings: Stage1DeliverySettings
    max_units: int | None = None
    """S6 §2.4 (AGG3-10): GRAPH passes the graph fan-out bound (11); legacy keeps the §11 cap."""
    envelope: Callable[[CanonicalUnitEntry], str] | None = None
    """S6 §2.4: renders one unit's exact graph envelope, measured against the provider ceiling
    as instantiation measures it; None outside GRAPH."""


def _refuse(reason: str, description: str) -> ValidationError:
    return ValidationError(
        FEASIBILITY_REFUSED,
        f"The delivery is refused before any authority mutation: {description}",
        context={"reason": reason},
    )


def require_feasible(check: FeasibilityCheck) -> FeasibleProjection:
    """Prove the §2.1 clean-attempt formula and the §11 roster bounds, or refuse typed.

    Raises :class:`ValidationError` ``FEASIBILITY_REFUSED``; the caller has mutated nothing.
    """
    settings = check.settings
    for group in check.groups:
        if len(group) > settings.max_group_members:
            raise _refuse(
                "group_members_exceed_cap",
                f"a publication group has {len(group)} members, beyond "
                f"{settings.max_group_members}.",
            )
    roster = check.roster
    max_units = settings.max_units if check.max_units is None else check.max_units
    if len(roster.units) > max_units:
        raise _refuse(
            "units_exceed_cap",
            f"the roster has {len(roster.units)} units, beyond {max_units}.",
        )
    if check.envelope is not None:
        for unit in roster.units:
            measured = provider_accounted_bytes(check.envelope(unit.entry), {})
            if measured > PROVIDER_CEILING_BYTES:
                raise _refuse(
                    "unit_envelope_oversize",
                    f"unit {unit.entry.unit_key!r} has a {measured}-byte graph envelope, beyond "
                    f"{PROVIDER_CEILING_BYTES}.",
                )
    for unit in roster.units:
        if len(unit.entry.objects) > settings.max_object_refs_per_unit:
            raise _refuse(
                "object_refs_exceed_cap",
                f"unit {unit.entry.unit_key!r} references {len(unit.entry.objects)} objects, "
                f"beyond {settings.max_object_refs_per_unit}.",
            )
        if len(unit.servable) > settings.max_pointers_per_table:
            raise _refuse(
                "pointers_exceed_cap",
                f"unit {unit.entry.unit_key!r} names {len(unit.servable)} pointers, beyond "
                f"{settings.max_pointers_per_table}.",
            )
    result = validate_projection_feasibility(
        ProjectionFeasibilityRequest(
            units=roster.entries,
            measurements=roster.measurements,
            max_accepted_attempts_per_unit=settings.max_accepted_attempts_per_unit,
            max_accepted_attempts_per_plan=settings.max_accepted_attempts_per_plan,
        )
    )
    if isinstance(result, ProjectionFeasibilityRefusal):
        raise _refuse(result.reason.value, result.description)
    return result
