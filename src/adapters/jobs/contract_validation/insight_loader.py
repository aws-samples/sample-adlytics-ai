# SPDX-License-Identifier: MIT-0
"""Read governed command-center insight declarations without treating them as data contracts."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

import yaml

from adapters.insight_finding_units import (
    ALLOWED_FINDING_UNITS,
    dictionary_id,
    finding_unit,
    metric_scale,
)
from adapters.insight_next_steps import SURFACED_SLOTS
from adapters.outbound import repository_yaml
from adapters.outbound.metric_dictionary import load_catalog
from domain.catalog.capability import Catalog
from domain.data_contracts.contract import ContractError
from domain.presentation.insights import (
    DETERMINISTIC_SLOT_IDS,
    BasisThresholds,
    DeliveryFollowUpEvidence,
    DeterministicFinding,
    FindingUnit,
    HorizonThresholds,
    InsightCalibrationCase,
    InsightCalibrationDraft,
    InsightCalibrationKind,
    InsightContract,
    InsightDeepContext,
    InsightEvidenceInput,
    InsightFindingKind,
    InsightHorizonRole,
    InsightMateriality,
    InsightNextStepKind,
    InsightOutputClass,
    InsightSlotId,
    InsightSlotPredicate,
    InsightStructuredOutputSchema,
    MetricScale,
    NearZeroPriors,
    OperandBasis,
    OverIndexPolicy,
    SaturationPolicy,
    ShiftPolicy,
)

from .loader import CONTRACTS_DIR

_INSIGHTS_DIRECTORY = "insights"
type Mapping = dict[str, object]


class _StrictSafeLoader(repository_yaml.RepositoryLoader):
    """Safe YAML loader that rejects duplicate mapping keys before construction."""


def _construct_unique_mapping(
    loader: _StrictSafeLoader, node: yaml.MappingNode, deep: bool = False
) -> Mapping:
    loader.flatten_mapping(node)
    mapping: Mapping = {}
    for key_node, value_node in node.value:
        key = cast("Any", loader).construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ContractError("insight YAML mapping keys must be text.")
        if key in mapping:
            raise ContractError(f"insight YAML contains duplicate key: {key}.")
        mapping[key] = cast("Any", loader).construct_object(value_node, deep=deep)
    return mapping


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _mapping(value: object, *, where: str) -> Mapping:
    if not isinstance(value, dict):
        raise ContractError(f"{where}: expected a mapping.")
    return cast("Mapping", value)


def _text(value: object, *, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{where}: expected nonblank text.")
    return value


def _integer(value: object, *, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{where}: expected an integer.")
    return value


def _texts(value: object, *, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ContractError(f"{where}: expected a list.")
    return tuple(_text(item, where=where) for item in cast("list[object]", value))


def _decimal(value: object, *, where: str) -> Decimal:
    if not isinstance(value, str):
        raise ContractError(f"{where}: expected decimal text.")
    try:
        decimal = Decimal(value)
    except InvalidOperation:
        raise ContractError(f"{where}: expected decimal text.") from None
    if not decimal.is_finite() or decimal <= 0:
        raise ContractError(f"{where}: expected a finite positive decimal.")
    return decimal


def _non_negative_decimal(value: object, *, where: str) -> Decimal:
    if not isinstance(value, str):
        raise ContractError(f"{where}: expected decimal text.")
    try:
        decimal = Decimal(value)
    except InvalidOperation:
        raise ContractError(f"{where}: expected decimal text.") from None
    if not decimal.is_finite() or decimal < 0:
        raise ContractError(f"{where}: expected a finite non-negative decimal.")
    return decimal


def _predicate(value: object, *, where: str) -> InsightSlotPredicate:
    mapping = _mapping(value, where=where)
    expected = {
        "delivery_follow_up_evidence",
        "materiality",
        "required_evidence_inputs",
        "required_finding_kinds",
    }
    if frozenset(mapping) != frozenset(expected):
        raise ContractError(f"{where}: predicate fields are not exact.")
    materiality = _materiality(mapping["materiality"], where=f"{where}.materiality")
    try:
        return InsightSlotPredicate(
            tuple(
                cast(
                    "InsightEvidenceInput",
                    _enum(InsightEvidenceInput, item, where=f"{where}.required_evidence_inputs"),
                )
                for item in _texts(
                    mapping["required_evidence_inputs"],
                    where=f"{where}.required_evidence_inputs",
                )
            ),
            tuple(
                cast(
                    "InsightFindingKind",
                    _enum(InsightFindingKind, item, where=f"{where}.required_finding_kinds"),
                )
                for item in _texts(
                    mapping["required_finding_kinds"],
                    where=f"{where}.required_finding_kinds",
                )
            ),
            materiality,
            tuple(
                cast(
                    "DeliveryFollowUpEvidence",
                    _enum(
                        DeliveryFollowUpEvidence,
                        item,
                        where=f"{where}.delivery_follow_up_evidence",
                    ),
                )
                for item in _texts(
                    mapping["delivery_follow_up_evidence"],
                    where=f"{where}.delivery_follow_up_evidence",
                )
            ),
        )
    except ValueError as error:
        raise ContractError(f"{where}: {error}") from error


def _horizons(value: object, *, where: str) -> HorizonThresholds:
    mapping = _mapping(value, where=where)
    if frozenset(mapping) != frozenset(role.value for role in InsightHorizonRole):
        raise ContractError(f"{where}: every horizon role is declared exactly once.")
    return HorizonThresholds(
        *(
            _decimal(mapping[role.value], where=f"{where}.{role.value}")
            for role in (
                InsightHorizonRole.OPERATIONAL_PULSE,
                InsightHorizonRole.TACTICAL_REVIEW,
                InsightHorizonRole.PATTERN_CONFIRMATION,
            )
        )
    )


def _materiality(value: object, *, where: str) -> InsightMateriality:
    """Parse `bases` (per basis: `per_unit` and `per_metric`) and `near_zero_prior` strictly."""
    mapping = _mapping(value, where=where)
    if frozenset(mapping) != frozenset({"bases", "near_zero_prior"}):
        raise ContractError(f"{where}: fields are not exact.")
    bases: dict[OperandBasis, BasisThresholds] = {}
    for basis_name, raw in _mapping(mapping["bases"], where=f"{where}.bases").items():
        basis = cast("OperandBasis", _enum(OperandBasis, basis_name, where=f"{where}.bases"))
        rows = _mapping(raw, where=f"{where}.bases.{basis_name}")
        if frozenset(rows) != frozenset({"per_unit", "per_metric"}):
            raise ContractError(f"{where}.bases.{basis_name}: fields are not exact.")
        per_unit = {
            cast(
                "FindingUnit", _enum(FindingUnit, unit, where=f"{where}.bases.{basis_name}")
            ): _horizons(row, where=f"{where}.bases.{basis_name}.per_unit.{unit}")
            for unit, row in _mapping(
                rows["per_unit"], where=f"{where}.bases.{basis_name}.per_unit"
            ).items()
        }
        per_metric = {
            _text(metric, where=f"{where}.bases.{basis_name}.per_metric"): _horizons(
                row, where=f"{where}.bases.{basis_name}.per_metric.{metric}"
            )
            for metric, row in _mapping(
                rows["per_metric"], where=f"{where}.bases.{basis_name}.per_metric"
            ).items()
        }
        try:
            bases[basis] = BasisThresholds(per_unit, per_metric)
        except ValueError as error:
            raise ContractError(f"{where}.bases.{basis_name}: {error}") from error
    near_zero = _mapping(mapping["near_zero_prior"], where=f"{where}.near_zero_prior")
    per_scale: dict[MetricScale, Decimal] = {}
    unit_price: dict[str, Decimal] = {}
    for scale_name, raw in near_zero.items():
        scale = cast(
            "MetricScale", _enum(MetricScale, scale_name, where=f"{where}.near_zero_prior")
        )
        if scale is MetricScale.UNIT_PRICE:
            unit_price = {
                _text(metric, where=f"{where}.near_zero_prior.unit_price"): _non_negative_decimal(
                    bound, where=f"{where}.near_zero_prior.unit_price.{metric}"
                )
                for metric, bound in _mapping(
                    raw, where=f"{where}.near_zero_prior.unit_price"
                ).items()
            }
        else:
            per_scale[scale] = _non_negative_decimal(
                raw, where=f"{where}.near_zero_prior.{scale_name}"
            )
    try:
        return InsightMateriality(bases, NearZeroPriors(per_scale, unit_price))
    except ValueError as error:
        raise ContractError(f"{where}: {error}") from error


def _enum(enum_type: type[StrEnum], value: object, *, where: str) -> StrEnum:
    try:
        return enum_type(_text(value, where=where))
    except ValueError:
        raise ContractError(f"{where}: unsupported enum value.") from None


def _schema(value: object, *, where: str) -> InsightStructuredOutputSchema:
    mapping = _mapping(value, where=where)
    if frozenset(mapping) != frozenset({"fields", "id"}):
        raise ContractError(f"{where}: structured_output_schema fields are not exact.")
    try:
        return InsightStructuredOutputSchema(
            _text(mapping["id"], where=f"{where}.id"),
            _texts(mapping["fields"], where=f"{where}.fields"),
        )
    except ValueError as error:
        raise ContractError(f"{where}: {error}") from error


def _deep(value: object, *, where: str) -> InsightDeepContext | None:
    if value is None:
        return None
    mapping = _mapping(value, where=where)
    if frozenset(mapping) != frozenset({"allowed_inputs", "structured_output_schema"}):
        raise ContractError(f"{where}: deep fields are not exact.")
    try:
        return InsightDeepContext(
            _texts(mapping["allowed_inputs"], where=f"{where}.allowed_inputs"),
            _schema(
                mapping["structured_output_schema"],
                where=f"{where}.structured_output_schema",
            ),
        )
    except ValueError as error:
        raise ContractError(f"{where}: {error}") from error


def _finite_decimal(value: object, *, where: str) -> Decimal:
    if not isinstance(value, str):
        raise ContractError(f"{where}: expected decimal text.")
    try:
        decimal = Decimal(value)
    except InvalidOperation:
        raise ContractError(f"{where}: expected decimal text.") from None
    if not decimal.is_finite():
        raise ContractError(f"{where}: expected a finite decimal.")
    return decimal


def _findings(value: object, *, where: str) -> tuple[DeterministicFinding, ...]:
    if not isinstance(value, list):
        raise ContractError(f"{where}: findings must be a list.")
    findings: list[DeterministicFinding] = []
    for raw in cast("list[object]", value):
        mapping = _mapping(raw, where=where)
        if frozenset(mapping) != frozenset({"name", "value"}):
            raise ContractError(f"{where}: finding fields are not exact.")
        findings.append(
            DeterministicFinding(
                _text(mapping["name"], where=f"{where}.name"),
                _finite_decimal(mapping["value"], where=f"{where}.value"),
            )
        )
    return tuple(findings)


def _draft(value: object, *, where: str) -> InsightCalibrationDraft:
    mapping = _mapping(value, where=where)
    if frozenset(mapping) != frozenset({"sentences"}):
        raise ContractError(f"{where}: draft fields are not exact.")
    try:
        return InsightCalibrationDraft(
            _texts(mapping["sentences"], where=f"{where}.sentences"),
        )
    except ValueError as error:
        raise ContractError(f"{where}: {error}") from error


def _calibrations(value: object, *, where: str) -> tuple[InsightCalibrationCase, ...]:
    if not isinstance(value, list):
        raise ContractError(f"{where}: calibration_cases must be a list.")
    required = frozenset({"expected_outcome", "findings", "horizon", "input_summary", "kind"})
    cases: list[InsightCalibrationCase] = []
    for raw in cast("list[object]", value):
        mapping = _mapping(raw, where=where)
        if frozenset(mapping) - {"draft"} != required:
            raise ContractError(f"{where}: calibration case fields are not exact.")
        try:
            kind = cast(
                "InsightCalibrationKind",
                _enum(InsightCalibrationKind, mapping["kind"], where=f"{where}.kind"),
            )
            cases.append(
                InsightCalibrationCase(
                    kind,
                    _text(mapping["input_summary"], where=f"{where}.input_summary"),
                    _text(mapping["expected_outcome"], where=f"{where}.expected_outcome"),
                    _findings(mapping["findings"], where=f"{where}.findings"),
                    cast(
                        "InsightHorizonRole",
                        _enum(InsightHorizonRole, mapping["horizon"], where=f"{where}.horizon"),
                    ),
                    _draft(mapping["draft"], where=f"{where}.draft")
                    if "draft" in mapping
                    else None,
                )
            )
        except ValueError as error:
            raise ContractError(f"{where}: {error}") from error
    return tuple(cases)


def insight_contract_from_mapping(raw: Mapping, *, where: str) -> InsightContract:
    """Construct the domain's one insight schema from a strict YAML declaration."""
    expected = {
        "allowed_inputs",
        "calibration_cases",
        "deep",
        "deterministic_findings",
        "display_name",
        "id",
        "initial_fictional_prompt",
        "materiality_cases",
        "output_class",
        "predicate",
        "purpose",
        "silence_condition",
        "structured_output_schema",
        "version",
    }
    # FR-11 (design 4.12): every model card contract declares its next-step kinds; the
    # deterministic movers slots never call a model, so they declare none.
    if raw.get("id") not in {slot.value for slot in DETERMINISTIC_SLOT_IDS}:
        expected.add("next_steps")
    # The frequency-saturation rule's policy values are versioned in its contract (design §6.3).
    if raw.get("id") == InsightSlotId.FREQUENCY_SATURATION.value:
        expected.add("saturation_policy")
    # So are retention-shift's minimum base and interval width (design §6.6).
    if raw.get("id") == InsightSlotId.RETENTION_SHIFT.value:
        expected.add("shift_policy")
    # And segment-over-index's minimum buyers and family alpha (design §6.5).
    if raw.get("id") == InsightSlotId.SEGMENT_OVER_INDEX.value:
        expected.add("over_index_policy")
    if frozenset(raw) != frozenset(expected):
        raise ContractError(f"{where}: insight contract fields are not exact.")
    prompt = raw["initial_fictional_prompt"]
    if prompt is not None and not isinstance(prompt, str):
        raise ContractError(f"{where}: initial_fictional_prompt must be text or null.")
    materiality_cases_from_mapping(raw, where=where)
    try:
        return InsightContract(
            cast("InsightSlotId", _enum(InsightSlotId, raw["id"], where=f"{where}.id")),
            _integer(raw["version"], where=f"{where}.version"),
            _text(raw["purpose"], where=f"{where}.purpose"),
            _text(raw["display_name"], where=f"{where}.display_name"),
            cast(
                "InsightOutputClass",
                _enum(InsightOutputClass, raw["output_class"], where=f"{where}.output_class"),
            ),
            _texts(raw["allowed_inputs"], where=f"{where}.allowed_inputs"),
            tuple(
                cast(
                    "InsightFindingKind",
                    _enum(InsightFindingKind, item, where=f"{where}.deterministic_findings"),
                )
                for item in _texts(
                    raw["deterministic_findings"],
                    where=f"{where}.deterministic_findings",
                )
            ),
            _predicate(raw["predicate"], where=f"{where}.predicate"),
            _text(raw["silence_condition"], where=f"{where}.silence_condition"),
            _schema(raw["structured_output_schema"], where=f"{where}.structured_output_schema"),
            prompt,
            _calibrations(raw["calibration_cases"], where=f"{where}.calibration_cases"),
            _deep(raw["deep"], where=f"{where}.deep"),
            _next_step_kinds(raw.get("next_steps"), where=f"{where}.next_steps"),
            _saturation_policy(raw.get("saturation_policy"), where=f"{where}.saturation_policy"),
            _shift_policy(raw.get("shift_policy"), where=f"{where}.shift_policy"),
            _over_index_policy(raw.get("over_index_policy"), where=f"{where}.over_index_policy"),
        )
    except ValueError as error:
        raise ContractError(f"{where}: {error}") from error


