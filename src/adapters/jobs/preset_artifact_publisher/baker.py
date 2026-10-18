# SPDX-License-Identifier: MIT-0
"""FIFO intent execution for retained preset-artifact publication."""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import lru_cache
from threading import Condition
from time import monotonic
from typing import Any, Final, Protocol, cast
from uuid import uuid4

import boto3
from aws_lambda_powertools import Logger

from adapters.config import (
    PresetArtifactBakeSettings,
    graph_consumer_settings,
    preset_artifact_bake_settings,
    tracing_settings,
)
from adapters.inbound.ingestion_handlers.ingress import queue_url_from_arn
from adapters.inbound.tool_handlers.gateway import contracts_root
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.jobs.execution_graph.post_grant_consumer import (
    ConsumeResult,
    PostGrantEnvelope,
    PostGrantEnvelopeError,
    post_grant_envelope,
)
from adapters.jobs.preset_artifact_publisher.audiences_source import (
    AUDIENCES_MATERIALIZATION_UNAVAILABLE,
    retained_audiences_preset_artifact_source,
)
from adapters.jobs.preset_artifact_publisher.bake_results import (
    BakePublication,
    BakeResult,
    BakeUnavailable,
    BakeUnchanged,
)
from adapters.jobs.preset_artifact_publisher.campaign_source import (
    retained_campaign_preset_artifact_source,
)
from adapters.jobs.preset_artifact_publisher.insight_fill import PresetInsightFillChild
from adapters.jobs.preset_artifact_publisher.new_customers_source import (
    retained_new_customers_preset_artifact_source,
)
from adapters.jobs.preset_artifact_publisher.page_bake import (
    PROCESS_LEDGER,
    PageParts,
    PageRender,
    PageWindow,
    render_page,
    stage_page,
)
from adapters.jobs.preset_artifact_publisher.reach_source import (
    REACH_MATERIALIZATION_UNAVAILABLE,
    retained_reach_preset_artifact_source,
)
from adapters.jobs.preset_artifact_publisher.retail_source import (
    retained_retail_health_preset_artifact_source,
)
from adapters.jobs.preset_artifact_publisher.retention_source import (
    RETENTION_MATERIALIZATION_UNAVAILABLE,
    retained_retention_preset_artifact_source,
)
from adapters.jobs.preset_artifact_publisher.scorecard_source import (
    retained_scorecard_preset_artifact_source,
)
from adapters.jobs.preset_artifact_publisher.transport import (
    BakeDelivery,
    BakeEnvelopeError,
    BakeIntent,
    BakeV3Body,
    DeclaredBakeIntents,
    load_declared_bake_intents,
    parse_bake_v3_body,
)
from adapters.outbound.athena.query import (
    BAKE_INVOCATION_WAIT,
    AthenaQueryAdapter,
    AthenaQueryTarget,
)
from adapters.outbound.bake_sources import BakeClock, BakeQueryCapture, BakeSourceReads
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.dynamodb.execution_graph_release import DynamoGraphReleaseStore
from adapters.outbound.dynamodb.insight_prompt_registry import DynamoInsightPromptRegistry
from adapters.outbound.dynamodb.preset_artifacts import DynamoPresetArtifactPointerStore
from adapters.outbound.dynamodb.semantic_state import DynamoCurrentPublications
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.operational_companions import companion_emitter
from adapters.outbound.preset_artifacts import PresetArtifactPersistence
from adapters.outbound.publication_selection import PublicationSelector
from adapters.outbound.s3.preset_artifacts import (
    S3PresetArtifactBodyStore,
    S3PresetArtifactCustodyStore,
)
from adapters.outbound.source_pack import load_source_packs
from adapters.outbound.sqs.insight_continuation import SqsInsightContinuation
from adapters.outbound.telemetry.bootstrap import configure_tracing, flushes_traces
from adapters.outbound.telemetry.graph_metrics import emit_cutover_drop
from adapters.outbound.telemetry.reporting import (
    METRICS,
    LogSeverity,
    OperationLog,
    Report,
    report_error,
    report_operation,
)
from adapters.outbound.telemetry.span_context import admitted, collecting
from adapters.outbound.telemetry.tracing import (
    PRESET_ARTIFACT_DELIVERY,
    PRESET_ARTIFACT_PROCESS,
    span,
    trace_boundary,
)
from domain.operations.companions import (
    CompanionFlow,
    CompanionFlowRequest,
    CompanionTerminal,
    companion_flow,
)
from domain.operations.execution_graph import AuthorityMode
from domain.operations.execution_graph_dispatch import (
    GRAPH_DISPATCH_NOT_ACTIVE,
    LEGACY_BAKE_AFTER_CUTOVER,
)
from domain.operations.execution_graph_release import (
    BakeRoster,
    CohortMember,
    unavailable_reason,
)
from domain.operations.generated_execution_graphs import BAKE_TASKS, ROUTES
from domain.operations.status import CompletionOutcome, FlowState
from domain.presentation.audiences import AUDIENCES_PAGE_ID
from domain.presentation.new_customers import NEW_CUSTOMERS_PAGE_ID
from domain.presentation.preset_artifacts import (
    PresetArtifactPage,
    PresetArtifactPublicationRequest,
    PresetArtifactPublicationResult,
    PresetArtifactVariant,
    SourceUnchanged,
)
from domain.presentation.reach import REACH_PAGE_ID
from domain.presentation.retention import RETENTION_PAGE_ID
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.shared_kernel.errors import ErrorCode, InvariantError
from ports.outbound.preset_artifacts import (
    LegacyPointerAfterCutoverError,
    PresetArtifactPersistencePort,
    PresetArtifactPointerStorePort,
)
from ports.outbound.query import (
    PeriodSeriesQueryPort,
    PeriodSeriesRequest,
    QueryPort,
    QueryRequest,
    QueryResult,
)

__all__ = [
    "NOT_GRAPH_BAKED_PAGES",
    "PAGE_BAKERS",
    "AthenaBatchAdmission",
    "BakeRegistryMismatchError",
    "PresetArtifactBakeApplication",
    "RegisteredPresetArtifactBaker",
    "apply_records",
    "handler",
]

_TRACING_SETTINGS = tracing_settings()
_TRACING = configure_tracing(_TRACING_SETTINGS)
LOGGER = Logger(service="adlytics-preset-artifact-baker")
_OPERATION = "preset_artifact.publish"
_COMPONENT = "preset_artifact_publisher.baker"

report_operation(
    OperationLog(
        operation=_OPERATION,
        component=_COMPONENT,
        phase="start",
        outcome="active" if _TRACING else "disabled",
        severity=LogSeverity.DEBUG,
        details={
            "stage": "startup",
            "tracing": _TRACING,
            "sample_ratio": _TRACING_SETTINGS.sample_ratio,
        },
    ),
    LOGGER,
)


