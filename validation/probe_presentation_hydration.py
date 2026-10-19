# SPDX-License-Identifier: MIT-0
"""Attest served hydration for every presentation page, roster campaign, and preset.

Walks the deployed V2 presentation API exactly as the browser does: the campaign
portfolio and retail-health parents under each declared preset, every roster
campaign's detail under each preset, bounded metric-focus samples per surface, and
every other baked roster page (from the generated ``BAKE_TASKS``) under each of its
baked variants.
Distinguishes designed silence (window_unavailable envelopes, ``insufficient``
outcomes with reason codes, ``silent`` insight slots) from stranding
(``unavailable`` insight slots on a populated page, missing declared metrics,
an empty roster where data exists, or any non-200 response).

Evidence is one JSON report under ``validation/evidence/reports/`` (ignored);
stdout carries only bounded counts. Requires ``ADL_BASE_URL``/``ADL_REFRESH``/
``ADL_CSRF`` in ``validation/.env.local``; credential values are never printed.

Usage:
    uv run python validation/probe_presentation_hydration.py \
        --profile <PROFILE> --environment <ENV> --region <REGION> \
        --expected-account <ACCOUNT_ID> --label baseline
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, NoReturn, cast
from uuid import uuid4

import boto3
import requests
from botocore.exceptions import ClientError

from probe_deadlines import ProbeDeadlineError, bounded_client, within

from adapters.outbound.https import HttpsSession, InsecureUrlError, require_https
from domain.operations.execution_graph_release import BakeRoster
from domain.presentation.page_schemas import TemporalPresetId

JsonObject = dict[str, Any]


def _empty_records() -> list[JsonObject]:
    """Return a typed empty record list for dataclass defaults."""
    return []


_ROOT: Final = Path(__file__).resolve().parents[1]
_ENV_FILE: Final = Path(__file__).parent / ".env.local"
_REPORT_DIR: Final = Path(__file__).parent / "evidence" / "reports"
_ENVIRONMENT: Final = "dev"
_REGION: Final = "us-east-1"
_ACCOUNT_DIGITS: Final = 12
_MEDIA_TYPE: Final = "application/vnd.adlytics.presentation-v2+json"
_PRESETS: Final = ("1d", "7d", "14d")
_FOCUS_SAMPLE_PRESET: Final = "7d"
_PORTFOLIO_FOCUS_METRICS: Final = (
    "spend",
    "purchases",
    "impressions",
    "portfolio_roas_attributed_14d",
)
_CAMPAIGN_FOCUS_METRICS: Final = (
    "purchases",
    "attributed_roas",
    "spend",
    "impressions",
    "clicks",
    "ctr",
    "cpc",
    "cpm",
)
_SUMMARY_SLOTS: Final = ("purchases", "attributed_roas", "spend")
_EXPECTED_PORTFOLIO_TOTALS: Final = 4
_RETAIL_FOCUS_SAMPLE_LIMIT: Final = 6
_HTTP_OK: Final = 200
_HTTP_FORBIDDEN: Final = 403
_TIMEOUT_SECONDS: Final = 60
_CALL_DEADLINE_SECONDS: Final = 90
"""Wall-clock bound on one request; a detail page took at most 8.1 s on 2026-10-06."""
_RUN_DEADLINE_SECONDS: Final = {"full": 3600, "sample": 900, "zero_delivery": 900}
"""Per-run bounds; the full walk took 2,463 s on 2026-10-06 (189 details at about 7.5 s)."""
# Every request goes through the HTTPS-only boundary, and no redirect is followed: the operator's
# cookies must never reach another origin or a plaintext hop (H-VALIDATION-HTTPS-001).
_HTTP: Final = HttpsSession()
_MAX_SEGMENTS: Final = 200


class ProbeError(Exception):
    """A bounded deployed-acceptance failure safe to print."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise ProbeError("invalid explicit target arguments")


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    """Every explicit operator target needed by the read-only acceptance."""

    profile: str
    environment: str
    region: str
    expected_account: str
    label: str
    zero_delivery_pages: bool = False
    sample: bool = False


@dataclass(slots=True)
class ApiSession:
    """Live cookie custody for the walk; values never leave this process."""

    base_url: str
    refresh_token: str
    session_cookie: str = ""
    csrf: str = ""


@dataclass(slots=True)
class WalkState:
    """Accumulated evidence records, hard failures, and bounded counters."""

    records: list[JsonObject] = field(default_factory=_empty_records)
    failures: list[JsonObject] = field(default_factory=_empty_records)
    parents: int = 0
    details: int = 0
    focuses: int = 0
    window_unavailable: int = 0
    slot_states: dict[str, int] = field(
        default_factory=lambda: {"filled": 0, "silent": 0, "unavailable": 0}
    )
    absence_classes: dict[str, int] = field(default_factory=dict[str, int])

    def fail(self, check: str, where: JsonObject, detail: str) -> None:
        """Record one bounded stranding failure at its page/preset coordinates."""
        self.failures.append({**where, "check": check, "detail": detail})


