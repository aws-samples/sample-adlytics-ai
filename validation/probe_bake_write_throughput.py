# SPDX-License-Identifier: MIT-0
"""Measure the Baker's sibling write throughput at several pairs in flight (ADR-145, dev only).

    uv run python validation/probe_bake_write_throughput.py --profile <profile> \
        --environment dev --region us-east-1 --expected-account <account> \
        --body-bytes <B> --custody-bytes <C> --pairs 512 --repeats 3 \
        --widths 8 16 32 64 --output <ignored evidence path>

One disputed fact: does the stage's write throughput rise with its width, and where does it stop
rising? Each run writes `--pairs` synthetic body->custody pairs through the real write path:
`PresetArtifactPersistence.stage` at `write_width=W` over the real `S3PresetArtifactBodyStore` and
`S3PresetArtifactCustodyStore`, each holding its own default `session.client("s3")`, exactly as
`baker.application()` composes them (`--shared-pool` instead gives both stores one client with
`max_pool_connections=W`). The stage's own code sends every `PutObject` with `IfNoneMatch="*"`,
body before custody, then the index shard, the parent body and the parent custody. Each pair is
rendered when the stage draws it, as a page source renders on draw.

The only difference from a bake is the key: a pass-through client prefixes every key the stores
name with `probe/bake-write-throughput/<run id>/<label>/`, so the probe writes nothing outside its
own prefix and its bytes, headers and client configuration are the stores' own. Before each
measured run, a warm-up stage of `2 x W` pairs opens the clients' connections, as a warm Baker
container has them open; the warm-up is written and deleted but not measured.

Per run: sibling-phase objects/s and MB/s (first sibling PUT start to last sibling PUT end),
whole-stage objects/s, PUT p50/p95 (linear-interpolated, each call including botocore's retries,
as the Lambda's `S3.PutObject` span does), the peak PUTs in flight, every 503 attempt botocore
saw (`needs-retry`), status counts, and `time.process_time` against wall time. A fresh run admits
only HTTP 200; a 412 means the keys were not fresh and the probe stops.

Controls, run in the same execution: one already-written sibling is written again through the
real store and must return exactly one 412 then one GET (the replay path, F6), proving
`IfNoneMatch` is enforced and fresh writes are not passing trivially; and the prefix listing must
show the run's objects before cleanup, so the empty listing after it is not a blind listing.

Cleanup: after each run every object version and delete marker under the run's label prefix is
deleted (`ListObjectVersions` then `DeleteObjects` by version id: the bucket is versioned), and
both `ListObjectVersions` and `ListObjectsV2` must show the prefix empty; the whole run prefix is
cleaned and listed once more on exit, also on failure.

Refuses any environment but dev before any AWS call, a profile named `default`, and an account
that is not `--expected-account`. The bucket is read from the publisher's published SSM parameter.
AWS calls: STS, SSM `GetParameter`, S3 `PutObject`/`GetObject` (the stores'),
`ListObjectVersions`, `ListObjectsV2`, `DeleteObjects`. The output holds measurements and
synthetic key suffixes only, never an account, a bucket or a profile.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NoReturn, cast

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from adapters.outbound.preset_artifacts import PresetArtifactPersistence
from adapters.outbound.s3.preset_artifacts import (
    S3PresetArtifactBodyStore,
    S3PresetArtifactCustodyStore,
)
from domain.operations.generated_execution_graphs import BAKE_TASKS
from domain.presentation.preset_artifacts import (
    PresetArtifactBody,
    PresetArtifactCustodyHeader,
    PresetArtifactPage,
    PresetArtifactPointerTemplate,
    PresetArtifactPublicationRequest,
    PresetArtifactRevision,
    PresetArtifactSiblingBody,
    PresetArtifactSiblingCustody,
    PresetArtifactSiblingIdentity,
    PresetArtifactSiblingPair,
    PresetArtifactSiblingStream,
    PresetArtifactVariant,
    canonical_preset_artifact_json,
)

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

    from ports.outbound.preset_artifacts import PresetArtifactPointerStorePort

SCHEMA: Final = "bake-write-throughput/v1"
ENVIRONMENT: Final = "dev"
PREFIX_ROOT: Final = "probe/bake-write-throughput/"
"""Every key the probe writes starts here; nothing a bake or a reader names does."""
WIDTHS: Final = (8, 16, 32, 64)
_PAGE: Final = PresetArtifactPage("audiences")
_VARIANT: Final = PresetArtifactVariant("last_complete_month")
_ORDERING: Final = "probe"
_ACCOUNT_DIGITS: Final = 12
_BUCKET_PARAMETER: Final = "/adlytics/{environment}/publisher/artifact-bucket-name"
_DELETE_BATCH: Final = 1_000
"""`DeleteObjects` takes at most 1,000 keys per request."""
_UNUSED_REMAINING_MS: Final = int(BAKE_TASKS["lambda_timeout_seconds"]) * 1_000
"""A whole invocation remains for every run, so the stage's deadline never stops a probe run."""


