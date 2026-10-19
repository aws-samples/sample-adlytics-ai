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

`audiences_over_bound_verdict` decides the over-bound Audiences acceptance (merge-gate finding
M2, owner decision 1b: an over-bound month shows the typed unavailable state, never a stale month
labelled current). `test_d2_audiences_over_bound_acceptance.py` hands it what it read back after
one GRAPH root over an over-bound month B, and the actual Audiences route's response.

`d2_task_timing_verdict` decides the complete-task timing of the same accepted run (merge-gate
round 6, F2): each row's one correlated Baker `task_summary` and its `artifact.write` spans,
bound to the run and the row's `SUCCEEDED` task node, must show render, write and tail fitting
the row's generated `budget_seconds`. Both live acceptances apply it to the run they decide.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from adapters.jobs.preset_artifact_publisher.bakeability import audiences_row_budget
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
from domain.presentation.audiences import (
    AUDIENCES_UNAVAILABLE_ARTIFACT_TYPE,
    require_audiences_artifact_payload,
    require_audiences_unavailable_artifact_payload,
)
from domain.presentation.graph_serving import RefreshPendingError, granted_generation
from domain.shared_kernel.values import DateRange

if TYPE_CHECKING:
    from domain.operations.execution_graph_release import ServingBinding
    from domain.presentation.preset_artifacts import PresetArtifactPointer
    from ports.outbound.execution_graph_dispatch import NodeRecord, RunRecord

__all__ = [
    "AUDIENCES_ROW",
    "D2_ROSTER_ROWS",
    "OVER_BOUND_DECLARATION",
    "OverBoundReadBack",
    "audiences_over_bound_verdict",
    "d2_root_verdict",
    "d2_task_timing_verdict",
    "declared_over_bound_source",
    "live_task_timing_verdict",
    "read_task_timing_evidence",
]

D2_ROSTER_ROWS = 20
"""The D2 roster's row count (`execution-graphs.yaml` `bake_tasks.roster`, S0 certificate)."""
AUDIENCES_ROW = "audiences#last_complete_month"
"""The Audiences roster row whose over-bound month the M2 acceptance drives."""
WINDOW_UNAVAILABLE = "window_unavailable"
PRESET_UNAVAILABLE = "preset_unavailable"
"""The route's typed pending result for an over-bound month (`_audiences_window_unavailable`)."""
_HTTP_OK = 200


OVER_BOUND_DECLARATION = (
    Path(__file__).resolve().parents[2] / "validation/fixtures/d2-audiences-over-bound-v1.json"
)
"""The tracked declaration of the one over-bound month-B delivery the acceptance accepts."""
OVER_BOUND_SCHEMA = "d2-audiences-over-bound-v1"


def declared_over_bound_source(supplied: str, declaration: Path = OVER_BOUND_DECLARATION) -> str:
    """Return `supplied` only when it is the tracked declaration's entry, else raise `ValueError`.

    Merge-gate round 4, M1: `over_bound_source` is bound to the declared month-B delivery, never
    to arbitrary text. The declaration's row floor must exceed the registered Audiences body
    bound, `audiences_row_budget(1)`, so the declared delivery is over-bound by construction.
    """
    document = cast("dict[str, object]", json.loads(declaration.read_text()))
    if document.get("schema_version") != OVER_BOUND_SCHEMA:
        raise ValueError("the over-bound declaration has the wrong schema")
    if document.get("page_row") != AUDIENCES_ROW:
        raise ValueError("the over-bound declaration names another roster row")
    floor = document.get("row_floor")
    if not isinstance(floor, int) or isinstance(floor, bool) or floor <= audiences_row_budget(1):
        raise ValueError("the over-bound declaration's row floor is not above the body bound")
    declared = document.get("over_bound_source")
    if not isinstance(declared, str) or not declared.strip():
        raise ValueError("the over-bound declaration names no source")
    if supplied.strip() != declared:
        raise ValueError("over_bound_source is not the declared over-bound month-B delivery")
    return declared


