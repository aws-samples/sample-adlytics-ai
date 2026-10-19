# SPDX-License-Identifier: MIT-0
"""Measure `FreshElectionTimingV1` and derive `next_election_min_remaining_ms` (plan item 12).

    uv run python validation/run_fresh_election_timing.py --profile <profile> --environment dev \
        --region <region> --expected-account <account> --graph-store-stack <store-stack> \
        --output <evidence-dir>/fresh-election-timing-v1.json [--samples 5] [--write-contract]

A non-clearing rehearsal of the held final runtime's fresh election, dev only. It runs the
promoter code at this worktree's clean HEAD from the operator host against the dev probe table
(`adlytics-<env>-execution-graph-probe`) under the probe role, never against the live table:

1. Strongly read the live `MODE#ingestion` (GRAPH revision 12 with the hold set, or refuse), the
   deployed Promoter's timeout (it must equal the contract's `worker_pass_seconds`), and every
   probe due shard (any serving `ARM#`, `COMPLETE#` or `COALESCE#` row there refuses the run, so
   the rehearsal can never elect or drain a row it did not seed).
2. Per sample, build the production rolling world over `FakeDynamo` (A drained to its final
   alias, B published while A was ACTIVE), seed its rows into the probe table with
   `attribute_not_exists` puts, and run `promoter.rehearse_fresh_election` with a real
   `DynamoGraphReleaseStore`: `max_redecide` injected conflicts, then one real fixed-member
   activation. Each attempt times its fresh ARM read, COMPLETE walk, candidate read,
   head/frontier read, activation, conflict retry and guarded total.
3. Dispose of every row the sample owns, and strongly prove each one absent. It owns a row it
   seeded, or one a write created after a strong preflight read found it absent and the write
   is known to have landed (`RecordingClient`); a row it does not own is never deleted. A
   collision, an occupied write target, a refused or failed attempt, an unresolved ambiguous
   write, or an incomplete disposal is fatal.

The guard is exactly `ceil(2 * max(total_ms))` over every attempt of every sample. An empty
sample set, or a guard at or above the Promoter timeout, is refused and writes no derivation.
`--write-contract` then rewrites only the contract's `next_election_min_remaining_ms` line and
its comment with the value, the artifact path and its SHA-256; regenerate both projections next.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

_ROOT: Final = Path(__file__).resolve().parents[1]
for _path in (_ROOT / "src", _ROOT / "tests" / "unit"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

SCHEMA: Final = "fresh-election-timing-v1"
DERIVATION: Final = "ceil(2 * max(total_ms))"
CONTRACT_PATH: Final = _ROOT / "contracts" / "operations" / "execution-graphs.yaml"
DEFAULT_SAMPLES: Final = 5
WORLD_A_SIZE: Final = 3
WORLD_B_SIZE: Final = 2
EXPECTED_MODE_REVISION: Final = 12
_ACCOUNT: Final = re.compile(r"^\d{12}$")
_SKIPPED_KEYS: Final = frozenset({("MODE#ingestion", "MODE")})
"""World rows never seeded: the rehearsal path reads no mode row, and the probe table owns its own."""
_OBLIGATION_PREFIXES: Final = ("ARM#", "COMPLETE#", "COALESCE#")
_GUARD_LINE: Final = re.compile(
    r"^(?P<comment>(?:  #[^\n]*\n)+)  next_election_min_remaining_ms: \d+\n", re.MULTILINE
)

type Key = tuple[str, str]
type Item = dict[str, Any]


class TimingError(RuntimeError):
    """The rehearsal cannot be proven; no derivation is written."""


# --- Pure parts (unit-tested in tests/unit/test_run_fresh_election_timing.py) ----------------


def derive_guard(totals: Sequence[float], promoter_timeout_ms: int) -> int:
    """Return `ceil(2 * max(total_ms))`, refusing an empty sample or a guard at the timeout."""
    if not totals:
        raise TimingError("the timing sample is empty")
    if any(not math.isfinite(value) or value <= 0 for value in totals):
        raise TimingError("every attempt total must be a positive finite duration")
    if isinstance(promoter_timeout_ms, bool) or promoter_timeout_ms <= 0:
        raise TimingError("the Promoter timeout must be a positive integer")
    guard = math.ceil(2 * max(totals))
    if guard >= promoter_timeout_ms:
        raise TimingError(
            f"the derived guard {guard} ms reaches the {promoter_timeout_ms} ms Promoter timeout"
        )
    return guard


def attempt_document(attempt: Any) -> dict[str, object]:
    """Render one `FreshElectionAttemptTiming` with every segment in milliseconds."""
    segments = cast("Mapping[str, float]", attempt.segments_ms)
    return {
        "attempt": attempt.attempt,
        "injected_conflict": attempt.injected_conflict,
        **{f"{name}_ms": round(float(segments[name]), 3) for name in sorted(segments)},
        "conflict_retry_ms": round(float(attempt.retry_ms), 3),
        "total_ms": round(float(attempt.total_ms), 3),
    }


def validate_samples(samples: Sequence[Mapping[str, object]], max_redecide: int) -> list[float]:
    """Require each sample to be the whole envelope; return every attempt total."""
    if not samples:
        raise TimingError("the timing sample is empty")
    totals: list[float] = []
    for sample in samples:
        attempts = cast("list[Mapping[str, object]]", sample.get("attempts") or [])
        flags = [item.get("injected_conflict") for item in attempts]
        if flags != [True] * max_redecide + [False]:
            raise TimingError(
                f"sample {sample.get('sample')} is not {max_redecide} conflicts then one activation"
            )
        if sample.get("outcome") not in {"elected", "no_eligible"} or not sample.get("transition"):
            raise TimingError(f"sample {sample.get('sample')} recorded no activation")
        disposition = cast("Mapping[str, object]", sample.get("disposition") or {})
        if disposition.get("remaining") != 0 or not disposition.get("removed"):
            raise TimingError(f"sample {sample.get('sample')} left probe rows behind")
        if disposition.get("unowned_kept"):
            raise TimingError(f"sample {sample.get('sample')} met probe rows it does not own")
        totals.extend(float(cast("float", item["total_ms"])) for item in attempts)
    return totals


def timing_document(
    identity: Mapping[str, object], samples: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    """Build the artifact and its derivation; any refusal is a `TimingError`."""
    max_redecide = int(cast("int", identity["max_redecide"]))
    timeout = int(cast("int", identity["promoter_timeout_ms"]))
    totals = validate_samples(samples, max_redecide)
    guard = derive_guard(totals, timeout)
    return {
        "schema_version": SCHEMA,
        **identity,
        "sample_count": len(samples),
        "samples": [dict(sample) for sample in samples],
        "max_total_ms": max(totals),
        "derivation": DERIVATION,
        "next_election_min_remaining_ms": guard,
        "passed": True,
    }


def contract_with_guard(text: str, guard: int, artifact: str, sha256: str) -> str:
    """Rewrite only the guard line and its comment; refuse a contract without exactly one."""
    if len(_GUARD_LINE.findall(text)) != 1:
        raise TimingError("the contract does not hold exactly one guard line")
    comment = (
        "  # Measured by FreshElectionTimingV1: ceil(2 * max(total_ms)) over every attempt of\n"
        f"  # every sample. Evidence: {artifact}\n"
        f"  # SHA-256 {sha256}.\n"
    )
    return _GUARD_LINE.sub(
        lambda _match: f"{comment}  next_election_min_remaining_ms: {guard}\n", text, count=1
    )


def repository_relative(path: Path) -> str:
    """Name the artifact from the repository's `.agents/` root, never by a home directory."""
    parts = path.parts
    if ".agents" not in parts:
        raise TimingError("the timing artifact must live under the repository's .agents/ tree")
    return Path(*parts[parts.index(".agents") :]).as_posix()


