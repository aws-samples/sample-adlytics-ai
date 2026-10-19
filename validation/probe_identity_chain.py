#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""ADR-077 identity-chain probe: the user's own token is the only path that works.

    uv run python validation/probe_identity_chain.py --profile <profile> --environment dev \
        --region us-east-1 --expected-account <account> [--case p1|p5]

**What it settles.** ADR-077 §1 says no hop admits an AWS role as a substitute for the user. This
probe drives the deployed dev stacks as the synthetic chat personas `probe-a` and `probe-b`
(provisioned by `provision_quality_review_test_users.py`) and records, per case, what was
expected, what was observed, and whether they match:

- `--case p1` (slices S2 and S3, probe P1): a persona turn through `/api/chat` succeeds, which
  since S3 carries a route-minted Harness token; the Harness then refuses the persona's own
  Cognito access token, its ID token, a token with every Harness-token claim signed by an
  unpublished key, and the operator's SigV4 session on the Harness and on its underlying runtime,
  each with no Memory side effect; and the route exchange refuses the operator by its
  resource-policy Deny.
- `--case p5` (probe P5): `probe-a` states a fictional preference, the long-term Memory extracts
  it into `probe-a`'s own namespace, a later `probe-a` conversation recalls it, and `probe-b`
  asking the same question does not. The run then deletes every codename record and waits until
  none reappears, so late extraction cannot label later `probe-a` answers.
- `--case s5` (slices S4 and S5): a persona tool turn completes and every tool call in it logged
  `caller_verified: true` without naming the user, and the turn's audit receipt names the
  persona's own `sub` as its principal; then the Gateway refuses a `tools/call` with no bearer,
  a Cognito token, a Gateway-shaped token signed by an unpublished key, and a Harness-audience
  token, each at its authorizer with no tool invoked; every tool Lambda invoked directly by the
  operator with a fabricated Gateway context, with no bearer or a foreign-key one, refuses
  `ADLY-CALLER-0001` before routing; the route exchange and the agent client secret refuse the
  operator by their resource-policy Denies; and `/token` refuses a wrong client secret with
  `invalid_client`. A `/token` request with a Cognito subject and the correct secret is
  unit-tested only, because the secret is unreadable to the operator by design.

**Structure.** Cases live in one table per suite and run in table order. A case declares the
positive controls it depends on; it runs only if every one of them passed earlier in the same
run, so every negative varies one attribute of a request just shown to succeed. Later slices
(S3-S5) append cases to these tables.

**Output.** One line per case, `CASE id | expected | observed | PASS/FAIL`, then exit 0 only if
every case passed; an unexpected success is a FAIL. Sanitized evidence (no password or token) is
written to `validation/evidence/identity-probes/<timestamp>.json`, which is gitignored.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import hashlib
import json
import re
import secrets
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import boto3
import jwt
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from cognito_personas import (
    AppClient,
    PersonaTokens,
    app_client,
    bearer_agentcore_client,
    load_persona,
    published_parameter,
    sign_in,
    unverified_claims,
)
from cryptography.hazmat.primitives.asymmetric import rsa
from memory_codenames import (
    CODENAME,
    cross_session_namespaces,
    settle_codenames,
    sweep_codenames,
)
from quality_review_client import (
    ATTRIBUTOR_PERSONA,
    ReviewedReceipt,
    reviewed_principal,
    reviewed_receipt,
    reviewer_client,
)

from adapters.inbound.conversation.turns import actor_id_for
from adapters.outbound.https import HttpsSession, require_https
from domain.conversation.audit import MANAGED_HARNESS_RUNTIME_V3

if TYPE_CHECKING:
    from types_boto3_bedrock_agentcore import BedrockAgentCoreClient

_ROOT = Path(__file__).resolve().parents[1]
_EVIDENCE_DIR = _ROOT / "validation" / "evidence" / "identity-probes"
_REQUIRED_ENVIRONMENT = "dev"
_REQUIRED_REGION = "us-east-1"
_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
_QUALIFIER = "DEFAULT"
_HTTP_OK = 200
_ROUTE_TIMEOUT_SECONDS = 240
_HARNESS_READ_TIMEOUT_SECONDS = 300
_RECORD_POLL_INTERVAL_SECONDS = 30
_DEFAULT_MEMORY_TIMEOUT_SECONDS = 900
_RECORD_PAGE_LIMIT = 20
_OBSERVED_MESSAGE_LIMIT = 300
_PERSONA_A = "probe-a"
_PERSONA_B = "probe-b"
_HARNESS_QUESTION = "Hello. Please briefly introduce how you can help."


@dataclass(frozen=True, slots=True)
class _ExpectedRefusal:
    """The exact refusal one negative must observe: its error code and the reason it names.

    Both halves are pinned so a refusal for a different cause (an expired token, a throttle, a
    missing grant) cannot pass a case written for another one. Values are the live observations
    of 2026-09-26 against the dev Harness.
    """

    code: str
    reason: re.Pattern[str]

    def describe(self) -> str:
        return f"{self.code} naming '{self.reason.pattern}'"


# A SigV4 call to a JWT-authorized Harness, or to its runtime, is refused 403 by the service.
_SIGV4_ON_HARNESS = _ExpectedRefusal(
    "AccessDeniedException", re.compile(r"requires OAuth Bearer token authentication")
)
_SIGV4_ON_RUNTIME = _ExpectedRefusal(
    "AccessDeniedException", re.compile(r"Authorization method mismatch")
)
# Since S3 the Harness trusts only the token service (Harness audience, route client). A Cognito
# token is refused on its audience before any signature check; a token with every claim right but
# signed by a key the issuer never published is refused on its signature (observed 2026-09-26).
_COGNITO_ACCESS_AS_BEARER = _ExpectedRefusal(
    "AccessDeniedException", re.compile(r"missing required audience claim")
)
_ID_TOKEN_AS_BEARER = _ExpectedRefusal(
    "AccessDeniedException", re.compile(r"audience does not match this harness")
)
_FOREIGN_KEY_BEARER = _ExpectedRefusal(
    "UnrecognizedClientException", re.compile(r"Invalid Signature")
)
# The route exchange's resource-policy Deny, not a missing identity grant: the operator holds
# administrator-class permissions, so only the explicit Deny can refuse it (ADR-077 §3).
_ROUTE_EXCHANGE_DENY = _ExpectedRefusal(
    "AccessDeniedException", re.compile(r"explicit deny in a resource-based policy")
)
_HARNESS_AUDIENCE = "adlytics-{environment}-harness"
_ROUTE_CLIENT = "adlytics-{environment}-conversation-route"
_FORGED_LIFETIME_SECONDS = 240
# An IAM identity-policy denial names the principal and action this way. A SigV4 refusal matching
# it was refused by IAM, not by the Harness authorizer, so it proves nothing about ADR-077 §2.
_IAM_DENIAL = re.compile(r"is not authorized to perform")
_JWT_SHAPE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")
_REDACTED = "<redacted>"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--case", choices=("p1", "p5", "s5"), default="p1", dest="suite")
    parser.add_argument(
        "--memory-timeout-seconds",
        type=int,
        default=_DEFAULT_MEMORY_TIMEOUT_SECONDS,
        help="P5: how long to wait for long-term Memory to extract the stated preference.",
    )
    return parser


# ---------------------------------------------------------------------------
# Run context
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Context:
    """Target values and per-run state shared by the cases of one suite."""

    session: boto3.Session
    environment: str
    origin: str
    harness_arn: str
    memory_id: str
    # The cross-session namespaces the Harness retrieves, read once from its configuration.
    cross_session_namespaces: tuple[str, ...]
    client: AppClient = field(repr=False)
    memory_timeout_seconds: int
    secrets: set[str] = field(default_factory=set[str], repr=False)
    state: dict[str, str] = field(default_factory=dict[str, str])
    _bearer_tokens: dict[str, PersonaTokens] = field(
        default_factory=dict[str, PersonaTokens], repr=False
    )

    def fresh_tokens(self, label: str) -> PersonaTokens:
        """Sign a persona in now, so each case presents a token that has just been issued."""
        credential = load_persona(self.session.client("ssm"), self.environment, label)
        self.secrets.add(credential.password)
        tokens = sign_in(self.session.client("cognito-idp"), credential, self.client)
        self.secrets.update({tokens.access_token, tokens.id_token})
        return tokens

    def harness_tokens(self, label: str) -> PersonaTokens:
        """One sign-in shared by a positive bearer case and the negatives that vary it."""
        if label not in self._bearer_tokens:
            self._bearer_tokens[label] = self.fresh_tokens(label)
        return self._bearer_tokens[label]

    def subject(self, label: str) -> str:
        """Return the persona's Cognito `sub`, read from its own access token."""
        subject = unverified_claims(self.harness_tokens(label).access_token).get("sub")
        if not isinstance(subject, str) or not subject:
            raise ValueError("persona access token carries no sub")
        return subject

    def actor_id(self, label: str) -> str:
        """Derive the per-user Memory actor the route uses, from the `sub` (ADR-077 §6)."""
        return actor_id_for(self.subject(label))

    def sigv4_agentcore(self) -> BedrockAgentCoreClient:
        """Return the operator's SigV4 AgentCore client: the credential the Harness must refuse."""
        return self.session.client(
            "bedrock-agentcore",
            config=Config(
                read_timeout=_HARNESS_READ_TIMEOUT_SECONDS,
                retries={"mode": "standard", "total_max_attempts": 1},
            ),
        )

    def redact(self, text: str) -> str:
        """Remove every password or token this run has handled, and anything shaped like one."""
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, _REDACTED)
        return _JWT_SHAPE.sub(_REDACTED, text)


