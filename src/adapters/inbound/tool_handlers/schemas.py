# SPDX-License-Identifier: MIT-0
"""What each tool accepts on the wire, declared once so nothing has to infer it.

**This did not exist before 2026-08-22, and its absence was invisible.** Every argument a tool takes
was implied by what `_request_from` happened to dereference — `arguments.get("metric_name")` and so
on — so the contract was real, enforced, and written down nowhere. Registering an AgentCore Gateway
target is what forced the issue, but the target is not the reason this matters.

`inputSchema` is **what the model reads to decide what to send**, and Gateway validates against it
before the function is invoked at all. Undeclared means a model inferring argument names from a
prose description, which is the same class of guessing this platform refuses everywhere else: a
metric is declared in the dictionary rather than assumed, and a fact's columns come from a contract
rather than from what a query happens to reference.

**Identity, the clock and the correlation id are deliberately absent as model-authored arguments,
but their custody is not the same.** `computed_at` is minted by the adapter clock, while
`correlation_id` comes from Gateway MCP/request metadata. No end-user identity, conversation
session, page-context assertion, or current-turn assertion accompanies the Lambda target call.
End-user identity and page context therefore remain forbidden schema fields rather than values
recovered from a side channel. The tool adapters construct deployment-wide analytical scope from
server-owned deployment configuration.
docs/capabilities/conversational-runtime/architecture.md §5 owns the full boundary; ADR-049
owns nonce assurance.

The generated artefact is `contracts/generated/tools.json`, written by
`python -m adapters.jobs.tool_schema_generation` and read by `AgentStack`. Same shape as
`tables.json`: one declaration, one generated file, one staleness gate. What a *gate* cannot check
is that the declaration matches what the handler dereferences, so a test builds an event from each
declaration and drives the real handler with it.
"""

from __future__ import annotations

import dataclasses
from enum import StrEnum
from typing import Any

__all__ = [
    "EXTERNAL_CLIENT_FIGURE_RULE",
    "TOOL_CONTRACTS",
    "Argument",
    "ArgumentType",
    "ToolContract",
    "as_tool_definitions",
    "contract_for",
]


SERVER_CLOCK_AND_CORRELATION_FIELDS: tuple[str, ...] = ("correlation_id", "computed_at", "today")
"""Argument names the server owns per call; a model-authored copy would move the clock or relabel
the call in every log."""

FORBIDDEN_IDENTITY_FIELDS: tuple[str, ...] = (
    "user_id",
    "identity",
    "actor",
    "actor_id",
    "actorId",
    "tenant",
    "brand",
    "session",
    "session_id",
    "sessionId",
    "page_context",
    "pageContext",
    "page_context_reference",
    "pageContextReference",
    "owner",
    "owner_id",
    "ownerId",
    "verified_owner_id",
    "verifiedOwnerId",
)
"""Identity, session, and page-context names no tool may declare or accept.

If identity were an argument the model fills in, the model could impersonate a user or pick a
session, and the call would still look ordinary in every log.
"""

FORBIDDEN_MODEL_AUTHORED_FIELDS: tuple[str, ...] = (
    *SERVER_CLOCK_AND_CORRELATION_FIELDS,
    *FORBIDDEN_IDENTITY_FIELDS,
)


class ArgumentType(StrEnum):
    """The JSON types a tool argument may take.

    Spelled to match AgentCore's own `SchemaDefinitionType` values rather than JSON Schema's, so the
    generated file needs no translation on the way into CDK.
    """

    STRING = "string"
    NUMBER = "number"
    OBJECT = "object"
    ARRAY = "array"
    BOOLEAN = "boolean"
    INTEGER = "integer"


@dataclasses.dataclass(frozen=True, slots=True)
class Argument:
    """One argument a tool accepts.

    `description` is not decoration. The model reads it, and it is the only place a constraint the
    type system cannot express — an ISO date rather than any string, a dimension name rather than a
    column name — can reach the caller before it guesses wrong.
    """

    name: str
    type: ArgumentType
    description: str
    required: bool = True
    items: ArgumentType | None = None
    """Element type, for an array. Required by the schema when `type` is `ARRAY`."""

    def __post_init__(self) -> None:
        if self.type is ArgumentType.ARRAY and self.items is None:
            raise ValueError(f"{self.name} is an array and must declare its element type.")
        if self.type is not ArgumentType.ARRAY and self.items is not None:
            raise ValueError(f"{self.name} declares an element type but is not an array.")
        if not self.description.strip():
            raise ValueError(f"{self.name} needs a description; the model reads it.")


