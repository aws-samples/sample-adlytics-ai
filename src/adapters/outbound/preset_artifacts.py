# SPDX-License-Identifier: MIT-0
"""Body-first composition of independently owned preset-artifact persistence ports.

Plan 4.5 (DB20) and ADR-145 (streamed writes and the sharded sibling index): `stage` draws a
publication's sibling stream one pair at a time and writes each as an ordered body->custody
pair, at most `BAKE_WRITE_WIDTH` (the contract's `bake_tasks.baker_write_width`) pairs in
flight, keeping only each pair's descriptor (identity, body digest, custody digest). On the first
failure (a write, or the stream's own refusal or defect) no further pair is submitted, every
in-flight pair is observed, and the first error is raised. Then the descriptors, sorted by
identity path, are cut into index shards of `SIBLING_INDEX_SHARD_ENTRIES` written at
`custody/{page}/{variant}/{revision}/index/segment-NNNN.json`; the parent custody is the source's
header plus one `sibling_index` entry per shard; the parent body, then that custody, are written
last. `stage` stops there (a GRAPH
bake task has no pointer capability); `publish` stages and then advances the pointer, completed
from the request's template with the staged custody's digest.

Retail's closed child set (ADR-125 as amended by ADR-145): a request's `closed_children` must
equal the stream's identities (the served rows' children and, from render format 12, the one
Products and ads part), checked before any write.

The runtime write deadline (ADR-145, ADR-142): `stage` and `publish` take the invocation's
`remaining_ms` and the contract's `tail_seconds` as required keywords. Before each pair is
submitted, `remaining_ms() / 1000 < tail_seconds` stops submission, every in-flight pair is
observed, and the stage raises `IsolatedRefusal(bodies_over_invocation, scope=page, written=k,
bodies=n)`: no index shard, parent, custody or pointer is written, so the pairs already written
are unreferenced objects under a revision no custody names.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from adapters.outbound.telemetry.span_context import correlation
from adapters.outbound.telemetry.tracing import ARTIFACT_WRITE, span, trace_boundary
from domain.operations.bake_memory import BAKE_WRITE_WIDTH
from domain.operations.bake_refusals import (
    IsolatedRefusal,
    IsolatedRefusalReason,
    RefusalScope,
)
from domain.presentation.preset_artifacts import (
    PRESET_ARTIFACT_POINTER_CONFLICT,
    ArtifactBodyWriteResult,
    CustodyManifestWriteResult,
    PresetArtifactCustodyManifest,
    PresetArtifactPublicationRequest,
    PresetArtifactPublicationResult,
    PresetArtifactSiblingCustodyWriteResult,
    PresetArtifactSiblingDescriptor,
    PresetArtifactSiblingIndexEntry,
    PresetArtifactSiblingWriteResult,
    PresetArtifactStageResult,
    parent_custody_manifest,
    sibling_index_shards,
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
        PresetArtifactSiblingPair,
    )

__all__ = ["PresetArtifactPersistence"]

_Pair = tuple[PresetArtifactSiblingWriteResult, PresetArtifactSiblingCustodyWriteResult]


class PresetArtifactPersistence(PresetArtifactPersistencePort, PresetArtifactStagePort):
    """Persist a body before exposing it through a shared DynamoDB pointer."""

    def __init__(  # noqa: PLR0913 -- three stores and the contract write width
        self,
        bodies: PresetArtifactBodyStorePort,
        pointers: PresetArtifactPointerStorePort,
        custody: PresetArtifactCustodyStorePort | None = None,
        *,
        write_width: int = BAKE_WRITE_WIDTH,
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
        """Stage the whole revision, then advance the pointer completed with its custody digest.

        A stage refused by its write deadline raises before the pointer: no pointer moves.
        """
        staged = self.stage(request, remaining_ms=remaining_ms, tail_seconds=tail_seconds)
        pointer_result = self._pointers.advance(
            request.pointer_template.complete(staged.custody_digest)
        )
        return PresetArtifactPublicationResult(
            staged.body, pointer_result, staged.custody, staged.pairs, staged.written_bytes
        )

    def stage(
        self,
        request: PresetArtifactPublicationRequest,
        *,
        remaining_ms: Callable[[], int],
        tail_seconds: float,
    ) -> PresetArtifactStageResult:
        """Write and attest every immutable object of one revision; never touch a pointer.

        In order (ADR-145): the closed-child check, the sibling stream (deadline before each
        pair), the index shards, the parent body, then the parent custody built from the
        header and the index (module docstring).
        """
        context = _context(request)
        if request.custody_header is not None and self._custody is None:
            raise InvariantError(
                PRESET_ARTIFACT_POINTER_CONFLICT,
                "A custody-bearing publication requires a composed custody store.",
                context=context,
            )
        _require_closed_children(request)
        descriptors, sibling_bytes = self._write_siblings(
            request, _Deadline(remaining_ms, tail_seconds)
        )
        index, index_bytes = self._write_index(request, descriptors)
        custody = (
            None
            if request.custody_header is None
            else parent_custody_manifest(request.body, request.custody_header, index)
        )
        size = _size(request.body.payload_json) + (
            0 if custody is None else _size(custody.payload_json)
        )
        with _artifact_chain("parent", size):
            body, custody_result = self._write_parent(request, custody)
        return PresetArtifactStageResult(
            body,
            custody_result,
            len(descriptors) + len(index) + 1,
            sibling_bytes + index_bytes + size,
        )

    def _write_parent(
        self,
        request: PresetArtifactPublicationRequest,
        custody: PresetArtifactCustodyManifest | None,
    ) -> tuple[ArtifactBodyWriteResult, CustodyManifestWriteResult | None]:
        """Write and attest the parent body, then its custody, after the siblings and index."""
        template = request.pointer_template
        body = self._bodies.write(request.body)
        if (body.page, body.variant, body.revision, body.key, body.digest) != (
            template.page,
            template.variant,
            template.revision,
            request.body.key,
            template.body_digest,
        ):
            raise InvariantError(
                PRESET_ARTIFACT_POINTER_CONFLICT,
                "Preset artifact body persistence did not attest the requested pointer evidence.",
                context=_context(request),
            )
        if custody is None:
            return body, None
        store = self._custody
        if store is None:  # checked before any write; held for the type
            raise InvariantError(
                PRESET_ARTIFACT_POINTER_CONFLICT,
                "A custody-bearing publication requires a composed custody store.",
                context=_context(request),
            )
        written = store.write(custody)
        if (written.page, written.variant, written.revision, written.key, written.digest) != (
            custody.page,
            custody.variant,
            custody.revision,
            custody.key,
            custody.digest,
        ):
            raise InvariantError(
                PRESET_ARTIFACT_POINTER_CONFLICT,
                "Preset artifact custody persistence did not attest the requested "
                "pointer evidence.",
                context=_context(request),
            )
        return body, written

    def _write_siblings(
        self, request: PresetArtifactPublicationRequest, deadline: _Deadline
    ) -> tuple[tuple[PresetArtifactSiblingDescriptor, ...], int]:
        """Stream the bounded body->custody pair jobs, keeping only each pair's descriptor.

        Each submission first checks the write deadline, then draws the stream's next pair; once
        the deadline is reached, a pair fails, or the stream raises, nothing more is submitted,
        every in-flight pair is observed, and the first error (or the refusal) is raised.
        """
        stream = request.siblings
        pairs = iter(stream)
        descriptors: list[PresetArtifactSiblingDescriptor] = []
        written_bytes = 0
        seen: set[str] = set()
        state = _StreamState()
        with ThreadPoolExecutor(
            max_workers=min(self._width, max(1, stream.count)),
            thread_name_prefix="preset-artifact-write",
        ) as executor:
            in_flight: set[Future[tuple[PresetArtifactSiblingDescriptor, int]]] = set()

            def submit_next() -> None:
                if state.stopped():
                    return
                if state.submitted == stream.count:
                    if next(pairs, None) is not None:
                        state.first_error = _stream_mismatch(request)
                    state.exhausted = True
                    return
                if deadline.reached():
                    state.expired = True
                    return
                try:
                    pair = next(pairs, None)
                    public, private = _admitted(request, pair, seen)
                except Exception as error:  # the stream's own refusal or defect, raised after
                    state.first_error = error
                    return
                state.submitted += 1
                # A copy of the caller's context parents each chain's span (plan A2).
                in_flight.add(
                    executor.submit(copy_context().run, self._write_pair, public, private)
                )

            for _ in range(self._width):
                submit_next()
            while in_flight:
                done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in done:
                    in_flight.discard(future)
                    try:
                        descriptor, size = future.result()
                    except Exception as error:  # observe every in-flight pair before raising
                        if state.first_error is None:
                            state.first_error = error
                        continue
                    descriptors.append(descriptor)
                    written_bytes += size
                    submit_next()
            if not state.stopped() and state.submitted == stream.count:
                submit_next()  # a stream with nothing left to submit still proves its count
        if state.first_error is not None:
            raise state.first_error
        if state.expired:
            raise IsolatedRefusal(
                IsolatedRefusalReason.BODIES_OVER_INVOCATION,
                RefusalScope.PAGE,
                written=len(descriptors),
                bodies=stream.count,
            )
        return tuple(descriptors), written_bytes

    def _write_index(
        self,
        request: PresetArtifactPublicationRequest,
        descriptors: tuple[PresetArtifactSiblingDescriptor, ...],
    ) -> tuple[tuple[PresetArtifactSiblingIndexEntry, ...], int]:
        """Write the sorted sibling index, 800 descriptors per shard, each its own chain."""
        if request.custody_header is None:
            return (), 0
        body = request.body
        try:
            shards = sibling_index_shards(body, descriptors)
        except ValueError as error:
            raise InvariantError(
                PRESET_ARTIFACT_POINTER_CONFLICT,
                "A preset artifact sibling index cannot be built.",
                context={**_context(request), "cause": str(error)},
            ) from None
        written = 0
        store = cast("PresetArtifactCustodyStorePort", self._custody)
        for shard, _ in shards:
            size = _size(shard.payload_json)
            with _artifact_chain("index", size):
                result = store.write_sibling(shard)
                if (result.identity, result.key, result.digest) != (
                    shard.identity,
                    shard.key,
                    shard.digest,
                ):
                    raise InvariantError(
                        PRESET_ARTIFACT_POINTER_CONFLICT,
                        "Sibling index persistence did not attest requested evidence.",
                        context=_context(request),
                    )
            written += size
        return tuple(entry for _, entry in shards), written

    def _write_pair(
        self, public: PresetArtifactSiblingBody, private: PresetArtifactSiblingCustody
    ) -> tuple[PresetArtifactSiblingDescriptor, int]:
        """Write and attest one sibling's body->custody chain inside its `artifact.write` span."""
        size = _size(public.payload_json) + _size(private.payload_json)
        with _artifact_chain("sibling", size):
            public_result, private_result = self._attested_pair(public, private)
        return (
            PresetArtifactSiblingDescriptor(
                public.identity, public_result.digest, private_result.digest
            ),
            size,
        )

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


