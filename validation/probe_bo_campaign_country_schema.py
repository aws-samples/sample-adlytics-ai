# SPDX-License-Identifier: MIT-0
"""P-BO-1 follow-up: is `dsp_impressions.campaign_country` resolvable on the Sandbox? One fact.

The Sandbox rejected Q3 (the whole pack 5 text, which reads the column through
`EXTEND_TIME_WINDOW`) with "Column 'campaign_country' not found in any table". A broad rejected
query can't settle a column-level fact, so this probe reads only that one column, controls on,
over Q3's week:

1. `plain`: `COUNT(campaign_country)` from `dsp_impressions`. A rejection naming the column
   settles that the column is absent from the Sandbox schema.
2. `extended`: the same read through `EXTEND_TIME_WINDOW('dsp_impressions', 'P0D', 'P14D')`,
   the table function Q3 uses. Submitted only when `plain` SUCCEEDED, to separate "absent from
   the table" from "not resolvable through the table function".

The output is one aggregate count; no country value is read out. Recovery-first: both forms go
through `amc_recovery`, so a re-run adopts the journal or history before it can submit.

    set -a; source validation/.env.local; set +a
    uv run python validation/probe_bo_campaign_country_schema.py \
      --from <Monday>T00:00:00 --to <next Monday>T00:00:00
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
from collections.abc import Callable, Sequence
from typing import cast

import amc_recovery
from bo_probe_sql import QUERIES
from probe_bo_suppression_exactness import validate_window

FINDINGS = pathlib.Path(__file__).parent / "findings"
COLUMN = "campaign_country"
_SELECT = """
SELECT COUNT(campaign_country) AS campaign_country_rows,
       BUILT_IN_PARAMETER('TIME_WINDOW_START') AS time_window_start,
       BUILT_IN_PARAMETER('TIME_WINDOW_END') AS time_window_end
"""
FORMS = {
    "plain": f"{_SELECT}FROM dsp_impressions\n",
    "extended": f"{_SELECT}FROM TABLE(EXTEND_TIME_WINDOW('dsp_impressions', 'P0D', 'P14D'))\n",
}
WINDOW_RULE = {"plain": "Q0", "extended": "Q3"}
"""Each form takes the window rule of the query it stands beside: one ISO week, and for the
table-function form the completed 14-day extension Q3 needs."""
RESOLVES, NOT_FOUND, INCONCLUSIVE = "resolves", "not_found", "inconclusive"


def spec(form: str) -> amc_recovery.ExecutionSpec:
    """Return the recovery identity for one form; controls on, no parameters."""
    return amc_recovery.ExecutionSpec(
        name=form,
        sql=FORMS[form],
        controls_off=False,
        workflow_prefix="probe-bo-cc",
        stem_prefix="bo-cc",
    )


def classify(status: str, reason: str) -> str:
    """Return whether the column resolved in this form, was named as not found, or neither."""
    if status == "SUCCEEDED":
        return RESOLVES
    text = reason.lower()
    if status == "REJECTED" and f"'{COLUMN}'" in text and "not found" in text:
        return NOT_FOUND
    return INCONCLUSIVE


def conclude(plain: str, extended: str | None) -> dict[str, str]:
    """Combine the two forms into the one schema-level statement they support."""
    if plain == NOT_FOUND:
        return {"conclusion": "absent_from_sandbox_schema", "q3_rejection": "explained"}
    if plain != RESOLVES:
        return {"conclusion": INCONCLUSIVE, "q3_rejection": "unexplained"}
    if extended == NOT_FOUND:
        return {
            "conclusion": "not_resolvable_through_extend_time_window",
            "q3_rejection": "explained",
        }
    if extended == RESOLVES:
        return {"conclusion": "resolvable_both_forms", "q3_rejection": "unexplained"}
    return {"conclusion": "present_on_plain_table", "q3_rejection": "unexplained"}


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
    today: dt.date | None = None,
) -> int:
    """Recover or submit `plain`, then `extended` only if `plain` resolved; write one summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="window_start", required=True)
    parser.add_argument("--to", dest="window_end", required=True)
    parser.add_argument("--recover-since")
    args = parser.parse_args(argv)
    window = (str(args.window_start), str(args.window_end))
    current = today or dt.datetime.now(tz=dt.UTC).date()
    for form in FORMS:
        validate_window(QUERIES[WINDOW_RULE[form]], *window, today=current)
    api, instance_id = connector()
    since = str(
        args.recover_since
        or (dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    )
    outcomes: dict[str, dict[str, str]] = {}
    for form, sql in FORMS.items():
        if form == "extended" and outcomes["plain"]["outcome"] != RESOLVES:
            break
        plan = amc_recovery.resolve_execution(
            amc_recovery.ExecutionRequest(
                api=api,
                instance_id=instance_id,
                instance_type="SANDBOX",
                spec=spec(form),
                window_start=window[0],
                window_end=window[1],
                recover_since=since,
                findings=findings,
            )
        )
        terminal = amc_recovery.collect_terminal(api, plan, byte_limit=100_000)
        outcomes[form] = {
            "terminal_status": terminal.status,
            "status_reason": terminal.reason,
            "sql_digest": amc_recovery.sql_digest(sql),
            "outcome": classify(terminal.status, terminal.reason),
        }
        print(f"  {form:8s} status={terminal.status} outcome={outcomes[form]['outcome']}")
    extended = outcomes.get("extended", {}).get("outcome")
    result = {
        "instance_type": "SANDBOX",
        "evidence_class": "sandbox",
        "window_start": window[0],
        "window_end": window[1],
        "forms": outcomes,
        **conclude(outcomes["plain"]["outcome"], extended),
    }
    stamp = dt.datetime.now(tz=dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    findings.mkdir(parents=True, exist_ok=True)
    (findings / f"bo-cc-verdict-{stamp}.json").write_text(json.dumps(result, indent=2))
    print(f"conclusion: {result['conclusion']}")
    return 2 if result["conclusion"] == INCONCLUSIVE else 0


if __name__ == "__main__":
    sys.exit(main())
