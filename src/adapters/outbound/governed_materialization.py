# SPDX-License-Identifier: MIT-0
"""`GovernedMaterializationPort`: the lowest raw-fetch boundary (S6; hard deny (d), FR-5).

Every materializing path fetches the exact delivery through this one port: the SQS handler's
legacy path, the direct operator (`ingest_delivery`) and, in `GRAPH`, the graph materialization
node. Before the first data-object read it reads the exact manifest version and applies the
historical predicate again, in every mode. A historical manifest is refused with
`FULL_HISTORY_NOT_ENABLED`: no data object is read, nothing is materialized and no product is
written. v1 has no legitimate historical producer, so the refusal is an invariant error that
pages, and a `delivery_event` refused this way stays unacknowledged for an I3-style disposition.

A graph-path caller passes the materialization claim it holds (`MaterializationClaim`); the port
re-attests it strongly before the fetch, so a taken-over or superseded claim cannot materialize.
In `GRAPH` the re-attestation is the two-member transaction `graph_materialization_claim` binds
(S6 §2.4, AGG3-17): the held lease version on `EXEC_CLAIM#`, which it stamps with
`materialization_began_at`, and a live (OPEN or PAUSED) `RUN#`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from botocore.exceptions import BotoCoreError, ClientError

from adapters.inbound.run_requests.service import contract_policy
from adapters.jobs.curation.manifests import read_manifest
from adapters.jobs.ingest_delivery.landing import (
    LoadedDelivery,
    ManifestInput,
    fetch_v2_delivery,
)
from domain.curation.landing import LandingError
from domain.operations.execution_graph_dispatch import GraphGuardError
from domain.operations.run_request import (
    AdmissionClass,
    ManifestFacts,
    derive_admission_class,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

    from ports.outbound.execution_graph_dispatch import ClaimRecord

__all__ = [
    "FULL_HISTORY_NOT_ENABLED",
    "MANIFEST_INVALID",
    "MATERIALIZATION_CLAIM_LOST",
    "FullHistoryRefusedError",
    "GovernedMaterializationPort",
    "MaterializationClaim",
    "MaterializationClaimLostError",
    "MaterializationReattestor",
    "graph_materialization_claim",
]

FULL_HISTORY_NOT_ENABLED: Final = "FULL_HISTORY_NOT_ENABLED"
MATERIALIZATION_CLAIM_LOST: Final = "MATERIALIZATION_CLAIM_LOST"
MANIFEST_INVALID: Final = ErrorCode("EXEC_GRAPH", 44)
"""The exact manifest version is not a valid manifest (an invariant, never retried)."""
_MAX_MANIFEST_BYTES: Final = 262_144


class FullHistoryRefusedError(InvariantError):
    """A historical manifest reached the materialization port: zero materialization."""

    def __init__(self, key: str) -> None:
        super().__init__(
            ErrorCode("EXEC_GRAPH", 41),
            "a historical manifest reached the governed materialization port.",
            context={"signal": FULL_HISTORY_NOT_ENABLED, "manifest_key": key},
        )
        self.signal = FULL_HISTORY_NOT_ENABLED


class MaterializationClaimLostError(InvariantError):
    """The graph materialization claim moved before the fetch: zero materialization."""

    def __init__(self) -> None:
        super().__init__(
            ErrorCode("EXEC_GRAPH", 42),
            "the materialization claim was taken over or superseded before the fetch.",
            context={"signal": MATERIALIZATION_CLAIM_LOST},
        )
        self.signal = MATERIALIZATION_CLAIM_LOST


@dataclass(frozen=True, slots=True)
class MaterializationClaim:
    """The graph claim a GRAPH-path caller holds, and how to re-attest it strongly."""

    node_id: str
    lease_version: int
    reattest: Callable[[str, int], bool]
    """True while `EXEC_CLAIM#<node_id>` still carries this lease version and its run is live."""


class MaterializationReattestor(Protocol):
    """The graph store's two-member re-attestation transaction (S6 §2.4, AGG3-17)."""

    def reattest_materialization(self, claim: ClaimRecord, began_at: str) -> None:
        """Commit, or raise `GraphGuardError` when the lease moved or the run is not live."""
        ...


def graph_materialization_claim(
    store: MaterializationReattestor, claim: ClaimRecord, began_at: str
) -> MaterializationClaim:
    """Bind a held graph claim to its re-attestation (S6 §2.4).

    The callable commits one transaction: a lease-conditioned `UpdateItem` on `EXEC_CLAIM#`
    that sets `materialization_began_at`, and a `RUN#` ConditionCheck for OPEN or PAUSED. That
    commit is both the FR-5 re-attestation and the takeover fact `MaterializationFacts` reads.
    A refused commit (a guard) is False, so `fetch` raises `MaterializationClaimLostError`;
    any other store failure propagates as the dependency it is.
    """

    def reattest(node_id: str, lease_version: int) -> bool:
        if (node_id, lease_version) != (claim.node_id, claim.lease_version):
            return False
        try:
            store.reattest_materialization(claim, began_at)
        except GraphGuardError:
            return False
        return True

    return MaterializationClaim(claim.node_id, claim.lease_version, reattest)


class GovernedMaterializationPort:
    """The one governed raw fetch over the raw bucket.

    The design's `GovernedMaterializationPort`, held as a concrete boundary because the port
    layer may not name the landing adapter's types.
    """

    def __init__(
        self,
        bucket: str,
        client: S3Client,
        fetch: Callable[[S3Client, str, ManifestInput], LoadedDelivery] = fetch_v2_delivery,
    ) -> None:
        self._bucket = bucket
        self._client = client
        self._fetch = fetch

    def _facts(self, manifest: ManifestInput) -> ManifestFacts:
        try:
            body = self._client.get_object(
                Bucket=self._bucket, Key=manifest.key, VersionId=manifest.version_id
            )
            text = body["Body"].read(_MAX_MANIFEST_BYTES + 1)
        except (BotoCoreError, ClientError) as error:
            raise DependencyError(
                ErrorCode("EXEC_GRAPH", 43),
                "the manifest version could not be read for the historical check.",
                context={"cause": type(error).__name__},
            ) from None
        try:
            parsed = read_manifest(text[:_MAX_MANIFEST_BYTES].decode("utf-8"))
        except LandingError, UnicodeDecodeError:
            raise InvariantError(
                MANIFEST_INVALID, "the manifest version is not a valid manifest."
            ) from None
        return ManifestFacts(
            parsed.source_system, parsed.source_dataset, parsed.period_end, parsed.retrieved_at
        )

    def require_not_historical(self, manifest: ManifestInput, reason: str = "") -> None:
        """Apply the historical predicate to the exact manifest version (every mode)."""
        facts = self._facts(manifest)
        if derive_admission_class(facts, contract_policy(), reason) is AdmissionClass.HISTORICAL:
            raise FullHistoryRefusedError(manifest.key)

    def fetch(
        self, manifest: ManifestInput, claim: MaterializationClaim | None = None
    ) -> LoadedDelivery:
        """Refuse a historical manifest, re-attest a graph claim, then fetch the delivery."""
        self.require_not_historical(manifest)
        if claim is not None and not claim.reattest(claim.node_id, claim.lease_version):
            raise MaterializationClaimLostError
        return self._fetch(self._client, self._bucket, manifest)
