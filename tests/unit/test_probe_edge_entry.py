# SPDX-License-Identifier: MIT-0
"""ADR-078 body-size exclusion: the edge probe names the refusing layer from positive evidence."""

# ruff: noqa: PLR0913

from __future__ import annotations

import ssl
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))

from validation import probe_edge_entry as probe  # noqa: E402
from validation import ship_prompts  # noqa: E402

from adapters.outbound.https import InsecureUrlError  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-078")]

Layer = probe.Layer
Observation = probe.Observation

# Recorded shapes, fictional values: a WAF block by each size rule, the managed group before the
# new rules propagate, an allowed request with the managed size rule counted, and access-log
# records for an authorizer refusal and a handler run.
_WAF_OUTSIDE: dict[str, Any] = {
    "action": "BLOCK",
    "terminatingRuleId": "BodySizeOutsidePromptCreate",
    "nonTerminatingMatchingRules": [],
}
_WAF_CEILING: dict[str, Any] = {"action": "BLOCK", "terminatingRuleId": "PromptCreateBodyCeiling"}
_WAF_MANAGED: dict[str, Any] = {"action": "BLOCK", "terminatingRuleId": "AwsCommonRuleSet"}
_WAF_ALLOW_COUNTED: dict[str, Any] = {
    "action": "ALLOW",
    "terminatingRuleId": "Default_Action",
    "ruleGroupList": [
        {
            "ruleGroupId": "AWS#AWSManagedRulesCommonRuleSet",
            "nonTerminatingMatchingRules": [],
            "excludedRules": [
                {"exclusionType": "EXCLUDED_AS_COUNT", "ruleId": "SizeRestrictions_BODY"}
            ],
        }
    ],
}
_ACCESS_AUTHORIZER = {"status": "403", "authorizerError": "", "integrationLatency": "-"}
_ACCESS_HANDLER = {"status": "202", "authorizerError": "", "integrationLatency": "41"}


def _observe(
    status: int,
    *,
    content_type: str = "application/json",
    reached: bool = False,
    access: dict[str, Any] | None = None,
    waf: dict[str, Any] | None = None,
) -> probe.Verdict:
    return probe.classify(Observation(status, content_type, reached, access, waf))


def test_a_waf_block_names_its_terminating_rule() -> None:
    verdict = _observe(403, content_type="text/html", waf=_WAF_OUTSIDE)
    assert verdict.layer is Layer.WAF_BLOCK
    assert verdict.terminating_rule == "BodySizeOutsidePromptCreate"
    assert not probe.leaked(verdict)
    assert not probe.not_yet_propagated(verdict)
    assert _observe(403, content_type="text/html", waf=_WAF_CEILING).terminating_rule == (
        "PromptCreateBodyCeiling"
    )


def test_the_managed_group_still_terminating_is_propagation_not_a_leak() -> None:
    verdict = _observe(403, content_type="text/html", waf=_WAF_MANAGED)
    assert verdict.layer is Layer.WAF_BLOCK
    assert probe.not_yet_propagated(verdict)
    assert not probe.leaked(verdict)


def test_an_authorizer_refusal_reached_api_gateway_and_did_not_run_the_handler() -> None:
    verdict = _observe(403, reached=True, access=_ACCESS_AUTHORIZER)
    assert verdict.layer is Layer.AUTHORIZER_REFUSAL
    assert verdict.integration_latency == "-"
    # A body the size rules must refuse that reaches the authorizer passed WAF: the leak trigger.
    assert probe.leaked(verdict)


def test_a_numeric_integration_latency_means_the_handler_ran() -> None:
    verdict = _observe(202, reached=True, access=_ACCESS_HANDLER, waf=_WAF_ALLOW_COUNTED)
    assert verdict.layer is Layer.HANDLER_INVOKED
    assert verdict.access_status == "202"
    assert verdict.waf_action == "ALLOW"
    assert verdict.counted_rules == ("SizeRestrictions_BODY",)
    assert probe.leaked(verdict)


def test_a_400_without_a_waf_block_is_the_canonical_path_function() -> None:
    verdict = _observe(400, content_type="application/json")
    assert verdict.layer is Layer.CANONICAL_PATH_REFUSAL


