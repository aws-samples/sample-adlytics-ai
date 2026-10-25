# SPDX-License-Identifier: MIT-0
"""The D2 GRAPH-path runtime acceptance verdict (merge-gate finding F13), with no AWS call.

The one exception is `read_task_timing_evidence`, the live acceptances' read of the accepted
run's Baker `task_summary` lines and `artifact.write` spans; it is called only when they run.

`test_d2_graph_root_acceptance.py` drives one deployed twenty-row root through GRAPH and hands
this function what it strongly read back. It is pure so the unit tier runs its paired controls:
every integration test skips without credentials, which would leave the verdict unexecuted in
every credential-free gate.

The root passes only when:

- the run is `SUCCEEDED` with a grant, its `bake_finalize` node is `SUCCEEDED`, and every one of
  the twenty roster rows has exactly one `SUCCEEDED` task node whose effect re-hashes to it and
  names the run's grant;
- every available row's CURRENT pointer is grant-bearing, holds the effect's artifact revision,
  carries its grant's serving generation and a write instant, and every row the run wrote holds
  the run's own grant;
- the run's grant has a serving binding, and every available row's pointer is served under its
  grant's binding as `built_at` its write instant (the presentation routes' own decision).


`d2_task_timing_verdict` decides the complete-task timing of the same accepted run (merge-gate
round 6, F2): each row's one correlated Baker `task_summary` and its `artifact.write` spans,
bound to the run and the row's `SUCCEEDED` task node, must show render, write and tail fitting
the row's generated `budget_seconds`. The live acceptance applies it to the run it decides.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from adapters.jobs.preset_artifact_publisher.graph_bake import (
    TASK_PHASES,
    read_task_effect,
    task_node_name,
)
from domain.operations.execution_graph_dispatch import NodeState
from domain.operations.execution_graph_release import (
    BAKE_FINALIZE_NODE,
    BakeEffectKind,
    UngrantedGenerationError,
    serving_generation,
)
from domain.operations.generated_execution_graphs import BAKE_TASKS
from domain.operations.run_request import RunState
from domain.presentation.graph_serving import RefreshPendingError, granted_generation

if TYPE_CHECKING:
    from domain.operations.execution_graph_release import ServingBinding
    from domain.presentation.preset_artifacts import PresetArtifactPointer
    from ports.outbound.execution_graph_dispatch import NodeRecord, RunRecord

__all__ = [
    "D2_ROSTER_ROWS",
    "d2_root_verdict",
    "d2_task_timing_verdict",
    "live_task_timing_verdict",
    "read_task_timing_evidence",
]

D2_ROSTER_ROWS = 20
"""The D2 roster's row count (`execution-graphs.yaml` `bake_tasks.roster`, S0 certificate)."""


def d2_root_verdict(  # noqa: PLR0912, PLR0913, PLR0915, PLR0917 -- one ordered list of closed checks over the read-back
    roster: tuple[str, ...],
    run: RunRecord,
    nodes: tuple[NodeRecord, ...],
    pointers: Mapping[str, PresetArtifactPointer | None],
    binding: Callable[[str], ServingBinding | None],
) -> dict[str, object]:
    """Return the root's verdict: `failures` is empty exactly when the root is accepted."""
    failures: list[str] = []
    rows: dict[str, dict[str, object]] = {}
    if len(roster) != D2_ROSTER_ROWS:
        failures.append(f"the roster has {len(roster)} rows, not {D2_ROSTER_ROWS}")
    grant = run.grant_id
    if run.state is not RunState.SUCCEEDED or grant is None:
        failures.append(f"the run is {run.state.value} with grant {grant!r}")
    by_name: dict[str, list[NodeRecord]] = {}
    for node in nodes:
        by_name.setdefault(node.name, []).append(node)
    finalize = by_name.get(BAKE_FINALIZE_NODE, [])
    if [item.state for item in finalize] != [NodeState.SUCCEEDED]:
        failures.append(f"bake_finalize is {[item.state.value for item in finalize]}")
    written = 0
    for key in roster:
        tasks = by_name.get(task_node_name(key), [])
        if [item.state for item in tasks] != [NodeState.SUCCEEDED]:
            failures.append(f"{key}: task nodes {[item.state.value for item in tasks]}")
            continue
        effect, _extra = read_task_effect(tasks[0])
        row: dict[str, object] = {"effect": effect.effect_kind.value}
        rows[key] = row
        if effect.grant_id != grant or f"{effect.page}#{effect.variant}" != key:
            failures.append(f"{key}: the effect names another grant or row")
        if effect.effect_kind is BakeEffectKind.UNAVAILABLE:
            row["reason"] = effect.reason_code
            continue
        pointer = pointers.get(key)
        if pointer is None or pointer.grant_id is None:
            failures.append(f"{key}: the CURRENT pointer is absent or grant-less")
            continue
        row.update(
            pointer_grant_is_run=pointer.grant_id == grant,
            revision=pointer.revision.value,
            written_at=pointer.written_at,
        )
        if pointer.revision.value != effect.artifact_revision:
            failures.append(f"{key}: the pointer does not hold the effect's revision")
        if pointer.serving_generation != serving_generation(pointer.grant_id):
            failures.append(f"{key}: the pointer's serving generation is not its grant's")
        if effect.effect_kind is BakeEffectKind.WRITTEN:
            written += 1
            if pointer.grant_id != grant:
                failures.append(f"{key}: a written row's pointer holds another grant")
        try:
            served = granted_generation(pointer, binding(pointer.grant_id))
        except (UngrantedGenerationError, RefreshPendingError) as error:
            failures.append(f"{key}: not served ({type(error).__name__})")
            continue
        row["served_built_at"] = served.built_at
        if served.built_at is None or served.built_at != pointer.written_at:
            failures.append(f"{key}: served as {served}, not built_at its write instant")
    if grant is not None and binding(grant) is None:
        failures.append("the run's grant has no serving binding")
    if written == 0:
        failures.append("no row was written under the run's grant")
    return {
        "run_id": run.run_id,
        "grant_id": grant,
        "serving_generation": None if grant is None else serving_generation(grant),
        "rows": rows,
        "written": written,
        "failures": failures,
    }


