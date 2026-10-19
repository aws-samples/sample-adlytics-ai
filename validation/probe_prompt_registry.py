# SPDX-License-Identifier: MIT-0
"""Ship one insight-slot prompt version through the deployed registry.

Create, calibrate, then activate: the only path a prompt reaches an environment (ADR-069).
Route grammar is owned by docs/ui/prompt-administration/low-level-design.md §1; this script
composes exactly those calls and prints no prompt text, cookie value, or live identifier.

    set -a; source validation/.env.local; set +a
    uv run python validation/probe_prompt_registry.py --slot retail-metric-focus \
        --prompt-file /path/to/prompt.txt --expected-active 1

Credentials come from the environment (ADL_REFRESH, ADL_CSRF, ADL_BASE_URL), never from an
argument. The operator identity is the logged-in browser session's; the body carries none.
ADL_EXPECTED_AUTHOR_EMAIL is that operator's email: the probe asserts the created version is
attributed to it (ADR-069) and prints only whether it matched, never the address.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from adapters.outbound.https import HttpsSession, InsecureUrlError, require_https

# Every request goes through the HTTPS-only boundary, and no redirect is followed: the operator's
# cookies must never reach another origin or a plaintext hop (H-VALIDATION-HTTPS-001).
_HTTP = HttpsSession()
_HEADERS_BASE = {"Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"}
_OK = 200
_ACCEPTED = (201, 202)
_NOT_PASSED = 2


def _require_https(url: str) -> str:
    try:
        return require_https(url)
    except InsecureUrlError:
        raise SystemExit("ADL_BASE_URL must be https.") from None


def _refreshed_session(origin: str, refresh: str, csrf: str) -> str:
    """Exchange the refresh cookie for a fresh session cookie through our own refresh route."""
    with _HTTP.post(
        require_https(f"{origin}/api/auth/refresh"),
        data=b"",
        headers={
            **_HEADERS_BASE,
            "Cookie": f"adl_refresh={refresh}; adl_csrf={csrf}",
            "x-adl-csrf": csrf,
        },
        timeout=30,
        allow_redirects=False,
    ) as response:
        # Any status but 200 (a 3xx included) is refused before a Set-Cookie header is read.
        if response.status_code != _OK:
            raise SystemExit(
                "The refresh route returned no session cookie. Is ADL_REFRESH current?"
            )
        issued = response.raw.headers.getlist("Set-Cookie")  # requests folds repeated headers
    for header in issued:
        if header.startswith("adl_session="):
            return header.split(";", 1)[0].split("=", 1)[1]
    raise SystemExit("The refresh route returned no session cookie. Is ADL_REFRESH current?")


class Registry:
    """The prompt-registry routes, called as the logged-in operator."""

    def __init__(self, origin: str, session_cookie: str, csrf: str) -> None:
        self._origin = origin
        self._cookie = f"adl_session={session_cookie}; adl_csrf={csrf}"
        self._csrf = csrf

    def post(self, route: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """POST one registry command; return the status and the decoded body."""
        status, decoded, _headers = self.post_with_headers(route, body)
        return status, decoded

    def post_with_headers(
        self, route: str, body: dict[str, Any]
    ) -> tuple[int, dict[str, Any], Mapping[str, str]]:
        """POST one registry command; return the status, the body, and lower-cased headers."""
        with _HTTP.post(
            require_https(f"{self._origin}/api/prompt-registry/{route}"),
            data=json.dumps(body).encode(),
            headers={**_HEADERS_BASE, "Cookie": self._cookie, "x-adl-csrf": self._csrf},
            timeout=60,
            allow_redirects=False,
        ) as response:
            headers = {name.lower(): value for name, value in response.headers.items()}
            payload = response.content
        try:
            decoded = json.loads(payload or b"{}")
        except json.JSONDecodeError:
            decoded = {"raw_length": len(payload)}
        return response.status_code, decoded, headers


def _listed_versions(status: int, listed: dict[str, Any]) -> list[dict[str, Any]]:
    if status != _OK:
        return []
    return cast("list[dict[str, Any]]", listed.get("versions", []))


def _version_record(versions: list[dict[str, Any]], version: int) -> dict[str, Any] | None:
    return next((item for item in versions if item.get("version") == version), None)


def _attribution_kind(record: dict[str, Any] | None) -> tuple[object, object]:
    """Return a version's `created_by` kind and email, or `None`s when it has neither."""
    created_by = None if record is None else record.get("created_by")
    if not isinstance(created_by, dict):
        return None, None
    attribution = cast("dict[str, object]", created_by)
    return attribution.get("kind"), attribution.get("email")


@dataclass(frozen=True, slots=True)
class _AuthorCheck:
    slot: str
    version: int
    expected_email: str


