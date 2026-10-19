# SPDX-License-Identifier: MIT-0
"""The execution explanation (FR-8, BF-5): arms, refusals, parked successors and paging.

The service runs over real S1 admission output (`seeded_graph_store`) and the real read-only
adapters, so "zero graph writes" is the graph fake's own transaction log.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from execution_explanation_support import (
    LAST_MODIFIED,
    PROBE_KEY,
    VERSION_ID,
    FakeMac,
    FakeVersions,
    Seeded,
    seeded_graph_store,
)

from adapters.inbound.operational_status.explanation import ExplanationDependencies
from adapters.inbound.operational_status.explanation import explain as _explain_service
from domain.operations.execution_cursor import (
    MAX_CURSOR_BYTES,
    CursorCapacityLimitedError,
    CursorInvalidError,
)
from domain.operations.flow_explain import (
    MAX_PAGE,
    PARKED_SUCCESSOR,
    REQUIRED_STAGES,
    ExplanationArm,
    ExplanationInvalidError,
    ExplanationRefusal,
    ExplanationUnavailableError,
    StageEvidence,
    StageStatus,
    elapsed_ms,
    graph_stage_evidence,
    missing_required_stages,
    node_failures,
    parse_explanation_request,
    require_document_bound,
    stage_rows,
)
from domain.operations.graph_projection import (
    GraphProjectionRow,
    ProjectionKind,
    project_graph_item,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-073")]

GRAPH_ID = "a" * 64


def _deps(seeded: Seeded, mac: FakeMac | None = None, **versions: Any) -> ExplanationDependencies:
    known = versions.get("versions", {PROBE_KEY: [(VERSION_ID, LAST_MODIFIED)]})
    return ExplanationDependencies(
        authority=seeded.reader,
        projection=seeded.projection,
        manifests=FakeVersions(known),
        cursor_mac=mac or FakeMac(),
    )


def explain(request: Any, deps: ExplanationDependencies) -> Any:
    """Run the service; the tree is read as untyped JSON in assertions."""
    return _explain_service(request, deps)


def _manifest_body(**extra: Any) -> dict[str, Any]:
    return {"manifest": {"key": PROBE_KEY, "version_id": VERSION_ID}, **extra}


def _projected(names: tuple[str, ...] = ("p1-first", "p1-second")) -> Seeded:
    seeded = seeded_graph_store(names)
    seeded.project_all()
    return seeded


class TestArms:
    @pytest.mark.parametrize(
        ("body", "arm"),
        [
            ({"flow_id": "6a1f0f51-3f0e-5b5c-9a43-1f2d3c4b5a69"}, ExplanationArm.FLOW),
            ({"manifest": {"key": PROBE_KEY, "version_id": "v"}}, ExplanationArm.MANIFEST_VERSION),
            ({"manifest": {"key": PROBE_KEY}}, ExplanationArm.MANIFEST_KEY),
            ({"plan": {"graph_id": GRAPH_ID}}, ExplanationArm.PLAN),
            ({"node": {"graph_id": GRAPH_ID, "node": "write_unit"}}, ExplanationArm.NODE),
        ],
    )
    def test_each_of_the_five_arms_parses(self, body: dict[str, Any], arm: ExplanationArm) -> None:
        assert parse_explanation_request(body).arm is arm

    @pytest.mark.parametrize(
        "body",
        [
            None,
            [],
            {},
            {"flow_id": "6A1F0F51-3F0E-5B5C-9A43-1F2D3C4B5A69"},
            {"flow_id": "not-a-uuid"},
            {"flow_id": "6a1f0f51-3f0e-5b5c-9a43-1f2d3c4b5a69", "plan": {"graph_id": GRAPH_ID}},
            {"manifest": {"key": "no/retrieved/segment.json"}},
            {"manifest": {"key": PROBE_KEY.replace("manifest.json", "part-0.parquet")}},
            {"manifest": {"key": PROBE_KEY.replace("manifest.json", "manifest.json.tmp")}},
            {"manifest": {"key": PROBE_KEY, "version_id": "has space"}},
            {"manifest": {"key": PROBE_KEY, "extra": "x"}},
            {"manifest": {"key": "k" * 1025}},
            {"plan": {"graph_id": "A" * 64}},
            {"node": {"graph_id": GRAPH_ID, "node": "not_a_node"}},
            {"plan": {"graph_id": GRAPH_ID}, "limit": 0},
            {"plan": {"graph_id": GRAPH_ID}, "limit": MAX_PAGE + 1},
            {"plan": {"graph_id": GRAPH_ID}, "limit": True},
            {"plan": {"graph_id": GRAPH_ID}, "cursor": 5},
            {"plan": {"graph_id": GRAPH_ID}, "unknown": 1},
        ],
    )
    def test_an_invalid_shape_value_or_bound_is_invalid_reference(self, body: object) -> None:
        with pytest.raises(ExplanationInvalidError) as refused:
            parse_explanation_request(body)
        assert refused.value.refusal is ExplanationRefusal.INVALID_REFERENCE


class TestCompleteness:
    def _evidence(self, *stages: str, parked: tuple[str, ...] = ()) -> list[StageEvidence]:
        items = [
            StageEvidence(stage, StageStatus.APPLIED, f"a-{stage}", "authority", None, "graph")
            for stage in stages
        ]
        items += [
            StageEvidence(
                stage,
                StageStatus.PARKED,
                f"p-{stage}",
                "authority",
                None,
                "graph",
                predecessor_stage="plan_admitted",
            )
            for stage in parked
        ]
        return items

    def test_the_fr8_positive_control_is_exactly_plan_admitted(self) -> None:
        applied = tuple(
            stage for stage in REQUIRED_STAGES if stage not in {"plan_admitted", "units_dispatched"}
        )
        evidence = self._evidence(*applied, parked=("units_dispatched",))

        missing = missing_required_stages(evidence)
        rows, _ = stage_rows(evidence, LAST_MODIFIED)

        assert missing == ["plan_admitted"]
        assert [row["status"] for row in rows].count(PARKED_SUCCESSOR) >= 1
        assert len(missing) == len(set(missing))

    def test_repeated_activities_never_duplicate_a_missing_stage(self) -> None:
        evidence = self._evidence("received", "received", "unit_claimed", "unit_claimed")

        missing = missing_required_stages(evidence)

        assert missing == [
            stage for stage in REQUIRED_STAGES if stage not in {"received", "unit_claimed"}
        ]

    def test_unavailable_satisfies_baked(self) -> None:
        missing = missing_required_stages(self._evidence("unavailable"))

        assert "baked" not in missing

    def test_elapsed_is_never_zero_clamped(self) -> None:
        assert elapsed_ms(LAST_MODIFIED, LAST_MODIFIED) == 0
        assert elapsed_ms(LAST_MODIFIED, LAST_MODIFIED + timedelta(seconds=1.5)) == 1500
        assert elapsed_ms(LAST_MODIFIED, LAST_MODIFIED - timedelta(milliseconds=1)) is None
        evidence = [
            StageEvidence(
                "materialized",
                StageStatus.APPLIED,
                "a",
                "x",
                LAST_MODIFIED - timedelta(seconds=1),
                "graph",
            )
        ]
        rows, gaps = stage_rows(evidence, LAST_MODIFIED)
        assert rows[0]["flow_root_elapsed_ms"] is None
        assert gaps == ["materialized:clock_invalid"]

    def test_an_over_bound_tree_is_explanation_too_large(self) -> None:
        with pytest.raises(ExplanationInvalidError) as refused:
            require_document_bound({"x": "y" * 300_000}, 1)
        assert refused.value.refusal is ExplanationRefusal.EXPLANATION_TOO_LARGE


class TestExplain:
    def test_the_manifest_version_returns_the_shadow_record_its_receipts_and_root_received(
        self,
    ) -> None:
        seeded = _projected()
        before = seeded.graph.writes()

        document = explain(parse_explanation_request(_manifest_body()), _deps(seeded))

        (graph,) = document["graphs"]
        assert (graph["authority_epoch"], graph["label"], graph["state"]) == (
            0,
            "shadow",
            "SHADOW_RECORDED",
        )
        assert graph["graph_id"] == seeded.receipts[0].graph_id
        assert [r["request_id"] for r in document["receipts"]] == ["p1-first", "p1-second"]
        assert [r["decision"] for r in document["receipts"]] == ["ADMITTED", "JOINED"]
        assert all(r["label"] == "shadow" and r["attested"] for r in document["receipts"])
        (received,) = document["stages"]
        assert (received["stage"], received["flow_root_elapsed_ms"]) == ("received", 0)
        assert received["occurred_at"] == LAST_MODIFIED.isoformat()
        assert received["label"] == "shadow"
        assert document["root_flow_id"] == seeded.receipts[0].root_flow_id
        assert document["missing_required_stages"] == list(REQUIRED_STAGES[1:])
        assert document["complete"] is False
        assert document["authority_mode"] == {"mode": "SHADOW", "revision": 1}
        assert document["next_cursor"] is None
        assert seeded.graph.writes() == before

    def test_bf5_two_pages_traverse_with_no_duplicate_or_omission_and_zero_writes(self) -> None:
        seeded = _projected(("p1-req-a", "p1-req-b", "p1-req-c"))
        mac = FakeMac()
        deps = _deps(seeded, mac)
        writes, puts = seeded.graph.writes(), seeded.status.puts

        first = explain(parse_explanation_request(_manifest_body(limit=2)), deps)
        retry = explain(parse_explanation_request(_manifest_body(limit=2)), deps)
        second = explain(
            parse_explanation_request(_manifest_body(limit=2, cursor=first["next_cursor"])), deps
        )

        assert first["next_cursor"] == retry["next_cursor"]  # byte-identical on retry
        seen = [r["request_id"] for r in first["receipts"]] + [
            r["request_id"] for r in second["receipts"]
        ]
        assert seen == ["p1-req-a", "p1-req-b", "p1-req-c"]
        assert second["next_cursor"] is None
        assert (seeded.graph.writes(), seeded.status.puts) == (writes, puts)

    def test_bf5_an_oversize_continuation_is_refused_before_any_read(self) -> None:
        seeded = _projected()
        writes, queries = seeded.graph.writes(), len(seeded.status.queries)
        oversize = "A" * (MAX_CURSOR_BYTES + 1)

        with pytest.raises(CursorCapacityLimitedError):
            explain(parse_explanation_request(_manifest_body(cursor=oversize)), _deps(seeded))

        assert seeded.graph.writes() == writes
        assert len(seeded.status.queries) == queries
        assert seeded.graph.batch_reads == 0

    def test_a_cursor_for_another_root_is_invalid(self) -> None:
        seeded = _projected(("p1-req-a", "p1-req-b", "p1-req-c"))
        deps = _deps(seeded)
        first = explain(parse_explanation_request(_manifest_body(limit=1)), deps)
        flow = "00000000-0000-5000-8000-000000000000"

        with pytest.raises((CursorInvalidError, ExplanationUnavailableError)):
            explain(
                parse_explanation_request({"flow_id": flow, "cursor": first["next_cursor"]}), deps
            )

    def test_the_key_arm_resolves_one_version_and_refuses_none_or_several(self) -> None:
        seeded = _projected()

        document = explain(
            parse_explanation_request({"manifest": {"key": PROBE_KEY}}), _deps(seeded)
        )
        assert document["manifest"] == {"key": PROBE_KEY, "version_id": VERSION_ID}

        two = {PROBE_KEY: [(VERSION_ID, LAST_MODIFIED), ("version-2", LAST_MODIFIED)]}
        with pytest.raises(ExplanationUnavailableError) as ambiguous:
            explain(
                parse_explanation_request({"manifest": {"key": PROBE_KEY}}),
                _deps(seeded, versions=two),
            )
        assert ambiguous.value.refusal is ExplanationRefusal.AMBIGUOUS_MANIFEST
        with pytest.raises(ExplanationUnavailableError) as absent:
            explain(
                parse_explanation_request({"manifest": {"key": PROBE_KEY}}),
                _deps(seeded, versions={}),
            )
        assert absent.value.refusal is ExplanationRefusal.MANIFEST_NOT_FOUND

    def test_an_absent_version_is_manifest_not_found(self) -> None:
        seeded = _projected()

        with pytest.raises(ExplanationUnavailableError) as refused:
            explain(
                parse_explanation_request(
                    {"manifest": {"key": PROBE_KEY, "version_id": "no-such-version"}}
                ),
                _deps(seeded),
            )

        assert refused.value.refusal is ExplanationRefusal.MANIFEST_NOT_FOUND

    def test_the_flow_arm_reads_the_projection_and_names_its_clock_gap(self) -> None:
        seeded = _projected()
        root = seeded.receipts[0].root_flow_id

        document = explain(parse_explanation_request({"flow_id": root}), _deps(seeded))

        assert [graph["graph_id"] for graph in document["graphs"]] == [seeded.receipts[0].graph_id]
        assert document["stages"][0]["occurred_at"] is None
        assert "received:root_clock_unavailable" in document["gaps"]
        with pytest.raises(ExplanationUnavailableError) as refused:
            explain(
                parse_explanation_request({"flow_id": "00000000-0000-5000-8000-000000000000"}),
                _deps(seeded),
            )
        assert refused.value.refusal is ExplanationRefusal.FLOW_NOT_FOUND

    def test_the_plan_and_node_arms_read_graph_authority(self) -> None:
        seeded = _projected()
        name = str(seeded.receipts[0].graph_id)

        plan = explain(parse_explanation_request({"plan": {"graph_id": name}}), _deps(seeded))
        node = explain(
            parse_explanation_request({"node": {"graph_id": name, "node": "write_unit"}}),
            _deps(seeded),
        )

        assert plan["focus"]["graph_state"] == "SHADOW_RECORDED"
        assert node["focus"]["after"] == ["materialize_raw"]
        assert node["focus"]["node_state"] is None
        with pytest.raises(ExplanationUnavailableError):
            explain(parse_explanation_request({"plan": {"graph_id": GRAPH_ID}}), _deps(seeded))

    def test_a_projection_row_that_no_longer_matches_authority_is_an_unattested_gap(self) -> None:
        seeded = _projected()
        key = ("RECEIPT#p1-second", "RECEIPT")
        seeded.graph.items[key] = {**seeded.graph.items[key], "reason": {"S": "changed"}}

        document = explain(parse_explanation_request(_manifest_body()), _deps(seeded))

        assert "receipt_projection_unattested:p1-second" in document["gaps"]
        attested = {r["request_id"]: r["attested"] for r in document["receipts"]}
        assert attested == {"p1-first": True, "p1-second": False}

    def test_a_graph_written_before_projection_is_still_found_from_authority(self) -> None:
        seeded = seeded_graph_store()

        document = explain(parse_explanation_request(_manifest_body()), _deps(seeded))

        (graph,) = document["graphs"]
        assert graph["projected"] is False
        assert document["receipts"] == []

    def test_a_revision_1_shadow_root_still_explains_through_its_projection_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Design §2.1: a revision-1 SHADOW graph still explains after the revision-2 bump.

        Explanation recomputes the epoch-0 graph ID at revision 2, so the revision-1 graph is
        reached only through its projection rows.
        """
        from adapters.inbound.run_requests import service  # noqa: PLC0415

        monkeypatch.setattr(service, "CONTRACT_REVISION", 1)
        seeded = seeded_graph_store()
        monkeypatch.undo()
        assert seeded.receipts[0].contract_revision == 1
        # Control: before projection the revision-1 graph is not reachable at revision 2.
        unprojected = explain(parse_explanation_request(_manifest_body()), _deps(seeded))
        assert unprojected["graphs"] == []
        seeded.project_all()
        document = explain(parse_explanation_request(_manifest_body()), _deps(seeded))
        (graph,) = document["graphs"]
        assert graph["graph_id"] == seeded.receipts[0].graph_id
        assert (graph["label"], graph["state"], graph["projected"]) == (
            "shadow",
            "SHADOW_RECORDED",
            True,
        )


