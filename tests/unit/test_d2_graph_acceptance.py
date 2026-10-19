# SPDX-License-Identifier: MIT-0
"""Paired controls for the D2 GRAPH-path runtime acceptance verdict (merge-gate finding F13).

`tests/integration/test_d2_graph_root_acceptance.py` runs only in D2's live-acceptance step, so
its verdict is exercised here on fictional read-backs: one accepted root, then one refusal per
closed check. The effect codec is the graph bake's own and is covered by `test_graph_bake.py`, so
each fictional node maps straight to its effect.
"""

from __future__ import annotations

import hashlib
import pathlib
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from tests.integration import d2_graph_acceptance as acceptance

from domain.operations.execution_graph_dispatch import NodeState
from domain.operations.execution_graph_release import (
    BakeEffectKind,
    ServingBinding,
    serving_generation,
)
from domain.operations.generated_execution_graphs import BAKE_TASKS
from domain.operations.run_request import RunState
from domain.presentation.preset_artifacts import (
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactRevision,
    PresetArtifactVariant,
)

pytestmark = [pytest.mark.unit]

GRANT = hashlib.sha256(b"fictional-d2-grant").hexdigest()
OLDER = hashlib.sha256(b"fictional-older-grant").hexdigest()
WRITTEN_AT = "2026-10-15T12:00:00Z"
NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
ROSTER = tuple(f"{row['page']}#{row['variant']}" for row in BAKE_TASKS["roster"])
WRITTEN_KEY = ROSTER[0]


def _revision(key: str) -> PresetArtifactRevision:
    return PresetArtifactRevision.from_publication_identities((f"publication-{key}",))


def _effect(key: str, kind: BakeEffectKind, grant: str = GRANT) -> SimpleNamespace:
    page, variant = key.split("#", 1)
    available = kind is not BakeEffectKind.UNAVAILABLE
    return SimpleNamespace(
        grant_id=grant,
        page=page,
        variant=variant,
        effect_kind=kind,
        reason_code=None if available else "fictional_unavailable",
        artifact_revision=_revision(key).value if available else None,
    )


def _pointer(key: str, grant: str | None) -> PresetArtifactPointer:
    page, variant = key.split("#", 1)
    return PresetArtifactPointer(
        PresetArtifactPage(page),
        PresetArtifactVariant(variant),
        _revision(key),
        "d" * 64,
        1,
        grant_id=grant,
        serving_generation=None if grant is None else serving_generation(grant),
        written_at=None if grant is None else WRITTEN_AT,
    )


def _binding(grant: str, generation: str | None = None) -> ServingBinding:
    return ServingBinding(
        binding_id="b" * 64,
        grant_id=grant,
        conflict_unit="u" * 64,
        served_revision=NOW,
        artifact_revision=serving_generation(grant) if generation is None else generation,
        contributor_roots=("root-flow-fictional",),
        bound_at=NOW,
    )


def _node(name: str, state: NodeState = NodeState.SUCCEEDED) -> Any:
    return SimpleNamespace(name=name, state=state)


