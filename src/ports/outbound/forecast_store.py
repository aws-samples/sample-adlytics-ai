# SPDX-License-Identifier: MIT-0
"""The demand-forecast store port: generation items, the `CURRENT` pointer and receipts (§6b).

Callers may rely on:
- every generation write is a conditional put; an exact replay (same bytes) succeeds, other bytes
  under the same key raise `ForecastStoreConflictError`, and nothing is ever overwritten;
- an item over `item_max_bytes` raises `ForecastItemTooLargeError` before any item of the call is
  written;
- `commit_outcome` applies the pointer change and its receipt together or not at all, and every
  write to a `SET#` key is inside a transaction;
- every read is one strongly consistent `GetItem`; no method queries or scans;
- a throttle that outlasts the client's retries raises `ForecastStoreThrottled`, the error name
  the state machine retries.

`ForecastReaderPort` is the read API's own port (ADR-163): it has no write method, and the same
adapter implements both. Each reader validates what it parses and raises `ForecastStoreIncomplete`
on an item that fails; a missing `ASIN#` or `PAGE#` item is None, a designed state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from domain.forecasting.boundary import CandidateSeriesV1, RosterEntryV1, RunIdentityV1
from domain.forecasting.list_pages import CLOSED_FILTERS, MAX_LIST_PAGE, ListPage, ListPages
from domain.forecasting.policy import MAX_SHARDS
from domain.forecasting.publish import (
    CurrentPointer,
    Disposition,
    GenerationRecord,
    LastAttempt,
    ReceiptIdentity,
    ReceiptRecord,
    RunOutcome,
    ShardAttempt,
    ShardPublishResult,
    changes_last_attempt,
    generation_id,
    generation_series_sha256,
)
from domain.forecasting.read_api import AsinItemView, GenerationView
from domain.forecasting.summaries import roster_entry

_GENERATION_OUTCOMES = frozenset({RunOutcome.PUBLISHED, RunOutcome.ROLLED_BACK})


class ForecastStoreThrottled(Exception):  # noqa: N818 - the name §6d's retries match
    """DynamoDB throttled past the client's adaptive retries; the step may be retried."""

    def __init__(self, code: str, retry_attempts: int) -> None:
        super().__init__(f"{code} after {retry_attempts} client retries")
        self.code = code
        self.retry_attempts = retry_attempts


class ForecastStoreConflictError(Exception):
    """A key already holds other bytes, or `META` no longer matches its attestation."""

    def __init__(self, key: str, detail: str) -> None:
        super().__init__(f"{key}: {detail}")
        self.key = key


class ForecastItemTooLargeError(Exception):
    """An item measures over `item_max_bytes`; nothing of the call was written."""

    def __init__(self, key: str, size: int, limit: int) -> None:
        super().__init__(f"{key}: {size} bytes over the {limit}-byte item ceiling")
        self.key = key
        self.size = size
        self.limit = limit


@dataclass(frozen=True)
class GenerationSeries:
    """One `ASIN#` item: the shard candidate and its display title, already capped."""

    candidate: CandidateSeriesV1
    title: str | None
    title_truncated: bool

    @property
    def roster(self) -> RosterEntryV1:
        """The unranked list row the roster part and the `ASIN#` item both store (ADR-163)."""
        return roster_entry(self.candidate, self.title, title_truncated=self.title_truncated)


@dataclass(frozen=True)
class GenerationMeta:
    """The generation's `META` item, written after every `ASIN#` and `PAGE#` item (§6b)."""

    set_id: str
    run_id: str
    receipt_sk: str
    input_end: date
    run_fingerprint: str
    run_identity: RunIdentityV1
    attempts: tuple[ShardPublishResult, ...]
    currency_codes: tuple[str, ...]
    counts_by_reason: Mapping[str, int]
    counts_by_model: Mapping[str, int]
    pages: ListPages

    def __post_init__(self) -> None:
        if not 1 <= len(self.attempts) <= MAX_SHARDS:
            raise ValueError(f"META binds 1 to {MAX_SHARDS} shard attempts")

    @property
    def generation_id(self) -> str:
        """The run ID and the digest of the bound attempts."""
        bound = [ShardAttempt(result.shard, result.attempt_id) for result in self.attempts]
        return generation_id(self.run_id, bound)

    @property
    def series_sha256(self) -> str:
        """SHA-256 of the shard digests in shard order."""
        return generation_series_sha256(self.attempts)

    @property
    def item_count(self) -> int:
        """The generation's `ASIN#` items."""
        return sum(result.item_count for result in self.attempts)

    @property
    def first_forecast_week(self) -> date:
        """The Sunday after `input_end`."""
        return self.input_end + timedelta(days=1)


@dataclass(frozen=True)
class StoreWriteResult:
    """How many items a call wrote, how many were exact replays, and the client's retries."""

    written: int
    replayed: int
    retry_attempts: int


class Attestation(StrEnum):
    """What the strongly consistent re-read of `META` found."""

    ATTESTED = "attested"
    META_MISSING = "meta_missing"
    META_DIFFERS = "meta_differs"
    """`item_count` or `series_sha256` differs from the generation being committed."""