@dataclass(slots=True)
class _StreamState:
    """What one stage's stream loop has submitted, and why it stopped, if it has."""

    submitted: int = 0
    expired: bool = False
    exhausted: bool = False
    first_error: BaseException | None = None

    def stopped(self) -> bool:
        return self.expired or self.exhausted or self.first_error is not None


def _context(request: PresetArtifactPublicationRequest) -> dict[str, str]:
    template = request.pointer_template
    return {"partition": template.partition_key, "revision": template.revision.value}


def _stream_mismatch(request: PresetArtifactPublicationRequest) -> InvariantError:
    return InvariantError(
        PRESET_ARTIFACT_POINTER_CONFLICT,
        "A preset artifact sibling stream yielded other than its declared count.",
        context={**_context(request), "declared": str(request.siblings.count)},
    )


def _admitted(
    request: PresetArtifactPublicationRequest,
    pair: PresetArtifactSiblingPair | None,
    seen: set[str],
) -> PresetArtifactSiblingPair:
    """Admit one drawn pair: aligned, of the parent revision and not yet seen; else invariant."""
    if pair is None:
        raise _stream_mismatch(request)
    public, private = pair
    parent = request.body
    identity = public.identity
    if (
        private.identity != identity
        or any(
            (item.page, item.variant, item.revision)
            != (parent.page, parent.variant, parent.revision)
            for item in (public, private)
        )
        or identity.path in seen
    ):
        raise InvariantError(
            PRESET_ARTIFACT_POINTER_CONFLICT,
            "Preset artifact siblings are unique, aligned pairs of their parent revision.",
            context=_context(request),
        )
    seen.add(identity.path)
    return public, private


def _require_closed_children(request: PresetArtifactPublicationRequest) -> None:
    """Retail's closed child set (ADR-125, ADR-145): its rows' children and Products and ads part.

    Checked before any write, so a mismatch writes nothing.
    """
    expected = request.closed_children
    if expected is None:
        return
    identities = [public.identity for public, _ in request.siblings]
    if (
        len(identities) != request.siblings.count
        or len(set(identities)) != len(identities)
        or {identity.path for identity in identities} != {item.path for item in expected}
    ):
        raise InvariantError(
            PRESET_ARTIFACT_POINTER_CONFLICT,
            "Retail children are exactly the served rows and the Products and ads part.",
            context=_context(request),
        )


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
