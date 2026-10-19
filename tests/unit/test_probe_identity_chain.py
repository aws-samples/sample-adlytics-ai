# SPDX-License-Identifier: MIT-0
"""Identity-chain probe start: an unreadable Harness retrieval configuration is a refusal."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))
from validation import probe_identity_chain as probe  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-147")]

_ACCOUNT = "111122223333"


class _Sts:
    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": _ACCOUNT}


class _Session:
    def __init__(self, **_kwargs: object) -> None:
        pass

    def client(self, name: str) -> object:
        return _Sts() if name == "sts" else object()


@pytest.mark.parametrize(
    "error",
    [
        ValueError("the Harness must retrieve exactly one /semantics/ or /context/ namespace"),
        ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "GetHarness"),
        EndpointConnectionError(endpoint_url="https://bedrock-agentcore-control.fictional"),
    ],
    ids=["retrieval-invariant", "throttling", "connection"],
)
def test_an_unreadable_retrieval_configuration_is_exit_2_before_any_case(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: Exception
) -> None:
    def _namespaces(_client: object, _harness_id: str) -> tuple[str, ...]:
        raise error

    def _parameter(_ssm: object, environment: str, name: str) -> str:
        return f"arn:aws:bedrock-agentcore:us-east-1:{_ACCOUNT}:{environment}/{name}-fictional"

    def _no_case(*_args: object) -> list[object]:
        raise AssertionError("no case may run")

    monkeypatch.setattr(probe.boto3, "Session", _Session)
    monkeypatch.setattr(probe, "published_parameter", _parameter)
    monkeypatch.setattr(probe, "cross_session_namespaces", _namespaces)
    monkeypatch.setattr(probe, "_run", _no_case)
    argv = [
        "--profile", "fictional", "--environment", "dev", "--region", "us-east-1",
        "--expected-account", _ACCOUNT, "--case", "p1",
    ]  # fmt: skip

    assert probe.main(argv) == 2

    assert capsys.readouterr().out.splitlines() == [
        "could not read the Harness's cross-session memory namespaces "
        f"({type(error).__name__}); no case ran"
    ]
