# SPDX-License-Identifier: MIT-0
"""The run-request API boundary: the requester group is checked server-side before admission.

The authorizer admits these routes only for the projected `operations_run_request` level; the
function re-reads that entitlement, so a caller without it gets 403 with no read of the store. A
pre-domain refusal gets 400 with its signal and no receipt.
"""

from __future__ import annotations

import importlib
import json
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-007")]

CONTEXT = SimpleNamespace(
    function_name="adlytics-test-RunRequestsFunction",
    memory_limit_in_mb=512,
    invoked_function_arn="arn:aws:lambda:us-east-1:000000000000:function:test",
    aws_request_id="request",
)


@pytest.fixture
def module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    for name, value in {
        "ADL_EXECUTION_GRAPH_TABLE": "graph",
        "ADL_RAW_BUCKET": "raw",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(name, value)
    from adapters import config  # noqa: PLC0415

    config.run_request_api_settings.cache_clear()
    from adapters.inbound.run_requests import handler  # noqa: PLC0415

    return importlib.reload(handler)


def _event(
    route: str, body: dict[str, Any] | None = None, *, entitled: bool = True, **path: str
) -> dict[str, Any]:
    return {
        "routeKey": route,
        "rawPath": route.split(" ", 1)[1],
        "requestContext": {
            "http": {"method": route.split(" ", 1)[0]},
            "authorizer": {
                "lambda": {
                    "subject": "subject-1",
                    "correlation_id": "c" * 32,
                    "can_request_runs": "true" if entitled else "false",
                }
            },
        },
        "body": None if body is None else json.dumps(body),
        "pathParameters": path or None,
    }


def test_a_caller_outside_the_requester_group_is_refused_before_any_store_read(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[str] = []
    monkeypatch.setattr(module, "_STORE", SimpleNamespace(read_receipt=reads.append))
    response = module.handler(
        _event("GET /api/operations/run-requests/{request_id}", entitled=False, request_id="r"),
        CONTEXT,
    )
    assert response["statusCode"] == 403
    assert json.loads(response["body"]) == {"refused": "RUN_REQUESTER_GROUP_REQUIRED"}
    assert reads == []


def test_a_group_member_with_a_non_v1_request_gets_400_and_no_receipt(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = {
        "request_id": "request-0001",
        "protocol_version": "v2",
        "reason": "operator_rerun",
        "manifest_key": "k",
        "version_id": "v",
    }

    def refuse(*_args: object) -> object:
        raise module.GraphCommandRefusedError("PROTOCOL_MIGRATION_UNSUPPORTED", "refused")

    monkeypatch.setattr(module, "admit_run_request", refuse)
    response = module.handler(_event("POST /api/operations/run-requests", body), CONTEXT)
    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"refused": "PROTOCOL_MIGRATION_UNSUPPORTED"}


def test_an_unknown_body_field_is_a_schema_refusal(module: ModuleType) -> None:
    response = module.handler(
        _event("POST /api/operations/run-requests", {"request_id": "r", "caller_kind": "schedule"}),
        CONTEXT,
    )
    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"refused": "SCHEMA_INVALID"}


def test_a_group_member_reads_a_receipt(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:

    def absent(_request_id: str) -> None:
        return None

    monkeypatch.setattr(module, "_STORE", SimpleNamespace(read_receipt=absent))
    response = module.handler(
        _event("GET /api/operations/run-requests/{request_id}", request_id="r"), CONTEXT
    )
    assert response["statusCode"] == 404


def test_the_routes_require_the_run_request_level_projected_from_the_group() -> None:
    from adapters.jobs.quality_review_token.handler import handler as pre_token  # noqa: PLC0415
    from domain.identity.authorization import required_route_level  # noqa: PLC0415
    from domain.identity.session import ReviewAccessLevel  # noqa: PLC0415

    for route in (
        "POST /api/operations/run-requests",
        "GET /api/operations/run-requests/{request_id}",
    ):
        assert required_route_level(route) is ReviewAccessLevel.OPERATIONS_RUN_REQUEST
    event: dict[str, Any] = {
        "request": {"groupConfiguration": {"groupsToOverride": ["operations-run-requesters"]}}
    }
    claims = pre_token(event, None)["response"]["claimsAndScopeOverrideDetails"]
    assert claims["accessTokenGeneration"]["claimsToAddOrOverride"] == {
        "quality_review_levels": ["operations_run_request"]
    }
    status_only: dict[str, Any] = {
        "request": {"groupConfiguration": {"groupsToOverride": ["operations-status-read"]}}
    }
    other = pre_token(status_only, None)["response"]["claimsAndScopeOverrideDetails"]
    assert (
        "operations_run_request"
        not in other["accessTokenGeneration"]["claimsToAddOrOverride"]["quality_review_levels"]
    )
