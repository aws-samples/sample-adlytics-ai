# SPDX-License-Identifier: MIT-0
"""Mint an AMC refresh token directly into an explicitly selected Secrets Manager secret.

A human must approve the Login with Amazon consent screen. Before consent, the script resolves the
explicit AWS profile, displays its caller identity, and requires confirmation of the destination.
The resulting refresh token and client secret are sent directly to Secrets Manager; neither is
printed or written to a local file.

Usage:

    uv run python -m validation.mint_token \
        --client-id amzn1.application-oa2-client.xxxx \
        --region na \
        --redirect-uri https://amazon.com \
        --profile <profile> \
        --secret-name adlytics/<env>/amc/api-credentials

The client secret is read from a prompt, never an argument. The destination secret must not already
exist: refusing overwrite keeps credential rotation a separate, deliberate operation.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError

from validation.amc_client import TOKEN_HOSTS, authorize_url, exchange_code


@dataclass(frozen=True, slots=True)
class _Destination:
    """One confirmed Secrets Manager target and its explicit-profile session."""

    secrets_manager: Any
    secret_name: str


def _destination(profile: str, secret_name: str) -> _Destination | None:
    """Resolve and confirm the exact account before any credential is minted."""
    session = boto3.Session(profile_name=profile)
    identity = session.client("sts").get_caller_identity()
    account = str(identity.get("Account", "")).strip()
    arn = str(identity.get("Arn", "")).strip()
    if not account or not arn:
        raise RuntimeError("the selected profile returned no caller identity")

    print(f"AWS account: {account}")
    print(f"Caller ARN: {arn}")
    print(f"Destination secret: {secret_name}")
    secrets_manager = session.client("secretsmanager")
    try:
        secrets_manager.describe_secret(SecretId=secret_name)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise
    else:
        print("Destination secret already exists; refusing to overwrite it.", file=sys.stderr)
        return None

    confirmed = input("Create this new secret in the displayed account? Type 'create': ").strip()
    return (
        _Destination(secrets_manager, secret_name) if confirmed == "create" else None
    )


def _secret_payload(
    *, client_id: str, client_secret: str, refresh_token: str, region: str
) -> str:
    """Serialize the complete secret without exposing it through operator output."""
    return json.dumps(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "region": region,
            "marketplace_id": "",
            "amc_account_id": "",
            "instance_id": "",
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _parser() -> argparse.ArgumentParser:
    """Declare every target explicitly; no profile or secret name has a default."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", required=True)
    parser.add_argument(
        "--region",
        required=True,
        choices=sorted(TOKEN_HOSTS),
        help="Chosen by the advertiser's marketplace, not by an AWS region.",
    )
    parser.add_argument(
        "--redirect-uri",
        required=True,
        help=(
            "Must match one of the app's Allowed Return URLs byte-for-byte. "
            "A trailing slash is a difference."
        ),
    )
    parser.add_argument("--profile", required=True, help="AWS profile for the destination account.")
    parser.add_argument("--secret-name", required=True, help="New Secrets Manager secret name.")
    return parser


def main() -> int:
    """Confirm the target, complete human consent, and create the secret without disclosure."""
    args = _parser().parse_args()
    destination = _destination(args.profile, args.secret_name)
    if destination is None:
        print("Secret creation cancelled; no authorization code was requested.", file=sys.stderr)
        return 1

    print("\nStep 1. Open this URL in a browser signed in as an authorized user:\n")
    print(f"  {authorize_url(args.client_id, args.redirect_uri)}\n")
    print("Approve the consent screen, then copy only the `code` query-parameter value.\n")

    code = input("Paste the authorization code: ").strip()
    if not code:
        print("No code supplied.", file=sys.stderr)
        return 1

    client_secret = os.environ.get("AMC_CLIENT_SECRET", "").strip()
    if client_secret:
        print("Using AMC_CLIENT_SECRET from the environment.")
    else:
        client_secret = getpass.getpass("Client secret (not echoed): ").strip()
    if not client_secret:
        print("No client secret supplied.", file=sys.stderr)
        return 1

    payload = exchange_code(
        code=code,
        client_id=args.client_id,
        client_secret=client_secret,
        redirect_uri=args.redirect_uri,
        region=args.region,
    )
    refresh_token = payload.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        fields = ", ".join(sorted(str(key) for key in payload)) or "none"
        print(
            f"Token response did not contain refresh_token; returned fields: {fields}.",
            file=sys.stderr,
        )
        return 1

    destination.secrets_manager.create_secret(
        Name=destination.secret_name,
        Description="AMC validation credentials",
        SecretString=_secret_payload(
            client_id=args.client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
            region=args.region,
        ),
    )
    print("Refresh token stored in the confirmed Secrets Manager secret.")
    print("Run probe_amc.py discovery to populate the empty AMC identity fields.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
