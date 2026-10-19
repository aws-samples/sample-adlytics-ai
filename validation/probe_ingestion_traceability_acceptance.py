# SPDX-License-Identifier: MIT-0
"""Plan items 13-14: the live verdict gatherers behind the gate's `--acceptance-verdicts` mode.

Read-only. Each gatherer turns one acceptance execution's retained same-run artifacts into the
evidence object its verdict takes, then writes one receipt per verdict into that execution's
evidence directory:

- `trace_completeness_verdict`: `TraceCompletenessEvidence` from the `aws/spans` retrieval of
  every direct cohort and the promoter passes, joined to the Athena executions of the BAKE and
  INSIGHT_PRECOMPUTE workgroups. The expected cardinality comes from records outside the spans:
  the generated bake roster and phases, the winner's precompute contribution request roster,
  the Baker `task_summary` lines, Athena's own execution list, and the serving transitions.
  Paired negative controls then mutate that same live evidence (one span of each producer
  class removed, a correlation field stripped, a parent rewired, written bytes and scanned
  statistics changed, the promoter pass removed); each must fail.
- `paired_bake_chat_verdict`: the two bound `ChatLatencySampleV1` artifacts against the bake
  interval the trace retrieval proves (A's `bake.run` start to its final successful
  `bake.finalize` end), refusing any execution ID an earlier accepted receipt used.
- `athena_admission_config_verdict`: the explicit `.env` value, every synthesized, deployed
  (CloudFormation template) and read-back (Lambda configuration) general batch value, and the
  seven static caller rows derived from those same templates.
- `hold_clear_matrix_verdict`: one evidence entry per historical row, keyed to this execution
  ID, from the receipts its command produces.

Every input is bound to the execution under test before a verdict may use it: the control
receipt, the trace artifacts and both chat samples by their own `acceptance_execution_id`; the
Athena admission artifact by its collection identity plus the current code SHA and environment;
and a matrix document that names no execution by the run-generated
`matrix-evidence-manifest-v1.json`, which records its path and digest. An input another
execution produced therefore yields `NOT_PROVEN`, never a freshly stamped `PASS`.

The live reads are separated from the pure builders so every builder is unit-tested over
fixtures (`tests/unit/test_probe_ingestion_traceability_acceptance.py`).
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

import probe_ingestion_traceability_gate as gate
from probe_ingestion_traceability_gate import (
    BAKE_FINALIZE_SUCCEEDED,
    PROMOTER_DOMAIN_SPAN,
    PROMOTER_PASS_SPAN,
    TRACE_COHORT_CLASSES,
    ProbeError,
    TraceCohortExpectation,
    TraceCompletenessEvidence,
    athena_admission_config_verdict,
    canonical_json_bytes,
    hold_clear_matrix_verdict,
    load_hold_clear_matrix,
    paired_bake_chat_verdict,
    trace_completeness_verdict,
    utc_instant,
)

RECEIPT_SCHEMA: Final = "acceptance-verdict-receipt-v1"
TRACE_ARTIFACTS_SCHEMA: Final = "trace-artifacts-v1"
CONTROL_RECEIPT: Final = "rolling-freshness-control-v1.json"
TRACE_ARTIFACTS: Final = "trace-artifacts-v1.json"
CHAT_BASELINE: Final = "chat-latency-paired-no-bake.json"
CHAT_DURING: Final = "chat-latency-during-bake.json"
OUTSIDE_SELECTION: Final = "chat-outside-list-selection-v3.json"
"""`probe_turn_latency.OUTSIDE_SELECTION_RECEIPT`: the execution's one governed selection."""
SELECTION_READBACK: Final = "outside-selection-athena-readback-v1.json"
"""The verdict-time re-read of the selection's Athena query, retained once per execution."""
CHAT_PROMPT_MANIFEST: Final = (
    Path(__file__).resolve().parent / "fixtures" / "chat-bake-acceptance-v2.json"
)
ATHENA_ADMISSION: Final = "athena-admission-receipt-v1.json"
ADMISSION_SCHEMA: Final = "athena-admission-receipt-v1"
MATRIX_MANIFEST: Final = "matrix-evidence-manifest-v1.json"
MATRIX_MANIFEST_SCHEMA: Final = "matrix-evidence-manifest-v1"
RECEIPTS: Final = {
    "trace_completeness_verdict": "trace-completeness-verdict.json",
    "paired_bake_chat_verdict": "paired-bake-chat-verdict.json",
    "athena_admission_config_verdict": "athena-admission-config-verdict.json",
    "hold_clear_matrix_verdict": "hold-clear-matrix-verdict.json",
}
MATRIX_COMMAND_RECEIPTS: Final = {
    "observed_cohort_verdict": "observed-cohort-verdict.json",
    "trace_completeness_verdict": RECEIPTS["trace_completeness_verdict"],
    "paired_bake_chat_verdict": RECEIPTS["paired_bake_chat_verdict"],
}
"""A historical row whose command is one of these reads that receipt; every other retained row
needs an explicit `HIST-<id>=<path>` source (P6(e), the footnote readback)."""
GENERAL_BATCH_VARIABLE: Final = "ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY"
ENV_VARIABLE: Final = "ADLYTICS_ATHENA_QUERY_BATCH_MAX_CONCURRENCY"
GENERAL_BATCH_STACKS: Final = ("Api", "Agent", "Ingestion")
"""The stacks whose functions consume the general value; Publisher and InsightPrecompute carry
their own independent caps under the same variable name and are static rows instead."""
_STATIC_FUNCTIONS: Final = {
    "baker": ("Publisher", "Baker"),
    "table_writer": ("Ingestion", "Stage1TableWriter"),
    "inline_ingestion": ("Ingestion", "IngestionFunction"),
    "provider_attempt_reconciler": ("Ingestion", "Stage1ProviderAttemptReconciler"),
    "insight_precompute": ("InsightPrecompute", ""),
}
"""Static row -> (stack, function logical-ID prefix): reserved concurrency x start cap."""
_OPERATOR_ROWS: Final = ("ingest_operator", "reprocess_operator")
_OPERATOR_CONSTANT: Final = re.compile(r"export const OPERATOR_PROCESSES_PER_ROW = (\d+);")
_ATHENA_WORKGROUP_KINDS: Final = ("bake", "insight-precompute")


# --- Receipts ------------------------------------------------------------------------------


def file_sha256(path: Path) -> str:
    """The SHA-256 of one retained input."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def receipt_document(
    verdict: str,
    execution_id: str,
    result: Mapping[str, object],
    inputs: Mapping[str, Path],
) -> dict[str, object]:
    """One verdict's receipt: its execution, every input's path and digest, and the result."""
    passed = result.get("passed") is True
    return {
        "schema_version": RECEIPT_SCHEMA,
        "verdict": verdict,
        "acceptance_execution_id": execution_id,
        "inputs": {
            name: {"path": str(path), "sha256": file_sha256(path)}
            for name, path in sorted(inputs.items())
        },
        "result": dict(result),
        "passed": passed,
        "disposition": "PASS" if passed else "NOT_PROVEN",
        "written_at": datetime.now(UTC).isoformat(),
    }


def write_json_atomic(path: Path, document: Mapping[str, object]) -> Path:
    """Write once through a temporary file and one rename; never over an existing receipt."""
    if path.exists():
        raise ProbeError(f"refusing to overwrite {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def read_json(path: Path, what: str) -> dict[str, object]:
    """Read one retained JSON artifact; a missing or malformed one is a `ProbeError`."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProbeError(f"the {what} artifact is unreadable: {path}") from error
    if not isinstance(document, dict):
        raise ProbeError(f"the {what} artifact is not an object: {path}")
    return cast("dict[str, object]", document)