@dataclass(frozen=True, slots=True)
class OverBoundReadBack:
    """What the M2 acceptance strongly read back, and the actual Audiences route's response.

    `before` is month A's pointer and body JSON, read before the root launched; `after` is the
    pointer and body JSON the root finalized; `route` is the deployed route's status and decoded
    response for `last_complete_month`.
    """

    run: RunRecord
    nodes: tuple[NodeRecord, ...]
    before: tuple[PresetArtifactPointer | None, str | None]
    after: tuple[PresetArtifactPointer | None, str | None]
    binding: Callable[[str], ServingBinding | None]
    route: tuple[int, Mapping[str, object]]


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


def _payload_type(payload_json: str | None) -> object:
    try:
        decoded = cast("object", json.loads(payload_json or "null"))
    except json.JSONDecodeError:
        return None
    return cast("dict[str, object]", decoded).get("type") if isinstance(decoded, dict) else None


def _full_body_month(payload_json: str | None) -> DateRange | None:
    """Decode a complete Audiences body as the route does and return its month, else `None`.

    The type tag alone is not a full body (merge-gate round 4, M2): a truncated payload carrying
    `AudienceProfileArtifact` is refused by `require_audiences_artifact_payload`, as the
    presentation handler refuses it before serving.
    """
    try:
        payload = require_audiences_artifact_payload(cast("object", json.loads(payload_json or "")))
        month = cast("dict[str, str]", payload["month"])
        return DateRange(date.fromisoformat(month["start"]), date.fromisoformat(month["end"]))
    except KeyError, TypeError, ValueError:  # a JSONDecodeError is a ValueError
        return None


def _route_failures(
    route: tuple[int, Mapping[str, object]], window: Mapping[str, str], label: str
) -> list[str]:
    """Require the actual route's typed pending result for month B, never a page."""
    status, body = route
    if status != _HTTP_OK:
        return [f"the Audiences route answered {status}"]
    failures: list[str] = []
    if (body.get("kind"), body.get("reason_code")) != (WINDOW_UNAVAILABLE, PRESET_UNAVAILABLE):
        failures.append(
            f"the Audiences route served {body.get('kind')!r} ({body.get('reason_code')!r}), "
            f"not {WINDOW_UNAVAILABLE} ({PRESET_UNAVAILABLE})"
        )
    if "page" in body:
        failures.append("the Audiences route served a page body for the over-bound month")
    temporal = body.get("temporal")
    found = (
        cast("Mapping[str, object]", temporal).get("resolved")
        if isinstance(temporal, Mapping)
        else None
    )
    resolved: Mapping[str, object] = (
        cast("Mapping[str, object]", found) if isinstance(found, Mapping) else {}
    )
    served = (resolved.get("window"), resolved.get("period_label"))
    if served != (dict(window), label):
        failures.append(
            f"the route resolved {served[0]!r} {served[1]!r}, "
            f"not month B {dict(window)!r} {label!r}"
        )
    return failures