@dataclasses.dataclass(frozen=True, slots=True)
class ToolContract:
    """One tool, as a caller sees it.

    `name` is the tool's own name, unprefixed. Gateway prepends the target name and three
    underscores on the way in, which `gateway.strip_target_prefix` removes — so what is declared
    here is what the handler's enum matches, not what arrives on the wire.
    """

    name: str
    description: str
    arguments: tuple[Argument, ...] = ()

    def __post_init__(self) -> None:
        names = [argument.name for argument in self.arguments]
        if len(names) != len(set(names)):
            raise ValueError(f"{self.name} declares an argument twice.")
        for forbidden in FORBIDDEN_MODEL_AUTHORED_FIELDS:
            if forbidden in names:
                raise ValueError(
                    f"{self.name} declares {forbidden}, which a model-authored schema must "
                    "never supply. `computed_at` is minted by the adapter clock; "
                    "`correlation_id` comes from Gateway MCP/request metadata; end-user identity "
                    "and page-context assertions are absent from the tool target."
                )

    @property
    def required(self) -> tuple[str, ...]:
        """The arguments a caller must send, in declaration order."""
        return tuple(argument.name for argument in self.arguments if argument.required)


_CATALOG_METRIC_NAME = Argument(
    name="metric_name",
    type=ArgumentType.STRING,
    description=(
        "A customer-facing metric reference: canonical name, label, or governed alias, for "
        "computable and declared-but-not-yet-computable metrics alike, resolved "
        "deterministically. An unresolvable or ambiguous reference returns a typed refusal "
        "naming alternatives rather than an approximation."
    ),
)

_DISCOVERY_SOURCE_REFERENCE = Argument(
    name="source_reference",
    type=ArgumentType.STRING,
    description=(
        "Optional customer-facing source or storage context stated in a broad metric-discovery "
        "question. Pass it unchanged when present; the governed catalog either resolves the "
        "request or returns a natural limitation without substituting another source."
    ),
    required=False,
)

_METRIC_REFERENCE = Argument(
    name="metric_reference",
    type=ArgumentType.STRING,
    description=(
        "Customer-facing metric name, label, or governed alias to resolve deterministically. "
        "This is candidate intent, not an execution field; unknown or ambiguous text is refused. "
        "Customers and buyers are distinct people; purchases and orders are transactions, and "
        "one is never answered with the other. New-to-brand customers or buyers: "
        "buyers_new_to_brand_12mo, measured per complete week or month (time_grain week or "
        "month). New-to-brand purchases or orders: purchases_new_to_brand_12mo."
    ),
)

_GRAINS = Argument(
    name="grains",
    type=ArgumentType.ARRAY,
    items=ArgumentType.STRING,
    description=(
        "One or more customer-facing dimensions to break the figure down by, as semantic grain "
        "names, never storage columns. Retail and advertising: ad_product, campaign, channel, "
        "channel_group, date, ecosystem, marketplace, platform, product. Reach and funnel "
        "(shoppers_reached and the other exposure-group metrics): exposure_group and "
        "identity_space; frequency metrics: frequency_bucket. Audience segments "
        "(segment_impressions, segment_index and the other segment metrics): audience_segment, "
        "with campaign for one campaign's segments. Cohorts and repeat rates "
        "(repeat_buyer_rate and the other cohort metrics): cohort, acquisition_channel_group "
        "and months_since_acquisition. catalog_describe_metric lists the grains one metric "
        "supports."
    ),
)

_OPERATION = Argument(
    name="operation",
    type=ArgumentType.STRING,
    description=(
        "Optional customer-facing operation candidate. Preserve explicit operation wording even "
        "when it may be unsupported; deterministic resolution returns the typed refusal. When "
        "omitted, the selected tool uses its own breakdown, comparison, or ranking default."
    ),
    required=False,
)

_PERIOD_REFERENCE = Argument(
    name="period_reference",
    type=ArgumentType.STRING,
    description=(
        "Optional exact governed period name, such as 'the 7 days ending yesterday'. Supply "
        "this or both candidate period dates, never both modes. When the question names no "
        "period at all, omit every period argument: the platform answers for the latest "
        "published coverage and the answer discloses the window it chose."
    ),
    required=False,
)

