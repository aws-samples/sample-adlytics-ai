# SPDX-License-Identifier: MIT-0
"""Unit contract for the D2 chat leg's verdicts: declared outcomes, selection, labels, alarms."""

from __future__ import annotations

import datetime
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))
from validation import run_d2_chat_acceptance as leg  # noqa: E402
from validation.probe_turn_latency import ClientTimes  # noqa: E402

from domain.evaluation.linkage import EvaluationResultStateV1  # noqa: E402

pytestmark = pytest.mark.unit


def _times(
    text: str, *, terminal: str = "done", tools: bool = True, **extra: object
) -> dict[str, object]:
    return {
        "status": 200,
        "terminal": terminal,
        "released_text": text,
        "refusal_reason": "",
        "tool_events": [{"tool": "measurement_metric_by_grain", "status": "completed"}]
        if tools
        else [],
        **extra,
    }


# --- C2: every turn has a declared outcome ----------------------------------------------------


def test_stated_figures_read_separators_percent_and_suffix() -> None:
    figures = leg.stated_figures("Reach was 146,325 (35.8%) and spend $129.3K on 2026-09-30.")
    values = [(item.value, item.percent) for item in figures]
    assert (Decimal(146325), False) in values
    assert (Decimal("35.8"), True) in values
    assert (Decimal("129300.0"), False) in values


def test_an_answer_passes_only_when_it_states_every_required_value() -> None:
    expect = leg.Expectation("answer", all_of=(("61709", "count"), ("37025", "count")))
    assert leg.turn_verdict(expect, _times("Segments: 61,709 and 37,025."))["verdict"] == leg.PASS
    partial = leg.turn_verdict(expect, _times("Segments: 61,709 and 37,000."))
    assert partial["verdict"] == leg.FAIL
    assert partial["reasons"] == ["values_missing:37025"]


def test_a_rate_matches_at_the_precision_shown_as_percent_or_fraction() -> None:
    expect = leg.Expectation("answer", any_of=(("0.358024691358", "rate"),))
    assert leg.turn_verdict(expect, _times("The ARP rate is 35.8%."))["verdict"] == leg.PASS
    assert leg.turn_verdict(expect, _times("The ARP rate is 0.36."))["verdict"] == leg.PASS
    assert leg.turn_verdict(expect, _times("The ARP rate is 36.4%."))["verdict"] == leg.FAIL


def test_money_matches_a_rounded_or_abbreviated_figure() -> None:
    expect = leg.Expectation("answer", all_of=(("129336.5837", "money"),))
    assert leg.turn_verdict(expect, _times("Spend was $129,336.58."))["verdict"] == leg.PASS
    assert leg.turn_verdict(expect, _times("Spend was $129.3K."))["verdict"] == leg.PASS
    assert leg.turn_verdict(expect, _times("Spend was $131.0K."))["verdict"] == leg.FAIL


def test_an_answer_without_a_governed_tool_call_or_done_terminal_fails() -> None:
    expect = leg.Expectation("answer")
    assert leg.turn_verdict(expect, _times("Revenue was $4,121.43.", tools=False))["reasons"] == [
        "no_governed_tool_call"
    ]
    refused = cast("list[str]", leg.turn_verdict(expect, _times("", terminal="refused"))["reasons"])
    assert "terminal_refused" in refused


def test_an_absent_period_needs_a_governed_absence_and_no_figure() -> None:
    expect = leg.Expectation("absence")
    absent = _times("Reach for October 5 to October 11, 2026 isn't available yet.")
    assert leg.turn_verdict(expect, absent)["verdict"] == leg.PASS
    invented = _times("Reach for October 5 to October 11, 2026 was 146,325 shoppers.")
    assert leg.turn_verdict(expect, invented)["reasons"] == [
        "no_governed_absence",
        "figure_stated_for_absent_period",
    ]
    hedged = _times("That week isn't available; the prior week reached 146,325.")
    assert leg.turn_verdict(expect, hedged)["reasons"] == ["figure_stated_for_absent_period"]


