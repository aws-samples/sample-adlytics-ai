# SPDX-License-Identifier: MIT-0
"""What may be published, under which identity, and how the set's pointer moves (§6b, §6c).

The publisher Lambda reads objects and writes items; every decision it takes is here, pure:

- `generation_id()`: one generation per run and set of bound shard attempts, so a redriven
  publish targets the same items and a late attempt can never write into another generation.
- `validate_shard_candidate()` and `validate_commit()`: the attempt, schema, content, gate and
  title-map checks. Each returns the `HeldQuality` that holds the run, or None.
- `next_pointer()` and `rollback_pointer()`: the fenced `CURRENT` transitions the store commits
  in one transaction. A fence that moved means another run or a rollback won (`superseded`).
- `retry_disposition()`: whether the next daily check re-runs a fingerprint (P-17).
- `receipt_sk()`: one immutable receipt key per execution pass and step (R3-18).

The adapter maps an unparseable object to `HeldQuality(SCHEMA_MISMATCH)`; the checks here start
from parsed boundary models and the digests the publisher computed over the bytes it read.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from domain.forecasting.boundary import (
    CandidateSeriesV1,
    InputManifestV1,
    RosterEntryV1,
    RunReportV1,
    ShardV1,
    TargetCandidateV1,
    TitleMapV1,
    boundary_schema_digests,
)
from domain.forecasting.checks import first_negative_week, first_unordered_week
from domain.forecasting.gate import GateRequest, choose_model
from domain.forecasting.policy import MAX_SHARDS, MONEY_TARGETS, SUNDAY, TARGETS, RetryPolicy

GENERATION_DIGEST_CHARS = 12
"""Hex characters of the attempt-list digest in a `generation_id` (§6b)."""
RECENT_WEEKS = 13
OPERATOR_REASON_MAX_CHARS = 200
"""Operator `REASON` length: input validation of a free-text field (§6c, §6d)."""
RETRY_EXHAUSTED = "retry_exhausted"
"""The category a fingerprint's failure takes once its transient retries are spent (§6d)."""
_ATTEMPT = re.compile(r"r[0-9]+a[0-9]+")
_STEP = re.compile(
    r"select|commit|rollback|build-(?P<build>r[0-9]+a[0-9]+)|s(?P<shard>[0-9]+)-r[0-9]+a[0-9]+"
)
_SATURDAY = (SUNDAY - 1) % 7


class HeldCheck(StrEnum):
    """The check that holds a run `held_quality(<check>)` (§6b, §6c)."""

    IDENTITY_MISMATCH = "identity_mismatch"
    SCHEMA_MISMATCH = "schema_mismatch"
    GATE_MISMATCH = "gate_mismatch"
    TITLE_MAP_MISMATCH = "title_map_mismatch"
    TARGET_EMPTY = "target_empty"
    ITEM_TOO_LARGE = "item_too_large"
    WEEK_GRID_MISMATCH = "week_grid_mismatch"
    """A record's recent weeks don't end at `input_end`, or `input_end` is not a Saturday."""
    FORECAST_BAND_UNORDERED = "forecast_band_unordered"
    FORECAST_NEGATIVE = "forecast_negative"
    CURRENCY_MISSING = "currency_missing"
    """A money target is shown without a currency."""


@dataclass(frozen=True)
class HeldQuality:
    """Why a run is held, naming the shard or record that failed."""

    check: HeldCheck
    detail: str

    @property
    def category(self) -> str:
        """The receipt's `failure_category`."""
        return self.check.value


def _run_id(run_id: str) -> None:
    if not run_id or "#" in run_id:
        raise ValueError(f"a run_id is non-empty and holds no '#' (the key separator): {run_id!r}")


def _attempt_id(attempt_id: str) -> None:
    if _ATTEMPT.fullmatch(attempt_id) is None:
        raise ValueError(f"an attempt_id is r<redrive>a<retry>, got {attempt_id!r}")


def _shard_index(shard: int) -> None:
    if isinstance(shard, bool) or not 0 <= shard < MAX_SHARDS:
        raise ValueError(f"a shard index is 0 to {MAX_SHARDS - 1}, got {shard!r}")


