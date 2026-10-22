# SPDX-License-Identifier: MIT-0
"""Body-first composition of independently owned preset-artifact persistence ports.

Plan 4.5 (DB20): siblings are written as ordered body->custody pairs, at most
`BAKE_TASKS["s3_chains_per_baker"]` pairs in flight. Each pair writes and attests its public body,
then writes and attests its private custody. On the first failure no further pair is submitted,
every in-flight pair is observed, and the first error is raised. The parent body, then its
custody, are written only after every sibling pair attests. `stage` stops there (a GRAPH bake
task has no pointer capability); `publish` stages and then advances the pointer.

The runtime write deadline (ADR-145, ADR-142): `stage` and `publish` take the invocation's
`remaining_ms` and the contract's `tail_seconds` as required keywords. Before each pair is
submitted, `remaining_ms() / 1000 < tail_seconds` stops submission, every in-flight pair is
observed, and the stage raises `IsolatedRefusal(bodies_over_invocation, scope=page, written=k,
bodies=n)`: no parent, custody or pointer is written, so the pairs already written are
unreferenced objects under a revision no custody names.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Generator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from contextvars import copy_context
from typing import TYPE_CHECKING, Final

from adapters.outbound.telemetry.span_context import correlation
from adapters.outbound.telemetry.tracing import ARTIFACT_WRITE, span, trace_boundary
from domain.operations.bake_refusals import (
    IsolatedRefusal,
    IsolatedRefusalReason,
    RefusalScope,
)
from domain.operations.generated_execution_graphs import BAKE_TASKS
from domain.presentation.preset_artifacts import (
    PRESET_ARTIFACT_POINTER_CONFLICT,
    ArtifactBodyWriteResult,
    CustodyManifestWriteResult,
    PresetArtifactPublicationRequest,
    PresetArtifactPublicationResult,
    PresetArtifactSiblingCustodyWriteResult,
    PresetArtifactSiblingWriteResult,
    PresetArtifactStageResult,
)
from domain.shared_kernel.errors import InvariantError
from ports.outbound.preset_artifacts import (
    PresetArtifactBodyStorePort,
    PresetArtifactCustodyStorePort,
    PresetArtifactPersistencePort,
    PresetArtifactPointerStorePort,
    PresetArtifactStagePort,
)

if TYPE_CHECKING:
    from domain.presentation.preset_artifacts import (
        PresetArtifactSiblingBody,
        PresetArtifactSiblingCustody,
    )

__all__ = ["S3_CHAINS_PER_BAKER", "PresetArtifactPersistence"]

S3_CHAINS_PER_BAKER: Final = int(BAKE_TASKS["s3_chains_per_baker"])
"""The most sibling body->custody pairs one Baker writes at once (contract `bake_tasks`)."""

_Pair = tuple[PresetArtifactSiblingWriteResult, PresetArtifactSiblingCustodyWriteResult]


class PresetArtifactPersistence(PresetArtifactPersistencePort, PresetArtifactStagePort):
    """Persist a body before exposing it through a shared DynamoDB pointer."""

    def __init__(  # noqa: PLR0913 -- three stores and the contract write width
        self,
        bodies: PresetArtifactBodyStorePort,
        pointers: PresetArtifactPointerStorePort,
        custody: PresetArtifactCustodyStorePort | None = None,
        *,
        write_width: int = S3_CHAINS_PER_BAKER,
    ) -> None:
        if write_width < 1:
            raise ValueError("preset artifact write width must be at least one pair.")
        self._bodies = bodies
        self._pointers = pointers
        self._custody = custody
        self._width = write_width

    def publish(
        self,
        request: PresetArtifactPublicationRequest,
        *,
        remaining_ms: Callable[[], int],
        tail_seconds: float,
    ) -> PresetArtifactPublicationResult:
        """Attest every sibling before parent bytes and expose only the complete revision.

        A stage refused by its write deadline raises before the pointer: no pointer moves.
        """
        staged = self.stage(request, remaining_ms=remaining_ms, tail_seconds=tail_seconds)
        pointer_result = self._pointers.advance(request.pointer_advance)
        return PresetArtifactPublicationResult(
            staged.body,
            pointer_result,
            staged.custody,
            staged.sibling_bodies,
            staged.sibling_custody,
        )

    def stage(
        self,
        request: PresetArtifactPublicationRequest,
        *,
        remaining_ms: Callable[[], int],
        tail_seconds: float,
    ) -> PresetArtifactStageResult:
        """Write and attest every immutable object of one revision; never touch a pointer.

        Before each pair is submitted, a deadline reached refuses the page (module docstring).
        """
        pointer = request.pointer_advance.pointer
        if request.sibling_custody and self._custody is None:
            raise InvariantError(
                PRESET_ARTIFACT_POINTER_CONFLICT,
                "Sibling-bearing publication requires a composed custody store.",
                context={"partition": pointer.partition_key, "revision": pointer.revision.value},
            )
        pairs = self._write_siblings(
            tuple(zip(request.sibling_bodies, request.sibling_custody, strict=True)),
            _Deadline(remaining_ms, tail_seconds),
        )
        size = _size(request.body.payload_json) + (
            0 if request.custody is None else _size(request.custody.payload_json)
        )
        with _artifact_chain("parent", size):
            body, custody = self._write_parent(request)
        return PresetArtifactStageResult(
            body,
            custody,
            tuple(public for public, _ in pairs),
            tuple(private for _, private in pairs),
        )

    def _write_parent(
        self, request: PresetArtifactPublicationRequest
    ) -> tuple[ArtifactBodyWriteResult, CustodyManifestWriteResult | None]:
        """Write and attest the parent body, then its custody, after every sibling pair."""
        pointer = request.pointer_advance.pointer
        body = self._bodies.write(request.body)
        if (body.page, body.variant, body.revision, body.key, body.digest) != (
            pointer.page,
            pointer.variant,
            pointer.revision,
            pointer.body_key,
            pointer.body_digest,
        ):
            raise InvariantError(
                PRESET_ARTIFACT_POINTER_CONFLICT,
                "Preset artifact body persistence did not attest the requested pointer evidence.",
                context={"partition": pointer.partition_key, "revision": pointer.revision.value},
            )
        custody = None
        if request.custody is not None:
            if self._custody is None:
                raise InvariantError(
                    PRESET_ARTIFACT_POINTER_CONFLICT,
                    "A custody-bearing publication requires a composed custody store.",
                    context={
                        "partition": pointer.partition_key,
                        "revision": pointer.revision.value,
                    },
                )
            custody = self._custody.write(request.custody)
            if (
                custody.page,
                custody.variant,
                custody.revision,
                custody.key,
                custody.digest,
            ) != (
                pointer.page,
                pointer.variant,
                pointer.revision,
                pointer.custody_key,
                pointer.custody_digest,
            ):
                raise InvariantError(
                    PRESET_ARTIFACT_POINTER_CONFLICT,
                    "Preset artifact custody persistence did not attest the requested "
                    "pointer evidence.",
                    context={
                        "partition": pointer.partition_key,
                        "revision": pointer.revision.value,
                    },
                )
        return body, custody

    def _write_siblings(
        self,
        pairs: tuple[tuple[PresetArtifactSiblingBody, PresetArtifactSiblingCustody], ...],
        deadline: _Deadline,
    ) -> tuple[_Pair, ...]:
        """Run the bounded body->custody pair jobs; join every one before the parent writes.

        Each submission first checks the write deadline; once it is reached nothing more is
        submitted, the in-flight pairs are observed, and the page is refused.
        """
        if not pairs:
            return ()
        results: dict[int, _Pair] = {}
        first_error: BaseException | None = None
        expired = False
        pending = deque(enumerate(pairs))
        with ThreadPoolExecutor(
            max_workers=min(self._width, len(pairs)), thread_name_prefix="preset-artifact-write"
        ) as executor:
            in_flight: dict[Future[_Pair], int] = {}

            def submit_next() -> None:
                nonlocal expired
                if pending and not expired and deadline.reached():
                    expired = True
                if pending and not expired:
                    index, (public, private) = pending.popleft()
                    # A copy of the caller's context parents each chain's span (plan A2).
                    job = executor.submit(copy_context().run, self._write_pair, public, private)
                    in_flight[job] = index

            for _ in range(self._width):
                submit_next()
            while in_flight:
                done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in done:
                    index = in_flight.pop(future)
                    try:
                        results[index] = future.result()
                    except Exception as error:  # observe every in-flight pair before raising
                        if first_error is None:
                            first_error = error
                    if first_error is None:
                        submit_next()
        if first_error is not None:
            raise first_error
        if expired:
            raise IsolatedRefusal(
                IsolatedRefusalReason.BODIES_OVER_INVOCATION,
                RefusalScope.PAGE,
                written=len(results),
                bodies=len(pairs),
            )
        return tuple(results[index] for index in range(len(pairs)))

    def _write_pair(
        self, public: PresetArtifactSiblingBody, private: PresetArtifactSiblingCustody
    ) -> _Pair:
        """Write and attest one sibling's body->custody chain inside its `artifact.write` span."""
        with _artifact_chain("sibling", _size(public.payload_json) + _size(private.payload_json)):
            return self._attested_pair(public, private)

    def _attested_pair(
        self, public: PresetArtifactSiblingBody, private: PresetArtifactSiblingCustody
    ) -> _Pair:
        """Write and attest one sibling's public body, then its private custody."""
        public_result = self._bodies.write_sibling(public)
        if (
            public_result.page,
            public_result.variant,
            public_result.revision,
            public_result.identity,
            public_result.key,
            public_result.digest,
        ) != (
            public.page,
            public.variant,
            public.revision,
            public.identity,
            public.key,
            public.digest,
        ):
            raise InvariantError(
                PRESET_ARTIFACT_POINTER_CONFLICT,
                "Sibling body persistence did not attest requested evidence.",
            )
        custody_store = self._custody
        if custody_store is None:
            raise InvariantError(
                PRESET_ARTIFACT_POINTER_CONFLICT,
                "Sibling custody store became unavailable.",
            )
        private_result = custody_store.write_sibling(private)
        if (
            private_result.page,
            private_result.variant,
            private_result.revision,
            private_result.identity,
            private_result.key,
            private_result.digest,
        ) != (
            private.page,
            private.variant,
            private.revision,
            private.identity,
            private.key,
            private.digest,
        ):
            raise InvariantError(
                PRESET_ARTIFACT_POINTER_CONFLICT,
                "Sibling custody persistence did not attest requested evidence.",
            )
        return public_result, private_result


