# SPDX-License-Identifier: MIT-0
"""The display ASIN the page and tools use, from T's canonical product key (design §3b, R2-16)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from domain.forecasting.models import SkipReason

CANONICAL_PREFIX = "amazon_asin:"
_DISPLAY = re.compile(r"^[A-Z0-9]{10}$")
_REQUEST = re.compile(r"^[A-Za-z0-9]{10}$")


class ProductKeyError(ValueError):
    """A canonical key that yields no display ASIN, or two keys that yield the same one."""

    def __init__(self, reason: SkipReason, key: str) -> None:
        super().__init__(f"{reason.value}: {key}")
        self.reason = reason


def display_asin(canonical: str) -> str:
    """Strip `amazon_asin:` and upper-case; refuse anything that isn't then a 10-character ASIN."""
    if not canonical.startswith(CANONICAL_PREFIX):
        raise ProductKeyError(SkipReason.PRODUCT_KEY_INVALID, canonical)
    shown = canonical.removeprefix(CANONICAL_PREFIX).upper()
    if _DISPLAY.fullmatch(shown) is None:
        raise ProductKeyError(SkipReason.PRODUCT_KEY_INVALID, canonical)
    return shown


@dataclass(frozen=True)
class DisplayMap:
    """Each canonical key's display ASIN, refused whole on a collision."""

    by_canonical: dict[str, str]


def display_map(canonicals: Iterable[str]) -> DisplayMap:
    """Map every canonical key to its display ASIN; two keys giving one display ASIN refuse."""
    seen: dict[str, str] = {}
    out: dict[str, str] = {}
    for canonical in canonicals:
        shown = display_asin(canonical)
        other = seen.get(shown)
        if other is not None and other != canonical:
            raise ProductKeyError(SkipReason.DISPLAY_ASIN_COLLISION, shown)
        seen[shown] = canonical
        out[canonical] = shown
    return DisplayMap(out)


def parse_request_asin(value: str) -> str:
    """Accept a requested ASIN in either case and return its display form; refuse anything else."""
    if _REQUEST.fullmatch(value) is None:
        raise ValueError("an ASIN is 10 letters or digits")
    return value.upper()
