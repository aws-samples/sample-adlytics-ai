# SPDX-License-Identifier: MIT-0
"""Deterministic prompt composition and fragment-digest provenance (ADR-076 decision 6)."""

from __future__ import annotations

import hashlib

import pytest

from domain.presentation.prompt_composition import (
    PromptFragmentDigests,
    PromptFragmentSet,
    compose_prompt,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-076")]

_FRAGMENTS = PromptFragmentSet(
    foundation="A fictional shared analyst foundation.",
    tier_core="A fictional card-tier output core.",
    slot_block="A fictional slot intent block.",
)


def test_same_fragments_render_byte_identical_prompts() -> None:
    """The calibratable unit is the composed whole: rendering must be reproducible."""
    first = compose_prompt(_FRAGMENTS)
    second = compose_prompt(
        PromptFragmentSet(
            foundation="A fictional shared analyst foundation.",
            tier_core="A fictional card-tier output core.",
            slot_block="A fictional slot intent block.",
        )
    )

    assert first.text == second.text
    assert first.text.encode("utf-8") == second.text.encode("utf-8")
    assert first.digests == second.digests
    assert first.text == (
        "A fictional shared analyst foundation.\n\n"
        "A fictional card-tier output core.\n\n"
        "A fictional slot intent block."
    )


def test_surrounding_whitespace_never_forks_two_identical_versions() -> None:
    """A trailing newline in a fragment file must not change the rendered bytes."""
    padded = compose_prompt(
        PromptFragmentSet(
            foundation="\nA fictional shared analyst foundation.\n\n",
            tier_core="A fictional card-tier output core.\n",
            slot_block="  A fictional slot intent block.  ",
        )
    )

    assert padded == compose_prompt(_FRAGMENTS)


def test_digests_are_taken_over_the_exact_normalized_fragment_texts() -> None:
    composed = compose_prompt(_FRAGMENTS)

    assert (
        composed.digests.foundation
        == hashlib.sha256(b"A fictional shared analyst foundation.").hexdigest()
    )
    assert (
        composed.digests.tier_core
        == hashlib.sha256(b"A fictional card-tier output core.").hexdigest()
    )
    assert (
        composed.digests.slot_block == hashlib.sha256(b"A fictional slot intent block.").hexdigest()
    )
    # The combined digest folds the three source digests in composition order.
    assert (
        composed.digests.combined
        == hashlib.sha256(
            "\0".join(
                (
                    composed.digests.foundation,
                    composed.digests.tier_core,
                    composed.digests.slot_block,
                )
            ).encode("ascii")
        ).hexdigest()
    )


def test_any_fragment_edit_changes_its_digest_and_the_combined_digest() -> None:
    base = compose_prompt(_FRAGMENTS)
    edited = compose_prompt(
        PromptFragmentSet(
            foundation=_FRAGMENTS.foundation,
            tier_core="A revised fictional card-tier output core.",
            slot_block=_FRAGMENTS.slot_block,
        )
    )

    assert edited.digests.foundation == base.digests.foundation
    assert edited.digests.slot_block == base.digests.slot_block
    assert edited.digests.tier_core != base.digests.tier_core
    assert edited.digests.combined != base.digests.combined


def test_fragment_shapes_are_validated() -> None:
    with pytest.raises(ValueError, match="nonblank"):
        PromptFragmentSet(foundation=" ", tier_core="core", slot_block="slot")
    with pytest.raises(ValueError, match="lowercase hex"):
        PromptFragmentDigests("not-hex", "a" * 64, "b" * 64)
