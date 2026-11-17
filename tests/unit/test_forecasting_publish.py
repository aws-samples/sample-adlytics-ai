# SPDX-License-Identifier: MIT-0
"""The publish rules (design §6b, §6c): identity, binding, receipts, pointer and rollback.

A lax check here publishes a forecast from the wrong attempt or input, one the gate would not
have chosen, or a title from another product; a wrong pointer rule lets an older or superseded
run replace what planners see, or a rollback land on a generation that was never published.
The handler part reads the objects the way the Lambda does: a missing, altered or short roster
part that published anyway would show planners a generation the shards never produced, and a
transient read error that held the run would stop its retries.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast

import pytest
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError, ReadTimeoutError
from forecast_series_support import INPUT_END, asin, policy
from forecast_store_support import ForecastTableFake, LambdaCrashed

from adapters.jobs.demand_forecast.json_output import REPORT_FILE, SERIES_FILE, series_bytes
from adapters.jobs.demand_forecast_publisher.handler import (
    ERROR_UNREADABLE,
    REQUEST_INVALID,
    ObjectMissingError,
    ObjectStoreError,
    Publisher,
    PublishFailedError,
    PublishHeld,
    PublishShardRequest,
    RecordOutcomeRequest,
    StepOutcome,
    commit_generation,
    commit_request,
    failure_category,
    publish_shard,
    record_outcome,
    record_request,
    rollback,
    s3_error,
)
from adapters.outbound.demand_forecast_policy import LoadedPolicy
from adapters.outbound.dynamodb.demand_forecast_store import (
    STORE_CLIENT_CONFIG,
    DynamoDbForecastStore,
)
from adapters.outbound.telemetry.demand_forecast_metrics import Datapoint, MetricEmitError
from domain.forecasting.boundary import (
    CandidateSeriesV1,
    InputManifestV1,
    RosterEntryV1,
    RunReportV1,
    TitleMapV1,
    WeekStateCountsV1,
    boundary_schema_digests,
)
from domain.forecasting.identity import display_asin
from domain.forecasting.list_pages import cut_pages, rank_entries
from domain.forecasting.models import SkipReason
from domain.forecasting.policy import TARGETS
from domain.forecasting.publish import (
    CommitValidationRequest,
    CurrentPointer,
    Disposition,
    GenerationRecord,
    HeldCheck,
    LastAttempt,
    OutcomeRecord,
    PointerAdvanceRequest,
    PointerDecision,
    PublishedGeneration,
    ReceiptKeyRequest,
    ReceiptOnlyOutcomeError,
    ReceiptRecord,
    RollbackRefusal,
    RollbackRefusedError,
    RollbackRequest,
    RollbackState,
    RosterPart,
    RunOutcome,
    ShardAttempt,
    ShardCandidateValidationRequest,
    ShardPublishResult,
    forecast_set_id,
    generation_id,
    next_pointer,
    receipt_sk,
    retry_disposition,
    rollback_pointer,
    validate_commit,
    validate_shard_candidate,
)
from domain.forecasting.selection import (
    SelectBasis,
    SelectDecision,
    SelectDecisionRequest,
    SelectOutcome,
    Trigger,
    select_decision,
)
from domain.forecasting.summaries import roster_entry
from ports.outbound.forecast_store import (
    Attestation,
    CommitDisposition,
    ForecastStoreConflictError,
    ForecastStoreThrottled,
    GenerationMeta,
    GenerationSeries,
    OutcomeCommitRequest,
    RunReceipt,
    superseded_receipt,
)

pytestmark = pytest.mark.unit

RUN = "run-1"
FINGERPRINT = "f" * 64
SERIES = "b" * 64
HEX = "a" * 64
SHARDS = ((1, 3), (5, 7), (9, 11))
"""Three shards of two ASINs each, with gaps, so an ASIN can sit inside a shard's bounds."""
AT = datetime.combine(INPUT_END + timedelta(days=5), time(13), tzinfo=UTC)
"""The Thursday check after `input_end`, derived so no test pins a calendar date."""


def _evaluations(chronos_wql: str = "0.4000") -> list[dict[str, Any]]:
    def valid(model: str, wql: str) -> dict[str, Any]:
        score = {"wql": wql, "mase": "1.0000", "mase_gap": None}
        return {
            "model": model,
            "backtest_score": {**score, "weeks_in_range": 13, "weeks_scored": 13},
            "validity_reason": None,
            "origin_status": "publishable",
            "origin_failure": None,
        }

    return [
        valid("chronos2", chronos_wql),
        valid("seasonal_naive", "0.5000"),
        valid("ets", "0.6000"),
    ]


def _target(**changes: Any) -> dict[str, Any]:
    bands = {"p10": ["1.0000"] * 13, "p50": ["2.0000"] * 13, "p90": ["3.0000"] * 13}
    target = {
        "chosen_model": "chronos2",
        "withheld_reason": None,
        "chosen_forecast": bands,
        "gate_reason": None,
        "scored_weeks": 13,
        "evaluations": _evaluations(),
    }
    return {**target, **changes}


def _week(start: date) -> dict[str, Any]:
    return {
        "week_start": start.isoformat(),
        "targets": {t: {"state": "observed", "value": "2.0000"} for t in TARGETS},
        "stockout": False,
    }


def _grid(input_end: date) -> list[date]:
    last = input_end - timedelta(days=6)
    return [last - timedelta(weeks=12 - n) for n in range(13)]


def _candidate(n: int, shard: int, **changes: Any) -> CandidateSeriesV1:
    record = {
        "schema_version": 1,
        "canonical_asin": asin(n),
        "display_asin": display_asin(asin(n)),
        "shard_index": shard,
        "targets": {t: _target() for t in TARGETS},
        "recent_weeks": [_week(start) for start in _grid(INPUT_END)],
        "week_state_counts": {"observed": 104, "inferred_zero": 0, "unknown": 0},
        "stockout_weeks_last_13": 0,
        "currency_code": "USD",
    }
    return CandidateSeriesV1.model_validate({**record, **changes})


def _manifest(**changes: Any) -> InputManifestV1:
    digests = boundary_schema_digests()
    document: dict[str, Any] = {
        "schema_version": 1,
        "run_id": RUN,
        "build_attempt_id": "r0a0",
        "set": {
            "marketplace_key": "amazon_us",
            "distributor_view": "MANUFACTURING",
            "selling_program": "RETAIL",
        },
        "input_end": INPUT_END.isoformat(),
        "expected_fence": 0,
        "input_fingerprint": HEX,
        "run_fingerprint": FINGERPRINT,
        "run_identity": {
            "input_fingerprint": HEX,
            "policy_sha256": HEX,
            "policy_section_versions": {"engine": 1},
            "processor_code_identity": HEX,
            "lock_digest": HEX,
            "model_id": "amazon/chronos-2",
            "model_artifact_digest": HEX,
            "boundary_schema_digests": digests,
            "instance_type": "ml.m5.xlarge",
            "max_runtime_seconds": 3600,
        },
        "pins": {"sales": [], "traffic": [], "products": []},
        "shards": [
            {
                "index": index,
                "asin_count": 2,
                "first_canonical_asin": asin(first),
                "last_canonical_asin": asin(last),
            }
            for index, (first, last) in enumerate(SHARDS)
        ],
        "parquet_objects": [],
        "titles_key": "titles.json",
        "titles_sha256": HEX,
        "coverage": {"sales": [], "traffic": []},
        "newest_publication_published_at": (AT - timedelta(days=3)).isoformat(),
        "boundary_schema_digests": digests,
    }
    return InputManifestV1.model_validate({**document, **changes})


def _report(shard: int, **changes: Any) -> RunReportV1:
    document = {
        "schema_version": 1,
        "run_id": RUN,
        "shard_index": shard,
        "asin_count": 2,
        "attempt_id": "r0a0",
        "input_attempt_id": "r0a0",
        "series_sha256": SERIES,
        "counts_by_reason": {},
        "week_state_counts": {"observed": 1, "inferred_zero": 0, "unknown": 0},
        "inferred_zero_days": 0,
        "durations_ms": {"forecast": 1},
        "policy_section_versions": {"engine": 1},
        "policy_sha256": HEX,
        "image_identity": HEX,
        "lock_digest": HEX,
        "model_id": "amazon/chronos-2",
        "model_artifact_digest": HEX,
        "boundary_schema_digests": boundary_schema_digests(),
        "run_fingerprint": FINGERPRINT,
    }
    return RunReportV1.model_validate({**document, **changes})


def _titles(numbers: tuple[int, ...] = (1, 3, 5, 7, 9, 11)) -> TitleMapV1:
    entries = [
        {"canonical_asin": asin(n), "display_asin": display_asin(asin(n)), "title": f"P{n}"}
        for n in sorted(numbers)
    ]
    return TitleMapV1(schema_version=1, entries=entries)  # pyright: ignore[reportArgumentType]


def _shard_request(shard: int = 1, **changes: Any) -> ShardCandidateValidationRequest:
    first, last = SHARDS[shard % len(SHARDS)]
    request = ShardCandidateValidationRequest(
        run_id=RUN,
        shard=shard,
        attempt_id="r0a0",
        manifest=_manifest(),
        report=_report(shard),
        records=(_candidate(first, shard), _candidate(last, shard)),
        series_sha256=SERIES,
        titles=_titles(),
        margin=0.0,
    )
    return replace(request, **changes)


def _held(request: ShardCandidateValidationRequest) -> HeldCheck | None:
    held = validate_shard_candidate(request)
    return None if held is None else held.check


# --- Generation identity --------------------------------------------------------------------


def test_generation_id_is_the_run_and_the_attempt_list_digest() -> None:
    attempts = [ShardAttempt(2, "r0a0"), ShardAttempt(0, "r0a0"), ShardAttempt(1, "r0a1")]
    assert generation_id(RUN, attempts) == "run-1-6d315e68e0b9"


def test_a_redriven_publish_of_the_same_attempts_targets_the_same_generation() -> None:
    first = [ShardAttempt(0, "r0a0"), ShardAttempt(1, "r0a1"), ShardAttempt(2, "r0a0")]
    assert generation_id(RUN, first) == generation_id(RUN, list(reversed(first)))


def test_a_late_attempt_gets_its_own_generation() -> None:
    published = [ShardAttempt(0, "r0a0"), ShardAttempt(1, "r0a0"), ShardAttempt(2, "r0a0")]
    late = [ShardAttempt(0, "r0a0"), ShardAttempt(1, "r0a1"), ShardAttempt(2, "r0a0")]
    assert generation_id(RUN, published) == "run-1-9a442b6277f3"
    assert generation_id(RUN, late) != generation_id(RUN, published)


@pytest.mark.parametrize(
    ("run_id", "attempts"),
    [
        pytest.param(RUN, [], id="no-attempts"),
        pytest.param(RUN, [ShardAttempt(0, "r0a0"), ShardAttempt(0, "r0a1")], id="shard-twice"),
        pytest.param("run#1", [ShardAttempt(0, "r0a0")], id="key-separator"),
        pytest.param("", [ShardAttempt(0, "r0a0")], id="empty-run"),
    ],
)
def test_generation_id_refuses_an_ambiguous_identity(
    run_id: str, attempts: list[ShardAttempt]
) -> None:
    with pytest.raises(ValueError, match=r"generation|run_id"):
        generation_id(run_id, attempts)


@pytest.mark.parametrize(("shard", "attempt"), [(100, "r0a0"), (-1, "r0a0"), (0, "a0r0")])
def test_a_shard_attempt_outside_the_plan_is_refused(shard: int, attempt: str) -> None:
    with pytest.raises(ValueError, match=r"shard index|attempt_id"):
        ShardAttempt(shard, attempt)


# --- Shard validation -----------------------------------------------------------------------


def test_every_shard_of_a_three_shard_run_passes() -> None:
    assert [validate_shard_candidate(_shard_request(shard)) for shard in range(3)] == [None] * 3


