# SPDX-License-Identifier: MIT-0
"""The value objects several subdomains share, and the two constants they agree on.

The shared kernel in the DDD sense, on the same terms as `provenance.py`: changing one of
these means checking every subdomain that uses it. They live here rather than in a
subdomain because none of them owns one — curation writes money, a port returns a coverage
window, measurement computes at a grain, narration reports all three.

**Two constants moved here because they existed twice.** `FOUR_PLACES` was declared in both
`domain/curation/mapping.py` and `adapters/jobs/fixture_generation/shapes.py`, and
`CURRENCY_PATTERN` in `adapters/jobs/schema_generation/dq.py`. Both describe facts the
canonical model owns — the stored scale of a monetary column, and what a currency code is —
so two copies could disagree about the scale money is stored at. `Money` has to agree with
`scaled()` exactly, and one definition is the only way to guarantee that.

**Where rounding is allowed, stated once.** `scaled()` rounds, because dividing by a
divisor genuinely produces more places than `decimal(38,4)` holds. Nothing else may:
`Money` *rejects* excess precision rather than quantizing it away, matching the generated
row models, which set `decimal_places=4` so an over-precise delivered value is refused
rather than silently rounded. Money that rounded on construction would hide the arithmetic
error it exists to prevent.

Invariants raise `ValueError` at construction, matching `provenance.py` and the request
objects in `ports/outbound`. Pure domain: no AWS, no serialisation, no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Self, cast

__all__ = [
    "CURRENCY_PATTERN",
    "FOUR_PLACES",
    "AsinKey",
    "DateRange",
    "Grain",
    "Money",
]

FOUR_PLACES = Decimal("0.0001")
"""The stored scale of every monetary column, `decimal(38,4)`.

One scale for the whole model, because scaled and unscaled money share a storage type —
AMC delivers attributed sales already at this precision while cost columns arrive as
scaled integers, and a second scale would make the two incomparable.
"""

MONETARY_DECIMAL_PLACES = -cast("int", FOUR_PLACES.as_tuple().exponent)
"""How many decimal places that scale allows, derived so the two cannot disagree.

The cast is safe by construction: `as_tuple().exponent` is only a string for NaN and the
infinities, and the literal above is neither.
"""

CURRENCY_PATTERN = "^[A-Z]{3}$"
"""ISO 4217 is three uppercase letters.

