# SPDX-License-Identifier: MIT-0
"""Unit contract for the turn latency probe's hop derivation: differences of measured points."""

from __future__ import annotations

import datetime
import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from dynamodb_reserved_words import guard_reserved_words

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))

from validation import probe_turn_latency as probe  # noqa: E402

pytestmark = pytest.mark.unit


def test_each_hop_is_the_difference_of_two_measured_milestones() -> None:
    times = probe.ClientTimes(headers_ms=560, terminal_ms=9_900, stream_end_ms=9_960)
    timing: dict[str, object] = {
        "ms_admitted": 1,
        "ms_input_assessed": 190,
        "ms_harness_token_exchanged": 300,
        "ms_response_started": 305,
        "ms_harness_invoked": 306,
        "ms_harness_stream_opened": 2_800,
        "ms_first_harness_event": 6_900,
        "ms_first_model_text": 6_900,
        "ms_last_tool_result": 8_000,
        "ms_first_answer_text": 9_200,
        "ms_end_turn": 9_400,
        "ms_receipt_written": 9_620,
        "ms_stream_closed": 9_630,
        "tools": [{"tool": "measurement_top_n", "input_started_ms": 7_100, "requested_ms": 7_300}],
    }
    spans = [{"name": "Bedrock Runtime.ApplyGuardrail", "duration_ms": 185}]

    derived = probe._breakdown(times, timing, spans)  # pyright: ignore[reportPrivateUsage]

    assert derived["client_headers_minus_route_response_started_ms"] == 255
    assert derived["input_guardrail_ms"] == 189
    assert derived["input_guardrail_span_ms"] == 185
    assert derived["harness_token_exchange_ms"] == 110
    assert derived["invoke_harness_call_ms"] == 2_494
    assert derived["stream_open_to_first_event_ms"] == 4_100
    assert derived["tool_input_write_ms"] == [200]
    # A call whose result never arrived has no round trip rather than a guessed one.
    assert derived["tool_round_trips_ms"] == [None]
    assert derived["last_tool_result_to_answer_text_ms"] == 1_200
    assert derived["end_turn_to_receipt_ms"] == 220
    assert derived["client_terminal_minus_route_stream_closed_ms"] == 270
    # The browser ends the turn at the body's end, after the route's flush.
    assert derived["client_done_to_stream_end_ms"] == 60


def test_a_turn_without_a_timing_line_derives_nothing() -> None:
    derived = probe._breakdown(probe.ClientTimes(), {}, [])  # pyright: ignore[reportPrivateUsage]

    assert all(value in (None, []) for value in derived.values())


def _span(name: str, start_ms: float, duration_ms: float) -> dict[str, object]:
    return {
        "name": name,
        "startTimeUnixNano": str(int(start_ms * 1e6)),
        "durationNano": str(int(duration_ms * 1e6)),
    }


def test_harness_segments_split_the_work_before_and_between_model_calls() -> None:
    spans = [
        _span("POST /invocations", 1_000, 9_000),
        _span("Bedrock AgentCore.GetResourceOauth2Token", 1_300, 250),
        _span("mcp tools/list", 1_730, 360),
        _span("Bedrock AgentCore.ListEvents", 2_280, 55),
        _span("Bedrock AgentCore.ListEvents", 2_335, 50),
        _span("Bedrock AgentCore.CreateEvent", 2_385, 100),
        _span("Bedrock AgentCore.CreateEvent", 2_485, 100),
        _span("Bedrock AgentCore.RetrieveMemoryRecords", 2_590, 250),
        _span("Bedrock AgentCore.RetrieveMemoryRecords", 2_591, 270),
        # Strands wraps each botocore model call in a bare `chat` span; only the inner counts.
        _span("chat", 2_864, 2_002),
        _span("chat us.fictional-model", 2_865, 2_000),
        _span("mcp tools/call measurement___measurement_top_n", 4_900, 800),
        _span("Bedrock AgentCore.CreateEvent", 5_700, 100),
        _span("Bedrock AgentCore.CreateEvent", 5_800, 110),
        _span("chat us.fictional-model", 5_915, 3_000),
    ]

    segments = probe.harness_segments(spans)

    assert segments["post_to_first_model_call_ms"] == 1_865
    assert segments["gateway_token_ms"] == 250
    assert segments["token_end_to_tools_list_ms"] == 180
    assert segments["tools_list_ms"] == 360
    # Only the reads and writes before the first model call, one after another.
    assert segments["short_term_memory_serial_ms"] == 305
    assert segments["long_term_memory_parallel_ms"] == 271
    assert segments["model_calls_ms"] == [2_000, 3_000]
    assert segments["tool_calls_ms"] == [800]
    assert segments["tool_result_to_next_model_call_ms"] == [215]
    assert segments["harness_total_ms"] == 9_000
    assert segments["post_to_token_request_ms"] == 300
    # Of 1000..2865, the token (250), tools/list (360), and memory 2280-2585 and 2590-2861
    # (305 + 271) are covered; the bare `chat` wrapper covers nothing.
    assert segments["uncovered_before_first_model_call_ms"] == 1_865 - 250 - 360 - 305 - 271


def test_cross_clock_anchors_route_milestones_on_its_root_span() -> None:
    segments = {"post_start_epoch_ms": 10_500, "model_call_starts_epoch_ms": [12_000, 15_000]}
    route_spans = [
        {"name": "conversation.turn", "start_epoch_ms": 10_000},
        {"name": "Bedrock AgentCore.InvokeHarness", "start_epoch_ms": 10_300},
    ]
    timing = {"ms_first_model_text": 3_300, "ms_first_answer_text": 6_400}

    joined = probe.cross_clock(segments, route_spans=route_spans, timing=timing)

    assert joined == {
        "invoke_harness_to_post_ms": 200,
        "model_time_to_first_token_ms": 1_300,
        "model_time_to_answer_token_ms": 1_400,
    }


class _FakeLogs:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self.events_returned = events
        self.requests: list[dict[str, object]] = []

    def events(self, group: str, **request: object) -> list[dict[str, object]]:
        self.requests.append({"group": group, **request})
        return self.events_returned


# pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
_TRACE = "0af7651916cd43dd8448eb211c80319c"
# pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
_OTHER_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
_SERVICE = "harness_adlytics_dev_agent.DEFAULT"


def _span_event(name: str, *, trace_id: str, service: str) -> dict[str, object]:
    span = {
        "name": name,
        "traceId": trace_id,
        "resource": {"attributes": {"service.name": service}},
    }
    return {"message": json.dumps(span)}


def _read_harness_trace(fake: _FakeLogs) -> list[dict[str, object]]:
    return probe._harness_trace(  # pyright: ignore[reportPrivateUsage]
        fake,  # pyright: ignore[reportArgumentType]
        trace_id=_TRACE,
        service_prefix=probe.harness_service_prefix("dev"),
        times=probe.ClientTimes(sent_epoch_ms=1_000, ended_epoch_ms=9_000),
    )


def test_the_harness_trace_is_read_from_aws_spans_by_trace_id() -> None:
    fake = _FakeLogs(
        [_span_event("POST /invocations", trace_id=_TRACE, service=_SERVICE), {"message": "x"}]
    )
    spans = _read_harness_trace(fake)
    assert [span["name"] for span in spans] == ["POST /invocations"]
    assert fake.requests == [
        {
            "group": "aws/spans",
            "start_ms": 0,
            "end_ms": 39_000,
            # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
            "pattern": '{ $.traceId = "0af7651916cd43dd8448eb211c80319c" }',
        }
    ]