def test_an_exclusive_rate_turn_fails_on_a_rate_outside_the_expected_cells() -> None:
    expect = leg.Expectation(
        "answer", any_of=(("0.358", "rate"), ("0.299", "rate")), exclusive=True
    )
    assert leg.turn_verdict(expect, _times("Cells: 35.8% and 29.9%."))["verdict"] == leg.PASS
    stray = leg.turn_verdict(expect, _times("Cells: 35.8% and 41.2%."))
    assert stray["reasons"] == ["unexpected_rates:41.2"]


def test_an_absence_states_no_small_invented_count() -> None:
    expect = leg.Expectation("absence")
    invented = _times("That week isn't available; about 40 shoppers were reached.")
    assert leg.turn_verdict(expect, invented)["reasons"] == ["figure_stated_for_absent_period"]


def test_a_required_phrase_must_appear() -> None:
    expect = leg.Expectation("answer", phrases=(r"mixed|all (?:Amazon )?purchases",))
    assert leg.turn_verdict(expect, _times("A mixed-basis rate: 35.8%."))["verdict"] == leg.PASS
    assert leg.turn_verdict(expect, _times("A rate: 35.8%."))["verdict"] == leg.FAIL


def test_an_answer_that_states_no_figure_is_not_an_answer() -> None:
    # A clarifying request ends `done` after a tool call, but it answers nothing.
    asked = _times("Please name an explicit date window, for example September 1-30, 2026.")
    assert leg.turn_verdict(leg.Expectation("answer"), asked)["reasons"] == ["no_figure_stated"]


def test_an_r1_refusal_is_deferred_not_failed() -> None:
    refused = _times(
        "",
        terminal="refused",
        refusal_reason="ADLY-PRESENTATION-0002 ungranted_generation",
    )
    verdict = leg.turn_verdict(leg.Expectation("answer"), refused)
    assert verdict["verdict"] == leg.DEFERRED_R1


def _expected() -> dict[str, object]:
    return {
        "arp": {"vintage": "2026-09-01", "arp_rates": ["0.358"], "standard_rates": ["0.239"]},
        "audiences": {
            "month": ["2026-09-01", "2026-09-30"],
            "selected_item": "campaign:13b41caa0b92128b2b1f00d3",
            "selected_name": "Northstar DSP | Flight 28",
            "segment_impressions": ["61709"],
            "exposure_impressions": "2194909",
            "exposure_spend": "129336.5837",
        },
        "reach": {
            "week": ["2026-09-28", "2026-10-04"],
            "values": ["146325"],
            "absent_week": ["2026-10-05", "2026-10-11"],
        },
        "index": {"indexes": {"dsp_exposed_only": ["1.2"], "asi_in_market": ["1.75"]}},
        "ntb": {"month": "2026-09-01", "buyers": ["41250"]},
    }


def test_new_to_brand_customers_must_state_the_distinct_buyer_count() -> None:
    """Run 3 answered "new-to-brand customers" with NTB purchases (45,524) and passed liveness."""
    turn = next(turn for turn in leg.build_turns(_expected()) if turn.label == "new_customers_lake")
    assert turn.expect == leg.Expectation(
        "answer",
        any_of=(("41250", "count"),),
        source="ntb_buyers",
        metrics_read=("buyers_new_to_brand_12mo",),
        metrics_forbidden=("purchases_new_to_brand_12mo",),
    )
    done = {
        "status": 200,
        "terminal": "done",
        "tool_events": [{"tool": "m", "status": "completed"}],
    }
    buyers = {**done, "released_text": "You acquired 41,250 new-to-brand customers."}
    purchases = {**done, "released_text": "You acquired 45,524 new-to-brand customers."}
    assert leg.turn_verdict(turn.expect, buyers)["verdict"] == leg.PASS
    assert leg.turn_verdict(turn.expect, purchases) == {
        "verdict": leg.FAIL,
        "reasons": ["no_expected_value_stated"],
        "tools": ["m"],
    }


def test_the_outside_campaign_is_asked_by_name_never_by_canonical_key() -> None:
    turns = {turn.label: turn for turn in leg.build_turns(_expected())}
    for label in ("outside_top_n_segments", "outside_top_n_campaign"):
        assert "Northstar DSP | Flight 28" in turns[label].question
        assert "campaign:" not in turns[label].question