@pytest.mark.parametrize(
    ("changes", "detail"),
    [
        pytest.param({"run_id": "run-2"}, "manifest run_id", id="manifest-run"),
        pytest.param({"report": _report(1, run_id="run-2")}, "run report run_id", id="report-run"),
        pytest.param({"report": _report(0)}, "shard_index", id="report-shard"),
        pytest.param({"attempt_id": "r0a1"}, "attempt_id", id="attempt"),
        pytest.param(
            {"report": _report(1, input_attempt_id="r1a0")}, "input_attempt_id", id="input-attempt"
        ),
        pytest.param(
            {"report": _report(1, run_fingerprint="e" * 64)}, "run_fingerprint", id="fingerprint"
        ),
        pytest.param({"series_sha256": "c" * 64}, "series_sha256", id="series-digest"),
        pytest.param({"report": _report(1, asin_count=3)}, "asin_count", id="report-count"),
        pytest.param({"records": (_candidate(5, 1),)}, "record count", id="missing-record"),
        pytest.param(
            {"records": (_candidate(5, 1), _candidate(6, 1), _candidate(7, 1))},
            "record count",
            id="extra-record",
        ),
        pytest.param(
            {"records": (_candidate(5, 1), _candidate(9, 1))}, "outside", id="out-of-bounds"
        ),
        pytest.param({"records": (_candidate(5, 1), _candidate(5, 1))}, "twice", id="repeated"),
        pytest.param(
            {"records": (_candidate(5, 1), _candidate(7, 2))}, "shard_index 2", id="record-shard"
        ),
        pytest.param({"shard": 3, "report": _report(3)}, "not in the manifest", id="shard"),
    ],
)
def test_a_candidate_bound_to_another_identity_is_held(
    changes: dict[str, Any], detail: str
) -> None:
    held = validate_shard_candidate(_shard_request(**changes))
    assert held is not None
    assert held.category == "identity_mismatch"
    assert detail in held.detail


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"publisher_schema_digests": {"RunReportV1": HEX}}, id="publisher"),
        pytest.param(
            {"report": _report(1, boundary_schema_digests={"RunReportV1": HEX})}, id="report"
        ),
        pytest.param(
            {"manifest": _manifest(boundary_schema_digests={"InputManifestV1": HEX})},
            id="manifest",
        ),
    ],
)
def test_a_candidate_written_under_other_schemas_is_held(changes: dict[str, Any]) -> None:
    assert _held(_shard_request(**changes)) is HeldCheck.SCHEMA_MISMATCH


def test_a_candidate_off_the_input_end_week_grid_is_held() -> None:
    later = _manifest(input_end=(INPUT_END + timedelta(weeks=1)).isoformat())
    assert _held(_shard_request(manifest=later)) is HeldCheck.WEEK_GRID_MISMATCH


def test_an_input_end_that_is_not_a_saturday_is_held() -> None:
    friday = _manifest(input_end=(INPUT_END - timedelta(days=1)).isoformat())
    assert _held(_shard_request(manifest=friday)) is HeldCheck.WEEK_GRID_MISMATCH


def _with_target(n: int, shard: int, **target: Any) -> CandidateSeriesV1:
    targets = {t: _target() for t in TARGETS}
    targets["units_ordered"] = _target(**target)
    return _candidate(n, shard, targets=targets)


@pytest.mark.parametrize(
    ("target", "check"),
    [
        pytest.param(
            {"chosen_model": "seasonal_naive", "gate_reason": "CHRONOS_NOT_BETTER"},
            HeldCheck.GATE_MISMATCH,
            id="other-model-recorded",
        ),
        pytest.param({"gate_reason": "CHRONOS_NOT_BETTER"}, HeldCheck.GATE_MISMATCH, id="reason"),
        pytest.param(
            {"evaluations": _evaluations(chronos_wql="0.5000")},
            HeldCheck.GATE_MISMATCH,
            id="evaluation-changed",
        ),
        pytest.param(
            {
                "chosen_forecast": {
                    "p10": ["2.5000"] * 13,
                    "p50": ["2.0000"] * 13,
                    "p90": ["3.0000"] * 13,
                }
            },
            HeldCheck.FORECAST_BAND_UNORDERED,
            id="unordered",
        ),
        pytest.param(
            {
                "chosen_forecast": {
                    "p10": ["-1.0000"] * 13,
                    "p50": ["0.0000"] * 13,
                    "p90": ["1.0000"] * 13,
                }
            },
            HeldCheck.FORECAST_NEGATIVE,
            id="negative",
        ),
    ],
)
def test_a_record_the_gate_or_numeric_checks_refuse_is_held(
    target: dict[str, Any], check: HeldCheck
) -> None:
    records = (_candidate(5, 1), _with_target(7, 1, **target))
    assert _held(_shard_request(records=records)) is check


def test_a_shown_money_target_without_a_currency_is_held() -> None:
    records = (_candidate(5, 1), _candidate(7, 1, currency_code=None))
    assert _held(_shard_request(records=records)) is HeldCheck.CURRENCY_MISSING


def test_a_withheld_series_without_a_currency_passes() -> None:
    withheld = {
        "chosen_model": None,
        "withheld_reason": "ACCURACY_NOT_TESTABLE",
        "chosen_forecast": None,
        "evaluations": [
            {
                "model": model,
                "backtest_score": None,
                "validity_reason": "ETS_FIT_FAILED",
                "origin_status": "not_attempted",
                "origin_failure": None,
            }
            for model in ("chronos2", "seasonal_naive", "ets")
        ],
    }
    targets = {t: _target(**withheld) for t in TARGETS}
    record = _candidate(7, 1, targets=targets, currency_code=None)
    assert validate_shard_candidate(_shard_request(records=(_candidate(5, 1), record))) is None


def test_a_title_map_missing_one_entry_of_shard_1_holds_shard_1_only() -> None:
    titles = _titles((1, 3, 5, 9, 11))
    held = [_held(_shard_request(shard, titles=titles)) for shard in range(3)]
    assert held == [None, HeldCheck.TITLE_MAP_MISMATCH, None]


def test_a_title_map_entry_inside_a_shard_without_a_candidate_holds_that_shard() -> None:
    titles = _titles((1, 2, 3, 5, 7, 9, 11))
    held = [_held(_shard_request(shard, titles=titles)) for shard in range(3)]
    assert held == [HeldCheck.TITLE_MAP_MISMATCH, None, None]


def test_a_title_map_naming_another_display_asin_is_held() -> None:
    entries = list(_titles().entries)
    entries[2] = entries[2].model_copy(update={"display_asin": "B0OTHER001"})
    titles = TitleMapV1(schema_version=1, entries=tuple(entries))
    assert _held(_shard_request(1, titles=titles)) is HeldCheck.TITLE_MAP_MISMATCH


def test_a_title_map_entry_outside_every_shard_passes_each_shard() -> None:
    titles = _titles((1, 3, 5, 7, 9, 11, 20))
    assert [_held(_shard_request(shard, titles=titles)) for shard in range(3)] == [None] * 3


# --- Commit validation ----------------------------------------------------------------------


def _part(shard: int, *, sha: str | None = None, drop: int = 0) -> RosterPart:
    first, last = SHARDS[shard]
    entries = tuple(
        roster_entry(_candidate(n, shard), f"P{n}", title_truncated=False) for n in (first, last)
    )
    return RosterPart(shard, sha or f"{shard}" * 64, entries[drop:])


def _result(shard: int, **changes: Any) -> ShardPublishResult:
    result = ShardPublishResult(
        shard=shard,
        attempt_id="r0a0",
        series_sha256=SERIES,
        item_count=2,
        roster_part_key=f"runs/{RUN}/shards/{shard}/attempts/r0a0/roster-part.jsonl.gz",
        roster_part_sha256=f"{shard}" * 64,
    )
    return replace(result, **changes)


def _commit(**changes: Any) -> CommitValidationRequest:
    request = CommitValidationRequest(
        manifest=_manifest(),
        shard_results=tuple(_result(shard) for shard in range(3)),
        parts=tuple(_part(shard) for shard in range(3)),
        title_entry_count=6,
    )
    return replace(request, **changes)


def test_a_complete_commit_passes_with_counts_by_model_shown() -> None:
    checked = validate_commit(_commit())
    assert checked.held is None
    assert checked.counts_by_model == {f"{t}.chronos2": 6 for t in sorted(TARGETS)}
    assert checked.counts_by_reason == {}


@pytest.mark.parametrize(
    ("changes", "detail"),
    [
        pytest.param(
            {"shard_results": (_result(0), _result(2))}, "shard results", id="missing-shard"
        ),
        pytest.param(
            {"shard_results": (_result(0), _result(1), _result(2), _result(3))},
            "shard results",
            id="extra-shard",
        ),
        pytest.param(
            {"shard_results": (_result(0), _result(1), _result(1))},
            "shard results",
            id="repeated-shard",
        ),
        pytest.param({"parts": (_part(0), _part(2))}, "roster parts", id="missing-part"),
        pytest.param(
            {"shard_results": (_result(0), _result(1, item_count=3), _result(2))},
            "item_count",
            id="count",
        ),
        pytest.param(
            {"parts": (_part(0), _part(1, sha="9" * 64), _part(2))}, "digest", id="altered-part"
        ),
        pytest.param({"parts": (_part(0), _part(1, drop=1), _part(2))}, "lines", id="short-part"),
        pytest.param({"parts": (_part(0), _part(0), _part(2))}, "roster parts", id="part-twice"),
    ],
)
def test_a_commit_whose_parts_disagree_with_the_results_is_held(
    changes: dict[str, Any], detail: str
) -> None:
    held = validate_commit(_commit(**changes)).held
    assert held is not None
    assert held.check is HeldCheck.IDENTITY_MISMATCH
    assert detail in held.detail


def test_a_roster_part_line_with_a_rank_is_held_as_a_schema_mismatch() -> None:
    part = _part(1)
    ranked = RosterPart(
        1, part.sha256, (part.entries[0].model_copy(update={"rank": 1}), part.entries[1])
    )
    held = validate_commit(_commit(parts=(_part(0), ranked, _part(2)))).held
    assert held is not None
    assert held.check is HeldCheck.SCHEMA_MISMATCH


def test_one_display_asin_in_two_roster_lines_is_held() -> None:
    twin = RosterPart(2, _part(2).sha256, _part(1).entries)
    held = validate_commit(_commit(parts=(_part(0), _part(1), twin))).held
    assert held is not None
    assert held.check is HeldCheck.IDENTITY_MISMATCH


def test_a_title_map_with_one_extra_entry_holds_at_the_commit() -> None:
    held = validate_commit(_commit(title_entry_count=7)).held
    assert held is not None
    assert held.check is HeldCheck.TITLE_MAP_MISMATCH


def test_a_manifest_under_other_schemas_is_held_at_the_commit() -> None:
    manifest = _manifest(boundary_schema_digests={"InputManifestV1": HEX})
    held = validate_commit(_commit(manifest=manifest)).held
    assert held is not None
    assert held.check is HeldCheck.SCHEMA_MISMATCH


def test_a_target_no_shard_shows_is_held_target_empty() -> None:
    def withheld(part: RosterPart) -> RosterPart:
        entries: list[RosterEntryV1] = []
        for entry in part.entries:
            row = entry.targets["units_shipped"].model_copy(
                update={
                    "chosen_model": None,
                    "withheld_reason": SkipReason.ACCURACY_NOT_TESTABLE,
                    "p50_totals": dict.fromkeys(("4", "8", "13")),
                }
            )
            entries.append(
                entry.model_copy(update={"targets": {**entry.targets, "units_shipped": row}})
            )
        return RosterPart(part.shard, part.sha256, tuple(entries))

    checked = validate_commit(_commit(parts=tuple(withheld(_part(s)) for s in range(3))))
    assert checked.held is not None
    assert checked.held.check is HeldCheck.TARGET_EMPTY
    assert checked.held.detail == "no shown series for units_shipped"


# --- Receipt keys ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "step", ["select", "build-r0a0", "s3-r0a1", "s99-r2a0", "commit", "rollback"]
)
def test_each_step_has_its_own_receipt_key_per_pass(step: str) -> None:
    assert receipt_sk(ReceiptKeyRequest(RUN, 2, step)) == f"RUN#run-1#r2#{step}"


@pytest.mark.parametrize(
    ("run_id", "redrive", "step"),
    [
        (RUN, 0, "publish"),
        (RUN, 0, "build"),
        (RUN, 0, "s1-a0"),
        (RUN, 0, "s100-r0a0"),
        (RUN, 0, ""),
        (RUN, -1, "select"),
        ("run#1", 0, "select"),
    ],
)
def test_a_receipt_key_outside_the_step_grammar_is_refused(
    run_id: str, redrive: int, step: str
) -> None:
    with pytest.raises(ValueError, match=r"step|redrive_count|run_id|shard index"):
        ReceiptKeyRequest(run_id, redrive, step)


# --- Retry disposition ----------------------------------------------------------------------


def _attempt(
    outcome: RunOutcome, category: str | None, *, retries: int = 0, **changes: Any
) -> LastAttempt:
    disposition = changes.pop("disposition", Disposition.RETRY)
    attempt = LastAttempt(
        run_id=RUN,
        attempt_id="r0a0",
        run_fingerprint=FINGERPRINT,
        outcome=outcome,
        failure_category=category,
        retry_disposition=disposition,
        transient_retries=retries,
        at=AT,
    )
    return replace(attempt, **changes)


def _record(
    outcome: RunOutcome, category: str | None, previous: LastAttempt | None = None
) -> OutcomeRecord:
    return OutcomeRecord(outcome, category, FINGERPRINT, previous, policy().retry)


@pytest.mark.parametrize(
    "outcome",
    [RunOutcome.NO_OP_SAME_INPUT, RunOutcome.HELD_INPUT_NOT_READY, RunOutcome.SUPERSEDED],
)
def test_a_receipt_only_outcome_has_no_retry_disposition(outcome: RunOutcome) -> None:
    with pytest.raises(ReceiptOnlyOutcomeError, match="receipt-only"):
        retry_disposition(_record(outcome, None))


