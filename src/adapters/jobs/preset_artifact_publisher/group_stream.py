# SPDX-License-Identifier: MIT-0
"""Group-streamed reads: one entity's figures at a time, handed across a bounded queue (ADR-152).

An entity-wide page (Audiences: every DSP campaign) reads each figure `ORDER BY` its grain and
renders one entity at a time, so it never holds the population. Each read runs as an ordinary
presentation measurement slot with a `reduction`: `_folded_group` streams it through the query
port's `fold` into `FoldingAnswer`, the existing `RowSink`, which turns each row into a figure by
the held rule. The fold it feeds is a `GroupSink`: it keeps the current group's figures and, when
the key changes, puts the completed group on a one-slot queue. `GroupStreams` runs one thread per
read and merges their groups by key (`domain.measurement.groups`).

Memory is at most three groups per read (the one being built, the one queued, the consumer's).
No wait is unbounded: every `put` and `get` waits in `POLL_SECONDS` slices and re-checks the
cancel flag and the producer thread. Closing the consumer, or dropping a stream nobody drew,
sets the flag; each sink then raises on its next figure, and the threads end.

Every read must run at once, because the merge needs each read's next group. A fold holds its
query admission slot for its whole stream, so the caller's query concurrency must admit every
read of a merge (`AudiencesArtifactRenderer` checks it at construction).
"""

from __future__ import annotations

import threading
import weakref
from collections.abc import Callable, Generator, Iterator
from contextvars import copy_context
from dataclasses import dataclass, field
from queue import Empty, Full, Queue
from typing import TYPE_CHECKING, Final

from adapters.inbound.tool_handlers.measurement import (
    MeasurementExecutionTooling,
    PreparedMeasurementBatch,
    PresentationMeasurementAnswerSlot,
    execute_presentation_batch,
)
from domain.measurement.groups import KeyedGroup, MergedGroup, merge_ordered_groups
from domain.shared_kernel.errors import ErrorCode, InvariantError

if TYPE_CHECKING:  # pragma: no cover
    from domain.measurement.figures import Figure
    from ports.outbound.publication_selection import PresentationEvidenceInspectionResult
    from ports.outbound.query import (
        FoldingQueryPort,
        FoldRead,
        PeriodSeriesRequest,
        QueryRequest,
        QueryResult,
        RowSink,
    )

__all__ = [
    "GROUP_STREAM_FAILED",
    "POLL_SECONDS",
    "RESIDENT_GROUPS",
    "GroupRead",
    "GroupSink",
    "GroupStreamCancelledError",
    "GroupStreams",
    "SinkSlot",
    "Unphased",
]

POLL_SECONDS: Final = 0.5
"""How long one `put` or `get` waits before re-checking cancellation and its producer."""
RESIDENT_GROUPS: Final = 3
"""Groups of one read resident at once: the one being built, the one queued, the consumer's."""
GROUP_STREAM_FAILED: Final = ErrorCode("GROUP_STREAM", 1)
"""A group-streamed read ended without its end marker or error: a defect."""


class GroupStreamCancelledError(Exception):
    """Raised inside a producer once its consumer is gone; never reaches a caller."""


@dataclass(frozen=True, slots=True)
class _Exhausted:
    """The end marker a sink puts after its read's last group."""


_EXHAUSTED: Final = _Exhausted()


