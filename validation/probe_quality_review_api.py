# SPDX-License-Identifier: MIT-0
"""Dev acceptance of the review API's persona x credential x route matrix (ADR-078 §2, §3, §5).

Through the CloudFront URL it reads: each persona on `turn` and `turns` with each credential
(the BFF cookie and the public-client bearer); the credential crossings; the retired per-level
routes; one `turns` walk followed to its last cursor; and that `USER_PASSWORD_AUTH` on the public
client is refused while `ADMIN_USER_PASSWORD_AUTH` issues a bearer only to an IAM-authenticated
caller. It then reads each of four requests' reviewer access-log records by their API Gateway
request identifier (ADR-081 §4, observability §3.8 acceptance 1 and 2). Output is only check names,
route, credential class, expected and observed statuses, and counts: never a token, selector,
identifier, log record, or response body.
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import ssl
import time
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlsplit

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError
from cognito_personas import (
    admin_sign_in,
    app_client,
    load_persona,
    public_client,
    public_password_sign_in_refused,
    published_parameter,
    sign_in,
    unverified_claims,
)

from adapters.outbound.https import HttpsSession, require_https

_REGION = "us-east-1"
_HTTP = HttpsSession()
_CSRF = "quality-review-validation"
_ABSENT_SELECTOR = {"selector": {"audit_id": "quality-review-validation-absent"}}
_WINDOW_DAYS = 31
_WALK_LIMIT = 5
_MAX_WALK_PAGES = 400
_OK = 200
_REFUSED = frozenset({401, 403})
"""The authorizer refuses 403; the edge slice's origin-verify identity source will add API
Gateway's 401 for a request that lacks it. Both are the refusal the credential binding requires."""
_TURN = "/api/quality-review/turn"
_TURNS = "/api/quality-review/turns"
_NON_REVIEW = "/api/me"
_RETIRED = ("audit", "findings", "evaluation")
_PERSONAS = ("reviewer", "attributor", "attribution-only", "denied")
# The stage access-log format ApiStack renders (ADR-081 §4); a record with any other key fails.
_ACCESS_LOG_KEYS = frozenset(
    {
        "requestId",
        "routeKey",
        "status",
        "reviewer",
        "attribution",
        "authorizerError",
        "responseLatency",
        "integrationLatency",
    }
)
# API Gateway renders a variable with no value as "-"; either form counts as empty.
_EMPTY = frozenset({"", "-"})
_REQUEST_ID = re.compile(r"^[A-Za-z0-9=_-]{8,64}$")
_ACCESS_LOG_WAIT_SECONDS = 300
_FILTER_OUTCOME = "complete"
_ACCESS_LOG_POLL_SECONDS = 10
_REVIEWERS = frozenset({"reviewer", "attributor"})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account", required=True)
    return parser


def _account(session: boto3.Session, expected: str) -> None:
    account = session.client("sts").get_caller_identity().get("Account")
    if not isinstance(account, str) or account != expected:
        raise ValueError("explicit profile did not resolve to the expected account")


@dataclass(frozen=True, slots=True)
class _Credential:
    """One credential slot set: an `adl_session` cookie, a bearer, or both, never printed."""

    kind: str
    cookie: str | None = None
    bearer: str | None = None
    extra_cookie: str | None = None

    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"}
        cookies = [part for part in (self.extra_cookie,) if part]
        if self.cookie is not None:
            cookies += [f"adl_session={self.cookie}", f"adl_csrf={_CSRF}"]
            headers["x-adl-csrf"] = _CSRF
        if cookies:
            headers["Cookie"] = "; ".join(cookies)
        if self.bearer is not None:
            headers["Authorization"] = f"Bearer {self.bearer}"
        return headers


def _window() -> dict[str, str]:
    """Return the widest window the API accepts, ending just past now: exactly 31 days."""
    to = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)
    return {"from": (to - timedelta(days=_WINDOW_DAYS)).isoformat(), "to": to.isoformat()}


