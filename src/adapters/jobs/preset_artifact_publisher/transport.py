# SPDX-License-Identifier: MIT-0
"""Strict, declared-only FIFO transport for preset-artifact bake intents."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, cast

import boto3

from adapters.jobs.presentation_validation.loader import load_v2_page_schemas
from domain.operations.generated_execution_graphs import BAKE_TASKS, BAKE_V3, ROUTES
from domain.shared_kernel.errors import DependencyError, ErrorCode

if TYPE_CHECKING:  # pragma: no cover
    from pathlib import Path

    from types_boto3_sqs import SQSClient

__all__ = [
    "BAKE_FINALIZE_KIND",
    "BAKE_TASK_KIND",
    "BakeDelivery",
    "BakeEnvelopeError",
    "BakeIntent",
    "BakeIntentSender",
    "BakeV3Body",
    "DeclaredBakeIntents",
    "SqsBakeIntentSender",
    "bake_finalize_body",
    "bake_task_body",
    "load_declared_bake_intents",
    "parse_bake_v3_body",
]

_BAKE_INTENT_UNWRITABLE: Final = ErrorCode("PRESET_ARTIFACT_BAKE_INTENT", 1)
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_VARIANT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
"""A variant is a declared preset id; calendar preset ids carry underscores (ADR-100)."""
MESSAGE_GROUP_ID_MAX: Final = 128
"""The SQS `MessageGroupId` length cap (SendMessage API reference; A5.2 item 4)."""


@dataclass(frozen=True, slots=True)
class BakeIntent:
    """One declared page/variant bake request with no event-derived authority."""

    page: str
    variant: str

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.page) or not _VARIANT.fullmatch(self.variant):
            raise ValueError("preset artifact bake intent fields must be lowercase identifiers.")
        if len(self.page) + 1 + len(self.variant) > MESSAGE_GROUP_ID_MAX:
            raise ValueError("a preset artifact bake intent's message group id exceeds 128.")

    @property
    def message_group_id(self) -> str:
        """Serialize changes for exactly one declared page/variant."""
        return f"{self.page}#{self.variant}"

    def serialize(self) -> str:
        """Render the one stable, content-addressable FIFO body."""
        return json.dumps(
            {"page": self.page, "variant": self.variant},
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def parse(cls, raw: object) -> BakeIntent | None:
        """Accept only the closed two-field FIFO envelope."""
        decoded = _decode_document(raw)
        if decoded is None:
            return None
        page, variant = decoded.get("page"), decoded.get("variant")
        if not isinstance(page, str) or not isinstance(variant, str):
            return None
        try:
            return cls(page, variant)
        except ValueError:
            return None


def _decode_document(raw: object) -> dict[str, object] | None:
    if not isinstance(raw, str):
        return None
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    document = cast("dict[str, object]", decoded)
    return document if set(document) == {"page", "variant"} else None


class BakeIntentSender(Protocol):
    """Durably admit one trusted page/variant intent to the FIFO transport."""

    def send(self, intent: BakeIntent) -> None:
        """Return only after durable admission or raise a retryable transport failure."""
        ...


@dataclass(frozen=True, slots=True)
class DeclaredBakeIntents:
    """The finite page/variant set governed by the typed presentation declaration."""

    intents: tuple[BakeIntent, ...]

    def __post_init__(self) -> None:
        if not self.intents:
            raise ValueError("preset artifact publisher requires declared bake intents.")
        if len(set(self.intents)) != len(self.intents):
            raise ValueError("preset artifact publisher declaration repeats a bake intent.")

    def contains(self, intent: BakeIntent) -> bool:
        """Whether an independently parsed envelope names a declared target."""
        return intent in self.intents


class SqsBakeIntentSender:
    """Typed SQS FIFO writer with stable content-derived deduplication."""

    def __init__(self, queue_url: str, client: SQSClient | None = None) -> None:
        if not queue_url.strip():
            raise ValueError("preset artifact bake queue URL cannot be blank.")
        self._queue_url = queue_url
        self._client = client

    @property
    def _sqs(self) -> SQSClient:
        if self._client is None:
            self._client = boto3.client("sqs")
        return self._client

    def send(self, intent: BakeIntent) -> None:
        """Send deterministic content with one FIFO ordering group per page/variant."""
        body = intent.serialize()
        try:
            response = self._sqs.send_message(
                QueueUrl=self._queue_url,
                MessageBody=body,
                MessageGroupId=intent.message_group_id,
                MessageDeduplicationId=hashlib.sha256(body.encode()).hexdigest(),
            )
        except Exception as error:
            raise DependencyError(
                _BAKE_INTENT_UNWRITABLE,
                "Preset artifact bake intent could not be admitted.",
                context={"cause": type(error).__name__},
            ) from None
        if not response.get("MessageId"):
            raise DependencyError(
                _BAKE_INTENT_UNWRITABLE,
                "Preset artifact bake intent admission returned no delivery reference.",
                context={"cause": "missing_message_id"},
            )


def load_declared_bake_intents(root: Path) -> DeclaredBakeIntents:
    """Derive every target only from the validated V2 page declarations.

    A preset page bakes each candidate preset; a vintage page (ADR-100 point 8) bakes its
    policy's default, `latest_with_current_publication`, the vintage it opens on.
    """
    schemas = load_v2_page_schemas(root)
    intents = tuple(
        BakeIntent(page.page_id, variant)
        for page in schemas.pages
        for variant in (
            (page.vintage_policy.default,)
            if page.vintage_policy is not None
            else page.temporal_policy.candidate_presets
        )
    )
    return DeclaredBakeIntents(intents)


# --- GRAPH bake V3 envelopes (plan 4.6; design DB1, DB4) -----------------------------------

BAKE_TASK_KIND: Final = str(BAKE_V3["task_message_kind"])
"""`graph_bake_task_v3`: the only task body the graph bake path parses."""
BAKE_FINALIZE_KIND: Final = "bake_finalize"
_BINDING: Final = frozenset({"run_id", "node_id", "dispatch_id"})
_OWNER: Final = frozenset(
    {
        "grant_id",
        "granted_run",
        "conflict_unit",
        "served_revision",
        "root_flow_id",
        "cohort_revision",
    }
)
_TASK_FIELDS: Final = frozenset(
    {"schema_version", "kind", "post_grant", "page", "variant", "lane", "manifest_digest"}
    | {"plan_count"}
    | _OWNER
)
_FINALIZE_FIELDS: Final = frozenset(
    {"schema_version", "kind", "post_grant", "manifest_digest", "roster_digest"} | _OWNER
)
_DIGEST: Final = re.compile(r"^sha256:[0-9a-f]{64}$")


class BakeEnvelopeError(ValueError):
    """A V3 bake body that is not exactly its closed shape; refused before any side effect."""


def bake_task_body(  # noqa: PLR0913 -- the closed task body, every field explicit
    owner: dict[str, str],
    *,
    page: str,
    variant: str,
    lane: str,
    manifest_digest: str,
    plan_count: int,
) -> str:
    """Render one `graph_bake_task_v3` owner body (the binding is added at dispatch)."""
    return json.dumps(
        {
            "schema_version": "3",
            "kind": BAKE_TASK_KIND,
            "post_grant": "bake_task",
            **owner,
            "page": page,
            "variant": variant,
            "lane": lane,
            "manifest_digest": manifest_digest,
            "plan_count": plan_count,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def bake_finalize_body(owner: dict[str, str], *, manifest_digest: str, roster_digest: str) -> str:
    """Render the `bake_finalize` control's owner body."""
    return json.dumps(
        {
            "schema_version": "3",
            "kind": BAKE_FINALIZE_KIND,
            "post_grant": "bake_finalize",
            **owner,
            "manifest_digest": manifest_digest,
            "roster_digest": roster_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True, slots=True)
class BakeV3Body:
    """One parsed V3 task or finalize body: its kind, binding, owner and task fields."""

    kind: str
    run_id: str
    node_id: str
    dispatch_id: str
    owner: dict[str, str]
    manifest_digest: str
    page: str | None = None
    variant: str | None = None
    lane: str | None = None
    plan_count: int | None = None


def parse_bake_v3_body(raw: object) -> BakeV3Body | None:
    """Parse a V3 task or finalize body; None when it is neither (a fan-out or legacy body).

    A body naming either V3 kind must be exactly that kind's closed fields plus the dispatch
    binding; anything else raises `BakeEnvelopeError` before any side effect.
    """
    if not isinstance(raw, str):
        return None
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    body = cast("dict[str, object]", decoded)
    kind = body.get("kind")
    if kind not in {BAKE_TASK_KIND, BAKE_FINALIZE_KIND}:
        return None
    expected = (_TASK_FIELDS if kind == BAKE_TASK_KIND else _FINALIZE_FIELDS) | _BINDING
    if frozenset(body) != expected or body.get("schema_version") != "3":
        raise BakeEnvelopeError("a V3 bake body is not its closed shape.")
    texts = {name: body[name] for name in expected if name != "plan_count"}
    if any(not isinstance(value, str) or not value for value in texts.values()):
        raise BakeEnvelopeError("a V3 bake body field is blank or not text.")
    typed = cast("dict[str, str]", texts)
    if not _DIGEST.fullmatch(typed["manifest_digest"]):
        raise BakeEnvelopeError("a V3 bake body's manifest digest is malformed.")
    owner = {name: typed[name] for name in _OWNER}
    common = {
        "run_id": typed["run_id"],
        "node_id": typed["node_id"],
        "dispatch_id": typed["dispatch_id"],
        "owner": owner,
        "manifest_digest": typed["manifest_digest"],
    }
    if kind == BAKE_FINALIZE_KIND:
        if typed["post_grant"] != "bake_finalize":
            raise BakeEnvelopeError("a finalize body names another node.")
        return BakeV3Body(BAKE_FINALIZE_KIND, **common)  # type: ignore[arg-type]
    count = body.get("plan_count")
    if typed["post_grant"] != "bake_task" or not isinstance(count, int) or isinstance(count, bool):
        raise BakeEnvelopeError("a task body names another node or a non-integer count.")
    if typed["lane"] not in BAKE_TASKS["lane_groups"]:
        raise BakeEnvelopeError("a task body names an undeclared lane.")
    try:
        BakeIntent(typed["page"], typed["variant"])
    except ValueError:
        raise BakeEnvelopeError("a task body names a malformed page or variant.") from None
    return BakeV3Body(
        BAKE_TASK_KIND,
        **common,  # type: ignore[arg-type]
        page=typed["page"],
        variant=typed["variant"],
        lane=typed["lane"],
        plan_count=count,
    )


@dataclass(frozen=True, slots=True)
class BakeDelivery:
    """One received V3 task or finalize message: its parsed body, bytes and receive."""

    body: BakeV3Body
    body_text: str
    message_id: str
    receive_count: int
    sent_timestamp: str | None = None
    first_receive_timestamp: str | None = None
    receipt_handle: str = ""
    """This receive's handle: the finalizer's armed nack shortens its redelivery with it."""
    queue_url: str = ""
    """The bake queue, from the record's `eventSourceARN`; empty when the record names none."""

    @property
    def final(self) -> bool:
        """Whether this is the bake route's last receive before the dead letter."""
        return self.receive_count >= int(ROUTES["bake"]["max_receive_count"])