# S6 part 2 (design §2.10 "N1 stages"): graph projection rows map to the lifecycle stages.

_ROOT = "root-n1"
_RUN = "run-n1"
_GRAPH = "b" * 64


def _at(second: int) -> str:
    return (LAST_MODIFIED + timedelta(seconds=second)).isoformat()


def _graph_run_rows(epoch: int = 1) -> list[GraphProjectionRow]:
    """Projection rows of one whole graph run: graph, receipt, nodes, claims and the binding."""
    base = {"root_flow_id": _ROOT, "authority_epoch": epoch}
    items: list[dict[str, Any]] = [
        {**base, "pk": f"GRAPH#{_GRAPH}", "sk": "GRAPH", "item_kind": "graph", "state": "OPEN"},
        {
            **base,
            "pk": "RECEIPT#r-n1",
            "sk": "RECEIPT",
            "item_kind": "receipt",
            "request_id": "r-n1",
            "decision": "ADMITTED",
            "graph_id": _GRAPH,
            "recorded_at": _at(1),
        },
    ]
    nodes = ("materialize_raw", "write_unit:campaigns", "grant_apply", "publish", "precompute")
    for index, node in enumerate((*nodes, "bake")):
        items.append(
            {
                **base,
                "pk": f"RUN#{_RUN}",
                "sk": f"NODE#{node}",
                "item_kind": "node",
                "state": "SUCCEEDED",
                "armed_at": _at(2 + index * 10),
                "settled_at": _at(9 + index * 10),
            }
        )
    for index, node in enumerate(("write_unit:campaigns", "bake")):
        items.append(
            {
                **base,
                "pk": f"EXEC_CLAIM#id-{node}",
                "sk": "CLAIM",
                "item_kind": "claim",
                "run_id": _RUN,
                "node": node,
                "admission": 1,
                "queue_enqueued_at": _at(3 + index * 50),
                "admitted_at": _at(4 + index * 50),
            }
        )
    items.append(
        {
            **base,
            "pk": "SERVING#g-n1",
            "sk": "BINDING",
            "item_kind": "serving_binding",
            "bound_at": _at(80),
        }
    )
    rows = [project_graph_item(item) for item in items]
    assert all(row is not None for row in rows)
    return [row for row in rows if row is not None]


