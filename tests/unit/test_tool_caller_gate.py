# SPDX-License-Identifier: MIT-0
"""Every Gateway tool verifies its caller first and refuses, closed, when it cannot (ADR-077 §5).

Each entry point is imported under a fictional caller configuration and given the real verifier
over a key made in the test session (`tool_caller_support`), so no AWS client is stubbed. For each
tool the known-good bearer is shown to pass the gate first, then a call without a bearer, with a
non-Bearer scheme, and with a token signed by a foreign key under the issuer's `kid` is refused
with the tool's own refusal shape and the closed `caller_unverified` classification, having
touched no argument and no data port, and with neither the bearer nor its claims in any log.

The measurement entry point composes Athena, DynamoDB, and S3 adapters at import. Constructing
them makes no call, so it is importable here with fictional configuration; its dispatch is replaced
by a recorder so the good-bearer case proves the gate opened without reaching a data port.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest
from aws_lambda_powertools import Logger
from tool_caller_support import (
    FOREIGN_KEY,
    PROPAGATED_HEADERS,
    SUBJECT,
    cognito_token,
    gateway_token,
    import_tool_entrypoint,
    mcp_verifier,
    with_bearer,
)

from adapters.inbound.tool_handlers import gateway
from adapters.inbound.tool_handlers.caller import (
    CALLER_UNVERIFIED_DESCRIPTION,
    CallerAdmission,
    CallerVerifier,
)
from adapters.inbound.tool_handlers.catalog import CatalogTool
from adapters.inbound.tool_handlers.measurement import MeasurementTool
from adapters.outbound.telemetry.reporting import GatewayTerminalLog, Report, report_lifecycle
from domain.shared_kernel.errors import ErrorCode, ValidationError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-077")]

_MEASUREMENT_ENV = {
    "AWS_DEFAULT_REGION": "eu-west-1",
    "AWS_EC2_METADATA_DISABLED": "true",
    "ADL_ENVIRONMENT": "dev",
    "ADL_AWS_REGION": "eu-west-1",
    "ADL_COGNITO_USER_POOL_ID": "eu-west-1_example",
    "ADL_COGNITO_CLIENT_ID": "1example23456789",
    "ADL_COGNITO_LOGIN_DOMAIN": "auth.example.invalid",
    "ADL_APP_BASE_URL": "https://app.example.com",
    "ADL_API_BASE_URL": "https://app.example.com",
    "ADL_COOKIE_SCOPE": "host_only",
    "ADL_REFRESH_COOKIE_MAX_AGE_SECONDS": "2592000",
    "ADL_ATHENA_WORKGROUP": "adlytics-test-interactive",
    "ADL_SEMANTIC_ATTEMPTS_TABLE": "adlytics-test-semantic-attempts",
    "ADL_SEMANTIC_LEASES_TABLE": "adlytics-test-semantic-leases",
    "ADL_SEMANTIC_PUBLICATIONS_TABLE": "adlytics-test-semantic-publications",
    "ADL_SEMANTIC_V2_BUCKET": "adlytics-test-semantic-v2",
    "ADL_SEMANTIC_V2_CURRENT_DATABASE": "adlytics_test_current",
    "ADL_ATHENA_RESULT_REUSE_MAX_AGE_MINUTES": "60",
    "ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY": "4",
}

catalog = import_tool_entrypoint("adapters.inbound.tool_handlers.catalog_entrypoint")
measurement = import_tool_entrypoint(
    "adapters.inbound.tool_handlers.measurement_entrypoint", _MEASUREMENT_ENV
)

MESSAGE_ID = "mcp-message-under-test"
REACHED = ErrorCode("TEST", 77)
UNVERIFIED_CODE = "ADLY-CALLER-0001"


@dataclass(frozen=True)
class _LambdaContext:
    """The attributes Powertools reads off a Lambda context, plus the Gateway client context."""

    client_context: SimpleNamespace
    function_name: str = "adlytics-tool"
    function_version: str = "$LATEST"
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:000000000000:function:adlytics-tool"
    memory_limit_in_mb: int = 512
    aws_request_id: str = "00000000-0000-4000-8000-000000000000"

    def get_remaining_time_in_millis(self) -> int:
        return 300_000


def _gateway_custom(tool_name: str) -> dict[str, Any]:
    return {"bedrockAgentCoreToolName": tool_name, "bedrockAgentCoreMcpMessageId": MESSAGE_ID}


def _context(custom: dict[str, Any]) -> _LambdaContext:
    return _LambdaContext(client_context=SimpleNamespace(custom=custom))


_UNVERIFIED: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "no bearer": lambda custom: custom,
    "a non-Bearer scheme": lambda custom: {
        **custom,
        PROPAGATED_HEADERS: {"Authorization": f"Basic {gateway_token()}"},
    },
    "a token signed by a foreign key under the issuer kid": lambda custom: with_bearer(
        custom, gateway_token(FOREIGN_KEY)
    ),
    "a harness-audience token": lambda custom: with_bearer(
        custom,
        gateway_token(
            aud="adlytics-dev-harness",
            client_id="adlytics-dev-conversation-route",
            act={"sub": "adlytics-dev-conversation-route"},
        ),
    ),
}


@dataclass
class _Recorder:
    """Every path past the caller gate that a refused call must never reach, by name."""

    calls: list[str] = field(default_factory=list[str])
    lifecycle: list[GatewayTerminalLog] = field(default_factory=list[GatewayTerminalLog])

    def lifecycle_sink(self) -> Callable[[GatewayTerminalLog, Logger], None]:
        def sink(event: GatewayTerminalLog, logger: Logger) -> None:
            self.lifecycle.append(event)
            report_lifecycle(event, logger)

        return sink


@dataclass
class _RecordingDispatch:
    """The measurement dispatch, recording each entry and stopping before any data port."""

    real: Any
    recorder: _Recorder

    def tool(self, name: str) -> MeasurementTool:
        self.recorder.calls.append("dispatch.tool")
        return self.real.tool(name)

    def request_from(self, *_args: object) -> object:
        self.recorder.calls.append("dispatch.request_from")
        raise ValidationError(code=REACHED, description="The measurement dispatch was reached.")

    @property
    def tooling(self) -> object:
        self.recorder.calls.append("dispatch.tooling")
        return self.real.tooling

    def refusal_response(self, error: ValidationError, correlation_id: str) -> dict[str, Any]:
        return self.real.refusal_response(error, correlation_id)


@dataclass(frozen=True)
class _Logs:
    """Every log a call writes: records the handler logged and what Powertools printed."""

    caplog: pytest.LogCaptureFixture
    capsys: pytest.CaptureFixture[str]

    def start(self) -> None:
        self.caplog.set_level("DEBUG")
        self.caplog.clear()
        self.capsys.readouterr()

    def text(self) -> str:
        captured = self.capsys.readouterr()
        records = "".join(repr(vars(record)) for record in self.caplog.records)
        return records + captured.out + captured.err


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]) -> _Logs:
    return _Logs(caplog, capsys)


def _assert_nothing_secret_logged(logged: str, custom: dict[str, Any]) -> None:
    # The refusal was logged, closed and by cause, so the absence below is not a silent log.
    assert "caller_unverified" in logged
    assert "managed lifecycle" in logged
    headers: object = custom.get(PROPAGATED_HEADERS, {})
    values = (
        [str(value) for value in cast("dict[str, object]", headers).values()]
        if isinstance(headers, dict)
        else []
    )
    for value in values:
        for secret in (value, *value.split(" "), *value.split(" ")[-1].split(".")):
            if len(secret) > 8:  # the scheme word itself is not a secret
                assert secret not in logged
    assert SUBJECT not in logged


def _assert_verified(projection: Mapping[str, object], logged: str = "") -> None:
    # The tool record says only that the caller verified. The principal is attributed on the
    # conversation audit receipt (ADR-077 §6), never in tool telemetry.
    assert projection["caller_verified"] is True
    assert SUBJECT not in repr(projection)
    assert SUBJECT not in logged


def _unverified_projection(operation: str) -> dict[str, object]:
    return {
        "operation": operation,
        "component": "gateway_target",
        "phase": "terminal",
        "outcome": "refused",
        "result_class": "caller_unverified",
        "investigation_next": "inspect_tool_boundary",
        "caller_verified": False,
        "gateway_message_ref": MESSAGE_ID,
    }


def _governed_refusal(response: dict[str, Any]) -> None:
    assert set(response) == {"refused", "code", "reason", "correlation_id", "metadata"}
    assert response["refused"] is True
    assert response["code"] == UNVERIFIED_CODE
    assert response["reason"] == CALLER_UNVERIFIED_DESCRIPTION
    assert response["metadata"]["grounding"]["renderings"][0]["text"] == response["reason"]


@dataclass(frozen=True)
class _RecordingCaller:
    """The module's real verifier, recording when it is consulted so ordering can be asserted."""

    real: CallerVerifier
    recorder: _Recorder

    def admit(self, context: object, report: Report) -> CallerAdmission:
        self.recorder.calls.append("caller.admit")
        return self.real.admit(context, report)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorded = _Recorder()
    for module in (catalog, measurement):
        monkeypatch.setattr(module, "report_lifecycle", recorded.lifecycle_sink())
        monkeypatch.setattr(module, "CALLER", _RecordingCaller(module.CALLER, recorded))
    real_read = gateway.GatewayInvocation.read

    def read(event: dict[str, Any], context: object) -> gateway.GatewayInvocation:
        recorded.calls.append("invocation.read")
        return real_read(event, context)

    # One class serves both entry points, each reaching it as `gateway.GatewayInvocation`.
    monkeypatch.setattr(gateway.GatewayInvocation, "read", staticmethod(read))
    return recorded


