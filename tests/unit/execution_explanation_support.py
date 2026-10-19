# SPDX-License-Identifier: MIT-0
"""Shared fakes for the S2 projection and explanation tests (fictional data only).

`seeded_graph_store` admits real requests through `admit_run_request` against `FakeDynamo`, so
the graph items every test projects and explains are exactly what the S1 adapter writes.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from botocore.exceptions import ClientError
from dynamodb_reserved_words import guard_reserved_words
from execution_graph_support import FakeDynamo, FakeManifests, fixed_now

from adapters.inbound.run_requests.service import (
    AdmissionDependencies,
    RunRequest,
    admit_run_request,
)
from adapters.outbound.dynamodb.execution_graph import DynamoExecutionGraphStore
from adapters.outbound.dynamodb.execution_graph_reader import DynamoExecutionGraphReader
from adapters.outbound.dynamodb.operational_status import (
    DynamoGraphProjectionStore,
    decode_item,
)
from domain.operations.graph_projection import project_graph_item
from domain.operations.ingestion_root import window_identity
from ports.outbound.execution_graph import ReceiptRecord

type Item = dict[str, Any]

WINDOW = (
    # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    "amc/dsp_impressions/query_pack=amc-dsp-impressions/source_scope=s/marketplace=ATVPDKIKX0DER/"
    "window=2026-10-04_2026-10-06/options=44136fa355b3678a/"
)
PROBE_KEY = f"{WINDOW}retrieved=20261006T092520Z/manifest.json"
VERSION_ID = "version-1"
LAST_MODIFIED = datetime(2026, 10, 7, 8, 37, 40, tzinfo=UTC)


@guard_reserved_words
class ReadableFakeDynamo(FakeDynamo):
    """`FakeDynamo` plus the strongly consistent batch read the explanation reader uses."""

    batch_reads: int = 0

    def batch_get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.batch_reads += 1
        ((table, request),) = kwargs["RequestItems"].items()
        assert request["ConsistentRead"] is True
        found = [
            copy.deepcopy(self.items[self._key(key)])
            for key in request["Keys"]
            if self._key(key) in self.items
        ]
        return {"Responses": {table: found}, "UnprocessedKeys": {}}

    def writes(self) -> int:
        """Every committed graph transaction."""
        return len(self.transactions)


@guard_reserved_words
@dataclass
class FakeStatusTable:
    """The status table's conditional put, consistent get and key-ordered query."""

    items: dict[tuple[str, str], Item] = field(default_factory=dict[tuple[str, str], Item])
    puts: int = 0
    queries: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    @staticmethod
    def _key(item: Item) -> tuple[str, str]:
        return (str(item["partition_key"]["S"]), str(item["sort_key"]["S"]))

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        item = kwargs["Item"]
        assert "attribute_not_exists" in kwargs["ConditionExpression"]
        if self._key(item) in self.items:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
                "PutItem",
            )
        self.puts += 1
        self.items[self._key(item)] = copy.deepcopy(item)
        return {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        found = self.items.get(self._key(kwargs["Key"]))
        return {} if found is None else {"Item": copy.deepcopy(found)}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(kwargs)
        values = kwargs["ExpressionAttributeValues"]
        pk, prefix = values[":pk"]["S"], values[":prefix"]["S"]
        start = kwargs.get("ExclusiveStartKey")
        after = None if start is None else start["sort_key"]["S"]
        ordered = sorted(
            (sk, item)
            for (item_pk, sk), item in self.items.items()
            if item_pk == pk and sk.startswith(prefix) and (after is None or sk > after)
        )
        page = [copy.deepcopy(item) for _, item in ordered[: kwargs["Limit"]]]
        response: dict[str, Any] = {"Items": page}
        if len(ordered) > kwargs["Limit"]:
            response["LastEvaluatedKey"] = {
                "partition_key": page[-1]["partition_key"],
                "sort_key": page[-1]["sort_key"],
            }
        return response


@dataclass
class FakeVersions:
    """The raw zone's manifest versions."""

    versions: dict[str, list[tuple[str, datetime]]] = field(
        default_factory=dict[str, list[tuple[str, datetime]]]
    )

    def version_ids(self, key: str) -> tuple[str, ...]:
        return tuple(version for version, _ in self.versions.get(key, []))

    def last_modified(self, key: str, version_id: str) -> datetime | None:
        return dict(self.versions.get(key, [])).get(version_id)


class FakeMac:
    """A deterministic HMAC with a fictional test key; counts calls."""

    def __init__(self, key: bytes = b"fictional-unit-test-key") -> None:
        self._key = key
        self.calls = 0

    def mac(self, message: bytes) -> bytes:
        self.calls += 1
        return hmac.new(self._key, message, hashlib.sha256).digest()

    def verify(self, message: bytes, mac: bytes) -> bool:
        self.calls += 1
        return hmac.compare_digest(self.mac(message), mac)


def run_request(request_id: str, **overrides: Any) -> RunRequest:
    """One on-demand request for the fixed probe manifest version."""
    values: dict[str, Any] = {
        "request_id": request_id,
        "protocol_version": "v1",
        "caller_kind": "on_demand",
        "reason": "operator_rerun",
        "manifest_key": PROBE_KEY,
        "version_id": VERSION_ID,
        "requester": "arn:aws:iam::000000000000:user/operator",
        "requester_is_iam": True,
    }
    values.update(overrides)
    return RunRequest(**values)


@dataclass
class Seeded:
    """A graph store holding real S1 admission output, and its projection."""

    graph: ReadableFakeDynamo
    status: FakeStatusTable
    receipts: list[ReceiptRecord]

    @property
    def reader(self) -> DynamoExecutionGraphReader:
        return DynamoExecutionGraphReader("graph", self.graph)  # type: ignore[arg-type]

    @property
    def projection(self) -> DynamoGraphProjectionStore:
        return DynamoGraphProjectionStore("status", self.status)  # type: ignore[arg-type]

    def project_all(self) -> int:
        """Project every graph item as the stream would deliver it; return rows written."""
        written = 0
        for item in list(self.graph.items.values()):
            row = project_graph_item(decode_item(cast("Any", item)))
            if row is not None and self.projection.apply(row):
                written += 1
        return written


def seeded_graph_store(request_ids: tuple[str, ...] = ("p1-first", "p1-second")) -> Seeded:
    """Admit the requests in SHADOW mode through the real S1 service and adapter."""
    client = ReadableFakeDynamo()
    client.items[("MODE#ingestion", "MODE")] = {
        "pk": {"S": "MODE#ingestion"},
        "sk": {"S": "MODE"},
        "mode": {"S": "SHADOW"},
        "revision": {"N": "1"},
    }
    store = DynamoExecutionGraphStore("graph", client)  # type: ignore[arg-type]
    deps = AdmissionDependencies(
        store=store,
        manifests=FakeManifests(retrieved_at=window_identity(PROBE_KEY).retrieved_at),  # type: ignore[arg-type]
        now=fixed_now,
    )
    receipts = [admit_run_request(run_request(name), deps) for name in request_ids]
    return Seeded(client, FakeStatusTable(), receipts)
