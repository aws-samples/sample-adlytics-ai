# SPDX-License-Identifier: MIT-0
"""Agent-mode regression runner: question-set decoding, label comparison, and baseline diff."""

from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))
from validation import probe_agent_quality_loop as probe  # noqa: E402
from validation import regress_agent as runner  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-079", "ADR-082")]

_V4_DIMENSIONS = {"grounded_correctness", "internal_consistency", "helpfulness"}


def _set(**question: object) -> str:
    body = {
        "name": "fictional_turn",
        "question": "Which metrics can this fictional workspace report?",
        "expected": {"grounded_correctness": ["Grounded"], "helpfulness": ["Excellent", "Good"]},
        **question,
    }
    return json.dumps(
        {"schema_version": "AgentRegressionSet.v1", "set_version": 1, "questions": [body]}
    )


def test_the_committed_set_decodes_and_names_only_v4_dimensions() -> None:
    question_set = runner.load_question_set(
        (_ROOT / "validation/agent-regression-v1.json").read_text()
    )

    assert question_set.set_version == 1
    assert {d for q in question_set.questions for d in q.expected} <= _V4_DIMENSIONS
    # The claim-free ambiguous turn is scored "Not Applicable", never "Unsupported" (ADR-082).
    ambiguous = next(q for q in question_set.questions if q.name == "ambiguous_reference")
    assert ambiguous.expected["grounded_correctness"] == ("not_applicable",)


def test_expected_labels_are_normalized_as_stored_verdicts_are() -> None:
    question = runner.load_question_set(_set()).questions[0]

    assert question.expected == {
        "grounded_correctness": ("grounded",),
        "helpfulness": ("excellent", "good"),
    }


@pytest.mark.parametrize(
    "text",
    [
        json.dumps({"schema_version": "Other.v1", "set_version": 1, "questions": []}),
        json.dumps({"schema_version": "AgentRegressionSet.v1", "set_version": 0, "questions": []}),
        _set(expected={"helpfulness": []}),
        _set(expected={"helpfulness": "Good"}),
    ],
    ids=["schema", "version", "empty-labels", "labels-not-a-list"],
)
def test_a_malformed_set_is_refused(text: str) -> None:
    with pytest.raises(ValueError):
        runner.load_question_set(text)


def test_duplicate_question_names_are_refused() -> None:
    document = json.loads(_set())
    document["questions"].append(document["questions"][0])

    with pytest.raises(ValueError, match="unique"):
        runner.load_question_set(json.dumps(document))


def test_verdicts_map_to_dimensions_by_the_evaluator_name_segment() -> None:
    labels = {
        "adlytics_dev_grounded_correctness_v4_53f3c237": "grounded",
        "adlytics_dev_helpfulness_v4_4576eb06": "good",
        "adlytics_other_helpfulness_v4_00000000": "poor",
    }

    assert runner.dimension_labels(labels, "dev", ("grounded_correctness", "helpfulness")) == {
        "grounded_correctness": "grounded",
        "helpfulness": "good",
    }


def test_a_question_matches_only_when_every_expected_dimension_matches() -> None:
    question = runner.load_question_set(_set()).questions[0]

    matched = runner.question_outcome(
        question, {"grounded_correctness": "grounded", "helpfulness": "good"}
    )
    mismatched = runner.question_outcome(
        question, {"grounded_correctness": "unsupported", "helpfulness": "good"}
    )
    missing = runner.question_outcome(question, {"helpfulness": "good"})

    assert matched["matched"] is True
    assert mismatched["matched"] is False
    assert mismatched["dimensions"]["grounded_correctness"]["state"] == "mismatched"  # type: ignore[index]
    assert missing["matched"] is False
    assert missing["dimensions"]["grounded_correctness"]["state"] == "missing"  # type: ignore[index]


def test_a_report_counts_matches_and_keeps_only_fictional_names() -> None:
    question_set = runner.load_question_set(_set())
    summary: dict[str, object] = {
        "terminal": "response_done",
        "evaluation_dispatch_outcome": "dispatched",
        "evaluation_verdict_labels": {
            "adlytics_dev_grounded_correctness_v4_53f3c237": "grounded",
            "adlytics_dev_helpfulness_v4_4576eb06": "excellent",
        },
    }

    report = runner.build_report(question_set, "dev", [summary])

    assert (report["question_count"], report["matched_count"]) == (1, 1)
    assert "Which metrics" not in json.dumps(report)


def test_a_baseline_comparison_lists_regressions_and_improvements() -> None:
    def report(**matched: bool) -> dict[str, object]:
        return {
            "schema_version": "AgentRegressionReport.v1",
            "set_version": 1,
            "questions": [{"name": name, "matched": value} for name, value in matched.items()],
        }

    assert runner.compare(report(a=True, b=False, c=True), report(a=False, b=True, d=True)) == {
        "regressed": ["a"],
        "improved": ["b"],
        "not_compared": ["c", "d"],
    }
    with pytest.raises(ValueError, match=r"AgentRegressionReport\.v1"):
        runner.compare({"schema_version": "Other", "set_version": 1}, report(a=True))


