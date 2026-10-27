# SPDX-License-Identifier: MIT-0
"""Group-streamed reads: boundaries, the merge, cancellation and worker errors (ADR-152)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest

from adapters.jobs.preset_artifact_publisher import group_stream
from adapters.jobs.preset_artifact_publisher.group_stream import (
    GroupRead,
    GroupSink,
    GroupStreams,
    SinkSlot,
)
from domain.measurement.figures import Figure
from domain.measurement.groups import GROUPS_OUT_OF_ORDER, KeyedGroup, merge_ordered_groups
from domain.shared_kernel.errors import InvariantError

pytestmark = pytest.mark.unit

_KEY = ("marketplace_key", "campaign_key")


def _figure(campaign: str, segment: str, value: int = 1) -> Figure:
    return Figure(
        dimensions=(
            ("marketplace_key", "US"),
            ("campaign_key", campaign),
            ("segment_id", segment),
        ),
        value=Decimal(value),
    )


type _Script = Callable[[GroupSink], None]


def _read(script: _Script) -> GroupRead:
    slot = SinkSlot(10)
    prepared = cast("Any", SimpleNamespace(plans=(object(),), script=script))
    return GroupRead(prepared, cast("Any", None), slot)


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> list[GroupRead]:
    """Run each read's script against its sink, as `_folded_group` feeds a fold."""
    reads: list[GroupRead] = []

    def execute(prepared: Any, inspection: object, tooling: object) -> Any:
        del inspection, tooling
        read = next(item for item in reads if item.prepared is prepared)
        sink = read.slot()
        prepared.script(sink)
        sink.value()
        return SimpleNamespace(slots=(f"slot:{reads.index(read)}",))

    monkeypatch.setattr(group_stream, "execute_presentation_batch", execute)
    return reads


def _streams(reads: list[GroupRead], scripts: tuple[_Script, ...]) -> GroupStreams:
    reads.extend(_read(script) for script in scripts)
    tooling = cast("Any", SimpleNamespace(query_batch_max_concurrency=len(scripts)))
    return GroupStreams(tuple(reads), tooling, _KEY)


def _feeding(*figures: Figure) -> _Script:
    def script(sink: GroupSink) -> None:
        for figure in figures:
            sink.add(figure)

    return script


def _keys(streams: GroupStreams) -> list[tuple[tuple[str, ...], tuple[int, ...]]]:
    streams.start()
    return [(item.key, tuple(len(group) for group in item.groups)) for item in streams.groups()]


def test_first_last_and_middle_groups_reach_the_merge_whole(scripted: list[GroupRead]) -> None:
    streams = _streams(
        scripted,
        (
            _feeding(_figure("a", "1"), _figure("a", "2"), _figure("b", "1"), _figure("c", "9")),
            _feeding(_figure("a", "1"), _figure("c", "9"), _figure("c", "8")),
        ),
    )

    assert _keys(streams) == [
        (("US", "a"), (2, 1)),
        (("US", "b"), (1, 0)),
        (("US", "c"), (1, 2)),
    ]
    assert streams.slots() == ("slot:0", "slot:1")


def test_one_campaign_and_an_empty_read(scripted: list[GroupRead]) -> None:
    streams = _streams(scripted, (_feeding(_figure("only", "1")), _feeding()))

    assert _keys(streams) == [(("US", "only"), (1, 0))]


def test_every_read_empty_yields_no_group(scripted: list[GroupRead]) -> None:
    assert _keys(_streams(scripted, (_feeding(), _feeding()))) == []


def test_a_key_present_in_only_one_read_merges_with_empty_groups(
    scripted: list[GroupRead],
) -> None:
    streams = _streams(
        scripted,
        (_feeding(_figure("a", "1")), _feeding(_figure("b", "1")), _feeding(_figure("c", "1"))),
    )

    assert _keys(streams) == [
        (("US", "a"), (1, 0, 0)),
        (("US", "b"), (0, 1, 0)),
        (("US", "c"), (0, 0, 1)),
    ]


