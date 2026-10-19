# SPDX-License-Identifier: MIT-0
# ruff: noqa: D102, PLR2004
"""Pure governed insight contracts, findings, slot states, and finding rendering."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final, Protocol, cast

from domain.presentation.currency_display import currency_display_text
from domain.presentation.next_steps import (
    AssistantTarget,
    CampaignKeyTarget,
    CampaignRefTarget,
    MetricFocusTarget,
    NextStepTarget,
    PageTarget,
    PresetTarget,
    ProductRefTarget,
    next_step_target_from_payload,
    next_step_target_payload,
)
from domain.presentation.prompt_authorship import (
    OperatorPromptAuthor,
    PromptVersionAuthor,
    SystemPromptAuthor,
)
from domain.presentation.prompt_composition import PromptFragmentDigests
from domain.presentation.temporal import TemporalDisplayGrain
from domain.shared_kernel.errors import ErrorCode, InvariantError
from domain.shared_kernel.values import DateRange

PROMPT_ACTIVATION_CALIBRATION_REQUIRED = ErrorCode("INSIGHT", 10)
"""Typed refusal when an immutable version lacks a passed calibration result."""

PROMPT_CALIBRATION_QUEUE_UNAVAILABLE = ErrorCode("INSIGHT", 11)
"""Dependency failure when a pending prompt version cannot be admitted durably."""


class InsightSlotId(StrEnum):
    """The closed command-center vocabulary of reviewed insight purposes."""

    CONTRIBUTION = "contribution"
    DIVERGENCE = "divergence"
    EXPOSURE = "exposure"
    MOVERS = "movers"
    TREND_SHAPE = "trend-shape"
    EFFICIENCY_DRIFT = "efficiency-drift"
    FUNNEL = "funnel"
    NEXT_CHECK = "next-check"
    METRIC_FOCUS = "metric-focus"
    RETAIL_METRIC_FOCUS = "retail-metric-focus"
    DEMAND_SHAPE = "demand-shape"
    RETURNS_PRESSURE = "returns-pressure"
    ORDERED_VS_SHIPPED = "ordered-vs-shipped"
    RETAIL_MOVERS = "retail-movers"
    NETWORK_SCORECARD = "network-scorecard"
    PORTFOLIO_ACQUISITION = "portfolio-acquisition"
    REACH_OVERLAP = "reach-overlap"
    FULL_FUNNEL_ASSOCIATION = "full-funnel-association"
    FREQUENCY_SATURATION = "frequency-saturation"
    RETENTION_SHIFT = "retention-shift"
    SEGMENT_OVER_INDEX = "segment-over-index"


DETERMINISTIC_SLOT_IDS: Final = frozenset(
    {
        InsightSlotId.MOVERS,
        InsightSlotId.RETAIL_MOVERS,
        InsightSlotId.NETWORK_SCORECARD,
        InsightSlotId.PORTFOLIO_ACQUISITION,
        InsightSlotId.REACH_OVERLAP,
        InsightSlotId.FULL_FUNNEL_ASSOCIATION,
        InsightSlotId.FREQUENCY_SATURATION,
        InsightSlotId.RETENTION_SHIFT,
        InsightSlotId.SEGMENT_OVER_INDEX,
    }
)
"""Slots the server renders from deterministic findings alone: no prompt, model or deep tier."""


class InsightOutputClass(StrEnum):
    """Narration authority classes accepted by the command-center surface."""

    OBSERVED = "observed"
    PERSPECTIVE = "perspective"
    HYPOTHESIS = "hypothesis"


class InsightSlotState(StrEnum):
    """A declared slot is filled, intentionally silent, or unavailable."""

    FILLED = "filled"
    SILENT = "silent"
    UNAVAILABLE = "unavailable"


class InsightAbsenceClass(StrEnum):
    """Why a declared slot carries no narration: the customer-layer outcome taxonomy.

    Absence of output is never the signal (declared-outcomes rule): every non-filled
    slot names its class so the page can state the business reason at the display
    site and a customer can judge expected-versus-flaw. ``EVALUATED_QUIET`` means the
    data was complete and either the deterministic predicate or the model judged
    nothing worth saying (provenance distinguishable via ``prompt_version``).
    ``INELIGIBLE`` means the slot could not be evaluated because a required evidence
    input is absent for this subject and window. ``GENERATION_PENDING`` means
    generation failed and a durable retry is queued. ``OUT_OF_SLICE`` marks slots a
    composition deliberately does not generate.
    """

    EVALUATED_QUIET = "evaluated-quiet"
    INELIGIBLE = "ineligible"
    GENERATION_PENDING = "generation-pending"
    OUT_OF_SLICE = "out-of-slice"


class InsightCalibrationKind(StrEnum):
    """Required fictional calibration families for a reviewed insight prompt.

    ``KNOWN_CORRECT``, ``EXPLORATORY``, and ``DERIVED_FIGURE`` are must-accept
    families; the others are must-reject. ``EXPLORATORY`` (ADR-076 decision 8 as
    amended 2026-09-24) makes the gate symmetric: a labeled hypothesis, rounded
    figures in prose, or an interpretive reading must be ACCEPTED, so an
    over-restrictive prompt fails calibration exactly as a fabricating one does.
    The three derived-figure families (ADR-082 §4) calibrate the figure rule: a
    correctly derived figure must be accepted, while a miscomputed derivation and
    one that combines findings of incompatible basis must be rejected.
    """

    KNOWN_CORRECT = "known-correct"
    CONTRADICTION = "contradiction"
    FLUENT_UNSUPPORTED = "fluent-unsupported"
    EXPLORATORY = "exploratory"
    DERIVED_FIGURE = "derived-figure"
    MISCOMPUTED_DERIVATION = "miscomputed-derivation"
    INCOMPATIBLE_BASIS = "incompatible-basis"


class InsightModelRoute(StrEnum):
    """The closed configured model-route vocabulary for reviewed insight prompts."""

    QUICK = "quick"
    DEEP = "deep"


class InsightPromptTier(StrEnum):
    """The closed delivery-tier vocabulary a prompt lifecycle identity belongs to.

    A tier is registry lifecycle partitioning (one activation pointer per slot per
    tier, ADR-076 decision 6); the model route stays the orthogonal model-selection
    attribute on the version record. The card tier is the baked/cached narration;
    the deep tier is the on-request dialog analysis (its serving path is slice G).
    """

    CARD = "card"
    DEEP = "deep"


class InsightCacheOutcome(StrEnum):
    """The closed cache-read outcome vocabulary used by logs and EMF metrics."""

    HIT = "hit"
    DEGRADED_HIT = "degraded_hit"
    MISS = "miss"


class InsightHorizonRole(StrEnum):
    """The closed horizon-role vocabulary a preset window declares (ADR-076 decision 5).

    Each preset answers a different question: ``OPERATIONAL_PULSE`` (1D) asks what
    changed yesterday and biases toward silence; ``TACTICAL_REVIEW`` (7D) reads this
    window against its aligned prior; ``PATTERN_CONFIRMATION`` (14D) weighs
    consistency and offsets over single movers. The payload declares the role;
    interpreting it is prompt work, never instruction-like context.
    """

    OPERATIONAL_PULSE = "operational-pulse"
    TACTICAL_REVIEW = "tactical-review"
    PATTERN_CONFIRMATION = "pattern-confirmation"


def horizon_role_for(days: int) -> InsightHorizonRole:
    """Classify a window length into its declared horizon role (total by banding).

    The ADR binds the roles to the 1D/7D/14D presets; lengths between and beyond
    them take the nearest question by banding (1 day asks what changed yesterday,
    up to a week reads against the aligned prior, anything longer weighs pattern
    consistency), so every window — including a custom or 30-day one — declares a
    role rather than silently omitting the profile.
    """
    if days < 1:
        raise ValueError("a horizon role requires a positive window length.")
    if days == 1:
        return InsightHorizonRole.OPERATIONAL_PULSE
    if days <= 7:
        return InsightHorizonRole.TACTICAL_REVIEW
    return InsightHorizonRole.PATTERN_CONFIRMATION


def preset_window_days(role: InsightHorizonRole) -> int:
    """Return the preset window length a horizon role is bound to (ADR-076 decision 5).

    The inverse companion of ``horizon_role_for`` for a caller that must render a
    representative window for a declared role — a calibration payload — so the
    1D/7D/14D binding has exactly one home.
    """
    return {
        InsightHorizonRole.OPERATIONAL_PULSE: 1,
        InsightHorizonRole.TACTICAL_REVIEW: 7,
        InsightHorizonRole.PATTERN_CONFIRMATION: 14,
    }[role]


@dataclass(frozen=True, slots=True)
class InsightWindowProfile:
    """The typed window profile declared in every generation payload (ADR-076 decision 5).

    Window length and grain, the aligned-prior definition, and the closed horizon
    role. The payload declares the role; interpreting it is prompt work, never
    instruction-like context. The prior pair is present exactly when the surface
    has an aligned prior window; ISO date strings, matching the cache key's form.
    """

    days: int
    grain: TemporalDisplayGrain
    prior_start: str | None
    prior_end: str | None
    role: InsightHorizonRole

    def __post_init__(self) -> None:
        if self.days < 1:
            raise ValueError("insight window profiles require a positive day count.")
        if (self.prior_start is None) != (self.prior_end is None):
            raise ValueError("insight window profile prior dates are present together.")
        if self.prior_start is not None and self.prior_end is not None:
            if not self.prior_start.strip() or not self.prior_end.strip():
                raise ValueError("insight window profile prior dates must be nonblank.")
            if self.prior_end < self.prior_start:
                raise ValueError("insight window profile prior end precedes its start.")
        if not isinstance(cast("object", self.role), InsightHorizonRole) or not isinstance(
            cast("object", self.grain), TemporalDisplayGrain
        ):
            raise ValueError("insight window profiles require declared role and grain.")


def insight_window_profile(
    window: DateRange,
    prior: DateRange | None,
    grain: TemporalDisplayGrain = TemporalDisplayGrain.DAY,
) -> InsightWindowProfile:
    """Derive the declared profile from the served window; the role is length-owned.

    The grain defaults to DAY because every insight-serving surface today renders
    day-grained series (the serving temporal policy declares day pages only). A
    surface serving WEEK or PERIOD grain must pass its artifact's grain explicitly;
    the default is a statement about today's surfaces, not a universal truth.
    """
    return InsightWindowProfile(
        days=window.days,
        grain=grain,
        prior_start=None if prior is None else prior.start.isoformat(),
        prior_end=None if prior is None else prior.end.isoformat(),
        role=horizon_role_for(window.days),
    )


class InsightCalibrationOutcome(StrEnum):
    """The recorded result of evaluating one required calibration case."""

    PASSED = "passed"
    FAILED = "failed"


class InsightCalibrationDisposition(StrEnum):
    """The machine-checkable disposition a fictional calibration case must produce."""

    ACCEPT = "accept"
    REJECT = "reject"


class InsightFindingKind(StrEnum):
    """Arithmetic shapes calculated by the domain before narration."""

    ABSOLUTE_CHANGE = "absolute-change"
    PERCENTAGE_CHANGE = "percentage-change"
    SHARE_OF_TOTAL = "share-of-total"
    RANKED_ABSOLUTE_CHANGE = "ranked-absolute-change"


class InsightEvidenceInput(StrEnum):
    """Closed server-owned evidence vocabulary used by executable slot predicates."""

    PORTFOLIO_TOTALS = "portfolio-totals"
    PORTFOLIO_DIRECTION = "portfolio-direction"
    CAMPAIGN_COMPARISONS = "campaign-comparisons"
    DETAIL_COMPARISONS = "detail-comparisons"
    PURCHASE_SERIES = "purchase-series"
    DELIVERY_METRICS = "delivery-metrics"
    DELIVERY_FOLLOW_UP = "delivery-follow-up"
    FOCUSED_METRIC = "focused-metric"
    RETAIL_TOTALS = "retail-totals"
    RETAIL_PRODUCT_COMPARISONS = "retail-product-comparisons"
    RETAIL_RECONCILIATION = "retail-reconciliation"
    NETWORK_CHANNEL_COMPARISONS = "network-channel-comparisons"
    EXPOSURE_GROUP_CELLS = "exposure-group-cells"
    """One shopper-aggregate delivery's exposure-group cells on account IDs (pack 5)."""
    FREQUENCY_BUCKET_CELLS = "frequency-bucket-cells"
    """One shopper-aggregate delivery's frequency buckets on account IDs (pack 6)."""
    COHORT_CELLS = "cohort-cells"
    """One cohort vintage's delivery: two cohorts of one acquisition group (packs 14-15)."""
    SEGMENT_PROFILE_ROWS = "segment-profile-rows"
    """One buyer-profile delivery's segment rows and its profiled_total row (packs 10-12)."""


class DeliveryFollowUpEvidence(StrEnum):
    """Declared delivery reads that may support a next-check hypothesis."""

    CLICKS = "delivery-clicks"


class PredicateQuietReason(StrEnum):
    """Why an evaluated slot admitted nothing, in decision order (ADR-068 2026-10-05)."""

    MISSING_INPUT = "missing-input"
    """A required evidence input or delivery follow-up evidence is absent."""
    MISSING_KIND = "missing-kind"
    """Some required finding kind has no finding at all, before materiality."""
    NEAR_ZERO_PRIOR = "near-zero-prior"
    """Every finding of some required kind was removed only by the near-zero prior bound."""
    BELOW_THRESHOLD = "below-threshold"
    """Some required kind has findings, but none at or above its threshold."""


