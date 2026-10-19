# SPDX-License-Identifier: MIT-0
"""`FixedMemberDrainActivationByteFixtureV1`: the rolling final-drain/activation byte profiles.

Each profile is built only through production paths over `FakeDynamo`, which evaluates the
adapters' own condition and update expressions. A rolling cohort A (`a_size` granted runs, each
published through the live frontier) is elected by the real promoter. Cohort B (`b_size` runs)
publishes while A is ACTIVE. A's winner completes through the release sweep's direct drain,
and A's non-final aliases drain through the store. The real promoter pass then commits A's
fixed-member final transaction, which also activates B when B is nonempty. That one
`TransactWriteItems` request is captured with every target's exact strong pre-state and
resulting item.

`fixture(branch)` renders the frozen v1 fixture (the shape `byte_oracle_support.check_fixture`
reads). Totals are measured by the production `Stage1DynamoByteOracle`, never typed by hand.
The committed files under
`tests/fixtures/stage1-dynamodb-byte-oracle/FixedMemberDrainActivation/` are regenerated
only by an explicit, reviewed run of this module, and never by a test:

    uv run python tests/unit/fixed_member_byte_fixture_support.py --write

Tests rebuild each profile and require byte equality with the committed file, so a builder or
serialized-field change that moves a frozen total fails until the fixture is reviewed.
"""

from __future__ import annotations

import argparse
import copy
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from boto3.dynamodb.types import TypeDeserializer
from execution_graph_dispatch_support import NOW, TABLE, now_text, write_text
from execution_graph_release_support import context, granted_run, release_store
from execution_graph_support import FakeDynamo

from adapters.inbound.ingestion_handlers.graph_publish import publish_effect_record
from adapters.jobs.execution_graph.fence_settler import due_shards
from adapters.jobs.execution_graph.instantiate import ingestion_node_plan
from adapters.jobs.execution_graph.promoter import PromoterContext, arm_pass
from adapters.jobs.execution_graph.release import contribute, try_complete_granted
from adapters.outbound.dynamodb.byte_oracle import (
    MAX_AGGREGATE_TARGET_UPPER_BOUND_BYTES,
    MAX_CONSERVATIVE_SERVICE_ITEM_BYTES,
    MAX_DISTINCT_ITEM_ACTIONS,
    ConditionCheckAction,
    DeleteAction,
    ExactPreState,
    PutAction,
    Stage1DynamoByteOracle,
    TransactionAction,
    TransactionEnvelopeRequest,
    UpdateAction,
)
from adapters.outbound.dynamodb.execution_graph import due_shard
from adapters.outbound.dynamodb.semantic_state import PublishCounts
from domain.operations.execution_graph_dispatch import ArmScope, NodeState
from domain.operations.execution_graph_release import (
    INGESTION_RELEASE_DOMAIN,
    CohortContribution,
    CohortMember,
    CohortOutcome,
    alias_serving,
)
from domain.operations.status import canonical_authority_digest
from ports.outbound.execution_graph_dispatch import (
    NodeSettlement,
    ServingPublicationMutationResult,
    ServingPublicationSettlementRequest,
    SuccessorRequest,
)
from ports.outbound.execution_graph_release import (
    ServingAliasDrainRequest,
    ServingMutationResult,
)

__all__ = [
    "FIXTURE_DIR",
    "OPERATION",
    "PROFILES",
    "DrainActivationWorld",
    "FinalTransactionCapture",
    "build_world",
    "capture_final_transaction",
    "complete_direct_winner",
    "drain_nonfinal_aliases",
    "envelope_actions",
    "fixture",
    "fixture_path",
    "fixture_text",
    "grant_and_publish",
    "profile_capture",
    "promoter_pass",
    "publish_rolling",
    "regenerate",
]

OPERATION: Final = "FixedMemberDrainActivation"
FIXTURE_DIR: Final = (
    Path(__file__).resolve().parents[1] / "fixtures" / "stage1-dynamodb-byte-oracle" / OPERATION
)
FINAL_REQUEST_ID: Final = "fixture-final-transition"


