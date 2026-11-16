# SPDX-License-Identifier: MIT-0
"""Demand-forecast operator commands: start a run now, or roll back to an earlier generation.

    make demand-forecast-run PROFILE=... ENV=... ACCOUNT=... [RETRY_FAILED=1 REASON=...]
    make demand-forecast-rollback PROFILE=... ENV=... ACCOUNT=... GENERATION=<id> REASON=...

Both take an optional `REGION=`; otherwise the profile's Region.

- **run** (`start_run()`, design §6d) starts the deployed state machine with an execution named
  `<set hash 12>-<UTC date>-op-<n>`. It tries n = 1, 2, ... and moves on at
  `ExecutionAlreadyExists`; it never lists executions, so its cost doesn't grow with history.
  The input holds only `retry_failed` and `operator_reason`, never `scheduled_time`, so
  `SelectInput` treats it as an operator start. `RETRY_FAILED=1` skips only the `last_attempt`
  rule and needs a `REASON`.
- **rollback** (`rollback()`, design §6c) reads `CURRENT`, takes its fence as `expected_fence`,
  and calls the publisher's `rollback()`: the target must be a once-published generation of the
  same set and not the current one, and the transaction is conditioned on that fence. The
  rollback's run ID is `<set hash 12>-<UTC timestamp>-rollback`.

Both refuse a `REASON` that isn't 1 to 200 printable characters before any AWS call, and a
profile whose account isn't `--expected-account` before any other AWS call. A refusal prints
its code and exits 2; nothing was written.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

import boto3
from botocore.exceptions import ClientError

from adapters.jobs.demand_forecast_publisher.handler import Publisher
from adapters.jobs.demand_forecast_publisher.handler import rollback as publisher_rollback
from adapters.outbound.demand_forecast_policy import LoadedPolicy, load_policy
from adapters.outbound.dynamodb.demand_forecast_store import (
    STORE_CLIENT_CONFIG,
    DynamoDbForecastStore,
    forecast_table_name,
)
from adapters.outbound.telemetry.demand_forecast_metrics import CloudWatchDemandForecastMetrics
from domain.forecasting.boundary import ForecastSetV1
from domain.forecasting.publish import (
    OPERATOR_REASON_MAX_CHARS,
    RollbackRefusedError,
    RollbackRequest,
    forecast_set_id,
    operator_reason_valid,
)

if TYPE_CHECKING:
    from types_boto3_stepfunctions.client import SFNClient

ROOT: Final = Path(__file__).resolve().parents[1]
POLICY: Final = ROOT / "contracts/forecasting/demand-forecast.yaml"
REFUSED: Final = 2
_ACCOUNT: Final = re.compile(r"[0-9]{12}")
NAME_TAKEN_CODE: Final = "ExecutionAlreadyExists"


class OperatorRefusedError(Exception):
    """A refused command, raised before any write; the message is the code."""


class NameTakenError(Exception):
    """`StartExecution` refused the name: an execution of that name exists."""


def reason_refusal(reason: str | None, *, required: bool) -> str | None:
    """Return why `REASON` is refused, or None."""
    if reason is None:
        return "OPERATOR_REASON_REQUIRED" if required else None
    if not operator_reason_valid(reason):
        return "OPERATOR_REASON_INVALID"
    return None


def identity_refusal(expected_account: str, caller_account: Callable[[], str]) -> str | None:
    """Return why the profile is refused, or None; the account is read only for a valid ID."""
    if _ACCOUNT.fullmatch(expected_account) is None:
        return "EXPECTED_ACCOUNT_INVALID"
    if caller_account() != expected_account:
        return "ACCOUNT_MISMATCH"
    return None


def set_id_for(policy: LoadedPolicy, marketplace_key: str) -> str:
    """Return the set hash the publisher and the input builder derive (`forecast_set_id`)."""
    selected = policy.policy.input
    return forecast_set_id(
        ForecastSetV1(
            marketplace_key=marketplace_key,
            distributor_view=selected.distributor_view,
            selling_program=selected.selling_program,
        )
    )


# ---- run ----


def operator_run_name(set_id: str, today: date, n: int) -> str:
    """Return `<set hash 12>-<UTC date>-op-<n>` (§6d)."""
    return f"{set_id}-{today.isoformat()}-op-{n}"


class ExecutionStarter(Protocol):
    """Starts the forecast state machine under one name."""

    def start(self, name: str, document: str) -> str:
        """Start an execution; return its name. Raise `NameTakenError` if the name exists."""
        ...


@dataclass(frozen=True)
class OperatorRetry:
    """`RETRY_FAILED` and `REASON`; a retry needs a reason (§6d step 3)."""

    retry_failed: bool = False
    reason: str | None = None

    def __post_init__(self) -> None:
        refusal = reason_refusal(self.reason, required=self.retry_failed)
        if refusal is not None:
            raise OperatorRefusedError(refusal)

    def document(self) -> str:
        """Return the execution input: operator fields only, never `scheduled_time`."""
        return json.dumps({"retry_failed": self.retry_failed, "operator_reason": self.reason})


@dataclass(frozen=True)
class StartRunRequest:
    """One operator start: the set, today's UTC date and the retry fields."""

    set_id: str
    today: date
    retry: OperatorRetry