def _call(
    origin: str, path: str, credential: _Credential, *, body: object | None
) -> tuple[int, dict[str, object] | None]:
    """Send one request; return its status and, for a 200, the decoded body kept in memory."""
    url = require_https(f"{origin}{path}")
    method = "GET" if body is None else "POST"
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    with _HTTP.request(
        method, url, data=data, headers=credential.headers(), timeout=30
    ) as response:
        if response.status_code != _OK:
            return response.status_code, None
        document: object = json.loads(response.text)
    return _OK, cast("dict[str, object]", document) if isinstance(document, dict) else {}


@dataclass
class _Matrix:
    """Collected check outcomes: names, route, credential class, and statuses only."""

    checks: list[dict[str, object]]

    def expect(
        self, name: str, *, route: str, credential: str, expected: str, observed: int
    ) -> None:
        passed = observed in _REFUSED if expected == "refused" else observed == int(expected)
        self.checks.append(
            {
                "check": name,
                "route": route,
                "credential": credential,
                "expected": expected,
                "observed": observed,
                "passed": passed,
            }
        )

    def fact(self, name: str, *, passed: bool, **counts: int) -> None:
        self.checks.append({"check": name, "passed": passed, **counts})

    @property
    def passed(self) -> bool:
        return all(check["passed"] for check in self.checks)


@dataclass(frozen=True)
class _Walk:
    """One `turns` walk: printable counts, and listed rows kept in memory only."""

    counts: dict[str, int]
    rows: tuple[tuple[str, str], ...]
    """Each listed row's audit id and terminal outcome, never printed."""

    @property
    def first(self) -> str | None:
        return self.rows[0][0] if self.rows else None


def _walk_turns(
    origin: str, credential: _Credential, *, window: dict[str, str], filters: object | None = None
) -> _Walk:
    """Follow the cursor across the window and count rows, pages, and short pages."""
    request: dict[str, object] = {"window": window, "limit": _WALK_LIMIT}
    if filters is not None:
        request["filters"] = filters
    seen: Counter[str] = Counter()
    counts = {"pages": 0, "rows": 0, "short_pages_with_cursor": 0, "principal_fields": 0}
    listed: list[tuple[str, str]] = []
    for _ in range(_MAX_WALK_PAGES):
        status, page = _call(origin, _TURNS, credential, body=request)
        if status != _OK or page is None or page.get("schema_version") != "QualityTurnsPage.v1":
            raise ValueError("turns page was not returned")
        rows = cast("list[dict[str, object]]", page.get("turns"))
        cursor = page.get("cursor")
        counts["pages"] += 1
        counts["rows"] += len(rows)
        for row in rows:
            seen[str(row.get("audit_id"))] += 1
            counts["principal_fields"] += int("principal" in row)
            listed.append((str(row.get("audit_id")), str(row.get("terminal_outcome"))))
        if cursor is None:
            break
        if len(rows) < _WALK_LIMIT:
            counts["short_pages_with_cursor"] += 1
        request = {**request, "cursor": cursor}
    else:
        raise ValueError("turns walk did not end within its page bound")
    counts["duplicate_rows"] = sum(count - 1 for count in seen.values())
    return _Walk(counts, tuple(listed))


def filtered_walk_check(unfiltered: _Walk, filtered: _Walk, outcome: str) -> dict[str, object]:
    """Judge a filtered walk against the unfiltered one over the same window (LLD acceptance 3).

    Passes when the filtered walk lists every receipt the unfiltered walk shows with `outcome`,
    each exactly once, and nothing else. Only counts are returned.
    """
    expected = {audit for audit, seen in unfiltered.rows if seen == outcome}
    got = [audit for audit, _ in filtered.rows]
    return {
        "check": "turns_filtered_walk_returns_every_match_once",
        "passed": bool(expected)
        and set(got) == expected
        and len(got) == len(expected)
        and all(seen == outcome for _, seen in filtered.rows),
        "matches": len(expected),
        "rows": len(got),
        "pages": filtered.counts["pages"],
        "short_pages_with_cursor": filtered.counts["short_pages_with_cursor"],
    }