_PERIOD_START_CANDIDATE = Argument(
    name="period_start_candidate",
    type=ArgumentType.STRING,
    description=(
        "Optional candidate first day, inclusive, as YYYY-MM-DD. It becomes authoritative only "
        "after deterministic closed-period resolution and requires period_end_candidate."
    ),
    required=False,
)

_PERIOD_END_CANDIDATE = Argument(
    name="period_end_candidate",
    type=ArgumentType.STRING,
    description=(
        "Optional candidate last day, inclusive, as YYYY-MM-DD. It requires "
        "period_start_candidate and cannot include the open current day."
    ),
    required=False,
)

_PRIOR_PERIOD_REFERENCE = Argument(
    name="prior_period_reference",
    type=ArgumentType.STRING,
    description=(
        "Optional exact governed name for the earlier period. Supply this or both prior candidate "
        "dates, never both modes. Required for comparison and increase ranking."
    ),
    required=False,
)

_PRIOR_PERIOD_START_CANDIDATE = Argument(
    name="prior_period_start_candidate",
    type=ArgumentType.STRING,
    description=(
        "Optional candidate first day of the earlier period, inclusive, as YYYY-MM-DD. It "
        "requires prior_period_end_candidate."
    ),
    required=False,
)

_PRIOR_PERIOD_END_CANDIDATE = Argument(
    name="prior_period_end_candidate",
    type=ArgumentType.STRING,
    description=(
        "Optional candidate last day of the earlier period, inclusive, as YYYY-MM-DD. The "
        "resolved earlier period must end before the later period begins."
    ),
    required=False,
)

_ENTITY_REFERENCES = Argument(
    name="entity_references",
    type=ArgumentType.OBJECT,
    description=(
        "Optional map from a governed entity kind to customer-visible name text. Known kinds are "
        "ecosystem, platform, channel_group, channel, campaign, ad_product, marketplace, product, "
        "brand, and advertiser. A marketplace phrase such as 'United States marketplace' belongs "
        "under the marketplace key here as the customer-visible name only, for example 'United "
        "States'; omit the generic word 'marketplace' and never put the name under selectors. "
        "Pass every entity kind the customer names; deterministic resolution accepts what is "
        "supported and returns a typed refusal for what is not. Values must be names only; "
        "identifiers, keys, source scopes, and nested reference objects are refused. Audience "
        "segments are not an entity kind: they can't be named here, only broken down by with "
        "the audience_segment grain, and the answer names each segment."
    ),
    required=False,
)

_REPORT_LENSES = Argument(
    name="report_lenses",
    type=ArgumentType.OBJECT,
    description=(
        "Optional map of customer-requested report-lens names to values. Every name and value is "
        "resolved through the governed vocabulary; omitted applicable lenses use governed defaults."
    ),
    required=False,
)

_SELECTORS = Argument(
    name="selectors",
    type=ArgumentType.OBJECT,
    description=(
        "Optional map of exact semantic selector names to governed values. Supply only selectors "
        "required by the chosen metric (catalog_describe_metric lists them); values are "
        "resolved exactly, and a selector the metric allows only one value for is pinned for "
        "you. time_grain is the kind of period the question asks about: week for a Monday to "
        "Sunday week, month for a calendar month (such as September, or last month), quarter "
        "for a calendar quarter, custom for a cohort metric's vintage. profile_path says whose "
        "segment profile is read: asi_in_market or asi_lifestyle for the shoppers your ads "
        "reached, profiled with Amazon audience segments (paid); dsp_exposed_only for "
        "DSP-exposed buyers, with DSP behaviour segments. The paths are never combined, so when "
        "the question doesn't say which, ask. cohort_path: standard for ad-touched repeat "
        "purchases, arp for all Amazon purchases. Marketplace, product, campaign, channel, and "
        "other entity names belong in entity_references, never in this map."
    ),
    required=False,
)

_SOURCE_SYSTEM_SELECTOR = Argument(
    name="source_system_selector",
    type=ArgumentType.STRING,
    description=(
        "Optional candidate source-system name paired with source_dataset_selector. Pass "
        "explicit source wording unchanged; deterministic resolution accepts or refuses it "
        "with a typed reason."
    ),
    required=False,
)

