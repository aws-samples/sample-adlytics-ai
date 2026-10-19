# SPDX-License-Identifier: MIT-0
# ruff: noqa: S608 -- read-only probe SQL; it interpolates only the dev environment name, ISO days
# from date objects, a campaign key that matched the canonical key pattern first and partition and
# publication IDs that matched the governed ID patterns; lake text is bound as `?`
# (A-SQL-VALIDATED-LITERAL-001)
"""D2 live acceptance, chat leg: chat as the governed surface for lake data (dev only).

    uv run --frozen python validation/run_d2_chat_acceptance.py --profile <profile> \
        --environment dev --region us-east-1 --expected-account <account> --output-dir <dir>

Runs one fixed sequence of chat turns as the synthetic `probe-a` persona through the deployed
same-origin chat route, and judges each turn against an outcome declared before it runs:

- **Expected values.** Every figure a turn must state is read first, read-only, from the
  current lake on the CHAT workgroup, and the turn passes only when its released text states it
  at the precision it displays. A turn about a period the lake doesn't hold must answer with a
  governed absence and state no figure.
- **ARP.** `repeat_buyer_rate_arp` per cell for the latest vintage both cohort paths hold
  (pack 15 repeat buyers over pack 14 account-ID cohort size), and the standard-path rate.
- **Outside the Audiences top-N.** Audiences shows the top `audiences_runtime_campaign_bound`
  campaigns of pack 8's month, ranked as the bake ranks them (largest matched-segment
  impressions, then key). The selected campaign is the first one beyond that bound, read from
  the landed pack 8 rows, and chat must answer its segment and campaign figures.
- **Labels.** Each turn's evaluator labels are read through the Quality Review API by the turn's
  correlation, within a bounded wait, and the three `*-worst-label` alarms are judged over the
  run's window: OK passes; an ALARM passes only when a label of this run explains it.
- **Spans.** Each turn's route trace and tool-side spans are read back through
  `probe_turn_latency`'s own trace join.
- **Latency.** One nearest-rank p95 over the turns' client intervals.

A turn refused because a page body is not served (R1, `ADLY-PRESENTATION-0002` /
`ungranted_generation`) is recorded `DEFERRED-R1` with its refusal, never `FAIL`.

Writes only to `--output-dir`. No token, CSRF value or password is printed or written; the
released text, correlation and trace IDs go only to that ignored evidence directory.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import secrets
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

import boto3
import probe_turn_latency as latency
from cognito_personas import app_client, load_persona, published_parameter, sign_in
from probe_ingestion_traceability_gate import nearest_rank_p95
from quality_review_client import (
    ReviewClient,
    ReviewUnavailableError,
    reviewed_evaluation,
    reviewer_client,
)

from adapters.jobs.preset_artifact_publisher.bakeability import audiences_runtime_campaign_bound
from adapters.outbound.athena.execution import StatementRequest, start
from adapters.outbound.cloudformation_stack import stack_resource_summaries
from domain.evaluation.linkage import EvaluationResultStateV1
from domain.evaluation.quality import QUALITY_LABELS, verdict_count

PASS, FAIL, DEFERRED_R1, NOT_PROVEN = "PASS", "FAIL", "DEFERRED-R1", "NOT_PROVEN"
WORST_LABELS: Mapping[str, str] = {
    "grounded_correctness": "unsupported",
    "internal_consistency": "contradictory",
    "helpfulness": "poor",
}
"""Each alarm's worst label (`evaluation-v4-stack.ts` `alarmLabel`, normalized)."""
_R1_MARKERS = ("ADLY-PRESENTATION-0002", "ungranted_generation")
_ABSENCE = re.compile(
    r"not available|isn't available|is not available|unavailable|no data|no governed|"
    r"not yet|hasn't (?:arrived|landed|been)|haven't (?:arrived|landed|been)|"
    r"(?:don't|do not|doesn't|does not) (?:have|hold|contain|cover)|no (?:published|delivered)|"
    r"not (?:been )?(?:published|delivered|landed)|no (?:results|figures|rows)",
    re.IGNORECASE,
)
_NUMBER = re.compile(
    r"(?<![\w.])\$?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<suffix>\s?(?:%|[KMB]\b))?",
)
_SUFFIX = {"K": Decimal(1_000), "M": Decimal(1_000_000), "B": Decimal(1_000_000_000)}
_YEARS = range(2000, 2101)
_SMALL = 32
"""Below this a stated number may be a day of the month or an ordinal, not a measured figure."""


# --- stated figures --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Stated:
    """One figure the released text states: its value, its shown decimals, and whether a %."""

    value: Decimal
    decimals: int
    percent: bool
    scale: Decimal = Decimal(1)


def stated_figures(text: str) -> list[Stated]:
    """Every figure in `text`, as written: `1,234`, `35.8%`, `$129.3K`, `0.36`."""
    found: list[Stated] = []
    for match in _NUMBER.finditer(text):
        raw = match.group("num").replace(",", "")
        suffix = (match.group("suffix") or "").strip()
        try:
            value = Decimal(raw)
        except InvalidOperation:
            continue
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        scale = _SUFFIX.get(suffix, Decimal(1))
        found.append(Stated(value * scale, decimals, suffix == "%", scale))
    return found


def _half_step(stated: Stated) -> Decimal:
    return Decimal(5) * Decimal(10) ** -(stated.decimals + 1) * stated.scale


def states_value(figures: Sequence[Stated], expected: Decimal, kind: str) -> bool:
    """Whether any stated figure is `expected` at the precision the text shows.

    `count` and `money` compare the figure; `rate` accepts a percentage or a fraction;
    `index` accepts the ratio or the ratio x 100. A figure shown with fewer decimals matches
    any value it rounds from (half a shown step either side).
    """
    for stated in figures:
        if kind == "rate":
            target = expected * 100 if stated.percent else expected
        elif kind == "index":
            targets = (expected, expected * 100)
            if any(abs(stated.value - target) <= _half_step(stated) for target in targets):
                return True
            continue
        else:
            if stated.percent:
                continue
            target = expected
        if abs(stated.value - target) <= _half_step(stated):
            return True
    return False


def states_no_figure(figures: Sequence[Stated]) -> bool:
    """Whether the text states no measured figure: only whole days, ordinals and years."""
    return all(
        not stated.percent
        and stated.decimals == 0
        and stated.scale == 1
        and (stated.value < _SMALL or int(stated.value) in _YEARS)
        for stated in figures
    )


