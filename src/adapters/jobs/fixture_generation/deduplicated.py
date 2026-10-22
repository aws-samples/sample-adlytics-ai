# SPDX-License-Identifier: MIT-0
"""Lay the deduplicated-attribution fixture out per day x model x ad product (dev-seeding §2).

`amc-deduplicated-temporal-attribution` credits each matched purchase once under every registered
model from one ranked set (`contracts/amc/query-packs/amc-deduplicated-temporal-attribution.sql`),
so every day carries every model, each model's day total is the same, and linear credit reaches
every ad product a purchaser touched. The generic row plan can't express that: the contract
carries no campaign identity, so it yields one row per day, and the model and ad product rotated
by row (the defect the 2026-10-22 Scorecard investigation found).

No value is random. A day's totals are the attributed-conversion corpus's purchases and
halo-inclusive sales, summed over its ad products, so the deduplicated revenue never exceeds the
channels' halo-inclusive attributed revenue and a coverage-gap campaign-day contributes nothing,
as on every other contract. Each model splits those totals across the ad products with purchases
that day, in proportion to each one's own measure times the model's declared weight
(`shape.amc.deduplicated_attribution`). First and last touch credit each purchase whole, so
their purchase credit is split in whole units; revenue and linear credit at four places. The
rounding remainder goes to the largest share, so every model's day total is exact.
"""

from __future__ import annotations

import decimal
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from adapters.jobs.fixture_generation.shapes import Fixture, FixtureError
from adapters.jobs.fixture_generation.values import FOUR_PLACES, quantize

if TYPE_CHECKING:  # pragma: no cover — typing only
    from adapters.jobs.fixture_generation.shape import DeduplicatedAttributionShape
    from domain.data_contracts.contract import DataContract

__all__ = [
    "DEDUPLICATED_BOUND",
    "DEDUPLICATED_CONTRACT",
    "DeduplicatedBound",
    "DeduplicatedInputs",
    "build_deduplicated",
]

DEDUPLICATED_CONTRACT = "amc-deduplicated-temporal-attribution"


@dataclass(frozen=True, slots=True)
class DeduplicatedBound:
    """Which corpus a deduplicated day splits, and which of its columns each measure sums.

    `pairs` maps each deduplicated measure to the corpus column whose day total it splits.
    """

    corpus_contract: str
    corpus_date_column: str
    corpus_channel_column: str
    corpus_group_column: str
    date_column: str
    channel_column: str
    group_column: str
    model_column: str
    pairs: tuple[tuple[str, str], ...]


DEDUPLICATED_BOUND = DeduplicatedBound(
    corpus_contract="amc-attributed-conversion",
    corpus_date_column="conversion_event_date_utc",
    corpus_channel_column="ad_product_type",
    corpus_group_column="channel_group_key",
    date_column="event_date_utc",
    channel_column="ad_product_type",
    group_column="channel_group_key",
    model_column="attribution_model_key",
    pairs=(
        ("matched_purchase_credit", "purchases"),
        ("revenue_attributed_deduplicated_incl_halo", "total_product_sales"),
    ),
)
"""The first pair decides which ad products a day credits: those with attributed purchases."""
_WHOLE = Decimal(1)
"""A whole-credit model's purchase credit step: each purchase goes whole to one touch."""


@dataclass(frozen=True, slots=True)
class DeduplicatedInputs:
    """What the deduplicated fixture is laid out from."""

    contract: DataContract
    corpus: tuple[Mapping[str, Any], ...]
    """The attributed-conversion corpus's generated rows (`DEDUPLICATED_BOUND.corpus_contract`)."""
    shape: DeduplicatedAttributionShape


def _domain(contract: DataContract, target: str) -> tuple[str, ...]:
    for mapping in contract.fields:
        if mapping.target_column == target and mapping.value_domain:
            return tuple(mapping.value_domain)
    raise FixtureError(f"{contract.contract_id}: {target} declares no value domain.")


