# SPDX-License-Identifier: MIT-0
# pyright: reportPrivateUsage=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false
"""Who can start Athena, and how many at once (plan S3, 3.6, F3, F5, A1.3).

The static start budget assumes three things this module proves over source, each beside a
paired control that the same census reports:

- Start sites: `start_query_execution` is called only inside `execution.start` and
  `Stage1AthenaStartClient._start_once`.
- In flight: no query-execution FAN-OUT exists on a sequential batch path, every Baker module
  or a BAKE probe, outside a closed allowlist of three non-query threads. Synchronization
  primitives are not fan-out. A recording fake drives the two statement loops behaviourally.
- Who reaches the wrappers: `AthenaQueryAdapter` is constructed only in a closed set of
  compositions, the Baker's only inside `AthenaBatchAdmission`; no source factory builds one.
"""

from __future__ import annotations

import ast
import importlib
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from adapters import config
from adapters.config import settings_without_client_secret, tracing_settings
from adapters.inbound.ingestion_handlers import inline_run

pytestmark = [pytest.mark.unit]

_TABLE_WRITER = "adapters.inbound.ingestion_handlers.table_writer"


@pytest.fixture
def table_writer(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Import the TableWriter under the queued-ingestion settings it resolves at import."""
    from test_table_writer import _INGESTION_ENV, _SETTINGS_ENV  # noqa: PLC0415

    for name, value in {**_SETTINGS_ENV, **_INGESTION_ENV}.items():
        monkeypatch.setenv(name, value)
    caches = (
        settings_without_client_secret,
        tracing_settings,
        config.table_writer_settings,
        config._queued_ingestion_config,
        config.run_request_api_settings,
    )
    for accessor in caches:
        accessor.cache_clear()
    sys.modules.pop(_TABLE_WRITER, None)
    yield importlib.import_module(_TABLE_WRITER)
    sys.modules.pop(_TABLE_WRITER, None)
    for accessor in caches:
        accessor.cache_clear()


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

START_SITES = frozenset(
    {
        # `start`/`start_counted`'s one submission, which its throttled-start retry repeats.
        "adapters.outbound.athena.execution._submit",
        "adapters.outbound.athena.table_attempt_start.Stage1AthenaStartClient._start_once",
    }
)
ADAPTER_SITES = frozenset(
    {
        "adapters.jobs.preset_artifact_publisher.baker.application",
        "adapters.jobs.insight_precompute.handler.application",
        "adapters.inbound.presentation_handlers.handler.<module>",
        "adapters.inbound.tool_handlers.measurement_entrypoint.<module>",
    }
)
_HANDLERS = "src/adapters/inbound/ingestion_handlers"
CENSUS_PATHS = (
    f"{_HANDLERS}/table_writer.py",
    f"{_HANDLERS}/inline_run.py",
    f"{_HANDLERS}/load.py",
    f"{_HANDLERS}/table_attempt_protocol.py",
    "src/adapters/jobs/provider_attempt_reconciler.py",
    "src/adapters/outbound/athena/execution.py",
    "src/adapters/outbound/athena/table_attempt_start.py",
    "src/adapters/outbound/athena/semantic_storage.py",
    "src/adapters/jobs/ingest_delivery",
    "src/adapters/jobs/reprocess_semantic_v2",
    # Every module, including every present and future `*_source.py` factory (A1.3).
    "src/adapters/jobs/preset_artifact_publisher",
    # The three BAKE probes occupy the reprocess_operator row (F5).
    "validation/probe_amc_current_invariants.py",
    "validation/probe_ingestion_traceability_gate.py",
    "validation/probe_vendor_seed_invariants.py",
)


@dataclass(frozen=True, slots=True)
class FanOutSite:
    """One query-execution fan-out construct, by its enclosing qualified function."""

    function: str
    kind: str
    keywords: tuple[tuple[str, str], ...] = ()


# Each allowlisted entry is a non-query thread [verified, plan §0]; a Thread keeps its keywords.
_INGEST = "adapters.jobs.ingest_delivery.__main__._Heartbeat.start"
_REPROCESS = "adapters.jobs.reprocess_semantic_v2.__main__.ReprocessHeartbeat.start"
_GATE = "validation.probe_ingestion_traceability_gate._lifecycle_under_explanation_load"
_THREAD_KEYWORDS = {
    _INGEST: (("name", "'ingest-delivery-heartbeat'"), ("target", "self._run")),
    _REPROCESS: (("name", "'reprocess-semantic-v2-heartbeat'"), ("target", "self._run")),
}
ALLOWLIST = frozenset({_INGEST, _REPROCESS, _GATE})

_CONSTRUCTORS = {
    "concurrent.futures.ThreadPoolExecutor": "executor",
    "concurrent.futures.ProcessPoolExecutor": "executor",
    "multiprocessing.Process": "process",
    "multiprocessing.Pool": "process",
    "asyncio.create_task": "asyncio",
    "asyncio.gather": "asyncio",
    "asyncio.TaskGroup": "asyncio",
    "asyncio.to_thread": "asyncio",
    "threading.Thread": "thread",
}
_HANDLE_METHODS = {"executor": frozenset({"submit", "map"}), "thread": frozenset({"start"})}


def _module_name(path: Path) -> str:
    relative = path.relative_to(SRC if path.is_relative_to(SRC) else ROOT).with_suffix("")
    return ".".join(relative.parts)


def _modules(paths: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    for entry in paths:
        path = ROOT / entry
        assert path.exists(), entry
        found.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    return found


def _aliases(tree: ast.AST) -> dict[str, str]:
    """Map every imported name, at any depth, to its dotted origin."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def _dotted(node: ast.AST, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value, aliases)
        return None if base is None else f"{base}.{node.attr}"
    return None


class _Scoped(ast.NodeVisitor):
    """Visit every call with its enclosing qualified function (`<module>` at top level)."""

    def __init__(self, module: str) -> None:
        self.module = module
        self.scope: list[str] = []
        self.calls: list[tuple[str, ast.Call]] = []

    def _enter(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_ClassDef = _enter  # noqa: N815 -- ast.NodeVisitor dispatch names
    visit_FunctionDef = _enter  # noqa: N815
    visit_AsyncFunctionDef = _enter  # noqa: N815

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append((".".join([self.module, *(self.scope or ["<module>"])]), node))
        self.generic_visit(node)


def _calls(source: str, module: str) -> tuple[ast.Module, list[tuple[str, ast.Call]]]:
    tree = ast.parse(source)
    visitor = _Scoped(module)
    visitor.visit(tree)
    return tree, visitor.calls


def _handles(tree: ast.Module, aliases: dict[str, str]) -> dict[str, str]:
    """Return each target bound to an executor or thread (`pool`, `self._thread`) and its kind."""
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        pairs: list[tuple[ast.AST, ast.AST]] = []
        if isinstance(node, ast.Assign):
            pairs = [(target, node.value) for target in node.targets]
        elif isinstance(node, ast.With):
            pairs = [
                (item.optional_vars, item.context_expr) for item in node.items if item.optional_vars
            ]
        for target, value in pairs:
            if isinstance(value, ast.Call):
                kind = _CONSTRUCTORS.get(_dotted(value.func, aliases) or "")
                name = ast.unparse(target)
                if kind in _HANDLE_METHODS:
                    bound[name] = kind
    return bound


def fan_out_sites(source: str, module: str) -> tuple[FanOutSite, ...]:
    """Report every query-execution fan-out construct in one module (F3).

    Executor/process/asyncio-task/thread construction, `.submit`/`.map` on a bound executor,
    `.start()` on a bound thread, and any `.run_in_executor`. `threading.Event`, `Lock`,
    `RLock`, `Condition` and `Semaphore` are synchronization, not fan-out, and are not reported.
    """
    tree, calls = _calls(source, module)
    aliases = _aliases(tree)
    handles = _handles(tree, aliases)
    sites: list[FanOutSite] = []
    for function, call in calls:
        dotted = _dotted(call.func, aliases) or ""
        kind = _CONSTRUCTORS.get(dotted)
        if kind is not None:
            keywords = tuple(
                sorted(
                    (keyword.arg, ast.unparse(keyword.value))
                    for keyword in call.keywords
                    if keyword.arg in {"name", "target"}
                )
            )
            sites.append(FanOutSite(function, kind, keywords if kind == "thread" else ()))
            continue
        if isinstance(call.func, ast.Attribute):
            receiver = ast.unparse(call.func.value)
            bound = handles.get(receiver)
            if bound is not None and call.func.attr in _HANDLE_METHODS[bound]:
                sites.append(FanOutSite(function, f"{bound}.{call.func.attr}"))
            elif call.func.attr == "run_in_executor":
                sites.append(FanOutSite(function, "asyncio.run_in_executor"))
    return tuple(sites)


def start_sites(source: str, module: str) -> set[str]:
    """Return the enclosing qualified function of every `start_query_execution` call."""
    _, calls = _calls(source, module)
    return {
        function
        for function, call in calls
        if isinstance(call.func, ast.Attribute) and call.func.attr == "start_query_execution"
    }


def adapter_sites(source: str, module: str) -> list[tuple[str, bool]]:
    """Return each `AthenaQueryAdapter(` site and whether it is AthenaBatchAdmission's first arg."""
    _, calls = _calls(source, module)
    wrapped = {
        id(call.args[0])
        for _, call in calls
        if (_dotted(call.func, {}) or "").endswith("AthenaBatchAdmission") and call.args
    }
    return [
        (function, id(call) in wrapped)
        for function, call in calls
        if (_dotted(call.func, {}) or "").split(".")[-1] == "AthenaQueryAdapter"
    ]


def _census(paths: tuple[str, ...]) -> tuple[FanOutSite, ...]:
    return tuple(
        site
        for path in _modules(paths)
        for site in fan_out_sites(path.read_text(encoding="utf-8"), _module_name(path))
    )


def _allowlist_violations(sites: tuple[FanOutSite, ...]) -> list[FanOutSite]:
    return [
        site
        for site in sites
        if site.function not in ALLOWLIST
        or (site.kind == "thread" and site.keywords != _THREAD_KEYWORDS.get(site.function))
    ]


# --- start sites --------------------------------------------------------------------------------


def test_only_the_two_wrappers_call_start_query_execution_across_src_and_validation() -> None:
    found = {
        site
        for path in [*sorted(SRC.rglob("*.py")), *sorted((ROOT / "validation").rglob("*.py"))]
        for site in start_sites(path.read_text(encoding="utf-8"), _module_name(path))
    }
    assert found == START_SITES


def test_control_a_third_start_site_is_reported() -> None:
    source = "def rogue(client):\n    return client.start_query_execution(QueryString='x')\n"
    assert start_sites(source, "adapters.rogue") == {"adapters.rogue.rogue"}


# --- in-flight bound: the fan-out census --------------------------------------------------------


def test_every_fan_out_site_on_the_budgeted_paths_is_in_the_closed_allowlist() -> None:
    sites = _census(CENSUS_PATHS)
    assert _allowlist_violations(sites) == []
    assert {site.function for site in sites} == ALLOWLIST
    threads = {site.function: site.keywords for site in sites if site.kind == "thread"}
    assert threads == _THREAD_KEYWORDS
    # Every module of the Baker package was read, so a future source factory is in scope.
    baker_modules = {
        path.name for path in _modules(("src/adapters/jobs/preset_artifact_publisher",))
    }
    assert {
        "baker.py",
        "campaign_source.py",
        "retail_source.py",
        "scorecard_source.py",
    } <= baker_modules


def test_control_a_thread_pool_executor_is_reported() -> None:
    source = (
        "from concurrent.futures import ThreadPoolExecutor\n"
        "def render(requests, queries):\n"
        "    with ThreadPoolExecutor(max_workers=8) as pool:\n"
        "        return list(pool.map(queries.run, requests))\n"
    )
    sites = fan_out_sites(source, "adapters.jobs.preset_artifact_publisher.rogue")
    assert [site.kind for site in sites] == ["executor", "executor.map"]
    assert _allowlist_violations(sites) == list(sites)


def test_control_an_event_used_only_for_cancellation_is_accepted_with_zero_sites() -> None:
    source = (
        "import threading\n"
        "class Cancellation:\n"
        "    def __init__(self):\n"
        "        self._stop = threading.Event()\n"
        "        self._lock = threading.Lock()\n"
        "    def cancel(self):\n"
        "        with self._lock:\n"
        "            self._stop.set()\n"
        "    def wait(self, seconds):\n"
        "        return self._stop.wait(seconds) or self._stop.is_set()\n"
    )
    assert fan_out_sites(source, "adapters.jobs.ingest_delivery.cancellation") == ()


def test_control_a_source_factory_that_builds_its_own_executor_is_reported_by_name() -> None:
    source = (
        "import concurrent.futures as cf\n"
        "def retained_reach_preset_artifact_source(settings, pointers, queries):\n"
        "    pool = cf.ThreadPoolExecutor(4)\n"
        "    return pool.submit(queries.run, settings)\n"
    )
    module = "adapters.jobs.preset_artifact_publisher.reach_source"
    sites = fan_out_sites(source, module)
    assert {site.function for site in sites} == {f"{module}.retained_reach_preset_artifact_source"}
    assert [site.kind for site in sites] == ["executor", "executor.submit"]
    assert _allowlist_violations(sites) == list(sites)


def test_control_an_allowlisted_heartbeat_retargeted_at_query_work_is_reported() -> None:
    real = (ROOT / "src/adapters/jobs/ingest_delivery/__main__.py").read_text(encoding="utf-8")
    retargeted = real.replace("target=self._run,", "target=self._run_queries,", 1)
    assert retargeted != real
    sites = fan_out_sites(retargeted, "adapters.jobs.ingest_delivery.__main__")
    violations = _allowlist_violations(sites)
    assert [(site.function, site.kind) for site in violations] == [(_INGEST, "thread")]


# --- in-flight bound: behaviour over a three-statement composition ------------------------------


class _InFlight:
    """Record starts and terminals; `maximum` is the most executions ever in flight at once."""

    def __init__(self) -> None:
        self.starts = 0
        self.active = 0
        self.maximum = 0

    def start(self) -> None:
        self.starts += 1
        self.active += 1
        self.maximum = max(self.maximum, self.active)

    def terminal(self) -> None:
        self.active -= 1


def _recording_run_statement(recorder: _InFlight, state: object) -> Any:
    def run(_run: object, _statement: object) -> object:
        recorder.start()
        recorder.terminal()
        return SimpleNamespace(state=state, query_execution_id="fictional", cancel_requested=False)

    return run


def test_table_writer_and_inline_run_keep_at_most_one_execution_in_flight(
    monkeypatch: pytest.MonkeyPatch, table_writer: Any
) -> None:
    statements = (object(), object(), object())
    composition = SimpleNamespace(statements=statements, verification=object())
    writer, inline = _InFlight(), _InFlight()
    succeeded = table_writer.ProviderTerminalState.SUCCEEDED
    for module, recorder in ((table_writer, writer), (inline_run, inline)):
        monkeypatch.setattr(module, "_statement_run", lambda _ctx: object())
        monkeypatch.setattr(module, "run_statement", _recording_run_statement(recorder, succeeded))
    assert table_writer._execute_statements(object(), composition) is None
    completed: list[int] = []
    ctx = SimpleNamespace(request=SimpleNamespace(composition=composition))
    inline_module = cast("Any", inline_run)
    assert inline_module._execute_statements(ctx, completed.append) is None
    assert (writer.starts, writer.maximum) == (3, 1)
    assert (inline.starts, inline.maximum, completed) == (3, 1, [1, 2, 3])


def test_control_the_recorder_reports_two_when_two_starts_overlap() -> None:
    recorder = _InFlight()
    recorder.start()
    recorder.start()
    recorder.terminal()
    recorder.terminal()
    assert recorder.maximum == 2


# --- who reaches the wrappers: the AthenaQueryAdapter construction census (F3a, A1.3) -----------


def test_athena_query_adapter_is_constructed_only_in_the_closed_compositions() -> None:
    found = [
        (site, wrapped)
        for path in sorted(SRC.rglob("*.py"))
        for site, wrapped in adapter_sites(path.read_text(encoding="utf-8"), _module_name(path))
    ]
    assert {site for site, _ in found} == ADAPTER_SITES
    assert len(found) == len(ADAPTER_SITES)
    baker = "adapters.jobs.preset_artifact_publisher.baker.application"
    # The Baker's one adapter is the first argument of the invocation-shared admission.
    assert [wrapped for site, wrapped in found if site == baker] == [True]
    factories = [site for site, _ in found if site.endswith("_preset_artifact_source")]
    assert factories == []


def test_control_a_source_factory_with_an_adapter_fallback_is_reported() -> None:
    source = (
        "from adapters.outbound.athena.query import AthenaQueryAdapter, AthenaQueryTarget\n"
        "def retained_reach_preset_artifact_source(settings, pointers, queries=None):\n"
        "    return queries or AthenaQueryAdapter(AthenaQueryTarget(workgroup='w'))\n"
    )
    module = "adapters.jobs.preset_artifact_publisher.reach_source"
    sites = adapter_sites(source, module)
    assert sites == [(f"{module}.retained_reach_preset_artifact_source", False)]
    assert {site for site, _ in sites} - ADAPTER_SITES == {sites[0][0]}


def test_control_a_second_unwrapped_adapter_in_the_baker_composition_is_reported() -> None:
    source = (
        "def application():\n"
        "    admission = AthenaBatchAdmission(AthenaQueryAdapter(target), 10)\n"
        "    direct = AthenaQueryAdapter(target)\n"
        "    return admission, direct\n"
    )
    module = "adapters.jobs.preset_artifact_publisher.baker"
    sites = adapter_sites(source, module)
    assert sites == [(f"{module}.application", True), (f"{module}.application", False)]
    assert [wrapped for _, wrapped in sites] != [True]


# --- D2 source reconciliation (plan A5.4) and the pinned clock (A5.1 Tests 3) ----------------

_BAKER_PACKAGE = SRC / "adapters/jobs/preset_artifact_publisher"
CLOSED_FACTORY_PARAMETERS = ("settings", "pointers", "queries", "current")
"""The one closed source-factory signature (prep §2.1): the reader carries the bake clock."""


def factory_signatures(source: str) -> dict[str, tuple[str, ...]]:
    """Return each `retained_*_preset_artifact_source` factory's parameter names."""
    return {
        node.name: tuple(arg.arg for arg in (*node.args.posonlyargs, *node.args.args))
        + tuple(arg.arg for arg in node.args.kwonlyargs)
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith("retained_")
        and node.name.endswith("_preset_artifact_source")
    }


def constructions(source: str, module: str, name: str) -> set[str]:
    """Return every enclosing function that calls `name(...)`."""
    found: set[str] = set()

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                inner = f"{scope}.{child.name}" if scope != module else f"{module}.{child.name}"
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == name
            ):
                found.add(scope)
            visit(child, inner)

    visit(ast.parse(source), module)
    return found


def _package_sources() -> dict[str, str]:
    return {
        _module_name(path): path.read_text(encoding="utf-8")
        for path in sorted(_BAKER_PACKAGE.glob("*.py"))
    }


def test_every_source_factory_has_the_one_closed_signature() -> None:
    signatures = {
        name: parameters
        for source in _package_sources().values()
        for name, parameters in factory_signatures(source).items()
    }
    assert len(signatures) == 7
    assert set(signatures.values()) == {CLOSED_FACTORY_PARAMETERS}


def test_control_a_factory_with_an_extra_parameter_is_reported() -> None:
    source = "def retained_reach_preset_artifact_source(settings, pointers, queries, current, x):\n"
    source += "    return None\n"
    assert factory_signatures(source)["retained_reach_preset_artifact_source"] != (
        CLOSED_FACTORY_PARAMETERS
    )


def test_current_publications_are_constructed_only_at_the_two_compositions() -> None:
    found = {
        site
        for module, source in _package_sources().items()
        for site in constructions(source, module, "DynamoCurrentPublications")
    }
    assert found == {
        "adapters.jobs.preset_artifact_publisher.baker.application",
        "adapters.jobs.preset_artifact_publisher.enqueue.application",
    }
    # Control: a source that builds its own reader is reported.
    rogue = "def retained_x_preset_artifact_source(s, p, q, c):\n"
    rogue += "    return DynamoCurrentPublications('t')\n"
    assert constructions(rogue, "adapters.x", "DynamoCurrentPublications") == {
        "adapters.x.retained_x_preset_artifact_source"
    }


def _ambient_clock(source: str) -> bool:
    tree = ast.parse(source)
    defines = any(
        isinstance(node, ast.FunctionDef) and node.name == "_clock" for node in ast.walk(tree)
    )
    calls_now = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "now"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "datetime"
        for node in ast.walk(tree)
    )
    return defines or calls_now