@dataclass(frozen=True, slots=True)
class Profile:
    """One fixture branch: A's cohort size, B's successor size, and any one-beyond growth."""

    a_size: int
    b_size: int
    extra_condition_checks: int = 0


PROFILES: Final[dict[str, Profile]] = {
    # The protocol maximum: `serving_candidate_roster_max` = 512 on both cohorts.
    "maximum": Profile(512, 512),
    # The 98-candidate offline profile (the retained 98-root delivery shape).
    "candidates-98": Profile(98, 98),
    # `NO_ELIGIBLE`: A's final alias with no successor publication.
    "no-eligible-98": Profile(98, 0),
    # One action beyond: the 512 maximum plus 50 distinct ConditionChecks, 101 actions.
    "one-beyond": Profile(512, 512, extra_condition_checks=50),
}

_ORACLE: Final = Stage1DynamoByteOracle()
_DESERIALIZER: Final = TypeDeserializer()
_HIGH_REMAINING_MS: Final = 60_000

type Item = dict[str, Any]


def _unit(cohort: str, index: int) -> str:
    return canonical_authority_digest(["fixed-member-drain-activation", cohort, index])


def publish_rolling(fake: FakeDynamo, run_id: str) -> None:
    """Flip and settle `publish` through the production live-frontier path (no head read)."""
    store = release_store(fake)
    run = store.read_run(run_id)
    publish = store.read_node(run_id, "publish")
    if run is None or publish is None or run.grant_id is None or run.grant_ordinal is None:
        raise AssertionError("a rolling publication needs a granted run and its publish node.")
    target = canonical_authority_digest(["fixed-member-target", run_id])
    authority = store.read_serving_publication_authority(run_id)
    members, _marker_key, _marker = store.build_publication_flip_participant(
        authority, root_flow_id=run.root_flow_id, target_digest=target, flipped_at=now_text()
    )
    fake.transact_write_items(TransactItems=list(members))
    record, digest = publish_effect_record(
        (run.grant_id, run.grant_ordinal), PublishCounts(1, 0, 0, 0), 1
    )
    result = store.settle_publish(
        ServingPublicationSettlementRequest(
            settlement=NodeSettlement(
                run,
                publish,
                NodeState.SUCCEEDED,
                now_text(),
                effect_digest=digest,
                effect_record=record,
            ),
            successors=SuccessorRequest(run, (), ArmScope.SERVING, False, now_text()),
            authority=store.read_serving_publication_authority(run_id),
            target_digest=target,
        )
    )
    if result is not ServingPublicationMutationResult.APPLIED:
        raise AssertionError(f"the rolling publication did not apply: {result}.")


def grant_and_publish(fake: FakeDynamo, cohort: str, index: int) -> str:
    """Grant one run on its own conflict unit, then publish it on the live frontier."""
    run = granted_run(
        fake,
        unit=_unit(cohort, index),
        fingerprint=canonical_authority_digest(["fixed-member-fingerprint", cohort, index]),
        request_id=f"fixture-{cohort}-{index:04d}",
    )
    publish_rolling(fake, run)
    return run


def promoter_pass(
    fake: FakeDynamo,
    request_id: str,
    remaining_ms: Callable[[], int] = lambda: _HIGH_REMAINING_MS,
) -> None:
    """Run one real promoter arm pass at the fixed clock and re-raise its one isolated failure.

    The pass isolates each domain and reports a failure through its sink; a fixture caller sees
    that one reported failure raised, and more than one is a fixture error.
    """
    failures: list[BaseException] = []
    arm_pass(
        PromoterContext(
            release_store(fake), ingestion_node_plan, lambda: NOW, on_failure=failures.append
        ),
        due_shards(),
        remaining_ms,
        request_id,
    )
    if len(failures) > 1:
        raise AssertionError(f"one pass reported {len(failures)} failures.")
    if failures:
        raise failures[0]


def _head(fake: FakeDynamo) -> Item:
    return fake.items[(f"SERVING_DOMAIN#{INGESTION_RELEASE_DOMAIN}", "HEAD")]