def test_a_span_of_another_trace_is_refused_even_if_the_read_returns_it() -> None:
    fake = _FakeLogs(
        [
            _span_event("chat", trace_id=_OTHER_TRACE, service=_SERVICE),
            _span_event("chat", trace_id=_TRACE, service=_SERVICE),
        ]
    )
    spans = _read_harness_trace(fake)
    assert [span["traceId"] for span in spans] == [_TRACE]


def test_a_route_span_of_the_same_trace_is_not_a_harness_span() -> None:
    fake = _FakeLogs(
        [
            _span_event("conversation.turn", trace_id=_TRACE, service="adlytics-dev"),
            _span_event("chat", trace_id=_TRACE, service=_SERVICE),
        ]
    )
    assert [span["name"] for span in _read_harness_trace(fake)] == ["chat"]


def test_harness_segments_are_empty_without_an_invocation_and_a_model_call() -> None:
    assert probe.harness_segments([_span("mcp tools/list", 0, 300)]) == {}


# --- D8 paired chat: the committed manifest, six-row drafts, and post-bake binding -----------

_MANIFEST = _ROOT / "validation" / "fixtures" / "chat-bake-acceptance-v2.json"
_OUTSIDE = "campaign:fe4dad179386b2c2d214b787"
_QUERY = "48f1b741-cef2-4abd-9223-fbf1ca06ec0a"
_SELECTION = {
    "outside_list_item": _OUTSIDE,
    "outside_selection_sha256": "5" * 64,
    "outside_selection_query_execution_id": _QUERY,
    "outside_list_present": True,
    "target_unit": "b" * 64,
    "target_generation": "a" * 64,
}


def _filled() -> probe.ChatManifest:
    return probe.with_outside_list_item(probe.load_chat_manifest(_MANIFEST), _OUTSIDE)


def test_the_committed_manifest_is_the_five_prior_prompts_plus_the_outside_control() -> None:
    manifest = probe.load_chat_manifest(_MANIFEST)
    assert [step.label for step in manifest.steps] == [
        "greeting_new",
        "greeting_follow_up",
        "catalog_new",
        "measurement_new",
        "measurement_follow_up",
        "outside_bounded_campaign",
    ]
    assert manifest.outside_list_item is None
    assert manifest.persona_id == "probe-a" and len(manifest.sha256) == 64
    filled = probe.with_outside_list_item(manifest, _OUTSIDE)
    assert filled.outside_list_item == _OUTSIDE and filled.sha256 == manifest.sha256
    assert filled.steps[:5] == manifest.steps[:5]
    assert filled.steps[5].question.endswith(f"campaign {_OUTSIDE}.")
    assert probe.OUTSIDE_PLACEHOLDER not in filled.steps[5].question


@pytest.mark.parametrize("item", ["", "campaign:", "advertiser:a", "campaign:a b", "{x}"])
def test_a_malformed_outside_item_is_never_filled_in(item: str) -> None:
    with pytest.raises(probe.SampleError, match="canonical campaign key"):
        probe.with_outside_list_item(probe.load_chat_manifest(_MANIFEST), item)
    with pytest.raises(probe.SampleError, match="already filled"):
        probe.with_outside_list_item(_filled(), _OUTSIDE)


_ACCEPTED = "a" * 64
# pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
_WINDOW = "amc/dsp_impressions/marketplace=ATVPDKIKX0DER/window=2026-10-01_2026-10-03/options=x/"
_TARGET = probe.window_unit(_WINDOW)
_CATALOG = [f"campaign:{index:02x}" for index in range(30)]
_GENERATION = [*_CATALOG[:29], "campaign:ff"]  # the last catalog key has no impressions here


def test_the_selection_is_the_last_catalog_campaign_with_impressions_in_the_generation() -> None:
    selected = probe.select_outside_list_item(generation_keys=_GENERATION, catalog_keys=_CATALOG)
    assert selected == "campaign:1c"  # campaign:1d is served but has no impressions in the window


@pytest.mark.parametrize(
    ("generation", "catalog", "message"),
    [
        (["campaign:ff"], _CATALOG, "no catalog campaign"),
        (_GENERATION, list(reversed(_CATALOG)), "key order"),
        (_GENERATION, [*_CATALOG, _CATALOG[0]], "repeated"),
        (_GENERATION, ["advertiser:a", *_CATALOG], "non-campaign"),
        (_GENERATION, [["campaign:00"], *_CATALOG], "non-campaign"),
        ("campaign:00", _CATALOG, "not a key list"),
    ],
    ids=["disjoint", "unsorted", "duplicate", "foreign-kind", "unhashable", "not-a-list"],
)
def test_a_selection_the_reads_do_not_support_is_refused(
    generation: object, catalog: list[object], message: str
) -> None:
    with pytest.raises(probe.SampleError, match=message):
        probe.select_outside_list_item(
            generation_keys=cast("list[str]", generation), catalog_keys=cast("list[str]", catalog)
        )


def test_the_window_names_the_selection_days_and_its_own_unit() -> None:
    assert probe.window_days(_WINDOW) == ("2026-10-01", "2026-10-03")
    sql, digest = probe.selection_sql("dev", _WINDOW)
    assert "DATE '2026-10-01' AND DATE '2026-10-03'" in sql
    assert digest == hashlib.sha256(sql.encode()).hexdigest()
    from domain.operations.ingestion_root import window_identity  # noqa: PLC0415

    key = f"{_WINDOW}retrieved=20261006T092523Z/manifest.json"
    assert window_identity(key).conflict_unit == _TARGET
    for window, message in (
        ("amc/window=2026-10-03_2026-10-01/", "reversed"),
        ("amc/window=2026-13-01_2026-13-03/", "ISO dates"),
        ("amc/no-window/", "no window"),
    ):
        with pytest.raises(probe.SampleError, match=message):
            probe.window_days(window)


def _observation(**changes: object) -> probe.SelectionObservation:
    values: dict[str, object] = {
        "query_execution_id": _QUERY,
        "query_completed_at": "2026-10-15T09:01:00+00:00",
        "generation_keys": tuple(_GENERATION),
        "browse_current_on": "2026-10-15",
        "catalog_keys": tuple(_CATALOG),
        "catalog_truncated": False,
        "target_unit": _TARGET,
        "target_window": _WINDOW,
        "target_generation": _ACCEPTED,
        "read_at": "2026-10-15T09:01:05+00:00",
        **changes,
    }
    return probe.SelectionObservation(**values)  # pyright: ignore[reportArgumentType]


def _receipt(**changes: object) -> dict[str, object]:
    receipt = probe.selection_receipt(
        execution_id="exec-1", environment="dev", generation=_ACCEPTED, observed=_observation()
    )
    receipt.update(changes)
    return receipt


def test_a_valid_receipt_proves_its_item_unit_and_generation_and_leaves_absence_to_d2() -> None:
    receipt = _receipt()
    assert receipt["selected_item"] == "campaign:1c"
    assert (receipt["target_unit"], receipt["target_generation"]) == (_TARGET, _ACCEPTED)
    assert receipt["absence"] == {
        "status": "MOVED_TO_D2",
        "reason": probe.ABSENCE_DEFERRED_REASON,
    }
    assert (
        probe.validate_outside_selection(
            receipt, execution_id="exec-1", environment="dev", generation=_ACCEPTED
        )
        == "campaign:1c"
    )


