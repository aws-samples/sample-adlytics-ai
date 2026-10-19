# SPDX-License-Identifier: MIT-0
"""The HTTPS-only `flow explain` client (FR-8): arms, transport, output and exit status."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from adapters.jobs.operational_status_cli import __main__ as cli
from adapters.jobs.operational_status_cli import client
from adapters.outbound.https import InsecureUrlError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-073")]

_PACKAGE = Path(client.__file__).parent
KEY = "amc/x/window=a/options=b/retrieved=20261006T092520Z/manifest.json"
FLOW = "6a1f0f51-3f0e-5b5c-9a43-1f2d3c4b5a69"


def test_the_client_imports_no_aws_sdk_log_or_trace_client() -> None:
    banned = ("boto3", "botocore", "aws_lambda_powertools", "opentelemetry", "logging")
    for path in _PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not name.startswith(banned), (path.name, name)
                assert "telemetry" not in name, (path.name, name)


@pytest.mark.parametrize(
    ("target", "options", "expected"),
    [
        (FLOW, {}, {"flow_id": FLOW}),
        (KEY, {"version_id": "v1"}, {"manifest": {"key": KEY, "version_id": "v1"}}),
        (KEY, {}, {"manifest": {"key": KEY}}),
        ("-", {"graph_id": "a" * 64}, {"plan": {"graph_id": "a" * 64}}),
        (
            "-",
            {"graph_id": "a" * 64, "node": "bake"},
            {"node": {"graph_id": "a" * 64, "node": "bake"}},
        ),
    ],
)
def test_the_target_maps_to_exactly_one_arm(
    target: str, options: dict[str, str], expected: dict[str, object]
) -> None:
    assert client.reference(target, **options) == expected


def test_a_plaintext_origin_is_refused_before_any_request() -> None:
    with pytest.raises(InsecureUrlError):
        client.explain("http://example.invalid", client.SessionCredential("s"), {"flow_id": FLOW})


class _Response:
    def __init__(self, status: int, body: dict[str, object]) -> None:
        self.status_code = status
        self.content = json.dumps(body).encode()
        self._body = body

    def json(self) -> dict[str, object]:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class _Http:
    def __init__(self, pages: list[tuple[int, dict[str, object]]]) -> None:
        self.pages = pages
        self.sent: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.sent.append({"url": url, **kwargs})
        status, body = self.pages.pop(0)
        return _Response(status, body)


def test_a_request_double_submits_a_fresh_csrf_and_follows_no_redirect() -> None:
    http = _Http([(200, {"complete": False})])

    response = client.explain(
        "https://app.example.invalid/",
        client.SessionCredential("session-value"),
        {"flow_id": FLOW},
        http=http,  # type: ignore[arg-type]
    )

    (sent,) = http.sent
    assert sent["url"] == "https://app.example.invalid/api/operations/execution-explanations"
    assert sent["allow_redirects"] is False
    csrf = sent["headers"]["x-adl-csrf"]
    assert sent["headers"]["Cookie"] == f"adl_session=session-value; adl_csrf={csrf}"
    assert response.status == 200
    assert "session-value" not in repr(client.SessionCredential("session-value"))


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pages: list[tuple[int, dict[str, object]]],
    *extra: str,
) -> tuple[int, _Http]:
    http = _Http(pages)
    session_file = tmp_path / "session"
    session_file.write_text("session-value\n", "utf-8")
    real = client.explain
    monkeypatch.setattr(
        cli,
        "explain",
        lambda origin, credential, body: real(origin, credential, body, http=http),  # type: ignore[arg-type]
    )
    argv = ["explain", FLOW, "--origin", "https://app.example.invalid"]
    status = cli.main([*argv, "--session-file", str(session_file), *extra])
    return status, http


def test_an_incomplete_tree_exits_non_success_and_all_pages_follow_the_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pages: list[tuple[int, dict[str, object]]] = [
        (200, {"complete": False, "next_cursor": "c1", "receipts": [{"request_id": "a"}]}),
        (200, {"complete": False, "next_cursor": None, "receipts": [{"request_id": "b"}]}),
    ]

    status, http = _run(monkeypatch, tmp_path, pages, "--all-pages", "--limit", "1")

    assert status == 2
    assert [json.loads(sent["data"]).get("cursor") for sent in http.sent] == [None, "c1"]
    printed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [page["receipts"][0]["request_id"] for page in printed] == ["a", "b"]


def test_a_refusal_exits_one_and_prints_the_server_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status, _ = _run(monkeypatch, tmp_path, [(404, {"error": "flow_not_found"})])

    assert status == 1
    assert json.loads(capsys.readouterr().out) == {"error": "flow_not_found"}


def test_a_complete_tree_exits_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    status, _ = _run(monkeypatch, tmp_path, [(200, {"complete": True, "next_cursor": None})])

    assert status == 0
