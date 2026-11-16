# SPDX-License-Identifier: MIT-0
"""The demand-forecast publisher Lambda and the operator's rollback (design §6b, §6c, §6d).

One Lambda (`ForecastPublisherFunction`) serves the `Publish` map, the `Commit` state and
`RecordOutcome`; `rollback()` is the operator's (`make demand-forecast-rollback`).

- **publish_shard** reads only the named attempt's candidate, the manifest and the title map,
  applies `validate_shard_candidate()`, writes the shard's `ASIN#` items and then its roster part
  with `If-None-Match: *`. A replayed part must have the same SHA-256. It returns only the shard
  result (about 300 bytes) and never touches a `SET#` key.
- **commit_generation** passes a held shard through to `RecordOutcome`, or reads exactly the
  roster parts the results name, applies `validate_commit()`, ranks, writes every `PAGE#` item,
  then `META`, attests it, and commits the outcome transaction.
- **record_outcome** writes a receipt-only outcome, or a held or failed outcome with its
  `last_attempt` and retry disposition, in one transaction.

Only three read failures hold a run: a missing object (`NoSuchKey`), a digest mismatch and a
line-count mismatch (`identity_mismatch`), plus an unparseable object (`schema_mismatch`). Any
other S3 error raises `ObjectStoreError` with the AWS code, so the state's retries apply and a
spent retry ends `failed(publish:<code>)` with disposition `retry` (R5C2-11). A request the
state machine should never send raises `PublishFailedError("REQUEST_INVALID")`.

Each operation emits `ForecastRunOutcome{outcome}` once per receipt it writes (never for a
replay), and a publish also `LandingToPublishSeconds`. The terminal log line carries
`store_retry_attempts` and, on a throttle, `throttled` (contention tracking).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

import boto3
from aws_lambda_powertools import Logger
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ValidationError

from adapters.config import demand_forecast_publisher_settings, tracing_settings
from adapters.jobs.demand_forecast.json_output import REPORT_FILE, SERIES_FILE, canonical_json
from adapters.outbound.demand_forecast_policy import LoadedPolicy, load_policy
from adapters.outbound.dynamodb.demand_forecast_store import DynamoDbForecastStore
from adapters.outbound.telemetry.bootstrap import configure_tracing, flushes_traces
from adapters.outbound.telemetry.demand_forecast_metrics import (
    CloudWatchDemandForecastMetrics,
    Datapoint,
    landing_to_publish,
    run_outcome,
)
from adapters.outbound.telemetry.reporting import LogSeverity, OperationLog, report_operation
from adapters.outbound.telemetry.tracing import DEMAND_FORECAST_PUBLISH, span, trace_boundary
from domain.forecasting.boundary import (
    CandidateSeriesV1,
    ForecastSetV1,
    InputManifestV1,
    RosterEntryV1,
    RunReportV1,
    TitleMapV1,
)
from domain.forecasting.list_pages import cut_pages, merge_roster_parts, rank_entries
from domain.forecasting.policy import MAX_SHARDS, MONEY_TARGETS
from domain.forecasting.publish import (
    CommitValidationRequest,
    CurrentPointer,
    Disposition,
    HeldCheck,
    HeldQuality,
    LastAttempt,
    OutcomeRecord,
    PointerAdvanceRequest,
    PointerDecision,
    PublishedGeneration,
    ReceiptKeyRequest,
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
from domain.forecasting.summaries import cap_title, roster_entry
from ports.outbound.forecast_store import (
    Attestation,
    CommitDisposition,
    ForecastItemTooLargeError,
    ForecastStoreConflictError,
    ForecastStorePort,
    ForecastStoreThrottled,
    GenerationMeta,
    GenerationSeries,
    OutcomeCommitRequest,
    OutcomeCommitResult,
    RunReceipt,
)

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

_TRACING = configure_tracing(tracing_settings())
_LOGGER = Logger(service="adlytics-demand-forecast-publisher")
_RUN_ID: Final = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
"""The input builder's run ID rule; the execution name is the run ID."""
_MANIFEST_KEY: Final = re.compile(
    r"^runs/(?P<run>[A-Za-z0-9_-]{1,80})/input/(?P<attempt>r[0-9]{1,4}a[0-9]{1,4})/manifest\.json$"
)
_CODE: Final = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
REQUEST_INVALID: Final = "REQUEST_INVALID"
META_MISSING: Final = "META_MISSING"
ERROR_UNREADABLE: Final = "ERROR_UNREADABLE"
"""The failure code when a caught error names no readable code."""
FAILURE_PHASES: Final = ("input_build", "job", "publish")
"""The `<phase>` of `failed(<phase>:<code>)` (§6d)."""
CODED_ERRORS: Final = frozenset(
    {"InputBuildFailedError", "PublishFailedError", "ObjectStoreError", "MetricEmitError"}
)
"""Lambda errors whose message is the failure code; any other error's code is its name."""
RECORDED_OUTCOMES: Final = frozenset(
    {
        RunOutcome.NO_OP_SAME_INPUT,
        RunOutcome.HELD_INPUT_NOT_READY,
        RunOutcome.HELD_QUALITY,
        RunOutcome.FAILED,
    }
)
"""The outcomes `RecordOutcome` writes; `published` and `superseded` come from the commit."""
_RECEIPT_ONLY: Final = frozenset({RunOutcome.NO_OP_SAME_INPUT, RunOutcome.HELD_INPUT_NOT_READY})
_NEW_RECEIPT: Final = frozenset({CommitDisposition.APPLIED, CommitDisposition.SUPERSEDED})


