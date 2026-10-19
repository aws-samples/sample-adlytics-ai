# SPDX-License-Identifier: MIT-0
"""Synthetic dev Cognito personas: their stored credentials, sign-in, and the bearer they carry.

The personas are provisioned by `provision_quality_review_test_users.py`. A cookie credential is
signed in with the dev-only `USER_PASSWORD_AUTH` flow and the BFF app client's `SECRET_HASH`, the
mechanism ADR-077 §7 extends so validation runs as real users on the real path. A bearer is
signed in with the IAM-authenticated `ADMIN_USER_PASSWORD_AUTH` flow on the public PKCE client
(ADR-078 §2), so a probe reaches the bearer path without a browser. Nothing here prints or
persists a password or token: the dataclasses holding them exclude those fields from `repr`, and
callers redact them from evidence.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    import boto3
    from botocore.awsrequest import AWSPreparedRequest
    from types_boto3_bedrock_agentcore import BedrockAgentCoreClient
    from types_boto3_cognito_idp import CognitoIdentityProviderClient
    from types_boto3_ssm import SSMClient

__all__ = [
    "AppClient",
    "PersonaCredential",
    "PersonaTokens",
    "PublicClient",
    "admin_sign_in",
    "app_client",
    "bearer_agentcore_client",
    "load_persona",
    "persona_parameter_name",
    "persona_username",
    "public_client",
    "public_password_sign_in_refused",
    "published_parameter",
    "secret_hash",
    "sign_in",
    "unverified_claims",
]

_ROOT = Path(__file__).resolve().parents[1]
_PARAMETER_CONTRACT = _ROOT / "contracts" / "generated" / "parameter-paths.json"
_USERNAME_PREFIX = "adlytics-quality-review-"
_INVOKE_HARNESS_BEFORE_SEND = "before-send.bedrock-agentcore.InvokeHarness"
_JWT_SEGMENTS = 3


def persona_username(label: str) -> str:
    """Return the reserved non-deliverable Cognito username for one persona."""
    return f"{_USERNAME_PREFIX}{label}@invalid.example"


def persona_parameter_name(environment: str, label: str) -> str:
    """Return the exact encrypted SSM path holding one persona's credential."""
    return f"/adlytics/{environment}/validation/quality-review-user/{label}"


def published_parameter(ssm: SSMClient, environment: str, key: str) -> str:
    """Read one value an infra stack publishes, by its generated PARAMS key.

    The path comes from `contracts/generated/parameter-paths.json`, so a renamed parameter is a
    contract change here rather than a literal that silently points at nothing.
    """
    document = cast("dict[str, object]", json.loads(_PARAMETER_CONTRACT.read_text("utf-8")))
    parameters = cast("dict[str, dict[str, str]]", document["parameters"])
    spec = parameters.get(key)
    if spec is None:
        raise ValueError(f"parameter contract has no key {key!r}")
    name = f"/adlytics/{environment}/{spec['area']}/{spec['name']}"
    value = ssm.get_parameter(Name=name)["Parameter"].get("Value")
    if not value or not value.strip():
        raise ValueError(f"published parameter {key!r} is empty")
    return value


@dataclass(frozen=True, slots=True)
class PersonaCredential:
    """One persona's stored sign-in credential."""

    label: str
    username: str
    password: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PersonaTokens:
    """The Cognito tokens one persona sign-in returned."""

    label: str
    access_token: str = field(repr=False)
    id_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class AppClient:
    """The BFF app client a persona signs in through."""

    client_id: str
    secret: str = field(repr=False)


def load_persona(ssm: SSMClient, environment: str, label: str) -> PersonaCredential:
    """Read and validate one persona's SecureString credential."""
    response = ssm.get_parameter(
        Name=persona_parameter_name(environment, label), WithDecryption=True
    )
    raw: object = json.loads(response["Parameter"].get("Value", ""))
    if not isinstance(raw, dict):
        raise ValueError("persona credential is malformed")
    value = {str(key): str(item) for key, item in cast("dict[object, object]", raw).items()}
    if value.get("username") != persona_username(label) or not value.get("password"):
        raise ValueError("persona credential is malformed")
    return PersonaCredential(label=label, username=value["username"], password=value["password"])


def app_client(session: boto3.Session, environment: str) -> AppClient:
    """Resolve the BFF app client id and its secret from the published auth parameters."""
    ssm = session.client("ssm")
    client_id = published_parameter(ssm, environment, "userPoolClientId")
    secret_name = published_parameter(ssm, environment, "userPoolClientSecretName")
    secret = (
        session.client("secretsmanager").get_secret_value(SecretId=secret_name).get("SecretString")
    )
    if not secret:
        raise ValueError("app client secret is empty")
    return AppClient(client_id=client_id, secret=secret)


