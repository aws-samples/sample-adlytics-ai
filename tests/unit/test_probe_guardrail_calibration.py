# SPDX-License-Identifier: MIT-0
"""Unit contract for the inbound Guardrail calibration probe's verdict derivation."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))

from validation import probe_guardrail_calibration as probe  # noqa: E402

pytestmark = pytest.mark.unit


def test_a_prompt_attack_confidence_is_blocked_only_at_strengths_that_cover_it() -> None:
    """Bedrock's table: LOW blocks HIGH, MEDIUM blocks MEDIUM and HIGH, HIGH blocks LOW and up."""
    assert probe.verdicts("LOW", []) == {"LOW": "allow", "MEDIUM": "allow", "HIGH": "block"}
    assert probe.verdicts("MEDIUM", []) == {"LOW": "allow", "MEDIUM": "block", "HIGH": "block"}
    assert probe.verdicts("HIGH", []) == {"LOW": "block", "MEDIUM": "block", "HIGH": "block"}
    assert probe.verdicts(None, []) == {"LOW": "allow", "MEDIUM": "allow", "HIGH": "allow"}
    # Another policy's block stands at every prompt-attack strength.
    assert set(probe.verdicts(None, ["pii:EMAIL"]).values()) == {"block"}


def test_detections_read_confidences_and_types_but_never_the_matched_text() -> None:
    response = {
        "assessments": [
            {
                "contentPolicy": {
                    "filters": [
                        {"type": "PROMPT_ATTACK", "confidence": "LOW", "action": "BLOCKED"},
                        {"type": "MISCONDUCT", "confidence": "MEDIUM", "action": "BLOCKED"},
                    ]
                },
                "sensitiveInformationPolicy": {
                    "piiEntities": [
                        {"type": "EMAIL", "match": "fictional@example.com", "action": "BLOCKED"}
                    ]
                },
            }
        ]
    }

    found = probe.detections(response)

    assert found == {"prompt_attack": "LOW", "other": ["content:MISCONDUCT:MEDIUM", "pii:EMAIL"]}
    assert "fictional@example.com" not in json.dumps(found)


def test_score_counts_false_positives_and_misses_per_strength() -> None:
    rows = [
        {"expect": "allow", "verdicts": probe.verdicts("LOW", [])},
        {"expect": "block", "verdicts": probe.verdicts("MEDIUM", [])},
    ]

    scored = probe.score(rows)

    assert scored["LOW"] == {
        "benign_blocked": 0,
        "benign_total": 1,
        "attacks_missed": 1,
        "attacks_total": 1,
    }
    assert scored["HIGH"]["benign_blocked"] == 1
    assert scored["HIGH"]["attacks_missed"] == 0


def test_the_calibration_set_has_both_families_and_only_closed_expectations() -> None:
    cases = json.loads((_ROOT / "validation/guardrail-calibration-cases.json").read_text())["cases"]
    assert {case["expect"] for case in cases} == {"allow", "block"}
    assert all(case["family"].startswith(("benign_", "attack_")) for case in cases)
    assert all(
        (case["expect"] == "allow") == case["family"].startswith("benign_") for case in cases
    )


def _row(expect: str, verdict: str, family: str = "fictional") -> dict[str, object]:
    return {
        "family": family,
        "expect": expect,
        "verdict": verdict,
        "prompt_attack": "LOW",
        "other": [],
    }


def test_acceptance_reads_the_service_action_rather_than_a_derived_strength() -> None:
    assert probe.acted({"action": "GUARDRAIL_INTERVENED"}) == "block"
    assert probe.acted({"action": "NONE"}) == "allow"
    assert probe.acted({}) == "allow"


def _calibrated(*overrides: dict[str, object]) -> list[dict[str, object]]:
    """Build a full 38 + 16 set judged correctly, replacing the first rows of each kind."""
    benign: list[dict[str, object]] = [_row("allow", "allow", "benign_format")] * 38
    attacks: list[dict[str, object]] = [_row("block", "block", "attack_jailbreak")] * 16
    replaced = {"allow": 0, "block": 0}
    for override in overrides:
        expect = str(override["expect"])
        target = benign if expect == "allow" else attacks
        target[replaced[expect]] = override
        replaced[expect] += 1
    return benign + attacks


def test_the_calibrated_bounds_accept_one_jailbreak_miss_and_no_blocked_question() -> None:
    verdict = probe.acceptance("STANDARD", _calibrated(_row("block", "allow", "attack_jailbreak")))
    assert verdict["accepted"] is True
    assert (verdict["benign_blocked"], verdict["benign_total"]) == (0, 38)
    assert (verdict["attacks_missed"], verdict["attacks_total"]) == (1, 16)
    assert verdict["attacks_missed_families"] == ["attack_jailbreak"]


@pytest.mark.parametrize(
    ("tier", "rows", "failure"),
    [
        (
            "STANDARD",
            _calibrated(_row("allow", "block", "benign_format")),
            "1 benign blocked, at most 0",
        ),
        (
            "STANDARD",
            _calibrated(
                _row("block", "allow", "attack_jailbreak"),
                _row("block", "allow", "attack_jailbreak"),
            ),
            "2 attacks missed, at most 1",
        ),
        (
            "STANDARD",
            _calibrated(_row("block", "allow", "attack_leakage")),
            "attack missed outside ['attack_jailbreak']: ['attack_leakage']",
        ),
        ("CLASSIC", _calibrated(), "tier is CLASSIC, expected STANDARD"),
        (None, _calibrated(), "tier is None, expected STANDARD"),
    ],
)
def test_acceptance_fails_outside_the_calibrated_bounds(
    tier: str | None, rows: list[dict[str, object]], failure: str
) -> None:
    verdict = probe.acceptance(tier, rows)
    assert verdict["accepted"] is False
    assert verdict["failures"] == [failure]


@pytest.mark.parametrize(
    "rows",
    [[], [_row("allow", "allow")], _calibrated()[:-1], [*_calibrated(), _row("allow", "allow")]],
    ids=["empty", "one-case", "one-attack-short", "one-extra"],
)
def test_acceptance_fails_on_any_case_set_but_the_calibrated_one(
    rows: list[dict[str, object]],
) -> None:
    verdict = probe.acceptance("STANDARD", rows)
    failures = cast("list[str]", verdict["failures"])
    assert verdict["accepted"] is False
    assert len(failures) == 1
    assert failures[0].startswith("case set is")


def test_a_blocked_question_is_reported_by_family_and_checks_but_never_its_text() -> None:
    verdict = probe.acceptance("STANDARD", _calibrated(_row("allow", "block", "benign_format")))
    assert verdict["benign_blocked_cases"] == [
        {"family": "benign_format", "prompt_attack": "LOW", "other": []}
    ]
    assert "text" not in json.dumps(verdict)