def test_the_absence_of_a_waf_record_is_not_proof_of_a_waf_block() -> None:
    # A 403 HTML page with no record and no API Gateway id is consistent with a block, never proof.
    assert _observe(403, content_type="text/html").layer is Layer.UNPROVEN
    # Reached API Gateway but no access-log record yet: unproven, not a handler run.
    assert _observe(403, reached=True).layer is Layer.UNPROVEN
    # An allow record with no API Gateway id proves nothing about the layer, but is a leak sign.
    allowed = _observe(403, content_type="text/html", waf={"action": "ALLOW"})
    assert allowed.layer is Layer.UNPROVEN
    assert probe.leaked(allowed)


def test_an_access_record_with_an_empty_latency_and_a_200_is_unproven() -> None:
    verdict = _observe(200, reached=True, access={"status": "200", "integrationLatency": ""})
    assert verdict.layer is Layer.UNPROVEN


def test_sized_bodies_are_exact_and_never_a_valid_create() -> None:
    for size in (200, 9_000, 20_000, 32_768, 33_000):
        body = probe._sized_body(size)  # pyright: ignore[reportPrivateUsage]
        assert len(body) == size
        assert b'"probe_padding"' in body


def test_the_variants_are_the_eight_path_confusion_spellings() -> None:
    paths = [path for _, path in probe._PATH_VARIANTS]  # pyright: ignore[reportPrivateUsage]
    assert len(paths) == len(set(paths)) == 8
    assert probe._EXEMPT_PATH not in paths  # pyright: ignore[reportPrivateUsage]


def test_cloudfront_method_refusal_needs_its_page_and_no_waf_record() -> None:
    refused = probe.classify(Observation(403, "text/html", False, None, None, True))
    assert refused.layer is Layer.CLOUDFRONT_METHOD_REFUSAL
    assert not probe.leaked(refused)
    # Without the page sentence, a bare 403 stays unproven; a WAF record wins over the page.
    assert probe.classify(Observation(403, "text/html", False, None, None)).layer is Layer.UNPROVEN
    blocked = probe.classify(Observation(403, "text/html", False, None, _WAF_OUTSIDE, True))
    assert blocked.layer is Layer.WAF_BLOCK


def test_a_variant_the_canonical_path_function_passes_must_name_the_8_kb_rule() -> None:
    outside = _observe(403, content_type="text/html", waf=_WAF_OUTSIDE)
    other_rule = _observe(
        403,
        content_type="text/html",
        waf={"action": "BLOCK", "terminatingRuleId": "AwsCommonRuleSet"},
    )
    canonical = _observe(400)
    method = probe.classify(Observation(403, "text/html", False, None, None, True))
    for label in ("trailing_slash", "upper_prefix", "mixed_case", "query_string"):
        assert probe.variant_refused(label, outside)  # positive control
        assert not probe.variant_refused(label, other_rule)
        assert not probe.variant_refused(label, canonical)
    # Only the /API/ prefix falls to the GET-only default behavior, so only it may be refused there.
    assert probe.variant_refused("upper_prefix", method)
    for label in ("trailing_slash", "mixed_case", "query_string"):
        assert not probe.variant_refused(label, method)
    # The spellings the canonical-path function owns may be refused by either layer.
    for label in ("doubled_slash", "escaped_hyphen", "dot_segment", "dot_dot_segment"):
        assert probe.variant_refused(label, outside)
        assert probe.variant_refused(label, canonical)
        assert not probe.variant_refused(label, _observe(403, reached=True, access=_ACCESS_HANDLER))
    labels = {label for label, _ in probe._PATH_VARIANTS}  # pyright: ignore[reportPrivateUsage]
    assert labels > probe._OUTSIDE_RULE_VARIANTS  # pyright: ignore[reportPrivateUsage]


class _FakeResponse:
    def __init__(self, status: int, headers: dict[str, str], payload: bytes) -> None:
        self.status = status
        self._headers = headers
        self._payload = payload

    def getheader(self, name: str) -> str | None:
        return self._headers.get(name)

    def read(self, _limit: int) -> bytes:
        return self._payload


