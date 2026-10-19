# SPDX-License-Identifier: MIT-0
"""Exact-key DynamoDB insight-cache supersession behavior."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError
from dynamodb_reserved_words import guard_reserved_words
from insight_key_support import FICTIONAL_INPUT_DIGEST

from adapters.outbound.dynamodb.insight_cache import (
    DynamoInsightCache,
    InsightCacheDegradeRequest,
)
from domain.presentation.insights import (
    InsightCacheKey,
    InsightDeepAnalysis,
    InsightDeepDiveState,
    InsightDeepRecord,
    InsightOutputClass,
    InsightPromptTier,
    InsightSlot,
    InsightSlotId,
    InsightSlotState,
)
from ports.outbound.insights import InsightCacheSupersessionRequest

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-068")]


def _requests() -> list[dict[str, object]]:
    return []


@guard_reserved_words
@dataclass
class RecordingDynamo:
    conflict: bool = False
    requests: list[dict[str, object]] = field(default_factory=_requests)

    def update_item(self, **kwargs: object) -> dict[str, object]:
        self.requests.append(dict(kwargs))
        if self.conflict:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "stale"}},
                "UpdateItem",
            )
        return {}


def _key(*, revision: str = "revision-old", prompt: str = "prompt-v1") -> InsightCacheKey:
    return InsightCacheKey(
        analytical_scope_id="analytical-scope-v1:scope-fictional",
        subject="campaign-fictional",
        window_start="2026-08-01",
        window_end="2026-08-07",
        artifact_revision=revision,
        prompt_version=prompt,
        slot_id=InsightSlotId.CONTRIBUTION,
        input_digest=FICTIONAL_INPUT_DIGEST,
    )


def _request() -> InsightCacheSupersessionRequest:
    return InsightCacheSupersessionRequest(
        stale_key=_key(),
        successor_key=_key(revision="revision-current"),
        superseded_at=1_800_000_000,
    )


def test_supersession_updates_only_the_exact_stale_key_with_numeric_ttl() -> None:
    client = RecordingDynamo()
    result = DynamoInsightCache("insight-cache", client=cast(Any, client)).supersede(_request())

    assert result.superseded is True
    assert client.requests == [
        {
            "TableName": "insight-cache",
            "Key": {
                "ownership_key": {"S": _request().stale_key.ownership_key},
                "content_key": {"S": _request().stale_key.content_key},
            },
            "UpdateExpression": "SET #superseded_at = :superseded_at",
            "ConditionExpression": (
                "attribute_exists(#ownership_key) AND attribute_exists(#content_key) "
                "AND #record_type = :record_type AND attribute_not_exists(#superseded_at)"
            ),
            "ExpressionAttributeNames": {
                "#ownership_key": "ownership_key",
                "#content_key": "content_key",
                "#record_type": "record_type",
                "#superseded_at": "superseded_at",
            },
            "ExpressionAttributeValues": {
                ":record_type": {"S": "INSIGHT_CACHE"},
                ":superseded_at": {"N": "1800000000"},
            },
        }
    ]
    assert _request().successor_key.content_key not in str(client.requests[0])


def test_supersession_reports_a_conditional_conflict_without_retrying() -> None:
    client = RecordingDynamo(conflict=True)
    result = DynamoInsightCache("insight-cache", client=cast(Any, client)).supersede(_request())

    assert result.superseded is False
    assert len(client.requests) == 1


def test_supersession_request_rejects_current_or_unrelated_keys_and_non_numeric_ttl() -> None:
    request = _request()
    with pytest.raises(ValueError, match="distinct successor"):
        replace(request, successor_key=request.stale_key)
    with pytest.raises(ValueError, match="exact logical slot"):
        replace(request, successor_key=replace(request.successor_key, subject="other-subject"))
    with pytest.raises(ValueError, match="positive integer"):
        replace(request, superseded_at=cast(int, True))


@guard_reserved_words
@dataclass
class LifecycleDynamo:
    items: dict[tuple[str, str], dict[str, object]] = field(
        default_factory=dict[tuple[str, str], dict[str, object]]
    )
    updates: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    queries: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    query_pages: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    query_error: Exception | None = None

    def put_item(self, **kwargs: object) -> dict[str, object]:
        item = cast("dict[str, dict[str, str]]", kwargs["Item"])
        self.items[(item["ownership_key"]["S"], item["content_key"]["S"])] = cast(
            "dict[str, object]", item
        )
        return {}

    def query(self, **kwargs: object) -> dict[str, object]:
        self.queries.append(dict(kwargs))
        if self.query_error is not None:
            raise self.query_error
        if self.query_pages:
            return self.query_pages.pop(0)
        return {"Items": list(self.items.values())}

    def update_item(self, **kwargs: object) -> dict[str, object]:
        self.updates.append(dict(kwargs))
        key = cast("dict[str, dict[str, str]]", kwargs["Key"])
        item = self.items[(key["ownership_key"]["S"], key["content_key"]["S"])]
        values = cast("dict[str, dict[str, str]]", kwargs["ExpressionAttributeValues"])
        item["superseded_at"] = values[":superseded_at"]
        return {}

    def get_item(self, **kwargs: object) -> dict[str, object]:
        key = cast("dict[str, dict[str, str]]", kwargs["Key"])
        item = self.items.get((key["ownership_key"]["S"], key["content_key"]["S"]))
        return {} if item is None else {"Item": item}


def _slot(key: InsightCacheKey) -> InsightSlot:
    return InsightSlot(
        key.slot_id,
        InsightOutputClass.PERSPECTIVE,
        InsightSlotState.SILENT,
        None,
        None,
        key.artifact_revision,
        key.prompt_version,
    )


def test_successful_cache_write_does_not_infer_sibling_revision_supersession() -> None:
    """Ordered segments are simultaneously serveable exact revisions of one page window."""
    client = LifecycleDynamo(
        query_error=ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "Query"
        )
    )
    cache = DynamoInsightCache("insight-cache", client=cast(Any, client))
    sibling = _key(revision="revision-segment-one", prompt="prompt-v1")
    current = _key(revision="revision-segment-zero", prompt="prompt-v1")

    cache.put_if_absent(_slot(sibling), sibling)
    result = cache.put_if_absent(_slot(current), current)

    assert result.stored is True
    assert client.queries == []
    assert client.updates == []
    assert "superseded_at" not in client.items[(sibling.ownership_key, sibling.content_key)]
    assert "superseded_at" not in client.items[(current.ownership_key, current.content_key)]


def _stored_item(key: InsightCacheKey) -> dict[str, object]:
    return {
        "ownership_key": {"S": key.ownership_key},
        "content_key": {"S": key.content_key},
        "record_type": {"S": "INSIGHT_CACHE"},
        "slot_id": {"S": key.slot_id.value},
        "output_class": {"S": InsightOutputClass.PERSPECTIVE.value},
        "state": {"S": InsightSlotState.SILENT.value},
        "artifact_revision": {"S": key.artifact_revision},
        "prompt_version": {"S": key.prompt_version},
    }


def test_exact_key_with_retention_marker_remains_readable_until_physical_deletion() -> None:
    """Retention time cannot invalidate content whose complete requested identity still matches."""
    client = LifecycleDynamo()
    key = _key()
    item = _stored_item(key)
    item["superseded_at"] = {"N": "1800000300"}
    client.items[(key.ownership_key, key.content_key)] = item

    slot = DynamoInsightCache("insight-cache", client=cast(Any, client)).get(key)

    assert slot == _slot(key)


@guard_reserved_words
@dataclass
class ConditionalLifecycleDynamo(LifecycleDynamo):
    """A LifecycleDynamo that honors the adapter's two conditional put forms."""

    def put_item(self, **kwargs: object) -> dict[str, object]:
        item = cast("dict[str, dict[str, str]]", kwargs["Item"])
        identity = (item["ownership_key"]["S"], item["content_key"]["S"])
        condition = str(kwargs.get("ConditionExpression", ""))
        existing = self.items.get(identity)
        if "attribute_not_exists" in condition and existing is not None:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
                "PutItem",
            )
        if "#state = :unavailable" in condition:
            state = cast("dict[str, dict[str, str]]", existing or {}).get("state", {}).get("S")
            if existing is None or state != "unavailable":
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException", "Message": "state"}},
                    "PutItem",
                )
        self.items[identity] = cast("dict[str, object]", item)
        return {}


