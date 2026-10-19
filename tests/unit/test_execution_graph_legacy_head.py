# SPDX-License-Identifier: MIT-0
"""The legacy head (S4, BF-8) and row 9's legacy-completed join (V-27).

`S3CoordinatorLegacyHead` runs against a fake S3 client (paginated listings) and a fake
coordinator reader. The release-side tests drive the real release adapter over `FakeDynamo`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from execution_graph_release_support import WINDOW, context, keys, seed_granting_run
from execution_graph_support import FakeDynamo, FakeManifests

from adapters.inbound.run_requests.service import (
    AdmissionDependencies,
    RunRequest,
    admit_run_request,
)
from adapters.jobs.execution_graph.release import ReleaseOutcome, release_run
from adapters.outbound.dynamodb.execution_graph import DynamoExecutionGraphStore
from adapters.outbound.legacy_head import S3CoordinatorLegacyHead
from domain.operations.execution_graph import AuthorityMode
from domain.operations.execution_graph_release import (
    CONFLICT_UNIT_HISTORY_OVER_BOUND,
    LegacyHistoryOverBoundError,
    LegacyRetrieval,
    legacy_head,
    newer_retrievals,
    published_head,
)
from domain.operations.ingestion_root import RETRIEVED_FORMAT, window_identity
from domain.operations.write_plan import (
    IntentOrigin,
    IntentOriginKind,
    PlanMeta,
    PlanStatus,
    SuccessorOwner,
    plan_digest_for_source_delivery,
    source_delivery_identity,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-074")]

BASE = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)


def _key(instant: datetime) -> str:
    return f"{WINDOW}retrieved={instant.strftime(RETRIEVED_FORMAT)}/manifest.json"


@dataclass
class _Paginator:
    pages: list[dict[str, Any]]

    def paginate(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return self.pages


@dataclass
class FakeS3:
    """Window listing (`CommonPrefixes`) and exact-version listings, as S3 returns them."""

    retrievals: list[datetime]
    versions: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    longer: bool = True
    """Also return a longer key under each manifest prefix, which the exact filter ignores."""

    def get_paginator(self, name: str) -> _Paginator:
        if name == "list_objects_v2":
            return _Paginator(
                [
                    {
                        "CommonPrefixes": [
                            {"Prefix": _key(instant).removesuffix("manifest.json")}
                            for instant in self.retrievals
                        ]
                    }
                ]
            )
        return _Paginator([{"Versions": self._version_entries()}])

    def _version_entries(self) -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []
        for key, ids in self.versions.items():
            entries += [{"Key": key, "VersionId": version} for version in ids]
            if self.longer:
                entries.append({"Key": f"{key}.longer", "VersionId": "longer"})
        return entries


def _plan_meta(digest: str, status: PlanStatus) -> PlanMeta:
    terminal = status is not PlanStatus.PLANNED
    return PlanMeta(
        plan_digest=digest,
        internal_execution_id="e" * 64,
        root_plan_key=f"WRITE_PLAN#{digest}#{'e' * 64}",
        generation=0,
        source_plan_key=None,
        origin_intent_id="c" * 64,
        roster_digest="a" * 64,
        origin=IntentOrigin(IntentOriginKind.SOURCE_DELIVERY, "b" * 64),
        status=status,
        unit_count=0,
        admitted_count=0,
        attested_count=0,
        failed_count=0,
        child_outcome_reduction_version=2,
        required_child_expectations=(),
        settled_attempt_count=0,
        settled_attempt_digest=None,
        terminal_at="00000001759740000000" if terminal else None,
        created_at="00000001759740000000",
    )


@dataclass
class FakeAuthority:
    """The coordinator's `SUCCESSOR_OWNER` and plan `META`, by plan digest."""

    plans: dict[str, PlanStatus] = field(default_factory=dict[str, PlanStatus])
    owner_reads: list[str] = field(default_factory=list[str])

    def owner(self, plan_digest: str) -> SuccessorOwner | None:
        self.owner_reads.append(plan_digest)
        if plan_digest not in self.plans:
            return None
        plan_key = f"WRITE_PLAN#{plan_digest}#{'e' * 64}"
        return SuccessorOwner(
            revision=1,
            root_plan_key=plan_key,
            last_plan_key=plan_key,
            last_generation=0,
            active_plan_key=None,
            active_intent_id=None,
            reserved_intent_id=None,
            reserved_intent_digest=None,
            reserved_generation=None,
            latest_internal_execution_id="e" * 64,
        )

    def plan_meta(self, plan_key: str) -> PlanMeta | None:
        digest = plan_key.removeprefix("WRITE_PLAN#").split("#", 1)[0]
        return _plan_meta(digest, self.plans[digest])


def _digest(key: str, version: str) -> str:
    return plan_digest_for_source_delivery(source_delivery_identity(((key, version),)))


def _reader(
    retrievals: list[datetime], completed: dict[datetime, PlanStatus]
) -> tuple[S3CoordinatorLegacyHead, FakeAuthority]:
    versions = {_key(instant): [f"v-{index}"] for index, instant in enumerate(retrievals)}
    authority = FakeAuthority(
        {
            _digest(_key(instant), versions[_key(instant)][0]): status
            for instant, status in completed.items()
        }
    )
    s3 = FakeS3(retrievals, versions)
    return S3CoordinatorLegacyHead("raw-fictional", s3, authority), authority  # type: ignore[arg-type]


