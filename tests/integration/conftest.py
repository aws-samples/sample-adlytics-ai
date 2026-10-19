# SPDX-License-Identifier: MIT-0
"""Fixtures for the integration tier.

Every test here talks to a real deployed resource, and skips rather than fails
when there is nothing deployed — so `make verify` stays credential-free and the
unit loop never depends on a stack. See ADR-014.

Configuration is read from the **SSM parameters the stacks publish**, not from the
environment. That distinction matters: the `ADL_*` variables exist only inside a
Lambda, so a harness reading them would skip forever no matter what was deployed.
The parameters are the published interface (ADR-012), and reading them is also a
real assertion — a missing one means the stack that owns it has not been deployed.

Run with `make test-integration PROFILE=<aws-profile>`.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import boto3
import pytest
from probe_identity import PROBE_PREFIX

from adapters.config import Settings, load_settings
from adapters.outbound.dynamodb.page_context import LOOKUP_KEY as PAGE_CONTEXT_LOOKUP_KEY
from adapters.outbound.dynamodb.preset_artifacts import (
    PARTITION_KEY as POINTER_PARTITION_KEY,
)
from adapters.outbound.dynamodb.preset_artifacts import (
    RECORD_KEY as POINTER_RECORD_KEY,
)
from adapters.outbound.dynamodb.semantic_state import (
    ATTEMPT_EVENT_KEY,
    ATTEMPT_KEY,
    PUBLICATION_RECORD_KEY,
)
from adapters.outbound.dynamodb.semantic_state import (
    PARTITION_KEY as SEMANTIC_PARTITION_KEY,
)
from domain.shared_kernel.errors import InvariantError

ENV_NAME = os.environ.get("ADL_TEST_ENV", "dev")


# Not published by any stack: a test harness value, and nothing here asserts on it.
REFRESH_MAX_AGE_FOR_TESTS = "2592000"


def _ssm() -> Any:
    """Build a client for the profile the Makefile supplied.

    boto3 picks up AWS_PROFILE, which `make test-integration` sets from the
    required PROFILE parameter. No profile name is written down anywhere.
    """
    return boto3.client("ssm")


def _read_parameters() -> dict[str, str]:
    """Fetch every parameter the deployed stacks publish, or return empty.

    Empty means "nothing deployed", which is a skip. A partially deployed
    environment surfaces as a missing key and therefore also a skip, naming what
    was absent — the stacks deploy in a fixed order, so a gap is informative.
    """
    paths = {
        "user_pool_id": f"/adlytics/{ENV_NAME}/auth/user-pool-id",
        "client_id": f"/adlytics/{ENV_NAME}/auth/user-pool-client-id",
        "client_secret_name": f"/adlytics/{ENV_NAME}/auth/user-pool-client-secret-name",
        "login_domain": f"/adlytics/{ENV_NAME}/auth/domain",
        "public_base_url": f"/adlytics/{ENV_NAME}/frontend/public-base-url",
        "page_context_table": f"/adlytics/{ENV_NAME}/agent/page-context-table",
        "semantic_attempts_table": f"/adlytics/{ENV_NAME}/data/v2/semantic-attempts-table",
        "semantic_leases_table": f"/adlytics/{ENV_NAME}/data/v2/semantic-leases-table",
        "semantic_publications_table": f"/adlytics/{ENV_NAME}/data/v2/semantic-publications-table",
        "semantic_v2_bucket": f"/adlytics/{ENV_NAME}/data/v2/bucket",
        "semantic_v2_workgroup_interactive": (
            f"/adlytics/{ENV_NAME}/data/v2/athena-workgroup-interactive"
        ),
        "semantic_v2_workgroup_bake": f"/adlytics/{ENV_NAME}/data/v2/athena-workgroup-bake",
        "semantic_v2_source_database": f"/adlytics/{ENV_NAME}/data/v2/source-aligned-glue-database",
        "semantic_v2_candidate_database": (
            f"/adlytics/{ENV_NAME}/data/v2/canonical-candidate-glue-database"
        ),
        "semantic_v2_current_database": (
            f"/adlytics/{ENV_NAME}/data/v2/canonical-current-glue-database"
        ),
        "preset_artifact_pointer_table": (
            f"/adlytics/{ENV_NAME}/publisher/artifact-pointer-table-name"
        ),
        "preset_artifact_bucket": f"/adlytics/{ENV_NAME}/publisher/artifact-bucket-name",
    }
    found: dict[str, str] = {}
    client = _ssm()
    for key, path in paths.items():
        try:
            found[key] = str(client.get_parameter(Name=path)["Parameter"]["Value"])
        except Exception:  # noqa: BLE001, S112 — an absent parameter is the signal, not an error
            continue
    return found


@pytest.fixture(scope="session")
def deployed_env() -> dict[str, str]:
    """Build the environment a deployed container holds, or skip naming what is missing.

    Separate from `settings` so a test can vary one key and still exercise the real
    `load_settings` — reconstructing this mapping inside a test would duplicate it
    and then drift from it. Note it carries `CLIENT_SECRET_ID` and no literal
    secret, which is what makes loading it reach Secrets Manager.

    Skipping on absent configuration is correct *here* and nowhere else: it means
    "not pointed at an environment", not "misconfigured". Inside a container the
    same condition must fail — see adapters.config.settings.
    """
    try:
        parameters = _read_parameters()
    except Exception as exc:  # noqa: BLE001 — pytest.skip raises: no credentials is a skip
        pytest.skip(f"integration tier needs AWS credentials: {type(exc).__name__}")

    missing = {
        "user_pool_id",
        "client_id",
        "client_secret_name",
        "login_domain",
        "public_base_url",
        "semantic_attempts_table",
        "semantic_leases_table",
        "semantic_publications_table",
        "semantic_v2_bucket",
        "semantic_v2_workgroup_interactive",
        "semantic_v2_workgroup_bake",
        "semantic_v2_source_database",
        "semantic_v2_candidate_database",
        "semantic_v2_current_database",
    } - set(parameters)
    if missing:
        pytest.skip(
            f"integration tier needs a deployed {ENV_NAME} environment; missing: {sorted(missing)}"
        )

    return {
        "ADL_ENVIRONMENT": ENV_NAME,
        "ADL_AWS_REGION": str(_ssm().meta.region_name),
        "ADL_COGNITO_USER_POOL_ID": parameters["user_pool_id"],
        "ADL_COGNITO_CLIENT_ID": parameters["client_id"],
        "ADL_COGNITO_CLIENT_SECRET_ID": parameters["client_secret_name"],
        "ADL_COGNITO_LOGIN_DOMAIN": parameters["login_domain"],
        # One origin: CloudFront serves the app and the API alike.
        "ADL_APP_BASE_URL": parameters["public_base_url"],
        "ADL_API_BASE_URL": parameters["public_base_url"],
        "ADL_COOKIE_SCOPE": "host_only",
        "ADL_REFRESH_COOKIE_MAX_AGE_SECONDS": REFRESH_MAX_AGE_FOR_TESTS,
        # Read from the V2 SSM contracts the stacks publish. This is the same interface deployed
        # consumers use and makes a missing V2 workgroup/database a named integration prerequisite.
        "ADL_ATHENA_WORKGROUP": parameters["semantic_v2_workgroup_interactive"],
        **(
            {"ADL_PAGE_CONTEXT_TABLE": parameters["page_context_table"]}
            if "page_context_table" in parameters
            else {}
        ),
        **(
            {"ADL_PRESET_ARTIFACT_POINTER_TABLE": parameters["preset_artifact_pointer_table"]}
            if "preset_artifact_pointer_table" in parameters
            else {}
        ),
        **(
            {"ADL_PRESET_ARTIFACT_BUCKET": parameters["preset_artifact_bucket"]}
            if "preset_artifact_bucket" in parameters
            else {}
        ),
        "ADL_SEMANTIC_ATTEMPTS_TABLE": parameters["semantic_attempts_table"],
        "ADL_SEMANTIC_LEASES_TABLE": parameters["semantic_leases_table"],
        "ADL_SEMANTIC_PUBLICATIONS_TABLE": parameters["semantic_publications_table"],
        "ADL_SEMANTIC_V2_BUCKET": parameters["semantic_v2_bucket"],
        "ADL_SEMANTIC_V2_ATHENA_BAKE_WORKGROUP": parameters["semantic_v2_workgroup_bake"],
        "ADL_SEMANTIC_V2_SOURCE_DATABASE": parameters["semantic_v2_source_database"],
        "ADL_SEMANTIC_V2_CANDIDATE_DATABASE": parameters["semantic_v2_candidate_database"],
        "ADL_SEMANTIC_V2_CURRENT_DATABASE": parameters["semantic_v2_current_database"],
        "ADL_ATHENA_RESULT_REUSE_MAX_AGE_MINUTES": "0",
        "ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY": "8",
    }


@pytest.fixture(scope="session")
def settings(deployed_env: dict[str, str]) -> Settings:
    """Load the deployed environment, resolving the client secret for real."""
    try:
        return load_settings(deployed_env)
    except InvariantError as exc:
        pytest.skip(f"deployed parameters did not produce usable settings: {exc.context}")


@dataclass(frozen=True, slots=True)
class _Store:
    """One probe-prefixed hash key and its optional complete DynamoDB key shape."""

    table: str
    key: str
    prefix: str
    sort_key: str | None = None


def _sweep(client: Any, store: _Store) -> int:
    """Delete probe-prefixed records using every element of each table's primary key."""
    removed = 0
    names = {"#hash": store.key}
    projection = "#hash"
    if store.sort_key is not None:
        names["#range"] = store.sort_key
        projection += ", #range"
    paginator = client.get_paginator("scan")
    pages = paginator.paginate(
        TableName=store.table,
        ProjectionExpression=projection,
        ExpressionAttributeNames=names,
    )
    for page in pages:
        for item in page.get("Items", []):
            value = str(item.get(store.key, {}).get("S", ""))
            if not value.startswith(store.prefix):
                continue
            key = {store.key: item[store.key]}
            if store.sort_key is not None:
                key[store.sort_key] = item[store.sort_key]
            client.delete_item(TableName=store.table, Key=key)
            removed += 1
    return removed


