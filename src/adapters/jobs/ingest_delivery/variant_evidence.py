# SPDX-License-Identifier: MIT-0
"""Read a series' cell-loss evidence before a delivery's period for the variant check (D10).

A pack declaring an automatic variant (`degrade_next_period`, packs 6-7 and 14-15) switches by
period: the load reads the CURRENT publications of the same contract, marketplace and period grain
before the delivery's period, under each automatic variant's lens and the base pack's, and hands
`domain.producer.variants.select_pack_variant` what it found. Any earlier period under an
automatic variant keeps that variant due, however many periods lie between (sticky); otherwise the
latest earlier base-pack period's grade decides, so a gap after a degraded period doesn't reset the
switch either. The read is the publication-state store the sidecar already reads before every
commit, so no new store and no new permission; it lists one scope per lens, a few dozen periods
for a weekly pack's year. A state that can't be read is `UNESTABLISHED`, and the load accepts
nothing. A pack with only manual variants (pack 5's touch-only) reads nothing: a manual variant is
accepted only when the registration names it, whatever came before.

A contract declaring `variant_follows` reads the named contract's series instead of its own: pack
15 follows pack 14's cohort grain for the same marketplace, so its own loss never switches it and
the ARP rate's two operands share one grain. Cohort vintages overlap, so "before the delivery's
period" means a period ending before this one ends (for calendar grains, the same periods as
ending before it starts).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from adapters.outbound.publication_selection import (
    PublicationScopeRequest,
    publication_scopes_for,
)
from domain.producer.models import PERIOD_GRAIN_OPTION, VariantTrigger
from domain.producer.variants import (
    UNESTABLISHED,
    PreviousPeriodEvidence,
    previous_period_evidence,
)
from domain.semantics.time import TimeGrain
from domain.shopper_aggregates.loss import cell_loss_rule_id

if TYPE_CHECKING:  # pragma: no cover
    from domain.curation.ingestion import DiscardPolicy
    from domain.curation.landing import Manifest
    from domain.data_contracts.contract import DataContract
    from domain.semantics.commerce_identity import CommerceIdentityBridge
    from domain.semantics.publication import SuccessfulPublication
    from domain.semantics.source_pack import SourcePack
    from ports.outbound.semantic_state import CurrentPublicationPort

__all__ = ["VariantEvidenceRequest", "read_variant_evidence"]


@dataclass(frozen=True, slots=True)
class VariantEvidenceRequest:
    """One delivery's manifest and the authorities that read its previous period."""

    manifest: Manifest
    contracts: tuple[DataContract, ...]
    discard_policies: tuple[DiscardPolicy, ...]
    packs: tuple[SourcePack, ...]
    bridge: CommerceIdentityBridge
    publications: CurrentPublicationPort
    governed: tuple[DataContract, ...] = ()
    """Every governed contract, so a `variant_follows` contract's series can be found."""


def _grain(manifest: Manifest) -> TimeGrain | None:
    try:
        return TimeGrain(str(manifest.option_set.get(PERIOD_GRAIN_OPTION)))
    except ValueError:  # an unknown or missing grain
        return None


@dataclass(frozen=True, slots=True)
class _Lookup:
    """One contract's source pack and the series (base pack or variant) to read."""

    contract: DataContract
    pack: SourcePack
    variant: str | None


def _earlier(
    request: VariantEvidenceRequest, lookup: _Lookup, grain: TimeGrain
) -> tuple[SuccessfulPublication, ...]:
    """Every CURRENT publication of one series whose period ends before the delivery's starts.

    Raises ValueError when the series' scope can't be rebuilt from the delivery.
    """
    scopes = publication_scopes_for(
        PublicationScopeRequest(
            pack=lookup.pack,
            contract=lookup.contract,
            bridge=request.bridge,
            filters={"marketplace_key": request.manifest.marketplace},
            period_grain=grain,
            variant=lookup.variant,
        )
    )
    end = request.manifest.period_end
    return tuple(
        publication
        for scope in scopes
        for publication in request.publications.current_for_scope(scope)
        if publication.partition.bucket.grain is grain
        and publication.partition.bucket.window.end < end
    )


def _state(
    request: VariantEvidenceRequest, series: _Lookup, automatic: tuple[str, ...]
) -> PreviousPeriodEvidence | None:
    """Return the series' switch state before the delivery, None when nothing came before."""
    contract, pack = series.contract, series.pack
    grain = _grain(request.manifest)
    if grain is None:
        return UNESTABLISHED
    rule = cell_loss_rule_id(contract.contract_id)
    try:
        for variant in automatic:
            if _earlier(request, _Lookup(contract, pack, variant), grain):
                # Sticky: the switch was made, so it holds, whatever the gap since.
                return PreviousPeriodEvidence(variant=variant, degraded=False)
        base = _earlier(request, _Lookup(contract, pack, None), grain)
    except ValueError:
        return UNESTABLISHED
    if not base:
        return None
    latest = max(base, key=lambda item: item.partition.bucket.window.start)
    return previous_period_evidence(latest.evidence, rule, None)


def _series_contract(
    request: VariantEvidenceRequest, contract: DataContract
) -> DataContract | None:
    """Return the contract whose series decides `contract`'s variant: itself or the followed one.

    None when a followed contract isn't governed, so the state can't be read and nothing loads.
    """
    if contract.variant_follows is None:
        return contract
    return next(
        (item for item in request.governed if item.contract_id == contract.variant_follows), None
    )


def read_variant_evidence(request: VariantEvidenceRequest) -> dict[str, PreviousPeriodEvidence]:
    """Return the switch state per contract whose pack declares an automatic variant.

    An automatic variant's own series is read first, so a switch already made stays made (sticky,
    plan D10); otherwise the latest earlier base-pack period's. A contract with no earlier
    publication in either is absent, and no automatic variant is then due; one whose state can't be
    read is `UNESTABLISHED`.
    """
    governance = request.manifest.governance
    if governance is None:
        return {}
    found: dict[str, PreviousPeriodEvidence] = {}
    for contract in request.contracts:
        policy = next(
            (
                item
                for item in request.discard_policies
                if item.query_pack_id == governance.query_pack_id
                and item.contract_id == contract.contract_id
            ),
            None,
        )
        if policy is None:
            continue
        automatic = tuple(
            item.variant
            for item in policy.variants
            if item.trigger is VariantTrigger.DEGRADE_NEXT_PERIOD
        )
        if not automatic:
            continue
        series = _series_contract(request, contract)
        pack = (
            None
            if series is None
            else next(
                (item for item in request.packs if item.contract_id == series.contract_id), None
            )
        )
        state = (
            UNESTABLISHED
            if series is None or pack is None
            else _state(request, _Lookup(series, pack, None), automatic)
        )
        if state is not None:
            found[contract.contract_id] = state
    return found
