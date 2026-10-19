# SPDX-License-Identifier: MIT-0
"""Resolve a pack's governed query parameters from the commerce identity bridge (ADR-110 point 2).

The adapter loads `contracts/shared/commerce-identities.yaml`; this module only reads the loaded
bridge, so the producer planner and the Finalizer resolve a parameter the same way and neither
reads a file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from domain.producer.models import GovernedQueryPack, PeriodValue
from domain.semantics.commerce_identity import CommerceIdentityBridge
from domain.semantics.identity import MarketplaceKey


class ParameterResolutionError(ValueError):
    """A declared parameter has no single governed value for this marketplace and period."""


@dataclass(frozen=True, slots=True)
class CommerceIdentityRequest:
    """One pack's parameters, for one registration's marketplace token and inclusive period."""

    pack: GovernedQueryPack
    marketplace: str
    period: tuple[date, date]


def _canonical_marketplace(bridge: CommerceIdentityBridge, token: str, on: date) -> MarketplaceKey:
    """Resolve the registration's bare marketplace token, as the load resolves a manifest's.

    Query-parameter namespaces are left out (`bare_value_marketplace_mappings`), so a token of
    `1` never resolves through the ASI segment marketplace alias.
    """
    targets = {
        mapping.target
        for mapping in bridge.bare_value_marketplace_mappings()
        if mapping.source.value == token and mapping.applies(mapping.source, on=on)
    }
    if len(targets) != 1:
        raise ParameterResolutionError(
            f"marketplace {token!r} has no single governed canonical marketplace on {on}."
        )
    return next(iter(targets))


def _alias(bridge: CommerceIdentityBridge, target: tuple[MarketplaceKey, str], on: date) -> str:
    marketplace, namespace = target
    values = {
        mapping.source.value
        for mapping in bridge.marketplace_mappings
        if mapping.source.namespace == namespace
        and mapping.target == marketplace
        and mapping.applies(mapping.source, on=on)
    }
    if len(values) != 1:
        found = "no value" if not values else f"{len(values)} values"
        raise ParameterResolutionError(
            f"{namespace} has {found} pinned for {marketplace} on {on}; the parameter can't be "
            "filled."
        )
    return next(iter(values))


def resolve_commerce_identities(
    bridge: CommerceIdentityBridge, request: CommerceIdentityRequest
) -> dict[str, str]:
    """Map each namespace the pack's parameters name to its one value for the marketplace.

    Resolved on the first and the last day of the period, and refused if they differ: a mapping
    that changes mid-period would leave the delivery with two values for one parameter.
    """
    namespaces = sorted(
        {
            parameter.commerce_identity
            for parameter in request.pack.input_parameters
            if parameter.commerce_identity is not None
        }
    )
    if not namespaces:
        return {}
    start, end = request.period
    resolved: dict[str, str] = {}
    for namespace in namespaces:
        values = {
            _alias(
                bridge, (_canonical_marketplace(bridge, request.marketplace, day), namespace), day
            )
            for day in (start, end)
        }
        if len(values) != 1:
            raise ParameterResolutionError(
                f"{namespace} changes value between {start} and {end}; split the period."
            )
        resolved[namespace] = next(iter(values))
    return resolved


def period_parameter_value(value: PeriodValue, period: tuple[date, date]) -> str:
    """Render a period-derived parameter for one registration's inclusive period.

    `as_of_month` is the first day of the period's last month at midnight, `YYYY-MM-01 00:00:00`:
    the text pack 14 casts with `CAST(CUSTOM_PARAMETER('as_of_month') AS TIMESTAMP)`, a form the
    2026-10-09 probes compiled and evaluated (`validation/RESULTS.md`).

    `snapshot_start` and `snapshot_end` are the day before and the day after the month's first
    Thursday, at midnight UTC: Amazon takes the monthly ASI snapshot that Thursday, so the 2-day
    range `[start, end)` holds exactly one snapshot (design §5.5, review DS-15). Packs 10-11 cast
    them with the same compiled `CAST(... AS TIMESTAMP)` form.
    """
    _start, end = period
    match value:
        case PeriodValue.AS_OF_MONTH:
            return f"{end.replace(day=1).isoformat()} 00:00:00"
        case PeriodValue.SNAPSHOT_START:
            return f"{(first_thursday(end) - timedelta(days=1)).isoformat()} 00:00:00"
        case PeriodValue.SNAPSHOT_END:
            return f"{(first_thursday(end) + timedelta(days=1)).isoformat()} 00:00:00"


_THURSDAY = 3
"""`date.weekday()` of a Thursday."""


def first_thursday(day: date) -> date:
    """Return the first Thursday of `day`'s calendar month: the ASI snapshot date (design §5.5)."""
    first = day.replace(day=1)
    return first + timedelta(days=(_THURSDAY - first.weekday()) % 7)


def resolve_period_parameters(pack: GovernedQueryPack, period: tuple[date, date]) -> dict[str, str]:
    """Map each period-derived parameter's name to its value for the inclusive period."""
    return {
        parameter.name: period_parameter_value(parameter.period_value, period)
        for parameter in pack.input_parameters
        if parameter.period_value is not None
    }
