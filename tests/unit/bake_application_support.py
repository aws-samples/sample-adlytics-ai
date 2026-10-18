# SPDX-License-Identifier: MIT-0
"""Composing the legacy bake application in unit tests (ADR-142, ADR-144).

The application renders every roster intent through the one page execution, so a test gives it
page parts over a query port no fake baker reaches, a roster with a row for each declared intent
it publishes, and the invocation's remaining-time callable a deployed Lambda always has. Every
identity is fictional.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest

from adapters.config import PresetArtifactBakeSettings
from adapters.jobs.preset_artifact_publisher import baker as baker_module
from adapters.jobs.preset_artifact_publisher.baker import PresetArtifactBakeApplication
from adapters.jobs.preset_artifact_publisher.page_bake import PageParts, ProcessBakeLedger
from adapters.jobs.preset_artifact_publisher.transport import BakeIntent, DeclaredBakeIntents
from adapters.outbound.athena.query import AthenaQueryTarget
from adapters.outbound.bake_sources import BakeQueryCapture
from domain.operations.execution_graph_release import BakeRoster, BakeRosterRow
from ports.outbound.preset_artifacts import PresetArtifactPersistencePort
from ports.outbound.query import PeriodSeriesRequest, QueryRequest, QueryResult

REMAINING_MS = 600_000
"""The fictional invocation's remaining time: well inside the 900 s Lambda timeout."""


def remaining_ms() -> int:
    """Return the fictional invocation's remaining time."""
    return REMAINING_MS


class RefusingQueries:
    """A query port no fake baker reaches: a real query here is a test defect."""

    def run(self, request: QueryRequest) -> QueryResult:
        """Refuse: the fake baker runs no statement."""
        raise AssertionError(f"a fake bake reached a query: {type(request).__name__}")

    def run_series(self, request: PeriodSeriesRequest) -> QueryResult:
        """Refuse: the fake baker runs no statement."""
        raise AssertionError(f"a fake bake reached a series: {type(request).__name__}")


def roster_for(intents: Iterable[BakeIntent], *, certified: int = 64) -> BakeRoster:
    """Return a roster with one row per intent (budget 300 s, write 16.6 s, horizon 30 days)."""
    return BakeRoster(
        tuple(
            BakeRosterRow(intent.page, intent.variant, 300.0, 16.6, 30, certified)
            for intent in intents
        )
    )


def bake_parts(
    baker: object,
    *,
    queries: BakeQueryCapture | None = None,
    process: ProcessBakeLedger | None = None,
) -> PageParts:
    """Return page parts over `baker`, a refusing query capture and a fresh process ledger."""
    return PageParts(
        cast("Any", baker),
        queries or BakeQueryCapture(RefusingQueries()),
        process or ProcessBakeLedger(peak=lambda: 0.0),
    )


def legacy_application(  # noqa: PLR0913 -- the application's three parts and its fields
    declared: DeclaredBakeIntents,
    baker: object,
    persistence: object,
    *,
    roster: BakeRoster | None = None,
    **fields: Any,
) -> PresetArtifactBakeApplication:
    """Compose the legacy application; every declared intent is a roster row by default."""
    return PresetArtifactBakeApplication(
        declared=declared,
        parts=bake_parts(baker),
        roster=roster or roster_for(declared.intents),
        persistence=cast("PresetArtifactPersistencePort", persistence),
        **fields,
    )


def apply_with_deadline(
    event: dict[str, object],
    application: PresetArtifactBakeApplication,
    remaining: Callable[[], int] = remaining_ms,
) -> tuple[str, ...]:
    """Apply one SQS event as the handler does: with the context's remaining-time callable."""
    batch = baker_module._batch_telemetry  # pyright: ignore[reportPrivateUsage]
    telemetry = batch(event, "fictional-execution")
    return baker_module.apply_records(
        event, application, replace(telemetry, remaining_ms=remaining)
    )


def _an_object(*_arguments: object, **_keywords: object) -> object:
    return object()


def _nothing(*_arguments: object, **_keywords: object) -> None:
    return None


COMPOSED_SETTINGS = PresetArtifactBakeSettings(
    artifact_bucket="fictional-artifacts",
    pointer_table="fictional-pointers",
    semantic_publications_table="fictional-publications",
    semantic_v2_bucket="fictional-semantic",
    athena_workgroup="adlytics-dev-v2-bake",
    semantic_v2_current_database="adlytics_dev_v2_current",
    athena_result_reuse_max_age_minutes=60,
    athena_query_batch_max_concurrency=10,
)
"""A fully configured fictional Baker environment (admission and a publications table)."""


def compose_baker(
    monkeypatch: pytest.MonkeyPatch,
    settings: PresetArtifactBakeSettings = COMPOSED_SETTINGS,
    *,
    graph_table: str = "",
) -> tuple[PresetArtifactBakeApplication, list[AthenaQueryTarget]]:
    """Run the real `baker.application` composition over fakes; return it and its targets.

    Every AWS client the composition would build is replaced, so it runs credential-free; the
    Athena adapter records the target it was composed with.
    """
    targets: list[AthenaQueryTarget] = []
    module = cast("Any", baker_module)

    def adapter(target: AthenaQueryTarget) -> RefusingQueries:
        targets.append(target)
        return RefusingQueries()

    monkeypatch.setattr(module, "preset_artifact_bake_settings", lambda: settings)
    monkeypatch.setattr(
        module,
        "graph_consumer_settings",
        lambda: SimpleNamespace(execution_graph_table=graph_table),
    )
    monkeypatch.setattr(module, "DynamoPresetArtifactPointerStore", _an_object)
    monkeypatch.setattr(module, "S3PresetArtifactBodyStore", _an_object)
    monkeypatch.setattr(module, "S3PresetArtifactCustodyStore", _an_object)
    monkeypatch.setattr(module, "AthenaQueryAdapter", adapter)
    monkeypatch.setattr(module, "DynamoCurrentPublications", _an_object)
    monkeypatch.setattr(module, "DynamoGraphReleaseStore", _an_object)
    monkeypatch.setattr(module, "boto3", SimpleNamespace(client=_an_object))
    monkeypatch.setattr(module, "_insight_fill_child", _nothing)
    baker_module.application.cache_clear()
    try:
        return baker_module.application(), targets
    finally:
        baker_module.application.cache_clear()