# --- Generation identity --------------------------------------------------------------------


@dataclass(frozen=True)
class ShardAttempt:
    """One shard's bound attempt, from the `Forecast` map's output."""

    shard: int
    attempt_id: str

    def __post_init__(self) -> None:
        _shard_index(self.shard)
        _attempt_id(self.attempt_id)


def generation_id(run_id: str, attempts: Sequence[ShardAttempt]) -> str:
    """Return `<run_id>-<first 12 hex of SHA-256 of the sorted (shard, attempt_id) list>`."""
    _run_id(run_id)
    if not attempts:
        raise ValueError("a generation binds at least one shard attempt")
    if len({attempt.shard for attempt in attempts}) != len(attempts):
        raise ValueError("a generation binds one attempt per shard")
    pairs = sorted([attempt.shard, attempt.attempt_id] for attempt in attempts)
    digest = hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"{run_id}-{digest[:GENERATION_DIGEST_CHARS]}"


@dataclass(frozen=True)
class ShardPublishResult:
    """What `publish_shard` returns for one shard (about 300 bytes, R5-15)."""

    shard: int
    attempt_id: str
    series_sha256: str
    item_count: int
    roster_part_key: str
    roster_part_sha256: str

    def __post_init__(self) -> None:
        _shard_index(self.shard)
        _attempt_id(self.attempt_id)
        if self.item_count < 1:
            raise ValueError("a published shard holds at least one record")


def generation_series_sha256(results: Sequence[ShardPublishResult]) -> str:
    """`META.series_sha256`: SHA-256 of the shard digests in shard order."""
    ordered = sorted(results, key=lambda result: result.shard)
    joined = "".join(result.series_sha256 for result in ordered)
    return hashlib.sha256(joined.encode("ascii")).hexdigest()


# --- Shard validation (publish_shard) -------------------------------------------------------


@dataclass(frozen=True)
class ShardCandidateValidationRequest:
    """One shard attempt's candidate as the publisher read it, and the publish request."""

    run_id: str
    shard: int
    attempt_id: str
    manifest: InputManifestV1
    report: RunReportV1
    records: tuple[CandidateSeriesV1, ...]
    series_sha256: str
    """The SHA-256 the publisher computed over the `series.jsonl.gz` bytes it read."""
    titles: TitleMapV1
    margin: float
    """`gate.margin`, for the gate re-run."""
    publisher_schema_digests: Mapping[str, str] = field(default_factory=boundary_schema_digests)

    def __post_init__(self) -> None:
        _run_id(self.run_id)
        _shard_index(self.shard)
        _attempt_id(self.attempt_id)


def _schemas(
    request: ShardCandidateValidationRequest | CommitValidationRequest,
) -> HeldQuality | None:
    own = dict(request.publisher_schema_digests)
    recorded = {"manifest": request.manifest.boundary_schema_digests}
    recorded["run identity"] = request.manifest.run_identity.boundary_schema_digests
    if isinstance(request, ShardCandidateValidationRequest):
        recorded["run report"] = request.report.boundary_schema_digests
    for source, digests in recorded.items():
        if dict(digests) != own:
            return HeldQuality(HeldCheck.SCHEMA_MISMATCH, f"{source} schema digests differ")
    return None


def _shard_spec(manifest: InputManifestV1, shard: int) -> ShardV1 | None:
    return next((spec for spec in manifest.shards if spec.index == shard), None)


def _attempt_binding(request: ShardCandidateValidationRequest, spec: ShardV1) -> str | None:
    manifest, report = request.manifest, request.report
    pairs = (
        ("manifest run_id", manifest.run_id, request.run_id),
        ("run report run_id", report.run_id, request.run_id),
        ("run report shard_index", report.shard_index, request.shard),
        ("run report attempt_id", report.attempt_id, request.attempt_id),
        ("run report input_attempt_id", report.input_attempt_id, manifest.build_attempt_id),
        ("run report run_fingerprint", report.run_fingerprint, manifest.run_fingerprint),
        ("series_sha256", request.series_sha256, report.series_sha256),
        ("run report asin_count", report.asin_count, spec.asin_count),
        ("record count", len(request.records), spec.asin_count),
    )
    for name, found, expected in pairs:
        if found != expected:
            return f"shard {request.shard}: {name} {found!r} differs from {expected!r}"
    return None