def complete_direct_winner(fake: FakeDynamo, winner: str) -> None:
    """Record both contributions and complete the winner through the release sweep."""
    store = release_store(fake)
    record = store.read_run(winner)
    if record is None or record.grant_id is None:
        raise AssertionError("the direct winner is not granted.")
    grant, root = record.grant_id, record.root_flow_id
    for member in (CohortMember.PRECOMPUTE, CohortMember.BAKE):
        contribution = CohortContribution(grant, member, CohortOutcome.NO_ELIGIBLE, None, NOW, root)
        contribute(store, contribution, record)
    for name in ("precompute", "bake"):
        fake.items[(f"RUN#{winner}", f"NODE#{name}")]["state"] = write_text("SUCCEEDED")
    if try_complete_granted(context(fake), winner) is not ServingMutationResult.COMMITTED:
        raise AssertionError("the direct winner did not complete.")


def drain_nonfinal_aliases(fake: FakeDynamo) -> None:
    """Drain every alias but A's last, newest first, exactly as the promoter orders them."""
    store = release_store(fake)
    head = store.read_serving_domain(INGESTION_RELEASE_DOMAIN).head
    if head is None or head.state != "BOUND_DRAINING" or head.direct_grant_id is None:
        raise AssertionError("A must be draining.")
    direct = store.read_serving_binding(head.direct_grant_id)
    if direct is None:
        raise AssertionError("A's direct binding is absent.")
    coalesce = fake.items[(due_shard(INGESTION_RELEASE_DOMAIN), f"COALESCE#{head.cohort_revision}")]
    found = [store.read_serving_candidate(str(value["S"])) for value in coalesce["run_ids"]["L"]]
    reads = sorted(
        (item for item in found if item is not None),
        key=lambda item: item.candidate.grant_ordinal,
        reverse=True,
    )
    if len(reads) != len(found):
        raise AssertionError("every undrained A member must remain a serving candidate.")
    # A non-final alias drain condition-checks the head and never moves it, so one strong read
    # of the head and of the candidates serves every drain in turn.
    for index, current in enumerate(reads[:-1]):
        alias = alias_serving(direct, current.candidate, direct.cohort_revision or "", bound_at=NOW)
        store.drain_alias_binding(
            ServingAliasDrainRequest(
                head,
                direct,
                alias,
                current.candidate,
                current.identity,
                now_text(),
                tuple(item.candidate.run_id for item in reads[index + 1 :]),
                "fixture-alias-drain",
            )
        )


@dataclass(frozen=True, slots=True)
class DrainActivationWorld:
    """A world whose next promoter pass commits A's fixed-member final transaction."""

    fake: FakeDynamo
    a_runs: tuple[str, ...]
    b_runs: tuple[str, ...]

    @property
    def final_alias(self) -> str:
        """A's lowest-ordinal member: the alias the final transaction drains."""
        return self.a_runs[0]


def build_world(a_size: int, b_size: int) -> DrainActivationWorld:
    """Elect A, publish B while A is ACTIVE, and drain A down to its final alias."""
    if a_size < 2:
        raise ValueError("cohort A needs a direct winner and a final alias.")
    fake = FakeDynamo()
    a_runs = tuple(grant_and_publish(fake, "a", index) for index in range(a_size))
    promoter_pass(fake, "fixture-elect-a")
    head = _head(fake)
    if head["state"] != write_text("ACTIVE") or head["winner_run_id"] != write_text(a_runs[-1]):
        raise AssertionError("A's newest member must be its ACTIVE winner.")
    b_runs = tuple(grant_and_publish(fake, "b", index) for index in range(b_size))
    complete_direct_winner(fake, a_runs[-1])
    drain_nonfinal_aliases(fake)
    return DrainActivationWorld(fake, a_runs, b_runs)


@dataclass(frozen=True, slots=True)
class FinalTransactionCapture:
    """The one captured final transaction: its request and every target's exact images."""

    request: dict[str, Any]
    pre_states: tuple[Item | None, ...]
    results: tuple[Item | None, ...]


def _target(member: dict[str, Any]) -> tuple[str, str]:
    ((kind, body),) = member.items()
    key = body["Item"] if kind == "Put" else body["Key"]
    return (str(key["pk"]["S"]), str(key["sk"]["S"]))