def test_main_refuses_a_non_dev_target_before_any_aws_call() -> None:
    argv = [
        "--profile", "fictional", "--environment", "prod", "--region", "us-east-1",
        "--expected-account", "111122223333", "--result-link-stack", "x", "--evidence-dir", "x",
    ]  # fmt: skip

    assert runner.main(argv) == 2


def test_two_verdicts_for_one_dimension_are_ambiguous_not_last_wins() -> None:
    labels = {
        "adlytics_dev_helpfulness_v4_aaaaaaaa": "good",
        "adlytics_dev_helpfulness_v5_bbbbbbbb": "poor",
    }
    question = runner.load_question_set(_set(expected={"helpfulness": ["Good"]})).questions[0]

    observed = runner.dimension_labels(labels, "dev", ("helpfulness",))
    outcome = runner.question_outcome(question, observed)

    assert outcome["matched"] is False
    assert outcome["dimensions"]["helpfulness"]["state"] == "ambiguous"  # type: ignore[index]


def test_reports_of_different_set_versions_are_not_compared() -> None:
    def report(version: int) -> dict[str, object]:
        return {
            "schema_version": "AgentRegressionReport.v1",
            "set_version": version,
            "questions": [],
        }

    with pytest.raises(ValueError, match="same question set version"):
        runner.compare(report(1), report(2))


def test_unknown_members_are_refused_rather_than_ignored() -> None:
    document = json.loads(_set())
    document["questions"][0]["expected_answer"] = "fictional"

    with pytest.raises(ValueError, match="unknown member"):
        runner.load_question_set(json.dumps(document))


def test_an_unscored_question_is_listed_so_the_run_fails() -> None:
    question_set = runner.load_question_set(_set())
    summary: dict[str, object] = {
        "terminal": "response_done",
        "evaluation_dispatch_outcome": "not_dispatched",
        "evaluation_verdict_labels": {},
    }

    report = runner.build_report(question_set, "dev", [summary])

    assert report["unscored"] == ["fictional_turn"]
    assert report["questions"][0]["dimensions"]["helpfulness"]["state"] == "missing"  # type: ignore[index]


def test_a_run_with_no_enabled_family_is_refused_before_any_turn() -> None:
    class _Target:
        enabled = ()

    class _Probe:
        target = _Target()

    with pytest.raises(runner.ProbeStageError, match="evaluation_not_enabled"):
        runner.require_full_sampling(cast("Any", _Probe()))


def test_a_question_whose_chain_failed_is_reported_unscored_with_its_marker() -> None:
    question_set = runner.load_question_set(_set())

    report = runner.build_report(
        question_set, "dev", [{"scenario": "fictional_turn", "stage_error": "verdict_pending"}]
    )

    assert report["unscored"] == ["fictional_turn"]
    assert report["questions"][0]["stage_error"] == "verdict_pending"  # type: ignore[index]


def _urllib3_protocol_error() -> Exception:
    """Build a fictional stand-in for urllib3's ProtocolError, which is not an OSError."""
    fictional = type("ProtocolError", (Exception,), {"__module__": "urllib3.exceptions"})
    return fictional("Connection broken: fictional-host.example")


@pytest.mark.parametrize(
    ("raised", "marker"),
    [
        (TimeoutError("Read timed out. host=fictional-host.example"), "timeout"),
        (ConnectionError("fictional-host.example refused"), "transport"),
        (_urllib3_protocol_error(), "transport"),
    ],
)
def test_one_questions_transport_failure_is_a_fixed_marker_not_a_crashed_run(
    monkeypatch: pytest.MonkeyPatch, raised: Exception, marker: str
) -> None:
    """One broken stream must not abort the pool; its text may name the Function URL host."""

    def _raise(*_: object) -> dict[str, object]:
        raise raised

    monkeypatch.setattr(probe, "run_scenario", _raise)

    summary = probe.run_scenario_marked(cast("Any", None), cast("Any", _Scenario()))

    assert summary == {"scenario": "fictional_turn", "stage_error": marker}


def test_an_unrelated_exception_still_propagates_from_the_marked_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*_: object) -> dict[str, object]:
        raise KeyError("fictional")

    monkeypatch.setattr(probe, "run_scenario", _raise)

    with pytest.raises(KeyError):
        probe.run_scenario_marked(cast("Any", None), cast("Any", _Scenario()))