TASK_SUMMARY = "task_summary"
"""The Baker's one structured line per task attempt (`graph_bake._report_task`)."""
ARTIFACT_WRITE_SPAN = "artifact.write"
"""One body->custody chain's span (`preset_artifacts._artifact_chain`), one per written pair."""
_MS_SLACK = 1
"""Phase milliseconds are truncated to whole milliseconds, so a nested duration may read 1 ms
longer than the phase holding it."""


def _attributes(item: Mapping[str, object]) -> Mapping[str, object]:
    found = item.get("attributes")
    return cast("Mapping[str, object]", found) if isinstance(found, Mapping) else {}


def _span_ms(item: Mapping[str, object]) -> int | None:
    try:
        start = int(cast("str | int", item["startTimeUnixNano"]))
        end = int(cast("str | int", item["endTimeUnixNano"]))
    except KeyError, TypeError, ValueError:
        return None
    return None if end < start else (end - start) // 1_000_000


def _summary_failures(  # noqa: PLR0913 -- one row's closed timing checks, all keyword inputs
    key: str,
    summary: Mapping[str, object],
    *,
    budget: object,
    written: bool,
    writes: list[Mapping[str, object]],
    row: dict[str, object],
) -> list[str]:
    """Check one row's correlated summary and write spans; record the measured figures."""
    try:
        seconds = float(cast("float", summary["seconds"]))
        phase_ms = {name: int(cast("int", summary[f"phase_{name}_ms"])) for name in TASK_PHASES}
        pairs = int(cast("int", summary["pairs"]))
        written_bytes = int(cast("int", summary["written_bytes"]))
    except KeyError, TypeError, ValueError:
        return [f"{key}: the task summary is malformed"]
    tail = round(seconds - sum(phase_ms.values()) / 1_000, 3)
    row.update(seconds=seconds, budget_seconds=budget, tail_seconds=tail, write_spans=len(writes))
    row.update({f"{name}_ms": value for name, value in phase_ms.items()})
    failures: list[str] = []
    if not isinstance(budget, int | float) or isinstance(budget, bool):
        failures.append(f"{key}: the row has no generated budget")
    elif seconds > budget:
        failures.append(f"{key}: the complete task took {seconds} s, over its {budget} s budget")
    if tail * 1_000 < -_MS_SLACK * len(TASK_PHASES):
        failures.append(f"{key}: the phases outlast the task that holds them")
    if not written:
        if writes:
            failures.append(f"{key}: {len(writes)} artifact.write spans for an unwritten row")
        return failures
    if pairs < 1 or len(writes) != pairs:
        failures.append(f"{key}: {len(writes)} artifact.write spans, the summary wrote {pairs}")
    span_bytes = [_attributes(item).get("bytes") for item in writes]
    if not all(isinstance(value, int) for value in span_bytes) or (
        sum(cast("list[int]", span_bytes)) != written_bytes
    ):
        failures.append(f"{key}: the artifact.write bytes are not the summary's written bytes")
    durations = [_span_ms(item) for item in writes]
    if any(value is None for value in durations):
        failures.append(f"{key}: an artifact.write span has no valid duration")
    elif writes and max(cast("list[int]", durations)) > phase_ms["write"] + _MS_SLACK:
        failures.append(f"{key}: an artifact.write span outlasts the write phase")
    return failures


