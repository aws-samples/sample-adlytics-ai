# SPDX-License-Identifier: MIT-0
"""The run-request decision table and its guards (S1).

Covers V-1 to V-8, V-21, V-22, V-27, the decision half of V-29, BF-6, BF-7, BF-9, and the S2
hard deny (a) and (b).

Each of the 20 rows has one named test through the generated table (V-6), then the epoch-1
effects are driven through the real service and adapter against the transactional fake.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from execution_graph_support import FakeDynamo, FakeManifests, fixed_now

from adapters.inbound.run_requests.service import (
    AdmissionDependencies,
    RunRequest,
    admit_run_request,
)
from adapters.jobs.execution_graph.instantiate import require_receipt, shadow_graph_for
from adapters.outbound.dynamodb.execution_graph import DynamoExecutionGraphStore, due_shard
from domain.operations.execution_graph import (
    AuthorityMode,
    GraphCommandRefusedError,
    graph_id,
    run_id,
)
from domain.operations.generated_execution_graphs import DECISION_TABLE, RETRY_ROW_APPROVED
from domain.operations.ingestion_root import window_identity
from domain.operations.run_request import (
    ACTIVE_COMPUTE_NOT_REPLACEABLE,
    ACTIVE_RUN_GRANTED,
    RUN_REQUEST_WAIT_SLOT_CONFLICT,
    ActiveRun,
    AdmissionClass,
    AdmissionGuardError,
    CallerKind,
    DecisionInputs,
    DecisionTableError,
    FingerprintMatch,
    MatchState,
    RunState,
    Waiter,
    check_decision_table,
    decide,
    require_replaceable,
    require_sole_waiter,
)
from ports.outbound.execution_graph import ReceiptRecord

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-074")]

R = datetime(2026, 10, 6, 9, 30, tzinfo=UTC)
OLDER = R - timedelta(seconds=5)
NEWER = R + timedelta(seconds=5)
F = "fingerprint"


def inputs(**overrides: object) -> DecisionInputs:
    base = DecisionInputs(
        request_digest="digest",
        receipt_digest=None,
        epoch=1,
        admitting=True,
        hold=False,
        caller=CallerKind.ON_DEMAND,
        reason="operator_rerun",
        admission_class=AdmissionClass.CADENCE,
        version_count=1,
        blocked=False,
        fingerprint=F,
        r=R,
        active=None,
        waiter=None,
        match=None,
        head=None,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def active(**overrides: object) -> ActiveRun:
    base = ActiveRun("run-a", RunState.OPEN, "other", OLDER, False, None, 3)
    return replace(base, **overrides)  # type: ignore[arg-type]


def waiter(**overrides: object) -> Waiter:
    base = Waiter("request-w", "waiter-f", OLDER, False, 2)
    return replace(base, **overrides)  # type: ignore[arg-type]


ROWS: list[tuple[int, str, DecisionInputs, str | None]] = [
    (1, "RETURN_RECEIPT", inputs(receipt_digest="digest"), None),
    (2, "REFUSED", inputs(receipt_digest="other"), "request_id_conflict"),
    (3, "REFUSED", inputs(admitting=False, epoch=0), "RUN_ADMISSION_NOT_ACTIVE"),
    (4, "REFUSED", inputs(admission_class=AdmissionClass.HISTORICAL), "FULL_HISTORY_NOT_ENABLED"),
    (5, "REFUSED", inputs(version_count=2), "manifest_key_rewritten"),
    (6, "REFUSED", inputs(blocked=True), "conflict_unit_blocked"),
    (7, "JOINED", inputs(active=active(fingerprint=F, r=R)), None),
    (8, "JOINED", inputs(waiter=waiter(fingerprint=F, r=R)), None),
    (9, "JOINED", inputs(match=FingerprintMatch(MatchState.SUCCEEDED, "run-s")), None),
    (
        10,
        "REFUSED",
        inputs(reason="data_correction", match=FingerprintMatch(MatchState.FAILED, "run-f")),
        "correction_requires_new_input",
    ),
    (
        12,
        "REFUSED",
        inputs(match=FingerprintMatch(MatchState.CANCELLED, "run-c")),
        "fingerprint_terminal",
    ),
    (13, "REFUSED", inputs(head=NEWER), "older_than_current"),
    (14, "REFUSED", inputs(waiter=waiter(r=NEWER)), "older_than_waiting"),
    (15, "REFUSED", inputs(active=active(r=NEWER)), "older_than_active"),
    (
        16,
        "QUEUED_LATEST",
        inputs(hold=True, caller=CallerKind.DELIVERY_EVENT, reason="delivery_arrived"),
        "automatic_admission_held",
    ),
    (17, "REPLACED_BEFORE_COMPUTE", inputs(active=active()), None),
    (
        18,
        "REPLACED_ON_INVALIDATION",
        inputs(reason="data_correction", active=active(compute_started=True)),
        None,
    ),
    (19, "QUEUED_LATEST", inputs(active=active(compute_started=True)), None),
    (20, "ADMITTED", inputs(), None),
]


@pytest.mark.parametrize(
    ("row", "decision", "given", "reason"), ROWS, ids=[f"row_{r[0]}" for r in ROWS]
)
def test_v6_each_row_has_its_named_case(  # noqa: PLR0913, PLR0917 -- one parametrized row
    row: int, decision: str, given: DecisionInputs, reason: str | None
) -> None:
    found = decide(DECISION_TABLE, given)
    assert (found.row, found.decision, found.reason) == (row, decision, reason)


def test_v6_row_11_sets_retry_identity_only_and_continues_to_row_13() -> None:
    assert RETRY_ROW_APPROVED is True
    failed = FingerprintMatch(MatchState.FAILED, "run-f", generation=2, issued_grant=False)
    admitted = decide(DECISION_TABLE, inputs(match=failed))
    assert (admitted.row, admitted.decision, admitted.retry_generation) == (20, "ADMITTED", 3)
    # V-22 negatives: a grant-issuing failure, another caller, another reason: row 12.
    for given in (
        inputs(match=replace(failed, issued_grant=True)),
        inputs(match=failed, caller=CallerKind.SCHEDULE, reason="scheduled_cadence"),
        inputs(match=failed, reason="bounded_validation"),
    ):
        refused = decide(DECISION_TABLE, given)
        assert (refused.row, refused.reason, refused.retry_generation) == (
            12,
            "fingerprint_terminal",
            None,
        )
    # An old retry: refused by row 13 or 15, never a second slot holder.
    assert decide(DECISION_TABLE, inputs(match=failed, head=NEWER)).row == 13
    assert decide(DECISION_TABLE, inputs(match=failed, active=active(r=NEWER))).row == 15
    # With an older computing A, the retry queues (row 19) as generation g+1.
    queued = decide(DECISION_TABLE, inputs(match=failed, active=active(compute_started=True)))
    assert (queued.row, queued.retry_generation) == (19, 3)


def test_v6_the_generated_table_is_total_and_every_gap_fails_generation() -> None:
    check_decision_table(DECISION_TABLE, retry_approved=True)
    without_row = tuple(row for row in DECISION_TABLE if row["row"] != 15)
    with pytest.raises(DecisionTableError, match="DECISION_TABLE_NOT_TOTAL"):
        check_decision_table(without_row, retry_approved=True)
    declined = tuple(row for row in DECISION_TABLE if row["row"] != 11)
    check_decision_table(declined, retry_approved=False)
    with pytest.raises(DecisionTableError, match="DECISION_TABLE_NOT_TOTAL"):
        check_decision_table(declined, retry_approved=True)
    misplaced = tuple(
        {**row, "epoch1_only": False} if row["row"] == 17 else row for row in DECISION_TABLE
    )
    with pytest.raises(DecisionTableError, match="epoch-1 only"):
        check_decision_table(misplaced, retry_approved=True)
    deciding_retry = tuple(
        {k: v for k, v in row.items() if k != "continue_to"} if row["row"] == 11 else row
        for row in DECISION_TABLE
    )
    with pytest.raises(DecisionTableError, match="only row 11 continues"):
        check_decision_table(deciding_retry, retry_approved=True)
    # An A state the table does not cover is refused where it is constructed.
    with pytest.raises(DecisionTableError, match="not covered"):
        active(state=RunState.FENCING)


def test_v6_rows_16_to_19_never_match_in_epoch_0() -> None:
    for given in (
        inputs(epoch=0, hold=True, caller=CallerKind.DELIVERY_EVENT, reason="delivery_arrived"),
        inputs(epoch=0),
    ):
        assert decide(DECISION_TABLE, given).row == 20


def test_v1_on_demand_outside_shadow_and_graph_is_refused_with_a_receipt_decision() -> None:
    for caller, reason in (
        (CallerKind.ON_DEMAND, "operator_rerun"),
        (CallerKind.DELIVERY_EVENT, "delivery_arrived"),
        (CallerKind.SCHEDULE, "scheduled_cadence"),
    ):
        found = decide(
            DECISION_TABLE, inputs(admitting=False, epoch=0, caller=caller, reason=reason)
        )
        assert (found.row, found.reason) == (3, "RUN_ADMISSION_NOT_ACTIVE")
    assert decide(DECISION_TABLE, inputs(epoch=0)).decision == "ADMITTED"


def test_v21_run_state_and_waiter_rules() -> None:
    paused = decide(DECISION_TABLE, inputs(active=active(state=RunState.PAUSED)))
    assert (paused.row, paused.decision) == (19, "QUEUED_LATEST")
    fencing = decide(DECISION_TABLE, inputs(match=FingerprintMatch(MatchState.FENCING, "run-x")))
    assert (fencing.row, fencing.reason) == (12, "fingerprint_terminal")
    held = waiter(fingerprint=F, r=R, held=True)
    on_demand = decide(DECISION_TABLE, inputs(waiter=held))
    assert (on_demand.row, on_demand.decision) == (20, "ADMITTED")
    automatic = decide(
        DECISION_TABLE,
        inputs(waiter=held, caller=CallerKind.DELIVERY_EVENT, reason="delivery_arrived"),
    )
    assert (automatic.row, automatic.decision) == (8, "JOINED")
    open_run = decide(DECISION_TABLE, inputs(active=active()))
    assert open_run.decision == "REPLACED_BEFORE_COMPUTE"


def test_v27_legacy_completed_input_joins_with_no_compute_in_epoch_1_only() -> None:
    legacy = FingerprintMatch(MatchState.LEGACY_COMPLETED, legacy_plan_key="WRITE_PLAN#p#e")
    joined = decide(DECISION_TABLE, inputs(match=legacy))
    assert (joined.row, joined.decision) == (9, "JOINED")
    rerun = decide(DECISION_TABLE, inputs(match=legacy, reason="operator_rerun"))
    assert rerun.row == 9
    assert decide(DECISION_TABLE, inputs(match=None)).row == 20


def test_v29_a_correction_against_a_granted_active_run_queues_by_row_19() -> None:
    granted = active(compute_started=True, grant_id="grant-1")
    found = decide(DECISION_TABLE, inputs(reason="data_correction", active=granted))
    assert (found.row, found.decision) == (19, "QUEUED_LATEST")
    with pytest.raises(AdmissionGuardError) as raised:
        require_replaceable(granted, invalidating=True)
    assert raised.value.signal == ACTIVE_RUN_GRANTED


def test_bf9_replacing_a_computing_run_without_invalidation_is_a_guard() -> None:
    computing = active(compute_started=True)
    with pytest.raises(AdmissionGuardError) as raised:
        require_replaceable(computing, invalidating=False)
    assert raised.value.signal == ACTIVE_COMPUTE_NOT_REPLACEABLE
    require_replaceable(active(), invalidating=False)


def test_bf7_a_second_waiter_without_supersession_is_a_guard() -> None:
    with pytest.raises(AdmissionGuardError) as raised:
        require_sole_waiter(waiter(), supersedes=False)
    assert raised.value.signal == RUN_REQUEST_WAIT_SLOT_CONFLICT
    require_sole_waiter(waiter(), supersedes=True)
    require_sole_waiter(None, supersedes=False)


def test_bf6_instantiation_without_an_admitting_receipt_is_refused() -> None:
    for receipt in (None,):
        with pytest.raises(GraphCommandRefusedError) as raised:
            require_receipt(receipt)
        assert raised.value.signal == "RUN_REQUEST_RECEIPT_REQUIRED"
    with pytest.raises(GraphCommandRefusedError):
        shadow_graph_for(None, "ingestion_manifest_to_served.v1")


# --- Epoch-1 effects through the service and the adapter ---------------------------------------

WINDOW = (
    # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    "amc/dsp_impressions/query_pack=amc-dsp-impressions/source_scope=s/marketplace=ATVPDKIKX0DER/"
    "window=2026-10-04_2026-10-06/options=44136fa355b3678a/"
)


def key(instant: str) -> str:
    return f"{WINDOW}retrieved={instant}/manifest.json"


class _NoLegacy:
    def completed_plan_key(self, key: str, version_id: str) -> str | None:
        del key, version_id
        return None

    def head_after(self, window: str, r: datetime) -> datetime | None:
        del window, r
        return None


def graph_store(mode: AuthorityMode) -> tuple[FakeDynamo, DynamoExecutionGraphStore]:
    client = FakeDynamo()
    client.items[("MODE#ingestion", "MODE")] = {
        "pk": {"S": "MODE#ingestion"},
        "sk": {"S": "MODE"},
        "mode": {"S": mode.value},
        "revision": {"N": "4"},
    }
    return client, DynamoExecutionGraphStore("graph", client)  # type: ignore[arg-type]


def admit(
    store: DynamoExecutionGraphStore, request_id: str, manifest_key: str, **extra: object
) -> ReceiptRecord:
    request = RunRequest(
        request_id=request_id,
        protocol_version="v1",
        caller_kind=str(extra.pop("caller_kind", "on_demand")),
        reason=str(extra.pop("reason", "operator_rerun")),
        manifest_key=manifest_key,
        version_id=str(extra.pop("version_id", "v1")),
        requester="arn:aws:iam::000000000000:user/operator",
        requester_is_iam=True,
        via_api_group=bool(extra.pop("via_api_group", False)),
    )
    return admit_run_request(
        request,
        AdmissionDependencies(
            store=store,
            manifests=FakeManifests(retrieved_at=window_identity(manifest_key).retrieved_at),
            now=fixed_now,
            legacy=_NoLegacy(),
        ),
    )


def test_epoch_1_admission_writes_a_run_its_slot_and_its_instantiation_obligation() -> None:
    client, store = graph_store(AuthorityMode.GRAPH)
    receipt = admit(store, "request-0001", key("20261006T092520Z"))
    assert (receipt.decision, receipt.authority_epoch) == ("ADMITTED", 1)
    unit = window_identity(key("20261006T092520Z")).conflict_unit
    assert client.items[(f"SLOT#{unit}", "SLOT")]["active_run_id"] == {"S": receipt.run_id}
    assert (due_shard(unit), f"INSTANTIATE#{receipt.run_id}") in client.items
    assert receipt.run_id == run_id(receipt.fingerprint, 1, 0)


def test_v5_an_epoch_1_request_never_joins_an_epoch_0_shadow_graph() -> None:
    client, store = graph_store(AuthorityMode.SHADOW)
    shadow = admit(store, "request-0001", key("20261006T092520Z"))
    graph_before = dict(client.items[(f"GRAPH#{shadow.graph_id}", "GRAPH")])
    client.items[("MODE#ingestion", "MODE")]["mode"] = {"S": "GRAPH"}
    epoch_1 = admit(store, "request-0002", key("20261006T092520Z"))
    assert (epoch_1.decision, epoch_1.authority_epoch) == ("ADMITTED", 1)
    assert epoch_1.graph_id != shadow.graph_id
    assert shadow.graph_id == graph_id(shadow.fingerprint, 0, 0)
    assert client.items[(f"GRAPH#{shadow.graph_id}", "GRAPH")] == graph_before
    # Within epoch 0 the same fingerprint joins.
    client.items[("MODE#ingestion", "MODE")]["mode"] = {"S": "SHADOW"}
    assert admit(store, "request-0003", key("20261006T092520Z")).decision == "JOINED"


def test_epoch_1_a_newer_input_replaces_an_uncomputed_run_and_moves_it_to_fencing() -> None:
    client, store = graph_store(AuthorityMode.GRAPH)
    first = admit(store, "request-0001", key("20261006T092520Z"))
    second = admit(store, "request-0002", key("20261006T092521Z"))
    assert second.decision == "REPLACED_BEFORE_COMPUTE"
    assert client.items[(f"RUN#{first.run_id}", "RUN")]["state"] == {"S": "FENCING"}
    unit = window_identity(key("20261006T092520Z")).conflict_unit
    assert client.items[(f"SLOT#{unit}", "SLOT")]["active_run_id"] == {"S": second.run_id}


def test_bf9_v10_a_newer_input_queues_behind_a_computing_run_and_supersedes_the_old_waiter() -> (
    None
):
    client, store = graph_store(AuthorityMode.GRAPH)
    first = admit(store, "request-0001", key("20261006T092520Z"))
    client.items[(f"RUN#{first.run_id}", "RUN")]["compute_started_at"] = {
        "S": "2026-10-07T09:01:00Z"
    }
    queued = admit(store, "request-0002", key("20261006T092521Z"))
    newer = admit(store, "request-0003", key("20261006T092522Z"))
    assert (queued.decision, newer.decision) == ("QUEUED_LATEST", "QUEUED_LATEST")
    unit = window_identity(key("20261006T092520Z")).conflict_unit
    assert client.items[(due_shard(unit), f"WAITER#{unit}")]["request_id"] == {"S": "request-0003"}
    assert client.items[("RECEIPT#request-0002", "SUPERSEDED")]["reason"] == {
        "S": "by=request-0003"
    }
    assert client.items[(f"RUN#{first.run_id}", "RUN")]["state"] == {"S": "OPEN"}


def test_epoch_1_a_correction_replaces_a_computing_ungranted_run_on_invalidation() -> None:
    client, store = graph_store(AuthorityMode.GRAPH)
    first = admit(store, "request-0001", key("20261006T092520Z"))
    client.items[(f"RUN#{first.run_id}", "RUN")]["compute_started_at"] = {
        "S": "2026-10-07T09:01:00Z"
    }
    corrected = admit(
        store, "request-0002", key("20261006T092521Z"), reason="data_correction", via_api_group=True
    )
    assert corrected.decision == "REPLACED_ON_INVALIDATION"
    client2, store2 = graph_store(AuthorityMode.GRAPH)
    granted = admit(store2, "request-0001", key("20261006T092520Z"))
    run = client2.items[(f"RUN#{granted.run_id}", "RUN")]
    run["compute_started_at"] = {"S": "2026-10-07T09:01:00Z"}
    run["grant_id"] = {"S": "grant-1"}
    queued = admit(
        store2,
        "request-0002",
        key("20261006T092521Z"),
        reason="data_correction",
        via_api_group=True,
    )
    assert queued.decision == "QUEUED_LATEST"
    assert run["state"] == {"S": "OPEN"}


def test_v21a_a_correction_against_a_paused_computing_run_queues_on_the_first_pass() -> None:
    """S1A-5: row 18 needs OPEN, so a PAUSED computing run queues at row 19 and never livelocks."""
    client, store = graph_store(AuthorityMode.GRAPH)
    first = admit(store, "request-0001", key("20261006T092520Z"))
    run = client.items[(f"RUN#{first.run_id}", "RUN")]
    run["compute_started_at"] = {"S": "2026-10-07T09:01:00Z"}
    run["state"] = {"S": "PAUSED"}
    before = len(client.transactions)
    queued = admit(
        store, "request-0002", key("20261006T092521Z"), reason="data_correction", via_api_group=True
    )
    assert (queued.row, queued.decision) == (19, "QUEUED_LATEST")
    # Bounded re-decide: one decision, one committed transaction, no failed pass.
    assert len(client.transactions) == before + 1
    assert run["state"] == {"S": "PAUSED"}
    computing = active(compute_started=True)
    assert (
        decide(DECISION_TABLE, inputs(reason="data_correction", active=computing)).decision
        == "REPLACED_ON_INVALIDATION"
    )
    paused = active(compute_started=True, state=RunState.PAUSED)
    assert decide(DECISION_TABLE, inputs(reason="data_correction", active=paused)).row == 19


def test_row_8_receipt_names_the_joined_waiter_not_a_supersession() -> None:
    """S1A-11: JOINED(W) records the waiter in joined_request_id."""
    client, store = graph_store(AuthorityMode.GRAPH)
    first = admit(store, "request-0001", key("20261006T092520Z"))
    client.items[(f"RUN#{first.run_id}", "RUN")]["compute_started_at"] = {"S": "now"}
    queued = admit(store, "request-0002", key("20261006T092521Z"))
    joined = admit(store, "request-0003", key("20261006T092521Z"))
    assert (queued.decision, joined.row, joined.decision) == ("QUEUED_LATEST", 8, "JOINED")
    assert joined.joined_request_id == "request-0002"
    assert joined.superseded_request_id is None
    stored = client.items[("RECEIPT#request-0003", "RECEIPT")]
    assert stored["joined_request_id"] == {"S": "request-0002"}
    assert "superseded_request_id" not in stored


def test_v22_a_row_11_retry_receipt_links_the_failed_run() -> None:
    """S1A-12: the retry receipt names the pre-grant failed run it retries."""
    client, store = graph_store(AuthorityMode.GRAPH)
    first = admit(store, "request-0001", key("20261006T092520Z"))
    client.items[(f"RUN#{first.run_id}", "RUN")]["state"] = {"S": "FAILED"}
    unit = window_identity(key("20261006T092520Z")).conflict_unit
    del client.items[(f"SLOT#{unit}", "SLOT")]
    retry = admit(store, "request-0002", key("20261006T092520Z"))
    assert (retry.decision, retry.retry_generation) == ("ADMITTED", 1)
    assert retry.retried_run_id == first.run_id
    assert retry.root_flow_id == first.root_flow_id
    assert retry.run_id == run_id(first.fingerprint, 1, 1)
    assert first.retried_run_id is None


def test_a_guard_race_re_reads_and_re_decides() -> None:
    client, store = graph_store(AuthorityMode.GRAPH)
    first = admit(store, "request-0001", key("20261006T092520Z"))

    def compute_starts(fake: FakeDynamo) -> None:
        fake.items[(f"RUN#{first.run_id}", "RUN")]["compute_started_at"] = {"S": "now"}

    client.before_transaction = compute_starts
    # Read as OPEN without compute (row 17); compute starts before commit; re-decided to row 19.
    second = admit(store, "request-0002", key("20261006T092521Z"))
    assert second.decision == "QUEUED_LATEST"
    assert client.items[(f"RUN#{first.run_id}", "RUN")]["state"] == {"S": "OPEN"}


def test_hard_deny_a_full_history_label_and_a_derived_historical_class() -> None:
    client, store = graph_store(AuthorityMode.SHADOW)
    request = RunRequest(
        request_id="request-0001",
        protocol_version="v1",
        caller_kind="on_demand",
        reason="bounded_validation",
        manifest_key=key("20261006T092520Z"),
        version_id="v1",
        requester="arn:aws:iam::000000000000:user/operator",
        requester_is_iam=True,
        label="full_history",
    )
    with pytest.raises(GraphCommandRefusedError) as raised:
        admit_run_request(
            request,
            AdmissionDependencies(store, FakeManifests(retrieved_at=R), fixed_now),
        )
    assert raised.value.signal == "FULL_HISTORY_NOT_ENABLED"
    assert client.kinds() == ["MODE/MODE"]
    refused = admit(store, "request-0002", key("20261006T092300Z"))
    assert (refused.decision, refused.reason) == ("REFUSED", "FULL_HISTORY_NOT_ENABLED")
    assert client.kinds() == ["MODE/MODE", "RECEIPT/RECEIPT"]
    # Positive control: a bounded validation of a phase-A probe slot is admitted.
    assert (
        admit(store, "request-0003", key("20261006T092520Z"), reason="bounded_validation").decision
        == "ADMITTED"
    )


def test_v2_data_correction_only_from_an_api_requester_in_the_group() -> None:
    _, store = graph_store(AuthorityMode.SHADOW)
    with pytest.raises(GraphCommandRefusedError) as raised:
        admit(store, "request-0001", key("20261006T092520Z"), reason="data_correction")
    assert raised.value.signal == "RUN_REQUEST_REASON_NOT_PERMITTED"
    accepted = admit(
        store, "request-0002", key("20261006T092520Z"), reason="data_correction", via_api_group=True
    )
    assert accepted.decision == "ADMITTED"