def test_target_preparation_failures_surface_as_the_public_stage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*_: object) -> object:
        raise probe._ProbeStageError("evaluation_release_unsubscribed")  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setattr(probe, "prepare_target", _raise)

    with pytest.raises(probe.ProbeStageError, match="evaluation_release_unsubscribed"):
        probe.prepare_attested(cast("Any", None))


class _Scenario:
    name = "fictional_turn"


def test_a_dispatched_evaluator_failure_is_unscored_even_with_a_terminal_result() -> None:
    question_set = runner.load_question_set(_set())
    summary: dict[str, object] = {
        "scenario": "fictional_turn",
        "terminal": "response_done",
        "evaluation_dispatch_outcome": "dispatched",
        "evaluation_result_state": "evaluator_failed",
        "evaluation_verdict_labels": {"adlytics_dev_helpfulness_v4_fixture": None},
    }

    report = runner.build_report(question_set, "dev", [summary])

    assert report["unscored"] == ["fictional_turn"]


def test_a_completed_label_mismatch_is_scored_but_does_not_match() -> None:
    question_set = runner.load_question_set(_set())
    summary: dict[str, object] = {
        "scenario": "fictional_turn",
        "terminal": "response_done",
        "evaluation_dispatch_outcome": "dispatched",
        "evaluation_result_state": "completed",
        "evaluation_verdict_labels": {
            "adlytics_dev_grounded_correctness_v4_fixture": "unsupported",
            "adlytics_dev_helpfulness_v4_fixture": "good",
        },
    }

    report = runner.build_report(question_set, "dev", [summary])

    assert report["unscored"] == []
    assert report["matched_count"] == 0


# --- AgentRegressionSet.v2: primed questions (ADR-147) ---------------------------------------

_PRIMED_EXPECTED = {
    "grounded_correctness": ("grounded", "not_applicable"),
    "internal_consistency": ("consistent",),
    # A curt refusal that never offers the nearest measured period is not a fix.
    "helpfulness": ("excellent", "good"),
}


@pytest.mark.covers("ADR-147")
def test_the_committed_v2_set_carries_v1_unchanged_plus_two_primed_questions() -> None:
    v1 = runner.load_question_set((_ROOT / "validation/agent-regression-v1.json").read_text())
    v2 = runner.load_question_set((_ROOT / "validation/agent-regression-v2.json").read_text())

    assert v2.set_version == 2
    assert [(q.name, q.question, q.expected) for q in v2.questions[: len(v1.questions)]] == [
        (q.name, q.question, q.expected) for q in v1.questions
    ]
    assert all(q.prime is None and q.precondition is None for q in v1.questions)
    primed = v2.questions[len(v1.questions) :]
    assert [q.name for q in primed] == [
        "remembered_figure_after_refusal",
        "remembered_figure_when_metric_has_no_data",
    ]
    assert [q.prime.question.count("{marker}") for q in primed if q.prime] == [1, 1]
    assert [q.prime.expected for q in primed if q.prime] == [
        {"grounded_correctness": ("grounded",)},
        {"grounded_correctness": ("grounded",)},
    ]
    assert [q.precondition for q in primed] == [
        runner.FixturePrecondition("fct_exposure_group_outcome", "week", date(2026, 10, 5)),
        runner.FixturePrecondition("fct_new_to_brand_buyers", "month", date(2026, 9, 1)),
    ]
    assert [q.expected for q in primed] == [_PRIMED_EXPECTED, _PRIMED_EXPECTED]
    assert runner._DEFAULT_SET == "validation/agent-regression-v2.json"  # pyright: ignore[reportPrivateUsage]


def _primed_question(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "primed_turn",
        "question": "How many fictional shoppers did we reach last week?",
        "expected": {"grounded_correctness": ["Grounded", "Not Applicable"]},
        "prime": {
            "question": "For context, I'm preparing the fictional {marker} memo. How many?",
            "expected": {"grounded_correctness": ["Grounded"]},
        },
        "precondition": {
            "fact": "fct_exposure_group_outcome",
            "time_grain": "week",
            "period_start": "2026-10-05",
        },
    }
    body.update(overrides)
    return body


def _v2_set(*questions: dict[str, object], schema: str = "AgentRegressionSet.v2") -> str:
    return json.dumps({"schema_version": schema, "set_version": 2, "questions": list(questions)})


