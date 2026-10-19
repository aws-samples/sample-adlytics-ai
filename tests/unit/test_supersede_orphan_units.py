# SPDX-License-Identifier: MIT-0
"""TD-50: the reviewed orphan-unit disposition proves supersession per unit and fails closed.

The table is the transactional `FakeDynamo`, which evaluates the command's own condition and
update expressions; immutable objects are a dictionary of exact bytes. Every identity is
fictional.
"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import copy
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError
from dynamodb_reserved_words import guard_reserved_words
from execution_graph_support import FakeDynamo
from phase_one_support import NOW, source

from adapters.jobs.supersede_orphan_units import __main__ as supersede
from adapters.jobs.supersede_orphan_units.evidence import (
    PlanRows,
    SourceFacts,
    Unproven,
    orphan_units,
    projection_evidence,
    source_evidence,
)
from adapters.jobs.verify_stage1_preswitch.checks import execution_evidence
from adapters.outbound.s3.semantic_storage import source_object_key, source_publication_payload
from domain.operations.write_plan import (
    META_RECORD_KEY,
    SUCCESSOR_OWNER_RECORD_KEY,
    intent_partition_key,
    owner_partition_key,
    plan_partition_key,
    unit_record_key,
)
from domain.semantics.canonical_compiler import canonical_checksum_digest, canonical_rows_checksum

pytestmark = [pytest.mark.unit]

TABLE = "adlytics-test-v2-semantic-publications"
QUEUE = "https://sqs.us-east-1.amazonaws.com/000000000000/adlytics-test-semantic-write.fifo"
DLQ = "https://sqs.us-east-1.amazonaws.com/000000000000/adlytics-test-semantic-write-dlq.fifo"
TARGETS = supersede.Targets(TABLE, "bucket-fictional", QUEUE, DLQ)
AT = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
CREATED = "01790000000000000000"
LATER = "01791000000000000000"
CURRENT_UNIT = "m_current.fct_alpha"
SOURCE_UNIT = "z_source.sa_alpha"


def _hex(seed: str) -> str:
    return (seed * 64)[:64]


def _low(value: object) -> dict[str, Any]:
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, int):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, list):
        return {"L": [_low(member) for member in cast("list[object]", value)]}
    if isinstance(value, dict):
        members = cast("dict[object, object]", value)
        return {"M": {str(name): _low(member) for name, member in members.items()}}
    raise TypeError(type(value).__name__)


def _item(**attributes: object) -> dict[str, Any]:
    return {name: _low(value) for name, value in attributes.items()}


@guard_reserved_words
@dataclass
class _Table(FakeDynamo):
    """`FakeDynamo` plus the one prefix-filtered scan the command issues."""

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        prefix = kwargs["ExpressionAttributeValues"][":prefix"]["S"]
        assert kwargs["ConsistentRead"] is True
        return {
            "Items": [
                copy.deepcopy(item)
                for (partition, _), item in sorted(self.items.items())
                if partition.startswith(prefix)
            ]
        }


class _Queues:
    def __init__(self, depth: int = 0) -> None:
        self.depth = depth

    def get_queue_attributes(self, **kwargs: Any) -> dict[str, Any]:
        visible = self.depth if kwargs["QueueUrl"] == QUEUE else 0
        return {
            "Attributes": {
                "ApproximateNumberOfMessages": str(visible),
                "ApproximateNumberOfMessagesNotVisible": "0",
                "ApproximateNumberOfMessagesDelayed": "0",
            }
        }


@dataclass(frozen=True)
class _Plan:
    seed: str

    @property
    def digest(self) -> str:
        return _hex(self.seed)

    @property
    def intent(self) -> str:
        return _hex(f"{self.seed}1")

    @property
    def key(self) -> str:
        return plan_partition_key(self.digest, _hex(f"{self.seed}2"))


class _World:
    def __init__(self) -> None:
        self.table = _Table(key_names=("partition_key", "record_key"))
        self.objects: dict[str, bytes] = {}
        self.queues = _Queues()

    def clients(self) -> supersede.Clients:
        return supersede.Clients(self.table, self.queues, self.objects.__getitem__)

    def put(self, partition: str, record: str, **attributes: object) -> None:
        self.table.items[partition, record] = _item(
            partition_key=partition, record_key=record, **attributes
        )

    def plan(  # noqa: PLR0913 -- one fixture builder with explicit knobs
        self,
        plan: _Plan,
        units: dict[str, dict[str, object]],
        *,
        status: str = "planned",
        intent_state: str | None = "admitted",
        owner_active: bool = True,
    ) -> None:
        attested = sum(unit["status"] == "attested" for unit in units.values())
        meta: dict[str, object] = {
            "plan_digest": plan.digest,
            "origin_intent_id": plan.intent,
            "generation": 0,
            "status": status,
            "unit_count": len(units),
            "admitted_count": len(units),
            "attested_count": attested,
            "failed_count": 0,
            "created_at": CREATED,
        }
        if status != "planned":
            meta["terminal_at"] = LATER
        self.put(plan.key, META_RECORD_KEY, **meta)
        for unit_key, unit in units.items():
            self.put(plan.key, unit_record_key(unit_key), unit_key=unit_key, **unit)
        if intent_state is not None:
            self.put(
                intent_partition_key(plan.digest, plan.intent), META_RECORD_KEY, state=intent_state
            )
        owner: dict[str, object] = {"revision": 4}
        if owner_active:
            owner |= {"active_plan_key": plan.key, "active_intent_id": plan.intent}
        self.put(owner_partition_key(plan.digest), SUCCESSOR_OWNER_RECORD_KEY, **owner)

    def current(self, partition: str, publication: str, token: int) -> None:
        self.put(
            partition,
            "CURRENT",
            publication_id=publication,
            projected_publication_id=publication,
            projected_fencing_token=token,
        )

    def canonical_object(  # noqa: PLR0913 -- one fixture builder with explicit knobs
        self,
        partition: str,
        publication: str,
        token: int,
        *,
        destination: str = "successful",
        entry_partition: bool = True,
        entry_checksum: str | None = None,
    ) -> list[object]:
        """Store one immutable canonical object and return its unit entry."""
        reference = f"immutable/canonical/{destination}/fct_alpha/{publication}.json"
        checksum = canonical_rows_checksum(())
        self.objects[reference] = json.dumps(
            {
                "version": 1,
                "destination": destination,
                "publication_id": publication,
                "attempt_id": "attempt-fictional",
                "partition_key": partition,
                "publisher": "publisher-fictional",
                "fencing_token": token,
                "source_publications": ["src-fictional"],
                "source_revision": {
                    "scheme": "scheme-fictional",
                    "precision": "precision-fictional",
                    "observed_at": NOW.isoformat(),
                    "source_id": "source-fictional",
                },
                "content_checksum": checksum,
                "rule_set_version": "semantic-v2.1",
                "evidence": {},
                "created_at": NOW.isoformat(),
                "rows": [],
            }
        ).encode()
        return [
            "canonical",
            reference,
            entry_checksum or canonical_checksum_digest(checksum),
            publication,
            partition if entry_partition else None,
            token,
        ]

    def source_object(self, reference: str, *, days: int) -> list[object]:
        publication = replace(source(reference), published_at=NOW + timedelta(days=days))
        self.objects[source_object_key(reference)] = json.dumps(
            source_publication_payload(publication)
        ).encode()
        digest = canonical_checksum_digest(publication.content_checksum)
        return ["source", reference, digest, reference, None, 1]

    def item(self, partition: str, record: str) -> dict[str, Any]:
        return self.table.items[partition, record]


def _queued(table_kind: str, objects: list[list[object]], **extra: object) -> dict[str, object]:
    return {
        "status": "queued",
        "admission": "admitted",
        "table_kind": table_kind,
        "merge_count": 1,
        "objects": objects,
        "flow_id": "flow-fictional",
        "owner_generation": 0,
        "created_at": CREATED,
        "admitted_at": CREATED,
        **extra,
    }


PROJECTED = _Plan("a")
SOURCED = _Plan("b")
NEWER = _Plan("c")
BEHIND = _Plan("d")
LIVE = _Plan("e")


def _world() -> _World:
    """Build a projected and a source-superseded orphan, an unproven one and a live one."""
    world = _World()
    world.current("partition-alpha", "publication-newer", 9)
    world.plan(
        PROJECTED,
        {
            CURRENT_UNIT: _queued(
                "current", [world.canonical_object("partition-alpha", "publication-old", 3)]
            )
        },
    )
    world.plan(
        SOURCED, {SOURCE_UNIT: _queued("source_aligned", [world.source_object("src-old", days=0)])}
    )
    newer = world.source_object("src-new", days=18)
    world.plan(
        NEWER,
        {
            SOURCE_UNIT: {
                **_queued("source_aligned", [newer]),
                "status": "attested",
                "created_at": LATER,
            }
        },
        status="completed",
        owner_active=False,
    )
    world.current("partition-beta", "publication-beta", 2)
    world.plan(
        BEHIND,
        {
            "m_current.fct_beta": _queued(
                "current", [world.canonical_object("partition-beta", "publication-b", 5)]
            )
        },
    )
    world.plan(
        LIVE,
        {"m_current.fct_gamma": {**_queued("current", []), "status": "writing"}},
    )
    return world


def _by_plan(document: Any) -> dict[str, Any]:
    return {plan["plan_key"]: plan for plan in document["plans"]}


def _run(world: _World, *, apply: bool) -> tuple[Any, Any]:
    return supersede.disposition(world.clients(), TARGETS, apply=apply, now=AT)


def _drain(world: _World) -> Any:
    """Read the table as `verify-drain` does: its execution evidence over every item."""
    return execution_evidence(cast("Any", list(world.table.items.values())))


# --- The decision rules ---------------------------------------------------------------------------


def test_projection_rule_needs_the_pointer_at_or_past_the_publication() -> None:
    def current(publication: str | None, token: int) -> dict[str, object]:
        return {"projected_publication_id": publication, "projected_fencing_token": token}

    assert projection_evidence("p", "pub", 3, current("newer", 4))[1] is None
    assert projection_evidence("p", "pub", 3, current("pub", 3))[1] is None
    # Controls: behind, an equal token naming another publication, and no projection at all.
    assert projection_evidence("p", "pub", 3, current("older", 2))[1] is Unproven.PROJECTION_BEHIND
    assert (
        projection_evidence("p", "pub", 3, current("other", 3))[1]
        is Unproven.PROJECTION_OTHER_PUBLICATION
    )
    assert projection_evidence("p", "pub", 3, current("NONE", 9))[1] is Unproven.PROJECTION_ABSENT
    assert projection_evidence("p", "pub", 3, None)[1] is Unproven.CURRENT_ABSENT


def _facts(**overrides: Any) -> SourceFacts:
    base: dict[str, Any] = {
        "reference": "src-old",
        "plan_key": "plan-old",
        "unit_key": SOURCE_UNIT,
        "scope": '{"scope":"a"}',
        "coverage_start": "2026-08-20",
        "coverage_end": "2026-08-22",
        "published_at": NOW,
    }
    return SourceFacts(**(base | overrides))


def test_source_rule_needs_a_newer_append_of_the_same_scope_covering_the_window() -> None:
    orphan = _facts()
    newer = _facts(reference="src-new", plan_key="plan-new", published_at=NOW + timedelta(days=1))
    proof, reason = source_evidence(orphan, [newer])
    assert reason is None
    assert cast("Any", proof)["newer_attested_append"]["reference"] == "src-new"
    # Controls: not newer, another scope, a narrower window, another table, the same plan.
    for candidate in (
        replace(newer, published_at=NOW),
        replace(newer, scope='{"scope":"b"}'),
        replace(newer, coverage_start="2026-08-21"),
        replace(newer, unit_key="z_source.sa_other"),
        replace(newer, plan_key="plan-old"),
    ):
        assert source_evidence(orphan, [candidate]) == (None, Unproven.NO_NEWER_APPEND)


def test_only_an_orphan_shaped_plan_yields_orphan_units() -> None:
    meta = {
        "status": "planned",
        "unit_count": 1,
        "admitted_count": 1,
        "attested_count": 0,
        "failed_count": 0,
    }
    unit = _queued("current", [])
    rows = PlanRows("plan", meta, {CURRENT_UNIT: unit}, {"state": "admitted"})
    assert orphan_units(rows) == ((unit,), None)
    assert orphan_units(replace(rows, intent=None))[1] is None
    assert orphan_units(replace(rows, intent={"state": "claimed"}))[1] is Unproven.INTENT_LIVE
    begun = {CURRENT_UNIT: {**unit, "unit_reservation_count": 1}}
    assert orphan_units(replace(rows, units=begun))[1] is Unproven.UNIT_SHAPE
    # FEAT-011 gate 2: a reservation no attested sibling accounts for is refused by name.
    reserved = {**meta, "plan_reservation_count": 1}
    assert orphan_units(replace(rows, meta=reserved))[1] is Unproven.RESERVATIONS_UNACCOUNTED
    done = {**meta, "status": "completed", "terminal_at": LATER}
    assert orphan_units(replace(rows, meta=done))[1] is Unproven.NOT_PLANNED


# --- Dry run, apply and the fail-closed paths -----------------------------------------------------


def test_a_dry_run_records_every_unit_and_writes_nothing() -> None:
    world = _world()
    before = copy.deepcopy(world.table.items)
    document, commit = _run(world, apply=False)
    assert commit is None
    assert world.table.items == before
    assert world.table.transactions == []
    plans = _by_plan(document)
    assert document["counts"] == {
        "plans_scanned": 5,
        "planned_plans": 4,
        "plans_proven": 2,
        "plans_left_untouched": 2,
        "orphan_units": 4,
        "units_proven": 2,
        "units_unproven": 2,
    }
    assert document["complete"] is False
    (projected,) = plans[PROJECTED.key]["units"]
    assert projected["evidence"]["rule"] == "projection_at_or_past"
    assert projected["evidence"]["objects"][0]["projected_fencing_token"] == 9
    (sourced,) = plans[SOURCED.key]["units"]
    assert sourced["evidence"]["rule"] == "newer_attested_source_append"
    proof = sourced["evidence"]["objects"][0]
    assert proof["newer_attested_append"]["plan_key"] == NEWER.key
    assert plans[BEHIND.key]["decision"] == "left_untouched"
    assert plans[BEHIND.key]["units"][0]["unproven"] == "projection_behind_publication"
    assert plans[LIVE.key]["unproven"] == "unit_not_orphan_shape"
    assert NEWER.key not in plans


def test_apply_supersedes_only_proven_plans_and_verify_drain_then_reads_them_terminal() -> None:
    world = _world()
    document, commit = _run(world, apply=True)
    assert commit is not None
    outcome = commit()
    assert outcome["outcomes"] == {PROJECTED.key: "superseded", SOURCED.key: "superseded"}
    assert outcome["complete"] is False  # two planned plans stay unproven
    digests = {
        plan["plan_key"]: plan["units"][0]["evidence_digest"]
        for plan in document["plans"]
        if plan["decision"] == "supersede"
    }
    for plan, unit_key in ((PROJECTED, CURRENT_UNIT), (SOURCED, SOURCE_UNIT)):
        unit = world.item(plan.key, unit_record_key(unit_key))
        assert unit["status"] == {"S": "superseded"}
        assert unit["supersession_evidence_digest"] == {"S": digests[plan.key]}
        assert unit["terminal_at"] == {"S": f"{int(AT.timestamp() * 1_000_000):020d}"}
        meta = world.item(plan.key, META_RECORD_KEY)
        assert meta["status"] == {"S": "superseded"}
        assert "terminal_at" in meta
        owner = world.item(owner_partition_key(plan.digest), SUCCESSOR_OWNER_RECORD_KEY)
        assert owner["revision"] == {"N": "5"}
        assert "active_plan_key" not in owner
    # The unproven and the live plans are untouched.
    assert world.item(BEHIND.key, META_RECORD_KEY)["status"] == {"S": "planned"}
    assert world.item(LIVE.key, unit_record_key("m_current.fct_gamma"))["status"] == {
        "S": "writing"
    }
    evidence = _drain(world)
    assert sorted(evidence.nonterminal_plans) == sorted(
        f"{plan.key}/META" for plan in (BEHIND, LIVE)
    )
    assert sorted(evidence.nonterminal_units) == sorted(
        (
            f"{BEHIND.key}/{unit_record_key('m_current.fct_beta')}",
            f"{LIVE.key}/{unit_record_key('m_current.fct_gamma')}",
        )
    )


def test_verify_drain_reads_a_superseded_unit_without_its_evidence_as_nonterminal() -> None:
    world = _world()
    key = (PROJECTED.key, unit_record_key(CURRENT_UNIT))
    world.table.items[key]["status"] = {"S": "superseded"}
    evidence = _drain(world)
    assert f"{key[0]}/{key[1]}" in evidence.nonterminal_units
    # Control: with a canonical evidence digest it is terminal.
    world.table.items[key]["supersession_evidence_digest"] = {"S": "f" * 64}
    evidence = _drain(world)
    assert f"{key[0]}/{key[1]}" not in evidence.nonterminal_units


def test_apply_is_refused_while_a_semantic_write_message_exists() -> None:
    world = _world()
    world.queues.depth = 1
    document, commit = _run(world, apply=True)
    assert commit is None
    assert document["apply_refused"] == "semantic_write_queue_not_empty"
    assert world.table.transactions == []


def _proven_order(document: Any) -> list[str]:
    return [plan["plan_key"] for plan in document["plans"] if plan["decision"] == "supersede"]


def test_a_message_that_appears_between_plans_stops_the_apply_before_the_next_plan() -> None:
    """FEAT-010 review MEDIUM: the queues are re-read before every plan's transaction.

    A message arrives while the first plan commits (after the assessment's queue read). The
    second plan is not attempted and stays `planned`; the evidence for both was written first.
    """
    world = _world()
    document, commit = _run(world, apply=True)
    assert commit is not None
    first, second = _proven_order(document)

    def message_arrives(_table: FakeDynamo) -> None:
        world.queues.depth = 1

    world.table.before_transaction = message_arrives
    outcome = commit()
    assert outcome["outcomes"] == {first: "superseded"}
    assert outcome["stopped"] == "semantic_write_queue_not_empty"
    assert outcome["queue_depth_at_stop"] == {"semantic_write_queue": 1, "semantic_write_dlq": 0}
    assert outcome["not_attempted"] == [second]
    assert (outcome["committed"], outcome["complete"]) == (1, False)
    assert len(world.table.transactions) == 1
    assert world.item(first, META_RECORD_KEY)["status"] == {"S": "superseded"}
    assert world.item(second, META_RECORD_KEY)["status"] == {"S": "planned"}
    # Per-unit evidence for every proven plan, the stopped one included, predates the commit.
    plans = _by_plan(document)
    assert all(plans[key]["units"][0]["evidence_digest"] for key in (first, second))


def test_the_command_reserves_its_outcome_file_and_records_a_stop_in_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world()
    reserved: list[Any] = []
    real_disposition = supersede.disposition

    class _Session:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def client(self, _name: str) -> Any:
            return SimpleNamespace(get_caller_identity=lambda: {"Account": "000000000000"})

    def assessed(*_args: object, **_kwargs: object) -> tuple[Any, Any]:
        document, commit = real_disposition(world.clients(), TARGETS, apply=True, now=AT)
        assert commit is not None

        def observed_commit() -> Any:
            # The outcome file already exists, pending, before the first transaction.
            reserved.extend(path.name for path in tmp_path.iterdir() if "outcome" in path.name)
            return commit()

        return document, observed_commit

    def message_arrives(_table: FakeDynamo) -> None:
        world.queues.depth = 1

    def targets(*_args: object) -> supersede.Targets:
        return TARGETS

    world.table.before_transaction = message_arrives
    monkeypatch.setattr(supersede, "_targets", targets)
    monkeypatch.setattr(supersede, "disposition", assessed)
    code = supersede.main(
        [
            "--profile",
            "operator",
            "--env",
            "test",
            "--region",
            "us-east-1",
            "--expected-account",
            "000000000000",
            "--evidence-dir",
            str(tmp_path),
            "--apply",
        ],
        session_factory=_Session,
        clock=lambda: AT,
        run_id=lambda: "run",
    )
    assert code == 1
    (outcome_path,) = (path for path in tmp_path.iterdir() if "outcome" in path.name)
    assert reserved == [outcome_path.name]
    outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
    assert outcome["stopped"] == "semantic_write_queue_not_empty"
    assert outcome["committed"] == 1
    assert outcome["evidence"].endswith("-evidence.json")


def test_a_unit_that_moved_after_the_read_is_refused_and_left_as_it_moved() -> None:
    world = _world()
    _, commit = _run(world, apply=True)
    assert commit is not None
    key = (PROJECTED.key, unit_record_key(CURRENT_UNIT))

    def claimed_by_a_writer(table: FakeDynamo) -> None:
        table.items[key]["status"] = {"S": "writing"}

    world.table.before_transaction = claimed_by_a_writer
    outcome = commit()
    assert outcome["outcomes"][PROJECTED.key] == "refused_state_moved"
    assert outcome["outcomes"][SOURCED.key] == "superseded"
    assert world.table.items[key]["status"] == {"S": "writing"}
    assert world.item(PROJECTED.key, META_RECORD_KEY)["status"] == {"S": "planned"}


def test_a_cancellation_that_is_not_a_condition_check_stops_the_apply() -> None:
    """FEAT-010 review MEDIUM: a conflict cancellation is not evidence that the plan moved.

    It is recorded `failed` with its codes, never `refused_state_moved`, and the run stops.
    """
    world = _world()
    document, commit = _run(world, apply=True)
    assert commit is not None
    first, second = _proven_order(document)
    world.table.conflicts_next = 1
    outcome = commit()
    assert outcome["outcomes"] == {first: "failed"}
    assert "refused_state_moved" not in outcome["outcomes"].values()
    assert outcome["stopped"] == "transaction_failed"
    assert outcome["failed_plan"] == first
    assert outcome["error_code"] == "TransactionCanceledException"
    assert set(outcome["cancellation_codes"]) == {"TransactionConflict"}
    assert outcome["not_attempted"] == [second]
    assert (outcome["committed"], outcome["complete"]) == (0, False)
    assert world.table.conflicts_raised == 1
    assert world.table.transactions == []
    for plan in (first, second):
        assert world.item(plan, META_RECORD_KEY)["status"] == {"S": "planned"}


def test_a_persistent_client_error_stops_after_the_first_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FEAT-010 review MEDIUM: an outage is attempted once, not once per proven plan."""
    world = _world()
    document, commit = _run(world, apply=True)
    assert commit is not None
    first, second = _proven_order(document)
    attempts: list[object] = []

    def unavailable(**kwargs: Any) -> dict[str, Any]:
        attempts.append(kwargs)
        raise ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "injected"}},
            "TransactWriteItems",
        )

    monkeypatch.setattr(world.table, "transact_write_items", unavailable)
    outcome = commit()
    assert len(attempts) == 1
    assert outcome["outcomes"] == {first: "failed"}
    assert outcome["stopped"] == "transaction_failed"
    assert (outcome["failed_plan"], outcome["error_class"], outcome["error_code"]) == (
        first,
        "ClientError",
        "InternalServerError",
    )
    assert outcome["cancellation_codes"] == []
    assert outcome["not_attempted"] == [second]
    assert (outcome["committed"], outcome["complete"]) == (0, False)


