# SPDX-License-Identifier: MIT-0
"""The Reach quarter partition probe: per-pack counts, the raising call, no identifiers (05 §3)."""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))

from validation import probe_reach_quarter_partitions as probe  # noqa: E402

from adapters.outbound.publication_selection import (  # noqa: E402
    SELECTION_TOO_LARGE,
    PublicationSelector,
)
from domain.shared_kernel.errors import ValidationError  # noqa: E402

pytestmark = pytest.mark.unit

_POPULATION = "population-fictional-7f3a"
_LENS = "lens-fictional-91b2"


def _partition(dataset: str, day: date, grain: str = "day") -> Any:
    return SimpleNamespace(
        scope=SimpleNamespace(
            dataset=SimpleNamespace(value=dataset),
            population_key=_POPULATION,
            lens=SimpleNamespace(value=_LENS),
        ),
        bucket=SimpleNamespace(
            grain=SimpleNamespace(value=grain),
            window=SimpleNamespace(start=day, end=day, days=1),
        ),
    )


def _days(dataset: str, count: int) -> list[Any]:
    return [_partition(dataset, date(2026, 7, 1) + timedelta(days=index)) for index in range(count)]


def _plan(metric: str, fact: str) -> Any:
    return SimpleNamespace(
        metric=SimpleNamespace(name=metric),
        fact=fact,
        filters={"time_grain": "quarter", "grain_set": "platform"},
    )


def test_a_call_summary_counts_per_pack_and_names_no_partition_or_population() -> None:
    call = probe.SelectionCall(
        "select",
        ["amc-exposure-frequency-platform", "amc-exposure-frequency-channel"],
        [
            {
                "metric": "shoppers_in_frequency_bucket",
                "fact": "fct_exposure_frequency",
                "time_grain": "quarter",
                "grain_set": "platform",
            }
        ]
        * 2,
        [*_days("pack-6", 92), *_days("pack-7", 92)],
    )
    summary = call.summary()
    assert summary["partitions_total"] == 184
    by_dataset = cast("dict[str, dict[str, Any]]", summary["by_dataset"])
    assert {name: item["partitions"] for name, item in by_dataset.items()} == {
        "pack-6": 92,
        "pack-7": 92,
    }
    assert by_dataset["pack-6"]["bucket_grains"] == {"day": 92}
    assert by_dataset["pack-6"]["window"] == ["2026-07-01", "2026-09-30"]
    assert by_dataset["pack-6"]["populations"] == 1
    assert summary["plans"] == [
        {
            "metric": "shoppers_in_frequency_bucket",
            "fact": "fct_exposure_frequency",
            "time_grain": "quarter",
            "grain_set": "platform",
            "plans": 2,
        }
    ]
    encoded = json.dumps(summary)
    assert _POPULATION not in encoded and _LENS not in encoded


def test_the_recorder_names_the_raising_call_and_its_count(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = SimpleNamespace(requirements=(), partitions=tuple(_days("pack-6", 101)))
    monkeypatch.setattr(
        PublicationSelector,
        "_resolve_requirements",
        lambda *_arguments: resolved,  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    )
    recorder = probe.Recorder()
    uninstall = probe.install(recorder)
    try:
        selector = PublicationSelector.__new__(PublicationSelector)
        with pytest.raises(ValidationError) as raised:
            selector.resolve_partitions(
                (_plan("shoppers_in_frequency_bucket", "fct_exposure_frequency"),),
                (cast("Any", SimpleNamespace(contract_id="amc-exposure-frequency-platform")),),
            )
    finally:
        uninstall()
    assert raised.value.code == SELECTION_TOO_LARGE
    (call,) = [item.summary() for item in recorder.calls]
    assert call["caller"] == "resolve_partitions"
    assert call["raised"] == {
        "code": str(SELECTION_TOO_LARGE),
        "required_partitions": 101,
        "maximum_partitions": 100,
    }
    verdict = probe.classify([call])
    assert (verdict["raising_call"], verdict["partitions_total"]) == ("resolve_partitions", 101)
    assert verdict["max_partitions_in_one_dataset"] == 101
    # Paired: uninstalling restores the real methods, so nothing outside the probe is wrapped.
    assert PublicationSelector.__dict__["resolve_partitions"].__name__ == "resolve_partitions"


def test_a_resolution_within_the_bound_records_no_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = SimpleNamespace(requirements=(), partitions=tuple(_days("pack-6", 4)))
    monkeypatch.setattr(
        PublicationSelector,
        "_resolve_requirements",
        lambda *_arguments: resolved,  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    )
    recorder = probe.Recorder()
    uninstall = probe.install(recorder)
    try:
        selector = PublicationSelector.__new__(PublicationSelector)
        assert len(selector.resolve_partitions((), ())) == 4
    finally:
        uninstall()
    (call,) = [item.summary() for item in recorder.calls]
    assert call["raised"] is None
    assert probe.classify([call])["raising_call"] is None


class _Client:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def get_item(self, **_arguments: object) -> dict[str, object]:
        self.sent.append("get_item")
        return {}

    def put_item(self, **_arguments: object) -> dict[str, object]:
        self.sent.append("put_item")
        return {}


def test_the_client_guard_sends_reads_and_refuses_every_write() -> None:
    client = _Client()
    guarded = probe.ReadOnlyDynamoDB(client)
    guarded.get_item(TableName="t", Key={})
    # Control: a write never reaches the client.
    with pytest.raises(probe.ProbeError, match="read-only"):
        guarded.put_item(TableName="t", Item={})
    assert client.sent == ["get_item"] and guarded.calls == {"get_item": 1}


_ARGS = [
    "--profile",
    "p",
    "--environment",
    "dev",
    "--region",
    "us-east-1",
    "--expected-account",
    "000000000000",
    "--publications-table",
    "t",
    "--graph-table",
    "g",
    "--run-id",
    "r",
]


def test_the_probe_refuses_to_run_without_an_output_path() -> None:
    with pytest.raises(SystemExit):
        probe.main(_ARGS)


def test_another_account_measures_nothing_and_writes_nothing(tmp_path: Path) -> None:
    class _Sts:
        def get_caller_identity(self) -> dict[str, str]:
            return {"Account": "111111111111"}

    class _Session:
        def __init__(self, **_arguments: object) -> None:
            return None

        def client(self, name: str) -> object:
            assert name == "sts", f"no {name} call before the identity check"
            return _Sts()

    output = tmp_path / "probe.json"
    assert probe.main([*_ARGS, "--output", str(output)], session_factory=_Session) == 2
    assert not output.exists()


def test_chat_outcomes_are_recorded_per_question(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each quarterly question records an answer or its refusal code, never raising out."""

    class _Refusing:
        def request_from(self, *_arguments: object) -> object:
            raise ValidationError(SELECTION_TOO_LARGE, "too many partitions")

        @staticmethod
        def tool(name: str) -> str:
            return name

    monkeypatch.setattr(probe, "MeasurementDispatch", lambda **_keywords: _Refusing())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    outcomes = probe.ask_chat(object(), cast("Any", None))
    assert outcomes == {
        label: {"outcome": "refused", "code": str(SELECTION_TOO_LARGE), "cause_code": None}
        for label, _ in probe.CHAT_QUESTIONS
    }