def _received() -> StageEvidence:
    return StageEvidence("received", StageStatus.APPLIED, _ROOT, "root", LAST_MODIFIED, "graph")


class TestGraphStages:
    def test_a_whole_graph_run_satisfies_every_required_stage(self) -> None:
        evidence = [_received(), *graph_stage_evidence(_graph_run_rows())]
        assert missing_required_stages(evidence) == []
        _, gaps = stage_rows(evidence, LAST_MODIFIED)
        assert gaps == []
        claimed = next(item for item in evidence if item.stage == "unit_claimed")
        assert elapsed_ms(claimed.admitted_at, claimed.occurred_at) == 1_000

    def test_ac08_rows_without_plan_admitted_are_incomplete(self) -> None:
        """AC-08 (`requirements.md:334`) as an explanation-layer fixture.

        One live commit gives both `materialized` and `plan_admitted`, so only a fixture can
        drop one. Paired control: every row present gives `complete=true`.
        """
        full = [_received(), *graph_stage_evidence(_graph_run_rows())]
        without = [item for item in full if item.stage != "plan_admitted"]
        assert missing_required_stages(without) == ["plan_admitted"]
        assert (not missing_required_stages(without)) is False
        # Paired control: every row present gives `complete=true`.
        assert (not missing_required_stages(full)) is True

    def test_materialized_and_plan_admitted_come_from_one_materialize_row(self) -> None:
        stages = {item.stage: item.activity_id for item in graph_stage_evidence(_graph_run_rows())}
        assert stages["materialized"] == stages["plan_admitted"] == f"{_RUN}/materialize_raw"

    def test_shadow_rows_never_count_as_progress(self) -> None:
        assert graph_stage_evidence(_graph_run_rows(epoch=0)) == []

    def test_a_failed_bake_is_unavailable_and_still_satisfies_baked(self) -> None:
        rows = [
            row
            for row in _graph_run_rows()
            if not (row.kind is ProjectionKind.NODE and row.document["node"] == "bake")
        ]
        failed = project_graph_item(
            {
                "root_flow_id": _ROOT,
                "authority_epoch": 1,
                "pk": f"RUN#{_RUN}",
                "sk": "NODE#bake",
                "item_kind": "node",
                "state": "FAILED",
                "settled_at": _at(70),
            }
        )
        assert failed is not None
        evidence = [_received(), *graph_stage_evidence([*rows, failed])]
        assert "unavailable" in {item.stage for item in evidence}
        assert missing_required_stages(evidence) == []

    def test_a_failed_materialize_gives_neither_materialized_nor_plan_admitted(self) -> None:
        rows = [
            row
            for row in _graph_run_rows()
            if not (row.kind is ProjectionKind.NODE and row.document["node"] == "materialize_raw")
        ]
        failed = project_graph_item(
            {
                "root_flow_id": _ROOT,
                "authority_epoch": 1,
                "pk": f"RUN#{_RUN}",
                "sk": "NODE#materialize_raw",
                "item_kind": "node",
                "state": "FAILED",
                "settled_at": _at(9),
            }
        )
        assert failed is not None
        missing = missing_required_stages([_received(), *graph_stage_evidence([*rows, failed])])
        assert missing == ["materialized", "plan_admitted"]

    def test_a_reclaim_is_unit_retrying_not_a_second_dispatch(self) -> None:
        reclaim = project_graph_item(
            {
                "root_flow_id": _ROOT,
                "authority_epoch": 1,
                "pk": "EXEC_CLAIM#id-write_unit:campaigns",
                "sk": "CLAIM",
                "item_kind": "claim",
                "node": "write_unit:campaigns",
                "admission": 2,
                "queue_enqueued_at": _at(3),
                "admitted_at": _at(40),
            }
        )
        assert reclaim is not None
        stages = [item.stage for item in graph_stage_evidence([reclaim])]
        assert stages == ["unit_retrying"]

    def test_the_explanation_reads_execution_rows_and_shows_materialized_applied(self) -> None:
        seeded = _projected()
        root = seeded.receipts[0].root_flow_id
        before = explain(parse_explanation_request(_manifest_body()), _deps(seeded))
        assert "materialized" in before["missing_required_stages"]
        node = project_graph_item(
            {
                "root_flow_id": root,
                "authority_epoch": 1,
                "pk": f"RUN#{_RUN}",
                "sk": "NODE#materialize_raw",
                "item_kind": "node",
                "state": "SUCCEEDED",
                "armed_at": _at(2),
                "settled_at": _at(9),
            }
        )
        assert node is not None
        seeded.projection.apply(node)
        document = explain(parse_explanation_request(_manifest_body()), _deps(seeded))
        materialized = [row for row in document["stages"] if row["stage"] == "materialized"]
        assert [row["status"] for row in materialized] == ["APPLIED"]
        assert "materialized" not in document["missing_required_stages"]
        assert "plan_admitted" not in document["missing_required_stages"]


