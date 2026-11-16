# SPDX-License-Identifier: MIT-0
"""The run fingerprint: what a run computes from, so a change to any component is a new run.

SHA-256 of canonical JSON of the input fingerprint, the policy file's SHA-256, the processor-code
identity (the image asset hash), the lock digest, the model ID, the model artifact digest, the
seven boundary schema digests and the execution plan (instance type, runtime) (design §4b).
`maxParallelJobs` is deliberately not in it: concurrency doesn't change what a run computes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class RunIdentity:
    """Every component of a run fingerprint."""

    input_fingerprint: str
    policy_sha256: str
    processor_code_identity: str
    lock_digest: str
    model_id: str
    model_artifact_digest: str
    boundary_schema_digests: Mapping[str, str]
    instance_type: str
    max_runtime_seconds: int

    def __post_init__(self) -> None:
        for name in (
            "input_fingerprint",
            "policy_sha256",
            "processor_code_identity",
            "lock_digest",
            "model_id",
            "model_artifact_digest",
            "instance_type",
        ):
            if not getattr(self, name):
                raise ValueError(f"run identity: {name} must be non-empty")
        if self.max_runtime_seconds < 1:
            raise ValueError("run identity: max_runtime_seconds must be at least 1")
        if not self.boundary_schema_digests:
            raise ValueError("run identity: boundary_schema_digests must be non-empty")

    def as_json(self) -> dict[str, object]:
        """Return the identity as plain JSON values (the manifest's `run_identity`)."""
        return {
            "input_fingerprint": self.input_fingerprint,
            "policy_sha256": self.policy_sha256,
            "processor_code_identity": self.processor_code_identity,
            "lock_digest": self.lock_digest,
            "model_id": self.model_id,
            "model_artifact_digest": self.model_artifact_digest,
            "boundary_schema_digests": dict(sorted(self.boundary_schema_digests.items())),
            "execution_plan": {
                "instance_type": self.instance_type,
                "max_runtime_seconds": self.max_runtime_seconds,
            },
        }


def run_fingerprint(identity: RunIdentity) -> str:
    """Return the run fingerprint of `identity`."""
    canonical = json.dumps(identity.as_json(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def run_hash12(run_id: str) -> str:
    """Return the job name's run hash: the first 12 hex characters of SHA-256 of the run ID."""
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12]
