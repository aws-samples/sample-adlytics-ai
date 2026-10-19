# SPDX-License-Identifier: MIT-0
"""Resolve prompt authors' Cognito subjects to email addresses at read time (ADR-069).

The registry stores only the opaque subject. The versions route resolves each distinct author
through `AdminGetUser` on its own least-privilege role, caches the answer per container for
`OPERATOR_CACHE_TTL_SECONDS`, and never stores or logs an email address or a subject.

Parts, in order: the closed lookup result; the bounded concurrent fan-out; the cached resolver
and its reporting; the `AdminGetUser` error classifier; and the thin SDK binding, which is the
only part that touches boto3 and is covered by integration tests.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextvars import copy_context
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from adapters.outbound.aws_errors import aws_failure_context
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    report_error,
    report_operation,
)
from domain.identity.operator_directory import (
    OperatorEmailResolution,
    OperatorEmailStatus,
    is_operator_email,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode

if TYPE_CHECKING:  # pragma: no cover
    from types_boto3_cognito_idp.client import CognitoIdentityProviderClient

__all__ = [
    "OPERATOR_CACHE_ENTRIES",
    "OPERATOR_CACHE_TTL_SECONDS",
    "OPERATOR_DIRECTORY_UNAVAILABLE",
    "OPERATOR_LOOKUPS_PER_CALL",
    "OPERATOR_RESOLUTION_BUDGET_SECONDS",
    "CognitoOperatorDirectory",
    "LookupFailed",
    "LookupFound",
    "LookupNotFound",
    "LookupTimedOut",
    "OperatorDirectoryRequest",
    "OperatorEmailResolver",
    "OperatorLookup",
    "OperatorLookupFanOutRequest",
    "classify_admin_get_user_error",
    "fan_out_lookups",
]

OPERATOR_LOOKUPS_PER_CALL = 25
"""Bounds Cognito calls, and so MAU exposure, per versions response."""
OPERATOR_CACHE_ENTRIES = 256
"""Bounds the per-container cache's memory."""
OPERATOR_CACHE_TTL_SECONDS = 900
"""Bounds how long a deleted user can still show their email address."""
OPERATOR_RESOLUTION_BUDGET_SECONDS = 1.5
"""Bounds how long the versions response waits for lookups, inside its 2 s p95 budget."""
OPERATOR_DIRECTORY_UNAVAILABLE = ErrorCode("COGNITO", 6)

_MAX_CONCURRENCY = 8
_OPERATION = "prompt_admin.versions"


# --- 1. The closed lookup result ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LookupFound:
    """The user exists; `email` is its email attribute, or `None` when it has none."""

    email: str | None


@dataclass(frozen=True, slots=True)
class LookupNotFound:
    """No user has this subject (expected for a deleted user)."""


@dataclass(frozen=True, slots=True)
class LookupFailed:
    """The lookup could not be completed."""

    error: BaseException


@dataclass(frozen=True, slots=True)
class LookupTimedOut:
    """The lookup was still running when the response's budget ended."""


type OperatorLookup = LookupFound | LookupNotFound | LookupFailed | LookupTimedOut


# --- 2. The bounded concurrent fan-out ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OperatorLookupFanOutRequest:
    """Which subjects to look up, how many at once, and how long the caller waits."""

    subjects: frozenset[str]
    max_workers: int
    budget_seconds: float

    def __post_init__(self) -> None:
        if isinstance(self.max_workers, bool) or self.max_workers < 1:
            raise ValueError("an operator lookup fan-out needs at least one worker.")
        if not math.isfinite(self.budget_seconds) or self.budget_seconds <= 0:
            raise ValueError("an operator lookup fan-out needs a finite positive budget.")


def fan_out_lookups(
    lookup: Callable[[str], OperatorLookup], request: OperatorLookupFanOutRequest
) -> Mapping[str, OperatorLookup]:
    """Run one lookup per subject, at most `max_workers` at once, for at most the budget.

    Results join by subject, never by completion order. A lookup still running when the budget
    ends is `LookupTimedOut` and its result is discarded; one that raises is `LookupFailed`.
    Each job runs in a copy of the caller's context, so worker spans join the request trace.
    Never logs: every record is written by the caller after the join.
    """
    if not request.subjects:
        return {}
    ordered = sorted(request.subjects)
    executor = ThreadPoolExecutor(max_workers=min(request.max_workers, len(ordered)))
    try:
        futures: dict[str, Future[OperatorLookup]] = {
            subject: executor.submit(copy_context().run, lookup, subject) for subject in ordered
        }
        wait(futures.values(), timeout=request.budget_seconds)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return {subject: _outcome(futures[subject]) for subject in ordered}


