# SPDX-License-Identifier: MIT-0
"""P4 release live tests against the dev probe table only (S4).

Every graph write goes to `adlytics-<env>-execution-graph-probe` through the probe role. Each
test works in its own fresh namespace: a random fingerprint, conflict unit and co-release
domain (`ReleaseContext.domain_override`), so no run ever shares a release-order head, lineage
or block with another run or with the live table. Fictional data only.

The legacy-head test also reads, never writes, the dev raw zone and the coordinator table under
the operator profile, for the window of the P0 probe retrieval, whose legacy plan COMPLETED.
Run only through the probe runner's fail-closed JUnit check, one file at a time:
    AWS_PROFILE=<bound> AWS_REGION=us-east-1 ADL_TEST_ENV=dev ADL_P4_RAW_BUCKET=<raw>
        ADL_P4_LEGACY_KEY=<P0 probe key> uv run pytest -m integration
        tests/integration/test_execution_graph_release_live.py   (one line; the runner adds JUnit)
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError

from adapters.jobs.execution_graph.release import (
    ReleaseContext,
    ReleaseOutcome,
    apply_run,
    issue_grant,
    release_run,
)
from adapters.outbound.dynamodb.execution_graph import due_shard, receipt_item
from adapters.outbound.dynamodb.execution_graph_release import DynamoGraphReleaseStore, head_epoch
from adapters.outbound.dynamodb.items import write_int, write_text
from adapters.outbound.dynamodb.stage1_writer_authority import Stage1WriterAuthorityReader
from adapters.outbound.legacy_head import S3CoordinatorLegacyHead
from domain.operations.execution_graph import AuthorityMode, graph_id, run_id
from domain.operations.execution_graph_dispatch import NodePhase
from domain.operations.execution_graph_release import (
    RELEASE_GRANT_RESERVATION_PROOF_REQUIRED,
    ReleaseRefusedError,
)
from domain.operations.generated_execution_graphs import SERVING_COALESCING
from domain.operations.ingestion_root import window_identity
from domain.operations.status import canonical_authority_digest
from ports.outbound.execution_graph import ReceiptRecord
from ports.outbound.execution_graph_dispatch import NodeSpec, PutNodesRequest

pytestmark = pytest.mark.integration

ENV_NAME = os.environ.get("ADL_TEST_ENV", "dev")
PROBE = f"adlytics-{ENV_NAME}-execution-graph-probe"
ROLE = f"adlytics-{ENV_NAME}-execution-graph-probe"
TEMPLATE = "ingestion_manifest_to_served.v1"
NOW = datetime.now(UTC).replace(microsecond=0)


class _NoLegacy:
    def head_after(self, window: str, r: datetime) -> None:
        del window, r

    def completed_plan_key(self, key: str, version_id: str) -> None:
        del key, version_id


@pytest.fixture(scope="module")
def client() -> Any:
    """Return a DynamoDB client under the probe role, or skip when it is not assumable."""
    session = boto3.Session()
    try:
        account = session.client("sts").get_caller_identity()["Account"]
        assumed = session.client("sts").assume_role(
            RoleArn=f"arn:aws:iam::{account}:role/{ROLE}", RoleSessionName="p4-release-live"
        )["Credentials"]
    except ClientError as error:  # pragma: no cover - only without a deployed probe role
        pytest.skip(f"the probe role is not assumable: {error.response.get('Error', {})}")
    dynamodb = boto3.client(
        "dynamodb",
        region_name=session.region_name or "us-east-1",
        aws_access_key_id=assumed["AccessKeyId"],
        aws_secret_access_key=assumed["SecretAccessKey"],
        aws_session_token=assumed["SessionToken"],
    )
    dynamodb.get_item(
        TableName=PROBE,
        Key={"pk": {"S": "MODE#ingestion"}, "sk": {"S": "MODE"}},
        ConsistentRead=True,
    )
    return dynamodb


@dataclass(frozen=True, slots=True)
class Namespace:
    """One test's fresh identities."""

    unit: str
    domain: str
    window: str


def _namespace() -> Namespace:
    token = uuid.uuid4().hex
    return Namespace(
        unit=canonical_authority_digest(["p4-unit", token]),
        domain=canonical_authority_digest(["p4-domain", token]),
        window=f"integration-probe/p4/{token}/",
    )


