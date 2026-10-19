# SPDX-License-Identifier: MIT-0
"""Operator email resolution: the cache, the per-call cap, reporting, and the bounded fan-out.

Plain callables only; the SDK binding is covered by the integration suite.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError

from adapters.outbound.cognito.operator_directory import (
    OPERATOR_CACHE_ENTRIES,
    OPERATOR_CACHE_TTL_SECONDS,
    OPERATOR_LOOKUPS_PER_CALL,
    LookupFailed,
    LookupFound,
    LookupNotFound,
    LookupTimedOut,
    OperatorDirectoryRequest,
    OperatorEmailResolver,
    OperatorLookup,
    OperatorLookupFanOutRequest,
    classify_admin_get_user_error,
    fan_out_lookups,
)
from adapters.outbound.telemetry.reporting import Report
from domain.identity.operator_directory import OperatorEmailResolution, OperatorEmailStatus

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-069")]

_SUBJECT = "fictional-operator-subject-01"
_EMAIL = "operator@example.com"
_CORRELATION_ID = "0f5a8c2e-1b7d-4e3a-9c6f-2d8b4a1e7c90"


class _Logger:
    """A Powertools `Logger` stand-in keeping each line's level, message, and fields."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str, dict[str, object]]] = []

    def _at(self, level: str) -> Any:
        def record(message: object, *_args: object, **kwargs: object) -> None:
            fields = dict(cast("dict[str, object]", kwargs.get("extra") or {}))
            self.lines.append((level, str(message), fields))

        return record

    def __getattr__(self, level: str) -> Any:
        if level in {"debug", "info", "warning", "error", "exception"}:
            return self._at(level)
        raise AttributeError(level)

    def at(self, level: str) -> list[dict[str, object]]:
        return [fields for recorded, _message, fields in self.lines if recorded == level]


@dataclass
class _Clock:
    now: float = 1_000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class _Lookups:
    """A scripted `lookup_many` recording every batch it receives."""

    outcomes: dict[str, OperatorLookup] = field(default_factory=dict[str, OperatorLookup])
    batches: list[frozenset[str]] = field(default_factory=list[frozenset[str]])

    def __call__(self, subjects: frozenset[str]) -> Mapping[str, OperatorLookup]:
        self.batches.append(subjects)
        return {subject: self.outcomes[subject] for subject in subjects if subject in self.outcomes}


def _resolver(lookups: _Lookups, clock: _Clock, logger: _Logger) -> OperatorEmailResolver:
    def report() -> Report:
        return Report(
            operation="prompt_admin.versions",
            correlation_id=_CORRELATION_ID,
            logger=logger,  # pyright: ignore[reportArgumentType] - a recording stand-in
        )

    return OperatorEmailResolver(lookups, clock, report)


def _client_error(code: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "Fictional provider message."}}, "AdminGetUser"
    )


def _status(resolutions: Mapping[str, OperatorEmailResolution], subject: str) -> str:
    return resolutions[subject].status.value


@pytest.fixture
def logger(monkeypatch: pytest.MonkeyPatch) -> _Logger:
    from adapters.outbound.telemetry import reporting  # noqa: PLC0415 - patch the metric sink

    def discard(_metric: object) -> None:
        return None

    monkeypatch.setattr(reporting, "_emit", discard)
    return _Logger()


def test_resolved_and_not_found_are_cached_until_the_ttl_passes(logger: _Logger) -> None:
    gone = "fictional-deleted-subject"
    lookups = _Lookups({_SUBJECT: LookupFound(_EMAIL), gone: LookupNotFound()})
    clock = _Clock()
    resolver = _resolver(lookups, clock, logger)

    first = resolver.resolve(frozenset({_SUBJECT, gone}))
    clock.now += OPERATOR_CACHE_TTL_SECONDS - 1
    second = resolver.resolve(frozenset({_SUBJECT, gone}))
    clock.now += 2
    resolver.resolve(frozenset({_SUBJECT, gone}))

    assert first[_SUBJECT] == OperatorEmailResolution(OperatorEmailStatus.RESOLVED, _EMAIL)
    assert _status(first, gone) == "not_found"
    assert second == first
    assert lookups.batches == [frozenset({_SUBJECT, gone}), frozenset({_SUBJECT, gone})]


def test_unavailable_is_never_cached(logger: _Logger) -> None:
    lookups = _Lookups({_SUBJECT: LookupTimedOut()})
    resolver = _resolver(lookups, _Clock(), logger)

    resolver.resolve(frozenset({_SUBJECT}))
    resolved = resolver.resolve(frozenset({_SUBJECT}))

    assert _status(resolved, _SUBJECT) == "unavailable"
    assert lookups.batches == [frozenset({_SUBJECT}), frozenset({_SUBJECT})]


