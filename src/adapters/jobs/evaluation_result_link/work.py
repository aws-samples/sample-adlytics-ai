# SPDX-License-Identifier: MIT-0
"""Content-free parsing and encoding for durable evaluation-result projections."""

from __future__ import annotations

from adapters.inbound.conversation.contract_codec import contract_to_json, decode_contract
from domain.evaluation.linkage import EvaluationResultV3


def result_to_json(result: EvaluationResultV3) -> str:
    """Encode one canonical content-free result projection for durable SQS handoff."""
    return contract_to_json(result)


def result_from_json(body: object) -> EvaluationResultV3:
    """Decode only one canonical EvaluationResult.v3 projection from SQS."""
    if not isinstance(body, str):
        raise ValueError("evaluation result projection body is not a string")
    return decode_contract(body, EvaluationResultV3)
