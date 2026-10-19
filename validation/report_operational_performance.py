# SPDX-License-Identifier: MIT-0
"""Stage 0 and Stage 1 operational performance evidence reporter.

Stage 0 preserves the existing observation-only surface report. Stage 1 assembles the
approved report only from separately supplied status, owner-authority, and structured-log
evidence. The pure assembly functions refuse inference: identities and predecessor proof
must match byte-for-byte, and unavailable evidence remains null.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import UUID

import boto3

STAGE0_SURFACES = (
    "semantic_candidate_publication",
    "preset_enqueue",
    "preset_bake",
    "insight_precompute",
)
STAGE1_SURFACES = (
    "semantic_projection_plan",
    "semantic_projection_unit",
    "semantic_inline_table_run",
    "semantic_servable_pointer",
)
ACTIVE_SURFACES = STAGE0_SURFACES
_EVIDENCE = Path(__file__).parent / "evidence" / "reports"
_LABEL_PATTERN = r"[a-z0-9][a-z0-9-]{0,63}"
_ACCOUNT_DIGITS = 12
_CONTRACTS = {
    "clock": "DataFlowMeasurement.v1",
    "eligibility": "DataFlowMeasurement.v1.eligible_outcomes/excluded_outcomes",
    "category": "UsageByCategory.v1",
}
_EVIDENCE_STATES = frozenset({"complete", "missing", "conflicting", "unmatched"})
# A "unit" authority is one physical table's work: a queued plan unit or, under the transitional
# inline composition, one inline table run. Both join the same owner-ordered ``units`` rows.
_KIND_SURFACES: Mapping[str, frozenset[str]] = {
    "plan": frozenset({"semantic_projection_plan"}),
    "unit": frozenset({"semantic_projection_unit", "semantic_inline_table_run"}),
    "pointer": frozenset({"semantic_servable_pointer"}),
}
_DEFAULT_DIRECTIONS: Mapping[str, Literal["higher", "lower"]] = {
    "delivery.invocation_count": "lower",
    "delivery.elapsed_ms": "lower",
    "delivery.athena_statement_count": "lower",
    "projection.merge_statements": "lower",
    "projection.verification_statements": "lower",
    "projection.total_statements": "lower",
    "projection.athena_engine_ms": "lower",
}

EvidenceState = Literal["complete", "missing", "conflicting", "unmatched"]
Direction = Literal["higher", "lower"]


class _OneValue(argparse.Action):
    """Reject repeated singleton options instead of accepting the last value."""

    def __call__(  # noqa: PLR0917 -- argparse.Action fixes this public signature
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        if getattr(namespace, self.dest, None) is not None:
            parser.error(f"{option_string or self.dest} must be supplied exactly once")
        setattr(namespace, self.dest, values)


def _nonblank(value: str) -> str:
    selected = value.strip()
    if not selected:
        raise argparse.ArgumentTypeError("must be nonblank")
    return selected


def _profile(value: str) -> str:
    selected = _nonblank(value)
    if selected == "default":
        raise argparse.ArgumentTypeError("must name an explicit non-default profile")
    return selected


def _positive_integer(value: str) -> int:
    try:
        selected = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if selected <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return selected


def _account(value: str) -> str:
    if len(value) != _ACCOUNT_DIGITS or not value.isascii() or not value.isdecimal():
        raise argparse.ArgumentTypeError("must be exactly twelve decimal digits")
    return value


def _label(value: str) -> str:
    import re

    if re.fullmatch(_LABEL_PATTERN, value) is None:
        raise argparse.ArgumentTypeError(f"must match {_LABEL_PATTERN}")
    return value


def _canonical_uuid(value: str) -> str:
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a canonical lowercase UUID") from error
    if str(parsed) != value:
        raise argparse.ArgumentTypeError("must be a canonical lowercase UUID")
    return value


def build_parser() -> argparse.ArgumentParser:
    """Build the strict approved Stage0/Stage1 command parser."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("stage0", "stage1"), action=_OneValue, required=True)
    parser.add_argument("--profile", type=_profile, action=_OneValue, required=True)
    parser.add_argument("--env", type=_nonblank, action=_OneValue, required=True)
    parser.add_argument("--window-minutes", type=_positive_integer, action=_OneValue, required=True)
    parser.add_argument("--label", type=_label, action=_OneValue, required=True)
    parser.add_argument("--expected-account", type=_account, action=_OneValue, required=True)
    parser.add_argument("--flow-id", type=_canonical_uuid, action=_OneValue)
    parser.add_argument("--baseline-label", type=_label, action=_OneValue)
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse and validate stage-specific cardinality before any evidence access."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.stage == "stage0" and (args.flow_id is not None or args.baseline_label is not None):
        parser.error("stage0 rejects --flow-id and --baseline-label")
    if args.stage == "stage1" and (args.flow_id is None or args.baseline_label is None):
        parser.error("stage1 requires exactly one --flow-id and one --baseline-label")
    return args


@dataclass(frozen=True, slots=True)
class IdentityProof:
    """Public flow/execution identity and byte-exact predecessor proof."""

    flow_id: str
    execution_id: str
    predecessor_execution_id: str | None

    def __post_init__(self) -> None:
        _require_canonical_uuid(self.flow_id, "flow_id")
        _require_canonical_uuid(self.execution_id, "execution_id")
        if self.predecessor_execution_id is not None:
            _require_canonical_uuid(self.predecessor_execution_id, "predecessor_execution_id")


@dataclass(frozen=True, slots=True)
class StatusObservation:
    """One retained protected-status flow projection and its event count."""

    identity: IdentityProof
    surface: str
    state: str | None
    outcome: str | None
    event_count: int
    evidence: EvidenceState = "complete"
    parent_flow_id: str | None = None

    def __post_init__(self) -> None:
        if self.surface not in STAGE1_SURFACES:
            raise ValueError("status surface is not an approved Stage1 surface")
        _require_nonnegative_integer(self.event_count, "event_count")
        _require_evidence(self.evidence)
        if self.state is not None and not self.state.strip():
            raise ValueError("state must be nonblank when present")
        if self.outcome is not None and not self.outcome.strip():
            raise ValueError("outcome must be nonblank when present")
        if self.parent_flow_id is not None:
            _require_canonical_uuid(self.parent_flow_id, "parent_flow_id")