def _filled(key: InsightCacheKey) -> InsightSlot:
    return InsightSlot(
        key.slot_id,
        InsightOutputClass.PERSPECTIVE,
        InsightSlotState.FILLED,
        "A fictional narration.",
        "A fictional evidence line.",
        key.artifact_revision,
        key.prompt_version,
    )


def test_degrade_write_is_create_only_and_converges_without_clobbering_a_filled_row() -> None:
    client = ConditionalLifecycleDynamo()
    cache = DynamoInsightCache("insight-cache", client=cast(Any, client))
    key = _key()

    first = cache.put_unavailable_if_absent(
        InsightCacheDegradeRequest(key, InsightOutputClass.PERSPECTIVE, "ADLY-INSIGHT-0012")
    )
    repeated = cache.put_unavailable_if_absent(
        InsightCacheDegradeRequest(key, InsightOutputClass.PERSPECTIVE, "untyped_RuntimeError")
    )

    assert first.stored
    assert first.slot.state is InsightSlotState.UNAVAILABLE
    stored_item = client.items[(key.ownership_key, key.content_key)]
    assert stored_item["unavailable_reason"] == {"S": "ADLY-INSIGHT-0012"}
    assert not repeated.stored
    assert repeated.slot.state is InsightSlotState.UNAVAILABLE
    # The first reason survives: a degrade write never replaces an existing row.
    assert stored_item["unavailable_reason"] == {"S": "ADLY-INSIGHT-0012"}

    filled_key = _key(revision="revision-filled")
    cache.put_if_absent(_filled(filled_key), filled_key)
    degrade_after_fill = cache.put_unavailable_if_absent(
        InsightCacheDegradeRequest(filled_key, InsightOutputClass.PERSPECTIVE, "late_failure")
    )
    assert not degrade_after_fill.stored
    assert degrade_after_fill.slot.state is InsightSlotState.FILLED