# --- Serving coalescing Slice 1: closed status fields and COALESCED applied semantics ---------


def test_coalesced_workers_are_applied_stages_and_never_node_failures() -> None:
    rows: list[GraphProjectionRow] = []
    for name in ("precompute", "bake"):
        row = project_graph_item(
            {
                "root_flow_id": _ROOT,
                "authority_epoch": 1,
                "pk": f"RUN#{_RUN}",
                "sk": f"NODE#{name}",
                "item_kind": "node",
                "node_id": f"node-{name}",
                "phase": "POST_GRANT",
                "state": "COALESCED",
                "settled_at": _at(70),
                "cohort_revision": "cohort-1",
                "serving_input_revision": "input-1",
                "winner_run_id": "run-winner",
                "binding_kind": "alias",
                "direct_binding_id": "direct-1",
            }
        )
        assert row is not None
        rows.append(row)
    evidence = graph_stage_evidence(rows)
    assert {item.stage for item in evidence} == {"precompute_settled", "baked"}
    rendered, _ = stage_rows(evidence, LAST_MODIFIED)
    assert all(row["cohort_revision"] == "cohort-1" for row in rendered)
    assert all(row["serving_input_revision"] == "input-1" for row in rendered)
    assert all(row["binding_kind"] == "alias" for row in rendered)
    assert node_failures(rows) == []


