# SPDX-License-Identifier: MIT-0
"""The Gateway REQUEST interceptor: bearer forwarding, reserved-argument stripping, and redaction.

Pure transformation of a documented input shape, so it belongs in the unit tier: no Gateway and
no AWS call is involved, and the output shape is what the Gateway acts on.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any

import pytest
from aws_lambda_powertools import Logger
from fictional_tokens import fictional_jwt

import adapters.inbound.gateway_interceptor.handler as interceptor
from adapters.inbound.gateway_interceptor.handler import BAD_INTERCEPTOR_INPUT, handler, intercept
from adapters.outbound.telemetry.reporting import OperationLog
from domain.shared_kernel.errors import ValidationError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-077")]

BEARER = "Bearer " + fictional_jwt("header-secret-claims", signature="signature-secret")
ARGUMENT_VALUE = "argument-value-that-must-never-be-logged"
RESERVED_VALUE = "model-supplied-assertion-that-must-never-be-logged"


def _context() -> object:
    return SimpleNamespace(
        function_name="adlytics-dev-GatewayInterceptor",
        function_version="$LATEST",
        invoked_function_arn=(
            "arn:aws:lambda:us-east-1:111122223333:function:adlytics-dev-GatewayInterceptor"
        ),
        memory_limit_in_mb=512,
        aws_request_id="request-1",
    )


def _tools_call(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {
            "name": "measurement___measurement_metric_by_grain",
            "arguments": {"metric_name": ARGUMENT_VALUE} if arguments is None else arguments,
        },
    }


def _event(body: dict[str, Any], headers: dict[str, Any] | None = None) -> dict[str, Any]:
    request: dict[str, Any] = {"path": "/mcp", "httpMethod": "POST", "body": body}
    if headers is not None:
        request["headers"] = headers
    return {
        "interceptorInputVersion": "1.0",
        "mcp": {"rawGatewayRequest": {"body": json.dumps(body)}, "gatewayRequest": request},
    }


def _unauthorized(body: dict[str, Any], request_id: object = 7) -> dict[str, Any]:
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayRequest": {"body": body},
            "transformedGatewayResponse": {
                "statusCode": 401,
                "body": {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32001, "message": "unauthorized"},
                },
            },
        },
    }


class TestAToolsCallWithABearer:
    def test_forwards_exactly_the_same_authorization_value_and_the_body(self) -> None:
        body = _tools_call()
        output = handler(_event(body, {"Authorization": BEARER, "Accept": "x"}), _context())
        assert output == {
            "interceptorOutputVersion": "1.0",
            "mcp": {
                "transformedGatewayRequest": {
                    "headers": {"Authorization": BEARER},
                    "body": body,
                }
            },
        }

    def test_finds_the_header_by_a_case_insensitive_name(self) -> None:
        output = handler(_event(_tools_call(), {"authorization": BEARER}), _context())
        assert output["mcp"]["transformedGatewayRequest"]["headers"] == {"Authorization": BEARER}

    def test_accepts_the_scheme_in_any_case_and_echoes_the_value_verbatim(self) -> None:
        value = "bEaReR opaque-credential"
        output = handler(_event(_tools_call(), {"AUTHORIZATION": value}), _context())
        assert output["mcp"]["transformedGatewayRequest"]["headers"] == {"Authorization": value}

    def test_strips_every_reserved_argument_and_keeps_the_rest(self) -> None:
        """The model must never supply an `_adl_` value: that prefix is the server's channel."""
        body = _tools_call(
            {
                "metric_name": ARGUMENT_VALUE,
                "_adl_caller_assertion": RESERVED_VALUE,
                "_adl_": "bare-prefix",
                "_adl_anything_else": {"nested": RESERVED_VALUE},
                "grains": ["date"],
                "adl_not_reserved": "kept",
            }
        )
        output = handler(_event(body, {"Authorization": BEARER}), _context())
        forwarded = output["mcp"]["transformedGatewayRequest"]["body"]
        assert forwarded == {
            **body,
            "params": {
                "name": "measurement___measurement_metric_by_grain",
                "arguments": {
                    "metric_name": ARGUMENT_VALUE,
                    "grains": ["date"],
                    "adl_not_reserved": "kept",
                },
            },
        }
        assert RESERVED_VALUE not in json.dumps(output)

    def test_leaves_a_call_without_arguments_to_the_gateway(self) -> None:
        body = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "x"}}
        output = handler(_event(body, {"Authorization": BEARER}), _context())
        assert output["mcp"]["transformedGatewayRequest"] == {
            "headers": {"Authorization": BEARER},
            "body": body,
        }


