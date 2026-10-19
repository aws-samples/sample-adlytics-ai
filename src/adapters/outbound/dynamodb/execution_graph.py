# SPDX-License-Identifier: MIT-0
"""DynamoDB adapter for the retained execution-graph store (S1; BF-10, V-5, V-7, BF-7).

One table, keyed `pk`/`sk`. Every read is strongly consistent. Every admission decision is one
`TransactWriteItems`: the receipt (`attribute_not_exists`) plus its effect, each conditioned on
the state the decision read. A cancelled transaction whose reasons are all condition failures is
a domain guard (`AdmissionConflictError`, no receipt); any other failure is
`RunRequestNotRecordedError`,
and nothing committed.

Items: `MODE#ingestion/MODE`, `RECEIPT#<request_id>/RECEIPT` (and `/SUPERSEDED`, and row 2's
`/CONFLICT#<request_digest>`),
`COMMAND#<id>/COMMAND`, `GRAPH#<graph_id>/GRAPH`, `RUN#<run_id>/RUN`,
`FINGERPRINT#<epoch>#<F>/FINGERPRINT`, `SLOT#<U>/SLOT`, `HEAD#<U>/HEAD`, `BLOCK#<U>/BLOCK`, and
in the fixed due shards `DUE#<n>`: `WAITER#<U>`, `INSTANTIATE#<run_id>`, `PROMOTE#<U>`.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, cast

from botocore.exceptions import BotoCoreError, ClientError

from adapters.outbound.dynamodb.items import write_bool, write_int, write_text
from domain.operations.cutover import (
    DRAIN_SEQ_CHANGED,
    DRAIN_SEQ_PARTITION_KEY,
    DRAIN_SEQ_RECORD_KEY,
)
from domain.operations.execution_graph import (
    MIGRATION_FENCE_SET,
    MODE_REVISION_CHANGED,
    AuthorityMode,
    ModeState,
    graph_id,
)
from domain.operations.execution_graph_release import (
    CONFLICT_DOMAIN_BLOCKED,
    INGESTION_RELEASE_DOMAIN,
)
from domain.operations.generated_execution_graphs import BOUNDS, STORE
from domain.operations.run_request import (
    ACTIVE_COMPUTE_NOT_REPLACEABLE,
    ACTIVE_RUN_GRANTED,
    RUN_REQUEST_WAIT_SLOT_CONFLICT,
    SLOT_REVISION_CHANGED,
    ActiveRun,
    FingerprintMatch,
    MatchState,
    RunState,
    Waiter,
)
from ports.outbound.execution_graph import (
    AdmissionCommit,
    AdmissionConflictError,
    CommandReceipt,
    EffectKind,
    GraphEntry,
    ModeChange,
    ReceiptRecord,
    RollingFreshnessFence,
    RollingFreshnessFenceChange,
    RollingFreshnessFenceRead,
    RunRequestNotRecordedError,
    UnitState,
    conflict_receipt_sk,
)

if TYPE_CHECKING:
    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_dynamodb.type_defs import (
        AttributeValueTypeDef,
        TransactWriteItemTypeDef,
    )

__all__ = [
    "MIGRATION_FENCE_PK",
    "MIGRATION_FENCE_SK",
    "DynamoExecutionGraphStore",
    "due_shard",
    "mode_state_from_item",
    "root_clock_attribute",
]

_MODE_PK: Final = str(STORE["mode_key"]["pk"])
_MODE_SK: Final = str(STORE["mode_key"]["sk"])
MIGRATION_FENCE_PK: Final = "MIGRATION#GRAPH_ROLLING_FRESHNESS"
MIGRATION_FENCE_SK: Final = "FENCE"
_SHARDS: Final = int(BOUNDS["due_shards"])
_CANCELLED: Final = "TransactionCanceledException"
_DRAIN_MEMBER: Final = 2
"""The `GRAPH` CAS's members: mode update, command receipt, then the `DRAIN_SEQ` check."""
_RECEIPT_EXISTS: Final = "RECEIPT_EXISTS"
_STATE_CHANGED: Final = "ADMISSION_STATE_CHANGED"
_RUN_MATCH: Final = {
    RunState.GRANTED: MatchState.GRANTED,
    RunState.SUCCEEDED: MatchState.SUCCEEDED,
    RunState.FAILED: MatchState.FAILED,
    RunState.SUPERSEDED: MatchState.SUPERSEDED,
    RunState.CANCELLED: MatchState.CANCELLED,
    RunState.FENCING: MatchState.FENCING,
}

type Item = dict[str, AttributeValueTypeDef]


def due_shard(conflict_unit: str) -> str:
    """Return the fixed base-table shard a unit's due obligations live in."""
    return f"DUE#{int(conflict_unit[:8], 16) % _SHARDS}"


def _key(pk: str, sk: str) -> Item:
    return {"pk": write_text(pk), "sk": write_text(sk)}


def _text(item: Item, name: str) -> str | None:
    value = item.get(name, {}).get("S")
    return None if value is None else str(value)


