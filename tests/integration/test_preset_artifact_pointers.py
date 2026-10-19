# SPDX-License-Identifier: MIT-0
"""Preset artifact pointer roster and forward-only advance against real DynamoDB.

These tests establish what no in-process substitute can: TransactGetItems response
ordering for the bounded roster read, stable absence for an unwritten variant, and the
conditional-transaction classification of an exact replay. Every partition key starts
with the shared probe prefix so session cleanup can remove it safely.
"""

from __future__ import annotations

import hashlib

import pytest
from probe_identity import PROBE_PREFIX, pytest_node_id

from adapters.outbound.dynamodb.preset_artifacts import DynamoPresetArtifactPointerStore
from domain.presentation.preset_artifacts import (
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactPointerAdvanceOutcome,
    PresetArtifactPointerAdvanceRequest,
    PresetArtifactPointerRosterRequest,
    PresetArtifactRevision,
    PresetArtifactVariant,
)

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-070")]


@pytest.fixture(scope="module")
def pointer_table(deployed_env: dict[str, str]) -> str:
    """Name the deployed pointer table, or skip when its stack is not deployed."""
    table = deployed_env.get("ADL_PRESET_ARTIFACT_POINTER_TABLE")
    if not table:
        pytest.skip("integration tier needs a deployed preset artifact pointer table")
    return table


def _probe_page(node_id: str) -> PresetArtifactPage:
    """Build a sweepable page namespace that satisfies the page identifier grammar."""
    digest = hashlib.sha256(node_id.encode()).hexdigest()[:12]
    return PresetArtifactPage(f"{PROBE_PREFIX}-{digest}")


def _pointer(page: PresetArtifactPage, variant: str, node_id: str) -> PresetArtifactPointer:
    revision = PresetArtifactRevision.from_publication_identities((f"{node_id}#{variant}",))
    body_digest = hashlib.sha256(f"{node_id}#{variant}#body".encode()).hexdigest()
    return PresetArtifactPointer(page, PresetArtifactVariant(variant), revision, body_digest, 1)


def test_roster_preserves_declared_order_and_reports_stable_absence(
    pointer_table: str, request: pytest.FixtureRequest
) -> None:
    """The serve path trusts roster position, so order must come from the request, not the store."""
    node_id = pytest_node_id(request)
    page = _probe_page(node_id)
    store = DynamoPresetArtifactPointerStore(pointer_table)
    seven = _pointer(page, "7d", node_id)
    fourteen = _pointer(page, "14d", node_id)
    for pointer in (seven, fourteen):
        result = store.advance(PresetArtifactPointerAdvanceRequest(pointer))
        assert result.outcome is PresetArtifactPointerAdvanceOutcome.ADVANCED

    roster = store.roster(
        PresetArtifactPointerRosterRequest(
            page,
            (
                PresetArtifactVariant("1d"),
                PresetArtifactVariant("7d"),
                PresetArtifactVariant("14d"),
            ),
        )
    )

    assert roster.pointers == (None, seven, fourteen)
    assert roster.pointer_for(PresetArtifactVariant("14d")) == fourteen
    assert store.current(page, PresetArtifactVariant("7d")) == seven
    assert store.current(page, PresetArtifactVariant("1d")) is None


def test_replaying_an_exact_advance_is_a_no_op_not_a_conflict(
    pointer_table: str, request: pytest.FixtureRequest
) -> None:
    """A redelivered bake must converge without inventing a new revision or failing the worker."""
    node_id = pytest_node_id(request)
    page = _probe_page(node_id)
    store = DynamoPresetArtifactPointerStore(pointer_table)
    pointer = _pointer(page, "7d", node_id)

    first = store.advance(PresetArtifactPointerAdvanceRequest(pointer))
    replay = store.advance(PresetArtifactPointerAdvanceRequest(pointer))

    assert first.outcome is PresetArtifactPointerAdvanceOutcome.ADVANCED
    assert replay.outcome is PresetArtifactPointerAdvanceOutcome.NO_OP