class ProbeError(RuntimeError):
    """The probe could not measure what it set out to; nothing is concluded."""


# --- target -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProbeTarget:
    """Every explicit target input; refused before any AWS call unless it names dev."""

    profile: str
    environment: str
    region: str
    expected_account: str

    def __post_init__(self) -> None:
        if self.environment != ENVIRONMENT:
            raise ProbeError("the write-throughput probe runs only against ENV=dev.")
        if not self.profile.strip() or self.profile.casefold() == "default":
            raise ProbeError("an explicit nondefault profile is required.")
        if not self.region.strip():
            raise ProbeError("an explicit region is required.")
        if len(self.expected_account) != _ACCOUNT_DIGITS or not self.expected_account.isdigit():
            raise ProbeError("the expected account must be 12 digits.")


@dataclass(frozen=True, slots=True)
class RunShape:
    """One measured run's width and synthetic pair shape."""

    width: int
    pairs: int
    body_bytes: int
    custody_bytes: int
    shared_pool: bool = False

    def __post_init__(self) -> None:
        if self.width < 1 or self.pairs < 1:
            raise ProbeError("a run needs a positive width and pair count.")
        if self.body_bytes < 1 or self.custody_bytes < 1:
            raise ProbeError("synthetic bodies need positive sizes.")


# --- the pass-through client: same client, keys under the probe prefix -----------------------


@dataclass(frozen=True, slots=True)
class PutSample:
    """One `put_object` call as the stores made it: key suffix, times and outcome."""

    key: str
    started: float
    ended: float
    status: str
    size: int


@dataclass
class Recorder:
    """Thread-safe record of every store call one stage made through its prefixed clients."""

    puts: list[PutSample] = field(default_factory=list[PutSample])
    gets: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def put(self, sample: PutSample) -> None:
        """Record one PUT call."""
        with self.lock:
            self.puts.append(sample)

    def get(self) -> None:
        """Count one GET call (the replay path's byte comparison)."""
        with self.lock:
            self.gets += 1


@dataclass
class SlowDownCounter:
    """Every PutObject attempt S3 answered 503 (SlowDown), counted on botocore's `needs-retry`.

    The handler returns None, so botocore's own retry handler alone decides each retry.
    """

    count: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def on_needs_retry(self, response: Any = None, **_kwargs: Any) -> None:
        """Count this attempt if S3 answered it 503."""
        status = getattr(response[0], "status_code", None) if response else None
        if status == 503:  # noqa: PLR2004 -- HTTP 503 Slow Down
            with self.lock:
                self.count += 1


class PrefixedClient:
    """Pass the stores' `put_object`/`get_object` through to a real S3 client under a prefix.

    Every argument is the store's own except `Key`, which is prefixed; the client, its
    configuration and its retries are untouched. Nothing else is exposed, so a store call the
    probe did not anticipate fails instead of writing outside the prefix.
    """

    def __init__(self, client: Any, prefix: str, recorder: Recorder) -> None:
        if not prefix.startswith(PREFIX_ROOT) or not prefix.endswith("/"):
            raise ProbeError("a probe key prefix must sit under the probe root.")
        self._client = client
        self._prefix = prefix
        self._recorder = recorder

    def put_object(self, **arguments: Any) -> Any:
        """Time one store PUT, its key under the prefix, every other argument the store's."""
        key = str(arguments["Key"])
        body = cast("bytes", arguments["Body"])
        started = time.perf_counter()
        status = "200"
        try:
            return self._client.put_object(**{**arguments, "Key": self._prefix + key})
        except ClientError as error:
            status = str(error.response.get("Error", {}).get("Code", "error"))
            raise
        finally:
            self._recorder.put(PutSample(key, started, time.perf_counter(), status, len(body)))

    def get_object(self, **arguments: Any) -> Any:
        """Pass one store GET through, its key under the prefix."""
        self._recorder.get()
        return self._client.get_object(**{**arguments, "Key": self._prefix + str(arguments["Key"])})


