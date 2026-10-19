# SPDX-License-Identifier: MIT-0
"""The ShapedResponse envelope — the load-bearing abstraction of the platform.

Every tool returns this and nothing else. Shape is specified in
docs/capabilities/conversational-runtime/low-level-design.md §6; decisions in ADR-006 and ADR-010.

Pure domain: no AWS, no serialisation framework, no HTTP. The invariants below
exist so that every governed figure a tool returns carries its own qualifiers
structurally, the ground the agent reasons from (ADR-082), rather than merely
documented — a tool cannot construct a response that hides a
truncation, omits its grain, or reports a row count it did not produce.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from domain.narrative.publication import (
    AnswerQualifier,
    GroundedCollectionV1,
    GroundedRendering,
    GroundingSet,
    RenderingKind,
)
from domain.semantics.analytical_resolution import FieldOrigin, ResolutionProvenance
from domain.semantics.compatibility import CompatibilityDifference
from domain.semantics.time import FactTimeClass
from domain.shared_kernel.provenance import FreshnessStatus, MeasurementBasis, Suppression
from domain.shared_kernel.values import DateRange, Grain

__all__ = [
    # Re-exported from the shared kernel so the envelope reads as one vocabulary at
    # its own boundary. The declarations live in domain/shared_kernel/ because ports and
    # other subdomains need them too and must not import from narration. `Grain` moved
    # there for exactly that reason: domain/catalog was importing it from here.
    "AnswerQualifier",
    "ClaimField",
    "DescriptorField",
    "FreshnessStatus",
    "Grain",
    "MeasurementBasis",
    "Metadata",
    "NonAdditivity",
    "Recommendation",
    "ResolutionProvenance",
    "ResponseData",
    "ShapeError",
    "ShapedResponse",
    "Suppression",
    "UnitKind",
    "Units",
    "non_additivity",
]


_QUALIFIER_SOURCE_PATHS = frozenset(
    {
        "metadata.suppression",
        "metadata.truncated",
        "metadata.actual_coverage_start",
        "metadata.actual_coverage_end",
        "metadata.requested_coverage_intervals",
        "metadata.actual_coverage_intervals",
        "metadata.coverage_holes",
        "metadata.loaded_at",
        "metadata.freshness_status",
        "metadata.freshness_reason",
        "metadata.measurement_basis",
        "metadata.time_class",
        "metadata.source_timezone",
        "metadata.qualified_dq",
        # The facts read decide an association-only answer: every metric on
        # fct_exposure_group_outcome is `causal: false` (ADR-102).
        "metadata.facts_read",
        "data.grain",
    }
)


_DEFAULT_METRIC_DISCLOSURE = "Metric selection used the governed default."


def _default_window_disclosure(window: str) -> str:
    return (
        "No reporting window was specified, so this answer uses "
        f"{window}, the latest published coverage."
    )


@dataclass(frozen=True, slots=True)
class _ProvenanceDisclosure:
    """One fixed factual disclosure derived from an approved typed origin."""

    disclosure_id: str
    text: str
    source_path: str


def _provenance_disclosures(
    provenance: tuple[ResolutionProvenance, ...],
) -> tuple[_ProvenanceDisclosure, ...]:
    """Map approved origin facts to fixed disclosures.

    The metric disclosure carries no value; the window disclosure carries the resolved window
    because a defaulted period the user cannot see is a population they did not choose. Both
    values are canonical public provenance scalars, never source or scope internals.
    """

    def _metric_disclosure(_value: str) -> str:
        return _DEFAULT_METRIC_DISCLOSURE

    builders: tuple[tuple[str, Callable[[str], str]], ...] = (
        ("metric", _metric_disclosure),
        ("current_period", _default_window_disclosure),
    )
    disclosures: list[_ProvenanceDisclosure] = []
    for field_name, build in builders:
        matches = tuple(
            index
            for index, item in enumerate(provenance)
            if item.field == field_name and item.origin is FieldOrigin.GOVERNED_DEFAULT
        )
        if len(matches) > 1:
            raise ShapeError(
                f"resolution provenance repeats the governed-default {field_name} origin."
            )
        if not matches:
            continue
        index = matches[0]
        disclosures.append(
            _ProvenanceDisclosure(
                disclosure_id=f"{field_name}:governed_default",
                text=build(provenance[index].value),
                source_path=f"metadata.resolution_provenance[{index}].origin",
            )
        )
    return tuple(disclosures)


class ShapeError(Exception):
    """A response violates an envelope invariant.

    Raised at construction, so an ill-formed response cannot reach the model.
    """


class UnitKind(StrEnum):
    """What a figure is denominated in."""

    CURRENCY = "currency"
    COUNT = "count"
    RATIO = "ratio"
    PERCENT = "percent"
    DURATION_SECONDS = "duration_seconds"
    ORDINAL = "ordinal"


@dataclass(frozen=True, slots=True)
class Units:
    """What a figure is denominated in.

    Currency requires a code. The canonical model forbids a bare monetary amount,
    so the type forbids it too — an amount without a currency cannot be built.
    """

    kind: UnitKind
    currency_code: str | None = None

    def __post_init__(self) -> None:
        if self.kind is UnitKind.CURRENCY and not self.currency_code:
            raise ShapeError("Currency units require an ISO 4217 currency_code.")
        if self.kind is not UnitKind.CURRENCY and self.currency_code:
            raise ShapeError(f"currency_code is meaningless for {self.kind}.")


@dataclass(frozen=True, slots=True)
class ClaimField:
    """One response field that is a numerical claim and the units it carries."""

    name: str
    units: Units
    label: str | None = None
    """Optional customer-facing name; the structural field name remains the evidence path."""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ShapeError("Claim field requires a name.")
        if self.label is not None and not self.label.strip():
            raise ShapeError("Claim field label cannot be blank.")


@dataclass(frozen=True, slots=True)
class DescriptorField:
    """One structured non-measurement field that may be rendered as catalog evidence."""

    name: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ShapeError("Descriptor field requires a name.")


@dataclass(frozen=True, slots=True)
class NonAdditivity:
    """The metric's own rule that these rows' figures don't add up, carried with the figures.

    Built from the metric dictionary (a distinct count, a ratio, or an axis its aggregation
    policy holds `exact_only`), never from the figures. Stated structurally as well as in the
    no-total sentence, so the rule the answer must keep sits beside the values it governs.
    """

    across: tuple[str, ...]
    """The grain columns these figures differ along and must not be added across."""

    instruction: str
    """The plain rule, in words: no sum, no total, and no share of a sum."""

    def __post_init__(self) -> None:
        if not self.across or len(set(self.across)) != len(self.across):
            raise ShapeError("Non-additivity names each grain column it holds for, once.")
        if not self.instruction.strip():
            raise ShapeError("Non-additivity requires its instruction.")


def non_additivity(across: tuple[str, ...], described_as: str) -> NonAdditivity:
    """Carry the columns these figures don't add across, with the one fixed instruction.

    `described_as` names them in customer words (the governed grain names, such as "audience
    segment"), never the storage columns. The caller derives it (`answers._axis_words` from
    `planning.GRAIN_NAMES`) because the grain vocabulary lives in `domain.measurement.planning`,
    which imports this module; reading it here would make the import circular.
    """
    return NonAdditivity(
        across=across,
        instruction=(
            f"These figures are not additive across {described_as}. Don't add them together, and "
            "don't state a total or a share of a total built from them. A total is stated "
            "only when a governed tool returns one."
        ),
    )


@dataclass(frozen=True, slots=True)
class Recommendation:
    """A ranked suggestion derived in domain code.

    The rationale is mandatory: the model presents recommendations and never
    invents them, so one without a reason cannot be presented honestly.
    """

    statement: str
    rationale: str
    rank: int

    def __post_init__(self) -> None:
        if not self.statement:
            raise ShapeError("Recommendation requires a statement.")
        if not self.rationale:
            raise ShapeError("Recommendation requires a rationale — it is rule-derived.")
        if self.rank < 1:
            raise ShapeError("Recommendation rank starts at 1.")


@dataclass(frozen=True, slots=True)
class ResponseData:
    """The only source of numbers.

    Values are already aggregated, rounded and unit-labelled by the tool. The
    model never divides, sums or converts.
    """

    values: tuple[dict[str, str | int | Decimal | None], ...]
    units: Units | None
    grain: Grain
    """The grain stays mandatory even with no values. "Nothing sold, by ASIN" and "nothing
    sold, in total" are different answers, and only the grain distinguishes them."""

    claim_fields: tuple[ClaimField, ...] = ()
    """Numerical fields allowed to become visible claims, each with its own denomination."""

    descriptor_fields: tuple[DescriptorField, ...] = ()
    """Structured capability fields that are descriptive rather than measured quantities."""

    claim_narrative_indices: tuple[int, ...] = ()
    """Narrative lines that are tool-authored factual atoms rather than connective scaffolding."""

    collections: tuple[GroundedCollectionV1, ...] = ()
    """Tool-owned ordered sets whose labels, membership and counts may be published."""

    population_total: Decimal | int | None = None
    """Every row's figure added up, or `None` when no total can be stated honestly.

    **Structured rather than stated only in prose, and G-45 is the reason.** The tool contract's
    central invariant is that the model never sums, and the prompt saying so is a soft control: a
    breakdown carried five rows and no total, and the model added them up and presented the result
    as a figure. Supplying the total removes the motive, but a total that lives only inside a
    paraphrasable sentence is still a prompt-shaped control — and it forces any later check over the
    narration to carry a permanent exception for the one figure it cannot find in an envelope, which
    is how such a check ends up not being built.

    Denominated by `units`, which already labels the rows. A currency total shares their code
    because `Money.total` refuses a mixed currency, so there is no second denomination to state.

    `None` covers every reason a total would be dishonest, and they are not all alike: the metric
    may not add, its units may not be a quantity, the read may be a truncated page, or Amazon may
    have withheld part of the population. `total_of` owns the first two, because they are properties
    of the figures. The caller owns the rest, because they are facts about the read. Whichever
    applies, the narrative must say which — an unexplained absence reads as an oversight and invites
    the model to supply the missing sum itself.
    """

    additivity: NonAdditivity | None = None
    """Present exactly when the metric forbids adding these rows' figures: never with a total."""

    def __post_init__(self) -> None:
        """Tie units to values, so neither can be present without the other.

        **Units were mandatory and that made one honest answer unconstructible.** A monetary
        figure's currency comes from the rows — `currency_code` is a column on the fact, not a
        property of the query — so an empty monetary result has no currency to state, and
        `Units` rightly refuses a currency without a code. A tool answering "what did I sell"
        over a window that contains nothing was therefore forced to either invent a currency or
        raise, and zero rows is an answer rather than a refusal.

        Optional *only* when there are no values. With figures present, units unstated is the
        bare monetary amount the canonical model forbids, so that direction is refused too.

        Third occurrence of one shape: `Measurement.units`, `Coverage.window` and now this are
        all absent exactly when the thing they describe is absent. **A label for figures is
        absent exactly when the figures are** — an empty label is a claim, and inventing one is
        the fabricated figure this platform exists to refuse.
        """
        if self.values and self.units is None:
            raise ShapeError(
                "Values were carried with no units to state them in. A figure without its "
                "units is the bare amount the canonical model forbids."
            )
        if not self.values and self.units is not None:
            raise ShapeError(
                "Units were stated for a response carrying no values. There is nothing to "
                "denominate, so naming a denomination is a claim about data that is absent."
            )
        if not self.values and self.population_total is not None:
            # The same shape a fourth time. A total over no rows is not zero, it is a figure
            # describing a population nothing was read from — and zero here would read as the
            # denominator an empty answer is judged against.
            raise ShapeError(
                "A population total was stated for a response carrying no values. Nothing was "
                "added, so the total is a claim about data that is absent."
            )
        self._check_additivity()
        claim_names = tuple(field.name for field in self.claim_fields)
        if len(set(claim_names)) != len(claim_names):
            raise ShapeError("Response data repeats a numerical claim field.")
        descriptor_names = tuple(field.name for field in self.descriptor_fields)
        if len(set(descriptor_names)) != len(descriptor_names):
            raise ShapeError("Response data repeats a descriptor field.")
        if set(claim_names).intersection(descriptor_names):
            raise ShapeError("A response field cannot be both a claim and a descriptor.")
        if len(set(self.claim_narrative_indices)) != len(self.claim_narrative_indices) or any(
            index < 0 for index in self.claim_narrative_indices
        ):
            raise ShapeError("claim narrative indices must be unique and non-negative.")
        collection_ids = tuple(collection.collection_id for collection in self.collections)
        if len(set(collection_ids)) != len(collection_ids):
            raise ShapeError("response data repeats a grounded collection.")
        for name in (*claim_names, *descriptor_names):
            if any(name not in row for row in self.values):
                raise ShapeError(f"Declared response field {name!r} is absent from a row.")

    def _check_additivity(self) -> None:
        """Refuse a total beside the rule against one, or a rule over a column not in the grain."""
        if self.additivity is None:
            return
        if self.population_total is not None:
            raise ShapeError("A total was stated across figures the metric forbids adding.")
        if not set(self.additivity.across) <= set(self.grain.dimensions):
            raise ShapeError("Non-additivity names a column outside the response grain.")


