# SPDX-License-Identifier: MIT-0
"""The receipt transaction (BF-10), the waiter rule in the store (BF-7) and receipt epochs (V-18).

Every test drives the real DynamoDB adapter against `FakeDynamo`, which evaluates the adapter's
own condition and update expressions transactionally.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from execution_graph_support import FakeDynamo, FakeManifests, fixed_now

from adapters.inbound.run_requests.service import (
    AdmissionDependencies,
    RunRequest,
    admit_run_request,
)
from adapters.outbound.dynamodb.execution_graph import DynamoExecutionGraphStore, due_shard
from domain.operations.execution_graph import (
    AuthorityMode,
    GraphCommandRefusedError,
    ModeState,
)
from domain.operations.ingestion_root import RootRefusedError, window_identity
from domain.operations.run_request import Waiter
from ports.outbound.execution_graph import (
    AdmissionCommit,
    AdmissionConflictError,
    CommandReceipt,
    EffectKind,
    ModeChange,
    RunRequestNotRecordedError,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-074")]

WINDOW = (
    # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    "amc/dsp_impressions/query_pack=amc-dsp-impressions/source_scope=s/marketplace=ATVPDKIKX0DER/"
    "window=2026-10-04_2026-10-06/options=44136fa355b3678a/"
)
PROBE_KEY = f"{WINDOW}retrieved=20261006T092520Z/manifest.json"
HISTORICAL_KEY = f"{WINDOW}retrieved=20261006T092300Z/manifest.json"


def _store(
    mode: AuthorityMode | None = AuthorityMode.SHADOW,
) -> tuple[FakeDynamo, DynamoExecutionGraphStore]:
    client = FakeDynamo()
    if mode is not None:
        client.items[("MODE#ingestion", "MODE")] = {
            "pk": {"S": "MODE#ingestion"},
            "sk": {"S": "MODE"},
            "mode": {"S": mode.value},
            "revision": {"N": "1"},
        }
    return client, DynamoExecutionGraphStore("graph", client)  # type: ignore[arg-type]


def request(request_id: str = "request-0001", key: str = PROBE_KEY, **overrides: Any) -> RunRequest:
    base = RunRequest(
        request_id=request_id,
        protocol_version="v1",
        caller_kind="on_demand",
        reason="operator_rerun",
        manifest_key=key,
        version_id="version-1",
        requester="arn:aws:iam::000000000000:user/operator",
        requester_is_iam=True,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def deps(
    store: DynamoExecutionGraphStore, key: str = PROBE_KEY, **manifest: Any
) -> AdmissionDependencies:
    retrieved = window_identity(key).retrieved_at
    return AdmissionDependencies(
        store=store,
        manifests=FakeManifests(retrieved_at=retrieved, **manifest),  # type: ignore[arg-type]
        now=fixed_now,
    )


def test_bf10_a_shadow_admission_commits_one_receipt_with_its_graph_atomically() -> None:
    client, store = _store()
    receipt = admit_run_request(request(), deps(store))
    assert (receipt.decision, receipt.row, receipt.authority_epoch) == ("ADMITTED", 20, 0)
    assert client.kinds() == ["GRAPH/GRAPH", "MODE/MODE", "RECEIPT/RECEIPT"]
    assert len(client.transactions) == 1 and len(client.transactions[0]) == 2
    graph = client.items[(f"GRAPH#{receipt.graph_id}", "GRAPH")]
    assert graph["state"] == {"S": "SHADOW_RECORDED"}
    # Exact replay returns the same receipt and writes nothing.
    again = admit_run_request(request(), deps(store))
    assert again == receipt
    assert len(client.transactions) == 1


def test_bf10_v5_a_second_request_for_the_same_input_joins_the_shadow_graph() -> None:
    client, store = _store()
    first = admit_run_request(request("request-0001"), deps(store))
    second = admit_run_request(request("request-0002"), deps(store))
    assert (second.decision, second.row, second.graph_id) == ("JOINED", 9, first.graph_id)
    assert second.authority_epoch == 0
    assert [kind for kind in client.kinds() if kind.startswith("GRAPH")] == ["GRAPH/GRAPH"]
    assert not any(
        kind.startswith(("SLOT", "RUN", "DUE", "FINGERPRINT")) for kind in client.kinds()
    )


def test_bf10_row_2_commits_its_own_receipt_once_and_a_replay_returns_it() -> None:
    """S1A-1: a reused request ID with another body is receipted, never a retryable failure."""
    client, store = _store()
    original = admit_run_request(request(), deps(store))
    conflicting = request(reason="bounded_validation")
    refused = admit_run_request(conflicting, deps(store))
    assert (refused.row, refused.decision, refused.reason) == (2, "REFUSED", "request_id_conflict")
    assert refused.request_id == original.request_id
    assert refused.request_digest != original.request_digest
    assert len(client.transactions) == 2
    assert len(client.transactions[1]) == 1
    conflict_key = ("RECEIPT#request-0001", f"CONFLICT#{refused.request_digest}")
    assert client.items[conflict_key]["decision"] == {"S": "REFUSED"}
    # The original receipt is untouched and is what the GET route reads.
    assert store.read_receipt("request-0001") == original
    # An exact replay of the conflicting request returns its row-2 receipt and writes nothing.
    assert admit_run_request(conflicting, deps(store)) == refused
    assert len(client.transactions) == 2
    # The original request still returns its own receipt (row 1).
    assert admit_run_request(request(), deps(store)) == original
    assert sorted(client.kinds()) == [
        "GRAPH/GRAPH",
        "MODE/MODE",
        "RECEIPT/CONFLICT",
        "RECEIPT/RECEIPT",
    ]


def test_the_receipt_fingerprint_is_keyed_on_the_contract_revision() -> None:
    """S1A-4: `F` carries contract_revision 2, not the contract file's bytes."""
    from adapters.inbound.run_requests.service import INGESTION_TEMPLATE  # noqa: PLC0415
    from domain.operations.ingestion_root import (  # noqa: PLC0415
        ManifestVersion,
        manifest_fingerprint,
    )

    _, store = _store()
    receipt = admit_run_request(request(), deps(store))
    expected = manifest_fingerprint(ManifestVersion(PROBE_KEY, "version-1"), INGESTION_TEMPLATE, 2)
    assert receipt.fingerprint == expected
    assert receipt.contract_revision == 2