def _record_binding(request: ShardCandidateValidationRequest, spec: ShardV1) -> str | None:
    seen: set[str] = set()
    for record in request.records:
        key = record.canonical_asin
        if key in seen:
            return f"shard {request.shard}: {key} appears twice"
        seen.add(key)
        if record.shard_index != request.shard:
            return f"shard {request.shard}: {key} carries shard_index {record.shard_index}"
        if not spec.first_canonical_asin <= key <= spec.last_canonical_asin:
            return f"shard {request.shard}: {key} lies outside the shard's bounds"
    return None


def _recent_grid(input_end: date) -> tuple[date, ...]:
    last = input_end - timedelta(days=6)
    return tuple(last - timedelta(weeks=RECENT_WEEKS - 1 - n) for n in range(RECENT_WEEKS))


def _record_content(
    record: CandidateSeriesV1, grid: tuple[date, ...], margin: float
) -> HeldQuality | None:
    key = record.canonical_asin
    if tuple(week.week_start for week in record.recent_weeks) != grid:
        return HeldQuality(HeldCheck.WEEK_GRID_MISMATCH, f"{key}: recent weeks off the grid")
    for target in TARGETS:
        held = _target_content(record, target, margin)
        if held is not None:
            return held
    return None


def _gate_mismatch(candidate: TargetCandidateV1, where: str, margin: float) -> HeldQuality | None:
    """Re-run the gate on the recorded evaluations and compare its decision (R4-08)."""
    try:
        decision = choose_model(GateRequest(candidate.evaluations, margin))
    except ValueError as error:
        return HeldQuality(HeldCheck.GATE_MISMATCH, f"{where}: {error}")
    recorded = (candidate.chosen_model, candidate.withheld_reason, candidate.gate_reason)
    if (decision.chosen_model, decision.withheld_reason, decision.gate_reason) != recorded:
        return HeldQuality(HeldCheck.GATE_MISMATCH, f"{where}: gate re-run differs")
    return None


def _target_content(record: CandidateSeriesV1, target: str, margin: float) -> HeldQuality | None:
    """Check the gate, then §4d steps 2 and 3 on the stored strings, then the currency."""
    where = f"{record.canonical_asin} {target}"
    candidate = record.targets[target]  # pyright: ignore[reportArgumentType]
    held = _gate_mismatch(candidate, where, margin)
    if held is not None:
        return held
    bands = candidate.chosen_forecast
    if bands is None:
        return None
    p10, p50, p90 = ([Decimal(v) for v in band] for band in (bands.p10, bands.p50, bands.p90))
    if first_unordered_week(p10, p50, p90) is not None:
        return HeldQuality(HeldCheck.FORECAST_BAND_UNORDERED, where)
    if first_negative_week(p10, p50, p90) is not None:
        return HeldQuality(HeldCheck.FORECAST_NEGATIVE, where)
    if target in MONEY_TARGETS and record.currency_code is None:
        return HeldQuality(HeldCheck.CURRENCY_MISSING, where)
    return None


def _content(request: ShardCandidateValidationRequest) -> HeldQuality | None:
    input_end = request.manifest.input_end
    if input_end.weekday() != _SATURDAY:
        return HeldQuality(HeldCheck.WEEK_GRID_MISMATCH, f"input_end {input_end} is no Saturday")
    grid = _recent_grid(input_end)
    for record in request.records:
        held = _record_content(record, grid, request.margin)
        if held is not None:
            return held
    return None


