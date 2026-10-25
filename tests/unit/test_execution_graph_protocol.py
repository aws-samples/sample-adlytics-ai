# SPDX-License-Identifier: MIT-0
"""The one immutable protocol version (BF-3), adoption refusal (V-4) and the generated contract.

A command with any version other than v1, and any live-migration command, is refused before any
read-modify-write with `PROTOCOL_MIGRATION_UNSUPPORTED`: no receipt and zero graph writes.
"""

from __future__ import annotations

import hashlib
import pathlib
from typing import cast

import pytest
from execution_graph_support import FakeDynamo, FakeManifests, fixed_now

from adapters.inbound.run_requests.service import (
    AdmissionDependencies,
    RunRequest,
    admit_run_request,
)
from adapters.jobs.data_flow_registry import execution_graphs as generator
from adapters.outbound.dynamodb.execution_graph import DynamoExecutionGraphStore
from domain.operations.execution_graph import (
    AuthorityMode,
    GraphCommandRefusedError,
    epoch_for_mode,
    require_no_legacy_source,
    require_protocol_v1,
)
from domain.operations.generated_execution_graphs import (
    BOUNDS,
    CONTRACT_DIGEST,
    CONTRACT_REVISION,
    CUTOVER,
    PROTOCOL_VERSION,
    ROUTES,
    SERVING_COALESCING,
    TEMPLATES,
    WRITE_UNIT_BATCHING,
)
from domain.operations.ingestion_root import window_identity

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-074")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
KEY = (
    # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    "amc/dsp_impressions/query_pack=amc-dsp-impressions/source_scope=s/marketplace=ATVPDKIKX0DER/"
    "window=2026-10-04_2026-10-06/options=44136fa355b3678a/retrieved=20261006T092520Z/manifest.json"
)


def test_bf3_any_version_other_than_v1_and_any_migration_is_refused() -> None:
    assert PROTOCOL_VERSION == "v1"
    require_protocol_v1("v1")
    for version, migration in (("v2", False), ("v0", False), ("", False), ("v1", True)):
        with pytest.raises(GraphCommandRefusedError) as raised:
            require_protocol_v1(version, migration=migration)
        assert raised.value.signal == "PROTOCOL_MIGRATION_UNSUPPORTED"


def test_bf3_a_non_v1_run_request_writes_nothing_and_v1_commits() -> None:
    client = FakeDynamo()
    client.items[("MODE#ingestion", "MODE")] = {
        "pk": {"S": "MODE#ingestion"},
        "sk": {"S": "MODE"},
        "mode": {"S": "SHADOW"},
        "revision": {"N": "1"},
    }
    store = DynamoExecutionGraphStore("graph", client)  # type: ignore[arg-type]
    deps = AdmissionDependencies(
        store, FakeManifests(retrieved_at=window_identity(KEY).retrieved_at), fixed_now
    )

    def request(version: str) -> RunRequest:
        return RunRequest(
            request_id=f"request-{version or 'blank'}-0001",
            protocol_version=version,
            caller_kind="on_demand",
            reason="operator_rerun",
            manifest_key=KEY,
            version_id="v1",
            requester="arn:aws:iam::000000000000:user/operator",
            requester_is_iam=True,
        )

    with pytest.raises(GraphCommandRefusedError):
        admit_run_request(request("v2"), deps)
    assert client.transactions == []
    assert admit_run_request(request("v1"), deps).decision == "ADMITTED"
    assert len(client.transactions) == 1


def test_v4_a_command_naming_a_legacy_plan_intent_or_unit_is_refused() -> None:
    for source in ("WRITE_PLAN#abc", "x/INTENT#i", "UNIT#u"):
        with pytest.raises(GraphCommandRefusedError) as raised:
            require_no_legacy_source(source)
        assert raised.value.signal == "LEGACY_ADOPTION_UNSUPPORTED"
    require_no_legacy_source(KEY, "version-1")


