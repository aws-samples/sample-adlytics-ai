# SPDX-License-Identifier: MIT-0
"""A change to the compared metrics' governed tone or scale bumps both preset render formats.

A baked Campaigns or Retail health body carries each comparison's change block, tone included,
and its revision hashes only the source publications and the render format
(`PresetArtifactRevision.from_publication_identities`). Re-baking different bytes at the same
revision is refused by the create-once body store, so a `favourable_direction` edit without a
render-format bump leaves baked tiles on the old tone while the serve-time focus takes the new
one. Bumping both formats gives every variant a new revision, so its re-bake supersedes.
"""

from __future__ import annotations

import hashlib
import json
from itertools import pairwise

import pytest
from change_support import SEMANTICS

from adapters.jobs.preset_artifact_publisher.campaign_source import CAMPAIGN_PRESET_RENDER_FORMAT
from adapters.jobs.preset_artifact_publisher.retail_source import (
    RETAIL_HEALTH_PRESET_RENDER_FORMAT,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-070", "ADR-121")]

_BAKED_SEMANTICS: tuple[tuple[int, int, str], ...] = (
    # (Campaigns render format, Retail health render format, compared-roster digest)
    # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    (7, 5, "a6569af05bc2d158fb69780f6775d8e0d341e28230a8a07c1d7e43483c9ae8a7"),
    # ADR-119: the coverage block bumped both formats; the roster is unchanged.
    # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    (8, 6, "a6569af05bc2d158fb69780f6775d8e0d341e28230a8a07c1d7e43483c9ae8a7"),
    # ADR-122: NextStep.v2 bumped both formats so cached slots re-fill; the roster is unchanged.
    # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    (9, 7, "a6569af05bc2d158fb69780f6775d8e0d341e28230a8a07c1d7e43483c9ae8a7"),
    # ADR-125: ordered units, average selling price and revenue per glance view join the roster.
    # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    (10, 8, "6079794f7505fb638e3c1bd3d9b279a6aea772c8ca77a1d0712a5ab25c8e0176"),
    # ADR-127: Delivery per channel's viewable rate joins the roster.
    # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    (11, 9, "7c69615cd2b11fb41d17cebea43721d6675dec6f2599ed0da7ae8c9cbde6a312"),
    # ADR-145: every sibling is found through the stage's sibling index; the roster is unchanged.
    # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    (12, 10, "7c69615cd2b11fb41d17cebea43721d6675dec6f2599ed0da7ae8c9cbde6a312"),
    # ADR-152: Retail's movers are ranked in the query (a tie keeps the lower ASIN); the roster
    # is unchanged.
    # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    (12, 11, "7c69615cd2b11fb41d17cebea43721d6675dec6f2599ed0da7ae8c9cbde6a312"),
)
"""Each (formats, roster) pair the bakers have rendered, oldest first. Append a row (never edit
one) whenever a render format moves, and with both formats bumped when the digest changes."""


def _roster_digest() -> str:
    rows = sorted(
        [name, metric.favourable.value, metric.scale.value] for name, metric in SEMANTICS.metrics
    )
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def test_the_baked_roster_is_the_committed_dictionary_roster_at_the_current_formats() -> None:
    current = (CAMPAIGN_PRESET_RENDER_FORMAT, RETAIL_HEALTH_PRESET_RENDER_FORMAT, _roster_digest())
    assert current == _BAKED_SEMANTICS[-1], (
        "The compared metrics' favourable_direction or change scale changed: bump both "
        "CAMPAIGN_PRESET_RENDER_FORMAT and RETAIL_HEALTH_PRESET_RENDER_FORMAT and append "
        "(campaign, retail, digest) to _BAKED_SEMANTICS."
    )


def test_every_roster_change_bumped_both_render_formats() -> None:
    for earlier, later in pairwise(_BAKED_SEMANTICS):
        assert later[0] >= earlier[0]
        assert later[1] >= earlier[1]
        assert later[:2] != earlier[:2]
        if later[2] != earlier[2]:
            assert later[0] > earlier[0]
            assert later[1] > earlier[1]
