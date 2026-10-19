# SPDX-License-Identifier: MIT-0
"""Recovery-first execution shared by the business-outcome AMC probes (fictional values)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "validation"))
sys.path.insert(0, str(ROOT))

from validation import amc_recovery  # noqa: E402

pytestmark = pytest.mark.unit

START = "2026-01-05T00:00:00"
END = "2026-01-12T00:00:00"
INSTANCE = "instance-fixture"


class FakeApi:
    """Records calls; never touches a network."""

    def __init__(self) -> None:
        self.history: list[dict[str, object]] = []
        self.submissions: list[dict[str, object]] = []
        self.terminal: dict[str, object] = {"status": "SUCCEEDED"}
        self.calls = 0

    def executions_since(
        self, *, instance_id: str, min_creation_time: str
    ) -> list[dict[str, object]]:
        self.calls += 1
        del instance_id, min_creation_time
        return self.history

    def execute_adhoc(  # noqa: PLR0913 -- mirrors the external AMC client
        self,
        *,
        instance_id: str,
        workflow_id: str,
        sql: str,
        window_start: str,
        window_end: str,
        require_synthetic_data: bool | None,
        disable_aggregation_controls: bool | None,
        suppression_columns: tuple[str, str, str],
        parameter_values: dict[str, str] | None = None,
    ) -> str:
        self.calls += 1
        self.submissions.append(
            {
                "instance_id": instance_id,
                "workflow_id": workflow_id,
                "sql": sql,
                "window": (window_start, window_end),
                "synthetic": require_synthetic_data,
                "disable": disable_aggregation_controls,
                "suppression": suppression_columns,
                "parameters": parameter_values,
            }
        )
        return f"exec-fixture-{len(self.submissions)}"

    def wait_for_execution(
        self, *, instance_id: str, execution_id: str, timeout_seconds: int
    ) -> dict[str, object]:
        del instance_id, execution_id, timeout_seconds
        return self.terminal

    def download_urls(self, *, instance_id: str, execution_id: str) -> list[str]:
        del instance_id, execution_id
        return ["https://results.example.test/result.csv"]


def _spec(
    *, controls_off: bool = False, sql: str = "SELECT COUNT(1) AS n FROM t"
) -> amc_recovery.ExecutionSpec:
    return amc_recovery.ExecutionSpec(
        name="Q1", sql=sql, controls_off=controls_off, workflow_prefix="probe-x", stem_prefix="x"
    )


def _request(  # noqa: PLR0913 -- fixture builder
    api: FakeApi,
    tmp_path: Path,
    *,
    spec: amc_recovery.ExecutionSpec,
    instance_type: str = "SANDBOX",
) -> amc_recovery.ExecutionRequest:
    return amc_recovery.ExecutionRequest(
        api=api,
        instance_id=INSTANCE,
        instance_type=instance_type,
        spec=spec,
        window_start=START,
        window_end=END,
        recover_since="2026-01-01T00:00:00",
        findings=tmp_path,
    )


def test_submission_is_journaled_and_a_rerun_adopts_the_journal(tmp_path: Path) -> None:
    api = FakeApi()
    first = amc_recovery.resolve_execution(_request(api, tmp_path, spec=_spec()))
    journal = json.loads((tmp_path / f"{first.stem}.submission.json").read_text())
    assert journal["execution_id"] == first.execution_id
    assert api.submissions[0]["synthetic"] is True
    assert api.submissions[0]["disable"] is False
    again = amc_recovery.resolve_execution(_request(api, tmp_path, spec=_spec()))
    assert again.source == "journal"
    assert len(api.submissions) == 1


def test_history_match_is_adopted_and_journaled_without_submitting(tmp_path: Path) -> None:
    api = FakeApi()
    workflow_id, _ = amc_recovery.names(_spec(), "SANDBOX", window_start=START, window_end=END)
    api.history = [{"workflowId": workflow_id, "workflowExecutionId": "exec-history"}]
    plan = amc_recovery.resolve_execution(_request(api, tmp_path, spec=_spec()))
    assert plan.source == "history"
    assert plan.execution_id == "exec-history"
    assert not api.submissions
    assert (tmp_path / f"{plan.stem}.submission.json").exists()


def test_more_than_one_history_match_exits(tmp_path: Path) -> None:
    api = FakeApi()
    workflow_id, _ = amc_recovery.names(_spec(), "SANDBOX", window_start=START, window_end=END)
    api.history = [
        {"workflowId": workflow_id, "workflowExecutionId": "exec-a"},
        {"workflow": {"workflowId": workflow_id}, "workflowExecutionId": "exec-b"},
    ]
    with pytest.raises(SystemExit, match="more than one"):
        amc_recovery.resolve_execution(_request(api, tmp_path, spec=_spec()))


def test_controls_flag_changes_the_identity() -> None:
    on, off = _spec(), _spec(controls_off=True)
    assert amc_recovery.identity_digest(on) != amc_recovery.identity_digest(off)
    assert amc_recovery.sql_digest(on.sql) == amc_recovery.sql_digest(off.sql)
    assert (
        amc_recovery.names(on, "SANDBOX", window_start=START, window_end=END)[0]
        != (amc_recovery.names(off, "SANDBOX", window_start=START, window_end=END)[0])
    )


def test_controls_off_on_standard_refuses_before_any_call(tmp_path: Path) -> None:
    api = FakeApi()
    with pytest.raises(SystemExit, match="SANDBOX"):
        amc_recovery.resolve_execution(
            _request(api, tmp_path, spec=_spec(controls_off=True), instance_type="STANDARD")
        )
    assert api.calls == 0


def test_standard_sends_neither_sandbox_option(tmp_path: Path) -> None:
    api = FakeApi()
    amc_recovery.resolve_execution(_request(api, tmp_path, spec=_spec(), instance_type="STANDARD"))
    assert api.submissions[0]["synthetic"] is None
    assert api.submissions[0]["disable"] is None


def test_journal_for_another_instance_refuses(tmp_path: Path) -> None:
    api = FakeApi()
    plan = amc_recovery.resolve_execution(_request(api, tmp_path, spec=_spec()))
    path = tmp_path / f"{plan.stem}.submission.json"
    record = json.loads(path.read_text())
    record["instance_id"] = "instance-other"
    path.write_text(json.dumps(record))
    with pytest.raises(SystemExit, match="another instance"):
        amc_recovery.resolve_execution(_request(api, tmp_path, spec=_spec()))


def test_parameterised_spec_is_refused_until_the_shape_is_confirmed(tmp_path: Path) -> None:
    api = FakeApi()
    spec = amc_recovery.ExecutionSpec(
        name="Q3",
        sql="SELECT 1",
        controls_off=False,
        workflow_prefix="probe-x",
        stem_prefix="x",
        parameter_values=(("sponsored_marketplace_name", "FIXTURE"),),
    )
    with pytest.raises(SystemExit, match="parameter"):
        amc_recovery.resolve_execution(_request(api, tmp_path, spec=spec))
    assert api.calls == 0


def test_non_succeeded_terminal_writes_no_csv(tmp_path: Path) -> None:
    api = FakeApi()
    api.terminal = {"status": "REJECTED", "statusReason": "fixture compile failure"}
    plan = amc_recovery.resolve_execution(_request(api, tmp_path, spec=_spec()))
    result = amc_recovery.collect_terminal(api, plan, byte_limit=1000)
    assert result.status == "REJECTED"
    assert result.csv_text is None
    assert (tmp_path / f"{plan.stem}.terminal.json").exists()
    assert not (tmp_path / f"{plan.stem}.csv").exists()


def test_parameterised_spec_is_sent_when_explicitly_allowed(tmp_path: Path) -> None:
    api = FakeApi()
    spec = amc_recovery.ExecutionSpec(
        name="shape",
        sql="SELECT 1",
        controls_off=False,
        workflow_prefix="probe-x",
        stem_prefix="x",
        parameter_values=(("probe_marker", "fixture-value"),),
    )
    plan = amc_recovery.resolve_execution(
        amc_recovery.ExecutionRequest(
            api=api, instance_id=INSTANCE, instance_type="SANDBOX", spec=spec,
            window_start=START, window_end=END, recover_since="2026-01-01T00:00:00",
            findings=tmp_path, allow_parameters=True,
        )
    )  # fmt: skip
    assert api.submissions[0]["parameters"] == {"probe_marker": "fixture-value"}
    journal = json.loads((tmp_path / f"{plan.stem}.submission.json").read_text())
    assert journal["parameter_values"] == {"probe_marker": "fixture-value"}