# --- Probe-table world: seed, record, dispose ------------------------------------------------


def _key_of(item: Mapping[str, Any]) -> Key:
    return (str(item["pk"]["S"]), str(item["sk"]["S"]))


_DEFINITE_REFUSALS: Final = frozenset(
    {"TransactionCanceledException", "ConditionalCheckFailedException", "ValidationException"}
)
"""Error codes DynamoDB returns only when it wrote nothing, so no target becomes owned."""


@dataclass(frozen=True, slots=True)
class _Write:
    """One write a request names: its kind, key, and (for a put) the exact image it carries."""

    kind: str
    key: Key
    image: Item | None


def _writes(members: Iterable[Mapping[str, Any]]) -> list[_Write]:
    """Return a transaction's put and update targets (checks and deletes create no row)."""
    found: list[_Write] = []
    for member in members:
        ((kind, body),) = member.items()
        if kind == "Put":
            found.append(_Write(kind, _key_of(body["Item"]), cast("Item", body["Item"])))
        elif kind == "Update":
            found.append(_Write(kind, _key_of(body["Key"]), None))
    return found


class ForeignRowError(TimingError):
    """A write would land on a probe row this rehearsal neither seeded nor created."""


@dataclass(slots=True)
class RecordingClient:
    """Delegate to the probe client, owning a key only once a write to it is known to land.

    Ownership is separate from intent. Before each write, every put or update target that the
    sample neither seeded nor already owns is strongly read; an occupied one refuses the request
    before it is sent (`ForeignRowError`), so the rehearsal never writes over a row it did not
    create. A target becomes owned only after the request succeeds. A definite refusal
    (`_DEFINITE_REFUSALS`) owns nothing. Any other failure is ambiguous, so each target is read
    back: absent stays unowned, a put whose stored image equals the exact image it sent is owned,
    and anything else is `unresolved` and is never deleted.
    """

    client: Any
    table: str
    seeded: frozenset[Key] = frozenset()
    owned: set[Key] = field(default_factory=set[Key])
    unresolved: set[Key] = field(default_factory=set[Key])
    refused: list[Key] = field(default_factory=list[Key])

    def transact_write_items(self, **kwargs: Any) -> Any:
        """Preflight the transaction's new targets, then own them only after it commits."""
        return self._write(
            _writes(kwargs["TransactItems"]), lambda: self.client.transact_write_items(**kwargs)
        )

    def put_item(self, **kwargs: Any) -> Any:
        """Preflight one put's target, then own it only after the put succeeds."""
        write = _Write("Put", _key_of(kwargs["Item"]), cast("Item", kwargs["Item"]))
        return self._write([write], lambda: self.client.put_item(**kwargs))

    def update_item(self, **kwargs: Any) -> Any:
        """Preflight one update's target, then own it only after the update succeeds."""
        write = _Write("Update", _key_of(kwargs["Key"]), None)
        return self._write([write], lambda: self.client.update_item(**kwargs))

    def _write(self, writes: Sequence[_Write], send: Callable[[], Any]) -> Any:
        from botocore.exceptions import ClientError

        fresh = [item for item in writes if item.key not in self.seeded | self.owned]
        occupied = [item.key for item in fresh if _strong_get(self.client, self.table, item.key)]
        if occupied:
            self.refused.extend(occupied)
            where = ", ".join(f"{key[0]}/{key[1]}" for key in occupied)
            raise ForeignRowError(f"the probe table already holds {where}; nothing was sent")
        try:
            response = send()
        except ClientError as error:
            if str(error.response.get("Error", {}).get("Code")) not in _DEFINITE_REFUSALS:
                self._resolve(fresh)
            raise
        except BaseException:
            self._resolve(fresh)
            raise
        self.owned.update(item.key for item in fresh)
        return response

    def _resolve(self, fresh: Sequence[_Write]) -> None:
        """After an ambiguous failure, own only a put whose stored image is exactly its own."""
        for item in fresh:
            stored = _strong_get(self.client, self.table, item.key)
            if stored is None:
                continue
            if item.image is not None and stored == item.image:
                self.owned.add(item.key)
            else:
                self.unresolved.add(item.key)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)


