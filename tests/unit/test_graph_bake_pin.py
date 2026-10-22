# SPDX-License-Identifier: MIT-0
"""The pin pass's bounded dependency retry and `pin_refusal` (ADR-142, design 02 §3.3).

One throttle during the pin pass used to withhold every page of the cohort. Now a row's plan
pass retries a `DependencyError` up to `PIN_DEPENDENCY_ATTEMPTS` times while the invocation
leaves `PIN_WRITE_MARGIN_SECONDS`; a row that runs out pins with zero requests and its
`pin_refusal`, the cohort proceeds, and its task settles `dependency_unavailable`. The pass's
own release-store calls retry the same way and re-raise when theirs run out. Every identity is
fictional.
"""

from __future__ import annotations

from typing import Any

import pytest
from execution_graph_release_support import release_store
from graph_bake_v3_support import Bake, roster
from refusal_telemetry_support import LOGGER, capture

from adapters.jobs.preset_artifact_publisher import graph_bake
from adapters.jobs.preset_artifact_publisher.graph_bake import (
    PIN_DEPENDENCY_ATTEMPTS,
    PIN_WRITE_MARGIN_SECONDS,
    task_node_name,
)
from adapters.jobs.preset_artifact_publisher.transport import BakeIntent
from adapters.outbound.dynamodb.execution_graph_dispatch import DynamoGraphDispatchStore
from adapters.outbound.dynamodb.execution_graph_release import DynamoGraphReleaseStore
from domain.operations.execution_graph_dispatch import NodeState
from domain.operations.execution_graph_release import (
    BakeEffectKind,
    DurableBakeSourceManifestInvalidError,
    PinRefusal,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode
from ports.outbound.execution_graph_dispatch import GraphStoreUnavailableError

pytestmark = pytest.mark.unit


def _throttle(index: int = 1) -> DependencyError:
    return DependencyError(ErrorCode("FICTIONAL", 50 + index), "a throttled CURRENT read")


def _failing_prefetch(bake: Bake, key: str, failures: int) -> list[int]:
    """Make `key`'s prefetch raise a throttle `failures` times, then succeed; count attempts."""
    attempts: list[int] = []
    original = bake.sources.prefetch

    def prefetch(intent: BakeIntent) -> None:
        if f"{intent.page}#{intent.variant}" == key:
            attempts.append(1)
            if len(attempts) <= failures:
                raise _throttle(len(attempts))
        original(intent)

    bake.sources.prefetch = prefetch  # type: ignore[method-assign]
    return attempts


def test_a_throttle_on_attempt_1_then_success_pins_the_row_normally() -> None:
    bake = Bake(roster=roster(2))
    run = bake.granted()
    flaky, _control = bake.roster.keys
    attempts = _failing_prefetch(bake, flaky, failures=1)
    assert bake.fan_out(run).node_state is NodeState.SUCCEEDED
    pin = release_store(bake.tables).read_durable_bake_sources(run)
    assert pin is not None
    source = pin.manifest.task(flaky)
    assert (source.pin_refusal, source.plan_count, len(attempts)) == (None, 2, 2)
    assert len(bake.slept) == 1 and 0 <= bake.slept[0] <= 1.0  # full jitter, uniform(0, 1)


def test_three_throttles_pin_the_row_refused_and_the_cohort_proceeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry = capture(monkeypatch)
    bake = Bake(roster=roster(2))
    bake.runtime.logger = LOGGER
    bake.runtime.refusal_metric = telemetry.metric
    run = bake.granted()
    refused, control = bake.roster.keys
    attempts = _failing_prefetch(bake, refused, failures=PIN_DEPENDENCY_ATTEMPTS)
    assert bake.fan_out(run).node_state is NodeState.SUCCEEDED
    assert len(attempts) == PIN_DEPENDENCY_ATTEMPTS and len(bake.slept) == 2
    pin = release_store(bake.tables).read_durable_bake_sources(run)
    assert pin is not None
    source = pin.manifest.task(refused)
    assert (source.pin_refusal, source.plan_count, source.request_digests) == (
        PinRefusal(PIN_DEPENDENCY_ATTEMPTS),
        0,
        (),
    )
    # The pin only logs; the task is the one emission site.
    assert telemetry.spans == []
    bake.dispatch(run)
    prefetched = len(attempts)
    result = bake.task(run, refused)
    assert (result.outcome, result.node_state) == (
        "graph_bake_task_unavailable",
        NodeState.SUCCEEDED,
    )
    assert len(attempts) == prefetched  # a refused pin doesn't replay
    assert refused not in bake.queries.calls
    effect, extra = graph_bake.read_task_effect(bake.node(run, task_node_name(refused)))
    assert effect.effect_kind is BakeEffectKind.UNAVAILABLE
    assert extra["isolated_refusals"] == [
        {
            "scope": "page",
            "reason": "dependency_unavailable",
            "refusal_class": "dependency",
            "count": 1,
            "context": {"attempts": PIN_DEPENDENCY_ATTEMPTS},
        }
    ]
    assert telemetry.triples() == [("page", "dependency_unavailable", 1)]
    assert telemetry.datapoints == [("dependency", 1)]
    # Control: the sibling pinned its plan and is written.
    assert bake.task(run, control).outcome == "graph_bake_task_written"


def test_the_row_refused_line_names_the_reason_and_its_allowlisted_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines: list[tuple[str, str, dict[str, object]]] = []
    bake = Bake(roster=roster(2))
    bake.runtime.logger = LOGGER

    def log(phase: str, outcome: str, severity: Any = None, **details: object) -> None:
        del severity
        lines.append((phase, outcome, details))

    monkeypatch.setattr(bake.runtime, "log", log)
    run = bake.granted()
    refused, _ = bake.roster.keys
    _failing_prefetch(bake, refused, failures=PIN_DEPENDENCY_ATTEMPTS)
    bake.fan_out(run)
    (row,) = [details for _phase, outcome, details in lines if outcome == "row_refused"]
    assert {key: value for key, value in row.items() if key != "run_id"} == {
        "key": refused,
        "error_code": "ADLY-FICTIONAL-0053",
        "reason": "dependency_unavailable",
        "attempts": PIN_DEPENDENCY_ATTEMPTS,
    }


def test_attempts_stop_when_the_invocation_leaves_less_than_the_pin_write_margin() -> None:
    bake = Bake(roster=roster(2))
    run = bake.granted()
    refused, _ = bake.roster.keys
    attempts = _failing_prefetch(bake, refused, failures=1)
    # Any sleep at all would leave less than the margin: no retry, the row pins refused.
    tight = int(PIN_WRITE_MARGIN_SECONDS * 1_000)
    assert bake.fan_out(run, remaining_ms=lambda: tight).node_state is NodeState.SUCCEEDED
    pin = release_store(bake.tables).read_durable_bake_sources(run)
    assert pin is not None
    assert pin.manifest.task(refused).pin_refusal == PinRefusal(1)
    assert (len(attempts), bake.slept) == (1, [])


def test_a_failed_attempts_partial_reads_are_not_in_the_pin() -> None:
    clean = Bake(roster=roster(2))
    clean_run = clean.granted()
    assert clean.fan_out(clean_run).node_state is NodeState.SUCCEEDED
    clean_pin = release_store(clean.tables).read_durable_bake_sources(clean_run)

    bake = Bake(roster=roster(2))
    run = bake.granted()
    flaky, _ = bake.roster.keys
    # The render reads its CURRENT scope, then raises: that attempt's read must be discarded.
    calls: list[int] = []
    original = bake.sources.bake

    def bake_once_failing(intent: BakeIntent) -> Any:
        if f"{intent.page}#{intent.variant}" == flaky and not calls:
            calls.append(1)
            assert bake.sources.reads is not None
            bake.sources.reads.current_for_scope(
                graph_bake_scope("extra-scope"), bake.sources.reads.window_horizon(1)
            )
            raise _throttle()
        return original(intent)

    bake.sources.bake = bake_once_failing  # type: ignore[method-assign]
    assert bake.fan_out(run).node_state is NodeState.SUCCEEDED
    pin = release_store(bake.tables).read_durable_bake_sources(run)
    assert pin is not None and clean_pin is not None
    assert pin.manifest.task(flaky).reads_digest == clean_pin.manifest.task(flaky).reads_digest


def graph_bake_scope(name: str) -> Any:
    from graph_bake_v3_support import _scope  # noqa: PLC0415  # pyright: ignore[reportPrivateUsage]

    return _scope(name)


# --- the pass's own release-store calls (round-2 M2) ----------------------------------------


def _fail_store_call(  # noqa: PLR0913, PLR0917 -- the patch, the call and its failures
    monkeypatch: pytest.MonkeyPatch, owner: type, name: str, failures: int
) -> list[int]:
    """Make `owner.name` raise `GraphStoreUnavailableError` `failures` times once armed.

    It arms when the pin pass marks its selection, so the consumer's own earlier calls run.
    """
    armed: list[int] = []
    calls: list[int] = []
    original_mark = DynamoGraphDispatchStore.mark_bake_selection
    original = getattr(owner, name)

    def mark(self: Any, claim: Any) -> None:
        armed.append(1)
        if name == "mark_bake_selection":
            calls.append(1)
            if len(calls) <= failures:
                raise GraphStoreUnavailableError("injected mark failure")
        original_mark(self, claim)

    def failing(self: Any, *args: Any) -> Any:
        if armed:
            calls.append(1)
            if len(calls) <= failures:
                raise GraphStoreUnavailableError(f"injected {name} failure")
        return original(self, *args)

    monkeypatch.setattr(DynamoGraphDispatchStore, "mark_bake_selection", mark)
    if name != "mark_bake_selection":
        monkeypatch.setattr(owner, name, failing)
    return calls


_STORE_CALLS: list[tuple[type, str]] = [
    (DynamoGraphDispatchStore, "mark_bake_selection"),
    (DynamoGraphReleaseStore, "read_serving_execution"),  # the pin's `_live_head`
    (DynamoGraphReleaseStore, "pin_durable_bake_sources"),
]


@pytest.mark.parametrize(
    ("owner", "name"), _STORE_CALLS, ids=lambda item: getattr(item, "__name__", item)
)
def test_a_store_call_failing_once_is_retried_and_the_pin_proceeds(
    monkeypatch: pytest.MonkeyPatch, owner: type, name: str
) -> None:
    bake = Bake(roster=roster(2))
    run = bake.granted()
    calls = _fail_store_call(monkeypatch, owner, name, failures=1)
    assert bake.fan_out(run).node_state is NodeState.SUCCEEDED
    assert len(calls) >= 2 and len(bake.slept) >= 1
    assert release_store(bake.tables).read_durable_bake_sources(run) is not None


@pytest.mark.parametrize(
    ("owner", "name"), _STORE_CALLS, ids=lambda item: getattr(item, "__name__", item)
)
def test_a_store_call_failing_every_attempt_re_raises_and_pins_nothing(
    monkeypatch: pytest.MonkeyPatch, owner: type, name: str
) -> None:
    bake = Bake(roster=roster(2))
    run = bake.granted()
    calls = _fail_store_call(monkeypatch, owner, name, failures=PIN_DEPENDENCY_ATTEMPTS)
    result = bake.fan_out(run)
    assert (result.acknowledge, result.node_state) == (False, NodeState.CLAIMED)
    assert len(calls) == PIN_DEPENDENCY_ATTEMPTS
    if name != "pin_durable_bake_sources":
        assert release_store(bake.tables).read_durable_bake_sources(run) is None


def test_pin_retry_backoff_is_full_jitter_under_the_cap() -> None:
    sleeps: list[float] = []
    bake = Bake(roster=roster(1))
    bake.runtime.sleep = sleeps.append
    run = bake.granted()
    (key,) = bake.roster.keys
    _failing_prefetch(bake, key, failures=PIN_DEPENDENCY_ATTEMPTS)
    bake.fan_out(run)
    # After failure n the sleep is uniform(0, min(4, 0.5 * 2**n)): at most 1 s, then 2 s.
    assert len(sleeps) == 2 and sleeps[0] <= 1.0 and sleeps[1] <= 2.0


# --- manifest compatibility (round-2 N5) ----------------------------------------------------


def _source_items(bake: Bake, run: str) -> dict[str, dict[str, Any]]:
    return {
        sk.removeprefix("BAKE_SOURCE#"): item
        for (pk, sk), item in bake.tables.items.items()
        if pk == f"RUN#{run}" and sk.startswith("BAKE_SOURCE#")
    }


def test_a_pin_without_a_refusal_stores_no_new_attributes_and_re_hashes() -> None:
    bake = Bake(roster=roster(2))
    run = bake.granted()
    assert bake.fan_out(run).node_state is NodeState.SUCCEEDED
    items = _source_items(bake, run)
    assert not any({"pin_refusal", "pin_refusal_attempts"} & set(item) for item in items.values())
    pin = release_store(bake.tables).read_durable_bake_sources(run)  # re-hashes, or refuses
    assert pin is not None
    assert all(task.pin_refusal is None for task in pin.manifest.tasks)


def test_a_refused_pin_stores_both_attributes_and_a_half_refusal_is_refused() -> None:
    bake = Bake(roster=roster(2))
    run = bake.granted()
    refused, _ = bake.roster.keys
    _failing_prefetch(bake, refused, failures=PIN_DEPENDENCY_ATTEMPTS)
    assert bake.fan_out(run).node_state is NodeState.SUCCEEDED
    item = _source_items(bake, run)[refused]
    assert (item["pin_refusal"], item["pin_refusal_attempts"]) == (
        {"S": "dependency_unavailable"},
        {"N": str(PIN_DEPENDENCY_ATTEMPTS)},
    )
    del item["pin_refusal_attempts"]
    with pytest.raises(DurableBakeSourceManifestInvalidError):
        release_store(bake.tables).read_durable_bake_sources(run)