def _object(value: object) -> JsonObject | None:
    """Narrow one decoded JSON value to an object without unknown-typed inference."""
    if isinstance(value, dict):
        return cast("JsonObject", value)
    return None


def _items(value: object) -> list[Any]:
    """Narrow one decoded JSON value to a list without unknown-typed inference."""
    if isinstance(value, list):
        return cast("list[Any]", value)
    return []


def _non_blank(value: str) -> str:
    selected = value.strip()
    if not selected:
        raise argparse.ArgumentTypeError("value must not be blank")
    return selected


def _profile(value: str) -> str:
    selected = _non_blank(value)
    if selected.casefold() == "default":
        raise argparse.ArgumentTypeError("an explicit nondefault profile is required")
    return selected


def _account(value: str) -> str:
    selected = _non_blank(value)
    if len(selected) != _ACCOUNT_DIGITS or not selected.isdigit():
        raise argparse.ArgumentTypeError("expected account must be 12 digits")
    return selected


def _label(value: str) -> str:
    selected = _non_blank(value)
    if not selected.replace("-", "").isalnum():
        raise argparse.ArgumentTypeError("label must be alphanumeric with dashes")
    return selected


def _arguments() -> ProbeRequest:
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=_profile)
    parser.add_argument("--environment", required=True, type=_non_blank)
    parser.add_argument("--region", required=True, type=_non_blank)
    parser.add_argument("--expected-account", required=True, type=_account)
    parser.add_argument("--label", required=True, type=_label)
    parser.add_argument(
        "--zero-delivery-pages",
        action="store_true",
        help="load only the pause and launch campaigns' detail pages, their ROAS, CTR and "
        "spend focus, and the portfolio, on every preset; fail on any 5xx (design §2.5)",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="intermediate check: per preset only the first, middle and last campaign detail plus "
        "every known-issue campaign (the pause and launch rows); the full walk stays for close-out",
    )
    args = parser.parse_args()
    return ProbeRequest(
        profile=args.profile,
        environment=args.environment,
        region=args.region,
        expected_account=args.expected_account,
        label=args.label,
        zero_delivery_pages=bool(args.zero_delivery_pages),
        sample=bool(args.sample),
    )


def _validate_target(request: ProbeRequest) -> None:
    if request.environment != _ENVIRONMENT:
        raise ProbeError("probe accepts only the dev environment")
    if request.region != _REGION:
        raise ProbeError("probe accepts only the governed dev region")


def _load_env() -> None:
    if not _ENV_FILE.is_file():
        return
    for line in _ENV_FILE.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"'))


def _session_from_env() -> ApiSession:
    base_url = os.environ.get("ADL_BASE_URL", "").rstrip("/")
    refresh = os.environ.get("ADL_REFRESH", "")
    csrf = os.environ.get("ADL_CSRF", "")
    if not base_url or not refresh or not csrf:
        raise ProbeError("ADL_BASE_URL, ADL_REFRESH, and ADL_CSRF are required")
    try:
        # The walk carries the refresh token in every request; plaintext is never acceptable.
        base_url = require_https(base_url)
    except InsecureUrlError:
        raise ProbeError("ADL_BASE_URL must be an https URL") from None
    return ApiSession(base_url=base_url, refresh_token=refresh, csrf=csrf)


def _refresh(session: ApiSession) -> None:
    """Exchange the refresh cookie for a session cookie and the rotated CSRF token."""
    with within(_CALL_DEADLINE_SECONDS, "session refresh"):
        _refresh_once(session)


def _refresh_once(session: ApiSession) -> None:
    with _HTTP.post(
        require_https(f"{session.base_url}/api/auth/refresh"),
        data=b"",
        headers={
            "Cookie": f"adl_refresh={session.refresh_token}; adl_csrf={session.csrf}",
            "x-adl-csrf": session.csrf,
            "Sec-Fetch-Site": "same-origin",
        },
        timeout=_TIMEOUT_SECONDS,
        allow_redirects=False,
    ) as response:
        # Any status but 200 (a 3xx included) is refused before a Set-Cookie header is read.
        if response.status_code != _HTTP_OK:
            raise ProbeError(f"session refresh was refused with status {response.status_code}")
        issued = response.raw.headers.getlist("Set-Cookie")  # requests folds repeated headers
    cookies: dict[str, str] = {}
    for header in issued:
        name, _, cookie_value = header.split(";", 1)[0].partition("=")
        cookies[name] = cookie_value
    if "adl_session" not in cookies:
        raise ProbeError("session refresh did not issue a session cookie")
    session.session_cookie = cookies["adl_session"]
    session.csrf = cookies.get("adl_csrf", session.csrf)


def _post_once(session: ApiSession, path: str, body: JsonObject) -> tuple[int, JsonObject]:
    with within(_CALL_DEADLINE_SECONDS, f"POST {path}"):
        return _post_unbounded(session, path, body)


