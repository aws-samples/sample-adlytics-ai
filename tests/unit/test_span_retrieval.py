# SPDX-License-Identifier: MIT-0
"""The 5a span-retrieval module and CLI (impl-plan S6 item 5a; plan B5).

Every refusal asserts its exact error class, code and reason, beside a paired positive control.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from adapters.jobs.span_retrieval import __main__ as cli
from adapters.jobs.span_retrieval.client import SPAN_LOG_GROUP, LogsInsightsSpanClient
from adapters.jobs.span_retrieval.parse import SpanRetrievalFailedError, parse_span_rows
from adapters.jobs.span_retrieval.query import (
    DEFAULT_LAG_SECONDS,
    SpanQuery,
    SpanQueryRefusedError,
    build_span_query,
    declared_span_attributes,
    query_string,
)

pytestmark = pytest.mark.unit

# pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
_TRACE = "0af7651916cd43dd8448eb211c80319c"
# pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
_OTHER_TRACE = "1bf7651916cd43dd8448eb211c80319d"
_START = datetime(2026, 10, 9, 0, 0, tzinfo=UTC)
_END = _START + timedelta(hours=1)
_LONG_AFTER = _END + timedelta(days=1)
_DECLARED = declared_span_attributes()


def _span(  # noqa: PLR0913 -- one keyword per span field under test
    span_id: str,
    *,
    trace: str = _TRACE,
    name: str = "ingest.table_writer.unit",
    attributes: dict[str, object] | None = None,
    environment: str | None = "dev",
    at: int = 0,
) -> dict[str, object]:
    resource: dict[str, object] = {"attributes": {"service.name": "adlytics-dev"}}
    if environment is not None:
        resource["attributes"] = {
            "service.name": "adlytics-dev",
            "deployment.environment": environment,
        }
    return {
        "traceId": trace,
        "spanId": span_id,
        "name": name,
        "attributes": attributes or {},
        "resource": resource,
        "_at": at,
    }


def _row(span: dict[str, object]) -> list[dict[str, str]]:
    return [
        {"field": "@timestamp", "value": "2026-10-09 00:00:00.000"},
        {"field": "@message", "value": json.dumps(span)},
    ]


@dataclass
class _FakeLogs:
    """Logs Insights over a fixed span set: rows inside [startTime, endTime], up to `limit`."""

    spans: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    running_polls: int = 0
    final_status: str = "Complete"
    starts: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    _queries: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])

    def start_query(self, **request: Any) -> dict[str, Any]:
        self.starts.append(request)
        query_id = f"query-{len(self.starts)}"
        self._queries[query_id] = {**request, "polls": 0}
        return {"queryId": query_id}

    def get_query_results(self, *, queryId: str) -> dict[str, Any]:  # noqa: N803 -- the SDK name
        query = self._queries[queryId]
        query["polls"] += 1
        if query["polls"] <= self.running_polls:
            return {"status": "Running", "results": []}
        if self.final_status != "Complete":
            return {"status": self.final_status, "results": []}
        base = int(_START.timestamp())
        inside = [
            span
            for span in self.spans
            if query["startTime"] <= base + int(str(span["_at"])) <= query["endTime"]
        ]
        return {"status": "Complete", "results": [_row(span) for span in inside[: query["limit"]]]}


@dataclass
class _Clock:
    value: float = 0.0
    sleeps: list[float] = field(default_factory=list[float])

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def _client(fake: _FakeLogs, *, now: datetime = _LONG_AFTER, timeout: float = 10.0) -> Any:
    clock = _Clock()
    return LogsInsightsSpanClient(
        fake,
        poll_seconds=1.0,
        timeout_seconds=timeout,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        now=lambda: now,
    )


def _query(**overrides: Any) -> SpanQuery:
    values: dict[str, Any] = {
        "environment": "dev",
        "window_start": _START,
        "window_end": _END,
        "declared_attributes": _DECLARED,
        "trace_id": _TRACE,
    }
    values.update(overrides)
    return build_span_query(**values)


# --- the query builder -------------------------------------------------------------------------


def test_the_builder_renders_a_trace_lookup_over_the_explicit_window() -> None:
    query = _query(span_names=["ingest.table_writer.unit"], limit=50)
    assert query_string(query) == (
        "fields @timestamp, @message "
        f"| filter traceId = '{_TRACE}' and (name = 'ingest.table_writer.unit') "
        "| sort @timestamp asc | limit 50"
    )
    assert query.lag_seconds == DEFAULT_LAG_SECONDS == 600


def test_the_builder_renders_declared_business_keys_and_name_prefixes() -> None:
    query = _query(
        trace_id=None,
        keys=[("operation_id", "op-1"), ("execution_id", "e" * 32)],
        span_name_prefixes=["Secrets Manager."],
    )
    assert query_string(query) == (
        "fields @timestamp, @message "
        f"| filter attributes.execution_id = '{'e' * 32}' and attributes.operation_id = 'op-1' "
        "and (name like /^Secrets\\ Manager\\./) | sort @timestamp asc | limit 1000"
    )


def test_an_undeclared_key_is_refused_and_a_declared_key_builds() -> None:
    assert "undeclared_attr" not in _DECLARED
    with pytest.raises(SpanQueryRefusedError) as refused:
        _query(keys=[("undeclared_attr", "x")])
    assert str(refused.value.code) == "ADLY-SPAN_RETRIEVAL-0001"
    assert refused.value.reason == "undeclared_key"
    # Control: an attribute the contract declares builds.
    assert "execution_id" in _DECLARED
    assert _query(keys=[("execution_id", "e" * 32)]).keys == (("execution_id", "e" * 32),)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"trace_id": None}, "selector_missing"),
        ({"trace_id": "NOT-A-TRACE"}, "trace_id_malformed"),
        ({"keys": [("execution_id", "x' or name = 'y")]}, "key_value_malformed"),
        ({"span_names": ["name' or 1=1"]}, "span_name_malformed"),
        ({"span_name_prefixes": ["a/b"]}, "span_name_malformed"),
        ({"window_end": _START}, "window_empty"),
        ({"window_start": _START.replace(tzinfo=None)}, "window_naive"),
        ({"window_end": _START + timedelta(days=15)}, "window_too_wide"),
        ({"limit": 0}, "limit_out_of_bound"),
        ({"limit": 10_001}, "limit_out_of_bound"),
        ({"max_pages": 65}, "max_pages_out_of_bound"),
        ({"lag_seconds": -1}, "lag_out_of_bound"),
        ({"environment": "Dev Prod"}, "environment_malformed"),
    ],
)
def test_malformed_lookups_are_refused_before_any_read(
    overrides: dict[str, Any], reason: str
) -> None:
    with pytest.raises(SpanQueryRefusedError) as refused:
        _query(**overrides)
    assert str(refused.value.code) == "ADLY-SPAN_RETRIEVAL-0001"
    assert refused.value.reason == reason


def test_the_bounds_accept_their_extremes() -> None:
    query = _query(limit=10_000, max_pages=64, lag_seconds=0)
    assert (query.limit, query.max_pages, query.lag_seconds) == (10_000, 64, 0)


# --- the parser --------------------------------------------------------------------------------


def test_the_parser_returns_span_documents_in_row_order() -> None:
    first, second = _span("1" * 16, name="a"), _span("2" * 16, name="b")
    documents = parse_span_rows([_row(first), _row(second)])
    assert [document["name"] for document in documents] == ["a", "b"]
    assert documents[0]["traceId"] == _TRACE


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        ([[{"field": "@message", "value": "not json"}]], "span_document_malformed"),
        ([[{"field": "@timestamp", "value": "x"}]], "span_message_missing"),
        ([_row({**_span("1" * 16), "spanId": "short"})], "span_identity_malformed"),
        ("not a list", "query_result_malformed"),
    ],
)
def test_the_parser_refuses_a_record_that_is_not_a_span(rows: object, reason: str) -> None:
    with pytest.raises(SpanRetrievalFailedError) as refused:
        parse_span_rows(rows)
    assert str(refused.value.code) == "ADLY-SPAN_RETRIEVAL-0003"
    assert refused.value.reason == reason


# --- exclusion, states and bounds --------------------------------------------------------------


def test_a_span_of_another_trace_is_excluded_and_the_matching_span_is_returned() -> None:
    fake = _FakeLogs([_span("1" * 16, trace=_OTHER_TRACE), _span("2" * 16)])
    result = _client(fake).retrieve(_query())
    assert [span["spanId"] for span in result["spans"]] == ["2" * 16]
    assert result["status"] == "complete"
    assert result["excluded"] == 1


def test_a_span_of_another_key_is_excluded_and_the_matching_key_is_returned() -> None:
    fake = _FakeLogs(
        [
            _span("1" * 16, attributes={"execution_id": "f" * 32}),
            _span("2" * 16, attributes={"execution_id": "e" * 32}),
        ]
    )
    result = _client(fake).retrieve(_query(trace_id=None, keys=[("execution_id", "e" * 32)]))
    assert [span["spanId"] for span in result["spans"]] == ["2" * 16]
    assert result["excluded"] == 1


def test_a_span_of_another_environment_is_excluded_and_a_managed_span_is_kept() -> None:
    fake = _FakeLogs(
        [
            _span("1" * 16, environment="prod"),
            _span("2" * 16, environment="dev"),
            _span("3" * 16, environment=None),
        ]
    )
    result = _client(fake).retrieve(_query())
    assert [span["spanId"] for span in result["spans"]] == ["2" * 16, "3" * 16]


def test_a_query_still_running_at_the_poll_bound_is_pending() -> None:
    fake = _FakeLogs([_span("1" * 16)], running_polls=100)
    result = _client(fake, timeout=5.0).retrieve(_query())
    assert (result["status"], result["reason"]) == ("pending", "query_running")
    assert result["spans"] == []
    # Control: the same query completing inside the bound is complete.
    done = _client(_FakeLogs([_span("1" * 16)], running_polls=2), timeout=5.0).retrieve(_query())
    assert done["status"] == "complete"


def test_an_empty_result_inside_the_lag_is_pending_and_outside_it_is_empty() -> None:
    inside = _client(_FakeLogs(), now=_END + timedelta(seconds=599)).retrieve(_query())
    assert (inside["status"], inside["reason"]) == ("pending", "inside_lag")
    assert inside["window"] == {
        "start": _START.isoformat(),
        "end": _END.isoformat(),
        "lag_seconds": 600,
        "inside_lag": True,
    }
    # Control: the same empty result once the window is older than the lag bound.
    outside = _client(_FakeLogs(), now=_END + timedelta(seconds=601)).retrieve(_query())
    assert (outside["status"], outside["reason"]) == ("empty", None)


def test_a_failed_query_raises_its_exact_error() -> None:
    with pytest.raises(SpanRetrievalFailedError) as failed:
        _client(_FakeLogs(final_status="Failed")).retrieve(_query())
    assert str(failed.value.code) == "ADLY-SPAN_RETRIEVAL-0002"
    assert failed.value.reason == "query_failed"


def test_one_page_reads_the_half_open_window_from_aws_spans() -> None:
    fake = _FakeLogs([_span("1" * 16)])
    result = _client(fake).retrieve(_query(limit=10))
    assert result["pages"] == 1
    assert fake.starts == [
        {
            "logGroupName": SPAN_LOG_GROUP,
            "startTime": int(_START.timestamp()),
            "endTime": int(_END.timestamp()) - 1,
            "queryString": query_string(_query(limit=10)),
            "limit": 10,
        }
    ]


def test_a_full_page_splits_its_window_and_keeps_time_order() -> None:
    spans = [_span(f"{index:016x}", at=index * 600) for index in range(1, 6)]
    fake = _FakeLogs(spans)
    result = _client(fake).retrieve(_query(limit=3, max_pages=8))
    assert [span["spanId"] for span in result["spans"]] == [str(s["spanId"]) for s in spans]
    assert result["truncated"] is False
    assert result["pages"] == 5


def test_the_page_bound_keeps_a_full_page_and_marks_the_lookup_truncated() -> None:
    spans = [_span(f"{index:016x}", at=index * 600) for index in range(1, 6)]
    result = _client(_FakeLogs(spans)).retrieve(_query(limit=3, max_pages=1))
    assert (result["pages"], result["truncated"], result["status"]) == (1, True, "complete")
    assert len(result["spans"]) == 3
    # Control: enough pages read every span, untruncated.
    whole = _client(_FakeLogs(spans)).retrieve(_query(limit=3, max_pages=8))
    assert (len(whole["spans"]), whole["truncated"]) == (5, False)


def test_a_truncated_read_with_nothing_matching_is_pending_never_empty() -> None:
    others = [_span(f"{index:016x}", trace=_OTHER_TRACE, at=index * 600) for index in range(1, 6)]
    result = _client(_FakeLogs(others)).retrieve(_query(limit=3, max_pages=1))
    assert (result["status"], result["reason"], result["excluded"]) == (
        "pending",
        "page_bound_reached",
        3,
    )
    # Control: the same non-matching spans read in full are an `empty` window.
    whole = _client(_FakeLogs(others)).retrieve(_query(limit=10, max_pages=1))
    assert (whole["status"], whole["truncated"]) == ("empty", False)


def test_a_full_one_second_window_is_kept_and_marked_truncated() -> None:
    spans = [_span(f"{index:016x}", at=10) for index in range(1, 4)]
    query = _query(
        window_start=_START + timedelta(seconds=10),
        window_end=_START + timedelta(seconds=11),
        limit=2,
    )
    result = _client(_FakeLogs(spans)).retrieve(query)
    assert result["truncated"] is True
    assert len(result["spans"]) == 2


# --- the CLI -----------------------------------------------------------------------------------


_CLI_TARGET = ["--profile", "fictional-profile", "--region", "us-east-1", "--env", "dev"]
_CLI_LOOKUP = [
    "--window-start",
    "2026-10-09T00:00:00Z",
    "--window-end",
    "2026-10-09T01:00:00Z",
    "--trace-id",
    _TRACE,
]


@pytest.mark.parametrize("missing", ["--profile", "--region", "--env"])
def test_the_cli_refuses_a_missing_target_input(
    missing: str, capsys: pytest.CaptureFixture[str]
) -> None:
    index = _CLI_TARGET.index(missing)
    argv = [*_CLI_TARGET[:index], *_CLI_TARGET[index + 2 :], *_CLI_LOOKUP]
    with pytest.raises(SystemExit) as refused:
        cli.main(argv)
    assert refused.value.code == 2
    assert f"the following arguments are required: {missing}" in capsys.readouterr().err


def test_the_cli_with_every_target_input_prints_one_result_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    arguments = cli._parser().parse_args([*_CLI_TARGET, *_CLI_LOOKUP])  # pyright: ignore[reportPrivateUsage]
    status = cli.run(arguments, logs=_FakeLogs([_span("1" * 16)]))
    lines = capsys.readouterr().out.splitlines()
    assert status == 0
    assert len(lines) == 1 and lines[0].startswith("RESULT ")
    result = json.loads(lines[0].removeprefix("RESULT "))
    assert (result["status"], result["region"]) == ("complete", "us-east-1")
    assert result["query"]["environment"] == "dev"


def test_the_cli_refuses_the_default_profile_and_an_undeclared_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    default = ["--profile", "default", *_CLI_TARGET[2:], *_CLI_LOOKUP]
    assert cli.run(cli._parser().parse_args(default), logs=_FakeLogs()) == 3  # pyright: ignore[reportPrivateUsage]
    undeclared = [*_CLI_TARGET, *_CLI_LOOKUP, "--key", "undeclared_attr=x"]
    assert cli.run(cli._parser().parse_args(undeclared), logs=_FakeLogs()) == 3  # pyright: ignore[reportPrivateUsage]
    results = [
        json.loads(line.removeprefix("RESULT ")) for line in capsys.readouterr().out.splitlines()
    ]
    assert results == [
        {"code": "ADLY-SPAN_RETRIEVAL-0001", "refused": "profile_not_explicit"},
        {"code": "ADLY-SPAN_RETRIEVAL-0001", "refused": "undeclared_key"},
    ]
