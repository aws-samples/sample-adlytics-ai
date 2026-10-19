# SPDX-License-Identifier: MIT-0
"""Resolve qualified source marketplace and product identifiers into coherent commerce identity."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import cast

from domain.semantics.identity import (
    CommerceScopeKey,
    IdentityMapping,
    IdentityResolution,
    IdentityResolutionError,
    MarketplaceKey,
    ProductKey,
    SourceIdentifier,
    resolve_identity,
)

__all__ = [
    "QUERY_PARAMETER_MARKETPLACE_NAMESPACES",
    "CommerceIdentityBridge",
    "MarketplaceDisplayName",
    "ProductIdentifierRule",
    "ResolveCommerceIdentityRequest",
    "ResolvedCommerceIdentity",
    "resolve_commerce_identity",
]

QUERY_PARAMETER_MARKETPLACE_NAMESPACES = frozenset(
    {"amc.segment_marketplace_id", "amc.arp_marketplace_name"}
)
"""Marketplace alias namespaces that exist to fill an AMC query parameter, never a bare value.

The ASI `segment_marketplace_id` value is a bare integer (`1` for the US). Matched by value
alone, a filter, a manifest marketplace or a chat reference of "1" would resolve to a
marketplace. These aliases resolve only as namespace-qualified source identifiers. Pack 15's
`amc.arp_marketplace_name` (plan D15) is the same kind: it fills one parameter and never names a
marketplace a delivery or a question carries.
"""


@dataclass(frozen=True, slots=True)
class ProductIdentifierRule:
    """Effective-dated bridge from one qualified source namespace into canonical product keys."""

    source_namespace: str
    canonical_namespace: str
    value_pattern: str
    normalization: str
    mapping_version: int
    valid_from: date
    valid_to: date | None = None

    def __post_init__(self) -> None:
        if not self.source_namespace.strip() or not self.canonical_namespace.strip():
            raise IdentityResolutionError("product identifier rule requires both namespaces.")
        try:
            SourceIdentifier(self.source_namespace, "probe")
            ProductKey(
                f"{self.canonical_namespace}:probe",
                CommerceScopeKey("probe"),
                MarketplaceKey("probe"),
            )
            re.compile(self.value_pattern)
        except (TypeError, ValueError, re.error) as error:
            raise IdentityResolutionError(f"invalid product identifier rule: {error}.") from None
        if self.normalization not in {"lowercase", "preserve"}:
            raise IdentityResolutionError("product normalization must be lowercase or preserve.")
        if self.mapping_version < 1:
            raise IdentityResolutionError("product identifier rule version starts at 1.")
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise IdentityResolutionError("product identifier rule cannot end before it begins.")

    def applies(self, source: SourceIdentifier, *, on: date) -> bool:
        """Whether this rule governs the source namespace on the business date."""
        return (
            source.namespace == self.source_namespace
            and self.valid_from <= on
            and (self.valid_to is None or on < self.valid_to)
        )


@dataclass(frozen=True, slots=True)
class MarketplaceDisplayName:
    """Preferred public display text for one canonical marketplace."""

    marketplace: MarketplaceKey
    value: str

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.marketplace), MarketplaceKey):
            raise TypeError("marketplace display name requires a MarketplaceKey.")
        if not self.value.strip():
            raise IdentityResolutionError("marketplace display name requires non-empty text.")


@dataclass(frozen=True, slots=True)
class CommerceIdentityBridge:
    """Governed mappings inside one declared canonical commerce scope."""

    commerce_scope: CommerceScopeKey
    marketplace_mappings: tuple[IdentityMapping[MarketplaceKey], ...]
    marketplace_display_names: tuple[MarketplaceDisplayName, ...] = ()
    product_mappings: tuple[IdentityMapping[ProductKey], ...] = ()
    product_rules: tuple[ProductIdentifierRule, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.commerce_scope), CommerceScopeKey):
            raise TypeError("commerce bridge requires a CommerceScopeKey.")
        if any(
            not isinstance(cast("object", mapping.target), MarketplaceKey)
            for mapping in self.marketplace_mappings
        ):
            raise TypeError("marketplace mappings must target MarketplaceKey values.")
        if any(
            not isinstance(cast("object", mapping.target), ProductKey)
            for mapping in self.product_mappings
        ):
            raise TypeError("product mappings must target ProductKey values.")
        if any(
            mapping.target.commerce_scope != self.commerce_scope
            for mapping in self.product_mappings
        ):
            raise IdentityResolutionError("product mapping belongs to another commerce scope.")
        marketplace_targets = {mapping.target for mapping in self.marketplace_mappings}
        display_targets = tuple(item.marketplace for item in self.marketplace_display_names)
        if len(set(display_targets)) != len(display_targets):
            raise IdentityResolutionError(
                "marketplace display names repeat a canonical marketplace."
            )
        if any(target not in marketplace_targets for target in display_targets):
            raise IdentityResolutionError(
                "marketplace display name names an undeclared marketplace."
            )
        if any(
            mapping.target.marketplace not in marketplace_targets
            for mapping in self.product_mappings
        ):
            raise IdentityResolutionError("product mapping names an undeclared marketplace.")
        _reject_overlapping_mappings(self.marketplace_mappings, "marketplace")
        _reject_overlapping_mappings(self.product_mappings, "product")
        _reject_overlapping_rules(self.product_rules)
        for mapping in self.product_mappings:
            for rule in self.product_rules:
                if mapping.source.namespace == rule.source_namespace and _intervals_overlap(
                    (mapping.valid_from, mapping.valid_to),
                    (rule.valid_from, rule.valid_to),
                ):
                    raise IdentityResolutionError(
                        "exact product mapping overlaps a product identifier rule."
                    )

    def bare_value_marketplace_mappings(self) -> tuple[IdentityMapping[MarketplaceKey], ...]:
        """Marketplace mappings a source value may match without its namespace.

        Leaves out `QUERY_PARAMETER_MARKETPLACE_NAMESPACES`, so a namespace-blind lookup never
        resolves a query-parameter integer.
        """
        return tuple(
            mapping
            for mapping in self.marketplace_mappings
            if mapping.source.namespace not in QUERY_PARAMETER_MARKETPLACE_NAMESPACES
        )


@dataclass(frozen=True, slots=True)
class ResolveCommerceIdentityRequest:
    """Qualified source identifiers and the row business date selecting effective mappings."""

    marketplace: SourceIdentifier
    business_date: date
    product: SourceIdentifier | None = None


@dataclass(frozen=True, slots=True)
class ResolvedCommerceIdentity:
    """Coherent canonical identities plus reproducible mapping evidence."""

    commerce_scope: CommerceScopeKey
    marketplace: IdentityResolution[MarketplaceKey]
    product: IdentityResolution[ProductKey] | None


def resolve_commerce_identity(
    bridge: CommerceIdentityBridge,
    request: ResolveCommerceIdentityRequest,
) -> ResolvedCommerceIdentity:
    """Resolve marketplace and optional product, refusing independently valid mismatches."""
    marketplace = resolve_identity(
        request.marketplace,
        on=request.business_date,
        mappings=bridge.marketplace_mappings,
    )
    product = (
        None
        if request.product is None
        else _resolve_product(
            _ProductResolutionRequest(
                bridge=bridge,
                source=request.product,
                on=request.business_date,
                marketplace=marketplace.target,
            )
        )
    )
    if product is not None:
        if product.target.commerce_scope != bridge.commerce_scope:
            raise IdentityResolutionError("resolved product belongs to another commerce scope.")
        if product.target.marketplace != marketplace.target:
            raise IdentityResolutionError("resolved product belongs to another marketplace.")
    return ResolvedCommerceIdentity(
        commerce_scope=bridge.commerce_scope,
        marketplace=marketplace,
        product=product,
    )


@dataclass(frozen=True, slots=True)
class _ProductResolutionRequest:
    bridge: CommerceIdentityBridge
    source: SourceIdentifier
    on: date
    marketplace: MarketplaceKey


def _resolve_product(
    request: _ProductResolutionRequest,
) -> IdentityResolution[ProductKey]:
    bridge = request.bridge
    source = request.source
    on = request.on
    marketplace = request.marketplace
    direct = [mapping for mapping in bridge.product_mappings if mapping.applies(source, on=on)]
    rules = [rule for rule in bridge.product_rules if rule.applies(source, on=on)]
    if len(direct) + len(rules) != 1:
        qualifier = "no" if not direct and not rules else "ambiguous"
        raise IdentityResolutionError(
            f"{source.namespace}:{source.value} has {qualifier} product bridge on {on}."
        )
    if direct:
        selected = direct[0]
        return IdentityResolution(
            source=source,
            target=selected.target,
            mapping_version=selected.mapping_version,
            valid_from=selected.valid_from,
            valid_to=selected.valid_to,
        )
    rule = rules[0]
    if re.fullmatch(rule.value_pattern, source.value) is None:
        raise IdentityResolutionError(
            f"{source.namespace}:{source.value} does not match its governed product pattern."
        )
    normalized = source.value.lower() if rule.normalization == "lowercase" else source.value
    return IdentityResolution(
        source=source,
        target=ProductKey(
            f"{rule.canonical_namespace}:{normalized}",
            commerce_scope=bridge.commerce_scope,
            marketplace=marketplace,
        ),
        mapping_version=rule.mapping_version,
        valid_from=rule.valid_from,
        valid_to=rule.valid_to,
    )


def _intervals_overlap(
    left: tuple[date, date | None],
    right: tuple[date, date | None],
) -> bool:
    left_from, left_to = left
    right_from, right_to = right
    return (left_to is None or right_from < left_to) and (right_to is None or left_from < right_to)


def _reject_overlapping_mappings(
    mappings: tuple[IdentityMapping[MarketplaceKey], ...] | tuple[IdentityMapping[ProductKey], ...],
    label: str,
) -> None:
    for index, left in enumerate(mappings):
        for right in mappings[index + 1 :]:
            if left.source == right.source and _intervals_overlap(
                (left.valid_from, left.valid_to), (right.valid_from, right.valid_to)
            ):
                raise IdentityResolutionError(f"overlapping {label} identity mappings.")


def _reject_overlapping_rules(rules: tuple[ProductIdentifierRule, ...]) -> None:
    for index, left in enumerate(rules):
        for right in rules[index + 1 :]:
            if left.source_namespace == right.source_namespace and _intervals_overlap(
                (left.valid_from, left.valid_to), (right.valid_from, right.valid_to)
            ):
                raise IdentityResolutionError("overlapping product identifier rules.")
