# SPDX-License-Identifier: MIT-0
"""A ranked restriction: the top `n` keys of a declared population, ranked inside the query.

Retail health shows its top 50 products at any catalog size (ADR-143, owner decision
2026-10-18). Ranking in Python needed every product's series rows first; ranking in the query
returns only the `n` shown, so the rows returned and the statements run are constant in the
catalog. The same statement states what the page says about the whole population (ADR-152):
its count, declared sums and whether any of it was withheld, per partition, so no read of
every key reaches the bake.

**One home for the rank rule.** Which metric ranks, over which window, among which population,
in which order and with which tie key: all of it is this value. The SQL compiler renders
`RANK_ORDER` as the `ranked` CTE's `ORDER BY`; `RankedRestriction.order` renders the same list
as a Python sort key for the at most `n` rows the ranked-keys read returns. A golden test holds
the two renderings equal. Ranking by change (`RankBy.ABS_CHANGE`, Retail's movers) needs no
second order: its rank value is |current - prior|, so the one order list covers both rules.

Publish-time only. It is not a tool input, so chat can never reach it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from domain.catalog.capability import MetricKind
from domain.measurement.planning import QueryPlan
from domain.measurement.publications import BoundQueryPlan
from domain.shared_kernel.errors import ErrorCode, InvariantError
from domain.shared_kernel.values import DateRange

__all__ = [
    "RANKED_INVARIANT",
    "RANKING_INELIGIBLE",
    "RANK_ORDER",
    "RankBy",
    "RankDirection",
    "RankField",
    "RankablePlans",
    "RankedMembership",
    "RankedPopulation",
    "RankedProjection",
    "RankedRead",
    "RankedRestriction",
    "RankedValue",
    "require_rankable",
]

RANKING_INELIGIBLE: Final = ErrorCode("RANKING", 1)
"""A ranked restriction was declared over a plan it can't rank exactly: a defect."""
RANKED_INVARIANT: Final = ErrorCode("RANKING", 2)
"""A ranked read disagreed with its declared population: the SQL and the declaration differ."""


class RankedProjection(StrEnum):
    """What a ranked statement selects: the ranked keys themselves, or a read restricted to them.

    Part of the request's digest, so the two projections never share an execution.
    """

    RANKED_KEYS = "ranked_keys"
    """One row per ranked key, including a key with no rank-metric row (its value is 0)."""
    SEMI_JOIN = "semi_join"
    """The request's own fact read, keeping only rows whose key is ranked."""


class RankBy(StrEnum):
    """What a key's rank value is."""

    VALUE = "value"
    """The rank metric's sum over the rank window (0 without a row)."""
    ABS_CHANGE = "abs_change"
    """|rank window sum - change window sum| of the rank metric (Retail's movers)."""


class RankedMembership(StrEnum):
    """Which population keys may rank, and so which the population count counts."""

    ANY_ROW = "any_row"
    """Every key with a row in any population read, including a zero rank value."""
    POSITIVE = "positive"
    """Keys whose rank value is above 0 (a gateway product, a campaign that spent), and any key
    Amazon withheld rows from, so the population's withholding is never filtered away."""


class RankField(StrEnum):
    """A column of the `ranked` CTE the order reads."""

    RANK_VALUE = "rank_value"
    KEY = "key"


class RankDirection(StrEnum):
    """An order direction, rendered as SQL text and as a Python sort sign."""

    DESC = "DESC"
    ASC = "ASC"


RANK_ORDER: Final = ((RankField.RANK_VALUE, RankDirection.DESC), (RankField.KEY, RankDirection.ASC))
"""The rank rule's order: largest rank value first, then the key, ascending, as the tie."""


@dataclass(frozen=True, slots=True)
class RankedValue:
    """One ranked key and its rank value, as the ranked-keys read returns it."""

    key: str
    rank_value: Decimal | int


