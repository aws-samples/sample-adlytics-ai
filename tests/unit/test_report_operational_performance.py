# SPDX-License-Identifier: MIT-0
"""Pure Stage0/Stage1 operational performance reporter contract coverage."""

from __future__ import annotations

# The live-reader tests exercise the reporter's private join and cohort seams on purpose.
# pyright: reportPrivateUsage=false
import argparse
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
from validation import report_operational_performance as reporter  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

_PLAN_FLOW = "68cb3192-c252-5fed-bbe8-8f0ab98f74b6"
_UNIT_FLOW_A = "960cfeca-0bad-57fd-b4b0-383e15d59085"
_UNIT_FLOW_B = "11111111-1111-4111-8111-111111111111"
_POINTER_FLOW = "e4de2f19-58db-5f51-b924-b4e5118d4db3"
_PLAN_EXECUTION = "76db826e-6da1-57ba-b170-75d627f053a3"
_UNIT_EXECUTION_A = "ef642f3b-65f1-580f-b01b-e2b7102b98f9"
_UNIT_EXECUTION_B = "22222222-2222-4222-8222-222222222222"
_POINTER_EXECUTION = "bf1bc15f-7b72-54db-8c7a-50682ce6810e"
_OTHER_EXECUTION = "33333333-3333-4333-8333-333333333333"
_WRONG_PREDECESSOR = "44444444-4444-4444-8444-444444444444"
_UNRELATED_FLOW = "55555555-5555-4555-8555-555555555555"
_UNRELATED_EXECUTION = "66666666-6666-4666-8666-666666666666"
_NOW = datetime(2026, 9, 20, 12, 30, tzinfo=UTC)


def _argv(stage: str = "stage1") -> list[str]:
    values = [
        "--stage",
        stage,
        "--profile",
        "fictional-readonly",
        "--env",
        "fictional-dev",
        "--window-minutes",
        "30",
        "--label",
        f"{stage}-fictional",
        "--expected-account",
        "000000000000",
    ]
    if stage == "stage1":
        values.extend(["--flow-id", _PLAN_FLOW, "--baseline-label", "stage0-fictional"])
    return values


@pytest.fixture
def stage1_args() -> argparse.Namespace:
    """Return a fully explicit fictional Stage1 command."""
    return reporter.parse_args(_argv())


@pytest.fixture
def complete_evidence() -> reporter.Stage1Evidence:
    """Return all three evidence families with exact public lineage proof."""
    plan = reporter.IdentityProof(_PLAN_FLOW, _PLAN_EXECUTION, None)
    unit = reporter.IdentityProof(_UNIT_FLOW_A, _UNIT_EXECUTION_A, None)
    pointer = reporter.IdentityProof(_POINTER_FLOW, _POINTER_EXECUTION, None)
    return reporter.Stage1Evidence(
        status=(
            reporter.StatusObservation(
                plan, "semantic_projection_plan", "completed", "succeeded", 3
            ),
            reporter.StatusObservation(
                unit,
                "semantic_projection_unit",
                "completed",
                "succeeded",
                2,
                parent_flow_id=_PLAN_FLOW,
            ),
            reporter.StatusObservation(
                pointer,
                "semantic_servable_pointer",
                "completed",
                "succeeded",
                1,
                parent_flow_id=_UNIT_FLOW_A,
            ),
        ),
        authority=(
            reporter.AuthorityObservation(
                plan,
                "plan",
                "completed",
                "complete",
                attempt_statement_count=2,
            ),
            reporter.AuthorityObservation(
                unit,
                "unit",
                "attested",
                "complete",
                owner_ordinal=1,
                merge_statements=1,
                verification_statements=1,
                athena_engine_ms=125.5,
            ),
            reporter.AuthorityObservation(
                pointer,
                "pointer",
                "completed",
                "complete",
                owner_ordinal=1,
            ),
        ),
        terminals=(reporter.DeliveryTerminal(plan, elapsed_ms=240.0),),
        status_evidence="complete",
        authority_evidence="complete",
        terminal_evidence="complete",
    )