def test_retired_day_and_capacity_fields_never_reach_stage_evidence_or_rows() -> None:
    """Plan item 4: an old image carrying day/capacity fields renders only rolling fields."""
    retired = {
        "serving_day": "2026-10-12",
        "reservation_seconds": 0,
        "capacity_state": "ROLLED_OVER",
        "capacity_reason": "serving_execution_budget_exhausted",
        "rollover_count": 1,
        "refusal_reason": "serving_execution_budget_exhausted",
    }
    row = project_graph_item(
        {
            "root_flow_id": _ROOT,
            "authority_epoch": 1,
            "pk": f"RUN#{_RUN}",
            "sk": "NODE#bake",
            "item_kind": "node",
            "node_id": "node-bake",
            "phase": "POST_GRANT",
            "state": "COALESCED",
            "settled_at": _at(70),
            "cohort_revision": "cohort-1",
            **retired,
        }
    )
    assert row is not None
    evidence = graph_stage_evidence([row])
    assert [item.stage for item in evidence] == ["baked"]
    for item in evidence:
        assert all(not hasattr(item, name) for name in retired)
    rendered, _ = stage_rows(evidence, LAST_MODIFIED)
    assert rendered and all(set(retired).isdisjoint(entry) for entry in rendered)
    # Control: the rolling cohort field on the same image is carried through.
    assert all(entry["cohort_revision"] == "cohort-1" for entry in rendered)