class PublishFailedError(Exception):
    """A refusal raised to the state machine; the message is the failure code."""


class ObjectMissingError(Exception):
    """The named object does not exist (`NoSuchKey`)."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


class ObjectStoreError(Exception):
    """Any other S3 failure; the message is the AWS error code. The step may be retried."""


class PublishHeld(Exception):  # noqa: N818 - names the outcome, which is not an error
    """A check held the run; `RecordOutcome` records `held_quality(<check>)`."""

    def __init__(self, held: HeldQuality) -> None:
        super().__init__(f"{held.category}: {held.detail}")
        self.held = held


# ---- composition seams (fakes in unit tests) ----


class PublisherObjects(Protocol):
    """The processing bucket operations the publisher needs."""

    def get(self, key: str) -> bytes:
        """Return the object's bytes; raise `ObjectMissingError` or `ObjectStoreError`."""
        ...

    def put_if_absent(self, key: str, body: bytes) -> bool:
        """Write the object unless the key exists (`If-None-Match: *`); False when it exists."""
        ...


class MetricsSink(Protocol):
    """Where operational datapoints go."""

    def emit(self, points: Sequence[Datapoint], timestamp: datetime) -> None:
        """Put every datapoint at `timestamp`."""
        ...


@dataclass(frozen=True)
class Publisher:
    """Everything one invocation uses: the policy, the set and its ports."""

    policy: LoadedPolicy
    set_id: str
    objects: PublisherObjects
    store: ForecastStorePort
    metrics: MetricsSink
    now: Callable[[], datetime]


@dataclass(frozen=True)
class StepOutcome:
    """What one operation returns to the state machine, and the counts its span records."""

    outcome: str
    step: str
    category: str | None = None
    result: ShardPublishResult | None = None
    shard: int | None = None
    attempt_id: str | None = None
    generation_id: str | None = None
    receipt_sk: str | None = None
    series_published: int = 0
    series_withheld: int = 0
    store_retry_attempts: int = 0

    def payload(self) -> dict[str, object]:
        """Return the state output: a shard result, a held outcome or a recorded outcome."""
        if self.result is not None:
            result = self.result
            return {
                "outcome": self.outcome,
                "step": self.step,
                "shard": result.shard,
                "attempt_id": result.attempt_id,
                "series_sha256": result.series_sha256,
                "item_count": result.item_count,
                "roster_part_key": result.roster_part_key,
                "roster_part_sha256": result.roster_part_sha256,
            }
        document: dict[str, object] = {
            "outcome": self.outcome,
            "step": self.step,
            "category": self.category,
            "shard": self.shard,
            "attempt_id": self.attempt_id,
            "generation_id": self.generation_id,
            "receipt_sk": self.receipt_sk,
        }
        return {name: value for name, value in document.items() if value is not None}


# ---- keys and bytes ----


def attempt_prefix(run_id: str, shard: int, attempt_id: str) -> str:
    """Return one shard attempt's prefix: `runs/<run>/shards/<k>/attempts/<attempt>/`."""
    return f"runs/{run_id}/shards/{shard}/attempts/{attempt_id}/"


def roster_part_key(run_id: str, shard: int, attempt_id: str) -> str:
    """Return the shard attempt's roster part key (R5-03)."""
    return f"{attempt_prefix(run_id, shard, attempt_id)}roster-part.jsonl.gz"


def roster_part_bytes(entries: Sequence[RosterEntryV1]) -> bytes:
    """One canonical JSON line per entry, gzip with a zero timestamp: same entries, same bytes."""
    lines = "".join(canonical_json(entry.model_dump(mode="json")) + "\n" for entry in entries)
    return gzip.compress(lines.encode("utf-8"), mtime=0)


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _held(check: HeldCheck, detail: str) -> PublishHeld:
    return PublishHeld(HeldQuality(check, detail))


def _read(objects: PublisherObjects, key: str, what: str) -> bytes:
    try:
        return objects.get(key)
    except ObjectMissingError:
        raise _held(HeldCheck.IDENTITY_MISMATCH, f"{what} {key} is missing") from None


def _parse[M: BaseModel](model: type[M], body: bytes, what: str) -> M:
    try:
        return model.model_validate_json(body)
    except ValidationError as error:
        detail = f"{what}: {error.error_count()} validation errors"
        raise _held(HeldCheck.SCHEMA_MISMATCH, detail) from None


def _lines[M: BaseModel](model: type[M], body: bytes, what: str) -> tuple[M, ...]:
    try:
        text = gzip.decompress(body).decode("utf-8")
    except OSError, EOFError, zlib.error, UnicodeDecodeError:
        raise _held(HeldCheck.SCHEMA_MISMATCH, f"{what} is not gzip UTF-8 text") from None
    return tuple(_parse(model, line.encode("utf-8"), what) for line in text.splitlines())