def test_every_prompt_is_natural_phrasing_with_no_metric_id_or_selector_syntax() -> None:
    turns = {turn.label: turn for turn in leg.build_turns(_expected())}
    for turn in turns.values():
        assert "=" not in turn.question
        assert not any(
            word in turn.question
            for word in ("time_grain", "profile_path", "cohort_path", "_rate", "segment_")
        )
    index = turns["segment_over_index"]
    assert "DSP-exposed buyers" in index.question
    # Only the DSP-exposed path's indexes are acceptable; another path's never are.
    assert index.expect.any_of == (("1.2", "index"),)


def test_the_index_turn_is_refused_when_another_path_holds_the_same_index() -> None:
    expected = _expected()
    expected["index"] = {"indexes": {"dsp_exposed_only": ["1.2"], "asi_in_market": ["1.2"]}}
    with pytest.raises(RuntimeError, match="another profile path"):
        leg.build_turns(expected)


def _row(prompt: str, **times: object) -> dict[str, object]:
    return {"prompt_id": prompt, **_times("Revenue was $4,121.43."), **times}


def test_answered_latency_reports_answered_turns_only_with_nearest_rank_stats() -> None:
    rows = [
        _row("a", first_text_ms=1_000, stream_end_ms=9_000),
        _row("b", first_text_ms=3_000, stream_end_ms=12_000),
        _row("c", first_text_ms=2_000, stream_end_ms=10_000),
        # A clarification and a refusal are reported apart, never in the answered figures.
        {**_row("d", first_text_ms=500, stream_end_ms=60_000), "released_text": "Which week?"},
        {**_row("e", first_text_ms=400, stream_end_ms=70_000), "terminal": "refused"},
    ]
    summary = leg.latency_summary(rows)
    assert summary["answered"] == ["a", "b", "c"]
    assert sorted(cast("dict[str, object]", summary["not_answered"])) == ["d", "e"]
    assert summary["first_text"] == {"n": 3, "median_ms": 2_000, "p95_ms": 3_000}
    assert summary["stream_end"] == {"n": 3, "median_ms": 10_000, "p95_ms": 12_000}


def test_nearest_rank_never_interpolates() -> None:
    assert leg.nearest_rank([10, 20], 50) == 10
    assert leg.nearest_rank(list(range(1, 21)), 95) == 19
    with pytest.raises(ValueError, match="at least one"):
        leg.nearest_rank([], 50)


def test_turn_attribution_counts_measured_spans_and_extra_tool_round_trips() -> None:
    times = ClientTimes(
        tool_events=[
            {"tool": "measurement_metric_by_grain", "status": "requested"},
            {"tool": "measurement_metric_by_grain", "status": "completed"},
            {"tool": "measurement_metric_by_grain", "status": "requested"},
            {"tool": "measurement_metric_by_grain", "status": "completed"},
        ]
    )
    harness = {"model_calls_ms": [3_000, 2_500, 1_500], "tool_calls_ms": [900, 700]}
    window = [
        {"name": "athena.query", "duration_ms": 600},
        {"name": "tool.measurement_gateway", "duration_ms": 800},
        {"name": "athena.query", "duration_ms": 400},
    ]
    assert leg.turn_attribution(times, harness, window) == {
        "model_calls": 3,
        "model_ms": 7_000,
        "tool_calls": 2,
        "tool_ms": 1_600,
        "athena_queries": 2,
        "athena_ms": 1_000,
        "extra_tool_round_trips": 1,
        "harness_total_ms": None,
    }
    # No Harness trace read back: its figures are None, never zero.
    assert leg.turn_attribution(times, {}, [])["model_ms"] is None


# --- C3: outside the Audiences top-N, from pack 8's own ranking ------------------------------


def test_the_selection_is_the_first_campaign_beyond_the_bound_by_impressions() -> None:
    rows = [
        ("campaign:zz", Decimal(900)),
        ("campaign:aa", Decimal(100)),
        ("campaign:mm", Decimal(500)),
        ("campaign:bb", Decimal(500)),
    ]
    selection = leg.audiences_outside_selection(rows, 2)
    # Ties rank by key, as the bake ranks them; the lexically last key is shown, not selected.
    assert selection["shown"] == ["campaign:zz", "campaign:bb"]
    assert selection["selected_item"] == "campaign:mm"
    assert selection["selected_rank"] == 3
    assert selection["listed"] == 4