class FindingUnit(StrEnum):
    """What one finding's value measures, so it meets a threshold of its own kind (ADR-076 §5)."""

    CURRENCY = "currency"
    UNIT_PRICE = "unit_price"
    COUNT = "count"
    RATE_PP = "rate_pp"
    MULTIPLIER = "multiplier"
    PERCENT = "percent"
    SHARE = "share"


class OperandBasis(StrEnum):
    """What a finding compares: one campaign's window, the portfolio's, one product's, ..."""

    CAMPAIGN_WINDOW = "campaign_window"
    PORTFOLIO_WINDOW = "portfolio_window"
    RETAIL_WINDOW = "retail_window"
    PRODUCT_WINDOW = "product_window"
    CAMPAIGN_DAY = "campaign_day"
    CHANNEL_WINDOW = "channel_window"
    """One channel's window inside one network and one marketplace (ADR-106)."""
    GROUP_DELIVERY = "group_delivery"
    """One exposure group inside one complete period's shopper-aggregate delivery (ADR-101)."""
    BUCKET_DELIVERY = "bucket_delivery"
    """One frequency bucket inside one complete period's shopper-aggregate delivery (ADR-101)."""
    COHORT_DELIVERY = "cohort_delivery"
    """One acquisition group's follow-up month inside one cohort vintage's delivery (ADR-104)."""
    SEGMENT_DELIVERY = "segment_delivery"
    """One audience segment inside one complete month's buyer-profile delivery (design §6.5)."""


class MetricScale(StrEnum):
    """The scale of a percentage finding's prior, which the near-zero bound is sized in."""

    CURRENCY = "currency"
    UNIT_PRICE = "unit_price"
    COUNT = "count"
    RATE_PP = "rate_pp"
    MULTIPLIER = "multiplier"


_PER_METRIC_UNITS = frozenset({FindingUnit.CURRENCY, FindingUnit.COUNT, FindingUnit.UNIT_PRICE})
"""The amount units a `per_metric` row can size (ADR-076 §5)."""

MATERIALITY_UNDECLARED = ErrorCode("INSIGHT", 13)
"""A finding's basis, unit, metric or prior scale has no declared threshold or bound."""


def _positive(value: Decimal, label: str) -> None:
    if not value.is_finite() or value <= 0:
        raise ValueError(f"insight materiality {label} must be finite and positive.")


@dataclass(frozen=True, slots=True)
class HorizonThresholds:
    """One threshold per horizon role."""

    operational_pulse: Decimal
    tactical_review: Decimal
    pattern_confirmation: Decimal

    def __post_init__(self) -> None:
        for value in (self.operational_pulse, self.tactical_review, self.pattern_confirmation):
            _positive(value, "thresholds")

    def for_role(self, role: InsightHorizonRole) -> Decimal:
        """Return the threshold declared for one horizon role."""
        return {
            InsightHorizonRole.OPERATIONAL_PULSE: self.operational_pulse,
            InsightHorizonRole.TACTICAL_REVIEW: self.tactical_review,
            InsightHorizonRole.PATTERN_CONFIRMATION: self.pattern_confirmation,
        }[role]


@dataclass(frozen=True, slots=True)
class BasisThresholds:
    """One operand basis's per-unit rows and its per-metric rows (dictionary ids)."""

    per_unit: Mapping[FindingUnit, HorizonThresholds]
    per_metric: Mapping[str, HorizonThresholds] = field(
        default_factory=dict[str, HorizonThresholds]
    )

    def __post_init__(self) -> None:
        if not self.per_unit and not self.per_metric:
            raise ValueError("an insight materiality basis declares at least one threshold.")


@dataclass(frozen=True, slots=True)
class NearZeroPriors:
    """The prior magnitude at or below which a percentage finding is suppressed, per scale.

    A rate prior is compared in percentage points (`abs(prior) x 100`); a unit-price bound is
    declared per dictionary id, because a CPC and a CPM differ by 1,000x (R5-13).
    """

    per_scale: Mapping[MetricScale, Decimal]
    unit_price: Mapping[str, Decimal] = field(default_factory=dict[str, Decimal])

    def __post_init__(self) -> None:
        if MetricScale.UNIT_PRICE in self.per_scale:
            raise ValueError("a unit-price near-zero bound is declared per metric.")
        for value in (*self.per_scale.values(), *self.unit_price.values()):
            if not value.is_finite() or value < 0:
                raise ValueError("insight near-zero prior bounds must be finite and non-negative.")


@dataclass(frozen=True, slots=True)
class InsightMateriality:
    """Unit-aware, basis-aware thresholds and near-zero prior bounds (ADR-076 §5).

    A finding meets the threshold declared for its operand basis, its unit and, when the basis
    declares one, its metric's own row. A finding the contract declares nothing for raises
    `InvariantError`: that is a code or contract defect, never a silent pass.
    """

    bases: Mapping[OperandBasis, BasisThresholds]
    near_zero_prior: NearZeroPriors

    def __post_init__(self) -> None:
        if not self.bases:
            raise ValueError("insight materiality declares at least one operand basis.")

    def threshold_for(  # noqa: PLR0913 — one finding's declared coordinates, by keyword
        self,
        role: InsightHorizonRole,
        basis: OperandBasis,
        *,
        unit: FindingUnit,
        metric_id: str,
    ) -> Decimal:
        """Return an amount's own `per_metric` row when the basis declares one, else its unit row.

        A `per_metric` row sizes one metric's amounts; a percentage, share, rate or multiplier
        finding on the same metric always meets its unit row.
        """
        declared = self.bases.get(basis)
        own = (
            declared.per_metric.get(metric_id)
            if declared is not None and unit in _PER_METRIC_UNITS
            else None
        )
        row = own or (None if declared is None else declared.per_unit.get(unit))
        if row is None:
            raise InvariantError(
                MATERIALITY_UNDECLARED,
                "No materiality threshold is declared for this finding.",
                context={"basis": basis.value, "unit": unit.value, "metric": metric_id},
            )
        return row.for_role(role)

    def prior_bound_for(self, scale: MetricScale, metric_id: str) -> Decimal:
        """Return the near-zero prior bound for a scale (unit prices: per metric)."""
        bound = (
            self.near_zero_prior.unit_price.get(metric_id)
            if scale is MetricScale.UNIT_PRICE
            else self.near_zero_prior.per_scale.get(scale)
        )
        if bound is None:
            raise InvariantError(
                MATERIALITY_UNDECLARED,
                "No near-zero prior bound is declared for this finding's scale.",
                context={"scale": scale.value, "metric": metric_id},
            )
        return bound


@dataclass(frozen=True, slots=True)
class InsightSlotPredicate:
    """Required evidence, finding kinds, and materiality for one admitted model request."""

    required_evidence_inputs: tuple[InsightEvidenceInput, ...]
    required_finding_kinds: tuple[InsightFindingKind, ...]
    materiality: InsightMateriality
    delivery_follow_up_evidence: tuple[DeliveryFollowUpEvidence, ...]

    def __post_init__(self) -> None:
        if not self.required_evidence_inputs or not self.required_finding_kinds:
            raise ValueError("insight predicates require evidence inputs and finding kinds.")
        if len(set(self.required_evidence_inputs)) != len(self.required_evidence_inputs):
            raise ValueError("insight predicate evidence inputs must be unique.")
        if len(set(self.required_finding_kinds)) != len(self.required_finding_kinds):
            raise ValueError("insight predicate finding kinds must be unique.")
        if len(set(self.delivery_follow_up_evidence)) != len(self.delivery_follow_up_evidence):
            raise ValueError("insight predicate delivery follow-up evidence must be unique.")


_PORTFOLIO_SLOT_IDS = (
    InsightSlotId.CONTRIBUTION,
    InsightSlotId.DIVERGENCE,
    InsightSlotId.EXPOSURE,
    InsightSlotId.MOVERS,
)
_DETAIL_SLOT_IDS = (
    InsightSlotId.TREND_SHAPE,
    InsightSlotId.EFFICIENCY_DRIFT,
    InsightSlotId.FUNNEL,
    InsightSlotId.NEXT_CHECK,
)
_SLOT_OUTPUT_CLASSES = {
    InsightSlotId.CONTRIBUTION: InsightOutputClass.PERSPECTIVE,
    InsightSlotId.DIVERGENCE: InsightOutputClass.PERSPECTIVE,
    InsightSlotId.EXPOSURE: InsightOutputClass.PERSPECTIVE,
    InsightSlotId.MOVERS: InsightOutputClass.OBSERVED,
    InsightSlotId.TREND_SHAPE: InsightOutputClass.PERSPECTIVE,
    InsightSlotId.EFFICIENCY_DRIFT: InsightOutputClass.PERSPECTIVE,
    InsightSlotId.FUNNEL: InsightOutputClass.PERSPECTIVE,
    InsightSlotId.NEXT_CHECK: InsightOutputClass.HYPOTHESIS,
    InsightSlotId.METRIC_FOCUS: InsightOutputClass.PERSPECTIVE,
    InsightSlotId.RETAIL_METRIC_FOCUS: InsightOutputClass.PERSPECTIVE,
    InsightSlotId.DEMAND_SHAPE: InsightOutputClass.PERSPECTIVE,
    InsightSlotId.RETURNS_PRESSURE: InsightOutputClass.PERSPECTIVE,
    InsightSlotId.ORDERED_VS_SHIPPED: InsightOutputClass.PERSPECTIVE,
    InsightSlotId.RETAIL_MOVERS: InsightOutputClass.OBSERVED,
    InsightSlotId.NETWORK_SCORECARD: InsightOutputClass.OBSERVED,
    InsightSlotId.PORTFOLIO_ACQUISITION: InsightOutputClass.PERSPECTIVE,
    InsightSlotId.REACH_OVERLAP: InsightOutputClass.PERSPECTIVE,
    InsightSlotId.FULL_FUNNEL_ASSOCIATION: InsightOutputClass.PERSPECTIVE,
    InsightSlotId.FREQUENCY_SATURATION: InsightOutputClass.PERSPECTIVE,
    InsightSlotId.RETENTION_SHIFT: InsightOutputClass.PERSPECTIVE,
    InsightSlotId.SEGMENT_OVER_INDEX: InsightOutputClass.PERSPECTIVE,
}
_OUTPUT_SCHEMA_FIELDS = ("output_class", "sentences")
# The closed vocabulary of declarable structured response shapes (ADR-076 decisions 1-3,
# ADR-082). insight-narration-v1 is the card envelope: the output class and the
# sentences, whose figure faithfulness is owned by calibration and evaluation.
# insight-deep-dive-v1 is the on-request deep-tier analysis; its `cited_figures` list
# is part of the immutable deep cache row and the served dialog wire shape, so it
# stays declared, but nothing refuses an analysis over it and it never renders.
_SUPPORTED_OUTPUT_SCHEMAS: dict[str, tuple[str, ...]] = {
    "insight-narration-v1": _OUTPUT_SCHEMA_FIELDS,
    "insight-deep-dive-v1": (
        "what_stands_out",
        "candidate_readings",
        "discriminating_evidence",
        "cited_figures",
    ),
}
_NARRATION_SCHEMA_IDS = frozenset({"insight-narration-v1"})
_DEEP_DIVE_SCHEMA_ID = "insight-deep-dive-v1"


@dataclass(frozen=True, slots=True)
class InsightStructuredOutputSchema:
    """A compact declared reference to one supported structured response shape."""

    schema_id: str
    fields: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.fields != _SUPPORTED_OUTPUT_SCHEMAS.get(self.schema_id):
            raise ValueError("insight structured output schema is unsupported.")


@dataclass(frozen=True, slots=True)
class InsightDeepContext:
    """The declared deep-tier context and response shape for one model slot (ADR-076).

    The deep tier receives the card tier's declared inputs plus the inputs granted
    here — full context on request, never at bake. The cross-horizon direction-only
    input joins this declaration only once the window-scope steering carve-out is
    approved; until then no contract names a sibling window.
    """

    allowed_inputs: tuple[str, ...]
    structured_output_schema: InsightStructuredOutputSchema

    def __post_init__(self) -> None:
        if not self.allowed_inputs or any(not value.strip() for value in self.allowed_inputs):
            raise ValueError("deep insight context must declare nonblank allowed inputs.")
        if len(set(self.allowed_inputs)) != len(self.allowed_inputs):
            raise ValueError("deep insight context allowed inputs must be unique.")
        if self.structured_output_schema.schema_id != _DEEP_DIVE_SCHEMA_ID:
            raise ValueError("deep insight context requires the deep-dive response shape.")


_MUST_ACCEPT_CALIBRATION_KINDS = frozenset(
    {
        InsightCalibrationKind.KNOWN_CORRECT,
        InsightCalibrationKind.EXPLORATORY,
        InsightCalibrationKind.DERIVED_FIGURE,
    }
)
# The families the candidate prompt generates; every other family judges a supplied
# fictional draft.
_GENERATED_CALIBRATION_KINDS = frozenset(
    {InsightCalibrationKind.KNOWN_CORRECT, InsightCalibrationKind.EXPLORATORY}
)


