# SPDX-License-Identifier: MIT-0
"""Load governed AMC query packs and bind their exact downstream artifacts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.attribution_models import load_attribution_models
from adapters.outbound.source_pack import PACKS_PATH, load_source_packs
from domain.curation.ingestion import DiscardPolicy
from domain.curation.mapping import delivered_name
from domain.data_contracts.contract import DataContract, NullMeaning, SourceFormat
from domain.producer.models import (
    DEFAULT_EXPECTED_BAR,
    AccessTier,
    GovernedQueryPack,
    InputParameter,
    LiveValidationPolicy,
    PackVariant,
    ParameterDataType,
    PeriodGrain,
    PeriodPolicy,
    PeriodValue,
    SuppressionPolicy,
    VariantTrigger,
    digest_bytes,
    digest_json,
)
from domain.producer.validation import REQUIRED_RESULT_COLUMNS, validate_pack
from domain.producer.variants import DeclaredVariant
from domain.semantics.source_pack import PackReadiness
from domain.shared_kernel.provenance import ProducerGovernance

DEFINITIONS_DIR = Path("contracts/amc/query-packs")
DEFINITION_SUFFIX = ".json"
_ATTRIBUTION_MODEL_COLUMN = "attribution_model_key"
_ATTRIBUTION_MODEL_LITERAL = re.compile(r"'([^']+)'\s+AS\s+attribution_model_key\b", re.IGNORECASE)


type JsonObject = dict[str, object]


def _object(value: object, label: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object.")
    return cast("JsonObject", value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonblank string.")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer.")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean.")
    return value


def _string_mapping(value: object, label: str) -> tuple[tuple[str, str], ...]:
    raw = _object(value, label)
    return tuple(
        sorted(
            (_text(name, f"{label} key"), _text(item, f"{label}.{name}"))
            for name, item in raw.items()
        )
    )


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list.")
    return tuple(_text(item, f"{label}[]") for item in cast("list[object]", value))


def _definition(path: Path) -> JsonObject:
    parsed = cast("object", json.loads(path.read_text()))
    raw = _object(parsed, str(path))
    required = {
        "schemaVersion",
        "packId",
        "packVersion",
        "contractId",
        "sourcePackId",
        "queryFile",
        "accessTier",
        "liveValidation",
        "maxResultBytes",
        "executionOptions",
        "safetyBlockers",
        "outputColumns",
    }
    # ADR-110: every field after rejectSuppressedRows is optional with a stated default, so a
    # pack that declares none keeps working unchanged. Another design's pack fields join this
    # set the same way (C-14); whichever lands second rebases.
    optional = {
        "rejectSuppressedRows",
        "suppressionPolicy",
        "periodPolicy",
        "cadenceLagDays",
        "inputParameters",
        "expectedBar",
        "retainedVersions",
        "variants",
    }
    if required - set(raw) or set(raw) - required - optional or raw.get("schemaVersion") != 1:
        raise ValueError(
            f"{path}: query pack requires schemaVersion 1 and keys {sorted(required)}; "
            f"optional keys are {sorted(optional)}."
        )
    if "rejectSuppressedRows" in raw and "suppressionPolicy" in raw:
        raise ValueError(
            f"{path}: declare suppressionPolicy or rejectSuppressedRows, not both; "
            "rejectSuppressedRows: true reads as suppressionPolicy: reject_delivery."
        )
    return raw


def _suppression_policy(raw: JsonObject) -> SuppressionPolicy | None:
    if "suppressionPolicy" in raw:
        return SuppressionPolicy(_text(raw["suppressionPolicy"], "suppressionPolicy"))
    if "rejectSuppressedRows" in raw and _boolean(
        raw["rejectSuppressedRows"], "rejectSuppressedRows"
    ):
        return SuppressionPolicy.REJECT_DELIVERY
    return None


def _period_policy(raw: JsonObject) -> PeriodPolicy | None:
    if "periodPolicy" not in raw:
        return None
    policy = _object(raw["periodPolicy"], "periodPolicy")
    if set(policy) != {"grains"}:
        raise ValueError("periodPolicy takes exactly one key, grains.")
    return PeriodPolicy(
        tuple(PeriodGrain(grain) for grain in _strings(policy["grains"], "periodPolicy.grains"))
    )


def _input_parameters(raw: JsonObject) -> tuple[InputParameter, ...]:
    if "inputParameters" not in raw:
        return ()
    declared = _object(raw["inputParameters"], "inputParameters")
    parameters: list[InputParameter] = []
    for name, value in declared.items():
        spec = _object(value, f"inputParameters.{name}")
        if set(spec) not in ({"commerceIdentity", "dataType"}, {"periodValue", "dataType"}):
            raise ValueError(
                f"inputParameters.{name} takes dataType and exactly one of commerceIdentity and "
                "periodValue."
            )
        data_type = ParameterDataType(_text(spec["dataType"], f"{name}.dataType"))
        parameters.append(
            InputParameter(
                name=name,
                data_type=data_type,
                commerce_identity=_text(spec["commerceIdentity"], f"{name}.commerceIdentity"),
            )
            if "commerceIdentity" in spec
            else InputParameter(
                name=name,
                data_type=data_type,
                period_value=PeriodValue(_text(spec["periodValue"], f"{name}.periodValue")),
            )
        )
    return tuple(sorted(parameters, key=lambda parameter: parameter.name))


_RETAINED_KEYS = frozenset(
    {"packVersion", "packDigest", "queryDigest", "contractDigest", "sourcePackDigest"}
)


def _retained_governance(
    raw: JsonObject, *, pack_id: str, contract: DataContract
) -> tuple[ProducerGovernance, ...]:
    """Read each superseded version's exact binding, so its landed deliveries still load."""
    if "retainedVersions" not in raw:
        return ()
    values = raw["retainedVersions"]
    if not isinstance(values, list) or not values:
        raise ValueError("retainedVersions must be a nonempty list when declared.")
    retained: list[ProducerGovernance] = []
    for item in cast("list[object]", values):
        entry = _object(item, "retainedVersions[]")
        if frozenset(entry) != _RETAINED_KEYS:
            raise ValueError(f"retainedVersions[] takes exactly {sorted(_RETAINED_KEYS)}.")
        retained.append(
            ProducerGovernance(
                query_pack_id=pack_id,
                query_pack_version=_integer(entry["packVersion"], "retainedVersions.packVersion"),
                query_pack_digest=_text(entry["packDigest"], "retainedVersions.packDigest"),
                query_digest=_text(entry["queryDigest"], "retainedVersions.queryDigest"),
                data_contract_id=contract.contract_id,
                data_contract_digest=_text(
                    entry["contractDigest"], "retainedVersions.contractDigest"
                ),
                source_pack_id=contract.contract_id,
                source_pack_digest=_text(
                    entry["sourcePackDigest"], "retainedVersions.sourcePackDigest"
                ),
            )
        )
    return tuple(sorted(retained, key=lambda item: item.query_pack_version))


