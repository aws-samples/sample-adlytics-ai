# SPDX-License-Identifier: MIT-0
"""Preset artifact pointer roster and forward-only advance against real DynamoDB.

These tests establish what no in-process substitute can: TransactGetItems response
ordering for the bounded roster read, stable absence for an unwritten variant, the
conditional-transaction classification of an exact replay, and the per-item cancellation
reasons and history old image the graph finalizer classifies a reused revision from. Every
partition key starts with the shared probe prefix so session cleanup can remove it safely.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from botocore.exceptions import ClientError
from probe_identity import PROBE_PREFIX, pytest_node_id

from adapters.outbound.dynamodb.preset_artifacts import (
    DynamoPresetArtifactPointerStore,
    existing_history_revision,
    pointer_transaction_items,
)
from domain.presentation.preset_artifacts import (
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactPointerAdvanceOutcome,
    PresetArtifactPointerAdvanceRequest,
    PresetArtifactPointerRosterRequest,
    PresetArtifactRevision,
    PresetArtifactVariant,
)

if TYPE_CHECKING:
    from types_boto3_dynamodb.type_defs import TransactWriteItemTypeDef

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-070")]
ADVANCED = PresetArtifactPointerAdvanceOutcome.ADVANCED


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


def _granted(pointer: PresetArtifactPointer, grant: str) -> PresetArtifactPointer:
    """Make a fictional pointer grant-bearing, as every graph finalize pointer is."""
    seed = pointer.revision.value
    return replace(
        pointer,
        custody_digest=hashlib.sha256(f"{seed}#custody".encode()).hexdigest(),
        grant_id=grant,
        serving_generation=hashlib.sha256(f"{seed}#s1".encode()).hexdigest(),
        written_at="2026-01-01T00:00:00Z",
    )


def _cancellation_reasons(
    client: Any, items: tuple[TransactWriteItemTypeDef, ...]
) -> list[dict[str, Any]]:
    """Submit one raw pointer transaction that must be cancelled; return its reasons."""
    try:
        client.transact_write_items(TransactItems=list(items))
    except ClientError as error:
        assert error.response.get("Error", {}).get("Code") == "TransactionCanceledException"
        return [dict(reason) for reason in error.response.get("CancellationReasons", [])]
    pytest.fail("the pointer transaction was expected to be cancelled")


@pytest.mark.covers("ADR-140")
def test_a_reused_history_revision_is_cancelled_with_its_old_image_and_per_item_reasons(
    pointer_table: str, request: pytest.FixtureRequest
) -> None:
    """The finalizer's abandon rests on DynamoDB's per-item reasons and the history old image."""
    node_id = pytest_node_id(request)
    page = _probe_page(node_id)
    store = DynamoPresetArtifactPointerStore(pointer_table)
    client = boto3.client("dynamodb")
    g1 = hashlib.sha256(f"{node_id}#g1".encode()).hexdigest()
    g2 = hashlib.sha256(f"{node_id}#g2".encode()).hexdigest()
    expiry = datetime.now(UTC) + timedelta(days=1)
    a1 = _granted(_pointer(page, "7d", node_id), g1)
    assert store.advance(PresetArtifactPointerAdvanceRequest(a1)).outcome is ADVANCED
    b = replace(
        _granted(_pointer(page, "7d", f"{node_id}-b"), g1), forward_token=2, supersedes=a1.revision
    )
    assert store.advance(PresetArtifactPointerAdvanceRequest(b, expiry)).outcome is ADVANCED

    # The rollback shape: A1's revision and bytes again, under another grant, superseding B.
    a2 = replace(a1, grant_id=g2, forward_token=3, supersedes=b.revision)
    items = pointer_transaction_items(
        pointer_table, PresetArtifactPointerAdvanceRequest(a2, expiry)
    )
    reasons = _cancellation_reasons(client, items)

    assert len(reasons) == len(items) == 3
    assert reasons[0]["Code"] == "ConditionalCheckFailed"
    assert "expires_at" in reasons[0]["Item"]  # B's advance retained A1's history
    assert [reason["Code"] for reason in reasons[1:]] == ["None", "None"]
    assert existing_history_revision(items[0], reasons[0]["Item"]) == a2

    # Same-grant control: the request's own grant is never an earlier writer.
    same = replace(a2, grant_id=g1)
    same_items = pointer_transaction_items(
        pointer_table, PresetArtifactPointerAdvanceRequest(same, expiry)
    )
    same_reasons = _cancellation_reasons(client, same_items)
    assert [reason["Code"] for reason in same_reasons] == ["ConditionalCheckFailed", "None", "None"]
    assert existing_history_revision(same_items[0], same_reasons[0]["Item"]) is None

    # Stale-writer control: C moved CURRENT past B, so A3 (superseding B) fails on CURRENT too.
    c = replace(
        _granted(_pointer(page, "7d", f"{node_id}-c"), g1), forward_token=3, supersedes=b.revision
    )
    assert store.advance(PresetArtifactPointerAdvanceRequest(c, expiry)).outcome is ADVANCED
    a3 = replace(a1, grant_id=g2, forward_token=4, supersedes=b.revision)
    stale_items = pointer_transaction_items(
        pointer_table, PresetArtifactPointerAdvanceRequest(a3, expiry)
    )
    stale = _cancellation_reasons(client, stale_items)

    assert len(stale) == len(stale_items) == 3
    assert [reason["Code"] for reason in stale] == [
        "ConditionalCheckFailed",
        "ConditionalCheckFailed",
        "None",
    ]
    assert existing_history_revision(stale_items[0], stale[0]["Item"]) == a3