def _strong_get(client: Any, table: str, key: Key) -> Item | None:
    found = client.get_item(
        TableName=table, Key={"pk": {"S": key[0]}, "sk": {"S": key[1]}}, ConsistentRead=True
    )
    return cast("Item | None", found.get("Item"))


def foreign_obligations(client: Any, table: str, shards: Iterable[str]) -> list[Key]:
    """Every serving obligation already in the probe due shards (any one refuses the run)."""
    found: list[Key] = []
    for shard in shards:
        for prefix in _OBLIGATION_PREFIXES:
            arguments: dict[str, Any] = {
                "TableName": table,
                "KeyConditionExpression": "pk = :pk AND begins_with(sk, :prefix)",
                "ExpressionAttributeValues": {":pk": {"S": shard}, ":prefix": {"S": prefix}},
                "ConsistentRead": True,
            }
            while True:
                page = client.query(**arguments)
                found.extend(_key_of(item) for item in page.get("Items", ()))
                token = page.get("LastEvaluatedKey")
                if not token:
                    break
                arguments["ExclusiveStartKey"] = token
    return sorted(found)


def seed_world(client: Any, table: str, items: Mapping[Key, Item]) -> list[Key]:
    """Put each world row absent-conditioned; a collision stops and returns what was seeded."""
    from botocore.exceptions import ClientError

    seeded: list[Key] = []
    for key in sorted(items):
        if key in _SKIPPED_KEYS:
            continue
        try:
            client.put_item(
                TableName=table, Item=items[key], ConditionExpression="attribute_not_exists(pk)"
            )
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code"))
            raise _Collision(seeded, key, code) from None
        seeded.append(key)
    return seeded


