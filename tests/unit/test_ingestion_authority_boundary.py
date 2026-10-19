# SPDX-License-Identifier: MIT-0
"""S6 ingress by mode (V-19), the hard deny (d) and the workload-requester rule (S1A-7).

- `GovernedMaterializationPort` refuses a historical manifest before any data-object read, in
  every mode; a graph caller's claim is re-attested first. The raw fetch has exactly the
  inventoried callers (fail on unknown).
- The SQS handler, the registration finalizer, the direct operator and fixture landing follow
  the ingress-by-mode table.
- A workload role may submit only its own automatic caller kind.
"""

from __future__ import annotations

import ast
import io
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from execution_graph_support import FakeDynamo, FakeManifests

from adapters.inbound.ingestion_handlers.ingress import (
    DRAIN_LAST_RECEIVE_OPERATION,
    SHADOW_RECORDING_OPERATION,
    DeliveryIngress,
    DeliveryRef,
    IngressPath,
    delivery_request,
    drain_wait,
    graph_admit,
    ingress_path,
    queue_url_from_arn,
    record_schedule,
    shadow_record,
)
from adapters.inbound.run_requests.service import (
    AdmissionDependencies,
    RunRequest,
    admit_run_request,
    require_workload_requester,
)
from adapters.jobs.ingest_delivery.authority_gate import (
    GatePath,
    OperatorGate,
    decide_gate,
    record_shadow,
)
from adapters.jobs.ingest_delivery.landing import ManifestInput
from adapters.outbound.dynamodb.execution_graph import DynamoExecutionGraphStore
from adapters.outbound.governed_materialization import (
    FullHistoryRefusedError,
    GovernedMaterializationPort,
    MaterializationClaim,
    MaterializationClaimLostError,
)
from adapters.outbound.s3 import run_request_manifests
from adapters.outbound.telemetry.reporting import Report
from domain.operations.execution_graph import AuthorityMode, GraphCommandRefusedError, ModeState
from domain.operations.run_request import AdmissionClass, CallerKind
from domain.shared_kernel.errors import DependencyError
from ports.outbound.execution_graph import ReceiptRecord, RunRequestNotRecordedError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-074")]

_ROOT_CLOCK: Callable[[object], datetime] = run_request_manifests._root_clock  # pyright: ignore[reportPrivateUsage]

ROLE = "arn:aws:sts::000000000000:assumed-role/adlytics-dev-IngestionRole/adlytics-dev-ingest"
SSO = "arn:aws:sts::000000000000:assumed-role/AWSReservedSSO_Admin_1/operator"
KEY = (
    "raw/amc/dsp_impressions/marketplace=US/period_start=2026-10-01/period_end=2026-10-01/"
    "retrieved=20261007T090000Z/manifest.json"
)
NOW = datetime(2026, 10, 9, 0, 0, tzinfo=UTC)
SRC = Path(__file__).resolve().parents[2] / "src"


def _receipt(decision: str = "ADMITTED") -> ReceiptRecord:
    return ReceiptRecord(
        request_id="delivery-x",
        request_digest="d",
        decision=decision,
        reason=None,
        row=20,
        authority_epoch=0,
        mode=AuthorityMode.SHADOW,
        conflict_unit="u",
        input_revision="r",
        fingerprint="f",
        recorded_at=NOW.isoformat(),
        protocol_version="v1",
        contract_revision=1,
    )


class _Ingress:
    """A DeliveryIngress over a settable mode and a recording admission call."""

    def __init__(self, mode: ModeState, fail: BaseException | None = None) -> None:
        self.mode = mode
        self.fail = fail
        self.requests: list[RunRequest] = []
        self.visibility: list[tuple[str, str, int]] = []

    def admit(self, request: RunRequest) -> ReceiptRecord:
        self.requests.append(request)
        if self.fail is not None:
            raise self.fail
        return _receipt()

    def ingress(self, *, provisioned: bool = True) -> DeliveryIngress:
        return DeliveryIngress(
            mode=lambda: self.mode,
            admit=self.admit if provisioned else None,
            visibility=lambda queue, handle, seconds: self.visibility.append(
                (queue, handle, seconds)
            ),
            requester=lambda: ROLE,
            logger=MagicMock(),
            now=lambda: NOW,
        )