def _int(item: Item, name: str) -> int:
    value = item.get(name, {}).get("N")
    return 0 if value is None else int(str(value))


def _optional(values: dict[str, str | int | None]) -> Item:
    item: Item = {}
    for name, value in values.items():
        if value is None:
            continue
        item[name] = write_int(value) if isinstance(value, int) else write_text(value)
    return item


def mode_state_from_item(item: Item | None) -> ModeState:
    """Parse the mode item, the one shared reading; absent is `LEGACY` at revision 0."""
    if item is None:
        return ModeState(AuthorityMode.LEGACY, 0)
    return ModeState(
        AuthorityMode(str(_text(item, "mode"))),
        _int(item, "revision"),
        _text(item, "graph_entered_at"),
        hold=bool(item.get("hold", {}).get("BOOL", False)),
        hold_revision=_int(item, "hold_revision"),
        hold_cleared_at=_text(item, "hold_cleared_at"),
        drain_entered_at=_text(item, "drain_entered_at"),
        drain_deadline=_text(item, "drain_deadline"),
    )


def rolling_freshness_fence_from_item(item: Item) -> RollingFreshnessFence:
    """Parse the retained one-time migration fence."""
    return RollingFreshnessFence(
        migration_id=str(_text(item, "migration_id")),
        owner=str(_text(item, "owner")),
        set_at=str(_text(item, "set_at")),
        revision=_int(item, "revision"),
        state=str(_text(item, "state")),
    )


def command_receipt_from_item(command_id: str, item: Item) -> CommandReceipt:
    """Parse one immutable operator command receipt."""
    details = item.get("details", {}).get("M", {})
    return CommandReceipt(
        command_id=command_id,
        command=str(_text(item, "command")),
        principal=str(_text(item, "principal")),
        authority_epoch=_int(item, "authority_epoch"),
        mode=AuthorityMode(str(_text(item, "mode"))),
        outcome=str(_text(item, "outcome")),
        recorded_at=str(_text(item, "recorded_at")),
        protocol_version=str(_text(item, "protocol_version")),
        details={str(name): str(value.get("S", "")) for name, value in details.items()},
    )


def root_clock_attribute(root_clock_started_at: str | None) -> Item:
    """S5: the root clock a flow obligation is due from, when the writer knows it (FR-7 kept rule).

    The stuck-flow detector computes `due = root_clock_started_at + ceiling(v1)`. An obligation
    written without one is counted apart, never given a guessed due time.
    """
    if root_clock_started_at is None:
        return {}
    return {"root_clock_started_at": write_text(root_clock_started_at)}


def receipt_item(receipt: ReceiptRecord, sk: str = "RECEIPT") -> Item:
    """Render one immutable receipt."""
    return {
        **_key(f"RECEIPT#{receipt.request_id}", sk),
        "item_kind": write_text("receipt"),
        **_optional(
            {
                "request_id": receipt.request_id,
                "request_digest": receipt.request_digest,
                "decision": receipt.decision,
                "reason": receipt.reason,
                "row": receipt.row,
                "authority_epoch": receipt.authority_epoch,
                "mode": receipt.mode.value,
                "conflict_unit": receipt.conflict_unit,
                "input_revision": receipt.input_revision,
                "fingerprint": receipt.fingerprint,
                "recorded_at": receipt.recorded_at,
                "protocol_version": receipt.protocol_version,
                "contract_revision": receipt.contract_revision,
                "run_id": receipt.run_id,
                "graph_id": receipt.graph_id,
                "root_flow_id": receipt.root_flow_id,
                "retry_generation": receipt.retry_generation,
                "superseded_request_id": receipt.superseded_request_id,
                "legacy_plan_key": receipt.legacy_plan_key,
                "caller_kind": receipt.caller_kind,
                "caller_reason": receipt.caller_reason,
                "requester": receipt.requester,
                "joined_request_id": receipt.joined_request_id,
                "retried_run_id": receipt.retried_run_id,
                "root_clock_started_at": receipt.root_clock_started_at,
                "manifest_key": receipt.manifest_key,
                "version_id": receipt.version_id,
            }
        ),
    }


