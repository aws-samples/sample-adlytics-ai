# SPDX-License-Identifier: MIT-0
r"""``adapters.jobs.rotate_origin_verify``: rotate the edge origin-verify value (ADR-078).

The two secret writes of the rotation procedure in docs/concepts/authentication.md (edge entry).
Between them the operator redeploys `FrontendStack` pinned to the version ``begin`` printed:

- ``begin`` (step 1) writes a version whose current value is new and whose previous value is the
  old current. It refuses while a previous value is still held, because a second ``begin`` before
  ``finish`` would drop the value the distribution may still be sending.
- ``finish`` (step 4) writes a version that drops the previous value. It refuses unless the
  distribution reads back deployed and sending the current value, so dropping the previous one
  cannot refuse edge traffic. The dropped value stops being accepted once the maximum secret-cache
  age has passed.

The caller account is proved with STS first. Output is a version id and flags only: never a value.

Usage:
    uv run python -m adapters.jobs.rotate_origin_verify \
        --profile <PROFILE> --environment <ENV> --region <REGION> \
        --expected-account <ACCOUNT_ID> --step begin|finish
"""

from __future__ import annotations

import argparse
import hmac
import json
import secrets
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import boto3

from adapters.jobs.stage1_operator_bootstrap import parameter_path
from domain.identity.origin_verify import ORIGIN_VERIFY_HEADER, parse_origin_verify_values

_VALUE_BYTES = 48
"""64 URL-safe characters, every one valid in a CloudFront origin custom header."""


class RotationRefusedError(Exception):
    """A step whose precondition does not hold; the message names the precondition only."""


def secret_name(environment: str) -> str:
    """Return the secret `FrontendStack` owns; `originVerifySecretName` in infra/lib/config."""
    return f"adlytics/{environment}/edge/origin-verify"


@dataclass(frozen=True, slots=True)
class RotationTarget:
    """The secret to rewrite and the distribution whose origin header must match its current."""

    secret_id: str
    distribution_id: str


def begin(secrets_manager: Any, cloudfront: Any, target: RotationTarget) -> dict[str, object]:
    """Step 1: new current, old current as previous. Returns the new version id.

    Refuses unless the distribution is deployed sending the current value: a drifted
    distribution, such as one redeployed with a stale version pin, would otherwise have its
    value dropped from the secret by this rotation.
    """
    secret_id = target.secret_id
    values = parse_origin_verify_values(
        secrets_manager.get_secret_value(SecretId=secret_id)["SecretString"]
    )
    if values.previous:
        raise RotationRefusedError("a rotation is already in progress; run finish first")
    if not distribution_sends_current(cloudfront, target.distribution_id, values.current):
        raise RotationRefusedError(
            "the distribution is not deployed sending the current value; redeploy FrontendStack "
            "without a version pin and wait for it to deploy"
        )
    document = {"current": secrets.token_urlsafe(_VALUE_BYTES), "previous": values.current}
    written = secrets_manager.put_secret_value(
        SecretId=secret_id, SecretString=json.dumps(document)
    )
    return {"step": "begin", "version_id": written["VersionId"]}


def distribution_sends_current(cloudfront: Any, distribution_id: str, current: str) -> bool:
    """Whether the deployed distribution adds the current value to its API origin requests."""
    distribution = cloudfront.get_distribution(Id=distribution_id)["Distribution"]
    if distribution.get("Status") != "Deployed":
        return False
    origins = distribution["DistributionConfig"]["Origins"].get("Items", [])
    sent = [
        header.get("HeaderValue", "")
        for origin in origins
        for header in origin.get("CustomHeaders", {}).get("Items", [])
        if str(header.get("HeaderName", "")).lower() == ORIGIN_VERIFY_HEADER
    ]
    return len(sent) == 1 and hmac.compare_digest(sent[0].encode(), current.encode())


def finish(secrets_manager: Any, cloudfront: Any, target: RotationTarget) -> dict[str, object]:
    """Step 4: drop the previous value once the distribution sends the current one."""
    secret_id, distribution_id = target.secret_id, target.distribution_id
    values = parse_origin_verify_values(
        secrets_manager.get_secret_value(SecretId=secret_id)["SecretString"]
    )
    if not values.previous:
        raise RotationRefusedError("no rotation is in progress; run begin first")
    if not distribution_sends_current(cloudfront, distribution_id, values.current):
        raise RotationRefusedError(
            "the distribution is not yet deployed sending the current value; redeploy "
            "FrontendStack pinned to the begin version and wait for it to deploy"
        )
    written = secrets_manager.put_secret_value(
        SecretId=secret_id, SecretString=json.dumps({"current": values.current})
    )
    return {"step": "finish", "version_id": written["VersionId"]}


def build_parser() -> argparse.ArgumentParser:
    """Every target-affecting input is explicit; none has a default."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--step", required=True, choices=("begin", "finish"))
    return parser


def _print(document: dict[str, object]) -> None:
    print(json.dumps(document, sort_keys=True))  # noqa: T201 - operator ids and flags only


def main(
    argv: list[str] | None = None,
    *,
    session_factory: Callable[..., Any] = boto3.Session,
) -> int:
    """Prove the account, then run one step and print its version id."""
    args = build_parser().parse_args(argv)
    session = session_factory(profile_name=args.profile, region_name=args.region)
    try:
        account = session.client("sts").get_caller_identity()["Account"]
    # error-propagation: operator CLI; the printed identity-stage error_class and exit 1
    except Exception as error:
        _print({"stage": "identity", "error_class": type(error).__name__})
        return 1
    if account != args.expected_account:
        _print({"stage": "identity", "error_class": "UnexpectedAccount"})
        return 1
    secret_id = secret_name(args.environment)
    try:
        distribution_id = session.client("ssm").get_parameter(
            Name=parameter_path(args.environment, "distributionId")
        )["Parameter"]["Value"]
        step = begin if args.step == "begin" else finish
        result = step(
            session.client("secretsmanager"),
            session.client("cloudfront"),
            RotationTarget(secret_id, str(distribution_id)),
        )
    except RotationRefusedError as refused:
        _print({"stage": args.step, "refused": str(refused)})
        return 1
    # error-propagation: operator CLI; the printed step-stage error_class and exit 1
    except Exception as error:
        _print({"stage": args.step, "error_class": type(error).__name__})
        return 1
    _print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
