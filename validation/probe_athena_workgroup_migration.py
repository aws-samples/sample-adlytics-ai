# SPDX-License-Identifier: MIT-0
"""Read-only readback of the SEMANTIC-M1 Athena workgroup migration (plan 2.5).

Every value is read with an explicit profile, environment, region and account; each is
required, none is defaulted, and a blank or `default` value is refused before the first AWS call.
Three checks, each a pure decision over what was read:

- ``workgroups``: the CHAT (``interactive``), BAKE and INSIGHT_PRECOMPUTE workgroups are enabled,
  enforce their configuration, pin the engine and write to their own ``v2-results/<kind>/``.
- ``lambda_workgroup_env``: every budgeted Lambda's ``ADL_*WORKGROUP*`` environment equals its
  expected map exactly, and none names the retained ``batch`` workgroup.
- ``batch_envelope_census``: the AGG-6 removal trigger for the reconciler's ``batch`` Start. One
  strongly consistent ``Scan`` of the publications base table through the botocore paginator,
  paged until no ``LastEvaluatedKey`` remains, counting ``ATTEMPT#`` items whose state is
  nonterminal and whose persisted start envelope names ``batch``, independent of fence, due time,
  block or reconcile attributes. It never reads the reconciler's eventually consistent GSI. An
  unknown state, a missing or unparseable envelope, a malformed key or a paginator error is
  ``CensusRefused``, never a zero.

- ``budget``: the static Athena start budget (plan S1, 3.5) over the OBSERVED deployed capacity.
  The supplied ``--batch-athena-budget`` and ``--chat-reserve-active-dml`` are compared with the
  contract ``ATHENA_START_BUDGET`` first, and a mismatch is refused ``supplied_limit_mismatch``
  before any ``GetFunctionConcurrency`` call (F4), so a supplied limit can never loosen the rule.
  Each budgeted function's ``ReservedConcurrentExecutions`` (0 is a quiesced row; an absent member
  is unreserved and refused) and the Baker's and InsightPrecompute's
  ``ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY`` feed ``validate_athena_start_budget``; the sequential
  rows' cap is 1, and the two operator rows are 1 x 1 by the runbook rule and are reported
  ``asserted: runbook`` (F5). The load function's reading is reported beside the verdict.

``removal_trigger_met`` is true only when, in the same run, ``lambda_workgroup_env`` passed and
the census counted zero nonterminal ``batch`` envelopes. After cutover no writer composes a
``batch`` envelope, so the count can only fall.

Usage (one line):
    uv run python -m validation.probe_athena_workgroup_migration
        --profile <PROFILE> --env <ENV> --region <REGION> --account <ACCOUNT_ID>
        --batch-athena-budget <N> --chat-reserve-active-dml <N>
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from adapters.jobs.reprocess_semantic_v2.resources import resolve_named_semantic_resources
from adapters.outbound.athena.table_attempt_statements import parse_start_envelope
from domain.operations.athena_start_budget import (
    AthenaStartBudgetRequest,
    AthenaStartBudgetRow,
    BudgetAccepted,
    BudgetRefused,
    validate_athena_start_budget,
)
from domain.operations.generated_execution_graphs import ATHENA_START_BUDGET, BAKE_TASKS
from domain.operations.table_attempt import (
    ATTEMPT_RECORD_PREFIX,
    TABLE_ATTEMPT_PREFIX,
    ProviderTerminalState,
)
from domain.shared_kernel.errors import ValidationError

ENGINE_VERSION: Final = "Athena engine version 3"
"""The engine every workgroup pins (`ATHENA_ENGINE_VERSION`, `infra/lib/config/index.ts`)."""
NONTERMINAL_STATES: Final = frozenset({"intent", "submitted", "cancel_requested", "parked"})
TERMINAL_STATES: Final = frozenset(member.value for member in ProviderTerminalState)
"""A terminal attempt stores its provider terminal state (`parse_attempt`), not a literal."""
KNOWN_STATES: Final = NONTERMINAL_STATES | TERMINAL_STATES
CHECKED_KINDS: Final = ("interactive", "bake", "insight-precompute")
BUDGETED_FUNCTIONS: Final = {
    "baker": "PresetArtifactBaker",
    "table_writer": "TableWriter",
    "inline_ingestion": "IngestionFunction",
    "provider_attempt_reconciler": "ProviderAttemptReconciler",
    "insight_precompute": "InsightPrecompute",
}
"""Each Lambda budget row and its function, `adlytics-<env>-<name>`."""
CAPPED_ROWS: Final = frozenset({"baker", "insight_precompute"})
"""The rows whose in-process start cap is the function's Athena batch concurrency setting."""
OPERATOR_ROWS: Final = ("ingest_operator", "reprocess_operator")
"""One process per row per environment, by the runbook rule; never read from AWS (F5)."""
CAP_VARIABLE: Final = "ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY"