@dataclass(frozen=True, slots=True)
class InsightCalibrationDraft:
    """A fictional candidate output a draft-judged calibration case supplies for judgment.

    A candidate prompt cannot be made to generate a bad output on purpose, so the
    must-reject families carry the bad draft themselves (ADR-076 decision 8), and the
    derived-figure family carries a correct derivation the evaluator must accept
    (ADR-082 §4): the case passes when the calibrated evaluator's disposition matches
    the family's expected disposition.
    """

    sentences: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.sentences or any(not sentence.strip() for sentence in self.sentences):
            raise ValueError("calibration drafts require nonblank sentences.")
        # A draft must satisfy the narration shape invariants at authoring time: a
        # draft the narration object refuses would otherwise abort every calibration
        # run for its slot instead of failing when the contract is loaded.
        if len(self.sentences) > 3:
            raise ValueError("calibration drafts carry one to three sentences.")
        if _canonical_json_string_bytes(" ".join(self.sentences)) > MAX_INSIGHT_SLOT_TEXT_BYTES:
            raise ValueError("calibration drafts must fit the slot byte bound.")


@dataclass(frozen=True, slots=True)
class InsightCalibrationCase:
    """One fictional prompt calibration case in the production payload shape.

    ``findings`` is the case's supplied finding set (fictional, production envelope
    shape); ``horizon`` places the case in the slot x horizon x family matrix. The
    known-correct and exploratory cases generate through the candidate prompt and
    carry no draft; every other family judges its supplied ``draft``.
    """

    kind: InsightCalibrationKind
    input_summary: str
    expected_outcome: str
    findings: tuple[DeterministicFinding, ...]
    horizon: InsightHorizonRole = InsightHorizonRole.TACTICAL_REVIEW
    draft: InsightCalibrationDraft | None = None

    def __post_init__(self) -> None:
        if not self.input_summary.strip() or not self.expected_outcome.strip():
            raise ValueError("insight calibration cases require fictional input and outcome.")
        if not self.findings:
            raise ValueError("insight calibration cases require fictional findings.")
        if len({finding.name for finding in self.findings}) != len(self.findings):
            raise ValueError("insight calibration case findings must have unique names.")
        if self.kind in _GENERATED_CALIBRATION_KINDS:
            if self.draft is not None:
                raise ValueError("a generated calibration case takes no draft.")
        elif self.draft is None:
            raise ValueError("a draft-judged calibration case requires its fictional draft.")

    @property
    def expected_disposition(self) -> InsightCalibrationDisposition:
        """Return the governed disposition for this fixed fictional case family."""
        return (
            InsightCalibrationDisposition.ACCEPT
            if self.kind in _MUST_ACCEPT_CALIBRATION_KINDS
            else InsightCalibrationDisposition.REJECT
        )


@dataclass(frozen=True, slots=True)
class InsightPromptCalibrationRunRequest:
    """The immutable version and contract-owned cases for one bounded server calibration.

    ``output_class`` is the slot's declared class: calibration generates in the
    production envelope shape, so its payloads declare — and its gate enforces —
    the same class production would (a hypothesis slot is calibrated as one).
    ``purpose`` is the slot contract's purpose, the value production sends. A case's
    summary never stands in for it: the summary describes the expected answer and
    would hand the generator facts its findings do not carry.
    """

    slot_id: InsightSlotId
    version: int
    prompt: str
    model_route: InsightModelRoute
    cases: tuple[InsightCalibrationCase, ...]
    output_class: InsightOutputClass
    tier: InsightPromptTier = InsightPromptTier.CARD
    purpose: str = field(kw_only=True)

    def __post_init__(self) -> None:
        if (
            self.slot_id in DETERMINISTIC_SLOT_IDS
            or self.version < 1
            or not self.prompt.strip()
            or not self.purpose.strip()
        ):
            raise ValueError("prompt calibration run identity is invalid.")
        kinds = tuple(case.kind for case in self.cases)
        if set(kinds) != set(InsightCalibrationKind) or len(set(kinds)) != len(kinds):
            raise ValueError("prompt calibration runs require every fictional case exactly once.")


@dataclass(frozen=True, slots=True)
class InsightCalibrationCaseResult:
    """The durable evaluation outcome for one required calibration case."""

    kind: InsightCalibrationKind
    outcome: InsightCalibrationOutcome


@dataclass(frozen=True, slots=True)
class InsightCalibrationResult:
    """The complete immutable evaluation result for one prompt version.

    ``evaluator`` names the judging model identity for model-judged dimensions
    (ADR-076 §7); ``None`` means no model judgment participated (a seeded result).
    """

    cases: tuple[InsightCalibrationCaseResult, ...]
    evaluator: str | None = None

    def __post_init__(self) -> None:
        kinds = tuple(case.kind for case in self.cases)
        if set(kinds) != set(InsightCalibrationKind) or len(set(kinds)) != len(kinds):
            raise ValueError("calibration results require each calibration kind exactly once.")
        if self.evaluator is not None and not self.evaluator.strip():
            raise ValueError("a calibration evaluator identity must be nonblank when present.")

    @property
    def outcome(self) -> InsightCalibrationOutcome:
        return (
            InsightCalibrationOutcome.PASSED
            if all(case.outcome is InsightCalibrationOutcome.PASSED for case in self.cases)
            else InsightCalibrationOutcome.FAILED
        )

    @classmethod
    def passed_result(cls) -> InsightCalibrationResult:
        """Construct the reviewed all-passed result used by an initial seeded version."""
        return cls(
            tuple(
                InsightCalibrationCaseResult(kind, InsightCalibrationOutcome.PASSED)
                for kind in InsightCalibrationKind
            )
        )


@dataclass(frozen=True, slots=True)
class InsightPromptVersionCreateRequest:
    """The immutable content, route, tier, and provenance for a newly reserved version.

    ``fragment_digests`` present means a tooling-composed version whose exact
    git-sourced fragments are recorded (ADR-076 decision 6); absent means an
    unsourced version — the operator emergency loop, or a contract-seeded v1.
    ``author`` and ``created_at`` are required: a new version always records who
    created it and when (ADR-069), and is never given a placeholder.
    """

    slot_id: InsightSlotId
    prompt: str
    model_route: InsightModelRoute
    tier: InsightPromptTier = InsightPromptTier.CARD
    fragment_digests: PromptFragmentDigests | None = None
    author: PromptVersionAuthor = field(kw_only=True)
    created_at: datetime = field(kw_only=True)

    def __post_init__(self) -> None:
        if self.slot_id is InsightSlotId.MOVERS or not self.prompt.strip():
            raise ValueError("only model slots can receive a nonblank prompt version.")
        if not isinstance(cast("object", self.model_route), InsightModelRoute):
            raise ValueError("prompt versions require an allowlisted model route.")
        if not isinstance(cast("object", self.tier), InsightPromptTier):
            raise ValueError("prompt versions require an allowlisted delivery tier.")
        if not isinstance(cast("object", self.author), OperatorPromptAuthor | SystemPromptAuthor):
            raise ValueError("prompt versions require an operator or system author.")
        if not isinstance(cast("object", self.created_at), datetime) or (
            self.created_at.tzinfo is None
        ):
            raise ValueError("prompt versions require a timezone-aware creation time.")


@dataclass(frozen=True, slots=True)
class InsightPromptSeedRequest:
    """A reviewed contract prompt to install as a slot's v1, and when this seed run started."""

    slot_id: InsightSlotId
    prompt: str
    created_at: datetime
    model_route: InsightModelRoute = InsightModelRoute.QUICK

    def __post_init__(self) -> None:
        if not self.prompt.strip() or not isinstance(
            cast("object", self.model_route), InsightModelRoute
        ):
            raise ValueError("seeded prompt and allowlisted model route are required.")
        if not isinstance(cast("object", self.created_at), datetime) or (
            self.created_at.tzinfo is None
        ):
            raise ValueError("a seeded prompt requires a timezone-aware creation time.")


@dataclass(frozen=True, slots=True)
class InsightPendingPromptVersion:
    """The immutable server-owned prompt, route, and tier eligible for calibration."""

    slot_id: InsightSlotId
    version: int
    prompt: str
    model_route: InsightModelRoute
    tier: InsightPromptTier = InsightPromptTier.CARD

    def __post_init__(self) -> None:
        if self.slot_id in DETERMINISTIC_SLOT_IDS or self.version < 1 or not self.prompt.strip():
            raise ValueError("only model prompt versions can be pending calibration.")
        if not isinstance(cast("object", self.model_route), InsightModelRoute):
            raise ValueError("pending prompt versions require an allowlisted model route.")
        if not isinstance(cast("object", self.tier), InsightPromptTier):
            raise ValueError("pending prompt versions require an allowlisted prompt tier.")


class InsightCalibrationClaimDisposition(StrEnum):
    """Whether one at-least-once worker delivery may run the pending calibration."""

    CLAIMED = "claimed"
    ACTIVE_LEASE = "active_lease"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class InsightPromptCalibrationOutboxWork:
    """One retained undispatched version identity created atomically with the version."""

    slot_id: InsightSlotId
    version: int

    def __post_init__(self) -> None:
        if self.slot_id in DETERMINISTIC_SLOT_IDS or self.version < 1:
            raise ValueError("prompt calibration outbox identity is invalid.")


@dataclass(frozen=True, slots=True)
class InsightPromptCalibrationClaimRequest:
    """One bounded worker claim on an immutable pending prompt version."""

    slot_id: InsightSlotId
    version: int
    lease_ref: str
    now_epoch_seconds: int
    lease_expires_epoch_seconds: int

    def __post_init__(self) -> None:
        if (
            self.slot_id in DETERMINISTIC_SLOT_IDS
            or self.version < 1
            or not self.lease_ref.strip()
            or self.now_epoch_seconds < 1
            or self.lease_expires_epoch_seconds <= self.now_epoch_seconds
        ):
            raise ValueError("prompt calibration claim identity is invalid.")


@dataclass(frozen=True, slots=True)
class InsightPromptCalibrationClaimResult:
    """Claim disposition plus the server-owned prompt only when work was claimed."""

    disposition: InsightCalibrationClaimDisposition
    pending: InsightPendingPromptVersion | None = None

    def __post_init__(self) -> None:
        if (self.disposition is InsightCalibrationClaimDisposition.CLAIMED) != (
            self.pending is not None
        ):
            raise ValueError("a claimed calibration alone carries its pending prompt.")


@dataclass(frozen=True, slots=True)
class InsightPromptCalibrationWorkState:
    """Exact state read by the recovery endpoint without claiming work."""

    calibration: str
    lease_active: bool

    def __post_init__(self) -> None:
        if self.calibration not in {"pending", "passed", "failed"}:
            raise ValueError("prompt calibration work state is invalid.")
        if self.calibration != "pending" and self.lease_active:
            raise ValueError("a terminal calibration cannot retain an active lease.")

    @property
    def should_enqueue(self) -> bool:
        return self.calibration == "pending" and not self.lease_active


@dataclass(frozen=True, slots=True)
class InsightClaimedPromptCalibrationRequest:
    """Complete a worker-owned calibration only while its exact lease remains current."""

    slot_id: InsightSlotId
    version: int
    lease_ref: str
    completed_at_epoch_seconds: int
    result: InsightCalibrationResult

    def __post_init__(self) -> None:
        if (
            self.slot_id in DETERMINISTIC_SLOT_IDS
            or self.version < 1
            or not self.lease_ref.strip()
            or self.completed_at_epoch_seconds < 1
        ):
            raise ValueError("claimed prompt calibration completion is invalid.")


@dataclass(frozen=True, slots=True)
class InsightPromptCalibrationRequest:
    """The complete persisted calibration outcome for one immutable prompt version."""

    slot_id: InsightSlotId
    version: int
    result: InsightCalibrationResult

    def __post_init__(self) -> None:
        if self.slot_id is InsightSlotId.MOVERS or self.version < 1:
            raise ValueError("only model prompt versions can receive calibration results.")


@dataclass(frozen=True, slots=True)
class InsightPromptActivationRequest:
    """One conditionally applied activation, including its immutable lifecycle event identity."""

    slot_id: InsightSlotId
    version: int
    expected_active_version: int | None
    operator: str
    event_id: str
    at: datetime

    def __post_init__(self) -> None:
        if (
            self.slot_id is InsightSlotId.MOVERS
            or self.version < 1
            or not self.operator.strip()
            or not self.event_id.strip()
            or self.at.tzinfo is None
            or self.at.utcoffset() is None
        ):
            raise ValueError("prompt activation identity is invalid.")
        if self.expected_active_version is not None and self.expected_active_version < 1:
            raise ValueError("expected active prompt version must be positive when present.")


@dataclass(frozen=True, slots=True)
class InsightPromptRetirementRequest:
    """One conditional active-pointer removal, including its immutable lifecycle event identity."""

    slot_id: InsightSlotId
    expected_active_version: int
    operator: str
    event_id: str
    at: datetime
    tier: InsightPromptTier = InsightPromptTier.CARD

    def __post_init__(self) -> None:
        if (
            self.slot_id is InsightSlotId.MOVERS
            or self.expected_active_version < 1
            or not self.operator.strip()
            or not self.event_id.strip()
            or self.at.tzinfo is None
            or self.at.utcoffset() is None
        ):
            raise ValueError("prompt retirement identity is invalid.")
        if not isinstance(cast("object", self.tier), InsightPromptTier):
            raise ValueError("prompt retirement requires an allowlisted delivery tier.")