def _shard_titles(request: ShardCandidateValidationRequest, spec: ShardV1) -> HeldQuality | None:
    """Each candidate once in the map with its display ASIN; each map entry in bounds has one."""
    entries = request.titles.entries
    keys = [entry.canonical_asin for entry in entries]
    for record in request.records:
        at = bisect.bisect_left(keys, record.canonical_asin)
        if at == len(keys) or keys[at] != record.canonical_asin:
            detail = f"{record.canonical_asin} has no title entry"
            return HeldQuality(HeldCheck.TITLE_MAP_MISMATCH, detail)
        if entries[at].display_asin != record.display_asin:
            detail = f"{record.canonical_asin}: title entry's display ASIN differs"
            return HeldQuality(HeldCheck.TITLE_MAP_MISMATCH, detail)
    start = bisect.bisect_left(keys, spec.first_canonical_asin)
    end = bisect.bisect_right(keys, spec.last_canonical_asin)
    if end - start != len(request.records):
        detail = (
            f"shard {request.shard}: {end - start} title entries in bounds, "
            f"{len(request.records)} candidates"
        )
        return HeldQuality(HeldCheck.TITLE_MAP_MISMATCH, detail)
    return None


def validate_shard_candidate(request: ShardCandidateValidationRequest) -> HeldQuality | None:
    """Apply the §6c shard checks in order: schemas, attempt binding, content, titles."""
    held = _schemas(request)
    if held is not None:
        return held
    spec = _shard_spec(request.manifest, request.shard)
    if spec is None:
        detail = f"shard {request.shard} is not in the manifest"
        return HeldQuality(HeldCheck.IDENTITY_MISMATCH, detail)
    mismatch = _attempt_binding(request, spec) or _record_binding(request, spec)
    if mismatch is not None:
        return HeldQuality(HeldCheck.IDENTITY_MISMATCH, mismatch)
    return _content(request) or _shard_titles(request, spec)


# --- Commit validation (commit_generation) --------------------------------------------------


@dataclass(frozen=True)
class RosterPart:
    """One shard's roster part as the commit read it: the SHA-256 of its bytes and its lines."""

    shard: int
    sha256: str
    entries: tuple[RosterEntryV1, ...]


@dataclass(frozen=True)
class CommitValidationRequest:
    """Every shard's publish result, the parts they name, and the title map's entry count."""

    manifest: InputManifestV1
    shard_results: tuple[ShardPublishResult, ...]
    parts: tuple[RosterPart, ...]
    title_entry_count: int
    publisher_schema_digests: Mapping[str, str] = field(default_factory=boundary_schema_digests)


@dataclass(frozen=True)
class CommitValidation:
    """The hold, or None, and the generation's counts by withheld reason and by model shown.

    Keys are `<target>.<SkipReason>` and `<target>.<model>`; every value counts series.
    """

    held: HeldQuality | None
    counts_by_reason: dict[str, int]
    counts_by_model: dict[str, int]


def _shard_coverage(request: CommitValidationRequest) -> str | None:
    expected = {spec.index: spec.asin_count for spec in request.manifest.shards}
    results = [result.shard for result in request.shard_results]
    if sorted(results) != sorted(expected) or len(set(results)) != len(results):
        return f"shard results cover {sorted(results)}, the manifest names {sorted(expected)}"
    parts = {part.shard: part for part in request.parts}
    if len(parts) != len(request.parts) or set(parts) != set(expected):
        return f"roster parts cover {sorted(parts)}, the manifest names {sorted(expected)}"
    for result in request.shard_results:
        part = parts[result.shard]
        if result.item_count != expected[result.shard]:
            return f"shard {result.shard}: item_count {result.item_count} differs from the plan"
        if part.sha256 != result.roster_part_sha256:
            return f"shard {result.shard}: roster part digest differs from its result"
        if len(part.entries) != result.item_count:
            return f"shard {result.shard}: roster part has {len(part.entries)} lines"
    return None


def _counts(request: CommitValidationRequest) -> CommitValidation:
    by_reason: Counter[str] = Counter()
    by_model: Counter[str] = Counter()
    displays: set[str] = set()
    for part in request.parts:
        for entry in part.entries:
            if entry.rank is not None:
                detail = f"shard {part.shard}: {entry.display_asin} carries a rank"
                return CommitValidation(HeldQuality(HeldCheck.SCHEMA_MISMATCH, detail), {}, {})
            if entry.display_asin in displays:
                detail = f"{entry.display_asin} appears in two roster lines"
                return CommitValidation(HeldQuality(HeldCheck.IDENTITY_MISMATCH, detail), {}, {})
            displays.add(entry.display_asin)
            for target, row in entry.targets.items():
                if row.chosen_model is not None:
                    by_model[f"{target}.{row.chosen_model.value}"] += 1
                elif row.withheld_reason is not None:
                    by_reason[f"{target}.{row.withheld_reason.value}"] += 1
    return CommitValidation(None, dict(sorted(by_reason.items())), dict(sorted(by_model.items())))