def test_an_unreadable_candidate_object_leaves_its_plan_untouched() -> None:
    world = _world()
    entry = world.canonical_object(
        "partition-alpha", "publication-cand", 3, destination="candidate", entry_partition=False
    )
    stored = world.objects.pop(str(entry[1]))
    world.plan(_Plan("f"), {"a_candidate.fct_alpha": _queued("candidate", [entry])})
    document, _ = _run(world, apply=False)
    (unit,) = _by_plan(document)[_Plan("f").key]["units"]
    assert unit["unproven"] == "immutable_object_unreadable"
    # Control: a readable candidate object resolves its partition and the projection proves it.
    world.objects[str(entry[1])] = stored
    document, _ = _run(world, apply=False)
    (unit,) = _by_plan(document)[_Plan("f").key]["units"]
    proof = unit["evidence"]["objects"][0]
    assert proof["partition"] == "partition-alpha"
    assert proof["content_checksum"] == entry[2]


@pytest.mark.parametrize("table_kind", ["current", "candidate"])
def test_a_canonical_object_whose_only_disagreement_is_its_checksum_is_unproven(
    table_kind: str,
) -> None:
    world = _world()
    destination = "successful" if table_kind == "current" else "candidate"
    plan = _Plan("8")

    def put(entry_checksum: str | None) -> list[object]:
        entry = world.canonical_object(
            "partition-alpha",
            "publication-g",
            3,
            destination=destination,
            entry_checksum=entry_checksum,
        )
        world.plan(plan, {f"m_{table_kind}.fct_alpha": _queued(table_kind, [entry])})
        return entry

    # Destination, publication, token and partition all agree; only the checksum differs.
    put(_hex("9"))
    document, _ = _run(world, apply=False)
    (unit,) = _by_plan(document)[plan.key]["units"]
    assert unit["unproven"] == "immutable_object_disagrees_with_unit"
    assert _by_plan(document)[plan.key]["decision"] == "left_untouched"
    # Control: the matching checksum proves it, and the proof records that checksum.
    entry = put(None)
    document, _ = _run(world, apply=False)
    (unit,) = _by_plan(document)[plan.key]["units"]
    assert unit["evidence"]["objects"][0]["content_checksum"] == entry[2]