@pytest.mark.parametrize(
    ("outcome", "category"),
    [
        (RunOutcome.PUBLISHED, None),
        (RunOutcome.ROLLED_BACK, None),
        (RunOutcome.HELD_QUALITY, "gate_mismatch"),
        (RunOutcome.FAILED, "target_empty"),
        (RunOutcome.FAILED, "job:MODEL_ARTIFACT_MISMATCH"),
        (RunOutcome.FAILED, "input_build:ROSTER_TOO_LARGE"),
    ],
)
def test_a_deterministic_outcome_suppresses(outcome: RunOutcome, category: str | None) -> None:
    decision = retry_disposition(_record(outcome, category))
    assert (decision.disposition, decision.failure_category) == (Disposition.SUPPRESS, category)


def test_a_transient_failure_retries_three_times_then_suppresses() -> None:
    previous: LastAttempt | None = None
    seen: list[tuple[Disposition, str | None, int]] = []
    for _ in range(4):
        decision = retry_disposition(_record(RunOutcome.FAILED, "publish:SlowDown", previous))
        seen.append((decision.disposition, decision.failure_category, decision.transient_retries))
        previous = _attempt(
            RunOutcome.FAILED,
            decision.failure_category,
            retries=decision.transient_retries,
            disposition=decision.disposition,
        )
    assert seen == [
        (Disposition.RETRY, "publish:SlowDown", 0),
        (Disposition.RETRY, "publish:SlowDown", 1),
        (Disposition.RETRY, "publish:SlowDown", 2),
        (Disposition.SUPPRESS, "retry_exhausted", 3),
    ]


def test_a_new_fingerprint_starts_its_own_retry_count() -> None:
    previous = _attempt(RunOutcome.FAILED, "publish:SlowDown", retries=2, run_fingerprint="e" * 64)
    decision = retry_disposition(_record(RunOutcome.FAILED, "publish:SlowDown", previous))
    assert (decision.disposition, decision.transient_retries) == (Disposition.RETRY, 0)


def test_a_transient_failure_after_a_deterministic_one_starts_counting_at_zero() -> None:
    previous = _attempt(RunOutcome.FAILED, "target_empty", disposition=Disposition.SUPPRESS)
    decision = retry_disposition(_record(RunOutcome.FAILED, "job:ResourceLimitExceeded", previous))
    assert (decision.disposition, decision.transient_retries) == (Disposition.RETRY, 0)


@pytest.mark.parametrize(
    ("outcome", "category"),
    [(RunOutcome.FAILED, None), (RunOutcome.HELD_QUALITY, ""), (RunOutcome.PUBLISHED, "x")],
)
def test_an_outcome_with_the_wrong_category_shape_is_refused(
    outcome: RunOutcome, category: str | None
) -> None:
    with pytest.raises(ValueError, match="failure_category"):
        _record(outcome, category)


def test_last_attempt_never_records_a_receipt_only_outcome() -> None:
    with pytest.raises(ValueError, match="receipt-only"):
        _attempt(RunOutcome.SUPERSEDED, None)


# --- Pointer advance ------------------------------------------------------------------------

G1 = PublishedGeneration("run-1-aaaaaaaaaaaa", INPUT_END, FINGERPRINT, AT)
G0 = PublishedGeneration(
    "run-0-bbbbbbbbbbbb", INPUT_END - timedelta(weeks=1), "e" * 64, AT - timedelta(weeks=1)
)
PUBLISHED = _attempt(RunOutcome.PUBLISHED, None, disposition=Disposition.SUPPRESS)
HELD = _attempt(RunOutcome.HELD_QUALITY, "gate_mismatch", disposition=Disposition.SUPPRESS)


def _current(generation: PublishedGeneration | None, fence: int) -> CurrentPointer:
    if generation is None:
        return CurrentPointer(fence=fence, last_attempt=HELD)
    return CurrentPointer(
        fence=fence,
        generation_id=generation.generation_id,
        input_end=generation.input_end,
        published_at=generation.published_at,
        run_fingerprint=generation.run_fingerprint,
    )


@pytest.mark.parametrize(
    ("current", "expected_fence", "fence_after"),
    [
        pytest.param(None, 0, 1, id="empty-table"),
        pytest.param(_current(None, 0), 0, 1, id="item-without-generation"),
        pytest.param(_current(G0, 3), 3, 4, id="existing-item"),
    ],
)
def test_a_published_outcome_advances_the_fence_by_one(
    current: CurrentPointer | None, expected_fence: int, fence_after: int
) -> None:
    advance = next_pointer(PointerAdvanceRequest(current, expected_fence, PUBLISHED, G1))
    assert advance.decision is PointerDecision.ADVANCE
    assert advance.pointer == CurrentPointer(
        fence=fence_after,
        generation_id="run-1-aaaaaaaaaaaa",
        input_end=INPUT_END,
        published_at=AT,
        run_fingerprint=FINGERPRINT,
        last_attempt=PUBLISHED,
    )


@pytest.mark.parametrize(
    ("current", "expected_fence"),
    [
        pytest.param(_current(G0, 4), 3, id="fence-moved"),
        pytest.param(_current(G0, 1), 0, id="published-since-read-empty"),
        pytest.param(None, 2, id="item-gone"),
        pytest.param(
            _current(replace(G0, input_end=INPUT_END + timedelta(weeks=1)), 3), 3, id="older-input"
        ),
    ],
)
def test_a_publish_whose_fence_condition_fails_is_superseded(
    current: CurrentPointer | None, expected_fence: int
) -> None:
    advance = next_pointer(PointerAdvanceRequest(current, expected_fence, PUBLISHED, G1))
    assert (advance.decision, advance.pointer) == (PointerDecision.SUPERSEDED, None)


def test_a_retry_after_an_unseen_success_records_nothing_more() -> None:
    advance = next_pointer(PointerAdvanceRequest(_current(G1, 4), 3, PUBLISHED, G1))
    assert (advance.decision, advance.pointer) == (PointerDecision.ALREADY_CURRENT, None)


def test_a_held_outcome_on_an_empty_table_creates_the_item_at_fence_zero() -> None:
    advance = next_pointer(PointerAdvanceRequest(None, 0, HELD))
    assert advance.pointer == CurrentPointer(fence=0, last_attempt=HELD)


def test_a_held_outcome_keeps_the_current_generation_and_fence() -> None:
    advance = next_pointer(PointerAdvanceRequest(_current(G0, 3), 3, HELD))
    assert advance.pointer == replace(_current(G0, 3), last_attempt=HELD)


def test_a_held_outcome_whose_fence_moved_is_superseded() -> None:
    advance = next_pointer(PointerAdvanceRequest(_current(G0, 4), 3, HELD))
    assert advance.decision is PointerDecision.SUPERSEDED


@pytest.mark.parametrize(
    ("attempt", "generation"),
    [(PUBLISHED, None), (HELD, G1), (_attempt(RunOutcome.ROLLED_BACK, None), None)],
)
def test_a_pointer_request_with_the_wrong_shape_is_refused(
    attempt: LastAttempt, generation: PublishedGeneration | None
) -> None:
    with pytest.raises(ValueError, match=r"generation|rollback"):
        PointerAdvanceRequest(None, 0, attempt, generation)


# --- Rollback -------------------------------------------------------------------------------

SET_ID = "set-1"
G1_META = GenerationRecord(G1.generation_id, SET_ID, "RUN#run-1#r0#commit", INPUT_END, FINGERPRINT)
G1_RECEIPT = ReceiptRecord("RUN#run-1#r0#commit", RunOutcome.PUBLISHED, G1.generation_id, AT)
G2 = PublishedGeneration(
    "run-2-cccccccccccc", INPUT_END + timedelta(weeks=1), "d" * 64, AT + timedelta(weeks=1)
)
LATER = AT + timedelta(weeks=2)


def _rollback_state(**changes: Any) -> RollbackState:
    state = RollbackState(_current(G2, 5), G1_META, G1_RECEIPT, "rollback-1", LATER)
    return replace(state, **changes)


def _rollback(**changes: Any) -> RollbackRequest:
    request = RollbackRequest(SET_ID, G1.generation_id, 5, "bad model")
    return replace(request, **changes)


def test_a_rollback_restores_a_published_generation_and_suppresses_the_bad_one() -> None:
    plan = rollback_pointer(_rollback(), _rollback_state())
    assert plan.receipt_sk == "RUN#rollback-1#r0#rollback"
    assert plan.pointer == CurrentPointer(
        fence=6,
        generation_id=G1.generation_id,
        input_end=INPUT_END,
        published_at=AT,
        run_fingerprint=FINGERPRINT,
        rolled_back_from=G2.generation_id,
        last_attempt=LastAttempt(
            run_id="rollback-1",
            attempt_id=None,
            run_fingerprint="d" * 64,
            outcome=RunOutcome.ROLLED_BACK,
            failure_category=None,
            retry_disposition=Disposition.SUPPRESS,
            transient_retries=0,
            at=LATER,
        ),
    )


@pytest.mark.parametrize(
    ("request_changes", "state_changes", "code"),
    [
        pytest.param({}, {"target": None}, RollbackRefusal.TARGET_UNKNOWN, id="no-meta"),
        pytest.param({"set_id": "set-2"}, {}, RollbackRefusal.TARGET_OTHER_SET, id="other-set"),
        pytest.param({}, {"current": None}, RollbackRefusal.NOTHING_CURRENT, id="no-current"),
        pytest.param(
            {},
            {"current": _current(None, 0)},
            RollbackRefusal.NOTHING_CURRENT,
            id="never-published",
        ),
        pytest.param(
            {}, {"current": _current(G1, 5)}, RollbackRefusal.TARGET_IS_CURRENT, id="is-current"
        ),
        pytest.param(
            {}, {"receipt": None}, RollbackRefusal.TARGET_NEVER_PUBLISHED, id="no-receipt"
        ),
        pytest.param(
            {},
            {"receipt": replace(G1_RECEIPT, outcome=RunOutcome.SUPERSEDED)},
            RollbackRefusal.TARGET_NEVER_PUBLISHED,
            id="superseded-receipt",
        ),
        pytest.param(
            {},
            {"receipt": replace(G1_RECEIPT, generation_id=G2.generation_id)},
            RollbackRefusal.TARGET_NEVER_PUBLISHED,
            id="receipt-for-another",
        ),
        pytest.param(
            {},
            {"receipt": replace(G1_RECEIPT, sk="RUN#run-1#r1#commit")},
            RollbackRefusal.TARGET_NEVER_PUBLISHED,
            id="other-receipt",
        ),
        pytest.param({"expected_fence": 4}, {}, RollbackRefusal.FENCE_MOVED, id="fence-moved"),
    ],
)
def test_a_rollback_outside_its_preconditions_is_refused(
    request_changes: dict[str, Any], state_changes: dict[str, Any], code: RollbackRefusal
) -> None:
    with pytest.raises(RollbackRefusedError) as refused:
        rollback_pointer(_rollback(**request_changes), _rollback_state(**state_changes))
    assert refused.value.code is code


@pytest.mark.parametrize("reason", ["", "x" * 201, "line\nbreak"])
def test_an_operator_reason_outside_1_to_200_printable_characters_is_refused(reason: str) -> None:
    with pytest.raises(ValueError, match="REASON"):
        _rollback(reason=reason)


def test_a_200_character_reason_is_accepted() -> None:
    assert _rollback(reason="x" * 200).reason == "x" * 200


# --- The store at each commit boundary (failure injection, §6b) ------------------------------


def _table_store(table: ForecastTableFake) -> DynamoDbForecastStore:
    return DynamoDbForecastStore("forecast-table", 350_000, cast("Any", table))


def _series(shard: int, title: str = "P") -> tuple[GenerationSeries, ...]:
    return tuple(
        GenerationSeries(_candidate(n, shard), f"{title}{n}", title_truncated=False)
        for n in SHARDS[shard]
    )


def _meta(run_id: str = RUN, input_end: date = INPUT_END) -> GenerationMeta:
    attempts = tuple(
        ShardPublishResult(
            shard, "r0a0", SERIES, 2, f"runs/{run_id}/shards/{shard}/roster.jsonl.gz", HEX
        )
        for shard in range(len(SHARDS))
    )
    entries = [
        roster_entry(_candidate(n, shard), f"P{n}", title_truncated=False)
        for shard, numbers in enumerate(SHARDS)
        for n in numbers
    ]
    pages = cut_pages(rank_entries(entries, "units_ordered_p50_13w"), policy().serving)
    return GenerationMeta(
        set_id=SET_ID,
        run_id=run_id,
        receipt_sk=f"RUN#{run_id}#r0#commit",
        input_end=input_end,
        run_fingerprint=FINGERPRINT,
        run_identity=_manifest().run_identity,
        attempts=attempts,
        currency_codes=("USD",),
        counts_by_reason={},
        counts_by_model={"chronos2": 6},
        pages=pages,
    )


