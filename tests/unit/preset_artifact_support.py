# SPDX-License-Identifier: MIT-0
"""What the streamed stage (ADR-145) writes for one publication, for fakes that don't stage.

A source's publication carries a pointer template and a custody header; the stage builds the
parent custody from the header and the sibling index, and the pointer is the template completed
with that custody's digest. These helpers compute the same objects with the domain's own
functions, so a fake persistence or a source test reads exactly what the real stage would write.
"""

from __future__ import annotations

from domain.presentation.preset_artifacts import (
    ArtifactBodyWriteResult,
    CustodyManifestWriteResult,
    PresetArtifactBody,
    PresetArtifactCustodyHeader,
    PresetArtifactCustodyManifest,
    PresetArtifactPointer,
    PresetArtifactPointerAdvanceOutcome,
    PresetArtifactPointerAdvanceRequest,
    PresetArtifactPointerAdvanceResult,
    PresetArtifactPointerTemplate,
    PresetArtifactPublicationRequest,
    PresetArtifactPublicationResult,
    PresetArtifactSiblingCustody,
    PresetArtifactSiblingDescriptor,
    PresetArtifactSiblingIndexEntry,
    PresetArtifactSiblingPair,
    PresetArtifactSiblingStream,
    PresetArtifactStageResult,
    canonical_preset_artifact_json,
    parent_custody_manifest,
    sibling_index_shards,
)

__all__ = [
    "indexed_parent_custody",
    "published_result",
    "staged_advance",
    "staged_custody",
    "staged_index",
    "staged_pointer",
    "staged_result",
    "template_of",
]


def template_of(advance: PresetArtifactPointerAdvanceRequest) -> PresetArtifactPointerTemplate:
    """Return the template of a pointer advance: every field except its custody digest."""
    pointer = advance.pointer
    return PresetArtifactPointerTemplate(
        pointer.page,
        pointer.variant,
        pointer.revision,
        pointer.body_digest,
        pointer.forward_token,
        pointer.supersedes,
        advance.superseded_history_expires_at,
        pointer.grant_id,
        pointer.serving_generation,
        pointer.written_at,
        pointer.resolved_window,
        pointer.comparison_window,
    )


def staged_index(
    request: PresetArtifactPublicationRequest,
) -> tuple[tuple[PresetArtifactSiblingCustody, PresetArtifactSiblingIndexEntry], ...]:
    """Return the index shards and their entries the stage writes for the request's stream."""
    if request.custody_header is None:
        return ()
    descriptors = [
        PresetArtifactSiblingDescriptor(public.identity, public.digest, private.digest)
        for public, private in request.siblings
    ]
    return sibling_index_shards(request.body, descriptors)


def staged_custody(
    request: PresetArtifactPublicationRequest,
) -> PresetArtifactCustodyManifest | None:
    """Return the parent custody the stage builds: the header plus the sibling index."""
    if request.custody_header is None:
        return None
    index = tuple(entry for _, entry in staged_index(request))
    return parent_custody_manifest(request.body, request.custody_header, index)


def staged_advance(
    request: PresetArtifactPublicationRequest,
) -> PresetArtifactPointerAdvanceRequest:
    """Return the pointer advance the stage's caller completes from the template."""
    custody = staged_custody(request)
    return request.pointer_template.complete(None if custody is None else custody.digest)


def staged_pointer(request: PresetArtifactPublicationRequest) -> PresetArtifactPointer:
    """Return the pointer one publication advances to."""
    return staged_advance(request).pointer


def staged_result(request: PresetArtifactPublicationRequest) -> PresetArtifactStageResult:
    """Return the stage result the real stage reports for one publication, attested."""
    body = request.body
    custody = staged_custody(request)
    shards = staged_index(request)
    pairs = tuple(request.siblings)
    written = (
        sum(len(item.payload_json.encode()) for pair in pairs for item in pair)
        + sum(len(shard.payload_json.encode()) for shard, _ in shards)
        + len(body.payload_json.encode())
        + (0 if custody is None else len(custody.payload_json.encode()))
    )
    return PresetArtifactStageResult(
        ArtifactBodyWriteResult(
            body.page, body.variant, body.revision, body.key, body.digest, "v1"
        ),
        None
        if custody is None
        else CustodyManifestWriteResult(
            custody.page, custody.variant, custody.revision, custody.key, custody.digest, "v1"
        ),
        len(pairs) + len(shards) + 1,
        written,
    )


def published_result(
    request: PresetArtifactPublicationRequest, outcome: PresetArtifactPointerAdvanceOutcome
) -> PresetArtifactPublicationResult:
    """Return what `publish` reports for one publication whose pointer advance had `outcome`."""
    staged = staged_result(request)
    return PresetArtifactPublicationResult(
        staged.body,
        PresetArtifactPointerAdvanceResult(outcome),
        staged.custody,
        staged.pairs,
        staged.written_bytes,
    )


def indexed_parent_custody(
    body: PresetArtifactBody,
    header: dict[str, object],
    pairs: tuple[PresetArtifactSiblingPair, ...],
) -> tuple[PresetArtifactCustodyManifest, tuple[PresetArtifactSiblingCustody, ...]]:
    """Return the parent custody and the index shards the stage writes for `pairs` under `body`."""
    request = PresetArtifactPublicationRequest(
        body,
        PresetArtifactPointerTemplate(body.page, body.variant, body.revision, body.digest, 1),
        PresetArtifactCustodyHeader(canonical_preset_artifact_json(header)),
        PresetArtifactSiblingStream.of(pairs),
    )
    custody = staged_custody(request)
    assert custody is not None
    return custody, tuple(shard for shard, _ in staged_index(request))