# --- Execution binding -----------------------------------------------------------------------


def execution_reasons(document: Mapping[str, object], execution_id: str, what: str) -> list[str]:
    """`execution_mismatch:<what>` unless the document names exactly this execution."""
    named = document.get("acceptance_execution_id")
    return [] if named == execution_id else [f"execution_mismatch:{what}"]


def bound_result(result: Mapping[str, object], failures: Sequence[str]) -> dict[str, object]:
    """A verdict result that is `NOT_PROVEN` whenever any of its inputs is unbound."""
    if not failures:
        return {**result, "binding_failures": []}
    return {
        **result,
        "binding_failures": list(failures),
        "passed": False,
        "disposition": "NOT_PROVEN",
    }


# --- Trace completeness ----------------------------------------------------------------------

TRACE_WINDOW_PAD: Final = timedelta(minutes=5)
"""The trace collection window extends this far past the control's end; the acceptance waits
the span lag after that, so the window's own tail is never read inside the lag."""


@dataclass(frozen=True, slots=True)
class DirectCohort:
    """One direct cohort the control drove: its winner, grant and chain window."""

    cohort_revision: str
    run_id: str
    grant_id: str
    window_start: str
    window_end: str


def _attribute(span: Mapping[str, object], key: str) -> object:
    attributes = span.get("attributes")
    if not isinstance(attributes, Mapping):
        return None
    return cast("Mapping[str, object]", attributes).get(key)


def cohort_expectation(
    cohort: DirectCohort,
    *,
    roster: Sequence[str],
    phases: Sequence[str],
    task_summaries: Sequence[Mapping[str, object]],
    request_roster: Sequence[Sequence[str]],
    athena: Sequence[Mapping[str, object]],
    generations: Mapping[str, int],
) -> TraceCohortExpectation:
    """Derive one cohort's exact span cardinality from the runtime's own records.

    Every count comes from the Baker's `task_summary` lines and the precompute's slot records,
    never from a time window: a cohort's queries are its summaries' `query_executions`, not the
    workgroup's executions inside the chain window (x5 rehearsal, 2026-10-16).
    """
    owned = [row for row in task_summaries if row.get("run_id") == cohort.run_id]
    keys = sorted(f"{row.get('page')}#{row.get('variant')}" for row in owned)
    if keys != sorted(roster):
        raise ProbeError(f"{cohort.cohort_revision}: the task summaries do not cover the roster")
    del phases, request_roster  # the runtime's own records below decide every count
    if any(int(cast("int", row.get("statistics_unavailable") or 0)) for row in owned):
        raise ProbeError(f"{cohort.cohort_revision}: a task's query statistics are unavailable")
    written = [row for row in owned if row.get("effect_kind") == "written"]
    return TraceCohortExpectation(
        cohort_revision=cohort.cohort_revision,
        run_id=cohort.run_id,
        bake_task_keys=tuple(keys),
        task_writes=tuple(
            (str(row.get("task_id")), row.get("effect_kind") == "written") for row in owned
        ),
        artifact_writes=sum(int(cast("int", row.get("pairs") or 0)) for row in written),
        artifact_bytes=tuple(sorted(int(cast("int", row.get("written_bytes"))) for row in written)),
        athena_queries=sum(int(cast("int", row.get("query_executions") or 0)) for row in owned),
        athena_succeeded_ids=frozenset(
            str(item["query_execution_id"]) for item in athena if item.get("state") == "SUCCEEDED"
        ),
        athena_bytes_scanned=sum(int(cast("int", row.get("scanned_bytes") or 0)) for row in owned),
        model_requests=int(generations.get(cohort.run_id, 0)),
        requestless_tasks=frozenset(str(row.get("task_id")) for row in owned if _requestless(row)),
    )


_REQUESTLESS_ZEROS: Final = (
    "query_executions",
    "executed_distinct",
    "phase_query_ms",
    "phase_render_ms",
    "phase_write_ms",
)
"""The summary fields a requestless task records as zero: nothing executed, no round timed."""


def _requestless(row: Mapping[str, object]) -> bool:
    """An unavailable task whose summary records no execution and no round (a `no_request` row).

    The field must be present and exactly zero; a summary missing one is not requestless, so its
    task keeps the at-least-one-round expectation and an absent record never relaxes it.
    """
    return row.get("effect_kind") == "unavailable" and all(
        row.get(name) == 0 and not isinstance(row.get(name), bool) for name in _REQUESTLESS_ZEROS
    )


def attribute_athena(
    executions: Sequence[Mapping[str, object]], cohorts: Sequence[DirectCohort]
) -> list[dict[str, object]]:
    """Attribute each BAKE/INSIGHT_PRECOMPUTE execution to the one cohort whose chain held it.

    Chains are serialized by election, so the windows are disjoint; an execution outside every
    window, or inside two, is refused rather than guessed.
    """
    attributed: list[dict[str, object]] = []
    for item in executions:
        submitted = utc_instant(item.get("submitted_at"), "Athena submission")
        owners = [
            cohort.cohort_revision
            for cohort in cohorts
            if utc_instant(cohort.window_start, "window start")
            <= submitted
            <= utc_instant(cohort.window_end, "window end")
        ]
        if len(owners) != 1:
            raise ProbeError(
                f"Athena execution {item.get('query_execution_id')} is in {len(owners)} windows"
            )
        attributed.append({**item, "cohort_revision": owners[0]})
    return attributed


def promoter_attempts(
    spans: Sequence[Mapping[str, object]], transitions: Sequence[Mapping[str, object]]
) -> tuple[tuple[tuple[str, str, str], ...], list[str]]:
    """The attempted promoter-domain children, cross-checked against every serving transition.

    The children are enumerated from the retrieved domain spans (no other record names an
    attempt that changed nothing); each transition's request, domain and drained cohort must be
    among them, and each attempt's request must own a pass span. Failures are returned.
    """
    passes = {
        str(_attribute(span, "aws_request_id"))
        for span in spans
        if span.get("name") == PROMOTER_PASS_SPAN
    }
    attempts = sorted(
        {
            (
                str(_attribute(span, "aws_request_id")),
                str(_attribute(span, "domain")),
                str(_attribute(span, "cohort_revision")),
            )
            for span in spans
            if span.get("name") == PROMOTER_DOMAIN_SPAN
        }
    )
    failures = [
        f"transition {item.get('aws_request_id')} has no promoter-domain child"
        for item in transitions
        if (
            str(item.get("aws_request_id")),
            str(item.get("domain")),
            str(item.get("drained_cohort_revision")),
        )
        not in set(attempts)
    ]
    failures.extend(
        f"attempt {request} has no promoter pass"
        for request, _, _ in attempts
        if request not in passes
    )
    return tuple(attempts), failures


