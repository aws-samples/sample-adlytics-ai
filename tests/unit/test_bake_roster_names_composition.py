# SPDX-License-Identifier: MIT-0
"""Every Baker spend roster asked for campaigns can name them, as chat's does (recover 87067199).

The Reach frequency read once asked a CAMPAIGN roster; a `SpendRosterReader` composed without
`names=` refuses it (PLAN-0008), which withheld every Reach row live. The source tests stub the
roster port, so only a composition-level check sees the wiring: each retained page source is
composed for real here, with `SpendRosterReader` recorded, and each that names campaigns must
pass a names port. Reach now counts campaigns with one ranked read (ADR-152) and asks only the
ad-product roster, so its composition carries none, and its source test refuses a campaign ask.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import date
from typing import Any, cast

import pytest

from adapters.config import PresetArtifactBakeSettings
from adapters.inbound.tool_handlers.dimension_labels import DimensionLabelResolver
from adapters.inbound.tool_handlers.gateway import contracts_root
from adapters.jobs.preset_artifact_publisher import (
    audiences_source,
    campaign_source,
    new_customers_source,
    reach_source,
)
from adapters.outbound.bake_sources import (
    BakeRowHorizon,
    BakeSourceReads,
    DurableBakeSourceManifestInvalidError,
)
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.entity_catalog import CanonicalEntityCatalog
from adapters.outbound.segment_labels import NAMESPACE_WRITERS
from adapters.outbound.spend_roster import BakePinnedNames
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.shared_kernel.errors import (
    AdlyticsError,
    DependencyError,
    DomainError,
    ErrorCode,
    ValidationError,
)
from domain.shared_kernel.values import DateRange
from ports.outbound.segment_labels import SegmentLabelPort, SegmentLabelRequest

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src/adapters"
COMPOSITION_ROOTS = (
    _SRC / "jobs/preset_artifact_publisher",
    _SRC / "jobs/insight_precompute",
    _SRC / "inbound/presentation_handlers",
)


pytestmark = pytest.mark.unit


def _settings() -> PresetArtifactBakeSettings:
    return PresetArtifactBakeSettings(
        artifact_bucket="fictional-artifacts",
        pointer_table="fictional-pointers",
        semantic_publications_table="fictional-table",
        semantic_v2_bucket="fictional-bucket",
        athena_workgroup="fictional-workgroup",
        semantic_v2_current_database="fictional-database",
        athena_result_reuse_max_age_minutes=1,
        athena_query_batch_max_concurrency=4,
    )


@pytest.mark.parametrize(
    ("module", "factory"),
    [
        (reach_source, "retained_reach_preset_artifact_source"),
        (new_customers_source, "retained_new_customers_preset_artifact_source"),
        (campaign_source, "retained_campaign_preset_artifact_source"),
    ],
)
def test_each_composed_baker_spend_roster_carries_a_names_port(
    monkeypatch: pytest.MonkeyPatch, module: Any, factory: str
) -> None:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    composed: list[dict[str, object]] = []
    real = module.SpendRosterReader

    def recording(**kwargs: object) -> object:
        composed.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(module, "SpendRosterReader", recording)
    source = getattr(module, factory)(
        _settings(),
        cast("Any", None),
        cast("Any", None),
        BakeSourceReads(cast("Any", None), cast("Any", None)),
    )
    assert source is not None
    assert composed, "the source composes a spend roster"
    for kwargs in composed:
        names = kwargs.get("names")
        if module is reach_source:
            # ADR-152: Reach asks only the ad-product roster, named from the taxonomy.
            assert names is None
            continue
        assert isinstance(names, BakePinnedNames)
        resolver = names.names
        assert isinstance(resolver, DimensionLabelResolver)
        governed = AuthorizedAnalyticalScope(
            frozenset({load_commerce_identity_bridge(contracts_root()).commerce_scope})
        )
        assert resolver._scope == governed  # pyright: ignore[reportPrivateUsage]
        assert isinstance(resolver._catalog, CanonicalEntityCatalog)  # pyright: ignore[reportPrivateUsage]


def _supplies_names(call: ast.Call) -> bool:
    return any(
        keyword.arg == "names"
        and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is None)
        for keyword in call.keywords
    )


def test_no_runtime_composes_a_spend_roster_without_names() -> None:
    """Every Baker, precompute and serving composition of `SpendRosterReader` supplies names."""
    paths = sorted(path for root in COMPOSITION_ROOTS for path in root.rglob("*.py"))
    calls = [
        (path, node)
        for path in paths
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", None)) == "SpendRosterReader"
    ]
    assert len(calls) >= 5, "the scan reached the known compositions"
    unnamed = [path.name for path, node in calls if not _supplies_names(node)]
    # ADR-152: Reach's is the one roster never asked for campaigns (see the module docstring).
    assert unnamed == ["reach_source.py"]


_ROW = BakeRowHorizon(date(2026, 10, 14), 400)
_SEPTEMBER = DateRange(date(2026, 9, 1), date(2026, 9, 30))
_SOURCES = [
    (reach_source, "retained_reach_preset_artifact_source"),
    (new_customers_source, "retained_new_customers_preset_artifact_source"),
    (campaign_source, "retained_campaign_preset_artifact_source"),
]
_NAMING_SOURCES = [item for item in _SOURCES if item[0] is not reach_source]
"""The sources whose spend roster names campaigns: Reach only counts them (ADR-152)."""


class _Current:
    """A fictional CURRENT: every scope has no publication; counts the reads that reach it."""

    def __init__(self) -> None:
        self.scope_reads = 0

    def current_for_scope(self, _scope: object, _horizon: object = None) -> tuple[()]:
        self.scope_reads += 1
        return ()


class _Exact:
    def select_exact(self, _request: object) -> tuple[()]:
        return ()


@pytest.mark.parametrize(("module", "factory"), _NAMING_SOURCES)
def test_a_campaign_roster_named_at_task_render_reads_only_pinned_scopes(
    monkeypatch: pytest.MonkeyPatch, module: Any, factory: str
) -> None:
    """GPT review 1-2: the pin pass sees no campaign spend; the task's render names real keys.

    The composed names port runs as GRAPH runs it: RECORD with the empty-answer pass's empty
    keys, then REPLAY with real campaign keys. Every CURRENT label read the render makes must
    have been pinned; an unpinned one is `DurableBakeSourceManifestInvalidError`.
    """
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    composed: list[dict[str, Any]] = []
    real = module.SpendRosterReader

    def recording(**kwargs: Any) -> object:
        composed.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(module, "SpendRosterReader", recording)
    current = _Current()
    reads = BakeSourceReads(current, cast("Any", _Exact()))
    getattr(module, factory)(_settings(), cast("Any", None), cast("Any", None), reads)
    names = composed[0]["names"]
    with reads.snapshot(), reads.recording(_ROW) as recorded:
        assert names.public_names("campaign_key", (), _SEPTEMBER) == {}
    assert current.scope_reads > 0, "the empty pass reads the campaign label scopes"
    pinned = current.scope_reads
    try:
        with reads.replaying(recorded.reads, _ROW):
            names.public_names("campaign_key", ("campaign-7", "campaign-9"), _SEPTEMBER)
    except DurableBakeSourceManifestInvalidError as error:  # pragma: no cover - the defect
        pytest.fail(f"the task render made an unpinned CURRENT read: {error}")
    assert current.scope_reads == pinned  # zero CURRENT reads past the pin


def test_audiences_segment_labels_read_at_task_render_only_pinned_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live cohort 50206235: the audiences task labelled real segments with an unpinned read.

    The empty-answer prefetch requests no segment, so the composed label port must still read
    every label scope then; the task's render with real segment IDs must read only pinned ones.
    """
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    def one_marketplace(_root: pathlib.Path) -> int:
        return 1

    monkeypatch.setattr(audiences_source, "audiences_marketplace_count", one_marketplace)
    current = _Current()
    reads = BakeSourceReads(current, cast("Any", _Exact()))
    source = audiences_source.retained_audiences_preset_artifact_source(
        _settings(), cast("Any", None), cast("Any", None), reads
    )
    assert source is not None
    renderer = cast("audiences_source.AudiencesArtifactRenderer", source.compiler)
    port: SegmentLabelPort = renderer.segment_labels
    market: str = renderer.marketplaces[0]
    for namespace in NAMESPACE_WRITERS:
        with reads.snapshot(), reads.recording(_ROW) as recorded:
            assert port.labels(SegmentLabelRequest(namespace, market, (), _SEPTEMBER.end)) == {}
        assert current.scope_reads > 0, "the empty pass reads the segment label scopes"
        pinned = current.scope_reads
        try:
            with reads.replaying(recorded.reads, _ROW):
                port.labels(SegmentLabelRequest(namespace, market, ("41001",), _SEPTEMBER.end))
        except DurableBakeSourceManifestInvalidError as error:  # pragma: no cover - the defect
            pytest.fail(f"the task render made an unpinned CURRENT read: {error}")
        assert current.scope_reads == pinned