@dataclass(frozen=True, slots=True)
class _Verdict:
    """What one case observed, and whether it matched the case's expectation."""

    passed: bool
    observed: str
    evidence: Mapping[str, object] = field(default_factory=dict[str, object])


@dataclass(frozen=True, slots=True)
class Case:
    """One row of a suite: an expectation, its positive controls, and how to observe it."""

    case_id: str
    expected: str
    requires: tuple[str, ...]
    run: Callable[[_Context], _Verdict]


@dataclass(frozen=True, slots=True)
class CaseResult:
    """One case's expected versus observed outcome, as printed and recorded."""

    case_id: str
    expected: str
    observed: str
    passed: bool
    evidence: Mapping[str, object]


# ---------------------------------------------------------------------------
# Observation helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Outcome:
    """How one AgentCore data-plane call ended."""

    accepted: bool
    phase: str
    http_status: int | None = None
    code: str | None = None
    message: str | None = None
    stop_reasons: tuple[str, ...] = ()

    def describe(self) -> str:
        if not self.accepted:
            return f"refused HTTP {self.http_status} {self.code}: {self.message}"
        if self.phase == "stream":
            return f"accepted, then stream error {self.code}: {self.message}"
        return f"accepted; stop reasons {list(self.stop_reasons)}"


def _client_error(error: ClientError) -> tuple[int | None, str | None, str | None]:
    detail = error.response.get("Error", {})
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    message = detail.get("Message")
    return (
        status,
        detail.get("Code"),
        message[:_OBSERVED_MESSAGE_LIMIT] if message is not None else None,
    )


def _session_id(case_id: str) -> str:
    # InvokeHarness requires 33-100 characters of [a-zA-Z0-9-_], starting alphanumeric.
    return f"identity-probe-{case_id.lower()}-{uuid.uuid4().hex}"


def _invoke_harness(
    ctx: _Context, client: BedrockAgentCoreClient, *, actor_id: str, session_id: str
) -> _Outcome:
    try:
        response = client.invoke_harness(
            harnessArn=ctx.harness_arn,
            qualifier=_QUALIFIER,
            runtimeSessionId=session_id,
            actorId=actor_id,
            messages=[{"role": "user", "content": [{"text": _HARNESS_QUESTION}]}],
        )
    except ClientError as error:
        status, code, message = _client_error(error)
        return _Outcome(
            accepted=False, phase="request", http_status=status, code=code, message=message
        )
    stop_reasons: list[str] = []
    stream = response["stream"]
    try:
        for raw_event in stream:
            event = cast("Mapping[str, object]", raw_event)
            stop = event.get("messageStop")
            if isinstance(stop, Mapping):
                stop_reasons.append(str(cast("Mapping[str, object]", stop).get("stopReason")))
    except ClientError as error:  # EventStreamError: the call was admitted, then failed
        status, code, message = _client_error(error)
        return _Outcome(
            accepted=True,
            phase="stream",
            http_status=status,
            code=code,
            message=message,
            stop_reasons=tuple(stop_reasons),
        )
    finally:
        stream.close()
    return _Outcome(
        accepted=True, phase="complete", http_status=_HTTP_OK, stop_reasons=tuple(stop_reasons)
    )


def _session_events(ctx: _Context, *, actor_id: str, session_id: str) -> int:
    """Count Memory events written for one exact actor and session (the side-effect check)."""
    pages = (
        ctx.session.client("bedrock-agentcore")
        .get_paginator("list_events")
        .paginate(memoryId=ctx.memory_id, sessionId=session_id, actorId=actor_id)
    )
    try:
        # Pages are fetched as they are iterated, so the count is taken inside the handler.
        return sum(len(page.get("events", [])) for page in pages)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return 0
        raise


@dataclass(frozen=True, slots=True)
class _RefusalCheck:
    """Where one negative's call landed, and the refusal it must have received."""

    actor_id: str | None
    """The Memory actor the call names; `None` when it names none, so no event can be scoped."""
    session_id: str
    expected: _ExpectedRefusal
    sigv4: bool


def _refusal(ctx: _Context, outcome: _Outcome, check: _RefusalCheck) -> _Verdict:
    """Judge a negative: refused before the stream, for the pinned reason, by the service.

    The Memory side-effect check runs only when the call named an actor: counting events under
    an actor the call never carried would pass whatever the call did.
    """
    events = (
        None
        if check.actor_id is None
        else _session_events(ctx, actor_id=check.actor_id, session_id=check.session_id)
    )
    layer = "iam" if check.sigv4 and _IAM_DENIAL.search(outcome.message or "") else "service"
    passed = (
        not outcome.accepted
        and outcome.code == check.expected.code
        and check.expected.reason.search(outcome.message or "") is not None
        and layer == "service"
        and events in {None, 0}
    )
    memory = "not checked (the call carries no actor)" if events is None else str(events)
    observed = f"{outcome.describe()}; refusing layer {layer}; memory events {memory}"
    return _Verdict(
        passed=passed,
        observed=observed,
        evidence={"outcome": asdict(outcome), "refusing_layer": layer, "memory_events": events},
    )


@dataclass(frozen=True, slots=True)
class Turn:
    """One `/api/chat` turn, reduced to what the cases judge."""

    status: int
    kinds: tuple[str, ...]
    text: str = field(repr=False)
    correlation_id: str

    @property
    def done(self) -> bool:
        """Whether the route answered 200 and the stream ended with `done`."""
        return self.status == _HTTP_OK and bool(self.kinds) and self.kinds[-1] == "done"

    def describe(self) -> str:
        """Summarize the turn's status and last stream event."""
        last = self.kinds[-1] if self.kinds else "none"
        return f"HTTP {self.status}; last event {last}"


def _chat(ctx: _Context, label: str, *, question: str, session_id: str) -> Turn:
    """One `/api/chat` turn as a persona signed in just now."""
    return chat_turn(
        ctx.origin,
        ctx.fresh_tokens(label).access_token,
        question=question,
        session_id=session_id,
        redactions=ctx.secrets,
    )