def _baseline(**overrides: object) -> dict[str, object]:
    command: dict[str, object] = {
        "stage": "stage0",
        "label": "stage0-fictional",
        "env": "fictional-dev",
        "expected_account": "000000000000",
        "window_minutes": 30,
    }
    command.update(overrides)
    return {
        "input": command,
        "contracts": {
            "clock": "DataFlowMeasurement.v1",
            "eligibility": "DataFlowMeasurement.v1.eligible_outcomes/excluded_outcomes",
            "category": "UsageByCategory.v1",
        },
        "comparison_values": {
            "delivery.invocation_count": 2,
            "delivery.elapsed_ms": 300.0,
            "delivery.athena_statement_count": 4,
            "projection.unit_count": 1,
            "projection.merge_statements": 3,
            "projection.verification_statements": 1,
            "projection.total_statements": 4,
            "projection.athena_engine_ms": 200.0,
            "status.flows": 3,
            "status.events": 6,
        },
    }


def test_parser_requires_explicit_stage_contract_and_rejects_cross_stage_options() -> None:
    stage0 = reporter.parse_args(_argv("stage0"))
    stage1 = reporter.parse_args(_argv("stage1"))

    assert (stage0.stage, stage0.flow_id, stage0.baseline_label) == ("stage0", None, None)
    assert (stage1.stage, stage1.flow_id, stage1.baseline_label) == (
        "stage1",
        _PLAN_FLOW,
        "stage0-fictional",
    )
    with pytest.raises(SystemExit):
        reporter.parse_args([*_argv("stage0"), "--flow-id", _PLAN_FLOW])
    missing_baseline = _argv("stage1")[:-2]
    with pytest.raises(SystemExit):
        reporter.parse_args(missing_baseline)


@pytest.mark.parametrize(
    ("option", "bad_value"),
    [
        ("--profile", "default"),
        ("--env", " "),
        ("--window-minutes", "0"),
        ("--label", "Unsafe_Label"),
        ("--expected-account", "123"),
        ("--flow-id", _PLAN_FLOW.upper()),
    ],
)
def test_parser_rejects_malformed_explicit_values(option: str, bad_value: str) -> None:
    values = _argv()
    index = values.index(option) + 1
    values[index] = bad_value

    with pytest.raises(SystemExit):
        reporter.parse_args(values)


def test_parser_rejects_duplicate_singleton_values() -> None:
    with pytest.raises(SystemExit):
        reporter.parse_args([*_argv(), "--baseline-label", "stage0-other"])
    with pytest.raises(SystemExit):
        reporter.parse_args([*_argv(), "--stage", "stage1"])


def test_identity_proof_requires_public_uuid_execution_and_predecessor() -> None:
    with pytest.raises(ValueError, match="execution_id"):
        reporter.IdentityProof(_PLAN_FLOW, "internal-selector", None)
    with pytest.raises(ValueError, match="predecessor_execution_id"):
        reporter.IdentityProof(_PLAN_FLOW, _PLAN_EXECUTION, "internal-predecessor")


def test_stage1_report_has_exact_schema_four_surfaces_and_joined_totals(
    stage1_args: argparse.Namespace,
    complete_evidence: reporter.Stage1Evidence,
) -> None:
    report = reporter.build_stage1_report(stage1_args, complete_evidence, [_baseline()])

    assert list(report) == [
        "input",
        "authority_readback",
        "delivery",
        "projection",
        "status",
        "comparison",
    ]
    status = cast("dict[str, object]", report["status"])
    by_surface = cast("dict[str, dict[str, object]]", status["by_surface"])
    assert tuple(by_surface) == reporter.STAGE1_SURFACES
    assert by_surface["semantic_inline_table_run"] == {
        "flows": 0,
        "events": 0,
        "outcomes": {},
    }
    assert report["delivery"] == {
        "invocation_count": 1,
        "elapsed_ms": 240.0,
        "athena_statement_count": 2,
    }
    projection = cast("dict[str, object]", report["projection"])
    assert projection == {
        "unit_count": 1,
        "merge_statements": 1,
        "verification_statements": 1,
        "total_statements": 2,
        "athena_engine_ms": 125.5,
        "by_unit": [
            {
                "unit_ordinal": 1,
                "merge_statements": 1,
                "verification_statements": 1,
                "total_statements": 2,
                "athena_engine_ms": 125.5,
            }
        ],
    }