# --- expected outcomes (C2) ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Expectation:
    """What one turn must do, declared before it runs.

    `answer`: a 200, a `done` terminal, at least one completed governed tool call, and every
    value in `all_of` (or at least one in `any_of`) stated. With `exclusive`, every percentage
    the text states must also be one of `any_of`'s rates, so a wrong cell's rate can't ride
    along with a right one. `absence`: a governed absence, with no figure stated. `phrases` are
    case-insensitive regexes the text must also match. `source` names the governed read the
    values came from; `liveness` marks a turn that proves only a governed answer, no figure.
    `metrics_read` and `metrics_forbidden` are checked against the turn's measurement queries
    (`measured_metrics_verdict`), never against the answer's text.
    """

    kind: str
    all_of: tuple[tuple[str, str], ...] = ()
    any_of: tuple[tuple[str, str], ...] = ()
    phrases: tuple[str, ...] = ()
    source: str = ""
    exclusive: bool = False
    metrics_read: tuple[str, ...] = ()
    metrics_forbidden: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Turn:
    """One prompt of the sequence: its label, conversation, question and expectation."""

    label: str
    conversation: str
    question: str
    expect: Expectation
    check: str


def r1_refusal(times: Mapping[str, Any]) -> str | None:
    """Return the R1 refusal evidence a turn carries, or None."""
    evidence = f"{times.get('refusal_reason', '')} {times.get('released_text', '')}"
    hits = [marker for marker in _R1_MARKERS if marker in evidence]
    return ", ".join(hits) if hits else None


def _value_reasons(expect: Expectation, figures: Sequence[Stated]) -> list[str]:
    """Name each expected value an answer doesn't state, and each stray rate it does."""
    reasons: list[str] = []
    missing = [
        value for value, kind in expect.all_of if not states_value(figures, Decimal(value), kind)
    ]
    if missing:
        reasons.append(f"values_missing:{','.join(missing)}")
    if expect.any_of and not any(
        states_value(figures, Decimal(value), kind) for value, kind in expect.any_of
    ):
        reasons.append("no_expected_value_stated")
    if expect.exclusive:
        rates = [Decimal(value) for value, kind in expect.any_of if kind == "rate"]
        stray = [
            str(stated.value)
            for stated in figures
            if stated.percent and not any(states_value([stated], rate, "rate") for rate in rates)
        ]
        if stray:
            reasons.append(f"unexpected_rates:{','.join(stray)}")
    return reasons


def turn_verdict(expect: Expectation, times: Mapping[str, Any]) -> dict[str, object]:
    """Judge one turn against its declared outcome; every failed condition is named."""
    deferred = r1_refusal(times)
    if deferred is not None:
        return {"verdict": DEFERRED_R1, "reasons": [f"r1_refusal:{deferred}"]}
    text = str(times.get("released_text", ""))
    figures = stated_figures(text)
    completed = [
        str(event.get("tool"))
        for event in cast("list[Mapping[str, object]]", times.get("tool_events", []))
        if event.get("status") == "completed"
    ]
    reasons: list[str] = []
    if times.get("status") != 200:  # noqa: PLR2004
        reasons.append(f"http_{times.get('status')}")
    if expect.kind == "answer":
        if times.get("terminal") != "done":
            reasons.append(f"terminal_{times.get('terminal')}")
        if not completed:
            reasons.append("no_governed_tool_call")
        if states_no_figure(figures):
            reasons.append("no_figure_stated")
        reasons.extend(_value_reasons(expect, figures))
    elif expect.kind == "absence":
        if times.get("terminal") not in {"done", "refused"}:
            reasons.append(f"terminal_{times.get('terminal')}")
        if _ABSENCE.search(f"{text} {times.get('refusal_reason', '')}") is None:
            reasons.append("no_governed_absence")
        if not states_no_figure(figures):
            reasons.append("figure_stated_for_absent_period")
    else:
        raise ValueError(f"unknown expectation kind {expect.kind!r}")
    reasons.extend(
        f"phrase_missing:{phrase}"
        for phrase in expect.phrases
        if re.search(phrase, text, re.IGNORECASE) is None
    )
    return {"verdict": FAIL if reasons else PASS, "reasons": reasons, "tools": completed}


# --- outside the Audiences top-N (C3) --------------------------------------------------------

AUDIENCES_SELECTION = "audiences-first-campaign-beyond-bound-v1"
AUDIENCES_RANK_SQL = (
    "SELECT campaign_key, max(coalesce(impressions, 0)) AS largest "
    "FROM adlytics_{environment}_v2_current.fct_segment_delivery "
    "WHERE period_start = DATE '{start}' AND period_end = DATE '{end}' "
    "AND grain_set = 'campaign' AND segment_namespace = 'amc_dsp_behavior' "
    "GROUP BY campaign_key"
)
"""Pack 8's month at campaign grain, DSP behaviour segments only (`_campaign_base`)."""


def audiences_outside_selection(
    rows: Sequence[tuple[str, Decimal]], bound: int
) -> dict[str, object]:
    """Rank pack 8's campaigns as the Audiences bake does and pick the first beyond the bound.

    The bake ranks by largest matched-segment impressions, descending, then campaign key
    (`AudiencesSource._ranked`), and shows the first `bound`. The selection is the campaign
    ranked `bound + 1`: listed by pack 8, not shown by the page. Refused when none is beyond.
    """
    if bound < 1:
        raise ValueError("the Audiences campaign bound must be positive")
    keys = [key for key, _ in rows]
    if len(set(keys)) != len(keys):
        raise ValueError("pack 8's campaign list repeats a campaign")
    ranked = sorted(rows, key=lambda row: (-row[1], row[0]))
    if len(ranked) <= bound:
        raise ValueError(f"pack 8 lists {len(ranked)} campaigns, none beyond the bound {bound}")
    return {
        "selection": AUDIENCES_SELECTION,
        "bound": bound,
        "listed": len(ranked),
        "shown": [key for key, _ in ranked[:bound]],
        "selected_item": ranked[bound][0],
        "selected_rank": bound + 1,
        "selected_largest_impressions": str(ranked[bound][1]),
    }


# --- labels and the worst-label alarms (C1, O1) ----------------------------------------------


def turn_labels(verdicts: Sequence[Any]) -> dict[str, str]:
    """Each current dimension's normalized label from one turn's completed verdicts."""
    labels: dict[str, str] = {}
    for verdict in verdicts:
        if verdict.state is not EvaluationResultStateV1.COMPLETED or not verdict.label:
            continue
        normalized = str(verdict.label).strip().lower().replace(" ", "_")
        counted = verdict_count(verdict.evaluator_name, normalized)
        if counted.dimension in QUALITY_LABELS:
            labels[counted.dimension] = counted.label
    return labels