def _outcome(future: Future[OperatorLookup]) -> OperatorLookup:
    if not future.done() or future.cancelled():
        return LookupTimedOut()
    try:
        return future.result()
    except Exception as error:
        # A lookup must not raise; one that does fails only its own subject.
        return LookupFailed(error)


# --- 3. The cached resolver and its reporting -----------------------------------------------


@dataclass(frozen=True, slots=True)
class _Cached:
    resolution: OperatorEmailResolution
    expires_at: float


@dataclass(slots=True)
class _Tally:
    failures: list[tuple[str, BaseException]]
    malformed: int = 0


class OperatorEmailResolver:
    """Resolve subjects through a per-instance TTL cache and one bounded lookup batch per call.

    Only the calling thread touches the cache. `resolved` and `not_found` are cached; an
    `unavailable` outcome never is, so it is retried on the next call.
    """

    def __init__(
        self,
        lookup_many: Callable[[frozenset[str]], Mapping[str, OperatorLookup]],
        clock: Callable[[], float],
        report: Callable[[], Report],
    ) -> None:
        self._lookup_many = lookup_many
        self._clock = clock
        self._report = report
        self._cache: dict[str, _Cached] = {}

    def resolve(self, subjects: frozenset[str]) -> Mapping[str, OperatorEmailResolution]:
        """Return exactly one resolution per requested subject; never raises."""
        now = self._clock()
        resolutions: dict[str, OperatorEmailResolution] = {}
        uncached: list[str] = []
        for subject in sorted(subjects):
            cached = self._cache.get(subject)
            if cached is not None and cached.expires_at > now:
                resolutions[subject] = cached.resolution
            else:
                uncached.append(subject)
        batch = frozenset(uncached[:OPERATOR_LOOKUPS_PER_CALL])
        tally = _Tally(failures=[])
        outcomes: Mapping[str, OperatorLookup] = self._lookup_many(batch) if batch else {}
        for subject in sorted(batch):
            outcome = outcomes.get(subject, LookupTimedOut())
            resolutions[subject] = self._resolution(subject, outcome, tally)
        unavailable = OperatorEmailResolution(OperatorEmailStatus.UNAVAILABLE)
        for subject in uncached[OPERATOR_LOOKUPS_PER_CALL:]:
            resolutions[subject] = unavailable
        self._write_reports(tally)
        return resolutions

    def _resolution(
        self, subject: str, outcome: OperatorLookup, tally: _Tally
    ) -> OperatorEmailResolution:
        match outcome:
            case LookupFound(email=email) if email is not None and is_operator_email(email):
                return self._cached(
                    subject, OperatorEmailResolution(OperatorEmailStatus.RESOLVED, email)
                )
            case LookupFound():
                tally.malformed += 1
            case LookupNotFound():
                return self._cached(subject, OperatorEmailResolution(OperatorEmailStatus.NOT_FOUND))
            case LookupFailed(error=error):
                tally.failures.append((subject, error))
            case LookupTimedOut():
                pass
        return OperatorEmailResolution(OperatorEmailStatus.UNAVAILABLE)

    def _cached(self, subject: str, resolution: OperatorEmailResolution) -> OperatorEmailResolution:
        self._cache.pop(subject, None)
        while len(self._cache) >= OPERATOR_CACHE_ENTRIES:
            # Dicts keep insertion order, so the first key is the oldest insertion.
            del self._cache[next(iter(self._cache))]
        self._cache[subject] = _Cached(resolution, self._clock() + OPERATOR_CACHE_TTL_SECONDS)
        return resolution

    def _write_reports(self, tally: _Tally) -> None:
        """Write at most one ERROR and one WARNING, never naming a subject or an address."""
        if not tally.failures and not tally.malformed:
            return
        report = self._report()
        if tally.failures:
            _, first = min(tally.failures, key=lambda failure: failure[0])
            report_error(
                DependencyError(
                    OPERATOR_DIRECTORY_UNAVAILABLE,
                    "Operator email addresses are temporarily unavailable.",
                    context={**aws_failure_context(first), "failed_lookups": len(tally.failures)},
                ),
                report,
            )
        if tally.malformed:
            report_operation(
                OperationLog(
                    operation=_OPERATION,
                    component="operator_directory",
                    phase="lookup",
                    outcome="degraded",
                    severity=LogSeverity.WARNING,
                    description="An operator email attribute is missing or malformed.",
                    details={"malformed_attributes": tally.malformed},
                ),
                report.logger,
            )


