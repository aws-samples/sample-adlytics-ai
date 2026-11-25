# SPDX-License-Identifier: MIT-0
"""validation/browser_session.py's dev-only guard and the environment it gives the child.

A failure here would let the sign-in command start a browser script against a non-dev target, or
let an operator's sourced cookie ride along with a persona session (security). No AWS: the guard
returns before boto3 is imported, and the environment is a pure function of its inputs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))
from validation import browser_session  # noqa: E402

pytestmark = pytest.mark.unit

COMMAND = ["node", "validation/probe_frontend_browser.mjs"]
TOKEN = "Fictional-Persona-Access-Token"


@pytest.mark.parametrize(
    "target",
    [("prod", "us-east-1", COMMAND), ("dev", "eu-west-1", COMMAND), ("dev", "us-east-1", [])],
)
def test_main_refuses_a_non_dev_target_or_no_command_with_exit_2(
    target: tuple[str, str, list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    environment, region, command = target
    argv = ["--profile", "p", "--environment", environment, "--region", region]
    assert browser_session.main([*argv, "--expected-account", "000000000000", "--", *command]) == 2
    assert capsys.readouterr().out.strip() == (
        "browser_session accepts only dev in us-east-1, with a command after --"
    )


def test_dev_in_us_east_1_with_a_command_passes_the_guard() -> None:
    assert browser_session.target_refused("dev", "us-east-1", COMMAND) is False


def test_the_child_gets_the_session_and_origin_but_never_an_operator_cookie() -> None:
    parent = {
        "ADL_REFRESH": "Fictional-Operator-Refresh",
        "ADL_CSRF": "Fictional-Operator-Csrf",
        "ADL_SESSION": "Fictional-Stale-Session",
        "ADL_CAPTURE_SKIP": "prompt-administration",
    }
    assert browser_session.child_environment(parent, "https://dev.example", TOKEN) == {
        "ADL_CAPTURE_SKIP": "prompt-administration",
        "ADL_BASE_URL": "https://dev.example",
        "ADL_SESSION": TOKEN,
    }
