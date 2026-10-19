# SPDX-License-Identifier: MIT-0
"""Paid-feature presence: whether each ASI, ARP and `conversions_all` table and column resolves.

Source validation for the paid tables our contracts, packs and the business-outcomes design read
(`paid_feature: true` in `contracts/shared/amc-columns.yaml`): `amazon_retail_purchases` (ARP;
`amc-retail-purchases` and pack 15), the ASI snapshot tables packs 10 and 11 read,
`segment_metadata` (pack 13), and `conversions_all`. Enrolment is per customer, so an absent
table is recorded as "not available on this instance", never as a defect: the packs keep a
standard-table path.

One fact per execution, controls on, read-only aggregate output:

- `table` probes read one column with `COUNT(...)`. `SUCCEEDED` settles that the table and that
  column resolve. `REJECTED` naming the table settles the table is unavailable on this instance
  (no subscription, or absent). `REJECTED` naming the column settles a schema mismatch.
- `columns` probes read every column the packs use. A `SUCCEEDED` settles the table and every
  listed column. A rejection naming the table settles that it is unavailable. A rejection naming
  one column settles only that column; the rest need a one-column follow-up. A `columns` probe
  with `requires` runs only after that table probe resolved.

Recovery-first: each run re-discovers the instance, then adopts a journaled or historical
execution before it can submit. Raw output stays in ignored `findings/`.

    set -a; source validation/.env.local; set +a
    uv run python validation/probe_amc_paid_feature_schema.py \
      --probe arp_table --instance-type STANDARD \
      --from 2026-09-01T00:00:00 --to 2026-10-01T00:00:00
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import cast

import amc_recovery

FINDINGS = pathlib.Path(__file__).parent / "findings"
STEM_PREFIX = "paid-schema"
WORKFLOW_PREFIX = "probe-paid-schema"
_WINDOW = (
    "BUILT_IN_PARAMETER('TIME_WINDOW_START') AS time_window_start,\n"
    "       BUILT_IN_PARAMETER('TIME_WINDOW_END') AS time_window_end"
)


@dataclass(frozen=True, slots=True)
class Probe:
    """One paid-feature read: the table, the columns it names, and its kind."""

    name: str
    table: str
    columns: tuple[str, ...]
    kind: str
    """`table` (one column, settles presence) or `columns` (every column the packs read)."""
    requires: str | None = None
    """The `table` probe that must have resolved first."""

    @property
    def sql(self) -> str:
        """Aggregate-only SQL; `user_id` is counted, never selected."""
        reads = ",\n       ".join(
            f"COUNT(DISTINCT {column}) AS {column}_distinct"
            if column in {"user_id", "purchase_id"}
            else f"COUNT({column}) AS {column}_rows"
            for column in self.columns
        )
        # A-SQL-001: table and column names only from PROBES below.
        return f"SELECT {reads},\n       {_WINDOW}\nFROM {self.table}\n"  # nosec B608


_ASI_COLUMNS = (
    "segment_id",
    "segment_marketplace_id",
    "segment_name",
    "snapshot_datetime",
    "user_id",
    "user_id_type",
    "no_3p_trackers",
)
PROBES = {
    probe.name: probe
    for probe in (
        Probe("arp_table", "amazon_retail_purchases", ("purchase_units_sold",), "table"),
        Probe(
            "arp_columns",
            "amazon_retail_purchases",
            # `amc-retail-purchases` (shipped) plus pack 15's cohort join columns.
            (
                "purchase_date_utc",
                "asin",
                "marketplace_name",
                "purchase_units_sold",
                "currency_code",
                "unit_price",
                "purchase_id",
                "user_id",
                "user_id_type",
            ),
            "columns",
            requires="arp_table",
        ),
        Probe(
            "asi_inmarket_snapshot",
            "audience_segments_amer_inmarket_snapshot",
            ("segment_id",),
            "table",
        ),
        Probe(
            "asi_lifestyle_snapshot",
            "audience_segments_amer_lifestyle_snapshot",
            ("segment_id",),
            "table",
        ),
        Probe(
            "asi_inmarket_snapshot_columns",
            "audience_segments_amer_inmarket_snapshot",
            _ASI_COLUMNS,
            "columns",
            requires="asi_inmarket_snapshot",
        ),
        # Under the 14-execution cap these two read every column at once, without a one-column
        # table read first: a SUCCEEDED settles the table and every column, a rejection naming
        # the table settles that it is unavailable, and one naming a column settles only that
        # column, as for any `columns` probe.
        Probe(
            "asi_lifestyle_snapshot_columns",
            "audience_segments_amer_lifestyle_snapshot",
            _ASI_COLUMNS,
            "columns",
        ),
        Probe("segment_metadata", "segment_metadata", ("segment_id",), "table"),
        Probe(
            "segment_metadata_columns",
            "segment_metadata",
            (
                "segment_id",
                "segment_marketplace_id",
                "segment_name",
                "segment_description",
                "category_level_1",
                "category_level_2",
                "category_path",
                "no_3p_trackers",
            ),
            "columns",
        ),
        Probe("conversions_all", "conversions_all", ("exposure_type",), "table"),
    )
}
RESOLVES, TABLE_UNAVAILABLE, COLUMN_NOT_FOUND, INCONCLUSIVE = (
    "resolves",
    "table_unavailable",
    "column_not_found",
    "inconclusive",
)


def classify(probe: Probe, status: str, reason: str) -> dict[str, str]:
    """Return the one fact a terminal status supports, naming the column when one is named."""
    if status == "SUCCEEDED":
        return {"outcome": RESOLVES}
    text = reason.lower()
    if status == "REJECTED":
        named = re.search(r"column '([a-z0-9_]+)' not found", text)
        if named:
            return {"outcome": COLUMN_NOT_FOUND, "column": named.group(1)}
        if probe.table in text:
            return {"outcome": TABLE_UNAVAILABLE}
    return {"outcome": INCONCLUSIVE}


def spec(probe: Probe) -> amc_recovery.ExecutionSpec:
    """Recovery identity for one probe; controls on, no parameters."""
    return amc_recovery.ExecutionSpec(
        name=probe.name.replace("_", "-"),
        sql=probe.sql,
        controls_off=False,
        workflow_prefix=WORKFLOW_PREFIX,
        stem_prefix=STEM_PREFIX,
    )


def _prior_outcome(findings: pathlib.Path, name: str, instance_type: str) -> str | None:
    paths = sorted(findings.glob(f"{STEM_PREFIX}-verdict-{instance_type.lower()}-{name}-*.json"))
    if not paths:
        return None
    return str(cast("dict[str, object]", json.loads(paths[-1].read_text())).get("outcome"))


def connect(instance_type: str) -> tuple[amc_recovery.ProbeApi, str]:
    """Authenticate from the sourced environment and re-discover an instance of the type."""
    from amc_client import AdsApi, credentials_from_environment
    from probe_amc_grain import discover

    api = AdsApi(credentials_from_environment())
    return cast("amc_recovery.ProbeApi", api), discover(api, instance_type)


def main(
    argv: Sequence[str] | None = None,
    *,
    findings: pathlib.Path = FINDINGS,
    connector: Callable[[str], tuple[amc_recovery.ProbeApi, str]] = connect,
) -> int:
    """Recover or submit one probe, poll it to a terminal state, and write its verdict."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", choices=tuple(PROBES), required=True)
    parser.add_argument("--instance-type", choices=("SANDBOX", "STANDARD"), required=True)
    parser.add_argument("--from", dest="window_start", required=True)
    parser.add_argument("--to", dest="window_end", required=True)
    parser.add_argument("--recover-since")
    args = parser.parse_args(argv)
    probe = PROBES[str(args.probe)]
    instance_type = str(args.instance_type)
    if probe.requires and _prior_outcome(findings, probe.requires, instance_type) != RESOLVES:
        raise SystemExit(f"{probe.name} runs only after {probe.requires} resolved")
    api, instance_id = connector(instance_type)
    since = str(
        args.recover_since
        or (dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    )
    plan = amc_recovery.resolve_execution(
        amc_recovery.ExecutionRequest(
            api=api,
            instance_id=instance_id,
            instance_type=instance_type,
            spec=spec(probe),
            window_start=str(args.window_start),
            window_end=str(args.window_end),
            recover_since=since,
            findings=findings,
        )
    )
    terminal = amc_recovery.collect_terminal(api, plan, byte_limit=100_000)
    verdict = {
        "probe": probe.name,
        "kind": probe.kind,
        "table": probe.table,
        "columns": list(probe.columns),
        "instance_type": instance_type,
        "evidence_class": "sandbox" if instance_type == "SANDBOX" else "validation_instance",
        "execution_source": plan.source,
        "terminal_status": terminal.status,
        "status_reason": terminal.reason,
        "sql_digest": amc_recovery.sql_digest(probe.sql),
        **classify(probe, terminal.status, terminal.reason),
    }
    stamp = dt.datetime.now(tz=dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"{STEM_PREFIX}-verdict-{instance_type.lower()}-{probe.name}-{stamp}.json"
    (findings / name).write_text(json.dumps(verdict, indent=2))
    print(
        f"{probe.name}: status={terminal.status} outcome={verdict['outcome']} source={plan.source}"
    )
    return 0 if verdict["outcome"] != INCONCLUSIVE else 2


if __name__ == "__main__":
    sys.exit(main())
