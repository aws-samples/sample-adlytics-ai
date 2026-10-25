# SPDX-License-Identifier: MIT-0
"""Audiences: `AudienceProfileArtifact` v2 and its parts (docs/ui/audiences/low-level-design.md).

One complete calendar month per artifact, per governed marketplace, built from the planned
answers of packs 10-12 (C2, `fct_segment_profile`) and pack 8 (C1, `fct_segment_delivery`),
design §5.5 and §6.5. Rendering-free: every text is a server field.

- **Profile views.** "Amazon audience segments (paid)" reads packs 10 and 11, each its own
  delivery and profile path (`asi_in_market`, `asi_lifestyle`) with its own profiled base;
  "DSP-exposed buyers only" reads pack 12 (`dsp_exposed_only`). A view's segments are in one
  namespace, and the two views are never linked or compared as one list (P-BO-4, ADR-103).
- **Shares and the index** are computed inside one delivery by `segment_figures`, the rule chat
  reads too: buyer share = segment buyers / profiled buyers, reached share = segment reached
  shoppers / profiled reached shoppers, index = buyer share / reached share. A delivery whose
  `profiled_total` row is missing makes every share and index of it "Not available". Only a
  segment with at least B_min buyers carries an index on the page. Rows are ranked inside each
  delivery, never against another path's.
- **Roster.** A segment the delivery returned the month before with no row this month is listed
  last as "Not available", never as index 0; month 1 says so (`FIRST_MONTH_NOTE`). A segment
  one planned figure carries and another lacks is listed the same way, never read as 0.
- **Provider labels.** "Amazon" or "Third party" from the governed classification; a segment it
  doesn't cover carries no label on the page (LLD revision 2026-10-09.2).
- **Paid state (ADR-107).** Packs 10-11 unobserved: the paid view carries the decision-1 text and
  the page defaults to the DSP view. Observed with pack 13 unobserved: ASI segments read by ID
  with "name not available yet" and still carry their index.
- **Insight.** `segment-over-index` v1, one card per profile path (delivery), each finding
  tested inside that delivery with a Bonferroni critical value over its segment rows.
- **Parts (ADR-141).** The parent carries each profile path's first 50 rows and the first
  campaign index shard. Every other 50-row profile page, every DSP campaign's 50-row pages and
  every further index shard is its own small body under the parent's revision, fetched only when
  the reader asks for it. Each part names that revision (`parent_revision`).
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from statistics import NormalDist
from typing import TYPE_CHECKING, Final, cast

from domain.capabilities.paid_features import (
    PaidFeatureDisplay,
    PaidFeatureState,
    ResolvePeriodStateRequest,
    resolve_period_state,
)
from domain.measurement.rosters import FIRST_MONTH_NOTE
from domain.presentation.insights import (
    InsightAbsenceClass,
    InsightOutputClass,
    InsightSlot,
    InsightSlotId,
    InsightSlotState,
    OverIndexPolicy,
    insight_slots_payload,
)
from domain.presentation.preset_artifacts import PresetArtifactSiblingIdentity
from domain.semantics.audience_segments import (
    ASI_NAMES_NOT_AVAILABLE_TEXT,
    ASI_NEEDS_PAID_FEATURE_TEXT,
    MAX_LABEL_LENGTH,
    SEGMENT_IDS_SENTENCE,
    SegmentLabel,
    SegmentNamespace,
    SegmentProvider,
    provider_label,
)
from domain.semantics.calendar_periods import day_label
from domain.semantics.time import TimeGrain
from domain.shared_kernel.values import DateRange

if TYPE_CHECKING:  # pragma: no cover
    from domain.semantics.calendar_periods import CalendarPeriod

__all__ = [
    "AUDIENCES_ARTIFACT_TYPE",
    "AUDIENCES_ARTIFACT_VERSION",
    "AUDIENCES_CAMPAIGN_INDEX_TYPE",
    "AUDIENCES_CAMPAIGN_PAGE_TYPE",
    "AUDIENCES_INDEX_ENTRY_MAX_BYTES",
    "AUDIENCES_INDEX_SHARD_ENTRIES",
    "AUDIENCES_PAGE_ID",
    "AUDIENCES_PAGE_ROWS",
    "AUDIENCES_PART_VERSION",
    "AUDIENCES_PROFILE_PAGE_TYPE",
    "BOUNDARY_TEXT",
    "C2_BUYER_LABEL",
    "CAMPAIGN_HISTORY_GAP_TEXT",
    "CAMPAIGN_INCOMPLETE_TEXT",
    "CAMPAIGN_LIMIT_TEXT",
    "DELIVERY_PATHS",
    "DSP_VIEW",
    "NO_DELIVERY_TEXT",
    "NO_IMPRESSIONS_TEXT",
    "NO_SALES_TEXT",
    "NO_SPEND_TEXT",
    "PAID_VIEW",
    "PROFILE_PATH_REFUSED_TEXTS",
    "ROAS_CURRENCY_TEXT",
    "ROSTER_GAP_TEXT",
    "SEGMENT_PARTIAL_TEXT",
    "SPEND_CURRENCY_TEXT",
    "TOTAL_NOT_AVAILABLE_TEXT",
    "AudienceMarket",
    "AudienceProfileArtifact",
    "AudiencesPart",
    "CampaignAudiences",
    "CampaignEntryState",
    "CampaignIndexEntry",
    "CampaignPage",
    "CampaignSegment",
    "OverIndexFinding",
    "PaidState",
    "ProfileDelivery",
    "ProfileSegment",
    "ProfileView",
    "SegmentFigures",
    "audiences_artifact_payload",
    "audiences_campaign_index_payload",
    "audiences_campaign_page_payload",
    "audiences_part_identity",
    "audiences_profile_page_payload",
    "bonferroni_z",
    "build_view",
    "campaign_index_shard",
    "campaign_segment",
    "order_campaign_rows",
    "over_index_findings",
    "page_segment_display",
    "profile_page_count",
    "require_audiences_artifact_payload",
    "require_audiences_part_payload",
    "resolve_audiences_payload",
    "segment_figures",
    "segment_over_index_slot",
]

AUDIENCES_PAGE_ID: Final = "audiences"
AUDIENCES_ARTIFACT_TYPE: Final = "AudienceProfileArtifact"
AUDIENCES_ARTIFACT_VERSION: Final = 2
"""v2: the parent carries page 0 of each path and index shard 0; everything else is a part."""
AUDIENCES_CAMPAIGN_PAGE_TYPE: Final = "AudienceCampaignPage"
AUDIENCES_PROFILE_PAGE_TYPE: Final = "AudienceProfilePage"
AUDIENCES_CAMPAIGN_INDEX_TYPE: Final = "AudienceCampaignIndex"
AUDIENCES_PART_VERSION: Final = 1
AUDIENCES_PAGE_ROWS: Final = 50
"""Rows per profile or campaign page (owner decision 2)."""
AUDIENCES_INDEX_ENTRY_MAX_BYTES: Final = 800
"""A ceiling on one encoded index entry (a wide name and its refusal sentence); a test holds it."""
AUDIENCES_INDEX_SHARD_ENTRIES: Final = 100
"""Campaign index entries per shard. The parent carries shard 0 beside three paths' first pages:
16,000 + 3 x 50 x 800 + 100 x 800 = 216,000 B, under the 256,000 B cap with 40,000 B margin."""
PAID_VIEW: Final = "asi_all_exposed"
"""The page's paid profile option: packs 10 and 11 (design §6.5's `asi_all_exposed`)."""
DSP_VIEW: Final = "dsp_exposed_only"
"""The page's standard profile option: pack 12."""
VIEW_LABELS: Final = {
    PAID_VIEW: "Amazon audience segments (paid)",
    DSP_VIEW: "DSP-exposed buyers only",
}
DELIVERY_PATHS: Final = {
    PAID_VIEW: ("asi_in_market", "asi_lifestyle"),
    DSP_VIEW: ("dsp_exposed_only",),
}
"""Each view's deliveries: the `profile_path` values, each its own base (packs 10, 11, 12)."""
VIEW_NAMESPACES: Final = {
    PAID_VIEW: SegmentNamespace.AMC_ASI,
    DSP_VIEW: SegmentNamespace.AMC_DSP_BEHAVIOR,
}
DELIVERY_LABELS: Final = {
    "asi_in_market": "In-market",
    "asi_lifestyle": "Lifestyle",
    "dsp_exposed_only": "DSP behaviour segments",
}
C2_BUYER_LABEL: Final = "Ad-touched buyers in the month · may have bought before seeing an ad"
"""The C2 basis label (design §5.5, review DS-14): buyers may have bought before their ad."""
TOTAL_NOT_AVAILABLE_TEXT: Final = (
    "Not available: this month's profiled total didn't come back, so no share or index is computed."
)
ROSTER_GAP_TEXT: Final = (
    "Amazon returned too few shoppers for this segment this month. It was listed last month."
)
BELOW_MINIMUM_TEXT: Final = "Fewer than {minimum} buyers, so no index is shown."
NOT_AVAILABLE_TEXT: Final = "Not available"
BOUNDARY_TEXT: Final = (
    "This describes an audience you can build in your own tools. Adlytics doesn't create or "
    "activate audiences."
)
CAMPAIGN_LIMIT_TEXT: Final = (
    "Campaign grain shows which segments perform, not which line item to change."
)
_CAMPAIGN_REF_PREFIX: Final = "campaign-"
"""A served campaign's opaque reference: its ordinal in the artifact, never its canonical key."""
CAMPAIGN_PENDING_TEXT: Final = "DSP campaigns by segment for {month} haven't arrived yet."
CAMPAIGN_HISTORY_GAP_TEXT: Final = (
    "{campaign} can't be broken down by segment for {month}: part of its earlier segment history "
    "isn't available, so its segments can't be checked against last month's."
)
"""AUD-CMP-REFUSED-HISTORY (owner approval A2): one campaign's own history gap."""
CAMPAIGN_INCOMPLETE_TEXT: Final = (
    "DSP campaign figures for {month} aren't complete yet, so they aren't shown. The segment "
    "profile above is complete."
)
"""AUD-CMP-SECTION-INCOMPLETE (owner approval A2): the campaign section's incomplete evidence."""
PROFILE_PATH_REFUSED_TEXTS: Final = {
    "history_gap": "This profile for {month} can't be shown: part of its earlier segment history "
    "isn't available.",
    "evidence_incomplete": "This profile for {month} can't be shown: its figures aren't complete "
    "yet.",
}
"""AUD-PROFILE-PATH-REFUSED (owner approval A2), by the path's refusal reason."""
_PAGING_TEXT: Final = "Segments {first}\u2013{last} of {total}"
_CAMPAIGN_REF: Final = re.compile(r"campaign-[1-9][0-9]{0,5}")
ROAS_CURRENCY_TEXT: Final = "Not available: sales and spend didn't come back in one currency."
"""ROAS withheld: the two currencies disagree, or either came back without one."""
NO_SPEND_TEXT: Final = "Not available: no DSP spend came back for this segment this month."
"""CPM and ROAS withheld: spend absent or zero, so neither is divided by it."""
NO_SALES_TEXT: Final = "Not available: attributed sales didn't come back for this segment."
NO_IMPRESSIONS_TEXT: Final = "Not available: no impressions came back for this segment."
SPEND_CURRENCY_TEXT: Final = "Not available: spend came back without its currency."
SEGMENT_PARTIAL_TEXT: Final = (
    "Not available: Amazon didn't return every figure for this segment this month."
)
"""A segment one planned figure carries and another lacks: never read as a measured 0."""
NO_DELIVERY_TEXT: Final = "No buyer profile was measured for this month."
SEGMENT_FIGURES_NEVER_ADD_UP: Final = "segment figures never add up"
_DETERMINISTIC: Final = "deterministic-v1"
_EVIDENCE_LINE: Final = "Found by a fixed rule from the figures above. Not written by an AI model."
_NAMED_MAX: Final = 3
_VIEW_COUNT: Final = 2
_EVIDENCE_VERSION: Final = 2
_PERCENT: Final = Decimal(100)
_SHARE_QUANTUM: Final = Decimal("0.1")
_INDEX_QUANTUM: Final = Decimal("0.01")
_MONEY_QUANTUM: Final = Decimal("0.01")
_MAX_SAFE_INTEGER: Final = 2**53 - 1
"""The browser reads JSON numbers as doubles, so a larger count is refused on both sides."""
_MAX_DECIMAL_DIGITS: Final = 28
"""Default decimal-context precision: the longest coefficient `_decimal` can write (both sides)."""
_ISO_DATE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# Python's Unicode `\d` and `str.strip` differ from JavaScript's: digits are ASCII-only and
# blank is Python whitespace or U+FEFF, the union both decoders refuse (audiences-parity §1).
_BLANK: Final = re.compile(r"[\s\ufeff]*")
_SERVED_REVISION: Final = re.compile(r"[A-Za-z0-9_-]{43}")
_PUBLICATION_REVISION: Final = re.compile(r"[0-9a-f]{64}")
"""A publication's revision as `PresetArtifactRevision` writes it: a lowercase SHA-256."""
_PAGE_PROVIDER_LABELS: Final = frozenset(provider_label(item) for item in SegmentProvider)
_MONTH_NAMES: Final = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip


