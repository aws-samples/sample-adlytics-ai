# SPDX-License-Identifier: MIT-0
"""The protected execution explanation: arms, typed refusals, stages and completeness (FR-8).

`POST /api/operations/execution-explanations` accepts exactly one arm and returns one bounded
lifecycle tree that reads graph authority plus the status projection:

- `{"flow_id": "<canonical UUID>"}`: a root flow;
- `{"manifest": {"key": "<key>", "version_id": "<VersionId>"}}`: one exact manifest version;
- `{"manifest": {"key": "<key>"}}`: the key's only version (more than one is ambiguous);
- `{"plan": {"graph_id": "<64 hex>"}}`: one graph record;
- `{"node": {"graph_id": "<64 hex>", "node": "<template node>"}}`: one node of it.

Optional `limit` (1 to `MAX_PAGE`) bounds the receipt page; optional `cursor` continues it.

Required-stage completeness is the canonical ordered set difference between the protocol's
required stages and the union of applied stages and stages with exact parked evidence. A parked
successor is visible as `PARKED-SUCCESSOR`, is never itself missing, and never hides its absent
predecessor: with evidence for every stage but `plan_admitted` and a parked `units_dispatched`,
the missing set is exactly `['plan_admitted']`. Pure: no I/O, no clock, no AWS.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, cast

from domain.curation.landing import MANIFEST_NAME
from domain.operations.generated_execution_graphs import TEMPLATES
from domain.operations.graph_projection import GraphProjectionRow, ProjectionKind, epoch_label
from domain.operations.ingestion_root import RootRefusedError, window_identity
from domain.shared_kernel.errors import DomainError, ErrorCode, InvariantError, ValidationError

__all__ = [
    "DEFAULT_PAGE",
    "MAX_DOCUMENT_BYTES",
    "MAX_GRAPHS",
    "MAX_PAGE",
    "PARKED_SUCCESSOR",
    "RECEIPTS_ROUTE",
    "REMEDY_BY_PHASE",
    "REQUIRED_STAGES",
    "ExplanationArm",
    "ExplanationInvalidError",
    "ExplanationRefusal",
    "ExplanationRequest",
    "ExplanationUnavailableError",
    "StageEvidence",
    "StageStatus",
    "elapsed_ms",
    "graph_stage_evidence",
    "missing_required_stages",
    "node_failures",
    "parse_explanation_request",
    "require_document_bound",
    "stage_rows",
]

# FR-1's lifecycle vocabulary in protocol order. `unit_retrying` is optional; `unavailable`
# satisfies `baked` (FR-1: "`baked` or `unavailable`").
REQUIRED_STAGES: Final = (
    "received",
    "materialized",
    "plan_recorded",
    "plan_admitted",
    "units_dispatched",
    "unit_claimed",
    "unit_attested",
    "plan_terminal",
    "current",
    "bake_enqueued",
    "baked",
    "precompute_settled",
    "served",
)
_ALTERNATIVES: Final[Mapping[str, tuple[str, ...]]] = {"baked": ("baked", "unavailable")}
_KNOWN_STAGES: Final = frozenset({*REQUIRED_STAGES, "unit_retrying", "unavailable"})
PARKED_SUCCESSOR: Final = "PARKED-SUCCESSOR"
RECEIPTS_ROUTE: Final = "execution-explanations/receipts"
DEFAULT_PAGE: Final = 10
MAX_PAGE: Final = 25
MAX_GRAPHS: Final = 32
MAX_DOCUMENT_BYTES: Final = 262_144
_MAX_KEY_BYTES: Final = 1024
_MAX_VERSION_CHARS: Final = 1024
_ARM_KEYS: Final = frozenset({"flow_id", "manifest", "plan", "node"})
_REQUEST_KEYS: Final = frozenset({*_ARM_KEYS, "cursor", "limit"})
_GRAPH_ID: Final = re.compile(r"^[0-9a-f]{64}$")
_VERSION_ID: Final = re.compile(r"^[\x21-\x7e]+$")
_TEMPLATE_NODES: Final = frozenset(
    str(node["node"]) for template in TEMPLATES.values() for node in template["nodes"]
)


class ExplanationRefusal(StrEnum):
    """The explanation's closed typed failures."""

    INVALID_REFERENCE = "invalid_reference"
    FLOW_NOT_FOUND = "flow_not_found"
    MANIFEST_NOT_FOUND = "manifest_not_found"
    AMBIGUOUS_MANIFEST = "ambiguous_manifest"
    EXPLANATION_TOO_LARGE = "explanation_too_large"
    # Logged and counted only: the caller sees the existing absence collapse (`not_found`).
    UNAUTHORIZED_COLLAPSED = "unauthorized_collapsed"


