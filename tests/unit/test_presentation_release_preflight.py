# SPDX-License-Identifier: MIT-0
"""Release refusal when deployed Presentation declaration generations differ."""

from __future__ import annotations

import zipfile
from copy import deepcopy
from io import BytesIO
from pathlib import Path

import pytest
import yaml

from adapters.jobs import presentation_release_preflight as preflight

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
V1, V2 = preflight._DECLARATIONS  # pyright: ignore[reportPrivateUsage]
_PREFLIGHT_ARGUMENTS = (
    "--profile",
    "explicit-profile",
    "--region",
    "us-east-1",
    "--expected-account",
    "000000000000",
    "--env",
    "dev",
    "--operation-id",
    "preflight-unit",
    "--stack",
    "Adlytics-Api-dev",
)


def _v1_page(page_id: str, *, artifact_version: int = 1) -> dict[str, object]:
    return {
        "actions": [],
        "artifact": {
            "outcomes": ["available"],
            "type": "FictionalArtifact",
            "version": artifact_version,
        },
        "boundary_required": True,
        "display_name": "Fictional page",
        "grain": {"dimensions": ["marketplace"], "time": "period"},
        "id": page_id,
        "metrics": [{"name": "fictional_metric", "roles": ["summary"]}],
        "mode": "single_basis",
        "preferred_window_duration_days": 7,
        "primary_job": "Internal prose may change independently.",
        "purpose": "Internal prose may change independently.",
        "route": "fictional-route",
        "sections": [{"cardinality": "exactly_one", "id": "summary"}],
    }


def _v1_document(*pages: dict[str, object]) -> dict[str, object]:
    return {
        "context_fields": ["visible-window"],
        "pages": list(pages),
        "version": 1,
    }


def _v2_page(page_id: str) -> dict[str, object]:
    return {
        "actions": [{"id": "inspect", "target_page": page_id}],
        "artifacts": [
            {
                "id": "fictional_artifact",
                "outcomes": ["available"],
                "type": "FictionalArtifact",
                "version": 2,
            }
        ],
        "boundary_required": True,
        "display_name": "Fictional page",
        "id": page_id,
        "mode": "selectable",
        "primary_job": "Render the governed browser contract.",
        "purpose": "Exercise every V2 browser compatibility field.",
        "route": "/fictional/",
        "sections": [
            {"cardinality": "exactly_one", "id": "summary"},
            {"cardinality": "exactly_one", "id": "boundary"},
        ],
        "temporal_policy": {
            "candidate_presets": ["7d"],
            "comparison_policy": "none",
            "custom": {
                "enabled": True,
                "max_duration_days": 90,
                "min_duration_days": 1,
            },
            "default_preset": "7d",
            "display_grains": ["period"],
        },
        "visuals": [
            {
                "id": "fictional_visual",
                "metric_cohort": ["fictional_metric"],
                "narration_eligible": False,
            }
        ],
    }


def _v2_document(*pages: dict[str, object]) -> dict[str, object]:
    return {
        "pages": list(pages),
        "presentation_result_version": 2,
        "temporal_control_version": 1,
        "version": 2,
    }


def _declarations() -> dict[str, dict[str, object]]:
    return {
        "v1": _v1_document(_v1_page("fictional-page")),
        "v2": _v2_document(_v2_page("fictional-page")),
    }


def test_identical_runtime_declarations_are_compatible() -> None:
    declarations = _declarations()

    assert preflight.all_compatibility_findings(declarations, declarations) == ()


def test_v1_artifact_drift_is_reported_by_exact_generation_and_page_path() -> None:
    candidate = _declarations()
    deployed = deepcopy(candidate)
    deployed["v1"]["pages"] = [_v1_page("fictional-page", artifact_version=1)]
    candidate["v1"]["pages"] = [_v1_page("fictional-page", artifact_version=2)]

    findings = preflight.all_compatibility_findings(candidate, deployed)

    assert tuple(finding.path for finding in findings) == ("v1.pages.fictional-page.artifact",)


