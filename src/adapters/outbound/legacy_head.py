# SPDX-License-Identifier: MIT-0
"""The legacy head of a dataset window, derived from immutable legacy authority (S4, BF-8).

`H(U)` is the larger of the graph head and the legacy head: the newest retrieval of the window
whose legacy plan reached `COMPLETED`. It is never copied into the graph. It is read as
`design.md`, "Release", describes:

1. one `ListObjectsV2` over the window's `retrieved=` prefix lists its retrievals (S3 listing is
   strongly consistent); a listing beyond the generated bound refuses with
   `conflict_unit_history_over_bound`;
2. only retrievals newer than the run's `r` are examined, because no other can raise `H` above
   `r`;
3. for each, the manifest's exact versions are listed (only entries whose key equals the key),
   the legacy plan digest of each `(key, version)` is derived exactly as the ingestion handler
   derives it, and the successor owner `WRITE_PLAN#<digest>` and its last plan's `META` are read
   strongly from the coordinator table.

The same reader answers decision row 9 in epoch 1: an exact version whose legacy plan is
`COMPLETED` is joined, never recomputed (V-27). Every read failure is a recoverable dependency
failure; nothing is written.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Final, Protocol

from botocore.exceptions import BotoCoreError, ClientError

from domain.curation.landing import MANIFEST_NAME
from domain.operations.execution_graph_release import (
    LegacyRetrieval,
    legacy_head,
    newer_retrievals,
)
from domain.operations.generated_execution_graphs import BOUNDS
from domain.operations.ingestion_root import RootRefusedError, window_identity
from domain.operations.write_plan import (
    PlanMeta,
    PlanStatus,
    SuccessorOwner,
    plan_digest_for_source_delivery,
    source_delivery_identity,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

__all__ = ["LegacyAuthorityReader", "S3CoordinatorLegacyHead"]

_READ_FAILED: Final = ErrorCode("EXEC_GRAPH", 30)
_BOUND: Final = int(BOUNDS["max_window_retrievals"])


class LegacyAuthorityReader(Protocol):
    """The strongly consistent coordinator reads the legacy head needs."""

    def owner(self, plan_digest: str) -> SuccessorOwner | None:
        """Read the one `SUCCESSOR_OWNER` of a plan digest."""
        ...

    def plan_meta(self, plan_key: str) -> PlanMeta | None:
        """Read one plan `META`."""
        ...


class S3CoordinatorLegacyHead:
    """The legacy head and row 9's legacy-completed read, over the raw bucket and coordinator."""

    def __init__(self, bucket: str, client: S3Client, authority: LegacyAuthorityReader) -> None:
        self._bucket = bucket
        self._client = client
        self._authority = authority
        self.reads: list[str] = []
        """Every `WRITE_PLAN#` digest read, so a test can prove only newer retrievals are read."""

    def completed_plan_key(self, key: str, version_id: str) -> str | None:
        """Return the exact version's legacy plan key when it reached COMPLETED, else None."""
        digest = plan_digest_for_source_delivery(source_delivery_identity(((key, version_id),)))
        self.reads.append(digest)
        owner = self._authority.owner(digest)
        if owner is None or owner.last_plan_key is None:
            return None
        meta = self._authority.plan_meta(owner.last_plan_key)
        if meta is None or meta.status is not PlanStatus.COMPLETED:
            return None
        return owner.last_plan_key

    def head_after(self, window: str, r: datetime) -> datetime | None:
        """Return the newest retrieval of the window above `r` whose legacy plan COMPLETED."""
        listing = self._retrievals(window)
        observed = [
            LegacyRetrieval(
                key,
                instant,
                tuple(
                    self.completed_plan_key(key, version) is not None
                    for version in self._versions(key)
                ),
            )
            for key, instant in newer_retrievals(listing, r)
        ]
        return legacy_head(observed)

    def _retrievals(self, window: str) -> list[tuple[str, datetime]]:
        found: list[tuple[str, datetime]] = []
        try:
            for page in self._client.get_paginator("list_objects_v2").paginate(
                Bucket=self._bucket, Prefix=f"{window}retrieved=", Delimiter="/"
            ):
                for prefix in page.get("CommonPrefixes", []):
                    key = f"{prefix.get('Prefix', '')}{MANIFEST_NAME}"
                    try:
                        identity = window_identity(key)
                    except RootRefusedError:
                        continue
                    if identity.window == window:
                        found.append((key, identity.retrieved_at))
                if len(found) > _BOUND:
                    break
        except (BotoCoreError, ClientError) as error:
            raise DependencyError(
                _READ_FAILED,
                "the window's retrievals could not be listed.",
                context={"cause": type(error).__name__},
            ) from None
        return found

    def _versions(self, key: str) -> list[str]:
        versions: list[str] = []
        try:
            for page in self._client.get_paginator("list_object_versions").paginate(
                Bucket=self._bucket, Prefix=key
            ):
                versions.extend(
                    str(item.get("VersionId", ""))
                    for item in page.get("Versions", [])
                    if item.get("Key") == key
                )
        except (BotoCoreError, ClientError) as error:
            raise DependencyError(
                _READ_FAILED,
                "a retrieval's exact versions could not be listed.",
                context={"cause": type(error).__name__},
            ) from None
        return versions