_SATURATION_FIELDS = frozenset({"shoppers_min", "purchasers_min", "tolerance", "z"})
_SHIFT_FIELDS = frozenset({"cohort_min", "z"})
_OVER_INDEX_FIELDS = frozenset({"buyers_min", "family_alpha", "correction"})


def _over_index_policy(value: object, *, where: str) -> OverIndexPolicy | None:
    """Read `over_index_policy: {buyers_min, family_alpha, correction: bonferroni}` strictly."""
    if value is None:
        return None
    mapping = _mapping(value, where=where)
    if frozenset(mapping) != _OVER_INDEX_FIELDS:
        raise ContractError(f"{where}: fields are not exact.")
    if mapping["correction"] != "bonferroni":
        raise ContractError(f"{where}.correction: bonferroni is the one declared correction.")
    return OverIndexPolicy(
        _integer(mapping["buyers_min"], where=f"{where}.buyers_min"),
        _decimal(mapping["family_alpha"], where=f"{where}.family_alpha"),
    )


def _shift_policy(value: object, *, where: str) -> ShiftPolicy | None:
    """Read `shift_policy: {cohort_min, z}` strictly."""
    if value is None:
        return None
    mapping = _mapping(value, where=where)
    if frozenset(mapping) != _SHIFT_FIELDS:
        raise ContractError(f"{where}: fields are not exact.")
    return ShiftPolicy(
        _integer(mapping["cohort_min"], where=f"{where}.cohort_min"),
        _decimal(mapping["z"], where=f"{where}.z"),
    )