class AthenaBatchAdmission:
    """One FIFO query admission shared by every render task in a bake invocation."""

    def __init__(self, queries: QueryPort, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("Athena batch admission capacity must be positive.")
        self._queries = queries
        self._capacity = capacity
        self._condition = Condition()
        self._waiting: deque[object] = deque()
        self._active = 0
        self._maximum = 0
        self._waited_ms = 0

    @property
    def maximum_occupancy(self) -> int:
        """Return the greatest simultaneous query occupancy observed so far."""
        with self._condition:
            return self._maximum

    @property
    def waited_ms(self) -> int:
        """Return the total time admitted queries waited for a slot since the last reset (A2)."""
        with self._condition:
            return self._waited_ms

    def reset_observation(self) -> None:
        """Begin one invocation only when the previous invocation released every token."""
        with self._condition:
            if self._active or self._waiting:
                raise RuntimeError("Athena admission cannot reset while work is active.")
            self._maximum = 0
            self._waited_ms = 0

    def run(self, request: QueryRequest) -> QueryResult:
        """Execute one query after FIFO admission and always return its token."""
        return self._admitted(lambda: self._queries.run(request))

    def run_series(self, request: PeriodSeriesRequest) -> QueryResult:
        """Execute one period series under the same FIFO ticket and capacity (A5.4)."""
        return self._admitted(
            lambda: cast("PeriodSeriesQueryPort", self._queries).run_series(request)
        )

    def _admitted(self, execute: Callable[[], QueryResult]) -> QueryResult:
        ticket = object()
        queued = monotonic()
        with self._condition:
            self._waiting.append(ticket)
            self._condition.wait_for(
                lambda: self._waiting[0] is ticket and self._active < self._capacity
            )
            self._waiting.popleft()
            self._active += 1
            self._maximum = max(self._maximum, self._active)
            waited = int((monotonic() - queued) * 1_000)
            self._waited_ms += waited
            self._condition.notify_all()
        try:
            # A2.1: the execution's `athena.query` span records how long it waited here.
            with admitted(waited):
                return execute()
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()


class PresetArtifactBaker(Protocol):
    """Render one already-validated declared intent from retained current truth."""

    def bake(self, intent: BakeIntent) -> BakeResult:
        """Return a publishable request or an explicit unavailable result; never a placeholder."""
        ...


class UnavailablePresetArtifactBaker:
    """Safe initial baker registration before campaigns and Retail bakers exist."""

    def bake(self, intent: BakeIntent) -> BakeUnavailable:
        """Declare missing page-specific work without fabricating an artifact."""
        return BakeUnavailable(intent, "page_baker_unavailable")


class CampaignPresetArtifactSource(Protocol):
    """Derive one capability-free Campaign body only from retained background material."""

    def publication_for(
        self, variant: PresetArtifactVariant
    ) -> PresetArtifactPublicationRequest | SourceUnchanged | None:
        """Return a complete retained-materializer publication or no currently bakeable artifact."""
        ...


class UnavailableCampaignPresetArtifactSource:
    """Safe composition until retained campaign material is exposed to this worker."""

    def publication_for(
        self, variant: PresetArtifactVariant
    ) -> PresetArtifactPublicationRequest | None:
        """Refuse to fabricate a Campaign artifact without retained materialization evidence."""
        del variant
        return None


def _bake_result(
    intent: BakeIntent,
    produced: PresetArtifactPublicationRequest | SourceUnchanged | None,
    unavailable_reason: str,
) -> BakeResult:
    """Map a source's typed result: unchanged, a publication, or no artifact (S6 §2.8, D26)."""
    if isinstance(produced, SourceUnchanged):
        return BakeUnchanged(intent, produced.revision)
    if produced is None:
        return BakeUnavailable(intent, unavailable_reason)
    return BakePublication(produced)


@dataclass(frozen=True, slots=True)
class CampaignPresetArtifactBaker:
    """Register Campaign background rendering without allowing request-handler dependencies."""

    source: CampaignPresetArtifactSource
    page: PresetArtifactPage = field(default_factory=lambda: PresetArtifactPage("campaigns"))

    def bake(self, intent: BakeIntent) -> BakeResult:
        """Publish only the request produced by retained campaign materialization."""
        if intent.page != self.page.value:
            return BakeUnavailable(intent, "page_baker_unavailable")
        return _bake_result(
            intent,
            self.source.publication_for(PresetArtifactVariant(intent.variant)),
            unavailable_reason(self.page.value),
        )

    def prefetch(self, intent: BakeIntent) -> None:
        """Delegate the A5.1 prefetch to a source that has one."""
        prefetch = getattr(self.source, "prefetch", None)
        if prefetch is not None and intent.page == self.page.value:
            prefetch(PresetArtifactVariant(intent.variant))


class UnavailableRetailHealthPresetArtifactSource:
    """Refuse to fabricate Retail Health until retained materialization is implemented."""

    def publication_for(
        self, variant: PresetArtifactVariant
    ) -> PresetArtifactPublicationRequest | None:
        del variant
        return None


class RetailHealthPresetArtifactSource(Protocol):
    """Derive one capability-free Retail body only from retained background material."""

    def publication_for(
        self, variant: PresetArtifactVariant
    ) -> PresetArtifactPublicationRequest | SourceUnchanged | None:
        """Return a complete retained-materializer publication or no bakeable artifact."""
        ...


@dataclass(frozen=True, slots=True)
class RetailHealthPresetArtifactBaker:
    """Register a background-only Retail page path without live measurement."""

    source: RetailHealthPresetArtifactSource
    page: PresetArtifactPage = field(default_factory=lambda: PresetArtifactPage("retail-health"))

    def bake(self, intent: BakeIntent) -> BakeResult:
        if intent.page != self.page.value:
            return BakeUnavailable(intent, "page_baker_unavailable")
        return _bake_result(
            intent,
            self.source.publication_for(PresetArtifactVariant(intent.variant)),
            unavailable_reason(self.page.value),
        )


BAKE_REGISTRY_MISMATCH: Final = ErrorCode("PRESET_ARTIFACT_BAKE", 2)


class BakeRegistryMismatchError(ValueError):
    """F7: the page->baker registry is not exactly the roster's pages; composition refuses."""

    def __init__(self, reason: str, page: str) -> None:
        super().__init__(f"{reason}: {page}")
        self.reason = reason
        self.page = page


class NewCustomersPresetArtifactSource(Protocol):
    """Derive one capability-free New customers body only from retained background material."""

    def publication_for(
        self, variant: PresetArtifactVariant
    ) -> PresetArtifactPublicationRequest | SourceUnchanged | None:
        """Return a complete publication, the current revision unchanged, or no artifact."""
        ...


class UnavailableNewCustomersPresetArtifactSource:
    """Refuse to fabricate New customers while its page entry or configuration is absent."""

    def publication_for(
        self, variant: PresetArtifactVariant
    ) -> PresetArtifactPublicationRequest | None:
        del variant
        return None


@dataclass(frozen=True, slots=True)
class NewCustomersPresetArtifactBaker:
    """Register the background-only New customers materializer (docs/ui/new-customers)."""

    source: NewCustomersPresetArtifactSource
    page: PresetArtifactPage = field(
        default_factory=lambda: PresetArtifactPage(NEW_CUSTOMERS_PAGE_ID)
    )

    def bake(self, intent: BakeIntent) -> BakeResult:
        if intent.page != self.page.value:
            return BakeUnavailable(intent, "page_baker_unavailable")
        # A None maps to the page's one generated reason (A5.4); an unchanged revision is
        # `revision_current`, never unavailable (D26).
        return _bake_result(
            intent,
            self.source.publication_for(PresetArtifactVariant(intent.variant)),
            unavailable_reason(self.page.value),
        )

    def prefetch(self, intent: BakeIntent) -> None:
        """Delegate the A5.1 prefetch to a source that has one."""
        prefetch = getattr(self.source, "prefetch", None)
        if prefetch is not None and intent.page == self.page.value:
            prefetch(PresetArtifactVariant(intent.variant))


class ReachPresetArtifactSource(Protocol):
    """Derive one capability-free Reach and funnel body only from retained background material."""

    def publication_for(
        self, variant: PresetArtifactVariant
    ) -> PresetArtifactPublicationRequest | SourceUnchanged | None:
        """Return a complete publication, the current revision unchanged, or no artifact."""
        ...


@dataclass(frozen=True, slots=True)
class UnavailableReachPresetArtifactSource:
    """Refuse to fabricate Reach and funnel while its page entry or configuration is absent.

    `reason` names why: the page entry or retained-truth configuration
    (`reach_materialization_unavailable`), or the governed AMC instance kind
    (`amc_instance_kind_missing`, `amc_instance_kind_unknown`).
    """

    reason: str = REACH_MATERIALIZATION_UNAVAILABLE

    def publication_for(
        self, variant: PresetArtifactVariant
    ) -> PresetArtifactPublicationRequest | None:
        del variant
        return None


@dataclass(frozen=True, slots=True)
class ReachPresetArtifactBaker:
    """Register the background-only Reach and funnel materializer (docs/ui/reach-and-funnel)."""

    source: ReachPresetArtifactSource
    page: PresetArtifactPage = field(default_factory=lambda: PresetArtifactPage(REACH_PAGE_ID))

    def bake(self, intent: BakeIntent) -> BakeResult:
        if intent.page != self.page.value:
            return BakeUnavailable(intent, "page_baker_unavailable")
        # A None maps to the page's one generated reason (A5.4); an unchanged revision is
        # `revision_current`, never unavailable (D26).
        return _bake_result(
            intent,
            self.source.publication_for(PresetArtifactVariant(intent.variant)),
            unavailable_reason(self.page.value),
        )

    def prefetch(self, intent: BakeIntent) -> None:
        """Delegate the A5.1 prefetch to a source that has one."""
        prefetch = getattr(self.source, "prefetch", None)
        if prefetch is not None and intent.page == self.page.value:
            prefetch(PresetArtifactVariant(intent.variant))


class RetentionPresetArtifactSource(Protocol):
    """Derive one capability-free Retention body only from retained background material."""

    def publication_for(
        self, variant: PresetArtifactVariant
    ) -> PresetArtifactPublicationRequest | SourceUnchanged | None:
        """Return a complete publication, the current revision unchanged, or no artifact."""
        ...


@dataclass(frozen=True, slots=True)
class UnavailableRetentionPresetArtifactSource:
    """Refuse to fabricate Retention while its page entry or configuration is absent."""

    reason: str = RETENTION_MATERIALIZATION_UNAVAILABLE

    def publication_for(
        self, variant: PresetArtifactVariant
    ) -> PresetArtifactPublicationRequest | None:
        del variant
        return None


@dataclass(frozen=True, slots=True)
class RetentionPresetArtifactBaker:
    """Register the background-only Retention materializer (docs/ui/retention)."""

    source: RetentionPresetArtifactSource
    page: PresetArtifactPage = field(default_factory=lambda: PresetArtifactPage(RETENTION_PAGE_ID))

    def bake(self, intent: BakeIntent) -> BakeResult:
        if intent.page != self.page.value:
            return BakeUnavailable(intent, "page_baker_unavailable")
        # A None maps to the page's one generated reason (A5.4); an unchanged revision is
        # `revision_current`, never unavailable (D26).
        return _bake_result(
            intent,
            self.source.publication_for(PresetArtifactVariant(intent.variant)),
            unavailable_reason(self.page.value),
        )

    def prefetch(self, intent: BakeIntent) -> None:
        """Delegate the A5.1 prefetch to a source that has one."""
        prefetch = getattr(self.source, "prefetch", None)
        if prefetch is not None and intent.page == self.page.value:
            prefetch(PresetArtifactVariant(intent.variant))


class AudiencesPresetArtifactSource(Protocol):
    """Derive one capability-free Audiences body only from retained background material."""

    def publication_for(
        self, variant: PresetArtifactVariant
    ) -> PresetArtifactPublicationRequest | SourceUnchanged | None:
        """Return a complete publication, the current revision unchanged, or no artifact."""
        ...


@dataclass(frozen=True, slots=True)
class UnavailableAudiencesPresetArtifactSource:
    """Refuse to fabricate Audiences while its page entry or configuration is absent."""

    reason: str = AUDIENCES_MATERIALIZATION_UNAVAILABLE

    def publication_for(
        self, variant: PresetArtifactVariant
    ) -> PresetArtifactPublicationRequest | None:
        del variant
        return None


@dataclass(frozen=True, slots=True)
class AudiencesPresetArtifactBaker:
    """Register the background-only Audiences materializer (docs/ui/audiences)."""

    source: AudiencesPresetArtifactSource
    page: PresetArtifactPage = field(default_factory=lambda: PresetArtifactPage(AUDIENCES_PAGE_ID))

    def bake(self, intent: BakeIntent) -> BakeResult:
        if intent.page != self.page.value:
            return BakeUnavailable(intent, "page_baker_unavailable")
        # A None maps to the page's one generated reason (A5.4); an unchanged revision is
        # `revision_current`, never unavailable (D26).
        return _bake_result(
            intent,
            self.source.publication_for(PresetArtifactVariant(intent.variant)),
            unavailable_reason(self.page.value),
        )

    def prefetch(self, intent: BakeIntent) -> None:
        """Delegate the A5.1 prefetch to a source that has one."""
        prefetch = getattr(self.source, "prefetch", None)
        if prefetch is not None and intent.page == self.page.value:
            prefetch(PresetArtifactVariant(intent.variant))


@dataclass(frozen=True, slots=True)
class RegisteredPresetArtifactBaker:
    """Dispatch each roster page through its one registry entry (F7, A1.2).

    The registry keys must equal the roster's pages: a roster page without a baker
    (`roster_page_without_baker`) or a baker for a page with no roster rows
    (`baker_page_not_in_roster`) refuses construction, so a roster intent can never reach the
    fallback. Only a declared page outside the roster does.
    """

    bakers: Mapping[str, PresetArtifactBaker]
    fallback: PresetArtifactBaker
    roster_pages: tuple[str, ...]
    admission: AthenaBatchAdmission | None = None

    def __post_init__(self) -> None:
        for page in self.roster_pages:
            if page not in self.bakers:
                raise BakeRegistryMismatchError("roster_page_without_baker", page)
        for page in self.bakers:
            if page not in self.roster_pages:
                raise BakeRegistryMismatchError("baker_page_not_in_roster", page)

    def registered_pages(self) -> tuple[str, ...]:
        """S6 §2.8 step 3 (A1.2): the roster's pages, each with its registry entry."""
        return self.roster_pages

    def bake(self, intent: BakeIntent) -> BakeResult:
        """Bake a roster page through its entry; any other declared page is the fallback's."""
        baker = self.bakers.get(intent.page)
        return self.fallback.bake(intent) if baker is None else baker.bake(intent)

    def prefetch(self, intent: BakeIntent) -> None:
        """Prefetch a roster page's CURRENT reads (A5.1); a result-independent page has none."""
        prefetch = getattr(self.bakers.get(intent.page), "prefetch", None)
        if prefetch is not None:
            prefetch(intent)


PageBakerFactory = Callable[
    [
        PresetArtifactBakeSettings,
        PresetArtifactPointerStorePort,
        PeriodSeriesQueryPort | None,
        BakeSourceReads | None,
    ],
    PresetArtifactBaker,
]
"""The one closed factory signature (A5.4): settings, pointers, the invocation's admitted and
captured `queries`, and its one pinnable CURRENT reader, which carries the bake clock."""


def _campaign_baker(  # noqa: PLR0913, PLR0917 -- the closed factory signature
    settings: PresetArtifactBakeSettings,
    pointers: PresetArtifactPointerStorePort,
    queries: PeriodSeriesQueryPort | None,
    current: BakeSourceReads | None,
) -> PresetArtifactBaker:
    source = (
        None
        if queries is None or current is None
        else retained_campaign_preset_artifact_source(settings, pointers, queries, current)
    )
    return CampaignPresetArtifactBaker(
        UnavailableCampaignPresetArtifactSource() if source is None else source
    )


def _retail_health_baker(  # noqa: PLR0913, PLR0917 -- the closed factory signature
    settings: PresetArtifactBakeSettings,
    pointers: PresetArtifactPointerStorePort,
    queries: PeriodSeriesQueryPort | None,
    current: BakeSourceReads | None,
) -> PresetArtifactBaker:
    source = (
        None
        if queries is None or current is None
        else retained_retail_health_preset_artifact_source(settings, pointers, queries, current)
    )
    return RetailHealthPresetArtifactBaker(
        UnavailableRetailHealthPresetArtifactSource() if source is None else source
    )


class ScorecardPresetArtifactSource(Protocol):
    """Derive one capability-free Scorecard body only from retained background material."""

    def publication_for(
        self, variant: PresetArtifactVariant
    ) -> PresetArtifactPublicationRequest | SourceUnchanged | None:
        """Return a complete publication, the current revision unchanged, or no artifact."""
        ...


class UnavailableScorecardPresetArtifactSource:
    """Refuse to fabricate a Scorecard while its page entry or configuration is absent."""

    def publication_for(
        self, variant: PresetArtifactVariant
    ) -> PresetArtifactPublicationRequest | None:
        del variant
        return None


@dataclass(frozen=True, slots=True)
class ScorecardPresetArtifactBaker:
    """Register the background-only Scorecard materializer (design §6.1)."""

    source: ScorecardPresetArtifactSource
    page: PresetArtifactPage = field(default_factory=lambda: PresetArtifactPage("scorecard"))

    def bake(self, intent: BakeIntent) -> BakeResult:
        if intent.page != self.page.value:
            return BakeUnavailable(intent, "page_baker_unavailable")
        return _bake_result(
            intent,
            self.source.publication_for(PresetArtifactVariant(intent.variant)),
            unavailable_reason(self.page.value),
        )


def _configured[T](
    queries: PeriodSeriesQueryPort | None,
    current: BakeSourceReads | None,
    compose: Callable[[PeriodSeriesQueryPort, BakeSourceReads], T | None],
) -> T | None:
    """Compose a D2 source over the shared port and reader, or None while either is absent."""
    if queries is None or current is None:
        return None
    return compose(queries, current)


def _scorecard_baker(  # noqa: PLR0913, PLR0917 -- the closed factory signature
    settings: PresetArtifactBakeSettings,
    pointers: PresetArtifactPointerStorePort,
    queries: PeriodSeriesQueryPort | None,
    current: BakeSourceReads | None,
) -> PresetArtifactBaker:
    source = _configured(
        queries,
        current,
        lambda port, reader: retained_scorecard_preset_artifact_source(
            settings, pointers, port, reader
        ),
    )
    return ScorecardPresetArtifactBaker(
        UnavailableScorecardPresetArtifactSource() if source is None else source
    )


def _new_customers_baker(  # noqa: PLR0913, PLR0917 -- the closed factory signature
    settings: PresetArtifactBakeSettings,
    pointers: PresetArtifactPointerStorePort,
    queries: PeriodSeriesQueryPort | None,
    current: BakeSourceReads | None,
) -> PresetArtifactBaker:
    source = _configured(
        queries,
        current,
        lambda port, reader: retained_new_customers_preset_artifact_source(
            settings, pointers, port, reader
        ),
    )
    return NewCustomersPresetArtifactBaker(
        UnavailableNewCustomersPresetArtifactSource() if source is None else source
    )


def _reach_baker(  # noqa: PLR0913, PLR0917 -- the closed factory signature
    settings: PresetArtifactBakeSettings,
    pointers: PresetArtifactPointerStorePort,
    queries: PeriodSeriesQueryPort | None,
    current: BakeSourceReads | None,
) -> PresetArtifactBaker:
    source = _configured(
        queries,
        current,
        lambda port, reader: retained_reach_preset_artifact_source(
            settings, pointers, port, reader
        ),
    )
    return ReachPresetArtifactBaker(
        UnavailableReachPresetArtifactSource() if source is None else source
    )


def _retention_baker(  # noqa: PLR0913, PLR0917 -- the closed factory signature
    settings: PresetArtifactBakeSettings,
    pointers: PresetArtifactPointerStorePort,
    queries: PeriodSeriesQueryPort | None,
    current: BakeSourceReads | None,
) -> PresetArtifactBaker:
    source = _configured(
        queries,
        current,
        lambda port, reader: retained_retention_preset_artifact_source(
            settings, pointers, port, reader
        ),
    )
    return RetentionPresetArtifactBaker(
        UnavailableRetentionPresetArtifactSource() if source is None else source
    )


def _audiences_baker(  # noqa: PLR0913, PLR0917 -- the closed factory signature
    settings: PresetArtifactBakeSettings,
    pointers: PresetArtifactPointerStorePort,
    queries: PeriodSeriesQueryPort | None,
    current: BakeSourceReads | None,
) -> PresetArtifactBaker:
    source = _configured(
        queries,
        current,
        lambda port, reader: retained_audiences_preset_artifact_source(
            settings, pointers, port, reader
        ),
    )
    return AudiencesPresetArtifactBaker(
        UnavailableAudiencesPresetArtifactSource() if source is None else source
    )


PAGE_BAKERS: Final[Mapping[str, PageBakerFactory]] = {
    "campaigns": _campaign_baker,
    "retail-health": _retail_health_baker,
    "scorecard": _scorecard_baker,
    "new-customers": _new_customers_baker,
    "reach": _reach_baker,
    "retention": _retention_baker,
    "audiences": _audiences_baker,
}
"""F7: the one closed page->baker registry; each factory takes the invocation's shared,
admission-wrapped `QueryPort` (None when unconfigured: the page fails closed as unavailable)."""

NOT_GRAPH_BAKED_PAGES: Final[frozenset[str]] = frozenset(
    {"channel-performance", "workspace", "data-health", "media-efficiency"}
)
"""A1.2: the declared pages GRAPH has never baked. Every other declared page is a roster page
with a registry entry; a newly declared page joins the roster or this set in a reviewed change."""


@dataclass(frozen=True, slots=True)
class _BakeReportContext:
    """Invocation lineage and durable completion count for one bake result."""

    execution_id: str
    plan_digest: str
    completed: int
    total: int


class GraphBakeConsumer(Protocol):
    """S6 §2.8: the graph bake (a `PostGrantRuntime`), as the FIFO consumer sees it."""

    def consume(
        self, envelope: PostGrantEnvelope, remaining_ms: Callable[[], int]
    ) -> ConsumeResult:
        """Consume one fan-out envelope; the result's `acknowledge` decides the batch item."""
        ...

    def consume_v3(self, delivery: BakeDelivery, remaining_ms: Callable[[], int]) -> ConsumeResult:
        """Consume one V3 task or finalize message; a task's deadline derives from the time."""
        ...


def _legacy_mode() -> AuthorityMode:
    return AuthorityMode.LEGACY


@dataclass(frozen=True, slots=True)
class PresetArtifactBakeApplication:
    """Validate closed FIFO data, render through the one page execution, persist real artifacts.

    `parts.baker` is the registry; a roster page renders only through `render_page` (ADR-142).
    """

    declared: DeclaredBakeIntents
    parts: PageParts
    """The registry, the counted query port and the process ledger every page renders on."""
    roster: BakeRoster
    """The roster rows: a declared intent with a row renders under its window (ADR-144)."""
    persistence: PresetArtifactPersistencePort
    insight_fill: PresetInsightFillChild | None = None
    graph: GraphBakeConsumer | None = None
    """S6 §2.8: the graph bake, composed only where the graph authority is provisioned."""
    mode: Callable[[], AuthorityMode] = _legacy_mode
    """S6 §2.9: one strong mode read per batch, before any record is parsed."""
    cutover_drop: Callable[[str, int], None] = emit_cutover_drop
    """S6 §2.9 (D6, AGG3-6): counts a legacy intent dropped in GRAPH, or refused at its write."""
    clock: BakeClock = field(default_factory=BakeClock)
    """The sources' one bake clock (A5.1 I6), opened on the wall clock for the legacy render."""

    def apply(  # noqa: PLR0913 -- the record, its lineage and the invocation's deadline inputs
        self,
        body: object,
        context: _BakeReportContext | None = None,
        *,
        remaining_ms: Callable[[], int],
        final: bool,
    ) -> BakeResult | PresetArtifactPublicationResult | None:
        """Acknowledge malformed data; retry only a real baking or persistence failure.

        A declared intent with no roster row (a page with no bake source) takes the registry's
        fallback, which runs no statement, so it needs no window. A roster intent renders through
        `render_page` under the invocation's `remaining_ms` and its row (ADR-144), then writes
        through `stage_page`.
        """
        intent = BakeIntent.parse(body)
        if intent is None or not self.declared.contains(intent):
            return None
        key = f"{intent.page}#{intent.variant}"
        if not self.roster.contains(key):
            started = monotonic()
            fallback = self.parts.baker.bake(intent)
            if isinstance(fallback, BakePublication):
                raise ValueError("a declared page outside the bake roster produced an artifact.")
            _report_bake(
                _BakeResultReport(
                    intent,
                    "unchanged" if isinstance(fallback, BakeUnchanged) else "unavailable",
                    _BakeStageDurations(int((monotonic() - started) * 1_000), 0),
                    context,
                )
            )
            return fallback
        window = PageWindow(
            remaining_ms, self.roster.row(key), float(BAKE_TASKS["tail_seconds"]), final
        )
        started = monotonic()
        # A5.1 I6: the pre-cutover legacy render is the one wall-clock bake (census-pinned).
        with collecting() as statistics, self.clock.legacy_wall():
            page = render_page(self.parts, intent, window)
        result = page.result
        source_ms = int((monotonic() - started) * 1_000)
        observed = _PageReport(sum(item.throttled_starts for item in statistics), page)
        if isinstance(result, BakeUnavailable | BakeUnchanged):
            # D26: an unchanged page writes nothing, as an unavailable one does; only the
            # report label differs (a report change, not a write change).
            _report_bake(
                _BakeResultReport(
                    intent,
                    "unchanged" if isinstance(result, BakeUnchanged) else "unavailable",
                    _BakeStageDurations(source_ms, 0),
                    context,
                    observed,
                )
            )
            return result
        request = result.request
        if (request.body.page.value, request.body.variant.value) != (intent.page, intent.variant):
            raise ValueError(
                "preset artifact baker returned an artifact for another declared intent."
            )
        persist_started = monotonic()
        try:
            published = stage_page(self.persistence.publish, request, window)
        except LegacyPointerAfterCutoverError:
            # AGG3-6: the CAS committed after this batch read the mode; zero effect, acked.
            self.cutover_drop(LEGACY_BAKE_AFTER_CUTOVER, 1)
            _report_bake(
                _BakeResultReport(
                    intent,
                    "after_cutover",
                    _BakeStageDurations(source_ms, int((monotonic() - persist_started) * 1_000)),
                    context,
                    observed,
                )
            )
            return None
        if self.insight_fill is not None and published.pointer.outcome.value == "advanced":
            # §5.3: enqueued strictly after the pointer advanced; best-effort by contract.
            self.insight_fill.warm(request)
        _report_bake(
            _BakeResultReport(
                intent,
                "published",
                _BakeStageDurations(source_ms, int((monotonic() - persist_started) * 1_000)),
                context,
                observed,
            )
        )
        return published


@dataclass(frozen=True, slots=True)
class _BakeStageDurations:
    """Per-bake stage split: sourcing covers resolve/snapshot/measure/render; persist the writes."""

    source_ms: int
    persist_ms: int


@dataclass(frozen=True, slots=True)
class _PageReport:
    """What a roster page's render observed: its throttled starts and its page execution."""

    throttled_starts: int
    page: PageRender


@dataclass(frozen=True, slots=True)
class _BakeResultReport:
    """One per-bake lifecycle projection with optional invocation lineage."""

    intent: BakeIntent
    outcome: str
    durations: _BakeStageDurations
    context: _BakeReportContext | None
    observed: _PageReport | None = None
    """A roster page's render; None for a declared page with no bake source (no render)."""


def _report_bake(request: _BakeResultReport) -> None:
    """Emit one structured per-bake stage-duration event (scale-latency runbook input)."""
    intent = request.intent
    outcome = request.outcome
    durations = request.durations
    context = request.context
    observed = request.observed
    page = (
        {}
        if observed is None
        else {
            "throttled_starts": observed.throttled_starts,
            "deadline_clamped": observed.page.deadline_clamped,
        }
    )
    lineage = (
        {}
        if context is None
        else {
            "execution_id": context.execution_id,
            "plan_digest": context.plan_digest,
            "stage": "bake",
            "completed": context.completed,
            "total": context.total,
        }
    )
    report_operation(
        OperationLog(
            operation=_OPERATION,
            component=_COMPONENT,
            phase="terminal",
            outcome=outcome,
            severity=LogSeverity.INFO,
            details={
                "page": intent.page,
                "variant": intent.variant,
                "source_ms": durations.source_ms,
                "persist_ms": durations.persist_ms,
                "total_ms": durations.source_ms + durations.persist_ms,
                **page,
                **lineage,
            },
        ),
        LOGGER,
    )


@dataclass(frozen=True, slots=True)
class _BakeTelemetry:
    execution_id: str
    plan_digest: str
    total: int
    remaining_ms: Callable[[], int] | None = None
    """The invocation's remaining time (the Lambda context's callable); None is a defect that
    `apply_records` refuses before any record (`BAKE_DEADLINE_UNBOUND`, ADR-144)."""


def _batch_telemetry(event: object, execution_id: str) -> _BakeTelemetry:
    if not isinstance(event, dict):
        return _BakeTelemetry(
            execution_id=execution_id,
            plan_digest=f"sha256:{hashlib.sha256(b'[]').hexdigest()}",
            total=0,
        )
    records = cast("dict[str, object]", event).get("Records")
    raw_records = cast("list[object]", records) if isinstance(records, list) else []
    intents: list[str] = []
    for raw in raw_records:
        if not isinstance(raw, dict):
            continue
        intent = BakeIntent.parse(cast("dict[str, object]", raw).get("body"))
        if intent is not None:
            intents.append(intent.serialize())
    encoded = json.dumps(intents, separators=(",", ":")).encode()
    return _BakeTelemetry(
        execution_id=execution_id,
        plan_digest=f"sha256:{hashlib.sha256(encoded).hexdigest()}",
        total=len(raw_records),
    )


def _bake_flow(record: dict[str, object]) -> CompanionFlow | None:
    """Derive one deterministic bake flow from stable SQS message attributes.

    `messageId` and `SentTimestamp` are constant across redeliveries of one message and
    the receive count distinguishes attempts, so a crashed receive leaves a start-only
    flow and the retry opens its own, without any conflict at the projection.
    """
    message_id = record.get("messageId")
    attributes = record.get("attributes")
    if not isinstance(message_id, str) or not message_id or not isinstance(attributes, dict):
        return None
    typed = cast("dict[str, object]", attributes)
    sent, receive_count = typed.get("SentTimestamp"), typed.get("ApproximateReceiveCount")
    if not isinstance(sent, str) or not sent.isdigit() or not isinstance(receive_count, str):
        return None
    return companion_flow(
        CompanionFlowRequest(
            surface="preset_bake",
            authority_kind="bake_message",
            identity={"message_id": message_id, "receive_count": receive_count},
            started_at=datetime.fromtimestamp(int(sent) / 1_000, tz=UTC),
            start_state=FlowState.RUNNING,
            start_stage="bake_message_received",
        )
    )


def _emit_bake_companion(
    record: dict[str, object],
    result: BakeResult | PresetArtifactPublicationResult | None,
) -> None:
    """Report one attested bake settlement; malformed intents produce no flow."""
    if result is None:
        return
    flow = _bake_flow(record)
    if flow is None:
        return
    if isinstance(result, BakeUnavailable):
        state, outcome, stage, reason = (
            FlowState.COMPLETED,
            CompletionOutcome.COMPLETED_REFUSED,
            "bake_unavailable_attested",
            result.reason,
        )
        digest_value = {"page": result.intent.page, "variant": result.intent.variant}
    elif isinstance(result, BakeUnchanged):
        state, outcome, stage, reason = (
            FlowState.COMPLETED,
            CompletionOutcome.COMPLETED_NO_CHANGE,
            "bake_unchanged_attested",
            "source_unchanged",
        )
        digest_value = {
            "page": result.intent.page,
            "variant": result.intent.variant,
            "revision": result.revision.value,
        }
    else:
        published = cast("PresetArtifactPublicationResult", result)
        pointer = published.pointer.outcome.value
        state, outcome, stage, reason = (
            FlowState.COMPLETED,
            (
                CompletionOutcome.SUCCEEDED
                if pointer == "advanced"
                else CompletionOutcome.COMPLETED_NO_CHANGE
            ),
            "pointer_last_attested",
            None if pointer == "advanced" else f"pointer_{pointer}",
        )
        digest_value = {"pointer_outcome": pointer, "body_digest": published.body.digest}
    companion_emitter(_OPERATION).emit(
        flow.start,
        flow.terminal(
            CompanionTerminal(
                state=state,
                outcome=outcome,
                stage=stage,
                # Message-derived, never a wall clock: the terminal instant is
                # identity-bearing, and a redelivery of this receive must reproduce
                # a byte-identical event that deduplicates at the projection.
                occurred_at=flow.started_at,
                authority_digest_value=digest_value,
                reason_code=reason,
            )
        ),
    )


@trace_boundary(PRESET_ARTIFACT_DELIVERY)
def apply_records(
    event: object,
    application: PresetArtifactBakeApplication,
    telemetry: _BakeTelemetry | None = None,
) -> tuple[str, ...]:
    """Return only individual SQS messages whose accepted bake work could not complete."""
    if not isinstance(event, dict):
        return ()
    records = cast("dict[str, object]", event).get("Records")
    if not isinstance(records, list):
        return ()
    invocation = telemetry or _batch_telemetry(cast("dict[str, object]", event), uuid4().hex)
    mode = application.mode()
    remaining_ms = invocation.remaining_ms
    if remaining_ms is None:
        # ADR-144: no bake on either entry runs without the invocation's deadline. A deployed
        # Lambda always has the callable, so only a mis-composed caller reaches this.
        report_error(
            InvariantError(
                BAKE_DEADLINE_UNBOUND_CODE,
                "A bake invocation has no remaining-time callable.",
                context={"signal": BAKE_DEADLINE_UNBOUND},
            ),
            Report(operation=_OPERATION, correlation_id=invocation.execution_id, logger=LOGGER),
        )
        return tuple(
            str(cast("dict[str, object]", raw).get("messageId", ""))
            for raw in cast("list[object]", records)
            if isinstance(raw, dict)
        )
    failures: list[str] = []
    completed = 0
    for ordinal, raw in enumerate(cast("list[object]", records), start=1):
        if not isinstance(raw, dict):
            completed += 1
            continue
        record = cast("dict[str, object]", raw)
        graph = _graph_record(record, application, (mode, invocation, remaining_ms))
        if graph is not None:
            completed += 1 if graph else 0
            if not graph:
                failures.append(str(record.get("messageId", "")))
            continue
        if mode is AuthorityMode.GRAPH:
            # D6: a legacy intent in GRAPH, including one on the FIFO at the CAS, has zero
            # effect; the first granted run's bake re-evaluates every registered page.
            application.cutover_drop(LEGACY_BAKE_AFTER_CUTOVER, 1)
            completed += 1
            continue
        with span(
            PRESET_ARTIFACT_DELIVERY,
            operation_id=_OPERATION,
            execution_id=invocation.execution_id,
            plan_digest=invocation.plan_digest,
            stage="delivery",
            completed=ordinal - 1,
            total=invocation.total,
        ) as delivery_span:
            try:
                result = application.apply(
                    record.get("body"),
                    _BakeReportContext(
                        invocation.execution_id,
                        invocation.plan_digest,
                        completed + 1,
                        invocation.total,
                    ),
                    remaining_ms=remaining_ms,
                    final=_final_receive(record),
                )
                _emit_bake_companion(record, result)
                completed += 1
                delivery_span.record(completed=completed)
            except Exception as error:
                message_id = record.get("messageId")
                report_error(
                    error,
                    Report(
                        operation=_OPERATION,
                        correlation_id=invocation.execution_id,
                        logger=LOGGER,
                    ),
                )
                report_operation(
                    OperationLog(
                        operation=_OPERATION,
                        component=_COMPONENT,
                        phase="terminal",
                        outcome="retry",
                        severity=LogSeverity.WARNING,
                        details={
                            "execution_id": invocation.execution_id,
                            "plan_digest": invocation.plan_digest,
                            "stage": "bake",
                            "completed": completed,
                            "total": invocation.total,
                        },
                    ),
                    LOGGER,
                )
                if isinstance(message_id, str) and message_id:
                    failures.append(message_id)
    return tuple(failures)


GRAPH_DISPATCH_NOT_ACTIVE_CODE = ErrorCode("GRAPH_POST_GRANT", 6)
"""A graph bake envelope received outside `GRAPH`: zero effect, left for the DLQ."""

BAKE_DEADLINE_UNBOUND: Final = "BAKE_DEADLINE_UNBOUND"
BAKE_DEADLINE_UNBOUND_CODE = ErrorCode("PRESET_ARTIFACT_BAKE", 5)
"""An invocation with no remaining-time callable: every record a batch-item failure (ADR-144)."""


def _final_receive(record: dict[str, object]) -> bool:
    """Whether a legacy record is on its last receive; an unreadable count is not final.

    Read as `_delivery` reads a V3 record's count and compared as `BakeDelivery.final` compares,
    so a count it can't read retries rather than settling an absence, and the queue's
    `maxReceiveCount` still dead-letters it.
    """
    attributes = record.get("attributes")
    typed = cast("dict[str, object]", attributes) if isinstance(attributes, dict) else {}
    receive = typed.get("ApproximateReceiveCount")
    if not isinstance(receive, str) or not receive.isdecimal():
        return False
    return int(receive) >= int(ROUTES["bake"]["max_receive_count"])


def _delivery(record: dict[str, object], body: BakeV3Body) -> BakeDelivery | None:
    attributes = record.get("attributes")
    typed = cast("dict[str, object]", attributes) if isinstance(attributes, dict) else {}
    receive, message_id = typed.get("ApproximateReceiveCount"), record.get("messageId")
    if not isinstance(receive, str) or not receive.isdecimal() or not isinstance(message_id, str):
        return None
    sent, first = typed.get("SentTimestamp"), typed.get("ApproximateFirstReceiveTimestamp")
    source, handle = record.get("eventSourceARN"), record.get("receiptHandle")
    try:
        queue_url = queue_url_from_arn(source) if isinstance(source, str) and source else ""
    except ValueError:
        return None
    return BakeDelivery(
        body,
        cast("str", record.get("body")),
        message_id,
        int(receive),
        sent if isinstance(sent, str) else None,
        first if isinstance(first, str) else None,
        receipt_handle=handle if isinstance(handle, str) else "",
        queue_url=queue_url,
    )


def _graph_record(
    record: dict[str, object],
    application: PresetArtifactBakeApplication,
    batch: tuple[AuthorityMode, _BakeTelemetry, Callable[[], int]],
) -> bool | None:
    """Route a graph envelope (plan 4.6); None for a legacy intent, else whether it is acked.

    A V3 task or finalize body (`graph_bake_task_v3`, `bake_finalize`) goes to its consumer; a
    `bake` POST_GRANT body is the fan-out control. A graph envelope is accepted only in `GRAPH`
    with the graph bake composed; in any other mode it is refused with zero effect and left
    unacknowledged, as TableWriter does. A malformed body is refused before any side effect.
    """
    mode, invocation, remaining_ms = batch
    report = Report(operation=_OPERATION, correlation_id=invocation.execution_id, logger=LOGGER)
    try:
        v3 = parse_bake_v3_body(record.get("body"))
        envelope = None if v3 is not None else post_grant_envelope(record, CohortMember.BAKE)
    except (BakeEnvelopeError, PostGrantEnvelopeError) as malformed:
        report_error(malformed, report)
        return False
    if v3 is None and envelope is None:
        return None
    if mode is not AuthorityMode.GRAPH or application.graph is None:
        report_error(
            InvariantError(
                GRAPH_DISPATCH_NOT_ACTIVE_CODE,
                "A graph bake envelope arrived outside GRAPH.",
                context={"signal": GRAPH_DISPATCH_NOT_ACTIVE},
            ),
            report,
        )
        return False
    if v3 is not None:
        delivery = _delivery(record, v3)
        if delivery is None:
            report_error(
                BakeEnvelopeError("a V3 bake record has no valid receive attributes."), report
            )
            return False
        return application.graph.consume_v3(delivery, remaining_ms).acknowledge
    result = application.graph.consume(cast("PostGrantEnvelope", envelope), remaining_ms)
    return result.acknowledge


@lru_cache(maxsize=1)
def application() -> PresetArtifactBakeApplication:
    """Compose immutable persistence and the registered background-only campaign baker."""
    settings = preset_artifact_bake_settings()
    graph_table = graph_consumer_settings().execution_graph_table
    # S6 §2.9 (AGG3-6): every legacy pointer write ConditionChecks the graph mode item.
    pointers = DynamoPresetArtifactPointerStore(
        settings.pointer_table, graph_table=graph_table or None
    )
    persistence = PresetArtifactPersistence(
        S3PresetArtifactBodyStore(settings.artifact_bucket),
        pointers,
        S3PresetArtifactCustodyStore(settings.artifact_bucket),
    )
    # One adapter and FIFO admission are shared by every registered source in this invocation.
    admission: AthenaBatchAdmission | None = None
    if all(
        (
            settings.athena_workgroup,
            settings.semantic_v2_current_database,
            settings.athena_result_reuse_max_age_minutes > 0,
            settings.athena_query_batch_max_concurrency > 0,
        )
    ):
        admission = AthenaBatchAdmission(
            AthenaQueryAdapter(
                AthenaQueryTarget(
                    workgroup=settings.athena_workgroup,
                    database=settings.semantic_v2_current_database,
                    reuse_minutes=settings.athena_result_reuse_max_age_minutes,
                    wait=BAKE_INVOCATION_WAIT,
                )
            ),
            settings.athena_query_batch_max_concurrency,
        )
    # Fail closed: without the retained-truth configuration each page stays explicitly
    # unavailable. A source is composed only over the shared admission (A1.3): no factory
    # builds its own adapter. Every source's CURRENT reads go through one pinnable reader and
    # its queries through one capturable port (plan 4.3), so the V3 bake pins and binds them.
    roster = BakeRoster.from_contract()
    clock = BakeClock()
    queries: BakeQueryCapture | None = None
    reads: BakeSourceReads | None = None
    if admission is not None and settings.semantic_publications_table:
        current = DynamoCurrentPublications(settings.semantic_publications_table)
        root = contracts_root()
        reads = BakeSourceReads(
            current,
            PublicationSelector(
                current, load_source_packs(root), load_commerce_identity_bridge(root)
            ),
            clock,
        )
        queries = BakeQueryCapture(admission)
    baker = RegisteredPresetArtifactBaker(
        bakers={
            page: PAGE_BAKERS[page](settings, pointers, queries, reads) for page in roster.pages
        },
        fallback=UnavailablePresetArtifactBaker(),
        roster_pages=roster.pages,
        admission=admission,
    )
    declared = load_declared_bake_intents(contracts_root())
    # One page execution for both entries (ADR-142): never a None query port, so an unconfigured
    # deployment still renders each roster page's unavailable result under its count.
    parts = PageParts(
        baker=baker, queries=queries or BakeQueryCapture(_NoQueries()), process=PROCESS_LEDGER
    )
    if not graph_table:
        return PresetArtifactBakeApplication(
            declared=declared,
            parts=parts,
            roster=roster,
            persistence=persistence,
            insight_fill=_insight_fill_child(settings),
            clock=clock,
        )
    # The V3 graph bake shares the composed registry, reader, port and persistence; it is
    # imported here, at composition, so a deployment without the graph authority never loads it.
    from adapters.jobs.preset_artifact_publisher.graph_bake import (  # noqa: PLC0415
        BakeRuntime,
        GraphBakeRuntime,
    )

    store = DynamoGraphReleaseStore(graph_table, boto3.client("dynamodb"))
    sqs = boto3.client("sqs")

    def visibility(queue_url: str, receipt_handle: str, seconds: int) -> None:
        # The finalizer's armed nack (plan 5.1): the event source's consume grant covers it.
        sqs.change_message_visibility(
            QueueUrl=queue_url, ReceiptHandle=receipt_handle, VisibilityTimeout=seconds
        )

    now = lambda: datetime.now(UTC)  # noqa: E731
    return PresetArtifactBakeApplication(
        declared=declared,
        parts=parts,
        roster=roster,
        persistence=persistence,
        insight_fill=_insight_fill_child(settings),
        clock=clock,
        graph=GraphBakeRuntime(
            store,
            BakeRuntime(
                roster=roster,
                baker=baker,
                reads=reads or BakeSourceReads(_NoCurrentReads(), _NoCurrentReads(), clock),
                queries=parts.queries,
                stage=persistence,
                pointers=pointers,
                pointer_table=settings.pointer_table,
                query_cap=settings.athena_query_batch_max_concurrency,
                now=now,
                logger=LOGGER,
                admission=admission,
                process=parts.process,
            ),
            now,
            LOGGER,
            visibility=visibility,
        ),
        mode=lambda: store.read_mode().mode,
    )


class _NoQueries:
    """The query port of an unconfigured invocation: every page fails closed before any query."""

    def run(self, request: QueryRequest) -> QueryResult:
        del request
        raise InvariantError(
            ErrorCode("PRESET_ARTIFACT_BAKE", 3), "an unconfigured bake reached a query."
        )

    def run_series(self, request: PeriodSeriesRequest) -> QueryResult:
        del request
        raise InvariantError(
            ErrorCode("PRESET_ARTIFACT_BAKE", 3), "an unconfigured bake reached a query."
        )


class _NoCurrentReads:
    """The CURRENT reader of an unconfigured invocation: unreachable, by construction."""

    def select_exact(self, request: object) -> tuple[Any, ...]:
        """Refuse: an unconfigured bake pins no source."""
        del request
        raise InvariantError(
            ErrorCode("PRESET_ARTIFACT_BAKE", 4), "an unconfigured bake reached select_exact."
        )

    def __getattr__(self, name: str) -> Any:
        raise InvariantError(
            ErrorCode("PRESET_ARTIFACT_BAKE", 4), f"an unconfigured bake reached {name}."
        )


def _insight_fill_child(settings: PresetArtifactBakeSettings) -> PresetInsightFillChild | None:
    """Compose the optional §5.3 child, or none while its targets are unconfigured."""
    if not settings.insight_continuation_queue_url or not settings.insight_prompt_registry_table:
        report_operation(
            OperationLog(
                operation=_OPERATION,
                component=_COMPONENT,
                phase="start",
                outcome="disabled",
                severity=LogSeverity.WARNING,
                details={"stage": "insight_fill_composition"},
            ),
            LOGGER,
        )
        return None
    root = contracts_root()
    bridge = load_commerce_identity_bridge(root)
    return PresetInsightFillChild(
        prompts=DynamoInsightPromptRegistry(settings.insight_prompt_registry_table),
        continuation=SqsInsightContinuation(settings.insight_continuation_queue_url),
        scope=AuthorizedAnalyticalScope(frozenset({bridge.commerce_scope})),
        contracts={contract.id: contract for _, contract in load_insight_contracts(root)},
        catalog=load_catalog(root),
    )


@trace_boundary(PRESET_ARTIFACT_PROCESS)
@flushes_traces
@METRICS.log_metrics
@LOGGER.inject_lambda_context
def handler(event: dict[str, Any], _context: object) -> dict[str, list[dict[str, str]]]:
    """Use SQS partial-batch retry only for admitted work that raised an execution failure."""
    execution_id = str(getattr(_context, "aws_request_id", "") or uuid4().hex)
    remaining: object = getattr(_context, "get_remaining_time_in_millis", None)
    telemetry = _batch_telemetry(event, execution_id)
    if callable(remaining):
        telemetry = replace(telemetry, remaining_ms=cast("Callable[[], int]", remaining))
    with span(
        PRESET_ARTIFACT_PROCESS,
        operation_id=_OPERATION,
        execution_id=telemetry.execution_id,
        plan_digest=telemetry.plan_digest,
        stage="batch",
        completed=0,
        total=telemetry.total,
    ) as process_span:
        failures = apply_records(event, application(), telemetry)
        completed = telemetry.total - len(failures)
        outcome = "completed" if not failures else "retry"
        process_span.record(completed=completed, outcome=outcome)
        report_operation(
            OperationLog(
                operation=_OPERATION,
                component=_COMPONENT,
                phase="terminal",
                outcome=outcome,
                severity=LogSeverity.INFO if not failures else LogSeverity.WARNING,
                details={
                    "execution_id": telemetry.execution_id,
                    "plan_digest": telemetry.plan_digest,
                    "stage": "batch",
                    "completed": completed,
                    "total": telemetry.total,
                },
            ),
            LOGGER,
        )
    return {"batchItemFailures": [{"itemIdentifier": item} for item in failures]}