_VARIANT_KEYS = frozenset({"variant", "packVersion", "queryFile", "outputColumns", "trigger"})


def _query_sql(path: Path, query_file: str) -> str:
    """Read a sibling query file as the one-line SQL AMC executes."""
    if Path(query_file).name != query_file or not query_file.endswith(".sql"):
        raise ValueError(f"{path}: queryFile must be a sibling .sql filename.")
    query_path = path.parent / query_file
    if not query_path.is_file():
        raise ValueError(f"{path}: query file {query_file!r} does not exist.")
    return " ".join(query_path.read_text().split())


def _variants(path: Path, raw: JsonObject, required: frozenset[str]) -> tuple[PackVariant, ...]:
    """Read the declared pre-built alternatives (plan D9); absent means none."""
    if "variants" not in raw:
        return ()
    values = raw["variants"]
    if not isinstance(values, list) or not values:
        raise ValueError(f"{path}: variants must be a nonempty list when declared.")
    variants: list[PackVariant] = []
    for item in cast("list[object]", values):
        entry = _object(item, "variants[]")
        if frozenset(entry) != _VARIANT_KEYS:
            raise ValueError(f"{path}: variants[] takes exactly {sorted(_VARIANT_KEYS)}.")
        sql = _query_sql(path, _text(entry["queryFile"], "variants.queryFile"))
        columns = _strings(entry["outputColumns"], "variants.outputColumns")
        if frozenset(columns) != required:
            raise ValueError(
                f"{path}: variant outputColumns disagree with the contract; a variant writes the "
                "same contract as its pack."
            )
        variants.append(
            PackVariant(
                variant=_text(entry["variant"], "variants.variant"),
                pack_version=_integer(entry["packVersion"], "variants.packVersion"),
                sql=sql,
                query_digest=digest_bytes(sql.encode()),
                expected_columns=columns,
                trigger=VariantTrigger(_text(entry["trigger"], "variants.trigger")),
            )
        )
    automatic = [item for item in variants if item.trigger is not VariantTrigger.MANUAL]
    if len(automatic) > 1:
        raise ValueError(f"{path}: a pack declares at most one automatic variant.")
    return tuple(variants)