def test_epochs_are_0_outside_graph_and_1_in_graph() -> None:
    assert [epoch_for_mode(mode) for mode in AuthorityMode] == [0, 0, 0, 1]


def test_the_generated_projections_are_current_and_bound_to_the_contract_digest() -> None:
    contract = generator.load_contract(ROOT)
    digest = hashlib.sha256((ROOT / generator.CONTRACT_PATH).read_bytes()).hexdigest()
    assert digest == CONTRACT_DIGEST
    assert (ROOT / generator.PYTHON_PATH).read_text() == generator.render_python(contract, digest)
    assert (ROOT / generator.TYPESCRIPT_PATH).read_text() == generator.render_typescript(
        contract, digest
    )
    generator.check_total(contract)


def _contract_with(tmp_path: pathlib.Path, edit: str, replacement: str) -> pathlib.Path:
    text = (ROOT / generator.CONTRACT_PATH).read_text()
    assert edit in text
    target = tmp_path / generator.CONTRACT_PATH
    target.parent.mkdir(parents=True)
    target.write_text(text.replace(edit, replacement, 1))
    return tmp_path


@pytest.mark.parametrize(
    ("edit", "replacement"),
    [
        ("protocol_ceiling_seconds: 7200", "protocol_ceiling_seconds: 7201"),
        ("max_redecide: 8", "max_redecide: 9"),
        ("  due_shards: 16\n", "  due_shards: 16\n  new_bound: 1\n"),
    ],
)
def test_bf3_a_changed_bound_or_ceiling_under_v1_fails_generation(
    tmp_path: pathlib.Path, edit: str, replacement: str
) -> None:
    root = _contract_with(tmp_path, edit, replacement)
    with pytest.raises(generator.ExecutionGraphContractError, match="PROTOCOL_MIGRATION"):
        generator.load_contract(root)


def test_bf3_a_new_protocol_version_has_no_pinned_bounds_and_fails_generation(
    tmp_path: pathlib.Path,
) -> None:
    root = _contract_with(tmp_path, "protocol_version: v1", "protocol_version: v2")
    with pytest.raises(generator.ExecutionGraphContractError, match="PROTOCOL_MIGRATION"):
        generator.load_contract(root)


def test_bf3_positive_the_v1_bounds_match_their_pin_and_a_comment_edit_passes(
    tmp_path: pathlib.Path,
) -> None:
    contract = generator.load_contract(ROOT)
    assert generator.bounds_digest(contract["bounds"]) == generator.PINNED_BOUNDS["v1"]
    root = _contract_with(tmp_path, "bounds:\n", "bounds:\n  # an editorial comment\n")
    assert generator.load_contract(root)["bounds"] == contract["bounds"]


def test_the_pinned_replay_band_equals_the_planner_band() -> None:
    assert generator._planner_findings(ROOT, generator.load_contract(ROOT)) == []  # pyright: ignore[reportPrivateUsage]


