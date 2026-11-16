# SPDX-License-Identifier: MIT-0
"""The job's `self-check` mode: the OQ-2 evidence (design §4b pass rule).

Adapted from the source engine's self-check (its promotion-plan case is dropped). It prints the
mounted artifact's roster digest, loads Chronos-2, forecasts fixed synthetic weekly series that
include NaN context weeks (OQ-18) twice in one process, and writes `self-check.json`. It passes
only when the weights load, every forecast has the declared shape (series x 13 x 3), every value
is finite, p10 <= p50 <= p90 in every week, and the two passes are identical.

It also watches, by code object, whether accelerate's `load_checkpoint_in_model` or
`load_checkpoint_and_dispatch` ever run during load or prediction (GHSA-4j2p-28q2-5m79's
vulnerable functions), and reports any call under `execute_path`.
"""

from __future__ import annotations

import importlib.metadata
import json
import math
import os
import platform
import sys
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import CodeType, FrameType
from typing import Any

from adapters.jobs.demand_forecast.artifact_digest import local_roster_entries
from adapters.jobs.demand_forecast.chronos2_model import Chronos2ForecastModel
from adapters.jobs.demand_forecast.job_log import JobLog
from adapters.jobs.demand_forecast.process_privileges import privilege_fields
from domain.forecasting.artifact_roster import roster_digest
from domain.forecasting.checks import check_forecast_shape, first_unordered_week
from domain.forecasting.models import (
    CHRONOS2_MODEL_ID,
    ForecastBatchRequest,
    QuantileForecast,
    SeriesHistory,
)
from ports.outbound.forecast_model import ForecastModelPort

HORIZON_WEEKS = 13
HISTORY_WEEKS = 104
WEEKS_PER_YEAR = 52
REPORT_NAME = "self-check.json"
# Week offsets (0-based, oldest first) given as NaN in each fixed series: a set gap, a single
# unknown week inside the holdout, and an unknown covariate week.
NAN_TARGET_WEEKS: tuple[frozenset[int], ...] = (
    frozenset({40, 41, 42}),
    frozenset({97}),
    frozenset(),
)
NAN_COVARIATE_WEEKS = frozenset({60})
WATCHED = (
    ("accelerate.utils.modeling", "load_checkpoint_in_model"),
    ("accelerate.big_modeling", "load_checkpoint_and_dispatch"),
)
VERSIONED = ("chronos-forecasting", "torch", "transformers", "accelerate", "safetensors")


def synthetic_series() -> tuple[SeriesHistory, ...]:
    """Three fixed weekly series (trend plus a yearly season) with NaN weeks. Synthetic only."""
    shapes = ((120.0, 30.0, 0.2), (40.0, 12.0, 0.05), (900.0, 150.0, 1.0))
    series: list[SeriesHistory] = []
    for index, (level, amplitude, trend) in enumerate(shapes):
        values = tuple(
            None
            if week in NAN_TARGET_WEEKS[index]
            else level + trend * week + amplitude * math.sin(2 * math.pi * week / WEEKS_PER_YEAR)
            for week in range(HISTORY_WEEKS)
        )
        views = tuple(
            None if week in NAN_COVARIATE_WEEKS else 25.0 * level + 3.0 * week
            for week in range(HISTORY_WEEKS)
        )
        series.append(
            SeriesHistory(
                canonical_asin=f"amazon_asin:b0selfck{index:02d}",
                target="units_ordered",
                values=values,
                covariate=views,
            )
        )
    return tuple(series)


@dataclass
class _Watch:
    codes: dict[CodeType, str] = field(default_factory=dict[CodeType, str])
    called: set[str] = field(default_factory=set[str])
    unavailable: list[str] = field(default_factory=list[str])


def _watched_codes() -> _Watch:
    watch = _Watch()
    for module_name, name in WATCHED:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            watch.unavailable.append(f"{module_name}.{name}")
            continue
        function: Any = getattr(module, name, None)
        code: Any = getattr(function, "__code__", None)
        if isinstance(code, CodeType):
            watch.codes[code] = f"{module_name}.{name}"
        else:
            watch.unavailable.append(f"{module_name}.{name}")
    return watch


@contextmanager
def watching(watch: _Watch) -> Generator[None]:
    """Record every call of a watched function's code object while the block runs."""

    def profile(frame: FrameType, event: str, _arg: object) -> None:
        if event == "call" and frame.f_code in watch.codes:
            watch.called.add(watch.codes[frame.f_code])

    previous = sys.getprofile()
    sys.setprofile(profile)
    threading.setprofile(profile)
    try:
        yield
    finally:
        sys.setprofile(previous)
        threading.setprofile(None)  # pyright: ignore[reportArgumentType]


