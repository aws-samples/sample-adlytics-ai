# SPDX-License-Identifier: MIT-0
"""Pin a winner's bake source reads once and replay them exactly in its tasks (plan 4.3).

Every CURRENT read a bake source makes goes through one reader per invocation: the coverage
and catalog reads (`current_for_scope`), the per-pack evidence inspection (`inspect_exact`) and
the selector (`current_exact`). Athena runs only through the invocation's admission-wrapped
`QueryPort`. Two mode-switchable wrappers at those two seams give the V3 bake its source
authority without changing any renderer:

- `BakeSourceReads` passes through for the legacy bake, **records** every read during the
  fan-out's one selection pass per winner (S5), and **replays** a task's recorded reads from
  the exact immutable records `select_exact` resolves (DB21). Replay never reads CURRENT: a
  read the manifest does not name, in another order or with another key, is the fatal
  `durable_bake_source_manifest_invalid`.
- `BakeQueryCapture` passes through, or **captures** every complete-bound `QueryRequest` and
  answers it with an empty result, so a source's own render becomes its pure bind step: the
  task's complete requests with zero Athena executions. During real execution it also exposes
  the measurement seam's non-overlapping query and answer-render boundaries to task tracing.
"""

from __future__ import annotations

import gzip
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, cast

from aws_lambda_powertools import Logger

from adapters.inbound.tool_handlers.measurement import (
    distinct_query_count,
    query_request_digest,
    series_request_digest,
)
from adapters.outbound.publication_selection import publication_digest
from adapters.outbound.telemetry.reporting import LogSeverity, OperationLog, report_operation
from domain.operations.execution_graph_release import (
    BAKE_EXECUTIONS_OVER_CERTIFIED_BOUND,
    BakeTaskSource,
    DurableBakeSourceManifestInvalidError,
    bake_v3_digest,
)
from domain.shared_kernel.errors import ErrorCode, InvariantError
from domain.shared_kernel.provenance import Suppression
from ports.outbound.publication_selection import (
    PinnedPublication,
    PublicationSelectionExactPort,
    PublicationSelectionExactRequest,
)
from ports.outbound.query import FoldRead, PeriodSeriesRequest, QueryRequest, QueryResult, RowSink
from ports.outbound.semantic_state import InspectExactResult, ScopeHorizon

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from contextlib import AbstractContextManager
    from datetime import date

    from domain.semantics.publication import (
        PartitionIdentity,
        PublicationScope,
        SuccessfulPublication,
    )

__all__ = [
    "BAKE_FAN_OUT_FAILED",
    "BAKE_READS_NAMESPACE",
    "EXECUTIONS_OVER_CERTIFIED_BOUND",
    "BakeClock",
    "BakeClockMode",
    "BakeQueryCapture",
    "BakeReadMode",
    "BakeRowHorizon",
    "BakeSourceReads",
    "CapturedRequests",
    "LimitResult",
    "PinnedRead",
    "RecordedReads",
    "bake_request_digest",
    "decode_reads",
    "encode_reads",
    "reads_digest",
    "request_key",
    "source_closed",
    "task_source",
]

BAKE_READS_NAMESPACE: Final = "graph-durable-bake-reads/v1"
_INSPECT: Final = "inspect_exact"
_SCOPE: Final = "current_for_scope"
_EXACT: Final = "current_exact"
_EARLIEST: Final = "earliest_current_for_scope"
BAKE_FAN_OUT_FAILED: Final = ErrorCode("GRAPH_BAKE_V3", 10)
"""The fan-out's fatal invariant code (`graph_bake.BAKE_FAN_OUT_FAILED`)."""
UNBOUNDED_SCOPE_READ: Final = BAKE_FAN_OUT_FAILED
"""`BAKE_FAN_OUT_FAILED`: a bake recorded a whole-scope read with no horizon (A5.1 I9)."""
EXECUTIONS_OVER_CERTIFIED_BOUND: Final = ErrorCode("GRAPH_BAKE_V3", 12)
"""A render's (bound+1)-th distinct request, refused before it starts (A5.1 I5)."""