def _post_unbounded(session: ApiSession, path: str, body: JsonObject) -> tuple[int, JsonObject]:
    payload = json.dumps(body).encode()
    with _HTTP.post(
        require_https(f"{session.base_url}{path}"),
        data=payload,
        headers={
            "Cookie": f"adl_session={session.session_cookie}; adl_csrf={session.csrf}",
            "x-adl-csrf": session.csrf,
            "Sec-Fetch-Site": "same-origin",
            "Content-Type": "application/json",
            "Accept": _MEDIA_TYPE,
        },
        timeout=_TIMEOUT_SECONDS,
        allow_redirects=False,
    ) as response:
        return response.status_code, _json_or_empty(response)


def _json_or_empty(response: requests.Response) -> JsonObject:
    """Read a JSON object body; a non-JSON body (for example a 3xx page) reads as empty."""
    try:
        decoded = _object(response.json()) if response.content else None
    except ValueError:
        return {}
    return decoded if decoded is not None else {}


def _post(session: ApiSession, path: str, body: JsonObject) -> tuple[int, JsonObject]:
    """POST with one refresh-and-retry on an expired-session 403, per the session rules."""
    status, decoded = _post_once(session, path, body)
    if status == _HTTP_FORBIDDEN:
        _refresh(session)
        status, decoded = _post_once(session, path, body)
    return status, decoded


def _slots(value: object) -> list[JsonObject]:
    """Extract insight-slot records defensively; the report carries them verbatim."""
    slots: list[JsonObject] = []
    for entry in _items(value):
        item = _object(entry)
        if item is not None and {"id", "state", "prompt_version"} <= set(item):
            projected: JsonObject = {
                "id": item.get("id"),
                "state": item.get("state"),
                "prompt_version": item.get("prompt_version"),
                "artifact_revision": item.get("artifact_revision"),
                "text": item.get("text"),
                "evidence_line": item.get("evidence_line"),
            }
            # The absence pair is present exactly when an absence is declared
            # (ADR-068); the projection must carry it or the declared-absence
            # checks and counters run blind.
            if "absence_class" in item:
                projected["absence_class"] = item.get("absence_class")
                projected["absence_reason"] = item.get("absence_reason")
            slots.append(projected)
    return slots


def _check_slots(
    state: WalkState,
    where: JsonObject,
    slots: list[JsonObject],
    *,
    populated: bool,
) -> None:
    for slot in slots:
        slot_state = str(slot.get("state"))
        if slot_state not in state.slot_states:
            # An undeclared state is stranding by definition; never let it pass invisibly.
            state.slot_states["unknown"] = state.slot_states.get("unknown", 0) + 1
            state.fail(
                "unknown_insight_slot_state",
                where,
                f"slot {slot.get('id')} carries undeclared state {slot_state}",
            )
            continue
        state.slot_states[slot_state] += 1
        absence_class = slot.get("absence_class")
        if isinstance(absence_class, str):
            state.absence_classes[absence_class] = state.absence_classes.get(absence_class, 0) + 1
        if populated and slot_state == "unavailable":
            # A declared generation-pending absence is a healing state, not stranding:
            # the slot names its pending retry. Unavailable WITHOUT a declared class
            # is an unexplained blank and stays a hard failure.
            if absence_class == "generation-pending":
                continue
            state.fail(
                "insight_slot_unavailable_on_populated_page",
                where,
                f"slot {slot.get('id')} is unavailable without a declared absence",
            )


def _comparison_values(entries: object) -> list[JsonObject]:
    values: list[JsonObject] = []
    for entry in _items(entries):
        item = _object(entry)
        if item is None:
            continue
        current = _object(item.get("current"))
        prior = _object(item.get("prior"))
        values.append(
            {
                "metric": item.get("metric") or item.get("slot"),
                "current": current.get("value") if current is not None else None,
                "prior": prior.get("value") if prior is not None else None,
                "absolute_change": item.get("absolute_change"),
                "percentage_change": item.get("percentage_change"),
            }
        )
    return values


def _envelope(
    *, where: JsonObject, state: WalkState, status: int, decoded: JsonObject
) -> JsonObject | None:
    """Return the page envelope, or None after recording a designed or failed one."""
    if status != _HTTP_OK:
        code = decoded.get("code")
        state.records.append({**where, "http_status": status, "error_code": code})
        state.fail("non_200_response", where, f"status {status} code {code}")
        return None
    if decoded.get("kind") == "window_unavailable":
        state.window_unavailable += 1
        state.records.append(
            {
                **where,
                "http_status": status,
                "kind": "window_unavailable",
                "reason_code": decoded.get("reason_code"),
                "recovery": decoded.get("recovery"),
            }
        )
        return None
    return decoded


