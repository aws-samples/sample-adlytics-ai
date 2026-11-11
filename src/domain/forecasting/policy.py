# SPDX-License-Identifier: MIT-0
"""The declared demand-forecast policy (contracts/forecasting/demand-forecast.yaml) as values.

Adapted from the source engine's inference-config parser: the values are refused whole on
anything they don't fully define, so a typo stops a run instead of changing a forecast. Each
section carries its own version; the loader records them with the file's SHA-256 (design §3a).
"""

from __future__ import annotations

from dataclasses import dataclass

TARGETS: tuple[str, ...] = ("units_shipped", "units_ordered", "revenue_shipped", "cogs_shipped")
"""The four forecast targets, in their declared order."""
MONEY_TARGETS: frozenset[str] = frozenset({"revenue_shipped", "cogs_shipped"})
QUANTILES: tuple[float, ...] = (0.1, 0.5, 0.9)
SORT_KEY: tuple[str, ...] = (
    "target",
    "origin",
    "marketplace_key",
    "distributor_view",
    "selling_program",
    "canonical_asin",
)
SUNDAY = 6
"""`date.weekday()` of Sunday, the first day of a Vendor Central week."""
MAX_SHARDS = 100
"""Athena `UNLOAD` writes at most 100 partitions, so a roster holds at most 100 shards."""
SHARD_LADDER: tuple[int, ...] = (2000, 1000, 500)
"""The approved `shard_max_asins` values (design §4b shard ladder, P-18)."""


def _positive(name: str, value: int, minimum: int = 1) -> None:
    if isinstance(value, bool) or value < minimum:
        raise ValueError(f"{name} must be a whole number of at least {minimum}, got {value!r}")


@dataclass(frozen=True)
class InputPolicy:
    """Which facts feed the forecast, for which set, and which week convention (`input` v1)."""

    version: int
    sales_fact: str
    traffic_fact: str
    product_metadata: str
    distributor_view: str
    selling_program: str
    week_starts_on: int
    targets: tuple[str, ...]
    covariates: tuple[str, ...]

    def __post_init__(self) -> None:
        _positive("input.version", self.version)
        if self.week_starts_on != SUNDAY:
            raise ValueError(f"input.week_starts_on must be {SUNDAY} (Sunday)")
        if self.targets != TARGETS:
            raise ValueError(f"input.targets must be {list(TARGETS)}")
        if self.covariates != ("glance_views",):
            raise ValueError("input.covariates must be ['glance_views']")
        for name, value in (
            ("input.facts.sales", self.sales_fact),
            ("input.facts.traffic", self.traffic_fact),
            ("input.product_metadata", self.product_metadata),
            ("input.selectors.distributor_view", self.distributor_view),
            ("input.selectors.selling_program", self.selling_program),
        ):
            if not value:
                raise ValueError(f"{name} must be non-empty")


@dataclass(frozen=True)
class MissingWeekPolicy:
    """The three-state missing-week rule's parameters (`missing_weeks` v1, design §3b)."""

    version: int
    activity_window_weeks: int
    max_unknown_run_weeks: int
    unknown_day_grace_days: int

    def __post_init__(self) -> None:
        _positive("missing_weeks.version", self.version)
        _positive("missing_weeks.activity_window_weeks", self.activity_window_weeks)
        _positive("missing_weeks.max_unknown_run_weeks", self.max_unknown_run_weeks, 0)
        _positive("missing_weeks.unknown_day_grace_days", self.unknown_day_grace_days, 0)