def _manifest(objects: PublisherObjects, run_id: str, manifest_key: str) -> InputManifestV1:
    match = _MANIFEST_KEY.fullmatch(manifest_key)
    if match is None or match["run"] != run_id:
        raise PublishFailedError(REQUEST_INVALID)
    manifest = _parse(InputManifestV1, _read(objects, manifest_key, "manifest"), "manifest")
    if manifest.run_id != run_id or manifest.build_attempt_id != match["attempt"]:
        detail = f"manifest {manifest_key} names another run or build attempt"
        raise _held(HeldCheck.IDENTITY_MISMATCH, detail)
    return manifest


def load_titles(objects: PublisherObjects, manifest: InputManifestV1) -> TitleMapV1:
    """Read the manifest's exact title map, check its SHA-256 and validate it (R3-08)."""
    expected = f"runs/{manifest.run_id}/input/{manifest.build_attempt_id}/titles.json"
    if manifest.titles_key != expected:
        detail = f"titles_key {manifest.titles_key} is not the build attempt's"
        raise _held(HeldCheck.IDENTITY_MISMATCH, detail)
    body = _read(objects, manifest.titles_key, "title map")
    if _sha256(body) != manifest.titles_sha256:
        raise _held(HeldCheck.TITLE_MAP_MISMATCH, "title map digest differs from the manifest")
    return _parse(TitleMapV1, body, "title map")


def _stored[R](write: Callable[[], R]) -> R:
    """Run one store call; a conflict or an over-ceiling item holds the run."""
    try:
        return write()
    except ForecastStoreConflictError as error:
        raise _held(HeldCheck.IDENTITY_MISMATCH, str(error)) from None
    except ForecastItemTooLargeError as error:
        raise _held(HeldCheck.ITEM_TOO_LARGE, str(error)) from None


def _emit(publisher: Publisher, points: Sequence[Datapoint], at: datetime) -> None:
    publisher.metrics.emit(points, at)


# ---- publish_shard ----


@dataclass(frozen=True)
class PublishShardRequest:
    """One `Publish` map iteration: the shard, its bound attempt and every shard's attempt."""

    run_id: str
    redrive_count: int
    shard: int
    attempt_id: str
    manifest_key: str
    attempts: tuple[ShardAttempt, ...]

    def __post_init__(self) -> None:
        bound = {attempt.shard: attempt.attempt_id for attempt in self.attempts}
        if (
            _RUN_ID.fullmatch(self.run_id) is None
            or self.redrive_count < 0
            or not 1 <= len(self.attempts) <= MAX_SHARDS
            or bound.get(self.shard) != self.attempt_id
        ):
            raise PublishFailedError(REQUEST_INVALID)

    @property
    def step(self) -> str:
        """The receipt step a held shard records: `s<k>-<attempt_id>`."""
        return f"s{self.shard}-{self.attempt_id}"


def _shown(records: Sequence[CandidateSeriesV1]) -> tuple[int, int]:
    shown = sum(
        1 for record in records for row in record.targets.values() if row.chosen_model is not None
    )
    return shown, sum(len(record.targets) for record in records) - shown


def _publish_shard(publisher: Publisher, request: PublishShardRequest) -> StepOutcome:
    objects = publisher.objects
    manifest = _manifest(objects, request.run_id, request.manifest_key)
    prefix = f"{attempt_prefix(request.run_id, request.shard, request.attempt_id)}candidate/"
    report = _parse(RunReportV1, _read(objects, prefix + REPORT_FILE, "run report"), "run report")
    series_body = _read(objects, prefix + SERIES_FILE, "series")
    if _sha256(series_body) != report.series_sha256:
        detail = f"shard {request.shard}: series digest differs from its run report"
        raise _held(HeldCheck.IDENTITY_MISMATCH, detail)
    records = _lines(CandidateSeriesV1, series_body, "series")
    titles = load_titles(objects, manifest)
    held = validate_shard_candidate(
        ShardCandidateValidationRequest(
            run_id=request.run_id,
            shard=request.shard,
            attempt_id=request.attempt_id,
            manifest=manifest,
            report=report,
            records=records,
            series_sha256=_sha256(series_body),
            titles=titles,
            margin=publisher.policy.policy.gate.margin,
        )
    )
    if held is not None:
        raise PublishHeld(held)
    title_of = {entry.canonical_asin: entry.title for entry in titles.entries}
    max_bytes = publisher.policy.policy.serving.title_max_bytes
    ordered = sorted(records, key=lambda record: record.canonical_asin)
    capped = [cap_title(title_of[record.canonical_asin], max_bytes) for record in ordered]
    series = tuple(
        GenerationSeries(record, title.title, title.truncated)
        for record, title in zip(ordered, capped, strict=True)
    )
    generation = generation_id(request.run_id, request.attempts)
    written = _stored(lambda: publisher.store.put_generation_items(generation, series))
    entries = [
        roster_entry(record, title.title, title_truncated=title.truncated)
        for record, title in zip(ordered, capped, strict=True)
    ]
    body = roster_part_bytes(entries)
    key = roster_part_key(request.run_id, request.shard, request.attempt_id)
    if not objects.put_if_absent(key, body) and _sha256(
        _read(objects, key, "roster part")
    ) != _sha256(body):
        raise _held(HeldCheck.IDENTITY_MISMATCH, f"replayed roster part {key} differs")
    result = ShardPublishResult(
        shard=request.shard,
        attempt_id=request.attempt_id,
        series_sha256=report.series_sha256,
        item_count=len(records),
        roster_part_key=key,
        roster_part_sha256=_sha256(body),
    )
    shown, withheld = _shown(records)
    return StepOutcome(
        outcome=RunOutcome.PUBLISHED.value,
        step=request.step,
        result=result,
        series_published=shown,
        series_withheld=withheld,
        store_retry_attempts=written.retry_attempts,
    )


