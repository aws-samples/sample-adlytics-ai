# SPDX-License-Identifier: MIT-0
"""V-7: the conflict unit `U`, the input revision `r` and the exact-key rewrite check; N1 root."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from domain.operations.ingestion_root import (
    ManifestVersion,
    RootRefusedError,
    exact_version_count,
    manifest_fingerprint,
    require_body_agrees,
    root_flow_id,
    window_identity,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-030")]

WINDOW = (
    # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    "amc/dsp_impressions/query_pack=amc-dsp-impressions/source_scope=s/marketplace=ATVPDKIKX0DER/"
    "window=2026-10-04_2026-10-06/options=44136fa355b3678a/"
)


# pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
def key(instant: str, options: str = "44136fa355b3678a") -> str:
    # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    return f"{WINDOW.replace('44136fa355b3678a', options)}retrieved={instant}/manifest.json"


def test_v7_two_retrievals_of_one_window_share_u_and_order_by_r() -> None:
    older = window_identity(key("20261006T081257Z"))
    newer = window_identity(key("20261006T092520Z"))
    assert older.window == newer.window == WINDOW
    assert older.conflict_unit == newer.conflict_unit
    assert older.retrieved_at < newer.retrieved_at
    assert newer.retrieved_at == datetime(2026, 10, 6, 9, 25, 20, tzinfo=UTC)


def test_v7_two_windows_that_differ_only_in_options_are_different_units() -> None:
    first = window_identity(key("20261006T081257Z"))
    second = window_identity(key("20261006T081257Z", options="0000000000000000"))
    assert first.conflict_unit != second.conflict_unit


def test_v7a_more_than_one_exact_version_is_a_rewrite() -> None:
    k = key("20261006T081257Z")
    assert exact_version_count(k, [k]) == 1
    assert exact_version_count(k, [k, k]) == 2


def test_v7b_a_longer_key_under_the_prefix_is_ignored() -> None:
    k = key("20261006T081257Z")
    assert exact_version_count(k, [k, f"{k}.bak", f"{k}/extra"]) == 1


def test_v7c_a_body_that_disagrees_with_its_key_is_refused() -> None:
    identity = window_identity(key("20261006T081257Z"))
    require_body_agrees(identity, datetime(2026, 10, 6, 8, 12, 57, tzinfo=UTC))
    for body in (
        datetime(2026, 10, 6, 8, 12, 58, tzinfo=UTC),
        datetime(2026, 10, 6, 8, 12, 57, 1, tzinfo=UTC),
        datetime(2026, 10, 6, 8, 12, 57),  # noqa: DTZ001
    ):
        with pytest.raises(RootRefusedError) as raised:
            require_body_agrees(identity, body)
        assert raised.value.signal == "MANIFEST_KEY_BODY_MISMATCH"


def test_a_key_without_exactly_one_retrieved_segment_is_refused() -> None:
    for bad in (
        f"{WINDOW}manifest.json",
        f"{WINDOW}retrieved=20261006T081257Z/retrieved=20261006T081258Z/manifest.json",
        f"{WINDOW}retrieved=2026-10-06/manifest.json",
    ):
        with pytest.raises(RootRefusedError):
            window_identity(bad)


def test_n1_one_root_per_exact_version_and_a_fingerprint_bound_to_template_and_contract() -> None:
    version = ManifestVersion(key("20261006T081257Z"), "v1")
    assert root_flow_id(version) == root_flow_id(ManifestVersion(version.key, "v1"))
    assert root_flow_id(version) != root_flow_id(ManifestVersion(version.key, "v2"))
    base = manifest_fingerprint(version, "ingestion_manifest_to_served.v1", 1)
    assert base != manifest_fingerprint(version, "ingestion_manifest_to_served.v1", 2)
    assert base != manifest_fingerprint(
        ManifestVersion(version.key, "v2"), "ingestion_manifest_to_served.v1", 1
    )
    assert base != manifest_fingerprint(version, "other_template.v1", 1)


def test_n1_the_fingerprint_keys_on_the_contract_revision_not_the_contract_bytes() -> None:
    """S1A-4: an edit to the contract file that is not a new revision re-keys nothing."""
    import domain.operations.generated_execution_graphs as generated  # noqa: PLC0415
    from adapters.inbound.run_requests import service  # noqa: PLC0415

    version = ManifestVersion(key("20261006T081257Z"), "v1")
    pinned = manifest_fingerprint(version, service.INGESTION_TEMPLATE, generated.CONTRACT_REVISION)
    # Pinned value at contract_revision 2 (S6 part 2, design §2.1): a change here re-keys every
    # live input. Revision 1 pinned 3aee3ed6...; no epoch-1 record exists, so nothing re-keys.
    # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    assert pinned == "7f0a20a599bcb85d5ea491aed46f1e9b3dc085e86385088206e26691aa75f786"
    assert "CONTRACT_DIGEST" not in service.__dict__