_SOURCE_DATASET_SELECTOR = Argument(
    name="source_dataset_selector",
    type=ArgumentType.STRING,
    description=(
        "Optional candidate source-dataset name paired with source_system_selector. Pass "
        "explicit source wording unchanged; deterministic resolution accepts or refuses it "
        "with a typed reason. No caller source scope or source identifier is accepted."
    ),
    required=False,
)

_CURRENCY_MODE = Argument(
    name="currency_mode",
    type=ArgumentType.STRING,
    description=(
        "Optional currency candidate: NATIVE or TARGET. NATIVE is the governed default. TARGET "
        "also requires target_currency; deterministic resolution accepts or refuses it with a "
        "typed reason."
    ),
    required=False,
)

_TARGET_CURRENCY = Argument(
    name="target_currency",
    type=ArgumentType.STRING,
    description=(
        "Optional three-letter target currency candidate, used only with currency_mode TARGET."
    ),
    required=False,
)

_MAX_ROWS = Argument(
    name="max_rows",
    type=ArgumentType.INTEGER,
    description=(
        "Optional. Ceiling on rows returned. The answer states when it was hit, so a "
        "truncated result is never mistaken for a complete one."
    ),
    required=False,
)

_SEMANTIC_QUALIFIERS = (
    _PERIOD_REFERENCE,
    _PERIOD_START_CANDIDATE,
    _PERIOD_END_CANDIDATE,
    _ENTITY_REFERENCES,
    _REPORT_LENSES,
    _SELECTORS,
    _SOURCE_SYSTEM_SELECTOR,
    _SOURCE_DATASET_SELECTOR,
    _CURRENCY_MODE,
    _TARGET_CURRENCY,
    _MAX_ROWS,
)

TOOL_CONTRACTS: tuple[ToolContract, ...] = (
    ToolContract(
        name="catalog_list_metrics",
        description=(
            "Report the authoritative inventory of metrics this platform declares, including "
            "declared-but-not-yet-computable metrics. Use it when the user asks what this "
            "platform can measure, report, or calculate; preserve any explicit source or "
            "storage context in source_reference. It is not needed to explain what a general "
            "advertising or retail concept means."
        ),
        arguments=(_DISCOVERY_SOURCE_REFERENCE,),
    ),
    ToolContract(
        name="catalog_describe_metric",
        description=(
            "Report how this platform defines one declared metric: its governed formula, "
            "units, valid breakdowns, and caveats. Use it for claims about this platform's "
            "definition or support of a metric, not to explain general industry terminology, "
            "which needs no tool. Answers from the metric dictionary and reads no data."
        ),
        arguments=(
            _CATALOG_METRIC_NAME,
            Argument(
                name="grain",
                type=ArgumentType.STRING,
                description=(
                    "Optional exact dimension name to check for support. For every explicit "
                    "'by X', breakdown, or grain request, pass X unchanged even when it may be "
                    "unsupported; never omit or substitute it to make the call succeed. Entity "
                    "selection is separate, so mixed entity and grain wording must preserve both."
                ),
                required=False,
            ),
        ),
    ),
    ToolContract(
        name="measurement_metric_by_grain",
        description=(
            "Resolve candidate semantic intent, then report one metric as a breakdown or trend "
            "over a closed period. Every candidate is resolved inside the server-configured, "
            "deployment-wide analytical scope before the existing measurement boundary executes."
        ),
        arguments=(
            _METRIC_REFERENCE,
            _GRAINS,
            _OPERATION,
            *_SEMANTIC_QUALIFIERS,
        ),
    ),
    ToolContract(
        name="measurement_compare_windows",
        description=(
            "Resolve candidate semantic intent, then compare one metric over two closed, "
            "non-overlapping periods inside the server-configured, deployment-wide analytical "
            "scope. Comparison is the default only when the customer names no operation. If they "
            "name reconciliation, causal analysis, or any other operation, copy that wording into "
            "operation so deterministic resolution can accept or refuse it without substitution."
        ),
        arguments=(
            _METRIC_REFERENCE,
            _GRAINS,
            _OPERATION,
            _PRIOR_PERIOD_REFERENCE,
            _PRIOR_PERIOD_START_CANDIDATE,
            _PRIOR_PERIOD_END_CANDIDATE,
            *_SEMANTIC_QUALIFIERS,
        ),
    ),
    ToolContract(
        name="measurement_top_n",
        description=(
            "Resolve candidate semantic intent, then rank one metric at a grain. VALUE ranks the "
            "later-period figure; INCREASE ranks growth and requires an earlier closed period. "
            "Ranking is the default only when the customer names no other operation; copy any "
            "explicit operation wording into operation for deterministic acceptance or refusal. "
            "Resolution and the server-configured, deployment-wide analytical scope are enforced "
            "before the existing ranking boundary executes."
        ),
        arguments=(
            _METRIC_REFERENCE,
            _GRAINS,
            _OPERATION,
            Argument(
                name="rank_size",
                type=ArgumentType.INTEGER,
                description="Number of ranked rows to return, at least 1.",
            ),
            Argument(
                name="rank_order",
                type=ArgumentType.STRING,
                description=(
                    "Exact ranking intent: VALUE for the largest later-period figures or INCREASE "
                    "for the largest increases against an earlier period."
                ),
            ),
            _PRIOR_PERIOD_REFERENCE,
            _PRIOR_PERIOD_START_CANDIDATE,
            _PRIOR_PERIOD_END_CANDIDATE,
            *_SEMANTIC_QUALIFIERS,
        ),
    ),
)
"""Every tool this platform exposes, in the order a caller meets them.

Catalog first on purpose: an agent that asks what exists before asking for a figure is the behaviour
the capability tools were built to make possible.

**The measurement three are shaped by operation rather than by metric** (ADR-032). Their
arguments are candidate semantic intent only. Comparison fixes comparison semantics, ranking fixes
top semantics plus an explicit rank order, and metric-by-grain permits only breakdown or trend.
None exposes the canonical values that the resolver and mapper produce for execution.
"""