def _write_generation(store: DynamoDbForecastStore, meta: GenerationMeta) -> int:
    """Write every item the way the publisher does, META last; return the replays seen."""
    replayed = 0
    for shard in range(len(SHARDS)):
        replayed += store.put_generation_items(meta.generation_id, _series(shard)).replayed
    replayed += store.put_list_pages(meta.generation_id, meta.pages.pages).replayed
    replayed += store.put_generation_meta(meta).replayed
    assert store.attest_generation(meta) is Attestation.ATTESTED
    return replayed


def _publish_request(
    meta: GenerationMeta, expected_fence: int, at: datetime = AT
) -> OutcomeCommitRequest:
    attempt = _attempt(RunOutcome.PUBLISHED, None, disposition=Disposition.SUPPRESS)
    attempt = replace(attempt, run_id=meta.run_id, at=at)
    pointer = CurrentPointer(
        fence=expected_fence + 1,
        generation_id=meta.generation_id,
        input_end=meta.input_end,
        published_at=at,
        run_fingerprint=FINGERPRINT,
        last_attempt=attempt,
    )
    receipt = RunReceipt(
        sk=meta.receipt_sk,
        outcome=RunOutcome.PUBLISHED,
        recorded_at=at,
        generation_id=meta.generation_id,
        published_at=at,
        retry_disposition=Disposition.SUPPRESS,
        run_fingerprint=FINGERPRINT,
    )
    return OutcomeCommitRequest(SET_ID, expected_fence, receipt, pointer, meta.series_sha256)


def _held_request(expected_fence: int) -> OutcomeCommitRequest:
    attempt = _attempt(RunOutcome.HELD_QUALITY, "target_empty", disposition=Disposition.SUPPRESS)
    receipt = RunReceipt(
        sk=f"RUN#{RUN}#r0#commit",
        outcome=RunOutcome.HELD_QUALITY,
        recorded_at=AT,
        failure_category="target_empty",
        retry_disposition=Disposition.SUPPRESS,
    )
    pointer = CurrentPointer(fence=expected_fence, last_attempt=attempt)
    return OutcomeCommitRequest(SET_ID, expected_fence, receipt, pointer)


def _receipts(table: ForecastTableFake) -> dict[str, str]:
    return {
        sk: table.items[(pk, sk)]["outcome"]["S"]
        for pk, sk in table.keys(f"SET#{SET_ID}")
        if sk.startswith("RUN#")
    }


def test_a_crash_after_some_asin_items_then_a_rerun_publishes_once() -> None:
    table = ForecastTableFake(crash_on_put=2)
    store, meta = _table_store(table), _meta()
    with pytest.raises(LambdaCrashed):
        _write_generation(store, meta)
    assert store.get_current(SET_ID) is None
    table.crash_on_put = None
    assert _write_generation(store, meta) >= 1
    result = store.commit_outcome(_publish_request(meta, 0))
    assert result.disposition is CommitDisposition.APPLIED
    current = store.get_current(SET_ID)
    assert current is not None
    assert (current.generation_id, current.fence) == (meta.generation_id, 1)
    assert len(list(table.keys(f"GEN#{meta.generation_id}#ASIN#"))) == 6
    assert _receipts(table) == {meta.receipt_sk: "published"}


def test_a_crash_after_every_item_but_before_meta_leaves_nothing_current() -> None:
    meta = _meta()
    before_meta = 6 + len(meta.pages.pages) + 1
    table = ForecastTableFake(crash_on_put=before_meta)
    store = _table_store(table)
    with pytest.raises(LambdaCrashed):
        _write_generation(store, meta)
    assert table.item(f"GEN#{meta.generation_id}", "META") is None
    assert store.attest_generation(meta) is Attestation.META_MISSING
    table.crash_on_put = None
    assert _write_generation(store, meta) == 6 + len(meta.pages.pages)
    assert store.commit_outcome(_publish_request(meta, 0)).disposition is CommitDisposition.APPLIED


def test_a_stop_after_meta_before_the_transaction_republishes_on_a_rerun() -> None:
    table = ForecastTableFake()
    store, meta = _table_store(table), _meta()
    _write_generation(store, meta)
    assert store.get_current(SET_ID) is None
    assert _receipts(table) == {}
    assert _write_generation(store, meta) == 6 + len(meta.pages.pages) + 1
    assert store.commit_outcome(_publish_request(meta, 0)).disposition is CommitDisposition.APPLIED


def test_a_fence_expected_on_an_empty_item_writes_only_a_superseded_receipt() -> None:
    table = ForecastTableFake()
    store, meta = _table_store(table), _meta()
    _write_generation(store, meta)
    result = store.commit_outcome(_publish_request(meta, 3))
    assert result.disposition is CommitDisposition.SUPERSEDED
    assert result.recorded_outcome is RunOutcome.SUPERSEDED
    assert store.get_current(SET_ID) is None
    assert _receipts(table) == {meta.receipt_sk: "superseded"}
    # R1-10: the receipt names the attested generation that did not become CURRENT, never a
    # publish time, so only a published receipt can be a rollback target.
    receipt = cast("dict[str, Any]", table.item(f"SET#{SET_ID}", meta.receipt_sk))
    assert receipt["generation_id"] == {"S": meta.generation_id}
    assert "published_at" not in receipt


def test_a_publish_whose_fence_moved_on_an_existing_item_leaves_current_alone() -> None:
    table = ForecastTableFake()
    store, first, second = _table_store(table), _meta(), _meta("run-2")
    _write_generation(store, first)
    store.commit_outcome(_publish_request(first, 0))
    _write_generation(store, second)
    result = store.commit_outcome(_publish_request(second, 0))
    assert result.disposition is CommitDisposition.SUPERSEDED
    current = store.get_current(SET_ID)
    assert current is not None
    assert (current.generation_id, current.fence) == (first.generation_id, 1)
    assert _receipts(table) == {first.receipt_sk: "published", second.receipt_sk: "superseded"}


def test_a_publish_of_an_older_input_end_than_current_is_superseded() -> None:
    table = ForecastTableFake()
    store = _table_store(table)
    newer, older = _meta(), _meta("run-2", INPUT_END - timedelta(weeks=1))
    _write_generation(store, newer)
    store.commit_outcome(_publish_request(newer, 0))
    _write_generation(store, older)
    result = store.commit_outcome(_publish_request(older, 1))
    assert result.disposition is CommitDisposition.SUPERSEDED
    current = store.get_current(SET_ID)
    assert current is not None
    assert current.generation_id == newer.generation_id


def test_a_meta_that_no_longer_matches_cancels_the_whole_transaction() -> None:
    table = ForecastTableFake()
    store, meta = _table_store(table), _meta()
    _write_generation(store, meta)
    request = replace(_publish_request(meta, 0), series_sha256="c" * 64)
    with pytest.raises(ForecastStoreConflictError) as refused:
        store.commit_outcome(request)
    assert refused.value.key == f"GEN#{meta.generation_id}"
    assert store.get_current(SET_ID) is None
    assert _receipts(table) == {}


def test_a_commit_retried_after_an_unseen_success_writes_nothing_twice() -> None:
    table = ForecastTableFake(lose_transaction_response=True)
    store, meta = _table_store(table), _meta()
    _write_generation(store, meta)
    with pytest.raises(ReadTimeoutError):
        store.commit_outcome(_publish_request(meta, 0))
    within_window = store.commit_outcome(_publish_request(meta, 0))
    assert within_window.disposition is CommitDisposition.APPLIED
    table.forget_tokens()
    after_window = store.commit_outcome(_publish_request(meta, 0))
    assert after_window.disposition is CommitDisposition.ALREADY_CURRENT
    assert after_window.recorded_outcome is RunOutcome.PUBLISHED
    current = store.get_current(SET_ID)
    assert current is not None
    assert current.fence == 1
    assert _receipts(table) == {meta.receipt_sk: "published"}


def test_a_held_outcome_on_an_empty_table_creates_current_at_fence_zero_and_replays() -> None:
    table = ForecastTableFake()
    store = _table_store(table)
    assert store.commit_outcome(_held_request(0)).disposition is CommitDisposition.APPLIED
    current = store.get_current(SET_ID)
    assert current is not None
    assert (current.fence, current.generation_id) == (0, None)
    assert current.last_attempt == cast("CurrentPointer", _held_request(0).pointer).last_attempt
    table.forget_tokens()
    replayed = store.commit_outcome(
        replace(
            _held_request(0),
            receipt=replace(_held_request(0).receipt, recorded_at=AT + timedelta(minutes=11)),
        )
    )
    assert replayed.disposition is CommitDisposition.REPLAYED
    assert replayed.recorded_outcome is RunOutcome.HELD_QUALITY
    assert store.get_current(SET_ID) == current


def test_a_rollback_moves_the_fence_and_names_what_it_rolled_back_from() -> None:
    table = ForecastTableFake()
    store, first, second = _table_store(table), _meta(), _meta("run-2")
    _write_generation(store, first)
    store.commit_outcome(_publish_request(first, 0))
    _write_generation(store, second)
    store.commit_outcome(_publish_request(second, 1, AT + timedelta(weeks=1)))
    target = store.get_generation(first.generation_id)
    assert target is not None
    state = RollbackState(
        store.get_current(SET_ID),
        target,
        store.get_receipt(SET_ID, target.receipt_sk),
        "rollback-1",
        LATER,
    )
    plan = rollback_pointer(RollbackRequest(SET_ID, first.generation_id, 2, "bad model"), state)
    receipt = RunReceipt(
        sk=plan.receipt_sk,
        outcome=RunOutcome.ROLLED_BACK,
        recorded_at=LATER,
        generation_id=first.generation_id,
        retry_disposition=Disposition.SUPPRESS,
        operator_reason="bad model",
    )
    result = store.commit_outcome(OutcomeCommitRequest(SET_ID, 2, receipt, plan.pointer))
    assert result.disposition is CommitDisposition.APPLIED
    assert store.get_current(SET_ID) == plan.pointer
    assert _receipts(table)[plan.receipt_sk] == "rolled_back"


def test_a_rollback_whose_fence_moved_writes_no_pointer_change() -> None:
    table = ForecastTableFake()
    store, first = _table_store(table), _meta()
    _write_generation(store, first)
    store.commit_outcome(_publish_request(first, 0))
    pointer = replace(
        cast("CurrentPointer", store.get_current(SET_ID)),
        rolled_back_from="run-0-aaaaaaaaaaaa",
        last_attempt=_attempt(RunOutcome.ROLLED_BACK, None, disposition=Disposition.SUPPRESS),
    )
    receipt = RunReceipt(
        sk="RUN#rollback-1#r0#rollback",
        outcome=RunOutcome.ROLLED_BACK,
        recorded_at=LATER,
        generation_id=first.generation_id,
        retry_disposition=Disposition.SUPPRESS,
    )
    result = store.commit_outcome(OutcomeCommitRequest(SET_ID, 7, receipt, pointer))
    assert result.disposition is CommitDisposition.SUPERSEDED
    assert cast("CurrentPointer", store.get_current(SET_ID)).fence == 1


def test_a_receipt_only_outcome_is_a_one_action_transaction_that_leaves_current_alone() -> None:
    table = ForecastTableFake()
    store = _table_store(table)
    receipt = RunReceipt(
        sk=f"RUN#{RUN}#r0#select", outcome=RunOutcome.NO_OP_SAME_INPUT, recorded_at=AT
    )
    result = store.commit_outcome(OutcomeCommitRequest(SET_ID, 0, receipt))
    assert result.disposition is CommitDisposition.APPLIED
    assert len(table.transactions[-1]["TransactItems"]) == 1
    assert store.get_current(SET_ID) is None
    assert _receipts(table) == {receipt.sk: "no_op_same_input"}


def test_a_replayed_asin_item_with_other_bytes_is_a_conflict_not_an_overwrite() -> None:
    table = ForecastTableFake()
    store, meta = _table_store(table), _meta()
    store.put_generation_items(meta.generation_id, _series(0))
    with pytest.raises(ForecastStoreConflictError) as refused:
        store.put_generation_items(meta.generation_id, _series(0, title="Other"))
    assert refused.value.key.startswith(f"GEN#{meta.generation_id}#ASIN#")
    stored = table.item(refused.value.key, "ITEM")
    assert stored is not None
    assert stored["title"]["S"].startswith("P")


def test_a_throttle_past_the_client_retries_raises_the_typed_error() -> None:
    table = ForecastTableFake(throttle_puts=True)
    store, meta = _table_store(table), _meta()
    with pytest.raises(ForecastStoreThrottled) as put_refused:
        store.put_generation_items(meta.generation_id, _series(0))
    assert (put_refused.value.code, put_refused.value.retry_attempts) == (
        "ProvisionedThroughputExceededException",
        9,
    )
    table.throttle_transaction = "ThrottlingError"
    with pytest.raises(ForecastStoreThrottled) as commit_refused:
        store.commit_outcome(_held_request(0))
    assert commit_refused.value.code == "ThrottlingError"
    assert store.get_current(SET_ID) is None