def read_receipt_item(item: Item) -> ReceiptRecord:
    """Parse one stored receipt."""
    row = item.get("row", {}).get("N")
    generation = item.get("retry_generation", {}).get("N")
    return ReceiptRecord(
        request_id=str(_text(item, "request_id")),
        request_digest=str(_text(item, "request_digest")),
        decision=str(_text(item, "decision")),
        reason=_text(item, "reason"),
        row=None if row is None else int(str(row)),
        authority_epoch=_int(item, "authority_epoch"),
        mode=AuthorityMode(str(_text(item, "mode"))),
        conflict_unit=str(_text(item, "conflict_unit")),
        input_revision=str(_text(item, "input_revision")),
        fingerprint=str(_text(item, "fingerprint")),
        recorded_at=str(_text(item, "recorded_at")),
        protocol_version=str(_text(item, "protocol_version")),
        contract_revision=_int(item, "contract_revision"),
        run_id=_text(item, "run_id"),
        graph_id=_text(item, "graph_id"),
        root_flow_id=_text(item, "root_flow_id"),
        retry_generation=None if generation is None else int(str(generation)),
        superseded_request_id=_text(item, "superseded_request_id"),
        legacy_plan_key=_text(item, "legacy_plan_key"),
        caller_kind=_text(item, "caller_kind"),
        caller_reason=_text(item, "caller_reason"),
        requester=_text(item, "requester"),
        joined_request_id=_text(item, "joined_request_id"),
        retried_run_id=_text(item, "retried_run_id"),
        root_clock_started_at=_text(item, "root_clock_started_at"),
        manifest_key=_text(item, "manifest_key"),
        version_id=_text(item, "version_id"),
    )


