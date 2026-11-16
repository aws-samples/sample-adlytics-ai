# SPDX-License-Identifier: MIT-0
"""Entry point of the demand-forecast container, run as a network-isolated Processing job.

Adapted from the source engine's inference entry. Two modes:

- `self-check` (design §4b, OQ-2): load Chronos-2 from the mount and check two passes;
- `forecast` (design §4b): one shard attempt. In order: the manifest and every Parquet file are
  validated (`INPUT_MANIFEST_INVALID`, `INPUT_SCHEMA_MISMATCH`, `INPUT_SHARD_MISMATCH`); the
  manifest's run and build attempt must be the arguments' (`INPUT_ATTEMPT_MISMATCH`); every run
  identity component the job can know must equal the manifest's, and the manifest's run
  fingerprint must recompute from them (`RUN_IDENTITY_MISMATCH`, see `check_run_identity`); the
  mounted artifact's digest must equal the run identity's before the model loads
  (`MODEL_ARTIFACT_MISMATCH`); then the weekly series, the run and the candidate, series first and
  the run report last. The run binds Chronos-2 and the statsmodels ETS comparator.

Callers may rely on: exit 0 means the mode passed; any other exit writes the typed error envelope
to `/opt/ml/output/message` (SageMaker's `ExitMessage`), with the code and the cause chain's class
names only. Logs are JSON lines on stdout matching `job_log_schema.json`.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import resource
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from adapters.jobs.demand_forecast.artifact_digest import local_roster_entries
from adapters.jobs.demand_forecast.cause_chain import cause_chain
from adapters.jobs.demand_forecast.chronos2_model import Chronos2ForecastModel
from adapters.jobs.demand_forecast.ets_statsmodels import StatsmodelsStateSpaceEts
from adapters.jobs.demand_forecast.job_log import JobLog, structured_error
from adapters.jobs.demand_forecast.json_output import JsonCandidateOutput
from adapters.jobs.demand_forecast.parquet_input import ParquetForecastInput
from adapters.jobs.demand_forecast.process_privileges import privilege_fields
from adapters.jobs.demand_forecast.self_check import SelfCheckRequest, run_self_check
from adapters.outbound.demand_forecast_policy import LoadedPolicy, load_policy
from domain.forecasting.artifact_roster import roster_digest
from domain.forecasting.boundary import InputManifestV1, RunReportV1, boundary_schema_digests
from domain.forecasting.comparators import EtsRequest, EtsResult
from domain.forecasting.fingerprint import RunIdentity, run_fingerprint
from domain.forecasting.models import (
    CHRONOS2_MODEL_ID,
    ForecastBatchRequest,
    ForecastBatchResult,
)
from domain.forecasting.run import ForecastRunError, ForecastRunRequest, run_forecast
from domain.forecasting.weekly_series import WeeklySeriesRequest, build_weekly_series
from ports.outbound.forecast_input import ForecastInputError

EXIT_MESSAGE_PATH = Path("/opt/ml/output/message")
# The image copies the repository's paths under /opt/forecast, so the policy and the lock sit at
# the same relative paths in the image and in a checkout.
POLICY_PATH = Path(__file__).resolve().parents[4] / "contracts/forecasting/demand-forecast.yaml"
LOCK_PATH = Path(__file__).with_name("requirements.lock")


class ForecastJobError(Exception):
    """A deterministic refusal of the forecast mode; `code` is its failure category."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="demand-forecast")
    modes = parser.add_subparsers(dest="mode", required=True)
    check = modes.add_parser("self-check")
    check.add_argument("--model", type=Path, required=True)
    check.add_argument("--output", type=Path, required=True)
    check.add_argument("--run-id", default="self-check")
    forecast = modes.add_parser("forecast")
    forecast.add_argument("--run-id", required=True)
    forecast.add_argument("--shard", type=int, required=True)
    forecast.add_argument("--attempt-id", required=True)
    forecast.add_argument("--input-attempt-id", required=True)
    forecast.add_argument("--processor-code-identity", required=True)
    forecast.add_argument("--input", type=Path, required=True)
    forecast.add_argument("--model", type=Path, required=True)
    forecast.add_argument("--output", type=Path, required=True)
    return parser


def _write_exit_message(message: str) -> None:
    try:
        EXIT_MESSAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        EXIT_MESSAGE_PATH.write_text(message, encoding="utf-8")
    except OSError:
        # Outside SageMaker (a local run) the path may not be writable; the log line still has it.
        return


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


@dataclass(frozen=True)
class ForecastModeRequest:
    """The forecast mode's arguments."""

    run_id: str
    shard: int
    attempt_id: str
    input_attempt_id: str
    processor_code_identity: str
    """The image asset hash the execution launched, passed by the state machine."""
    input_dir: Path
    model_dir: Path
    output_dir: Path