class GroupSink:
    """The fold a group-streamed read feeds: one group at a time onto a one-slot queue.

    `max_kept` bounds one group's figures (the largest entity); a group past it marks the
    sink overflowed and keeps nothing more, so the read is truncated and refuses as one.
    """

    def __init__(
        self, key_columns: tuple[str, ...], max_kept: int, cancel: threading.Event
    ) -> None:
        if not key_columns:
            raise ValueError("a group sink keys by at least one column.")
        if isinstance(max_kept, bool) or max_kept < 1:
            raise ValueError("a group sink keeps at least one figure per group.")
        self._columns = key_columns
        self._max_kept = max_kept
        self._cancel = cancel
        self._queue: Queue[KeyedGroup[Figure] | _Exhausted] = Queue(maxsize=1)
        self._key: tuple[str, ...] | None = None
        self._members: list[Figure] = []
        self._overflowed = False
        self._groups = 0
        self.started = threading.Event()
        """Set by the first figure or the read's end: its statement has executed."""

    @property
    def max_kept(self) -> int:
        """The most figures one group keeps."""
        return self._max_kept

    @property
    def overflowed(self) -> bool:
        """Whether one group had more figures than `max_kept`."""
        return self._overflowed

    def add(self, figure: Figure) -> None:
        """Keep the figure in its group; hand the previous group on when the key changes."""
        self.started.set()
        if self._cancel.is_set():
            raise GroupStreamCancelledError
        dimensions = dict(figure.dimensions)
        key = tuple(dimensions[column] for column in self._columns)
        if key != self._key:
            self._flush()
            self._key = key
        if len(self._members) >= self._max_kept:
            self._overflowed = True
            return
        self._members.append(figure)

    def value(self) -> int:
        """End the read: hand on its last group and the end marker; return the groups handed on.

        `FoldingAnswer.finish` calls this once, after the read's last row.
        """
        self._flush()
        self._put(_EXHAUSTED)
        self.started.set()
        return self._groups

    def _flush(self) -> None:
        if self._key is not None and self._members:
            self._put(KeyedGroup(self._key, tuple(self._members)))
            self._groups += 1
        self._key, self._members = None, []

    def _put(self, item: KeyedGroup[Figure] | _Exhausted) -> None:
        while True:
            if self._cancel.is_set():
                raise GroupStreamCancelledError
            try:
                self._queue.put(item, timeout=POLL_SECONDS)
            except Full:
                continue
            return

    def take(self, alive: Callable[[], bool]) -> KeyedGroup[Figure] | _Exhausted | None:
        """Take the next item, or None once the producer ended without putting one."""
        while True:
            try:
                return self._queue.get(timeout=POLL_SECONDS)
            except Empty:
                if not alive():
                    try:
                        return self._queue.get_nowait()
                    except Empty:
                        return None


@dataclass(slots=True)
class SinkSlot:
    """A read's `reduction` factory: the sink of the stream currently running it.

    One prepared read can be streamed twice (a count pass, then a page pass); each start puts
    a fresh sink here before its thread runs, so `_folded_group` hands each execution its own.
    `max_kept` is what the memory model counts for the read: `RESIDENT_GROUPS` groups of at most
    `group_max_kept` figures.
    """

    group_max_kept: int
    sink: GroupSink | None = None

    @property
    def max_kept(self) -> int:
        """The most figures of this read resident at once."""
        return RESIDENT_GROUPS * self.group_max_kept

    def __call__(self) -> GroupSink:
        """Return the running stream's sink."""
        if self.sink is None:
            raise InvariantError(GROUP_STREAM_FAILED, "A group-streamed read ran with no sink.")
        return self.sink


@dataclass(frozen=True, slots=True)
class GroupRead:
    """One prepared, inspected read whose one request reduces through `slot`."""

    prepared: PreparedMeasurementBatch
    inspection: PresentationEvidenceInspectionResult
    slot: SinkSlot

    def __post_init__(self) -> None:
        if len(self.prepared.plans) != 1:
            raise ValueError("a group-streamed read is one prepared plan.")


@dataclass(slots=True)
class _Outcome:
    """What one producer thread ended with: its executed slot, or its error."""

    slot: PresentationMeasurementAnswerSlot | None = None
    error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class Unphased:
    """A folding query port without a task phase: a stream drained by the stage, not the render.

    A query phase opened by a read the stage drains would stay open into the write phase, so a
    page pass reads through this, and its time is the write's.
    """

    queries: FoldingQueryPort

    def run(self, request: QueryRequest) -> QueryResult:
        """Run one plan."""
        return self.queries.run(request)

    def run_series(self, request: PeriodSeriesRequest) -> QueryResult:
        """Run one period series."""
        return self.queries.run_series(request)

    def fold(self, request: QueryRequest, sink: RowSink) -> FoldRead:
        """Stream one plan through `sink`."""
        return self.queries.fold(request, sink)

    def fold_series(self, request: PeriodSeriesRequest, sink: RowSink) -> FoldRead:
        """Stream one period series through `sink`."""
        return self.queries.fold_series(request, sink)