@dataclass(frozen=True)
class RunReceipt:
    """One immutable `RUN#` receipt (§6b): outcome, category and run facts, never values."""

    sk: str
    outcome: RunOutcome
    recorded_at: datetime
    generation_id: str | None = None
    published_at: datetime | None = None
    failure_category: str | None = None
    retry_disposition: Disposition | None = None
    attempt_id: str | None = None
    operator_reason: str | None = None
    run_fingerprint: str | None = None
    run_identity: RunIdentityV1 | None = None
    counts_by_reason: Mapping[str, int] = field(default_factory=dict[str, int])
    durations_ms: Mapping[str, int] = field(default_factory=dict[str, int])
    landing_to_publish_seconds: int | None = None
    shard: int | None = None
    """The shard a held commit names (design §6c, R1-5); absent on every other receipt."""

    def __post_init__(self) -> None:
        if not self.sk.startswith("RUN#"):
            raise ValueError(f"a receipt key starts RUN#, got {self.sk!r}")
        if self.recorded_at.tzinfo is None:
            raise ValueError("recorded_at is timezone-aware")
        # A superseded commit names the attested generation that did not become CURRENT, so its
        # replay can be told from another attempt set's (R1-10); a superseded hold names none.
        if self.outcome in _GENERATION_OUTCOMES and self.generation_id is None:
            raise ValueError("generation_id is set on published and rolled_back")
        if (
            self.generation_id is not None
            and self.outcome not in _GENERATION_OUTCOMES
            and self.outcome is not RunOutcome.SUPERSEDED
        ):
            raise ValueError("generation_id is set only on published, rolled_back and superseded")
        if (self.published_at is not None) != (self.outcome is RunOutcome.PUBLISHED):
            raise ValueError("published_at is set exactly on published")
        if (self.retry_disposition is not None) != changes_last_attempt(
            self.outcome, self.run_fingerprint
        ):
            raise ValueError("retry_disposition is set exactly when last_attempt changes")

    @property
    def identity(self) -> ReceiptIdentity:
        """The request-bound fields the replay rule compares (`replay_basis()`, §6b)."""
        return ReceiptIdentity(
            outcome=self.outcome,
            generation_id=self.generation_id,
            attempt_id=self.attempt_id,
            run_fingerprint=self.run_fingerprint,
            run_identity=self.run_identity,
            failure_category=self.failure_category,
            operator_reason=self.operator_reason,
            shard=self.shard,
        )


def superseded_receipt(prepared: RunReceipt) -> RunReceipt:
    """Return the one `superseded` derivation of a prepared receipt (§6b replay rule).

    Every request-bound field stays, so the publisher's and the store's derivations of one
    request are the same receipt; only what an applied outcome alone has is dropped.
    """
    return replace(
        prepared,
        outcome=RunOutcome.SUPERSEDED,
        published_at=None,
        retry_disposition=None,
        landing_to_publish_seconds=None,
    )


@dataclass(frozen=True)
class OutcomeCommitRequest:
    """One outcome transaction: the receipt and, for a pointer outcome, `CURRENT` after it.

    `pointer` is what `next_pointer()` or `rollback_pointer()` returned; `series_sha256` is the
    attested `META` digest a publish conditions on. `previous_attempt` is the `last_attempt` read
    with `CURRENT`: a held or failed update conditions on it as well as on the fence, so two
    outcomes prepared from one `CURRENT` can't both replace it (R1-2).
    """

    set_id: str
    expected_fence: int
    receipt: RunReceipt
    pointer: CurrentPointer | None = None
    series_sha256: str | None = None
    previous_attempt: LastAttempt | None = None

    def __post_init__(self) -> None:
        outcome = self.receipt.outcome
        if not self.set_id or "#" in self.set_id:
            raise ValueError("set_id is non-empty and has no '#'")
        if self.expected_fence < 0:
            raise ValueError("expected_fence is never negative")
        if (self.pointer is not None) != changes_last_attempt(
            outcome, self.receipt.run_fingerprint
        ):
            raise ValueError("a pointer is given exactly when last_attempt changes")
        if (self.series_sha256 is not None) != (outcome is RunOutcome.PUBLISHED):
            raise ValueError("series_sha256 is given exactly for a published outcome")
        pointer = self.pointer
        if pointer is None:
            return
        attempt = pointer.last_attempt
        if attempt is None or attempt.outcome is not outcome:
            raise ValueError("the pointer's last_attempt records the receipt's outcome")
        if attempt.retry_disposition is not self.receipt.retry_disposition:
            raise ValueError("the receipt and last_attempt carry one disposition")
        if outcome in _GENERATION_OUTCOMES and pointer.generation_id != self.receipt.generation_id:
            raise ValueError("the pointer names the receipt's generation")
        if outcome is RunOutcome.ROLLED_BACK and pointer.rolled_back_from is None:
            raise ValueError("a rollback names the generation it rolls back from")


