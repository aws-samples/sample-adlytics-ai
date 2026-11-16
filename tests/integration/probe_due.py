# SPDX-License-Identifier: MIT-0
"""Leave the dev probe table's due shards as a live graph suite found them.

A due obligation a suite leaves behind (`ARM#`, `COMPLETE#`, `OUTBOX#`, `PROMOTE#` and the rest)
is read by the next rehearsal's custody preflight, which then refuses as NOT_PROVEN until an
operator deletes it. Each suite claims the conflict units and domains it invents, removes at
teardown exactly the new obligations those namespaces own, and asserts the shards then hold the
keys they held before it ran.
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from domain.operations.generated_execution_graphs import BOUNDS

SHARDS = tuple(f"DUE#{shard}" for shard in range(int(BOUNDS["due_shards"])))

type Key = tuple[str, str]


def due_items(client: Any, table: str) -> dict[Key, dict[str, Any]]:
    """Return every item in the due shards, by (pk, sk), from strongly consistent queries."""
    found: dict[Key, dict[str, Any]] = {}
    for shard in SHARDS:
        request: dict[str, Any] = {
            "TableName": table,
            "KeyConditionExpression": "pk = :pk",
            "ExpressionAttributeValues": {":pk": {"S": shard}},
            "ConsistentRead": True,
        }
        while True:
            page = client.query(**request)
            for item in page["Items"]:
                found[(item["pk"]["S"], item["sk"]["S"])] = item
            if "LastEvaluatedKey" not in page:
                break
            request["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return found


class DueLedger:
    """The due-shard keys one suite found, and the namespaces it claims."""

    def __init__(self, client: Any, table: str) -> None:
        self.client = client
        self.table = table
        self.tokens: set[str] = set()
        self.before = frozenset(due_items(client, table))

    def claim(self, *tokens: str) -> None:
        """Record conflict units or domains this suite invented."""
        self.tokens.update(tokens)

    def _owned(self, sk: str, item: dict[str, Any]) -> bool:
        # `PROMOTE#<unit>` and `ELECTION#<domain>#<page>` name the namespace in the key; every
        # other obligation names its run, whose RUN row names the conflict unit.
        if sk.partition("#")[2].split("#")[0] in self.tokens:
            return True
        run = item.get("run_id", {}).get("S")
        if run is None:
            return False
        row = self.client.get_item(
            TableName=self.table,
            Key={"pk": {"S": f"RUN#{run}"}, "sk": {"S": "RUN"}},
            ConsistentRead=True,
        ).get("Item")
        return row is not None and row.get("conflict_unit", {}).get("S") in self.tokens

    def restore(self) -> list[Key]:
        """Delete each new obligation a claimed namespace owns, if it is still the one read."""
        removed: list[Key] = []
        for (pk, sk), item in due_items(self.client, self.table).items():
            if (pk, sk) in self.before or not self._owned(sk, item):
                continue
            run = item.get("run_id")
            condition: dict[str, Any] = (
                {"ConditionExpression": "attribute_exists(pk)"}
                if run is None
                else {
                    "ConditionExpression": "run_id = :run",
                    "ExpressionAttributeValues": {":run": run},
                }
            )
            try:
                self.client.delete_item(
                    TableName=self.table, Key={"pk": {"S": pk}, "sk": {"S": sk}}, **condition
                )
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    raise
                continue
            removed.append((pk, sk))
        return removed

    def assert_as_found(self) -> None:
        """Fail unless the due shards hold exactly the keys they held before the suite ran."""
        after = frozenset(due_items(self.client, self.table))
        assert sorted(after - self.before) == [], "the suite left due obligations behind"
        assert sorted(self.before - after) == [], "the suite removed due obligations it never made"