def test_every_store_read_is_strongly_consistent_and_the_client_retries_adaptively() -> None:
    table = ForecastTableFake()
    store, meta = _table_store(table), _meta()
    _write_generation(store, meta)
    store.commit_outcome(_publish_request(meta, 0))
    store.get_receipt(SET_ID, meta.receipt_sk)
    store.get_generation(meta.generation_id)
    assert table.gets
    assert all(get["ConsistentRead"] is True for get in table.gets)
    assert cast("Any", STORE_CLIENT_CONFIG).retries == {"mode": "adaptive", "max_attempts": 10}


# --- The publisher handler: objects in, items and receipts out (§6c) -------------------------

MANIFEST_KEY = f"runs/{RUN}/input/r0a0/manifest.json"
PUBLISH_SET = forecast_set_id(_manifest().forecast_set)
LANDING_SECONDS = 3 * 24 * 3600
"""`AT` less the manifest's newest publication (`AT` - 3 days)."""


@dataclass
class ObjectsFake:
    """The processing bucket at the publisher's seam: missing keys and injected read errors."""

    objects: dict[str, bytes] = field(default_factory=dict[str, bytes])
    failures: dict[str, Exception] = field(default_factory=dict[str, Exception])
    reads: list[str] = field(default_factory=list[str])

    def get(self, key: str) -> bytes:
        self.reads.append(key)
        if key in self.failures:
            raise self.failures[key]
        if key not in self.objects:
            raise ObjectMissingError(key)
        return self.objects[key]

    def put_if_absent(self, key: str, body: bytes) -> bool:
        if key in self.objects:
            return False
        self.objects[key] = body
        return True


@dataclass
class MetricsFake:
    points: list[tuple[str, tuple[tuple[str, str], ...], float]] = field(
        default_factory=list[tuple[str, tuple[tuple[str, str], ...], float]]
    )

    def emit(self, points: Sequence[Datapoint], timestamp: datetime) -> None:
        assert timestamp.tzinfo is not None
        self.points.extend((p.spec.name, p.dimensions, p.value) for p in points)

    def outcomes(self) -> list[str]:
        return [dims[0][1] for name, dims, _ in self.points if name == "ForecastRunOutcome"]


@dataclass
class _Rig:
    objects: ObjectsFake = field(default_factory=ObjectsFake)
    table: ForecastTableFake = field(default_factory=ForecastTableFake)
    metrics: MetricsFake = field(default_factory=MetricsFake)

    @property
    def publisher(self) -> Publisher:
        return Publisher(
            policy=LoadedPolicy(policy(), HEX, {}),
            set_id=PUBLISH_SET,
            objects=self.objects,
            store=_table_store(self.table),
            metrics=self.metrics,
            now=lambda: AT,
        )

    def current(self) -> CurrentPointer | None:
        return _table_store(self.table).get_current(PUBLISH_SET)

    def receipts(self) -> dict[str, str]:
        return {
            sk: self.table.items[(pk, sk)]["outcome"]["S"]
            for pk, sk in self.table.keys(f"SET#{PUBLISH_SET}")
            if sk.startswith("RUN#")
        }


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _stage(rig: _Rig, run_id: str = RUN, expected_fence: int = 0, **records: Any) -> None:
    """Stage a 3-shard build attempt and each shard's r0a0 candidate as the job writes them.

    `fingerprint=` sets the run fingerprint the manifest and every run report carry.
    """
    fingerprint = str(records.pop("fingerprint", FINGERPRINT))
    base = f"runs/{run_id}/input/r0a0"
    titles = (_titles().model_dump_json() + "\n").encode("utf-8")
    manifest = _manifest(
        run_id=run_id,
        expected_fence=expected_fence,
        run_fingerprint=fingerprint,
        titles_key=f"{base}/titles.json",
        titles_sha256=_digest(titles),
    )
    rig.objects.objects[f"{base}/titles.json"] = titles
    rig.objects.objects[f"{base}/manifest.json"] = manifest.model_dump_json(by_alias=True).encode()
    for shard, numbers in enumerate(SHARDS):
        candidates = records.get(f"shard{shard}") or [_candidate(n, shard) for n in numbers]
        body = series_bytes(candidates)
        prefix = f"runs/{run_id}/shards/{shard}/attempts/r0a0/candidate/"
        rig.objects.objects[prefix + SERIES_FILE] = body
        report = _report(
            shard, run_id=run_id, series_sha256=_digest(body), run_fingerprint=fingerprint
        )
        rig.objects.objects[prefix + REPORT_FILE] = report.model_dump_json().encode()


def _restate_week_states(rig: _Rig, states: Mapping[int, dict[str, int]]) -> None:
    """Rewrite each staged shard's run report with its declared week-state counts."""
    for shard, counts in states.items():
        key = f"runs/{RUN}/shards/{shard}/attempts/r0a0/candidate/{REPORT_FILE}"
        report = RunReportV1.model_validate_json(rig.objects.objects[key])
        changed = report.model_copy(update={"week_state_counts": WeekStateCountsV1(**counts)})
        rig.objects.objects[key] = changed.model_dump_json().encode()


def _shard_publish(shard: int, run_id: str = RUN) -> PublishShardRequest:
    return PublishShardRequest(
        run_id=run_id,
        redrive_count=0,
        shard=shard,
        attempt_id="r0a0",
        manifest_key=f"runs/{run_id}/input/r0a0/manifest.json",
        attempts=tuple(ShardAttempt(k, "r0a0") for k in range(len(SHARDS))),
    )


def _publish_all(rig: _Rig, run_id: str = RUN) -> list[dict[str, object]]:
    return [publish_shard(rig.publisher, _shard_publish(k, run_id)).payload() for k in range(3)]


def _commit_run(
    rig: _Rig, payloads: list[dict[str, object]], run_id: str = RUN, **fields: object
) -> StepOutcome:
    """`fields` adds Commit payload fields, e.g. `operator_reason=`."""
    event: dict[str, object] = {
        "run_id": run_id,
        "redrive_count": 0,
        "manifest_key": f"runs/{run_id}/input/r0a0/manifest.json",
        "shard_results": payloads,
        **fields,
    }
    return commit_generation(rig.publisher, commit_request(event))


def test_a_three_shard_run_publishes_and_makes_its_generation_current() -> None:
    rig = _Rig()
    _stage(rig)
    payloads = _publish_all(rig)
    assert [p["outcome"] for p in payloads] == ["published"] * 3
    assert all(len(json.dumps(p)) < 400 for p in payloads)  # the ~300-byte result (R5-15)
    committed = _commit_run(rig, payloads)
    assert (committed.outcome, committed.receipt_sk) == ("published", f"RUN#{RUN}#r0#commit")
    current = rig.current()
    assert current is not None
    assert (current.generation_id, current.fence) == (committed.generation_id, 1)
    assert rig.receipts() == {f"RUN#{RUN}#r0#commit": "published"}
    receipt = rig.table.items[(f"SET#{PUBLISH_SET}", f"RUN#{RUN}#r0#commit")]
    assert receipt["landing_to_publish_seconds"] == {"N": str(LANDING_SECONDS)}
    meta = rig.table.items[(f"GEN#{committed.generation_id}", "META")]
    assert meta["item_count"] == {"N": "6"}
    assert rig.metrics.points == [
        ("ForecastRunOutcome", (("outcome", "published"),), 1.0),
        ("LandingToPublishSeconds", (), LANDING_SECONDS),
        ("InferredZeroWeekShare", (), 0.0),
    ]
    assert (committed.series_published, committed.series_withheld) == (24, 0)


def test_the_published_generation_emits_its_set_wide_inferred_zero_share() -> None:
    # 15 inferred-zero of 200 known weeks = 7.5 %; unknown weeks are outside the denominator.
    rig = _Rig()
    states = {
        0: {"observed": 90, "inferred_zero": 10, "unknown": 4},
        1: {"observed": 50, "inferred_zero": 0, "unknown": 0},
        2: {"observed": 45, "inferred_zero": 5, "unknown": 9},
    }
    _stage(rig)
    _restate_week_states(rig, states)
    payloads = _publish_all(rig)
    assert [(p["inferred_zero_weeks"], p["known_weeks"]) for p in payloads] == [
        (10, 100),
        (0, 50),
        (5, 50),
    ]
    assert _commit_run(rig, payloads).outcome == "published"
    assert ("InferredZeroWeekShare", (), 7.5) in rig.metrics.points


def test_a_superseded_commit_emits_no_inferred_zero_share() -> None:
    rig = _Rig()
    _stage(rig, expected_fence=5)
    committed = _commit_run(rig, _publish_all(rig))
    assert committed.outcome == "superseded"
    assert [name for name, _dims, _value in rig.metrics.points] == ["ForecastRunOutcome"]


def test_a_replayed_shard_publish_and_commit_change_nothing_and_count_once() -> None:
    rig = _Rig()
    _stage(rig)
    payloads = _publish_all(rig)
    first = _commit_run(rig, payloads)
    assert _publish_all(rig) == payloads
    again = _commit_run(rig, payloads)
    assert (again.outcome, again.generation_id) == ("published", first.generation_id)
    current = rig.current()
    assert current is not None
    assert current.fence == 1
    assert rig.metrics.outcomes() == ["published"]


@pytest.mark.covers("ADR-163")
def test_each_shard_asin_item_stores_the_same_list_row_as_its_roster_part_line() -> None:
    """The read API serves the item's row as stored; a second build could drift from the part."""
    rig = _Rig()
    _stage(rig)
    payloads = _publish_all(rig)
    deserializer = TypeDeserializer()
    stored = {
        key[0].rsplit("#", 1)[1]: json.loads(
            json.dumps(deserializer.deserialize(item["roster"]), default=int)
        )
        for key, item in rig.table.items.items()
        if "#ASIN#" in key[0]
    }
    lines = [
        json.loads(line)
        for shard in range(len(payloads))
        for line in _part_lines(rig, payloads, shard)
    ]
    assert len(stored) == len(lines) == 6
    assert {line["display_asin"]: line for line in lines} == stored
    assert all(line["rank"] is None for line in lines)


def _with_part(rig: _Rig, key: object, body: bytes) -> None:
    rig.objects.objects[str(key)] = body


def _part_lines(rig: _Rig, payloads: list[dict[str, object]], shard: int) -> list[bytes]:
    body = rig.objects.objects[str(payloads[shard]["roster_part_key"])]
    return gzip.decompress(body).splitlines(keepends=True)


def test_a_missing_roster_part_holds_the_commit_naming_its_shard_and_nothing_is_current() -> None:
    rig = _Rig()
    _stage(rig)
    payloads = _publish_all(rig)
    del rig.objects.objects[str(payloads[1]["roster_part_key"])]
    held = _commit_run(rig, payloads)
    assert (held.outcome, held.step, held.category, held.shard) == (
        "held_quality",
        "commit",
        "identity_mismatch",
        1,
    )
    # Design §6c and R1-5: the hold names the shard and the missing key, for the operator.
    assert held.held_detail == (
        f"shard 1 roster part runs/{RUN}/shards/1/attempts/r0a0/roster-part.jsonl.gz is missing"
    )
    assert held.payload()["shard"] == 1
    assert rig.current() is None
    assert rig.metrics.points == []


def test_a_roster_part_with_one_changed_byte_holds_the_commit_run() -> None:
    rig = _Rig()
    _stage(rig)
    payloads = _publish_all(rig)
    lines = _part_lines(rig, payloads, 2)
    lines[0] = lines[0].replace(b'"title":"P9"', b'"title":"P8"')
    _with_part(rig, payloads[2]["roster_part_key"], gzip.compress(b"".join(lines), mtime=0))
    assert _commit_run(rig, payloads).category == "identity_mismatch"
    assert rig.current() is None


def test_a_roster_part_one_line_short_holds_the_commit_even_when_its_digest_matches() -> None:
    rig = _Rig()
    _stage(rig)
    payloads = _publish_all(rig)
    short = gzip.compress(b"".join(_part_lines(rig, payloads, 0)[:1]), mtime=0)
    _with_part(rig, payloads[0]["roster_part_key"], short)
    payloads[0] = {**payloads[0], "roster_part_sha256": _digest(short)}
    held = _commit_run(rig, payloads)
    assert (held.outcome, held.category) == ("held_quality", "identity_mismatch")
    assert rig.current() is None


def test_a_replayed_shard_publish_whose_part_exists_with_other_bytes_is_held() -> None:
    rig = _Rig()
    _stage(rig)
    key = f"runs/{RUN}/shards/1/attempts/r0a0/roster-part.jsonl.gz"
    rig.objects.objects[key] = gzip.compress(b"{}\n", mtime=0)
    held = publish_shard(rig.publisher, _shard_publish(1))
    assert held.payload() == {
        "outcome": "held_quality",
        "step": "s1-r0a0",
        "category": "identity_mismatch",
        "shard": 1,
        "attempt_id": "r0a0",
    }


