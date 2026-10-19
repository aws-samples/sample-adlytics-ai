# SPDX-License-Identifier: MIT-0
"""boto3 implementation of LogRetentionPort over CloudWatch Logs.

`CreateLogGroup` first, because the group may not exist yet: AgentCore creates a runtime's group
on its first write, and the retention must be in place before that write (infra-composition-stacks).
A group that already exists is the expected case on every update and is not an error. Then the
retention, then a `DescribeLogGroups` readback that must show it, then the tags, which an existing
group never received from `CreateLogGroup`. Every other SDK failure becomes a `DependencyError`
carrying the shared provider diagnostic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from botocore.exceptions import BotoCoreError, ClientError

from adapters.outbound.aws_errors import aws_failure_context
from domain.shared_kernel.errors import DependencyError, ErrorCode
from ports.outbound.log_retention import LogRetentionPort, RetainLogGroupRequest

if TYPE_CHECKING:  # pragma: no cover - typing only
    from types_boto3_logs.client import CloudWatchLogsClient

__all__ = ["LOG_RETENTION_FAILED", "CloudWatchLogRetention", "already_exists"]

LOG_RETENTION_FAILED: Final = ErrorCode("HARNESSCR", 6)
_ALREADY_EXISTS: Final = "ResourceAlreadyExistsException"


def already_exists(error: ClientError | BotoCoreError) -> bool:
    """Whether a CreateLogGroup failure only says the group is already there."""
    return (
        isinstance(error, ClientError)
        and error.response.get("Error", {}).get("Code") == _ALREADY_EXISTS
    )


class CloudWatchLogRetention(LogRetentionPort):
    """One typed `logs` client per process."""

    def __init__(self, client: CloudWatchLogsClient) -> None:
        self._client = client

    def retain(self, request: RetainLogGroupRequest) -> None:
        """Create the group when absent, put its retention, read it back, and tag it."""
        name = request.log_group_name
        try:
            self._client.create_log_group(logGroupName=name)
        except (ClientError, BotoCoreError) as error:
            if not already_exists(error):
                raise _failed(error, "CreateLogGroup") from error
        try:
            self._client.put_retention_policy(
                logGroupName=name, retentionInDays=request.retention_days
            )
            # A prefix also matches every longer name, so the exact group is sought on every page.
            pages = self._client.get_paginator("describe_log_groups").paginate(
                logGroupNamePrefix=name
            )
            groups = [item for page in pages for item in page["logGroups"]]
        except (ClientError, BotoCoreError) as error:
            raise _failed(error, "PutRetentionPolicy") from error
        group = next((item for item in groups if item.get("logGroupName") == name), None)
        if group is None or group.get("retentionInDays") != request.retention_days:
            raise DependencyError(
                LOG_RETENTION_FAILED,
                "The log group did not read back with the requested retention.",
                context={"operation": "DescribeLogGroups"},
            )
        arn = group.get("logGroupArn", "")
        if request.tags and arn:
            try:
                self._client.tag_resource(resourceArn=arn, tags=dict(request.tags))
            except (ClientError, BotoCoreError) as error:
                raise _failed(error, "TagResource") from error


def _failed(error: ClientError | BotoCoreError, operation: str) -> DependencyError:
    return DependencyError(
        LOG_RETENTION_FAILED,
        f"{operation} failed.",
        context={"operation": operation, **aws_failure_context(error)},
    )
