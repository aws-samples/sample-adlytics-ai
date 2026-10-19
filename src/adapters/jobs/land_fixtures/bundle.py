# SPDX-License-Identifier: MIT-0
"""Build the objects one fixture delivery puts in the raw zone.

Separated from the write for the same reason every job in this package splits planning from
applying: every key and every byte is inspectable and assertable without an AWS account, so a
defect in the layout shows up as a wrong key in a test rather than as a misplaced object in a
bucket.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Final, cast

from adapters.amc_csv import dict_rows, write_rows
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.curation.manifests import (
    BuildManifestRequest,
    build_manifest,
    checksum_of,
    declared_columns_of,
    write_manifest,
)
from adapters.jobs.fixture_generation.catalog import load_synthetic_entity_catalog
from adapters.jobs.fixture_generation.shape import CatalogShape
from adapters.jobs.fixture_generation.shapes import WINDOW_COLUMNS
from adapters.jobs.fixture_generation.vendor import (
    reconcile_vendor_aggregates,
    reported_units_adjustments,
)
from adapters.producer.packs import load_query_packs
from domain.curation.landing import (
    GOVERNED_MANIFEST_LAYOUT,
    MANIFEST_NAME,
    LandedObject,
    LandingError,
    Manifest,
)
from domain.data_contracts.contract import DataContract
from domain.operations.run_request import (
    HistoricalPolicy,
    ManifestFacts,
    RetrievalEncoding,
    is_historical,
    probe_slot_instants,
)
from domain.producer.models import (
    PACK_VARIANT_OPTION,
    PERIOD_GRAIN_OPTION,
    GovernedQueryPack,
    PeriodGrain,
    PeriodValue,
)
from domain.producer.parameters import period_parameter_value
from domain.producer.periods import is_aligned
from domain.semantics.calendar_periods import VINTAGE_MONTHS, shift_months, vintage_ending

__all__ = [
    "AMC_DAY_DELIVERY_FIXTURES",
    "AMC_FIXTURE_CONTRACTS",
    "AMC_PERIODIC_FIXTURES",
    "CAMPAIGN_DIMENSION_FIXTURES",
    "LANDABLE",
    "MAX_SEAL_DELIVERIES_PER_FIXTURE",
    "PROBE_SLOT_COUNT",
    "DeliveryClass",
    "LandedDelivery",
    "PlanSelection",
    "PlannedObject",
    "ProbeRetrieval",
    "ProbeRetrievalRequest",
    "historical_policy_for",
    "manifest_facts",
    "named_cadence_delivery",
    "plan_landings",
    "plan_probe_retrieval",
    "plan_with_backfill",
    "restating_deliveries",
    "window_prefix",
]

DATA_OBJECT_STEM = "data-0001"
"""What a single-part delivery's data object is called.

Numbered from one even though every fixture is one part, because the alternative is a name that
has to change the first time a delivery arrives split — and the reader of a real bucket should
not have to know whether a given day was one part or four.
"""

AMC_FIXTURE_CONTRACTS: dict[str, str] = {
    "generated/amc-campaign-dsp.csv": "amc-campaign-dsp",
    "generated/amc-campaign-sponsored.csv": "amc-campaign-sponsored",
    "generated/amc-dsp-impressions.csv": "amc-dsp-impressions",
    "generated/amc-dsp-clicks.csv": "amc-dsp-clicks",
    "generated/amc-dsp-views.csv": "amc-dsp-views",
    "generated/amc-reach-frequency.csv": "amc-reach-frequency",
    "generated/amc-attributed-conversion.csv": "amc-attributed-conversion",
    "generated/amc-deduplicated-temporal-attribution.csv": (
        "amc-deduplicated-temporal-attribution"
    ),
    "generated/amc-retail-purchases.csv": "amc-retail-purchases",
    "generated/amc-new-to-brand-buyers-platform.csv": "amc-new-to-brand-buyers-platform",
    "generated/amc-new-to-brand-buyers-channel.csv": "amc-new-to-brand-buyers-channel",
    "generated/amc-new-to-brand-buyers-campaign.csv": "amc-new-to-brand-buyers-campaign",
    "generated/amc-dsp-reach.csv": "amc-dsp-reach",
    # The D2 periodic corpora (packs 5-15) feeding Reach, Audiences and Retention.
    "generated/amc-exposure-groups.csv": "amc-exposure-groups",
    "generated/amc-exposure-frequency-platform.csv": "amc-exposure-frequency-platform",
    "generated/amc-exposure-frequency-channel.csv": "amc-exposure-frequency-channel",
    "generated/amc-dsp-segment-names.csv": "amc-dsp-segment-names",
    "generated/amc-segment-metadata.csv": "amc-segment-metadata",
    "generated/amc-audience-performance.csv": "amc-audience-performance",
    "generated/amc-segment-buyer-profile-inmarket.csv": "amc-segment-buyer-profile-inmarket",
    "generated/amc-segment-buyer-profile-lifestyle.csv": "amc-segment-buyer-profile-lifestyle",
    "generated/amc-segment-buyer-profile-dsp-exposed.csv": "amc-segment-buyer-profile-dsp-exposed",
    "generated/amc-acquisition-cohorts.csv": "amc-acquisition-cohorts",
    "generated/amc-acquisition-cohorts-arp.csv": "amc-acquisition-cohorts-arp",
    "traps/amc-dsp-partial-overlap-impressions.csv": "amc-dsp-impressions",
    "traps/amc-dsp-partial-overlap-clicks.csv": "amc-dsp-clicks",
    "traps/amc-dsp-partial-overlap-views.csv": "amc-dsp-views",
    "traps/amc-withheld-residual.csv": "amc-dsp-impressions",
}
"""Exact governed query pack represented by each fictional AMC result."""

# Customer-faithful scheduled-query simulation: generated AMC result files are the complete
# deterministic result corpus, and landing expands each into immutable daily query runs.
# Each run re-emits its contract's declared restatement window, exactly as a scheduled AMC
# query does; attribution measures mature across repeated windows to exercise restatement.
# Each run then lands as an ordered oldest-first series of chunked deliveries within the
# populated-bucket bound owned by write-discipline §2.1 (2026-09-22 amendment).
MAX_POPULATED_BUCKETS_PER_DELIVERY = 3
"""The write-discipline §2.1 per-derived-table populated-bucket bound this seam applies."""

_RUN_RETRIEVAL_STRIDE_SECONDS = 64
"""Retrieval-second stride between runs: every chunk gets a distinct, strictly increasing
retrieval instant (chunks per run stay far below the stride), so prefixes never collide and
provenance order encodes the oldest-first series order."""
AMC_DAILY_DATE_COLUMNS: dict[str, str] = {
    "generated/amc-campaign-dsp.csv": "source_observed_on",
    "generated/amc-campaign-sponsored.csv": "source_observed_on",
    "generated/amc-dsp-impressions.csv": "impression_date_utc",
    "generated/amc-dsp-clicks.csv": "impression_date_utc",
    "generated/amc-dsp-views.csv": "event_date_utc",
    "generated/amc-reach-frequency.csv": "event_date_utc",
    "generated/amc-attributed-conversion.csv": "conversion_event_date_utc",
    "generated/amc-deduplicated-temporal-attribution.csv": "event_date_utc",
    "generated/amc-retail-purchases.csv": "purchase_date_utc",
}
AMC_PERIODIC_FIXTURES: Final = frozenset(
    {
        "generated/amc-new-to-brand-buyers-platform.csv",
        "generated/amc-new-to-brand-buyers-channel.csv",
        "generated/amc-new-to-brand-buyers-campaign.csv",
        "generated/amc-exposure-groups.csv",
        "generated/amc-exposure-frequency-platform.csv",
        "generated/amc-exposure-frequency-channel.csv",
        "generated/amc-dsp-segment-names.csv",
        "generated/amc-segment-metadata.csv",
        "generated/amc-audience-performance.csv",
        "generated/amc-segment-buyer-profile-inmarket.csv",
        "generated/amc-segment-buyer-profile-lifestyle.csv",
        "generated/amc-segment-buyer-profile-dsp-exposed.csv",
        # Packs 14-15: one delivery per cohort vintage, each its own `as_of_month` option set.
        "generated/amc-acquisition-cohorts.csv",
        "generated/amc-acquisition-cohorts-arp.csv",
    }
)
"""Generated periodic corpora (ADR-100): one delivery per complete week or month, each its own
AMC window, landed `cadenceLagDays` after its period ends."""
AMC_DAY_DELIVERY_FIXTURES: Final = frozenset({"generated/amc-dsp-reach.csv"})
"""Generated daily distinct-count corpora (pack 4): one delivery per UTC day, each its own AMC
window, landed `cadenceLagDays` after its day. A day whose delivery holds a suppressed row isn't
landed: `reject_delivery` refuses it, and a refused delivery belongs in a test or the induced live
probe, never in the working bundle (`LANDABLE`)."""

CAMPAIGN_DIMENSION_FIXTURES: tuple[str, ...] = (
    "generated/amc-campaign-dsp.csv",
    "generated/amc-campaign-sponsored.csv",
)
"""The SCD2 campaign-dimension fixtures that carry a dimension seal (design §2.3, ADR-086)."""

MAX_SEAL_DELIVERIES_PER_FIXTURE = 63
"""The stride envelope: seal offsets `(span_days + seal_generation) x 64 + i` need i < 64."""

_SEAL_TAIL_DELIVERIES = 29
"""Tail deliveries `[b - 1, b]` for every b in `[last_day - 28, last_day]`."""

_SEAL_KEY_TARGET = "source_campaign_id"
"""The target whose source column identifies a campaign in a campaign-dimension result."""

_UNTRACKED_DIMENSION_TARGETS = frozenset(
    {"valid_from", "valid_to", "is_current", "rows_suppressed"}
)
"""Targets the sidecar's SCD2 fold never tracks (`semantic_sidecar.py`, its `tracked` rule)."""

