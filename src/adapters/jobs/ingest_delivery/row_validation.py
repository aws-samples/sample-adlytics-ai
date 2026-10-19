# SPDX-License-Identifier: MIT-0
"""Validate mapped rows through the generated contract model before any physical write."""

from __future__ import annotations

from adapters.jobs.schema_generation.models import class_name_for
from domain.data_contracts import generated_rows
from domain.data_contracts.contract import DataContract

__all__ = ["validated_rows"]


def validated_rows(
    contract: DataContract,
    rows: tuple[dict[str, object], ...],
) -> tuple[dict[str, object], ...]:
    """Restore target date/number/boolean/null types and refuse invalid mapped representation."""
    model = getattr(generated_rows, class_name_for(contract.contract_id))
    return tuple(dict(model.model_validate(row)) for row in rows)