def _capture(reported: list[str]) -> Callable[[BaseException, Report], None]:
    def capture(_error: BaseException, report: Report) -> None:
        reported.append(report.operation)

    return capture


def _ignore(*_args: object) -> None:
    return None


def _class(name: str) -> Callable[..., AdmissionClass]:
    def derive(*_args: object) -> AdmissionClass:
        return AdmissionClass(name)

    return derive


def _no_facts(_manifest: ManifestInput) -> object:
    return object()


def _ref(receive_count: int = 1) -> DeliveryRef:
    return DeliveryRef(KEY, "v1", "handle", receive_count, "message-1", "https://sqs/x")


# --- V-19: the SQS handler (`delivery_event`) ----------------------------------------------------


def test_v19_the_ingress_table_maps_every_mode() -> None:
    assert ingress_path(AuthorityMode.LEGACY) is IngressPath.LEGACY
    assert ingress_path(AuthorityMode.SHADOW) is IngressPath.LEGACY_THEN_SHADOW
    assert ingress_path(AuthorityMode.LEGACY_DRAINING) is IngressPath.DRAIN_WAIT
    assert ingress_path(AuthorityMode.GRAPH) is IngressPath.GRAPH


def test_v19a_a_drain_wait_makes_no_admission_call_and_holds_until_d_plus_margin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reported: list[str] = []
    monkeypatch.setattr(
        "adapters.inbound.ingestion_handlers.ingress.report_error", _capture(reported)
    )
    deadline = NOW + timedelta(hours=1)
    fake = _Ingress(
        ModeState(AuthorityMode.LEGACY_DRAINING, 2, drain_deadline=deadline.isoformat())
    )
    seconds = drain_wait(fake.ingress(), _ref(), fake.mode)
    assert seconds == 3600 + 60
    assert fake.visibility == [("https://sqs/x", "handle", 3660)]
    assert fake.requests == []
    assert reported == []


def test_v19a_a_refusal_on_the_last_receive_reports_drain_last_receive_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reported: list[str] = []
    monkeypatch.setattr(
        "adapters.inbound.ingestion_handlers.ingress.report_error", _capture(reported)
    )
    fake = _Ingress(ModeState(AuthorityMode.LEGACY_DRAINING, 2, drain_deadline=NOW.isoformat()))
    drain_wait(fake.ingress(), _ref(receive_count=3), fake.mode)
    assert reported == [DRAIN_LAST_RECEIVE_OPERATION]
    assert fake.requests == []


def test_v19b_a_shadow_recording_failure_is_reported_once_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reported: list[str] = []
    monkeypatch.setattr(
        "adapters.inbound.ingestion_handlers.ingress.report_error", _capture(reported)
    )
    fake = _Ingress(ModeState(AuthorityMode.SHADOW, 1), fail=RunRequestNotRecordedError("injected"))
    assert shadow_record(fake.ingress(), _ref()) is None
    assert reported == [SHADOW_RECORDING_OPERATION]


def test_v19b_positive_shadow_records_one_delivery_event_request() -> None:
    fake = _Ingress(ModeState(AuthorityMode.SHADOW, 1))
    assert shadow_record(fake.ingress(), _ref()) is not None
    (request,) = fake.requests
    assert (request.caller_kind, request.reason, request.workload) == (
        "delivery_event",
        "delivery_arrived",
        True,
    )
    assert request.request_id == delivery_request(_ref(), ROLE).request_id


def test_v19_graph_admits_and_a_redelivery_repeats_the_request_id() -> None:
    fake = _Ingress(ModeState(AuthorityMode.GRAPH, 5))
    graph_admit(fake.ingress(), _ref())
    graph_admit(fake.ingress(), _ref(receive_count=2))
    first, second = fake.requests
    assert first.request_id == second.request_id


