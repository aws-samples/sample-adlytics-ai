# SPDX-License-Identifier: MIT-0
"""The one stack-resource reader returns every ListStackResources page, in order, or fails whole.

DescribeStackResources stops at 100 resources with no continuation token, so a resource past the
100th was simply absent. The fake is a plain paged object, not an AWS mock.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from adapters.outbound.cloudformation_stack import stack_resource_summaries

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-134", "ADR-075")]

STACK = "Adlytics-Api-dev"


def _summary(index: int) -> dict[str, str]:
    return {
        "LogicalResourceId": f"Resource{index}",
        "PhysicalResourceId": f"fictional-resource-{index}",
        "ResourceType": "AWS::Lambda::Function",
    }


class _Paged:
    """ListStackResources pages. `None` is a page without the member; an exception is raised."""

    def __init__(self, pages: list[list[dict[str, str]] | Exception | None]) -> None:
        self.pages = pages
        self.requests: list[tuple[str, dict[str, object]]] = []

    def get_paginator(self, operation: str) -> _Paged:
        self.requests.append(("get_paginator", {"operation": operation}))
        return self

    def paginate(self, **request: object) -> Any:
        self.requests.append(("paginate", request))
        for page in self.pages:
            if isinstance(page, Exception):
                raise page
            yield {} if page is None else {"StackResourceSummaries": page}


def _read(client: _Paged) -> tuple[dict[str, object], ...]:
    summaries = stack_resource_summaries(cast("Any", client), STACK)
    return cast("tuple[dict[str, object], ...]", summaries)


def test_every_page_is_read_in_order_and_the_101st_resource_is_found() -> None:
    client = _Paged([[_summary(index) for index in range(100)], [_summary(100)]])
    summaries = _read(client)
    assert [item["LogicalResourceId"] for item in summaries] == [
        f"Resource{index}" for index in range(101)
    ]
    assert summaries[100]["PhysicalResourceId"] == "fictional-resource-100"
    assert client.requests == [
        ("get_paginator", {"operation": "list_stack_resources"}),
        ("paginate", {"StackName": STACK}),
    ]


def test_a_page_without_the_summaries_member_reads_as_empty() -> None:
    client = _Paged([[_summary(0)], None])
    assert [item["LogicalResourceId"] for item in _read(client)] == ["Resource0"]


def test_a_failed_later_page_fails_the_read_instead_of_returning_part_of_the_stack() -> None:
    client = _Paged([[_summary(0)], RuntimeError("page two refused")])
    with pytest.raises(RuntimeError, match="page two refused"):
        _read(client)