_CODES: Final = {
    ExplanationRefusal.INVALID_REFERENCE: 1,
    ExplanationRefusal.FLOW_NOT_FOUND: 2,
    ExplanationRefusal.MANIFEST_NOT_FOUND: 3,
    ExplanationRefusal.AMBIGUOUS_MANIFEST: 4,
    ExplanationRefusal.EXPLANATION_TOO_LARGE: 5,
    ExplanationRefusal.UNAUTHORIZED_COLLAPSED: 6,
}


class ExplanationInvalidError(ValidationError):
    """A malformed, out-of-bounds or over-bound explanation request (never alarms)."""

    def __init__(self, refusal: ExplanationRefusal, description: str) -> None:
        super().__init__(ErrorCode("EXEC_EXPLAIN", _CODES[refusal]), description)
        self.refusal = refusal


class ExplanationUnavailableError(DomainError):
    """A well-formed reference whose answer is absent or ambiguous (never alarms)."""

    def __init__(self, refusal: ExplanationRefusal, description: str) -> None:
        super().__init__(ErrorCode("EXEC_EXPLAIN", _CODES[refusal]), description)
        self.refusal = refusal


class ExplanationArm(StrEnum):
    """Which reference the request named."""

    FLOW = "flow"
    MANIFEST_VERSION = "manifest_version"
    MANIFEST_KEY = "manifest_key"
    PLAN = "plan"
    NODE = "node"


@dataclass(frozen=True, slots=True)
class ExplanationRequest:
    """One validated explanation request."""

    arm: ExplanationArm
    flow_id: str | None = None
    manifest_key: str | None = None
    version_id: str | None = None
    graph_id: str | None = None
    node: str | None = None
    cursor: str | None = None
    limit: int = DEFAULT_PAGE


def _invalid(description: str) -> ExplanationInvalidError:
    return ExplanationInvalidError(ExplanationRefusal.INVALID_REFERENCE, description)