def trace_evidence(artifacts: Mapping[str, object]) -> tuple[TraceCompletenessEvidence, list[str]]:
    """Build `TraceCompletenessEvidence` from the retained trace artifacts."""
    if artifacts.get("schema_version") != TRACE_ARTIFACTS_SCHEMA:
        raise ProbeError("the trace artifacts have the wrong schema")
    cohorts = [
        DirectCohort(**cast("dict[str, str]", item))
        for item in cast("list[object]", artifacts["cohorts"])
    ]
    retrievals = cast("Mapping[str, Mapping[str, object]]", artifacts["retrievals"])
    spans: list[Mapping[str, object]] = []
    seen: set[tuple[object, object]] = set()
    complete = True
    for retrieval in retrievals.values():
        complete = (
            complete and retrieval.get("status") == "complete" and not retrieval.get("truncated")
        )
        for span in cast("list[Mapping[str, object]]", retrieval.get("spans") or []):
            identity = (span.get("traceId"), span.get("spanId"))
            if identity not in seen:
                seen.add(identity)
                spans.append(span)
    athena = cast("list[Mapping[str, object]]", artifacts["athena_executions"])
    if "slot_generations" not in artifacts:
        raise ProbeError("the trace artifacts name no precompute slot generations")
    generations = cast("Mapping[str, int]", artifacts["slot_generations"])
    contributions = cast("Mapping[str, list[list[str]]]", artifacts["request_rosters"])
    summaries = cast("list[Mapping[str, object]]", artifacts["task_summaries"])
    expectations = tuple(
        cohort_expectation(
            cohort,
            roster=cast("list[str]", artifacts["roster"]),
            phases=cast("list[str]", artifacts["phases"]),
            task_summaries=summaries,
            request_roster=contributions.get(cohort.grant_id, []),
            athena=athena,
            generations=generations,
        )
        for cohort in cohorts
    )
    attempts, failures = promoter_attempts(
        spans, cast("list[Mapping[str, object]]", artifacts["transitions"])
    )
    return (
        TraceCompletenessEvidence(
            cohorts=expectations,
            promoter_attempts=attempts,
            spans=tuple(spans),
            retrieval_complete=complete,
        ),
        failures,
    )


def _without(spans: Sequence[Mapping[str, object]], index: int) -> tuple[Mapping[str, object], ...]:
    return tuple(span for position, span in enumerate(spans) if position != index)


def _changed(
    spans: Sequence[Mapping[str, object]], index: int, change: Callable[[dict[str, Any]], None]
) -> tuple[Mapping[str, object], ...]:
    mutated = [copy.deepcopy(dict(span)) for span in spans]
    change(mutated[index])
    return tuple(mutated)


def _first(spans: Sequence[Mapping[str, object]], name: str) -> int | None:
    return next((index for index, span in enumerate(spans) if span.get("name") == name), None)


def trace_negative_controls(evidence: TraceCompletenessEvidence) -> dict[str, dict[str, object]]:
    """Mutate the live evidence once per paired control; each mutated verdict must fail."""
    spans = evidence.spans
    mutations: dict[str, tuple[Mapping[str, object], ...]] = {}
    for name in (*TRACE_COHORT_CLASSES, PROMOTER_DOMAIN_SPAN):
        index = _first(spans, name)
        if index is not None:
            mutations[f"omit:{name}"] = _without(spans, index)
    # A pass control must mutate a pass that owns an attempt. Most passes are idle (x9: 4 of 49
    # owned a domain child), and the verdict rightly ignores idle ones, so omitting the first
    # pass span (x9's, idle) changed nothing and the control was accepted.
    attempting = {attempt[0] for attempt in evidence.promoter_attempts}
    passes = [i for i, span in enumerate(spans) if span.get("name") == PROMOTER_PASS_SPAN]
    owning = next((i for i in passes if _attribute(spans[i], "aws_request_id") in attempting), None)
    if owning is not None:
        mutations[f"omit:{PROMOTER_PASS_SPAN}"] = _without(spans, owning)
        mutations[f"request:{PROMOTER_PASS_SPAN}"] = _changed(
            spans,
            owning,
            lambda span: span["attributes"].__setitem__("aws_request_id", "req-mismatched"),
        )
        child = next(
            (
                i
                for i, span in enumerate(spans)
                if span.get("name") == PROMOTER_DOMAIN_SPAN
                and _attribute(span, "aws_request_id")
                == _attribute(spans[owning], "aws_request_id")
            ),
            None,
        )
        other = next(
            (str(spans[i].get("spanId")) for i in passes if i != owning and spans[i].get("spanId")),
            None,
        )
        if child is not None and other is not None:
            mutations[f"other-pass:{PROMOTER_DOMAIN_SPAN}"] = _changed(
                spans, child, lambda span: span.__setitem__("parentSpanId", other)
            )
    index = _first(spans, "bake.task")
    if index is not None:
        for field_name in ("run_id", "task_id", "cohort_revision"):
            mutations[f"strip:{field_name}"] = _changed(
                spans, index, lambda span, f=field_name: span["attributes"].pop(f, None)
            )
    for child in (
        "bake.source_pin",
        "bake.fan_out",
        "bake.phase",
        "insight.precompute.plan",
        PROMOTER_DOMAIN_SPAN,
    ):
        index = _first(spans, child)
        if index is not None:
            mutations[f"reparent:{child}"] = _changed(
                spans, index, lambda span: span.__setitem__("parentSpanId", "0" * 16)
            )
    requestless = {task for cohort in evidence.cohorts for task in cohort.requestless_tasks}
    index = next(
        (
            i
            for i, span in enumerate(spans)
            if span.get("name") == "bake.phase"
            and _attribute(span, "phase") == "plan"
            and _attribute(span, "task_id") in requestless
        ),
        None,
    )
    if index is not None:
        # A requestless task's one `plan` is its whole phase record: dropping it, moving it, or
        # adding a round beside it must each still fail (d2-live-20261016-r2).
        mutations["omit:requestless-plan"] = _without(spans, index)
        mutations["reparent:requestless-plan"] = _changed(
            spans, index, lambda span: span.__setitem__("parentSpanId", "0" * 16)
        )
        extra = copy.deepcopy(dict(spans[index]))
        extra["spanId"] = f"{extra.get('spanId')}-extra"
        extra["attributes"] = {
            **cast("Mapping[str, object]", extra["attributes"]),
            "phase": "query",
        }
        mutations["extra:requestless-round"] = (*spans, extra)
    index = _first(spans, "artifact.write")
    if index is not None:
        mutations["bytes:artifact.write"] = _changed(
            spans,
            index,
            lambda span: span["attributes"].__setitem__(
                "bytes", int(span["attributes"].get("bytes") or 0) + 1
            ),
        )
    index = _first(spans, "athena.query")
    if index is not None:
        mutations["statistics:athena.query"] = _changed(
            spans,
            index,
            lambda span: span["attributes"].__setitem__(
                "bytes_scanned", int(span["attributes"].get("bytes_scanned") or 0) + 1
            ),
        )
    index = _first(spans, "bake.finalize")
    if index is not None:
        mutations["nonterminal:bake.finalize"] = _changed(
            spans, index, lambda span: span.pop("endTimeUnixNano", None)
        )
    controls: dict[str, dict[str, object]] = {}
    for name, mutated in sorted(mutations.items()):
        result = trace_completeness_verdict(
            TraceCompletenessEvidence(
                evidence.cohorts, evidence.promoter_attempts, mutated, evidence.retrieval_complete
            )
        )
        controls[name] = {"rejected": result["passed"] is False, "failures": result["failures"][:5]}
    return controls


TRACE_REQUIRED_CONTROLS: Final = (
    *(f"omit:{name}" for name in (*TRACE_COHORT_CLASSES, PROMOTER_PASS_SPAN, PROMOTER_DOMAIN_SPAN)),
    "strip:run_id",
    "strip:task_id",
    "strip:cohort_revision",
    "reparent:bake.source_pin",
    "reparent:bake.fan_out",
    "reparent:bake.phase",
    "reparent:insight.precompute.plan",
    f"reparent:{PROMOTER_DOMAIN_SPAN}",
    f"other-pass:{PROMOTER_DOMAIN_SPAN}",
    f"request:{PROMOTER_PASS_SPAN}",
    "bytes:artifact.write",
    "statistics:athena.query",
    "nonterminal:bake.finalize",
)
TRACE_REQUESTLESS_CONTROLS: Final = (
    "omit:requestless-plan",
    "reparent:requestless-plan",
    "extra:requestless-round",
)
"""Required whenever some cohort expects a requestless task, so its relaxed rule is exercised."""