@dataclass(frozen=True)
class SelfCheckRequest:
    """Where the artifact is mounted, where the report goes, and the engine's batch policy."""

    model_dir: Path
    output_dir: Path
    batch_size: int
    cross_learning: bool


@dataclass(frozen=True)
class SelfCheckReport:
    """The written report and whether it passed."""

    is_passed: bool
    body: dict[str, Any]


def _versions() -> dict[str, str]:
    found: dict[str, str] = {"python": platform.python_version()}
    for name in VERSIONED:
        try:
            found[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            found[name] = "absent"
    return found


def _check(forecasts: tuple[QuantileForecast, ...], count: int) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    if len(forecasts) != count:
        failures.append({"code": "FORECAST_COUNT", "detail": f"{len(forecasts)} of {count}"})
    for forecast in forecasts:
        shape = check_forecast_shape(forecast, HORIZON_WEEKS)
        if shape.reason is not None:
            failures.append({"code": shape.reason.value, "detail": shape.detail or ""})
            continue
        week = first_unordered_week(forecast.p10, forecast.p50, forecast.p90)
        if week is not None:
            failures.append({"code": "FORECAST_BAND_UNORDERED", "detail": f"week {week + 1}"})
    return failures


def _timed[T](call: Callable[[], T]) -> tuple[T, int]:
    started = time.monotonic()
    value = call()
    return value, round((time.monotonic() - started) * 1000)


def run_self_check(
    request: SelfCheckRequest,
    log: JobLog,
    load: Callable[[Path, int], ForecastModelPort] = Chronos2ForecastModel,
) -> SelfCheckReport:
    """Run the self-check and write `self-check.json`, pass or fail."""
    log.write(
        "INFO",
        "self_check.start",
        uid=os.getuid(),
        model_id=CHRONOS2_MODEL_ID,
        **privilege_fields(),
    )
    entries = local_roster_entries(request.model_dir)
    digest = roster_digest(entries)
    log.write("INFO", "self_check.artifact", artifact_digest=digest, artifact_files=len(entries))
    watch = _watched_codes()
    series = synthetic_series()
    body: dict[str, Any] = {
        "mode": "self-check",
        "model_id": CHRONOS2_MODEL_ID,
        "uid": os.getuid(),
        "artifact_digest": digest,
        "artifact_files": len(entries),
        "series_count": len(series),
        "horizon_weeks": HORIZON_WEEKS,
        "quantiles": [0.1, 0.5, 0.9],
        "nan_target_weeks": [sorted(weeks) for weeks in NAN_TARGET_WEEKS],
        "nan_covariate_weeks": sorted(NAN_COVARIATE_WEEKS),
        "cross_learning": request.cross_learning,
        "versions": _versions(),
    }
    durations: dict[str, int] = {}
    failures: list[dict[str, str]] = []
    passes: list[tuple[QuantileForecast, ...]] = []
    with watching(watch):
        try:
            model, durations["load"] = _timed(lambda: load(request.model_dir, request.batch_size))
            log.write("INFO", "self_check.model_loaded", duration_ms=durations["load"])
            batch = ForecastBatchRequest(series, HORIZON_WEEKS, request.cross_learning)
            for index in (1, 2):
                result, durations[f"predict_{index}"] = _timed(lambda: model.forecast(batch))
                passes.append(result.forecasts)
        # error-propagation: recorded in self-check.json failures, exit 1
        except Exception as error:
            failures.append({"code": "MODEL_SELF_CHECK_FAILED", "detail": type(error).__name__})
    identical = len(passes) == 2 and passes[0] == passes[1]  # noqa: PLR2004 - two passes
    if passes:
        failures.extend(_check(passes[0], len(series)))
    if len(passes) == 2 and not identical:  # noqa: PLR2004 - two passes
        failures.append({"code": "PASSES_DIFFER", "detail": "pass 2 differs from pass 1"})
    called = sorted(watch.called)
    body.update(
        {
            "weights_loaded": "load" in durations,
            "passes_identical": identical,
            "durations_ms": durations,
            "execute_path": {
                "watched": [f"{m}.{n}" for m, n in WATCHED],
                "unavailable": watch.unavailable,
                "called": called,
            },
            "failures": failures,
            "is_passed": not failures and identical,
            "sample_p50_first_series": list(passes[0][0].p50[:3]) if passes else [],
        }
    )
    request.output_dir.mkdir(parents=True, exist_ok=True)
    (request.output_dir / REPORT_NAME).write_text(json.dumps(body, indent=2, sort_keys=True))
    log.write(
        "INFO",
        "self_check.result",
        is_passed=bool(body["is_passed"]),
        series_count=len(series),
        passes_identical=identical,
        failure_codes=sorted({f["code"] for f in failures}),
        watched_called=called,
        durations_ms=durations,
    )
    return SelfCheckReport(bool(body["is_passed"]), body)