def test_the_selection_is_refused_when_no_campaign_lies_beyond_the_bound() -> None:
    with pytest.raises(ValueError, match="none beyond"):
        leg.audiences_outside_selection([("campaign:aa", Decimal(1))], 1)
    with pytest.raises(ValueError, match="repeats"):
        leg.audiences_outside_selection(
            [("campaign:aa", Decimal(1)), ("campaign:aa", Decimal(2))], 1
        )


def test_the_exposure_expectation_reads_only_current_publications() -> None:
    current = {"p|day|2026-09-01": "pub-b", "p|day|2026-09-02": "pub-c"}
    ids = leg.current_publication_ids(
        ["p|day|2026-09-01", "p|day|2026-09-02", "p|day|2026-09-03"], current.get
    )
    # A partition without a CURRENT record serves nothing; superseded publications never appear,
    # and each publication is bound to its own partition.
    assert ids == [("p|day|2026-09-01", "pub-b"), ("p|day|2026-09-02", "pub-c")]
    with pytest.raises(ValueError, match="no partition"):
        leg.current_publication_ids(["p|day|2026-09-03"], current.get)
    with pytest.raises(ValueError, match="governed id"):
        leg.current_publication_ids(["x"], lambda _partition: "pub'; DROP")
    with pytest.raises(ValueError, match="governed id"):
        leg.current_publication_ids(["x' OR '1'='1"], lambda _partition: "pub-a")


# --- C1 and O1: labels and the worst-label alarms ---------------------------------------------


def _verdict(name: str, label: str) -> SimpleNamespace:
    return SimpleNamespace(
        state=EvaluationResultStateV1.COMPLETED,
        label=label,
        evaluator_name=f"adlytics_dev_{name}_v4_0123abcd",
    )


def test_turn_labels_normalize_each_current_dimension() -> None:
    labels = leg.turn_labels(
        [
            _verdict("grounded_correctness", "Partially Grounded"),
            _verdict("internal_consistency", "Consistent"),
            _verdict("helpfulness", "Good"),
        ]
    )
    assert labels == {
        "grounded_correctness": "partially_grounded",
        "internal_consistency": "consistent",
        "helpfulness": "good",
    }


def test_labels_are_not_proven_when_a_turn_lacks_a_dimension() -> None:
    complete = {"grounded_correctness": "grounded", "internal_consistency": "consistent"}
    verdict = leg.labels_verdict(
        {"a": {**complete, "helpfulness": "poor"}, "b": complete, "c": None}
    )
    assert verdict["verdict"] == leg.NOT_PROVEN
    assert verdict["missing"] == {"b": ["helpfulness"], "c": sorted(leg.WORST_LABELS)}
    assert cast("dict[str, list[str]]", verdict["worst_label_turns"])["helpfulness"] == ["a"]


def _judge(
    states: tuple[str, str], *, history: list[str], worst: dict[str, int]
) -> dict[str, object]:
    return leg.alarm_judgement(
        "adlytics-dev-evaluation-helpfulness-worst-label",
        "helpfulness",
        start_state=states[0],
        end_state=states[1],
        history=[
            {"timestamp": at, "summary": f"Alarm updated from OK to {state}"}
            for at, state in (item.split("|") for item in history)
        ],
        worst_label_sent_ms=worst,
    )


_AT = "2026-10-17T03:10:00+00:00"
_AT_MS = 1_792_206_600_000
"""`_AT` in epoch milliseconds."""


def test_an_alarm_ok_over_the_window_passes() -> None:
    assert _judge(("OK", "OK"), history=[], worst={})["verdict"] == leg.PASS


def test_an_alarm_in_the_window_passes_only_when_an_earlier_worst_label_explains_it() -> None:
    alarmed = [f"{_AT}|ALARM", f"{_AT}|OK"]
    assert _judge(("OK", "OK"), history=alarmed, worst={})["verdict"] == leg.FAIL
    explained = _judge(("OK", "OK"), history=alarmed, worst={"reach_week": _AT_MS - 60_000})
    assert explained["verdict"] == leg.PASS
    # A worst label sent after the transition can't have caused it.
    later = _judge(("OK", "OK"), history=alarmed, worst={"reach_week": _AT_MS + 60_000})
    assert later["verdict"] == leg.FAIL