def chat_turn(
    origin: str, access: str, *, question: str, session_id: str, redactions: set[str]
) -> Turn:
    """One same-origin `/api/chat` turn with a persona's access token, read to its stream end.

    The request shape is `accept_managed_harness._same_origin_call`'s, plus `Origin`. The CSRF
    value it invents is added to `redactions`, so no evidence written later can carry it.
    """
    csrf = secrets.token_urlsafe(24)
    redactions.add(csrf)
    payload = json.dumps(
        {
            "schema_version": "ConversationRequest.v3",
            "question": question,
            "session_id": session_id,
        },
        separators=(",", ":"),
    ).encode()
    body = b""
    with HttpsSession().request(
        "POST",
        require_https(f"{origin}/api/chat"),
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-amz-content-sha256": hashlib.sha256(payload).hexdigest(),
            "Cookie": f"adl_session={access}; adl_csrf={csrf}",
            "x-adl-csrf": csrf,
            "Origin": origin,
            "Sec-Fetch-Site": "same-origin",
        },
        timeout=_ROUTE_TIMEOUT_SECONDS,
        stream=True,
    ) as response:
        # ADR-081 §2: the route mints the correlation and returns it; the probe sends none.
        correlation_id = response.headers.get("x-correlation-id", "")
        while chunk := response.raw.read(8192):
            body += chunk
        status = response.status_code
    kinds: list[str] = []
    texts: list[str] = []
    for frame in body.decode("utf-8", errors="replace").split("\n\n"):
        data = [line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:")]
        if not data:
            continue
        try:
            event: object = json.loads("\n".join(data))
        except ValueError:
            kinds.append("unparseable")
            continue
        if not isinstance(event, Mapping):
            kinds.append("unparseable")
            continue
        typed = cast("Mapping[str, object]", event)
        kind = str(typed.get("kind", "unknown"))
        kinds.append(kind)
        # ADR-083: the answer is the concatenation of the V4 `text` deltas, in stream order.
        delta = typed.get("text")
        if kind == "text" and isinstance(delta, str):
            texts.append(delta)
    return Turn(
        status=status, kinds=tuple(kinds), text="".join(texts), correlation_id=correlation_id
    )


def _persona_actors(ctx: _Context) -> list[str]:
    return [ctx.actor_id(persona) for persona in (_PERSONA_A, _PERSONA_B)]


def _sweep_earlier_codenames(ctx: _Context) -> int:
    """Delete both personas' long-term records naming an earlier run's fictional codename.

    P5 states a fresh codename each run, and Memory keeps every earlier one as a competing
    preference. On 2026-09-26 a recall answered with an earlier run's codename, which says
    nothing about isolation and fails the case.
    """
    return sweep_codenames(
        ctx.session.client("bedrock-agentcore"),
        ctx.memory_id,
        _persona_actors(ctx),
        ctx.cross_session_namespaces,
    )


def _records_containing(ctx: _Context, *, actor_id: str, token: str) -> tuple[int, int]:
    """Scan a persona's preference namespace; return (records scanned, records with token)."""
    client = ctx.session.client("bedrock-agentcore")
    namespace = f"/preferences/{actor_id}"
    scanned = 0
    matches = 0
    next_token: str | None = None
    for _ in range(_RECORD_PAGE_LIMIT):
        if next_token is None:
            response = client.list_memory_records(
                memoryId=ctx.memory_id, namespace=namespace, maxResults=100
            )
        else:
            response = client.list_memory_records(
                memoryId=ctx.memory_id, namespace=namespace, maxResults=100, nextToken=next_token
            )
        for record in response.get("memoryRecordSummaries", []):
            scanned += 1
            if token.lower() in record.get("content", {}).get("text", "").lower():
                matches += 1
        next_token = response.get("nextToken")
        if not next_token:
            break
    return scanned, matches


# ---------------------------------------------------------------------------
# P1: the token is the only path to the Harness (slice S2)
# ---------------------------------------------------------------------------


def _p1_route_turn(ctx: _Context) -> _Verdict:
    turn = _chat(ctx, _PERSONA_A, question=_HARNESS_QUESTION, session_id=_session_id("p1-1"))
    return _Verdict(
        passed=turn.done,
        observed=turn.describe(),
        evidence={
            "http_status": turn.status,
            "event_kinds": list(turn.kinds),
            "correlation_id": turn.correlation_id,
        },
    )


def _p1_sigv4_harness(ctx: _Context) -> _Verdict:
    actor_id = ctx.actor_id(_PERSONA_A)
    session_id = _session_id("p1-2")
    outcome = _invoke_harness(ctx, ctx.sigv4_agentcore(), actor_id=actor_id, session_id=session_id)
    return _refusal(
        ctx, outcome, _RefusalCheck(actor_id, session_id, _SIGV4_ON_HARNESS, sigv4=True)
    )


def _p1_id_token(ctx: _Context) -> _Verdict:
    actor_id = ctx.actor_id(_PERSONA_A)
    session_id = _session_id("p1-4")
    client = bearer_agentcore_client(
        ctx.session,
        ctx.harness_tokens(_PERSONA_A).id_token,
        read_timeout=_HARNESS_READ_TIMEOUT_SECONDS,
    )
    outcome = _invoke_harness(ctx, client, actor_id=actor_id, session_id=session_id)
    return _refusal(
        ctx, outcome, _RefusalCheck(actor_id, session_id, _ID_TOKEN_AS_BEARER, sigv4=False)
    )


def _p1_cognito_access(ctx: _Context) -> _Verdict:
    """Present the user's own token, which S2 accepted and S3 must refuse (ADR-077 §2)."""
    actor_id = ctx.actor_id(_PERSONA_A)
    session_id = _session_id("p1-7")
    client = bearer_agentcore_client(
        ctx.session,
        ctx.harness_tokens(_PERSONA_A).access_token,
        read_timeout=_HARNESS_READ_TIMEOUT_SECONDS,
    )
    outcome = _invoke_harness(ctx, client, actor_id=actor_id, session_id=session_id)
    return _refusal(
        ctx, outcome, _RefusalCheck(actor_id, session_id, _COGNITO_ACCESS_AS_BEARER, sigv4=False)
    )


def _forged_harness_token(ctx: _Context) -> str:
    """Every claim and the `kid` a real Harness token carries, signed by a key made here.

    The key is generated in this process and never published, so the only attribute that differs
    from a route-minted token is the signature.
    """
    return _forged_token(
        ctx,
        audience=_HARNESS_AUDIENCE.format(environment=ctx.environment),
        client_id=_ROUTE_CLIENT.format(environment=ctx.environment),
    )


def _forged_token(ctx: _Context, *, audience: str, client_id: str) -> str:
    """Sign the issuer's claim set and published `kid` for one audience with a key made here."""
    issuer = published_parameter(ctx.session.client("ssm"), ctx.environment, "tokenIssuerUrl")
    with HttpsSession().request("GET", require_https(f"{issuer}/jwks.json"), timeout=10) as reply:
        kid = str(reply.json()["keys"][0]["kid"])
    now = int(time.time())
    subject = unverified_claims(ctx.harness_tokens(_PERSONA_A).access_token)["sub"]
    claims = {
        "iss": issuer,
        "sub": subject,
        "aud": audience,
        "client_id": client_id,
        "act": {"sub": client_id},
        "iat": now,
        "exp": now + _FORGED_LIFETIME_SECONDS,
        "jti": uuid.uuid4().hex,
    }
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})
    ctx.secrets.add(forged)
    return forged


def _p1_foreign_key(ctx: _Context) -> _Verdict:
    actor_id = ctx.actor_id(_PERSONA_A)
    session_id = _session_id("p1-8")
    client = bearer_agentcore_client(
        ctx.session, _forged_harness_token(ctx), read_timeout=_HARNESS_READ_TIMEOUT_SECONDS
    )
    outcome = _invoke_harness(ctx, client, actor_id=actor_id, session_id=session_id)
    return _refusal(
        ctx, outcome, _RefusalCheck(actor_id, session_id, _FOREIGN_KEY_BEARER, sigv4=False)
    )


def _p1_route_exchange_as_operator(ctx: _Context) -> _Verdict:
    """Invoke the route exchange with a valid subject, as an administrator-class principal."""
    function_arn = published_parameter(
        ctx.session.client("ssm"), ctx.environment, "tokenRouteExchangeFunctionArn"
    )
    payload = json.dumps({"subject_token": ctx.harness_tokens(_PERSONA_A).access_token})
    try:
        response = ctx.session.client("lambda").invoke(
            FunctionName=function_arn, InvocationType="RequestResponse", Payload=payload.encode()
        )
    except ClientError as error:
        status, code, message = _client_error(error)
        outcome = _Outcome(
            accepted=False, phase="request", http_status=status, code=code, message=message
        )
    else:
        response["Payload"].read()
        outcome = _Outcome(accepted=True, phase="complete", http_status=_HTTP_OK)
    passed = (
        not outcome.accepted
        and outcome.code == _ROUTE_EXCHANGE_DENY.code
        and _ROUTE_EXCHANGE_DENY.reason.search(outcome.message or "") is not None
    )
    return _Verdict(
        passed=passed,
        observed=outcome.describe(),
        evidence={"outcome": asdict(outcome)},
    )


def _p1_sigv4_runtime(ctx: _Context) -> _Verdict:
    harness_id = ctx.harness_arn.rsplit("/", maxsplit=1)[-1]
    harness = ctx.session.client("bedrock-agentcore-control").get_harness(harnessId=harness_id)
    runtime_arn = (
        harness.get("harness", {})
        .get("environment", {})
        .get("agentCoreRuntimeEnvironment", {})
        .get("agentRuntimeArn")
    )
    if not runtime_arn or ":runtime/" not in runtime_arn:
        return _Verdict(passed=False, observed="GetHarness returned no underlying runtime ARN")
    session_id = _session_id("p1-3")
    try:
        response = ctx.sigv4_agentcore().invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            qualifier=_QUALIFIER,
            runtimeSessionId=session_id,
            contentType="application/json",
            accept="application/json",
            payload=json.dumps({"prompt": _HARNESS_QUESTION}).encode(),
        )
    except ClientError as error:
        status, code, message = _client_error(error)
        outcome = _Outcome(
            accepted=False, phase="request", http_status=status, code=code, message=message
        )
    else:
        body = response["response"]
        try:
            body.read()
        finally:
            body.close()
        outcome = _Outcome(accepted=True, phase="complete", http_status=_HTTP_OK)
    # InvokeAgentRuntime takes no actorId, so no Memory event can be scoped to this call.
    return _refusal(ctx, outcome, _RefusalCheck(None, session_id, _SIGV4_ON_RUNTIME, sigv4=True))


