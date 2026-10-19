# SPDX-License-Identifier: MIT-0
"""Identifiers invented by real-resource tests, so shared-store sweeps can find them."""

from __future__ import annotations

import base64
import hashlib

__all__ = [
    "PROBE_PREFIX",
    "page_context_reference",
    "probe_key",
    "probe_reference",
    "pytest_node_id",
]

PROBE_PREFIX = "integration-probe"
"""The prefix every contract id, ASIN or key this tier invents begins with.

Hyphenated, matching the shape of a real contract id such as `vendor-sales-by-asin`, so a probe
record is obviously a contract id and obviously not a real one.
"""


def probe_key(node_name: str) -> str:
    """Build a probe identifier unique to one test, from its own node name.

    Unique per test because both load-state stores refuse a write that moves a key backwards, and
    that refusal is the property under test — so two tests sharing a key would have the first
    one's write decide whether the second one's was even attempted. Derived from the node name
    rather than randomised so that a record surviving a killed run names the test that wrote it.
    """
    return f"{PROBE_PREFIX}-{node_name}"


def pytest_node_id(request: object) -> str:
    """Read pytest's derived node id without allowing an untyped value downstream."""
    node = getattr(request, "node", None)
    value = getattr(node, "nodeid", None)
    if not isinstance(value, str) or not value:
        raise ValueError("pytest request carries no node id.")
    return value


def probe_reference(node_name: str, *, length: int = 43) -> str:
    """Build one grammar-safe, sweepable reference from the pytest node id."""
    prefix = f"{PROBE_PREFIX}-"
    if length <= len(prefix):
        raise ValueError("probe reference length must leave room for derived identity.")
    digest = base64.urlsafe_b64encode(hashlib.sha256(node_name.encode()).digest()).decode()
    return prefix + digest.rstrip("=")[: length - len(prefix)]


def page_context_reference(node_name: str) -> str:
    """Build one sweepable 43-character opaque reference from the pytest node name."""
    return probe_reference(node_name)