def test_a_newer_publication_on_an_earlier_created_plan_still_proves_the_source_rule() -> None:
    world = _world()
    newer = world.item(NEWER.key, unit_record_key(SOURCE_UNIT))
    # The attested plan unit was created before the orphan's; its publication is still later.
    newer["created_at"] = {"S": "01780000000000000000"}
    document, _ = _run(world, apply=False)
    (sourced,) = _by_plan(document)[SOURCED.key]["units"]
    assert sourced["evidence"]["rule"] == "newer_attested_source_append"
    assert sourced["evidence"]["objects"][0]["newer_attested_append"]["plan_key"] == NEWER.key


def test_an_evidence_file_is_never_replaced(tmp_path: Path) -> None:
    first = supersede._write(tmp_path, "proof.json", {"run": 1})
    with pytest.raises(FileExistsError):
        supersede._write(tmp_path, "proof.json", {"run": 2})
    assert json.loads(first.read_text(encoding="utf-8")) == {"run": 1}


def test_two_invocations_in_one_second_keep_both_proofs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Session:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def client(self, _name: str) -> Any:
            return SimpleNamespace(get_caller_identity=lambda: {"Account": "000000000000"})

    runs = iter(range(3))

    def assessed(*_args: object, **_kwargs: object) -> tuple[dict[str, object], None]:
        counts = {"run": next(runs)}
        return {"mode": "dry_run", "counts": counts, "queue_depth": {}, "complete": True}, None

    def targets(*_args: object) -> supersede.Targets:
        return TARGETS

    monkeypatch.setattr(supersede, "_targets", targets)
    monkeypatch.setattr(supersede, "disposition", assessed)
    argv = [
        "--profile",
        "operator",
        "--env",
        "test",
        "--region",
        "us-east-1",
        "--expected-account",
        "000000000000",
        "--evidence-dir",
        str(tmp_path),
    ]

    def invoke(run_id: str) -> int:
        return supersede.main(
            argv, session_factory=_Session, clock=lambda: AT, run_id=lambda: run_id
        )

    assert invoke("first") == 0
    assert invoke("second") == 0
    proofs = sorted(tmp_path.iterdir())
    assert [json.loads(path.read_text(encoding="utf-8"))["counts"] for path in proofs] == [
        {"run": 0},
        {"run": 1},
    ]
    # Control: a colliding name is refused, not overwritten, and nothing is committed after it.
    assert invoke("first") == 1
    assert sorted(tmp_path.iterdir()) == proofs
    assert json.loads(proofs[0].read_text(encoding="utf-8"))["counts"] == {"run": 0}


