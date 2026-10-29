# SPDX-License-Identifier: MIT-0
"""Generate and check the execution-graph protocol projections (S1, V-6).

`contracts/operations/execution-graphs.yaml` is the single source. This job renders it into a
pure Python module and a TypeScript module, and refuses to write either unless the decision
table is total over its closed inputs (`DECISION_TABLE_NOT_TOTAL`). The check also proves the
pinned historical replay band equals the one the fixture planner computes from its own output.

    python -m adapters.jobs.data_flow_registry.execution_graphs [--check] [--deployable]

`--deployable` (slice 7, D40) also refuses a contract whose
`write_unit_batching.statement_allowance_ms` is still null, so no deploy path ships a build that
lacks the derived allowance.
"""

from __future__ import annotations

import hashlib
import json
import pprint
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, cast

from adapters.outbound import repository_yaml

CONTRACT_PATH: Final = Path("contracts/operations/execution-graphs.yaml")
PYTHON_PATH: Final = Path("src/domain/operations/generated_execution_graphs.py")
TYPESCRIPT_PATH: Final = Path("infra/lib/generated/execution-graph-protocol.ts")
_CHECK: Final = "--check"
_DEPLOYABLE: Final = "--deployable"
_ARGUMENTS: Final = frozenset({_CHECK, _DEPLOYABLE})
BATCHING_INVALID: Final = "WRITE_UNIT_BATCHING_INVALID"
PENDING_PROBE: Final = "WRITE_UNIT_BATCHING_PENDING_PROBE"
_ROWS_WITH_RETRY: Final = tuple(range(1, 21))
_RETRY_ROW: Final = 11
PINNED_BOUNDS: Final = {
    # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    "v1": "fc05393dde7138d81eb334ba479c2d0c37be7dc52408d33f3814666446b63800",
}
"""BF-3: each protocol version's bounds digest. A changed bound or ceiling is a new protocol
version, so generation refuses a v1 contract whose bounds differ from these."""


