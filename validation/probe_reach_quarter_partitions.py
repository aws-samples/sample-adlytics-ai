# SPDX-License-Identifier: MIT-0
"""Measure how many publication partitions the Reach quarter resolves, read-only (dev only).

    uv run python validation/probe_reach_quarter_partitions.py --profile <profile> \
        --environment dev --region us-east-1 --expected-account <account> \
        --publications-table <semantic V2 publications table> --graph-table <execution graph \
        table> --run-id <the last dev bake run> --output <path>

The Reach quarter (`reach#last_complete_quarter`) is refused at the bake's pin with
`SELECTION_TOO_LARGE`, and the count that decides how to fix it was never recorded. This probe
composes the quarter's render exactly as the bake's pin pass does (the real Reach source, the
real `PublicationSelector` over the real CURRENT reader, the bake's horizon and its pinned
clock, a query port that captures and executes nothing) and records, per selection call, which
call it was, the plans' contracts, metrics and grains, and the partitions it resolved per
dataset with their bucket grains and window. Then it asks chat's quarterly Reach questions
through the measurement tool's own request path, without a horizon, under the same recorder.

Control: the month variant (`reach#last_complete_month`) runs through the same composition and
must not raise `SELECTION_TOO_LARGE`; if it does, the composition, not the quarter, is wrong.

AWS calls: STS, then DynamoDB reads only (`GetItem`, `Query`, `TransactGetItems`,
`BatchGetItem`); every other DynamoDB call is refused by the client guard before it is sent.
No Athena, no S3, no writes. The output names counts, contract IDs, dataset keys, metric names
and grains; never a partition key, a population key or a publication ID.
"""

from __future__ import annotations

import argparse
import contextvars
import json
import sys
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

import boto3

from adapters.config import PresetArtifactBakeSettings
from adapters.inbound.tool_handlers import gateway
from adapters.inbound.tool_handlers.measurement import MeasurementTooling, handle
from adapters.inbound.tool_handlers.measurement_dispatch import MeasurementDispatch
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.preset_artifact_publisher.baker import (
    _reach_baker,  # pyright: ignore[reportPrivateUsage]
)
from adapters.jobs.preset_artifact_publisher.transport import BakeIntent
from adapters.outbound.bake_sources import (
    BakeClock,
    BakeQueryCapture,
    BakeRowHorizon,
    BakeSourceReads,
)
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.dynamodb.semantic_state import DynamoCurrentPublications
from adapters.outbound.media_taxonomy import load_media_taxonomy
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.publication_selection import (
    SELECTION_TOO_LARGE,
    PerPackPresentationEvidenceInspection,
    PublicationSelector,
)
from adapters.outbound.segment_roster import RosterReaders
from adapters.outbound.source_pack import load_source_packs
from adapters.outbound.spend_roster import SpendRosterReader, ad_product_labels
from adapters.producer.packs import load_query_packs
from domain.operations.execution_graph_release import BakeRoster
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.shared_kernel.errors import AdlyticsError, DependencyError
from domain.shared_kernel.provenance import Suppression
from ports.outbound.query import QueryResult

SCHEMA: Final = "reach-quarter-partitions/v1"
_ENVIRONMENT: Final = "dev"
_REGION: Final = "us-east-1"
_ROOT: Final = Path(__file__).resolve().parents[1]
_QUARTER: Final = BakeIntent("reach", "last_complete_quarter")
_MONTH: Final = BakeIntent("reach", "last_complete_month")
_READS: Final = frozenset(
    {"get_item", "query", "transact_get_items", "batch_get_item", "get_paginator"}
)
CHAT_QUESTIONS: Final[tuple[tuple[str, dict[str, Any]], ...]] = (
    (
        "reach_last_quarter",
        {
            "metric_reference": "shoppers_in_frequency_bucket",
            "grains": ["marketplace", "frequency_bucket", "identity_space"],
            "selectors": {"time_grain": "quarter"},
            "period_reference": "last quarter",
        },
    ),
    (
        "reach_by_channel_last_quarter",
        {
            "metric_reference": "shoppers_in_frequency_bucket",
            "grains": [
                "marketplace",
                "channel",
                "ad_product",
                "frequency_bucket",
                "identity_space",
            ],
            "selectors": {"time_grain": "quarter"},
            "period_reference": "last quarter",
        },
    ),
)
"""Chat's two quarterly Reach questions, as the model would call `measurement_metric_by_grain`."""