@dataclass(frozen=True, slots=True)
class BakeRowHorizon:
    """One roster row's declared source horizon, anchored on the run-pinned instant (I6, I9)."""

    anchor: date
    """The run's `compute_started_at` date: the same in the fan-out and every task attempt."""
    days: int
    """The row's generated `source_horizon_days`."""

    def __post_init__(self) -> None:
        if isinstance(self.days, bool) or self.days < 1:
            raise ValueError("a bake row's source horizon is at least one day.")

    def window(self, longest_bucket_days: int) -> ScopeHorizon:
        """Return the windowed horizon, shifted back by the pack's longest bucket (overlap rule)."""
        if isinstance(longest_bucket_days, bool) or longest_bucket_days < 1:
            raise ValueError("a pack's longest bucket is at least one day.")
        return ScopeHorizon(
            from_bucket_start=self.anchor - timedelta(days=self.days + longest_bucket_days)
        )


class BakeReadMode(StrEnum):
    """Which authority a bake source's CURRENT reads use in this phase."""

    PASSTHROUGH = "passthrough"
    RECORD = "record"
    REPLAY = "replay"


@dataclass(frozen=True, slots=True)
class PinnedRead:
    """One recorded source read: its kind, its exact key and the publications it returned.

    `pins` is None only for a `current_exact` that found a slot absent; an `inspect_exact`
    slot that was absent is a None entry inside `pins`.
    """

    kind: str
    key: tuple[str, ...]
    pins: tuple[PinnedPublication | None, ...] | None

    def to_json(self) -> list[object]:
        """Return the canonical JSON form stored in the manifest."""
        return [
            self.kind,
            list(self.key),
            None
            if self.pins is None
            else [
                None
                if pin is None
                else [pin.partition, pin.publication_id, pin.fencing_token, pin.digest]
                for pin in self.pins
            ],
        ]

    @classmethod
    def from_json(cls, value: object) -> PinnedRead:
        """Parse one stored read; anything malformed is the fatal manifest refusal."""
        try:
            kind, key, pins = cast("list[Any]", value)
            parsed = (
                None
                if pins is None
                else tuple(
                    None
                    if pin is None
                    else PinnedPublication(str(pin[0]), str(pin[1]), int(pin[2]), str(pin[3]))
                    for pin in cast("list[Any]", pins)
                )
            )
            return cls(str(kind), tuple(str(item) for item in cast("list[Any]", key)), parsed)
        except TypeError, ValueError, IndexError:
            raise DurableBakeSourceManifestInvalidError("a stored read is malformed") from None


def reads_digest(reads: tuple[PinnedRead, ...]) -> str:
    """Return the canonical digest of a task's recorded reads."""
    return bake_v3_digest(BAKE_READS_NAMESPACE, [read.to_json() for read in reads])


def encode_reads(reads: tuple[PinnedRead, ...]) -> bytes:
    """Compress a task's reads for its `RUN#<run_id>/BAKE_SOURCE#` item (repetitive keys)."""
    text = json.dumps([read.to_json() for read in reads], separators=(",", ":"))
    return gzip.compress(text.encode(), mtime=0)


def decode_reads(blob: bytes, expected_digest: str) -> tuple[PinnedRead, ...]:
    """Decode stored reads and refuse any whose digest is not the pinned one."""
    try:
        decoded: object = json.loads(gzip.decompress(blob))
    except OSError, ValueError:
        raise DurableBakeSourceManifestInvalidError("stored reads do not decode") from None
    if not isinstance(decoded, list):
        raise DurableBakeSourceManifestInvalidError("stored reads are not a list")
    reads = tuple(PinnedRead.from_json(item) for item in cast("list[object]", decoded))
    if reads_digest(reads) != expected_digest:
        raise DurableBakeSourceManifestInvalidError("stored reads differ from their digest")
    return reads


def _pin(publication: SuccessfulPublication) -> PinnedPublication:
    return PinnedPublication(
        publication.partition.identity,
        publication.publication_id.value,
        publication.fencing_token,
        publication_digest(publication),
    )


_LOGGER = Logger(service="adlytics-preset-artifact-baker")