def _day_measures(
    corpus: tuple[Mapping[str, Any], ...],
) -> tuple[dict[str, dict[str, list[Decimal]]], dict[str, str]]:
    """Sum each pair's corpus column per day and ad product; record each one's channel group."""
    bound = DEDUPLICATED_BOUND
    days: dict[str, dict[str, list[Decimal]]] = {}
    groups: dict[str, str] = {}
    for row in corpus:
        channel = str(row[bound.corpus_channel_column])
        group = str(row[bound.corpus_group_column])
        if groups.setdefault(channel, group) != group:
            raise FixtureError(f"{bound.corpus_contract}: {channel} names two channel groups.")
        totals = days.setdefault(str(row[bound.corpus_date_column]), {}).setdefault(
            channel, [Decimal(0)] * len(bound.pairs)
        )
        for index, (_, source) in enumerate(bound.pairs):
            totals[index] += Decimal(str(row[source]))
    return days, groups


def split(
    measures: tuple[Decimal, ...], weights: tuple[Decimal, ...], step: Decimal = FOUR_PLACES
) -> tuple[Decimal, ...]:
    """Split the measures' total in proportion to measure x weight, exact at `step`.

    The rounding remainder goes to the largest share, so the shares sum to the total exactly.
    Shares are written at four places whatever the step.
    """
    with decimal.localcontext(decimal.Context(prec=28, rounding=decimal.ROUND_HALF_EVEN)):
        total = sum(measures, Decimal(0))
        weighted = tuple(
            measure * weight for measure, weight in zip(measures, weights, strict=True)
        )
        denominator = sum(weighted, Decimal(0))
        shares = [
            (total * item / denominator).quantize(step, rounding=decimal.ROUND_HALF_EVEN)
            for item in weighted
        ]
        largest = shares.index(max(shares))
        shares[largest] += total - sum(shares, Decimal(0))
        return tuple(quantize(share) for share in shares)


def build_deduplicated(fixture: Fixture, inputs: DeduplicatedInputs) -> Fixture:
    """Expand each generated day into one row per registered model and credited ad product."""
    bound = DEDUPLICATED_BOUND
    contract = inputs.contract
    models = _domain(contract, bound.model_column)
    order = _domain(contract, "channel_key")
    weights = inputs.shape.channel_weights
    whole = inputs.shape.whole_credit_models
    if set(weights) != set(models):
        raise FixtureError(
            f"{contract.contract_id}: the declared channel weights name {sorted(weights)}, "
            f"not the contract's models {sorted(models)}."
        )
    days, groups = _day_measures(inputs.corpus)
    unknown = sorted(set(groups) - set(order))
    if unknown:
        raise FixtureError(f"{contract.contract_id}: undeclared ad products {unknown}.")
    unweighted = sorted(set(groups) - set(weights[models[0]]))
    if unweighted:
        raise FixtureError(f"{contract.contract_id}: no declared weight for {unweighted}.")
    rows: list[dict[str, Any]] = []
    for template in fixture.rows:
        measures = days.get(str(template[bound.date_column]), {})
        channels = tuple(
            channel for channel in order if channel in measures and measures[channel][0] > 0
        )
        if not channels:
            continue
        for model in models:
            factors = tuple(weights[model][channel] for channel in channels)
            columns = tuple(
                split(
                    tuple(measures[channel][index] for channel in channels),
                    factors,
                    _WHOLE if index == 0 and model in whole else FOUR_PLACES,
                )
                for index in range(len(bound.pairs))
            )
            if any(share <= 0 for share in columns[0]):
                # A real query emits no row for an ad product a model never credits, and its
                # revenue would move with it; the declared weights must not imply that here.
                raise FixtureError(
                    f"{contract.contract_id}: {model} credits an ad product with purchases "
                    f"nothing on {template[bound.date_column]}; revise the declared weights."
                )
            for position, channel in enumerate(channels):
                rows.append(
                    {
                        **template,
                        bound.channel_column: channel,
                        bound.group_column: groups[channel],
                        bound.model_column: model,
                        **{
                            target: str(columns[index][position])
                            for index, (target, _) in enumerate(bound.pairs)
                        },
                    }
                )
    return replace(fixture, rows=tuple(rows))