def test_a_commit_missing_one_shard_is_held_before_any_part_is_read() -> None:
    rig = _Rig()
    _stage(rig)
    payloads = _publish_all(rig)
    rig.objects.reads.clear()
    held = _commit_run(rig, payloads[:2])
    assert (held.outcome, held.category) == ("held_quality", "identity_mismatch")
    assert not any(key.endswith("roster-part.jsonl.gz") for key in rig.objects.reads)


def test_a_held_shard_passes_through_the_commit_without_a_read() -> None:
    rig = _Rig()
    _stage(rig)
    payloads = _publish_all(rig)
    payloads[2] = {
        "outcome": "held_quality",
        "step": "s2-r0a0",
        "category": "gate_mismatch",
        "shard": 2,
        "attempt_id": "r0a0",
    }
    rig.objects.reads.clear()
    held = _commit_run(rig, payloads)
    assert (held.step, held.category) == ("s2-r0a0", "gate_mismatch")
    assert rig.objects.reads == []


def test_a_candidate_the_gate_would_not_choose_is_held_before_any_item_is_written() -> None:
    rig = _Rig()
    worse = _with_target(5, 1, evaluations=_evaluations(chronos_wql="0.9000"))
    _stage(rig, shard1=[worse, _candidate(7, 1)])
    held = publish_shard(rig.publisher, _shard_publish(1))
    assert (held.outcome, held.category) == ("held_quality", "gate_mismatch")
    assert rig.table.puts == []


def test_a_candidate_from_another_build_attempt_is_held_identity_mismatch() -> None:
    rig = _Rig()
    _stage(rig)
    prefix = f"runs/{RUN}/shards/0/attempts/r0a0/candidate/"
    body = rig.objects.objects[prefix + SERIES_FILE]
    other = _report(0, input_attempt_id="r1a0", series_sha256=_digest(body))
    rig.objects.objects[prefix + REPORT_FILE] = other.model_dump_json().encode()
    assert publish_shard(rig.publisher, _shard_publish(0)).category == "identity_mismatch"


def test_a_missing_candidate_is_held_identity_mismatch() -> None:
    rig = _Rig()
    _stage(rig)
    del rig.objects.objects[f"runs/{RUN}/shards/2/attempts/r0a0/candidate/{SERIES_FILE}"]
    assert publish_shard(rig.publisher, _shard_publish(2)).category == "identity_mismatch"


@pytest.mark.parametrize("code", ["SlowDown", "InternalError"])
def test_a_transient_part_read_error_is_raised_then_recorded_failed_with_retry(code: str) -> None:
    rig = _Rig()
    _stage(rig)
    payloads = _publish_all(rig)
    key = str(payloads[1]["roster_part_key"])
    rig.objects.failures[key] = s3_error(key, ClientError({"Error": {"Code": code}}, "GetObject"))
    with pytest.raises(ObjectStoreError, match=code):
        _commit_run(rig, payloads)
    cause = json.dumps({"errorType": "ObjectStoreError", "errorMessage": code})
    event = {
        "run_id": RUN,
        "redrive_count": 0,
        "step": "commit",
        "outcome": "failed",
        "phase": "publish",
        "error": {"Error": "ObjectStoreError", "Cause": cause},
        "run_fingerprint": FINGERPRINT,
        "expected_fence": 0,
    }
    recorded = record_outcome(rig.publisher, record_request(event))
    assert (recorded.outcome, recorded.category) == ("failed", f"publish:{code}")
    current = rig.current()
    assert current is not None
    assert current.generation_id is None
    assert current.last_attempt is not None
    assert current.last_attempt.retry_disposition is Disposition.RETRY
    assert rig.metrics.outcomes() == ["failed"]


def test_an_s3_failure_keeps_its_code_and_only_no_such_key_is_a_missing_object() -> None:
    def client_error(code: str) -> ClientError:
        return ClientError({"Error": {"Code": code}}, "GetObject")

    assert isinstance(s3_error("k", client_error("NoSuchKey")), ObjectMissingError)
    assert str(s3_error("k", client_error("AccessDenied"))) == "AccessDenied"
    timeout = s3_error("k", ReadTimeoutError(endpoint_url="https://s3"))
    assert (type(timeout), str(timeout)) == (ObjectStoreError, "ReadTimeoutError")


def _caught(name: str, message: str | None = None, exit_code: str | None = None) -> dict[str, str]:
    cause: dict[str, object] = {}
    if message is not None:
        cause = {"errorType": name, "errorMessage": message}
    if exit_code is not None:
        cause = {"ExitMessage": json.dumps({"code": exit_code, "cause": [], "run_id": None})}
    return {"Error": name, "Cause": json.dumps(cause)}


@pytest.mark.parametrize(
    "case",
    [
        (
            "build-r0a0",
            "input_build",
            _caught("InputBuildFailedError", "MARKETPLACE_UNKNOWN"),
            "input_build:MARKETPLACE_UNKNOWN",
            Disposition.SUPPRESS,
        ),
        ("s1-r0a0", "job", _caught("States.Timeout"), "job:States.Timeout", Disposition.RETRY),
        (
            "s1-r0a0",
            "job",
            _caught("SageMaker.ProcessingJobFailed", exit_code="MODEL_ARTIFACT_MISMATCH"),
            "job:MODEL_ARTIFACT_MISMATCH",
            Disposition.SUPPRESS,
        ),
        (
            "commit",
            "publish",
            _caught("ForecastStoreThrottled", "ThrottlingException after 9 client retries"),
            "publish:ForecastStoreThrottled",
            Disposition.RETRY,
        ),
        (
            "commit",
            "publish",
            {"Error": "not a code", "Cause": "not json"},
            f"publish:{ERROR_UNREADABLE}",
            Disposition.RETRY,
        ),
    ],
    ids=["coded-deterministic", "timeout", "job-exit-code", "throttle", "unreadable"],
)
def test_every_caught_failure_records_failed_with_its_category_and_emits_failed(
    case: tuple[str, str, dict[str, str], str, Disposition],
) -> None:
    step, phase, error, category, disposition = case
    assert failure_category(phase, error) == category
    rig = _Rig()
    request = RecordOutcomeRequest(RUN, 0, step, RunOutcome.FAILED, category, FINGERPRINT)
    assert record_outcome(rig.publisher, request).outcome == "failed"
    current = rig.current()
    assert current is not None
    assert current.last_attempt is not None
    assert (current.fence, current.last_attempt.failure_category) == (0, category)
    assert current.last_attempt.retry_disposition is disposition
    assert rig.receipts() == {f"RUN#{RUN}#r0#{step}": "failed"}
    assert rig.metrics.outcomes() == ["failed"]


def test_a_no_op_writes_only_its_receipt_and_leaves_current_alone() -> None:
    rig = _Rig()
    request = RecordOutcomeRequest(RUN, 0, "select", RunOutcome.NO_OP_SAME_INPUT, None, FINGERPRINT)
    assert record_outcome(rig.publisher, request).outcome == "no_op_same_input"
    assert rig.current() is None
    assert rig.receipts() == {f"RUN#{RUN}#r0#select": "no_op_same_input"}
    assert rig.metrics.outcomes() == ["no_op_same_input"]


def test_a_commit_whose_fence_moved_writes_only_a_superseded_receipt() -> None:
    rig = _Rig()
    _stage(rig, run_id="run-2")
    _stage(rig, run_id="run-3")
    _commit_run(rig, _publish_all(rig, "run-2"), "run-2")
    late = _commit_run(rig, _publish_all(rig, "run-3"), "run-3")
    assert (late.outcome, late.generation_id) == ("superseded", None)
    current = rig.current()
    assert current is not None
    assert current.generation_id is not None
    assert current.generation_id.startswith("run-2-")
    assert rig.receipts()["RUN#run-3#r0#commit"] == "superseded"
    assert rig.metrics.outcomes() == ["published", "superseded"]


# --- Dev loop, criterion 8: an operator RETRY_FAILED run's REASON is on its commit receipt ----


def test_an_operator_reason_is_recorded_on_the_published_commit_receipt() -> None:
    # Design §6d step 3: a RETRY_FAILED start's receipt records the reason. The dev run
    # 471f4b9397d2-2026-11-18-op-5 published with no reason on its commit receipt.
    rig = _Rig()
    _stage(rig)
    committed = _commit_run(rig, _publish_all(rig), operator_reason="retry after the failed job")
    assert committed.outcome == "published"
    receipt = rig.table.items[(f"SET#{PUBLISH_SET}", f"RUN#{RUN}#r0#commit")]
    assert receipt["operator_reason"] == {"S": "retry after the failed job"}


def test_an_operator_reason_stays_on_a_superseded_commit_receipt() -> None:
    rig = _Rig()
    _stage(rig, run_id="run-2")
    _stage(rig, run_id="run-3")
    _commit_run(rig, _publish_all(rig, "run-2"), "run-2")
    late = _commit_run(rig, _publish_all(rig, "run-3"), "run-3", operator_reason="operator retry")
    assert late.outcome == "superseded"
    receipt = rig.table.items[(f"SET#{PUBLISH_SET}", "RUN#run-3#r0#commit")]
    assert receipt["operator_reason"] == {"S": "operator retry"}


def test_a_commit_without_an_operator_reason_stores_none() -> None:
    rig = _Rig()
    _stage(rig)
    assert _commit_run(rig, _publish_all(rig)).outcome == "published"
    assert "operator_reason" not in rig.table.items[(f"SET#{PUBLISH_SET}", f"RUN#{RUN}#r0#commit")]


@pytest.mark.parametrize("reason", ["", "x" * 201, "line\nbreak"], ids=["empty", "201", "control"])
def test_a_commit_with_an_invalid_operator_reason_is_refused_before_any_write(reason: str) -> None:
    rig = _Rig()
    _stage(rig)
    payloads = _publish_all(rig)
    with pytest.raises(PublishFailedError, match=f"^{REQUEST_INVALID}$"):
        _commit_run(rig, payloads, operator_reason=reason)
    assert rig.current() is None
    assert rig.receipts() == {}


def test_a_rollback_restores_the_earlier_generation_and_counts_rolled_back() -> None:
    rig = _Rig()
    _stage(rig, run_id="run-2")
    _stage(rig, run_id="run-3", expected_fence=1)
    first = _commit_run(rig, _publish_all(rig, "run-2"), "run-2")
    _commit_run(rig, _publish_all(rig, "run-3"), "run-3")
    assert first.generation_id is not None
    request = RollbackRequest(PUBLISH_SET, first.generation_id, 2, "bad model")
    done = rollback(rig.publisher, request, "rollback-1")
    assert (done.outcome, done.generation_id) == ("rolled_back", first.generation_id)
    current = rig.current()
    assert current is not None
    assert (current.generation_id, current.fence) == (first.generation_id, 3)
    assert rig.metrics.outcomes() == ["published", "published", "rolled_back"]


# --- R1 round 1: what a failure, a hold and a rollback leave in last_attempt ----------------

BAD_FINGERPRINT = "b" * 64
"""The run fingerprint of the model an operator rolled back or a check held."""


def _rolled_back(rig: _Rig) -> CurrentPointer:
    """run-2 publishes, run-3 publishes a bad model, and the operator rolls back to run-2."""
    _stage(rig, run_id="run-2")
    _stage(rig, run_id="run-3", expected_fence=1, fingerprint=BAD_FINGERPRINT)
    first = _commit_run(rig, _publish_all(rig, "run-2"), "run-2")
    _commit_run(rig, _publish_all(rig, "run-3"), "run-3")
    assert first.generation_id is not None
    request = RollbackRequest(PUBLISH_SET, first.generation_id, 2, "bad model")
    assert rollback(rig.publisher, request, "rollback-1").outcome == "rolled_back"
    current = rig.current()
    assert current is not None
    return current


def _failure(run_id: str, step: str, **changes: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "run_id": run_id,
        "redrive_count": 0,
        "step": step,
        "outcome": "failed",
        "phase": "input_build",
        "error": _caught("InputBuildFailedError", "ATHENA_TIMEOUT"),
        "run_fingerprint": None,
        "expected_fence": None,
    }
    return {**event, **changes}


def _slot_check(current: CurrentPointer | None, fingerprint: str) -> SelectDecision:
    retry = policy().retry
    request = SelectDecisionRequest(
        fingerprint, current, Trigger.SCHEDULED, SLOT_DAY, retry.admission_weekday
    )
    return select_decision(request)


SLOT_DAY = date(2026, 11, 19)
"""A Thursday, the contract's admission weekday."""