class ProbeError(RuntimeError):
    """The probe could not measure; nothing is written as a result."""


class ReadOnlyDynamoDB:
    """A DynamoDB client that sends reads only; any other call raises before it is sent."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.calls: dict[str, int] = {}

    def __getattr__(self, name: str) -> Any:
        if name not in _READS:
            raise ProbeError(f"the probe is read-only: DynamoDB {name} refused")
        self.calls[name] = self.calls.get(name, 0) + 1
        return getattr(self._client, name)


# --- the selection recorder ---------------------------------------------------------------------

_CALLER: contextvars.ContextVar[str] = contextvars.ContextVar("probe_caller", default="unknown")


@dataclass
class SelectionCall:
    """One resolution: who asked, what it planned, what it resolved, whether it raised."""

    caller: str
    contracts: list[str]
    plans: list[dict[str, str | None]]
    partitions: list[Any]
    raised: dict[str, object] | None = None

    def summary(self) -> dict[str, object]:
        """Return the call's counts by dataset; no partition, population or publication key."""
        by_dataset: dict[str, dict[str, Any]] = {}
        for partition in self.partitions:
            entry = by_dataset.setdefault(
                partition.scope.dataset.value,
                {
                    "partitions": 0,
                    "populations": set(),
                    "lenses": set(),
                    "bucket_grains": {},
                    "bucket_days": set(),
                    "window": [None, None],
                },
            )
            bucket = partition.bucket
            entry["partitions"] += 1
            entry["populations"].add(partition.scope.population_key)
            entry["lenses"].add(partition.scope.lens.value)
            grain = bucket.grain.value
            entry["bucket_grains"][grain] = entry["bucket_grains"].get(grain, 0) + 1
            entry["bucket_days"].add(bucket.window.days)
            start, end = entry["window"]
            entry["window"] = [
                min(start, bucket.window.start) if start else bucket.window.start,
                max(end, bucket.window.end) if end else bucket.window.end,
            ]
        return {
            "caller": self.caller,
            "contracts": sorted(set(self.contracts)),
            "plans": _plan_counts(self.plans),
            "partitions_total": len(self.partitions),
            "by_dataset": {
                dataset: {
                    "partitions": entry["partitions"],
                    "populations": len(entry["populations"]),
                    "lenses": len(entry["lenses"]),
                    "bucket_grains": dict(sorted(entry["bucket_grains"].items())),
                    "bucket_days": sorted(entry["bucket_days"]),
                    "window": [day.isoformat() for day in entry["window"]],
                }
                for dataset, entry in sorted(by_dataset.items())
            },
            "raised": self.raised,
        }


def _plan_counts(plans: Sequence[Mapping[str, str | None]]) -> list[dict[str, object]]:
    counted: dict[tuple[str, str, str, str], int] = {}
    for item in plans:
        key = tuple(
            str(item.get(name) or "") for name in ("metric", "fact", "time_grain", "grain_set")
        )
        counted[cast("tuple[str, str, str, str]", key)] = counted.get(cast("Any", key), 0) + 1
    return [
        {
            "metric": metric,
            "fact": fact,
            "time_grain": grain or None,
            "grain_set": grain_set or None,
            "plans": count,
        }
        for (metric, fact, grain, grain_set), count in sorted(counted.items())
    ]


@dataclass
class Recorder:
    """Every selection call made while it is installed, in order."""

    calls: list[SelectionCall] = field(default_factory=list[SelectionCall])

    def reset(self) -> list[SelectionCall]:
        """Return the calls so far and start again."""
        calls, self.calls = self.calls, []
        return calls


@contextmanager
def caller(name: str) -> Generator[None]:
    """Name the selection entry point the nested resolutions belong to."""
    token = _CALLER.set(name)
    try:
        yield
    finally:
        _CALLER.reset(token)