@pytest.fixture(scope="session", autouse=True)
def cleared_probe_records(deployed_env: dict[str, str]) -> Iterator[None]:
    """Remove probe-prefixed records from every configured real test store."""
    client = boto3.client("dynamodb")
    stores = [
        _Store(
            deployed_env["ADL_SEMANTIC_ATTEMPTS_TABLE"],
            ATTEMPT_KEY,
            PROBE_PREFIX,
            ATTEMPT_EVENT_KEY,
        ),
        _Store(
            deployed_env["ADL_SEMANTIC_LEASES_TABLE"],
            SEMANTIC_PARTITION_KEY,
            PROBE_PREFIX,
        ),
        _Store(
            deployed_env["ADL_SEMANTIC_PUBLICATIONS_TABLE"],
            SEMANTIC_PARTITION_KEY,
            PROBE_PREFIX,
            PUBLICATION_RECORD_KEY,
        ),
        # S6 §2.6 (AGG4-19): window generation pointers of probe-prefixed conflict units.
        _Store(
            deployed_env["ADL_SEMANTIC_PUBLICATIONS_TABLE"],
            SEMANTIC_PARTITION_KEY,
            f"GENERATION#{PROBE_PREFIX}",
            PUBLICATION_RECORD_KEY,
        ),
    ]
    pointer_table = deployed_env.get("ADL_PRESET_ARTIFACT_POINTER_TABLE")
    if pointer_table:
        stores.append(
            _Store(
                pointer_table,
                POINTER_PARTITION_KEY,
                PROBE_PREFIX,
                POINTER_RECORD_KEY,
            )
        )
    page_context_table = deployed_env.get("ADL_PAGE_CONTEXT_TABLE")
    if page_context_table:
        stores.extend(
            (
                _Store(
                    page_context_table,
                    PAGE_CONTEXT_LOOKUP_KEY,
                    f"page#{PROBE_PREFIX}",
                ),
                _Store(
                    page_context_table,
                    PAGE_CONTEXT_LOOKUP_KEY,
                    f"turn#{PROBE_PREFIX}",
                ),
                _Store(
                    page_context_table,
                    PAGE_CONTEXT_LOOKUP_KEY,
                    f"selection#{PROBE_PREFIX}",
                ),
                _Store(
                    page_context_table,
                    PAGE_CONTEXT_LOOKUP_KEY,
                    f"artifact#{PROBE_PREFIX}",
                ),
                _Store(
                    page_context_table,
                    PAGE_CONTEXT_LOOKUP_KEY,
                    f"campaign-capability#{PROBE_PREFIX}",
                ),
                _Store(
                    page_context_table,
                    PAGE_CONTEXT_LOOKUP_KEY,
                    f"channel-capability#{PROBE_PREFIX}",
                ),
            )
        )

    for store in stores:
        _sweep(client, store)
    yield
    removed = [_sweep(client, store) for store in stores]
    if any(removed):
        print(  # noqa: T201 — teardown reporting; pytest captures it under -s
            "\nswept probe records from attempts, leases, and publications: "
            + ", ".join(str(count) for count in removed)
        )
