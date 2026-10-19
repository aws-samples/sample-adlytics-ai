# SPDX-License-Identifier: MIT-0
"""Read audience segments' governed names and providers from `dim_audience_segment` (ADR-103).

A segment is addressed by its namespace, ID and marketplace together: the two namespaces use
different IDs (P-BO-4), and an ID is unique only within its segment marketplace, so a request
names one namespace and one marketplace and gets labels from those rows only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable

from domain.semantics.audience_segments import SegmentLabel, SegmentNamespace

__all__ = ["SegmentLabelPort", "SegmentLabelRequest"]


@dataclass(frozen=True, slots=True)
class SegmentLabelRequest:
    """Which segments of one namespace and marketplace to label, as they stood on one day."""

    namespace: SegmentNamespace
    marketplace_key: str
    """The marketplace the segments were delivered in, canonical or a governed alias.

    It is the segment's marketplace too: pack 9 keys a DSP segment by its campaign's
    marketplace, and pack 13 keys an ASI segment by the pinned segment marketplace.
    """
    segment_ids: tuple[str, ...]
    on: date

    def __post_init__(self) -> None:
        if not self.marketplace_key.strip():
            raise ValueError("a segment label request names its marketplace.")
        if any(not item.strip() for item in self.segment_ids):
            raise ValueError("a segment label request names each segment ID.")


@runtime_checkable
class SegmentLabelPort(Protocol):
    """Reads each segment's name and provider from CURRENT `dim_audience_segment` history."""

    def labels(self, request: SegmentLabelRequest) -> Mapping[str, SegmentLabel]:
        """Return each requested segment's label by ID; a segment with no version is left out.

        Only the requested marketplace's rows are read: a segment with no version there is left
        out, never named from another marketplace. An ID that still resolves to two different
        versions on the day is ambiguous and left out too, never guessed.
        """
        ...