def test_a_select_failure_after_a_rollback_keeps_the_rolled_back_model_suppressed() -> None:
    rig = _Rig()
    before = _rolled_back(rig)
    recorded = record_outcome(rig.publisher, record_request(_failure("run-4", "select")))
    assert (recorded.outcome, recorded.category) == ("failed", "input_build:ATHENA_TIMEOUT")
    # Ruling 2026-11-16 R1-1: a failure with no fingerprint writes only its receipt.
    assert rig.current() == before
    assert rig.receipts()["RUN#run-4#r0#select"] == "failed"
    assert rig.metrics.outcomes()[-1] == "failed"
    decision = _slot_check(rig.current(), BAD_FINGERPRINT)
    assert (decision.outcome, decision.basis) == (
        SelectOutcome.NO_OP_SAME_INPUT,
        SelectBasis.LAST_ATTEMPT_SUPPRESSED,
    )


def test_a_select_failure_after_a_hold_keeps_the_held_fingerprint_suppressed() -> None:
    rig = _Rig()
    _stage(rig, run_id="run-2")
    _commit_run(rig, _publish_all(rig, "run-2"), "run-2")
    _stage(rig, run_id="run-3", expected_fence=1, fingerprint=BAD_FINGERPRINT)
    payloads = _publish_all(rig, "run-3")
    del rig.objects.objects[str(payloads[2]["roster_part_key"])]
    held = _commit_run(rig, payloads, "run-3")
    event = {
        "run_id": "run-3",
        "redrive_count": 0,
        "step": held.step,
        "outcome": held.outcome,
        "category": held.category,
        "shard": held.shard,
        "run_fingerprint": BAD_FINGERPRINT,
        "expected_fence": 1,
    }
    assert record_outcome(rig.publisher, record_request(event)).shard == 2
    receipt = rig.table.items[(f"SET#{PUBLISH_SET}", "RUN#run-3#r0#commit")]
    assert (receipt["outcome"], receipt["shard"]) == ({"S": "held_quality"}, {"N": "2"})
    before = rig.current()
    record_outcome(rig.publisher, record_request(_failure("run-4", "select")))
    assert rig.current() == before
    decision = _slot_check(rig.current(), BAD_FINGERPRINT)
    assert (decision.outcome, decision.basis) == (
        SelectOutcome.NO_OP_SAME_INPUT,
        SelectBasis.LAST_ATTEMPT_SUPPRESSED,
    )


def test_a_fingerprinted_failure_after_a_rollback_still_records_last_attempt() -> None:
    rig = _Rig()
    _rolled_back(rig)
    other = "c" * 64
    event = _failure("run-4", "build-r0a0", run_fingerprint=other, expected_fence=3)
    assert record_outcome(rig.publisher, record_request(event)).outcome == "failed"
    current = rig.current()
    assert current is not None
    attempt = current.last_attempt
    assert attempt is not None
    assert (attempt.run_fingerprint, attempt.outcome, attempt.retry_disposition) == (
        other,
        RunOutcome.FAILED,
        Disposition.RETRY,
    )


def test_a_probe_failure_staged_before_a_rollback_writes_only_a_superseded_receipt() -> None:
    rig = _Rig()
    before = _rolled_back(rig)
    event = _failure("scale-1", "s0-r0a0", phase="job", run_fingerprint="d" * 64, expected_fence=1)
    event["error"] = _caught("States.Timeout")
    assert record_outcome(rig.publisher, record_request(event)).outcome == "superseded"
    assert rig.current() == before
    assert rig.receipts()["RUN#scale-1#r0#s0-r0a0"] == "superseded"


def test_two_outcomes_prepared_from_one_current_cannot_both_replace_last_attempt() -> None:
    table = ForecastTableFake()
    store = _table_store(table)
    store.commit_outcome(_held_request(0))
    read = cast("CurrentPointer", store.get_current(SET_ID))

    def failed(run_id: str) -> OutcomeCommitRequest:
        attempt = _attempt(RunOutcome.FAILED, "publish:SlowDown", run_id=run_id)
        receipt = RunReceipt(
            sk=f"RUN#{run_id}#r0#commit",
            outcome=RunOutcome.FAILED,
            recorded_at=AT,
            failure_category="publish:SlowDown",
            retry_disposition=Disposition.RETRY,
            run_fingerprint=FINGERPRINT,
        )
        pointer = replace(read, last_attempt=attempt)
        return OutcomeCommitRequest(SET_ID, 0, receipt, pointer, previous_attempt=read.last_attempt)

    first, late = failed("run-5"), failed("run-6")
    assert store.commit_outcome(first).disposition is CommitDisposition.APPLIED
    assert store.commit_outcome(late).disposition is CommitDisposition.SUPERSEDED
    assert cast("CurrentPointer", store.get_current(SET_ID)).last_attempt == (
        cast("CurrentPointer", first.pointer).last_attempt
    )
    assert _receipts(table)["RUN#run-6#r0#commit"] == "superseded"


def test_two_rollbacks_under_one_receipt_key_never_report_the_others_generation() -> None:
    table = ForecastTableFake()
    store, first, second = _table_store(table), _meta(), _meta("run-2")
    _write_generation(store, first)
    store.commit_outcome(_publish_request(first, 0))
    _write_generation(store, second)
    store.commit_outcome(_publish_request(second, 1, AT + timedelta(weeks=1)))

    def rollback_to(target: GenerationMeta, fence: int) -> OutcomeCommitRequest:
        record = store.get_generation(target.generation_id)
        assert record is not None
        state = RollbackState(
            store.get_current(SET_ID),
            record,
            store.get_receipt(SET_ID, record.receipt_sk),
            "rollback-1",
            LATER,
        )
        plan = rollback_pointer(RollbackRequest(SET_ID, target.generation_id, fence, "x"), state)
        receipt = RunReceipt(
            sk=plan.receipt_sk,
            outcome=RunOutcome.ROLLED_BACK,
            recorded_at=LATER,
            generation_id=target.generation_id,
            retry_disposition=Disposition.SUPPRESS,
            operator_reason="x",
        )
        return OutcomeCommitRequest(SET_ID, fence, receipt, plan.pointer)

    applied = store.commit_outcome(rollback_to(first, 2))
    assert (applied.disposition, applied.recorded_generation_id) == (
        CommitDisposition.APPLIED,
        first.generation_id,
    )
    # A second rollback in the same second, to the generation now rolled back from.
    with pytest.raises(ForecastStoreConflictError, match="another generation"):
        store.commit_outcome(rollback_to(second, 3))
    assert cast("CurrentPointer", store.get_current(SET_ID)).generation_id == first.generation_id


def test_a_publish_after_a_rollback_clears_rolled_back_from() -> None:
    rig = _Rig()
    assert _rolled_back(rig).rolled_back_from is not None
    _stage(rig, run_id="run-4", expected_fence=3, fingerprint="c" * 64)
    published = _commit_run(rig, _publish_all(rig, "run-4"), "run-4")
    assert published.outcome == "published"
    current = rig.current()
    assert current is not None
    # Ruling 2026-11-16 R1-8: the field names only the rollback that made CURRENT what it is.
    assert (current.generation_id, current.rolled_back_from) == (published.generation_id, None)
    assert "rolled_back_from" not in rig.table.items[(f"SET#{PUBLISH_SET}", "CURRENT")]


# --- R1 round 2: a late attempt's own receipt, strict replays, select receipts ------------------

LATE_ATTEMPTS = (ShardAttempt(0, "r0a1"), ShardAttempt(1, "r0a0"), ShardAttempt(2, "r0a0"))
"""Shard 0's late attempt r0a1 beside the published r0a0 attempts of shards 1 and 2."""


def _stage_late_attempt(rig: _Rig) -> None:
    """Write shard 0's complete r0a1 candidate, as the operator's dev job does (acceptance 5)."""
    source = f"runs/{RUN}/shards/0/attempts/r0a0/candidate/"
    target = f"runs/{RUN}/shards/0/attempts/r0a1/candidate/"
    rig.objects.objects[target + SERIES_FILE] = rig.objects.objects[source + SERIES_FILE]
    report = RunReportV1.model_validate_json(rig.objects.objects[source + REPORT_FILE])
    late = report.model_copy(update={"attempt_id": "r0a1"})
    rig.objects.objects[target + REPORT_FILE] = late.model_dump_json().encode()


def _publish_late(rig: _Rig) -> list[dict[str, object]]:
    return [
        publish_shard(
            rig.publisher,
            replace(_shard_publish(a.shard), attempt_id=a.attempt_id, attempts=LATE_ATTEMPTS),
        ).payload()
        for a in LATE_ATTEMPTS
    ]


def test_a_late_attempt_committed_after_its_pass_published_is_superseded_under_its_own_key() -> (
    None
):
    """Slice-2 acceptance 5 as ruled (OQ-S2-4): a late attempt never reads as published."""
    rig = _Rig()
    _stage(rig)
    first = _commit_run(rig, _publish_all(rig))
    assert first.outcome == "published"
    before = rig.current()
    _stage_late_attempt(rig)
    late = _commit_run(rig, _publish_late(rig))
    assert (late.outcome, late.receipt_sk, late.generation_id) == (
        "superseded",
        f"RUN#{RUN}#r0#s0-r0a1",
        None,
    )
    assert rig.current() == before
    assert rig.receipts() == {
        f"RUN#{RUN}#r0#commit": "published",
        f"RUN#{RUN}#r0#s0-r0a1": "superseded",
    }
    published = rig.table.items[(f"SET#{PUBLISH_SET}", f"RUN#{RUN}#r0#commit")]
    assert published["generation_id"] == {"S": first.generation_id}
    receipt = rig.table.items[(f"SET#{PUBLISH_SET}", f"RUN#{RUN}#r0#s0-r0a1")]
    late_generation = receipt["generation_id"]["S"]
    assert late_generation != first.generation_id
    assert late_generation.startswith(f"{RUN}-")
    meta = rig.table.items[(f"GEN#{late_generation}", "META")]
    assert meta["receipt_sk"] == {"S": f"RUN#{RUN}#r0#s0-r0a1"}
    assert rig.metrics.outcomes() == ["published", "superseded"]
    # The same late commit again keeps its own key and writes no other receipt.
    receipts = rig.receipts()
    again = _commit_run(rig, _publish_late(rig))
    assert (again.outcome, again.receipt_sk) == ("superseded", f"RUN#{RUN}#r0#s0-r0a1")
    assert (rig.current(), rig.receipts()) == (before, receipts)


def test_a_second_commit_after_a_commit_receipt_with_no_generation_holds_and_writes_nothing() -> (
    None
):
    rig = _Rig()
    _stage(rig)
    held = RecordOutcomeRequest(RUN, 0, "commit", RunOutcome.HELD_QUALITY, "target_empty")
    assert record_outcome(rig.publisher, held).outcome == "held_quality"
    before = (rig.current(), rig.receipts())
    late = _commit_run(rig, _publish_all(rig))
    assert (late.outcome, late.category) == ("held_quality", "identity_mismatch")
    assert late.held_detail == (
        f"RUN#{RUN}#r0#commit records held_quality for no readable attempt set of this pass"
    )
    assert (rig.current(), rig.receipts()) == before


def test_a_receipt_key_holding_another_outcome_is_a_conflict_not_a_replay() -> None:
    store = _table_store(ForecastTableFake())
    held = _held_request(0)
    assert store.commit_outcome(held).disposition is CommitDisposition.APPLIED
    no_op = RunReceipt(
        sk=held.receipt.sk,
        outcome=RunOutcome.NO_OP_SAME_INPUT,
        recorded_at=AT,
        run_fingerprint=FINGERPRINT,
    )
    with pytest.raises(
        ForecastStoreConflictError, match=r"records held_quality, not no_op_same_input$"
    ):
        store.commit_outcome(OutcomeCommitRequest(SET_ID, 0, no_op))
    # Paired control: the held outcome retried later (another request, one token) replays.
    retried = replace(held, receipt=replace(held.receipt, recorded_at=AT + timedelta(minutes=1)))
    replayed = store.commit_outcome(retried)
    assert (replayed.disposition, replayed.recorded_outcome) == (
        CommitDisposition.REPLAYED,
        RunOutcome.HELD_QUALITY,
    )


def _select_no_op(**changes: Any) -> dict[str, Any]:
    """`RecordOutcome`'s input after `Decide` passes a select no-op on (R1-12)."""
    event: dict[str, Any] = {
        "run_id": RUN,
        "redrive_count": 0,
        "step": "select",
        "outcome": "no_op_same_input",
        "run_identity": _manifest().run_identity.model_dump(mode="json"),
        "run_fingerprint": FINGERPRINT,
        "expected_fence": 0,
    }
    return event | changes


def test_a_select_no_op_receipt_records_the_input_fingerprint_select_read() -> None:
    rig = _Rig()
    assert record_outcome(rig.publisher, record_request(_select_no_op())).outcome == (
        "no_op_same_input"
    )
    receipt = rig.table.items[(f"SET#{PUBLISH_SET}", f"RUN#{RUN}#r0#select")]
    identity = receipt["run_identity"]["M"]
    assert identity["input_fingerprint"] == {"S": HEX}
    assert identity["max_runtime_seconds"] == {"N": "3600"}
    assert receipt["run_fingerprint"] == {"S": FINGERPRINT}