_DETERMINISTIC_FINDING_KINDS: Final[Mapping[InsightSlotId, tuple[InsightFindingKind, ...]]] = {
    InsightSlotId.MOVERS: (InsightFindingKind.RANKED_ABSOLUTE_CHANGE,),
    InsightSlotId.RETAIL_MOVERS: (InsightFindingKind.RANKED_ABSOLUTE_CHANGE,),
    # A channel's share of its network's spend, and a channel's attributed ROAS move.
    InsightSlotId.NETWORK_SCORECARD: (
        InsightFindingKind.SHARE_OF_TOTAL,
        InsightFindingKind.PERCENTAGE_CHANGE,
    ),
    # One ad product's new-to-brand revenue share move inside Amazon Ads (New customers).
    InsightSlotId.PORTFOLIO_ACQUISITION: (InsightFindingKind.ABSOLUTE_CHANGE,),
    InsightSlotId.REACH_OVERLAP: (InsightFindingKind.SHARE_OF_TOTAL,),
    InsightSlotId.FULL_FUNNEL_ASSOCIATION: (InsightFindingKind.PERCENTAGE_CHANGE,),
    # The share of account shoppers who saw more ads than the named bucket (pack 6).
    InsightSlotId.FREQUENCY_SATURATION: (InsightFindingKind.SHARE_OF_TOTAL,),
    # The change in one group's month-1 or month-3 repeat rate, latest cohort against the one
    # before it, in rate points (packs 14-15).
    InsightSlotId.RETENTION_SHIFT: (InsightFindingKind.ABSOLUTE_CHANGE,),
    # A named segment's buyer share minus its reached share, inside one profile delivery
    # (packs 10-12); the bound and the minimum buyers are the gate.
    InsightSlotId.SEGMENT_OVER_INDEX: (InsightFindingKind.ABSOLUTE_CHANGE,),
}


@dataclass(frozen=True, slots=True)
class ShiftPolicy:
    """`retention-shift`'s versioned policy values (design §6.6, review DS5-7).

    A shift is named only when both cohorts hold at least `cohort_min` shoppers in the rate's
    denominator and the `z` interval of the difference of the two rates excludes zero (normal
    approximation); materiality is the contract's predicate.
    """

    cohort_min: int
    z: Decimal

    def __post_init__(self) -> None:
        if self.cohort_min < 1 or self.z <= 0:
            raise ValueError("the shift minimum is positive and z is positive.")


@dataclass(frozen=True, slots=True)
class OverIndexPolicy:
    """`segment-over-index`'s versioned policy values (design §6.5, reviews DS-8, DS3-6).

    A segment is named only when it holds at least `buyers_min` buyers (B_min) and the lower bound
    of its buyer share minus its reached share is above zero, with the Bonferroni critical value
    z_S = inv_cdf(1 - `family_alpha` / S) over the S segment rows of its delivery and profile
    path (`statistics.NormalDist`). Materiality is the contract's predicate.
    """

    buyers_min: int
    family_alpha: Decimal

    def __post_init__(self) -> None:
        if self.buyers_min < 1 or not Decimal(0) < self.family_alpha < Decimal("0.5"):
            raise ValueError("B_min is positive and the family alpha lies in (0, 0.5).")


@dataclass(frozen=True, slots=True)
class SaturationPolicy:
    """`frequency-saturation`'s versioned policy values (design §6.3, findings BO-24, DS-8).

    A step from bucket b to b+1 is compared only when both buckets hold at least
    `shoppers_min` shoppers and `purchasers_min` purchasers; it passes when the upper bound of
    the rise, (r2 - r1) + z * sqrt(r1(1 - r1)/n1 + r2(1 - r2)/n2), is at most `tolerance` * r1.
    The two-step rule (both b to b+1 and b+1 to b+2 pass) is the multiplicity guard, so z isn't
    corrected further.
    """

    shoppers_min: int
    purchasers_min: int
    tolerance: Decimal
    z: Decimal

    def __post_init__(self) -> None:
        if self.shoppers_min < 1 or self.purchasers_min < 1:
            raise ValueError("saturation minimums must be positive.")
        if not Decimal(0) < self.tolerance < 1 or self.z <= 0:
            raise ValueError("saturation tolerance lies in (0, 1) and z is positive.")


@dataclass(frozen=True, slots=True)
class InsightContract:
    """A versioned, reviewable slot declaration loaded from one YAML contract."""

    id: InsightSlotId
    version: int
    purpose: str
    display_name: str
    """The operator-facing slot name prompt administration shows (ADR-067 governed text)."""
    output_class: InsightOutputClass
    allowed_inputs: tuple[str, ...]
    deterministic_findings: tuple[InsightFindingKind, ...]
    predicate: InsightSlotPredicate
    silence_condition: str
    structured_output_schema: InsightStructuredOutputSchema
    initial_fictional_prompt: str | None
    calibration_cases: tuple[InsightCalibrationCase, ...]
    deep: InsightDeepContext | None
    next_step_kinds: tuple[InsightNextStepKind, ...] = ()
    """FR-11: the kinds this card may offer; empty until a gated prompt ships (R6-6)."""
    saturation_policy: SaturationPolicy | None = None
    """`frequency-saturation`'s policy values, declared by that contract and no other."""
    shift_policy: ShiftPolicy | None = None
    """`retention-shift`'s policy values, declared by that contract and no other."""
    over_index_policy: OverIndexPolicy | None = None
    """`segment-over-index`'s policy values, declared by that contract and no other."""

    def __post_init__(self) -> None:  # noqa: PLR0912
        if (
            self.version < 1
            or not self.purpose.strip()
            or not self.display_name.strip()
            or not self.silence_condition.strip()
        ):
            raise ValueError("insight contract identity and purpose are invalid.")
        if not self.allowed_inputs or any(not value.strip() for value in self.allowed_inputs):
            raise ValueError("insight contract must declare nonblank allowed inputs.")
        if len(set(self.allowed_inputs)) != len(self.allowed_inputs):
            raise ValueError("insight contract allowed inputs must be unique.")
        if not self.deterministic_findings or len(set(self.deterministic_findings)) != len(
            self.deterministic_findings
        ):
            raise ValueError("insight contract deterministic findings must be unique and nonempty.")
        if not set(self.predicate.required_finding_kinds).issubset(self.deterministic_findings):
            raise ValueError("insight predicate finding kinds must be declared by the contract.")
        if self.id is InsightSlotId.NEXT_CHECK:
            if (
                InsightEvidenceInput.DELIVERY_FOLLOW_UP
                not in self.predicate.required_evidence_inputs
                or not self.predicate.delivery_follow_up_evidence
            ):
                raise ValueError("next-check requires declared delivery follow-up evidence.")
        elif self.predicate.delivery_follow_up_evidence:
            raise ValueError("only next-check may declare delivery follow-up evidence.")
        if (self.saturation_policy is None) == (self.id is InsightSlotId.FREQUENCY_SATURATION):
            raise ValueError("frequency-saturation, and only it, declares a saturation policy.")
        if (self.shift_policy is None) == (self.id is InsightSlotId.RETENTION_SHIFT):
            raise ValueError("retention-shift, and only it, declares a shift policy.")
        if (self.over_index_policy is None) == (self.id is InsightSlotId.SEGMENT_OVER_INDEX):
            raise ValueError("segment-over-index, and only it, declares an over-index policy.")
        calibration_kinds = tuple(case.kind for case in self.calibration_cases)
        if set(calibration_kinds) != set(InsightCalibrationKind) or len(
            set(calibration_kinds)
        ) != len(calibration_kinds):
            raise ValueError(
                "insight contract requires each fictional calibration case exactly once."
            )
        if self.structured_output_schema.schema_id not in _NARRATION_SCHEMA_IDS:
            raise ValueError("an insight card contract must declare a narration response shape.")
        if self.id in DETERMINISTIC_SLOT_IDS:
            if self.initial_fictional_prompt is not None:
                raise ValueError("a deterministic contract cannot declare a prompt.")
            if self.deep is not None:
                raise ValueError("a deterministic contract cannot declare a deep tier.")
            if self.deterministic_findings != _DETERMINISTIC_FINDING_KINDS[self.id]:
                raise ValueError(
                    "a deterministic contract must declare exactly its slot's finding kinds."
                )
            if self.next_step_kinds:
                raise ValueError("a deterministic contract cannot offer next steps.")
        else:
            if not self.initial_fictional_prompt or not self.initial_fictional_prompt.strip():
                raise ValueError("a model-authored insight contract requires a fictional prompt.")
            if self.deep is None:
                raise ValueError("a model-authored insight contract must declare its deep tier.")
            if len(set(self.next_step_kinds)) != len(self.next_step_kinds):
                raise ValueError("insight contract next-step kinds must be unique.")
            if InsightNextStepKind.OPEN_PAGE in self.next_step_kinds:
                raise ValueError("open_page steps are declared by their page, not by a card.")


MAX_INSIGHT_SLOT_TEXT_BYTES = 4_096
MAX_INSIGHT_EVIDENCE_BYTES = 2_048


def _canonical_json_string_bytes(value: str) -> int:
    """Count one string exactly as canonical artifact JSON will encode it."""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class InsightNextStepKind(StrEnum):
    """A view the product already serves that a card may point to (FR-11; ADR-122).

    ``open_product_detail`` (ADR-125 decision 5) is written only by the serve path, for the
    product the baked Largest movers card names first; no prompt offers it.
    """

    OPEN_PAGE = "open_page"
    OPEN_PRESET = "open_preset"
    OPEN_CAMPAIGN_DETAIL = "open_campaign_detail"
    OPEN_METRIC_FOCUS = "open_metric_focus"
    OPEN_EVIDENCE_STATUS = "open_evidence_status"
    ASK_ASSISTANT = "ask_assistant"
    OPEN_PRODUCT_DETAIL = "open_product_detail"


MAX_NEXT_STEP_REASON_CHARS: Final = 240
"""The structural bound on a next step's reason (the tool schema states it too)."""
# ``NextStep.v2`` (ADR-122): each kind's one target shape. The grammar shipped with a
# render-format bump (Campaigns 9, Retail health 7), so no cached slot keeps a v1 step.
_STEP_TARGET_TYPES: Final[dict[InsightNextStepKind, tuple[type, ...]]] = {
    InsightNextStepKind.OPEN_PAGE: (PageTarget,),
    InsightNextStepKind.OPEN_PRESET: (PresetTarget,),
    InsightNextStepKind.OPEN_CAMPAIGN_DETAIL: (CampaignKeyTarget, CampaignRefTarget),
    InsightNextStepKind.OPEN_METRIC_FOCUS: (MetricFocusTarget,),
    InsightNextStepKind.OPEN_EVIDENCE_STATUS: (),
    InsightNextStepKind.ASK_ASSISTANT: (AssistantTarget,),
    InsightNextStepKind.OPEN_PRODUCT_DETAIL: (ProductRefTarget,),
}


def _require_step_target(kind: InsightNextStepKind, target: NextStepTarget | None) -> None:
    types = _STEP_TARGET_TYPES[kind]
    if not types:
        if target is not None:
            raise ValueError("this next-step kind takes no target.")
    elif not isinstance(target, types):
        raise ValueError("this next-step kind requires its own target shape.")


@dataclass(frozen=True, slots=True)
class InsightNextStepOption:
    """One served step a card's model may choose by id; the server built every field.

    A campaign option holds its campaign key: options travel in continuation messages and
    the chosen step is cached, so a per-serve capability can never be in one.
    """

    step_id: str
    kind: InsightNextStepKind
    target: NextStepTarget | None
    label: str

    def __post_init__(self) -> None:
        if not self.step_id.strip() or not self.label.strip():
            raise ValueError("a next-step option requires an id and a label.")
        _require_step_target(self.kind, self.target)
        if isinstance(self.target, CampaignRefTarget):
            raise ValueError("a next-step option holds a campaign key, never a capability.")


@dataclass(frozen=True, slots=True)
class InsightNextStep:
    """The step a filled card offers: a served option plus the model's one-line reason.

    The checks are structural only (ADR-082; design 4.12, R5-2): whether the reason is
    supported is owned by calibration and evaluation, never by a text check here. A cached
    step's campaign target holds the key; the served one holds the capability (ADR-122).
    """

    kind: InsightNextStepKind
    target: NextStepTarget | None
    label: str
    reason: str

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise ValueError("a next step requires its served label.")
        if not self.reason.strip() or len(self.reason) > MAX_NEXT_STEP_REASON_CHARS:
            raise ValueError(
                f"a next-step reason must be nonblank and at most "
                f"{MAX_NEXT_STEP_REASON_CHARS} characters."
            )
        _require_step_target(self.kind, self.target)


def resolve_next_step(
    options: tuple[InsightNextStepOption, ...], step_id: str, reason: str
) -> InsightNextStep:
    """Resolve the model's chosen id against this request's served options, or refuse."""
    chosen = next((option for option in options if option.step_id == step_id), None)
    if chosen is None:
        raise ValueError("the chosen next step is not one of this card's served options.")
    return InsightNextStep(chosen.kind, chosen.target, chosen.label, reason)