_ATTRIBUTION_MATURING_COLUMNS = frozenset(
    {
        "conversions",
        "purchases",
        "total_purchases",
        "product_sales",
        "total_product_sales",
        "new_to_brand_product_sales",
        "units_sold",
        "total_units_sold",
        "new_to_brand_purchases",
    }
)
_VENDOR_DAILY_ARRAYS: dict[str, tuple[str, ...]] = {
    "generated/vendor-sales-by-asin.json": ("salesByAsin", "salesAggregate"),
    "generated/vendor-traffic-by-asin.json": ("trafficByAsin", "trafficAggregate"),
}
_VENDOR_MONEY_FIELDS = ("shippedRevenue", "shippedCogs", "orderedRevenue")


@dataclass(frozen=True, slots=True)
class RetiredPartition:
    """A dev-lake CURRENT partition range whose watermark a generated delivery must outrank."""

    fixture: str
    first: date
    last: date
    watermark: date
    reason: str


_RETIRED_TRAP_PARTITIONS: tuple[RetiredPartition, ...] = (
    # Vendor trap partitions that earlier landings left CURRENT on span days. The trap files now
    # sit 420 days earlier (tests/fixtures/traps/README.md); these are the old trap manifests'
    # days and watermarks at git HEAD 237df7c: traps/vendor-restatement-later.manifest.json
    # (2026-08-05, 08-19), traps/vendor-control-total-consistent.manifest.json (2026-08-10,
    # 08-12) and traps/vendor-traffic-control-total-consistent.manifest.json (2026-08-10..08-15,
    # 08-16). A generated delivery on a listed day lands one day newer (design §2.3).
    RetiredPartition(
        "generated/vendor-sales-by-asin.json",
        date(2026, 8, 5),
        date(2026, 8, 5),
        date(2026, 8, 19),
        "retired restatement trap",
    ),
    RetiredPartition(
        "generated/vendor-sales-by-asin.json",
        date(2026, 8, 10),
        date(2026, 8, 10),
        date(2026, 8, 12),
        "retired control-total trap",
    ),
    RetiredPartition(
        "generated/vendor-traffic-by-asin.json",
        date(2026, 8, 10),
        date(2026, 8, 15),
        date(2026, 8, 16),
        "retired traffic control-total trap",
    ),
)
"""Closed list; a new masked partition found in the lake is added here under review only."""

_RETIRED_GENERATION_PARTITIONS: tuple[RetiredPartition, ...] = tuple(
    RetiredPartition(fixture, date(2026, 8, 10), date(2026, 9, 15), watermark, reason)
    for fixture, watermark, reason in (
        # Dev-only and closed (ADR-086 §15; orchestrator-approved deviation, 2026-10-05). The dev
        # lake read of 2026-10-05 (task evidence state-before.md and state_read_stale_spans.out)
        # found one whole-span delivery per dataset, period 2026-08-10..2026-09-15, CURRENT on
        # every generated-lens day: Vendor sales and traffic retrieved 2026-09-15 with watermark
        # 2026-09-15, every AMC dataset retrieved 2026-09-18 with watermark 2026-09-16. Their
        # watermarks outrank the per-day watermarks a cadence delivery carries, so the
        # ingestion path correctly refuses the new days (the 2026-09-13 retail break). A
        # generated delivery touching the span lands with max(its watermark, the listed one);
        # ties publish, so the newer retrieval wins. Nothing in ingestion changes. The campaign
        # dimensions are not listed: the dimension seal (watermark last_day) owns them.
        ("generated/vendor-sales-by-asin.json", date(2026, 9, 15), "stale whole-span sales"),
        ("generated/vendor-traffic-by-asin.json", date(2026, 9, 15), "stale whole-span traffic"),
        ("generated/amc-dsp-impressions.csv", date(2026, 9, 16), "stale whole-span AMC fact"),
        ("generated/amc-dsp-clicks.csv", date(2026, 9, 16), "stale whole-span AMC fact"),
        ("generated/amc-dsp-views.csv", date(2026, 9, 16), "stale whole-span AMC fact"),
        ("generated/amc-reach-frequency.csv", date(2026, 9, 16), "stale whole-span AMC fact"),
        ("generated/amc-attributed-conversion.csv", date(2026, 9, 16), "stale whole-span AMC fact"),
        (
            "generated/amc-deduplicated-temporal-attribution.csv",
            date(2026, 9, 16),
            "stale whole-span AMC fact",
        ),
        ("generated/amc-retail-purchases.csv", date(2026, 9, 16), "stale whole-span AMC fact"),
    )
)
"""Closed list of the retired whole-span generation, hard-coded from the 2026-10-05 lake read."""


def _retired_floor(fixture: str, first: date, last: date) -> date | None:
    """Return the newest watermark a delivery covering [first, last] must outrank or tie.

    Trap partitions need a strictly newer watermark (the trap would win a tie on arrival
    order), so they contribute their watermark + 1 day; the retired generation is outranked by
    a tie, because every cadence delivery arrives after it.
    """
    floors = [
        item.watermark + timedelta(days=1)
        for item in _RETIRED_TRAP_PARTITIONS
        if item.fixture == fixture and item.first <= last and first <= item.last
    ] + [
        item.watermark
        for item in _RETIRED_GENERATION_PARTITIONS
        if item.fixture == fixture and item.first <= last and first <= item.last
    ]
    return max(floors) if floors else None


def _watermark(fixture: str, period: tuple[date, date], normal: date) -> date:
    floor = _retired_floor(fixture, *period)
    return normal if floor is None or floor <= normal else floor


LANDABLE: tuple[str, ...] = (
    # Enumerated, never globbed, and each entry carries its directory. Half the files in the trap
    # directory exist precisely to be *rejected*, and landing one would put a delivery in the
    # bucket that the chain is supposed to refuse — a legitimate test and a terrible default.
    #
    # The consistent pair first: it is the only vendor document whose detail rows actually sum
    # to its reported aggregate, so it is the only one that can clear the control-total
    # assertion. Every other vendor trap below is a trap for a *different* check.
    "traps/vendor-control-total-consistent.json",
    # Vendor traffic is the same source-document relationship: detail and reported totals must
    # arrive together so their exact daily candidates can clear one grouped control transaction.
    "traps/vendor-traffic-control-total-consistent.json",
    "traps/vendor-sourcing-nulls.json",
    "traps/vendor-fresh-nulls.json",
    "traps/vendor-negative-units.json",
    # The restatement pair, in order. They share a window and an option set and differ in
    # `lastUpdatedDate`, so they land at two prefixes and are the only way to exercise the
    # watermark decision against real objects.
    "traps/vendor-restatement-earlier.json",
    "traps/vendor-restatement-later.json",
    # Both hierarchies, and both are required because the two fixture families use different
    # ASINs. The trap one matches the control-total document's `B0TRAP*` products; the generated
    # one carries the `B0FIXTUR*` products every generated AMC fixture references.
    #
    # **A dimension has to be landed before the facts that reference it, and missing the second
    # hierarchy quarantines two facts rather than degrading them.** Dimension resolution refuses
    # a product-grain delivery whose keys are absent from `dim_product`, because such rows write
    # correctly and then drop out of every grouped answer — so `fct_attributed_conversion`, the
    # numerator of every attributed ROAS, is quarantined by a missing dimension row rather than
    # by anything wrong with the fact itself.
    #
    # They are the same contract at two windows, so they land at two prefixes and merge into one
    # SCD2 dimension rather than colliding.
    "traps/vendor-hierarchy-matching-control-total.jsonl",
    "generated/vendor-product-hierarchy.jsonl",
    # Generated Vendor source documents now carry both sibling arrays. One landable delivery
    # per source dataset avoids a raw-prefix collision while both matching contracts consume it.
    "generated/vendor-sales-by-asin.json",
    "generated/vendor-traffic-by-asin.json",
    # Campaign dimensions are separate governed query outputs over the same AMC source tables as
    # two fact packs. Their governance selects the exact contract, and layout v2 qualifies each
    # immutable raw identity by query pack even when the source window and retrieval second match.
    "generated/amc-campaign-dsp.csv",
    "generated/amc-campaign-sponsored.csv",
    # The seven AMC deliveries, generated rather than hand-built. They are the only way any AMC
    # fact holds data, and until they landed the whole advertising half of the model was empty:
    # every ROAS, every cost-per-conversion metric and both viewability measures had nothing to
    # read. Four of them write `fct_ad_exposure` between them, which is also what exercises a
    # fact assembled from several contracts.
    #
    # They are generated from our own declarations, so they prove the *pipeline* handles AMC's
    # shape — the CSV reader, the suppression columns, the four-contract fan-out — and are not
    # evidence about what Amazon actually sends. Phase 1.1 owns that distinction.
    "generated/amc-dsp-impressions.csv",
    "generated/amc-dsp-clicks.csv",
    "generated/amc-dsp-views.csv",
    "generated/amc-reach-frequency.csv",
    "generated/amc-attributed-conversion.csv",
    "generated/amc-deduplicated-temporal-attribution.csv",
    "generated/amc-retail-purchases.csv",
    # The first periodic shopper aggregate (pack 1, ADR-099, ADR-100): one delivery per complete
    # ISO week and calendar month, split from one corpus by window. Each lands as a cadence
    # delivery on the day its period's cadence lag ends.
    "generated/amc-new-to-brand-buyers-platform.csv",
    # Pack 2, the channel grain set: one delivery per period as pack 1, one row per ad product.
    "generated/amc-new-to-brand-buyers-channel.csv",
    # Pack 3, the campaign grain set: one delivery per calendar month, one row per campaign.
    "generated/amc-new-to-brand-buyers-campaign.csv",
    # Pack 4, DSP reach (plan D6): one delivery per UTC day for the catalog's few clean days, so
    # every earlier DSP day stays uncovered and reads the coverage refusal.
    "generated/amc-dsp-reach.csv",
    # The D2 periodic corpora, one delivery per period as pack 1 (ADR-100). Without them Reach,
    # Audiences and Retention resolve no period and serve their absence state in dev. Pack 5
    # (exposure groups) and packs 6-7 (frequency) feed Reach; the segment dimensions (packs 8-9)
    # precede the facts that name segments, then segment delivery (pack 13) and the three buyer
    # profile paths (packs 10-12) feed Audiences; the cohort vintages (packs 14-15) feed
    # Retention. Each declared refused period (`_refused_periods`) stays out of the bundle.
    "generated/amc-exposure-groups.csv",
    "generated/amc-exposure-frequency-platform.csv",
    "generated/amc-exposure-frequency-channel.csv",
    "generated/amc-dsp-segment-names.csv",
    "generated/amc-segment-metadata.csv",
    "generated/amc-audience-performance.csv",
    "generated/amc-segment-buyer-profile-inmarket.csv",
    "generated/amc-segment-buyer-profile-lifestyle.csv",
    "generated/amc-segment-buyer-profile-dsp-exposed.csv",
    "generated/amc-acquisition-cohorts.csv",
    "generated/amc-acquisition-cohorts-arp.csv",
    # The partial-overlap trap: three DSP deliveries in ONE partition covering DIFFERENT grains.
    # Hand-built because a generated fixture cannot produce it — the generator derives every value
    # from a column name, so its three DSP fixtures cover identical grains with identical values,
    # and a load that erased one contract's columns would leave a table indistinguishable from a
    # correct one.
    #
    # `TRAPDSP-SHARED` is covered by all three. `TRAPDSP-IMPRONLY` by impressions alone;
    # `TRAPDSP-CLICKONLY` by clicks alone. So each delivery withdraws across rows two
    # of its siblings never wrote, which is exactly what ADR-033's ownership predicate had to stop
    # short of and what G-28's diagnostic columns made unfixable. A separate window from the
    # generated fixtures, so it is its own partition and neither masks the other.
    "traps/amc-dsp-partial-overlap-impressions.csv",
    "traps/amc-dsp-partial-overlap-clicks.csv",
    "traps/amc-dsp-partial-overlap-views.csv",
    # The withheld residual: one delivery carrying a row AMC stripped of every dimension it
    # identified, which is what a grain with a single distinct user comes back as. Hand-built
    # because a generated fixture never nulls a value, and this is the row that quarantined a
    # whole partition before ADR-034 gave it somewhere to go.
    #
    # Its own window again, so the residual it produces is keyed on a period no other delivery
    # claims — which is also what demonstrates that two windows do not collide.
    "traps/amc-withheld-residual.csv",
)
"""Which fixtures are landed, in order, as paths under `tests/fixtures/`.

Deliberately excludes `traps/vendor-control-total-short.json`,
`traps/vendor-mixed-currency.json` and `traps/amc-suppression-merged.csv`. All three must
quarantine and belong in a test that asserts they do, rather than in a bundle whose purpose is a
working environment.

Generated Vendor fixtures carry the same real report-option values as serving lens defaults,
so they are landable alongside the fixture traps. The traps retain their focused coverage of
malformed and restated deliveries.
"""