def publish_shard(publisher: Publisher, request: PublishShardRequest) -> StepOutcome:
    """Validate one shard attempt, write its `ASIN#` items and roster part; or hold it."""
    try:
        return _publish_shard(publisher, request)
    except PublishHeld as held:
        return StepOutcome(
            outcome=RunOutcome.HELD_QUALITY.value,
            step=request.step,
            category=held.held.category,
            shard=request.shard,
            attempt_id=request.attempt_id,
        )


# ---- commit_generation ----


@dataclass(frozen=True)
class HeldShard:
    """A `publish_shard` that held, passed through the commit to `RecordOutcome`."""

    shard: int
    attempt_id: str
    step: str
    category: str


@dataclass(frozen=True)
class CommitRequest:
    """The `Commit` state: the run, its pass and every shard's `publish_shard` output."""

    run_id: str
    redrive_count: int
    manifest_key: str
    published: tuple[ShardPublishResult, ...]
    held: tuple[HeldShard, ...] = ()

    def __post_init__(self) -> None:
        if (
            _RUN_ID.fullmatch(self.run_id) is None
            or self.redrive_count < 0
            or not 1 <= len(self.published) + len(self.held) <= MAX_SHARDS
        ):
            raise PublishFailedError(REQUEST_INVALID)

    @property
    def receipt_sk(self) -> str:
        """`RUN#<run_id>#r<RedriveCount>#commit`."""
        return receipt_sk(ReceiptKeyRequest(self.run_id, self.redrive_count, "commit"))


def _read_parts(
    publisher: Publisher, request: CommitRequest, manifest: InputManifestV1
) -> tuple[RosterPart, ...]:
    """Read exactly the parts the results name, after checking they are the planned shards."""
    planned = sorted(spec.index for spec in manifest.shards)
    named = sorted(result.shard for result in request.published)
    if named != planned:
        detail = f"shard results cover {named}, the manifest names {planned}"
        raise _held(HeldCheck.IDENTITY_MISMATCH, detail)
    parts: list[RosterPart] = []
    for result in sorted(request.published, key=lambda result: result.shard):
        expected = roster_part_key(request.run_id, result.shard, result.attempt_id)
        if result.roster_part_key != expected:
            detail = f"shard {result.shard}: roster part key is not its attempt's"
            raise _held(HeldCheck.IDENTITY_MISMATCH, detail)
        what = f"shard {result.shard} roster part"
        body = _read(publisher.objects, expected, what)
        digest = _sha256(body)
        if digest != result.roster_part_sha256:
            # Before parsing: a changed byte is a digest mismatch, never a schema mismatch.
            raise _held(HeldCheck.IDENTITY_MISMATCH, f"{what} digest differs from its result")
        parts.append(RosterPart(result.shard, digest, _lines(RosterEntryV1, body, what)))
    return tuple(parts)


def _currencies(parts: Sequence[RosterPart]) -> tuple[str, ...]:
    found = {
        row.currency_code
        for part in parts
        for entry in part.entries
        for target, row in entry.targets.items()
        if target in MONEY_TARGETS and row.currency_code is not None
    }
    return tuple(sorted(found))