def _temporal_summary(envelope: JsonObject) -> JsonObject:
    control = _object(envelope.get("temporal"))
    if control is None:
        return {}
    presets: list[JsonObject] = []
    for entry in _items(control.get("presets")):
        item = _object(entry)
        if item is not None:
            presets.append(
                {
                    "id": item.get("id"),
                    "availability": item.get("availability"),
                    "reason_code": item.get("reason_code"),
                }
            )
    return {"resolved": control.get("resolved"), "presets": presets}


def _page_artifact(
    *, where: JsonObject, state: WalkState, envelope: JsonObject
) -> tuple[JsonObject, JsonObject] | None:
    page = _object(envelope.get("page"))
    artifact = _object(page.get("artifact")) if page is not None else None
    if page is None or artifact is None:
        state.fail("malformed_envelope", where, "page artifact is absent")
        return None
    return page, artifact


def _walk_portfolio_parent(
    *,
    session: ApiSession,
    state: WalkState,
    session_id: str,
    preset: str,
    segment: int,
) -> JsonObject | None:
    """Fetch one portfolio segment, record hydration, and return the raw artifact."""
    where: JsonObject = {"page": "campaigns", "preset": preset, "segment": segment}
    body: JsonObject = {
        "session_id": session_id,
        "temporal_selection": {"kind": "preset", "preset_id": preset},
    }
    if segment > 0:
        body["segment"] = segment
    status, decoded = _post(session, "/api/presentation/campaigns", body)
    envelope = _envelope(where=where, state=state, status=status, decoded=decoded)
    if envelope is None:
        return None
    located = _page_artifact(where=where, state=state, envelope=envelope)
    if located is None:
        return None
    page, artifact = located
    state.parents += 1
    outcome = artifact.get("outcome")
    rows = _items(artifact.get("rows"))
    roster = _object(artifact.get("roster"))
    roster_count = len(_items(roster.get("entries"))) if roster is not None else 0
    trend = _object(artifact.get("trend"))
    trend_points = len(_items(trend.get("points"))) if trend is not None else 0
    slots = _slots(artifact.get("insights"))
    populated = outcome == "complete"
    _check_slots(state, where, slots, populated=populated)
    totals = _comparison_values(artifact.get("totals"))
    state.records.append(
        {
            **where,
            "http_status": status,
            "kind": "page",
            "artifact_revision": page.get("artifact_revision"),
            "outcome": outcome,
            "reason_codes": artifact.get("reason_codes"),
            "total_count": artifact.get("total_count"),
            "rows_count": len(rows),
            "roster_count": roster_count,
            "pagination": artifact.get("pagination"),
            "trend_points": trend_points,
            "totals": totals,
            "insights": slots,
            "temporal": _temporal_summary(envelope),
        }
    )
    if populated:
        if len(totals) != _EXPECTED_PORTFOLIO_TOTALS or any(
            item["current"] is None for item in totals
        ):
            state.fail("missing_declared_metrics", where, "portfolio totals are incomplete")
        if trend_points == 0:
            state.fail("missing_declared_metrics", where, "portfolio trend is absent")
        if roster_count == 0 or not rows:
            state.fail("empty_roster_with_data", where, "complete outcome served no roster rows")
    return artifact


def _detail_record(
    *,
    session: ApiSession,
    state: WalkState,
    session_id: str,
    preset: str,
    position: int,
    row: JsonObject,
) -> JsonObject | None:
    where: JsonObject = {"page": "campaign-detail", "preset": preset, "position": position}
    capability = _object(row.get("capability"))
    reference = capability.get("selection_reference") if capability is not None else None
    if not isinstance(reference, str):
        state.fail("malformed_envelope", where, "row carries no selection reference")
        return None
    status, decoded = _post(
        session,
        "/api/presentation/campaign-detail",
        {"session_id": session_id, "selection_reference": reference},
    )
    envelope = _envelope(where=where, state=state, status=status, decoded=decoded)
    if envelope is None:
        return None
    located = _page_artifact(where=where, state=state, envelope=envelope)
    if located is None:
        return None
    _, artifact = located
    state.details += 1
    outcome = artifact.get("outcome")
    populated = outcome == "selected"
    summary = _object(artifact.get("summary"))
    summary_values: JsonObject = {}
    for slot_name in _SUMMARY_SLOTS:
        value = _object(summary.get(slot_name)) if summary is not None else None
        summary_values[slot_name] = value.get("value") if value is not None else None
    slots = _slots(artifact.get("insights"))
    _check_slots(state, where, slots, populated=populated)
    delivery = _object(artifact.get("delivery"))
    delivery_summary: JsonObject = {}
    if delivery is not None:
        evidence: list[JsonObject] = []
        for entry in _items(delivery.get("evidence")):
            item = _object(entry)
            if item is not None:
                evidence.append(
                    {
                        "slot": item.get("slot"),
                        "role": item.get("role"),
                        "condition": item.get("condition"),
                    }
                )
        delivery_summary = {"outcome": delivery.get("outcome"), "evidence": evidence}
    series = _object(artifact.get("purchase_series"))
    state.records.append(
        {
            **where,
            "http_status": status,
            "kind": "page",
            "signature": row.get("signature"),
            "outcome": outcome,
            "insufficiency_reasons": artifact.get("insufficiency_reasons"),
            "summary": summary_values,
            "comparison_present": artifact.get("comparison") is not None,
            "series_points": len(_items(series.get("points"))) if series is not None else 0,
            "insights": slots,
            "delivery": delivery_summary,
        }
    )
    if populated and any(value is None for value in summary_values.values()):
        state.fail("missing_declared_metrics", where, "selected detail summary has absent metrics")
    return artifact


