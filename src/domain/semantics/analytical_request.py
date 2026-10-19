# SPDX-License-Identifier: MIT-0
"""Resolve candidate analytical intent into one all-or-nothing measurement request."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import cast

from domain.catalog.capability import Catalog, Metric, RefusalReason
from domain.conversation.temporal import (
    ReferencePeriod,
    reference_period_candidates,
    reference_periods,
)
from domain.measurement.lenses import REPORT_LENSES, ReportLens, report_lenses_for
from domain.measurement.planning import GRAIN_COLUMNS
from domain.narrative.shaped_response import UnitKind
from domain.semantics.analytical_resolution import (
    AuthorizedAnalyticalScope,
    EntityAlias,
    EntityContext,
    EntityKind,
    EntityReference,
    FieldOrigin,
    MappingEvidence,
    ResolutionOutcome,
    ResolveEntityRequest,
    normalize_exact_text,
    resolve_entity,
)
from domain.semantics.analytical_resolution import (
    ResolutionCandidate as EntityResolutionCandidate,
)
from domain.semantics.analytical_resolution import (
    ResolutionRecord as EntityResolutionRecord,
)
from domain.semantics.identity import (
    CanonicalKey,
    ChannelKey,
    MarketplaceKey,
    PlatformKey,
    SourceDatasetKey,
    SourceSystemKey,
)
from domain.shared_kernel.values import DateRange

__all__ = [
    "AnalyticalCurrencyMode",
    "AnalyticalCurrencyPolicy",
    "AnalyticalCurrencyRequest",
    "AnalyticalGrain",
    "AnalyticalOperation",
    "AnalyticalOperationKind",
    "AnalyticalResolutionResult",
    "AnalyticalSource",
    "CandidateAnalyticalRequest",
    "DeclarationEvidence",
    "EntityIntent",
    "EntitySelection",
    "FieldResolution",
    "PeriodIntent",
    "ReportLensReference",
    "ResolutionCandidate",
    "ResolvedAnalyticalRequest",
    "SelectorReference",
    "SourceSelection",
    "SourceSelector",
    "resolve_analytical_request",
]

_MIN_AMBIGUOUS_CANDIDATES = 2
_ISO_CURRENCY_CODE_LENGTH = 3


@dataclass(frozen=True, slots=True)
class DeclarationEvidence:
    """Stable governed declaration behind a non-entity resolution decision."""

    authority: str
    reference: str

    def __post_init__(self) -> None:
        if not self.authority.strip() or not self.reference.strip():
            raise ValueError("declaration evidence requires an authority and reference.")


type ResolutionEvidence = DeclarationEvidence | MappingEvidence


@dataclass(frozen=True, slots=True)
class ResolutionCandidate[Value]:
    """Field-neutral typed candidate with provenance and bounded differentiators."""

    value: Value
    label: str
    evidence: tuple[ResolutionEvidence, ...]
    differentiators: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.label.strip() or not self.evidence:
            raise ValueError("a resolution candidate requires a label and evidence.")


@dataclass(frozen=True, slots=True)
class FieldResolution[Value]:
    """One field's typed outcome, origin, candidates, value, and provenance."""

    field: str
    origin: FieldOrigin
    outcome: ResolutionOutcome
    value: Value | None = None
    candidates: tuple[ResolutionCandidate[Value], ...] = ()
    evidence: tuple[ResolutionEvidence, ...] = ()

    def __post_init__(self) -> None:
        if not self.field.strip():
            raise ValueError("a field resolution requires a field name.")
        if self.outcome is ResolutionOutcome.RESOLVED:
            if self.value is None or not self.candidates or not self.evidence:
                raise ValueError("a resolved field requires value, candidates, and evidence.")
        elif self.value is not None or self.evidence:
            raise ValueError("only a resolved field may carry value or resolved evidence.")
        if (
            self.outcome is ResolutionOutcome.AMBIGUOUS
            and len(self.candidates) < _MIN_AMBIGUOUS_CANDIDATES
        ):
            raise ValueError("an ambiguous field requires at least two candidates.")
        if (
            self.outcome
            in {
                ResolutionOutcome.NOT_FOUND,
                ResolutionOutcome.UNAUTHORIZED_OR_HIDDEN,
            }
            and self.candidates
        ):
            raise ValueError("not-found and hidden fields cannot disclose candidates.")


class AnalyticalOperationKind(StrEnum):
    """Operations the existing measurement tools can execute without inference."""

    BREAKDOWN = "breakdown"
    TREND = "trend"
    COMPARISON = "comparison"
    TOP_VALUE = "top_value"
    TOP_INCREASE = "top_increase"


@dataclass(frozen=True, slots=True)
class AnalyticalOperation:
    """One supported operation and its governed rank size where applicable."""

    kind: AnalyticalOperationKind
    n: int | None = None

    def __post_init__(self) -> None:
        ranked = self.kind in {
            AnalyticalOperationKind.TOP_VALUE,
            AnalyticalOperationKind.TOP_INCREASE,
        }
        if ranked != (self.n is not None):
            raise ValueError("only a ranking operation carries n.")
        if self.n is not None and self.n < 1:
            raise ValueError("a ranking operation requires n of at least one.")


@dataclass(frozen=True, slots=True)
class AnalyticalGrain:
    """A typed public grain paired with its canonical fact-column name."""

    name: str
    column: str

    def __post_init__(self) -> None:
        if GRAIN_COLUMNS.get(self.name) != self.column:
            raise ValueError(f"unknown analytical grain {self.name}={self.column}.")

    @classmethod
    def named(cls, name: str) -> AnalyticalGrain:
        """Construct a typed grain from the one canonical grain vocabulary."""
        column = GRAIN_COLUMNS.get(name)
        if column is None:
            raise ValueError(f"unknown analytical grain {name}.")
        return cls(name=name, column=column)