class ProbeError(Exception):
    """A refused input or an identity mismatch; nothing further is read."""


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    """The explicit target every read is bound to."""

    profile: str
    environment: str
    region: str
    account: str
    batch_athena_budget: int
    chat_reserve_active_dml: int

    def __post_init__(self) -> None:
        for name in ("profile", "environment", "region", "account"):
            if not getattr(self, name).strip():
                raise ProbeError(f"--{name} must be an explicit nonblank value")
        if self.profile.casefold() == "default":
            raise ProbeError("an explicit nondefault profile is required")


def workgroup_names(environment: str) -> dict[str, str]:
    """Every Semantic V2 workgroup's physical name (`semanticV2AthenaWorkgroupName`)."""
    return {
        "interactive": f"adlytics-{environment}-v2-interactive",
        "batch": f"adlytics-{environment}-v2-batch",
        "bake": f"adlytics-{environment}-v2-bake",
        "insight-precompute": f"adlytics-{environment}-v2-insight-precompute",
    }


def expected_workgroup_env(environment: str) -> dict[str, dict[str, str]]:
    """Each budgeted Lambda's exact `ADL_*WORKGROUP*` environment after the migration."""
    names = workgroup_names(environment)
    ingestion = {
        "ADL_ATHENA_WORKGROUP": names["interactive"],
        "ADL_SEMANTIC_V2_ATHENA_BAKE_WORKGROUP": names["bake"],
    }
    return {
        f"adlytics-{environment}-PresetArtifactBaker": {
            "ADL_SEMANTIC_V2_ATHENA_BAKE_WORKGROUP": names["bake"],
        },
        f"adlytics-{environment}-IngestionFunction": dict(ingestion),
        f"adlytics-{environment}-TableWriter": dict(ingestion),
        f"adlytics-{environment}-ProviderAttemptReconciler": dict(ingestion),
        f"adlytics-{environment}-InsightPrecompute": {
            "ADL_SEMANTIC_V2_ATHENA_INSIGHT_PRECOMPUTE_WORKGROUP": names["insight-precompute"],
        },
    }


def workgroup_findings(
    observed: Mapping[str, Mapping[str, Any] | None], environment: str, results_bucket: str
) -> tuple[str, ...]:
    """Return `<kind>: <field>` for every CHAT/BAKE/INSIGHT_PRECOMPUTE workgroup difference."""
    findings: list[str] = []
    names = workgroup_names(environment)
    for kind in CHECKED_KINDS:
        workgroup = observed.get(names[kind])
        if workgroup is None:
            findings.append(f"{kind}: missing")
            continue
        configuration = cast("Mapping[str, Any]", workgroup.get("Configuration", {}))
        result = cast("Mapping[str, Any]", configuration.get("ResultConfiguration", {}))
        engine = cast("Mapping[str, Any]", configuration.get("EngineVersion", {}))
        expected = {
            "state": (workgroup.get("State"), "ENABLED"),
            "enforce": (configuration.get("EnforceWorkGroupConfiguration"), True),
            "output": (result.get("OutputLocation"), f"s3://{results_bucket}/{kind}/"),
            "engine": (engine.get("SelectedEngineVersion"), ENGINE_VERSION),
        }
        findings.extend(
            f"{kind}: {field}" for field, (actual, want) in expected.items() if actual != want
        )
    return tuple(findings)