@dataclass(frozen=True, slots=True)
class RankedPopulation:
    """What the ranked statement says about one partition's whole population (ADR-152).

    `partition` holds the partition's canonical column values in `partition_by` order (empty when
    unpartitioned); `count` its members; `sums` each declared population sum, by metric, over the
    rank window; `suppressed` whether any population row was withheld.
    """

    partition: tuple[str, ...]
    count: int
    sums: tuple[tuple[str, Decimal], ...]
    suppressed: bool

    def __post_init__(self) -> None:
        if isinstance(self.count, bool) or self.count < 0:
            raise ValueError("a ranked population counts zero or more keys.")

    def sum_of(self, metric: str) -> Decimal:
        """Return one declared population sum."""
        return dict(self.sums)[metric]


@dataclass(frozen=True, slots=True)
class RankedRestriction:
    """The top `n` keys of a population, ranked by one metric over one window.

    `key` is the ranked grain's dimension name ("product"). The population is every key with a
    row in any `population_metrics` fact over any `population_windows` window; a key's rank value
    is the rank metric's sum over `rank_window` (0 without a row), or with `ABS_CHANGE` the
    absolute difference from its sum over `change_window`. `partition_by` names grains each
    keeping its own top `n` and count; `membership` says whether a zero rank value is a member;
    `population_sums` are population metrics summed over `rank_window` (unpartitioned only).
    """

    key: str
    rank_metric: str
    rank_window: DateRange
    population_metrics: tuple[str, ...]
    population_windows: tuple[DateRange, ...]
    n: int
    projection: RankedProjection
    rank_by: RankBy = RankBy.VALUE
    change_window: DateRange | None = None
    partition_by: tuple[str, ...] = ()
    membership: RankedMembership = RankedMembership.ANY_ROW
    population_sums: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.n, bool) or self.n < 1:
            raise ValueError("a ranked restriction keeps at least one key.")
        if not self.key.strip() or not self.rank_metric.strip():
            raise ValueError("a ranked restriction names its key and its rank metric.")
        if not self.population_metrics or not self.population_windows:
            raise ValueError("a ranked restriction declares its population.")
        if len(set(self.population_metrics)) != len(self.population_metrics):
            raise ValueError("a ranked restriction's population repeats a metric.")
        if len(set(self.population_windows)) != len(self.population_windows):
            raise ValueError("a ranked restriction's population repeats a window.")
        if self.rank_window not in self.population_windows:
            raise ValueError("a ranked restriction ranks within its population's windows.")
        self._check_change()
        if len(set(self.partition_by)) != len(self.partition_by) or self.key in self.partition_by:
            raise ValueError(
                "a ranked restriction partitions by distinct grains other than its key."
            )
        if len(set(self.population_sums)) != len(self.population_sums):
            raise ValueError("a ranked restriction's population sums repeat a metric.")
        if not set(self.population_sums) <= set(self.population_metrics):
            raise ValueError("a ranked restriction sums only its population metrics.")
        if self.population_sums and self.partition_by:
            raise ValueError("a partitioned ranked restriction declares no population sums.")

    def _check_change(self) -> None:
        if (self.rank_by is RankBy.ABS_CHANGE) != (self.change_window is not None):
            raise ValueError("a ranked restriction names a change window exactly when by change.")
        if self.change_window is not None and (
            self.change_window not in self.population_windows
            or self.change_window == self.rank_window
        ):
            raise ValueError("a change window is another of the population's windows.")

    @property
    def population(self) -> tuple[tuple[str, DateRange], ...]:
        """Every (metric, window) read whose keys make up the population, in declared order."""
        return tuple(
            (metric, window)
            for metric in self.population_metrics
            for window in self.population_windows
        )

    @staticmethod
    def order(rows: Iterable[RankedValue]) -> tuple[RankedValue, ...]:
        """Sort ranked rows by `RANK_ORDER`: the Python rendering of the SQL `ORDER BY`."""
        ordered = list(rows)
        # Stable sorts applied from the last order term to the first give the compound order.
        for field, direction in reversed(RANK_ORDER):
            ordered.sort(
                key=(
                    (lambda item: Decimal(item.rank_value))
                    if field is RankField.RANK_VALUE
                    else (lambda item: item.key)
                ),
                reverse=direction is RankDirection.DESC,
            )
        return tuple(ordered)


