# SPDX-License-Identifier: MIT-0
"""``adapters.jobs.requeue_provider_fence``: list or requeue blocked provider-fence cases (§5).

The only module that invokes the deployed ``ProviderFenceRecoveryFunction``. It has exactly two
modes, ``list`` and ``requeue``; validation of every argument and of the closed
``ProviderFenceRecoveryInvocation.v1`` document completes before any AWS client exists. The ambient
session performs the §3.2 bootstrap (identity, typed parameters, named-stack ownership) and
assumes only the invoke-only ``ProviderFenceRecoveryOperatorRole``; the assumed session creates only
a Lambda client and synchronously invokes only the verified unqualified function.

Output and logs carry only the operation id, opaque recovery reference or cursor, safe reason and
block instant, and the closed outcome or error code.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from dataclasses import dataclass
from typing import Any, cast

import boto3
from aws_lambda_powertools import Logger

from adapters.config import TracingSettings
from adapters.jobs.stage1_operator_bootstrap import (
    PUBLICATIONS_TABLE_KEY,
    BootstrapRequest,
    bootstrap_operator_session,
)
from adapters.outbound.telemetry.bootstrap import configure_tracing, flush_traces
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    flush_metrics,
    report_error,
    report_operation,
)
from adapters.outbound.telemetry.tracing import REQUEUE_PROVIDER_FENCE_PROCESS, span, trace_boundary
from domain.operations.provider_fence_recovery import (
    MAX_LIST_LIMIT,
    PROVIDER_FENCE_RECOVERY_SCHEMA_VERSION,
    ProviderFenceRecoveryErrorCode,
    parse_invocation,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode

__all__ = ["build_parser", "invocation_document", "main", "result_summary"]

LOGGER = Logger(service="adlytics-requeue-provider-fence", level="INFO")
_OPERATION = REQUEUE_PROVIDER_FENCE_PROCESS
_COMPONENT = "requeue_provider_fence.operator"
ROLE_KEY = "providerFenceRecoveryOperatorRoleArn"
FUNCTION_KEY = "providerFenceRecoveryFunctionArn"
PARAMETER_KEYS = (ROLE_KEY, FUNCTION_KEY, PUBLICATIONS_TABLE_KEY)
BROKER_REFUSED = ErrorCode("REQUEUE_PROVIDER_FENCE", 1)
_ACCOUNT = re.compile(r"\A\d{12}\Z")


def _non_blank(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("value must not be blank")
    return value.strip()


def _profile(value: str) -> str:
    normalized = _non_blank(value)
    if normalized.casefold() == "default":
        raise argparse.ArgumentTypeError("an explicit nondefault AWS profile is required")
    return normalized


def _account(value: str) -> str:
    normalized = _non_blank(value)
    if _ACCOUNT.match(normalized) is None:
        raise argparse.ArgumentTypeError("expected account must be a 12-digit AWS account id")
    return normalized


def _limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("limit must be an integer") from error
    if not 1 <= parsed <= MAX_LIST_LIMIT:
        raise argparse.ArgumentTypeError(f"limit must be between 1 and {MAX_LIST_LIMIT}")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Build the two-mode parser; every target-affecting value is explicit."""
    parser = argparse.ArgumentParser(
        prog="python -m adapters.jobs.requeue_provider_fence",
        description="List or requeue blocked provider-fence cases through the deployed broker.",
    )
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--profile", required=True, type=_profile)
    shared.add_argument("--region", required=True, type=_non_blank)
    shared.add_argument("--expected-account", required=True, type=_account)
    shared.add_argument("--env", required=True, type=_non_blank)
    shared.add_argument("--trace", action="store_true")
    modes = parser.add_subparsers(dest="mode", required=True)
    listing = modes.add_parser("list", parents=[shared])
    listing.add_argument("--limit", required=True, type=_limit)
    listing.add_argument("--cursor", type=_non_blank)
    listing.add_argument("--operation-id", type=_non_blank)
    requeue = modes.add_parser("requeue", parents=[shared])
    requeue.add_argument("--operation-id", required=True, type=_non_blank)
    requeue.add_argument("--recovery-ref", required=True, type=_non_blank)
    requeue.add_argument("--resolution-reason", required=True, type=_non_blank)
    return parser


def invocation_document(args: argparse.Namespace) -> dict[str, object]:
    """Render the closed ``Invocation.v1`` document and prove it parses before any client."""
    document: dict[str, object]
    if args.mode == "list":
        document = {
            "schema_version": PROVIDER_FENCE_RECOVERY_SCHEMA_VERSION,
            "mode": "list",
            "limit": args.limit,
        }
        if args.cursor is not None:
            document["cursor"] = args.cursor
    else:
        document = {
            "schema_version": PROVIDER_FENCE_RECOVERY_SCHEMA_VERSION,
            "mode": "requeue",
            "recovery_ref": args.recovery_ref,
            "operation_id": args.operation_id,
            "resolution_reason": args.resolution_reason,
        }
    parse_invocation(document)
    return document


