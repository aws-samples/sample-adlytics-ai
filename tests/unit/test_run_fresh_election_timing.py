# SPDX-License-Identifier: MIT-0
"""Plan item 12: `FreshElectionTimingV1` derivation, refusals, and the probe-world custody.

The probe table is a `FakeDynamo` with deletes, so seeding, the real rehearsal transaction, and
the disposal all run the production adapters without AWS. Fictional data only.
"""

from __future__ import annotations

import copy
import itertools
import pathlib
import sys
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError
from dynamodb_reserved_words import guard_reserved_words
from execution_graph_support import FakeDynamo

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))

from validation import run_fresh_election_timing as timing  # noqa: E402

from adapters.jobs.execution_graph.fence_settler import due_shards  # noqa: E402
from domain.operations.execution_graph_release import INGESTION_RELEASE_DOMAIN  # noqa: E402
from domain.operations.generated_execution_graphs import BOUNDS  # noqa: E402

pytestmark = pytest.mark.unit

_MAX_REDECIDE = int(BOUNDS["max_redecide"])


@guard_reserved_words
class _ProbeTable(FakeDynamo):
    """The probe table: `FakeDynamo` plus the unconditional delete the disposal issues."""

    def delete_item(self, **kwargs: Any) -> dict[str, Any]:
        self.items.pop(self._key(kwargs["Key"]), None)
        return {}


def _clock() -> Any:
    ticks = itertools.count(1)
    return lambda: next(ticks) / 1000


def _inputs(table: _ProbeTable) -> timing.SampleInputs:
    return timing.SampleInputs(table, "probe", _MAX_REDECIDE, 60_000, clock=_clock())


def _identity(**overrides: object) -> dict[str, object]:
    return {"max_redecide": _MAX_REDECIDE, "promoter_timeout_ms": 60_000, **overrides}


# --- Derivation and refusals -----------------------------------------------------------------


def test_the_guard_is_exactly_ceil_two_times_the_largest_attempt_total() -> None:
    assert timing.derive_guard([1200.0, 4321.2, 30.5], 60_000) == 8643
    assert timing.derive_guard([0.4], 60_000) == 1


def test_an_empty_sample_is_refused() -> None:
    with pytest.raises(timing.TimingError, match="empty"):
        timing.derive_guard([], 60_000)
    with pytest.raises(timing.TimingError, match="empty"):
        timing.timing_document(_identity(), [])


def test_a_guard_at_or_above_the_promoter_timeout_is_refused() -> None:
    assert timing.derive_guard([29_999.5], 60_000) == 59_999
    with pytest.raises(timing.TimingError, match="reaches"):
        timing.derive_guard([30_000.0], 60_000)
    with pytest.raises(timing.TimingError, match="reaches"):
        timing.derive_guard([45_000.0], 60_000)


@pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), float("inf")])
def test_a_nonpositive_or_nonfinite_total_is_refused(value: float) -> None:
    with pytest.raises(timing.TimingError, match="positive finite"):
        timing.derive_guard([10.0, value], 60_000)


def _sample(flags: list[bool], *, remaining: int = 0, total: float = 10.0) -> dict[str, object]:
    return {
        "sample": 1,
        "outcome": "elected",
        "transition": {"domain": "ingestion"},
        "attempts": [
            {"attempt": i + 1, "injected_conflict": flag, "total_ms": total}
            for i, flag in enumerate(flags)
        ],
        "disposition": {"removed": 10, "remaining": remaining},
    }


def test_a_sample_that_is_not_the_whole_envelope_is_refused() -> None:
    whole = [True] * _MAX_REDECIDE + [False]
    assert timing.validate_samples([_sample(whole)], _MAX_REDECIDE) == [10.0] * len(whole)
    for flags in (whole[1:], [*whole, False], [False, *whole[1:]]):
        with pytest.raises(timing.TimingError, match="conflicts then one activation"):
            timing.validate_samples([_sample(flags)], _MAX_REDECIDE)
    with pytest.raises(timing.TimingError, match="left probe rows"):
        timing.validate_samples([_sample(whole, remaining=1)], _MAX_REDECIDE)


def test_the_document_records_its_identity_and_derivation() -> None:
    whole = [True] * _MAX_REDECIDE + [False]
    document = timing.timing_document(_identity(code_sha="a" * 40), [_sample(whole, total=1000.25)])
    assert document["schema_version"] == "fresh-election-timing-v1"
    assert document["derivation"] == "ceil(2 * max(total_ms))"
    assert document["next_election_min_remaining_ms"] == 2001
    assert document["code_sha"] == "a" * 40
    assert document["sample_count"] == 1


