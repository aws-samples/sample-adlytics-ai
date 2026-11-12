# SPDX-License-Identifier: MIT-0
"""`make deploy-all`'s preflight refuses an account that cannot run the synthesized app.

A failure here lets a first deployment start with a Lambda, Athena or declared quota below what
the app reserves, or with a chat model it cannot use, so a stack fails midway or chat cannot
answer. The script runs as a process against a stub `aws` on PATH that answers from files and
records its argv; the real CLI and AWS are never reachable. Each refusal is paired with the
passing run it departs from.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from gate_script_support import SCRIPTS

pytestmark = pytest.mark.unit

APP_REGION = "test-region-1"
EDGE_REGION = "edge-region-1"
PROFILE = "fixture-profile"
HAIKU = "anthropic.fixture-haiku-v1:0"
MEMORY = "anthropic.fixture-memory"
MODELS = {
    "ADLYTICS_QUICK_CHAT_MODEL_ID": f"us.{HAIKU}",
    "ADLYTICS_DEEP_DIVE_MODEL_ID": f"us.{HAIKU}",
    "ADLYTICS_MEMORY_MODEL_ID": MEMORY,
}

_AWS_STUB = r"""#!/bin/bash
# Test stub of the aws CLI: answers from $STUB_STATE files and never reaches AWS.
echo "aws $*" >> "$STUB_STATE/calls.log"
region="" key=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[$i]}" in
    --region) region="${args[$((i + 1))]}" ;;
    --service-code|--model-id|--inference-profile-identifier) key="${args[$((i + 1))]}" ;;
  esac
done
name="$1-$2-$key-$region"
if [ -f "$STUB_STATE/fail-$1-$2" ]; then
  echo "Could not connect to the endpoint URL: stubbed failure" >&2
  exit 255
fi
if [ ! -f "$STUB_STATE/$name.json" ]; then
  echo "stub: no answer for $name" >&2
  exit 99
