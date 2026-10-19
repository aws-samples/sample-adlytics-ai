# SPDX-License-Identifier: MIT-0
"""Strict JSON serialization for the producer registration contract."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import cast

from domain.producer.models import (
    ExecutionIdentity,
    IncomingObject,
    QueryPackSelection,
    ScheduleKind,
    SourceRegistration,
)
from domain.semantics.identity import SourceIdentifier

type JsonObject = dict[str, object]


def _object(value: object, label: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object.")
    return cast("JsonObject", value)


def _exact(raw: JsonObject, keys: set[str], label: str) -> None:
    if set(raw) != keys:
        raise ValueError(f"{label} requires exactly {sorted(keys)}, got {sorted(raw)}.")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonblank string.")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer.")
    return value


def registration_from_json(text: str) -> SourceRegistration:
    """Parse one versioned registration and reject missing or unknown schema fields."""
    raw = _object(cast("object", json.loads(text)), "registration")
    _exact(
        raw,
        {
            "schema_version",
            "source_system",
            "source_dataset",
            "source_scope",
            "marketplace",
            "period_start",
            "period_end",
            "source_watermark",
            "retrieved_at",
            "option_set",
            "execution",
            "query_pack",
            "object",
        },
        "registration",
    )
    execution = _object(raw["execution"], "execution")
    _exact(execution, {"execution_id", "workflow_id", "schedule_kind", "schedule_id"}, "execution")
    selection = _object(raw["query_pack"], "query_pack")
    _exact(selection, {"id", "version", "query_digest"}, "query_pack")
    object_ref = _object(raw["object"], "object")
    _exact(object_ref, {"key", "version_id"}, "object")
    source_scope = _object(raw["source_scope"], "source_scope")
    _exact(source_scope, {"namespace", "value"}, "source_scope")
    options = _object(raw["option_set"], "option_set")

    watermark = raw["source_watermark"]
    schedule_id = execution["schedule_id"]
    return SourceRegistration(
        schema_version=_text(raw["schema_version"], "schema_version"),
        source_system=_text(raw["source_system"], "source_system"),
        source_dataset=_text(raw["source_dataset"], "source_dataset"),
        source_scope=SourceIdentifier(
            _text(source_scope["namespace"], "source_scope.namespace"),
            _text(source_scope["value"], "source_scope.value"),
        ),
        marketplace=_text(raw["marketplace"], "marketplace"),
        period_start=date.fromisoformat(_text(raw["period_start"], "period_start")),
        period_end=date.fromisoformat(_text(raw["period_end"], "period_end")),
        source_watermark=(
            None if watermark is None else date.fromisoformat(_text(watermark, "source_watermark"))
        ),
        retrieved_at=datetime.fromisoformat(_text(raw["retrieved_at"], "retrieved_at")),
        option_set=options,
        execution=ExecutionIdentity(
            execution_id=_text(execution["execution_id"], "execution_id"),
            workflow_id=_text(execution["workflow_id"], "workflow_id"),
            schedule_kind=ScheduleKind(_text(execution["schedule_kind"], "schedule_kind")),
            schedule_id=None if schedule_id is None else _text(schedule_id, "schedule_id"),
        ),
        query_pack=QueryPackSelection(
            pack_id=_text(selection["id"], "query_pack.id"),
            version=_integer(selection["version"], "query_pack.version"),
            query_digest=_text(selection["query_digest"], "query_pack.query_digest"),
        ),
        object=IncomingObject(
            key=_text(object_ref["key"], "object.key"),
            version_id=_text(object_ref["version_id"], "object.version_id"),
        ),
    )


def registration_as_dict(registration: SourceRegistration) -> dict[str, object]:
    """Project domain values onto the protected JSON field names."""
    return {
        "schema_version": registration.schema_version,
        "source_system": registration.source_system,
        "source_dataset": registration.source_dataset,
        "source_scope": {
            "namespace": registration.source_scope.namespace,
            "value": registration.source_scope.value,
        },
        "marketplace": registration.marketplace,
        "period_start": registration.period_start.isoformat(),
        "period_end": registration.period_end.isoformat(),
        "source_watermark": (
            None
            if registration.source_watermark is None
            else registration.source_watermark.isoformat()
        ),
        "retrieved_at": registration.retrieved_at.isoformat(),
        "option_set": dict(registration.option_set),
        "execution": {
            "execution_id": registration.execution.execution_id,
            "workflow_id": registration.execution.workflow_id,
            "schedule_kind": registration.execution.schedule_kind.value,
            "schedule_id": registration.execution.schedule_id,
        },
        "query_pack": {
            "id": registration.query_pack.pack_id,
            "version": registration.query_pack.version,
            "query_digest": registration.query_pack.query_digest,
        },
        "object": {
            "key": registration.object.key,
            "version_id": registration.object.version_id,
        },
    }


def write_registration(registration: SourceRegistration) -> str:
    """Render deterministic registration bytes with a trailing newline."""
    return json.dumps(registration_as_dict(registration), indent=2, sort_keys=True) + "\n"