def test_an_alarm_the_window_opens_in_is_never_explained_by_this_run() -> None:
    opened = _judge(("ALARM", "OK"), history=[], worst={"reach_week": _AT_MS})
    assert opened["verdict"] == leg.FAIL
    assert opened["reason"] == "alarm_unexplained:alarm_before_run"


def test_an_alarm_that_never_reads_ok_is_not_proven() -> None:
    assert (
        _judge(("INSUFFICIENT_DATA", "INSUFFICIENT_DATA"), history=[], worst={})["verdict"]
        == leg.NOT_PROVEN
    )


def test_the_leg_fails_on_any_fail_and_is_not_proven_on_any_gap() -> None:
    assert leg.leg_verdict([leg.PASS, leg.DEFERRED_R1, leg.PASS]) == leg.DEFERRED_R1
    assert leg.leg_verdict([leg.PASS, leg.NOT_PROVEN, leg.DEFERRED_R1]) == leg.NOT_PROVEN
    assert leg.leg_verdict([leg.NOT_PROVEN, leg.FAIL]) == leg.FAIL
    assert leg.leg_verdict([leg.PASS]) == leg.PASS


# --- C5: span readback --------------------------------------------------------------------------


def test_spans_need_the_turn_span_and_an_answers_tool_span() -> None:
    answer, absence = leg.Expectation("answer"), leg.Expectation("absence")
    turn = {"route_spans": ["conversation.turn"], "tool_side_spans": []}
    assert leg.spans_verdict(turn, answer) == leg.FAIL
    assert leg.spans_verdict(turn, absence) == leg.PASS
    tooled = {**turn, "tool_side_spans": ["tool.measurement_gateway"]}
    assert leg.spans_verdict(tooled, answer) == leg.PASS
    assert leg.spans_verdict({"route_spans": []}, absence) == leg.FAIL


def _query_span(metric: str, at_ms: int = 1_500) -> dict[str, object]:
    return {
        "name": "athena.query",
        "startTimeUnixNano": at_ms * 1_000_000,
        "attributes": {"metric": metric, "facts": "fct_new_to_brand_buyers"},
    }


def test_the_new_customers_gate_reads_the_measured_metric_never_the_answer_text() -> None:
    """Review r2's counterexamples, which both passed on the answer text alone.

    One is a mixed answer that also calls 45,524 purchases customers. The other is an answer
    whose only measurement read a different metric. The gate reads each query's `metric` span
    attribute instead.
    """
    turn = next(turn for turn in leg.build_turns(_expected()) if turn.label == "new_customers_lake")

    def judged(*documents: dict[str, object]) -> dict[str, object]:
        measured = leg.measured_metrics(documents, sent_ms=1_000, ended_ms=2_000)
        return leg.measured_metrics_verdict(turn.expect, measured)

    assert judged(_query_span("buyers_new_to_brand_12mo")) == {
        "verdict": leg.PASS,
        "reasons": [],
        "measured": ["buyers_new_to_brand_12mo"],
    }
    # Counterexample 1: buyers and NTB purchases both read, so a purchases figure can be called
    # customers beside the right count.
    assert judged(
        _query_span("buyers_new_to_brand_12mo"), _query_span("purchases_new_to_brand_12mo")
    ) == {
        "verdict": leg.FAIL,
        "reasons": ["forbidden_metric_read:purchases_new_to_brand_12mo"],
        "measured": ["buyers_new_to_brand_12mo", "purchases_new_to_brand_12mo"],
    }
    # Counterexample 2: a completed measurement call that read an unrelated metric.
    assert judged(_query_span("impressions")) == {
        "verdict": leg.FAIL,
        "reasons": ["metric_not_read:buyers_new_to_brand_12mo"],
        "measured": ["impressions"],
    }
    # Only this turn's window counts, and only an athena.query span's own attribute.
    assert judged(
        _query_span("buyers_new_to_brand_12mo", at_ms=2_500),
        {**_query_span("buyers_new_to_brand_12mo"), "name": "tool.measurement_gateway"},
    )["reasons"] == ["metric_not_read:buyers_new_to_brand_12mo"]
    # A turn with no metric expectation is never failed by what it read.
    other = next(turn for turn in leg.build_turns(_expected()) if turn.label == "reach_week")
    assert leg.measured_metrics_verdict(other.expect, ["impressions"])["verdict"] == leg.PASS


