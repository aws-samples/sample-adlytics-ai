# SPDX-License-Identifier: MIT-0
"""Declarative Semantic V2 source interpretation contracts, separate from legacy table writers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from domain.semantics.calendar_periods import is_complete_period, longest_grain_days
from domain.semantics.identity import SourceIdentifier
from domain.semantics.publication import CanonicalDatasetKey
from domain.semantics.time import FactTimeClass, ObservationTimeBasis, TimeGrain
from domain.shared_kernel.values import DateRange

__all__ = [
    "CampaignIdentityField",
    "IdentityField",
    "MediaIdentityField",
    "PackReadiness",
    "SourcePack",
    "SourcePackBlocker",
    "SourcePackBlockerFamily",
    "SourcePackControl",
    "SourcePackControlRole",
    "SourcePackRole",
    "TimeField",
]


class SourcePackRole(StrEnum):
    """Canonical responsibility of one interpreted source dataset."""

    FACT = "fact"
    DIMENSION = "dimension"
    CONTROL_TOTAL = "control_total"


class SourcePackControlRole(StrEnum):
    """One member's responsibility in an exact same-delivery control relationship."""

    DETAIL = "detail"
    REPORTED = "reported"


@dataclass(frozen=True, slots=True)
class SourcePackControl:
    """Explicit sibling pack and group for exact same-delivery reconciliation."""

    group: str
    role: SourcePackControlRole
    peer_contract_id: str

    def __post_init__(self) -> None:
        if not self.group.strip() or not self.peer_contract_id.strip():
            raise ValueError("source-pack control requires group and peer contract identities.")


class PackReadiness(StrEnum):
    """Whether verified evidence is sufficient to compile this source pack."""

    READY = "ready"
    BLOCKED = "blocked"


class SourcePackBlockerFamily(StrEnum):
    """Evidence class whose absence prevents source-pack conformance."""

    IDENTITY = "identity"
    TIME = "time"


@dataclass(frozen=True, slots=True)
class SourcePackBlocker:
    """Typed evidence gap that must become a failed conformance result."""

    family: SourcePackBlockerFamily
    detail: str

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise ValueError("source-pack blocker requires diagnostic detail.")


@dataclass(frozen=True, slots=True)
class IdentityField:
    """Qualified source identifier role and exact source path."""

    namespace: str
    source_path: str

    def __post_init__(self) -> None:
        SourceIdentifier(self.namespace, "probe")
        if not self.source_path.strip():
            raise ValueError("identity field requires a source path.")


@dataclass(frozen=True, slots=True)
class CampaignIdentityField:
    """Qualified campaign alias plus its explicit source-native identifier kind."""

    namespace: str
    source_path: str
    identifier_kind: str

    def __post_init__(self) -> None:
        SourceIdentifier(self.namespace, "probe")
        SourceIdentifier(self.identifier_kind, "probe")
        if not self.source_path.strip():
            raise ValueError("campaign identity field requires a source path.")


@dataclass(frozen=True, slots=True)
class MediaIdentityField:
    """Source media identity supplied by one path or one governed profile literal."""

    namespace: str
    source_path: str | None = None
    literal_value: str | None = None

    def __post_init__(self) -> None:
        SourceIdentifier(self.namespace, "probe")
        if (self.source_path is None) == (self.literal_value is None):
            raise ValueError("media identity requires exactly one source path or literal value.")
        if self.source_path is not None and not self.source_path.strip():
            raise ValueError("media identity source path cannot be blank.")
        if self.literal_value is not None and not self.literal_value.strip():
            raise ValueError("media identity literal cannot be blank.")


_PERIODIC_GRAINS = (TimeGrain.WEEK, TimeGrain.MONTH, TimeGrain.QUARTER, TimeGrain.CUSTOM)
"""The complete periods a periodic source pack may declare, in canonical order: the calendar
grains, then the cohort vintage (ADR-104)."""