class DynamoExecutionGraphStore:
    """The live (or probe) execution-graph table."""

    def __init__(self, table: str, client: DynamoDBClient) -> None:
        self._table = table
        self._client = client

    def _get(self, pk: str, sk: str) -> Item | None:
        try:
            found = self._client.get_item(
                TableName=self._table, Key=_key(pk, sk), ConsistentRead=True
            )
        except (BotoCoreError, ClientError) as error:
            raise RunRequestNotRecordedError(
                f"execution-graph read failed: {type(error).__name__}"
            ) from None
        item = found.get("Item")
        return None if item is None else dict(item)

    def read_mode(self) -> ModeState:
        """Read the mode item; absent is `LEGACY` at revision 0."""
        return mode_state_from_item(self._get(_MODE_PK, _MODE_SK))

    def read_rolling_freshness_fence(self, migration_id: str) -> RollingFreshnessFenceRead:
        """Strongly read the mode, migration fence, and its named command receipt."""
        fence_item = self._get(MIGRATION_FENCE_PK, MIGRATION_FENCE_SK)
        command = self._get(f"COMMAND#{migration_id}", "COMMAND")
        return RollingFreshnessFenceRead(
            mode=self.read_mode(),
            fence=(None if fence_item is None else rolling_freshness_fence_from_item(fence_item)),
            receipt=(None if command is None else command_receipt_from_item(migration_id, command)),
        )

    def commit_rolling_freshness_fence(self, change: RollingFreshnessFenceChange) -> None:
        """Set the one-time fence and receipt under held GRAPH revision conditioning."""
        fence, receipt = change.fence, change.receipt
        if (
            fence.state != "SET"
            or fence.migration_id != receipt.command_id
            or fence.owner != receipt.principal
            or fence.set_at != receipt.recorded_at
            or fence.revision != change.expected_mode_revision
            or receipt.command != "set_rolling_freshness_fence"
            or receipt.mode is not AuthorityMode.GRAPH
        ):
            raise RunRequestNotRecordedError("the migration fence change is inconsistent.")
        mode_check = _check(
            self._table,
            _key(_MODE_PK, _MODE_SK),
            "#mode = :graph AND revision = :revision AND #hold = :held",
            graph=write_text(AuthorityMode.GRAPH.value),
            revision=write_int(change.expected_mode_revision),
            held=write_bool(True),
        )
        cast("dict[str, Any]", mode_check)["ConditionCheck"]["ExpressionAttributeNames"] = {
            "#mode": "mode",
            "#hold": "hold",
        }
        items = [
            mode_check,
            _put(
                self._table,
                {
                    **_key(MIGRATION_FENCE_PK, MIGRATION_FENCE_SK),
                    "item_kind": write_text("migration_fence"),
                    "state": write_text(fence.state),
                    "migration_id": write_text(fence.migration_id),
                    "owner": write_text(fence.owner),
                    "set_at": write_text(fence.set_at),
                    "revision": write_int(fence.revision),
                },
            ),
            _put(self._table, command_item(receipt)),
        ]
        try:
            self._client.transact_write_items(TransactItems=items)
        except ClientError as error:
            response = cast("dict[str, Any]", error.response)
            if response.get("Error", {}).get("Code") == _CANCELLED:
                codes = [
                    str(dict(reason).get("Code", "None"))
                    for reason in response.get("CancellationReasons", [])
                ]
                failed = [code for code in codes if code not in {"None", ""}]
                if failed and all(code == "ConditionalCheckFailed" for code in failed):
                    signal = (
                        MODE_REVISION_CHANGED
                        if codes[0] == "ConditionalCheckFailed"
                        else MIGRATION_FENCE_SET
                    )
                    raise AdmissionConflictError(signal) from None
            raise RunRequestNotRecordedError(
                f"the migration fence transaction failed: {response.get('Error', {}).get('Code')}"
            ) from None
        except BotoCoreError as error:
            raise RunRequestNotRecordedError(
                f"the migration fence transaction failed: {type(error).__name__}"
            ) from None

    def read_receipt(self, request_id: str) -> ReceiptRecord | None:
        """Read one decision receipt."""
        item = self._get(f"RECEIPT#{request_id}", "RECEIPT")
        return None if item is None else read_receipt_item(item)

    def read_conflict_receipt(self, request_id: str, request_digest: str) -> ReceiptRecord | None:
        """Read row 2's receipt for this request ID and conflicting digest."""
        item = self._get(f"RECEIPT#{request_id}", conflict_receipt_sk(request_digest))
        return None if item is None else read_receipt_item(item)

    def read_unit(self, conflict_unit: str, epoch: int) -> UnitState:
        """Read `U`'s slot and its run, waiter, head and block, strongly."""
        slot = self._get(f"SLOT#{conflict_unit}", "SLOT")
        active: ActiveRun | None = None
        revision = 0 if slot is None else _int(slot, "revision")
        run_name = None if slot is None else _text(slot, "active_run_id")
        if run_name is not None:
            run = self._get(f"RUN#{run_name}", "RUN")
            if run is None:
                raise RunRequestNotRecordedError("a slot names a run that does not exist.")
            active = ActiveRun(
                run_id=run_name,
                state=RunState(str(_text(run, "state"))),
                fingerprint=str(_text(run, "fingerprint")),
                r=datetime.fromisoformat(str(_text(run, "retrieved_at"))),
                compute_started=_text(run, "compute_started_at") is not None,
                grant_id=_text(run, "grant_id"),
                slot_revision=revision,
                root_clock_started_at=_text(run, "root_clock_started_at"),
            )
        waiter_item = self._get(due_shard(conflict_unit), f"WAITER#{conflict_unit}")
        waiter = None
        if waiter_item is not None:
            waiter = Waiter(
                request_id=str(_text(waiter_item, "request_id")),
                fingerprint=str(_text(waiter_item, "fingerprint")),
                r=datetime.fromisoformat(str(_text(waiter_item, "retrieved_at"))),
                held=bool(waiter_item.get("held", {}).get("BOOL", False)),
                revision=_int(waiter_item, "revision"),
                window=_text(waiter_item, "window"),
                root_clock_started_at=_text(waiter_item, "root_clock_started_at"),
            )
        head_item = self._get(f"HEAD#{conflict_unit}", "HEAD")
        head = None if head_item is None else _text(head_item, "retrieved_at")
        # S4 (S1A-8 / R5-5): row 6 reads the co-release domain's block, not a per-window one.
        blocked = self._get(f"BLOCK#{INGESTION_RELEASE_DOMAIN}", "BLOCK") is not None
        del epoch
        return UnitState(
            active=active,
            waiter=waiter,
            head=None if head is None else datetime.fromisoformat(head),
            blocked=blocked,
            slot_revision=revision,
        )

    def read_match(self, fingerprint: str, epoch: int) -> FingerprintMatch | None:
        """Read what the fingerprint names: a shadow graph in epoch 0, a run in epoch 1."""
        if epoch == 0:
            name = graph_id(fingerprint, 0, 0)
            graph = self._get(f"GRAPH#{name}", "GRAPH")
            return None if graph is None else FingerprintMatch(MatchState.SHADOW_RECORDED, name)
        index = self._get(f"FINGERPRINT#{epoch}#{fingerprint}", "FINGERPRINT")
        if index is None:
            return None
        run_name = str(_text(index, "run_id"))
        run = self._get(f"RUN#{run_name}", "RUN")
        if run is None:
            raise RunRequestNotRecordedError("a fingerprint names a run that does not exist.")
        state = RunState(str(_text(run, "state")))
        if state not in _RUN_MATCH:
            return None
        return FingerprintMatch(
            _RUN_MATCH[state],
            run_name,
            _int(run, "generation"),
            issued_grant=_text(run, "grant_id") is not None,
        )

    def _transact(self, items: list[TransactWriteItemTypeDef]) -> None:
        try:
            self._client.transact_write_items(TransactItems=items)
        except ClientError as error:
            response = cast("dict[str, Any]", error.response)
            if response.get("Error", {}).get("Code") == _CANCELLED:
                raw = [dict(r) for r in response.get("CancellationReasons", [])]
                reasons = [str(r.get("Code", "None")) for r in raw]
                failed = [code for code in reasons if code not in {"None", ""}]
                if failed and all(code == "ConditionalCheckFailed" for code in failed):
                    raise AdmissionConflictError(failed_member_signal(items, raw)) from None
            raise RunRequestNotRecordedError(
                f"the receipt transaction failed: {response.get('Error', {}).get('Code')}"
            ) from None
        except BotoCoreError as error:
            raise RunRequestNotRecordedError(
                f"the receipt transaction failed: {type(error).__name__}"
            ) from None

    def commit_admission(self, commit: AdmissionCommit) -> None:
        """Commit the receipt with its effect in one transaction (BF-10)."""
        self._transact(build_admission_transaction(self._table, commit))

    def commit_mode(self, change: ModeChange) -> None:
        """CAS the mode item and write its command receipt in one transaction.

        A failed `DRAIN_SEQ` check (the `GRAPH` CAS's third member) raises
        `AdmissionConflictError(DRAIN_SEQ_CHANGED)`; a failed mode condition raises it with
        `MODE_REVISION_CHANGED`. Nothing commits in either case.
        """
        items = build_mode_transaction(self._table, change)
        try:
            self._client.transact_write_items(TransactItems=items)
        except ClientError as error:
            response = cast("dict[str, Any]", error.response)
            if response.get("Error", {}).get("Code") == _CANCELLED:
                codes = [
                    str(dict(reason).get("Code", "None"))
                    for reason in response.get("CancellationReasons", [])
                ]
                failed = [code for code in codes if code not in {"None", ""}]
                if failed and all(code == "ConditionalCheckFailed" for code in failed):
                    drain = len(codes) > _DRAIN_MEMBER and codes[_DRAIN_MEMBER] == (
                        "ConditionalCheckFailed"
                    )
                    raise AdmissionConflictError(
                        DRAIN_SEQ_CHANGED if drain else MODE_REVISION_CHANGED
                    ) from None
            raise RunRequestNotRecordedError(
                f"the mode transaction failed: {response.get('Error', {}).get('Code')}"
            ) from None
        except BotoCoreError as error:
            raise RunRequestNotRecordedError(
                f"the mode transaction failed: {type(error).__name__}"
            ) from None

    def record_command(self, receipt: CommandReceipt) -> None:
        """Write one refusal command receipt and nothing else."""
        self._transact([_put(self._table, command_item(receipt))])