# --- The command ----------------------------------------------------------------------------------


def test_the_command_is_a_dry_run_unless_apply_is_given() -> None:
    parser = supersede.build_parser()
    shared = [
        "--profile",
        "operator",
        "--env",
        "test",
        "--region",
        "us-east-1",
        "--expected-account",
        "000000000000",
        "--evidence-dir",
        "evidence",
    ]
    assert parser.parse_args(shared).apply is False
    assert parser.parse_args([*shared, "--apply"]).apply is True
    with pytest.raises(SystemExit):
        parser.parse_args([*shared[:-2]])
    with pytest.raises(SystemExit):
        parser.parse_args(["--profile", "default", *shared[2:]])


def test_the_command_refuses_another_account_before_any_other_client(tmp_path: Path) -> None:
    created: list[str] = []

    class _Session:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def client(self, name: str) -> Any:
            created.append(name)
            return SimpleNamespace(get_caller_identity=lambda: {"Account": "111111111111"})

    code = supersede.main(
        [
            "--profile",
            "operator",
            "--env",
            "test",
            "--region",
            "us-east-1",
            "--expected-account",
            "000000000000",
            "--evidence-dir",
            str(tmp_path),
            "--apply",
        ],
        session_factory=_Session,
    )
    assert code == 1
    assert created == ["sts"]
