# SPDX-License-Identifier: MIT-0
"""Prove deployed Semantic V2 ingestion accepts one exact immutable traffic delivery.

Example:
    uv run python validation/probe_ingestion_cutover.py \
        --profile <profile> --env dev --day 2026-08-23 --glance-views 85

The probe lands data immutably, binds its exact VersionId into the immutable manifest marker, waits
for the exact attempts to succeed and their CURRENT pointers to advance, and requires the ingestion
DLQ depth to remain unchanged. It never lists account resources and only runs against dev.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from adapters.jobs.contract_validation.loader import load_contracts  # noqa: E402
from adapters.jobs.curation.manifests import (  # noqa: E402
    BuildManifestRequest,
    build_manifest,
    checksum_of,
    declared_columns_of,
    write_manifest,
)
from adapters.outbound.commerce_identity import load_commerce_identity_bridge  # noqa: E402
from adapters.outbound.dynamodb.semantic_state import (  # noqa: E402
    CURRENT_RECORD,
    DynamoAttemptStore,
    DynamoCurrentPublications,
)
from adapters.outbound.source_pack import load_source_packs  # noqa: E402
from domain.curation.landing import LandedObject, Manifest  # noqa: E402
from domain.curation.mapping import SourceDocument  # noqa: E402
from domain.data_contracts.contract import DataContract  # noqa: E402
from domain.semantics.commerce_identity import (  # noqa: E402
    ResolveCommerceIdentityRequest,
    resolve_commerce_identity,
)
from domain.semantics.publication import (  # noqa: E402
    AttemptEventKind,
    PartitionIdentity,
    PublicationScope,
    publication_lens_for,
)
from domain.semantics.source_aligned import LandedDeliveryRef  # noqa: E402
from domain.semantics.source_compiler import (  # noqa: E402
    CompileSourceRequest,
    compile_source_publication,
)
from domain.semantics.source_pack import SourcePack  # noqa: E402

if TYPE_CHECKING:
    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_dynamodb.type_defs import AttributeValueTypeDef
    from types_boto3_s3.client import S3Client
    from types_boto3_sqs.client import SQSClient
    from types_boto3_ssm.client import SSMClient

ENVIRONMENT = "dev"
MARKETPLACE = "ATVPDKIKX0DER"
REPORT_TYPE = "GET_VENDOR_TRAFFIC_REPORT"
ASIN = "B0TRAP0001"
CONTRACT_IDS = ("vendor-traffic-by-asin", "vendor-traffic-reported-total")
TERMINAL_V2 = frozenset(kind for kind in AttemptEventKind if kind is not AttemptEventKind.STARTED)
TIMEOUT_SECONDS = 300
POLL_SECONDS = 3

# Mirrors the exact typed PARAMS entries in infra/lib/config. Keeping suffixes in one map avoids
# constructing an SSM interface independently at each call site.
PARAMETER_SUFFIXES = {
    "raw_bucket": "data/v2/raw-bucket",
    "attempts_table": "data/v2/semantic-attempts-table",
    "publications_table": "data/v2/semantic-publications-table",
}


class ProbeError(RuntimeError):
    """Deployed Semantic V2 state contradicts the acceptance contract."""


@dataclass(frozen=True, slots=True)
class Resources:
    """Exact stack-published resources used by the probe."""

    raw_bucket: str
    attempts_table: str
    publications_table: str


@dataclass(frozen=True, slots=True)
class ProbeModel:
    """Synthetic evidence and identities derived before any object is landed."""

    body: bytes
    draft_manifest: Manifest
    contracts: dict[str, DataContract]
    v2_partitions: dict[str, PartitionIdentity]


@dataclass(frozen=True, slots=True)
class Baseline:
    """Exact CURRENT snapshots before landing."""

    v2_current: dict[str, bytes | None]


@dataclass(frozen=True, slots=True)
class Landing:
    """The exact versioned evidence written data-first and marker-last."""

    manifest: Manifest
    manifest_key: str
    manifest_version: str
    manifest_checksum: str
    object_key: str
    object_version: str
    object_checksum: str


@dataclass(frozen=True, slots=True)
class PartitionRequest:
    """Inputs needed to derive one exact V2 partition."""

    manifest: Manifest
    report: dict[str, object]
    contract: DataContract
    pack: SourcePack


@dataclass(frozen=True, slots=True)
class AwaitContext:
    """Shared exact resources and identities for terminal-state polling."""

    client: DynamoDBClient
    resources: Resources
    model: ProbeModel
    baseline: Baseline
    landing: Landing


@dataclass(frozen=True, slots=True)
class V2Success:
    """Identifiers proving both exact V2 partitions reached success."""

    publication_ids: dict[str, str]
    attempt_ids: dict[str, str]


def _iso_day(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected an ISO date (YYYY-MM-DD)") from error


def _views(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected an integer") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("glance views cannot be negative")
    return parsed


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="AWS profile. No default, on purpose.")
    parser.add_argument("--env", required=True, choices=(ENVIRONMENT,))
    parser.add_argument("--day", required=True, type=_iso_day)
    parser.add_argument("--glance-views", required=True, type=_views)
    return parser.parse_args()


def _published(client: SSMClient, env_name: str) -> Resources:
    """Resolve only exact typed V2 SSM paths; never discover account resources."""
    paths = {role: f"/adlytics/{env_name}/{suffix}" for role, suffix in PARAMETER_SUFFIXES.items()}
    response = client.get_parameters(Names=list(paths.values()), WithDecryption=False)
    invalid = tuple(str(value) for value in response.get("InvalidParameters", []))
    if invalid:
        raise ProbeError("required stack parameters are absent: " + ", ".join(sorted(invalid)))
    by_name = {
        str(parameter.get("Name", "")): str(parameter.get("Value", "")).strip()
        for parameter in response.get("Parameters", [])
    }
    missing = tuple(path for path in paths.values() if not by_name.get(path))
    if missing:
        raise ProbeError("required stack parameters resolved empty: " + ", ".join(sorted(missing)))
    return Resources(**{role: by_name[path] for role, path in paths.items()})


def _retrieved_at(day: dt.date) -> dt.datetime:
    """Give every probe day its own deterministic immutable delivery key."""
    return dt.datetime.combine(day, dt.time(21, 2), tzinfo=dt.UTC)


def _document(day: dt.date, glance_views: int) -> dict[str, object]:
    rendered = day.isoformat()
    return {
        "reportSpecification": {
            "reportType": REPORT_TYPE,
            "marketplaceIds": [MARKETPLACE],
            "dataStartTime": rendered,
            "dataEndTime": rendered,
            "lastUpdatedDate": f"{rendered}T00:00:00Z",
            "reportOptions": {"reportPeriod": "DAY"},
        },
        "trafficByAsin": [{"asin": ASIN, "startDate": rendered, "glanceViews": glance_views}],
        "trafficAggregate": [{"startDate": rendered, "glanceViews": glance_views}],
    }


def _manifest_declaration(day: dt.date) -> dict[str, object]:
    return {
        "source_system": "vendor_central",
        "source_dataset": REPORT_TYPE,
        "source_scope": {
            "namespace": "vendor_central.vendor_group",
            "value": "adlytics-fictional-dev-probe-vendor-group",
        },
        "option_set": {"reportPeriod": "DAY"},
        "marketplace": MARKETPLACE,
        "period_start": day.isoformat(),
        "period_end": day.isoformat(),
        "source_watermark": day.isoformat(),
        "retrieved_at": _retrieved_at(day).isoformat(),
    }


def _contracts() -> dict[str, DataContract]:
    available = {contract.contract_id: contract for _, contract in load_contracts(REPO_ROOT)}
    missing = tuple(contract_id for contract_id in CONTRACT_IDS if contract_id not in available)
    if missing:
        raise ProbeError("required traffic contracts are absent: " + ", ".join(missing))
    return {contract_id: available[contract_id] for contract_id in CONTRACT_IDS}


def _expected_v2_partition(request: PartitionRequest) -> PartitionIdentity:
    """Derive the exact CURRENT key through source compilation and governed identity resolution."""
    manifest = request.manifest
    report = request.report
    contract = request.contract
    pack = request.pack
    landed = LandedDeliveryRef(
        manifest_key=manifest.manifest_key,
        manifest_version="prelanding-manifest-version",
        manifest_checksum="prelanding-manifest-checksum",
        object_key=manifest.object_keys()[0],
        object_version="prelanding-object-version",
        object_checksum=manifest.objects[0].checksum,
    )
    publication = compile_source_publication(
        CompileSourceRequest(
            pack=pack,
            contract=contract,
            manifest=manifest,
            document=SourceDocument(document=report),
            landed_delivery=landed,
            published_at=manifest.retrieved_at,
        )
    )
    buckets = {row.measurement for row in publication.rowset.rows}
    if len(buckets) != 1:
        raise ProbeError(f"{contract.contract_id} did not compile to one day bucket")
    bucket = next(iter(buckets))
    marketplace_ids = {
        identifier
        for row in publication.rowset.rows
        for identifier in row.source_identifiers
        if identifier.namespace == pack.marketplace.namespace
    }
    if len(marketplace_ids) != 1:
        raise ProbeError(f"{contract.contract_id} did not compile one marketplace identity")
    bridge = load_commerce_identity_bridge(REPO_ROOT)
    resolved = resolve_commerce_identity(
        bridge,
        ResolveCommerceIdentityRequest(
            marketplace=next(iter(marketplace_ids)),
            business_date=bucket.window.start,
        ),
    )
    options = tuple((option.name, option.value) for option in publication.rowset.source_options)
    scope = PublicationScope(
        dataset=pack.dataset,
        commerce_scope=bridge.commerce_scope,
        population_key=resolved.marketplace.target.value,
        lens=publication_lens_for(
            publication.rowset.interpreter.value,
            options,
            publication.rowset.interpreter.version,
        ),
        time_class=pack.time.time_class,
        business_calendar=bucket.calendar,
        source_timezone=bucket.source_timezone,
        observation_basis=bucket.observation_basis,
    )
    return PartitionIdentity(scope=scope, bucket=bucket)


def _model(day: dt.date, glance_views: int) -> ProbeModel:
    report = _document(day, glance_views)
    text = json.dumps(report, sort_keys=True, indent=2, separators=(",", ": ")) + "\n"
    body = text.encode()
    object_name = f"probe-v2-{ASIN}.json"
    draft = build_manifest(
        BuildManifestRequest(
            declaration=_manifest_declaration(day),
            objects=(
                LandedObject(
                    name=object_name,
                    byte_count=len(body),
                    checksum=checksum_of(body),
                ),
            ),
            declared_columns=declared_columns_of(text, source_format="json_dual_array"),
        )
    )
    contracts = _contracts()
    packs = {pack.contract_id: pack for pack in load_source_packs(REPO_ROOT)}
    missing_packs = tuple(contract_id for contract_id in CONTRACT_IDS if contract_id not in packs)
    if missing_packs:
        raise ProbeError("required V2 source packs are absent: " + ", ".join(missing_packs))
    return ProbeModel(
        body=body,
        draft_manifest=draft,
        contracts=contracts,
        v2_partitions={
            contract_id: _expected_v2_partition(
                PartitionRequest(
                    manifest=draft,
                    report=report,
                    contract=contracts[contract_id],
                    pack=packs[contract_id],
                )
            )
            for contract_id in CONTRACT_IDS
        },
    )


def _raw_item(
    client: DynamoDBClient,
    table: str,
    key: dict[str, AttributeValueTypeDef],
) -> dict[str, AttributeValueTypeDef] | None:
    item = client.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item")
    return None if item is None else dict(item)


def _bytes(item: dict[str, AttributeValueTypeDef] | None) -> bytes | None:
    if item is None:
        return None
    return json.dumps(item, sort_keys=True, separators=(",", ":")).encode()


def _v2_current(
    client: DynamoDBClient, resources: Resources, model: ProbeModel
) -> dict[str, bytes | None]:
    return {
        contract_id: _bytes(
            _raw_item(
                client,
                resources.publications_table,
                {
                    "partition_key": {"S": model.v2_partitions[contract_id].identity},
                    "record_key": {"S": CURRENT_RECORD},
                },
            )
        )
        for contract_id in CONTRACT_IDS
    }


def _baseline(client: DynamoDBClient, resources: Resources, model: ProbeModel) -> Baseline:
    return Baseline(v2_current=_v2_current(client, resources, model))


def _version(response: Mapping[str, object], label: str) -> str:
    version = str(response.get("VersionId", "")).strip()
    if not version:
        raise ProbeError(f"the versioned raw bucket returned no VersionId for {label}")
    return version


@dataclass(frozen=True, slots=True)
class _ImmutableObject:
    bucket: str
    key: str
    body: bytes


def _put_immutable(client: S3Client, request: _ImmutableObject) -> str:
    """Create one object once, accepting a retry only when current bytes are identical."""
    try:
        return _version(
            client.put_object(
                Bucket=request.bucket,
                Key=request.key,
                Body=request.body,
                IfNoneMatch="*",
            ),
            request.key,
        )
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", ""))
        if code not in {"PreconditionFailed", "412"}:
            raise
    existing = client.get_object(Bucket=request.bucket, Key=request.key)
    if existing["Body"].read() != request.body:
        raise ProbeError(
            f"the immutable probe key already exists with different bytes: {request.key}"
        )
    return _version(existing, request.key)


def _land(client: S3Client, bucket: str, model: ProbeModel) -> Landing:
    """Create exact data first, then its immutable version-bound completion marker."""
    draft = model.draft_manifest
    object_key = draft.object_keys()[0]
    object_version = _put_immutable(
        client,
        _ImmutableObject(bucket, object_key, model.body),
    )
    manifest = build_manifest(
        BuildManifestRequest(
            declaration={
                "source_system": draft.source_system,
                "source_dataset": draft.source_dataset,
                "source_scope": {
                    "namespace": draft.source_scope.namespace,
                    "value": draft.source_scope.value,
                },
                "option_set": dict(draft.option_set),
                "marketplace": draft.marketplace,
                "period_start": draft.period_start.isoformat(),
                "period_end": draft.period_end.isoformat(),
                "source_watermark": (
                    None if draft.source_watermark is None else draft.source_watermark.isoformat()
                ),
                "retrieved_at": draft.retrieved_at.isoformat(),
            },
            objects=(
                LandedObject(
                    name=draft.objects[0].name,
                    byte_count=len(model.body),
                    checksum=checksum_of(model.body),
                    version_id=object_version,
                ),
            ),
            declared_columns=draft.declared_columns,
        )
    )
    manifest_body = write_manifest(manifest).encode()
    manifest_version = _put_immutable(
        client,
        _ImmutableObject(bucket, manifest.manifest_key, manifest_body),
    )
    return Landing(
        manifest=manifest,
        manifest_key=manifest.manifest_key,
        manifest_version=manifest_version,
        manifest_checksum=checksum_of(manifest_body),
        object_key=object_key,
        object_version=object_version,
        object_checksum=checksum_of(model.body),
    )


def _await_v2(context: AwaitContext) -> V2Success:
    client = context.client
    resources = context.resources
    model = context.model
    baseline = context.baseline
    attempts = DynamoAttemptStore(resources.attempts_table, client)
    currents = DynamoCurrentPublications(resources.publications_table, client)
    deadline = time.monotonic() + TIMEOUT_SECONDS

    while time.monotonic() < deadline:
        raw_current = _v2_current(client, resources, model)
        publications: dict[str, str] = {}
        attempt_ids: dict[str, str] = {}
        complete = True
        for contract_id in CONTRACT_IDS:
            if raw_current[contract_id] == baseline.v2_current[contract_id]:
                complete = False
                continue
            expected_partition = model.v2_partitions[contract_id]
            current = currents.current(expected_partition)
            if current is None:
                complete = False
                continue
            if current.partition != expected_partition:
                raise ProbeError(f"{contract_id} CURRENT names a substituted partition")
            attempt_id = current.attempt_id
            events = attempts.events_for(attempt_id)
            terminal: AttemptEventKind | None = None
            for event in events:
                if event.partition != expected_partition:
                    raise ProbeError(f"{contract_id} attempt names a substituted partition")
                if event.kind in TERMINAL_V2:
                    terminal = event.kind
                    if event.kind is not AttemptEventKind.SUCCEEDED:
                        detail = f": {event.reason}" if event.reason else ""
                        raise ProbeError(
                            f"{contract_id} reached terminal V2 state {event.kind.value}{detail}"
                        )
            if terminal is not AttemptEventKind.SUCCEEDED:
                complete = False
                continue
            publications[contract_id] = current.publication_id.value
            attempt_ids[contract_id] = attempt_id.value

        if complete and len(publications) == len(CONTRACT_IDS):
            return V2Success(publications, attempt_ids)
        time.sleep(POLL_SECONDS)

    raise ProbeError(
        f"V2 did not advance both exact CURRENT pointers and succeed within {TIMEOUT_SECONDS}s"
    )


def _dlq_depth(client: SQSClient, env_name: str) -> int | None:
    """Read the exact stack-owned DLQ without listing or consuming messages."""
    queue_name = f"adlytics-{env_name}-ingestion-dlq"
    try:
        queue_url = client.get_queue_url(QueueName=queue_name)["QueueUrl"]
        attributes = client.get_queue_attributes(
            QueueUrl=queue_url,
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
                "ApproximateNumberOfMessagesDelayed",
            ],
        ).get("Attributes", {})
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", ""))
        if code in {
            "AWS.SimpleQueueService.NonExistentQueue",
            "QueueDoesNotExist",
            "AccessDenied",
            "AccessDeniedException",
        }:
            return None
        raise
    return sum(
        int(attributes.get(name, "0"))
        for name in (
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
            "ApproximateNumberOfMessagesDelayed",
        )
    )


def _run(args: argparse.Namespace) -> int:
    session = boto3.Session(profile_name=args.profile)

    # Deliberately the first AWS API call: show the explicit profile's identity before targets.
    identity = session.client("sts").get_caller_identity()
    account = str(identity.get("Account", "")).strip()
    arn = str(identity.get("Arn", "")).strip()
    if not account or not arn:
        raise ProbeError("caller identity returned no account or ARN")
    print(f"identity  account={account} arn={arn}")

    resources = _published(session.client("ssm"), args.env)
    model = _model(args.day, args.glance_views)
    dynamodb = session.client("dynamodb")
    baseline = _baseline(dynamodb, resources, model)

    sqs = session.client("sqs")
    dlq_before = _dlq_depth(sqs, args.env)
    landing = _land(session.client("s3"), resources.raw_bucket, model)
    print(
        f"manifest  key={landing.manifest_key} version={landing.manifest_version} "
        f"data_version={landing.object_version}"
    )

    success = _await_v2(AwaitContext(dynamodb, resources, model, baseline, landing))
    for contract_id in CONTRACT_IDS:
        print(
            f"  positive  {contract_id}: publication={success.publication_ids[contract_id]} "
            f"attempt={success.attempt_ids[contract_id]} state=succeeded current=advanced"
        )

    dlq_after = _dlq_depth(sqs, args.env)
    if dlq_before is not None and dlq_after != dlq_before:
        raise ProbeError(f"the ingestion DLQ depth changed from {dlq_before} to {dlq_after}")
    print(
        "  negative  ingestion-dlq: "
        + (
            f"unchanged at {dlq_after} historical message(s)"
            if dlq_after is not None
            else "not checked (unavailable to this profile)"
        )
    )
    print(f"PASS: Semantic V2 ingestion accepted exact evidence for {args.day.isoformat()}")
    return 0


def main() -> int:
    """Run the probe and convert controlled failures into a nonzero exit."""
    args = _arguments()
    try:
        return _run(args)
    except ProbeError as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", "unknown"))
        print(f"FAILED: AWS request returned {code}", file=sys.stderr)
        return 1
    except Exception as error:  # defensive terminal boundary; never print response bodies
        print(f"FAILED: unexpected {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