def labels_verdict(labels: Mapping[str, Mapping[str, str] | None]) -> dict[str, object]:
    """Every turn carries a label for each current dimension, or the check is not proven."""
    missing = {
        turn: sorted(set(WORST_LABELS) - set(found or {}))
        for turn, found in labels.items()
        if found is None or set(WORST_LABELS) - set(found)
    }
    worst = {
        dimension: sorted(
            turn for turn, found in labels.items() if (found or {}).get(dimension) == label
        )
        for dimension, label in WORST_LABELS.items()
    }
    return {
        "verdict": NOT_PROVEN if missing else PASS,
        "missing": missing,
        "worst_label_turns": worst,
    }


def alarm_judgement(
    alarm: str,
    dimension: str,
    *,
    start_state: str,
    end_state: str,
    history: Sequence[Mapping[str, str]],
    worst_label_sent_ms: Mapping[str, int],
) -> dict[str, object]:
    """O1: OK over the window passes; any ALARM passes only when this run's labels explain it.

    An ALARM the window opens in predates every turn of this run, so no label of it explains
    that ALARM: it fails. A transition to ALARM inside the window is explained only by a turn of
    this run that carries the dimension's worst label and was sent before the transition.
    Without any ALARM, the window passes when it ends OK and is not proven otherwise
    (insufficient data).
    """
    sent = sorted(worst_label_sent_ms.items(), key=lambda item: item[1])
    unexplained: list[str] = []
    explained: list[str] = []
    if start_state == "ALARM":
        unexplained.append("alarm_before_run")
    for item in history:
        if _to_state(item) != "ALARM":
            continue
        at = datetime.datetime.fromisoformat(item["timestamp"]).timestamp() * 1000
        causes = [turn for turn, sent_ms in sent if sent_ms <= at]
        (explained if causes else unexplained).append(
            f"{item['timestamp']}:{','.join(causes) or 'no_worst_label_before'}"
        )
    if end_state == "ALARM" and not explained and not unexplained:
        unexplained.append("ends_in_alarm_without_transition")
    if unexplained:
        verdict, reason = FAIL, f"alarm_unexplained:{';'.join(unexplained)}"
    elif explained:
        verdict, reason = PASS, f"alarm_explained_by:{';'.join(explained)}"
    elif end_state == "OK":
        verdict, reason = PASS, "ok_over_window"
    else:
        verdict, reason = NOT_PROVEN, f"end_state_{end_state}"
    return {
        "alarm": alarm,
        "dimension": dimension,
        "start_state": start_state,
        "end_state": end_state,
        "updates_in_window": list(history),
        "verdict": verdict,
        "reason": reason,
    }


def leg_verdict(verdicts: Sequence[str]) -> str:
    """Roll the leg's check verdicts up: any FAIL fails, then NOT_PROVEN, then DEFERRED-R1."""
    for outcome in (FAIL, NOT_PROVEN, DEFERRED_R1):
        if outcome in verdicts:
            return outcome
    return PASS


def _to_state(item: Mapping[str, str]) -> str:
    match = re.search(r"to (OK|ALARM|INSUFFICIENT_DATA)", item.get("summary", ""))
    return match.group(1) if match else "UNKNOWN"


# --- live reads --------------------------------------------------------------------------------


class ChatAthena:
    """Read-only governed reads on the CHAT workgroup, each recorded with its query ID."""

    def __init__(self, session: boto3.Session, environment: str) -> None:
        self._client: Any = session.client("athena")
        self._workgroup = latency.chat_workgroup(environment)
        self._database = f"adlytics_{environment}_v2_current"
        self.queries: list[dict[str, object]] = []

    def rows(
        self, label: str, sql: str, parameters: tuple[str, ...] = ()
    ) -> list[list[str | None]]:
        """Run one read and return its data rows; raise `RuntimeError` unless it succeeded."""
        # Through the repository's one sanctioned start wrapper, with result reuse off. A value
        # read back from the lake is bound as `?` there, never formatted into `sql`.
        query_id = start(
            self._client, StatementRequest(sql, parameters, self._workgroup, self._database, 0)
        )
        state = "QUEUED"
        for _ in range(120):
            status = self._client.get_query_execution(QueryExecutionId=query_id)
            state = str(status["QueryExecution"]["Status"]["State"])
            if state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                break
            time.sleep(1)
        self.queries.append(
            {
                "label": label,
                "sql": sql,
                "parameters": list(parameters),
                "query_execution_id": query_id,
            }
        )
        if state != "SUCCEEDED":
            raise RuntimeError(f"governed read {label} ended {state}")
        found: list[list[str | None]] = []
        first = True
        for page in self._client.get_paginator("get_query_results").paginate(
            QueryExecutionId=query_id
        ):
            rows = page["ResultSet"]["Rows"]
            for row in rows[1:] if first else rows:
                found.append([cell.get("VarCharValue") for cell in row["Data"]])
            first = False
        return found


def _last_complete_month(today: datetime.date) -> tuple[datetime.date, datetime.date]:
    end = today.replace(day=1) - datetime.timedelta(days=1)
    return end.replace(day=1), end


def _ratio(numerator: str | None, denominator: str | None) -> Decimal | None:
    if numerator is None or denominator is None or Decimal(denominator) == 0:
        return None
    return Decimal(numerator) / Decimal(denominator)


def _arp_expectations(athena: ChatAthena, environment: str) -> dict[str, object]:
    """ARP and standard rates per cell for the latest vintage both cohort paths hold."""
    db = f"adlytics_{environment}_v2_current"
    keys = "as_of_month, cohort_month, acquisition_channel_group, months_since_acquisition"
    join = " AND ".join(f"a.{key} = s.{key}" for key in (*keys.split(", "), "marketplace_key"))
    sql = (
        f"WITH s AS (SELECT * FROM {db}.fct_shopper_acquisition WHERE cohort_path = 'standard' "  # nosec B608 # A-SQL-VALIDATED-LITERAL-001
        f"AND cell_state = 'returned'), a AS (SELECT * FROM {db}.fct_shopper_acquisition WHERE "
        f"cohort_path = 'arp' AND cell_state = 'returned') "
        f"SELECT CAST(s.as_of_month AS varchar), CAST(s.cohort_month AS varchar), "
        f"s.acquisition_channel_group, s.months_since_acquisition, a.repeat_buyers, "
        f"s.cohort_buyers_account_ids, a.cohort_buyers_account_ids_check, s.repeat_buyers, "
        f"s.cohort_buyers_new_to_brand_12mo, s.cohort_grain, a.cohort_grain, "
        f"s.repeat_buyers_account_ids "
        f"FROM s JOIN a ON {join} "
        f"WHERE s.as_of_month = (SELECT max(s2.as_of_month) FROM s s2 JOIN a a2 ON "
        f"a2.as_of_month = s2.as_of_month) ORDER BY 2, 3, 4"
    )
    rows = athena.rows("arp_cells", sql)
    arp: list[str] = []
    standard: list[str] = []
    withheld = 0
    for row in rows:
        # The ARP rate: pack 15 (arp) repeat buyers over pack 14 (standard) account-ID cohort.
        num, standard_account_ids, check, std_num, std_den, s_grain, a_grain = row[4:11]
        den = standard_account_ids
        rate = _ratio(num, den)
        if (
            rate is None
            or check != den
            or Decimal(num or 0) > Decimal(den or 0)
            or s_grain != a_grain
        ):
            withheld += 1
        else:
            arp.append(str(rate))
        # The two standard-path rates, both operands from pack 14: `repeat_buyer_rate` and
        # `repeat_buyer_rate_account_ids` (account-ID repeat buyers over account-ID cohort).
        for std in (_ratio(std_num, std_den), _ratio(row[11], standard_account_ids)):
            if std is not None:
                standard.append(str(std))
    if not rows or not arp or not standard:
        raise RuntimeError("no vintage holds both cohort paths with a served rate")
    return {
        "vintage": rows[0][0],
        "cells": len(rows),
        "withheld": withheld,
        "arp_rates": sorted(set(arp)),
        "standard_rates": sorted(set(standard)),
    }