def test_the_contract_rewrite_changes_only_the_guard_and_its_comment() -> None:
    text = timing.CONTRACT_PATH.read_text(encoding="utf-8")
    updated = timing.contract_with_guard(text, 8643, ".agents/x/fresh.json", "b" * 64)
    assert "next_election_min_remaining_ms: 8643\n" in updated
    assert "next_election_min_remaining_ms: 30000" not in updated
    assert ".agents/x/fresh.json" in updated and "b" * 64 in updated
    before = [line for line in text.splitlines() if "next_election" not in line and "#" not in line]
    after = [
        line for line in updated.splitlines() if "next_election" not in line and "#" not in line
    ]
    assert before == after
    with pytest.raises(timing.TimingError, match="exactly one guard"):
        timing.contract_with_guard(updated + updated, 1, "p", "s")


def test_the_artifact_path_is_repository_relative() -> None:
    path = pathlib.Path("/home/someone/repo/.agents/tasks/evidence/fresh-election-timing-v1.json")
    assert (
        timing.repository_relative(path) == ".agents/tasks/evidence/fresh-election-timing-v1.json"
    )
    with pytest.raises(timing.TimingError, match=r"\.agents"):
        timing.repository_relative(pathlib.Path("/tmp/fresh.json"))  # noqa: S108


# --- Probe-world custody ---------------------------------------------------------------------


def test_one_sample_seeds_rehearses_activates_once_and_returns_every_row() -> None:
    table = _ProbeTable()

    sample = timing.run_sample(1, _inputs(table))

    assert sample["outcome"] == "elected"
    attempts = cast("list[dict[str, Any]]", sample["attempts"])
    assert [item["injected_conflict"] for item in attempts] == [True] * _MAX_REDECIDE + [False]
    assert all(item["total_ms"] > 0 for item in attempts)
    assert {"arm_read_ms", "complete_walk_ms", "candidate_read_ms", "head_frontier_read_ms"} <= set(
        attempts[-1]
    )
    final = [  # the single-member puts are the seeding; only the activation is a transaction
        members
        for members in table.transactions
        if len(members) > 1 and any("SERVING_TRANSITION#" in str(member) for member in members)
    ]
    assert len(final) == 1 and len(final[0]) == 51
    disposition = sample["disposition"]
    assert isinstance(disposition, dict)
    assert disposition["remaining"] == 0 and disposition["removed"] > 0
    assert table.items == {}  # the probe table holds no row the sample wrote
    document = timing.timing_document(_identity(), [sample])
    assert document["next_election_min_remaining_ms"] == timing.derive_guard(
        [float(item["total_ms"]) for item in attempts], 60_000
    )


def test_a_colliding_probe_row_is_refused_and_only_the_seeded_rows_are_returned() -> None:
    table = _ProbeTable()
    foreign_key = (f"SERVING_DOMAIN#{INGESTION_RELEASE_DOMAIN}", "HEAD")
    foreign = {"pk": {"S": foreign_key[0]}, "sk": {"S": foreign_key[1]}, "owner": {"S": "other"}}
    table.items[foreign_key] = copy.deepcopy(foreign)

    with pytest.raises(timing.TimingError, match="already holds SERVING_DOMAIN#"):
        timing.run_sample(1, _inputs(table))

    assert table.items == {foreign_key: foreign}  # the foreign row is untouched, ours are gone


def test_a_failed_activation_still_disposes_every_row() -> None:
    @guard_reserved_words
    class _Failing(_ProbeTable):
        def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
            if len(kwargs["TransactItems"]) > 1 and "SERVING_TRANSITION#" in str(kwargs):
                raise ClientError({"Error": {"Code": "InternalServerError"}}, "TransactWrite")
            return super().transact_write_items(**kwargs)

    failing = _Failing()
    with pytest.raises(timing.TimingError, match="sample 1 failed"):
        timing.run_sample(1, _inputs(failing))
    assert failing.items == {}


_TRANSITION = "SERVING_TRANSITION#"


def _is_final(kwargs: dict[str, Any]) -> bool:
    return len(kwargs["TransactItems"]) > 1 and _TRANSITION in str(kwargs)


def _transition_key(kwargs: dict[str, Any]) -> tuple[str, str]:
    (key,) = [
        write.key
        for write in timing._writes(kwargs["TransactItems"])  # pyright: ignore[reportPrivateUsage]
        if write.key[0].startswith(_TRANSITION)
    ]
    return key


def _foreign(key: tuple[str, str]) -> dict[str, Any]:
    return {"pk": {"S": key[0]}, "sk": {"S": key[1]}, "owner": {"S": "another-probe"}}


