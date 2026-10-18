# SPDX-License-Identifier: MIT-0
"""Read a campaign's segment roster: its segments returned the month before (design §6.5).

Pack 8 returns every segment a campaign's impressions matched in a month; one with too few
shoppers comes back withheld and is dropped. Segments have no spend of their own, so the roster
is load-side history: the segments `fct_segment_delivery` returned for the same campaign and
marketplace in the previous complete month. Whether a month is the campaign's first is read from
the campaign's own rows, never from whether the marketplace published: a month with no earlier
published month holding this campaign is its first, so its roster is empty and says so. A month
whose previous month isn't published while an older one holds this campaign follows a gap in the
campaign's history, so it is refused rather than read as a first month. Another campaign's older
months decide neither. Every month read is planned and bound like any figure, so it reads
exactly the CURRENT publication serving would. Whether an older month holds the campaign costs
one publication selection and one downstream statement however many months are retained, never
one query per month: the older months are bound together and read together
(`PeriodSeriesQueryPort`). Planning and the partitions the selection resolves still grow with
each retained month, up to the selector's exact-partition bound, past which the roster is refused.

`RosterReaders` routes each roster kind to its reader: ad products and campaigns to spend, and
segments here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from domain.measurement.figures import measure
from domain.measurement.periodic import PROFILE_PATH_COLUMN, ROW_KIND_COLUMN
from domain.measurement.planning import FactShape, PlanRequest, plan
from domain.measurement.rosters import (
    ROSTER_REFUSED,
    SEGMENT_PROFILE_FACT,
    Roster,
    RosterEntry,
    RosterKind,
)
from domain.semantics.audience_segments import (
    SEGMENT_FACT_NAMESPACES,
    SEGMENT_ID_COLUMN,
    SEGMENT_NAMESPACE_COLUMN,
    SegmentNamespace,
    unnamed_segment_label,
)
from domain.semantics.calendar_periods import period_containing
from domain.semantics.time import TimeGrain
from domain.shared_kernel.errors import DomainError, ValidationError
from domain.shared_kernel.values import DateRange
from ports.outbound.publication_selection import (
    PublicationSelectionPort,
    PublicationSelectionRequest,
)
from ports.outbound.query import (
    PeriodSeriesQueryPort,
    PeriodSeriesRequest,
    QueryRequest,
    row_ceiling,
)
from ports.outbound.rosters import RosterPort, RosterRequest
from ports.outbound.segment_labels import SegmentLabelPort, SegmentLabelRequest
from ports.outbound.served_periods import ServedPeriodsPort, ServedPeriodsRequest

if TYPE_CHECKING:  # pragma: no cover
    from datetime import datetime

    from domain.catalog.capability import Catalog
    from domain.data_contracts.contract import DataContract
    from domain.measurement.planning import QueryPlan
    from domain.measurement.publications import BoundQueryPlan
    from ports.outbound.publication_selection import PublicationSelection

__all__ = ["SEGMENT_FACT", "SEGMENT_METRIC", "RosterReaders", "SegmentRosterReader"]

SEGMENT_FACT = "fct_segment_delivery"
SEGMENT_METRIC = "segment_reach"
"""Every returned segment row carries its reach, so its keys are the month's returned segments."""
PROFILE_SEGMENT_METRIC = "segment_exposed_shoppers"
"""Every returned C2 segment row carries its exposed shoppers (packs 10-12)."""
SEGMENT_ROW_KIND = "segment"


def _previous_month(period: DateRange) -> DateRange:
    month = period_containing(TimeGrain.MONTH, period.start - timedelta(days=1))
    return DateRange(month.start, month.end)


def _roster_query(request: RosterRequest, plan: BoundQueryPlan) -> QueryRequest:
    """Build one roster read at the caller kind's row ceiling (ADR-143)."""
    return QueryRequest(plan=plan, max_rows=row_ceiling(request.ceiling), ceiling=request.ceiling)