def test_the_oldest_entry_is_evicted_when_the_cache_is_full(logger: _Logger) -> None:
    subjects = [f"fictional-subject-{index:04d}" for index in range(OPERATOR_CACHE_ENTRIES + 1)]
    lookups = _Lookups({subject: LookupNotFound() for subject in subjects})
    resolver = _resolver(lookups, _Clock(), logger)
    for subject in subjects:
        resolver.resolve(frozenset({subject}))
    lookups.batches.clear()

    resolver.resolve(frozenset({subjects[1], subjects[-1]}))
    resolver.resolve(frozenset({subjects[0]}))

    assert lookups.batches == [frozenset({subjects[0]})]


def test_one_call_looks_up_at_most_the_cap_and_the_overflow_is_unavailable(
    logger: _Logger,
) -> None:
    subjects = [f"fictional-subject-{index:03d}" for index in range(OPERATOR_LOOKUPS_PER_CALL + 3)]
    lookups = _Lookups({subject: LookupNotFound() for subject in subjects})
    resolver = _resolver(lookups, _Clock(), logger)

    resolutions = resolver.resolve(frozenset(subjects))

    [batch] = lookups.batches
    assert batch == frozenset(sorted(subjects)[:OPERATOR_LOOKUPS_PER_CALL])
    assert len(resolutions) == len(subjects)
    for subject in sorted(subjects)[OPERATOR_LOOKUPS_PER_CALL:]:
        assert _status(resolutions, subject) == "unavailable"
    later = resolver.resolve(frozenset(subjects))
    assert lookups.batches[1] == frozenset(sorted(subjects)[OPERATOR_LOOKUPS_PER_CALL:])
    assert {resolution.status for resolution in later.values()} == {OperatorEmailStatus.NOT_FOUND}


def test_a_subject_missing_from_the_lookup_result_is_unavailable(logger: _Logger) -> None:
    resolver = _resolver(_Lookups(), _Clock(), logger)

    assert _status(resolver.resolve(frozenset({_SUBJECT})), _SUBJECT) == "unavailable"
    assert logger.lines == []


def test_failed_lookups_in_one_call_write_exactly_one_error(logger: _Logger) -> None:
    subjects = [f"fictional-failing-subject-{index}" for index in range(3)]
    lookups = _Lookups(
        {subject: LookupFailed(_client_error("TooManyRequestsException")) for subject in subjects}
    )
    resolver = _resolver(lookups, _Clock(), logger)

    resolutions = resolver.resolve(frozenset(subjects))

    assert {resolution.status for resolution in resolutions.values()} == {
        OperatorEmailStatus.UNAVAILABLE
    }
    [error] = logger.at("error")
    assert error["operation"] == "prompt_admin.versions"
    assert error["correlation_id"] == _CORRELATION_ID
    assert error["error_code"] == "ADLY-COGNITO-0006"
    context = error["error_context"]
    assert isinstance(context, dict)
    assert context["failed_lookups"] == 3
    assert context["service_code"] == "TooManyRequestsException"
    assert logger.at("warning") == []


@pytest.mark.parametrize("email", [None, "not-an-address"], ids=["missing", "malformed"])
def test_a_missing_or_malformed_email_is_unavailable_with_one_warning(
    logger: _Logger, email: str | None
) -> None:
    lookups = _Lookups({_SUBJECT: LookupFound(email)})
    resolver = _resolver(lookups, _Clock(), logger)

    resolutions = resolver.resolve(frozenset({_SUBJECT}))
    resolver.resolve(frozenset({_SUBJECT}))

    assert _status(resolutions, _SUBJECT) == "unavailable"
    assert len(lookups.batches) == 2
    warnings = logger.at("warning")
    assert len(warnings) == 2
    assert (warnings[0]["component"], warnings[0]["phase"], warnings[0]["outcome"]) == (
        "operator_directory",
        "lookup",
        "degraded",
    )
    assert logger.at("error") == []


def test_no_record_names_the_subject_or_the_email(logger: _Logger) -> None:
    failing = "fictional-failing-subject"
    lookups = _Lookups(
        {
            _SUBJECT: LookupFound(_EMAIL),
            "fictional-malformed-subject": LookupFound("fictional-bad-attribute"),
            failing: LookupFailed(_client_error("AccessDeniedException")),
        }
    )
    resolver = _resolver(lookups, _Clock(), logger)

    resolver.resolve(frozenset(lookups.outcomes))

    assert logger.lines
    text = repr(logger.lines)
    for secret in (_SUBJECT, _EMAIL, failing, "fictional-malformed-subject", "fictional-bad"):
        assert secret not in text


def _request(subjects: set[str], max_workers: int = 4, budget: float = 2.0) -> Any:
    return OperatorLookupFanOutRequest(frozenset(subjects), max_workers, budget)


