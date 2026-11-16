# SPDX-License-Identifier: MIT-0
"""The run fingerprint: a component it ignores would let a changed run no-op against CURRENT."""

from __future__ import annotations

from dataclasses import replace

import pytest

from domain.forecasting.fingerprint import RunIdentity, run_fingerprint, run_hash12

pytestmark = pytest.mark.unit

BASE = RunIdentity(
    input_fingerprint="1" * 64,
    policy_sha256="2" * 64,
    processor_code_identity="3" * 64,
    lock_digest="4" * 64,
    model_id="amazon/chronos-2",
    model_artifact_digest="5" * 64,
    boundary_schema_digests={"InputManifestV1": "6" * 64},
    instance_type="ml.m5.xlarge",
    max_runtime_seconds=3600,
)


@pytest.mark.parametrize(
    "changed",
    [
        replace(BASE, input_fingerprint="7" * 64),
        replace(BASE, policy_sha256="7" * 64),
        replace(BASE, processor_code_identity="7" * 64),
        replace(BASE, lock_digest="7" * 64),
        replace(BASE, model_id="amazon/chronos-2-small"),
        replace(BASE, model_artifact_digest="7" * 64),
        replace(BASE, boundary_schema_digests={"InputManifestV1": "7" * 64}),
        replace(BASE, instance_type="ml.m5.2xlarge"),
        replace(BASE, max_runtime_seconds=7200),
    ],
)
def test_each_component_changes_the_fingerprint(changed: RunIdentity) -> None:
    assert run_fingerprint(changed) != run_fingerprint(BASE)


def test_the_fingerprint_is_stable_for_one_identity() -> None:
    assert run_fingerprint(BASE) == run_fingerprint(replace(BASE))


def test_the_run_hash_is_twelve_hex_characters_of_the_run_id() -> None:
    assert run_hash12("abc") == "ba7816bf8f01"