def _context(client: Any, ns: Namespace, legacy: Any = None) -> ReleaseContext:
    return ReleaseContext(
        DynamoGraphReleaseStore(PROBE, client),
        legacy or _NoLegacy(),
        lambda: datetime.now(UTC),
        domain_override=ns.domain,
    )


def _seed(  # noqa: PLR0913 -- one fixture builder
    client: Any, ns: Namespace, retrieved: datetime, *, window: str | None = None
) -> str:
    """Put an OPEN epoch-1 run, its receipt and slot, its nodes, and settle its writes."""
    fingerprint = canonical_authority_digest(["p4-fingerprint", uuid.uuid4().hex])
    name, graph = run_id(fingerprint, 1, 0), graph_id(fingerprint, 1, 0)
    root = canonical_authority_digest(["p4-root", fingerprint])
    request_id = f"p4-{uuid.uuid4().hex[:24]}"
    receipt = ReceiptRecord(
        request_id=request_id,
        request_digest="d" * 64,
        decision="ADMITTED",
        reason=None,
        row=20,
        authority_epoch=1,
        mode=AuthorityMode.GRAPH,
        conflict_unit=ns.unit,
        input_revision=retrieved.isoformat(),
        fingerprint=fingerprint,
        recorded_at=NOW.isoformat(),
        protocol_version="v1",
        contract_revision=1,
        run_id=name,
        graph_id=graph,
        root_flow_id=root,
    )
    client.put_item(TableName=PROBE, Item=receipt_item(receipt))
    client.put_item(
        TableName=PROBE,
        Item={
            "pk": write_text(f"RUN#{name}"),
            "sk": write_text("RUN"),
            "item_kind": write_text("run"),
            "state": write_text("OPEN"),
            "authority_epoch": write_int(1),
            "fingerprint": write_text(fingerprint),
            "conflict_unit": write_text(ns.unit),
            "retrieved_at": write_text(retrieved.isoformat()),
            "root_flow_id": write_text(root),
            "generation": write_int(0),
            "graph_id": write_text(graph),
            "template": write_text(TEMPLATE),
            "dispatch_generation": write_int(0),
            "revision": write_int(1),
            "receipt_request_id": write_text(request_id),
            "protocol_version": write_text("v1"),
            "compute_started_at": write_text(NOW.isoformat()),
            "window": write_text(window or ns.window),
        },
    )
    client.put_item(
        TableName=PROBE,
        Item={
            "pk": write_text(f"SLOT#{ns.unit}"),
            "sk": write_text("SLOT"),
            "active_run_id": write_text(name),
            "revision": write_int(1),
            "authority_epoch": write_int(1),
        },
    )
    store = DynamoGraphReleaseStore(PROBE, client)
    run = store.read_run(name)
    assert run is not None
    table = f"integration-probe-db.{ns.unit[:12]}"
    specs = (
        NodeSpec("write_units", (), NodePhase.PRE_GRANT, table, table, _body(name, "write_units")),
        NodeSpec(
            "grant_apply",
            ("write_units",),
            NodePhase.PRE_GRANT,
            f"grant_apply#{name}",
            f"grant_apply#{name}",
            _body(name, "grant_apply"),
        ),
    )
    store.put_nodes(PutNodesRequest(run, specs, NOW.isoformat()))
    client.update_item(
        TableName=PROBE,
        Key={"pk": write_text(f"RUN#{name}"), "sk": write_text("NODE#write_units")},
        UpdateExpression="SET #s = :s",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={":s": write_text("SUCCEEDED")},
    )
    return name


def _body(run: str, node: str) -> str:
    import json  # noqa: PLC0415

    return json.dumps(
        {"schema_version": "1", "unit_key": f"integration-probe.{node}", "plan_key": run},
        sort_keys=True,
    )


def _get(client: Any, pk: str, sk: str) -> dict[str, Any] | None:
    return client.get_item(
        TableName=PROBE, Key={"pk": {"S": pk}, "sk": {"S": sk}}, ConsistentRead=True
    ).get("Item")