def test_status_counts_only_the_exact_rooted_flow_cohort(
    stage1_args: argparse.Namespace,
    complete_evidence: reporter.Stage1Evidence,
) -> None:
    unrelated = reporter.StatusObservation(
        reporter.IdentityProof(_UNRELATED_FLOW, _UNRELATED_EXECUTION, None),
        "semantic_projection_plan",
        "completed",
        "succeeded",
        99,
    )
    evidence = replace(complete_evidence, status=(*complete_evidence.status, unrelated))

    report = reporter.build_stage1_report(stage1_args, evidence, [_baseline()])
    status = cast("dict[str, object]", report["status"])

    assert status["flows"] == 3
    assert status["events"] == 6


def test_stable_owner_ordinals_hide_selectors_and_ignore_reader_order(
    stage1_args: argparse.Namespace,
    complete_evidence: reporter.Stage1Evidence,
) -> None:
    second_identity = reporter.IdentityProof(_UNIT_FLOW_B, _UNIT_EXECUTION_B, None)
    second_status = reporter.StatusObservation(
        second_identity,
        "semantic_projection_unit",
        "completed",
        "succeeded",
        2,
        parent_flow_id=_PLAN_FLOW,
    )
    second_authority = reporter.AuthorityObservation(
        second_identity,
        "unit",
        "attested",
        "complete",
        owner_ordinal=2,
        merge_statements=1,
        verification_statements=1,
        athena_engine_ms=50,
    )
    evidence = reporter.Stage1Evidence(
        status=(*complete_evidence.status, second_status),
        authority=(second_authority, *complete_evidence.authority),
        terminals=complete_evidence.terminals,
        status_evidence="complete",
        authority_evidence="complete",
        terminal_evidence="complete",
    )
    plan = evidence.authority[1]
    adjusted_plan = reporter.AuthorityObservation(
        plan.identity,
        "plan",
        plan.state,
        plan.evidence,
        attempt_statement_count=4,
    )
    evidence = reporter.Stage1Evidence(
        status=evidence.status,
        authority=(second_authority, adjusted_plan, *evidence.authority[2:]),
        terminals=evidence.terminals,
        status_evidence="complete",
        authority_evidence="complete",
        terminal_evidence="complete",
    )

    report = reporter.build_stage1_report(stage1_args, evidence, [_baseline()])
    projection = cast("dict[str, object]", report["projection"])
    by_unit = cast("list[dict[str, object]]", projection["by_unit"])

    assert [row["unit_ordinal"] for row in by_unit] == [1, 2]
    assert all(
        set(row)
        == {
            "unit_ordinal",
            "merge_statements",
            "verification_statements",
            "total_statements",
            "athena_engine_ms",
        }
        for row in by_unit
    )
    assert projection["total_statements"] == 4


def test_predecessor_mismatch_makes_authority_and_metrics_unavailable(
    stage1_args: argparse.Namespace,
    complete_evidence: reporter.Stage1Evidence,
) -> None:
    original = complete_evidence.authority[1]
    mismatched = reporter.AuthorityObservation(
        reporter.IdentityProof(_UNIT_FLOW_A, _UNIT_EXECUTION_A, _WRONG_PREDECESSOR),
        "unit",
        original.state,
        "complete",
        owner_ordinal=1,
        merge_statements=1,
        verification_statements=1,
        athena_engine_ms=125.5,
    )
    evidence = reporter.Stage1Evidence(
        status=complete_evidence.status,
        authority=(complete_evidence.authority[0], mismatched, complete_evidence.authority[2]),
        terminals=complete_evidence.terminals,
        status_evidence="complete",
        authority_evidence="complete",
        terminal_evidence="complete",
    )

    report = reporter.build_stage1_report(stage1_args, evidence, [_baseline()])
    readback = cast("dict[str, object]", report["authority_readback"])
    projection = cast("dict[str, object]", report["projection"])

    assert cast("list[dict[str, object]]", readback["units"])[0]["evidence"] == "unmatched"
    assert projection["unit_count"] is None
    assert projection["athena_engine_ms"] is None


