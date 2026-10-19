# SPDX-License-Identifier: MIT-0
"""Apply deterministic fixture deliveries through independently resumable spans."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast

from botocore.exceptions import ClientError

from adapters.jobs.curation.manifests import write_manifest
from adapters.jobs.land_fixtures.bundle import DeliveryClass, PlannedObject
from adapters.outbound.telemetry.tracing import LAND_FIXTURES_DELIVERY, span, trace_boundary
from domain.curation.landing import LandingError
from domain.operations.run_request import (
    FULL_HISTORY_NOT_ENABLED,
    HistoricalPolicy,
    ManifestFacts,
    is_historical,
)
from domain.shared_kernel.errors import DependencyError

if TYPE_CHECKING:  # pragma: no cover
    from types_boto3_s3.client import S3Client

    from adapters.jobs.land_fixtures.bundle import LandedDelivery
    from domain.curation.landing import LandedObject
    from domain.semantics.publication import SuccessfulPublication

CONTENT_TYPES = {"json": "application/json", "jsonl": "application/x-ndjson", "csv": "text/csv"}

SEAL_POLL_SECONDS = 5.0
"""How often the sequence guard reads the sealed partition's CURRENT (plan D1)."""

SEMANTIC_WRITE_VISIBILITY_SECONDS = 960
"""The semantic-write queue's visibility timeout: table-writer timeout 900 s plus 60 s margin."""
SEMANTIC_WRITE_MAX_RECEIVES = 3
"""The semantic-write queue's maxReceiveCount before its dead-letter queue."""
TABLE_WRITER_TIMEOUT_SECONDS = 900
SEAL_TIMEOUT_SECONDS = float(
    (SEMANTIC_WRITE_MAX_RECEIVES - 1) * SEMANTIC_WRITE_VISIBILITY_SECONDS
    + TABLE_WRITER_TIMEOUT_SECONDS
    + 60
)
"""How long one seal delivery may take to become CURRENT before the landing stops (plan D1).

A write unit the TableWriter returns as retryable (for example `admission_incomplete`) is
redelivered only after the queue's visibility timeout, so a live unit can take up to two such
waits plus one writer run before its last receive: 2 x 960 + 900 + 60 = 2,880 s. Past that the
unit is in the dead-letter queue and stopping is right. The earlier 600 s was shorter than one
retry: on 2026-10-06 a seal unit retried at 16 min attested after the guard had stopped.
"""


SEAL_READ_RETRY_SECONDS = 120.0
"""How long the guard keeps retrying a CURRENT read that fails to connect (read-only), before
the put; on 2026-10-06 a host network change made one read fail and stopped the landing."""
SETTLED_REPORT_KIND = "pipeline-settled"
SETTLED_REPORT_VERSION = 1
SETTLED_REPORT_MAX_AGE = timedelta(minutes=15)
"""The settled report a seal landing requires (plan D1); `probe_pipeline_settled.py` writes it.

Shape, a JSON object: `{"report": "pipeline-settled", "version": 1, "environment": <env>,
"account": <12 digits>, "checked_at": <ISO-8601 UTC instant>, "settled": <bool>,
"findings": [<string>, ...]}`. `findings` names each unsettled reason and is empty when
`settled` is true.
"""


class LandFixturesCancelledError(Exception):
    """Fixture landing stopped at a safe delivery boundary."""


def check_settled_report(  # noqa: PLR0913 -- the report and the three facts it must match
    document: object, *, environment: str, account: str, now: datetime
) -> None:
    """Refuse a seal landing unless the report says this pipeline settled within 15 minutes."""
    if not isinstance(document, dict):
        raise LandingError("the settled report must be a JSON object.")
    report = cast("dict[str, object]", document)
    if report.get("report") != SETTLED_REPORT_KIND or report.get("version") != (
        SETTLED_REPORT_VERSION
    ):
        raise LandingError(
            f"the settled report is not a {SETTLED_REPORT_KIND} v{SETTLED_REPORT_VERSION} report."
        )
    if report.get("environment") != environment or report.get("account") != account:
        raise LandingError("the settled report describes another environment or account.")
    try:
        checked_at = datetime.fromisoformat(str(report.get("checked_at")).replace("Z", "+00:00"))
    except ValueError:
        raise LandingError("the settled report's checked_at is not an instant.") from None
    if checked_at.tzinfo is None:
        raise LandingError("the settled report's checked_at is not an instant.")
    if checked_at > now or now - checked_at > SETTLED_REPORT_MAX_AGE:
        raise LandingError(
            f"the settled report was checked at {checked_at.isoformat()}, not within "
            f"{SETTLED_REPORT_MAX_AGE.total_seconds() / 60:.0f} minutes before "
            f"{now.isoformat()}; re-run probe_pipeline_settled.py."
        )
    findings = report.get("findings")
    if report.get("settled") is not True or findings != []:
        raise LandingError(f"the pipeline is not settled: {findings!r}.")