def validate_commit(request: CommitValidationRequest) -> CommitValidation:
    """Apply the §6c run-level checks: schemas, shard coverage, parts, title count, targets."""
    held = _schemas(request)
    if held is not None:
        return CommitValidation(held, {}, {})
    mismatch = _shard_coverage(request)
    if mismatch is not None:
        return CommitValidation(HeldQuality(HeldCheck.IDENTITY_MISMATCH, mismatch), {}, {})
    counted = _counts(request)
    if counted.held is not None:
        return counted
    roster = sum(spec.asin_count for spec in request.manifest.shards)
    if request.title_entry_count != roster:
        detail = f"title map has {request.title_entry_count} entries, the roster {roster}"
        return CommitValidation(HeldQuality(HeldCheck.TITLE_MAP_MISMATCH, detail), {}, {})
    shown = {key.split(".", 1)[0] for key in counted.counts_by_model}
    empty = [target for target in TARGETS if target not in shown]
    if empty:
        detail = f"no shown series for {', '.join(empty)}"
        return CommitValidation(HeldQuality(HeldCheck.TARGET_EMPTY, detail), {}, {})
    return counted


# --- Outcomes, retry disposition and receipts -----------------------------------------------


class RunOutcome(StrEnum):
    """A receipt's outcome (§6b)."""

    PUBLISHED = "published"
    NO_OP_SAME_INPUT = "no_op_same_input"
    HELD_INPUT_NOT_READY = "held_input_not_ready"
    HELD_QUALITY = "held_quality"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    ROLLED_BACK = "rolled_back"


POINTER_OUTCOMES = frozenset(
    {RunOutcome.PUBLISHED, RunOutcome.HELD_QUALITY, RunOutcome.FAILED, RunOutcome.ROLLED_BACK}
)
"""The outcomes that change `CURRENT` or `last_attempt`; every other one is receipt-only."""


class Disposition(StrEnum):
    """Whether the next daily check re-runs `last_attempt`'s fingerprint (§6d step 3)."""

    SUPPRESS = "suppress"
    RETRY = "retry"


@dataclass(frozen=True)
class LastAttempt:
    """`CURRENT.last_attempt`: the newest pointer-changing outcome and its disposition."""

    run_id: str
    attempt_id: str | None
    run_fingerprint: str | None
    outcome: RunOutcome
    failure_category: str | None
    retry_disposition: Disposition
    transient_retries: int
    at: datetime

    def __post_init__(self) -> None:
        _run_id(self.run_id)
        if self.outcome not in POINTER_OUTCOMES:
            raise ValueError(f"last_attempt never records a receipt-only {self.outcome}")
        if self.transient_retries < 0:
            raise ValueError("transient_retries is never negative")
        if self.at.tzinfo is None:
            raise ValueError("last_attempt.at is timezone-aware")


class ReceiptOnlyOutcomeError(ValueError):
    """`retry_disposition` was asked about an outcome that never touches `last_attempt`."""


@dataclass(frozen=True)
class OutcomeRecord:
    """One outcome to record, the `last_attempt` it replaces, and the retry policy."""

    outcome: RunOutcome
    failure_category: str | None
    run_fingerprint: str | None
    previous: LastAttempt | None
    policy: RetryPolicy

    def __post_init__(self) -> None:
        categorised = self.outcome in {RunOutcome.HELD_QUALITY, RunOutcome.FAILED}
        if categorised and not self.failure_category:
            raise ValueError(f"{self.outcome} needs its failure_category")
        if self.outcome in {RunOutcome.PUBLISHED, RunOutcome.ROLLED_BACK} and (
            self.failure_category is not None
        ):
            raise ValueError(f"{self.outcome} has no failure_category")