def test_successful_fill_replaces_only_a_degraded_placeholder_never_a_generated_row() -> None:
    """A degrade row is a placeholder, never a tombstone (ADR-068 amendment)."""
    client = ConditionalLifecycleDynamo()
    cache = DynamoInsightCache("insight-cache", client=cast(Any, client))
    key = _key()
    cache.put_unavailable_if_absent(
        InsightCacheDegradeRequest(key, InsightOutputClass.PERSPECTIVE, "ADLY-INSIGHT-0012")
    )

    healed = cache.put_if_absent(_filled(key), key)

    assert healed.stored
    assert healed.slot.state is InsightSlotState.FILLED
    healed_item = client.items[(key.ownership_key, key.content_key)]
    assert healed_item["state"] == {"S": "filled"}
    assert "unavailable_reason" not in healed_item

    competing = cache.put_if_absent(_filled(key), key)
    assert not competing.stored
    assert competing.slot.state is InsightSlotState.FILLED


@guard_reserved_words
@dataclass
class DeepDynamo:
    item: dict[str, object] | None = None
    puts: list[dict[str, object]] = field(default_factory=list[dict[str, object]])

    def get_item(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        return {} if self.item is None else {"Item": self.item}

    def put_item(self, **kwargs: object) -> None:
        self.puts.append(dict(kwargs))
        self.item = cast("dict[str, object]", kwargs["Item"])


@pytest.mark.covers("ADR-076")
def test_cache_keys_carry_the_input_digest_and_deep_keys_their_deep_input_revision() -> None:
    """ADR-076 decision 1 (2026-10-05): every row moves once; each later input change moves it."""
    card = _key()
    assert card.content_key == (
        "WINDOW#2026-08-01#2026-08-07#REVISION#revision-old#FILTER#all"
        f"#PROMPT#prompt-v1#SLOT#contribution#INPUT#{FICTIONAL_INPUT_DIGEST}"
    )
    deep = replace(card, tier=InsightPromptTier.DEEP, deep_input_revision="2026-10-05.1")
    assert deep.content_key == f"{card.content_key}#TIER#deep#DEEPINPUT#2026-10-05.1"
    # A different input is a different row on both tiers.
    other = replace(card, input_digest="e" * 64)
    assert other.content_key != card.content_key
    assert replace(deep, input_digest="e" * 64).content_key != deep.content_key


@pytest.mark.covers("ADR-076")
def test_cache_key_refuses_a_malformed_digest_or_a_misplaced_deep_revision() -> None:
    card = _key()
    for digest in ("F" * 64, "f" * 63, "g" * 64, ""):
        with pytest.raises(ValueError, match="64 lowercase hex"):
            replace(card, input_digest=digest)
    with pytest.raises(ValueError, match="requires its deep-input revision"):
        replace(card, tier=InsightPromptTier.DEEP)
    with pytest.raises(ValueError, match="cannot carry a deep-input revision"):
        replace(card, deep_input_revision="2026-10-05.1")


@pytest.mark.covers("ADR-076")
def test_deep_cache_round_trips_the_structured_analysis_under_a_deep_only_record_type() -> None:
    key = replace(_key(), tier=InsightPromptTier.DEEP, deep_input_revision="2026-10-05.1")
    record = InsightDeepRecord(
        slot_id=key.slot_id,
        state=InsightDeepDiveState.FILLED,
        analysis=InsightDeepAnalysis(
            "A fictional movement stands out.",
            ("Reading one: it may be temporary.",),
            "The next delivered window would discriminate.",
            ("5",),
        ),
        absence_reason=None,
        artifact_revision=key.artifact_revision,
        prompt_version=key.prompt_version,
    )
    client = DeepDynamo()
    cache = DynamoInsightCache("insight-cache", client=cast(Any, client))

    written = cache.put_deep_if_absent(record, key)
    read = cache.get_deep(key)

    assert written.stored is True
    assert read == record
    assert client.item is not None
    assert cast("dict[str, str]", client.item["record_type"])["S"] == "INSIGHT_DEEP_CACHE"
    assert cast("dict[str, str]", client.item["content_key"])["S"] == key.content_key
    assert key.content_key.endswith("#TIER#deep#DEEPINPUT#2026-10-05.1")


@pytest.mark.covers("ADR-127")
def test_filter_siblings_of_one_revision_cache_apart_and_a_prompt_moves_both() -> None:
    """Every filter of a preset shares one revision, so the filter must be in the key itself.

    Without it, Amazon DSP's card and All channels' card for the same slot and subject would
    read one row, and an identical input would serve one sibling's narration on the other.
    """
    everything = _key()
    dsp = replace(everything, filter_id="amazon_dsp")
    assert everything.filter_id == "all"
    assert (everything.artifact_revision, everything.slot_id, everything.subject) == (
        dsp.artifact_revision,
        dsp.slot_id,
        dsp.subject,
    )
    assert everything.ownership_key == dsp.ownership_key
    assert everything.content_key != dsp.content_key
    assert "#FILTER#amazon_dsp#" in dsp.content_key
    # Activating a new prompt version moves both siblings off their cached rows.
    activated = {replace(key, prompt_version="prompt-v2").content_key for key in (everything, dsp)}
    assert activated.isdisjoint({everything.content_key, dsp.content_key})
    assert len(activated) == 2
    with pytest.raises(ValueError, match="nonblank"):
        replace(everything, filter_id=" ")