@dataclass(frozen=True, slots=True)
class AnalyticalSource:
    """One typed source system and dataset declared by a metric."""

    system: SourceSystemKey
    dataset: SourceDatasetKey


@dataclass(frozen=True, slots=True)
class SourceSelector:
    """Candidate source selector proposed by the intent layer."""

    system: SourceSystemKey
    dataset: SourceDatasetKey


@dataclass(frozen=True, slots=True)
class SourceSelection:
    """All metric-required sources and an optional explicit selector among them."""

    required: tuple[AnalyticalSource, ...]
    selector: AnalyticalSource | None = None

    def __post_init__(self) -> None:
        if not self.required or len(set(self.required)) != len(self.required):
            raise ValueError("a source selection requires unique metric sources.")
        if self.selector is not None and self.selector not in self.required:
            raise ValueError("an explicit source selector must be among required sources.")


class AnalyticalCurrencyMode(StrEnum):
    """Whether figures remain source-native or request governed conversion."""

    NATIVE = "native"
    TARGET = "target"


@dataclass(frozen=True, slots=True)
class AnalyticalCurrencyRequest:
    """Candidate currency intent; target conversion is not implemented yet."""

    mode: AnalyticalCurrencyMode
    target_currency: str | None = None

    def __post_init__(self) -> None:
        if self.mode is AnalyticalCurrencyMode.TARGET:
            if (
                self.target_currency is None
                or len(self.target_currency.strip()) != _ISO_CURRENCY_CODE_LENGTH
            ):
                raise ValueError("target currency requires a three-letter currency code.")
        elif self.target_currency is not None:
            raise ValueError("native currency must not carry a target code.")


@dataclass(frozen=True, slots=True)
class AnalyticalCurrencyPolicy:
    """Resolved native or target-currency behavior."""

    mode: AnalyticalCurrencyMode
    target_currency: str | None = None

    def __post_init__(self) -> None:
        if self.mode is AnalyticalCurrencyMode.TARGET:
            if (
                self.target_currency is None
                or len(self.target_currency) != _ISO_CURRENCY_CODE_LENGTH
            ):
                raise ValueError("target currency policy requires an ISO-like code.")
        elif self.target_currency is not None:
            raise ValueError("native currency policy carries no target code.")


@dataclass(frozen=True, slots=True)
class EntityIntent:
    """One explicit entity kind and unresolved customer reference."""

    kind: EntityKind
    reference: EntityReference


@dataclass(frozen=True, slots=True)
class EntitySelection:
    """One canonical entity paired with the kind that gives the key meaning."""

    kind: EntityKind
    key: CanonicalKey


@dataclass(frozen=True, slots=True)
class PeriodIntent:
    """Exactly one explicit date window or named reference period."""

    reference: str | None = None
    window: DateRange | None = None

    def __post_init__(self) -> None:
        if bool(self.reference and self.reference.strip()) == (self.window is not None):
            raise ValueError("a period intent requires exactly one reference or window.")


@dataclass(frozen=True, slots=True)
class ReportLensReference:
    """Untrusted candidate name and value resolved through the governed lens vocabulary."""

    name: str
    value: str

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.value.strip():
            raise ValueError("a report-lens reference requires a name and value.")


@dataclass(frozen=True, slots=True)
class SelectorReference:
    """Untrusted exact selector resolved against the selected metric's closed vocabulary."""

    name: str
    value: str

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.value.strip():
            raise ValueError("a selector reference requires a name and value.")


@dataclass(frozen=True, slots=True)
class CandidateAnalyticalRequest:
    """Candidate intent and authoritative inputs for deterministic resolution."""

    catalog: Catalog
    today: date
    scope: AuthorizedAnalyticalScope
    authorized_aliases: tuple[EntityAlias, ...]
    metric_reference: str
    operation_reference: str
    current_period: PeriodIntent | None = None
    default_current_period: DateRange | None = None
    """Latest covered window, adapter-computed from CURRENT coverage; used only when the
    question named no current period, and always recorded as a governed default."""

    prior_period: PeriodIntent | None = None
    grains: tuple[str, ...] = ()
    entities: tuple[EntityIntent, ...] = ()
    source: SourceSelector | None = None
    report_lenses: tuple[ReportLensReference, ...] = ()
    selectors: tuple[SelectorReference, ...] = ()
    currency: AnalyticalCurrencyRequest | None = None
    rank_n: int | None = None


@dataclass(frozen=True, slots=True)
class ResolvedAnalyticalRequest:
    """Canonical request whose every customer-meaning field carries an origin."""

    catalog: Catalog
    scope: FieldResolution[AuthorizedAnalyticalScope]
    metric: FieldResolution[Metric]
    current_period: FieldResolution[DateRange]
    operation: FieldResolution[AnalyticalOperation]
    grains: FieldResolution[tuple[AnalyticalGrain, ...]]
    sources: FieldResolution[SourceSelection]
    currency: FieldResolution[AnalyticalCurrencyPolicy]
    prior_period: FieldResolution[DateRange] | None = None
    entities: tuple[FieldResolution[EntitySelection], ...] = ()
    report_lenses: tuple[FieldResolution[ReportLens], ...] = ()
    selectors: tuple[FieldResolution[SelectorReference], ...] = ()

    def __post_init__(self) -> None:
        records = [
            _erase(self.scope),
            _erase(self.metric),
            _erase(self.current_period),
            _erase(self.operation),
            _erase(self.grains),
            _erase(self.sources),
            _erase(self.currency),
            *(_erase(record) for record in self.entities),
            *(_erase(record) for record in self.report_lenses),
            *(_erase(record) for record in self.selectors),
        ]
        if self.prior_period is not None:
            records.append(_erase(self.prior_period))
        if any(record.outcome is not ResolutionOutcome.RESOLVED for record in records):
            raise ValueError("a resolved analytical request may contain only resolved fields.")
        entity_kinds = tuple(cast("EntitySelection", record.value).kind for record in self.entities)
        if len(set(entity_kinds)) != len(entity_kinds):
            raise ValueError("a resolved analytical request repeats an entity kind.")