def test_bf10_negative_an_injected_store_failure_commits_nothing() -> None:
    client, store = _store()
    client.fail_next = True
    before = dict(client.items)
    with pytest.raises(RunRequestNotRecordedError):
        admit_run_request(request(), deps(store))
    assert client.items == before
    assert client.transactions == []


@pytest.mark.parametrize(
    ("mode", "key", "manifest", "reason"),
    [
        (AuthorityMode.LEGACY, PROBE_KEY, {}, "RUN_ADMISSION_NOT_ACTIVE"),
        (AuthorityMode.LEGACY_DRAINING, PROBE_KEY, {}, "RUN_ADMISSION_NOT_ACTIVE"),
        (AuthorityMode.SHADOW, HISTORICAL_KEY, {}, "FULL_HISTORY_NOT_ENABLED"),
        (AuthorityMode.SHADOW, PROBE_KEY, {"exact_versions": 2}, "manifest_key_rewritten"),
        (
            AuthorityMode.SHADOW,
            PROBE_KEY,
            {"window_retrievals": 10_000},
            "conflict_unit_history_over_bound",
        ),
    ],
)
def test_bf10_each_domain_state_refusal_writes_exactly_one_receipt_and_nothing_else(  # noqa: PLR0913, PLR0917
    mode: AuthorityMode, key: str, manifest: dict[str, Any], reason: str
) -> None:
    client, store = _store(mode)
    receipt = admit_run_request(request(key=key), deps(store, key, **manifest))
    assert (receipt.decision, receipt.reason) == ("REFUSED", reason)
    assert receipt.authority_epoch == 0
    assert receipt.mode is mode
    assert client.kinds() == ["MODE/MODE", "RECEIPT/RECEIPT"]
    assert len(client.transactions[0]) == 1


