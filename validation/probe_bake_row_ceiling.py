# SPDX-License-Identifier: MIT-0
"""Measure the bytes the bake memory model needs and check the streamed reader (ADR-143).

    uv run python validation/probe_bake_row_ceiling.py --output <path> --local-only
    uv run python validation/probe_bake_row_ceiling.py --output <path> --profile <profile> \
        --environment dev --region us-east-1 --expected-account <account> \
        --bake-workgroup <bake workgroup> --interactive-workgroup <interactive workgroup> \
        --database <current database>

**P1, local, no AWS.** For N in {10,000; 30,000; 50,000}, three runs each, under `tracemalloc`:
N synthetic CSV rows of the two widest held shapes (the Campaigns compare: a 40-character
campaign key, the marketplace, a decimal, a currency; the Audiences grouped answer: three string
dimensions of 10, 40 and 24 characters and a decimal) are streamed from an in-memory result
object through the real path: `stream_result_rows`, the adapter's row mapping, a held answer
(`answer_measurement`) and a fold of each kind (`fold_answer`). B_raw is the held rows' peak per
row, B_shaped the shaped answer's retained bytes per row, B_kept a fold entry's bytes. B_render
is the real Campaigns seven-sibling render's peak per campaign at the existing estate fixture.
Each is the maximum over the runs. The derivation then asks, for V in 50,000 down to 10,000,
whether every roster row's `bakeability.resident` fits at 1,024 MiB, and prints the same at
2,048 MiB.

**P2, dev, read-only SQL** in the explicit bake workgroup, reading no table and no customer data:
P2a reads one `VALUES` row of edge cases both ways (`GetQueryResults` and the result object)
and compares every cell; P2b streams `UNNEST` rows at 50,000, 500,000 and 1,550,000, three runs
each, for engine, queue and stream time; P2c runs one nonce-literal statement in the interactive
workgroup, then twice-plus-one in the bake workgroup with reuse on, recording
`ReusedPreviousResult` and each `OutputLocation`'s prefix. AWS calls: STS, Athena
`StartQueryExecution`, `GetQueryExecution`, `GetQueryResults`, and S3 `GetObject` on the result
objects those executions name. Nothing is written by the probe except Athena's own results.

The output holds every measured term, the derivation per candidate, the stop-rule verdict and
the identity readback; never an account, a bucket or a workgroup name.
"""

from __future__ import annotations

import argparse
import csv
import gc
import importlib
import io
import json
import sys
import time
import tracemalloc
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final, cast

import boto3

from adapters.inbound.tool_handlers.measurement import (
    MeasurementTool,
    MeasurementToolRequest,
    prepare_batch,
)
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.preset_artifact_publisher import bakeability
from adapters.outbound.athena.execution import (
    StatementOutcome,
    StatementRequest,
    await_completion,
    iter_pages,
    start,
    stream_result_rows,
)
from adapters.outbound.athena.query import (
    _as_row,  # pyright: ignore[reportPrivateUsage]
    _suppression_from,  # pyright: ignore[reportPrivateUsage]
)
from adapters.outbound.metric_dictionary import load_catalog
from domain.catalog.freshness import Coverage
from domain.measurement.answers import (
    AnswerRequest,
    MeasurementContext,
    answer_measurement,
    fold_answer,
)
from domain.measurement.folds import KeysByGroup, ObservedKeys
from domain.measurement.planning import CURRENCY_COLUMN, QueryPlan
from domain.shared_kernel.provenance import FreshnessStatus
from domain.shared_kernel.values import DateRange
from ports.outbound.query import BAKE_MAX_ROWS, RowCeiling