@dataclass(frozen=True, slots=True)
class Metadata:
    """Provenance and honesty flags. Not optional in narration."""

    facts_read: tuple[str, ...]
    measurement_basis: tuple[MeasurementBasis, ...]
    coverage_start: datetime
    coverage_end: datetime
    freshness_status: FreshnessStatus
    suppression: Suppression
    row_count: int
    computed_at: datetime
    correlation_id: str
    resolution_provenance: tuple[ResolutionProvenance, ...] = ()
    """Sanitized origins; approved typed defaults may produce fixed mandatory context."""

    semantic_qualifiers: tuple[CompatibilityDifference, ...] = ()
    requested_coverage_intervals: tuple[DateRange, ...] = ()
    """Exact periods the figures answer; two comparison windows stay two intervals."""

    actual_coverage_intervals: tuple[DateRange, ...] = ()
    """Exact normalized intervals admitted by the selected CURRENT publications."""

    coverage_holes: tuple[DateRange, ...] = ()
    """Requested intervals unavailable from CURRENT, never inferred from min/max bounds."""

    actual_coverage_start: datetime | None = None
    actual_coverage_end: datetime | None = None
    loaded_at: datetime | None = None
    freshness_reason: str | None = None
    time_class: FactTimeClass | None = None
    source_timezone: str = ""
    qualified_dq: tuple[str, ...] = ()
    qualifiers: tuple[AnswerQualifier, ...] = ()
    """Typed mandatory caveats derived independently of model prose."""

    grounding: GroundingSet | None = None
    """Candidate-bound deterministic renderings, filled by ``ShapedResponse`` itself."""

    truncated: bool = False
    contract_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.facts_read:
            raise ShapeError("A response must name the facts it read.")
        if not self.measurement_basis:
            raise ShapeError("A response must declare its measurement basis.")
        for label, value in (
            ("coverage_start", self.coverage_start),
            ("coverage_end", self.coverage_end),
            ("computed_at", self.computed_at),
        ):
            if value.tzinfo is None:
                raise ShapeError(f"{label} must be timezone-aware.")
        if self.coverage_end < self.coverage_start:
            raise ShapeError("coverage_end cannot precede coverage_start.")
        self._validate_semantic_evidence()
        if self.row_count < 0:
            raise ShapeError("row_count cannot be negative.")
        if not self.correlation_id:
            raise ShapeError("A response must carry its correlation_id.")
        if list(self.contract_ids) != sorted(set(self.contract_ids)):
            # Refused rather than corrected, which is how `QueryPlan` treats a shape it cannot
            # honour. A duplicate is meaningless — it reads as a contract having contributed twice
            # — and load order carries no meaning, so two identical answers could otherwise differ
            # in this field alone. `facts_read` is deliberately exempt: its order is the
            # numerator-then-denominator order of the metric, which is information.
            raise ShapeError("contract_ids must be sorted and free of duplicates.")
        qualifier_renderings = tuple(item.rendering for item in self.qualifiers)
        if len(set(qualifier_renderings)) != len(qualifier_renderings):
            raise ShapeError("metadata repeats a mandatory qualifier rendering.")
        unknown_paths = {
            path
            for qualifier in self.qualifiers
            for path in qualifier.source_paths
            if path not in _QUALIFIER_SOURCE_PATHS
        }
        if unknown_paths:
            raise ShapeError(
                f"qualifier evidence names unknown typed paths: {sorted(unknown_paths)}."
            )

    def _validate_semantic_evidence(self) -> None:
        if (self.actual_coverage_start is None) != (self.actual_coverage_end is None):
            raise ShapeError("actual coverage requires both bounds or neither.")
        for label, intervals in (
            ("requested coverage intervals", self.requested_coverage_intervals),
            ("actual coverage intervals", self.actual_coverage_intervals),
            ("coverage holes", self.coverage_holes),
        ):
            if len(set(intervals)) != len(intervals):
                raise ShapeError(f"{label} must not repeat an interval.")
            if tuple(sorted(intervals, key=lambda item: (item.start, item.end))) != intervals:
                raise ShapeError(f"{label} must be sorted.")
        for label, value in (
            ("actual_coverage_start", self.actual_coverage_start),
            ("actual_coverage_end", self.actual_coverage_end),
            ("loaded_at", self.loaded_at),
        ):
            if value is not None and value.tzinfo is None:
                raise ShapeError(f"{label} must be timezone-aware when present.")
        if (
            self.actual_coverage_start is not None
            and self.actual_coverage_end is not None
            and self.actual_coverage_end < self.actual_coverage_start
        ):
            raise ShapeError("actual coverage end cannot precede its start.")
        if any(not caveat.strip() for caveat in self.qualified_dq):
            raise ShapeError("qualified DQ caveats must be non-empty.")

    @property
    def mixes_basis(self) -> bool:
        """Whether more than one measurement basis is present.

        Derived, never stored, so it cannot drift from the bases it describes.
        A ratio spanning two bases is legitimate to compute and dishonest to
        present unlabelled.
        """
        return len(set(self.measurement_basis)) > 1


