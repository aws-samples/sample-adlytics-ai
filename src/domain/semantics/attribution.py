# SPDX-License-Identifier: MIT-0
"""Governed temporal attribution models and their closed selector vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "AttributionMethod",
    "AttributionModel",
    "AttributionModelError",
    "AttributionModelRegistry",
]


class AttributionModelError(ValueError):
    """An attribution model declaration is malformed or contradictory."""


class AttributionMethod(StrEnum):
    """Supported deterministic credit-allocation algorithms."""

    EARLIEST = "earliest_qualifying_touch"
    LATEST = "latest_qualifying_touch"
    LINEAR = "equal_qualifying_touch_credit"


@dataclass(frozen=True, slots=True)
class AttributionModel:
    """One immutable attribution interpretation selected explicitly by key."""

    key: str
    label: str
    method: AttributionMethod
    lookback_days: int
    time_basis: str
    eligible_sources: tuple[str, ...]
    tie_break: tuple[str, ...]
    population: str
    revenue_scope: str

    def __post_init__(self) -> None:
        for label, value in (
            ("key", self.key),
            ("label", self.label),
            ("time basis", self.time_basis),
            ("population", self.population),
            ("revenue scope", self.revenue_scope),
        ):
            if not value.strip():
                raise AttributionModelError(f"attribution model requires a nonblank {label}.")
        if self.lookback_days < 1:
            raise AttributionModelError(f"{self.key}: lookback must be positive.")
        for label, values in (
            ("eligible sources", self.eligible_sources),
            ("tie break", self.tie_break),
        ):
            if not values or len(set(values)) != len(values) or any(not value for value in values):
                raise AttributionModelError(f"{self.key}: {label} must be nonempty and unique.")


@dataclass(frozen=True, slots=True)
class AttributionModelRegistry:
    """The complete versioned selector vocabulary for temporal attribution."""

    version: int
    models: tuple[AttributionModel, ...]

    def __post_init__(self) -> None:
        if self.version != 1:
            raise AttributionModelError("attribution model registry version must be integer 1.")
        if not self.models:
            raise AttributionModelError("attribution model registry must declare models.")
        keys = tuple(model.key for model in self.models)
        labels = tuple(model.label for model in self.models)
        if len(set(keys)) != len(keys):
            raise AttributionModelError("attribution model keys must be unique.")
        if len(set(labels)) != len(labels):
            raise AttributionModelError("attribution model labels must be unique.")

    @property
    def keys(self) -> tuple[str, ...]:
        """Return model keys in declaration order for deterministic selectors."""
        return tuple(model.key for model in self.models)

    def model(self, key: str) -> AttributionModel | None:
        """Resolve one exact model key without fuzzy matching or defaults."""
        return next((model for model in self.models if model.key == key), None)
