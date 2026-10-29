# SPDX-License-Identifier: MIT-0
"""Capability-free preset-artifact contracts and conditional persistence planning."""

from __future__ import annotations

import threading
import time
from collections.abc import Generator
from concurrent.futures import ALL_COMPLETED, wait
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import pytest
from preset_artifact_support import template_of

from adapters.outbound.dynamodb.items import ItemError
from adapters.outbound.dynamodb.preset_artifacts import (
    _pointer_from_item,  # pyright: ignore[reportPrivateUsage]
    _pointer_item,  # pyright: ignore[reportPrivateUsage]
    pointer_transaction_items,
)
from adapters.outbound.preset_artifacts import PresetArtifactPersistence
from adapters.outbound.telemetry.span_context import correlated
from domain.operations.bake_memory import BAKE_WRITE_WIDTH
from domain.presentation.preset_artifacts import (
    MAX_PRESET_ARTIFACT_BYTES,
    PRESET_ARTIFACT_POINTER_CONFLICT,
    PRESET_BODY_CEILING,
    RETAIL_BODY_CEILING,
    ArtifactBodyWriteResult,
    CustodyManifestWriteResult,
    PresetArtifactBody,
    PresetArtifactBodyCeilingError,
    PresetArtifactCustodyHeader,
    PresetArtifactCustodyManifest,
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactPointerAdvanceOutcome,
    PresetArtifactPointerAdvanceRequest,
    PresetArtifactPointerAdvanceResult,
    PresetArtifactPointerRosterRequest,
    PresetArtifactPointerRosterResult,
    PresetArtifactPublicationRequest,
    PresetArtifactRevision,
    PresetArtifactSiblingBody,
    PresetArtifactSiblingCustody,
    PresetArtifactSiblingCustodyWriteResult,
    PresetArtifactSiblingIdentity,
    PresetArtifactSiblingReadRequest,
    PresetArtifactSiblingStream,
    PresetArtifactSiblingWriteResult,
    PresetArtifactVariant,
    canonical_preset_artifact_json,
    classify_conditional_pointer_failure,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-070")]

PAGE = PresetArtifactPage("campaign-portfolio")
VARIANT = PresetArtifactVariant("last-30-days")
NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def cohort_correlation() -> Generator[None]:
    with correlated("run-fictional", "task-fictional", "cohort-fictional"):
        yield


def _body(identity: str = "publication-a") -> PresetArtifactBody:
    revision = PresetArtifactRevision.from_publication_identities((identity,))
    return PresetArtifactBody(
        PAGE,
        VARIANT,
        revision,
        canonical_preset_artifact_json({"kind": "portfolio", "source": identity}),
    )


def _pointer(
    body: PresetArtifactBody,
    *,
    token: int = 1,
    supersedes: PresetArtifactRevision | None = None,
) -> PresetArtifactPointer:
    return PresetArtifactPointer(
        body.page,
        body.variant,
        body.revision,
        body.digest,
        token,
        supersedes,
    )


def test_revision_is_order_independent_and_body_key_is_immutable() -> None:
    revision = PresetArtifactRevision.from_publication_identities(
        ("publication-b", "publication-a")
    )
    body = _body("publication-a")

    assert revision == PresetArtifactRevision.from_publication_identities(
        ("publication-a", "publication-b")
    )
    assert body.key == f"pages/{PAGE.value}/{VARIANT.value}/{body.revision.value}.json"
    assert len(body.digest) == 64


def test_exact_immutable_replay_is_a_no_op() -> None:
    body = _body()
    request = PresetArtifactPointerAdvanceRequest(_pointer(body))

    result = classify_conditional_pointer_failure(request, request.pointer, request.pointer)

    assert result.outcome is PresetArtifactPointerAdvanceOutcome.NO_OP


def test_divergent_immutable_history_is_a_collision_not_a_retry() -> None:
    body = _body()
    request = PresetArtifactPointerAdvanceRequest(_pointer(body))

    with pytest.raises(InvariantError) as error:
        classify_conditional_pointer_failure(
            request,
            _pointer(body, token=2),
            None,
        )

    assert error.value.code == PRESET_ARTIFACT_POINTER_CONFLICT


def test_conditional_pointer_failure_with_different_current_is_stale() -> None:
    current = _pointer(_body("publication-a"))
    next_body = _body("publication-b")
    request = PresetArtifactPointerAdvanceRequest(
        _pointer(next_body, token=2, supersedes=current.revision),
        NOW + timedelta(days=30),
    )

    result = classify_conditional_pointer_failure(request, None, current)

    assert result.outcome is PresetArtifactPointerAdvanceOutcome.STALE


def test_forward_pointer_transaction_conditions_exact_predecessor_and_retires_history() -> None:
    current = _pointer(_body("publication-a"))
    next_body = _body("publication-b")
    request = PresetArtifactPointerAdvanceRequest(
        _pointer(next_body, token=2, supersedes=current.revision),
        NOW + timedelta(days=30),
    )

    items = pointer_transaction_items("preset-artifact-table", request)

    assert [next(iter(item)) for item in items] == ["Put", "Put", "Update"]
    # ADR-140 amendment 2026-10-28: only the history put returns its old image on failure.
    history_write = cast("dict[str, object]", items[0].get("Put"))
    assert history_write.get("ReturnValuesOnConditionCheckFailure") == "ALL_OLD"
    assert all(
        "ReturnValuesOnConditionCheckFailure" not in cast("dict[str, object]", body)
        for item in items[1:]
        for body in item.values()
    )
    current_write = cast("dict[str, object]", items[1].get("Put"))
    assert current_write.get("ConditionExpression") == (
        "revision = :supersedes AND forward_token < :token"
    )
    assert current_write.get("ExpressionAttributeValues") == {
        ":supersedes": {"S": current.revision.value},
        ":token": {"N": "2"},
    }
    superseded_update = cast("dict[str, object]", items[2].get("Update"))
    assert superseded_update.get("ConditionExpression") == "revision = :supersedes"
    assert superseded_update.get("UpdateExpression") == "SET expires_at = :expires"


@dataclass
class RecordingBodies:
    events: list[str]

    def write(self, body: PresetArtifactBody) -> ArtifactBodyWriteResult:
        self.events.append("body")
        return ArtifactBodyWriteResult(
            body.page, body.variant, body.revision, body.key, body.digest, "version-1"
        )

    def write_sibling(self, body: PresetArtifactSiblingBody) -> PresetArtifactSiblingWriteResult:
        del body
        raise AssertionError("this publication has no sibling body")

    def read_sibling(self, request: PresetArtifactSiblingReadRequest) -> PresetArtifactSiblingBody:
        del request
        raise AssertionError("publish must not read a sibling body")

    def read(self, pointer: PresetArtifactPointer) -> PresetArtifactBody:
        del pointer
        raise AssertionError("publish must not read a body")


@dataclass
class RecordingPointers:
    events: list[str]
    result: PresetArtifactPointerAdvanceResult = field(
        default_factory=lambda: PresetArtifactPointerAdvanceResult(
            PresetArtifactPointerAdvanceOutcome.ADVANCED
        )
    )

    def current(
        self, page: PresetArtifactPage, variant: PresetArtifactVariant
    ) -> PresetArtifactPointer | None:
        del page, variant
        raise AssertionError("publish must not read the current pointer")

    def roster(
        self, request: PresetArtifactPointerRosterRequest
    ) -> PresetArtifactPointerRosterResult:
        del request
        raise AssertionError("publish must not read the pointer roster")

    def advance(
        self, request: PresetArtifactPointerAdvanceRequest
    ) -> PresetArtifactPointerAdvanceResult:
        del request
        self.events.append("pointer")
        return self.result


def test_persistence_writes_body_before_attempting_the_conditional_pointer() -> None:
    body = _body()
    request = PresetArtifactPublicationRequest(
        body, template_of(PresetArtifactPointerAdvanceRequest(_pointer(body)))
    )
    events: list[str] = []
    persistence = PresetArtifactPersistence(RecordingBodies(events), RecordingPointers(events))

    result = persistence.publish(request, remaining_ms=lambda: 600_000, tail_seconds=30.0)

    assert events == ["body", "pointer"]
    assert result.pointer.outcome is PresetArtifactPointerAdvanceOutcome.ADVANCED


# --- S6 slice 5i (D28): grant-bearing pointers through the existing classification ------------

GRANT = "e" * 64


def test_d28_a_grant_bearing_replay_of_its_own_write_is_a_no_op() -> None:
    from adapters.jobs.preset_artifact_publisher.graph_bake import (  # noqa: PLC0415
        granted_request,
    )

    body = _body()
    legacy = PresetArtifactPublicationRequest(
        body, template_of(PresetArtifactPointerAdvanceRequest(_pointer(body)))
    )
    granted = granted_request(legacy, GRANT, "2026-08-29T12:00:00Z").pointer_template.complete(None)
    pointer = granted.pointer

    result = classify_conditional_pointer_failure(granted, pointer, pointer)

    assert result.outcome is PresetArtifactPointerAdvanceOutcome.NO_OP
    assert (pointer.revision, pointer.body_digest) == (body.revision, body.digest)
    assert pointer.grant_id == GRANT


def test_d28_control_a_legacy_history_of_the_same_revision_is_a_conflict_not_a_no_op() -> None:
    from adapters.jobs.preset_artifact_publisher.graph_bake import (  # noqa: PLC0415
        granted_request,
    )

    body = _body()
    legacy_pointer = _pointer(body)
    granted = granted_request(
        PresetArtifactPublicationRequest(
            body, template_of(PresetArtifactPointerAdvanceRequest(legacy_pointer))
        ),
        GRANT,
        "2026-08-29T12:00:00Z",
    ).pointer_template.complete(None)

    with pytest.raises(InvariantError) as refused:
        classify_conditional_pointer_failure(granted, legacy_pointer, None)

    assert refused.value.code == PRESET_ARTIFACT_POINTER_CONFLICT


def _payload_of(size: int) -> str:
    """Return a canonical JSON object of exactly `size` UTF-8 bytes."""
    frame = len(canonical_preset_artifact_json({"pad": ""}).encode())
    return canonical_preset_artifact_json({"pad": "x" * (size - frame)})


@pytest.mark.parametrize("kind", [PresetArtifactBody, PresetArtifactCustodyManifest])
def test_d18_exactly_the_cap_is_accepted_and_one_byte_over_is_the_typed_ceiling(
    kind: type[PresetArtifactBody] | type[PresetArtifactCustodyManifest],
) -> None:
    """Paired: 256,000 bytes construct; 256,001 raise the typed, non-retryable D18 error."""
    retail = PresetArtifactPage("retail-health")
    revision = PresetArtifactRevision.from_publication_identities(("publication-a",))
    at_cap = _payload_of(MAX_PRESET_ARTIFACT_BYTES)
    assert len(at_cap.encode()) == MAX_PRESET_ARTIFACT_BYTES

    accepted = kind(retail, VARIANT, revision, at_cap)

    assert accepted.payload_json == at_cap
    with pytest.raises(PresetArtifactBodyCeilingError) as refused:
        kind(retail, VARIANT, revision, _payload_of(MAX_PRESET_ARTIFACT_BYTES + 1))
    assert refused.value.reason == RETAIL_BODY_CEILING
    assert refused.value.observed_bytes == MAX_PRESET_ARTIFACT_BYTES + 1
    assert isinstance(refused.value, ValueError)


def test_d18_names_the_ceiling_for_its_page_family() -> None:
    revision = PresetArtifactRevision.from_publication_identities(("publication-a",))
    with pytest.raises(PresetArtifactBodyCeilingError) as refused:
        PresetArtifactBody(PAGE, VARIANT, revision, _payload_of(MAX_PRESET_ARTIFACT_BYTES + 1))
    assert refused.value.reason == PRESET_BODY_CEILING


# --- Plan 4.5 (DB20): eight-wide ordered body->custody sibling pairs ---------------------------


@dataclass
class _SiblingStores:
    """Body and custody stores that record order and in-flight sibling occupancy."""

    events: list[str] = field(default_factory=list[str])
    fail_body: str | None = None
    wrong_custody: str | None = None
    hold_seconds: float = 0.02
    lock: threading.Lock = field(default_factory=threading.Lock)
    active: int = 0
    max_active: int = 0

    def _record(self, event: str) -> None:
        with self.lock:
            self.events.append(event)

    def write_sibling(self, body: PresetArtifactSiblingBody) -> PresetArtifactSiblingWriteResult:
        name = cast("PresetArtifactSiblingIdentity", body.identity).ordering
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.hold_seconds)
            self._record(f"body:{name}")
            if name == self.fail_body:
                raise DependencyError(ErrorCode("FICTIONAL", 1), "a sibling body write failed")
            return PresetArtifactSiblingWriteResult(
                body.page, body.variant, body.revision, body.identity, body.key, body.digest, "v1"
            )
        finally:
            with self.lock:
                self.active -= 1

    def write(self, body: PresetArtifactBody) -> ArtifactBodyWriteResult:
        self._record("parent-body")
        return ArtifactBodyWriteResult(
            body.page, body.variant, body.revision, body.key, body.digest, "v1"
        )

    def custody_write_sibling(
        self, manifest: PresetArtifactSiblingCustody
    ) -> PresetArtifactSiblingCustodyWriteResult:
        identity = manifest.identity
        # A sibling is named by its ordering; an index shard by its path (`index/segment-NNNN`).
        name = (
            identity.ordering
            if isinstance(identity, PresetArtifactSiblingIdentity)
            else identity.path
        )
        self._record(f"custody:{name}")
        digest = "0" * 64 if name == self.wrong_custody else manifest.digest
        return PresetArtifactSiblingCustodyWriteResult(
            manifest.page,
            manifest.variant,
            manifest.revision,
            manifest.identity,
            manifest.key,
            digest,
            "v1",
        )

    def custody_write(self, manifest: PresetArtifactCustodyManifest) -> CustodyManifestWriteResult:
        self._record("parent-custody")
        return CustodyManifestWriteResult(
            manifest.page, manifest.variant, manifest.revision, manifest.key, manifest.digest, "v1"
        )