def source_closed(page: str, condition: str) -> None:
    """Report, once at composition, why a page's retained source stays closed; return None.

    The baker maps the None to the page's one generated `<page>_materialization_unavailable`
    reason (A5.4), so no closure reason other than the generated one reaches a V3 task.
    """
    report_operation(
        OperationLog(
            operation="preset_artifact.publish",
            component="preset_artifact_publisher.composition",
            phase="compose",
            outcome="preset_artifact_source_closed",
            severity=LogSeverity.WARNING,
            details={"page": page, "closure_condition": condition},
        ),
        _LOGGER,
    )


def bake_request_digest(request: QueryRequest | PeriodSeriesRequest) -> str:
    """Return a bound request's digest, dispatched by kind (A5.4).

    A `QueryRequest` keeps its stage-1 digest; a `PeriodSeriesRequest` has its own namespace, so
    the two kinds never collide.
    """
    if isinstance(request, PeriodSeriesRequest):
        return series_request_digest(request)
    return query_request_digest(request)


def request_key(request: QueryRequest | PeriodSeriesRequest) -> str:
    """Return the canonical request key `<kind>:<digest>`, kind in {query, series} (A5.1)."""
    kind = "series" if isinstance(request, PeriodSeriesRequest) else "query"
    return f"{kind}:{bake_request_digest(request)}"


def task_source(  # noqa: PLR0913, PLR0917 -- one roster row and what its dry render saw
    page: str,
    variant: str,
    reads: tuple[PinnedRead, ...],
    requests: tuple[QueryRequest | PeriodSeriesRequest, ...],
) -> BakeTaskSource:
    """Build one roster row's pinned slice from its recorded reads and captured requests.

    Requests run concurrently, so their digests are stored sorted: the pin names the request
    set and its distinct count, never a completion order. Exact query fan-in still refuses an
    equal digest over unequal requests.
    """
    distinct_query_count(tuple(item for item in requests if isinstance(item, QueryRequest)))
    digests = tuple(sorted(bake_request_digest(request) for request in requests))
    return BakeTaskSource(page, variant, reads_digest(reads), digests, len(set(digests)))


# --- pinned clock (A5.1 I6) -------------------------------------------------------------------


class BakeClockMode(StrEnum):
    """Which instant a bake source's clock answers: none, the run's, or the legacy wall."""

    UNSET = "unset"
    PINNED = "pinned"
    LEGACY_WALL = "legacy_wall"


@dataclass
class BakeClock:
    """The bake sources' one clock: fail-closed outside a pinned or legacy scope (A5.1 I6).

    The V3 fan-out and every task pin it to the run's `compute_started_at`, so every attempt
    resolves the same periods, stamps and expiries. Only the pre-cutover legacy render enters
    `legacy_wall()`. Called with no scope open it raises; there is no wall-clock fallback.
    """

    mode: BakeClockMode = BakeClockMode.UNSET
    _instant: datetime | None = None

    def __call__(self) -> datetime:
        """Return the pinned instant, or the wall clock inside the legacy render only."""
        if self.mode is BakeClockMode.PINNED and self._instant is not None:
            return self._instant
        if self.mode is BakeClockMode.LEGACY_WALL:
            return datetime.now(UTC)
        raise InvariantError(BAKE_FAN_OUT_FAILED, "a bake read the clock without a pinned instant.")

    @contextmanager
    def pinned(self, instant: datetime) -> Generator[None]:
        """Answer `instant`, an aware UTC datetime, until the scope exits."""
        if instant.tzinfo is None or instant.utcoffset() != timedelta(0):
            raise InvariantError(BAKE_FAN_OUT_FAILED, "a bake clock pins an aware UTC instant.")
        self._enter(BakeClockMode.PINNED)
        self._instant = instant
        try:
            yield
        finally:
            self.mode, self._instant = BakeClockMode.UNSET, None

    @contextmanager
    def legacy_wall(self) -> Generator[None]:
        """Answer the wall clock: the pre-cutover legacy render only (census-pinned caller)."""
        self._enter(BakeClockMode.LEGACY_WALL)
        try:
            yield
        finally:
            self.mode = BakeClockMode.UNSET

    def _enter(self, mode: BakeClockMode) -> None:
        if self.mode is not BakeClockMode.UNSET:
            raise InvariantError(BAKE_FAN_OUT_FAILED, "bake clock scopes do not nest.")
        self.mode = mode