def test_an_absent_mode_item_reads_legacy_and_refuses_with_a_receipt() -> None:
    client, store = _store(None)
    assert store.read_mode() == ModeState(AuthorityMode.LEGACY, 0)
    receipt = admit_run_request(request(), deps(store))
    assert (receipt.reason, receipt.mode) == ("RUN_ADMISSION_NOT_ACTIVE", AuthorityMode.LEGACY)
    assert client.kinds() == ["RECEIPT/RECEIPT"]


@pytest.mark.parametrize(
    ("overrides", "signal"),
    [
        ({"protocol_version": "v2"}, "PROTOCOL_MIGRATION_UNSUPPORTED"),
        ({"label": "full_history"}, "FULL_HISTORY_NOT_ENABLED"),
        ({"reason": "data_correction"}, "RUN_REQUEST_REASON_NOT_PERMITTED"),
        (
            {"caller_kind": "schedule", "reason": "data_correction"},
            "RUN_REQUEST_REASON_NOT_PERMITTED",
        ),
        (
            {"requester": "arn:aws:sts::000000000000:assumed-role/agent/x"},
            "AGENT_MUTATION_NOT_ENABLED",
        ),
        (
            {"manifest_key": "WRITE_PLAN#abc/retrieved=20261006T092520Z/manifest.json"},
            "LEGACY_ADOPTION_UNSUPPORTED",
        ),
        ({"request_id": "x"}, "SCHEMA_INVALID"),
    ],
)
def test_bf10_pre_domain_refusals_write_no_receipt(overrides: dict[str, Any], signal: str) -> None:
    client, store = _store()
    with pytest.raises((GraphCommandRefusedError, RootRefusedError)) as raised:
        admit_run_request(request(**overrides), deps(store))
    assert raised.value.signal == signal  # type: ignore[union-attr]
    assert client.kinds() == ["MODE/MODE"]


def test_v7c_a_body_that_disagrees_with_its_key_is_refused_before_the_domain() -> None:
    client, store = _store()
    dependencies = deps(store)
    dependencies.manifests.retrieved_at += timedelta(seconds=1)  # type: ignore[attr-defined]
    with pytest.raises(RootRefusedError) as raised:
        admit_run_request(request(), dependencies)
    assert raised.value.signal == "MANIFEST_KEY_BODY_MISMATCH"
    assert client.kinds() == ["MODE/MODE"]


def test_v7d_a_failed_listing_is_a_dependency_failure_with_no_receipt() -> None:
    from domain.shared_kernel.errors import DependencyError  # noqa: PLC0415

    client, store = _store()
    with pytest.raises(DependencyError):
        admit_run_request(request(), deps(store, fail=True))
    assert client.kinds() == ["MODE/MODE"]


def _waiter(request_id: str, revision: int) -> Waiter:
    return Waiter(
        request_id, f"f-{request_id}", datetime(2026, 10, 6, 9, 30, tzinfo=UTC), False, revision
    )


def _queue_commit(  # noqa: PLR0913, PLR0917 -- one commit's four parts
    store_request: str, old: Waiter | None, waiter: Waiter, slot: int
) -> AdmissionCommit:
    from ports.outbound.execution_graph import ReceiptRecord  # noqa: PLC0415

    receipt = ReceiptRecord(
        request_id=store_request,
        request_digest="d",
        decision="QUEUED_LATEST",
        reason=None,
        row=19,
        authority_epoch=1,
        mode=AuthorityMode.GRAPH,
        conflict_unit="ab12cd34",
        input_revision="2026-10-06T09:30:00+00:00",
        fingerprint=waiter.fingerprint,
        recorded_at="2026-10-07T09:00:00+00:00",
        protocol_version="v1",
        contract_revision=1,
    )
    return AdmissionCommit(
        receipt, EffectKind.QUEUE_WAITER, "ab12cd34", slot, new_waiter=waiter, superseded_waiter=old
    )