def _require_variant_cells(path: Path, pack: GovernedQueryPack, contract: DataContract) -> None:
    """Every declared variant has its own expected cells in the contract, and no other does."""
    cells = contract.expected_cells
    declared = {name for name, _sets in (() if cells is None else cells.variants)}
    if declared != {item.variant for item in pack.variants}:
        raise ValueError(
            f"{path}: the contract's expected_cells variants must be exactly the pack's variants."
        )


def _require_pack_versioned_fields(path: Path, pack_version: int, contract: DataContract) -> None:
    """Refuse a pack older than the version that first outputs one of its contract's columns."""
    for field in contract.pack_versioned_fields:
        if cast("int", field.present_from_pack_version) > pack_version:
            raise ValueError(
                f"{path}: {field.target_column} is present from pack version "
                f"{field.present_from_pack_version}, but this pack is version {pack_version}."
            )


def installed_governance(packs: tuple[GovernedQueryPack, ...]) -> tuple[ProducerGovernance, ...]:
    """Every binding a landed manifest may carry: each pack's current and retained versions.

    The one set every composition root (load, ingest, reprocess) checks a manifest against, so
    a pack-v1 delivery of a pack now at version 2 loads, with its added columns NULL.
    """
    return tuple(
        governance
        for pack in packs
        for governance in (
            pack.governance,
            *pack.retained_governance,
            *pack.variant_governance,
        )
    )


def _required_columns(contract: DataContract) -> frozenset[str]:
    required = {
        delivered_name(field) for field in contract.fields if not field.has_no_delivered_value
    }
    if contract.suppression is not None:
        required.update(contract.suppression.all_columns)
    # Read by the load's currency check and never stored (`currency_minimums`).
    required.update(contract.delivered_check_columns)
    required.update(REQUIRED_RESULT_COLUMNS)
    return frozenset(required)


def validate_attribution_model_binding(root: Path, contract: DataContract, sql: str) -> None:
    """Bind an attribution producer to the same closed vocabulary as its consumers."""
    mappings = tuple(
        field for field in contract.fields if field.target_column == _ATTRIBUTION_MODEL_COLUMN
    )
    if not mappings:
        return
    sql_values = tuple(_ATTRIBUTION_MODEL_LITERAL.findall(sql))
    if (
        len(mappings) == 1
        and not mappings[0].value_domain
        and mappings[0].assigned_by_load
        and mappings[0].null_meaning is NullMeaning.NOT_APPLICABLE_UNDER_OPTIONS
    ):
        # The column exists from creation for a branch no pack version writes yet (packs 6-7:
        # the credit branch isn't built, plan D11, D12), so it binds no vocabulary, and SQL
        # emitting a model would be a branch its contract doesn't declare.
        if sql_values:
            raise ValueError(
                f"{contract.contract_id}: SQL emits {_ATTRIBUTION_MODEL_COLUMN} branches the "
                "contract declares not applicable."
            )
        return
    if len(mappings) != 1 or not mappings[0].value_domain:
        raise ValueError(
            f"{contract.contract_id}: {_ATTRIBUTION_MODEL_COLUMN} must have one nonempty "
            "contract value_domain."
        )

    contract_keys = frozenset(mappings[0].value_domain)
    registry_keys = frozenset(load_attribution_models(root).keys)
    sql_keys = frozenset(sql_values)
    if len(sql_values) != len(sql_keys):
        raise ValueError(
            f"{contract.contract_id}: SQL repeats an {_ATTRIBUTION_MODEL_COLUMN} branch."
        )
    if contract_keys != registry_keys or contract_keys != sql_keys:
        raise ValueError(
            f"{contract.contract_id}: attribution model vocabulary drift; "
            f"contract={sorted(contract_keys)}, registry={sorted(registry_keys)}, "
            f"sql={sorted(sql_keys)}."
        )


