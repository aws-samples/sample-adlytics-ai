# SPDX-License-Identifier: MIT-0
"""Asynchronous admission interface for protected released-response evaluation."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domain.evaluation.envelope import EvaluationEnvelope


@runtime_checkable
class EvaluationSinkPort(Protocol):
    """Durably admit exact released responses without coupling to a transport."""

    def enqueue(self, envelope: EvaluationEnvelope, receipt_created_at: str) -> str:
        """Durably admit one released response and return its opaque delivery reference.

        The turn's receipt creation time travels beside the envelope, never inside it, so the
        evaluation writer can compute its record's expiry without reading receipts (ADR-080 §1).
        """
        ...