class _Collision(TimingError):
    """Seeding stopped at one row; `seeded` holds every row it did put."""

    def __init__(self, seeded: list[Key], key: Key, code: str) -> None:
        where = f"{key[0]}/{key[1]}"
        super().__init__(
            f"the probe table already holds {where}"
            if code == "ConditionalCheckFailedException"
            else f"could not seed {where} ({code})"
        )
        self.seeded = seeded


def dispose(client: Any, table: str, keys: Iterable[Key]) -> dict[str, object]:
    """Delete every recorded key, then strongly prove each one absent."""
    ordered = sorted(set(keys))
    for key in ordered:
        client.delete_item(TableName=table, Key={"pk": {"S": key[0]}, "sk": {"S": key[1]}})
    remaining = [key for key in ordered if _strong_get(client, table, key) is not None]
    digest = hashlib.sha256(json.dumps(ordered, separators=(",", ":")).encode()).hexdigest()
    return {
        "removed": len(ordered) - len(remaining),
        "remaining": len(remaining),
        "remaining_keys": [list(key) for key in remaining],
        "keys_sha256": digest,
    }


@dataclass(frozen=True, slots=True)
class SampleInputs:
    """What one sample needs: the probe client and table, the envelope, and the clock."""

    client: Any
    table: str
    max_redecide: int
    remaining_ms: int
    clock: Callable[[], float] = time.perf_counter


def run_sample(index: int, inputs: SampleInputs) -> dict[str, object]:
    """Build, seed, rehearse, and dispose one world; any failure disposes before it raises."""
    from fixed_member_byte_fixture_support import build_world

    from adapters.jobs.execution_graph.fence_settler import due_shards
    from adapters.jobs.execution_graph.instantiate import ingestion_node_plan
    from adapters.jobs.execution_graph.promoter import (
        FreshElectionRehearsal,
        PromoterContext,
        rehearse_fresh_election,
    )
    from adapters.outbound.dynamodb.execution_graph_release import DynamoGraphReleaseStore
    from domain.operations.execution_graph_release import INGESTION_RELEASE_DOMAIN

    world = build_world(WORLD_A_SIZE, WORLD_B_SIZE)
    recording = RecordingClient(inputs.client, inputs.table)
    seeded: list[Key] = []
    rehearsal = FreshElectionRehearsal(conflicts=inputs.max_redecide, clock=inputs.clock)
    request_id = f"fresh-election-timing-{uuid.uuid4().hex[:16]}"
    started = datetime.now(UTC).isoformat()
    failure: BaseException | None = None
    result: Any = None
    try:
        seeded = seed_world(inputs.client, inputs.table, world.fake.items)
        recording.seeded = frozenset(seeded)
        store = DynamoGraphReleaseStore(inputs.table, recording)
        remaining = inputs.remaining_ms
        result = rehearse_fresh_election(
            PromoterContext(store, ingestion_node_plan, lambda: datetime.now(UTC)),
            due_shards(),
            INGESTION_RELEASE_DOMAIN,
            rehearsal,
            lambda: lambda: remaining,
            request_id,
        )
    except _Collision as collision:
        seeded, failure = collision.seeded, collision
    except Exception as error:  # noqa: BLE001 -- disposed first, then re-raised below
        failure = error
    disposition = dispose(inputs.client, inputs.table, [*seeded, *recording.owned])
    if failure is None and (recording.refused or recording.unresolved):
        failure = TimingError("a rehearsal write met a probe row it does not own")
    unowned = sorted({*recording.refused, *recording.unresolved})
    disposition["unowned_kept"] = [list(key) for key in unowned]
    if failure is not None:
        raise TimingError(
            f"sample {index} failed ({type(failure).__name__}: {failure}); "
            f"disposition remaining={disposition['remaining']}"
        ) from failure
    transition = result.transition
    return {
        "sample": index,
        "started_at": started,
        "ended_at": datetime.now(UTC).isoformat(),
        "aws_request_id": request_id,
        "world": {
            "a_size": WORLD_A_SIZE,
            "b_size": WORLD_B_SIZE,
            "a_run_ids": list(world.a_runs),
            "b_run_ids": list(world.b_runs),
            "final_alias_run_id": world.final_alias,
        },
        "outcome": result.outcome.value,
        "transition": None
        if transition is None
        else {
            "domain": transition.domain,
            "drained_cohort_revision": transition.drained_cohort_revision,
            "elected_cohort_revision": transition.elected_cohort_revision,
            "aws_request_id": transition.aws_request_id,
            "drained_at": transition.drained_at,
        },
        "attempts": [attempt_document(attempt) for attempt in rehearsal.attempts],
        "disposition": {"seeded": len(seeded), "owned": len(recording.owned), **disposition},
    }


