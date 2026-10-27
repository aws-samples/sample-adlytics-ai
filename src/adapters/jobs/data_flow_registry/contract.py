# SPDX-License-Identifier: MIT-0
"""Strict loader and deterministic pure-source renderer for data-flow declarations."""

from __future__ import annotations

import pprint
from pathlib import Path
from typing import Any, Final, cast

import yaml

from adapters.outbound import repository_yaml

CONTRACT_PATH: Final = Path("contracts/operations/data-flows.yaml")
GENERATED_PATH: Final = Path("src/domain/operations/generated_data_flows.py")
INFRA_PATH: Final = Path("infra/lib/stacks/operational-status-processor-stack.ts")
ASYNC_IDS: Final = (
    "external_source_delivery",
    "producer_registration",
    "raw_manifest_landing",
    "ingestion_transport",
    "semantic_source_materialization",
    "semantic_candidate_publication",
    "semantic_current_publication",
    "semantic_projection_plan",
    "semantic_projection_unit",
    "semantic_inline_table_run",
    "semantic_servable_pointer",
    "page_cohort_readiness",
    "preset_enqueue",
    "preset_bake",
    "insight_precompute",
    "insight_continuation",
    "evaluation_handoff",
    "evaluation_dispatch",
    "evaluation_input_write",
    "evaluation_result_ingress",
    "evaluation_result_persist",
    "agent_quality_processing",
)
STAGE0_ACTIVE_IDS: Final = (
    "semantic_candidate_publication",
    "preset_enqueue",
    "preset_bake",
    "insight_precompute",
)
STAGE1_SURFACE_BY_AUTHORITY_KIND: Final = {
    "write_plan_meta": "semantic_projection_plan",
    "write_plan_unit": "semantic_projection_unit",
    "inline_table_run": "semantic_inline_table_run",
    "current_servable_pointer": "semantic_servable_pointer",
}
STAGE1_STATES_BY_SURFACE: Final = {
    "semantic_projection_plan": ("unknown", "queued", "failed", "completed", "superseded"),
    "semantic_projection_unit": ("unknown", "running", "retrying", "failed", "completed"),
    "semantic_inline_table_run": (
        "unknown",
        "running",
        "retrying",
        "failed",
        "completed",
        "superseded",
    ),
    "semantic_servable_pointer": ("unknown", "completed", "superseded"),
}
ACTIVE_IDS: Final = (
    "semantic_candidate_publication",
    *STAGE1_STATES_BY_SURFACE,
    "preset_enqueue",
    "preset_bake",
    "insight_precompute",
)
SYNC_IDS: Final = (
    "campaign_portfolio_parent",
    "campaign_portfolio_ordered_segment",
    "campaign_detail_displayed",
    "campaign_detail_live_tail",
    "campaign_metric_focus",
    "retail_health_parent",
    "retail_health_metric_focus",
)
ROOT_FIELDS: Final = frozenset(
    {"registry_version", "identity_contract", "observation_policy", "surfaces"}
)
IDENTITY_FIELDS: Final = frozenset(
    {"namespace_version", "uuid_namespace", "canonical_authority_digest", "event_name_tuple"}
)
SURFACE_FIELDS: Final = frozenset(
    {
        "id",
        "classification",
        "owner",
        "boundary",
        "trigger",
        "workload_transport",
        "authority",
        "source_observation",
        "status_producer",
        "states",
        "correlation",
        "telemetry",
        "reconciliation",
        "freshness",
        "retention",
        "completion_contract",
        "dependency_roster_ref",
        "evaluation_mode",
        "measurements",
        "operator_definition",
        "status",
    }
)
MEASUREMENT_FIELDS: Final = frozenset(
    {
        "measurement_ref",
        "measurement_version",
        "measure",
        "unit",
        "start_authority",
        "completion_authority",
    }
)
FORBIDDEN_FIELD_PARTS: Final = ("objective", "target", "result")
REQUIRED_INFRA_LITERALS: Final = (
    "adapters.jobs.operational_status.attempts.handler",
    "adapters.jobs.operational_status.projector.handler",
    "adapters.jobs.operational_status.reconciler.handler",
    "ADL_OPERATIONAL_STATUS_QUEUE_URL",
    "ADL_OPERATIONAL_STATUS_TABLE",
    "ADL_SEMANTIC_ATTEMPTS_TABLE",
    "reportBatchItemFailures: true",
)


class DataFlowRegistryError(ValueError):
    """The governed registry or its infrastructure coverage is invalid."""