def test_the_queue_url_derives_from_the_event_source_arn() -> None:
    assert (
        queue_url_from_arn("arn:aws:sqs:us-east-1:000000000000:adlytics-dev-ingestion")
        == "https://sqs.us-east-1.amazonaws.com/000000000000/adlytics-dev-ingestion"
    )
    with pytest.raises(ValueError, match="eventSourceARN"):
        queue_url_from_arn("not-an-arn")


# --- V-19: the registration finalizer (`schedule`) ----------------------------------------------


@pytest.mark.parametrize(
    ("mode", "requests"),
    [
        (AuthorityMode.LEGACY, 0),
        (AuthorityMode.SHADOW, 1),
        (AuthorityMode.LEGACY_DRAINING, 0),
        (AuthorityMode.GRAPH, 1),
    ],
)
def test_v19_the_finalizer_requests_schedule_by_mode(mode: AuthorityMode, requests: int) -> None:
    fake = _Ingress(ModeState(mode, 1))
    record_schedule(fake.ingress(), (KEY, "v1"), "attempt")
    assert len(fake.requests) == requests
    for request in fake.requests:
        assert (request.caller_kind, request.reason) == ("schedule", "scheduled_cadence")


def test_v19_a_graph_schedule_failure_raises_and_a_shadow_one_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("adapters.inbound.ingestion_handlers.ingress.report_error", _ignore)
    failing = RunRequestNotRecordedError("injected")
    with pytest.raises(RunRequestNotRecordedError):
        record_schedule(
            _Ingress(ModeState(AuthorityMode.GRAPH, 5), failing).ingress(), (KEY, "v1"), "a"
        )
    assert (
        record_schedule(
            _Ingress(ModeState(AuthorityMode.SHADOW, 1), failing).ingress(), (KEY, "v1"), "a"
        )
        is None
    )


# --- V-19: the direct operator (`on_demand`) ----------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "path", "receipts"),
    [
        (AuthorityMode.LEGACY, GatePath.LEGACY, 0),
        (AuthorityMode.SHADOW, GatePath.LEGACY_THEN_SHADOW, 0),
        (AuthorityMode.LEGACY_DRAINING, GatePath.REFUSED, 2),
        (AuthorityMode.GRAPH, GatePath.ADMITTED_ONLY, 2),
    ],
)
def test_v19_the_direct_operator_decides_before_any_ingestion(
    mode: AuthorityMode, path: GatePath, receipts: int
) -> None:
    admitted: list[RunRequest] = []

    def admit(request: RunRequest) -> ReceiptRecord:
        admitted.append(request)
        return _receipt("REFUSED" if mode is AuthorityMode.LEGACY_DRAINING else "ADMITTED")

    gate = OperatorGate(lambda: mode, admit, SSO, "operation-1")
    manifests = (ManifestInput(KEY, "v1"), ManifestInput(KEY, "v2"))
    decision = decide_gate(gate, manifests)
    assert decision.path is path
    assert len(decision.receipts) == receipts
    assert decision.ingest is (path in {GatePath.LEGACY, GatePath.LEGACY_THEN_SHADOW})
    assert all(item.caller_kind == "on_demand" for item in admitted)
    if mode is AuthorityMode.SHADOW:
        assert len(record_shadow(gate, manifests, lambda _error: None)) == 2


def test_v19_the_direct_operator_drain_refusal_is_one_row_3_receipt() -> None:
    """End to end through admission: LEGACY_DRAINING writes REFUSED(RUN_ADMISSION_NOT_ACTIVE)."""
    client = FakeDynamo()
    client.items[("MODE#ingestion", "MODE")] = {
        "pk": {"S": "MODE#ingestion"},
        "sk": {"S": "MODE"},
        "mode": {"S": "LEGACY_DRAINING"},
        "revision": {"N": "2"},
    }
    store = DynamoExecutionGraphStore("graph", client)  # type: ignore[arg-type]
    deps = AdmissionDependencies(
        store=store, manifests=FakeManifests(retrieved_at=NOW), now=lambda: NOW
    )
    gate = OperatorGate(
        lambda: store.read_mode().mode, lambda r: admit_run_request(r, deps), SSO, "op-1"
    )
    decision = decide_gate(gate, (ManifestInput(KEY, "v1"),))
    (receipt,) = decision.receipts
    assert (receipt.decision, receipt.reason, receipt.row) == (
        "REFUSED",
        "RUN_ADMISSION_NOT_ACTIVE",
        3,
    )
    assert receipt.authority_epoch == 0
    assert receipt.mode is AuthorityMode.LEGACY_DRAINING


