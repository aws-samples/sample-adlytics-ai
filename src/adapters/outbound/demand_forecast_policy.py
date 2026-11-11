# SPDX-License-Identifier: MIT-0
"""Load contracts/forecasting/demand-forecast.yaml into the typed policy (design §3a).

Adapted from the source engine's inference-config parser: refused whole on an unknown key, a
missing key or a wrong type, so a typo stops a run instead of changing a forecast. Also runs in
the job image (image-paths.txt), so it imports only the standard library, PyYAML and
`domain.forecasting.policy`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

from domain.forecasting.policy import (
    DemandForecastPolicy,
    EligibilityPolicy,
    EnginePolicy,
    InputPolicy,
    MissingWeekPolicy,
    NumericPolicy,
    ServingPolicy,
)

SECTIONS = ("input", "missing_weeks", "eligibility", "engine", "numeric", "serving")


class PolicyError(ValueError):
    """The policy file is malformed or outside its contract; the message names the key."""


@dataclass(frozen=True)
class LoadedPolicy:
    """The typed policy with the file's SHA-256 and each section's version."""

    policy: DemandForecastPolicy
    sha256: str
    section_versions: dict[str, int]


def _mapping(value: object, where: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PolicyError(f"{where} must be a mapping")
    items = cast("Mapping[object, object]", value).items()
    return {str(key): item for key, item in items}


def _keys(document: Mapping[str, object], allowed: tuple[str, ...], where: str) -> None:
    missing = sorted(set(allowed) - set(document))
    unknown = sorted(set(document) - set(allowed))
    if missing or unknown:
        raise PolicyError(f"{where}: missing {missing}, unknown {unknown}")


def _int(document: Mapping[str, object], key: str, where: str) -> int:
    value = document[key]
    # bool is an int in Python; a YAML true is not a count.
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyError(f"{where}.{key} must be a whole number")
    return value


def _str(document: Mapping[str, object], key: str, where: str) -> str:
    value = document[key]
    if not isinstance(value, str) or not value:
        raise PolicyError(f"{where}.{key} must be a non-empty string")
    return value


def _bool(document: Mapping[str, object], key: str, where: str) -> bool:
    value = document[key]
    if not isinstance(value, bool):
        raise PolicyError(f"{where}.{key} must be true or false")
    return value


def _list(document: Mapping[str, object], key: str, where: str) -> list[object]:
    value = document[key]
    if not isinstance(value, list):
        raise PolicyError(f"{where}.{key} must be a list")
    return list(value)  # pyright: ignore[reportUnknownArgumentType]


def _strs(document: Mapping[str, object], key: str, where: str) -> tuple[str, ...]:
    items = _list(document, key, where)
    if not all(isinstance(item, str) and item for item in items):
        raise PolicyError(f"{where}.{key} must be a list of non-empty strings")
    return tuple(str(item) for item in items)


def _ints(document: Mapping[str, object], key: str, where: str) -> tuple[int, ...]:
    items = _list(document, key, where)
    if not all(isinstance(item, int) and not isinstance(item, bool) for item in items):
        raise PolicyError(f"{where}.{key} must be a list of whole numbers")
    return tuple(int(str(item)) for item in items)


def _floats(document: Mapping[str, object], key: str, where: str) -> tuple[float, ...]:
    items = _list(document, key, where)
    if not all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in items):
        raise PolicyError(f"{where}.{key} must be a list of numbers")
    return tuple(float(str(item)) for item in items)


def _input(raw: Mapping[str, object]) -> InputPolicy:
    keys = ("version", "facts", "product_metadata", "selectors", "week_starts_on")
    _keys(raw, (*keys, "targets", "covariates"), "input")
    facts = _mapping(raw["facts"], "input.facts")
    _keys(facts, ("sales", "traffic"), "input.facts")
    selectors = _mapping(raw["selectors"], "input.selectors")
    _keys(selectors, ("distributor_view", "selling_program"), "input.selectors")
    return InputPolicy(
        version=_int(raw, "version", "input"),
        sales_fact=_str(facts, "sales", "input.facts"),
        traffic_fact=_str(facts, "traffic", "input.facts"),
        product_metadata=_str(raw, "product_metadata", "input"),
        distributor_view=_str(selectors, "distributor_view", "input.selectors"),
        selling_program=_str(selectors, "selling_program", "input.selectors"),
        week_starts_on=_int(raw, "week_starts_on", "input"),
        targets=_strs(raw, "targets", "input"),
        covariates=_strs(raw, "covariates", "input"),
    )


def _missing_weeks(raw: Mapping[str, object]) -> MissingWeekPolicy:
    names = ("activity_window_weeks", "max_unknown_run_weeks", "unknown_day_grace_days")
    _keys(raw, ("version", *names), "missing_weeks")
    return MissingWeekPolicy(*(_int(raw, key, "missing_weeks") for key in ("version", *names)))


def _eligibility(raw: Mapping[str, object]) -> EligibilityPolicy:
    head = ("version", "min_context_weeks", "holdout_weeks", "min_scored_holdout_weeks")
    tail = ("history_weeks_max", "ets_min_known_weeks", "min_seasonal_residuals")
    _keys(raw, (*head, "horizon_weeks", "quantiles", *tail), "eligibility")
    return EligibilityPolicy(
        *(_int(raw, key, "eligibility") for key in head),
        horizon_weeks=_int(raw, "horizon_weeks", "eligibility"),
        quantiles=_floats(raw, "quantiles", "eligibility"),
        history_weeks_max=_int(raw, "history_weeks_max", "eligibility"),
        ets_min_known_weeks=_int(raw, "ets_min_known_weeks", "eligibility"),
        min_seasonal_residuals=_int(raw, "min_seasonal_residuals", "eligibility"),
    )


def _engine(raw: Mapping[str, object]) -> EnginePolicy:
    keys = ("version", "cross_learning", "batch_size", "batch_scope", "sort_key")
    _keys(raw, (*keys, "shard_max_asins"), "engine")
    return EnginePolicy(
        version=_int(raw, "version", "engine"),
        cross_learning=_bool(raw, "cross_learning", "engine"),
        batch_size=_int(raw, "batch_size", "engine"),
        batch_scope=_str(raw, "batch_scope", "engine"),
        sort_key=_strs(raw, "sort_key", "engine"),
        shard_max_asins=_int(raw, "shard_max_asins", "engine"),
    )


def _numeric(raw: Mapping[str, object]) -> NumericPolicy:
    keys = ("version", "stored_decimal_places", "stored_rounding", "check_order", "display")
    _keys(raw, keys, "numeric")
    return NumericPolicy(
        version=_int(raw, "version", "numeric"),
        stored_decimal_places=_int(raw, "stored_decimal_places", "numeric"),
        stored_rounding=_str(raw, "stored_rounding", "numeric"),
        check_order=_strs(raw, "check_order", "numeric"),
        display=_str(raw, "display", "numeric"),
    )


def _serving(raw: Mapping[str, object]) -> ServingPolicy:
    counts = (
        "version",
        "stale_after_days",
        "page_rows",
        "tool_list_max",
        "compare_max_asins",
    )
    sizes = ("title_max_bytes", "item_max_bytes")
    words = ("rank_by", "status_target", "search")
    _keys(raw, (*counts, "views", *sizes, *words), "serving")
    return ServingPolicy(
        *(_int(raw, key, "serving") for key in counts),
        views=_ints(raw, "views", "serving"),
        title_max_bytes=_int(raw, "title_max_bytes", "serving"),
        item_max_bytes=_int(raw, "item_max_bytes", "serving"),
        rank_by=_str(raw, "rank_by", "serving"),
        status_target=_str(raw, "status_target", "serving"),
        search=_str(raw, "search", "serving"),
    )


def parse_policy(document: object) -> DemandForecastPolicy:
    """Return the typed policy, refusing any structural or value defect as `PolicyError`."""
    root = _mapping(document, "policy")
    _keys(root, ("schema_version", *SECTIONS), "policy")
    try:
        return DemandForecastPolicy(
            schema_version=_int(root, "schema_version", "policy"),
            input=_input(_mapping(root["input"], "input")),
            missing_weeks=_missing_weeks(_mapping(root["missing_weeks"], "missing_weeks")),
            eligibility=_eligibility(_mapping(root["eligibility"], "eligibility")),
            engine=_engine(_mapping(root["engine"], "engine")),
            numeric=_numeric(_mapping(root["numeric"], "numeric")),
            serving=_serving(_mapping(root["serving"], "serving")),
        )
    except PolicyError:
        raise
    except ValueError as error:
        raise PolicyError(str(error)) from None


def load_policy(path: Path) -> LoadedPolicy:
    """Read, hash and parse the policy file (always `yaml.safe_load`)."""
    raw = path.read_bytes()
    try:
        document: object = yaml.safe_load(raw.decode("utf-8"))
    except yaml.YAMLError as error:
        raise PolicyError(f"{path.name} is not valid YAML: {error}") from None
    policy = parse_policy(document)
    return LoadedPolicy(policy, hashlib.sha256(raw).hexdigest(), policy.section_versions)
