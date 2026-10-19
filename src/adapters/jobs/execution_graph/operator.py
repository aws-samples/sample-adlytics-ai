# SPDX-License-Identifier: MIT-0
"""The human-only operator CLI for the execution-graph authority (S1: modes and direct requests).

Runs only under the bound human operator profile. No Lambda role and no agent principal has
IAM for these commands, and an agent principal is refused before any write
(`AGENT_MUTATION_NOT_ENABLED`, Tier 0/1). Every target-affecting input is explicit: profile,
region, expected account, environment, and the store stack that must own the table.

    python -m adapters.jobs.execution_graph.operator set-mode --to SHADOW --expected-revision 0 ...
    python -m adapters.jobs.execution_graph.operator admit --request-id ... --manifest-key ...
    python -m adapters.jobs.execution_graph.operator show-mode | show-receipt --request-id ...

`set-mode` writes one command receipt with the CAS (epoch 0 outside GRAPH). S6: the drain CAS
needs `--drain-deadline` and the coordinator table (`--coordinator-table`, `--coordinator-stack`);
`set-mode --to LEGACY` from `LEGACY_DRAINING` is the abort (refused once `graph_entered_at`
exists); `GRAPH` is entered only with `enter-graph`, which runs the zero proof and binds the CAS
to `DRAIN_SEQ`; `drain-status` and `enumerate` are the read-only proof steps (`cutover.py`).
In `GRAPH`, `enumerate` is the uncounted post-CAS re-read and exits 3 when F1-F4 are non-zero.
`admit` is the direct-operator `on_demand` requester. S3 adds `pause`, `resume` and `cancel`
(`--run-id`) and `set-hold` / `clear-hold` (`commands.py`); each writes exactly one command
receipt, and a domain refusal exits 3 with its signal. S6 part 2 (§2.12) adds `dlq-status`
(read-only) and `redrive --queue {delivery,semantic-write,bake,precompute} --expected-count N`
(`dead_letters.py`); neither reads the graph table. Each command prints one `RESULT {json}` line.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import boto3
from aws_lambda_powertools import Logger

from adapters.inbound.run_requests.service import (
    AdmissionDependencies,
    RunRequest,
    admit_run_request,
)
from adapters.jobs.execution_graph.commands import (
    OperatorCommand,
    RunCommandKind,
    run_operator_command,
)
from adapters.jobs.execution_graph.cutover import (
    CutoverCommand,
    CutoverDependencies,
    DrainProof,
    abort_drain,
    begin_drain,
    counted_enumeration,
    enter_graph,
    post_graph_enumeration,
    refuse_plain_graph_cas,
)
from adapters.jobs.execution_graph.dead_letters import (
    DEAD_LETTER_QUEUES,
    dead_letter_route,
    dlq_status,
    redrive,
)
from adapters.jobs.execution_graph.resolution import table_parameter_path
from adapters.jobs.reprocess_semantic_v2.resources import (
    NamedRawResourceRequest,
    resolve_named_semantic_resources,
    verify_named_raw_bucket,
)
from adapters.outbound.cloudformation_stack import stack_resource_summaries
from adapters.outbound.dynamodb.execution_graph import DynamoExecutionGraphStore
from adapters.outbound.dynamodb.execution_graph_dispatch import DynamoGraphDispatchStore
from adapters.outbound.dynamodb.legacy_families import DynamoLegacyFamilies, FamilyEnumeration
from adapters.outbound.dynamodb.stage1_writer_authority import Stage1WriterAuthorityReader
from adapters.outbound.legacy_head import S3CoordinatorLegacyHead
from adapters.outbound.s3.run_request_manifests import S3RunRequestManifests
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    flush_metrics,
    report_error,
    report_operation,
)
from domain.operations.cutover import FENCE_MARGIN_SECONDS, drain_deadline_bounds
from domain.operations.execution_graph import (
    MIGRATION_FENCE_SET,
    AuthorityMode,
    GraphCommandRefusedError,
    epoch_for_mode,
    require_human_principal,
    require_protocol_v1,
    validate_mode_transition,
)
from domain.operations.generated_execution_graphs import PROTOCOL_VERSION, T_MAX_SECONDS
from domain.operations.ingestion_root import RootRefusedError
from ports.outbound.execution_graph import (
    AdmissionConflictError,
    CommandReceipt,
    ExecutionGraphStore,
    ModeChange,
    RollingFreshnessFence,
    RollingFreshnessFenceChange,
    RollingFreshnessFenceRead,
)

__all__ = ["admission_dependencies", "main", "table_parameter_path"]

LOGGER: Final = Logger(service="adlytics-execution-graph-operator")
_OPERATION: Final = "execution_graph.operator"
_COMPONENT: Final = "execution_graph.operator"
_COMMAND_ID: Final = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_ROLLING_FRESHNESS_REVISION: Final = 12
REFUSED_EXIT: Final = 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--env", required=True)
    parser.add_argument("--store-stack", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    mode = commands.add_parser("set-mode")
    mode.add_argument("--command-id", required=True)
    mode.add_argument("--to", required=True, choices=[item.value for item in AuthorityMode])
    mode.add_argument("--expected-revision", required=True, type=int)
    mode.add_argument("--protocol-version", default=PROTOCOL_VERSION)
    mode.add_argument(
        "--drain-deadline",
        help="S6: the drain deadline D (ISO-8601 UTC), required for --to LEGACY_DRAINING.",
    )
    _cutover_arguments(mode)
    for name in ("drain-status", "enumerate"):
        _cutover_arguments(commands.add_parser(name), required=True)
    graph = commands.add_parser("enter-graph")
    graph.add_argument("--command-id", required=True)
    graph.add_argument("--expected-revision", required=True, type=int)
    graph.add_argument("--protocol-version", default=PROTOCOL_VERSION)
    _cutover_arguments(graph, required=True)
    admit = commands.add_parser("admit")
    admit.add_argument("--request-id", required=True)
    admit.add_argument("--manifest-key", required=True)
    admit.add_argument("--version-id", required=True)
    admit.add_argument("--reason", required=True)
    admit.add_argument("--raw-stack", required=True)
    admit.add_argument("--bucket", required=True)
    admit.add_argument("--label")
    admit.add_argument("--protocol-version", default=PROTOCOL_VERSION)
    commands.add_parser("show-mode")
    set_fence = commands.add_parser("set-rolling-freshness-fence")
    set_fence.add_argument("--command-id", required=True)
    set_fence.add_argument("--expected-mode-revision", required=True, type=int)
    set_fence.add_argument("--protocol-version", default=PROTOCOL_VERSION)
    show_fence = commands.add_parser("show-rolling-freshness-fence")
    show_fence.add_argument("--migration-id", required=True)
    receipt = commands.add_parser("show-receipt")
    receipt.add_argument("--request-id", required=True)
    for name in ("pause", "resume", "cancel"):
        run_command = commands.add_parser(name)
        run_command.add_argument("--command-id", required=True)
        run_command.add_argument("--run-id", required=True)
        run_command.add_argument("--protocol-version", default=PROTOCOL_VERSION)
    for name in ("set-hold", "clear-hold"):
        hold = commands.add_parser(name)
        hold.add_argument("--command-id", required=True)
        hold.add_argument("--protocol-version", default=PROTOCOL_VERSION)
    commands.add_parser("dlq-status")
    move = commands.add_parser("redrive")
    # Free text, refused in `dead_letters.py` with REDRIVE_QUEUE_UNKNOWN (a typed exit 3).
    move.add_argument("--queue", required=True, help=", ".join(sorted(DEAD_LETTER_QUEUES)))
    move.add_argument("--expected-count", required=True, type=int)
    return parser


def _cutover_arguments(parser: argparse.ArgumentParser, *, required: bool = False) -> None:
    """S6: the coordinator table the zero proof reads, and the stack that must own it."""
    parser.add_argument("--coordinator-table", required=required)
    parser.add_argument("--coordinator-stack", required=required)


def _correlation(arguments: argparse.Namespace) -> str:
    return str(
        getattr(arguments, "command_id", None)
        or getattr(arguments, "request_id", None)
        or getattr(arguments, "migration_id", None)
        or "operator"
    )


def _print(result: dict[str, object]) -> None:
    print(f"RESULT {json.dumps(result, sort_keys=True, default=str)}")  # noqa: T201


def _resolve_table(session: boto3.Session, arguments: argparse.Namespace) -> str:
    """Resolve the table through its exact SSM path and require the named stack to own it."""
    path = table_parameter_path(str(arguments.env))
    value = str(session.client("ssm").get_parameter(Name=path)["Parameter"].get("Value", ""))
    resources = stack_resource_summaries(
        session.client("cloudformation"), str(arguments.store_stack)
    )
    pairs = {(item.get("ResourceType"), item.get("PhysicalResourceId")) for item in resources}
    owned = ("AWS::DynamoDB::Table", value) in pairs and ("AWS::SSM::Parameter", path) in pairs
    if not owned:
        raise PermissionError("the named store stack does not own the execution-graph table.")
    return value


def _set_mode(context: _Context, arguments: argparse.Namespace) -> int:
    store, principal = context.store, context.principal
    if _COMMAND_ID.fullmatch(str(arguments.command_id)) is None:
        raise RootRefusedError("SCHEMA_INVALID", "the command id is malformed.")
    require_protocol_v1(str(arguments.protocol_version))
    current = store.read_mode()
    to_mode = AuthorityMode(str(arguments.to))
    validate_mode_transition(current, to_mode, int(arguments.expected_revision))
    if to_mode is AuthorityMode.GRAPH:
        refuse_plain_graph_cas()
    if to_mode is AuthorityMode.LEGACY_DRAINING or current.mode is AuthorityMode.LEGACY_DRAINING:
        return _cutover_set_mode(context, arguments, to_mode)
    receipt = CommandReceipt(
        command_id=str(arguments.command_id),
        command="set_mode",
        principal=principal,
        # The CAS into GRAPH is the first epoch-1 act, so its receipt carries the target's
        # epoch; every other transition records the epoch of the mode it leaves (S1A-13).
        authority_epoch=epoch_for_mode(to_mode if to_mode is AuthorityMode.GRAPH else current.mode),
        mode=current.mode,
        outcome="committed",
        recorded_at=datetime.now(UTC).isoformat(),
        protocol_version=PROTOCOL_VERSION,
        details={"from": current.mode.value, "to": to_mode.value},
    )
    store.commit_mode(ModeChange(current, to_mode, receipt))
    after = store.read_mode()
    _print(
        {
            "command": "set_mode",
            "receipt": asdict(receipt),
            "mode": after.mode.value,
            "revision": after.revision,
        }
    )
    return 0


def _validated_fence_readback(context: _Context, migration_id: str) -> RollingFreshnessFenceRead:
    """Require one matching retained fence, receipt, and held GRAPH authority readback."""
    readback = context.store.read_rolling_freshness_fence(migration_id)
    fence, receipt, mode = readback.fence, readback.receipt, readback.mode
    if (
        fence is None
        or receipt is None
        or fence.state != "SET"
        or fence.migration_id != migration_id
        or fence.owner != receipt.principal
        or fence.set_at != receipt.recorded_at
        or fence.revision != mode.revision
        or receipt.command_id != migration_id
        or receipt.command != "set_rolling_freshness_fence"
        or receipt.mode is not AuthorityMode.GRAPH
        or receipt.outcome != "committed"
        or mode.mode is not AuthorityMode.GRAPH
        or not mode.hold
    ):
        raise GraphCommandRefusedError(
            MIGRATION_FENCE_SET,
            "the rolling-freshness fence readback does not match held GRAPH authority.",
        )
    return readback


def _fence_view(context: _Context, readback: RollingFreshnessFenceRead) -> dict[str, object]:
    fence, receipt = readback.fence, readback.receipt
    assert fence is not None and receipt is not None  # noqa: S101 -- validated immediately above
    return {
        "caller": context.principal,
        "table": context.table,
        "fence": asdict(fence),
        "receipt": asdict(receipt),
        "mode": readback.mode.mode.value,
        "mode_revision": readback.mode.revision,
        "hold": readback.mode.hold,
    }


def _set_rolling_freshness_fence(context: _Context, arguments: argparse.Namespace) -> int:
    """Set the one-time fence while GRAPH revision 12 remains held, or return exact replay."""
    migration_id = str(arguments.command_id)
    if _COMMAND_ID.fullmatch(migration_id) is None:
        raise RootRefusedError("SCHEMA_INVALID", "the command id is malformed.")
    require_protocol_v1(str(arguments.protocol_version))
    expected_revision = int(arguments.expected_mode_revision)
    if expected_revision != _ROLLING_FRESHNESS_REVISION or str(arguments.env) != "dev":
        raise RootRefusedError(
            "SCHEMA_INVALID", "the rolling-freshness fence requires dev GRAPH revision 12."
        )
    existing = context.store.read_rolling_freshness_fence(migration_id)
    if existing.fence is not None:
        validated = _validated_fence_readback(context, migration_id)
        if validated.fence is None or validated.fence.owner != context.principal:
            raise GraphCommandRefusedError(
                MIGRATION_FENCE_SET, "the rolling-freshness fence belongs to another operator."
            )
        _print(
            {
                "command": str(arguments.command),
                "replay": True,
                **_fence_view(context, validated),
            }
        )
        return 0
    recorded_at = datetime.now(UTC).isoformat()
    receipt = CommandReceipt(
        command_id=migration_id,
        command="set_rolling_freshness_fence",
        principal=context.principal,
        authority_epoch=1,
        mode=AuthorityMode.GRAPH,
        outcome="committed",
        recorded_at=recorded_at,
        protocol_version=PROTOCOL_VERSION,
        details={
            "migration_id": migration_id,
            "expected_mode_revision": str(expected_revision),
            "state": "SET",
        },
    )
    context.store.commit_rolling_freshness_fence(
        RollingFreshnessFenceChange(
            RollingFreshnessFence(
                migration_id=migration_id,
                owner=context.principal,
                set_at=recorded_at,
                revision=expected_revision,
            ),
            receipt,
            expected_revision,
        )
    )
    readback = _validated_fence_readback(context, migration_id)
    _print(
        {
            "command": str(arguments.command),
            "replay": False,
            **_fence_view(context, readback),
        }
    )
    return 0


def _show_rolling_freshness_fence(context: _Context, arguments: argparse.Namespace) -> int:
    """Print the strong matching fence, receipt, and held GRAPH authority readback."""
    migration_id = str(arguments.migration_id)
    if _COMMAND_ID.fullmatch(migration_id) is None:
        raise RootRefusedError("SCHEMA_INVALID", "the migration id is malformed.")
    readback = _validated_fence_readback(context, migration_id)
    _print({"command": str(arguments.command), **_fence_view(context, readback)})
    return 0


@dataclass(frozen=True, slots=True)
class _Context:
    """The verified session, store and human principal one command runs with."""

    session: boto3.Session
    store: ExecutionGraphStore
    principal: str
    table: str | None = None


def _admit(context: _Context, arguments: argparse.Namespace) -> int:
    session, store, principal = context.session, context.store, context.principal
    verify_named_raw_bucket(
        NamedRawResourceRequest(
            environment=str(arguments.env),
            stack_name=str(arguments.raw_stack),
            bucket=str(arguments.bucket),
            cloudformation=session.client("cloudformation"),
            ssm=session.client("ssm"),
        )
    )
    request = RunRequest(
        request_id=str(arguments.request_id),
        protocol_version=str(arguments.protocol_version),
        caller_kind="on_demand",
        reason=str(arguments.reason),
        manifest_key=str(arguments.manifest_key),
        version_id=str(arguments.version_id),
        requester=principal,
        requester_is_iam=True,
        label=None if arguments.label is None else str(arguments.label),
    )
    receipt = admit_run_request(request, admission_dependencies(session, store, arguments))
    _print({"command": "admit", "receipt": asdict(receipt)})
    return 0


def admission_dependencies(
    session: boto3.Session, store: ExecutionGraphStore, arguments: argparse.Namespace
) -> AdmissionDependencies:
    """Return the admission dependencies the probe twin also uses (`land_fixtures/twin.py`).

    Epoch 1 (`GRAPH`) reads row 9's legacy-completed plan and the legacy head of newer
    retrievals, so `legacy` is always wired; without it every GRAPH admit raises
    `LegacyLookupUnavailableError` before deciding (P6E-8).
    """
    s3, dynamodb = session.client("s3"), session.client("dynamodb")
    resources = resolve_named_semantic_resources(
        str(arguments.env), session.client("cloudformation"), session.client("ssm")
    )
    reader = Stage1WriterAuthorityReader(resources["publications"], resources["leases"], dynamodb)
    return AdmissionDependencies(
        store=store,
        manifests=S3RunRequestManifests(str(arguments.bucket), s3),
        now=lambda: datetime.now(UTC),
        legacy=S3CoordinatorLegacyHead(str(arguments.bucket), s3, reader),
    )


def _run(arguments: argparse.Namespace) -> int:  # noqa: PLR0911 -- one return per command
    if re.fullmatch(r"\d{12}", str(arguments.expected_account)) is None:
        raise RootRefusedError("SCHEMA_INVALID", "the expected account must be 12 digits.")
    if str(arguments.profile).casefold() == "default":
        raise RootRefusedError("SCHEMA_INVALID", "an explicit nondefault profile is required.")
    session = boto3.Session(profile_name=str(arguments.profile), region_name=str(arguments.region))
    identity: dict[str, Any] = dict(session.client("sts").get_caller_identity())
    if str(identity.get("Account", "")) != str(arguments.expected_account):
        raise PermissionError("caller account does not match the explicitly expected account")
    principal = str(identity.get("Arn", ""))
    command = str(arguments.command)
    if command in _DEAD_LETTER_COMMANDS:
        return _dead_letters(session, principal, arguments)
    table = _resolve_table(session, arguments)
    store = DynamoExecutionGraphStore(table, session.client("dynamodb"))
    context = _Context(session, store, principal, table)
    if command == "show-mode":
        mode = store.read_mode()
        _print({"command": command, "mode": mode.mode.value, "revision": mode.revision})
        return 0
    if command == "show-rolling-freshness-fence":
        return _show_rolling_freshness_fence(context, arguments)
    if command == "show-receipt":
        found = store.read_receipt(str(arguments.request_id))
        _print({"command": command, "receipt": None if found is None else asdict(found)})
        return 0
    if command in _CUTOVER_READS:
        return _cutover_read(context, arguments)
    require_human_principal(principal)
    if command == "set-rolling-freshness-fence":
        return _set_rolling_freshness_fence(context, arguments)
    if command == "set-mode":
        return _set_mode(context, arguments)
    if command == "enter-graph":
        return _enter_graph(context, arguments)
    if command in _RUN_COMMANDS:
        dispatch = DynamoGraphDispatchStore(table, session.client("dynamodb"))
        return _run_command(dispatch, principal, arguments)
    return _admit(context, arguments)


_CUTOVER_READS: Final = frozenset({"drain-status", "enumerate"})
_DEAD_LETTER_COMMANDS: Final = frozenset({"dlq-status", "redrive"})


def _dead_letters(session: boto3.Session, principal: str, arguments: argparse.Namespace) -> int:
    """S6 part 2 (§2.12): `dlq-status` reads; `redrive` moves, for a human principal only."""
    sqs = session.client("sqs")
    if str(arguments.command) == "dlq-status":
        _print(dlq_status(sqs, str(arguments.env)))
        return 0
    require_human_principal(principal)
    route = dead_letter_route(str(arguments.env), str(arguments.queue))
    _print(redrive(sqs, route, int(arguments.expected_count)))
    return 0


def _families(context: _Context, arguments: argparse.Namespace) -> DynamoLegacyFamilies:
    """Resolve the coordinator table the zero proof reads; the named stack must own it."""
    table, stack = arguments.coordinator_table, arguments.coordinator_stack
    if not table or not stack:
        raise RootRefusedError(
            "SCHEMA_INVALID", "the cutover needs --coordinator-table and --coordinator-stack."
        )
    owned = any(
        (item.get("ResourceType"), item.get("PhysicalResourceId"))
        == ("AWS::DynamoDB::Table", str(table))
        for item in stack_resource_summaries(context.session.client("cloudformation"), str(stack))
    )
    if not owned:
        raise PermissionError("the named coordinator stack does not own the coordinator table.")
    return DynamoLegacyFamilies(str(table), context.session.client("dynamodb"))


def _cutover_deps(context: _Context, arguments: argparse.Namespace) -> CutoverDependencies:
    return CutoverDependencies(
        context.store, _families(context, arguments), lambda: datetime.now(UTC)
    )


def _proof_view(proof: DrainProof) -> dict[str, object]:
    return {
        "mode": proof.mode.mode.value,
        "mode_revision": proof.mode.revision,
        "drain_entered_at": proof.mode.drain_entered_at,
        "drain_deadline": proof.mode.drain_deadline,
        "started_at": proof.started_at,
        "drain_seq_before": proof.drain_seq_before,
        "drain_seq_after": proof.drain_seq_after,
        "stable": proof.stable,
        **_enumeration_view(proof.enumeration),
    }


def _enumeration_view(enumeration: FamilyEnumeration) -> dict[str, object]:
    return {
        "families": enumeration.counts.as_dict(),
        "zero": enumeration.counts.zero,
        "items_read": enumeration.items_read,
        "pages": enumeration.pages,
        "open_keys_sample": {name: list(keys) for name, keys in enumeration.open_keys.items()},
    }


def _cutover_read(context: _Context, arguments: argparse.Namespace) -> int:
    """`drain-status` (mode, `DRAIN_SEQ`, bounds) or `enumerate` (one F1-F4 enumeration).

    `enumerate` is counted in `LEGACY_DRAINING`. In `GRAPH` it is the uncounted post-CAS
    re-read (P6(d')), and a non-zero read exits 3: a stop with the hold kept set.
    """
    deps = _cutover_deps(context, arguments)
    if str(arguments.command) == "enumerate":
        if deps.store.read_mode().mode is AuthorityMode.GRAPH:
            read = post_graph_enumeration(deps)
            _print(
                {
                    "command": "enumerate",
                    "counted": False,
                    "mode": read.mode.mode.value,
                    "mode_revision": read.mode.revision,
                    "graph_entered_at": read.mode.graph_entered_at,
                    "hold": read.mode.hold,
                    "started_at": read.started_at,
                    **_enumeration_view(read.enumeration),
                }
            )
            return 0 if read.enumeration.counts.zero else REFUSED_EXIT
        _print({"command": "enumerate", "counted": True, **_proof_view(counted_enumeration(deps))})
        return 0
    mode = deps.store.read_mode()
    now = deps.now()
    earliest, latest = drain_deadline_bounds(now)
    fence = None
    if mode.drain_entered_at is not None:
        fence = (
            datetime.fromisoformat(mode.drain_entered_at)
            + timedelta(seconds=T_MAX_SECONDS + FENCE_MARGIN_SECONDS)
        ).isoformat()
    _print(
        {
            "command": "drain-status",
            "mode": mode.mode.value,
            "revision": mode.revision,
            "graph_entered_at": mode.graph_entered_at,
            "hold": mode.hold,
            "drain_entered_at": mode.drain_entered_at,
            "drain_deadline": mode.drain_deadline,
            "enumeration_permitted_from": fence,
            "drain_seq": deps.families.read_drain_seq(),
            "t_max_seconds": T_MAX_SECONDS,
            "fence_margin_seconds": FENCE_MARGIN_SECONDS,
            "deadline_bounds_if_drained_now": [earliest.isoformat(), latest.isoformat()],
        }
    )
    return 0


def _cutover_set_mode(
    context: _Context, arguments: argparse.Namespace, to_mode: AuthorityMode
) -> int:
    """Commit the drain CAS (with `D`) or the abort; both are validated in `cutover.py`."""
    deps = _cutover_deps(context, arguments)
    command = CutoverCommand(
        context.principal, str(arguments.command_id), int(arguments.expected_revision)
    )
    if to_mode is AuthorityMode.LEGACY_DRAINING:
        if not arguments.drain_deadline:
            raise RootRefusedError("SCHEMA_INVALID", "the drain CAS needs --drain-deadline.")
        deadline = datetime.fromisoformat(str(arguments.drain_deadline).replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            raise RootRefusedError("SCHEMA_INVALID", "the drain deadline must carry a UTC offset.")
        receipt = begin_drain(deps, command, deadline)
    else:
        receipt = abort_drain(deps, command)
    after = deps.store.read_mode()
    _print(
        {
            "command": "set_mode",
            "receipt": asdict(receipt),
            "mode": after.mode.value,
            "revision": after.revision,
            "drain_deadline": after.drain_deadline,
            "drain_seq": deps.families.read_drain_seq(),
        }
    )
    return 0


def _enter_graph(context: _Context, arguments: argparse.Namespace) -> int:
    """P6(c)-(d'): the zero proof, the `GRAPH` CAS bound to `DRAIN_SEQ`, the post-CAS read."""
    if _COMMAND_ID.fullmatch(str(arguments.command_id)) is None:
        raise RootRefusedError("SCHEMA_INVALID", "the command id is malformed.")
    require_protocol_v1(str(arguments.protocol_version))
    deps = _cutover_deps(context, arguments)
    result = enter_graph(
        deps,
        CutoverCommand(
            context.principal, str(arguments.command_id), int(arguments.expected_revision)
        ),
    )
    post = result.post_enumeration
    view: dict[str, object] = {
        "command": "enter_graph",
        "committed": True,
        "receipt": asdict(result.receipt),
        "proof": _proof_view(result.proof),
        "post_cas": None if post is None else _enumeration_view(post),
        "post_cas_zero": None if post is None else post.counts.zero,
        "post_cas_unread": result.post_unread,
    }
    # The CAS has committed: a failed mode read-back must not hide its receipt.
    try:
        after = deps.store.read_mode()
    # error-propagation: operator CLI; the receipt prints with mode_unread naming it, and exit 1
    except Exception as error:
        view["mode_unread"] = type(error).__name__
        _print(view)
        return 1
    view |= {
        "mode": after.mode.value,
        "revision": after.revision,
        "graph_entered_at": after.graph_entered_at,
        "hold": after.hold,
    }
    _print(view)
    # An unread post-CAS enumeration exits 1 (re-read it with `enumerate`); a non-zero one is a
    # stop with the hold kept set (P6(d')).
    if post is None:
        return 1
    return 0 if post.counts.zero else REFUSED_EXIT


_RUN_COMMANDS: Final = {
    "pause": RunCommandKind.PAUSE,
    "resume": RunCommandKind.RESUME,
    "cancel": RunCommandKind.CANCEL,
    "set-hold": RunCommandKind.SET_HOLD,
    "clear-hold": RunCommandKind.CLEAR_HOLD,
}


def _run_command(
    store: DynamoGraphDispatchStore, principal: str, arguments: argparse.Namespace
) -> int:
    """S3: pause, resume, cancel and the hold; one command receipt each (V-14, V-29)."""
    if _COMMAND_ID.fullmatch(str(arguments.command_id)) is None:
        raise RootRefusedError("SCHEMA_INVALID", "the command id is malformed.")
    require_protocol_v1(str(arguments.protocol_version))
    result = run_operator_command(
        store,
        OperatorCommand(
            kind=_RUN_COMMANDS[str(arguments.command)],
            command_id=str(arguments.command_id),
            principal=principal,
            now=lambda: datetime.now(UTC).isoformat(),
            run_id=getattr(arguments, "run_id", None),
        ),
    )
    _print(
        {
            "command": str(arguments.command),
            "receipt": asdict(result.receipt),
            "committed": result.committed,
            **({"refused": result.details["signal"]} if "signal" in result.details else {}),
        }
    )
    return 0 if result.committed else REFUSED_EXIT


def main(argv: Sequence[str] | None = None) -> int:
    """Run one operator command; a pre-domain refusal exits 3 and prints its signal."""
    arguments = _parser().parse_args(argv)
    try:
        status = _run(arguments)
    except (GraphCommandRefusedError, RootRefusedError) as error:
        _print({"command": arguments.command, "refused": error.signal})
        report_operation(
            OperationLog(
                operation=_OPERATION,
                component=_COMPONENT,
                phase="terminal",
                outcome="refused",
                severity=LogSeverity.WARNING,
                details={"command": str(arguments.command), "signal": error.signal},
            ),
            LOGGER,
        )
        return REFUSED_EXIT
    except AdmissionConflictError as error:
        _print({"command": arguments.command, "refused": error.signal})
        return REFUSED_EXIT
    except Exception as error:
        report_error(
            error,
            Report(operation=_OPERATION, correlation_id=_correlation(arguments), logger=LOGGER),
        )
        return 1
    finally:
        flush_metrics()
    report_operation(
        OperationLog(
            operation=_OPERATION,
            component=_COMPONENT,
            phase="terminal",
            outcome="completed",
            severity=LogSeverity.INFO,
            details={"command": str(arguments.command)},
        ),
        LOGGER,
    )
    return status


if __name__ == "__main__":
    sys.exit(main())
