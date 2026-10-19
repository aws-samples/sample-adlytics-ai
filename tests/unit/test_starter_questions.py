# SPDX-License-Identifier: MIT-0
"""Starter questions: governed templates checked at load, dated and bounded when written."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from adapters.jobs.presentation_validation.loader import load_v2_page_schemas
from domain.presentation.page_schemas import PageSchemaError
from domain.presentation.starter_questions import (
    MAX_STARTER_QUESTION_CHARS,
    StarterQuestionTemplateError,
    StarterQuestionTemplates,
    page_context_label,
    render_starter_questions,
    window_label,
)
from domain.shared_kernel.values import DateRange

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
WINDOW = "Sep 9\u201315, 2026"
PRIOR = "Sep 2\u20138, 2026"


@pytest.mark.covers("ADR-120")
@pytest.mark.parametrize(
    "template",
    [
        "What were total attributed sales across all channels in {window}?",
        "How did combined ROAS change between {prior_window} and {window}?",
        "What were purchases in total across every channel in {window}?",
    ],
    ids=["total sales across channels", "combined ROAS change", "purchases in total"],
)
def test_a_template_that_states_a_cross_channel_attributed_total_fails_loading(
    template: str,
) -> None:
    with pytest.raises(StarterQuestionTemplateError, match="cross_channel_attributed_total"):
        StarterQuestionTemplates(page=(template,))


@pytest.mark.covers("ADR-120")
def test_the_page_declaration_refuses_such_a_template_when_it_loads(tmp_path: Path) -> None:
    source = ROOT / "presentation/pages/v2/pages.yaml"
    document = yaml.safe_load(source.read_text())
    campaigns = next(page for page in document["pages"] if page["id"] == "campaigns")
    campaigns["assistant"]["starter_questions"]["page"] = [
        "What were total attributed sales across all channels in {window}?"
    ]
    target = tmp_path / "presentation/pages/v2/pages.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(yaml.safe_dump(document, sort_keys=False))

    with pytest.raises(PageSchemaError, match="cross_channel_attributed_total"):
        load_v2_page_schemas(tmp_path)
    # The committed declaration loads, with templates on exactly the routed tabs.
    routed = {
        page.page_id: page.assistant
        for page in load_v2_page_schemas(ROOT).pages
        if page.assistant is not None
    }
    assert set(routed) == {
        "audiences",
        "campaigns",
        "new-customers",
        "reach",
        "retail-health",
        "retention",
        "scorecard",
    }


@pytest.mark.covers("ADR-120")
@pytest.mark.parametrize(
    ("templates", "message"),
    [
        (("Which campaigns drove spend in {window}?",), "causal_verb"),
        (("Which campaign spent most this week?",), "names its"),
        (("Which {product} sold most in {window}?",), "undeclared placeholders"),
        (("Which campaign spent most in {window} and {window}?",), "repeats"),
        (("Which campaign spent {most in {window}?",), "not a placeholder"),
        (("a {window}", "b {window}", "c {window}", "d {window}"), "at most 3"),
        (("x" * MAX_STARTER_QUESTION_CHARS + " {window}",), "length"),
    ],
    ids=["causal verb", "undated", "unknown placeholder", "repeat", "brace", "four", "long"],
)
def test_templates_follow_the_text_rules_and_their_own_grammar(
    templates: tuple[str, ...], message: str
) -> None:
    with pytest.raises(StarterQuestionTemplateError, match=message):
        StarterQuestionTemplates(page=templates)


@pytest.mark.covers("ADR-120")
def test_a_template_that_loads_still_fits_with_the_longest_window_the_server_writes() -> None:
    longest = window_label(DateRange(date(2025, 12, 31), date(2026, 1, 13)))
    room = MAX_STARTER_QUESTION_CHARS - len(longest) - 1
    at_bound = "x" * room + " {window}"

    StarterQuestionTemplates(page=(at_bound,))
    with pytest.raises(StarterQuestionTemplateError, match="length"):
        StarterQuestionTemplates(page=("x" + at_bound,))
    written = render_starter_questions((at_bound,), window=longest, prior_window=None)
    assert [len(question) for question in written] == [MAX_STARTER_QUESTION_CHARS]


@pytest.mark.covers("ADR-120")
def test_a_detail_placeholder_is_refused_on_a_page_template() -> None:
    with pytest.raises(StarterQuestionTemplateError, match="undeclared placeholders"):
        StarterQuestionTemplates(page=("How did ROAS on {campaign} move in {window}?",))
    assert StarterQuestionTemplates(
        page=(), detail=("How did ROAS on {campaign} move in {window}?",)
    ).detail


@pytest.mark.covers("ADR-120")
def test_written_questions_are_dated_and_a_failing_or_unfilled_one_is_dropped() -> None:
    templates = (
        "Which campaign changed spend most between {prior_window} and {window}?",
        "How did ROAS on {campaign} change from {prior_window} to {window}?",
        "How is spend split across campaigns in {window}?",
    )

    written = render_starter_questions(
        templates, window=WINDOW, prior_window=PRIOR, campaign="Fall prospecting · DSP"
    )
    no_prior = render_starter_questions(templates, window=WINDOW, prior_window=None)
    long_name = render_starter_questions(
        templates, window=WINDOW, prior_window=PRIOR, campaign="N" * 120
    )
    instruction = render_starter_questions(
        templates,
        window=WINDOW,
        prior_window=PRIOR,
        campaign="Total sales across all channels",
    )

    assert written == (
        f"Which campaign changed spend most between {PRIOR} and {WINDOW}?",
        f"How did ROAS on Fall prospecting · DSP change from {PRIOR} to {WINDOW}?",
        f"How is spend split across campaigns in {WINDOW}?",
    )
    assert all(len(question) <= MAX_STARTER_QUESTION_CHARS for question in written)
    assert no_prior == (f"How is spend split across campaigns in {WINDOW}?",)
    assert len(long_name) == 2
    # A campaign name can't make a written question state a cross-channel attributed total.
    assert not any("Total sales" in question for question in instruction)


PRODUCT_WINDOW = "Aug 13\u201319, 2026"


def _product_templates() -> tuple[str, ...]:
    """Retail health's committed product-detail templates."""
    page = next(
        item for item in load_v2_page_schemas(ROOT).pages if item.page_id == "retail-health"
    )
    assert page.assistant is not None
    return page.assistant.detail


