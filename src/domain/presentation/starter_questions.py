# SPDX-License-Identifier: MIT-0
"""The assistant's page text: chip label, window label and starter questions (ADR-120).

Every word the assistant panel shows about a page is written here, on the server, from governed
names: the browser never assembles it.

- **Window label.** One calendar form for a reporting window, the form the page's date control
  shows (September 9 to 15, 2026 is "Sep 9", an en dash, then "15, 2026"), so a chip, a starter
  question and the band read the same dates.
- **Chip label.** The page's display parts joined with " · ": the tab, then the window.
- **Starter questions** (decision 3). Up to three, each at most 120 characters, written from the
  page's governed templates and dated with the window label. A template is checked against the
  §8.5 text rules when the page declaration loads, so one that states a cross-channel attributed
  total, or uses a causal verb, fails loading. Each written question is checked again with its
  real values, and one that fails (a long campaign name, say) is dropped, never shortened.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

from domain.presentation.question_rules import question_refusal

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from domain.shared_kernel.values import DateRange

__all__ = [
    "DETAIL_PLACEHOLDERS",
    "MAX_STARTER_QUESTIONS",
    "MAX_STARTER_QUESTION_CHARS",
    "PAGE_PLACEHOLDERS",
    "StarterQuestionTemplateError",
    "StarterQuestionTemplates",
    "page_context_label",
    "render_starter_questions",
    "window_label",
]

MAX_STARTER_QUESTIONS: Final = 3
MAX_STARTER_QUESTION_CHARS: Final = 120
PAGE_PLACEHOLDERS: Final = frozenset({"window", "prior_window"})
DETAIL_PLACEHOLDERS: Final = PAGE_PLACEHOLDERS | {"campaign", "product"}
"""A detail names its entity: campaign detail its {campaign}, product detail its {product}."""
_PLACEHOLDER: Final = re.compile(r"\{([a-z_]+)\}")
_MONTHS: Final = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_RANGE: Final = "\u2013"
"""The en dash between a range's ends, as the browser's date formatter writes it."""
_LONGEST_WINDOW: Final = f"Dec 31, 2025{_RANGE}Jan 13, 2026"
"""The longest window label form (cross-year, two-digit days), used to bound a template's length
when it loads."""
_SEPARATOR: Final = " · "


class StarterQuestionTemplateError(ValueError):
    """A declared starter-question template breaks a §8.5 text rule or its own grammar."""


def window_label(window: DateRange) -> str:
    """Write one inclusive window as the date control does (`formatIsoDateRange`)."""
    start, end = window.start, window.end

    def day(value: date) -> str:
        return f"{_MONTHS[value.month - 1]} {value.day}"

    if start == end:
        return f"{day(start)}, {start.year}"
    if start.year == end.year and start.month == end.month:
        return f"{day(start)}{_RANGE}{end.day}, {start.year}"
    if start.year == end.year:
        return f"{day(start)}{_RANGE}{day(end)}, {start.year}"
    return f"{day(start)}, {start.year}{_RANGE}{day(end)}, {end.year}"


def page_context_label(*parts: str) -> str:
    """Join a page's governed display parts into its chip text."""
    if not parts or any(not part.strip() for part in parts):
        raise ValueError("a page context label needs nonblank parts.")
    return _SEPARATOR.join(parts)


def _render(template: str, values: Mapping[str, str]) -> str:
    return _PLACEHOLDER.sub(lambda match: values[match.group(1)], template)


def _check_template(template: str, allowed: frozenset[str], where: str) -> None:
    names = _PLACEHOLDER.findall(template)
    if "{" in _PLACEHOLDER.sub("", template) or "}" in _PLACEHOLDER.sub("", template):
        raise StarterQuestionTemplateError(f"{where}: a template brace is not a placeholder.")
    if unknown := sorted(set(names) - allowed):
        raise StarterQuestionTemplateError(f"{where}: undeclared placeholders {unknown}.")
    if len(names) != len(set(names)):
        raise StarterQuestionTemplateError(f"{where}: a placeholder repeats.")
    if "window" not in names:
        raise StarterQuestionTemplateError(f"{where}: a starter question names its {{window}}.")
    sample = _render(
        template,
        {
            "window": _LONGEST_WINDOW,
            "prior_window": _LONGEST_WINDOW,
            "campaign": "C",
            "product": "P",
        },
    )
    refusal = question_refusal(
        sample,
        window_label=_LONGEST_WINDOW,
        # A page-level question can read every channel the page shows, so both rules always
        # apply: no cross-channel attributed total, and no causal verb.
        cross_channel=True,
        association_only=True,
        max_chars=MAX_STARTER_QUESTION_CHARS,
    )
    if refusal is not None:
        raise StarterQuestionTemplateError(f"{where}: refused by the text rules ({refusal}).")


@dataclass(frozen=True, slots=True)
class StarterQuestionTemplates:
    """One page's governed starter templates: for the page, and for its detail, if any."""

    page: tuple[str, ...]
    detail: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label, templates, allowed in (
            ("page", self.page, PAGE_PLACEHOLDERS),
            ("detail", self.detail, DETAIL_PLACEHOLDERS),
        ):
            if len(templates) > MAX_STARTER_QUESTIONS:
                raise StarterQuestionTemplateError(
                    f"{label}: at most {MAX_STARTER_QUESTIONS} starter questions."
                )
            if len(set(templates)) != len(templates):
                raise StarterQuestionTemplateError(f"{label}: starter questions repeat.")
            for index, template in enumerate(templates):
                if not isinstance(cast("object", template), str) or not template.strip():
                    raise StarterQuestionTemplateError(f"{label}[{index}]: a template is blank.")
                _check_template(template, allowed, f"{label}[{index}]")


def render_starter_questions(  # noqa: PLR0913 - one keyword per declared placeholder
    templates: tuple[str, ...],
    *,
    window: str,
    prior_window: str | None,
    campaign: str | None = None,
    product: str | None = None,
    product_identifier: str | None = None,
) -> tuple[str, ...]:
    """Write a page's starter questions; a question whose value is absent or fails is dropped.

    `{product}` is the full product title. When the question with the title fails (in practice,
    a long Vendor Central title passes 120 characters), it is written once more with
    `product_identifier`, the exact identifier the page already shows (the child ASIN). Neither
    is ever cut: if both fail, the question is dropped whole (ADR-120).
    """
    values = {
        name: value
        for name, value in (
            ("window", window),
            ("prior_window", prior_window),
            ("campaign", campaign),
            ("product", product),
        )
        if value is not None
    }
    fallback = (
        None
        if product is None or product_identifier is None
        else {**values, "product": product_identifier}
    )
    written: list[str] = []
    for template in templates:
        names = set(_PLACEHOLDER.findall(template))
        if not names <= set(values):
            continue
        candidates = [values] if fallback is None or "product" not in names else [values, fallback]
        for candidate in candidates:
            question = _render(template, candidate)
            if (
                question_refusal(
                    question,
                    window_label=window,
                    cross_channel=True,
                    association_only=True,
                    max_chars=MAX_STARTER_QUESTION_CHARS,
                )
                is None
            ):
                written.append(question)
                break
    return tuple(written[:MAX_STARTER_QUESTIONS])
