#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""Refuse a deployment whose account cannot run what the synthesized app needs.

    uv run python scripts/deploy_preflight.py --profile <p> --region <r> --cdk-out infra/cdk.out

`scripts/deploy-all.sh` runs it after synth, before any diff or deploy. Every read is read-only:

- Lambda `AccountLimit.ConcurrentExecutions` in each Region the app deploys to. The minimum is
  the reserved concurrency of that Region's functions in the synthesized templates, plus the 100
  executions Lambda keeps unreserved.
- Athena "Active DML queries" (`L-FC5F6546`) in the deploy Region. The minimum is
  `batch_athena_budget + chat_reserve_active_dml` from contracts/operations/execution-graphs.yaml.
- Each Service Quotas minimum a stack template declares under the metadata key
  `Adlytics::ServiceQuotaRequirements` (`{serviceCode, quotaName, minimum}`), such as SageMaker
  Processing instances.
  None declared prints "not required by this deployment".
- Bedrock model access for every configured model ID (`MODEL_ENV` below). An inference profile
  ID (two or more dots, such as `us.anthropic...`) is resolved to its foundation models. Each must
  read `AUTHORIZED` and `AVAILABLE`; the availability API can report that for a model that still
  cannot be invoked, so a pass reads "access granted per the availability API".

One line per check, with the value read and the minimum. A refusal prints its fix. A read error
refuses too, so nothing is diffed or deployed on an unknown value. Exit 0 only when every check
passes. Writes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = Path("contracts/operations/execution-graphs.yaml")
UNRESERVED_CONCURRENCY = 100
ATHENA_DML_QUOTA = "L-FC5F6546"
# Lambda "Concurrent executions"; used only in the printed increase command.
LAMBDA_CONCURRENCY_QUOTA = "L-B99A9384"
REQUIREMENTS_KEY = "Adlytics::ServiceQuotaRequirements"
MODEL_ENV = (
    "ADLYTICS_QUICK_CHAT_MODEL_ID",
    "ADLYTICS_DEEP_DIVE_MODEL_ID",
    "ADLYTICS_MEMORY_MODEL_ID",
    "ADLYTICS_INSIGHT_QUICK_MODEL_ID",
    "ADLYTICS_INSIGHT_DEEP_MODEL_ID",
)
MODEL_ACCESS_PAGE = "https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html"
_PROFILE_DOTS = 2
NOT_REQUIRED = (
    "preflight: ok SageMaker Processing and other declared quotas: not required by this deployment"
)


class PreflightError(Exception):
    """A value the preflight needs could not be read or derived; the deployment is refused."""


@dataclass(frozen=True)
class Stack:
    """One synthesized stack: its name, Region and template."""

    name: str
    region: str
    template: Mapping[str, Any]


@dataclass(frozen=True)
class Requirement:
    """A Service Quotas minimum a template declares, in the Region its stack deploys to."""

    region: str
    service_code: str
    quota_name: str
    minimum: int


@dataclass(frozen=True)
class Check:
    """One check's outcome: what was read, the minimum, and the fix when it falls short."""

    name: str
    value: str
    minimum: str
    ok: bool
    fix: str = ""


@dataclass(frozen=True)
class Target:
    """Where to read: the profile, the deploy Region and the CLI runner."""

    profile: str
    region: str
    aws: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] = field(
        default_factory=lambda: _run_aws
    )