def lambda_env_findings(
    observed: Mapping[str, Mapping[str, str] | None], environment: str
) -> tuple[str, ...]:
    """Return `<function>: <reason>` for every budgeted Lambda whose workgroup env is inexact."""
    findings: list[str] = []
    batch = workgroup_names(environment)["batch"]
    for function, expected in expected_workgroup_env(environment).items():
        variables = observed.get(function)
        if variables is None:
            findings.append(f"{function}: missing")
            continue
        actual = {key: value for key, value in variables.items() if "WORKGROUP" in key}
        if batch in actual.values():
            findings.append(f"{function}: names batch")
        if actual != expected:
            findings.append(f"{function}: workgroup env differs")
    return tuple(findings)


@dataclass(frozen=True, slots=True)
class CensusCount:
    """A complete read: nonterminal `batch` envelopes, pages and attempt items read."""

    nonterminal_batch: int
    pages: int
    items_read: int


@dataclass(frozen=True, slots=True)
class CensusRefused:
    """An incomplete or unreadable census; never reported as a zero."""

    reason: str


def _text(item: Mapping[str, Any], name: str) -> str | None:
    value = item.get(name)
    if not isinstance(value, Mapping):
        return None
    text = cast("Mapping[str, Any]", value).get("S")
    return text if isinstance(text, str) else None


def batch_envelope_census(  # noqa: PLR0911 -- one closed refusal per reason
    pages: Iterable[Mapping[str, Any]], batch_workgroup: str
) -> CensusCount | CensusRefused:
    """Count nonterminal attempts whose persisted start envelope names `batch_workgroup`."""
    count = read_pages = read_items = 0
    try:
        for page in pages:
            read_pages += 1
            for item in cast("Sequence[Mapping[str, Any]]", page.get("Items", [])):
                read_items += 1
                partition, record = _text(item, "partition_key"), _text(item, "record_key")
                if (
                    partition is None
                    or record is None
                    or not partition.startswith(TABLE_ATTEMPT_PREFIX)
                    or not record.startswith(ATTEMPT_RECORD_PREFIX)
                ):
                    return CensusRefused("malformed_item")
                state = _text(item, "state")
                if state not in KNOWN_STATES:
                    return CensusRefused("unknown_state")
                if state in TERMINAL_STATES:
                    continue
                envelope = _text(item, "start_envelope")
                if envelope is None:
                    return CensusRefused("missing_envelope")
                try:
                    workgroup = parse_start_envelope(envelope).workgroup
                except ValidationError:
                    return CensusRefused("unparseable_envelope")
                count += 1 if workgroup == batch_workgroup else 0
    except ClientError, BotoCoreError:
        return CensusRefused("paginator_error")
    if read_pages == 0:
        return CensusRefused("no_page_read")
    return CensusCount(count, read_pages, read_items)


def census_pages(dynamodb: Any, table: str) -> Iterable[Mapping[str, Any]]:
    """Every page of the strongly consistent base-table scan of attempt items."""
    return cast(
        "Iterable[Mapping[str, Any]]",
        dynamodb.get_paginator("scan").paginate(
            TableName=table,
            ConsistentRead=True,
            FilterExpression=(
                "begins_with(partition_key, :tap) AND begins_with(record_key, :attempt)"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":tap": {"S": TABLE_ATTEMPT_PREFIX},
                ":attempt": {"S": ATTEMPT_RECORD_PREFIX},
            },
            ProjectionExpression="partition_key, record_key, #state, start_envelope",
        ),
    )


@dataclass(frozen=True, slots=True)
class BudgetInputRefused:
    """A supplied limit differs from the contract; capacity was never evaluated (F4)."""

    reason: str
    field: str
    supplied: int
    contract: int


