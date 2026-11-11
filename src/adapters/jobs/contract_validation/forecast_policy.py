# SPDX-License-Identifier: MIT-0
"""`validate_forecast_policy()`: the demand-forecast policy against its schema and typed loader.

T has no JSON Schema validator dependency, so the schema's structure (`required`,
`properties`, `additionalProperties: false`) is checked key for key here, and every value rule
is the typed policy's (`domain.forecasting.policy`), reached through the job's own loader.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import yaml

from adapters.outbound.demand_forecast_policy import PolicyError, load_policy
from domain.data_contracts.validation import Finding, Severity

POLICY = Path("contracts/forecasting/demand-forecast.yaml")
SCHEMA = Path("contracts/forecasting/demand-forecast.schema.json")
CONTRACT_ID = "demand-forecast-policy"


def _finding(rule: str, message: str) -> Finding:
    return Finding(rule, Severity.ERROR, CONTRACT_ID, message)


def _as_mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    items = cast("Mapping[object, object]", value).items()
    return {str(key): item for key, item in items}


def _structure(document: object, schema: Mapping[str, object], where: str) -> list[Finding]:
    found: list[Finding] = []
    declared = _as_mapping(schema.get("properties"))
    values = _as_mapping(document)
    if declared is None or values is None:
        return found
    required = schema.get("required", [])
    names = cast("list[object]", required) if isinstance(required, list) else []
    for name in sorted(str(r) for r in names if str(r) not in values):
        found.append(_finding("forecast_policy_required", f"{where}{name} is required"))
    for name in sorted(set(values) - set(declared)):
        found.append(_finding("forecast_policy_unknown", f"{where}{name} is not in the schema"))
    for name in sorted(set(values) & set(declared)):
        child = _as_mapping(declared[name])
        if child is not None:
            found.extend(_structure(values[name], child, f"{where}{name}."))
    return found


def validate_forecast_policy(root: Path) -> tuple[Finding, ...]:
    """Every finding for the policy file; empty when it matches its schema and loads."""
    schema: object = json.loads((root / SCHEMA).read_text(encoding="utf-8"))
    document: object = yaml.safe_load((root / POLICY).read_text(encoding="utf-8"))
    root_schema = _as_mapping(schema)
    if root_schema is None:
        return (_finding("forecast_policy_schema", f"{SCHEMA} is not a JSON object"),)
    found = _structure(document, root_schema, "")
    if found:
        return tuple(found)
    try:
        load_policy(root / POLICY)
    except PolicyError as error:
        return (_finding("forecast_policy_value", str(error)),)
    return ()