class CommitDisposition(StrEnum):
    """What `commit_outcome` did."""

    APPLIED = "applied"
    """The transaction applied: the pointer change (if any) and the receipt."""
    SUPERSEDED = "superseded"
    """The fence moved: only a `superseded` receipt was written."""
    ALREADY_CURRENT = "already_current"
    """A retry after an unseen success: `CURRENT` already names this generation."""
    REPLAYED = "replayed"
    """The receipt already existed; `recorded_outcome` is the one on record."""


@dataclass(frozen=True)
class OutcomeCommitResult:
    """The disposition, the outcome now on record under the receipt key, and client retries."""

    disposition: CommitDisposition
    receipt_sk: str
    recorded_outcome: RunOutcome
    retry_attempts: int
    recorded_generation_id: str | None = None
    """The generation the receipt on record names (R1-3); None for an outcome without one."""
    recorded: ReceiptRecord | None = None
    """On `REPLAYED`, the receipt on record the replay rule matched (R1-14)."""


@dataclass(frozen=True)
class SlotClaimRequest:
    """Claim one slot period's marker for a scheduled check (F-A8 ruling, 2026-11-16)."""

    set_id: str
    slot_date: date
    """The period's admission weekday, `selection.slot_date()` of the check date."""
    run_id: str
    execution_name: str
    """The Scheduler-named execution; the same execution re-claiming is a Lambda retry."""
    scheduled_time: datetime

    def __post_init__(self) -> None:
        if not self.set_id or "#" in self.set_id:
            raise ValueError("set_id is non-empty and has no '#'")
        if not self.run_id or not self.execution_name:
            raise ValueError("a slot claim names its run and execution")
        if self.scheduled_time.tzinfo is None:
            raise ValueError("scheduled_time is timezone-aware")


@dataclass(frozen=True)
class SlotClaimResult:
    """Whether this execution holds the period's marker, and the run that holds it."""

    claimed: bool
    holder_run_id: str


class ForecastStorePort(Protocol):
    """The publisher's and the input builder's view of the forecast table."""

    def put_generation_items(
        self, generation: str, series: tuple[GenerationSeries, ...]
    ) -> StoreWriteResult:
        """Write one shard's `ASIN#` items, each under its own partition key."""
        ...

    def put_list_pages(self, generation: str, pages: tuple[ListPage, ...]) -> StoreWriteResult:
        """Write the generation's `PAGE#` items."""
        ...

    def put_generation_meta(self, meta: GenerationMeta) -> StoreWriteResult:
        """Write `META`, after every page item."""
        ...

    def attest_generation(self, meta: GenerationMeta) -> Attestation:
        """Re-read `META` and compare `item_count` and `series_sha256`."""
        ...

    def commit_outcome(self, request: OutcomeCommitRequest) -> OutcomeCommitResult:
        """Apply one outcome as one `TransactWriteItems` call (§6b)."""
        ...

    def claim_slot(self, request: SlotClaimRequest) -> SlotClaimResult:
        """Write the period's `SLOT#` marker if absent; never overwrite or delete one."""
        ...

    def get_current(self, set_id: str) -> CurrentPointer | None:
        """Read the set's `CURRENT` item, or None before the first outcome."""
        ...

    def get_receipt(self, set_id: str, sk: str) -> ReceiptRecord | None:
        """Read one `RUN#` receipt."""
        ...

    def get_generation(self, generation: str) -> GenerationRecord | None:
        """Read what a rollback needs of a generation's `META`."""
        ...


class ForecastStoreIncomplete(Exception):  # noqa: N818 - names the store state, as the code does
    """A stored item fails validation, or one the generation must hold is missing."""

    def __init__(self, key: str, detail: str) -> None:
        super().__init__(f"{key}: {detail}")
        self.key = key
        self.detail = detail


@dataclass(frozen=True)
class ReadListPageRequest:
    """One `PAGE#` read: a generation, a closed filter and a page from 1 to `MAX_LIST_PAGE`."""

    generation_id: str
    filter: str
    page: int

    def __post_init__(self) -> None:
        if self.filter not in CLOSED_FILTERS:
            raise ValueError(f"{self.filter!r} is not a closed list filter")
        if isinstance(self.page, bool) or not 1 <= self.page <= MAX_LIST_PAGE:
            raise ValueError(f"page {self.page} is outside 1 to {MAX_LIST_PAGE}")


@dataclass(frozen=True)
class ReadListPageResult:
    """The page's stored entries, or None when the item doesn't exist."""

    entries: tuple[RosterEntryV1, ...] | None


class ForecastReaderPort(Protocol):
    """Strongly consistent reads of `CURRENT` and one generation's items; no write method."""

    def get_current(self, set_id: str) -> CurrentPointer | None:
        """Read the set's `CURRENT` item, or None before the first outcome."""
        ...

    def read_generation(self, generation_id: str) -> GenerationView | None:
        """Read and validate the generation's `META`, or None when it is absent."""
        ...

    def read_list_page(self, request: ReadListPageRequest) -> ReadListPageResult:
        """Read and validate one `PAGE#` item."""
        ...

    def read_asin(self, generation_id: str, display_asin: str) -> AsinItemView | None:
        """Read and validate one product's `ASIN#` item, or None when it is absent."""
        ...