_PUBLICATION_ID = re.compile(r"^[A-Za-z0-9:_-]+$")
_PARTITION_KEY = re.compile(r"^[A-Za-z0-9|:_.-]+$")


def current_publication_ids(
    partitions: Sequence[str], current_of: Callable[[str], str | None]
) -> list[tuple[str, str]]:
    """Return the CURRENT publication of each partition, the only rows a governed read serves.

    `fct_ad_exposure` keeps every publication's rows, so a sum over the table counts superseded
    landings too; the governed read selects one CURRENT publication per partition. Each pair is
    (partition, its CURRENT publication), so a read keyed on both never takes a publication's
    rows from another partition. A partition with no CURRENT record serves nothing and
    contributes none. Refused when no partition serves.
    """
    pairs = sorted(
        (partition, current)
        for partition in set(partitions)
        for current in [current_of(partition)]
        if current is not None
    )
    if not pairs:
        raise ValueError("no partition of the read has a CURRENT publication")
    if any(
        _PARTITION_KEY.fullmatch(partition) is None or _PUBLICATION_ID.fullmatch(current) is None
        for partition, current in pairs
    ):
        raise ValueError("a CURRENT publication id or partition key is not a governed id")
    return pairs


def _publications_reader(session: boto3.Session, environment: str) -> Callable[[str], str | None]:
    """Read CURRENT records from the table the deployed measurement tool itself reads."""
    cloudformation: Any = session.client("cloudformation")
    lambda_client: Any = session.client("lambda")
    resources = cast(
        "list[dict[str, str]]",
        stack_resource_summaries(cloudformation, f"Adlytics-Agent-{environment}"),
    )
    function = next(
        resource["PhysicalResourceId"]
        for resource in resources
        if resource["ResourceType"] == "AWS::Lambda::Function"
        and resource["LogicalResourceId"].startswith("MeasurementTool")
    )
    variables = lambda_client.get_function_configuration(FunctionName=function)["Environment"][
        "Variables"
    ]
    table = str(variables["ADL_SEMANTIC_PUBLICATIONS_TABLE"])
    dynamodb: Any = session.client("dynamodb")

    def current_of(partition: str) -> str | None:
        item = dynamodb.get_item(
            TableName=table,
            Key={"partition_key": {"S": partition}, "record_key": {"S": "CURRENT"}},
            ConsistentRead=True,
        ).get("Item")
        return None if item is None else str(item["publication_id"]["S"])

    return current_of


def _audiences_expectations(
    athena: ChatAthena,
    environment: str,
    month: tuple[datetime.date, datetime.date],
    *,
    current_of: Callable[[str], str | None],
) -> dict[str, object]:
    start, end = (day.isoformat() for day in month)
    sql = AUDIENCES_RANK_SQL.format(environment=environment, start=start, end=end)
    ranked = athena.rows("audiences_rank", sql)
    selection = audiences_outside_selection(
        [(str(key), Decimal(str(value))) for key, value in ranked],
        audiences_runtime_campaign_bound(1),
    )
    selection.update({"month": [start, end], "sql": sql})
    key = str(selection["selected_item"])
    if latency._CAMPAIGN_KEY.fullmatch(key) is None:  # pyright: ignore[reportPrivateUsage]
        raise RuntimeError("the selected campaign is not a canonical campaign key")
    db = f"adlytics_{environment}_v2_current"
    segments = athena.rows(
        "outside_segments",
        f"SELECT segment_id, impressions FROM {db}.fct_segment_delivery WHERE period_start = "  # nosec B608 # A-SQL-VALIDATED-LITERAL-001
        f"DATE '{start}' AND period_end = DATE '{end}' AND grain_set = 'campaign' AND "
        f"segment_namespace = 'amc_dsp_behavior' AND campaign_key = '{key}' ORDER BY 2 DESC",
    )
    window = f"campaign_key = '{key}' AND date_key BETWEEN DATE '{start}' AND DATE '{end}'"
    partitions = athena.rows(
        "outside_exposure_partitions",
        f"SELECT DISTINCT partition_key FROM {db}.fct_ad_exposure WHERE {window}",  # nosec B608 # A-SQL-VALIDATED-LITERAL-001
    )
    served = current_publication_ids([str(row[0]) for row in partitions], current_of)
    listed = " OR ".join(
        f"(partition_key = '{partition}' AND publication_id = '{current}')"
        for partition, current in served
    )
    exposure = athena.rows(
        "outside_exposure",
        f"SELECT sum(impressions), sum(spend) FROM {db}.fct_ad_exposure WHERE {window} AND "  # nosec B608 # A-SQL-VALIDATED-LITERAL-001
        f"NOT rows_suppressed AND currency_code = 'USD' AND ({listed})",
    )
    selection["exposure_current_publications"] = len(served)
    names = athena.rows(
        "outside_name",
        f"SELECT DISTINCT campaign_name FROM {db}.dim_campaign WHERE campaign_key = '{key}' "  # nosec B608 # A-SQL-VALIDATED-LITERAL-001
        f"AND is_current",
    )
    if len(names) != 1 or not names[0][0]:
        raise RuntimeError("the selected campaign has no single current name")
    name = str(names[0][0])
    # The name is lake text an advertiser wrote, so it is bound, never formatted into the SQL.
    namesakes = athena.rows(
        "outside_namesakes",
        f"SELECT count(DISTINCT campaign_key) FROM {db}.dim_campaign WHERE is_current AND "  # nosec B608 # A-SQL-VALIDATED-LITERAL-001
        "campaign_name = CAST(? AS varchar)",
        (name,),
    )
    if namesakes[0][0] != "1":
        raise RuntimeError("the selected campaign's name is not unique among current campaigns")
    # Chat resolves a campaign by its customer-facing name; a canonical key is never user text.
    selection["selected_name"] = name
    selection["segment_impressions"] = [str(value) for _, value in segments if value]
    selection["exposure_impressions"], selection["exposure_spend"] = exposure[0]
    return selection