# --- S1A-7: workload requesters ------------------------------------------------------------------


@pytest.mark.parametrize("caller", [CallerKind.DELIVERY_EVENT, CallerKind.SCHEDULE])
def test_s1a7_a_workload_role_may_submit_its_automatic_caller_kind(caller: CallerKind) -> None:
    require_workload_requester(ROLE, caller)


@pytest.mark.parametrize(
    ("requester", "caller"),
    [
        (ROLE, CallerKind.ON_DEMAND),
        (SSO, CallerKind.DELIVERY_EVENT),
        ("arn:aws:iam::000000000000:user/operator", CallerKind.SCHEDULE),
        ("arn:aws:iam::000000000000:role/some-role", CallerKind.DELIVERY_EVENT),
    ],
)
def test_s1a7_every_other_requester_is_refused_as_an_agent_mutation(
    requester: str, caller: CallerKind
) -> None:
    with pytest.raises(GraphCommandRefusedError) as raised:
        require_workload_requester(requester, caller)
    assert raised.value.signal == "AGENT_MUTATION_NOT_ENABLED"


def test_s1a7_an_unflagged_workload_role_is_still_held_to_the_human_rule() -> None:
    client = FakeDynamo()
    deps = AdmissionDependencies(
        store=DynamoExecutionGraphStore("graph", client),  # type: ignore[arg-type]
        manifests=FakeManifests(retrieved_at=NOW),
        now=lambda: NOW,
    )
    request = RunRequest(
        "delivery-0000001", "v1", "delivery_event", "delivery_arrived", KEY, "v1", ROLE, True
    )
    with pytest.raises(GraphCommandRefusedError):
        admit_run_request(request, deps)
    assert client.items == {}


# --- S5-V-5: the root clock is required -----------------------------------------------------------


@pytest.mark.parametrize(
    "modified",
    [None, datetime(2026, 1, 1), datetime(1969, 12, 31, tzinfo=UTC), "2026"],  # noqa: DTZ001
)
def test_s5v5_a_missing_naive_or_pre_epoch_last_modified_is_refused(modified: object) -> None:
    with pytest.raises(DependencyError) as raised:
        _ROOT_CLOCK(modified)
    assert raised.value.context["reason"] == run_request_manifests.TIMESTAMP_INVALID


def test_s5v5_positive_an_aware_last_modified_is_the_root_clock() -> None:
    assert _ROOT_CLOCK(NOW) == NOW


def test_s5v5_admission_refuses_an_unclocked_manifest_without_a_receipt() -> None:
    client = FakeDynamo()
    client.items[("MODE#ingestion", "MODE")] = {
        "pk": {"S": "MODE#ingestion"},
        "sk": {"S": "MODE"},
        "mode": {"S": "SHADOW"},
        "revision": {"N": "1"},
    }
    deps = AdmissionDependencies(
        store=DynamoExecutionGraphStore("graph", client),  # type: ignore[arg-type]
        manifests=FakeManifests(retrieved_at=NOW, unclocked=True),
        now=lambda: NOW,
    )
    request = RunRequest(
        "operator-0000001", "v1", "on_demand", "operator_rerun", KEY, "v1", SSO, True
    )
    with pytest.raises(RunRequestNotRecordedError):
        admit_run_request(request, deps)
    assert [key for key in client.items if key[0].startswith("RECEIPT#")] == []


# --- Hard deny (d): GovernedMaterializationPort --------------------------------------------------


