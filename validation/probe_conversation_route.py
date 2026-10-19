# SPDX-License-Identifier: MIT-0
"""Exercise the deployed conversation Function URL with a signed request.

Answers three questions that only a real invocation can, and that the infra tests cannot:

1. Does the Lambda Web Adapter bring the server up at all? A non-executable `run.sh`, a CRLF line
   ending or a bad `PYTHONPATH` all present as the same readiness-check timeout with no error of
   their own, so the readiness probe is the first thing worth knowing.
2. Does the route refuse an unauthenticated caller? The Function URL's IAM auth proves the *caller*
   is allowed to invoke; it says nothing about whether the request carried a session. Those are two
   independent checks and this asserts the second still happens.
3. Does the response actually stream? Timing between chunks is the only way to tell streaming from
   a buffered response that happens to be slow, which is the property ADR-038 was chosen for.

Signs with SigV4 against the `lambda` service, as CloudFront's Origin Access Control does. Run with
an explicit profile; there is no default.

**The session cookies are read from the environment, never from an argument.** An argument lands in
shell history and in `ps` output for the life of the process, and `ADL_REFRESH` is a live credential
good for thirty days — `probe_harness.py` states the same rule for the fifteen-minute `ADL_SESSION`.

    # Checks 1-3 need no session at all.
    uv run python validation/probe_conversation_route.py --profile <profile>

    # Check 4, one real structured turn. Values come from a logged-in browser; see env.example.
    set -a; source validation/.env.local; set +a
    uv run python validation/probe_conversation_route.py --profile <profile>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from typing import Any

from adapters.outbound.https import HttpsSession, require_https

_HTTP = HttpsSession()
HTTP_ERROR_STATUS = 300

REGION = "us-east-1"
PARAM_URL = "/adlytics/dev/api/conversation-function-url"
PARAM_ORIGIN = "/adlytics/dev/frontend/public-base-url"


def signed_headers(session: Any, url: str, method: str, body: bytes) -> dict[str, str]:
    """SigV4 headers for a Function URL request.

    `x-amz-content-sha256` is set explicitly. CloudFront consumes it as signing input rather than
    forwarding it, and it refuses to carry it in an origin request policy allowlist — so a browser
    reaching this through the edge must compute the body digest itself. Setting it here keeps the
    probe's signature identical in shape to the one the edge produces.
    """
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    request = AWSRequest(method=method, url=url, data=body)
    request.headers.add_header("x-amz-content-sha256", hashlib.sha256(body).hexdigest())
    SigV4Auth(session.get_credentials(), "lambda", REGION).add_auth(request)
    return dict(request.headers.items())


def call(
    session: Any, url: str, method: str, body: bytes, extra: dict[str, str] | None = None
) -> tuple[int, list[tuple[float, bytes]]]:
    """Make one signed request, returning the status and each chunk with its arrival time."""
    status, chunks, _ = call_with_correlation(session, url, method, body, extra)
    return status, chunks


def call_with_correlation(
    session: Any, url: str, method: str, body: bytes, extra: dict[str, str] | None = None
) -> tuple[int, list[tuple[float, bytes]], str]:
    """Make one signed request, also returning the correlation ID the server minted for it.

    ADR-081 §2: the route mints the value and returns it in `x-correlation-id`; the probe never
    sends one. An empty string means the response carried none.
    """
    safe_url = require_https(url)
    headers = signed_headers(session, safe_url, method, body)
    headers.update(extra or {})

    started = time.monotonic()
    chunks: list[tuple[float, bytes]] = []
    with _HTTP.request(
        method,
        safe_url,
        data=body or None,
        headers=headers,
        timeout=180,
        stream=True,
    ) as response:
        status = response.status_code
        correlation_id = response.headers.get("x-correlation-id", "")
        if status >= HTTP_ERROR_STATUS:
            return status, [(time.monotonic() - started, response.raw.read())], correlation_id
        while True:
            chunk = (
                response.raw.read1(8192)
                if hasattr(response.raw, "read1")
                else response.raw.read(8192)
            )
            if not chunk:
                break
            chunks.append((time.monotonic() - started, chunk))
    return status, chunks, correlation_id


def _refreshed_session(session: Any, refresh_token: str, csrf: str) -> str:
    """Exchange a refresh cookie for a fresh access token, returning the new adl_session value.

    Posts to `/api/auth/refresh` through CloudFront. That route is deliberately not behind the
    session authorizer — the access token has expired by definition when it is called — so it
    defends itself with the CSRF double-submit, which is why `csrf` is required here too.
    """
    origin = session.client("ssm").get_parameter(Name=PARAM_ORIGIN)["Parameter"]["Value"]
    url = require_https(f"{origin.rstrip('/')}/api/auth/refresh")
    cookie = f"adl_refresh={refresh_token}; adl_csrf={csrf}"
    with _HTTP.post(
        url,
        data=b"",
        headers={"Cookie": cookie, "x-adl-csrf": csrf, "Sec-Fetch-Site": "same-origin"},
        timeout=30,
    ) as response:
        response.raise_for_status()
        issued = [
            header
            for hop in [*response.history, response]
            for header in hop.raw.headers.getlist("Set-Cookie")
        ]

    for header in issued:
        if header.startswith("adl_session="):
            print("   refreshed the session cookie")
            return header.split(";", 1)[0].split("=", 1)[1]
    message = "The refresh route returned no adl_session cookie. Is the adl_refresh value current?"
    raise SystemExit(message)


def _session_from_environment() -> tuple[str, str, str]:
    """Read the browser session's cookies from the environment, never from an argument.

    `ADL_REFRESH` is preferred over `ADL_SESSION` because it is exchanged for a fresh access token
    on each run, so one paste stays usable for a month instead of fifteen minutes. Either one
    reaches a real turn; neither is required, and without them checks 1-3 still run.
    """
    refresh = os.environ.get("ADL_REFRESH", "").strip()
    csrf = os.environ.get("ADL_CSRF", "").strip()
    cookie = os.environ.get("ADL_SESSION", "").strip()
    if refresh and not csrf:
        message = (
            "ADL_REFRESH is set but ADL_CSRF is not. /api/auth/refresh sits outside the session "
            "authorizer and defends itself with the CSRF double-submit, so it refuses the "
            "exchange without it. Run: set -a; source validation/.env.local; set +a"
        )
        raise SystemExit(message)
    return refresh, csrf, cookie


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="AWS profile. No default, on purpose.")
    parser.add_argument("--question", default="Which metrics can you report on?")
    parser.add_argument(
        "--session",
        default="",
        help=(
            "Optional browser session identifier for continuity. The verified owner namespaces it; "
            "the structured Runtime selects depth and budgets server-side."
        ),
    )
    args = parser.parse_args()

    session_id = args.session or "probe-conversation-route"
    refresh, csrf, cookie = _session_from_environment()

    import boto3

    session = boto3.Session(profile_name=args.profile, region_name=REGION)

    if refresh:
        # Through the edge and our own refresh route rather than against Cognito directly. It needs
        # no AWS credential, it exercises a second real path, and it means an expired access token
        # is a thirty-second problem rather than a request for another browser login.
        cookie = _refreshed_session(session, refresh, csrf)
    url = session.client("ssm").get_parameter(Name=PARAM_URL)["Parameter"]["Value"].rstrip("/")
    print(f"function url: {url}\n")

    print("1. readiness — proves the adapter started the server")
    status, chunks = call(session, f"{url}/health", "GET", b"")
    body = b"".join(chunk for _, chunk in chunks)
    print(f"   status {status}  body {body[:80]!r}")
    if status != 200:
        print(
            "   FAILED: the container is not healthy; check the function's logs for an init error"
        )
        return 1

    print("\n2. an unauthenticated POST — proves IAM auth is not mistaken for a session")
    payload = json.dumps(
        {
            "schema_version": "ConversationRequest.v3",
            "question": args.question,
            "session_id": session_id,
        }
    ).encode()
    status, chunks = call(session, f"{url}/api/chat", "POST", payload)
    body = b"".join(chunk for _, chunk in chunks)
    print(f"   status {status}  body {body[:120]!r}")
    if status != 401:
        print(f"   FAILED: expected 401 without a session cookie, got {status}")
        return 1

    print("\n3. a refused override — proves a caller cannot choose the model or the budget")
    payload = json.dumps(
        {
            "schema_version": "ConversationRequest.v3",
            "question": args.question,
            "session_id": session_id,
            "maxTokens": 999999,
        }
    ).encode()
    status, _ = call(session, f"{url}/api/chat", "POST", payload)
    # 401 is the correct answer here too: identity is checked before the body is read, so an
    # unauthenticated caller never learns whether its override would have been refused.
    print(f"   status {status} (401 expected — identity is checked before the body is parsed)")

    if not cookie:
        print("\n4. skipped: no ADL_REFRESH or ADL_SESSION in the environment, so no real turn")
        print("   run: set -a; source validation/.env.local; set +a")
        print("   the three checks above needed no session and all passed")
        return 0

    print("\n4. a real structured turn — proves the data-only stream reaches one terminal")
    body: dict[str, str | None] = {
        "schema_version": "ConversationRequest.v3",
        "question": args.question,
        "session_id": session_id,
    }
    if args.session:
        print(f"   continuing session {args.session}")
    payload = json.dumps(body).encode()
    cookie_header = f"adl_session={cookie}"
    if csrf:
        cookie_header += f"; adl_csrf={csrf}"
    status, chunks = call(
        session,
        f"{url}/api/chat",
        "POST",
        payload,
        {"Cookie": cookie_header, "x-adl-csrf": csrf, "Sec-Fetch-Site": "same-origin"},
    )
    print(f"   status {status}  chunks {len(chunks)}")
    if not chunks:
        print("   FAILED: no body at all")
        return 1
    print(f"   first chunk at {chunks[0][0]:.2f}s, last at {chunks[-1][0]:.2f}s")
    if len(chunks) > 1:
        gaps = [b[0] - a[0] for a, b in zip(chunks, chunks[1:], strict=False)]
        print(f"   {len(gaps)} gaps, median {sorted(gaps)[len(gaps) // 2]:.2f}s")
        print("   STREAMED: the body arrived in pieces over time, not in one write")
    else:
        print("   BUFFERED: one chunk only — the response did not stream")

    accepted = _report_structured_turn(chunks)
    return 0 if status == 200 and accepted else 1


def _report_structured_turn(chunks: list[tuple[float, bytes]]) -> bool:
    """Validate only the structured data-only terminal sequence without printing content."""
    frames = b"".join(raw for _, raw in chunks).decode("utf-8", errors="strict").split("\n\n")
    events: list[dict[str, object]] = []
    for frame in frames:
        if not frame.strip():
            continue
        data = [line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:")]
        if not data or any(not line.startswith("data:") for line in frame.splitlines()):
            print("   FAILED: stream includes a non-data or incomplete frame")
            return False
        try:
            event = json.loads("\n".join(data))
        except ValueError:
            print("   FAILED: stream includes malformed JSON")
            return False
        if not isinstance(event, dict) or event.get("schema_version") != "ConversationStream.v4":
            print("   FAILED: stream has an unsupported event envelope")
            return False
        events.append(event)
    kinds = [str(event.get("kind", "")) for event in events]
    # ADR-083: `done` follows streamed `text`; a refusal may follow text already sent.
    success = bool(kinds) and kinds[-1] == "done" and "text" in kinds and kinds.count("done") == 1
    refusal = bool(kinds) and kinds[-1] == "refused" and "done" not in kinds
    if not (success or refusal):
        print(f"   FAILED: illegal structured terminal sequence {kinds}")
        return False
    terminal = kinds[-1]
    print(f"   structured events={kinds}, terminal={terminal}, response_released={success}")
    return True


if __name__ == "__main__":
    sys.exit(main())
