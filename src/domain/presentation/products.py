# SPDX-License-Identifier: MIT-0
"""Closed product browse models and effective-window display-label policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from domain.presentation.campaigns import EffectiveNameStatus
from domain.semantics.analytical_resolution import EffectiveLabel, EntityAlias, EntityKind
from domain.semantics.identity import ProductKey
from domain.shared_kernel.values import DateRange

__all__ = [
    "ProductListPage",
    "ProductName",
    "ProductPresentation",
    "ProductPresentationRequest",
    "present_product",
]


@dataclass(frozen=True, slots=True)
class ProductName:
    """One untrusted effective-dated product display label."""

    value: str
    valid_from: date
    valid_to: date | None

    @classmethod
    def from_alias(cls, label: EffectiveLabel) -> ProductName:
        """Copy an authorized alias label without changing its validity interval."""
        return cls(label.value, label.valid_from, label.valid_to)


@dataclass(frozen=True, slots=True)
class ProductPresentation:
    """Canonical product custody paired with presentation-safe display attributes."""

    product: ProductKey
    effective_name: str | None
    effective_name_status: EffectiveNameStatus
    names: tuple[ProductName, ...]

    def __post_init__(self) -> None:
        if self.effective_name_status is EffectiveNameStatus.AVAILABLE and not self.effective_name:
            raise ValueError("an available product name requires display text.")
        if self.effective_name_status is EffectiveNameStatus.MISSING and (
            self.names or self.effective_name is not None
        ):
            raise ValueError("a product with no labels cannot fall back to canonical identity.")
        if self.effective_name_status is EffectiveNameStatus.UNAVAILABLE_FOR_WINDOW and (
            self.effective_name is not None or not self.names
        ):
            raise ValueError("an unavailable product name retains history without fallback.")


@dataclass(frozen=True, slots=True)
class ProductListPage:
    """Bounded authorized product browse result without canonical selectors."""

    products: tuple[ProductPresentation, ...]
    visible_window: DateRange
    comparison_window: DateRange | None
    truncated: bool


@dataclass(frozen=True, slots=True)
class ProductPresentationRequest:
    """One authorized product alias history and selected visible window."""

    aliases: tuple[EntityAlias, ...]
    visible_window: DateRange

    def __post_init__(self) -> None:
        if not self.aliases:
            raise ValueError("product presentation requires an authorized alias history.")
        keys = {(alias.kind, alias.key) for alias in self.aliases}
        if len(keys) != 1 or next(iter(keys))[0] is not EntityKind.PRODUCT:
            raise ValueError("product presentation requires one canonical product history.")


def present_product(request: ProductPresentationRequest) -> ProductPresentation:
    """Select the period-end product label while retaining complete safe label history."""
    product = request.aliases[0].key
    if not isinstance(product, ProductKey):  # pragma: no cover - request invariant
        raise TypeError("product alias history carries a non-product key.")
    labels = tuple(
        sorted(
            {label for alias in request.aliases for label in alias.labels},
            key=lambda item: (item.valid_from, item.valid_to or date.max, item.value),
        )
    )
    active = tuple({label.value for label in labels if label.contains(request.visible_window.end)})
    if len(active) > 1:
        raise ValueError("product has overlapping effective names at the visible-window end.")
    if active:
        effective_name = active[0]
        status = EffectiveNameStatus.AVAILABLE
    elif labels:
        effective_name = None
        status = EffectiveNameStatus.UNAVAILABLE_FOR_WINDOW
    else:
        effective_name = None
        status = EffectiveNameStatus.MISSING
    return ProductPresentation(
        product=product,
        effective_name=effective_name,
        effective_name_status=status,
        names=tuple(ProductName.from_alias(label) for label in labels),
    )
