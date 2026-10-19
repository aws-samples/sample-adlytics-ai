# SPDX-License-Identifier: MIT-0
"""Provision the persistent synthetic dev validation users and retire the old synthetic ones.

The Quality Review personas exercise the review route matrix (ADR-078 §3): `reviewer` is in
`quality-reviewer`, `attributor` in both `quality-reviewer` and `audit-attribution`,
`attribution-only` in `audit-attribution` alone, and `denied` in neither. The two chat personas,
`probe-a` and `probe-b`, hold no group and exist for ADR-077 identity-chain validation: probes
sign in as them and call the product through `/api/chat`, and two distinct users are what proves
one user cannot recall another's memory.

Each persona's review-group membership is reconciled exactly, so a stale membership cannot
survive a re-run. The three synthetic personas of the retired per-level groups (`audit`,
`findings`, `evaluation`) are deleted with their credential parameters, so the operator group
migration (`make migrate-review-groups`) never carries them into `quality-reviewer`.
"""

from __future__ import annotations

import argparse
import json
import secrets
import string
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError
from cognito_personas import persona_parameter_name, persona_username

_REGION = "us-east-1"
QUALITY_REVIEWER = "quality-reviewer"
AUDIT_ATTRIBUTION = "audit-attribution"
MANAGED_GROUPS = frozenset({QUALITY_REVIEWER, AUDIT_ATTRIBUTION})
"""The review groups this script owns; membership in any other group is left untouched."""
RETIRED_PERSONAS = ("audit", "findings", "evaluation")
PERSONAS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("reviewer", (QUALITY_REVIEWER,)),
    ("attributor", (QUALITY_REVIEWER, AUDIT_ATTRIBUTION)),
    ("attribution-only", (AUDIT_ATTRIBUTION,)),
    ("denied", ()),
    ("probe-a", ()),
    ("probe-b", ()),
)


@dataclass(frozen=True, slots=True)
class Persona:
    """One persistent synthetic dev identity and the review groups it belongs to."""

    label: str
    groups: tuple[str, ...]

    @property
    def username(self) -> str:
        """Return the reserved non-deliverable Cognito username."""
        return persona_username(self.label)

    def parameter_name(self, environment: str) -> str:
        """Return this persona's exact encrypted validation credential path."""
        return persona_parameter_name(environment, self.label)


def _password() -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "Aa1!" + "".join(secrets.choice(alphabet) for _ in range(28))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account", required=True)
    return parser


def _parameter_path(environment: str, key: str) -> str:
    return f"/adlytics/{environment}/auth/{key}"


def _account(session: boto3.Session, expected: str) -> None:
    account = session.client("sts").get_caller_identity().get("Account")
    if not isinstance(account, str) or account != expected:
        raise ValueError("explicit profile did not resolve to the expected account")


def _secret(ssm: Any, persona: Persona, environment: str) -> dict[str, str] | None:
    try:
        response = ssm.get_parameter(Name=persona.parameter_name(environment), WithDecryption=True)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ParameterNotFound":
            return None
        raise
    value = response["Parameter"]["Value"]
    raw: object = json.loads(value)
    if not isinstance(raw, dict):
        raise ValueError("quality review validation credential is malformed")
    document = {str(key): str(item) for key, item in raw.items()}
    if document.get("username") != persona.username or not document.get("password"):
        raise ValueError("quality review validation credential is malformed")
    return document


def _create_user(*, cognito: Any, pool_id: str, persona: Persona, password: str) -> None:
    try:
        cognito.admin_get_user(UserPoolId=pool_id, Username=persona.username)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "UserNotFoundException":
            raise
        cognito.admin_create_user(
            UserPoolId=pool_id,
            Username=persona.username,
            UserAttributes=[
                {"Name": "email", "Value": persona.username},
                {"Name": "email_verified", "Value": "true"},
            ],
            TemporaryPassword=password,
            MessageAction="SUPPRESS",
        )
    cognito.admin_set_user_password(
        UserPoolId=pool_id,
        Username=persona.username,
        Password=password,
        Permanent=True,
    )
    _reconcile_groups(cognito=cognito, pool_id=pool_id, persona=persona)


def _reconcile_groups(*, cognito: Any, pool_id: str, persona: Persona) -> None:
    """Make the persona's review-group membership exactly its declared groups.

    Only `MANAGED_GROUPS` are added or removed; any other group, such as a prompt-admin grant an
    operator made, is left as it is.
    """
    current: set[str] = set()
    token: str | None = None
    while True:
        request: dict[str, object] = {"UserPoolId": pool_id, "Username": persona.username}
        if token is not None:
            request["NextToken"] = token
        response = cognito.admin_list_groups_for_user(**request)
        current.update(str(group["GroupName"]) for group in response.get("Groups", []))
        next_token = response.get("NextToken")
        if not next_token:
            break
        token = str(next_token)
    for group in sorted(set(persona.groups) - current):
        cognito.admin_add_user_to_group(
            UserPoolId=pool_id, Username=persona.username, GroupName=group
        )
    for group in sorted((current & MANAGED_GROUPS) - set(persona.groups)):
        cognito.admin_remove_user_from_group(
            UserPoolId=pool_id, Username=persona.username, GroupName=group
        )


def _retire_personas(*, ssm: Any, cognito: Any, pool_id: str, environment: str) -> int:
    """Delete each retired synthetic persona and its credential; return how many existed."""
    removed = 0
    for label in RETIRED_PERSONAS:
        persona = Persona(label=label, groups=())
        try:
            cognito.admin_delete_user(UserPoolId=pool_id, Username=persona.username)
            removed += 1
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "UserNotFoundException":
                raise
        try:
            ssm.delete_parameter(Name=persona.parameter_name(environment))
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ParameterNotFound":
                raise
    return removed


def _provision(*, ssm: Any, cognito: Any, pool_id: str, environment: str) -> None:
    for label, groups in PERSONAS:
        persona = Persona(label=label, groups=groups)
        secret = _secret(ssm, persona, environment)
        if secret is None:
            secret = {"username": persona.username, "password": _password(), "state": "permanent"}
            ssm.put_parameter(
                Name=persona.parameter_name(environment),
                Type="SecureString",
                Value=json.dumps(secret, separators=(",", ":"), sort_keys=True),
                KeyId="alias/aws/ssm",
                Overwrite=False,
            )
        _create_user(
            cognito=cognito,
            pool_id=pool_id,
            persona=persona,
            password=secret["password"],
        )


def main(argv: list[str] | None = None) -> int:
    """Provision the six exact persistent synthetic dev identities and remove retired ones."""
    args = _parser().parse_args(argv)
    if args.environment != "dev" or args.region != _REGION:
        print("quality review validation users accept only the approved dev region")
        return 2
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    _account(session, args.expected_account)
    ssm = session.client("ssm")
    cognito = session.client("cognito-idp")
    parameter = ssm.get_parameter(Name=_parameter_path(args.environment, "user-pool-id"))[
        "Parameter"
    ]
    pool_id = parameter["Value"]
    if not isinstance(pool_id, str) or not pool_id:
        raise ValueError("user pool parameter is invalid")
    _provision(ssm=ssm, cognito=cognito, pool_id=pool_id, environment=args.environment)
    retired = _retire_personas(
        ssm=ssm, cognito=cognito, pool_id=pool_id, environment=args.environment
    )
    labels = ", ".join(label for label, _ in PERSONAS)
    print(f"validation users provisioned: {labels}; retired personas removed: {retired}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
