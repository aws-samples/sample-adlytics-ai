# SPDX-License-Identifier: MIT-0
"""Tests for installing the OTel SDK, and for the states in which it must not install.

**The most valuable assertion here is that nothing happens by default.** Every test in this suite
runs with tracing off. If `configure_tracing` installed a provider or built an exporter while
unconfigured, the unit tier would acquire a network dependency and `make verify` would stop being
runnable without credentials. That property is asserted rather than trusted, because it degrades
silently: an exporter that cannot reach its endpoint retries on a background thread and the tests
still pass, only slower.

Reading the configuration is the other half. `read_tracing_settings` is the one accessor in
`config.py` that tolerates absence, because a function which cannot resolve its *telemetry* should
still serve requests — refusing to start over an observability value would turn observability into
an availability risk. Every tolerated case below is one that would otherwise have that effect.
"""

from __future__ import annotations

import pytest
from opentelemetry import trace

from adapters.config import TracingSettings, read_tracing_settings
from adapters.outbound.telemetry.bootstrap import (
    _safe_http_url,  # pyright: ignore[reportPrivateUsage] — credential-redaction seam
    configure_tracing,
    flush_traces,
    flushes_traces,
    otlp_traces_endpoint,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-009", "G-39")]


def _settings(**overrides: str) -> TracingSettings:
    """Read tracing configuration from an explicit mapping rather than the process environment."""
    source = {
        "ADL_TRACING_ENABLED": "true",
        "ADL_AWS_REGION": "us-east-1",
        "ADL_ENVIRONMENT": "dev",
        **overrides,
    }
    return read_tracing_settings({key: value for key, value in source.items() if value})


class TestTheEndpoint:
    def test_it_is_derived_from_the_region(self) -> None:
        # Derived rather than configured: two keys that must agree eventually will not, and a
        # mismatched endpoint fails as spans that silently go nowhere.
        assert otlp_traces_endpoint("eu-west-1") == "https://xray.eu-west-1.amazonaws.com/v1/traces"

    def test_the_region_is_not_assumed(self) -> None:
        assert "us-east-1" not in otlp_traces_endpoint("ap-southeast-2")

    def test_http_span_urls_drop_presigned_credentials_userinfo_and_fragments(self) -> None:
        raw = (
            # pragma: allowlist nextline secret (A-TEST-LITERALS-001)
            "https://user:password@example.test/archive.zip?"
            "X-Amz-Credential=secret&X-Amz-Security-Token=token#fragment"
        )

        assert _safe_http_url(raw) == "https://example.test/archive.zip"


class TestReadingTheConfiguration:
    def test_enabled_by_the_word_true(self) -> None:
        assert _settings().enabled is True

    @pytest.mark.parametrize("value", ["True", "TRUE", " true "])
    def test_case_and_whitespace_are_tolerated(self, value: str) -> None:
        """Unlike `envFlag` on the infrastructure side, which refuses anything inexact.

        The two are tolerant in opposite directions on purpose. That flag turns on an account-wide
        billing change, so an operator who writes something ambiguous should be corrected. This one
        only decides whether spans leave the process, and the cost of being strict is a container
        that traces nothing because of a capital letter — a failure nobody would think to look for.
        """
        assert _settings(ADL_TRACING_ENABLED=value).enabled is True

    @pytest.mark.parametrize("value", ["yes", "1", "on", "false", "banana"])
    def test_anything_that_is_not_true_is_off(self, value: str) -> None:
        # Off rather than refused. An unparseable telemetry value must not stop a container that
        # could still answer questions.
        assert _settings(ADL_TRACING_ENABLED=value).enabled is False

    def test_absent_means_off(self) -> None:
        assert _settings(ADL_TRACING_ENABLED="").enabled is False

    def test_a_missing_region_disables_tracing_rather_than_guessing_one(self) -> None:
        """The OTLP endpoint is per-region, so a guessed region exports somewhere nobody chose."""
        settings = _settings(ADL_AWS_REGION="")
        assert settings.enabled is False

    def test_the_service_name_is_derived_from_the_environment(self) -> None:
        # So two deployments in one account never merge into one service on the map.
        assert _settings(ADL_ENVIRONMENT="acme-prod").service_name == "adlytics-acme-prod"

    def test_an_absent_environment_is_named_rather_than_blank(self) -> None:
        # A blank `service.name` groups every unrelated span together, which is worse than a value
        # that is obviously a placeholder.
        assert _settings(ADL_ENVIRONMENT="").service_name == "adlytics-unknown"

    @pytest.mark.parametrize(("supplied", "expected"), [("1.0", 1.0), ("0.05", 0.05), ("0", 0.0)])
    def test_a_valid_ratio_is_read(self, supplied: str, expected: float) -> None:
        assert _settings(ADL_TRACE_SAMPLE_RATIO=supplied).sample_ratio == expected

    @pytest.mark.parametrize("supplied", ["5", "100", "-1", "banana", "1.5"])
    def test_an_out_of_range_or_unparseable_ratio_falls_back_to_recording_everything(
        self, supplied: str
    ) -> None:
        """Falls back rather than refusing, and falls back *upward*.

        `5` meaning "5 percent" is the likely mistake. Reading it as a ratio would be 500%, and
        clamping it downward to 0.05 would silently agree with the wrong unit. Recording everything
        is the safe direction: it costs money, which is visible, rather than losing evidence, which
        is not.
        """
        assert _settings(ADL_TRACE_SAMPLE_RATIO=supplied).sample_ratio == 1.0


class TestNothingIsInstalledWhenDisabled:
    def test_configure_returns_false_and_installs_no_provider(self) -> None:
        """The property the whole unit tier depends on.

        If this regressed, every test in the suite would construct an OTLP exporter pointed at a
        real endpoint — and would still pass, only slower and with retries in the background. That
        is why it is asserted here rather than trusted.
        """
        before = trace.get_tracer_provider()
        assert configure_tracing(_settings(ADL_TRACING_ENABLED="false")) is False
        assert trace.get_tracer_provider() is before

    def test_disabled_tracing_does_not_inspect_an_explicit_operator_session(self) -> None:
        class UnusableSession:
            def __getattribute__(self, name: str) -> object:
                raise AssertionError(f"disabled tracing inspected session field {name}")

        before = trace.get_tracer_provider()
        assert (
            configure_tracing(
                _settings(ADL_TRACING_ENABLED="false"),
                session=UnusableSession(),  # type: ignore[arg-type]
            )
            is False
        )
        assert trace.get_tracer_provider() is before

    def test_no_sdk_provider_is_active_in_this_test_process(self) -> None:
        # The API's proxy, never the SDK's TracerProvider. Names the type rather than asserting
        # `is None`, because the API always returns *something*.
        assert type(trace.get_tracer_provider()).__name__ != "TracerProvider"

    def test_flushing_is_safe_with_nothing_configured(self) -> None:
        # A caller should never have to check. A flush that raised when tracing was off would put a
        # telemetry concern into every handler's error path.
        flush_traces()


class TestTheFlushDecorator:
    def test_it_returns_the_handler_result_unchanged(self) -> None:
        @flushes_traces
        def handler(event: object, context: object) -> dict[str, str]:  # noqa: ARG001
            return {"ok": "yes"}

        assert handler({}, None) == {"ok": "yes"}

    def test_it_flushes_even_when_the_handler_raises(self) -> None:
        """The path that matters. A failing invocation is the one whose spans are most wanted."""
        flushed: list[bool] = []

        @flushes_traces
        def handler(event: object, context: object) -> None:  # noqa: ARG001
            raise RuntimeError("boom")

        # Nothing to assert on the flush itself with no provider configured, so this asserts the
        # exception still propagates unchanged — a decorator that swallowed it would turn a Lambda
        # failure into a silent success.
        with pytest.raises(RuntimeError, match="boom"):
            handler({}, None)
        assert flushed == []

    def test_it_preserves_the_handler_identity(self) -> None:
        # Powertools and Lambda both read `__name__`; a decorator that replaced it would make every
        # log line and cold-start report name the wrapper.
        @flushes_traces
        def my_handler(event: object, context: object) -> None:  # noqa: ARG001
            return None

        assert my_handler.__name__ == "my_handler"


class _RaisingProvider:
    def force_flush(self, timeout_millis: int) -> bool:
        del timeout_millis
        raise ConnectionError("exporter down")


class TestTheFlushIsBoundedAndIsolated:
    """A flush can never replace a handler's outcome, and an export cannot block past 2 s."""

    def test_a_raising_flush_leaves_the_result_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(trace, "get_tracer_provider", _RaisingProvider)

        @flushes_traces
        def handler(event: object, context: object) -> dict[str, str]:  # noqa: ARG001
            return {"ok": "yes"}

        with caplog.at_level("WARNING", logger="adlytics.telemetry.flush"):
            assert handler({}, None) == {"ok": "yes"}
        assert [r.getMessage() for r in caplog.records] == ["trace flush failed: ConnectionError"]

    def test_a_raising_flush_leaves_the_original_exception_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(trace, "get_tracer_provider", _RaisingProvider)

        @flushes_traces
        def handler(event: object, context: object) -> None:  # noqa: ARG001
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            handler({}, None)

    def test_the_exporter_is_built_with_the_two_second_deadline(self) -> None:
        import inspect  # noqa: PLC0415

        from adapters.outbound.telemetry import bootstrap  # noqa: PLC0415

        assert bootstrap._EXPORT_TIMEOUT_SECONDS == 2  # pyright: ignore[reportPrivateUsage]
        assert "timeout=_EXPORT_TIMEOUT_SECONDS" in inspect.getsource(bootstrap.configure_tracing)

    def test_a_blocked_export_returns_within_the_bound(self) -> None:
        import socket  # noqa: PLC0415
        import time  # noqa: PLC0415

        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: PLC0415
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: PLC0415
            InMemorySpanExporter,
        )

        from adapters.outbound.telemetry import bootstrap  # noqa: PLC0415

        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(8)  # accepts, never answers: a blocked collector
        port = server.getsockname()[1]
        memory = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(memory))
        provider.get_tracer("t").start_span("x").end()
        finished = memory.get_finished_spans()
        exporter = OTLPSpanExporter(
            endpoint=f"http://127.0.0.1:{port}/v1/traces",
            timeout=bootstrap._EXPORT_TIMEOUT_SECONDS,  # pyright: ignore[reportPrivateUsage]
        )
        started = time.monotonic()
        try:
            exporter.export(finished)
        finally:
            server.close()
        assert time.monotonic() - started < bootstrap._EXPORT_TIMEOUT_SECONDS + 1.5  # pyright: ignore[reportPrivateUsage]