@pytest.mark.parametrize("field", V2.page_fields)
def test_every_v2_browser_compatibility_field_blocks_publication_when_deployed_value_drifts(
    field: str,
) -> None:
    candidate = _declarations()
    deployed = deepcopy(candidate)
    mutated = _v2_page("fictional-page")
    mutated[field] = "different-page" if field == "id" else {"unexpected": field}
    deployed["v2"]["pages"] = [mutated]

    findings = preflight.all_compatibility_findings(candidate, deployed)

    expected_path = (
        "v2.pages.fictional-page" if field == "id" else f"v2.pages.fictional-page.{field}"
    )
    assert tuple(finding.path for finding in findings) == (expected_path,)


@pytest.mark.parametrize("field", V2.document_fields)
def test_every_v2_browser_transport_version_blocks_publication_when_deployed_value_drifts(
    field: str,
) -> None:
    candidate = _declarations()
    deployed = deepcopy(candidate)
    deployed["v2"][field] = 99

    findings = preflight.all_compatibility_findings(candidate, deployed)

    assert tuple(finding.path for finding in findings) == (f"v2.{field}",)


def test_missing_deployed_v2_declaration_is_a_release_blocker() -> None:
    candidate = _declarations()
    deployed = {"v1": candidate["v1"]}

    findings = preflight.all_compatibility_findings(candidate, deployed)

    assert [(finding.path, finding.candidate, finding.deployed) for finding in findings] == [
        ("v2", "present", "<missing>"),
    ]


def test_archive_reader_requires_and_loads_both_declaration_paths() -> None:
    declarations = _declarations()
    archive_bytes = BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        for declaration in (V1, V2):
            archive.writestr(
                declaration.deployed,
                yaml.safe_dump(declarations[declaration.generation]),
            )

    deployed = preflight._archive_declarations(archive_bytes.getvalue())  # pyright: ignore[reportPrivateUsage]

    assert preflight.all_compatibility_findings(declarations, deployed) == ()


def test_archive_reader_refuses_when_the_v2_declaration_is_not_staged() -> None:
    archive_bytes = BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        archive.writestr(V1.deployed, yaml.safe_dump(_v1_document(_v1_page("fictional-page"))))

    with pytest.raises(
        preflight.CompatibilityInspectionError,
        match="both governed page declarations",
    ):
        preflight._archive_declarations(archive_bytes.getvalue())  # pyright: ignore[reportPrivateUsage]


class _PagedCloudFormation:
    """Two ListStackResources pages; the Presentation function is only on the second."""

    def __init__(self, pages: list[list[dict[str, str]]]) -> None:
        self.pages = pages
        self.stack_names: list[str] = []

    def get_paginator(self, operation: str) -> _PagedCloudFormation:
        assert operation == "list_stack_resources"
        return self

    def paginate(self, *, StackName: str) -> list[dict[str, object]]:  # noqa: N803
        self.stack_names.append(StackName)
        return [{"StackResourceSummaries": page} for page in self.pages]


class _Session:
    def __init__(self, cloudformation: _PagedCloudFormation) -> None:
        self.cloudformation = cloudformation

    def client(self, service: str) -> _PagedCloudFormation:
        assert service == "cloudformation"
        return self.cloudformation


def _function(logical_id: str, physical_id: str) -> dict[str, str]:
    return {
        "LogicalResourceId": logical_id,
        "PhysicalResourceId": physical_id,
        "ResourceType": "AWS::Lambda::Function",
    }


def test_presentation_function_is_found_past_the_first_hundred_stack_resources() -> None:
    first_page = [_function(f"Other{index}", f"fictional-other-{index}") for index in range(100)]
    second_page = [_function("PresentationFunctionABC123", "fictional-presentation")]
    cloudformation = _PagedCloudFormation([first_page, second_page])

    name = preflight._presentation_function_name(  # pyright: ignore[reportPrivateUsage]
        _Session(cloudformation),  # pyright: ignore[reportArgumentType]
        "Adlytics-Api-dev",
    )

    assert name == "fictional-presentation"
    assert cloudformation.stack_names == ["Adlytics-Api-dev"]


def test_presentation_function_lookup_refuses_when_absent_from_every_page() -> None:
    cloudformation = _PagedCloudFormation([[_function("Other", "fictional-other")], []])

    with pytest.raises(preflight.CompatibilityInspectionError, match="exactly one"):
        preflight._presentation_function_name(  # pyright: ignore[reportPrivateUsage]
            _Session(cloudformation),  # pyright: ignore[reportArgumentType]
            "Adlytics-Api-dev",
        )