@pytest.mark.parametrize(
    ("receipt", "expectation", "message"),
    [
        ({"items": _CATALOG[:25], "selected_item": "campaign:1c"}, {}, "missing or unexpected"),
        (_receipt(workgroup="adlytics-dev-v2-bake"), {}, "workgroup"),
        (_receipt(workgroup="primary"), {}, "workgroup"),
        (_receipt(sql="SELECT 'campaign:1c' AS campaign_key"), {}, "sql"),
        (_receipt(sql_sha256="0" * 64), {}, "sql"),
        (_receipt(acceptance_execution_id="exec-0"), {}, "execution"),
        (_receipt(generation="c" * 64), {}, "generation"),
        (
            _receipt(generation="gen-7", target_generation="gen-7"),
            {"generation": None},
            "generation",
        ),
        (_receipt(target_generation="c" * 64), {}, "target_generation"),
        (_receipt(target_unit="b" * 64), {}, "target_unit"),
        (_receipt(target_window="amc/window=2026-09-19_2026-09-21/"), {}, "sql, target_unit"),
        (_receipt(query_execution_id="q-1"), {}, "query"),
        (_receipt(schema_version="chat-outside-list-selection-v2"), {}, "schema"),
        (_receipt(selected_item="campaign:1d"), {}, "selected_item"),
        (_receipt(selected_item=_CATALOG[0]), {}, "selected_item"),
        (_receipt(catalog_truncated=True), {}, "catalog"),
        (_receipt(absence={"status": "PROVEN"}), {}, "absence"),
        (_receipt(generation_keys=["campaign:ff"]), {}, "no catalog campaign"),
        (_receipt(generation_keys=[{"key": 1}]), {}, "non-campaign"),
        (_receipt(target_unit=7), {}, "not text"),
        (_receipt(query_execution_id=["x"]), {}, "not text"),
        (_receipt(catalog_keys="campaign:00"), {}, "not a key list"),
        (_receipt(query_completed_at="2026-10-15T09:01:00"), {}, "UTC"),
        (_receipt(browse_current_on="today"), {}, "ISO date"),
        (_receipt(), {"environment": "prod"}, "environment"),
    ],
    ids=[
        "hand-written-pair",
        "bake-workgroup",
        "primary-workgroup",
        "other-sql",
        "sql-digest",
        "other-execution",
        "other-generation",
        "label-generation",
        "target-generation-not-accepted",
        "unit-not-the-windows",
        "window-of-another-unit",
        "malformed-query-id",
        "old-schema",
        "item-without-impressions",
        "not-the-last-key",
        "catalog-truncated",
        "absence-claimed",
        "generation-disjoint",
        "unhashable-key",
        "non-string-unit",
        "non-string-query",
        "keys-not-a-list",
        "naive-completion",
        "browse-day",
        "other-environment",
    ],
)
def test_a_receipt_failing_any_field_proves_no_item(
    receipt: dict[str, object], expectation: dict[str, str | None], message: str
) -> None:
    expected: dict[str, str | None] = {
        "execution_id": "exec-1",
        "environment": "dev",
        "generation": _ACCEPTED,
    }
    with pytest.raises(probe.SampleError, match=message):
        probe.validate_outside_selection(receipt, **{**expected, **expectation})  # pyright: ignore[reportArgumentType]


class _FakeAthena:
    """One query whose results span two pages, the header only on the first."""

    def __init__(self, keys: list[str], *, state: str = "SUCCEEDED") -> None:
        self.keys = keys
        self.state = state
        self.started: list[dict[str, Any]] = []

    def start_query_execution(self, **request: Any) -> dict[str, str]:
        self.started.append(request)
        return {"QueryExecutionId": _QUERY}

    def get_query_execution(self, **_: str) -> dict[str, object]:
        query = self.started[-1] if self.started else {"QueryString": "", "WorkGroup": ""}
        return {
            "QueryExecution": {
                "QueryExecutionId": _QUERY,
                "Query": query["QueryString"],
                "WorkGroup": query["WorkGroup"],
                "Status": {
                    "State": self.state,
                    "CompletionDateTime": datetime.datetime(
                        2026, 10, 15, 9, 1, tzinfo=datetime.UTC
                    ),
                },
            }
        }

    def get_paginator(self, _: str) -> SimpleNamespace:
        rows = [{"Data": [{"VarCharValue": key}]} for key in self.keys]
        header = {"Data": [{"VarCharValue": "campaign_key"}]}
        pages = [{"ResultSet": {"Rows": [header, *rows[:10]]}}, {"ResultSet": {"Rows": rows[10:]}}]
        return SimpleNamespace(paginate=lambda **_kwargs: iter(pages))  # pyright: ignore[reportUnknownLambdaType]


def _browse_result(keys: list[str], truncated: bool, count: int) -> SimpleNamespace:
    aliases = [SimpleNamespace(key=SimpleNamespace(value=key)) for key in keys]
    return SimpleNamespace(aliases=aliases, truncated=truncated, authorized_count=count)


def _ports(
    athena: _FakeAthena,
    generations: list[probe.GenerationProof] | None = None,
    window: str = _WINDOW,
) -> tuple[probe.SelectionPorts, list[tuple[datetime.date, int]]]:
    browses: list[tuple[datetime.date, int]] = []
    proofs = list(generations or [probe.GenerationProof(_ACCEPTED, None)] * 2)

    def browse(current_on: datetime.date, limit: int) -> SimpleNamespace:
        browses.append((current_on, limit))
        return _browse_result(list(reversed(_CATALOG)), False, 30)

    ports = probe.SelectionPorts(
        athena=athena,
        browse=browse,
        generation=lambda: proofs.pop(0),
        window=lambda: window,
        now=lambda: datetime.datetime(2026, 10, 15, 9, 1, 5, tzinfo=datetime.UTC),
    )
    return ports, browses


def _select_args(tmp_path: Path, **changes: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "environment": "dev",
        "region": "us-east-1",
        "acceptance_execution_id": "exec-1",
        "accepted_generation": _ACCEPTED,
        "generation_unit": _TARGET,
        "graph_store_stack": "Adlytics-ControlPlaneAuthorityStore-dev",
        "output": tmp_path / probe.OUTSIDE_SELECTION_RECEIPT,
        **changes,
    }
    return SimpleNamespace(**values)


