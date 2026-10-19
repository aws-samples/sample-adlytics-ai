# SPDX-License-Identifier: MIT-0
"""Artifact-derived GRAPH insight planning with no Athena fallback."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import pytest
from test_preset_insight_fill import _fictional_portfolio  # pyright: ignore[reportPrivateUsage]
from test_retail_health import (
    _empty_artifact as _fictional_retail_health,  # pyright: ignore[reportPrivateUsage]
)

from adapters.campaign_insight_requests import portfolio_insight_request_derivation
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.jobs.insight_precompute.materializer import StagedArtifactInsightPlanner
from adapters.outbound.metric_dictionary import load_catalog
from adapters.retail_insight_requests import (
    RetailInsightRequestDerivationInput,
    retail_insight_request_derivation,
)
from domain.operations.execution_graph_release import (
    BakeEffectKind,
    DurableBakeEffectV3,
    PrecomputeInputV1,
)
from domain.presentation.campaign_custody import (
    CampaignCustodyManifest,
    campaign_custody_manifest_payload,
)
from domain.presentation.campaign_portfolio import (
    campaign_portfolio_artifact_from_payload,
    campaign_portfolio_artifact_payload,
)
from domain.presentation.preset_artifacts import (
    PresetArtifactBody,
    PresetArtifactCustodyManifest,
    PresetArtifactPage,
    PresetArtifactRevision,
    PresetArtifactVariant,
    canonical_preset_artifact_json,
)
from domain.presentation.retail_health import (
    retail_health_artifact_from_payload,
    retail_health_artifact_payload,
)
from domain.presentation.retail_health_custody import (
    RetailHealthCustodyManifest,
    retail_health_custody_manifest_payload,
)
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.identity import CommerceScopeKey

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075", "ADR-068")]
ROOT = Path(__file__).resolve().parents[2]
SCOPE = AuthorizedAnalyticalScope(frozenset({CommerceScopeKey("fictional-scope")}))
CONTRACTS = {contract.id: contract for _, contract in load_insight_contracts(ROOT)}
CATALOG = load_catalog(ROOT)


@dataclass
class Bodies:
    body: PresetArtifactBody
    reads: int = 0

    def read(self, pointer: Any) -> PresetArtifactBody:
        self.reads += 1
        if pointer.revision != self.body.revision or pointer.body_digest != self.body.digest:
            raise ValueError("body identity mismatch")
        return self.body


@dataclass
class Custody:
    manifest: PresetArtifactCustodyManifest
    reads: int = 0

    def read(self, pointer: Any) -> PresetArtifactCustodyManifest:
        self.reads += 1
        if (
            pointer.revision != self.manifest.revision
            or pointer.custody_digest != self.manifest.digest
        ):
            raise ValueError("custody identity mismatch")
        return self.manifest


def _fixture() -> tuple[PrecomputeInputV1, PresetArtifactBody, PresetArtifactCustodyManifest]:
    artifact = _fictional_portfolio()
    revision = PresetArtifactRevision.from_publication_identities(("publication-fictional",))
    body = PresetArtifactBody(
        PresetArtifactPage("campaigns"),
        PresetArtifactVariant("7d"),
        revision,
        canonical_preset_artifact_json(campaign_portfolio_artifact_payload(artifact)),
    )
    custody = PresetArtifactCustodyManifest(
        body.page,
        body.variant,
        revision,
        canonical_preset_artifact_json(
            campaign_custody_manifest_payload(CampaignCustodyManifest(body.digest, ()))
        ),
    )
    effect = DurableBakeEffectV3(
        task_id="task-campaigns-7d",
        run_id="run-fictional",
        grant_id="grant-fictional",
        head_version=1,
        page="campaigns",
        variant="7d",
        effect_kind=BakeEffectKind.WRITTEN,
        reason_code=None,
        source_manifest_digest="sha256:" + "a" * 64,
        artifact_revision=revision.value,
        output_manifest_digest="sha256:" + "b" * 64,
        pointer_candidate_digest="sha256:" + "c" * 64,
    )
    inputs = PrecomputeInputV1.from_effects_and_manifests(
        (effect,), {effect.key: (body.key, body.digest, custody.digest)}
    )
    return inputs, body, custody


def _retail_fixture(
    *, custody_body_digest: str | None = None
) -> tuple[PrecomputeInputV1, PresetArtifactBody, PresetArtifactCustodyManifest]:
    artifact = _fictional_retail_health()
    revision = PresetArtifactRevision.from_publication_identities(("publication-retail-fictional",))
    body = PresetArtifactBody(
        PresetArtifactPage("retail-health"),
        PresetArtifactVariant("7d"),
        revision,
        canonical_preset_artifact_json(retail_health_artifact_payload(artifact)),
    )
    custody = PresetArtifactCustodyManifest(
        body.page,
        body.variant,
        revision,
        canonical_preset_artifact_json(
            retail_health_custody_manifest_payload(
                RetailHealthCustodyManifest(custody_body_digest or body.digest, (), ())
            )
        ),
    )
    effect = DurableBakeEffectV3(
        task_id="task-retail-health-7d",
        run_id="run-retail-fictional",
        grant_id="grant-retail-fictional",
        head_version=1,
        page="retail-health",
        variant="7d",
        effect_kind=BakeEffectKind.WRITTEN,
        reason_code=None,
        source_manifest_digest="sha256:" + "d" * 64,
        artifact_revision=revision.value,
        output_manifest_digest="sha256:" + "e" * 64,
        pointer_candidate_digest="sha256:" + "f" * 64,
    )
    inputs = PrecomputeInputV1.from_effects_and_manifests(
        (effect,), {effect.key: (body.key, body.digest, custody.digest)}
    )
    return inputs, body, custody


def _planner(body: Bodies, custody: Custody) -> StagedArtifactInsightPlanner:
    return StagedArtifactInsightPlanner(
        bodies=cast("Any", body),
        custody=cast("Any", custody),
        scope=SCOPE,
        contracts=CONTRACTS,
        catalog=CATALOG,
    )


def test_staged_campaign_artifact_produces_the_same_requests_as_the_served_body() -> None:
    inputs, body_value, custody_value = _fixture()
    bodies, custody = Bodies(body_value), Custody(custody_value)

    planned = _planner(bodies, custody).plan(inputs)

    decoded = campaign_portfolio_artifact_from_payload(json.loads(body_value.payload_json))
    expected, _ = portfolio_insight_request_derivation(
        artifact=decoded,
        scope=SCOPE,
        contracts=CONTRACTS,
    )
    assert planned.requests == expected.requests
    assert planned.request_roster == (("campaigns#7d", "requested"),)
    assert (bodies.reads, custody.reads) == (1, 1)


def test_staged_retail_artifact_produces_the_same_requests_as_the_served_body() -> None:
    inputs, body_value, custody_value = _retail_fixture()
    bodies, custody = Bodies(body_value), Custody(custody_value)

    planned = _planner(bodies, custody).plan(inputs)

    expected = retail_insight_request_derivation(
        RetailInsightRequestDerivationInput(
            artifact=retail_health_artifact_from_payload(json.loads(body_value.payload_json)),
            scope=SCOPE,
            contracts=CONTRACTS,
            catalog=CATALOG,
        )
    )
    assert planned.requests == expected.requests == ()
    assert planned.request_roster == (("retail-health#7d", "no_request"),)
    assert (bodies.reads, custody.reads) == (1, 1)


def test_missing_or_mismatched_custody_fails_closed_without_an_athena_fallback() -> None:
    inputs, body_value, custody_value = _fixture()
    corrupted = PresetArtifactCustodyManifest(
        custody_value.page,
        custody_value.variant,
        custody_value.revision,
        canonical_preset_artifact_json(
            campaign_custody_manifest_payload(CampaignCustodyManifest("d" * 64, ()))
        ),
    )
    bodies, custody = Bodies(body_value), Custody(corrupted)

    with pytest.raises(ValueError, match=r"custody identity mismatch|does not bind"):
        _planner(bodies, custody).plan(inputs)

    assert bodies.reads == 1
    assert custody.reads == 1


def test_mismatched_retail_custody_fails_closed_without_an_athena_fallback() -> None:
    inputs, body_value, custody_value = _retail_fixture(custody_body_digest="d" * 64)
    bodies, custody = Bodies(body_value), Custody(custody_value)

    with pytest.raises(ValueError, match="artifact custody does not bind"):
        _planner(bodies, custody).plan(inputs)

    assert bodies.reads == 1
    assert custody.reads == 1


def test_unavailable_input_is_a_no_request_without_reading_or_model_work() -> None:
    inputs, body_value, custody_value = _fixture()
    entry = inputs.entries[0]
    unavailable = PrecomputeInputV1(
        (
            type(entry)(
                entry.page,
                entry.variant,
                BakeEffectKind.UNAVAILABLE,
                entry.source_manifest_digest,
                None,
                None,
                "campaigns_materialization_unavailable",
            ),
        )
    )
    bodies, custody = Bodies(body_value), Custody(custody_value)

    planned = _planner(bodies, custody).plan(unavailable)

    assert planned.requests == ()
    assert planned.request_roster == (("campaigns#7d", "no_request"),)
    assert (bodies.reads, custody.reads) == (0, 0)


def _deterministic_fixture(
    page: str, variant: str
) -> tuple[PrecomputeInputV1, PresetArtifactBody, DurableBakeEffectV3]:
    revision = PresetArtifactRevision.from_publication_identities((f"publication-{page}",))
    body = PresetArtifactBody(
        PresetArtifactPage(page),
        PresetArtifactVariant(variant),
        revision,
        canonical_preset_artifact_json({"type": "fictional-deterministic-body"}),
    )
    effect = DurableBakeEffectV3(
        task_id=f"task-{page}-{variant}",
        run_id="run-fictional",
        grant_id="grant-fictional",
        head_version=1,
        page=page,
        variant=variant,
        effect_kind=BakeEffectKind.WRITTEN,
        reason_code=None,
        source_manifest_digest="sha256:" + "a" * 64,
        artifact_revision=revision.value,
        output_manifest_digest="sha256:" + "b" * 64,
        pointer_candidate_digest="sha256:" + "c" * 64,
    )
    inputs = PrecomputeInputV1.from_effects_and_manifests(
        (effect,), {effect.key: (body.key, body.digest, None)}
    )
    return inputs, body, effect


@pytest.mark.parametrize(
    ("page", "variant"),
    [
        ("scorecard", "7d"),
        ("new-customers", "last_complete_week"),
        ("reach", "last_complete_quarter"),
        ("retention", "latest_with_current_publication"),
        ("audiences", "last_complete_month"),
    ],
)
def test_o5_a_deterministic_insight_page_is_attested_then_explicitly_no_request(
    page: str, variant: str
) -> None:
    """O5 (B3): the body is read and attested, no custody is read, and no model is asked."""
    inputs, body_value, _ = _deterministic_fixture(page, variant)
    _, _, unused_custody = _fixture()
    bodies, custody = Bodies(body_value), Custody(unused_custody)

    planned = _planner(bodies, custody).plan(inputs)

    assert planned.requests == ()
    assert planned.request_roster == ((f"{page}#{variant}", "no_request"),)
    assert (bodies.reads, custody.reads) == (1, 0)
    # Control: a body that isn't the attested one is refused, never silently skipped.
    other = PresetArtifactBody(
        body_value.page,
        body_value.variant,
        body_value.revision,
        canonical_preset_artifact_json({"type": "another-body"}),
    )
    tampered = Bodies(other)
    with pytest.raises(ValueError, match=r"body identity mismatch|not the attested"):
        _planner(tampered, custody).plan(inputs)


def test_o5_controls_a_model_page_without_custody_and_an_unsupported_page_are_refused() -> None:
    _, body_value, effect = _deterministic_fixture("scorecard", "7d")
    campaigns = replace(effect, page="campaigns")
    with pytest.raises(ValueError, match="no custody manifest"):
        PrecomputeInputV1.from_effects_and_manifests(
            (campaigns,), {campaigns.key: (body_value.key, body_value.digest, None)}
        )
    workspace = replace(effect, page="workspace")
    with pytest.raises(ValueError, match="no custody manifest"):
        PrecomputeInputV1.from_effects_and_manifests(
            (workspace,), {workspace.key: (body_value.key, body_value.digest, None)}
        )


def test_o5_the_deterministic_pages_declare_no_model_prompt_and_campaigns_do() -> None:
    from domain.operations.execution_graph_release import (  # noqa: PLC0415
        DETERMINISTIC_INSIGHT_PAGES,
    )

    deterministic = {
        "network-scorecard": "scorecard",
        "portfolio-acquisition": "new-customers",
        "reach-overlap": "reach",
        "retention-shift": "retention",
        "segment-over-index": "audiences",
    }
    by_id = {contract.id.value: contract for contract in CONTRACTS.values()}
    assert set(deterministic.values()) == DETERMINISTIC_INSIGHT_PAGES
    for slot in deterministic:
        assert by_id[slot].initial_fictional_prompt is None, slot
    # Control: a campaigns model slot carries its prompt.
    assert by_id["contribution"].initial_fictional_prompt is not None


def test_f6_the_bake_roster_pages_are_exactly_the_pages_precompute_handles() -> None:
    """F6: a roster page outside the deterministic set and the two model pages has no planner."""
    from domain.operations.execution_graph_release import (  # noqa: PLC0415
        DETERMINISTIC_INSIGHT_PAGES,
        BakeRoster,
    )

    assert set(BakeRoster.from_contract().pages) == DETERMINISTIC_INSIGHT_PAGES | {
        "campaigns",
        "retail-health",
    }
