# SPDX-License-Identifier: MIT-0
"""The SEMANTIC-M1 workgroup-migration readback probe (plan 2.5), over fakes only.

Every passing check runs beside a paired control that must fail on the defect it guards
(steering `validation-probes.md`). The census fixtures carry the attempt shapes the
reconciler's eventually consistent GSI would miss; each must still count.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from validation import probe_athena_workgroup_migration as probe  # noqa: E402

pytestmark = [pytest.mark.unit]

ENV = "dev"
BATCH = "adlytics-dev-v2-batch"
BAKE = "adlytics-dev-v2-bake"
RESULTS = "adlytics-dev-v2-results-111122223333"


def _envelope(workgroup: str) -> str:
    return json.dumps(
        {"Database": "fictional_db", "QueryString": "SELECT 1", "WorkGroup": workgroup},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _attempt(
    state: str, workgroup: str | None = BATCH, **extra: Mapping[str, str]
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "partition_key": {"S": "TABLE_ATTEMPT#fictional-table"},
        "record_key": {"S": "ATTEMPT#fictional-attempt"},
        "state": {"S": state},
        **extra,
    }
    if workgroup is not None:
        item["start_envelope"] = {"S": _envelope(workgroup)}
    return item


def _pages(*pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"Items": items} for items in pages]


# --- batch_envelope_census -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("shape", "item"),
    [
        (
            "not-yet-due pending attempt",
            _attempt(
                "submitted",
                reconcile_status={"S": "pending"},
                reconcile_after_or_block={"S": "99999999999999999999#digest"},
            ),
        ),
        (
            "canonical blocked fence's attempt",
            _attempt("cancel_requested", reconcile_status={"S": "blocked"}),
        ),
        ("attempt with no reconcile attributes (index-invisible)", _attempt("intent")),
        ("parked attempt with no query_execution_id", _attempt("parked")),
    ],
)
def test_each_nonterminal_batch_envelope_the_index_would_miss_counts_one(
    shape: str, item: dict[str, Any]
) -> None:
    result = probe.batch_envelope_census(_pages([item]), BATCH)
    assert result == probe.CensusCount(nonterminal_batch=1, pages=1, items_read=1), shape


def test_an_attempt_on_the_second_page_counts_and_both_pages_are_read() -> None:
    result = probe.batch_envelope_census(_pages([], [_attempt("submitted")]), BATCH)
    assert result == probe.CensusCount(nonterminal_batch=1, pages=2, items_read=1)


@pytest.mark.parametrize("terminal", ["SUCCEEDED", "FAILED", "CANCELLED"])
def test_control_a_terminal_batch_attempt_and_a_nonterminal_bake_attempt_count_zero(
    terminal: str,
) -> None:
    # A terminal attempt stores its provider terminal state, as `parse_attempt` reads it.
    result = probe.batch_envelope_census(
        _pages([_attempt(terminal), _attempt("submitted", BAKE)]), BATCH
    )
    assert result == probe.CensusCount(nonterminal_batch=0, pages=1, items_read=2)


@pytest.mark.parametrize(
    ("reason", "item"),
    [
        ("unparseable_envelope", {**_attempt("submitted"), "start_envelope": {"S": "{not json"}}),
        ("missing_envelope", _attempt("parked", workgroup=None)),
        ("unknown_state", _attempt("queued")),
        # No writer stores the literal "terminal"; only the provider terminal states.
        ("unknown_state", _attempt("terminal")),
        ("malformed_item", {**_attempt("submitted"), "record_key": {"S": "FENCE"}}),
    ],
)
def test_an_unreadable_attempt_refuses_the_census_never_a_zero(
    reason: str, item: dict[str, Any]
) -> None:
    # The refusal is the census decision itself, before any count is reported.
    assert probe.batch_envelope_census(_pages([item]), BATCH) == probe.CensusRefused(reason)


def test_a_paginator_error_mid_scan_refuses_the_census() -> None:
    def failing() -> Iterator[dict[str, Any]]:
        yield {"Items": []}
        raise ClientError({"Error": {"Code": "InternalServerError"}}, "Scan")

    assert probe.batch_envelope_census(failing(), BATCH) == probe.CensusRefused("paginator_error")
    # Control: the same first page with no error is a complete zero.
    assert probe.batch_envelope_census(_pages([]), BATCH) == probe.CensusCount(0, 1, 0)


def test_a_paginator_that_yields_no_page_is_refused_not_zero() -> None:
    assert probe.batch_envelope_census([], BATCH) == probe.CensusRefused("no_page_read")


class _RecordingPaginator:
    def __init__(self) -> None:
        self.operation: str | None = None
        self.arguments: dict[str, Any] = {}

    def paginate(self, **arguments: Any) -> list[dict[str, Any]]:
        self.arguments = arguments
        return _pages([_attempt("submitted")])


class _RecordingDynamo:
    def __init__(self) -> None:
        self.paginator = _RecordingPaginator()

    def get_paginator(self, operation: str) -> _RecordingPaginator:
        self.paginator.operation = operation
        return self.paginator


def test_the_census_reads_the_base_table_strongly_through_the_scan_paginator() -> None:
    dynamo = _RecordingDynamo()
    result = probe.batch_envelope_census(probe.census_pages(dynamo, "publications"), BATCH)
    arguments = dynamo.paginator.arguments
    assert dynamo.paginator.operation == "scan"
    assert arguments["TableName"] == "publications"
    assert arguments["ConsistentRead"] is True
    assert "IndexName" not in arguments  # never the reconciler's eventually consistent GSI
    assert arguments["ExpressionAttributeValues"] == {
        ":tap": {"S": "TABLE_ATTEMPT#"},
        ":attempt": {"S": "ATTEMPT#"},
    }
    assert arguments["ProjectionExpression"] == (
        "partition_key, record_key, #state, start_envelope"
    )
    assert result == probe.CensusCount(1, 1, 1)


# --- workgroups and lambda_workgroup_env -----------------------------------------------------


def _workgroup(kind: str) -> dict[str, Any]:
    return {
        "State": "ENABLED",
        "Configuration": {
            "EnforceWorkGroupConfiguration": True,
            "ResultConfiguration": {"OutputLocation": f"s3://{RESULTS}/{kind}/"},
            "EngineVersion": {"SelectedEngineVersion": "Athena engine version 3"},
        },
    }


def _all_workgroups() -> dict[str, Any]:
    return {name: _workgroup(kind) for kind, name in probe.workgroup_names(ENV).items()}


def test_the_three_workgroups_pass_exactly_and_a_bake_on_the_batch_prefix_fails() -> None:
    assert probe.workgroup_findings(_all_workgroups(), ENV, RESULTS) == ()
    control = _all_workgroups()
    control[BAKE]["Configuration"]["ResultConfiguration"]["OutputLocation"] = (
        f"s3://{RESULTS}/batch/"
    )
    assert probe.workgroup_findings(control, ENV, RESULTS) == ("bake: output",)
    missing = _all_workgroups()
    missing["adlytics-dev-v2-insight-precompute"] = None
    assert probe.workgroup_findings(missing, ENV, RESULTS) == ("insight-precompute: missing",)


def _env() -> dict[str, dict[str, str]]:
    return {
        function: {**expected, "ADL_OTHER": "unrelated"}
        for function, expected in probe.expected_workgroup_env(ENV).items()
    }


def test_every_budgeted_lambda_names_its_own_workgroup_and_a_batch_env_fails() -> None:
    assert probe.lambda_env_findings(_env(), ENV) == ()
    control = _env()
    control["adlytics-dev-TableWriter"]["ADL_SEMANTIC_V2_ATHENA_BAKE_WORKGROUP"] = BATCH
    assert probe.lambda_env_findings(control, ENV) == (
        "adlytics-dev-TableWriter: names batch",
        "adlytics-dev-TableWriter: workgroup env differs",
    )
    stale = _env()
    stale["adlytics-dev-PresetArtifactBaker"]["ADL_SEMANTIC_V2_ATHENA_BATCH_WORKGROUP"] = BATCH
    assert "adlytics-dev-PresetArtifactBaker: names batch" in probe.lambda_env_findings(stale, ENV)


_PASSING_BUDGET = probe.BudgetReadback(probe.BudgetAccepted(41), 2)


def test_the_removal_trigger_needs_a_clean_env_and_a_complete_zero_in_one_run() -> None:
    zero = probe.CensusCount(0, 1, 0)
    assert probe.MigrationReport((), (), zero, _PASSING_BUDGET).removal_trigger_met is True
    # Controls: each missing half keeps the trigger false.
    env_failed = probe.MigrationReport((), ("f: names batch",), zero, _PASSING_BUDGET)
    assert env_failed.removal_trigger_met is False
    one = probe.MigrationReport((), (), probe.CensusCount(1, 1, 1), _PASSING_BUDGET)
    assert one.removal_trigger_met is False
    refused = probe.MigrationReport((), (), probe.CensusRefused("paginator_error"), _PASSING_BUDGET)
    assert refused.removal_trigger_met is False
    assert refused.failed is True


# --- explicit target -------------------------------------------------------------------------

_LIMITS = ["--batch-athena-budget", "41", "--chat-reserve-active-dml", "20"]
_FULL = ["--profile", "fictional", "--env", ENV, "--region", "us-east-1", "--account", "1"]


def test_an_omitted_or_default_target_is_refused_before_any_aws_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions: list[object] = []

    def record(**kwargs: object) -> None:
        sessions.append(kwargs)

    monkeypatch.setattr(probe.boto3, "Session", record)
    with pytest.raises(SystemExit):
        probe.request_from([*_FULL[:-2], *_LIMITS])  # --account omitted: argparse refuses
    with pytest.raises(SystemExit):
        probe.request_from(_FULL)  # both limits omitted: argparse refuses, never defaults
    assert probe.main([*_FULL[:1], "default", *_FULL[2:], *_LIMITS]) == 1  # default profile
    assert probe.main([*_FULL[:3], " ", *_FULL[4:], *_LIMITS]) == 1  # blank env refused
    assert sessions == []
    # Control: the full explicit target reaches the session with its profile and region.
    with pytest.raises(AttributeError):
        probe.main([*_FULL, *_LIMITS])
    assert sessions == [{"profile_name": "fictional", "region_name": "us-east-1"}]


# --- budget (plan 3.5, S1, F4, F5): the observed deployed capacity ----------------------------


class _RecordingLambda:
    """`GetFunctionConcurrency` over fixed reservations; `None` omits the member (unreserved)."""

    def __init__(self, reserved: Mapping[str, int | None]) -> None:
        self._reserved = reserved
        self.calls: list[str] = []

    def get_function_concurrency(self, *, FunctionName: str) -> dict[str, int]:  # noqa: N803
        self.calls.append(FunctionName)
        value = self._reserved[FunctionName.removeprefix(f"adlytics-{ENV}-")]
        return {} if value is None else {"ReservedConcurrentExecutions": value}


_ACTIVE = {
    "PresetArtifactBaker": 2,
    "TableWriter": 8,
    "IngestionFunction": 2,
    "ProviderAttemptReconciler": 1,
    "InsightPrecompute": 2,
}
_CAPS: dict[str, Mapping[str, str] | None] = {
    f"adlytics-{ENV}-PresetArtifactBaker": {"ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY": "10"},
    f"adlytics-{ENV}-InsightPrecompute": {"ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY": "4"},
}


def _budget_request(budget: int = 41, reserve: int = 20) -> probe.ProbeRequest:
    return probe.ProbeRequest("fictional", ENV, "us-east-1", "1", budget, reserve)


def _readback(
    reserved: Mapping[str, int | None], request: probe.ProbeRequest | None = None
) -> tuple[probe.BudgetReadback, _RecordingLambda]:
    functions = _RecordingLambda(reserved)
    return probe.read_budget(functions, request or _budget_request(), _CAPS), functions


def test_active_defaults_are_accepted_at_41_with_the_operator_rows_asserted_by_runbook() -> None:
    readback, functions = _readback(_ACTIVE)
    assert readback.verdict == probe.BudgetAccepted(41)
    assert readback.load_reserved_concurrency == 2
    assert readback.evidence()["asserted"] == {
        "ingest_operator": "runbook",
        "reprocess_operator": "runbook",
    }
    assert len(functions.calls) == 5
    # Paired control: the same deployment with the Baker drifted to 3 is over budget.
    drifted, _ = _readback({**_ACTIVE, "PresetArtifactBaker": 3})
    assert isinstance(drifted.verdict, probe.BudgetRefused)
    assert (drifted.verdict.reason.value, drifted.verdict.observed) == ("SUM_OVER_BUDGET", 51)
    assert drifted.passed is False


def test_a_quiesced_load_reading_0_is_accepted_at_39_and_an_unreserved_one_is_refused() -> None:
    quiesced, _ = _readback({**_ACTIVE, "IngestionFunction": 0})
    assert quiesced.verdict == probe.BudgetAccepted(39)
    assert quiesced.load_reserved_concurrency == 0
    assert quiesced.evidence()["asserted"] == {
        "ingest_operator": "runbook",
        "reprocess_operator": "runbook",
    }
    # Paired control: a response with no ReservedConcurrentExecutions member is unreserved.
    unreserved, _ = _readback({**_ACTIVE, "IngestionFunction": None})
    assert isinstance(unreserved.verdict, probe.BudgetRefused)
    assert unreserved.verdict.reason.value == "UNRESERVED"
    assert unreserved.load_reserved_concurrency is None


@pytest.mark.parametrize(
    ("budget", "reserve", "field", "supplied", "contract"),
    [
        (42, 20, "batch_athena_budget", 42, 41),
        (41, 19, "chat_reserve_active_dml", 19, 20),
    ],
)
def test_a_supplied_limit_off_the_contract_is_refused_before_any_concurrency_read(  # noqa: PLR0913, PLR0917 -- parametrized
    budget: int, reserve: int, field: str, supplied: int, contract: int
) -> None:
    refused, functions = _readback(_ACTIVE, _budget_request(budget, reserve))
    assert refused.verdict == probe.BudgetInputRefused(
        "supplied_limit_mismatch", field, supplied, contract
    )
    assert refused.passed is False
    assert functions.calls == []  # the refusal precedes capacity evaluation
    # Paired control: the contract values over the same deployment are read and accepted.
    accepted, control = _readback(_ACTIVE)
    assert accepted.passed is True
    assert len(control.calls) == 5


def test_a_refused_budget_fails_the_report_and_is_printed_as_evidence() -> None:
    zero = probe.CensusCount(0, 1, 0)
    refused = probe.BudgetReadback(
        probe.BudgetInputRefused("supplied_limit_mismatch", "batch_athena_budget", 42, 41), None
    )
    report = probe.MigrationReport((), (), zero, refused)
    assert report.failed is True
    budget = report.evidence()["budget"]
    assert isinstance(budget, dict)
    assert (budget["reason"], budget["field"], budget["passed"]) == (
        "supplied_limit_mismatch",
        "batch_athena_budget",
        False,
    )
    # Control: the same report with an accepted budget passes.
    assert probe.MigrationReport((), (), zero, _PASSING_BUDGET).failed is False