def test_the_selection_mode_writes_one_receipt_from_the_chat_query_and_the_real_browse(
    tmp_path: Path,
) -> None:
    athena = _FakeAthena(list(reversed(_GENERATION)))
    ports, browses = _ports(athena)
    args = _select_args(tmp_path)
    assert probe._select_main(cast("Any", args), ports) == 0  # pyright: ignore[reportPrivateUsage]
    assert athena.started == [
        {
            "QueryString": probe.selection_sql("dev", _WINDOW)[0],
            "WorkGroup": "adlytics-dev-v2-interactive",
            "QueryExecutionContext": {"Database": "adlytics_dev_v2_current"},
            "ResultReuseConfiguration": {
                "ResultReuseByAgeConfiguration": {"Enabled": False, "MaxAgeInMinutes": 0}
            },
        }
    ]
    assert browses == [(datetime.date(2026, 10, 15), 1_000)]
    written = json.loads(args.output.read_text())
    assert written["generation_keys"] == sorted(_GENERATION) and written["catalog_keys"] == _CATALOG
    assert written["selected_item"] == "campaign:1c" and written["target_window"] == _WINDOW
    assert written["query_completed_at"] == "2026-10-15T09:01:00+00:00"
    loaded = probe.load_outside_selection(
        args.output, execution_id="exec-1", environment="dev", generation=_ACCEPTED
    )
    assert loaded == {
        "outside_list_item": "campaign:1c",
        "outside_list_present": True,
        "outside_selection_sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
        "outside_selection_query_execution_id": _QUERY,
        "target_unit": _TARGET,
        "target_generation": _ACCEPTED,
    }
    # Written once: a second selection cannot replace the execution's receipt.
    again, _ = _ports(_FakeAthena(_GENERATION))
    assert probe._select_main(cast("Any", args), again) == 1  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("args", "athena", "proofs", "window", "code"),
    [
        ({"environment": "prod"}, "SUCCEEDED", None, _WINDOW, 2),
        ({"region": "us-west-2"}, "SUCCEEDED", None, _WINDOW, 2),
        ({"generation_unit": None}, "SUCCEEDED", None, _WINDOW, 2),
        ({"graph_store_stack": None}, "SUCCEEDED", None, _WINDOW, 2),
        ({"accepted_generation": "gen-7"}, "SUCCEEDED", None, _WINDOW, 1),
        ({}, "FAILED", None, _WINDOW, 1),
        ({}, "SUCCEEDED", [probe.GenerationProof(None, "binding_not_direct")], _WINDOW, 1),
        (
            {},
            "SUCCEEDED",
            [probe.GenerationProof(_ACCEPTED, None), probe.GenerationProof("c" * 64, None)],
            _WINDOW,
            1,
        ),
        ({}, "SUCCEEDED", None, "amc/window=2026-09-19_2026-09-21/", 1),
    ],
    ids=[
        "not-dev",
        "other-region",
        "no-unit",
        "no-store",
        "label-generation",
        "query-failed",
        "unit-not-governed",
        "generation-moved",
        "window-of-another-unit",
    ],
)
def test_a_selection_without_its_inputs_or_governed_reads_writes_nothing(  # noqa: PLR0913, PLR0917 -- one parametrized case
    tmp_path: Path,
    args: dict[str, object],
    athena: str,
    proofs: list[probe.GenerationProof] | None,
    window: str,
    code: int,
) -> None:
    ports, _ = _ports(_FakeAthena(_GENERATION, state=athena), proofs, window)
    selected = _select_args(tmp_path, **args)
    assert probe._select_main(cast("Any", selected), ports) == code  # pyright: ignore[reportPrivateUsage]
    assert not (tmp_path / probe.OUTSIDE_SELECTION_RECEIPT).exists()


def _readback(**changes: object) -> dict[str, object]:
    return {
        "query_execution_id": _QUERY,
        "state": "SUCCEEDED",
        "workgroup": "adlytics-dev-v2-interactive",
        "query_sha256": probe.selection_sql("dev", _WINDOW)[1],
        "completed_at": "2026-10-15T09:01:00+00:00",
        "keys": list(reversed(_GENERATION)),
        **changes,
    }


def test_the_athena_readback_proves_the_receipt_inside_this_execution() -> None:
    athena = _FakeAthena(list(reversed(_GENERATION)))
    athena.started.append(
        {
            "QueryString": probe.selection_sql("dev", _WINDOW)[0],
            "WorkGroup": "adlytics-dev-v2-interactive",
        }
    )
    readback = probe.athena_query_readback(athena, _QUERY)
    assert readback == _readback()
    assert (
        probe.selection_readback_reasons(
            _receipt(),
            readback,
            preflight_completed_at="2026-10-15T09:00:00+00:00",
            baseline_started_at="2026-10-15T09:02:00+00:00",
        )
        == []
    )


@pytest.mark.parametrize(
    ("readback", "preflight", "reason"),
    [
        (_readback(query_execution_id="00000000-0000-4000-8000-000000000000"), None, "other_query"),
        (_readback(state="FAILED", keys=[]), None, "not_succeeded"),
        (_readback(workgroup="adlytics-dev-v2-bake"), None, "workgroup_mismatch"),
        (_readback(query_sha256="0" * 64), None, "sql_mismatch"),
        (_readback(keys=_CATALOG), None, "keys_mismatch"),
        (_readback(keys="campaign:00"), None, "keys_mismatch"),
        (_readback(), "2026-10-15T09:01:30+00:00", "outside_execution"),
        (_readback(completed_at="2026-10-15T09:03:00+00:00"), None, "outside_execution"),
        (_readback(completed_at="2026-10-15T09:01:30+00:00"), None, "completion_mismatch"),
        (_readback(completed_at=None), None, "time_unprovable"),
    ],
    ids=[
        "fabricated-query",
        "failed",
        "other-workgroup",
        "other-sql",
        "other-results",
        "malformed-results",
        "stale-before-preflight",
        "after-baseline-start",
        "completion-rewritten",
        "no-completion",
    ],
)
def test_a_readback_that_does_not_prove_the_receipt_is_named(
    readback: dict[str, object], preflight: str | None, reason: str
) -> None:
    reasons = probe.selection_readback_reasons(
        _receipt(),
        readback,
        preflight_completed_at=preflight or "2026-10-15T09:00:00+00:00",
        baseline_started_at="2026-10-15T09:02:00+00:00",
    )
    assert f"readback_{reason}" in reasons


def test_a_phase_run_binds_the_receipt_to_its_execution_and_unit(tmp_path: Path) -> None:
    path = tmp_path / probe.OUTSIDE_SELECTION_RECEIPT
    path.write_text(json.dumps(_receipt()))
    phased = SimpleNamespace(
        outside_selection_receipt=path,
        phase="paired-no-bake",
        acceptance_execution_id="exec-1",
        environment="dev",
        accepted_generation=_ACCEPTED,
        generation_unit=_TARGET,
    )
    assert probe._recorded_selection(cast("Any", phased))["target_unit"] == _TARGET  # pyright: ignore[reportPrivateUsage]
    for change, message in (
        ({"acceptance_execution_id": "exec-2"}, "execution"),
        ({"generation_unit": "c" * 64}, "target unit"),
        ({"outside_selection_receipt": None}, "--outside-selection-receipt"),
    ):
        refused = SimpleNamespace(**{**vars(phased), **change})
        with pytest.raises(probe.SampleError, match=message):
            probe._recorded_selection(cast("Any", refused))  # pyright: ignore[reportPrivateUsage]
    plain = SimpleNamespace(**{**vars(phased), "phase": None, "acceptance_execution_id": None})
    assert probe._recorded_selection(cast("Any", plain))["outside_list_item"] == "campaign:1c"  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("value", ["gen-7", "", "A" * 64, "a" * 63, None, 7])
def test_the_accepted_generation_must_be_one_serving_digest(value: object) -> None:
    with pytest.raises(probe.SampleError, match="64-hex serving_generation"):
        probe.require_serving_digest(value)
    assert probe.require_serving_digest(_ACCEPTED) == _ACCEPTED


def test_a_unit_serving_another_generation_says_what_it_serves_and_why() -> None:
    probe.require_accepted_generation(probe.GenerationProof(_ACCEPTED, None), _ACCEPTED, _TARGET)
    with pytest.raises(probe.SampleError, match=f"serves governed generation {'c' * 64}"):
        probe.require_accepted_generation(probe.GenerationProof("c" * 64, None), _ACCEPTED, _TARGET)
    with pytest.raises(probe.SampleError, match="none \\(binding_stale\\)"):
        probe.require_accepted_generation(
            probe.GenerationProof(None, "binding_stale"), _ACCEPTED, _TARGET
        )
    with pytest.raises(probe.SampleError, match="store failure binding_unreadable"):
        probe.require_accepted_generation(
            probe.GenerationProof(None, "binding_unreadable"), _ACCEPTED, _TARGET
        )


