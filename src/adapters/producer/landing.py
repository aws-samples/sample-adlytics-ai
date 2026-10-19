# SPDX-License-Identifier: MIT-0
"""Render a governance-bound canonical manifest from a pure finalization plan."""

from __future__ import annotations

from adapters.jobs.curation.manifests import write_manifest
from domain.curation.landing import GOVERNED_MANIFEST_LAYOUT
from domain.producer.planning import FinalizedManifestPlan


def write_governed_manifest(plan: FinalizedManifestPlan) -> str:
    """Render the same manifest serialization Semantic V2 reads."""
    if plan.manifest.governance is None:
        raise ValueError("a governed producer manifest requires its artifact binding.")
    if plan.manifest.layout_version != GOVERNED_MANIFEST_LAYOUT:
        raise ValueError("a governed producer manifest requires query-pack-qualified layout v2.")
    return write_manifest(plan.manifest)
