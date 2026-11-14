# SPDX-License-Identifier: MIT-0
"""A `publish` node's frozen effect: the counts that decide post-grant eligibility (§2.6, §2.8).

The publish handler writes the record and its digest; the serving election, the precompute and
bake consumers read it. One parser serves all of them, so an election records exactly what a
consumer would decide from (ADR-162 §10).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, cast

from domain.operations.status import canonical_authority_digest

__all__ = ["PUBLISH_EFFECT_NAMESPACE", "PublishEffect", "cohort_publish_effect", "publish_effect"]

PUBLISH_EFFECT_NAMESPACE: Final = "execution-graph-publish-effect/v1"


@dataclass(frozen=True, slots=True)
class PublishEffect:
    """The `publish` node's frozen counts (§2.6), which decide eligibility."""

    advanced_count: int
    advanced_curation_count: int


def publish_effect(record: str | None, digest: str | None, grant_id: str) -> PublishEffect | None:
    """Return a publish record's counts; None unless it re-hashes to `digest` for `grant_id`."""
    if record is None or digest is None:
        return None
    if canonical_authority_digest([PUBLISH_EFFECT_NAMESPACE, record]) != digest:
        return None
    parsed: object = json.loads(record)
    if not isinstance(parsed, dict):
        return None
    fields = cast("dict[str, object]", parsed)
    advanced, curation = fields.get("advanced_count"), fields.get("advanced_curation_count")
    if (
        fields.get("grant_id") != grant_id
        or not isinstance(advanced, int)
        or not isinstance(curation, int)
        or advanced < 0
        or curation < 0
    ):
        return None
    return PublishEffect(advanced, curation)


def cohort_publish_effect(effects: Iterable[PublishEffect]) -> PublishEffect:
    """Sum a cohort's member effects.

    Every consumer's eligibility is a count above zero, so the sum is eligible exactly when some
    member is: what the consumer once decided by reading every member, one at a time.
    """
    advanced = curation = 0
    for effect in effects:
        advanced += effect.advanced_count
        curation += effect.advanced_curation_count
    return PublishEffect(advanced, curation)