def _record_plans(plans: Sequence[Any]) -> list[dict[str, str | None]]:
    recorded: list[dict[str, str | None]] = []
    for plan in plans:
        filters = dict(plan.filters)
        recorded.append(
            {
                "metric": getattr(getattr(plan, "metric", None), "name", None),
                "fact": plan.fact,
                "time_grain": filters.get("time_grain"),
                "grain_set": filters.get("grain_set"),
            }
        )
    return recorded


def install(recorder: Recorder) -> Callable[[], None]:
    """Wrap the selector's resolution and its three entry points; return the uninstaller."""
    originals: list[tuple[type, str, Any]] = []

    def wrap(owner: type, name: str, replacement: Callable[..., Any]) -> None:
        originals.append((owner, name, owner.__dict__[name]))
        setattr(owner, name, replacement)

    real_requirements = PublicationSelector._resolve_requirements  # pyright: ignore[reportPrivateUsage]

    def resolve_requirements(self: Any, plans: Any, contracts: Any) -> Any:
        resolved = real_requirements(self, plans, contracts)
        recorder.calls.append(
            SelectionCall(
                _CALLER.get(),
                [contract.contract_id for contract in contracts],
                _record_plans(plans),
                list(resolved.partitions),
            )
        )
        return resolved

    def raising(entry: Callable[..., Any], name: str) -> Callable[..., Any]:
        def recorded(self: Any, *arguments: Any, **keywords: Any) -> Any:
            with caller(name):
                try:
                    return entry(self, *arguments, **keywords)
                except AdlyticsError as error:
                    if recorder.calls and recorder.calls[-1].caller == name:
                        recorder.calls[-1].raised = {
                            "code": str(error.code),
                            **{
                                key: error.context.get(key)
                                for key in ("required_partitions", "maximum_partitions")
                                if key in error.context
                            },
                        }
                    raise

        return recorded

    wrap(PublicationSelector, "_resolve_requirements", resolve_requirements)
    wrap(PublicationSelector, "select", raising(PublicationSelector.select, "select"))
    wrap(
        PublicationSelector,
        "resolve_partitions",
        raising(PublicationSelector.resolve_partitions, "resolve_partitions"),
    )
    wrap(PublicationSelector, "inspect", raising(PublicationSelector.inspect, "inspect"))
    wrap(
        PerPackPresentationEvidenceInspection,
        "inspect",
        raising(PerPackPresentationEvidenceInspection.inspect, "per_pack_inspection"),
    )

    def uninstall() -> None:
        for owner, name, original in reversed(originals):
            setattr(owner, name, original)

    return uninstall


# --- the bake's quarter render ------------------------------------------------------------------


class EmptyAnswers:
    """A query port that executes nothing: every statement answers no rows."""

    def run(self, request: object) -> QueryResult:
        """Answer empty."""
        del request
        return QueryResult(
            rows=(), bytes_scanned=0, truncated=False, suppression=Suppression(False)
        )

    def run_series(self, request: object) -> QueryResult:
        """Answer empty."""
        return self.run(request)


class NoPointers:
    """No current pointer: the render runs to its publication, which is never written."""

    def current(self, page: object, variant: object) -> None:
        """Return no pointer."""
        del page, variant


@dataclass(frozen=True, slots=True)
class BakeComposition:
    """The bake's Reach source over a read-only CURRENT reader, and its reads and queries."""

    baker: Any
    reads: BakeSourceReads
    queries: BakeQueryCapture


def compose_bake(current: Any) -> BakeComposition:
    """Compose the Reach baker as `baker.application` does, over `current` and no Athena."""
    root = _ROOT
    reads = BakeSourceReads(
        current,
        PublicationSelector(current, load_source_packs(root), load_commerce_identity_bridge(root)),
        BakeClock(),
    )
    queries = BakeQueryCapture(EmptyAnswers())
    # Only truthiness and the concurrency are read: no client is built from these.
    settings = PresetArtifactBakeSettings(
        artifact_bucket="probe-unused",
        pointer_table="probe-unused",
        semantic_publications_table="probe-reader-injected",
        semantic_v2_bucket="probe-unused",
        athena_workgroup="probe-unused",
        semantic_v2_current_database="probe-unused",
        athena_result_reuse_max_age_minutes=1,
        athena_query_batch_max_concurrency=10,
    )
    return BakeComposition(
        _reach_baker(settings, cast("Any", NoPointers()), queries, reads), reads, queries
    )


