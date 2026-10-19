# SPDX-License-Identifier: MIT-0
"""Resolve the live execution-graph table for an operator tool (S1 rule, shared from S6).

The table name comes from its exact SSM path, and the named store stack must own both the table
and the parameter, so a tool can never be pointed at a table by a typed-in name alone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from adapters.outbound.cloudformation_stack import stack_resource_summaries

if TYPE_CHECKING:
    import boto3

__all__ = ["resolve_graph_table", "table_parameter_path"]


def table_parameter_path(environment: str) -> str:
    """Return the exact SSM path the store stack publishes the live table name under."""
    return f"/adlytics/{environment}/operations/execution-graph-table-name"


def resolve_graph_table(session: boto3.Session, environment: str, store_stack: str) -> str:
    """Resolve the table through its SSM path and require the named stack to own it."""
    path = table_parameter_path(environment)
    value = str(session.client("ssm").get_parameter(Name=path)["Parameter"].get("Value", ""))
    pairs = {
        (str(item.get("ResourceType")), str(item.get("PhysicalResourceId")))
        for item in stack_resource_summaries(session.client("cloudformation"), store_stack)
    }
    owned = ("AWS::DynamoDB::Table", value) in pairs and ("AWS::SSM::Parameter", path) in pairs
    if not value or not owned:
        raise PermissionError("the named store stack does not own the execution-graph table.")
    return value