_PRIME = cast("dict[str, object]", _primed_question()["prime"])
_PRECONDITION = cast("dict[str, object]", _primed_question()["precondition"])


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            _v2_set(_primed_question(prime={**_PRIME, "question": "No placeholder here."})),
            "question primed_turn prime must hold {marker} exactly once.",
        ),
        (
            _v2_set(_primed_question(prime={**_PRIME, "question": "{marker} and {marker}"})),
            "question primed_turn prime must hold {marker} exactly once.",
        ),
        (
            _v2_set(_primed_question(precondition={**_PRECONDITION, "fact": "fct_other"})),
            "question primed_turn precondition fact is outside the allowlist.",
        ),
        (
            _v2_set(_primed_question(precondition={**_PRECONDITION, "time_grain": "day"})),
            "question primed_turn precondition time_grain must be week or month.",
        ),
        (
            _v2_set(_primed_question(precondition={**_PRECONDITION, "period_start": "5 Oct"})),
            "question primed_turn precondition period_start must be an ISO date.",
        ),
        (
            _v2_set(
                {key: value for key, value in _primed_question().items() if key != "precondition"}
            ),
            "question primed_turn carries a prime without a precondition.",
        ),
        (
            _v2_set(_primed_question(), schema="AgentRegressionSet.v1"),
            "only an AgentRegressionSet.v2 question may carry a prime or precondition.",
        ),
        (
            _v2_set(_primed_question(prime={**_PRIME, "answer": "fictional"})),
            "question primed_turn prime carries an unknown member.",
        ),
    ],
    ids=[
        "marker-missing",
        "marker-twice",
        "fact-outside-allowlist",
        "grain",
        "date",
        "prime-without-precondition",
        "prime-in-v1-set",
        "unknown-prime-member",
    ],
)
@pytest.mark.covers("ADR-147")
def test_a_malformed_v2_question_is_refused(text: str, message: str) -> None:
    with pytest.raises(ValueError) as refused:
        runner.load_question_set(text)
    assert str(refused.value) == message


# --- The six stages, against stubbed AWS seams (no network) -----------------------------------

_START = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
_MEMORY = runner.PersonaMemory(
    memory_id="memory",
    actor_id="actor-a",
    namespaces=("/context/{actorId}", "/preferences/{actorId}"),
)
_MEASURED = [{"ordinal": 1, "tool_name": "measurement_metric_by_grain", "disposition": "succeeded"}]


def _summary(
    grounded: str = "grounded", trajectory: list[dict[str, object]] | None = None
) -> dict[str, object]:
    return {
        "terminal": "response_done",
        "evaluation_dispatch_outcome": "dispatched",
        "evaluation_result_state": "completed",
        "evaluation_verdict_labels": {
            "adlytics_dev_grounded_correctness_v4_fixture": grounded,
            "adlytics_dev_internal_consistency_v4_fixture": "consistent",
        },
        "trajectory": list(_MEASURED if trajectory is None else trajectory),
    }


class _Memory:
    """Long-term records by namespace path; a prime's marker is extracted when `extract` is on."""

    def __init__(self, *, extract: bool = True, created_offset: timedelta = timedelta()) -> None:
        self.extract = extract
        self.created_offset = created_offset
        self.records: dict[str, list[dict[str, object]]] = {}
        self.requests: list[dict[str, object]] = []
        self.deleted: list[str] = []
        self.delete_error: Exception | None = None
        # Raised in order, one per list request after a prime was remembered: the settle polls.
        self.settle_errors: list[Exception] = []
        # How many list requests had been made when the first prime was remembered.
        self.remembered_at: int | None = None
        # Once a prime was remembered, every page is empty and carries a `nextToken`: a
        # namespace whose records lie beyond any page budget.
        self.endless_pages = False

    def remember(self, text: str) -> None:
        if not self.extract:
            return
        if self.remembered_at is None:
            self.remembered_at = len(self.requests)
        records = self.records.setdefault("/context/actor-a/", [])
        records.append(
            {
                "memoryRecordId": f"r{len(records)}",
                "content": {"text": text},
                "createdAt": _START + self.created_offset,
            }
        )

    def list_memory_records(self, **request: object) -> dict[str, object]:
        self.requests.append(dict(request))
        if self.settle_errors and self.records:
            raise self.settle_errors.pop(0)
        if self.endless_pages and self.records:
            return {"memoryRecordSummaries": [], "nextToken": "more"}
        return {
            "memoryRecordSummaries": [
                record
                for record in self.records.get(str(request["namespacePath"]), [])
                if record["memoryRecordId"] not in self.deleted
            ]
        }

    def delete_memory_record(self, *, memoryId: str, memoryRecordId: str) -> None:  # noqa: N803
        del memoryId
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(memoryRecordId)


class _Session:
    def __init__(self, memory: _Memory) -> None:
        self.memory = memory

    def client(self, name: str) -> object:
        assert name == "bedrock-agentcore", name
        return self.memory


class _Athena:
    counts: ClassVar[dict[str, int]] = {}
    error: ClassVar[Exception | None] = None
    reads: ClassVar[list[str]] = []

    def __init__(self, session: object, environment: str) -> None:
        del session, environment
        self.queries: list[dict[str, object]] = []

    def rows(self, label: str, sql: str) -> list[list[str | None]]:
        type(self).reads.append(sql)
        error = type(self).error
        if error is not None:
            raise error
        self.queries.append({"label": label, "sql": sql, "query_execution_id": f"q-{label}"})
        return [[str(type(self).counts.get(label, 0))]]