SCHEMA: Final = "bake-row-ceiling/v1"
_ENVIRONMENT: Final = "dev"
_REGION: Final = "us-east-1"
_ROOT: Final = Path(__file__).resolve().parents[1]
P1_SIZES: Final = (10_000, 30_000, 50_000)
P1_RUNS: Final = 3
P2B_SIZES: Final = (50_000, 500_000, 1_550_000)
P2B_RUNS: Final = 3
CANDIDATES: Final = (50_000, 40_000, 30_000, 20_000, 10_000)
"""The derivation's candidates, largest first; never above the owner's 50,000 proposal."""
OWNER_ENVELOPE: Final = 10_000
"""Below it Campaigns couldn't hold its own shown population (decision 6): the stop rule."""
MEBIBYTE: Final = 1024 * 1024
_WINDOW: Final = DateRange(date(2026, 8, 1), date(2026, 8, 30))
_NOW: Final = datetime(2026, 9, 1, tzinfo=UTC)
_REUSE_MINUTES: Final = 60


class ProbeError(RuntimeError):
    """The probe could not measure what it set out to; nothing is concluded."""


# --- P1: the local byte terms -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Shape:
    """One held answer's widest real shape: a plan and the cell values of row `index`."""

    name: str
    plan: QueryPlan
    cells: Callable[[int], list[str | None]]


def _planned(metric: str, grains: tuple[str, ...]) -> QueryPlan:
    catalog = load_catalog(_ROOT)
    contracts = tuple(contract for _, contract in load_contracts(_ROOT))
    request = MeasurementToolRequest(
        tool=MeasurementTool.METRIC_BY_GRAIN,
        catalog=catalog,
        contracts=contracts,
        metric_name=metric,
        grains=grains,
        window=_WINDOW,
        computed_at=_NOW,
        correlation_id="bake-row-ceiling-probe",
        max_rows=BAKE_MAX_ROWS,
        row_ceiling=RowCeiling.BAKE,
    )
    return prepare_batch((request,)).operations[0].current


def _key(prefix: str, index: int, width: int) -> str:
    return f"{prefix}{index:0{width - len(prefix)}d}"


def shapes() -> tuple[Shape, ...]:
    """Return the Campaigns compare and the Audiences grouped-answer width, on real plans."""
    campaigns = _planned("ad_spend", ("campaign", "marketplace"))
    audiences = _planned("retail_revenue_shipped", ("date", "product", "marketplace"))

    def _cells(
        plan: QueryPlan, values: Mapping[str, Callable[[int], str]]
    ) -> Callable[[int], list[str | None]]:
        def cells(index: int) -> list[str | None]:
            row: list[str | None] = [values[column](index) for column in plan.group_by]
            row.extend(
                "USD" if column == CURRENCY_COLUMN else "x" for column in plan.carried_columns
            )
            row.append(f"{index % 9_973}.{index % 10_000:04d}")
            row.extend("0" for _ in plan.result_aliases[1:])
            return row

        return cells

    return (
        Shape(
            "campaigns_compare",
            campaigns,
            _cells(
                campaigns,
                {
                    "campaign_key": lambda index: _key("campaign:", index, 40),
                    "marketplace_key": lambda _index: "amazon_us",
                },
            ),
        ),
        Shape(
            "audiences_grouped",
            audiences,
            _cells(
                audiences,
                {
                    "date_key": lambda index: _key("seg:", index, 24),
                    "amazon_asin": lambda index: _key("campaign:", index, 40),
                    "marketplace_key": lambda _index: "amazon_us",
                },
            ),
        ),
    )


class _Body(io.RawIOBase):
    """An in-memory result object served in 64 KiB reads, as S3 streams one."""

    def __init__(self, data: bytes) -> None:
        self._data = memoryview(data)
        self._at = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        size = min(len(buffer), 65_536, len(self._data) - self._at)
        buffer[:size] = self._data[self._at : self._at + size]
        self._at += size
        return size


class _ObjectStore:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def get_object(self, **_arguments: str) -> dict[str, object]:
        return {"Body": io.BufferedReader(_Body(self._data))}