def next_step_payload(step: InsightNextStep, *, cached: bool = False) -> dict[str, object]:
    """Encode one ``NextStep.v2``: kind, label, reason and the kind's object target.

    The wire form (the default) refuses a campaign key; ``cached=True`` writes the cache
    form, which refuses a capability (ADR-122 decision 4).
    """
    return {
        "kind": step.kind.value,
        "label": step.label,
        "reason": step.reason,
        "target": next_step_target_payload(step.target, cached=cached),
    }


def next_step_from_payload(value: object, *, cached: bool = False) -> InsightNextStep:
    """Strictly decode one next step; any missing or extra key, here or in its target, fails."""
    if not isinstance(value, dict):
        raise ValueError("a next step must be an object.")
    item = cast("dict[str, object]", value)
    if frozenset(item) != frozenset({"kind", "label", "reason", "target"}):
        raise ValueError("next-step fields are not exact.")
    kind, label, reason = item["kind"], item["label"], item["reason"]
    if not isinstance(kind, str) or not isinstance(label, str) or not isinstance(reason, str):
        raise ValueError("next-step fields are malformed.")
    step_kind = InsightNextStepKind(kind)
    target = next_step_target_from_payload(step_kind.value, item["target"], cached=cached)
    return InsightNextStep(step_kind, target, label, reason)


@dataclass(frozen=True, slots=True)
class NextStepServeContext:
    """What one response offers a cached step: its minted capabilities and served filters.

    ``campaign_refs`` maps a campaign key to the capability this response issued for that
    campaign's row. ``offered_filters`` are the channel filters this workspace serves.
    """

    campaign_refs: Mapping[str, str] = field(default_factory=dict[str, str])
    offered_filters: frozenset[str] = frozenset()


NO_SERVED_CAPABILITIES: Final = NextStepServeContext()
"""A response that mints no campaign capability and serves no channel filter."""


def served_next_step(
    step: InsightNextStep, context: NextStepServeContext
) -> InsightNextStep | None:
    """Map a cached step to its served form, or drop it so no dead chip renders.

    A campaign step whose campaign has no capability in this response is dropped, and so is
    any step whose ``filter_id`` this workspace doesn't offer (ADR-122 decisions 4 and 5).
    """
    target = step.target
    if isinstance(target, CampaignRefTarget):
        raise ValueError("a cached next step cannot already hold a capability.")
    if isinstance(target, CampaignKeyTarget):
        reference = context.campaign_refs.get(target.campaign_key)
        if reference is None:
            return None
        return replace(step, target=CampaignRefTarget(target.page_id, reference))
    filter_id = getattr(target, "filter_id", None)
    if filter_id is not None and filter_id not in context.offered_filters:
        return None
    return step


def served_insight_slots(
    slots: tuple[InsightSlot, ...], context: NextStepServeContext
) -> tuple[InsightSlot, ...]:
    """Serve every slot's next step through :func:`served_next_step`; text never changes."""
    return tuple(
        slot
        if slot.next_step is None
        else replace(slot, next_step=served_next_step(slot.next_step, context))
        for slot in slots
    )


@dataclass(frozen=True, slots=True)
class InsightSlot:
    """A delivered declared slot; only filled slots carry model-authored text.

    A non-filled slot names its absence class and a governed customer-legible reason
    so the page never renders an unexplained blank. Both stay ``None`` on filled
    slots, and may be ``None`` on slots decoded from artifacts baked before the
    taxonomy existed — the serve path overlays them for every slot it composes.
    """

    id: InsightSlotId
    output_class: InsightOutputClass
    state: InsightSlotState
    text: str | None
    evidence_line: str | None
    artifact_revision: str
    prompt_version: str
    absence_class: InsightAbsenceClass | None = None
    absence_reason: str | None = None
    next_step: InsightNextStep | None = None
    """FR-11: present only on a filled card whose gated prompt offered one (design 4.12)."""

    def __post_init__(self) -> None:
        if self.output_class is not _SLOT_OUTPUT_CLASSES[self.id]:
            raise ValueError("insight slot output class contradicts its declared identity.")
        if not self.artifact_revision.strip() or not self.prompt_version.strip():
            raise ValueError("insight slots require artifact and prompt provenance stamps.")
        filled = self.state is InsightSlotState.FILLED
        if (filled and (self.text is None or self.evidence_line is None)) or (
            not filled and (self.text is not None or self.evidence_line is not None)
        ):
            raise ValueError(
                "filled insight slots require text and evidence; absent slots cannot carry either."
            )
        if filled and (self.absence_class is not None or self.absence_reason is not None):
            raise ValueError("filled insight slots cannot declare an absence.")
        if self.next_step is not None and not filled:
            raise ValueError("only a filled insight slot can offer a next step.")
        if self.absence_reason is not None and self.absence_class is None:
            raise ValueError("an insight absence reason requires its absence class.")
        if self.absence_reason is not None and (
            not self.absence_reason.strip()
            or _canonical_json_string_bytes(self.absence_reason) > MAX_INSIGHT_EVIDENCE_BYTES
        ):
            raise ValueError("insight absence reason must be nonblank and within its byte bound.")
        if self.text is not None and (
            not self.text.strip()
            or _canonical_json_string_bytes(self.text) > MAX_INSIGHT_SLOT_TEXT_BYTES
        ):
            raise ValueError("insight slot text must be nonblank and within its byte bound.")
        if self.evidence_line is not None and (
            not self.evidence_line.strip()
            or _canonical_json_string_bytes(self.evidence_line) > MAX_INSIGHT_EVIDENCE_BYTES
        ):
            raise ValueError("insight evidence line must be nonblank and within its byte bound.")


def _slot_class(slot_id: InsightSlotId) -> InsightOutputClass:
    return _SLOT_OUTPUT_CLASSES[slot_id]


GENERATION_PENDING_REASON = (
    "This insight could not be generated just now; it retries automatically and appears "
    "on a later view."
)
OUT_OF_SLICE_REASON = "Insights are not generated for this view."

INSIGHT_MODEL_SILENCE_REASON = "Nothing here stands out beyond what the chart already shows."
"""The served reason when Gate-1 admitted findings and the model chose silence."""

_QUIET_DEFAULTS: Final[dict[PredicateQuietReason, str]] = {
    PredicateQuietReason.BELOW_THRESHOLD: "No change in this view is large enough to call out.",
    PredicateQuietReason.MISSING_INPUT: "This view doesn't have the data this insight needs.",
    PredicateQuietReason.MISSING_KIND: (
        "This view doesn't have the comparison this insight needs."
    ),
    PredicateQuietReason.NEAR_ZERO_PRIOR: (
        "The earlier period had too little activity to compare against."
    ),
}
_DIVERGENCE_QUIET = (
    "No campaign moved against the portfolio's direction enough to call out in this view."
)
_EXPOSURE_QUIET = "No campaign holds a large enough share of spend in this view to call out."
_FUNNEL_QUIET = "No step in this campaign's funnel stands out in this view."
_QUIET_OVERRIDES: Final[dict[tuple[InsightSlotId, PredicateQuietReason], str]] = {
    (InsightSlotId.DIVERGENCE, PredicateQuietReason.MISSING_KIND): _DIVERGENCE_QUIET,
    (InsightSlotId.DIVERGENCE, PredicateQuietReason.BELOW_THRESHOLD): _DIVERGENCE_QUIET,
    (InsightSlotId.EXPOSURE, PredicateQuietReason.MISSING_KIND): _EXPOSURE_QUIET,
    (InsightSlotId.EXPOSURE, PredicateQuietReason.BELOW_THRESHOLD): _EXPOSURE_QUIET,
    (InsightSlotId.FUNNEL, PredicateQuietReason.MISSING_KIND): _FUNNEL_QUIET,
    (InsightSlotId.FUNNEL, PredicateQuietReason.BELOW_THRESHOLD): _FUNNEL_QUIET,
    (InsightSlotId.NEXT_CHECK, PredicateQuietReason.MISSING_INPUT): (
        "There's no follow-up check available for this view."
    ),
    (InsightSlotId.TREND_SHAPE, PredicateQuietReason.BELOW_THRESHOLD): (
        "The daily series in this view has no pattern large enough to call out."
    ),
}
INSIGHT_QUIET_COPY: Final[Mapping[tuple[InsightSlotId, PredicateQuietReason], str]] = {
    **{
        (slot_id, reason): text
        for slot_id in InsightSlotId
        if slot_id is not InsightSlotId.MOVERS
        for reason, text in _QUIET_DEFAULTS.items()
    },
    **_QUIET_OVERRIDES,
}
"""One customer string per (slot, quiet reason); the contracts keep `silence_condition`
as their governance statement (design §4.10, ADR-068 2026-10-05 amendment)."""

QUIET_COPY_UNDECLARED = ErrorCode("INSIGHT", 15)
"""A quiet slot's (slot, reason) has no declared customer string."""


def insight_quiet_reason(slot_id: InsightSlotId, reason: PredicateQuietReason) -> str:
    """Return the served customer string for one quiet slot, failing closed when undeclared."""
    text = INSIGHT_QUIET_COPY.get((slot_id, reason))
    if text is None:
        raise InvariantError(
            QUIET_COPY_UNDECLARED,
            "A quiet insight slot has no declared customer reason.",
            context={"slot": slot_id.value, "reason": reason.value},
        )
    return text


_EVIDENCE_INPUT_LABELS: Final[dict[InsightEvidenceInput, str]] = {
    InsightEvidenceInput.PORTFOLIO_TOTALS: "portfolio totals",
    InsightEvidenceInput.PORTFOLIO_DIRECTION: "a portfolio-level direction",
    InsightEvidenceInput.CAMPAIGN_COMPARISONS: "campaign comparisons",
    InsightEvidenceInput.DETAIL_COMPARISONS: "prior-period comparisons",
    InsightEvidenceInput.PURCHASE_SERIES: "a daily purchase series",
    InsightEvidenceInput.DELIVERY_METRICS: "delivery funnel metrics",
    InsightEvidenceInput.DELIVERY_FOLLOW_UP: "a governed delivery follow-up read",
    InsightEvidenceInput.FOCUSED_METRIC: "the focused metric beside its comparator",
    InsightEvidenceInput.RETAIL_TOTALS: "retail totals",
    InsightEvidenceInput.RETAIL_RECONCILIATION: "ordered-versus-shipped reconciliation",
    InsightEvidenceInput.RETAIL_PRODUCT_COMPARISONS: "product comparisons",
    InsightEvidenceInput.NETWORK_CHANNEL_COMPARISONS: "channel comparisons inside one network",
    InsightEvidenceInput.EXPOSURE_GROUP_CELLS: "every exposure group on Amazon account IDs",
    InsightEvidenceInput.FREQUENCY_BUCKET_CELLS: "every frequency bucket on Amazon account IDs",
    InsightEvidenceInput.COHORT_CELLS: "two cohorts of one acquisition group in one vintage",
    InsightEvidenceInput.SEGMENT_PROFILE_ROWS: "every segment of one buyer-profile delivery",
}


def ineligible_reason(missing: tuple[InsightEvidenceInput, ...]) -> str:
    """Compose the governed customer-legible sentence naming the missing evidence."""
    if not missing:
        raise ValueError("an ineligible reason requires at least one missing evidence input.")
    labels = ", ".join(_EVIDENCE_INPUT_LABELS[item] for item in missing)
    return f"This insight requires {labels}, which is not available for this view and window."


_UNAVAILABLE_ABSENCE_REASONS: Final[dict[InsightAbsenceClass, str]] = {
    InsightAbsenceClass.GENERATION_PENDING: GENERATION_PENDING_REASON,
    InsightAbsenceClass.OUT_OF_SLICE: OUT_OF_SLICE_REASON,
}


def unavailable_insight_slots(
    slot_ids: tuple[InsightSlotId, ...],
    *,
    artifact_revision: str = "unpublished",
    absence_class: InsightAbsenceClass = InsightAbsenceClass.OUT_OF_SLICE,
) -> tuple[InsightSlot, ...]:
    """Declare every slot a composition does not fill, with its declared absence.

    Only the two not-generated classes are legal here; the governed customer reason
    is derived from the class so a caller can never pair them inconsistently.
    """
    absence_reason = _UNAVAILABLE_ABSENCE_REASONS.get(absence_class)
    if absence_reason is None:
        raise ValueError("unavailable insight slots carry a not-generated absence class.")
    return tuple(
        InsightSlot(
            id=slot_id,
            output_class=_slot_class(slot_id),
            state=InsightSlotState.UNAVAILABLE,
            text=None,
            evidence_line=None,
            artifact_revision=artifact_revision,
            prompt_version=(
                "deterministic-v1" if slot_id in DETERMINISTIC_SLOT_IDS else "not-generated"
            ),
            absence_class=absence_class,
            absence_reason=absence_reason,
        )
        for slot_id in slot_ids
    )


