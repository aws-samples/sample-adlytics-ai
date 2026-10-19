# SPDX-License-Identifier: MIT-0
"""Attest the deployed ADR-076 deep-dive route end to end on dev.

One disputed fact family: a click on a filled metric card or insight card serves
an Opus-generated deep analysis through POST /api/presentation/insight-deep-dive,
and a repeat click for the same identity is served from the tier-keyed cache.

The walk mirrors the browser exactly: serve the campaigns and retail-health
parents under the 7d preset, pick filled deep-capable slots, call the deep route
(following the declared 5s/6 pending schedule when generation continues
asynchronously), then repeat the identical call and record both round-trip
times. Each response is validated against the insight-deep-dive.v1 shape and
its artifact revision is attested against the revision the parent page served.
The capability-bound detail family is exercised through the first roster row's
selection reference.

Evidence is one JSON report under ``validation/evidence/reports/`` (ignored);
stdout carries only bounded counts and timings. Requires ``ADL_BASE_URL``/
``ADL_REFRESH``/``ADL_CSRF`` in ``validation/.env.local``.

Usage:
    uv run python validation/probe_insight_deep_dive.py \
        --profile <PROFILE> --environment <ENV> --region <REGION> \
        --expected-account <ACCOUNT_ID> --label sliceG-acceptance
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import boto3
import probe_presentation_hydration as hydration
from botocore.exceptions import ClientError
from probe_presentation_hydration import ApiSession, ProbeError

JsonObject = dict[str, Any]

_ROOT: Final = Path(__file__).resolve().parents[1]
_REPORT_DIR: Final = Path(__file__).parent / "evidence" / "reports"
_ENVIRONMENT: Final = "dev"
_REGION: Final = "us-east-1"
_ACCOUNT_DIGITS: Final = 12
_PRESET: Final = "7d"
_DEEP_PATH: Final = "/api/presentation/insight-deep-dive"
_HTTP_OK: Final = 200
_REFETCH_SECONDS: Final = 5
_MAX_REFETCHES: Final = 6
# The dialog gives up after its declared budget; the durable continuation must
# still land. The deadline absorbs one failed worker receive (95s queue
# visibility) plus a full 60s patient generation on the redelivery.
_CONTINUATION_DEADLINE_SECONDS: Final = 240
_CONTINUATION_POLL_SECONDS: Final = 10
_MAX_READINGS: Final = 3
# Deep-capable page slots per surface (movers is deterministic and declares no deep).
_PORTFOLIO_SLOTS: Final = ("contribution", "divergence", "exposure")
_RETAIL_SLOTS: Final = ("demand-shape", "ordered-vs-shipped", "returns-pressure")
_DETAIL_SLOTS: Final = ("funnel", "efficiency-drift", "trend-shape", "next-check")


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    """The explicit operator-supplied target for one probe run."""

    profile: str
    environment: str
    region: str
    expected_account: str
    label: str


@dataclass
class ProbeState:
    """Bounded counters and sanitized records the report and summary carry."""

    serves: int = 0
    cache_hits_observed: int = 0
    pending_polls: int = 0
    failures: list[JsonObject] = field(default_factory=list)
    records: list[JsonObject] = field(default_factory=list)

    def fail(self, kind: str, where: JsonObject, detail: str) -> None:
        """Record one typed failure without carrying live payload text."""
        self.failures.append({"kind": kind, **where, "detail": detail})


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


def _arguments() -> ProbeRequest:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=_profile)
    parser.add_argument("--environment", required=True, type=_non_blank)
    parser.add_argument("--region", required=True, type=_non_blank)
    parser.add_argument("--expected-account", required=True, type=_account)
    parser.add_argument("--label", required=True, type=_non_blank)
    args = parser.parse_args()
    return ProbeRequest(
        profile=args.profile,
        environment=args.environment,
        region=args.region,
        expected_account=args.expected_account,
        label=args.label,
    )


def _page_artifact(decoded: JsonObject) -> tuple[JsonObject, JsonObject] | None:
    page = decoded.get("page")
    if not isinstance(page, dict):
        return None
    artifact = page.get("artifact")
    if not isinstance(artifact, dict):
        return None
    return page, artifact


def _filled_slots(artifact: JsonObject, allowed: tuple[str, ...]) -> list[JsonObject]:
    slots = artifact.get("insights")
    if not isinstance(slots, list):
        return []
    return [
        slot
        for slot in slots
        if isinstance(slot, dict) and slot.get("state") == "filled" and slot.get("id") in allowed
    ]


def _validated_deep(
    state: ProbeState,
    where: JsonObject,
    payload: JsonObject,
    *,
    expected_revision: str | None,
) -> bool:
    """Attest the insight-deep-dive.v1 shape and its revision provenance."""
    if payload.get("state") != "filled":
        state.fail("not_filled", where, f"terminal state {payload.get('state')!r}")
        return False
    deep = payload.get("deep")
    if not isinstance(deep, dict):
        state.fail("malformed", where, "filled response carries no deep object")
        return False
    readings = deep.get("candidate_readings")
    if (
        not isinstance(deep.get("what_stands_out"), str)
        or not str(deep.get("what_stands_out")).strip()
        or not isinstance(readings, list)
        or not (1 <= len(readings) <= _MAX_READINGS)
        or not isinstance(deep.get("discriminating_evidence"), str)
        or not str(deep.get("discriminating_evidence")).strip()
    ):
        state.fail("malformed", where, "deep analysis shape violates the v1 contract")
        return False
    if expected_revision is not None and payload.get("artifact_revision") != expected_revision:
        state.fail(
            "revision_mismatch",
            where,
            "served deep revision differs from the parent page's slot revision",
        )
        return False
    return True


def _deep_serve(
    *,
    session: ApiSession,
    state: ProbeState,
    where: JsonObject,
    body: JsonObject,
    expected_revision: str | None,
) -> None:
    """First call (miss; follow the declared pending schedule), then a repeat call."""

    def _call() -> tuple[int, JsonObject, float]:
        started = time.monotonic()
        status, decoded = hydration.post(session, _DEEP_PATH, body)
        return status, decoded, (time.monotonic() - started) * 1000.0

    status, decoded, first_ms = _call()
    if status != _HTTP_OK:
        state.fail("http", where, f"deep dive returned {status}")
        return
    polls = 0
    client_bound_exhausted = False
    while decoded.get("state") == "pending" and polls < _MAX_REFETCHES:
        polls += 1
        state.pending_polls += 1
        time.sleep(_REFETCH_SECONDS)
        status, decoded, _ = _call()
        if status != _HTTP_OK:
            state.fail("http", where, f"pending re-fetch returned {status}")
            return
    if decoded.get("state") == "pending":
        # The dialog's declared budget is exhausted (a designed give-up, not a
        # failure); acceptance still requires the durable continuation to land.
        client_bound_exhausted = True
        deadline = time.monotonic() + _CONTINUATION_DEADLINE_SECONDS
        while decoded.get("state") == "pending" and time.monotonic() < deadline:
            time.sleep(_CONTINUATION_POLL_SECONDS)
            status, decoded, _ = _call()
            if status != _HTTP_OK:
                state.fail("http", where, f"continuation re-check returned {status}")
                return
    if not _validated_deep(state, where, decoded, expected_revision=expected_revision):
        return
    status, repeat, repeat_ms = _call()
    if status != _HTTP_OK or not _validated_deep(
        state, where, repeat, expected_revision=expected_revision
    ):
        state.fail("repeat", where, f"repeat serve failed with {status}")
        return
    if repeat.get("deep_prompt_version") != decoded.get("deep_prompt_version"):
        state.fail("repeat", where, "repeat serve changed the prompt provenance")
        return
    state.serves += 1
    # Recorded observation only, never a gate: latency comparison cannot prove a
    # cache read. The server-side criterion is the InsightCacheOutcome metric
    # (Tier=deep, Outcome=hit) read from CloudWatch during acceptance.
    if repeat_ms < first_ms or polls > 0:
        state.cache_hits_observed += 1
    state.records.append(
        {
            **where,
            "first_ms": round(first_ms),
            "pending_polls": polls,
            "client_bound_exhausted": client_bound_exhausted,
            "repeat_ms": round(repeat_ms),
            "prompt_version": decoded.get("deep_prompt_version"),
            "artifact_revision": decoded.get("artifact_revision"),
            "period": decoded.get("period"),
        }
    )


def _walk(session: ApiSession, state: ProbeState, session_id: str) -> None:
    temporal: JsonObject = {"kind": "preset", "preset_id": _PRESET}
    status, decoded = hydration.post(
        session,
        "/api/presentation/campaigns",
        {"session_id": session_id, "temporal_selection": temporal},
    )
    located = _page_artifact(decoded) if status == _HTTP_OK else None
    if located is None:
        state.fail("http", {"page": "campaigns"}, f"portfolio parent returned {status}")
        return
    _, artifact = located
    for slot in _filled_slots(artifact, _PORTFOLIO_SLOTS)[:1]:
        _deep_serve(
            session=session,
            state=state,
            where={"surface": "portfolio", "slot": str(slot.get("id"))},
            body={
                "session_id": session_id,
                "slot_id": slot.get("id"),
                "temporal_selection": temporal,
            },
            expected_revision=str(slot.get("artifact_revision")),
        )
    # The metric-card path: the dialog's deep section attests against the FOCUS
    # payload's interpretation-slot revision (what surface.deepFor carries), never
    # the page card's derivation fingerprint.
    status, focus = hydration.post(
        session,
        "/api/presentation/portfolio-metric-focus",
        {"session_id": session_id, "metric": "spend", "temporal_selection": temporal},
    )
    focus_insight = focus.get("insight") if status == _HTTP_OK else None
    focus_revision = (
        str(focus_insight.get("artifact_revision")) if isinstance(focus_insight, dict) else None
    )
    if focus_revision is None:
        state.fail("http", {"page": "portfolio-metric-focus"}, f"focus returned {status}")
    _deep_serve(
        session=session,
        state=state,
        where={"surface": "portfolio", "slot": "metric-focus", "metric": "spend"},
        body={
            "session_id": session_id,
            "slot_id": "metric-focus",
            "metric": "spend",
            "temporal_selection": temporal,
        },
        expected_revision=focus_revision,
    )
    rows = artifact.get("rows")
    reference = None
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        capability = rows[0].get("capability")
        if isinstance(capability, dict):
            reference = capability.get("selection_reference")
    if isinstance(reference, str):
        detail_exercised = False
        for slot_id in _DETAIL_SLOTS:
            where = {"surface": "campaign-detail", "slot": slot_id}
            body = {
                "session_id": session_id,
                "slot_id": slot_id,
                "selection_reference": reference,
            }
            status, decoded = hydration.post(session, _DEEP_PATH, body)
            if status == _HTTP_OK and decoded.get("state") in {"filled", "pending"}:
                _deep_serve(
                    session=session,
                    state=state,
                    where=where,
                    body=body,
                    expected_revision=None,  # detail provenance is the derivation fingerprint
                )
                detail_exercised = True
                break
            state.records.append(
                {**where, "state": decoded.get("state"), "http_status": status, "skipped": True}
            )
        if not detail_exercised:
            # Acceptance must not silently pass with the capability-bound family dark.
            state.fail(
                "detail_unexercised",
                {"page": "campaign-detail"},
                "no detail slot served filled or pending for the first roster row",
            )
    else:
        state.fail("no_capability", {"page": "campaigns"}, "no roster row capability to redeem")
    status, decoded = hydration.post(
        session,
        "/api/presentation/retail-health",
        {"session_id": session_id, "temporal_selection": temporal},
    )
    located = _page_artifact(decoded) if status == _HTTP_OK else None
    if located is None:
        state.fail("http", {"page": "retail-health"}, f"retail parent returned {status}")
        return
    _, artifact = located
    for slot in _filled_slots(artifact, _RETAIL_SLOTS)[:1]:
        _deep_serve(
            session=session,
            state=state,
            where={"surface": "retail-health", "slot": str(slot.get("id"))},
            body={
                "session_id": session_id,
                "slot_id": slot.get("id"),
                "temporal_selection": temporal,
            },
            expected_revision=str(slot.get("artifact_revision")),
        )


def _write_report(request: ProbeRequest, state: ProbeState) -> Path:
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = _REPORT_DIR / f"{stamp}-insight-deep-dive-{request.label}.json"
    report = {
        "probe": "insight-deep-dive",
        "generated_at": stamp,
        "label": request.label,
        "environment": request.environment,
        "preset": _PRESET,
        "counts": {
            "serves": state.serves,
            "cache_hits_observed": state.cache_hits_observed,
            "pending_polls": state.pending_polls,
            "failures": len(state.failures),
        },
        "failures": state.failures,
        "records": state.records,
    }
    path.write_text(json.dumps(report, indent=1, default=str) + "\n")
    return path


def _run(request: ProbeRequest) -> tuple[ProbeState, Path]:
    if request.environment != _ENVIRONMENT:
        raise ProbeError("probe accepts only the dev environment")
    if request.region != _REGION:
        raise ProbeError("probe accepts only the governed dev region")
    hydration.load_env()
    identity = (
        boto3.Session(profile_name=request.profile, region_name=request.region)
        .client("sts")
        .get_caller_identity()
    )
    if str(identity.get("Account", "")) != request.expected_account:
        raise ProbeError("explicit profile account differs from expected account")
    session = hydration.session_from_env()
    hydration.refresh_session(session)
    state = ProbeState()
    _walk(session, state, str(uuid4()))
    return state, _write_report(request, state)


def main() -> int:
    """Run the bounded probe and return nonzero without exposing live evidence."""
    try:
        state, report_path = _run(_arguments())
    except ProbeError as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", "unknown"))
        print(f"FAILED: AWS request returned {code}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"FAILED: unexpected {type(error).__name__}", file=sys.stderr)
        return 1
    timings = " ".join(
        f"{record.get('surface')}/{record.get('slot')}:"
        f"{record.get('first_ms')}ms→{record.get('repeat_ms')}ms"
        for record in state.records
        if "first_ms" in record
    )
    summary = (
        f"serves={state.serves} cache_hits_observed={state.cache_hits_observed} "
        f"pending_polls={state.pending_polls} failures={len(state.failures)} "
        f"timings=[{timings}] report={report_path.relative_to(_ROOT)}"
    )
    if state.failures or state.serves == 0:
        print(f"FAILED: deep-dive acceptance {summary}", file=sys.stderr)
        return 1
    print(f"PASS: deep-dive acceptance {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