@dataclass(frozen=True, slots=True)
class AnalyticalResolutionResult:
    """All-or-nothing result; only RESOLVED carries a canonical request."""

    outcome: ResolutionOutcome
    request: ResolvedAnalyticalRequest | None = None
    records: tuple[FieldResolution[object], ...] = ()

    def __post_init__(self) -> None:
        if (self.request is not None) != (self.outcome is ResolutionOutcome.RESOLVED):
            raise ValueError("only a resolved analytical result may carry a request.")
        if self.outcome is not ResolutionOutcome.RESOLVED and not self.records:
            raise ValueError("an unresolved analytical result requires resolution records.")

    def field(self, name: str) -> FieldResolution[object] | None:
        """Return the latest resolution record for one field name."""
        return next((record for record in reversed(self.records) if record.field == name), None)


@dataclass(frozen=True, slots=True)
class _ResolvedFieldMetadata:
    field: str
    origin: FieldOrigin
    evidence: ResolutionEvidence
    label: str


@dataclass(frozen=True, slots=True)
class _PeriodResolutionRequest:
    field: str
    intent: PeriodIntent
    today: date
    origin: FieldOrigin


@dataclass(frozen=True, slots=True)
class _ClosedWindowRequest:
    field: str
    window: DateRange
    today: date
    origin: FieldOrigin
    label: str


@dataclass(frozen=True, slots=True)
class _CurrencyResolutionRequest:
    requested: AnalyticalCurrencyRequest | None
    metric: Metric
    grains: tuple[AnalyticalGrain, ...]
    entities: tuple[FieldResolution[EntitySelection], ...]


@dataclass(frozen=True, slots=True)
class _MetricCompatibilityRequest:
    catalog: Catalog
    metric: FieldResolution[Metric]
    grains: FieldResolution[tuple[AnalyticalGrain, ...]]
    lenses: tuple[FieldResolution[ReportLens], ...]
    selectors: tuple[FieldResolution[SelectorReference], ...]


class _ResolutionBlockedError(Exception):
    """Internal short circuit carrying the typed records already resolved."""

    outcome: ResolutionOutcome
    records: tuple[FieldResolution[object], ...]

    def __init__(
        self,
        outcome: ResolutionOutcome,
        records: tuple[FieldResolution[object], ...],
    ) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome
        self.records = records


def _resolved_value[Value](
    records: list[FieldResolution[object]],
    record: FieldResolution[Value],
) -> Value:
    records.append(_erase(record))
    if record.outcome is not ResolutionOutcome.RESOLVED or record.value is None:
        raise _ResolutionBlockedError(record.outcome, tuple(records))
    return record.value


def _stop_if_blocked[Value](
    records: list[FieldResolution[object]],
    blocker: FieldResolution[Value] | None,
) -> None:
    if blocker is None:
        return
    records.append(_erase(blocker))
    raise _ResolutionBlockedError(blocker.outcome, tuple(records))


def resolve_analytical_request(
    candidate: CandidateAnalyticalRequest,
) -> AnalyticalResolutionResult:
    """Resolve every field or return the first typed blocker without a request."""
    records: list[FieldResolution[object]] = []
    try:
        scope = _resolved(
            candidate.scope,
            _ResolvedFieldMetadata(
                "scope",
                FieldOrigin.DETERMINISTIC_RESOLVER,
                DeclarationEvidence("authorized_analytical_scope", "current invocation"),
                "authorized analytical scope",
            ),
        )
        _resolved_value(records, scope)

        metric = _resolve_metric(candidate)
        selected_metric = _resolved_value(records, metric)
        current = _resolve_current_period(candidate)
        current_window = _resolved_value(records, current)
        operation = _resolve_operation(candidate)
        selected_operation = _resolved_value(records, operation)

        prior = _resolve_prior_period(candidate, selected_operation, current_window)
        if prior is not None:
            _resolved_value(records, prior)

        grains = _resolve_grains(candidate.grains, selected_metric, selected_operation)
        selected_grains = _resolved_value(records, grains)

        entity_result = _resolve_entities(candidate, selected_metric, current_window)
        records.extend(_erase(record) for record in entity_result.records)
        _stop_if_blocked(records, entity_result.blocker)

        sources = _resolve_sources(candidate.source, candidate.catalog, selected_metric)
        selected_sources = _resolved_value(records, sources)
        lenses = _resolve_report_lenses(candidate.report_lenses, selected_sources)
        records.extend(_erase(record) for record in lenses.records)
        _stop_if_blocked(records, lenses.blocker)
        selectors = _resolve_selectors(candidate.selectors, selected_metric)
        records.extend(_erase(record) for record in selectors.records)
        _stop_if_blocked(records, selectors.blocker)

        blocker = _metric_compatibility_blocker(
            _MetricCompatibilityRequest(
                candidate.catalog,
                metric,
                grains,
                lenses.records,
                selectors.records,
            )
        )
        _stop_if_blocked(records, blocker)

        currency = _resolve_currency(
            _CurrencyResolutionRequest(
                candidate.currency,
                selected_metric,
                selected_grains,
                entity_result.records,
            )
        )
        _resolved_value(records, currency)

        resolved = ResolvedAnalyticalRequest(
            catalog=candidate.catalog,
            scope=scope,
            metric=metric,
            current_period=current,
            prior_period=prior,
            operation=operation,
            grains=grains,
            entities=entity_result.records,
            sources=sources,
            report_lenses=lenses.records,
            selectors=selectors.records,
            currency=currency,
        )
        return AnalyticalResolutionResult(
            outcome=ResolutionOutcome.RESOLVED,
            request=resolved,
            records=tuple(records),
        )
    except _ResolutionBlockedError as blocked:
        return AnalyticalResolutionResult(
            outcome=blocked.outcome,
            records=blocked.records,
        )