def _commit_generation(publisher: Publisher, request: CommitRequest) -> StepOutcome:
    manifest = _manifest(publisher.objects, request.run_id, request.manifest_key)
    if forecast_set_id(manifest.forecast_set) != publisher.set_id:
        raise _held(HeldCheck.IDENTITY_MISMATCH, "the manifest names another forecast set")
    parts = _read_parts(publisher, request, manifest)
    titles = load_titles(publisher.objects, manifest)
    validation = validate_commit(
        CommitValidationRequest(manifest, request.published, parts, len(titles.entries))
    )
    if validation.held is not None:
        raise PublishHeld(validation.held)
    serving = publisher.policy.policy.serving
    ranked = rank_entries(merge_roster_parts([part.entries for part in parts]), serving.rank_by)
    attempts = [ShardAttempt(result.shard, result.attempt_id) for result in request.published]
    generation = generation_id(request.run_id, attempts)
    meta = GenerationMeta(
        set_id=publisher.set_id,
        run_id=request.run_id,
        receipt_sk=request.receipt_sk,
        input_end=manifest.input_end,
        run_fingerprint=manifest.run_fingerprint,
        run_identity=manifest.run_identity,
        attempts=request.published,
        currency_codes=_currencies(parts),
        counts_by_reason=validation.counts_by_reason,
        counts_by_model=validation.counts_by_model,
        pages=cut_pages(ranked, serving),
    )
    store = publisher.store
    retries = _stored(lambda: store.put_list_pages(generation, meta.pages.pages)).retry_attempts
    retries += _stored(lambda: store.put_generation_meta(meta)).retry_attempts
    attestation = store.attest_generation(meta)
    if attestation is Attestation.META_MISSING:
        raise PublishFailedError(META_MISSING)
    if attestation is Attestation.META_DIFFERS:
        raise _held(HeldCheck.IDENTITY_MISMATCH, f"META of {generation} differs on re-read")
    shown = sum(validation.counts_by_model.values())
    withheld = sum(validation.counts_by_reason.values())
    committed = _commit_publish(publisher, manifest, meta)
    return StepOutcome(
        outcome=committed.outcome,
        step="commit",
        generation_id=generation if committed.outcome == RunOutcome.PUBLISHED else None,
        receipt_sk=request.receipt_sk,
        series_published=shown,
        series_withheld=withheld,
        store_retry_attempts=retries + committed.retry_attempts,
    )


@dataclass(frozen=True)
class _Committed:
    outcome: str
    retry_attempts: int


def _recorded(result: OutcomeCommitResult) -> str:
    if result.disposition is CommitDisposition.ALREADY_CURRENT:
        return RunOutcome.PUBLISHED.value
    if result.disposition is CommitDisposition.SUPERSEDED:
        return RunOutcome.SUPERSEDED.value
    return result.recorded_outcome.value


def _commit_publish(
    publisher: Publisher,
    manifest: InputManifestV1,
    meta: GenerationMeta,
) -> _Committed:
    """Advance `CURRENT` to the attested generation, or record `superseded` (§6b)."""
    store, now = publisher.store, publisher.now()
    current = store.get_current(publisher.set_id)
    fingerprint = manifest.run_fingerprint
    decision = retry_disposition(
        OutcomeRecord(
            outcome=RunOutcome.PUBLISHED,
            failure_category=None,
            run_fingerprint=fingerprint,
            previous=None if current is None else current.last_attempt,
            policy=publisher.policy.policy.retry,
        )
    )
    attempt = LastAttempt(
        run_id=meta.run_id,
        attempt_id=None,
        run_fingerprint=fingerprint,
        outcome=RunOutcome.PUBLISHED,
        failure_category=None,
        retry_disposition=decision.disposition,
        transient_retries=decision.transient_retries,
        at=now,
    )
    generation = PublishedGeneration(meta.generation_id, meta.input_end, fingerprint, now)
    advance = next_pointer(
        PointerAdvanceRequest(current, manifest.expected_fence, attempt, generation)
    )
    if advance.decision is PointerDecision.ALREADY_CURRENT:
        return _Committed(RunOutcome.PUBLISHED.value, 0)
    landing = round((now - manifest.newest_publication_published_at).total_seconds())
    if advance.decision is PointerDecision.SUPERSEDED:
        receipt = RunReceipt(
            sk=meta.receipt_sk,
            outcome=RunOutcome.SUPERSEDED,
            recorded_at=now,
            run_fingerprint=fingerprint,
            run_identity=manifest.run_identity,
            counts_by_reason=meta.counts_by_reason,
        )
        commit = OutcomeCommitRequest(publisher.set_id, manifest.expected_fence, receipt)
    else:
        receipt = RunReceipt(
            sk=meta.receipt_sk,
            outcome=RunOutcome.PUBLISHED,
            recorded_at=now,
            generation_id=meta.generation_id,
            published_at=now,
            retry_disposition=decision.disposition,
            run_fingerprint=fingerprint,
            run_identity=manifest.run_identity,
            counts_by_reason=meta.counts_by_reason,
            landing_to_publish_seconds=landing,
        )
        commit = OutcomeCommitRequest(
            publisher.set_id,
            manifest.expected_fence,
            receipt,
            advance.pointer,
            meta.series_sha256,
        )
    result = _stored(lambda: store.commit_outcome(commit))
    outcome = _recorded(result)
    if result.disposition in _NEW_RECEIPT:
        points = [run_outcome(outcome)]
        if outcome == RunOutcome.PUBLISHED:
            points.append(landing_to_publish(landing))
        _emit(publisher, points, now)
    return _Committed(outcome, result.retry_attempts)


def commit_generation(publisher: Publisher, request: CommitRequest) -> StepOutcome:
    """Pass a held shard through, or validate, page, attest and commit the generation."""
    if request.held:
        first = min(request.held, key=lambda held: held.shard)
        return StepOutcome(
            outcome=RunOutcome.HELD_QUALITY.value,
            step=first.step,
            category=first.category,
            shard=first.shard,
            attempt_id=first.attempt_id,
        )
    try:
        return _commit_generation(publisher, request)
    except PublishHeld as held:
        return StepOutcome(
            outcome=RunOutcome.HELD_QUALITY.value,
            step="commit",
            category=held.held.category,
            receipt_sk=request.receipt_sk,
        )