_SLOT_FIELDS: Final = frozenset({"id", "state", "prompt_version", "artifact_revision"})
_HTTP_SERVER_ERROR: Final = 500


def focus_decode_findings(decoded: JsonObject, metric: str) -> list[str]:
    """Name what a served metric-focus body lacks: its metric route and a decodable card slot.

    Run after every deploy: the focus route must answer for the metric asked, and its insight
    slot must carry the declared slot fields, so the card the dialog renders can decode.
    """
    findings: list[str] = []
    served = _object(decoded.get("metric"))
    if served is None or served.get("id") != metric:
        findings.append(f"focus answered for {None if served is None else served.get('id')}")
    slot = _object(decoded.get("insight"))
    if slot is None or not set(slot) >= _SLOT_FIELDS:
        missing = sorted(_SLOT_FIELDS - set(slot or {}))
        findings.append(f"focus insight slot lacks {missing}")
    return findings


def zero_delivery_campaign_names() -> tuple[str, ...]:
    """Return the pause and launch campaigns' served names, read from the governed catalog."""
    from adapters.jobs.fixture_generation.catalog import (
        load_synthetic_entity_catalog,
    )
    from adapters.jobs.fixture_generation.shape import Pause

    catalog = load_synthetic_entity_catalog(_ROOT)
    pause = next(event for event in catalog.shape.events if isinstance(event, Pause))
    keys = (pause.campaign, catalog.shape.launch.campaign)
    return tuple(
        campaign.names[-1].value
        for key in keys
        for campaign in catalog.campaigns
        if campaign.key == key
    )


def _focus_record(
    *,
    session: ApiSession,
    state: WalkState,
    where: JsonObject,
    path: str,
    body: JsonObject,
    detail_absent: bool = False,
) -> None:
    """Open one metric focus. `detail_absent` says the served detail carries no current or no
    prior value for the metric; only then is its 409 not-served refusal the designed answer."""
    status, decoded = _post(session, path, body)
    if status >= _HTTP_SERVER_ERROR:
        state.fail("server_error", where, f"status {status}")
    if status != _HTTP_OK:
        code = decoded.get("code")
        designed = detail_absent and status == _HTTP_CONFLICT and code == _FOCUS_NOT_SERVED_CODE
        state.records.append(
            {**where, "http_status": status, "error_code": code, "designed_refusal": designed}
        )
        if not designed:
            state.fail("non_200_response", where, f"status {status} code {code}")
        return
    state.focuses += 1
    for finding in focus_decode_findings(decoded, str(body.get("metric"))):
        state.fail("focus_decode", where, finding)
    slot_value = _object(decoded.get("insight"))
    slots = _slots([slot_value] if slot_value is not None else [])
    _check_slots(state, where, slots, populated=True)
    metric = _object(decoded.get("metric"))
    drivers = _object(decoded.get("drivers"))
    state.records.append(
        {
            **where,
            "http_status": status,
            "kind": "metric_focus",
            "metric_id": metric.get("id") if metric is not None else None,
            "change": decoded.get("change"),
            "drivers_kind": (
                "absent" if drivers is not None and "absent_reason" in drivers else "entries"
            ),
            "facts_count": len(_items(decoded.get("facts"))),
            "insights": slots,
        }
    )