def test_cli_requires_explicit_operator_identity_and_exact_stack() -> None:
    required_pairs = tuple(zip(_PREFLIGHT_ARGUMENTS[::2], _PREFLIGHT_ARGUMENTS[1::2], strict=True))
    for omitted, _value in required_pairs:
        arguments = tuple(item for pair in required_pairs if pair[0] != omitted for item in pair)
        with pytest.raises(SystemExit) as refusal:
            preflight._parse_args(arguments)  # pyright: ignore[reportPrivateUsage]
        assert refusal.value.code == 2

    with pytest.raises(SystemExit) as refusal:
        preflight._parse_args(  # pyright: ignore[reportPrivateUsage]
            (*_PREFLIGHT_ARGUMENTS[:-1], "Adlytics-Api-prod")
        )
    assert refusal.value.code == 2


def test_cli_refuses_default_profile() -> None:
    with pytest.raises(SystemExit) as refusal:
        preflight._parse_args(  # pyright: ignore[reportPrivateUsage]
            ("--profile", "default", *_PREFLIGHT_ARGUMENTS[2:])
        )
    assert refusal.value.code == 2


def test_candidate_plan_digest_is_stable_and_qualified() -> None:
    first = preflight._candidate_plan()  # pyright: ignore[reportPrivateUsage]
    second = preflight._candidate_plan()  # pyright: ignore[reportPrivateUsage]

    assert first.digest == second.digest
    assert first.digest.startswith("sha256:")
    assert first.declarations == second.declarations


def test_preflight_configures_tracing_before_sts_and_stops_on_account_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    flushed: list[str] = []
    botocore_session = object()

    class Identity:
        def get_caller_identity(self) -> dict[str, str]:
            calls.append("identity")
            return {"Account": "111111111111"}

    class Session:
        _session = botocore_session

        def client(self, service: str) -> Identity:
            calls.append(f"client:{service}")
            if service != "sts":
                pytest.fail(f"account mismatch reached non-STS client {service}")
            return Identity()

    def configure(_settings: object, *, session: object) -> bool:
        assert session is botocore_session
        calls.append("configure")
        return False

    def session_factory(**_kwargs: object) -> Session:
        return Session()

    def ignore(*_args: object) -> None:
        return None

    def flush_metrics() -> None:
        flushed.append("metrics")

    def flush_traces() -> None:
        flushed.append("traces")

    monkeypatch.setattr(preflight.boto3, "Session", session_factory)
    monkeypatch.setattr(preflight, "configure_tracing", configure)
    monkeypatch.setattr(preflight, "report_error", ignore)
    monkeypatch.setattr(preflight, "report_operation", ignore)
    monkeypatch.setattr(preflight, "flush_metrics", flush_metrics)
    monkeypatch.setattr(preflight, "flush_traces", flush_traces)

    result = preflight.main(_PREFLIGHT_ARGUMENTS)

    assert result == 2
    assert calls == ["configure", "client:sts", "identity"]
    assert flushed == ["metrics", "traces"]


def test_frontend_publish_attests_compatibility_before_first_remote_write() -> None:
    script = (ROOT / "scripts/build-frontend.sh").read_text()

    validation = script.index("adapters.jobs.presentation_validation")
    compatibility = script.index("adapters.jobs.presentation_release_preflight")
    first_sync = script.index("aws s3 sync")
    assert validation < compatibility < first_sync
    invocation = script[compatibility:first_sync]
    assert '--profile "$PROFILE"' in invocation
    assert '--region "$REGION"' in invocation
    assert '--expected-account "$EXPECTED_ACCOUNT"' in invocation
    assert '--env "$ENV_NAME"' in invocation
    assert '--stack "$API_STACK"' in invocation
    assert "--trace" in invocation
    assert 'EXPECTED_ACCOUNT="${EXPECTED_ACCOUNT:-}"' in script[:compatibility]
    assert 'if [ "$ACTUAL_ACCOUNT" != "$EXPECTED_ACCOUNT" ]' in script[:compatibility]
    read_param = script[script.index("read_param()") : script.index("BUCKET=$(read_param")]
    assert '--region "$REGION"' in read_param