def _refused(expected: _ExpectedRefusal) -> str:
    return (
        f"refused before the stream with {expected.describe()} by the service, not IAM; "
        "no Memory event written"
    )


def _refused_without_actor(expected: _ExpectedRefusal) -> str:
    return (
        f"refused before the stream with {expected.describe()} by the service, not IAM; the "
        "runtime call carries no actor, so the refusal is the whole assertion"
    )


# Declared once and reused by S5, whose own positive control it then depends on.
_ROUTE_EXCHANGE_CASE = Case(
    "P1-9",
    "Invoke the route exchange as the administrator-class operator with a valid subject: "
    f"refused with {_ROUTE_EXCHANGE_DENY.describe()}",
    ("P1-1",),
    _p1_route_exchange_as_operator,
)

# Execution order is table order. The positive is the real product path (P1-1): its Harness call
# carries the route-minted token, and since P1-7 shows the Harness refuses a Cognito token, a done
# turn is only possible through the token service. Every negative runs only after it passes.
P1_CASES: tuple[Case, ...] = (
    Case("P1-1", "probe-a /api/chat turn: HTTP 200, stream ends with done", (), _p1_route_turn),
    Case(
        "P1-7",
        "InvokeHarness with probe-a's own Cognito access token as bearer: "
        f"{_refused(_COGNITO_ACCESS_AS_BEARER)}",
        ("P1-1",),
        _p1_cognito_access,
    ),
    Case(
        "P1-4",
        f"InvokeHarness with probe-a ID token as bearer: {_refused(_ID_TOKEN_AS_BEARER)}",
        ("P1-1",),
        _p1_id_token,
    ),
    Case(
        "P1-8",
        "InvokeHarness with a token carrying every Harness-token claim and the published kid, "
        f"signed by an unpublished key: {_refused(_FOREIGN_KEY_BEARER)}",
        ("P1-1",),
        _p1_foreign_key,
    ),
    Case(
        "P1-2",
        f"InvokeHarness signed with operator SigV4: {_refused(_SIGV4_ON_HARNESS)}",
        ("P1-1",),
        _p1_sigv4_harness,
    ),
    Case(
        "P1-3",
        "InvokeAgentRuntime on the Harness's underlying runtime with operator SigV4: "
        f"{_refused_without_actor(_SIGV4_ON_RUNTIME)}",
        ("P1-1",),
        _p1_sigv4_runtime,
    ),
    _ROUTE_EXCHANGE_CASE,
)


# ---------------------------------------------------------------------------
# P5: per-user Memory recall across conversations
# ---------------------------------------------------------------------------


def _p5_state_preference(ctx: _Context) -> _Verdict:
    swept = _sweep_earlier_codenames(ctx)
    token = f"ZEPHYRINE-{uuid.uuid4().hex[:6]}"
    ctx.state["token"] = token
    turn = _chat(
        ctx,
        _PERSONA_A,
        question=(
            "Please remember this preference for all my future conversations: label every "
            f"report you give me with my fictional project codename {token}."
        ),
        session_id=_session_id("p5-1"),
    )
    return _Verdict(
        passed=turn.done,
        observed=f"{turn.describe()}; token {token}; earlier codename records swept {swept}",
        evidence={
            "http_status": turn.status,
            "event_kinds": list(turn.kinds),
            "correlation_id": turn.correlation_id,
            "token": token,
            "earlier_codename_records_swept": swept,
        },
    )


def _retrievable_with_token(ctx: _Context, *, actor_id: str, token: str) -> int:
    """Count records the recall question's semantic search returns that carry the token.

    This is the Harness's own precondition: it injects what retrieval returns, not what listing
    shows. On 2026-09-26 a recall turn issued as soon as the record was listable did not name the
    token, and the same question a few minutes later did. Whether retrieval lagged listing or the
    model varied was not isolated, so the probe waits on the precondition it can observe.
    """
    response = ctx.session.client("bedrock-agentcore").retrieve_memory_records(
        memoryId=ctx.memory_id,
        namespace=f"/preferences/{actor_id}",
        searchCriteria={"searchQuery": _RECALL_QUESTION, "topK": 10},
    )
    return sum(
        token.lower() in record.get("content", {}).get("text", "").lower()
        for record in response.get("memoryRecordSummaries", [])
    )


def _p5_record_extracted(ctx: _Context) -> _Verdict:
    token = ctx.state["token"]
    actor_id = ctx.actor_id(_PERSONA_A)
    started = time.monotonic()
    deadline = started + ctx.memory_timeout_seconds

    def observe() -> tuple[int, int, int]:
        scanned, matches = _records_containing(ctx, actor_id=actor_id, token=token)
        retrievable = _retrievable_with_token(ctx, actor_id=actor_id, token=token) if matches else 0
        return scanned, matches, retrievable

    scanned, matches, retrievable = observe()
    while retrievable == 0 and time.monotonic() < deadline:
        time.sleep(_RECORD_POLL_INTERVAL_SECONDS)
        scanned, matches, retrievable = observe()
    waited = round(time.monotonic() - started)
    return _Verdict(
        passed=matches >= 1 and retrievable >= 1,
        observed=(
            f"{matches} of {scanned} records in /preferences/<probe-a actor> contain the token; "
            f"{retrievable} returned by the recall query's retrieval after {waited}s "
            f"(timeout {ctx.memory_timeout_seconds}s)"
        ),
        evidence={
            "records_scanned": scanned,
            "records_with_token": matches,
            "retrievable_with_token": retrievable,
            "waited_seconds": waited,
        },
    )


# Phrased as a reporting request, like P5-1: on 2026-09-26 the dev input guardrail declined the
# bare "what codename did I ask you" form (input_intervened) before the Harness ran, which tests
# the guardrail, not Memory.
_CODENAME = CODENAME
_RECALL_QUESTION = (
    "Which project codename should my campaign performance reports be labelled with? "
    "Reply with the codename."
)


def _p5_same_user_recall(ctx: _Context) -> _Verdict:
    token = ctx.state["token"]
    turn = _chat(ctx, _PERSONA_A, question=_RECALL_QUESTION, session_id=_session_id("p5-3"))
    recalled = token.lower() in turn.text.lower()
    # Fictional codenames only; naming which ones the answer used separates "recalled an
    # earlier run's preference" from "recalled nothing".
    named = sorted(set(_CODENAME.findall(turn.text)))
    return _Verdict(
        passed=turn.done and recalled,
        observed=f"{turn.describe()}; answer contains token: {recalled}; codenames named {named}",
        evidence={
            "http_status": turn.status,
            "event_kinds": list(turn.kinds),
            "correlation_id": turn.correlation_id,
            "recalled": recalled,
            "codenames_named": named,
        },
    )


def _p5_other_user_isolation(ctx: _Context) -> _Verdict:
    token = ctx.state["token"]
    turn = _chat(ctx, _PERSONA_B, question=_RECALL_QUESTION, session_id=_session_id("p5-4"))
    leaked = token.lower() in turn.text.lower()
    scanned, matches = _records_containing(ctx, actor_id=ctx.actor_id(_PERSONA_B), token=token)
    return _Verdict(
        passed=turn.done and not leaked and matches == 0,
        observed=(
            f"{turn.describe()}; answer contains token: {leaked}; "
            f"{matches} of {scanned} records in /preferences/<probe-b actor> contain it"
        ),
        evidence={
            "http_status": turn.status,
            "event_kinds": list(turn.kinds),
            "correlation_id": turn.correlation_id,
            "leaked": leaked,
            "records_scanned": scanned,
            "records_with_token": matches,
        },
    )


P5_CASES: tuple[Case, ...] = (
    Case(
        "P5-1",
        "probe-a conversation 1 states a preference: HTTP 200, stream ends with done",
        (),
        _p5_state_preference,
    ),
    Case(
        "P5-2",
        "a record containing the token appears in probe-a's /preferences/<actor> namespace "
        "and the recall query's retrieval returns it",
        ("P5-1",),
        _p5_record_extracted,
    ),
    Case(
        "P5-3",
        "probe-a conversation 2 (new session) asks: done, and the answer contains the token",
        ("P5-2",),
        _p5_same_user_recall,
    ),
    Case(
        "P5-4",
        "probe-b asks the same: done, the answer does NOT contain the token, and "
        "probe-b's namespace holds no record of it",
        ("P5-3",),
        _p5_other_user_isolation,
    ),
)

# ---------------------------------------------------------------------------
# S5: the Gateway and every tool admit only the exchanged Gateway token
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LogWindow:
    """An inclusive CloudWatch Logs time range in epoch milliseconds, widened for clock skew."""

    start_ms: int
    end_ms: int

    @classmethod
    def around(cls, start_ms: int, end_ms: int) -> LogWindow:
        """Widen a local start-to-end range on both sides."""
        return cls(start_ms - _LOG_SKEW_MS, end_ms + _LOG_SKEW_MS)