def month_label(month: date) -> str:
    """Return 'September 2026'."""
    return f"{_MONTH_NAMES[month.month - 1]} {month.year}"


# --- the shared rule: shares and the index inside one delivery -------------------------------


@dataclass(frozen=True, slots=True)
class ProfileSegment:
    """One returned segment row of a buyer-profile delivery."""

    segment_id: str
    exposed: int
    buyers: int
    buyers_new_to_brand: int

    def __post_init__(self) -> None:
        if not self.segment_id.strip():
            raise ValueError("a profile segment names its ID.")
        if min(self.exposed, self.buyers, self.buyers_new_to_brand) < 0:
            raise ValueError("a profile segment's counts are not negative.")


@dataclass(frozen=True, slots=True)
class ProfileDelivery:
    """One month's buyer-profile delivery for one profile path and marketplace.

    `profiled_exposed` and `profiled_buyers` are the `profiled_total` row's bases, both None when
    that row is missing (withheld, so written not_returned, or absent).
    """

    path: str
    namespace: SegmentNamespace
    segments: tuple[ProfileSegment, ...]
    profiled_exposed: int | None
    profiled_buyers: int | None

    def __post_init__(self) -> None:
        if (self.profiled_exposed is None) != (self.profiled_buyers is None):
            raise ValueError("a delivery's two profiled totals come back together or not at all.")
        ids = tuple(item.segment_id for item in self.segments)
        if len(set(ids)) != len(ids):
            raise ValueError("a delivery lists each segment once.")

    @property
    def total_returned(self) -> bool:
        """Whether the delivery's `profiled_total` row came back."""
        return self.profiled_exposed is not None

    def segment(self, segment_id: str) -> ProfileSegment | None:
        """Return one returned segment row, or None."""
        return next((item for item in self.segments if item.segment_id == segment_id), None)


@dataclass(frozen=True, slots=True)
class SegmentFigures:
    """One segment's buyer share, reached share and index, or the reason they're absent."""

    buyer_share: Decimal | None
    exposed_share: Decimal | None
    index: Decimal | None
    reason: str | None = None


def segment_figures(delivery: ProfileDelivery, segment_id: str) -> SegmentFigures:
    """Compute one segment's shares and index inside its own delivery (design §6.5).

    The rule chat and the Audiences bake both read. A missing `profiled_total` row makes every
    share and index of the delivery not available; a zero base leaves its share empty, never 0.
    Raises `ValueError` for a segment the delivery didn't return.
    """
    row = delivery.segment(segment_id)
    if row is None:
        raise ValueError(f"segment {segment_id} isn't a returned row of this delivery.")
    if delivery.profiled_exposed is None or delivery.profiled_buyers is None:
        return SegmentFigures(None, None, None, TOTAL_NOT_AVAILABLE_TEXT)
    buyer_share = (
        Decimal(row.buyers) / Decimal(delivery.profiled_buyers)
        if delivery.profiled_buyers
        else None
    )
    exposed_share = (
        Decimal(row.exposed) / Decimal(delivery.profiled_exposed)
        if delivery.profiled_exposed
        else None
    )
    index = (
        buyer_share / exposed_share
        if buyer_share is not None and exposed_share is not None and exposed_share
        else None
    )
    return SegmentFigures(buyer_share, exposed_share, index)


# --- segment-over-index v1 ----------------------------------------------------------------------


def bonferroni_z(family_alpha: Decimal, rows: int) -> Decimal:
    """Return z_S = inv_cdf(1 - alpha / S), the one-sided Bonferroni value over S segment rows."""
    if rows < 1:
        raise ValueError("a Bonferroni value needs at least one segment row.")
    value = NormalDist().inv_cdf(1 - float(family_alpha) / rows)
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class OverIndexFinding:
    """One named segment: its shares inside its delivery and the bound that named it."""

    path: str
    segment_id: str
    buyers: int
    buyer_share: Decimal
    exposed_share: Decimal
    lower_bound: Decimal

    @property
    def index(self) -> Decimal:
        """The segment index, buyer share over reached share."""
        return self.buyer_share / self.exposed_share


def over_index_findings(
    delivery: ProfileDelivery, policy: OverIndexPolicy
) -> tuple[OverIndexFinding, ...]:
    """Name every segment of one delivery that clearly over-indexes (design §6.5, DS-8, DS3-6).

    A segment is named when it holds at least B_min buyers and
    L = (p_b - p_e) - z_S * sqrt(p_b(1 - p_b)/n_b + p_e(1 - p_e)/n_e) > 0, z_S being the
    Bonferroni value over the delivery's S segment rows. Nothing is named without the total row;
    no share is summed and nothing is compared with another delivery. Highest index first.
    """
    if not delivery.segments or not delivery.total_returned:
        return ()
    n_b, n_e = Decimal(delivery.profiled_buyers or 0), Decimal(delivery.profiled_exposed or 0)
    if not n_b or not n_e:
        return ()
    z = bonferroni_z(policy.family_alpha, len(delivery.segments))
    found: list[OverIndexFinding] = []
    for row in delivery.segments:
        if row.buyers < policy.buyers_min:
            continue
        p_b, p_e = Decimal(row.buyers) / n_b, Decimal(row.exposed) / n_e
        if not p_e:
            continue
        spread = (p_b * (1 - p_b) / n_b + p_e * (1 - p_e) / n_e).sqrt()
        bound = (p_b - p_e) - z * spread
        if bound > 0:
            found.append(
                OverIndexFinding(delivery.path, row.segment_id, row.buyers, p_b, p_e, bound)
            )
    return tuple(sorted(found, key=lambda item: (-item.index, item.segment_id)))


def _percent(value: Decimal) -> str:
    return f"{(value * _PERCENT).quantize(_SHARE_QUANTUM):f}%"


@dataclass(frozen=True, slots=True)
class OverIndexSlotRequest:
    """One delivery's named findings that cleared materiality, their labels and the provenance.

    `delivery` is the profile path's delivery for the month, or None when none came back.
    """

    view: str
    path: str
    month: date
    delivery: ProfileDelivery | None
    findings: tuple[OverIndexFinding, ...]
    labels: dict[str, SegmentLabel]
    policy: OverIndexPolicy
    artifact_revision: str
    silence_reason: str
    no_delivery_text: str = NO_DELIVERY_TEXT
    """Why a path with no delivery is ineligible: the paid callout where the paid profile wasn't
    observed (ADR-107), so the card and the view describe that absence alike."""

    def __post_init__(self) -> None:
        if self.path not in DELIVERY_PATHS[self.view]:
            raise ValueError(f"the {self.view} view has no {self.path} delivery.")
        if self.delivery is not None and self.delivery.path != self.path:
            raise ValueError("a segment-over-index card reads its own path's delivery.")
        if any(finding.path != self.path for finding in self.findings):
            # Each card reads one delivery: never two profiled bases in one sentence.
            raise ValueError("a segment-over-index card names its own path's segments only.")


def _silent_slot(
    request: OverIndexSlotRequest, absence: InsightAbsenceClass, reason: str
) -> InsightSlot:
    return InsightSlot(
        InsightSlotId.SEGMENT_OVER_INDEX,
        InsightOutputClass.PERSPECTIVE,
        InsightSlotState.SILENT,
        None,
        None,
        request.artifact_revision,
        _DETERMINISTIC,
        absence_class=absence,
        absence_reason=reason,
    )