# --- keyed pinned reads (A5.1 I1-I3) ----------------------------------------------------------


@dataclass
class RecordedReads:
    """One row's `recording()` handle: its canonical reads on normal exit only."""

    partial: tuple[PinnedRead, ...] = ()
    """The reads seen before an exception, for the failure report only; never pinned."""
    _reads: tuple[PinnedRead, ...] | None = None

    def seal(self, reads: tuple[PinnedRead, ...]) -> None:
        """Set the canonical reads once, on the recording's normal exit."""
        self._reads = reads

    @property
    def reads(self) -> tuple[PinnedRead, ...]:
        """The row's canonical reads; reading them after a failed recording is an invariant."""
        if self._reads is None:
            raise InvariantError(BAKE_FAN_OUT_FAILED, "a failed recording has no reads.")
        return self._reads


@dataclass
class _Snapshot:
    """The fan-out invocation's RECORD caches: each CURRENT key reaches the provider once."""

    slots: dict[str, SuccessfulPublication | None] = field(default_factory=lambda: {})
    scopes: dict[tuple[str, ...], tuple[SuccessfulPublication, ...]] = field(
        default_factory=lambda: {}
    )
    earliest: dict[str, SuccessfulPublication | None] = field(default_factory=lambda: {})
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass(frozen=True, slots=True)
class _Replay:
    """A task's read-only REPLAY indexes over its stored reads (keyed, no cursor)."""

    by_call: dict[tuple[str, tuple[str, ...]], PinnedRead]
    slots: dict[str, PinnedPublication | None]


def _replay_indexes(reads: tuple[PinnedRead, ...]) -> _Replay:
    """Index stored reads in stored order: first record wins, a differing duplicate is fatal."""
    by_call: dict[tuple[str, tuple[str, ...]], PinnedRead] = {}
    slots: dict[str, PinnedPublication | None] = {}
    for read in reads:
        existing = by_call.setdefault((read.kind, read.key), read)
        if existing.pins != read.pins:
            raise DurableBakeSourceManifestInvalidError(f"a {read.kind} read pins two answers")
        if read.kind not in {_INSPECT, _EXACT} or read.pins is None:
            continue
        if len(read.pins) != len(read.key):
            raise DurableBakeSourceManifestInvalidError(f"a {read.kind} read is misaligned")
        for partition, pin in zip(read.key, read.pins, strict=True):
            slot = slots.setdefault(partition, pin)
            if slot != pin:
                raise DurableBakeSourceManifestInvalidError("a partition slot pins two answers")
    return _Replay(by_call, slots)