def test_bf8_the_legacy_head_is_the_newest_newer_retrieval_whose_plan_completed() -> None:
    r = BASE
    older, newer, newest = r - timedelta(hours=1), r + timedelta(hours=1), r + timedelta(hours=2)
    reader, _ = _reader(
        [older, r, newer, newest],
        {older: PlanStatus.COMPLETED, newer: PlanStatus.COMPLETED, newest: PlanStatus.PLANNED},
    )
    assert reader.head_after(WINDOW, r) == newer


def test_bf8_the_legacy_head_reads_write_plans_only_for_retrievals_newer_than_r() -> None:
    r = BASE
    retrievals = [r - timedelta(hours=3), r - timedelta(hours=1), r, r + timedelta(hours=1)]
    reader, authority = _reader(retrievals, {retrievals[0]: PlanStatus.COMPLETED})
    assert reader.head_after(WINDOW, r) is None
    assert authority.owner_reads == [_digest(_key(retrievals[3]), "v-3")]


def test_bf8_a_failed_or_superseded_legacy_plan_does_not_raise_the_head() -> None:
    r = BASE
    newer = r + timedelta(hours=1)
    for status in (PlanStatus.SUPERSEDED, PlanStatus.ADMISSION_FAILED, PlanStatus.PLANNED):
        reader, _ = _reader([r, newer], {newer: status})
        assert reader.head_after(WINDOW, r) is None


def test_a_window_listing_beyond_the_generated_bound_is_refused() -> None:
    listing = [(f"k{index}", BASE + timedelta(seconds=index)) for index in range(513)]
    with pytest.raises(LegacyHistoryOverBoundError) as refused:
        newer_retrievals(listing, BASE)
    assert refused.value.signal == CONFLICT_UNIT_HISTORY_OVER_BOUND


def test_the_pure_reduction_and_the_published_head() -> None:
    assert legacy_head([LegacyRetrieval("a", BASE, (False, True))]) == BASE
    assert legacy_head([LegacyRetrieval("a", BASE, (False,))]) is None
    assert published_head(None, None) is None
    assert published_head(BASE, BASE + timedelta(seconds=1)) == BASE + timedelta(seconds=1)
    assert published_head(BASE + timedelta(seconds=2), BASE) == BASE + timedelta(seconds=2)


def test_v27_completed_plan_key_names_the_exact_versions_completed_legacy_plan() -> None:
    key = _key(BASE)
    reader, _ = _reader([BASE], {BASE: PlanStatus.COMPLETED})
    assert reader.completed_plan_key(key, "v-0") == f"WRITE_PLAN#{_digest(key, 'v-0')}#{'e' * 64}"
    assert reader.completed_plan_key(key, "v-other") is None
    failed, _ = _reader([BASE], {BASE: PlanStatus.ADMISSION_FAILED})
    assert failed.completed_plan_key(key, "v-0") is None


def test_bf8_release_refuses_r_after_a_legacy_completed_newer_retrieval() -> None:
    fake = FakeDynamo()
    r = datetime(2026, 10, 7, 7, 0, tzinfo=UTC)
    seeded = seed_granting_run(fake, retrieved=r)
    reader, _ = _reader([r, r + timedelta(hours=1)], {r + timedelta(hours=1): PlanStatus.COMPLETED})

    assert release_run(context(fake, reader), seeded.record.run_id) is ReleaseOutcome.SUPERSEDED  # type: ignore[arg-type]
    assert keys(fake, "GRANT#") == [] and keys(fake, "HEAD#") == []


def test_bf8_positive_release_grants_when_no_newer_legacy_retrieval_completed() -> None:
    fake = FakeDynamo()
    r = datetime(2026, 10, 7, 7, 0, tzinfo=UTC)
    seeded = seed_granting_run(fake, retrieved=r)
    reader, _ = _reader([r - timedelta(hours=1), r], {r - timedelta(hours=1): PlanStatus.COMPLETED})
    assert release_run(context(fake, reader), seeded.record.run_id) is ReleaseOutcome.APPLIED  # type: ignore[arg-type]


def test_row_13_uses_the_same_head_a_newer_legacy_completed_retrieval_refuses_older() -> None:
    fake = FakeDynamo()
    fake.items[("MODE#ingestion", "MODE")] = {
        "pk": {"S": "MODE#ingestion"},
        "sk": {"S": "MODE"},
        "mode": {"S": AuthorityMode.GRAPH.value},
        "revision": {"N": "3"},
    }
    r = datetime(2026, 10, 7, 7, 0, tzinfo=UTC)
    key = _key(r)
    reader, _ = _reader([r, r + timedelta(hours=1)], {r + timedelta(hours=1): PlanStatus.COMPLETED})
    deps = AdmissionDependencies(
        store=DynamoExecutionGraphStore("graph", fake),  # type: ignore[arg-type]
        manifests=FakeManifests(retrieved_at=window_identity(key).retrieved_at),  # type: ignore[arg-type]
        now=lambda: r + timedelta(hours=3),
        legacy=reader,
    )
    request = RunRequest(
        request_id="request-legacy-0001",
        protocol_version="v1",
        caller_kind="on_demand",
        reason="operator_rerun",
        manifest_key=key,
        version_id="v-0",
        requester="arn:aws:iam::000000000000:user/operator",
        requester_is_iam=True,
    )
    receipt = admit_run_request(request, deps)
    assert (receipt.decision, receipt.reason, receipt.row) == (
        "REFUSED",
        "older_than_current",
        13,
    )