def test_the_filled_prompt_digest_attests_the_literal_questions() -> None:
    first = probe.filled_prompt_sha256(_filled())
    other = probe.filled_prompt_sha256(
        probe.with_outside_list_item(probe.load_chat_manifest(_MANIFEST), "campaign:1d")
    )
    assert len(first) == 64 and first != other != probe.load_chat_manifest(_MANIFEST).sha256
    with pytest.raises(probe.SampleError, match="never filled"):
        probe.filled_prompt_sha256(probe.load_chat_manifest(_MANIFEST))


_GRANT = "1" * 64
_UNIT = "u" * 64


def _pointer(**changes: object) -> dict[str, object]:
    """One flipped window pointer exactly as the runtime writes it; a None change drops a field."""
    pointer: dict[str, object] = {
        "partition_key": {"S": f"GENERATION#{_UNIT}"},
        "record_key": {"S": "POINTER"},
        "item_kind": {"S": "generation_pointer"},
        "schema_version": {"N": "1"},
        "grant_id": {"S": _GRANT},
        "grant_ordinal": {"N": "7"},
        "advanced_count": {"N": "3"},
        "advanced_curation_count": {"N": "3"},
        "superseded_count": {"N": "0"},
        "superseded_curation_count": {"N": "0"},
        "target_count": {"N": "3"},
    }
    pointer.update(changes)
    return {key: value for key, value in pointer.items() if value is not None}


def _binding_item(**changes: object) -> dict[str, object]:
    """One complete, executed, non-stale DIRECT binding; a None change drops a field."""
    from domain.operations.execution_graph_release import serving_generation  # noqa: PLC0415

    bound_at = "2026-10-14T05:29:51.410000+00:00"
    item: dict[str, object] = {
        "pk": {"S": f"SERVING#{_GRANT}"},
        "sk": {"S": "BINDING"},
        "item_kind": {"S": "serving_binding"},
        "binding_id": {"S": "b" * 64},
        "conflict_unit": {"S": _UNIT},
        "served_revision": {"S": bound_at},
        "contributor_roots": {"L": [{"S": "root-flow"}]},
        "bound_at": {"S": bound_at},
        "artifact_revision": {"S": serving_generation(_GRANT)},
        "unavailable": {"L": []},
        "binding_kind": {"S": "direct"},
        "domain": {"S": "ingestion"},
        "cohort_revision": {"S": "c-a"},
        "serving_input_revision": {"S": "i-a"},
        "plan_digest": {"S": "p" * 64},
        "execution_attempts": {"N": "1"},
        "stale": {"BOOL": False},
    }
    item.update(changes)
    return {key: value for key, value in item.items() if value is not None}


@guard_reserved_words
class _PointerClient:
    """The publications table's one transactional get, as the runtime store issues it."""

    def __init__(self, item: dict[str, object] | None) -> None:
        self.item = item
        self.requests: list[dict[str, Any]] = []

    def transact_get_items(self, **request: Any) -> dict[str, object]:
        self.requests.append(request)
        return {"Responses": [{} if self.item is None else {"Item": self.item}]}


def _governed(
    pointer: dict[str, object] | None, binding: dict[str, object] | None
) -> probe.GenerationProof:
    reads: list[str] = []
    client = _PointerClient(pointer)

    def read_binding(grant: str) -> dict[str, object] | None:
        reads.append(grant)
        return binding

    store = probe._publication_reader("publications", client)  # pyright: ignore[reportPrivateUsage]
    result = probe.governed_generation(store, _UNIT, read_binding)
    assert len(client.requests) == 1 and len(reads) <= 1
    return result


def test_the_governed_generation_needs_a_runtime_parsed_pointer_and_current_direct_truth() -> None:
    from domain.operations.execution_graph_release import serving_generation  # noqa: PLC0415

    assert _governed(_pointer(), _binding_item()) == probe.GenerationProof(
        serving_generation(_GRANT), None
    )


@pytest.mark.parametrize(
    ("pointer", "reason"),
    [
        (None, "pointer_absent"),
        (_pointer(partition_key={"S": "GENERATION#" + "v" * 64}), "pointer_malformed"),
        (_pointer(record_key={"S": "CURRENT"}), "pointer_malformed"),
        (_pointer(item_kind={"S": "publication"}), "pointer_malformed"),
        (_pointer(schema_version={"N": "2"}), "pointer_malformed"),
        (_pointer(grant_ordinal={"N": "0"}), "pointer_malformed"),
        (_pointer(grant_ordinal=None), "pointer_malformed"),
        (_pointer(advanced_count=None), "pointer_malformed"),
        (_pointer(grant_id=None), "pointer_malformed"),
        (_pointer(grant_id={"S": "not-a-grant"}), "grant_malformed"),
        (_pointer(grant_id={"S": "2" * 64}), "binding_other_revision"),
    ],
    ids=[
        "absent",
        "wrong-partition",
        "wrong-record",
        "wrong-kind",
        "wrong-schema",
        "zero-ordinal",
        "no-ordinal",
        "no-counts",
        "no-grant",
        "malformed-grant",
        "other-grant",
    ],
)
def test_a_substituted_or_malformed_pointer_proves_no_generation(
    pointer: dict[str, object] | None, reason: str
) -> None:
    assert _governed(pointer, _binding_item()) == probe.GenerationProof(None, reason)


@pytest.mark.parametrize(
    ("binding", "reason"),
    [
        (None, "binding_absent"),
        (_binding_item(artifact_revision={"S": "a" * 64}), "binding_other_revision"),
        (_binding_item(artifact_revision=None), "binding_other_revision"),
        (
            _binding_item(
                binding_kind=None,
                domain=None,
                cohort_revision=None,
                serving_input_revision=None,
                plan_digest=None,
            ),
            "binding_not_direct",
        ),
        (_binding_item(stale={"BOOL": True}), "binding_stale"),
        (_binding_item(execution_attempts={"N": "0"}), "binding_not_executed"),
        (_binding_item(execution_attempts=None), "binding_not_executed"),
        (
            _binding_item(unavailable={"L": [{"L": [{"S": "campaigns#30d"}, {"S": "timeout"}]}]}),
            "binding_incomplete",
        ),
        (
            _binding_item(
                binding_kind={"S": "alias"},
                direct_binding_id={"S": "d" * 64},
                artifact_revision=None,
                execution_attempts=None,
            ),
            "binding_not_direct",
        ),
        (_binding_item(conflict_unit={"S": "w" * 64}), "binding_other_unit"),
        (_binding_item(bound_at=None), "binding_malformed"),
        (_binding_item(binding_kind={"S": "direct"}, plan_digest=None), "binding_malformed"),
    ],
    ids=[
        "absent",
        "other-revision",
        "no-revision",
        "legacy-no-kind",
        "stale",
        "zero-attempts",
        "no-attempts",
        "partial-outcome",
        "alias",
        "other-unit",
        "malformed",
        "incomplete-typed-identity",
    ],
)
def test_a_binding_without_current_direct_truth_proves_no_generation(
    binding: dict[str, object] | None, reason: str
) -> None:
    assert _governed(_pointer(), binding) == probe.GenerationProof(None, reason)