fi
cat "$STUB_STATE/$name.json"
"""


class World:
    """A cloud assembly in two Regions, a stub `aws`, and the answers it gives."""

    def __init__(self, root: Path) -> None:
        self.bin = root / "bin"
        self.state = root / "state"
        self.cdk_out = root / "cdk.out"
        for directory in (self.bin, self.state, self.cdk_out):
            directory.mkdir()
        (self.bin / "aws").write_text(_AWS_STUB)
        (self.bin / "aws").chmod(0o755)
        self.templates: dict[str, tuple[str, dict[str, Any]]] = {
            "Adlytics-Api-test": (APP_REGION, _template(30, 5)),
            "Adlytics-Edge-test": (EDGE_REGION, _template(2)),
        }
        self.answer("lambda-get-account-settings--" + APP_REGION, _lambda(135))
        self.answer("lambda-get-account-settings--" + EDGE_REGION, _lambda(1000))
        self.answer("service-quotas-get-service-quota-athena-" + APP_REGION, _quota(200))
        self.answer(
            f"bedrock-get-inference-profile-us.{HAIKU}-{APP_REGION}",
            {
                "models": [
                    {"modelArn": f"arn:aws:bedrock:{APP_REGION}::foundation-model/{HAIKU}"},
                    {"modelArn": f"arn:aws:bedrock:other-region-1::foundation-model/{HAIKU}"},
                ]
            },
        )
        for model in (HAIKU, MEMORY):
            self.answer(f"bedrock-get-foundation-model-availability-{model}-{APP_REGION}", _model())

    def answer(self, name: str, body: dict[str, Any]) -> None:
        (self.state / f"{name}.json").write_text(json.dumps(body))

    def fail(self, service: str, operation: str) -> None:
        (self.state / f"fail-{service}-{operation}").write_text("")

    def run(self) -> subprocess.CompletedProcess[str]:
        artifacts: dict[str, Any] = {"Tree": {"type": "cdk:tree"}}
        for name, (region, template) in self.templates.items():
            (self.cdk_out / f"{name}.template.json").write_text(json.dumps(template))
            artifacts[name] = {
                "type": "aws:cloudformation:stack",
                "environment": f"aws://000000000000/{region}",
                "properties": {"templateFile": f"{name}.template.json"},
            }
        (self.cdk_out / "manifest.json").write_text(json.dumps({"artifacts": artifacts}))
        argv = [
            sys.executable,
            str(SCRIPTS / "deploy_preflight.py"),
            "--profile",
            PROFILE,
            "--region",
            APP_REGION,
            "--cdk-out",
            str(self.cdk_out),
        ]
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.state),
            "STUB_STATE": str(self.state),
            "AWS_CONFIG_FILE": "/dev/null",
            "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
            **MODELS,
        }
        # A-SUBPROCESS-001: the test interpreter and a repository-owned script; no caller input.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        return subprocess.run(  # noqa: S603
            argv, capture_output=True, text=True, check=False, env=env, cwd=SCRIPTS.parent
        )

    @property
    def calls(self) -> list[str]:
        log = self.state / "calls.log"
        return log.read_text().splitlines() if log.exists() else []


def _template(*reserved: int) -> dict[str, Any]:
    resources: dict[str, Any] = {
        f"Function{index}": {
            "Type": "AWS::Lambda::Function",
            "Properties": {"ReservedConcurrentExecutions": value},
        }
        for index, value in enumerate(reserved)
    }
    resources["Unreserved"] = {"Type": "AWS::Lambda::Function", "Properties": {}}
    return {"Resources": resources}


def _lambda(limit: int) -> dict[str, Any]:
    return {"AccountLimit": {"ConcurrentExecutions": limit}}


def _quota(value: float) -> dict[str, Any]:
    return {"Quota": {"QuotaCode": "L-FC5F6546", "Value": value}}


def _model(authorization: str = "AUTHORIZED") -> dict[str, Any]:
    return {
        "agreementAvailability": {"status": "AVAILABLE"},
        "authorizationStatus": authorization,
        "entitlementAvailability": "AVAILABLE",
        "regionAvailability": "AVAILABLE",
    }


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _refusals(result: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in result.stdout.splitlines() if " REFUSED " in f" {line} "]


def test_every_value_at_its_minimum_passes_and_is_printed(world: World) -> None:
    result = world.run()
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    # 30 + 5 reserved + 100 unreserved = 135 in the app Region; 2 + 100 = 102 at the edge.
    assert f"preflight: ok Lambda concurrent executions ({APP_REGION}): read 135, minimum 135" in (
        lines
    )
    assert (
        f"preflight: ok Lambda concurrent executions ({EDGE_REGION}): read 1000, minimum 102"
        in (lines)
    )
    # The contract's batch_athena_budget 41 + chat_reserve_active_dml 20.
    assert (
        f"preflight: ok Athena active DML queries L-FC5F6546 ({APP_REGION}): read 200, minimum 61"
        in lines
    )
    # The inference profile resolves to one foundation model; two IDs name it, checked once.
    assert (
        f"preflight: ok Bedrock model access us.{HAIKU} ({APP_REGION}): read {HAIKU} "
        "AUTHORIZED/AVAILABLE, minimum AUTHORIZED/AVAILABLE "
        "(access granted per the availability API)"
    ) in lines
    assert any(line.startswith(f"preflight: ok Bedrock model access {MEMORY} ") for line in lines)
    assert (
        "preflight: ok SageMaker Processing and other declared quotas: not required by this "
        "deployment"
    ) in lines
    assert all(f"--profile {PROFILE}" in call for call in world.calls)
    assert not any(" request-service-quota-increase " in call for call in world.calls)


def test_a_lambda_limit_one_below_its_minimum_refuses_with_the_increase_command(
    world: World,
) -> None:
    world.answer("lambda-get-account-settings--" + APP_REGION, _lambda(134))
    result = world.run()
    assert result.returncode == 1
    assert _refusals(result) == [
        f"preflight: REFUSED Lambda concurrent executions ({APP_REGION}): read 134, minimum 135"
    ]
    assert (
        "  fix: request an increase: aws service-quotas request-service-quota-increase "
        "--service-code lambda --quota-code L-B99A9384 --desired-value <value> "
        f"--profile {PROFILE} --region {APP_REGION}"
    ) in result.stdout.splitlines()
    assert "preflight: nothing was diffed or deployed" in result.stdout


def test_an_athena_quota_below_61_refuses(world: World) -> None:
    world.answer("service-quotas-get-service-quota-athena-" + APP_REGION, _quota(60.0))
    result = world.run()
    assert result.returncode == 1
    assert _refusals(result) == [
        f"preflight: REFUSED Athena active DML queries L-FC5F6546 ({APP_REGION}): read 60, "
        "minimum 61"
    ]


def test_a_model_without_authorization_refuses_with_the_model_access_page(world: World) -> None:
    world.answer(
        f"bedrock-get-foundation-model-availability-{MEMORY}-{APP_REGION}",
        _model("NOT_AUTHORIZED"),
    )
    result = world.run()
    assert result.returncode == 1
    (refusal,) = _refusals(result)
    assert refusal.startswith(f"preflight: REFUSED Bedrock model access {MEMORY} ")
    assert "NOT_AUTHORIZED/AVAILABLE" in refusal
    assert (
        "  fix: enable model access in the Bedrock console: "
        "https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html"
    ) in result.stdout.splitlines()


def test_a_read_error_refuses_before_any_other_check_passes(world: World) -> None:
    world.fail("service-quotas", "get-service-quota")
    result = world.run()
    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        "preflight: REFUSED aws service-quotas get-service-quota exited 255: Could not connect "
        "to the endpoint URL: stubbed failure",
        "preflight: nothing was diffed or deployed",
    ]


def test_a_declared_sagemaker_minimum_above_the_value_read_refuses(world: World) -> None:
    region, template = world.templates["Adlytics-Api-test"]
    template["Metadata"] = {
        "Adlytics::ServiceQuotaRequirements": [
            {
                "serviceCode": "sagemaker",
                "quotaName": "ml.m5.xlarge for processing job usage",
                "minimum": 8,
            }
        ]
    }
    world.templates["Adlytics-Api-test"] = (region, template)
    world.answer(
        f"service-quotas-list-service-quotas-sagemaker-{APP_REGION}",
        {
            "Quotas": [
                {
                    "QuotaName": "ml.m5.xlarge for processing job usage",
                    "QuotaCode": "L-X",
                    "Value": 4,
                },
                {"QuotaName": "ml.m5.xlarge for endpoint usage", "QuotaCode": "L-Y", "Value": 20},
            ]
        },
    )
    result = world.run()
    assert result.returncode == 1
    assert _refusals(result) == [
        f"preflight: REFUSED sagemaker ml.m5.xlarge for processing job usage ({APP_REGION}): "
        "read 4, minimum 8"
    ]
    assert "--service-code sagemaker --quota-code L-X" in result.stdout
    assert "not required by this deployment" not in result.stdout


def test_a_reserved_concurrency_that_is_not_a_literal_refuses(world: World) -> None:
    region, template = world.templates["Adlytics-Edge-test"]
    template["Resources"]["Function0"]["Properties"]["ReservedConcurrentExecutions"] = {
        "Ref": "Reserved"
    }
    world.templates["Adlytics-Edge-test"] = (region, template)
    result = world.run()
    assert result.returncode == 1
    assert "so the minimum cannot be derived" in result.stdout
    assert world.calls == []
