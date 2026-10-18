# SPDX-License-Identifier: MIT-0
"""Break a chat turn's latency into its hops, from the browser to the model and back (dev only).

    uv run python validation/probe_turn_latency.py --profile <profile> --environment dev \
        --region us-east-1 --expected-account <account> \
        --manifest validation/fixtures/chat-bake-acceptance-v2.json \
        --outside-selection-receipt <receipt> [--gap-seconds 45] [--rounds 1]

Runs the committed six-prompt manifest's fictional turns through the deployed same-origin chat
route as the synthetic `probe-a` persona, one at a time and spaced apart so that telemetry exported
in batches belongs to exactly one turn. The sequence covers a new conversation and a follow-up in
the same conversation, with and without a governed tool call, because a new conversation starts a
new Harness session, plus the fixed out-of-list campaign control.

D8 paired chat (`--phase paired-no-bake|during-bake --output <draft>`) also writes one six-row
sample draft bound to one acceptance execution ID. A during-bake row is launched only after a
strong head read shows the target cohort `ACTIVE`. The launch instant, the target bake run, and
the telemetry-derived bake interval are known only after the bake, so `--bind-draft <draft>
--output <sample>` binds them later and writes the `ChatLatencySampleV1` artifact. A refused or
malformed bind writes nothing.

The latency diagnosis (`--turns <ChatLatencyTurnsV1 list>`, instead of `--manifest`) runs a
class-labelled turn list of 2 to 60 turns through the same per-turn run and join. Each turn's
evidence and the summary carry its class, and the summary groups first-text, after-tool-text and
stream-end percentiles by class. It takes no D8 sample flag: D8 samples stay six-prompt only.

For each turn it joins five sources by the turn's own correlation, trace, and time window:

1. the client's own arrival times: response headers, first event, first text, each tool event,
   first answer text after the last tool, and the terminal;
2. the route's `conversation.turn` `timing` line (observability §3.10);
3. the route trace's spans in `aws/spans`, and the tool, interceptor, and Athena spans in the
   turn's window (the Gateway starts a new trace, so those join by window, not by trace);
4. the Harness runtime's own spans, read from `aws/spans` by the route's trace ID and kept only
   when they carry that trace ID and the Harness's service name (token fetch, MCP handshake,
   memory reads and writes, each model call and tool call), plus its OpenTelemetry metrics and
   the session's process start-up lines from its runtime log group;
5. Lambda `REPORT` lines for the route and tool functions (duration and cold-start init).

Prints milestones, durations, counts, and token numbers only. Correlations, trace IDs, and the
released text go only to the ignored evidence directory.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import secrets
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import boto3
from accept_managed_harness import (
    _decoded_frames,  # pyright: ignore[reportPrivateUsage]
    _StageError,  # pyright: ignore[reportPrivateUsage]
)
from cognito_personas import app_client, load_persona, published_parameter, sign_in

from adapters.jobs.span_retrieval.client import SPAN_LOG_GROUP, LogsInsightsSpanClient
from adapters.jobs.span_retrieval.query import (
    SpanQuery,
    SpanQueryRefusedError,
    build_span_query,
    declared_span_attributes,
)
from adapters.outbound.cloudformation_stack import stack_resource_summaries
from adapters.outbound.https import HttpsSession, require_https
from domain.conversation.answer_stream import (
    ConversationDoneEventV4,
    ConversationRefusedEventV4,
    ConversationTextEventV4,
    ConversationToolEventV4,
)

_REGION = "us-east-1"
_ROOT = Path(__file__).resolve().parents[1]
_EVIDENCE_ROOT = _ROOT / "validation" / "evidence"
_SPAN_LOG_GROUP = SPAN_LOG_GROUP
_HARNESS_LOG_PREFIX = "/aws/bedrock-agentcore/runtimes/harness_"
_QUERY_TIMEOUT_SECONDS = 90
# Spans, EMF, and Lambda logs arrive in CloudWatch after the turn; wait before reading them.
_TELEMETRY_SETTLE_SECONDS = 75
_MAX_FILTER_PAGES = 400
_DURATION = re.compile(r"\bDuration: ([\d.]+) ms")
_INIT_DURATION = re.compile(r"Init Duration: ([\d.]+) ms")


@dataclass(frozen=True, slots=True)
class Step:
    """One turn of the fixed sequence: its label, the conversation it belongs to, its question."""

    label: str
    conversation: str
    question: str


CHAT_MANIFEST_SCHEMA = "chat-bake-acceptance-v2"
CHAT_SAMPLE_SCHEMA = "chat-latency-sample-v1"
CHAT_DRAFT_SCHEMA = "chat-latency-sample-draft-v1"
CHAT_SAMPLE_COUNT = 6
OUTSIDE_PROMPT_ID = "outside_bounded_campaign"
OUTSIDE_PLACEHOLDER = "{outside_list_item}"
OUTSIDE_SELECTION = "lexically-last-served-campaign-v3"
OUTSIDE_SELECTION_SCHEMA = "chat-outside-list-selection-v3"
OUTSIDE_SELECTION_RECEIPT = "chat-outside-list-selection-v3.json"
"""The one selection receipt an acceptance execution's directory holds; both samples bind to it."""
OUTSIDE_SELECTION_SQL = (
    "SELECT campaign_key FROM adlytics_{environment}_v2_current.fct_ad_exposure "
    "WHERE date_key BETWEEN DATE '{start}' AND DATE '{end}' AND impressions > 0 "
    "GROUP BY campaign_key"
)
"""The one governed read: every campaign with impressions in the target unit's window."""
_CAMPAIGN_KEY = re.compile(r"^campaign:[A-Za-z0-9-]+$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ATHENA_QUERY_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SELECTION_FIELDS = frozenset(
    {
        "schema_version",
        "selection",
        "acceptance_execution_id",
        "environment",
        "workgroup",
        "sql",
        "sql_sha256",
        "query_execution_id",
        "query_completed_at",
        "generation_keys",
        "browse_current_on",
        "catalog_keys",
        "catalog_truncated",
        "generation",
        "target_unit",
        "target_window",
        "target_generation",
        "read_at",
        "absence",
        "selected_item",
    }
)
ABSENCE_DEFERRED = "MOVED_TO_D2"
ABSENCE_DEFERRED_REASON = (
    "bounded-list absence is not a GRAPH criterion-8 leg: the owner moved it to D2 live "
    "acceptance on 2026-10-15 (owner-decisions.md, 'Owner approvals, 2026-10-15 ~17:15Z', item 3)"
)
_WINDOW_DAYS = re.compile(r"/window=(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})/")
_PHASES = {"paired-no-bake": "paired_no_bake", "during-bake": "during_bake"}


class SampleError(ValueError):
    """A D8 sample input or binding is invalid; no valid sample is written."""


@dataclass(frozen=True, slots=True)
class ChatManifest:
    """The committed D8 prompt manifest: six fixed prompts, one outside the bounded list.

    The outside prompt names `{outside_list_item}`; `with_outside_list_item` fills it from the
    recorded selection, so the baseline and during-bake samples ask the identical question.
    """

    path: Path
    sha256: str
    persona_id: str
    outside_list_item: str | None
    steps: tuple[Step, ...]


def load_chat_manifest(path: Path) -> ChatManifest:
    """Load the exact six-prompt manifest; a missing, unknown, or duplicate prompt is refused."""
    raw = path.read_bytes()
    try:
        document: object = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SampleError("the chat manifest is not JSON") from error
    if not isinstance(document, dict) or document.get("schema_version") != CHAT_MANIFEST_SCHEMA:
        raise SampleError("the chat manifest has the wrong schema")
    typed = cast("dict[str, object]", document)
    prompts = typed.get("prompts")
    persona = typed.get("persona_id")
    if not isinstance(prompts, list) or typed.get("outside_list_selection") != OUTSIDE_SELECTION:
        raise SampleError("the chat manifest has no prompts or outside-list selection")
    if persona != "probe-a":
        raise SampleError("the chat manifest must name the probe-a persona")
    steps: list[Step] = []
    for item in cast("list[object]", prompts):
        if not isinstance(item, dict):
            raise SampleError("a chat manifest prompt is malformed")
        prompt = cast("dict[str, object]", item)
        values = [prompt.get(name) for name in ("prompt_id", "conversation", "question")]
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise SampleError("a chat manifest prompt has a blank field")
        steps.append(Step(*cast("list[str]", values)))
    ids = [step.label for step in steps]
    if len(steps) != CHAT_SAMPLE_COUNT or len(set(ids)) != len(ids):
        raise SampleError("the chat manifest must hold six unique prompts")
    named = [step.label for step in steps if OUTSIDE_PLACEHOLDER in step.question]
    outside_steps = [step for step in steps if step.label == OUTSIDE_PROMPT_ID]
    if (
        len(outside_steps) != 1
        or named != [OUTSIDE_PROMPT_ID]
        or outside_steps[0].question.count(OUTSIDE_PLACEHOLDER) != 1
    ):
        raise SampleError("the outside-list prompt must name the outside-list item once")
    return ChatManifest(path, hashlib.sha256(raw).hexdigest(), persona, None, tuple(steps))


def with_outside_list_item(manifest: ChatManifest, item: str) -> ChatManifest:
    """Fill the outside prompt with the recorded selection; a malformed key is refused."""
    if manifest.outside_list_item is not None:
        raise SampleError("the manifest's outside-list item is already filled")
    if not isinstance(item, str) or _CAMPAIGN_KEY.fullmatch(item) is None:
        raise SampleError("the outside-list item is not a canonical campaign key")
    steps = tuple(
        Step(step.label, step.conversation, step.question.replace(OUTSIDE_PLACEHOLDER, item))
        for step in manifest.steps
    )
    return ChatManifest(manifest.path, manifest.sha256, manifest.persona_id, item, steps)


LATENCY_TURNS_SCHEMA = "chat-latency-turns/v1"
TURN_CLASSES = ("warm_up", "single_tool", "multi_tool", "clarification")
"""The closed turn classes of a latency turn list; exactly one `warm_up`, and it runs first."""
MIN_LATENCY_TURNS = 2
MAX_LATENCY_TURNS = 60
MAX_QUESTION_CHARS = 2_000
_TURN_ID = re.compile(r"^[a-z0-9-]{1,40}$")
_TURN_FIELDS = frozenset({"turn_id", "class", "conversation", "question", "follow_up_of"})


@dataclass(frozen=True, slots=True)
class LatencyTurns:
    """A class-labelled turn list for the latency diagnosis (`ChatLatencyTurnsV1`).

    `steps` run through the same per-turn run and join as the manifest's; `classes[i]` labels
    `steps[i]`. It is never a D8 sample: the six-prompt manifest stays the only sample input.
    """

    path: Path
    sha256: str
    persona_id: str
    steps: tuple[Step, ...]
    classes: tuple[str, ...]