def _member_key(member: TransactWriteItemTypeDef) -> tuple[str, str, str]:
    ((kind, body),) = cast("dict[str, dict[str, Any]]", member).items()
    key = body["Item"] if kind == "Put" else body["Key"]
    return kind, str(key["pk"]["S"]), str(key["sk"]["S"])


def failed_member_signal(  # noqa: PLR0911 -- one typed signal per member family
    items: list[TransactWriteItemTypeDef], reasons: list[dict[str, Any]]
) -> str:
    """Map the first failed member to its typed domain guard (S1A-10).

    The receipt member is first, so its failure is an exact replay race. A replaced run's
    `RUN#` update returns its old image: a `grant_id` is `ACTIVE_RUN_GRANTED` (V-29), a set
    `compute_started_at` is `ACTIVE_COMPUTE_NOT_REPLACEABLE` (BF-9). A `SLOT#` condition is
    `SLOT_REVISION_CHANGED` (row 19, V-20), and a `WAITER#` condition is the second-waiter
    guard `RUN_REQUEST_WAIT_SLOT_CONFLICT` (BF-7).
    """
    for index, reason in enumerate(reasons):
        if str(reason.get("Code", "None")) != "ConditionalCheckFailed":
            continue
        if index == 0:
            return _RECEIPT_EXISTS
        kind, pk, sk = _member_key(items[index])
        if pk.startswith("RUN#") and kind == "Update":
            old = cast("dict[str, Any]", reason.get("Item") or {})
            if "grant_id" in old:
                return ACTIVE_RUN_GRANTED
            if "compute_started_at" in old:
                return ACTIVE_COMPUTE_NOT_REPLACEABLE
            return _STATE_CHANGED
        if pk.startswith("SLOT#"):
            return SLOT_REVISION_CHANGED
        if sk.startswith("WAITER#"):
            return RUN_REQUEST_WAIT_SLOT_CONFLICT
        if pk.startswith("BLOCK#"):
            return CONFLICT_DOMAIN_BLOCKED
        return _STATE_CHANGED
    return _STATE_CHANGED


def _domain_not_blocked(table: str) -> TransactWriteItemTypeDef:
    """S4 (S1A-8): every run-making admission ConditionChecks the co-release domain's block."""
    return _check(
        table, _key(f"BLOCK#{INGESTION_RELEASE_DOMAIN}", "BLOCK"), "attribute_not_exists(pk)"
    )


def command_item(receipt: CommandReceipt) -> Item:
    """Render one immutable command receipt."""
    return {
        **_key(f"COMMAND#{receipt.command_id}", "COMMAND"),
        "item_kind": write_text("command_receipt"),
        "command": write_text(receipt.command),
        "principal": write_text(receipt.principal),
        "authority_epoch": write_int(receipt.authority_epoch),
        "mode": write_text(receipt.mode.value),
        "outcome": write_text(receipt.outcome),
        "recorded_at": write_text(receipt.recorded_at),
        "protocol_version": write_text(receipt.protocol_version),
        "details": {"M": {key: write_text(value) for key, value in receipt.details.items()}},
    }


def _put(
    table: str,
    item: Item,
    condition: str = "attribute_not_exists(pk)",
    **values: AttributeValueTypeDef,
) -> TransactWriteItemTypeDef:
    put: dict[str, Any] = {"TableName": table, "Item": item, "ConditionExpression": condition}
    if values:
        put["ExpressionAttributeValues"] = {f":{name}": value for name, value in values.items()}
    return cast("TransactWriteItemTypeDef", {"Put": put})


