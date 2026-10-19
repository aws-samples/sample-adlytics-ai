# SPDX-License-Identifier: MIT-0
"""Deterministic incoming-key, registration, and finalized-manifest planning."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from domain.curation.landing import (
    GOVERNED_MANIFEST_LAYOUT,
    LandedObject,
    Manifest,
    ManifestPrefix,
    manifest_prefix_for,
    options_hash,
)
from domain.producer.models import (
    PACK_VARIANT_OPTION,
    PERIOD_GRAIN_OPTION,
    REGISTRATION_SCHEMA_VERSION,
    ExecutionIdentity,
    GovernedQueryPack,
    IncomingObject,
    PeriodGrain,
    QueryPackSelection,
    ResultEvidence,
    ScheduleKind,
    SourceRegistration,
)
from domain.producer.parameters import resolve_period_parameters
from domain.producer.periods import is_aligned
from domain.producer.validation import WINDOW_END_OPTION, WINDOW_START_OPTION
from domain.semantics.identity import SourceIdentifier


@dataclass(frozen=True, slots=True)
class PlanLandingRequest:
    """Everything known after a source result upload returns its VersionId."""

    pack: GovernedQueryPack
    execution_id: str
    workflow_id: str
    schedule_kind: ScheduleKind
    schedule_id: str | None
    source_scope: SourceIdentifier
    marketplace: str
    period_start: date
    period_end: date
    time_window_start: datetime
    time_window_end: datetime
    source_watermark: date | None
    retrieved_at: datetime
    object_version_id: str
    period_grain: PeriodGrain | None = None
    """The declared grain of a periodic pack's delivery; `None` for a daily pack (ADR-110)."""
    commerce_identities: Mapping[str, str] = field(default_factory=dict[str, str])
    """Namespace to value for this registration's marketplace, resolved by the adapter from
    `contracts/shared/commerce-identities.yaml` (`resolve_commerce_identities`). The planner
    reads no file; it copies each parameter's value into the option set."""

    def __post_init__(self) -> None:
        self._check_window()
        self._check_period_grain()
        missing = sorted(
            {
                parameter.commerce_identity
                for parameter in self.pack.input_parameters
                if parameter.commerce_identity is not None
                and parameter.commerce_identity not in self.commerce_identities
            }
        )
        if missing:
            raise ValueError(
                f"{self.pack.pack_id}: no pinned value for {', '.join(missing)}, so its "
                "parameters can't be filled."
            )

    def _check_period_grain(self) -> None:
        policy = self.pack.period_policy
        if policy is None:
            if self.period_grain is not None:
                raise ValueError(f"{self.pack.pack_id} is a daily pack and takes no period_grain.")
            return
        if self.period_grain is None or self.period_grain not in policy.grains:
            declared = ", ".join(grain.value for grain in policy.grains)
            raise ValueError(f"{self.pack.pack_id} requires period_grain, one of {declared}.")
        if not is_aligned(self.period_grain, (self.period_start, self.period_end)):
            raise ValueError(
                f"{self.period_start}..{self.period_end} is not one whole "
                f"{self.period_grain.value}."
            )

    def _check_window(self) -> None:
        for label, value in (
            ("time_window_start", self.time_window_start),
            ("time_window_end", self.time_window_end),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{label} must be timezone-aware.")
            if value.utcoffset() != timedelta(0):
                raise ValueError(f"{label} must use an explicit UTC offset.")
        if self.time_window_end <= self.time_window_start:
            raise ValueError("time_window_end must be later than time_window_start.")
        if self.period_start != self.time_window_start.astimezone(UTC).date():
            raise ValueError("period_start must equal the UTC date of time_window_start.")
        inclusive_end = self.time_window_end.astimezone(UTC) - timedelta(microseconds=1)
        if self.period_end != inclusive_end.date():
            raise ValueError(
                "period_end must be the inclusive UTC day before the half-open time_window_end."
            )


def _amc_timestamp(value: datetime) -> str:
    """Render the bare AMC timestamp paired with timeWindowTimeZone=UTC."""
    normalized = value.astimezone(UTC).replace(microsecond=0)
    return normalized.strftime("%Y-%m-%dT%H:%M:%S")


def plan_landing(request: PlanLandingRequest) -> SourceRegistration:
    """Build the exact registration the producer writes after its result."""
    pack = request.pack
    execution = ExecutionIdentity(
        execution_id=request.execution_id,
        workflow_id=request.workflow_id,
        schedule_kind=request.schedule_kind,
        schedule_id=request.schedule_id,
    )
    prefix = f"incoming/{pack.source_system}/{pack.source_dataset}/{execution.execution_id}/"
    options: dict[str, object] = {
        **dict(pack.execution_options),
        WINDOW_START_OPTION: _amc_timestamp(request.time_window_start),
        WINDOW_END_OPTION: _amc_timestamp(request.time_window_end),
        **(
            {PERIOD_GRAIN_OPTION: request.period_grain.value}
            if request.period_grain is not None
            else {}
        ),
        # A declared variant's delivery names it, so its rows are a series of their own and the
        # load can check it is the variant this period expects (plan D9, D10).
        **({PACK_VARIANT_OPTION: pack.variant_id} if pack.variant_id is not None else {}),
        **{
            parameter.name: request.commerce_identities[parameter.commerce_identity]
            for parameter in pack.input_parameters
            if parameter.commerce_identity is not None
        },
        # A cohort vintage's `as_of_month` comes from the period itself (ADR-104), so each
        # vintage is its own option set and overlapping vintages never trip the overlap check.
        **resolve_period_parameters(pack, (request.period_start, request.period_end)),
    }
    return SourceRegistration(
        schema_version=REGISTRATION_SCHEMA_VERSION,
        source_system=pack.source_system,
        source_dataset=pack.source_dataset,
        source_scope=request.source_scope,
        marketplace=request.marketplace,
        period_start=request.period_start,
        period_end=request.period_end,
        source_watermark=request.source_watermark,
        retrieved_at=request.retrieved_at,
        option_set=options,
        execution=execution,
        query_pack=QueryPackSelection(
            pack_id=pack.pack_id,
            version=pack.pack_version,
            query_digest=pack.query_digest,
        ),
        object=IncomingObject(
            key=f"{prefix}result.csv",
            version_id=request.object_version_id,
        ),
    )


def canonical_result_key(registration: SourceRegistration) -> str:
    """Return the governed destination key without duplicating manifest prefix logic."""
    prefix = manifest_prefix_for(
        ManifestPrefix(
            source_system=registration.source_system,
            source_dataset=registration.source_dataset,
            source_scope=registration.source_scope,
            marketplace=registration.marketplace,
            period_start=registration.period_start,
            period_end=registration.period_end,
            option_set_hash=options_hash(dict(registration.option_set)),
            retrieved_at=registration.retrieved_at,
            layout_version=GOVERNED_MANIFEST_LAYOUT,
            query_pack_id=registration.query_pack.pack_id,
        )
    )
    return f"{prefix}result.csv"


@dataclass(frozen=True, slots=True)
class PlanManifestRequest:
    """Validated exact evidence needed to create one canonical manifest."""

    registration: SourceRegistration
    pack: GovernedQueryPack
    evidence: ResultEvidence
    destination_version_id: str

    def __post_init__(self) -> None:
        if not self.destination_version_id.strip():
            raise ValueError("destination result VersionId cannot be blank.")


@dataclass(frozen=True, slots=True)
class FinalizedManifestPlan:
    """One canonical governance-bound manifest ready for serialization."""

    manifest: Manifest


def plan_manifest(request: PlanManifestRequest) -> FinalizedManifestPlan:
    """Build one canonical one-object manifest from validated immutable evidence."""
    registration = request.registration
    evidence = request.evidence
    manifest = Manifest(
        source_system=registration.source_system,
        source_dataset=registration.source_dataset,
        source_scope=registration.source_scope,
        option_set=dict(registration.option_set),
        option_set_hash=options_hash(dict(registration.option_set)),
        marketplace=registration.marketplace,
        period_start=registration.period_start,
        period_end=registration.period_end,
        source_watermark=registration.source_watermark,
        retrieved_at=registration.retrieved_at,
        declared_columns=evidence.declared_columns,
        objects=(
            LandedObject(
                name="result.csv",
                byte_count=evidence.byte_count,
                checksum=evidence.checksum,
                version_id=request.destination_version_id,
            ),
        ),
        governance=request.pack.governance,
        layout_version=GOVERNED_MANIFEST_LAYOUT,
    )
    return FinalizedManifestPlan(manifest=manifest)