def gate_refused_insight_slots(
    slot_ids: tuple[InsightSlotId, ...],
    *,
    artifact_revision: str,
    reasons: Mapping[InsightSlotId, str],
) -> tuple[InsightSlot, ...]:
    """Declare every slot whose Gate-1 predicate admitted nothing: silent, no model invoked.

    The deterministic matrix mandates that a declared slot with no surviving findings renders
    silent rather than unavailable — silence is the designed answer, not a delivery failure.
    The prompt stamp records that the decision was deterministic and predates any prompt read,
    and the governed reason is the contract's own silence condition.
    """
    return tuple(
        InsightSlot(
            id=slot_id,
            output_class=_slot_class(slot_id),
            state=InsightSlotState.SILENT,
            text=None,
            evidence_line=None,
            artifact_revision=artifact_revision,
            prompt_version="deterministic-gate1",
            absence_class=InsightAbsenceClass.EVALUATED_QUIET,
            absence_reason=reasons[slot_id],
        )
        for slot_id in slot_ids
    )


def ineligible_insight_slots(
    slot_ids: tuple[InsightSlotId, ...],
    *,
    artifact_revision: str,
    missing_by_slot: Mapping[InsightSlotId, tuple[InsightEvidenceInput, ...]],
) -> tuple[InsightSlot, ...]:
    """Declare every slot that could not be evaluated: a required evidence input is absent.

    Ineligibility is a designed absence, never a delivery failure, so the state stays
    silent — but its class and reason let the page say exactly which evidence is missing,
    which is how a customer distinguishes an expected gap from a flaw.
    """
    return tuple(
        InsightSlot(
            id=slot_id,
            output_class=_slot_class(slot_id),
            state=InsightSlotState.SILENT,
            text=None,
            evidence_line=None,
            artifact_revision=artifact_revision,
            prompt_version="deterministic-gate1",
            absence_class=InsightAbsenceClass.INELIGIBLE,
            absence_reason=ineligible_reason(missing_by_slot[slot_id]),
        )
        for slot_id in slot_ids
    )


def default_portfolio_insights() -> tuple[InsightSlot, ...]:
    """Return the fixed portfolio roster in contract order."""
    return unavailable_insight_slots(_PORTFOLIO_SLOT_IDS)


def default_detail_insights() -> tuple[InsightSlot, ...]:
    """Return the fixed campaign-detail roster in contract order."""
    return unavailable_insight_slots(_DETAIL_SLOT_IDS)


def require_insight_roster(
    slots: tuple[InsightSlot, ...], expected: tuple[InsightSlotId, ...]
) -> None:
    """Refuse omitted, reordered, duplicated, or undeclared delivered slots."""
    if tuple(slot.id for slot in slots) != expected:
        raise ValueError("insight slots must exactly match the declared roster and order.")


def insight_slots_payload(slots: tuple[InsightSlot, ...]) -> list[dict[str, object]]:
    """Encode the one shared insight slot schema used by both Slice-B artifacts.

    The absence pair is present exactly when an absence is declared: a slot with no
    declared absence (every filled slot, and slots decoded from artifacts baked
    before the taxonomy) carries the seven base fields only. This one shape rule is
    what lets artifact codecs re-attest exact readback over retained bodies.
    """
    return [
        {
            **(
                {}
                if slot.absence_class is None
                else {
                    "absence_class": slot.absence_class.value,
                    "absence_reason": slot.absence_reason,
                }
            ),
            **({} if slot.next_step is None else {"next_step": next_step_payload(slot.next_step)}),
            "artifact_revision": slot.artifact_revision,
            "evidence_line": slot.evidence_line,
            "id": slot.id.value,
            "output_class": slot.output_class.value,
            "prompt_version": slot.prompt_version,
            "state": slot.state.value,
            "text": slot.text,
        }
        for slot in slots
    ]


_SLOT_BASE_FIELDS = frozenset(
    {
        "artifact_revision",
        "evidence_line",
        "id",
        "output_class",
        "prompt_version",
        "state",
        "text",
    }
)
_SLOT_ABSENCE_FIELDS = _SLOT_BASE_FIELDS | {"absence_class", "absence_reason"}
_SLOT_STEP_FIELDS = _SLOT_BASE_FIELDS | {"next_step"}
"""A filled card with a next step: the key is present only when a step is (exact readback)."""


def insight_slots_from_payload(
    value: object, *, expected: tuple[InsightSlotId, ...]
) -> tuple[InsightSlot, ...]:
    """Strictly decode shared slots before each artifact re-attests its full payload.

    One closed shape family: the absence pair is present together (a declared
    absence) or absent together (no declared absence). Anything else is refused.
    """
    if not isinstance(value, list):
        raise ValueError("insight slots must be a list.")
    slots: list[InsightSlot] = []
    for raw in cast("list[object]", value):
        if not isinstance(raw, dict):
            raise ValueError("insight slot fields are not exact.")
        item = cast("dict[str, object]", raw)
        names = frozenset(item)
        if names not in {_SLOT_BASE_FIELDS, _SLOT_ABSENCE_FIELDS, _SLOT_STEP_FIELDS}:
            raise ValueError("insight slot fields are not exact.")
        try:
            slot_id = InsightSlotId(_nonblank_text(item["id"], "insight slot id"))
            output_class = InsightOutputClass(
                _nonblank_text(item["output_class"], "insight output class")
            )
            state = InsightSlotState(_nonblank_text(item["state"], "insight slot state"))
            # The pair present means an absence is declared: a null class here would
            # re-encode as the base shape and break exact readback, so it is refused.
            absence_class = (
                InsightAbsenceClass(_nonblank_text(item["absence_class"], "insight absence class"))
                if "absence_class" in item
                else None
            )
        except ValueError:
            raise ValueError("insight slot enum is unsupported.") from None
        text = _optional_text(item["text"], "insight slot text")
        evidence_line = _optional_text(item["evidence_line"], "insight evidence line")
        slots.append(
            InsightSlot(
                slot_id,
                output_class,
                state,
                text,
                evidence_line,
                _nonblank_text(item["artifact_revision"], "insight artifact revision"),
                _nonblank_text(item["prompt_version"], "insight prompt version"),
                absence_class=absence_class,
                absence_reason=_optional_text(item.get("absence_reason"), "insight absence reason"),
                next_step=(
                    next_step_from_payload(item["next_step"]) if "next_step" in item else None
                ),
            )
        )
    result = tuple(slots)
    require_insight_roster(result, expected)
    return result


def _nonblank_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonblank text.")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _nonblank_text(value, label)


@dataclass(frozen=True, slots=True)
class InsightFindingOperand:
    """A server-owned named operand for deterministic insight arithmetic."""

    name: str
    current: Decimal
    prior: Decimal | None = None

    def __post_init__(self) -> None:
        if (
            not self.name.strip()
            or not self.current.is_finite()
            or (self.prior is not None and not self.prior.is_finite())
        ):
            raise ValueError("insight finding operands require finite named values.")


# Division-derived findings are display-governed, never Decimal-native: two decimal
# places is the precision the pages render and the model may cite (ADR-068 amendment).
PERCENTAGE_FINDING_QUANTUM = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class DeterministicFindingRequest:
    """Typed operands for one slot; callers select only findings declared in its contract."""

    kind: InsightFindingKind
    operands: tuple[InsightFindingOperand, ...]
    total: Decimal | None = None

    def __post_init__(self) -> None:
        names = tuple(item.name for item in self.operands)
        if not self.operands or len(set(names)) != len(names):
            raise ValueError("deterministic insight findings require uniquely named operands.")
        if self.total is not None and (not self.total.is_finite() or self.total <= 0):
            raise ValueError("insight finding totals must be finite and positive.")
        if self.kind is InsightFindingKind.SHARE_OF_TOTAL and self.total is None:
            raise ValueError("share-of-total findings require an explicit positive total.")
        if self.kind is InsightFindingKind.PERCENTAGE_CHANGE and any(
            item.prior in {None, Decimal(0)} for item in self.operands
        ):
            raise ValueError("percentage-change findings require nonzero prior operands.")
        if self.kind in {
            InsightFindingKind.ABSOLUTE_CHANGE,
            InsightFindingKind.PERCENTAGE_CHANGE,
        } and any(item.prior is None for item in self.operands):
            raise ValueError("change findings require a prior operand.")


@dataclass(frozen=True, slots=True)
class DeterministicFinding:
    """One exact output supplied to a structured narration call."""

    name: str
    value: Decimal


@dataclass(frozen=True, slots=True)
class InsightContextEntry:
    """One server-rendered display fact supplied beside the numeric findings.

    Context widens a slot's input to the substance the ADR-068 context-depth amendment
    mandates — windows, entity display labels, relevant artifact sections — without adding
    citable figures: allowed figures derive only from deterministic findings, so a context
    value can inform wording but can never be verified as an observed citation.
    """

    name: str
    value: str

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.value.strip():
            raise ValueError("insight context entries require nonblank names and values.")


def calculate_deterministic_findings(
    request: DeterministicFindingRequest,
) -> tuple[DeterministicFinding, ...]:
    """Calculate changes, shares, or mover rankings without model or adapter dependencies.

    Change findings are exact differences and keep their operands' served display scale.
    Division-derived findings (percentage change, share of total) are quantized to two
    decimal places at this seam: exact-decimal division yields analytically meaningless
    28-significant-digit rates whose verbatim citation contract multiplied payload and
    model-output size until structured output truncated (ADR-068 precision amendment).
    """
    if request.kind is InsightFindingKind.ABSOLUTE_CHANGE:
        return tuple(
            DeterministicFinding(item.name, item.current - cast("Decimal", item.prior))
            for item in request.operands
        )
    if request.kind is InsightFindingKind.PERCENTAGE_CHANGE:
        return tuple(
            DeterministicFinding(
                item.name,
                (
                    ((item.current - cast("Decimal", item.prior)) / cast("Decimal", item.prior))
                    * Decimal(100)
                ).quantize(PERCENTAGE_FINDING_QUANTUM),
            )
            for item in request.operands
        )
    if request.kind is InsightFindingKind.SHARE_OF_TOTAL:
        total = cast("Decimal", request.total)
        return tuple(
            DeterministicFinding(
                item.name,
                ((item.current / total) * Decimal(100)).quantize(PERCENTAGE_FINDING_QUANTUM),
            )
            for item in request.operands
        )
    ranked = sorted(
        request.operands,
        key=lambda item: (-abs(item.current - (item.prior or Decimal(0))), item.name),
    )
    return tuple(
        DeterministicFinding(item.name, item.current - (item.prior or Decimal(0)))
        for item in ranked
    )


@dataclass(frozen=True, slots=True)
class PredicateFinding:
    """A deterministic finding with its calculation kind and server-owned evidence inputs.

    ``prior_magnitude`` carries the operand prior a percentage finding was derived
    against — the only place the prior still exists once findings are calculated —
    so the contract-owned near-zero-prior guard can be enforced at selection
    (ADR-076 decision 5). ``None`` means the finding has no prior operand (shares,
    rankings) or the composer predates the guard; such findings are never suppressed.
    """

    finding: DeterministicFinding
    kind: InsightFindingKind
    evidence_inputs: tuple[InsightEvidenceInput, ...]
    unit: FindingUnit
    basis: OperandBasis
    metric_id: str
    prior_scale: MetricScale | None = None
    """The metric's scale; required on a percentage finding and absent otherwise."""
    prior_magnitude: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.evidence_inputs or len(set(self.evidence_inputs)) != len(self.evidence_inputs):
            raise ValueError("predicate findings require unique nonempty evidence inputs.")
        if self.prior_magnitude is not None and not self.prior_magnitude.is_finite():
            raise ValueError("predicate finding prior magnitudes must be finite.")
        if not self.metric_id.strip():
            raise ValueError("predicate findings name their metric.")
        if (self.kind is InsightFindingKind.PERCENTAGE_CHANGE) != (self.prior_scale is not None):
            raise ValueError("a prior scale is declared exactly on a percentage finding.")


@dataclass(frozen=True, slots=True)
class InsightEvidenceBundle:
    """The complete server-owned evidence offered to one executable slot predicate."""

    available_evidence_inputs: tuple[InsightEvidenceInput, ...]
    findings: tuple[PredicateFinding, ...]
    available_delivery_follow_up_evidence: tuple[DeliveryFollowUpEvidence, ...] = ()

    def __post_init__(self) -> None:
        if len(set(self.available_evidence_inputs)) != len(self.available_evidence_inputs):
            raise ValueError("available insight evidence inputs must be unique.")
        if len(set(self.available_delivery_follow_up_evidence)) != len(
            self.available_delivery_follow_up_evidence
        ):
            raise ValueError("available delivery follow-up evidence must be unique.")


@dataclass(frozen=True, slots=True)
class SlotPredicateEvaluation:
    """One declared slot's classified predicate outcome under its offered evidence.

    Exactly one of three shapes: admitted (``findings`` nonempty), ineligible
    (``missing_evidence`` names every absent required input), or evaluated-quiet
    (both empty: the evidence was complete and the predicate admitted nothing).
    """

    findings: tuple[DeterministicFinding, ...]
    missing_evidence: tuple[InsightEvidenceInput, ...]
    quiet_reason: PredicateQuietReason | None = None
    """Why an evaluated-quiet slot admitted nothing; set exactly on that shape."""

    def __post_init__(self) -> None:
        if self.findings and self.missing_evidence:
            raise ValueError(
                "a slot predicate evaluation cannot both admit findings and lack evidence."
            )
        if (self.quiet_reason is not None) != (not self.findings and not self.missing_evidence):
            raise ValueError("a quiet reason is declared exactly on an evaluated-quiet slot.")


