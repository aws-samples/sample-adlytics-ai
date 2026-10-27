# SPDX-License-Identifier: MIT-0
"""Canonical digests inside a projection memo: each shared immutable value is projected once.

A digest that changed inside the memo would split a bake's fan-in or break its bind pin, and a
memo that kept a mutable value's earlier projection would pin a request that no longer exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from domain.shared_kernel import canonical
from domain.shared_kernel.canonical import canonical_model_digest, projection_memo

pytestmark = [pytest.mark.unit]


@dataclass(frozen=True, slots=True)
class _Publication:
    publication_id: str
    rules: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Request:
    metric: str
    publications: tuple[_Publication, ...]


@dataclass(slots=True)
class _Filters:
    values: list[str] = field(default_factory=list[str])


@dataclass(frozen=True, slots=True)
class _Holding:
    filters: _Filters


def _counting(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the type of every dataclass the projection actually projects (memo hits excluded)."""
    projected: list[str] = []
    real = canonical.fields

    def counted(value: object) -> object:
        projected.append(type(value).__name__)
        return real(value)  # type: ignore[arg-type]

    monkeypatch.setattr(canonical, "fields", counted)
    return projected


def test_requests_sharing_a_publication_digest_the_same_and_project_it_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D4 (dev 2026-10-25): every bound request re-projected the same publications."""
    shared = _Publication("publication-1", ("grain", "currency"))
    requests = tuple(
        _Request(metric, (shared, _Publication(f"{metric}-own", ()))) for metric in ("a", "b", "c")
    )
    outside = tuple(canonical_model_digest("request/v1", item) for item in requests)
    projected = _counting(monkeypatch)
    with projection_memo():
        inside = tuple(canonical_model_digest("request/v1", item) for item in requests)

    assert inside == outside
    assert len(set(outside)) == 3
    # The shared publication once, each request's own publication once, each request once.
    assert projected.count("_Publication") == 4
    assert projected.count("_Request") == 3


def test_a_value_holding_a_mutable_field_is_projected_again_inside_the_memo() -> None:
    filters = _Filters(["marketplace"])
    holding = _Holding(filters)
    with projection_memo():
        before = canonical_model_digest("request/v1", holding)
        filters.values.append("channel")
        after = canonical_model_digest("request/v1", holding)

    assert before != after
    assert after == canonical_model_digest("request/v1", holding)


def test_outside_the_scope_nothing_is_memoized(monkeypatch: pytest.MonkeyPatch) -> None:
    shared = _Publication("publication-1", ())
    with projection_memo():
        canonical_model_digest("request/v1", shared)
    projected = _counting(monkeypatch)
    canonical_model_digest("request/v1", _Request("a", (shared, shared)))

    assert projected.count("_Publication") == 2
