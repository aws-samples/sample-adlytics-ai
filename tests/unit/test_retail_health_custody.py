# SPDX-License-Identifier: MIT-0
"""Retail health's private parent custody manifest: the served rows' products (ADR-125).

ADR-145 gives the product-detail children one digest home, the stage's sibling index, so the
manifest names only the rows' products and the movers card's product. The closed child set is
the stage's input check (`test_preset_artifact_publisher.py`); the index read at serve is the
product-detail handler test's (`test_retail_health_product.py`).
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from domain.presentation.retail_health_custody import (
    RETAIL_HEALTH_CUSTODY_MANIFEST_KIND,
    RetailHealthCustodyManifest,
    RetailHealthCustodyProduct,
    product_ref_for,
    retail_health_custody_manifest_from_payload,
    retail_health_custody_manifest_payload,
)
from domain.semantics.identity import CommerceScopeKey, MarketplaceKey, ProductKey

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-125", "ADR-145")]

SCOPE = CommerceScopeKey("fictional-scope")
MARKETPLACE = MarketplaceKey("amazon_us")
BODY = "c" * 64


def _product(asin: str) -> ProductKey:
    return ProductKey(f"amazon_asin:{asin.lower()}", SCOPE, MARKETPLACE)


def _manifest() -> RetailHealthCustodyManifest:
    products = tuple(
        RetailHealthCustodyProduct(product_ref_for(_product(asin)), _product(asin))
        for asin in ("FICTA", "FICTB")
    )
    return RetailHealthCustodyManifest(BODY, products, products[0].product_ref)


def test_the_manifest_names_the_rows_products_and_no_child_digest() -> None:
    manifest = _manifest()
    payload = retail_health_custody_manifest_payload(manifest)
    assert set(payload) == {"kind", "body_digest", "products", "movers_product_ref"}
    assert payload["kind"] == RETAIL_HEALTH_CUSTODY_MANIFEST_KIND
    assert retail_health_custody_manifest_from_payload(payload) == manifest
    ref = manifest.products[1].product_ref
    assert manifest.product(ref) == _product("FICTB")
    # Paired: a ref the rows don't name has no product.
    assert manifest.product(product_ref_for(_product("STRANGER"))) is None


def test_the_kind_is_versioned_so_a_retained_manifest_is_never_read_in_the_new_shape() -> None:
    payload = retail_health_custody_manifest_payload(_manifest())
    assert RETAIL_HEALTH_CUSTODY_MANIFEST_KIND == "retail_health_custody_manifest/v2"
    with pytest.raises(ValueError, match="kind is unsupported"):
        retail_health_custody_manifest_from_payload(
            {**payload, "kind": "retail_health_custody_manifest"}
        )
    # Any extra field, an inline child set included, is refused: the index is the one home.
    with pytest.raises(ValueError, match="fields are not exact"):
        retail_health_custody_manifest_from_payload({**payload, "children": {}})
    # A parent custody is split from the stage's index before it is decoded.
    with pytest.raises(ValueError, match="fields are not exact"):
        retail_health_custody_manifest_from_payload({**payload, "sibling_index": []})


def test_products_are_unique_and_the_movers_product_is_a_served_row() -> None:
    manifest = _manifest()
    with pytest.raises(ValueError, match="products are unique"):
        replace(manifest, products=(manifest.products[0], manifest.products[0]))
    stranger = product_ref_for(_product("STRANGER"))
    with pytest.raises(ValueError, match="movers product"):
        replace(manifest, movers_product_ref=stranger)
    with pytest.raises(ValueError, match="does not name its product"):
        RetailHealthCustodyProduct(stranger, _product("FICTA"))
    # Paired: no movers product is valid.
    assert replace(manifest, movers_product_ref=None).movers_product_ref is None
