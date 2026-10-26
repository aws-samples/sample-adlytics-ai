# SPDX-License-Identifier: MIT-0
"""Verify explicit reprocess resources against named CloudFormation and SSM ownership."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from adapters.outbound.cloudformation_stack import stack_resource_summaries
from domain.shared_kernel.errors import DependencyError, ErrorCode

if TYPE_CHECKING:  # pragma: no cover
    from types_boto3_cloudformation.client import CloudFormationClient
    from types_boto3_ssm.client import SSMClient

__all__ = [
    "NamedRawResourceRequest",
    "resolve_named_semantic_resources",
    "verify_named_raw_bucket",
]

RESOURCE_LOOKUP_FAILED = ErrorCode("REPROCESS_RESOURCE", 1)


@dataclass(frozen=True, slots=True)
class NamedRawResourceRequest:
    """Explicit environment, stack, bucket, and clients used for ownership verification."""

    environment: str
    stack_name: str
    bucket: str
    cloudformation: CloudFormationClient
    ssm: SSMClient

    def __post_init__(self) -> None:
        expected = f"Adlytics-DataV2-{self.environment}"
        if self.stack_name != expected:
            raise ValueError(f"raw owner stack must be exactly {expected}.")
        if not self.bucket.strip():
            raise ValueError("raw bucket must be explicit.")


def verify_named_raw_bucket(request: NamedRawResourceRequest) -> None:
    """Require exact SSM and named-stack agreement without enumerating account resources."""
    path = f"/adlytics/{request.environment}/data/v2/raw-bucket"
    try:
        resources = stack_resource_summaries(request.cloudformation, request.stack_name)
        parameter = request.ssm.get_parameter(Name=path)["Parameter"]
    except Exception as error:
        raise DependencyError(
            RESOURCE_LOOKUP_FAILED,
            "The named raw resource ownership could not be verified.",
            context={"cause": type(error).__name__, "stack": request.stack_name},
        ) from None
    value = str(parameter.get("Value", ""))
    if value != request.bucket:
        raise ValueError("explicit raw bucket differs from the exact SSM parameter value.")
    bucket_owned = any(
        item.get("ResourceType") == "AWS::S3::Bucket"
        and item.get("PhysicalResourceId") == request.bucket
        for item in resources
    )
    parameter_owned = any(
        item.get("ResourceType") == "AWS::SSM::Parameter" and item.get("PhysicalResourceId") == path
        for item in resources
    )
    if not bucket_owned or not parameter_owned:
        raise ValueError("named DataV2 stack does not own the explicit V2 raw bucket and SSM path.")


def resolve_named_semantic_resources(
    environment: str,
    cloudformation: CloudFormationClient,
    ssm: SSMClient,
) -> dict[str, str]:
    """Resolve V2 bucket/state values by exact paths and verify their named stack ownership."""
    paths = {
        "bucket": f"/adlytics/{environment}/data/v2/bucket",
        "bake_workgroup": f"/adlytics/{environment}/data/v2/athena-workgroup-bake",
        "source_database": f"/adlytics/{environment}/data/v2/source-aligned-glue-database",
        "candidate_database": f"/adlytics/{environment}/data/v2/canonical-candidate-glue-database",
        "current_database": f"/adlytics/{environment}/data/v2/canonical-current-glue-database",
        "staging_database": f"/adlytics/{environment}/data/v2/staging-glue-database",
        "staging_bucket": f"/adlytics/{environment}/data/v2/staging-bucket",
        "attempts": f"/adlytics/{environment}/data/v2/semantic-attempts-table",
        "leases": f"/adlytics/{environment}/data/v2/semantic-leases-table",
        "publications": f"/adlytics/{environment}/data/v2/semantic-publications-table",
    }
    try:
        values = {
            key: str(ssm.get_parameter(Name=path)["Parameter"].get("Value", ""))
            for key, path in paths.items()
        }
        v2_resources = stack_resource_summaries(cloudformation, f"Adlytics-DataV2-{environment}")
    except Exception as error:
        raise DependencyError(
            RESOURCE_LOOKUP_FAILED,
            "Named Semantic V2 resources could not be resolved.",
            context={"cause": type(error).__name__, "environment": environment},
        ) from None
    if any(not value for value in values.values()):
        raise ValueError("a named Semantic V2 resource parameter resolved to nothing.")
    owned = {
        str(item.get("PhysicalResourceId", ""))
        for item in v2_resources
        if item.get("ResourceType")
        in {
            "AWS::S3::Bucket",
            "AWS::Athena::WorkGroup",
            "AWS::Glue::Database",
            "AWS::DynamoDB::Table",
            "AWS::SSM::Parameter",
        }
    }
    required = set(values.values()) | set(paths.values())
    if not required <= owned:
        raise ValueError("named DataV2 stack does not own every resolved semantic resource.")
    return values
