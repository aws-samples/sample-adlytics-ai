# SPDX-License-Identifier: MIT-0
"""An in-memory CURRENT reader over the committed packs, contracts and bridge."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.publication_selection import PublicationScopeRequest, publication_scopes_for
from adapters.outbound.source_pack import load_source_packs
from domain.data_contracts.contract import DataContract
from domain.semantics.commerce_identity import CommerceIdentityBridge
from domain.semantics.publication import (
    AttemptId,
    CanonicalPublisherKey,
    PartitionIdentity,
    PublicationId,
    PublicationScope,
    SourcePublicationRef,
    SuccessfulPublication,
)
from domain.semantics.revision import RevisionSchemeKey, SourceRevision
from domain.semantics.source_pack import SourcePack
from domain.semantics.time import MeasurementBucket, TimeGrain
from domain.semantics.validation import LegacyPublicationRules, PublicationEvidence
from domain.shared_kernel.values import DateRange
from ports.outbound.semantic_state import ScopeHorizon

ROOT = Path(__file__).resolve().parents[2]
PUBLISHED = datetime(2026, 10, 10, 12, tzinfo=UTC)
SALES_FILTERS = {
    "marketplace_key": "amazon_us",
    "distributor_view": "MANUFACTURING",
    "selling_program": "RETAIL",
}


@dataclass(frozen=True)
class Declarations:
    """The committed contracts, packs and bridge."""

    contracts: tuple[DataContract, ...]
    packs: tuple[SourcePack, ...]
    bridge: CommerceIdentityBridge

    def scope(self, contract_id: str, filters: dict[str, str]) -> PublicationScope:
        """Return the one publisher-identical scope of a contract under these filters."""
        pack = next(item for item in self.packs if item.contract_id == contract_id)
        contract = next(item for item in self.contracts if item.contract_id == contract_id)
        (scope,) = publication_scopes_for(
            PublicationScopeRequest(pack, contract, self.bridge, filters)
        )
        return scope


def declarations() -> Declarations:
    """Read the repository's declarations once per caller."""
    return Declarations(
        contracts=tuple(contract for _, contract in load_contracts(ROOT)),
        packs=load_source_packs(ROOT),
        bridge=load_commerce_identity_bridge(ROOT),
    )


def publication(scope: PublicationScope, day: date, revision: str = "a") -> SuccessfulPublication:
    """One CURRENT daily publication of `scope` for `day`."""
    partition = PartitionIdentity(
        scope=scope,
        bucket=MeasurementBucket(
            window=DateRange(day, day),
            grain=TimeGrain.DAY,
            calendar=scope.business_calendar,
            source_timezone=scope.source_timezone,
            observation_basis=scope.observation_basis,
        ),
    )
    token = f"{scope.dataset.value}-{day.isoformat()}-{revision}"
    return SuccessfulPublication(
        publication_id=PublicationId(f"publication-{token}"),
        publisher=CanonicalPublisherKey("unit-forecast-selection"),
        partition=partition,
        attempt_id=AttemptId(f"attempt-{token}"),
        source_inputs=(SourcePublicationRef(f"source-{token}"),),
        source_revision=SourceRevision.instant(
            scheme=RevisionSchemeKey("unit.forecast_selection"),
            source_id=f"source-{token}",
            observed_at=PUBLISHED,
        ),
        content_checksum=f"sha256:{token}",
        evidence=PublicationEvidence(
            rule_set_version="forecast-selection-unit",
            legacy=LegacyPublicationRules(passed_rules=("historical_success",)),
        ),
        fencing_token=1,
        published_at=PUBLISHED,
    )


@dataclass
class MemoryCurrent:
    """CURRENT publications per scope identity; honours the horizon as the store does."""

    by_scope: dict[str, list[SuccessfulPublication]] = field(
        default_factory=dict[str, list[SuccessfulPublication]]
    )
    reads: list[str] = field(default_factory=list[str])

    def put(self, item: SuccessfulPublication) -> None:
        """Make `item` CURRENT for its day, replacing an earlier revision."""
        rows = self.by_scope.setdefault(item.partition.scope.identity, [])
        rows[:] = [row for row in rows if row.partition.identity != item.partition.identity]
        rows.append(item)

    def load_days(self, scope: PublicationScope, first: date, last: date) -> None:
        """Make one publication CURRENT for every day from `first` to `last`."""
        for offset in range((last - first).days + 1):
            self.put(publication(scope, first + timedelta(days=offset)))

    def current_for_scope(
        self, scope: PublicationScope, horizon: ScopeHorizon | None = None
    ) -> tuple[SuccessfulPublication, ...]:
        """Return the scope's publications in bucket order, bounded by the horizon."""
        self.reads.append(scope.identity)
        rows = sorted(
            self.by_scope.get(scope.identity, []),
            key=lambda row: row.partition.bucket.window.start,
        )
        if horizon is not None and horizon.from_bucket_start is not None:
            start = horizon.from_bucket_start
            rows = [row for row in rows if row.partition.bucket.window.start >= start]
        if horizon is not None and horizon.newest is not None:
            rows = rows[-horizon.newest :]
        return tuple(rows)