def _path_variants(route: str) -> tuple[tuple[str, str, str], ...]:
    """Non-canonical spellings of one review route (ADR-078 §5), each as (name, method, path)."""
    prefix, _, leaf = route.rpartition("/")
    return (
        ("trailing_slash", "POST", f"{route}/"),
        # Reaches `/api/*` only through CloudFront's own normalization for behaviour matching.
        ("prefix_parent_segment", "POST", f"/validation/..{route}"),
        ("upper_leaf", "POST", f"{prefix}/{leaf.upper()}"),
        ("upper_prefix", "POST", route.replace("/api/", "/API/", 1)),
        ("double_slash_prefix", "POST", route.replace("/api/", "/api//", 1)),
        ("double_slash_leaf", "POST", f"{prefix}//{leaf}"),
        ("dot_segment", "POST", f"{prefix}/./{leaf}"),
        ("parent_segment", "POST", f"{prefix}/validation/../{leaf}"),
        ("encoded_slash", "POST", f"{prefix}%2F{leaf}"),
        ("encoded_leaf_char", "POST", f"{prefix}/{leaf[:-1]}%{ord(leaf[-1]):02X}"),
        ("query_suffix", "POST", f"{route};validation"),
        ("wrong_method", "GET", route),
    )


_REVIEW_SCHEMAS = frozenset({"QualityTurnReview.v1", "QualityTurnsPage.v1"})
_VARIANT_BODY_LIMIT = 65_536


def _raw_request(
    origin: str, *, method: str, path: str, credential: _Credential, body: object
) -> tuple[int, str]:
    """Send exactly `path`, unnormalized, and return the status and the bounded body text."""
    target = urlsplit(require_https(origin))
    host = target.hostname
    if host is None:
        raise ValueError("origin has no host")
    data = json.dumps(body, separators=(",", ":")).encode()
    # A-HTTPS-CONNECTION-001: verifying context, https only; see docs/security-dispositions.md
    # nosemgrep: python.lang.security.audit.httpsconnection-detected.httpsconnection-detected
    connection = http.client.HTTPSConnection(
        host, target.port or 443, timeout=30, context=ssl.create_default_context()
    )
    try:
        connection.request(
            method,
            path,
            body=None if method == "GET" else data,
            # http.client sends no User-Agent, which every real client does and the edge web ACL's
            # core rule set counts as NoUserAgent_HEADER (ADR-078): name the probe instead. Every
            # raw request is a deliberately non-canonical path, so it carries the edge probe marker
            # that excludes it from the count-to-block evidence (validation/probe_edge_entry.py).
            headers={
                **credential.headers(),
                "Host": host,
                "User-Agent": "adlytics-validation",
                "x-adl-edge-probe": "path-variant",
            },
        )
        response = connection.getresponse()
        text = response.read(_VARIANT_BODY_LIMIT).decode("utf-8", errors="replace")
    finally:
        connection.close()
    return response.status, text


def _raw_reached_review(
    origin: str, *, method: str, path: str, credential: _Credential, body: object
) -> tuple[int, bool]:
    """Send exactly `path`, unnormalized; return the status and whether a review came back.

    A variant outside `/api/*` is served by the static site, whose fallback can answer 200 with
    the application shell, so a 2xx alone is not access: only a review document is.

    An HTTP library normalizes dot segments before sending, which would test the library rather
    than the edge, so this uses a raw connection with default certificate verification.
    """
    status, text = _raw_request(origin, method=method, path=path, credential=credential, body=body)
    if not 200 <= status < 300:  # noqa: PLR2004 - the 2xx range
        return status, False
    try:
        document: object = json.loads(text)
    except ValueError:
        return status, False
    if not isinstance(document, dict):
        return status, False
    schema = cast("dict[str, object]", document).get("schema_version")
    return status, schema in _REVIEW_SCHEMAS


_CHAT_VARIANT = "/api/quality-review/../chat"
"""A spelling CloudFront normalizes onto the chat behaviour; the edge function must refuse it."""
_EDGE_REFUSAL = 400
_WAF_BLOCK = 403


