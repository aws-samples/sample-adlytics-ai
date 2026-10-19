# SPDX-License-Identifier: MIT-0
"""Pure append-only Semantic V2 reprocessing scope and exact raw-version plans."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise

from domain.shared_kernel.values import DateRange

__all__ = [
    "ContractArtifact",
    "CreateReprocessPlanRequest",
    "RawDeliveryBinding",
    "RawObjectBinding",
    "ReprocessOperationId",
    "ReprocessPlan",
    "ReprocessRequest",
]

_OPERATION = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{2,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_RETRIEVED_TOKEN = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")


@dataclass(frozen=True, slots=True)
class ContractArtifact:
    """One exact installed data-contract artifact authorized by a reviewed replay plan."""

    contract_id: str
    digest: str

    def __post_init__(self) -> None:
        if not self.contract_id.strip():
            raise ValueError("contract artifact requires a nonblank contract id.")
        if not _DIGEST.fullmatch(self.digest):
            raise ValueError("contract artifact requires an exact lowercase SHA-256 digest.")

    @property
    def identity(self) -> str:
        """Stable selection and byte identity included in the replay plan digest."""
        return f"{self.contract_id}@{self.digest}"


@dataclass(frozen=True, slots=True)
class ReprocessOperationId:
    """Operator-supplied stable identity making retries explainable and idempotent."""

    value: str

    def __post_init__(self) -> None:
        normalized = self.value.strip()
        if not _OPERATION.fullmatch(normalized):
            raise ValueError("reprocess operation id must be 3-128 safe identifier characters.")
        object.__setattr__(self, "value", normalized)


@dataclass(frozen=True, slots=True)
class ReprocessRequest:
    """Explicit account resource, contract, and business-window scope for one plan."""

    environment: str
    stack_name: str
    bucket: str
    contracts: tuple[str, ...]
    window: DateRange
    operation_id: ReprocessOperationId
    retrieved_tokens: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label, value in (
            ("environment", self.environment),
            ("stack name", self.stack_name),
            ("bucket", self.bucket),
        ):
            if not value.strip():
                raise ValueError(f"reprocess request requires its {label}.")
        if any(not _RETRIEVED_TOKEN.fullmatch(token) for token in self.retrieved_tokens):
            raise ValueError(
                "reprocess retrieved tokens must be exact YYYYMMDDTHHMMSSZ path tokens."
            )
        if len(set(self.retrieved_tokens)) != len(self.retrieved_tokens):
            raise ValueError("reprocess request repeats a retrieved token.")
        if not self.contracts or any(not item.strip() for item in self.contracts):
            raise ValueError("reprocess request requires explicit non-empty contract scope.")
        seen: set[str] = set()
        for contract in self.contracts:
            if contract in seen:
                raise ValueError("reprocess request repeats a contract.")
            seen.add(contract)


@dataclass(frozen=True, slots=True)
class RawObjectBinding:
    """One exact retained object version and its verified manifest claims."""

    key: str
    version_id: str
    name: str
    declared_byte_count: int
    actual_byte_count: int
    declared_checksum: str
    actual_checksum: str

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.version_id.strip() or not self.name.strip():
            raise ValueError("raw object binding requires key, version and object name.")
        for label, count in (
            ("declared", self.declared_byte_count),
            ("actual", self.actual_byte_count),
        ):
            if count <= 0:
                raise ValueError(f"raw object binding requires positive {label} byte count.")
        if self.actual_byte_count not in (self.declared_byte_count,):
            raise ValueError("raw object binding byte count differs from its manifest.")
        if not self.declared_checksum or self.declared_checksum != self.actual_checksum:
            raise ValueError("raw object binding checksum differs from its manifest.")


@dataclass(frozen=True, slots=True)
class RawDeliveryBinding:
    """Exact manifest version and the sole exact object version it authorizes."""

    contract_artifacts: tuple[ContractArtifact, ...]
    manifest_key: str
    manifest_version_id: str
    manifest_checksum: str
    first_observed_at: datetime
    object: RawObjectBinding
    period: DateRange

    def __post_init__(self) -> None:
        if not self.contract_artifacts:
            raise ValueError("raw delivery binding requires at least one selected contract.")
        contract_ids = self.contract_ids
        if len(set(contract_ids)) != len(contract_ids):
            raise ValueError("raw delivery binding repeats a contract artifact.")
        if not self.manifest_key.strip() or not self.manifest_version_id.strip():
            raise ValueError("raw delivery binding requires exact manifest key and version.")
        if not self.manifest_checksum.strip():
            raise ValueError("raw delivery binding requires a manifest checksum.")
        if self.first_observed_at.tzinfo is None:
            raise ValueError("raw delivery binding first observation must be timezone-aware.")

    @property
    def contract_ids(self) -> tuple[str, ...]:
        """Contract scope derived from the exact artifacts so selection cannot drift."""
        return tuple(artifact.contract_id for artifact in self.contract_artifacts)

    @property
    def identity(self) -> str:
        """Stable exact-version and semantic-authority identity used by reviewed plans."""
        return "|".join(
            (
                ",".join(artifact.identity for artifact in self.contract_artifacts),
                self.manifest_key,
                self.manifest_version_id,
                self.manifest_checksum,
                self.first_observed_at.isoformat(),
                self.object.key,
                self.object.version_id,
                self.object.actual_checksum,
            )
        )


def _strictly_sorted(values: tuple[str, ...]) -> bool:
    return all(left < right for left, right in pairwise(values))


@dataclass(frozen=True, slots=True)
class CreateReprocessPlanRequest:
    """Complete operator scope, semantic authority, and retained evidence for one plan."""

    request: ReprocessRequest
    prefixes: tuple[str, ...]
    contract_artifacts: tuple[ContractArtifact, ...]
    semantic_authority_digest: str
    deliveries: tuple[RawDeliveryBinding, ...]


@dataclass(frozen=True, slots=True)
class ReprocessPlan:
    """Immutable, digest-bound dry-run/apply plan; apply never rediscovers versions."""

    request: ReprocessRequest
    prefixes: tuple[str, ...]
    contract_artifacts: tuple[ContractArtifact, ...]
    semantic_authority_digest: str
    deliveries: tuple[RawDeliveryBinding, ...]
    digest: str

    def __post_init__(self) -> None:
        selected_ids = tuple(artifact.contract_id for artifact in self.contract_artifacts)
        if selected_ids != self.request.contracts:
            raise ValueError(
                "reprocess plan contract artifacts must exactly match its selected contract scope."
            )
        if not _DIGEST.fullmatch(self.semantic_authority_digest):
            raise ValueError("reprocess plan requires an exact semantic authority digest.")
        if not self.prefixes or any(
            not prefix.strip() or prefix.startswith("/") or ".." in prefix
            for prefix in self.prefixes
        ):
            raise ValueError("reprocess plan requires safe non-root source prefixes.")
        if not _strictly_sorted(self.prefixes):
            raise ValueError("reprocess plan prefixes must be sorted and unique.")
        identities = tuple(delivery.identity for delivery in self.deliveries)
        if not _strictly_sorted(identities):
            raise ValueError("reprocess deliveries must be sorted and unique by exact version.")
        contents = CreateReprocessPlanRequest(
            request=self.request,
            prefixes=self.prefixes,
            contract_artifacts=self.contract_artifacts,
            semantic_authority_digest=self.semantic_authority_digest,
            deliveries=self.deliveries,
        )
        if self.digest != self.digest_for(contents):
            raise ValueError("reprocess plan digest does not bind its exact contents.")

    @staticmethod
    def digest_for(contents: CreateReprocessPlanRequest) -> str:
        """Hash every operator scope and exact retained-object binding deterministically."""
        request = contents.request
        payload = {
            "environment": request.environment,
            "stack_name": request.stack_name,
            "bucket": request.bucket,
            "contracts": request.contracts,
            "window": [request.window.start.isoformat(), request.window.end.isoformat()],
            "operation_id": request.operation_id.value,
            **({"retrieved_tokens": request.retrieved_tokens} if request.retrieved_tokens else {}),
            "prefixes": contents.prefixes,
            "contract_artifacts": [artifact.identity for artifact in contents.contract_artifacts],
            "semantic_authority_digest": contents.semantic_authority_digest,
            "deliveries": [delivery.identity for delivery in contents.deliveries],
        }
        rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(rendered.encode()).hexdigest()}"

    @classmethod
    def create(cls, contents: CreateReprocessPlanRequest) -> ReprocessPlan:
        """Normalize an already verified exact-version plan and bind its digest."""
        normalized_prefixes = tuple(sorted(set(contents.prefixes)))
        normalized_deliveries = tuple(sorted(contents.deliveries, key=lambda item: item.identity))
        normalized = CreateReprocessPlanRequest(
            request=contents.request,
            prefixes=normalized_prefixes,
            contract_artifacts=contents.contract_artifacts,
            semantic_authority_digest=contents.semantic_authority_digest,
            deliveries=normalized_deliveries,
        )
        return cls(
            request=normalized.request,
            prefixes=normalized.prefixes,
            contract_artifacts=normalized.contract_artifacts,
            semantic_authority_digest=normalized.semantic_authority_digest,
            deliveries=normalized.deliveries,
            digest=cls.digest_for(normalized),
        )