@dataclass(frozen=True)
class RetryDecision:
    """The disposition, category and transient-retry count `last_attempt` records."""

    disposition: Disposition
    failure_category: str | None
    transient_retries: int


def _suppressed(record: OutcomeRecord) -> bool:
    listed = record.policy.suppressed_outcomes
    if record.outcome.value in listed:
        return True
    return f"{record.outcome.value}({record.failure_category})" in listed


def _retries_used(record: OutcomeRecord) -> int:
    """Transient re-runs this fingerprint has had: one more than the previous transient failure."""
    previous = record.previous
    if (
        previous is None
        or record.run_fingerprint is None
        or previous.run_fingerprint != record.run_fingerprint
        or previous.outcome is not RunOutcome.FAILED
    ):
        return 0
    transient = previous.retry_disposition is Disposition.RETRY
    if not transient and previous.failure_category != RETRY_EXHAUSTED:
        return 0
    return previous.transient_retries + 1


def retry_disposition(record: OutcomeRecord) -> RetryDecision:
    """Return `last_attempt`'s disposition; refuse a receipt-only outcome (R3-02).

    `published` and the policy's deterministic outcomes suppress. Any other failure retries
    until `max_transient_retries` re-runs of the same fingerprint have failed; the next failure
    suppresses with category `retry_exhausted`.
    """
    if record.outcome not in POINTER_OUTCOMES:
        raise ReceiptOnlyOutcomeError(f"{record.outcome} is receipt-only")
    if record.outcome is RunOutcome.PUBLISHED or _suppressed(record):
        return RetryDecision(Disposition.SUPPRESS, record.failure_category, 0)
    used = _retries_used(record)
    if used >= record.policy.max_transient_retries:
        return RetryDecision(Disposition.SUPPRESS, RETRY_EXHAUSTED, used)
    return RetryDecision(Disposition.RETRY, record.failure_category, used)


@dataclass(frozen=True)
class ReceiptKeyRequest:
    """One receipt's execution, pass and step.

    The step is `select`, `build-<attempt>`, `s<k>-<attempt>`, `commit`, or `rollback` for an
    operator rollback.
    """

    run_id: str
    redrive_count: int
    step: str

    def __post_init__(self) -> None:
        _run_id(self.run_id)
        if isinstance(self.redrive_count, bool) or self.redrive_count < 0:
            raise ValueError(f"redrive_count is a whole number from 0, got {self.redrive_count!r}")
        match = _STEP.fullmatch(self.step)
        if match is None:
            raise ValueError(f"unknown receipt step {self.step!r}")
        if match["shard"] is not None:
            _shard_index(int(match["shard"]))


def receipt_sk(request: ReceiptKeyRequest) -> str:
    """Return `RUN#<run_id>#r<RedriveCount>#<step>` (R3-18)."""
    return f"RUN#{request.run_id}#r{request.redrive_count}#{request.step}"


# --- The CURRENT pointer --------------------------------------------------------------------


@dataclass(frozen=True)
class CurrentPointer:
    """The set's `CURRENT` item; generation fields are absent until the first publish."""

    fence: int
    generation_id: str | None = None
    input_end: date | None = None
    published_at: datetime | None = None
    run_fingerprint: str | None = None
    rolled_back_from: str | None = None
    last_attempt: LastAttempt | None = None

    def __post_init__(self) -> None:
        if isinstance(self.fence, bool) or self.fence < 0:
            raise ValueError(f"fence is a whole number from 0, got {self.fence!r}")
        generation = (self.generation_id, self.input_end, self.published_at, self.run_fingerprint)
        if any(value is None for value in generation) and any(v is not None for v in generation):
            raise ValueError("CURRENT's generation fields are all set or all absent")


@dataclass(frozen=True)
class PublishedGeneration:
    """The attested generation a `published` outcome makes CURRENT."""

    generation_id: str
    input_end: date
    run_fingerprint: str
    published_at: datetime