def baker_clients(session: Any, shape: RunShape, counter: SlowDownCounter) -> tuple[Any, Any]:
    """Build the stores' clients as `baker.application()` does: two default S3 clients.

    `S3PresetArtifactBodyStore(bucket)` and `S3PresetArtifactCustodyStore(bucket)` each call
    `boto3.client("s3")` with no configuration. `shared_pool` builds B2's alternative: one client
    whose pool holds `width` connections, shared by both stores.
    """
    if shape.shared_pool:
        shared = session.client("s3", config=Config(max_pool_connections=shape.width))
        clients = (shared, shared)
    else:
        clients = (session.client("s3"), session.client("s3"))
    for client in {id(item): item for item in clients}.values():
        client.meta.events.register("needs-retry.s3.PutObject", counter.on_needs_retry)
    return clients


def client_config(client: Any) -> dict[str, Any]:
    """Return the effective configuration that decides a PUT's connections, timeouts and retries."""
    config = client.meta.config
    return {
        "max_pool_connections": config.max_pool_connections,
        "connect_timeout": config.connect_timeout,
        "read_timeout": config.read_timeout,
        "retries": config.retries,
        "tcp_keepalive": config.tcp_keepalive,
    }


# --- synthetic revision -----------------------------------------------------------------------


def padded_json(fields: Mapping[str, object], size: int) -> str:
    """Canonical JSON of `fields` plus a pad, exactly `size` UTF-8 bytes."""
    base = canonical_preset_artifact_json({**fields, "pad": ""})
    if len(base.encode()) > size:
        raise ProbeError(f"a synthetic body cannot be as small as {size} B.")
    payload = canonical_preset_artifact_json({**fields, "pad": "x" * (size - len(base.encode()))})
    if len(payload.encode()) != size:
        raise ProbeError("a synthetic body did not reach its exact size.")
    return payload


@dataclass(frozen=True, slots=True)
class _SyntheticPairs:
    """Render each synthetic pair when the stage draws it, as a page source does."""

    revision: PresetArtifactRevision
    shape: RunShape

    def __call__(self) -> Iterator[PresetArtifactSiblingPair]:
        for index in range(self.shape.pairs):
            identity = PresetArtifactSiblingIdentity(_ORDERING, index)
            fields = {"probe": SCHEMA, "segment": index}
            yield (
                PresetArtifactSiblingBody(
                    _PAGE,
                    _VARIANT,
                    self.revision,
                    identity,
                    padded_json({**fields, "part": "body"}, self.shape.body_bytes),
                ),
                PresetArtifactSiblingCustody(
                    _PAGE,
                    _VARIANT,
                    self.revision,
                    identity,
                    padded_json({**fields, "part": "custody"}, self.shape.custody_bytes),
                ),
            )


def synthetic_request(label: str, shape: RunShape) -> PresetArtifactPublicationRequest:
    """One synthetic revision: `pairs` sibling pairs, then the stage's index and parent."""
    revision = PresetArtifactRevision.from_publication_identities((f"probe:{label}",))
    body = PresetArtifactBody(
        _PAGE, _VARIANT, revision, padded_json({"probe": SCHEMA, "part": "parent"}, 1_024)
    )
    return PresetArtifactPublicationRequest(
        body,
        PresetArtifactPointerTemplate(_PAGE, _VARIANT, revision, body.digest, 1),
        PresetArtifactCustodyHeader(canonical_preset_artifact_json({"probe": SCHEMA})),
        PresetArtifactSiblingStream(_SyntheticPairs(revision, shape), shape.pairs),
    )


class _NoPointers:
    """`stage` never touches a pointer; any call is a probe defect."""

    def __getattr__(self, name: str) -> NoReturn:
        raise ProbeError(f"the probe's stage called the pointer store ({name}).")