def run_pin_pass(
    composition: BakeComposition, intent: BakeIntent, instant: datetime
) -> dict[str, object]:
    """Run one row's pin pass as the fan-out does: prefetch, then the capturing render."""
    row = BakeRoster.from_contract().row(f"{intent.page}#{intent.variant}")
    horizon = BakeRowHorizon(instant.date(), row.source_horizon_days)
    reads, queries = composition.reads, composition.queries
    outcome: dict[str, object] = {"raised": None, "captured_requests": 0}
    with reads.clock.pinned(instant), reads.snapshot():
        with reads.recording(horizon), queries.capturing_scope() as captured:
            try:
                with queries.forbidden():
                    composition.baker.prefetch(intent)
                composition.baker.bake(intent)
            except DependencyError:
                raise
            except AdlyticsError as error:
                outcome["raised"] = str(error.code)
        outcome["captured_requests"] = len(captured.requests) if outcome["raised"] is None else 0
    return outcome


# --- chat's quarterly reads ---------------------------------------------------------------------


class NoEntities:
    """Chat's entity catalog, unreachable: the quarterly questions name no entity."""

    def __getattr__(self, name: str) -> Any:
        raise ProbeError(f"a quarterly reach question reached the entity catalog ({name})")


def ask_chat(current: Any, now: datetime) -> dict[str, dict[str, object]]:
    """Ask each quarterly question through chat's request path; select reads whole (no horizon)."""
    root = _ROOT
    bridge = load_commerce_identity_bridge(root)
    catalog = load_catalog(root)
    contracts = tuple(contract for _, contract in load_contracts(root))
    selector = PublicationSelector(
        current,
        load_source_packs(root),
        bridge,
        cadence_lag_days={
            pack.contract_id: pack.effective_cadence_lag_days for pack in load_query_packs(root)
        },
    )
    queries = cast("Any", EmptyAnswers())
    tooling = MeasurementTooling(
        queries=queries,
        publications=selector,
        periods=selector,
        pack_coverage=selector,
        # Chat's spend roster, as the measurement entrypoint composes it: a per-channel answer
        # rosters the ad products with spend in the period through the same selector.
        rosters=RosterReaders(
            spend=SpendRosterReader(
                catalog=catalog,
                contracts=contracts,
                publications=selector,
                queries=queries,
                labels=ad_product_labels(load_media_taxonomy(root)),
                clock=lambda: now,
            )
        ),
    )
    dispatch = MeasurementDispatch(
        catalog=catalog,
        contracts=contracts,
        entity_catalog=cast("Any", NoEntities()),
        scope=AuthorizedAnalyticalScope(frozenset({bridge.commerce_scope})),
        tooling=tooling,
        now=lambda: now,
    )
    outcomes: dict[str, dict[str, object]] = {}
    for label, arguments in CHAT_QUESTIONS:
        tool = dispatch.tool("measurement_metric_by_grain")
        try:
            request = dispatch.request_from(
                gateway.GatewayInvocation("measurement_metric_by_grain", dict(arguments), "probe"),
                tool,
                f"probe-{label}",
            )
            handle(request, tooling)
        except DependencyError:
            raise
        except AdlyticsError as error:
            outcomes[label] = {"outcome": "refused", "code": str(error.code)}
            continue
        outcomes[label] = {"outcome": "answered", "code": None}
    return outcomes


# --- the run ------------------------------------------------------------------------------------


def classify(quarter_calls: Sequence[Mapping[str, Any]]) -> dict[str, object]:
    """Name the raising call and its per-dataset counts, for 05 §4's table (not the branch)."""
    raising = [call for call in quarter_calls if call.get("raised")]
    first = raising[0] if raising else None
    datasets: Mapping[str, Mapping[str, Any]] = (
        cast("Mapping[str, Mapping[str, Any]]", first["by_dataset"]) if first else {}
    )
    return {
        "raising_call": None if first is None else first["caller"],
        "raising_code": None
        if first is None
        else cast("Mapping[str, Any]", first["raised"])["code"],
        "partitions_total": None if first is None else first["partitions_total"],
        "max_partitions_in_one_dataset": max(
            (int(item["partitions"]) for item in datasets.values()), default=None
        ),
        "bucket_grains": sorted(
            {
                str(grain)
                for item in datasets.values()
                for grain in cast("Mapping[str, int]", item["bucket_grains"])
            }
        ),
    }


