# SPDX-License-Identifier: MIT-0
"""Crash-safe, idempotent, sink-confirmed measurement (FR-3), fed by graph transitions (S5).

Design: "Lifecycle projection and crash-safe measurements".

A measurable transition writes one immutable contribution (`MEASUREMENT#<measurement_id>`) and
one `PENDING` acceptance in the same transaction as the projection row it measures. The
reconciler then accepts each contribution at most once into one fixed-window aggregate by a
strong-read compare-and-swap that also writes the next complete snapshot. The emitter publishes
a snapshot's complete cumulative statistics as gauges read only with `Maximum`, so a replay can
never add a contribution twice, and only an independent `GetMetricData` read that shows every
expected value moves the snapshot to `DELIVERED`, with an immutable confirmation receipt.

This module is the pure half: identities, statistics, the acceptance decision, the recovery
verdict after an ambiguous commit, the gauges a snapshot publishes and the confirmation check.
Its v1 constants (families, closed dimensions, buckets and window) are the metric contract,
revision `METRIC_CONTRACT_REVISION`; changing any of them is a new revision.

Pure: no I/O, no clock, no AWS.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final

from domain.operations.generated_execution_graphs import TEMPLATES
from domain.operations.status import canonical_authority_digest
from domain.shared_kernel.errors import ErrorCode, InvariantError

__all__ = [
    "BUCKETS_MS",
    "CLAIMED_NODE_KINDS",
    "DIMENSION_NAMES",
    "FAMILIES",
    "GRAPH_NODE_KINDS",
    "MEASURED_TRANSITIONS",
    "METRIC_CONTRACT_REVISION",
    "WINDOW",
    "AcceptanceRecord",
    "AcceptanceState",
    "Aggregate",
    "Gauge",
    "Measurement",
    "MeasurementCorruptError",
    "RecoveryVerdict",
    "Snapshot",
    "SnapshotState",
    "Statistics",
    "aggregate_id",
    "claim_measurement",
    "confirmed",
    "gauges",
    "measurement",
    "next_snapshot",
    "node_kind",
    "node_measurement",
    "receipt_measurement",
    "recovery_verdict",
    "snapshot_digest",
    "validate_chain",
    "window_of",
]

METRIC_CONTRACT_REVISION: Final = 1
FAMILIES: Final = ("FlowRootElapsed", "FlowTransitionLatency", "ActivityAdmissionElapsed")
"""Published family prefixes; each gains Count, SumMs, MaxMs and Bucket suffixes."""
DIMENSION_NAMES: Final = ("Protocol", "Surface", "Stage", "Outcome", "AuthorityScope")
"""The closed dimension set, in canonical order. No identifier is ever a dimension."""
BUCKETS_MS: Final = (1_000, 5_000, 15_000, 60_000, 300_000, 900_000, 3_600_000, 7_200_000)
"""Cumulative histogram upper bounds in milliseconds; a final `+Inf` bucket counts every sample."""
INFINITY_BUCKET: Final = "+Inf"
WINDOW: Final = timedelta(hours=1)
"""Fixed aggregation windows, aligned to the hour in UTC."""
GRAPH_NODE_KINDS: Final = tuple(
    dict.fromkeys(
        str(node["node"]) for template in TEMPLATES.values() for node in template["nodes"]
    )
)
"""Every template node kind, in template order: the `Stage` of a graph measurement."""
CLAIMED_NODE_KINDS: Final = tuple(
    str(node["node"])
    for template in TEMPLATES.values()
    for node in template["nodes"]
    if node.get("route") is not None
)
"""The routed kinds, the only ones a claimer admits (`grant_apply` runs in the release sweep)."""
MEASURED_TRANSITIONS: Final = frozenset(
    {
        ("FlowRootElapsed", "run_request_decided"),
        *(("FlowTransitionLatency", kind) for kind in GRAPH_NODE_KINDS),
        *(("ActivityAdmissionElapsed", kind) for kind in CLAIMED_NODE_KINDS),
    }
)
"""The FR-3 audit's measurable-transition set as `(family, Stage)` (S6 §2.10, carried item 0a).