@dataclass(frozen=True, slots=True)
class StageClients:
    """One run's two store clients (one object when the pool is shared) and its 503 counter."""

    bucket: str
    bodies: Any
    custody: Any
    slowdowns: SlowDownCounter

    def persistence(self, prefix: str, recorder: Recorder, width: int) -> PresetArtifactPersistence:
        """Build the Baker's persistence over the real stores at `width`, keys under `prefix`."""
        return PresetArtifactPersistence(
            S3PresetArtifactBodyStore(
                self.bucket, cast("S3Client", PrefixedClient(self.bodies, prefix, recorder))
            ),
            cast("PresetArtifactPointerStorePort", _NoPointers()),
            S3PresetArtifactCustodyStore(
                self.bucket, cast("S3Client", PrefixedClient(self.custody, prefix, recorder))
            ),
            write_width=width,
        )


# --- measurement ------------------------------------------------------------------------------


def percentile(values: list[float], fraction: float) -> float:
    """Linear-interpolated percentile, as `perf/v3/it3/writes.py` computes the Lambda spans'."""
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def peak_in_flight(samples: list[PutSample]) -> int:
    """Return the most calls that overlapped in time."""
    events = sorted(
        [(item.started, 1) for item in samples] + [(item.ended, -1) for item in samples]
    )
    current = peak = 0
    for _, step in events:
        current += step
        peak = max(peak, current)
    return peak


def summarize(  # noqa: PLR0917 -- one run's samples, shape and three measured totals
    recorder: Recorder, shape: RunShape, wall_seconds: float, cpu_seconds: float, slowdowns: int
) -> dict[str, Any]:
    """One run's throughput, latency, concurrency, 503 and CPU figures from its samples."""
    puts = recorder.puts
    statuses: dict[str, int] = {}
    for item in puts:
        statuses[item.status] = statuses.get(item.status, 0) + 1
    siblings = [item for item in puts if "/segments/" in item.key]
    if len(siblings) != 2 * shape.pairs:
        raise ProbeError(f"the stage wrote {len(siblings)} sibling objects, not {2 * shape.pairs}.")
    phase = max(item.ended for item in siblings) - min(item.started for item in siblings)
    sibling_bytes = sum(item.size for item in siblings)
    durations = [(item.ended - item.started) * 1_000 for item in puts]
    origin = min(item.started for item in puts)
    return {
        "width": shape.width,
        "shared_pool": shape.shared_pool,
        "pairs": shape.pairs,
        "objects": len(puts),
        "sibling_objects": len(siblings),
        "sibling_phase_seconds": round(phase, 4),
        "sibling_objects_per_second": round(len(siblings) / phase, 2),
        "sibling_mb_per_second": round(sibling_bytes / phase / 1_000_000, 4),
        "stage_wall_seconds": round(wall_seconds, 4),
        "stage_objects_per_second": round(len(puts) / wall_seconds, 2),
        "put_ms_p50": round(percentile(durations, 0.5), 2),
        "put_ms_p95": round(percentile(durations, 0.95), 2),
        "put_ms_max": round(max(durations), 2),
        "puts_peak_in_flight": peak_in_flight(puts),
        "statuses": statuses,
        "slowdown_503_attempts": slowdowns,
        "gets": recorder.gets,
        "process_cpu_seconds": round(cpu_seconds, 4),
        "cpu_over_wall": round(cpu_seconds / wall_seconds, 4),
        "cpu_ms_per_object": round(cpu_seconds * 1_000 / len(puts), 3),
        "samples": [
            [
                item.key,
                round((item.started - origin) * 1_000, 3),
                round((item.ended - item.started) * 1_000, 3),
                item.status,
                item.size,
            ]
            for item in sorted(puts, key=lambda item: item.started)
        ],
        "samples_columns": ["key_suffix", "start_ms", "duration_ms", "status", "bytes"],
    }


