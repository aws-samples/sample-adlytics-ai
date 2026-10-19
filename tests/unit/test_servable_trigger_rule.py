# SPDX-License-Identifier: MIT-0
"""The servable trigger rule and envelope tolerance for the projection pointer."""

from __future__ import annotations

# The envelope guard is a private attestation seam exercised directly.
# pyright: reportPrivateUsage=false
import json
from typing import Any

import pytest

from adapters.jobs.insight_precompute import handler as precompute
from adapters.jobs.preset_artifact_publisher import enqueue
from adapters.outbound.dynamodb.semantic_state import (
    _require_envelope_fields,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]


def _record(projected: str | None, *, publication_id: str = "publication-1") -> dict[str, Any]:
    image: dict[str, Any] = {
        "partition_key": {"S": "partition-1"},
        "record_key": {"S": "CURRENT"},
        "payload": {"S": json.dumps({"publication_id": publication_id})},
    }
    if projected is not None:
        image["projected_publication_id"] = {"S": projected}
    return {"eventName": "MODIFY", "dynamodb": {"NewImage": image}}


class TestTriggerRule:
    """One rule, two consumers: only the applied servable transition is routed."""

    @pytest.mark.parametrize("parse", [enqueue.parse_record, precompute.parse_record])
    def test_a_legacy_record_without_the_pointer_is_processed(self, parse: Any) -> None:
        """Pre-migration authority advances are applied transitions: projection ran inline."""
        trigger = parse(_record(None))

        assert trigger is not None
        assert trigger.publication_id == "publication-1"

    @pytest.mark.parametrize("parse", [enqueue.parse_record, precompute.parse_record])
    def test_the_servable_advance_for_the_current_authority_is_processed(self, parse: Any) -> None:
        assert parse(_record("publication-1")) is not None

    @pytest.mark.parametrize("parse", [enqueue.parse_record, precompute.parse_record])
    def test_an_authority_only_advance_is_acknowledged_without_work(self, parse: Any) -> None:
        """A supersede moves authority while the pointer still names the predecessor."""
        assert parse(_record("publication-0")) is None

    @pytest.mark.parametrize("parse", [enqueue.parse_record, precompute.parse_record])
    def test_an_unprojected_first_publication_is_not_routed(self, parse: Any) -> None:
        assert parse(_record("NONE")) is None


def _current_item(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "partition_key": {"S": "partition-1"},
        "record_key": {"S": "CURRENT"},
        "publication_id": {"S": "publication-1"},
        "fencing_token": {"N": "1"},
        "payload": {"S": "{}"},
        "coverage_scope": {"S": "scope-1"},
        "bucket_start": {"S": "2026-08-25"},
    }
    return {**item, **(extra or {})}


def _immutable_item() -> dict[str, Any]:
    return {
        "partition_key": {"S": "partition-1"},
        "record_key": {"S": "PUBLICATION#publication-1"},
        "publication_id": {"S": "publication-1"},
        "fencing_token": {"N": "1"},
        "payload": {"S": "{}"},
    }


class TestEnvelopeTolerance:
    def test_the_authority_envelope_permits_the_three_servable_attributes(self) -> None:
        _require_envelope_fields(
            _current_item(
                {
                    "projected_publication_id": {"S": "publication-1"},
                    "projected_fencing_token": {"N": "1"},
                    "projected_at": {"S": "2026-08-25T00:00:00+00:00"},
                }
            ),
            _immutable_item(),
        )

    def test_an_unknown_extra_attribute_is_still_refused(self) -> None:
        with pytest.raises(ValueError, match="missing or unexpected"):
            _require_envelope_fields(_current_item({"extra": {"S": "x"}}), _immutable_item())

    def test_a_missing_authority_attribute_is_still_refused(self) -> None:
        broken = _current_item()
        del broken["coverage_scope"]

        with pytest.raises(ValueError, match="missing or unexpected"):
            _require_envelope_fields(broken, _immutable_item())

    def test_the_immutable_envelope_permits_no_extras(self) -> None:
        with pytest.raises(ValueError, match="missing or unexpected"):
            _require_envelope_fields(
                _current_item(),
                {**_immutable_item(), "projected_publication_id": {"S": "publication-1"}},
            )