class _StrictSafeLoader(repository_yaml.RepositoryLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _StrictSafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[str, object]:
    loader.flatten_mapping(node)
    result: dict[str, object] = {}
    for key_node, value_node in node.value:
        key = cast("Any", loader).construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise DataFlowRegistryError("data-flow registry mapping keys must be text")
        if key in result:
            raise DataFlowRegistryError(f"data-flow registry contains duplicate key: {key}")
        result[key] = cast("Any", loader).construct_object(value_node, deep=deep)
    return result


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def load_registry(root: Path) -> dict[str, object]:
    """Load the closed YAML registry, rejecting unknown fields and semantic drift."""
    path = root / CONTRACT_PATH
    try:
        # A-YAML-SAFELOADER-001: a CSafeLoader subclass; see docs/security-dispositions.md
        raw = cast("object", yaml.load(path.read_text(), Loader=_StrictSafeLoader))  # noqa: S506  # nosec B506
    except (OSError, yaml.YAMLError) as error:
        raise DataFlowRegistryError(f"could not load {CONTRACT_PATH}: {error}") from error
    document = _mapping(raw, str(CONTRACT_PATH))
    _exact_fields(document, ROOT_FIELDS, str(CONTRACT_PATH))
    if document["registry_version"] != "1":
        raise DataFlowRegistryError("registry_version must be the text '1'")
    if document["observation_policy"] != "authority_backed_observation_only":
        raise DataFlowRegistryError("registry observation policy is not the accepted v1 policy")
    identity = _mapping(document["identity_contract"], "identity_contract")
    _exact_fields(identity, IDENTITY_FIELDS, "identity_contract")
    if identity["namespace_version"] != "operational-data-flow-v1":
        raise DataFlowRegistryError("identity namespace version is not v1")
    if identity["canonical_authority_digest"] != "sha256-canonical-json-v1":
        raise DataFlowRegistryError("authority digest contract is not canonical v1")
    surfaces = _sequence(document["surfaces"], "surfaces")
    parsed = tuple(_surface(item, index) for index, item in enumerate(surfaces))
    _check_rosters(parsed)
    _reject_forbidden_fields(document)
    return document


def _check_rosters(parsed: tuple[dict[str, object], ...]) -> None:
    identifiers = tuple(cast("str", item["id"]) for item in parsed)
    if len(identifiers) != len(set(identifiers)):
        raise DataFlowRegistryError("surface ids must be unique")
    asynchronous = tuple(
        cast("str", item["id"]) for item in parsed if item["classification"] == "asynchronous"
    )
    synchronous = tuple(
        cast("str", item["id"])
        for item in parsed
        if item["classification"] == "synchronous_not_movement"
    )
    if asynchronous != ASYNC_IDS:
        raise DataFlowRegistryError("asynchronous roster must match the exact ordered v1 ids")
    if synchronous != SYNC_IDS:
        raise DataFlowRegistryError("synchronous roster must match the exact ordered v1 ids")
    active = tuple(item for item in parsed if item["status"] == "active")
    active_ids = tuple(cast("str", item["id"]) for item in active)
    if active_ids != ACTIVE_IDS:
        raise DataFlowRegistryError("active surfaces must match the exact ordered v1 roster")
    for item in active:
        if item["status_producer"] == "planned" or not item["measurements"]:
            raise DataFlowRegistryError(
                "an active surface requires a named producer and at least one measurement"
            )
    _check_stage1_surfaces(parsed)
    external = tuple(item for item in parsed if item["status"] == "external_boundary")
    if tuple(item["id"] for item in external) != ("external_source_delivery",):
        raise DataFlowRegistryError("external_source_delivery must be the only external boundary")
    for item in parsed:
        if item["evaluation_mode"] != "observation_only":
            raise DataFlowRegistryError("v1 surfaces must remain observation_only")
        if item["status"] not in {"active", "planned", "external_boundary"}:
            raise DataFlowRegistryError("surface status is outside the closed v1 vocabulary")


def _check_stage1_surfaces(parsed: tuple[dict[str, object], ...]) -> None:
    expected_pairs = tuple(STAGE1_SURFACE_BY_AUTHORITY_KIND.items())
    expected_authorities = frozenset(STAGE1_SURFACE_BY_AUTHORITY_KIND)
    expected_ids = tuple(STAGE1_STATES_BY_SURFACE)
    actual_pairs = tuple(
        (cast("str", item["authority"]), cast("str", item["id"]))
        for item in parsed
        if item["authority"] in expected_authorities or item["id"] in STAGE1_STATES_BY_SURFACE
    )
    if actual_pairs != expected_pairs:
        raise DataFlowRegistryError(
            "Stage 1 authority kinds and surfaces must match the exact ordered v1 pairs"
        )
    producer_ids = tuple(
        item["id"] for item in parsed if item["status_producer"] == "stage1_lifecycle_producer"
    )
    if producer_ids != expected_ids:
        raise DataFlowRegistryError(
            "stage1_lifecycle_producer must name exactly the four Stage 1 surfaces"
        )
    stage1 = tuple(item for item in parsed if item["id"] in STAGE1_STATES_BY_SURFACE)
    if tuple(item["id"] for item in stage1) != expected_ids:
        raise DataFlowRegistryError("Stage 1 surfaces must match the exact ordered v1 roster")
    for item in stage1:
        identifier = cast("str", item["id"])
        if tuple(cast("list[object]", item["states"])) != STAGE1_STATES_BY_SURFACE[identifier]:
            raise DataFlowRegistryError(f"{identifier} states differ from the exact v1 states")
        if not item["measurements"]:
            raise DataFlowRegistryError(f"{identifier} requires at least one measurement")


def infrastructure_findings(root: Path) -> tuple[str, ...]:
    """Check the pre-existing processor declaration without synth or AWS access."""
    try:
        source = (root / INFRA_PATH).read_text()
    except OSError:
        return (f"missing {INFRA_PATH}",)
    findings = [
        f"{INFRA_PATH} is missing {literal}"
        for literal in REQUIRED_INFRA_LITERALS
        if literal not in source
    ]
    if "new targets.SqsQueue(statusQueue" in source:
        findings.append("status FIFO must not be an EventBridge target")
    return tuple(findings)


def render_generated(document: dict[str, object]) -> str:
    """Render a deterministic Python-only registry with no filesystem dependency."""
    identity = cast("dict[str, object]", document["identity_contract"])
    surfaces = cast("list[object]", document["surfaces"])

    def constant(name: str, value: object, *, sort_dicts: bool) -> str:
        # The value opens on its own line so pformat's width is the whole line width: a prefix
        # in front of the first pformat line would push that line past the 100-column lint.
        body = pprint.pformat(value, sort_dicts=sort_dicts, width=88)
        return f"{name}: Final = (\n{body}\n)"

    return "\n".join(
        [
            "# SPDX-License-Identifier: MIT-0",
            '"""Generated by `make generate-data-flow-registry`; do not edit by hand."""',
            "",
            "from typing import Final",
            "",
            f"REGISTRY_VERSION: Final = {document['registry_version']!r}",
            f"OBSERVATION_POLICY: Final = {document['observation_policy']!r}",
            constant("IDENTITY_CONTRACT", identity, sort_dicts=True),
            constant(
                "STAGE1_SURFACE_BY_AUTHORITY_KIND",
                STAGE1_SURFACE_BY_AUTHORITY_KIND,
                sort_dicts=False,
            ),
            constant("STAGE1_STATES_BY_SURFACE", STAGE1_STATES_BY_SURFACE, sort_dicts=False),
            constant("SURFACES", tuple(surfaces), sort_dicts=True),
            "",
        ]
    )


def _surface(value: object, index: int) -> dict[str, object]:
    where = f"surfaces[{index}]"
    item = _mapping(value, where)
    _exact_fields(item, SURFACE_FIELDS, where)
    for key in SURFACE_FIELDS - {"dependency_roster_ref", "measurements", "states"}:
        if not isinstance(item[key], str) or not cast("str", item[key]).strip():
            raise DataFlowRegistryError(f"{where}.{key} must be nonblank text")
    if item["classification"] not in {"asynchronous", "synchronous_not_movement"}:
        raise DataFlowRegistryError(f"{where}.classification is invalid")
    states = _sequence(item["states"], f"{where}.states")
    if not states or not all(isinstance(state, str) and state for state in states):
        raise DataFlowRegistryError(f"{where}.states must be nonempty text")
    if len(states) != len(set(cast("list[str]", states))):
        raise DataFlowRegistryError(f"{where}.states contains a duplicate")
    measurements = _sequence(item["measurements"], f"{where}.measurements")
    for measurement_index, measurement_value in enumerate(measurements):
        measurement = _mapping(measurement_value, f"{where}.measurements[{measurement_index}]")
        _exact_fields(
            measurement,
            MEASUREMENT_FIELDS,
            f"{where}.measurements[{measurement_index}]",
        )
        if not all(isinstance(field, str) and field.strip() for field in measurement.values()):
            raise DataFlowRegistryError(f"{where}.measurements must contain nonblank text")
    return item


def _reject_forbidden_fields(value: object) -> None:
    if isinstance(value, dict):
        for key, child in cast("dict[object, object]", value).items():
            if isinstance(key, str) and any(part in key.lower() for part in FORBIDDEN_FIELD_PARTS):
                raise DataFlowRegistryError(f"forbidden observation-only field: {key}")
            _reject_forbidden_fields(child)
    elif isinstance(value, list):
        for child in cast("list[object]", value):
            _reject_forbidden_fields(child)


def _mapping(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise DataFlowRegistryError(f"{where} must be a mapping")
    return cast("dict[str, object]", value)


def _sequence(value: object, where: str) -> list[object]:
    if not isinstance(value, list):
        raise DataFlowRegistryError(f"{where} must be a list")
    return cast("list[object]", value)


def _exact_fields(value: dict[str, object], expected: frozenset[str], where: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = ", ".join(sorted(expected - actual)) or "none"
        extra = ", ".join(sorted(actual - expected)) or "none"
        raise DataFlowRegistryError(f"{where} fields differ; missing={missing}; extra={extra}")
