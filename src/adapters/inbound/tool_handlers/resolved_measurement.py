# SPDX-License-Identifier: MIT-0
"""Resolve semantic candidate intent and map only canonical results to measurement."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, cast

from adapters.inbound.tool_handlers import gateway
from adapters.inbound.tool_handlers.measurement import (
    DEFAULT_MAX_ROWS,
    MeasurementTool,
    MeasurementToolRequest,
)
from adapters.inbound.tool_handlers.schemas import contract_for
from adapters.outbound.publication_selection import SELECTION_FAILED
from domain.catalog.capability import Catalog, Metric
from domain.measurement.comparison import RankOrder
from domain.semantics.analytical_request import (
    AnalyticalCurrencyMode,
    AnalyticalCurrencyRequest,
    AnalyticalOperation,
    AnalyticalOperationKind,
    AnalyticalResolutionResult,
    CandidateAnalyticalRequest,
    EntityIntent,
    EntitySelection,
    FieldResolution,
    PeriodIntent,
    ReportLensReference,
    ResolvedAnalyticalRequest,
    SelectorReference,
    SourceSelector,
    resolve_analytical_request,
)
from domain.semantics.analytical_resolution import (
    AuthorizedAnalyticalScope,
    EntityAlias,
    EntityContext,
    EntityKind,
    EntityReference,
    FieldOrigin,
    ResolutionOutcome,
    ResolutionProvenance,
)
from domain.semantics.identity import SourceDatasetKey, SourceSystemKey
from domain.shared_kernel.errors import DomainError, ValidationError
from domain.shared_kernel.values import DateRange
from ports.outbound.entity_catalog import (
    MAX_ENTITY_CANDIDATES,
    AnalyticalEntityCatalogPort,
    EntityCatalogLookupRequest,
)

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from domain.data_contracts.contract import DataContract

__all__ = [
    "MeasurementRequestContext",
    "SemanticMeasurementContext",
    "map_resolved_measurement",
    "request_from_semantic_intent",
    "to_measurement_tool_request",
]

_ENTITY_FILTERS = {
    EntityKind.ECOSYSTEM: "ecosystem_key",
    EntityKind.PLATFORM: "platform_key",
    EntityKind.CHANNEL_GROUP: "channel_group_key",
    EntityKind.CHANNEL: "channel_key",
    EntityKind.CAMPAIGN: "campaign_key",
    EntityKind.AD_PRODUCT: "ad_product_key",
    EntityKind.MARKETPLACE: "marketplace_key",
    EntityKind.PRODUCT: "amazon_asin",
}
_NOT_FOUND_OUTCOMES = {
    ResolutionOutcome.NOT_FOUND,
    ResolutionOutcome.UNAUTHORIZED_OR_HIDDEN,
}
_SAFE_DIFFERENTIATORS = frozenset({"measurement_basis", "unit", "start", "end"})
_MAX_CLARIFICATION_CANDIDATES = 5


@dataclass(frozen=True, slots=True)
class MeasurementRequestContext:
    """Existing adapter-owned values that are not customer semantic intent."""

    contracts: tuple[DataContract, ...]
    computed_at: datetime
    correlation_id: str
    max_rows: int = DEFAULT_MAX_ROWS


@dataclass(frozen=True, slots=True)
class SemanticMeasurementContext:
    """Authoritative runtime inputs used to resolve one untrusted candidate request."""

    catalog: Catalog
    contracts: tuple[DataContract, ...]
    entity_catalog: AnalyticalEntityCatalogPort
    scope: AuthorizedAnalyticalScope
    today: date
    computed_at: datetime
    correlation_id: str
    default_window: Callable[[Metric], DateRange | None] | None = None
    """Newest published window for a metric, consulted only when no period was named."""


def request_from_semantic_intent(
    tool: MeasurementTool,
    arguments: dict[str, Any],
    context: SemanticMeasurementContext,
) -> MeasurementToolRequest:
    """Parse, authorize, resolve, and map candidate intent without a raw fallback."""
    _reject_undeclared_arguments(tool, arguments)
    try:
        candidate = _candidate_request(tool, arguments, context)
    except ValidationError:
        raise
    except (TypeError, ValueError) as error:
        raise ValidationError(
            code=gateway.BAD_INVOCATION,
            description="The semantic measurement intent is malformed.",
            context={"cause": type(error).__name__},
        ) from None
    if candidate.current_period is None:
        candidate = replace(candidate, default_current_period=_coverage_default(candidate, context))

    if candidate.entities:
        preliminary = resolve_analytical_request(candidate)
        _require_expected_preliminary_blocker(preliminary, candidate, context)
        current_period = _preliminary_current_period(preliminary)
        aliases = _authorized_aliases(candidate.entities, context, current_period)
        candidate = replace(candidate, authorized_aliases=aliases)

    result = resolve_analytical_request(candidate)
    if result.outcome is not ResolutionOutcome.RESOLVED:
        _refuse_unresolved(result, candidate, context)

    request_context = MeasurementRequestContext(
        contracts=context.contracts,
        computed_at=context.computed_at,
        correlation_id=context.correlation_id,
        max_rows=_optional_positive_integer(arguments, "max_rows") or DEFAULT_MAX_ROWS,
    )
    try:
        mapped = to_measurement_tool_request(result, request_context)
    except ValueError as error:
        raise ValidationError(
            code=gateway.BAD_INVOCATION,
            description="The resolved semantic intent cannot be applied to measurement execution.",
            context={"cause": type(error).__name__},
        ) from None
    if mapped.tool is not tool:
        raise ValidationError(
            code=gateway.BAD_INVOCATION,
            description=(
                "The operation candidate is incompatible with the selected measurement tool."
            ),
            context={
                "field": "operation",
                "outcome": ResolutionOutcome.INVALID_CONTEXT.value,
            },
        )
    return mapped


def _require_expected_preliminary_blocker(
    result: AnalyticalResolutionResult,
    candidate: CandidateAnalyticalRequest,
    context: SemanticMeasurementContext,
) -> None:
    if not result.records:
        raise ValueError("preliminary resolution returned no blocker.")
    blocker = result.records[-1]
    entity_fields = {intent.kind.value for intent in candidate.entities}
    expected_entity = (
        result.outcome is ResolutionOutcome.NOT_FOUND and blocker.field in entity_fields
    )
    if not expected_entity:
        _refuse_unresolved(result, candidate, context)


def _preliminary_current_period(result: AnalyticalResolutionResult) -> DateRange:
    record = result.field("current_period")
    if (
        record is None
        or record.outcome is not ResolutionOutcome.RESOLVED
        or not isinstance(record.value, DateRange)
    ):
        raise ValueError("preliminary resolution did not produce a resolved DateRange.")
    return record.value


def _candidate_request(
    tool: MeasurementTool,
    arguments: dict[str, Any],
    context: SemanticMeasurementContext,
) -> CandidateAnalyticalRequest:
    """Narrow JSON values into candidate semantic domain types."""
    return CandidateAnalyticalRequest(
        catalog=context.catalog,
        today=context.today,
        scope=context.scope,
        authorized_aliases=(),
        metric_reference=_required_text(arguments, "metric_reference"),
        operation_reference=_operation_reference(tool, arguments),
        current_period=_period_intent(arguments, prefix=""),
        prior_period=_period_intent(arguments, prefix="prior_"),
        grains=_text_array(arguments, "grains"),
        entities=_entity_intents(arguments),
        source=_source_selector(arguments),
        report_lenses=_report_lenses(arguments),
        selectors=_selectors(arguments),
        currency=_currency_request(arguments),
        rank_n=(
            _required_positive_integer(arguments, "rank_size")
            if tool is MeasurementTool.TOP_N
            else None
        ),
    )


def _reject_undeclared_arguments(tool: MeasurementTool, arguments: dict[str, Any]) -> None:
    declared = {argument.name for argument in contract_for(tool.value).arguments}
    unknown = set(arguments) - declared
    if unknown:
        raise ValidationError(
            code=gateway.BAD_INVOCATION,
            description=(
                "This measurement request contains one or more undeclared arguments. "
                "Retry using only names in this tool's input schema."
            ),
            context={"outcome": "undeclared_arguments", "n": len(unknown)},
        )


def _required_text(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            code=gateway.BAD_INVOCATION,
            description=f"{name} must be non-empty text.",
            context={"argument": name},
        )
    return value.strip()


def _optional_text(arguments: dict[str, Any], name: str) -> str | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            code=gateway.BAD_INVOCATION,
            description=f"{name} must be non-empty text when supplied.",
            context={"argument": name},
        )
    return value.strip()


def _text_array(arguments: dict[str, Any], name: str) -> tuple[str, ...]:
    value = arguments.get(name)
    if not isinstance(value, list) or not value:
        raise ValidationError(
            code=gateway.BAD_INVOCATION,
            description=f"{name} must be a non-empty array of names.",
            context={"argument": name},
        )
    items = cast("list[object]", value)
    if any(not isinstance(item, str) or not item.strip() for item in items):
        raise ValidationError(
            code=gateway.BAD_INVOCATION,
            description=f"Every {name} entry must be a non-empty name.",
            context={"argument": name},
        )
    return tuple(cast("str", item).strip() for item in items)


def _string_map(arguments: dict[str, Any], name: str) -> dict[str, str]:
    value = arguments.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValidationError(
            code=gateway.BAD_INVOCATION,
            description=f"{name} must be an object mapping names to text.",
            context={"argument": name},
        )
    pairs = cast("dict[object, object]", value)
    if any(
        not isinstance(key, str) or not key.strip() or not isinstance(item, str) or not item.strip()
        for key, item in pairs.items()
    ):
        raise ValidationError(
            code=gateway.BAD_INVOCATION,
            description=f"{name} accepts non-empty name and text pairs only.",
            context={"argument": name},
        )
    return {cast("str", key).strip(): cast("str", item).strip() for key, item in pairs.items()}


def _period_intent(arguments: dict[str, Any], *, prefix: str) -> PeriodIntent | None:
    reference_name = f"{prefix}period_reference"
    start_name = f"{prefix}period_start_candidate"
    end_name = f"{prefix}period_end_candidate"
    reference = _optional_text(arguments, reference_name)
    start = _optional_text(arguments, start_name)
    end = _optional_text(arguments, end_name)
    if reference is not None and (start is not None or end is not None):
        raise ValidationError(
            code=gateway.BAD_INVOCATION,
            description=(f"{reference_name} cannot be combined with {start_name} or {end_name}."),
            context={"period": prefix or "current"},
        )
    if (start is None) != (end is None):
        missing = end_name if end is None else start_name
        raise ValidationError(
            code=gateway.BAD_INVOCATION,
            description=f"A candidate date pair also requires {missing}.",
            context={"period": prefix or "current"},
        )
    if reference is not None:
        return PeriodIntent(reference=reference)
    if start is None or end is None:
        return None
    try:
        return PeriodIntent(
            window=DateRange(start=date.fromisoformat(start), end=date.fromisoformat(end))
        )
    except ValueError:
        raise ValidationError(
            code=gateway.BAD_INVOCATION,
            description=f"{start_name} and {end_name} must form an inclusive ISO date range.",
            context={"period": prefix or "current"},
        ) from None


def _operation_reference(tool: MeasurementTool, arguments: dict[str, Any]) -> str:
    explicit = _optional_text(arguments, "operation")
    if explicit is not None:
        return explicit.strip().lower()
    if tool is MeasurementTool.COMPARE_WINDOWS:
        return "comparison"
    if tool is MeasurementTool.TOP_N:
        order = _required_text(arguments, "rank_order").upper()
        if order == "VALUE":
            return "top value"
        if order == "INCREASE":
            return "top increase"
        raise ValidationError(
            code=gateway.BAD_INVOCATION,
            description="rank_order must be VALUE or INCREASE.",
            context={"argument": "rank_order"},
        )
    return "breakdown"


def _entity_intents(arguments: dict[str, Any]) -> tuple[EntityIntent, ...]:
    references = _string_map(arguments, "entity_references")
    intents: list[EntityIntent] = []
    for raw_kind, reference in references.items():
        try:
            kind = EntityKind(raw_kind)
        except ValueError:
            raise ValidationError(
                code=gateway.BAD_INVOCATION,
                description=f"{raw_kind} is not a supported measurement entity kind.",
                context={"argument": "entity_references"},
            ) from None
        intents.append(EntityIntent(kind, EntityReference(text=reference)))
    return tuple(intents)


def _report_lenses(arguments: dict[str, Any]) -> tuple[ReportLensReference, ...]:
    lenses = _string_map(arguments, "report_lenses")
    return tuple(ReportLensReference(name, value) for name, value in sorted(lenses.items()))


def _selectors(arguments: dict[str, Any]) -> tuple[SelectorReference, ...]:
    selectors = _string_map(arguments, "selectors")
    return tuple(SelectorReference(name, value) for name, value in sorted(selectors.items()))


def _source_selector(arguments: dict[str, Any]) -> SourceSelector | None:
    system = _optional_text(arguments, "source_system_selector")
    dataset = _optional_text(arguments, "source_dataset_selector")
    if system is None and dataset is None:
        return None
    if system is None or dataset is None:
        missing = "source_system_selector" if system is None else "source_dataset_selector"
        raise ValidationError(
            code=gateway.BAD_INVOCATION,
            description=f"An explicit source selector also requires {missing}.",
            context={"argument": missing},
        )
    return SourceSelector(SourceSystemKey(system), SourceDatasetKey(dataset))


def _currency_request(arguments: dict[str, Any]) -> AnalyticalCurrencyRequest | None:
    raw_mode = _optional_text(arguments, "currency_mode")
    target = _optional_text(arguments, "target_currency")
    if raw_mode is None:
        if target is not None:
            raise ValidationError(
                code=gateway.BAD_INVOCATION,
                description="target_currency requires currency_mode TARGET.",
                context={"argument": "target_currency"},
            )
        return None
    try:
        mode = AnalyticalCurrencyMode(raw_mode.lower())
        return AnalyticalCurrencyRequest(mode, target.upper() if target is not None else None)
    except ValueError:
        raise ValidationError(
            code=gateway.BAD_INVOCATION,
            description=(
                "currency_mode must be NATIVE without a target or TARGET with a three-letter "
                "target_currency."
            ),
            context={"argument": "currency_mode"},
        ) from None


def _required_positive_integer(arguments: dict[str, Any], name: str) -> int:
    value = arguments.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError(
            code=gateway.BAD_INVOCATION,
            description=f"{name} must be a whole number of at least 1.",
            context={"argument": name},
        )
    return value


def _optional_positive_integer(arguments: dict[str, Any], name: str) -> int:
    value = arguments.get(name)
    if value is None:
        return 0
    return _required_positive_integer(arguments, name)


def _authorized_aliases(
    intents: tuple[EntityIntent, ...],
    context: SemanticMeasurementContext,
    window: DateRange,
) -> tuple[EntityAlias, ...]:
    aliases: list[EntityAlias] = []
    for intent in intents:
        result = context.entity_catalog.lookup(
            EntityCatalogLookupRequest(
                scope=context.scope,
                reference=intent.reference,
                kinds=frozenset({intent.kind}),
                window=window,
                current_on=None,
                context=EntityContext(),
                max_candidates=MAX_ENTITY_CANDIDATES,
            )
        )
        if result.hidden_direct_match:
            _raise_not_found(intent.kind.value)
        if result.truncated:
            raise ValidationError(
                code=gateway.BAD_INVOCATION,
                description=(
                    f"The {intent.kind.value} name matched too many authorized candidates; "
                    "clarify it before measurement."
                ),
                context={"field": intent.kind.value, "truncated": True},
            )
        aliases.extend(result.aliases)
    return tuple(dict.fromkeys(aliases))


def _refuse_unresolved(
    result: AnalyticalResolutionResult,
    candidate: CandidateAnalyticalRequest,
    context: SemanticMeasurementContext,
) -> None:
    """Refuse an unresolved request, as no data when its metric has published nothing."""
    _raise_if_nothing_published(candidate, context)
    _raise_unresolved(result)


def _raise_if_nothing_published(
    candidate: CandidateAnalyticalRequest,
    context: SemanticMeasurementContext,
) -> None:
    """Refuse as no data when a uniquely named metric's sources have published nothing.

    Runs only on the refusal path, so a resolved request never reads coverage. With
    nothing published no rephrasing can succeed, so a shape refusal would mislead.
    """
    if context.default_window is None:
        return
    metrics = context.catalog.metric_candidates(candidate.metric_reference)
    if len(metrics) != 1:
        return
    metric = metrics[0]
    if not context.catalog.sources_for(metric) or not context.catalog.facts_read(metric):
        return
    if context.default_window(metric) is not None:
        return
    # A window the platform has not published is an answer, not an outage (ADR-062).
    raise DomainError(
        SELECTION_FAILED,
        f"Nothing has been published for {metric.label} yet, so there is no figure to report "
        "for any window. Figures appear once your data producers deliver its source data and "
        "it is published.",
        context={"field": "publication", "outcome": "nothing_published"},
    )


def _raise_unresolved(result: AnalyticalResolutionResult) -> None:
    if result.outcome is ResolutionOutcome.RESOLVED or not result.records:
        raise ValueError("resolved or empty resolution result cannot become a refusal.")
    record = result.records[-1]
    if result.outcome in _NOT_FOUND_OUTCOMES:
        _raise_not_found(record.field)
    candidates = _safe_candidate_projection(record)
    if result.outcome is ResolutionOutcome.AMBIGUOUS:
        suffix = f" Candidates: {'; '.join(candidates)}." if candidates else ""
        description = f"The {record.field} reference is ambiguous; clarify it.{suffix}"
    elif result.outcome is ResolutionOutcome.INVALID_CONTEXT:
        description = f"The {record.field} candidate is invalid in this request context."
    elif result.outcome is ResolutionOutcome.UNSUPPORTED_CAPABILITY:
        description = f"The {record.field} candidate is not supported by this measurement."
    else:
        description = f"The {record.field} candidate is incompatible with this measurement."
    raise ValidationError(
        code=gateway.BAD_INVOCATION,
        description=description,
        context={
            "field": record.field,
            "outcome": result.outcome.value,
            "candidates": candidates,
        },
    )


def _raise_not_found(field: str) -> None:
    """Use one external shape for absent and unauthorized-or-hidden references."""
    description = f"We could not resolve {field} from the supplied semantic intent."
    if field == "current_period":
        # Reaching here means no window was named AND no published coverage existed to
        # default to; the actionable next step belongs in the reason (ADR-062).
        description = (
            "The question names no reporting window, and no published coverage exists for "
            "this metric's sources to default to. Specify an explicit date window."
        )
    raise ValidationError(
        code=gateway.BAD_INVOCATION,
        description=description,
        context={"field": field},
    )


def _coverage_default(
    candidate: CandidateAnalyticalRequest,
    context: SemanticMeasurementContext,
) -> DateRange | None:
    """Compute the governed default window when the question named no period.

    Only an unambiguous metric reference can default: coverage belongs to a metric's own
    sources, and guessing the metric to pick a window would compound two defaults. A None
    return leaves the resolver's refusal path intact.
    """
    if context.default_window is None:
        return None
    metrics = context.catalog.metric_candidates(candidate.metric_reference)
    if len(metrics) != 1:
        return None
    return context.default_window(metrics[0])


def _safe_candidate_projection(record: FieldResolution[object]) -> tuple[str, ...]:
    """Project bounded labels and non-identity differentiators, never keys or evidence."""
    projected: list[str] = []
    for candidate in record.candidates[:_MAX_CLARIFICATION_CANDIDATES]:
        value = candidate.value
        if isinstance(value, EntitySelection) and candidate.label == value.key.value:
            continue
        details = tuple(
            f"{name}={item}"
            for name, item in candidate.differentiators
            if name in _SAFE_DIFFERENTIATORS
        )
        projected.append(
            candidate.label if not details else f"{candidate.label} ({', '.join(details)})"
        )
    return tuple(projected)


def to_measurement_tool_request(
    result: AnalyticalResolutionResult,
    context: MeasurementRequestContext,
) -> MeasurementToolRequest:
    """Map canonical values only; unresolved results cannot reach measurement."""
    if result.outcome is not ResolutionOutcome.RESOLVED or result.request is None:
        raise ValueError("only a resolved analytical request can map to measurement.")
    return _map_request(result.request, context)


def map_resolved_measurement(
    result: AnalyticalResolutionResult,
    context: MeasurementRequestContext,
) -> MeasurementToolRequest:
    """Named adapter entry point equivalent to `to_measurement_tool_request`."""
    return to_measurement_tool_request(result, context)


def _map_request(
    resolved: ResolvedAnalyticalRequest,
    context: MeasurementRequestContext,
) -> MeasurementToolRequest:
    metric = resolved.metric.value
    current = resolved.current_period.value
    operation = cast("AnalyticalOperation", resolved.operation.value)
    grains = resolved.grains.value
    sources = resolved.sources.value
    currency = resolved.currency.value
    if metric is None or current is None or grains is None or sources is None or currency is None:
        raise ValueError("resolved analytical fields unexpectedly carry no value.")
    if sources.selector is not None:
        raise ValueError("an explicit source selector cannot affect current measurement execution.")
    if currency.mode is not AnalyticalCurrencyMode.NATIVE or currency.target_currency is not None:
        raise ValueError("only native currency policy can reach measurement execution.")

    filters: dict[str, str] = {}
    for record in resolved.entities:
        selection = record.value
        if selection is None:
            raise ValueError("resolved entity unexpectedly carries no value.")
        column = _ENTITY_FILTERS.get(selection.kind)
        if column is None:
            raise ValueError(f"resolved entity kind {selection.kind.value} has no measurement map.")
        filters[column] = selection.key.value

    defaulted_filters: dict[str, str] = {}
    for record in resolved.report_lenses:
        lens = record.value
        if lens is None:  # pragma: no cover - resolved request invariant
            raise ValueError("resolved report lens unexpectedly carries no value.")
        if record.origin is FieldOrigin.GOVERNED_DEFAULT:
            defaulted_filters[lens.name] = lens.value
        elif record.origin is FieldOrigin.EXPLICIT_CUSTOMER_TEXT:
            filters[lens.name] = lens.value
        else:
            raise ValueError(
                f"report lens origin {record.origin.value} cannot map to measurement execution."
            )

    filters.update(_selector_filters(resolved))

    tool, order_by = _measurement_operation(operation)
    prior = resolved.prior_period.value if resolved.prior_period is not None else None
    return MeasurementToolRequest(
        tool=tool,
        catalog=resolved.catalog,
        contracts=context.contracts,
        metric_name=metric.name,
        grains=tuple(grain.name for grain in grains),
        window=current,
        computed_at=context.computed_at,
        correlation_id=context.correlation_id,
        resolution_provenance=_resolution_provenance(resolved),
        filters=filters,
        defaulted_filters=defaulted_filters,
        max_rows=context.max_rows,
        prior_window=prior,
        n=operation.n or 0,
        order_by=order_by,
    )


def _selector_filters(resolved: ResolvedAnalyticalRequest) -> dict[str, str]:
    filters: dict[str, str] = {}
    for record in resolved.selectors:
        selector = record.value
        if selector is None:  # pragma: no cover - resolved request invariant
            raise ValueError("resolved selector unexpectedly carries no value.")
        # Explicit, or pinned because the metric admits exactly one value (`_resolve_selectors`).
        if record.origin not in {FieldOrigin.EXPLICIT_CUSTOMER_TEXT, FieldOrigin.GOVERNED_DEFAULT}:
            raise ValueError(
                f"selector origin {record.origin.value} cannot map to measurement execution."
            )
        filters[selector.name] = selector.value
    return filters


def _resolution_provenance(
    resolved: ResolvedAnalyticalRequest,
) -> tuple[ResolutionProvenance, ...]:
    """Project only canonical public values, never resolution evidence or scope internals."""
    metric = resolved.metric.value
    current = resolved.current_period.value
    operation = resolved.operation.value
    grains = resolved.grains.value
    currency = resolved.currency.value
    if metric is None or current is None or operation is None or grains is None or currency is None:
        raise ValueError("resolved analytical fields unexpectedly carry no public value.")

    operation_value = operation.kind.value
    if operation.n is not None:
        operation_value = f"{operation_value} (n={operation.n})"
    currency_value = currency.mode.value
    if currency.target_currency is not None:
        currency_value = f"{currency_value} {currency.target_currency}"

    entries = [
        ResolutionProvenance(resolved.metric.field, resolved.metric.origin, metric.name),
        ResolutionProvenance(
            resolved.current_period.field,
            resolved.current_period.origin,
            str(current),
        ),
    ]
    if resolved.prior_period is not None:
        prior = resolved.prior_period.value
        if prior is None:  # pragma: no cover - ResolvedAnalyticalRequest rejects this shape
            raise ValueError("resolved prior period unexpectedly carries no public value.")
        entries.append(
            ResolutionProvenance(
                resolved.prior_period.field,
                resolved.prior_period.origin,
                str(prior),
            )
        )
    entries.extend(
        (
            ResolutionProvenance(
                resolved.operation.field,
                resolved.operation.origin,
                operation_value,
            ),
            ResolutionProvenance(
                resolved.grains.field,
                resolved.grains.origin,
                ", ".join(grain.name for grain in grains),
            ),
        )
    )

    for record in sorted(resolved.entities, key=lambda item: item.field):
        selection = record.value
        if selection is None:  # pragma: no cover - ResolvedAnalyticalRequest rejects this shape
            raise ValueError("resolved entity unexpectedly carries no public value.")
        entries.append(
            ResolutionProvenance(selection.kind.value, record.origin, selection.key.value)
        )
    for record in sorted(resolved.report_lenses, key=lambda item: item.field):
        lens = record.value
        if lens is None:  # pragma: no cover - ResolvedAnalyticalRequest rejects this shape
            raise ValueError("resolved report lens unexpectedly carries no public value.")
        entries.append(ResolutionProvenance(lens.name, record.origin, lens.value))

    for record in sorted(resolved.selectors, key=lambda item: item.field):
        selector = record.value
        if selector is None:  # pragma: no cover - resolved request invariant
            raise ValueError("resolved selector unexpectedly carries no public value.")
        entries.append(ResolutionProvenance(selector.name, record.origin, selector.value))

    entries.append(
        ResolutionProvenance(resolved.currency.field, resolved.currency.origin, currency_value)
    )
    return tuple(entries)


def _measurement_operation(
    operation: AnalyticalOperation,
) -> tuple[MeasurementTool, RankOrder]:
    match operation.kind:
        case AnalyticalOperationKind.BREAKDOWN | AnalyticalOperationKind.TREND:
            return MeasurementTool.METRIC_BY_GRAIN, RankOrder.VALUE
        case AnalyticalOperationKind.COMPARISON:
            return MeasurementTool.COMPARE_WINDOWS, RankOrder.VALUE
        case AnalyticalOperationKind.TOP_VALUE:
            return MeasurementTool.TOP_N, RankOrder.VALUE
        case AnalyticalOperationKind.TOP_INCREASE:
            return MeasurementTool.TOP_N, RankOrder.CHANGE
    raise ValueError(f"unsupported resolved operation {operation.kind}.")
