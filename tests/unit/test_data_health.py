# SPDX-License-Identifier: MIT-0
"""Data-health condition precedence, recovery, evidence, and page-state reduction."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import pairwise
from typing import cast

import pytest

from domain.narrative.shaped_response import UnitKind, Units
from domain.presentation.data_health import (
    BASE_CONDITION_PRECEDENCE,
    CHECK_PRECEDENCE,
    DATA_HEALTH_LIMITATIONS,
    DataHealthBaseCondition,
    DataHealthCheck,
    DataHealthCheckRequest,
    DataHealthDetailArtifact,
    DataHealthDetailRequest,
    DataHealthEvidenceRow,
    DataHealthFreshnessQualifier,
    DataHealthNarrativeActionCandidate,
    DataHealthNoSelfServiceReason,
    DataHealthRecovery,
    DataHealthRecoveryAction,
    DataHealthServingProvenance,
    DataHealthState,
    build_data_health_detail,
    data_health_detail_payload,
    recovery_for_condition,
    reduce_base_condition,
)
from domain.shared_kernel.provenance import Suppression
from domain.shared_kernel.values import DateRange, Grain

pytestmark = pytest.mark.unit

PERIOD = DateRange(date(2026, 8, 1), date(2026, 8, 3))
COVERED_PERIOD = DateRange(date(2026, 7, 28), date(2026, 7, 30))
GRAIN = Grain(("date", "ad_product", "campaign", "channel"))
DIMENSIONS = (
    ("date", date(2026, 8, 1)),
    ("ad_product", "Sponsored Products"),
    ("campaign", "Fictional campaign"),
    ("channel", "Sponsored ads"),
)
COUNT = Units(UnitKind.COUNT)
RATIO = Units(UnitKind.RATIO)
LOADED_AT = datetime(2026, 8, 4, 12, tzinfo=UTC)
PROVENANCE = DataHealthServingProvenance(
    source="Amazon Marketing Cloud",
    population="Sponsored-ad exposed users",
    evidence_digest="opaque-health-evidence",
    inspection_attested=True,
)


@dataclass(frozen=True, slots=True)
class _CheckOptions:
    """Compact test-only variations for one typed check request."""

    qualifiers: tuple[DataHealthFreshnessQualifier, ...] = ()
    dq: tuple[str, ...] = ()
    value: int | Decimal | None = None
    extra_candidates: tuple[DataHealthBaseCondition, ...] = ()
    complete_recovery_period: DateRange | None = None
    can_narrow_exact_scope: bool = False
    reach_slice_compatible: bool = True
    denominator_ref: str = "reach-1"
    dimensions: tuple[tuple[str, str | date], ...] = DIMENSIONS


def _request(
    check: DataHealthCheck,
    condition: DataHealthBaseCondition = DataHealthBaseCondition.AVAILABLE,
    options: _CheckOptions | None = None,
) -> DataHealthCheckRequest:
    selected = options or _CheckOptions()
    actual_coverage: tuple[DateRange, ...] = (PERIOD,)
    coverage_holes: tuple[DateRange, ...] = ()
    suppression = Suppression(False)
    truncated = False
    loaded_at: datetime | None = LOADED_AT
    include_row = condition in {
        DataHealthBaseCondition.AVAILABLE,
        DataHealthBaseCondition.TRUNCATED,
    }
    if condition in {
        DataHealthBaseCondition.NEVER_LOADED,
        DataHealthBaseCondition.NO_CURRENT_PUBLICATION,
    }:
        actual_coverage = ()
        loaded_at = None
    elif condition is DataHealthBaseCondition.COVERAGE_HOLE:
        actual_coverage = ()
        coverage_holes = (DateRange(PERIOD.start, PERIOD.start),)
    elif condition is DataHealthBaseCondition.SUPPRESSED:
        suppression = Suppression(True, "source privacy threshold")
    elif condition is DataHealthBaseCondition.TRUNCATED:
        truncated = True
    elif condition in {
        DataHealthBaseCondition.UNSUPPORTED_POPULATION,
        DataHealthBaseCondition.QUERY_REFUSED,
    }:
        actual_coverage = ()

    units = COUNT if check is DataHealthCheck.REACH_USERS else RATIO
    default_value: int | Decimal = 10 if check is DataHealthCheck.REACH_USERS else Decimal("2.0000")
    value = default_value if selected.value is None else selected.value
    rows: tuple[DataHealthEvidenceRow, ...] = ()
    if include_row:
        rows = (
            DataHealthEvidenceRow(
                evidence_ref=("reach-1" if check is DataHealthCheck.REACH_USERS else "frequency-1"),
                dimensions=selected.dimensions,
                value=value,
                units=units,
                reach_denominator_ref=(
                    None if check is DataHealthCheck.REACH_USERS else selected.denominator_ref
                ),
            ),
        )
    return DataHealthCheckRequest(
        check=check,
        source=PROVENANCE.source,
        population=PROVENANCE.population,
        grain=GRAIN,
        units=units,
        requested_period=PERIOD,
        condition_candidates=(condition, *selected.extra_candidates),
        actual_coverage=actual_coverage,
        coverage_holes=coverage_holes,
        freshness_qualifiers=selected.qualifiers,
        suppression=suppression,
        dq=selected.dq,
        row_count=len(rows),
        evidence_rows=rows,
        loaded_at=loaded_at,
        truncated=truncated,
        complete_recovery_period=selected.complete_recovery_period,
        can_narrow_exact_scope=selected.can_narrow_exact_scope,
        reach_slice_compatible=selected.reach_slice_compatible,
    )


def _build(
    reach: DataHealthCheckRequest,
    frequency: DataHealthCheckRequest,
) -> DataHealthDetailArtifact:
    return build_data_health_detail(DataHealthDetailRequest(PERIOD, PROVENANCE, reach, frequency))


class TestClosedVocabulary:
    def test_states_checks_conditions_qualifiers_and_recoveries_are_exhaustive(self) -> None:
        assert tuple(DataHealthState) == (
            DataHealthState.HEALTHY,
            DataHealthState.QUALIFIED,
            DataHealthState.INSUFFICIENT,
            DataHealthState.NO_COVERAGE,
        )
        assert tuple(DataHealthCheck) == CHECK_PRECEDENCE
        assert {condition.value for condition in DataHealthBaseCondition} == {
            "available",
            "empty_covered",
            "suppressed",
            "truncated",
            "never_loaded",
            "no_current_publication",
            "coverage_hole",
            "unsupported_population",
            "query_refused",
            "blocked_by_reach",
        }
        assert {qualifier.value for qualifier in DataHealthFreshnessQualifier} == {
            "stale",
            "latest_attempt_rejected",
            "qualified_dq",
        }
        assert {action.value for action in DataHealthRecoveryAction} == {
            "contact_workspace_data_owner",
            "choose_covered_period",
            "review_supported_population",
            "review_supported_request",
            "inspect_reach_condition",
            "narrow_exact_scope",
        }
        assert {reason.value for reason in DataHealthNoSelfServiceReason} == {
            "source_privacy_threshold",
            "covered_no_rows",
        }

    def test_recovery_is_exactly_one_tagged_alternative(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            DataHealthRecovery()
        with pytest.raises(ValueError, match="exactly one"):
            DataHealthRecovery(
                DataHealthRecoveryAction.REVIEW_SUPPORTED_REQUEST,
                DataHealthNoSelfServiceReason.COVERED_NO_ROWS,
            )

    def test_serving_provenance_requires_attested_inspection(self) -> None:
        with pytest.raises(ValueError, match="attested"):
            DataHealthServingProvenance(
                "Amazon Marketing Cloud",
                "Sponsored-ad exposed users",
                "opaque-evidence",
                False,
            )


class TestConditionPrecedence:
    @pytest.mark.parametrize(
        ("higher", "lower"),
        list(pairwise(BASE_CONDITION_PRECEDENCE)),
    )
    def test_every_adjacent_condition_precedes_the_next(
        self,
        higher: DataHealthBaseCondition,
        lower: DataHealthBaseCondition,
    ) -> None:
        assert reduce_base_condition((lower, higher)) is higher

    def test_full_reversed_condition_set_still_selects_unsupported_population(self) -> None:
        result = reduce_base_condition(tuple(reversed(BASE_CONDITION_PRECEDENCE)))

        assert result is DataHealthBaseCondition.UNSUPPORTED_POPULATION

    def test_empty_or_duplicate_condition_candidates_are_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            reduce_base_condition(())
        with pytest.raises(ValueError, match="unique"):
            reduce_base_condition(
                (
                    DataHealthBaseCondition.AVAILABLE,
                    DataHealthBaseCondition.AVAILABLE,
                )
            )

    @pytest.mark.parametrize(
        "condition",
        [
            DataHealthBaseCondition.AVAILABLE,
            DataHealthBaseCondition.EMPTY_COVERED,
            DataHealthBaseCondition.SUPPRESSED,
            DataHealthBaseCondition.TRUNCATED,
            DataHealthBaseCondition.NEVER_LOADED,
            DataHealthBaseCondition.NO_CURRENT_PUBLICATION,
            DataHealthBaseCondition.COVERAGE_HOLE,
            DataHealthBaseCondition.UNSUPPORTED_POPULATION,
            DataHealthBaseCondition.QUERY_REFUSED,
        ],
    )
    def test_every_non_dependency_condition_is_derived_without_coercion(
        self,
        condition: DataHealthBaseCondition,
    ) -> None:
        artifact = _build(
            _request(DataHealthCheck.REACH_USERS, condition),
            _request(DataHealthCheck.AVG_FREQUENCY),
        )

        assert artifact.checks[0].base_condition is condition

    def test_higher_check_condition_wins_when_lower_facts_are_also_present(self) -> None:
        reach = _request(
            DataHealthCheck.REACH_USERS,
            DataHealthBaseCondition.COVERAGE_HOLE,
            _CheckOptions(extra_candidates=(DataHealthBaseCondition.AVAILABLE,)),
        )
        artifact = _build(reach, _request(DataHealthCheck.AVG_FREQUENCY))

        assert artifact.checks[0].base_condition is DataHealthBaseCondition.COVERAGE_HOLE


class TestReachBeforeFrequency:
    def test_represented_zero_reach_remains_evidence_and_blocks_frequency(self) -> None:
        artifact = _build(
            _request(
                DataHealthCheck.REACH_USERS,
                options=_CheckOptions(value=0),
            ),
            _request(DataHealthCheck.AVG_FREQUENCY),
        )
        reach, frequency = artifact.checks

        assert reach.base_condition is DataHealthBaseCondition.AVAILABLE
        assert reach.usable is True
        assert reach.evidence_rows[0].value == 0
        assert reach.evidence_rows[0].units == COUNT
        assert reach.grain == GRAIN
        assert frequency.base_condition is DataHealthBaseCondition.BLOCKED_BY_REACH
        assert frequency.usable is False
        assert frequency.evidence_rows == ()
        assert frequency.recovery == DataHealthRecovery(
            action_id=DataHealthRecoveryAction.INSPECT_REACH_CONDITION
        )

    @pytest.mark.parametrize(
        "reach_condition",
        [
            DataHealthBaseCondition.EMPTY_COVERED,
            DataHealthBaseCondition.SUPPRESSED,
            DataHealthBaseCondition.NO_CURRENT_PUBLICATION,
        ],
    )
    def test_missing_or_suppressed_reach_blocks_frequency(
        self,
        reach_condition: DataHealthBaseCondition,
    ) -> None:
        artifact = _build(
            _request(DataHealthCheck.REACH_USERS, reach_condition),
            _request(DataHealthCheck.AVG_FREQUENCY),
        )

        assert artifact.checks[1].base_condition is DataHealthBaseCondition.BLOCKED_BY_REACH

    def test_incompatible_exact_slice_blocks_frequency(self) -> None:
        frequency = _request(
            DataHealthCheck.AVG_FREQUENCY,
            options=_CheckOptions(reach_slice_compatible=False),
        )
        artifact = _build(_request(DataHealthCheck.REACH_USERS), frequency)

        assert artifact.checks[1].base_condition is DataHealthBaseCondition.BLOCKED_BY_REACH

    def test_higher_frequency_failure_precedes_derived_reach_block(self) -> None:
        artifact = _build(
            _request(
                DataHealthCheck.REACH_USERS,
                options=_CheckOptions(value=0),
            ),
            _request(DataHealthCheck.AVG_FREQUENCY, DataHealthBaseCondition.SUPPRESSED),
        )

        assert artifact.checks[1].base_condition is DataHealthBaseCondition.SUPPRESSED


class TestTopLevelState:
    def test_both_available_unqualified_checks_are_healthy(self) -> None:
        artifact = _build(
            _request(DataHealthCheck.REACH_USERS),
            _request(DataHealthCheck.AVG_FREQUENCY),
        )

        assert artifact.state is DataHealthState.HEALTHY
        assert all(check.usable for check in artifact.checks)
        assert artifact.narrative_action_candidate is None
        assert artifact.limitations == DATA_HEALTH_LIMITATIONS

    @pytest.mark.parametrize(
        "qualifier",
        [
            DataHealthFreshnessQualifier.STALE,
            DataHealthFreshnessQualifier.LATEST_ATTEMPT_REJECTED,
            DataHealthFreshnessQualifier.QUALIFIED_DQ,
        ],
    )
    def test_each_usable_freshness_or_dq_qualifier_yields_qualified(
        self,
        qualifier: DataHealthFreshnessQualifier,
    ) -> None:
        options = _CheckOptions(
            qualifiers=(qualifier,),
            dq=("Source caveat",) if qualifier is DataHealthFreshnessQualifier.QUALIFIED_DQ else (),
        )
        artifact = _build(
            _request(DataHealthCheck.REACH_USERS, options=options),
            _request(DataHealthCheck.AVG_FREQUENCY),
        )

        assert artifact.state is DataHealthState.QUALIFIED
        assert artifact.checks[0].base_condition is DataHealthBaseCondition.AVAILABLE
        assert artifact.checks[0].usable is True
        assert artifact.checks[0].freshness_qualifiers == (qualifier,)

    def test_newer_rejected_attempt_does_not_replace_serving_current_authority(self) -> None:
        artifact = _build(
            _request(
                DataHealthCheck.REACH_USERS,
                options=_CheckOptions(
                    qualifiers=(DataHealthFreshnessQualifier.LATEST_ATTEMPT_REJECTED,)
                ),
            ),
            _request(DataHealthCheck.AVG_FREQUENCY),
        )

        reach = artifact.checks[0]
        assert artifact.state is DataHealthState.QUALIFIED
        assert reach.base_condition is DataHealthBaseCondition.AVAILABLE
        assert reach.usable is True
        assert reach.loaded_at == LOADED_AT

    def test_any_noncoverage_blocker_yields_insufficient(self) -> None:
        artifact = _build(
            _request(DataHealthCheck.REACH_USERS, DataHealthBaseCondition.EMPTY_COVERED),
            _request(DataHealthCheck.AVG_FREQUENCY),
        )

        assert artifact.state is DataHealthState.INSUFFICIENT

    @pytest.mark.parametrize(
        "condition",
        [
            DataHealthBaseCondition.NEVER_LOADED,
            DataHealthBaseCondition.NO_CURRENT_PUBLICATION,
        ],
    )
    def test_absent_required_serving_publication_has_no_coverage_precedence(
        self,
        condition: DataHealthBaseCondition,
    ) -> None:
        artifact = _build(
            _request(DataHealthCheck.REACH_USERS),
            _request(DataHealthCheck.AVG_FREQUENCY, condition),
        )

        assert artifact.state is DataHealthState.NO_COVERAGE
        assert artifact.checks[0].usable is True

    def test_state_precedence_refuses_a_manual_contradiction(self) -> None:
        artifact = _build(
            _request(DataHealthCheck.REACH_USERS),
            _request(DataHealthCheck.AVG_FREQUENCY),
        )

        with pytest.raises(ValueError, match="top-level state"):
            replace(artifact, state=DataHealthState.INSUFFICIENT)


class TestClosedRecoveryMapping:
    @pytest.mark.parametrize(
        ("condition", "action", "reason"),
        [
            (DataHealthBaseCondition.AVAILABLE, None, None),
            (
                DataHealthBaseCondition.NEVER_LOADED,
                DataHealthRecoveryAction.CONTACT_WORKSPACE_DATA_OWNER,
                None,
            ),
            (
                DataHealthBaseCondition.NO_CURRENT_PUBLICATION,
                DataHealthRecoveryAction.CONTACT_WORKSPACE_DATA_OWNER,
                None,
            ),
            (
                DataHealthBaseCondition.COVERAGE_HOLE,
                DataHealthRecoveryAction.CONTACT_WORKSPACE_DATA_OWNER,
                None,
            ),
            (
                DataHealthBaseCondition.UNSUPPORTED_POPULATION,
                DataHealthRecoveryAction.REVIEW_SUPPORTED_POPULATION,
                None,
            ),
            (
                DataHealthBaseCondition.QUERY_REFUSED,
                DataHealthRecoveryAction.REVIEW_SUPPORTED_REQUEST,
                None,
            ),
            (
                DataHealthBaseCondition.BLOCKED_BY_REACH,
                DataHealthRecoveryAction.INSPECT_REACH_CONDITION,
                None,
            ),
            (
                DataHealthBaseCondition.TRUNCATED,
                DataHealthRecoveryAction.CONTACT_WORKSPACE_DATA_OWNER,
                None,
            ),
            (
                DataHealthBaseCondition.SUPPRESSED,
                None,
                DataHealthNoSelfServiceReason.SOURCE_PRIVACY_THRESHOLD,
            ),
            (
                DataHealthBaseCondition.EMPTY_COVERED,
                None,
                DataHealthNoSelfServiceReason.COVERED_NO_ROWS,
            ),
        ],
    )
    def test_every_condition_has_its_exact_default_recovery(
        self,
        condition: DataHealthBaseCondition,
        action: DataHealthRecoveryAction | None,
        reason: DataHealthNoSelfServiceReason | None,
    ) -> None:
        recovery = recovery_for_condition(condition)

        if condition is DataHealthBaseCondition.AVAILABLE:
            assert recovery is None
        else:
            assert recovery is not None
            assert recovery.action_id is action
            assert recovery.no_self_service_reason is reason

    def test_coverage_hole_uses_supplied_complete_period_only(self) -> None:
        recovery = recovery_for_condition(
            DataHealthBaseCondition.COVERAGE_HOLE,
            COVERED_PERIOD,
        )

        assert recovery == DataHealthRecovery(
            action_id=DataHealthRecoveryAction.CHOOSE_COVERED_PERIOD
        )

    def test_truncation_narrows_only_when_an_exact_scope_control_exists(self) -> None:
        recovery = recovery_for_condition(
            DataHealthBaseCondition.TRUNCATED,
            can_narrow_exact_scope=True,
        )

        assert recovery == DataHealthRecovery(action_id=DataHealthRecoveryAction.NARROW_EXACT_SCOPE)


class TestNarrativeActionSelection:
    def test_highest_condition_with_action_is_the_one_candidate(self) -> None:
        artifact = _build(
            _request(
                DataHealthCheck.REACH_USERS,
                DataHealthBaseCondition.UNSUPPORTED_POPULATION,
            ),
            _request(DataHealthCheck.AVG_FREQUENCY, DataHealthBaseCondition.QUERY_REFUSED),
        )

        assert artifact.narrative_action_candidate == DataHealthNarrativeActionCandidate(
            DataHealthCheck.REACH_USERS,
            DataHealthRecoveryAction.REVIEW_SUPPORTED_POPULATION,
        )

    def test_reach_precedes_frequency_when_the_conditions_tie(self) -> None:
        artifact = _build(
            _request(DataHealthCheck.REACH_USERS, DataHealthBaseCondition.QUERY_REFUSED),
            _request(DataHealthCheck.AVG_FREQUENCY, DataHealthBaseCondition.QUERY_REFUSED),
        )

        assert artifact.narrative_action_candidate == DataHealthNarrativeActionCandidate(
            DataHealthCheck.REACH_USERS,
            DataHealthRecoveryAction.REVIEW_SUPPORTED_REQUEST,
        )

    def test_no_self_service_top_blocker_does_not_fall_through_to_lower_action(self) -> None:
        artifact = _build(
            _request(DataHealthCheck.REACH_USERS, DataHealthBaseCondition.SUPPRESSED),
            _request(DataHealthCheck.AVG_FREQUENCY, DataHealthBaseCondition.QUERY_REFUSED),
        )

        assert artifact.checks[0].recovery == DataHealthRecovery(
            no_self_service_reason=DataHealthNoSelfServiceReason.SOURCE_PRIVACY_THRESHOLD
        )
        assert artifact.checks[1].recovery == DataHealthRecovery(
            action_id=DataHealthRecoveryAction.REVIEW_SUPPORTED_REQUEST
        )
        assert artifact.narrative_action_candidate is None

    def test_artifact_has_no_hypothesis_surface(self) -> None:
        artifact = _build(
            _request(DataHealthCheck.REACH_USERS),
            _request(DataHealthCheck.AVG_FREQUENCY),
        )

        assert not hasattr(artifact, "hypothesis")
        assert not hasattr(artifact, "hypothesis_candidate")


class TestExactEvidenceRows:
    def test_frequency_row_links_to_same_slice_nonzero_reach(self) -> None:
        artifact = _build(
            _request(DataHealthCheck.REACH_USERS),
            _request(DataHealthCheck.AVG_FREQUENCY),
        )
        reach, frequency = artifact.checks
        frequency_row = frequency.evidence_rows[0]

        assert reach.evidence_rows[0].evidence_ref == frequency_row.reach_denominator_ref
        assert reach.evidence_rows[0].dimensions == frequency_row.dimensions
        assert reach.evidence_rows[0].value == 10
        assert reach.evidence_rows[0].units == COUNT
        assert reach.grain == GRAIN
        assert frequency_row.value == Decimal("2.0000")
        assert frequency_row.units == RATIO
        assert frequency.grain == GRAIN
        assert not hasattr(frequency_row, "total")

    def test_unknown_reach_denominator_reference_is_refused(self) -> None:
        frequency = _request(
            DataHealthCheck.AVG_FREQUENCY,
            options=_CheckOptions(denominator_ref="unknown-reach"),
        )

        with pytest.raises(ValueError, match="compatible nonzero reach"):
            _build(_request(DataHealthCheck.REACH_USERS), frequency)

    def test_different_slice_reach_denominator_is_refused(self) -> None:
        different_dimensions = (
            ("date", date(2026, 8, 2)),
            *DIMENSIONS[1:],
        )
        frequency = _request(
            DataHealthCheck.AVG_FREQUENCY,
            options=_CheckOptions(dimensions=different_dimensions),
        )

        with pytest.raises(ValueError, match="same slice"):
            _build(_request(DataHealthCheck.REACH_USERS), frequency)

    def test_internal_selector_dimension_is_refused(self) -> None:
        with pytest.raises(ValueError, match="safe public"):
            DataHealthEvidenceRow(
                "unsafe-row",
                (("campaign_key", "internal-campaign"),),
                10,
                COUNT,
            )

    def test_covered_empty_is_absence_while_zero_is_a_represented_row(self) -> None:
        zero_artifact = _build(
            _request(
                DataHealthCheck.REACH_USERS,
                options=_CheckOptions(value=0),
            ),
            _request(DataHealthCheck.AVG_FREQUENCY),
        )
        empty_artifact = _build(
            _request(DataHealthCheck.REACH_USERS, DataHealthBaseCondition.EMPTY_COVERED),
            _request(DataHealthCheck.AVG_FREQUENCY),
        )

        zero_reach = zero_artifact.checks[0]
        empty_reach = empty_artifact.checks[0]
        assert zero_reach.evidence_rows[0].value == 0
        assert zero_reach.evidence_rows[0].units == COUNT
        assert zero_reach.grain == GRAIN
        assert empty_reach.evidence_rows == ()
        assert empty_reach.row_count == 0
        assert empty_reach.units == COUNT
        assert empty_reach.grain == GRAIN


class TestCanonicalPayload:
    def test_decimal_dates_instants_units_and_evidence_links_are_json_safe(self) -> None:
        artifact = _build(
            _request(DataHealthCheck.REACH_USERS),
            _request(DataHealthCheck.AVG_FREQUENCY),
        )

        payload = data_health_detail_payload(artifact)
        reach, frequency = cast("list[dict[str, object]]", payload["checks"])
        reach_row = cast("list[dict[str, object]]", reach["evidence_rows"])[0]
        frequency_row = cast("list[dict[str, object]]", frequency["evidence_rows"])[0]

        assert payload["state"] == "healthy"
        assert payload["period"] == {"end": "2026-08-03", "start": "2026-08-01"}
        assert payload["serving_provenance"] == {
            "evidence_digest": "opaque-health-evidence",
            "inspection_attested": True,
            "population": "Sponsored-ad exposed users",
            "source": "Amazon Marketing Cloud",
        }
        assert reach["grain"] == ["date", "ad_product", "campaign", "channel"]
        assert reach["units"] == {"currency_code": None, "kind": "count"}
        assert reach["loaded_at"] == "2026-08-04T12:00:00+00:00"
        assert reach["suppression"] == {"reason": None, "rows_suppressed": False}
        dimensions = cast("list[dict[str, object]]", reach_row["dimensions"])
        assert dimensions[0] == {"name": "date", "value": "2026-08-01"}
        assert reach_row["value"] == 10
        assert frequency["grain"] == reach["grain"]
        assert frequency["units"] == {"currency_code": None, "kind": "ratio"}
        assert frequency_row["value"] == "2.0000"
        assert frequency_row["reach_denominator_ref"] == reach_row["evidence_ref"]
        assert reach["recovery"] is None
        assert frequency["recovery"] is None
        assert payload["limitations"] == list(DATA_HEALTH_LIMITATIONS)
        assert payload["narrative_action_candidate"] is None

    def test_recovery_tags_and_narrative_candidate_remain_closed(self) -> None:
        no_coverage = data_health_detail_payload(
            _build(
                _request(
                    DataHealthCheck.REACH_USERS,
                    DataHealthBaseCondition.NO_CURRENT_PUBLICATION,
                ),
                _request(DataHealthCheck.AVG_FREQUENCY),
            )
        )
        no_coverage_reach = cast("list[dict[str, object]]", no_coverage["checks"])[0]
        assert no_coverage_reach["recovery"] == {
            "action_id": "contact_workspace_data_owner",
            "no_self_service_reason": None,
        }
        assert no_coverage["narrative_action_candidate"] == {
            "action_id": "contact_workspace_data_owner",
            "check": "reach_users",
        }

        suppressed = data_health_detail_payload(
            _build(
                _request(DataHealthCheck.REACH_USERS, DataHealthBaseCondition.SUPPRESSED),
                _request(DataHealthCheck.AVG_FREQUENCY),
            )
        )
        suppressed_reach = cast("list[dict[str, object]]", suppressed["checks"])[0]
        assert suppressed_reach["recovery"] == {
            "action_id": None,
            "no_self_service_reason": "source_privacy_threshold",
        }
        assert suppressed["narrative_action_candidate"] is None


class TestStrictCheckInvariants:
    def test_qualifiers_are_canonically_ordered(self) -> None:
        options = _CheckOptions(
            qualifiers=(
                DataHealthFreshnessQualifier.LATEST_ATTEMPT_REJECTED,
                DataHealthFreshnessQualifier.STALE,
            )
        )
        artifact = _build(
            _request(DataHealthCheck.REACH_USERS, options=options),
            _request(DataHealthCheck.AVG_FREQUENCY),
        )

        assert artifact.checks[0].freshness_qualifiers == (
            DataHealthFreshnessQualifier.STALE,
            DataHealthFreshnessQualifier.LATEST_ATTEMPT_REJECTED,
        )

    def test_qualified_dq_requires_disclosed_dq_evidence(self) -> None:
        with pytest.raises(ValueError, match="qualified_dq"):
            _request(
                DataHealthCheck.REACH_USERS,
                options=_CheckOptions(qualifiers=(DataHealthFreshnessQualifier.QUALIFIED_DQ,)),
            )

    def test_freshness_qualifier_on_blocked_check_is_refused(self) -> None:
        with pytest.raises(ValueError, match="only to usable"):
            _request(
                DataHealthCheck.REACH_USERS,
                DataHealthBaseCondition.SUPPRESSED,
                _CheckOptions(qualifiers=(DataHealthFreshnessQualifier.STALE,)),
            )

    def test_result_refuses_usable_flag_that_contradicts_condition(self) -> None:
        artifact = _build(
            _request(DataHealthCheck.REACH_USERS),
            _request(DataHealthCheck.AVG_FREQUENCY),
        )

        with pytest.raises(ValueError, match="only available"):
            replace(artifact.checks[0], usable=False)

    def test_result_refuses_recovery_that_contradicts_condition(self) -> None:
        artifact = _build(
            _request(DataHealthCheck.REACH_USERS, DataHealthBaseCondition.EMPTY_COVERED),
            _request(DataHealthCheck.AVG_FREQUENCY),
        )

        with pytest.raises(ValueError, match="recovery contradicts"):
            replace(
                artifact.checks[0],
                recovery=DataHealthRecovery(
                    action_id=DataHealthRecoveryAction.REVIEW_SUPPORTED_REQUEST
                ),
            )
