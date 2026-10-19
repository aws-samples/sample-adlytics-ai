# SPDX-License-Identifier: MIT-0
"""Probe 7 · whether AMC can deliver a contract's projection exactly as the contract describes it.

One question, asked because it decides whether an end-to-end ingestion test can use real AMC
data at all: does the projection the contract declares compile, and do the values it returns
satisfy the assertions a load runs?

**The suppression half is already settled and is not what this asks.** A landed fixture in dev
carries a row with every grain column NULL, `rows_suppressed=true` and a populated
`suppression_reason`, and it loads because `split_withheld` moves withheld rows out before
validation (ADR-034). AMC nulling a grain column is designed for.

What is genuinely open, in the order it would break a load:

1. **Admissibility.** `REJECTED` is a compile failure carrying no `statusMessage`, so the whole
   projection either works or says nothing about why it does not. Narrow it column by column if
   this comes back rejected — RESULTS.md records that lesson twice.

2. **`campaign_country`.** The contract maps it to `marketplace_key` and marks it
   `[unverified]`: "Whether campaign_country is populated for every DSP campaign is
   [unverified] … an unresolvable value quarantines the partition." The dev fixture puts
   `ATVPDKIKX0DER` in it, which is a marketplace *id*, while the contract says the value
   resolves against `dim_marketplace` on the **country_code** namespace. Those cannot both be
   right, so a real value is the only thing that settles which.

3. **`currency_iso_code`.** One currency column for the fact (ADR-021). Whether a single
   campaign-day row ever carries more than one is what `check_currency_agreement` exists for.

The projection below is transcribed from `contracts/amc/amc-dsp-impressions.yaml`, and the
aliases are not cosmetic: `delivered_name` says a derived or extraction-option column arrives
under its **target** name, so `reach_users` and the three suppression columns must be aliased or
`check_header` refuses the delivery. The names were read back off the landed fixture's own
header rather than inferred.

Static SQL with no parameters. The rule against string-templating SQL is about interpolating
values, and there are none here.

Run:

    set -a; source validation/.env.local; set +a
    uv run python validation/probe_amc_grain.py

Writes the result and the raw CSV to `findings/`, which is gitignored. Anything learned belongs
in `RESULTS.md`, `docs/sources/amc.md` and the contract.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
import uuid

from amc_client import AdsApi, AmcError, Credentials, fetch_csv

FINDINGS = pathlib.Path(__file__).parent / "findings"

SUPPRESSION = ("rows_suppressed", "suppression_reason", "distinct_user_count")
"""The three option columns, named as `amc-dsp-impressions.yaml`'s suppression block names them.

Ours to choose, because they are workflow options rather than table columns — and they must match
the contract exactly, since `check_header` reads `contract.suppression.all_columns` and quarantines
on anything missing.
"""

PROJECTIONS: dict[str, str] = {
    # `amc-dsp-impressions`. REJECTED on SANDBOX with "Column 'campaign_country' not found in any
    # table", and on STANDARD it compiled far enough to enumerate seven underlying data lakes —
    # which it can only do after resolving every column. So the column exists on an advertiser
    # instance and is absent from the sandbox's synthetic schema. The contract is right and the
    # sandbox cannot exercise it.
    "amc-dsp-impressions": """
        SELECT impression_date_utc,
               campaign_id_string,
               campaign_country,
               SUM(impressions) AS impressions,
               SUM(total_cost) AS total_cost,
               SUM(impression_cost) AS impression_cost,
               currency_iso_code,
               COUNT(DISTINCT user_id) AS reach_users
        FROM dsp_impressions
        GROUP BY 1, 2, 3, 7
    """,
    # `amc-reach-frequency`. Every column is one the registry records on `sponsored_ads_traffic`,
    # and `marketplace_name` is the column its own note calls "the usable marketplace column".
    # `ad_product_type` is selected once and mapped to both `ad_product_key` and `channel_key`.
    "amc-reach-frequency": """
        SELECT event_date_utc,
               ad_product_type,
               campaign_id_string,
               marketplace_name,
               SUM(impressions) AS impressions,
               SUM(clicks) AS clicks,
               SUM(spend) AS spend,
               currency_iso_code,
               COUNT(DISTINCT user_id) AS reach_users
        FROM sponsored_ads_traffic
        GROUP BY 1, 2, 3, 4, 8
    """,
}
"""One projection per contract, transcribed from the contract and verified against the header of
that contract's own landed fixture rather than inferred."""

DAYS_BACK = 3
"""AMC's own guidance is a window ending at least 48 hours ago. Three days is the margin the
earlier probes used and the one that has returned rows."""

WINDOW_DAYS = 1
"""A single day, because that is the fixed period a scheduled pull should use.

A wider window would return more rows and prove less: overlapping windows land as separate live
partitions rather than superseding, so a one-day period is both the smallest useful delivery and
the shape the schedule has to adopt.
"""