class TestAToolsCallWithoutAUsableBearer:
    @pytest.mark.parametrize(
        "headers",
        [
            pytest.param(None, id="headers-not-passed"),
            pytest.param({}, id="no-authorization"),
            pytest.param({"Authorization": ""}, id="empty"),
            pytest.param({"Authorization": "Bearer"}, id="scheme-only"),
            pytest.param({"Authorization": "Bearer "}, id="empty-credential"),
            pytest.param({"Authorization": "Bearer a b"}, id="two-credentials"),
            pytest.param({"Authorization": "Basic dXNlcjpwYXNz"}, id="other-scheme"),
            pytest.param({"Authorization": ["Bearer x"]}, id="not-a-string"),
            pytest.param(
                {"Authorization": "Bearer x", "authorization": "Bearer y"}, id="ambiguous"
            ),
        ],
    )
    def test_answers_401_with_a_json_rpc_error_and_strips_reserved_arguments(
        self, headers: dict[str, Any] | None
    ) -> None:
        body = _tools_call({"metric_name": ARGUMENT_VALUE, "_adl_caller_assertion": "forged"})
        output = handler(_event(body, headers), _context())
        stripped = {
            **body,
            "params": {**body["params"], "arguments": {"metric_name": ARGUMENT_VALUE}},
        }
        assert output == _unauthorized(stripped)

    def test_echoes_a_null_id_when_the_request_carries_none(self) -> None:
        body: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "x", "arguments": {}},
        }
        assert handler(_event(body, {}), _context()) == _unauthorized(body, request_id=None)


class TestAnyOtherMethod:
    @pytest.mark.parametrize("method", ["tools/list", "initialize", "notifications/initialized"])
    def test_passes_the_body_through_unchanged_with_no_headers(self, method: str) -> None:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {"arguments": {"_adl_caller_assertion": "untouched"}},
        }
        output = handler(_event(body, {"Authorization": BEARER}), _context())
        assert output == {
            "interceptorOutputVersion": "1.0",
            "mcp": {"transformedGatewayRequest": {"body": body}},
        }

    def test_passes_through_without_any_bearer_at_all(self) -> None:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        assert handler(_event(body), _context()) == {
            "interceptorOutputVersion": "1.0",
            "mcp": {"transformedGatewayRequest": {"body": body}},
        }


class TestAnUnsupportedInput:
    @pytest.mark.parametrize(
        ("event", "field"),
        [
            pytest.param([], "event", id="not-an-object"),
            pytest.param({"mcp": {}}, "interceptorInputVersion", id="no-version"),
            pytest.param(
                {**_event(_tools_call()), "interceptorInputVersion": "2.0"},
                "interceptorInputVersion",
                id="other-version",
            ),
            pytest.param({"interceptorInputVersion": "1.0"}, "mcp", id="no-mcp"),
            pytest.param(
                {"interceptorInputVersion": "1.0", "mcp": {}}, "gatewayRequest", id="no-request"
            ),
            pytest.param(
                {"interceptorInputVersion": "1.0", "mcp": {"gatewayRequest": {"body": "raw"}}},
                "body",
                id="body-not-an-object",
            ),
            pytest.param(
                {
                    "interceptorInputVersion": "1.0",
                    "mcp": {"gatewayRequest": {"body": {}, "headers": ["Authorization"]}},
                },
                "headers",
                id="headers-not-an-object",
            ),
        ],
    )
    def test_is_refused_naming_only_the_field(self, event: Any, field: str) -> None:
        with pytest.raises(ValidationError) as raised:
            handler(event, _context())
        assert raised.value.code == BAD_INTERCEPTOR_INPUT
        assert raised.value.context == {"field": field}