def _latency_turn(item: object, earlier: Mapping[str, str]) -> tuple[Step, str]:
    """Validate one turn against the turns before it; return its step and class."""
    if not isinstance(item, dict) or not frozenset(cast("dict[str, object]", item)) <= _TURN_FIELDS:
        raise SampleError("a latency turn is malformed")
    turn = cast("dict[str, object]", item)
    turn_id, kind = turn.get("turn_id"), turn.get("class")
    conversation, question = turn.get("conversation"), turn.get("question")
    if not isinstance(turn_id, str) or _TURN_ID.fullmatch(turn_id) is None:
        raise SampleError("a latency turn_id is not [a-z0-9-]{1,40}")
    if turn_id in earlier:
        raise SampleError("a latency turn_id is repeated")
    if kind not in TURN_CLASSES:
        raise SampleError("a latency turn has an unknown class")
    if not isinstance(conversation, str) or not conversation.strip():
        raise SampleError("a latency turn has a blank conversation")
    if not isinstance(question, str) or not question.strip():
        raise SampleError("a latency turn has a blank question")
    if len(question) > MAX_QUESTION_CHARS:
        raise SampleError("a latency turn question is over 2,000 characters")
    if OUTSIDE_PLACEHOLDER in question:
        raise SampleError("a latency turn names the outside-list placeholder")
    follow_up = turn.get("follow_up_of")
    if follow_up is not None and (
        not isinstance(follow_up, str) or earlier.get(follow_up) != conversation
    ):
        raise SampleError("a latency follow_up_of names no earlier turn of its conversation")
    return Step(turn_id, conversation, question), cast("str", kind)


def load_latency_turns(path: Path) -> LatencyTurns:
    """Load a `ChatLatencyTurnsV1` list; any violation refuses the whole list and nothing runs."""
    raw = path.read_bytes()
    try:
        document: object = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SampleError("the latency turn list is not JSON") from error
    if not isinstance(document, dict) or document.get("schema_version") != LATENCY_TURNS_SCHEMA:
        raise SampleError("the latency turn list has the wrong schema")
    typed = cast("dict[str, object]", document)
    if typed.get("persona_id") != "probe-a":
        raise SampleError("the latency turn list must name the probe-a persona")
    turns = typed.get("turns")
    if not isinstance(turns, list):
        raise SampleError("the latency turn list has no turns")
    items = cast("list[object]", turns)
    if not MIN_LATENCY_TURNS <= len(items) <= MAX_LATENCY_TURNS:
        raise SampleError("the latency turn list must hold 2 to 60 turns")
    earlier: dict[str, str] = {}
    steps: list[Step] = []
    classes: list[str] = []
    for item in items:
        step, kind = _latency_turn(item, earlier)
        earlier[step.label] = step.conversation
        steps.append(step)
        classes.append(kind)
    if classes.count("warm_up") != 1 or classes[0] != "warm_up":
        raise SampleError("the latency turn list needs exactly one warm_up, first")
    return LatencyTurns(
        path, hashlib.sha256(raw).hexdigest(), "probe-a", tuple(steps), tuple(classes)
    )