class _World:
    """One fictional root's read-back: one written row, the rest unchanged under an older grant."""

    def __init__(self) -> None:
        self.run: Any = SimpleNamespace(
            run_id="run-fictional", state=RunState.SUCCEEDED, grant_id=GRANT
        )
        self.effects = {
            key: _effect(
                key, BakeEffectKind.WRITTEN if key == WRITTEN_KEY else BakeEffectKind.UNCHANGED
            )
            for key in ROSTER
        }
        self.nodes: list[Any] = [_node("bake_finalize")] + [
            _node(f"bake_task:{key}") for key in ROSTER
        ]
        self.pointers: dict[str, PresetArtifactPointer | None] = {
            key: _pointer(key, GRANT if key == WRITTEN_KEY else OLDER) for key in ROSTER
        }
        self.bindings = {GRANT: _binding(GRANT), OLDER: _binding(OLDER)}

    def verdict(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        def read(node: Any) -> tuple[SimpleNamespace, dict[str, object]]:
            return self.effects[node.name.removeprefix("bake_task:")], {}

        monkeypatch.setattr(acceptance, "read_task_effect", read)
        return cast(
            "dict[str, Any]",
            acceptance.d2_root_verdict(
                ROSTER, self.run, tuple(self.nodes), self.pointers, self.bindings.get
            ),
        )


def test_the_twenty_row_roster_is_the_acceptance_roster() -> None:
    assert len(ROSTER) == acceptance.D2_ROSTER_ROWS


def test_a_finalized_root_whose_pointers_are_served_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verdict = _World().verdict(monkeypatch)
    assert verdict["failures"] == []
    assert (verdict["written"], verdict["serving_generation"]) == (1, serving_generation(GRANT))
    assert verdict["rows"][WRITTEN_KEY]["served_built_at"] == WRITTEN_AT


def test_control_a_grant_less_pointer_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World()
    world.pointers[ROSTER[1]] = _pointer(ROSTER[1], None)
    assert world.verdict(monkeypatch)["failures"] == [
        f"{ROSTER[1]}: the CURRENT pointer is absent or grant-less"
    ]


def test_control_a_written_row_under_another_grant_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World()
    world.pointers[WRITTEN_KEY] = _pointer(WRITTEN_KEY, OLDER)
    assert world.verdict(monkeypatch)["failures"] == [
        f"{WRITTEN_KEY}: a written row's pointer holds another grant"
    ]


def test_control_an_unbound_grant_is_not_served(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World()
    del world.bindings[GRANT]
    assert world.verdict(monkeypatch)["failures"] == [
        f"{WRITTEN_KEY}: not served (RefreshPendingError)",
        "the run's grant has no serving binding",
    ]


def test_control_a_binding_naming_another_generation_is_not_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World()
    world.bindings[GRANT] = _binding(GRANT, serving_generation(OLDER))
    assert world.verdict(monkeypatch)["failures"] == [
        f"{WRITTEN_KEY}: not served (UngrantedGenerationError)"
    ]


def test_control_an_unfinalized_or_short_root_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World()
    world.nodes[0] = _node("bake_finalize", NodeState.CLAIMED)
    del world.nodes[-1]
    assert world.verdict(monkeypatch)["failures"] == [
        "bake_finalize is ['CLAIMED']",
        f"{ROSTER[-1]}: task nodes []",
    ]


def test_control_a_root_that_wrote_nothing_is_not_proven(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World()
    world.effects[WRITTEN_KEY] = _effect(WRITTEN_KEY, BakeEffectKind.UNCHANGED)
    assert world.verdict(monkeypatch)["failures"] == ["no row was written under the run's grant"]


def test_control_a_failed_run_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World()
    world.run = SimpleNamespace(run_id="run-fictional", state=RunState.FAILED, grant_id=GRANT)
    assert world.verdict(monkeypatch)["failures"] == [f"the run is FAILED with grant {GRANT!r}"]


# --- merge-gate round 6, F2: the complete task, write phase included, fits its budget ---------

BUDGETS = cast("dict[str, int]", BAKE_TASKS["budget_seconds"])
PAIR_BYTES = (120_000, 30_000)
"""The written row's two body->custody chains (parent and one sibling)."""
_MS = 1_000_000


def _summary(  # noqa: PLR0913 -- one fictional summary's identity
    key: str, task_id: str, kind: BakeEffectKind, *, run_id: str
) -> dict[str, object]:
    page, variant = key.split("#", 1)
    written = kind is BakeEffectKind.WRITTEN
    return {
        "phase": "task_summary",
        "run_id": run_id,
        "task_id": task_id,
        "page": page,
        "variant": variant,
        "effect_kind": kind.value,
        "seconds": 60.5,
        "phase_plan_ms": 2_000,
        "phase_query_ms": 40_000,
        "phase_render_ms": 3_000,
        "phase_write_ms": 15_000 if written else 0,
        "pairs": len(PAIR_BYTES) if written else 0,
        "written_bytes": sum(PAIR_BYTES) if written else 0,
    }


def _write_span(  # noqa: PLR0913 -- one fictional span's identity and duration
    task_id: str, size: int, *, run_id: str, ms: int = 14_000
) -> dict[str, object]:
    return {
        "name": "artifact.write",
        "startTimeUnixNano": str(10 * _MS),
        "endTimeUnixNano": str(10 * _MS + ms * _MS),
        "attributes": {"run_id": run_id, "task_id": task_id, "bytes": size},
    }


class _Timing:
    """One accepted run's timing evidence: one written row, the rest unchanged, all in budget."""

    def __init__(self) -> None:
        self.world = _World()
        self.run_id = cast("str", self.world.run.run_id)
        self.nodes: list[Any] = [
            SimpleNamespace(
                name=f"bake_task:{key}", node_id=f"node-{index}", state=NodeState.SUCCEEDED
            )
            for index, key in enumerate(ROSTER)
        ]
        self.summaries = [
            _summary(key, f"node-{index}", self.world.effects[key].effect_kind, run_id=self.run_id)
            for index, key in enumerate(ROSTER)
        ]
        self.spans = [_write_span("node-0", size, run_id=self.run_id) for size in PAIR_BYTES]

    def verdict(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        def read(node: Any) -> tuple[SimpleNamespace, dict[str, object]]:
            return self.world.effects[node.name.removeprefix("bake_task:")], {}

        monkeypatch.setattr(acceptance, "read_task_effect", read)
        return cast(
            "dict[str, Any]",
            acceptance.d2_task_timing_verdict(
                ROSTER,
                self.world.run,
                tuple(self.nodes),
                tuple(self.summaries),
                tuple(self.spans),
                BUDGETS,
            ),
        )


def test_every_row_whose_complete_task_fits_its_budget_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verdict = _Timing().verdict(monkeypatch)
    assert verdict["failures"] == []
    row = verdict["rows"][WRITTEN_KEY]
    assert (row["write_ms"], row["write_spans"], row["tail_seconds"]) == (15_000, 2, 0.5)
    assert row["budget_seconds"] == BUDGETS[WRITTEN_KEY]


def test_control_a_render_that_consumes_the_write_allowance_is_over_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timing = _Timing()
    budget = BUDGETS[WRITTEN_KEY]
    timing.summaries[0] = {**timing.summaries[0], "seconds": budget + 0.5}
    timing.summaries[0]["phase_render_ms"] = (budget - 57) * 1_000
    assert timing.verdict(monkeypatch)["failures"] == [
        f"{WRITTEN_KEY}: the complete task took {budget + 0.5} s, over its {budget} s budget"
    ]


def test_control_a_written_row_without_its_write_spans_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timing = _Timing()
    timing.spans = []
    assert timing.verdict(monkeypatch)["failures"] == [
        f"{WRITTEN_KEY}: 0 artifact.write spans, the summary wrote 2",
        f"{WRITTEN_KEY}: the artifact.write bytes are not the summary's written bytes",
    ]
    timing.spans = [_write_span("node-0", PAIR_BYTES[0], run_id=timing.run_id)]
    assert timing.verdict(monkeypatch)["failures"] == [
        f"{WRITTEN_KEY}: 1 artifact.write spans, the summary wrote 2",
        f"{WRITTEN_KEY}: the artifact.write bytes are not the summary's written bytes",
    ]


def test_control_evidence_from_another_run_never_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    timing = _Timing()
    timing.summaries[0] = {**timing.summaries[0], "run_id": "run-other"}
    assert timing.verdict(monkeypatch)["failures"] == [
        f"{WRITTEN_KEY}: 0 task summaries correlate to the run, not one"
    ]
    timing = _Timing()
    timing.spans = [_write_span("node-0", size, run_id="run-other") for size in PAIR_BYTES]
    assert timing.verdict(monkeypatch)["failures"] == [
        f"{WRITTEN_KEY}: 0 artifact.write spans, the summary wrote 2",
        f"{WRITTEN_KEY}: the artifact.write bytes are not the summary's written bytes",
    ]


def test_control_another_task_or_a_second_attempt_never_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timing = _Timing()
    timing.summaries[0] = {**timing.summaries[0], "task_id": "node-1"}
    assert timing.verdict(monkeypatch)["failures"] == [
        f"{WRITTEN_KEY}: 0 task summaries correlate to the run, not one"
    ]
    timing = _Timing()
    timing.summaries.append(dict(timing.summaries[0]))
    assert timing.verdict(monkeypatch)["failures"] == [
        f"{WRITTEN_KEY}: 2 task summaries correlate to the run, not one"
    ]


def test_control_a_write_span_outlasting_its_phase_or_an_unwritten_write_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timing = _Timing()
    timing.spans[0] = _write_span("node-0", PAIR_BYTES[0], run_id=timing.run_id, ms=15_002)
    assert timing.verdict(monkeypatch)["failures"] == [
        f"{WRITTEN_KEY}: an artifact.write span outlasts the write phase"
    ]
    timing = _Timing()
    timing.spans.append(_write_span("node-1", 10, run_id=timing.run_id))
    assert timing.verdict(monkeypatch)["failures"] == [
        f"{ROSTER[1]}: 1 artifact.write spans for an unwritten row"
    ]


def test_control_phases_outlasting_the_task_or_a_malformed_summary_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timing = _Timing()
    timing.summaries[1] = {**timing.summaries[1], "seconds": 30.0}
    assert timing.verdict(monkeypatch)["failures"] == [
        f"{ROSTER[1]}: the phases outlast the task that holds them"
    ]
    timing = _Timing()
    del timing.summaries[1]["phase_write_ms"]
    assert timing.verdict(monkeypatch)["failures"] == [
        f"{ROSTER[1]}: the task summary is malformed"
    ]


def test_control_a_row_without_a_succeeded_task_node_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timing = _Timing()
    timing.nodes[0].state = NodeState.CLAIMED
    assert timing.verdict(monkeypatch)["failures"] == [f"{WRITTEN_KEY}: task nodes ['CLAIMED']"]
