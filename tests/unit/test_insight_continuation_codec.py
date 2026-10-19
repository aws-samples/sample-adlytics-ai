# SPDX-License-Identifier: MIT-0
"""Durable continuation envelope round trips for exact insight generation requests."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
from insight_key_support import FICTIONAL_DEEP_INPUT_REVISION, PENDING_INPUT_DIGEST, card_key

from adapters.outbound.sqs.insight_continuation import parse, serialize
from domain.presentation.insights import (
    DeterministicFinding,
    InsightCacheKey,
    InsightContextEntry,
    InsightContinuationRequest,
    InsightGenerationRequest,
    InsightHorizonRole,
    InsightModelRoute,
    InsightNextStepKind,
    InsightNextStepOption,
    InsightOutputClass,
    InsightPromptActivation,
    InsightPromptTier,
    InsightSlotId,
    InsightWindowProfile,
)
from domain.presentation.next_steps import CampaignKeyTarget, PresetTarget
from domain.presentation.temporal import TemporalDisplayGrain

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-068", "ADR-076")]

_PROFILE = InsightWindowProfile(
    days=7,
    grain=TemporalDisplayGrain.DAY,
    prior_start="2026-08-25",
    prior_end="2026-08-31",
    role=InsightHorizonRole.TACTICAL_REVIEW,
)


def _request() -> InsightContinuationRequest:
    key = InsightCacheKey(
        analytical_scope_id="analytical-scope-v1:fictional",
        subject="campaign-fictional",
        window_start="2026-09-01",
        window_end="2026-09-07",
        artifact_revision="revision-fictional",
        prompt_version="v2",
        slot_id=InsightSlotId.CONTRIBUTION,
        input_digest=PENDING_INPUT_DIGEST,
    )
    model_input = SimpleNamespace(
        output_class=InsightOutputClass.PERSPECTIVE,
        purpose="Explain one server-owned contribution.",
        findings=(DeterministicFinding("Fictional Campaign", Decimal("5")),),
        window_profile=_PROFILE,
        context=(InsightContextEntry("reporting window", "2026-09-01 to 2026-09-07"),),
    )
    return InsightContinuationRequest(
        InsightGenerationRequest(
            key=card_key(key, model_input),
            prompt=InsightPromptActivation(
                InsightSlotId.CONTRIBUTION,
                "v2",
                "A reviewed fictional prompt.",
                InsightModelRoute.QUICK,
            ),
            **vars(model_input),
        )
    )


def test_envelope_round_trips_display_context_beside_exact_findings() -> None:
    request = _request()

    parsed = parse(serialize(request))

    assert parsed is not None
    generation = parsed.generation
    assert generation.key == request.generation.key
    assert generation.findings == request.generation.findings
    assert generation.context == request.generation.context
    # The typed window profile must survive the durable round trip: continuation
    # fills carry the same payload declaration and horizon attributes a serve does.
    assert generation.window_profile == _PROFILE


def test_deep_tier_round_trips_in_the_v4_cache_identity() -> None:
    request = _request()
    request = InsightContinuationRequest(
        replace(
            request.generation,
            key=replace(
                request.generation.key,
                tier=InsightPromptTier.DEEP,
                deep_input_revision=FICTIONAL_DEEP_INPUT_REVISION,
            ),
        )
    )

    parsed = parse(serialize(request))

    assert parsed is not None
    assert parsed.generation.key.tier is InsightPromptTier.DEEP
    assert json.loads(serialize(request))["schema_version"] == 4


def test_prior_schema_version_is_dropped_rather_than_misread() -> None:
    """A version-3 envelope in flight across deploy drops to the established self-heal path."""
    document = json.loads(serialize(_request()))
    document["schema_version"] = 3
    del document["key"]["tier"]

    assert parse(json.dumps(document)) is None


def test_malformed_window_profile_is_rejected() -> None:
    document = json.loads(serialize(_request()))
    document["window_profile"] = {"days": 7, "grain": "day", "role": "tactical-review"}

    assert parse(json.dumps(document)) is None


def test_malformed_context_entries_are_rejected() -> None:
    document = json.loads(serialize(_request()))
    document["context"] = [{"name": "reporting window"}]

    assert parse(json.dumps(document)) is None


@pytest.mark.covers("ADR-076")
def test_both_input_identity_fields_round_trip_on_each_tier() -> None:
    card = _request()
    deep = InsightContinuationRequest(
        replace(
            card.generation,
            key=replace(
                card.generation.key,
                tier=InsightPromptTier.DEEP,
                deep_input_revision=FICTIONAL_DEEP_INPUT_REVISION,
            ),
        )
    )

    for request in (card, deep):
        parsed = parse(serialize(request))
        assert parsed is not None
        assert parsed.generation.key == request.generation.key
        assert parsed.generation.key.content_key == request.generation.key.content_key


@pytest.mark.covers("ADR-076")
def test_a_message_from_before_the_input_identity_is_refused() -> None:
    """Old code enqueued no digest: the worker refuses and acks it, never guessing one."""
    document = json.loads(serialize(_request()))
    for field in ("input_digest", "deep_input_revision"):
        legacy = json.loads(json.dumps(document))
        del legacy["key"][field]
        assert parse(json.dumps(legacy)) is None
    # Positive control: the unedited message parses.
    assert parse(json.dumps(document)) is not None
    # A tampered digest is refused at the generation seam, not served under another key.
    document["key"]["input_digest"] = "e" * 64
    assert parse(json.dumps(document)) is None


@pytest.mark.covers("ADR-076")
def test_served_next_step_options_round_trip_and_a_malformed_one_is_refused() -> None:
    options = (
        InsightNextStepOption(
            "s1", InsightNextStepKind.OPEN_PRESET, PresetTarget("campaigns", "14d"), "Show 14 days"
        ),
        InsightNextStepOption(
            "s2",
            InsightNextStepKind.OPEN_CAMPAIGN_DETAIL,
            CampaignKeyTarget("campaigns", "campaign-key-fictional"),
            "Open Fictional Campaign",
        ),
        InsightNextStepOption("s3", InsightNextStepKind.OPEN_EVIDENCE_STATUS, None, "See why"),
    )
    request = InsightContinuationRequest(replace(_request().generation, next_step_options=options))

    parsed = parse(serialize(request))

    assert parsed is not None and parsed.generation.next_step_options == options
    document = json.loads(serialize(request))
    # A campaign option travels with its key, never a capability.
    assert document["next_step_options"][1]["target"] == {
        "page_id": "campaigns",
        "campaign_key": "campaign-key-fictional",
    }
    for index, target in (
        (2, {"page_id": "campaigns", "preset_id": "14d"}),
        (0, "14d"),
        (1, {"page_id": "campaigns", "campaign_ref": "capability-fictional"}),
    ):
        tampered = json.loads(serialize(request))
        tampered["next_step_options"][index]["target"] = target
        assert parse(json.dumps(tampered)) is None