class _TimedModel:
    """The model's `forecast`, timed, as the domain's `BatchForecaster`."""

    def __init__(self, call: Callable[[ForecastBatchRequest], ForecastBatchResult]) -> None:
        self._call = call
        self.elapsed_ms = 0

    def __call__(self, request: ForecastBatchRequest, /) -> ForecastBatchResult:
        started = time.monotonic()
        try:
            return self._call(request)
        finally:
            self.elapsed_ms += _elapsed_ms(started)


class _TimedEts:
    """The ETS adapter's `fit_forecast`, timed, as the domain's `EtsForecaster`.

    Its total is the run report's `ets` duration, the ETS time per ASIN that approval C-1 bounds.
    """

    def __init__(self, call: Callable[[EtsRequest], EtsResult]) -> None:
        self._call = call
        self.elapsed_ms = 0

    def __call__(self, request: EtsRequest, /) -> EtsResult:
        started = time.monotonic()
        try:
            return self._call(request)
        finally:
            self.elapsed_ms += _elapsed_ms(started)


def check_run_identity(
    manifest: InputManifestV1, processor_code_identity: str, loaded: LoadedPolicy
) -> None:
    """Refuse a manifest whose run identity names other code, policy, lock or schemas.

    Compared with what this job runs: the processor-code identity (the state machine passes the
    image asset hash it launched; the image can't hold its own hash, which is computed over the
    image's inputs), the policy file's SHA-256 and section versions, the lock digest, the model ID
    and the boundary schema digests. Then the manifest's run fingerprint must recompute from its
    run identity, so the fingerprint a report carries names exactly what computed it. The artifact
    digest is checked separately, against the mount (`MODEL_ARTIFACT_MISMATCH`).
    """
    identity = manifest.run_identity
    schemas = boundary_schema_digests()
    expected = {
        "processor_code_identity": processor_code_identity,
        "policy_sha256": loaded.sha256,
        "policy_section_versions": loaded.section_versions,
        "lock_digest": hashlib.sha256(LOCK_PATH.read_bytes()).hexdigest(),
        "model_id": CHRONOS2_MODEL_ID,
        "boundary_schema_digests": schemas,
    }
    differ = [name for name, value in expected.items() if getattr(identity, name) != value]
    if manifest.boundary_schema_digests != schemas:
        differ.append("manifest.boundary_schema_digests")
    recomputed = run_fingerprint(
        RunIdentity(
            input_fingerprint=identity.input_fingerprint,
            policy_sha256=identity.policy_sha256,
            processor_code_identity=identity.processor_code_identity,
            lock_digest=identity.lock_digest,
            model_id=identity.model_id,
            model_artifact_digest=identity.model_artifact_digest,
            boundary_schema_digests=identity.boundary_schema_digests,
            instance_type=identity.instance_type,
            max_runtime_seconds=identity.max_runtime_seconds,
        )
    )
    if recomputed != manifest.run_fingerprint:
        differ.append("run_fingerprint")
    if differ:
        raise ForecastJobError("RUN_IDENTITY_MISMATCH", ", ".join(differ))


