# SPDX-License-Identifier: MIT-0
r"""``adapters.jobs.migrate_review_groups``: move the retired review groups' members (ADR-078 §3).

Slice 6 replaces the three per-level Cognito groups with `quality-reviewer` and
`audit-attribution`. Before the old groups are removed, every member of each retired group is
added to `quality-reviewer` and only then removed from the retired group, so no member is ever
without review access. Each retired group is then read back and required empty. A retired level
never implied attribution, so no member is added to `audit-attribution`.

The caller account is proved with STS before the user pool is resolved. ``--dry-run`` writes
nothing and reports what a run would move. Output is counts only: never a username, subject, or
group membership list. A retired group that still has members after a run exits nonzero, and the
old groups are not removed until a run reports none remaining.

Usage:
    uv run python -m adapters.jobs.migrate_review_groups \
        --profile <PROFILE> --environment <ENV> --region <REGION> \
        --expected-account <ACCOUNT_ID> [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError

from adapters.jobs.stage1_operator_bootstrap import parameter_path

QUALITY_REVIEWER = "quality-reviewer"
RETIRED_GROUPS = ("quality-review-audit", "quality-review-findings", "quality-review-evaluation")
_PAGE_LIMIT = 60


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """Counts only: how many memberships were (or would be) moved and how many remain."""

    retired_groups: int
    absent_groups: int
    memberships_moved: int
    distinct_members: int
    remaining: int
    dry_run: bool

    def document(self) -> dict[str, object]:
        """Return the printable counts."""
        return {
            "dry_run": self.dry_run,
            "retired_groups": self.retired_groups,
            "absent_groups": self.absent_groups,
            "memberships_moved": self.memberships_moved,
            "distinct_members": self.distinct_members,
            "remaining": self.remaining,
        }


def members(cognito: Any, pool_id: str, group: str) -> tuple[str, ...] | None:
    """Read every username in one group, following pagination to the end; `None` if it is gone.

    The caller has already proved the pool exists, so a not-found here names the group.
    """
    usernames: list[str] = []
    token: str | None = None
    while True:
        request: dict[str, object] = {
            "UserPoolId": pool_id,
            "GroupName": group,
            "Limit": _PAGE_LIMIT,
        }
        if token is not None:
            request["NextToken"] = token
        try:
            response = cognito.list_users_in_group(**request)
        except ClientError as error:
            # A group already removed (slice 6 commit B) has no members left to move.
            if error.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                return None
            raise
        for user in response.get("Users", []):
            username = user.get("Username")
            if not isinstance(username, str) or not username:
                raise ValueError("group member listing is malformed")
            usernames.append(username)
        next_token = response.get("NextToken")
        if not next_token:
            return tuple(usernames)
        token = str(next_token)


def migrate(cognito: Any, pool_id: str, *, dry_run: bool) -> MigrationResult:
    """Add each retired-group member to `quality-reviewer`, then remove it from the retired group.

    Adding is idempotent in Cognito, so a rerun after a partial run completes it.
    """
    # Cognito answers a missing pool with the same not-found code as a missing group, so the pool
    # is proved first: a wrong pool must fail the run, not read as groups already removed.
    cognito.describe_user_pool(UserPoolId=pool_id)
    moved = 0
    absent = 0
    distinct: set[str] = set()
    for group in RETIRED_GROUPS:
        listed = members(cognito, pool_id, group)
        if listed is None:
            absent += 1
            continue
        for username in listed:
            distinct.add(username)
            moved += 1
            if dry_run:
                continue
            cognito.admin_add_user_to_group(
                UserPoolId=pool_id, Username=username, GroupName=QUALITY_REVIEWER
            )
            cognito.admin_remove_user_from_group(
                UserPoolId=pool_id, Username=username, GroupName=group
            )
    remaining = (
        moved
        if dry_run
        else sum(len(members(cognito, pool_id, group) or ()) for group in RETIRED_GROUPS)
    )
    return MigrationResult(
        retired_groups=len(RETIRED_GROUPS),
        absent_groups=absent,
        memberships_moved=moved,
        distinct_members=len(distinct),
        remaining=remaining,
        dry_run=dry_run,
    )


def build_parser() -> argparse.ArgumentParser:
    """Every target-affecting input is explicit; none has a default."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _print(document: dict[str, object]) -> None:
    print(json.dumps(document, sort_keys=True))  # noqa: T201 - operator counts only


def main(
    argv: list[str] | None = None,
    *,
    session_factory: Callable[..., Any] = boto3.Session,
) -> int:
    """Prove the account, resolve the pool, migrate (or report), and print counts."""
    args = build_parser().parse_args(argv)
    session = session_factory(profile_name=args.profile, region_name=args.region)
    try:
        account = session.client("sts").get_caller_identity()["Account"]
    # error-propagation: operator CLI; the printed identity-stage error_class and exit 1
    except Exception as error:
        _print({"stage": "identity", "error_class": type(error).__name__})
        return 1
    if account != args.expected_account:
        _print({"stage": "identity", "error_class": "UnexpectedAccount"})
        return 1
    try:
        pool_id = session.client("ssm").get_parameter(
            Name=parameter_path(args.environment, "userPoolId")
        )["Parameter"]["Value"]
        if not isinstance(pool_id, str) or not pool_id:
            raise ValueError("user pool parameter is invalid")
        result = migrate(session.client("cognito-idp"), pool_id, dry_run=args.dry_run)
    # error-propagation: operator CLI; the printed migration-stage error_class and exit 1
    except Exception as error:
        _print({"stage": "migration", "error_class": type(error).__name__})
        return 1
    _print(result.document())
    return 1 if result.remaining and not result.dry_run else 0


if __name__ == "__main__":
    sys.exit(main())
