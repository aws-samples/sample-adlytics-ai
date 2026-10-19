# SPDX-License-Identifier: MIT-0
"""Protected no-store operational-status read API routing and absence collapsing."""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from operational_status_support import terminal_event, uuid_text

from adapters.config import operational_api_settings, run_request_api_settings, tracing_settings
from adapters.outbound.dynamodb.operational_status import encode_cursor
from adapters.outbound.telemetry import bootstrap, reporting
from domain.operations.generated_data_flows import SURFACES
from domain.operations.status import BaselineWindow, event_document
from ports.outbound.operational_status import EventPage

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-073")]

_ENV = {
    "ADL_OPERATIONAL_STATUS_TABLE": "unit-test-operational-status",
    "ADL_EXECUTION_GRAPH_TABLE": "unit-test-execution-graph",
    "ADL_RAW_BUCKET": "unit-test-raw",
    "AWS_DEFAULT_REGION": "us-east-1",
}

FLOW_ID = uuid_text(2)
FLOWS = "/api/operations/data-flows"
DEFINITIONS = "/api/operations/data-flow-definitions"


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Import production composition under fictional module-level settings."""
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)
    operational_api_settings.cache_clear()
    run_request_api_settings.cache_clear()
    tracing_settings.cache_clear()
    return cast("Any", importlib.import_module("adapters.inbound.operational_status.handler"))


class _Context:
    function_name = "operational-status-api"
    function_version = "$LATEST"
    invoked_function_arn = "arn:aws:lambda:us-east-1:111122223333:function:status-api"
    memory_limit_in_mb = 512
    aws_request_id = "request-1"


@dataclass
class _Store:
    """Answer the projection port reads the routed handler performs."""

    snapshot: dict[str, object] | None = None
    page: EventPage | None = None
    flow_reads: list[str] = field(default_factory=list[str])
    event_reads: list[tuple[str, str, int]] = field(default_factory=list[tuple[str, str, int]])

    def flow(self, flow_id: str) -> dict[str, object] | None:
        self.flow_reads.append(flow_id)
        return self.snapshot

    def events(self, flow_id: str, cursor: str, limit: int) -> EventPage | None:
        self.event_reads.append((flow_id, cursor, limit))
        return self.page

    def surface(self, surface: str, limit: int) -> tuple[dict[str, object], ...]:
        del surface, limit
        return ()

    def baselines(self, surface: str, measurement_ref: str) -> tuple[BaselineWindow, ...]:
        del surface, measurement_ref
        return ()


def _request(path: str, query: dict[str, str] | None = None, **overrides: Any) -> dict[str, Any]:
    lambda_context: dict[str, Any] = {
        "can_read_operational_status": overrides.get("entitlement", "true"),
        "subject": "operator-1",
    }
    return {
        "requestContext": {
            "http": {"method": overrides.get("method", "GET"), "path": path},
            "authorizer": {"lambda": lambda_context},
        },
        "queryStringParameters": query,
    }


def _call(api: Any, event: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    result = api.handler(event, _Context())
    assert result["headers"]["Cache-Control"] == "no-store"
    return result["statusCode"], cast("dict[str, Any]", json.loads(result["body"]))


NOT_FOUND = {"error": "not_found"}


class TestAccessAndRouting:
    def test_a_missing_entitlement_collapses_to_not_found(self, api: Any) -> None:
        status, body = _call(api, _request(DEFINITIONS, entitlement="false"))

        assert (status, body) == (404, NOT_FOUND)

    @pytest.mark.covers("ADR-115")
    def test_the_feed_summary_is_404_without_the_entitlement_and_served_with_it(
        self, api: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ADR-115 decision 8: `/me` saying "provisioned" grants nothing; the API decides alone.
        monkeypatch.setattr(api, "_STORE", _Store())
        assert _call(api, _request(FLOWS, entitlement="false")) == (404, NOT_FOUND)
        # Positive control: the same request, entitled, is served.
        status, body = _call(api, _request(FLOWS, entitlement="true"))
        assert status == 200
        assert body != NOT_FOUND

    def test_an_event_without_an_authorizer_collapses_to_not_found(self, api: Any) -> None:
        status, body = _call(api, {"requestContext": {"http": {"method": "GET", "path": FLOWS}}})

        assert (status, body) == (404, NOT_FOUND)

    def test_a_mutating_method_collapses_to_not_found(self, api: Any) -> None:
        status, body = _call(api, _request(FLOWS, method="POST"))

        assert (status, body) == (404, NOT_FOUND)

    def test_an_unknown_route_collapses_to_not_found(self, api: Any) -> None:
        status, body = _call(api, _request("/api/operations/other"))

        assert (status, body) == (404, NOT_FOUND)

    def test_a_non_uuid_flow_id_collapses_to_not_found(self, api: Any) -> None:
        status, body = _call(api, _request(f"{FLOWS}/not-a-uuid"))

        assert (status, body) == (404, NOT_FOUND)

    def test_the_events_route_requires_cursor_and_limit(self, api: Any) -> None:
        status, body = _call(api, _request(f"{FLOWS}/{FLOW_ID}/events", query={"limit": "5"}))

        assert (status, body) == (404, NOT_FOUND)

    def test_a_non_numeric_event_limit_collapses_to_not_found(self, api: Any) -> None:
        query = {"cursor": encode_cursor(FLOW_ID, 0), "limit": "many"}

        status, body = _call(api, _request(f"{FLOWS}/{FLOW_ID}/events", query=query))

        assert (status, body) == (404, NOT_FOUND)


class TestDefinitions:
    def test_the_definitions_route_returns_the_closed_usage_guide(self, api: Any) -> None:
        status, body = _call(api, _request(DEFINITIONS))

        assert status == 200
        assert body["schema_version"] == "1"
        assert body["observation_policy"] == "authority_backed_observation_only"
        assert body["evaluation_modes"] == ["observation_only"]
        assert "running" in body["states"]
        assert len(body["surfaces"]) == len(SURFACES)

    def test_the_definitions_route_accepts_no_query_parameters(self, api: Any) -> None:
        status, body = _call(api, _request(DEFINITIONS, query={"surface": "preset_bake"}))

        assert (status, body) == (404, NOT_FOUND)


class TestFlowRoutes:
    def test_a_known_flow_returns_its_snapshot_projection(
        self, api: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        event = terminal_event(flow_id=FLOW_ID)
        monkeypatch.setattr(api, "_STORE", _Store(snapshot=event_document(event)))

        status, body = _call(api, _request(f"{FLOWS}/{FLOW_ID}"))

        assert status == 200
        assert body["flow_id"] == FLOW_ID
        assert body["latest_sequence"] == 1
        assert body["state"] == "completed"
        assert body["outcome"] == "succeeded"
        assert body["evaluation_mode"] == "observation_only"
        assert body["next_action"] == "observe"
        assert body["events_url"] == (
            f"{FLOWS}/{FLOW_ID}/events?cursor={encode_cursor(FLOW_ID, 0)}"
        )

    def test_an_absent_flow_collapses_to_not_found(
        self, api: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(api, "_STORE", _Store(snapshot=None))

        status, body = _call(api, _request(f"{FLOWS}/{FLOW_ID}"))

        assert (status, body) == (404, NOT_FOUND)

    def test_the_events_route_projects_applied_history_through_the_cursor(
        self, api: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        event = terminal_event(flow_id=FLOW_ID)
        store = _Store(page=EventPage((event,), next_cursor=None))
        monkeypatch.setattr(api, "_STORE", store)
        cursor = encode_cursor(FLOW_ID, 0)

        status, body = _call(
            api, _request(f"{FLOWS}/{FLOW_ID}/events", query={"cursor": cursor, "limit": "5"})
        )

        assert status == 200
        assert store.event_reads == [(FLOW_ID, cursor, 5)]
        (projected,) = body["events"]
        assert projected["event_id"] == event.event_id
        assert projected["transition_sequence"] == 1
        assert projected["state"] == "completed"
        assert body["next_cursor"] is None

    def test_an_invalid_cursor_page_collapses_to_not_found(
        self, api: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(api, "_STORE", _Store(page=None))
        query = {"cursor": "forged-cursor", "limit": "5"}

        status, body = _call(api, _request(f"{FLOWS}/{FLOW_ID}/events", query=query))

        assert (status, body) == (404, NOT_FOUND)

    def test_the_summary_reports_unknown_state_when_nothing_is_observed(
        self, api: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(api, "_STORE", _Store())

        status, body = _call(api, _request(FLOWS))

        assert status == 200
        assert body["aggregate_lifecycle_state"] == "unknown"
        assert body["evidence"] == "unavailable"
        assert len(body["surfaces"]) == len(SURFACES)
        assert body["registry"]["version"] == "1"


EXPLANATIONS = "/api/operations/execution-explanations"


def _post(body: object, **overrides: Any) -> dict[str, Any]:
    event = _request(EXPLANATIONS, method="POST", **overrides)
    event["body"] = body if isinstance(body, str) else json.dumps(body)
    return event


@pytest.fixture
def explained(api: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, list[BaseException]]:
    """Route the explanation to real admission output; count reports and flushes."""
    from execution_explanation_support import (  # noqa: PLC0415
        LAST_MODIFIED,
        PROBE_KEY,
        VERSION_ID,
        FakeMac,
        FakeVersions,
        seeded_graph_store,
    )

    from adapters.inbound.operational_status.explanation import (  # noqa: PLC0415
        ExplanationDependencies,
    )

    seeded = seeded_graph_store(("p1-req-a", "p1-req-b", "p1-req-c"))
    seeded.project_all()
    monkeypatch.setattr(
        api,
        "_EXPLANATION",
        ExplanationDependencies(
            authority=seeded.reader,
            projection=seeded.projection,
            manifests=FakeVersions({PROBE_KEY: [(VERSION_ID, LAST_MODIFIED)]}),
            cursor_mac=FakeMac(),
        ),
    )
    reports: list[BaseException] = []

    def _record(error: BaseException, _report: object) -> None:
        reports.append(error)

    monkeypatch.setattr(api, "report_error", _record)
    return api, seeded, reports


def _manifest(**extra: Any) -> dict[str, Any]:
    from execution_explanation_support import PROBE_KEY, VERSION_ID  # noqa: PLC0415

    return {"manifest": {"key": PROBE_KEY, "version_id": VERSION_ID}, **extra}


class TestExecutionExplanations:
    def test_an_entitled_request_returns_the_tree_with_no_error_report(
        self, explained: tuple[Any, Any, list[BaseException]]
    ) -> None:
        api, seeded, reports = explained

        status, body = _call(api, _post(_manifest(limit=2)))

        assert status == 200
        assert body["graphs"][0]["label"] == "shadow"
        assert body["stages"][0]["flow_root_elapsed_ms"] == 0
        assert [r["request_id"] for r in body["receipts"]] == ["p1-req-a", "p1-req-b"]
        assert isinstance(body["next_cursor"], str)
        assert body["node_failures"] == []  # slice 7 (12e): additive, returned whole
        assert reports == []
        del seeded

    def test_two_pages_cover_the_collection_and_a_retry_is_byte_identical(
        self, explained: tuple[Any, Any, list[BaseException]]
    ) -> None:
        api, seeded, _ = explained
        writes = seeded.graph.writes()

        _, first = _call(api, _post(_manifest(limit=2)))
        _, retry = _call(api, _post(_manifest(limit=2)))
        _, second = _call(api, _post(_manifest(limit=2, cursor=first["next_cursor"])))

        assert first["next_cursor"] == retry["next_cursor"]
        names = [r["request_id"] for r in first["receipts"] + second["receipts"]]
        assert names == ["p1-req-a", "p1-req-b", "p1-req-c"]
        assert second["next_cursor"] is None
        assert seeded.graph.writes() == writes

    def test_bf5_an_oversize_continuation_is_cursor_capacity_limited_with_zero_writes(
        self, explained: tuple[Any, Any, list[BaseException]]
    ) -> None:
        api, seeded, reports = explained
        writes = seeded.graph.writes()

        status, body = _call(api, _post(_manifest(cursor="A" * 2049)))

        assert (status, body) == (413, {"error": "CURSOR_CAPACITY_LIMITED"})
        assert len(reports) == 1
        assert seeded.graph.writes() == writes

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ("not json", (400, "invalid_reference")),
            ({"flow_id": "nope"}, (400, "invalid_reference")),
            ({"flow_id": "00000000-0000-5000-8000-000000000000"}, (404, "flow_not_found")),
            (
                {"manifest": {"key": "a/b/retrieved=20261006T092520Z/manifest.json"}},
                (404, "manifest_not_found"),
            ),
        ],
    )
    def test_each_typed_failure_is_reported_exactly_once(
        self,
        explained: tuple[Any, Any, list[BaseException]],
        body: object,
        expected: tuple[int, str],
    ) -> None:
        api, _, reports = explained

        status, document = _call(api, _post(body))

        assert (status, document) == (expected[0], {"error": expected[1]})
        assert len(reports) == 1

    def test_an_unauthorized_request_keeps_the_absence_collapse_and_is_reported_once(
        self, explained: tuple[Any, Any, list[BaseException]]
    ) -> None:
        api, seeded, reports = explained
        batch_reads = seeded.graph.batch_reads

        status, body = _call(api, _post(_manifest(), entitlement="false"))

        assert (status, body) == (404, NOT_FOUND)
        assert len(reports) == 1
        assert seeded.graph.batch_reads == batch_reads

    def test_an_unexpected_failure_is_reported_once_and_unwinds_both_flushes(
        self, explained: tuple[Any, Any, list[BaseException]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api, _, reports = explained
        flushed: list[str] = []
        monkeypatch.setattr(reporting, "flush_metrics", lambda: flushed.append("metrics"))

        def _flush_traces(*_: object) -> None:
            flushed.append("traces")

        monkeypatch.setattr(bootstrap, "flush_traces", _flush_traces)

        def _boom(*_: object) -> object:
            raise RuntimeError("fictional failure")

        monkeypatch.setattr(api, "explain", _boom)

        status, body = _call(api, _post(_manifest()))

        assert (status, body) == (500, {"error": "unavailable"})
        assert len(reports) == 1
        assert sorted(flushed) == ["metrics", "traces"]

    def test_get_on_the_explanation_route_is_not_routed(
        self, explained: tuple[Any, Any, list[BaseException]]
    ) -> None:
        api, _, _ = explained

        status, body = _call(api, _request(EXPLANATIONS))

        assert (status, body) == (404, NOT_FOUND)