def run_forecast_mode(request: ForecastModeRequest, log: JobLog) -> RunReportV1:
    """Run one shard attempt end to end; raise a typed error on any refusal."""
    durations: dict[str, int] = {}
    log.write(
        "INFO",
        "forecast.start",
        uid=os.getuid(),
        **privilege_fields(),
        model_id=CHRONOS2_MODEL_ID,
        shard=request.shard,
        attempt_id=request.attempt_id,
        input_attempt_id=request.input_attempt_id,
    )
    started = time.monotonic()
    shard = ParquetForecastInput(request.input_dir).read_shard(request.shard)
    manifest = shard.manifest
    if (manifest.run_id, manifest.build_attempt_id) != (request.run_id, request.input_attempt_id):
        raise ForecastJobError(
            "INPUT_ATTEMPT_MISMATCH", "the manifest names another run or build attempt"
        )
    durations["input"] = _elapsed_ms(started)
    log.write(
        "INFO",
        "forecast.input",
        target_rows=len(shard.targets),
        traffic_rows=len(shard.traffic),
        duration_ms=durations["input"],
    )
    loaded = load_policy(POLICY_PATH)
    check_run_identity(manifest, request.processor_code_identity, loaded)
    policy = loaded.policy
    started = time.monotonic()
    entries = local_roster_entries(request.model_dir)
    digest = roster_digest(entries)
    durations["artifact_digest"] = _elapsed_ms(started)
    log.write("INFO", "forecast.artifact", artifact_digest=digest, artifact_files=len(entries))
    if digest != manifest.run_identity.model_artifact_digest:
        raise ForecastJobError(
            "MODEL_ARTIFACT_MISMATCH", "the mounted artifact differs from the run identity"
        )
    started = time.monotonic()
    series = build_weekly_series(
        WeeklySeriesRequest(
            input_end=manifest.input_end,
            history_weeks=policy.eligibility.history_weeks_max,
            sales_coverage=manifest.coverage.sales,
            traffic_coverage=manifest.coverage.traffic,
            target_rows=shard.targets,
            traffic_rows=shard.traffic,
            missing_weeks=policy.missing_weeks,
        )
    )
    durations["series"] = _elapsed_ms(started)
    started = time.monotonic()
    model = Chronos2ForecastModel(request.model_dir, policy.engine.batch_size)
    durations["model_load"] = _elapsed_ms(started)
    timed = _TimedModel(model.forecast)
    timed_ets = _TimedEts(StatsmodelsStateSpaceEts().fit_forecast)
    started = time.monotonic()
    result = run_forecast(
        ForecastRunRequest(
            shard_index=request.shard,
            forecast_set=manifest.forecast_set,
            series=series,
            policy=policy,
            forecaster=timed,
            ets=timed_ets,
        )
    )
    durations["run"] = _elapsed_ms(started)
    durations["model"] = timed.elapsed_ms
    durations["ets"] = timed_ets.elapsed_ms
    started = time.monotonic()
    output = JsonCandidateOutput(request.output_dir)
    written = output.write_series(result.candidates)
    durations["write_series"] = _elapsed_ms(started)
    report = RunReportV1(
        schema_version=1,
        run_id=request.run_id,
        shard_index=request.shard,
        asin_count=len(result.candidates),
        attempt_id=request.attempt_id,
        input_attempt_id=request.input_attempt_id,
        series_sha256=written.series_sha256,
        counts_by_reason=result.counts_by_reason,
        week_state_counts=result.week_state_counts,
        inferred_zero_days=result.inferred_zero_days,
        durations_ms=durations,
        policy_section_versions=loaded.section_versions,
        policy_sha256=loaded.sha256,
        image_identity=request.processor_code_identity,
        lock_digest=hashlib.sha256(LOCK_PATH.read_bytes()).hexdigest(),
        model_id=model.model_id,
        model_artifact_digest=digest,
        boundary_schema_digests=boundary_schema_digests(),
        run_fingerprint=manifest.run_fingerprint,
    )
    output.write_report(report)
    log.write(
        "INFO",
        "forecast.result",
        asin_count=report.asin_count,
        counts_by_reason=result.counts_by_reason,
        model_calls=result.model_calls,
        durations_ms=durations,
        series_sha256=written.series_sha256,
        # P1-1's peak memory, recorded by the job itself; Linux reports ru_maxrss in KiB.
        peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
    )
    return report


def _self_check(arguments: argparse.Namespace, log: JobLog) -> bool:
    policy = load_policy(POLICY_PATH).policy
    report = run_self_check(
        SelfCheckRequest(
            model_dir=arguments.model,
            output_dir=arguments.output,
            batch_size=policy.engine.batch_size,
            cross_learning=policy.engine.cross_learning,
        ),
        log,
    )
    return report.is_passed


def main(argv: list[str] | None = None) -> int:
    """Run one mode; return its exit code."""
    arguments = _parser().parse_args(argv)
    run_id = str(arguments.run_id)
    log = JobLog(correlation_id=run_id)
    started = time.monotonic()
    try:
        if arguments.mode == "self-check":
            if _self_check(arguments, log):
                return 0
            _write_exit_message(structured_error("MODEL_SELF_CHECK_FAILED", (), run_id))
            return 1
        run_forecast_mode(
            ForecastModeRequest(
                run_id=run_id,
                shard=int(arguments.shard),
                attempt_id=str(arguments.attempt_id),
                input_attempt_id=str(arguments.input_attempt_id),
                processor_code_identity=str(arguments.processor_code_identity),
                input_dir=arguments.input,
                model_dir=arguments.model,
                output_dir=arguments.output,
            ),
            log,
        )
        return 0
    except (ForecastInputError, ForecastRunError, ForecastJobError) as error:
        code = error.code
        chain = cause_chain(error)
    # error-propagation: job ERROR log line and typed exit message, exit 1
    except Exception as error:
        code = "FORECAST_JOB_CRASHED"
        chain = cause_chain(error)
    log.write(
        "ERROR",
        "job",
        mode=str(arguments.mode),
        duration_ms=_elapsed_ms(started),
        error={"code": code, "cause": list(chain)},
    )
    _write_exit_message(structured_error(code, chain, run_id))
    return 1


if __name__ == "__main__":
    sys.exit(main())
