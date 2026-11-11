# SPDX-License-Identifier: MIT-0
"""Display ASINs from canonical product keys: a wrong key would show or join the wrong product."""

from __future__ import annotations

import pytest

from domain.forecasting.identity import (
    ProductKeyError,
    display_asin,
    display_map,
    parse_request_asin,
)
from domain.forecasting.models import SkipReason

pytestmark = pytest.mark.unit


def test_the_prefix_is_stripped_and_the_asin_upper_cased() -> None:
    assert display_asin("amazon_asin:b0fixtur01") == "B0FIXTUR01"


@pytest.mark.parametrize(
    "key", ["b0fixtur01", "amc_asin:b0fixtur01", "amazon_asin:b0fix", "amazon_asin:b0fixtur-1"]
)
def test_a_key_outside_the_vendor_namespace_or_shape_is_refused(key: str) -> None:
    with pytest.raises(ProductKeyError) as raised:
        display_asin(key)
    assert raised.value.reason is SkipReason.PRODUCT_KEY_INVALID


def test_two_canonical_keys_with_one_display_asin_are_refused() -> None:
    with pytest.raises(ProductKeyError) as raised:
        display_map(["amazon_asin:b0fixtur01", "amazon_asin:B0FIXTUR01"])
    assert raised.value.reason is SkipReason.DISPLAY_ASIN_COLLISION


def test_distinct_keys_map_one_to_one() -> None:
    mapped = display_map(["amazon_asin:b0fixtur01", "amazon_asin:b0fixtur02"])
    assert mapped.by_canonical == {
        "amazon_asin:b0fixtur01": "B0FIXTUR01",
        "amazon_asin:b0fixtur02": "B0FIXTUR02",
    }


def test_a_request_asin_in_either_case_is_accepted_and_upper_cased() -> None:
    assert parse_request_asin("b0Fixtur01") == "B0FIXTUR01"
    with pytest.raises(ValueError, match="10 letters or digits"):
        parse_request_asin("B0FIXTUR0")
