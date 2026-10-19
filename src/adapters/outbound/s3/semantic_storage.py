# SPDX-License-Identifier: MIT-0
"""Immutable S3 object authority for Semantic V2 source and canonical publications."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, cast

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from adapters.outbound.aws_errors import merged_aws_failure_context
from domain.semantics.canonical_compiler import (
    CanonicalRow,
    CanonicalScalar,
    canonical_rows_checksum,
)
from domain.semantics.identity import (
    DeliveryScope,
    ExtractionProfileKey,
    SourceDatasetKey,
    SourceIdentifier,
    SourceSystemKey,
)
from domain.semantics.identity_evidence import (
    IdentityMappingsClaim,
    IdentityMappingsDocument,
    verify_identity_mappings,
)
from domain.semantics.publication import SourcePublicationRef, SuccessfulPublication
from domain.semantics.revision import RevisionPrecision, RevisionSchemeKey, SourceRevision
from domain.semantics.source_aligned import (
    LandedDeliveryRef,
    SourceAlignedField,
    SourceAlignedPublication,
    SourceAlignedRow,
    SourceAlignedRowset,
    SourceInterpreterKey,
    SourceOption,
)
from domain.semantics.time import (
    BusinessCalendarKey,
    ExtractionCoverage,
    MeasurementBucket,
    ObservationTimeBasis,
    TimeGrain,
)
from domain.semantics.validation import PublicationEvidence
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError
from domain.shared_kernel.values import DateRange
from ports.outbound.canonical import (
    CanonicalDestination,
    CanonicalMaterializeRequest,
    CanonicalWriteResult,
)
from ports.outbound.identity_evidence import IdentityEvidenceWriteResult
from ports.outbound.source_aligned import MaterializeSourceRowsetResult

if TYPE_CHECKING:  # pragma: no cover
    from types_boto3_s3.client import S3Client

__all__ = [
    "IDENTITY_EVIDENCE_PREFIX",
    "ProjectionUnitSource",
    "S3CanonicalAdapter",
    "S3IdentityEvidenceAdapter",
    "S3ProjectionObjectSource",
    "S3SourceAlignedAdapter",
    "canonical_object_bytes",
    "canonical_object_key",
    "identity_evidence_key",
    "projection_source_publication",
    "projection_unit_source",
    "source_object_key",
    "source_publication_payload",
]

SEMANTIC_STORAGE_UNAVAILABLE = ErrorCode("SEMANTIC_STORAGE", 1)
SEMANTIC_STORAGE_MALFORMED = ErrorCode("SEMANTIC_STORAGE", 2)
SOURCE_PUBLICATION_VERSION = 2
IDENTITY_EVIDENCE_PREFIX = "immutable/identity-evidence/"
"""Content-addressed claim-check documents; the only prefix the ingestion role may write them to."""


def _scalar(value: object) -> dict[str, object]:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("semantic storage cannot encode a naive datetime.")
        return {"type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if isinstance(value, Decimal):
        return {"type": "decimal", "value": str(value)}
    if value is None:
        return {"type": "null", "value": None}
    if isinstance(value, (str, int, float, bool)):
        return {"type": type(value).__name__, "value": value}
    raise ValueError(f"semantic storage cannot encode {type(value).__name__}.")


def _read_scalar(raw: object) -> CanonicalScalar:  # noqa: PLR0911
    item = _object(raw)
    kind = _string(item, "type")
    value = item.get("value")
    if kind == "null" and value is None:
        return None
    if kind == "date" and isinstance(value, str):
        return date.fromisoformat(value)
    if kind == "datetime" and isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("stored datetime is naive.")
        return parsed
    if kind == "decimal" and isinstance(value, str):
        return Decimal(value)
    if kind == "str" and isinstance(value, str):
        return value
    if kind == "bool" and isinstance(value, bool):
        return value
    if kind == "int" and isinstance(value, int) and not isinstance(value, bool):
        return value
    if kind == "float" and isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    raise ValueError(f"stored scalar {kind!r} has an invalid value.")


def _source_data(publication: SourceAlignedPublication) -> dict[str, object]:
    rowset = publication.rowset
    scope = rowset.delivery_scope
    revision = publication.source_revision
    return {
        "version": SOURCE_PUBLICATION_VERSION,
        "reference": publication.reference.value,
        "landed_delivery": {
            "manifest_key": publication.landed_delivery.manifest_key,
            "manifest_version": publication.landed_delivery.manifest_version,
            "manifest_checksum": publication.landed_delivery.manifest_checksum,
            "object_key": publication.landed_delivery.object_key,
            "object_version": publication.landed_delivery.object_version,
            "object_checksum": publication.landed_delivery.object_checksum,
            "first_observed_at": (
                None
                if publication.landed_delivery.first_observed_at is None
                else publication.landed_delivery.first_observed_at.isoformat()
            ),
        },
        "delivery_scope": {
            "source_system": scope.source_system.value,
            "source_dataset": scope.source_dataset.value,
            "extraction_profile": scope.extraction_profile.value,
            "extraction_profile_version": scope.extraction_profile.version,
            "source_scope_namespace": scope.source_scope.namespace,
            "source_scope_value": scope.source_scope.value,
            "option_set_hash": scope.option_set_hash,
        },
        "coverage": {
            "start": rowset.extraction_coverage.window.start.isoformat(),
            "end": rowset.extraction_coverage.window.end.isoformat(),
        },
        "interpreter": {"key": rowset.interpreter.value, "version": rowset.interpreter.version},
        "source_options": [
            {"name": item.name, "value": item.value} for item in rowset.source_options
        ],
        "rows": [
            {
                "fields": [
                    {"name": field.name, "value": _scalar(field.value)} for field in row.fields
                ],
                "source_identifiers": [
                    {"namespace": item.namespace, "value": item.value}
                    for item in row.source_identifiers
                ],
                "measurement": {
                    "start": row.measurement.window.start.isoformat(),
                    "end": row.measurement.window.end.isoformat(),
                    "grain": row.measurement.grain.value,
                    "calendar": row.measurement.calendar.value,
                    "week_starts_on": row.measurement.calendar.week_starts_on,
                    "source_timezone": row.measurement.source_timezone,
                    "observation_basis": row.measurement.observation_basis.value,
                },
            }
            for row in rowset.rows
        ],
        "source_revision": {
            "scheme": revision.scheme.value,
            "precision": revision.precision.value,
            "observed_at": revision.observed_at.isoformat(),
            "source_id": revision.source_id,
        },
        "published_at": publication.published_at.isoformat(),
        "content_checksum": publication.content_checksum,
    }


def source_publication_payload(publication: SourceAlignedPublication) -> dict[str, object]:
    """Return the deterministic typed source envelope projected into S3 and Iceberg."""
    return _source_data(publication)


def _evidence_data(evidence: PublicationEvidence) -> dict[str, object]:
    subject = evidence.subject
    return {
        "rule_set_version": evidence.rule_set_version,
        "subject": (
            None
            if subject is None
            else {
                "publisher": subject.publisher,
                "partition": subject.partition,
                "source_publications": list(subject.source_publications),
                "source_revision": subject.source_revision,
                "content_checksum": subject.content_checksum,
            }
        ),
        "results": [
            {
                "family": result.family.value,
                "rule_id": result.rule_id,
                "outcome": result.outcome.value,
                "detail": result.detail,
                "qualifier": result.qualifier,
            }
            for result in evidence.results
        ],
        "legacy": (
            None
            if evidence.legacy is None
            else {
                "passed_rules": list(evidence.legacy.passed_rules),
                "qualified_warnings": list(evidence.legacy.qualified_warnings),
                "failed_rules": list(evidence.legacy.failed_rules),
            }
        ),
    }


def _canonical_data(request: CanonicalMaterializeRequest) -> dict[str, object]:
    publication = request.publication
    return {
        "version": 1,
        "destination": request.destination.value,
        "publication_id": publication.publication_id.value,
        "attempt_id": publication.attempt_id.value,
        "partition_key": publication.partition.identity,
        "publisher": publication.publisher.value,
        "fencing_token": publication.fencing_token,
        "source_publications": [item.value for item in publication.source_inputs],
        "source_revision": {
            "scheme": publication.source_revision.scheme.value,
            "precision": publication.source_revision.precision.value,
            "observed_at": publication.source_revision.observed_at.isoformat(),
            "source_id": publication.source_revision.source_id,
        },
        "content_checksum": publication.content_checksum,
        "rule_set_version": request.rule_set_version,
        "evidence": _evidence_data(publication.evidence),
        "created_at": publication.published_at.isoformat(),
        "rows": [
            [{"name": name, "value": _scalar(value)} for name, value in row] for row in request.rows
        ],
    }


def _canonical_rows_from(
    data: bytes, publication: SuccessfulPublication
) -> tuple[tuple[tuple[str, CanonicalScalar], ...], ...]:
    root = _object(json.loads(data))
    if (
        root.get("version") != 1
        or _string(root, "publication_id") != publication.publication_id.value
    ):
        raise ValueError("stored canonical success has a substituted publication identity.")
    rows = tuple(
        tuple(
            sorted(
                (
                    (_string(field, "name"), _read_scalar(field.get("value")))
                    for field in (_object(raw) for raw in _array(row))
                ),
                key=lambda item: item[0],
            )
        )
        for row in (_array(raw) for raw in _array(root.get("rows")))
    )
    if canonical_rows_checksum(rows) != publication.content_checksum:
        raise ValueError("stored canonical success rows disagree with the publication checksum.")
    return rows


@dataclass(frozen=True, slots=True)
class ProjectionUnitSource:
    """The flat canonical envelope plus rows a table writer projects, nothing more.

    Deliberately narrower than the domain publication: `canonical_records` composes the
    merge from exactly these fields, and the servable advance needs only the partition
    key and fencing token, so the structured `PartitionIdentity` is never reconstructed
    and the immutable DynamoDB record is never read.
    """

    destination: str
    publication_id: str
    attempt_id: str
    partition_key: str
    publisher: str
    fencing_token: int
    source_publications: tuple[str, ...]
    source_revision: str
    content_checksum: str
    rule_set_version: str
    created_at: datetime
    rows: tuple[tuple[tuple[str, CanonicalScalar], ...], ...]


def projection_unit_source(data: bytes) -> ProjectionUnitSource:
    """Decode one immutable canonical object into its strict projection workload."""
    root = _object(json.loads(data))
    if root.get("version") != 1:
        raise ValueError("stored canonical object has an unsupported version.")
    revision = _object(root["source_revision"])
    rows = tuple(
        tuple(
            sorted(
                (
                    (_string(field, "name"), _read_scalar(field.get("value")))
                    for field in (_object(raw) for raw in _array(row))
                ),
                key=lambda item: item[0],
            )
        )
        for row in (_array(raw) for raw in _array(root.get("rows")))
    )
    checksum = _string(root, "content_checksum")
    if canonical_rows_checksum(rows) != checksum:
        raise ValueError("stored canonical rows disagree with the object checksum.")
    raw_sources = _array(root["source_publications"])
    if not all(isinstance(value, str) and value for value in raw_sources):
        raise ValueError("stored canonical source publications are invalid.")
    created_at = datetime.fromisoformat(_string(root, "created_at"))
    if created_at.tzinfo is None:
        raise ValueError("stored canonical creation instant is naive.")
    destination = _string(root, "destination")
    CanonicalDestination(destination)
    fencing_token = _integer(root, "fencing_token")
    if fencing_token <= 0:
        raise ValueError("stored canonical fencing token is not positive.")
    return ProjectionUnitSource(
        destination=destination,
        publication_id=_string(root, "publication_id"),
        attempt_id=_string(root, "attempt_id"),
        partition_key=_string(root, "partition_key"),
        publisher=_string(root, "publisher"),
        fencing_token=fencing_token,
        source_publications=tuple(cast("list[str]", raw_sources)),
        source_revision="|".join(
            (
                _string(revision, "scheme"),
                _string(revision, "precision"),
                _string(revision, "observed_at"),
                _string(revision, "source_id"),
            )
        ),
        content_checksum=checksum,
        rule_set_version=_string(root, "rule_set_version"),
        created_at=created_at,
        rows=rows,
    )


def canonical_object_key(destination: str, dataset: str, publication_id: str) -> str:
    """Address one immutable canonical object without a domain publication."""
    return f"immutable/canonical/{destination}/{dataset}/{publication_id}.json"


def identity_evidence_key(claim: IdentityMappingsClaim) -> str:
    """Return the deterministic, content-addressed key of one claim-check document."""
    return f"{IDENTITY_EVIDENCE_PREFIX}{claim.sha256}.json"


def source_object_key(reference: str) -> str:
    """Address one immutable source-aligned object by its stable reference."""
    return f"immutable/source-aligned/{reference[:2]}/{reference}.json"


def _render(value: dict[str, object]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def canonical_object_bytes(request: CanonicalMaterializeRequest) -> bytes:
    """Render the complete immutable canonical envelope, including candidate-bound evidence."""
    return _render(_canonical_data(request))


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("stored semantic value is not an object.")
    return {str(key): item for key, item in cast("dict[object, object]", value).items()}


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("stored semantic value is not an array.")
    return cast("list[object]", value)


def _string(value: dict[str, object], key: str) -> str:
    found = value.get(key)
    if not isinstance(found, str) or not found:
        raise ValueError(f"stored semantic value has no {key}.")
    return found


def _integer(value: dict[str, object], key: str) -> int:
    found = value.get(key)
    if not isinstance(found, int) or isinstance(found, bool):
        raise ValueError(f"stored semantic value has no integer {key}.")
    return found


def _source_from(data: bytes) -> SourceAlignedPublication:
    root = _object(json.loads(data))
    if root.get("version") != SOURCE_PUBLICATION_VERSION:
        raise ValueError("stored source publication has an unsupported version.")
    landed = _object(root.get("landed_delivery"))
    scope = _object(root.get("delivery_scope"))
    coverage = _object(root.get("coverage"))
    interpreter = _object(root.get("interpreter"))
    revision = _object(root.get("source_revision"))
    rows: list[SourceAlignedRow] = []
    for raw_row in _array(root.get("rows")):
        item = _object(raw_row)
        measurement = _object(item.get("measurement"))
        week_start = measurement.get("week_starts_on")
        if week_start is not None and not isinstance(week_start, int):
            raise ValueError("stored business week start is invalid.")
        rows.append(
            SourceAlignedRow(
                fields=tuple(
                    SourceAlignedField(_string(field, "name"), _read_scalar(field.get("value")))
                    for field in (_object(raw) for raw in _array(item.get("fields")))
                ),
                source_identifiers=tuple(
                    SourceIdentifier(_string(identity, "namespace"), _string(identity, "value"))
                    for identity in (_object(raw) for raw in _array(item.get("source_identifiers")))
                ),
                measurement=MeasurementBucket(
                    window=DateRange(
                        date.fromisoformat(_string(measurement, "start")),
                        date.fromisoformat(_string(measurement, "end")),
                    ),
                    grain=TimeGrain(_string(measurement, "grain")),
                    calendar=BusinessCalendarKey(
                        _string(measurement, "calendar"),
                        week_start,
                    ),
                    source_timezone=_string(measurement, "source_timezone"),
                    observation_basis=ObservationTimeBasis(
                        str(
                            measurement.get(
                                "observation_basis",
                                ObservationTimeBasis.SOURCE_BUSINESS_DATE.value,
                            )
                        )
                    ),
                ),
            )
        )
    first_observed_raw = landed.get("first_observed_at")
    first_observed = (
        None if first_observed_raw is None else datetime.fromisoformat(str(first_observed_raw))
    )
    publication = SourceAlignedPublication(
        reference=SourcePublicationRef(_string(root, "reference")),
        landed_delivery=LandedDeliveryRef(
            manifest_key=_string(landed, "manifest_key"),
            manifest_version=_string(landed, "manifest_version"),
            manifest_checksum=_string(landed, "manifest_checksum"),
            object_key=_string(landed, "object_key"),
            object_version=_string(landed, "object_version"),
            object_checksum=_string(landed, "object_checksum"),
            first_observed_at=first_observed,
        ),
        rowset=SourceAlignedRowset(
            delivery_scope=DeliveryScope(
                source_system=SourceSystemKey(_string(scope, "source_system")),
                source_dataset=SourceDatasetKey(_string(scope, "source_dataset")),
                extraction_profile=ExtractionProfileKey(
                    _string(scope, "extraction_profile"),
                    _integer(scope, "extraction_profile_version"),
                ),
                source_scope=SourceIdentifier(
                    _string(scope, "source_scope_namespace"),
                    _string(scope, "source_scope_value"),
                ),
                option_set_hash=_string(scope, "option_set_hash"),
            ),
            extraction_coverage=ExtractionCoverage(
                DateRange(
                    date.fromisoformat(_string(coverage, "start")),
                    date.fromisoformat(_string(coverage, "end")),
                )
            ),
            interpreter=SourceInterpreterKey(
                _string(interpreter, "key"), _integer(interpreter, "version")
            ),
            source_options=tuple(
                SourceOption(_string(item, "name"), _string(item, "value"))
                for item in (_object(raw) for raw in _array(root.get("source_options")))
            ),
            rows=tuple(rows),
        ),
        source_revision=SourceRevision(
            scheme=RevisionSchemeKey(_string(revision, "scheme")),
            precision=RevisionPrecision(_string(revision, "precision")),
            observed_at=datetime.fromisoformat(_string(revision, "observed_at")),
            source_id=_string(revision, "source_id"),
        ),
        published_at=datetime.fromisoformat(_string(root, "published_at")),
    )
    if publication.content_checksum != _string(root, "content_checksum"):
        raise ValueError("stored source publication checksum does not match its content.")
    return publication


def projection_source_publication(data: bytes) -> SourceAlignedPublication:
    """Strictly decode one immutable source object for table projection."""
    return _source_from(data)


class S3ProjectionObjectSource:
    """Read exact plan-referenced immutable bytes from the configured semantic bucket."""

    def __init__(self, bucket: str, client: S3Client | None = None) -> None:
        if not bucket.strip():
            raise ValueError("semantic projection source bucket is required.")
        self._bucket = bucket
        self._client = client or boto3.client("s3")

    def read(self, key: str) -> bytes:
        """Return one exact immutable object, preserving any SDK failure as the cause."""
        if not key.strip():
            raise ValueError("semantic projection object key is required.")
        try:
            body = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except (BotoCoreError, ClientError) as error:
            failure = DependencyError(
                SEMANTIC_STORAGE_UNAVAILABLE,
                "Semantic projection authority could not be read.",
                context=merged_aws_failure_context(error, {"key": key}),
            )
            raise failure from error
        except Exception as error:
            failure = DependencyError(
                SEMANTIC_STORAGE_UNAVAILABLE,
                "Semantic projection authority could not be read.",
                context={"cause": type(error).__name__, "key": key},
            )
            raise failure from error
        return body


def _is_precondition(error: ClientError) -> bool:
    return str(error.response.get("Error", {}).get("Code", "")) in {"PreconditionFailed", "412"}


def _is_missing(error: ClientError) -> bool:
    return str(error.response.get("Error", {}).get("Code", "")) in {"NoSuchKey", "404"}


class S3SemanticStorageAdapter:
    """Create immutable semantic objects and accept only byte-identical replay."""

    def __init__(self, bucket: str, client: S3Client | None = None) -> None:
        if not bucket.strip():
            raise ValueError("semantic storage bucket is required.")
        self._bucket = bucket
        self._client = client or boto3.client("s3")

    def materialize(self, value: object) -> MaterializeSourceRowsetResult | CanonicalWriteResult:
        """Materialize source publication or canonical request through one typed adapter."""
        if isinstance(value, SourceAlignedPublication):
            body = _render(_source_data(value))
            key = self._source_key(value.reference)
            accepted, created = self._put_immutable(key, body)
            return MaterializeSourceRowsetResult(
                accepted=accepted,
                created=created,
                reason="" if accepted else "source publication reference already has other bytes.",
            )
        if isinstance(value, CanonicalMaterializeRequest):
            body = canonical_object_bytes(value)
            publication = value.publication
            if publication.content_checksum != canonical_rows_checksum(value.rows):
                return CanonicalWriteResult(
                    accepted=False,
                    created=False,
                    row_count=len(value.rows),
                    content_checksum=publication.content_checksum,
                    reason="canonical rows do not match the publication content checksum.",
                )
            key = self._canonical_key(value)
            accepted, created = self._put_immutable(key, body)
            return CanonicalWriteResult(
                accepted=accepted,
                created=created,
                row_count=len(value.rows),
                content_checksum=publication.content_checksum,
                reason="" if accepted else "publication identity already has other bytes.",
            )
        raise TypeError("semantic storage accepts a source publication or canonical request.")

    def attest_canonical(self, request: CanonicalMaterializeRequest) -> CanonicalWriteResult:
        """Read and compare one exact canonical object without writing or repairing it."""
        publication = request.publication
        if publication.content_checksum != canonical_rows_checksum(request.rows):
            return CanonicalWriteResult(
                accepted=False,
                created=False,
                row_count=len(request.rows),
                content_checksum=publication.content_checksum,
                reason="canonical rows do not match the publication content checksum.",
            )
        key = self._canonical_key(request)
        try:
            body = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except ClientError as error:
            if _is_missing(error):
                return CanonicalWriteResult(
                    accepted=False,
                    created=False,
                    row_count=len(request.rows),
                    content_checksum=publication.content_checksum,
                    reason="immutable S3 canonical object is absent.",
                )
            raise DependencyError(
                SEMANTIC_STORAGE_UNAVAILABLE,
                "Canonical evidence could not be attested.",
                context={"cause": type(error).__name__, "key": key},
            ) from None
        except Exception as error:
            raise DependencyError(
                SEMANTIC_STORAGE_UNAVAILABLE,
                "Canonical evidence could not be attested.",
                context={"cause": type(error).__name__, "key": key},
            ) from None
        accepted = body == canonical_object_bytes(request)
        return CanonicalWriteResult(
            accepted=accepted,
            created=False,
            row_count=len(request.rows),
            content_checksum=publication.content_checksum,
            reason="" if accepted else "immutable S3 canonical bytes differ.",
        )

    def publication(self, reference: SourcePublicationRef) -> SourceAlignedPublication | None:
        """Read one immutable source publication by its content-derived reference."""
        key = self._source_key(reference)
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            body = response["Body"].read()
        except ClientError as error:
            if _is_missing(error):
                return None
            raise DependencyError(
                SEMANTIC_STORAGE_UNAVAILABLE,
                "Semantic source evidence could not be read.",
                context={"cause": type(error).__name__, "key": key},
            ) from None
        except Exception as error:
            raise DependencyError(
                SEMANTIC_STORAGE_UNAVAILABLE,
                "Semantic source evidence could not be read.",
                context={"cause": type(error).__name__, "key": key},
            ) from None
        try:
            publication = _source_from(body)
        except Exception as error:
            raise InvariantError(
                SEMANTIC_STORAGE_MALFORMED,
                "Stored semantic source evidence is malformed.",
                context={"cause": type(error).__name__, "key": key},
            ) from None
        if publication.reference != reference:
            raise InvariantError(
                SEMANTIC_STORAGE_MALFORMED,
                "Stored semantic source evidence has a substituted identity.",
                context={"key": key},
            )
        return publication

    def canonical_rows(self, publication: SuccessfulPublication) -> tuple[CanonicalRow, ...]:
        """Read one immutable successful canonical publication by exact identity."""
        key = self._successful_key(publication)
        try:
            body = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except ClientError as error:
            if _is_missing(error):
                raise InvariantError(
                    SEMANTIC_STORAGE_MALFORMED,
                    "Current canonical publication has no immutable successful object.",
                    context={"key": key},
                ) from None
            raise DependencyError(
                SEMANTIC_STORAGE_UNAVAILABLE,
                "Canonical history evidence could not be read.",
                context={"cause": type(error).__name__, "key": key},
            ) from None
        try:
            return _canonical_rows_from(body, publication)
        except Exception as error:
            raise InvariantError(
                SEMANTIC_STORAGE_MALFORMED,
                "Stored canonical history evidence is malformed.",
                context={"cause": type(error).__name__, "key": key},
            ) from None

    def put_identity_evidence(
        self, document: IdentityMappingsDocument
    ) -> IdentityEvidenceWriteResult:
        """Write one claim-check document once, under its content-addressed key."""
        accepted, created = self._put_immutable(
            identity_evidence_key(document.claim), document.body
        )
        return IdentityEvidenceWriteResult(
            accepted=accepted,
            created=created,
            reason="" if accepted else "identity evidence key already holds other bytes.",
        )

    def identity_evidence(self, claim: IdentityMappingsClaim) -> tuple[dict[str, object], ...]:
        """Read one claim-check document and verify it; absence or a digest mismatch raises."""
        key = identity_evidence_key(claim)
        body: bytes | None
        try:
            body = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except ClientError as error:
            if not _is_missing(error):
                raise DependencyError(
                    SEMANTIC_STORAGE_UNAVAILABLE,
                    "Identity-mapping evidence could not be read.",
                    context={"cause": type(error).__name__, "key": key},
                ) from None
            body = None
        except Exception as error:
            raise DependencyError(
                SEMANTIC_STORAGE_UNAVAILABLE,
                "Identity-mapping evidence could not be read.",
                context={"cause": type(error).__name__, "key": key},
            ) from None
        return verify_identity_mappings(claim, body)

    def _put_immutable(self, key: str, body: bytes) -> tuple[bool, bool]:
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
                IfNoneMatch="*",
            )
            return True, True
        except ClientError as error:
            if not _is_precondition(error):
                raise DependencyError(
                    SEMANTIC_STORAGE_UNAVAILABLE,
                    "Semantic evidence could not be materialized.",
                    context={"cause": type(error).__name__, "key": key},
                ) from None
        except Exception as error:
            raise DependencyError(
                SEMANTIC_STORAGE_UNAVAILABLE,
                "Semantic evidence could not be materialized.",
                context={"cause": type(error).__name__, "key": key},
            ) from None
        try:
            existing = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except Exception as error:
            raise DependencyError(
                SEMANTIC_STORAGE_UNAVAILABLE,
                "Existing semantic evidence could not be verified.",
                context={"cause": type(error).__name__, "key": key},
            ) from None
        return existing == body, False

    @staticmethod
    def _source_key(reference: SourcePublicationRef) -> str:
        return f"immutable/source-aligned/{reference.value[:2]}/{reference.value}.json"

    @staticmethod
    def _successful_key(publication: SuccessfulPublication) -> str:
        dataset = publication.partition.scope.dataset.value
        return (
            f"immutable/canonical/{CanonicalDestination.SUCCESSFUL.value}/{dataset}/"
            f"{publication.publication_id.value}.json"
        )

    @staticmethod
    def _canonical_key(request: CanonicalMaterializeRequest) -> str:
        publication = request.publication
        dataset = publication.partition.scope.dataset.value
        return (
            f"immutable/canonical/{request.destination.value}/{dataset}/"
            f"{publication.publication_id.value}.json"
        )


class S3SourceAlignedAdapter:
    """Source-aligned read/write authority over immutable S3 publication objects."""

    def __init__(self, bucket: str, client: S3Client | None = None) -> None:
        self._storage = S3SemanticStorageAdapter(bucket, client)

    def materialize(self, publication: SourceAlignedPublication) -> MaterializeSourceRowsetResult:
        """Create one immutable source publication or accept an exact replay."""
        return cast("MaterializeSourceRowsetResult", self._storage.materialize(publication))

    def publication(self, reference: SourcePublicationRef) -> SourceAlignedPublication | None:
        """Read one immutable source publication by exact reference."""
        return self._storage.publication(reference)


class S3IdentityEvidenceAdapter:
    """Identity-mapping claim-check documents, write-once under their own SHA-256."""

    def __init__(self, bucket: str, client: S3Client | None = None) -> None:
        self._storage = S3SemanticStorageAdapter(bucket, client)

    def put(self, document: IdentityMappingsDocument) -> IdentityEvidenceWriteResult:
        """Create the document (`If-None-Match: *`), or accept an exact existing replay."""
        return self._storage.put_identity_evidence(document)

    def read(self, claim: IdentityMappingsClaim) -> tuple[dict[str, object], ...]:
        """Fetch the document and return it only after the digest is recomputed and matches."""
        return self._storage.identity_evidence(claim)


class S3CanonicalAdapter:
    """Canonical candidate/success write authority over immutable S3 objects."""

    def __init__(self, bucket: str, client: S3Client | None = None) -> None:
        self._storage = S3SemanticStorageAdapter(bucket, client)

    def rows(self, publication: SuccessfulPublication) -> tuple[CanonicalRow, ...]:
        """Read one exact successful publication from immutable S3 authority."""
        return self._storage.canonical_rows(publication)

    def attest(self, request: CanonicalMaterializeRequest) -> CanonicalWriteResult:
        """Compare one exact canonical object without writing or repairing it."""
        return self._storage.attest_canonical(request)

    def materialize(self, request: CanonicalMaterializeRequest) -> CanonicalWriteResult:
        """Create one immutable candidate or successful version, or accept an exact replay."""
        return cast("CanonicalWriteResult", self._storage.materialize(request))
