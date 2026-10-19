# SPDX-License-Identifier: MIT-0
"""Drive one dev chat turn in a real browser and check what it sends and shows (ADR-081 §2).

Checks that the browser sends no `x-correlation-id`, and that the support reference the chat
panel renders equals the receipt the review API returns for the server's correlation. The
correlation and the reference stay in ignored evidence; only booleans are printed.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import cast

import boto3
from cognito_personas import app_client, load_persona, published_parameter, sign_in
from quality_review_client import (
    DEFAULT_REVIEWER_PERSONA,
    ReviewedReceipt,
    reviewed_receipt,
    reviewer_client,
)

_ROOT = Path(__file__).resolve().parents[1]
_EVIDENCE_ROOT = (_ROOT / "validation" / "evidence").resolve()
_REGION = "us-east-1"
_REVIEW_TIMEOUT_SECONDS = 180
_REVIEW_POLL_SECONDS = 10


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--evidence-dir", required=True)
    return parser


def _parameter(environment: str, area: str, name: str) -> str:
    return f"/adlytics/{environment}/{area}/{name}"


def _account(session: boto3.Session, expected: str) -> None:
    account = session.client("sts").get_caller_identity().get("Account")
    if not isinstance(account, str) or account != expected:
        raise ValueError("explicit profile did not resolve to the expected account")


def _token(session: boto3.Session, environment: str) -> tuple[str, str]:
    """Sign the synthetic `probe-a` chat persona in and return its token and the public origin."""
    ssm = session.client("ssm")
    tokens = sign_in(
        session.client("cognito-idp"),
        load_persona(ssm, environment, "probe-a"),
        app_client(session, environment),
    )
    return tokens.access_token, published_parameter(ssm, environment, "publicBaseUrl")


def _receipt(
    session: boto3.Session, environment: str, correlation_id: str
) -> ReviewedReceipt | None:
    """Read the turn's receipt through the review API, waiting for the processor to project it."""
    review = reviewer_client(session, environment, DEFAULT_REVIEWER_PERSONA)
    deadline = time.monotonic() + _REVIEW_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        document = review.turn(correlation_id)
        if document is not None:
            return reviewed_receipt(document)
        time.sleep(_REVIEW_POLL_SECONDS)
    return None


def main(argv: list[str] | None = None) -> int:
    """Write raw SSE only beneath ignored evidence and print bounded protocol facts."""
    args = _parser().parse_args(argv)
    if args.environment != "dev" or args.region != _REGION:
        print("orientation stream probe accepts only the approved dev region")
        return 2
    evidence_dir = (_ROOT / args.evidence_dir).resolve()
    if _EVIDENCE_ROOT not in evidence_dir.parents:
        print("orientation stream evidence must be beneath validation/evidence")
        return 2
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    try:
        _account(session, args.expected_account)
        token, origin = _token(session, args.environment)
        payload = json.dumps({"origin": origin.rstrip("/"), "token": token})
        node = shutil.which("node")
        if node is None:
            raise ValueError("node is unavailable for browser stream capture")
        # A-SUBPROCESS-001: fixed interpreter and script; see docs/security-dispositions.md
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        run = subprocess.run(  # noqa: S603 - executable and script are repository-owned constants
            [node, str(_ROOT / "validation" / "probe_page_orientation_stream.mjs")],
            input=payload,
            text=True,
            capture_output=True,
            check=False,
            cwd=_ROOT,
        )
        if run.returncode != 0:
            stage: object = None
            with contextlib.suppress(AttributeError, json.JSONDecodeError):
                stage = json.loads(run.stdout).get("stage")
            safe_stage = stage if isinstance(stage, str) else "unknown"
            print(f"orientation stream probe failed before capture: {safe_stage}")
            return 1
        result: object = json.loads(run.stdout)
        if not isinstance(result, dict):
            raise ValueError("browser stream capture result is invalid")
        facts = cast("dict[str, object]", result)
        correlation = facts.pop("correlation", None)
        references = facts.pop("rendered_references", [])
        receipt = (
            _receipt(session, args.environment, correlation)
            if isinstance(correlation, str) and correlation
            else None
        )
        rendered = (
            [item for item in cast("list[object]", references) if isinstance(item, str)]
            if isinstance(references, list)
            else []
        )
        facts["response_correlation_present"] = receipt is not None or bool(correlation)
        facts["receipt_found"] = receipt is not None
        facts["rendered_reference_matches_receipt"] = receipt is not None and (
            f"Reference: {receipt.audit_id}" in rendered
        )
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "page-orientation-reference.json").write_text(
            json.dumps({"correlation": correlation, "rendered_references": rendered}),
            encoding="utf-8",
        )
        result = facts
        print(json.dumps(result, separators=(",", ":"), sort_keys=True))
        return 0
    except Exception:
        print("orientation stream probe could not capture the page conversation")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