def _variants(matrix: _Matrix, origin: str, *, credential: _Credential, selector: object) -> None:
    """Every non-canonical variant of both routes is refused to a caller the canonical route serves.

    The credential is the reviewer's valid bearer, so a review document in the response means
    the variant reached the review function; anything else is a refusal. Only the status and that
    flag are recorded.
    """
    bodies = {_TURN: selector, _TURNS: {"window": _window(), "limit": 1}}
    for route, body in bodies.items():
        for name, method, path in _path_variants(route):
            status, reached = _raw_reached_review(
                origin, method=method, path=path, credential=credential, body=body
            )
            matrix.fact(
                f"variant_{route.rsplit('/', 1)[-1]}_{name}",
                passed=not reached,
                observed=status,
            )
    # The chat origin returns no review document, so refusal is the edge's own answer: the
    # viewer-request function's 400 with an empty body, or, once the web ACL's core rule set blocks
    # (its GenericLFI_URIPATH rule matches a `..` segment first), CloudFront's own 403 block page.
    # The conversation server writes a JSON body on every 400 it returns.
    status, text = _raw_request(
        origin, method="POST", path=_CHAT_VARIANT, credential=credential, body={}
    )
    edge_refused = (status == _EDGE_REFUSAL and not text.strip()) or (
        status == _WAF_BLOCK and "Request blocked" in text
    )
    matrix.fact("variant_chat_parent_segment", passed=edge_refused, observed=status)


def _persona_matrix(
    matrix: _Matrix, origin: str, credentials: dict[tuple[str, str], _Credential]
) -> str | None:
    """Each persona on both routes with both credentials (LLD target acceptance 1 and 2)."""
    reviewer_bearer = credentials[("reviewer", "bearer")]
    window = _window()
    walk = _walk_turns(origin, reviewer_bearer, window=window)
    counts, audit_id = walk.counts, walk.first
    matrix.fact(
        "turns_walk_follows_every_cursor_once",
        passed=counts["duplicate_rows"] == 0 and counts["principal_fields"] == 0,
        **counts,
    )
    filtered = _walk_turns(
        origin, reviewer_bearer, window=window, filters={"terminal_outcome": [_FILTER_OUTCOME]}
    )
    matrix.checks.append(filtered_walk_check(walk, filtered, _FILTER_OUTCOME))
    selector = _ABSENT_SELECTOR if audit_id is None else {"selector": {"audit_id": audit_id}}
    turns_body = {"window": _window(), "limit": 1}
    for (persona, kind), credential in credentials.items():
        reviewer = persona in _REVIEWERS
        status, review = _call(origin, _TURN, credential, body=selector)
        expected = ("200" if audit_id else "404") if reviewer else "refused"
        matrix.expect(
            f"{persona}_turn", route="turn", credential=kind, expected=expected, observed=status
        )
        if reviewer and review is not None:
            matrix.fact(
                f"{persona}_attribution_section",
                passed=("attribution" in review) is (persona == "attributor"),
            )
        status, _ = _call(origin, _TURNS, credential, body=turns_body)
        matrix.expect(
            f"{persona}_turns",
            route="turns",
            credential=kind,
            expected="200" if reviewer else "refused",
            observed=status,
        )
    matrix.fact("a_listed_receipt_was_available_for_turn", passed=audit_id is not None)
    _variants(matrix, origin, credential=reviewer_bearer, selector=selector)
    return audit_id


