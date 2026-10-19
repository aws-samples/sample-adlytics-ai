# SPDX-License-Identifier: MIT-0
"""Does AMC null a *dimension* column we map into a curated grain, and what does the flag do?

One question, asked because it decides a schema. Every AMC contract maps `campaign_id_string`
(threshold LOW, bar of 2 distinct users) into `campaign_key`, which is declared NOT NULL and sits
in the grain of all three AMC facts. If AMC can return that column as NULL, the row fails our row
model and the whole partition quarantines — and ADR-019's design depends on those rows landing,
because the withheld residual is what makes a coarse total exact.

**Amazon's own documentation already answers half of it.** The aggregation-threshold page's worked
example returns 4 rows carrying purchases with a blank `postal_code`, "omitted from the output
because there were fewer than 100 users" [verified 2026-08-21]. So a dimension does come back null
and the metric survives.

What that example does *not* show, because it never sets the discriminator:

1. Whether a **LOW** column nulls the same way a HIGH one does. Our contracts map only NONE and
   LOW columns, so this is the case that matters to us and the example uses HIGH.
2. What the **discriminator** changes. amc.md section 5 says it adds re-aggregation and a flag;
   the worked example's four null rows are unmerged (75, 52, 41, 15), which is consistent with
   nulling-without-merging when the option is absent.
3. Whether the three option **field names** are right. They are documented as workflow options but
   the API reference page is JS-rendered and could not be read, so a FAILED execution naming an
   unknown field is itself a result.

Run:

    set -a; source validation/.env.local; set +a
    uv run python validation/probe_suppression_nulls.py

Writes JSON and the raw CSV to `findings/`, which is gitignored — fold anything learned into
`docs/sources/amc.md` and `RESULTS.md`, per the README.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import uuid
from typing import Any

from amc_client import AdsApi, AmcError, Credentials, fetch_csv

FINDINGS = pathlib.Path(__file__).parent / "findings"

# Ends three days back, following AMC's own guidance to query a window ending at least 48 hours
# ago, and wide enough that a low-volume grain still has rows to suppress.
_END = dt.datetime.now(tz=dt.UTC).date() - dt.timedelta(days=3)
_START = _END - dt.timedelta(days=14)
WINDOW_START = f"{_START.isoformat()}T00:00:00"
WINDOW_END = f"{_END.isoformat()}T00:00:00"

SUPPRESSION = ("values_removed", "filtered_reason", "distinct_users_used")
"""The three option columns, named as our contracts name them.

The names are ours to choose — they are workflow options rather than table columns — so using the
same names the contracts use means the probe's output is directly comparable to a fixture.
"""

CANDIDATES: tuple[tuple[str, str, str], ...] = (
    # (table, HIGH column, metric). A HIGH column forces the 100-user bar, which is the only
    # reliable way to make the sandbox suppress — a LOW column needs a genuinely 1-user row and
    # synthetic data may contain none. `campaign_id_string` (LOW) rides alongside in every case,
    # because the question is whether it nulls too or survives.
    #
    # Tried in order because the schema is not what the registry says. `city_name` is recorded on
    # `dsp_impressions` in `contracts/shared/amc-columns.yaml` and the live instance rejected it
    # with "Column 'city_name' not found in any table" [2026-08-21] — so the candidates start with
    # the table and column from Amazon's own worked example on the aggregation-threshold page.
    ("amazon_attributed_events_by_conversion_time", "postal_code", "purchases"),
    ("amazon_attributed_events_by_conversion_time", "city_name", "purchases"),
    ("dsp_impressions", "postal_code", "impressions"),
    ("dsp_clicks", "postal_code", "clicks"),
)


def _sql(table: str, high_column: str, metric: str) -> str:
    """One grouped query selecting a LOW column beside a HIGH one."""
    # A-SQL-001: names only from CANDIDATES; see docs/security-dispositions.md
    return f"""
        SELECT campaign_id_string,
               {high_column},
               SUM({metric}) AS {metric}
        FROM {table}
        GROUP BY 1, 2
    """  # nosec B608


def _credentials() -> Credentials:
    """Read credentials from the environment, the way the README's step four does."""
    import os

    missing = [
        name
        for name in ("AMC_CLIENT_ID", "AMC_CLIENT_SECRET", "AMC_REFRESH_TOKEN", "AMC_REGION")
        if not os.environ.get(name)
    ]
    if missing:
        raise SystemExit(
            f"missing {', '.join(missing)}. Run: set -a; source validation/.env.local; set +a"
        )
    return Credentials(
        client_id=os.environ["AMC_CLIENT_ID"],
        client_secret=os.environ["AMC_CLIENT_SECRET"],
        refresh_token=os.environ["AMC_REFRESH_TOKEN"],
        region=os.environ["AMC_REGION"],
        marketplace_id=os.environ.get("AMC_MARKETPLACE_ID", ""),
        amc_account_id=os.environ.get("AMC_ACCOUNT_ID", ""),
        instance_id=os.environ.get("AMC_INSTANCE_ID", ""),
    )