def d2_task_timing_verdict(  # noqa: PLR0913, PLR0917 -- the accepted run's read-back and evidence
    roster: tuple[str, ...],
    run: RunRecord,
    nodes: tuple[NodeRecord, ...],
    summaries: tuple[Mapping[str, object], ...],
    spans: tuple[Mapping[str, object], ...],
    budgets: Mapping[str, object],
) -> dict[str, object]:
    """Return the complete-task timing verdict: `failures` is empty exactly when every row fits.

    Merge-gate round 6, F2: a render may not consume the write allowance unseen. For each row
    the evidence is bound to the accepted run and the row's one `SUCCEEDED` task node: exactly
    one `task_summary` names that run, that node's task id, the row, and the node's effect kind.
    Its complete `seconds` (plan, query, render, write and the tail after them) must not exceed
    the row's generated `budget_seconds`, and its phases must fit inside it. A written row must
    carry exactly one `artifact.write` span per written pair, under the same run and task id,
    whose bytes sum to the summary's written bytes and none of which outlasts the write phase;
    an unwritten row must carry none. Evidence from another run or task never counts.
    """
    failures: list[str] = []
    rows: dict[str, dict[str, object]] = {}
    by_name: dict[str, list[NodeRecord]] = {}
    for node in nodes:
        by_name.setdefault(node.name, []).append(node)
    for key in roster:
        tasks = by_name.get(task_node_name(key), [])
        if [item.state for item in tasks] != [NodeState.SUCCEEDED]:
            failures.append(f"{key}: task nodes {[item.state.value for item in tasks]}")
            continue
        (task,) = tasks
        effect, _extra = read_task_effect(task)
        row: dict[str, object] = {"task_id": task.node_id, "effect": effect.effect_kind.value}
        rows[key] = row
        owned = [
            item
            for item in summaries
            if item.get("phase") == TASK_SUMMARY
            and item.get("run_id") == run.run_id
            and item.get("task_id") == task.node_id
            and f"{item.get('page')}#{item.get('variant')}" == key
            and item.get("effect_kind") == effect.effect_kind.value
        ]
        if len(owned) != 1:
            failures.append(f"{key}: {len(owned)} task summaries correlate to the run, not one")
            continue
        writes = [
            item
            for item in spans
            if item.get("name") == ARTIFACT_WRITE_SPAN
            and _attributes(item).get("run_id") == run.run_id
            and _attributes(item).get("task_id") == task.node_id
        ]
        failures.extend(
            _summary_failures(
                key,
                owned[0],
                budget=budgets.get(key),
                written=effect.effect_kind is BakeEffectKind.WRITTEN,
                writes=writes,
                row=row,
            )
        )
    return {"run_id": run.run_id, "rows": rows, "failures": failures}


def read_task_timing_evidence(  # noqa: PLR0913 -- the session, target and the run's identity
    session: Any, environment: str, *, started_at: datetime, run_id: str
) -> tuple[tuple[Mapping[str, object], ...], tuple[Mapping[str, object], ...]]:
    """Read one run's Baker `task_summary` lines and `artifact.write` spans (live, read-only).

    The readers are the traceability acceptance's own (`task_summaries`, which refuses a cut
    result, and the `aws/spans` retrieval keyed by `run_id`); an incomplete or truncated span
    retrieval raises rather than letting missing spans read as an unwritten row.
    """
    root = Path(__file__).resolve().parents[2]
    for path in (str(root), str(root / "validation")):  # the module imports its gate top-level
        if path not in sys.path:
            sys.path.insert(0, path)
    from validation import (  # noqa: PLC0415 -- live-only reader
        probe_ingestion_traceability_acceptance as trace,
    )

    window = (started_at - timedelta(minutes=5), datetime.now(UTC))
    logs = session.client("logs")
    group = trace._function_log_group(session, f"Adlytics-Publisher-{environment}", "Baker")  # pyright: ignore[reportPrivateUsage]
    summaries = trace.task_summaries(logs, group, window, [run_id])
    retrieval = trace._retrieve(  # pyright: ignore[reportPrivateUsage]
        logs, environment, window, keys=(("run_id", run_id),), span_names=(ARTIFACT_WRITE_SPAN,)
    )
    if retrieval.get("status") != "complete" or retrieval.get("truncated"):
        raise RuntimeError("the artifact.write span retrieval is incomplete")
    spans = cast("list[Mapping[str, object]]", retrieval.get("spans") or [])
    return tuple(summaries), tuple(spans)


TIMING_ATTEMPTS = 20
TIMING_POLL_SECONDS = 30.0
"""Logs and spans reach Logs Insights after the run settles; re-read until they cover it."""


def live_task_timing_verdict(  # noqa: PLR0913 -- the session, target and the accepted read-back
    session: Any,
    environment: str,
    *,
    started_at: datetime,
    accepted: tuple[tuple[str, ...], RunRecord, tuple[NodeRecord, ...]],
) -> dict[str, object]:
    """Read the accepted run's timing evidence and decide it against the generated budgets.

    `accepted` is the roster rows to decide, the run and its nodes the run's own verdict read.
    Only the last read decides; earlier reads only wait out Logs Insights ingestion.
    """
    roster, run, nodes = accepted
    budgets = cast("Mapping[str, object]", BAKE_TASKS["budget_seconds"])
    timing: dict[str, object] = {}
    for attempt in range(TIMING_ATTEMPTS):
        summaries, spans = read_task_timing_evidence(
            session, environment, started_at=started_at, run_id=run.run_id
        )
        timing = d2_task_timing_verdict(roster, run, nodes, summaries, spans, budgets)
        if not timing["failures"] or attempt == TIMING_ATTEMPTS - 1:
            break
        time.sleep(TIMING_POLL_SECONDS)
    return timing