@dataclass(frozen=True, slots=True)
class PlannedObject:
    """One object to write, with the key it goes to.

    `body` is bytes rather than text because a checksum is over bytes, and encoding it twice —
    once to digest and once to upload — is how the digest comes to describe something other
    than what was stored.
    """

    key: str
    body: bytes


class DeliveryClass(StrEnum):
    """What kind of delivery a planned landing is, so a phase selects it by class (design §2.3).

    `cadence` is an AMC daily-run chunk or a Vendor daily-run or restatement; `whole` is a
    generated fixture landed as one delivery; `trap` is a hand-built trap; `seal` is a
    campaign-dimension seal delivery.
    """

    CADENCE = "cadence"
    WHOLE = "whole"
    TRAP = "trap"
    SEAL = "seal"
    BACKFILL = "backfill"
    """Phase B (owner, 2026-10-06): each older day once, at its final values."""


@dataclass(frozen=True, slots=True)
class PlanSelection:
    """Which deliveries one landing invocation selects, in full-plan order.

    A delivery is selected when its class is in `classes` and, for `cadence` and `whole`, its
    run day lies in the inclusive range; `trap` and `seal` deliveries ignore the range.
    """

    classes: frozenset[DeliveryClass]
    run_days_from: date | None = None
    run_days_to: date | None = None

    def __post_init__(self) -> None:
        if not self.classes:
            raise LandingError("a plan selection names at least one delivery class.")
        if (
            self.run_days_from is not None
            and self.run_days_to is not None
            and self.run_days_from > self.run_days_to
        ):
            raise LandingError("a plan selection's run-day bounds are reversed.")

    def selects(self, delivery: LandedDelivery) -> bool:
        """Whether this selection includes the delivery."""
        if delivery.delivery_class not in self.classes:
            return False
        if delivery.delivery_class in {DeliveryClass.TRAP, DeliveryClass.SEAL}:
            return True
        day = delivery.run_day
        if day is None:
            return False
        return (self.run_days_from is None or day >= self.run_days_from) and (
            self.run_days_to is None or day <= self.run_days_to
        )


@dataclass(frozen=True, slots=True)
class LandedDelivery:
    """One delivery's objects, with the manifest kept separate because it is written last."""

    fixture: str
    manifest: Manifest
    data: tuple[PlannedObject, ...]
    marker: PlannedObject
    delivery_class: DeliveryClass = DeliveryClass.WHOLE
    run_day: date | None = None

    @property
    def ordered(self) -> tuple[PlannedObject, ...]:
        """Give the objects in the order they must be written: data first, marker last.

        The order is the entire trigger contract. Writing the marker first would fire the load
        against objects that are not there yet, and an event-driven pipeline has no way to tell
        that from a delivery whose data is missing.
        """
        return (*self.data, self.marker)


def _extension(fixture: str) -> str:
    """Take a fixture's extension, which the landed object keeps.

    Kept rather than normalised because the extension is a hint about the format and a reader
    of the bucket should not have to open a manifest to guess whether an object is CSV.
    """
    return fixture.rsplit(".", maxsplit=1)[-1]


def _format_name(fixture: str) -> str:
    """Name the reader shape from the fixture's extension.

    Three extensions, three readers. `json` is a vendor dual-array document rather than plain
    JSON, which is why this maps rather than passes the extension through.
    """
    extension = _extension(fixture)
    if extension == "csv":
        return "csv"
    if extension == "jsonl":
        return "jsonl"
    if extension == "json":
        return "json_dual_array"
    raise LandingError(f"{fixture}: no reader shape is known for a .{extension} object.")


def plan_landings(
    root: pathlib.Path, selection: PlanSelection | None = None
) -> tuple[LandedDelivery, ...]:
    """Read every enumerated fixture and build the delivery it lands as.

    With a `selection`, only the selected deliveries are returned, in full-plan order.

    The declaration beside a fixture supplies provenance — dataset, options, window, watermark,
    retrieval instant. Everything else is measured from the bytes: the digest, the byte count,
    and the columns the object actually contains. Measuring rather than declaring the columns is
    what keeps the header assertion honest; a declared list would let it pass against a column
    the file does not have.
    """
    context = _plan_context(root, load_synthetic_entity_catalog(root).shape)
    planned = [delivery for fixture in LANDABLE for delivery in _plan_fixture(context, fixture)]
    _refuse_collisions(planned)
    if selection is not None and DeliveryClass.BACKFILL in selection.classes:
        # The backfill replaces the old days' cadence and is never part of the full plan. A
        # Vendor backfill delivery is the replay's own last report for its day, at its own key.
        cut = backfill_last_day(context.shape)
        backfill = _backfill(context, planned)
        _refuse_collisions(
            [
                delivery
                for delivery in planned
                if not (
                    delivery.delivery_class is DeliveryClass.CADENCE
                    and delivery.run_day is not None
                    and (
                        (
                            delivery.run_day <= cut + timedelta(days=2)
                            and delivery.fixture.partition("#")[0] in _VENDOR_DAILY_ARRAYS
                        )
                        or (
                            delivery.run_day <= cut
                            and delivery.fixture.partition("#")[0] in AMC_PERIODIC_FIXTURES
                        )
                    )
                )
            ]
            + backfill
        )
        planned = [*planned, *backfill]
    if selection is None:
        return tuple(planned)
    return tuple(delivery for delivery in planned if selection.selects(delivery))


REPLAYED_DAYS: Final = 56
"""Phase A replays every daily run for the newest 56 days; phase B is the backfill before them."""
_BACKFILL_SLOT_BASE: Final = 64
"""Backfill retrieval slots start 64 strides above the last cadence run, above every seal
generation below 64, so no backfill prefix can equal a cadence or seal prefix."""


def backfill_last_day(shape: CatalogShape) -> date:
    """Return the newest day the phase-B backfill covers: the day before the replayed window."""
    return shape.fixture_span.last_day - timedelta(days=REPLAYED_DAYS)


def _backfill(context: _PlanContext, planned: list[LandedDelivery]) -> list[LandedDelivery]:
    """Final-state phase-B deliveries (ADR-086 §14, amended 2026-10-06).

    AMC: each fixture's days up to `backfill_last_day` in consecutive chunks of
    `MAX_POPULATED_BUCKETS_PER_DELIVERY` days from `first_day`, each row matured as of the last
    run that restates its day, with that run's watermark through the retired-floor rule. Vendor:
    the last report the replay delivers for each day (the restating report where one falls due),
    unchanged. Every row equals what the replay leaves CURRENT; only the delivery count changes.
    """
    cut = backfill_last_day(context.shape)
    if context.shape.seal_generation >= _BACKFILL_SLOT_BASE:
        raise LandingError("seal_generation would reach the backfill retrieval slots.")
    backfill: list[LandedDelivery] = []
    vendor_last: dict[tuple[str, date], LandedDelivery] = {}
    for delivery in planned:
        fixture, _, suffix = delivery.fixture.partition("#")
        if fixture in _VENDOR_DAILY_ARRAYS and delivery.delivery_class is DeliveryClass.CADENCE:
            day = date.fromisoformat(suffix.split("=", 1)[1])
            if day <= cut:
                vendor_last[(fixture, day)] = delivery  # plan order: a restatement follows
    for (_, day), delivery in vendor_last.items():
        backfill.append(replace(delivery, delivery_class=DeliveryClass.BACKFILL, run_day=day))
    # A periodic delivery never restates: its backfill is the same immutable delivery, so every
    # period that landed before the replay window is in phase B too (ADR-100).
    backfill.extend(
        replace(delivery, delivery_class=DeliveryClass.BACKFILL)
        for delivery in planned
        if delivery.fixture.partition("#")[0] in AMC_PERIODIC_FIXTURES
        and delivery.delivery_class is DeliveryClass.CADENCE
        and delivery.run_day is not None
        and delivery.run_day <= cut
    )
    for fixture in LANDABLE:
        if fixture in AMC_DAILY_DATE_COLUMNS:
            backfill.extend(_amc_backfill(context, fixture, cut))
    return backfill