def _result_object(shape: Shape, rows: int) -> bytes:
    plan = shape.plan
    out = io.StringIO()
    writer = csv.writer(out, quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writerow((*plan.selected_columns, *plan.result_aliases))
    for index in range(rows):
        writer.writerow(shape.cells(index))
    return out.getvalue().encode()


_LOCATION: Final = "s3://probe-local/bake/result.csv"


def _mapped_rows(shape: Shape, data: bytes) -> Iterator[dict[str, Any]]:
    plan = shape.plan
    cells = len(plan.selected_columns) + len(plan.result_aliases)
    for values in stream_result_rows(
        cast("Any", _ObjectStore(data)),
        StatementOutcome(output_location=_LOCATION),
        execution_id="probe-local",
        cells=cells,
    ):
        yield _as_row(plan.selected_columns, values, aggregates=plan.result_aliases)


def _answer_request(plan: QueryPlan, rows: tuple[dict[str, Any], ...]) -> AnswerRequest:
    return AnswerRequest(
        plan=plan,
        context=MeasurementContext(computed_at=_NOW, correlation_id="bake-row-ceiling-probe"),
        rows=rows,
        suppression=_suppression_from(plan, rows),
        coverage=Coverage(
            fact_name=plan.fact,
            window=_WINDOW,
            freshness=FreshnessStatus.FRESH,
            measurement_basis=plan.measurement_basis[0],
            valid_grains=plan.grain.dimensions,
            intervals=(_WINDOW,),
        ),
        applied_defaults={},
        additive=True,
    )


def _traced(work: Callable[[], object]) -> tuple[int, int, object]:
    """Run `work` under tracemalloc; return its peak, its retained bytes and its result."""
    gc.collect()
    tracemalloc.start()
    try:
        result = work()
        gc.collect()
        current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak, current, result


@dataclass(frozen=True, slots=True)
class HeldMeasure:
    """One held read's raw peak and shaped retained bytes."""

    shape: str
    rows: int
    raw_peak: int
    shaped_retained: int


def measure_held(shape: Shape, rows: int) -> HeldMeasure:
    """One held read at `rows`: the raw rows' peak, then the shaped answer's retained bytes."""
    data = _result_object(shape, rows)
    raw_peak, _, held = _traced(lambda: tuple(_mapped_rows(shape, data)))
    held_rows = cast("tuple[dict[str, Any], ...]", held)
    request = _answer_request(shape.plan, held_rows)
    del held, held_rows
    gc.collect()
    _, shaped, answer = _traced(lambda: answer_measurement(request))
    if len(cast("Any", answer).data.values) != rows:
        raise ProbeError(f"{shape.name}: the held answer did not keep {rows} rows.")
    return HeldMeasure(shape.name, rows, raw_peak, shaped)


def measure_kept(shape: Shape, rows: int) -> dict[str, int]:
    """Each fold kind's retained bytes at `rows` distinct kept entries, streamed (never held)."""
    data = _result_object(shape, rows)
    plan = shape.plan
    key, group = plan.group_by[-2], plan.group_by[0]
    kept: dict[str, int] = {}
    for name, fold in (
        ("observed_keys", lambda: ObservedKeys((key,), rows)),
        ("keys_by_group", lambda: KeysByGroup(group, key, rows)),
    ):
        _, retained, result = _traced(
            lambda fold=fold: (
                fold_answer(_answer_request(plan, ()), _mapped_rows(shape, data), fold()).value
            )
        )
        size = (
            sum(len(item) for item in cast("Any", result).values())
            if isinstance(result, dict)
            else len(cast("Any", result))
        )
        if size != rows:
            raise ProbeError(f"{shape.name}: the {name} fold kept {size}, not {rows}.")
        kept[name] = retained
    return kept


def _support_path() -> None:
    for path in (str(_ROOT / "validation"), str(_ROOT / "tests" / "unit")):
        if path not in sys.path:
            sys.path.insert(0, path)


def measure_retail_render(products: int) -> int:
    """Measure the Retail render's own structures at `products`: its peak after the batch.

    The real renderer over the unit tier's fictional query fake; tracing restarts its peak the
    moment the measurement batch returns, so the bytes are the render's per-product structures
    (every product's comparisons, the movers, the 50 rows), not the answers the model counts.
    """
    _support_path()
    fixture = importlib.import_module("test_retail_health_preset_source")
    retail: Any = importlib.import_module("adapters.jobs.preset_artifact_publisher.retail_source")
    keys = fixture._products(products)
    renderer = fixture._renderer(
        fixture.RetailQueries(keys, fixture.PERIOD),
        fixture.FakeHierarchy(fixture._hierarchy_result(keys)),
        fixture.FakeReported(fixture.RetailReportedTotals(0, 0, ("reported-total",))),
    )
    real = retail.execute_presentation_batch
    baseline: list[int] = []

    def measured(*arguments: Any, **keywords: Any) -> Any:
        result = real(*arguments, **keywords)
        if not baseline:
            gc.collect()
            tracemalloc.reset_peak()
            baseline.append(tracemalloc.get_traced_memory()[0])
        return result

    retail.execute_presentation_batch = measured
    gc.collect()
    tracemalloc.start()
    try:
        rendered = renderer.render_retail_health(
            retail.RetailHealthRenderInput(fixture.PERIOD, fixture.PRIOR)
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        retail.execute_presentation_batch = real
    if rendered.artifact is None or rendered.artifact.total_count != products or not baseline:
        raise ProbeError(f"the Retail render at {products} products did not bake.")
    return peak - baseline[0]


def measure_campaign_render() -> tuple[int, int, int]:
    """Measure the Campaigns seven-sibling estate bake: (peak, encoded body bytes, campaigns).

    Today's Campaigns bake holds every body until it writes (bodies stream only with 04 §4), so
    its peak less its encoded bodies still carries every rendered body object.
    """
    _support_path()
    estate = importlib.import_module("accept_campaign_estate_local")
    result = estate.run_filter_estate(_ROOT)
    return int(result.peak_traced_bytes), int(result.total_bytes), int(result.campaign_count)


def run_p1(sizes: tuple[int, ...] = P1_SIZES, runs: int = P1_RUNS) -> dict[str, Any]:
    """Measure B_raw, B_shaped, B_kept and B_render; each the maximum over every run, per row."""
    raw, shaped, kept, render = 0.0, 0.0, 0.0, 0.0
    samples: list[dict[str, Any]] = []
    for shape in shapes():
        for rows in sizes:
            for run in range(runs):
                held = measure_held(shape, rows)
                folds = measure_kept(shape, rows)
                raw = max(raw, held.raw_peak / rows)
                shaped = max(shaped, held.shaped_retained / rows)
                kept = max(kept, *(value / rows for value in folds.values()))
                samples.append(
                    {
                        "shape": shape.name,
                        "rows": rows,
                        "run": run + 1,
                        "raw_peak_bytes": held.raw_peak,
                        "shaped_retained_bytes": held.shaped_retained,
                        "fold_retained_bytes": folds,
                    }
                )
    renders: list[dict[str, Any]] = []
    campaign_render = 0.0
    for _run in range(runs):
        for products in sizes:
            peak = measure_retail_render(products)
            render = max(render, peak / products)
            renders.append({"render": "retail_health", "rows": products, "peak_bytes": peak})
        peak, bodies, campaigns = measure_campaign_render()
        campaign_render = max(campaign_render, (peak - bodies) / campaigns)
        renders.append(
            {
                "render": "campaigns_estate",
                "rows": campaigns,
                "peak_bytes": peak,
                "encoded_body_bytes": bodies,
            }
        )
    return {
        "sizes": list(sizes),
        "runs": runs,
        "samples": samples,
        "render_samples": renders,
        "terms_bytes_per_row": {
            "B_raw": round(raw, 1),
            "B_shaped": round(shaped, 1),
            "B_kept": round(kept, 1),
            "B_render": round(render, 1),
            "B_render_campaigns_estate": round(campaign_render, 1),
        },
    }


def _terms(
    p1: Mapping[str, Any], measured: str, *, render: str = "B_render"
) -> bakeability.BakeMemoryTerms:
    terms = cast("Mapping[str, float]", p1["terms_bytes_per_row"])
    return bakeability.BakeMemoryTerms(
        raw=int(-(-terms["B_raw"] // 1)),
        shaped=int(-(-terms["B_shaped"] // 1)),
        kept=int(-(-terms["B_kept"] // 1)),
        render=int(-(-max(terms["B_render"], terms.get(render, 0.0)) // 1)),
        measured=measured,
    )


def derive(
    terms: bakeability.BakeMemoryTerms, memory_mib: int, root: Path = _ROOT
) -> dict[str, Any]:
    """For each candidate V, every row's `resident` at `memory_mib`; the first V that fits all."""
    memory = memory_mib * MEBIBYTE
    rows: dict[str, Any] = {}
    chosen: int | None = None
    for ceiling in CANDIDATES:
        models = bakeability.memory_models(root, ceiling=ceiling, memory_bytes=memory, terms=terms)
        binding = max(models, key=lambda item: item.total)
        fits = all(item.certified for item in models)
        rows[str(ceiling)] = {
            "fits": fits,
            "binding_row": binding.key,
            "binding_terms": binding.terms(),
            "rows": {item.key: item.terms() for item in models},
        }
        if fits and chosen is None:
            chosen = ceiling
    return {"memory_mib": memory_mib, "candidates": rows, "derived": chosen}


# --- P2: dev, read-only SQL --------------------------------------------------------------------

P2A_SQL: Final = (
    "SELECT * FROM (VALUES (CAST(NULL AS VARCHAR), '', 'a \"quoted\" word', 'a,b,c', "
    "concat('line one', chr(10), 'line two'), 'café ü ✓', "
    "CAST('12345678901234567890.1234' AS DECIMAL(38,4)), "
    "TIMESTAMP '2026-10-18 01:02:03.456')) "
    "AS t(c_null, c_empty, c_quoted, c_comma, c_newline, c_unicode, c_decimal, c_timestamp)"
)
"""One row of edge cases; reads no table."""
P2A_CELLS: Final = 8


def p2b_sql(rows: int) -> str:
    """Build `UNNEST` rows, crossed because one `sequence` holds at most 10,000 entries."""
    outer = -(-rows // 1_000)
    return (
        "SELECT a.x * 1000 + b.y AS n, CAST(a.x * 1000 + b.y AS VARCHAR) AS s "  # noqa: S608 -- ints only
        f"FROM UNNEST(sequence(0, {outer - 1})) AS a(x) "
        "CROSS JOIN UNNEST(sequence(1, 1000)) AS b(y) "
        f"LIMIT {rows}"
    )


def p2c_sql(nonce: str) -> str:
    """One `VALUES` row carrying the run's nonce as a literal, so no earlier execution matches."""
    return f"SELECT n FROM (VALUES ('{nonce}')) AS t(n)"  # noqa: S608 -- a uuid4 hex literal


@dataclass
class _Run:
    athena: Any
    s3: Any
    database: str

    def execute(self, sql: str, workgroup: str, reuse_minutes: int) -> tuple[str, StatementOutcome]:
        execution = start(
            self.athena, StatementRequest(sql, (), workgroup, self.database, reuse_minutes)
        )
        return execution, await_completion(self.athena, execution, limit=600)


def _prefix(location: str | None) -> str | None:
    """Name a result location's first prefix only (`bake`, `interactive`), never its bucket."""
    if location is None or not location.startswith("s3://"):
        return None
    parts = location.removeprefix("s3://").split("/")
    return parts[1] if len(parts) > 2 else None  # noqa: PLR2004 -- bucket/prefix/object


def p2a_parity(run: _Run, bake_workgroup: str) -> dict[str, Any]:
    """Read one edge-case row both ways and compare every cell."""
    execution, outcome = run.execute(P2A_SQL, bake_workgroup, 0)
    paged = list(iter_pages(run.athena, execution))
    streamed = list(stream_result_rows(run.s3, outcome, execution_id=execution, cells=P2A_CELLS))
    differing = [
        {"row": row, "cell": cell, "paged": repr(left), "object": repr(right)}
        for row, (paged_row, streamed_row) in enumerate(zip(paged, streamed, strict=False))
        for cell, (left, right) in enumerate(zip(paged_row, streamed_row, strict=False))
        if left != right
    ]
    return {
        "passed": len(paged) == len(streamed) == 1 and not differing,
        "rows": {"paged": len(paged), "object": len(streamed)},
        "differing_cells": differing,
        "null_cell": {
            "paged": repr(paged[0][0]) if paged else None,
            "object": repr(streamed[0][0]) if streamed else None,
        },
        "location_prefix": _prefix(outcome.output_location),
    }


def p2b_stream(
    run: _Run, bake_workgroup: str, *, sizes: tuple[int, ...] = P2B_SIZES, runs: int = P2B_RUNS
) -> dict[str, Any]:
    """Time engine, queue and stream seconds for `UNNEST` rows (evidence, sizes nothing)."""
    samples: list[dict[str, Any]] = []
    for rows in sizes:
        for attempt in range(runs):
            execution, outcome = run.execute(p2b_sql(rows), bake_workgroup, 0)
            started = time.monotonic()
            count = sum(
                1 for _ in stream_result_rows(run.s3, outcome, execution_id=execution, cells=2)
            )
            seconds = time.monotonic() - started
            if count != rows:
                raise ProbeError(f"P2b streamed {count} rows, not {rows}.")
            samples.append(
                {
                    "rows": rows,
                    "run": attempt + 1,
                    "engine_ms": outcome.engine_millis,
                    "queue_ms": outcome.queue_millis,
                    "total_ms": outcome.total_millis,
                    "stream_seconds": round(seconds, 3),
                    "stream_seconds_per_100k": round(seconds * 100_000 / rows, 3),
                }
            )
    return {"samples": samples}


def p2c_reuse(run: _Run, bake_workgroup: str, interactive_workgroup: str) -> dict[str, Any]:
    """Run one nonce statement across workgroups and twice more in the bake one (reuse on)."""
    sql = p2c_sql(uuid.uuid4().hex)
    executions: list[dict[str, Any]] = []
    for step, workgroup in (
        ("cross_interactive", interactive_workgroup),
        ("cross_bake", bake_workgroup),
        ("control_bake_first", bake_workgroup),
        ("control_bake_second", bake_workgroup),
    ):
        _, outcome = run.execute(sql, workgroup, _REUSE_MINUTES)
        executions.append(
            {
                "step": step,
                "workgroup_kind": "interactive" if workgroup == interactive_workgroup else "bake",
                "reused_previous_result": outcome.reused_previous_result,
                "location_prefix": _prefix(outcome.output_location),
            }
        )
    return {"executions": executions, "outcome": classify_p2c(executions)}


def classify_p2c(executions: list[dict[str, Any]]) -> str:
    """Classify P2c by 03 §5's rule: (a) as documented, (b) not, or inconclusive."""
    by_step = {item["step"]: item for item in executions}
    bake = [item for item in executions if item["workgroup_kind"] == "bake"]
    if by_step["cross_bake"]["reused_previous_result"] or any(
        item["location_prefix"] != "bake" for item in bake
    ):
        return "b_not_as_documented"
    if not by_step["control_bake_second"]["reused_previous_result"]:
        return "inconclusive"
    return "a_as_documented"


def stop_rule(
    derived_1024: int | None, p2a: Mapping[str, Any] | None, p2c: Mapping[str, Any] | None
) -> dict[str, Any]:
    """03 §5's stop rule: any reason here pauses for the owner's A3 answer."""
    reasons: list[str] = []
    if derived_1024 is None or derived_1024 < OWNER_ENVELOPE:
        reasons.append("no candidate V >= 10,000 fits at 1,024 MiB")
    if p2a is not None and not p2a["passed"]:
        reasons.append("P2a reader parity failed")
    if p2c is not None and p2c["outcome"] != "a_as_documented":
        reasons.append(f"P2c is {p2c['outcome']}")
    return {"stop": bool(reasons), "reasons": reasons}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--local-only", action="store_true")
    parser.add_argument("--profile")
    parser.add_argument("--environment")
    parser.add_argument("--region")
    parser.add_argument("--expected-account")
    parser.add_argument("--bake-workgroup")
    parser.add_argument("--interactive-workgroup")
    parser.add_argument("--database")
    return parser


_P2_INPUTS: Final = (
    "profile",
    "environment",
    "region",
    "expected_account",
    "bake_workgroup",
    "interactive_workgroup",
    "database",
)


def main(argv: list[str] | None = None, session_factory: Callable[..., Any] = boto3.Session) -> int:
    """Measure, derive and write `--output`; a refused identity writes nothing."""
    args = _parser().parse_args(argv)
    p2_inputs = {name: getattr(args, name) for name in _P2_INPUTS}
    if not args.local_only and not all(p2_inputs.values()):
        missing = ", ".join(
            f"--{name.replace('_', '-')}" for name, value in p2_inputs.items() if not value
        )
        print(f"P2 needs every target input explicitly: {missing} (or --local-only)")
        return 2
    session = None
    identity: dict[str, object] = {"aws": False}
    if not args.local_only:
        if args.environment != _ENVIRONMENT or args.region != _REGION:
            print("this probe runs only against the approved dev region")
            return 2
        session = session_factory(profile_name=args.profile, region_name=args.region)
        caller = session.client("sts").get_caller_identity()
        if caller.get("Account") != args.expected_account:
            print("explicit profile did not resolve to the expected account")
            return 2
        identity = {
            "aws": True,
            "account_matches_expected": True,
            "environment": args.environment,
            "region": args.region,
        }
    measured_at = datetime.now(UTC)
    p1 = run_p1()
    label = f"P1 {measured_at.date().isoformat()}, tracemalloc, max of {P1_RUNS} runs"
    terms = _terms(p1, label)
    derivations = {str(memory): derive(terms, memory) for memory in (1_024, 2_048)}
    # 03 §5 names the Campaigns render too: today it holds every body object (04 §4 streams
    # them later), so its per-row term is derived beside the population-wide Retail render's.
    literal = _terms(p1, label, render="B_render_campaigns_estate")
    literal_derivations = {str(memory): derive(literal, memory) for memory in (1_024, 2_048)}
    p2: dict[str, Any] = {"ran": False}
    if session is not None:
        run = _Run(session.client("athena"), session.client("s3"), args.database)
        p2a = p2a_parity(run, args.bake_workgroup)
        p2 = {
            "ran": True,
            "p2a": p2a,
            "p2b": p2b_stream(run, args.bake_workgroup),
            "p2c": p2c_reuse(run, args.bake_workgroup, args.interactive_workgroup),
        }
    verdict = stop_rule(
        literal_derivations["1024"]["derived"],
        p2.get("p2a") if p2["ran"] else None,
        p2.get("p2c") if p2["ran"] else None,
    )
    document = {
        "schema_version": SCHEMA,
        "measured_at": measured_at.isoformat(),
        "identity": identity,
        "p1": p1,
        "terms": {
            "B_raw": terms.raw,
            "B_shaped": terms.shaped,
            "B_kept": terms.kept,
            "B_render": terms.render,
            "W": bakeability.WRITES_IN_FLIGHT_BYTES,
            "Q": bakeability.CERTIFIED_QUERY_CAP,
            "resident_share": bakeability.BAKE_RESIDENT_SHARE,
        },
        "derivation": derivations,
        "derivation_with_campaigns_estate_render": literal_derivations,
        "p2": p2,
        "stop_rule": verdict,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"terms per row: {document['terms']}")
    for name, table in (
        ("population-wide render", derivations),
        ("with the Campaigns estate render", literal_derivations),
    ):
        for memory, derivation in table.items():
            for ceiling, row in derivation["candidates"].items():
                print(
                    f"  [{name}] {memory} MiB, V={ceiling}: fits {row['fits']}; binding "
                    f"{row['binding_row']}: {row['binding_terms']}"
                )
            print(f"  [{name}] {memory} MiB derives BAKE_MAX_ROWS={derivation['derived']}")
    if p2["ran"]:
        print(f"P2a parity {p2['p2a']['passed']}; P2c {p2['p2c']['outcome']}")
    print(f"stop rule: {verdict}")
    print(f"output: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