class _FakeConnection:
    sent: list[tuple[str, str, dict[str, str]]] = []  # noqa: RUF012
    reply = _FakeResponse(400, {"content-type": "application/json"}, b'{"error":"invalid_request"}')

    def __init__(self, host: str, port: int, **options: object) -> None:
        assert (host, port) == ("example.invalid", 443)
        # A-HTTPS-CONNECTION-001: certificate and host-name verification on every connection.
        context = options["context"]
        assert isinstance(context, ssl.SSLContext)
        assert context.verify_mode is ssl.CERT_REQUIRED
        assert context.check_hostname

    def request(self, method: str, path: str, *, body: bytes, headers: dict[str, str]) -> None:
        assert body
        _FakeConnection.sent.append((method, path, headers))

    def getresponse(self) -> _FakeResponse:
        return _FakeConnection.reply

    def close(self) -> None:
        return None


def test_every_variant_leaves_the_host_exactly_as_spelled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe.http.client, "HTTPSConnection", _FakeConnection)
    _FakeConnection.sent = []
    for _, path in probe._PATH_VARIANTS:  # pyright: ignore[reportPrivateUsage]
        response = probe._send_body("https://example.invalid", path, {"x": "y"}, b"{}")  # pyright: ignore[reportPrivateUsage]
        assert response.error == "invalid_request"
        assert not response.cloudfront_method_refusal
    sent_paths = [path for _, path, _ in _FakeConnection.sent]
    # Positive control: the three spellings `requests` rewrites go out unchanged.
    assert "/api/prompt-registry/create%2Dversion" in sent_paths
    assert "/api/./prompt-registry/create-version" in sent_paths
    assert "/api/x/../prompt-registry/create-version" in sent_paths
    assert sent_paths == [path for _, path in probe._PATH_VARIANTS]  # pyright: ignore[reportPrivateUsage]
    assert all(
        headers["User-Agent"] == "adlytics-validation" for _, _, headers in _FakeConnection.sent
    )
    # A plaintext origin is refused before any connection opens.
    sent_before = len(_FakeConnection.sent)
    with pytest.raises(InsecureUrlError):
        probe._send_body("http://example.invalid", "/api/x", {}, b"{}")  # pyright: ignore[reportPrivateUsage]
    assert len(_FakeConnection.sent) == sent_before


def test_the_cloudfront_method_page_is_read_from_the_raw_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe.http.client, "HTTPSConnection", _FakeConnection)
    page = b"<html>This distribution is not configured to allow the HTTP request method</html>"
    monkeypatch.setattr(
        _FakeConnection,
        "reply",
        _FakeResponse(403, {"content-type": "text/html; charset=utf-8"}, page),
    )
    response = probe._send_body("https://example.invalid", "/API/x", {}, b"{}")  # pyright: ignore[reportPrivateUsage]
    assert (response.status, response.content_type, response.api_request_id) == (
        403,
        "text/html",
        "",
    )
    assert response.cloudfront_method_refusal


def test_the_operator_session_is_re_minted_before_its_access_token_expires() -> None:
    now = [0.0]
    minted: list[int] = []

    def mint() -> dict[str, str]:
        minted.append(1)
        return {"Cookie": f"adl_session=fictional-{len(minted)}"}

    cookies = probe._FreshCookies(mint, max_age=300, clock=lambda: now[0])  # pyright: ignore[reportPrivateUsage]
    assert cookies() == {"Cookie": "adl_session=fictional-1"}
    now[0] = 299
    assert cookies() == {"Cookie": "adl_session=fictional-1"}  # reused inside the window
    now[0] = 300
    assert cookies() == {"Cookie": "adl_session=fictional-2"}  # re-minted at the window's end
    assert len(minted) == 2
    # The window sits well inside the 15-minute access token.
    assert probe._SESSION_REUSE_SECONDS < 15 * 60  # pyright: ignore[reportPrivateUsage]


def test_the_probe_marker_is_a_canonical_uuid_or_refused() -> None:
    marker = "0F1E2D3C-4B5A-4968-8776-655443322110"
    assert ship_prompts.probe_marker(marker) == marker.lower()
    with pytest.raises(Exception, match="--probe-marker must be a UUID"):
        ship_prompts.probe_marker("not-a-uuid")


def test_the_edge_rule_roster_names_the_six_deployed_rules() -> None:
    assert probe._EDGE_RULES == (  # pyright: ignore[reportPrivateUsage]
        "AwsIpReputation",
        "AwsKnownBadInputs",
        "AwsCommonRuleSet",
        "PromptCreateBodyCeiling",
        "BodySizeOutsidePromptCreate",
        "RateLimitPerSourceIp",
    )
