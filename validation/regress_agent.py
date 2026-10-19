# SPDX-License-Identifier: MIT-0
"""Agent-mode regression: run a versioned fictional question set and record expected vs actual.

ADR-079 §5 and ADR-082: each question runs through the deployed dev conversation route and the same
attested chain the acceptance probe proves, and the released evaluators' verdicts are compared with
the labels the set allows per dimension. The report is content-free apart from the set's own
fictional names, and a label mismatch never fails the run: the model is nondeterministic, so the
owner reads the report, optionally against a baseline report from before the change.

A run needs every question scored: it refuses to start unless a family is enabled and each enabled
configuration samples 100% of sessions, and it fails when any question was not scored.

An `AgentRegressionSet.v2` question may carry a prime: a turn in its own earlier conversation that
seeds a figure into the persona's long-term Memory, so the question measures whether the agent
restates a remembered figure (ADR-147). The run then goes in six stages: fixture preconditions,
unprimed questions, primes, a settle wait for each prime's memory record, primed questions, and
teardown of the prime markers. Matching a fictional marker in memory records is harness readiness
and cleanup; it never reads or gates an answer (ADR-082).
"""

from __future__ import annotations

import json
import re
import secrets
import sys
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from botocore.exceptions import BotoCoreError, ClientError
from cognito_personas import published_parameter, unverified_claims
from memory_codenames import (
    CODENAME,
    cross_session_namespaces,
    semantic_namespace,
    settle_codenames,
    sweep_codenames,
)
from probe_agent_quality_loop import (
    ProbeStageError,
    ProbeTarget,
    Scenario,
    prepare_attested,
    probe_parser,
    run_scenario_marked,
)
from run_d2_chat_acceptance import ChatAthena

from adapters.inbound.conversation.turns import actor_id_for
from adapters.outbound.agentcore.online_evaluation_control import (
    AgentCoreOnlineEvaluationControl,
)
from domain.evaluation.linkage import normalize_scale_label
from domain.shared_kernel.errors import AdlyticsError

if TYPE_CHECKING:
    from types_boto3_bedrock_agentcore import BedrockAgentCoreClient

_ROOT = Path(__file__).resolve().parents[1]
_SET_SCHEMA = "AgentRegressionSet.v1"
_SET_SCHEMA_V2 = "AgentRegressionSet.v2"
_REPORT_SCHEMA = "AgentRegressionReport.v1"
_DEFAULT_SET = "validation/agent-regression-v2.json"
_REPORT_NAME = "agent-regression-report.json"
_MATCHED = "matched"
_MISMATCHED = "mismatched"
_MISSING = "missing"
_AMBIGUOUS = "ambiguous"
_FULL_SAMPLING = 100.0
_SET_KEYS = frozenset({"schema_version", "set_version", "description", "questions"})
_QUESTION_KEYS = frozenset({"name", "intent", "question", "expected"})
_PRIMED_KEYS = frozenset({"prime", "precondition"})
_PRIME_KEYS = frozenset({"question", "expected"})
_PRECONDITION_KEYS = frozenset({"fact", "time_grain", "period_start"})
# Closed allowlists: nothing but these literals and an ISO date reaches the precondition SQL.
_PRECONDITION_FACTS = frozenset({"fct_exposure_group_outcome", "fct_new_to_brand_buyers"})
_PRECONDITION_GRAINS = frozenset({"week", "month"})
_MARKER_PLACEHOLDER = "{marker}"
PRIME_MARKER = re.compile(r"REGRESSPRIME-[0-9a-f]{6}", re.IGNORECASE)
_FIXTURE_DRIFT = "fixture_drift"
_PRIME_NOT_SEEDED = "prime_not_seeded"
_MEMORY_SETTLE = "memory_settle"
_PAGE_LIMIT = 20
# A poll that read `_PAGE_LIMIT` pages and still held a `nextToken` left records unread.
_PAGE_BUDGET_EXHAUSTED = "PageBudgetExhausted"
# A semantic record appeared 72 s after its turn on 2026-10-18; the bound leaves room for a
# slower extraction without letting an unseeded prime stall the run.
_SETTLE_BOUND_SECONDS = 600
_SETTLE_POLL_SECONDS = 30
# Absorbs a local clock running ahead of the service's createdAt.
_CLOCK_SKEW = timedelta(seconds=60)