def _saturation_policy(value: object, *, where: str) -> SaturationPolicy | None:
    """Read `saturation_policy: {shoppers_min, purchasers_min, tolerance, z}` strictly."""
    if value is None:
        return None
    mapping = _mapping(value, where=where)
    if frozenset(mapping) != _SATURATION_FIELDS:
        raise ContractError(f"{where}: fields are not exact.")
    return SaturationPolicy(
        _integer(mapping["shoppers_min"], where=f"{where}.shoppers_min"),
        _integer(mapping["purchasers_min"], where=f"{where}.purchasers_min"),
        _decimal(mapping["tolerance"], where=f"{where}.tolerance"),
        _decimal(mapping["z"], where=f"{where}.z"),
    )


def _next_step_kinds(value: object, *, where: str) -> tuple[InsightNextStepKind, ...]:
    """Read ``next_steps: {allowed_kinds: [...]}`` strictly; an unknown kind is refused."""
    if value is None:
        return ()
    mapping = _mapping(value, where=where)
    if frozenset(mapping) != frozenset({"allowed_kinds"}):
        raise ContractError(f"{where}: expected exactly {{allowed_kinds: [...]}}.")
    kinds: object = mapping["allowed_kinds"]
    if not isinstance(kinds, list):
        raise ContractError(f"{where}.allowed_kinds: expected a list.")
    parsed = tuple(
        cast(
            "InsightNextStepKind",
            _enum(InsightNextStepKind, item, where=f"{where}.allowed_kinds"),
        )
        for item in cast("list[object]", kinds)
    )
    if InsightNextStepKind.OPEN_PRODUCT_DETAIL in parsed:
        # ADR-125 decision 5: the serve path writes this step for the baked movers product;
        # no prompt chooses it.
        raise ContractError(f"{where}.allowed_kinds: open_product_detail is served, not chosen.")
    return parsed