# Reading the invocation before admission would consult a model-authored argument for a caller
# that never verified, so a verified call's first two steps are pinned in this order.
_ADMITTED = ["caller.admit", "invocation.read"]


def _assert_unattributed(projection: Mapping[str, object]) -> None:
    assert projection["caller_verified"] is False
    assert SUBJECT not in repr(projection)


class TestTheCatalogTool:
    TOOL = f"catalog___{CatalogTool.LIST_METRICS.value}"

    @pytest.fixture(autouse=True)
    def _guard(self, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder) -> None:
        real_handle = catalog.handle
        real_request = catalog._request_from

        def handle(request: object) -> object:
            recorder.calls.append("handle")
            return real_handle(request)

        def request_from(*args: Any) -> object:
            recorder.calls.append("request_from")
            return real_request(*args)

        monkeypatch.setattr(catalog, "handle", handle)
        monkeypatch.setattr(catalog, "_request_from", request_from)

    def test_a_verified_caller_is_answered(self, recorder: _Recorder) -> None:
        answer = catalog.handler({}, _context(with_bearer(_gateway_custom(self.TOOL))))

        assert answer.get("refused") is not True
        assert answer["metadata"]["row_count"] > 0
        assert recorder.calls == [*_ADMITTED, "request_from", "handle"]
        _assert_verified(recorder.lifecycle[-1].projection)
        assert recorder.lifecycle[-1].projection["result_class"] == "answer"

    @pytest.mark.parametrize("case", sorted(_UNVERIFIED))
    def test_an_unverified_caller_is_refused_before_anything_is_read(
        self, recorder: _Recorder, logs: _Logs, case: str
    ) -> None:
        good = catalog.handler({}, _context(with_bearer(_gateway_custom(self.TOOL))))
        assert good.get("refused") is not True
        recorder.calls.clear()
        logs.start()
        custom = _UNVERIFIED[case](_gateway_custom(self.TOOL))

        response = catalog.handler({"metric_name": "retail_revenue_shipped"}, _context(custom))

        _governed_refusal(response)
        assert recorder.calls == ["caller.admit"]
        projection = recorder.lifecycle[-1].projection
        assert {key: projection[key] for key in _unverified_projection("x")} == (
            _unverified_projection("unverified_caller")
        )
        _assert_unattributed(projection)
        _assert_nothing_secret_logged(logs.text(), custom)


