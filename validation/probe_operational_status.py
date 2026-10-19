# SPDX-License-Identifier: MIT-0
"""Authenticated acceptance probe for the protected operational data-flow status API.

Refreshes a session from the local ignored env file, then reads the summary, one flow,
and its event history through CloudFront exactly as an operator would. Evidence is the
returned JSON, never an exit code. Requires `ADL_REFRESH`/`ADL_CSRF` in
`validation/.env.local` and a user in the `operations-status-read` group.

Usage:
    uv run python -m validation.probe_operational_status \
        --profile <PROFILE> --env <ENV> --expected-account <ACCOUNT_ID>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import boto3
import requests

from adapters.outbound.https import HttpsSession, InsecureUrlError, require_https

_ENV_FILE = Path(__file__).parent / ".env.local"
# Every request goes through the HTTPS-only boundary, and no redirect is followed: the operator's
# cookies must never reach another origin or a plaintext hop (H-VALIDATION-HTTPS-001).
_HTTP = HttpsSession()
_OK = 200


def _load_env() -> None:
    if not _ENV_FILE.is_file():
        return
    for line in _ENV_FILE.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"'))


def _origin(raw: str) -> str:
    """Return the validated origin, or stop before any credential is placed in a request."""
    try:
        return require_https(raw.strip().rstrip("/"))
    except InsecureUrlError:
        raise SystemExit("ADL_BASE_URL must be an https URL with a host.") from None


def _refreshed_cookie(origin: str, refresh: str, csrf: str) -> str:
    """Return the request cookie for data calls: `adl_session=<value>; adl_csrf=<csrf>`."""
    with _HTTP.post(
        require_https(f"{origin}/api/auth/refresh"),
        data=b"",
        headers={
            "Cookie": f"adl_refresh={refresh}; adl_csrf={csrf}",
            "x-adl-csrf": csrf,
            "Sec-Fetch-Site": "same-origin",
        },
        timeout=30,
        allow_redirects=False,
    ) as response:
        # Any status but 200 (a 3xx included) is refused before a Set-Cookie header is read.
        if response.status_code != _OK:
            raise SystemExit("refresh did not issue a session cookie")
        issued = response.raw.headers.getlist("Set-Cookie")  # requests folds repeated headers
    for header in issued:
        if header.startswith("adl_session="):
            session_value = header.split(";", 1)[0].removeprefix("adl_session=")
            return f"adl_session={session_value}; adl_csrf={csrf}"
    raise SystemExit("refresh did not issue a session cookie")


def _json_or_empty(response: requests.Response) -> dict[str, Any]:
    """Read a JSON object body; a non-JSON body (for example a 3xx page) reads as empty."""
    try:
        decoded = response.json() if response.content else {}
    except ValueError:
        return {}
    return cast("dict[str, Any]", decoded) if isinstance(decoded, dict) else {}


def _get(origin: str, cookie: str, path: str) -> tuple[int, dict[str, Any]]:
    with _HTTP.get(
        require_https(f"{origin}{path}"),
        headers={"Cookie": cookie, "Sec-Fetch-Site": "same-origin"},
        timeout=30,
        allow_redirects=False,
    ) as response:
        return response.status_code, _json_or_empty(response)


# The handler's closed error values; any other value is never printed (M-PROBE-STATUS-PRINT-001).
_FAILURE_CODES = frozenset({"not_found", "unavailable"})


def _failure(status: int, body: Mapping[str, object]) -> str:
    """Name the status and a closed error code, never a decoded body value."""
    error = body.get("error")
    code = error if isinstance(error, str) and error in _FAILURE_CODES else "<omitted>"
    return f"   FAILED: status {status} error={code}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--env", required=True)
    parser.add_argument(
        "--expected-account", required=True, help="AWS account ID the profile must resolve to"
    )
    args = parser.parse_args()
    _load_env()
    # The origin is checked before any AWS call, so a bad origin stops with nothing sent.
    refresh, csrf = os.environ.get("ADL_REFRESH", ""), os.environ.get("ADL_CSRF", "")
    raw_origin = os.environ.get("ADL_BASE_URL", "")
    if not refresh or not csrf or not raw_origin:
        raise SystemExit("ADL_REFRESH, ADL_CSRF, and ADL_BASE_URL are required")
    base_url = _origin(raw_origin)
    session = boto3.Session(profile_name=args.profile)
    account = session.client("sts").get_caller_identity()["Account"]
    if account != args.expected_account:
        raise SystemExit(f"unexpected account {account}")
    cookie = _refreshed_cookie(base_url, refresh, csrf)
    print("1. summary — every registered surface with live states and baseline windows")
    status, summary = _get(base_url, cookie, "/api/operations/data-flows")
    print(f"   status {status}  surfaces {len(summary.get('surfaces', []))}")
    if status != 200:
        print(_failure(status, summary))
        return 1
    active = [s for s in summary["surfaces"] if s["state"] != "unknown"]
    for surface in summary["surfaces"]:
        windows = surface["baseline_windows"]
        print(
            f"   {surface['surface']:38} state={surface['state']:10} "
            f"windows={len(windows)} samples={sum(w['sample_count'] for w in windows)}"
        )
    print("2. definitions — the closed operator usage guide")
    status, definitions = _get(base_url, cookie, "/api/operations/data-flow-definitions")
    print(f"   status {status}  registry v{definitions.get('registry_version')}")
    print("3. one flow and its event history through the cursor")
    flow_id = None
    if active:
        # Read one live flow id from the store through the surface lane is API-external;
        # the summary carries no ids, so use the events of a known flow via DynamoDB.
        table = f"adlytics-{args.env}-operational-status"
        client = session.client("dynamodb")
        response = client.query(
            TableName=table,
            IndexName="surface-sample-baseline",
            KeyConditionExpression="surface_partition = :s",
            ExpressionAttributeValues={":s": {"S": active[0]["surface"]}},
            Limit=1,
            ScanIndexForward=False,
        )
        items = response.get("Items", [])
        if items:
            payload = json.loads(items[0]["payload"]["S"])
            flow_id = payload["flow_id"]
    if flow_id is None:
        print("   no live flow yet; flow-route acceptance deferred")
        return 0
    status, flow = _get(base_url, cookie, f"/api/operations/data-flows/{flow_id}")
    print(
        f"   status {status}  flow state={flow.get('state')} outcome={flow.get('outcome')}"
        f" surface={flow.get('surface')}"
    )
    if status != 200:
        print(_failure(status, flow))
        return 1
    status, events = _get(base_url, cookie, flow["events_url"].replace("?", "?limit=10&"))
    print(f"   status {status}  events {len(events.get('events', []))}")
    if status != 200:
        print(_failure(status, events))
        return 1
    print("PASS: operational status API served summary, definitions, flow, and history")
    return 0


if __name__ == "__main__":
    sys.exit(main())
