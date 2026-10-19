# SPDX-License-Identifier: MIT-0
"""D2 option (b) source authority (plan A5.1): keyed pinned reads, the bound, the clock.

Each passing check runs beside a paired failing control.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import pytest

from adapters.outbound.bake_sources import (
    BAKE_FAN_OUT_FAILED,
    EXECUTIONS_OVER_CERTIFIED_BOUND,
    BakeClock,
    BakeClockMode,
    BakeQueryCapture,
    BakeReadMode,
    BakeRowHorizon,
    BakeSourceReads,
    PinnedRead,
    reads_digest,
)
from domain.operations.execution_graph_release import DurableBakeSourceManifestInvalidError
from domain.shared_kernel.errors import InvariantError
from ports.outbound.publication_selection import PinnedPublication
from ports.outbound.query import QueryResult
from ports.outbound.semantic_state import InspectExactResult

pytestmark = [pytest.mark.unit]

ROW = BakeRowHorizon(date(2026, 10, 14), 123)
"""A fictional row horizon."""


@dataclass(frozen=True)
class _Partition:
    identity: str


@dataclass(frozen=True)
class _Scope:
    identity: str


@dataclass
class _Publication:
    """A fictional successful publication: just what `_pin` and `publication_digest` read."""

    identity: str
    version: int = 1

    @property
    def partition(self) -> _Partition:
        return _Partition(self.identity)

    @property
    def publication_id(self) -> Any:
        return type("Id", (), {"value": f"{self.identity}@{self.version}"})()

    @property
    def fencing_token(self) -> int:
        return self.version


@dataclass
class _Current:
    """A fake CURRENT provider that counts every provider read; CURRENT can move."""

    version: int = 1
    reads: list[str] = field(default_factory=list[str])
    absent: set[str] = field(default_factory=set[str])
    gate: threading.Event | None = None

    def inspect_exact(self, partitions: tuple[_Partition, ...]) -> InspectExactResult:
        self.reads.append("inspect:" + ",".join(item.identity for item in partitions))
        if self.gate is not None:
            self.gate.wait(1)
        return InspectExactResult(
            tuple(
                None
                if item.identity in self.absent
                else cast("Any", _Publication(item.identity, self.version))
                for item in partitions
            )
        )

    def current_exact(self, partitions: tuple[_Partition, ...]) -> Any:
        self.reads.append("exact")
        return tuple(_Publication(item.identity, self.version) for item in partitions)

    def current_for_scope(self, scope: _Scope, horizon: Any = None) -> Any:
        del horizon
        self.reads.append(f"scope:{scope.identity}")
        if scope.identity == "empty":
            return ()
        return (_Publication(f"{scope.identity}-p", self.version),)

    def earliest_current_for_scope(self, scope: _Scope) -> Any:
        self.reads.append(f"earliest:{scope.identity}")
        return None if scope.identity == "none" else _Publication(f"{scope.identity}-e")


@dataclass
class _Exact:
    """Resolve pins back to fictional publications, as `select_exact` does."""

    def select_exact(self, request: Any) -> tuple[Any, ...]:
        return tuple(_Publication(pin.partition, pin.fencing_token) for pin in request.pins)


def _fake_digest(item: _Publication) -> str:
    return _digest_of(item.partition.identity, item.fencing_token)


@pytest.fixture(autouse=True)
def fake_publication_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Digest fake publications by identity and version (the real digest needs a row)."""
    import adapters.outbound.bake_sources as module  # noqa: PLC0415

    monkeypatch.setattr(module, "publication_digest", _fake_digest)


def _digest_of(name: str, version: int) -> str:
    return hashlib.sha256(f"{name}:{version}".encode()).hexdigest()


def _reads(current: _Current | None = None) -> tuple[BakeSourceReads, _Current]:
    provider = current or _Current()
    return BakeSourceReads(provider, cast("Any", _Exact())), provider


def _parts(*names: str) -> tuple[Any, ...]:
    return tuple(_Partition(name) for name in names)


# --- RECORD: one provider read per key, canonical recording ---------------------------------