@dataclass(frozen=True)
class EligibilityPolicy:
    """Context, holdout and horizon lengths (`eligibility` v1)."""

    version: int
    min_context_weeks: int
    holdout_weeks: int
    min_scored_holdout_weeks: int
    horizon_weeks: int
    quantiles: tuple[float, ...]
    history_weeks_max: int
    ets_min_known_weeks: int
    min_seasonal_residuals: int

    def __post_init__(self) -> None:
        _positive("eligibility.version", self.version)
        for name in (
            "min_context_weeks",
            "holdout_weeks",
            "min_scored_holdout_weeks",
            "horizon_weeks",
            "history_weeks_max",
            "ets_min_known_weeks",
            "min_seasonal_residuals",
        ):
            _positive(f"eligibility.{name}", getattr(self, name))
        if self.quantiles != QUANTILES:
            raise ValueError(f"eligibility.quantiles must be {list(QUANTILES)}")
        if self.min_scored_holdout_weeks > self.holdout_weeks:
            raise ValueError("eligibility.min_scored_holdout_weeks must not exceed holdout_weeks")
        if self.min_context_weeks + self.holdout_weeks > self.history_weeks_max:
            raise ValueError("eligibility: min_context_weeks + holdout_weeks exceeds the history")


@dataclass(frozen=True)
class EnginePolicy:
    """Batching, cross-learning and shard size (`engine` v1, design §4c)."""

    version: int
    cross_learning: bool
    batch_size: int
    batch_scope: str
    sort_key: tuple[str, ...]
    shard_max_asins: int

    def __post_init__(self) -> None:
        _positive("engine.version", self.version)
        _positive("engine.batch_size", self.batch_size)
        if self.batch_scope != "one_target_one_origin":
            raise ValueError("engine.batch_scope must be one_target_one_origin")
        if self.sort_key != SORT_KEY:
            raise ValueError(f"engine.sort_key must be {list(SORT_KEY)}")
        if self.shard_max_asins not in SHARD_LADDER:
            raise ValueError(f"engine.shard_max_asins must be one of {list(SHARD_LADDER)}")


@dataclass(frozen=True)
class NumericPolicy:
    """Storage precision and check order (`numeric` v1, design §4d)."""

    version: int
    stored_decimal_places: int
    stored_rounding: str
    check_order: tuple[str, ...]
    display: str

    def __post_init__(self) -> None:
        _positive("numeric.version", self.version)
        if self.stored_decimal_places != 4 or self.stored_rounding != "half_even":  # noqa: PLR2004
            raise ValueError("numeric: storage is 4 decimal places, half_even")
        if self.check_order != ("finite", "band_unordered", "negative"):
            raise ValueError("numeric.check_order must be [finite, band_unordered, negative]")
        if self.display != "whole_half_away_from_zero":
            raise ValueError("numeric.display must be whole_half_away_from_zero")


@dataclass(frozen=True)
class ServingPolicy:
    """What the page and tools may read and show (`serving` v1)."""

    version: int
    stale_after_days: int
    page_rows: int
    tool_list_max: int
    compare_max_asins: int
    views: tuple[int, ...]
    title_max_bytes: int
    item_max_bytes: int
    rank_by: str
    status_target: str
    search: str

    def __post_init__(self) -> None:
        _positive("serving.version", self.version)
        for name in (
            "stale_after_days",
            "page_rows",
            "tool_list_max",
            "compare_max_asins",
            "title_max_bytes",
            "item_max_bytes",
        ):
            _positive(f"serving.{name}", getattr(self, name))
        if not self.views or list(self.views) != sorted(set(self.views)):
            raise ValueError("serving.views must be a non-empty ascending list of distinct weeks")
        if self.status_target not in TARGETS:
            raise ValueError("serving.status_target must be one of the targets")
        if self.search != "exact_asin":
            raise ValueError("serving.search must be exact_asin")


@dataclass(frozen=True)
class DemandForecastPolicy:
    """Every slice 1 section of the policy, each validated on construction."""

    schema_version: int
    input: InputPolicy
    missing_weeks: MissingWeekPolicy
    eligibility: EligibilityPolicy
    engine: EnginePolicy
    numeric: NumericPolicy
    serving: ServingPolicy

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("schema_version must be 1")
        if self.eligibility.horizon_weeks != max(self.serving.views):
            raise ValueError("serving.views must end at eligibility.horizon_weeks")

    @property
    def section_versions(self) -> dict[str, int]:
        """Each section's declared version, keyed by section name."""
        return {
            "input": self.input.version,
            "missing_weeks": self.missing_weeks.version,
            "eligibility": self.eligibility.version,
            "engine": self.engine.version,
            "numeric": self.numeric.version,
            "serving": self.serving.version,
        }