def _amc_rows(text: str) -> tuple[tuple[str, ...], tuple[dict[str, str], ...]]:
    """Read an AMC fixture through AMC's CSV dialect, header and rows (`adapters.amc_csv`)."""
    fieldnames, rows = dict_rows(text)
    return fieldnames or (), tuple(cast("dict[str, str]", row) for row in rows)


def _windowed(rows: list[dict[str, str]], first: date, last: date) -> list[dict[str, str]]:
    """Stamp each row with its own delivery's AMC window, `[first, last + 1 day)`.

    A split of the generated corpus is landed as its own delivery, so it carries the window a
    producer registration for that period would (`PlanLandingRequest._check_window`), not the
    corpus's. Only the date moves; the time and format are the ones the generator wrote.
    """
    start, end = WINDOW_COLUMNS
    if not rows or start not in rows[0]:
        return rows
    first_text, end_text = first.isoformat(), (last + timedelta(days=1)).isoformat()
    return [
        row
        | {
            start: first_text + row[start][len(first_text) :],
            end: end_text + row[end][len(end_text) :],
        }
        for row in rows
    ]


def _amc_backfill(context: _PlanContext, fixture: str, cut: date) -> list[LandedDelivery]:
    contract = context.contracts[AMC_FIXTURE_CONTRACTS[fixture]]
    window = contract.restatement_window_days
    if window < 1:
        raise LandingError(f"{fixture}: no positive contract restatement window was declared.")
    path = context.fixtures / fixture
    text = path.read_text()
    declaration = _declaration(path.with_suffix("").with_suffix(".manifest.json"))
    column = AMC_DAILY_DATE_COLUMNS[fixture]
    fieldnames, parsed = _amc_rows(text)
    rows = list(parsed)
    last_day = context.shape.fixture_span.last_day
    retrieved = datetime.fromisoformat(str(declaration["retrieved_at"]).replace("Z", "+00:00"))
    span_days = context.shape.fixture_span.span_days
    days = sorted({day for row in rows if (day := date.fromisoformat(row[column])) <= cut})
    runs: list[tuple[str, str, dict[str, object], date | None]] = []
    for index, start in enumerate(range(0, len(days), MAX_POPULATED_BUCKETS_PER_DELIVERY)):
        chunk = days[start : start + MAX_POPULATED_BUCKETS_PER_DELIVERY]
        chunk_days = frozenset(chunk)

        def final_run(day: date) -> date:
            return min(day + timedelta(days=window - 1), last_day)

        chunk_rows = [
            _mature_attribution_row(
                row, column, final_run(date.fromisoformat(row[column])), context.shape
            )
            if fixture == "generated/amc-attributed-conversion.csv"
            else row
            for row in rows
            if date.fromisoformat(row[column]) in chunk_days
        ]
        body = write_rows(fieldnames, _windowed(chunk_rows, chunk[0], chunk[-1]))
        offset = (span_days + _BACKFILL_SLOT_BASE + index // _RUN_RETRIEVAL_STRIDE_SECONDS) * (
            _RUN_RETRIEVAL_STRIDE_SECONDS
        ) + index % _RUN_RETRIEVAL_STRIDE_SECONDS
        runs.append(
            (
                f"{fixture}#backfill#chunk={chunk[0].isoformat()}..{chunk[-1].isoformat()}",
                body,
                {
                    **declaration,
                    "period_start": chunk[0].isoformat(),
                    "period_end": chunk[-1].isoformat(),
                    "source_watermark": _watermark(
                        fixture, (chunk[0], chunk[-1]), final_run(chunk[-1])
                    ).isoformat(),
                    "retrieved_at": (retrieved + timedelta(seconds=offset))
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
                chunk[-1],
            )
        )
    return [
        replace(delivery, delivery_class=DeliveryClass.BACKFILL, run_day=run[3])
        for delivery, run in zip(_landed(context, fixture, runs), runs, strict=True)
    ]


@dataclass(frozen=True, slots=True)
class _PlanContext:
    """What every fixture's deliveries are planned from, read once per plan."""

    fixtures: pathlib.Path
    shape: CatalogShape
    query_packs: dict[str, GovernedQueryPack]
    contracts: dict[str, DataContract]
    encoded: frozenset[tuple[str, str]] = frozenset()
    """Every (source system, dataset) the replay band gives a retrieval encoding (`_encodings`)."""


def _plan_context(root: pathlib.Path, shape: CatalogShape) -> _PlanContext:
    fixtures = root / "tests" / "fixtures"
    return _PlanContext(
        fixtures=fixtures,
        shape=shape,
        query_packs={pack.pack_id: pack for pack in load_query_packs(root)},
        contracts={contract.contract_id: contract for _, contract in load_contracts(root)},
        encoded=frozenset(_encodings(fixtures)),
    )


def _plan_fixture(context: _PlanContext, fixture: str) -> list[LandedDelivery]:
    """Plan one enumerated fixture's deliveries: its runs, then its seal, if it has one."""
    path = context.fixtures / fixture
    if not path.exists():
        raise LandingError(f"{fixture} does not exist under {context.fixtures}.")

    declaration_path = path.with_suffix("").with_suffix(".manifest.json")
    if not declaration_path.exists():
        raise LandingError(f"{fixture} has no landing declaration beside it.")

    contract_id = AMC_FIXTURE_CONTRACTS.get(fixture)
    contract = None if contract_id is None else context.contracts[contract_id]
    text = path.read_text()
    declaration = _declaration(declaration_path)
    runs: list[tuple[str, str, dict[str, object], date | None, DeliveryClass]] = [
        (
            run_name,
            run_text,
            run_declaration,
            run_day,
            DeliveryClass.TRAP
            if fixture.startswith("traps/")
            else DeliveryClass.CADENCE
            if run_name != fixture and fixture not in CHUNKED_WHOLE_FIXTURES
            else DeliveryClass.WHOLE,
        )
        for run_name, run_text, run_declaration, run_day in _source_runs(
            _SourceRunsRequest(
                fixture,
                text,
                declaration,
                None if contract is None else contract.restatement_window_days,
                context.shape,
                next(
                    (
                        pack.effective_cadence_lag_days
                        for pack in context.query_packs.values()
                        if pack.contract_id == contract_id
                    ),
                    None,
                ),
                phase_a_only=fixture in AMC_PERIODIC_FIXTURES
                and (str(declaration["source_system"]), str(declaration["source_dataset"]))
                not in context.encoded,
            )
        )
    ]
    if fixture in CAMPAIGN_DIMENSION_FIXTURES and contract is not None:
        runs.extend(
            (run_name, run_text, run_declaration, None, DeliveryClass.SEAL)
            for run_name, run_text, run_declaration in _dimension_seal_runs(
                _SealRequest(fixture, text, declaration, contract, context.shape)
            )
        )

    return _landed(context, fixture, runs)


def _landed(
    context: _PlanContext,
    fixture: str,
    runs: list[tuple[str, str, dict[str, object], date | None, DeliveryClass]]
    | list[tuple[str, str, dict[str, object], date | None]],
) -> list[LandedDelivery]:
    """Build each run's manifest and objects; a 4-tuple run is a `whole`-class placeholder."""
    contract_id = AMC_FIXTURE_CONTRACTS.get(fixture)
    planned: list[LandedDelivery] = []
    for run in runs:
        run_name, run_text, run_declaration, run_day = run[:4]
        delivery_class = run[4] if len(run) == 5 else DeliveryClass.WHOLE  # noqa: PLR2004
        body = run_text.encode()
        name = f"{DATA_OBJECT_STEM}.{_extension(fixture)}"
        manifest = build_manifest(
            BuildManifestRequest(
                declaration=run_declaration,
                objects=(
                    LandedObject(
                        name=name,
                        byte_count=len(body),
                        checksum=checksum_of(body),
                    ),
                ),
                declared_columns=declared_columns_of(run_text, source_format=_format_name(fixture)),
            )
        )

        if contract_id is not None:
            pack = context.query_packs.get(contract_id)
            if pack is None:
                raise LandingError(f"{fixture} names unavailable governed pack {contract_id!r}.")
            # A declared variant's delivery is governed by the variant binding, as a producer
            # runs it (`variant_binding`): its own pack version and query digest. The base
            # governance on a variant delivery is refused PACK_VARIANT_MISMATCH at the load.
            variant = manifest.option_set.get(PACK_VARIANT_OPTION)
            try:
                binding = pack if variant is None else pack.variant_binding(str(variant))
            except ValueError as error:
                raise LandingError(f"{run_name}: {error}") from None
            manifest = replace(
                manifest,
                governance=binding.governance,
                layout_version=GOVERNED_MANIFEST_LAYOUT,
            )

        planned.append(
            LandedDelivery(
                fixture=run_name,
                manifest=manifest,
                data=(PlannedObject(key=f"{manifest.prefix}{name}", body=body),),
                marker=PlannedObject(
                    key=manifest.manifest_key,
                    body=write_manifest(manifest).encode(),
                ),
                delivery_class=delivery_class,
                run_day=(manifest.period_end if delivery_class is DeliveryClass.WHOLE else run_day),
            )
        )
    return planned


@dataclass(frozen=True, slots=True)
class _SealRequest:
    """One campaign-dimension result and what its seal is planned from."""

    fixture: str
    text: str
    declaration: dict[str, object]
    contract: DataContract
    shape: CatalogShape


def _seal_columns(contract: DataContract) -> tuple[str, tuple[str, ...]]:
    """Return the campaign key's source column and the tracked targets' source columns.

    Read from the contract at plan time with the sidecar's `tracked` rule: every target except
    the business key, the validity columns, `rows_suppressed` and `source_*`, so the seal's
    chain points move whenever the fold would see a change.
    """
    business_key = {column for column in contract.grain if column != "valid_from"}
    keys = [
        field.source_path for field in contract.fields if field.target_column == _SEAL_KEY_TARGET
    ]
    tracked = tuple(
        field.source_path
        for field in contract.fields
        if field.source_path
        and field.target_column not in business_key | _UNTRACKED_DIMENSION_TARGETS
        and not field.target_column.startswith("source_")
    )
    if len(keys) != 1 or not keys[0] or not tracked:
        raise LandingError(
            f"{contract.contract_id}: no campaign key or tracked source column to seal."
        )
    return keys[0], tracked


def _seal_chain_points(
    rows: tuple[dict[str, str], ...], columns: tuple[str, str, tuple[str, ...]], shape: CatalogShape
) -> tuple[date, ...]:
    """Return `first_day`, every first appearance or tracked change before the tail, then the cut.

    The cut is `last_day - 29`: every bucket from it on gets a tail delivery instead.
    """
    date_column, key_column, tracked = columns
    span = shape.fixture_span
    cut = span.last_day - timedelta(days=_SEAL_TAIL_DELIVERIES)
    if cut <= span.first_day:
        raise LandingError("the fixture span is too short for a dimension seal.")
    previous: dict[str, tuple[str, ...]] = {}
    points = {span.first_day}
    for row in sorted(rows, key=lambda item: item[date_column]):
        values = tuple(row[column] for column in tracked)
        if previous.get(row[key_column]) != values:
            points.add(date.fromisoformat(row[date_column]))
        previous[row[key_column]] = values
    return (*sorted(point for point in points if point < cut), cut)


def _seal_chunks(points: tuple[date, ...], last_day: date) -> tuple[tuple[date, ...], ...]:
    """Chain the points three at a time with an overlap of one, then add the tail."""
    chain = tuple(
        points[index : index + MAX_POPULATED_BUCKETS_PER_DELIVERY]
        for index in range(0, len(points) - 1, MAX_POPULATED_BUCKETS_PER_DELIVERY - 1)
    )
    tail = tuple(
        (last_day - timedelta(days=offset + 1), last_day - timedelta(days=offset))
        for offset in range(_SEAL_TAIL_DELIVERIES - 1, -1, -1)
    )
    return chain + tail


def _dimension_seal_runs(request: _SealRequest) -> tuple[tuple[str, str, dict[str, object]], ...]:
    """Plan the campaign-dimension seal: write-discipline §2.1 rule 3's operator re-land.

    Rows are the generated result's own rows for the selected observation days, unchanged but for
    their AMC window (`_windowed`), and every delivery carries watermark `last_day`. When the
    seal lands in phase 0, no incumbent carries `last_day` yet: the old generation's partitions
    are older and phase A has not run. Phase A's `last_day` run later ties and republishes
    buckets `last_day - 29 .. last_day` from sealed bases, so the history is unchanged (test 11c).
    Offsets
    `(span_days + seal_generation) x 64 + i` sit above every cadence offset, and a re-land
    under `seal_generation + 1` lands at fresh prefixes (ADR-086).
    """
    date_column = AMC_DAILY_DATE_COLUMNS[request.fixture]
    key_column, tracked = _seal_columns(request.contract)
    fieldnames, rows = _amc_rows(request.text)
    if not {date_column, key_column, *tracked} <= set(fieldnames):
        raise LandingError(f"{request.fixture}: the seal's source columns were not delivered.")
    span = request.shape.fixture_span
    observed = {date.fromisoformat(row[date_column]) for row in rows}
    chunks = _seal_chunks(
        _seal_chain_points(rows, (date_column, key_column, tracked), request.shape), span.last_day
    )
    if len(chunks) > MAX_SEAL_DELIVERIES_PER_FIXTURE:
        raise LandingError(
            f"{request.fixture}: the dimension seal needs {len(chunks)} deliveries, above the "
            f"stride envelope of {MAX_SEAL_DELIVERIES_PER_FIXTURE}."
        )
    try:
        retrieved = datetime.fromisoformat(
            str(request.declaration["retrieved_at"]).replace("Z", "+00:00")
        )
    except KeyError, ValueError:
        raise LandingError(f"{request.fixture}: the retrieval instant is invalid.") from None
    generation = request.shape.seal_generation
    runs: list[tuple[str, str, dict[str, object]]] = []
    for index, days in enumerate(chunks):
        if not set(days) <= observed:
            raise LandingError(f"{request.fixture}: a seal day has no observed rows.")
        chosen = {day.isoformat() for day in days}
        body = write_rows(
            fieldnames,
            _windowed([row for row in rows if row[date_column] in chosen], days[0], days[-1]),
        )
        offset = (span.span_days + generation) * _RUN_RETRIEVAL_STRIDE_SECONDS + index
        runs.append(
            (
                f"{request.fixture}#seal-generation={generation}"
                f"#chunk={days[0].isoformat()}..{days[-1].isoformat()}",
                body,
                {
                    **request.declaration,
                    "period_start": days[0].isoformat(),
                    "period_end": days[-1].isoformat(),
                    "source_watermark": span.last_day.isoformat(),
                    "retrieved_at": (retrieved + timedelta(seconds=offset))
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
            )
        )
    return tuple(runs)


HIERARCHY_ROWS_PER_DELIVERY: Final = 238
"""Rows per generated product-hierarchy delivery (ADR-086 amendment, 2026-10-06).

Stage-1 refuses a delivery whose unit needs more than 10 clean attempts, `M(u) + 1` with `M(u)`
the unit's MERGE statements (`projection_feasibility.MAX_ACCEPTED_ATTEMPTS_PER_UNIT`). The
source-aligned hierarchy table has 26 columns; under the then 900-parameter bound a statement
held at most 34 rows, so 238 rows was 7 statements, `M + 1 = 8`, two attempts inside the cap.
It is also 34 whole cycles of the 7 fixture products, so every chunk is a complete snapshot.
On 2026-10-06 the unsplit 463-row delivery was refused live: "needs 14 clean attempts". The
parameter bound was removed on 2026-10-10 (write-discipline §11): packing is now bounded only by
statement envelope bytes, which fits more rows per MERGE, so the chunk stays inside the cap.
"""

CHUNKED_WHOLE_FIXTURES: Final = {
    "generated/vendor-product-hierarchy.jsonl": HIERARCHY_ROWS_PER_DELIVERY
}
"""Whole-landed fixtures split into ordered row chunks; each chunk is still a `whole` delivery."""


def _whole_chunk_runs(
    request: _SourceRunsRequest,
) -> tuple[tuple[str, str, dict[str, object], date | None], ...]:
    """Split a whole JSONL fixture into consecutive row chunks with their own day windows.

    Row `i` belongs to day `period_start + i`, as the generator writes one row per span day, so a
    chunk's window is its rows' days and every chunk gets its own raw prefix. A fixture at or under
    the chunk size stays one delivery, byte-identical to before the split.
    """
    size = CHUNKED_WHOLE_FIXTURES[request.fixture]
    lines = request.text.splitlines(keepends=True)
    if len(lines) <= size:
        return ((request.fixture, request.text, request.declaration, None),)
    try:
        first = date.fromisoformat(str(request.declaration["period_start"]))
        last = date.fromisoformat(str(request.declaration["period_end"]))
    except KeyError, ValueError:
        raise LandingError(f"{request.fixture}: its declared period is invalid.") from None
    if (last - first).days + 1 != len(lines):
        raise LandingError(f"{request.fixture}: its rows do not match its declared days.")
    runs: list[tuple[str, str, dict[str, object], date | None]] = []
    for start in range(0, len(lines), size):
        chunk = lines[start : start + size]
        chunk_first = first + timedelta(days=start)
        chunk_last = chunk_first + timedelta(days=len(chunk) - 1)
        runs.append(
            (
                f"{request.fixture}#chunk={chunk_first.isoformat()}..{chunk_last.isoformat()}",
                "".join(chunk),
                {
                    **request.declaration,
                    "period_start": chunk_first.isoformat(),
                    "period_end": chunk_last.isoformat(),
                },
                None,
            )
        )
    return tuple(runs)


@dataclass(frozen=True, slots=True)
class _SourceRunsRequest:
    """One generated source corpus and the cadence metadata used to split it."""

    fixture: str
    text: str
    declaration: dict[str, object]
    restatement_window_days: int | None
    shape: CatalogShape
    cadence_lag_days: int | None = None
    """The governed pack's `cadenceLagDays`, for a periodic corpus (ADR-110)."""
    phase_a_only: bool = False
    """Plan only periods landing after the backfill cut: the corpus's dataset has no retrieval
    encoding in the pinned replay band, so an older delivery would be admitted as non-historical,
    against the phase rule (V-8). Its older periods wait for an encoding in that band."""


@dataclass(frozen=True, slots=True)
class _VendorDailyDocumentRequest:
    """One Vendor report corpus projected to one source-valid DAY document."""

    source: dict[str, object]
    rows_by_array: dict[str, list[dict[str, object]]]
    arrays: tuple[str, ...]
    run_day: date
    units_adjustments: dict[str, int]


def _source_runs(
    request: _SourceRunsRequest,
) -> tuple[tuple[str, str, dict[str, object], date | None], ...]:
    """Expand generated source corpora into immutable customer-cadence deliveries."""
    fixture = request.fixture
    text = request.text
    declaration = request.declaration
    restatement_window_days = request.restatement_window_days
    if fixture in _VENDOR_DAILY_ARRAYS:
        return _vendor_source_runs(request)
    if fixture in CHUNKED_WHOLE_FIXTURES:
        return _whole_chunk_runs(request)
    if fixture in AMC_PERIODIC_FIXTURES:
        return _periodic_runs(request)
    if fixture in AMC_DAY_DELIVERY_FIXTURES:
        return _day_delivery_runs(request)
    date_column = AMC_DAILY_DATE_COLUMNS.get(fixture)
    if date_column is None:
        return ((fixture, text, declaration, None),)
    if restatement_window_days is None or restatement_window_days < 1:
        raise LandingError(f"{fixture}: no positive contract restatement window was declared.")
    fieldnames, rows = _amc_rows(text)
    if date_column not in fieldnames or not rows:
        raise LandingError(f"{fixture}: no governed AMC cadence date column was delivered.")
    try:
        days = tuple(sorted({date.fromisoformat(row[date_column]) for row in rows}))
        retrieved = datetime.fromisoformat(str(declaration["retrieved_at"]).replace("Z", "+00:00"))
    except KeyError, ValueError:
        raise LandingError(f"{fixture}: cadence dates or retrieval instant are invalid.") from None
    runs: list[tuple[str, str, dict[str, object], date | None]] = []
    for run_index, run_day in enumerate(days):
        first_day = max(days[0], run_day - timedelta(days=restatement_window_days - 1))
        selected = [
            _mature_attribution_row(row, date_column, run_day, request.shape)
            if fixture == "generated/amc-attributed-conversion.csv"
            else row
            for row in rows
            if first_day <= date.fromisoformat(row[date_column]) <= run_day
        ]
        if (
            len({row[date_column] for row in selected})
            > _RUN_RETRIEVAL_STRIDE_SECONDS * MAX_POPULATED_BUCKETS_PER_DELIVERY
        ):
            # Collision-freedom of the strided retrieval instants is load-bearing: a run
            # whose chunk count could reach the stride must fail loudly, never overlap.
            raise LandingError(
                f"{fixture}: a run's populated days exceed the retrieval stride envelope."
            )
        # Write-discipline §2.1 (2026-09-22 amendment) rule 2: a run's trailing window lands
        # as an ordered oldest-first series of chunked deliveries, each spanning at most the
        # owned per-derived-table populated-bucket bound; the generator is the dev landing
        # seam that applies it (dev-seeding §1). Maturation stays as-of the run day.
        populated_days = tuple(sorted({date.fromisoformat(row[date_column]) for row in selected}))
        for chunk_index in range(0, len(populated_days), MAX_POPULATED_BUCKETS_PER_DELIVERY):
            chunk = populated_days[chunk_index : chunk_index + MAX_POPULATED_BUCKETS_PER_DELIVERY]
            chunk_days = frozenset(chunk)
            chunk_rows = [
                row for row in selected if date.fromisoformat(row[date_column]) in chunk_days
            ]
            body = write_rows(fieldnames, _windowed(chunk_rows, chunk[0], chunk[-1]))
            offset = run_index * _RUN_RETRIEVAL_STRIDE_SECONDS + (
                chunk_index // MAX_POPULATED_BUCKETS_PER_DELIVERY
            )
            run_declaration = {
                **declaration,
                "period_start": chunk[0].isoformat(),
                "period_end": chunk[-1].isoformat(),
                "source_watermark": _watermark(fixture, (chunk[0], chunk[-1]), run_day).isoformat(),
                "retrieved_at": (retrieved + timedelta(seconds=offset))
                .isoformat()
                .replace("+00:00", "Z"),
            }
            runs.append(
                (
                    f"{fixture}#daily-run={run_day.isoformat()}#chunk={chunk[0].isoformat()}",
                    body,
                    run_declaration,
                    run_day,
                )
            )
    return tuple(runs)


def _refused_periods(fixture: str, shape: CatalogShape) -> frozenset[tuple[date, date]]:
    """Return the periods a periodic corpus declares the load refuses, which are never landed."""
    if fixture in _FREQUENCY_FIXTURES:
        # Packs 6 and 7: the month whose NULL bucket the load refuses (FREQUENCY_BUCKET_NULL).
        month = shape.exposure_frequency.null_bucket_month
        following = date(month.year + month.month // 12, month.month % 12 + 1, 1)
        return frozenset({(month, following - timedelta(days=1))})
    if fixture == _COHORT_FIXTURE:
        # Pack 14: the vintage whose repeated cohort size differs (REPEATED_TOTAL_MISMATCH).
        vintage = vintage_ending(shape.acquisition_cohorts.refused_vintage)
        return frozenset({(vintage.start, vintage.end)})
    if fixture != "generated/amc-exposure-groups.csv":
        return frozenset()
    week = shape.exposure_groups.currency_disagreement_week
    return frozenset({(week, week + timedelta(days=6))})


_FREQUENCY_FIXTURES: Final = frozenset(
    {
        "generated/amc-exposure-frequency-platform.csv",
        "generated/amc-exposure-frequency-channel.csv",
    }
)
_COHORT_FIXTURE: Final = "generated/amc-acquisition-cohorts.csv"
_VINTAGE_FIXTURES: Final = frozenset({_COHORT_FIXTURE, "generated/amc-acquisition-cohorts-arp.csv"})
"""Packs 14-15: corpora of 13-month cohort vintages (ADR-104)."""


def _holds_a_vintage(shape: CatalogShape) -> bool:
    """Whether the catalog span holds at least one whole 13-month cohort vintage."""
    span = shape.fixture_span
    first = span.first_day
    month = first if first.day == 1 else shift_months(first.replace(day=1), 1)
    return shift_months(month, VINTAGE_MONTHS) - timedelta(days=1) <= span.last_day


_COHORT_GRAIN_COLUMN: Final = "cohort_grain"
_QUARTER: Final = "quarter"
_QUARTERLY: Final = "quarterly"
"""A cohort delivery grouped under calendar-quarter cohorts is packs 14-15's `quarterly` variant."""
_AS_OF_MONTH: Final = "as_of_month"
_BUCKET_SCHEME_COLUMN: Final = "bucket_scheme"
_COARSE: Final = "coarse"
"""A frequency delivery bucketed under the coarse scheme is that pack's declared `coarse`
variant (plan D9), so its registration names the variant and its series is its own."""


def _variant_options(fieldnames: tuple[str, ...], rows: list[dict[str, str]]) -> dict[str, object]:
    """Name a delivery's pack variant from its coarse buckets or calendar-quarter cohorts.

    Packs 6-7's coarse scheme and packs 14-15's quarterly cohorts are declared variants (plan D9).
    """
    for column, variant_value, variant, what in (
        (_BUCKET_SCHEME_COLUMN, _COARSE, _COARSE, "frequency delivery carries bucket schemes"),
        (_COHORT_GRAIN_COLUMN, _QUARTER, _QUARTERLY, "cohort delivery carries cohort grains"),
    ):
        if column not in fieldnames:
            continue
        values = {row[column] for row in rows}
        if len(values) != 1:
            raise LandingError(f"one {what} {sorted(values)}.")
        return {PACK_VARIANT_OPTION: variant} if values == {variant_value} else {}
    return {}


def _vintage_options(grain: PeriodGrain, end: date) -> dict[str, object]:
    """Return a cohort vintage's `as_of_month`, the parameter its registration carries (ADR-104).

    Each vintage is then its own option set, so overlapping vintages never trip the load's
    period-overlap check, exactly as producer-landed vintages don't.
    """
    if grain is not PeriodGrain.CUSTOM:
        return {}
    return {_AS_OF_MONTH: period_parameter_value(PeriodValue.AS_OF_MONTH, (end, end))}


def _periodic_runs(
    request: _SourceRunsRequest,
) -> tuple[tuple[str, str, dict[str, object], date | None], ...]:
    """Split a periodic corpus into one delivery per AMC window, oldest period end first.

    Each delivery's declared period is its window's inclusive days, so the Finalizer's alignment
    and the load's period bucket agree, and it lands on its period end plus the cadence lag,
    never after the corpus's last day. A week and a month ending the same day land a second
    apart, week first.
    """
    fixture = request.fixture
    fieldnames, rows = _amc_rows(request.text)
    if (
        not fieldnames
        and not rows
        and fixture in _VINTAGE_FIXTURES
        and not _holds_a_vintage(request.shape)
    ):
        # The generator writes an empty cohort corpus when the catalog span holds no whole
        # 13-month vintage: nothing to land. Any other empty corpus is refused below.
        return ()
    start_column, end_column = WINDOW_COLUMNS
    if not rows or start_column not in fieldnames:
        raise LandingError(f"{fixture}: a periodic corpus carries no AMC window.")
    lag_days = request.cadence_lag_days
    if lag_days is None:
        raise LandingError(f"{fixture}: a periodic corpus has no governed pack cadence lag.")
    try:
        retrieved = datetime.fromisoformat(
            str(request.declaration["retrieved_at"]).replace("Z", "+00:00")
        )
        windows = sorted(
            {
                (
                    date.fromisoformat(row[start_column][:10]),
                    date.fromisoformat(row[end_column][:10]) - timedelta(days=1),
                )
                for row in rows
            },
            key=lambda window: (window[1], window[0]),
        )
    except KeyError, ValueError:
        raise LandingError(
            f"{fixture}: periodic windows or retrieval instant are invalid."
        ) from None
    first_day = request.shape.fixture_span.first_day
    last_day = request.shape.fixture_span.last_day
    refused = _refused_periods(fixture, request.shape)
    cut = backfill_last_day(request.shape)
    runs: list[tuple[str, str, dict[str, object], date | None]] = []
    for index, (start, end) in enumerate(windows):
        grain = next((item for item in PeriodGrain if is_aligned(item, (start, end))), None)
        if grain is None:
            raise LandingError(f"{fixture}: window {start}..{end} is no declared period.")
        run_day = min(end + timedelta(days=lag_days), last_day)
        if request.phase_a_only and run_day <= cut:
            # Validated above, so a malformed earlier window still fails the plan.
            continue
        if (start, end) in refused:
            # The load refuses this declared delivery (pack 5's currency disagreement): a refused
            # delivery belongs in a test or the induced live probe, never the working bundle.
            continue
        selected = [
            row
            for row in rows
            if row[start_column][:10] == start.isoformat()
            and row[end_column][:10] == (end + timedelta(days=1)).isoformat()
        ]
        body = write_rows(fieldnames, selected)
        offset = (run_day - first_day).days * _RUN_RETRIEVAL_STRIDE_SECONDS + index % (
            _RUN_RETRIEVAL_STRIDE_SECONDS
        )
        declared = request.declaration.get("option_set")
        declared_options = cast("dict[str, object]", declared) if isinstance(declared, dict) else {}
        runs.append(
            (
                f"{fixture}#period={start.isoformat()}..{end.isoformat()}",
                body,
                {
                    **request.declaration,
                    # The grain a producer registration names (`plan_landing`), so the
                    # publication's lens is the one serving rebuilds for that grain.
                    "option_set": {
                        **declared_options,
                        PERIOD_GRAIN_OPTION: grain.value,
                        **_variant_options(tuple(fieldnames), selected),
                        **_vintage_options(grain, end),
                    },
                    "period_start": start.isoformat(),
                    "period_end": end.isoformat(),
                    "source_watermark": run_day.isoformat(),
                    "retrieved_at": (retrieved + timedelta(seconds=offset))
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
                run_day,
            )
        )
    return tuple(runs)


def _day_delivery_runs(
    request: _SourceRunsRequest,
) -> tuple[tuple[str, str, dict[str, object], date | None], ...]:
    """Split a daily distinct-count corpus into one delivery per UTC day, oldest first.

    Each lands on its day plus the cadence lag, never after the corpus's last day. A day holding a
    suppressed row is skipped (`AMC_DAY_DELIVERY_FIXTURES`).
    """
    fixture = request.fixture
    fieldnames, rows = _amc_rows(request.text)
    start_column, end_column = WINDOW_COLUMNS
    if not rows or start_column not in fieldnames:
        raise LandingError(f"{fixture}: a daily corpus carries no AMC window.")
    lag_days = request.cadence_lag_days
    if lag_days is None:
        raise LandingError(f"{fixture}: a daily corpus has no governed pack cadence lag.")
    try:
        retrieved = datetime.fromisoformat(
            str(request.declaration["retrieved_at"]).replace("Z", "+00:00")
        )
        by_day: dict[date, list[dict[str, str]]] = {}
        for row in rows:
            day = date.fromisoformat(row[start_column][:10])
            if date.fromisoformat(row[end_column][:10]) != day + timedelta(days=1):
                raise ValueError(row[end_column])
            by_day.setdefault(day, []).append(row)
    except KeyError, ValueError:
        raise LandingError(f"{fixture}: daily windows or retrieval instant are invalid.") from None
    first_day = request.shape.fixture_span.first_day
    last_day = request.shape.fixture_span.last_day
    runs: list[tuple[str, str, dict[str, object], date | None]] = []
    for day, selected in sorted(by_day.items()):
        if any(row["rows_suppressed"] != "false" for row in selected):
            continue
        run_day = min(day + timedelta(days=lag_days), last_day)
        offset = (run_day - first_day).days * _RUN_RETRIEVAL_STRIDE_SECONDS + (
            day.toordinal() % _RUN_RETRIEVAL_STRIDE_SECONDS
        )
        runs.append(
            (
                f"{fixture}#day={day.isoformat()}",
                write_rows(fieldnames, selected),
                {
                    **request.declaration,
                    "period_start": day.isoformat(),
                    "period_end": day.isoformat(),
                    "source_watermark": run_day.isoformat(),
                    "retrieved_at": (retrieved + timedelta(seconds=offset))
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
                run_day,
            )
        )
    return tuple(runs)


def _vendor_source_runs(
    request: _SourceRunsRequest,
) -> tuple[tuple[str, str, dict[str, object], date | None], ...]:
    """Emit one source-valid DAY report per date, plus delayed sales restatements.

    A restating report is emitted only when it has fallen due by the retrieval date
    (`run_day + 2 <= retrieved`), so the last two days carry only the first report.
    """
    fixture, text, declaration, shape = (
        request.fixture,
        request.text,
        request.declaration,
        request.shape,
    )
    parsed: object = json.loads(text)
    if not isinstance(parsed, dict):
        raise LandingError(f"{fixture}: Vendor report must be an object.")
    document = cast("dict[str, object]", parsed)
    specification = document.get("reportSpecification")
    arrays = _VENDOR_DAILY_ARRAYS[fixture]
    if not isinstance(specification, dict):
        raise LandingError(f"{fixture}: Vendor report specification is missing.")
    array_rows: dict[str, list[dict[str, object]]] = {}
    for array_name in arrays:
        raw = document.get(array_name)
        if not isinstance(raw, list):
            raise LandingError(f"{fixture}: Vendor {array_name} rows are invalid.")
        typed_rows: list[dict[str, object]] = []
        for row in cast("list[object]", raw):
            if not isinstance(row, dict):
                raise LandingError(f"{fixture}: Vendor {array_name} rows are invalid.")
            typed_rows.append(cast("dict[str, object]", row))
        array_rows[array_name] = typed_rows
    try:
        days = tuple(
            sorted(
                {
                    date.fromisoformat(cast("str", row["startDate"]))
                    for rows in array_rows.values()
                    for row in rows
                }
            )
        )
        retrieved = datetime.fromisoformat(str(declaration["retrieved_at"]).replace("Z", "+00:00"))
    except KeyError, TypeError, ValueError:
        raise LandingError(
            f"{fixture}: Vendor row dates or retrieval instant are invalid."
        ) from None
    runs: list[tuple[str, str, dict[str, object], date | None]] = []
    is_sales = fixture == "generated/vendor-sales-by-asin.json"
    adjustments = reported_units_adjustments(shape)
    for day_index, run_day in enumerate(days):
        final = _vendor_daily_document(
            _VendorDailyDocumentRequest(document, array_rows, arrays, run_day, adjustments)
        )
        restated = run_day + timedelta(days=2) <= retrieved.date()
        first_report = _watermark(fixture, (run_day, run_day), run_day)
        if is_sales:
            initial = _vendor_initial_sales(
                final, _InitialReport(shape, restated=restated, adjustments=adjustments)
            )
            variants: tuple[tuple[str, dict[str, object], date], ...] = (
                ("daily-run", initial, first_report),
            ) + (
                (
                    (
                        "restatement",
                        final,
                        _watermark(fixture, (run_day, run_day), run_day + timedelta(days=2)),
                    ),
                )
                if restated
                else ()
            )
        else:
            variants = (("daily-run", final, first_report),)
        for variant_index, (kind, run_document, watermark) in enumerate(variants):
            spec = cast("dict[str, object]", run_document["reportSpecification"])
            spec["lastUpdatedDate"] = watermark.isoformat()
            run_retrieved = retrieved + timedelta(seconds=day_index * 2 + variant_index)
            run_declaration = {
                **declaration,
                "period_start": run_day.isoformat(),
                "period_end": run_day.isoformat(),
                "source_watermark": watermark.isoformat(),
                "retrieved_at": run_retrieved.isoformat().replace("+00:00", "Z"),
            }
            runs.append(
                (
                    f"{fixture}#{kind}={run_day.isoformat()}",
                    json.dumps(run_document, indent=2) + "\n",
                    run_declaration,
                    # A restating report's run day is the day it falls due (run day + 2), the
                    # day an increment selects it; never its retired-span watermark.
                    run_day if variant_index == 0 else run_day + timedelta(days=2),
                )
            )
    return tuple(runs)


def _vendor_daily_document(
    request: _VendorDailyDocumentRequest,
) -> dict[str, object]:
    """Project one complete source report document for one DAY period."""
    source = request.source
    rows_by_array = request.rows_by_array
    arrays = request.arrays
    run_day = request.run_day
    result = cast("dict[str, object]", json.loads(json.dumps(source)))
    specification = cast("dict[str, object]", result["reportSpecification"])
    specification["dataStartTime"] = run_day.isoformat()
    specification["dataEndTime"] = run_day.isoformat()
    for array_name in arrays:
        result[array_name] = [
            json.loads(json.dumps(row))
            for row in rows_by_array[array_name]
            if row.get("startDate") == run_day.isoformat()
        ]
    return reconcile_vendor_aggregates(result, request.units_adjustments)


@dataclass(frozen=True, slots=True)
class _InitialReport:
    """How one day's first Vendor sales report relates to its final report (design §3.3)."""

    shape: CatalogShape
    restated: bool
    adjustments: dict[str, int]


def _vendor_initial_sales(document: dict[str, object], report: _InitialReport) -> dict[str, object]:
    """Return the first daily sales report, net of only the returns it carries.

    A restated day's first report carries `floor(R x first_report_returns_share)` of the final
    returns R, and its restating report later carries R. An unrestated day (the last two days)
    carries R itself under `unrestated_returns: final`, so the first report equals the final row.
    Each money field is (units - returns carried) times the final row's net per-unit amount, so
    net revenue per unit is the same on both reports.
    """
    initial = cast("dict[str, object]", json.loads(json.dumps(document)))
    retail = report.shape.retail
    partial = report.restated or retail.unrestated_returns == "first_report"
    rows = cast("list[dict[str, object]]", initial["salesByAsin"])
    for row in rows:
        final_returns = int(cast("int", row["customerReturns"]))
        if not partial:
            continue
        carried = int(
            (final_returns * retail.first_report_returns_share).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        bases = (
            int(cast("int", row["shippedUnits"])),
            int(cast("int", row["shippedUnits"])),
            int(cast("int", row["orderedUnits"])),
        )
        for money_name, units in zip(_VENDOR_MONEY_FIELDS, bases, strict=True):
            amount = cast("dict[str, object]", row[money_name])
            per_unit = Decimal(str(amount["amount"])) / (units - final_returns)
            amount["amount"] = float(
                ((units - carried) * per_unit).quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)
            )
        row["customerReturns"] = carried
    return reconcile_vendor_aggregates(initial, report.adjustments)


def _mature_attribution_row(  # noqa: PLR0913, PLR0917 -- the row, its run and the shape
    row: dict[str, str], date_column: str, run_day: date, shape: CatalogShape
) -> dict[str, str]:
    """Simulate late attribution with the declared maturity of the last two event days."""
    event_day = date.fromisoformat(row[date_column])
    age = (run_day - event_day).days
    maturity = shape.attribution_maturity
    factor = maturity.age_0 if age == 0 else maturity.age_1 if age == 1 else Decimal(1)
    matured = dict(row)
    for column in _ATTRIBUTION_MATURING_COLUMNS & row.keys():
        value = Decimal(row[column])
        adjusted = value * factor
        matured[column] = (
            str(adjusted.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN))
            if "." in row[column]
            else str(int(adjusted.to_integral_value(rounding=ROUND_HALF_EVEN)))
        )
    return matured


def _declaration(path: pathlib.Path) -> dict[str, object]:
    """Read a landing declaration as a mapping, leaving every value for the builder to narrow."""
    parsed: object = json.loads(path.read_text())
    if not isinstance(parsed, dict):
        raise LandingError(f"{path.name} must be a JSON object.")
    return cast("dict[str, object]", parsed)


def _refuse_collisions(planned: list[LandedDelivery]) -> None:
    """Refuse two fixtures that would land at one prefix.

    Two deliveries at one prefix would have the second silently overwrite the first, which is
    the exact thing the landing contract forbids a producer from doing. It is worth checking
    here because the restatement pair is *designed* to differ only in provenance, so a
    copy-paste in a declaration produces it.
    """
    seen: dict[str, str] = {}
    for delivery in planned:
        prefix = delivery.manifest.prefix
        if prefix in seen:
            raise LandingError(
                f"{delivery.fixture} and {seen[prefix]} both land at {prefix!r}, so one would "
                f"overwrite the other. Their declarations differ in nothing that reaches the "
                f"prefix — check {MANIFEST_NAME.split('.')[0]} retrieved_at and option_set."
            )
        seen[prefix] = delivery.fixture


# --- The historical predicate's replay band and the probe-input producer (V-8, V-24) --------

PROBE_SLOT_COUNT: Final = 64
"""Reserved probe retrieval slots: 64 strides of 64 one-second instants above every backfill."""


def manifest_facts(manifest: Manifest) -> ManifestFacts:
    """Project the manifest authority the historical predicate reads."""
    return ManifestFacts(
        source_system=manifest.source_system,
        source_dataset=manifest.source_dataset,
        period_end=manifest.period_end,
        retrieved_at=manifest.retrieved_at,
    )


def _replay_base(fixtures: pathlib.Path) -> datetime:
    """Return the one retrieval instant every generated fixture declaration carries."""
    bases = {
        datetime.fromisoformat(
            str(
                _declaration(fixtures / pathlib.Path(name).with_suffix(".manifest.json"))[
                    "retrieved_at"
                ]
            ).replace("Z", "+00:00")
        )
        for name in LANDABLE
        if name.startswith("generated/")
    }
    if len(bases) != 1:
        raise LandingError("the generated fixtures do not share one replay base instant.")
    return bases.pop()


def _encodings(fixtures: pathlib.Path) -> dict[tuple[str, str], RetrievalEncoding]:
    """Map each daily-run dataset to the retrieval encoding its runs use."""
    found: dict[tuple[str, str], RetrievalEncoding] = {}
    families = [(name, RetrievalEncoding.STRIDED) for name in AMC_DAILY_DATE_COLUMNS] + [
        (name, RetrievalEncoding.PAIRED) for name in _VENDOR_DAILY_ARRAYS
    ]
    for name, encoding in families:
        declaration = _declaration(fixtures / pathlib.Path(name).with_suffix(".manifest.json"))
        key = (str(declaration["source_system"]), str(declaration["source_dataset"]))
        if found.setdefault(key, encoding) is not encoding:
            raise LandingError(f"{key} would carry two retrieval encodings.")
    return found


def historical_policy_for(
    root: pathlib.Path, planned: tuple[LandedDelivery, ...]
) -> HistoricalPolicy:
    """Compute the replay band from the planner's own output, never from typed-in values.

    `planned` must include the phase-B backfill. The probe slot base is the first stride above
    the largest backfill retrieval offset, and every planned retrieval must sit below it.
    """
    shape = load_synthetic_entity_catalog(root).shape
    fixtures = root / "tests" / "fixtures"
    base = _replay_base(fixtures)
    backfill = [item for item in planned if item.delivery_class is DeliveryClass.BACKFILL]
    if not backfill:
        raise LandingError("the replay band needs the plan's phase-B backfill deliveries.")
    stride = _RUN_RETRIEVAL_STRIDE_SECONDS

    def offset(item: LandedDelivery) -> int:
        return int((item.manifest.retrieved_at - base) // timedelta(seconds=1))

    probe_base = max(offset(item) for item in backfill) // stride + 1
    policy = HistoricalPolicy(
        replay_base=base,
        first_day=shape.fixture_span.first_day,
        cut=backfill_last_day(shape),
        stride_seconds=stride,
        seal_slot_base=shape.fixture_span.span_days,
        backfill_slot_base=shape.fixture_span.span_days + _BACKFILL_SLOT_BASE,
        probe_slot_base=probe_base,
        probe_slot_count=PROBE_SLOT_COUNT,
        encodings=_encodings(fixtures),
    )
    if any(offset(item) >= policy.probe_first_offset for item in planned):
        raise LandingError("a planned retrieval reaches the reserved probe slots.")
    return policy


def plan_with_backfill(root: pathlib.Path) -> tuple[tuple[LandedDelivery, ...], HistoricalPolicy]:
    """Plan every class, phase-B backfill included, and the replay band computed from it."""
    planned = plan_landings(root, PlanSelection(frozenset(DeliveryClass)))
    return planned, historical_policy_for(root, planned)


def window_prefix(manifest: Manifest) -> str:
    """Return the conflict unit's prefix: the manifest prefix without its `retrieved=` segment."""
    head, separator, _ = manifest.prefix.rstrip("/").rpartition("/retrieved=")
    if not separator:
        raise LandingError("a manifest prefix carries no retrieved= segment.")
    return f"{head}/"


def _written_cells(delivery: LandedDelivery) -> frozenset[tuple[str, date]]:
    """Every (table, date) one delivery writes: its governed contract, or its dataset."""
    family = delivery.fixture.split("#", 1)[0]
    manifest = delivery.manifest
    table = AMC_FIXTURE_CONTRACTS.get(family) or (
        f"{manifest.source_system}/{manifest.source_dataset}/{manifest.marketplace}"
    )
    days = (manifest.period_end - manifest.period_start).days + 1
    return frozenset(
        (table, manifest.period_start + timedelta(days=offset)) for offset in range(days)
    )


def restating_deliveries(
    planned: tuple[LandedDelivery, ...], original: LandedDelivery
) -> tuple[LandedDelivery, ...]:
    """Every other planned delivery writing a (table, date) of `original` with a newer retrieval.

    The strong form of `test_probe_delivery_unrestated` (design, "Probe inputs", residue): a probe
    copy is harmless only if this is empty, phase-B backfill included. The same immutable key
    planned twice (a Vendor backfill reuses its day's last report) is the original, not another.
    """
    cells = _written_cells(original)
    key = original.manifest.manifest_key
    newer = original.manifest.retrieved_at
    return tuple(
        item
        for item in planned
        if item.manifest.manifest_key != key
        and item.manifest.retrieved_at > newer
        and not cells.isdisjoint(_written_cells(item))
    )


@dataclass(frozen=True, slots=True)
class ProbeRetrievalRequest:
    """One named delivery to copy, and the retrieval instants its window already holds."""

    name: str
    taken: frozenset[datetime]
    """Every `retrieved=` instant already present under the window prefix."""


@dataclass(frozen=True, slots=True)
class ProbeRetrieval:
    """The original phase-A delivery and its probe copy at the next free probe instant."""

    original: LandedDelivery
    probe: LandedDelivery
    policy: HistoricalPolicy
    window: str


def named_cadence_delivery(
    planned: tuple[LandedDelivery, ...], policy: HistoricalPolicy, name: str
) -> LandedDelivery:
    """Return exactly one named, non-historical `cadence` delivery, or refuse.

    Refuses a name that is not exactly one planned key, a delivery that is not `cadence`, and
    one the historical predicate classifies historical (`FULL_HISTORY_NOT_ENABLED`).
    """
    named = [item for item in planned if item.fixture == name]
    keys = {item.manifest.manifest_key for item in named}
    if len(keys) != 1:
        raise LandingError(
            f"{name!r} names {len(keys)} planned deliveries; exactly one is required."
        )
    # A Vendor backfill delivery reuses its day's last report, at the same key and name.
    cadence = [item for item in named if item.delivery_class is DeliveryClass.CADENCE]
    if len(cadence) != 1:
        raise LandingError("a named delivery must be a cadence delivery.")
    (delivery,) = cadence
    if is_historical(manifest_facts(delivery.manifest), policy):
        raise LandingError("FULL_HISTORY_NOT_ENABLED: the named delivery is historical.")
    return delivery


def plan_probe_retrieval(
    planned: tuple[LandedDelivery, ...],
    policy: HistoricalPolicy,
    request: ProbeRetrievalRequest,
) -> ProbeRetrieval:
    """Copy one named phase-A cadence delivery to a newer retrieval of the same window.

    Refuses, before any key is produced, a name that is not exactly one planned delivery, a
    delivery that is not `cadence`, and one the historical predicate classifies historical
    (`FULL_HISTORY_NOT_ENABLED`). The data bytes are unchanged; only `retrieved_at` moves, to the
    first free reserved probe instant, so the result is a new key, never a rewrite.
    """
    original = named_cadence_delivery(planned, policy, request.name)
    window = window_prefix(original.manifest)
    free = [instant for instant in probe_slot_instants(policy) if instant not in request.taken]
    if not free:
        raise LandingError("every reserved probe retrieval slot of this window is taken.")
    manifest = replace(original.manifest, retrieved_at=free[0])
    if window_prefix(manifest) != window or manifest.retrieved_at <= original.manifest.retrieved_at:
        raise LandingError("a probe retrieval must be a newer retrieval of the same window.")
    if is_historical(manifest_facts(manifest), policy):
        raise LandingError("FULL_HISTORY_NOT_ENABLED: a probe slot classified historical.")
    if manifest.prefix in {item.manifest.prefix for item in planned}:
        raise LandingError("the probe retrieval would land at a planned prefix.")
    (data,) = original.data
    name = data.key.removeprefix(original.manifest.prefix)
    probe = LandedDelivery(
        fixture=f"{request.name}#probe-retrieval={manifest.retrieved_at.strftime('%Y%m%dT%H%M%SZ')}",
        manifest=manifest,
        data=(PlannedObject(key=f"{manifest.prefix}{name}", body=data.body),),
        marker=PlannedObject(key=manifest.manifest_key, body=write_manifest(manifest).encode()),
        delivery_class=DeliveryClass.CADENCE,
        run_day=original.run_day,
    )
    return ProbeRetrieval(original=original, probe=probe, policy=policy, window=window)
