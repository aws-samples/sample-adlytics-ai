# SPDX-License-Identifier: MIT-0
"""Semantic product-dimension validity values."""

from datetime import UTC, datetime

__all__ = ["OPEN_ENDED"]

OPEN_ENDED = datetime(9999, 12, 31, tzinfo=UTC)
"""Exclusive validity end on the product-dimension row currently in force."""