def secret_hash(username: str, client_id: str, secret: str) -> str:
    """Compute Cognito's SECRET_HASH for a confidential app client."""
    digest = hmac.new(secret.encode(), f"{username}{client_id}".encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def sign_in(
    cognito: CognitoIdentityProviderClient, credential: PersonaCredential, client: AppClient
) -> PersonaTokens:
    """Sign one persona in with USER_PASSWORD_AUTH and return its access and ID tokens."""
    response = cognito.initiate_auth(
        AuthFlow="USER_PASSWORD_AUTH",
        ClientId=client.client_id,
        AuthParameters={
            "USERNAME": credential.username,
            "PASSWORD": credential.password,
            "SECRET_HASH": secret_hash(credential.username, client.client_id, client.secret),
        },
    )
    result = response.get("AuthenticationResult", {})
    access = result.get("AccessToken")
    identity = result.get("IdToken")
    if not access or not identity:
        raise ValueError("persona sign-in did not return both an access and an ID token")
    return PersonaTokens(label=credential.label, access_token=access, id_token=identity)


@dataclass(frozen=True, slots=True)
class PublicClient:
    """The public PKCE app client a bearer is issued through, and the pool it belongs to."""

    user_pool_id: str
    client_id: str


def public_client(session: boto3.Session, environment: str) -> PublicClient:
    """Resolve the user pool and its public client from the published auth parameters."""
    ssm = session.client("ssm")
    return PublicClient(
        user_pool_id=published_parameter(ssm, environment, "userPoolId"),
        client_id=published_parameter(ssm, environment, "userPoolPublicClientId"),
    )


def admin_sign_in(
    cognito: CognitoIdentityProviderClient, credential: PersonaCredential, client: PublicClient
) -> PersonaTokens:
    """Sign one persona in on the public client with IAM-authenticated ADMIN_USER_PASSWORD_AUTH.

    The public client has no secret, so no `SECRET_HASH` is sent; the caller's IAM credentials
    are what authenticate the call.
    """
    response = cognito.admin_initiate_auth(
        UserPoolId=client.user_pool_id,
        ClientId=client.client_id,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": credential.username, "PASSWORD": credential.password},
    )
    result = response.get("AuthenticationResult", {})
    access = result.get("AccessToken")
    identity = result.get("IdToken")
    if not access or not identity:
        raise ValueError("persona bearer sign-in did not return both an access and an ID token")
    return PersonaTokens(label=credential.label, access_token=access, id_token=identity)


_REFUSED_PASSWORD_FLOW_CODES = frozenset({"InvalidParameterException", "NotAuthorizedException"})
"""What Cognito answers a password flow its client does not enable. Which of the two it returns
for a client without USER_PASSWORD_AUTH is [unverified], so either counts as refused."""


def public_password_sign_in_refused(
    cognito: CognitoIdentityProviderClient, credential: PersonaCredential, client: PublicClient
) -> bool:
    """Report whether USER_PASSWORD_AUTH on the public client is refused (ADR-078 §2).

    Any issued token means the client admits a password flow that needs neither a secret nor IAM
    credentials, which is `False`. A refusal of another kind is raised rather than counted.
    """
    try:
        response = cognito.initiate_auth(
            AuthFlow="USER_PASSWORD_AUTH",
            ClientId=client.client_id,
            AuthParameters={"USERNAME": credential.username, "PASSWORD": credential.password},
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in _REFUSED_PASSWORD_FLOW_CODES:
            return True
        raise
    return not response.get("AuthenticationResult")


def unverified_claims(token: str) -> dict[str, object]:
    """Decode a JWT payload WITHOUT verifying it.

    For reading the caller's own `sub` to derive where its records live; never an authorization
    decision. The token is one this process just received from Cognito over TLS.
    """
    segments = token.split(".")
    if len(segments) != _JWT_SEGMENTS:
        raise ValueError("token is not a compact JWS")
    payload = segments[1] + "=" * (-len(segments[1]) % 4)
    try:
        raw: object = json.loads(base64.urlsafe_b64decode(payload))
    except ValueError as error:  # binascii.Error and UnicodeDecodeError are ValueErrors
        raise ValueError("token payload is not base64url JSON") from error
    if not isinstance(raw, dict):
        raise ValueError("token payload is not a JSON object")
    return {str(key): value for key, value in cast("dict[object, object]", raw).items()}


def bearer_agentcore_client(
    session: boto3.Session, token: str, *, read_timeout: int
) -> BedrockAgentCoreClient:
    """Build an AgentCore client that presents `token` as the InvokeHarness bearer.

    The same technique as the application's Harness client: SigV4 is suppressed with `UNSIGNED`
    so the request asserts exactly one identity, and the header is attached at `before-send`.
    The session supplies only the explicit region; its credentials are never used to sign.
    """
    if not token.strip():
        raise ValueError("a bearer token is required")
    client = session.client(
        "bedrock-agentcore",
        config=Config(
            signature_version=UNSIGNED,
            read_timeout=read_timeout,
            retries={"mode": "standard", "total_max_attempts": 1},
        ),
    )
    authorization = f"Bearer {token}"

    def _attach_bearer(request: AWSPreparedRequest, **_: Any) -> None:
        request.headers["Authorization"] = authorization

    client.meta.events.register_first(_INVOKE_HARNESS_BEFORE_SEND, _attach_bearer)
    return client
