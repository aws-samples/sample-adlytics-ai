# SPDX-License-Identifier: MIT-0
"""The committed Chronos-2 artifact pin per Region and its review command (D7, ADR-160).

A Region without its own prefix, or a review that reports a moved artifact as matching, would let a
deploy load weights nobody reviewed, which the accelerate NOT AFFECTED disposition rules out.
"""

from __future__ import annotations

import pytest

from adapters.jobs.demand_forecast_artifact_digest.__main__ import (
    PinnedArtifact,
    committed_entry,
    verdict,
)

pytestmark = pytest.mark.unit
# pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
REVIEWED = "07e105dbb16a7a6247b9ea9f5b6043b49959406cf28176a25c59076fdd50c939"
PIN = PinnedArtifact("s3://b/p/", "a" * 64, 41)


@pytest.mark.parametrize(
    "region", ["us-east-1", "us-east-2", "us-west-2", "eu-central-1", "eu-west-1"]
)
def test_each_supported_region_is_pinned_to_its_own_prefix_at_the_reviewed_digest(
    region: str,
) -> None:
    assert committed_entry(region) == PinnedArtifact(
        f"s3://jumpstart-cache-prod-{region}/pytorch-forecasting/pytorch-forecasting-chronos-2/"
        "artifacts/inference-prepack/v1.2.0/",
        REVIEWED,
        41,
    )


def test_an_unlisted_region_has_no_pin() -> None:
    assert committed_entry("ap-south-1") is None


def test_an_unchanged_prefix_matches_its_pin() -> None:
    assert verdict(PIN, PinnedArtifact("s3://b/p/", "a" * 64, 41)) == (
        0,
        "committed: matches model-artifacts.json",
    )


@pytest.mark.parametrize(
    "read",
    [PinnedArtifact("s3://b/p/", "b" * 64, 41), PinnedArtifact("s3://b/p/", "a" * 64, 40)],
    ids=["digest moved", "object count moved"],
)
def test_a_moved_artifact_exits_1(read: PinnedArtifact) -> None:
    assert verdict(PIN, read) == (1, "committed: DIFFERS from model-artifacts.json")


def test_a_prefix_the_contract_does_not_hold_is_reported_unpinned() -> None:
    unpinned = (0, "committed: no model-artifacts.json entry for this Region and prefix")
    assert verdict(None, PIN) == unpinned
    assert verdict(PIN, PinnedArtifact("s3://c/q/", "a" * 64, 41)) == unpinned