@dataclass
class BakeSourceReads:
    """The bake sources' one CURRENT reader, switched per phase by the V3 bake (A5.1).

    RECORD (the fan-out, inside `snapshot()`): each CURRENT key reaches the provider at most
    once per invocation, its first answer is the pin, and every row records what it read.
    REPLAY (a task): every answer comes from the stored reads by key, through one
    `select_exact`; an unpinned key is the fatal manifest refusal. Order and repetition are free.
    """

    real: Any
    """The invocation's `DynamoCurrentPublications`."""
    exact: PublicationSelectionExactPort
    clock: BakeClock = field(default_factory=BakeClock)
    """The invocation's one bake clock: every source the factories compose reads it (I6)."""
    mode: BakeReadMode = BakeReadMode.PASSTHROUGH
    _recorded: list[PinnedRead] = field(default_factory=list[PinnedRead])
    _record_lock: threading.Lock = field(default_factory=threading.Lock)
    _snapshot: _Snapshot | None = None
    _replay: _Replay | None = None
    _resolved: dict[tuple[str, str], SuccessfulPublication] = field(default_factory=lambda: {})
    _row: BakeRowHorizon | None = None
    select_exact_calls: int = 0
    current_reads: int = 0
    """Reads that reached CURRENT through this wrapper (zero in replay, by construction)."""

    @contextmanager
    def snapshot(self) -> Generator[None]:
        """Create the fan-out's shared RECORD caches; drop them on exit."""
        if self._snapshot is not None or self.mode is not BakeReadMode.PASSTHROUGH:
            raise InvariantError(BAKE_FAN_OUT_FAILED, "bake read scopes do not nest.")
        self._snapshot = _Snapshot()
        try:
            yield
        finally:
            self._snapshot = None

    @contextmanager
    def recording(self, row: BakeRowHorizon) -> Generator[RecordedReads]:
        """Record one roster row's reads at its declared horizon; always restore PASSTHROUGH."""
        if self._snapshot is None or self.mode is not BakeReadMode.PASSTHROUGH:
            raise InvariantError(BAKE_FAN_OUT_FAILED, "a bake recording needs an open snapshot.")
        handle = RecordedReads()
        self.mode, self._recorded, self._row = BakeReadMode.RECORD, [], row
        try:
            yield handle
            handle.seal(self._canonical())
        except BaseException:
            handle.partial = tuple(self._recorded)
            raise
        finally:
            self.mode, self._recorded, self._row = BakeReadMode.PASSTHROUGH, [], None

    def _canonical(self) -> tuple[PinnedRead, ...]:
        """Deduplicate by (kind, key), lossless under I3, and sort, so order never moves a pin."""
        unique = {(read.kind, read.key): read for read in self._recorded}
        return tuple(unique[key] for key in sorted(unique))

    @contextmanager
    def replaying(self, reads: tuple[PinnedRead, ...], row: BakeRowHorizon) -> Generator[None]:
        """Answer a task's reads by key from its stored pins; always restore PASSTHROUGH."""
        if self.mode is not BakeReadMode.PASSTHROUGH or self._snapshot is not None:
            raise InvariantError(BAKE_FAN_OUT_FAILED, "bake read scopes do not nest.")
        indexes = _replay_indexes(reads)
        pins = tuple(
            {
                (pin.partition, pin.publication_id): pin
                for read in reads
                for pin in (read.pins or ())
                if pin is not None
            }.values()
        )
        self.select_exact_calls += 1
        resolved = self.exact.select_exact(PublicationSelectionExactRequest(pins))
        self._resolved = {
            (pin.partition, pin.publication_id): publication
            for pin, publication in zip(pins, resolved, strict=True)
        }
        self._replay, self._row, self.mode = indexes, row, BakeReadMode.REPLAY
        try:
            yield
        finally:
            self.done()

    def done(self) -> None:
        """Leave replay; a later CURRENT read passes through only outside the V3 bake."""
        if self.mode is BakeReadMode.REPLAY:
            self.mode = BakeReadMode.PASSTHROUGH
        self._replay = None
        self._resolved = {}
        if self.mode is BakeReadMode.PASSTHROUGH:
            self._row = None

    def window_horizon(self, longest_bucket_days: int) -> ScopeHorizon | None:
        """Return the row's windowed scope horizon inside a bake; None (unbounded) outside it."""
        if self.mode is BakeReadMode.PASSTHROUGH or self._row is None:
            return None
        return self._row.window(longest_bucket_days)

    def _publication(self, pin: PinnedPublication) -> SuccessfulPublication:
        return self._resolved[(pin.partition, pin.publication_id)]

    def _record(self, read: PinnedRead) -> None:
        with self._record_lock:
            self._recorded.append(read)

    def _replayed(self, kind: str, key: tuple[str, ...]) -> PinnedRead | None:
        return cast("_Replay", self._replay).by_call.get((kind, key))

    def _replayed_slots(self, key: tuple[str, ...]) -> tuple[PinnedPublication | None, ...]:
        slots = cast("_Replay", self._replay).slots
        if any(partition not in slots for partition in key):
            raise DurableBakeSourceManifestInvalidError("an unpinned partition read")
        return tuple(slots[partition] for partition in key)

    def _recorded_slots(
        self, partitions: tuple[PartitionIdentity, ...]
    ) -> tuple[SuccessfulPublication | None, ...]:
        """Answer exact slots from the snapshot; read only the missing ones (single-flight)."""
        snapshot = cast("_Snapshot", self._snapshot)
        with snapshot.lock:
            missing = tuple(
                dict.fromkeys(item for item in partitions if item.identity not in snapshot.slots)
            )
            if missing:
                self.current_reads += 1
                found = cast("InspectExactResult", self.real.inspect_exact(missing))
                for partition, publication in zip(missing, found.publications, strict=True):
                    snapshot.slots[partition.identity] = publication
            return tuple(snapshot.slots[item.identity] for item in partitions)

    def _passthrough(self, read: Callable[[], Any]) -> Any:
        self.current_reads += 1
        return read()

    def inspect_exact(self, partitions: tuple[PartitionIdentity, ...]) -> InspectExactResult:
        """Inspect exact CURRENT slots, or replay the pinned ones."""
        key = tuple(partition.identity for partition in partitions)
        if self.mode is BakeReadMode.REPLAY:
            read = self._replayed(_INSPECT, key)
            pins = read.pins or () if read is not None else self._replayed_slots(key)
            return InspectExactResult(
                tuple(None if pin is None else self._publication(pin) for pin in pins)
            )
        if self.mode is BakeReadMode.RECORD:
            slots = self._recorded_slots(partitions)
            self._record(
                PinnedRead(
                    _INSPECT, key, tuple(None if item is None else _pin(item) for item in slots)
                )
            )
            return InspectExactResult(slots)
        return self._passthrough(lambda: self.real.inspect_exact(partitions))

    def current_exact(
        self, partitions: tuple[PartitionIdentity, ...]
    ) -> tuple[SuccessfulPublication, ...] | None:
        """Select exact CURRENT publications, or replay the pinned ones."""
        key = tuple(partition.identity for partition in partitions)
        if self.mode is BakeReadMode.REPLAY:
            read = self._replayed(_EXACT, key)
            pins = read.pins if read is not None else self._replayed_slots(key)
            if pins is None or any(pin is None for pin in pins):
                return None
            return tuple(self._publication(cast("PinnedPublication", pin)) for pin in pins)
        if self.mode is BakeReadMode.RECORD:
            slots = self._recorded_slots(partitions)
            if any(item is None for item in slots):
                self._record(PinnedRead(_EXACT, key, None))
                return None
            found = tuple(cast("SuccessfulPublication", item) for item in slots)
            self._record(PinnedRead(_EXACT, key, tuple(_pin(item) for item in found)))
            return found
        return self._passthrough(lambda: self.real.current_exact(partitions))

    def current_for_scope(
        self, scope: PublicationScope, horizon: ScopeHorizon | None = None
    ) -> tuple[SuccessfulPublication, ...]:
        """Read a scope's CURRENT publications, or replay the pinned ones (A5.1 I9).

        The horizon token is part of the pinned key, so a replay at another horizon is the
        fatal manifest refusal. Recording a whole-scope read with no horizon is refused: an
        unbounded read never reaches a pin. A stage-1 pin's one-element key replays unbounded.
        """
        key = (scope.identity,) if horizon is None else (scope.identity, horizon.token)
        if self.mode is BakeReadMode.REPLAY:
            read = self._replayed(_SCOPE, key)
            if read is None:
                raise DurableBakeSourceManifestInvalidError(f"an unpinned {_SCOPE} read")
            return tuple(
                self._publication(cast("PinnedPublication", pin)) for pin in read.pins or ()
            )
        if self.mode is BakeReadMode.RECORD:
            if horizon is None:
                raise InvariantError(
                    UNBOUNDED_SCOPE_READ,
                    "an unbounded bake scope read",
                    context={"signal": "bake_unbounded_scope_read", "scope": scope.identity},
                )
            snapshot = cast("_Snapshot", self._snapshot)
            with snapshot.lock:
                if key not in snapshot.scopes:
                    self.current_reads += 1
                    snapshot.scopes[key] = tuple(self.real.current_for_scope(scope, horizon))
                found = snapshot.scopes[key]
            self._record(PinnedRead(_SCOPE, key, tuple(_pin(item) for item in found)))
            return found
        if horizon is None:
            return self._passthrough(lambda: self.real.current_for_scope(scope))
        return self._passthrough(lambda: self.real.current_for_scope(scope, horizon))

    def earliest_current_for_scope(self, scope: PublicationScope) -> SuccessfulPublication | None:
        """Read a scope's earliest CURRENT publication (already bounded), or replay its pin."""
        key = (scope.identity,)
        if self.mode is BakeReadMode.REPLAY:
            read = self._replayed(_EARLIEST, key)
            if read is None:
                raise DurableBakeSourceManifestInvalidError(f"an unpinned {_EARLIEST} read")
            pins = read.pins or ()
            return None if not pins or pins[0] is None else self._publication(pins[0])
        if self.mode is BakeReadMode.RECORD:
            snapshot = cast("_Snapshot", self._snapshot)
            with snapshot.lock:
                if scope.identity not in snapshot.earliest:
                    self.current_reads += 1
                    snapshot.earliest[scope.identity] = self.real.earliest_current_for_scope(scope)
                found = snapshot.earliest[scope.identity]
            self._record(PinnedRead(_EARLIEST, key, () if found is None else (_pin(found),)))
            return found
        return self._passthrough(lambda: self.real.earliest_current_for_scope(scope))

    def __getattr__(self, name: str) -> Any:
        """Pass any other reader method through outside the V3 bake; refuse it inside."""
        if name.startswith("_") or self.mode is not BakeReadMode.PASSTHROUGH:
            raise DurableBakeSourceManifestInvalidError(f"an unpinned {name} read")
        return getattr(self.real, name)