# ---- record_outcome ----


def failure_category(phase: str, error: Mapping[str, object]) -> str:
    """Return `<phase>:<code>` from a state's caught error (`{Error, Cause}`).

    A coded Lambda error's code is its message; a job's is the `code` of its JSON exit message
    when the cause carries one [unverified: the `.sync` cause shape, OQ-11]; any other error's
    code is its name (`States.Timeout`, `ForecastStoreThrottled`, ...).
    """
    if phase not in FAILURE_PHASES:
        raise PublishFailedError(REQUEST_INVALID)
    name = error.get("Error")
    cause = _json_object(error.get("Cause"))
    code: object = name
    if name in CODED_ERRORS and cause is not None:
        code = cause.get("errorMessage", name)
    elif phase == "job" and cause is not None:
        exit_message = _json_object(cause.get("ExitMessage"))
        if exit_message is not None and "code" in exit_message:
            code = exit_message["code"]
    if not isinstance(code, str) or _CODE.fullmatch(code) is None:
        code = ERROR_UNREADABLE
    return f"{phase}:{code}"


def _json_object(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed: object = json.loads(value)
    except ValueError:
        return None
    return cast("Mapping[str, object]", parsed) if isinstance(parsed, dict) else None


@dataclass(frozen=True)
class RecordOutcomeRequest:
    """One `RecordOutcome` call: the step, what happened and what the run had read."""

    run_id: str
    redrive_count: int
    step: str
    outcome: RunOutcome
    category: str | None = None
    run_fingerprint: str | None = None
    attempt_id: str | None = None
    expected_fence: int | None = None
    """The fence `SelectInput` read; None when the run failed before reading it."""
    operator_reason: str | None = None

    def __post_init__(self) -> None:
        try:
            ReceiptKeyRequest(self.run_id, self.redrive_count, self.step)
        except ValueError:
            raise PublishFailedError(REQUEST_INVALID) from None
        categorised = self.outcome in {RunOutcome.HELD_QUALITY, RunOutcome.FAILED}
        if (
            self.outcome not in RECORDED_OUTCOMES
            or _RUN_ID.fullmatch(self.run_id) is None
            or (categorised and not self.category)
            or (self.outcome is RunOutcome.NO_OP_SAME_INPUT and self.category is not None)
            or (self.expected_fence is not None and self.expected_fence < 0)
        ):
            raise PublishFailedError(REQUEST_INVALID)

    @property
    def receipt_sk(self) -> str:
        """`RUN#<run_id>#r<RedriveCount>#<step>`."""
        return receipt_sk(ReceiptKeyRequest(self.run_id, self.redrive_count, self.step))


def _receipt(request: RecordOutcomeRequest, outcome: RunOutcome, at: datetime) -> RunReceipt:
    return RunReceipt(
        sk=request.receipt_sk,
        outcome=outcome,
        recorded_at=at,
        failure_category=request.category,
        attempt_id=request.attempt_id,
        operator_reason=request.operator_reason,
        run_fingerprint=request.run_fingerprint,
    )


def _pointer_commit(
    publisher: Publisher, request: RecordOutcomeRequest, at: datetime
) -> OutcomeCommitRequest:
    """Return a held or failed outcome's commit: `last_attempt` and receipt, or `superseded`."""
    current: CurrentPointer | None = publisher.store.get_current(publisher.set_id)
    decision = retry_disposition(
        OutcomeRecord(
            outcome=request.outcome,
            failure_category=request.category,
            run_fingerprint=request.run_fingerprint,
            previous=None if current is None else current.last_attempt,
            policy=publisher.policy.policy.retry,
        )
    )
    attempt = LastAttempt(
        run_id=request.run_id,
        attempt_id=request.attempt_id,
        run_fingerprint=request.run_fingerprint,
        outcome=request.outcome,
        failure_category=decision.failure_category,
        retry_disposition=decision.disposition,
        transient_retries=decision.transient_retries,
        at=at,
    )
    fence = request.expected_fence
    if fence is None:
        fence = 0 if current is None else current.fence
    advance = next_pointer(PointerAdvanceRequest(current, fence, attempt))
    if advance.decision is PointerDecision.SUPERSEDED:
        receipt = _receipt(request, RunOutcome.SUPERSEDED, at)
        return OutcomeCommitRequest(publisher.set_id, fence, receipt)
    receipt = RunReceipt(
        sk=request.receipt_sk,
        outcome=request.outcome,
        recorded_at=at,
        failure_category=request.category,
        retry_disposition=decision.disposition,
        attempt_id=request.attempt_id,
        operator_reason=request.operator_reason,
        run_fingerprint=request.run_fingerprint,
    )
    return OutcomeCommitRequest(publisher.set_id, fence, receipt, advance.pointer)


def record_outcome(publisher: Publisher, request: RecordOutcomeRequest) -> StepOutcome:
    """Write the outcome's receipt and, for a held or failed outcome, `last_attempt`."""
    at = publisher.now()
    if request.outcome in _RECEIPT_ONLY:
        commit = OutcomeCommitRequest(
            publisher.set_id, request.expected_fence or 0, _receipt(request, request.outcome, at)
        )
    else:
        commit = _pointer_commit(publisher, request, at)
    result = _stored(lambda: publisher.store.commit_outcome(commit))
    outcome = _recorded(result)
    if result.disposition in _NEW_RECEIPT:
        _emit(publisher, [run_outcome(outcome)], at)
    return StepOutcome(
        outcome=outcome,
        step=request.step,
        category=request.category,
        receipt_sk=result.receipt_sk,
        store_retry_attempts=result.retry_attempts,
    )


# ---- rollback (operator) ----


def rollback(publisher: Publisher, request: RollbackRequest, rollback_run_id: str) -> StepOutcome:
    """Make a once-published generation CURRENT again, fenced (§6c); refuse otherwise.

    Raises `RollbackRefusedError` with its code before any write.
    """
    store, at = publisher.store, publisher.now()
    current = store.get_current(request.set_id)
    target = store.get_generation(request.generation_id)
    receipt = None if target is None else store.get_receipt(request.set_id, target.receipt_sk)
    plan = rollback_pointer(request, RollbackState(current, target, receipt, rollback_run_id, at))
    record = RunReceipt(
        sk=plan.receipt_sk,
        outcome=RunOutcome.ROLLED_BACK,
        recorded_at=at,
        generation_id=request.generation_id,
        retry_disposition=Disposition.SUPPRESS,
        operator_reason=request.reason,
        run_fingerprint=plan.pointer.run_fingerprint,
    )
    commit = OutcomeCommitRequest(request.set_id, request.expected_fence, record, plan.pointer)
    result = store.commit_outcome(commit)
    outcome = _recorded(result)
    if result.disposition in _NEW_RECEIPT:
        _emit(publisher, [run_outcome(outcome)], at)
    return StepOutcome(
        outcome=outcome,
        step="rollback",
        generation_id=request.generation_id if outcome == RunOutcome.ROLLED_BACK else None,
        receipt_sk=result.receipt_sk,
        store_retry_attempts=result.retry_attempts,
    )


# ---- deployed composition ----


class S3PublisherObjects:
    """`PublisherObjects` over the processing bucket."""

    def __init__(self, client: S3Client, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def get(self, key: str) -> bytes:
        """Read one object whole (a candidate, a part, the manifest or the title map)."""
        try:
            return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except (ClientError, BotoCoreError) as error:
            raise s3_error(key, error) from error

    def put_if_absent(self, key: str, body: bytes) -> bool:
        """Write with `If-None-Match: *`; a 412 means the key exists."""
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=body,
                IfNoneMatch="*",
                ContentType="application/gzip",
            )
        except ClientError as error:
            if _s3_code(error) == "PreconditionFailed":
                return False
            raise s3_error(key, error) from error
        except BotoCoreError as error:
            raise s3_error(key, error) from error
        return True


def _s3_code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def s3_error(key: str, error: ClientError | BotoCoreError) -> Exception:
    """Map an S3 failure: `NoSuchKey` is a missing object; anything else keeps its code."""
    if isinstance(error, BotoCoreError):
        return ObjectStoreError(type(error).__name__)
    code = _s3_code(error)
    if code == "NoSuchKey":
        return ObjectMissingError(key)
    return ObjectStoreError(code if _CODE.fullmatch(code) else ERROR_UNREADABLE)


def _root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "contracts").is_dir():
            return candidate
    raise PublishFailedError("CONTRACTS_UNAVAILABLE")