def _metric_compatibility_blocker(
    request: _MetricCompatibilityRequest,
) -> FieldResolution[object] | None:
    selected_metric = cast("Metric", request.metric.value)
    selected_grains = cast("tuple[AnalyticalGrain, ...]", request.grains.value)
    lens_filters = {
        cast("ReportLens", record.value).name: cast("ReportLens", record.value).value
        for record in request.lenses
    }
    selector_filters = {
        cast("SelectorReference", record.value).name: cast("SelectorReference", record.value).value
        for record in request.selectors
    }
    filters = lens_filters | selector_filters
    refusal = request.catalog.refusal_for(selected_metric.name, filters=filters)
    if refusal is not None:
        outcome = (
            ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE
            if refusal.reason is RefusalReason.REQUIRES_SOURCE_OPTION
            else ResolutionOutcome.UNSUPPORTED_CAPABILITY
        )
        return _erase(
            FieldResolution[Metric](
                field="metric",
                origin=request.metric.origin,
                outcome=outcome,
                candidates=request.metric.candidates,
            )
        )
    unsupported = next(
        (
            grain
            for grain in selected_grains
            if request.catalog.refusal_for(
                selected_metric.name,
                grain=grain.name,
                filters=filters,
            )
            is not None
        ),
        None,
    )
    if unsupported is None:
        return None
    return _erase(
        FieldResolution[tuple[AnalyticalGrain, ...]](
            field="grains",
            origin=request.grains.origin,
            outcome=ResolutionOutcome.UNSUPPORTED_CAPABILITY,
            candidates=request.grains.candidates,
        )
    )


def _resolve_metric(candidate: CandidateAnalyticalRequest) -> FieldResolution[Metric]:
    normalized = normalize_exact_text(candidate.metric_reference)
    if normalized == "roas":
        metric = candidate.catalog.default_roas()
        return _resolved(
            metric,
            _ResolvedFieldMetadata(
                "metric",
                FieldOrigin.GOVERNED_DEFAULT,
                DeclarationEvidence("metric_dictionary", metric.name),
                metric.label,
            ),
        )
    candidates = candidate.catalog.metric_candidates(candidate.metric_reference)
    if not candidates:
        refusal = candidate.catalog.refusal_for(candidate.metric_reference)
        outcome = (
            ResolutionOutcome.UNSUPPORTED_CAPABILITY
            if refusal is not None and refusal.reason is RefusalReason.NOT_YET_COMPUTABLE
            else ResolutionOutcome.NOT_FOUND
        )
        return FieldResolution("metric", FieldOrigin.EXPLICIT_CUSTOMER_TEXT, outcome)
    resolved_candidates = tuple(_metric_candidate(metric) for metric in candidates)
    if len(candidates) > 1:
        return FieldResolution(
            "metric",
            FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
            ResolutionOutcome.AMBIGUOUS,
            candidates=resolved_candidates,
        )
    metric = next(iter(candidates))
    evidence = DeclarationEvidence("metric_dictionary", metric.name)
    return FieldResolution(
        "metric",
        FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
        ResolutionOutcome.RESOLVED,
        value=metric,
        candidates=resolved_candidates,
        evidence=(evidence,),
    )


def _resolve_current_period(
    candidate: CandidateAnalyticalRequest,
) -> FieldResolution[DateRange]:
    if candidate.current_period is None:
        if candidate.default_current_period is not None:
            # The adapter derived this window from published coverage. Recording it as a
            # governed default is what forces the released answer to disclose the choice:
            # the origin flows into resolution provenance, and the envelope turns that
            # provenance into a mandatory rendering (ADR-062 transparency).
            window = candidate.default_current_period
            evidence = DeclarationEvidence("coverage_default", str(window))
            return FieldResolution(
                "current_period",
                FieldOrigin.GOVERNED_DEFAULT,
                ResolutionOutcome.RESOLVED,
                value=window,
                candidates=(ResolutionCandidate(window, "latest covered window", (evidence,)),),
                evidence=(evidence,),
            )
        return FieldResolution(
            "current_period",
            FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
            ResolutionOutcome.NOT_FOUND,
        )
    return _resolve_period(
        _PeriodResolutionRequest(
            "current_period",
            candidate.current_period,
            candidate.today,
            FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
        )
    )