def _check(
    table: str, key: Item, condition: str, **values: AttributeValueTypeDef
) -> TransactWriteItemTypeDef:
    check: dict[str, Any] = {"TableName": table, "Key": key, "ConditionExpression": condition}
    if values:
        check["ExpressionAttributeValues"] = {f":{name}": value for name, value in values.items()}
    return cast("TransactWriteItemTypeDef", {"ConditionCheck": check})


def _update(  # noqa: PLR0913, PLR0917 -- one private transaction-member builder
    table: str,
    key: Item,
    update: str,
    condition: str | None,
    values: dict[str, AttributeValueTypeDef],
    names: dict[str, str] | None = None,
) -> TransactWriteItemTypeDef:
    member: dict[str, Any] = {
        "TableName": table,
        "Key": key,
        "UpdateExpression": update,
        "ExpressionAttributeValues": {f":{name}": value for name, value in values.items()},
    }
    if condition is not None:
        member["ConditionExpression"] = condition
    if names:
        member["ExpressionAttributeNames"] = names
    return cast("TransactWriteItemTypeDef", {"Update": member})


def _slot_condition(table: str, commit: AdmissionCommit) -> TransactWriteItemTypeDef:
    key = _key(f"SLOT#{commit.conflict_unit}", "SLOT")
    if commit.slot_revision == 0:
        return _check(table, key, "attribute_not_exists(pk)")
    return _check(table, key, "revision = :rev", rev=write_int(commit.slot_revision))


def _waiter_members(table: str, commit: AdmissionCommit) -> list[TransactWriteItemTypeDef]:
    old = commit.superseded_waiter
    if old is None:
        return []
    by = commit.receipt.request_id
    supersession = ReceiptRecord(
        request_id=old.request_id,
        request_digest="superseded",
        decision="SUPERSEDED",
        reason=f"by={by}",
        row=None,
        authority_epoch=commit.receipt.authority_epoch,
        mode=commit.receipt.mode,
        conflict_unit=commit.conflict_unit,
        input_revision=old.r.isoformat(),
        fingerprint=old.fingerprint,
        recorded_at=commit.receipt.recorded_at,
        protocol_version=commit.receipt.protocol_version,
        contract_revision=commit.receipt.contract_revision,
        superseded_request_id=by,
    )
    members = [_put(table, receipt_item(supersession, "SUPERSEDED"))]
    if commit.kind is not EffectKind.QUEUE_WAITER:
        members.append(
            cast(
                "TransactWriteItemTypeDef",
                {
                    "Delete": {
                        "TableName": table,
                        "Key": _key(
                            due_shard(commit.conflict_unit), f"WAITER#{commit.conflict_unit}"
                        ),
                        "ConditionExpression": "revision = :rev AND request_id = :rid",
                        "ExpressionAttributeValues": {
                            ":rev": write_int(old.revision),
                            ":rid": write_text(old.request_id),
                        },
                    }
                },
            )
        )
    return members


def _run_members(table: str, commit: AdmissionCommit) -> list[TransactWriteItemTypeDef]:
    run = commit.new_run
    if run is None:
        raise RunRequestNotRecordedError("a run-making decision carried no run.")
    receipt = commit.receipt
    epoch = receipt.authority_epoch
    run_item = {
        **_key(f"RUN#{run.run_id}", "RUN"),
        "item_kind": write_text("run"),
        "state": write_text(RunState.OPEN.value),
        "authority_epoch": write_int(epoch),
        "fingerprint": write_text(run.fingerprint),
        "conflict_unit": write_text(commit.conflict_unit),
        "retrieved_at": write_text(run.retrieved_at),
        "root_flow_id": write_text(run.root_flow_id),
        "generation": write_int(run.generation),
        "graph_id": write_text(run.graph_id),
        "template": write_text(run.template),
        "dispatch_generation": write_int(0),
        "revision": write_int(1),
        "receipt_request_id": write_text(receipt.request_id),
        "protocol_version": write_text(receipt.protocol_version),
    }
    if run.window is not None:
        run_item["window"] = write_text(run.window)
    run_item |= root_clock_attribute(run.root_clock_started_at)
    fingerprint_item = {
        **_key(f"FINGERPRINT#{epoch}#{run.fingerprint}", "FINGERPRINT"),
        "run_id": write_text(run.run_id),
        "generation": write_int(run.generation),
    }
    obligation = {
        **_key(due_shard(commit.conflict_unit), f"INSTANTIATE#{run.run_id}"),
        "item_kind": write_text("instantiate"),
        "authority_epoch": write_int(epoch),
        "run_id": write_text(run.run_id),
        "due_at": write_text(receipt.recorded_at),
        **root_clock_attribute(run.root_clock_started_at),
    }
    return [
        _put(table, run_item),
        _put(
            table,
            fingerprint_item,
            "attribute_not_exists(pk) OR generation < :gen",
            gen=write_int(run.generation),
        ),
        _put(table, obligation),
    ]