def test_statement_total_disagreement_nulls_both_authoritative_totals(
    stage1_args: argparse.Namespace,
    complete_evidence: reporter.Stage1Evidence,
) -> None:
    plan = complete_evidence.authority[0]
    mismatched_plan = reporter.AuthorityObservation(
        plan.identity,
        "plan",
        plan.state,
        plan.evidence,
        attempt_statement_count=3,
    )
    evidence = reporter.Stage1Evidence(
        status=complete_evidence.status,
        authority=(mismatched_plan, *complete_evidence.authority[1:]),
        terminals=complete_evidence.terminals,
        status_evidence="complete",
        authority_evidence="complete",
        terminal_evidence="complete",
    )

    report = reporter.build_stage1_report(stage1_args, evidence, [_baseline()])

    assert cast("dict[str, object]", report["delivery"])["athena_statement_count"] is None
    assert cast("dict[str, object]", report["projection"])["total_statements"] is None


def test_terminal_identity_mismatch_never_becomes_delivery_evidence(
    stage1_args: argparse.Namespace,
    complete_evidence: reporter.Stage1Evidence,
) -> None:
    evidence = reporter.Stage1Evidence(
        status=complete_evidence.status,
        authority=complete_evidence.authority,
        terminals=(
            reporter.DeliveryTerminal(
                reporter.IdentityProof(_PLAN_FLOW, _OTHER_EXECUTION, None), elapsed_ms=10
            ),
        ),
        status_evidence="complete",
        authority_evidence="complete",
        terminal_evidence="complete",
    )

    report = reporter.build_stage1_report(stage1_args, evidence, [_baseline()])
    delivery = cast("dict[str, object]", report["delivery"])

    assert delivery["invocation_count"] is None
    assert delivery["elapsed_ms"] is None


def test_empty_evidence_with_matching_baseline_is_never_zero_or_improved(
    stage1_args: argparse.Namespace,
) -> None:
    report = reporter.build_stage1_report(
        stage1_args, reporter.Stage1Evidence((), (), ()), [_baseline()]
    )
    delivery = cast("dict[str, object]", report["delivery"])
    readback = cast("dict[str, object]", report["authority_readback"])
    status = cast("dict[str, object]", report["status"])
    metrics = cast(
        "dict[str, dict[str, object]]",
        cast("dict[str, object]", report["comparison"])["metrics"],
    )

    assert delivery == {
        "invocation_count": None,
        "elapsed_ms": None,
        "athena_statement_count": None,
    }
    assert cast("dict[str, object]", readback["plan"])["evidence"] == "missing"
    assert status["flows"] is None
    assert status["events"] is None
    assert "improved" not in {entry["classification"] for entry in metrics.values()}


def test_delivery_terminal_requires_matching_owner_authority(
    stage1_args: argparse.Namespace,
    complete_evidence: reporter.Stage1Evidence,
) -> None:
    evidence = reporter.Stage1Evidence(
        status=complete_evidence.status,
        authority=(),
        terminals=complete_evidence.terminals,
        status_evidence="complete",
        authority_evidence="missing",
        terminal_evidence="complete",
    )

    report = reporter.build_stage1_report(stage1_args, evidence, [_baseline()])
    delivery = cast("dict[str, object]", report["delivery"])

    assert delivery["invocation_count"] is None
    assert delivery["elapsed_ms"] is None


def test_duplicate_authority_identity_is_conflicting_and_not_counted(
    stage1_args: argparse.Namespace,
    complete_evidence: reporter.Stage1Evidence,
) -> None:
    unit = complete_evidence.authority[1]
    duplicate = reporter.AuthorityObservation(
        unit.identity,
        "unit",
        unit.state,
        "complete",
        owner_ordinal=2,
        merge_statements=unit.merge_statements,
        verification_statements=unit.verification_statements,
        athena_engine_ms=unit.athena_engine_ms,
    )
    evidence = reporter.Stage1Evidence(
        status=complete_evidence.status,
        authority=(*complete_evidence.authority, duplicate),
        terminals=complete_evidence.terminals,
        status_evidence="complete",
        authority_evidence="complete",
        terminal_evidence="complete",
    )

    report = reporter.build_stage1_report(stage1_args, evidence, [_baseline()])
    readback = cast("dict[str, object]", report["authority_readback"])
    unit_rows = cast("list[dict[str, object]]", readback["units"])
    projection = cast("dict[str, object]", report["projection"])

    assert [row["evidence"] for row in unit_rows] == ["conflicting", "conflicting"]
    assert projection["unit_count"] is None
    assert projection["total_statements"] is None