# --- Live identity and inputs -----------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("--profile", "--environment", "--region", "--expected-account"):
        parser.add_argument(name, required=True)
    parser.add_argument("--graph-store-stack", required=True)
    parser.add_argument("--ingestion-stack", default="")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    parser.add_argument("--write-contract", action="store_true")
    return parser


def _git(*arguments: str) -> str:
    return subprocess.run(  # noqa: S603 -- fixed git argv, no shell
        ("git", "-C", str(_ROOT), *arguments), capture_output=True, text=True, check=True
    ).stdout.strip()


def _clean_head() -> str:
    if _git("status", "--porcelain"):
        raise TimingError("the worktree is dirty; the rehearsal measures only a committed SHA")
    return _git("rev-parse", "HEAD")


def _assumed_probe_client(session: Any, account: str, environment: str, region: str) -> Any:
    import boto3

    role = f"arn:aws:iam::{account}:role/adlytics-{environment}-execution-graph-probe"
    credentials = session.client("sts").assume_role(
        RoleArn=role, RoleSessionName="fresh-election-timing"
    )["Credentials"]
    return boto3.client(
        "dynamodb",
        region_name=region,
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
    )


def _held_mode(session: Any, environment: str, store_stack: str) -> dict[str, object]:
    from adapters.jobs.execution_graph.resolution import resolve_graph_table

    table = resolve_graph_table(session, environment, store_stack)
    item = _strong_get(session.client("dynamodb"), table, ("MODE#ingestion", "MODE")) or {}
    mode = {
        "table": table,
        "mode": item.get("mode", {}).get("S"),
        "revision": int(item.get("revision", {}).get("N", "0")),
        "hold": item.get("hold", {}).get("BOOL") is True,
        "read_at": datetime.now(UTC).isoformat(),
    }
    if (mode["mode"], mode["revision"], mode["hold"]) != ("GRAPH", EXPECTED_MODE_REVISION, True):
        raise TimingError("the live mode is not held GRAPH revision 12")
    return mode


def _promoter_timeout(session: Any, ingestion_stack: str) -> dict[str, object]:
    from domain.operations.generated_execution_graphs import BAKE_TASKS

    from adapters.outbound.cloudformation_stack import stack_resource_summaries

    contract_ms = int(BAKE_TASKS["worker_pass_seconds"]) * 1000
    resources = stack_resource_summaries(session.client("cloudformation"), ingestion_stack)
    names = [
        str(item["PhysicalResourceId"])
        for item in resources
        if item.get("ResourceType") == "AWS::Lambda::Function"
        and str(item.get("LogicalResourceId", "")).startswith("Stage1ExecutionGraphPromoter")
    ]
    if len(names) != 1:
        raise TimingError(f"expected one deployed Promoter function, found {len(names)}")
    configuration = session.client("lambda").get_function_configuration(FunctionName=names[0])
    deployed_ms = int(configuration["Timeout"]) * 1000
    if deployed_ms != contract_ms:
        raise TimingError(
            f"the deployed Promoter timeout {deployed_ms} ms differs from the contract {contract_ms}"
        )
    return {
        "function_name": names[0],
        "timeout_ms": deployed_ms,
        "contract_timeout_ms": contract_ms,
    }


