# SPDX-License-Identifier: MIT-0
"""No-network validation of direct refresh-token storage and output redaction."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from secrets import token_urlsafe
from typing import Any

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).parents[2]))
from validation import mint_token

pytestmark = pytest.mark.unit


class _Sts:
    def get_caller_identity(self) -> dict[str, str]:
        return {
            "Account": "111122223333",
            "Arn": "arn:aws:iam::111122223333:role/validation-operator",
        }


class _Secrets:
    def __init__(self, *, exists: bool = False) -> None:
        self.exists = exists
        self.created: list[dict[str, str]] = []

    def describe_secret(self, *, SecretId: str) -> dict[str, str]:  # noqa: N803
        if self.exists:
            return {"Name": SecretId}
        raise ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "missing"}},
            "DescribeSecret",
        )

    def create_secret(self, **kwargs: str) -> dict[str, str]:
        self.created.append(kwargs)
        return {"Name": kwargs["Name"]}


class _Session:
    def __init__(self, secrets: _Secrets) -> None:
        self.secrets = secrets

    def client(self, service: str) -> Any:
        return _Sts() if service == "sts" else self.secrets


def _arguments() -> list[str]:
    return [
        "mint_token.py",
        "--client-id",
        "fictional-client",
        "--region",
        "na",
        "--redirect-uri",
        "https://example.test/callback",
        "--profile",
        "fictional-profile",
        "--secret-name",
        "example/validation/amc",
    ]


def test_minted_credentials_go_directly_to_the_confirmed_secret_without_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secrets = _Secrets()
    client_credential = token_urlsafe(18)
    refresh_credential = token_urlsafe(24)

    def session_factory(**_kwargs: object) -> _Session:
        return _Session(secrets)

    def answer(_prompt: str) -> str:
        return next(answers)

    def exchange(**_kwargs: object) -> dict[str, str]:
        return {"refresh_token": refresh_credential}

    monkeypatch.setattr(mint_token.boto3, "Session", session_factory)
    answers = iter(("example/validation/amc", "fictional-code"))
    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setenv("AMC_CLIENT_SECRET", client_credential)
    monkeypatch.setattr(mint_token, "exchange_code", exchange)
    monkeypatch.setattr(sys, "argv", _arguments())

    assert mint_token.main() == 0

    assert len(secrets.created) == 1
    assert secrets.created[0]["Name"] == "example/validation/amc"
    stored = json.loads(secrets.created[0]["SecretString"])
    assert stored["client_secret"] == client_credential
    assert stored["refresh_token"] == refresh_credential
    output = capsys.readouterr()
    rendered = output.out + output.err
    assert client_credential not in rendered
    assert refresh_credential not in rendered
    assert "example/validation/amc" not in rendered


def test_typing_anything_but_the_destination_name_creates_nothing_and_requests_no_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """O-CODEQL-005: the confirmation is the typed name, so the script never echoes it."""
    secrets = _Secrets()
    answers = iter(("create",))

    def session_factory(**_kwargs: object) -> _Session:
        return _Session(secrets)

    def answer(_prompt: str) -> str:
        reply = next(answers, None)
        if reply is None:
            pytest.fail("must not request an authorization code")
        return reply

    monkeypatch.setattr(mint_token.boto3, "Session", session_factory)
    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(sys, "argv", _arguments())

    assert mint_token.main() == 1

    assert secrets.created == []
    output = capsys.readouterr()
    assert output.err == "Secret creation cancelled; no authorization code was requested.\n"
    assert "example/validation/amc" not in output.out


def test_existing_secret_is_refused_before_requesting_authorization(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secrets = _Secrets(exists=True)

    def session_factory(**_kwargs: object) -> _Session:
        return _Session(secrets)

    def unexpected_prompt(_prompt: str) -> str:
        pytest.fail("must not prompt")

    monkeypatch.setattr(mint_token.boto3, "Session", session_factory)
    monkeypatch.setattr("builtins.input", unexpected_prompt)
    monkeypatch.setattr(sys, "argv", _arguments())

    assert mint_token.main() == 1
    assert secrets.created == []
    assert "already exists" in capsys.readouterr().err


def test_unexpected_token_response_reports_only_field_names(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secrets = _Secrets()
    client_credential = token_urlsafe(18)
    response_credential = token_urlsafe(24)
    response_detail = token_urlsafe(12)

    def session_factory(**_kwargs: object) -> _Session:
        return _Session(secrets)

    def answer(_prompt: str) -> str:
        return next(answers)

    def exchange(**_kwargs: object) -> dict[str, object]:
        return {
            "access_token": {"unexpected": response_credential},
            "error_description": response_detail,
        }

    monkeypatch.setattr(mint_token.boto3, "Session", session_factory)
    answers = iter(("example/validation/amc", "fictional-code"))
    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setenv("AMC_CLIENT_SECRET", client_credential)
    monkeypatch.setattr(mint_token, "exchange_code", exchange)
    monkeypatch.setattr(sys, "argv", _arguments())

    assert mint_token.main() == 1

    rendered = capsys.readouterr().err
    assert "access_token" in rendered
    assert "error_description" in rendered
    assert response_credential not in rendered
    assert response_detail not in rendered
    assert secrets.created == []
