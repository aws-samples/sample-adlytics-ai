# SPDX-License-Identifier: MIT-0
"""Immutable preset sibling public/custody behavior against real S3."""

from __future__ import annotations

import hashlib

import boto3
import pytest
from probe_identity import PROBE_PREFIX, pytest_node_id

from adapters.outbound.s3.preset_artifacts import (
    S3PresetArtifactBodyStore,
    S3PresetArtifactCustodyStore,
)
from domain.presentation.preset_artifacts import (
    PresetArtifactPage,
    PresetArtifactRevision,
    PresetArtifactSiblingBody,
    PresetArtifactSiblingCustody,
    PresetArtifactSiblingDescriptor,
    PresetArtifactSiblingIdentity,
    PresetArtifactSiblingReadRequest,
    PresetArtifactVariant,
    canonical_preset_artifact_json,
)
from domain.shared_kernel.errors import InvariantError

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-071")]


@pytest.fixture(scope="module")
def artifact_bucket(deployed_env: dict[str, str]) -> str:
    """Use the deployed artifact bucket or skip when the store is absent."""
    bucket = deployed_env.get("ADL_PRESET_ARTIFACT_BUCKET")
    if not bucket:
        pytest.skip("integration tier needs a deployed preset artifact bucket")
    return bucket


def test_sibling_public_and_custody_create_replay_conflict_and_read_attestation(
    artifact_bucket: str,
    request: pytest.FixtureRequest,
) -> None:
    node_id = pytest_node_id(request)
    page = PresetArtifactPage(f"{PROBE_PREFIX}-{hashlib.sha256(node_id.encode()).hexdigest()[:12]}")
    variant = PresetArtifactVariant("7d")
    revision = PresetArtifactRevision.from_publication_identities((node_id,))
    identity = PresetArtifactSiblingIdentity("name-asc", 0)
    public = PresetArtifactSiblingBody(
        page,
        variant,
        revision,
        identity,
        canonical_preset_artifact_json({"kind": "fictional-public-sibling"}),
    )
    custody = PresetArtifactSiblingCustody(
        page,
        variant,
        revision,
        identity,
        canonical_preset_artifact_json({"kind": "fictional-custody-sibling"}),
    )
    descriptor = PresetArtifactSiblingDescriptor(identity, public.digest, custody.digest)
    read_request = PresetArtifactSiblingReadRequest(page, variant, revision, descriptor)
    public_store = S3PresetArtifactBodyStore(artifact_bucket)
    custody_store = S3PresetArtifactCustodyStore(artifact_bucket)
    s3 = boto3.client("s3")

    try:
        first_public = public_store.write_sibling(public)
        replay_public = public_store.write_sibling(public)
        first_custody = custody_store.write_sibling(custody)
        replay_custody = custody_store.write_sibling(custody)

        assert first_public.digest == replay_public.digest == public.digest
        assert first_custody.digest == replay_custody.digest == custody.digest
        assert public_store.read_sibling(read_request) == public
        assert custody_store.read_sibling(read_request) == custody

        conflicting_public = PresetArtifactSiblingBody(
            page,
            variant,
            revision,
            identity,
            canonical_preset_artifact_json({"kind": "different-public-sibling"}),
        )
        conflicting_custody = PresetArtifactSiblingCustody(
            page,
            variant,
            revision,
            identity,
            canonical_preset_artifact_json({"kind": "different-custody-sibling"}),
        )
        with pytest.raises(InvariantError, match="different bytes"):
            public_store.write_sibling(conflicting_public)
        with pytest.raises(InvariantError, match="different bytes"):
            custody_store.write_sibling(conflicting_custody)

        substituted = PresetArtifactSiblingDescriptor(
            identity,
            hashlib.sha256(b"substituted-public").hexdigest(),
            custody.digest,
        )
        with pytest.raises(InvariantError, match="checksum"):
            public_store.read_sibling(
                PresetArtifactSiblingReadRequest(page, variant, revision, substituted)
            )
    finally:
        for key in (public.key, custody.key):
            s3.delete_object(Bucket=artifact_bucket, Key=key)