@guard_reserved_words
class _BrokenPointerClient:
    def transact_get_items(self, **_: Any) -> dict[str, object]:
        raise ConnectionError("store down")


def test_a_store_failure_is_named_apart_from_a_refused_authority() -> None:
    store = probe._publication_reader("publications", _BrokenPointerClient())  # pyright: ignore[reportPrivateUsage]
    down = probe.governed_generation(store, _UNIT, lambda _grant: _binding_item())
    assert down == probe.GenerationProof(None, "pointer_unreadable") and down.store_failure

    def broken(_grant: str) -> dict[str, object] | None:
        raise ConnectionError("store down")

    good = probe._publication_reader("publications", _PointerClient(_pointer()))  # pyright: ignore[reportPrivateUsage]
    unread = probe.governed_generation(good, _UNIT, broken)
    assert unread == probe.GenerationProof(None, "binding_unreadable") and unread.store_failure
    assert not probe.GenerationProof(None, "binding_stale").store_failure


def test_a_phase_run_needs_the_graph_store_for_its_generation_reads() -> None:
    args = SimpleNamespace(
        output=Path("draft.json"),
        acceptance_execution_id="exec-1",
        accepted_generation="gen-7",
        generation_unit="u",
        preflight_completed_at="2026-10-15T09:00:00+00:00",
        outside_selection_receipt=Path("selection.json"),
        graph_store_stack=None,
        rounds=1,
        phase="paired-no-bake",
        target_cohort_revision=None,
    )
    with pytest.raises(probe.SampleError, match="graph-store-stack"):
        probe._phase_identity(cast("Any", args), "https://app.invalid")  # pyright: ignore[reportPrivateUsage]


def test_an_unfilled_manifest_writes_no_draft() -> None:
    with pytest.raises(probe.SampleError, match="never filled"):
        probe.draft_sample(
            probe.load_chat_manifest(_MANIFEST),
            [],
            phase="paired_no_bake",
            identity={},
            selection=_SELECTION,
            started_at="2026-10-15T09:00:00+00:00",
            ended_at="2026-10-15T09:01:00+00:00",
        )


def _changed(name: str, document: dict[str, Any]) -> None:
    prompts = cast("list[dict[str, str]]", document["prompts"])
    if name == "five":
        document["prompts"] = prompts[:5]
    elif name == "seven":
        prompts.append({"prompt_id": "seventh", "conversation": "e", "question": "Hello again."})
    elif name == "duplicate":
        prompts[1] = prompts[0]
    elif name == "outside":
        prompts[5]["question"] = "Report another campaign."
    elif name == "twice":
        prompts[0]["question"] = "Compare {outside_list_item}."
    elif name == "selection":
        document["outside_list_selection"] = "first-campaign"
    elif name == "schema":
        document["schema_version"] = "v0"
    else:
        prompts[0]["question"] = " "


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("five", "six unique prompts"),
        # The control for `--turns`: the D8 manifest still holds exactly six prompts.
        ("seven", "six unique prompts"),
        ("duplicate", "six unique prompts"),
        ("outside", "outside-list"),
        ("twice", "outside-list"),
        ("selection", "outside-list selection"),
        ("schema", "wrong schema"),
        ("blank", "blank field"),
    ],
)
def test_a_changed_manifest_is_refused(tmp_path: Path, change: str, message: str) -> None:
    document = cast("dict[str, Any]", json.loads(_MANIFEST.read_text()))
    _changed(change, document)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document))
    with pytest.raises(probe.SampleError, match=message):
        probe.load_chat_manifest(path)


def _times(  # noqa: PLR0913 -- one fixture builder
    sent: int, latency: int, *, status: int = 200, terminal: str = "done"
) -> probe.ClientTimes:
    return probe.ClientTimes(
        status=status,
        sent_epoch_ms=sent,
        ended_epoch_ms=sent + latency,
        terminal=terminal,
        released_text="An answer.",
    )


def _rows(
    phase: str, start_ms: int, receipt: Mapping[str, object] | None = None
) -> list[dict[str, object]]:
    manifest = _filled()
    return [
        probe.chat_sample_row(
            step,
            _times(start_ms + index * 10_000, 1_000 + index),
            governed_generation="gen-7",
            head_receipt=receipt if phase == "during_bake" else None,
        )
        for index, step in enumerate(manifest.steps)
    ]


def test_a_row_records_the_exact_epoch_interval_and_governance() -> None:
    (row, *_rest) = _rows("paired_no_bake", 1_760_520_000_000)
    assert row["latency_ms"] == 1_000
    assert row["request_started_at"] == "2025-10-15T09:20:00+00:00"
    assert (row["terminal_class"], row["governed_answer"], row["throttled"]) == (
        "answer",
        True,
        False,
    )
    step = _filled().steps[0]
    throttled = probe.chat_sample_row(
        step,
        _times(0, 5, status=429, terminal="refused"),
        governed_generation=None,
        head_receipt=None,
    )
    assert (throttled["throttled"], throttled["governed_answer"]) == (True, False)
    with pytest.raises(probe.SampleError, match="non-positive"):
        probe.chat_sample_row(step, _times(5, 0), governed_generation=None, head_receipt=None)


_IDENTITY: dict[str, object] = {
    "acceptance_execution_id": "exec-1",
    "environment": "dev",
    "account": "111122223333",
    "origin": "https://app.invalid",
    "accepted_generation": "gen-7",
    "generation_unit": "b" * 64,
    "preflight_completed_at": "2026-10-15T09:00:00+00:00",
    "target_cohort_revision": None,
}


def _draft(  # noqa: PLR0913, PLR0917 -- one fixture builder
    phase: str, rows: list[dict[str, object]], start: str, end: str, **identity: object
) -> dict[str, object]:
    return probe.draft_sample(
        _filled(),
        rows,
        phase=phase,
        identity={**_IDENTITY, **identity},
        selection=_SELECTION,
        started_at=start,
        ended_at=end,
    )


def _binding(**changes: str) -> dict[str, str]:
    return {
        "first_root_launch_at": "2026-10-15T10:00:00+00:00",
        "target_bake_run_id": "run-a",
        "target_cohort_revision": "c-a",
        "bake_run_started_at": "2026-10-15T10:10:00+00:00",
        "bake_finalize_ended_at": "2026-10-15T10:30:00+00:00",
        **changes,
    }


def test_a_baseline_binds_only_after_preflight_and_strictly_before_first_launch() -> None:
    draft = _draft(
        "paired_no_bake",
        _rows("paired_no_bake", 0),
        "2026-10-15T09:10:00+00:00",
        "2026-10-15T09:59:59+00:00",
    )
    sample = probe.bind_chat_sample(draft, _binding())
    assert sample["schema_version"] == probe.CHAT_SAMPLE_SCHEMA
    assert sample["target_bake_run_id"] == "run-a"
    at_launch = _draft(
        "paired_no_bake",
        _rows("paired_no_bake", 0),
        "2026-10-15T09:10:00+00:00",
        "2026-10-15T10:00:00+00:00",
    )
    with pytest.raises(probe.SampleError, match="strictly before first launch"):
        probe.bind_chat_sample(at_launch, _binding())
    early = _draft(
        "paired_no_bake",
        _rows("paired_no_bake", 0),
        "2026-10-15T08:59:59+00:00",
        "2026-10-15T09:30:00+00:00",
    )
    with pytest.raises(probe.SampleError, match="after preflight"):
        probe.bind_chat_sample(early, _binding())


