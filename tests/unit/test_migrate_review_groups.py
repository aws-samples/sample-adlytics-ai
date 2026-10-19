# SPDX-License-Identifier: MIT-0
"""The operator move of retired review-group members into `quality-reviewer` (ADR-078 §3)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from botocore.exceptions import ClientError

from adapters.jobs.migrate_review_groups import __main__ as job

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-078")]

_POOL = "us-east-1_fictional"


@dataclass
class _Cognito:
    groups: dict[str, list[str]]
    calls: list[tuple[str, str, str]] = field(default_factory=list[tuple[str, str, str]])

    absent: frozenset[str] = frozenset()
    pool_exists: bool = True

    def describe_user_pool(self, **_request: Any) -> dict[str, object]:
        if not self.pool_exists:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "fictional"}},
                "DescribeUserPool",
            )
        return {"UserPool": {"Id": _POOL}}

    def list_users_in_group(self, **request: Any) -> dict[str, object]:
        if request["GroupName"] in self.absent:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "fictional"}},
                "ListUsersInGroup",
            )
        users = self.groups.get(request["GroupName"], [])
        start = int(request.get("NextToken", "0"))
        page = users[start : start + 2]
        response: dict[str, object] = {"Users": [{"Username": name} for name in page]}
        if start + 2 < len(users):
            response["NextToken"] = str(start + 2)
        return response

    def admin_add_user_to_group(self, **request: Any) -> None:
        self.calls.append(("add", request["Username"], request["GroupName"]))
        members = self.groups.setdefault(request["GroupName"], [])
        if request["Username"] not in members:
            members.append(request["Username"])

    def admin_remove_user_from_group(self, **request: Any) -> None:
        self.calls.append(("remove", request["Username"], request["GroupName"]))
        self.groups[request["GroupName"]].remove(request["Username"])


def _pool() -> _Cognito:
    return _Cognito(
        groups={
            "quality-review-audit": ["user-a", "user-b", "user-c"],
            "quality-review-findings": ["user-a"],
            "quality-review-evaluation": [],
        }
    )


def test_every_member_joins_quality_reviewer_before_leaving_its_retired_group() -> None:
    cognito = _pool()

    result = job.migrate(cognito, _POOL, dry_run=False)

    assert result.remaining == 0
    assert result.memberships_moved == 4
    assert result.distinct_members == 3
    assert sorted(cognito.groups["quality-reviewer"]) == ["user-a", "user-b", "user-c"]
    for index, (action, username, group) in enumerate(cognito.calls):
        if action == "remove":
            assert cognito.calls[index - 1] == ("add", username, "quality-reviewer")
            assert group in job.RETIRED_GROUPS
    assert "audit-attribution" not in cognito.groups


def test_a_dry_run_writes_nothing_and_reports_what_would_move() -> None:
    cognito = _pool()

    result = job.migrate(cognito, _POOL, dry_run=True)

    assert cognito.calls == []
    assert (result.memberships_moved, result.remaining, result.dry_run) == (4, 4, True)


class _Session:
    def __init__(self, account: str, cognito: _Cognito) -> None:
        self._account = account
        self._cognito = cognito

    def client(self, name: str) -> object:
        if name == "sts":
            return _Sts(self._account)
        if name == "ssm":
            return _Ssm()
        return self._cognito


@dataclass
class _Sts:
    account: str

    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": self.account}


class _Ssm:
    def get_parameter(self, *, Name: str) -> dict[str, object]:  # noqa: N803 - boto3 keyword
        assert Name == "/adlytics/stage/auth/user-pool-id"
        return {"Parameter": {"Value": _POOL}}


def _factory(account: str, cognito: _Cognito) -> Any:
    def build(**_kwargs: object) -> _Session:
        return _Session(account, cognito)

    return build


_ARGS = [
    "--profile",
    "fictional",
    "--environment",
    "stage",
    "--region",
    "eu-west-1",
    "--expected-account",
    "111111111111",
]


def test_the_job_runs_in_any_environment_and_prints_counts_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cognito = _pool()

    code = job.main(_ARGS, session_factory=_factory("111111111111", cognito))

    printed = capsys.readouterr().out
    assert code == 0
    assert json.loads(printed)["remaining"] == 0
    assert "user-" not in printed


def test_an_unexpected_account_is_refused_before_any_read(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cognito = _pool()

    code = job.main(_ARGS, session_factory=_factory("222222222222", cognito))

    assert code == 1
    assert json.loads(capsys.readouterr().out)["error_class"] == "UnexpectedAccount"
    assert cognito.calls == []


def test_a_rerun_after_the_groups_are_removed_moves_nothing_and_reports_none_remaining() -> None:
    cognito = _Cognito(groups={}, absent=frozenset(job.RETIRED_GROUPS))
    result = job.migrate(cognito, _POOL, dry_run=False)
    assert (result.memberships_moved, result.remaining, result.distinct_members) == (0, 0, 0)
    assert result.absent_groups == len(job.RETIRED_GROUPS)
    assert cognito.calls == []


def test_a_missing_user_pool_fails_the_run_instead_of_reading_as_removed_groups() -> None:
    cognito = _Cognito(groups={}, absent=frozenset(job.RETIRED_GROUPS), pool_exists=False)
    with pytest.raises(ClientError):
        job.migrate(cognito, _POOL, dry_run=False)


def test_any_other_cognito_error_still_stops_the_migration() -> None:
    class _Broken(_Cognito):
        def list_users_in_group(self, **_request: Any) -> dict[str, object]:
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "fictional"}},
                "ListUsersInGroup",
            )

    with pytest.raises(ClientError):
        job.migrate(_Broken(groups={}), _POOL, dry_run=False)