@dataclass(frozen=True, slots=True)
class AuthorityObservation:
    """One owner record already read through an exact owner-selected key."""

    identity: IdentityProof
    kind: Literal["plan", "unit", "pointer"]
    state: str | None
    evidence: EvidenceState
    owner_ordinal: int | None = None
    merge_statements: int | None = None
    verification_statements: int | None = None
    athena_engine_ms: float | int | None = None
    attempt_statement_count: int | None = None

    def __post_init__(self) -> None:
        _require_evidence(self.evidence)
        if self.state is not None and not self.state.strip():
            raise ValueError("state must be nonblank when present")
        if self.kind == "plan" and self.owner_ordinal is not None:
            raise ValueError("plan authority has no ordinal")
        if self.kind != "plan":
            _require_positive_integer(self.owner_ordinal, "owner_ordinal")
        for name in (
            "merge_statements",
            "verification_statements",
            "attempt_statement_count",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_nonnegative_integer(value, name)
        if self.athena_engine_ms is not None:
            _require_nonnegative_number(self.athena_engine_ms, "athena_engine_ms")


@dataclass(frozen=True, slots=True)
class DeliveryTerminal:
    """One structured delivery terminal matched to retained identity authority."""

    identity: IdentityProof
    elapsed_ms: float | int
    evidence: EvidenceState = "complete"

    def __post_init__(self) -> None:
        _require_evidence(self.evidence)
        _require_nonnegative_number(self.elapsed_ms, "elapsed_ms")


@dataclass(frozen=True, slots=True)
class Stage1Evidence:
    """The three evidence families kept separate until exact join validation."""

    status: tuple[StatusObservation, ...]
    authority: tuple[AuthorityObservation, ...]
    terminals: tuple[DeliveryTerminal, ...]
    status_evidence: EvidenceState = "missing"
    authority_evidence: EvidenceState = "missing"
    terminal_evidence: EvidenceState = "missing"

    def __post_init__(self) -> None:
        _require_evidence(self.status_evidence)
        _require_evidence(self.authority_evidence)
        _require_evidence(self.terminal_evidence)


class StatusEvidenceReader(Protocol):
    """Read protected API/status-projection evidence for one public flow."""

    def read(self, flow_id: str, since: datetime, until: datetime) -> Sequence[StatusObservation]:
        """Return exact status observations in the requested closed window."""
        ...


class AuthorityEvidenceReader(Protocol):
    """Read retained plan/unit/pointer authority through exact owner selectors."""

    def read(self, status: Sequence[StatusObservation]) -> Sequence[AuthorityObservation]:
        """Return owner records selected only by exact retained status proof."""
        ...


class DeliveryLogEvidenceReader(Protocol):
    """Read structured terminals for authority-selected public executions."""

    def read(  # noqa: PLR0917 -- the seam carries both families and the closed window
        self,
        status: Sequence[StatusObservation],
        authority: Sequence[AuthorityObservation],
        since: datetime,
        until: datetime,
    ) -> Sequence[DeliveryTerminal]:
        """Return terminals matched to exact status and authority identities."""
        ...


@dataclass(frozen=True, slots=True)
class EvidenceReaders:
    """Constructor-injected seams for the three Stage1 evidence families."""

    status: StatusEvidenceReader
    authority: AuthorityEvidenceReader
    logs: DeliveryLogEvidenceReader

    def read(self, flow_id: str, since: datetime, until: datetime) -> Stage1Evidence:
        """Read status first, then owner authority, then matched terminals.

        Authority and terminals resolve only the requested flow's cohort: the report is
        one flow's readback, so a window that contains sibling flows must not widen the
        unit roster beyond the cohort the status proof names.
        """
        statuses = tuple(self.status.read(flow_id, since, until))
        cohort = _status_cohort(flow_id, statuses)
        authorities = tuple(self.authority.read(cohort))
        terminals = tuple(self.logs.read(cohort, authorities, since, until))
        return Stage1Evidence(
            statuses,
            authorities,
            terminals,
            status_evidence="complete" if statuses else "missing",
            authority_evidence="complete" if authorities else "missing",
            terminal_evidence="complete" if terminals else "missing",
        )


def _require_canonical_uuid(value: str, name: str) -> None:
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a canonical lowercase UUID") from error
    if str(parsed) != value:
        raise ValueError(f"{name} must be a canonical lowercase UUID")


def _require_evidence(value: str) -> None:
    if value not in _EVIDENCE_STATES:
        raise ValueError("evidence must use the approved closed vocabulary")


def _require_nonnegative_integer(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


def _require_positive_integer(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _require_nonnegative_number(value: object, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{name} must be a finite nonnegative number")


def _identity_matches(left: IdentityProof, right: IdentityProof) -> bool:
    return (
        left.flow_id == right.flow_id
        and left.execution_id == right.execution_id
        and left.predecessor_execution_id == right.predecessor_execution_id
    )


def _joined_authority(
    status: Sequence[StatusObservation], authority: AuthorityObservation
) -> EvidenceState:
    if authority.evidence != "complete":
        return authority.evidence
    expected_surfaces = _KIND_SURFACES[authority.kind]
    matches = [
        item
        for item in status
        if item.surface in expected_surfaces
        and _identity_matches(item.identity, authority.identity)
    ]
    if len(matches) != 1:
        return "unmatched" if not matches else "conflicting"
    if matches[0].evidence != "complete":
        return matches[0].evidence
    return "complete"


def _status_cohort(
    flow_id: str, observations: Sequence[StatusObservation]
) -> tuple[StatusObservation, ...]:
    selected_flows = {flow_id}
    selected: list[StatusObservation] = []
    pending = list(observations)
    while True:
        matched = [
            item
            for item in pending
            if item.identity.flow_id in selected_flows or item.parent_flow_id in selected_flows
        ]
        if not matched:
            return tuple(selected)
        selected.extend(matched)
        selected_flows.update(item.identity.flow_id for item in matched)
        pending = [item for item in pending if item not in matched]


def _status_document(evidence: Stage1Evidence) -> dict[str, object]:
    by_surface: dict[str, dict[str, object]] = {}
    usable_by_surface: dict[str, bool] = {}
    has_status = bool(evidence.status) and evidence.status_evidence == "complete"
    for surface in STAGE1_SURFACES:
        selected = [item for item in evidence.status if item.surface == surface]
        usable = has_status and all(item.evidence == "complete" for item in selected)
        usable_by_surface[surface] = usable
        outcomes = Counter(item.outcome for item in selected if item.outcome is not None)
        by_surface[surface] = {
            "flows": len({item.identity.flow_id for item in selected}) if usable else None,
            "events": sum(item.event_count for item in selected) if usable else None,
            "outcomes": dict(sorted(outcomes.items())) if usable else {},
        }
    usable = has_status and all(usable_by_surface.values())
    outcomes = Counter(item.outcome for item in evidence.status if item.outcome is not None)
    return {
        "flows": len({item.identity.flow_id for item in evidence.status}) if usable else None,
        "events": sum(item.event_count for item in evidence.status) if usable else None,
        "outcomes": dict(sorted(outcomes.items())) if usable else {},
        "by_surface": by_surface,
    }


def _authority_documents(
    flow_id: str, evidence: Stage1Evidence
) -> tuple[dict[str, object], dict[str, object], dict[AuthorityObservation, EvidenceState]]:
    identity_counts = Counter((item.kind, item.identity) for item in evidence.authority)
    joined: dict[AuthorityObservation, EvidenceState] = {
        item: (
            evidence.authority_evidence
            if evidence.authority_evidence != "complete"
            else evidence.status_evidence
            if evidence.status_evidence != "complete"
            else "conflicting"
            if identity_counts[(item.kind, item.identity)] != 1
            else _joined_authority(evidence.status, item)
        )
        for item in evidence.authority
    }
    plans = [
        item
        for item in evidence.authority
        if item.kind == "plan" and item.identity.flow_id == flow_id
    ]
    if len(plans) == 1:
        plan = plans[0]
        plan_evidence = joined[plan]
        plan_state = plan.state if plan_evidence == "complete" else None
    else:
        plan = None
        plan_evidence = (
            evidence.authority_evidence
            if evidence.authority_evidence != "complete"
            else "unmatched"
            if not plans
            else "conflicting"
        )
        plan_state = None

    def ordered(kind: Literal["unit", "pointer"]) -> list[AuthorityObservation]:
        selected = [item for item in evidence.authority if item.kind == kind]
        by_ordinal: dict[int, list[AuthorityObservation]] = {}
        for item in selected:
            by_ordinal.setdefault(cast("int", item.owner_ordinal), []).append(item)
        ordered_items: list[AuthorityObservation] = []
        for ordinal in sorted(by_ordinal):
            matches = by_ordinal[ordinal]
            representative = matches[0]
            if len(matches) != 1:
                joined[representative] = "conflicting"
            ordered_items.append(representative)
        return ordered_items

    units = ordered("unit")
    pointers = ordered("pointer")
    readback: dict[str, object] = {
        "plan": {"state": plan_state, "evidence": plan_evidence},
        "units": [
            {
                "unit_ordinal": item.owner_ordinal,
                "state": item.state if joined[item] == "complete" else None,
                "evidence": joined[item],
            }
            for item in units
        ],
        "pointers": [
            {
                "pointer_ordinal": item.owner_ordinal,
                "state": item.state if joined[item] == "complete" else None,
                "evidence": joined[item],
            }
            for item in pointers
        ],
    }
    complete_units = (
        bool(units)
        and evidence.authority_evidence == "complete"
        and all(joined[item] == "complete" for item in units)
    )
    by_unit: list[dict[str, object]] = []
    for item in units:
        complete = joined[item] == "complete"
        merge = item.merge_statements if complete else None
        verification = item.verification_statements if complete else None
        total = merge + verification if merge is not None and verification is not None else None
        by_unit.append(
            {
                "unit_ordinal": item.owner_ordinal,
                "merge_statements": merge,
                "verification_statements": verification,
                "total_statements": total,
                "athena_engine_ms": item.athena_engine_ms if complete else None,
            }
        )
    projection: dict[str, object] = {
        "unit_count": len(units) if complete_units else None,
        "merge_statements": (
            sum(cast("int", row["merge_statements"]) for row in by_unit) if complete_units else None
        ),
        "verification_statements": (
            sum(cast("int", row["verification_statements"]) for row in by_unit)
            if complete_units
            else None
        ),
        "total_statements": (
            sum(cast("int", row["total_statements"]) for row in by_unit) if complete_units else None
        ),
        "athena_engine_ms": (
            sum(cast("float | int", row["athena_engine_ms"]) for row in by_unit)
            if complete_units
            else None
        ),
        "by_unit": by_unit,
    }
    if plan is None or plan_evidence != "complete":
        projection["total_statements"] = None
    return readback, projection, joined


def _delivery_document(
    evidence: Stage1Evidence,
    projection: dict[str, object],
    joined: Mapping[AuthorityObservation, EvidenceState],
) -> dict[str, object]:
    status_matches = {
        item.identity: item for item in evidence.status if item.evidence == "complete"
    }
    terminals_complete = (
        bool(evidence.terminals)
        and evidence.terminal_evidence == "complete"
        and evidence.authority_evidence == "complete"
        and all(
            terminal.evidence == "complete"
            and terminal.identity in status_matches
            and sum(
                1
                for status in evidence.status
                if _identity_matches(status.identity, terminal.identity)
            )
            == 1
            and sum(
                1
                for authority in evidence.authority
                if authority.kind
                == (
                    "unit"
                    if status_matches[terminal.identity].surface
                    == "semantic_inline_table_run"
                    else "plan"
                )
                and _identity_matches(authority.identity, terminal.identity)
                and joined.get(authority) == "complete"
            )
            == 1
            for terminal in evidence.terminals
        )
    )
    invocation_count = len(evidence.terminals) if terminals_complete else None
    elapsed_ms: float | int | None = (
        sum(terminal.elapsed_ms for terminal in evidence.terminals) if terminals_complete else None
    )
    plans = [item for item in evidence.authority if item.kind == "plan"]
    attempt_total = plans[0].attempt_statement_count if len(plans) == 1 else None
    projection_total = projection["total_statements"]
    if (
        len(plans) != 1
        or joined.get(plans[0]) != "complete"
        or attempt_total is None
        or projection_total is None
        or attempt_total != projection_total
    ):
        athena_statement_count = None
        projection["total_statements"] = None
    else:
        athena_statement_count = attempt_total
    return {
        "invocation_count": invocation_count,
        "elapsed_ms": elapsed_ms,
        "athena_statement_count": athena_statement_count,
    }


def _fixed_metric_values(report: Mapping[str, object]) -> dict[str, float | int | None]:
    delivery = cast("Mapping[str, object]", report["delivery"])
    projection = cast("Mapping[str, object]", report["projection"])
    status = cast("Mapping[str, object]", report["status"])
    values: dict[str, float | int | None] = {}
    for name in ("invocation_count", "elapsed_ms", "athena_statement_count"):
        values[f"delivery.{name}"] = cast("float | int | None", delivery[name])
    for name in (
        "unit_count",
        "merge_statements",
        "verification_statements",
        "total_statements",
        "athena_engine_ms",
    ):
        values[f"projection.{name}"] = cast("float | int | None", projection[name])
    values["status.flows"] = cast("int | None", status["flows"])
    values["status.events"] = cast("int | None", status["events"])
    for outcome, value in cast("Mapping[str, int]", status["outcomes"]).items():
        values[f"status.outcomes.{outcome}"] = value
    for surface in STAGE1_SURFACES:
        surface_data = cast(
            "Mapping[str, object]", cast("Mapping[str, object]", status["by_surface"])[surface]
        )
        values[f"status.by_surface.{surface}.flows"] = cast("int | None", surface_data["flows"])
        values[f"status.by_surface.{surface}.events"] = cast("int | None", surface_data["events"])
        for outcome, value in cast("Mapping[str, int]", surface_data["outcomes"]).items():
            values[f"status.by_surface.{surface}.outcomes.{outcome}"] = value
    return dict(sorted(values.items()))


def _baseline_values(report: Mapping[str, object]) -> dict[str, float | int | None]:
    explicit = report.get("comparison_values")
    if isinstance(explicit, Mapping):
        result: dict[str, float | int | None] = {}
        for key, value in cast("Mapping[object, object]", explicit).items():
            if not isinstance(key, str) or (
                value is not None
                and (isinstance(value, bool) or not isinstance(value, int | float))
            ):
                continue
            result[key] = value
        return result
    if all(name in report for name in ("delivery", "projection", "status")):
        return _fixed_metric_values(report)
    return {}


def match_baseline(
    candidates: Sequence[Mapping[str, object]],
    *,
    baseline_label: str,
    env: str,
    expected_account: str,
    window_minutes: int,
    contracts: Mapping[str, str] = _CONTRACTS,
) -> Mapping[str, object] | None:
    """Return the sole exact Stage0 baseline or fail closed as unmatched."""
    matches: list[Mapping[str, object]] = []
    for candidate in candidates:
        value = candidate.get("input")
        if not isinstance(value, Mapping):
            continue
        input_data = cast("Mapping[str, object]", value)
        if (
            input_data.get("stage") == "stage0"
            and input_data.get("label") == baseline_label
            and input_data.get("env") == env
            and input_data.get("expected_account") == expected_account
            and input_data.get("window_minutes") == window_minutes
            and candidate.get("contracts") == contracts
        ):
            matches.append(candidate)
    return matches[0] if len(matches) == 1 else None


def compare_metrics(
    after: Mapping[str, float | int | None],
    baseline: Mapping[str, object] | None,
    directions: Mapping[str, Direction] = _DEFAULT_DIRECTIONS,
) -> dict[str, dict[str, object]]:
    """Compare canonical dotted values with uncertainty taking precedence."""
    before = {} if baseline is None else _baseline_values(baseline)
    compared: dict[str, dict[str, object]] = {}
    for key in sorted(after):
        before_value = before.get(key)
        after_value = after[key]
        if baseline is None:
            classification, delta, reason = "uncompared", None, "baseline_unmatched"
        elif key not in before or before_value is None:
            classification, delta, reason = "uncompared", None, "before_unavailable"
        elif after_value is None:
            classification, delta, reason = "uncompared", None, "after_unavailable"
        elif key not in directions:
            classification, delta, reason = "uncompared", None, "direction_unavailable"
        else:
            delta = after_value - before_value
            if delta == 0:
                classification, reason = "unchanged", "values_equal"
            else:
                direction = directions[key]
                improved = delta < 0 if direction == "lower" else delta > 0
                classification = "improved" if improved else "regressed"
                reason = f"{direction}_is_better"
        compared[key] = {
            "before": before_value if key in before else None,
            "after": after_value,
            "delta": delta,
            "classification": classification,
            "reason": reason,
        }
    return compared


def build_stage1_report(
    args: argparse.Namespace,
    evidence: Stage1Evidence,
    baseline_candidates: Sequence[Mapping[str, object]],
    *,
    directions: Mapping[str, Direction] = _DEFAULT_DIRECTIONS,
) -> dict[str, object]:
    """Build and validate the exact Stage1OperationalPerformanceReport.v1 shape."""
    selected_flow = cast("str", args.flow_id)
    evidence = replace(evidence, status=_status_cohort(selected_flow, evidence.status))
    readback, projection, joined = _authority_documents(selected_flow, evidence)
    delivery = _delivery_document(evidence, projection, joined)
    status = _status_document(evidence)
    report: dict[str, object] = {
        "input": {
            "schema_version": "1",
            "stage": "stage1",
            "flow_id": args.flow_id,
            "profile": args.profile,
            "env": args.env,
            "window_minutes": args.window_minutes,
            "label": args.label,
            "expected_account": args.expected_account,
            "baseline_label": args.baseline_label,
        },
        "authority_readback": readback,
        "delivery": delivery,
        "projection": projection,
        "status": status,
        "comparison": {"baseline_label": args.baseline_label, "metrics": {}},
    }
    baseline = match_baseline(
        baseline_candidates,
        baseline_label=cast("str", args.baseline_label),
        env=args.env,
        expected_account=args.expected_account,
        window_minutes=args.window_minutes,
    )
    cast("dict[str, object]", report["comparison"])["metrics"] = compare_metrics(
        _fixed_metric_values(report), baseline, directions
    )
    validate_stage1_report(report)
    return report


def _exact_keys(value: Mapping[str, object], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{name} has unexpected or missing fields")


def _object(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return cast("Mapping[str, object]", value)


def _objects(value: object, name: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return [_object(item, name) for item in cast("list[object]", value)]


def _nullable_state(value: object, name: str) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"{name} must be null or a nonblank string")


def _nullable_integer(value: object, name: str) -> None:
    if value is not None:
        _require_nonnegative_integer(value, name)


def _nullable_number(value: object, name: str) -> None:
    if value is not None:
        _require_nonnegative_number(value, name)


def _outcomes(value: object, name: str) -> None:
    outcomes = _object(value, name)
    for outcome, count in outcomes.items():
        if not outcome.strip():
            raise ValueError(f"{name} keys must be nonblank")
        _require_nonnegative_integer(count, f"{name}.{outcome}")


def _text_field(value: Mapping[str, object], key: str, path: str) -> str:
    selected = value[key]
    if not isinstance(selected, str):
        raise ValueError(f"{path}.{key} must be a string")
    return selected


def _validate_input(value: object) -> None:
    input_data = _object(value, "input")
    _exact_keys(
        input_data,
        {
            "schema_version",
            "stage",
            "flow_id",
            "profile",
            "env",
            "window_minutes",
            "label",
            "expected_account",
            "baseline_label",
        },
        "input",
    )
    if input_data["schema_version"] != "1" or input_data["stage"] != "stage1":
        raise ValueError("input schema_version and stage are fixed")
    flow_id = _text_field(input_data, "flow_id", "input")
    profile = _text_field(input_data, "profile", "input")
    environment = _text_field(input_data, "env", "input")
    label = _text_field(input_data, "label", "input")
    expected_account = _text_field(input_data, "expected_account", "input")
    baseline_label = _text_field(input_data, "baseline_label", "input")
    _require_canonical_uuid(flow_id, "flow_id")
    _require_positive_integer(input_data["window_minutes"], "window_minutes")
    _nonblank(profile)
    _nonblank(environment)
    _account(expected_account)
    _label(label)
    _label(baseline_label)


def _validate_readback(value: object) -> None:
    readback = _object(value, "authority_readback")
    _exact_keys(readback, {"plan", "units", "pointers"}, "authority_readback")
    plan = _object(readback["plan"], "authority_readback.plan")
    _exact_keys(plan, {"state", "evidence"}, "authority_readback.plan")
    _nullable_state(plan["state"], "authority_readback.plan.state")
    _require_evidence(_text_field(plan, "evidence", "authority_readback.plan"))
    for name, ordinal_name in (("units", "unit_ordinal"), ("pointers", "pointer_ordinal")):
        rows = _objects(readback[name], f"authority_readback.{name}")
        ordinals: list[int] = []
        for row in rows:
            _exact_keys(row, {ordinal_name, "state", "evidence"}, f"authority_readback.{name}")
            _require_positive_integer(row[ordinal_name], ordinal_name)
            ordinals.append(cast("int", row[ordinal_name]))
            _nullable_state(row["state"], f"authority_readback.{name}.state")
            _require_evidence(_text_field(row, "evidence", f"authority_readback.{name}"))
        if ordinals != sorted(set(ordinals)):
            raise ValueError(f"authority_readback.{name} ordinals must be unique and sorted")


def _validate_performance(delivery_value: object, projection_value: object) -> None:
    delivery = _object(delivery_value, "delivery")
    _exact_keys(delivery, {"invocation_count", "elapsed_ms", "athena_statement_count"}, "delivery")
    projection = _object(projection_value, "projection")
    _exact_keys(
        projection,
        {
            "unit_count",
            "merge_statements",
            "verification_statements",
            "total_statements",
            "athena_engine_ms",
            "by_unit",
        },
        "projection",
    )
    for container, names in (
        (delivery, ("invocation_count", "athena_statement_count")),
        (
            projection,
            ("unit_count", "merge_statements", "verification_statements", "total_statements"),
        ),
    ):
        for name in names:
            _nullable_integer(container[name], name)
    _nullable_number(delivery["elapsed_ms"], "elapsed_ms")
    _nullable_number(projection["athena_engine_ms"], "athena_engine_ms")
    unit_rows = _objects(projection["by_unit"], "projection.by_unit")
    unit_ordinals: list[int] = []
    for row in unit_rows:
        _exact_keys(
            row,
            {
                "unit_ordinal",
                "merge_statements",
                "verification_statements",
                "total_statements",
                "athena_engine_ms",
            },
            "projection.by_unit",
        )
        _require_positive_integer(row["unit_ordinal"], "unit_ordinal")
        unit_ordinals.append(cast("int", row["unit_ordinal"]))
        for name in ("merge_statements", "verification_statements", "total_statements"):
            _nullable_integer(row[name], f"projection.by_unit.{name}")
        _nullable_number(row["athena_engine_ms"], "projection.by_unit.athena_engine_ms")
    if unit_ordinals != sorted(set(unit_ordinals)):
        raise ValueError("projection.by_unit ordinals must be unique and sorted")


def _validate_status(value: object) -> None:
    status = _object(value, "status")
    _exact_keys(status, {"flows", "events", "outcomes", "by_surface"}, "status")
    _nullable_integer(status["flows"], "status.flows")
    _nullable_integer(status["events"], "status.events")
    _outcomes(status["outcomes"], "status.outcomes")
    by_surface = _object(status["by_surface"], "status.by_surface")
    _exact_keys(by_surface, set(STAGE1_SURFACES), "status.by_surface")
    for surface in STAGE1_SURFACES:
        surface_data = _object(by_surface[surface], f"status.by_surface.{surface}")
        _exact_keys(
            surface_data,
            {"flows", "events", "outcomes"},
            f"status.by_surface.{surface}",
        )
        _nullable_integer(surface_data["flows"], f"status.by_surface.{surface}.flows")
        _nullable_integer(surface_data["events"], f"status.by_surface.{surface}.events")
        _outcomes(surface_data["outcomes"], f"status.by_surface.{surface}.outcomes")


def _comparison_number(entry: Mapping[str, object], name: str) -> float | int | None:
    value = entry[name]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"comparison {name} must be a finite number or null")
    return value


def _validate_comparison_entry(dotted_key: str, raw_entry: object) -> None:
    if not dotted_key.strip():
        raise ValueError("comparison metric key must be nonblank")
    entry = _object(raw_entry, f"comparison.metrics.{dotted_key}")
    _exact_keys(
        entry,
        {"before", "after", "delta", "classification", "reason"},
        f"comparison.metrics.{dotted_key}",
    )
    before = _comparison_number(entry, "before")
    after = _comparison_number(entry, "after")
    delta = _comparison_number(entry, "delta")
    classification = entry["classification"]
    if classification not in {"improved", "regressed", "unchanged", "uncompared"}:
        raise ValueError("comparison classification is invalid")
    reason = entry["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("comparison reason must be nonblank")
    if classification == "uncompared":
        if delta is not None:
            raise ValueError("uncompared metric delta must be null")
    elif before is None or after is None or delta != after - before:
        raise ValueError("comparable metric requires exact after-minus-before delta")
    elif classification == "unchanged" and delta != 0:
        raise ValueError("unchanged metric requires zero delta")
    elif classification in {"improved", "regressed"} and delta == 0:
        raise ValueError("changed classification requires nonzero delta")


def _validate_comparison(value: object) -> None:
    comparison = _object(value, "comparison")
    _exact_keys(comparison, {"baseline_label", "metrics"}, "comparison")
    _label(_text_field(comparison, "baseline_label", "comparison"))
    metrics = _object(comparison["metrics"], "comparison.metrics")
    metric_keys = list(metrics)
    if metric_keys != sorted(metric_keys):
        raise ValueError("comparison metrics must be lexicographically ordered")
    for dotted_key, raw_entry in metrics.items():
        _validate_comparison_entry(dotted_key, raw_entry)


def validate_stage1_report(report: Mapping[str, object]) -> None:
    """Strictly validate exact keys and scalar constraints of the Stage1 schema."""
    _exact_keys(
        report,
        {"input", "authority_readback", "delivery", "projection", "status", "comparison"},
        "report",
    )
    _validate_input(report["input"])
    _validate_readback(report["authority_readback"])
    _validate_performance(report["delivery"], report["projection"])
    _validate_status(report["status"])
    _validate_comparison(report["comparison"])


def _contributions(
    client: Any, *, table: str, surface: str, since: datetime
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "TableName": table,
        "IndexName": "surface-sample-baseline",
        "KeyConditionExpression": "surface_partition = :s AND surface_sort >= :t",
        "ExpressionAttributeValues": {
            ":s": {"S": surface},
            ":t": {"S": since.isoformat()},
        },
    }
    while True:
        response = client.query(**kwargs)
        found.extend(json.loads(item["payload"]["S"]) for item in response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            return found
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def _baselines(client: Any, table: str, surface: str) -> list[dict[str, Any]]:
    response = client.query(
        TableName=table,
        KeyConditionExpression="partition_key = :p AND begins_with(sort_key, :b)",
        ExpressionAttributeValues={
            ":p": {"S": f"SURFACE#{surface}"},
            ":b": {"S": "BASELINE#"},
        },
        ScanIndexForward=False,
        Limit=48,
    )
    return [json.loads(item["payload"]["S"]) for item in response.get("Items", [])]


def _latencies_ms(events: list[dict[str, Any]]) -> list[float]:
    values: list[float] = []
    for event in events:
        if event.get("outcome") is None or event.get("clock_started_at") is None:
            continue
        started = datetime.fromisoformat(cast("str", event["clock_started_at"]))
        occurred = datetime.fromisoformat(cast("str", event["occurred_at"]))
        values.append((occurred - started).total_seconds() * 1_000)
    return values


def _distribution(values: list[float]) -> dict[str, float | int] | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min_ms": round(ordered[0], 1),
        "p50_ms": round(statistics.median(ordered), 1),
        "p90_ms": round(ordered[max(0, int(len(ordered) * 0.9) - 1)], 1),
        "max_ms": round(ordered[-1], 1),
    }


def build_stage0_report(args: argparse.Namespace, client: Any, now: datetime) -> dict[str, object]:
    """Build the retained Stage0 report while preserving its existing surface detail."""
    since = now - timedelta(minutes=args.window_minutes)
    table = f"adlytics-{args.env}-operational-status"
    surfaces: dict[str, object] = {}
    flows_by_surface: dict[str, int] = {}
    all_flows: set[str] = set()
    all_outcomes: Counter[str] = Counter()
    event_total = 0
    comparison_values: dict[str, float | int | None] = {}
    for surface in STAGE0_SURFACES:
        events = _contributions(client, table=table, surface=surface, since=since)
        flows = {cast("str", event["flow_id"]) for event in events}
        outcomes = Counter(
            cast("str", event["outcome"]) for event in events if event.get("outcome") is not None
        )
        flows_by_surface[surface] = len(flows)
        all_flows.update(flows)
        all_outcomes.update(outcomes)
        event_total += len(events)
        surfaces[surface] = {
            "events": len(events),
            "flows": len(flows),
            "terminal_outcomes": dict(sorted(outcomes.items())),
            "settlement_latency": _distribution(_latencies_ms(events)),
            "baseline_windows": _baselines(client, table, surface),
        }
    comparison_values["status.flows"] = len(all_flows)
    comparison_values["status.events"] = event_total
    comparison_values.update(
        {f"status.outcomes.{key}": value for key, value in sorted(all_outcomes.items())}
    )
    enqueue_flows = flows_by_surface["preset_enqueue"]
    bake_flows = flows_by_surface["preset_bake"]
    return {
        "input": {
            "schema_version": "1",
            "stage": "stage0",
            "profile": args.profile,
            "env": args.env,
            "window_minutes": args.window_minutes,
            "label": args.label,
            "expected_account": args.expected_account,
        },
        "contracts": dict(_CONTRACTS),
        "label": args.label,
        "generated_at": now.isoformat(),
        "window_minutes": args.window_minutes,
        "surfaces": surfaces,
        "comparison_values": dict(sorted(comparison_values.items())),
        "amplification": {
            "bake_flows_per_enqueue_admission": (
                None if enqueue_flows == 0 else round(bake_flows / enqueue_flows, 2)
            ),
            "note": (
                "Global declaration fan-out is the before-side of ADR-075 Stage 2; "
                "Stage 1 comparisons use only matching canonical dotted metrics."
            ),
        },
    }


def read_baseline_reports(directory: Path = _EVIDENCE) -> list[Mapping[str, object]]:
    """Read retained local JSON reports; malformed or non-object files fail closed."""
    if not directory.exists():
        return []
    reports: list[Mapping[str, object]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            reports.append(cast("Mapping[str, object]", value))
    return reports


def write_report(
    report: Mapping[str, object],
    *,
    label: str,
    now: datetime,
    directory: Path = _EVIDENCE,
) -> tuple[Path, Mapping[str, object]]:
    """Create one confined report exclusively and parse the exact bytes back."""
    _label(label)
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("report timestamp must be UTC-aware")
    directory.mkdir(parents=True, exist_ok=True)
    root = directory.resolve()
    output = (root / f"{now.strftime('%Y%m%dT%H%M%SZ')}-{label}.json").resolve()
    if output.parent != root:
        raise ValueError("report output escaped the evidence directory")
    serialized = json.dumps(report, indent=1, sort_keys=True, allow_nan=False) + "\n"
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(serialized)
    readback = json.loads(output.read_text(encoding="utf-8"))
    if readback != report:
        raise OSError("report readback differs from the written report")
    return output, cast("Mapping[str, object]", readback)


def _print_stage0(report: Mapping[str, object], args: argparse.Namespace) -> None:
    print(f"\n{args.label} — last {args.window_minutes} min")
    surfaces = cast("Mapping[str, Mapping[str, object]]", report["surfaces"])
    for surface in STAGE0_SURFACES:
        data = surfaces[surface]
        latency = cast("Mapping[str, object] | None", data["settlement_latency"])
        windows = cast("Sequence[Mapping[str, object]]", data["baseline_windows"])
        print(
            f"  {surface:38} flows={cast('int', data['flows']):3} "
            f"events={cast('int', data['events']):3} "
            f"outcomes={data['terminal_outcomes']} "
            f"p50={'-' if latency is None else str(latency['p50_ms']) + 'ms'} "
            f"windows={len(windows)} "
            f"window_samples={sum(cast('int', window['sample_count']) for window in windows)}"
        )
    amplification = cast("Mapping[str, object]", report["amplification"])
    print(
        "  amplification: "
        f"{amplification['bake_flows_per_enqueue_admission']} "
        "bake flows per enqueue admission"
    )


def _verified_session(args: argparse.Namespace) -> Any:
    session = boto3.Session(profile_name=args.profile)
    account = session.client("sts").get_caller_identity()["Account"]
    if account != args.expected_account:
        raise SystemExit("unexpected account")
    return session


# --- Live Stage 1 evidence readers ---------------------------------------------------------------
#
# Three readers, three sources, joined only through owner-persisted public identity:
#   1. the retained status projection (the protected API's own table), read through its declared
#      per-surface contribution lane bounded by the window — never a scan;
#   2. retained owner authority in the publications table, selected only by exact keys derived
#      from the status proof: the flow head, its lifecycle event items, the run item, and its
#      attempt items;
#   3. the structured delivery terminal, which today carries no Lambda elapsed milliseconds with a
#      public identity, so the terminal family is honestly reported missing (see the delivery log).


@dataclass(frozen=True, slots=True)
class _Window:
    since: datetime
    until: datetime


def _contribution_documents(
    client: Any, table: str, *, surface: str, window: _Window
) -> list[dict[str, Any]]:
    return [
        document
        for document in _contributions(client, table=table, surface=surface, since=window.since)
        if datetime.fromisoformat(cast("str", document["occurred_at"])) <= window.until
    ]


def status_observations(documents: Sequence[Mapping[str, Any]]) -> tuple[StatusObservation, ...]:
    """Fold window-bounded contribution documents into one observation per public flow.

    The flow's identity, state, and outcome come from its highest-sequence event in the window;
    ``event_count`` is the number of applied events observed for it.
    """
    by_flow: dict[str, list[Mapping[str, Any]]] = {}
    for document in documents:
        by_flow.setdefault(cast("str", document["flow_id"]), []).append(document)
    observations: list[StatusObservation] = []
    for flow_id, events in sorted(by_flow.items()):
        latest = max(events, key=lambda event: cast("int", event["transition_sequence"]))
        observations.append(
            StatusObservation(
                identity=IdentityProof(
                    flow_id,
                    cast("str", latest["execution_id"]),
                    cast("str | None", latest.get("predecessor_execution_id")),
                ),
                surface=cast("str", latest["surface"]),
                state=cast("str | None", latest.get("state")),
                outcome=cast("str | None", latest.get("outcome")),
                event_count=len(events),
                parent_flow_id=cast("str | None", latest.get("parent_flow_id")),
            )
        )
    return tuple(observations)


@dataclass(frozen=True, slots=True)
class ProjectionStatusReader:
    """Family 1: the retained status projection through its per-surface contribution lane."""

    client: Any
    table: str

    def read(self, flow_id: str, since: datetime, until: datetime) -> Sequence[StatusObservation]:
        """Read every Stage-1 surface lane in the window; the cohort is selected later by proof."""
        del flow_id  # the cohort is rooted by build_stage1_report, not by a lane filter
        documents: list[Mapping[str, Any]] = []
        window = _Window(since, until)
        for surface in STAGE1_SURFACES:
            documents.extend(
                _contribution_documents(self.client, self.table, surface=surface, window=window)
            )
        return status_observations(documents)


_INLINE_SURFACE = "semantic_inline_table_run"
_RUN_INTERNAL_KEY_LENGTH = 4


@dataclass(frozen=True, slots=True)
class RunAuthorityFacts:
    """The owner facts one inline run's retained RUN#/ATTEMPT# items yield."""

    flow_id: str
    execution_id: str
    physical_table_id: str
    state: str
    merge_statements: int
    verification_statements: int
    athena_engine_ms: int
    attempt_statement_count: int


def run_authority_facts(
    run: Mapping[str, object], attempts: Sequence[Mapping[str, object]]
) -> RunAuthorityFacts:
    """Derive one run's owner facts from plain RUN# and ATTEMPT# items, never from status."""
    kinds = Counter(str(attempt.get("statement_kind", "")) for attempt in attempts)
    return RunAuthorityFacts(
        flow_id=str(run["status_flow_binding"]),
        execution_id=str(run["execution_binding"]),
        physical_table_id=str(run["physical_table_id"]),
        state=str(run["state"]),
        merge_statements=kinds["merge"],
        verification_statements=kinds["verification"],
        athena_engine_ms=sum(
            int(cast("int", attempt["engine_ms"]))
            for attempt in attempts
            if isinstance(attempt.get("engine_ms"), int)
        ),
        attempt_statement_count=len(attempts),
    )


@dataclass(frozen=True, slots=True)
class RetainedAuthorityReader:
    """Family 2: owner authority through exact keys the status proof names, never a scan."""

    client: Any
    publications_table: str
    leases_table: str

    def _reader(self) -> Any:
        from adapters.outbound.dynamodb.stage1_writer_authority import (
            Stage1WriterAuthorityReader,
        )

        return Stage1WriterAuthorityReader(self.publications_table, self.leases_table, self.client)

    def _run_item_for(self, flow_id: str, reader: Any) -> Mapping[str, object] | None:
        from domain.operations.table_attempt import (
            RunIdentityRequest,
            WriterKind,
            run_identity,
        )

        response = self.client.query(
            TableName=self.publications_table,
            KeyConditionExpression="partition_key = :p AND begins_with(record_key, :e)",
            ExpressionAttributeValues={
                ":p": {"S": f"STATUS_FLOW#{flow_id}"},
                ":e": {"S": "EVENT#"},
            },
            ConsistentRead=True,
        )
        for item in response.get("Items", []):
            members = [member.get("S") for member in item.get("internal_key", {}).get("L", [])]
            if len(members) != _RUN_INTERNAL_KEY_LENGTH or any(m is None for m in members):
                continue
            writer_kind, operation_id, table_id, execution_id = cast("list[str]", members)
            run_id = run_identity(
                RunIdentityRequest(WriterKind(writer_kind), operation_id, execution_id, table_id)
            )
            run = reader.run_item(table_id, run_id)
            if run is not None:
                return run
        return None

    def read(self, status: Sequence[StatusObservation]) -> Sequence[AuthorityObservation]:
        """Resolve inline-run authority for every run flow in the status proof."""
        reader = self._reader()
        facts: list[RunAuthorityFacts] = []
        for observation in status:
            if observation.surface != _INLINE_SURFACE:
                continue
            head = reader.head(observation.identity.flow_id)
            if head is None:
                continue
            run = self._run_item_for(observation.identity.flow_id, reader)
            if run is None:
                continue
            attempts = [
                item
                for reference in cast("list[object]", run.get("attempt_references", []))
                if isinstance(reference, str)
                and (item := reader.attempt_item(str(run["physical_table_id"]), reference))
                is not None
            ]
            facts.append(run_authority_facts(run, attempts))
        ordered = sorted(facts, key=lambda fact: fact.physical_table_id.encode("utf-8"))
        # Every run of this status proof is a "unit" of the transitional inline composition; the
        # owner's stable order is ascending UTF-8 of the physical table id (the §5 unit order).
        return tuple(
            AuthorityObservation(
                identity=IdentityProof(
                    fact.flow_id,
                    fact.execution_id,
                    next(
                        (
                            item.identity.predecessor_execution_id
                            for item in status
                            if item.identity.flow_id == fact.flow_id
                        ),
                        None,
                    ),
                ),
                kind="unit",
                state=fact.state,
                evidence="complete",
                owner_ordinal=ordinal,
                merge_statements=fact.merge_statements,
                verification_statements=fact.verification_statements,
                athena_engine_ms=fact.athena_engine_ms,
                attempt_statement_count=fact.attempt_statement_count,
            )
            for ordinal, fact in enumerate(ordered, start=1)
        )


class CloudWatchDeliveryTerminalReader:
    """Family 3: structured ``ingest_delivery`` terminals joined by exact public identity.

    The delivery terminal record carries its own ``elapsed_ms`` and the owner-persisted
    public ``(flow_id, execution_id)`` pair of every inline run the invocation advanced
    (``details.run_flows``). A terminal is admitted only when one of its pairs exactly
    matches a cohort status identity — never by timestamp overlap, count, or any other
    heuristic, which the contract forbids. Records that predate the identity-bearing
    terminal simply never match, keeping older windows honestly ``missing``.
    """

    __slots__ = ("client", "log_group")

    def __init__(self, client: Any, log_group: str) -> None:
        if not log_group.strip():
            raise ValueError("delivery terminal reader requires the exact log group name")
        self.client = client
        self.log_group = log_group

    def read(  # noqa: PLR0917 -- the seam carries both families and the closed window
        self,
        status: Sequence[StatusObservation],
        authority: Sequence[AuthorityObservation],
        since: datetime,
        until: datetime,
    ) -> Sequence[DeliveryTerminal]:
        """Return one terminal per invocation whose run identity matches the cohort exactly."""
        del authority
        identities = {
            (
                item.identity.flow_id,
                item.identity.execution_id,
                item.identity.predecessor_execution_id,
            ): item.identity
            for item in status
        }
        terminals: list[DeliveryTerminal] = []
        token: str | None = None
        while True:
            request: dict[str, object] = {
                "logGroupName": self.log_group,
                "startTime": int(since.timestamp() * 1000),
                "endTime": int(until.timestamp() * 1000),
                "filterPattern": '{ $.operation = "ingest_delivery" && $.phase = "terminal" }',
            }
            if token is not None:
                request["nextToken"] = token
            response = self.client.filter_log_events(**request)
            next_token = response.get("nextToken")
            # CloudWatch can return the same token to signal that the requested page did
            # not advance. Such a response may repeat the prior page, so detect it before
            # admitting any events; otherwise one invocation and its elapsed time count twice.
            if token is not None and next_token == token:
                return tuple(terminals)
            for event in response.get("events", []):
                terminal = _delivery_terminal(str(event.get("message", "")), identities)
                if terminal is not None:
                    terminals.append(terminal)
            if next_token is None:
                return tuple(terminals)
            token = str(next_token)


def _delivery_terminal(
    message: str, identities: Mapping[tuple[str, str, str | None], IdentityProof]
) -> DeliveryTerminal | None:
    """Parse one structured terminal and join it by exact run identity, or return nothing."""
    try:
        record = json.loads(message.strip())
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    details = cast("dict[str, object]", record)
    elapsed = details.get("elapsed_ms")
    run_flows = details.get("run_flows")
    if (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, int | float)
        or elapsed < 0
        or not isinstance(run_flows, list)
    ):
        return None
    for member in cast("list[object]", run_flows):
        if not isinstance(member, dict):
            continue
        pair = cast("dict[str, object]", member)
        if "predecessor_execution_id" not in pair:
            continue
        key = (
            str(pair.get("flow_id", "")),
            str(pair.get("execution_id", "")),
            (
                None
                if pair.get("predecessor_execution_id") is None
                else str(pair.get("predecessor_execution_id"))
            ),
        )
        matched = identities.get(key)
        if matched is not None:
            return DeliveryTerminal(identity=matched, elapsed_ms=elapsed)
    return None


def live_stage1_readers(session: Any, env: str) -> EvidenceReaders:
    """Compose the three live readers for one verified deployment from the verified session."""
    dynamodb = session.client("dynamodb")
    return EvidenceReaders(
        status=ProjectionStatusReader(dynamodb, f"adlytics-{env}-operational-status"),
        authority=RetainedAuthorityReader(
            dynamodb,
            f"adlytics-{env}-v2-semantic-publications",
            f"adlytics-{env}-v2-semantic-leases",
        ),
        logs=CloudWatchDeliveryTerminalReader(
            session.client("logs"), f"/aws/lambda/adlytics-{env}-IngestionFunction"
        ),
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    evidence_readers: EvidenceReaders | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    evidence_directory: Path = _EVIDENCE,
) -> int:
    """Run the approved stage after parser and account validation."""
    args = parse_args(argv)
    session = _verified_session(args)
    now = clock()
    if args.stage == "stage0":
        report = build_stage0_report(args, session.client("dynamodb"), now)
    else:
        readers = evidence_readers or live_stage1_readers(session, args.env)
        evidence = readers.read(
            cast("str", args.flow_id), now - timedelta(minutes=args.window_minutes), now
        )
        report = build_stage1_report(args, evidence, read_baseline_reports(evidence_directory))
    output, readback = write_report(report, label=args.label, now=now, directory=evidence_directory)
    if readback != report:
        raise SystemExit("report readback failed")
    print(f"raw report: {output.relative_to(Path(__file__).parent.parent)}")
    if args.stage == "stage0":
        _print_stage0(report, args)
    else:
        print(f"stage=stage1 label={args.label} window_minutes={args.window_minutes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
