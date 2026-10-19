# SPDX-License-Identifier: MIT-0
"""Strict trace-registry loading, decorator conformance, generation, and span hierarchy."""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path
from textwrap import dedent
from typing import Any, cast
from unittest.mock import patch

import pytest
import yaml
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanContext

from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.trace_contract import (
    GENERATED_SPANS_PATH,
    SpanDeclaration,
    TraceContract,
    TraceContractError,
    conformance_findings,
    load_trace_contract,
    render_generated_spans,
)
from adapters.jobs.trace_contract.__main__ import main
from adapters.outbound.telemetry import tracing
from adapters.outbound.telemetry.tracing import (
    ATHENA_STATEMENT,
    INGEST_DELIVERY,
    INGEST_PARTITION,
    REPROCESS_APPLY,
    REPROCESS_DELIVERY,
    REPROCESS_PROCESS,
    span,
    trace_boundary,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-009")]
ROOT = Path(__file__).resolve().parents[2]
_SOURCE_PATH = "src/adapters/jobs/reprocess_semantic_v2/sample.py"
_DEFAULT_SOURCE = """
@trace_boundary(SAMPLE_SPAN)
def run() -> None:
    with span(SAMPLE_SPAN, operation_id="operation") as recording:
        recording.record(completed=1)
"""


def _record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "constant": "SAMPLE_SPAN",
        "name": "sample.span",
        "parent": "root",
        "required_attributes": ["operation_id"],
        "allowed_attributes": ["operation_id", "completed"],
        "propagation": "remote_boundary",
        "purpose": "Exercise one static test span.",
    }
    record.update(overrides)
    return record


def _registry(*records: dict[str, object]) -> dict[str, object]:
    return {"version": 1, "spans": list(records) if records else [_record()]}


def _write_tree(
    root: Path,
    source: str = _DEFAULT_SOURCE,
    document: dict[str, object] | None = None,
) -> None:
    source_path = root / _SOURCE_PATH
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(dedent(source))
    contract_path = root / "contracts/observability/spans.yaml"
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    contract_path.write_text(
        yaml.safe_dump(document if document is not None else _registry(), sort_keys=False)
    )


def _messages(root: Path) -> str:
    contract = load_trace_contract(root)
    return "\n".join(conformance_findings(root, contract))


@pytest.mark.parametrize(
    "document",
    [
        {"version": 1, "spans": [_record()], "unknown": True},
        {"version": 2, "spans": [_record()]},
        {"version": 1, "spans": [_record(unknown=True)]},
        {"version": 1, "spans": [_record(function={"path": _SOURCE_PATH, "symbol": "run"})]},
    ],
)
def test_loader_rejects_nonexact_or_unsupported_schema(
    tmp_path: Path, document: dict[str, object]
) -> None:
    _write_tree(tmp_path, document=document)

    with pytest.raises(TraceContractError):
        load_trace_contract(tmp_path)


def test_data_contract_discovery_excludes_observability_registry(tmp_path: Path) -> None:
    _write_tree(tmp_path)

    assert load_contracts(tmp_path) == []


