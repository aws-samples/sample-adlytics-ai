# SPDX-License-Identifier: MIT-0
"""The §8.5 text rules for every server-written question the assistant offers.

One home for the rules a next-step question (ADR-122 decision 2) and a starter question
(ADR-120 decision 3) both follow: a question is dated with its window label, never states a
cross-channel attributed total or its change, and never uses a causal verb about a
full-funnel (E1) or frequency (D2) figure, which read "associated with".
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["question_refusal"]

# Closed word lists. Attributed outcomes never total across governed channels.
_ATTRIBUTED_WORDS: Final = re.compile(
    r"\b(roas|attributed|purchases?|sales|new[- ]to[- ]brand|conversions?)\b", re.IGNORECASE
)
_CROSS_CHANNEL_WORDS: Final = re.compile(
    r"\b(across (all |every |the )?channels|all channels|every channel|combined|in total|"
    r"total)\b",
    re.IGNORECASE,
)
_CAUSAL_VERBS: Final = re.compile(
    r"\b(caus(e|ed|es|ing)|dr(ive|ives|ove|iven|iving)|le(d|ad|ads) to|because of|due to|"
    r"result(ed|s)? in)\b",
    re.IGNORECASE,
)


def question_refusal(  # noqa: PLR0913
    question: str,
    *,
    window_label: str,
    cross_channel: bool,
    association_only: bool,
    max_chars: int,
) -> str | None:
    """Return why a written question can't be offered, or None when it can.

    ``cross_channel`` says the question can read figures from more than one governed channel,
    and ``association_only`` that its subject is a full-funnel (E1) or frequency (D2) figure.
    Length counts code points, as the browser's bound does.
    """
    if not question.strip() or len(question) > max_chars:
        return "length"
    if not window_label.strip() or window_label not in question:
        return "undated"
    if (
        cross_channel
        and _ATTRIBUTED_WORDS.search(question)
        and _CROSS_CHANNEL_WORDS.search(question)
    ):
        return "cross_channel_attributed_total"
    if association_only and _CAUSAL_VERBS.search(question):
        return "causal_verb"
    return None