@lru_cache(maxsize=1)
def _publisher() -> Publisher:
    settings = demand_forecast_publisher_settings()
    policy = load_policy(_root() / "contracts/forecasting/demand-forecast.yaml")
    forecast_set = ForecastSetV1(
        marketplace_key=settings.marketplace_key,
        distributor_view=policy.policy.input.distributor_view,
        selling_program=policy.policy.input.selling_program,
    )
    return Publisher(
        policy=policy,
        set_id=forecast_set_id(forecast_set),
        objects=S3PublisherObjects(boto3.client("s3"), settings.processing_bucket),
        store=DynamoDbForecastStore(settings.forecast_table, policy.policy.serving.item_max_bytes),
        metrics=CloudWatchDemandForecastMetrics(boto3.client("cloudwatch")),
        now=lambda: datetime.now(UTC),
    )


# ---- event parsing ----


def _text(event: Mapping[str, Any], key: str) -> str:
    value = event.get(key)
    if not isinstance(value, str) or not value:
        raise PublishFailedError(REQUEST_INVALID)
    return value


def _optional_text(event: Mapping[str, Any], key: str) -> str | None:
    value = event.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise PublishFailedError(REQUEST_INVALID)
    return value


def _int(event: Mapping[str, Any], key: str) -> int:
    value = event.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PublishFailedError(REQUEST_INVALID)
    return value


