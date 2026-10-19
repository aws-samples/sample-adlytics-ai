# SPDX-License-Identifier: MIT-0
"""Compile one verified landed delivery into typed source-aligned evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from domain.curation.landing import Manifest
from domain.curation.mapping import SourceDocument, resolve, suppression_flag
from domain.data_contracts.contract import DataContract, SourceFormat
from domain.semantics.identity import (
    DeliveryScope,
    ExtractionProfileKey,
    SourceDatasetKey,
    SourceIdentifier,
    SourceSystemKey,
)
from domain.semantics.publication import SourcePublicationRef
from domain.semantics.revision import RevisionSchemeKey, SourceRevision
from domain.semantics.source_aligned import (
    LandedDeliveryRef,
    SourceAlignedField,
    SourceAlignedPublication,
    SourceAlignedRow,
    SourceAlignedRowset,
    SourceInterpreterKey,
    SourceOption,
    SourceScalar,
)
from domain.semantics.source_pack import PackReadiness, SourcePack
from domain.semantics.time import (
    BusinessCalendarKey,
    ExtractionCoverage,
    FactTimeClass,
    MeasurementBucket,
    ObservationTimeBasis,
    TimeGrain,
)
from domain.shared_kernel.values import DateRange

__all__ = ["CompileSourceRequest", "compile_source_publication", "periodic_manifest_bucket"]


@dataclass(frozen=True, slots=True)
class CompileSourceRequest:
    """Verified delivery and source-pack interpretation to compile immutably."""

    pack: SourcePack
    contract: DataContract
    manifest: Manifest
    document: SourceDocument
    landed_delivery: LandedDeliveryRef
    published_at: datetime

    def __post_init__(self) -> None:
        if self.pack.contract_id != self.contract.contract_id:
            raise ValueError("source compiler pack and contract identities differ.")
        if self.published_at.tzinfo is None:
            raise ValueError("source compilation instant must be timezone-aware.")


def _array_name(pack: SourcePack) -> str | None:
    paths = [pack.marketplace.source_path, pack.time.start_path or "", pack.time.end_path or ""]
    if pack.product is not None:
        paths.append(pack.product.source_path)
    if pack.media is not None and pack.media.source_path is not None:
        paths.append(pack.media.source_path)
    if pack.campaign is not None:
        paths.append(pack.campaign.source_path)
    if pack.advertiser is not None:
        paths.append(pack.advertiser.source_path)
    names = {path.split("[]", 1)[0] for path in paths if "[]" in path}
    if len(names) > 1:
        raise ValueError(f"{pack.contract_id}: source pack spans more than one row array.")
    return next(iter(names), None)


def _elements(request: CompileSourceRequest) -> tuple[dict[str, Any], ...]:
    document = request.document
    if request.contract.source_format is not SourceFormat.JSON_DUAL_ARRAY:
        return document.rows
    if document.document is None:
        raise ValueError(f"{request.pack.contract_id}: nested source document is absent.")
    array = _array_name(request.pack)
    if array is None:
        raise ValueError(f"{request.pack.contract_id}: nested source pack has no row array.")
    holder = document.document.get(array)
    if not isinstance(holder, list):
        raise ValueError(f"{request.pack.contract_id}: source array {array!r} is absent.")
    items = cast("list[object]", holder)
    return tuple(cast("dict[str, Any]", item) for item in items if isinstance(item, dict))


def _value(request: CompileSourceRequest, element: dict[str, Any], path: str) -> object:
    if "[]" in path:
        _, _, remainder = path.partition("[].")
        return resolve(element, remainder)
    if request.document.document is not None:
        return resolve(request.document.document, path)
    return element.get(path.rsplit(".", 1)[-1])


def _day(value: object, *, label: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ValueError(f"{label} is not an ISO business date: {value!r}.") from None


def _scalar(value: object) -> SourceScalar:
    if value is None or isinstance(value, (str, int, float, Decimal, bool, date, datetime)):
        return value
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _flatten(value: object, prefix: str = "") -> tuple[SourceAlignedField, ...]:
    if not isinstance(value, dict):
        return ()
    fields: list[SourceAlignedField] = []
    for name, raw in sorted(cast("dict[str, object]", value).items()):
        path = f"{prefix}.{name}" if prefix else name
        if isinstance(raw, dict):
            fields.extend(_flatten(cast("dict[str, object]", raw), path))
        elif not isinstance(raw, list):
            fields.append(SourceAlignedField(path, _scalar(raw)))
    return tuple(fields)


def _reference(request: CompileSourceRequest) -> SourcePublicationRef:
    evidence = request.landed_delivery
    payload = "|".join(
        (
            request.pack.contract_id,
            evidence.manifest_key,
            evidence.manifest_version,
            evidence.manifest_checksum,
            evidence.object_key,
            evidence.object_version,
            evidence.object_checksum,
            (
                "unknown"
                if evidence.first_observed_at is None
                else evidence.first_observed_at.astimezone(UTC).isoformat()
            ),
        )
    )
    return SourcePublicationRef(f"src-{hashlib.sha256(payload.encode()).hexdigest()}")


def _suppressed(request: CompileSourceRequest, element: dict[str, object]) -> bool:
    suppression = request.contract.suppression
    if suppression is None:
        return False
    value = _value(request, element, suppression.discriminator_column)
    return suppression_flag(value)


def _measurement_bounds(
    request: CompileSourceRequest,
    element: dict[str, object],
    paths: tuple[str, str],
) -> tuple[date, date, TimeGrain]:
    start_path, end_path = paths
    start_value = _value(request, element, start_path)
    end_value = _value(request, element, end_path)
    periodic = request.pack.time.time_class is FactTimeClass.PERIODIC
    if _suppressed(request, element) and start_value in {None, ""} and end_value in {None, ""}:
        window = DateRange(request.manifest.period_start, request.manifest.period_end)
        if periodic:
            return window.start, window.end, request.pack.time.bucket_grain(window)
        grain = (
            TimeGrain.DAY
            if request.manifest.period_start == request.manifest.period_end
            else TimeGrain.CUSTOM
        )
        return request.manifest.period_start, request.manifest.period_end, grain
    if periodic:
        # A periodic row's bounds are the AMC window it was extracted over: half-open UTC
        # instants, so the period's last day is the one before the end instant (ADR-100).
        start = _instant(start_value, label=start_path)
        end = _instant(end_value, label=end_path)
        if end <= start:
            raise ValueError(f"{end_path} must be later than {start_path}.")
        window = DateRange(start.date(), (end - timedelta(microseconds=1)).date())
        manifest_period = DateRange(request.manifest.period_start, request.manifest.period_end)
        if window != manifest_period:
            # One delivery measures the one period its manifest names; a row over any other
            # window would publish a period the delivery never declared.
            raise ValueError(
                f"{start_path}..{end_path} measure {window}, but the manifest declares "
                f"{manifest_period}."
            )
        return window.start, window.end, request.pack.time.bucket_grain(window)
    return (
        _day(start_value, label=start_path),
        _day(end_value, label=end_path),
        request.pack.time.grain,
    )


def _instant(value: object, *, label: str) -> datetime:
    """Read one delivered AMC window instant, refusing anything but an exact UTC instant."""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        except ValueError:
            raise ValueError(f"{label} is not an ISO UTC instant: {value!r}.") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be a UTC instant: {value!r}.")
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class _TimeContext:
    system_observed: bool
    first_observed: datetime | None
    calendar: BusinessCalendarKey


def _time_context(request: CompileSourceRequest) -> _TimeContext:
    time = request.pack.time
    system_observed = time.observation_basis is ObservationTimeBasis.SYSTEM_FIRST_OBSERVED
    if not system_observed and (time.start_path is None or time.end_path is None):
        raise ValueError(f"{request.pack.contract_id}: source pack has no verified row time.")
    first_observed = request.landed_delivery.first_observed_at
    if system_observed and first_observed is None:
        raise ValueError(
            f"{request.pack.contract_id}: system-first-observed snapshot lacks S3 observation time."
        )
    calendar = (
        BusinessCalendarKey("technical_utc")
        if system_observed
        else BusinessCalendarKey("iso_gregorian", week_starts_on=0)
    )
    return _TimeContext(system_observed, first_observed, calendar)


def periodic_manifest_bucket(
    pack: SourcePack, contract: DataContract, manifest: Manifest
) -> MeasurementBucket:
    """Return the bucket of a periodic delivery from its manifest period alone (ADR-100).

    For a delivery that returned no row: its one expected period is still the manifest's, built
    exactly as a row over that window would be. Raises `ValueError` when the pack isn't
    periodic or the manifest period isn't one complete declared period.
    """
    if pack.time.time_class is not FactTimeClass.PERIODIC:
        raise ValueError(f"{pack.contract_id}: only a periodic pack has a manifest bucket.")
    window = DateRange(manifest.period_start, manifest.period_end)
    return MeasurementBucket(
        window=window,
        grain=pack.time.bucket_grain(window),
        calendar=BusinessCalendarKey("iso_gregorian", week_starts_on=0),
        source_timezone=contract.source_timezone,
        observation_basis=pack.time.observation_basis,
    )


def _row_measurement(
    request: CompileSourceRequest,
    element: dict[str, object],
    context: _TimeContext,
) -> MeasurementBucket:
    time = request.pack.time
    if context.system_observed:
        if context.first_observed is None:  # pragma: no cover - context validates this
            raise ValueError("system observation time disappeared during compilation.")
        observed_day = context.first_observed.astimezone(UTC).date()
        start, end, grain = observed_day, observed_day, TimeGrain.DAY
    else:
        if time.start_path is None or time.end_path is None:  # pragma: no cover - validated above
            raise ValueError("source business-time paths disappeared during compilation.")
        start, end, grain = _measurement_bounds(
            request,
            element,
            (time.start_path, time.end_path),
        )
    return MeasurementBucket(
        window=DateRange(start, end),
        grain=grain,
        calendar=context.calendar,
        source_timezone="UTC" if context.system_observed else request.contract.source_timezone,
        observation_basis=time.observation_basis,
    )


def _source_identifiers(
    request: CompileSourceRequest,
    element: dict[str, object],
) -> tuple[SourceIdentifier, ...]:
    marketplace_value = _value(request, element, request.pack.marketplace.source_path)
    identifiers: list[SourceIdentifier] = []
    if marketplace_value not in {None, ""}:
        identifiers.append(
            SourceIdentifier(request.pack.marketplace.namespace, str(marketplace_value))
        )
    if request.pack.product is not None:
        product_value = _value(request, element, request.pack.product.source_path)
        if product_value not in {None, ""}:
            identifiers.append(SourceIdentifier(request.pack.product.namespace, str(product_value)))
    media = request.pack.media
    if media is not None:
        media_value = (
            media.literal_value
            if media.literal_value is not None
            else _value(request, element, str(media.source_path))
        )
        if media_value not in {None, ""}:
            identifiers.append(SourceIdentifier(media.namespace, str(media_value)))
    for identity in (request.pack.campaign, request.pack.advertiser):
        if identity is None:
            continue
        identity_value = _value(request, element, identity.source_path)
        if identity_value not in {None, ""}:
            identifiers.append(SourceIdentifier(identity.namespace, str(identity_value)))
    return tuple(identifiers)


def _source_revision(
    request: CompileSourceRequest,
    reference: SourcePublicationRef,
    context: _TimeContext,
) -> SourceRevision:
    if context.system_observed:
        if context.first_observed is None:  # pragma: no cover - context validates this
            raise ValueError("system observation time disappeared during revision construction.")
        return SourceRevision.instant(
            scheme=RevisionSchemeKey("adlytics.raw_manifest_first_observed"),
            source_id=reference.value,
            observed_at=context.first_observed,
        )
    watermark = request.manifest.source_watermark
    if watermark is None:
        raise ValueError(
            f"{request.pack.contract_id}: source-business-date pack lacks source watermark."
        )
    return SourceRevision.dated(
        scheme=RevisionSchemeKey(f"{request.manifest.source_system}.source_watermark"),
        source_id=watermark.isoformat(),
        observed_on=watermark,
    )


def compile_source_publication(request: CompileSourceRequest) -> SourceAlignedPublication:
    """Interpret READY source rows without promoting identifiers into canonical keys."""
    if request.pack.readiness is not PackReadiness.READY:
        raise ValueError(f"{request.pack.contract_id}: blocked source pack cannot compile.")
    context = _time_context(request)
    rows: list[SourceAlignedRow] = []
    for element in _elements(request):
        measurement = _row_measurement(request, element, context)
        identifiers = _source_identifiers(request, element)
        fields = list(_flatten(element))
        if request.document.document is not None:
            document_root = request.document.document
            document_fields: dict[str, Any] = {
                str(key): value
                for key, value in document_root.items()
                if not isinstance(value, list)
            }
            fields.extend(_flatten(document_fields))
        rows.append(
            SourceAlignedRow(
                fields=tuple(fields),
                source_identifiers=identifiers,
                measurement=measurement,
            )
        )
    manifest = request.manifest
    rowset = SourceAlignedRowset(
        delivery_scope=DeliveryScope(
            source_system=SourceSystemKey(manifest.source_system),
            source_dataset=SourceDatasetKey(manifest.source_dataset),
            extraction_profile=ExtractionProfileKey(
                request.pack.profile,
                request.pack.registry_version,
            ),
            source_scope=manifest.source_scope,
            option_set_hash=manifest.option_set_hash,
        ),
        extraction_coverage=ExtractionCoverage(
            DateRange(manifest.period_start, manifest.period_end)
        ),
        interpreter=SourceInterpreterKey(
            f"source_pack.{request.pack.contract_id.replace('-', '_')}",
            request.pack.registry_version,
        ),
        source_options=tuple(
            SourceOption(name, json.dumps(value, sort_keys=True, default=str))
            for name, value in sorted(manifest.option_set.items())
        ),
        rows=tuple(rows),
    )
    reference = _reference(request)
    revision = _source_revision(request, reference, context)
    return SourceAlignedPublication(
        reference=reference,
        landed_delivery=request.landed_delivery,
        rowset=rowset,
        source_revision=revision,
        published_at=request.published_at,
    )