def now_ms() -> int:
    """Wall-clock epoch milliseconds, the unit CloudWatch Logs timestamps use."""
    return int(time.time() * 1000)


def log_messages(
    session: boto3.Session, *, function_name: str, window: LogWindow, pattern: str
) -> list[str]:
    """Every message one function logged in the window that matches a Logs filter pattern.

    Paginated to the end: `FilterLogEvents` can return an empty page that still has a next token.
    """
    messages: list[str] = []
    paginator = session.client("logs").get_paginator("filter_log_events")
    for page in paginator.paginate(
        logGroupName=f"/aws/lambda/{function_name}",
        startTime=window.start_ms,
        endTime=window.end_ms,
        filterPattern=pattern,
    ):
        messages.extend(event.get("message", "") for event in page.get("events", []))
    return messages


def terminal_records(
    session: boto3.Session, *, function_name: str, window: LogWindow
) -> list[Mapping[str, object]]:
    """Return the structured `phase: terminal` records one function logged in the window."""
    records: list[Mapping[str, object]] = []
    for message in log_messages(
        session, function_name=function_name, window=window, pattern=_TERMINAL_PATTERN
    ):
        try:
            record: object = json.loads(message)
        except ValueError:
            continue
        if isinstance(record, Mapping):
            records.append(cast("Mapping[str, object]", record))
    return records


def deployed_function_name(environment: str, logical_id: str) -> str:
    """Name a deployed stack function: `pythonFunction(id)` is `adlytics-<env>-<id>`."""
    return f"adlytics-{environment}-{logical_id}"


@dataclass(frozen=True, slots=True)
class _Tool:
    """One Gateway Lambda tool: its function, a real tool it serves, and how it refuses."""

    label: str
    logical_id: str
    tool_name: str
    arguments: Mapping[str, str]
    refusal_operation: str

    @property
    def gateway_tool_name(self) -> str:
        """The prefixed name the Gateway sends this tool's function."""
        # Gateway prefixes the target name with three underscores (gateway.TOOL_NAME_DELIMITER).
        return f"{self.label}___{self.tool_name}"


# Target names are AgentStack's `gatewayTargetName`s; tool names are the handlers' enums. The
# refusal operation is the terminal record's `operation`: both name the fixed `unverified_caller`
# dimension, because nothing of an unverified call is read (reporting.py). ADR-083 retired the
# `presentation` finalizer tool, so catalog and measurement are the only Gateway tools.
_TOOLS: tuple[_Tool, ...] = (
    _Tool("catalog", "CatalogTool", "catalog_list_metrics", {}, "unverified_caller"),
    _Tool("measurement", "MeasurementTool", "measurement_metric_by_grain", {}, "unverified_caller"),
)
_INTERCEPTOR = "GatewayInterceptor"
_ISSUER = "TokenIssuer"
_GATEWAY_AUDIENCE = "adlytics-{environment}-gateway"
_AGENT_CLIENT = "adlytics-{environment}-agent"
_TERMINAL_PATTERN = '{ $.phase = "terminal" }'
_LOG_SKEW_MS = 2_000
_LOG_POLL_SECONDS = 10
_LOG_TIMEOUT_SECONDS = 240
_ABSENCE_SETTLE_SECONDS = 30
_MCP_TIMEOUT_SECONDS = 30
_TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"  # noqa: S105
_JWT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"  # noqa: S105
_CALLER_UNVERIFIED = "ADLY-CALLER-0001"
_CALLER_UNVERIFIED_REASON = re.compile(
    r"^This tool call could not be attributed to a verified user, so it was not run\.$"
)
# The operator's read of the agent client secret meets the secret's resource-policy Deny, not a
# missing identity grant: the operator holds administrator-class permissions (ADR-077 §3).
_SECRET_DENY = _ExpectedRefusal(
    "AccessDeniedException", re.compile(r"explicit deny in a resource-based policy")
)
# The route's multi-tool question from accept_managed_harness: one turn that reaches catalog and
# measurement, so every tool's negatives follow a verified call to that tool.
TOOL_QUESTION = (
    "First explain retail_revenue_shipped, then report retail_revenue_shipped by product in the "
    "United States marketplace on August 11, 2026."
)


@dataclass(frozen=True, slots=True)
class _ExpectedHttpRefusal:
    """The exact HTTP refusal one negative must observe: status, body, and bearer challenge."""

    status: int
    body: re.Pattern[str]
    challenge: re.Pattern[str] = re.compile(r"^$")
    """The `WWW-Authenticate` value; by default the refusal must carry none."""

    def describe(self) -> str:
        """Name the pinned status, body, and challenge."""
        challenge = (
            f" and WWW-Authenticate matching '{self.challenge.pattern}'"
            if self.challenge.pattern != "^$"
            else ""
        )
        return f"HTTP {self.status} with body matching '{self.body.pattern}'{challenge}"

    def matches(self, reply: _HttpReply) -> bool:
        """Whether one reply is exactly this refusal."""
        return (
            reply.status == self.status
            and self.body.search(reply.body) is not None
            and self.challenge.search(reply.www_authenticate) is not None
        )


# Observed 2026-09-26 against the dev Gateway: every bearer defect is refused by the CUSTOM_JWT
# authorizer before the interceptor runs (no interceptor or tool record in the case window). A
# missing bearer is a 401 `invalid_token`; every presented bearer that does not verify, whatever
# its defect (issuer and audience for a Cognito token, signature for a foreign key, audience and
# client and signature for a Harness-shaped one), gets one undifferentiated 403
# `insufficient_scope`, so the Gateway's response cannot show which check refused it. The JSON-RPC
# id is not pinned: the Gateway answers id 0 rather than echoing the request's.
_MCP_NO_BEARER = _ExpectedHttpRefusal(
    401,
    re.compile(
        r'^\{"jsonrpc":"2\.0","id":[^,]+,'
        r'"error":\{"code":-32001,"message":"Missing Bearer token"\}\}$'
    ),
    re.compile(r'^Bearer error="invalid_token", resource_metadata="'),
)
_MCP_UNVERIFIED_BEARER = _ExpectedHttpRefusal(
    403,
    re.compile(
        r'^\{"jsonrpc":"2\.0","id":[^,]+,"error":\{"code":-32002,"message":"insufficient_scope - '
        r'The request requires higher privileges than provided by the access token\."\}\}$'
    ),
    re.compile(r'^Bearer error="insufficient_scope", resource_metadata="'),
)
_MCP_COGNITO_BEARER = _MCP_UNVERIFIED_BEARER
_MCP_FOREIGN_KEY = _MCP_UNVERIFIED_BEARER
_MCP_HARNESS_AUDIENCE = _MCP_UNVERIFIED_BEARER
_GATEWAY_AUTHORIZER = "gateway_authorizer"
# The issuer answers a wrong client secret with RFC 6749 invalid_client and logs the reason.
_TOKEN_WRONG_SECRET = _ExpectedHttpRefusal(401, re.compile(r'^\{"error":"invalid_client"\}$'))
# pragma: allowlist nextline secret (A-PROTOCOL-CONSTANT-001)
_TOKEN_WRONG_SECRET_REASON = "The presented client authentication does not match."  # noqa: S105


def _tool_records(ctx: _Context, window: LogWindow) -> dict[str, list[Mapping[str, object]]]:
    """Every tool's Gateway-target terminal records in the window, by tool label."""
    return {
        tool.label: [
            record
            for record in terminal_records(
                ctx.session,
                function_name=deployed_function_name(ctx.environment, tool.logical_id),
                window=window,
            )
            if record.get("component") == "gateway_target"
        ]
        for tool in _TOOLS
    }


def _issued(ctx: _Context, window: LogWindow) -> int:
    """Count the issuer's `/token` exchanges that ended `issued` in the window."""
    return sum(
        record.get("outcome") == "issued"
        for record in terminal_records(
            ctx.session,
            function_name=deployed_function_name(ctx.environment, _ISSUER),
            window=window,
        )
    )


def _forwarded(ctx: _Context, window: LogWindow) -> int:
    """Count the interceptor's terminal records that ended `forwarded` in the window.

    `forwarded` is the one outcome a `tools/call` carrying a bearer reaches; every other method
    passes through. It is the positive control for the S5 Gateway negatives' "no interceptor
    record": the same read is shown to find a record when one exists.
    """
    return sum(
        record.get("outcome") == "forwarded"
        for record in terminal_records(
            ctx.session,
            function_name=deployed_function_name(ctx.environment, _INTERCEPTOR),
            window=window,
        )
    )


_ALL_TOOL_LABELS = sorted(tool.label for tool in _TOOLS)
"""S5-1's turn reaches every tool (see `TOOL_QUESTION`), so each must verify and attribute."""