def test_fan_out_joins_results_by_subject_not_completion_order() -> None:
    subjects = [f"fictional-subject-{index}" for index in range(4)]
    gates = {subject: threading.Event() for subject in subjects}
    gates[subjects[-1]].set()

    def lookup(subject: str) -> OperatorLookup:
        # Each finishes only after the next one has: completion runs in reverse order.
        assert gates[subject].wait(timeout=2)
        index = subjects.index(subject)
        if index > 0:
            gates[subjects[index - 1]].set()
        return LookupFound(f"{subject}@example.com")

    results = fan_out_lookups(lookup, _request(set(subjects)))

    assert results == {subject: LookupFound(f"{subject}@example.com") for subject in subjects}


def test_fan_out_runs_at_most_max_workers_at_once() -> None:
    lock = threading.Lock()
    state = {"running": 0, "peak": 0}

    def lookup(_subject: str) -> OperatorLookup:
        with lock:
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
        threading.Event().wait(0.02)
        with lock:
            state["running"] -= 1
        return LookupNotFound()

    results = fan_out_lookups(lookup, _request({f"s-{index}" for index in range(5)}, 2))

    assert len(results) == 5
    assert state["peak"] <= 2


def test_a_lookup_still_running_at_the_budget_is_timed_out() -> None:
    release = threading.Event()

    def lookup(subject: str) -> OperatorLookup:
        if subject == "fictional-slow-subject":
            release.wait(timeout=5)
        return LookupNotFound()

    try:
        results = fan_out_lookups(
            lookup, _request({"fictional-slow-subject", "fictional-quick-subject"}, 2, 0.05)
        )
        assert results["fictional-slow-subject"] == LookupTimedOut()
        assert results["fictional-quick-subject"] == LookupNotFound()
    finally:
        release.set()


def test_a_raising_lookup_fails_only_its_own_subject() -> None:
    failure = RuntimeError("fictional lookup bug")

    def lookup(subject: str) -> OperatorLookup:
        if subject == "fictional-raising-subject":
            raise failure
        return LookupNotFound()

    results = fan_out_lookups(
        lookup, _request({"fictional-raising-subject", "fictional-quiet-subject"})
    )

    assert results["fictional-raising-subject"] == LookupFailed(failure)
    assert results["fictional-quiet-subject"] == LookupNotFound()


_REQUEST_CONTEXT: ContextVar[str] = ContextVar("_REQUEST_CONTEXT", default="unset")


def test_each_lookup_runs_in_a_copy_of_the_callers_context() -> None:
    seen: list[str] = []

    def lookup(_subject: str) -> OperatorLookup:
        seen.append(_REQUEST_CONTEXT.get())
        return LookupNotFound()

    token = _REQUEST_CONTEXT.set("fictional-request")
    try:
        fan_out_lookups(lookup, _request({"s-1", "s-2"}))
    finally:
        _REQUEST_CONTEXT.reset(token)

    assert seen == ["fictional-request", "fictional-request"]


def test_an_empty_fan_out_returns_nothing_without_looking_up() -> None:
    def lookup(_subject: str) -> OperatorLookup:
        raise AssertionError("no lookup expected")

    assert fan_out_lookups(lookup, _request(set())) == {}


@pytest.mark.parametrize(
    ("max_workers", "budget"),
    [(0, 1.0), (-1, 1.0), (1, 0.0), (1, -1.0), (1, float("inf")), (1, float("nan"))],
)
def test_a_fan_out_request_refuses_no_workers_or_an_unbounded_budget(
    max_workers: int, budget: float
) -> None:
    with pytest.raises(ValueError, match="operator lookup fan-out"):
        OperatorLookupFanOutRequest(frozenset({"s"}), max_workers, budget)


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        (("", "eu-west-1", 4), "user pool and a region"),
        (("eu-west-1_example", " ", 4), "user pool and a region"),
        (("eu-west-1_example", "eu-west-1", 0), "between 1 and 8"),
        (("eu-west-1_example", "eu-west-1", 9), "between 1 and 8"),
    ],
)
def test_a_directory_request_refuses_a_blank_pool_or_unbounded_concurrency(
    fields: tuple[str, str, int], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        OperatorDirectoryRequest(*fields)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_client_error("UserNotFoundException"), "not_found"),
        (_client_error("TooManyRequestsException"), "dependency"),
        (_client_error("AccessDeniedException"), "dependency"),
        (_client_error("ResourceNotFoundException"), "dependency"),
        (ConnectTimeoutError(endpoint_url="https://cognito-idp.test.invalid"), "dependency"),
        (ValueError("fictional"), "dependency"),
    ],
    ids=["user-not-found", "throttled", "denied", "no-pool", "connect-timeout", "other"],
)
def test_only_user_not_found_classifies_as_not_found(error: BaseException, expected: str) -> None:
    assert classify_admin_get_user_error(error) == expected
