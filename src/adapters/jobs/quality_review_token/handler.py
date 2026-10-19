# SPDX-License-Identifier: MIT-0
"""Cognito pre-token trigger for closed Quality Review route entitlements."""

from __future__ import annotations

from typing import Any, cast

_GROUP_LEVELS: dict[str, tuple[str, ...]] = {
    "quality-reviewer": ("quality_reviewer",),
    "audit-attribution": ("audit_attribution",),
    "prompt-admin": ("prompt_admin",),
    "operations-status-read": ("operations_status_read",),
    "operations-run-requesters": ("operations_run_request",),
}


def handler(event: dict[str, Any], _context: object) -> dict[str, Any]:
    """Project only approved Cognito group membership into a V2 access-token claim."""
    request = event.get("request")
    groups: tuple[str, ...] = ()
    if isinstance(request, dict):
        configuration = cast("dict[str, object]", request).get("groupConfiguration")
        if isinstance(configuration, dict):
            raw_groups = cast("dict[str, object]", configuration).get("groupsToOverride")
            if isinstance(raw_groups, list):
                group_values = cast("list[object]", raw_groups)
                groups = tuple(value for value in group_values if isinstance(value, str))
    levels = sorted({level for group in groups for level in _GROUP_LEVELS.get(group, ())})
    response = event.setdefault("response", {})
    if not isinstance(response, dict):
        raise ValueError("pre-token response is malformed")
    response["claimsAndScopeOverrideDetails"] = {
        "accessTokenGeneration": {"claimsToAddOrOverride": {"quality_review_levels": levels}}
    }
    return event
