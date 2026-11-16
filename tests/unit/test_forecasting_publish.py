# SPDX-License-Identifier: MIT-0
"""The publish rules (design §6b, §6c): identity, binding, receipts, pointer and rollback.

A lax check here publishes a forecast from the wrong attempt or input, one the gate would not
have chosen, or a title from another product; a wrong pointer rule lets an older or superseded
run replace what planners see, or a rollback land on a generation that was never published.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast

import pytest
from botocore.exceptions import ReadTimeoutError
from forecast_series_support import INPUT_END, asin, policy
from forecast_store_support import ForecastTableFake, LambdaCrashed

from adapters.outbound.dynamodb.demand_forecast_store import (
    STORE_CLIENT_CONFIG,
    DynamoDbForecastStore,
)
from domain.forecasting.boundary import (
    CandidateSeriesV1,
    InputManifestV1,
    RosterEntryV1,
    RunReportV1,
    TitleMapV1,
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
    generation_id,
    next_pointer,
    receipt_sk,
    retry_disposition,
    rollback_pointer,
    validate_commit,
    validate_shard_candidate,
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
    assert "generation_id" not in cast(
        "dict[str, Any]", table.item(f"SET#{SET_ID}", meta.receipt_sk)
    )


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