@dataclass(frozen=True, slots=True)
class RankedRead:
    """A ranked restriction bound to the exact publications of every read it ranks over.

    `population`, `rank` and `change` are the very bound plans the batch's own reads read, so the
    statement's population is fenced to the publications the stated total was measured from.
    `key_column` and `marketplace_column` are the canonical columns of the ranked grain;
    `partition_columns` those of `partition_by`, in order.
    """

    restriction: RankedRestriction
    key_column: str
    marketplace_column: str
    population: tuple[BoundQueryPlan, ...]
    rank: BoundQueryPlan
    change: BoundQueryPlan | None = None
    partition_columns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.population) != len(self.restriction.population):
            raise ValueError("a ranked read binds exactly its declared population.")
        if (self.change is None) != (self.restriction.change_window is None):
            raise ValueError("a ranked read binds its change read exactly when it ranks by change.")
        if len(self.partition_columns) != len(self.restriction.partition_by):
            raise ValueError("a ranked read names one column per partition grain.")

    @property
    def sums(self) -> tuple[BoundQueryPlan, ...]:
        """The bound population reads each declared population sum is summed over, in order."""
        declared = self.restriction.population
        return tuple(
            self.population[declared.index((metric, self.restriction.rank_window))]
            for metric in self.restriction.population_sums
        )


def _refuse(problem: str, metric: str) -> InvariantError:
    return InvariantError(
        RANKING_INELIGIBLE,
        "A ranked restriction was declared over a read it cannot rank exactly.",
        context={"problem": problem, "metric": metric},
    )


def _require_simple_additive(plan: QueryPlan) -> None:
    metric = plan.metric.name
    if plan.metric.kind is not MetricKind.SIMPLE or len(plan.measures) != 1:
        raise _refuse("not a simple one-measure metric", metric)
    if plan.join is not None:
        raise _refuse("a joined read", metric)
    if plan.roster is not None:
        raise _refuse("a rostered read", metric)
    if plan.periodic:
        raise _refuse("a periodic read", metric)


@dataclass(frozen=True, slots=True)
class RankablePlans:
    """The planned reads one ranked restriction ranks over, checked by `require_rankable`.

    `columns` is the ranked grain's canonical columns: each partition grain's, then the key's,
    then the marketplace's.
    """

    restricted: QueryPlan
    rank: QueryPlan
    population: tuple[QueryPlan, ...]
    columns: tuple[str, ...]
    change: QueryPlan | None = None


def require_rankable(restriction: RankedRestriction, plans: RankablePlans) -> None:
    """Refuse a restriction its plans can't rank exactly (closed, checked at plan time).

    The rank metric and every population metric are simple, one-measure, unjoined, unrostered,
    daily reads; every read groups by the partition grains, the ranked key and the marketplace,
    so the population and the restricted rows share one key shape. A fact that can withhold is
    rankable: the statement reports the population's withholding (ADR-152), and a caller
    refuses a suppressed answer as it always has.
    """
    rank, change = plans.rank, plans.change
    if rank.metric.name != restriction.rank_metric or rank.window != restriction.rank_window:
        raise _refuse("the rank plan is not the declared rank read", rank.metric.name)
    if (change is None) != (restriction.change_window is None) or (
        change is not None
        and (
            change.metric.name != restriction.rank_metric
            or change.window != restriction.change_window
        )
    ):
        raise _refuse("the change plan is not the declared change read", rank.metric.name)
    declared = restriction.population
    if tuple((item.metric.name, item.window) for item in plans.population) != declared:
        raise _refuse("the population plans are not the declared population", rank.metric.name)
    for item in (rank, *(() if change is None else (change,)), *plans.population):
        _require_simple_additive(item)
        if item.group_by != plans.columns:
            raise _refuse("a population read not at the ranked grain", item.metric.name)
    restricted = plans.restricted
    if restricted.join is not None or restricted.roster is not None:
        raise _refuse("a joined or rostered restricted read", restricted.metric.name)
    if not set(plans.columns) <= set(restricted.group_by):
        raise _refuse("a restricted read without the ranked key", restricted.metric.name)