@dataclass(frozen=True)
class PointerAdvanceRequest:
    """`CURRENT` as read, the run's `expected_fence`, its new `last_attempt` and generation."""

    current: CurrentPointer | None
    expected_fence: int
    attempt: LastAttempt
    generation: PublishedGeneration | None = None

    def __post_init__(self) -> None:
        if self.expected_fence < 0:
            raise ValueError("expected_fence is never negative")
        if self.attempt.outcome is RunOutcome.ROLLED_BACK:
            raise ValueError("a rollback moves the pointer through rollback_pointer()")
        if (self.generation is not None) != (self.attempt.outcome is RunOutcome.PUBLISHED):
            raise ValueError("a generation is given exactly for a published outcome")


class PointerDecision(StrEnum):
    """What the outcome transaction does (§6b)."""

    ADVANCE = "advance"
    """Write `CURRENT` as returned, with the outcome's receipt."""
    SUPERSEDED = "superseded"
    """The fence moved: write only a `superseded` receipt."""
    ALREADY_CURRENT = "already_current"
    """A retry after an unseen success: record nothing more, return `published`."""


@dataclass(frozen=True)
class PointerAdvance:
    """The decision and, for `ADVANCE`, the `CURRENT` state after the transaction."""

    decision: PointerDecision
    pointer: CurrentPointer | None


def _fence_holds(request: PointerAdvanceRequest) -> bool:
    """Apply the §6b condition expression `expected_fence` selects."""
    current = request.current
    if request.expected_fence == 0:
        return current is None or current.fence == 0
    if current is None or current.fence != request.expected_fence:
        return False
    generation = request.generation
    if generation is None or current.input_end is None:
        return True
    return current.input_end <= generation.input_end


def next_pointer(request: PointerAdvanceRequest) -> PointerAdvance:
    """Return how a published, held or failed outcome moves `CURRENT`.

    `published` sets the generation and `fence = if_not_exists(fence, 0) + 1`; held and failed
    set only `last_attempt` and keep the fence (0 on a new item). `rolled_back_from` is left as
    it was: the §6b update expression doesn't name it.
    """
    current, generation = request.current, request.generation
    if (
        generation is not None
        and current is not None
        and current.generation_id == generation.generation_id
    ):
        return PointerAdvance(PointerDecision.ALREADY_CURRENT, None)
    if not _fence_holds(request):
        return PointerAdvance(PointerDecision.SUPERSEDED, None)
    base = current or CurrentPointer(fence=0)
    if generation is None:
        pointer = CurrentPointer(
            fence=base.fence,
            generation_id=base.generation_id,
            input_end=base.input_end,
            published_at=base.published_at,
            run_fingerprint=base.run_fingerprint,
            rolled_back_from=base.rolled_back_from,
            last_attempt=request.attempt,
        )
    else:
        pointer = CurrentPointer(
            fence=base.fence + 1,
            generation_id=generation.generation_id,
            input_end=generation.input_end,
            published_at=generation.published_at,
            run_fingerprint=generation.run_fingerprint,
            rolled_back_from=base.rolled_back_from,
            last_attempt=request.attempt,
        )
    return PointerAdvance(PointerDecision.ADVANCE, pointer)


# --- Rollback -------------------------------------------------------------------------------


@dataclass(frozen=True)
class RollbackRequest:
    """An operator rollback: the set, the target generation, the fence read and the reason."""

    set_id: str
    generation_id: str
    expected_fence: int
    reason: str

    def __post_init__(self) -> None:
        if not self.set_id or not self.generation_id:
            raise ValueError("a rollback names its set and target generation")
        if self.expected_fence < 0:
            raise ValueError("expected_fence is never negative")
        if not 1 <= len(self.reason) <= OPERATOR_REASON_MAX_CHARS or not self.reason.isprintable():
            raise ValueError(f"REASON is 1 to {OPERATOR_REASON_MAX_CHARS} printable characters")


@dataclass(frozen=True)
class GenerationRecord:
    """What a rollback reads of the target's `META`."""

    generation_id: str
    set_id: str
    receipt_sk: str
    input_end: date
    run_fingerprint: str


@dataclass(frozen=True)
class ReceiptRecord:
    """What a rollback reads of the receipt `META.receipt_sk` names."""

    sk: str
    outcome: RunOutcome
    generation_id: str | None
    published_at: datetime | None