class MaterialityExpectation(StrEnum):
    """What a deterministic materiality case expects of its one finding (AC-11)."""

    ADMITTED = "admitted"
    SILENT = "silent"
    SUPPRESSED = "suppressed"
    NOT_BUILT = "not-built"
    """A percentage off a zero prior is refused at calculation, before Gate-1 (B-15)."""


@dataclass(frozen=True, slots=True)
class MaterialityCase:
    """One expected-silence or admission case a contract declares beside its thresholds."""

    name: str
    horizon: InsightHorizonRole
    basis: OperandBasis
    kind: InsightFindingKind
    metric: str
    """The artifact metric id, as builders name it."""
    current: Decimal
    prior: Decimal
    expect: MaterialityExpectation


_CASE_FIELDS = frozenset(
    {"name", "horizon", "basis", "kind", "metric", "current", "prior", "expect"}
)


def materiality_cases_from_mapping(raw: Mapping, *, where: str) -> tuple[MaterialityCase, ...]:
    """Parse a contract's `materiality_cases` strictly (design §4.3)."""
    value = raw.get("materiality_cases")
    if not isinstance(value, list) or not value:
        raise ContractError(f"{where}.materiality_cases: expected a nonempty list.")
    cases: list[MaterialityCase] = []
    for index, entry in enumerate(cast("list[object]", value)):
        label = f"{where}.materiality_cases[{index}]"
        mapping = _mapping(entry, where=label)
        if frozenset(mapping) != _CASE_FIELDS:
            raise ContractError(f"{label}: fields are not exact.")
        cases.append(
            MaterialityCase(
                _text(mapping["name"], where=f"{label}.name"),
                cast(
                    "InsightHorizonRole",
                    _enum(InsightHorizonRole, mapping["horizon"], where=f"{label}.horizon"),
                ),
                cast("OperandBasis", _enum(OperandBasis, mapping["basis"], where=f"{label}.basis")),
                cast(
                    "InsightFindingKind",
                    _enum(InsightFindingKind, mapping["kind"], where=f"{label}.kind"),
                ),
                _text(mapping["metric"], where=f"{label}.metric"),
                _finite_decimal(mapping["current"], where=f"{label}.current"),
                _finite_decimal(mapping["prior"], where=f"{label}.prior"),
                cast(
                    "MaterialityExpectation",
                    _enum(MaterialityExpectation, mapping["expect"], where=f"{label}.expect"),
                ),
            )
        )
    return tuple(cases)