def _write_atomic(path: Path, document: Mapping[str, object]) -> bytes:
    if path.exists():
        raise TimingError(f"refusing to overwrite {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)
    return data


def main(argv: Sequence[str] | None = None) -> int:
    """Run the rehearsal end to end; a refusal prints `NOT_PROVEN` and writes no derivation."""
    arguments = _parser().parse_args(argv)
    try:
        if arguments.environment != "dev":
            raise TimingError("the rehearsal runs only against dev")
        if str(arguments.profile).casefold() in {"", "default"}:
            raise TimingError("an explicit nondefault profile is required")
        if _ACCOUNT.fullmatch(str(arguments.expected_account)) is None:
            raise TimingError("--expected-account must be a 12-digit account id")
        if arguments.samples < 1:
            raise TimingError("--samples must be at least 1")
        output = Path(arguments.output).resolve()
        if output.exists():
            raise TimingError(f"refusing to overwrite {output}")
        repository_relative(output)
        code_sha = _clean_head()
        import boto3

        from adapters.jobs.execution_graph.fence_settler import due_shards
        from domain.operations.generated_execution_graphs import (
            BOUNDS,
            CONTRACT_REVISION,
            PROTOCOL_VERSION,
            SERVING_COALESCING,
        )

        session = boto3.Session(profile_name=arguments.profile, region_name=arguments.region)
        caller = session.client("sts").get_caller_identity()
        if caller["Account"] != arguments.expected_account:
            raise TimingError("the explicit profile is not the expected account")
        environment = str(arguments.environment)
        mode = _held_mode(session, environment, str(arguments.graph_store_stack))
        promoter = _promoter_timeout(
            session, str(arguments.ingestion_stack or f"Adlytics-Ingestion-{environment}")
        )
        table = f"adlytics-{environment}-execution-graph-probe"
        client = _assumed_probe_client(
            session, str(arguments.expected_account), environment, str(arguments.region)
        )
        foreign = foreign_obligations(client, table, due_shards())
        if foreign:
            raise TimingError(f"the probe due shards hold {len(foreign)} foreign obligations")
        max_redecide = int(BOUNDS["max_redecide"])
        sample_started = datetime.now(UTC).isoformat()
        samples = [
            run_sample(
                index,
                SampleInputs(client, table, max_redecide, int(cast("int", promoter["timeout_ms"]))),
            )
            for index in range(1, arguments.samples + 1)
        ]
        identity: dict[str, object] = {
            "contract_revision": int(CONTRACT_REVISION),
            "protocol_version": PROTOCOL_VERSION,
            "code_sha": code_sha,
            "environment": environment,
            "account": caller["Account"],
            "region": arguments.region,
            "caller_arn": caller["Arn"],
            "runner": "operator host, final-runtime code at code_sha, dev probe table",
            "probe_table": table,
            "live_mode": mode,
            "promoter": promoter,
            "promoter_timeout_ms": promoter["timeout_ms"],
            "max_redecide": max_redecide,
            "conflicts_per_sample": max_redecide,
            "current_contract_guard_ms": int(SERVING_COALESCING["next_election_min_remaining_ms"]),
            "sample_started_at": sample_started,
            "sample_ended_at": datetime.now(UTC).isoformat(),
        }
        data = _write_atomic(output, timing_document(identity, samples))
    except TimingError as error:
        print(f"NOT_PROVEN: {error}", file=sys.stderr)
        return 1
    document = json.loads(data)
    sha = hashlib.sha256(data).hexdigest()
    print(f"evidence: {output} sha256={sha} guard={document['next_election_min_remaining_ms']}")
    if arguments.write_contract:
        CONTRACT_PATH.write_text(
            contract_with_guard(
                CONTRACT_PATH.read_text(encoding="utf-8"),
                int(document["next_election_min_remaining_ms"]),
                repository_relative(output),
                sha,
            ),
            encoding="utf-8",
        )
        print(f"contract: {CONTRACT_PATH} (run make generate-execution-graph-protocol)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