@dataclass(frozen=True, slots=True)
class _Store:
    """The listing/deletion side, on its own default client: it measures nothing."""

    client: Any
    bucket: str

    def versions(self, prefix: str) -> list[dict[str, str]]:
        found: list[dict[str, str]] = []
        paginator = self.client.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for item in (*page.get("Versions", ()), *page.get("DeleteMarkers", ())):
                found.append({"Key": item["Key"], "VersionId": item["VersionId"]})
        return found

    def listed_count(self, prefix: str) -> int:
        # aws-pagination: emptiness needs one key, so MaxKeys=1 and no continuation is exact.
        response = self.client.list_objects_v2(Bucket=self.bucket, Prefix=prefix, MaxKeys=1)
        return int(response.get("KeyCount", 0))

    def clean(self, prefix: str) -> dict[str, Any]:
        """Delete every version and marker under `prefix`, then prove it empty both ways."""
        if not prefix.startswith(PREFIX_ROOT):
            raise ProbeError("the probe deletes only under its own prefix.")
        before = self.versions(prefix)
        listed_before = self.listed_count(prefix)
        errors = 0
        for start in range(0, len(before), _DELETE_BATCH):
            response = self.client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": before[start : start + _DELETE_BATCH], "Quiet": True},
            )
            errors += len(response.get("Errors", ()))
        after = self.versions(prefix)
        listed_after = self.listed_count(prefix)
        return {
            "versions_before": len(before),
            "listed_before": listed_before,
            "delete_errors": errors,
            "versions_after": len(after),
            "listed_after": listed_after,
            "empty": errors == 0 and not after and listed_after == 0,
        }


def _staged(writer: PresetArtifactPersistence, request: PresetArtifactPublicationRequest) -> None:
    writer.stage(
        request,
        remaining_ms=lambda: _UNUSED_REMAINING_MS,
        tail_seconds=float(BAKE_TASKS["tail_seconds"]),
    )


def measured_run(
    session: Any, store: _Store, *, run_prefix: str, label: str, shape: RunShape
) -> dict[str, Any]:
    """Warm the clients, stage one fresh revision, run the replay control, then clean up."""
    prefix = f"{run_prefix}{label}/"
    measured = f"{prefix}measured/"
    try:
        counter = SlowDownCounter()
        bodies, custody = baker_clients(session, shape, counter)
        clients = StageClients(store.bucket, bodies, custody, counter)
        warm = RunShape(shape.width, 2 * shape.width, shape.body_bytes, shape.custody_bytes)
        _staged(
            clients.persistence(f"{prefix}warmup/", Recorder(), shape.width),
            synthetic_request(f"{label}:warmup", warm),
        )
        recorder = Recorder()
        request = synthetic_request(label, shape)
        writer = clients.persistence(measured, recorder, shape.width)
        slowdowns_before = counter.count
        wall, cpu = time.perf_counter(), time.process_time()
        _staged(writer, request)
        wall, cpu = time.perf_counter() - wall, time.process_time() - cpu
        result = summarize(recorder, shape, wall, cpu, counter.count - slowdowns_before)
        if set(result["statuses"]) != {"200"}:
            raise ProbeError(f"a fresh run saw {result['statuses']}: its keys were not fresh.")
        result["replay_control"] = replay_control(clients, measured, request)
        result["client_config"] = client_config(bodies)
        result["distinct_clients"] = len({id(bodies), id(custody)})
    finally:
        cleaned = store.clean(prefix)
    result["cleanup"] = cleaned
    if not cleaned["empty"] or cleaned["listed_before"] == 0:
        raise ProbeError(f"the run prefix {label} was not proven cleaned: {cleaned}.")
    return result


def replay_control(
    clients: StageClients, prefix: str, request: PresetArtifactPublicationRequest
) -> dict[str, Any]:
    """Write one already-written sibling again: the store must see one 412, then one GET."""
    recorder = Recorder()
    store = S3PresetArtifactBodyStore(
        clients.bucket, cast("S3Client", PrefixedClient(clients.bodies, prefix, recorder))
    )
    public, _ = next(iter(request.siblings))
    started = time.perf_counter()
    store.write_sibling(public)
    elapsed = (time.perf_counter() - started) * 1_000
    statuses = [item.status for item in recorder.puts]
    if statuses not in (["PreconditionFailed"], ["412"]) or recorder.gets != 1:
        raise ProbeError(f"the replay control saw {statuses} and {recorder.gets} GETs.")
    return {"statuses": statuses, "gets": recorder.gets, "replay_ms": round(elapsed, 2)}


def schedule(widths: tuple[int, ...], repeats: int) -> list[tuple[int, int]]:
    """(repeat, width) in alternating order, so drift over the session doesn't favour a width."""
    order: list[tuple[int, int]] = []
    for repeat in range(repeats):
        for width in widths if repeat % 2 == 0 else tuple(reversed(widths)):
            order.append((repeat + 1, width))
    return order


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    """One probe execution: the target and the run shapes."""

    target: ProbeTarget
    widths: tuple[int, ...]
    repeats: int
    pairs: int
    body_bytes: int
    custody_bytes: int
    shared_pool: bool
    output: Path