def trace_receipt_result(artifacts: Mapping[str, object]) -> dict[str, object]:
    """The positive trace verdict plus every paired negative control over the same live data."""
    try:
        evidence, attempt_failures = trace_evidence(artifacts)
    except (KeyError, TypeError, ValueError, ProbeError) as error:
        return {"reasons": [f"invalid:{error}"], "passed": False}
    positive = trace_completeness_verdict(evidence)
    controls = trace_negative_controls(evidence)
    # A cohort whose precompute generated nothing has no model-call span to omit, so that one
    # control is required only when some cohort records a generation.
    generated = any(cohort.model_requests for cohort in evidence.cohorts)
    required = [
        name for name in TRACE_REQUIRED_CONTROLS if generated or name != "omit:insight.model.call"
    ]
    if any(cohort.requestless_tasks for cohort in evidence.cohorts):
        required.extend(TRACE_REQUESTLESS_CONTROLS)
    missing = [name for name in required if name not in controls]
    accepted = [name for name, control in controls.items() if not control["rejected"]]
    passed = bool(positive["passed"]) and not attempt_failures and not missing and not accepted
    return {
        "positive": positive,
        "attempt_failures": attempt_failures,
        "attempt_source": "promoter-domain spans, cross-checked against every serving transition",
        "negative_controls": controls,
        "missing_controls": missing,
        "accepted_controls": accepted,
        "span_count": len(evidence.spans),
        "passed": passed,
    }


def bake_interval(artifacts: Mapping[str, object], run_id: str) -> tuple[str, str]:
    """A's `bake.run` start and its final successful `bake.finalize` end, from the retrieval."""
    retrievals = cast("Mapping[str, Mapping[str, object]]", artifacts["retrievals"])
    spans = [
        span
        for retrieval in retrievals.values()
        for span in cast("list[Mapping[str, object]]", retrieval.get("spans") or [])
        if _attribute(span, "run_id") == run_id
    ]
    runs = [span for span in spans if span.get("name") == "bake.run"]
    finals = [
        span
        for span in spans
        if span.get("name") == "bake.finalize"
        and _attribute(span, "outcome") == BAKE_FINALIZE_SUCCEEDED
        and span.get("endTimeUnixNano")
    ]
    if len(runs) != 1 or not finals:
        raise ProbeError("the bake telemetry cannot prove one bake.run and a successful finalize")
    start = _nanos(runs[0].get("startTimeUnixNano"))
    end = max(_nanos(span.get("endTimeUnixNano")) for span in finals)
    return start, end


def _nanos(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int | str) or not str(value).isdigit():
        raise ProbeError("a span instant is not an integer of nanoseconds")
    return datetime.fromtimestamp(int(value) / 1_000_000_000, tz=UTC).isoformat()


# --- Live trace collection (read-only) -------------------------------------------------------


def control_cohorts(
    control: Mapping[str, object], read: Callable[[str, str], Mapping[str, Any] | None]
) -> list[DirectCohort]:
    """Every direct cohort the control drove, with serialized chain windows from strong reads."""
    observations = cast("Mapping[str, Any]", control.get("observations") or {})
    run_ids = cast("Mapping[str, str]", control.get("run_ids") or {})
    groups: list[list[str]] = [
        list(cast("Mapping[str, object]", observations["single_root_terminal"])),
        [
            run
            for run in cast("Mapping[str, object]", observations["waves_terminal"])
            if run
            in set(
                cast(
                    "Sequence[str]", observations["a_snapshot_before"].get("elected_run_ids") or ()
                )
            )
        ],
        [
            run
            for run in cast("Mapping[str, object]", observations["waves_terminal"])
            if run
            in set(cast("Sequence[str]", observations["b_active"].get("elected_run_ids") or ()))
        ],
    ]
    if not set(run_ids.values()) >= {run for group in groups for run in group}:
        raise ProbeError("a cohort member is not one of the control's launched runs")
    cohorts: list[DirectCohort] = []
    prior_end: datetime | None = None
    for members in groups:
        runs = {run: read(f"RUN#{run}", "RUN") for run in members}
        if not members or any(item is None for item in runs.values()):
            raise ProbeError("a control cohort run is absent")
        direct = []
        for run, item in runs.items():
            grant = cast("Mapping[str, Any]", item)["grant_id"]["S"]
            binding = read(f"SERVING#{grant}", "BINDING")
            if binding is None:
                raise ProbeError(f"run {run} has no serving binding")
            if binding.get("binding_kind", {}).get("S") == "direct":
                direct.append((run, grant, binding["cohort_revision"]["S"]))
        if len(direct) != 1:
            raise ProbeError(f"a control cohort has {len(direct)} direct bindings")
        starts = [
            utc_instant(cast("Mapping[str, Any]", r)["root_clock_started_at"]["S"], "root clock")
            for r in runs.values()
        ]
        ends = [
            utc_instant(cast("Mapping[str, Any]", r)["terminal_at"]["S"], "terminal_at")
            for r in runs.values()
        ]
        start = min(starts) if prior_end is None else max(min(starts), prior_end)
        prior_end = max(ends)
        run, grant, cohort = direct[0]
        cohorts.append(DirectCohort(cohort, run, grant, start.isoformat(), prior_end.isoformat()))
    return cohorts


def _retrieve(
    logs: Any, environment: str, window: tuple[datetime, datetime], **selector: Any
) -> dict[str, object]:
    from adapters.jobs.span_retrieval import (
        LogsInsightsSpanClient,
        build_span_query,
        declared_span_attributes,
    )

    query = build_span_query(
        environment=environment,
        window_start=window[0],
        window_end=window[1],
        declared_attributes=declared_span_attributes(),
        limit=10_000,
        max_pages=64,
        **selector,
    )
    return LogsInsightsSpanClient(logs, timeout_seconds=300.0).retrieve(query)


def athena_executions(
    athena: Any, environment: str, window: tuple[datetime, datetime]
) -> list[dict[str, object]]:
    """Every BAKE and INSIGHT_PRECOMPUTE execution submitted inside the window."""
    found: list[dict[str, object]] = []
    for kind in _ATHENA_WORKGROUP_KINDS:
        workgroup = f"adlytics-{environment}-v2-{kind}"
        pages = athena.get_paginator("list_query_executions").paginate(
            WorkGroup=workgroup, PaginationConfig={"PageSize": 50}
        )
        for page in pages:
            ids = list(page.get("QueryExecutionIds", ()))
            older = False
            for start in range(0, len(ids), 50):
                batch = athena.batch_get_query_execution(QueryExecutionIds=ids[start : start + 50])
                for item in batch.get("QueryExecutions", ()):
                    submitted = item["Status"]["SubmissionDateTime"]
                    if submitted < window[0]:
                        older = True  # the list is newest first
                        continue
                    if submitted <= window[1]:
                        found.append(
                            {
                                "query_execution_id": item["QueryExecutionId"],
                                "workgroup": workgroup,
                                "state": item["Status"]["State"],
                                "submitted_at": submitted.astimezone(UTC).isoformat(),
                                "data_scanned_bytes": int(
                                    item.get("Statistics", {}).get("DataScannedInBytes", 0)
                                ),
                            }
                        )
            if older:
                break  # every later page is older still
    return sorted(found, key=lambda item: str(item["query_execution_id"]))