class TestTheMeasurementTool:
    TOOL = f"measurement___{MeasurementTool.METRIC_BY_GRAIN.value}"

    @pytest.fixture(autouse=True)
    def _guard(self, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder) -> None:
        monkeypatch.setattr(
            measurement, "DISPATCH", _RecordingDispatch(measurement.DISPATCH, recorder)
        )

        def handle(*_args: object) -> object:
            recorder.calls.append("handle")
            raise AssertionError("no measurement is computed in this test")

        monkeypatch.setattr(measurement, "handle", handle)

    def test_a_verified_caller_reaches_the_dispatch(self, recorder: _Recorder) -> None:
        response = measurement.handler(
            {"metric_reference": "shipped revenue"},
            _context(with_bearer(_gateway_custom(self.TOOL))),
        )

        assert response["refused"] is True
        assert response["code"] == str(REACHED)
        assert recorder.calls == [*_ADMITTED, "dispatch.tool", "dispatch.request_from"]
        projection = recorder.lifecycle[-1].projection
        _assert_verified(projection)
        assert projection["result_class"] == "classified_refusal"

    @pytest.mark.covers("ADR-150")
    def test_an_unentitled_mcp_client_caller_is_refused_before_any_data_port(
        self, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder, logs: _Logs
    ) -> None:
        """The external MCP path reaches the same function; the gate refuses it the same way."""
        monkeypatch.setattr(measurement, "CALLER", _RecordingCaller(mcp_verifier(), recorder))
        entitled = with_bearer(_gateway_custom(self.TOOL), cognito_token())
        response = measurement.handler({"metric_reference": "shipped revenue"}, _context(entitled))
        assert response["code"] == str(REACHED)
        assert recorder.calls == [*_ADMITTED, "dispatch.tool", "dispatch.request_from"]
        recorder.calls.clear()
        logs.start()

        custom = with_bearer(_gateway_custom(self.TOOL), cognito_token(quality_review_levels=[]))
        response = measurement.handler({"metric_reference": "shipped revenue"}, _context(custom))

        _governed_refusal(response)
        assert recorder.calls == ["caller.admit"]
        projection = recorder.lifecycle[-1].projection
        assert {key: projection[key] for key in _unverified_projection("x")} == (
            _unverified_projection("unverified_caller")
        )
        _assert_unattributed(projection)
        logged = logs.text()
        assert "entitlement_missing" in logged
        _assert_nothing_secret_logged(logged, custom)

    def test_the_deployment_wide_scope_is_still_the_dispatch_scope(self) -> None:
        """Identity is attribution, not entitlement: ADR-054's scope is unchanged by the gate."""
        assert measurement.DISPATCH.real.scope is measurement.DEPLOYMENT_SCOPE

    @pytest.mark.parametrize("case", sorted(_UNVERIFIED))
    def test_an_unverified_caller_is_refused_before_any_data_port(
        self, recorder: _Recorder, logs: _Logs, case: str
    ) -> None:
        measurement.handler({}, _context(with_bearer(_gateway_custom(self.TOOL))))
        assert recorder.calls == [*_ADMITTED, "dispatch.tool", "dispatch.request_from"]
        recorder.calls.clear()
        logs.start()
        custom = _UNVERIFIED[case](_gateway_custom(self.TOOL))

        response = measurement.handler({"metric_reference": "shipped revenue"}, _context(custom))

        _governed_refusal(response)
        assert recorder.calls == ["caller.admit"]
        projection = recorder.lifecycle[-1].projection
        assert {key: projection[key] for key in _unverified_projection("x")} == (
            _unverified_projection("unverified_caller")
        )
        _assert_unattributed(projection)
        _assert_nothing_secret_logged(logs.text(), custom)


