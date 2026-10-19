# SPDX-License-Identifier: MIT-0
"""Settle the disputed entries in contracts/shared/amc-columns.yaml against live AMC.

Every probe below exists to convert one `status: disputed` entry, or one open item in
docs/sources/amc.md section 10, into a verified fact. Nothing here explores. If a
probe cannot change what the registry says, it does not belong in this file.

Aggregation controls are left ON for every probe. That is the whole point: a question
about whether a column may be selected only means something under the rules a real
advertiser instance enforces. Turning them off would answer a different question.

Usage:

    # Credentials from Secrets Manager, the intended path.
    uv run python validation/probe_amc.py \
        --secret example/AMC_SECRET_NAME --profile <profile>

    # Or, before the secret exists, from the environment. Local only.
    AMC_CLIENT_ID=... AMC_CLIENT_SECRET=... AMC_REFRESH_TOKEN=... AMC_REGION=na \
        uv run python validation/probe_amc.py --from-env

Run discovery on its own first; it prints the instance IDs and account IDs the probes
need, and tells you whether the instance is a sandbox or a production advertiser
instance:

    uv run python validation/probe_amc.py --from-env --discover-only
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from amc_client import API_HOSTS, TOKEN_HOSTS, AdsApi, AmcError, Credentials, fetch_csv

FINDINGS = Path(__file__).parent / "findings"


@dataclass(frozen=True)
class Probe:
    """One question, the query that answers it, and what the answer changes."""

    name: str
    settles: str
    sql: str
    reading: str


# The window sits well inside the sandbox's 13 months of backfill and ends a few days
# back, because AMC's own guidance is to query a window ending 48 hours before now.
_END = dt.datetime.now(tz=dt.UTC).date() - dt.timedelta(days=3)
_START = _END - dt.timedelta(days=7)
WINDOW_START = f"{_START.isoformat()}T00:00:00"
WINDOW_END = f"{_END.isoformat()}T00:00:00"


PROBES: tuple[Probe, ...] = (
    Probe(
        name="name_columns_outputtable",
        settles=(
            "15 disputed entries: whether `advertiser` and `campaign` are INTERNAL "
            "(amc.md section 4, researched) or LOW (seven data-source pages, read "
            "2026-08-19). One query settles all of them. SETTLED 2026-08-19: LOW."
        ),
        # Derived rather than copied from Amazon's published examples: playbook and
        # sample SQL licensing is unresolved (amc.md section 10) and the guidance is to
        # derive. Ordinal GROUP BY because that is the documented form.
        sql="""
            SELECT campaign_id_string, campaign, advertiser, SUM(impressions) AS impressions
            FROM dsp_impressions
            GROUP BY 1, 2, 3
        """,
        reading=(
            "SUCCEEDED with campaign and advertiser present => LOW, outputtable. "
            "Mark both LOW in the registry, drop `status: disputed`, and correct "
            "amc.md section 4 and section 9 — a third-party campaign-metadata source "
            "is then unnecessary. FAILED naming those columns => INTERNAL confirmed."
        ),
    ),
    Probe(
        name="dsp_views_event_type_values",
        settles=(
            "dsp_views.event_type: amc.md section 3 records VIEWABLE_IMP, the page "
            "enumerates MEASURABLE_IMP, VIEW, UNMEASURABLE_IMP, SYNTHETIC_VIEW."
        ),
        sql="""
            SELECT event_type, SUM(events) AS events
            FROM dsp_views
            GROUP BY 1
        """,
        reading=(
            "The returned value set is authoritative. A filter written against the "
            "wrong literal returns zero rows rather than erroring, which is why this "
            "is worth a query rather than a guess."
        ),
    ),
    Probe(
        name="conversions_all_money_scale",
        settles=(
            "3 disputed entries: conversions_all types combined_sales, "
            "off_amazon_product_sales and off_amazon_conversion_value as LONG where "
            "the sibling pages type them DECIMAL(38,4). Blocks amc-organic-vs-ad."
        ),
        # total_product_sales is documented as a decimal on every page, so it is the
        # control. If combined_sales comes back ~1e5 or ~1e8 times larger for a
        # comparable row set, it is a scaled integer.
        sql="""
            SELECT purchase_currency,
                   SUM(total_product_sales) AS total_product_sales,
                   SUM(combined_sales) AS combined_sales,
                   SUM(off_amazon_product_sales) AS off_amazon_product_sales,
                   SUM(total_units_sold) AS units
            FROM conversions_all
            GROUP BY 1
        """,
        reading=(
            "Compare combined_sales against total_product_sales. Same order of "
            "magnitude => local currency, divisor 1. Larger by 1e5 or 1e8 => a scaled "
            "integer and the divisor is that factor. Also sanity-check "
            "total_product_sales / units against a plausible unit price."
        ),
    ),
    Probe(
        name="money_scale_control",
        settles=(
            "The highest-consequence unverified claim in the AMC set: that total_cost "
            "and impression_cost are millicents (divisor 100000) while every fee "
            "column and bid_price are microcents (divisor 100000000) — one table, two "
            "scales (amc.md section 6). A wrong divisor is a 1000x error on ad_spend "
            "and on every ROAS built from it, and it looks entirely plausible."
        ),
        # Deliberately narrow. Three columns that certainly exist, so this cannot fail
        # for a reason unrelated to the question — the lesson of the two probes above,
        # both of which returned FAILED or REJECTED with no message.
        sql="""
            SELECT SUM(impressions) AS impressions,
                   SUM(total_cost) AS total_cost,
                   SUM(impression_cost) AS impression_cost
            FROM dsp_impressions
        """,
        reading=(
            "Compute total_cost / impressions * 1000 under each candidate divisor. "
            "Only one yields a plausible CPM: divisor 100000 gives dollars, and being "
            "wrong by 1000 gives either ~$0.003 or ~$3,000, neither of which any "
            "advertiser pays. That settles the millicent claim on its own. Then compare "
            "impression_cost against total_cost — strictly smaller is consistent with "
            "it excluding fees, equal is ambiguous and needs the identity probe below."
        ),
    ),
    Probe(
        name="impression_cost_fee_identity",
        settles=(
            "impression_cost's `excludes fees` claim, [unverified] and downgraded "
            "2026-08-19, which is the only thing blocking a spend_media_only metric. "
            "Settles both divisors at once as a side effect, because the identity "
            "balances only if each side is scaled correctly."
        ),
        # Raw sums rather than the arithmetic. Computing the identity in SQL would embed
        # the divisor assumption being tested, so the comparison belongs in `reading`.
        # Separate from the probe above precisely because it names six more columns: if
        # any one of them is absent or thresholded the query dies, and the divisor
        # answer must survive that.
        sql="""
            SELECT SUM(total_cost) AS total_cost,
                   SUM(impression_cost) AS impression_cost,
                   SUM(supply_cost) AS supply_cost,
                   SUM(platform_fee) AS platform_fee,
                   SUM(audience_fee) AS audience_fee,
                   SUM(third_party_fees) AS third_party_fees,
                   SUM(managed_service_fee) AS managed_service_fee
            FROM dsp_impressions
        """,
        reading=(
            "Test whether total_cost - impression_cost equals the fee sum once both "
            "sides are in one unit: fees are microcents and the cost columns are "
            "millicents, so divide the fee sum by 1000 before comparing. Balancing "
            "means impression_cost genuinely excludes fees AND both divisors are "
            "correct — mark the claim [verified] and declare spend_media_only. Not "
            "balancing means one of the two is wrong, and the ratio of the discrepancy "
            "to the fee sum says which. All fees zero means the synthetic data carries "
            "none, which settles nothing and needs a production instance."
        ),
    ),
    Probe(
        name="winning_bid_cost_exists",
        settles=(
            "amc.md section 10: winning_bid_cost is absent from the dsp_impressions "
            "schema page but used by the live media-performance playbook and cited by "
            "the aggregation-threshold page. The docs contradict each other."
        ),
        sql="""
            SELECT SUM(winning_bid_cost) AS winning_bid_cost, SUM(impressions) AS impressions
            FROM dsp_impressions
        """,
        reading=(
            "SUCCEEDED => the column exists and the schema page is stale; record it "
            "with divisor 100000 and threshold MEDIUM, and remove it from "
            "`do_not_model`. FAILED on an unknown column => the playbook is stale and "
            "`do_not_model` stands."
        ),
    ),
    Probe(
        name="arp_available_in_sandbox",
        settles=(
            "The sandbox coverage table omits amazon_retail_purchases entirely, so it "
            "is unclear whether ARP is absent or merely undocumented. ARP is our only "
            "source of parent ASIN and brand and our only reconciliation anchor."
        ),
        sql="""
            SELECT marketplace_name, SUM(purchase_units_sold) AS units
            FROM amazon_retail_purchases
            GROUP BY 1
        """,
        reading=(
            "SUCCEEDED => ARP is queryable here, and the 26-vs-27 column count and "
            "the missing-revenue-column finding can be settled without a production "
            "instance. FAILED on an unknown table => those stay unverified and need a "
            "real advertiser instance."
        ),
    ),
)


def load_credentials(args: argparse.Namespace) -> Credentials:
    """From Secrets Manager, or from the environment for a first local run.

    The environment path exists because the secret cannot be filled in until discovery
    has run, and discovery needs credentials. It is not a pattern to copy into src/:
    `make check-config-access` enforces that only adapters/config.py reads the
    environment, and it scans src/ only, which is why this is allowed to live here.
    """
    if args.from_env:
        missing = [
            name
            for name in ("AMC_CLIENT_ID", "AMC_CLIENT_SECRET", "AMC_REFRESH_TOKEN", "AMC_REGION")
            if not os.environ.get(name)
        ]
        if missing:
            raise SystemExit(f"missing environment variables: {', '.join(missing)}")
        return Credentials(
            client_id=os.environ["AMC_CLIENT_ID"],
            client_secret=os.environ["AMC_CLIENT_SECRET"],
            refresh_token=os.environ["AMC_REFRESH_TOKEN"],
            region=os.environ["AMC_REGION"],
            marketplace_id=os.environ.get("AMC_MARKETPLACE_ID") or None,
            amc_account_id=os.environ.get("AMC_ACCOUNT_ID") or None,
            instance_id=os.environ.get("AMC_INSTANCE_ID") or None,
        )

    if not args.secret or not args.profile:
        raise SystemExit("--secret and --profile are both required unless --from-env is used")

    import boto3  # imported here so --from-env needs no AWS session at all

    session = boto3.Session(profile_name=args.profile)
    payload = json.loads(
        session.client("secretsmanager").get_secret_value(SecretId=args.secret)["SecretString"]
    )
    return Credentials(
        client_id=payload["client_id"],
        client_secret=payload["client_secret"],
        refresh_token=payload["refresh_token"],
        region=payload["region"],
        marketplace_id=payload.get("marketplace_id") or None,
        amc_account_id=payload.get("amc_account_id") or None,
        instance_id=payload.get("instance_id") or None,
    )


def discover(api: AdsApi) -> dict[str, Any]:
    """Find out what this token can actually reach.

    Two-phase on purpose. `/amc/instances` rejects a request with no entity ID, and the
    entity ID is what `/amc/accounts` returns — so the account call runs bare, its result
    is adopted, and only then can instances be listed.

    Deliberately tolerant: each step reports its own failure and the walk continues.
    Knowing that `/amc/accounts` works while `/amc/instances` is empty is a completely
    different diagnosis from the token being wrong.
    """
    found: dict[str, Any] = {}

    def attempt(label: str, path: str, *, amc_headers: bool = False) -> Any:
        try:
            result = api.call("GET", path, amc_headers=amc_headers)
            print(f"  {label:16s} OK")
            return result
        except (AmcError, ValueError) as error:
            body = error.body if isinstance(error, AmcError) else str(error)
            status = error.status if isinstance(error, AmcError) else "-"
            found[label] = {"error": status, "body": body}
            print(f"  {label:16s} HTTP {status}")
            print(f"                   {body[:400]}")
            return None

    accounts = attempt("amc_accounts", "/amc/accounts")
    if accounts is not None:
        found["amc_accounts"] = accounts
        listed = accounts.get("amcAccounts") or []
        if listed:
            first = listed[0]
            account_id = str(first.get("accountId", ""))
            marketplace_id = str(first.get("marketplaceId", ""))
            api.adopt_amc_identity(account_id=account_id, marketplace_id=marketplace_id)
            print(
                f"  {'adopted':16s} accountId={account_id} "
                f"marketplaceId={marketplace_id} ({first.get('accountName', '?')})"
            )
        else:
            print(f"  {'adopted':16s} none — amcAccounts is empty, so nothing to query")

    instances = attempt("amc_instances", "/amc/instances", amc_headers=True)
    if instances is not None:
        found["amc_instances"] = instances
        for instance in instances.get("instances") or []:
            print(
                f"  {'instance':16s} id={instance.get('instanceId')} "
                f"type={instance.get('instanceType')} "
                f"name={instance.get('instanceName')!r}"
            )

    profiles = attempt("ads_profiles", "/v2/profiles")
    if profiles is not None:
        found["ads_profiles"] = profiles
        if isinstance(profiles, list) and not profiles:
            print(
                f"  {'note':16s} no sponsored-ads profiles: this is an AMC-only account, "
                "so DSP and sponsored-ads campaign data may be absent"
            )

    return found


def run_probe(api: AdsApi, instance_id: str, probe: Probe) -> dict[str, Any]:
    """Submit, wait for a terminal status, and read the result if there is one."""
    print(f"\n=== {probe.name} ===")
    print(f"settles: {probe.settles}")
    result: dict[str, Any] = {"probe": probe.name, "settles": probe.settles, "sql": probe.sql}
    try:
        execution_id = api.execute_adhoc(
            instance_id=instance_id,
            workflow_id=f"validation-{probe.name}",
            sql=probe.sql,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
        print(f"  submitted: {execution_id}")
        execution = api.wait_for_execution(instance_id=instance_id, execution_id=execution_id)
        status = str(execution.get("status", "")).upper()
        result["status"] = status
        result["execution"] = execution
        print(f"  status: {status}")

        if status == "SUCCEEDED":
            urls = api.download_urls(instance_id=instance_id, execution_id=execution_id)
            result["rows"] = [fetch_csv(url) for url in urls]
            for csv in result["rows"]:
                print("  --- result ---")
                print("\n".join(f"  {line}" for line in csv.splitlines()[:15]))
        else:
            # The failure message is the finding for a probe testing legality.
            print(f"  message: {execution.get('statusMessage') or execution}")
    except (AmcError, TimeoutError, ValueError) as error:
        result["status"] = "ERROR"
        result["error"] = str(error)
        print(f"  ERROR: {error}")

    print(f"  how to read this: {probe.reading}")
    return result


def print_requests(credentials: Credentials, instance_id: str | None) -> None:
    """Print every request this would issue, without issuing any of them.

    Exists because several request details here are documented but unverified against a
    live instance, and a refresh token needs a human at a consent screen. This is the
    most that can be checked without one: the shapes can be read against Amazon's docs,
    or pasted into curl, before a token is spent on discovering a typo.

    Secrets are redacted. The point is the shape, not the values.
    """
    token_host = TOKEN_HOSTS[credentials.region]
    api_host = API_HOSTS[credentials.region]
    marker = "<REDACTED>"
    account = credentials.amc_account_id or "<amc_account_id, from GET /amc/accounts>"
    marketplace = credentials.marketplace_id or "<marketplace_id>"
    target = instance_id or "<instance_id, from GET /amc/instances>"

    print("1. Mint an access token\n")
    print(f"   POST {token_host}/auth/o2/token")
    print("   Content-Type: application/x-www-form-urlencoded")
    print(
        f"     grant_type=refresh_token&refresh_token={marker}"
        f"&client_id={credentials.client_id}&client_secret={marker}\n"
    )

    print("2. Discovery — no AMC headers, because these are what discover them\n")
    for path in ("/amc/accounts", "/amc/instances", "/v2/profiles"):
        print(f"   GET {api_host}{path}")
        print(f"     Authorization: Bearer {marker}")
        print(f"     Amazon-Advertising-API-ClientId: {credentials.client_id}\n")

    print("3. Probes — AMC headers required from here on\n")
    for probe in PROBES:
        body = {
            "workflow": {
                "workflowId": f"validation-{probe.name}",
                "sqlQuery": " ".join(probe.sql.split()),
            },
            "timeWindowStart": WINDOW_START,
            "timeWindowEnd": WINDOW_END,
            "timeWindowType": "EXPLICIT",
            "timeWindowTimeZone": "UTC",
            "requireSyntheticData": True,
            "disableAggregationControls": False,
        }
        print(f"   --- {probe.name} ---")
        print(f"   POST {api_host}/amc/reporting/{target}/workflowExecutions")
        print(f"     Authorization: Bearer {marker}")
        print(f"     Amazon-Advertising-API-ClientId: {credentials.client_id}")
        print(f"     Amazon-Advertising-API-AdvertiserId: {account}")
        print(f"     Amazon-Advertising-API-MarketplaceId: {marketplace}")
        print(f"   {json.dumps(body, indent=2)}\n")

    print("   then poll until terminal, because a 200 on submit means accepted, not compiled:")
    print(f"   GET {api_host}/amc/reporting/{target}/workflowExecutions/<executionId>")
    print("   then, on SUCCEEDED:")
    print(
        f"   GET {api_host}/amc/reporting/{target}"
        "/workflowExecutions/<executionId>/downloadUrls\n"
    )


def main() -> int:
    """Discover what the credentials reach, then run every probe against it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secret", help="Secrets Manager secret name")
    parser.add_argument("--profile", help="AWS profile. Required with --secret, no default.")
    parser.add_argument("--from-env", action="store_true", help="Read credentials from env instead")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument(
        "--print-requests",
        action="store_true",
        help="Print every request without sending any. Needs no valid token.",
    )
    parser.add_argument("--instance-id", help="Overrides the value in the secret or env")
    args = parser.parse_args()

    try:
        credentials = load_credentials(args)
    except (ValueError, KeyError) as error:
        # A legible failure is the point of this directory; a traceback for a bad
        # region or a missing key in the secret is noise.
        print(f"credentials are not usable: {error}", file=sys.stderr)
        return 1

    print(f"region: {credentials.region}")
    print(f"window: {WINDOW_START} to {WINDOW_END}\n")

    if args.print_requests:
        print_requests(credentials, args.instance_id or credentials.instance_id)
        return 0

    try:
        api = AdsApi(credentials)
    except AmcError as error:
        print(f"could not mint an access token: {error}", file=sys.stderr)
        # These two are easy to conflate and point at different fixes. Observed
        # empirically against the live endpoint, not guessed.
        print(
            "\n  invalid_client  -> client_id or client_secret is wrong\n"
            "  invalid_grant   -> the refresh token is wrong or revoked, OR the region is\n"
            "                     wrong. Region follows the advertiser's marketplace, not\n"
            "                     an AWS region: na | eu | fe.\n",
            file=sys.stderr,
        )
        return 1

    print("discovery:")
    discovered = discover(api)

    FINDINGS.mkdir(exist_ok=True)
    stamp = dt.datetime.now(tz=dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    output = FINDINGS / f"{stamp}-amc-probe.json"
    results: list[dict[str, Any]] = []

    instance_id = args.instance_id or credentials.instance_id
    if args.discover_only or not instance_id:
        if not instance_id and not args.discover_only:
            print(
                "\nNo instance id. Read it out of amc_instances above, then re-run with "
                "--instance-id or add it to the secret."
            )
        output.write_text(json.dumps({"discovery": discovered}, indent=2, default=str))
        print(f"\nwrote {output}")
        return 0

    for probe in PROBES:
        results.append(run_probe(api, instance_id, probe))

    output.write_text(
        json.dumps({"discovery": discovered, "probes": results}, indent=2, default=str)
    )
    print(f"\nwrote {output}")
    print(
        "\nNext: fold each answer into contracts/shared/amc-columns.yaml and "
        "docs/sources/amc.md. A probe that does not change one of those was not worth "
        "running."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