def test_a_foreign_transition_row_found_by_the_preflight_is_never_written_or_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = _ProbeTable()
    placed: dict[tuple[str, str], Any] = {}
    strong_get = timing._strong_get  # pyright: ignore[reportPrivateUsage]

    def occupied(client: Any, name: str, key: tuple[str, str]) -> Any:
        """Place a foreign `SERVING_TRANSITION#` row before the preflight reads it."""
        if key[0].startswith(_TRANSITION) and not placed:
            placed[key] = _foreign(key)
            table.items[key] = copy.deepcopy(placed[key])
        return strong_get(client, name, key)

    monkeypatch.setattr(timing, "_strong_get", occupied)
    with pytest.raises(timing.TimingError, match=r"sample 1 failed \(ForeignRowError"):
        timing.run_sample(1, _inputs(table))
    assert placed and table.items == placed  # byte-identical; every row the sample owned is gone
    assert not [members for members in table.transactions if len(members) > 1]


def test_a_foreign_transition_row_that_wins_the_conditioned_put_stays_byte_identical() -> None:
    @guard_reserved_words
    class _Raced(_ProbeTable):
        """A foreign row lands after the preflight, so the activation's create is refused."""

        placed: dict[tuple[str, str], Any]

        def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
            if _is_final(kwargs):
                key = _transition_key(kwargs)
                self.placed = {key: _foreign(key)}
                self.items[key] = copy.deepcopy(self.placed[key])
            return super().transact_write_items(**kwargs)

    table = _Raced()
    with pytest.raises(timing.TimingError, match="sample 1 failed"):
        timing.run_sample(1, _inputs(table))
    assert table.items == table.placed


def test_an_ambiguous_commit_owns_only_the_rows_holding_its_own_exact_image() -> None:
    @guard_reserved_words
    class _LostResponse(_ProbeTable):
        def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
            if _is_final(kwargs):
                self.ambiguous_next = True  # commit, then raise as if the response were lost
            return super().transact_write_items(**kwargs)

    table = _LostResponse()
    with pytest.raises(timing.TimingError, match="sample 1 failed"):
        timing.run_sample(1, _inputs(table))
    assert table.items == {}  # every created row carried its own image, so each was owned


def test_an_ambiguous_failure_never_deletes_a_row_holding_another_image() -> None:
    @guard_reserved_words
    class _ForeignAfterTimeout(_ProbeTable):
        """The activation times out uncommitted while a foreign row takes its transition key."""

        placed: dict[tuple[str, str], Any]

        def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
            if _is_final(kwargs):
                key = _transition_key(kwargs)
                self.placed = {key: _foreign(key)}
                self.items[key] = copy.deepcopy(self.placed[key])
                raise ClientError({"Error": {"Code": "RequestTimeout"}}, "TransactWriteItems")
            return super().transact_write_items(**kwargs)

    table = _ForeignAfterTimeout()
    with pytest.raises(timing.TimingError, match="sample 1 failed"):
        timing.run_sample(1, _inputs(table))
    assert table.items == table.placed


def test_the_recording_client_owns_a_key_only_after_its_write_lands() -> None:
    table = _ProbeTable()
    recording = timing.RecordingClient(table, "probe")
    item = {"pk": {"S": "P#1"}, "sk": {"S": "S"}}
    recording.put_item(TableName="probe", Item=item, ConditionExpression="attribute_not_exists(pk)")
    assert recording.owned == {("P#1", "S")}
    refused = timing.RecordingClient(table, "probe")
    with pytest.raises(timing.ForeignRowError, match="already holds P#1/S"):
        refused.put_item(TableName="probe", Item=item)
    assert refused.owned == set() and refused.refused == [("P#1", "S")]
    seeded = timing.RecordingClient(table, "probe", frozenset({("P#1", "S")}))
    seeded.update_item(
        TableName="probe",
        Key={"pk": {"S": "P#1"}, "sk": {"S": "S"}},
        UpdateExpression="SET #v = :v",
        ExpressionAttributeNames={"#v": "v"},
        ExpressionAttributeValues={":v": {"S": "x"}},
    )
    assert seeded.owned == set()  # a seeded row is already the sample's; nothing new is claimed


def test_a_sample_that_met_an_unowned_row_is_refused() -> None:
    whole = [True] * _MAX_REDECIDE + [False]
    sample = _sample(whole)
    sample["disposition"] = {"removed": 10, "remaining": 0, "unowned_kept": [["P#1", "S"]]}
    with pytest.raises(timing.TimingError, match="does not own"):
        timing.validate_samples([sample], _MAX_REDECIDE)


