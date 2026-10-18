# SPDX-License-Identifier: MIT-0
"""The typed result of one page bake, shared by both bake entries and the page execution.

Their own module so the page execution (`page_bake`) can name them without importing `baker`,
which imports it (ADR-142): one home, no import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass

from adapters.jobs.preset_artifact_publisher.transport import BakeIntent
from domain.presentation.preset_artifacts import (
    PresetArtifactPublicationRequest,
    PresetArtifactRevision,
)

__all__ = ["BakePublication", "BakeResult", "BakeUnavailable", "BakeUnchanged"]


@dataclass(frozen=True, slots=True)
class BakePublication:
    """A real page baker's capability-free artifact ready for persistence."""

    request: PresetArtifactPublicationRequest


@dataclass(frozen=True, slots=True)
class BakeUnavailable:
    """Typed result used until a page-specific baker is registered."""

    intent: BakeIntent
    reason: str


@dataclass(frozen=True, slots=True)
class BakeUnchanged:
    """The source computed the revision the current pointer carries: nothing to write (D26)."""

    intent: BakeIntent
    revision: PresetArtifactRevision


BakeResult = BakePublication | BakeUnavailable | BakeUnchanged