def check_materiality_completeness(
    contract: InsightContract, catalog: Catalog, *, where: str
) -> None:
    """Require a threshold and a near-zero bound for every finding the slot can emit.

    Over the triples `ALLOWED_FINDING_UNITS` declares for the slot's required finding kinds:
    the declared bases are exactly those triples' bases; each triple has its metric's own
    `per_metric` row or its unit's row, and a unit price always has its own row; each
    percentage triple's prior scale has a bound (a unit price: per metric); and every
    `per_metric` key is a metric the slot emits at that basis whose unit is currency, count
    or unit price.
    """
    materiality = contract.predicate.materiality
    triples = tuple(
        triple
        for triple in ALLOWED_FINDING_UNITS.get(contract.id, frozenset())
        if triple[0] in contract.predicate.required_finding_kinds
    )
    expected_bases = {basis for _, _, basis in triples}
    if triples and set(materiality.bases) != expected_bases:
        raise ContractError(
            f"{where}.materiality.bases: declares "
            f"{sorted(item.value for item in materiality.bases)}, the slot emits "
            f"{sorted(item.value for item in expected_bases)}."
        )
    for kind, metric, basis in sorted(triples, key=lambda item: (item[2], item[1], item[0])):
        unit, scale = finding_unit(
            contract.id, kind, metric_id=metric, basis=basis, catalog=catalog
        )
        declared = materiality.bases[basis]
        key = dictionary_id(metric)
        if key not in declared.per_metric and (
            unit is FindingUnit.UNIT_PRICE or unit not in declared.per_unit
        ):
            raise ContractError(
                f"{where}.materiality.bases.{basis.value}: no threshold for {kind.value} "
                f"{metric} ({unit.value})."
            )
        if scale is not None:
            bounds = materiality.near_zero_prior
            present = (
                key in bounds.unit_price
                if scale is MetricScale.UNIT_PRICE
                else scale in bounds.per_scale
            )
            if not present:
                raise ContractError(
                    f"{where}.materiality.near_zero_prior: no bound for {scale.value} ({metric})."
                )
    for basis, declared in materiality.bases.items():
        emitted = {dictionary_id(metric) for _, metric, at in triples if at is basis}
        for key in declared.per_metric:
            if key not in emitted or metric_scale(key, catalog) not in {
                MetricScale.CURRENCY,
                MetricScale.COUNT,
                MetricScale.UNIT_PRICE,
            }:
                raise ContractError(
                    f"{where}.materiality.bases.{basis.value}.per_metric: {key} is not an "
                    "amount or unit-price metric this slot emits."
                )