def _release_post_grant_locks(client: Any, run: str) -> None:
    store = DynamoGraphReleaseStore(PROBE, client)
    for node in store.read_nodes(run):
        if node.phase is NodePhase.POST_GRANT:
            client.delete_item(
                TableName=PROBE,
                Key={"pk": write_text(f"LOCK#{node.lock_key}"), "sk": write_text("LOCK")},
            )


def test_bf1_a_grant_without_the_ordinal_is_refused_then_the_one_path_grants(client: Any) -> None:
    ns = _namespace()
    name = _seed(client, ns, NOW - timedelta(hours=1))
    context = _context(client, ns)
    with pytest.raises(ReleaseRefusedError) as refused:
        issue_grant(context, name, ordinal=None, expected_revision=1)
    assert refused.value.signal == RELEASE_GRANT_RESERVATION_PROOF_REQUIRED
    assert _get(client, f"HEAD#{ns.unit}", "HEAD") is None
    assert _get(client, f"RELEASE_ORDER#{ns.domain}", "HEAD") is None

    assert release_run(context, name) is ReleaseOutcome.APPLIED

    run = _get(client, f"RUN#{name}", "RUN")
    assert run is not None and run["state"] == {"S": "GRANTED"}
    grant = run["grant_id"]["S"]
    assert _get(client, f"GRANT#{grant}", "GRANT") is not None
    replay = issue_grant(context, name, ordinal=None, expected_revision=None)
    assert replay.grant_id == grant
    assert release_run(context, name) is ReleaseOutcome.ALREADY_APPLIED


def test_rolling_release_bootstraps_only_the_domain_head_and_its_live_frontier(
    client: Any,
) -> None:
    """D7: a first grant creates an IDLE domain head and zeroed live-frontier shards, no day."""
    ns = _namespace()
    name = _seed(client, ns, NOW - timedelta(hours=1))
    store = DynamoGraphReleaseStore(PROBE, client)
    before = store.read_serving_domain(ns.domain)
    assert (before.head, before.frontier) == (None, None)

    assert release_run(_context(client, ns), name) is ReleaseOutcome.APPLIED

    authority = store.read_serving_domain(ns.domain)
    assert authority.head is not None and authority.frontier is not None
    assert (authority.head.domain, authority.head.state) == (ns.domain, "IDLE")
    shards = len(authority.frontier.revisions)
    assert shards == int(SERVING_COALESCING["serving_frontier_shards"])
    zero = (0,) * shards
    assert (
        authority.frontier.revisions,
        authority.frontier.visible_counts,
        authority.frontier.eligible_counts,
    ) == (zero, zero, zero)
    head = _get(client, f"SERVING_DOMAIN#{ns.domain}", "HEAD")
    assert head is not None and "reservation_day" not in head and "cycle_epoch" not in head
    # The equal replay leaves the domain head and frontier byte-identical.
    assert release_run(_context(client, ns), name) is ReleaseOutcome.ALREADY_APPLIED
    assert _get(client, f"SERVING_DOMAIN#{ns.domain}", "HEAD") == head
    assert store.read_serving_domain(ns.domain).frontier == authority.frontier


def test_bf8_r_after_r_plus_one_is_refused_and_the_head_never_moves_back(client: Any) -> None:
    ns = _namespace()
    newer_r, older_r = NOW - timedelta(minutes=30), NOW - timedelta(hours=2)
    newer = _seed(client, ns, newer_r)
    assert release_run(_context(client, ns), newer) is ReleaseOutcome.APPLIED
    older = _seed(client, ns, older_r)

    assert release_run(_context(client, ns), older) is ReleaseOutcome.SUPERSEDED

    head = _get(client, f"HEAD#{ns.unit}", "HEAD")
    assert head is not None and head["head_epoch"] == {"N": str(head_epoch(newer_r))}
    run = _get(client, f"RUN#{older}", "RUN")
    assert run is not None and run["fence_reason"] == {"S": "older_than_current"}
    assert "grant_id" not in run
    assert _get(client, due_shard(ns.unit), f"PROMOTE#{ns.unit}") is not None