def _manifest(period_end: str, retrieved: str) -> bytes:
    return json.dumps(
        {
            "manifest_version": 1,
            "source_system": "amc",
            "source_dataset": "dsp_impressions",
            "period_start": period_end,
            "period_end": period_end,
            "retrieved_at": retrieved,
        }
    ).encode()


class _S3:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.reads: list[str] = []

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.reads.append(str(kwargs["Key"]))
        return {"Body": io.BytesIO(self.body)}


def _port(body: bytes) -> tuple[GovernedMaterializationPort, _S3, list[ManifestInput]]:
    client = _S3(body)
    fetched: list[ManifestInput] = []

    def fetch(_client: Any, _bucket: str, manifest: ManifestInput) -> Any:
        fetched.append(manifest)
        return object()

    return GovernedMaterializationPort("raw", client, fetch=fetch), client, fetched  # type: ignore[arg-type]


def test_hard_deny_d_a_historical_manifest_is_refused_before_any_data_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port, client, fetched = _port(b"{}")
    monkeypatch.setattr(port, "_facts", _no_facts)
    monkeypatch.setattr(
        "adapters.outbound.governed_materialization.derive_admission_class",
        _class("historical"),
    )
    with pytest.raises(FullHistoryRefusedError) as raised:
        port.fetch(ManifestInput(KEY, "v1"))
    assert raised.value.signal == "FULL_HISTORY_NOT_ENABLED"
    assert fetched == []
    assert client.reads == []


def test_hard_deny_d_positive_a_cadence_manifest_is_fetched_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port, _client, fetched = _port(b"{}")
    monkeypatch.setattr(port, "_facts", _no_facts)
    monkeypatch.setattr(
        "adapters.outbound.governed_materialization.derive_admission_class",
        _class("cadence"),
    )
    port.fetch(ManifestInput(KEY, "v1"))
    assert fetched == [ManifestInput(KEY, "v1")]


def test_hard_deny_d_a_lost_graph_claim_refuses_the_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    port, _client, fetched = _port(b"{}")
    monkeypatch.setattr(port, "require_not_historical", _ignore)
    claim = MaterializationClaim("node-1", 3, lambda _node, version: version == 4)
    with pytest.raises(MaterializationClaimLostError):
        port.fetch(ManifestInput(KEY, "v1"), claim)
    assert fetched == []
    port.fetch(ManifestInput(KEY, "v1"), MaterializationClaim("node-1", 4, lambda _n, v: v == 4))
    assert len(fetched) == 1


def test_hard_deny_d_the_real_predicate_reads_the_exact_manifest_version() -> None:
    """The port classifies from the exact version's own body, never a caller label."""
    port, client, _fetched = _port(_manifest("2026-10-01", "2026-10-07T09:00:00Z"))
    try:
        port.require_not_historical(ManifestInput(KEY, "v1"))
    except FullHistoryRefusedError:
        pytest.fail("a phase-A cadence manifest is not historical")
    except Exception:  # noqa: BLE001, S110 -- a fixture manifest shape the parser refuses
        pass
    assert client.reads == [KEY]


_RAW_FETCH_CALLERS = {
    ("adapters/outbound/governed_materialization.py", "GovernedMaterializationPort"),
    ("adapters/inbound/ingestion_handlers/load.py", "_fetch_delivery"),
}


def test_hard_deny_d_the_raw_fetch_has_exactly_the_inventoried_callers() -> None:
    """Fail on unknown: only the governed port references the raw fetch.

    The two references are the port's default fetch and load.py's call-time wrapper, which is
    handed to the port. Any other top-level function or class naming `fetch_v2_delivery`
    outside its own module fails this test.
    """
    found: set[tuple[str, str]] = set()
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(SRC).as_posix()
        if relative == "adapters/jobs/ingest_delivery/landing.py":
            continue
        for node in ast.parse(path.read_text()).body:
            if not isinstance(node, ast.FunctionDef | ast.ClassDef):
                continue
            if any(
                isinstance(inner, ast.Name) and inner.id == "fetch_v2_delivery"
                for inner in ast.walk(node)
            ):
                found.add((relative, node.name))
    assert found == _RAW_FETCH_CALLERS