def _run_aws(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    # A-SUBPROCESS-001: a fixed `aws` argv built in this module; see docs/security-dispositions.md
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    # `aws` from PATH, as scripts/deploy-all.sh, which checks it is installed, runs it.
    return subprocess.run(  # noqa: S603
        ["aws", *argv],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )


def read_json(target: Target, region: str, argv: Sequence[str]) -> dict[str, Any]:
    """Run one read-only `aws` call and return its JSON object, refusing on any error."""
    try:
        result = target.aws(
            [*argv, "--profile", target.profile, "--region", region, "--output", "json"]
        )
    except OSError as error:
        raise PreflightError(f"aws could not start: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr.strip().splitlines() or ["(no output)"])[-1]
        raise PreflightError(f"aws {' '.join(argv[:2])} exited {result.returncode}: {detail}")
    try:
        parsed: object = json.loads(result.stdout)
    except ValueError as error:
        raise PreflightError(f"aws {' '.join(argv[:2])} returned no JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise PreflightError(f"aws {' '.join(argv[:2])} returned no JSON object")
    return cast("dict[str, Any]", parsed)


# --- What the synthesized app needs (pure) ------------------------------------------------------


def stacks(cdk_out: Path, default_region: str) -> list[Stack]:
    """Return every stack in the cloud assembly, with its Region and template."""
    try:
        manifest = json.loads((cdk_out / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PreflightError(f"no readable cloud assembly at {cdk_out}: {error}") from error
    found: list[Stack] = []
    for name, artifact in cast("dict[str, Any]", manifest.get("artifacts", {})).items():
        if artifact.get("type") != "aws:cloudformation:stack":
            continue
        region = str(artifact.get("environment", "")).rsplit("/", 1)[-1]
        if not region or region.startswith("unknown-"):
            region = default_region
        template_file = artifact.get("properties", {}).get("templateFile")
        try:
            template = json.loads((cdk_out / str(template_file)).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise PreflightError(f"{name}: no readable template: {error}") from error
        found.append(Stack(str(name), region, cast("dict[str, Any]", template)))
    if not found:
        raise PreflightError(f"the cloud assembly at {cdk_out} holds no stack")
    return found


def lambda_minimums(synthesized: Sequence[Stack]) -> dict[str, int]:
    """Return, per Region, the reserved concurrency of its functions plus the unreserved 100."""
    reserved: dict[str, int] = {}
    for stack in synthesized:
        total = reserved.setdefault(stack.region, 0)
        for logical_id, resource in cast(
            "dict[str, Any]", stack.template.get("Resources", {})
        ).items():
            if resource.get("Type") != "AWS::Lambda::Function":
                continue
            value = resource.get("Properties", {}).get("ReservedConcurrentExecutions")
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise PreflightError(
                    f"{stack.name}/{logical_id}: reserved concurrency is not a literal integer, "
                    "so the minimum cannot be derived"
                )
            total += value
        reserved[stack.region] = total
    return {region: total + UNRESERVED_CONCURRENCY for region, total in reserved.items()}


def athena_minimum(root: Path) -> int:
    """Return batch_athena_budget + chat_reserve_active_dml from the execution-graph contract."""
    try:
        contract = yaml.safe_load((root / CONTRACT).read_text(encoding="utf-8"))
        budget = contract["athena_start_budget"]
        return int(budget["batch_athena_budget"]) + int(budget["chat_reserve_active_dml"])
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError) as error:
        raise PreflightError(
            f"cannot derive the Athena minimum from {CONTRACT}: {error}"
        ) from error


def declared_requirements(synthesized: Sequence[Stack]) -> list[Requirement]:
    """Return every Service Quotas minimum a stack template declares in its metadata."""
    found: list[Requirement] = []
    for stack in synthesized:
        entries = stack.template.get("Metadata", {}).get(REQUIREMENTS_KEY, [])
        if not isinstance(entries, list):
            raise PreflightError(f"{stack.name}: {REQUIREMENTS_KEY} is not a list")
        for entry in cast("list[Any]", entries):
            try:
                found.append(
                    Requirement(
                        stack.region,
                        str(entry["serviceCode"]),
                        str(entry["quotaName"]),
                        int(entry["minimum"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as error:
                raise PreflightError(f"{stack.name}: malformed {REQUIREMENTS_KEY}") from error
    return found


def model_ids(env: Mapping[str, str]) -> list[str]:
    """Return each configured model ID once, in MODEL_ENV order; unset optional ones are skipped."""
    ids: list[str] = []
    for key in MODEL_ENV:
        value = env.get(key, "").strip()
        if value and value not in ids:
            ids.append(value)
    return ids


# --- Reading the account and deciding ----------------------------------------------------------


def _increase(target: Target, region: str, quota: tuple[str, str]) -> str:
    """Return the Service Quotas increase command for `quota`, a (service code, quota code)."""
    service, code = quota
    return (
        f"request an increase: aws service-quotas request-service-quota-increase --service-code "
        f"{service} --quota-code {code} --desired-value <value> --profile {target.profile} "
        f"--region {region}"
    )


def check_lambda(target: Target, minimums: Mapping[str, int]) -> list[Check]:
    """Read each Region's Lambda account concurrency against its minimum."""
    checks: list[Check] = []
    for region, minimum in sorted(minimums.items()):
        settings = read_json(target, region, ["lambda", "get-account-settings"])
        try:
            value = int(settings["AccountLimit"]["ConcurrentExecutions"])
        except (KeyError, TypeError, ValueError) as error:
            raise PreflightError(f"lambda get-account-settings in {region}: no limit") from error
        checks.append(
            Check(
                f"Lambda concurrent executions ({region})",
                str(value),
                str(minimum),
                value >= minimum,
                _increase(target, region, ("lambda", LAMBDA_CONCURRENCY_QUOTA)),
            )
        )
    return checks


def check_athena(target: Target, minimum: int) -> Check:
    """Read the Athena active DML quota in the deploy Region."""
    region = target.region
    quota = read_json(
        target,
        region,
        [
            "service-quotas",
            "get-service-quota",
            "--service-code",
            "athena",
            "--quota-code",
            ATHENA_DML_QUOTA,
        ],
    )
    try:
        value = int(float(quota["Quota"]["Value"]))
    except (KeyError, TypeError, ValueError) as error:
        raise PreflightError("service-quotas get-service-quota athena: no value") from error
    return Check(
        f"Athena active DML queries {ATHENA_DML_QUOTA} ({region})",
        str(value),
        str(minimum),
        value >= minimum,
        _increase(target, region, ("athena", ATHENA_DML_QUOTA)),
    )


def check_requirement(target: Target, requirement: Requirement) -> Check:
    """Read one declared Service Quotas minimum by its quota name."""
    listed = read_json(
        target,
        requirement.region,
        ["service-quotas", "list-service-quotas", "--service-code", requirement.service_code],
    )
    quotas = [
        q
        for q in cast("list[dict[str, Any]]", listed.get("Quotas", []))
        if q.get("QuotaName") == requirement.quota_name
    ]
    if len(quotas) != 1:
        raise PreflightError(
            f"{requirement.service_code} quota {requirement.quota_name!r} is listed "
            f"{len(quotas)} times in {requirement.region}"
        )
    (quota,) = quotas
    value = int(float(quota.get("Value", 0)))
    return Check(
        f"{requirement.service_code} {requirement.quota_name} ({requirement.region})",
        str(value),
        str(requirement.minimum),
        value >= requirement.minimum,
        _increase(
            target, requirement.region, (requirement.service_code, str(quota.get("QuotaCode")))
        ),
    )


def foundation_models(target: Target, model_id: str) -> list[str]:
    """Return the foundation model IDs one configured ID names (a profile resolves to several)."""
    if model_id.count(".") < _PROFILE_DOTS:
        return [model_id]
    profile = read_json(
        target,
        target.region,
        ["bedrock", "get-inference-profile", "--inference-profile-identifier", model_id],
    )
    found: list[str] = []
    for model in cast("list[dict[str, Any]]", profile.get("models", [])):
        name = str(model.get("modelArn", "")).rpartition("foundation-model/")[2]
        if name and name not in found:
            found.append(name)
    if not found:
        raise PreflightError(f"inference profile {model_id} names no foundation model")
    return found


def check_model(target: Target, model_id: str) -> Check:
    """Read the availability of every foundation model a configured ID names."""
    states: list[str] = []
    ok = True
    for name in foundation_models(target, model_id):
        found = read_json(
            target,
            target.region,
            ["bedrock", "get-foundation-model-availability", "--model-id", name],
        )
        authorization = str(found.get("authorizationStatus"))
        entitlement = str(found.get("entitlementAvailability"))
        ok = ok and authorization == "AUTHORIZED" and entitlement == "AVAILABLE"
        states.append(f"{name} {authorization}/{entitlement}")
    return Check(
        f"Bedrock model access {model_id} ({target.region})",
        "; ".join(states),
        "AUTHORIZED/AVAILABLE",
        ok,
        f"enable model access in the Bedrock console: {MODEL_ACCESS_PAGE}",
    )


@dataclass(frozen=True)
class Needs:
    """Everything the synthesized app needs, derived before any read."""

    lambda_minimums: Mapping[str, int]
    athena_minimum: int
    requirements: Sequence[Requirement]
    model_ids: Sequence[str]


def evaluate(target: Target, needs: Needs) -> list[Check]:
    """Read every value the app needs and compare it with its minimum."""
    checks = check_lambda(target, needs.lambda_minimums)
    checks.append(check_athena(target, needs.athena_minimum))
    checks.extend(check_requirement(target, requirement) for requirement in needs.requirements)
    checks.extend(check_model(target, model_id) for model_id in needs.model_ids)
    return checks


def _line(check: Check) -> str:
    status = "ok" if check.ok else "REFUSED"
    granted = (
        " (access granted per the availability API)" if check.ok and "Bedrock" in check.name else ""
    )
    line = f"preflight: {status} {check.name}: read {check.value}, minimum {check.minimum}{granted}"
    return line if check.ok else f"{line}\n  fix: {check.fix}"


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """Derive the needs from the cloud assembly, read the account, print, and exit 0 or 1."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--profile", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--cdk-out", required=True, type=Path)
    args = parser.parse_args(argv)
    target = Target(args.profile, args.region)
    try:
        synthesized = stacks(args.cdk_out, args.region)
        needs = Needs(
            lambda_minimums(synthesized),
            athena_minimum(ROOT),
            declared_requirements(synthesized),
            model_ids(os.environ if env is None else env),
        )
        if not needs.model_ids:
            raise PreflightError("no Bedrock model ID is configured (see .env.example)")
        checks = evaluate(target, needs)
    except PreflightError as error:
        print(f"preflight: REFUSED {error}")  # noqa: T201
        print("preflight: nothing was diffed or deployed")  # noqa: T201
        return 1
    for check in checks:
        print(_line(check))  # noqa: T201
    if not needs.requirements:
        print(NOT_REQUIRED)  # noqa: T201
    if all(check.ok for check in checks):
        return 0
    print("preflight: nothing was diffed or deployed")  # noqa: T201
    return 1


if __name__ == "__main__":
    sys.exit(main())