def _reach_expectations(athena: ChatAthena, environment: str) -> dict[str, object]:
    db = f"adlytics_{environment}_v2_current.fct_exposure_group_outcome"
    weeks = athena.rows(
        "reach_weeks",
        f"SELECT CAST(max(period_start) AS varchar), CAST(max(period_end) AS varchar) FROM {db} "  # nosec B608 # A-SQL-VALIDATED-LITERAL-001
        f"WHERE time_grain = 'week'",
    )
    start, end = (cast("str", value) for value in weeks[0])
    # The week is read back from the lake, so it is bound, never formatted into the SQL.
    rows = athena.rows(
        "reach_rows",
        f"SELECT identity_space, exposure_group, shoppers_reached FROM {db} WHERE time_grain = "  # nosec B608 # A-SQL-VALIDATED-LITERAL-001
        "'week' AND period_start = CAST(? AS date)",
        (start,),
    )
    values = {str(row[2]) for row in rows if row[2]}
    after = datetime.date.fromisoformat(end) + datetime.timedelta(days=1)
    absent = (after, after + datetime.timedelta(days=6))
    held = athena.rows(
        "reach_absent_week",
        f"SELECT count(*) FROM {db} WHERE time_grain = 'week' AND period_start = "  # nosec B608 # A-SQL-VALIDATED-LITERAL-001
        f"DATE '{absent[0].isoformat()}'",
    )
    if held[0][0] != "0":
        raise RuntimeError("the week after the latest reach week is already held")
    return {
        "week": [start, end],
        "values": sorted(values),
        "absent_week": [day.isoformat() for day in absent],
    }


def _index_expectations(
    athena: ChatAthena, environment: str, month: tuple[datetime.date, datetime.date]
) -> dict[str, object]:
    db = f"adlytics_{environment}_v2_current.fct_segment_profile"
    start = month[0].isoformat()
    rows = athena.rows(
        "segment_index",
        f"WITH t AS (SELECT profile_path, max(profiled_buyers_total) pb, "  # nosec B608 # A-SQL-VALIDATED-LITERAL-001
        f"max(profiled_exposed_total) pe FROM {db} WHERE row_kind = 'profiled_total' AND "
        f"cell_state = 'returned' AND period_start = DATE '{start}' GROUP BY 1) "
        f"SELECT s.profile_path, s.segment_id, s.segment_buyers, s.segment_exposed_shoppers, "
        f"t.pb, t.pe FROM {db} s JOIN t ON t.profile_path = s.profile_path WHERE "
        f"s.row_kind = 'segment' AND s.period_start = DATE '{start}'",
    )
    indexes: dict[str, list[str]] = {}
    for path, _, buyers, exposed, profiled_buyers, profiled_exposed in rows:
        buyer_share = _ratio(buyers, profiled_buyers)
        reached_share = _ratio(exposed, profiled_exposed)
        if buyer_share is not None and reached_share:
            indexes.setdefault(str(path), []).append(str(buyer_share / reached_share))
    return {
        "month": start,
        "indexes": {path: sorted(set(values)) for path, values in sorted(indexes.items())},
        "rows": len(rows),
    }


def _ntb_buyer_expectations(
    athena: ChatAthena, environment: str, month: tuple[datetime.date, datetime.date]
) -> dict[str, object]:
    """Read the month's distinct new-to-brand buyers per marketplace (pack 1, platform grain).

    "New-to-brand customers" is people, so the answer must state this count, never the NTB
    purchases figure run 3 called customers.
    """
    db = f"adlytics_{environment}_v2_current.fct_new_to_brand_buyers"
    rows = athena.rows(
        "ntb_buyers",
        f"SELECT marketplace_key, buyers_new_to_brand_12mo FROM {db} WHERE grain_set = "  # nosec B608 # A-SQL-VALIDATED-LITERAL-001
        f"'platform' AND time_grain = 'month' AND period_start = DATE '{month[0].isoformat()}' "
        f"AND cell_state = 'returned'",
    )
    return {"month": month[0].isoformat(), **ntb_buyer_counts(rows)}


def ntb_buyer_counts(rows: Sequence[Sequence[str | None]]) -> dict[str, object]:
    """Pin the one marketplace the question means and its returned buyer count.

    The question names no marketplace, so it is one population only where one marketplace
    returned a cell; with two, any one count would pass for a population the question didn't
    choose, so the run refuses. A returned zero is a count to state, never a missing cell.
    """
    returned = [(str(row[0]), str(row[1])) for row in rows if row[1] is not None]
    marketplaces = sorted({marketplace for marketplace, _count in returned})
    if len(marketplaces) != 1:
        raise RuntimeError(f"new-to-brand buyers need one returned marketplace, not {marketplaces}")
    return {"marketplace": marketplaces[0], "buyers": sorted({count for _m, count in returned})}


INDEX_PATH = "dsp_exposed_only"
"""The standard profile path (pack 12), the one every deployment holds without a paid pack."""


def _days(start: str, end: str) -> str:
    return " to ".join(
        datetime.date.fromisoformat(day).strftime("%B %-d, %Y") for day in (start, end)
    )


