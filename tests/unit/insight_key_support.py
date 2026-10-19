# SPDX-License-Identifier: MIT-0
"""Card cache keys whose input digest matches their own model input (ADR-076 decision 1)."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import cast

from adapters.inbound.presentation_handlers.insights import InsightAssemblyRequest
from domain.presentation.insights import (
    InsightCacheKey,
    InsightGenerationRequest,
    InsightInputSource,
    insight_input_digest,
    insight_input_document,
)

PENDING_INPUT_DIGEST = "0" * 64
"""A well-formed digest for a key built before its input; ``card_key`` replaces it."""
FICTIONAL_INPUT_DIGEST = "f" * 64
"""A well-formed digest for a key that never reaches a generation (cache-only tests)."""
FICTIONAL_DEEP_INPUT_REVISION = "2026-10-05.fictional"
"""A deep-input revision for deep keys a test derives from a card key."""
_INPUT_FIELDS = ("output_class", "purpose", "findings", "window_profile", "context")


def card_key(key: InsightCacheKey, source: object) -> InsightCacheKey:
    """Return ``key`` carrying the digest of ``source``'s exact model input.

    ``source`` is any object with the five input fields (an assembly, a generation, or a
    ``SimpleNamespace`` a test builds the generation from).
    """
    document = insight_input_document(cast("InsightInputSource", source))
    return replace(key, input_digest=insight_input_digest(document))


def with_card_digest(request: InsightAssemblyRequest) -> InsightAssemblyRequest:
    """Return an assembly request whose key carries the digest of its own input."""
    return replace(request, key=card_key(request.key, request))


def rekeyed_generation(
    request: InsightGenerationRequest, **changes: object
) -> InsightGenerationRequest:
    """Return a card generation with changed input fields and the key digest they imply."""
    fields: dict[str, object] = {name: getattr(request, name) for name in _INPUT_FIELDS}
    fields.update(changes)
    return InsightGenerationRequest(
        key=card_key(request.key, SimpleNamespace(**fields)),
        prompt=request.prompt,
        **fields,  # type: ignore[arg-type]
    )
