# SPDX-License-Identifier: MIT-0
"""V4 stream contract coverage for validation and acceptance probes (ADR-083).

The module keeps its V3-era file name; the probes it covers now parse only ConversationStream.v4.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))
from validation import accept_managed_harness as harness  # noqa: E402
from validation import probe_conversation_runtime_observability as observability  # noqa: E402

pytestmark = pytest.mark.unit


_Event = dict[str, object]
_TerminalParser = Callable[[list[tuple[float, bytes]]], str]


def _chunks(*events: _Event) -> list[tuple[float, bytes]]:
    return [
        (
            0.0,
            b"".join(
                f"data:{json.dumps(event, separators=(',', ':'), sort_keys=True)}\n\n".encode()
                for event in events
            ),
        )
    ]


def _activity() -> _Event:
    return {
        "schema_version": "ConversationStream.v4",
        "kind": "activity",
        "stage": "retrieving_governed_data",
    }


def _tool() -> _Event:
    return {
        "schema_version": "ConversationStream.v4",
        "kind": "tool",
        "tool_name": "catalog_list_metrics",
        "status": "completed",
    }


def _text(sequence: int = 1, text: str = "Fictional public answer.") -> _Event:
    return {
        "schema_version": "ConversationStream.v4",
        "kind": "text",
        "sequence": sequence,
        "text": text,
    }


def _done() -> _Event:
    return {"schema_version": "ConversationStream.v4", "kind": "done", "audit_id": "audit-01"}


def _refused() -> _Event:
    return {
        "schema_version": "ConversationStream.v4",
        "kind": "refused",
        "reason_code": "dependency_unavailable",
        "reason": "Fictional refusal.",
        "audit_id": "audit-01",
    }


@pytest.mark.parametrize(
    "terminal_parser",
    [observability._terminal, harness._terminal],  # pyright: ignore[reportPrivateUsage]
)
def test_validation_terminal_parsers_accept_a_v4_streamed_answer_and_refusal(
    terminal_parser: _TerminalParser,
) -> None:
    assert terminal_parser(_chunks(_activity(), _text(), _done())) == "response_done"
    assert (
        terminal_parser(_chunks(_text(1, "Part "), _tool(), _text(2, "two."), _done()))
        == "response_done"
    )
    assert terminal_parser(_chunks(_activity(), _refused())) == "refused"
    # Text already sent stays on screen; a later refusal still ends the stream.
    assert terminal_parser(_chunks(_text(), _refused())) == "refused"


@pytest.mark.parametrize(
    "terminal_parser",
    [observability._terminal, harness._terminal],  # pyright: ignore[reportPrivateUsage]
)
@pytest.mark.parametrize("schema_version", ["ConversationStream.v2", "ConversationStream.v3"])
def test_validation_terminal_parsers_reject_earlier_streams(
    terminal_parser: _TerminalParser, schema_version: str
) -> None:
    earlier_refusal: _Event = {
        "schema_version": schema_version,
        "kind": "refused",
        "reason_code": "output_intervened",
        "reason": "Fictional refusal.",
    }

    with pytest.raises(ValueError):
        terminal_parser(_chunks(earlier_refusal))


@pytest.mark.parametrize(
    "terminal_parser",
    [observability._terminal, harness._terminal],  # pyright: ignore[reportPrivateUsage]
)
def test_validation_terminal_parsers_reject_a_textless_done(
    terminal_parser: _TerminalParser,
) -> None:
    with pytest.raises(ValueError, match="done requires released answer text"):
        terminal_parser(_chunks(_activity(), _done()))


def test_managed_harness_requires_a_non_blank_released_answer() -> None:
    with pytest.raises(harness._StageError) as error:  # pyright: ignore[reportPrivateUsage]
        harness._terminal(_chunks(_text(1, "  \n"), _done()))  # pyright: ignore[reportPrivateUsage]
    assert error.value.reason == "managed_final_public_content_missing"


class _Reviews:
    def __init__(self, documents: list[dict[str, object] | None]) -> None:
        self._documents = documents

    def turn(self, correlation_id: str) -> dict[str, object] | None:  # noqa: ARG002
        return self._documents.pop(0)


def _review(correlation_id: str) -> dict[str, object]:
    return {
        "schema_version": "QualityTurnReview.v1",
        "audit": {
            "schema_version": "QualityAuditReview.v3",
            "identity": {
                "audit_id": "audit-fictional",
                "correlation_id": correlation_id,
                "trace_id": None,
                "created_at": "2026-09-28T00:00:00+00:00",
            },
            "provenance": {
                "runtime_version": "fictional-runtime",
                "prompt_catalog_digest": None,
                "response_contract_version": "fictional-contract",
            },
            "outcome": {
                "operation_category": "composition",
                "operation_outcome": "refused",
                "publication_outcome": "refused",
                "terminal_outcome": "governed_refusal",
                "failure_error_code": None,
                "completion_refusal_stage": None,
            },
            "trajectory": [],
            "publication": {
                "tool_custody_digest": None,
                "presentation_plan_digest": None,
                "public_block_kinds": [],
                "public_block_count": 0,
                "evaluation_admission_ref": None,
            },
            "expires_at": None,
        },
    }


def test_the_observability_probe_reads_its_receipt_through_the_review_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(observability.time, "sleep", no_sleep)
    correlation = "123e4567-e89b-42d3-a456-426614174000"

    receipt = observability._exact_audit_receipt(  # pyright: ignore[reportPrivateUsage]
        _Reviews([None, _review(correlation)]), correlation
    )

    assert receipt.audit_id == "audit-fictional"
    assert observability._audit_classifications(receipt)["terminal_outcome"] == "governed_refusal"  # pyright: ignore[reportPrivateUsage]


def test_the_observability_probe_refuses_a_receipt_for_another_correlation() -> None:
    with pytest.raises(ValueError, match="another correlation"):
        observability._exact_audit_receipt(  # pyright: ignore[reportPrivateUsage]
            _Reviews([_review("223e4567-e89b-42d3-a456-426614174000")]),
            "123e4567-e89b-42d3-a456-426614174000",
        )
