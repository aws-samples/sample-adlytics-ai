# SPDX-License-Identifier: MIT-0
"""Dev acceptance of the origin-verify rotation, authentication.md acceptance item 5 (ADR-078).

Runs the whole rotation procedure while a watcher sends requests through the distribution:

1. `rotate_origin_verify begin` writes the new version;
2. the given deploy command redeploys `FrontendStack` with
   `ADLYTICS_ORIGIN_VERIFY_SECRET_VERSION_ID` set to that version, and the probe waits until the
   distribution reads back deployed and sending the new current value — which is also the first
   read of whether CloudFormation resolves a version-pinned dynamic reference in an origin custom
   header;
3. straight to the `execute-api` hostname, the previous value is still accepted;
4. `rotate_origin_verify finish` drops the previous value, and once the maximum secret-cache age
   has passed the previous value and a value that was never current are refused, and the current
   one accepted;
5. the deploy command runs again without the pin, and the distribution still sends the current
   value, so no later deploy can resolve a version whose value was dropped.

The watcher alternates `GET /api/me` with a probe-a session (expecting 200) and `GET
/api/auth/login` (expecting 302) through CloudFront, at a pace far below the edge rate rule. Every
value stays in this process's memory. Output is step names, statuses, and counts only.

Usage (the deploy command follows `--` and receives the version id in its environment):
    uv run python validation/probe_origin_verify_rotation.py --profile <PROFILE> --environment dev \
        --region us-east-1 --expected-account <ACCOUNT_ID> --max-cache-age-seconds 300 -- \
        bash -c '. /tmp/adl-env.sh && make deploy STACK=Adlytics-Frontend-dev ...'
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import boto3
from cognito_personas import app_client, load_persona, published_parameter, sign_in

from adapters.jobs.rotate_origin_verify import __main__ as rotation
from adapters.outbound.https import HttpsSession, require_https
from domain.identity.origin_verify import ORIGIN_VERIFY_HEADER, parse_origin_verify_values

_REGION = "us-east-1"
_HTTP = HttpsSession()
_PACE_SECONDS = 2.0
_SESSION_REFRESH_SECONDS = 600
_DEPLOYED_WAIT_SECONDS = 1800
_MARGIN_SECONDS = 30
_REREAD_INTERVAL_SECONDS = 60
"""`EdgeConfig.originVerifyRereadIntervalSeconds` in infra/lib/config."""


@dataclass
class _Watch:
    """Counts per route and status, refreshed session, and a stop flag."""

    origin: str
    session_token: str
    counts: Counter[str] = field(default_factory=Counter[str])
    refused: int = 0
    stop: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def once(self) -> None:
        with self.lock:
            cookie = f"adl_session={self.session_token}"
        for path, headers, expected in (
            ("/api/me", {"Cookie": cookie, "Sec-Fetch-Site": "same-origin"}, 200),
            ("/api/auth/login", {}, 302),
        ):
            try:
                with _HTTP.request(
                    "GET",
                    require_https(f"{self.origin}{path}"),
                    headers=headers,
                    timeout=30,
                    allow_redirects=False,
                ) as response:
                    status = response.status_code
            except Exception:
                status = 0
            self.counts[f"{path} {status}"] += 1
            if status != expected:
                self.refused += 1


def _watch(watch: _Watch, renew: Any) -> None:
    renewed = time.monotonic()
    while not watch.stop.is_set():
        if time.monotonic() - renewed >= _SESSION_REFRESH_SECONDS:
            token = renew()
            with watch.lock:
                watch.session_token = token
            renewed = time.monotonic()
        watch.once()
        watch.stop.wait(_PACE_SECONDS)


def _header_pin(cloudformation: Any, environment: str) -> str | None:
    """Return the version id the deployed FrontendStack template pins the header to, or `None`.

    The template holds the dynamic reference, never the resolved value, so this reads the pin
    itself rather than inferring it from what the distribution sends.
    """
    body = cloudformation.get_template(StackName=f"Adlytics-Frontend-{environment}")["TemplateBody"]
    text = body if isinstance(body, str) else json.dumps(body)
    match = re.search(r":SecretString:current::([0-9a-f-]*)}}", text)
    if match is None:
        raise ValueError("the deployed FrontendStack template carries no origin-verify reference")
    return match.group(1) or None


def _direct(api_host: str, session_token: str, value: str) -> int:
    with _HTTP.request(
        "GET",
        require_https(f"https://{api_host}/api/me"),
        headers={
            "Cookie": f"adl_session={session_token}",
            "Sec-Fetch-Site": "same-origin",
            ORIGIN_VERIFY_HEADER: value,
        },
        timeout=30,
    ) as response:
        return response.status_code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--max-cache-age-seconds", required=True, type=int)
    parser.add_argument("deploy", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0915 - one linear procedure
    """Rotate under watch and print step outcomes and counts."""
    args = _parser().parse_args(argv)
    deploy = [part for part in args.deploy if part != "--"]
    if args.environment != "dev" or args.region != _REGION or not deploy:
        print("rotation acceptance accepts only dev in us-east-1, with a deploy command")
        return 2
    steps: list[dict[str, object]] = []
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    if session.client("sts").get_caller_identity().get("Account") != args.expected_account:
        print("explicit profile did not resolve to the expected account")
        return 1
    ssm = session.client("ssm")
    cognito = session.client("cognito-idp")
    persona = load_persona(ssm, args.environment, "probe-a")
    client = app_client(session, args.environment)

    def renew() -> str:
        return sign_in(cognito, persona, client).access_token

    origin = published_parameter(ssm, args.environment, "publicBaseUrl").rstrip("/")
    api_host = f"{published_parameter(ssm, args.environment, 'apiId')}.execute-api.{args.region}.amazonaws.com"
    distribution_id = published_parameter(ssm, args.environment, "distributionId")
    secrets_manager = session.client("secretsmanager")
    cloudfront = session.client("cloudfront")
    cloudformation = session.client("cloudformation")
    secret_id = rotation.secret_name(args.environment)

    watch = _Watch(origin=origin, session_token=renew())
    watcher = threading.Thread(target=_watch, args=(watch, renew), daemon=True)
    watcher.start()
    try:
        time.sleep(20)
        target = rotation.RotationTarget(secret_id, distribution_id)
        begun = rotation.begin(secrets_manager, cloudfront, target)
        steps.append({"step": "begin", "passed": True})
        values = parse_origin_verify_values(
            secrets_manager.get_secret_value(SecretId=secret_id)["SecretString"]
        )
        # Step 2 starts no sooner than the re-read interval after step 1 (authentication.md).
        time.sleep(_REREAD_INTERVAL_SECONDS + 5)
        env = {**os.environ, "ADLYTICS_ORIGIN_VERIFY_SECRET_VERSION_ID": str(begun["version_id"])}
        # A-SUBPROCESS-OPERATOR-001: operator argv, dev only; see docs/security-dispositions.md
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        deployed = subprocess.call(
            deploy, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        steps.append({"step": "redeploy_pinned", "passed": deployed == 0, "exit_code": deployed})
        if deployed != 0:
            raise RuntimeError("redeploy failed")
        deadline = time.monotonic() + _DEPLOYED_WAIT_SECONDS
        sending = False
        while time.monotonic() < deadline and not sending:
            sending = rotation.distribution_sends_current(
                cloudfront, distribution_id, values.current
            )
            if not sending:
                time.sleep(15)
        steps.append(
            {
                "step": "distribution_sends_pinned_current",
                "passed": sending
                and _header_pin(cloudformation, args.environment) == begun["version_id"],
            }
        )
        if not sending:
            raise RuntimeError("distribution did not resolve the pinned version")
        token = renew()
        steps.append(
            {
                "step": "previous_still_accepted_after_redeploy",
                "passed": _direct(api_host, token, values.previous) == 200,
            }
        )
        rotation.finish(secrets_manager, cloudfront, target)
        steps.append({"step": "finish", "passed": True})
        time.sleep(args.max_cache_age_seconds + _MARGIN_SECONDS)
        token = renew()
        observed = {
            "previous": _direct(api_host, token, values.previous),
            "never_current": _direct(api_host, token, "fictional-never-current-origin-value"),
            "current": _direct(api_host, token, values.current),
        }
        steps.append(
            {
                "step": "after_maximum_cache_age",
                "passed": observed == {"previous": 403, "never_current": 403, "current": 200},
                "observed": observed,
            }
        )
        # Step 5: unpin, so no later deploy can resolve a version whose value was dropped.
        unpinned = {
            k: v for k, v in os.environ.items() if k != "ADLYTICS_ORIGIN_VERIFY_SECRET_VERSION_ID"
        }
        # A-SUBPROCESS-OPERATOR-001: operator argv, dev only; see docs/security-dispositions.md
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        redeployed = subprocess.call(
            deploy, env=unpinned, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        steps.append(
            {"step": "redeploy_unpinned", "passed": redeployed == 0, "exit_code": redeployed}
        )
        deadline = time.monotonic() + _DEPLOYED_WAIT_SECONDS
        sending = False
        while redeployed == 0 and time.monotonic() < deadline and not sending:
            sending = rotation.distribution_sends_current(
                cloudfront, distribution_id, values.current
            )
            if not sending:
                time.sleep(15)
        steps.append(
            {
                "step": "unpinned_distribution_sends_current",
                "passed": sending and _header_pin(cloudformation, args.environment) is None,
            }
        )
        time.sleep(20)
    except Exception as error:
        # Which step the secret was left at, so the operator knows whether `finish` or a
        # redeploy comes next; never a value.
        steps.append(
            {
                "step": "aborted",
                "passed": False,
                "error_class": type(error).__name__,
                "rotation_begun": any(step["step"] == "begin" for step in steps),
                "rotation_finished": any(step["step"] == "finish" for step in steps),
            }
        )
    finally:
        watch.stop.set()
        watcher.join(timeout=60)
    steps.append(
        {
            "step": "watcher_saw_no_refusal",
            "passed": watch.refused == 0 and sum(watch.counts.values()) > 0,
            "requests": sum(watch.counts.values()),
            "refused": watch.refused,
            "by_route_and_status": dict(watch.counts),
        }
    )
    passed = all(bool(step["passed"]) for step in steps)
    print(json.dumps({"steps": steps, "passed": passed}, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
