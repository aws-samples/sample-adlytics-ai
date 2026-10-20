# SPDX-License-Identifier: MIT-0
"""The operator rotation of the edge origin-verify value (ADR-078). Every value is fictional."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from adapters.jobs.rotate_origin_verify import __main__ as job
from domain.identity.origin_verify import ORIGIN_VERIFY_HEADER

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-078")]

# pragma: allowlist nextline secret (A-TEST-LITERALS-001)
_SECRET = "adlytics/dev/edge/origin-verify"  # nosec B105
_OLD = "fictional-old-value"
_TARGET = job.RotationTarget(_SECRET, "E0FICTIONAL")


@dataclass
class _Secrets:
    document: dict[str, str]
    writes: list[dict[str, str]] = field(default_factory=list[dict[str, str]])

    def get_secret_value(self, **request: Any) -> dict[str, object]:
        assert request == {"SecretId": _SECRET}
        return {"SecretString": json.dumps(self.document)}

    def put_secret_value(self, **request: Any) -> dict[str, object]:
        assert request["SecretId"] == _SECRET
        self.document = json.loads(request["SecretString"])
        self.writes.append(self.document)
        return {"VersionId": f"0f1e2d3c-4b5a-4968-8776-65544332211{len(self.writes)}"}


@dataclass
class _CloudFront:
    sent: str
    status: str = "Deployed"

    def get_distribution(self, **_request: Any) -> dict[str, object]:
        api_origin = {
            "CustomHeaders": {
                "Items": [{"HeaderName": ORIGIN_VERIFY_HEADER, "HeaderValue": self.sent}]
            }
        }
        return {
            "Distribution": {
                "Status": self.status,
                "DistributionConfig": {"Origins": {"Items": [{"CustomHeaders": {}}, api_origin]}},
            }
        }


def test_the_secret_name_is_the_one_frontend_stack_owns() -> None:
    assert job.secret_name("dev") == _SECRET


def test_begin_moves_current_to_previous_and_prints_no_value() -> None:
    secrets = _Secrets({"current": _OLD})
    result = job.begin(secrets, _CloudFront(sent=_OLD), _TARGET)
    assert secrets.document["previous"] == _OLD
    assert secrets.document["current"] not in {"", _OLD}
    assert len(secrets.document["current"]) == 64
    assert result == {"step": "begin", "version_id": "0f1e2d3c-4b5a-4968-8776-655443322111"}
    assert _OLD not in json.dumps(result)
    assert secrets.document["current"] not in json.dumps(result)


def test_begin_refuses_while_a_rotation_is_in_progress() -> None:
    secrets = _Secrets({"current": "fictional-new", "previous": _OLD})
    with pytest.raises(job.RotationRefusedError):
        job.begin(secrets, _CloudFront(sent="fictional-new"), _TARGET)
    assert secrets.writes == []


@pytest.mark.parametrize(
    "distribution",
    [_CloudFront(sent="fictional-stale-pinned-value"), _CloudFront(sent=_OLD, status="InProgress")],
)
def test_begin_refuses_a_distribution_not_sending_current(distribution: _CloudFront) -> None:
    """A stale version pin redeployed after a rotation would otherwise lose its value here."""
    secrets = _Secrets({"current": _OLD})
    with pytest.raises(job.RotationRefusedError):
        job.begin(secrets, distribution, _TARGET)
    assert secrets.writes == []


def test_finish_drops_previous_only_once_the_distribution_sends_current() -> None:
    secrets = _Secrets({"current": "fictional-new", "previous": _OLD})
    with pytest.raises(job.RotationRefusedError):
        job.finish(secrets, _CloudFront(sent=_OLD), _TARGET)
    with pytest.raises(job.RotationRefusedError):
        job.finish(secrets, _CloudFront(sent="fictional-new", status="InProgress"), _TARGET)
    assert secrets.writes == []
    result = job.finish(secrets, _CloudFront(sent="fictional-new"), _TARGET)
    assert secrets.document == {"current": "fictional-new"}
    assert result["step"] == "finish"


def test_finish_refuses_when_no_rotation_is_in_progress() -> None:
    secrets = _Secrets({"current": _OLD})
    with pytest.raises(job.RotationRefusedError):
        job.finish(secrets, _CloudFront(sent=_OLD), _TARGET)


class _Sts:
    def __init__(self, account: str) -> None:
        self.account = account

    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": self.account}


class _Ssm:
    def get_parameter(self, **_request: Any) -> dict[str, object]:
        return {"Parameter": {"Value": "E0FICTIONAL"}}


class _Session:
    def __init__(self, account: str, secrets: _Secrets) -> None:
        self.account = account
        self.secrets = secrets

    def client(self, name: str) -> Any:
        if name == "sts":
            return _Sts(self.account)
        if name == "secretsmanager":
            return self.secrets
        if name == "ssm":
            return _Ssm()
        if name == "cloudfront":
            return _CloudFront(sent=_OLD)
        raise AssertionError(name)


def test_main_proves_the_account_before_any_write(capsys: pytest.CaptureFixture[str]) -> None:
    secrets = _Secrets({"current": _OLD})
    argv = [
        "--profile", "fictional", "--environment", "dev", "--region", "us-east-1",
        "--expected-account", "111122223333", "--step", "begin",
    ]  # fmt: skip

    def elsewhere(**_kw: object) -> _Session:
        return _Session("444455556666", secrets)

    def expected(**_kw: object) -> _Session:
        return _Session("111122223333", secrets)

    assert job.main(argv, session_factory=elsewhere) == 1
    assert secrets.writes == []
    assert json.loads(capsys.readouterr().out) == {
        "error_class": "UnexpectedAccount",
        "stage": "identity",
    }
    assert job.main(argv, session_factory=expected) == 0
    printed = capsys.readouterr().out
    assert _OLD not in printed
    assert secrets.document["current"] not in printed