S5 measured `{run_request_decided}` only. S6 part 2 adds each node kind's terminal transition and
each routed kind's claim admission; a contribution outside this set is not a declared producer.
"""


class MeasurementCorruptError(InvariantError):
    """Partial, conflicting, noncontiguous or regressed evidence: gate-blocking, never retried."""

    def __init__(self, description: str) -> None:
        super().__init__(ErrorCode("MEASUREMENT", 1), description)


class AcceptanceState(StrEnum):
    """`METRIC_ACCEPT#<measurement_id>` state."""

    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"


class SnapshotState(StrEnum):
    """`METRIC_SNAPSHOT#` state: local emission is never delivery."""

    PENDING = "PENDING"
    EMITTED_UNCONFIRMED = "EMITTED_UNCONFIRMED"
    DELIVERED = "DELIVERED"


@dataclass(frozen=True, slots=True)
class Statistics:
    """Monotonic sample count, sum, cumulative buckets (one per bound, then `+Inf`) and maximum."""

    count: int = 0
    sum_ms: int = 0
    buckets: tuple[int, ...] = (0,) * (len(BUCKETS_MS) + 1)
    max_ms: int = 0

    def add(self, value_ms: int) -> Statistics:
        """Return the statistics with one more sample."""
        if value_ms < 0:
            raise MeasurementCorruptError("a contribution is negative.")
        bounds = (*BUCKETS_MS, None)
        return Statistics(
            count=self.count + 1,
            sum_ms=self.sum_ms + value_ms,
            buckets=tuple(
                current + (1 if bound is None or value_ms <= bound else 0)
                for current, bound in zip(self.buckets, bounds, strict=True)
            ),
            max_ms=max(self.max_ms, value_ms),
        )

    def canonical(self) -> list[object]:
        """Return the statistics in canonical digest form."""
        return [self.count, self.sum_ms, list(self.buckets), self.max_ms]


@dataclass(frozen=True, slots=True)
class Measurement:
    """One immutable contribution, derived deterministically from the event it measures."""

    measurement_id: str
    family: str
    dimensions: tuple[tuple[str, str], ...]
    value_ms: int
    occurred_at: datetime
    source: str
    """The authority key of the measured transition (never a dimension)."""

    @property
    def window(self) -> tuple[datetime, datetime]:
        """Return the fixed window this contribution aggregates into."""
        return window_of(self.occurred_at)

    @property
    def aggregate_id(self) -> str:
        """Return the deterministic aggregate this contribution belongs to."""
        start, end = self.window
        return aggregate_id(self.family, start, end, self.dimensions)


@dataclass(frozen=True, slots=True)
class Aggregate:
    """One fixed-window aggregate at its strongly read revision (absent is revision 0)."""

    aggregate_id: str
    revision: int
    statistics: Statistics


@dataclass(frozen=True, slots=True)
class Snapshot:
    """`METRIC_SNAPSHOT#<aggregate_id>#<revision>`: one complete revision of an aggregate."""

    aggregate_id: str
    revision: int
    statistics: Statistics
    measurement_id: str
    digest: str
    state: SnapshotState = SnapshotState.PENDING


@dataclass(frozen=True, slots=True)
class AcceptanceRecord:
    """`METRIC_ACCEPT#<measurement_id>`, and the binding acceptance writes."""

    measurement_id: str
    state: AcceptanceState
    aggregate_id: str | None = None
    revision: int | None = None
    snapshot_digest: str | None = None


class RecoveryVerdict(StrEnum):
    """What a strong re-read after an ambiguous acceptance response proves."""

    ACCEPTED = "accepted"
    """The exact bound evidence exists: the acceptance committed; never retry it."""
    RETRY = "retry"
    """Still `PENDING` with no member and no snapshot: retry against the current revision."""


def window_of(occurred_at: datetime) -> tuple[datetime, datetime]:
    """Return the fixed UTC window containing the instant."""
    moment = occurred_at.astimezone(UTC)
    start = moment.replace(minute=0, second=0, microsecond=0)
    return start, start + WINDOW


def _closed(dimensions: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    if set(dimensions) != set(DIMENSION_NAMES):
        raise MeasurementCorruptError("a measurement's dimensions are not the closed set.")
    if any(not dimensions[name] for name in DIMENSION_NAMES):
        raise MeasurementCorruptError("a measurement dimension is blank.")
    return tuple((name, dimensions[name]) for name in DIMENSION_NAMES)


def aggregate_id(  # noqa: PLR0913, PLR0917 -- the four parts of one identity
    family: str, window_start: datetime, window_end: datetime, dims: Sequence[tuple[str, str]]
) -> str:
    """Return `sha256(family/version, window, canonical closed dimensions)`."""
    return canonical_authority_digest(
        [
            f"{family}/v{METRIC_CONTRACT_REVISION}",
            window_start.astimezone(UTC).isoformat(),
            window_end.astimezone(UTC).isoformat(),
            [list(pair) for pair in dims],
        ]
    )


def measurement(  # noqa: PLR0913, PLR0917 -- the five facts one contribution is derived from
    source: str,
    family: str,
    dimensions: Mapping[str, str],
    value_ms: int,
    occurred_at: datetime,
) -> Measurement:
    """Derive one contribution; replaying the same event reproduces the same identity."""
    if family not in FAMILIES:
        raise MeasurementCorruptError("the measurement family is not declared.")
    closed = _closed(dimensions)
    identity = canonical_authority_digest(
        [
            "measurement/v1",
            source,
            f"{family}/v{METRIC_CONTRACT_REVISION}",
            [list(pair) for pair in closed],
        ]
    )
    return Measurement(identity, family, closed, value_ms, occurred_at.astimezone(UTC), source)


def snapshot_digest(aggregate: str, revision: int, statistics: Statistics) -> str:
    """Return `sha256(canonical_json(aggregate_id, revision, statistics, contract revision))`."""
    return canonical_authority_digest(
        [aggregate, revision, statistics.canonical(), METRIC_CONTRACT_REVISION]
    )


def next_snapshot(current: Aggregate, contribution: Measurement) -> Snapshot:
    """Return the snapshot one acceptance writes: revision `r+1` with exact `S + contribution`."""
    if current.aggregate_id != contribution.aggregate_id:
        raise MeasurementCorruptError("the contribution belongs to another aggregate.")
    statistics = current.statistics.add(contribution.value_ms)
    revision = current.revision + 1
    return Snapshot(
        aggregate_id=current.aggregate_id,
        revision=revision,
        statistics=statistics,
        measurement_id=contribution.measurement_id,
        digest=snapshot_digest(current.aggregate_id, revision, statistics),
    )


def validate_chain(snapshots: Sequence[Snapshot], start: int, current: Aggregate) -> None:
    """Prove a contiguous, digest-valid, monotonic chain from `start` to the current revision."""
    expected = list(range(start, current.revision + 1))
    if [item.revision for item in snapshots] != expected:
        raise MeasurementCorruptError("the snapshot chain is not contiguous.")
    previous: Statistics | None = None
    for item in snapshots:
        if item.digest != snapshot_digest(item.aggregate_id, item.revision, item.statistics):
            raise MeasurementCorruptError("a snapshot digest does not match its statistics.")
        if previous is not None and item.statistics.count != previous.count + 1:
            raise MeasurementCorruptError("the snapshot chain regressed or skipped a sample.")
        previous = item.statistics
    if snapshots and snapshots[-1].statistics != current.statistics:
        raise MeasurementCorruptError("the latest snapshot is not the aggregate's statistics.")


def recovery_verdict(  # noqa: PLR0913, PLR0917 -- the strong re-read's five pieces of evidence
    contribution: Measurement,
    acceptance: AcceptanceRecord,
    member_present: bool,
    bound_snapshot: Snapshot | None,
    current: Aggregate,
) -> RecoveryVerdict:
    """Decide from strongly read evidence whether an ambiguous acceptance committed.

    Exact bound identity, aggregate, revision, snapshot digest and that revision's statistics
    prove it committed, even if later work advanced the aggregate. `PENDING` with no member and
    no bound snapshot proves it did not. Anything else is corruption.
    """
    if acceptance.state is AcceptanceState.PENDING:
        if member_present or acceptance.revision is not None:
            raise MeasurementCorruptError("a pending measurement carries acceptance evidence.")
        return RecoveryVerdict.RETRY
    if (
        acceptance.aggregate_id != contribution.aggregate_id
        or acceptance.revision is None
        or not member_present
        or bound_snapshot is None
        or bound_snapshot.revision != acceptance.revision
        or bound_snapshot.measurement_id != contribution.measurement_id
        or bound_snapshot.digest != acceptance.snapshot_digest
        or bound_snapshot.digest
        != snapshot_digest(
            bound_snapshot.aggregate_id, bound_snapshot.revision, bound_snapshot.statistics
        )
        or current.revision < acceptance.revision
    ):
        raise MeasurementCorruptError("an accepted measurement's bound evidence does not match.")
    return RecoveryVerdict.ACCEPTED


@dataclass(frozen=True, slots=True)
class Gauge:
    """One published gauge: a name, its closed dimensions and the snapshot's value."""

    name: str
    dimensions: tuple[tuple[str, str], ...]
    value: int
    unit: str