def test_a_during_bake_draft_needs_an_active_target_receipt_for_every_row() -> None:
    active = {"state": "ACTIVE", "cohort_revision": "c-a"}
    draft = _draft(
        "during_bake",
        _rows("during_bake", 0, active),
        "2026-10-15T10:15:00+00:00",
        "2026-10-15T10:20:00+00:00",
        target_cohort_revision="c-a",
    )
    assert probe.bind_chat_sample(draft, _binding())["phase"] == "during_bake"
    with pytest.raises(probe.SampleError, match="ACTIVE target is another cohort"):
        probe.bind_chat_sample(draft, _binding(target_cohort_revision="c-b"))
    with pytest.raises(probe.SampleError, match="without an ACTIVE head receipt"):
        _draft(
            "during_bake",
            _rows("during_bake", 0, {"state": "IDLE"}),
            "2026-10-15T10:15:00+00:00",
            "2026-10-15T10:20:00+00:00",
        )


def test_a_reversed_bake_interval_or_missing_binding_writes_nothing(tmp_path: Path) -> None:
    draft = _draft(
        "paired_no_bake",
        _rows("paired_no_bake", 0),
        "2026-10-15T09:10:00+00:00",
        "2026-10-15T09:20:00+00:00",
    )
    with pytest.raises(probe.SampleError, match="reversed"):
        probe.bind_chat_sample(draft, _binding(bake_finalize_ended_at="2026-10-15T10:09:59+00:00"))
    with pytest.raises(probe.SampleError, match="no bake_finalize_ended_at"):
        probe.bind_chat_sample(draft, _binding(bake_finalize_ended_at=""))
    with pytest.raises(probe.SampleError, match="UTC"):
        probe.bind_chat_sample(draft, _binding(bake_run_started_at="2026-10-15T10:10:00"))
    path = tmp_path / "sample.json"
    probe.write_atomic(path, probe.bind_chat_sample(draft, _binding()))
    with pytest.raises(probe.SampleError, match="overwrite"):
        probe.write_atomic(path, {})
    assert not list(tmp_path.glob(".*.tmp"))


def test_a_draft_binds_only_to_the_receipt_its_prompt_was_filled_from() -> None:
    rows = _rows("paired_no_bake", 1_760_518_800_000)
    for change in (
        {"outside_list_item": "campaign:1d"},
        {"outside_selection_sha256": ""},
        {"outside_selection_query_execution_id": ""},
    ):
        with pytest.raises(probe.SampleError, match="not bound to the receipt"):
            probe.draft_sample(
                _filled(),
                rows,
                phase="paired_no_bake",
                identity=_IDENTITY,
                selection={**_SELECTION, **change},
                started_at="2025-10-15T09:00:00+00:00",
                ended_at="2025-10-15T09:59:00+00:00",
            )


def test_bound_samples_feed_the_gate_comparator_exactly() -> None:
    from validation import probe_ingestion_traceability_gate as gate  # noqa: PLC0415

    base_draft = _draft(
        "paired_no_bake",
        _rows("paired_no_bake", 1_760_518_800_000),
        "2025-10-15T09:00:00+00:00",
        "2025-10-15T09:59:00+00:00",
        preflight_completed_at="2025-10-15T09:00:00+00:00",
    )
    active = {"state": "ACTIVE", "cohort_revision": "c-a"}
    load_draft = _draft(
        "during_bake",
        _rows("during_bake", 1_760_522_400_000, active),
        "2025-10-15T10:00:00+00:00",
        "2025-10-15T10:05:00+00:00",
        preflight_completed_at="2025-10-15T09:00:00+00:00",
        target_cohort_revision="c-a",
    )
    binding = _binding(
        first_root_launch_at="2025-10-15T09:59:30+00:00",
        bake_run_started_at="2025-10-15T10:00:30+00:00",
        bake_finalize_ended_at="2025-10-15T10:30:00+00:00",
    )
    baseline = probe.bind_chat_sample(base_draft, binding)
    during = probe.bind_chat_sample(load_draft, binding)
    verdict = gate.paired_bake_chat_verdict(
        baseline,
        during,
        "gen-7",
        _OUTSIDE,
        (binding["bake_run_started_at"], binding["bake_finalize_ended_at"]),
    )
    # The bounded-list absence leg is D2's (owner, 2026-10-15): a probe-produced pair records it
    # as not measured and passes GRAPH criterion 8 on its own legs.
    assert baseline["outside_list_absent_from_bounded_list"] is None
    assert baseline["outside_list_absence_status"] == "MOVED_TO_D2"
    assert verdict["passed"] is True, verdict["reasons"]
    # Paired control: a pair whose receipt did not prove presence is refused.
    unproven = {"outside_list_present_in_generation": False}
    refused = gate.paired_bake_chat_verdict(
        {**baseline, **unproven},
        during,
        "gen-7",
        _OUTSIDE,
        (binding["bake_run_started_at"], binding["bake_finalize_ended_at"]),
    )
    assert refused["reasons"] == ["outside_list_unproven"]


_TURNS = _ROOT / "validation" / "fixtures" / "chat-latency-turns-v1.json"


def _turn(turn_id: str, kind: str, conversation: str, **changes: object) -> dict[str, object]:
    return {
        "turn_id": turn_id,
        "class": kind,
        "conversation": conversation,
        "question": f"Question {turn_id}?",
        "follow_up_of": None,
        **changes,
    }


def _turn_list(turns: list[dict[str, object]], **changes: object) -> dict[str, object]:
    return {
        "schema_version": "chat-latency-turns/v1",
        "persona_id": "probe-a",
        "turns": turns,
        **changes,
    }


def _valid_turns() -> list[dict[str, object]]:
    return [
        _turn("warm-up", "warm_up", "w"),
        _turn("single-01", "single_tool", "a"),
        _turn("multi-01", "multi_tool", "b"),
        _turn("clarify-01", "clarification", "c"),
        _turn("clarify-02", "clarification", "c", follow_up_of="clarify-01"),
    ]


