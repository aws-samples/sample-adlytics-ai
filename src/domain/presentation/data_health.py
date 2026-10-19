# SPDX-License-Identifier: MIT-0
"""Pure data-health condition, recovery, and artifact reduction."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise

from domain.narrative.shaped_response import UnitKind, Units
from domain.shared_kernel.provenance import Suppression
from domain.shared_kernel.values import DateRange, Grain

DATA_HEALTH_SOURCE_LABEL = "Amazon Marketing Cloud"
DATA_HEALTH_POPULATION_LABEL = "Sponsored-ad exposed users"
DATA_HEALTH_LIMITATIONS = (
    "Reach is disclosed only at its exact source grain and is never reaggregated.",
    "Distinct reach is not added across dates, campaigns, channels, or populations.",
    "Average-frequency rows are neither summed nor averaged.",
    "A source population is never substituted for another population.",
    "Sponsored-ad reach does not establish DSP or cross-channel reach.",
)


type DataHealthNumber = int | Decimal
type EvidenceDimensionValue = str | date


class DataHealthState(StrEnum):
    """Closed top-level health states."""

    HEALTHY = "healthy"
    QUALIFIED = "qualified"
    INSUFFICIENT = "insufficient"
    NO_COVERAGE = "no_coverage"


class DataHealthCheck(StrEnum):
    """The two required checks in dependency order."""

    REACH_USERS = "reach_users"
    AVG_FREQUENCY = "avg_frequency"


class DataHealthBaseCondition(StrEnum):
    """Exhaustive base condition for one check."""

    AVAILABLE = "available"
    EMPTY_COVERED = "empty_covered"
    SUPPRESSED = "suppressed"
    TRUNCATED = "truncated"
    NEVER_LOADED = "never_loaded"
    NO_CURRENT_PUBLICATION = "no_current_publication"
    COVERAGE_HOLE = "coverage_hole"
    UNSUPPORTED_POPULATION = "unsupported_population"
    QUERY_REFUSED = "query_refused"
    BLOCKED_BY_REACH = "blocked_by_reach"


class DataHealthFreshnessQualifier(StrEnum):
    """Bounded qualifiers on otherwise usable serving evidence."""

    STALE = "stale"
    LATEST_ATTEMPT_REJECTED = "latest_attempt_rejected"
    QUALIFIED_DQ = "qualified_dq"


class DataHealthRecoveryAction(StrEnum):
    """Closed enabled actions for blocked checks."""

    CONTACT_WORKSPACE_DATA_OWNER = "contact_workspace_data_owner"
    CHOOSE_COVERED_PERIOD = "choose_covered_period"
    REVIEW_SUPPORTED_POPULATION = "review_supported_population"
    REVIEW_SUPPORTED_REQUEST = "review_supported_request"
    INSPECT_REACH_CONDITION = "inspect_reach_condition"
    NARROW_EXACT_SCOPE = "narrow_exact_scope"


class DataHealthNoSelfServiceReason(StrEnum):
    """Closed reasons that deliberately expose no action."""

    SOURCE_PRIVACY_THRESHOLD = "source_privacy_threshold"
    COVERED_NO_ROWS = "covered_no_rows"


BASE_CONDITION_PRECEDENCE = (
    DataHealthBaseCondition.UNSUPPORTED_POPULATION,
    DataHealthBaseCondition.NEVER_LOADED,
    DataHealthBaseCondition.NO_CURRENT_PUBLICATION,
    DataHealthBaseCondition.COVERAGE_HOLE,
    DataHealthBaseCondition.SUPPRESSED,
    DataHealthBaseCondition.TRUNCATED,
    DataHealthBaseCondition.QUERY_REFUSED,
    DataHealthBaseCondition.BLOCKED_BY_REACH,
    DataHealthBaseCondition.EMPTY_COVERED,
    DataHealthBaseCondition.AVAILABLE,
)
CHECK_PRECEDENCE = (DataHealthCheck.REACH_USERS, DataHealthCheck.AVG_FREQUENCY)
FRESHNESS_QUALIFIER_ORDER = tuple(DataHealthFreshnessQualifier)
_CONDITION_RANK = {condition: index for index, condition in enumerate(BASE_CONDITION_PRECEDENCE)}
_CHECK_RANK = {check: index for index, check in enumerate(CHECK_PRECEDENCE)}
_NO_COVERAGE_CONDITIONS = frozenset(
    {
        DataHealthBaseCondition.NEVER_LOADED,
        DataHealthBaseCondition.NO_CURRENT_PUBLICATION,
    }
)
_CONSEQUENCE = {
    DataHealthBaseCondition.AVAILABLE: "Evidence is usable at the exact source grain.",
    DataHealthBaseCondition.EMPTY_COVERED: (
        "The covered source slice returned no rows, so the check is unavailable."
    ),
    DataHealthBaseCondition.SUPPRESSED: (
        "A source privacy threshold withholds the check from use."
    ),
    DataHealthBaseCondition.TRUNCATED: ("Partial source rows cannot establish the complete check."),
    DataHealthBaseCondition.NEVER_LOADED: (
        "No serving load has ever established the required source evidence."
    ),
    DataHealthBaseCondition.NO_CURRENT_PUBLICATION: (
        "No attested current publication covers the required source evidence."
    ),
    DataHealthBaseCondition.COVERAGE_HOLE: (
        "The requested period contains an uncovered source interval."
    ),
    DataHealthBaseCondition.UNSUPPORTED_POPULATION: (
        "The requested population is outside the governed source capability."
    ),
    DataHealthBaseCondition.QUERY_REFUSED: (
        "The exact governed request was refused and no result is usable."
    ),
    DataHealthBaseCondition.BLOCKED_BY_REACH: (
        "Average frequency is unavailable without compatible nonzero reach."
    ),
}
_SAFE_DIMENSION = re.compile(r"^[a-z][a-z0-9_]*$")
_FORBIDDEN_DIMENSION_PARTS = (
    "attempt",
    "checksum",
    "publication",
    "resource",
    "table",
)


def _nonblank(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} cannot be blank.")


def _safe_dimension_name(value: str) -> None:
    if (
        not _SAFE_DIMENSION.fullmatch(value)
        or value.endswith("_key")
        or value.endswith("_id")
        or any(part in value for part in _FORBIDDEN_DIMENSION_PARTS)
    ):
        raise ValueError("data-health evidence dimensions must use safe public field names.")


def _number(value: object) -> DataHealthNumber:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise TypeError("data-health evidence values must be integers or Decimals.")
    if value < 0:
        raise ValueError("data-health evidence values cannot be negative.")
    return value


def _ordered_ranges(values: tuple[DateRange, ...], field_name: str) -> None:
    if tuple(sorted(values, key=lambda item: (item.start, item.end))) != values:
        raise ValueError(f"{field_name} must be chronologically ordered.")
    if any(left.overlaps(right) for left, right in pairwise(values)):
        raise ValueError(f"{field_name} cannot overlap.")


def reduce_base_condition(
    candidates: tuple[DataHealthBaseCondition, ...],
) -> DataHealthBaseCondition:
    """Select exactly one condition by the documented total precedence."""
    if not candidates:
        raise ValueError("data-health condition reduction requires at least one candidate.")
    if len(set(candidates)) != len(candidates):
        raise ValueError("data-health condition candidates must be unique.")
    return min(candidates, key=_CONDITION_RANK.__getitem__)


reduce_data_health_condition = reduce_base_condition


@dataclass(frozen=True, slots=True)
class DataHealthRecovery:
    """One enabled action or one explicit no-self-service disposition."""

    action_id: DataHealthRecoveryAction | None = None
    no_self_service_reason: DataHealthNoSelfServiceReason | None = None

    def __post_init__(self) -> None:
        if (self.action_id is None) == (self.no_self_service_reason is None):
            raise ValueError("data-health recovery requires exactly one tagged alternative.")


@dataclass(frozen=True, slots=True)
class DataHealthNarrativeActionCandidate:
    """The sole declared action eligible for a narrative next-step task."""

    check: DataHealthCheck
    action_id: DataHealthRecoveryAction


@dataclass(frozen=True, slots=True)
class DataHealthEvidenceRow:
    """One exact-source row with an opaque local evidence reference."""

    evidence_ref: str
    dimensions: tuple[tuple[str, EvidenceDimensionValue], ...]
    value: DataHealthNumber
    units: Units
    reach_denominator_ref: str | None = None

    def __post_init__(self) -> None:
        _nonblank(self.evidence_ref, "data-health evidence reference")
        if not self.dimensions:
            raise ValueError("data-health evidence row requires exact source dimensions.")
        names = tuple(name for name, _value in self.dimensions)
        if len(set(names)) != len(names):
            raise ValueError("data-health evidence row cannot repeat a dimension.")
        for name, value in self.dimensions:
            _safe_dimension_name(name)
            if isinstance(value, str):
                _nonblank(value, "data-health evidence dimension value")
        _number(self.value)
        if self.reach_denominator_ref is not None:
            _nonblank(self.reach_denominator_ref, "reach denominator reference")


@dataclass(frozen=True, slots=True)
class DataHealthServingProvenance:
    """Public serving scope plus opaque evidence custody, without resource identity."""

    source: str
    population: str
    evidence_digest: str
    inspection_attested: bool

    def __post_init__(self) -> None:
        _nonblank(self.source, "data-health source")
        _nonblank(self.population, "data-health population")
        _nonblank(self.evidence_digest, "data-health evidence digest")
        if not self.inspection_attested:
            raise ValueError("data-health artifacts require attested serving inspection evidence.")


@dataclass(frozen=True, slots=True)
class DataHealthCheckRequest:
    """Typed inspection and shaped-answer facts for one check reduction."""

    check: DataHealthCheck
    source: str
    population: str
    grain: Grain
    units: Units
    requested_period: DateRange
    condition_candidates: tuple[DataHealthBaseCondition, ...]
    actual_coverage: tuple[DateRange, ...]
    coverage_holes: tuple[DateRange, ...]
    freshness_qualifiers: tuple[DataHealthFreshnessQualifier, ...]
    suppression: Suppression
    dq: tuple[str, ...]
    row_count: int
    evidence_rows: tuple[DataHealthEvidenceRow, ...]
    loaded_at: datetime | None
    truncated: bool
    complete_recovery_period: DateRange | None = None
    can_narrow_exact_scope: bool = False
    reach_slice_compatible: bool = True

    def __post_init__(self) -> None:
        _validate_check_common(self)
        condition = reduce_base_condition(self.condition_candidates)
        if DataHealthBaseCondition.BLOCKED_BY_REACH in self.condition_candidates:
            raise ValueError("blocked_by_reach is derived only after the reach check.")
        if self.check is DataHealthCheck.REACH_USERS and not self.reach_slice_compatible:
            raise ValueError("reach-slice compatibility applies only to average frequency.")
        _validate_condition_metadata(self, condition)
        if self.complete_recovery_period is not None and condition is not (
            DataHealthBaseCondition.COVERAGE_HOLE
        ):
            raise ValueError("a covered recovery period applies only to a coverage hole.")
        if self.can_narrow_exact_scope and condition is not DataHealthBaseCondition.TRUNCATED:
            raise ValueError("exact-scope narrowing applies only to truncation.")


@dataclass(frozen=True, slots=True)
class DataHealthCheckResult:
    """One exact check condition with evidence, consequence, and recovery."""

    check: DataHealthCheck
    source: str
    population: str
    grain: Grain
    units: Units
    base_condition: DataHealthBaseCondition
    usable: bool
    consequence: str
    requested_period: DateRange
    actual_coverage: tuple[DateRange, ...]
    coverage_holes: tuple[DateRange, ...]
    freshness_qualifiers: tuple[DataHealthFreshnessQualifier, ...]
    suppression: Suppression
    dq: tuple[str, ...]
    row_count: int
    evidence_rows: tuple[DataHealthEvidenceRow, ...]
    loaded_at: datetime | None
    truncated: bool
    recovery: DataHealthRecovery | None

    def __post_init__(self) -> None:
        _validate_check_common(self)
        if self.usable != (self.base_condition is DataHealthBaseCondition.AVAILABLE):
            raise ValueError("only available data-health evidence is usable.")
        if self.consequence != _CONSEQUENCE[self.base_condition]:
            raise ValueError("data-health consequence must be derived from the base condition.")
        _validate_condition_metadata(self, self.base_condition)
        if not _valid_recovery(self.base_condition, self.recovery):
            raise ValueError("data-health recovery contradicts its base condition.")


@dataclass(frozen=True, slots=True)
class DataHealthDetailRequest:
    """The exact source scope and two dependency-ordered check requests."""

    period: DateRange
    serving_provenance: DataHealthServingProvenance
    reach: DataHealthCheckRequest
    frequency: DataHealthCheckRequest

    def __post_init__(self) -> None:
        if (
            self.reach.check is not DataHealthCheck.REACH_USERS
            or self.frequency.check is not DataHealthCheck.AVG_FREQUENCY
        ):
            raise ValueError("data-health detail requires reach before average frequency.")
        if (
            self.reach.requested_period != self.period
            or self.frequency.requested_period != self.period
        ):
            raise ValueError("data-health checks must use the requested page period.")
        if self.reach.grain != self.frequency.grain:
            raise ValueError("reach and average frequency require the same exact source slice.")
        for check in (self.reach, self.frequency):
            if (
                check.source != self.serving_provenance.source
                or check.population != self.serving_provenance.population
            ):
                raise ValueError("data-health checks must match the attested serving scope.")


@dataclass(frozen=True, slots=True)
class DataHealthDetailArtifact:
    """Closed data-health page artifact with no hypothesis surface."""

    state: DataHealthState
    period: DateRange
    serving_provenance: DataHealthServingProvenance
    checks: tuple[DataHealthCheckResult, ...]
    limitations: tuple[str, ...]
    narrative_action_candidate: DataHealthNarrativeActionCandidate | None

    def __post_init__(self) -> None:
        if tuple(check.check for check in self.checks) != CHECK_PRECEDENCE:
            raise ValueError("data-health artifact must preserve reach-before-frequency order.")
        if any(check.requested_period != self.period for check in self.checks):
            raise ValueError("data-health artifact checks must use its page period.")
        if any(
            check.source != self.serving_provenance.source
            or check.population != self.serving_provenance.population
            for check in self.checks
        ):
            raise ValueError("data-health artifact checks contradict serving provenance.")
        if self.limitations != DATA_HEALTH_LIMITATIONS:
            raise ValueError("data-health limitations are fixed domain policy.")
        if self.state is not reduce_data_health_state(self.checks):
            raise ValueError("data-health top-level state contradicts its checks.")
        if self.narrative_action_candidate != select_narrative_action(self.checks):
            raise ValueError("data-health narrative action must target the highest blocker only.")
        _validate_denominator_links(self.checks)


type _CheckEvidence = DataHealthCheckRequest | DataHealthCheckResult


def _validate_check_common(check: _CheckEvidence) -> None:
    _nonblank(check.source, "data-health check source")
    _nonblank(check.population, "data-health check population")
    expected_kind = UnitKind.COUNT if check.check is DataHealthCheck.REACH_USERS else UnitKind.RATIO
    if check.units.kind is not expected_kind:
        raise ValueError("data-health check units contradict the metric.")
    for name in check.grain.dimensions:
        _safe_dimension_name(name)
    _ordered_ranges(check.actual_coverage, "data-health actual coverage")
    _ordered_ranges(check.coverage_holes, "data-health coverage holes")
    if any(not check.requested_period.covers(item) for item in check.coverage_holes):
        raise ValueError("data-health coverage holes must fall inside the requested period.")
    if check.row_count < 0 or check.row_count != len(check.evidence_rows):
        raise ValueError("data-health row count must exactly match the disclosed rows.")
    qualifiers = check.freshness_qualifiers
    if len(set(qualifiers)) != len(qualifiers):
        raise ValueError("data-health freshness qualifiers must be unique.")
    if isinstance(check, DataHealthCheckResult) and qualifiers != tuple(
        qualifier for qualifier in FRESHNESS_QUALIFIER_ORDER if qualifier in qualifiers
    ):
        raise ValueError("data-health freshness qualifiers must preserve canonical order.")
    if len(set(check.dq)) != len(check.dq) or any(not value.strip() for value in check.dq):
        raise ValueError("data-health DQ evidence must be unique and nonblank.")
    if check.loaded_at is not None and (
        check.loaded_at.tzinfo is None or check.loaded_at.utcoffset() is None
    ):
        raise ValueError("data-health loaded time must be timezone-aware.")
    refs = tuple(row.evidence_ref for row in check.evidence_rows)
    if len(set(refs)) != len(refs):
        raise ValueError("data-health evidence references must be unique per check.")
    for row in check.evidence_rows:
        if row.units != check.units or tuple(name for name, _value in row.dimensions) != (
            check.grain.dimensions
        ):
            raise ValueError("data-health rows require the check's exact units and grain.")
        denominator_present = row.reach_denominator_ref is not None
        if denominator_present != (check.check is DataHealthCheck.AVG_FREQUENCY):
            raise ValueError("only frequency rows require one reach denominator reference.")


def _validate_available(check: _CheckEvidence) -> None:
    if (
        check.actual_coverage != (check.requested_period,)
        or check.coverage_holes
        or check.suppression.rows_suppressed
        or check.truncated
        or check.row_count == 0
        or check.loaded_at is None
    ):
        raise ValueError("available data-health evidence must be complete and represented.")
    dq_qualified = DataHealthFreshnessQualifier.QUALIFIED_DQ in check.freshness_qualifiers
    if dq_qualified != bool(check.dq):
        raise ValueError("qualified_dq must exactly represent disclosed DQ evidence.")


def _validate_empty_covered(check: _CheckEvidence) -> None:
    if check.actual_coverage != (check.requested_period,) or check.row_count != 0:
        raise ValueError("covered-empty health evidence has coverage and no rows.")


def _validate_suppressed(check: _CheckEvidence) -> None:
    if not check.suppression.rows_suppressed or check.row_count != 0:
        raise ValueError("suppressed health evidence requires typed suppression and no rows.")


def _validate_truncated(check: _CheckEvidence) -> None:
    if not check.truncated:
        raise ValueError("truncated health evidence requires its truncation flag.")


def _validate_coverage_hole(check: _CheckEvidence) -> None:
    if not check.coverage_holes:
        raise ValueError("coverage-hole health evidence requires at least one hole.")


def _validate_absent_coverage(check: _CheckEvidence) -> None:
    if check.actual_coverage or check.row_count != 0:
        raise ValueError("absent serving coverage cannot claim coverage or rows.")


def _validate_rowless_block(check: _CheckEvidence) -> None:
    if check.row_count != 0:
        raise ValueError("a blocked data-health condition cannot carry measurement rows.")


_CONDITION_VALIDATORS = {
    DataHealthBaseCondition.AVAILABLE: _validate_available,
    DataHealthBaseCondition.EMPTY_COVERED: _validate_empty_covered,
    DataHealthBaseCondition.SUPPRESSED: _validate_suppressed,
    DataHealthBaseCondition.TRUNCATED: _validate_truncated,
    DataHealthBaseCondition.NEVER_LOADED: _validate_absent_coverage,
    DataHealthBaseCondition.NO_CURRENT_PUBLICATION: _validate_absent_coverage,
    DataHealthBaseCondition.COVERAGE_HOLE: _validate_coverage_hole,
    DataHealthBaseCondition.UNSUPPORTED_POPULATION: _validate_rowless_block,
    DataHealthBaseCondition.QUERY_REFUSED: _validate_rowless_block,
    DataHealthBaseCondition.BLOCKED_BY_REACH: _validate_rowless_block,
}


def _validate_condition_metadata(
    check: _CheckEvidence,
    condition: DataHealthBaseCondition,
) -> None:
    if condition is not DataHealthBaseCondition.AVAILABLE and check.freshness_qualifiers:
        raise ValueError("freshness and DQ qualifiers apply only to usable serving evidence.")
    _CONDITION_VALIDATORS[condition](check)
    if condition is DataHealthBaseCondition.BLOCKED_BY_REACH and check.check is not (
        DataHealthCheck.AVG_FREQUENCY
    ):
        raise ValueError("only average frequency can be blocked by reach.")


def recovery_for_condition(
    condition: DataHealthBaseCondition,
    complete_coverage_period: DateRange | None = None,
    can_narrow_exact_scope: bool = False,
) -> DataHealthRecovery | None:
    """Apply the closed condition-to-recovery mapping."""
    recovery: DataHealthRecovery | None
    if condition is DataHealthBaseCondition.AVAILABLE:
        recovery = None
    elif condition in _NO_COVERAGE_CONDITIONS:
        recovery = DataHealthRecovery(
            action_id=DataHealthRecoveryAction.CONTACT_WORKSPACE_DATA_OWNER
        )
    elif condition is DataHealthBaseCondition.COVERAGE_HOLE:
        action = (
            DataHealthRecoveryAction.CHOOSE_COVERED_PERIOD
            if complete_coverage_period is not None
            else DataHealthRecoveryAction.CONTACT_WORKSPACE_DATA_OWNER
        )
        recovery = DataHealthRecovery(action_id=action)
    elif condition is DataHealthBaseCondition.UNSUPPORTED_POPULATION:
        recovery = DataHealthRecovery(
            action_id=DataHealthRecoveryAction.REVIEW_SUPPORTED_POPULATION
        )
    elif condition is DataHealthBaseCondition.QUERY_REFUSED:
        recovery = DataHealthRecovery(action_id=DataHealthRecoveryAction.REVIEW_SUPPORTED_REQUEST)
    elif condition is DataHealthBaseCondition.BLOCKED_BY_REACH:
        recovery = DataHealthRecovery(action_id=DataHealthRecoveryAction.INSPECT_REACH_CONDITION)
    elif condition is DataHealthBaseCondition.TRUNCATED:
        action = (
            DataHealthRecoveryAction.NARROW_EXACT_SCOPE
            if can_narrow_exact_scope
            else DataHealthRecoveryAction.CONTACT_WORKSPACE_DATA_OWNER
        )
        recovery = DataHealthRecovery(action_id=action)
    elif condition is DataHealthBaseCondition.SUPPRESSED:
        recovery = DataHealthRecovery(
            no_self_service_reason=DataHealthNoSelfServiceReason.SOURCE_PRIVACY_THRESHOLD
        )
    else:
        recovery = DataHealthRecovery(
            no_self_service_reason=DataHealthNoSelfServiceReason.COVERED_NO_ROWS
        )
    return recovery


def _valid_recovery(
    condition: DataHealthBaseCondition,
    recovery: DataHealthRecovery | None,
) -> bool:
    if condition is DataHealthBaseCondition.AVAILABLE:
        return recovery is None
    if recovery is None:
        return False
    if condition is DataHealthBaseCondition.COVERAGE_HOLE:
        return recovery.action_id in {
            DataHealthRecoveryAction.CHOOSE_COVERED_PERIOD,
            DataHealthRecoveryAction.CONTACT_WORKSPACE_DATA_OWNER,
        }
    if condition is DataHealthBaseCondition.TRUNCATED:
        return recovery.action_id in {
            DataHealthRecoveryAction.NARROW_EXACT_SCOPE,
            DataHealthRecoveryAction.CONTACT_WORKSPACE_DATA_OWNER,
        }
    return recovery == recovery_for_condition(condition)


def _reach_blocks_frequency(
    reach: DataHealthCheckResult,
    frequency: DataHealthCheckRequest,
) -> bool:
    return (
        not reach.usable
        or not frequency.reach_slice_compatible
        or any(row.value == 0 for row in reach.evidence_rows)
    )


def _build_check(
    request: DataHealthCheckRequest,
    reach: DataHealthCheckResult | None = None,
) -> DataHealthCheckResult:
    candidates = set(request.condition_candidates)
    if request.check is DataHealthCheck.AVG_FREQUENCY:
        if reach is None:
            raise ValueError("average frequency must be derived after reach.")
        if _reach_blocks_frequency(reach, request):
            candidates.add(DataHealthBaseCondition.BLOCKED_BY_REACH)
    condition = reduce_base_condition(tuple(candidates))
    usable = condition is DataHealthBaseCondition.AVAILABLE
    qualifiers = tuple(
        qualifier
        for qualifier in FRESHNESS_QUALIFIER_ORDER
        if usable and qualifier in request.freshness_qualifiers
    )
    rows = request.evidence_rows
    if condition is DataHealthBaseCondition.BLOCKED_BY_REACH:
        rows = ()
    return DataHealthCheckResult(
        check=request.check,
        source=request.source,
        population=request.population,
        grain=request.grain,
        units=request.units,
        base_condition=condition,
        usable=usable,
        consequence=_CONSEQUENCE[condition],
        requested_period=request.requested_period,
        actual_coverage=request.actual_coverage,
        coverage_holes=request.coverage_holes,
        freshness_qualifiers=qualifiers,
        suppression=request.suppression,
        dq=request.dq,
        row_count=len(rows),
        evidence_rows=rows,
        loaded_at=request.loaded_at,
        truncated=request.truncated,
        recovery=recovery_for_condition(
            condition,
            request.complete_recovery_period,
            request.can_narrow_exact_scope,
        ),
    )


def reduce_data_health_state(
    checks: tuple[DataHealthCheckResult, ...],
) -> DataHealthState:
    """Apply top-state precedence independently of condition precedence."""
    if any(check.base_condition in _NO_COVERAGE_CONDITIONS for check in checks):
        return DataHealthState.NO_COVERAGE
    if any(not check.usable for check in checks):
        return DataHealthState.INSUFFICIENT
    if any(check.freshness_qualifiers for check in checks):
        return DataHealthState.QUALIFIED
    return DataHealthState.HEALTHY


def select_narrative_action(
    checks: tuple[DataHealthCheckResult, ...],
) -> DataHealthNarrativeActionCandidate | None:
    """Select an action only from the highest-precedence blocked check."""
    blocked = tuple(check for check in checks if not check.usable)
    if not blocked:
        return None
    highest = min(
        blocked,
        key=lambda item: (_CONDITION_RANK[item.base_condition], _CHECK_RANK[item.check]),
    )
    if highest.recovery is None or highest.recovery.action_id is None:
        return None
    return DataHealthNarrativeActionCandidate(highest.check, highest.recovery.action_id)


def _validate_denominator_links(checks: tuple[DataHealthCheckResult, ...]) -> None:
    reach, frequency = checks
    reach_rows = {row.evidence_ref: row for row in reach.evidence_rows}
    if frequency.usable:
        for row in frequency.evidence_rows:
            denominator = reach_rows.get(row.reach_denominator_ref or "")
            if (
                denominator is None
                or denominator.value == 0
                or denominator.dimensions != row.dimensions
            ):
                raise ValueError(
                    "frequency evidence must link to compatible nonzero reach at the same slice."
                )


def build_data_health_detail(request: DataHealthDetailRequest) -> DataHealthDetailArtifact:
    """Derive reach, then frequency, then top state and one narrative action candidate."""
    reach = _build_check(request.reach)
    frequency = _build_check(request.frequency, reach)
    checks = (reach, frequency)
    _validate_denominator_links(checks)
    return DataHealthDetailArtifact(
        state=reduce_data_health_state(checks),
        period=request.period,
        serving_provenance=request.serving_provenance,
        checks=checks,
        limitations=DATA_HEALTH_LIMITATIONS,
        narrative_action_candidate=select_narrative_action(checks),
    )


def _period_payload(period: DateRange) -> dict[str, str]:
    return {"end": period.end.isoformat(), "start": period.start.isoformat()}


def _units_payload(units: Units) -> dict[str, str | None]:
    return {"currency_code": units.currency_code, "kind": units.kind.value}


def _evidence_row_payload(row: DataHealthEvidenceRow) -> dict[str, object]:
    return {
        "dimensions": [
            {
                "name": name,
                "value": value.isoformat() if isinstance(value, date) else value,
            }
            for name, value in row.dimensions
        ],
        "evidence_ref": row.evidence_ref,
        "reach_denominator_ref": row.reach_denominator_ref,
        "units": _units_payload(row.units),
        "value": str(row.value) if isinstance(row.value, Decimal) else row.value,
    }


def _recovery_payload(recovery: DataHealthRecovery | None) -> dict[str, str | None] | None:
    if recovery is None:
        return None
    return {
        "action_id": None if recovery.action_id is None else recovery.action_id.value,
        "no_self_service_reason": (
            None
            if recovery.no_self_service_reason is None
            else recovery.no_self_service_reason.value
        ),
    }


def _check_payload(check: DataHealthCheckResult) -> dict[str, object]:
    return {
        "actual_coverage": [_period_payload(period) for period in check.actual_coverage],
        "base_condition": check.base_condition.value,
        "check": check.check.value,
        "consequence": check.consequence,
        "coverage_holes": [_period_payload(period) for period in check.coverage_holes],
        "dq": list(check.dq),
        "evidence_rows": [_evidence_row_payload(row) for row in check.evidence_rows],
        "freshness_qualifiers": [item.value for item in check.freshness_qualifiers],
        "grain": list(check.grain.dimensions),
        "loaded_at": (
            None if check.loaded_at is None else check.loaded_at.astimezone(UTC).isoformat()
        ),
        "population": check.population,
        "recovery": _recovery_payload(check.recovery),
        "requested_period": _period_payload(check.requested_period),
        "row_count": check.row_count,
        "source": check.source,
        "suppression": {
            "reason": check.suppression.reason,
            "rows_suppressed": check.suppression.rows_suppressed,
        },
        "truncated": check.truncated,
        "units": _units_payload(check.units),
        "usable": check.usable,
    }


def data_health_detail_payload(artifact: DataHealthDetailArtifact) -> dict[str, object]:
    """Project one DataHealthDetail into its canonical public v1 payload."""
    candidate = artifact.narrative_action_candidate
    return {
        "checks": [_check_payload(check) for check in artifact.checks],
        "limitations": list(artifact.limitations),
        "narrative_action_candidate": (
            None
            if candidate is None
            else {
                "action_id": candidate.action_id.value,
                "check": candidate.check.value,
            }
        ),
        "period": _period_payload(artifact.period),
        "serving_provenance": {
            "evidence_digest": artifact.serving_provenance.evidence_digest,
            "inspection_attested": artifact.serving_provenance.inspection_attested,
            "population": artifact.serving_provenance.population,
            "source": artifact.serving_provenance.source,
        },
        "state": artifact.state.value,
    }