def evaluate_slot_predicate(
    contract: InsightContract,
    evidence: InsightEvidenceBundle | None,
    role: InsightHorizonRole,
) -> SlotPredicateEvaluation:
    """Classify one slot: admitted findings, missing required evidence, or quiet.

    Missing evidence and an evaluated-quiet predicate are different declared
    outcomes (declared-outcomes rule): the first means the slot could not be
    evaluated for this subject and window, the second that it was evaluated in
    full and admitted nothing, with its quiet reason. Selection is horizon-aware
    (ADR-076 decision 5): the role picks the declared threshold.
    """
    predicate = contract.predicate
    if evidence is None:
        return SlotPredicateEvaluation((), predicate.required_evidence_inputs)
    missing = tuple(
        item
        for item in predicate.required_evidence_inputs
        if item not in evidence.available_evidence_inputs
    )
    if missing:
        return SlotPredicateEvaluation((), missing)
    if not set(predicate.delivery_follow_up_evidence).issubset(
        evidence.available_delivery_follow_up_evidence
    ):
        return SlotPredicateEvaluation((), (InsightEvidenceInput.DELIVERY_FOLLOW_UP,))
    admission = predicate_admission(contract, evidence, role)
    return SlotPredicateEvaluation(admission.findings, (), admission.quiet_reason)


def finding_magnitude(item: PredicateFinding) -> Decimal:
    """Return what a threshold is compared with: rates in percentage points."""
    value = abs(item.finding.value)
    return value * 100 if item.unit is FindingUnit.RATE_PP else value


def _near_zero_prior_suppressed(item: PredicateFinding, materiality: InsightMateriality) -> bool:
    """Suppress a percentage finding derived against a near-zero prior (ADR-076 §5).

    A percentage against a vanishing prior is a degenerate rate that must never reach the
    model. The bound is the prior's own scale; a rate prior is compared in percentage points.
    """
    if item.kind is not InsightFindingKind.PERCENTAGE_CHANGE or item.prior_magnitude is None:
        return False
    scale = cast("MetricScale", item.prior_scale)
    prior = abs(item.prior_magnitude)
    if scale is MetricScale.RATE_PP:
        prior *= 100
    return prior <= materiality.prior_bound_for(scale, item.metric_id)


def finding_is_material(
    materiality: InsightMateriality, item: PredicateFinding, role: InsightHorizonRole
) -> bool:
    """Whether one finding clears its declared threshold and is not off a near-zero prior."""
    return finding_magnitude(item) >= materiality.threshold_for(
        role, item.basis, unit=item.unit, metric_id=item.metric_id
    ) and not _near_zero_prior_suppressed(item, materiality)


@dataclass(frozen=True, slots=True)
class PredicateAdmission:
    """What Gate-1 admitted, or why it admitted nothing: exactly one is present."""

    findings: tuple[DeterministicFinding, ...]
    quiet_reason: PredicateQuietReason | None

    def __post_init__(self) -> None:
        if bool(self.findings) == (self.quiet_reason is not None):
            raise ValueError("a predicate admission carries findings or a quiet reason.")


def _kind_quiet_reason(
    materiality: InsightMateriality, items: list[PredicateFinding], role: InsightHorizonRole
) -> PredicateQuietReason | None:
    """Why one required kind has no material finding, or None when it has one."""
    if not items:
        return PredicateQuietReason.MISSING_KIND
    if any(finding_is_material(materiality, item, role) for item in items):
        return None
    removed_only_by_bound = all(
        _near_zero_prior_suppressed(item, materiality)
        and finding_magnitude(item)
        >= materiality.threshold_for(role, item.basis, unit=item.unit, metric_id=item.metric_id)
        for item in items
    )
    return (
        PredicateQuietReason.NEAR_ZERO_PRIOR
        if removed_only_by_bound
        else PredicateQuietReason.BELOW_THRESHOLD
    )


def predicate_admission(
    contract: InsightContract,
    evidence: InsightEvidenceBundle,
    role: InsightHorizonRole,
) -> PredicateAdmission:
    """Return the material findings, or the first quiet reason in decision order.

    Materiality is horizon-, basis- and unit-aware (ADR-076 §5): each finding of a required
    kind meets the threshold its basis, unit and metric declare, and a percentage finding
    off a near-zero prior in its own scale is suppressed server-side. The quiet reason is
    decided in order: missing input, missing kind, near-zero prior, below threshold.
    """
    predicate = contract.predicate
    if not set(predicate.required_evidence_inputs).issubset(
        evidence.available_evidence_inputs
    ) or not set(predicate.delivery_follow_up_evidence).issubset(
        evidence.available_delivery_follow_up_evidence
    ):
        return PredicateAdmission((), PredicateQuietReason.MISSING_INPUT)
    materiality = predicate.materiality
    by_kind = {
        kind: [item for item in evidence.findings if item.kind is kind]
        for kind in predicate.required_finding_kinds
    }
    reasons = {_kind_quiet_reason(materiality, items, role) for items in by_kind.values()}
    for reason in (
        PredicateQuietReason.MISSING_KIND,
        PredicateQuietReason.NEAR_ZERO_PRIOR,
        PredicateQuietReason.BELOW_THRESHOLD,
    ):
        if reason in reasons:
            return PredicateAdmission((), reason)
    material = tuple(
        item
        for item in evidence.findings
        if item.kind in predicate.required_finding_kinds
        and finding_is_material(materiality, item, role)
    )
    counts = Counter(item.finding.name for item in material)
    return PredicateAdmission(
        tuple(
            item.finding
            if counts[item.finding.name] == 1
            else DeterministicFinding(f"{item.kind.value}: {item.finding.name}", item.finding.value)
            for item in material
        ),
        None,
    )


def admitted_predicate_findings(
    contract: InsightContract,
    evidence: InsightEvidenceBundle,
    role: InsightHorizonRole,
) -> tuple[DeterministicFinding, ...]:
    """Return material findings only when every declared predicate requirement is met."""
    return predicate_admission(contract, evidence, role).findings


def decimal_figure(value: Decimal) -> str:
    """Render a finite deterministic decimal in the canonical form a model input carries."""
    try:
        normalized = value.normalize()
    except InvalidOperation:
        raise ValueError("deterministic figure must be finite.") from None
    if not normalized.is_finite():
        raise ValueError("deterministic figure must be finite.")
    return format(normalized, "f")


def validate_insight_contracts(contracts: tuple[InsightContract, ...]) -> tuple[str, ...]:
    """Validate the governed roster after each declaration has passed structural loading."""
    identifiers = tuple(contract.id for contract in contracts)
    expected = tuple(InsightSlotId)
    if identifiers == expected:
        return ()
    if len(set(identifiers)) != len(identifiers):
        return ("insight contract ids must be unique.",)
    if set(identifiers) != set(expected):
        return ("insight contracts must declare the exact command-center roster.",)
    return ("insight contracts must use canonical command-center roster order.",)


class InsightInputSource(Protocol):
    """The five fields a narration's model input is built from (assembly or generation)."""

    @property
    def purpose(self) -> str: ...
    @property
    def output_class(self) -> InsightOutputClass: ...
    @property
    def window_profile(self) -> InsightWindowProfile: ...
    @property
    def context(self) -> tuple[InsightContextEntry, ...]: ...
    @property
    def findings(self) -> tuple[DeterministicFinding, ...]: ...


def insight_input_document(source: InsightInputSource) -> dict[str, object]:
    """Return the model input every tier sends, minus the tier-owned schema fields.

    It is the one home of the narration input shape: the Bedrock adapter adds only
    ``schema`` and ``silence_allowed``, which the tier determines, so the cached
    identity and the payload cannot drift (ADR-076 decision 1, 2026-10-05).
    """
    window_profile = source.window_profile
    return {
        "purpose": source.purpose,
        "output_class": source.output_class.value,
        # The typed window profile every payload declares (ADR-076 decision 5).
        "window_profile": {
            "days": window_profile.days,
            "grain": window_profile.grain.value,
            "prior_start": window_profile.prior_start,
            "prior_end": window_profile.prior_end,
            "role": window_profile.role.value,
        },
        "context": [{"name": entry.name, "value": entry.value} for entry in source.context],
        # Canonical decimal rendering (decimal_figure): "12.45", never "12.4500".
        "findings": [
            {"name": finding.name, "value": decimal_figure(finding.value)}
            for finding in source.findings
        ],
    }


def canonical_input_json(document: Mapping[str, object]) -> str:
    """Serialize a model input document in the one canonical form the model receives."""
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def insight_input_digest(document: Mapping[str, object]) -> str:
    """Return the lowercase hex SHA-256 of an input document's canonical JSON."""
    return hashlib.sha256(canonical_input_json(document).encode("utf-8")).hexdigest()


def _is_input_digest(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


@dataclass(frozen=True, slots=True)
class InsightCacheKey:
    """One analytical-scope-scoped immutable derived-insight identity.

    ``tier`` is the ADR-076 decision 1 discriminator: the deep tier appends its own
    segment — mirroring the registry's POINTER / POINTER#deep pattern — carrying the
    deep prompt's own version in the PROMPT segment so deep activation invalidates
    exactly the deep tier.

    ``input_digest`` (2026-10-05 amendment) is the digest of the card's exact model
    input, so a changed finding or context entry is a new identity on every pointer.
    ``deep_input_revision`` is the declared revision of the deep-input code, required
    on the deep tier and refused on the card tier: the deep enrichment runs only on a
    miss, so its input cannot be digested into the key it is looked up by.
    """

    analytical_scope_id: str
    subject: str
    window_start: str
    window_end: str
    artifact_revision: str
    prompt_version: str
    slot_id: InsightSlotId
    input_digest: str
    tier: InsightPromptTier = InsightPromptTier.CARD
    deep_input_revision: str | None = None
    filter_id: str = "all"
    """The channel filter sibling the slot reads (ADR-127 decision 5), ``all`` on a page without
    filters: siblings of one revision share it, so each caches separately."""

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (
                self.analytical_scope_id,
                self.subject,
                self.window_start,
                self.window_end,
                self.artifact_revision,
                self.prompt_version,
                self.filter_id,
            )
        ):
            raise ValueError("insight cache identity fields must be nonblank.")
        if self.window_end < self.window_start:
            raise ValueError("insight cache window end must not precede its start.")
        if not _is_input_digest(self.input_digest):
            raise ValueError("insight cache input digest must be 64 lowercase hex characters.")
        deep = self.tier is InsightPromptTier.DEEP
        revision = self.deep_input_revision
        if deep and (revision is None or not revision.strip()):
            raise ValueError("a deep insight cache identity requires its deep-input revision.")
        if not deep and revision is not None:
            raise ValueError("a card insight cache identity cannot carry a deep-input revision.")

    @property
    def ownership_key(self) -> str:
        """Return the cache partition that prevents cross-scope reads."""
        return f"SCOPE#{self.analytical_scope_id}#SUBJECT#{self.subject}"

    @property
    def content_key(self) -> str:
        """Return the immutable revision, prompt-version and input cache sort key.

        Both tiers carry the card input digest; the deep tier appends its
        discriminator segment and its deep-input revision.
        """
        base = (
            f"WINDOW#{self.window_start}#{self.window_end}#REVISION#{self.artifact_revision}"
            f"#FILTER#{self.filter_id}"
            f"#PROMPT#{self.prompt_version}#SLOT#{self.slot_id.value}#INPUT#{self.input_digest}"
        )
        if self.tier is InsightPromptTier.CARD:
            return base
        return f"{base}#TIER#{self.tier.value}#DEEPINPUT#{self.deep_input_revision}"


@dataclass(frozen=True, slots=True)
class InsightPromptActivation:
    """The immutable active prompt and allowlisted route read from the registry.

    ``fragment_digest`` is the composed version's combined source digest, replicated
    from the registry so the generation logs can stamp it; ``None`` reads as
    unsourced (direct, seeded, or pre-tier records).
    """

    slot_id: InsightSlotId
    version: str
    prompt: str
    model_route: InsightModelRoute
    fragment_digest: str | None = None

    def __post_init__(self) -> None:
        if not self.version.strip() or not self.prompt.strip():
            raise ValueError("insight prompt activation requires versioned nonblank prompt text.")
        if not isinstance(cast("object", self.model_route), InsightModelRoute):
            raise ValueError("insight prompt activation requires an allowlisted model route.")
        if self.fragment_digest is not None and not self.fragment_digest.strip():
            raise ValueError("insight prompt activation fragment digest must be nonblank.")