def _crossings(
    matrix: _Matrix,
    origin: str,
    tokens: dict[str, str],
) -> None:
    """Read each credential binding and crossing (authentication target acceptance 1 to 3)."""
    turns_body = {"window": _window(), "limit": 1}
    bff, public = tokens["bff"], tokens["public"]
    cases = (
        ("bff_cookie_on_non_review", _NON_REVIEW, _Credential("cookie", cookie=bff), None, "200"),
        (
            "cookie_and_bearer",
            _TURNS,
            _Credential("cookie+bearer", cookie=bff, bearer=public),
            turns_body,
            "refused",
        ),
        (
            "bearer_with_unrelated_cookie",
            _TURNS,
            _Credential("bearer+unrelated", bearer=public, extra_cookie="theme=validation"),
            turns_body,
            "200",
        ),
        ("neither_credential", _TURNS, _Credential("none"), turns_body, "refused"),
        (
            "bff_token_as_bearer_on_review",
            _TURNS,
            _Credential("bff-as-bearer", bearer=bff),
            turns_body,
            "refused",
        ),
        (
            "bff_token_as_bearer_on_non_review",
            _NON_REVIEW,
            _Credential("bff-as-bearer", bearer=bff),
            None,
            "refused",
        ),
        (
            "public_bearer_on_non_review",
            _NON_REVIEW,
            _Credential("bearer", bearer=public),
            None,
            "refused",
        ),
        (
            "public_token_as_cookie",
            _TURNS,
            _Credential("public-as-cookie", cookie=public),
            turns_body,
            "refused",
        ),
    )
    for name, path, credential, body, expected in cases:
        status, _ = _call(origin, path, credential, body=body)
        matrix.expect(
            name,
            route=path.rsplit("/", 1)[-1],
            credential=credential.kind,
            expected=expected,
            observed=status,
        )
    for level in _RETIRED:
        status, _ = _call(
            origin,
            f"/api/quality-review/{level}",
            _Credential("cookie", cookie=bff),
            body=_ABSENT_SELECTOR,
        )
        matrix.expect(
            f"retired_{level}_route",
            route=level,
            credential="cookie",
            expected="404",
            observed=status,
        )


@dataclass(frozen=True)
class _Sent:
    """One access-log request: what was sent and what its record must show, kept in memory."""

    name: str
    route_key: str
    expected: str
    status: int
    request_id: str | None
    reviewer: str | None
    attribution: str | None


def _send(
    origin: str, path: str, credential: _Credential, *, body: object | None, correlation: str
) -> tuple[int, str | None]:
    """Send one request with a marker correlation header; return its status and request id."""
    url = require_https(f"{origin}{path}")
    method = "GET" if body is None else "POST"
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    headers = {**credential.headers(), "x-correlation-id": correlation}
    with _HTTP.request(method, url, data=data, headers=headers, timeout=30) as response:
        request_id = response.headers.get("apigw-requestid") or ""
        # Checked before it is spliced into a Logs filter pattern.
        return response.status_code, request_id if _REQUEST_ID.match(request_id) else None


def access_log_checks(
    sent: Sequence[_Sent], records: Mapping[str, str], markers: Sequence[str]
) -> list[dict[str, object]]:
    """Judge each request's access-log record (observability §3.8 acceptance 1 and 2).

    Returns check names, pass flags, and counts only; subjects and records stay in memory.
    """
    checks: list[dict[str, object]] = []
    parsed: dict[str, dict[str, object]] = {}
    for request_id, message in records.items():
        document: object = json.loads(message)
        parsed[request_id] = (
            cast("dict[str, object]", document) if isinstance(document, dict) else {}
        )
    checks.append(
        {
            "check": "access_log_request_ids_returned",
            "passed": all(item.request_id for item in sent),
            "requests": len(sent),
        }
    )
    checks.append(
        {
            "check": "access_log_records_found",
            "passed": all(item.request_id in parsed for item in sent),
            "found": sum(item.request_id in parsed for item in sent),
        }
    )
    checks.append(
        {
            "check": "access_log_record_keys_exactly_the_format",
            "passed": bool(parsed) and all(set(doc) == _ACCESS_LOG_KEYS for doc in parsed.values()),
        }
    )
    checks.append(
        {
            "check": "access_log_holds_no_selector_correlation_or_content",
            "passed": not any(
                marker in message for marker in markers for message in records.values()
            ),
        }
    )
    dashes = 0
    for item in sent:
        record = parsed.get(item.request_id or "", {})
        reviewer, attribution = record.get("reviewer"), record.get("attribution")
        dashes += sum(value == "-" for value in (reviewer, attribution))
        empty = reviewer in _EMPTY and attribution in _EMPTY
        passed = bool(record) and record.get("routeKey") == item.route_key
        if item.reviewer is None:
            passed = passed and empty
        else:
            passed = passed and reviewer == item.reviewer and attribution == item.attribution
        checks.append({"check": f"access_log_{item.name}", "passed": passed})
    # Recorded, never gated: which empty rendering API Gateway used, so the docs can state it.
    checks.append({"check": "access_log_empty_rendering", "passed": True, "dash_values": dashes})
    return checks


