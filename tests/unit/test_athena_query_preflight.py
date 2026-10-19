# SPDX-License-Identifier: MIT-0
"""Athena query preflight semantics before any remote execution begins."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest

from adapters.outbound.athena import query as athena_query
from adapters.outbound.athena.query import ALL_ROWS_WITHHELD, AthenaQueryAdapter, AthenaQueryTarget
from domain.shared_kernel.errors import ValidationError
from ports.outbound.query import QueryRequest

if TYPE_CHECKING:
    from types_boto3_athena.client import AthenaClient

pytestmark = pytest.mark.unit


@dataclass(frozen=True, slots=True)
class _Evidence:
    withheld_only: bool


@dataclass(frozen=True, slots=True)
class _Publication:
    evidence: _Evidence


@dataclass(frozen=True, slots=True)
class _BoundPlan:
    all_publications: tuple[_Publication, ...]


@dataclass(frozen=True, slots=True)
class _Request:
    plan: _BoundPlan


class _NoAthenaCalls:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"withheld-only preflight must not call Athena: {name}")


def test_withheld_only_publication_refuses_before_athena() -> None:
    request = cast(
        "QueryRequest",
        _Request(
            _BoundPlan(
                (
                    _Publication(_Evidence(False)),
                    _Publication(_Evidence(True)),
                )
            )
        ),
    )
    adapter = AthenaQueryAdapter(
        AthenaQueryTarget("unit-workgroup", "unit-current", 0),
        cast("AthenaClient", _NoAthenaCalls()),
    )

    with pytest.raises(ValidationError) as raised:
        adapter.run(request)

    assert raised.value.code == ALL_ROWS_WITHHELD
    assert raised.value.context == {"withheld_periods": 1}
    assert "no figure can be reported" in raised.value.user_visible_description


def test_ordinary_selected_publications_pass_withheld_only_preflight() -> None:
    request = cast(
        "QueryRequest",
        _Request(_BoundPlan((_Publication(_Evidence(False)),))),
    )

    athena_query._refuse_withheld_only(request)  # pyright: ignore[reportPrivateUsage]