def gauges(
    family: str, dims: Sequence[tuple[str, str]], statistics: Statistics
) -> tuple[Gauge, ...]:
    """Return every gauge one snapshot publishes: count, sum, maximum and each cumulative bucket."""
    base = tuple(dims)
    labels = (*(str(bound) for bound in BUCKETS_MS), INFINITY_BUCKET)
    return (
        Gauge(f"{family}Count", base, statistics.count, "Count"),
        Gauge(f"{family}SumMs", base, statistics.sum_ms, "Milliseconds"),
        Gauge(f"{family}MaxMs", base, statistics.max_ms, "Milliseconds"),
        *(
            Gauge(f"{family}Bucket", (*base, ("BucketLe", label)), count, "Count")
            for label, count in zip(labels, statistics.buckets, strict=True)
        ),
    )


def confirmed(expected: Sequence[Gauge], observed: Mapping[Gauge, float | None]) -> bool:
    """Return True only if every expected gauge was read back at least at its value.

    `observed` maps each expected gauge to the `Maximum` the sink returned for it, or None when
    the sink returned nothing. A later monotonic revision may satisfy an earlier one's values.
    """
    for gauge in expected:
        value = observed.get(gauge)
        if value is None or value < gauge.value:
            return False
    return True


def _elapsed_ms(start: str, end: str) -> int:
    """Whole milliseconds from one ISO instant to another, never negative across host clocks."""
    elapsed = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    return max(int(elapsed.total_seconds() * 1000), 0)