@dataclass(frozen=True)
class RollbackState:
    """The two items a rollback reads, `CURRENT`, and the rollback's own run ID and time."""

    current: CurrentPointer | None
    target: GenerationRecord | None
    receipt: ReceiptRecord | None
    rollback_run_id: str
    at: datetime


class RollbackRefusal(StrEnum):
    """Why a rollback is refused (§6c)."""

    TARGET_UNKNOWN = "ROLLBACK_TARGET_UNKNOWN"
    TARGET_OTHER_SET = "ROLLBACK_TARGET_OTHER_SET"
    NOTHING_CURRENT = "ROLLBACK_NOTHING_CURRENT"
    TARGET_IS_CURRENT = "ROLLBACK_TARGET_IS_CURRENT"
    TARGET_NEVER_PUBLISHED = "ROLLBACK_TARGET_NEVER_PUBLISHED"
    FENCE_MOVED = "ROLLBACK_FENCE_MOVED"


class RollbackRefusedError(Exception):
    """A rollback refused before any write, with its code."""

    def __init__(self, code: RollbackRefusal, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code


@dataclass(frozen=True)
class RollbackPlan:
    """The `CURRENT` state after the rollback and its receipt key."""

    pointer: CurrentPointer
    receipt_sk: str


@dataclass(frozen=True)
class _RollbackReads:
    current: CurrentPointer
    target: GenerationRecord
    published_at: datetime


def _admitted(request: RollbackRequest, state: RollbackState) -> _RollbackReads:
    """Raise `RollbackRefusedError` unless every §6c precondition holds."""
    target, current, receipt = state.target, state.current, state.receipt
    wanted = request.generation_id
    if target is None or target.generation_id != wanted:
        raise RollbackRefusedError(RollbackRefusal.TARGET_UNKNOWN, f"no META for {wanted}")
    if target.set_id != request.set_id:
        raise RollbackRefusedError(RollbackRefusal.TARGET_OTHER_SET, f"{wanted}: other set")
    if current is None or current.generation_id is None:
        raise RollbackRefusedError(RollbackRefusal.NOTHING_CURRENT, "no generation is CURRENT")
    if current.generation_id == wanted:
        raise RollbackRefusedError(RollbackRefusal.TARGET_IS_CURRENT, f"{wanted} is CURRENT")
    if (
        receipt is None
        or receipt.sk != target.receipt_sk
        or receipt.outcome is not RunOutcome.PUBLISHED
        or receipt.generation_id != wanted
        or receipt.published_at is None
    ):
        detail = f"{wanted}: {target.receipt_sk} is no published receipt for it"
        raise RollbackRefusedError(RollbackRefusal.TARGET_NEVER_PUBLISHED, detail)
    if current.fence != request.expected_fence:
        detail = f"fence {current.fence}, expected {request.expected_fence}"
        raise RollbackRefusedError(RollbackRefusal.FENCE_MOVED, detail)
    return _RollbackReads(current, target, receipt.published_at)


def rollback_pointer(request: RollbackRequest, state: RollbackState) -> RollbackPlan:
    """Return `CURRENT` after rolling back to a once-published generation, or refuse.

    `published_at` is the target's own (from its receipt, R4-15), so freshness describes what
    planners see; `last_attempt` suppresses the rolled-back-from fingerprint.
    """
    reads = _admitted(request, state)
    current, target = reads.current, reads.target
    attempt = LastAttempt(
        run_id=state.rollback_run_id,
        attempt_id=None,
        run_fingerprint=current.run_fingerprint,
        outcome=RunOutcome.ROLLED_BACK,
        failure_category=None,
        retry_disposition=Disposition.SUPPRESS,
        transient_retries=0,
        at=state.at,
    )
    pointer = CurrentPointer(
        fence=current.fence + 1,
        generation_id=target.generation_id,
        input_end=target.input_end,
        published_at=reads.published_at,
        run_fingerprint=target.run_fingerprint,
        rolled_back_from=current.generation_id,
        last_attempt=attempt,
    )
    key = receipt_sk(ReceiptKeyRequest(state.rollback_run_id, 0, "rollback"))
    return RollbackPlan(pointer, key)
