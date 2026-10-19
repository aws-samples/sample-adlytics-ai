# SPDX-License-Identifier: MIT-0
"""M-PROBE-STATUS-PRINT-001: the operational status probe never prints a decoded body."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))
from validation import probe_operational_status as probe  # noqa: E402

pytestmark = pytest.mark.unit

_SENTINEL = "SENTINEL-fictional-body-value"
_failure = probe._failure  # pyright: ignore[reportPrivateUsage]


class TestFailureLine:
    def test_names_the_status_and_not_a_body_field(self) -> None:
        line = _failure(500, {"error": "not_found", "detail": _SENTINEL})

        assert line == "   FAILED: status 500 error=not_found"
        assert _SENTINEL not in line

    def test_each_closed_error_value_is_named(self) -> None:
        assert _failure(503, {"error": "unavailable"}) == (
            "   FAILED: status 503 error=unavailable"
        )

    @pytest.mark.parametrize(
        "body",
        [{"error": _SENTINEL}, {"error": 7}, {}, {"message": _SENTINEL}],
        ids=["unknown-text", "not-text", "absent", "other-member"],
    )
    def test_any_other_error_value_is_omitted(self, body: dict[str, object]) -> None:
        assert _failure(500, body) == "   FAILED: status 500 error=<omitted>"


class _Sts:
    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": "111122223333"}


class _Session:
    def __init__(self, **_: object) -> None:
        pass

    def client(self, name: str) -> _Sts:
        assert name == "sts"
        return _Sts()


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Stub every input, so nothing from the operator's machine or AWS is read."""
    monkeypatch.setattr(probe, "_ENV_FILE", tmp_path / "absent.env")
    monkeypatch.setenv("ADL_REFRESH", "fictional-refresh")
    monkeypatch.setenv("ADL_CSRF", "fictional-csrf")
    monkeypatch.setenv("ADL_BASE_URL", "https://probe.example.invalid")
    monkeypatch.setattr(
        sys,
        "argv",
        ["probe", "--profile", "fixture", "--env", "dev", "--expected-account", "111122223333"],
    )
    monkeypatch.setattr(probe.boto3, "Session", _Session)

    def cookie(*_: object) -> str:
        return "adl_session=x; adl_csrf=y"

    monkeypatch.setattr(probe, "_refreshed_cookie", cookie)


@pytest.mark.usefixtures("stubbed")
def test_main_prints_no_body_value_on_a_failed_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def get(*_: object) -> tuple[int, dict[str, Any]]:
        return 500, {"error": "boom", "secret": _SENTINEL}

    monkeypatch.setattr(probe, "_get", get)

    assert probe.main() == 1
    out = capsys.readouterr().out
    assert _SENTINEL not in out
    assert "   FAILED: status 500 error=<omitted>" in out


@pytest.mark.usefixtures("stubbed")
def test_main_names_a_closed_error_on_a_failed_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def get(*_: object) -> tuple[int, dict[str, Any]]:
        return 503, {"error": "unavailable", "secret": _SENTINEL}

    monkeypatch.setattr(probe, "_get", get)

    assert probe.main() == 1
    out = capsys.readouterr().out
    assert _SENTINEL not in out
    assert "   FAILED: status 503 error=unavailable" in out