def audiences_over_bound_verdict(  # noqa: PLR0912, PLR0915 -- one ordered list of closed checks
    read: OverBoundReadBack,
) -> dict[str, object]:
    """Return the M2 verdict: `failures` is empty exactly when month B is served as pending.

    Accepted only when the run is a `SUCCEEDED` grant with `bake_finalize` `SUCCEEDED`; the
    Audiences task `SUCCEEDED` and wrote under the run's grant; month A's pointer held a full
    Audiences body, decoded whole, for the pointer's own window; the finalized pointer is the
    run's grant-bearing revision for a later month and carries the grant's serving generation;
    its body is the typed `audiences_body_over_bound`
    marker for that month; the pointer is served under the grant's binding; and the actual
    Audiences route answers the typed `window_unavailable` result for that month.
    """
    run = read.run
    failures: list[str] = []
    grant = run.grant_id
    if run.state is not RunState.SUCCEEDED or grant is None:
        failures.append(f"the run is {run.state.value} with grant {grant!r}")
    by_name: dict[str, list[NodeRecord]] = {}
    for node in read.nodes:
        by_name.setdefault(node.name, []).append(node)
    finalize = [item.state for item in by_name.get(BAKE_FINALIZE_NODE, [])]
    if finalize != [NodeState.SUCCEEDED]:
        failures.append(f"bake_finalize is {[state.value for state in finalize]}")
    verdict: dict[str, object] = {"run_id": run.run_id, "grant_id": grant, "failures": failures}
    tasks = by_name.get(task_node_name(AUDIENCES_ROW), [])
    if [item.state for item in tasks] != [NodeState.SUCCEEDED]:
        failures.append(f"{AUDIENCES_ROW}: task nodes {[item.state.value for item in tasks]}")
        return verdict
    effect, _extra = read_task_effect(tasks[0])
    verdict["effect"] = effect.effect_kind.value
    if effect.effect_kind is not BakeEffectKind.WRITTEN:
        failures.append(f"{AUDIENCES_ROW}: the task's effect is {effect.effect_kind.value}")
    if effect.grant_id != grant or f"{effect.page}#{effect.variant}" != AUDIENCES_ROW:
        failures.append(f"{AUDIENCES_ROW}: the effect names another grant or row")
    (before, before_json), (after, after_json) = read.before, read.after
    if before is None or before.resolved_window is None:
        failures.append("month A had no Audiences pointer before the root")
    elif (month_a := _full_body_month(before_json)) is None:
        failures.append("month A's pointer did not hold a full Audiences body")
    elif month_a != before.resolved_window:
        failures.append("month A's body is not for its pointer's window")
    if after is None or after.grant_id is None or after.resolved_window is None:
        failures.append(f"{AUDIENCES_ROW}: the finalized pointer is absent or grant-less")
        return verdict
    verdict["revision"] = after.revision.value
    if after.grant_id != grant:
        failures.append(f"{AUDIENCES_ROW}: the finalized pointer holds another grant")
    if after.serving_generation != serving_generation(after.grant_id):
        failures.append(f"{AUDIENCES_ROW}: the pointer's serving generation is not its grant's")
    if after.revision.value != effect.artifact_revision:
        failures.append(f"{AUDIENCES_ROW}: the pointer does not hold the effect's revision")
    if before is not None and before.resolved_window is not None:
        if after.revision == before.revision:
            failures.append(f"{AUDIENCES_ROW}: the pointer did not advance past month A")
        if after.resolved_window.start <= before.resolved_window.end:
            failures.append(f"{AUDIENCES_ROW}: month B is not after month A")
    if _payload_type(after_json) != AUDIENCES_UNAVAILABLE_ARTIFACT_TYPE:
        failures.append(f"{AUDIENCES_ROW}: month B's body is not the over-bound marker")
        return verdict
    try:
        unavailable = require_audiences_unavailable_artifact_payload(json.loads(after_json or ""))
    except (KeyError, TypeError, ValueError) as error:
        failures.append(f"{AUDIENCES_ROW}: month B's marker is malformed ({type(error).__name__})")
        return verdict
    verdict["reason"] = unavailable.reason_code
    if unavailable.month != after.resolved_window:
        failures.append(f"{AUDIENCES_ROW}: the marker's month is not the pointer's window")
    try:
        served = granted_generation(after, read.binding(after.grant_id))
    except (UngrantedGenerationError, RefreshPendingError) as error:
        failures.append(f"{AUDIENCES_ROW}: not served ({type(error).__name__})")
    else:
        verdict["served_built_at"] = served.built_at
        if served.built_at is None or served.built_at != after.written_at:
            failures.append(f"{AUDIENCES_ROW}: served as {served}, not built_at its write instant")
    window = {
        "start": after.resolved_window.start.isoformat(),
        "end": after.resolved_window.end.isoformat(),
    }
    failures.extend(_route_failures(read.route, window, unavailable.month_label))
    return verdict


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