@dataclass
class _CustodyView:
    stores: _SiblingStores

    def write_sibling(
        self, manifest: PresetArtifactSiblingCustody
    ) -> PresetArtifactSiblingCustodyWriteResult:
        return self.stores.custody_write_sibling(manifest)

    def write(self, manifest: PresetArtifactCustodyManifest) -> CustodyManifestWriteResult:
        return self.stores.custody_write(manifest)


def _sibling_request(count: int) -> PresetArtifactPublicationRequest:
    body = _body()
    names = tuple(f"order{index:02d}" for index in range(count))
    publics = tuple(
        PresetArtifactSiblingBody(
            body.page,
            body.variant,
            body.revision,
            PresetArtifactSiblingIdentity(name, 0),
            canonical_preset_artifact_json({"sibling": name}),
        )
        for name in names
    )
    privates = tuple(
        PresetArtifactSiblingCustody(
            body.page,
            body.variant,
            body.revision,
            PresetArtifactSiblingIdentity(name, 0),
            canonical_preset_artifact_json({"custody": name}),
        )
        for name in names
    )
    return PresetArtifactPublicationRequest(
        body,
        template_of(PresetArtifactPointerAdvanceRequest(_pointer(body))),
        PresetArtifactCustodyHeader(canonical_preset_artifact_json({"parent": "c"})),
        PresetArtifactSiblingStream.of(zip(publics, privates, strict=True)),
    )