def test_bf7_a_newer_waiter_supersedes_the_older_one_atomically() -> None:
    client, store = _store(AuthorityMode.GRAPH)
    store.commit_admission(_queue_commit("request-old1", None, _waiter("request-old1", 1), 0))
    store.commit_admission(
        _queue_commit("request-new1", _waiter("request-old1", 1), _waiter("request-new1", 2), 0)
    )
    waiter = client.items[(due_shard("ab12cd34"), "WAITER#ab12cd34")]
    assert waiter["request_id"] == {"S": "request-new1"}
    supersession = client.items[("RECEIPT#request-old1", "SUPERSEDED")]
    assert supersession["decision"] == {"S": "SUPERSEDED"}
    assert supersession["reason"] == {"S": "by=request-new1"}


def test_bf7_negative_a_second_waiter_without_supersession_commits_nothing() -> None:
    client, store = _store(AuthorityMode.GRAPH)
    store.commit_admission(_queue_commit("request-old1", None, _waiter("request-old1", 1), 0))
    before = dict(client.items)
    with pytest.raises(AdmissionConflictError) as second:
        store.commit_admission(_queue_commit("request-new1", None, _waiter("request-new1", 1), 0))
    # S1A-10: the failed WAITER# member surfaces as BF-7's own typed guard.
    assert second.value.signal == "RUN_REQUEST_WAIT_SLOT_CONFLICT"
    with pytest.raises(AdmissionConflictError) as stale:
        # A supersession against a stale waiter revision is refused the same way.
        store.commit_admission(
            _queue_commit("request-new2", _waiter("request-old1", 7), _waiter("request-new2", 8), 0)
        )
    assert stale.value.signal == "RUN_REQUEST_WAIT_SLOT_CONFLICT"
    assert client.items == before


def test_v18_every_receipt_written_outside_graph_carries_epoch_0() -> None:
    for mode in (AuthorityMode.LEGACY, AuthorityMode.SHADOW, AuthorityMode.LEGACY_DRAINING):
        client, store = _store(mode)
        admit_run_request(request(), deps(store))
        receipts = [item for (pk, _), item in client.items.items() if pk.startswith("RECEIPT#")]
        assert receipts
        assert all(item["authority_epoch"] == {"N": "0"} for item in receipts)
        assert all(item["mode"] == {"S": mode.value} for item in receipts)


def test_the_mode_cas_writes_its_command_receipt_and_refuses_a_stale_revision() -> None:
    client, store = _store(None)
    receipt = CommandReceipt(
        command_id="command-0001",
        command="set_mode",
        principal="arn:aws:iam::000000000000:user/operator",
        authority_epoch=0,
        mode=AuthorityMode.LEGACY,
        outcome="committed",
        recorded_at="2026-10-07T09:00:00+00:00",
        protocol_version="v1",
    )
    store.commit_mode(ModeChange(ModeState(AuthorityMode.LEGACY, 0), AuthorityMode.SHADOW, receipt))
    assert store.read_mode() == ModeState(AuthorityMode.SHADOW, 1)
    assert ("COMMAND#command-0001", "COMMAND") in client.items
    with pytest.raises(AdmissionConflictError):
        store.commit_mode(
            ModeChange(
                ModeState(AuthorityMode.LEGACY, 0),
                AuthorityMode.SHADOW,
                replace(receipt, command_id="command-0002"),
            )
        )
    assert store.read_mode() == ModeState(AuthorityMode.SHADOW, 1)


# --- S3: typed guards per failed member (S1A-10) and the hold at admission (V-14(a)) ----------


def _replace_commit(invalidating: bool) -> AdmissionCommit:
    from ports.outbound.execution_graph import NewRun, ReceiptRecord, ReplacedRun  # noqa: PLC0415

    receipt = ReceiptRecord(
        request_id="request-new9",
        request_digest="d",
        decision="REPLACED_ON_INVALIDATION" if invalidating else "REPLACED_BEFORE_COMPUTE",
        reason=None,
        row=18 if invalidating else 17,
        authority_epoch=1,
        mode=AuthorityMode.GRAPH,
        conflict_unit="ab12cd34",
        input_revision="2026-10-06T09:30:00+00:00",
        fingerprint="f-new",
        recorded_at="2026-10-07T09:00:00+00:00",
        protocol_version="v1",
        contract_revision=1,
    )
    return AdmissionCommit(
        receipt,
        EffectKind.REPLACE_RUN,
        "ab12cd34",
        1,
        new_run=NewRun("run-new", "graph-new", "f-new", "2026-10-06T09:30:00+00:00", "r", 0, "t"),
        replaced=ReplacedRun("run-old", invalidating),
    )


