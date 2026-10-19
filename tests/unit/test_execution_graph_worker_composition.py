# SPDX-License-Identifier: MIT-0
"""Each execution-graph worker's production store has every method its worker module calls.

Unit tests compose workers with stores they build themselves, so a production composition that
hands a worker the wrong store passes every one of them: on 2026-10-14 the promoter was composed
with `DynamoGraphDispatchStore` while its serving election called `DynamoGraphReleaseStore`-only
methods, and it failed live with `AttributeError` at the first eligible candidate. This reads the
real composition in `workers.py` (which module-level store each handler passes, and that store's
real class) and every `store.<method>(` call in the worker module, so a mismatch fails here.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
from pathlib import Path
from typing import Final

import pytest

from adapters.outbound.dynamodb.execution_graph_dispatch import DynamoGraphDispatchStore
from adapters.outbound.dynamodb.execution_graph_release import DynamoGraphReleaseStore
from domain.operations.stuck_flow import FLOW_OBLIGATION_KINDS

pytestmark = [pytest.mark.unit]

WORKERS = "adapters.jobs.execution_graph.workers"
COMPOSERS = frozenset(
    {"DispatchContext", "PromoterContext", "ReleaseContext", "VerifierContext", "settle_shard"}
)
HANDLERS = frozenset(
    {
        "dispatcher_handler",
        "fence_settler_handler",
        "node_verifier_handler",
        "promoter_handler",
        "release_handler",
    }
)


def _tree(module: str) -> ast.Module:
    spec = importlib.util.find_spec(module)
    assert spec is not None and spec.origin is not None
    return ast.parse(Path(spec.origin).read_text(encoding="utf-8"))


def _imports(tree: ast.Module) -> dict[str, str]:
    return {
        alias.asname or alias.name: node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
        for alias in node.names
    }


def _stores(tree: ast.Module, imports: dict[str, str]) -> dict[str, type]:
    """Module-level `NAME: Final = StoreClass(...)` bindings, resolved to the real classes."""
    found: dict[str, type] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id.endswith("Store")
        ):
            name = node.value.func.id
            found[node.target.id] = getattr(importlib.import_module(imports[name]), name)
    return found


def composition() -> dict[str, tuple[str, type]]:
    """Each handler's worker module and the class of the store it is composed with."""
    tree = _tree(WORKERS)
    imports = _imports(tree)
    stores = _stores(tree, imports)
    composed: dict[str, tuple[str, type]] = {}
    for handler in tree.body:
        if not isinstance(handler, ast.FunctionDef) or handler.name not in HANDLERS:
            continue
        for call in ast.walk(handler):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id in COMPOSERS
                and call.args
                and isinstance(call.args[0], ast.Name)
                and call.args[0].id in stores
            ):
                composed[handler.name] = (imports[call.func.id], stores[call.args[0].id])
    return composed


def store_calls(module: str) -> frozenset[str]:
    """Every `store.<m>(`, `context.store.<m>(` or `ctx.store.<m>(` call in the module."""
    called: set[str] = set()
    for call in ast.walk(_tree(module)):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
            continue
        target = call.func.value
        if (isinstance(target, ast.Name) and target.id == "store") or (
            isinstance(target, ast.Attribute) and target.attr == "store"
        ):
            called.add(call.func.attr)
    return frozenset(called)


def missing(module: str, store: type) -> frozenset[str]:
    return frozenset(name for name in store_calls(module) if not hasattr(store, name))


def test_every_worker_is_composed_with_a_store_that_has_every_method_it_calls() -> None:
    composed = composition()
    assert set(composed) == HANDLERS
    for handler, (module, store) in sorted(composed.items()):
        assert store_calls(module), handler
        assert (handler, missing(module, store)) == (handler, frozenset())


RETIRED_METHODS: Final = frozenset(
    {
        # The nine stage-1 fenced day/wait writers and the publication park/wake pair.
        "bootstrap_serving_authority_and_apply_grant",
        "begin_close_serving_day",
        "close_serving_day",
        "reserve_serving_winner",
        "park_cohort_for_capacity",
        "merge_capacity_wait",
        "roll_capacity_wait",
        "settle_capacity_unavailable",
        "park_publish_for_serving_barrier",
        "wake_parked_publish",
        "read_serving_authority",
        # The retained fence is historical data; no runtime path reads it for admission.
        "read_rolling_freshness_fence",
    }
)
RUNTIME_MODULES: Final = (
    "adapters.jobs.execution_graph.dispatcher",
    "adapters.jobs.execution_graph.fence_settler",
    "adapters.jobs.execution_graph.node_verifier",
    "adapters.jobs.execution_graph.promoter",
    "adapters.jobs.execution_graph.release",
    "adapters.inbound.ingestion_handlers.graph_publish",
    "adapters.jobs.preset_artifact_publisher.graph_bake",
)


def test_no_runtime_store_or_worker_keeps_a_park_wake_day_wait_or_fence_path() -> None:
    """Plan item 4: the final runtime has no executable day/wait/park/wake/fence method."""
    for store in (DynamoGraphDispatchStore, DynamoGraphReleaseStore):
        assert (store.__name__, RETIRED_METHODS & set(dir(store))) == (store.__name__, set())
    for module in RUNTIME_MODULES:
        assert (module, store_calls(module) & RETIRED_METHODS) == (module, frozenset())
    assert {"PUBLISH_WAIT", "CAPACITY_WAIT"}.isdisjoint(FLOW_OBLIGATION_KINDS)
    # Control: the scan sees the rolling calls these modules do make.
    assert "drain_final_alias_and_activate_next" in store_calls(RUNTIME_MODULES[3])
    assert "settle_publish" in store_calls(RUNTIME_MODULES[5])


def test_control_the_promoter_on_the_dispatch_store_is_refused() -> None:
    module, store = composition()["promoter_handler"]
    assert store is not DynamoGraphDispatchStore
    refused = missing(module, DynamoGraphDispatchStore)
    assert {
        "read_serving_candidate",
        "read_serving_domain",
        "activate_serving_winner",
        "drain_final_alias_and_activate_next",
    } <= refused