def test_a_seeding_failure_is_not_reported_as_a_collision() -> None:
    @guard_reserved_words
    class _Unavailable(_ProbeTable):
        def put_item(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceeded"}}, "PutItem")

    with pytest.raises(timing.TimingError, match="could not seed"):
        timing.run_sample(1, _inputs(_Unavailable()))


def test_foreign_obligations_in_the_probe_due_shards_are_found() -> None:
    table = _ProbeTable()
    assert timing.foreign_obligations(table, "probe", due_shards()) == []
    for sk in ("ARM#run-1", "COMPLETE#run-2", "COALESCE#cohort", "RELEASE#run-3"):
        table.items[("DUE#3", sk)] = {"pk": {"S": "DUE#3"}, "sk": {"S": sk}}
    assert timing.foreign_obligations(table, "probe", due_shards()) == [
        ("DUE#3", "ARM#run-1"),
        ("DUE#3", "COALESCE#cohort"),
        ("DUE#3", "COMPLETE#run-2"),
    ]


class _Session:
    """A session whose clients answer the rehearsal's read-only identity checks."""

    def __init__(self, *, timeout: int = 60, mode: dict[str, Any] | None = None) -> None:
        self.timeout, self.mode = timeout, mode

    def client(self, name: str) -> Any:
        session = self

        class _CloudFormation:
            def get_paginator(self, operation: str) -> Any:
                assert operation == "list_stack_resources"

                class _Pages:
                    def paginate(self, **_kwargs: Any) -> Any:
                        yield {
                            "StackResourceSummaries": [
                                {
                                    "ResourceType": "AWS::Lambda::Function",
                                    "LogicalResourceId": "Stage1ExecutionGraphPromoterFixture",
                                    "PhysicalResourceId": "promoter-fixture",
                                },
                                {
                                    "ResourceType": "AWS::Lambda::Function",
                                    "LogicalResourceId": "Stage1ExecutionGraphReleaseFixture",
                                    "PhysicalResourceId": "release-fixture",
                                },
                            ]
                        }

                return _Pages()

        class _Lambda:
            def get_function_configuration(self, FunctionName: str) -> dict[str, Any]:  # noqa: N803
                assert FunctionName == "promoter-fixture"
                return {"Timeout": session.timeout}

        @guard_reserved_words
        class _Dynamo:
            def get_item(self, **_kwargs: Any) -> dict[str, Any]:
                return {} if session.mode is None else {"Item": session.mode}

        return {"cloudformation": _CloudFormation, "lambda": _Lambda, "dynamodb": _Dynamo}[name]()


def test_the_deployed_promoter_timeout_must_equal_the_contract() -> None:
    reading = timing._promoter_timeout(_Session(), "Adlytics-Ingestion-dev")  # pyright: ignore[reportPrivateUsage]
    assert reading == {
        "function_name": "promoter-fixture",
        "timeout_ms": 60_000,
        "contract_timeout_ms": 60_000,
    }
    with pytest.raises(timing.TimingError, match="differs from the contract"):
        timing._promoter_timeout(_Session(timeout=900), "Adlytics-Ingestion-dev")  # pyright: ignore[reportPrivateUsage]


def test_the_live_mode_must_be_held_graph_revision_12(monkeypatch: pytest.MonkeyPatch) -> None:
    from adapters.jobs.execution_graph import resolution  # noqa: PLC0415

    def resolved(*_args: object) -> str:
        return "graph-fixture"

    monkeypatch.setattr(resolution, "resolve_graph_table", resolved)
    held = {"mode": {"S": "GRAPH"}, "revision": {"N": "12"}, "hold": {"BOOL": True}}
    reading = timing._held_mode(_Session(mode=held), "dev", "store")  # pyright: ignore[reportPrivateUsage]
    assert (reading["mode"], reading["revision"], reading["hold"]) == ("GRAPH", 12, True)
    for mode in ({**held, "hold": {"BOOL": False}}, {**held, "revision": {"N": "13"}}, None):
        with pytest.raises(timing.TimingError, match="not held GRAPH revision 12"):
            timing._held_mode(_Session(mode=mode), "dev", "store")  # pyright: ignore[reportPrivateUsage]


def test_the_mode_row_is_never_seeded() -> None:
    items = {
        ("MODE#ingestion", "MODE"): {"pk": {"S": "MODE#ingestion"}, "sk": {"S": "MODE"}},
        ("RUN#x", "RUN"): {"pk": {"S": "RUN#x"}, "sk": {"S": "RUN"}},
    }
    table = _ProbeTable()
    assert timing.seed_world(table, "probe", items) == [("RUN#x", "RUN")]
    assert set(table.items) == {("RUN#x", "RUN")}