def _source_pack_payload(root: Path, contract_id: str) -> tuple[int, JsonObject]:
    path = root / PACKS_PATH
    document = _object(cast("object", json.loads(path.read_text())), str(path))
    version = _integer(document.get("version"), "source-pack registry version")
    values = document.get("sourcePacks")
    if not isinstance(values, list):
        raise ValueError("source-pack registry sourcePacks must be a list.")
    matches = [
        _object(cast("object", item), "source-pack entry")
        for item in cast("list[object]", values)
        if isinstance(item, dict) and cast("JsonObject", item).get("contractId") == contract_id
    ]
    if len(matches) != 1:
        raise ValueError(f"source-pack registry must contain exactly one {contract_id!r} entry.")
    return version, matches[0]


def _require_discard_shape(path: Path, pack: GovernedQueryPack, contract: DataContract) -> None:
    """Pair `discard_suppressed_rows` with a contract that records `cell_state` (ADR-099 pt 7).

    The load discards withheld rows only where a missing cell can be written `not_returned`, and
    a contract recording cells is loaded only under a pack that discards. An SCD2 dimension that
    stores no discriminator discards too: its history keeps an omitted key's earlier version.
    """
    discards = pack.suppression_policy is SuppressionPolicy.DISCARD_SUPPRESSED_ROWS
    if contract.discards_into_history:
        if not discards:
            raise ValueError(
                f"{path}: a dimension that stores no suppression discriminator loads under "
                "suppressionPolicy discard_suppressed_rows (ADR-099 point 7, ADR-103)."
            )
        # A dimension has no cells to write not_returned: a withheld row is dropped and the SCD2
        # history keeps the key's earlier version (packs 9 and 13, ADR-103; design §5.4).
        return
    if discards != contract.records_cell_state:
        raise ValueError(
            f"{path}: suppressionPolicy discard_suppressed_rows and a contract recording "
            "cell_state go together (ADR-099 point 7)."
        )


def load_query_packs(root: Path) -> tuple[GovernedQueryPack, ...]:
    """Load every definition and fail closed on any binding or conformance drift."""
    contracts = {contract.contract_id: (path, contract) for path, contract in load_contracts(root)}
    source_packs = {pack.contract_id: pack for pack in load_source_packs(root)}
    definitions = root / DEFINITIONS_DIR
    if not definitions.is_dir():
        raise ValueError(f"query-pack directory is missing at {definitions}.")

    packs: list[GovernedQueryPack] = []
    for path in sorted(definitions.glob(f"*{DEFINITION_SUFFIX}")):
        raw = _definition(path)
        contract_id = _text(raw["contractId"], "contractId")
        source_pack_id = _text(raw["sourcePackId"], "sourcePackId")
        if contract_id not in contracts:
            raise ValueError(f"{path}: contract {contract_id!r} does not exist.")
        if source_pack_id != contract_id or source_pack_id not in source_packs:
            raise ValueError(f"{path}: sourcePackId must name the contract's Semantic V2 pack.")
        contract_path, contract = contracts[contract_id]
        source_pack = source_packs[source_pack_id]
        if contract.source_format is not SourceFormat.CSV or contract.source_system != "amc":
            raise ValueError(f"{path}: governed AMC query packs require an AMC CSV contract.")
        if source_pack.readiness is not PackReadiness.READY:
            raise ValueError(f"{path}: source pack {source_pack_id!r} is not ready.")

        sql = _query_sql(path, _text(raw["queryFile"], "queryFile"))

        expected_columns = _strings(raw["outputColumns"], "outputColumns")
        required_columns = _required_columns(contract)
        if frozenset(expected_columns) != required_columns:
            missing = sorted(required_columns - frozenset(expected_columns))
            extra = sorted(frozenset(expected_columns) - required_columns)
            raise ValueError(
                f"{path}: outputColumns disagree with contract; missing={missing}, extra={extra}."
            )

        source_registry_version, source_payload = _source_pack_payload(root, source_pack_id)
        pack = GovernedQueryPack(
            pack_id=_text(raw["packId"], "packId"),
            pack_version=_integer(raw["packVersion"], "packVersion"),
            contract_id=contract.contract_id,
            contract_version=contract.contract_version,
            contract_digest=digest_bytes(contract_path.read_bytes()),
            source_pack_id=source_pack_id,
            source_pack_version=source_registry_version,
            source_pack_digest=digest_json(
                {"version": source_registry_version, "sourcePack": source_payload}
            ),
            source_system=contract.source_system,
            source_dataset=contract.source_dataset,
            sql=sql,
            query_digest=digest_bytes(sql.encode()),
            expected_columns=expected_columns,
            execution_options=_string_mapping(raw["executionOptions"], "executionOptions"),
            max_result_bytes=_integer(raw["maxResultBytes"], "maxResultBytes"),
            access_tier=AccessTier(_text(raw["accessTier"], "accessTier")),
            live_validation=LiveValidationPolicy(_text(raw["liveValidation"], "liveValidation")),
            safety_blockers=_strings(raw["safetyBlockers"], "safetyBlockers"),
            suppression_policy=_suppression_policy(raw),
            period_policy=_period_policy(raw),
            cadence_lag_days=(
                _integer(raw["cadenceLagDays"], "cadenceLagDays")
                if "cadenceLagDays" in raw
                else None
            ),
            input_parameters=_input_parameters(raw),
            expected_bar=(
                _integer(raw["expectedBar"], "expectedBar")
                if "expectedBar" in raw
                else DEFAULT_EXPECTED_BAR
            ),
            retained_governance=_retained_governance(
                raw, pack_id=_text(raw["packId"], "packId"), contract=contract
            ),
            variants=_variants(path, raw, required_columns),
        )
        _require_discard_shape(path, pack, contract)
        _require_pack_versioned_fields(path, pack.pack_version, contract)
        _require_variant_cells(path, pack, contract)
        for binding in (pack, *(pack.variant_binding(item.variant) for item in pack.variants)):
            validate_attribution_model_binding(root, contract, binding.sql)
            report = validate_pack(binding)
            if not report.valid:
                details = "; ".join(f"{item.code}: {item.detail}" for item in report.findings)
                raise ValueError(f"{path}: {details}")
        packs.append(pack)

    identities = tuple(pack.pack_id for pack in packs)
    if not packs:
        raise ValueError(f"no query packs found under {definitions}.")
    if len(set(identities)) != len(identities):
        raise ValueError("query-pack definitions repeat a packId.")
    return tuple(packs)