def test_the_audiences_compiler_asks_its_label_port_even_with_no_segments() -> None:
    """The pinning port only reads on the empty pass if the compiler still calls it."""
    from types import SimpleNamespace  # noqa: PLC0415

    from domain.semantics.calendar_periods import CalendarPeriod  # noqa: PLC0415
    from domain.semantics.time import TimeGrain  # noqa: PLC0415

    asked: list[SegmentLabelRequest] = []

    class _Port:
        def labels(self, request: SegmentLabelRequest) -> dict[str, object]:
            asked.append(request)
            return {}

    namespace = next(iter(NAMESPACE_WRITERS))
    period = CalendarPeriod(_SEPTEMBER.start, _SEPTEMBER.end, TimeGrain.MONTH)
    compiler = SimpleNamespace(segment_labels=_Port())
    labels = audiences_source.AudiencesArtifactRenderer._labels  # pyright: ignore[reportPrivateUsage]
    assert labels(cast("Any", compiler), "amazon_us", namespace, set(), period) == {}
    assert [request.segment_ids for request in asked] == [()]


def _unprotected_label_reads(root: object) -> list[str]:
    """Walk a composed source; name every result-dependent label port no bake wrapper guards.

    Those ports (`DimensionLabelResolver`, `CanonicalSegmentLabels`) read CURRENT only for the
    keys a query returned, so a GRAPH pin pass, which answers queries empty, never pins them.
    """
    from adapters.outbound.segment_labels import (  # noqa: PLC0415
        BakePinnedSegmentLabels,
        CanonicalSegmentLabels,
    )

    found: list[str] = []
    seen: set[int] = set()

    def visit(item: object, path: str) -> None:
        if id(item) in seen:
            return
        seen.add(id(item))
        if isinstance(item, (BakePinnedNames, BakePinnedSegmentLabels)):
            return  # guarded: its empty-call probe pins the scopes
        if isinstance(item, (DimensionLabelResolver, CanonicalSegmentLabels)):
            found.append(f"{path}: {type(item).__name__}")
            return
        if isinstance(item, (tuple, list, frozenset, set)):
            for index, child in enumerate(cast("Any", item)):
                visit(child, f"{path}[{index}]")
            return
        if isinstance(item, dict):
            for key, child in cast("dict[object, object]", item).items():
                visit(child, f"{path}[{key!r}]")
            return
        if not type(item).__module__.startswith(("adapters.", "domain.")):
            return
        slots = [name for klass in type(item).__mro__ for name in getattr(klass, "__slots__", ())]
        names = [*getattr(item, "__dict__", {}), *slots]
        for name in names:
            if hasattr(item, name):
                visit(getattr(item, name), f"{path}.{name}")

    visit(root, "source")
    return found