def _resolve_prior_period(
    candidate: CandidateAnalyticalRequest,
    operation: AnalyticalOperation,
    current: DateRange,
) -> FieldResolution[DateRange] | None:
    needs_prior = operation.kind in {
        AnalyticalOperationKind.COMPARISON,
        AnalyticalOperationKind.TOP_INCREASE,
    }
    if not needs_prior and candidate.prior_period is None:
        return None
    if not needs_prior:
        return FieldResolution(
            "prior_period",
            FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
            ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE,
        )
    if candidate.prior_period is None:
        return FieldResolution(
            "prior_period",
            FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
            ResolutionOutcome.NOT_FOUND,
        )
    record = _resolve_period(
        _PeriodResolutionRequest(
            "prior_period",
            candidate.prior_period,
            candidate.today,
            FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
        )
    )
    if (
        record.outcome is ResolutionOutcome.RESOLVED
        and cast("DateRange", record.value).end >= current.start
    ):
        return FieldResolution(
            "prior_period",
            record.origin,
            ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE,
            candidates=record.candidates,
        )
    return record


def _resolve_period(request: _PeriodResolutionRequest) -> FieldResolution[DateRange]:
    intent = request.intent
    if intent.window is not None:
        return _closed_window_record(
            _ClosedWindowRequest(
                request.field,
                intent.window,
                request.today,
                request.origin,
                "explicit date range",
            )
        )
    reference = cast("str", intent.reference)
    if normalize_exact_text(reference) == "last week":
        named = reference_periods(request.today)
        periods = tuple(
            period
            for period in named
            if period.label
            in {
                "the 7 days ending yesterday",
                "last week (calendar, Monday to Sunday)",
            }
        )
    else:
        periods = reference_period_candidates(request.today, reference)
    candidates = tuple(_period_candidate(period) for period in periods)
    if not periods:
        return FieldResolution(request.field, request.origin, ResolutionOutcome.NOT_FOUND)
    if len(periods) > 1:
        return FieldResolution(
            request.field,
            request.origin,
            ResolutionOutcome.AMBIGUOUS,
            candidates=candidates,
        )
    period = next(iter(periods))
    evidence = DeclarationEvidence("reference_periods", period.label)
    return FieldResolution(
        request.field,
        request.origin,
        ResolutionOutcome.RESOLVED,
        value=period.window,
        candidates=candidates,
        evidence=(evidence,),
    )


def _closed_window_record(request: _ClosedWindowRequest) -> FieldResolution[DateRange]:
    evidence = DeclarationEvidence("closed_business_day", request.label)
    candidate = ResolutionCandidate(request.window, request.label, (evidence,))
    if request.window.end > request.today.fromordinal(request.today.toordinal() - 1):
        return FieldResolution(
            request.field,
            request.origin,
            ResolutionOutcome.INVALID_CONTEXT,
            candidates=(candidate,),
        )
    return FieldResolution(
        request.field,
        request.origin,
        ResolutionOutcome.RESOLVED,
        value=request.window,
        candidates=(candidate,),
        evidence=(evidence,),
    )


def _resolve_operation(
    candidate: CandidateAnalyticalRequest,
) -> FieldResolution[AnalyticalOperation]:
    normalized = normalize_exact_text(candidate.operation_reference)
    supported = {
        "breakdown": AnalyticalOperationKind.BREAKDOWN,
        "trend": AnalyticalOperationKind.TREND,
        "comparison": AnalyticalOperationKind.COMPARISON,
        "compare": AnalyticalOperationKind.COMPARISON,
        "top value": AnalyticalOperationKind.TOP_VALUE,
        "top increase": AnalyticalOperationKind.TOP_INCREASE,
    }
    unsupported = {
        "bottom",
        "bottom value",
        "bottom n",
        "decline",
        "top decline",
        "causal",
        "causality",
        "reconciliation",
        "reconcile",
    }
    if normalized in unsupported:
        return FieldResolution(
            "operation",
            FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
            ResolutionOutcome.UNSUPPORTED_CAPABILITY,
        )
    kind = supported.get(normalized)
    if kind is None:
        return FieldResolution(
            "operation",
            FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
            ResolutionOutcome.NOT_FOUND,
        )
    ranked = kind in {AnalyticalOperationKind.TOP_VALUE, AnalyticalOperationKind.TOP_INCREASE}
    if ranked and (candidate.rank_n is None or candidate.rank_n < 1):
        return FieldResolution(
            "operation",
            FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
            ResolutionOutcome.INVALID_CONTEXT,
        )
    if not ranked and candidate.rank_n is not None:
        return FieldResolution(
            "operation",
            FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
            ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE,
        )
    operation = AnalyticalOperation(kind, candidate.rank_n if ranked else None)
    evidence = DeclarationEvidence("measurement_operations", kind.value)
    return _resolved(
        operation,
        _ResolvedFieldMetadata(
            "operation",
            FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
            evidence,
            kind.value,
        ),
    )


def _resolve_grains(
    names: tuple[str, ...],
    metric: Metric,
    operation: AnalyticalOperation,
) -> FieldResolution[tuple[AnalyticalGrain, ...]]:
    evidence = DeclarationEvidence("grain_columns", "canonical grain vocabulary")
    if not names or len(set(names)) != len(names):
        return FieldResolution(
            "grains",
            FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
            ResolutionOutcome.INVALID_CONTEXT,
        )
    if any(name not in GRAIN_COLUMNS for name in names):
        return FieldResolution(
            "grains",
            FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
            ResolutionOutcome.NOT_FOUND,
        )
    grains = tuple(AnalyticalGrain.named(name) for name in names)
    candidates = (
        ResolutionCandidate(
            grains,
            ", ".join(grain.name for grain in grains),
            (evidence,),
        ),
    )
    if any(not metric.supports(grain.name) for grain in grains):
        return FieldResolution(
            "grains",
            FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
            ResolutionOutcome.UNSUPPORTED_CAPABILITY,
            candidates=candidates,
        )
    if operation.kind is AnalyticalOperationKind.TREND and "date" not in names:
        return FieldResolution(
            "grains",
            FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
            ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE,
            candidates=candidates,
        )
    return FieldResolution(
        "grains",
        FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
        ResolutionOutcome.RESOLVED,
        value=grains,
        candidates=candidates,
        evidence=(evidence,),
    )