def discard_policies(packs: tuple[GovernedQueryPack, ...]) -> tuple[DiscardPolicy, ...]:
    """Project the packs declaring `discard_suppressed_rows` into the load's policy (ADR-099)."""
    return tuple(
        DiscardPolicy(
            query_pack_id=pack.pack_id,
            contract_id=pack.contract_id,
            expected_bar=pack.expected_bar,
            variants=tuple(
                DeclaredVariant(item.variant, item.pack_version, item.trigger)
                for item in pack.variants
            ),
        )
        for pack in packs
        if pack.suppression_policy is SuppressionPolicy.DISCARD_SUPPRESSED_ROWS
    )


def pack_by_id(root: Path, pack_id: str) -> GovernedQueryPack:
    """Resolve one exact pack identity without a fallback or fuzzy match."""
    matches = [pack for pack in load_query_packs(root) if pack.pack_id == pack_id]
    if len(matches) != 1:
        raise ValueError(f"query pack {pack_id!r} is not installed exactly once.")
    return matches[0]


def pack_as_dict(pack: GovernedQueryPack, *, include_sql: bool = True) -> dict[str, object]:
    """Render one pack for local distribution and the generated registry.

    A declared variant renders inside its pack, with its own digest and, for distribution, its SQL,
    so `producer pack-describe` shows everything a producer may run.
    """
    variants = (
        {
            "variantBindings": [
                {
                    **item.declared,
                    "packDigest": pack.variant_binding(item.variant).digest,
                    **({"sql": item.sql} if include_sql else {}),
                }
                for item in pack.variants
            ]
        }
        if pack.variants
        else {}
    )
    return {
        **variants,
        "accessTier": pack.access_tier.value,
        "contract": {
            "digest": pack.contract_digest,
            "id": pack.contract_id,
            "version": pack.contract_version,
        },
        "expectedColumns": list(pack.expected_columns),
        "executionOptions": dict(pack.execution_options),
        "liveValidation": pack.live_validation.value,
        "maxResultBytes": pack.max_result_bytes,
        "packDigest": pack.digest,
        "packId": pack.pack_id,
        "packVersion": pack.pack_version,
        "queryDigest": pack.query_digest,
        **({"sql": pack.sql} if include_sql else {}),
        "safetyBlockers": list(pack.safety_blockers),
        **pack.declared_fields,
        "sourceDataset": pack.source_dataset,
        "sourcePack": {
            "digest": pack.source_pack_digest,
            "id": pack.source_pack_id,
            "version": pack.source_pack_version,
        },
        "sourceSystem": pack.source_system,
    }