@dataclass(frozen=True, slots=True)
class RegressionPrime:
    """A turn in its own earlier conversation; `{marker}` becomes a fresh marker per run."""

    question: str
    expected: dict[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class FixturePrecondition:
    """The fixture period a primed question needs still unmeasured: no rows at this grain."""

    fact: str
    time_grain: str
    period_start: date


@dataclass(frozen=True, slots=True)
class RegressionQuestion:
    """One fictional question and the normalized labels each dimension may return."""

    name: str
    question: str
    expected: dict[str, tuple[str, ...]]
    prime: RegressionPrime | None = None
    precondition: FixturePrecondition | None = None

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.question.strip() or not self.expected:
            raise ValueError("a regression question needs a name, a question, and expectations.")
        if any(not labels for labels in self.expected.values()):
            raise ValueError(f"regression question {self.name} expects no label for a dimension.")


@dataclass(frozen=True, slots=True)
class RegressionSet:
    """A versioned question set; a new or edited question is a new set version."""

    set_version: int
    questions: tuple[RegressionQuestion, ...]


def load_question_set(text: str) -> RegressionSet:
    """Decode an `AgentRegressionSet.v1` or `.v2` document, refusing unknown members."""
    document: object = json.loads(text)
    if not isinstance(document, dict):
        raise ValueError(f"question set must be {_SET_SCHEMA} or {_SET_SCHEMA_V2}.")
    raw = cast("dict[str, object]", document)
    schema = raw.get("schema_version")
    if schema not in {_SET_SCHEMA, _SET_SCHEMA_V2} or not set(raw) <= _SET_KEYS:
        raise ValueError(
            f"question set must be {_SET_SCHEMA} or {_SET_SCHEMA_V2} with no unknown members."
        )
    version = raw.get("set_version")
    items = raw.get("questions")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("question set needs a positive integer set_version.")
    if not isinstance(items, list) or not items:
        raise ValueError("question set needs at least one question.")
    primed = schema == _SET_SCHEMA_V2
    questions = tuple(_question(item, primed=primed) for item in cast("list[object]", items))
    names = [question.name for question in questions]
    if len(set(names)) != len(names):
        raise ValueError("question names must be unique.")
    return RegressionSet(set_version=version, questions=questions)


def _question(item: object, *, primed: bool) -> RegressionQuestion:
    if not isinstance(item, dict):
        raise ValueError("each question must be an object.")
    raw = cast("dict[str, object]", item)
    if not primed and set(raw) & _PRIMED_KEYS:
        raise ValueError(f"only an {_SET_SCHEMA_V2} question may carry a prime or precondition.")
    if not set(raw) <= (_QUESTION_KEYS | _PRIMED_KEYS if primed else _QUESTION_KEYS):
        raise ValueError("a question carries an unknown member.")
    name, question, expected = raw.get("name"), raw.get("question"), raw.get("expected")
    if not isinstance(name, str) or not isinstance(question, str) or not isinstance(expected, dict):
        raise ValueError("a question needs a name, a question, and an expected object.")
    prime = _prime(name, raw["prime"]) if "prime" in raw else None
    precondition = _precondition(name, raw["precondition"]) if "precondition" in raw else None
    if prime is not None and precondition is None:
        raise ValueError(f"question {name} carries a prime without a precondition.")
    return RegressionQuestion(
        name=name,
        question=question,
        expected=_labels(name, expected),
        prime=prime,
        precondition=precondition,
    )


def _labels(name: str, expected: object) -> dict[str, tuple[str, ...]]:
    labels: dict[str, tuple[str, ...]] = {}
    for dimension, value in cast("dict[str, object]", expected).items():
        values = cast("list[object]", value) if isinstance(value, list) else None
        if values is None or not all(isinstance(item, str) for item in values):
            raise ValueError(f"question {name} dimension {dimension} must list labels.")
        labels[dimension] = tuple(normalize_scale_label(cast("str", item)) for item in values)
    return labels


def _prime(name: str, item: object) -> RegressionPrime:
    if not isinstance(item, dict):
        raise ValueError(f"question {name} prime must be an object.")
    raw = cast("dict[str, object]", item)
    if not set(raw) <= _PRIME_KEYS:
        raise ValueError(f"question {name} prime carries an unknown member.")
    question, expected = raw.get("question"), raw.get("expected")
    if not isinstance(question, str) or not isinstance(expected, dict) or not expected:
        raise ValueError(f"question {name} prime needs a question and an expected object.")
    if question.count(_MARKER_PLACEHOLDER) != 1:
        raise ValueError(f"question {name} prime must hold {_MARKER_PLACEHOLDER} exactly once.")
    labels = _labels(name, expected)
    if any(not values for values in labels.values()):
        raise ValueError(f"question {name} prime expects no label for a dimension.")
    return RegressionPrime(question=question, expected=labels)


def _precondition(name: str, item: object) -> FixturePrecondition:
    if not isinstance(item, dict):
        raise ValueError(f"question {name} precondition must be an object.")
    raw = cast("dict[str, object]", item)
    if not set(raw) <= _PRECONDITION_KEYS:
        raise ValueError(f"question {name} precondition carries an unknown member.")
    fact, grain, start = raw.get("fact"), raw.get("time_grain"), raw.get("period_start")
    if fact not in _PRECONDITION_FACTS:
        raise ValueError(f"question {name} precondition fact is outside the allowlist.")
    if grain not in _PRECONDITION_GRAINS:
        raise ValueError(f"question {name} precondition time_grain must be week or month.")
    try:
        period_start = date.fromisoformat(cast("str", start))
    except TypeError, ValueError:
        raise ValueError(
            f"question {name} precondition period_start must be an ISO date."
        ) from None
    return FixturePrecondition(
        fact=cast("str", fact), time_grain=cast("str", grain), period_start=period_start
    )


def dimension_labels(
    verdict_labels: Mapping[str, str | None], environment: str, dimensions: tuple[str, ...]
) -> dict[str, str | None]:
    """Map each evaluator's label to its dimension by the name's `adlytics_<env>_<dimension>_`.

    Two verdicts for one dimension (two enabled families scoring it) map to `_AMBIGUOUS`, never
    to whichever arrived last.
    """
    prefix = f"adlytics_{environment.replace('-', '_')}_"
    mapped: dict[str, str | None] = {}
    for evaluator_name, label in verdict_labels.items():
        for dimension in dimensions:
            if evaluator_name.startswith(f"{prefix}{dimension}_"):
                mapped[dimension] = _AMBIGUOUS if dimension in mapped else label
    return mapped


def question_outcome(
    question: RegressionQuestion, observed: Mapping[str, str | None]
) -> dict[str, object]:
    """Compare one question's observed labels with its expectations, dimension by dimension."""
    dimensions: dict[str, dict[str, object]] = {}
    for dimension, allowed in question.expected.items():
        label = observed.get(dimension)
        if label is None or label == _AMBIGUOUS:
            state = _MISSING if label is None else _AMBIGUOUS
        else:
            state = _MATCHED if label in allowed else _MISMATCHED
        dimensions[dimension] = {"observed": label, "expected": list(allowed), "state": state}
    matched = all(item["state"] == _MATCHED for item in dimensions.values())
    return {"name": question.name, "matched": matched, "dimensions": dimensions}


def compare(baseline: dict[str, object], current: dict[str, object]) -> dict[str, object]:
    """List the questions that matched in the baseline and not now, and those that improved.

    Reports of different set versions are not comparable: an edited question is a new version.
    Primes never enter the comparison. When the current report ran primes, `end_to_end` lists
    each primed question as `fixed` only when the baseline scored it with an unsupported grounded
    correctness and this run scored and matched it; anything else is `pending` with its reason.
    """
    if baseline.get("set_version") != current.get("set_version"):
        raise ValueError("a baseline must come from the same question set version.")
    before = _matched_by_name(baseline)
    after = _matched_by_name(current)
    shared = sorted(set(before) & set(after))
    result: dict[str, object] = {
        "regressed": [name for name in shared if before[name] and not after[name]],
        "improved": [name for name in shared if not before[name] and after[name]],
        "not_compared": sorted(set(before) ^ set(after)),
    }
    if "primes" in current:
        result["end_to_end"] = _end_to_end(baseline, current)
    return result


def _end_to_end(baseline: dict[str, object], current: dict[str, object]) -> dict[str, object]:
    primes = cast("list[dict[str, object]]", current["primes"])
    names = list(dict.fromkeys(str(item["question"]) for item in primes))
    before = _outcomes_by_name(baseline)
    after = _outcomes_by_name(current)
    before_unscored = set(cast("list[str]", baseline.get("unscored", [])))
    after_unscored = set(cast("list[str]", current.get("unscored", [])))
    fixed: list[str] = []
    pending: list[dict[str, str]] = []
    for name in names:
        if name not in before or name in before_unscored:
            reason = "baseline_unscored"
        elif _grounded_state(before[name]) != _MISMATCHED:
            reason = "baseline_not_mismatched"
        elif name not in after or name in after_unscored:
            reason = "current_unscored"
        elif not after[name].get("matched"):
            reason = "current_not_matched"
        else:
            fixed.append(name)
            continue
        pending.append({"name": name, "reason": reason})
    return {"fixed": fixed, "pending": pending}


def _outcomes_by_name(report: dict[str, object]) -> dict[str, dict[str, object]]:
    outcomes = cast("list[dict[str, object]]", report.get("questions", []))
    return {str(item["name"]): item for item in outcomes}


def _grounded_state(outcome: dict[str, object]) -> object:
    dimensions = cast("dict[str, dict[str, object]]", outcome.get("dimensions", {}))
    return dimensions.get("grounded_correctness", {}).get("state")


def _matched_by_name(report: dict[str, object]) -> dict[str, bool]:
    if report.get("schema_version") != _REPORT_SCHEMA:
        raise ValueError(f"a report must be {_REPORT_SCHEMA}.")
    outcomes = cast("list[dict[str, object]]", report.get("questions", []))
    return {str(item["name"]): bool(item["matched"]) for item in outcomes}


def build_report(
    question_set: RegressionSet,
    environment: str,
    summaries: list[dict[str, object]],
) -> dict[str, object]:
    """Assemble the content-free report from the probe's per-question summaries."""
    outcomes = [
        _scored_outcome(question, summary, environment)
        for question, summary in zip(question_set.questions, summaries, strict=True)
    ]
    unscored = [
        item["name"]
        for item, summary in zip(outcomes, summaries, strict=True)
        if _unscored(item, summary)
    ]
    return {
        "schema_version": _REPORT_SCHEMA,
        "set_version": question_set.set_version,
        "question_count": len(outcomes),
        "matched_count": sum(1 for item in outcomes if item["matched"]),
        # A question whose chain could not be attested or whose evaluator did not
        # complete with one unambiguous label per expected dimension was not scored.
        "unscored": unscored,
        "questions": outcomes,
    }


def _scored_outcome(
    question: RegressionQuestion, summary: Mapping[str, object], environment: str
) -> dict[str, object]:
    labels = cast("dict[str, str | None]", summary.get("evaluation_verdict_labels") or {})
    observed = dimension_labels(labels, environment, tuple(question.expected))
    outcome = question_outcome(question, observed)
    outcome["terminal"] = summary.get("terminal")
    outcome["dispatch_outcome"] = summary.get("evaluation_dispatch_outcome")
    outcome["stage_error"] = summary.get("stage_error")
    return outcome


def _unscored(outcome: Mapping[str, object], summary: Mapping[str, object]) -> bool:
    return (
        outcome["dispatch_outcome"] != "dispatched"
        or outcome["stage_error"] is not None
        or summary.get("evaluation_result_state") != "completed"
        or any(
            dimension["state"] in {_MISSING, _AMBIGUOUS}
            for dimension in cast("dict[str, dict[str, object]]", outcome["dimensions"]).values()
        )
    )


def prime_seeded(outcome: Mapping[str, object], summary: Mapping[str, object]) -> bool:
    """Return whether a prime was scored, matched, and read a measured figure successfully.

    A correct refusal can be graded Grounded, so the grade alone cannot show that the prime read
    a measured figure; the content-free audit receipt's trajectory can.
    """
    if _unscored(outcome, summary) or not outcome["matched"]:
        return False
    steps = cast("list[dict[str, object]]", summary.get("trajectory") or [])
    return any(
        str(step.get("tool_name", "")).startswith("measurement_")
        and step.get("disposition") == "succeeded"
        for step in steps
    )


def require_full_sampling(probe: ProbeTarget) -> None:
    """Refuse a run that could not score every question: no family enabled, or sampling < 100%."""
    releases = probe.target.enabled
    if not releases:
        raise ProbeStageError("evaluation_not_enabled")
    control = AgentCoreOnlineEvaluationControl(
        probe.session.client("bedrock-agentcore-control"),
        probe.session.client("bedrock-agentcore"),
    )
    for release in releases:
        if release.config_id is None:
            raise ProbeStageError("evaluation_release_incomplete")
        if control.get(release.config_id).sampling_percentage < _FULL_SAMPLING:
            raise ProbeStageError("evaluation_sampling_below_full")


@dataclass(frozen=True, slots=True)
class PersonaMemory:
    """The regression persona's Memory and the cross-session namespaces the Harness retrieves."""

    memory_id: str
    actor_id: str
    namespaces: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PrimeSeed:
    """One seeded prime: its question, this run's marker, and when its turn was sent."""

    name: str
    marker: str
    started: datetime


def resolve_persona_memory(probe: ProbeTarget, environment: str) -> PersonaMemory:
    """Resolve the persona's actor, the Memory, and the Harness's namespaces once per run."""
    access = probe.cookie.split(";", 1)[0].removeprefix("adl_session=")
    subject = unverified_claims(access).get("sub")
    if not isinstance(subject, str) or not subject:
        raise ProbeStageError("memory_preflight")
    ssm = probe.session.client("ssm")
    memory_arn = published_parameter(ssm, environment, "memoryArn")
    harness_arn = published_parameter(ssm, environment, "harnessArn")
    try:
        namespaces = cross_session_namespaces(
            probe.session.client("bedrock-agentcore-control"),
            harness_arn.rsplit("/", maxsplit=1)[-1],
        )
    except ValueError:
        raise ProbeStageError("memory_preflight") from None
    return PersonaMemory(
        memory_id=memory_arn.rsplit("/", maxsplit=1)[-1],
        actor_id=actor_id_for(subject),
        namespaces=namespaces,
    )


def sweep_persona_codenames(probe: ProbeTarget, memory: PersonaMemory) -> tuple[int, int]:
    """Remove P5 codename and earlier prime-marker records from the regression persona's Memory.

    A codename left there is a live preference that labels every answer, and the evaluator
    scores the label as an unsupported claim (2026-09-30), so the run would measure the probe
    rather than the agent. A prime marker left by an aborted run would crowd the same recall.
    Returns the codename count and the prime-marker count.
    """
    client = probe.session.client("bedrock-agentcore")
    actors = [memory.actor_id]
    codenames = sweep_codenames(
        client, memory.memory_id, actors, memory.namespaces, pattern=CODENAME
    )
    markers = sweep_codenames(
        client, memory.memory_id, actors, memory.namespaces, pattern=PRIME_MARKER
    )
    return codenames, markers


def read_preconditions(
    athena: ChatAthena, environment: str, questions: Sequence[RegressionQuestion]
) -> dict[str, dict[str, object]]:
    """Count the fixture rows each preconditioned question needs absent, before any turn.

    Raises `RuntimeError` when a read fails or returns no count, so no turn runs on a fixture
    the run could not observe.
    """
    found: dict[str, dict[str, object]] = {}
    for question in questions:
        condition = question.precondition
        if condition is None:
            continue
        # The fact and grain come only from closed allowlists and the date from `date`.
        sql = (
            f"SELECT count(*) FROM adlytics_{environment}_v2_current.{condition.fact} "  # noqa: S608
            f"WHERE time_grain = '{condition.time_grain}' "
            f"AND period_start = DATE '{condition.period_start.isoformat()}'"
        )
        rows = athena.rows(f"precondition_{question.name}", sql)
        try:
            count = int(str(rows[0][0]))
        except IndexError, ValueError:
            raise RuntimeError(f"precondition {question.name} returned no count") from None
        found[question.name] = {
            "fact": condition.fact,
            "time_grain": condition.time_grain,
            "period_start": condition.period_start.isoformat(),
            "count": count,
            "query_execution_id": athena.queries[-1]["query_execution_id"],
        }
    return found


def _now() -> datetime:
    return datetime.now(UTC)


def _record_seeds(record: Mapping[str, Any], seed: PrimeSeed) -> bool:
    text = str(record.get("content", {}).get("text", ""))
    markers = {match.group(0).lower() for match in PRIME_MARKER.finditer(text)}
    created = record.get("createdAt")
    return (
        seed.marker.lower() in markers
        and isinstance(created, datetime)
        and created >= seed.started - _CLOCK_SKEW
    )


def await_prime_records(
    client: BedrockAgentCoreClient,
    memory: PersonaMemory,
    primes: Sequence[PrimeSeed],
    *,
    bound_seconds: int = _SETTLE_BOUND_SECONDS,
    poll_seconds: int = _SETTLE_POLL_SECONDS,
) -> tuple[set[str], str | None]:
    """Wait until each seeded prime has a record in the cross-session semantic namespace.

    That namespace is the one the next question retrieves, so a record there proves the
    strategy processed the prime; a summary record would not. Returns the settled prime names
    and, when the last poll before the bound failed, that failure's error type. A failed list
    request is retried at the next poll until the bound, so one throttle does not end the wait;
    the error type separates a prime whose record could not be read from one never processed.
    A poll that ends with its page budget spent and a `nextToken` still set left records unread,
    so it fails the same way, as `PageBudgetExhausted`.
    """
    template = semantic_namespace(memory.namespaces)
    namespace_path = f"{template.replace('{actorId}', memory.actor_id)}/"
    pending = {seed.name: seed for seed in primes}
    deadline = time.monotonic() + bound_seconds
    poll_error: str | None = None
    while pending:
        poll_error = None
        next_token: str | None = None
        for _ in range(_PAGE_LIMIT):
            request: dict[str, object] = {
                "memoryId": memory.memory_id,
                "namespacePath": namespace_path,
                "maxResults": 100,
            }
            if next_token is not None:
                request["nextToken"] = next_token
            try:
                response = client.list_memory_records(**request)  # type: ignore[arg-type]
            except (BotoCoreError, ClientError) as error:
                poll_error = type(error).__name__
                break
            for record in response.get("memoryRecordSummaries", []):
                for name in [n for n, seed in pending.items() if _record_seeds(record, seed)]:
                    del pending[name]
            next_token = response.get("nextToken")
            if not next_token:
                break
        else:
            poll_error = _PAGE_BUDGET_EXHAUSTED
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(poll_seconds)
    return {seed.name for seed in primes} - set(pending), poll_error if pending else None


def _run_pool(
    probe: ProbeTarget, scenarios: Sequence[Scenario], concurrency: int
) -> dict[str, dict[str, object]]:
    if not scenarios:
        return {}
    # Verdicts arrive in service batches, not per turn, so questions run concurrently and each
    # waits for its own verdicts; the bound keeps the route and the review reads modest.
    with ThreadPoolExecutor(max_workers=min(concurrency, len(scenarios))) as pool:
        summaries = list(pool.map(lambda scenario: run_scenario_marked(probe, scenario), scenarios))
    return {scenario.name: summary for scenario, summary in zip(scenarios, summaries, strict=True)}


def _scenario(question: RegressionQuestion) -> Scenario:
    return Scenario(name=question.name, question=question.question, expects_finding=False)


@dataclass(slots=True)
class _PrimedRun:
    """What the prime stages leave behind for the report."""

    primes: list[dict[str, object]]
    teardown: str | None = None
    teardown_error: str | None = None
    settle_error: str | None = None


def _run_primed(  # noqa: PLR0917 - the run's target, its memory, and the stage inputs
    probe: ProbeTarget,
    memory: PersonaMemory,
    primed: Sequence[RegressionQuestion],
    environment: str,
    concurrency: int,
    summaries: dict[str, dict[str, object]],
) -> _PrimedRun:
    """Stages 3-6: primes in sequence, settle, primed questions, then teardown whatever happened."""
    run = _PrimedRun(primes=[])
    if not primed:
        return run
    client = probe.session.client("bedrock-agentcore")
    try:
        seeds: list[PrimeSeed] = []
        for question in primed:
            prime = cast("RegressionPrime", question.prime)
            marker = f"REGRESSPRIME-{secrets.token_hex(3)}"
            name = f"{question.name}.prime"
            scenario = Scenario(
                name=name,
                question=prime.question.replace(_MARKER_PLACEHOLDER, marker),
                expects_finding=False,
            )
            started = _now()
            summary = run_scenario_marked(probe, scenario)
            outcome = _scored_outcome(
                RegressionQuestion(name=name, question=prime.question, expected=prime.expected),
                summary,
                environment,
            )
            seeded = prime_seeded(outcome, summary)
            run.primes.append(
                {
                    "question": question.name,
                    "dimensions": outcome["dimensions"],
                    "seeded": seeded,
                    "stage_error": summary.get("stage_error"),
                }
            )
            if seeded:
                seeds.append(PrimeSeed(name=question.name, marker=marker, started=started))
            else:
                summaries[question.name] = {
                    "scenario": question.name,
                    "stage_error": _PRIME_NOT_SEEDED,
                }
        settled: set[str] = set()
        if seeds:
            settled, run.settle_error = await_prime_records(client, memory, seeds)
        for seed in seeds:
            if seed.name not in settled:
                summaries[seed.name] = {"scenario": seed.name, "stage_error": _MEMORY_SETTLE}
        ready = [_scenario(question) for question in primed if question.name not in summaries]
        summaries.update(_run_pool(probe, ready, concurrency))
    finally:
        try:
            settle_codenames(
                client, memory.memory_id, [memory.actor_id], memory.namespaces, pattern=PRIME_MARKER
            )
            run.teardown = "complete"
        except (TimeoutError, ClientError) as error:
            run.teardown = "incomplete"
            run.teardown_error = type(error).__name__
    return run


def _assemble_report(
    question_set: RegressionSet,
    environment: str,
    summaries: Mapping[str, dict[str, object]],
    *,
    sweeps: tuple[int, int],
    preconditions: Mapping[str, dict[str, object]],
    run: _PrimedRun,
    baseline: dict[str, object] | None,
) -> dict[str, object]:
    """Build the report in set order; primes never enter `questions` or `matched_count`."""
    report = build_report(
        question_set,
        environment,
        [summaries[question.name] for question in question_set.questions],
    )
    report["memory_preflight_swept"], report["prime_markers_swept"] = sweeps
    report["preconditions"] = [{"question": name, **item} for name, item in preconditions.items()]
    if run.teardown is not None:
        report["primes"] = run.primes
        if run.settle_error is not None:
            report["prime_settle_error"] = run.settle_error
        report["prime_teardown"] = run.teardown
        if run.teardown_error is not None:
            report["prime_teardown_error"] = run.teardown_error
    if baseline is not None:
        report["comparison"] = compare(baseline, report)
    return report


def _printed_summary(report: Mapping[str, object]) -> dict[str, object]:
    keys = (
        "set_version",
        "question_count",
        "matched_count",
        "unscored",
        "comparison",
        "memory_preflight_swept",
        "prime_teardown",
    )
    printed = {key: report[key] for key in keys if key in report}
    comparison = cast("dict[str, object]", report.get("comparison", {}))
    if "end_to_end" in comparison:
        printed["end_to_end"] = comparison["end_to_end"]
    return printed


def main(argv: list[str] | None = None) -> int:
    """Run the set, write the report into local evidence custody, and print its summary."""
    parser = probe_parser()
    parser.description = __doc__
    parser.add_argument("--question-set", default=_DEFAULT_SET)
    parser.add_argument("--baseline", help="A prior AgentRegressionReport.v1 to compare with.")
    parser.add_argument("--concurrency", type=int, default=4, choices=range(1, 9), metavar="1-8")
    args = parser.parse_args(argv)
    if args.environment != "dev" or args.region != "us-east-1":
        print("agent regression accepts only the approved dev region")
        return 2
    question_set = load_question_set((_ROOT / args.question_set).read_text())
    # Read and check the baseline before any turn runs, so a bad baseline costs nothing.
    baseline: dict[str, object] | None = None
    if args.baseline:
        baseline = cast("dict[str, object]", json.loads((_ROOT / args.baseline).read_text()))
        compare(
            baseline, {"schema_version": _REPORT_SCHEMA, "set_version": question_set.set_version}
        )
    try:
        probe = prepare_attested(args)
        require_full_sampling(probe)
        memory = resolve_persona_memory(probe, args.environment)
        swept, markers_swept = sweep_persona_codenames(probe, memory)
    except ProbeStageError as error:
        detail = f": {error}" if str(error) else ""
        print(f"agent regression could not establish the bounded dev audit chain{detail}")
        return 1
    except BotoCoreError, ClientError, AdlyticsError, TypeError, ValueError:
        print("agent regression could not establish the bounded dev audit chain")
        return 1
    # Stage 1, before any turn: a primed question needs its fixture period still unmeasured.
    try:
        preconditions = read_preconditions(
            ChatAthena(probe.session, args.environment), args.environment, question_set.questions
        )
    except RuntimeError, ClientError, BotoCoreError:
        print("agent regression could not read the fixture precondition")
        return 1
    summaries: dict[str, dict[str, object]] = {
        name: {"scenario": name, "stage_error": _FIXTURE_DRIFT}
        for name, item in preconditions.items()
        if cast("int", item["count"]) > 0
    }
    runnable = [question for question in question_set.questions if question.name not in summaries]
    # Stage 2: unprimed questions run before any prime, so no prime marker is in recall yet.
    unprimed = [_scenario(question) for question in runnable if question.prime is None]
    summaries.update(_run_pool(probe, unprimed, args.concurrency))
    # Stages 3-6.
    primed = [question for question in runnable if question.prime is not None]
    run = _run_primed(probe, memory, primed, args.environment, args.concurrency, summaries)
    report = _assemble_report(
        question_set,
        args.environment,
        summaries,
        sweeps=(swept, markers_swept),
        preconditions=preconditions,
        run=run,
        baseline=baseline,
    )
    evidence_dir = _ROOT / args.evidence_dir
    (evidence_dir / _REPORT_NAME).write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(_printed_summary(report)))
    if run.teardown == "incomplete":
        print("agent regression could not finish removing prime markers")
        return 1
    # Every question must have been scored; an undispatched turn is a run failure, not a result.
    return 1 if report["unscored"] else 0


if __name__ == "__main__":
    sys.exit(main())