def build_turns(expected: Mapping[str, Any]) -> list[Turn]:
    """Build the fixed sequence, each prompt bound to the outcome read before the run.

    Every prompt is natural business phrasing with no metric id, grain or selector syntax
    (owner, 2026-10-17): the model chooses them from the tool descriptions, and a selector the
    metric allows only one value for is pinned server-side. The outside campaign is asked by
    its current name, because a canonical key is never customer text.
    """
    arp, audiences = expected["arp"], expected["audiences"]
    reach, index, ntb = expected["reach"], expected["index"], expected["ntb"]
    asked = set(index["indexes"].get(INDEX_PATH, ()))
    other = {
        value for path, values in index["indexes"].items() if path != INDEX_PATH for value in values
    }
    if asked & other:
        # The prompt names the DSP-exposed population in words, so a stated index proves the
        # path only when no other path holds the same value.
        raise RuntimeError("a DSP-exposed index equals another profile path's index")
    month = datetime.date.fromisoformat(audiences["month"][0]).strftime("%B %Y")
    name = audiences["selected_name"]
    vintage_day = datetime.date.fromisoformat(arp["vintage"])
    vintage_phrase = rf"{vintage_day:%B}(?: \d{{1,2}},)? {vintage_day.year}|{arp['vintage'][:7]}"
    sequence = [
        Turn(
            "arp_repeat_rate",
            "a",
            "For our latest acquisition cohorts, what share of new customers bought again "
            "anywhere on Amazon, counting all Amazon purchases? Which cohort vintage is that?",
            Expectation(
                "answer",
                any_of=tuple((rate, "rate") for rate in arp["arp_rates"]),
                phrases=(vintage_phrase, r"mixed|all (?:Amazon )?purchases"),
                source="arp_cells",
                exclusive=True,
            ),
            "arp",
        ),
        Turn(
            "standard_cohort",
            "b",
            "For our latest acquisition cohorts, what's the repeat buyer rate from ad-touched "
            "purchases? Which cohort vintage is that?",
            Expectation(
                "answer",
                any_of=tuple((rate, "rate") for rate in arp["standard_rates"]),
                phrases=(vintage_phrase,),
                source="arp_cells",
                exclusive=True,
            ),
            "arp",
        ),
        Turn(
            "outside_top_n_segments",
            "c",
            f"How many impressions did {name} deliver to each audience segment in {month}?",
            Expectation(
                "answer",
                all_of=tuple((value, "count") for value in audiences["segment_impressions"]),
                source="outside_segments",
            ),
            "outside_top_n",
        ),
        Turn(
            "outside_top_n_campaign",
            "c",
            f"What did {name} spend in {month}, and how many impressions did it get?",
            Expectation(
                "answer",
                all_of=(
                    (str(audiences["exposure_impressions"]), "count"),
                    (str(audiences["exposure_spend"]), "money"),
                ),
                source="outside_exposure",
            ),
            "outside_top_n",
        ),
        Turn(
            "reach_week",
            "d",
            "How many shoppers did we reach in the week of "
            f"{_days(*reach['week'])}, by exposure group and ID type?",
            Expectation(
                "answer",
                all_of=tuple((value, "count") for value in reach["values"]),
                source="reach_rows",
            ),
            "lake",
        ),
        Turn(
            "reach_absent_week",
            "e",
            "How many shoppers did we reach in the week of "
            f"{_days(*reach['absent_week'])}, by exposure group and ID type?",
            Expectation("absence", source="reach_absent_week"),
            "lake",
        ),
        Turn(
            "segment_over_index",
            "f",
            f"Among our DSP-exposed buyers, which audience segments over-indexed in {month}?",
            Expectation(
                "answer",
                any_of=tuple((value, "index") for value in index["indexes"].get(INDEX_PATH, ())),
                source="segment_index",
            ),
            "lake",
        ),
        Turn(
            "retail_lake",
            "g",
            "What was retail revenue shipped by product in the US marketplace on August 11, 2026?",
            Expectation("answer", source="liveness"),
            "lake_liveness",
        ),
        Turn(
            "new_customers_lake",
            "h",
            f"How many new-to-brand customers did we acquire in {month}?",
            Expectation(
                "answer",
                any_of=tuple((value, "count") for value in ntb["buyers"]),
                source="ntb_buyers",
                # Customers are people: the turn must read the distinct-buyer metric, and any
                # read of NTB purchases puts a purchases figure in a customers answer (run 3).
                metrics_read=("buyers_new_to_brand_12mo",),
                metrics_forbidden=("purchases_new_to_brand_12mo",),
            ),
            "lake",
        ),
    ]
    if any(
        turn.expect.kind == "answer"
        and turn.expect.source != "liveness"
        and not (turn.expect.all_of or turn.expect.any_of)
        for turn in sequence
    ):
        raise RuntimeError("a value turn has no expected value to state")
    return sequence


def _poll_labels(
    review: ReviewClient, correlations: Mapping[str, str], timeout_seconds: int
) -> dict[str, dict[str, object]]:
    """Read each turn's evaluation through the review API until every label lands or time out."""
    deadline = time.monotonic() + timeout_seconds
    found: dict[str, dict[str, object]] = {
        label: {"state": "receipt_absent", "labels": None} for label in correlations
    }
    pending = dict(correlations)
    while pending and time.monotonic() < deadline:
        for label, correlation in list(pending.items()):
            try:
                document = review.turn(correlation)
            except ReviewUnavailableError as error:
                found[label] = {"state": f"review_unavailable:{error}", "labels": None}
                continue
            if document is None:
                continue
            evaluation = reviewed_evaluation(document)
            if evaluation is None:
                found[label] = {"state": "not_admitted", "labels": None}
                del pending[label]
                continue
            labels = turn_labels(evaluation.verdicts)
            found[label] = {
                "state": evaluation.state,
                "labels": labels,
                "verdicts": [
                    {
                        "evaluator": item.evaluator_name,
                        "state": item.state.value,
                        "label": item.label,
                    }
                    for item in evaluation.verdicts
                ],
            }
            if not set(WORST_LABELS) - set(labels):
                del pending[label]
        if pending:
            time.sleep(30)
    return found


def _alarm_reads(
    session: boto3.Session, environment: str, since: datetime.datetime
) -> dict[str, dict[str, object]]:
    cloudwatch: Any = session.client("cloudwatch")
    now = datetime.datetime.now(datetime.UTC)
    reads: dict[str, dict[str, object]] = {}
    for dimension in WORST_LABELS:
        name = f"adlytics-{environment}-evaluation-{dimension.replace('_', '-')}-worst-label"
        # aws-pagination: one exact alarm name returns at most one alarm; the count is checked.
        alarms = cloudwatch.describe_alarms(AlarmNames=[name])["MetricAlarms"]
        if len(alarms) != 1:
            raise RuntimeError(f"alarm {name} does not read back exactly once")
        history: list[dict[str, str]] = []
        for page in cloudwatch.get_paginator("describe_alarm_history").paginate(
            AlarmName=name, HistoryItemType="StateUpdate", StartDate=since, EndDate=now
        ):
            history.extend(
                {"timestamp": item["Timestamp"].isoformat(), "summary": item["HistorySummary"]}
                for item in page.get("AlarmHistoryItems", [])
            )
        reads[dimension] = {
            "alarm": name,
            "state": alarms[0]["StateValue"],
            "history": sorted(history, key=lambda item: item["timestamp"]),
        }
    return reads