class _Deadline:
    """The stage's runtime write deadline: the invocation's time against the contract's tail."""

    __slots__ = ("_remaining_ms", "_tail_seconds")

    def __init__(self, remaining_ms: Callable[[], int], tail_seconds: float) -> None:
        self._remaining_ms = remaining_ms
        self._tail_seconds = tail_seconds

    def reached(self) -> bool:
        """Whether less than the tail remains, so no further pair may be submitted."""
        return self._remaining_ms() / 1_000 < self._tail_seconds


def _size(payload_json: str) -> int:
    return len(payload_json.encode())


@trace_boundary(ARTIFACT_WRITE)
@contextmanager
def _artifact_chain(object_kind: str, size: int) -> Generator[None]:
    """Own the `artifact.write` span of one body->custody chain (plan A2.1)."""
    # A GRAPH bake writes beneath its cohort correlation and records run, task and cohort; bake
    # trace completeness enforces that. The legacy intent path has no cohort and records them
    # absent rather than refusing the write it made before cohort correlation existed.
    corr = correlation()
    with span(
        ARTIFACT_WRITE,
        object_kind=object_kind,
        bytes=size,
        run_id=None if corr is None else corr.run_id,
        task_id=None if corr is None else corr.task_id,
        cohort_revision=None if corr is None else corr.cohort_revision,
    ) as recording:
        yield
        recording.record(outcome="attested")