@dataclass(frozen=True, slots=True)
class InsightGenerationRequest:
    """Closed server-owned direct-Converse input for a single declared slot.

    ``window_profile`` joins every payload (ADR-076 decision 5): the profile is a
    pure function of the served window, so it adds no cache-identity dimension —
    the key's window dates already pin it.
    """

    key: InsightCacheKey
    output_class: InsightOutputClass
    prompt: InsightPromptActivation
    purpose: str
    findings: tuple[DeterministicFinding, ...]
    window_profile: InsightWindowProfile
    context: tuple[InsightContextEntry, ...] = ()
    next_step_options: tuple[InsightNextStepOption, ...] = ()
    """FR-11: the card's served steps; the tool schema offers ``next_step`` only with these."""

    def __post_init__(self) -> None:
        if (
            self.prompt.slot_id is not self.key.slot_id
            or self.prompt.version != self.key.prompt_version
        ):
            raise ValueError("insight generation prompt must match the cache identity.")
        if not self.purpose.strip() or not self.findings:
            raise ValueError(
                "insight generation needs declared purpose and deterministic findings."
            )
        if len({finding.name for finding in self.findings}) != len(self.findings):
            raise ValueError("insight generation findings must have unique names.")
        if len({entry.name for entry in self.context}) != len(self.context):
            raise ValueError("insight generation context entries must have unique names.")
        if len({option.step_id for option in self.next_step_options}) != len(
            self.next_step_options
        ):
            raise ValueError("insight generation next-step options must have unique ids.")
        if self.next_step_options and self.key.tier is not InsightPromptTier.CARD:
            raise ValueError("only the card tier offers next steps.")
        if self.key.tier is InsightPromptTier.CARD and self.key.input_digest != (
            insight_input_digest(insight_input_document(self))
        ):
            raise ValueError("a card insight generation must match its cache input digest.")

    @property
    def finding_values(self) -> tuple[str, ...]:
        """Return each finding's value in the canonical rendering the payload carries."""
        return tuple(decimal_figure(finding.value) for finding in self.findings)


@dataclass(frozen=True, slots=True)
class InsightContinuationRequest:
    """A validated exact cache miss admitted to durable background generation."""

    generation: InsightGenerationRequest


@dataclass(frozen=True, slots=True)
class InsightPromptVersionRecord:
    """One immutable version with persisted calibration detail for operator read-only views.

    ``fragment_digests`` is present exactly for tooling-composed versions; ``None``
    reads as unsourced (direct operator versions, contract-seeded v1s, and every
    record retained from before the tier dimension existed). ``author`` and
    ``created_at`` are both ``None`` exactly for a record written before authorship
    was recorded; one without the other is invalid.
    """

    slot_id: InsightSlotId
    version: int
    prompt: str
    model_route: InsightModelRoute
    author: PromptVersionAuthor | None
    created_at: datetime | None
    calibration: str
    calibration_cases: tuple[InsightCalibrationCaseResult, ...]
    tier: InsightPromptTier = InsightPromptTier.CARD
    fragment_digests: PromptFragmentDigests | None = None

    def __post_init__(self) -> None:
        if (
            self.slot_id is InsightSlotId.MOVERS
            or self.version < 1
            or not self.prompt.strip()
            or (self.author is None) != (self.created_at is None)
            or (self.created_at is not None and self.created_at.tzinfo is None)
            or self.calibration not in {"pending", "passed", "failed"}
        ):
            raise ValueError("prompt version projection is invalid.")
        if self.calibration == "pending" and self.calibration_cases:
            raise ValueError("pending calibration cannot have final case outcomes.")
        if self.calibration != "pending":
            # Deliberately relaxed to unique valid kinds (ADR-076 slice E): records
            # calibrated before the vocabulary grew carry fewer case rows and stay
            # readable unmigrated (the ADR consequence); FRESH runs still require the
            # full vocabulary via InsightPromptCalibrationRunRequest and
            # InsightCalibrationResult.
            kinds = tuple(result.kind for result in self.calibration_cases)
            if not kinds or len(set(kinds)) != len(kinds):
                raise ValueError("completed calibration requires unique case outcomes.")


@dataclass(frozen=True, slots=True)
class InsightPromptSlotState:
    """Registry-owned state overlaid onto one declared prompt slot.

    ``active_version`` is the card tier's pointer; ``active_deep_version`` is the
    deep tier's (ADR-076 decision 6). ``newest_version`` stays tier-blind because
    versions form one dense per-slot sequence shared across tiers.
    """

    slot_id: InsightSlotId
    active_version: int | None
    newest_version: int | None
    newest_calibration: str | None
    active_deep_version: int | None = None

    def __post_init__(self) -> None:
        if self.slot_id is InsightSlotId.MOVERS and any(
            value is not None
            for value in (
                self.active_version,
                self.newest_version,
                self.newest_calibration,
                self.active_deep_version,
            )
        ):
            raise ValueError("the deterministic movers slot cannot have prompt state.")
        if self.active_version is not None and self.active_version < 1:
            raise ValueError("active prompt version must be positive.")
        if self.newest_version is not None and self.newest_version < 1:
            raise ValueError("newest prompt version must be positive.")
        if self.active_deep_version is not None and self.active_deep_version < 1:
            raise ValueError("active deep prompt version must be positive.")
        if self.newest_calibration not in {None, "pending", "passed", "failed"}:
            raise ValueError("prompt calibration state is unsupported.")


@dataclass(frozen=True, slots=True)
class InsightGeneratedNarration:
    """One structured model response before its cacheable slot is assembled."""

    output_class: InsightOutputClass
    sentences: tuple[str, ...]
    next_step: InsightNextStep | None = None
    """Resolved from the request's served options by id; a legacy row reads as ``None``."""

    def __post_init__(self) -> None:
        if not self.sentences:
            if self.next_step is not None:
                raise ValueError("a silent insight narration cannot offer a next step.")
            return
        if len(self.sentences) > 3 or any(not sentence.strip() for sentence in self.sentences):
            raise ValueError("insight narration must contain one to three nonblank sentences.")
        if _canonical_json_string_bytes(" ".join(self.sentences)) > MAX_INSIGHT_SLOT_TEXT_BYTES:
            raise ValueError("insight narration exceeds the slot byte bound.")

    @property
    def text(self) -> str | None:
        return None if not self.sentences else " ".join(self.sentences)


@dataclass(frozen=True, slots=True)
class InsightCacheWriteResult:
    """The outcome of a conditional immutable cache creation."""

    stored: bool
    slot: InsightSlot


# The deep analysis carries a deliberately larger budget than a card narration (its
# token budget is ~5x the card's); the bound exists so a cached row stays a bounded
# item, not to shape the prose.
MAX_INSIGHT_DEEP_TEXT_BYTES = 16_384
MAX_INSIGHT_DEEP_READINGS = 3


@dataclass(frozen=True, slots=True)
class InsightDeepAnalysis:
    """One structured deep-dive analysis (``insight-deep-dive-v1``, ADR-076 decision 1).

    What stands out; up to three candidate readings, each labeled by position as a
    reading, never a finding; and the evidence that would discriminate between them.
    ``cited_figures`` is the model's declared list of the finding values it relied
    on. It stays in the stored and served shape (ADR-082 keeps it rather than
    migrating retained deep rows), is never rendered, and refuses nothing: figure
    faithfulness is owned by calibration and evaluation.
    """

    what_stands_out: str
    candidate_readings: tuple[str, ...]
    discriminating_evidence: str
    cited_figures: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.what_stands_out.strip() or not self.discriminating_evidence.strip():
            raise ValueError("a deep analysis requires nonblank standout and evidence sections.")
        if (
            not self.candidate_readings
            or len(self.candidate_readings) > MAX_INSIGHT_DEEP_READINGS
            or any(not reading.strip() for reading in self.candidate_readings)
        ):
            raise ValueError("a deep analysis carries one to three nonblank candidate readings.")
        if len(set(self.cited_figures)) != len(self.cited_figures) or any(
            not figure.strip() for figure in self.cited_figures
        ):
            raise ValueError("deep analysis cited figures must be unique nonblank text.")
        if _canonical_json_string_bytes(self.text) > MAX_INSIGHT_DEEP_TEXT_BYTES:
            raise ValueError("a deep analysis exceeds its byte bound.")

    @property
    def text(self) -> str:
        """Return the joined prose the byte bound measures."""
        return " ".join(
            (self.what_stands_out, *self.candidate_readings, self.discriminating_evidence)
        )


class InsightDeepDiveState(StrEnum):
    """The closed serving vocabulary for one deep-tier dialog section (contract §3.1)."""

    FILLED = "filled"
    PENDING = "pending"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class InsightDeepRecord:
    """One cached deep-tier result for an exact tier-bearing identity.

    A deep record is never an ``InsightSlot``: its content shape and byte budget
    differ by design. ``FILLED`` carries the analysis; ``UNAVAILABLE`` is the
    durable retry-exhausted placeholder carrying its bounded reason — a reader
    regenerates it exactly as the card tier heals its degrade rows. ``PENDING``
    never persists: it is a serving state, not a stored one.
    """

    slot_id: InsightSlotId
    state: InsightDeepDiveState
    analysis: InsightDeepAnalysis | None
    absence_reason: str | None
    artifact_revision: str
    prompt_version: str

    def __post_init__(self) -> None:
        if self.state is InsightDeepDiveState.PENDING:
            raise ValueError("a pending deep section is a serving state, never a cached record.")
        if not self.artifact_revision.strip() or not self.prompt_version.strip():
            raise ValueError("deep records require artifact and prompt provenance stamps.")
        filled = self.state is InsightDeepDiveState.FILLED
        if filled != (self.analysis is not None) or filled == (self.absence_reason is not None):
            raise ValueError(
                "filled deep records carry exactly the analysis; unavailable ones the reason."
            )
        if self.absence_reason is not None and (
            not self.absence_reason.strip()
            or _canonical_json_string_bytes(self.absence_reason) > MAX_INSIGHT_EVIDENCE_BYTES
        ):
            raise ValueError("deep absence reason must be nonblank and within its byte bound.")


@dataclass(frozen=True, slots=True)
class InsightDeepCacheWriteResult:
    """The outcome of a conditional immutable deep-record creation."""

    stored: bool
    record: InsightDeepRecord


@dataclass(frozen=True, slots=True)
class CrossHorizonDirectionRequest:
    """One sibling preset's movement offered to the deep payload as direction only."""

    preset_label: str
    metric_label: str
    current: Decimal
    prior: Decimal | None

    def __post_init__(self) -> None:
        if not self.preset_label.strip() or not self.metric_label.strip():
            raise ValueError("a cross-horizon direction entry requires preset and metric labels.")


def cross_horizon_direction_entry(request: CrossHorizonDirectionRequest) -> InsightContextEntry:
    """Render one sibling preset's movement as uncitable, direction-only display context.

    The approved window-scope carve-out (ADR-076 decision 5) admits a sibling
    window's DIRECTION into the deep payload and nothing else: the entry carries
    direction words only — never a figure — and lands in context, which by
    construction never joins the supplied findings.
    """
    if request.prior is None:
        direction = "has no aligned prior in that window"
    elif request.current > request.prior:
        direction = "is rising against its aligned prior"
    elif request.current < request.prior:
        direction = "is falling against its aligned prior"
    else:
        direction = "is flat against its aligned prior"
    return InsightContextEntry(
        f"Sibling {request.preset_label} window direction",
        f"{request.metric_label} {direction} in the {request.preset_label} window "
        "(direction only; figures from another window are never shown here)",
    )


@dataclass(frozen=True, slots=True)
class DeterministicMoversSlotRequest:
    """The ranked monetary movers one deterministic movers slot renders, and their currency.

    ``silence_reason`` is the movers contract's governed silence condition, carried
    whenever the ranked findings are empty so the quiet slot names its business reason.
    ``currency_code`` is the code of the money the findings move; it is required whenever
    there are findings and ignored when there are none.
    """

    artifact_revision: str
    findings: tuple[DeterministicFinding, ...]
    silence_reason: str
    currency_code: str | None

    def __post_init__(self) -> None:
        if not self.silence_reason.strip():
            raise ValueError("the deterministic movers slot requires its silence condition.")
        if self.findings and self.currency_code is None:
            raise ValueError("deterministic movers findings require their currency code.")


def nonzero_movers(findings: tuple[DeterministicFinding, ...]) -> tuple[DeterministicFinding, ...]:
    """W12: drop zero-change rows, so a row that did not move is never listed as a mover."""
    return tuple(item for item in findings if item.value != 0)


def deterministic_movers_text(
    findings: tuple[DeterministicFinding, ...], currency_code: str
) -> str:
    """Write the top three nonzero monetary movers at two places with their code (ADR-084)."""
    return "; ".join(
        f"{item.name} changed by {currency_display_text(item.value, currency_code)}"
        for item in nonzero_movers(findings)[:3]
    )


def deterministic_movers_slot(request: DeterministicMoversSlotRequest) -> InsightSlot:
    """Render the declared deterministic movers slot without prompt, model, or cache access.

    No nonzero mover is the movers contract's declared silence (W12).
    """
    artifact_revision = request.artifact_revision
    if not nonzero_movers(request.findings) or request.currency_code is None:
        return InsightSlot(
            InsightSlotId.MOVERS,
            InsightOutputClass.OBSERVED,
            InsightSlotState.SILENT,
            None,
            None,
            artifact_revision,
            "deterministic-v1",
            absence_class=InsightAbsenceClass.EVALUATED_QUIET,
            absence_reason=request.silence_reason,
        )
    rendered = deterministic_movers_text(request.findings, request.currency_code)
    return InsightSlot(
        InsightSlotId.MOVERS,
        InsightOutputClass.OBSERVED,
        InsightSlotState.FILLED,
        f"{rendered}.",
        "Server-ranked absolute changes.",
        artifact_revision,
        "deterministic-v1",
    )