def sample_positions(names: list[object], known: frozenset[str]) -> frozenset[int]:
    """The rows a sampled walk loads: the first, middle and last, plus every known-issue row."""
    if not names:
        return frozenset()
    picked = {0, len(names) // 2, len(names) - 1}
    picked.update(index for index, name in enumerate(names) if name in known)
    return frozenset(picked)


def _row_name(entry: object) -> object:
    row = _object(entry)
    signature = _object(row.get("signature")) if row is not None else None
    return signature.get("effective_name") if signature is not None else None


def _walk_campaigns(
    session: ApiSession, state: WalkState, session_id: str, *, sample: bool = False
) -> None:
    focus_reference: str | None = None
    for preset in _PRESETS:
        artifact = _walk_portfolio_parent(
            session=session, state=state, session_id=session_id, preset=preset, segment=0
        )
        if artifact is None:
            continue
        rows = _items(artifact.get("rows"))
        pagination = _object(artifact.get("pagination"))
        raw_segments = pagination.get("segment_count", 1) if pagination is not None else 1
        segment_count = raw_segments if isinstance(raw_segments, int) else 1
        for segment in range(1, min(segment_count, _MAX_SEGMENTS)):
            more = _walk_portfolio_parent(
                session=session,
                state=state,
                session_id=session_id,
                preset=preset,
                segment=segment,
            )
            if more is not None:
                rows.extend(_items(more.get("rows")))
        chosen = (
            sample_positions(
                [_row_name(entry) for entry in rows], frozenset(zero_delivery_campaign_names())
            )
            if sample
            else frozenset(range(len(rows)))
        )
        for position, entry in enumerate(rows):
            row = _object(entry)
            if row is None or position not in chosen:
                continue
            _detail_record(
                session=session,
                state=state,
                session_id=session_id,
                preset=preset,
                position=position,
                row=row,
            )
            if preset == _FOCUS_SAMPLE_PRESET and focus_reference is None:
                capability = _object(row.get("capability"))
                reference = (
                    capability.get("selection_reference") if capability is not None else None
                )
                focus_reference = reference if isinstance(reference, str) else None
    for metric in _PORTFOLIO_FOCUS_METRICS:
        _focus_record(
            session=session,
            state=state,
            where={
                "page": "portfolio-metric-focus",
                "preset": _FOCUS_SAMPLE_PRESET,
                "metric": metric,
            },
            path="/api/presentation/portfolio-metric-focus",
            body={
                "session_id": session_id,
                "metric": metric,
                "temporal_selection": {"kind": "preset", "preset_id": _FOCUS_SAMPLE_PRESET},
            },
        )
    if focus_reference is not None:
        for metric in _CAMPAIGN_FOCUS_METRICS:
            _focus_record(
                session=session,
                state=state,
                where={
                    "page": "campaign-metric-focus",
                    "preset": _FOCUS_SAMPLE_PRESET,
                    "metric": metric,
                },
                path="/api/presentation/campaign-metric-focus",
                body={
                    "session_id": session_id,
                    "selection_reference": focus_reference,
                    "metric": metric,
                },
            )


def _walk_retail(session: ApiSession, state: WalkState, session_id: str) -> None:
    focus_metrics: list[str] = []
    for preset in _PRESETS:
        where: JsonObject = {"page": "retail-health", "preset": preset}
        status, decoded = _post(
            session,
            "/api/presentation/retail-health",
            {
                "session_id": session_id,
                "temporal_selection": {"kind": "preset", "preset_id": preset},
            },
        )
        envelope = _envelope(where=where, state=state, status=status, decoded=decoded)
        if envelope is None:
            continue
        located = _page_artifact(where=where, state=state, envelope=envelope)
        if located is None:
            continue
        _, artifact = located
        state.parents += 1
        outcome = artifact.get("outcome")
        slots = _slots(artifact.get("insights"))
        _check_slots(state, where, slots, populated=outcome == "complete")
        totals = _comparison_values(artifact.get("totals"))
        absent_metrics: set[str] = set()
        for entry in _items(artifact.get("absences")):
            item = _object(entry)
            if item is not None:
                absent_metrics.add(str(item.get("metric")))
        state.records.append(
            {
                **where,
                "http_status": status,
                "kind": "page",
                "outcome": outcome,
                "totals": totals,
                "absent_metrics": sorted(absent_metrics),
                "rows_count": len(_items(artifact.get("rows"))),
                "insights": slots,
                "temporal": _temporal_summary(envelope),
            }
        )
        if preset == _FOCUS_SAMPLE_PRESET:
            focus_metrics = [
                str(item["metric"])
                for item in totals
                if item["metric"] is not None and str(item["metric"]) not in absent_metrics
            ][:_RETAIL_FOCUS_SAMPLE_LIMIT]
    for metric in focus_metrics:
        _focus_record(
            session=session,
            state=state,
            where={"page": "retail-metric-focus", "preset": _FOCUS_SAMPLE_PRESET, "metric": metric},
            path="/api/presentation/retail-metric-focus",
            body={
                "session_id": session_id,
                "metric": metric,
                "temporal_selection": {"kind": "preset", "preset_id": _FOCUS_SAMPLE_PRESET},
            },
        )


_WALKED_IN_DEPTH: Final = frozenset(
    (page, preset) for page in ("campaigns", "retail-health") for preset in _PRESETS
)
"""The (page, variant) roster rows the walks above attest, with details and focuses.

Keyed by row, not page: those walks iterate `_PRESETS`, so any other baked variant of their
pages (campaigns#30d) falls to the roster walk instead of to neither.
"""


def roster_page_requests() -> tuple[tuple[str, str | None], ...]:
    """Return one (page, preset) request per baked roster row not walked in depth.

    D2 seam sweep finding 11: the list is the generated bake roster (`BAKE_TASKS`), so a row
    added to the contract is attested without editing this probe. A variant that is a declared
    temporal preset is requested by preset; a page's one non-preset variant (Retention's vintage)
    is its omitted selection. A page with two non-preset variants would collapse to one request,
    and a roster leaving no row for this walk would attest nothing: both are refused.
    """
    presets = {item.value for item in TemporalPresetId}
    requests: list[tuple[str, str | None]] = []
    omitted: dict[str, str] = {}
    for row in BakeRoster.from_contract().rows:
        if (row.page, row.variant) in _WALKED_IN_DEPTH:
            continue
        if row.variant in presets:
            requests.append((row.page, row.variant))
            continue
        if row.page in omitted:
            raise ProbeError(
                f"roster page {row.page} bakes more than one non-preset variant "
                f"({omitted[row.page]}, {row.variant}); the walk can request only one"
            )
        omitted[row.page] = row.variant
        requests.append((row.page, None))
    if not requests:
        raise ProbeError("the bake roster leaves no row for the roster walk")
    return tuple(requests)


def _walk_roster_pages(session: ApiSession, state: WalkState, session_id: str) -> None:
    """Every other roster row serves 200 with a built page or its designed window_unavailable.

    This walk covers build state only: the HTTP status, the envelope, and a served page's
    `generation.built_at`, with its outcome recorded. It does not apply `_check_slots`, so an
    `unavailable` insight slot on a populated page (campaigns#30d included) is not counted as
    stranding here: these pages don't all carry slots at the artifact's top level (Reach's and
    Retention's are per market), and a top-level-only check would pass them vacuously.
    """
    for page_id, preset in roster_page_requests():
        where: JsonObject = {"page": page_id, "preset": preset}
        body: JsonObject = {"session_id": session_id}
        if preset is not None:
            body["temporal_selection"] = {"kind": "preset", "preset_id": preset}
        status, decoded = _post(session, f"/api/presentation/{page_id}", body)
        envelope = _envelope(where=where, state=state, status=status, decoded=decoded)
        if envelope is None:
            continue
        located = _page_artifact(where=where, state=state, envelope=envelope)
        if located is None:
            continue
        page, artifact = located
        state.parents += 1
        generation = _object(page.get("generation"))
        if generation is None or not generation.get("built_at"):
            state.fail("malformed_envelope", where, "page generation.built_at is absent")
        state.records.append(
            {
                **where,
                "http_status": status,
                "kind": "page",
                "outcome": artifact.get("outcome"),
                "artifact_revision": page.get("artifact_revision"),
                "temporal": _temporal_summary(envelope),
            }
        )


_ZERO_DELIVERY_FOCUS_METRICS: Final = ("attributed_roas", "ctr", "spend")
_FOCUS_NOT_SERVED_CODE: Final = "ADLY-PRESENTATION-0002"
_HTTP_CONFLICT: Final = 409
_DETAIL_SUMMARY_METRICS: Final = frozenset({"attributed_roas", "spend"})


def _served_value(entry: object) -> object:
    """The value of one served detail metric entry, or None when it is typed absent."""
    item = _object(entry)
    if item is None:
        return None
    value = item.get("value")
    nested = _object(value)
    return nested.get("value") if nested is not None else value


def detail_metric_absent(artifact: JsonObject, metric: str) -> bool:
    """Whether the served detail carries no current or no prior value for `metric`.

    That is exactly when the campaign focus refuses with 409 ADLY-PRESENTATION-0002 ("the focused
    metric is not served for this selection", `campaign_detail_focus_ingredients`): ROAS and spend
    are read from the summary and the comparison, CTR from the delivery summary and comparison.
    """
    if metric in _DETAIL_SUMMARY_METRICS:
        summary = _object(artifact.get("summary")) or {}
        comparison = _object(artifact.get("comparison"))
        prior = None
        if comparison is not None:
            for entry in _items(comparison.get("metrics")):
                item = _object(entry) or {}
                if item.get("slot") == metric or item.get("metric") == metric:
                    prior = _served_value(item.get("prior"))
        return _served_value(summary.get(metric)) is None or prior is None
    delivery = _object(artifact.get("delivery")) or {}
    rosters = (
        _items(delivery.get("summary")),
        _items((_object(delivery.get("comparison")) or {}).get("slots")),
    )
    values = [
        next(
            (
                _served_value(item)
                for item in (_object(entry) or {} for entry in roster)
                if item.get("slot") == metric
            ),
            None,
        )
        for roster in rosters
    ]
    return any(value is None for value in values)


def _walk_zero_delivery(session: ApiSession, state: WalkState, session_id: str) -> None:
    """Load the pause and launch campaigns on every preset; any 5xx is a failure."""
    names = set(zero_delivery_campaign_names())
    for preset in _PRESETS:
        artifact = _walk_portfolio_parent(
            session=session, state=state, session_id=session_id, preset=preset, segment=0
        )
        if artifact is None:
            continue
        rows = _items(artifact.get("rows"))
        pagination = _object(artifact.get("pagination"))
        raw_segments = pagination.get("segment_count", 1) if pagination is not None else 1
        for segment in range(
            1, min(raw_segments if isinstance(raw_segments, int) else 1, _MAX_SEGMENTS)
        ):
            more = _walk_portfolio_parent(
                session=session, state=state, session_id=session_id, preset=preset, segment=segment
            )
            if more is not None:
                rows.extend(_items(more.get("rows")))
        found = 0
        for position, entry in enumerate(rows):
            row = _object(entry)
            signature = _object(row.get("signature")) if row is not None else None
            if row is None or signature is None or signature.get("effective_name") not in names:
                continue
            found += 1
            detail = _detail_record(
                session=session,
                state=state,
                session_id=session_id,
                preset=preset,
                position=position,
                row=row,
            )
            capability = _object(row.get("capability"))
            reference = capability.get("selection_reference") if capability is not None else None
            for metric in _ZERO_DELIVERY_FOCUS_METRICS:
                _focus_record(
                    session=session,
                    state=state,
                    where={"page": "campaign-metric-focus", "preset": preset, "metric": metric},
                    path="/api/presentation/campaign-metric-focus",
                    body={
                        "session_id": session_id,
                        "selection_reference": reference,
                        "metric": metric,
                    },
                    detail_absent=detail is not None and detail_metric_absent(detail, metric),
                )
        if found != len(names):
            state.fail(
                "zero_delivery_campaign_absent",
                {"page": "campaigns", "preset": preset},
                f"found {found} of {len(names)} pause and launch rows",
            )


def _write_report(request: ProbeRequest, state: WalkState) -> Path:
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = _REPORT_DIR / f"{stamp}-hydration-{request.label}.json"
    report = {
        "probe": "presentation-hydration",
        "generated_at": stamp,
        "label": request.label,
        "mode": "zero_delivery"
        if request.zero_delivery_pages
        else "sample"
        if request.sample
        else "full",
        "environment": request.environment,
        "presets": list(_PRESETS),
        "counts": {
            "parents": state.parents,
            "details": state.details,
            "focuses": state.focuses,
            "window_unavailable": state.window_unavailable,
            "slot_states": state.slot_states,
            "absence_classes": state.absence_classes,
            "failures": len(state.failures),
        },
        "failures": state.failures,
        "records": state.records,
    }
    path.write_text(json.dumps(report, indent=1, default=str) + "\n")
    return path


# Shared session plumbing, exported for sibling probes (probe_insight_regeneration):
# one home for the auth/refresh/post conventions instead of a second copy.
load_env = _load_env
session_from_env = _session_from_env
refresh_session = _refresh
post = _post


def _run(request: ProbeRequest) -> tuple[WalkState, Path]:
    mode = (
        "zero_delivery" if request.zero_delivery_pages else "sample" if request.sample else "full"
    )
    with within(_RUN_DEADLINE_SECONDS[mode], f"hydration {mode} run"):
        return _run_bounded(request)


def _run_bounded(request: ProbeRequest) -> tuple[WalkState, Path]:
    _validate_target(request)
    _load_env()
    identity = bounded_client(
        boto3.Session(profile_name=request.profile, region_name=request.region), "sts"
    ).get_caller_identity()
    if str(identity.get("Account", "")) != request.expected_account:
        raise ProbeError("explicit profile account differs from expected account")
    session = _session_from_env()
    _refresh(session)
    state = WalkState()
    session_id = str(uuid4())
    if request.zero_delivery_pages:
        _walk_zero_delivery(session, state, session_id)
        return state, _write_report(request, state)
    _walk_campaigns(session, state, session_id, sample=request.sample)
    _walk_retail(session, state, session_id)
    _walk_roster_pages(session, state, session_id)
    return state, _write_report(request, state)


def main() -> int:
    """Run the bounded probe and return nonzero without exposing live evidence."""
    try:
        state, report_path = _run(_arguments())
    except (ProbeError, ProbeDeadlineError) as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", "unknown"))
        print(f"FAILED: AWS request returned {code}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"FAILED: unexpected {type(error).__name__}", file=sys.stderr)
        return 1
    summary = (
        f"parents={state.parents} details={state.details} focuses={state.focuses} "
        f"window_unavailable={state.window_unavailable} "
        f"slots_filled={state.slot_states['filled']} "
        f"slots_silent={state.slot_states['silent']} "
        f"slots_unavailable={state.slot_states['unavailable']} "
        f"slots_unknown={state.slot_states.get('unknown', 0)} "
        f"absence_classes={state.absence_classes} "
        f"failures={len(state.failures)} "
        f"report={report_path.relative_to(_ROOT)}"
    )
    if state.failures:
        print(f"FAILED: presentation hydration found stranding {summary}", file=sys.stderr)
        return 1
    print(f"PASS: presentation hydration attested {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