def test_revision_2_generates_the_routes_and_each_node_route_to_both_projections() -> None:
    """Design §2.1 (D4): `route` per node and the `routes` block, in Python and TypeScript."""
    assert CONTRACT_REVISION == 2
    assert ROUTES == {
        "delivery": {"max_receive_count": 3},
        "semantic_write": {"max_receive_count": 3},
        "bake": {"max_receive_count": 3},
        "precompute": {"max_receive_count": 3},
    }
    (template,) = TEMPLATES.values()
    nodes = {str(node["node"]): node for node in template["nodes"]}
    assert list(nodes) == [
        "materialize_raw",
        "write_unit",
        "grant_apply",
        "publish",
        "bake",
        "precompute",
        "bake_task",
        "bake_finalize",
    ]
    assert {name: node.get("route") for name, node in nodes.items()} == {
        "materialize_raw": "delivery",
        "write_unit": "semantic_write",
        "grant_apply": None,
        "publish": "semantic_write",
        "precompute": "precompute",
        "bake": "bake",
        "bake_task": "bake",
        "bake_finalize": "bake",
    }
    assert nodes["write_unit"].get("dynamic") is True
    # Plan 4.6: the fan-out puts the roster tasks and the finalizer; apply never does.
    assert nodes["bake_task"].get("dynamic") is nodes["bake_finalize"].get("dynamic") is True
    assert (nodes["bake_task"]["after"], nodes["bake_finalize"]["after"]) == (
        ("bake",),
        ("bake_task",),
    )
    # DB25: publish wakes bake first; precompute waits on the exact roster effects.
    assert (nodes["bake"]["after"], nodes["precompute"]["after"]) == (("publish",), ("bake_task",))
    assert "delivery_queue_max_receive_count" not in CUTOVER
    typescript = (ROOT / generator.TYPESCRIPT_PATH).read_text()
    assert (
        'export const ROUTES = {"bake": {"max_receive_count": 3}, "delivery": '
        '{"max_receive_count": 3}, "precompute": {"max_receive_count": 3}, "semantic_write": '
        '{"max_receive_count": 3}} as const;'
    ) in typescript
    assert '"materialize_raw": "delivery"' in typescript


def test_serving_coalescing_has_dedicated_closed_controls_without_moving_v1_bounds() -> None:
    assert SERVING_COALESCING == {
        "serving_candidate_roster_max": 512,
        "serving_frontier_shards": 32,
        "next_election_min_remaining_ms": 10493,
        "binding_kinds": ("direct", "alias"),
    }
    assert "serving_candidate_roster_max" not in BOUNDS
    assert "serving_frontier_shards" not in BOUNDS
    assert BOUNDS["max_window_retrievals"] == 512
    assert BOUNDS["max_transaction_items"] == 100
    assert BOUNDS["due_shards"] == 16
    assert BOUNDS["protocol_ceiling_seconds"] == 7_200
    assert 15 + 3 + SERVING_COALESCING["serving_frontier_shards"] == 50
    assert BOUNDS["max_transaction_items"] >= 50
    assert SERVING_COALESCING["serving_frontier_shards"] + 8 == 40
    assert BOUNDS["max_transaction_items"] >= 40
    assert {route["max_receive_count"] for route in ROUTES.values()} == {3}


def test_revision_2_keeps_the_v1_bounds_digest() -> None:
    """The bump is a pre-cutover revision, not a protocol version: the bounds are unchanged."""
    contract = generator.load_contract(ROOT)
    assert generator.bounds_digest(contract["bounds"]) == generator.PINNED_BOUNDS["v1"]
    assert generator.PINNED_BOUNDS["v1"] == (
        # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
        "fc05393dde7138d81eb334ba479c2d0c37be7dc52408d33f3814666446b63800"
    )


def test_a_node_naming_an_unknown_route_is_refused_and_a_known_route_generates(
    tmp_path: pathlib.Path,
) -> None:
    root = _contract_with(
        tmp_path,
        "{node: bake, after: [publish], phase: POST_GRANT, route: bake}",
        "{node: bake, after: [publish], phase: POST_GRANT, route: bake_lane}",
    )
    with pytest.raises(generator.ExecutionGraphContractError, match="ROUTE_UNKNOWN") as refused:
        generator.load_contract(root)
    assert "undeclared route 'bake_lane'" in str(refused.value)
    # Control: the declared route generates.
    assert (
        generator.node_routes(generator.load_contract(ROOT))["ingestion_manifest_to_served.v1"][
            "bake"
        ]
        == "bake"
    )


_ALLOWANCE = "  statement_allowance_ms: 45000\n"


def _no_planner_findings(_root: pathlib.Path, _contract: dict[str, object]) -> list[str]:
    return []


