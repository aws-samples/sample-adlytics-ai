# SPDX-License-Identifier: MIT-0
"""Audience segments: two separate namespaces and who provides each segment (ADR-103).

`dim_audience_segment` holds DSP behaviour segments (`amc_dsp_behavior`, pack 9) and Amazon
audience segments from Audience Segment Insights (`amc_asi`, pack 13, paid). The two use
different IDs (P-BO-4 step 1 failed on 2026-10-09), so a segment is always addressed by its
namespace and ID together, and nothing here links, joins or compares the two (ADR-103 point 6).

Every segment carries its provider, Amazon or a third party, from the governed classification
(`contracts/shared/audience-segment-providers.yaml`, owner decision 2 of 2026-10-09): an exact
(namespace, name) entry first, then the namespace's rule. A segment neither covers has no
provider and reads "Provider not recorded", never "Amazon" (ADR-103 point 7).

Pure domain; the classification file is read by `adapters.outbound.audience_segment_providers`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from domain.capabilities.paid_features import (
    DerivePaidFeatureStateRequest,
    PaidFeatureState,
    derive_paid_feature_state,
)

__all__ = [
    "ASI_NAMES_NOT_AVAILABLE_TEXT",
    "ASI_NEEDS_PAID_FEATURE_TEXT",
    "MAX_LABEL_LENGTH",
    "PROVIDER_NOT_RECORDED",
    "SEGMENT_FACT_NAMESPACES",
    "SEGMENT_IDS_SENTENCE",
    "SEGMENT_ID_COLUMN",
    "SEGMENT_NAMESPACE_COLUMN",
    "NamespaceRule",
    "ProviderEntry",
    "SegmentLabel",
    "SegmentNamespace",
    "SegmentProvider",
    "SegmentProviderClassification",
    "asi_metadata_state",
    "provider_label",
    "unnamed_segment_label",
]

PROVIDER_NOT_RECORDED: Final = "Provider not recorded"
"""What a segment no classification rule covers reads: never a guessed provider."""

SEGMENT_IDS_SENTENCE: Final = (
    "DSP behaviour segments and Amazon audience segments (paid) use different IDs, so they're "
    "never linked or compared as one list."
)
"""The server sentence the Audiences package carries (revision 2026-10-09.2, P-BO-4)."""

ASI_NEEDS_PAID_FEATURE_TEXT: Final = (
    "Needs the Amazon Insights paid feature (Audience Segment Insights). No data has arrived for "
    "this workspace."
)
"""ADR-107 point 3: nothing observed. Never claims the customer isn't subscribed."""

ASI_NAMES_NOT_AVAILABLE_TEXT: Final = "name not available yet"
"""An ASI segment shown by its ID while pack 13 hasn't been observed (design §6.5)."""


class SegmentNamespace(StrEnum):
    """Which source a segment ID belongs to; IDs are never compared across namespaces."""

    AMC_DSP_BEHAVIOR = "amc_dsp_behavior"
    AMC_ASI = "amc_asi"


class SegmentProvider(StrEnum):
    """Who provides a segment's audience."""

    AMAZON = "amazon"
    THIRD_PARTY = "third_party"


_PROVIDER_LABELS: Final = {
    SegmentProvider.AMAZON: "Amazon",
    SegmentProvider.THIRD_PARTY: "Third party",
}


def provider_label(provider: SegmentProvider | str | None) -> str:
    """Return the display label of a stored provider value, "Provider not recorded" for NULL."""
    if provider is None:
        return PROVIDER_NOT_RECORDED
    return _PROVIDER_LABELS[SegmentProvider(provider)]


@dataclass(frozen=True, slots=True)
class NamespaceRule:
    """One namespace's provider, or None where the namespace mixes providers."""

    namespace: SegmentNamespace
    provider: SegmentProvider | None
    evidence: str

    def __post_init__(self) -> None:
        if not self.evidence.strip():
            raise ValueError(f"the {self.namespace.value} rule names its evidence.")


