# SPDX-License-Identifier: MIT-0
"""Private Retail health custody: product keys for the served rows and their children.

ADR-125 decision 2, as amended by ADR-145: for each preset, one child body per served product
row is baked in the parent's publication. The parent custody's header names each row's
canonical product key under a server-only ``product_ref``; the children's digests have one home,
the parent custody's sibling index, which the stage builds. The set stays closed: the stage
refuses a publication whose children aren't exactly the served rows before any write, so a serve
can never read a child the bake didn't attest. From render format 12 the same set also holds one
Products and ads part, whose private custody binds only its public bytes.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import cast

from domain.semantics.identity import CommerceScopeKey, MarketplaceKey, ProductKey

__all__ = [
    "RETAIL_HEALTH_CUSTODY_MANIFEST_KIND",
    "RETAIL_PRODUCT_ADS_CUSTODY_KIND",
    "RETAIL_PRODUCT_CHILD_CUSTODY_KIND",
    "RetailHealthCustodyManifest",
    "RetailHealthCustodyProduct",
    "RetailProductAdsCustody",
    "RetailProductChildCustody",
    "product_ref_for",
    "retail_health_custody_manifest_from_payload",
    "retail_health_custody_manifest_payload",
    "retail_product_ads_custody_from_payload",
    "retail_product_ads_custody_payload",
    "retail_product_child_custody_from_payload",
    "retail_product_child_custody_payload",
]

RETAIL_HEALTH_CUSTODY_MANIFEST_KIND = "retail_health_custody_manifest/v2"
"""v2 (ADR-145) drops the inline child digests; a v1 manifest is never read in the v2 shape."""
RETAIL_PRODUCT_CHILD_CUSTODY_KIND = "retail_product_child_custody"
RETAIL_PRODUCT_ADS_CUSTODY_KIND = "retail_product_ads_custody"
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


def product_ref_for(product: ProductKey) -> str:
    """Name one canonical product for object keys and custody; server-only, never served."""
    identity = "|".join((product.commerce_scope.value, product.marketplace.value, product.value))
    return hashlib.sha256(identity.encode()).hexdigest()[:32]


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError(f"{label} digest is invalid.")
    return value


@dataclass(frozen=True, slots=True)
class RetailHealthCustodyProduct:
    """One served row's canonical product key under its server-only reference."""

    product_ref: str
    product: ProductKey

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.product), ProductKey):
            raise TypeError("retail custody product requires a ProductKey.")
        if self.product_ref != product_ref_for(self.product):
            raise ValueError("retail custody product reference does not name its product.")


@dataclass(frozen=True, slots=True)
class RetailHealthCustodyManifest:
    """Ordered custody products matching the public rows, and the movers card's product."""

    body_digest: str
    products: tuple[RetailHealthCustodyProduct, ...]
    movers_product_ref: str | None = None
    """The product the baked Largest movers card names first; its ``open_product_detail``."""
    kind: str = RETAIL_HEALTH_CUSTODY_MANIFEST_KIND

    def __post_init__(self) -> None:
        if self.kind != RETAIL_HEALTH_CUSTODY_MANIFEST_KIND:
            raise ValueError("retail health custody manifest kind is unsupported.")
        _digest(self.body_digest, "retail health custody parent body")
        refs = tuple(item.product_ref for item in self.products)
        if len(set(refs)) != len(refs):
            raise ValueError("retail health custody products are unique.")
        if self.movers_product_ref is not None and self.movers_product_ref not in refs:
            raise ValueError("the movers product is one of the served rows.")

    def product(self, product_ref: str) -> ProductKey | None:
        """Return the canonical product a served row names, or none."""
        return next(
            (item.product for item in self.products if item.product_ref == product_ref), None
        )


def _product_payload(product: ProductKey) -> dict[str, str]:
    return {
        "commerce_scope": product.commerce_scope.value,
        "marketplace": product.marketplace.value,
        "value": product.value,
    }