def test_no_bake_source_or_counter_reads_a_wall_clock_and_one_legacy_wall_caller() -> None:
    sources = _package_sources()
    ambient = sorted(
        module
        for module, source in sources.items()
        if (module.endswith("_source") or module.endswith(".bakeability"))
        and _ambient_clock(source)
    )
    assert ambient == []
    legacy = {
        site
        for module, source in sources.items()
        for site in _attribute_call_sites(source, module, "legacy_wall")
    }
    assert legacy == {
        "adapters.jobs.preset_artifact_publisher.baker.PresetArtifactBakeApplication.apply"
    }
    pinned = _attribute_call_sites(
        sources["adapters.jobs.preset_artifact_publisher.graph_bake"],
        "adapters.jobs.preset_artifact_publisher.graph_bake",
        "pinned",
    )
    assert pinned == {
        "adapters.jobs.preset_artifact_publisher.graph_bake.GraphBakeCoordinator._selected_and_pinned",
        "adapters.jobs.preset_artifact_publisher.graph_bake.GraphBakeTaskWork.effect_for",
    }
    # Control: a synthetic source with its own `_clock` is reported.
    assert _ambient_clock("from datetime import datetime\ndef _clock():\n    return 1\n")


def _attribute_call_sites(source: str, module: str, attribute: str) -> set[str]:
    found: set[str] = set()

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                inner = f"{scope}.{child.name}"
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == attribute
            ):
                found.add(scope)
            visit(child, inner)

    visit(ast.parse(source), module)
    return found


def _premise_violations(columns: tuple[str, ...]) -> list[str]:
    return [
        column for column in columns if column == "marketplace_key" or ".reportOptions." in column
    ]


def test_the_prefetch_premise_no_roster_derived_filter_selects_a_partition_or_scope() -> None:
    """A5.1 Tests 4: a roster-derived filter key is never `marketplace_key` or a report option.

    Those are the only filter-sensitive inputs of `publication_scopes_for` and `_selected_side`,
    so a result-dependent key never changes which partition or scope a plan selects.
    """
    from domain.measurement.rosters import (  # noqa: PLC0415
        PROFILE_ROSTER_SCOPE,
        ROSTER_MEDIA_COLUMNS,
        SEGMENT_ROSTER_SCOPE,
        RosterKind,
        roster_column,
    )

    derived = (
        *(roster_column(kind) for kind in RosterKind),
        *ROSTER_MEDIA_COLUMNS,
        *SEGMENT_ROSTER_SCOPE,
        *PROFILE_ROSTER_SCOPE,
    )
    assert _premise_violations(derived) == []
    # Control: a synthetic contract that binds a roster key as a report option is reported.
    assert _premise_violations(("amc.reportOptions.campaign_key",)) == [
        "amc.reportOptions.campaign_key"
    ]
