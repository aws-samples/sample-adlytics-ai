# SPDX-License-Identifier: MIT-0
"""Stamp every legacy CURRENT item with its own identity as the servable pointer.

The Stage 1 tolerance slice must be deployed first: after it, every write is either
Update-shaped (preserving the stamp) or seeds the pointer itself, so the stamp is truthful
for every item this job can see — each was projected inline by the deployed path. The
conditional write (`attribute_not_exists(projected_publication_id)`) makes reruns no-ops
and can never regress a pointer a writer already advanced.

Usage:
    uv run python -m adapters.jobs.migrate_servable_pointer \
        --profile <PROFILE> --env <ENV> --expected-account <ACCOUNT_ID>
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

import boto3
from botocore.exceptions import ClientError

from adapters.outbound.dynamodb.semantic_state import (
    CURRENT_RECORD,
    PARTITION_KEY,
    PROJECTED_AT,
    PROJECTED_FENCING_TOKEN,
    PROJECTED_PUBLICATION_ID,
    PUBLICATION_RECORD_KEY,
)


def main() -> int:
    """Stamp, then verify zero unstamped CURRENT items remain."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--env", required=True)
    parser.add_argument("--expected-account", required=True)
    args = parser.parse_args()
    session = boto3.Session(profile_name=args.profile)
    account = session.client("sts").get_caller_identity()["Account"]
    if account != args.expected_account:
        raise SystemExit(f"unexpected account {account}; refusing to migrate")
    table = f"adlytics-{args.env}-v2-semantic-publications"
    client = session.client("dynamodb")
    now = datetime.now(tz=UTC).isoformat()
    stamped = already = failed = 0
    scan_kwargs: dict[str, object] = {
        "TableName": table,
        "FilterExpression": f"{PUBLICATION_RECORD_KEY} = :current",
        "ExpressionAttributeValues": {":current": {"S": CURRENT_RECORD}},
        "ProjectionExpression": (
            f"{PARTITION_KEY}, publication_id, fencing_token, {PROJECTED_PUBLICATION_ID}"
        ),
    }
    while True:
        response = client.scan(**scan_kwargs)  # type: ignore[arg-type]
        for item in response.get("Items", []):
            if PROJECTED_PUBLICATION_ID in item:
                already += 1
                continue
            try:
                client.update_item(
                    TableName=table,
                    Key={
                        PARTITION_KEY: item[PARTITION_KEY],
                        PUBLICATION_RECORD_KEY: {"S": CURRENT_RECORD},
                    },
                    UpdateExpression=(
                        f"SET {PROJECTED_PUBLICATION_ID} = :projected, "
                        f"{PROJECTED_FENCING_TOKEN} = :token, {PROJECTED_AT} = :at"
                    ),
                    ConditionExpression=f"attribute_not_exists({PROJECTED_PUBLICATION_ID})",
                    ExpressionAttributeValues={
                        ":projected": item["publication_id"],
                        ":token": item["fencing_token"],
                        ":at": {"S": now},
                    },
                )
                stamped += 1
            # error-propagation: operator CLI; a FAILED stderr line, the failed count and exit 1
            except ClientError as error:
                code = str(error.response.get("Error", {}).get("Code", ""))
                if code == "ConditionalCheckFailedException":
                    already += 1
                else:
                    failed += 1
                    sys.stderr.write(f"FAILED {item[PARTITION_KEY]}: {code}\n")
        if "LastEvaluatedKey" not in response:
            break
        scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    print(f"stamped={stamped} already_stamped={already} failed={failed}")
    if failed:
        return 1
    # Verification pass: zero unstamped CURRENT items may remain.
    remaining = 0
    scan_kwargs.pop("ExclusiveStartKey", None)
    while True:
        response = client.scan(**scan_kwargs)  # type: ignore[arg-type]
        remaining += sum(
            1 for item in response.get("Items", []) if PROJECTED_PUBLICATION_ID not in item
        )
        if "LastEvaluatedKey" not in response:
            break
        scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    print(f"unstamped_after_migration={remaining}")
    return 0 if remaining == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