def task_summaries(
    logs: Any, log_group: str, window: tuple[datetime, datetime], run_ids: Iterable[str]
) -> list[dict[str, object]]:
    """The Baker's structured `task_summary` lines for the named runs, from one complete query."""
    import time

    wanted = set(run_ids)
    started = logs.start_query(
        logGroupName=log_group,
        startTime=int(window[0].timestamp()),
        endTime=int(window[1].timestamp()) + 1,
        queryString="fields @message | filter @message like /task_summary/ | limit 10000",
        limit=10_000,
    )
    for _ in range(150):
        result = logs.get_query_results(queryId=started["queryId"])
        if result.get("status") == "Complete":
            break
        if result.get("status") in {"Failed", "Cancelled", "Timeout"}:
            raise ProbeError(f"the task summary query ended {result.get('status')}")
        time.sleep(2)
    else:
        raise ProbeError("the task summary query did not complete")
    rows = result.get("results", [])
    matched = cast("Mapping[str, object]", result.get("statistics") or {}).get("recordsMatched")
    if len(rows) >= 10_000 or not isinstance(matched, int | float) or matched > len(rows):
        raise ProbeError("the task summary query may be cut; completeness unproven")
    summaries: list[dict[str, object]] = []
    for row in rows:
        for field_item in row:
            if field_item.get("field") != "@message":
                continue
            try:
                line = json.loads(str(field_item.get("value")))
            except ValueError:
                continue
            if (
                isinstance(line, dict)
                and line.get("phase") == "task_summary"
                and line.get("run_id") in wanted
            ):
                summaries.append(cast("dict[str, object]", line))
    return summaries


def slot_generations(
    logs: Any, log_group: str, window: tuple[datetime, datetime], run_ids: Iterable[str]
) -> dict[str, int]:
    """Per named run, the insight slots its precompute generated (one model call each)."""
    import time

    wanted = set(run_ids)
    started = logs.start_query(
        logGroupName=log_group,
        startTime=int(window[0].timestamp()),
        endTime=int(window[1].timestamp()) + 1,
        queryString=(
            "filter phase = 'slot' and generated = 1 | stats count(*) as n by correlation_id"
        ),
        limit=10_000,
    )
    for _ in range(150):
        result = logs.get_query_results(queryId=started["queryId"])
        if result.get("status") == "Complete":
            break
        if result.get("status") in {"Failed", "Cancelled", "Timeout"}:
            raise ProbeError(f"the slot generation query ended {result.get('status')}")
        time.sleep(2)
    else:
        raise ProbeError("the slot generation query did not complete")
    found = dict.fromkeys(wanted, 0)
    for row in result.get("results", []):
        fields = {str(item.get("field")): str(item.get("value")) for item in row}
        run = fields.get("correlation_id")
        if run in wanted:
            found[run] = int(fields.get("n") or 0)
    return found


def _function_log_group(session: Any, stack: str, prefix: str) -> str:
    from adapters.outbound.cloudformation_stack import stack_resource_summaries

    names = [
        str(item["PhysicalResourceId"])
        for item in stack_resource_summaries(session.client("cloudformation"), stack)
        if item.get("ResourceType") == "AWS::Lambda::Function"
        and str(item.get("LogicalResourceId", "")).startswith(prefix)
    ]
    if len(names) != 1:
        raise ProbeError(f"expected one {prefix} function in {stack}, found {len(names)}")
    configuration = session.client("lambda").get_function_configuration(FunctionName=names[0])
    group = cast("Mapping[str, object]", configuration.get("LoggingConfig") or {}).get("LogGroup")
    return str(group or f"/aws/lambda/{names[0]}")


def collect_trace_artifacts(
    session: Any, environment: str, graph_table: str, control: Mapping[str, object]
) -> dict[str, object]:
    """Read every trace input of one control execution; strongly consistent and read-only."""
    from adapters.jobs.preset_artifact_publisher.graph_bake import TASK_PHASES
    from adapters.outbound.dynamodb.execution_graph_release import DynamoGraphReleaseStore
    from domain.operations.execution_graph_release import CohortMember
    from domain.operations.generated_execution_graphs import BAKE_TASKS

    dynamodb = session.client("dynamodb")

    def read(pk: str, sk: str) -> Mapping[str, Any] | None:
        found = dynamodb.get_item(
            TableName=graph_table, Key={"pk": {"S": pk}, "sk": {"S": sk}}, ConsistentRead=True
        ).get("Item")
        return cast("Mapping[str, Any] | None", found)

    cohorts = control_cohorts(control, read)
    window = (
        utc_instant(control.get("started_at"), "control start") - TRACE_WINDOW_PAD,
        utc_instant(control.get("ended_at"), "control end") + TRACE_WINDOW_PAD,
    )
    logs = session.client("logs")
    retrievals = {
        cohort.cohort_revision: _retrieve(
            logs,
            environment,
            window,
            keys=(("cohort_revision", cohort.cohort_revision),),
            span_names=TRACE_COHORT_CLASSES,
        )
        for cohort in cohorts
    }
    retrievals["promoter"] = _retrieve(
        logs, environment, window, span_names=(PROMOTER_PASS_SPAN, PROMOTER_DOMAIN_SPAN)
    )
    store = DynamoGraphReleaseStore(graph_table, dynamodb)
    rosters = {
        cohort.grant_id: [
            list(entry)
            for item in store.read_contributions(cohort.grant_id)
            if item.member is CohortMember.PRECOMPUTE
            for entry in item.request_roster
        ]
        for cohort in cohorts
    }
    from domain.operations.execution_graph_release import INGESTION_RELEASE_DOMAIN

    transitions = []
    for cohort in cohorts:
        item = read(
            f"SERVING_TRANSITION#{INGESTION_RELEASE_DOMAIN}", f"COHORT#{cohort.cohort_revision}"
        )
        if item is not None:
            transitions.append(
                {
                    "domain": INGESTION_RELEASE_DOMAIN,
                    "drained_cohort_revision": cohort.cohort_revision,
                    "aws_request_id": item.get("aws_request_id", {}).get("S"),
                }
            )
    baker_group = _function_log_group(session, f"Adlytics-Publisher-{environment}", "Baker")
    return {
        "schema_version": TRACE_ARTIFACTS_SCHEMA,
        "acceptance_execution_id": control.get("acceptance_execution_id"),
        "window": [window[0].isoformat(), window[1].isoformat()],
        "cohorts": [
            {
                "cohort_revision": c.cohort_revision,
                "run_id": c.run_id,
                "grant_id": c.grant_id,
                "window_start": c.window_start,
                "window_end": c.window_end,
            }
            for c in cohorts
        ],
        "roster": sorted(f"{row['page']}#{row['variant']}" for row in BAKE_TASKS["roster"]),
        "phases": list(TASK_PHASES),
        "retrievals": retrievals,
        "athena_executions": athena_executions(session.client("athena"), environment, window),
        "request_rosters": rosters,
        "task_summaries": task_summaries(logs, baker_group, window, [c.run_id for c in cohorts]),
        "slot_generations": slot_generations(
            logs,
            _function_log_group(session, f"Adlytics-InsightPrecompute-{environment}", "Precompute"),
            window,
            [c.run_id for c in cohorts],
        ),
        "transitions": transitions,
    }


# --- Athena admission ------------------------------------------------------------------------