def load_materiality_cases(root: Path) -> dict[InsightSlotId, tuple[MaterialityCase, ...]]:
    """Load every contract's materiality cases, keyed by slot."""
    found: dict[InsightSlotId, tuple[MaterialityCase, ...]] = {}
    for path in sorted((root / CONTRACTS_DIR / _INSIGHTS_DIRECTORY).glob("*.yaml")):
        raw = _mapping(
            cast("object", yaml.load(path.read_text(), Loader=_StrictSafeLoader)),  # noqa: S506  # nosec B506 # A-YAML-SAFELOADER-001
            where=str(path),
        )
        slot = cast("InsightSlotId", _enum(InsightSlotId, raw["id"], where=f"{path}.id"))
        found[slot] = materiality_cases_from_mapping(raw, where=str(path))
    return found


def load_insight_contracts(root: Path) -> list[tuple[Path, InsightContract]]:
    """Load every versioned command-center slot contract in deterministic path order."""
    directory = root / CONTRACTS_DIR / _INSIGHTS_DIRECTORY
    if not directory.exists():
        return []
    contracts: list[tuple[Path, InsightContract]] = []
    for path in sorted(directory.glob("*.yaml")):
        raw = yaml.load(path.read_text(), Loader=_StrictSafeLoader)  # noqa: S506  # nosec B506 # A-YAML-SAFELOADER-001
        mapping = _mapping(cast("object", raw), where=str(path))
        contracts.append((path, insight_contract_from_mapping(mapping, where=str(path))))
    # Completeness reads the metric dictionary, so it runs once every contract has parsed.
    catalog = load_catalog(root)
    for path, contract in contracts:
        check_materiality_completeness(contract, catalog, where=str(path))
        # ADR-122 decision 5: a step's targets come from the slot's served surface, so a kind
        # on a slot whose derivation builds none is a declaration failure, never a per-card raise.
        if contract.next_step_kinds and contract.id not in SURFACED_SLOTS:
            raise ContractError(
                f"{path}.next_steps: no derivation builds a served next-step surface for "
                f"{contract.id.value}, so it can't list a kind."
            )
    slot_order = {slot_id: index for index, slot_id in enumerate(InsightSlotId)}
    return sorted(contracts, key=lambda item: slot_order[item[1].id])