_EMPTY: Final = QueryResult(
    rows=(), bytes_scanned=0, truncated=False, suppression=Suppression(rows_suppressed=False)
)
_EMPTY_FOLD: Final = FoldRead(
    rows_read=0, truncated=False, bytes_scanned=0, suppression=Suppression(rows_suppressed=False)
)


@dataclass
class CapturedRequests:
    """One `capturing()` handle: the bind pass's requests on normal exit only."""

    partial: tuple[QueryRequest | PeriodSeriesRequest, ...] = ()
    _requests: tuple[QueryRequest | PeriodSeriesRequest, ...] | None = None

    def seal(self, requests: tuple[QueryRequest | PeriodSeriesRequest, ...]) -> None:
        """Set the captured requests once, on the capture's normal exit."""
        self._requests = requests

    @property
    def requests(self) -> tuple[QueryRequest | PeriodSeriesRequest, ...]:
        """The captured requests; reading them after a failed capture is an invariant."""
        if self._requests is None:
            raise InvariantError(BAKE_FAN_OUT_FAILED, "a failed capture has no requests.")
        return self._requests


@dataclass(frozen=True, slots=True)
class LimitResult:
    """A render's executed distinct requests under its certified bound (A5.1 I5)."""

    distinct: int
    keys: tuple[str, ...]
    """The bytewise-sorted canonical `<kind>:<digest>` keys."""
    over_bound: bool


