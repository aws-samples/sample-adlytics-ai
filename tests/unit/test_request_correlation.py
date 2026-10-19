# SPDX-License-Identifier: MIT-0
"""One server-minted correlation identifier per request, returned in its response (ADR-081 §2)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from adapters.inbound.correlation import (
    current_correlation_id,
    request_correlation,
    returns_correlation,
    with_correlation_header,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-081")]

AUTHORIZER_VALUE = "0f8e7d6c-5b4a-4c3d-8e2f-1a0b9c8d7e6f"
CLIENT_VALUE = "123e4567-e89b-42d3-a456-426614174000"


def _authorized_event(correlation_id: object) -> dict[str, Any]:
    return {
        "headers": {"x-correlation-id": CLIENT_VALUE},
        "requestContext": {"authorizer": {"lambda": {"correlation_id": correlation_id}}},
    }


class _Logger:
    """Records what the decorator stamps on the handler's log lines."""

    def __init__(self) -> None:
        self.values: list[str | None] = []

    def set_correlation_id(self, value: str | None) -> None:
        self.values.append(value)


def _is_minted(value: str) -> bool:
    parsed = uuid.UUID(value)
    return str(parsed) == value and parsed.version == 4


def test_an_authorized_route_uses_the_authorizer_value() -> None:
    assert request_correlation(_authorized_event(AUTHORIZER_VALUE)) == AUTHORIZER_VALUE


@pytest.mark.parametrize(
    "event",
    [
        {"headers": {"x-correlation-id": CLIENT_VALUE}},
        _authorized_event("not-a-canonical-uuid"),
        _authorized_event(None),
    ],
    ids=["client-header-only", "non-canonical-authorizer-value", "missing-authorizer-value"],
)
def test_every_other_request_is_minted_and_never_takes_the_client_value(
    event: dict[str, Any],
) -> None:
    minted = request_correlation(event)
    assert minted != CLIENT_VALUE
    assert _is_minted(minted)
    assert request_correlation(event) != minted


def test_the_header_is_added_without_disturbing_the_response() -> None:
    response = {"statusCode": 200, "headers": {"content-type": "application/json"}, "body": "{}"}
    returned = with_correlation_header(response, AUTHORIZER_VALUE)
    assert returned == {
        "statusCode": 200,
        "headers": {"content-type": "application/json", "x-correlation-id": AUTHORIZER_VALUE},
        "body": "{}",
    }
    assert response["headers"] == {"content-type": "application/json"}
    assert with_correlation_header({"statusCode": 204}, AUTHORIZER_VALUE) == {
        "statusCode": 204,
        "headers": {"x-correlation-id": AUTHORIZER_VALUE},
    }


def test_the_wrapped_handler_reads_logs_and_returns_the_same_one_value() -> None:
    seen: list[str] = []
    logger = _Logger()

    @returns_correlation(logger)
    def handler(_event: dict[str, Any], _context: object) -> dict[str, Any]:
        seen.append(current_correlation_id())
        return {"statusCode": 200, "headers": {"x-correlation-id": CLIENT_VALUE}}

    authorized = handler(_authorized_event(AUTHORIZER_VALUE), object())
    minted = handler({"headers": {"x-correlation-id": CLIENT_VALUE}}, object())

    assert seen[0] == authorized["headers"]["x-correlation-id"] == AUTHORIZER_VALUE
    assert seen[1] == minted["headers"]["x-correlation-id"]
    assert seen[1] != CLIENT_VALUE
    assert current_correlation_id() == ""
    # Every line the handler logs carries the value, and it is cleared between requests.
    assert logger.values == [AUTHORIZER_VALUE, None, seen[1], None]


def test_the_value_is_cleared_when_the_handler_raises() -> None:
    logger = _Logger()

    @returns_correlation(logger)
    def handler(_event: dict[str, Any], _context: object) -> dict[str, Any]:
        raise RuntimeError(current_correlation_id())

    with pytest.raises(RuntimeError, match=AUTHORIZER_VALUE):
        handler(_authorized_event(AUTHORIZER_VALUE), object())
    assert current_correlation_id() == ""
    assert logger.values == [AUTHORIZER_VALUE, None]