def test_roster_page_compositions_wrap_the_known_label_reader_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Composition-only check of the known reader families, for every roster page.

    Every `BakeRoster.from_contract()` page is composed through `PAGE_BAKERS`, and its object
    graph must hold no `DimensionLabelResolver` or `CanonicalSegmentLabels` outside a bake pin
    wrapper. It proves wiring of those two families only. It runs no pin pass or render, and it
    can't see readers hidden in callables or new reader types. The live GRAPH cohort is the
    behavioural proof.
    """
    from adapters.jobs.preset_artifact_publisher.baker import PAGE_BAKERS  # noqa: PLC0415
    from domain.operations.execution_graph_release import BakeRoster  # noqa: PLC0415

    def one_marketplace(_root: pathlib.Path) -> int:
        return 1

    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setattr(audiences_source, "audiences_marketplace_count", one_marketplace)
    reads = BakeSourceReads(cast("Any", _Current()), cast("Any", _Exact()))
    pages = BakeRoster.from_contract().pages
    assert len(pages) >= 7
    queries = cast("Any", object())  # never run: composition only
    bakers = {
        page: PAGE_BAKERS[page](_settings(), cast("Any", None), queries, reads) for page in pages
    }
    closed = [
        page
        for page, baker in bakers.items()
        if "Unavailable" in type(getattr(baker, "source", None)).__name__
    ]
    assert closed == [], "every roster page composes its retained source"
    found = {page: _unprotected_label_reads(baker) for page, baker in bakers.items()}
    assert {page: hits for page, hits in found.items() if hits} == {}


def test_audiences_campaign_names_pinned_by_the_synthetic_prefetch_cover_real_campaigns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prefetch walker names one synthetic campaign; a real render's names read no more."""

    def one_marketplace(_root: pathlib.Path) -> int:
        return 1

    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setattr(audiences_source, "audiences_marketplace_count", one_marketplace)
    current = _Current()
    reads = BakeSourceReads(current, cast("Any", _Exact()))
    source = audiences_source.retained_audiences_preset_artifact_source(
        _settings(), cast("Any", None), cast("Any", None), reads
    )
    assert source is not None
    renderer = cast("audiences_source.AudiencesArtifactRenderer", source.compiler)
    names = renderer.campaign_labels
    synthetic = audiences_source._PREFETCH_CAMPAIGN  # pyright: ignore[reportPrivateUsage]
    with reads.snapshot(), reads.recording(_ROW) as recorded:
        names((synthetic,), _SEPTEMBER)
    assert current.scope_reads > 0, "the synthetic prefetch reads the campaign label scopes"
    pinned = current.scope_reads
    try:
        with reads.replaying(recorded.reads, _ROW):
            names(("campaign-7", "campaign-9"), _SEPTEMBER)
    except DurableBakeSourceManifestInvalidError as error:  # pragma: no cover - the defect
        pytest.fail(f"the task render made an unpinned CURRENT read: {error}")
    assert current.scope_reads == pinned