def _s5_tool_turn(ctx: _Context) -> _Verdict:
    """Drive the known-good path: a persona tool turn whose every tool call verified its caller."""
    started = now_ms()
    turn = _chat(ctx, _PERSONA_A, question=TOOL_QUESTION, session_id=_session_id("s5-1"))
    window = LogWindow.around(started, now_ms())
    waited_from = time.monotonic()
    deadline = waited_from + _LOG_TIMEOUT_SECONDS
    previous: tuple[int, int, int] | None = None
    while True:
        by_tool = _tool_records(ctx, window)
        issued = _issued(ctx, window)
        forwarded = _forwarded(ctx, window)
        current = (sum(len(records) for records in by_tool.values()), issued, forwarded)
        if (all(current) and current == previous) or time.monotonic() > deadline:
            break
        previous = current
        time.sleep(_LOG_POLL_SECONDS)
    visible_after = round(time.monotonic() - waited_from)
    subject = ctx.subject(_PERSONA_A)

    # A tool record says only that its caller verified; the user is never in tool telemetry.
    def verified(record: Mapping[str, object]) -> bool:
        return record.get("caller_verified") is True and subject not in json.dumps(record)

    summary = {
        label: [
            {
                "operation": record.get("operation"),
                "result_class": record.get("result_class"),
                "caller_verified": record.get("caller_verified"),
                "free_of_the_subject": subject not in json.dumps(record),
            }
            for record in records
        ]
        for label, records in by_tool.items()
    }
    all_records = [record for records in by_tool.values() for record in records]
    verified_calls = [verified(record) for record in all_records]
    verified_tools = sorted(
        label
        for label, records in by_tool.items()
        if records and all(verified(record) for record in records)
    )
    # ADR-077 §6: the durable attribution is the turn's audit receipt, which must name this
    # persona's own `sub`. Kept as booleans; the `sub` itself never enters the evidence.
    receipt = _receipt(ctx, turn.correlation_id)
    attributed = (
        receipt is not None
        and receipt.receipt.runtime_version == MANAGED_HARNESS_RUNTIME_V3
        and receipt.principal == subject
        and any(step.governed for step in receipt.receipt.trajectory)
    )
    ctx.state["s5_verified_tools"] = ",".join(verified_tools)
    ctx.state["s5_log_visible_seconds"] = str(visible_after)
    counts = {label: len(records) for label, records in by_tool.items()}
    every_tool = verified_tools == _ALL_TOOL_LABELS
    return _Verdict(
        passed=(
            turn.done
            and bool(all_records)
            and all(verified_calls)
            and every_tool
            and issued >= 1
            and forwarded >= 1
            and attributed
        ),
        observed=(
            f"{turn.describe()}; tool terminal records {counts}, verified without naming the "
            f"user on {sum(verified_calls)} of {len(verified_calls)}; tools called and verified "
            f"{verified_tools} of {_ALL_TOOL_LABELS}; issuer /token issued {issued}; "
            f"interceptor forwarded tools/call {forwarded}; audit receipt found "
            f"{receipt is not None}, attributed to the persona with governed steps {attributed}; "
            f"logs settled after {visible_after}s"
        ),
        evidence={
            "http_status": turn.status,
            "event_kinds": list(turn.kinds),
            "correlation_id": turn.correlation_id,
            "tool_records": summary,
            "issued": issued,
            "verified_tools": verified_tools,
            "every_tool_verified": every_tool,
            "interceptor_forwarded": forwarded,
            "receipt_found": receipt is not None,
            "receipt_runtime_version": (
                None if receipt is None else receipt.receipt.runtime_version
            ),
            "receipt_attributed_to_persona": attributed,
        },
    )


@dataclass(frozen=True, slots=True)
class _AttributedReceipt:
    """The turn's receipt as the review API returns it to a caller entitled to attribution."""

    receipt: ReviewedReceipt
    principal: str | None = field(repr=False)


def _receipt(ctx: _Context, correlation_id: str | None) -> _AttributedReceipt | None:
    """Read the turn's receipt and principal through the review API (ADR-078 §5).

    The receipts table denies every reader but its named roles, so the probe asks as the
    attributor persona, the one caller that receives the `attribution` section.
    """
    if not correlation_id:
        return None
    review_client = reviewer_client(ctx.session, ctx.environment, ATTRIBUTOR_PERSONA)
    deadline = time.monotonic() + _LOG_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        review = review_client.turn(correlation_id)
        if review is not None:
            return _AttributedReceipt(reviewed_receipt(review), reviewed_principal(review))
        time.sleep(_LOG_POLL_SECONDS)
    return None


@dataclass(frozen=True, slots=True)
class _HttpReply:
    """One HTTP response, bounded; the body is redacted before it is recorded."""

    status: int
    body: str
    www_authenticate: str

    def describe(self) -> str:
        challenge = f"; WWW-Authenticate {self.www_authenticate}" if self.www_authenticate else ""
        return f"HTTP {self.status} body {self.body}{challenge}"


def _mcp_tools_call(ctx: _Context, bearer: str | None) -> _HttpReply:
    """POST one MCP `tools/call` for a real tool to the Gateway, with or without a bearer."""
    gateway_url = require_https(
        published_parameter(ctx.session.client("ssm"), ctx.environment, "gatewayUrl")
    )
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    payload = {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "tools/call",
        "params": {"name": _TOOLS[0].gateway_tool_name, "arguments": dict(_TOOLS[0].arguments)},
    }
    with HttpsSession().request(
        "POST", gateway_url, json=payload, headers=headers, timeout=_MCP_TIMEOUT_SECONDS
    ) as response:
        return _HttpReply(
            status=response.status_code,
            body=ctx.redact(response.text[:_OBSERVED_MESSAGE_LIMIT]),
            www_authenticate=ctx.redact(response.headers.get("WWW-Authenticate", "")),
        )


def _absence_after(
    ctx: _Context, window: LogWindow
) -> tuple[list[Mapping[str, object]], dict[str, int]]:
    """Wait out log ingestion, then read the interceptor's and every tool's records in `window`.

    Absence is only evidence once ingestion has caught up: the wait is the longer of a floor and
    twice the delay S5-1 observed before its own records were visible.
    """
    observed = int(ctx.state.get("s5_log_visible_seconds", "0"))
    time.sleep(max(_ABSENCE_SETTLE_SECONDS, 2 * observed))
    interceptor = terminal_records(
        ctx.session,
        function_name=deployed_function_name(ctx.environment, _INTERCEPTOR),
        window=window,
    )
    tools = {label: len(records) for label, records in _tool_records(ctx, window).items()}
    return interceptor, tools


def _mcp_refusal(ctx: _Context, bearer: str | None, expected: _ExpectedHttpRefusal) -> _Verdict:
    """Judge a Gateway negative: pinned status and body, refused before the interceptor runs."""
    started = now_ms()
    reply = _mcp_tools_call(ctx, bearer)
    window = LogWindow.around(started, now_ms())
    interceptor, tools = _absence_after(ctx, window)
    if any(tools.values()):
        layer = "tool"
    elif any(record.get("outcome") == "unauthorized" for record in interceptor):
        layer = "interceptor"
    elif interceptor:
        layer = "after_interceptor"
    else:
        layer = _GATEWAY_AUTHORIZER
    passed = expected.matches(reply) and layer == _GATEWAY_AUTHORIZER and not any(tools.values())
    return _Verdict(
        passed=passed,
        observed=(
            f"{reply.describe()}; refusing layer {layer}; interceptor records "
            f"{len(interceptor)}; tool records {tools}"
        ),
        evidence={
            "http_status": reply.status,
            "body": reply.body,
            "www_authenticate": reply.www_authenticate,
            "refusing_layer": layer,
            "interceptor_outcomes": [record.get("outcome") for record in interceptor],
            "tool_records": tools,
        },
    )


def _forged_gateway_token(ctx: _Context) -> str:
    """Every claim and the `kid` a real Gateway token carries, signed by a key made here."""
    agent = _AGENT_CLIENT.format(environment=ctx.environment)
    return _forged_token(
        ctx, audience=_GATEWAY_AUDIENCE.format(environment=ctx.environment), client_id=agent
    )


def _s5_mcp_no_bearer(ctx: _Context) -> _Verdict:
    return _mcp_refusal(ctx, None, _MCP_NO_BEARER)


def _s5_mcp_cognito(ctx: _Context) -> _Verdict:
    return _mcp_refusal(ctx, ctx.harness_tokens(_PERSONA_A).access_token, _MCP_COGNITO_BEARER)


def _s5_mcp_foreign_key(ctx: _Context) -> _Verdict:
    return _mcp_refusal(ctx, _forged_gateway_token(ctx), _MCP_FOREIGN_KEY)