def _persistence(
    stores: _SiblingStores, *, width: int = BAKE_WRITE_WIDTH
) -> tuple[PresetArtifactPersistence, RecordingPointers]:
    pointers = RecordingPointers(stores.events)
    persistence = PresetArtifactPersistence(
        cast("Any", stores), pointers, cast("Any", _CustodyView(stores)), write_width=width
    )
    return persistence, pointers


def _require_occupancy_within(observed: int, width: int) -> None:
    """Refuse an observed occupancy above the contract width (the plan's control helper)."""
    if observed > width:
        raise AssertionError(f"{observed} sibling writes in flight exceed the width {width}")


def test_stage_writes_at_most_the_contract_width_and_custody_never_precedes_its_body() -> None:
    assert BAKE_WRITE_WIDTH == 23  # pairs in flight: the contract's derived baker_write_width
    stores = _SiblingStores()
    persistence, _ = _persistence(stores)

    staged = persistence.stage(
        _sibling_request(30), remaining_ms=lambda: 600_000, tail_seconds=30.0
    )

    assert stores.max_active == 23
    _require_occupancy_within(stores.max_active, BAKE_WRITE_WIDTH)
    for index in range(30):
        name = f"order{index:02d}"
        assert stores.events.index(f"body:{name}") < stores.events.index(f"custody:{name}")
    siblings = [event for event in stores.events if event.startswith(("body:", "custody:"))]
    assert stores.events[: len(siblings)] == siblings
    # ADR-145: the one index shard after every pair, then the parent body and its custody.
    assert stores.events[-3:] == ["custody:index/segment-0000", "parent-body", "parent-custody"]
    assert "pointer" not in stores.events
    assert staged.pairs == 30 + 1 + 1