@dataclass(frozen=True, slots=True)
class _EntityResolutionSet:
    records: tuple[FieldResolution[EntitySelection], ...]
    blocker: FieldResolution[EntitySelection] | None = None


def _resolve_entities(
    candidate: CandidateAnalyticalRequest,
    metric: Metric,
    window: DateRange,
) -> _EntityResolutionSet:
    explicit = {intent.kind: intent for intent in candidate.entities}
    if len(explicit) != len(candidate.entities):
        blocker = FieldResolution[EntitySelection](
            "entities",
            FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
            ResolutionOutcome.INVALID_CONTEXT,
        )
        return _EntityResolutionSet((), blocker)
    unsupported = explicit.keys() & {EntityKind.BRAND, EntityKind.ADVERTISER}
    if unsupported:
        kind = sorted(unsupported, key=lambda item: item.value)[0]
        blocker = FieldResolution[EntitySelection](
            kind.value,
            FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
            ResolutionOutcome.UNSUPPORTED_CAPABILITY,
        )
        return _EntityResolutionSet((), blocker)

    records: dict[EntityKind, FieldResolution[EntitySelection]] = {}
    order = (
        EntityKind.MARKETPLACE,
        EntityKind.PLATFORM,
        EntityKind.CHANNEL,
        EntityKind.ECOSYSTEM,
        EntityKind.CHANNEL_GROUP,
        EntityKind.AD_PRODUCT,
        EntityKind.PRODUCT,
        EntityKind.CAMPAIGN,
    )
    for kind in order:
        intent = explicit.get(kind)
        if intent is None:
            continue
        if kind.value not in metric.valid_grains:
            blocker = FieldResolution[EntitySelection](
                kind.value,
                FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
                ResolutionOutcome.UNSUPPORTED_CAPABILITY,
            )
            return _EntityResolutionSet(tuple(records.values()), blocker)
        context = EntityContext(
            platform=cast("PlatformKey | None", _selected_key(EntityKind.PLATFORM, records)),
            channel=cast("ChannelKey | None", _selected_key(EntityKind.CHANNEL, records)),
            marketplace=cast(
                "MarketplaceKey | None", _selected_key(EntityKind.MARKETPLACE, records)
            ),
        )
        entity = resolve_entity(
            ResolveEntityRequest(
                scope=candidate.scope,
                reference=intent.reference,
                origin=FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
                aliases=candidate.authorized_aliases,
                kinds=frozenset({kind}),
                window=window,
                context=context,
            )
        )
        record = _convert_entity_record(kind, entity)
        if record.outcome is not ResolutionOutcome.RESOLVED:
            return _EntityResolutionSet(tuple(records.values()), record)
        records[kind] = record

    compatibility = _entity_compatibility(tuple(records.values()))
    return _EntityResolutionSet(tuple(records.values()), compatibility)


def _selected_key(
    kind: EntityKind,
    records: dict[EntityKind, FieldResolution[EntitySelection]],
) -> CanonicalKey | None:
    record = records.get(kind)
    if record is None or record.value is None:
        return None
    return record.value.key


def _convert_entity_record(
    kind: EntityKind,
    record: EntityResolutionRecord[CanonicalKey],
) -> FieldResolution[EntitySelection]:
    candidates = tuple(
        ResolutionCandidate(
            value=EntitySelection(kind, candidate.key),
            label=_entity_label(candidate),
            evidence=candidate.evidence,
            differentiators=_entity_differentiators(candidate),
        )
        for candidate in record.candidates
    )
    if record.outcome is not ResolutionOutcome.RESOLVED:
        return FieldResolution(
            kind.value,
            record.origin,
            record.outcome,
            candidates=candidates,
        )
    selection = EntitySelection(kind, cast("CanonicalKey", record.value))
    return FieldResolution(
        kind.value,
        record.origin,
        ResolutionOutcome.RESOLVED,
        value=selection,
        candidates=candidates,
        evidence=record.evidence,
    )


def _entity_compatibility(
    records: tuple[FieldResolution[EntitySelection], ...],
) -> FieldResolution[EntitySelection] | None:
    by_kind = {cast("EntitySelection", record.value).kind: record for record in records}
    campaign = by_kind.get(EntityKind.CAMPAIGN)
    if campaign is None:
        return None
    selected = campaign.candidates[0]
    expected = dict(selected.differentiators)
    for kind in (EntityKind.PLATFORM, EntityKind.CHANNEL, EntityKind.MARKETPLACE):
        record = by_kind.get(kind)
        if record is None:
            continue
        actual = cast("EntitySelection", record.value).key.value
        if expected.get(kind.value) not in {None, actual}:
            return FieldResolution(
                "entity_context",
                FieldOrigin.DETERMINISTIC_RESOLVER,
                ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE,
                candidates=campaign.candidates,
            )
    return None