def test_two_threads_missing_one_key_make_exactly_one_provider_read() -> None:
    gate = threading.Event()
    reads, current = _reads(_Current(gate=gate))
    with reads.snapshot(), reads.recording(ROW) as handle:
        threads = [
            threading.Thread(target=reads.inspect_exact, args=(_parts("a"),)) for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        gate.set()
        for thread in threads:
            thread.join()
    assert current.reads == ["inspect:a"]
    assert [read.kind for read in handle.reads] == ["inspect_exact"]


def test_a_cached_key_is_never_re_read_even_when_current_moves() -> None:
    """I3: the first answer is the pin; the deleted conflict rule's control."""
    reads, current = _reads()
    with reads.snapshot():
        with reads.recording(ROW):
            first = reads.current_exact(_parts("a", "b"))
        current.version = 2
        with reads.recording(ROW) as second:
            again = reads.current_exact(_parts("a", "b"))
            reads.inspect_exact(_parts("b"))
    assert again == first
    assert current.reads == ["inspect:a,b"]
    assert {read.kind for read in second.reads} == {"current_exact", "inspect_exact"}
    # Control: outside the snapshot the moved CURRENT is read again.
    assert reads.current_exact(_parts("a"))[0].fencing_token == 2  # type: ignore[index]


def test_recording_is_canonical_so_completion_order_never_moves_the_pin() -> None:
    def walk(order: tuple[str, ...]) -> str:
        reads, _ = _reads()
        with reads.snapshot(), reads.recording(ROW) as handle:
            for name in order:
                reads.inspect_exact(_parts(name))
                reads.inspect_exact(_parts(name))
        return reads_digest(handle.reads)

    assert walk(("a", "b", "c")) == walk(("c", "a", "b"))
    assert walk(("a", "b")) != walk(("a", "c"))


def test_an_unbounded_scope_read_is_refused_and_earliest_is_recorded() -> None:
    reads, current = _reads()
    with reads.snapshot(), reads.recording(ROW) as handle:
        with pytest.raises(InvariantError) as refused:
            reads.current_for_scope(cast("Any", _Scope("s")))
        assert refused.value.code == BAKE_FAN_OUT_FAILED
        assert reads.earliest_current_for_scope(cast("Any", _Scope("none"))) is None
        assert reads.earliest_current_for_scope(cast("Any", _Scope("s"))) is not None
    assert current.reads == ["earliest:none", "earliest:s"]
    assert [read.kind for read in handle.reads] == ["earliest_current_for_scope"] * 2


def test_a_recording_without_a_snapshot_and_nested_scopes_are_refused() -> None:
    reads, _ = _reads()
    with pytest.raises(InvariantError), reads.recording(ROW):
        pass
    with reads.snapshot(), pytest.raises(InvariantError), reads.snapshot():
        pass
    assert reads.mode is BakeReadMode.PASSTHROUGH


def test_a_failed_recording_restores_passthrough_and_pins_nothing() -> None:
    reads, current = _reads()
    with pytest.raises(RuntimeError), reads.snapshot(), reads.recording(ROW) as handle:
        reads.inspect_exact(_parts("a"))
        raise RuntimeError("bake failed")
    assert reads.mode is BakeReadMode.PASSTHROUGH
    assert [read.kind for read in handle.partial] == ["inspect_exact"]
    with pytest.raises(InvariantError):
        _ = handle.reads
    reads.inspect_exact(_parts("a"))
    assert current.reads == ["inspect:a", "inspect:a"]


# --- REPLAY: keyed, with the duplicate rule --------------------------------------------------


def _pin(name: str, version: int = 1) -> PinnedPublication:
    return PinnedPublication(name, f"{name}@{version}", version, _digest_of(name, version))


def test_a_reordered_and_repeated_replay_answers_equal_and_empty_reads_replay_typed() -> None:
    reads, current = _reads()
    with reads.snapshot(), reads.recording(ROW) as handle:
        reads.inspect_exact(_parts("a", "b"))
        horizon = ROW.window(1)
        reads.current_for_scope(cast("Any", _Scope("empty")), horizon)
        reads.earliest_current_for_scope(cast("Any", _Scope("none")))
    seen = len(current.reads)
    with reads.replaying(tuple(reversed(handle.reads)), ROW):
        assert reads.current_exact(_parts("b", "a")) is not None  # derived from slots
        assert reads.current_for_scope(cast("Any", _Scope("empty")), horizon) == ()
        assert reads.earliest_current_for_scope(cast("Any", _Scope("none"))) is None
        assert reads.inspect_exact(_parts("a", "b")) == reads.inspect_exact(_parts("a", "b"))
        with pytest.raises(DurableBakeSourceManifestInvalidError):
            reads.inspect_exact(_parts("c"))  # control: an unpinned partition
        with pytest.raises(DurableBakeSourceManifestInvalidError):
            reads.earliest_current_for_scope(cast("Any", _Scope("other")))
    assert len(current.reads) == seen  # I1: zero provider reads in replay
    assert reads.mode is BakeReadMode.PASSTHROUGH


def test_the_duplicate_rule_first_wins_equal_ignored_conflicting_fatal() -> None:
    reads, current = _reads()
    equal = (
        PinnedRead("inspect_exact", ("a",), (_pin("a"),)),
        PinnedRead("inspect_exact", ("a",), (_pin("a"),)),
    )
    with reads.replaying(equal, ROW):
        assert reads.inspect_exact(_parts("a")).publications[0] is not None
    conflicting = (
        PinnedRead("inspect_exact", ("a",), (_pin("a"),)),
        PinnedRead("inspect_exact", ("a",), (_pin("a", 2),)),
    )
    slot_conflict = (
        PinnedRead("inspect_exact", ("a",), (_pin("a"),)),
        PinnedRead("current_exact", ("a", "b"), (_pin("a", 2), _pin("b"))),
    )
    for stored in (conflicting, slot_conflict):
        with pytest.raises(DurableBakeSourceManifestInvalidError), reads.replaying(stored, ROW):
            pass
        assert reads.mode is BakeReadMode.PASSTHROUGH
    assert current.reads == []


# --- the certified bound (I5) ----------------------------------------------------------------


@dataclass
class _Inner:
    calls: list[Any] = field(default_factory=list[Any])

    def run(self, request: Any) -> QueryResult:
        self.calls.append(request)
        return cast("QueryResult", "rows")

    def run_series(self, request: Any) -> QueryResult:
        self.calls.append(request)
        return cast("QueryResult", "series-rows")


def _fake_key(request: object) -> str:
    return f"query:{request}"


@pytest.fixture
def fake_request_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Key fake string requests directly (the real digest needs a bound request)."""
    import adapters.outbound.bake_sources as module  # noqa: PLC0415

    monkeypatch.setattr(module, "request_key", _fake_key)


@pytest.mark.usefixtures("fake_request_keys")
def test_the_bound_plus_one_distinct_request_is_refused_before_it_executes() -> None:
    inner = _Inner()
    port = BakeQueryCapture(inner)
    with port.limited(2) as limit:
        assert port.run(cast("Any", "a")) == "rows"
        assert port.run(cast("Any", "a")) == "rows"  # a repeat is not counted again
        port.run(cast("Any", "b"))
        with pytest.raises(InvariantError) as refused:
            port.run(cast("Any", "c"))
        assert refused.value.code == EXECUTIONS_OVER_CERTIFIED_BOUND
        with pytest.raises(InvariantError):
            port.run(cast("Any", "d"))  # sticky: every later new request is refused
    assert inner.calls == ["a", "a", "b"]
    assert limit.result is not None
    assert (limit.result.distinct, limit.result.keys, limit.result.over_bound) == (
        2,
        ("query:a", "query:b"),
        True,
    )
    # Control: the same render at its bound n executes every request, and the scope is clear.
    with port.limited(3) as control:
        for name in ("a", "b", "c"):
            port.run(cast("Any", name))
    assert control.result is not None
    assert not control.result.over_bound
    assert port.run(cast("Any", "e")) == "rows"


@pytest.mark.parametrize("bound", [0, True, None, -1])
def test_a_render_without_a_valid_bound_is_refused_before_it_starts(bound: object) -> None:
    port = BakeQueryCapture(_Inner())
    with pytest.raises(InvariantError) as refused, port.limited(cast("int", bound)):
        pass
    assert refused.value.code == EXECUTIONS_OVER_CERTIFIED_BOUND


def test_capture_forbid_and_series_share_every_scope_and_always_restore() -> None:
    inner = _Inner()
    port = BakeQueryCapture(inner)
    with pytest.raises(RuntimeError), port.capturing_scope() as handle:
        port.run_series(cast("Any", "series"))
        with port.forbidden():
            with pytest.raises(InvariantError) as refused:
                port.run(cast("Any", "q"))
            assert refused.value.code == BAKE_FAN_OUT_FAILED
        raise RuntimeError("bind failed")
    assert handle.partial == ("series",)
    assert port.capturing is False
    assert port.run_series(cast("Any", "series")) == "series-rows"
    assert port.run(cast("Any", "q")) == "rows"
    with pytest.raises(InvariantError), port.capturing_scope(), port.capturing_scope():
        pass
    with port.limited(1), pytest.raises(InvariantError), port.forbidden():
        pass


# --- the pinned clock (I6) ------------------------------------------------------------------


def test_the_clock_is_fail_closed_pinned_and_legacy_wall_only_inside_its_scope() -> None:
    clock = BakeClock()
    with pytest.raises(InvariantError) as unset:
        clock()
    assert unset.value.code == BAKE_FAN_OUT_FAILED
    instant = datetime(2026, 10, 14, 23, 59, tzinfo=UTC)
    with clock.pinned(instant):
        assert clock() == instant
        with pytest.raises(InvariantError), clock.legacy_wall():
            pass
    assert clock.mode is BakeClockMode.UNSET
    before = datetime.now(UTC)
    with clock.legacy_wall():
        stamped = clock()
    assert before <= stamped <= datetime.now(UTC) + timedelta(seconds=1)
    with pytest.raises(InvariantError), clock.pinned(datetime(2026, 10, 14)):  # noqa: DTZ001
        pass
    # Control: a clock with a wall-clock default would have answered outside every scope.
    with pytest.raises(InvariantError):
        clock()
