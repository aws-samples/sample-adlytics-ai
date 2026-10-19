# SPDX-License-Identifier: MIT-0
"""Producer S3 and status adapters against exact deployed dev resources, without AWS fakes."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from probe_identity import probe_key

from adapters.outbound.cloudformation_stack import stack_resource_summaries
from adapters.outbound.dynamodb.producer_status import ATTEMPT_ID, DynamoRegistrationStatus
from adapters.outbound.s3.producer_landing import S3ProducerLanding
from domain.producer.status import (
    FinalizedRegistration,
    RegistrationAttempt,
    RegistrationRejection,
    RegistrationState,
    RegistrationStatus,
)
from domain.shared_kernel.errors import InvariantError
from ports.outbound.producer_landing import ProducerLandingPort, RegistrationStatusPort

if TYPE_CHECKING:
    from types_boto3_cloudformation.client import CloudFormationClient

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-050")]
ENV_NAME = os.environ.get("ADL_TEST_ENV", "dev")


@dataclass(frozen=True, slots=True)
class ProducerResources:
    raw_bucket: str
    status_table: str


def _stack_resources(name: str) -> list[dict[str, Any]]:
    cloudformation: CloudFormationClient = boto3.client("cloudformation")
    return [dict(item) for item in stack_resource_summaries(cloudformation, name)]


@pytest.fixture(scope="module")
def producer_resources() -> ProducerResources:
    try:
        data = _stack_resources(f"Adlytics-DataV2-{ENV_NAME}")
        ingestion = _stack_resources(f"Adlytics-Ingestion-{ENV_NAME}")
    except Exception as error:  # noqa: BLE001 — pytest.skip raises: no dev stacks is a skip
        pytest.skip(f"producer integration needs deployed dev stacks: {type(error).__name__}")

    def physical(resources: list[dict[str, Any]], prefix: str, resource_type: str) -> str:
        matches = [
            str(item.get("PhysicalResourceId", ""))
            for item in resources
            if str(item.get("LogicalResourceId", "")).startswith(prefix)
            and item.get("ResourceType") == resource_type
        ]
        if len(matches) != 1 or not matches[0]:
            pytest.skip(f"deployed stack does not yet expose one {prefix} {resource_type}")
        return matches[0]

    return ProducerResources(
        raw_bucket=physical(data, "SemanticV2RawBucket", "AWS::S3::Bucket"),
        status_table=physical(
            ingestion,
            "RegistrationStatusTable",
            "AWS::DynamoDB::Table",
        ),
    )


def test_s3_adapter_reads_exact_versions_and_create_once_converges(
    request: pytest.FixtureRequest,
    producer_resources: ProducerResources,
) -> None:
    probe = probe_key(str(request.node.name))  # type: ignore[reportUnknownArgumentType]
    s3 = boto3.client("s3")
    landing = S3ProducerLanding(producer_resources.raw_bucket, s3)
    assert isinstance(landing, ProducerLandingPort)
    incoming_key = f"incoming/amc/integration_probe/{probe}/result.csv"
    governed_key = f"amc/integration_probe/{probe}/result.csv"
    created: list[tuple[str, str]] = []
    try:
        first = s3.put_object(
            Bucket=producer_resources.raw_bucket,
            Key=incoming_key,
            Body=b"first exact bytes",
        )
        first_version = str(first.get("VersionId", ""))
        assert first_version
        created.append((incoming_key, first_version))
        second = s3.put_object(
            Bucket=producer_resources.raw_bucket,
            Key=incoming_key,
            Body=b"later current bytes",
        )
        second_version = str(second.get("VersionId", ""))
        assert second_version and second_version != first_version
        created.append((incoming_key, second_version))

        assert landing.size(incoming_key, first_version) == len(b"first exact bytes")
        assert landing.body(incoming_key, first_version).body == b"first exact bytes"
        stored = landing.put(governed_key, b"canonical bytes", "text/csv")
        created.append((governed_key, stored.version_id))
        replay = landing.put(governed_key, b"canonical bytes", "text/csv")
        assert replay == stored
        with pytest.raises(InvariantError, match="different evidence"):
            landing.put(governed_key, b"conflicting bytes", "text/csv")
    finally:
        for key, version in created:
            s3.delete_object(
                Bucket=producer_resources.raw_bucket,
                Key=key,
                VersionId=version,
            )


def test_status_adapter_conditionally_moves_one_exact_attempt_to_terminal(
    request: pytest.FixtureRequest,
    producer_resources: ProducerResources,
) -> None:
    probe = probe_key(str(request.node.name))  # type: ignore[reportUnknownArgumentType]
    attempt = RegistrationAttempt(
        f"incoming/amc/integration_probe/{probe}/registration.json",
        f"{probe}-version",
    )
    store = DynamoRegistrationStatus(producer_resources.status_table)
    assert isinstance(store, RegistrationStatusPort)
    now = datetime.now(UTC).replace(microsecond=0)
    processing = RegistrationStatus.processing(attempt, at=now)
    finalized = RegistrationStatus.finalized(
        attempt,
        FinalizedRegistration(
            result_key=f"amc/integration_probe/{probe}/result.csv",
            result_version_id=f"{probe}-result-version",
            manifest_key=f"amc/integration_probe/{probe}/manifest.json",
            manifest_version_id=f"{probe}-manifest-version",
        ),
        at=now,
    )
    try:
        assert store.record(processing).accepted
        assert store.record(processing).accepted
        assert store.get(attempt) == processing
        assert store.record(finalized).accepted
        assert store.get(attempt) == finalized
        contradictory = RegistrationStatus.rejected(
            attempt,
            RegistrationRejection("LATE_REJECTION", "must not replace finalized evidence"),
            at=now,
        )
        refused = store.record(contradictory)
        assert not refused.accepted
        assert refused.status.state is RegistrationState.FINALIZED
    finally:
        boto3.client("dynamodb").delete_item(
            TableName=producer_resources.status_table,
            Key={ATTEMPT_ID: {"S": attempt.attempt_id}},
        )