def test_baseline_matching_requires_one_exact_contract_identity() -> None:
    exact = _baseline()

    assert (
        reporter.match_baseline(
            [exact],
            baseline_label="stage0-fictional",
            env="fictional-dev",
            expected_account="000000000000",
            window_minutes=30,
        )
        is exact
    )
    assert (
        reporter.match_baseline(
            [exact, _baseline()],
            baseline_label="stage0-fictional",
            env="fictional-dev",
            expected_account="000000000000",
            window_minutes=30,
        )
        is None
    )
    assert (
        reporter.match_baseline(
            [_baseline(env="fictional-other")],
            baseline_label="stage0-fictional",
            env="fictional-dev",
            expected_account="000000000000",
            window_minutes=30,
        )
        is None
    )


def test_dotted_comparison_is_sorted_excludes_by_unit_and_uncertainty_never_improves(
    stage1_args: argparse.Namespace,
    complete_evidence: reporter.Stage1Evidence,
) -> None:
    report = reporter.build_stage1_report(stage1_args, complete_evidence, [_baseline()])
    comparison = cast("dict[str, object]", report["comparison"])
    metrics = cast("dict[str, dict[str, object]]", comparison["metrics"])

    assert list(metrics) == sorted(metrics)
    assert not any("by_unit" in key for key in metrics)
    assert metrics["delivery.elapsed_ms"] == {
        "before": 300.0,
        "after": 240.0,
        "delta": -60.0,
        "classification": "improved",
        "reason": "lower_is_better",
    }
    assert metrics["projection.unit_count"]["classification"] == "uncompared"
    assert metrics["projection.unit_count"]["reason"] == "direction_unavailable"
    assert metrics["status.events"]["classification"] == "uncompared"
    assert metrics["status.events"]["delta"] is None


def test_unmatched_baseline_keeps_every_metric_uncompared(
    stage1_args: argparse.Namespace,
    complete_evidence: reporter.Stage1Evidence,
) -> None:
    report = reporter.build_stage1_report(
        stage1_args, complete_evidence, [_baseline(env="fictional-other")]
    )
    metrics = cast(
        "dict[str, dict[str, object]]",
        cast("dict[str, object]", report["comparison"])["metrics"],
    )

    assert {entry["classification"] for entry in metrics.values()} == {"uncompared"}
    assert {entry["reason"] for entry in metrics.values()} == {"baseline_unmatched"}
    assert {entry["delta"] for entry in metrics.values()} == {None}


def test_strict_schema_rejects_extra_and_invalid_nested_fields(
    stage1_args: argparse.Namespace,
    complete_evidence: reporter.Stage1Evidence,
) -> None:
    report = reporter.build_stage1_report(stage1_args, complete_evidence, [_baseline()])
    cast("dict[str, object]", report["delivery"])["inferred"] = 1

    with pytest.raises(ValueError, match="unexpected or missing fields"):
        reporter.validate_stage1_report(report)

    report = reporter.build_stage1_report(stage1_args, complete_evidence, [_baseline()])
    metrics = cast(
        "dict[str, dict[str, object]]",
        cast("dict[str, object]", report["comparison"])["metrics"],
    )
    metrics["delivery.elapsed_ms"]["delta"] = None
    with pytest.raises(ValueError, match="exact after-minus-before delta"):
        reporter.validate_stage1_report(report)


