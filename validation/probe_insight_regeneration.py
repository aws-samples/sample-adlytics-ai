# SPDX-License-Identifier: MIT-0
"""Verify that served insight slots carry an expected prompt version end to end.

The prompt-iteration loop's verification step (declared-outcomes program): after an
operator activates a new immutable prompt version for a slot, this probe fetches the
live page that serves the slot and attests, from the served payload itself, which
prompt version produced each delivered narration. Cache validity is content identity
(ADR-068), so activation alone makes every affected slot a cache miss — regeneration
needs no invalidation, and this probe proves it happened.

    set -a; source validation/.env.local; set +a
    uv run python validation/probe_insight_regeneration.py \
        --profile <PROFILE> --environment <ENV> --region <REGION> \
        --expected-account <ACCOUNT_ID> --label divergence-v5 \
        --slot divergence --expected-prompt-version v5

Without --expected-prompt-version the probe records the served versions (the
"before" reading of an iteration). With it, the probe FAILS unless (a) no served
instance in a prompted state carries a different version, AND (b) at least one
served instance carries exactly the expected version — so a slot that never
regenerated (everything unprompted: ``deterministic-gate1``, ``deterministic-v1``,
``not-generated``) cannot pass vacuously. A generated silence still attests its
version. The deterministic movers slots never carry a prompt version, so an
expectation on them is refused up front. Sampling scope: one page fetch on one
preset (--preset), and for detail slots the first roster row. Evidence (served
versions, states, narration digests — never narration text) lands under
validation/evidence/reports/.

Credentials come from the environment (ADL_REFRESH, ADL_CSRF, ADL_BASE_URL), never
from arguments. The traceability half lives in the runbook: every generation this
probe causes emits an ``insight.model.call`` span queryable in the ``aws/spans``
log group by slot and prompt version over the probe's printed time window.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import boto3
import probe_presentation_hydration as hydration

_ROOT = Path(__file__).resolve().parents[1]
_REPORTS = _ROOT / "validation" / "evidence" / "reports"
_PRESETS = ("1d", "7d", "14d")

# Which parent page serves each declared slot (metric-focus slots are dialog-served
# and iterate through the same registry; verify them via their parent page's detail).
_PAGE_BY_SLOT = {
    "contribution": "campaigns",
    "divergence": "campaigns",
    "exposure": "campaigns",
    "movers": "campaigns",
    "trend-shape": "campaign-detail",
    "efficiency-drift": "campaign-detail",
    "funnel": "campaign-detail",
    "next-check": "campaign-detail",
    "demand-shape": "retail-health",
    "returns-pressure": "retail-health",
    "ordered-vs-shipped": "retail-health",
    "retail-movers": "retail-health",
}
_UNPROMPTED_VERSIONS = frozenset({"deterministic-gate1", "deterministic-v1", "not-generated"})
_HTTP_OK = 200


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    """Explicit target-affecting inputs; the operator identity rides the session env."""

    profile: str
    environment: str
    region: str
    expected_account: str
    label: str
    slot: str
    expected_prompt_version: str | None
    preset: str


def _arguments() -> ProbeRequest:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--slot", required=True, choices=sorted(_PAGE_BY_SLOT))
    parser.add_argument("--expected-prompt-version", default=None)
    parser.add_argument("--preset", default="14d", choices=_PRESETS)
    args = parser.parse_args()
    if args.expected_prompt_version is not None and args.slot in {"movers", "retail-movers"}:
        parser.error(
            "the deterministic movers slots never carry a prompt version; "
            "an expectation on them can never be attested"
        )
    return ProbeRequest(
        profile=args.profile,
        environment=args.environment,
        region=args.region,
        expected_account=args.expected_account,
        label=args.label,
        slot=args.slot,
        expected_prompt_version=args.expected_prompt_version,
        preset=args.preset,
    )


def _validate_target(request: ProbeRequest) -> None:
    if request.environment != "dev" or request.region != "us-east-1":
        raise SystemExit("probe accepts only the governed dev target")
    identity = (
        boto3.Session(profile_name=request.profile, region_name=request.region)
        .client("sts")
        .get_caller_identity()
    )
    if str(identity.get("Account", "")) != request.expected_account:
        raise SystemExit("explicit profile account differs from expected account")


def _find_lists(node: object, key: str) -> list[list[dict[str, object]]]:
    found: list[list[dict[str, object]]] = []
    if isinstance(node, dict):
        item = cast("dict[str, object]", node)
        value = item.get(key)
        if isinstance(value, list):
            found.append(cast("list[dict[str, object]]", value))
        for child in item.values():
            found.extend(_find_lists(child, key))
    elif isinstance(node, list):
        for child in cast("list[object]", node):
            found.extend(_find_lists(child, key))
    return found


def _served_slots(
    session: hydration.ApiSession, request: ProbeRequest, session_id: str
) -> list[dict[str, object]]:
    """Fetch the page that serves the slot and return every served instance of it."""
    page = _PAGE_BY_SLOT[request.slot]
    temporal = {"kind": "preset", "preset_id": request.preset}
    if page == "retail-health":
        body = _fetched(
            session,
            "/api/presentation/retail-health",
            {"session_id": session_id, "temporal_selection": temporal},
        )
    else:
        body = _fetched(
            session,
            "/api/presentation/campaigns",
            {"session_id": session_id, "segment": 0, "temporal_selection": temporal},
        )
        if page == "campaign-detail":
            body = _fetched(
                session,
                "/api/presentation/campaign-detail",
                {
                    "session_id": session_id,
                    "selection_reference": _first_selection_reference(body),
                },
            )
    served: list[dict[str, object]] = []
    for slots in _find_lists(body, "insights"):
        for slot in slots:
            if slot.get("id") == request.slot:
                served.append(slot)
    if not served:
        raise SystemExit(f"the served page carries no '{request.slot}' slot")
    return served


def _fetched(
    session: hydration.ApiSession, path: str, body: dict[str, object]
) -> dict[str, object]:
    status, decoded = hydration.post(session, path, body)
    if status != _HTTP_OK:
        raise SystemExit(f"{path} returned {status}")
    return decoded


def _first_selection_reference(parent: dict[str, object]) -> str:
    found = _find_lists(parent, "rows")
    rows: list[dict[str, object]] = found[0] if found else []
    if not rows:
        raise SystemExit("campaigns parent returned no rows to open a detail from")
    capability = rows[0].get("capability")
    reference: object = None
    if isinstance(capability, dict):
        reference = cast("dict[str, object]", capability).get("selection_reference")
    if not isinstance(reference, str) or not reference:
        raise SystemExit("the first roster row carries no selection reference")
    return reference


def _record(slot: dict[str, object]) -> dict[str, object]:
    text = slot.get("text")
    record: dict[str, object] = {
        "id": slot.get("id"),
        "state": slot.get("state"),
        "prompt_version": slot.get("prompt_version"),
        "artifact_revision": slot.get("artifact_revision"),
        "narration_sha256": (
            hashlib.sha256(text.encode()).hexdigest() if isinstance(text, str) else None
        ),
    }
    # Mirror the wire's closed shape: the pair is present exactly when declared.
    if "absence_class" in slot:
        record["absence_class"] = slot.get("absence_class")
        record["absence_reason"] = slot.get("absence_reason")
    return record


def main() -> int:
    """Attest served prompt versions for one slot; fail on an unmet expectation."""
    request = _arguments()
    _validate_target(request)
    hydration.load_env()
    session = hydration.session_from_env()
    hydration.refresh_session(session)
    started = datetime.now(tz=UTC)
    served = _served_slots(session, request, str(uuid4()))
    ended = datetime.now(tz=UTC)

    records = [_record(slot) for slot in served]
    failures: list[str] = []
    expected = request.expected_prompt_version
    versions = [str(record["prompt_version"]) for record in records]
    for version in versions:
        if expected is not None and version not in _UNPROMPTED_VERSIONS and version != expected:
            failures.append(
                f"slot {request.slot} served prompt_version {version}, expected {expected}"
            )
    # A pass must attest the expectation, never hold vacuously: if everything served
    # is unprompted, the slot never regenerated and the loop's one job is to say so.
    if expected is not None and expected not in versions:
        failures.append(
            f"no served instance of {request.slot} carries {expected} "
            f"(served: {', '.join(versions)})"
        )

    report = {
        "probe": "insight-regeneration",
        "label": request.label,
        "slot": request.slot,
        "preset": request.preset,
        "expected_prompt_version": expected,
        "window_utc": {"start": started.isoformat(), "end": ended.isoformat()},
        "span_lookup": (
            "aws/spans log group: filter insight.model.call spans by slot and "
            "prompt version over window_utc"
        ),
        "served": records,
        "failures": failures,
    }
    _REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    path = _REPORTS / f"{stamp}-insight-regeneration-{request.label}.json"
    path.write_text(json.dumps(report, indent=1) + "\n")

    summary = "; ".join(f"{record['state']}@{record['prompt_version']}" for record in records)
    if failures:
        print(f"FAILED: {'; '.join(failures)} report={path.relative_to(_ROOT)}")
        return 1
    print(
        f"PASS: slot {request.slot} served [{summary}] "
        f"expected={expected or '(recorded only)'} report={path.relative_to(_ROOT)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