@dataclass(frozen=True, slots=True)
class _Outcome:
    """The closed outcome of one broker invocation, with nothing else carried."""

    outcome: str
    document: dict[str, object] | None
    error_code: str | None


def result_summary(response: dict[str, Any], mode: str) -> _Outcome:
    """Interpret one synchronous invoke: a closed result document or a closed error code."""
    raw: object = json.loads(response["Payload"].read()) if "Payload" in response else None
    payload = cast("dict[str, object]", raw) if isinstance(raw, dict) else None
    if response.get("FunctionError"):
        message = payload.get("errorMessage", "") if payload is not None else ""
        codes = {code.value for code in ProviderFenceRecoveryErrorCode}
        code = message if message in codes else ProviderFenceRecoveryErrorCode.DEPENDENCY_FAILURE
        return _Outcome("failed", None, str(code))
    if (
        payload is None
        or payload.get("schema_version") != PROVIDER_FENCE_RECOVERY_SCHEMA_VERSION
        or payload.get("mode") != mode
    ):
        raise DependencyError(BROKER_REFUSED, "The broker returned an unrecognized result form.")
    outcome = str(payload.get("outcome", "listed"))
    return _Outcome(outcome, payload, None)


@dataclass(frozen=True, slots=True)
class _Lifecycle:
    """One lifecycle record: only the operation id, closed mode, and closed outcome or code."""

    operation_id: str
    phase: str
    outcome: str
    details: dict[str, object]

    def report(self) -> None:
        report_operation(
            OperationLog(
                operation=_OPERATION,
                component=_COMPONENT,
                phase=self.phase,
                outcome=self.outcome,
                severity=LogSeverity.INFO if self.outcome != "failed" else LogSeverity.WARNING,
                details={"operation_id": self.operation_id, **self.details},
            ),
            LOGGER,
        )


def _print(document: dict[str, object]) -> None:
    print(json.dumps(document, sort_keys=True, indent=1))  # noqa: T201


@trace_boundary(REQUEUE_PROVIDER_FENCE_PROCESS)
def main(argv: list[str] | None = None) -> int:
    """Validate, bootstrap, invoke exactly once, and report the closed outcome."""
    args = build_parser().parse_args(argv)
    document = invocation_document(args)
    operation_id: str = args.operation_id or f"list-{uuid.uuid4()}"
    execution_id = uuid.uuid4().hex
    request = BootstrapRequest(
        profile=args.profile,
        region=args.region,
        expected_account=args.expected_account,
        environment=args.env,
        operation_id=operation_id,
        role_key=ROLE_KEY,
        parameter_keys=PARAMETER_KEYS,
    )
    ambient = boto3.Session(profile_name=args.profile, region_name=args.region)
    tracing = TracingSettings(
        enabled=args.trace, aws_region=args.region, environment=args.env, sample_ratio=1.0
    )
    try:
        configure_tracing(tracing, session=ambient._session)
        with span(
            REQUEUE_PROVIDER_FENCE_PROCESS,
            operation_id=operation_id,
            execution_id=execution_id,
            predecessor_execution_id="none",
            plan_digest="none",
            stage="bootstrap",
            completed=0,
            total=1,
            mode=args.mode,
        ) as root:
            _Lifecycle(operation_id, "start", "started", {"mode": args.mode}).report()
            bootstrapped = bootstrap_operator_session(ambient, request)
            _Lifecycle(operation_id, "identity", "verified", {}).report()
            invoke = bootstrapped.assumed.client("lambda").invoke(
                FunctionName=bootstrapped.values[FUNCTION_KEY],
                InvocationType="RequestResponse",
                Payload=json.dumps(document).encode("utf-8"),
            )
            result = result_summary(cast("dict[str, Any]", invoke), args.mode)
            root.record(stage="invoke", completed=1, outcome=result.outcome)
            details: dict[str, object] = {"mode": args.mode}
            if result.error_code is not None:
                details["error_code"] = result.error_code
            elif result.document is not None:
                details["outcome"] = result.outcome
                if "next_cursor" in result.document:
                    details["next_cursor"] = result.document["next_cursor"]
                if "recovery_ref" in result.document:
                    details["recovery_ref"] = result.document["recovery_ref"]
            _Lifecycle(operation_id, "terminal", result.outcome, details).report()
            if result.document is not None:
                _print(result.document)
                return 0
            _print({"mode": args.mode, "error_code": result.error_code})
            return 1
    except Exception as error:
        report_error(
            error, Report(operation=_OPERATION, correlation_id=execution_id, logger=LOGGER)
        )
        _Lifecycle(operation_id, "terminal", "failed", {"mode": args.mode}).report()
        return 1
    finally:
        try:
            flush_metrics()
        finally:
            flush_traces()


if __name__ == "__main__":
    sys.exit(main())