def _list(event: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = event.get(key)
    if not isinstance(value, list):
        raise PublishFailedError(REQUEST_INVALID)
    items = cast("list[object]", value)
    if not all(isinstance(item, dict) for item in items):
        raise PublishFailedError(REQUEST_INVALID)
    return cast("list[Mapping[str, Any]]", items)


def shard_request(event: Mapping[str, Any]) -> PublishShardRequest:
    """Parse one `Publish` map iteration's input."""
    try:
        attempts = tuple(
            ShardAttempt(_int(item, "shard"), _text(item, "attempt_id"))
            for item in _list(event, "attempts")
        )
    except ValueError:
        raise PublishFailedError(REQUEST_INVALID) from None
    return PublishShardRequest(
        run_id=_text(event, "run_id"),
        redrive_count=_int(event, "redrive_count"),
        shard=_int(event, "shard"),
        attempt_id=_text(event, "attempt_id"),
        manifest_key=_text(event, "manifest_key"),
        attempts=attempts,
    )


def commit_request(event: Mapping[str, Any]) -> CommitRequest:
    """Parse the `Commit` state's input: every `publish_shard` output."""
    published: list[ShardPublishResult] = []
    held: list[HeldShard] = []
    try:
        for item in _list(event, "shard_results"):
            outcome = item.get("outcome")
            if outcome == RunOutcome.HELD_QUALITY.value:
                held.append(
                    HeldShard(
                        _int(item, "shard"),
                        _text(item, "attempt_id"),
                        _text(item, "step"),
                        _text(item, "category"),
                    )
                )
            elif outcome == RunOutcome.PUBLISHED.value:
                published.append(
                    ShardPublishResult(
                        shard=_int(item, "shard"),
                        attempt_id=_text(item, "attempt_id"),
                        series_sha256=_text(item, "series_sha256"),
                        item_count=_int(item, "item_count"),
                        roster_part_key=_text(item, "roster_part_key"),
                        roster_part_sha256=_text(item, "roster_part_sha256"),
                    )
                )
            else:
                raise PublishFailedError(REQUEST_INVALID)
    except ValueError:
        raise PublishFailedError(REQUEST_INVALID) from None
    return CommitRequest(
        run_id=_text(event, "run_id"),
        redrive_count=_int(event, "redrive_count"),
        manifest_key=_text(event, "manifest_key"),
        published=tuple(published),
        held=tuple(held),
    )


def record_request(event: Mapping[str, Any]) -> RecordOutcomeRequest:
    """Parse `RecordOutcome`'s input; a caught error gives `failed(<phase>:<code>)`."""
    try:
        outcome = RunOutcome(_text(event, "outcome"))
    except ValueError:
        raise PublishFailedError(REQUEST_INVALID) from None
    category = _optional_text(event, "category")
    error = event.get("error")
    if error is not None:
        if outcome is not RunOutcome.FAILED or category is not None or not isinstance(error, dict):
            raise PublishFailedError(REQUEST_INVALID)
        category = failure_category(_text(event, "phase"), cast("Mapping[str, object]", error))
    fence = event.get("expected_fence")
    return RecordOutcomeRequest(
        run_id=_text(event, "run_id"),
        redrive_count=_int(event, "redrive_count"),
        step=_text(event, "step"),
        outcome=outcome,
        category=category,
        run_fingerprint=_optional_text(event, "run_fingerprint"),
        attempt_id=_optional_text(event, "attempt_id"),
        expected_fence=None if fence is None else _int(event, "expected_fence"),
        operator_reason=_optional_text(event, "operator_reason"),
    )


def _report(operation: str, outcome: str, details: Mapping[str, object]) -> None:
    report_operation(
        OperationLog(
            operation=f"demand_forecast.publish.{operation}",
            component="demand-forecast-publisher",
            phase=operation,
            outcome=outcome,
            severity=LogSeverity.INFO if outcome == "published" else LogSeverity.WARNING,
            details=details,
        ),
        _LOGGER,
    )


def _run(publisher: Publisher, operation: str, event: Mapping[str, Any]) -> StepOutcome:
    if operation == "publish_shard":
        return publish_shard(publisher, shard_request(event))
    if operation == "commit":
        return commit_generation(publisher, commit_request(event))
    if operation == "record":
        return record_outcome(publisher, record_request(event))
    raise PublishFailedError(REQUEST_INVALID)


@trace_boundary(DEMAND_FORECAST_PUBLISH)
@flushes_traces
@_LOGGER.inject_lambda_context
def handler(event: dict[str, Any], context: Any) -> dict[str, object]:
    """Dispatch `publish_shard`, `commit` or `record` from the state machine."""
    del context
    operation = str(event.get("operation", ""))
    with span(DEMAND_FORECAST_PUBLISH, operation=operation or "unknown") as recording:
        try:
            step = _run(_publisher(), operation, event)
        except ForecastStoreThrottled as error:
            _report(
                operation or "unknown",
                "throttled",
                {"throttled": True, "store_retry_attempts": error.retry_attempts},
            )
            raise
        held = step.outcome == RunOutcome.HELD_QUALITY.value
        recording.record(
            outcome=step.outcome,
            series_published=step.series_published,
            series_withheld=step.series_withheld,
            held_category=step.category if held else None,
        )
        payload = step.payload()
        _report(
            operation,
            step.outcome,
            {**payload, "store_retry_attempts": step.store_retry_attempts, "throttled": False},
        )
        return payload