@dataclass(frozen=True, slots=True)
class SegmentRosterReader:
    """Plan, bind and read the previous month's segments of one campaign and marketplace."""

    catalog: Catalog
    contracts: tuple[DataContract, ...]
    publications: PublicationSelectionPort
    periods: ServedPeriodsPort
    queries: PeriodSeriesQueryPort
    labels: SegmentLabelPort
    clock: Callable[[], datetime]

    def roster(self, request: RosterRequest) -> Roster:
        """Return the campaign's segments of the month before, each named with its provider.

        A C2 request (`fct_segment_profile`, packs 10-12) reads its profile path's own delivery
        instead (`_profile_roster`).
        """
        if request.fact == SEGMENT_PROFILE_FACT:
            return self._profile_roster(request)
        prior = _previous_month(request.period)
        writers = tuple(item for item in self.contracts if item.target_table == SEGMENT_FACT)
        population = {"marketplace_key": request.marketplace_key}
        served = self.periods.served_periods(
            ServedPeriodsRequest(writers, population, self.clock())
        )
        published = {(period.start, period.end) for _contract, period in served.publications}
        # Older published months, newest first: read only to learn whether this campaign was
        # observed before, never graded against.
        older = tuple(
            DateRange(start, end)
            for start, end in sorted(published, reverse=True)
            if end < prior.start
        )
        if (prior.start, prior.end) not in published:
            if self._observed_in_any(request, older):
                # This campaign was observed before the unpublished month, so that month is a gap
                # in its history, not its first month: its missing segments can't be named.
                raise DomainError(
                    ROSTER_REFUSED,
                    f"Segment delivery from {prior.start.isoformat()} to "
                    f"{prior.end.isoformat()} isn't published, though this campaign was "
                    "measured in an earlier month, so which segments it reached then can't be "
                    "read and no segment breakdown is answered against it.",
                    context={"roster": request.kind.value},
                )
            return self._first_month(request)
        keys = self._segments_in(request, prior)
        # Before any early return (review #8): even with no segments, as in a bake's pin pass,
        # the label port is asked, so its scopes are pinned by this roster's own read.
        namespace = SEGMENT_FACT_NAMESPACES[SEGMENT_FACT]
        named = self.labels.labels(
            SegmentLabelRequest(namespace, request.marketplace_key, keys, prior.end)
        )
        if not keys and not self._observed_in_any(request, older):
            return self._first_month(request)
        entries = tuple(
            RosterEntry(key, named[key].display if key in named else unnamed_segment_label(key))
            for key in keys
        )
        return Roster(
            kind=RosterKind.SEGMENT,
            period=request.period,
            marketplace_key=request.marketplace_key,
            platform_key=request.platform_key,
            entries=tuple(sorted(entries, key=lambda item: (item.label, item.key))),
            filters=request.filters,
        )

    def _profile_roster(self, request: RosterRequest) -> Roster:
        """Return the segments a profile path's delivery returned the month before (design §6.5).

        Each profile path is one contract's delivery, so its history is that contract's own
        published months: a published previous month is the roster; an unpublished one with an
        older published month is a gap and refused; with none, this is the path's first month.
        """
        path = dict(request.filters).get(PROFILE_PATH_COLUMN)
        writers = tuple(
            item
            for item in self.contracts
            if item.target_table == SEGMENT_PROFILE_FACT
            and (field := item.field_for(PROFILE_PATH_COLUMN)) is not None
            and field.literal_value == path
        )
        namespace = next(
            (
                field.literal_value
                for item in writers
                if (field := item.field_for(SEGMENT_NAMESPACE_COLUMN)) is not None
            ),
            None,
        )
        if len(writers) != 1 or namespace is None:
            raise DomainError(
                ROSTER_REFUSED,
                f"No one buyer-profile delivery writes profile path {path!r}, so its segments "
                "can't be listed.",
                context={"roster": request.kind.value},
            )
        prior = _previous_month(request.period)
        population = {"marketplace_key": request.marketplace_key}
        served = self.periods.served_periods(
            ServedPeriodsRequest(writers, population, self.clock())
        )
        published = {(period.start, period.end) for _contract, period in served.publications}
        if (prior.start, prior.end) not in published:
            if any(end < prior.start for _start, end in published):
                raise DomainError(
                    ROSTER_REFUSED,
                    f"The {path} profile from {prior.start.isoformat()} to "
                    f"{prior.end.isoformat()} isn't published, though an earlier month is, so "
                    "which segments it listed then can't be read and no segment breakdown is "
                    "answered against it.",
                    context={"roster": request.kind.value},
                )
            return self._first_month(request)
        keys = self._profile_segments_in(request, (writers, str(namespace)), prior)
        named = self.labels.labels(
            SegmentLabelRequest(
                SegmentNamespace(str(namespace)), request.marketplace_key, keys, prior.end
            )
        )
        entries = tuple(
            RosterEntry(key, named[key].display if key in named else unnamed_segment_label(key))
            for key in keys
        )
        return Roster(
            kind=RosterKind.SEGMENT,
            period=request.period,
            marketplace_key=request.marketplace_key,
            platform_key=request.platform_key,
            entries=tuple(sorted(entries, key=lambda item: (item.label, item.key))),
            filters=request.filters,
        )

    def _profile_segments_in(
        self,
        request: RosterRequest,
        writer: tuple[tuple[DataContract, ...], str],
        month: DateRange,
    ) -> tuple[str, ...]:
        """Plan, bind and read the segment rows one profile path returned in a published month."""
        writers, namespace = writer
        filters = {
            "marketplace_key": request.marketplace_key,
            "platform_key": request.platform_key,
            "grain_set": "platform",
            "time_grain": TimeGrain.MONTH.value,
            SEGMENT_NAMESPACE_COLUMN: namespace,
            ROW_KIND_COLUMN: SEGMENT_ROW_KIND,
            **dict(request.filters),
        }
        try:
            logical = plan(
                self.catalog,
                PlanRequest(
                    PROFILE_SEGMENT_METRIC,
                    ("audience_segment",),
                    month,
                    filters,
                    # A history month is read for its keys only; it is graded by nothing.
                    roster=Roster(
                        RosterKind.SEGMENT,
                        month,
                        request.marketplace_key,
                        request.platform_key,
                        (),
                        filters=request.filters,
                    ),
                ),
                FactShape.for_fact(SEGMENT_PROFILE_FACT, writers),
            )
        except DomainError as error:
            raise self._refused(request, month, error) from error
        selection = self._select(request, (logical,), month)
        result = self.queries.run(_roster_query(request, selection.plans[0]))
        if result.truncated or not selection.coverage.covers_all((month,)):
            raise self._unreadable(request, month)
        return tuple(
            sorted(
                {
                    str(dict(figure.dimensions)[SEGMENT_ID_COLUMN])
                    for figure in measure(logical, result.rows).figures
                }
            )
        )

    def _first_month(self, request: RosterRequest) -> Roster:
        """Return the campaign's first observed month: an empty roster that says so."""
        return Roster(
            kind=RosterKind.SEGMENT,
            period=request.period,
            marketplace_key=request.marketplace_key,
            platform_key=request.platform_key,
            entries=(),
            filters=request.filters,
            first_period=True,
        )

    def _observed_in_any(self, request: RosterRequest, months: tuple[DateRange, ...]) -> bool:
        """Whether the campaign returned any segment in one of these published months, newest first.

        One selection and one statement however much history is retained: every month is planned
        as the prior-month read is, all are bound in one selection (at most
        `MAX_EXACT_CURRENT_PARTITIONS` partitions, refused beyond it), and one statement reads
        them together, each month fenced to its own publications. A month Amazon withheld whole
        can't say whether the campaign ran, so it is reached only as a newest-first walk would
        reach it: the months newer than it are read first, and only a campaign absent from all of
        them reads on into it, whose read refuses.
        """
        if not months:
            return False
        logical = tuple(self._plan_month(request, month) for month in months)
        span = DateRange(months[-1].start, months[0].end)
        selection = self._select(request, logical, span)
        if not selection.coverage.covers_all(months):
            raise self._unreadable(request, span)
        bound = selection.plans
        withheld = next(
            (
                index
                for index, item in enumerate(bound)
                if any(publication.evidence.withheld_only for publication in item.primary)
            ),
            len(bound),
        )
        return any(
            self._observed_in(request, part)
            for part in (bound[:withheld], bound[withheld:])
            if part
        )

    def _observed_in(self, request: RosterRequest, bound: tuple[BoundQueryPlan, ...]) -> bool:
        """Read these bound months in one statement: whether the campaign returned any segment."""
        result = self.queries.run_series(
            PeriodSeriesRequest(bound, row_ceiling(request.ceiling), request.ceiling)
        )
        observed = bool(measure(bound[0].logical, result.rows).figures)
        # A truncated result still proves a returned segment; only an empty page would mislead.
        if result.truncated and not observed:
            raise self._unreadable(
                request, DateRange(bound[-1].logical.window.start, bound[0].logical.window.end)
            )
        return observed

    def _segments_in(self, request: RosterRequest, month: DateRange) -> tuple[str, ...]:
        """Plan, bind and read the segments the campaign returned in one published month."""
        logical = self._plan_month(request, month)
        selection = self._select(request, (logical,), month)
        result = self.queries.run(_roster_query(request, selection.plans[0]))
        if result.truncated or not selection.coverage.covers_all((month,)):
            raise self._unreadable(request, month)
        return tuple(
            sorted(
                {
                    str(dict(figure.dimensions)[SEGMENT_ID_COLUMN])
                    for figure in measure(logical, result.rows).figures
                }
            )
        )

    def _plan_month(self, request: RosterRequest, month: DateRange) -> QueryPlan:
        """Plan the campaign's returned segments in one published month, as a figure is planned."""
        filters = {
            "marketplace_key": request.marketplace_key,
            "platform_key": request.platform_key,
            "grain_set": "campaign",
            "time_grain": TimeGrain.MONTH.value,
            SEGMENT_NAMESPACE_COLUMN: SEGMENT_FACT_NAMESPACES[SEGMENT_FACT].value,
            **dict(request.filters),
        }
        try:
            return plan(
                self.catalog,
                PlanRequest(
                    SEGMENT_METRIC,
                    ("audience_segment",),
                    month,
                    filters,
                    # A history month is read for its keys only; it is graded by nothing.
                    roster=Roster(
                        RosterKind.SEGMENT,
                        month,
                        request.marketplace_key,
                        request.platform_key,
                        (),
                        filters=request.filters,
                    ),
                ),
                FactShape.for_fact(SEGMENT_FACT, self.contracts),
            )
        except DomainError as error:
            raise self._refused(request, month, error) from error

    def _select(
        self, request: RosterRequest, logical: tuple[QueryPlan, ...], span: DateRange
    ) -> PublicationSelection:
        """Bind every planned month to its CURRENT publications in one selection."""
        try:
            return self.publications.select(
                PublicationSelectionRequest(logical, self.contracts, self.clock())
            )
        except DomainError as error:
            raise self._refused(request, span, error) from error
        except ValidationError as error:
            # Past the selector's bound (`SELECTION_TOO_LARGE`): the span is retained history the
            # user never named and can't narrow, so the roster refuses in its own words.
            raise self._unreadable(request, span, reason=str(error.code)) from error

    @staticmethod
    def _refused(request: RosterRequest, span: DateRange, error: DomainError) -> DomainError:
        return DomainError(
            ROSTER_REFUSED,
            f"Which segments this campaign reached from {span.start.isoformat()} to "
            f"{span.end.isoformat()} can't be read: {error.description}",
            context={"roster": request.kind.value, "reason": str(error.code)},
        )

    @staticmethod
    def _unreadable(
        request: RosterRequest, span: DateRange, *, reason: str | None = None
    ) -> DomainError:
        context = {"roster": request.kind.value}
        if reason is not None:
            context["reason"] = reason
        return DomainError(
            ROSTER_REFUSED,
            f"Which segments this campaign reached from {span.start.isoformat()} to "
            f"{span.end.isoformat()} can't be read in full, so no segment breakdown is "
            "answered against it.",
            context=context,
        )


@dataclass(frozen=True, slots=True)
class RosterReaders(RosterPort):
    """Route a roster to its reader: spend for ad products and campaigns, history for segments."""

    spend: RosterPort
    segments: RosterPort | None = None

    def roster(self, request: RosterRequest) -> Roster:
        """Read the roster from the reader that owns its kind."""
        if request.kind is not RosterKind.SEGMENT:
            return self.spend.roster(request)
        if self.segments is None:
            raise DomainError(
                ROSTER_REFUSED,
                "Segment rosters can't be read in this runtime, so no segment breakdown is "
                "answered.",
                context={"roster": request.kind.value},
            )
        return self.segments.roster(request)