def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a monotonic clock that only `sleep` advances, so bounded waits end at once."""
    clock = [0.0]

    def _sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runner.time, "sleep", _sleep)


class _World:
    """Stubs every AWS seam `main` reaches and records the order scenarios ran in."""

    def __init__(  # noqa: PLR0913 -- one keyword per stubbed seam a case varies
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        memory: _Memory | None = None,
        prime_summary: dict[str, object] | None = None,
        question_summary: dict[str, object] | None = None,
        counts: dict[str, int] | None = None,
        athena_error: Exception | None = None,
    ) -> None:
        self.memory = memory or _Memory()
        self.tmp_path = tmp_path
        self.log: list[tuple[str, str]] = []
        self.prime_summary = prime_summary or _summary()
        self.question_summary = question_summary or _summary()
        _fake_clock(monkeypatch)
        monkeypatch.setattr(runner, "_now", lambda: _START)
        probe_target = SimpleNamespace(session=_Session(self.memory))

        def _prepare(_args: object) -> object:
            return probe_target

        def _sampling(_probe: object) -> None:
            return None

        def _memory(_probe: object, _environment: str) -> runner.PersonaMemory:
            return _MEMORY

        monkeypatch.setattr(runner, "prepare_attested", _prepare)
        monkeypatch.setattr(runner, "require_full_sampling", _sampling)
        monkeypatch.setattr(runner, "resolve_persona_memory", _memory)
        _Athena.counts = dict(counts or {})
        _Athena.error = athena_error
        _Athena.reads = []
        monkeypatch.setattr(runner, "ChatAthena", _Athena)
        monkeypatch.setattr(runner, "run_scenario_marked", self._run)

    def _run(self, _probe: object, scenario: probe.Scenario) -> dict[str, object]:
        self.log.append(("start", scenario.name))
        if scenario.name.endswith(".prime"):
            assert runner.PRIME_MARKER.search(scenario.question), scenario.question
            self.memory.remember(f"The user is preparing the {scenario.question[30:80]} memo.")
            summary = dict(self.prime_summary)
        else:
            summary = dict(self.question_summary)
        self.log.append(("end", scenario.name))
        return {"scenario": scenario.name, **summary}

    def main(self, *questions: dict[str, object]) -> int:
        question_set = self.tmp_path / "set.json"
        question_set.write_text(_v2_set(*questions))
        argv = [
            "--profile", "fictional", "--environment", "dev", "--region", "us-east-1",
            "--expected-account", "111122223333", "--result-link-stack", "x",
            "--evidence-dir", str(self.tmp_path), "--question-set", str(question_set),
            "--concurrency", "2",
        ]  # fmt: skip
        return runner.main(argv)

    def report(self) -> dict[str, Any]:
        return cast("dict[str, Any]", json.loads((self.tmp_path / runner._REPORT_NAME).read_text()))  # pyright: ignore[reportPrivateUsage]


_UNPRIMED: dict[str, object] = {
    "name": "unprimed_a",
    "question": "Which fictional metrics exist?",
    "expected": {"grounded_correctness": ["Grounded"]},
}
_UNPRIMED_B: dict[str, object] = {**_UNPRIMED, "name": "unprimed_b"}
_PRIMED_B = _primed_question(
    name="primed_other",
    precondition={**_PRECONDITION, "fact": "fct_new_to_brand_buyers", "time_grain": "month"},
)


@pytest.mark.covers("ADR-147")
def test_unprimed_questions_finish_before_primes_and_primes_before_primed_questions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    world = _World(monkeypatch, tmp_path)

    assert world.main(_UNPRIMED, _primed_question(), _UNPRIMED_B, _PRIMED_B) == 0

    first_prime = world.log.index(("start", "primed_turn.prime"))
    assert {name for event, name in world.log[:first_prime] if event == "end"} == {
        "unprimed_a",
        "unprimed_b",
    }
    primes = [entry for entry in world.log if entry[1].endswith(".prime")]
    assert primes == [
        ("start", "primed_turn.prime"),
        ("end", "primed_turn.prime"),
        ("start", "primed_other.prime"),
        ("end", "primed_other.prime"),
    ]
    first_primed = min(
        world.log.index(("start", "primed_turn")), world.log.index(("start", "primed_other"))
    )
    assert world.log.index(("end", "primed_other.prime")) < first_primed
    report = world.report()
    assert [item["name"] for item in report["questions"]] == [
        "unprimed_a",
        "primed_turn",
        "unprimed_b",
        "primed_other",
    ]
    assert (report["question_count"], report["matched_count"], report["unscored"]) == (4, 4, [])
    assert [(item["question"], item["seeded"]) for item in report["primes"]] == [
        ("primed_turn", True),
        ("primed_other", True),
    ]
    assert report["prime_teardown"] == "complete"
    assert report["preconditions"] == [
        {
            "question": "primed_turn",
            "fact": "fct_exposure_group_outcome",
            "time_grain": "week",
            "period_start": "2026-10-05",
            "count": 0,
            "query_execution_id": "q-precondition_primed_turn",
        },
        {
            "question": "primed_other",
            "fact": "fct_new_to_brand_buyers",
            "time_grain": "month",
            "period_start": "2026-10-05",
            "count": 0,
            "query_execution_id": "q-precondition_primed_other",
        },
    ]
    assert _Athena.reads[0] == (
        "SELECT count(*) FROM adlytics_dev_v2_current.fct_exposure_group_outcome "
        "WHERE time_grain = 'week' AND period_start = DATE '2026-10-05'"
    )
    # Teardown removed both prime markers it had waited for.
    assert world.memory.deleted == ["r0", "r1"]


@pytest.mark.covers("ADR-147")
def test_a_measured_fixture_period_is_drift_and_its_prime_never_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    world = _World(monkeypatch, tmp_path, counts={"precondition_primed_turn": 1})

    assert world.main(_UNPRIMED, _primed_question()) == 1

    assert [name for _event, name in world.log] == ["unprimed_a", "unprimed_a"]
    report = world.report()
    assert report["unscored"] == ["primed_turn"]
    assert report["questions"][1]["stage_error"] == "fixture_drift"
    assert "primes" not in report


@pytest.mark.covers("ADR-147")
def test_a_prime_graded_unsupported_is_not_seeded_and_its_question_never_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    world = _World(monkeypatch, tmp_path, prime_summary=_summary(grounded="unsupported"))

    assert world.main(_primed_question()) == 1

    assert ("start", "primed_turn") not in world.log
    report = world.report()
    assert report["unscored"] == ["primed_turn"]
    assert report["questions"][0]["stage_error"] == "prime_not_seeded"
    assert report["primes"][0]["seeded"] is False
    # No settle time was spent on it: every list request is a teardown sweep of both namespaces.
    sweep = [
        {"memoryId": "memory", "namespacePath": "/context/actor-a/", "maxResults": 100},
        {"memoryId": "memory", "namespacePath": "/preferences/actor-a/", "maxResults": 100},
    ]
    assert world.memory.requests == sweep * (len(world.memory.requests) // 2)


@pytest.mark.covers("ADR-147")
def test_a_prime_with_no_record_by_the_bound_is_memory_settle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    world = _World(monkeypatch, tmp_path, memory=_Memory(extract=False))

    assert world.main(_primed_question()) == 1

    assert ("start", "primed_turn") not in world.log
    report = world.report()
    assert report["unscored"] == ["primed_turn"]
    assert report["questions"][0]["stage_error"] == "memory_settle"
    # Every poll was read, so nothing says the record could not be observed.
    assert "prime_settle_error" not in report
    settle = [
        request
        for request in world.memory.requests
        if request["namespacePath"] == "/context/actor-a/"
    ]
    # One poll at 0 s and one every 30 s up to the 600 s bound, then teardown's sweeps.
    assert (
        settle[:21]
        == [{"memoryId": "memory", "namespacePath": "/context/actor-a/", "maxResults": 100}] * 21
    )


_SETTLE_ERROR_CASES = [
    (
        ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "ListMemoryRecords"
        ),
        "ClientError",
    ),
    (
        EndpointConnectionError(endpoint_url="https://bedrock-agentcore.fictional"),
        "EndpointConnectionError",
    ),
]
_SETTLE_ERROR_IDS = ["throttling", "connection"]
_SETTLE_REQUEST = {"memoryId": "memory", "namespacePath": "/context/actor-a/", "maxResults": 100}


@pytest.mark.parametrize(
    "error", [error for error, _type in _SETTLE_ERROR_CASES], ids=_SETTLE_ERROR_IDS
)
@pytest.mark.covers("ADR-147")
def test_a_transient_settle_poll_failure_is_retried_and_the_primed_question_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception
) -> None:
    memory = _Memory()
    memory.settle_errors = [error]
    world = _World(monkeypatch, tmp_path, memory=memory)

    assert world.main(_primed_question()) == 0

    assert ("start", "primed_turn") in world.log
    report = world.report()
    assert (report["question_count"], report["matched_count"], report["unscored"]) == (1, 1, [])
    assert "prime_settle_error" not in report
    # The failed poll at 0 s was retried at 30 s, which found the prime's record; teardown's
    # sweep of both namespaces follows.
    assert memory.settle_errors == []
    start = cast("int", memory.remembered_at)
    assert memory.requests[start : start + 4] == [
        _SETTLE_REQUEST,
        _SETTLE_REQUEST,
        _SETTLE_REQUEST,
        {**_SETTLE_REQUEST, "namespacePath": "/preferences/actor-a/"},
    ]
    assert report["prime_teardown"] == "complete"


@pytest.mark.parametrize(("error", "error_type"), _SETTLE_ERROR_CASES, ids=_SETTLE_ERROR_IDS)
@pytest.mark.covers("ADR-147")
def test_a_settle_poll_failing_until_the_bound_is_memory_settle_with_its_error_type(  # noqa: PLR0913, PLR0917 -- pytest fixtures and the error case
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    error_type: str,
) -> None:
    memory = _Memory()
    # One poll at 0 s and one every 30 s up to the 600 s bound, every one failing.
    memory.settle_errors = [error] * 21
    world = _World(monkeypatch, tmp_path, memory=memory)

    assert world.main(_primed_question()) == 1

    assert ("start", "primed_turn") not in world.log
    # 21 failed polls, then teardown's sweep of both namespaces.
    assert memory.settle_errors == []
    start = cast("int", memory.remembered_at)
    assert memory.requests[start : start + 23] == [
        *[_SETTLE_REQUEST] * 22,
        {**_SETTLE_REQUEST, "namespacePath": "/preferences/actor-a/"},
    ]
    report = world.report()
    assert report["unscored"] == ["primed_turn"]
    assert report["questions"][0]["stage_error"] == "memory_settle"
    assert report["prime_settle_error"] == error_type
    assert [(item["question"], item["seeded"]) for item in report["primes"]] == [
        ("primed_turn", True)
    ]
    # Teardown still removed the prime's marker once the bound was reached.
    assert report["prime_teardown"] == "complete"
    assert world.memory.deleted == ["r0"]
    assert capsys.readouterr().out.splitlines() == [
        json.dumps(
            {
                "set_version": 2,
                "question_count": 1,
                "matched_count": 0,
                "unscored": ["primed_turn"],
                "memory_preflight_swept": 0,
                "prime_teardown": "complete",
            }
        )
    ]


@pytest.mark.covers("ADR-147")
def test_a_settle_poll_that_spends_its_page_budget_is_memory_settle_with_that_error_type(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    memory = _Memory()
    memory.endless_pages = True
    world = _World(monkeypatch, tmp_path, memory=memory)

    assert world.main(_primed_question()) == 1

    assert ("start", "primed_turn") not in world.log
    report = world.report()
    assert report["unscored"] == ["primed_turn"]
    assert report["questions"][0]["stage_error"] == "memory_settle"
    # Records past the budget were never read, so the report does not claim a clean namespace.
    assert report["prime_settle_error"] == "PageBudgetExhausted"
    start = cast("int", memory.remembered_at)
    # Each poll reads 20 pages, then the next poll starts again from the first page.
    assert memory.requests[start : start + 21] == [
        _SETTLE_REQUEST,
        *[{**_SETTLE_REQUEST, "nextToken": "more"}] * 19,
        _SETTLE_REQUEST,
    ]


@pytest.mark.parametrize(
    ("trajectory", "seeded"),
    [
        (_MEASURED, True),
        (
            [{"ordinal": 1, "tool_name": "measurement_metric_by_grain", "disposition": "refused"}],
            False,
        ),
        ([{"ordinal": 1, "tool_name": "catalog_list_metrics", "disposition": "succeeded"}], False),
    ],
    ids=["succeeded-measurement", "refused-only", "catalog-only"],
)
@pytest.mark.covers("ADR-147")
def test_a_grounded_prime_is_seeded_only_by_a_succeeded_measurement_read(
    trajectory: list[dict[str, object]], *, seeded: bool
) -> None:
    question = runner.RegressionQuestion(
        name="primed_turn.prime",
        question="fictional",
        expected={"grounded_correctness": ("grounded",)},
    )
    summary = _summary(trajectory=trajectory)
    outcome = runner._scored_outcome(question, summary, "dev")  # pyright: ignore[reportPrivateUsage]

    assert outcome["matched"] is True
    assert runner.prime_seeded(outcome, summary) is seeded


@pytest.mark.parametrize(
    "case",
    [
        ("Preparing the REGRESSPRIME-0A1B2C memo.", timedelta(), {"primed_turn"}),
        ("Preparing the REGRESSPRIME-0a1b2c memo.", timedelta(seconds=-30), {"primed_turn"}),
        ("Preparing the REGRESSPRIME-0a1b2c memo.", timedelta(seconds=-90), set[str]()),
        ("Preparing the REGRESSPRIME-ffffff memo.", timedelta(), set[str]()),
    ],
    ids=["upper-case-marker", "30s-before-start", "90s-before-start", "other-marker"],
)
@pytest.mark.covers("ADR-147")
def test_a_settle_record_must_carry_this_runs_marker_after_the_prime_started(
    monkeypatch: pytest.MonkeyPatch, case: tuple[str, timedelta, set[str]]
) -> None:
    text, offset, settled = case
    _fake_clock(monkeypatch)
    memory = _Memory(created_offset=offset)
    memory.remember(text)
    seed = runner.PrimeSeed(name="primed_turn", marker="REGRESSPRIME-0a1b2c", started=_START)

    found = runner.await_prime_records(
        cast("Any", memory), _MEMORY, [seed], bound_seconds=60, poll_seconds=30
    )

    assert found == (settled, None)
    assert set(map(json.dumps, memory.requests)) == {
        json.dumps({"memoryId": "memory", "namespacePath": "/context/actor-a/", "maxResults": 100})
    }


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("fictional"),
        ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "Delete"),
    ],
    ids=["timeout", "throttling"],
)
@pytest.mark.covers("ADR-147")
def test_an_incomplete_teardown_still_writes_the_report_and_fails_the_run(  # noqa: PLR0913, PLR0917 -- pytest fixtures and the error case
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    world = _World(monkeypatch, tmp_path)
    if isinstance(error, TimeoutError):

        def _timeout(*_args: object, **_kwargs: object) -> int:
            raise error

        monkeypatch.setattr(runner, "settle_codenames", _timeout)
    else:
        world.memory.delete_error = error

    assert world.main(_primed_question()) == 1

    report = world.report()
    assert report["unscored"] == []
    assert report["matched_count"] == 1
    assert report["prime_teardown"] == "incomplete"
    assert report["prime_teardown_error"] == type(error).__name__
    assert capsys.readouterr().out.splitlines() == [
        json.dumps(
            {
                "set_version": 2,
                "question_count": 1,
                "matched_count": 1,
                "unscored": [],
                "memory_preflight_swept": 0,
                "prime_teardown": "incomplete",
            }
        ),
        "agent regression could not finish removing prime markers",
    ]


@pytest.mark.covers("ADR-147")
def test_an_unreadable_fixture_precondition_stops_the_run_before_any_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _World(monkeypatch, tmp_path, athena_error=RuntimeError("governed read ended FAILED"))

    assert world.main(_UNPRIMED, _primed_question()) == 1

    assert world.log == []
    assert capsys.readouterr().out.splitlines() == [
        "agent regression could not read the fixture precondition"
    ]


def _report(**questions: tuple[bool, str, bool]) -> dict[str, object]:
    """Each question: (matched, grounded_correctness state, unscored)."""
    return {
        "schema_version": "AgentRegressionReport.v1",
        "set_version": 2,
        "unscored": [name for name, (_m, _s, unscored) in questions.items() if unscored],
        "questions": [
            {
                "name": name,
                "matched": matched,
                "dimensions": {"grounded_correctness": {"state": state}},
            }
            for name, (matched, state, _unscored) in questions.items()
        ],
    }


@pytest.mark.covers("ADR-147")
def test_primes_never_enter_the_counts_or_the_comparison() -> None:
    baseline = {**_report(a=(True, "matched", False)), "primes": [{"question": "a"}]}
    current = {**_report(a=(True, "matched", False)), "primes": [{"question": "a"}]}

    comparison = runner.compare(baseline, current)

    assert (comparison["regressed"], comparison["improved"], comparison["not_compared"]) == (
        [],
        [],
        [],
    )
    question_set = runner.load_question_set(_v2_set(_primed_question()))
    report = runner.build_report(question_set, "dev", [_summary()])
    assert (report["question_count"], report["matched_count"]) == (1, 1)


@pytest.mark.covers("ADR-147")
def test_end_to_end_evidence_counts_only_a_scored_baseline_mismatch_now_matched() -> None:
    baseline = _report(
        fixed=(False, "mismatched", False),
        unscored_before=(False, "missing", True),
        matched_before=(True, "matched", False),
        still_failing=(False, "mismatched", False),
    )
    current = {
        **_report(
            fixed=(True, "matched", False),
            unscored_before=(True, "matched", False),
            matched_before=(True, "matched", False),
            still_failing=(False, "mismatched", False),
        ),
        "primes": [
            {"question": name}
            for name in ("fixed", "unscored_before", "matched_before", "still_failing")
        ],
    }

    assert runner.compare(baseline, current)["end_to_end"] == {
        "fixed": ["fixed"],
        "pending": [
            {"name": "unscored_before", "reason": "baseline_unscored"},
            {"name": "matched_before", "reason": "baseline_not_mismatched"},
            {"name": "still_failing", "reason": "current_not_matched"},
        ],
    }