def test_ordering_is_by_code_point_as_trino_orders_varchar() -> None:
    keys = ("B", "a", "Z", "é", "ä", "1")
    ordered = sorted(keys)
    stream = (KeyedGroup((key,), (key,)) for key in ordered)

    assert [item.key[0] for item in merge_ordered_groups((stream,))] == [
        "1",
        "B",
        "Z",
        "a",
        "ä",
        "é",
    ]


def test_a_key_out_of_order_is_a_defect_not_a_reorder() -> None:
    stream = (KeyedGroup((key,), ()) for key in ("b", "a"))

    with pytest.raises(InvariantError) as raised:
        list(merge_ordered_groups((stream,)))
    assert raised.value.code == GROUPS_OUT_OF_ORDER


def test_a_group_past_max_kept_overflows_and_keeps_nothing_more() -> None:
    sink = GroupSink(_KEY, 2, threading.Event())
    for segment in ("1", "2", "3"):
        sink.add(_figure("a", segment))
    producer = threading.Thread(target=sink.value, daemon=True)
    producer.start()

    group = sink.take(producer.is_alive)
    assert sink.take(producer.is_alive) is not None
    producer.join(timeout=5)
    assert isinstance(group, KeyedGroup)
    assert len(group.members) == 2
    assert sink.overflowed


def test_closing_mid_group_cancels_every_producer_and_joins_them(
    scripted: list[GroupRead],
) -> None:
    def endless(campaign_prefix: str) -> _Script:
        def script(sink: GroupSink) -> None:
            for index in range(10_000):
                sink.add(_figure(f"{campaign_prefix}{index:05d}", "1"))

        return script

    streams = _streams(scripted, (endless("a"), endless("a")))
    streams.start()
    merged = streams.groups()
    first = next(merged)
    assert first.key == ("US", "a00000")
    started = time.monotonic()
    merged.close()

    assert all(not thread.is_alive() for thread in streams._threads)  # pyright: ignore[reportPrivateUsage]
    assert time.monotonic() - started < 5


def test_a_stream_nobody_draws_is_cancelled_when_dropped(scripted: list[GroupRead]) -> None:
    def endless(sink: GroupSink) -> None:
        for index in range(10_000):
            sink.add(_figure(f"a{index:05d}", "1"))

    streams = _streams(scripted, (endless,))
    streams.start()
    streams.wait_started()
    threads = streams._threads  # pyright: ignore[reportPrivateUsage]
    del streams
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)


def test_a_worker_error_reaches_the_consumer(scripted: list[GroupRead]) -> None:
    class _BrokenError(RuntimeError):
        pass

    def broken(sink: GroupSink) -> None:
        sink.add(_figure("a", "1"))
        raise _BrokenError

    def endless(sink: GroupSink) -> None:
        for index in range(10_000):
            sink.add(_figure(f"a{index:05d}", "1"))

    streams = _streams(scripted, (endless, broken))
    streams.start()

    with pytest.raises(_BrokenError):
        list(streams.groups())
    assert all(not thread.is_alive() for thread in streams._threads)  # pyright: ignore[reportPrivateUsage]


def test_an_error_before_the_first_figure_surfaces_when_waiting_for_the_start(
    scripted: list[GroupRead],
) -> None:
    def refused(sink: GroupSink) -> None:
        del sink
        raise ValueError("statement refused")

    streams = _streams(scripted, (refused,))
    streams.start()

    with pytest.raises(ValueError, match="statement refused"):
        streams.wait_started()


def test_a_merge_wider_than_the_query_bound_is_refused_at_construction() -> None:
    tooling = cast("Any", SimpleNamespace(query_batch_max_concurrency=1))
    reads = (_read(_feeding()), _read(_feeding()))

    with pytest.raises(ValueError, match="every read at once"):
        GroupStreams(reads, tooling, _KEY)