def test_the_buyer_expectation_pins_one_marketplace_and_keeps_a_zero() -> None:
    """Review r2 #7 and #8: a second marketplace's count, or a dropped zero, must not slip by."""
    assert leg.ntb_buyer_counts([["amazon_us", "0"]]) == {
        "marketplace": "amazon_us",
        "buyers": ["0"],
    }
    assert leg.ntb_buyer_counts([["amazon_us", "41250"], ["amazon_us", None]]) == {
        "marketplace": "amazon_us",
        "buyers": ["41250"],
    }
    with pytest.raises(
        RuntimeError, match=r"one returned marketplace, not \['amazon_ca', 'amazon_us'\]"
    ):
        leg.ntb_buyer_counts([["amazon_us", "41250"], ["amazon_ca", "900"]])


# --- B608: lake text is bound as `?`, never formatted into the SQL ----------------------------


class _LakeClient:
    """Answer each governed read in order and record what was sent; no AWS call is made."""

    def __init__(self, answers: list[list[list[str | None]]]) -> None:
        self._answers = answers
        self.sent: list[dict[str, object]] = []

    def start_query_execution(self, **request: object) -> dict[str, str]:
        self.sent.append(request)
        return {"QueryExecutionId": str(len(self.sent) - 1)}

    def get_query_execution(self, **_request: object) -> dict[str, object]:
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

    def get_paginator(self, _operation: str) -> SimpleNamespace:
        def paginate(**request: str) -> list[dict[str, object]]:
            rows = self._answers[int(request["QueryExecutionId"])]
            header: dict[str, object] = {"Data": []}
            body = [
                {"Data": [{} if cell is None else {"VarCharValue": cell} for cell in row]}
                for row in rows
            ]
            return [{"ResultSet": {"Rows": [header, *body]}}]

        return SimpleNamespace(paginate=paginate)


def _athena(client: _LakeClient) -> object:
    def athena_client(_service: str) -> _LakeClient:
        return client

    session = cast("Any", SimpleNamespace(client=athena_client))
    return leg.ChatAthena(session, "dev")


def test_the_reach_week_read_back_from_the_lake_is_bound_never_formatted() -> None:
    week = "2026-10-05' OR '1'='1"
    client = _LakeClient([[[week, "2026-10-11"]], [["amazon", "exposed", "1200"]], [["0"]]])
    expected = leg._reach_expectations(cast("Any", _athena(client)), "dev")  # pyright: ignore[reportPrivateUsage]
    rows = client.sent[1]
    assert "'1'='1" not in str(rows["QueryString"])
    assert "period_start = CAST(? AS date)" in str(rows["QueryString"])
    assert rows["ExecutionParameters"] == ["'2026-10-05'' OR ''1''=''1'"]
    assert expected["values"] == ["1200"]


def test_the_campaign_name_read_back_from_the_lake_is_bound_never_formatted() -> None:
    bound = leg.audiences_runtime_campaign_bound(1)
    ranked: list[list[str | None]] = [
        [f"campaign:c{index:03d}", str(10_000 - index)] for index in range(bound + 1)
    ]
    name = "O'Brien's -- Fall Sale"
    client = _LakeClient(
        [
            ranked,
            [["seg-1", "40"]],
            [["p|day|2026-09-01"]],
            [["40", "12.5"]],
            [[name]],
            [["1"]],
        ]
    )
    selection = leg._audiences_expectations(  # pyright: ignore[reportPrivateUsage]
        cast("Any", _athena(client)),
        "dev",
        (datetime.date(2026, 9, 1), datetime.date(2026, 9, 30)),
        current_of={"p|day|2026-09-01": "pub-a"}.get,
    )
    namesakes = client.sent[5]
    assert "Brien" not in str(namesakes["QueryString"])
    assert "campaign_name = CAST(? AS varchar)" in str(namesakes["QueryString"])
    assert namesakes["ExecutionParameters"] == ["'O''Brien''s -- Fall Sale'"]
    assert selection["selected_name"] == name
    # Every other read formats only validated identifiers and ISO days, and binds nothing.
    assert all("ExecutionParameters" not in sent for sent in client.sent[:5])