@dataclass(frozen=True, slots=True)
class BudgetReadback:
    """The budget verdict, the load function's handoff reading, and the runbook-only rows."""

    verdict: BudgetAccepted | BudgetRefused | BudgetInputRefused
    load_reserved_concurrency: int | None
    asserted: tuple[tuple[str, str], ...] = tuple((row, "runbook") for row in OPERATOR_ROWS)

    @property
    def passed(self) -> bool:
        """True only when the rule accepted the observed capacity."""
        return isinstance(self.verdict, BudgetAccepted)

    def evidence(self) -> dict[str, object]:
        """Return the verdict as JSON-ready values."""
        verdict = self.verdict
        if isinstance(verdict, BudgetAccepted):
            body: dict[str, object] = {"verdict": "accepted", "total": verdict.total}
        elif isinstance(verdict, BudgetRefused):
            body = {
                "verdict": "refused",
                "reason": verdict.reason.value,
                "observed": verdict.observed,
                "limit": verdict.limit,
            }
        else:
            body = {
                "verdict": "refused",
                "reason": verdict.reason,
                "field": verdict.field,
                "supplied": verdict.supplied,
                "contract": verdict.contract,
            }
        return {
            **body,
            "passed": self.passed,
            "load_reserved_concurrency": self.load_reserved_concurrency,
            "asserted": dict(self.asserted),
        }


def supplied_limit_refusal(
    batch_athena_budget: int, chat_reserve_active_dml: int
) -> BudgetInputRefused | None:
    """Refuse the first supplied limit that differs from the contract `athena_start_budget`."""
    for field, supplied in (
        ("batch_athena_budget", batch_athena_budget),
        ("chat_reserve_active_dml", chat_reserve_active_dml),
    ):
        contract = int(ATHENA_START_BUDGET[field])
        if supplied != contract:
            return BudgetInputRefused("supplied_limit_mismatch", field, supplied, contract)
    return None


def _start_cap(variables: Mapping[str, str] | None) -> int:
    """Return a function's in-process cap; a missing or non-integer setting reads as 0."""
    text = (variables or {}).get(CAP_VARIABLE, "")
    return int(text) if text.strip().isdigit() else 0


def read_budget(
    functions: Any,
    request: ProbeRequest,
    variables: Mapping[str, Mapping[str, str] | None],
) -> BudgetReadback:
    """Evaluate the start budget over the deployed reserved concurrency and start caps.

    The supplied limits are compared with the contract before any `GetFunctionConcurrency`
    call; only on equality is capacity read and evaluated, with the contract's limits.
    """
    refusal = supplied_limit_refusal(request.batch_athena_budget, request.chat_reserve_active_dml)
    if refusal is not None:
        return BudgetReadback(refusal, None)
    environment = request.environment
    reserved: dict[str, int | None] = {}
    for row, name in BUDGETED_FUNCTIONS.items():
        response = functions.get_function_concurrency(FunctionName=f"adlytics-{environment}-{name}")
        value = response.get("ReservedConcurrentExecutions")
        reserved[row] = value if isinstance(value, int) else None
    rows = tuple(
        AthenaStartBudgetRow(
            row,
            reserved[row],
            _start_cap(variables.get(f"adlytics-{environment}-{name}"))
            if row in CAPPED_ROWS
            else 1,
        )
        for row, name in BUDGETED_FUNCTIONS.items()
    ) + tuple(AthenaStartBudgetRow(row, 1, 1) for row in OPERATOR_ROWS)
    verdict = validate_athena_start_budget(
        AthenaStartBudgetRequest(
            rows=rows,
            batch_athena_budget=int(ATHENA_START_BUDGET["batch_athena_budget"]),
            chat_reserve_active_dml=int(ATHENA_START_BUDGET["chat_reserve_active_dml"]),
            active_dml_quota=int(ATHENA_START_BUDGET["active_dml_quota"]),
            certified_query_cap=int(BAKE_TASKS["certified_query_cap"]),
        )
    )
    return BudgetReadback(verdict, reserved["inline_ingestion"])


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """Every check's verdict and the AGG-6 removal trigger, as printable evidence."""

    workgroups: tuple[str, ...]
    lambda_workgroup_env: tuple[str, ...]
    census: CensusCount | CensusRefused
    budget: BudgetReadback

    @property
    def removal_trigger_met(self) -> bool:
        """True only with a clean env check and a complete zero census in the same run."""
        return (
            not self.lambda_workgroup_env
            and isinstance(self.census, CensusCount)
            and self.census.nonterminal_batch == 0
        )

    @property
    def failed(self) -> bool:
        """A check failed or the census could not be completed."""
        return (
            bool(self.workgroups or self.lambda_workgroup_env)
            or isinstance(self.census, CensusRefused)
            or not self.budget.passed
        )

    def evidence(self) -> dict[str, object]:
        """Return the report as JSON-ready values; no item content is ever included."""
        census: dict[str, object] = (
            {"refused": self.census.reason}
            if isinstance(self.census, CensusRefused)
            else {
                "nonterminal_batch": self.census.nonterminal_batch,
                "pages": self.census.pages,
                "items_read": self.census.items_read,
            }
        )
        return {
            "workgroups": {"passed": not self.workgroups, "findings": list(self.workgroups)},
            "lambda_workgroup_env": {
                "passed": not self.lambda_workgroup_env,
                "findings": list(self.lambda_workgroup_env),
            },
            "batch_envelope_census": census,
            "budget": self.budget.evidence(),
            "removal_trigger_met": self.removal_trigger_met,
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--env", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--account", required=True)
    # Explicit, never defaulted; each must equal the contract or the budget check refuses.
    parser.add_argument("--batch-athena-budget", required=True, type=int)
    parser.add_argument("--chat-reserve-active-dml", required=True, type=int)
    return parser