def nearest_rank(values: Sequence[int], percent: int) -> int:
    """Return the nearest-rank percentile, `sorted(values)[ceil(percent/100*n)-1]`."""
    if not values:
        raise ValueError("a percentile needs at least one value")
    ordered = sorted(values)
    return ordered[-(-percent * len(ordered) // 100) - 1]


_CLASS_MEASURES = {
    "first_text": "first_text",
    "after_tool_text": "first_answer_text",
    "stream_end": "stream_end",
}


def class_percentiles(summaries: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Group first-text, after-tool-text and stream-end client times by turn class.

    Each measure is n, nearest-rank median and nearest-rank p95 over the turns of that class
    that observed it. The warm-up turn is discarded, as the method runs warm after it.
    """
    grouped: dict[str, object] = {}
    for kind in TURN_CLASSES[1:]:
        rows = [row for row in summaries if row.get("class") == kind]
        measures: dict[str, object] = {"turns": len(rows)}
        for measure, client_key in _CLASS_MEASURES.items():
            values = [
                value
                for row in rows
                for value in [cast("Mapping[str, object]", row["client_ms"]).get(client_key)]
                if isinstance(value, int)
            ]
            measures[measure] = (
                {
                    "n": len(values),
                    "median_ms": nearest_rank(values, 50),
                    "p95_ms": nearest_rank(values, 95),
                }
                if values
                else {"n": 0, "median_ms": None, "p95_ms": None}
            )
        grouped[kind] = measures
    return grouped


def latency_turns_summary(
    turn_list: LatencyTurns, summaries: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    """Build the `--turns` summary: the list it ran, every labelled turn, and the class groups."""
    return {
        "schema_version": "chat-latency-summary/v1",
        "turns_path": turn_list.path.name,
        "turns_sha256": turn_list.sha256,
        "rule": "nearest-rank sorted(values)[ceil(p/100*n)-1]; the warm_up turn is discarded",
        "by_class": class_percentiles(summaries),
        "turns": [dict(row) for row in summaries],
    }


def hop_split(
    times: ClientTimes,
    *,
    route_spans: Sequence[Mapping[str, object]],
    harness: Mapping[str, object],
    timing: Mapping[str, object],
) -> dict[str, object]:
    """Split send to stream end at the route span's start and the harness span's start (Q0).

    Read from existing spans only: the client's send instant and stream end, the route's
    `conversation.turn` span start and the Harness's `POST /invocations` start. Each hop crosses
    hosts' clocks, so it is inferred, not one clock. A hop whose span is missing is None and
    named in `unmeasured`, never substituted.
    """
    turn = next((span for span in route_spans if span["name"] == "conversation.turn"), None)
    route_start = cast("int", turn["start_epoch_ms"]) if turn is not None else None
    post = harness.get("post_start_epoch_ms")
    harness_start = post if isinstance(post, int) else None
    end = None if times.stream_end_ms is None else times.sent_epoch_ms + times.stream_end_ms
    unmeasured = [
        name
        for name, value in (
            ("route_span", route_start),
            ("harness_span", harness_start),
            ("stream_end", end),
        )
        if value is None
    ]

    def gap(later: int | None, earlier: int | None) -> int | None:
        return None if later is None or earlier is None else later - earlier

    return {
        "client_send_to_route_start_ms": gap(route_start, times.sent_epoch_ms),
        "route_start_to_harness_start_ms": gap(harness_start, route_start),
        "harness_start_to_stream_end_ms": gap(end, harness_start),
        "harness_total_ms": harness.get("harness_total_ms"),
        "route_cold_start": timing.get("cold_start"),
        "invoke_harness_spans": sum(
            1 for span in route_spans if span["name"] == "Bedrock AgentCore.InvokeHarness"
        ),
        "unmeasured": unmeasured,
    }


_D8_FLAGS = (
    "phase",
    "output",
    "acceptance_execution_id",
    "accepted_generation",
    "generation_unit",
    "preflight_completed_at",
    "graph_store_stack",
    "target_cohort_revision",
    "outside_selection_receipt",
    "bind_draft",
    "select_outside_list_item",
    "first_root_launch_at",
    "target_bake_run_id",
    "bake_run_started_at",
    "bake_finalize_ended_at",
)


def refuse_d8_flags_with_turns(args: argparse.Namespace) -> None:
    """D8 samples stay six-prompt only: `--turns` takes no sample, bind or selection flag."""
    named = [name for name in _D8_FLAGS if getattr(args, name, None)]
    if named:
        flags = ", ".join(f"--{name.replace('_', '-')}" for name in named)
        raise SampleError(f"--turns takes no D8 sample flag ({flags})")


def _sorted_campaign_keys(keys: object, what: str) -> list[str]:
    """Require a sorted, duplicate-free list of canonical campaign keys (types checked first)."""
    if not isinstance(keys, list):
        raise SampleError(f"the {what} is not a key list")
    typed = cast("list[object]", keys)
    if any(not isinstance(key, str) or _CAMPAIGN_KEY.fullmatch(key) is None for key in typed):
        raise SampleError(f"the {what} holds a non-campaign key")
    ordered = cast("list[str]", typed)
    if len(set(ordered)) != len(ordered):
        raise SampleError(f"the {what} repeated a key")
    if ordered != sorted(ordered):
        raise SampleError(f"the {what} is not in key order")
    return ordered


def _as_list(keys: object) -> object:
    return list(cast("tuple[object, ...]", keys)) if isinstance(keys, tuple) else keys


def select_outside_list_item(*, generation_keys: Sequence[str], catalog_keys: Sequence[str]) -> str:
    """Pick the campaign the chat asks about: served by the catalog and in the generation.

    `catalog_keys` is the complete current campaign browse through the chat tool's own
    `CanonicalEntityCatalog`; `generation_keys` are the campaigns with impressions in the
    target unit's window, read while that unit serves the accepted generation. The selection is
    the last key, in key order, of their intersection.
    """
    generation = _sorted_campaign_keys(_as_list(generation_keys), "generation read")
    catalog = _sorted_campaign_keys(_as_list(catalog_keys), "complete catalog browse")
    served = [key for key in catalog if key in set(generation)]
    if not served:
        raise SampleError("no catalog campaign has impressions in the accepted generation")
    return served[-1]


def outside_list_absence() -> dict[str, str]:
    """Return the bounded-list absence record: not measured here, owned by D2 acceptance.

    The campaign-only serving browse requests `MAX_CAMPAIGN_PORTFOLIO_CANDIDATES` (1,000) and
    dev serves far fewer campaigns, so no real dev campaign lies outside it. The owner moved
    the leg to D2 live acceptance; every GRAPH sample records it as not measured (`None`).
    """
    return {"status": ABSENCE_DEFERRED, "reason": ABSENCE_DEFERRED_REASON}


def chat_workgroup(environment: str) -> str:
    """Return the CHAT (interactive) Athena workgroup the governed selection must run on."""
    return f"adlytics-{environment}-v2-interactive"


def window_days(window: str) -> tuple[str, str]:
    """Return the first and last ISO day of a manifest window, or refuse it."""
    match = _WINDOW_DAYS.search(window)
    if match is None:
        raise SampleError("the target window names no window=<start>_<end> days")
    start, end = match.group(1), match.group(2)
    try:
        first, last = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
    except ValueError as error:
        raise SampleError("the target window days are not ISO dates") from error
    if last < first:
        raise SampleError("the target window is reversed")
    return start, end


def window_unit(window: str) -> str:
    """Return the runtime's conflict unit of a window: `conflict-unit/v1` over its exact text."""
    from domain.operations.status import canonical_authority_digest

    return canonical_authority_digest(["conflict-unit/v1", window])


def selection_sql(environment: str, window: str) -> tuple[str, str]:
    """Return the selection's exact SQL for the target window, and its SHA-256."""
    start, end = window_days(window)
    sql = OUTSIDE_SELECTION_SQL.format(environment=environment, start=start, end=end)
    return sql, hashlib.sha256(sql.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SelectionObservation:
    """Everything one governed selection observed, before it becomes a receipt."""

    query_execution_id: str
    query_completed_at: str
    generation_keys: tuple[str, ...]
    browse_current_on: str
    catalog_keys: tuple[str, ...]
    catalog_truncated: bool
    target_unit: str
    target_window: str
    target_generation: str
    read_at: str


def selection_receipt(
    *, execution_id: str, environment: str, generation: str, observed: SelectionObservation
) -> dict[str, object]:
    """Build the one execution-scoped selection receipt, refusing anything the rule refuses."""
    sql, digest = selection_sql(environment, observed.target_window)
    receipt: dict[str, object] = {
        "schema_version": OUTSIDE_SELECTION_SCHEMA,
        "selection": OUTSIDE_SELECTION,
        "acceptance_execution_id": execution_id,
        "environment": environment,
        "workgroup": chat_workgroup(environment),
        "sql": sql,
        "sql_sha256": digest,
        "query_execution_id": observed.query_execution_id,
        "query_completed_at": observed.query_completed_at,
        "generation_keys": sorted(observed.generation_keys),
        "browse_current_on": observed.browse_current_on,
        "catalog_keys": list(observed.catalog_keys),
        "catalog_truncated": observed.catalog_truncated,
        "generation": generation,
        "target_unit": observed.target_unit,
        "target_window": observed.target_window,
        "target_generation": observed.target_generation,
        "read_at": observed.read_at,
        "absence": outside_list_absence(),
        "selected_item": "",
    }
    receipt["selected_item"] = select_outside_list_item(
        generation_keys=cast("list[str]", receipt["generation_keys"]),
        catalog_keys=observed.catalog_keys,
    )
    validate_outside_selection(
        receipt, execution_id=execution_id, environment=environment, generation=generation
    )
    return receipt


def _text(receipt: Mapping[str, object], name: str) -> str:
    value = receipt[name]
    if not isinstance(value, str):
        raise SampleError(f"the selection receipt's {name} is not text")
    return value


def _selection_checks(
    receipt: Mapping[str, object],
    *,
    execution_id: str | None,
    environment: str,
    generation: str | None,
) -> dict[str, bool]:
    window = _text(receipt, "target_window")
    sql, digest = selection_sql(environment, window)
    accepted = _text(receipt, "generation")
    return {
        "schema": receipt["schema_version"] == OUTSIDE_SELECTION_SCHEMA,
        "rule": receipt["selection"] == OUTSIDE_SELECTION,
        "environment": receipt["environment"] == environment,
        "workgroup": receipt["workgroup"] == chat_workgroup(environment),
        "sql": receipt["sql"] == sql and receipt["sql_sha256"] == digest,
        "query": _ATHENA_QUERY_ID.fullmatch(_text(receipt, "query_execution_id")) is not None,
        "execution": bool(_text(receipt, "acceptance_execution_id"))
        and execution_id in (None, receipt["acceptance_execution_id"]),
        "generation": _DIGEST.fullmatch(accepted) is not None and generation in (None, accepted),
        "target_unit": _text(receipt, "target_unit") == window_unit(window),
        "target_generation": receipt["target_generation"] == accepted,
        "catalog": receipt["catalog_truncated"] is False,
        "absence": receipt["absence"] == outside_list_absence(),
    }


def validate_outside_selection(
    document: object,
    *,
    execution_id: str | None,
    environment: str,
    generation: str | None,
) -> str:
    """Re-validate every receipt field independently and return the item it proves.

    The workgroup, the SQL and the target unit are recomputed (the unit from the recorded window
    with the runtime's own digest), the selected key from the recorded key lists, and the target
    unit's selection-time generation must equal the accepted one. The absence leg must be the
    exact deferred record. Any failure, including a field of the wrong type, refuses with a
    `SampleError`: no item comes from a receipt that fails a check.
    """
    if not isinstance(document, dict):
        raise SampleError("the selection receipt is not an object")
    receipt = cast("dict[str, object]", document)
    if frozenset(receipt) != _SELECTION_FIELDS:
        raise SampleError("the selection receipt has missing or unexpected fields")
    try:
        checks = _selection_checks(
            receipt, execution_id=execution_id, environment=environment, generation=generation
        )
    except TypeError as error:
        raise SampleError("the selection receipt holds a field of the wrong type") from error
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise SampleError(f"the selection receipt fails {', '.join(failed)}")
    for name in ("query_completed_at", "read_at"):
        _utc(receipt[name], f"selection {name}")
    try:
        datetime.date.fromisoformat(_text(receipt, "browse_current_on"))
    except ValueError as error:
        raise SampleError("the selection browse day is not an ISO date") from error
    selected = select_outside_list_item(
        generation_keys=cast("list[str]", receipt["generation_keys"]),
        catalog_keys=cast("list[str]", receipt["catalog_keys"]),
    )
    if receipt["selected_item"] != selected:
        raise SampleError("the selection receipt's selected_item is not the rule's result")
    return selected


def load_outside_selection(
    path: Path, *, execution_id: str | None, environment: str, generation: str | None
) -> dict[str, Any]:
    """Read and validate one retained receipt; return its item, unit, file digest and query ID."""
    raw = path.read_bytes()
    try:
        document: object = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SampleError("the selection receipt is not JSON") from error
    item = validate_outside_selection(
        document, execution_id=execution_id, environment=environment, generation=generation
    )
    receipt = cast("dict[str, object]", document)
    return {
        "outside_list_item": item,
        "outside_list_present": item in cast("list[str]", receipt["generation_keys"]),
        "outside_selection_sha256": hashlib.sha256(raw).hexdigest(),
        "outside_selection_query_execution_id": str(receipt["query_execution_id"]),
        "target_unit": str(receipt["target_unit"]),
        "target_generation": str(receipt["target_generation"]),
    }


def athena_query_readback(athena: Any, query_execution_id: str) -> dict[str, object]:
    """Read one Athena query back, read-only: its state, workgroup, SQL digest, end and keys."""
    status = athena.get_query_execution(QueryExecutionId=query_execution_id)["QueryExecution"]
    state = str(status["Status"]["State"])
    completed = status["Status"].get("CompletionDateTime")
    keys: list[str] = []
    if state == "SUCCEEDED":
        first = True
        paginator = athena.get_paginator("get_query_results")
        for page in paginator.paginate(QueryExecutionId=query_execution_id):
            rows = page["ResultSet"]["Rows"]
            for row in rows[1:] if first else rows:
                keys.append(str(row["Data"][0].get("VarCharValue", "")))
            first = False
    return {
        "query_execution_id": str(status.get("QueryExecutionId", query_execution_id)),
        "state": state,
        "workgroup": str(status.get("WorkGroup", "")),
        "query_sha256": hashlib.sha256(str(status.get("Query", "")).encode()).hexdigest(),
        "completed_at": None
        if completed is None
        else completed.astimezone(datetime.UTC).isoformat(),
        "keys": keys,
    }


def selection_readback_reasons(
    receipt: Mapping[str, object],
    readback: Mapping[str, object],
    *,
    preflight_completed_at: object,
    baseline_started_at: object,
) -> list[str]:
    """Why a re-read of the receipt's Athena query does not prove it, or nothing.

    The query must have SUCCEEDED on the receipt's workgroup with the receipt's exact SQL, its
    results must recompute the receipt's generation keys, and it must have completed inside
    this execution: after its preflight and before its baseline sample started.
    """
    reasons: list[str] = []
    if readback.get("query_execution_id") != receipt.get("query_execution_id"):
        reasons.append("readback_other_query")
    if readback.get("state") != "SUCCEEDED":
        reasons.append("readback_not_succeeded")
    if readback.get("workgroup") != receipt.get("workgroup"):
        reasons.append("readback_workgroup_mismatch")
    if readback.get("query_sha256") != receipt.get("sql_sha256"):
        reasons.append("readback_sql_mismatch")
    keys = readback.get("keys")
    if not isinstance(keys, list) or sorted(map(str, cast("list[object]", keys))) != receipt.get(
        "generation_keys"
    ):
        reasons.append("readback_keys_mismatch")
    try:
        completed = _utc(readback.get("completed_at"), "readback completion")
        lower = _utc(preflight_completed_at, "preflight")
        upper = _utc(baseline_started_at, "baseline start")
    except SampleError:
        reasons.append("readback_time_unprovable")
    else:
        if not lower <= completed <= upper:
            reasons.append("readback_outside_execution")
        if receipt.get("query_completed_at") != readback.get("completed_at"):
            reasons.append("readback_completion_mismatch")
    return reasons


def filled_prompt_sha256(manifest: ChatManifest) -> str:
    """Digest the six literal prompts a filled manifest asks, in order."""
    if manifest.outside_list_item is None:
        raise SampleError("the manifest's outside-list item was never filled")
    steps = [[step.label, step.conversation, step.question] for step in manifest.steps]
    return hashlib.sha256(json.dumps(steps, separators=(",", ":")).encode()).hexdigest()


@dataclass(slots=True)
class ClientTimes:
    """What the client saw, in ms since it sent the request, and the turn's wall-clock window."""

    status: int = 0
    correlation_id: str = ""
    sent_epoch_ms: int = 0
    ended_epoch_ms: int = 0
    headers_ms: int | None = None
    first_event_ms: int | None = None
    first_text_ms: int | None = None
    first_answer_text_ms: int | None = None
    terminal_ms: int | None = None
    stream_end_ms: int | None = None
    terminal: str | None = None
    audit_id: str | None = None
    tool_events: list[dict[str, object]] = field(default_factory=lambda: [])
    text_deltas: int = 0
    released_text: str = ""
    refusal_reason: str = ""
    stream_error: str | None = None
    """Why the body was not a complete SSE stream (an error page), or None; never its text."""
    content_type: str = ""
    body_bytes: int = 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--environment", default="dev")
    parser.add_argument("--region", default=_REGION)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--gap-seconds", type=int, default=45)
    parser.add_argument("--rounds", type=int, default=1, choices=range(1, 6), metavar="1-5")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", type=Path)
    source.add_argument(
        "--turns",
        type=Path,
        help="A class-labelled ChatLatencyTurnsV1 list (latency diagnosis; no D8 sample flags).",
    )
    parser.add_argument("--phase", choices=sorted(_PHASES))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--acceptance-execution-id")
    parser.add_argument("--accepted-generation")
    parser.add_argument("--generation-unit")
    parser.add_argument("--preflight-completed-at")
    parser.add_argument("--graph-store-stack")
    parser.add_argument("--target-cohort-revision")
    parser.add_argument(
        "--outside-selection-receipt",
        type=Path,
        help="The execution's selection receipt (`--select-outside-list-item --output`).",
    )
    parser.add_argument("--bind-draft", type=Path)
    parser.add_argument(
        "--select-outside-list-item",
        action="store_true",
        help="Read-only: run the governed campaign read on the CHAT workgroup and write the "
        "execution's selection receipt to --output.",
    )
    parser.add_argument("--first-root-launch-at")
    parser.add_argument("--target-bake-run-id")
    parser.add_argument("--bake-run-started-at")
    parser.add_argument("--bake-finalize-ended-at")
    parser.add_argument(
        "--telemetry-dir",
        type=Path,
        help="Where the per-turn telemetry directory is created (default validation/evidence).",
    )
    return parser


def _ms(seconds: float | None) -> int | None:
    return None if seconds is None else round(seconds * 1000)


def _call(
    *,
    origin: str,
    token: str,
    csrf: str,
    question: str,
    session_id: str,
    record_stream_errors: bool = False,
) -> ClientTimes:
    """Send one turn and stamp every arrival against the moment the request left.

    With `record_stream_errors` (the `--turns` diagnosis) a body that isn't a complete SSE
    stream is recorded on the turn and the run continues; a D8 sample still refuses it.
    """
    payload = json.dumps(
        {
            "schema_version": "ConversationRequest.v3",
            "question": question,
            "session_id": session_id,
        },
        separators=(",", ":"),
    ).encode()
    times = ClientTimes(sent_epoch_ms=int(time.time() * 1000))
    started = time.monotonic()
    chunks: list[tuple[float, bytes]] = []
    with HttpsSession().request(
        "POST",
        require_https(f"{origin}/api/chat"),
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-amz-content-sha256": hashlib.sha256(payload).hexdigest(),
            "Cookie": f"adl_session={token}; adl_csrf={csrf}",
            "x-adl-csrf": csrf,
            "Sec-Fetch-Site": "same-origin",
        },
        timeout=240,
        stream=True,
    ) as response:
        times.headers_ms = _ms(time.monotonic() - started)
        times.correlation_id = response.headers.get("x-correlation-id", "")
        times.status = response.status_code
        while True:
            chunk = response.raw.read1(8192)
            if not chunk:
                break
            chunks.append((time.monotonic() - started, chunk))
        # The body's end, which the browser waits for after `done` (frontend `sendChat`).
        times.stream_end_ms = _ms(time.monotonic() - started)
    times.ended_epoch_ms = int(time.time() * 1000)
    times.content_type = response.headers.get("content-type", "")[:80]
    times.body_bytes = sum(len(chunk) for _, chunk in chunks)
    try:
        events, _ = _decoded_frames(chunks)
    except _StageError as error:
        # A body that isn't a complete SSE stream (a gateway or error page) is this turn's
        # recorded failure; the run continues, so no other turn's telemetry is lost.
        if not record_stream_errors:
            raise
        times.stream_error = error.reason
        events = []
    texts = [at for at, event in events if isinstance(event, ConversationTextEventV4)]
    tools = [(at, event) for at, event in events if isinstance(event, ConversationToolEventV4)]
    last_tool = max((at for at, _ in tools), default=None)
    answer = [at for at in texts if last_tool is None or at > last_tool]
    times.first_event_ms = _ms(events[0][0]) if events else None
    times.first_text_ms = _ms(texts[0]) if texts else None
    times.first_answer_text_ms = _ms(answer[0]) if answer else None
    times.text_deltas = len(texts)
    times.released_text = "".join(
        event.text for _, event in events if isinstance(event, ConversationTextEventV4)
    )
    times.tool_events = [
        {"tool": event.tool_name, "status": event.status.value, "at_ms": _ms(at)}
        for at, event in tools
    ]
    for at, event in events:
        if isinstance(event, ConversationDoneEventV4 | ConversationRefusedEventV4):
            times.terminal_ms = _ms(at)
            times.terminal = event.kind
            times.audit_id = event.audit_id
        if isinstance(event, ConversationRefusedEventV4):
            times.refusal_reason = f"{event.reason_code.value} {event.reason}"
    return times


class _Logs:
    """Bounded CloudWatch Logs Insights reads."""

    def __init__(self, session: boto3.Session) -> None:
        self._logs: Any = session.client("logs")

    def spans(self, query: SpanQuery) -> list[dict[str, object]]:
        """Read one lookup from `aws/spans` through the 5a span-retrieval module."""
        result = LogsInsightsSpanClient(
            self._logs, poll_seconds=2.0, timeout_seconds=_QUERY_TIMEOUT_SECONDS
        ).retrieve(query)
        if result["reason"] == "query_running":
            raise TimeoutError("Logs Insights query did not complete")
        return cast("list[dict[str, object]]", result["spans"])

    def events(
        self,
        group: str,
        *,
        start_ms: int,
        end_ms: int,
        pattern: str = "",
        streams: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        request: dict[str, object] = {
            "logGroupName": group,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 1000,
        }
        if pattern:
            request["filterPattern"] = pattern
        if streams:
            request["logStreamNames"] = list(streams)
        found: list[dict[str, Any]] = []
        # A filtered read pages through every stream in the window, and most pages are empty.
        for _ in range(_MAX_FILTER_PAGES):
            page = self._logs.filter_log_events(**request)
            found += page.get("events", [])
            token = page.get("nextToken")
            if not token:
                break
            request["nextToken"] = token
        return found

    def newest_group(self, prefix: str) -> str:
        pages = self._logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix)
        groups = [group for page in pages for group in page["logGroups"]]
        best, best_at = "", -1
        for group in groups:
            # aws-pagination: limit=1 by LastEventTime descending asks for the newest stream only.
            streams = self._logs.describe_log_streams(
                logGroupName=group["logGroupName"],
                orderBy="LastEventTime",
                descending=True,
                limit=1,
            )["logStreams"]
            at = streams[0].get("lastEventTimestamp", 0) if streams else 0
            if at > best_at:
                best, best_at = group["logGroupName"], at
        if not best:
            raise RuntimeError(f"no log group under {prefix}")
        return best


def _stack_log_groups(session: boto3.Session, environment: str) -> dict[str, str]:
    """Read the route, tool, and token-service log groups from the deployed stacks themselves."""
    cloudformation: Any = session.client("cloudformation")
    lambda_client: Any = session.client("lambda")
    wanted = {
        f"Adlytics-Api-{environment}": ("ConversationFunction",),
        f"Adlytics-Agent-{environment}": ("MeasurementTool", "CatalogTool", "GatewayInterceptor"),
        f"Adlytics-TokenService-{environment}": ("TokenRouteExchange", "TokenIssuer"),
    }
    groups: dict[str, str] = {}
    for stack, prefixes in wanted.items():
        for resource in stack_resource_summaries(cloudformation, stack):
            if resource["ResourceType"] != "AWS::Lambda::Function":
                continue
            prefix = next(
                (item for item in prefixes if resource["LogicalResourceId"].startswith(item)), None
            )
            if prefix is None or prefix in groups:
                continue
            configuration = lambda_client.get_function_configuration(
                FunctionName=resource["PhysicalResourceId"]
            )
            groups[prefix] = configuration.get("LoggingConfig", {}).get(
                "LogGroup", f"/aws/lambda/{resource['PhysicalResourceId']}"
            )
    return groups


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _span_query(times: ClientTimes, environment: str, **selector: Any) -> SpanQuery | None:
    """One turn's lookup: its window padded 5 s each side, one page of at most 200 spans."""
    try:
        return build_span_query(
            environment=environment,
            window_start=datetime.datetime.fromtimestamp(
                times.sent_epoch_ms // 1000 - 5, datetime.UTC
            ),
            window_end=datetime.datetime.fromtimestamp(
                times.ended_epoch_ms // 1000 + 6, datetime.UTC
            ),
            declared_attributes=declared_span_attributes(),
            limit=200,
            max_pages=1,
            **selector,
        )
    except SpanQueryRefusedError:
        # A timing line without a W3C trace ID names no trace to read.
        return None


def _timing_line(logs: _Logs, group: str, times: ClientTimes) -> dict[str, object]:
    # A filtered read by the turn's own correlation, so the line is found without a query index.
    events = logs.events(
        group,
        start_ms=times.sent_epoch_ms - 1_000,
        end_ms=times.ended_epoch_ms + 5_000,
        pattern=f'{{ $.phase = "timing" && $.correlation_id = "{times.correlation_id}" }}',
    )
    if not events:
        return {}
    return cast("dict[str, object]", json.loads(events[0]["message"]))


def _route_spans(
    logs: _Logs, trace_id: str, times: ClientTimes, *, environment: str
) -> list[dict[str, object]]:
    query = _span_query(times, environment, trace_id=trace_id)
    return [] if query is None else _spans(logs.spans(query))


def _window_spans(
    logs: _Logs, times: ClientTimes, names: Sequence[str], *, environment: str
) -> list[dict[str, object]]:
    query = _span_query(times, environment, span_name_prefixes=names)
    return [
        span
        for span in ([] if query is None else _spans(logs.spans(query)))
        if times.sent_epoch_ms <= cast("int", span["start_epoch_ms"]) <= times.ended_epoch_ms
    ]


def _spans(documents: list[dict[str, object]]) -> list[dict[str, object]]:
    spans: list[dict[str, object]] = []
    for document in documents:
        start = _number(document.get("startTimeUnixNano"))
        duration = _number(document.get("durationNano"))
        if start is None or duration is None:
            continue
        spans.append(
            {
                "name": str(document.get("name", "")),
                "start_epoch_ms": round(start / 1e6),
                "duration_ms": round(duration / 1e6),
                "trace": str(document.get("traceId", "")),
            }
        )
    return sorted(spans, key=lambda span: cast("int", span["start_epoch_ms"]))


def _harness_metrics(logs: _Logs, group: str, times: ClientTimes) -> dict[str, object]:
    """Sum the Harness runtime's own EMF documents exported in this turn's window (+30 s)."""
    documents = logs.events(
        group,
        start_ms=times.sent_epoch_ms,
        end_ms=times.ended_epoch_ms + 30_000,
        pattern='"CloudWatchMetrics"',
    )
    model_calls: list[float] = []
    tokens: dict[str, float] = {}
    cycles: list[float] = []
    tool_seconds: list[dict[str, object]] = []
    for event in documents:
        try:
            document = json.loads(event["message"])
        except ValueError:
            continue
        duration = document.get("gen_ai.client.operation.duration")
        if isinstance(duration, Mapping):
            model_calls += [float(value) for value in duration.get("Values", [])]  # type: ignore[union-attr]
        usage = document.get("gen_ai.client.token.usage")
        if isinstance(usage, Mapping):
            kind = str(document.get("gen_ai.token.type", "unknown"))
            tokens[kind] = tokens.get(kind, 0.0) + float(usage.get("Sum", 0.0))  # type: ignore[union-attr]
        cycle = document.get("strands.event_loop.cycle_duration")
        if isinstance(cycle, Mapping):
            cycles += [float(value) for value in cycle.get("Values", [])]  # type: ignore[union-attr]
        tool = document.get("strands.tool.duration")
        if isinstance(tool, Mapping):
            tool_seconds.append(
                {
                    "tool": str(document.get("tool_name", ""))[:80],
                    "seconds": float(tool.get("Sum", 0.0)),  # type: ignore[union-attr]
                }
            )
    return {
        "model_call_seconds": [round(value, 3) for value in model_calls],
        "token_sums": {key: round(value) for key, value in tokens.items()},
        "agent_cycle_seconds": [round(value, 3) for value in cycles],
        "tool_seconds": tool_seconds,
    }


_MODEL_CALL = "chat "
_SERIAL_MEMORY = ("Bedrock AgentCore.ListEvents", "Bedrock AgentCore.CreateEvent")
# Spans that contain the others; the time no other span covers is measured without them.
_CONTAINERS = ("POST /invocations", "invoke_agent", "execute_event_loop_cycle", "chat")
_CACHE_TOKENS = (
    "gen_ai.usage.cache_read_input_tokens",
    "gen_ai.usage.cache_write_input_tokens",
)


def _uncovered_ms(spans: Sequence[Mapping[str, Any]], *, start_ms: float, end_ms: float) -> int:
    """Milliseconds of [start, end) that no non-container span covers."""
    intervals = sorted(
        (
            max(start_ms, int(span["startTimeUnixNano"]) / 1e6),
            min(end_ms, (int(span["startTimeUnixNano"]) + int(span["durationNano"])) / 1e6),
        )
        for span in spans
        if not str(span.get("name", "")).startswith(_CONTAINERS)
    )
    covered = 0.0
    current: list[float] | None = None
    for low, high in intervals:
        if high <= low:
            continue
        if current is None or low > current[1]:
            if current is not None:
                covered += current[1] - current[0]
            current = [low, high]
        else:
            current[1] = max(current[1], high)
    if current is not None:
        covered += current[1] - current[0]
    return round((end_ms - start_ms) - covered)


def harness_service_prefix(environment: str) -> str:
    """Return the `service.name` prefix of the Harness runtime's spans in `environment`."""
    return f"harness_adlytics_{environment}_agent."


def _is_harness_span(span: Mapping[str, Any], *, trace_id: str, service_prefix: str) -> bool:
    resource = span.get("resource")
    attributes: object = (
        cast("Mapping[str, object]", resource).get("attributes")
        if isinstance(resource, Mapping)
        else None
    )
    service: object = (
        cast("Mapping[str, object]", attributes).get("service.name")
        if isinstance(attributes, Mapping)
        else None
    )
    return (
        span.get("traceId") == trace_id
        and isinstance(service, str)
        and service.startswith(service_prefix)
    )


def _harness_trace(
    logs: _Logs, *, trace_id: str, service_prefix: str, times: ClientTimes
) -> list[dict[str, Any]]:
    """Read the Harness runtime's own spans of this turn's trace from `aws/spans`.

    The runtime's execution role cannot open its own log group to X-Ray (ADR-009), so its spans
    reach the account's Transaction Search group, `aws/spans`, which every traced service shares.
    They carry the trace the route propagated in `traceParent`. A span is kept only when it
    carries exactly this turn's trace ID and the Harness's service name, so another turn's or
    another service's spans never enter the breakdown.
    """
    if not trace_id:
        return []
    found = logs.events(
        _SPAN_LOG_GROUP,
        start_ms=times.sent_epoch_ms - 1_000,
        end_ms=times.ended_epoch_ms + 30_000,
        pattern=f'{{ $.traceId = "{trace_id}" }}',
    )
    spans: list[dict[str, Any]] = []
    for event in found:
        try:
            span = cast("dict[str, Any]", json.loads(event["message"]))
        except ValueError:
            continue
        if _is_harness_span(span, trace_id=trace_id, service_prefix=service_prefix):
            spans.append(span)
    return spans


def harness_segments(spans: Sequence[Mapping[str, Any]]) -> dict[str, object]:
    """Split the Harness's work before and between model calls into measured segments (ms).

    Only span names and times are read. A segment whose spans are absent is None.
    """

    def start(span: Mapping[str, Any]) -> float:
        return int(span["startTimeUnixNano"]) / 1e6

    def end(span: Mapping[str, Any]) -> float:
        return start(span) + int(span["durationNano"]) / 1e6

    ordered = sorted(spans, key=start)
    posts = [span for span in ordered if span.get("name") == "POST /invocations"]
    models = [span for span in ordered if str(span.get("name", "")).startswith(_MODEL_CALL)]
    if not posts or not models:
        return {}
    post, first_model = posts[0], models[0]
    before = [span for span in ordered if start(span) < start(first_model)]

    def named(prefix: str) -> list[Mapping[str, Any]]:
        return [span for span in before if str(span.get("name", "")).startswith(prefix)]

    def span_ms(items: Sequence[Mapping[str, Any]]) -> int | None:
        return round(sum(end(item) - start(item) for item in items)) if items else None

    token = named("Bedrock AgentCore.GetResourceOauth2Token")
    tools_list = named("mcp tools/list")
    retrieve = named("Bedrock AgentCore.RetrieveMemoryRecords")
    serial = [span for span in before if span.get("name") in _SERIAL_MEMORY]
    tool_calls = [
        span for span in ordered if str(span.get("name", "")).startswith("mcp tools/call")
    ]
    after_tool = [
        round(start(next_model) - end(call))
        for call in tool_calls
        for next_model in [next((m for m in models if start(m) >= end(call)), None)]
        if next_model is not None
    ]
    agent = [span for span in ordered if str(span.get("name", "")).startswith("invoke_agent")]
    cache = {
        key.removeprefix("gen_ai.usage."): int(agent[0]["attributes"][key])
        for key in _CACHE_TOKENS
        if agent and key in agent[0].get("attributes", {})
    }
    first_input = first_model.get("attributes", {}).get("gen_ai.usage.input_tokens")
    return {
        "post_to_first_model_call_ms": round(start(first_model) - start(post)),
        "post_to_token_request_ms": round(start(token[0]) - start(post)) if token else None,
        "uncovered_before_first_model_call_ms": _uncovered_ms(
            before, start_ms=start(post), end_ms=start(first_model)
        ),
        "first_model_call_input_tokens": first_input if isinstance(first_input, int) else None,
        "cache_tokens": cache,
        "model_call_starts_epoch_ms": [round(start(item)) for item in models],
        "gateway_token_ms": span_ms(token[:1]),
        "tools_list_ms": span_ms(tools_list[:1]),
        "token_end_to_tools_list_ms": (
            round(start(tools_list[0]) - end(token[0])) if token and tools_list else None
        ),
        "short_term_memory_serial_ms": span_ms(serial),
        "long_term_memory_parallel_ms": (
            round(max(end(item) for item in retrieve) - min(start(item) for item in retrieve))
            if retrieve
            else None
        ),
        "model_calls_ms": [round(end(item) - start(item)) for item in models],
        "tool_calls_ms": [round(end(item) - start(item)) for item in tool_calls],
        "tool_result_to_next_model_call_ms": after_tool,
        "harness_total_ms": round(end(post) - start(post)),
        "post_start_epoch_ms": round(start(post)),
        "first_model_call_start_epoch_ms": round(start(first_model)),
    }


def cross_clock(
    segments: Mapping[str, object],
    *,
    route_spans: Sequence[Mapping[str, object]],
    timing: Mapping[str, object],
) -> dict[str, int | None]:
    """Join route and Harness times across the two hosts' clocks (inferred, not one clock).

    The route's timing milestones count from its handler receiving the request, which starts the
    `conversation.turn` span within a millisecond, so that span's start anchors them.
    """
    turn = next((span for span in route_spans if span["name"] == "conversation.turn"), None)
    invoke = next(
        (span for span in route_spans if span["name"] == "Bedrock AgentCore.InvokeHarness"), None
    )
    starts = cast("list[int]", segments.get("model_call_starts_epoch_ms") or [])
    post = segments.get("post_start_epoch_ms")
    if turn is None or not isinstance(post, int):
        return {}
    anchor = cast("int", turn["start_epoch_ms"])

    def route_at(name: str) -> int | None:
        value = timing.get(f"ms_{name}")
        return anchor + value if isinstance(value, int) else None

    first_text = route_at("first_model_text")
    answer = route_at("first_answer_text")
    return {
        "invoke_harness_to_post_ms": (
            post - cast("int", invoke["start_epoch_ms"]) if invoke is not None else None
        ),
        "model_time_to_first_token_ms": (
            first_text - starts[0] if first_text is not None and starts else None
        ),
        "model_time_to_answer_token_ms": (
            answer - starts[1] if answer is not None and len(starts) > 1 else None
        ),
    }


def _harness_session_start(logs: _Logs, group: str, times: ClientTimes) -> dict[str, object]:
    """Process start-up lines of a Harness session microVM that began during this turn."""
    lines = logs.events(group, start_ms=times.sent_epoch_ms - 2_000, end_ms=times.ended_epoch_ms)
    starts: dict[str, dict[str, int]] = {}
    for event in lines:
        stream = str(event.get("logStreamName", ""))
        if "[runtime-logs]" not in stream:
            continue
        message = str(event.get("message", ""))
        entry = starts.setdefault(stream, {})
        at = int(event["timestamp"]) - times.sent_epoch_ms
        entry.setdefault("first_line_ms", at)
        if "Application startup complete" in message:
            entry.setdefault("server_ready_ms", at)
    return {"sessions_started": len(starts), "starts": list(starts.values())}


def _lambda_reports(
    logs: _Logs, groups: Mapping[str, str], times: ClientTimes
) -> dict[str, object]:
    reports: dict[str, object] = {}
    for name, group in groups.items():
        found = logs.events(
            group,
            start_ms=times.sent_epoch_ms,
            end_ms=times.ended_epoch_ms + 5_000,
            pattern='"REPORT RequestId"',
        )
        rows: list[dict[str, object]] = []
        for event in found:
            message = str(event.get("message", ""))
            duration = _DURATION.search(message)
            if duration is None:
                continue
            init = _INIT_DURATION.search(message)
            rows.append(
                {
                    "at_ms": int(event["timestamp"]) - times.sent_epoch_ms,
                    "duration_ms": round(float(duration.group(1))),
                    "init_ms": None if init is None else round(float(init.group(1))),
                }
            )
        reports[name] = rows
    return reports


def _breakdown(
    times: ClientTimes, timing: Mapping[str, object], route_spans: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    """Derive hop durations; each is a difference of two measured points, never a guess."""

    def at(name: str) -> int | None:
        value = timing.get(f"ms_{name}")
        return value if isinstance(value, int) else None

    def gap(later: int | None, earlier: int | None) -> int | None:
        return None if later is None or earlier is None else later - earlier

    def span_ms(prefix: str) -> int | None:
        total = [
            cast("int", span["duration_ms"])
            for span in route_spans
            if str(span["name"]).startswith(prefix)
        ]
        return sum(total) if total else None

    tools = cast("list[Mapping[str, int]]", timing.get("tools", []))
    return {
        "client_headers_minus_route_response_started_ms": gap(
            times.headers_ms, at("response_started")
        ),
        "route_admit_ms": at("admitted"),
        "input_guardrail_ms": gap(at("input_assessed"), at("admitted")),
        "input_guardrail_span_ms": span_ms("Bedrock Runtime.ApplyGuardrail"),
        "harness_token_exchange_ms": gap(at("harness_token_exchanged"), at("input_assessed")),
        "invoke_harness_call_ms": gap(at("harness_stream_opened"), at("harness_invoked")),
        "stream_open_to_first_event_ms": gap(
            at("first_harness_event"), at("harness_stream_opened")
        ),
        "invoke_to_first_model_text_ms": gap(at("first_model_text"), at("harness_invoked")),
        "tool_round_trips_ms": [
            gap(item.get("result_ms"), item.get("requested_ms")) for item in tools
        ],
        "tool_input_write_ms": [
            gap(item.get("requested_ms"), item.get("input_started_ms")) for item in tools
        ],
        "last_tool_result_to_answer_text_ms": gap(at("first_answer_text"), at("last_tool_result")),
        "answer_text_to_end_turn_ms": gap(
            at("end_turn"), at("first_answer_text") or at("first_model_text")
        ),
        "end_turn_to_receipt_ms": gap(at("receipt_written"), at("end_turn")),
        "receipt_to_stream_closed_ms": gap(at("stream_closed"), at("receipt_written")),
        "client_terminal_minus_route_stream_closed_ms": gap(times.terminal_ms, at("stream_closed")),
        "client_done_to_stream_end_ms": gap(times.stream_end_ms, times.terminal_ms),
    }


def _utc(value: object, what: str) -> datetime.datetime:
    if not isinstance(value, str) or not value:
        raise SampleError(f"{what} is not an ISO instant")
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError as error:
        raise SampleError(f"{what} is not an ISO instant") from error
    if parsed.tzinfo is None or parsed.utcoffset() != datetime.timedelta(0):
        raise SampleError(f"{what} is not a timezone-aware UTC instant")
    return parsed


def _epoch_instant(epoch_ms: int) -> str:
    return datetime.datetime.fromtimestamp(epoch_ms / 1000, tz=datetime.UTC).isoformat()


def chat_sample_row(
    step: Step,
    times: ClientTimes,
    *,
    governed_generation: str | None,
    head_receipt: Mapping[str, object] | None,
    governed_generation_reason: str | None = None,
) -> dict[str, object]:
    """One prompt's row: exact epoch interval, terminal class, governance, quota and auth."""
    if times.ended_epoch_ms <= times.sent_epoch_ms:
        raise SampleError(f"the {step.label} row has a non-positive interval")
    refusal = times.refusal_reason.lower()
    row: dict[str, object] = {
        "prompt_id": step.label,
        "request_started_at": _epoch_instant(times.sent_epoch_ms),
        "request_ended_at": _epoch_instant(times.ended_epoch_ms),
        "latency_ms": times.ended_epoch_ms - times.sent_epoch_ms,
        "terminal_class": {"done": "answer", "refused": "refused"}.get(
            times.terminal or "", "error"
        ),
        "governed_answer": times.status == 200
        and times.terminal == "done"
        and bool(times.released_text),
        "governed_generation": governed_generation,
        "governed_generation_reason": governed_generation_reason,
        "governed_tool_calls": [
            str(event["tool"]) for event in times.tool_events if event.get("status") == "completed"
        ],
        "http_status": times.status,
        "auth_outcome": "denied" if times.status in {401, 403} else "authenticated",
        "quota_refused": "quota" in refusal,
        "throttled": times.status == 429 or "throttl" in refusal,  # noqa: PLR2004
    }
    if head_receipt is not None:
        row["head_active_receipt"] = dict(head_receipt)
    return row


def draft_sample(
    manifest: ChatManifest,
    rows: Sequence[Mapping[str, object]],
    *,
    phase: str,
    identity: Mapping[str, object],
    selection: Mapping[str, Any],
    started_at: str,
    ended_at: str,
) -> dict[str, object]:
    """Build the six-row draft; launch, target-bake, and bake-interval fields are bound later."""
    if phase not in _PHASES.values():
        raise SampleError("unknown sample phase")
    if manifest.outside_list_item is None:
        raise SampleError("the manifest's outside-list item was never filled")
    if selection.get("outside_list_item") != manifest.outside_list_item or not all(
        selection.get(name)
        for name in ("outside_selection_sha256", "outside_selection_query_execution_id")
    ):
        raise SampleError("the draft is not bound to the receipt its prompt was filled from")
    if selection.get("target_unit") != identity.get("generation_unit"):
        raise SampleError("the draft's generation unit is not the receipt's target unit")
    if [row.get("prompt_id") for row in rows] != [step.label for step in manifest.steps]:
        raise SampleError("the draft rows do not follow the manifest exactly")
    if phase == "during_bake" and any(
        cast("Mapping[str, object]", row.get("head_active_receipt") or {}).get("state") != "ACTIVE"
        for row in rows
    ):
        raise SampleError("a during-bake row launched without an ACTIVE head receipt")
    return {
        "schema_version": CHAT_DRAFT_SCHEMA,
        "phase": phase,
        **identity,
        "persona_id": manifest.persona_id,
        "prompt_manifest_path": manifest.path.as_posix(),
        "prompt_manifest_sha256": manifest.sha256,
        "sample_count": CHAT_SAMPLE_COUNT,
        "sample_started_at": started_at,
        "sample_ended_at": ended_at,
        "outside_list_item": manifest.outside_list_item,
        "outside_selection_sha256": selection["outside_selection_sha256"],
        "outside_selection_query_execution_id": selection["outside_selection_query_execution_id"],
        "prompt_filled_sha256": filled_prompt_sha256(manifest),
        # Presence comes from the validated receipt: its item has impressions in the target
        # unit's window while that unit served the accepted generation. Absence is D2's leg
        # (owner, 2026-10-15) and is recorded as not measured here.
        "outside_list_present_in_generation": selection.get("outside_list_present") is True,
        "outside_list_absent_from_bounded_list": None,
        "outside_list_absence_status": ABSENCE_DEFERRED,
        "rows": [dict(row) for row in rows],
    }


_BIND_FIELDS = (
    "first_root_launch_at",
    "target_bake_run_id",
    "target_cohort_revision",
    "bake_run_started_at",
    "bake_finalize_ended_at",
)


def bind_chat_sample(draft: Mapping[str, object], binding: Mapping[str, str]) -> dict[str, object]:
    """Bind launch, target bake, and telemetry-derived interval; refuse anything inconsistent."""
    if (
        draft.get("schema_version") != CHAT_DRAFT_SCHEMA
        or draft.get("phase") not in _PHASES.values()
    ):
        raise SampleError("the draft has the wrong schema or phase")
    for name in _BIND_FIELDS:
        if not binding.get(name):
            raise SampleError(f"the binding has no {name}")
    rows = draft.get("rows")
    if not isinstance(rows, list) or len(cast("list[object]", rows)) != CHAT_SAMPLE_COUNT:
        raise SampleError("the draft does not hold six rows")
    preflight = _utc(draft.get("preflight_completed_at"), "preflight")
    launch = _utc(binding["first_root_launch_at"], "first root launch")
    started = _utc(draft.get("sample_started_at"), "sample start")
    ended = _utc(draft.get("sample_ended_at"), "sample end")
    bake_start = _utc(binding["bake_run_started_at"], "bake.run start")
    bake_end = _utc(binding["bake_finalize_ended_at"], "final bake.finalize end")
    if ended < started or bake_end < bake_start or launch < preflight:
        raise SampleError("a sample or bake interval is reversed")
    if draft["phase"] == "paired_no_bake" and not (started >= preflight and ended < launch):
        raise SampleError("the baseline is not after preflight and strictly before first launch")
    if draft["phase"] == "during_bake":
        cohort = binding["target_cohort_revision"]
        if draft.get("target_cohort_revision") not in (None, cohort):
            raise SampleError("the draft's ACTIVE target is another cohort")
        for row in cast("list[Mapping[str, object]]", rows):
            receipt = cast("Mapping[str, object]", row.get("head_active_receipt") or {})
            if receipt.get("state") != "ACTIVE" or receipt.get("cohort_revision") != cohort:
                raise SampleError("a during-bake row was not launched while the target was ACTIVE")
    return {**draft, **binding, "schema_version": CHAT_SAMPLE_SCHEMA}


@dataclass(frozen=True, slots=True)
class GenerationProof:
    """One strong generation read: the governed generation, or the one reason there is none."""

    generation: str | None
    reason: str | None

    @property
    def store_failure(self) -> bool:
        """Whether the read failed in the store rather than refusing the authority it read."""
        return self.reason in {"pointer_unreadable", "binding_unreadable"}


def _refused(reason: str) -> GenerationProof:
    return GenerationProof(None, reason)


def _binding_reason(parsed: Any, conflict_unit: str, generation: str) -> str | None:
    """Name the first fact that keeps a parsed binding from being current DIRECT truth."""
    from domain.operations.execution_graph_release import (
        BindingKind,
        binding_authorizes_serving,
    )

    checks = (
        ("binding_not_direct", parsed.binding_kind is BindingKind.DIRECT),
        ("binding_other_unit", parsed.conflict_unit == conflict_unit),
        ("binding_not_executed", parsed.execution_attempts > 0),
        ("binding_stale", not parsed.stale),
        ("binding_other_revision", parsed.artifact_revision == generation),
        ("binding_incomplete", binding_authorizes_serving(parsed)),
    )
    return next((reason for reason, holds in checks if not holds), None)


def governed_generation(  # noqa: PLR0911 -- one return per named refusal
    publications: Any,
    conflict_unit: str,
    read_binding: Callable[[str], Mapping[str, Any] | None],
) -> GenerationProof:
    """Return the governed generation a strong pointer and binding read prove, or why not.

    The pointer is read and parsed by the runtime's own `read_generation_pointer` (exact
    partition and record keys, item kind, schema version, positive ordinal and frozen counts),
    so a substituted or malformed envelope proves nothing. The semantic pointer carries the grant,
    not a generation, so the generation is `serving_generation(grant_id)`, exactly as the runtime
    stamps it. It counts only under that grant's `SERVING#/BINDING` holding complete current
    truth: a typed DIRECT binding for this conflict unit with executed attempts, not stale,
    naming that generation and authorizing serving (`binding_authorizes_serving`: every
    unavailable entry a designed absence), accepted by `require_granted_generation`.
    A legacy binding (no `binding_kind`) never counts, because the runtime accepts it only as
    read compatibility and before any of those checks. A store failure is named apart from a
    refused authority, so a broken read never reads as an ungranted generation.
    """
    from adapters.outbound.dynamodb.execution_graph_release import serving_binding_from_item
    from domain.operations.execution_graph_release import (
        UngrantedGenerationError,
        require_granted_generation,
        serving_generation,
    )
    from domain.shared_kernel.errors import DependencyError, InvariantError
    from ports.outbound.execution_graph_dispatch import GraphStoreUnavailableError

    try:
        pointer = publications.read_generation_pointer(conflict_unit)
    except DependencyError:
        return _refused("pointer_unreadable")
    except InvariantError, ValueError:
        return _refused("pointer_malformed")
    if pointer is None:
        return _refused("pointer_absent")
    if _DIGEST.fullmatch(pointer.grant_id) is None:
        return _refused("grant_malformed")
    try:
        binding = read_binding(pointer.grant_id)
    except Exception:  # noqa: BLE001 -- any read failure is a store failure, never a refusal
        return _refused("binding_unreadable")
    if binding is None:
        return _refused("binding_absent")
    generation = serving_generation(pointer.grant_id)
    try:
        parsed = serving_binding_from_item(pointer.grant_id, dict(binding))
    except GraphStoreUnavailableError, KeyError, ValueError, TypeError:
        return _refused("binding_malformed")
    reason = _binding_reason(parsed, conflict_unit, generation)
    if reason is not None:
        return _refused(reason)
    try:
        require_granted_generation({pointer.grant_id: parsed}, generation, graph_authority=True)
    except UngrantedGenerationError:
        return _refused("ungranted")
    return GenerationProof(generation, None)


def write_atomic(path: Path, document: Mapping[str, object]) -> None:
    """Write one artifact through a temporary file and one rename; never over an existing one."""
    if path.exists():
        raise SampleError(f"refusing to overwrite {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _bind_main(args: argparse.Namespace) -> int:
    """`--bind-draft`: local only; bind the post-bake facts and write the final sample."""
    if args.output is None:
        print("--bind-draft needs --output")
        return 2
    binding = {name: str(getattr(args, name) or "") for name in _BIND_FIELDS}
    try:
        draft = json.loads(Path(args.bind_draft).read_text(encoding="utf-8"))
        write_atomic(Path(args.output), bind_chat_sample(draft, binding))
    except (OSError, json.JSONDecodeError, SampleError) as error:
        print(f"NOT_PROVEN: {error}")
        return 1
    print(f"sample: {args.output}")
    return 0


def _publication_reader(publications: str, client: Any) -> Any:
    """Return the runtime publication store, used here only for its strong pointer read."""
    from adapters.outbound.dynamodb.semantic_state import (
        DynamoPublicationStore,
        DynamoPublicationStoreAccess,
    )

    # No lease or attempt table is touched by `read_generation_pointer`.
    return DynamoPublicationStore(publications, "", DynamoPublicationStoreAccess(client=client))


class _PhaseReads:
    """D8 strong reads: the serving head (during-bake launches) and the governed generation."""

    def __init__(self, session: boto3.Session, args: argparse.Namespace) -> None:
        from adapters.jobs.execution_graph.resolution import resolve_graph_table
        from domain.operations.execution_graph_release import INGESTION_RELEASE_DOMAIN

        self._dynamodb: Any = session.client("dynamodb")
        self._graph = (
            resolve_graph_table(session, args.environment, str(args.graph_store_stack))
            if args.graph_store_stack
            else None
        )
        self._domain = INGESTION_RELEASE_DOMAIN
        self._publications = published_parameter(
            session.client("ssm"), args.environment, "semanticV2PublicationsTableName"
        )
        self._unit = str(args.generation_unit)
        self._pointers = _publication_reader(self._publications, self._dynamodb)

    def _get(self, table: str, key: dict[str, object]) -> dict[str, Any] | None:
        return self._dynamodb.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item")

    def head(self) -> dict[str, object]:
        """Strongly read the serving head immediately before a during-bake launch."""
        if self._graph is None:
            raise SampleError("--graph-store-stack is required for during-bake")
        item = (
            self._get(
                self._graph, {"pk": {"S": f"SERVING_DOMAIN#{self._domain}"}, "sk": {"S": "HEAD"}}
            )
            or {}
        )
        return {
            "state": item.get("state", {}).get("S"),
            "cohort_revision": item.get("cohort_revision", {}).get("S"),
            "version": item.get("version", {}).get("N"),
            "read_at": datetime.datetime.now(datetime.UTC).isoformat(),
        }

    def window(self) -> str:
        """Strongly read the target unit's window from its pointer's grant and that grant's run."""
        graph = self._graph
        if graph is None:
            raise SampleError("--graph-store-stack is required to read the target window")
        pointer = self._pointers.read_generation_pointer(self._unit)
        if pointer is None:
            raise SampleError("the target unit has no generation pointer")
        grant = self._get(graph, {"pk": {"S": f"GRANT#{pointer.grant_id}"}, "sk": {"S": "GRANT"}})
        run_id = (grant or {}).get("run_id", {}).get("S")
        run = (
            self._get(graph, {"pk": {"S": f"RUN#{run_id}"}, "sk": {"S": "RUN"}}) if run_id else None
        )
        window = (run or {}).get("window", {}).get("S")
        if not isinstance(window, str) or not window:
            raise SampleError("the target unit's grant names no run window")
        return window

    def generation(self) -> GenerationProof:
        """Strongly read the unit's pointer and its grant's binding; see `governed_generation`."""
        graph = self._graph
        if graph is None:
            raise SampleError("--graph-store-stack is required with --phase")
        return governed_generation(
            self._pointers,
            self._unit,
            lambda grant: self._get(
                graph, {"pk": {"S": f"SERVING#{grant}"}, "sk": {"S": "BINDING"}}
            ),
        )


def require_serving_digest(value: object) -> str:
    """Require `--accepted-generation` to be one exact 64-hex `serving_generation` digest."""
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise SampleError(
            "--accepted-generation must be the 64-hex serving_generation(grant_id) digest of "
            "the target unit's DIRECT binding, not a label, cohort revision or ordinal"
        )
    return value


def require_accepted_generation(proof: GenerationProof, accepted: str, unit: str) -> None:
    """Refuse a phase whose target unit does not serve the accepted generation, and say why."""
    if proof.generation == accepted:
        return
    observed = proof.generation or "none"
    cause = f"store failure {proof.reason}" if proof.store_failure else proof.reason or "mismatch"
    raise SampleError(
        f"unit {unit} serves governed generation {observed} ({cause}), not the accepted "
        f"generation {accepted}"
    )


def _phase_identity(args: argparse.Namespace, origin: str) -> dict[str, object]:
    for name in (
        "output",
        "acceptance_execution_id",
        "accepted_generation",
        "generation_unit",
        "preflight_completed_at",
        "outside_selection_receipt",
        "graph_store_stack",
    ):
        if not getattr(args, name):
            raise SampleError(f"--{name.replace('_', '-')} is required with --phase")
    if args.rounds != 1:
        raise SampleError("--phase takes exactly one round of six prompts")
    require_serving_digest(args.accepted_generation)
    if args.phase == "during-bake" and not (args.graph_store_stack and args.target_cohort_revision):
        raise SampleError(
            "--phase during-bake needs --graph-store-stack and --target-cohort-revision"
        )
    _utc(args.preflight_completed_at, "preflight")
    return {
        "acceptance_execution_id": args.acceptance_execution_id,
        "environment": args.environment,
        "account": args.expected_account,
        "origin": origin,
        "accepted_generation": args.accepted_generation,
        "generation_unit": args.generation_unit,
        "preflight_completed_at": args.preflight_completed_at,
        "target_cohort_revision": args.target_cohort_revision,
    }


def _phase_setup(
    session: boto3.Session, args: argparse.Namespace, origin: str
) -> tuple[dict[str, object], _PhaseReads]:
    """Validate a phase's identity and require its unit to serve the accepted generation."""
    identity = _phase_identity(args, origin)
    reads = _PhaseReads(session, args)
    require_accepted_generation(
        reads.generation(), str(args.accepted_generation), str(args.generation_unit)
    )
    return identity, reads


def _evidence_dir(root: Path | None = None) -> Path:
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    directory = (root or _EVIDENCE_ROOT) / f"turn-latency.{stamp}"
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def _recorded_selection(args: argparse.Namespace) -> dict[str, Any]:
    """Validate the selection receipt; a phase run also binds it to its execution and generation.

    A plain latency run (no `--phase`) is informational and checks the receipt's own fields only.
    """
    if args.outside_selection_receipt is None:
        raise SampleError("the outside-list prompt needs --outside-selection-receipt")
    phased = args.phase is not None
    selection = load_outside_selection(
        Path(args.outside_selection_receipt),
        execution_id=str(args.acceptance_execution_id) if phased else None,
        environment=str(args.environment),
        generation=str(args.accepted_generation) if phased else None,
    )
    if phased and selection["target_unit"] != args.generation_unit:
        raise SampleError("--generation-unit is not the selection receipt's target unit")
    return selection


def _run_selection_query(
    athena: Any, sql: str, *, workgroup: str, database: str
) -> dict[str, object]:
    """Run the one read-only selection query to completion and read it back.

    The start goes through the repository's one sanctioned wrapper (`execution.start`), with
    result reuse off, so the verdict's re-read describes a fresh read of this execution.
    """
    from adapters.outbound.athena.execution import StatementRequest, start
    from domain.shared_kernel.errors import DependencyError

    try:
        execution = start(athena, StatementRequest(sql, (), workgroup, database, 0))
    except DependencyError as error:
        raise SampleError(f"the selection query could not start: {error}") from error
    deadline = time.monotonic() + _QUERY_TIMEOUT_SECONDS
    while True:
        status = athena.get_query_execution(QueryExecutionId=execution)["QueryExecution"]
        state = str(status["Status"]["State"])
        if state == "SUCCEEDED":
            break
        if state in {"FAILED", "CANCELLED"} or time.monotonic() > deadline:
            raise SampleError(f"the selection query {execution} ended {state}")
        time.sleep(2)
    return athena_query_readback(athena, execution)


@dataclass(frozen=True, slots=True)
class SelectionPorts:
    """What the selection touches: Athena, the real entity catalog, the unit's generation."""

    athena: Any
    browse: Callable[[datetime.date, int], Any]
    generation: Callable[[], GenerationProof]
    window: Callable[[], str]
    now: Callable[[], datetime.datetime]


def _browse_keys(result: Any) -> list[str]:
    return sorted({str(alias.key.value) for alias in result.aliases})


def observe_selection(
    ports: SelectionPorts, *, environment: str, accepted: str, unit: str
) -> SelectionObservation:
    """Run the governed selection reads: generation, window, CHAT query, browse, generation.

    The target unit must serve the accepted generation before and after every other read, and
    its window (read from its grant's run) must digest back to the unit itself.
    """
    from ports.outbound.entity_catalog import MAX_CAMPAIGN_PORTFOLIO_CANDIDATES

    require_accepted_generation(ports.generation(), accepted, unit)
    window = ports.window()
    if window_unit(window) != unit:
        raise SampleError("the target unit's run names a window of another unit")
    sql, _digest = selection_sql(environment, window)
    readback = _run_selection_query(
        ports.athena,
        sql,
        workgroup=chat_workgroup(environment),
        database=f"adlytics_{environment}_v2_current",
    )
    current_on = ports.now().date()
    complete = ports.browse(current_on, MAX_CAMPAIGN_PORTFOLIO_CANDIDATES)
    require_accepted_generation(ports.generation(), accepted, unit)
    return SelectionObservation(
        query_execution_id=str(readback["query_execution_id"]),
        query_completed_at=str(readback["completed_at"]),
        generation_keys=tuple(cast("list[str]", readback["keys"])),
        browse_current_on=current_on.isoformat(),
        catalog_keys=tuple(_browse_keys(complete)),
        catalog_truncated=bool(complete.truncated),
        target_unit=unit,
        target_window=window,
        target_generation=accepted,
        read_at=ports.now().isoformat(),
    )


def live_catalog_browse(session: Any, environment: str) -> Callable[[datetime.date, int], Any]:
    """Return the chat tool's own `CanonicalEntityCatalog` browse over the explicit profile."""
    from adapters.inbound.tool_handlers import gateway
    from adapters.jobs.contract_validation.loader import load_contracts
    from adapters.outbound.commerce_identity import load_commerce_identity_bridge
    from adapters.outbound.dynamodb.semantic_state import DynamoCurrentPublications
    from adapters.outbound.entity_catalog import CanonicalEntityCatalog, EntityCatalogTooling
    from adapters.outbound.media_taxonomy import load_media_taxonomy
    from adapters.outbound.s3.semantic_storage import S3CanonicalAdapter
    from adapters.outbound.source_pack import load_source_packs
    from domain.semantics.analytical_resolution import (
        AuthorizedAnalyticalScope,
        EntityContext,
        EntityKind,
    )
    from ports.outbound.entity_catalog import EntityCatalogBrowseRequest

    root = gateway.contracts_root()
    ssm = session.client("ssm")
    bridge = load_commerce_identity_bridge(root)
    catalog = CanonicalEntityCatalog(
        EntityCatalogTooling(
            current=DynamoCurrentPublications(
                published_parameter(ssm, environment, "semanticV2PublicationsTableName"),
                session.client("dynamodb"),
            ),
            canonical=S3CanonicalAdapter(
                published_parameter(ssm, environment, "semanticV2BucketName"),
                session.client("s3"),
            ),
            packs=load_source_packs(root),
            contracts=tuple(contract for _, contract in load_contracts(root)),
            bridge=bridge,
            taxonomy=load_media_taxonomy(root),
        )
    )
    scope = AuthorizedAnalyticalScope(frozenset({bridge.commerce_scope}))

    def browse(current_on: datetime.date, limit: int) -> Any:
        return catalog.browse(
            EntityCatalogBrowseRequest(
                scope=scope,
                kinds=frozenset({EntityKind.CAMPAIGN}),
                window=None,
                current_on=current_on,
                context=EntityContext(),
                max_candidates=limit,
            )
        )

    return browse


def _select_main(args: argparse.Namespace, ports: SelectionPorts | None = None) -> int:
    """`--select-outside-list-item`: governed read-only selection; writes the one receipt.

    Without injected `ports` it opens the explicit profile, requires its account, and reads the
    CHAT workgroup, the chat tool's own catalog and the target unit's strong generation.
    """
    if args.environment != "dev" or args.region != _REGION:
        print("this probe runs only against the approved dev region")
        return 2
    for name in (
        "acceptance_execution_id",
        "accepted_generation",
        "generation_unit",
        "graph_store_stack",
        "output",
    ):
        if not getattr(args, name):
            print(f"--{name.replace('_', '-')} is required with --select-outside-list-item")
            return 2
    try:
        accepted = require_serving_digest(args.accepted_generation)
        if ports is None:
            session = boto3.Session(profile_name=args.profile, region_name=args.region)
            if session.client("sts").get_caller_identity()["Account"] != args.expected_account:
                print("explicit profile did not resolve to the expected account")
                return 2
            reads = _PhaseReads(session, args)
            ports = SelectionPorts(
                athena=session.client("athena"),
                browse=live_catalog_browse(session, str(args.environment)),
                generation=reads.generation,
                window=reads.window,
                now=lambda: datetime.datetime.now(datetime.UTC),
            )
        observed = observe_selection(
            ports,
            environment=str(args.environment),
            accepted=accepted,
            unit=str(args.generation_unit),
        )
        receipt = selection_receipt(
            execution_id=str(args.acceptance_execution_id),
            environment=str(args.environment),
            generation=accepted,
            observed=observed,
        )
        write_atomic(Path(args.output), receipt)
    except SampleError as error:
        print(f"NOT_PROVEN: {error}")
        return 1
    print(
        f"selected {receipt['selected_item']} of {len(observed.catalog_keys)} catalog and "
        f"{len(observed.generation_keys)} generation campaigns, query {observed.query_execution_id}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the sequence, join each turn's telemetry, and print the sanitized breakdown."""
    args = _parser().parse_args(argv)
    turn_list: LatencyTurns | None = None
    if args.turns is not None:
        try:
            refuse_d8_flags_with_turns(args)
            turn_list = load_latency_turns(args.turns)
        except (OSError, SampleError) as error:
            print(f"the latency turn list is refused: {error}")
            return 2
    if args.bind_draft is not None or args.select_outside_list_item:
        return _bind_main(args) if args.bind_draft is not None else _select_main(args)
    if args.environment != "dev" or args.region != _REGION:
        print("this probe runs only against the approved dev region")
        return 2
    manifest: ChatManifest | None = None
    selection: dict[str, Any] = {}
    if turn_list is None:
        try:
            selection = _recorded_selection(args)
            manifest = with_outside_list_item(
                load_chat_manifest(args.manifest), selection["outside_list_item"]
            )
        except (OSError, json.JSONDecodeError, SampleError) as error:
            print(f"the chat manifest is refused: {error}")
            return 2
    steps = manifest.steps if manifest is not None else cast("LatencyTurns", turn_list).steps
    classes = (
        dict(zip((step.label for step in turn_list.steps), turn_list.classes, strict=True))
        if turn_list is not None
        else {}
    )
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    if session.client("sts").get_caller_identity()["Account"] != args.expected_account:
        print("explicit profile did not resolve to the expected account")
        return 2
    ssm = session.client("ssm")
    origin = published_parameter(ssm, args.environment, "publicBaseUrl").rstrip("/")
    tokens = sign_in(
        session.client("cognito-idp"),
        load_persona(ssm, args.environment, "probe-a"),
        app_client(session, args.environment),
    )
    csrf = secrets.token_urlsafe(24)
    logs = _Logs(session)
    groups = _stack_log_groups(session, args.environment)
    harness_group = logs.newest_group(f"{_HARNESS_LOG_PREFIX}adlytics_{args.environment}_agent-")
    phase_rows: list[dict[str, object]] = []
    phase_reads: _PhaseReads | None = None
    if args.phase is not None:
        try:
            identity, phase_reads = _phase_setup(session, args, origin)
        except SampleError as error:
            print(f"NOT_PROVEN: {error}")
            return 1
    sample_started = datetime.datetime.now(datetime.UTC).isoformat()
    turns: list[tuple[int, Step, ClientTimes]] = []
    for round_index in range(args.rounds):
        conversations = {step.conversation: f"turn-latency-{uuid.uuid4()}" for step in steps}
        for step in steps:
            receipt = (
                phase_reads.head()
                if phase_reads is not None and args.phase == "during-bake"
                else None
            )
            before = None if phase_reads is None else phase_reads.generation()
            times = _call(
                origin=origin,
                token=tokens.access_token,
                csrf=csrf,
                question=step.question,
                session_id=conversations[step.conversation],
                record_stream_errors=turn_list is not None,
            )
            turns.append((round_index, step, times))
            if phase_reads is not None and before is not None:
                after = phase_reads.generation()
                held = before.generation is not None and before.generation == after.generation
                phase_rows.append(
                    chat_sample_row(
                        step,
                        times,
                        governed_generation=before.generation if held else None,
                        head_receipt=receipt,
                        governed_generation_reason=None
                        if held
                        else before.reason or after.reason or "generation_changed",
                    )
                )
            print(
                f"ran {step.label} round {round_index + 1}: status {times.status}, "
                f"terminal {times.terminal}, first text {times.first_text_ms} ms, "
                f"terminal at {times.terminal_ms} ms"
                + (
                    ""
                    if times.stream_error is None
                    else f", stream {times.stream_error} ({times.content_type}, "
                    f"{times.body_bytes} B)"
                ),
                flush=True,
            )
            time.sleep(args.gap_seconds)
    if args.phase is not None and manifest is not None:
        try:
            write_atomic(
                Path(args.output),
                draft_sample(
                    manifest,
                    phase_rows,
                    phase=_PHASES[args.phase],
                    identity=identity,
                    selection=selection,
                    started_at=sample_started,
                    ended_at=datetime.datetime.now(datetime.UTC).isoformat(),
                ),
            )
        except SampleError as error:
            print(f"NOT_PROVEN: {error}")
            return 1
        print(f"draft: {args.output}")
    time.sleep(_TELEMETRY_SETTLE_SECONDS)
    evidence_dir = _evidence_dir(args.telemetry_dir)
    summaries: list[dict[str, object]] = []
    for round_index, step, times in turns:
        timing = _timing_line(logs, groups["ConversationFunction"], times)
        trace_id = str(timing.get("trace_id", ""))
        route_spans = (
            _route_spans(logs, trace_id, times, environment=args.environment) if trace_id else []
        )
        window = _window_spans(
            logs,
            times,
            (
                "tool.",
                "gateway.interceptor",
                "athena.query",
                "Athena.",
                "token_service.",
                "KMS.",
                "Secrets Manager.",
            ),
            environment=args.environment,
        )
        harness = harness_segments(
            _harness_trace(
                logs,
                trace_id=trace_id,
                service_prefix=harness_service_prefix(args.environment),
                times=times,
            )
        )
        summary: dict[str, object] = {
            "round": round_index + 1,
            "turn": step.label,
            "status": times.status,
            "terminal": times.terminal,
            "stream_error": times.stream_error,
            "content_type": times.content_type,
            "body_bytes": times.body_bytes,
            # The client's send time, so offsets below can be placed on the wall clock later.
            "sent_epoch_ms": times.sent_epoch_ms,
            "client_ms": {
                "headers": times.headers_ms,
                "first_event": times.first_event_ms,
                "first_text": times.first_text_ms,
                "first_answer_text": times.first_answer_text_ms,
                "terminal": times.terminal_ms,
                "stream_end": times.stream_end_ms,
                "tool_events": times.tool_events,
                "text_deltas": times.text_deltas,
            },
            "route_timing": {
                key: value
                for key, value in timing.items()
                if key.startswith(("ms_", "usage_"))
                or key
                in {
                    "cold_start",
                    "tools",
                    "assistant_messages_ms",
                    "text_deltas",
                    "metadata_events",
                    "tool_calls",
                    "assistant_messages",
                }
            },
            "breakdown": _breakdown(times, timing, route_spans),
            "route_spans": [
                {
                    "name": span["name"],
                    "offset_ms": cast("int", span["start_epoch_ms"]) - times.sent_epoch_ms,
                    "duration_ms": span["duration_ms"],
                }
                for span in route_spans
            ],
            "tool_side_spans": [
                {
                    "name": span["name"],
                    "offset_ms": cast("int", span["start_epoch_ms"]) - times.sent_epoch_ms,
                    "duration_ms": span["duration_ms"],
                }
                for span in window
            ],
            "harness_trace": harness,
            "cross_clock_inferred": cross_clock(harness, route_spans=route_spans, timing=timing),
            "harness_runtime": _harness_metrics(logs, harness_group, times),
            "harness_session": _harness_session_start(logs, harness_group, times),
            "lambda_reports": _lambda_reports(logs, groups, times),
            "hop_split": hop_split(times, route_spans=route_spans, harness=harness, timing=timing),
        }
        if classes:
            summary["class"] = classes[step.label]
        summaries.append(summary)
        (evidence_dir / f"{round_index + 1}-{step.label}.json").write_text(
            json.dumps(
                {
                    "summary": summary,
                    "correlation_id": times.correlation_id,
                    "audit_id": times.audit_id,
                    "trace_id": trace_id,
                    "released_text": times.released_text,
                },
                indent=1,
            ),
            encoding="utf-8",
        )
    document: object = summaries
    if turn_list is not None:
        document = latency_turns_summary(turn_list, summaries)
    (evidence_dir / "turn-latency-summary.json").write_text(
        json.dumps(document, indent=1), encoding="utf-8"
    )
    print(json.dumps(document, indent=1))
    shown = evidence_dir.relative_to(_ROOT) if evidence_dir.is_relative_to(_ROOT) else evidence_dir
    print(f"evidence: {shown}")
    return 0 if all(item["terminal"] == "done" for item in summaries) else 1


if __name__ == "__main__":
    sys.exit(main())