EXTERNAL_CLIENT_FIGURE_RULE = (
    "These are finished governed figures. Quote each as returned, with its measurement basis "
    "(metadata.measurement_basis) and its date range (metadata.requested_coverage_intervals) "
    "where the result states one; if metadata.coverage_holes is non-empty, say the figures do not "
    "cover those periods. Never state a date range the result does not state. State an "
    "attribution window only where the result states one: in the figure's label "
    "(data.claim_fields[].label) or in a metadata.semantic_qualifiers entry whose field is "
    "attribution_window. Never assume or add a window the result does not state. Do not add, "
    "total, average, convert or recompute figures, and never sum attributed sales across ad "
    "networks; ask this tool for a governed figure instead."
)
"""What an external MCP client is told about the figures these tools return (ADR-150).

Generated into `contracts/generated/tools.json` as `external_client_rule`, never into the in-app
tool descriptions. The external MCP gateway appends it to each tool's description and states it as
the server instructions, because an external client's model narrates the answer itself and no
in-app figure verification runs on that path. It names only fields a `ShapedResponse` serializes.
"""


def contract_for(name: str) -> ToolContract:
    """Find one tool's contract, or raise naming what exists."""
    for contract in TOOL_CONTRACTS:
        if contract.name == name:
            return contract
    raise KeyError(f"{name} is not a declared tool. Declared: {[c.name for c in TOOL_CONTRACTS]}")


def as_tool_definitions() -> list[dict[str, Any]]:
    """Render every contract as AgentCore's `ToolDefinition` shape.

    A plain structure rather than a typed model because the consumer is CDK reading JSON — the same
    reason `tables.json` is JSON rather than DDL. Emitting anything the TypeScript side had to parse
    beyond JSON would be a parser nobody should write.

    Candidate maps render as bare objects because AgentCore's `SchemaDefinition` cannot declare
    dynamic map keys. The descriptions restrict those maps to name-only semantic text, and the
    inbound adapter validates their keys and values again before deterministic resolution.
    """
    return [_definition(contract) for contract in TOOL_CONTRACTS]


def _definition(contract: ToolContract) -> dict[str, Any]:
    """Render one contract, omitting empty keys rather than emitting nulls."""
    properties = {argument.name: _property(argument) for argument in contract.arguments}
    schema: dict[str, Any] = {"type": ArgumentType.OBJECT.value}
    if properties:
        schema["properties"] = properties
    if contract.required:
        schema["required"] = list(contract.required)
    return {
        "name": contract.name,
        "description": contract.description,
        "inputSchema": schema,
    }


def _property(argument: Argument) -> dict[str, Any]:
    """Render one argument. An array carries its element type; nothing else does."""
    rendered: dict[str, Any] = {
        "type": argument.type.value,
        "description": argument.description,
    }
    if argument.items is not None:
        rendered["items"] = {"type": argument.items.value}
    return rendered