def _read_access_log(
    session: boto3.Session, *, group: str, request_ids: Sequence[str], start_ms: int
) -> dict[str, str]:
    """Poll the reviewer access log until every request id's record arrives or time runs out."""
    logs = session.client("logs")
    found: dict[str, str] = {}
    deadline = time.monotonic() + _ACCESS_LOG_WAIT_SECONDS
    while True:
        for request_id in [rid for rid in request_ids if rid not in found]:
            pages = logs.get_paginator("filter_log_events").paginate(
                logGroupName=group,
                startTime=start_ms,
                filterPattern=f'{{ $.requestId = "{request_id}" }}',
            )
            for page in pages:
                events = page.get("events", [])
                if events:
                    found[request_id] = events[0].get("message", "")
                    break
        if len(found) == len(request_ids) or time.monotonic() >= deadline:
            return found
        time.sleep(_ACCESS_LOG_POLL_SECONDS)


def _access_log(
    matrix: _Matrix,
    session: boto3.Session,
    *,
    origin: str,
    environment: str,
    credentials: dict[tuple[str, str], _Credential],
) -> None:
    """Read four requests' reviewer access-log records (observability §3.8 acceptance 1 and 2)."""
    ssm = session.client("ssm")
    arn = published_parameter(ssm, environment, "agentQualityReviewerAccessLogGroupArn")
    group = arn.split(":log-group:", 1)[1].removesuffix(":*")
    audit_marker = f"quality-review-access-log-{uuid.uuid4().hex}"
    correlation = str(uuid.uuid4())
    subject = {
        persona: str(
            unverified_claims(cast("str", credentials[(persona, "bearer")].bearer)).get("sub", "")
        )
        for persona in ("reviewer", "attributor")
    }
    plan = (
        (
            "reviewer_turn",
            "POST /api/quality-review/turn",
            _TURN,
            ("reviewer", "bearer"),
            {"selector": {"audit_id": audit_marker}},
            "404",
            subject["reviewer"],
            "false",
        ),
        (
            "attributor_turns",
            "POST /api/quality-review/turns",
            _TURNS,
            ("attributor", "bearer"),
            {"window": _window(), "limit": 1},
            "200",
            subject["attributor"],
            "true",
        ),
        (
            "non_review_me",
            "GET /api/me",
            _NON_REVIEW,
            ("reviewer", "cookie"),
            None,
            "200",
            None,
            None,
        ),
        (
            "refused_turns",
            "POST /api/quality-review/turns",
            _TURNS,
            ("denied", "bearer"),
            {"window": _window(), "limit": 1},
            "refused",
            None,
            None,
        ),
    )
    start_ms = int(time.time() * 1000) - 60_000
    sent: list[_Sent] = []
    for name, route_key, path, slot, body, expected, reviewer, attribution in plan:
        status, request_id = _send(
            origin, path, credentials[slot], body=body, correlation=correlation
        )
        matrix.expect(
            f"access_log_{name}_request",
            route=path.rsplit("/", 1)[-1],
            credential=slot[1],
            expected=expected,
            observed=status,
        )
        sent.append(_Sent(name, route_key, expected, status, request_id, reviewer, attribution))
    request_ids = [item.request_id for item in sent if item.request_id]
    records = _read_access_log(session, group=group, request_ids=request_ids, start_ms=start_ms)
    matrix.checks.extend(access_log_checks(sent, records, (audit_marker, correlation)))