def env_file_value(text: str) -> tuple[str, int | None]:
    """The explicit `.env` value: `("explicit_env", value)`, or `("absent", None)`."""
    values = [
        line.split("=", 1)[1].strip()
        for line in text.splitlines()
        if line.strip().startswith(f"{ENV_VARIABLE}=")
    ]
    if len(values) != 1:
        return "absent", None
    value = values[0]
    return "explicit_env", int(value) if re.fullmatch(r"[1-9]\d*", value) else None


def _functions(template: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    resources = cast("Mapping[str, Mapping[str, Any]]", template.get("Resources") or {})
    return {
        logical: cast("Mapping[str, Any]", resource.get("Properties") or {})
        for logical, resource in resources.items()
        if resource.get("Type") == "AWS::Lambda::Function"
    }


def _variable(properties: Mapping[str, Any]) -> object:
    environment = cast("Mapping[str, Any]", properties.get("Environment") or {})
    return cast("Mapping[str, object]", environment.get("Variables") or {}).get(
        GENERAL_BATCH_VARIABLE
    )


def _stack_kind(stack: str) -> str:
    return stack.removeprefix("Adlytics-").rsplit("-", 1)[0]


def general_values(templates: Mapping[str, Mapping[str, Any]]) -> dict[str, object]:
    """Every general-batch value a consuming stack's function carries, by `stack/logical`."""
    found: dict[str, object] = {}
    for stack, template in sorted(templates.items()):
        if _stack_kind(stack) not in GENERAL_BATCH_STACKS:
            continue
        for logical, properties in sorted(_functions(template).items()):
            value = _variable(properties)
            if value is not None:
                found[f"{stack}/{logical}"] = int(value) if str(value).isdigit() else value
    return found


def static_rows(
    templates: Mapping[str, Mapping[str, Any]], operator_processes: int
) -> dict[str, object]:
    """The seven caller rows: reserved concurrency x start cap, from the same templates."""
    rows: dict[str, object] = dict.fromkeys(_OPERATOR_ROWS, operator_processes)
    by_kind = {_stack_kind(stack): template for stack, template in templates.items()}
    for row, (kind, prefix) in _STATIC_FUNCTIONS.items():
        candidates = [
            properties
            for logical, properties in _functions(by_kind.get(kind) or {}).items()
            if logical.startswith(prefix) and (prefix or _variable(properties) is not None)
        ]
        if len(candidates) != 1:
            rows[row] = None
            continue
        properties = candidates[0]
        reserved = properties.get("ReservedConcurrentExecutions")
        cap = _variable(properties) if row in {"baker", "insight_precompute"} else 1
        rows[row] = (
            int(reserved) * int(cast("int", cap))
            if isinstance(reserved, int) and str(cap).isdigit()
            else None
        )
    return dict(sorted(rows.items()))


def operator_processes(worktree: Path) -> int:
    """The committed operator row constant (`OPERATOR_PROCESSES_PER_ROW`)."""
    text = (worktree / "infra" / "lib" / "config" / "athena-start-budget.ts").read_text(
        encoding="utf-8"
    )
    match = _OPERATOR_CONSTANT.search(text)
    if match is None:
        raise ProbeError("the operator row constant is unreadable")
    return int(match.group(1))


def admission_receipt(
    *,
    env_text: str,
    synthesized: Mapping[str, Mapping[str, Any]],
    deployed: Mapping[str, Mapping[str, Any]],
    readback: Mapping[str, object],
    operators: int,
    code_sha: str,
    environment: str,
    acceptance_execution_id: str,
) -> dict[str, object]:
    """Assemble the receipt `athena_admission_config_verdict` reads.

    Its `collection` block names the execution and one fresh collection ID, so a retained
    receipt can be reused only by the execution, code SHA and environment that collected it.
    """
    from domain.operations.generated_execution_graphs import ATHENA_START_BUDGET

    source, configured = env_file_value(env_text)
    synth_rows = static_rows(synthesized, operators)
    deployed_rows = static_rows(deployed, operators)
    return {
        "schema_version": ADMISSION_SCHEMA,
        "collection": {
            "acceptance_execution_id": acceptance_execution_id,
            "collection_id": uuid.uuid4().hex,
            "collected_at": datetime.now(UTC).isoformat(),
        },
        "source": source,
        "configured": configured,
        "synthesized": general_values(synthesized),
        "deployed": general_values(deployed),
        "readback": dict(readback),
        "static_rows": deployed_rows
        if deployed_rows == synth_rows
        else {"synth": synth_rows, "deployed": deployed_rows},
        "active_dml_quota": ATHENA_START_BUDGET["active_dml_quota"],
        "chat_reserve": ATHENA_START_BUDGET["chat_reserve_active_dml"],
        "code_sha": code_sha,
        "environment": environment,
    }


def admission_binding_reasons(
    receipt: Mapping[str, object], *, execution_id: str, code_sha: str, environment: str
) -> list[str]:
    """Why a retained admission receipt is not this execution's own collection."""
    reasons: list[str] = []
    if receipt.get("schema_version") != ADMISSION_SCHEMA:
        reasons.append("admission_schema")
    collection = receipt.get("collection")
    typed: Mapping[str, object] = (
        cast("Mapping[str, object]", collection) if isinstance(collection, Mapping) else {}
    )
    if typed.get("acceptance_execution_id") != execution_id:
        reasons.append("execution_mismatch:admission_receipt")
    identity = typed.get("collection_id")
    if not isinstance(identity, str) or not identity:
        reasons.append("admission_collection_unidentified")
    if receipt.get("code_sha") != code_sha:
        reasons.append("admission_code_sha_mismatch")
    if receipt.get("environment") != environment:
        reasons.append("admission_environment_mismatch")
    return reasons


def deployed_templates(session: Any, environment: str) -> dict[str, Mapping[str, Any]]:
    """The deployed (CloudFormation) templates of every stack a row or the value lives in."""
    cloudformation = session.client("cloudformation")
    kinds = sorted({*GENERAL_BATCH_STACKS, *(kind for kind, _ in _STATIC_FUNCTIONS.values())})
    templates: dict[str, Mapping[str, Any]] = {}
    for kind in kinds:
        stack = f"Adlytics-{kind}-{environment}"
        body = cloudformation.get_template(StackName=stack, TemplateStage="Original")[
            "TemplateBody"
        ]
        templates[stack] = cast(
            "Mapping[str, Any]", json.loads(body) if isinstance(body, str) else body
        )
    return templates


def lambda_readback(session: Any, environment: str) -> dict[str, object]:
    """Every consuming function's live value, read from its Lambda configuration."""
    from adapters.outbound.cloudformation_stack import stack_resource_summaries

    values: dict[str, object] = {}
    lambdas = session.client("lambda")
    for kind in GENERAL_BATCH_STACKS:
        stack = f"Adlytics-{kind}-{environment}"
        for item in stack_resource_summaries(session.client("cloudformation"), stack):
            if item.get("ResourceType") != "AWS::Lambda::Function":
                continue
            configuration = lambdas.get_function_configuration(
                FunctionName=item["PhysicalResourceId"]
            )
            value = _variable(configuration)
            if value is not None:
                values[f"{stack}/{item['LogicalResourceId']}"] = (
                    int(value) if str(value).isdigit() else value
                )
    return values


def synthesized_templates(cdk_out: Path, environment: str) -> dict[str, Mapping[str, Any]]:
    """The synthesized templates of the same stacks, from one `make synth` output directory."""
    templates: dict[str, Mapping[str, Any]] = {}
    kinds = sorted({*GENERAL_BATCH_STACKS, *(kind for kind, _ in _STATIC_FUNCTIONS.values())})
    for kind in kinds:
        path = cdk_out / f"Adlytics-{kind}-{environment}.template.json"
        templates[path.name.removesuffix(".template.json")] = read_json(
            path, "synthesized template"
        )
    return templates


# --- Hold-clear matrix -----------------------------------------------------------------------


def matrix_manifest(
    execution_id: str, pairs: Sequence[str], operator_rows: Iterable[str]
) -> dict[str, object]:
    """The run-generated matrix manifest: each `HIST-<id>=<path>` source with its digest now.

    `run_graph_acceptance.py` writes it once, after every evidence-producing step and before the
    verdicts. A source not yet written (a receipt the verdict mode itself produces) records no
    digest, so it can bind only by its own execution ID. Operator-named rows are marked.
    """
    operator = set(operator_rows)
    entries: dict[str, dict[str, object]] = {}
    for criterion, path in parse_matrix_sources(pairs).items():
        entries[criterion] = {
            "path": str(path.resolve()),
            "sha256": file_sha256(path) if path.is_file() else None,
            "origin": "operator" if criterion in operator else "run",
        }
    return {
        "schema_version": MATRIX_MANIFEST_SCHEMA,
        "acceptance_execution_id": execution_id,
        "entries": entries,
        "written_at": datetime.now(UTC).isoformat(),
    }


def load_matrix_manifest(
    evidence_dir: Path, execution_id: str
) -> tuple[dict[str, Mapping[str, object]], str]:
    """This execution's manifest entries and why they are usable; any other manifest binds none."""
    path = evidence_dir / MATRIX_MANIFEST
    if not path.is_file():
        return {}, "absent"
    document = read_json(path, "matrix manifest")
    if document.get("schema_version") != MATRIX_MANIFEST_SCHEMA:
        return {}, "wrong_schema"
    if document.get("acceptance_execution_id") != execution_id:
        return {}, "other_execution"
    entries = document.get("entries")
    if not isinstance(entries, Mapping):
        return {}, "malformed"
    return dict(cast("Mapping[str, Mapping[str, object]]", entries)), "bound"


def matrix_evidence(
    matrix: Mapping[str, object],
    evidence_dir: Path,
    execution_id: str,
    sources: Mapping[str, Path],
    manifest: Mapping[str, Mapping[str, object]],
) -> tuple[dict[str, dict[str, object]], dict[str, Path]]:
    """One evidence entry per retained/replaced row, bound to this execution, plus its inputs.

    A row reads its command's receipt in this execution's directory, or the explicit source the
    operator names for it. The execution is bound by the document's own execution ID when it
    carries one (which must match). A document naming no execution binds only through this
    execution's run-generated manifest: same resolved path, same digest, and inside this
    execution's directory. Location alone never binds; anything else is another execution's.
    """
    entries: dict[str, dict[str, object]] = {}
    inputs: dict[str, Path] = {}
    root = evidence_dir.resolve()
    for row in cast("list[Mapping[str, object]]", matrix.get("rows") or []):
        criterion, status = str(row.get("criterion_id")), row.get("status")
        if status == "retired":
            continue
        source = sources.get(criterion)
        if source is None:
            name = MATRIX_COMMAND_RECEIPTS.get(str(row.get("command")))
            source = None if name is None else evidence_dir / name
        if source is None or not source.is_file():
            continue  # the verdict reports `evidence_absent`
        document = read_json(source, criterion)
        named = document.get("acceptance_execution_id") or document.get("execution_id")
        digest = file_sha256(source)
        if named is not None:
            bound = named == execution_id
        else:
            recorded = manifest.get(criterion)
            bound = (
                recorded is not None
                and recorded.get("path") == str(source.resolve())
                and recorded.get("sha256") == digest
                and root in source.resolve().parents
            )
        entries[criterion] = {
            "execution_id": execution_id if bound else (named or "unbound"),
            "passed": document.get("passed") is True,
            "source": str(source),
            "sha256": digest,
            "binding": "document" if named is not None else "manifest",
        }
        if "out_of_graph_scope" in document:
            # Recorded beside the row's verdict, so an owned alarm is named, never folded away.
            entries[criterion]["out_of_graph_scope"] = document["out_of_graph_scope"]
        inputs[criterion] = source
    return entries, inputs


# --- The mode --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerdictInputs:
    """The retained inputs of one `--acceptance-verdicts` run."""

    execution_id: str
    evidence_dir: Path
    accepted_generation: str
    env_file: Path
    cdk_out: Path
    worktree: Path
    matrix_sources: Mapping[str, Path]
    code_sha: str
    environment: str


def used_execution_ids(evidence_root: Path, current: Path) -> frozenset[str]:
    """Execution IDs of every earlier accepted paired-chat receipt (a reuse is refused)."""
    used: set[str] = set()
    for path in evidence_root.rglob(RECEIPTS["paired_bake_chat_verdict"]):
        if path.parent.resolve() == current.resolve():
            continue
        try:
            document = read_json(path, "paired chat receipt")
        except ProbeError:
            continue
        if document.get("passed") is True:
            used.add(str(document.get("acceptance_execution_id")))
    return frozenset(used)


def outside_selection(
    path: Path, *, execution_id: str, environment: str, generation: str
) -> tuple[dict[str, Any], list[str]]:
    """Return the retained selection receipt's item, digest and query ID, re-validated.

    The expected outside-list item comes only from here, never from a sample being judged. An
    absent, unreadable or failing receipt yields no item and a binding failure.
    """
    from probe_turn_latency import SampleError, load_outside_selection

    if not path.is_file():
        return {}, ["outside_selection_absent"]
    try:
        return (
            load_outside_selection(
                path, execution_id=execution_id, environment=environment, generation=generation
            ),
            [],
        )
    except (OSError, SampleError, TypeError, ValueError) as error:
        return {}, [f"outside_selection_invalid:{error}"]


def selection_binding_reasons(
    outside: Mapping[str, Any], sample: Mapping[str, object], what: str
) -> list[str]:
    """List why a sample is not bound to the retained receipt: digest, query, item, unit, prompt."""
    from probe_turn_latency import filled_prompt_sha256, load_chat_manifest, with_outside_list_item

    if not outside:
        return []  # the receipt's own failure is already the reason
    expected = {
        "outside_selection_sha256": outside["outside_selection_sha256"],
        "outside_selection_query_execution_id": outside["outside_selection_query_execution_id"],
        "outside_list_item": outside["outside_list_item"],
        "generation_unit": outside["target_unit"],
        "prompt_filled_sha256": filled_prompt_sha256(
            with_outside_list_item(
                load_chat_manifest(CHAT_PROMPT_MANIFEST), outside["outside_list_item"]
            )
        ),
    }
    return [
        f"{what}_{name}_mismatch" for name, value in expected.items() if sample.get(name) != value
    ]


def target_unit_reasons(
    outside: Mapping[str, Any], control: Mapping[str, object], accepted_generation: str
) -> list[str]:
    """List why the chat target unit is planned by the control or serves another generation.

    Each control-planned key's conflict unit is derived with the runtime's `window_identity`; the
    target must be none of them. The receipt's selection-time generation must be the accepted.
    """
    from domain.operations.ingestion_root import RootRefusedError, window_identity

    if not outside:
        return []
    reasons: list[str] = []
    if outside["target_generation"] != accepted_generation:
        reasons.append("target_generation_not_accepted")
    planned = control.get("planned")
    if not isinstance(planned, list) or not planned:
        return [*reasons, "control_planned_keys_absent"]
    units: set[str] = set()
    for item in cast("list[object]", planned):
        key = cast("Mapping[str, object]", item).get("key") if isinstance(item, Mapping) else None
        try:
            units.add(window_identity(str(key)).conflict_unit)
        except RootRefusedError:
            reasons.append("control_planned_key_malformed")
    if outside["target_unit"] in units:
        reasons.append("target_unit_planned_by_control")
    return reasons


def selection_readback(
    path: Path,
    *,
    outside: Mapping[str, Any],
    receipt_path: Path,
    baseline: Mapping[str, object],
    collect: Callable[[str], Mapping[str, object]],
) -> list[str]:
    """Re-read the receipt's Athena query once (retained), and say why it does not prove it."""
    from probe_turn_latency import selection_readback_reasons

    if not outside:
        return []
    query = outside["outside_selection_query_execution_id"]
    if not path.exists():
        try:
            collected = collect(query)
        except Exception as error:  # noqa: BLE001 -- a failed re-read proves nothing, named apart
            return [f"readback_unreadable:{type(error).__name__}"]
        write_json_atomic(path, {**collected, "acceptance_readback_of": query})
    readback = read_json(path, "selection readback")
    return selection_readback_reasons(
        read_json(receipt_path, "selection receipt"),
        readback,
        preflight_completed_at=baseline.get("preflight_completed_at"),
        baseline_started_at=baseline.get("sample_started_at"),
    )


def run_acceptance_verdicts(
    inputs: VerdictInputs,
    *,
    collect_trace: Callable[[Mapping[str, object]], Mapping[str, object]],
    collect_admission: Callable[[], Mapping[str, object]],
    collect_selection: Callable[[str], Mapping[str, object]],
    evidence_root: Path,
) -> dict[str, Path]:
    """Gather and write every verdict receipt; return each receipt's path.

    Live reads are injected (`collect_trace`, `collect_admission`, `collect_selection`, the
    selection query's Athena re-read), and each one's output is
    retained before its verdict reads it. A retained input (collected now or found already in
    the directory) is used only if it is bound to this execution; an unbound input makes its
    verdict `NOT_PROVEN`. The matrix runs last over the receipts just written.
    """
    directory = inputs.evidence_dir
    execution = inputs.execution_id
    control_path = directory / CONTROL_RECEIPT
    control = read_json(control_path, "rolling-freshness control")
    if control.get("acceptance_execution_id") != execution:
        raise ProbeError("the control receipt names another acceptance execution")
    written: dict[str, Path] = {}
    trace_path = directory / TRACE_ARTIFACTS
    if not trace_path.exists():
        write_json_atomic(trace_path, collect_trace(control))
    artifacts = read_json(trace_path, "trace")
    trace_binding = execution_reasons(artifacts, execution, "trace_artifacts")
    written["trace_completeness_verdict"] = write_json_atomic(
        directory / RECEIPTS["trace_completeness_verdict"],
        receipt_document(
            "trace_completeness_verdict",
            execution,
            bound_result(trace_receipt_result(artifacts), trace_binding),
            {"control": control_path, "trace_artifacts": trace_path},
        ),
    )
    baseline_path, during_path = directory / CHAT_BASELINE, directory / CHAT_DURING
    selection_path = directory / OUTSIDE_SELECTION
    chat_binding = list(trace_binding)
    try:
        outside, selection_reasons = outside_selection(
            selection_path,
            execution_id=execution,
            environment=inputs.environment,
            generation=inputs.accepted_generation,
        )
        chat_binding += selection_reasons
        winner = str(
            cast(
                "Mapping[str, object]",
                cast("Mapping[str, object]", control["observations"])["a_snapshot_before"],
            )["winner_run_id"]
        )
        interval = bake_interval(artifacts, winner)
        baseline = read_json(baseline_path, "paired no-bake chat")
        during = read_json(during_path, "during-bake chat")
        chat_binding += execution_reasons(baseline, execution, "baseline")
        chat_binding += execution_reasons(during, execution, "during_bake")
        chat_binding += selection_binding_reasons(outside, baseline, "baseline")
        chat_binding += selection_binding_reasons(outside, during, "during_bake")
        chat_binding += target_unit_reasons(outside, control, inputs.accepted_generation)
        chat_binding += selection_readback(
            directory / SELECTION_READBACK,
            outside=outside,
            receipt_path=selection_path,
            baseline=baseline,
            collect=collect_selection,
        )
        chat = paired_bake_chat_verdict(
            baseline,
            during,
            inputs.accepted_generation,
            outside.get("outside_list_item", ""),
            interval,
            used_execution_ids=used_execution_ids(evidence_root, directory),
        )
        chat = {**chat, "bake_interval": list(interval), "target_bake_run_id": winner}
    except (KeyError, ProbeError) as error:
        chat = {"reasons": [f"invalid:{error}"], "passed": False, "disposition": "NOT_PROVEN"}
    written["paired_bake_chat_verdict"] = write_json_atomic(
        directory / RECEIPTS["paired_bake_chat_verdict"],
        receipt_document(
            "paired_bake_chat_verdict",
            execution,
            bound_result(chat, chat_binding),
            {
                name: path
                for name, path in (
                    ("control", control_path),
                    ("trace_artifacts", trace_path),
                    ("baseline", baseline_path),
                    ("during_bake", during_path),
                    ("outside_selection", selection_path),
                    ("outside_selection_readback", directory / SELECTION_READBACK),
                )
                if path.is_file()
            },
        ),
    )
    admission_path = directory / ATHENA_ADMISSION
    if not admission_path.exists():
        write_json_atomic(admission_path, collect_admission())
    admission = read_json(admission_path, "Athena admission")
    written["athena_admission_config_verdict"] = write_json_atomic(
        directory / RECEIPTS["athena_admission_config_verdict"],
        receipt_document(
            "athena_admission_config_verdict",
            execution,
            bound_result(
                athena_admission_config_verdict(admission),
                admission_binding_reasons(
                    admission,
                    execution_id=execution,
                    code_sha=inputs.code_sha,
                    environment=inputs.environment,
                ),
            ),
            {"admission_receipt": admission_path},
        ),
    )
    matrix = load_hold_clear_matrix()
    manifest, manifest_status = load_matrix_manifest(directory, execution)
    entries, sources = matrix_evidence(
        matrix, directory, execution, inputs.matrix_sources, manifest
    )
    entries_path = write_json_atomic(
        directory / "hold-clear-matrix-evidence.json",
        {"acceptance_execution_id": execution, "entries": entries},
    )
    manifest_path = directory / MATRIX_MANIFEST
    written["hold_clear_matrix_verdict"] = write_json_atomic(
        directory / RECEIPTS["hold_clear_matrix_verdict"],
        receipt_document(
            "hold_clear_matrix_verdict",
            execution,
            {
                **hold_clear_matrix_verdict(matrix, entries, execution_id=execution),
                "matrix_sha256": hashlib.sha256(canonical_json_bytes(matrix)).hexdigest(),
                "manifest_status": manifest_status,
            },
            {
                "evidence_map": entries_path,
                **({"manifest": manifest_path} if manifest_path.is_file() else {}),
                **sources,
            },
        ),
    )
    return written


def parse_matrix_sources(values: Sequence[str]) -> dict[str, Path]:
    """`HIST-<id>=<path>` pairs; a malformed or repeated pair is refused."""
    sources: dict[str, Path] = {}
    for value in values:
        criterion, separator, path = value.partition("=")
        if not separator or not criterion.startswith("HIST-") or not path or criterion in sources:
            raise ProbeError(f"--matrix-evidence {value!r} must be one HIST-<id>=<path>")
        sources[criterion] = Path(path)
    return sources