@pytest.mark.parametrize("name", ("materialize_raw", "write_unit:campaigns", "publish"))
def test_coalesced_non_serving_nodes_do_not_assert_applied_stages(name: str) -> None:
    row = project_graph_item(
        {
            "root_flow_id": _ROOT,
            "authority_epoch": 1,
            "pk": f"RUN#{_RUN}",
            "sk": f"NODE#{name}",
            "item_kind": "node",
            "node_id": f"node-{name}",
            "state": "COALESCED",
            "settled_at": _at(70),
        }
    )
    assert row is not None
    assert graph_stage_evidence([row]) == []


# --- Slice 7 (1b, D35, D37; tests 12d and 12e) --------------------------------------------------


def _unit_rows(*, state: str, batch: str | None, **extra: Any) -> list[GraphProjectionRow]:
    base = {"root_flow_id": _ROOT, "authority_epoch": 1}
    node: dict[str, Any] = {
        **base,
        "pk": f"RUN#{_RUN}",
        "sk": "NODE#write_unit:orders",
        "item_kind": "node",
        "node_id": "n-orders",
        "phase": "PRE_GRANT",
        "state": state,
        "settled_at": _at(30),
        **extra,
    }
    if batch is not None:
        node["batch_id"] = batch
    claims = [
        {
            **base,
            "pk": "EXEC_CLAIM#n-orders",
            "sk": "CLAIM",
            "item_kind": "claim",
            "run_id": _RUN,
            "node": "write_unit:orders",
            "node_id": "n-orders",
            "admission": admission,
            "batch_id": f"batch-{admission}",
            "queue_enqueued_at": _at(admission),
            "admitted_at": _at(admission + 1),
        }
        for admission in (1, 2)
    ]
    rows = [project_graph_item(item) for item in (node, *claims)]
    return [row for row in rows if row is not None]


