# SPDX-License-Identifier: MIT-0
"""Deterministic prompt composition from git-sourced fragments (ADR-076 decision 6).

One shared foundation, one core per tier, and one block per slot live as reviewed
files in this repository; this module owns the pure composition rule that renders
``foundation + tier core + slot block`` into the complete prompt text and the
digests that make a served narration traceable to its exact sources. Reading the
fragment files is an edge concern (tooling); no prompt text lives here.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _fragment_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PromptFragmentDigests:
    """The sha256 digests of the exact normalized fragments that rendered one prompt."""

    foundation: str
    tier_core: str
    slot_block: str

    def __post_init__(self) -> None:
        for value in (self.foundation, self.tier_core, self.slot_block):
            if not _HEX_DIGEST.fullmatch(value):
                raise ValueError("fragment digests must be 64-character lowercase hex.")

    @property
    def combined(self) -> str:
        """One digest over the three source digests, in composition order.

        This is the value the pointer replicates and the generation log stamps; the
        per-fragment map stays on the immutable version record.
        """
        return hashlib.sha256(
            "\0".join((self.foundation, self.tier_core, self.slot_block)).encode("ascii")
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class PromptFragmentSet:
    """The three reviewed fragment texts that compose one slot-tier prompt."""

    foundation: str
    tier_core: str
    slot_block: str

    def __post_init__(self) -> None:
        if any(not value.strip() for value in (self.foundation, self.tier_core, self.slot_block)):
            raise ValueError("prompt fragments must be nonblank.")


@dataclass(frozen=True, slots=True)
class ComposedPrompt:
    """One rendered prompt with the digests of the exact fragments that produced it."""

    text: str
    digests: PromptFragmentDigests


def compose_prompt(fragments: PromptFragmentSet) -> ComposedPrompt:
    """Render the complete prompt deterministically: same fragments, identical bytes.

    Normalization is part of the rule, not an accident of the file system: each
    fragment is stripped of surrounding whitespace, fragments are joined by exactly
    one blank line in the fixed foundation → tier core → slot block order, and the
    digests are taken over the normalized texts so a trailing-newline difference in
    a file can never fork two "identical" versions.
    """
    normalized = (
        fragments.foundation.strip(),
        fragments.tier_core.strip(),
        fragments.slot_block.strip(),
    )
    return ComposedPrompt(
        text="\n\n".join(normalized),
        digests=PromptFragmentDigests(
            foundation=_fragment_digest(normalized[0]),
            tier_core=_fragment_digest(normalized[1]),
            slot_block=_fragment_digest(normalized[2]),
        ),
    )