def _mapping(value: object, keys: frozenset[str], required: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _invalid("an arm must be an object.")
    document = cast("dict[object, object]", value)
    names = {name for name in document if isinstance(name, str)}
    if len(names) != len(document) or not required <= names or not names <= keys:
        raise _invalid("an arm carries another field set.")
    return {str(name): item for name, item in document.items()}


def _graph_id(value: object) -> str:
    if not isinstance(value, str) or _GRAPH_ID.fullmatch(value) is None:
        raise _invalid("graph_id must be 64 lowercase hex characters.")
    return value


def _manifest(value: object) -> tuple[str, str | None]:
    fields = _mapping(value, frozenset({"key", "version_id"}), frozenset({"key"}))
    key = fields["key"]
    if not isinstance(key, str) or not key or len(key.encode("utf-8")) > _MAX_KEY_BYTES:
        raise _invalid("the manifest key is outside its bound.")
    try:
        window_identity(key)
    except RootRefusedError:
        raise _invalid("the manifest key is not a canonical raw manifest key.") from None
    # The read role may read manifests only (S2), so no other raw object is ever named here.
    if key.rsplit("/", 1)[-1] != MANIFEST_NAME:
        raise _invalid("the key does not name a raw manifest.")
    version = fields.get("version_id")
    if version is None:
        return key, None
    if (
        not isinstance(version, str)
        or len(version) > _MAX_VERSION_CHARS
        or _VERSION_ID.fullmatch(version) is None
    ):
        raise _invalid("the version_id is outside its bound.")
    return key, version


def _limit(value: object) -> int:
    if value is None:
        return DEFAULT_PAGE
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_PAGE:
        raise _invalid(f"limit must be an integer from 1 to {MAX_PAGE}.")
    return value


def parse_explanation_request(body: object) -> ExplanationRequest:
    """Validate the body: exactly one bounded arm, an optional cursor and an optional limit."""
    if not isinstance(body, dict):
        raise _invalid("the request body must be a JSON object.")
    document = cast("dict[object, object]", body)
    names = {name for name in document if isinstance(name, str)}
    if len(names) != len(document) or not names <= _REQUEST_KEYS:
        raise _invalid("the request carries an unknown field.")
    arms = names & _ARM_KEYS
    if len(arms) != 1:
        raise _invalid("the request must name exactly one arm.")
    cursor = document.get("cursor")
    if cursor is not None and not isinstance(cursor, str):
        raise _invalid("cursor must be text.")
    limit = _limit(document.get("limit"))
    (arm,) = arms
    value = document[arm]
    if arm == "flow_id":
        if not isinstance(value, str):
            raise _invalid("flow_id must be a canonical UUID.")
        try:
            canonical = str(uuid.UUID(value))
        except ValueError:
            raise _invalid("flow_id must be a canonical UUID.") from None
        if canonical != value:
            raise _invalid("flow_id must be a canonical UUID.")
        return ExplanationRequest(ExplanationArm.FLOW, flow_id=value, cursor=cursor, limit=limit)
    if arm == "manifest":
        key, version = _manifest(value)
        return ExplanationRequest(
            ExplanationArm.MANIFEST_KEY if version is None else ExplanationArm.MANIFEST_VERSION,
            manifest_key=key,
            version_id=version,
            cursor=cursor,
            limit=limit,
        )
    if arm == "plan":
        fields = _mapping(value, frozenset({"graph_id"}), frozenset({"graph_id"}))
        return ExplanationRequest(
            ExplanationArm.PLAN, graph_id=_graph_id(fields["graph_id"]), cursor=cursor, limit=limit
        )
    fields = _mapping(value, frozenset({"graph_id", "node"}), frozenset({"graph_id", "node"}))
    node = fields["node"]
    if not isinstance(node, str) or node not in _TEMPLATE_NODES:
        raise _invalid("node must name a template node.")
    return ExplanationRequest(
        ExplanationArm.NODE,
        graph_id=_graph_id(fields["graph_id"]),
        node=node,
        cursor=cursor,
        limit=limit,
    )


class StageStatus(StrEnum):
    """Whether a stage's evidence applied, or arrived before its predecessor."""

    APPLIED = "APPLIED"
    PARKED = PARKED_SUCCESSOR


@dataclass(frozen=True, slots=True)
class StageEvidence:
    """One authority-backed stage instant; a parked one names its absent predecessor."""

    stage: str
    status: StageStatus
    activity_id: str
    authority: str
    occurred_at: datetime | None
    label: str
    admitted_at: datetime | None = None
    predecessor_stage: str | None = None
    batch_id: str | None = None
    """The batch of a unit claim/attestation row."""
    cohort_revision: str | None = None
    serving_input_revision: str | None = None
    winner_run_id: str | None = None
    binding_kind: str | None = None
    direct_binding_id: str | None = None

    def __post_init__(self) -> None:
        if self.stage not in _KNOWN_STAGES:
            raise _invalid(f"stage {self.stage!r} is not in the lifecycle vocabulary.")


def missing_required_stages(evidence: Sequence[StageEvidence]) -> list[str]:
    """Return the canonical ordered set difference (FR-8): required minus applied-or-parked."""
    seen = {item.stage for item in evidence}
    return [
        stage
        for stage in REQUIRED_STAGES
        if not any(name in seen for name in _ALTERNATIVES.get(stage, (stage,)))
    ]


def elapsed_ms(start: datetime | None, end: datetime | None) -> int | None:
    """Return whole elapsed milliseconds, or None when either instant is unknown or reversed."""
    if start is None or end is None:
        return None
    delta = end - start
    milliseconds = (delta.days * 86_400 + delta.seconds) * 1000 + delta.microseconds // 1000
    return None if milliseconds < 0 else milliseconds


def stage_rows(
    evidence: Sequence[StageEvidence], root_clock_started_at: datetime | None
) -> tuple[list[dict[str, object]], list[str]]:
    """Render stage rows in protocol order and the typed gaps their clocks leave."""
    order = {stage: index for index, stage in enumerate((*REQUIRED_STAGES, "unit_retrying"))}
    order["unavailable"] = order["baked"]
    rows: list[dict[str, object]] = []
    gaps: list[str] = []
    for item in sorted(evidence, key=lambda entry: (order[entry.stage], entry.activity_id)):
        root_elapsed = elapsed_ms(root_clock_started_at, item.occurred_at)
        if root_elapsed is None:
            if root_clock_started_at is None or item.occurred_at is None:
                gaps.append(f"{item.stage}:root_clock_unavailable")
            else:
                gaps.append(f"{item.stage}:clock_invalid")
        rows.append(
            {
                "stage": item.stage,
                "status": item.status.value,
                "activity_id": item.activity_id,
                "authority": item.authority,
                "label": item.label,
                "occurred_at": None if item.occurred_at is None else item.occurred_at.isoformat(),
                "flow_root_elapsed_ms": root_elapsed,
                "activity_admission_elapsed_ms": elapsed_ms(item.admitted_at, item.occurred_at),
                "predecessor_stage": item.predecessor_stage,
                "batch_id": item.batch_id,
                "cohort_revision": item.cohort_revision,
                "serving_input_revision": item.serving_input_revision,
                "winner_run_id": item.winner_run_id,
                "binding_kind": item.binding_kind,
                "direct_binding_id": item.direct_binding_id,
            }
        )
    return rows, gaps


_GRAPH_STAGE_AUTHORITY: Final = "execution_graph_store"
_GRAPH_EPOCH: Final = 1


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _node_stages(name: str, state: object) -> tuple[str, ...]:
    """N1 stages a terminal NODE row satisfies (design §2.10 table)."""
    kind = name.split(":", 1)[0]
    succeeded = state == "SUCCEEDED"
    serving_applied = succeeded or (state == "COALESCED" and kind in {"precompute", "bake"})
    if kind == "materialize_raw":
        # One transaction commits the materialization receipt and the unit roster (S2 A-2).
        return ("materialized", "plan_admitted") if succeeded else ()
    if kind == "write_unit":
        return ("unit_attested",) if succeeded else ()
    if kind == "publish":
        # `SUCCEEDED` implies the generation pointer flipped (§2.6).
        return ("current",) if succeeded else ()
    if kind == "bake":
        return ("baked",) if serving_applied else ("unavailable",)
    return {"grant_apply": ("plan_terminal",), "precompute": ("precompute_settled",)}.get(kind, ())


def _claim_stages(name: str, admission: object) -> tuple[tuple[str, str], ...]:
    """(stage, instant field) pairs a CLAIM row satisfies: `write_unit:*` and `bake` only."""
    kind = name.split(":", 1)[0]
    first = admission == 1
    if kind == "write_unit":
        if first:
            return (("units_dispatched", "queue_enqueued_at"), ("unit_claimed", "admitted_at"))
        return (("unit_retrying", "admitted_at"),)
    if kind == "bake" and first:
        return (("bake_enqueued", "queue_enqueued_at"),)
    return ()


def graph_stage_evidence(rows: Sequence[GraphProjectionRow]) -> list[StageEvidence]:
    """Map epoch-1 projection rows to N1 stage evidence (S6 part 2, design §2.10 table).

    `plan_recorded` is the GRAPH row, at the recorded time of the receipt naming its graph;
    `materialized` and `plan_admitted` both come from `materialize_raw`'s `SUCCEEDED` row; the
    `write_unit:*` and `bake` CLAIM rows give the dispatch and claim stages (a RECLAIM is
    `unit_retrying`); terminal NODE rows give the rest, and the SERVING row gives `served`.
    Epoch-0 rows are shadow observation and never count as lifecycle progress (V-5). The
    received stage is the root clock's, which the caller supplies. Pure.
    """
    recorded = {
        str(row.document.get("graph_id")): _instant(row.document.get("recorded_at"))
        for row in rows
        if row.kind is ProjectionKind.RECEIPT
    }
    evidence: list[StageEvidence] = []

    def add(  # noqa: PLR0913, PLR0917 -- one evidence row's authority facts
        stage: str,
        activity: str,
        at: object,
        admitted: object = None,
        batch: object = None,
        serving: Mapping[str, object] | None = None,
    ) -> None:
        serving = serving or {}

        def text(name: str) -> str | None:
            value = serving.get(name)
            return value if isinstance(value, str) else None

        evidence.append(
            StageEvidence(
                stage=stage,
                status=StageStatus.APPLIED,
                activity_id=activity,
                authority=_GRAPH_STAGE_AUTHORITY,
                occurred_at=_instant(at) if not isinstance(at, datetime) else at,
                label=epoch_label(_GRAPH_EPOCH),
                admitted_at=_instant(admitted),
                batch_id=batch if isinstance(batch, str) and stage in _BATCH_STAGES else None,
                cohort_revision=text("cohort_revision"),
                serving_input_revision=text("serving_input_revision"),
                winner_run_id=text("winner_run_id"),
                binding_kind=text("binding_kind"),
                direct_binding_id=text("direct_binding_id"),
            )
        )

    for row in rows:
        if row.authority_epoch != _GRAPH_EPOCH:
            continue
        document = row.document
        if row.kind is ProjectionKind.GRAPH:
            graph = str(document.get("graph_id"))
            add("plan_recorded", graph, recorded.get(graph))
        elif row.kind is ProjectionKind.NODE:
            name = str(document.get("node"))
            for stage in _node_stages(name, document.get("state")):
                add(
                    stage,
                    f"{document.get('run_id')}/{name}",
                    document.get("settled_at"),
                    batch=document.get("batch_id"),
                    serving=document,
                )
        elif row.kind is ProjectionKind.CLAIM:
            name = str(document.get("node"))
            activity = f"{document.get('node_id')}#{document.get('admission')}"
            for stage, field in _claim_stages(name, document.get("admission")):
                admitted = document.get("queue_enqueued_at") if field == "admitted_at" else None
                add(stage, activity, document.get(field), admitted, document.get("batch_id"))
        elif row.kind is ProjectionKind.SERVING:
            add(
                "served",
                str(document.get("grant_id")),
                document.get("bound_at"),
                serving=document,
            )
    return evidence


_BATCH_STAGES: Final = frozenset({"unit_claimed", "unit_retrying", "unit_attested"})
"""Slice 7 (D37): the stage rows that name a batch; every other row's `batch_id` is null."""
REMEDY_BY_PHASE: Final[Mapping[str, str]] = {"PRE_GRANT": "operator_rerun"}
"""D35: a `FAILED` PRE_GRANT node's remedy is decision row 11's `operator_rerun`."""
_REMEDY_UNMAPPED: Final = ErrorCode("FLOW_EXPLAIN", 1)


def node_failures(rows: Sequence[GraphProjectionRow]) -> list[dict[str, object]]:
    """One entry per terminal `FAILED` epoch-1 NODE row, with its closed remedy (D35).

    A `FAILED` node whose phase has no remedy is an `InvariantError`, never an empty remedy.
    """
    failures: list[dict[str, object]] = []
    for row in rows:
        if row.kind is not ProjectionKind.NODE or row.authority_epoch != _GRAPH_EPOCH:
            continue
        document = row.document
        if document.get("state") != "FAILED":
            continue
        phase = str(document.get("phase"))
        remedy = REMEDY_BY_PHASE.get(phase)
        if remedy is None:
            raise InvariantError(
                _REMEDY_UNMAPPED,
                "a FAILED node's phase has no remedy in the closed map.",
                context={"phase": phase, "node": str(document.get("node"))},
            )
        batch = document.get("batch_id")
        failures.append(
            {
                "node": document.get("node"),
                "node_id": document.get("node_id"),
                "failure_reason": document.get("failure_reason"),
                "failure_code": document.get("failure_code"),
                "batch_id": batch if isinstance(batch, str) else None,
                "remedy": remedy,
            }
        )
    return sorted(failures, key=lambda item: (str(item["node"]), str(item["node_id"])))


def require_document_bound(document: Mapping[str, object], graphs: int) -> None:
    """Refuse a tree beyond the generated graph count or the response byte bound."""
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"))
    if graphs > MAX_GRAPHS or len(encoded.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise ExplanationInvalidError(
            ExplanationRefusal.EXPLANATION_TOO_LARGE, "the explanation exceeds its bound."
        )
