# SPDX-License-Identifier: MIT-0
"""P-BO parameter shape: test whether an ad hoc execution accepts `workflow.inputParameters`.

One disputed fact. AMC documents `inputParameters` on saved workflows and `parameterValues` on
executions, but not the ad hoc form. This probe sends one STRING parameter declared on the ad
hoc `workflow` object, with no default, and its value in `parameterValues`, on a SANDBOX with
controls on. It passes only if the execution SUCCEEDS and the output carries the supplied value
(so neither a default nor a literal could explain it). The value is a fixed, non-identifying
marker.

    set -a; source validation/.env.local; set +a
    uv run python validation/probe_bo_parameter_shape.py \
      --from <Monday>T00:00:00 --to <next Monday>T00:00:00
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import pathlib
import sys
from collections.abc import Callable, Sequence
from typing import cast

import amc_recovery

FINDINGS = pathlib.Path(__file__).parent / "findings"
MARKER_NAME = "probe_marker"
MARKER_VALUE = "pbo-shape-probe"
SQL = """
SELECT CUSTOM_PARAMETER('probe_marker') AS marker,
       COUNT(DISTINCT user_id) AS users,
       BUILT_IN_PARAMETER('TIME_WINDOW_START') AS time_window_start,
       BUILT_IN_PARAMETER('TIME_WINDOW_END') AS time_window_end
FROM dsp_impressions
"""
FORM = "workflow.inputParameters + execution parameterValues"
SPEC = amc_recovery.ExecutionSpec(
    name="shape",
    sql=SQL,
    controls_off=False,
    workflow_prefix="probe-bo-param",
    stem_prefix="bo-param",
    parameter_values=((MARKER_NAME, MARKER_VALUE),),
)


def classify(status: str, csv_text: str | None) -> dict[str, object]:
    """Return the shape outcome: confirmed only when the supplied value comes back."""
    if status != "SUCCEEDED" or csv_text is None:
        return {"outcome": "not_confirmed", "reason": f"terminal {status}"}
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    markers = {row.get("marker", "") for row in rows}
    if markers == {MARKER_VALUE}:
        return {"outcome": "confirmed", "form": FORM}
    return {"outcome": "not_confirmed", "reason": "the supplied value did not come back"}


def connect() -> tuple[amc_recovery.ProbeApi, str]:
    """Authenticate from the sourced environment and re-discover the SANDBOX."""
    from amc_client import AdsApi, credentials_from_environment
    from probe_amc_grain import discover

    api = AdsApi(credentials_from_environment())
    return cast("amc_recovery.ProbeApi", api), discover(api, "SANDBOX")


def main(
    argv: Sequence[str] | None = None,
    *,
    findings: pathlib.Path = FINDINGS,
    connector: Callable[[], tuple[amc_recovery.ProbeApi, str]] = connect,
) -> int:
    """Recover or submit the one shape execution and classify it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="window_start", required=True)
    parser.add_argument("--to", dest="window_end", required=True)
    parser.add_argument("--recover-since")
    args = parser.parse_args(argv)
    api, instance_id = connector()
    since = str(
        args.recover_since
        or (dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    )
    plan = amc_recovery.resolve_execution(
        amc_recovery.ExecutionRequest(
            api=api,
            instance_id=instance_id,
            instance_type="SANDBOX",
            spec=SPEC,
            window_start=str(args.window_start),
            window_end=str(args.window_end),
            recover_since=since,
            findings=findings,
            allow_parameters=True,
        )
    )
    terminal = amc_recovery.collect_terminal(api, plan, byte_limit=100_000)
    result = {
        "terminal_status": terminal.status,
        "status_reason": terminal.reason,
        **classify(terminal.status, terminal.csv_text),
    }
    (findings / f"{plan.stem}.summary.json").write_text(json.dumps(result, indent=2))
    print(f"shape: status={terminal.status} outcome={result['outcome']}")
    return 0 if result["outcome"] == "confirmed" else 1


if __name__ == "__main__":
    sys.exit(main())
