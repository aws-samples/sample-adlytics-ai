# SPDX-License-Identifier: MIT-0
"""Native DynamoDB cache/registry and Bedrock Converse insight-adapter coverage.

The targets are deliberately operator-supplied rather than discovered.  The prompt-registry
lifecycle needs a table dedicated to this integration probe: registry slot keys are fixed by the
reviewed vocabulary and therefore cannot contain the normal probe prefix.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import boto3
import pytest
from botocore.exceptions import ClientError
from probe_identity import PROBE_PREFIX, probe_key, pytest_node_id

from adapters.outbound.bedrock.insight_calibration import BedrockInsightCalibrationRunner
from adapters.outbound.bedrock.insights import BedrockInsightGenerator, InsightModelRouteModels
from adapters.outbound.dynamodb.insight_cache import (
    DynamoInsightCache,
    InsightCacheDegradeRequest,
)
from adapters.outbound.dynamodb.insight_prompt_registry import DynamoInsightPromptRegistry
from domain.presentation.insights import (
    DeterministicFinding,
    InsightCacheKey,
    InsightCalibrationCase,
    InsightCalibrationDraft,
    InsightCalibrationKind,
    InsightCalibrationResult,
    InsightClaimedPromptCalibrationRequest,
    InsightDeepAnalysis,
    InsightDeepDiveState,
    InsightDeepRecord,
    InsightGenerationRequest,
    InsightHorizonRole,
    InsightModelRoute,
    InsightOutputClass,
    InsightPromptActivation,
    InsightPromptActivationRequest,
    InsightPromptCalibrationClaimRequest,
    InsightPromptCalibrationRunRequest,
    InsightPromptRetirementRequest,
    InsightPromptSeedRequest,
    InsightPromptTier,
    InsightPromptVersionCreateRequest,
    InsightSlot,
    InsightSlotId,
    InsightSlotState,
    InsightWindowProfile,
)
from domain.presentation.prompt_authorship import OperatorPromptAuthor, SystemPromptAuthor
from domain.presentation.temporal import TemporalDisplayGrain
from ports.outbound.insights import InsightCacheSupersessionRequest

if TYPE_CHECKING:
    from boto3.session import Session
    from types_boto3_dynamodb.client import DynamoDBClient

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-068")]

_ENVIRONMENT = "ADL_TEST_ENV"
_REGION = "ADL_TEST_AWS_REGION"
_CACHE_TABLE = "ADL_TEST_INSIGHT_CACHE_TABLE"
_REGISTRY_TABLE = "ADL_TEST_INSIGHT_PROMPT_REGISTRY_TABLE"
_MODEL_ID = "ADL_TEST_INSIGHT_MODEL_ID"
_NONPRODUCTION_ENVIRONMENTS = frozenset({"dev", "test", "sandbox", "integration"})


def _explicit_nonproduction_target(
    variable: str, *, target_must_name_environment: bool = True
) -> str:
    """Require an operator-selected target in an explicit non-production environment."""
    environment = os.environ.get(_ENVIRONMENT, "").strip().lower()
    target = os.environ.get(variable, "").strip()
    if environment not in _NONPRODUCTION_ENVIRONMENTS:
        pytest.skip(f"integration tier needs explicit non-production {_ENVIRONMENT}")
    if not target:
        pytest.skip(f"integration tier needs explicit {variable}")
    if "prod" in target.lower() or (
        target_must_name_environment and environment not in target.lower()
    ):
        pytest.skip(f"{variable} must name the explicit {environment} non-production target")
    return target


@pytest.fixture(scope="module")
def aws_session() -> Session:
    """Create no client until credentials, region, and an explicit target all passed admission."""
    region = os.environ.get(_REGION, "").strip()
    if not region:
        pytest.skip(f"integration tier needs explicit {_REGION}")
    session = boto3.Session(region_name=region)
    if session.get_credentials() is None:
        pytest.skip("integration tier needs AWS credentials")
    return session


@pytest.fixture
def probe(request: pytest.FixtureRequest) -> str:
    return probe_key(pytest_node_id(request))


@pytest.fixture(scope="module")
def dynamodb(aws_session: Session) -> DynamoDBClient:
    return aws_session.client("dynamodb")


@pytest.fixture(scope="module")
def cache_table() -> str:
    return _explicit_nonproduction_target(_CACHE_TABLE)


@pytest.fixture(scope="module")
def registry_table() -> str:
    table = _explicit_nonproduction_target(_REGISTRY_TABLE)
    if PROBE_PREFIX not in table.lower():
        pytest.skip(f"{_REGISTRY_TABLE} must be a dedicated {PROBE_PREFIX} table")
    return table


@pytest.fixture(scope="module")
def model_id() -> str:
    return _explicit_nonproduction_target(_MODEL_ID, target_must_name_environment=False)


def _window_profile() -> InsightWindowProfile:
    return InsightWindowProfile(
        days=7,
        grain=TemporalDisplayGrain.DAY,
        prior_start="2026-08-08",
        prior_end="2026-08-14",
        role=InsightHorizonRole.TACTICAL_REVIEW,
    )


def _cache_key(probe: str) -> InsightCacheKey:
    return InsightCacheKey(
        analytical_scope_id=probe,
        subject=f"campaign-{probe}",
        window_start="2026-08-15",
        window_end="2026-08-21",
        artifact_revision=f"revision-{probe}",
        prompt_version=f"v1-{probe}",
        slot_id=InsightSlotId.CONTRIBUTION,
        input_digest="f" * 64,
    )


def _filled_slot(key: InsightCacheKey) -> InsightSlot:
    return InsightSlot(
        id=key.slot_id,
        output_class=InsightOutputClass.PERSPECTIVE,
        state=InsightSlotState.FILLED,
        text="A fictional probe insight.",
        evidence_line="Fictional campaign spend changed.",
        artifact_revision=key.artifact_revision,
        prompt_version=key.prompt_version,
    )


def _delete_cache_probe(dynamodb: DynamoDBClient, table: str, key: InsightCacheKey) -> None:
    """Remove only this test's complete probe-prefixed DynamoDB identity."""
    if not key.ownership_key.startswith(f"SCOPE#{PROBE_PREFIX}"):
        raise ValueError("integration cache cleanup may remove only probe-prefixed records.")
    try:
        dynamodb.delete_item(
            TableName=table,
            Key={
                "ownership_key": {"S": key.ownership_key},
                "content_key": {"S": key.content_key},
            },
            ConditionExpression="#ownership_key = :ownership_key AND #content_key = :content_key",
            ExpressionAttributeNames={
                "#ownership_key": "ownership_key",
                "#content_key": "content_key",
            },
            ExpressionAttributeValues={
                ":ownership_key": {"S": key.ownership_key},
                ":content_key": {"S": key.content_key},
            },
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise


def test_deep_cache_round_trips_one_structured_analysis_under_the_tier_identity(
    cache_table: str,
    dynamodb: DynamoDBClient,
    probe: str,
) -> None:
    key = replace(_cache_key(probe), tier=InsightPromptTier.DEEP)
    cache = DynamoInsightCache(cache_table, dynamodb)
    _delete_cache_probe(dynamodb, cache_table, key)
    record = InsightDeepRecord(
        slot_id=key.slot_id,
        state=InsightDeepDiveState.FILLED,
        analysis=InsightDeepAnalysis(
            "A fictional movement stands out.",
            ("Reading one: it may be temporary.",),
            "The next delivered window would discriminate.",
            ("5",),
        ),
        absence_reason=None,
        artifact_revision=key.artifact_revision,
        prompt_version=key.prompt_version,
    )
    try:
        written = cache.put_deep_if_absent(record, key)
        assert written.stored
        assert cache.get_deep(key) == record
    finally:
        _delete_cache_probe(dynamodb, cache_table, key)


def test_cache_conditional_write_returns_existing_slot_and_supersedes_only_stale_probe_key(
    cache_table: str,
    dynamodb: DynamoDBClient,
    probe: str,
) -> None:
    key = _cache_key(probe)
    successor = replace(key, artifact_revision=f"revision-successor-{probe}")
    cache = DynamoInsightCache(cache_table, dynamodb)
    _delete_cache_probe(dynamodb, cache_table, key)
    try:
        first = cache.put_if_absent(_filled_slot(key), key)
        repeated = cache.put_if_absent(_filled_slot(key), key)
        superseded = cache.supersede(
            InsightCacheSupersessionRequest(
                stale_key=key,
                successor_key=successor,
                superseded_at=1_800_000_000,
            )
        )
        stale_repeat = cache.supersede(
            InsightCacheSupersessionRequest(
                stale_key=key,
                successor_key=successor,
                superseded_at=1_800_000_001,
            )
        )

        assert first.stored
        assert not repeated.stored
        assert repeated.slot == first.slot
        assert superseded.superseded
        assert not stale_repeat.superseded
    finally:
        _delete_cache_probe(dynamodb, cache_table, key)


def test_prompt_registry_creates_calibrates_activates_and_retires_only_in_empty_probe_table(
    dynamodb: DynamoDBClient,
    probe: str,
    registry_table: str,
) -> None:
    slot_key = "SLOT#contribution"
    existing = dynamodb.get_item(
        TableName=registry_table,
        Key={"slot_key": {"S": slot_key}, "record_key": {"S": "COUNTER"}},
        ConsistentRead=True,
    )
    if "Item" in existing:
        pytest.skip("dedicated prompt-registry probe table already contains contribution records")

    registry = DynamoInsightPromptRegistry(registry_table, dynamodb)
    seed_prompt = f"{probe}: reviewed fictional seed prompt."
    version_prompt = f"{probe}: reviewed fictional calibrated prompt."
    seeded_at = datetime.now(UTC).replace(microsecond=0)
    created_at = seeded_at + timedelta(seconds=1)
    author = OperatorPromptAuthor(probe_key("prompt-author"))
    registry.seed_v1(InsightPromptSeedRequest(InsightSlotId.CONTRIBUTION, seed_prompt, seeded_at))
    version = registry.create_version(
        InsightPromptVersionCreateRequest(
            InsightSlotId.CONTRIBUTION,
            version_prompt,
            InsightModelRoute.DEEP,
            author=author,
            created_at=created_at,
        )
    )
    # B1 round trip against the real table: what create and seed wrote is what versions reads.
    created, seeded = registry.versions(InsightSlotId.CONTRIBUTION)
    assert (created.author, created.created_at) == (author, created_at)
    assert (seeded.author, seeded.created_at) == (SystemPromptAuthor.CONTRACT_SEED, seeded_at)
    work = registry.pending_calibration_outbox((InsightSlotId.CONTRIBUTION,))
    assert [(item.slot_id, item.version) for item in work] == [
        (InsightSlotId.CONTRIBUTION, version)
    ]
    registry.acknowledge_calibration_outbox(InsightSlotId.CONTRIBUTION, version)
    assert registry.pending_calibration_outbox((InsightSlotId.CONTRIBUTION,)) == ()
    claim = registry.claim_calibration(
        InsightPromptCalibrationClaimRequest(
            InsightSlotId.CONTRIBUTION,
            version,
            f"{probe}-lease",
            1_800_000_000,
            1_800_000_090,
        )
    )
    assert claim.pending is not None
    registry.complete_claimed_calibration(
        InsightClaimedPromptCalibrationRequest(
            InsightSlotId.CONTRIBUTION,
            version,
            f"{probe}-lease",
            1_800_000_010,
            InsightCalibrationResult.passed_result(),
        )
    )
    registry.activate(
        InsightPromptActivationRequest(
            InsightSlotId.CONTRIBUTION,
            version,
            1,
            probe,
            f"{probe}-activate",
            datetime.now(UTC),
        )
    )

    active = registry.active(InsightSlotId.CONTRIBUTION, InsightPromptTier.CARD)
    assert version == 2
    assert active is not None
    assert active.version == "v2"
    assert active.prompt == version_prompt
    assert active.model_route is InsightModelRoute.DEEP

    registry.retire(
        InsightPromptRetirementRequest(
            InsightSlotId.CONTRIBUTION,
            version,
            probe,
            f"{probe}-retire",
            datetime.now(UTC),
        )
    )

    assert registry.active(InsightSlotId.CONTRIBUTION, InsightPromptTier.CARD) is None


def test_bedrock_converse_returns_forced_tool_use_perspective_output(
    aws_session: Session,
    model_id: str,
    probe: str,
) -> None:
    key = _cache_key(probe)
    generator = BedrockInsightGenerator(
        InsightModelRouteModels.shared(model_id),
        4,
        aws_session.client("bedrock-runtime"),
    )

    narration = generator.generate(
        InsightGenerationRequest(
            key=key,
            output_class=InsightOutputClass.PERSPECTIVE,
            prompt=InsightPromptActivation(
                InsightSlotId.CONTRIBUTION,
                key.prompt_version,
                f"{probe}: return one concise fictional perspective as the requested JSON.",
                InsightModelRoute.QUICK,
            ),
            purpose="Explain a fictional server-owned contribution.",
            findings=(DeterministicFinding("Fictional campaign spend change", Decimal("5")),),
            window_profile=_window_profile(),
        )
    )

    assert narration.output_class is InsightOutputClass.PERSPECTIVE
    assert len(narration.sentences) <= 3
    assert all(sentence.strip() for sentence in narration.sentences)


@pytest.mark.covers("ADR-076")
def test_bedrock_deep_dive_returns_the_pinned_structured_analysis(
    aws_session: Session,
    model_id: str,
    probe: str,
) -> None:
    """Exercise the real deep tool envelope; a dialog click must not be a card narration."""
    card_key = _cache_key(probe)
    key = replace(card_key, tier=InsightPromptTier.DEEP)
    generator = BedrockInsightGenerator(
        InsightModelRouteModels.shared(model_id),
        20,
        aws_session.client("bedrock-runtime"),
    )

    analysis: InsightDeepAnalysis = generator.generate_deep(
        InsightGenerationRequest(
            key=key,
            output_class=InsightOutputClass.PERSPECTIVE,
            prompt=InsightPromptActivation(
                InsightSlotId.CONTRIBUTION,
                key.prompt_version,
                (
                    f"{probe}: return the requested structured deep analysis. Include "
                    "one concise candidate reading explicitly labeled as a reading; "
                    "cite only the supplied findings value verbatim."
                ),
                InsightModelRoute.DEEP,
            ),
            purpose="Explore one fictional server-owned contribution.",
            findings=(DeterministicFinding("Fictional campaign spend change", Decimal("5")),),
            window_profile=_window_profile(),
        )
    )

    assert analysis.what_stands_out.strip()
    assert 1 <= len(analysis.candidate_readings) <= 3
    # The deep list is kept for the stored shape and never refused (ADR-082).
    assert all(value.strip() for value in analysis.cited_figures)


@pytest.mark.covers("ADR-076", "ADR-082")
def test_bedrock_calibration_generates_and_judges_one_bounded_run(
    aws_session: Session,
    model_id: str,
    probe: str,
) -> None:
    """The generate-then-judge pipeline against real Converse, both tool envelopes.

    Model-judged dispositions are not asserted — they are the calibration's own
    subject — only the structural invariants: every draft reaches the evaluator (no
    text check precedes it, ADR-082), and the run records the evaluator identity.
    """
    runner = BedrockInsightCalibrationRunner(
        InsightModelRouteModels.shared(model_id),
        4,
        aws_session.client("bedrock-runtime"),
    )

    result = runner.run(
        InsightPromptCalibrationRunRequest(
            InsightSlotId.CONTRIBUTION,
            1,
            (
                f"{probe}: narrate the supplied fictional findings in at most three "
                "sentences, stating only supplied values or figures derived from them."
            ),
            InsightModelRoute.QUICK,
            (
                InsightCalibrationCase(
                    InsightCalibrationKind.KNOWN_CORRECT,
                    "A fictional material spend change.",
                    "Accept a grounded perspective.",
                    (DeterministicFinding("Fictional spend change", Decimal("12.45")),),
                ),
                InsightCalibrationCase(
                    InsightCalibrationKind.EXPLORATORY,
                    "A fictional movement invites a hedged reading.",
                    "Accept the labeled reading.",
                    (DeterministicFinding("Fictional reading basis", Decimal("3")),),
                ),
                InsightCalibrationCase(
                    InsightCalibrationKind.CONTRADICTION,
                    "The fictional change is negative but the draft calls it a gain.",
                    "Reject the contradiction.",
                    (DeterministicFinding("Fictional spend change", Decimal("-7.5")),),
                    draft=InsightCalibrationDraft(("The fictional spend rose 7.5 on the week.",)),
                ),
                InsightCalibrationCase(
                    InsightCalibrationKind.FLUENT_UNSUPPORTED,
                    "A fluent draft cites a benchmark outside the findings.",
                    "Reject the figure that is neither supplied nor derived from the findings.",
                    (DeterministicFinding("Fictional spend change", Decimal("12.45")),),
                    draft=InsightCalibrationDraft(
                        ("The change of 12.45 beats the fictional benchmark of 99.",),
                    ),
                ),
                InsightCalibrationCase(
                    InsightCalibrationKind.DERIVED_FIGURE,
                    "A fictional draft derives a percentage change from two supplied values.",
                    "Accept the correctly derived percentage change.",
                    (
                        DeterministicFinding("Fictional spend this window", Decimal("125")),
                        DeterministicFinding("Fictional spend prior window", Decimal("100")),
                    ),
                    draft=InsightCalibrationDraft(
                        ("Fictional spend rose from 100 to 125, up 25 percent.",),
                    ),
                ),
                InsightCalibrationCase(
                    InsightCalibrationKind.MISCOMPUTED_DERIVATION,
                    "A fictional draft divides the change by the current value.",
                    "Reject the miscomputed percentage change.",
                    (
                        DeterministicFinding("Fictional spend this window", Decimal("125")),
                        DeterministicFinding("Fictional spend prior window", Decimal("100")),
                    ),
                    draft=InsightCalibrationDraft(
                        ("Fictional spend rose from 100 to 125, up 20 percent.",),
                    ),
                ),
                InsightCalibrationCase(
                    InsightCalibrationKind.INCOMPATIBLE_BASIS,
                    "A fictional draft adds spend reported in two currencies.",
                    "Reject the figure that combines findings of incompatible currency.",
                    (
                        DeterministicFinding("Fictional spend in US dollars", Decimal("100")),
                        DeterministicFinding("Fictional spend in euros", Decimal("125")),
                    ),
                    draft=InsightCalibrationDraft(
                        ("Fictional spend totals 225 across both.",),
                    ),
                ),
            ),
            InsightOutputClass.PERSPECTIVE,
            purpose="Explain which fictional campaign changes contribute most.",
        )
    )

    assert {case.kind for case in result.cases} == set(InsightCalibrationKind)
    # Every draft carries text, so a judgment call happened and the judging model
    # identity is recorded (ADR-076 §7).
    assert result.evaluator == model_id


def test_cache_degrade_row_is_create_only_and_healed_by_a_conditioned_successful_fill(
    cache_table: str,
    dynamodb: DynamoDBClient,
    probe: str,
) -> None:
    """The retry-exhausted placeholder round-trip against the real conditional writes.

    Create-only degrade, duplicate convergence, read-back through the strict codec,
    and the one permitted replacement (unavailable → generated) asserting the exact
    state it replaces.
    """
    key = _cache_key(probe)
    cache = DynamoInsightCache(cache_table, dynamodb)
    _delete_cache_probe(dynamodb, cache_table, key)
    try:
        degraded = cache.put_unavailable_if_absent(
            InsightCacheDegradeRequest(key, InsightOutputClass.PERSPECTIVE, "ADLY-INSIGHT-0012")
        )
        converged = cache.put_unavailable_if_absent(
            InsightCacheDegradeRequest(key, InsightOutputClass.PERSPECTIVE, "untyped_RuntimeError")
        )
        read_back = cache.get(key)
        healed = cache.put_if_absent(_filled_slot(key), key)
        healed_read = cache.get(key)
        competing_degrade = cache.put_unavailable_if_absent(
            InsightCacheDegradeRequest(key, InsightOutputClass.PERSPECTIVE, "late_failure")
        )

        assert degraded.stored
        assert degraded.slot.state is InsightSlotState.UNAVAILABLE
        assert not converged.stored
        assert converged.slot.state is InsightSlotState.UNAVAILABLE
        assert read_back is not None
        assert read_back.state is InsightSlotState.UNAVAILABLE
        assert healed.stored
        assert healed.slot.state is InsightSlotState.FILLED
        assert healed_read is not None
        assert healed_read.state is InsightSlotState.FILLED
        assert healed_read.text == "A fictional probe insight."
        assert not competing_degrade.stored
        assert competing_degrade.slot.state is InsightSlotState.FILLED
    finally:
        _delete_cache_probe(dynamodb, cache_table, key)