def test_slice7_12d_each_attempt_and_the_attested_row_carry_their_batch() -> None:
    stages, _gaps = stage_rows(
        graph_stage_evidence(_unit_rows(state="SUCCEEDED", batch="batch-2")), None
    )
    by_stage = {row["stage"]: row["batch_id"] for row in stages}
    assert by_stage["unit_claimed"] == "batch-1"
    assert by_stage["unit_retrying"] == "batch-2"
    assert by_stage["unit_attested"] == "batch-2"
    # Control: a non-unit stage row's batch_id is null.
    assert by_stage["units_dispatched"] is None
    assert all("batch_id" in row for row in stages)


def test_slice7_12d_a_failed_member_has_one_complete_node_failure_entry() -> None:
    rows = _unit_rows(
        state="FAILED",
        batch="batch-2",
        failure_reason="statement_failed",
        failure_code="ADLY-FICTIONAL-0001",
    )
    assert node_failures(rows) == [
        {
            "node": "write_unit:orders",
            "node_id": "n-orders",
            "failure_reason": "statement_failed",
            "failure_code": "ADLY-FICTIONAL-0001",
            "batch_id": "batch-2",
            "remedy": "operator_rerun",
        }
    ]
    # Controls: fenced while PENDING after a return carries no batch; none failed is empty.
    (fenced,) = node_failures(_unit_rows(state="FAILED", batch=None, failure_reason="x"))
    assert fenced["batch_id"] is None
    assert node_failures(_unit_rows(state="SUCCEEDED", batch="batch-1")) == []
    assert node_failures(_graph_run_rows()) == []