def build_admission_transaction(
    table: str, commit: AdmissionCommit
) -> list[TransactWriteItemTypeDef]:
    """Build the one admission transaction: the receipt first, then its conditioned effect."""
    members: list[TransactWriteItemTypeDef] = [
        _put(table, receipt_item(commit.receipt, commit.receipt_sk))
    ]
    kind = commit.kind
    if kind is EffectKind.SHADOW_GRAPH:
        graph = commit.shadow_graph
        if graph is None:
            raise RunRequestNotRecordedError("a shadow admission carried no graph.")
        members.append(
            _put(
                table,
                {
                    **_key(f"GRAPH#{graph.graph_id}", "GRAPH"),
                    "item_kind": write_text("graph"),
                    "state": write_text("SHADOW_RECORDED"),
                    "authority_epoch": write_int(0),
                    "template": write_text(graph.template),
                    "nodes": {"L": [write_text(node) for node in graph.nodes]},
                    "fingerprint": write_text(graph.fingerprint),
                    "root_flow_id": write_text(graph.root_flow_id),
                    "receipt_request_id": write_text(commit.receipt.request_id),
                },
            )
        )
    elif kind is EffectKind.ADMIT_RUN:
        run = commit.new_run
        assert run is not None  # noqa: S101 -- _run_members raises first
        members += _run_members(table, commit)
        slot_key = _key(f"SLOT#{commit.conflict_unit}", "SLOT")
        members.append(
            _update(
                table,
                slot_key,
                "SET active_run_id = :run, revision = :next, authority_epoch = :epoch",
                "attribute_not_exists(pk) "
                "OR (revision = :rev AND attribute_not_exists(active_run_id))",
                {
                    "run": write_text(run.run_id),
                    "next": write_int(commit.slot_revision + 1),
                    "rev": write_int(commit.slot_revision),
                    "epoch": write_int(commit.receipt.authority_epoch),
                },
            )
        )
        members += _waiter_members(table, commit)
        members.append(_domain_not_blocked(table))
    elif kind is EffectKind.REPLACE_RUN:
        run, replaced = commit.new_run, commit.replaced
        if run is None or replaced is None:
            raise RunRequestNotRecordedError("a replacing admission carried no run.")
        compute = (
            "attribute_exists(compute_started_at)"
            if replaced.invalidating
            else "attribute_not_exists(compute_started_at)"
        )
        replaced_member = _update(
            table,
            _key(f"RUN#{replaced.run_id}", "RUN"),
            "SET #state = :fencing, dispatch_generation = dispatch_generation + :one, "
            "revision = revision + :one, fence_reason = :reason, fenced_at = :at",
            f"#state = :open AND attribute_not_exists(grant_id) AND {compute}",
            {
                "fencing": write_text(RunState.FENCING.value),
                "open": write_text(RunState.OPEN.value),
                "one": write_int(1),
                "reason": write_text("input_invalidated" if replaced.invalidating else "replaced"),
                "at": write_text(commit.receipt.recorded_at),
            },
            {"#state": "state"},
        )
        # S1A-10: the old image tells a granted run (V-29) from a computing one (BF-9).
        cast("dict[str, dict[str, Any]]", replaced_member)["Update"][
            "ReturnValuesOnConditionCheckFailure"
        ] = "ALL_OLD"
        members.append(replaced_member)
        # S3: the replaced run is FENCING; the fence settler finds it through FENCE#.
        members.append(
            _put(
                table,
                {
                    **_key(due_shard(commit.conflict_unit), f"FENCE#{replaced.run_id}"),
                    "item_kind": write_text("fence"),
                    "authority_epoch": write_int(commit.receipt.authority_epoch),
                    "run_id": write_text(replaced.run_id),
                    "due_at": write_text(commit.receipt.recorded_at),
                    **root_clock_attribute(replaced.root_clock_started_at),
                },
            )
        )
        members += _run_members(table, commit)
        members.append(
            _update(
                table,
                _key(f"SLOT#{commit.conflict_unit}", "SLOT"),
                "SET active_run_id = :run, revision = :next",
                "active_run_id = :old AND revision = :rev",
                {
                    "run": write_text(run.run_id),
                    "old": write_text(replaced.run_id),
                    "next": write_int(commit.slot_revision + 1),
                    "rev": write_int(commit.slot_revision),
                },
            )
        )
        members += _waiter_members(table, commit)
        members.append(_domain_not_blocked(table))
    elif kind is EffectKind.QUEUE_WAITER:
        waiter = commit.new_waiter
        if waiter is None:
            raise RunRequestNotRecordedError("a queued decision carried no waiter.")
        old = commit.superseded_waiter
        item = {
            **_key(due_shard(commit.conflict_unit), f"WAITER#{commit.conflict_unit}"),
            "item_kind": write_text("waiter"),
            "authority_epoch": write_int(commit.receipt.authority_epoch),
            "request_id": write_text(waiter.request_id),
            "fingerprint": write_text(waiter.fingerprint),
            "retrieved_at": write_text(waiter.r.isoformat()),
            "held": write_bool(waiter.held),
            "revision": write_int(waiter.revision),
            "due_at": write_text(commit.receipt.recorded_at),
            **root_clock_attribute(waiter.root_clock_started_at),
        }
        if waiter.window is not None:
            item["window"] = write_text(waiter.window)
        if old is None:
            members.append(_put(table, item, "attribute_not_exists(sk)"))
        else:
            members.append(
                _put(
                    table,
                    item,
                    "revision = :rev AND request_id = :rid",
                    rev=write_int(old.revision),
                    rid=write_text(old.request_id),
                )
            )
        members.append(_slot_condition(table, commit))
        members += _waiter_members(table, commit)
        members.append(_domain_not_blocked(table))
        if commit.wake_held:
            members.append(
                _update(
                    table,
                    _key(due_shard(commit.conflict_unit), f"PROMOTE#{commit.conflict_unit}"),
                    "ADD wake_seq :one SET woken_at = :at, held = :held",
                    None,
                    {
                        "one": write_int(1),
                        "at": write_text(commit.receipt.recorded_at),
                        "held": write_bool(True),
                    },
                )
            )
    if len(members) > int(BOUNDS["max_transaction_items"]):
        raise RunRequestNotRecordedError("an admission transaction exceeds the generated bound.")
    return members


