# SPDX-License-Identifier: MIT-0
"""PB-1 (b, c): an older version arriving after a newer one is a typed skip (S4).

Two layers enforce it, and this test locates and drives both:

- across windows, per canonical partition, ADR-047's publication rule
  (`src/adapters/jobs/ingest_delivery/semantic_sidecar.py`, `_split_stale_stages`) skips a
  candidate whose source revision is strictly older than the incumbent CURRENT, terminating it
  as a SKIPPED attempt; an equal revision republishes. The final CURRENT is the newest revision
  in either arrival order;
- within one window, two pulls in flight in one `unit_key` group are two retrievals of one
  conflict unit, and the graph's published-input head `H(U)` (BF-8, S4 release) refuses the
  later-processed older one: it is fenced `older_than_current` and never published.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from execution_graph_dispatch_support import NOW
from execution_graph_release_support import context, head_item, keys, seed_granting_run
from execution_graph_support import FakeDynamo

from adapters.jobs.execution_graph.release import ReleaseOutcome, release_run
from adapters.jobs.ingest_delivery import semantic_sidecar
from adapters.outbound.dynamodb.execution_graph_release import head_epoch
from domain.semantics.revision import RevisionSchemeKey, SourceRevision

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-047")]

SCHEME = RevisionSchemeKey("amc.source_watermark")


def _revision(day: date) -> SourceRevision:
    return SourceRevision.dated(scheme=SCHEME, source_id=day.isoformat(), observed_on=day)


@dataclass
class _Publications:
    """One partition's incumbent CURRENT, advanced as each fresh candidate publishes."""

    current_by_partition: dict[str, Any]

    def current(self, partition: str) -> Any:
        return self.current_by_partition.get(partition)


def _stage(publication_id: str, day: date) -> Any:
    return SimpleNamespace(
        candidate=SimpleNamespace(
            partition="fct_ad_exposure#2026-08-08",
            publication_id=publication_id,
            source_revision=_revision(day),
        )
    )


def _arrive(publications: _Publications, stage: Any) -> bool:
    """One candidate's arrival: True if it publishes, False if ADR-047 skips it."""
    group = SimpleNamespace(tooling=SimpleNamespace(publications=publications))
    fresh, stale = semantic_sidecar._split_stale_stages(cast("Any", group), (stage,))  # pyright: ignore[reportPrivateUsage]
    if fresh:
        publications.current_by_partition[stage.candidate.partition] = stage.candidate
        return True
    assert stale == (stage,)
    return False


@pytest.mark.parametrize("newer_first", [True, False])
def test_pb1b_an_older_version_arriving_after_a_newer_one_is_a_skip(newer_first: bool) -> None:
    newer = _stage("publication-newer", date(2026, 9, 16))
    older = _stage("publication-older", date(2026, 9, 7))
    publications = _Publications({})
    order = (newer, older) if newer_first else (older, newer)

    outcomes = [_arrive(publications, stage) for stage in order]

    assert outcomes == ([True, False] if newer_first else [True, True])
    final = publications.current("fct_ad_exposure#2026-08-08")
    assert final.publication_id == "publication-newer"


def test_pb1b_an_equal_revision_republishes_and_its_rows_decide_nothing_new() -> None:
    publications = _Publications({})
    assert _arrive(publications, _stage("publication-a", date(2026, 9, 16)))
    assert _arrive(publications, _stage("publication-b", date(2026, 9, 16)))


def test_pb1c_two_in_flight_pulls_of_one_window_the_later_processed_older_one_is_a_skip() -> None:
    fake = FakeDynamo()
    older_r, newer_r = NOW - timedelta(hours=2), NOW - timedelta(hours=1)
    newer = seed_granting_run(fake, retrieved=newer_r, fingerprint="2" * 64, request_id="r-new")
    assert release_run(context(fake), newer.record.run_id) is ReleaseOutcome.APPLIED
    older = seed_granting_run(fake, retrieved=older_r, fingerprint="1" * 64, request_id="r-old")

    assert release_run(context(fake), older.record.run_id) is ReleaseOutcome.SUPERSEDED

    run = fake.items[(f"RUN#{older.record.run_id}", "RUN")]
    assert run["fence_reason"] == {"S": "older_than_current"}
    assert head_item(fake)["head_epoch"] == {"N": str(head_epoch(newer_r))}  # type: ignore[index]
    assert len(keys(fake, "GRANT#")) == 1


def test_pb1c_positive_the_newest_retrieval_publishes_whatever_order_it_is_processed_in() -> None:
    fake = FakeDynamo()
    older_r, newer_r = NOW - timedelta(hours=2), NOW - timedelta(hours=1)
    older = seed_granting_run(fake, retrieved=older_r, fingerprint="1" * 64, request_id="r-old")
    assert release_run(context(fake), older.record.run_id) is ReleaseOutcome.APPLIED
    # S6 §2.2: apply arms only `publish`, so it holds the run's one POST_GRANT lock.
    for name in ("publish",):
        node = fake.items[(f"RUN#{older.record.run_id}", f"NODE#{name}")]
        fake.items.pop((f"LOCK#{node['lock_key']['S']}", "LOCK"))
    newer = seed_granting_run(fake, retrieved=newer_r, fingerprint="2" * 64, request_id="r-new")
    assert release_run(context(fake), newer.record.run_id) is ReleaseOutcome.APPLIED
    assert head_item(fake)["head_epoch"] == {"N": str(head_epoch(newer_r))}  # type: ignore[index]