def segment_over_index_slot(request: OverIndexSlotRequest) -> InsightSlot:
    """Render `segment-over-index` v1 for one profile path: up to three named segments.

    Deterministic, no prompt or model. One card per path (LLD "Insights"): it names its view and
    delivery and the B_min sentence, and lists its findings highest index first. A path with no
    delivery or no profiled total is ineligible, with its reason; a whole delivery naming nothing
    is evaluated quiet.
    """
    minimum = f"Only segments with at least {request.policy.buyers_min} buyers are named."
    if request.delivery is None:
        return _silent_slot(request, InsightAbsenceClass.INELIGIBLE, request.no_delivery_text)
    if not request.delivery.total_returned:
        return _silent_slot(request, InsightAbsenceClass.INELIGIBLE, TOTAL_NOT_AVAILABLE_TEXT)
    if not request.findings:
        return _silent_slot(request, InsightAbsenceClass.EVALUATED_QUIET, request.silence_reason)
    ranked = sorted(request.findings, key=lambda item: (-item.index, item.segment_id))
    named = [
        f"{page_segment_display(finding.segment_id, request.labels.get(finding.segment_id))}: "
        f"{_percent(finding.buyer_share)} of buyers against "
        f"{_percent(finding.exposed_share)} of the shoppers your ads reached"
        for finding in ranked[:_NAMED_MAX]
    ]
    text = (
        f"{VIEW_LABELS[request.view]} · {DELIVERY_LABELS[request.path]} · "
        f"{month_label(request.month)}: buyers over-index in "
        f"{'; '.join(named)}. {C2_BUYER_LABEL}. {minimum}"
    )
    return InsightSlot(
        InsightSlotId.SEGMENT_OVER_INDEX,
        InsightOutputClass.PERSPECTIVE,
        InsightSlotState.FILLED,
        text,
        _EVIDENCE_LINE,
        request.artifact_revision,
        _DETERMINISTIC,
    )


# --- the profile view -----------------------------------------------------------------------------


class PaidState(StrEnum):
    """Which paid-feature state a profile view renders (ADR-107)."""

    STANDARD = "standard"
    """The DSP view: no paid feature."""
    NOT_OBSERVED = "not_observed"
    """Packs 10-11 delivered nothing in their last three months: the decision-1 callout."""
    OBSERVED = "observed"
    """Packs 10-11 observed; a month not yet published reads its own period state."""


@dataclass(frozen=True, slots=True)
class RankedSegment:
    """One returned segment row as the ranking shows it."""

    path: str
    segment: ProfileSegment
    label: SegmentLabel | None
    figures: SegmentFigures
    shows_index: bool


@dataclass(frozen=True, slots=True)
class RosterGap:
    """A segment with no whole row this month: "Not available" with its reason, never 0.

    Either the roster listed it the month before and it didn't come back (`ROSTER_GAP_TEXT`), or
    one of its planned figures came back and another didn't (`SEGMENT_PARTIAL_TEXT`).
    """

    path: str
    segment_id: str
    label: SegmentLabel | None
    text: str = ROSTER_GAP_TEXT


@dataclass(frozen=True, slots=True)
class DeliveryState:
    """One delivery of a view: its period state (ADR-107 for the paid ones) and its delivery.

    `partial` lists the segments one planned figure returned and another didn't: they are not
    rows of `delivery`, and the view lists them not available.
    """

    path: str
    delivery: ProfileDelivery | None
    paid_state: PaidFeatureState | None = None
    first_month: bool = False
    roster_note: str | None = None
    partial: tuple[str, ...] = ()
    refused_text: str | None = None
    """The path's own refusal sentence (AUD-PROFILE-PATH-REFUSED): it reads no delivery."""

    def __post_init__(self) -> None:
        if self.refused_text is not None and self.delivery is not None:
            raise ValueError("a refused profile path reads no delivery.")
        if self.delivery is not None and self.delivery.path != self.path:
            raise ValueError("a delivery state holds its own path's delivery.")
        if self.delivery is None and self.partial:
            raise ValueError("only a returned delivery has partially returned segments.")
        if self.delivery is not None and any(self.delivery.segment(key) for key in self.partial):
            raise ValueError("a partially returned segment isn't a row of its delivery.")


@dataclass(frozen=True, slots=True)
class ProfileView:
    """One profile option: its state, its deliveries and the ranking built from them.

    `insights` holds one `segment-over-index` card per delivery, in delivery order.
    """

    view: str
    paid: PaidState
    deliveries: tuple[DeliveryState, ...]
    rows: tuple[RankedSegment, ...]
    gaps: tuple[RosterGap, ...]
    insights: tuple[InsightSlot, ...]

    def __post_init__(self) -> None:
        namespace = VIEW_NAMESPACES[self.view]
        if tuple(item.path for item in self.deliveries) != DELIVERY_PATHS[self.view]:
            raise ValueError(f"the {self.view} view reads exactly its own deliveries.")
        if len(self.insights) != len(self.deliveries):
            raise ValueError("a view carries one segment-over-index card per delivery.")
        for item in self.deliveries:
            if item.delivery is not None and item.delivery.namespace is not namespace:
                # P-BO-4: a DSP segment is never listed with, or linked to, an ASI segment.
                raise ValueError(f"the {self.view} view lists {namespace.value} segments only.")
        labels = (row.label for row in self.rows)
        if any(label is not None and label.namespace is not namespace for label in labels):
            raise ValueError(f"the {self.view} view names {namespace.value} segments only.")

    @property
    def available(self) -> bool:
        """Whether any delivery of the view came back for the month."""
        return any(item.delivery is not None for item in self.deliveries)


@dataclass(frozen=True, slots=True)
class ViewRequest:
    """What one view is built from: its deliveries, last month's segments and the labels."""

    view: str
    paid: PaidState
    deliveries: tuple[DeliveryState, ...]
    rosters: dict[str, tuple[str, ...]]
    """Per delivery path, the segments it returned the month before (empty on a first month)."""
    labels: dict[str, SegmentLabel]
    policy: OverIndexPolicy
    insights: tuple[InsightSlot, ...]
    """One `segment-over-index` card per delivery, in delivery order."""


def build_view(request: ViewRequest) -> ProfileView:
    """Rank one view's returned segments by index inside each delivery, gaps listed last.

    Each delivery has its own profiled base, so its rows are ranked among themselves and the
    deliveries follow the view's order: an index is never ranked against another path's.
    """
    rows: list[RankedSegment] = []
    gaps: list[RosterGap] = []
    for state in request.deliveries:
        delivery = state.delivery
        if delivery is None:
            continue
        ranked: list[RankedSegment] = []
        for segment in delivery.segments:
            figures = segment_figures(delivery, segment.segment_id)
            ranked.append(
                RankedSegment(
                    delivery.path,
                    segment,
                    request.labels.get(segment.segment_id),
                    figures,
                    shows_index=figures.index is not None
                    and segment.buyers >= request.policy.buyers_min,
                )
            )
        rows.extend(
            sorted(
                ranked,
                key=lambda row: (
                    not row.shows_index,
                    -(row.figures.index or Decimal(0)),
                    -row.segment.buyers,
                    row.segment.segment_id,
                ),
            )
        )
        gaps.extend(
            RosterGap(delivery.path, key, request.labels.get(key), SEGMENT_PARTIAL_TEXT)
            for key in state.partial
        )
        returned = {segment.segment_id for segment in delivery.segments} | set(state.partial)
        gaps.extend(
            RosterGap(delivery.path, key, request.labels.get(key))
            for key in request.rosters.get(delivery.path, ())
            if key not in returned
        )
    return ProfileView(
        request.view,
        request.paid,
        request.deliveries,
        tuple(rows),
        tuple(gaps),
        request.insights,
    )


# --- campaign audiences (C1) ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CampaignSegment:
    """One DSP campaign's segment row: impressions, CPM and attributed ROAS (pack 8)."""

    campaign_key: str
    campaign_label: str
    segment_id: str
    label: SegmentLabel | None
    impressions: Decimal | None
    cpm: Decimal | None
    dsp_currency: str | None
    cpm_reason: str | None
    roas: Decimal | None
    roas_reason: str | None


@dataclass(frozen=True, slots=True)
class CampaignRowRequest:
    """One campaign and segment's planned figures, each amount with its own currency."""

    campaign: tuple[str, str]
    segment_id: str
    label: SegmentLabel | None
    impressions: Decimal | None
    spend: tuple[Decimal | None, str | None]
    revenue: tuple[Decimal | None, str | None]


def campaign_segment(request: CampaignRowRequest) -> CampaignSegment:
    """Compute CPM and ROAS within one campaign and segment row (pack 8, design §6.5).

    Neither figure is divided by an absent or zero spend, and no amount renders without its
    currency. ROAS is computed only where the sales and spend currencies agree: each comes back in
    its own currency column, so a disagreement or a missing currency reads not available rather
    than a mixed quotient. Every withheld figure carries its reason.
    """
    spend, spend_currency = request.spend
    revenue, revenue_currency = request.revenue
    impressions = request.impressions
    cpm: Decimal | None = None
    roas: Decimal | None = None
    cpm_reason: str | None
    roas_reason: str | None
    if not spend:
        cpm_reason = roas_reason = NO_SPEND_TEXT
    else:
        if not impressions:
            cpm_reason = NO_IMPRESSIONS_TEXT
        elif spend_currency is None:
            cpm_reason = SPEND_CURRENCY_TEXT
        else:
            cpm, cpm_reason = (spend * 1000 / impressions).quantize(_MONEY_QUANTUM), None
        if revenue is None:
            roas_reason = NO_SALES_TEXT
        elif spend_currency is None or spend_currency != revenue_currency:
            roas_reason = ROAS_CURRENCY_TEXT
        else:
            roas, roas_reason = (revenue / spend).quantize(_INDEX_QUANTUM), None
    key, campaign_label = request.campaign
    return CampaignSegment(
        key,
        campaign_label,
        request.segment_id,
        request.label,
        impressions,
        cpm,
        spend_currency if cpm is not None else None,
        cpm_reason,
        roas,
        roas_reason,
    )