class ExecutionGraphContractError(ValueError):
    """The contract is malformed or its decision table is not total.

    `code` names the refusal where a caller branches on it (slice 7's batching block and deploy
    gate); older refusals carry their code in the message only, and leave `code` None.
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def bounds_digest(bounds: dict[str, Any]) -> str:
    """Return the canonical digest of a contract's bounds."""
    canonical = json.dumps(bounds, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def load_contract(root: Path) -> dict[str, Any]:
    """Read the contract and validate its closed shape."""
    document = repository_yaml.safe_load((root / CONTRACT_PATH).read_text())
    if not isinstance(document, dict):
        raise ExecutionGraphContractError("the execution-graph contract is malformed.")
    contract = cast("dict[str, Any]", document)
    if contract.get("contract") != "execution-graphs":
        raise ExecutionGraphContractError("the execution-graph contract is malformed.")
    rows = [int(item["row"]) for item in contract["decision_table"]]
    expected = [
        row for row in _ROWS_WITH_RETRY if contract["retry_row_approved"] or row != _RETRY_ROW
    ]
    if rows != expected:
        raise ExecutionGraphContractError(
            f"DECISION_TABLE_NOT_TOTAL: rows {rows} differ from the closed rows {expected}."
        )
    check_routes(contract)
    check_write_unit_batching(contract)
    check_bake_tasks(contract)
    check_bake_v3(contract)
    check_athena_start_budget(contract)
    pinned = PINNED_BOUNDS.get(str(contract.get("protocol_version")))
    if pinned is None or bounds_digest(contract["bounds"]) != pinned:
        raise ExecutionGraphContractError(
            "PROTOCOL_MIGRATION_UNSUPPORTED: the bounds differ from the pinned bounds of "
            f"{contract.get('protocol_version')}; a changed bound is a new protocol version."
        )
    return contract


def check_routes(contract: dict[str, Any]) -> None:
    """Refuse a route without a positive `max_receive_count`, or a node naming an unknown route.

    Revision 2 (design §2.1): each graph queue's `maxReceiveCount` and every claimer's
    final-receive test read `routes`, so a node may only name a route the block declares.
    """
    routes = cast("dict[str, Any]", contract.get("routes") or {})
    if not routes:
        raise ExecutionGraphContractError("ROUTE_UNKNOWN: the contract declares no routes.")
    for name, route in routes.items():
        count = cast("dict[str, object]", route or {}).get("max_receive_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise ExecutionGraphContractError(
                f"ROUTE_UNKNOWN: route {name!r} has no positive max_receive_count."
            )
    for template_name, template in cast("dict[str, Any]", contract["templates"]).items():
        for node in cast("list[dict[str, object]]", template["nodes"]):
            route = node.get("route")
            if route is not None and route not in routes:
                raise ExecutionGraphContractError(
                    f"ROUTE_UNKNOWN: {template_name} node {node.get('node')!r} names "
                    f"undeclared route {route!r}."
                )


def check_write_unit_batching(contract: dict[str, Any]) -> None:
    """Require the `write_unit_batching` block: `statement_allowance_ms` null or a positive int.

    Design §2.5 step 4 (D40): the key must be present; null is accepted so the slice builds, and
    the deploy gate (`require_deployable`) refuses it. A bool, zero, a negative, a string or a
    missing key is refused with `WRITE_UNIT_BATCHING_INVALID`.
    """
    block = contract.get("write_unit_batching")
    if not isinstance(block, dict) or "statement_allowance_ms" not in block:
        raise ExecutionGraphContractError(
            f"{BATCHING_INVALID}: write_unit_batching.statement_allowance_ms is required.",
            code=BATCHING_INVALID,
        )
    batching = cast("dict[str, object]", block)
    if set(batching) != {"statement_allowance_ms"}:
        raise ExecutionGraphContractError(
            f"{BATCHING_INVALID}: write_unit_batching carries only statement_allowance_ms.",
            code=BATCHING_INVALID,
        )
    value = batching["statement_allowance_ms"]
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ExecutionGraphContractError(
            f"{BATCHING_INVALID}: statement_allowance_ms must be null or a positive integer.",
            code=BATCHING_INVALID,
        )


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _one_decimal(value: object) -> bool:
    """Return whether a value is positive with at most one decimal place (tenths sum)."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return False
    exponent = Decimal(str(value)).as_tuple().exponent
    return isinstance(exponent, int) and exponent >= -1


def check_bake_tasks(contract: dict[str, Any]) -> None:
    """Refuse a bake roster that is not one-to-one with its budgets and write seconds (A1.1).

    Each refusal names its reason: `BAKE_ROSTER_COVERAGE` (a row without exactly one budget or
    its page without one write-seconds entry, or the reverse), `BAKE_ROSTER_OVER_MAX_TASKS`,
    `BAKE_MAX_TASKS_OVER_TRANSACTION_BOUND` (the final-transaction terms over `max_tasks` exceed
    `max_transaction_items`), the certified-count refusals of `check_certified_plan_counts`,
    `BAKE_VALUE_PRECISION` (more than one decimal place), `BAKE_PAGE_ID_COLLISION` (two pages
    whose normalized ids, `-` read as `_`, are equal) and `BAKE_SOURCE_HORIZON_DAYS_INVALID` (a
    roster row without exactly one integer `source_horizon_days` >= 1, or an entry without a
    row; plan A5.1 I9). `BAKE_TASKS_INVALID` covers the shape.
    """
    block = cast("dict[str, Any]", contract.get("bake_tasks") or {})
    roster = cast("list[dict[str, object]]", block.get("roster") or [])
    budgets = cast("dict[str, object]", block.get("budget_seconds") or {})
    writes = cast("dict[str, object]", block.get("write_seconds") or {})
    counts = ("per_query_seconds", "tail_seconds", "certified_query_cap", "control_claim_seconds")
    counts += ("lambda_timeout_seconds", "visibility_seconds", "lanes", "max_tasks")
    counts += ("worker_schedule_seconds", "worker_pass_seconds", "precompute_claim_lease_seconds")
    counts += ("final_authority_members", "final_task_condition_members")
    counts += ("final_written_pointer_members", "baker_memory_mib", "baker_write_width")
    groups = block.get("lane_groups")
    serving = cast("dict[str, object]", contract.get("serving_coalescing") or {})
    guard = serving.get("next_election_min_remaining_ms")
    promoter_timeout_ms = int(block.get("worker_pass_seconds") or 0) * 1000
    if (
        not roster
        or any(not _is_count(block.get(name)) for name in counts)
        or not isinstance(guard, int)
        or isinstance(guard, bool)
        or guard <= 0
        or guard >= promoter_timeout_ms
        or not isinstance(groups, list)
        or len(cast("list[object]", groups)) != block["lanes"]
    ):
        raise ExecutionGraphContractError(
            "BAKE_TASKS_INVALID: bake_tasks needs a roster and positive integer controls.",
            code="BAKE_TASKS_INVALID",
        )
    keys = [f"{row.get('page')}#{row.get('variant')}" for row in roster]
    pages = sorted({str(row.get("page")) for row in roster})
    if len(set(keys)) != len(keys) or set(keys) != set(budgets) or set(pages) != set(writes):
        raise ExecutionGraphContractError(
            "BAKE_ROSTER_COVERAGE: every roster row needs exactly one budget and its page one "
            "write_seconds entry, and every budget and write_seconds entry needs a roster row.",
            code="BAKE_ROSTER_COVERAGE",
        )
    limit = int(block["max_tasks"])
    if len(roster) > limit:
        raise ExecutionGraphContractError(
            f"BAKE_ROSTER_OVER_MAX_TASKS: {len(roster)} roster rows exceed max_tasks {limit}.",
            code="BAKE_ROSTER_OVER_MAX_TASKS",
        )
    authority = int(block["final_authority_members"])
    per_task = int(block["final_task_condition_members"]) + int(
        block["final_written_pointer_members"]
    )
    members = authority + per_task * limit
    if members > int(contract["bounds"]["max_transaction_items"]):
        raise ExecutionGraphContractError(
            f"BAKE_MAX_TASKS_OVER_TRANSACTION_BOUND: {authority} + {per_task} x {limit} = "
            f"{members} exceeds bounds.max_transaction_items.",
            code="BAKE_MAX_TASKS_OVER_TRANSACTION_BOUND",
        )
    if any(not _one_decimal(value) for value in (*budgets.values(), *writes.values())):
        raise ExecutionGraphContractError(
            "BAKE_VALUE_PRECISION: budgets and write_seconds are positive with at most one "
            "decimal place.",
            code="BAKE_VALUE_PRECISION",
        )
    horizons = block.get("source_horizon_days")
    if (
        not isinstance(horizons, dict)
        or set(cast("dict[str, object]", horizons)) != set(keys)
        or any(not _is_count(value) for value in cast("dict[str, object]", horizons).values())
    ):
        raise ExecutionGraphContractError(
            "BAKE_SOURCE_HORIZON_DAYS_INVALID: every roster row needs exactly one "
            "source_horizon_days entry, an integer number of days >= 1, and every entry a row.",
            code="BAKE_SOURCE_HORIZON_DAYS_INVALID",
        )
    check_certified_plan_counts(contract, keys)
    normalized = [page.replace("-", "_") for page in pages]
    if len(set(normalized)) != len(normalized):
        raise ExecutionGraphContractError(
            "BAKE_PAGE_ID_COLLISION: two roster pages share a normalized id.",
            code="BAKE_PAGE_ID_COLLISION",
        )
    check_bake_roster_identifiers(contract, roster)


def check_certified_plan_counts(contract: dict[str, Any], keys: list[str]) -> None:
    """Refuse an uncertified, over-budget or over-root roster (plan A5.3, I5).

    `BAKE_CERTIFIED_PLAN_COUNTS_INVALID`: the key set is not the roster's exactly, or a value is
    not an integer >= 1. `BAKE_CERTIFIED_PLAN_COUNT_OVER_BUDGET`: a row's
    `ceil(count / certified_query_cap) x per_query_seconds + tail_seconds + write_seconds` is
    over its budget. `bake_winner_over_root_bound`: the summed budgets plus two control claims
    exceed `bounds.protocol_ceiling_seconds`, one winner's root bound.
    """
    block = cast("dict[str, Any]", contract["bake_tasks"])
    certified = block.get("certified_plan_counts")
    if (
        not isinstance(certified, dict)
        or set(cast("dict[str, object]", certified)) != set(keys)
        or any(not _is_count(value) for value in cast("dict[str, object]", certified).values())
    ):
        raise ExecutionGraphContractError(
            "BAKE_CERTIFIED_PLAN_COUNTS_INVALID: every roster row needs exactly one "
            "certified_plan_counts entry, an integer >= 1, and every entry a row.",
            code="BAKE_CERTIFIED_PLAN_COUNTS_INVALID",
        )
    counts = cast("dict[str, int]", certified)
    budgets = cast("dict[str, float]", block["budget_seconds"])
    writes = cast("dict[str, float]", block["write_seconds"])
    cap, per_query = int(block["certified_query_cap"]), int(block["per_query_seconds"])
    for key in keys:
        waves = -(-counts[key] // cap)
        tenths = (waves * per_query + int(block["tail_seconds"])) * 10 + round(
            float(writes[key.split("#", 1)[0]]) * 10
        )
        if tenths > round(float(budgets[key]) * 10):
            raise ExecutionGraphContractError(
                f"BAKE_CERTIFIED_PLAN_COUNT_OVER_BUDGET: {key}'s {counts[key]} certified plans "
                f"need {tenths / 10} s, over its {budgets[key]} s budget.",
                code="BAKE_CERTIFIED_PLAN_COUNT_OVER_BUDGET",
            )
    root = sum(float(budgets[key]) for key in keys) + 2 * int(block["control_claim_seconds"])
    ceiling = int(contract["bounds"]["protocol_ceiling_seconds"])
    if root > ceiling:
        raise ExecutionGraphContractError(
            f"bake_winner_over_root_bound: the roster's budgets need {root} s, over the "
            f"{ceiling} s root bound.",
            code="bake_winner_over_root_bound",
        )


_ROSTER_PAGE: Final = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_ROSTER_VARIANT: Final = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_MESSAGE_GROUP_ID_MAX: Final = 128


def check_bake_roster_identifiers(
    contract: dict[str, Any], roster: list[dict[str, object]]
) -> None:
    """Refuse the split roster grammar at generation, not at Lambda init (A5.2, O1).

    Pages stay hyphen-only and variants take the domain variant grammar, each at most
    `bake_v3.identifier_max_length` (`BAKE_ROSTER_IDENTIFIER_INVALID`); a row's legacy intent
    group id `<page>#<variant>` fits SQS's 128-character `MessageGroupId`
    (`BAKE_INTENT_GROUP_ID_TOO_LONG`).
    """
    bake_v3 = cast("dict[str, object]", contract.get("bake_v3") or {})
    limit = cast("int", bake_v3.get("identifier_max_length", 80))
    for row in roster:
        page, variant = row.get("page"), row.get("variant")
        if (
            not isinstance(page, str)
            or not isinstance(variant, str)
            or not _ROSTER_PAGE.fullmatch(page)
            or not _ROSTER_VARIANT.fullmatch(variant)
            or len(page) > limit
            or len(variant) > limit
        ):
            raise ExecutionGraphContractError(
                f"BAKE_ROSTER_IDENTIFIER_INVALID: roster row {page}#{variant} is not a "
                "hyphen-only page and a lowercase variant identifier.",
                code="BAKE_ROSTER_IDENTIFIER_INVALID",
            )
        if len(page) + 1 + len(variant) > _MESSAGE_GROUP_ID_MAX:
            raise ExecutionGraphContractError(
                f"BAKE_INTENT_GROUP_ID_TOO_LONG: {page}#{variant} exceeds the 128-character "
                "SQS message group id.",
                code="BAKE_INTENT_GROUP_ID_TOO_LONG",
            )


_BAKE_V3_LISTS: Final = {
    "control_kinds": ("bake_fan_out", "bake_finalize"),
    "effect_kinds": ("written", "unchanged", "unavailable"),
    "recovery_reasons": ("fatal", "retry_exhausted"),
}


def check_bake_v3(contract: dict[str, Any]) -> None:
    """Require the closed V3 vocabulary exactly (plan 4.1); `BAKE_V3_INVALID` names any drift."""
    block = cast("dict[str, object]", contract.get("bake_v3") or {})
    names = {
        "schema_version",
        "task_message_kind",
        "control_kinds",
        "waiting_input_state",
        "effect_kinds",
        "unchanged_reason",
        "recovery_reasons",
        "unavailable_reason_suffix",
        "identifier_max_length",
    }
    lists_exact = all(
        tuple(cast("list[object]", block.get(name) or [])) == expected
        for name, expected in _BAKE_V3_LISTS.items()
    )
    suffix = block.get("unavailable_reason_suffix")
    if (
        set(block) != names
        or block.get("schema_version") != 3  # noqa: PLR2004 -- the V3 schema
        or not lists_exact
        or not _is_count(block.get("identifier_max_length"))
        or not isinstance(suffix, str)
        or not suffix.startswith("_")
    ):
        raise ExecutionGraphContractError(
            "BAKE_V3_INVALID: bake_v3 carries exactly the closed V3 task, effect and recovery "
            "vocabulary.",
            code="BAKE_V3_INVALID",
        )


def bake_unavailable_reasons(contract: dict[str, Any]) -> dict[str, str]:
    """Generate each roster page's one terminal unavailable reason (plan 4.1, F12, A1.2).

    `<page id with '-' read as '_'><suffix>`, in roster page order; `check_bake_tasks` already
    refuses two pages whose normalized ids collide.
    """
    suffix = str(contract["bake_v3"]["unavailable_reason_suffix"])
    pages: dict[str, str] = {}
    for row in cast("list[dict[str, object]]", contract["bake_tasks"]["roster"]):
        page = str(row["page"])
        pages.setdefault(page, f"{page.replace('-', '_')}{suffix}")
    return pages


def check_athena_start_budget(contract: dict[str, Any]) -> None:
    """Require the three positive integer start-budget limits, the only home of each (S1, F4)."""
    block = cast("dict[str, object]", contract.get("athena_start_budget") or {})
    names = {"active_dml_quota", "batch_athena_budget", "chat_reserve_active_dml"}
    if set(block) != names or any(not _is_count(block[name]) for name in names):
        raise ExecutionGraphContractError(
            "ATHENA_START_BUDGET_INVALID: athena_start_budget carries exactly active_dml_quota, "
            "batch_athena_budget and chat_reserve_active_dml, each a positive integer.",
            code="ATHENA_START_BUDGET_INVALID",
        )


def require_deployable(contract: dict[str, Any]) -> None:
    """Refuse a contract whose allowance is still null (D40), the deploy gate's one rule."""
    batching = cast("dict[str, object]", contract["write_unit_batching"])
    if batching["statement_allowance_ms"] is None:
        raise ExecutionGraphContractError(
            f"{PENDING_PROBE}: write_unit_batching.statement_allowance_ms is null; derive it "
            "(design §2.5 step 4) before any deploy.",
            code=PENDING_PROBE,
        )


def node_routes(contract: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Return each template's routed nodes and the route each one is dispatched to."""
    return {
        name: {
            str(node["node"]): str(node["route"])
            for node in cast("list[dict[str, object]]", template["nodes"])
            if "route" in node
        }
        for name, template in cast("dict[str, Any]", contract["templates"]).items()
    }


def t_max_seconds(contract: dict[str, Any]) -> int:
    """Return T_max: the largest configured timeout among the inventoried legacy writers (V-17b).

    A writer without a positive integer timeout cannot be bounded, so `T_max` cannot be
    generated; that is the S6 stop rule, raised here before any projection is written.
    """
    cutover = cast("dict[str, Any]", contract.get("cutover") or {})
    writers = cast("list[dict[str, object]]", cutover.get("legacy_writers") or [])
    timeouts: list[int] = []
    for writer in writers:
        value = writer.get("timeout_seconds")
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ExecutionGraphContractError(
                f"T_MAX_NOT_GENERATED: legacy writer {writer.get('writer')!r} has no "
                "configured positive timeout."
            )
        timeouts.append(value)
    if not timeouts:
        raise ExecutionGraphContractError("T_MAX_NOT_GENERATED: no legacy writer is inventoried.")
    return max(timeouts)


def _frozen(value: object) -> object:
    """Render contract lists as tuples, so every projected value has a known element type."""
    if isinstance(value, list):
        return tuple(_frozen(item) for item in cast("list[object]", value))
    if isinstance(value, dict):
        return {key: _frozen(item) for key, item in cast("dict[str, object]", value).items()}
    return value


def _python_value(value: object) -> str:
    return pprint.pformat(_frozen(value), width=92, sort_dicts=False)


def render_python(contract: dict[str, Any], digest: str) -> str:
    """Render the deterministic pure-Python projection."""
    constants: list[tuple[str, object]] = [
        ("CONTRACT_REVISION", contract["contract_revision"]),
        ("CONTRACT_DIGEST", digest),
        ("PROTOCOL_VERSION", contract["protocol_version"]),
        ("AUTHORITY_EPOCHS", contract["authority_epochs"]),
        ("MODES", tuple(contract["modes"])),
        ("DEFAULT_MODE", contract["default_mode"]),
        ("MODE_TRANSITIONS", tuple(contract["mode_transitions"])),
        ("TEMPLATES", contract["templates"]),
        ("ROUTES", contract["routes"]),
        ("WRITE_UNIT_BATCHING", contract["write_unit_batching"]),
        ("SERVING_COALESCING", contract["serving_coalescing"]),
        ("BAKE_TASKS", contract["bake_tasks"]),
        ("BAKE_V3", contract["bake_v3"]),
        ("BAKE_UNAVAILABLE_REASONS", bake_unavailable_reasons(contract)),
        ("ATHENA_START_BUDGET", contract["athena_start_budget"]),
        ("CALLER_REASONS", {k: tuple(v) for k, v in contract["caller_kinds"].items()}),
        ("API_ONLY_REASONS", tuple(contract["api_only_reasons"])),
        ("REQUESTER_GROUP", contract["requester_group"]),
        ("API_ROUTES", tuple(contract["api_routes"])),
        ("ADMISSION_CLASSES", tuple(contract["admission_classes"])),
        ("CALLER_LABELS_REFUSED", tuple(contract["caller_labels_refused"])),
        ("RUN_REQUEST_SCHEMA", contract["run_request_schema"]),
        ("REFUSAL_CLASSES", {k: tuple(v) for k, v in contract["refusal_classes"].items()}),
        ("RETRY_ROW_APPROVED", bool(contract["retry_row_approved"])),
        ("DECISION_TABLE", tuple(contract["decision_table"])),
        ("BOUNDS", contract["bounds"]),
        ("OPERATOR_COMMANDS", contract["operator_commands"]),
        ("MANAGED_JOB_EXECUTORS", tuple(contract["managed_job_executors"])),
        ("TIMING_MANIFEST", contract["timing_manifest"]),
        ("ROOT_VOCABULARY", contract["root_vocabulary"]),
        ("HISTORICAL_POLICY", contract["historical_policy"]),
        ("STORE", contract["store"]),
        ("CUTOVER", contract["cutover"]),
        ("T_MAX_SECONDS", t_max_seconds(contract)),
    ]
    lines = [
        "# SPDX-License-Identifier: MIT-0",
        '"""Generated by `make generate-execution-graph-protocol`; do not edit by hand."""',
        "",
        "from typing import Any, Final",
        "",
    ]
    for name, value in constants:
        if isinstance(value, str | int | bool):
            lines.append(f"{name}: Final = {value!r}")
        else:
            lines.append(f"{name}: Final[Any] = (\n{_python_value(value)}\n)")
    return "\n".join(lines) + "\n"


def render_typescript(contract: dict[str, Any], digest: str) -> str:
    """Render the infrastructure projection: store keys, table suffixes, API and graph routes."""
    store = contract["store"]
    values: dict[str, object] = {
        "CONTRACT_DIGEST": digest,
        "PROTOCOL_VERSION": contract["protocol_version"],
        "LIVE_TABLE_SUFFIX": store["live_table_suffix"],
        "PROBE_TABLE_SUFFIXES": store["probe_table_suffixes"],
        "PARTITION_KEY": store["partition_key"],
        "SORT_KEY": store["sort_key"],
        "MODE_KEY": store["mode_key"],
        "REQUESTER_GROUP": contract["requester_group"],
        "API_ROUTES": contract["api_routes"],
        "WRITER_ACTIONS": store["writer_actions"],
        "OPERATOR_ONLY_PREFIXES": store["operator_only_prefixes"],
        "OPERATOR_ONLY_DENIED_ACTIONS": store["operator_only_denied_actions"],
        "PROBE_ROLE_ACTIONS": store["probe_role_actions"],
        "DRAIN_SEQ_KEY": contract["cutover"]["drain_seq_key"],
        "LEGACY_WRITERS": contract["cutover"]["legacy_writers"],
        "T_MAX_SECONDS": t_max_seconds(contract),
        "ROUTES": contract["routes"],
        "NODE_ROUTES": node_routes(contract),
        "BAKE_TASKS": contract["bake_tasks"],
        "ATHENA_START_BUDGET": contract["athena_start_budget"],
    }
    lines = [
        "// SPDX-License-Identifier: MIT-0",
        "// Generated by `make generate-execution-graph-protocol`; do not edit by hand.",
    ]
    for name, value in values.items():
        lines.append(f"export const {name} = {json.dumps(value, sort_keys=True)} as const;")
    return "\n".join(lines) + "\n"


def _planner_findings(root: Path, contract: dict[str, Any]) -> list[str]:
    """Check that the pinned replay band equals the planner's own (land_fixtures.bundle)."""
    from adapters.jobs.land_fixtures.bundle import plan_with_backfill  # noqa: PLC0415

    _, policy = plan_with_backfill(root)
    pinned = contract["historical_policy"]
    computed = {
        "replay_base": policy.replay_base.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "first_day": policy.first_day.isoformat(),
        "cut": policy.cut.isoformat(),
        "stride_seconds": policy.stride_seconds,
        "seal_slot_base": policy.seal_slot_base,
        "backfill_slot_base": policy.backfill_slot_base,
        "probe_slot_base": policy.probe_slot_base,
        "probe_slot_count": policy.probe_slot_count,
        "encodings": sorted(
            [
                {"source_system": key[0], "source_dataset": key[1], "encoding": value.value}
                for key, value in policy.encodings.items()
            ],
            key=lambda item: (item["source_system"], item["source_dataset"]),
        ),
    }
    pinned_view = {
        **pinned,
        "encodings": sorted(
            pinned["encodings"], key=lambda item: (item["source_system"], item["source_dataset"])
        ),
    }
    if pinned_view != computed:
        return ["historical_policy differs from the planner's computed replay band"]
    return []


def check_total(contract: dict[str, Any]) -> None:
    """Run the domain totality proof over the contract's rows (V-6)."""
    from domain.operations.run_request import check_decision_table  # noqa: PLC0415

    check_decision_table(
        tuple(contract["decision_table"]), retry_approved=bool(contract["retry_row_approved"])
    )


def main(argv: list[str] | None = None, *, root_override: Path | None = None) -> int:
    """Write both projections, or check that they are current and the table is total.

    `--check` and `--deployable` combine in any order; `--deployable` alone also refuses a null
    allowance before writing (D40). `root_override` lets a test point at a contract copy.
    """
    arguments = sys.argv[1:] if argv is None else argv
    if any(argument not in _ARGUMENTS for argument in arguments):
        sys.stderr.write(
            "usage: python -m adapters.jobs.data_flow_registry.execution_graphs "
            "[--check] [--deployable]\n"
        )
        return 2
    root = Path.cwd() if root_override is None else root_override
    try:
        contract = load_contract(root)
        check_total(contract)
        if _DEPLOYABLE in arguments:
            require_deployable(contract)
    except (ExecutionGraphContractError, ValueError) as error:
        sys.stderr.write(f"EXECUTION GRAPH CONTRACT ERROR: {error}\n")
        return 1
    digest = hashlib.sha256((root / CONTRACT_PATH).read_bytes()).hexdigest()
    rendered = {
        PYTHON_PATH: render_python(contract, digest),
        TYPESCRIPT_PATH: render_typescript(contract, digest),
    }
    if _CHECK in arguments:
        findings = [
            f"{path} is missing or stale; run make generate-execution-graph-protocol"
            for path, text in rendered.items()
            if not (root / path).is_file() or (root / path).read_text() != text
        ]
        findings.extend(_planner_findings(root, contract))
        if findings:
            sys.stderr.write("EXECUTION GRAPH PROTOCOL CHECK FAILED\n")
            for finding in findings:
                sys.stderr.write(f"  {finding}\n")
            return 1
        sys.stdout.write("EXECUTION GRAPH PROTOCOL CHECK PASSED: decision table total.\n")
        return 0
    for path, text in rendered.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
        sys.stdout.write(f"wrote {path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