def _span_readback(
    logs: Any, group: str, times: latency.ClientTimes, *, environment: str
) -> dict[str, object]:
    """C5: the turn's route trace and tool-side spans, through probe_turn_latency's join."""
    timing = latency._timing_line(logs, group, times)  # pyright: ignore[reportPrivateUsage]
    trace_id = str(timing.get("trace_id", ""))
    route = (
        latency._route_spans(logs, trace_id, times, environment=environment)  # pyright: ignore[reportPrivateUsage]
        if trace_id
        else []
    )
    window = latency._window_spans(  # pyright: ignore[reportPrivateUsage]
        logs, times, ("tool.", "athena.query"), environment=environment
    )
    harness = latency.harness_segments(
        latency._harness_trace(  # pyright: ignore[reportPrivateUsage]
            logs,
            trace_id=trace_id,
            service_prefix=latency.harness_service_prefix(environment),
            times=times,
        )
    )
    query = latency._span_query(  # pyright: ignore[reportPrivateUsage]
        times, environment, span_name_prefixes=(MEASURED_QUERY_SPAN,)
    )
    documents: list[dict[str, object]] = [] if query is None else logs.spans(query)
    return {
        "trace_id": trace_id,
        "timing_line": bool(timing),
        "route_spans": sorted({str(span["name"]) for span in route}),
        "tool_side_spans": sorted({str(span["name"]) for span in window}),
        "attribution": turn_attribution(times, harness, window),
        "metrics_read": list(
            measured_metrics(documents, sent_ms=times.sent_epoch_ms, ended_ms=times.ended_epoch_ms)
        ),
    }


MEASURED_QUERY_SPAN = "athena.query"
"""The span every measurement query runs in, carrying the planned metric's name as `metric`."""


def measured_metrics(
    documents: Sequence[Mapping[str, object]], *, sent_ms: int, ended_ms: int
) -> tuple[str, ...]:
    """Name the metrics the turn's measurement queries read, from their spans' structure.

    `AthenaQueryAdapter._execute` opens `athena.query` with `metric=` the planned metric's
    dictionary name, so this is the metric the tool measured, whatever the answer says.
    """
    found: set[str] = set()
    for document in documents:
        start = document.get("startTimeUnixNano")
        attributes = document.get("attributes")
        if document.get("name") != MEASURED_QUERY_SPAN or not isinstance(attributes, Mapping):
            continue
        if not isinstance(start, int | float | str) or not (
            sent_ms <= round(float(start) / 1e6) <= ended_ms
        ):
            continue
        metric = cast("Mapping[str, object]", attributes).get("metric")
        if isinstance(metric, str) and metric:
            found.add(metric)
    return tuple(sorted(found))


def measured_metrics_verdict(expect: Expectation, measured: Sequence[str]) -> dict[str, object]:
    """Fail a turn that didn't read a required metric or read a forbidden one."""
    reasons = [
        *(f"metric_not_read:{name}" for name in expect.metrics_read if name not in measured),
        *(f"forbidden_metric_read:{name}" for name in expect.metrics_forbidden if name in measured),
    ]
    return {"verdict": FAIL if reasons else PASS, "reasons": reasons, "measured": list(measured)}