def order_campaign_rows(rows: tuple[CampaignSegment, ...]) -> tuple[CampaignSegment, ...]:
    """Order one campaign's rows: impressions descending, withheld impressions last (01 §4).

    Ties read by the segment's page name, then its ID, so the order never depends on the read.
    """
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                row.impressions is None,
                -(row.impressions or Decimal(0)),
                page_segment_display(row.segment_id, row.label),
                row.segment_id,
            ),
        )
    )


def _page_count(items: int) -> int:
    return max(1, math.ceil(items / AUDIENCES_PAGE_ROWS))


def _paging_text(page: int, items: int) -> str | None:
    if items <= AUDIENCES_PAGE_ROWS:
        return None
    first = page * AUDIENCES_PAGE_ROWS + 1
    last = min(items, (page + 1) * AUDIENCES_PAGE_ROWS)
    return _PAGING_TEXT.format(first=f"{first:,}", last=f"{last:,}", total=f"{items:,}")


class CampaignEntryState(StrEnum):
    """One campaign's state in the index."""

    AVAILABLE = "available"
    WITHDRAWN = "withdrawn"
    """Its roster withdraws the grain set: its pages carry the note and no rows."""
    REFUSED = "refused"
    """Its own history gap (AUD-CMP-REFUSED-HISTORY): it has no pages."""


@dataclass(frozen=True, slots=True)
class CampaignIndexEntry:
    """One DSP campaign as the index lists it, by rank: its reference, name, rows and state."""

    ref: str
    label: str
    segment_count: int
    """The segments its pages list: every one any of the three figures returned."""
    state: CampaignEntryState
    text: str | None = None
    """The withdrawal note or the refusal sentence; None when available."""

    def __post_init__(self) -> None:
        if not _CAMPAIGN_REF.fullmatch(self.ref) or not self.label.strip():
            raise ValueError("an Audiences index entry names its reference and label.")
        if self.segment_count < 0:
            raise ValueError("an Audiences index entry's segment count is not negative.")
        if (self.text is None) != (self.state is CampaignEntryState.AVAILABLE):
            raise ValueError("an Audiences index entry carries text exactly when not available.")

    @property
    def page_count(self) -> int:
        """Its 50-row pages: none for a refused campaign, at least one otherwise."""
        if self.state is CampaignEntryState.REFUSED:
            return 0
        return _page_count(self.segment_count)


@dataclass(frozen=True, slots=True)
class CampaignAudiences:
    """Pack 8's month: every DSP campaign's index entry, by rank; or the section's own state."""

    published: bool
    index: tuple[CampaignIndexEntry, ...] = ()
    refused_text: str | None = None
    """AUD-CMP-SECTION-INCOMPLETE when the month's campaign figures aren't complete."""

    def __post_init__(self) -> None:
        if (not self.published or self.refused_text is not None) and self.index:
            raise ValueError("a pending or refused Audiences campaign section lists no campaign.")
        if not self.published and self.refused_text is not None:
            raise ValueError("a pending Audiences campaign section is not refused.")
        if any(
            entry.ref != f"{_CAMPAIGN_REF_PREFIX}{rank}"
            for rank, entry in enumerate(self.index, start=1)
        ):
            raise ValueError("Audiences campaign references number campaigns by rank.")

    @property
    def index_shards(self) -> int:
        """The index's shards; shard 0 rides in the parent."""
        return max(1, math.ceil(len(self.index) / AUDIENCES_INDEX_SHARD_ENTRIES))


def campaign_index_shard(
    campaigns: CampaignAudiences, shard: int
) -> tuple[CampaignIndexEntry, ...]:
    """Return one index shard's entries, in rank order."""
    start = shard * AUDIENCES_INDEX_SHARD_ENTRIES
    return campaigns.index[start : start + AUDIENCES_INDEX_SHARD_ENTRIES]


@dataclass(frozen=True, slots=True)
class CampaignPage:
    """One 50-row page of one campaign: its entry, its page number and its ordered rows."""

    entry: CampaignIndexEntry
    page: int
    rows: tuple[CampaignSegment, ...]

    def __post_init__(self) -> None:
        if not 0 <= self.page < self.entry.page_count:
            raise ValueError("an Audiences campaign page is one of its campaign's pages.")
        if len(self.rows) > AUDIENCES_PAGE_ROWS:
            raise ValueError("an Audiences campaign page holds at most 50 rows.")
        if self.entry.state is CampaignEntryState.WITHDRAWN and self.rows:
            raise ValueError("a withdrawn campaign's pages carry no rows.")


# --- the artifact ---------------------------------------------------------------------------------


class AudiencesOutcome(StrEnum):
    """The page entry's declared outcomes."""

    COMPLETE = "complete"
    EMPTY = "empty"
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True, slots=True)
class AudienceMarket:
    """One marketplace's two profile views, campaign audiences and default view."""

    marketplace_key: str
    paid: ProfileView
    standard: ProfileView
    campaigns: CampaignAudiences

    def __post_init__(self) -> None:
        if (self.paid.view, self.standard.view) != (PAID_VIEW, DSP_VIEW):
            raise ValueError("an Audiences market carries the paid view, then the DSP view.")

    @property
    def default_view(self) -> str:
        """The paid view when packs 10-11 are observed and delivered the month, else DSP."""
        if self.paid.paid is PaidState.OBSERVED and self.paid.available:
            return PAID_VIEW
        return DSP_VIEW

    @property
    def outcome(self) -> AudiencesOutcome:
        """Complete with any shown share, insufficient with deliveries but none, else empty."""
        views = (self.paid, self.standard)
        if not any(view.available for view in views):
            return AudiencesOutcome.EMPTY
        if any(row.figures.buyer_share is not None for view in views for row in view.rows):
            return AudiencesOutcome.COMPLETE
        return AudiencesOutcome.INSUFFICIENT


@dataclass(frozen=True, slots=True)
class AudienceProfileArtifact:
    """`AudienceProfileArtifact` v2 for one complete month. Every text is a server field."""

    month: CalendarPeriod
    markets: tuple[AudienceMarket, ...]
    display_minimum: int
    revision: str
    """The publication's revision, which every part names as its `parent_revision`."""

    def __post_init__(self) -> None:
        if not _PUBLICATION_REVISION.fullmatch(self.revision):
            raise ValueError("an Audiences artifact names its publication revision.")
        if self.month.grain is not TimeGrain.MONTH:
            raise ValueError("an Audiences artifact reads one complete calendar month.")
        keys = tuple(market.marketplace_key for market in self.markets)
        if len(set(keys)) != len(keys):
            raise ValueError("an Audiences artifact carries one section set per marketplace.")

    @property
    def outcome(self) -> AudiencesOutcome:
        """The page entry's declared outcome for the month."""
        outcomes = {market.outcome for market in self.markets}
        for outcome in (AudiencesOutcome.COMPLETE, AudiencesOutcome.INSUFFICIENT):
            if outcome in outcomes:
                return outcome
        return AudiencesOutcome.EMPTY

    @property
    def month_label(self) -> str:
        """'September 2026'."""
        return month_label(self.month.start)

    @property
    def starter_questions(self) -> tuple[str, ...]:
        """Starter questions read one profile view each and never link the two kinds of IDs."""
        return (
            f"Which DSP behaviour segments did {self.month_label}'s buyers over-index in?",
            f"Which DSP campaigns reached which segments in {self.month_label}?",
        )


def _decimal(value: Decimal | None, quantum: Decimal = _INDEX_QUANTUM) -> str | None:
    return None if value is None else f"{value.quantize(quantum):f}"


def _share(value: Decimal | None) -> dict[str, str] | None:
    return (
        None
        if value is None
        else {"value": _decimal(value, Decimal("0.0001")) or "", "text": _percent(value)}
    )


def _page_provider(label: SegmentLabel | None) -> str | None:
    """Return the page's provider label: none where the classification doesn't cover it (LLD)."""
    return None if label is None or label.provider is None else provider_label(label.provider)


def page_segment_display(segment_id: str, label: SegmentLabel | None) -> str:
    """Name a segment as the Audiences page does (docs/ui/audiences, revision 2026-10-09.2).

    As `SegmentLabel.display`, except that a segment no provider rule covers carries no provider
    label rather than "Provider not recorded"; one with no governed name reads by its ID.
    """
    provider = _page_provider(label)
    if provider is not None and label is not None:
        return label.display
    name = None if label is None else label.name
    if name is not None and name.strip() and len(name) <= MAX_LABEL_LENGTH:
        return name
    return f"Segment {segment_id} ({ASI_NAMES_NOT_AVAILABLE_TEXT})"


def _segment_names(segment_id: str, label: SegmentLabel | None) -> dict[str, object]:
    named = label is not None and label.name is not None and bool(label.name.strip())
    return {
        "segment_id": segment_id,
        "name": label.name if named and label is not None else None,
        "display_name": page_segment_display(segment_id, label),
        "provider_label": _page_provider(label),
        "category_level_1": None if label is None else label.category_level_1,
        "category_path": None if label is None else label.category_path,
    }


def _count(value: int | Decimal | None) -> str | None:
    return None if value is None else f"{value:f}" if isinstance(value, Decimal) else str(value)


def _multiplier(value: Decimal | None) -> str | None:
    return None if value is None else f"{value.quantize(_INDEX_QUANTUM):f}x"


def _money(value: Decimal | None, currency: str | None) -> str | None:
    return (
        None
        if value is None or currency is None
        else f"{value.quantize(_MONEY_QUANTUM):f} {currency}"
    )


def _row_payload(row: RankedSegment, minimum: int) -> dict[str, object]:
    figures = row.figures
    if figures.reason is not None:
        state, text = "not_available", figures.reason
    elif not row.shows_index:
        state, text = "below_minimum", BELOW_MINIMUM_TEXT.format(minimum=minimum)
    else:
        state, text = "value", None
    return {
        **_segment_names(row.segment.segment_id, row.label),
        "profile_path": row.path,
        "state": state,
        "text": text,
        "segment_buyers": row.segment.buyers,
        "segment_buyers_text": _count(row.segment.buyers),
        "segment_exposed_shoppers": row.segment.exposed,
        "segment_buyers_new_to_brand": row.segment.buyers_new_to_brand,
        "segment_buyers_new_to_brand_text": _count(row.segment.buyers_new_to_brand),
        "segment_buyer_share": _share(figures.buyer_share),
        "segment_exposed_share": _share(figures.exposed_share),
        "segment_index": _decimal(figures.index) if row.shows_index else None,
        "segment_index_text": _multiplier(figures.index) if row.shows_index else None,
    }