def start_run(starter: ExecutionStarter, request: StartRunRequest) -> str:
    """Start `-op-1`, or the first free `-op-<n>` today; return the execution name.

    The loop ends at the first name no execution holds, so it makes one call per operator start
    already made that day, plus one.
    """
    document = request.retry.document()
    n = 1
    while True:
        try:
            return starter.start(operator_run_name(request.set_id, request.today, n), document)
        except NameTakenError:
            n += 1


def name_taken(error: ClientError) -> bool:
    """Whether a `StartExecution` error says the name is already used."""
    return error.response.get("Error", {}).get("Code") == NAME_TAKEN_CODE


class SfnExecutionStarter:
    """`ExecutionStarter` over Step Functions `StartExecution`."""

    def __init__(self, client: SFNClient, machine_arn: str) -> None:
        self._client = client
        self._machine = machine_arn

    def start(self, name: str, document: str) -> str:
        """Start one execution; `ExecutionAlreadyExists` becomes `NameTakenError`."""
        try:
            started = self._client.start_execution(
                stateMachineArn=self._machine, name=name, input=document
            )
        except ClientError as error:
            if name_taken(error):
                raise NameTakenError(name) from error
            raise
        return started["executionArn"].rsplit(":", 1)[-1]


# ---- rollback ----


class _NoObjects:
    """The publisher's bucket seam; a rollback reads and writes no object."""

    def get(self, key: str) -> bytes:
        """Refuse: a rollback reads no object."""
        raise AssertionError(f"a rollback reads no object: {key}")

    def put_if_absent(self, key: str, body: bytes) -> bool:
        """Refuse: a rollback writes no object."""
        del body
        raise AssertionError(f"a rollback writes no object: {key}")


def rollback_run_id(set_id: str, at: datetime) -> str:
    """Return `<set hash 12>-<UTC timestamp>-rollback`, the rollback's `RUN#` key."""
    return f"{set_id}-{at.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}-rollback"


@dataclass(frozen=True)
class RollbackCommand:
    """One operator rollback: the target generation and the reason."""

    generation_id: str
    reason: str

    def __post_init__(self) -> None:
        if not self.generation_id:
            raise OperatorRefusedError("GENERATION_REQUIRED")
        refusal = reason_refusal(self.reason or None, required=True)
        if refusal is not None:
            raise OperatorRefusedError(refusal)


@dataclass(frozen=True)
class RollbackDone:
    """What the rollback recorded."""

    outcome: str
    generation_id: str | None
    receipt_sk: str | None
    expected_fence: int


def rollback(publisher: Publisher, command: RollbackCommand) -> RollbackDone:
    """Roll `CURRENT` back to `command.generation_id`, fenced on the fence read now (§6c).

    Raises `RollbackRefusedError` with its code before any write.
    """
    current = publisher.store.get_current(publisher.set_id)
    fence = 0 if current is None else current.fence
    request = RollbackRequest(publisher.set_id, command.generation_id, fence, command.reason)
    run_id = rollback_run_id(publisher.set_id, publisher.now())
    step = publisher_rollback(publisher, request, run_id)
    return RollbackDone(step.outcome, step.generation_id, step.receipt_sk, fence)