def _produce(  # noqa: PLR0913, PLR0917 -- one thread's read, tooling, outcome and sink
    read: GroupRead, tooling: MeasurementExecutionTooling, outcome: _Outcome, sink: GroupSink
) -> None:
    try:
        outcome.slot = execute_presentation_batch(read.prepared, read.inspection, tooling).slots[0]
    except GroupStreamCancelledError:
        return
    # error-propagation: held and re-raised in the consumer, which the render or stage owns
    except BaseException as error:
        outcome.error = error
    finally:
        sink.started.set()


@dataclass
class GroupStreams:
    """Run every read of one merge at once and yield one key's groups from all of them."""

    reads: tuple[GroupRead, ...]
    tooling: MeasurementExecutionTooling
    key_columns: tuple[str, ...]
    _cancel: threading.Event = field(default_factory=threading.Event)
    _sinks: tuple[GroupSink, ...] = ()
    _threads: tuple[threading.Thread, ...] = ()
    _outcomes: tuple[_Outcome, ...] = ()

    def __post_init__(self) -> None:
        if not self.reads:
            raise ValueError("a group stream merges at least one read.")
        if self.tooling.query_batch_max_concurrency < len(self.reads):
            raise ValueError("a group stream runs every read at once: raise the query bound.")

    def start(self) -> None:
        """Start one producer per read; a stream nobody draws is cancelled when it is dropped."""
        if self._threads:
            raise InvariantError(GROUP_STREAM_FAILED, "A group stream starts once.")
        sinks: list[GroupSink] = []
        threads: list[threading.Thread] = []
        outcomes: list[_Outcome] = []
        for read in self.reads:
            sink = GroupSink(self.key_columns, read.slot.group_max_kept, self._cancel)
            read.slot.sink = sink
            outcome = _Outcome()
            context = copy_context()
            thread = threading.Thread(
                target=context.run,
                args=(_produce, read, self.tooling, outcome, sink),
                name="group-stream",
                daemon=True,
            )
            sinks.append(sink)
            threads.append(thread)
            outcomes.append(outcome)
        self._sinks, self._threads, self._outcomes = tuple(sinks), tuple(threads), tuple(outcomes)
        weakref.finalize(self, self._cancel.set)
        for thread in self._threads:
            thread.start()

    def wait_started(self) -> None:
        """Return once every read's statement has executed; raise the first producer error.

        A read has executed once its sink took a figure or its read ended, so the caller (the
        render) owns every statement's deadline and certified bound.
        """
        for sink, thread in zip(self._sinks, self._threads, strict=True):
            while not sink.started.wait(POLL_SECONDS):
                if not thread.is_alive():
                    break
        self._raise_error()

    def cancel(self) -> None:
        """Stop every producer and wait for each to end."""
        self._cancel.set()
        for thread in self._threads:
            thread.join()

    def groups(self) -> Generator[MergedGroup[Figure]]:
        """Merge every read's groups by key; cancel and join the producers however it ends."""
        if not self._threads:
            raise InvariantError(GROUP_STREAM_FAILED, "A group stream is drawn after it starts.")
        try:
            yield from merge_ordered_groups(
                tuple(self._drain(index) for index in range(len(self.reads)))
            )
        finally:
            self.cancel()
        self._raise_error()

    def slots(self) -> tuple[PresentationMeasurementAnswerSlot, ...]:
        """Each read's executed slot (its metadata), once `groups()` is exhausted."""
        if any(outcome.slot is None for outcome in self._outcomes):
            raise InvariantError(GROUP_STREAM_FAILED, "A group-streamed read has no answer.")
        return tuple(outcome.slot for outcome in self._outcomes if outcome.slot is not None)

    def _drain(self, index: int) -> Iterator[KeyedGroup[Figure]]:
        sink, thread = self._sinks[index], self._threads[index]
        while True:
            item = sink.take(thread.is_alive)
            if isinstance(item, KeyedGroup):
                yield item
                continue
            if item is None:
                thread.join()
                self._raise_error()
                if self._outcomes[index].slot is not None:
                    return  # a slot with no CURRENT publication: answered absent, never read
                raise InvariantError(
                    GROUP_STREAM_FAILED, "A group-streamed read ended without its last group."
                )
            return

    def _raise_error(self) -> None:
        error = next((item.error for item in self._outcomes if item.error is not None), None)
        if error is not None:
            raise error
