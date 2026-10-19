# SPDX-License-Identifier: MIT-0
"""Every resource of one CloudFormation stack, read through all of ListStackResources' pages.

DescribeStackResources returns at most 100 resources and has no continuation token, so a larger
stack (ApiStack holds 144) is cut short without any signal. ListStackResources pages the whole
set. This module is the one reader of a stack's resources; `make check-aws-pagination` refuses
DescribeStackResources anywhere in `src/` or `validation/`.

The summaries carry `LogicalResourceId`, `PhysicalResourceId`, `ResourceType`, `ResourceStatus`
and `LastUpdatedTimestamp`, not the `StackName` or `Timestamp` DescribeStackResources returned.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from types_boto3_cloudformation.client import CloudFormationClient
    from types_boto3_cloudformation.type_defs import StackResourceSummaryTypeDef

__all__ = ["stack_resource_summaries"]


def stack_resource_summaries(
    cloudformation: CloudFormationClient, stack_name: str
) -> tuple[StackResourceSummaryTypeDef, ...]:
    """Return every resource summary of the named stack, in page order.

    Nothing is caught: a failed page fails the read, so a caller never sees part of a stack.
    """
    paginator = cloudformation.get_paginator("list_stack_resources")
    return tuple(
        item
        for page in paginator.paginate(StackName=stack_name)
        # The service model marks the list optional, so a page may omit it when it is empty.
        for item in page.get("StackResourceSummaries", [])
    )