def capture_final_transaction(
    world: DrainActivationWorld,
    request_id: str = FINAL_REQUEST_ID,
    remaining_ms: Callable[[], int] = lambda: _HIGH_REMAINING_MS,
) -> FinalTransactionCapture | None:
    """Run one promoter pass and capture the `SERVING_TRANSITION#` transaction, if it commits."""
    fake = world.fake
    original = fake.transact_write_items
    captured: list[FinalTransactionCapture] = []

    def recording(**kwargs: Any) -> dict[str, Any]:
        members: list[dict[str, Any]] = kwargs["TransactItems"]
        final = any(_target(member)[0].startswith("SERVING_TRANSITION#") for member in members)
        before = [copy.deepcopy(fake.items.get(_target(member))) for member in members]
        answer = original(**kwargs)
        if final:
            captured.append(
                FinalTransactionCapture(
                    copy.deepcopy({"TransactItems": members}),
                    tuple(before),
                    tuple(copy.deepcopy(fake.items.get(_target(member))) for member in members),
                )
            )
        return answer

    fake.transact_write_items = recording  # type: ignore[method-assign]
    try:
        promoter_pass(fake, request_id, remaining_ms)
    finally:
        fake.transact_write_items = original  # type: ignore[method-assign]
    if len(captured) > 1:
        raise AssertionError("one pass committed two final transactions.")
    return captured[0] if captured else None


def _plain(item: Item | None) -> dict[str, object] | None:
    if item is None:
        return None
    return {name: _DESERIALIZER.deserialize(value) for name, value in item.items()}


def _with_extra_checks(capture: FinalTransactionCapture, count: int) -> FinalTransactionCapture:
    """Grow the captured request by `count` distinct absent-item ConditionChecks."""
    members = list(capture.request["TransactItems"])
    extra = [
        {
            "ConditionCheck": {
                "TableName": TABLE,
                "Key": {
                    "pk": write_text(f"FIXTURE_ONE_BEYOND#{index:03d}"),
                    "sk": write_text("CHECK"),
                },
                "ConditionExpression": "attribute_not_exists(pk)",
            }
        }
        for index in range(count)
    ]
    return FinalTransactionCapture(
        {"TransactItems": [*members, *extra]},
        (*capture.pre_states, *(None,) * count),
        (*capture.results, *(None,) * count),
    )


def envelope_actions(
    request: dict[str, Any],
    pre_states: tuple[Item | None, ...],
    results: tuple[Item | None, ...],
) -> tuple[TransactionAction, ...]:
    """Map a low-level request and its exact images onto the production oracle's actions."""
    actions: list[TransactionAction] = []
    for member, pre, result in zip(request["TransactItems"], pre_states, results, strict=True):
        ((kind, body),) = member.items()
        table = str(body["TableName"])
        key = {
            name: _DESERIALIZER.deserialize(body.get("Key", body.get("Item"))[name])
            for name in ("pk", "sk")
        }
        prior = ExactPreState(_plain(pre))
        if kind == "Put":
            actions.append(PutAction(table, key, cast("dict[str, object]", _plain(result)), prior))
        elif kind == "Delete":
            actions.append(DeleteAction(table, key, prior))
        elif kind == "ConditionCheck":
            actions.append(ConditionCheckAction(table, key, prior))
        else:
            after = cast("dict[str, object]", _plain(result))
            before = prior.item or {}
            changed = {
                name: value
                for name, value in after.items()
                if name not in key and before.get(name) != value
            }
            if set(before) - set(after):
                raise AssertionError("a final-transaction update removed an attribute.")
            actions.append(UpdateAction(table, key, changed, prior))
    return tuple(actions)


def profile_capture(
    branch: str, cache: dict[tuple[int, int], FinalTransactionCapture] | None = None
) -> FinalTransactionCapture:
    """Build one branch's world and capture its final transaction (one world per cohort pair)."""
    profile = PROFILES[branch]
    pair = (profile.a_size, profile.b_size)
    capture = None if cache is None else cache.get(pair)
    if capture is None:
        capture = capture_final_transaction(build_world(*pair))
        if capture is None:
            raise AssertionError(f"profile {branch} committed no final transaction.")
        if cache is not None:
            cache[pair] = capture
    if profile.extra_condition_checks:
        capture = _with_extra_checks(capture, profile.extra_condition_checks)
    return capture