def _check_author(registry: Registry, check: _AuthorCheck) -> int:
    """Assert the created version is attributed to the expected email; print no address.

    Prints the response's correlation ID and the UTC window around the call, so the span read
    of acceptance A-B1-3 can find the request's trace, and the count of legacy versions that
    read as not recorded (A-B1-2).
    """
    window_start = datetime.now(UTC)
    status, listed, headers = registry.post_with_headers("versions", {"slot_id": check.slot})
    window_end = datetime.now(UTC)
    print(
        f"   x-correlation-id={headers.get('x-correlation-id')} "
        f"window_utc={window_start.isoformat()}/{window_end.isoformat()}"
    )
    versions = _listed_versions(status, listed)
    legacy = sum(1 for item in versions if _attribution_kind(item)[0] == "not_recorded")
    print(f"   versions status={status} not_recorded={legacy}")
    kind, email = _attribution_kind(_version_record(versions, check.version))
    if kind != "operator_email" or email != check.expected_email:
        print(f"   author=mismatch kind={kind}")
        return 1
    print("   author=matched")
    return 0


def _created_version(registry: Registry, args: argparse.Namespace, prompt: str) -> tuple[int, str]:
    """Create the version; return it and its outcome, or exit when the route refuses."""
    status, created = registry.post(
        "create-version",
        {"slot_id": args.slot, "prompt_template": prompt, "model_routing_hint": args.route},
    )
    if status not in _ACCEPTED:
        raise SystemExit(f"create-version refused: status={status} body_keys={sorted(created)}")
    version = created.get("version")
    if not isinstance(version, int):
        raise SystemExit(f"create-version returned no integer version: keys={sorted(created)}")
    outcome = str(created.get("outcome"))
    print(f"   created v{version}; calibration outcome: {outcome}")
    return version, outcome


def _calibrated(
    registry: Registry, args: argparse.Namespace, created: tuple[int, str]
) -> tuple[str, dict[str, Any] | None]:
    """Poll the versions projection until calibration settles; return the outcome and record."""
    version, outcome = created
    attempts = 0
    record: dict[str, Any] | None = None
    prior_delay, delay = 1, 2
    while outcome == "pending" and attempts < args.max_retries:
        attempts += 1
        time.sleep(delay)
        status, listed = registry.post("versions", {"slot_id": args.slot})
        record = _version_record(_listed_versions(status, listed), version)
        if record is None:
            raise SystemExit(f"   calibration status read failed: status={status}")
        outcome = str(record.get("calibration"))
        print(f"   poll {attempts}: calibration={outcome}")
        prior_delay, delay = delay, min(13, prior_delay + delay)
    if record is None:
        status, listed = registry.post("versions", {"slot_id": args.slot})
        record = _version_record(_listed_versions(status, listed), version)
    return outcome, record


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", required=True)
    parser.add_argument("--prompt-file", required=True, type=Path)
    parser.add_argument("--expected-active", required=True, type=int)
    parser.add_argument("--route", default="quick", choices=("quick", "deep"))
    parser.add_argument("--max-retries", default=26, type=int)
    return parser.parse_args()


def main() -> int:
    """Create, calibrate, attribute, and (when calibration passed) activate one version."""
    args = _arguments()
    origin = _require_https(os.environ.get("ADL_BASE_URL", "").strip().rstrip("/"))
    refresh = os.environ.get("ADL_REFRESH", "").strip()
    csrf = os.environ.get("ADL_CSRF", "").strip()
    expected_email = os.environ.get("ADL_EXPECTED_AUTHOR_EMAIL", "").strip()
    if not (origin and refresh and csrf and expected_email):
        raise SystemExit(
            "Source validation/.env.local "
            "(ADL_BASE_URL, ADL_REFRESH, ADL_CSRF, ADL_EXPECTED_AUTHOR_EMAIL)."
        )
    prompt = args.prompt_file.read_text().strip()
    if not prompt:
        raise SystemExit("The prompt file is empty.")

    registry = Registry(origin, _refreshed_session(origin, refresh, csrf), csrf)
    print(f"slot {args.slot}: prompt {len(prompt)} chars, expected active v{args.expected_active}")

    status, versions_before = registry.post("versions", {"slot_id": args.slot})
    if status != _OK:
        print(f"versions read refused: status={status} body_keys={sorted(versions_before)}")
        return 1
    known = _listed_versions(status, versions_before)
    print(f"   versions before: {[item.get('version') for item in known]}")

    # create-version persists the immutable version and durably queues calibration; the
    # existing version projection is the polling surface and never starts model work.
    version, outcome = _created_version(registry, args, prompt)
    outcome, record = _calibrated(registry, args, (version, outcome))
    cases = None if record is None else record.get("calibration_cases")
    recorded = None if record is None else record.get("calibration")
    print(f"   recorded calibration: {recorded} cases={cases}")
    if _check_author(registry, _AuthorCheck(args.slot, version, expected_email)) != 0:
        return 1
    if outcome != "passed":
        print(f"   not activating: calibration={outcome}")
        return _NOT_PASSED

    status, activated = registry.post(
        "activate",
        {"slot_id": args.slot, "version": version, "expected_active_version": args.expected_active},
    )
    if status != _OK:
        print(f"activate refused: status={status} body_keys={sorted(activated)}")
        return 1
    event = activated.get("event", {})
    print(f"   activated: from v{event.get('from_version')} to v{event.get('to_version')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