def _surface(authority_epoch: int) -> str:
    return "graph" if authority_epoch == 1 else "shadow"


def node_kind(node: str) -> str:
    """Return a node's template kind: `write_unit:<unit>` is a `write_unit` (S6 §2.10)."""
    kind = node.split(":", 1)[0]
    if kind not in GRAPH_NODE_KINDS:
        raise MeasurementCorruptError("a measured node is not a template node kind.")
    return kind


def node_measurement(  # noqa: PLR0913 -- one terminal node's closed fields
    *,
    authority_key: str,
    authority_epoch: int,
    node: str,
    state: str,
    armed_at: str | None,
    settled_at: str | None,
    scope: str = "live",
) -> Measurement | None:
    """Measure one terminal node as `FlowTransitionLatency` = `settled_at - armed_at` (S6 §2.10).

    `Stage` is the node kind and `Outcome` its terminal state. A node that was never armed (a
    PENDING successor superseded before its turn) or carries no settlement instant measures
    nothing. The authority key is the node item's, so a replayed terminal image reproduces the
    same contribution identity and never adds a second sample.
    """
    if armed_at is None or settled_at is None:
        return None
    return measurement(
        authority_key,
        "FlowTransitionLatency",
        {
            "Protocol": "v1",
            "Surface": _surface(authority_epoch),
            "Stage": node_kind(node),
            "Outcome": state,
            "AuthorityScope": scope,
        },
        _elapsed_ms(armed_at, settled_at),
        datetime.fromisoformat(settled_at),
    )


def claim_measurement(  # noqa: PLR0913 -- one admission's closed fields
    *,
    authority_key: str,
    authority_epoch: int,
    node: str,
    admission: int,
    admitted_at: str | None,
    queue_enqueued_at: str | None,
    scope: str = "live",
) -> Measurement | None:
    """Measure one claim admission as `ActivityAdmissionElapsed` (S6 §2.10, carried item 0a).

    The value is `admitted_at - queue_enqueued_at`. A RECLAIM keeps the first claim's
    `queue_enqueued_at`, so its sample includes the retry wait (full-load gate item 4). `Outcome`
    is `claim` for admission 1 and `reclaim` for each later one. The authority key carries the
    admission, so each admission is one contribution and a lease bump (which changes no admission
    field) reproduces an existing one.
    """
    if admitted_at is None or queue_enqueued_at is None:
        return None
    if admission < 1:
        raise MeasurementCorruptError("a measured claim has no admission.")
    return measurement(
        f"{authority_key}#{admission}",
        "ActivityAdmissionElapsed",
        {
            "Protocol": "v1",
            "Surface": _surface(authority_epoch),
            "Stage": node_kind(node),
            "Outcome": "claim" if admission == 1 else "reclaim",
            "AuthorityScope": scope,
        },
        _elapsed_ms(queue_enqueued_at, admitted_at),
        datetime.fromisoformat(admitted_at),
    )


def receipt_measurement(  # noqa: PLR0913 -- one receipt's closed fields
    *,
    authority_key: str,
    authority_epoch: int,
    decision: str,
    recorded_at: str,
    root_clock_started_at: str | None,
    scope: str = "live",
) -> Measurement | None:
    """Measure one run-request decision as `FlowRootElapsed` from its root clock (S5).

    The input is the committed receipt the status projection copies: the elapsed time from the
    manifest version's `LastModified` to the decision. A receipt with no root clock (a refusal
    decided before the manifest was read, or one written before S5) measures nothing. `Surface`
    names the authority (`shadow` for epoch 0, `graph` for epoch 1), so a shadow observation is
    never aggregated with graph authority (V-5).
    """
    if root_clock_started_at is None:
        return None
    started = datetime.fromisoformat(root_clock_started_at)
    decided = datetime.fromisoformat(recorded_at)
    # S3's and the Lambda's clocks can differ by milliseconds; a decision never precedes its root.
    elapsed = int((decided - started).total_seconds() * 1000)
    return measurement(
        authority_key,
        "FlowRootElapsed",
        {
            "Protocol": "v1",
            "Surface": "graph" if authority_epoch == 1 else "shadow",
            "Stage": "run_request_decided",
            "Outcome": decision,
            "AuthorityScope": scope,
        },
        max(elapsed, 0),
        decided,
    )