def fixture(branch: str, capture: FinalTransactionCapture | None = None) -> dict[str, Any]:
    """Render one branch's v1 fixture from a fresh production capture."""
    profile = PROFILES[branch]
    capture = capture or profile_capture(branch)
    actions = envelope_actions(capture.request, capture.pre_states, capture.results)
    targets: list[dict[str, Any]] = []
    for pre, result in zip(capture.pre_states, capture.results, strict=True):
        pre_bytes = 0 if pre is None else _ORACLE.conservative_service_item_bytes(_plain(pre) or {})
        result_bytes = (
            0 if result is None else _ORACLE.conservative_service_item_bytes(_plain(result) or {})
        )
        targets.append(
            {
                "expected_pre_state_map": pre,
                "expected_result_map": result,
                "pre_state_conservative_bytes": pre_bytes,
                "result_conservative_bytes": result_bytes,
                "target_upper_bound": max(pre_bytes, result_bytes),
            }
        )
    action_count = _ORACLE.action_count(TransactionEnvelopeRequest(actions))
    aggregate = sum(int(target["target_upper_bound"]) for target in targets)
    refused = (
        action_count > MAX_DISTINCT_ITEM_ACTIONS
        or any(
            int(target["target_upper_bound"]) > MAX_CONSERVATIVE_SERVICE_ITEM_BYTES
            for target in targets
        )
        or aggregate > MAX_AGGREGATE_TARGET_UPPER_BOUND_BYTES
    )
    return {
        "fixture_version": "v1",
        "fixture_schema": "FixedMemberDrainActivationByteFixtureV1",
        "typed_operation": OPERATION,
        "branch": branch,
        "table_name": TABLE,
        "typed_input": {
            "a_candidates": profile.a_size,
            "b_candidates": profile.b_size,
            "elected": profile.b_size > 0,
            "extra_condition_checks": profile.extra_condition_checks,
        },
        "expected_request": capture.request,
        "targets": targets,
        "expected": {
            "action_count": action_count,
            "aggregate_upper_bound": aggregate,
            "largest_target_upper_bound": max(int(t["target_upper_bound"]) for t in targets),
            "wire_json_bytes": _ORACLE.wire_json_bytes(capture.request),
            "outcome": "refused" if refused else "accepted",
        },
    }


def fixture_text(value: dict[str, Any]) -> str:
    """Render the committed file text: sorted keys, one-space indent, trailing newline."""
    return json.dumps(value, indent=1, sort_keys=True, ensure_ascii=True) + "\n"


def fixture_path(branch: str) -> Path:
    """Return the committed file of one branch."""
    return FIXTURE_DIR / f"{branch}-v1.json"


def regenerate(*, write: bool) -> dict[str, dict[str, object]]:
    """Rebuild every branch, then write it (reviewed use only) or report committed drift."""
    cache: dict[tuple[int, int], FinalTransactionCapture] = {}
    report: dict[str, dict[str, object]] = {}
    for branch in PROFILES:
        value = fixture(branch, profile_capture(branch, cache))
        text = fixture_text(value)
        path = fixture_path(branch)
        if write:
            FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        expected = value["expected"]
        report[branch] = {
            "action_count": expected["action_count"],
            "aggregate_upper_bound": expected["aggregate_upper_bound"],
            "largest_target_upper_bound": expected["largest_target_upper_bound"],
            "outcome": expected["outcome"],
            "matches_committed": path.is_file() and path.read_text(encoding="utf-8") == text,
        }
    return report


def _main() -> int:
    parser = argparse.ArgumentParser(description="FixedMemberDrainActivationByteFixtureV1")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="rewrite the committed fixtures")
    mode.add_argument("--check", action="store_true", help="fail on any committed-fixture drift")
    arguments = parser.parse_args()
    report = regenerate(write=arguments.write)
    print(json.dumps(report, indent=2, sort_keys=True))  # noqa: T201
    return 0 if all(item["matches_committed"] for item in report.values()) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