class TestIdempotence:
    def test_the_input_is_never_mutated_and_a_repeat_gives_the_same_output(self) -> None:
        event = _event(
            _tools_call({"metric_name": ARGUMENT_VALUE, "_adl_caller_assertion": "forged"}),
            {"Authorization": BEARER},
        )
        before = copy.deepcopy(event)
        first = handler(event, _context())
        second = handler(event, _context())
        assert event == before
        assert first == second

    def test_intercepting_the_transformed_request_again_changes_nothing(self) -> None:
        first = intercept(
            _event(
                _tools_call({"metric_name": ARGUMENT_VALUE, "_adl_caller_assertion": "forged"}),
                {"Authorization": BEARER},
            )
        ).output
        transformed = first["mcp"]["transformedGatewayRequest"]
        again = intercept(_event(transformed["body"], transformed["headers"])).output
        assert again == first


class TestTelemetry:
    @pytest.fixture
    def terminal(self, monkeypatch: pytest.MonkeyPatch) -> list[OperationLog]:
        captured: list[OperationLog] = []

        def capture(event: OperationLog, _logger: Logger) -> None:
            captured.append(event)

        monkeypatch.setattr(interceptor, "report_operation", capture)
        return captured

    @pytest.mark.parametrize(
        ("event", "expected"),
        [
            pytest.param(
                _event(_tools_call(), {"Authorization": BEARER}),
                ("forwarded", {"jsonrpc_method": "tools/call", "bearer_present": True}),
                id="forwarded",
            ),
            pytest.param(
                _event(_tools_call(), {}),
                ("unauthorized", {"jsonrpc_method": "tools/call", "bearer_present": False}),
                id="401",
            ),
            pytest.param(
                _event({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                ("passed_through", {"jsonrpc_method": "tools/list", "bearer_present": False}),
                id="passed-through",
            ),
            pytest.param(
                _event({"jsonrpc": "2.0", "id": 1, "method": "x" * 65}),
                ("passed_through", {"jsonrpc_method": "unrecognised", "bearer_present": False}),
                id="odd-method-is-not-echoed",
            ),
        ],
    )
    def test_one_terminal_record_carries_only_method_bearer_flag_and_outcome(
        self,
        terminal: list[OperationLog],
        event: dict[str, Any],
        expected: tuple[str, dict[str, object]],
    ) -> None:
        handler(event, _context())
        assert [
            (record.operation, record.component, record.phase, record.outcome, dict(record.details))
            for record in terminal
        ] == [("gateway.interceptor", "gateway_interceptor", "terminal", *expected)]

    def test_a_refused_input_still_reports_one_failed_terminal(
        self, terminal: list[OperationLog]
    ) -> None:
        with pytest.raises(ValidationError):
            handler({"interceptorInputVersion": "0.9"}, _context())
        assert [(record.outcome, dict(record.details)) for record in terminal] == [
            ("failed", {"jsonrpc_method": "unreadable", "bearer_present": False})
        ]

    def test_no_header_bearer_or_argument_value_reaches_any_log_record(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("DEBUG")
        arguments = {"metric_name": ARGUMENT_VALUE, "_adl_caller_assertion": RESERVED_VALUE}
        handler(_event(_tools_call(arguments), {"Authorization": BEARER}), _context())
        handler(
            _event(_tools_call(arguments), {"Authorization": "Basic " + ARGUMENT_VALUE}), _context()
        )
        with pytest.raises(ValidationError):
            handler(
                {
                    "interceptorInputVersion": "1.0",
                    "mcp": {"gatewayRequest": {"body": BEARER, "headers": {"a": BEARER}}},
                },
                _context(),
            )

        logged = "".join(repr(vars(record)) for record in caplog.records)
        assert "operation lifecycle" in logged
        assert "authorization denied" in logged
        for secret in (BEARER, BEARER.split(" ")[1], ARGUMENT_VALUE, RESERVED_VALUE):
            assert secret not in logged