_TOOL_HANDLERS = Path(__file__).resolve().parents[2] / "src/adapters/inbound/tool_handlers"


def _local_calls(function: ast.FunctionDef) -> set[str]:
    return {
        node.func.id
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def _admits_the_caller(function: ast.FunctionDef) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "admit"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "CALLER"
        for node in ast.walk(function)
    )


def _gate(source: Path) -> tuple[bool, bool]:
    """Whether a module declares `CALLER: CallerVerifier`, and whether `handler` reaches admit.

    Read from the source rather than imported, so a new entry point is checked before anyone
    has written the configuration its import would need.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    declared = any(
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "CALLER"
        and isinstance(node.annotation, ast.Name)
        and node.annotation.id == "CallerVerifier"
        for node in tree.body
    )
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    reached: set[str] = set()
    pending = ["handler"] if "handler" in functions else []
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        reached.add(name)
        pending.extend(call for call in _local_calls(functions[name]) if call in functions)
    return declared, any(_admits_the_caller(functions[name]) for name in reached)


def test_every_gateway_tool_entry_point_holds_a_caller_verifier_its_handler_consults() -> None:
    """A third tool added without the gate fails here rather than answering anyone.

    Every `*_entrypoint.py` under tool_handlers is a Gateway Lambda target (the two today are
    AgentStack's catalog and measurement targets; ADR-083 retired the presentation finalizer),
    so none is excluded.
    """
    sources = sorted(_TOOL_HANDLERS.glob("*_entrypoint.py"))
    assert sources, "the glob must find the entry points, or this test proves nothing"
    assert {source.name: _gate(source) for source in sources} == {
        source.name: (True, True) for source in sources
    }
    imported: tuple[ModuleType, ...] = (catalog, measurement)
    assert {Path(cast("str", module.__file__)).name for module in imported} <= {
        source.name for source in sources
    }
    assert all(isinstance(module.CALLER, CallerVerifier) for module in imported)