def test_control_a_writer_one_wider_than_the_contract_is_refused_by_the_occupancy_helper() -> None:
    stores = _SiblingStores()
    persistence, _ = _persistence(stores, width=24)

    persistence.stage(_sibling_request(30), remaining_ms=lambda: 600_000, tail_seconds=30.0)

    assert stores.max_active == 24
    with pytest.raises(AssertionError, match="exceed the width 23"):
        _require_occupancy_within(stores.max_active, BAKE_WRITE_WIDTH)


def test_publish_stages_every_object_before_the_pointer_advances() -> None:
    stores = _SiblingStores(hold_seconds=0)
    persistence, _ = _persistence(stores)

    result = persistence.publish(
        _sibling_request(3), remaining_ms=lambda: 600_000, tail_seconds=30.0
    )

    assert stores.events[-4:] == [
        "custody:index/segment-0000",
        "parent-body",
        "parent-custody",
        "pointer",
    ]
    assert result.pointer.outcome is PresetArtifactPointerAdvanceOutcome.ADVANCED


def test_a_failed_pair_stops_submission_observes_in_flight_and_writes_no_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first ``wait`` returns only once the whole first wave is done; later calls pass through.

    Submission stops once the fault is observed: ``stopped()`` turns true only when the stream
    loop handles the failed future and sets ``first_error`` (preset_artifacts.py:256-259, :390).
    With the first wave of W in one batch, the W-1 successes handled before the fault can refill
    at most W-1 pairs, and nothing is submitted after it, so at most 2W-1 bodies run. Without the
    stop, every one of the 3W requested pairs would be written.
    """
    first_call = [True]

    def whole_first_wave(*args: Any, **kwargs: Any) -> Any:
        if first_call[0]:
            first_call[0] = False
            kwargs["return_when"] = ALL_COMPLETED
        return wait(*args, **kwargs)

    monkeypatch.setattr("adapters.outbound.preset_artifacts.wait", whole_first_wave)
    assert BAKE_WRITE_WIDTH == 23  # pairs in flight: the contract's derived baker_write_width
    stores = _SiblingStores(fail_body="order02")
    persistence, _ = _persistence(stores)

    with pytest.raises(DependencyError) as failed:
        persistence.publish(
            _sibling_request(3 * BAKE_WRITE_WIDTH), remaining_ms=lambda: 600_000, tail_seconds=30.0
        )

    assert failed.value.code == ErrorCode("FICTIONAL", 1)
    bodies = [event for event in stores.events if event.startswith("body:")]
    # The first wave and at most W-1 refills ran; no new pair after the fault was observed.
    assert len(bodies) <= 2 * BAKE_WRITE_WIDTH - 1
    assert stores.active == 0
    assert "parent-body" not in stores.events
    assert "pointer" not in stores.events


def test_a_custody_attestation_mismatch_is_an_invariant_with_no_parent_write() -> None:
    stores = _SiblingStores(wrong_custody="order01", hold_seconds=0)
    persistence, _ = _persistence(stores)

    with pytest.raises(InvariantError) as refused:
        persistence.stage(_sibling_request(4), remaining_ms=lambda: 600_000, tail_seconds=30.0)

    assert refused.value.code == PRESET_ARTIFACT_POINTER_CONFLICT
    assert "parent-body" not in stores.events


def test_an_exact_replay_attests_the_same_objects() -> None:
    stores = _SiblingStores(hold_seconds=0)
    persistence, _ = _persistence(stores)
    request = _sibling_request(5)

    assert persistence.stage(
        request, remaining_ms=lambda: 600_000, tail_seconds=30.0
    ) == persistence.stage(request, remaining_ms=lambda: 600_000, tail_seconds=30.0)


def test_a_pointer_carries_its_served_windows_and_an_older_pointer_reads_as_no_dates() -> None:
    """ADR-116 decision 4: windows round-trip; a pointer written before them decodes as None."""
    window = DateRange(date(2026, 9, 9), date(2026, 9, 15))
    prior = DateRange(date(2026, 9, 2), date(2026, 9, 8))
    body = _body()
    dated = PresetArtifactPointer(
        body.page,
        body.variant,
        body.revision,
        body.digest,
        1,
        None,
        None,
        resolved_window=window,
        comparison_window=prior,
    )
    item = _pointer_item(dated, current=True)
    assert item["resolved_window_start"] == {"S": "2026-09-09"}
    assert item["comparison_window_end"] == {"S": "2026-09-08"}
    assert _pointer_from_item(dict(item), current=True) == dated
    older = _pointer(body)
    older_item = _pointer_item(older, current=True)
    assert "resolved_window_start" not in older_item
    decoded = _pointer_from_item(dict(older_item), current=True)
    assert decoded.resolved_window is None
    assert decoded.comparison_window is None
    # Paired: a malformed window is refused, and so is half a window.
    malformed = dict(item)
    malformed["resolved_window_end"] = {"S": "2026-09-31"}
    with pytest.raises(ValueError, match=r"day is out of range|Invalid isoformat|month"):
        _pointer_from_item(malformed, current=True)
    half = {key: value for key, value in item.items() if key != "resolved_window_end"}
    with pytest.raises(ItemError, match="resolved_window_end"):
        _pointer_from_item(half, current=True)
    with pytest.raises(ValueError, match="comparison window ends before its resolved window"):
        PresetArtifactPointer(
            body.page,
            body.variant,
            body.revision,
            body.digest,
            1,
            None,
            None,
            resolved_window=prior,
            comparison_window=window,
        )


def test_a_calendar_preset_id_is_a_variant_and_a_bake_intent() -> None:
    """ADR-100: calendar preset ids carry underscores, and each is a declared variant."""
    from adapters.jobs.preset_artifact_publisher.transport import BakeIntent  # noqa: PLC0415

    assert PresetArtifactVariant("last_complete_month").value == "last_complete_month"
    assert BakeIntent("scorecard", "last_complete_week").variant == "last_complete_week"
    # Paired: an uppercase or slash-bearing variant is still refused.
    with pytest.raises(ValueError, match="lowercase identifier"):
        PresetArtifactVariant("Last_Complete_Month")
    with pytest.raises(ValueError, match="lowercase identifiers"):
        BakeIntent("scorecard", "last/complete")