def test_loader_rejects_duplicate_yaml_keys(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    path = tmp_path / "contracts/observability/spans.yaml"
    path.write_text(path.read_text().replace("version: 1", "version: 1\nversion: 1", 1))

    with pytest.raises(TraceContractError, match="duplicate key: version"):
        load_trace_contract(tmp_path)


def test_loader_rejects_duplicate_names(tmp_path: Path) -> None:
    duplicate = _record(constant="OTHER_SPAN")
    _write_tree(tmp_path, document=_registry(_record(), duplicate))

    with pytest.raises(TraceContractError, match="duplicate span name"):
        load_trace_contract(tmp_path)


def test_loader_rejects_duplicate_constants(tmp_path: Path) -> None:
    duplicate = _record(name="sample.other")
    _write_tree(tmp_path, document=_registry(_record(), duplicate))

    with pytest.raises(TraceContractError, match="duplicate span constant"):
        load_trace_contract(tmp_path)


def test_loader_rejects_unknown_parent(tmp_path: Path) -> None:
    _write_tree(
        tmp_path,
        document=_registry(_record(parent="MISSING_PARENT", propagation="current")),
    )

    with pytest.raises(TraceContractError, match="not a declared constant"):
        load_trace_contract(tmp_path)


def test_loader_rejects_required_attribute_that_is_not_allowed(tmp_path: Path) -> None:
    _write_tree(
        tmp_path,
        document=_registry(_record(required_attributes=["operation_id", "execution_id"])),
    )

    with pytest.raises(TraceContractError, match="required attributes are not allowed"):
        load_trace_contract(tmp_path)


def test_checker_discovers_decorated_nested_class_method(tmp_path: Path) -> None:
    source = """
class Projection:
    @trace_boundary(SAMPLE_SPAN)
    def statement(self) -> None:
        with span(SAMPLE_SPAN, operation_id="operation") as recording:
            recording.record(completed=1)
"""
    _write_tree(tmp_path, source=source)

    assert _messages(tmp_path) == ""


def test_checker_rejects_missing_trace_boundary_owner(tmp_path: Path) -> None:
    source = """
def run() -> None:
    with span(SAMPLE_SPAN, operation_id="operation") as recording:
        recording.record(completed=1)
"""
    _write_tree(tmp_path, source=source)

    assert "SAMPLE_SPAN: missing trace_boundary implementation owner" in _messages(tmp_path)


def test_checker_rejects_undeclared_decorator_constant(tmp_path: Path) -> None:
    source = """
@trace_boundary(SAMPLE_SPAN, OTHER_SPAN)
def run() -> None:
    with span(SAMPLE_SPAN, operation_id="operation") as recording:
        recording.record(completed=1)
"""
    _write_tree(tmp_path, source=source)

    assert "trace_boundary declares undeclared static span constant OTHER_SPAN" in _messages(
        tmp_path
    )


def test_checker_rejects_duplicate_implementation_ownership(tmp_path: Path) -> None:
    source = """
@trace_boundary(SAMPLE_SPAN)
def first() -> None:
    with span(SAMPLE_SPAN, operation_id="operation") as recording:
        recording.record(completed=1)

@trace_boundary(SAMPLE_SPAN)
def second() -> None:
    with span(SAMPLE_SPAN, operation_id="operation") as recording:
        recording.record(completed=1)
"""
    _write_tree(tmp_path, source=source)

    assert "duplicate trace_boundary implementation ownership" in _messages(tmp_path)


def test_checker_allows_multiple_declared_constants_on_one_function(tmp_path: Path) -> None:
    source = """
@trace_boundary(SAMPLE_SPAN, OTHER_SPAN)
def run() -> None:
    with span(SAMPLE_SPAN, operation_id="operation") as recording:
        recording.record(completed=1)
    with span(OTHER_SPAN, operation_id="operation") as recording:
        recording.record(completed=1)
"""
    other = _record(constant="OTHER_SPAN", name="sample.other")
    _write_tree(tmp_path, source=source, document=_registry(_record(), other))

    assert _messages(tmp_path) == ""


def test_checker_rejects_missing_declared_span_in_decorated_function(tmp_path: Path) -> None:
    _write_tree(
        tmp_path,
        source="@trace_boundary(SAMPLE_SPAN)\ndef run() -> None:\n    return None\n",
    )

    assert "must contain exactly one matching span; found 0" in _messages(tmp_path)


def test_checker_rejects_wrong_constant_and_undeclared_static_span(tmp_path: Path) -> None:
    source = """
@trace_boundary(SAMPLE_SPAN)
def run() -> None:
    with span(OTHER_SPAN, operation_id="operation") as recording:
        recording.record(completed=1)
"""
    _write_tree(tmp_path, source=source)

    messages = _messages(tmp_path)
    assert "must contain exactly one matching span; found 0" in messages
    assert "undeclared static span name 'OTHER_SPAN'" in messages


def test_checker_rejects_missing_required_metadata(tmp_path: Path) -> None:
    source = """
@trace_boundary(SAMPLE_SPAN)
def run() -> None:
    with span(SAMPLE_SPAN) as recording:
        recording.record(completed=1)
"""
    _write_tree(tmp_path, source=source)

    assert "must provide required metadata when the span opens: operation_id" in _messages(tmp_path)


def test_checker_rejects_metadata_outside_allowed_set(tmp_path: Path) -> None:
    source = """
@trace_boundary(SAMPLE_SPAN)
def run() -> None:
    with span(SAMPLE_SPAN, operation_id="operation", surprise=True) as recording:
        recording.record(completed=1)
"""
    _write_tree(tmp_path, source=source)

    assert "records undeclared metadata: surprise" in _messages(tmp_path)


def test_checker_rejects_forbidden_metadata_even_if_constructed_registry_allows_it(
    tmp_path: Path,
) -> None:
    source = """
@trace_boundary(SAMPLE_SPAN)
def run() -> None:
    with span(SAMPLE_SPAN, operation_id="operation", sql="select secret") as recording:
        recording.record(completed=1)
"""
    _write_tree(tmp_path, source=source)
    declaration = SpanDeclaration(
        constant="SAMPLE_SPAN",
        name="sample.span",
        parent="root",
        required_attributes=("operation_id",),
        allowed_attributes=("operation_id", "completed", "sql"),
        propagation="remote_boundary",
        purpose="Exercise the checker independently of loader defenses.",
    )

    messages = "\n".join(
        conformance_findings(tmp_path, TraceContract(version=1, spans=(declaration,)))
    )

    assert "records forbidden metadata: sql" in messages


@pytest.mark.parametrize("method", ["info", "warning", "error", "exception"])
def test_checker_rejects_direct_logger_lifecycle_calls(tmp_path: Path, method: str) -> None:
    source = f"""
from aws_lambda_powertools import Logger

telemetry = Logger()

@trace_boundary(SAMPLE_SPAN)
def run() -> None:
    with span(SAMPLE_SPAN, operation_id="operation") as recording:
        recording.record(completed=1)
    telemetry.{method}("lifecycle")
"""
    _write_tree(tmp_path, source=source)

    assert f"direct Logger.{method} lifecycle call" in _messages(tmp_path)


def test_checker_rejects_undeclared_static_span_in_other_source(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    other = tmp_path / "src/other.py"
    other.write_text(
        dedent(
            """
            def other() -> None:
                with span(UNDECLARED_SPAN) as recording:
                    recording.record(outcome="complete")
            """
        )
    )

    assert "undeclared static span name 'UNDECLARED_SPAN'" in _messages(tmp_path)


def test_checker_explicitly_allows_dynamic_tool_span_names(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    other = tmp_path / "src/tool.py"
    other.write_text(
        dedent(
            """
            def tool() -> None:
                with span(tool_span("governed_tool")) as recording:
                    recording.record(outcome="complete")
            """
        )
    )

    assert "undeclared static span" not in _messages(tmp_path)


def test_checker_rejects_direct_opentelemetry_import_in_decorated_source(
    tmp_path: Path,
) -> None:
    source = """
from opentelemetry import trace

@trace_boundary(SAMPLE_SPAN)
def run() -> None:
    with span(SAMPLE_SPAN, operation_id="operation") as recording:
        recording.record(completed=1)
"""
    _write_tree(tmp_path, source=source)

    assert "direct OpenTelemetry import outside an approved seam" in _messages(tmp_path)


@pytest.mark.parametrize("span_names", [(), ("",), ("   ",), ("sample.span", "sample.span")])
def test_trace_boundary_rejects_missing_blank_or_duplicate_names(
    span_names: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError):
        trace_boundary(*span_names)


def test_trace_boundary_preserves_function_identity_behavior_and_immutable_marker() -> None:
    calls: list[str] = []

    def original(value: str) -> str:
        calls.append(value)
        return value.upper()

    decorated = trace_boundary("sample.span", "sample.child")(original)

    assert decorated is original
    assert decorated("result") == "RESULT"
    assert calls == ["result"]
    marker = vars(decorated)["__trace_boundaries__"]
    assert marker == ("sample.span", "sample.child")
    assert isinstance(marker, tuple)


def test_check_mode_detects_stale_generated_file_without_rewriting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_tree(tmp_path)
    contract = load_trace_contract(tmp_path)
    generated = tmp_path / GENERATED_SPANS_PATH
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text(render_generated_spans(contract))
    monkeypatch.chdir(tmp_path)

    assert main(["--check"]) == 0
    generated.write_text("stale\n")
    assert main(["--check"]) == 1
    assert generated.read_text() == "stale\n"


def test_generate_mode_writes_constants_before_reporting_missing_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A newly declared span needs its constant to exist before its owner can be written."""
    _write_tree(tmp_path, source="\n")
    generated = tmp_path / GENERATED_SPANS_PATH
    monkeypatch.chdir(tmp_path)

    assert main([]) == 1
    assert "SAMPLE_SPAN" in generated.read_text()


def test_checker_rejects_generated_constant_missing_from_tracing_seam_reexport(
    tmp_path: Path,
) -> None:
    """An import error at handler load is the wrong place to learn a constant was not exported."""
    _write_tree(tmp_path)
    seam = tmp_path / "src/adapters/outbound/telemetry/tracing.py"
    seam.parent.mkdir(parents=True, exist_ok=True)
    seam.write_text(
        dedent(
            """
            from adapters.outbound.telemetry.generated_spans import OTHER_SPAN

            __all__ = ["OTHER_SPAN"]
            """
        )
    )

    messages = _messages(tmp_path)

    assert "generated span constant SAMPLE_SPAN is not imported" in messages
    seam.write_text(
        dedent(
            """
            from adapters.outbound.telemetry.generated_spans import SAMPLE_SPAN

            __all__ = ["span"]
            """
        )
    )
    assert "SAMPLE_SPAN is not re-exported in __all__" in _messages(tmp_path)


def test_real_sdk_spans_preserve_the_declared_operator_hierarchy_and_required_attributes() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    expected: dict[str, dict[str, object]] = {
        REPROCESS_PROCESS: {
            "operation_id": "semantic-v2",
            "execution_id": "execution-current",
            "predecessor_execution_id": "execution-prior",
            "plan_digest": "digest-reviewed",
            "stage": "start",
            "completed": 0,
            "total": 1,
        },
        REPROCESS_APPLY: {
            "operation_id": "semantic-v2",
            "execution_id": "execution-current",
            "predecessor_execution_id": "execution-prior",
            "plan_digest": "digest-reviewed",
            "stage": "apply",
            "completed": 0,
            "total": 1,
        },
        REPROCESS_DELIVERY: {
            "operation_id": "semantic-v2",
            "execution_id": "execution-current",
            "predecessor_execution_id": "execution-prior",
            "plan_digest": "digest-reviewed",
            "stage": "apply",
            "completed": 0,
            "total": 1,
            "contract_ids": "('contract-a',)",
        },
        INGEST_DELIVERY: {
            "operation_id": "semantic-v2",
            "execution_id": "execution-current",
            "predecessor_execution_id": "execution-prior",
            "plan_digest": "digest-reviewed",
            "stage": "prepare",
            "completed": 0,
            "total": 1,
            "contract_ids": "('contract-a',)",
        },
        INGEST_PARTITION: {
            "operation_id": "semantic-v2",
            "execution_id": "execution-current",
            "predecessor_execution_id": "execution-prior",
            "plan_digest": "digest-reviewed",
            "stage": "publish",
            "contract_ids": "('contract-a',)",
            "bucket_starts": "('2026-01-01',)",
            "completed": 0,
            "total": 1,
        },
        ATHENA_STATEMENT: {
            "operation_kind": "merge",
            "logical_table": "fact_daily",
        },
    }

    with (
        patch.object(tracing, "TRACER", provider.get_tracer("trace-contract-test")),
        span(REPROCESS_PROCESS, **expected[REPROCESS_PROCESS]),
        span(REPROCESS_APPLY, **expected[REPROCESS_APPLY]),
        span(REPROCESS_DELIVERY, **expected[REPROCESS_DELIVERY]),
        span(INGEST_DELIVERY, **expected[INGEST_DELIVERY]),
        span(INGEST_PARTITION, **expected[INGEST_PARTITION]),
        span(ATHENA_STATEMENT, **expected[ATHENA_STATEMENT]),
    ):
        pass

    finished = exporter.get_finished_spans()
    by_name = {finished_span.name: finished_span for finished_span in finished}
    chain = (
        REPROCESS_PROCESS,
        REPROCESS_APPLY,
        REPROCESS_DELIVERY,
        INGEST_DELIVERY,
        INGEST_PARTITION,
        ATHENA_STATEMENT,
    )
    contract = load_trace_contract(ROOT)
    declarations = {declaration.name: declaration for declaration in contract.spans}
    contexts: dict[str, SpanContext] = {}
    assert set(by_name) == set(chain)
    for span_name in chain:
        context = by_name[span_name].context
        assert context is not None
        contexts[span_name] = context

    assert {context.trace_id for context in contexts.values()} == {
        contexts[REPROCESS_PROCESS].trace_id
    }
    assert by_name[REPROCESS_PROCESS].parent is None
    for parent_name, child_name in pairwise(chain):
        child_parent = by_name[child_name].parent
        assert child_parent is not None
        assert child_parent.span_id == contexts[parent_name].span_id
    for span_name in chain:
        attributes = dict(by_name[span_name].attributes or {})
        assert attributes == expected[span_name]
        assert set(attributes) == set(declarations[span_name].required_attributes)

    provider.shutdown()


def test_checker_rejects_aliased_trace_boundary_and_span_symbols(tmp_path: Path) -> None:
    source = """
from adapters.outbound.telemetry.tracing import span as measured
from adapters.outbound.telemetry.tracing import trace_boundary as boundary

@boundary(SAMPLE_SPAN)
def run() -> None:
    with measured(SAMPLE_SPAN, operation_id="operation") as recording:
        recording.record(completed=1)
"""
    _write_tree(tmp_path, source=source)
    messages = _messages(tmp_path)
    assert "telemetry symbol span cannot be imported with an alias" in messages
    assert "telemetry symbol trace_boundary cannot be imported with an alias" in messages
    assert "missing trace_boundary implementation owner" in messages


def test_checker_rejects_multiple_matching_spans_in_one_decorated_owner(tmp_path: Path) -> None:
    source = """
@trace_boundary(SAMPLE_SPAN)
def run() -> None:
    with span(SAMPLE_SPAN, operation_id="one") as recording:
        recording.record(completed=1)
    with span(SAMPLE_SPAN, operation_id="two") as recording:
        recording.record(completed=1)
"""
    _write_tree(tmp_path, source=source)
    assert "must contain exactly one matching span; found 2" in _messages(tmp_path)


def test_checker_rejects_forbidden_source_hidden_under_allowed_attribute(tmp_path: Path) -> None:
    source = """
@trace_boundary(SAMPLE_SPAN)
def run(request) -> None:
    with span(SAMPLE_SPAN, operation_id=request.sql) as recording:
        recording.record(completed=1)
"""
    _write_tree(tmp_path, source=source)
    assert "operation_id reads forbidden source expression(s): sql" in _messages(tmp_path)


def test_checker_scans_governed_undecorated_files_for_direct_logger_calls(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    adjacent = tmp_path / "src/adapters/jobs/preset_artifact_publisher/enqueue.py"
    adjacent.parent.mkdir(parents=True)
    adjacent.write_text(
        'from aws_lambda_powertools import Logger\n\nlog = Logger()\nlog.exception("failed")\n'
    )
    assert "direct Logger.exception lifecycle call" in _messages(tmp_path)


@pytest.mark.parametrize(
    "relative_path",
    [
        "src/adapters/inbound/example.py",
        "src/adapters/jobs/example.py",
    ],
)
def test_checker_rejects_direct_logger_calls_across_adapters(
    tmp_path: Path, relative_path: str
) -> None:
    _write_tree(tmp_path)
    source_path = tmp_path / relative_path
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(
        dedent(
            """
            from aws_lambda_powertools import Logger

            audit = Logger()
            audit.error("failed")
            """
        )
    )

    assert "direct Logger.error lifecycle call" in _messages(tmp_path)


def test_checker_allows_direct_logger_calls_in_reporting_seam(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    reporting_path = tmp_path / "src/adapters/outbound/telemetry/reporting.py"
    reporting_path.parent.mkdir(parents=True, exist_ok=True)
    reporting_path.write_text(
        dedent(
            """
            from aws_lambda_powertools import Logger

            telemetry = Logger()
            telemetry.error("shared implementation")
            """
        )
    )

    assert "direct Logger.error lifecycle call" not in _messages(tmp_path)


def test_checker_allows_non_lifecycle_and_unrelated_error_calls(tmp_path: Path) -> None:
    _write_tree(
        tmp_path,
        source="""
        from aws_lambda_powertools import Logger

        telemetry = Logger()

        def run(parser, dependency) -> None:
            telemetry.inject_lambda_context()
            parser.error("invalid arguments")
            dependency.error("domain operation")
        """,
    )

    assert "direct Logger" not in _messages(tmp_path)


def test_checker_detects_aliased_powertools_logger_constructor(tmp_path: Path) -> None:
    _write_tree(
        tmp_path,
        source="""
        from aws_lambda_powertools import Logger as StructuredLogger

        audit = StructuredLogger()
        audit.warning("degraded")
        """,
    )

    assert "direct Logger.warning lifecycle call" in _messages(tmp_path)


def test_checker_requires_report_error_handler_to_flush_metrics(tmp_path: Path) -> None:
    _write_tree(
        tmp_path,
        source="""
        from adapters.outbound.telemetry.reporting import report_error

        def handler(event, context) -> None:
            report_error(RuntimeError("failed"), event.report)
        """,
    )

    assert "handler calls report_error but does not flush shared metrics" in _messages(tmp_path)


@pytest.mark.parametrize(
    "case",
    [
        (
            "from adapters.outbound.telemetry.reporting import METRICS, report_error",
            "@METRICS.log_metrics",
            'report_error(RuntimeError("failed"), event.report)',
        ),
        (
            "from adapters.outbound.telemetry.reporting import flushes_metrics, report_error",
            "@flushes_metrics",
            'report_error(RuntimeError("failed"), event.report)',
        ),
        (
            "from adapters.outbound.telemetry.reporting import flush_metrics, report_error",
            "",
            'try:\n        report_error(RuntimeError("failed"), event.report)\n'
            "    finally:\n        flush_metrics()",
        ),
    ],
)
def test_checker_accepts_report_error_handler_metric_flushes(
    tmp_path: Path, case: tuple[str, str, str]
) -> None:
    imports, decorator, body = case
    _write_tree(
        tmp_path,
        source=f"""
        {imports}

        {decorator}
        def handler(event, context) -> None:
            {body}
        """,
    )

    assert "handler calls report_error but does not flush shared metrics" not in _messages(tmp_path)


def test_checker_does_not_treat_non_handler_server_as_lambda(tmp_path: Path) -> None:
    _write_tree(
        tmp_path,
        source="""
        from adapters.outbound.telemetry.reporting import report_error

        def serve(report) -> None:
            report_error(RuntimeError("failed"), report)
        """,
    )

    assert "does not flush shared metrics" not in _messages(tmp_path)


def test_checker_requires_required_metadata_when_span_opens_not_on_one_branch(
    tmp_path: Path,
) -> None:
    source = """
@trace_boundary(SAMPLE_SPAN)
def run(complete) -> None:
    with span(SAMPLE_SPAN) as recording:
        if complete:
            recording.record(operation_id="operation")
"""
    _write_tree(tmp_path, source=source)

    assert "must provide required metadata when the span opens: operation_id" in _messages(tmp_path)


def test_checker_requires_typed_root_for_every_configured_lambda_handler(
    tmp_path: Path,
) -> None:
    source = """
from adapters.outbound.telemetry.bootstrap import configure_tracing

configure_tracing(settings)

def handler(event, context):
    return event
"""
    _write_tree(tmp_path, source=source)

    assert "traced handler has no typed trace_boundary root" in _messages(tmp_path)


def test_slice7_a_unit_span_without_its_batch_id_is_refused(tmp_path: Path) -> None:
    """Test 13's control (D37): the real unit span declaration requires `batch_id`."""
    real = cast(
        "dict[str, list[dict[str, Any]]]",
        yaml.safe_load((ROOT / "contracts/observability/spans.yaml").read_text()),
    )
    (unit,) = [item for item in real["spans"] if item["constant"] == "TABLE_WRITER_GRAPH_UNIT"]
    assert "batch_id" in unit["required_attributes"]
    assert unit["parent"] == "current"
    record: dict[str, object] = {
        **unit,
        "constant": "SAMPLE_SPAN",
        "name": "sample.span",
        "parent": "root",
        "propagation": "remote_boundary",
    }
    required = {name: '"x"' for name in unit["required_attributes"] if name != "batch_id"}
    arguments = ", ".join(f"{name}={value}" for name, value in required.items())
    source = f"""
@trace_boundary(SAMPLE_SPAN)
def run() -> None:
    with span(SAMPLE_SPAN, {arguments}) as recording:
        recording.record(completed=1)
"""
    _write_tree(tmp_path, source=source, document=_registry(record))
    assert "must provide required metadata when the span opens: batch_id" in _messages(tmp_path)
    # Control: the same call carrying `batch_id` conforms.
    good = source.replace("with span(SAMPLE_SPAN, ", 'with span(SAMPLE_SPAN, batch_id="b", ')
    _write_tree(tmp_path, source=good, document=_registry(record))
    assert "batch_id" not in _messages(tmp_path)