def _read(
    api: AdsApi, instance_id: str, sql: str, *, with_flag: bool
) -> dict[str, Any]:
    """Run the probe once, with or without the discriminator, and return what came back."""
    label = "with_discriminator" if with_flag else "without_discriminator"
    workflow_id = f"probe-suppression-nulls-{label}-{uuid.uuid4().hex[:8]}"
    print(f"\n=== {label} ===")
    print(f"    workflow: {workflow_id}")

    try:
        execution_id = api.execute_adhoc(
            instance_id=instance_id,
            workflow_id=workflow_id,
            sql=sql,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            suppression_columns=SUPPRESSION if with_flag else None,
        )
    except AmcError as error:
        print(f"    SUBMIT FAILED: {error}")
        return {"label": label, "outcome": "submit_failed", "error": str(error)}

    print(f"    execution: {execution_id} — polling (server-side, may take ~15 min)")
    execution = api.wait_for_execution(instance_id=instance_id, execution_id=execution_id)
    status = str(execution.get("status", "")).upper()
    print(f"    status: {status}")
    if status != "SUCCEEDED":
        message = execution.get("statusMessage") or execution.get("message") or ""
        print(f"    message: {message}")
        return {
            "label": label,
            "outcome": status,
            "message": str(message),
            "execution": execution,
        }

    urls = api.download_urls(instance_id=instance_id, execution_id=execution_id)
    if not urls:
        return {"label": label, "outcome": "no_download_url", "execution": execution}

    csv_text = fetch_csv(urls[0])
    (FINDINGS / f"suppression-nulls-{label}.csv").write_text(csv_text)
    return {
        "label": label,
        "outcome": "SUCCEEDED",
        "row_count": max(len(csv_text.strip().splitlines()) - 1, 0),
        "header": csv_text.splitlines()[0] if csv_text.strip() else "",
        "csv_head": "\n".join(csv_text.splitlines()[:12]),
    }


def _summarise(csv_text: str, *, high_column: str) -> dict[str, Any]:
    """Count the shapes that answer the question, from the delivered CSV."""
    lines = [line for line in csv_text.splitlines() if line.strip()]
    if len(lines) < 2:
        return {"rows": 0}
    header = [name.strip() for name in lines[0].split(",")]
    rows = [dict(zip(header, line.split(","), strict=False)) for line in lines[1:]]

    def blank(row: dict[str, str], column: str) -> bool:
        return not (row.get(column) or "").strip()

    return {
        "rows": len(rows),
        "header": header,
        "high_column_null": sum(1 for r in rows if blank(r, high_column)),
        "campaign_null": sum(1 for r in rows if blank(r, "campaign_id_string")),
        "both_null": sum(
            1 for r in rows if blank(r, high_column) and blank(r, "campaign_id_string")
        ),
        "flagged": sum(1 for r in rows if (r.get("values_removed") or "").strip().lower() == "true")
        if "values_removed" in header
        else None,
        "reasons": sorted(
            {(r.get("filtered_reason") or "").strip() for r in rows if (r.get("filtered_reason") or "").strip()}
        )
        if "filtered_reason" in header
        else None,
    }


def main() -> int:
    """Run the probe both ways and write the findings."""
    FINDINGS.mkdir(exist_ok=True)
    credentials = _credentials()
    api = AdsApi(credentials)

    instance_id = credentials.instance_id
    if not instance_id:
        raise SystemExit("AMC_INSTANCE_ID is blank. Run probe_amc.py --discover-only first.")

    print(f"instance: {instance_id}")
    print(f"window:   {WINDOW_START} .. {WINDOW_END}")

    # Find a candidate the instance actually accepts. REJECTED is terminal and arrives in seconds,
    # so probing the schema this way is cheap — it was not, before that status was handled.
    chosen: tuple[str, str, str] | None = None
    rejected: list[dict[str, Any]] = []
    for table, high_column, metric in CANDIDATES:
        print(f"\n--- trying {table}.{high_column} ---")
        attempt = _read(api, instance_id, _sql(table, high_column, metric), with_flag=False)
        attempt["candidate"] = f"{table}.{high_column}"
        if attempt["outcome"] == "SUCCEEDED":
            chosen = (table, high_column, metric)
            rejected.append(attempt)
            break
        rejected.append(attempt)

    if chosen is None:
        (FINDINGS / "suppression-nulls.json").write_text(
            json.dumps({"window": [WINDOW_START, WINDOW_END], "results": rejected}, indent=2)
        )
        print("\nNo candidate validated. The geo columns may be absent from this instance.")
        for attempt in rejected:
            print(f"  {attempt['candidate']}: {attempt['outcome']} — {attempt.get('message', '')[:140]}")
        return 1

    table, high_column, metric = chosen
    print(f"\nchosen: {table}.{high_column}")
    results = [*rejected, _read(api, instance_id, _sql(table, high_column, metric), with_flag=True)]

    for result in results:
        path = FINDINGS / f"suppression-nulls-{result['label']}.csv"
        if path.exists() and result["outcome"] == "SUCCEEDED":
            result["summary"] = _summarise(path.read_text(), high_column=high_column)

    out = FINDINGS / "suppression-nulls.json"
    out.write_text(json.dumps({"window": [WINDOW_START, WINDOW_END], "results": results}, indent=2))
    print(f"\nwrote {out}")
    for result in results:
        print(f"\n{result['label']}: {result['outcome']}")
        if "summary" in result:
            print(json.dumps(result["summary"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