@dataclass
class LimitHandle:
    """One `limited(n)` handle: its result once the scope has exited."""

    result: LimitResult | None = None


@dataclass
class BakeQueryCapture:
    """Capture bind requests, bound a render's distinct executions, expose phase boundaries.

    Implements `PeriodSeriesQueryPort`: `run` and `run_series` share every scope. Inside
    `capturing()` both record the request and answer empty; inside `forbidden()` (prefetch)
    both refuse; inside `limited(n)` the (n+1)-th distinct request is refused before it starts,
    and the refusal is sticky so a source that catches it still fails its task.
    """

    inner: Any
    """The invocation-shared admission (`AthenaBatchAdmission`), a `PeriodSeriesQueryPort`."""
    capturing: bool = False
    _captured: list[QueryRequest | PeriodSeriesRequest] = field(
        default_factory=list[QueryRequest | PeriodSeriesRequest]
    )
    _forbidden: bool = False
    _limit: int | None = None
    _executed: set[str] = field(default_factory=set[str])
    _over_bound: bool = False
    _observer: Callable[[str], AbstractContextManager[None]] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    executions: int = 0

    @contextmanager
    def capturing_scope(self) -> Generator[CapturedRequests]:
        """Start a bind pass: every request is recorded and answered empty; always restore."""
        if self.capturing:
            raise InvariantError(BAKE_FAN_OUT_FAILED, "bake query scopes do not nest.")
        handle = CapturedRequests()
        self.capturing, self._captured = True, []
        try:
            yield handle
            handle.seal(tuple(self._captured))
        except BaseException:
            handle.partial = tuple(self._captured)
            raise
        finally:
            self.capturing, self._captured = False, []

    @contextmanager
    def forbidden(self) -> Generator[None]:
        """Refuse every query (a CURRENT-only prefetch); always restore."""
        if self._forbidden or self._limit is not None:
            raise InvariantError(BAKE_FAN_OUT_FAILED, "bake query scopes do not nest.")
        self._forbidden = True
        try:
            yield
        finally:
            self._forbidden = False

    @contextmanager
    def limited(self, bound: int) -> Generator[LimitHandle]:
        """Admit at most `bound` distinct requests to execution; snapshot and clear on exit."""
        if isinstance(bound, bool) or not isinstance(bound, int) or bound < 1:  # pyright: ignore[reportUnnecessaryIsInstance]
            raise InvariantError(
                EXECUTIONS_OVER_CERTIFIED_BOUND,
                "a bake render needs a certified bound of at least one request.",
                context={"signal": BAKE_EXECUTIONS_OVER_CERTIFIED_BOUND},
            )
        if self._forbidden or self._limit is not None or self.capturing:
            raise InvariantError(BAKE_FAN_OUT_FAILED, "bake query scopes do not nest.")
        handle = LimitHandle()
        self._limit, self._executed, self._over_bound = bound, set(), False
        try:
            yield handle
        finally:
            with self._lock:
                handle.result = LimitResult(
                    len(self._executed), tuple(sorted(self._executed)), self._over_bound
                )
                self._limit, self._executed, self._over_bound = None, set(), False

    def observe(self, observer: Callable[[str], AbstractContextManager[None]]) -> None:
        """Attach the current task's non-overlapping query/render phase recorder."""
        self._observer = observer

    def observed(self) -> None:
        """Detach the task recorder before another invocation phase uses this port."""
        self._observer = None

    @contextmanager
    def phase(self, name: str) -> Generator[None]:
        """Record a real execution boundary; bind capture remains inside the plan phase only."""
        observer = None if self.capturing else self._observer
        if observer is None:
            yield
            return
        with observer(name):
            yield

    def _admit(self, request: QueryRequest | PeriodSeriesRequest) -> bool:
        """Return True to answer empty (bind capture); refuse a forbidden or over-bound request."""
        if self._forbidden:
            raise InvariantError(BAKE_FAN_OUT_FAILED, "a bake prefetch issued a query.")
        if self.capturing:
            with self._lock:
                self._captured.append(request)
            return True
        with self._lock:
            if self._limit is not None:
                key = request_key(request)
                if key not in self._executed:
                    if len(self._executed) >= self._limit:
                        self._over_bound = True
                        raise InvariantError(
                            EXECUTIONS_OVER_CERTIFIED_BOUND,
                            "a bake render exceeded its certified request bound.",
                            context={"signal": BAKE_EXECUTIONS_OVER_CERTIFIED_BOUND},
                        )
                    self._executed.add(key)
            self.executions += 1
        return False

    def run(self, request: QueryRequest) -> QueryResult:
        """Capture the request, or run it through the admission-wrapped port."""
        if self._admit(request):
            return _EMPTY
        return self.inner.run(request)

    def run_series(self, request: PeriodSeriesRequest) -> QueryResult:
        """Capture the series request, or run it through the same admission (A5.4)."""
        if self._admit(request):
            return _EMPTY
        return self.inner.run_series(request)

    def fold(self, request: QueryRequest, sink: RowSink) -> FoldRead:
        """Capture the request (its sink gets no rows), or stream it through the admission."""
        if self._admit(request):
            return _EMPTY_FOLD
        return self.inner.fold(request, sink)

    def fold_series(self, request: PeriodSeriesRequest, sink: RowSink) -> FoldRead:
        """Capture the series request (no rows), or stream it through the same admission."""
        if self._admit(request):
            return _EMPTY_FOLD
        return self.inner.fold_series(request, sink)