def request_from(arguments: Sequence[str]) -> ProbeRequest:
    """Parse and refuse the explicit target before any AWS client exists."""
    args = _parser().parse_args(list(arguments))
    return ProbeRequest(
        args.profile,
        args.env,
        args.region,
        args.account,
        args.batch_athena_budget,
        args.chat_reserve_active_dml,
    )


def _session(request: ProbeRequest) -> boto3.Session:
    session = boto3.Session(profile_name=request.profile, region_name=request.region)
    if session.region_name != request.region:
        raise ProbeError("the session region differs from --region")
    identity = session.client("sts").get_caller_identity()
    if str(identity.get("Account", "")) != request.account:
        raise ProbeError("the explicit profile's account differs from --account")
    return session


def read_report(request: ProbeRequest, session: boto3.Session) -> MigrationReport:
    """Read every check's inputs through the session and decide the report."""
    env = request.environment
    ssm = session.client("ssm")
    resources = resolve_named_semantic_resources(env, session.client("cloudformation"), ssm)
    results_bucket = str(
        ssm.get_parameter(Name=f"/adlytics/{env}/data/v2/athena-results-bucket")["Parameter"].get(
            "Value", ""
        )
    )
    athena = session.client("athena")
    workgroups: dict[str, Mapping[str, Any] | None] = {}
    for name in workgroup_names(env).values():
        try:
            workgroups[name] = cast(
                "Mapping[str, Any]", athena.get_work_group(WorkGroup=name)["WorkGroup"]
            )
        except ClientError:
            workgroups[name] = None
    functions = session.client("lambda")
    variables: dict[str, Mapping[str, str] | None] = {}
    for function in expected_workgroup_env(env):
        try:
            configuration = functions.get_function_configuration(FunctionName=function)
            variables[function] = dict(configuration.get("Environment", {}).get("Variables", {}))
        except ClientError:
            variables[function] = None
    census = batch_envelope_census(
        census_pages(session.client("dynamodb"), resources["publications"]),
        workgroup_names(env)["batch"],
    )
    return MigrationReport(
        workgroup_findings(workgroups, env, results_bucket),
        lambda_env_findings(variables, env),
        census,
        read_budget(functions, request, variables),
    )


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the read-only readback and print its evidence; nonzero when a check fails."""
    try:
        request = request_from(sys.argv[1:] if arguments is None else arguments)
        report = read_report(request, _session(request))
    except ProbeError as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", "unknown"))
        print(f"FAILED: AWS request returned {code}", file=sys.stderr)
        return 1
    print(json.dumps(report.evidence(), indent=2, sort_keys=True))
    if report.failed:
        print("FAILED: a migration check failed or the census was refused", file=sys.stderr)
        return 1
    print("PASS: workgroups and Lambda environments migrated; start budget accepted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