def test_safe_output_is_utf8_newline_read_back_and_collision_refused(tmp_path: Path) -> None:
    report: dict[str, object] = {"fictional": "évidence"}

    output, readback = reporter.write_report(
        report, label="stage1-fictional", now=_NOW, directory=tmp_path
    )

    assert output.parent == tmp_path.resolve()
    assert output.name == "20260920T123000Z-stage1-fictional.json"
    assert output.read_bytes().endswith(b"\n")
    assert readback == report
    with pytest.raises(FileExistsError):
        reporter.write_report(report, label="stage1-fictional", now=_NOW, directory=tmp_path)


# --- Live reader builders (pure) ------------------------------------------------------------------

_RUN_FLOW = "77777777-7777-4777-8777-777777777777"
_RUN_EXECUTION = "88888888-8888-4888-8888-888888888888"


def _contribution(flow_id: str, sequence: int, **overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "flow_id": flow_id,
        "execution_id": _RUN_EXECUTION,
        "predecessor_execution_id": None,
        "parent_flow_id": None,
        "surface": "semantic_inline_table_run",
        "state": "running" if sequence == 1 else "completed",
        "outcome": None if sequence == 1 else "succeeded",
        "transition_sequence": sequence,
        "occurred_at": f"2026-09-21T13:12:{10 + sequence:02d}+00:00",
    }
    document.update(overrides)
    return document


def test_status_observations_fold_one_flow_from_its_highest_sequence_event() -> None:
    observations = reporter.status_observations(
        [
            _contribution(_RUN_FLOW, 2),
            _contribution(_RUN_FLOW, 1),
            _contribution(
                _POINTER_FLOW,
                1,
                surface="semantic_servable_pointer",
                state="completed",
                outcome="succeeded",
                parent_flow_id=_RUN_FLOW,
                execution_id=_POINTER_EXECUTION,
            ),
        ]
    )
    by_flow = {item.identity.flow_id: item for item in observations}
    run = by_flow[_RUN_FLOW]
    # Grain: one observation per public flow; event_count counts applied events in the window.
    assert (run.surface, run.state, run.outcome, run.event_count) == (
        "semantic_inline_table_run",
        "completed",
        "succeeded",
        2,
    )
    assert by_flow[_POINTER_FLOW].parent_flow_id == _RUN_FLOW
    assert reporter._status_cohort(_RUN_FLOW, observations) == observations


def test_run_authority_facts_count_statement_kinds_and_sum_engine_milliseconds() -> None:
    run = {
        "status_flow_binding": _RUN_FLOW,
        "execution_binding": _RUN_EXECUTION,
        "physical_table_id": "fictional_db.fictional_table",
        "state": "terminal",
    }
    attempts = [
        {"statement_kind": "merge", "engine_ms": 1713},
        {"statement_kind": "verification", "engine_ms": 412},
    ]
    facts = reporter.run_authority_facts(run, attempts)
    # Grain: one physical table's run; engine time is the sum of its attempts in milliseconds.
    assert (facts.merge_statements, facts.verification_statements) == (1, 1)
    assert facts.athena_engine_ms == 2125
    assert facts.attempt_statement_count == 2


def test_inline_run_authority_joins_the_inline_run_surface_as_a_unit() -> None:
    """The transitional inline composition has no plan or queued unit; its runs are the units."""
    status = (
        reporter.StatusObservation(
            reporter.IdentityProof(_RUN_FLOW, _RUN_EXECUTION, None),
            "semantic_inline_table_run",
            "completed",
            "succeeded",
            2,
        ),
    )
    authority = reporter.AuthorityObservation(
        reporter.IdentityProof(_RUN_FLOW, _RUN_EXECUTION, None),
        "unit",
        "terminal",
        "complete",
        owner_ordinal=1,
        merge_statements=1,
        verification_statements=1,
        athena_engine_ms=2125,
        attempt_statement_count=2,
    )
    assert reporter._joined_authority(status, authority) == "complete"
    evidence = reporter.Stage1Evidence(status, (authority,), (), "complete", "complete", "missing")
    readback, projection, _ = reporter._authority_documents(_RUN_FLOW, evidence)
    assert readback["plan"] == {"state": None, "evidence": "unmatched"}
    assert readback["units"] == [{"unit_ordinal": 1, "state": "terminal", "evidence": "complete"}]
    assert projection["merge_statements"] == 1
    assert projection["athena_engine_ms"] == 2125
    # Without a plan there is no plan-side attempt total to agree with: the total stays null.
    assert projection["total_statements"] is None