@dataclass(frozen=True, slots=True)
class SealWait:
    """How long one seal delivery took to become CURRENT, for the job lifecycle details."""

    fixture: str
    polls: int
    waited_seconds: float
    publication_id: str


@dataclass(frozen=True, slots=True)
class SealSequenceGuard:
    """Land each campaign-dimension seal delivery only after the previous one is CURRENT.

    The SCD2 fold takes its base from the latest CURRENT partition at or before a delivery's
    first bucket, so a seal delivery prepared before its predecessor is written would fold from
    the wrong base (R8-1). The guard reads the delivery's scope at its last bucket before the
    put, then polls until that partition carries the delivery's watermark as its source
    revision and is a newer publication than the one read before the put. `read_current` is a
    read-only view of the CURRENT publications; nothing here writes state.
    """

    read_current: Callable[[LandedDelivery], SuccessfulPublication | None]
    report: Callable[[SealWait], None]
    sleep: Callable[[float], None] = field(default=time.sleep)
    clock: Callable[[], float] = field(default=time.monotonic)
    poll_seconds: float = SEAL_POLL_SECONDS
    timeout_seconds: float = SEAL_TIMEOUT_SECONDS

    def read_before(self, delivery: LandedDelivery) -> SuccessfulPublication | None:
        """Read the CURRENT before a put, retrying a dependency failure for a bounded time.

        Nothing is put until this returns, so a retry here can never reorder a seal.
        """
        started = self.clock()
        while True:
            try:
                return self.read_current(delivery)
            except DependencyError:
                if self.clock() - started >= SEAL_READ_RETRY_SECONDS:
                    raise
                self.sleep(self.poll_seconds)

    def await_current(
        self,
        delivery: LandedDelivery,
        before: SuccessfulPublication | None,
        cancelled: Callable[[], bool],
    ) -> SealWait:
        """Poll until the delivery is CURRENT, raising `LandingError` at the timeout."""
        started = self.clock()
        polls = 0
        while True:
            _checkpoint(cancelled)
            self.sleep(self.poll_seconds)
            polls += 1
            try:
                current = self.read_current(delivery)
            # error-propagation: a bounded poll; a failed read proves nothing, the timeout raises
            except DependencyError:
                current = None
            if current is not None and _sealed(delivery, before, current):
                return SealWait(
                    delivery.fixture,
                    polls,
                    self.clock() - started,
                    current.publication_id.value,
                )
            if self.clock() - started >= self.timeout_seconds:
                raise LandingError(
                    f"{delivery.fixture} was not CURRENT with watermark "
                    f"{delivery.manifest.source_watermark} after {self.timeout_seconds:.0f} s; "
                    "no further delivery was put. Diagnose, then re-land the seal under "
                    "seal_generation + 1 with --classes seal (ADR-086)."
                )


def _sealed(
    delivery: LandedDelivery,
    before: SuccessfulPublication | None,
    current: SuccessfulPublication,
) -> bool:
    """Whether `current` is this delivery's publication: its watermark, newer than `before`."""
    watermark = delivery.manifest.source_watermark
    if watermark is None or current.source_revision.observed_at.date() != watermark:
        return False
    return before is None or (
        current.publication_id != before.publication_id
        and current.published_at >= before.published_at
    )


@dataclass(frozen=True, slots=True)
class LandFixturesProgress:
    """Last durable fixture-delivery progress for lifecycle and recovery reporting."""

    operation_id: str
    execution_id: str
    predecessor_execution_id: str | None
    plan_digest: str
    stage: str
    completed: int
    total: int


@dataclass(frozen=True, slots=True)
class ApplyLandFixturesRequest:
    """Explicit immutable landing target, lineage, and operator callbacks."""

    client: S3Client
    bucket: str
    deliveries: tuple[LandedDelivery, ...]
    operation_id: str
    execution_id: str
    predecessor_execution_id: str | None
    plan_digest: str
    progress: Callable[[LandFixturesProgress], None]
    cancelled: Callable[[], bool]
    historical_policy: HistoricalPolicy
    """The V-8 replay band: a historical delivery is refused before any put."""
    seal_guard: SealSequenceGuard | None = None


