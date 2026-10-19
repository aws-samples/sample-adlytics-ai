# SPDX-License-Identifier: MIT-0
"""Recovery, privacy, and reconciliation for the AMC deduplicated-attribution probe."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "validation"))
sys.path.insert(0, str(ROOT))
from validation import probe_deduplicated_attribution as probe  # noqa: E402

pytestmark = pytest.mark.unit
START = "2026-08-01T00:00:00"
END = "2026-08-08T00:00:00"


class _Api:
    def __init__(self) -> None:
        self.history: list[dict[str, object]] = []
        self.submissions: list[dict[str, object]] = []
        self.history_calls: list[tuple[str, str]] = []
        self.terminal: dict[str, object] = {"status": "SUCCEEDED"}
        self.urls = ["https://results.example.test/result.csv"]

    def executions_since(
        self, *, instance_id: str, min_creation_time: str
    ) -> list[dict[str, object]]:
        self.history_calls.append((instance_id, min_creation_time))
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
    ) -> str:
        self.submissions.append(
            {
                "instance_id": instance_id,
                "workflow_id": workflow_id,
                "sql": sql,
                "window_start": window_start,
                "window_end": window_end,
                "require_synthetic_data": require_synthetic_data,
                "disable_aggregation_controls": disable_aggregation_controls,
                "suppression_columns": suppression_columns,
            }
        )
        return "execution-new"

    def wait_for_execution(
        self, *, instance_id: str, execution_id: str, timeout_seconds: int
    ) -> dict[str, object]:
        del instance_id, execution_id, timeout_seconds
        return self.terminal

    def download_urls(self, *, instance_id: str, execution_id: str) -> list[str]:
        del instance_id, execution_id
        return self.urls


def _request(
    api: _Api,
    findings: Path,
    variant: tuple[str, str] = ("conversion-id-cte", "SANDBOX"),
) -> probe.ExecutionRequest:
    case, instance_type = variant
    return probe.ExecutionRequest(
        api=api,
        instance_id="instance-1",
        instance_type=instance_type,
        case=probe.CASES[case],
        window_start=START,
        window_end=END,
        recover_since="2026-07-25T00:00:00",
        findings=findings,
    )


def _workflow(case: probe.ProbeCase) -> str:
    identity = probe.query_digest(case.sql).removeprefix("sha256:")[:10]
    return f"probe-dedup-{case.name}-{identity}-20260801-20260808"


def _csv(case: probe.ProbeCase, values: tuple[str, ...], *, suppressed: str = "false") -> str:
    headers = (*case.columns, *probe.SUPPRESSION)
    row = (*values, suppressed, "", "100")
    return ",".join(headers) + "\n" + ",".join(row) + "\n"


def test_submission_is_journaled_and_rerun_adopts_it(tmp_path: Path) -> None:
    first_api = _Api()
    first = probe.resolve_execution(_request(first_api, tmp_path))
    journal = tmp_path / f"{first.stem}.submission.json"

    assert first.execution_id == "execution-new"
    assert len(first_api.submissions) == 1
    assert json.loads(journal.read_text())["execution_id"] == "execution-new"

    second_api = _Api()
    second = probe.resolve_execution(_request(second_api, tmp_path))

    assert second.execution_id == "execution-new"
    assert second_api.submissions == []
    assert second_api.history_calls == []


def test_server_history_is_recovered_before_submission(tmp_path: Path) -> None:
    api = _Api()
    workflow = _workflow(probe.CASES["conversion-id-cte"])
    api.history = [{"workflowId": workflow, "workflowExecutionId": "execution-recovered"}]

    plan = probe.resolve_execution(_request(api, tmp_path))

    assert plan.execution_id == "execution-recovered"
    assert api.submissions == []


def test_duplicate_history_refuses_to_guess(tmp_path: Path) -> None:
    api = _Api()
    workflow = _workflow(probe.CASES["conversion-id-cte"])
    api.history = [
        {"workflowId": workflow, "workflowExecutionId": "execution-a"},
        {"workflowId": workflow, "workflowExecutionId": "execution-b"},
    ]

    with pytest.raises(SystemExit, match="more than one execution"):
        probe.resolve_execution(_request(api, tmp_path))

    assert api.submissions == []


def test_sandbox_and_standard_submission_flags_are_distinct(tmp_path: Path) -> None:
    sandbox_api = _Api()
    probe.resolve_execution(_request(sandbox_api, tmp_path / "sandbox"))
    sandbox = sandbox_api.submissions[0]
    assert sandbox["require_synthetic_data"] is True
    assert sandbox["disable_aggregation_controls"] is False
    assert sandbox["suppression_columns"] == probe.SUPPRESSION

    standard_api = _Api()
    probe.resolve_execution(
        _request(standard_api, tmp_path / "standard", ("conversion-id-cte", "STANDARD"))
    )
    standard = standard_api.submissions[0]
    assert standard["require_synthetic_data"] is None
    assert standard["disable_aggregation_controls"] is None


@pytest.mark.parametrize(
    ("case_name", "values"),
    (
        ("conversion-id-cte", ("10", "17")),
        ("row-number", ("4", "1", "4")),
        ("model-reconciliation", ("10", "10", "10", "10.000000")),
        ("temporal-touch-join", ("10", "27")),
        ("temporal-model-reconciliation", ("10", "10", "10", "10.000000")),
    ),
)
def test_each_positive_case_returns_viable(case_name: str, values: tuple[str, ...]) -> None:
    case = probe.CASES[case_name]

    result = probe.validate_csv(case, _csv(case, values))

    assert result["outcome"] == "VIABLE"


def test_zero_data_is_inconclusive_and_suppression_is_blocking() -> None:
    case = probe.CASES["conversion-id-cte"]

    assert probe.validate_csv(case, _csv(case, ("0", "0")))["outcome"] == "INCONCLUSIVE"
    assert (
        probe.validate_csv(case, _csv(case, ("10", "17"), suppressed="true"))["outcome"]
        == "PRIVACY_BLOCKED"
    )


def test_reconciliation_and_sensitive_output_fail_closed() -> None:
    case = probe.CASES["model-reconciliation"]
    with pytest.raises(ValueError, match="linear_credit_total"):
        probe.validate_csv(case, _csv(case, ("10", "10", "10", "9.5")))

    csv_text = "conversion_id," + ",".join(case.columns) + "," + ",".join(probe.SUPPRESSION) + "\n"
    with pytest.raises(ValueError, match="header"):
        probe.validate_csv(case, csv_text)


def test_terminal_evidence_is_persisted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _Api()
    plan = probe.resolve_execution(_request(api, tmp_path))
    case = probe.CASES["conversion-id-cte"]

    def fake_fetch(_url: str, *, max_bytes: int) -> str:
        del max_bytes
        return _csv(case, ("10", "17"))

    monkeypatch.setattr(probe, "fetch_csv", fake_fetch)

    summary = probe.collect_terminal(api, plan)

    assert summary["outcome"] == "VIABLE"
    assert (tmp_path / f"{plan.stem}.terminal.json").is_file()
    assert (tmp_path / f"{plan.stem}.summary.json").is_file()
    assert (tmp_path / f"{plan.stem}.csv").is_file()


def test_suppression_columns_may_use_service_selected_order() -> None:
    case = probe.CASES["conversion-id-cte"]
    headers = (*case.columns, "distinct_user_count", "suppression_reason", "rows_suppressed")
    row = ("10", "17", "100", "", "false")

    result = probe.validate_csv(case, ",".join(headers) + "\n" + ",".join(row) + "\n")

    assert result["outcome"] == "VIABLE"


def test_revenue_reconciliation_is_currency_scoped_and_exact() -> None:
    case = probe.CASES["temporal-revenue-reconciliation"]
    headers = (*case.columns, *probe.SUPPRESSION)
    rows = (
        ("USD", "10", "100.0000", "100.0000", "100.0000", "100.000000", "false", "", "100"),
        ("CAD", "4", "35.5000", "35.5000", "35.5000", "35.500000", "false", "", "100"),
    )
    csv_text = ",".join(headers) + "\n" + "".join(",".join(row) + "\n" for row in rows)

    result = probe.validate_csv(case, csv_text)

    assert result["outcome"] == "VIABLE"
    assert len(cast("list[object]", result["rows"])) == 2


def test_revenue_reconciliation_refuses_bare_or_unreconciled_money() -> None:
    case = probe.CASES["temporal-revenue-reconciliation"]
    headers = (*case.columns, *probe.SUPPRESSION)
    bare = ("", "10", "100", "100", "100", "100", "false", "", "100")
    with pytest.raises(ValueError, match="bare monetary"):
        probe.validate_csv(case, ",".join(headers) + "\n" + ",".join(bare) + "\n")

    wrong = ("USD", "10", "100", "99", "100", "100", "false", "", "100")
    with pytest.raises(ValueError, match="first_revenue_total"):
        probe.validate_csv(case, ",".join(headers) + "\n" + ",".join(wrong) + "\n")


def test_production_shape_reconciles_models_without_sensitive_output() -> None:
    case = probe.CASES["production-shape"]
    headers = (*case.columns, *probe.SUPPRESSION)
    rows: list[tuple[str, ...]] = []
    for model in sorted(probe.MODEL_KEYS):
        rows.append(
            (
                "2026-08-22",
                "amazon_dsp",
                "display",
                "AMAZON.COM",
                model,
                "10.000000",
                "100.0000",
                "USD",
                "2026-08-22T00:00:00Z",
                "2026-08-24T00:00:00Z",
                "false",
                "",
                "100",
            )
        )
    csv_text = ",".join(headers) + "\n" + "".join(",".join(row) + "\n" for row in rows)

    result = probe.validate_csv(case, csv_text)

    assert result["outcome"] == "VIABLE"
    assert result["row_count"] == 3
    assert result["currencies"] == ["USD"]


def test_production_shape_refuses_missing_model_or_suppressed_rows() -> None:
    case = probe.CASES["production-shape"]
    headers = (*case.columns, *probe.SUPPRESSION)
    base = [
        "2026-08-22",
        "amazon_dsp",
        "display",
        "AMAZON.COM",
        "unknown_model",
        "10",
        "100",
        "USD",
        "2026-08-22T00:00:00Z",
        "2026-08-24T00:00:00Z",
        "false",
        "",
        "100",
    ]
    with pytest.raises(ValueError, match="unknown attribution model"):
        probe.validate_csv(case, ",".join(headers) + "\n" + ",".join(base) + "\n")

    base[4] = "first_touch_v1"
    base[10] = "true"
    assert (
        probe.validate_csv(case, ",".join(headers) + "\n" + ",".join(base) + "\n")["outcome"]
        == "PRIVACY_BLOCKED"
    )