def _credentials() -> Credentials:
    """Read credentials from the environment, the way the README's step four does."""
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


def discover(api: AdsApi, wanted_type: str) -> str:
    """Adopt the account's identity and return an instance of the given type.

    Two-phase because it has to be: `/amc/instances` returns `400 entityId provided is null`
    without an entity id, and the entity id is what `/amc/accounts` returns. Recorded in
    `docs/sources/amc.md` section 8, learned by hitting it.

    `.env.local` carries no account, marketplace or instance id — all three are empty — so
    discovery is not a convenience here, it is the only way to reach an instance.
    """
    accounts = api.call("GET", "/amc/accounts")
    listed = accounts.get("amcAccounts") or []
    if not listed:
        raise SystemExit("GET /amc/accounts returned no accounts; the token cannot reach AMC")
    first = listed[0]
    api.adopt_amc_identity(
        account_id=str(first.get("accountId", "")),
        marketplace_id=str(first.get("marketplaceId", "")),
    )
    print(f"account  : {first.get('accountName', '?')} ({first.get('accountId', '')})")

    instances = api.call("GET", "/amc/instances", amc_headers=True).get("instances") or []
    for instance in instances:
        if str(instance.get("instanceType", "")).upper() == wanted_type:
            return str(instance.get("instanceId", ""))
    raise SystemExit(f"no {wanted_type} instance among {[i.get('instanceId') for i in instances]}")


def main() -> int:
    """Run the contract's own projection and report what came back."""
    FINDINGS.mkdir(exist_ok=True)
    credentials = _credentials()

    contract_id = next((arg for arg in sys.argv[1:] if arg in PROJECTIONS), "amc-reach-frequency")
    sql = PROJECTIONS[contract_id]

    end = dt.datetime.now(tz=dt.UTC).date() - dt.timedelta(days=DAYS_BACK)
    start = end - dt.timedelta(days=WINDOW_DAYS - 1)
    workflow_id = f"probe-amc-grain-{uuid.uuid4().hex[:8]}"

    api = AdsApi(credentials)
    # SANDBOX by default: both instances sit in the same internal demo account so neither carries
    # an advertiser's real data, and `execute_adhoc` sets `requireSyntheticData` unconditionally.
    #
    # `--standard` exists because sandbox schema coverage is *not* the same as an advertiser
    # instance's — RESULTS.md records the sandbox coverage table omitting ARP entirely. So a
    # "column not found" here is only evidence about the sandbox until the same query is tried on
    # STANDARD, and confusing the two would condemn a contract for the test environment's gap.
    wanted = "STANDARD" if "--standard" in sys.argv else "SANDBOX"
    instance_id = os.environ.get("AMC_INSTANCE_ID") or discover(api, wanted)

    print(f"instance : {instance_id}")
    print(f"contract : {contract_id}")
    print(f"window   : {start.isoformat()} .. {end.isoformat()}")
    print(f"workflow : {workflow_id}")

    try:
        execution_id = api.execute_adhoc(
            instance_id=instance_id,
            workflow_id=workflow_id,
            sql=sql,
            # AMC's window is a half-open interval on timestamps, so a single calendar day ends
            # at the start of the next one. `period_end` in the manifest stays the inclusive day.
            window_start=f"{start.isoformat()}T00:00:00",
            window_end=f"{(end + dt.timedelta(days=1)).isoformat()}T00:00:00",
            suppression_columns=SUPPRESSION,
        )
    except AmcError as error:
        print(f"\nsubmission failed:\n{error}")
        return 1

    # Printed before the wait, because a client that dies mid-poll otherwise loses the only handle
    # on work AMC has already done. RESULTS.md records this costing two full runs.
    print(f"execution: {execution_id}")

    execution = api.wait_for_execution(instance_id=instance_id, execution_id=execution_id)
    status = str(execution.get("status", "")).upper()
    print(f"status   : {status}")
    if status != "SUCCEEDED":
        print(f"reason   : {execution.get('statusReason') or '(none given)'}")
        (FINDINGS / f"{workflow_id}.json").write_text(json.dumps(execution, indent=2))
        return 1

    urls = api.download_urls(instance_id=instance_id, execution_id=execution_id)
    if not urls:
        print("no download urls")
        return 1

    csv_text = fetch_csv(urls[0])
    (FINDINGS / f"{workflow_id}.csv").write_text(csv_text)

    lines = csv_text.splitlines()
    print(f"\nheader   : {lines[0] if lines else '(empty)'}")
    print(f"rows     : {max(len(lines) - 1, 0)}")
    for line in lines[1:9]:
        print(f"           {line}")
    print(f"\nwritten  : findings/{workflow_id}.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