# --- 4. The AdminGetUser error classifier ---------------------------------------------------


def classify_admin_get_user_error(error: BaseException) -> Literal["not_found", "dependency"]:
    """Return `not_found` only for Cognito's `UserNotFoundException`; everything else alarms."""
    if isinstance(error, ClientError):
        code = error.response.get("Error", {}).get("Code")
        if code == "UserNotFoundException":
            return "not_found"
    return "dependency"


# --- 5. The SDK binding ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OperatorDirectoryRequest:
    """Which user pool to read, in which region, and how many lookups may run at once."""

    pool_id: str
    region: str
    max_concurrency: int

    def __post_init__(self) -> None:
        if not self.pool_id.strip() or not self.region.strip():
            raise ValueError("an operator directory needs a user pool and a region.")
        if (
            isinstance(self.max_concurrency, bool)
            or not 1 <= self.max_concurrency <= _MAX_CONCURRENCY
        ):
            raise ValueError("operator directory concurrency must be between 1 and 8.")


class CognitoOperatorDirectory:
    """`OperatorDirectoryPort` over `cognito-idp:AdminGetUser` on one user pool.

    One client is built in `__init__`, which runs at container initialization on one thread,
    and is then shared by the fan-out's worker threads. The boto3 guide says low-level clients
    are generally thread-safe while sessions and resources are not, and that `boto3.client()`
    itself should not be called inside a concurrent context ("Multithreading or multiprocessing
    with clients", https://docs.aws.amazon.com/boto3/latest/guide/clients.html, read
    2026-10-01); `adapters.outbound.agentcore.harness` records the same rule beside
    `_CLIENT_CREATION`. Creating the client before any concurrency needs no lock.
    """

    def __init__(self, request: OperatorDirectoryRequest, report: Callable[[], Report]) -> None:
        self._pool_id = request.pool_id
        self._max_concurrency = request.max_concurrency
        self._client: CognitoIdentityProviderClient = boto3.client(
            "cognito-idp",
            region_name=request.region,
            # One attempt with 1 s connect and read timeouts: a straggler past the response's
            # budget ends by itself in about 2 s, well inside the function's timeout.
            config=Config(
                connect_timeout=1,
                read_timeout=1,
                retries={"mode": "standard", "total_max_attempts": 1},
            ),
        )
        self._resolver = OperatorEmailResolver(self._lookup_many, time.monotonic, report)

    def email_addresses(self, subjects: frozenset[str]) -> Mapping[str, OperatorEmailResolution]:
        """Return one resolution per requested subject; never raises for a lookup failure."""
        return self._resolver.resolve(subjects)

    def _lookup_many(self, subjects: frozenset[str]) -> Mapping[str, OperatorLookup]:
        return fan_out_lookups(
            self._lookup,
            OperatorLookupFanOutRequest(
                subjects, self._max_concurrency, OPERATOR_RESOLUTION_BUDGET_SECONDS
            ),
        )

    def _lookup(self, subject: str) -> OperatorLookup:
        try:
            user = self._client.admin_get_user(UserPoolId=self._pool_id, Username=subject)
        except Exception as error:
            if classify_admin_get_user_error(error) == "not_found":
                return LookupNotFound()
            return LookupFailed(error)
        email = next(
            (
                attribute.get("Value")
                for attribute in user.get("UserAttributes", [])
                if attribute.get("Name") == "email"
            ),
            None,
        )
        return LookupFound(email)
