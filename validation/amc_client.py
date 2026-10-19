# SPDX-License-Identifier: MIT-0
"""Minimal AMC / Amazon Ads API client, for source validation only.

NOT PRODUCTION CODE, and deliberately not importable from src/. This platform does
not call AMC at runtime: the producer that operates extraction, outside this repository,
lands files in S3, and our world starts at the S3 object
(docs/capabilities/source-acquisition/README.md §1).
So these credentials are a validation-time dependency, no Lambda ever holds them,
and nothing in infra/ grants access to them.

Standard library, boto3, and the project HTTP boundary only. The boundary rejects
non-HTTPS initial URLs and redirect downgrades before credentials can be sent.

Design note: this client is built to be DIAGNOSTIC rather than clever. Several
request details below are documented but unverified against a live instance, so
every failure prints the status code and the response body. A legible 400 is worth
more here than a wrapper that hides it.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, cast

from adapters.outbound.https import HttpsSession, require_https

_HTTP = HttpsSession()
HTTP_ERROR_STATUS = 300

# Login with Amazon token endpoints, per region. [verified 2026-08-19]
# https://advertising.amazon.com/API/docs/en-us/get-started/generate-api-tokens
TOKEN_HOSTS = {
    "na": "https://api.amazon.com",
    "eu": "https://api.amazon.co.uk",
    "fe": "https://api.amazon.co.jp",
}

# Amazon Ads API hosts. NA is [verified] in docs/sources/amc.md section 8; the EU and
# FE forms follow the documented naming convention but are [unverified] here.
API_HOSTS = {
    "na": "https://advertising-api.amazon.com",
    "eu": "https://advertising-api-eu.amazon.com",
    "fe": "https://advertising-api-fe.amazon.com",
}

# The LWA consent page. [verified] for NA. EU and FE may use a regional host; if the
# consent page rejects the client, that is the first thing to check.
AUTHORIZE_URL = "https://www.amazon.com/ap/oa"

SCOPE = "advertising::campaign_management"


class AmcError(RuntimeError):
    """An API call failed. Carries the status and body so the cause is visible."""

    def __init__(self, status: int, body: str, url: str) -> None:
        super().__init__(f"HTTP {status} from {url}\n{body}")
        self.status = status
        self.body = body


@dataclass(frozen=True)
class Credentials:
    """Everything needed to reach one AMC instance.

    `amc_account_id` and `instance_id` are discovered rather than supplied on a first
    run, which is why they are optional here.
    """

    client_id: str
    client_secret: str
    refresh_token: str
    region: str
    marketplace_id: str | None = None
    amc_account_id: str | None = None
    instance_id: str | None = None

    def __post_init__(self) -> None:
        if self.region not in TOKEN_HOSTS:
            raise ValueError(f"region must be one of {sorted(TOKEN_HOSTS)}, got {self.region!r}")


def credentials_from_environment() -> Credentials:
    """Read local validation credentials without logging any secret value."""
    required = ("AMC_CLIENT_ID", "AMC_CLIENT_SECRET", "AMC_REFRESH_TOKEN", "AMC_REGION")
    missing = tuple(name for name in required if not os.environ.get(name))
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


def _post_form(url: str, fields: dict[str, str]) -> dict[str, Any]:
    """Form-encoded POST, used only against the LWA token endpoint."""
    safe_url = require_https(url)
    with _HTTP.post(
        safe_url,
        data=urllib.parse.urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    ) as response:
        raw = response.content
        if response.status_code >= HTTP_ERROR_STATUS:
            raise AmcError(response.status_code, raw.decode("utf-8", "replace"), safe_url)
        return json.loads(raw)


def authorize_url(client_id: str, redirect_uri: str) -> str:
    """Build the consent URL a human opens in a browser.

    `redirect_uri` must match one of the app's Allowed Return URLs byte-for-byte,
    and must be repeated identically in the token exchange. A trailing slash is a
    difference — the Security Profile page lists Allowed Origins with one and Allowed
    Return URLs without, which is the trap.
    """
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "scope": SCOPE,
            "response_type": "code",
            "redirect_uri": redirect_uri,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def exchange_code(
    *, code: str, client_id: str, client_secret: str, redirect_uri: str, region: str
) -> dict[str, Any]:
    """Redeem a one-time authorization code for a refresh token.

    The code is single-use and expires in minutes, so have this ready before
    consenting rather than after.
    """
    return _post_form(
        f"{TOKEN_HOSTS[region]}/auth/o2/token",
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )


def access_token(credentials: Credentials) -> str:
    """Mint a short-lived access token from the refresh token.

    Access tokens last about an hour. The refresh token does not expire on its own,
    which is exactly why it is treated like a password.
    """
    payload = _post_form(
        f"{TOKEN_HOSTS[credentials.region]}/auth/o2/token",
        {
            "grant_type": "refresh_token",
            "refresh_token": credentials.refresh_token,
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
        },
    )
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        fields = ", ".join(sorted(str(key) for key in payload)) or "none"
        raise AmcError(200, f"unexpected token response fields: {fields}", "token endpoint")
    return token


class AdsApi:
    """Authenticated caller for the Amazon Ads API, including the AMC operations."""

    def __init__(self, credentials: Credentials) -> None:
        self._credentials = credentials
        self._token = access_token(credentials)
        self._host = API_HOSTS[credentials.region]

    @property
    def credentials(self) -> Credentials:
        """The current credentials, including anything discovery has filled in."""
        return self._credentials

    def adopt_amc_identity(self, *, account_id: str, marketplace_id: str) -> None:
        """Take on the AMC account and marketplace discovered from /amc/accounts.

        Discovery is necessarily two-phase: `/amc/instances` rejects a request with no
        entity ID ("entityId provided is null", observed 2026-08-19), and the entity ID
        is what `/amc/accounts` returns. So the first call cannot send the AMC headers
        and the second one must.
        """
        self._credentials = dataclasses.replace(
            self._credentials, amc_account_id=account_id, marketplace_id=marketplace_id
        )

    def call(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        amc_headers: bool = False,
        content_type: str = "application/json",
    ) -> Any:
        """One request. Raises AmcError carrying the response body on any failure.

        `amc_headers` adds the two headers the migrated AMC APIs require —
        Amazon-Advertising-API-AdvertiserId (the AMC account ID) and
        Amazon-Advertising-API-MarketplaceId. They are opt-in because the discovery
        calls run before those values are known.
        """
        url = require_https(f"{self._host}{path}")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Amazon-Advertising-API-ClientId": self._credentials.client_id,
            "Content-Type": content_type,
            "Accept": "application/json",
        }
        if amc_headers:
            if not self._credentials.amc_account_id or not self._credentials.marketplace_id:
                raise ValueError(
                    "amc_headers requested but amc_account_id or marketplace_id is unset. "
                    "Run discovery first."
                )
            headers["Amazon-Advertising-API-AdvertiserId"] = self._credentials.amc_account_id
            headers["Amazon-Advertising-API-MarketplaceId"] = self._credentials.marketplace_id

        payload = json.dumps(body).encode() if body is not None else None
        with _HTTP.request(
            method,
            url,
            data=payload,
            headers=headers,
            timeout=60,
        ) as response:
            raw = response.content
            if response.status_code >= HTTP_ERROR_STATUS:
                raise AmcError(response.status_code, raw.decode("utf-8", "replace"), url)
            return json.loads(raw) if raw else {}

    def execute_adhoc(
        self,
        *,
        instance_id: str,
        workflow_id: str,
        sql: str,
        window_start: str,
        window_end: str,
        require_synthetic_data: bool | None = True,
        disable_aggregation_controls: bool | None = False,
        suppression_columns: tuple[str, str, str] | None = None,
        parameter_values: dict[str, str] | None = None,
    ) -> str:
        """Submit an ad hoc workflow, with sandbox-only controls explicitly omittable.

        Sandbox callers keep both defaults. STANDARD callers pass None for both controls so the
        request does not claim synthetic data or use a sandbox-only aggregation switch.

        `parameter_values` declares each name as a STRING `inputParameters` entry on the ad hoc
        `workflow` object, with no default, and sends the values in the execution's
        `parameterValues` map. Whether AMC accepts that ad hoc form is what the P-BO parameter
        shape probe settles. When None, the body is unchanged.
        """
        body: dict[str, Any] = {
            "workflow": {
                "workflowId": workflow_id,
                # AMC requires a single-line query: no newlines, no tabs.
                "sqlQuery": " ".join(sql.split()),
            },
            "timeWindowStart": window_start,
            "timeWindowEnd": window_end,
            "timeWindowType": "EXPLICIT",
            "timeWindowTimeZone": "UTC",
        }
        if require_synthetic_data is not None:
            body["requireSyntheticData"] = require_synthetic_data
        if disable_aggregation_controls is not None:
            body["disableAggregationControls"] = disable_aggregation_controls
        if suppression_columns is not None:
            discriminator, reason, distinct_users = suppression_columns
            body["workflow"]["filteredMetricsDiscriminatorColumn"] = discriminator
            body["workflow"]["filteredReasonColumn"] = reason
            body["workflow"]["distinctUserCountColumn"] = distinct_users
        if parameter_values:
            body["workflow"]["inputParameters"] = [
                {
                    "name": name,
                    "dataType": "STRING",
                    "columnType": "DIMENSION",
                    "description": f"validation probe parameter {name}",
                }
                for name in sorted(parameter_values)
            ]
            body["parameterValues"] = dict(sorted(parameter_values.items()))

        response = self.call(
            "POST",
            f"/amc/reporting/{instance_id}/workflowExecutions",
            body=body,
            amc_headers=True,
        )
        execution = response.get("workflowExecution", response)
        execution_id = execution.get("workflowExecutionId")
        if not execution_id:
            raise AmcError(200, json.dumps(response), "workflowExecutions")
        return str(execution_id)

    def wait_for_execution(
        self, *, instance_id: str, execution_id: str, timeout_seconds: int = 900
    ) -> dict[str, Any]:
        """Poll until the execution reaches a terminal status.

        A 200 on submission means accepted, not compiled. This is the call that tells
        you whether the query was actually legal — which for a probe testing whether a
        column may be selected is the entire result.
        """
        deadline = time.monotonic() + timeout_seconds
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.call(
                "GET",
                f"/amc/reporting/{instance_id}/workflowExecutions/{execution_id}",
                amc_headers=True,
            )
            execution = last.get("workflowExecution", last)
            status = str(execution.get("status", "")).upper()
            # REJECTED is terminal and was missing, which cost a full 900s wait on an execution
            # AMC had already refused four seconds in. It is the status an invalid query gets —
            # the compile failure lands in `statusReason` — so for a probe testing admissibility
            # it is the most likely outcome of all, not an edge case.
            if status in {"SUCCEEDED", "FAILED", "CANCELLED", "REJECTED"}:
                return execution
            time.sleep(10)
        raise TimeoutError(
            f"execution {execution_id} did not finish within {timeout_seconds}s; last: {last}"
        )

    def executions_since(
        self,
        *,
        instance_id: str,
        min_creation_time: str,
    ) -> list[dict[str, Any]]:
        """Recover server-side executions created since an exact AMC timestamp."""
        query = urllib.parse.urlencode({"minCreationTime": min_creation_time})
        response = cast(
            "dict[str, Any]",
            self.call(
                "GET",
                f"/amc/reporting/{instance_id}/workflowExecutions?{query}",
                amc_headers=True,
            ),
        )
        executions = cast("list[object]", response.get("workflowExecutions") or [])
        return [cast("dict[str, Any]", item) for item in executions if isinstance(item, dict)]

    def download_urls(self, *, instance_id: str, execution_id: str) -> list[str]:
        """Presigned result URLs.

        Preferred over the instance's S3 bucket for a probe: the bucket policy has to
        be updated after the instance-level API migration or results silently stop
        arriving, and a probe should not depend on that being right.
        """
        response = cast(
            "dict[str, Any]",
            self.call(
                "GET",
                f"/amc/reporting/{instance_id}/workflowExecutions/{execution_id}/downloadUrls",
                amc_headers=True,
            ),
        )
        urls = cast("list[object]", response.get("downloadUrls") or [])
        return [str(url) for url in urls]


def fetch_csv(url: str, max_bytes: int = 2_000_000) -> str:
    """Read a presigned result URL whole, or refuse. Results are CSV.

    **This used to truncate silently.** `response.read(max_bytes)` returns the first `max_bytes` and
    says nothing, so a result larger than the cap came back as a shorter CSV that parsed cleanly.
    For a probe that costs a wrong conclusion; for the ingestion harness, which computes a byte
    count and a digest over whatever it fetched, it costs something worse — a delivery that is
    internally consistent and quietly short, which is the one corruption a manifest cannot catch,
    because the manifest would describe the truncated bytes correctly.

    So it reads one byte past the cap and refuses if that byte exists. Refusing is the honest
    outcome: the caller wanted the result and the result did not fit, and raising the cap is a
    decision rather than something to infer.
    """
    safe_url = require_https(url)
    with _HTTP.get(safe_url, timeout=60, stream=True) as response:
        body = response.raw.read(max_bytes + 1)
        if response.status_code >= HTTP_ERROR_STATUS:
            raise AmcError(
                response.status_code,
                body.decode("utf-8", "replace"),
                safe_url,
            )
    if len(body) > max_bytes:
        raise AmcError(
            200,
            f"the result exceeds {max_bytes} bytes. Reading a prefix of it would produce a CSV "
            "that parses cleanly and is short, so it is refused instead. Raise max_bytes.",
            url,
        )
    return body.decode("utf-8", "replace")