def _write(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "turns.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_a_valid_turn_list_loads_its_steps_and_classes_in_order(tmp_path: Path) -> None:
    loaded = probe.load_latency_turns(_write(tmp_path, _turn_list(_valid_turns())))
    assert [step.label for step in loaded.steps] == [
        "warm-up",
        "single-01",
        "multi-01",
        "clarify-01",
        "clarify-02",
    ]
    assert loaded.classes == (
        "warm_up",
        "single_tool",
        "multi_tool",
        "clarification",
        "clarification",
    )
    assert loaded.steps[4].conversation == "c" and loaded.persona_id == "probe-a"
    assert len(loaded.sha256) == 64


def test_the_tracked_turn_list_loads_with_the_planned_classes() -> None:
    loaded = probe.load_latency_turns(_TURNS)
    counts = {kind: loaded.classes.count(kind) for kind in probe.TURN_CLASSES}
    # 1 warm-up, 15 single-tool, 15 multi-tool, 10 clarification, plus the two Q0 reruns.
    assert counts == {"warm_up": 1, "single_tool": 17, "multi_tool": 15, "clarification": 10}
    labels = [step.label for step in loaded.steps]
    assert {"q0-outside-top-n-segments", "q0-reach-absent-week"} <= set(labels)
    # The fixture names fictional dev entities only and no outside-list placeholder.
    assert all(probe.OUTSIDE_PLACEHOLDER not in step.question for step in loaded.steps)


def _mutate(turns: list[dict[str, object]], rule: str) -> dict[str, object]:  # noqa: PLR0912 -- one case per schema rule
    document = _turn_list(turns)
    if rule == "unknown_class":
        turns[1]["class"] = "two_tool"
    elif rule == "duplicate_id":
        turns[2]["turn_id"] = "single-01"
    elif rule == "bad_id":
        turns[1]["turn_id"] = "Single_01"
    elif rule == "follow_up_later":
        turns[3]["follow_up_of"] = "clarify-02"
    elif rule == "follow_up_other_conversation":
        turns[4]["follow_up_of"] = "single-01"
    elif rule == "no_warm_up":
        turns[0]["class"] = "single_tool"
    elif rule == "second_warm_up":
        turns[2]["class"] = "warm_up"
    elif rule == "warm_up_not_first":
        turns[0], turns[1] = turns[1], turns[0]
    elif rule == "too_many":
        document["turns"] = [turns[0]] + [
            _turn(f"single-{index:02d}", "single_tool", f"s{index}") for index in range(60)
        ]
    elif rule == "too_few":
        document["turns"] = turns[:1]
    elif rule == "placeholder":
        turns[1]["question"] = "Report {outside_list_item}."
    elif rule == "long_question":
        turns[1]["question"] = "x" * 2_001
    elif rule == "blank_conversation":
        turns[1]["conversation"] = " "
    elif rule == "unknown_field":
        turns[1]["prompt_id"] = "single-01"
    elif rule == "persona":
        document["persona_id"] = "probe-b"
    elif rule == "schema":
        document["schema_version"] = "chat-bake-acceptance-v2"
    return document


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        ("unknown_class", "unknown class"),
        ("duplicate_id", "turn_id is repeated"),
        ("bad_id", r"turn_id is not \[a-z0-9-\]\{1,40\}"),
        ("follow_up_later", "follow_up_of names no earlier turn"),
        ("follow_up_other_conversation", "follow_up_of names no earlier turn"),
        ("no_warm_up", "exactly one warm_up, first"),
        ("second_warm_up", "exactly one warm_up, first"),
        ("warm_up_not_first", "exactly one warm_up, first"),
        ("too_many", "2 to 60 turns"),
        ("too_few", "2 to 60 turns"),
        ("placeholder", "outside-list placeholder"),
        ("long_question", "over 2,000 characters"),
        ("blank_conversation", "blank conversation"),
        ("unknown_field", "malformed"),
        ("persona", "probe-a persona"),
        ("schema", "wrong schema"),
    ],
)
def test_each_turn_list_rule_refuses_its_violation(tmp_path: Path, rule: str, message: str) -> None:
    path = _write(tmp_path, _mutate(_valid_turns(), rule))
    with pytest.raises(probe.SampleError, match=message):
        probe.load_latency_turns(path)


def test_a_turn_list_at_sixty_turns_and_a_2000_character_question_loads(tmp_path: Path) -> None:
    turns = [_turn("warm-up", "warm_up", "w")] + [
        _turn(f"single-{index:02d}", "single_tool", f"s{index}") for index in range(59)
    ]
    turns[1]["question"] = "x" * 2_000
    assert len(probe.load_latency_turns(_write(tmp_path, _turn_list(turns))).steps) == 60


def test_turns_with_a_d8_sample_flag_is_refused_before_anything_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path, _turn_list(_valid_turns()))
    base = ["--profile", "p", "--expected-account", "1", "--turns", str(path)]
    assert probe.main([*base, "--phase", "paired-no-bake"]) == 2
    assert "--turns takes no D8 sample flag (--phase)" in capsys.readouterr().out
    assert probe.main([*base, "--bind-draft", str(path)]) == 2
    assert "--bind-draft" in capsys.readouterr().out
    # Paired: `--turns` and `--manifest` are one choice, never both.
    with pytest.raises(SystemExit):
        probe.main([*base, "--manifest", str(_MANIFEST)])


def test_turns_with_no_sample_flag_passes_the_refusal() -> None:
    args = probe._parser().parse_args(  # pyright: ignore[reportPrivateUsage]
        ["--profile", "p", "--expected-account", "1", "--turns", str(_TURNS)]
    )
    probe.refuse_d8_flags_with_turns(args)
    assert args.manifest is None and args.turns == _TURNS


def _class_row(kind: str, *, first: int | None, answer: int | None, end: int) -> dict[str, object]:  # noqa: PLR0913 -- one fixture builder
    return {
        "class": kind,
        "client_ms": {"first_text": first, "first_answer_text": answer, "stream_end": end},
    }


def test_the_summary_groups_client_times_by_class_and_discards_the_warm_up() -> None:
    rows = [
        _class_row("warm_up", first=90_000, answer=90_000, end=99_000),
        *(
            _class_row("single_tool", first=1_000 * i, answer=2_000 * i, end=3_000 * i)
            for i in range(1, 21)
        ),
        _class_row("multi_tool", first=5_000, answer=9_000, end=30_000),
        _class_row("multi_tool", first=None, answer=None, end=60_000),
    ]
    grouped = probe.class_percentiles(rows)
    single = cast("dict[str, Any]", grouped["single_tool"])
    assert single["turns"] == 20
    assert single["first_text"] == {"n": 20, "median_ms": 10_000, "p95_ms": 19_000}
    assert single["after_tool_text"] == {"n": 20, "median_ms": 20_000, "p95_ms": 38_000}
    assert single["stream_end"] == {"n": 20, "median_ms": 30_000, "p95_ms": 57_000}
    multi = cast("dict[str, Any]", grouped["multi_tool"])
    # A turn that rendered no text counts only where it was measured.
    assert multi["first_text"] == {"n": 1, "median_ms": 5_000, "p95_ms": 5_000}
    assert multi["stream_end"]["n"] == 2
    assert cast("dict[str, Any]", grouped["clarification"])["turns"] == 0
    assert "warm_up" not in grouped
    loaded = probe.load_latency_turns(_TURNS)
    summary = probe.latency_turns_summary(loaded, rows)
    assert summary["turns_sha256"] == loaded.sha256 and summary["by_class"] == grouped
    first_row = cast("list[dict[str, object]]", summary["turns"])[0]
    assert first_row["class"] == "warm_up"


def test_the_q0_hop_split_reads_route_and_harness_span_starts() -> None:
    times = probe.ClientTimes(sent_epoch_ms=1_000_000, stream_end_ms=36_000)
    route = [
        {"name": "conversation.turn", "start_epoch_ms": 1_000_400},
        {"name": "Bedrock AgentCore.InvokeHarness", "start_epoch_ms": 1_000_700},
    ]
    harness = {"post_start_epoch_ms": 1_033_000, "harness_total_ms": 3_000}
    split = probe.hop_split(times, route_spans=route, harness=harness, timing={"cold_start": False})
    assert split["client_send_to_route_start_ms"] == 400
    assert split["route_start_to_harness_start_ms"] == 32_600
    assert split["harness_start_to_stream_end_ms"] == 3_000
    assert split["invoke_harness_spans"] == 1 and split["unmeasured"] == []
    # Paired: a missing harness span leaves its hops unmeasured, never inferred.
    missing = probe.hop_split(times, route_spans=route, harness={}, timing={})
    assert missing["route_start_to_harness_start_ms"] is None
    assert missing["harness_start_to_stream_end_ms"] is None
    assert missing["unmeasured"] == ["harness_span"]