def _seed_active(client: FakeDynamo, **attributes: dict[str, str]) -> None:
    client.items[("RUN#run-old", "RUN")] = {
        "pk": {"S": "RUN#run-old"},
        "sk": {"S": "RUN"},
        "state": {"S": "OPEN"},
        "revision": {"N": "1"},
        "dispatch_generation": {"N": "0"},
        **attributes,
    }
    client.items[("SLOT#ab12cd34", "SLOT")] = {
        "pk": {"S": "SLOT#ab12cd34"},
        "sk": {"S": "SLOT"},
        "active_run_id": {"S": "run-old"},
        "revision": {"N": "1"},
    }


@pytest.mark.parametrize(
    ("attributes", "invalidating", "signal"),
    [
        (
            {"grant_id": {"S": "grant-1"}, "compute_started_at": {"S": "t"}},
            True,
            "ACTIVE_RUN_GRANTED",
        ),
        ({"compute_started_at": {"S": "t"}}, False, "ACTIVE_COMPUTE_NOT_REPLACEABLE"),
    ],
)
def test_s1a10_a_lost_replace_names_its_exact_guard_from_the_runs_old_image(
    attributes: dict[str, dict[str, str]], invalidating: bool, signal: str
) -> None:
    client, store = _store(AuthorityMode.GRAPH)
    _seed_active(client, **attributes)
    before = dict(client.items)
    with pytest.raises(AdmissionConflictError) as lost:
        store.commit_admission(_replace_commit(invalidating))
    assert lost.value.signal == signal
    assert client.items == before


def test_a_replacing_admission_fences_the_old_run_and_writes_its_fence_obligation() -> None:
    client, store = _store(AuthorityMode.GRAPH)
    _seed_active(client)
    store.commit_admission(_replace_commit(invalidating=False))
    old = client.items[("RUN#run-old", "RUN")]
    assert old["state"] == {"S": "FENCING"}
    assert old["fence_reason"] == {"S": "replaced"}
    assert (due_shard("ab12cd34"), "FENCE#run-old") in client.items
    assert not any(sk.startswith("PROMOTE#") for _pk, sk in client.items)


class _NoLegacy:
    def completed_plan_key(self, key: str, version_id: str) -> str | None:
        del key, version_id
        return None

    def head_after(self, window: str, r: datetime) -> datetime | None:
        del window, r
        return None


def _graph_hold_store() -> tuple[FakeDynamo, DynamoExecutionGraphStore]:
    client, store = _store(AuthorityMode.GRAPH)
    client.items[("MODE#ingestion", "MODE")]["hold"] = {"BOOL": True}
    return client, store


def test_v14a_under_the_hold_a_delivery_event_is_queued_held_and_starts_no_run() -> None:
    client, store = _graph_hold_store()
    automatic = request(
        caller_kind="delivery_event",
        reason="delivery_arrived",
        requester="digest-of-the-sqs-handler-role",
        requester_is_iam=False,
    )
    receipt = admit_run_request(automatic, replace(deps(store), legacy=_NoLegacy()))
    assert (receipt.decision, receipt.reason, receipt.row) == (
        "QUEUED_LATEST",
        "automatic_admission_held",
        16,
    )
    assert not any(pk.startswith("RUN#") for pk, _sk in client.items)
    unit = window_identity(PROBE_KEY).conflict_unit
    assert client.items[(due_shard(unit), f"WAITER#{unit}")]["held"] == {"BOOL": True}
    assert client.items[(due_shard(unit), f"PROMOTE#{unit}")]["held"] == {"BOOL": True}


def test_v14a_positive_under_the_hold_an_on_demand_request_is_decided_normally() -> None:
    client, store = _graph_hold_store()
    receipt = admit_run_request(request(), replace(deps(store), legacy=_NoLegacy()))
    assert (receipt.decision, receipt.row) == ("ADMITTED", 20)
    assert any(pk.startswith("RUN#") for pk, _sk in client.items)
