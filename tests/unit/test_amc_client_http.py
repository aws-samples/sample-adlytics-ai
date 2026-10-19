# SPDX-License-Identifier: MIT-0
"""No-network tests for AMC HTTP response and bounded-download handling."""

from __future__ import annotations

import json
import sys
from io import BytesIO
from pathlib import Path
from secrets import token_urlsafe
from types import TracebackType
from typing import Any, Self

import pytest

sys.path.insert(0, str(Path(__file__).parents[2]))
from validation import amc_client

pytestmark = pytest.mark.unit


class _Response:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self.content = body
        self.status_code = status
        self.raw = BytesIO(body)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback


class _Session:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append(("GET", url, kwargs))
        return self.response

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append(("POST", url, kwargs))
        return self.response


def test_fetch_csv_accepts_a_body_exactly_at_the_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(_Response(b"abc"))
    monkeypatch.setattr(amc_client, "_HTTP", session)

    assert amc_client.fetch_csv("https://results.example.test/file.csv", max_bytes=3) == "abc"
    assert session.calls == [
        (
            "GET",
            "https://results.example.test/file.csv",
            {"timeout": 60, "stream": True},
        )
    ]


def test_fetch_csv_reads_one_byte_past_the_limit_and_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _Response(b"abcde")
    monkeypatch.setattr(amc_client, "_HTTP", _Session(response))

    with pytest.raises(amc_client.AmcError, match="exceeds 3 bytes"):
        amc_client.fetch_csv("https://results.example.test/file.csv", max_bytes=3)

    assert response.raw.tell() == 4


def test_fetch_csv_preserves_http_status_and_diagnostic_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(amc_client, "_HTTP", _Session(_Response(b"denied\xff", status=403)))

    with pytest.raises(amc_client.AmcError) as caught:
        amc_client.fetch_csv("https://results.example.test/file.csv", max_bytes=20)

    assert caught.value.status == 403
    assert caught.value.body == "denied\ufffd"


def test_token_form_parses_json_response_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _Session(_Response(b'{"access_token":"token"}'))
    monkeypatch.setattr(amc_client, "_HTTP", session)
    credentials = amc_client.Credentials(
        client_id="client",
        # pragma: allowlist nextline secret (A-TEST-LITERALS-001)
        client_secret="secret",
        refresh_token="refresh",
        region="na",
    )

    assert amc_client.access_token(credentials) == "token"


def test_token_form_preserves_http_status_and_diagnostic_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(amc_client, "_HTTP", _Session(_Response(b"bad request", status=400)))
    credentials = amc_client.Credentials(
        client_id="client",
        # pragma: allowlist nextline secret (A-TEST-LITERALS-001)
        client_secret="secret",
        refresh_token="refresh",
        region="na",
    )

    with pytest.raises(amc_client.AmcError) as caught:
        amc_client.access_token(credentials)

    assert caught.value.status == 400
    assert caught.value.body == "bad request"


def test_token_shape_error_reports_field_names_without_credential_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response_credential = token_urlsafe(24)
    response_refresh = token_urlsafe(24)
    response = json.dumps(
        {
            "access_token": {"unexpected": response_credential},
            "refresh_token": response_refresh,
            "token_type": "bearer",
        }
    ).encode()
    monkeypatch.setattr(amc_client, "_HTTP", _Session(_Response(response)))
    credentials = amc_client.Credentials(
        client_id=token_urlsafe(12),
        client_secret=token_urlsafe(18),
        refresh_token=token_urlsafe(24),
        region="na",
    )

    with pytest.raises(amc_client.AmcError) as caught:
        amc_client.access_token(credentials)

    fields = caught.value.body.removeprefix("unexpected token response fields: ").split(", ")
    assert set(fields) == {
        "access_token",
        "refresh_token",
        "token_type",
    }
    assert response_credential not in str(caught.value)
    assert response_refresh not in str(caught.value)


def _adhoc_body(monkeypatch: pytest.MonkeyPatch, **extra: Any) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def token(_credentials: amc_client.Credentials) -> str:
        return "fixture-token"

    monkeypatch.setattr(amc_client, "access_token", token)
    api = amc_client.AdsApi(
        amc_client.Credentials(
            client_id="fixture-client", client_secret="fixture-secret",
            refresh_token="fixture-refresh", region="na",
            marketplace_id="fixture-mkt", amc_account_id="fixture-account",
        )
    )  # fmt: skip

    def fake_call(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        del method, path
        captured.update(kwargs["body"])
        return {"workflowExecutionId": "exec-fixture"}

    monkeypatch.setattr(api, "call", fake_call)
    api.execute_adhoc(
        instance_id="instance-fixture", workflow_id="wf-fixture", sql="SELECT 1",
        window_start="2026-01-05T00:00:00", window_end="2026-01-12T00:00:00", **extra,
    )  # fmt: skip
    return captured


def test_adhoc_body_without_parameters_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _adhoc_body(monkeypatch)
    assert "parameterValues" not in body
    assert "inputParameters" not in body["workflow"]
    assert set(body) == {
        "workflow", "timeWindowStart", "timeWindowEnd", "timeWindowType",
        "timeWindowTimeZone", "requireSyntheticData", "disableAggregationControls",
    }  # fmt: skip


def test_adhoc_body_declares_and_supplies_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _adhoc_body(monkeypatch, parameter_values={"probe_marker": "fixture-value"})
    assert body["parameterValues"] == {"probe_marker": "fixture-value"}
    declared = body["workflow"]["inputParameters"]
    assert [item["name"] for item in declared] == ["probe_marker"]
    assert declared[0]["dataType"] == "STRING"
    assert "defaultValue" not in declared[0]
    assert json.dumps(body)  # serialisable as sent