def test_bf8_positive_r_then_r_plus_one_advance_once_each(client: Any) -> None:
    ns = _namespace()
    first_r, second_r = NOW - timedelta(hours=2), NOW - timedelta(minutes=30)
    first = _seed(client, ns, first_r)
    assert release_run(_context(client, ns), first) is ReleaseOutcome.APPLIED
    _release_post_grant_locks(client, first)
    second = _seed(client, ns, second_r)
    assert release_run(_context(client, ns), second) is ReleaseOutcome.APPLIED
    head = _get(client, f"HEAD#{ns.unit}", "HEAD")
    assert head is not None and head["head_epoch"] == {"N": str(head_epoch(second_r))}


def _legacy_reader() -> tuple[S3CoordinatorLegacyHead, str]:
    """Return the real legacy-head reader over the dev raw zone and coordinator, read-only."""
    bucket, key = os.environ.get("ADL_P4_RAW_BUCKET"), os.environ.get("ADL_P4_LEGACY_KEY")
    if not bucket or not key:
        pytest.fail("ADL_P4_RAW_BUCKET and ADL_P4_LEGACY_KEY are required (no skip)")
    session = boto3.Session()
    table = (
        session.client("ssm")
        .get_parameter(Name=f"/adlytics/{ENV_NAME}/data/v2/semantic-publications-table")[
            "Parameter"
        ]
        .get("Value", "")
    )
    authority = Stage1WriterAuthorityReader(
        str(table), "legacy-head-reads-no-leases", session.client("dynamodb")
    )
    return S3CoordinatorLegacyHead(bucket, session.client("s3"), authority), key


def test_bf8_r_after_a_legacy_completed_newer_retrieval_is_refused(client: Any) -> None:
    reader, key = _legacy_reader()
    identity = window_identity(key)
    older = identity.retrieved_at - timedelta(seconds=1)
    assert reader.head_after(identity.window, older) is not None, "the P0 probe plan completed"
    ns = _namespace()
    name = _seed(client, ns, older, window=identity.window)

    assert release_run(_context(client, ns, reader), name) is ReleaseOutcome.SUPERSEDED

    run = _get(client, f"RUN#{name}", "RUN")
    assert run is not None and run["fence_reason"] == {"S": "older_than_current"}
    assert _get(client, f"HEAD#{ns.unit}", "HEAD") is None


def test_bf8_positive_no_newer_legacy_completed_retrieval_grants(client: Any) -> None:
    reader, key = _legacy_reader()
    identity = window_identity(key)
    newest = NOW + timedelta(days=1)
    assert reader.head_after(identity.window, newest) is None
    ns = _namespace()
    name = _seed(client, ns, newest, window=identity.window)
    assert release_run(_context(client, ns, reader), name) is ReleaseOutcome.APPLIED


def test_v28_an_injected_apply_time_refusal_blocks_only_the_probe_namespace(client: Any) -> None:
    ns = _namespace()
    r = NOW - timedelta(hours=1)
    name = _seed(client, ns, r)
    context = _context(client, ns)
    store = context.store
    from domain.operations.execution_graph_release import reservation_set  # noqa: PLC0415

    run = store.read_run(name)
    assert run is not None
    order = store.read_release_order(ns.domain)
    table = f"integration-probe-db.{ns.unit[:12]}"
    reservation = reservation_set(name, ns.domain, order.next_ordinal, (table,))
    store.acquire_ordinal(order, run, reservation)
    store.reserve_target(reservation, table)
    store.seal_proof(reservation)
    run = store.read_run(name)
    assert run is not None
    issue_grant(context, name, ordinal=reservation.ordinal, expected_revision=run.revision)
    client.put_item(  # fault injection: a newer head appears between grant and apply
        TableName=PROBE,
        Item={
            "pk": write_text(f"HEAD#{ns.unit}"),
            "sk": write_text("HEAD"),
            "retrieved_at": write_text(NOW.isoformat()),
            "head_epoch": write_int(head_epoch(NOW)),
        },
    )

    assert apply_run(context, name) is ReleaseOutcome.LINEAGE_FAILED

    block = _get(client, f"BLOCK#{ns.domain}", "BLOCK")
    assert block is not None and block["block_reason"] == {"S": "lineage_failed_owner_review"}
    failed = _get(client, f"RUN#{name}", "RUN")
    assert failed is not None and failed["state"] == {"S": "FAILED"}