def _s5_mcp_harness_audience(ctx: _Context) -> _Verdict:
    return _mcp_refusal(ctx, _forged_harness_token(ctx), _MCP_HARNESS_AUDIENCE)


def _client_context(tool: _Tool, message_id: str, bearer: str | None) -> str:
    """Encode a Gateway-shaped client context: the keys a tool reads, and any bearer."""
    custom: dict[str, object] = {
        "bedrockAgentCoreToolName": tool.gateway_tool_name,
        "bedrockAgentCoreMcpMessageId": message_id,
    }
    if bearer is not None:
        custom["bedrockAgentCorePropagatedHeaders"] = {"Authorization": f"Bearer {bearer}"}
    return base64.b64encode(json.dumps({"custom": custom}).encode()).decode()


def _record_for(
    ctx: _Context, tool: _Tool, *, window: LogWindow, message_id: str
) -> list[Mapping[str, object]]:
    """Poll until the tool's terminal record for one probe message id is visible."""
    deadline = time.monotonic() + _LOG_TIMEOUT_SECONDS
    while True:
        matched = [
            record
            for record in terminal_records(
                ctx.session,
                function_name=deployed_function_name(ctx.environment, tool.logical_id),
                window=window,
            )
            if record.get("gateway_message_ref") == message_id
        ]
        if matched or time.monotonic() > deadline:
            return matched
        time.sleep(_LOG_POLL_SECONDS)


def _payload_refusal(function_error: str | None, body: object) -> tuple[bool, str]:
    """Judge an invoke result against the pinned `ADLY-CALLER-0001` refusal envelope."""
    document: Mapping[str, object] = (
        cast("Mapping[str, object]", body) if isinstance(body, Mapping) else {}
    )
    code = document.get("code")
    reason = str(document.get("reason", ""))
    matched = (
        function_error is None
        and document.get("refused") is True
        and code == _CALLER_UNVERIFIED
        and _CALLER_UNVERIFIED_REASON.search(reason) is not None
    )
    return (
        matched,
        f"FunctionError {function_error}; refused {document.get('refused')} {code}: {reason}",
    )


def _direct_tool_refusal(ctx: _Context, tool: _Tool, bearer: str | None) -> _Verdict:
    """Invoke one tool Lambda as the operator with a fabricated Gateway context; judge the refusal.

    The positive control is S5-1's verified Gateway call to this same tool. The refusal must be
    the pinned `ADLY-CALLER-0001`, and the tool's only terminal record for this message id must
    say the caller did not verify and that routing never ran (its refusal operation), which is
    the no-side-effect evidence: a tool reads no argument and touches no data before that check.
    """
    if tool.label not in ctx.state.get("s5_verified_tools", "").split(","):
        return _Verdict(
            passed=False,
            observed=f"positive control absent: S5-1 made no verified Gateway call to {tool.label}",
        )
    message_id = f"identity-probe-{uuid.uuid4().hex}"
    started = now_ms()
    try:
        response = ctx.session.client("lambda").invoke(
            FunctionName=deployed_function_name(ctx.environment, tool.logical_id),
            InvocationType="RequestResponse",
            ClientContext=_client_context(tool, message_id, bearer),
            Payload=json.dumps(dict(tool.arguments)).encode(),
        )
    except ClientError as error:
        status, code, message = _client_error(error)
        return _Verdict(
            passed=False,
            observed=f"invoke refused before the tool ran: HTTP {status} {code}: {message}",
        )
    body: object = json.loads(response["Payload"].read() or b"null")
    function_error = response.get("FunctionError")
    window = LogWindow.around(started, now_ms())
    matched, described = _payload_refusal(function_error, body)
    records = _record_for(ctx, tool, window=window, message_id=message_id)
    record: Mapping[str, object] = records[0] if len(records) == 1 else {}
    logged = (
        len(records) == 1
        and record.get("caller_verified") is False
        and record.get("result_class") == "caller_unverified"
        and record.get("operation") == tool.refusal_operation
        # Nothing about an unverified bearer is trusted, so no claim of it reaches the log.
        and ctx.subject(_PERSONA_A) not in json.dumps(record)
    )
    return _Verdict(
        passed=matched and logged,
        observed=(
            f"{described}; terminal records for the probe message {len(records)}: "
            f"operation {record.get('operation')}, result_class {record.get('result_class')}, "
            f"caller_verified {record.get('caller_verified')}"
        ),
        evidence={
            "function_error": function_error,
            "payload_matched": matched,
            "terminal_records": [
                {
                    key: item.get(key)
                    for key in ("operation", "outcome", "result_class", "caller_verified")
                }
                for item in records
            ],
        },
    )


def _direct_case(tool: _Tool, *, forged: bool) -> Callable[[_Context], _Verdict]:
    def run(ctx: _Context) -> _Verdict:
        return _direct_tool_refusal(ctx, tool, _forged_gateway_token(ctx) if forged else None)

    return run


def _s5_secret_read(ctx: _Context) -> _Verdict:
    """Read the agent client secret as the operator; only its resource-policy Deny may refuse."""
    secret_arn = published_parameter(
        ctx.session.client("ssm"), ctx.environment, "agentClientSecretArn"
    )
    try:
        value = ctx.session.client("secretsmanager").get_secret_value(SecretId=secret_arn)
    except ClientError as error:
        status, code, message = _client_error(error)
        outcome = _Outcome(
            accepted=False, phase="request", http_status=status, code=code, message=message
        )
    else:
        # Never recorded: the value is added to the redaction set before anything is printed.
        ctx.secrets.add(value.get("SecretString", ""))
        outcome = _Outcome(accepted=True, phase="complete", http_status=_HTTP_OK)
    passed = (
        not outcome.accepted
        and outcome.code == _SECRET_DENY.code
        and _SECRET_DENY.reason.search(outcome.message or "") is not None
    )
    return _Verdict(
        passed=passed, observed=outcome.describe(), evidence={"outcome": asdict(outcome)}
    )


def _s5_token_wrong_secret(ctx: _Context) -> _Verdict:
    """POST `/token` as the registered agent client with a secret made here.

    The issuer authenticates the client before it reads the grant or the subject, so the subject
    (a probe-forged Harness-shaped token) is never examined; the issuer's terminal record names
    the client-authentication reason, and no exchange in the window ends `issued` (no mint).
    """
    issuer = published_parameter(ctx.session.client("ssm"), ctx.environment, "tokenIssuerUrl")
    wrong_secret = secrets.token_urlsafe(32)
    ctx.secrets.add(wrong_secret)
    form = {
        "grant_type": _TOKEN_EXCHANGE_GRANT,
        "client_id": _AGENT_CLIENT.format(environment=ctx.environment),
        "client_secret": wrong_secret,
        "subject_token": _forged_harness_token(ctx),
        "subject_token_type": _JWT_TOKEN_TYPE,
    }
    started = now_ms()
    with HttpsSession().request(
        "POST", require_https(f"{issuer}/token"), data=form, timeout=_MCP_TIMEOUT_SECONDS
    ) as response:
        reply = _HttpReply(
            status=response.status_code,
            body=ctx.redact(response.text[:_OBSERVED_MESSAGE_LIMIT]),
            www_authenticate=ctx.redact(response.headers.get("WWW-Authenticate", "")),
        )
    window = LogWindow.around(started, now_ms())
    deadline = time.monotonic() + _LOG_TIMEOUT_SECONDS
    while True:
        records = terminal_records(
            ctx.session,
            function_name=deployed_function_name(ctx.environment, _ISSUER),
            window=window,
        )
        refusals = [record for record in records if record.get("outcome") == "invalid_client"]
        if refusals or time.monotonic() > deadline:
            break
        time.sleep(_LOG_POLL_SECONDS)
    issued = sum(record.get("outcome") == "issued" for record in records)
    reasons = sorted({str(record.get("description")) for record in refusals})
    passed = (
        _TOKEN_WRONG_SECRET.matches(reply)
        and reasons == [_TOKEN_WRONG_SECRET_REASON]
        and issued == 0
    )
    return _Verdict(
        passed=passed,
        observed=(
            f"{reply.describe()}; issuer invalid_client records {len(refusals)} naming {reasons}; "
            f"issued in window {issued}"
        ),
        evidence={
            "http_status": reply.status,
            "body": reply.body,
            "issuer_outcomes": [record.get("outcome") for record in records],
            "issuer_reasons": reasons,
            "issued": issued,
        },
    )


def _mcp_expected(what: str, expected: _ExpectedHttpRefusal) -> str:
    return (
        f"Gateway MCP tools/call with {what}: {expected.describe()}, refused by the Gateway "
        "CUSTOM_JWT authorizer (no interceptor record), no tool invoked"
    )