def test_slice7_12d_a_failed_node_kind_without_a_remedy_is_an_invariant() -> None:
    from domain.shared_kernel.errors import InvariantError  # noqa: PLC0415

    rows = _unit_rows(state="FAILED", batch="b", phase="POST_GRANT")
    with pytest.raises(InvariantError) as refused:
        node_failures(rows)
    assert str(refused.value.code) == "ADLY-FLOW_EXPLAIN-0001"
    # Control: the PRE_GRANT phase maps to its remedy.
    assert node_failures(_unit_rows(state="FAILED", batch="b"))[0]["remedy"] == "operator_rerun"


def test_slice7_p6_an_old_terminal_image_projects_to_the_same_document() -> None:
    item = {
        "root_flow_id": _ROOT,
        "authority_epoch": 1,
        "pk": f"RUN#{_RUN}",
        "sk": "NODE#write_unit:orders",
        "item_kind": "node",
        "state": "SUCCEEDED",
        "settled_at": _at(9),
    }
    first, again = project_graph_item(dict(item)), project_graph_item(dict(item))
    assert first is not None and again is not None and first.document == again.document
    # The pre-1b image has none of the new fields, so its document gains none of them.
    assert not {"batch_id", "failure_code", "failure_reason", "arm_alone"} & set(first.document)


class TestSlice7Document:
    def test_12e_explain_returns_node_failures_as_a_top_level_list(self) -> None:
        document = explain(parse_explanation_request(_manifest_body()), _deps(_projected()))
        assert document["node_failures"] == []

    def test_12e_an_oversize_node_failures_list_is_refused_and_a_small_one_passes(self) -> None:
        entry = {"node": "write_unit:x", "failure_reason": "y" * 1_000}
        with pytest.raises(ExplanationInvalidError) as refused:
            require_document_bound({"node_failures": [entry] * 300}, 1)
        assert refused.value.refusal is ExplanationRefusal.EXPLANATION_TOO_LARGE
        require_document_bound({"node_failures": [entry] * 3}, 1)
