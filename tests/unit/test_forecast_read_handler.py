# SPDX-License-Identifier: MIT-0
"""The demand forecast read routes' HTTP boundary (ADR-163).

A route that answers without a subject would serve a signed-out caller; a malformed query that
reaches the store wastes reads and can return the wrong page; a failure without its coded body
reaches the page as an untyped 500 with no Try again. The service runs over the shared fake
reader; the handler module is loaded with fictional configuration and makes no AWS call.
"""

from __future__ import annotations

import importlib
import json
import logging
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ReadTimeoutError
from forecast_read_support import VALUES, FakeReader, fake_reader
from forecast_series_support import policy
from test_config import VALID

from adapters.inbound.forecast_read.service import ForecastReadService

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-163")]

LIST = "GET /api/demand-forecast"
STATUS = "GET /api/demand-forecast/status"


class _Context(SimpleNamespace):
    def get_remaining_time_in_millis(self) -> int:
        return int(self.remaining_ms)


def _context(remaining_ms: int = 10_000) -> _Context:
    return _Context(
        function_name="adlytics-test-DemandForecastReadFunction",
        memory_limit_in_mb=1024,
        invoked_function_arn="arn:aws:lambda:us-east-1:000000000000:function:test",
        aws_request_id="request",
        remaining_ms=remaining_ms,
    )