def _room(template: str) -> int:
    """Characters `{product}` can take in this template before the 120-character limit."""
    fixed = template.replace("{product}", "").replace("{window}", PRODUCT_WINDOW)
    return MAX_STARTER_QUESTION_CHARS - len(fixed)


def _write(template: str, title: str, identifier: str | None) -> tuple[str, ...]:
    return render_starter_questions(
        (template,),
        window=PRODUCT_WINDOW,
        prior_window=None,
        product=title,
        product_identifier=identifier,
    )


@pytest.mark.covers("ADR-120", "ADR-125")
@pytest.mark.parametrize("index", [0, 1], ids=["shipped revenue", "glance views"])
def test_a_product_question_uses_the_full_title_then_the_asin_and_otherwise_drops_whole(
    index: int,
) -> None:
    """Write the full title, then the ASIN, and otherwise drop the question; never cut.

    Full title at 120 characters; one more falls back to the ASIN; an ASIN that can't fit
    either drops the question.
    """
    template = _product_templates()[index]
    room = _room(template)
    asin = "B0FICT0001"

    # Branch 1, full title: at exactly 120 the title is written as it is.
    fits = "T" * room
    assert _write(template, fits, asin) == (
        template.replace("{product}", fits).replace("{window}", PRODUCT_WINDOW),
    )
    assert len(_write(template, fits, asin)[0]) == MAX_STARTER_QUESTION_CHARS
    # Branch 2, identifier: one character more and the question names the ASIN instead.
    too_long = "T" * (room + 1)
    assert _write(template, too_long, asin) == (
        template.replace("{product}", asin).replace("{window}", PRODUCT_WINDOW),
    )
    # The identifier branch has the same boundary: at 120 it is written, at 121 it isn't.
    assert len(_write(template, too_long, "A" * room)[0]) == MAX_STARTER_QUESTION_CHARS
    # Branch 3, dropped: neither the title nor the identifier fits, or there is no identifier.
    assert _write(template, too_long, "A" * (room + 1)) == ()
    assert _write(template, too_long, None) == ()


@pytest.mark.covers("ADR-120", "ADR-125")
def test_a_fitting_title_is_never_replaced_by_the_identifier_and_no_question_is_cut() -> None:
    templates = _product_templates()
    asin = "B0FICT0001"
    for title in ("Trail Bottle 750 ml", "Ridge Socks, 3 pairs", "T" * min(map(_room, templates))):
        written = render_starter_questions(
            templates,
            window=PRODUCT_WINDOW,
            prior_window=None,
            product=title,
            product_identifier=asin,
        )
        assert len(written) == len(templates)
        assert all(title in question and asin not in question for question in written)
    long_title = "Insulated Stainless Steel Water Bottle with Leak-Proof Lid, 750 ml, BPA-Free" * 2
    for title in (long_title, "T" * (max(map(_room, templates)) + 1)):
        written = render_starter_questions(
            templates,
            window=PRODUCT_WINDOW,
            prior_window=None,
            product=title,
            product_identifier=asin,
        )
        assert written and all(asin in question for question in written)
        # Never a cut title: no prefix of it, and no ellipsis.
        assert not any(title[:20] in question for question in written)
        assert not any("\u2026" in question or "..." in question for question in written)


def test_the_window_label_matches_the_date_control_form() -> None:
    assert window_label(DateRange(date(2026, 9, 9), date(2026, 9, 15))) == WINDOW
    assert window_label(DateRange(date(2026, 9, 11), date(2026, 9, 11))) == "Sep 11, 2026"
    assert (
        window_label(DateRange(date(2026, 8, 29), date(2026, 9, 11))) == "Aug 29\u2013Sep 11, 2026"
    )
    assert (
        window_label(DateRange(date(2025, 12, 31), date(2026, 1, 1)))
        == "Dec 31, 2025\u2013Jan 1, 2026"
    )
    assert page_context_label("Campaigns", WINDOW) == f"Campaigns · {WINDOW}"
    with pytest.raises(ValueError, match="nonblank"):
        page_context_label("Campaigns", " ")