def test_delivery_terminal_joins_only_the_exact_public_identity_proof() -> None:
    """A log terminal cannot join by flow/execution while dropping predecessor lineage."""
    identity = reporter.IdentityProof(_RUN_FLOW, _RUN_EXECUTION, _WRONG_PREDECESSOR)
    record = {
        "operation": "ingest_delivery",
        "phase": "terminal",
        "elapsed_ms": 1234.5,
        "run_flows": [
            {
                "flow_id": _RUN_FLOW,
                "execution_id": _RUN_EXECUTION,
                "predecessor_execution_id": _WRONG_PREDECESSOR,
            }
        ],
    }

    matched = reporter._delivery_terminal(
        json.dumps(record),
        {(_RUN_FLOW, _RUN_EXECUTION, _WRONG_PREDECESSOR): identity},
    )
    wrong_predecessor = reporter._delivery_terminal(
        json.dumps(
            {
                **record,
                "run_flows": [
                    {
                        "flow_id": _RUN_FLOW,
                        "execution_id": _RUN_EXECUTION,
                        "predecessor_execution_id": None,
                    }
                ],
            }
        ),
        {(_RUN_FLOW, _RUN_EXECUTION, _WRONG_PREDECESSOR): identity},
    )

    assert matched == reporter.DeliveryTerminal(identity, elapsed_ms=1234.5)
    assert wrong_predecessor is None


def test_legacy_delivery_terminal_without_public_identity_stays_unmatched() -> None:
    """Pre-identity logs remain unavailable rather than joining by their time window."""
    identity = reporter.IdentityProof(_RUN_FLOW, _RUN_EXECUTION, None)
    message = json.dumps(
        {
            "operation": "ingest_delivery",
            "phase": "terminal",
            "elapsed_ms": 1234.5,
        }
    )

    assert (
        reporter._delivery_terminal(message, {(_RUN_FLOW, _RUN_EXECUTION, None): identity}) is None
    )


class _FakeLogs:
    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        self.requests: list[dict[str, object]] = []

    def filter_log_events(self, **request: object) -> dict[str, object]:
        self.requests.append(dict(request))
        return {
            "events": [
                {"message": message, "timestamp": int(_NOW.timestamp() * 1000)}
                for message in self.messages
            ]
        }


def test_cloudwatch_reader_returns_only_exactly_joined_structured_terminals() -> None:
    identity = reporter.IdentityProof(_RUN_FLOW, _RUN_EXECUTION, None)
    status = reporter.StatusObservation(
        identity, "semantic_inline_table_run", "completed", "succeeded", 2
    )
    logs = _FakeLogs(
        [
            json.dumps(
                {
                    "operation": "ingest_delivery",
                    "phase": "terminal",
                    "elapsed_ms": 987.6,
                    "run_flows": [
                        {
                            "flow_id": _RUN_FLOW,
                            "execution_id": _RUN_EXECUTION,
                            "predecessor_execution_id": None,
                        }
                    ],
                }
            ),
            json.dumps(
                {
                    "operation": "ingest_delivery",
                    "phase": "terminal",
                    "elapsed_ms": 111.0,
                    "run_flows": [
                        {
                            "flow_id": _UNRELATED_FLOW,
                            "execution_id": _UNRELATED_EXECUTION,
                            "predecessor_execution_id": None,
                        }
                    ],
                }
            ),
        ]
    )
    reader = reporter.CloudWatchDeliveryTerminalReader(logs, "/aws/lambda/fictional")

    terminals = reader.read((status,), (), _NOW, _NOW)

    assert terminals == (reporter.DeliveryTerminal(identity, elapsed_ms=987.6),)
    assert logs.requests == [
        {
            "logGroupName": "/aws/lambda/fictional",
            "startTime": int(_NOW.timestamp() * 1000),
            "endTime": int(_NOW.timestamp() * 1000),
            "filterPattern": ('{ $.operation = "ingest_delivery" && $.phase = "terminal" }'),
        }
    ]


