# SPDX-License-Identifier: MIT-0
"""Closed Cognito group-to-review-level token projection."""

from __future__ import annotations

import pytest

from adapters.jobs.quality_review_token.handler import (
    _GROUP_LEVELS,  # pyright: ignore[reportPrivateUsage]
    handler,
)

pytestmark = pytest.mark.unit


def _levels(*groups: str) -> list[str]:
    result = handler({"request": {"groupConfiguration": {"groupsToOverride": list(groups)}}}, None)
    claims = result["response"]["claimsAndScopeOverrideDetails"]["accessTokenGeneration"]
    return claims["claimsToAddOrOverride"]["quality_review_levels"]


@pytest.mark.covers("ADR-078")
def test_pre_token_trigger_projects_only_allowlisted_groups() -> None:
    assert _levels("quality-reviewer", "ignored", "audit-attribution") == [
        "audit_attribution",
        "quality_reviewer",
    ]


def test_pre_token_trigger_projects_an_empty_level_list_when_no_group_is_granted() -> None:
    assert _levels() == []


def test_pre_token_trigger_projects_prompt_admin_without_any_quality_review_level() -> None:
    assert _levels("prompt-admin") == ["prompt_admin"]


@pytest.mark.covers("ADR-078")
@pytest.mark.parametrize(
    "group", ["quality-review-audit", "quality-review-findings", "quality-review-evaluation"]
)
def test_a_retired_group_projects_no_level(group: str) -> None:
    """Commit B of slice 6: the per-level groups are gone, and a stray membership grants nothing."""
    assert _levels(group) == []


@pytest.mark.covers("ADR-078")
def test_audit_attribution_alone_projects_no_reviewer_level() -> None:
    assert _levels("audit-attribution") == ["audit_attribution"]


@pytest.mark.covers("ADR-150")
def test_mcp_tool_users_projects_only_the_mcp_tool_use_level() -> None:
    assert _levels("mcp-tool-users") == ["mcp_tool_use"]
    assert _levels("mcp-tool-users", "quality-reviewer") == ["mcp_tool_use", "quality_reviewer"]


@pytest.mark.covers("ADR-150")
def test_the_group_mapping_is_exactly_the_approved_set() -> None:
    """Adding the MCP group changed no existing mapping, and no other group grants its level."""
    assert _GROUP_LEVELS == {
        "quality-reviewer": ("quality_reviewer",),
        "audit-attribution": ("audit_attribution",),
        "prompt-admin": ("prompt_admin",),
        "operations-status-read": ("operations_status_read",),
        "operations-run-requesters": ("operations_run_request",),
        "mcp-tool-users": ("mcp_tool_use",),
    }
