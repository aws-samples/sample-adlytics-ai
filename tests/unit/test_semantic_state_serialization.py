# SPDX-License-Identifier: MIT-0
"""Semantic publication evidence persistence compatibility at the DynamoDB adapter boundary."""

import pytest

from adapters.outbound.dynamodb import semantic_state as semantic_state_module
from adapters.outbound.dynamodb.semantic_state import publication_evidence_from_data

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-049")]


def test_legacy_untyped_evidence_remains_readable_but_cannot_authorize_a_candidate() -> None:
    evidence = publication_evidence_from_data(
        {
            "rule_set_version": "v2-before-typed-families",
            "passed_rules": ["identity", "grain"],
            "qualified_warnings": ["historical warning"],
            "failed_rules": [],
        }
    )

    assert evidence.readable_historical_success
    assert not evidence.publishable
    assert evidence.passed_rules == ("identity", "grain")
    assert evidence.qualified_warnings == ("historical warning",)


@pytest.mark.parametrize(
    "response",
    (
        None,
        {},
        {"Responses": None},
        {"Responses": [None]},
        {"Responses": [{"Item": "not-an-item"}]},
    ),
)
def test_malformed_transaction_response_shapes_are_refused(response: object) -> None:
    with pytest.raises(ValueError):
        semantic_state_module._transaction_response_items(  # pyright: ignore[reportPrivateUsage]
            response,
            1,
        )


def test_publication_item_bound_is_derived_from_transaction_limit_with_headroom() -> None:
    assert (
        semantic_state_module.MAX_PUBLICATION_ITEM_BYTES
        == (
            semantic_state_module.DYNAMODB_TRANSACTION_LIMIT_BYTES
            - semantic_state_module.PUBLICATION_TRANSACTION_HEADROOM_BYTES
        )
        // semantic_state_module.MAX_TRANSACTION_ITEMS
    )
    assert (
        semantic_state_module.MAX_PUBLICATION_ITEM_BYTES
        * semantic_state_module.MAX_TRANSACTION_ITEMS
        <= semantic_state_module.DYNAMODB_TRANSACTION_LIMIT_BYTES
        - semantic_state_module.PUBLICATION_TRANSACTION_HEADROOM_BYTES
    )