# ---- command line ----


@dataclass(frozen=True)
class _Target:
    session: boto3.Session
    account: str
    region: str
    env: str
    policy: LoadedPolicy
    set_id: str


def _target(args: argparse.Namespace) -> _Target:
    sessions: list[boto3.Session] = []

    def caller_account() -> str:
        sessions.append(boto3.Session(profile_name=args.profile, region_name=args.region))
        return str(sessions[0].client("sts").get_caller_identity()["Account"])

    if not args.marketplace_key:
        raise OperatorRefusedError("MARKETPLACE_KEY_REQUIRED")
    refusal = identity_refusal(args.expected_account, caller_account)
    if refusal is not None:
        raise OperatorRefusedError(refusal)
    session = sessions[0]
    if not session.region_name:
        raise OperatorRefusedError("REGION_REQUIRED")
    policy = load_policy(POLICY)
    return _Target(
        session,
        args.expected_account,
        session.region_name,
        args.env,
        policy,
        set_id_for(policy, args.marketplace_key),
    )


def _run(args: argparse.Namespace) -> str:
    retry = OperatorRetry(args.retry_failed, args.reason)  # refuse before any AWS call
    target = _target(args)
    request = StartRunRequest(target.set_id, datetime.now(UTC).date(), retry)
    machine = (
        f"arn:aws:states:{target.region}:{target.account}"
        f":stateMachine:adlytics-{target.env}-demand-forecast"
    )
    starter = SfnExecutionStarter(target.session.client("stepfunctions"), machine)
    return f"execution={start_run(starter, request)}"


def _rollback(args: argparse.Namespace) -> str:
    command = RollbackCommand(args.generation, args.reason or "")  # refuse before any AWS call
    target = _target(args)
    session, policy = target.session, target.policy
    publisher = Publisher(
        policy=policy,
        set_id=target.set_id,
        objects=_NoObjects(),
        store=DynamoDbForecastStore(
            forecast_table_name(target.env),
            policy.policy.serving.item_max_bytes,
            session.client("dynamodb", config=STORE_CLIENT_CONFIG),
        ),
        metrics=CloudWatchDemandForecastMetrics(session.client("cloudwatch")),
        now=lambda: datetime.now(UTC),
    )
    done = rollback(publisher, command)
    return (
        f"outcome={done.outcome} generation={done.generation_id} "
        f"receipt={done.receipt_sk} expected_fence={done.expected_fence}"
    )


def parser() -> argparse.ArgumentParser:
    """Return the command-line parser; `PROFILE` and `ENV` have no default."""
    top = argparse.ArgumentParser(prog="demand-forecast-operator")
    commands = top.add_subparsers(dest="command", required=True)
    for name in ("run", "rollback"):
        command = commands.add_parser(name)
        command.add_argument("--profile", required=True)
        command.add_argument("--env", required=True)
        command.add_argument("--expected-account", required=True)
        command.add_argument("--region", default=None)
        command.add_argument("--marketplace-key", required=True)
        command.add_argument(
            "--reason", default=None, help=f"1 to {OPERATOR_REASON_MAX_CHARS} printable characters"
        )
        if name == "run":
            command.add_argument("--retry-failed", action="store_true")
        else:
            command.add_argument("--generation", required=True)
    return top


def main(argv: list[str]) -> int:
    """Run one command; return 0, or 2 on a refusal (nothing written)."""
    args = parser().parse_args(argv)
    try:
        line = _run(args) if args.command == "run" else _rollback(args)
    except OperatorRefusedError as error:
        sys.stderr.write(f"refused: {error}\n")
        return REFUSED
    except RollbackRefusedError as error:
        sys.stderr.write(f"refused: {error.code.value}\n")
        return REFUSED
    sys.stdout.write(f"{line}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