@dataclass(frozen=True, slots=True)
class ProviderEntry:
    """One classified segment, matched by its namespace and exact name."""

    namespace: SegmentNamespace
    segment_name: str
    provider: SegmentProvider
    category: str
    evidence: str

    def __post_init__(self) -> None:
        if not self.segment_name.strip() or not self.category.strip():
            raise ValueError("a provider entry names its segment and category.")
        if not self.evidence.strip():
            raise ValueError(f"the entry for {self.segment_name!r} names its evidence.")


@dataclass(frozen=True, slots=True)
class SegmentProviderClassification:
    """The governed provider classification: namespace rules and per-segment entries."""

    version: int
    rules: tuple[NamespaceRule, ...]
    entries: tuple[ProviderEntry, ...]

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("the provider classification is versioned from 1.")
        namespaces = [rule.namespace for rule in self.rules]
        if sorted(namespaces) != sorted(SegmentNamespace):
            raise ValueError("the provider classification states one rule per namespace.")
        keys = [(entry.namespace, entry.segment_name) for entry in self.entries]
        if len(set(keys)) != len(keys):
            raise ValueError("the provider classification lists each segment once.")

    def classify(
        self, namespace: SegmentNamespace | str, segment_name: str | None
    ) -> SegmentProvider | None:
        """Return the segment's provider, or None when no entry or namespace rule covers it."""
        space = SegmentNamespace(namespace)
        if segment_name is not None:
            for entry in self.entries:
                if entry.namespace is space and entry.segment_name == segment_name:
                    return entry.provider
        return next(rule.provider for rule in self.rules if rule.namespace is space)


SEGMENT_ID_COLUMN: Final = "segment_id"
SEGMENT_NAMESPACE_COLUMN: Final = "segment_namespace"

SEGMENT_FACT_NAMESPACES: Final[dict[str, SegmentNamespace]] = {
    "fct_segment_delivery": SegmentNamespace.AMC_DSP_BEHAVIOR,
}
"""The one namespace a segment fact's rows carry when a row doesn't name its own.

Pack 8 writes DSP behaviour segments only, so a `segment_id` on `fct_segment_delivery` is always
an `amc_dsp_behavior` ID. A fact holding both namespaces (C2) isn't listed: its rows must carry
`segment_namespace`, or their segments stay unlabelled rather than borrow the other namespace's
names.
"""

MAX_LABEL_LENGTH: Final = 120
"""As `DimensionLabelResolver`'s bound: a longer label falls back to the segment's ID form."""


@dataclass(frozen=True, slots=True)
class SegmentLabel:
    """One segment's governed name and provider, as `dim_audience_segment` holds them."""

    namespace: SegmentNamespace
    segment_id: str
    name: str | None
    provider: SegmentProvider | None
    category_level_1: str | None = None
    """ASI only (pack 13): the segment's top category, which the paid profile shows."""
    category_path: str | None = None
    """ASI only (pack 13): the segment's full category path."""

    @property
    def display(self) -> str:
        """The segment as an answer names it: its name and provider, never a guessed provider.

        A segment whose name hasn't arrived (withheld in its first month, design §5.4) reads by
        its ID with "name not available yet".
        """
        provider = provider_label(self.provider)
        if self.name is not None and self.name.strip():
            named = f"{self.name} · {provider}"
            if len(named) <= MAX_LABEL_LENGTH:
                return named
        return unnamed_segment_label(self.segment_id, self.provider)


def unnamed_segment_label(segment_id: str, provider: SegmentProvider | None = None) -> str:
    """Name a segment by its ID: no governed name for it has arrived yet."""
    return f"Segment {segment_id} ({ASI_NAMES_NOT_AVAILABLE_TEXT}) · {provider_label(provider)}"


def asi_metadata_state(request: DerivePaidFeatureStateRequest) -> PaidFeatureState:
    """Derive pack 13's paid-feature state from its publication evidence (ADR-107).

    `feature_observed` is False when no `amc-segment-metadata` publication exists in its latest
    three declared months: ASI segments then read `ASI_NEEDS_PAID_FEATURE_TEXT` on the paid
    path, or their ID with `ASI_NAMES_NOT_AVAILABLE_TEXT` when packs 10-11 were observed.
    """
    return derive_paid_feature_state(request)