class _Refusing:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def labels(self, _request: SegmentLabelRequest) -> dict[str, object]:
        raise self.error

    def public_names(self, *_args: object) -> dict[str, str]:
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        DomainError(ErrorCode("FICTIONAL", 41), "refused"),
        ValidationError(ErrorCode("FICTIONAL", 43), "invalid"),
    ],
)
def test_a_refused_pin_probe_never_fails_the_pin_pass_and_keeps_its_code(
    error: AdlyticsError, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review #4/#6/#11: a designed probe refusal names nothing and keeps its code.

    The pin pass carries on, and the code is logged at both probe sites, so a later
    manifest failure keeps its cause.
    """
    from adapters.outbound import segment_labels, spend_roster  # noqa: PLC0415
    from adapters.outbound.segment_labels import BakePinnedSegmentLabels  # noqa: PLC0415

    emitted: list[dict[str, Any]] = []

    class _Logger:
        def warning(self, message: str, extra: dict[str, Any]) -> None:
            emitted.append({"message": message, **extra})

    for module in (segment_labels, spend_roster):
        monkeypatch.setattr(module, "_PROBE_LOG", _Logger())
    namespace = next(iter(NAMESPACE_WRITERS))
    port = BakePinnedSegmentLabels(cast("Any", _Refusing(error)))
    assert port.labels(SegmentLabelRequest(namespace, "amazon_us", (), _SEPTEMBER.end)) == {}
    names = BakePinnedNames(cast("Any", _Refusing(error)))
    assert names.public_names("campaign_key", (), _SEPTEMBER) == {}
    fields = [
        {key: item.get(key) for key in ("operation", "phase", "outcome", "probe", "error_code")}
        for item in emitted
    ]
    assert fields == [
        {
            "operation": "graph_bake",
            "phase": "source_pin",
            "outcome": "probe_refused",
            "probe": probe,
            "error_code": str(error.code),
        }
        for probe in ("segment_labels", "campaign_names")
    ]


def test_a_dependency_failure_on_the_pin_probe_still_re_raises() -> None:
    """DB21 control: a throttled read is never swallowed."""
    from adapters.outbound.segment_labels import BakePinnedSegmentLabels  # noqa: PLC0415

    namespace = next(iter(NAMESPACE_WRITERS))
    error = DependencyError(ErrorCode("FICTIONAL", 42), "throttled")
    with pytest.raises(DependencyError):
        BakePinnedSegmentLabels(cast("Any", _Refusing(error))).labels(
            SegmentLabelRequest(namespace, "amazon_us", (), _SEPTEMBER.end)
        )
    with pytest.raises(DependencyError):
        BakePinnedNames(cast("Any", _Refusing(error))).public_names("campaign_key", (), _SEPTEMBER)