def run(
    request: ProbeRequest, session_factory: Callable[..., Any] = boto3.Session
) -> dict[str, Any]:
    """Check the identity, resolve the bucket, run every (repeat, width), and clean up."""
    target = request.target
    session = session_factory(profile_name=target.profile, region_name=target.region)
    identity = session.client("sts").get_caller_identity()
    if str(identity.get("Account", "")) != target.expected_account:
        raise ProbeError("the explicit profile's account differs from the expected account.")
    parameter = session.client("ssm").get_parameter(
        Name=_BUCKET_PARAMETER.format(environment=target.environment)
    )
    bucket = str(parameter.get("Parameter", {}).get("Value", "")).strip()
    if not bucket:
        raise ProbeError("the artifact bucket parameter resolved empty.")
    store = _Store(session.client("s3"), bucket)
    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    run_prefix = f"{PREFIX_ROOT}{run_id}/"
    runs: list[dict[str, Any]] = []
    started = datetime.now(UTC)
    try:
        for repeat, width in schedule(request.widths, request.repeats):
            shape = RunShape(
                width, request.pairs, request.body_bytes, request.custody_bytes, request.shared_pool
            )
            label = f"w{width:03d}-r{repeat}"
            result = measured_run(session, store, run_prefix=run_prefix, label=label, shape=shape)
            result["repeat"] = repeat
            result["ended_at"] = datetime.now(UTC).isoformat()
            runs.append(result)
            print(
                f"{label} {result['sibling_objects_per_second']} obj/s "
                f"{result['sibling_mb_per_second']} MB/s p50={result['put_ms_p50']} "
                f"p95={result['put_ms_p95']} peak={result['puts_peak_in_flight']} "
                f"503={result['slowdown_503_attempts']} cpu/wall={result['cpu_over_wall']}",
                flush=True,
            )
    finally:
        final = store.clean(run_prefix)
    if not final["empty"]:
        raise ProbeError(f"the probe prefix was not proven empty: {final}.")
    return {
        "schema": SCHEMA,
        "environment": target.environment,
        "region": target.region,
        "run_id": run_id,
        "prefix": run_prefix,
        "started_at": started.isoformat(),
        "ended_at": datetime.now(UTC).isoformat(),
        "identity_matched": True,
        "shape": {
            "pairs": request.pairs,
            "body_bytes": request.body_bytes,
            "custody_bytes": request.custody_bytes,
            "shared_pool": request.shared_pool,
            "page": _PAGE.value,
            "variant": _VARIANT.value,
        },
        "runs": runs,
        "final_cleanup": final,
    }


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ProbeError(f"invalid arguments: {message}")


def arguments(argv: list[str] | None = None) -> ProbeRequest:
    """Parse every explicit input; the target refuses anything but dev."""
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--widths", type=int, nargs="+", default=list(WIDTHS))
    parser.add_argument("--repeats", type=int, required=True)
    parser.add_argument("--pairs", type=int, required=True)
    parser.add_argument("--body-bytes", type=int, required=True)
    parser.add_argument("--custody-bytes", type=int, required=True)
    parser.add_argument("--shared-pool", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parsed = parser.parse_args(argv)
    if parsed.repeats < 1 or any(width < 1 for width in parsed.widths):
        raise ProbeError("repeats and widths must be positive.")
    return ProbeRequest(
        ProbeTarget(parsed.profile, parsed.environment, parsed.region, parsed.expected_account),
        tuple(parsed.widths),
        parsed.repeats,
        parsed.pairs,
        parsed.body_bytes,
        parsed.custody_bytes,
        parsed.shared_pool,
        parsed.output,
    )


def main(argv: list[str] | None = None) -> int:
    """Run the probe and write its output; print only a bounded verdict."""
    try:
        request = arguments(argv)
        result = run(request)
    except ProbeError as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", "unknown"))
        print(f"FAILED: AWS request returned {code}", file=sys.stderr)
        return 1
    request.output.parent.mkdir(parents=True, exist_ok=True)
    request.output.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    print(f"PASS: {len(result['runs'])} runs, prefix empty, written {request.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