def _resolve_sources(
    selector: SourceSelector | None,
    catalog: Catalog,
    metric: Metric,
) -> FieldResolution[SourceSelection]:
    required = tuple(
        AnalyticalSource(SourceSystemKey(system), SourceDatasetKey(dataset))
        for system, dataset in catalog.sources_for(metric)
    )
    evidence = tuple(
        DeclarationEvidence("metric_dictionary_source", f"{item.system.value}:{item.dataset.value}")
        for item in required
    )
    if not required:
        return FieldResolution[SourceSelection](
            "sources",
            FieldOrigin.DETERMINISTIC_RESOLVER,
            ResolutionOutcome.UNSUPPORTED_CAPABILITY,
        )
    selected = (
        None
        if selector is None
        else AnalyticalSource(system=selector.system, dataset=selector.dataset)
    )
    origin = (
        FieldOrigin.DETERMINISTIC_RESOLVER
        if selected is None
        else FieldOrigin.EXPLICIT_CUSTOMER_TEXT
    )
    possible = tuple(
        ResolutionCandidate(
            SourceSelection(required=required, selector=item),
            f"{item.system.value}:{item.dataset.value}",
            (item_evidence,),
        )
        for item, item_evidence in zip(required, evidence, strict=True)
    )
    if selected is not None and selected not in required:
        return FieldResolution(
            "sources",
            origin,
            ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE,
            candidates=possible,
        )
    if selected is not None:
        return FieldResolution(
            "sources",
            origin,
            ResolutionOutcome.UNSUPPORTED_CAPABILITY,
            candidates=possible,
        )
    value = SourceSelection(required=required)
    label = ", ".join(f"{item.system.value}:{item.dataset.value}" for item in required)
    candidate = ResolutionCandidate(value, label, evidence)
    return FieldResolution(
        "sources",
        origin,
        ResolutionOutcome.RESOLVED,
        value=value,
        candidates=(candidate,),
        evidence=evidence,
    )


@dataclass(frozen=True, slots=True)
class _LensResolutionSet:
    records: tuple[FieldResolution[ReportLens], ...]
    blocker: FieldResolution[ReportLens] | None = None


def _resolve_report_lenses(
    references: tuple[ReportLensReference, ...],
    sources: SourceSelection,
) -> _LensResolutionSet:
    supplied = {reference.name: reference for reference in references}
    if len(supplied) != len(references):
        blocker = FieldResolution[ReportLens](
            "report_lenses",
            FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
            ResolutionOutcome.INVALID_CONTEXT,
        )
        return _LensResolutionSet((), blocker)
    unknown = tuple(name for name in supplied if name not in REPORT_LENSES)
    if unknown:
        blocker = FieldResolution[ReportLens](
            unknown[0],
            FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
            ResolutionOutcome.NOT_FOUND,
        )
        return _LensResolutionSet((), blocker)
    source_pairs = tuple((source.system.value, source.dataset.value) for source in sources.required)
    applicable = report_lenses_for(source_pairs)
    applicable_names = {definition.name for definition in applicable}
    inapplicable = tuple(name for name in supplied if name not in applicable_names)
    if inapplicable:
        name = inapplicable[0]
        definition = REPORT_LENSES[name]
        candidates = tuple(
            ResolutionCandidate(
                ReportLens(name, value),
                f"{name}={value}",
                (DeclarationEvidence("report_lenses", f"{name}={value}"),),
            )
            for value in definition.values
        )
        blocker = FieldResolution(
            name,
            FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
            ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE,
            candidates=candidates,
        )
        return _LensResolutionSet((), blocker)

    records: list[FieldResolution[ReportLens]] = []
    for definition in applicable:
        reference = supplied.get(definition.name)
        value = definition.default if reference is None else reference.value
        origin = (
            FieldOrigin.GOVERNED_DEFAULT
            if reference is None
            else FieldOrigin.EXPLICIT_CUSTOMER_TEXT
        )
        if value not in definition.values:
            blocker = FieldResolution[ReportLens](
                definition.name,
                origin,
                ResolutionOutcome.NOT_FOUND,
            )
            return _LensResolutionSet(tuple(records), blocker)
        lens = ReportLens(definition.name, value)
        evidence = DeclarationEvidence("report_lenses", f"{definition.name}={value}")
        records.append(
            _resolved(
                lens,
                _ResolvedFieldMetadata(
                    definition.name,
                    origin,
                    evidence,
                    f"{definition.name}={value}",
                ),
            )
        )
    return _LensResolutionSet(tuple(records))


@dataclass(frozen=True, slots=True)
class _SelectorResolutionSet:
    records: tuple[FieldResolution[SelectorReference], ...]
    blocker: FieldResolution[SelectorReference] | None = None