def _paid_payload(state: PaidFeatureState | None) -> dict[str, object] | None:
    if state is None:
        return None
    latest = state.latest_available_period
    return {
        "feature_observed": state.feature_observed,
        "period_published": state.period_published,
        "expected_after": state.expected_after.isoformat(),
        "latest_available_period": None
        if latest is None
        else {"start": latest.start.isoformat(), "end": latest.end.isoformat()},
    }


def _path_items(view: ProfileView, path: str) -> tuple[RankedSegment | RosterGap, ...]:
    """One path's rows, then its "Not available" segments: the order its pages list them."""
    return (
        *(row for row in view.rows if row.path == path),
        *(gap for gap in view.gaps if gap.path == path),
    )


def profile_page_count(view: ProfileView, path: str) -> int:
    """Count a returned path's 50-row pages (page 0 rides in the parent); 0 if it didn't return."""
    state = next(item for item in view.deliveries if item.path == path)
    return 0 if state.delivery is None else _page_count(len(_path_items(view, path)))


def _gap_payload(gap: RosterGap) -> dict[str, object]:
    return {
        **_segment_names(gap.segment_id, gap.label),
        "profile_path": gap.path,
        "state": "not_available",
        "text": gap.text,
    }


def _profile_page(  # noqa: PLR0913, PLR0917 -- one view's path, its page and the minimum
    view: ProfileView, path: str, page: int, minimum: int
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    items = _path_items(view, path)[page * AUDIENCES_PAGE_ROWS : (page + 1) * AUDIENCES_PAGE_ROWS]
    return (
        [_row_payload(item, minimum) for item in items if isinstance(item, RankedSegment)],
        [_gap_payload(item) for item in items if isinstance(item, RosterGap)],
    )


def _view_payload(view: ProfileView, month: str, minimum: int) -> dict[str, object]:
    first = {item.path: _profile_page(view, item.path, 0, minimum) for item in view.deliveries}
    return {
        "view": view.view,
        "label": VIEW_LABELS[view.view],
        "paid_state": view.paid.value,
        "callout": ASI_NEEDS_PAID_FEATURE_TEXT if view.paid is PaidState.NOT_OBSERVED else None,
        "caption": f"{VIEW_LABELS[view.view]} · {month} · {SEGMENT_FIGURES_NEVER_ADD_UP}",
        "segment_ids_sentence": SEGMENT_IDS_SENTENCE,
        "buyer_label": C2_BUYER_LABEL,
        "display_minimum_text": f"Only segments with at least {minimum} buyers carry an index.",
        "deliveries": [
            {
                "profile_path": item.path,
                "label": DELIVERY_LABELS[item.path],
                "returned": item.delivery is not None,
                "total_returned": item.delivery is not None and item.delivery.total_returned,
                "total_text": None
                if item.delivery is None or item.delivery.total_returned
                else TOTAL_NOT_AVAILABLE_TEXT,
                "profiled_buyers_total": None
                if item.delivery is None
                else item.delivery.profiled_buyers,
                "profiled_buyers_total_text": None
                if item.delivery is None
                else _count(item.delivery.profiled_buyers),
                "profiled_exposed_total": None
                if item.delivery is None
                else item.delivery.profiled_exposed,
                "profiled_exposed_total_text": None
                if item.delivery is None
                else _count(item.delivery.profiled_exposed),
                "first_month_note": FIRST_MONTH_NOTE if item.first_month else None,
                "roster_note": item.roster_note,
                "paid_state": _paid_payload(item.paid_state),
                "period_text": None,
                "insights": insight_slots_payload((insight,)),
                "page_count": profile_page_count(view, item.path),
                "paging_text": _paging_text(0, len(_path_items(view, item.path))),
            }
            for item, insight in zip(view.deliveries, view.insights, strict=True)
        ],
        "rows": [row for item in view.deliveries for row in first[item.path][0]],
        "not_available": [gap for item in view.deliveries for gap in first[item.path][1]],
    }


def audiences_profile_page_payload(  # noqa: PLR0913, PLR0917 -- a view, path, page and its parent
    view: ProfileView, path: str, page: int, minimum: int, revision: str
) -> dict[str, object]:
    """One profile path's page `page` (>= 1), under its parent's revision."""
    if not 1 <= page < profile_page_count(view, path):
        raise ValueError("an Audiences profile page is one of its path's later pages.")
    state = next(item for item in view.deliveries if item.path == path)
    rows, gaps = _profile_page(view, path, page, minimum)
    return {
        "type": AUDIENCES_PROFILE_PAGE_TYPE,
        "version": AUDIENCES_PART_VERSION,
        "parent_revision": revision,
        "profile_path": path,
        "page": page,
        "page_count": profile_page_count(view, path),
        "paging_text": _paging_text(page, len(_path_items(view, path))),
        "total_returned": state.delivery is not None and state.delivery.total_returned,
        "rows": rows,
        "not_available": gaps,
    }


_CAMPAIGN_BASIS_LABEL: Final = (
    "DSP-attributed sales \u00f7 DSP spend \u00b7 14-day attribution, conversion date"
)


def _index_entry_payload(entry: CampaignIndexEntry) -> dict[str, object]:
    return {
        "campaign_ref": entry.ref,
        "campaign_label": entry.label,
        "segment_count": entry.segment_count,
        "page_count": entry.page_count,
        "state": entry.state.value,
        "text": entry.text,
    }


def _campaign_payload(campaigns: CampaignAudiences, month: str) -> dict[str, object]:
    return {
        "limit_text": CAMPAIGN_LIMIT_TEXT,
        "pending_text": None if campaigns.published else CAMPAIGN_PENDING_TEXT.format(month=month),
        "refused_text": campaigns.refused_text,
        "segment_ids_sentence": SEGMENT_IDS_SENTENCE,
        "basis_label": _CAMPAIGN_BASIS_LABEL,
        "campaign_count": len(campaigns.index),
        "index_shards": campaigns.index_shards,
        "index": [_index_entry_payload(entry) for entry in campaign_index_shard(campaigns, 0)],
    }


def audiences_campaign_index_payload(
    campaigns: CampaignAudiences, shard: int, revision: str
) -> dict[str, object]:
    """One campaign index shard (>= 1), under its parent's revision."""
    if not 1 <= shard < campaigns.index_shards:
        raise ValueError("an Audiences index part is one of its later shards.")
    return {
        "type": AUDIENCES_CAMPAIGN_INDEX_TYPE,
        "version": AUDIENCES_PART_VERSION,
        "parent_revision": revision,
        "shard": shard,
        "entries": [
            _index_entry_payload(entry) for entry in campaign_index_shard(campaigns, shard)
        ],
    }


def _campaign_row_payload(row: CampaignSegment) -> dict[str, object]:
    return {
        **_segment_names(row.segment_id, row.label),
        "segment_impressions": None if row.impressions is None else int(row.impressions),
        "segment_impressions_text": _count(row.impressions),
        "segment_cpm": None
        if row.cpm is None
        else {
            "value": _decimal(row.cpm, _MONEY_QUANTUM),
            "currency": row.dsp_currency,
            "text": _money(row.cpm, row.dsp_currency),
        },
        "cpm_text": row.cpm_reason,
        "segment_roas_attributed": _decimal(row.roas),
        "segment_roas_attributed_text": _multiplier(row.roas),
        "roas_text": row.roas_reason,
    }


def audiences_campaign_page_payload(page: CampaignPage, revision: str) -> dict[str, object]:
    """One campaign's page, under its parent's revision."""
    entry = page.entry
    return {
        "type": AUDIENCES_CAMPAIGN_PAGE_TYPE,
        "version": AUDIENCES_PART_VERSION,
        "parent_revision": revision,
        "campaign_ref": entry.ref,
        "campaign_label": entry.label,
        "page": page.page,
        "page_count": entry.page_count,
        "paging_text": None
        if entry.state is CampaignEntryState.WITHDRAWN
        else _paging_text(page.page, entry.segment_count),
        "state": entry.state.value,
        "text": entry.text,
        "rows": [_campaign_row_payload(row) for row in page.rows],
    }


def _evidence_status(artifact: AudienceProfileArtifact) -> dict[str, object]:
    """`EvidenceStatus.v2`: every delivery the month planned that didn't come back."""
    month = artifact.month
    slots = [
        {
            "slot_label": f"{VIEW_LABELS[view.view]} · {DELIVERY_LABELS[item.path]}",
            "role_label": "This month",
            "missing_dates": [{"start": month.start.isoformat(), "end": month.end.isoformat()}],
            "reason_text": item.refused_text or NO_DELIVERY_TEXT,
            "recovery_text": None,
        }
        for market in artifact.markets
        for view in (market.paid, market.standard)
        if view.paid is not PaidState.NOT_OBSERVED
        for item in view.deliveries
        if item.delivery is None
    ]
    return {"version": 2, "slots": slots}


def audiences_artifact_payload(artifact: AudienceProfileArtifact) -> dict[str, object]:
    """Serialize the parent body; the UI's strict v2 decoder reads exactly this."""
    month = artifact.month_label
    minimum = artifact.display_minimum
    return {
        "type": AUDIENCES_ARTIFACT_TYPE,
        "version": AUDIENCES_ARTIFACT_VERSION,
        "revision": artifact.revision,
        "outcome": artifact.outcome.value,
        "month": {
            "start": artifact.month.start.isoformat(),
            "end": artifact.month.end.isoformat(),
            "label": month,
        },
        "display_minimum": minimum,
        "markets": [
            {
                "outcome": market.outcome.value,
                "default_view": market.default_view,
                "views": [
                    _view_payload(market.paid, month, minimum),
                    _view_payload(market.standard, month, minimum),
                ],
                "campaign_audiences": _campaign_payload(market.campaigns, month),
            }
            for market in artifact.markets
        ],
        "boundary": [BOUNDARY_TEXT, SEGMENT_IDS_SENTENCE],
        "evidence_status": _evidence_status(artifact),
        "page_context_label": f"Audiences · {month}",
        "page_context_expires_at": None,
        "starter_questions": list(artifact.starter_questions),
    }


def _record(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(cast("dict[object, object]", value)) != keys:
        raise ValueError("the Audiences artifact has invalid fields.")
    return cast("dict[str, object]", value)


def _items(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("the Audiences artifact has an invalid roster.")
    return cast("list[object]", value)


def _text(value: object) -> str:
    if not isinstance(value, str) or _BLANK.fullmatch(value):
        raise ValueError("the Audiences artifact requires nonblank text.")
    return value


def _nullable_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _decimal_text(value: object, places: int, *, signed: bool = False) -> Decimal:
    """Read the serializer's own fixed-point text (`_decimal`) and nothing else.

    The browser decoder applies the same pattern, so an agreeing-but-noncanonical pair such as
    1.333/1.333x or 7/7 USD is refused on both sides (docs: audiences-parity inventory).
    """
    raw = _text(value)
    sign = "-?" if signed else ""
    # At most _MAX_DECIMAL_DIGITS digits, the most the serializer's quantize can write, so a
    # longer coefficient is refused here rather than raising InvalidOperation on requantizing.
    integer = rf"(0|[1-9][0-9]{{0,{_MAX_DECIMAL_DIGITS - places - 1}}})"
    if not re.fullmatch(rf"{sign}{integer}\.[0-9]{{{places}}}", raw):
        raise ValueError("the Audiences artifact has noncanonical decimal text.")
    return Decimal(raw)


def _safe_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_SAFE_INTEGER:
        raise ValueError("an Audiences count is invalid.")
    return value


def _iso_date(value: object) -> date:
    raw = _text(value)
    if not _ISO_DATE.fullmatch(raw):
        raise ValueError("an Audiences date is not an ISO calendar date.")
    return date.fromisoformat(raw)


def _validate_count_pair(count: object, display: object, *, required: bool = False) -> int | None:
    if count is None:
        if display is not None or required:
            raise ValueError("an absent Audiences count carries no display text.")
        return None
    if display != str(_safe_count(count)):
        raise ValueError("an Audiences count display does not match its value.")
    return cast("int", count)


def _validate_names(row: dict[str, object]) -> None:
    """Validate the segment naming fields the page reads, as `_segment_names` writes them."""
    _text(row["segment_id"])
    _nullable_text(row["name"])
    _text(row["display_name"])
    _nullable_text(row["category_level_1"])
    _nullable_text(row["category_path"])
    if row["provider_label"] is not None and row["provider_label"] not in _PAGE_PROVIDER_LABELS:
        raise ValueError("an Audiences segment has an invalid provider label.")


def _validate_share(value: object) -> Decimal | None:
    if value is None:
        return None
    share = _record(value, {"text", "value"})
    parsed = _decimal_text(share["value"], 4)
    if parsed > 1 or share["text"] != _percent(parsed):
        raise ValueError("an Audiences share display does not match its value.")
    return parsed


def _validate_insight(value: object, served: bool) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("an Audiences insight is invalid.")
    mapping = cast("dict[object, object]", value)
    state = mapping.get("state")
    required = {
        "artifact_revision",
        "evidence_line",
        "id",
        "output_class",
        "prompt_version",
        "state",
        "text",
    }
    if state == "silent":
        required |= {"absence_class", "absence_reason"}
    insight = _record(mapping, required)
    if insight["id"] != "segment-over-index" or insight["output_class"] != "perspective":
        raise ValueError("an Audiences insight has an invalid identity.")
    revision = _text(insight["artifact_revision"])
    _text(insight["prompt_version"])
    if served and not _SERVED_REVISION.fullmatch(revision):
        raise ValueError("a served Audiences insight is bound to its page reference.")
    if state == "filled":
        _text(insight["text"])
        _text(insight["evidence_line"])
    elif state == "silent":
        if insight["text"] is not None or insight["evidence_line"] is not None:
            raise ValueError("a silent Audiences insight carries no narration.")
        _text(insight["absence_class"])
        _text(insight["absence_reason"])
    else:
        raise ValueError("an Audiences insight has an invalid state.")
    return insight


def _validate_paid_state(value: object) -> dict[str, object]:
    state = _record(
        value,
        {"expected_after", "feature_observed", "latest_available_period", "period_published"},
    )
    if not isinstance(state["feature_observed"], bool) or not isinstance(
        state["period_published"], bool
    ):
        raise ValueError("an Audiences paid state has invalid flags.")
    _iso_date(state["expected_after"])
    latest = state["latest_available_period"]
    if latest is not None:
        period = _record(latest, {"end", "start"})
        if _iso_date(period["end"]) < _iso_date(period["start"]):
            raise ValueError("an Audiences paid state has an invalid latest period.")
    elif state["period_published"]:
        # `PaidFeatureState`'s invariant, stated here so both boundaries refuse it alike.
        raise ValueError("a published Audiences paid period names its latest period.")
    return state


def _served_period_texts(paid: dict[str, object], month: str) -> set[str | None]:
    """Return the period texts `resolve_audiences_payload` can serve for one baked state."""
    baked = PaidFeatureState(
        feature_observed=cast("bool", paid["feature_observed"]),
        period_published=cast("bool", paid["period_published"]),
        expected_after=date.fromisoformat(cast("str", paid["expected_after"])),
        latest_available_period=_optional_range(paid["latest_available_period"]),
    )
    if baked.period_published:
        return {None}
    if not baked.feature_observed:
        return {ASI_NEEDS_PAID_FEATURE_TEXT}
    return {
        _paid_period_text(display, month, baked)
        for display in (PaidFeatureDisplay.PENDING, PaidFeatureDisplay.MISSING)
    }


@dataclass(frozen=True, slots=True)
class _DeliveryShape:
    """What a validated delivery tells its rows: whether it and its total came back."""

    returned: bool
    total_returned: bool


def _validate_delivery(  # noqa: PLR0912, PLR0913, PLR0917 -- a delivery, its path, view, month
    raw: object, expected_path: str, paid_view: dict[str, object] | None, month: str | None
) -> _DeliveryShape:
    """Validate one delivery; `paid_view` is the paid view's state, `month` set when served."""
    delivery = _record(
        raw,
        {
            "first_month_note",
            "insights",
            "label",
            "page_count",
            "paging_text",
            "paid_state",
            "period_text",
            "profile_path",
            "profiled_buyers_total",
            "profiled_buyers_total_text",
            "profiled_exposed_total",
            "profiled_exposed_total_text",
            "returned",
            "roster_note",
            "total_returned",
            "total_text",
        },
    )
    if delivery["profile_path"] != expected_path:
        raise ValueError("Audiences deliveries are missing or reordered.")
    returned, total_returned = delivery["returned"], delivery["total_returned"]
    if not isinstance(returned, bool) or not isinstance(total_returned, bool):
        raise ValueError("an Audiences delivery has invalid availability.")
    _text(delivery["label"])
    _nullable_text(delivery["first_month_note"])
    _nullable_text(delivery["roster_note"])
    buyers_total = _validate_count_pair(
        delivery["profiled_buyers_total"], delivery["profiled_buyers_total_text"]
    )
    exposed_total = _validate_count_pair(
        delivery["profiled_exposed_total"], delivery["profiled_exposed_total_text"]
    )
    totals = (buyers_total is not None, exposed_total is not None)
    if total_returned and not returned:
        raise ValueError("an unavailable Audiences delivery carries no returned totals.")
    if totals != (total_returned, total_returned):
        raise ValueError("an Audiences delivery carries both totals exactly when they came back.")
    expected_total_text = TOTAL_NOT_AVAILABLE_TEXT if returned and not total_returned else None
    if delivery["total_text"] != expected_total_text:
        raise ValueError("an Audiences delivery's total text does not match its total.")
    raw_paid = delivery["paid_state"]
    period_text = _nullable_text(delivery["period_text"])
    if paid_view is None:
        if raw_paid is not None or period_text is not None:
            raise ValueError("a DSP delivery carries no paid-period state.")
    else:
        if raw_paid is None:
            raise ValueError("a paid delivery requires paid-period state.")
        paid = _validate_paid_state(raw_paid)
        if cast("bool", paid["feature_observed"]) != (
            paid_view["paid_state"] == PaidState.OBSERVED.value
        ):
            raise ValueError("a paid delivery does not match its view availability.")
        if month is not None and period_text not in _served_period_texts(paid, month):
            raise ValueError("a served paid delivery's period text does not match its state.")
    insights = _items(delivery["insights"])
    if len(insights) != 1:
        raise ValueError("an Audiences delivery carries one insight.")
    insight = _validate_insight(insights[0], month is not None)
    if not total_returned and insight["state"] != "silent":
        raise ValueError("an Audiences delivery without its total carries a silent insight.")
    if returned and not total_returned and insight["absence_reason"] != TOTAL_NOT_AVAILABLE_TEXT:
        raise ValueError("an Audiences delivery without its total says so in its insight.")
    page_count = _safe_count(delivery["page_count"])
    paging = _nullable_text(delivery["paging_text"])
    if (page_count >= 1) != returned or (paging is not None) != (page_count > 1):
        raise ValueError("an Audiences delivery pages exactly its returned rows.")
    return _DeliveryShape(returned, total_returned)


def _validate_profile_row(
    raw: object, expected_paths: tuple[str, ...], shapes: dict[str, _DeliveryShape]
) -> str:
    """Validate one ranked segment row; return its profile path."""
    row = _record(
        raw,
        {
            "category_level_1",
            "category_path",
            "display_name",
            "name",
            "profile_path",
            "provider_label",
            "segment_buyer_share",
            "segment_buyers",
            "segment_buyers_new_to_brand",
            "segment_buyers_new_to_brand_text",
            "segment_buyers_text",
            "segment_exposed_share",
            "segment_exposed_shoppers",
            "segment_id",
            "segment_index",
            "segment_index_text",
            "state",
            "text",
        },
    )
    path = _text(row["profile_path"])
    if path not in expected_paths or not shapes[path].returned:
        raise ValueError("an Audiences row belongs to a returned delivery of its view.")
    _validate_names(row)
    _validate_count_pair(row["segment_buyers"], row["segment_buyers_text"], required=True)
    _validate_count_pair(
        row["segment_buyers_new_to_brand"], row["segment_buyers_new_to_brand_text"], required=True
    )
    _safe_count(row["segment_exposed_shoppers"])
    buyer_share = _validate_share(row["segment_buyer_share"])
    exposed_share = _validate_share(row["segment_exposed_share"])
    raw_index = row["segment_index"]
    index_text = _nullable_text(row["segment_index_text"])
    index = None if raw_index is None else _decimal_text(raw_index, 2)
    if (index is None) != (index_text is None) or (
        index is not None and index_text != _multiplier(index)
    ):
        raise ValueError("an Audiences index display does not match its value.")
    state = _text(row["state"])
    row_text = _nullable_text(row["text"])
    shares_present = buyer_share is not None and exposed_share is not None
    if state == "value":
        valid_state = shares_present and index is not None and row_text is None
    elif state == "below_minimum":
        valid_state = shares_present and index is None and row_text is not None
    elif state == "not_available":
        valid_state = (
            buyer_share is None
            and exposed_share is None
            and index is None
            and row_text == TOTAL_NOT_AVAILABLE_TEXT
        )
    else:
        valid_state = False
    if not valid_state or (state == "not_available") == shapes[path].total_returned:
        raise ValueError("an Audiences row has inconsistent availability fields.")
    return path


def _validate_view(value: object, expected_view: str, month: str | None) -> bool:
    """Validate one profile view; `month` is set when served. Return whether any delivery came."""
    view = _record(
        value,
        {
            "buyer_label",
            "callout",
            "caption",
            "deliveries",
            "display_minimum_text",
            "label",
            "not_available",
            "paid_state",
            "rows",
            "segment_ids_sentence",
            "view",
        },
    )
    if view["view"] != expected_view:
        raise ValueError("Audiences views are missing or reordered.")
    expected_paths = DELIVERY_PATHS[expected_view]
    paid_state = _text(view["paid_state"])
    allowed = (
        {PaidState.STANDARD.value}
        if expected_view == DSP_VIEW
        else {PaidState.NOT_OBSERVED.value, PaidState.OBSERVED.value}
    )
    if paid_state not in allowed:
        raise ValueError("an Audiences view has invalid paid state.")
    callout = ASI_NEEDS_PAID_FEATURE_TEXT if paid_state == PaidState.NOT_OBSERVED.value else None
    if view["callout"] != callout:
        raise ValueError("an Audiences view callout does not match its state.")
    if view["segment_ids_sentence"] != SEGMENT_IDS_SENTENCE:
        raise ValueError("an Audiences view carries the pinned segment-ID sentence.")
    for key in ("label", "caption", "buyer_label", "display_minimum_text"):
        _text(view[key])
    deliveries = _items(view["deliveries"])
    if len(deliveries) != len(expected_paths):
        raise ValueError("an Audiences view has the wrong delivery roster.")
    paid_view = None if expected_view == DSP_VIEW else view
    shapes = {
        path: _validate_delivery(raw, path, paid_view, month)
        for raw, path in zip(deliveries, expected_paths, strict=True)
    }
    paths = _validate_rows(view, expected_paths, shapes)
    if any(paths.count(path) > AUDIENCES_PAGE_ROWS for path in expected_paths):
        raise ValueError("an Audiences parent carries at most one page of each path.")
    return any(shape.returned for shape in shapes.values())


def _validate_rows(
    page: dict[str, object], expected_paths: tuple[str, ...], shapes: dict[str, _DeliveryShape]
) -> list[str]:
    """Validate a page's rows, then its "Not available" segments; return every item's path."""
    paths: list[str] = []
    last_path = -1
    for raw in _items(page["rows"]):
        path = _validate_profile_row(raw, expected_paths, shapes)
        if expected_paths.index(path) < last_path:
            raise ValueError("Audiences rows do not follow delivery order.")
        last_path = expected_paths.index(path)
        paths.append(path)
    last_path = -1
    for raw in _items(page["not_available"]):
        row = _record(
            raw,
            {
                "category_level_1",
                "category_path",
                "display_name",
                "name",
                "profile_path",
                "provider_label",
                "segment_id",
                "state",
                "text",
            },
        )
        path = _text(row["profile_path"])
        if path not in expected_paths or not shapes[path].returned:
            raise ValueError("an unavailable Audiences row belongs to a returned delivery.")
        if expected_paths.index(path) < last_path:
            raise ValueError("Audiences unavailable rows do not follow delivery order.")
        last_path = expected_paths.index(path)
        if row["state"] != "not_available" or row["text"] not in (
            ROSTER_GAP_TEXT,
            SEGMENT_PARTIAL_TEXT,
        ):
            raise ValueError("an unavailable Audiences row has its state and a server reason.")
        _validate_names(row)
        paths.append(path)
    return paths


def _validate_campaign_row(raw: object) -> None:
    row = _record(
        raw,
        {
            "category_level_1",
            "category_path",
            "cpm_text",
            "display_name",
            "name",
            "provider_label",
            "roas_text",
            "segment_cpm",
            "segment_id",
            "segment_impressions",
            "segment_impressions_text",
            "segment_roas_attributed",
            "segment_roas_attributed_text",
        },
    )
    _validate_names(row)
    _validate_count_pair(row["segment_impressions"], row["segment_impressions_text"])
    cpm = row["segment_cpm"]
    if cpm is None:
        _text(row["cpm_text"])
    else:
        amount = _record(cpm, {"currency", "text", "value"})
        currency = _text(amount["currency"])
        parsed_amount = _decimal_text(amount["value"], 2, signed=True)
        if amount["text"] != _money(parsed_amount, currency) or row["cpm_text"] is not None:
            raise ValueError("an Audiences CPM display does not match its value.")
    raw_roas = row["segment_roas_attributed"]
    roas_display = _nullable_text(row["segment_roas_attributed_text"])
    roas = None if raw_roas is None else _decimal_text(raw_roas, 2, signed=True)
    if (roas is None) != (roas_display is None) or (
        roas is not None and roas_display != _multiplier(roas)
    ):
        raise ValueError("an Audiences ROAS display does not match its value.")
    if roas is None:
        _text(row["roas_text"])
    elif row["roas_text"] is not None:
        raise ValueError("an available Audiences ROAS carries no absence text.")


def _validate_index_entries(raw_entries: object, first_rank: int) -> int:
    """Validate index entries numbered from `first_rank`; return how many there are."""
    entries = _items(raw_entries)
    for rank, raw in enumerate(entries, start=first_rank):
        entry = _record(
            raw,
            {"campaign_label", "campaign_ref", "page_count", "segment_count", "state", "text"},
        )
        if entry["campaign_ref"] != f"{_CAMPAIGN_REF_PREFIX}{rank}":
            raise ValueError("Audiences campaign references number campaigns by rank.")
        state = CampaignEntryState(_text(entry["state"]))
        parsed = CampaignIndexEntry(
            cast("str", entry["campaign_ref"]),
            _text(entry["campaign_label"]),
            _safe_count(entry["segment_count"]),
            state,
            _nullable_text(entry["text"]),
        )
        if _safe_count(entry["page_count"]) != parsed.page_count:
            raise ValueError("an Audiences index entry's page count does not match its rows.")
    return len(entries)


def _validate_campaigns(value: object) -> None:
    campaigns = _record(
        value,
        {
            "basis_label",
            "campaign_count",
            "index",
            "index_shards",
            "limit_text",
            "pending_text",
            "refused_text",
            "segment_ids_sentence",
        },
    )
    _text(campaigns["limit_text"])
    _text(campaigns["basis_label"])
    if campaigns["segment_ids_sentence"] != SEGMENT_IDS_SENTENCE:
        raise ValueError("Audiences campaigns carry the pinned segment-ID sentence.")
    count = _safe_count(campaigns["campaign_count"])
    shards = _safe_count(campaigns["index_shards"])
    pending = _nullable_text(campaigns["pending_text"])
    refused = _nullable_text(campaigns["refused_text"])
    listed = _validate_index_entries(campaigns["index"], 1)
    if (pending is not None or refused is not None) and count:
        raise ValueError("a pending or refused Audiences campaign section lists no campaign.")
    if pending is not None and refused is not None:
        raise ValueError("a pending Audiences campaign section is not refused.")
    if shards != max(1, math.ceil(count / AUDIENCES_INDEX_SHARD_ENTRIES)) or listed != min(
        count, AUDIENCES_INDEX_SHARD_ENTRIES
    ):
        raise ValueError("an Audiences parent carries the index's first shard and its count.")


class AudiencesPart(StrEnum):
    """The parts one Audiences publication serves beside its parent."""

    CAMPAIGN = "campaign"
    PROFILE = "profile"
    CAMPAIGN_INDEX = "campaign_index"


_PART_TYPES: Final = {
    AudiencesPart.CAMPAIGN: AUDIENCES_CAMPAIGN_PAGE_TYPE,
    AudiencesPart.PROFILE: AUDIENCES_PROFILE_PAGE_TYPE,
    AudiencesPart.CAMPAIGN_INDEX: AUDIENCES_CAMPAIGN_INDEX_TYPE,
}
_PART_ORDERINGS: Final = {
    AudiencesPart.CAMPAIGN: "impressions-desc",
    AudiencesPart.PROFILE: "index-desc",
    AudiencesPart.CAMPAIGN_INDEX: "rank",
}


def audiences_part_identity(
    part: AudiencesPart, page: int, subject: str = ""
) -> PresetArtifactSiblingIdentity:
    """Return one part's sibling identity: the bake writes it and the serve reads it (01 §4).

    `subject` is the campaign reference (`campaign-<n>`) or the profile path; an index shard
    has none.
    """
    match part:
        case AudiencesPart.CAMPAIGN:
            if not _CAMPAIGN_REF.fullmatch(subject):
                raise ValueError("a campaign part is named by its campaign reference.")
            filter_id = subject
        case AudiencesPart.PROFILE:
            if subject not in DELIVERY_LABELS:
                raise ValueError("a profile part is named by its profile path.")
            filter_id = f"profile-{subject}"
        case AudiencesPart.CAMPAIGN_INDEX:
            filter_id = "campaign-index"
    return PresetArtifactSiblingIdentity(_PART_ORDERINGS[part], page, filter_id)


_PART_KEYS: Final = {
    AudiencesPart.CAMPAIGN: {
        "campaign_label",
        "campaign_ref",
        "page",
        "page_count",
        "paging_text",
        "rows",
        "state",
        "text",
    },
    AudiencesPart.PROFILE: {
        "not_available",
        "page",
        "page_count",
        "paging_text",
        "profile_path",
        "rows",
        "total_returned",
    },
    AudiencesPart.CAMPAIGN_INDEX: {"entries", "shard"},
}


def require_audiences_part_payload(  # noqa: PLR0912 -- strict matrix, per part
    value: object, part: AudiencesPart, parent_revision: str
) -> dict[str, object]:
    """Refuse a part that isn't exactly what this module writes, under `parent_revision`.

    The browser decoder (frontend/lib/audiences.ts) applies the same rules to the served part.
    """
    payload = _record(value, {"parent_revision", "type", "version", *_PART_KEYS[part]})
    if (
        payload["type"] != _PART_TYPES[part]
        or isinstance(payload["version"], bool)
        or payload["version"] != AUDIENCES_PART_VERSION
        or payload["parent_revision"] != parent_revision
    ):
        raise ValueError("an Audiences part has an invalid identity.")
    if part is AudiencesPart.CAMPAIGN_INDEX:
        shard = _safe_count(payload["shard"])
        if shard < 1:
            raise ValueError("an Audiences index part is a later shard.")
        if (
            not 1
            <= _validate_index_entries(
                payload["entries"], shard * AUDIENCES_INDEX_SHARD_ENTRIES + 1
            )
            <= AUDIENCES_INDEX_SHARD_ENTRIES
        ):
            raise ValueError("an Audiences index part holds one shard's entries.")
        return payload
    page = _safe_count(payload["page"])
    page_count = _safe_count(payload["page_count"])
    paging = _nullable_text(payload["paging_text"])
    if not page < page_count:
        raise ValueError("an Audiences part is one of its pages.")
    if part is AudiencesPart.PROFILE:
        path = _text(payload["profile_path"])
        total_returned = payload["total_returned"]
        if page < 1 or path not in DELIVERY_PATHS[PAID_VIEW] + DELIVERY_PATHS[DSP_VIEW]:
            raise ValueError("an Audiences profile part is a later page of a profile path.")
        if not isinstance(total_returned, bool) or paging is None:
            raise ValueError("an Audiences profile part states its total and its paging.")
        shapes = {path: _DeliveryShape(returned=True, total_returned=total_returned)}
        if len(_validate_rows(payload, (path,), shapes)) > AUDIENCES_PAGE_ROWS:
            raise ValueError("an Audiences page holds at most 50 rows.")
        return payload
    if not _CAMPAIGN_REF.fullmatch(_text(payload["campaign_ref"])):
        raise ValueError("an Audiences campaign part names its reference.")
    _text(payload["campaign_label"])
    state = CampaignEntryState(_text(payload["state"]))
    text = _nullable_text(payload["text"])
    rows = _items(payload["rows"])
    if state is CampaignEntryState.REFUSED or (text is None) != (
        state is CampaignEntryState.AVAILABLE
    ):
        raise ValueError("an Audiences campaign part has its campaign's state.")
    if len(rows) > AUDIENCES_PAGE_ROWS or (state is CampaignEntryState.WITHDRAWN and rows):
        raise ValueError("an Audiences campaign part holds its page's rows only.")
    if state is CampaignEntryState.WITHDRAWN and paging is not None:
        raise ValueError("a withdrawn Audiences campaign part has no paging.")
    for raw in rows:
        _validate_campaign_row(raw)
    return payload


def _validate_evidence(value: object) -> None:
    evidence = _record(value, {"slots", "version"})
    if evidence["version"] != _EVIDENCE_VERSION:
        raise ValueError("the Audiences evidence status has an invalid version.")
    for raw in _items(evidence["slots"]):
        slot = _record(
            raw,
            {
                "missing_dates",
                "reason_text",
                "recovery_text",
                "role_label",
                "slot_label",
            },
        )
        for key in ("reason_text", "role_label", "slot_label"):
            _text(slot[key])
        _nullable_text(slot["recovery_text"])
        for raw_period in _items(slot["missing_dates"]):
            period = _record(raw_period, {"end", "start"})
            if _iso_date(period["end"]) < _iso_date(period["start"]):
                raise ValueError("an Audiences evidence period runs backwards.")


def require_audiences_artifact_payload(  # noqa: PLR0912 -- strict matrix
    value: object, *, served: bool = False
) -> dict[str, object]:
    """Refuse unknown fields, invalid cross-fields, and reordered semantic rosters.

    The browser decoder (frontend/lib/audiences.ts) enforces the same rules on the served form;
    the shared parity table drives both from this module's own output.
    """
    payload = _record(
        value,
        {
            "boundary",
            "display_minimum",
            "evidence_status",
            "markets",
            "month",
            "outcome",
            "page_context_expires_at",
            "page_context_label",
            "revision",
            "starter_questions",
            "type",
            "version",
        },
    )
    if not _PUBLICATION_REVISION.fullmatch(_text(payload["revision"])):
        raise ValueError("the Audiences artifact names its publication revision.")
    if (
        payload["type"] != AUDIENCES_ARTIFACT_TYPE
        # `True == 1` in Python; the browser's `!== 1` refuses a boolean, so the server does too.
        or isinstance(payload["version"], bool)
        or payload["version"] != AUDIENCES_ARTIFACT_VERSION
    ):
        raise ValueError("the Audiences artifact has an invalid identity.")
    if payload["outcome"] not in {item.value for item in AudiencesOutcome}:
        raise ValueError("the Audiences artifact has an invalid outcome.")
    month = _record(payload["month"], {"end", "label", "start"})
    if _iso_date(month["end"]) < _iso_date(month["start"]):
        raise ValueError("the Audiences month is invalid.")
    month_text = _text(month["label"])
    if _safe_count(payload["display_minimum"]) < 1:
        raise ValueError("the Audiences display minimum is invalid.")
    markets = _items(payload["markets"])
    if len(markets) != 1:
        raise ValueError("Audiences serves exactly one marketplace.")
    market = _record(markets[0], {"campaign_audiences", "default_view", "outcome", "views"})
    if market["outcome"] != payload["outcome"]:
        raise ValueError("an Audiences market's outcome is the artifact's.")
    views = _items(market["views"])
    if len(views) != _VIEW_COUNT:
        raise ValueError("an Audiences market carries two views.")
    served_month = month_text if served else None
    paid_available = _validate_view(views[0], PAID_VIEW, served_month)
    _validate_view(views[1], DSP_VIEW, served_month)
    validated_views = cast("list[dict[str, object]]", views)
    paid_observed = validated_views[0]["paid_state"] == PaidState.OBSERVED.value
    if market["default_view"] != (PAID_VIEW if paid_observed and paid_available else DSP_VIEW):
        raise ValueError("an Audiences market's default view does not match its paid state.")
    if market["outcome"] != AudiencesOutcome.COMPLETE.value:
        absence_texts = [
            candidate
            for raw_view in validated_views
            for raw_delivery in cast("list[dict[str, object]]", raw_view["deliveries"])
            for candidate in (
                raw_delivery["period_text"],
                raw_delivery["total_text"],
                cast("list[dict[str, object]]", raw_delivery["insights"])[0].get("absence_reason"),
            )
            if candidate is not None
        ]
        if not absence_texts:
            raise ValueError("an unavailable Audiences market requires display text.")
    _validate_campaigns(market["campaign_audiences"])
    if payload["boundary"] != [BOUNDARY_TEXT, SEGMENT_IDS_SENTENCE]:
        raise ValueError("the Audiences boundary is invalid.")
    _validate_evidence(payload["evidence_status"])
    starters = _items(payload["starter_questions"])
    for question in starters:
        _text(question)
    if served:
        _text(payload["page_context_expires_at"])
        _text(payload["page_context_label"])
        expected = 2 if payload["outcome"] == AudiencesOutcome.COMPLETE.value else 0
        if len(starters) != expected:
            raise ValueError("the served Audiences starter roster does not match its outcome.")
    return payload


def _optional_range(value: object) -> DateRange | None:
    if value is None:
        return None
    period = _record(value, {"end", "start"})
    return DateRange(
        date.fromisoformat(_text(period["start"])), date.fromisoformat(_text(period["end"]))
    )


def _paid_period_text(state: PaidFeatureDisplay, month: str, baked: PaidFeatureState) -> str | None:
    if state is PaidFeatureDisplay.PUBLISHED:
        return None
    if state is PaidFeatureDisplay.NEEDS_PAID_FEATURE:
        return ASI_NEEDS_PAID_FEATURE_TEXT
    if state is PaidFeatureDisplay.PENDING:
        return f"Figures for {month} are expected after {day_label(baked.expected_after)}."
    latest = baked.latest_available_period
    suffix = "" if latest is None else f" The latest available is {month_label(latest.start)}."
    return f"Figures for {month} didn't arrive.{suffix}"


def resolve_audiences_payload(payload: dict[str, object], as_of: date) -> dict[str, object]:
    """Resolve every paid delivery's pending/missing text at the serving date."""
    served = copy.deepcopy(require_audiences_artifact_payload(payload))
    month = _text(cast("dict[str, object]", served["month"])["label"])
    market = cast("list[dict[str, object]]", served["markets"])[0]
    paid = cast("list[dict[str, object]]", market["views"])[0]
    for delivery in cast("list[dict[str, object]]", paid["deliveries"]):
        raw = cast("dict[str, object]", delivery["paid_state"])
        baked = PaidFeatureState(
            feature_observed=cast("bool", raw["feature_observed"]),
            period_published=cast("bool", raw["period_published"]),
            expected_after=date.fromisoformat(cast("str", raw["expected_after"])),
            latest_available_period=_optional_range(raw["latest_available_period"]),
        )
        resolved = resolve_period_state(ResolvePeriodStateRequest(baked, as_of))
        delivery["period_text"] = _paid_period_text(resolved.display, month, baked)
    return served