def _object(value: object, keys: frozenset[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or frozenset(cast("dict[str, object]", value)) != keys:
        raise ValueError(f"{label} fields are not exact.")
    return cast("dict[str, object]", value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonblank text.")
    return value


def _product(value: object) -> ProductKey:
    item = _object(value, frozenset({"commerce_scope", "marketplace", "value"}), "product key")
    return ProductKey(
        _text(item["value"], "product value"),
        CommerceScopeKey(_text(item["commerce_scope"], "commerce scope")),
        MarketplaceKey(_text(item["marketplace"], "marketplace")),
    )


def retail_health_custody_manifest_payload(
    manifest: RetailHealthCustodyManifest,
) -> dict[str, object]:
    """Encode the private parent custody header; it is never served."""
    return {
        "kind": manifest.kind,
        "body_digest": manifest.body_digest,
        "products": [
            {"product_ref": item.product_ref, "product": _product_payload(item.product)}
            for item in manifest.products
        ],
        "movers_product_ref": manifest.movers_product_ref,
    }


def retail_health_custody_manifest_from_payload(value: object) -> RetailHealthCustodyManifest:
    """Strictly decode and re-attest one parent custody header (its index split off first)."""
    item = _object(
        value,
        frozenset({"kind", "body_digest", "products", "movers_product_ref"}),
        "retail health custody manifest",
    )
    raw_products = item["products"]
    if not isinstance(raw_products, list):
        raise ValueError("retail health custody collections are malformed.")
    products = tuple(
        RetailHealthCustodyProduct(
            _text(entry["product_ref"], "product ref"), _product(entry["product"])
        )
        for entry in (
            _object(raw, frozenset({"product_ref", "product"}), "custody product")
            for raw in cast("list[object]", raw_products)
        )
    )
    movers = item["movers_product_ref"]
    manifest = RetailHealthCustodyManifest(
        _digest(item["body_digest"], "parent body"),
        products,
        None if movers is None else _text(movers, "movers product ref"),
        _text(item["kind"], "custody kind"),
    )
    if retail_health_custody_manifest_payload(manifest) != value:
        raise ValueError("retail health custody manifest is not canonical.")
    return manifest


@dataclass(frozen=True, slots=True)
class RetailProductChildCustody:
    """One child's private custody: its product and the public bytes it accompanies."""

    product_ref: str
    product: ProductKey
    body_digest: str
    kind: str = RETAIL_PRODUCT_CHILD_CUSTODY_KIND

    def __post_init__(self) -> None:
        if self.kind != RETAIL_PRODUCT_CHILD_CUSTODY_KIND:
            raise ValueError("retail product child custody kind is unsupported.")
        if self.product_ref != product_ref_for(self.product):
            raise ValueError("retail product child custody names another product.")
        _digest(self.body_digest, "retail product child")


def retail_product_child_custody_payload(custody: RetailProductChildCustody) -> dict[str, object]:
    """Encode one child's private custody."""
    return {
        "kind": custody.kind,
        "product_ref": custody.product_ref,
        "product": _product_payload(custody.product),
        "body_digest": custody.body_digest,
    }


def retail_product_child_custody_from_payload(value: object) -> RetailProductChildCustody:
    """Strictly decode and re-attest one child's private custody."""
    item = _object(
        value, frozenset({"kind", "product_ref", "product", "body_digest"}), "child custody"
    )
    custody = RetailProductChildCustody(
        _text(item["product_ref"], "product ref"),
        _product(item["product"]),
        _digest(item["body_digest"], "child body"),
        _text(item["kind"], "child custody kind"),
    )
    if retail_product_child_custody_payload(custody) != value:
        raise ValueError("retail product child custody is not canonical.")
    return custody


@dataclass(frozen=True, slots=True)
class RetailProductAdsCustody:
    """The Products and ads part's private custody: the public bytes it accompanies.

    The part names products only by the served rows' child ASINs, in row order, so it needs no
    product key here; serving checks its rows against the parent body's.
    """

    body_digest: str
    kind: str = RETAIL_PRODUCT_ADS_CUSTODY_KIND

    def __post_init__(self) -> None:
        if self.kind != RETAIL_PRODUCT_ADS_CUSTODY_KIND:
            raise ValueError("retail product ads custody kind is unsupported.")
        _digest(self.body_digest, "retail product ads")


def retail_product_ads_custody_payload(custody: RetailProductAdsCustody) -> dict[str, object]:
    """Encode the Products and ads part's private custody."""
    return {"kind": custody.kind, "body_digest": custody.body_digest}


def retail_product_ads_custody_from_payload(value: object) -> RetailProductAdsCustody:
    """Strictly decode and re-attest the Products and ads part's private custody."""
    item = _object(value, frozenset({"kind", "body_digest"}), "product ads custody")
    return RetailProductAdsCustody(
        _digest(item["body_digest"], "product ads body"),
        _text(item["kind"], "product ads custody kind"),
    )
