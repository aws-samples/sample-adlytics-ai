# SPDX-License-Identifier: MIT-0
"""Local producer CLI commands share the governed conformance implementation."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import cast

import pytest

from adapters.jobs.producer import __main__ as producer_cli
from adapters.producer.packs import pack_by_id

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-050", "ADR-053")]
ROOT = Path(__file__).resolve().parents[2]


def _plan_args(pack_id: str = "amc-dsp-impressions") -> list[str]:
    return [
        "plan-landing",
        pack_id,
        "--execution-id",
        "integration_probe_cli_001",
        "--workflow-id",
        f"adlytics_{pack_id.replace('-', '_')}_v1",
        "--schedule-kind",
        "adhoc",
        "--source-scope-namespace",
        "amc.entity",
        "--source-scope-value",
        "fictional-cli-entity",
        "--marketplace",
        "US",
        "--period-start",
        "2026-08-10",
        "--period-end",
        "2026-08-16",
        "--time-window-start",
        "2026-08-10T00:00:00+00:00",
        "--time-window-end",
        "2026-08-17T00:00:00+00:00",
        "--source-watermark",
        "2026-08-19",
        "--retrieved-at",
        "2026-08-19T06:00:00+00:00",
        "--object-version-id",
        "incoming-version-001",
    ]


def _result(path: Path, registration: dict[str, object]) -> None:
    pack = pack_by_id(ROOT, str(cast_dict(registration["query_pack"])["id"]))
    columns = list(pack.expected_columns)
    options = cast_dict(registration["option_set"])
    row = dict.fromkeys(columns, "1")
    row["time_window_start"] = str(options["timeWindowStart"])
    row["time_window_end"] = str(options["timeWindowEnd"])
    row["rows_suppressed"] = "false"
    row["suppression_reason"] = ""
    row["distinct_user_count"] = "2"
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerow(row)
    path.write_text(stream.getvalue())


def cast_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)


def test_pack_describe_exposes_exact_query_and_all_governed_digests(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert producer_cli.main(["pack-describe", "amc-dsp-clicks"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["packId"] == "amc-dsp-clicks"
    assert document["sql"].startswith("SELECT")
    for key in ("packDigest", "queryDigest"):
        assert document[key].startswith("sha256:")
    assert document["contract"]["digest"].startswith("sha256:")
    assert document["sourcePack"]["digest"].startswith("sha256:")


def test_plan_validate_result_and_render_manifest_share_one_registration(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert producer_cli.main(_plan_args()) == 0
    registration = json.loads(capsys.readouterr().out)
    assert registration["schema_version"] == "2"
    assert registration["source_scope"] == {
        "namespace": "amc.entity",
        "value": "fictional-cli-entity",
    }
    registration_path = tmp_path / "registration.json"
    registration_path.write_text(json.dumps(registration))
    result_path = tmp_path / "result.csv"
    _result(result_path, registration)

    assert producer_cli.main(["validate-registration", str(registration_path)]) == 0
    assert "conformance: OK" in capsys.readouterr().out
    assert (
        producer_cli.main(
            [
                "validate-result",
                "amc-dsp-impressions",
                str(result_path),
                "--registration",
                str(registration_path),
            ]
        )
        == 0
    )
    assert "conformance: OK" in capsys.readouterr().out
    assert (
        producer_cli.main(
            [
                "render-manifest",
                str(registration_path),
                str(result_path),
                "--destination-version-id",
                "canonical-version-001",
            ]
        )
        == 0
    )
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["objects"][0]["version_id"] == "canonical-version-001"
    assert manifest["source_scope"] == registration["source_scope"]
    assert manifest["governance"]["query_pack_id"] == "amc-dsp-impressions"


def test_reach_pack_describes_its_runtime_suppression_refusal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert producer_cli.main(["pack-describe", "amc-reach-frequency"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["rejectSuppressedRows"] is True
    assert document["safetyBlockers"] == []