@dataclass(frozen=True, slots=True)
class _ClaimRenderingRequest:
    """Inputs for one natural claim sentence."""

    row: dict[str, str | int | Decimal | None]
    claim_field: ClaimField
    claim_names: frozenset[str]
    grain: Grain


@dataclass(frozen=True, slots=True)
class ShapedResponse:
    """What every tool returns.

    Three invariants are enforced here rather than trusted:

    - row_count must match the values actually carried, so a tool cannot report a
      count it did not produce.
    - a truncated response must say so in its narrative, so a silently truncated
      result read as complete is impossible.
    - a population total must appear in the narrative, verbatim, so the figure the
      envelope carries and the figure the sentence states cannot drift apart.
    """

    data: ResponseData
    narrative: tuple[str, ...]
    metadata: Metadata
    recommendations: tuple[Recommendation, ...] = field(default=())

    def __post_init__(self) -> None:
        if not self.narrative:
            raise ShapeError("A response must carry factual narrative scaffolding.")
        if self.metadata.row_count != len(self.data.values):
            raise ShapeError(
                f"row_count {self.metadata.row_count} does not match "
                f"{len(self.data.values)} values carried."
            )
        if any(index >= len(self.narrative) for index in self.data.claim_narrative_indices):
            raise ShapeError("claim narrative index does not resolve to a narrative line.")
        if self.metadata.truncated and not any("truncat" in s.lower() for s in self.narrative):
            raise ShapeError("A truncated response must say so in its narrative.")
        self._check_total_is_stated()
        self._attach_grounding()
        ranks = [r.rank for r in self.recommendations]
        if len(set(ranks)) != len(ranks):
            raise ShapeError("Recommendation ranks must be unique.")

    def _attach_grounding(self) -> None:  # noqa: PLR0912 - closed evidence families
        """Derive authoritative renderings from the typed envelope, never from model prose."""
        qualifier_by_text = {item.rendering: item for item in self.metadata.qualifiers}
        for qualifier in self.metadata.qualifiers:
            if self.narrative.count(qualifier.rendering) != 1:
                raise ShapeError(
                    f"Mandatory {qualifier.kind.value} qualifier must appear exactly once."
                )

        provenance_disclosures = _provenance_disclosures(self.metadata.resolution_provenance)
        fingerprint = repr(
            (
                self.metadata.correlation_id,
                self.data,
                self.narrative,
                self.recommendations,
                self.metadata.facts_read,
                self.metadata.measurement_basis,
                self.metadata.actual_coverage_start,
                self.metadata.actual_coverage_end,
                self.metadata.requested_coverage_intervals,
                self.metadata.actual_coverage_intervals,
                self.metadata.coverage_holes,
                self.metadata.loaded_at,
                self.metadata.time_class,
                self.metadata.source_timezone,
                self.metadata.qualified_dq,
                self.metadata.qualifiers,
                tuple(item.disclosure_id for item in provenance_disclosures),
                self.metadata.computed_at,
            )
        )
        grounding_id = "grounding-" + hashlib.sha256(fingerprint.encode()).hexdigest()[:24]
        renderings: list[GroundedRendering] = []
        mandatory: list[str] = []
        claim_narrative_indices = set(self.data.claim_narrative_indices)
        for index, line in enumerate(self.narrative):
            rendering_id = f"{grounding_id}:n:{index}"
            qualifier = qualifier_by_text.get(line)
            if qualifier is not None and index in claim_narrative_indices:
                raise ShapeError("a narrative line cannot be both a claim and qualifier.")
            if qualifier is None and index not in claim_narrative_indices:
                continue
            if qualifier is None:
                renderings.append(
                    GroundedRendering(
                        rendering_id=rendering_id,
                        text=line,
                        kind=RenderingKind.CLAIM,
                        source_paths=(f"narrative[{index}]",),
                    )
                )
                continue
            renderings.append(
                GroundedRendering(
                    rendering_id=rendering_id,
                    text=line,
                    kind=RenderingKind.QUALIFIER,
                    source_paths=qualifier.source_paths,
                    qualifier=qualifier.kind,
                )
            )
            mandatory.append(rendering_id)

        for disclosure in provenance_disclosures:
            rendering_id = f"{grounding_id}:provenance:{disclosure.disclosure_id}"
            renderings.append(
                GroundedRendering(
                    rendering_id=rendering_id,
                    text=disclosure.text,
                    kind=RenderingKind.CONTEXT,
                    source_paths=(disclosure.source_path,),
                )
            )
            mandatory.append(rendering_id)

        renderings.extend(self._descriptor_renderings(grounding_id))

        claim_names = frozenset(field.name for field in self.data.claim_fields)
        for index, row in enumerate(self.data.values):
            for claim_field in self.data.claim_fields:
                value = row[claim_field.name]
                if value is None:
                    continue
                rendering_id = f"{grounding_id}:row:{index}:{claim_field.name}"
                renderings.append(
                    GroundedRendering(
                        rendering_id=rendering_id,
                        text=self._claim_text(
                            _ClaimRenderingRequest(
                                row=row,
                                claim_field=claim_field,
                                claim_names=claim_names,
                                grain=self.data.grain,
                            )
                        ),
                        kind=RenderingKind.CLAIM,
                        source_paths=(
                            f"data.values[{index}].{claim_field.name}",
                            f"data.claim_fields.{claim_field.name}",
                            "data.grain",
                        ),
                    )
                )

        if self.data.population_total is not None:
            units = self.data.units
            if units is None:  # pragma: no cover - ResponseData rejects this shape
                raise ShapeError("A population total has no units.")
            renderings.append(
                GroundedRendering(
                    rendering_id=f"{grounding_id}:total",
                    text=(
                        "The total across this population was "
                        f"{self._claim_value(self.data.population_total, units)}, "
                        f"at the {self._grain_phrase(self.data.grain)} grain."
                    ),
                    kind=RenderingKind.CLAIM,
                    source_paths=(
                        "data.population_total",
                        "data.units",
                        "data.grain",
                    ),
                )
            )

        if self.data.additivity is not None:
            # The evaluator reads the grounding set, so the rule reaches it as the model gets it.
            renderings.append(
                GroundedRendering(
                    rendering_id=f"{grounding_id}:additivity",
                    text=self.data.additivity.instruction,
                    kind=RenderingKind.CONTEXT,
                    source_paths=("data.additivity", "data.grain"),
                )
            )

        for index, recommendation in enumerate(self.recommendations):
            renderings.append(
                GroundedRendering(
                    rendering_id=f"{grounding_id}:recommendation:{index}",
                    text=f"{recommendation.statement} {recommendation.rationale}",
                    kind=RenderingKind.RECOMMENDATION,
                    source_paths=(f"recommendations[{index}]",),
                )
            )

        grounding = GroundingSet(
            grounding_id=grounding_id,
            renderings=tuple(renderings),
            mandatory_rendering_ids=tuple(mandatory),
            requires_claim_selection=any(item.kind is RenderingKind.CLAIM for item in renderings),
            collections=self.data.collections,
        )
        if self.metadata.grounding is not None and self.metadata.grounding != grounding:
            raise ShapeError("metadata grounding differs from the typed response envelope.")
        object.__setattr__(self, "metadata", replace(self.metadata, grounding=grounding))

    def _descriptor_renderings(self, grounding_id: str) -> list[GroundedRendering]:
        names = tuple(field.name for field in self.data.descriptor_fields)
        if not names:
            return []
        renderings: list[GroundedRendering] = []
        if len(self.data.values) > 1 and {"metric", "label", "status"}.issubset(names):
            renderings.append(
                GroundedRendering(
                    rendering_id=f"{grounding_id}:descriptor:summary",
                    text=(
                        "I can help with advertising delivery and cost, attributed conversions "
                        "and ROAS, reach and frequency, retail sales and units, product traffic, "
                        "and cross-channel efficiency. Ask about a metric, reporting period, or "
                        "supported breakdown and I can narrow the analysis."
                    ),
                    kind=RenderingKind.CONTEXT,
                    source_paths=tuple(
                        f"data.values[{index}].metric" for index in range(len(self.data.values))
                    ),
                )
            )
        renderings.extend(
            GroundedRendering(
                rendering_id=f"{grounding_id}:descriptor:{index}",
                text=self._descriptor_text(row, names),
                kind=RenderingKind.CONTEXT,
                source_paths=tuple(f"data.values[{index}].{name}" for name in names),
            )
            for index, row in enumerate(self.data.values)
        )
        return renderings

    @classmethod
    def _descriptor_text(
        cls,
        row: dict[str, str | int | Decimal | None],
        names: tuple[str, ...],
    ) -> str:
        """Render structured catalog evidence as readable, deterministic prose."""
        if {"label", "status"}.issubset(names):
            label = str(row["label"] or row.get("metric") or "This metric")
            status = row["status"]
            if status != "available":
                return f"{label} is declared but is not yet computable."

            kind = cls._words(row.get("kind"))
            units = cls._words(row.get("units"))
            basis = cls._words(row.get("measurement_basis"))
            grains = cls._words(row.get("valid_grains"))
            opening = f"{label} is available"
            if kind:
                opening += f" as a {kind} metric"
            if units:
                opening += f" reported in {units}"
            sentences = [opening + "."]
            if basis:
                sentences.append(f"Its measurement basis is {basis}.")
            if grains:
                sentences.append(f"Supported breakdowns are {grains}.")
            customer_definition = row.get("customer_definition")
            if customer_definition:
                sentences.append(str(customer_definition))
            customer_caveat = row.get("customer_caveat")
            if customer_caveat:
                sentences.append(str(customer_caveat))
            return " ".join(sentences)

        parts = [
            f"{cls._field_label(name)}: {row[name]}" for name in names if row[name] is not None
        ]
        return ". ".join(parts) + "."

    @staticmethod
    def _words(value: object) -> str:
        return "" if value is None else str(value).replace("_", " ")

    @classmethod
    def _field_label(cls, name: str) -> str:
        return cls._words(name.removesuffix("_key")).capitalize()

    @classmethod
    def _grain_phrase(cls, grain: Grain) -> str:
        """Say a grain in customer words: canonical columns lose their `_key` suffix."""
        return " and ".join(cls._words(name.removesuffix("_key")) for name in grain.dimensions)

    @classmethod
    def _claim_text(cls, request: _ClaimRenderingRequest) -> str:
        value = request.row[request.claim_field.name]
        rendered_value = cls._claim_value(value, request.claim_field.units)
        label = request.claim_field.label or cls._field_label(request.claim_field.name)
        dimensions = [
            f"{cls._field_label(name).lower()} {request.row[name]}"
            for name in sorted(request.row)
            if name not in request.claim_names and request.row[name] is not None
        ]
        scope = f" for {', '.join(dimensions)}" if dimensions else ""
        grain_text = cls._grain_phrase(request.grain)
        # A-FORMAT-STRING-001: domain text, not a Flask view; see docs/security-dispositions.md
        # nosemgrep: python.flask.security.audit.directly-returned-format-string
        return f"{label} was {rendered_value}{scope}, at the {grain_text} grain."

    @staticmethod
    def _claim_value(value: object, units: Units) -> str:
        if units.kind is UnitKind.CURRENCY:
            return f"{value} {units.currency_code}"
        if units.kind is UnitKind.PERCENT:
            rendered = str(value)
            return rendered if rendered.endswith("%") else f"{rendered}%"
        if units.kind is UnitKind.DURATION_SECONDS:
            return f"{value} seconds"
        return str(value)

    def _check_total_is_stated(self) -> None:
        """Refuse a population total the narrative does not state, verbatim.

        **A substring test on the figure rather than on a word, and the difference is what it
        buys.** The truncation invariant above looks for "truncat", which proves a sentence on the
        subject exists and proves nothing about what it says. Here the figure itself has to appear,
        so the envelope's total and the narrated total cannot drift — a sentence rebuilt from a
        different value, or left stating a stale one, fails at construction rather than reaching a
        reader who has no way to check.

        **One-directional, deliberately.** A narrative that states a total the envelope does not
        carry is not detectable here: the narrative is prose, and searching it for numbers that
        might be totals would be the grounding check this invariant is a step towards rather than a
        substitute for. What is closed is the direction a tool controls.
        """
        total = self.data.population_total
        if total is None:
            return
        if not any(str(total) in line for line in self.narrative):
            raise ShapeError(
                f"A population total of {total} is carried but no narrative line states it. "
                "The figure and the sentence would be free to disagree."
            )

    @property
    def requires_basis_warning(self) -> bool:
        """Whether the narration layer must surface a basis caveat."""
        return self.metadata.mixes_basis