@pytest.mark.parametrize(
    "changes",
    [
        {"step": "commit", "outcome": "held_quality", "category": "target_empty"},
        {"step": "select", "outcome": "failed", "category": "input_build:ATHENA_TIMEOUT"},
        {"run_identity": {"input_fingerprint": HEX}},
    ],
    ids=["not-select", "select-failure", "malformed"],
)
def test_a_run_identity_outside_a_select_no_op_or_hold_is_refused(changes: dict[str, Any]) -> None:
    with pytest.raises(PublishFailedError, match=f"^{REQUEST_INVALID}$"):
        record_request(_select_no_op(**changes))


# --- R1F: one replay rule for every receipt write (R1-14, R1-15) -------------------------------

SELECT_SK = f"RUN#{RUN}#r0#select"
COMMIT_SK = f"RUN#{RUN}#r0#commit"


def _select_failure(**changes: Any) -> RunReceipt:
    """Return a `SelectInput` failure's receipt: receipt-only, no fingerprint (ruling R1-1)."""
    receipt = RunReceipt(
        sk=SELECT_SK,
        outcome=RunOutcome.FAILED,
        recorded_at=AT,
        failure_category="input_build:ATHENA_TIMEOUT",
    )
    return replace(receipt, **changes)


def _operator_no_op(**changes: Any) -> RunReceipt:
    """Return an operator start's no-op receipt: fingerprint, run identity, reason (R1-12)."""
    receipt = RunReceipt(
        sk=SELECT_SK,
        outcome=RunOutcome.NO_OP_SAME_INPUT,
        recorded_at=AT,
        operator_reason="restated sales",
        run_fingerprint=FINGERPRINT,
        run_identity=_manifest().run_identity,
    )
    return replace(receipt, **changes)


def _commit_hold(**changes: Any) -> OutcomeCommitRequest:
    """Return a commit-level hold naming its shard and attempt, prepared from an empty table."""
    request = _held_request(0)
    receipt = replace(request.receipt, **({"attempt_id": "r0a0", "shard": 1} | changes))
    return replace(request, receipt=receipt)


OTHER_IDENTITY = _manifest().run_identity.model_copy(update={"input_fingerprint": "e" * 64})


@pytest.mark.parametrize(
    ("first", "second", "field_name"),
    [
        pytest.param(
            _select_failure(),
            _select_failure(failure_category="input_build:MARKETPLACE_UNKNOWN"),
            "failure_category",
            id="failure-category",
        ),
        pytest.param(
            _operator_no_op(),
            _operator_no_op(run_identity=OTHER_IDENTITY),
            "run_identity",
            id="run-identity",
        ),
        pytest.param(
            _operator_no_op(),
            _operator_no_op(run_fingerprint="e" * 64),
            "run_fingerprint",
            id="run-fingerprint",
        ),
        pytest.param(
            _operator_no_op(),
            _operator_no_op(operator_reason="another reason"),
            "operator_reason",
            id="operator-reason",
        ),
    ],
)
def test_a_receipt_only_write_with_another_request_identity_under_its_key_is_a_conflict(
    first: RunReceipt, second: RunReceipt, field_name: str
) -> None:
    """R1-14: the second request's facts must never be reported over the first's receipt."""
    table = ForecastTableFake()
    store = _table_store(table)
    assert store.commit_outcome(OutcomeCommitRequest(SET_ID, 0, first)).disposition is (
        CommitDisposition.APPLIED
    )
    stored = dict(table.items[(f"SET#{SET_ID}", SELECT_SK)])
    with pytest.raises(
        ForecastStoreConflictError,
        match=f"^{SELECT_SK}: the receipt records another {field_name}$",
    ):
        store.commit_outcome(OutcomeCommitRequest(SET_ID, 0, second))
    assert table.items[(f"SET#{SET_ID}", SELECT_SK)] == stored


@pytest.mark.parametrize(
    ("changes", "field_name"),
    [({"attempt_id": "r0a1"}, "attempt_id"), ({"shard": 2}, "shard")],
    ids=["attempt-id", "shard"],
)
def test_a_pointer_write_with_another_request_identity_under_its_key_is_a_conflict(
    changes: dict[str, Any], field_name: str
) -> None:
    """R1-14 on the pointer path: `last_attempt` moved, so the store derives `superseded`.

    The key holds another hold's receipt, which is no replay of this one.
    """
    table = ForecastTableFake()
    store = _table_store(table)
    first = _commit_hold()
    assert store.commit_outcome(first).disposition is CommitDisposition.APPLIED
    before = (dict(table.items[(f"SET#{SET_ID}", COMMIT_SK)]), store.get_current(SET_ID))
    with pytest.raises(
        ForecastStoreConflictError, match=f"^{COMMIT_SK}: the receipt records another {field_name}$"
    ):
        store.commit_outcome(_commit_hold(**changes))
    assert (dict(table.items[(f"SET#{SET_ID}", COMMIT_SK)]), store.get_current(SET_ID)) == before


def test_a_recorded_outcome_never_reports_a_category_its_receipt_does_not_hold() -> None:
    """gpt-5.6-sol round 3 #1: a second request under one key read back as a replay."""
    rig = _Rig()
    first = _failure(RUN, "select")
    assert record_outcome(rig.publisher, record_request(first)).category == (
        "input_build:ATHENA_TIMEOUT"
    )
    # Paired control: the same failure recorded again is its own replay and counts once.
    rig.table.forget_tokens()
    again = record_outcome(rig.publisher, record_request(first))
    assert (again.outcome, again.category) == ("failed", "input_build:ATHENA_TIMEOUT")
    other = _failure(RUN, "select", error=_caught("InputBuildFailedError", "MARKETPLACE_UNKNOWN"))
    with pytest.raises(PublishHeld, match=r"the receipt records another failure_category$"):
        record_outcome(rig.publisher, record_request(other))
    receipt = rig.table.items[(f"SET#{PUBLISH_SET}", SELECT_SK)]
    assert receipt["failure_category"] == {"S": "input_build:ATHENA_TIMEOUT"}
    assert rig.metrics.outcomes() == ["failed"]


@dataclass
class _MetricsLost(MetricsFake):
    """`PutMetricData` fails once, after the commit applied (opus-5.5 round 3 F1)."""

    lose_next: bool = True

    def emit(self, points: Sequence[Datapoint], timestamp: datetime) -> None:
        if self.lose_next:
            self.lose_next = False
            raise MetricEmitError("ThrottlingException")
        super().emit(points, timestamp)


def _commit_catch(run_id: str, expected_fence: int) -> dict[str, Any]:
    """`RecordOutcome`'s input from the `Commit` Catch after `MetricEmitError`."""
    cause = json.dumps({"errorType": "MetricEmitError", "errorMessage": "ThrottlingException"})
    return {
        "run_id": run_id,
        "redrive_count": 0,
        "step": "commit",
        "outcome": "failed",
        "phase": "publish",
        "error": {"Error": "MetricEmitError", "Cause": cause},
        "run_fingerprint": FINGERPRINT,
        "expected_fence": expected_fence,
    }


def test_a_failure_after_the_pass_published_reports_the_published_commit() -> None:
    """R1-15: the commit applied and its metric was lost; the run's outcome is `published`."""
    rig = _Rig(metrics=_MetricsLost())
    _stage(rig)
    with pytest.raises(MetricEmitError, match=r"^ThrottlingException$"):
        _commit_run(rig, _publish_all(rig))
    generation = rig.table.items[(f"SET#{PUBLISH_SET}", COMMIT_SK)]["generation_id"]["S"]
    before = (rig.current(), rig.receipts())
    recorded = record_outcome(rig.publisher, record_request(_commit_catch(RUN, 0)))
    assert (
        recorded.outcome,
        recorded.receipt_sk,
        recorded.category,
        recorded.generation_id,
    ) == ("published", COMMIT_SK, None, generation)
    assert (rig.current(), rig.receipts()) == before
    assert before[1] == {COMMIT_SK: "published"}
    # The lost datapoint stays lost: a replay emits nothing, so nothing counts twice.
    assert rig.metrics.outcomes() == []


def test_a_failure_after_the_pass_was_superseded_reports_the_superseded_commit() -> None:
    rig = _Rig()
    _stage(rig, run_id="run-2")
    _stage(rig, run_id="run-3")
    _commit_run(rig, _publish_all(rig, "run-2"), "run-2")
    rig.metrics = _MetricsLost()
    with pytest.raises(MetricEmitError, match=r"^ThrottlingException$"):
        _commit_run(rig, _publish_all(rig, "run-3"), "run-3")
    before = (rig.current(), rig.receipts())
    recorded = record_outcome(rig.publisher, record_request(_commit_catch("run-3", 0)))
    assert (recorded.outcome, recorded.receipt_sk, recorded.category) == (
        "superseded",
        "RUN#run-3#r0#commit",
        None,
    )
    assert (rig.current(), rig.receipts()) == before
    assert before[1]["RUN#run-3#r0#commit"] == "superseded"


def test_a_hold_recorded_under_a_commit_key_holding_the_pass_publish_is_still_a_conflict() -> None:
    """Paired control for R1-15: only a `publish:` failure after the commit reads its outcome.

    A later attempt set's hold under that key never reads as `published` (R1-10, R1-16).
    """
    rig = _Rig()
    _stage(rig)
    assert _commit_run(rig, _publish_all(rig)).outcome == "published"
    before = (rig.current(), rig.receipts())
    event = {
        "run_id": RUN,
        "redrive_count": 0,
        "step": "commit",
        "outcome": "held_quality",
        "category": "identity_mismatch",
        "shard": 0,
        "run_fingerprint": FINGERPRINT,
        "expected_fence": 0,
    }
    with pytest.raises(PublishHeld, match=r"the receipt records published, not superseded$"):
        record_outcome(rig.publisher, record_request(event))
    assert (rig.current(), rig.receipts()) == before


@pytest.mark.parametrize(
    "later",
    [timedelta(0), timedelta(minutes=1)],
    ids=["after-the-token-window", "same-token-later-request"],
)
def test_a_true_replay_is_accepted_and_reports_every_field_on_record(later: timedelta) -> None:
    """R1-14 (c): the rule compares what the store reads back.

    So a true replay must read every request-bound field back equal, run identity included.
    """
    table = ForecastTableFake()
    store = _table_store(table)
    receipt = _operator_no_op()
    assert store.commit_outcome(OutcomeCommitRequest(SET_ID, 0, receipt)).disposition is (
        CommitDisposition.APPLIED
    )
    if not later:
        table.forget_tokens()
    retry = replace(receipt, recorded_at=AT + later)
    replayed = store.commit_outcome(OutcomeCommitRequest(SET_ID, 0, retry))
    assert (replayed.disposition, replayed.recorded_outcome) == (
        CommitDisposition.REPLAYED,
        RunOutcome.NO_OP_SAME_INPUT,
    )
    assert replayed.recorded == ReceiptRecord(
        sk=SELECT_SK,
        outcome=RunOutcome.NO_OP_SAME_INPUT,
        generation_id=None,
        published_at=None,
        run_fingerprint=FINGERPRINT,
        run_identity=_manifest().run_identity,
        operator_reason="restated sales",
    )
    assert _receipts(table) == {SELECT_SK: "no_op_same_input"}


def test_the_store_and_the_publisher_derive_one_superseded_receipt_for_one_request() -> None:
    """A retry finding the fence moved replays the store's first derivation, never conflicts.

    Both paths call one `superseded_receipt()`.
    """
    table = ForecastTableFake()
    store = _table_store(table)
    assert store.commit_outcome(_held_request(0)).disposition is CommitDisposition.APPLIED
    attempt = _attempt(RunOutcome.FAILED, "publish:SlowDown", run_id="run-5")
    receipt = RunReceipt(
        sk="RUN#run-5#r0#commit",
        outcome=RunOutcome.FAILED,
        recorded_at=AT,
        failure_category="publish:SlowDown",
        retry_disposition=Disposition.RETRY,
        run_fingerprint=FINGERPRINT,
    )
    prepared = OutcomeCommitRequest(
        SET_ID, 0, receipt, CurrentPointer(fence=0, last_attempt=attempt)
    )
    assert store.commit_outcome(prepared).disposition is CommitDisposition.SUPERSEDED
    retry = replace(superseded_receipt(receipt), recorded_at=AT + timedelta(minutes=1))
    replayed = store.commit_outcome(OutcomeCommitRequest(SET_ID, 0, retry))
    assert replayed.disposition is CommitDisposition.REPLAYED
    assert replayed.recorded is not None
    assert (replayed.recorded.outcome, replayed.recorded.failure_category) == (
        RunOutcome.SUPERSEDED,
        "publish:SlowDown",
    )