def _resolve_selectors(
    references: tuple[SelectorReference, ...], metric: Metric
) -> _SelectorResolutionSet:
    supplied = {reference.name: reference for reference in references}
    if len(supplied) != len(references):
        return _SelectorResolutionSet(
            (),
            FieldResolution(
                "selectors",
                FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
                ResolutionOutcome.INVALID_CONTEXT,
            ),
        )

    required = dict(metric.required_selectors)
    # A selector the metric is defined at one exact value of (`requires: cohort_path: standard`):
    # the metric names its population, so there is nothing to choose. Two kinds of `requires` key
    # stay out. A report lens is the lens resolution's (ADR-027). A key that is also one of the
    # metric's grains (`identity_space`, beside `identity_space=account`) isn't pinned: a pin
    # there would filter the column a breakdown asks for, so it still refuses, naming the value.
    exact = tuple(
        (key, value)
        for key, value in metric.requires
        if key not in REPORT_LENSES and key not in metric.valid_grains
    )
    exact_names = {key for key, _value in exact}
    irrelevant = tuple(
        name for name in supplied if name not in required and name not in exact_names
    )
    if irrelevant:
        return _SelectorResolutionSet(
            (),
            FieldResolution(
                irrelevant[0],
                FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
                ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE,
            ),
        )

    records: list[FieldResolution[SelectorReference]] = []
    for name, value in exact:
        evidence = DeclarationEvidence("metric_required_value", f"{name}={value}")
        reference = supplied.get(name)
        if reference is not None and reference.value != value:
            return _SelectorResolutionSet(
                tuple(records),
                FieldResolution(
                    name,
                    FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
                    ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE,
                    candidates=(
                        ResolutionCandidate(
                            SelectorReference(name, value), f"{name}={value}", (evidence,)
                        ),
                    ),
                ),
            )
        # The metric is defined at this one value, so there is nothing to choose: pin it from
        # the metric's own declaration, as a single allowed value is pinned below.
        origin = (
            FieldOrigin.GOVERNED_DEFAULT
            if reference is None
            else FieldOrigin.EXPLICIT_CUSTOMER_TEXT
        )
        records.append(
            _resolved(
                SelectorReference(name, value),
                _ResolvedFieldMetadata(name, origin, evidence, f"{name}={value}"),
            )
        )
    for name, values in metric.required_selectors:
        candidates = tuple(
            ResolutionCandidate(
                SelectorReference(name, value),
                f"{name}={value}",
                (DeclarationEvidence("metric_required_selector", f"{name}={value}"),),
            )
            for value in values
        )
        reference = supplied.get(name)
        if reference is None and len(values) == 1:
            # The metric admits exactly one value, so there is nothing to choose and nothing to
            # assume: pin it as the metric's own declaration says, never from the question text.
            (only,) = values
            evidence = DeclarationEvidence("metric_required_selector", f"{name}={only}")
            records.append(
                _resolved(
                    SelectorReference(name, only),
                    _ResolvedFieldMetadata(
                        name, FieldOrigin.GOVERNED_DEFAULT, evidence, f"{name}={only}"
                    ),
                )
            )
            continue
        if reference is None:
            return _SelectorResolutionSet(
                tuple(records),
                FieldResolution(
                    name,
                    FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
                    ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE,
                    candidates=candidates,
                ),
            )
        if reference.value not in values:
            return _SelectorResolutionSet(
                tuple(records),
                FieldResolution(
                    name,
                    FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
                    ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE,
                    candidates=candidates,
                ),
            )
        evidence = DeclarationEvidence("metric_required_selector", f"{name}={reference.value}")
        records.append(
            _resolved(
                reference,
                _ResolvedFieldMetadata(
                    name,
                    FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
                    evidence,
                    f"{name}={reference.value}",
                ),
            )
        )
    return _SelectorResolutionSet(tuple(records))


def _resolve_currency(
    request: _CurrencyResolutionRequest,
) -> FieldResolution[AnalyticalCurrencyPolicy]:
    origin = (
        FieldOrigin.GOVERNED_DEFAULT
        if request.requested is None
        else FieldOrigin.EXPLICIT_CUSTOMER_TEXT
    )
    selected = request.requested or AnalyticalCurrencyRequest(AnalyticalCurrencyMode.NATIVE)
    if selected.mode is AnalyticalCurrencyMode.TARGET:
        return FieldResolution(
            "currency",
            origin,
            ResolutionOutcome.UNSUPPORTED_CAPABILITY,
        )
    has_marketplace = any(
        cast("EntitySelection", record.value).kind is EntityKind.MARKETPLACE
        for record in request.entities
    )
    grouped_marketplace = any(grain.name == "marketplace" for grain in request.grains)
    if request.metric.unit_kind is UnitKind.CURRENCY and not (
        has_marketplace or grouped_marketplace
    ):
        return FieldResolution(
            "currency",
            origin,
            ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE,
        )
    policy = AnalyticalCurrencyPolicy(AnalyticalCurrencyMode.NATIVE)
    evidence = DeclarationEvidence("currency_policy", "native")
    return _resolved(
        policy,
        _ResolvedFieldMetadata("currency", origin, evidence, "native currency"),
    )


def _resolved[Value](
    value: Value,
    metadata: _ResolvedFieldMetadata,
) -> FieldResolution[Value]:
    candidate = ResolutionCandidate(
        value=value,
        label=metadata.label,
        evidence=(metadata.evidence,),
    )
    return FieldResolution(
        field=metadata.field,
        origin=metadata.origin,
        outcome=ResolutionOutcome.RESOLVED,
        value=value,
        candidates=(candidate,),
        evidence=(metadata.evidence,),
    )


def _metric_candidate(metric: Metric) -> ResolutionCandidate[Metric]:
    evidence = DeclarationEvidence("metric_dictionary", metric.name)
    return ResolutionCandidate(
        value=metric,
        label=metric.label,
        evidence=(evidence,),
        differentiators=(
            ("metric", metric.name),
            ("measurement_basis", ",".join(item.value for item in metric.measurement_basis)),
            ("unit", metric.unit_kind.value),
        ),
    )


def _period_candidate(period: ReferencePeriod) -> ResolutionCandidate[DateRange]:
    evidence = DeclarationEvidence("reference_periods", period.label)
    return ResolutionCandidate(
        value=period.window,
        label=period.label,
        evidence=(evidence,),
        differentiators=(
            ("start", period.window.start.isoformat()),
            ("end", period.window.end.isoformat()),
        ),
    )


def _entity_label(candidate: EntityResolutionCandidate) -> str:
    labels = tuple(label.value for label in candidate.labels)
    return " / ".join(labels) if labels else candidate.key.value


def _entity_differentiators(
    candidate: EntityResolutionCandidate,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (name, value.value)
        for name, value in (
            ("platform", candidate.platform),
            ("channel", candidate.channel),
            ("marketplace", candidate.marketplace),
        )
        if value is not None
    )


def _erase[Value](record: FieldResolution[Value]) -> FieldResolution[object]:
    return cast("FieldResolution[object]", record)
