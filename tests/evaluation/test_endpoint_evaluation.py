# SPDX-License-Identifier: MIT-0
"""Collected acceptance hook for the intentionally deferred Endpoint Evaluation V4 target."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.evaluation, pytest.mark.covers("ADR-057")]


def test_v4_direct_runtime_acceptance_requires_the_governed_ingress_and_evaluator() -> None:
    pytest.skip(
        "V4 acceptance is deferred until the IAM ingress and protected evaluator are deployed"
    )