@dataclass(frozen=True, slots=True)
class TimeField:
    """Source paths and semantics constructing each row measurement bucket.

    A periodic pack (ADR-100) declares `period_grains`, the complete calendar periods its
    deliveries measure, and `grain` is the finest of them. Its row bounds are the delivered AMC
    window, a half-open pair of UTC instants, so the bucket is the inclusive days from the start
    to the day before the end, and its grain is the one declared period that window is exactly
    (`bucket_grain`). Week and month deliveries of one pack are then distinct partitions.
    """

    time_class: FactTimeClass
    start_path: str | None
    end_path: str | None
    grain: TimeGrain
    observation_basis: ObservationTimeBasis
    period_grains: tuple[TimeGrain, ...] = ()

    @property
    def longest_bucket_days(self) -> int:
        """The longest bucket this pack publishes, in days: a scope horizon's overlap shift."""
        return max(longest_grain_days(grain) for grain in (self.period_grains or (self.grain,)))

    def serves_window(self, window: DateRange) -> bool:
        """Whether a bucket over `window` exists: always for a daily pack, else one period."""
        return self.time_class is not FactTimeClass.PERIODIC or any(
            is_complete_period(grain, window.start, window.end) for grain in self.period_grains
        )

    def bucket_grain(self, window: DateRange) -> TimeGrain:
        """Return the grain of a bucket over `window`: the declared grain, or the period's.

        Refuses a periodic window that isn't exactly one complete declared period, so a
        misaligned or partial window can't publish as a period (design §4.6).
        """
        if self.time_class is not FactTimeClass.PERIODIC:
            return self.grain
        for grain in self.period_grains:
            if is_complete_period(grain, window.start, window.end):
                return grain
        declared = ", ".join(grain.value for grain in self.period_grains)
        raise ValueError(
            f"periodic window {window} is not one complete {declared} period (ISO week or "
            "calendar month, UTC)."
        )

    def __post_init__(self) -> None:
        if self.time_class is FactTimeClass.PERIODIC:
            if (
                not self.period_grains
                or self.period_grains
                != tuple(grain for grain in _PERIODIC_GRAINS if grain in self.period_grains)
                or self.grain is not self.period_grains[0]
            ):
                raise ValueError(
                    "a periodic source pack declares period_grains from week, month, quarter and "
                    "custom, "
                    "once each and in that order, and its grain is the first of them."
                )
        elif self.period_grains:
            raise ValueError("only a periodic source pack declares period_grains.")
        if (self.start_path is None) != (self.end_path is None):
            raise ValueError("measurement time requires both bounds or neither.")
        if self.observation_basis is ObservationTimeBasis.SOURCE_BUSINESS_DATE and (
            self.start_path is None or self.end_path is None
        ):
            raise ValueError("source-business-date observation requires explicit row bounds.")
        if self.observation_basis is ObservationTimeBasis.SYSTEM_FIRST_OBSERVED and (
            self.time_class is not FactTimeClass.SNAPSHOT
            or self.grain is not TimeGrain.DAY
            or self.start_path is not None
            or self.end_path is not None
        ):
            raise ValueError(
                "system-first-observed time requires SNAPSHOT, DAY, and no source row bounds."
            )
        if self.time_class is FactTimeClass.DAILY_ADDITIVE and (
            self.grain is not TimeGrain.DAY or self.start_path is None
        ):
            raise ValueError("daily additive source pack requires explicit DAY bounds.")


@dataclass(frozen=True, slots=True)
class SourcePack:
    """One source dataset's V2 identity, time, role, and readiness declaration."""

    contract_id: str
    registry_version: int
    dataset: CanonicalDatasetKey
    role: SourcePackRole
    profile: str
    marketplace: IdentityField
    product: IdentityField | None
    time: TimeField
    readiness: PackReadiness
    blocker: SourcePackBlocker | None = None
    control: SourcePackControl | None = None
    media: MediaIdentityField | None = None
    campaign: CampaignIdentityField | None = None
    advertiser: IdentityField | None = None

    def __post_init__(self) -> None:
        if self.registry_version < 1:
            raise ValueError("source-pack registry versions start at 1.")
        if not self.contract_id.strip() or not self.profile.strip():
            raise ValueError("source pack requires contract and profile identities.")
        if self.readiness is PackReadiness.BLOCKED and self.blocker is None:
            raise ValueError("blocked source pack must state its evidence blocker.")
        if self.readiness is PackReadiness.READY and self.blocker is not None:
            raise ValueError("ready source pack cannot carry a blocker.")
        if self.control is not None:
            if self.control.peer_contract_id == self.contract_id:
                raise ValueError("source-pack control peer must be another contract.")
            expected = (
                SourcePackRole.FACT
                if self.control.role is SourcePackControlRole.DETAIL
                else SourcePackRole.CONTROL_TOTAL
            )
            if self.role is not expected:
                raise ValueError("source-pack role contradicts its control relationship role.")