A currency column holding `usd`, an empty string or a marketplace id is a currency we
cannot convert, and FX conversion happens in the semantic layer long after the load that
could have rejected it (ADR-004).
"""

_CURRENCY = re.compile(CURRENCY_PATTERN)


@dataclass(frozen=True, slots=True)
class Money:
    """An amount and the currency it is denominated in, which are never separable.

    The canonical model forbids a bare monetary amount, so this type forbids one too: there
    is no constructor that yields an amount without a currency. `Units` enforces the same
    rule at the response envelope; this enforces it everywhere upstream of that.

    **Addition across currencies raises rather than converting.** ADR-004 puts conversion in
    the semantic layer, using the rate as of the fact's own date, and gives it a dated
    `fx_rate` table to do it with. A `Money` that converted would have to pick a rate, and
    the only rate available at this level is the wrong one — today's. Refusing is what makes
    "convert late" structural instead of a rule to remember.
    """

    amount: Decimal
    currency_code: str

    def __post_init__(self) -> None:
        # `type(...) is not` rather than `isinstance`: the annotation already says Decimal, so
        # pyright treats an isinstance check as unreachable, yet this is a plain dataclass and
        # an untyped caller can still pass a float. That is the defect the guard exists for —
        # 11.37 has no exact binary representation and the error survives every later sum.
        if type(self.amount) is not Decimal:
            raise ValueError(
                f"amount must be a Decimal, not {type(self.amount).__name__}. "
                "A binary float cannot hold a monetary value exactly."
            )
        if not _CURRENCY.match(self.currency_code):
            raise ValueError(
                f"currency_code {self.currency_code!r} is not an ISO 4217 code "
                f"matching {CURRENCY_PATTERN}."
            )
        exponent = self.amount.as_tuple().exponent
        if not isinstance(exponent, int):
            # A string exponent is how Decimal spells NaN and the infinities.
            raise ValueError(f"amount {self.amount} is not a finite quantity.")
        if -exponent > MONETARY_DECIMAL_PLACES:
            raise ValueError(
                f"amount {self.amount} carries more than {MONETARY_DECIMAL_PLACES} decimal "
                "places, which decimal(38,4) cannot hold. Rounding belongs in "
                "curation.mapping.scaled, at the divisor, and nowhere else."
            )

    def __add__(self, other: Money) -> Money:
        self._must_agree(other, "added")
        return Money(self.amount + other.amount, self.currency_code)

    def __sub__(self, other: Money) -> Money:
        self._must_agree(other, "subtracted")
        return Money(self.amount - other.amount, self.currency_code)

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency_code)

    def _must_agree(self, other: Money, verb: str) -> None:
        if self.currency_code != other.currency_code:
            raise ValueError(
                f"{self.currency_code} and {other.currency_code} cannot be {verb}. "
                "Conversion uses the rate as of the fact's date and belongs in the "
                "semantic layer (ADR-004)."
            )

    @classmethod
    def zero(cls, currency_code: str) -> Self:
        """Build a zero that still knows its currency, so a total can start somewhere."""
        return cls(Decimal(0), currency_code)

    @classmethod
    def total(cls, amounts: tuple[Money, ...]) -> Money:
        """Sum amounts that must already agree on currency.

        Refuses an empty sequence rather than returning zero, because a zero would need a
        currency this cannot know — and a zero total is indistinguishable from "no rows",
        which is the distinction this product must not blur.
        """
        if not amounts:
            raise ValueError(
                "an empty total has no currency, and zero is not the same answer as no rows."
            )
        first, *rest = amounts
        result = first
        for item in rest:
            result = result + item
        return result

    def __str__(self) -> str:
        return f"{self.amount} {self.currency_code}"


@dataclass(frozen=True, slots=True)
class DateRange:
    """An inclusive range of business days.

    **Deliberately carries no timezone.** A day range means different instants depending on
    which source bucketed the day — Vendor Central always PST, AMC UTC by default — and
    ADR-004 establishes that re-bucketing a pre-aggregated day is fabrication rather than
    conversion. So the range stays as the user expressed it, and the *answer* states which
    boundary applied, read from the fact's own `source_timezone` column. Attaching a
    timezone here would force that choice before the fact being queried is known, and imply
    a conversion that is not available.

    Inclusive at both ends because `date_key` is a stored date and a question about a week
    means seven days, not six and a boundary.
    """

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"end {self.end} cannot precede start {self.start}.")

    @property
    def days(self) -> int:
        """How many business days the range covers, both ends included."""
        return (self.end - self.start).days + 1

    def covers(self, other: DateRange) -> bool:
        """Whether this range fully contains another.

        Partial overlap is not coverage. A window half-covered by data produces a number
        that looks whole, which is the answer this product must refuse — so the test is
        containment and never intersection.
        """
        return self.start <= other.start and other.end <= self.end

    def overlaps(self, other: DateRange) -> bool:
        """Whether this range shares any day with another.

        The opposite question to `covers`, and both are needed because they answer for opposite
        sides of the platform. Coverage asks whether stored data is sufficient to answer a
        question. Intersection asks whether two *loads* describe any of the same days — which,
        for a fact keyed on its period bounds, is how the same day comes to be counted twice.

        Two ranges that are exactly equal overlap by this test, and callers must decide whether
        that matters: an identical re-pull is a restatement the watermark supersedes wholesale,
        while a merely intersecting one lands beside what it duplicates.
        """
        return self.start <= other.end and other.start <= self.end

    def __str__(self) -> str:
        return f"{self.start.isoformat()}..{self.end.isoformat()}"


@dataclass(frozen=True, slots=True)
class Grain:
    """What one row represents.

    Mandatory wherever a figure is reported. A correct figure at an unstated grain is the
    failure mode this product must not have, so a response cannot omit it.

    Declared here rather than in `domain/narrative` — where it began — because `narration`
    consumes a grain and does not own one. `domain/catalog` already imported it from
    narration to describe a listing, which is the same backwards direction `provenance.py`
    records fixing for `Suppression` and `MeasurementBasis`. `shaped_response` re-exports it
    so the envelope still reads as one vocabulary at its own boundary.
    """

    dimensions: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.dimensions:
            raise ValueError("Grain requires at least one dimension.")

    def __str__(self) -> str:
        return " x ".join(self.dimensions)


@dataclass(frozen=True, slots=True)
class AsinKey:
    """A product's identity: an ASIN and the marketplace it was observed in.

    Composite because ADR-023 keys `dim_product` on `(amazon_asin, marketplace_key)` and
    every fact joins on both. An ASIN alone is not a product identity — whether the same
    ASIN can appear under two marketplaces is [unverified] and recorded as Q-16, and the
    composite is free if it cannot and necessary if it can.

    **No format rule on the ASIN beyond being present.** No source documentation in this
    repository states one, and the obvious guess is wrong: book ASINs are ISBNs, so a
    `B0`-prefix or a fixed-length check would reject legitimate products. A validation rule
    invented here would be a rule about our assumptions rather than about ASINs.
    """

    amazon_asin: str
    marketplace_key: str

    def __post_init__(self) -> None:
        if not self.amazon_asin:
            raise ValueError("amazon_asin is required.")
        if not self.marketplace_key:
            raise ValueError(
                "marketplace_key is required: a product's identity is the pair, because "
                "every fact joins dim_product on both (ADR-023)."
            )

    def __str__(self) -> str:
        return f"{self.amazon_asin}@{self.marketplace_key}"