@pytest.fixture
def module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    for name, value in {
        **VALID,
        "ADL_DEMAND_FORECAST_TABLE": "adlytics-test-demand-forecast",
        "ADL_DEMAND_FORECAST_MARKETPLACE_KEY": "fictional-marketplace",
        "ADL_DEMAND_FORECAST_CHECK_SCHEDULE": "cron(0 13 * * ? *)",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(name, value)
    from adapters import config  # noqa: PLC0415

    config.settings_without_client_secret.cache_clear()
    from adapters.inbound.forecast_read import handler  # noqa: PLC0415

    return importlib.reload(handler)


def _serve(  # noqa: PLR0913 -- one request with keyword overrides
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    *,
    query: dict[str, str] | None = None,
    reader: FakeReader | None = None,
    subject: bool = True,
) -> tuple[int, dict[str, Any], FakeReader]:
    used = reader or fake_reader()
    monkeypatch.setattr(module, "SERVICE", ForecastReadService(used, VALUES, policy()))
    authorizer = {"lambda": {"subject": "subject-1", "correlation_id": "c" * 32}}
    event = {
        "routeKey": route,
        "rawPath": route.split(" ", 1)[1],
        "requestContext": {
            "http": {"method": "GET"},
            "authorizer": authorizer if subject else {},
        },
        "queryStringParameters": query,
    }
    response = module.handler(event, _context())
    headers = response["headers"]
    assert (headers["content-type"], headers["cache-control"]) == ("application/json", "no-store")
    return int(response["statusCode"]), json.loads(response["body"]), used


def test_the_list_serves_page_one_and_reads_two_items(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, body, reader = _serve(module, monkeypatch, LIST)
    assert (status, body["outcome"], body["page"], body["match_count"]) == (200, "complete", 1, 3)
    assert [kind for kind, _ in reader.calls] == ["current", "meta"]


def test_an_asin_serves_its_detail_upper_cased(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, body, _ = _serve(module, monkeypatch, LIST, query={"asin": "b0fixtur01"})
    assert (status, body["outcome"], body["asin"]) == (200, "selected", "B0FIXTUR01")


def test_status_serves_the_last_forecast_run_and_next_checks(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, body, reader = _serve(module, monkeypatch, STATUS)
    assert (status, body["hold"], body["last_run"]["outcome"]) == (200, "none", "published")
    assert len(reader.calls) == 1


@pytest.mark.parametrize(
    "case",
    [
        (LIST, {"q": "B0FIXTUR01"}, "QUERY_INVALID"),
        (LIST, {"asin": "B0FIXTUR01", "page": "2"}, "QUERY_INVALID"),
        (STATUS, {"page": "1"}, "QUERY_INVALID"),
        (LIST, {"asin": "B0FIXTUR0"}, "ASIN_INVALID"),
        (LIST, {"status": "all"}, "STATUS_INVALID"),
        (LIST, {"page": "01"}, "PAGE_INVALID"),
    ],
)
def test_a_malformed_query_is_refused_400_before_any_store_read(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, case: tuple[str, dict[str, str], str]
) -> None:
    route, query, refused = case
    status, body, reader = _serve(module, monkeypatch, route, query=query)
    assert (status, body["refused"]) == (400, refused)
    assert body["code"].startswith("ADLY-FORECAST_READ-")
    assert reader.calls == []


def test_a_request_without_a_subject_is_refused_403_before_any_read(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, body, reader = _serve(module, monkeypatch, LIST, subject=False)
    assert (status, body["refused"], body["code"]) == (
        403,
        "IDENTITY_REQUIRED",
        "ADLY-FORECAST_READ-0012",
    )
    assert reader.calls == []


def test_a_route_this_function_does_not_serve_is_404(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, body, _ = _serve(module, monkeypatch, "GET /api/demand-forecast/other")
    assert (status, body["refused"]) == (404, "ROUTE_UNKNOWN")


def test_an_event_without_a_route_key_is_a_coded_404(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "SERVICE", ForecastReadService(fake_reader(), VALUES, policy()))
    event = {"rawPath": "/api/demand-forecast", "requestContext": {"http": {"method": "GET"}}}
    response = module.handler(event, _context())
    body = json.loads(response["body"])
    assert (int(response["statusCode"]), body["refused"]) == (404, "ROUTE_UNKNOWN")


def test_a_read_timeout_is_503_store_unavailable(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = fake_reader()
    reader.fail = ReadTimeoutError(endpoint_url="https://dynamodb")
    status, body, _ = _serve(module, monkeypatch, LIST, query={"asin": "B0FIXTUR01"}, reader=reader)
    assert (status, body["refused"], body["code"]) == (
        503,
        "FORECAST_STORE_UNAVAILABLE",
        "ADLY-FORECAST_READ-0009",
    )


def test_a_missing_meta_is_503_store_incomplete(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = fake_reader()
    reader.meta = None
    status, body, _ = _serve(module, monkeypatch, LIST, reader=reader)
    assert (status, body["refused"]) == (503, "FORECAST_STORE_INCOMPLETE")


def test_an_unexpected_error_is_500_with_only_its_class_name_logged(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    reader = fake_reader()
    reader.fail = RuntimeError("provider text B0FIXTUR01")
    with caplog.at_level(logging.INFO):
        status, body, _ = _serve(
            module, monkeypatch, LIST, query={"asin": "B0FIXTUR01"}, reader=reader
        )
    assert (status, body["refused"], body["code"]) == (
        500,
        "FORECAST_READ_FAILED",
        "ADLY-FORECAST_READ-0011",
    )
    assert body["detail"] == "The request could not be completed. The issue has been recorded."
    assert "provider text" not in caplog.text
    assert "B0FIXTUR01" not in json.dumps(body)


def test_the_deadline_is_the_remaining_time_less_the_response_reserve(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = fake_reader()
    monkeypatch.setattr(module, "SERVICE", ForecastReadService(reader, VALUES, policy()))
    event = {
        "routeKey": STATUS,
        "rawPath": "/api/demand-forecast/status",
        "requestContext": {
            "http": {"method": "GET"},
            "authorizer": {"lambda": {"subject": "s", "correlation_id": "c" * 32}},
        },
    }
    response = module.handler(event, _context(remaining_ms=900))
    body = json.loads(response["body"])
    assert (response["statusCode"], body["refused"]) == (503, "FORECAST_STORE_UNAVAILABLE")
    assert reader.calls == []
