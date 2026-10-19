# SPDX-License-Identifier: MIT-0
r"""Rebuild the status v2 graph projection from execution-graph authority (S2, operator only).

The projection is rebuildable by design: every row is an immutable copy of one graph item. The
table's stream feeds the projector from the moment it is enabled; items committed before that
(and any row an operator wants re-derived) are projected by this bounded job. It reads the graph
table with a strongly consistent scan of the item kinds the projector maps (`graph`, `receipt`,
and S6 part 2's `node`, `claim` and `serving_binding`), maps each through the same pure function
the projector uses, and puts each row idempotently: an exact replay writes nothing, and a row
whose content differs from its authority is reported as a conflict.

It never writes the graph table. It runs under the bound operator profile with an explicit
region, expected account and environment, and resolves both tables through their published
Parameter Store paths (ADR-012). `--dry-run` reads and maps without writing.

    uv run python -m adapters.jobs.operational_status.graph_projection_rebuild \
        --profile <PROFILE> --region us-east-1 --expected-account <ACCOUNT> --env dev [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final

import boto3

from adapters.outbound.dynamodb.metric_store import DynamoMetricStore
from adapters.outbound.dynamodb.operational_status import DynamoGraphProjectionStore, decode_item
from domain.operations.graph_projection import project_graph_item

if TYPE_CHECKING:
    from types_boto3_dynamodb.client import DynamoDBClient

__all__ = ["main", "parameter_path", "projection_store", "rebuild"]

# The scan stops past this many items: the store holds a handful of records per root, and a
# larger table needs a reviewed paging plan rather than an unbounded operator scan. Graph and
# receipt items have no TTL, so the table grows with every root: the dev table held 11,062
# items (7.5 MB) on 2026-10-11, past the first bound of 10,000.
MAX_SCANNED_ITEMS: Final = 100_000


def parameter_path(environment: str, name: str) -> str:
    """Return the exact published path of one operations parameter."""
    return f"/adlytics/{environment}/operations/{name}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--env", required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _parameter(session: boto3.Session, path: str) -> str:
    value = str(session.client("ssm").get_parameter(Name=path)["Parameter"].get("Value", ""))
    if not value.strip():
        raise ValueError(f"published parameter {path} is empty")
    return value


def _scan(session: boto3.Session, table: str) -> list[dict[str, object]]:
    client = session.client("dynamodb")
    request: dict[str, Any] = {
        "TableName": table,
        "ConsistentRead": True,
        "FilterExpression": "item_kind IN (:graph, :receipt, :node, :claim, :serving)",
        "ExpressionAttributeValues": {
            ":graph": {"S": "graph"},
            ":receipt": {"S": "receipt"},
            ":node": {"S": "node"},
            ":claim": {"S": "claim"},
            ":serving": {"S": "serving_binding"},
        },
    }
    found: list[dict[str, object]] = []
    scanned = 0
    while True:
        response = client.scan(**request)
        scanned += int(response.get("ScannedCount", 0))
        if scanned > MAX_SCANNED_ITEMS:
            raise RuntimeError("the graph table exceeds the rebuild scan bound")
        found.extend(decode_item(dict(item)) for item in response.get("Items", []))
        last = response.get("LastEvaluatedKey")
        if last is None:
            return found
        request["ExclusiveStartKey"] = last


def _run(arguments: argparse.Namespace) -> dict[str, object]:
    if re.fullmatch(r"\d{12}", str(arguments.expected_account)) is None:
        raise ValueError("the expected account must be 12 digits")
    if str(arguments.profile).casefold() == "default":
        raise ValueError("an explicit nondefault profile is required")
    session = boto3.Session(profile_name=str(arguments.profile), region_name=str(arguments.region))
    account = str(session.client("sts").get_caller_identity().get("Account", ""))
    if account != str(arguments.expected_account):
        raise PermissionError("caller account does not match the explicitly expected account")
    environment = str(arguments.env)
    graph_table = _parameter(session, parameter_path(environment, "execution-graph-table-name"))
    status_table = _parameter(session, parameter_path(environment, "status-projection-table-name"))
    store = projection_store(status_table, session.client("dynamodb"))
    return rebuild(_scan(session, graph_table), store, dry_run=bool(arguments.dry_run))


def projection_store(status_table: str, client: DynamoDBClient) -> DynamoGraphProjectionStore:
    """Return the projection store the stream projector also uses, measurement store included.

    S5 (FR-3): a receipt row and its `FlowRootElapsed` contribution commit in one transaction on
    every write path. Rows are create-once, so a rebuilt row without its contribution could never
    gain one later.
    """
    return DynamoGraphProjectionStore(
        status_table, client, metrics=DynamoMetricStore(status_table, client)
    )


def rebuild(
    items: Sequence[dict[str, object]], store: DynamoGraphProjectionStore, *, dry_run: bool
) -> dict[str, object]:
    """Map every graph item and put each row once; return the counts and row digests."""
    rows = [row for row in (project_graph_item(item) for item in items) if row is not None]
    written = 0
    if not dry_run:
        written = sum(1 for row in rows if store.apply(row))
    return {
        "dry_run": dry_run,
        "items_read": len(items),
        "rows_mapped": len(rows),
        "rows_written": written,
        "rows_replayed": 0 if dry_run else len(rows) - written,
        "rows": sorted(f"{row.partition_key}|{row.sort_key}|{row.digest}" for row in rows),
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Run the rebuild once and print its counts and row digests."""
    result = _run(_parser().parse_args(argv))
    print(f"RESULT {json.dumps(result, sort_keys=True)}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