def turn_attribution(
    times: latency.ClientTimes,
    harness: Mapping[str, object],
    window: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Where one turn's time went, from measured spans only; an absent source reads None.

    Model calls and tool calls are the Harness runtime's own spans of the turn's trace
    (`harness_segments`); Athena time is the `athena.query` spans in the turn's window. Tool
    round trips beyond the first are retries or follow-up reads: the client stream does not
    say which, so they are counted, never labelled one or the other.
    """
    requested = sum(1 for event in times.tool_events if event.get("status") == "requested")
    model = cast("list[int] | None", harness.get("model_calls_ms"))
    tools = cast("list[int] | None", harness.get("tool_calls_ms"))
    athena = [cast("int", span["duration_ms"]) for span in window if span["name"] == "athena.query"]
    return {
        "model_calls": None if model is None else len(model),
        "model_ms": None if model is None else sum(model),
        "tool_calls": requested,
        "tool_ms": None if tools is None else sum(tools),
        "athena_queries": len(athena),
        "athena_ms": sum(athena),
        "extra_tool_round_trips": max(requested - 1, 0),
        "harness_total_ms": harness.get("harness_total_ms"),
    }


def answered(times: Mapping[str, Any]) -> bool:
    """Whether a turn answered: 200, `done`, a completed governed tool call and a figure.

    A refusal, a failure, or a `done` turn that only asks for clarification is not an answer,
    so its latency is reported apart (owner, 2026-10-17).
    """
    completed = any(
        event.get("status") == "completed"
        for event in cast("list[Mapping[str, object]]", times.get("tool_events", []))
    )
    return (
        times.get("status") == 200  # noqa: PLR2004
        and times.get("terminal") == "done"
        and completed
        and not states_no_figure(stated_figures(str(times.get("released_text", ""))))
    )


def nearest_rank(values: Sequence[int], percent: int) -> int:
    """Return the nearest-rank percentile, `sorted(values)[ceil(percent/100*n)-1]`."""
    if not values:
        raise ValueError("a percentile needs at least one value")
    ordered = sorted(values)
    return ordered[-(-percent * len(ordered) // 100) - 1]


def latency_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, object]:
    """Answered turns' first-text and stream-end latency, apart from every other turn's.

    `first_text_ms` is send to the first text chunk the client renders; `stream_end_ms` is
    send to the end of the body. Median and p95 are nearest-rank over answered turns only. A
    measurement, with no pass or fail bar.
    """
    answers = [row for row in rows if answered(row)]
    others = [row for row in rows if not answered(row)]

    def stats(name: str) -> dict[str, object]:
        values = [cast("int", row[name]) for row in answers if row.get(name) is not None]
        if not values:
            return {"n": 0, "median_ms": None, "p95_ms": None}
        return {
            "n": len(values),
            "median_ms": nearest_rank(values, 50),
            "p95_ms": nearest_rank(values, 95),
        }

    return {
        "answered": [str(row["prompt_id"]) for row in answers],
        "not_answered": {
            str(row["prompt_id"]): {
                "terminal": row.get("terminal"),
                "stream_end_ms": row.get("stream_end_ms"),
            }
            for row in others
        },
        "first_text": stats("first_text_ms"),
        "stream_end": stats("stream_end_ms"),
        "rule": "nearest-rank sorted(values)[ceil(p/100*n)-1], answered turns only",
    }


def spans_verdict(readback: Mapping[str, Any], expect: Expectation) -> str:
    """Pass a trace holding its `conversation.turn` span and, for an answer, a tool span."""
    if "conversation.turn" not in cast("list[str]", readback.get("route_spans", [])):
        return FAIL
    tool_side = cast("list[str]", readback.get("tool_side_spans", []))
    if expect.kind == "answer" and not any(name.startswith("tool.") for name in tool_side):
        return FAIL
    return PASS


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--gap-seconds", type=int, default=20)
    parser.add_argument("--label-timeout-seconds", type=int, default=1_200)
    parser.add_argument("--alarm-settle-seconds", type=int, default=360)
    return parser


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0915 -- one live sequence
    """Read the expectations, run the turns, read labels, spans and alarms, write verdicts."""
    args = _parser().parse_args(argv)
    if args.environment != "dev":
        print("this leg runs only against dev")
        return 2
    out = cast("Path", args.output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    if session.client("sts").get_caller_identity()["Account"] != args.expected_account:
        print("explicit profile did not resolve to the expected account")
        return 2
    athena = ChatAthena(session, args.environment)
    month = _last_complete_month(datetime.datetime.now(datetime.UTC).date())
    expected: dict[str, object] = {
        "read_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "arp": _arp_expectations(athena, args.environment),
        "audiences": _audiences_expectations(
            athena,
            args.environment,
            month,
            current_of=_publications_reader(session, args.environment),
        ),
        "reach": _reach_expectations(athena, args.environment),
        "index": _index_expectations(athena, args.environment, month),
        "ntb": _ntb_buyer_expectations(athena, args.environment, month),
    }
    expected["queries"] = athena.queries
    (out / "expectations.json").write_text(json.dumps(expected, indent=1) + "\n")
    sequence = build_turns(expected)
    ssm = session.client("ssm")
    origin = published_parameter(ssm, args.environment, "publicBaseUrl").rstrip("/")
    tokens = sign_in(
        session.client("cognito-idp"),
        load_persona(ssm, args.environment, "probe-a"),
        app_client(session, args.environment),
    )
    csrf = secrets.token_urlsafe(24)
    window_start = datetime.datetime.now(datetime.UTC)
    start_states = {
        dimension: str(read["state"])
        for dimension, read in _alarm_reads(session, args.environment, window_start).items()
    }
    sessions: dict[str, str] = {}
    observed: list[tuple[Turn, latency.ClientTimes]] = []
    for turn in sequence:
        sessions.setdefault(turn.conversation, str(uuid.uuid4()))
        times = latency._call(  # pyright: ignore[reportPrivateUsage]
            origin=origin,
            token=tokens.access_token,
            csrf=csrf,
            question=turn.question,
            session_id=sessions[turn.conversation],
        )
        observed.append((turn, times))
        print(
            f"ran {turn.label}: status {times.status}, terminal {times.terminal}, "
            f"{times.ended_epoch_ms - times.sent_epoch_ms} ms",
            flush=True,
        )
        time.sleep(args.gap_seconds)
    rows: list[dict[str, object]] = []
    for turn, times in observed:
        record = asdict(times)
        rows.append(
            {
                "prompt_id": turn.label,
                "check": turn.check,
                "question": turn.question,
                "expectation": asdict(turn.expect),
                "outcome": turn_verdict(turn.expect, record),
                "latency_ms": times.ended_epoch_ms - times.sent_epoch_ms,
                **record,
            }
        )
    (out / "chat-turns.json").write_text(
        json.dumps({"persona": "probe-a", "turns": rows}, indent=1)
    )
    time.sleep(latency._TELEMETRY_SETTLE_SECONDS)  # pyright: ignore[reportPrivateUsage]
    logs = latency._Logs(session)  # pyright: ignore[reportPrivateUsage]
    group = latency._stack_log_groups(session, args.environment)["ConversationFunction"]  # pyright: ignore[reportPrivateUsage]
    spans: dict[str, dict[str, object]] = {}
    for turn, times in observed:
        readback = _span_readback(logs, group, times, environment=args.environment)
        spans[turn.label] = {
            **readback,
            "verdict": spans_verdict(readback, turn.expect),
            "metrics": measured_metrics_verdict(
                turn.expect, cast("list[str]", readback["metrics_read"])
            ),
        }
    (out / "spans.json").write_text(json.dumps(spans, indent=1))
    review = reviewer_client(session, args.environment)
    labels = _poll_labels(
        review,
        {turn.label: times.correlation_id for turn, times in observed},
        args.label_timeout_seconds,
    )
    label_check = labels_verdict(
        {name: cast("Mapping[str, str] | None", item["labels"]) for name, item in labels.items()}
    )
    (out / "labels.json").write_text(
        json.dumps({"turns": labels, "verdict": label_check}, indent=1)
    )
    time.sleep(args.alarm_settle_seconds)
    reads = _alarm_reads(session, args.environment, window_start)
    sent = {turn.label: times.sent_epoch_ms for turn, times in observed}
    alarms = [
        alarm_judgement(
            str(read["alarm"]),
            dimension,
            start_state=start_states[dimension],
            end_state=str(read["state"]),
            history=cast("list[Mapping[str, str]]", read["history"]),
            worst_label_sent_ms={
                turn: sent[turn]
                for turn in cast("dict[str, list[str]]", label_check["worst_label_turns"])[
                    dimension
                ]
            },
        )
        for dimension, read in reads.items()
    ]
    window_end = datetime.datetime.now(datetime.UTC)
    latencies = [cast("int", row["latency_ms"]) for row in rows]
    summary: dict[str, object] = {
        "window": [window_start.isoformat(), window_end.isoformat()],
        "turns": {
            str(row["prompt_id"]): {
                "check": row["check"],
                **cast("dict[str, object]", row["outcome"]),
                "spans": spans[str(row["prompt_id"])]["verdict"],
                "metrics": spans[str(row["prompt_id"])]["metrics"],
                "latency_ms": row["latency_ms"],
            }
            for row in rows
        },
        "labels": {
            **label_check,
            "states": {name: item["state"] for name, item in labels.items()},
        },
        "alarms": alarms,
        "p95_ms": nearest_rank_p95(latencies),
        "p95_rule": "nearest-rank sorted(values)[ceil(0.95*n)-1] over client sent->ended",
        "p95_n": len(latencies),
        "answered_latency": latency_summary(rows),
        "attribution": {name: item["attribution"] for name, item in spans.items()},
    }
    checks = [
        *(str(cast("dict[str, object]", row["outcome"])["verdict"]) for row in rows),
        *(str(item["verdict"]) for item in spans.values()),
        *(str(cast("dict[str, object]", item["metrics"])["verdict"]) for item in spans.values()),
        str(label_check["verdict"]),
        *(str(item["verdict"]) for item in alarms),
    ]
    summary["leg_verdict"] = leg_verdict(checks)
    (out / "chat-leg-verdict.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps(summary, indent=1))
    return 0 if summary["leg_verdict"] in {PASS, DEFERRED_R1} else 1


if __name__ == "__main__":
    sys.exit(main())