def test_inline_terminal_makes_invocation_and_elapsed_metrics_available() -> None:
    identity = reporter.IdentityProof(_RUN_FLOW, _RUN_EXECUTION, None)
    evidence = reporter.Stage1Evidence(
        status=(
            reporter.StatusObservation(
                identity, "semantic_inline_table_run", "completed", "succeeded", 2
            ),
        ),
        authority=(
            reporter.AuthorityObservation(
                identity,
                kind="unit",
                state="terminal",
                evidence="complete",
                owner_ordinal=1,
                merge_statements=1,
                verification_statements=1,
                athena_engine_ms=700,
                attempt_statement_count=2,
            ),
        ),
        terminals=(reporter.DeliveryTerminal(identity, elapsed_ms=10_750),),
        status_evidence="complete",
        authority_evidence="complete",
        terminal_evidence="complete",
    )
    _, projection, joined = reporter._authority_documents(_RUN_FLOW, evidence)

    delivery = reporter._delivery_document(evidence, projection, joined)

    assert delivery["invocation_count"] == 1
    assert delivery["elapsed_ms"] == 10_750


def test_terminal_with_multiple_run_flows_counts_once_for_either_exact_cohort() -> None:
    first = reporter.IdentityProof(_RUN_FLOW, _RUN_EXECUTION, None)
    second = reporter.IdentityProof(_UNIT_FLOW_B, _UNIT_EXECUTION_B, _WRONG_PREDECESSOR)
    record = json.dumps(
        {
            "operation": "ingest_delivery",
            "phase": "terminal",
            "elapsed_ms": 25_000,
            "run_flows": [
                {
                    "flow_id": first.flow_id,
                    "execution_id": first.execution_id,
                    "predecessor_execution_id": first.predecessor_execution_id,
                },
                {
                    "flow_id": second.flow_id,
                    "execution_id": second.execution_id,
                    "predecessor_execution_id": second.predecessor_execution_id,
                },
            ],
        }
    )

    for identity in (first, second):
        terminal = reporter._delivery_terminal(
            record,
            {
                (
                    identity.flow_id,
                    identity.execution_id,
                    identity.predecessor_execution_id,
                ): identity
            },
        )
        assert terminal == reporter.DeliveryTerminal(identity, elapsed_ms=25_000)


def test_terminal_omitting_root_predecessor_key_stays_unmatched() -> None:
    identity = reporter.IdentityProof(_RUN_FLOW, _RUN_EXECUTION, None)
    message = json.dumps(
        {
            "elapsed_ms": 100,
            "run_flows": [{"flow_id": _RUN_FLOW, "execution_id": _RUN_EXECUTION}],
        }
    )

    assert (
        reporter._delivery_terminal(message, {(_RUN_FLOW, _RUN_EXECUTION, None): identity}) is None
    )


class _PagedLogs:
    def __init__(self, message: str) -> None:
        self.message = message
        self.requests: list[dict[str, object]] = []

    def filter_log_events(self, **request: object) -> dict[str, object]:
        self.requests.append(dict(request))
        # The non-advancing second response deliberately repeats the prior terminal;
        # the reader must detect the stable token before admitting this duplicate page.
        return {"events": [{"message": self.message}], "nextToken": "stable"}


def test_cloudwatch_reader_stops_when_the_pagination_token_repeats() -> None:
    identity = reporter.IdentityProof(_RUN_FLOW, _RUN_EXECUTION, None)
    message = json.dumps(
        {
            "elapsed_ms": 100,
            "run_flows": [
                {
                    "flow_id": _RUN_FLOW,
                    "execution_id": _RUN_EXECUTION,
                    "predecessor_execution_id": None,
                }
            ],
        }
    )
    logs = _PagedLogs(message)
    reader = reporter.CloudWatchDeliveryTerminalReader(logs, "/aws/lambda/fictional")
    status = reporter.StatusObservation(
        identity, "semantic_inline_table_run", "completed", "succeeded", 2
    )

    assert reader.read((status,), (), _NOW, _NOW) == (
        reporter.DeliveryTerminal(identity, elapsed_ms=100),
    )
    assert len(logs.requests) == 2
    assert logs.requests[1]["nextToken"] == "stable"