_DEV_PUBLIC_CLIENT_FLOWS = frozenset({"ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_ADMIN_USER_PASSWORD_AUTH"})


def public_client_check(description: Mapping[str, object]) -> dict[str, object]:
    """Judge the public client's read-back (authentication acceptance 11, dev form)."""
    flows = set(cast("list[str]", description.get("ExplicitAuthFlows") or []))
    return {
        "check": "public_client_reads_back_code_openid_and_dev_flows",
        "passed": description.get("AllowedOAuthFlows") == ["code"]
        and description.get("AllowedOAuthScopes") == ["openid"]
        and "ClientSecret" not in description
        and flows == _DEV_PUBLIC_CLIENT_FLOWS,
    }


def _password_flows(
    matrix: _Matrix, session: boto3.Session, *, credential: Any, client: Any
) -> None:
    """Read the public client's two dev password flows (authentication target acceptance 6)."""
    cognito = session.client("cognito-idp")
    matrix.fact(
        "public_client_user_password_auth_refused",
        passed=public_password_sign_in_refused(cognito, credential, client),
    )
    unsigned = session.client("cognito-idp", config=Config(signature_version=UNSIGNED))
    try:
        admin_sign_in(unsigned, credential, client)
    except ClientError:
        refused = True
    else:
        refused = False
    matrix.fact("admin_password_auth_requires_iam", passed=refused)
    described = cognito.describe_user_pool_client(
        UserPoolId=client.user_pool_id, ClientId=client.client_id
    )["UserPoolClient"]
    matrix.checks.append(public_client_check(cast("Mapping[str, object]", described)))
    # ADR-078 §3 says the public client never carries the Cognito admin scope; the dev password
    # flow is an IAM-authorized exception whose tokens may. Recorded, never gated, so the
    # measured value decides what authentication.md states. The scope set carries no identity.
    scopes = str(
        unverified_claims(admin_sign_in(cognito, credential, client).access_token).get("scope", "")
    ).split()
    matrix.fact(
        "dev_bearer_scope_observation",
        passed=True,
        admin_scope=int("aws.cognito.signin.user.admin" in scopes),
        scope_count=len(scopes),
    )


def main(argv: list[str] | None = None) -> int:
    """Run the matrix and print only its checks; exit nonzero when any check fails."""
    args = _parser().parse_args(argv)
    if args.environment != "dev" or args.region != _REGION:
        print("quality review acceptance accepts only the approved dev region")
        return 2
    matrix = _Matrix(checks=[])
    try:
        session = boto3.Session(profile_name=args.profile, region_name=args.region)
        _account(session, args.expected_account)
        ssm = session.client("ssm")
        cognito = session.client("cognito-idp")
        origin = published_parameter(ssm, args.environment, "publicBaseUrl").rstrip("/")
        bff_client = app_client(session, args.environment)
        pkce_client = public_client(session, args.environment)
        credentials: dict[tuple[str, str], _Credential] = {}
        for persona in _PERSONAS:
            stored = load_persona(ssm, args.environment, persona)
            credentials[(persona, "cookie")] = _Credential(
                "cookie", cookie=sign_in(cognito, stored, bff_client).access_token
            )
            credentials[(persona, "bearer")] = _Credential(
                "bearer", bearer=admin_sign_in(cognito, stored, pkce_client).access_token
            )
        _persona_matrix(matrix, origin, credentials)
        _crossings(
            matrix,
            origin,
            {
                "bff": cast("str", credentials[("reviewer", "cookie")].cookie),
                "public": cast("str", credentials[("reviewer", "bearer")].bearer),
            },
        )
        _password_flows(
            matrix,
            session,
            credential=load_persona(ssm, args.environment, "reviewer"),
            client=pkce_client,
        )
        _access_log(
            matrix,
            session,
            origin=origin,
            environment=args.environment,
            credentials=credentials,
        )
    except Exception:
        print("quality review acceptance could not establish the persona and credential matrix")
        return 1
    print(json.dumps({"checks": matrix.checks, "passed": matrix.passed}, sort_keys=True))
    return 0 if matrix.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