def measure(current: Any, instant: datetime, now: datetime) -> dict[str, object]:
    """Run the quarter, the month control and chat's questions under one recorder."""
    recorder = Recorder()
    uninstall = install(recorder)
    try:
        variants: dict[str, object] = {}
        for intent in (_QUARTER, _MONTH):
            outcome = run_pin_pass(compose_bake(current), intent, instant)
            calls = [call.summary() for call in recorder.reset()]
            variants[intent.variant] = {**outcome, "calls": calls}
        chat = ask_chat(current, now)
        chat_calls = [call.summary() for call in recorder.reset()]
    finally:
        uninstall()
    quarter = cast("Mapping[str, Any]", variants[_QUARTER.variant])
    month = cast("Mapping[str, Any]", variants[_MONTH.variant])
    control_passed = all(
        not call.get("raised")
        or cast("Mapping[str, Any]", call["raised"]).get("code") != str(SELECTION_TOO_LARGE)
        for call in month["calls"]
    )
    return {
        "variants": variants,
        "quarter": classify(quarter["calls"]),
        "month_control": {"passed": control_passed, "raised": month["raised"]},
        "chat": {"outcomes": chat, "calls": chat_calls},
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--publications-table", required=True)
    parser.add_argument("--graph-table", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def run_instant(dynamodb: Any, graph_table: str, run_id: str) -> datetime:
    """Read the run's pinned instant, `compute_started_at`, with one strong GetItem."""
    item = cast(
        "Mapping[str, Mapping[str, object]]",
        dynamodb.get_item(
            TableName=graph_table,
            Key={"pk": {"S": f"RUN#{run_id}"}, "sk": {"S": "RUN"}},
            ConsistentRead=True,
        ).get("Item")
        or {},
    )
    started = item.get("compute_started_at", {}).get("S")
    if not isinstance(started, str):
        raise ProbeError("the run record has no compute_started_at")
    instant = datetime.fromisoformat(started)
    if instant.tzinfo is None:
        raise ProbeError("the run's compute_started_at is not timezone-aware")
    return instant.astimezone(UTC)


def main(argv: list[str] | None = None, session_factory: Callable[..., Any] = boto3.Session) -> int:
    """Measure and write `--output`; a refused identity or a failed read writes nothing."""
    args = _parser().parse_args(argv)
    if args.environment != _ENVIRONMENT or args.region != _REGION:
        print("this probe runs only against the approved dev region")
        return 2
    session = session_factory(profile_name=args.profile, region_name=args.region)
    identity = session.client("sts").get_caller_identity()
    if identity.get("Account") != args.expected_account:
        print("explicit profile did not resolve to the expected account")
        return 2
    dynamodb = ReadOnlyDynamoDB(session.client("dynamodb"))
    try:
        instant = run_instant(dynamodb, args.graph_table, args.run_id)
        current = DynamoCurrentPublications(args.publications_table, cast("Any", dynamodb))
        now = datetime.now(UTC)
        measured = measure(current, instant, now)
    except (ProbeError, DependencyError) as error:
        print(f"NOT_MEASURED: {type(error).__name__}: {error}")
        return 1
    document = {
        "schema_version": SCHEMA,
        "measured_at": now.isoformat(),
        "identity": {
            "account_matches_expected": True,
            "environment": args.environment,
            "region": args.region,
        },
        "bake_instant": instant.isoformat(),
        "dynamodb_calls": dict(sorted(dynamodb.calls.items())),
        **measured,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    quarter = cast("Mapping[str, object]", measured["quarter"])
    print(
        f"quarter raised {quarter['raising_code']} at {quarter['raising_call']} with "
        f"{quarter['partitions_total']} partitions (max {quarter['max_partitions_in_one_dataset']} "
        f"in one dataset); month control passed: "
        f"{cast('Mapping[str, object]', measured['month_control'])['passed']}"
    )
    print(f"output: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
