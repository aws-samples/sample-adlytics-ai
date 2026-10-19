# SPDX-License-Identifier: MIT-0
"""The D2 traced source render's dev target guard (merge-gate finding F2), with paired controls.

The guard lives beside the integration test it protects; it is pure, so it is checked here, where
the credential-free gates run it. Fictional account ids only.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from tests.integration.runtime_bake_target import (
    BAKER_FUNCTION,
    REFUSE_ACCOUNT,
    REFUSE_CALLER,
    REFUSE_ENV,
    REFUSE_FUNCTION,
    REFUSE_FUNCTION_ARN,
    identity_refusal,
    target_refusal,
)

pytestmark = pytest.mark.unit

_ACCOUNT = "111122223333"
_ADMITTED = {
    "ADL_TEST_ENV": "dev",
    "ADL_RUNTIME_BAKE_EXPECTED_ACCOUNT": _ACCOUNT,
    "ADL_RUNTIME_BAKE_FUNCTION": BAKER_FUNCTION,
}
_ARN = f"arn:aws:lambda:us-east-1:{_ACCOUNT}:function:{BAKER_FUNCTION}"


def test_the_exact_dev_target_passes_both_layers() -> None:
    """Positive control for every refusal below."""
    assert target_refusal(_ADMITTED) is None
    assert identity_refusal(_ACCOUNT, _ACCOUNT, _ARN) is None


@pytest.mark.parametrize(
    ("environ", "refusal"),
    [
        ({k: v for k, v in _ADMITTED.items() if k != "ADL_TEST_ENV"}, REFUSE_ENV),
        (_ADMITTED | {"ADL_TEST_ENV": "prod"}, REFUSE_ENV),
        (_ADMITTED | {"ADL_RUNTIME_BAKE_EXPECTED_ACCOUNT": ""}, REFUSE_ACCOUNT),
        (_ADMITTED | {"ADL_RUNTIME_BAKE_EXPECTED_ACCOUNT": "11112222333"}, REFUSE_ACCOUNT),
        (
            _ADMITTED | {"ADL_RUNTIME_BAKE_FUNCTION": "adlytics-prod-PresetArtifactBaker"},
            REFUSE_FUNCTION,
        ),
    ],
)
def test_each_single_deviation_from_the_declared_target_is_refused(
    environ: dict[str, str], refusal: str
) -> None:
    assert target_refusal(environ) == refusal


def test_an_identity_outside_the_expected_account_or_dev_function_is_refused() -> None:
    other = "444455556666"
    assert identity_refusal(_ACCOUNT, other, _ARN) == REFUSE_CALLER
    assert identity_refusal(_ACCOUNT, _ACCOUNT, _ARN.replace(_ACCOUNT, other)) == (
        REFUSE_FUNCTION_ARN
    )
    assert identity_refusal(_ACCOUNT, _ACCOUNT, _ARN.replace("-dev-", "-prod-")) == (
        REFUSE_FUNCTION_ARN
    )
