# SPDX-License-Identifier: MIT-0
"""The fail-closed dev target guard for the D2 traced source render (merge-gate finding F2).

Pure functions with no AWS call, so the unit tier runs their paired controls: every integration
test skips without credentials, which would leave the guard's controls unexecuted in every
credential-free gate.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

__all__ = [
    "BAKER_FUNCTION",
    "REFUSE_ACCOUNT",
    "REFUSE_CALLER",
    "REFUSE_ENV",
    "REFUSE_FUNCTION",
    "REFUSE_FUNCTION_ARN",
    "TARGET_ENV",
    "identity_refusal",
    "target_refusal",
]

TARGET_ENV: Final = "dev"
"""The only environment the diagnostic runs against; `ADL_TEST_ENV` must name it explicitly."""
BAKER_FUNCTION: Final = f"adlytics-{TARGET_ENV}-PresetArtifactBaker"
"""The deployed Baker's exact function name (`infra/lib/stacks/publisher-stack.ts`)."""

REFUSE_ENV: Final = "ADL_TEST_ENV must be set explicitly to dev"
REFUSE_ACCOUNT: Final = "ADL_RUNTIME_BAKE_EXPECTED_ACCOUNT must be exactly 12 digits"
REFUSE_FUNCTION: Final = f"ADL_RUNTIME_BAKE_FUNCTION must be exactly {BAKER_FUNCTION}"
REFUSE_CALLER: Final = "the STS caller account is not the expected account"
REFUSE_FUNCTION_ARN: Final = "the Baker function is not in the expected account"


def target_refusal(environ: Mapping[str, str]) -> str | None:
    """Refuse an opted-in run whose declared target isn't exactly dev; no AWS call is made."""
    if environ.get("ADL_TEST_ENV") != TARGET_ENV:
        return REFUSE_ENV
    account = environ.get("ADL_RUNTIME_BAKE_EXPECTED_ACCOUNT", "")
    if len(account) != 12 or not account.isdigit():
        return REFUSE_ACCOUNT
    if environ.get("ADL_RUNTIME_BAKE_FUNCTION") != BAKER_FUNCTION:
        return REFUSE_FUNCTION
    return None


def identity_refusal(expected: str, caller: str, function_arn: str) -> str | None:
    """Refuse unless STS and the function's ARN both name the expected account."""
    if caller != expected:
        return REFUSE_CALLER
    if function_arn.split(":")[4:5] != [expected] or not function_arn.endswith(
        f":function:{BAKER_FUNCTION}"
    ):
        return REFUSE_FUNCTION_ARN
    return None
