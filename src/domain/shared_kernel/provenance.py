# SPDX-License-Identifier: MIT-0
"""Provenance vocabulary: what a number is, and what was withheld from it.

The shared kernel in the DDD sense — the small set of model elements several
subdomains agree to share, on the understanding that changing one means checking
every subdomain that uses it. These three live here rather than in any single
subdomain because none of them owns one: a fact carries a measurement basis, a port
reports one, a response declares one, and the narration layer reads one.

They were originally declared in `domain/narrative/shaped_response.py`, which made
`ports/outbound/catalog.py` import from the *narration* subdomain in order to
describe a query result. `import-linter` permits it — both are domain — but the
direction is backwards: narration consumes a measurement basis, it does not own one.
Adding suppression to the query port would have made that three ports reaching into
narration.

Duplicating them was the alternative, and for `MeasurementBasis` that would be
actively unsafe: its whole purpose is "never add these together", so two copies that
drift would silently defeat it.

Concepts owned by docs/canonical-model.md section 5 and
docs/concepts/provenance-chain.md. Pure domain: no AWS, no serialisation, no I/O.
Invariants raise `ValueError` at construction, matching the value objects in
`ports/outbound`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class MeasurementBasis(StrEnum):
    """Why two numbers may not be added together.

    Mirrors `dim_measurement_basis` in docs/canonical-model.md. These bases do not
    reconcile with each other, and that is by design rather than a defect: ad
    attribution and retail actuals are different measurement systems, so a single
    `revenue` column spanning both would be a bug with good branding. See ADR-002.
    """

    AD_ATTRIBUTED = "ad_attributed"
    RETAIL_ACTUAL = "retail_actual"
    MODELLED = "modelled"


class FreshnessStatus(StrEnum):
    """How much the data behind a response can be trusted right now.

    `QUARANTINED` is not a worse `STALE`. It means a load was rejected on provenance
    and the fact must not be answered from at all, even though the last good
    partition remains queryable.
    """

    FRESH = "fresh"
    STALE = "stale"
    QUARANTINED = "quarantined"

    NEVER_LOADED = "never_loaded"
    """No load has ever established anything for this fact.

    Distinct from "no such fact", and the distinction is the whole reason it is a state rather
    than an absent record. A caller that conflates them tells a user their question is
    unanswerable, when the truth is that it becomes answerable as soon as the first load runs —
    a different answer, and a more useful one.

    Primarily a *registry* state, and a capability response carries it routinely because
    describing what is not yet loaded is exactly that response's job.

    **A measurement response can carry it, which this docstring used to deny.** The denial was
    reasonable and wrong: a tool with no data does refuse, but rows in a table and a missing record
    of the load that wrote them are an independent pair, so figures can arrive with no observation
    behind them. Asserting the impossibility let a sentence be written that assumed it — an answer
    reported a real ROAS and then that nothing had ever been loaded. What the answer must never do
    is describe an absence it does not have; ADR-036 records the two defects and the fix.
    """


@dataclass(frozen=True, slots=True)
class Suppression:
    """Whether a privacy threshold withheld data.

    Distinguishing zero from suppressed is impossible downstream unless it is stated
    here — and impossible *anywhere* unless it was declared at extraction, because
    AMC's default can null an identifying value without a discriminator, making privacy
    withholding indistinguishable from an ordinary absent value. See
    docs/concepts/provenance-chain.md section 2.4.

    So this type never detects suppression. It carries a fact established before the
    data reached us, and exists so that dropping the fact is impossible.
    """

    rows_suppressed: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.rows_suppressed and not self.reason:
            raise ValueError("A suppressed result must state why.")


WITHHELD_AMOUNT_REASON: Final = "withheld"
WITHHELD_AMOUNT_TEXT: Final = "Amazon withheld this amount, so it isn't added to the total."


@dataclass(frozen=True, slots=True)
class WithheldAmount:
    """A withheld money amount that came back without its currency: a typed absence.

    On multi-source withheld rows AMC returned purchase-side sums and every currency column NULL
    [verified, Sandbox and STANDARD, 2026-10-09; docs/sources/amc.md §5]; that a single-source
    withheld row nulls its LOW currency column with its keys is [inference] from the verified
    per-column nulling of LOW columns on one-user rows (2026-08-21). An amount with no
    currency has no unit, so it is never added to a money total; it reads "withheld" instead
    (AMC-V decision 5, owner S-8). `withheld_money_counts` is the one test of that rule.
    Counts and other non-money measures on the same row are unaffected.

    The measurement carries it typed (`Withholding.withheld_amount`); an answer's `Suppression`
    states its `text` in the reason, so the wire shape of every answer and page stays as it is.
    """

    rows: int
    """How many withheld rows, or result groups, carried such an amount."""

    def __post_init__(self) -> None:
        if self.rows < 1:
            raise ValueError("A withheld amount stands for at least one row.")

    @property
    def reason(self) -> str:
        """The typed absence reason, `withheld`."""
        return WITHHELD_AMOUNT_REASON

    @property
    def text(self) -> str:
        """What an answer or artifact says in place of the amount."""
        return WITHHELD_AMOUNT_TEXT


def withheld_money_counts(currency: object) -> bool:
    """Whether a withheld money amount may join a money total: only beside its own currency."""
    return currency is not None and str(currency).strip() != ""


_PRODUCER_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ProducerGovernance:
    """Exact producer and interpretation artifacts bound into a landed manifest."""

    query_pack_id: str
    query_pack_version: int
    query_pack_digest: str
    query_digest: str
    data_contract_id: str
    data_contract_digest: str
    source_pack_id: str
    source_pack_digest: str

    def __post_init__(self) -> None:
        for label, value in (
            ("query_pack_id", self.query_pack_id),
            ("data_contract_id", self.data_contract_id),
            ("source_pack_id", self.source_pack_id),
        ):
            if not _PRODUCER_ID.fullmatch(value):
                raise ValueError(f"{label} is not a governed artifact identifier.")
        if self.query_pack_version < 1:
            raise ValueError("query pack version starts at 1.")
        for label, value in (
            ("query_pack_digest", self.query_pack_digest),
            ("query_digest", self.query_digest),
            ("data_contract_digest", self.data_contract_digest),
            ("source_pack_digest", self.source_pack_digest),
        ):
            if not _SHA256.fullmatch(value):
                raise ValueError(f"{label} must be an algorithm-qualified SHA-256 digest.")

    def as_dict(self) -> dict[str, object]:
        """Project onto the stable manifest field vocabulary."""
        return {
            "query_pack_id": self.query_pack_id,
            "query_pack_version": self.query_pack_version,
            "query_pack_digest": self.query_pack_digest,
            "query_digest": self.query_digest,
            "data_contract_id": self.data_contract_id,
            "data_contract_digest": self.data_contract_digest,
            "source_pack_id": self.source_pack_id,
            "source_pack_digest": self.source_pack_digest,
        }