def build_mode_transaction(table: str, change: ModeChange) -> list[TransactWriteItemTypeDef]:
    """Build the mode CAS: the mode item at its read revision, and the command receipt."""
    expected = change.expected.revision
    condition = "attribute_not_exists(pk)" if expected == 0 else "revision = :rev AND #mode = :from"
    values: dict[str, AttributeValueTypeDef] = {
        "to": write_text(change.to_mode.value),
        "next": write_int(expected + 1),
    }
    if expected != 0:
        values["rev"] = write_int(expected)
        values["from"] = write_text(change.expected.mode.value)
    expression = "SET #mode = :to, revision = :next"
    from_mode, to_mode = change.expected.mode, change.to_mode
    extra: list[TransactWriteItemTypeDef] = []
    if to_mode is AuthorityMode.LEGACY_DRAINING:
        # S6 step 3: the drain CAS records when it committed and the operator's `D`.
        if change.drain_deadline is None:
            raise RunRequestNotRecordedError("the drain CAS needs its deadline.")
        expression += ", drain_entered_at = :at, drain_deadline = :deadline"
        values["at"] = write_text(change.receipt.recorded_at)
        values["deadline"] = write_text(change.drain_deadline)
    elif from_mode is AuthorityMode.LEGACY_DRAINING and to_mode is AuthorityMode.LEGACY:
        # V-18: the abort is legal only while `graph_entered_at` is absent.
        condition += " AND attribute_not_exists(graph_entered_at)"
        expression += " REMOVE drain_entered_at, drain_deadline"
    elif to_mode is AuthorityMode.GRAPH:
        # S6 step 6 (V-17b): the CAS, the hold and `graph_entered_at` in one transaction that
        # ConditionChecks the `DRAIN_SEQ` value the zero proof read.
        entry = change.graph_entry
        if entry is None:
            raise RunRequestNotRecordedError("the GRAPH CAS needs its zero-proof binding.")
        expression += ", graph_entered_at = :at, #hold = :held, hold_revision = :hold_next"
        values |= {
            "at": write_text(change.receipt.recorded_at),
            "held": write_bool(True),
            "hold_next": write_int(change.expected.hold_revision + 1),
        }
        condition += " AND attribute_not_exists(graph_entered_at)"
        if change.expected.hold_revision == 0:
            condition += " AND attribute_not_exists(hold_revision)"
        else:
            condition += " AND hold_revision = :hold_rev"
            values["hold_rev"] = write_int(change.expected.hold_revision)
        extra.append(drain_seq_check(entry))
    names = {"#mode": "mode"}
    if to_mode is AuthorityMode.GRAPH:
        names["#hold"] = "hold"
    return [
        _update(table, _key(_MODE_PK, _MODE_SK), expression, condition, values, names),
        _put(table, command_item(change.receipt)),
        *extra,
    ]


def drain_seq_check(entry: GraphEntry) -> TransactWriteItemTypeDef:
    """ConditionCheck `DRAIN_SEQ.seq` in the coordinator table equals the value the proof read."""
    key: Item = {
        "partition_key": write_text(DRAIN_SEQ_PARTITION_KEY),
        "record_key": write_text(DRAIN_SEQ_RECORD_KEY),
    }
    if entry.drain_seq == 0:
        return _check(entry.coordinator_table, key, "attribute_not_exists(partition_key)")
    return _check(entry.coordinator_table, key, "seq = :seq", seq=write_int(entry.drain_seq))