def _checkpoint(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise LandFixturesCancelledError("fixture landing cancellation requested")


def _content_type(key: str) -> str:
    """Give the content type for a landed object, defaulting to octet-stream."""
    return CONTENT_TYPES.get(key.rsplit(".", maxsplit=1)[-1], "application/octet-stream")


def _put_immutable(client: S3Client, bucket: str, planned: PlannedObject) -> tuple[str, bool]:
    try:
        response = client.put_object(
            Bucket=bucket,
            Key=planned.key,
            Body=planned.body,
            ContentType=_content_type(planned.key),
            IfNoneMatch="*",
        )
        version = str(response.get("VersionId", ""))
        if not version:
            raise RuntimeError("immutable fixture write returned no VersionId.")
        return version, True
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", ""))
        if code not in {"PreconditionFailed", "412"}:
            raise
    existing = client.get_object(Bucket=bucket, Key=planned.key)
    if existing["Body"].read() != planned.body:
        raise RuntimeError("immutable fixture target already contains different bytes.") from None
    version = str(existing.get("VersionId", ""))
    if not version:
        raise RuntimeError("immutable fixture read returned no VersionId.")
    return version, False


@trace_boundary(LAND_FIXTURES_DELIVERY)
def apply_immutable(request: ApplyLandFixturesRequest) -> int:
    """Write each delivery's data once and then its exact-version manifest marker.

    A `seal` delivery is put only through the sequence guard: its CURRENT is read before the
    put and awaited after it, so the next delivery is never put before this one is CURRENT.
    Every other delivery is put as before.
    """
    historical = [
        delivery
        for delivery in request.deliveries
        if is_historical(
            ManifestFacts(
                delivery.manifest.source_system,
                delivery.manifest.source_dataset,
                delivery.manifest.period_end,
                delivery.manifest.retrieved_at,
            ),
            request.historical_policy,
        )
    ]
    if historical:
        raise LandingError(
            f"{FULL_HISTORY_NOT_ENABLED}: {len(historical)} historical deliveries refused."
        )
    created = 0
    total = len(request.deliveries)
    guard = request.seal_guard
    if guard is None and any(
        delivery.delivery_class is DeliveryClass.SEAL for delivery in request.deliveries
    ):
        raise LandingError("seal deliveries land only through the sequence guard.")
    for completed, delivery in enumerate(request.deliveries):
        _checkpoint(request.cancelled)
        sealing = guard if delivery.delivery_class is DeliveryClass.SEAL else None
        before = None if sealing is None else sealing.read_before(delivery)
        delivery_created = 0
        with span(
            LAND_FIXTURES_DELIVERY,
            operation_id=request.operation_id,
            execution_id=request.execution_id,
            predecessor_execution_id=request.predecessor_execution_id or "none",
            plan_digest=request.plan_digest,
            stage="apply",
            completed=completed,
            total=total,
        ) as delivery_span:
            bound_objects: list[LandedObject] = []
            for planned, landed in zip(delivery.data, delivery.manifest.objects, strict=True):
                _checkpoint(request.cancelled)
                version, was_created = _put_immutable(request.client, request.bucket, planned)
                delivery_created += int(was_created)
                bound_objects.append(replace(landed, version_id=version))
            _checkpoint(request.cancelled)
            bound_manifest = replace(delivery.manifest, objects=tuple(bound_objects))
            marker = PlannedObject(delivery.marker.key, write_manifest(bound_manifest).encode())
            _version, was_created = _put_immutable(request.client, request.bucket, marker)
            delivery_created += int(was_created)
            created += delivery_created
            # A resumed run re-puts identical keys, which trigger nothing; only then may an
            # already-sealed CURRENT stand in for the wait.
            if sealing is not None and (
                delivery_created or before is None or not _sealed(delivery, None, before)
            ):
                wait = sealing.await_current(delivery, before, request.cancelled)
                sealing.report(wait)
            durable_completed = completed + 1
            delivery_span.record(completed=durable_completed)
        request.progress(
            LandFixturesProgress(
                operation_id=request.operation_id,
                execution_id=request.execution_id,
                predecessor_execution_id=request.predecessor_execution_id,
                plan_digest=request.plan_digest,
                stage="apply",
                completed=durable_completed,
                total=total,
            )
        )
    return created
