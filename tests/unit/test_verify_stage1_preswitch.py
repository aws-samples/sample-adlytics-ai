# SPDX-License-Identifier: MIT-0
"""Read-only Stage-1 pre-switch verification: GSI proofs, page checks, edge evidence, drain timer.

The reserved-namespace rows come from the real coordinator over a fake DynamoDB, so "canonical"
means "what the amended builders actually wrote"; every negative case tampers with one attribute
and expects exactly that key to be reported.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import argparse
from typing import Any, cast

import pytest
from stage1_world_support import PUBLICATIONS, World

from adapters.config import DRAIN_OBSERVATION_INTERVAL_ENV
from adapters.jobs.stage1_operator_bootstrap import BootstrapResult
from adapters.jobs.verify_stage1_preswitch import __main__ as cli
from adapters.jobs.verify_stage1_preswitch.checks import (
    STAGE1_INDEXES,
    EdgeObservation,
    canonical_namespace_checks,
    edge_checks,
    execution_evidence,
    gsi_schema_check,
    ready_field_check,
    stray_reconciliation_check,
)
from adapters.jobs.verify_stage1_preswitch.drain import DrainObservation, DrainTimer
from domain.shared_kernel.errors import DependencyError, InvariantError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

TABLE = "fictional_db.fictional_table"


def _items(world: World) -> list[dict[str, Any]]:
    return [
        dict(item) for (table, _, _), item in world.dynamo.items.items() if table == PUBLICATIONS
    ]


def _world_with_inline_run() -> World:
    world = World()
    run_id, execution = world.open_inline_run(TABLE, "one")
    begun = world.begin_inline(TABLE, run_id, execution)
    world.record(TABLE, begun, "fictional-query-1")
    return world


def _world_with_pending_intent() -> World:
    world = World()
    world.submit_intent("pending", (TABLE,))
    return world


def _describe_table(*, status: str = "ACTIVE", projection: str = "KEYS_ONLY") -> dict[str, Any]:
    return {
        "GlobalSecondaryIndexes": [
            {
                "IndexName": expectation.name,
                "IndexStatus": status,
                "KeySchema": [
                    {"AttributeName": expectation.partition_key, "KeyType": "HASH"},
                    {"AttributeName": expectation.sort_key, "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": projection},
            }
            for expectation in STAGE1_INDEXES
        ]
    }


# --- GSI proofs -----------------------------------------------------------------------------------


def test_three_stage1_indexes_active_keys_only_with_pinned_schema_pass() -> None:
    check = gsi_schema_check(_describe_table())
    assert check.passed and check.violating_keys == ()
    assert check.detail == {"indexes_checked": 3}


@pytest.mark.parametrize(
    "table",
    [
        _describe_table(status="CREATING"),
        _describe_table(projection="ALL"),
        {"GlobalSecondaryIndexes": _describe_table()["GlobalSecondaryIndexes"][:2]},
    ],
    ids=["backfilling", "projection", "missing"],
)
def test_backfilling_mismatched_or_missing_index_blocks(table: dict[str, Any]) -> None:
    check = gsi_schema_check(table)
    assert not check.passed
    assert check.violating_keys


# --- Ready fields ---------------------------------------------------------------------------------


def test_pending_intent_meta_carries_both_ready_fields_and_nothing_else_does() -> None:
    world = _world_with_pending_intent()
    check = ready_field_check(_items(world))
    assert check.passed, check.violating_keys


def test_ready_field_on_a_non_intent_row_or_wrong_order_form_is_a_violation() -> None:
    world = _world_with_pending_intent()
    items = _items(world)
    meta = next(item for item in items if item.get("ready_status"))
    stray = dict(items[0] if items[0] is not meta else items[1])
    stray["ready_status"] = {"S": "ready"}
    tampered = dict(meta)
    tampered["ready_order"] = {"S": "not-a-canonical-order"}
    check = ready_field_check([*items, stray])
    assert not check.passed
    check = ready_field_check([tampered if item is meta else item for item in items])
    assert not check.passed
    assert check.violating_keys == (f"{meta['partition_key']['S']}/META",)


# --- Reserved namespaces --------------------------------------------------------------------------


def test_builder_written_inline_namespace_recomputes_canonically() -> None:
    world = _world_with_inline_run()
    checks = canonical_namespace_checks(_items(world))
    failed = [check.name for check in checks if not check.passed]
    assert failed == [], [(check.name, check.violating_keys) for check in checks]
    assert stray_reconciliation_check(_items(world)).passed


@pytest.mark.parametrize(
    ("record_prefix", "attribute", "check_name"),
    [
        ("RUN#", "operation_id", "run_canonical"),
        ("ATTEMPT#", "start_envelope", "attempt_canonical"),
        ("FENCE", "lease_holder", "fence_canonical"),
    ],
)
def test_one_tampered_immutable_attribute_names_exactly_that_key(
    record_prefix: str, attribute: str, check_name: str
) -> None:
    """Recomputation is provenance: a row an amended builder did not write cannot recompute."""
    world = _world_with_inline_run()
    items = _items(world)
    victim = next(
        item
        for item in items
        if item["partition_key"]["S"].startswith("TABLE_ATTEMPT#")
        and item["record_key"]["S"].startswith(record_prefix)
    )
    victim[attribute] = {"S": "tampered"}
    by_name = {check.name: check for check in canonical_namespace_checks(items)}
    assert not by_name[check_name].passed
    assert by_name[check_name].violating_keys == (
        f"{victim['partition_key']['S']}/{victim['record_key']['S']}",
    )


def test_usage_row_without_a_settled_terminal_attempt_sibling_is_a_violation() -> None:
    world = _world_with_inline_run()
    items = _items(world)
    orphan: dict[str, Any] = {
        "partition_key": {"S": f"TABLE_ATTEMPT#{TABLE}"},
        "record_key": {"S": "USAGE#attempt-that-never-settled"},
    }
    by_name = {check.name: check for check in canonical_namespace_checks([*items, orphan])}
    assert by_name["usage_beside_settled_attempt"].violating_keys == (
        f"TABLE_ATTEMPT#{TABLE}/USAGE#attempt-that-never-settled",
    )


def test_reconciliation_attribute_outside_a_fence_is_a_stray() -> None:
    world = _world_with_inline_run()
    items = _items(world)
    run = next(item for item in items if item["record_key"]["S"].startswith("RUN#"))
    run["reconcile_status"] = {"S": "pending"}
    check = stray_reconciliation_check(items)
    assert check.violating_keys == (f"{run['partition_key']['S']}/{run['record_key']['S']}",)


# --- Execution evidence and edges -----------------------------------------------------------------


def test_execution_evidence_names_open_runs_unsettled_attempts_and_active_fences() -> None:
    evidence = execution_evidence(_items(_world_with_inline_run()))
    assert not evidence.quiet
    assert len(evidence.nonterminal_runs) == 1
    assert len(evidence.nonterminal_attempts) == 1
    # A recorded submission leaves the fence pending reconciliation until a terminal clears it.
    assert evidence.nonclear_fences == evidence.pending_fences and evidence.blocked_fences == ()


def test_inline_quiesced_edge_requires_zero_concurrency_and_clear_fences() -> None:
    quiet = EdgeObservation("INLINE_QUIESCED", [], 0, {}, {}, None)
    assert all(check.passed for check in edge_checks(quiet))
    busy = EdgeObservation("INLINE_QUIESCED", _items(_world_with_inline_run()), 2, {}, {}, None)
    by_name = {check.name: check for check in edge_checks(busy)}
    assert not by_name["delivery_concurrency_zero"].passed
    assert not by_name["automatic_writer_attempts_terminal_fences_clear"].passed


def test_queued_accumulate_edge_proves_the_statement_ledger_against_the_commit_instant() -> None:
    world = _world_with_inline_run()
    items = _items(world)
    run = next(item for item in items if item["record_key"]["S"].startswith("RUN#"))
    run["writer_kind"] = {"S": "transitional_delivery"}
    attempt = next(item for item in items if item["record_key"]["S"].startswith("ATTEMPT#"))
    state = {
        "disposition": {"S": "ACTIVE"},
        "state": {"S": "QUEUED_QUIESCED"},
        "last_transition_at": {"S": "00000000000000000000"},
    }
    after_commit = EdgeObservation("QUEUED_ACCUMULATE", items, 0, {}, {}, cast("Any", state))
    by_name = {check.name: check for check in edge_checks(after_commit)}
    assert by_name["handoff_state_active"].passed
    assert not by_name["no_delivery_composition_statements_after_commit"].passed
    keys = by_name["no_delivery_composition_statements_after_commit"].violating_keys
    assert f"{run['partition_key']['S']}/{run['record_key']['S']}" in keys
    assert f"{attempt['partition_key']['S']}/{attempt['record_key']['S']}" in keys
    state["last_transition_at"] = {"S": "99999999999999999999"}
    before = EdgeObservation("QUEUED_ACTIVE", items, 0, {}, {}, cast("Any", state))
    by_name = {check.name: check for check in edge_checks(before)}
    assert by_name["no_delivery_composition_statements_after_commit"].passed


def test_queued_active_edge_admits_accumulated_units_when_the_ledger_is_clean() -> None:
    """§2.5 gives ACCUMULATE the quiet-execution demand and ACTIVE only the ledger re-proof.

    Demanding quiet at ACTIVE would make the designed ACCUMULATE state unswitchable.
    """
    accumulated: list[dict[str, Any]] = [
        {
            "partition_key": {"S": "WRITE_PLAN#" + "d" * 64 + "#" + "e" * 64},
            "record_key": {"S": "META"},
            "status": {"S": "planned"},
            "plan_key": {"S": "WRITE_PLAN#" + "d" * 64 + "#" + "e" * 64},
        },
        {
            "partition_key": {"S": "WRITE_PLAN#" + "d" * 64 + "#" + "e" * 64},
            "record_key": {"S": f"UNIT#{TABLE}"},
            "status": {"S": "queued"},
        },
    ]
    state = {
        "disposition": {"S": "ACTIVE"},
        "state": {"S": "QUEUED_ACCUMULATE"},
        "last_transition_at": {"S": "00000000000000000000"},
    }
    active = EdgeObservation(
        "QUEUED_ACTIVE", accumulated, 2, {"visible": 1}, {}, cast("Any", state)
    )
    by_active = {check.name: check for check in edge_checks(active)}
    assert "no_old_version_delivery_execution" not in by_active
    assert by_active["no_delivery_composition_statements_after_commit"].passed
    accumulate = EdgeObservation(
        "QUEUED_ACCUMULATE", accumulated, 2, {"visible": 1}, {}, cast("Any", state)
    )
    by_accumulate = {check.name: check for check in edge_checks(accumulate)}
    assert not by_accumulate["no_old_version_delivery_execution"].passed


def test_edge_without_an_active_handoff_state_blocks() -> None:
    by_name = {
        check.name: check
        for check in edge_checks(EdgeObservation("QUEUED_ACTIVE", [], 0, {}, {}, None))
    }
    assert list(by_name) == ["handoff_state_active"]
    assert not by_name["handoff_state_active"].passed


# --- Drain timer ----------------------------------------------------------------------------------


def test_drain_timer_denies_at_299_permits_at_300_and_resets_on_nonquiet() -> None:
    timer = DrainTimer(300)
    timer.observe(DrainObservation(0.0))
    timer.observe(DrainObservation(299.0))
    assert not timer.passing
    timer.observe(DrainObservation(300.0))
    assert timer.passing
    timer.observe(DrainObservation(310.0, ("queue",)))
    assert not timer.passing and timer.elapsed_quiet_seconds == 0.0
    timer.observe(DrainObservation(320.0))
    timer.observe(DrainObservation(619.0))
    assert not timer.passing
    timer.observe(DrainObservation(620.0))
    assert timer.passing
    assert timer.document()["resets"] == [{"at_seconds": 310.0, "reasons": ["queue"]}]


def test_drain_timer_requires_a_final_all_clear_read_and_closed_reasons() -> None:
    timer = DrainTimer(300)
    timer.observe(DrainObservation(0.0))
    timer.observe(DrainObservation(400.0, ("failed_read",)))
    assert not timer.passing
    with pytest.raises(ValueError, match="closed vocabulary"):
        DrainObservation(0.0, ("unknown",))
    with pytest.raises(ValueError, match="monotonic"):
        timer.observe(DrainObservation(10.0))


# --- Drain reads across sessions ------------------------------------------------------------------

_TARGETS = cli._Targets(
    table="fictional-publications",
    queue_arn="arn:aws:sqs:us-east-1:000000000000:fictional-write.fifo",
    dlq_arn="arn:aws:sqs:us-east-1:000000000000:fictional-write-dlq.fifo",
    function_arn="arn:aws:lambda:us-east-1:000000000000:function:fictional-delivery",
)
_READ_SECONDS = 482.0


class _SessionReads:
    """A read boundary whose session serves one complete drain read, then has expired."""

    def __init__(self, now: list[float]) -> None:
        self._now = now
        self.spent = False

    def rendered_environment(self) -> dict[str, object]:
        return {DRAIN_OBSERVATION_INTERVAL_ENV: "300"}

    def scan_all_pages(self) -> tuple[list[dict[str, Any]], int]:
        if self.spent:
            raise DependencyError(cli.READ_FAILED, "A publications-table page could not be read.")
        self.spent = True
        self._now[0] += _READ_SECONDS
        return [], 1

    def queue_counts(self, _queue_arn: str) -> dict[str, int]:
        return dict.fromkeys(cli._QUEUE_COUNT_NAMES, 0)

    def reserved_concurrency(self) -> int:
        return 0


def _drain_run() -> cli._Run:
    return cli._Run("verify-drain", "dev", "op", "execution", None)


def _drain(renew: Any) -> tuple[dict[str, object], list[_SessionReads]]:
    now = [0.0]
    sessions = [_SessionReads(now)]

    def fresh() -> _SessionReads:
        sessions.append(_SessionReads(now))
        return sessions[-1]

    def advance(seconds: float) -> None:
        now[0] += seconds

    verification = cli._Verification(
        cast("cli._Reads", sessions[0]),
        _drain_run(),
        _TARGETS,
        clock=lambda: now[0],
        sleep=advance,
        renew=cast("Any", fresh) if renew else None,
    )
    return verification.drain(1800), sessions


def test_drain_reads_after_the_first_run_on_a_fresh_session_and_pass() -> None:
    report, sessions = _drain(renew=True)
    assert report["passed"] is True
    assert report["pages_read"] == 2
    assert len(sessions) == 2 and all(session.spent for session in sessions)
    assert cast("dict[str, Any]", report["drain"])["resets"] == []


def test_control_without_renewal_the_second_read_fails_on_the_spent_session() -> None:
    report, sessions = _drain(renew=False)
    assert report["passed"] is False
    assert len(sessions) == 1
    assert cast("list[dict[str, Any]]", report["checks"])[0]["violating_keys"] == ["failed_read"]


def test_a_rebootstrap_that_resolves_other_targets_is_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]

    def reads(_assumed: object, _targets: object) -> _SessionReads:
        return _SessionReads(now)

    monkeypatch.setattr(cli, "_Reads", reads)
    monkeypatch.setattr(cli, "_DRAIN_POLL_SECONDS", 0.0)
    values = {
        cli.PUBLICATIONS_TABLE_KEY: _TARGETS.table,
        cli.QUEUE_KEY: _TARGETS.queue_arn,
        cli.DLQ_KEY: _TARGETS.dlq_arn,
        cli.FUNCTION_KEY: _TARGETS.function_arn,
    }
    first = BootstrapResult(values=values, assumed=cast("Any", None), role_arn="role")
    moved = BootstrapResult(
        values={**values, cli.QUEUE_KEY: f"{_TARGETS.queue_arn}-other"},
        assumed=cast("Any", None),
        role_arn="role",
    )
    args = argparse.Namespace(max_seconds=1)
    with pytest.raises(InvariantError) as raised:
        cli._verify(args, _drain_run(), cli._Sessions(first, lambda: moved))
    assert raised.value.code == cli.TARGETS_CHANGED
    # Control: a re-run resolving the same targets keeps reading, on fresh sessions, with no reset
    # until the (real-clock) one-second budget ends the run.
    report = cli._verify(args, _drain_run(), cli._Sessions(first, lambda: first))
    assert cast("dict[str, Any]", report["drain"])["resets"] == []


# --- CLI boundary ---------------------------------------------------------------------------------


def _no_aws(**_kwargs: object) -> object:
    pytest.fail("argument validation must complete before any session exists")


@pytest.mark.parametrize(
    "argv",
    [
        [
            "verify-pre-switch",
            "--profile",
            "default",
            "--region",
            "us-east-1",
            "--expected-account",
            "000000000000",
            "--env",
            "dev",
            "--operation-id",
            "op",
        ],
        [
            "verify-edge",
            "--profile",
            "p",
            "--region",
            "us-east-1",
            "--expected-account",
            "000000000000",
            "--env",
            "dev",
            "--operation-id",
            "op",
            "--target",
            "QUEUED_DRAIN",
        ],
        [
            "verify-drain",
            "--profile",
            "p",
            "--region",
            "us-east-1",
            "--expected-account",
            "12345",
            "--env",
            "dev",
            "--operation-id",
            "op",
        ],
        [
            "verify-pre-switch",
            "--profile",
            "p",
            "--region",
            "us-east-1",
            "--expected-account",
            "000000000000",
            "--env",
            "dev",
        ],
    ],
    ids=["default-profile", "target-outside-vocabulary", "short-account", "missing-operation"],
)
def test_argument_defects_are_refused_before_any_session(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    monkeypatch.setattr(cli.boto3, "Session", _no_aws)
    with pytest.raises(SystemExit) as raised:
        cli.main(argv)
    assert raised.value.code == 2