def _direct_expected(tool: _Tool, what: str) -> str:
    shape = f"refusal envelope with code {_CALLER_UNVERIFIED}"
    return (
        f"operator lambda:Invoke of the {tool.label} tool with a Gateway-shaped context and "
        f"{what}: {shape}, refused by the tool's caller check; one terminal record, "
        f"caller_verified false and the persona's sub absent, result_class caller_unverified, "
        f"operation {tool.refusal_operation}"
    )


# Execution order is table order. S5-1 is the known-good request every negative varies: the
# Harness's own Gateway calls carry an exchanged token and every tool verifies it. The Gateway
# negatives vary only the bearer; the direct invokes vary only the transport (a fabricated
# context instead of the Gateway's) and then the bearer's key. A `/token` request with a Cognito
# subject and the correct client secret is covered only by unit tests with real cryptography:
# the secret is unreadable to the operator by design (S5-12), so no live request can present it.
_DIRECT_CASES: tuple[Case, ...] = tuple(
    Case(
        f"S5-{6 + 2 * index + offset}",
        _direct_expected(
            tool,
            "a propagated bearer carrying every Gateway-token claim and the published kid, "
            "signed by an unpublished key"
            if forged
            else "no propagated headers",
        ),
        ("S5-1",),
        _direct_case(tool, forged=forged),
    )
    for index, tool in enumerate(_TOOLS)
    for offset, forged in enumerate((False, True))
)
S5_CASES: tuple[Case, ...] = (
    Case(
        "S5-1",
        "probe-a /api/chat tool turn: done; every tool terminal record in the turn window has "
        "caller_verified true and does not contain probe-a's sub, and catalog and measurement "
        "each have at least one such record; the issuer logged at least one "
        "/token exchange issued; the Gateway interceptor logged at least one terminal record "
        "ending forwarded (a tools/call it passed on with the bearer); and the turn's audit "
        "receipt is a managed-harness.v3 receipt whose principal is probe-a's own sub, with at "
        "least one governed tool step",
        (),
        _s5_tool_turn,
    ),
    Case("S5-2", _mcp_expected("no Authorization", _MCP_NO_BEARER), ("S5-1",), _s5_mcp_no_bearer),
    Case(
        "S5-3",
        _mcp_expected("probe-a's Cognito access token as bearer", _MCP_COGNITO_BEARER),
        ("S5-1",),
        _s5_mcp_cognito,
    ),
    Case(
        "S5-4",
        _mcp_expected(
            "a bearer carrying every Gateway-token claim and the published kid, signed by an "
            "unpublished key",
            _MCP_FOREIGN_KEY,
        ),
        ("S5-1",),
        _s5_mcp_foreign_key,
    ),
    Case(
        "S5-5",
        _mcp_expected(
            "a Harness-audience, route-client token signed by an unpublished key",
            _MCP_HARNESS_AUDIENCE,
        ),
        ("S5-1",),
        _s5_mcp_harness_audience,
    ),
    *_DIRECT_CASES,
    dataclasses.replace(_ROUTE_EXCHANGE_CASE, requires=("S5-1",)),
    Case(
        "S5-12",
        "GetSecretValue of the agent client secret as the administrator-class operator: "
        f"refused with {_SECRET_DENY.describe()}",
        ("S5-1",),
        _s5_secret_read,
    ),
    Case(
        "S5-13",
        "POST /token as the agent client with a wrong secret: "
        f"{_TOKEN_WRONG_SECRET.describe()}, refused by the issuer's client authentication "
        f"('{_TOKEN_WRONG_SECRET_REASON}'), no token issued in the window",
        ("S5-1",),
        _s5_token_wrong_secret,
    ),
)

_SUITES: Mapping[str, tuple[Case, ...]] = {"p1": P1_CASES, "p5": P5_CASES, "s5": S5_CASES}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _redact_value(ctx: _Context, value: object) -> object:
    if isinstance(value, str):
        return ctx.redact(value)
    if isinstance(value, Mapping):
        return {
            str(key): _redact_value(ctx, item)
            for key, item in cast("Mapping[object, object]", value).items()
        }
    if isinstance(value, list | tuple):
        return [_redact_value(ctx, item) for item in cast("Sequence[object]", value)]
    return value


def _run(ctx: _Context, cases: Sequence[Case]) -> list[CaseResult]:
    results: dict[str, CaseResult] = {}
    for case in cases:
        missing = [
            required
            for required in case.requires
            if required not in results or not results[required].passed
        ]
        if missing:
            verdict = _Verdict(
                passed=False,
                observed=f"not run: positive control {', '.join(missing)} did not pass in this run",
            )
        else:
            try:
                verdict = case.run(ctx)
            except Exception as error:  # a probe fault is a FAIL with its type, never a pass
                verdict = _Verdict(
                    passed=False,
                    observed=(
                        f"probe error {type(error).__name__}: "
                        f"{str(error)[:_OBSERVED_MESSAGE_LIMIT]}"
                    ),
                )
        result = CaseResult(
            case_id=case.case_id,
            expected=case.expected,
            observed=ctx.redact(verdict.observed),
            passed=verdict.passed,
            evidence=cast("Mapping[str, object]", _redact_value(ctx, verdict.evidence)),
        )
        results[case.case_id] = result
        status = "PASS" if result.passed else "FAIL"
        print(
            f"CASE {result.case_id} | {result.expected} | {result.observed} | {status}", flush=True
        )
    return list(results.values())


def _write_evidence(ctx: _Context, suite: str, results: Sequence[CaseResult]) -> Path:
    _EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = _EVIDENCE_DIR / f"{stamp}.json"
    document = {
        "probe": "identity-chain",
        "decision": "ADR-077",
        "suite": suite,
        "environment": ctx.environment,
        "recorded_at": stamp,
        "cases": [asdict(result) for result in results],
    }
    text = ctx.redact(json.dumps(_redact_value(ctx, document), indent=2, sort_keys=True))
    path.write_text(text + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    """Run one suite against the explicit dev target and exit 0 only if every case passed."""
    args = _parser().parse_args(argv)
    if (
        args.environment != _REQUIRED_ENVIRONMENT
        or args.region != _REQUIRED_REGION
        or not _ACCOUNT_ID.fullmatch(args.expected_account)
    ):
        print("the identity-chain probe accepts only ENV=dev, REGION=us-east-1, a 12-digit account")
        return 2
    if args.memory_timeout_seconds < 1:
        print("--memory-timeout-seconds must be positive")
        return 2
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    if session.client("sts").get_caller_identity().get("Account") != args.expected_account:
        print("explicit profile did not resolve to the expected account")
        return 2
    ssm = session.client("ssm")
    memory_arn = published_parameter(ssm, args.environment, "memoryArn")
    harness_arn = published_parameter(ssm, args.environment, "harnessArn")
    # Refused rather than raised, like every check above: during the strategy cutover the Harness
    # can retrieve both namespaces or neither, and every suite needs its bounded exit code then.
    try:
        namespaces = cross_session_namespaces(
            session.client("bedrock-agentcore-control"), harness_arn.rsplit("/", maxsplit=1)[-1]
        )
    except (BotoCoreError, ClientError, ValueError) as error:
        print(
            "could not read the Harness's cross-session memory namespaces "
            f"({type(error).__name__}); no case ran"
        )
        return 2
    ctx = _Context(
        session=session,
        environment=args.environment,
        origin=require_https(
            published_parameter(ssm, args.environment, "publicBaseUrl").rstrip("/")
        ),
        harness_arn=harness_arn,
        memory_id=memory_arn.rsplit("/", maxsplit=1)[-1],
        cross_session_namespaces=namespaces,
        client=app_client(session, args.environment),
        memory_timeout_seconds=args.memory_timeout_seconds,
    )
    ctx.secrets.add(ctx.client.secret)
    try:
        results = _run(ctx, _SUITES[args.suite])
    finally:
        torn_down = args.suite != "p5" or _teardown_codenames(ctx)
    path = _write_evidence(ctx, args.suite, results)
    passed = sum(result.passed for result in results)
    print(f"{passed}/{len(results)} cases passed; evidence {path.relative_to(_ROOT)}")
    return 0 if passed == len(results) and torn_down else 1


def _teardown_codenames(ctx: _Context) -> bool:
    """Remove the codename P5 stated, waiting out late extraction; report, never raise.

    Left in place it labels every later probe-a answer (see `memory_codenames`). A failed
    teardown still lets the run write its evidence, and fails the run.
    """
    try:
        swept = settle_codenames(
            ctx.session.client("bedrock-agentcore"),
            ctx.memory_id,
            _persona_actors(ctx),
            ctx.cross_session_namespaces,
        )
    except (BotoCoreError, ClientError, TimeoutError, ValueError) as error:
        print(f"P5 teardown FAILED: {type(error).__name__}; codename records may remain")
        return False
    print(f"P5 teardown: {swept} codename records swept; memory settled clean")
    return True


if __name__ == "__main__":
    sys.exit(main())