def test_slice7_12c_the_allowance_is_generated_and_pinned_to_the_d42_value() -> None:
    """D42: 2 x 22,193 ms rounded up to the next 1,000 ms is 45,000 ms (design §2.5 step 4)."""
    assert WRITE_UNIT_BATCHING == {"statement_allowance_ms": 45_000}
    contract = generator.load_contract(ROOT)
    generator.require_deployable(contract)
    # The block sits outside `bounds`, so the v1 bounds digest is unchanged.
    assert generator.bounds_digest(contract["bounds"]) == generator.PINNED_BOUNDS["v1"]
    assert "WRITE_UNIT_BATCHING" not in (ROOT / generator.TYPESCRIPT_PATH).read_text()


def test_slice7_12c_a_null_allowance_is_refused_by_the_deploy_gate_only(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _contract_with(tmp_path, _ALLOWANCE, "  statement_allowance_ms: null\n")
    contract = generator.load_contract(root)
    with pytest.raises(generator.ExecutionGraphContractError) as refused:
        generator.require_deployable(contract)
    assert refused.value.code == generator.PENDING_PROBE == "WRITE_UNIT_BATCHING_PENDING_PROBE"
    # The planner pin reads repository fixtures the contract copy lacks; it is checked on ROOT.
    monkeypatch.setattr(generator, "_planner_findings", _no_planner_findings)
    assert generator.main([], root_override=root) == 0
    capsys.readouterr()
    assert generator.main(["--check", "--deployable"], root_override=root) == 1
    assert generator.PENDING_PROBE in capsys.readouterr().err
    # Control: `--check` alone accepts the null contract, so the slice builds.
    assert generator.main(["--check"], root_override=root) == 0


def test_slice7_12c_a_positive_allowance_passes_the_deploy_gate(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _contract_with(tmp_path, _ALLOWANCE, "  statement_allowance_ms: 1\n")
    monkeypatch.setattr(generator, "_planner_findings", _no_planner_findings)
    assert generator.main(["--deployable"], root_override=root) == 0
    assert generator.main(["--deployable", "--check"], root_override=root) == 0
    assert generator.main(["--check", "--other"], root_override=root) == 2


@pytest.mark.parametrize(
    "replacement",
    [
        "  statement_allowance_ms: 0\n",
        "  statement_allowance_ms: -1\n",
        '  statement_allowance_ms: "45000"\n',
        "  statement_allowance_ms: true\n",
        "  statement_allowance_ms: 45000\n  extra: 1\n",
        "  other_key: 45000\n",
    ],
)
def test_slice7_12c_a_malformed_allowance_is_refused_by_both_checks(
    tmp_path: pathlib.Path, replacement: str
) -> None:
    root = _contract_with(tmp_path, _ALLOWANCE, replacement)
    with pytest.raises(generator.ExecutionGraphContractError) as refused:
        generator.load_contract(root)
    assert refused.value.code == generator.BATCHING_INVALID
    assert generator.main(["--check"], root_override=root) == 1
    assert generator.main(["--check", "--deployable"], root_override=root) == 1


def test_slice7_12c_a_missing_block_is_refused(tmp_path: pathlib.Path) -> None:
    root = _contract_with(tmp_path, "write_unit_batching:\n" + _ALLOWANCE, "")
    with pytest.raises(generator.ExecutionGraphContractError) as refused:
        generator.load_contract(root)
    assert refused.value.code == generator.BATCHING_INVALID
    # Control: the repository contract carries the block and loads.
    assert generator.load_contract(ROOT)["write_unit_batching"] == {
        "statement_allowance_ms": 45_000
    }


def test_a_route_without_a_positive_receive_count_is_refused(tmp_path: pathlib.Path) -> None:
    root = _contract_with(
        tmp_path, "  bake: {max_receive_count: 3}", "  bake: {max_receive_count: 0}"
    )
    with pytest.raises(generator.ExecutionGraphContractError, match="ROUTE_UNKNOWN"):
        generator.load_contract(root)


# --- GRAPH bake stage 1 (plan 3.1, A1.1, F4, F6): roster and start-budget constants ----------


def test_the_bake_roster_and_start_budget_are_generated_with_exact_values() -> None:
    from domain.operations.generated_execution_graphs import (  # noqa: PLC0415
        ATHENA_START_BUDGET,
        BAKE_TASKS,
    )

    assert [f"{row['page']}#{row['variant']}" for row in BAKE_TASKS["roster"]] == [
        "campaigns#1d",
        "campaigns#7d",
        "campaigns#14d",
        "campaigns#30d",
        "retail-health#1d",
        "retail-health#7d",
        "retail-health#14d",
        "scorecard#7d",
        "scorecard#30d",
        "scorecard#last_complete_week",
        "scorecard#last_complete_month",
        "new-customers#7d",
        "new-customers#30d",
        "new-customers#last_complete_week",
        "new-customers#last_complete_month",
        "reach#last_complete_week",
        "reach#last_complete_month",
        "reach#last_complete_quarter",
        "retention#latest_with_current_publication",
        "audiences#last_complete_month",
    ]
    assert list(BAKE_TASKS["budget_seconds"].values()) == [
        570,
        570,
        570,
        570,
        240,
        240,
        240,
        120,
        120,
        120,
        120,
        90,
        90,
        150,
        150,
        90,
        180,
        150,
        90,
        600,
    ]
    # Audiences' parts and Campaigns' segments grow with campaigns, so each write term is the
    # whole write allowance (ADR-152).
    assert BAKE_TASKS["write_seconds"]["audiences"] == 450.0
    assert BAKE_TASKS["write_seconds"]["campaigns"] == 450.0
    assert {
        value
        for page, value in BAKE_TASKS["write_seconds"].items()
        if page not in {"audiences", "campaigns"}
    } == {16.6}
    assert BAKE_TASKS["baker_memory_mib"] == 2048
    assert len(BAKE_TASKS["write_seconds"]) == 7
    assert (BAKE_TASKS["max_tasks"], BAKE_TASKS["certified_query_cap"], BAKE_TASKS["lanes"]) == (
        21,
        10,
        2,
    )
    assert BAKE_TASKS["lane_groups"] == ("preset-bake-lane-0", "preset-bake-lane-1")
    assert 10 + 4 * BAKE_TASKS["max_tasks"] <= BOUNDS["max_transaction_items"]
    assert ATHENA_START_BUDGET == {
        "active_dml_quota": 200,
        "batch_athena_budget": 41,
        "chat_reserve_active_dml": 20,
    }
    typescript = (ROOT / generator.TYPESCRIPT_PATH).read_text()
    assert '"max_tasks": 21' in typescript
    assert '"batch_athena_budget": 41' in typescript


_ROSTER_ROW = "    - {page: retail-health, variant: 14d}\n"
_BUDGET_ROW = '    "retail-health#14d": 240\n'


@pytest.mark.parametrize(
    ("code", "edit", "replacement"),
    [
        # Seven budgets with six roster rows: a budget without its row (the reverse direction).
        ("BAKE_ROSTER_COVERAGE", _ROSTER_ROW, ""),
        # A roster row with no budget (the A1.1 control).
        ("BAKE_ROSTER_COVERAGE", _BUDGET_ROW, ""),
        (
            "BAKE_ROSTER_COVERAGE",
            "    audiences: 450.0\n",
            "",
        ),
        ("BAKE_VALUE_PRECISION", "    retail-health: 16.6\n", "    retail-health: 16.65\n"),
        ("BAKE_ROSTER_OVER_MAX_TASKS", "  max_tasks: 21\n", "  max_tasks: 6\n"),
        ("BAKE_MAX_TASKS_OVER_TRANSACTION_BOUND", "  max_tasks: 21\n", "  max_tasks: 23\n"),
        (
            "BAKE_MAX_TASKS_OVER_TRANSACTION_BOUND",
            "  final_authority_members: 10\n",
            "  final_authority_members: 17\n",
        ),
        ("BAKE_CERTIFIED_PLAN_COUNTS_INVALID", '    "campaigns#1d": 26\n', ""),
        (
            "BAKE_CERTIFIED_PLAN_COUNTS_INVALID",
            '    "campaigns#1d": 26\n',
            '    "campaigns#1d": 0\n',
        ),
        (
            "BAKE_CERTIFIED_PLAN_COUNT_OVER_BUDGET",
            '    "campaigns#1d": 26\n',
            '    "campaigns#1d": 61\n',
        ),
        (
            "bake_winner_over_root_bound",
            '    "retail-health#14d": 240\n',
            '    "retail-health#14d": 7000\n',
        ),
        (
            "ATHENA_START_BUDGET_INVALID",
            "  batch_athena_budget: 41\n",
            "  batch_athena_budget: 0\n",
        ),
    ],
)
def test_bake_generation_refuses_each_named_defect(  # noqa: PLR0913, PLR0917 -- parametrized
    tmp_path: pathlib.Path, code: str, edit: str, replacement: str
) -> None:
    root = _contract_with(tmp_path, edit, replacement)
    with pytest.raises(generator.ExecutionGraphContractError) as refused:
        generator.load_contract(root)
    assert refused.value.code == code
    # Paired control: the repository contract, without the edit, loads.
    assert len(generator.load_contract(ROOT)["bake_tasks"]["roster"]) == 20


_FINAL_TERMS = (
    "  final_authority_members: 10\n"
    "  final_task_condition_members: 1\n"
    "  final_written_pointer_members: 3\n"
)


def _certified(*keys: str, count: int = 1) -> str:
    """Return a `certified_plan_counts` block for `keys` plus the final-transaction terms."""
    entries = "".join(f'    "{key}": {count}\n' for key in keys)
    return f"  certified_plan_counts:\n{entries}{_FINAL_TERMS}"


def _roster_of(rows: int) -> str:
    """Return a roster of `rows` campaigns rows with their budgets (the max_tasks bound)."""
    roster = "".join(f"    - {{page: campaigns, variant: v{index}}}\n" for index in range(rows))
    budgets = "".join(f'    "campaigns#v{index}": 100\n' for index in range(rows))
    horizons = "".join(f'    "campaigns#v{index}": 123\n' for index in range(rows))
    return (
        f"  roster:\n{roster}  budget_seconds:\n{budgets}  write_seconds: {{campaigns: 1.6}}\n"
        f"  source_horizon_days:\n{horizons}"
        + _certified(*(f"campaigns#v{index}" for index in range(rows)))
    )


def _roster_block() -> str:
    text = (ROOT / generator.CONTRACT_PATH).read_text()
    start = text.index("  roster:\n")
    end = text.index("  per_query_seconds:")
    return text[start:end]


def test_a_22_row_roster_is_refused_and_the_21_row_ceiling_generates(
    tmp_path: pathlib.Path,
) -> None:
    over = _contract_with(tmp_path / "over", _roster_block(), _roster_of(22))
    with pytest.raises(generator.ExecutionGraphContractError) as refused:
        generator.load_contract(over)
    assert refused.value.code == "BAKE_ROSTER_OVER_MAX_TASKS"
    ceiling = _contract_with(tmp_path / "ceiling", _roster_block(), _roster_of(21))
    assert len(generator.load_contract(ceiling)["bake_tasks"]["roster"]) == 21


def test_two_pages_with_one_normalized_id_are_refused_and_distinct_pages_generate(
    tmp_path: pathlib.Path,
) -> None:
    colliding = (
        "  roster:\n    - {page: retail-health, variant: 1d}\n"
        "    - {page: retail_health, variant: 1d}\n"
        '  budget_seconds:\n    "retail-health#1d": 420\n    "retail_health#1d": 420\n'
        "  write_seconds: {retail-health: 1.6, retail_health: 1.6}\n"
        '  source_horizon_days:\n    "retail-health#1d": 106\n    "retail_health#1d": 106\n'
        + _certified("retail-health#1d", "retail_health#1d")
    )
    root = _contract_with(tmp_path / "collide", _roster_block(), colliding)
    with pytest.raises(generator.ExecutionGraphContractError) as refused:
        generator.load_contract(root)
    assert refused.value.code == "BAKE_PAGE_ID_COLLISION"
    distinct = colliding.replace("retail_health", "retail-vendor")
    control = _contract_with(tmp_path / "distinct", _roster_block(), distinct)
    assert len(generator.load_contract(control)["bake_tasks"]["roster"]) == 2


@pytest.mark.parametrize(
    "horizons",
    [
        "",
        '  source_horizon_days:\n    "campaigns#v0": 123\n',
        '  source_horizon_days:\n    "campaigns#v0": 123\n    "campaigns#v1": 0\n',
        '  source_horizon_days:\n    "campaigns#v0": 123\n    "campaigns#v1": true\n',
        '  source_horizon_days:\n    "campaigns#v0": 123\n    "campaigns#v1": 12.5\n',
        '  source_horizon_days:\n    "campaigns#v0": 1\n    "campaigns#v1": 1\n'
        '    "campaigns#v2": 1\n',
    ],
)
def test_a_row_without_one_valid_source_horizon_is_refused_and_the_full_set_generates(
    tmp_path: pathlib.Path, horizons: str
) -> None:
    """A5.1 I9: every roster row has exactly one integer horizon >= 1, and no extra entry."""
    two = _roster_of(2).split("  source_horizon_days:")[0]
    certified = _certified("campaigns#v0", "campaigns#v1")
    root = _contract_with(tmp_path / "refused", _roster_block(), two + horizons + certified)
    with pytest.raises(generator.ExecutionGraphContractError) as refused:
        generator.load_contract(root)
    assert refused.value.code == "BAKE_SOURCE_HORIZON_DAYS_INVALID"
    control = _contract_with(tmp_path / "control", _roster_block(), _roster_of(2))
    assert generator.load_contract(control)["bake_tasks"]["source_horizon_days"] == {
        "campaigns#v0": 123,
        "campaigns#v1": 123,
    }


def test_the_v3_bake_vocabulary_and_per_page_unavailable_reasons_are_generated() -> None:
    """Plan 4.1 (F12): each roster page's reason is `<normalized page>_materialization_...`."""
    from domain.operations.generated_execution_graphs import (  # noqa: PLC0415
        BAKE_UNAVAILABLE_REASONS,
        BAKE_V3,
    )

    assert BAKE_V3["control_kinds"] == ("bake_fan_out", "bake_finalize")
    assert BAKE_V3["recovery_reasons"] == ("fatal", "retry_exhausted")
    assert BAKE_V3["waiting_input_state"] == "WAITING_INPUT"
    assert BAKE_UNAVAILABLE_REASONS == {
        "campaigns": "campaigns_materialization_unavailable",
        "retail-health": "retail_health_materialization_unavailable",
        "scorecard": "scorecard_materialization_unavailable",
        "new-customers": "new_customers_materialization_unavailable",
        "reach": "reach_materialization_unavailable",
        "retention": "retention_materialization_unavailable",
        "audiences": "audiences_materialization_unavailable",
    }


@pytest.mark.parametrize(
    ("edit", "replacement"),
    [
        ("recovery_reasons: [fatal, retry_exhausted]", "recovery_reasons: [fatal]"),
        ("control_kinds: [bake_fan_out, bake_finalize]", "control_kinds: [bake_fan_out]"),
        ("unavailable_reason_suffix: _materialization_unavailable", "unavailable_reason_suffix: x"),
    ],
)
def test_control_a_drifted_v3_vocabulary_refuses_generation(
    tmp_path: pathlib.Path, edit: str, replacement: str
) -> None:
    root = _contract_with(tmp_path, edit, replacement)
    with pytest.raises(generator.ExecutionGraphContractError, match="BAKE_V3_INVALID"):
        generator.load_contract(root)


@pytest.mark.parametrize(
    ("row", "code"),
    [
        ("{page: retail_health, variant: 7d}", "BAKE_ROSTER_IDENTIFIER_INVALID"),
        ("{page: campaigns, variant: Last-month}", "BAKE_ROSTER_IDENTIFIER_INVALID"),
        (f"{{page: campaigns, variant: {'v' * 81}}}", "BAKE_ROSTER_IDENTIFIER_INVALID"),
        (f"{{page: {'p' * 48}, variant: {'v' * 80}}}", "BAKE_INTENT_GROUP_ID_TOO_LONG"),
    ],
)
def test_o1_generation_refuses_the_split_grammar_and_an_over_long_group_id(
    tmp_path: pathlib.Path, row: str, code: str
) -> None:
    """A5.2: a contract typo fails at generation, not at Lambda init; paired accepted rows."""
    page, variant = (part.split(": ")[1] for part in row.strip("{}").split(", "))
    block = (
        f'  roster:\n    - {row}\n  budget_seconds:\n    "{page}#{variant}": 90\n'
        f"  write_seconds: {{{page}: 16.6}}\n"
        f'  source_horizon_days:\n    "{page}#{variant}": 125\n' + _certified(f"{page}#{variant}")
    )
    with pytest.raises(generator.ExecutionGraphContractError) as refused:
        generator.load_contract(_contract_with(tmp_path / "refused", _roster_block(), block))
    assert refused.value.code == code
    # Accepted pairs: an underscore calendar variant, and a group id of exactly 128 characters.
    for name, accepted_page, accepted_variant in (
        ("calendar", "new-customers", "last_complete_month"),
        ("ceiling", "p" * 47, "v" * 80),
    ):
        accepted = (
            f"  roster:\n    - {{page: {accepted_page}, variant: {accepted_variant}}}\n"
            f'  budget_seconds:\n    "{accepted_page}#{accepted_variant}": 90\n'
            f"  write_seconds: {{{accepted_page}: 16.6}}\n"
            f'  source_horizon_days:\n    "{accepted_page}#{accepted_variant}": 125\n'
            + _certified(f"{accepted_page}#{accepted_variant}")
        )
        root = _contract_with(tmp_path / name, _roster_block(), accepted)
        assert len(generator.load_contract(root)["bake_tasks"]["roster"]) == 1


def test_every_contract_bake_value_is_the_s0_certificate_value() -> None:
    """A5.3 enforcement: the contract copies the approved S0 values; any drift is refused.

    Control: a certificate row whose count differs is reported by the same comparison.
    """
    import json  # noqa: PLC0415

    from domain.operations.generated_execution_graphs import BAKE_TASKS  # noqa: PLC0415

    certificate = json.loads(
        (ROOT / "tests/fixtures/bake-certification/d2-certificate.json").read_text()
    )
    rows = {row["key"]: row for row in certificate["rows"]}

    def drift(table: dict[str, dict[str, object]]) -> list[str]:
        found: list[str] = []
        for row in BAKE_TASKS["roster"]:
            key = f"{row['page']}#{row['variant']}"
            expected = table[key]
            actual = (
                BAKE_TASKS["certified_plan_counts"][key],
                BAKE_TASKS["budget_seconds"][key],
                BAKE_TASKS["write_seconds"][row["page"]],
                BAKE_TASKS["source_horizon_days"][key],
            )
            certified = (
                expected["certified_count"],
                expected["budget_seconds"],
                cast("dict[str, object]", expected["write_seconds"])["value"],
                expected["source_horizon_days"],
            )
            if actual != certified:
                found.append(key)
        return found

    assert drift(rows) == []
    tampered = {key: dict(row) for key, row in rows.items()}
    tampered["campaigns#1d"]["certified_count"] = 27
    assert drift(tampered) == ["campaigns#1d"]
