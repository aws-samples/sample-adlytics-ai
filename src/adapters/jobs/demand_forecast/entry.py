# SPDX-License-Identifier: MIT-0
"""Entry point of the demand-forecast container, run as a network-isolated Processing job.

Adapted from the source engine's inference entry. Slice 1 item A4 ships the `self-check` mode
(design §4b, OQ-2); the `forecast` mode arrives with the job's forecast path. Callers may rely
on: exit 0 means the mode passed; any other exit writes the typed error envelope to
`/opt/ml/output/message` (SageMaker's `ExitMessage`), with the code and the cause chain's class
names only. Logs are JSON lines on stdout matching `job_log_schema.json`.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from adapters.jobs.demand_forecast.cause_chain import cause_chain
from adapters.jobs.demand_forecast.job_log import JobLog, structured_error
from adapters.jobs.demand_forecast.self_check import SelfCheckRequest, run_self_check
from adapters.outbound.demand_forecast_policy import load_policy

EXIT_MESSAGE_PATH = Path("/opt/ml/output/message")
# The image copies the repository's paths under /opt/forecast, so the policy sits at the same
# relative path in the image and in a checkout.
POLICY_PATH = Path(__file__).resolve().parents[4] / "contracts/forecasting/demand-forecast.yaml"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="demand-forecast")
    modes = parser.add_subparsers(dest="mode", required=True)
    check = modes.add_parser("self-check")
    check.add_argument("--model", type=Path, required=True)
    check.add_argument("--output", type=Path, required=True)
    check.add_argument("--run-id", default="self-check")
    return parser


def _write_exit_message(message: str) -> None:
    try:
        EXIT_MESSAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        EXIT_MESSAGE_PATH.write_text(message, encoding="utf-8")
    except OSError:
        # Outside SageMaker (a local run) the path may not be writable; the log line still has it.
        return


def main(argv: list[str] | None = None) -> int:
    """Run one mode; return its exit code."""
    arguments = _parser().parse_args(argv)
    log = JobLog(correlation_id=str(arguments.run_id))
    started = time.monotonic()
    code = "MODEL_SELF_CHECK_FAILED"
    try:
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
        if report.is_passed:
            return 0
        _write_exit_message(structured_error(code, (), str(arguments.run_id)))
    # error-propagation: job ERROR log line and typed exit message, exit 1
    except Exception as error:
        chain = cause_chain(error)
        log.write(
            "ERROR",
            "job",
            mode=str(arguments.mode),
            duration_ms=round((time.monotonic() - started) * 1000),
            error={"code": "FORECAST_JOB_CRASHED", "cause": list(chain)},
        )
        _write_exit_message(structured_error("FORECAST_JOB_CRASHED", chain, str(arguments.run_id)))
    return 1


if __name__ == "__main__":
    sys.exit(main())
