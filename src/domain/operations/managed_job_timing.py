# SPDX-License-Identifier: MIT-0
"""`InJobTimingManifest.v1`: the shared managed-job executor timing contract (BF-11).

A managed job reports its in-job stages in one manifest bound to its provider job. S1 owns the
contract and the refusal. S5 adds the verifier and span synthesizer that run after provider
terminal (`verify_after_terminal`); the managed-job adapter that feeds them and exports the
spans is built by the first consumer (forecasting). A manifest bound to another provider job, from
an undeclared executor, or naming an undeclared stage is refused with
`MANAGED_JOB_TIMING_INVALID`, and no span is synthesized: the closed timing gap is recorded
instead. One validated manifest yields one span set; a replay of the same manifest yields the
same digest, so it is deduplicated rather than emitted twice.

Pure: no I/O, no clock, no AWS.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from domain.operations.generated_execution_graphs import MANAGED_JOB_EXECUTORS, TIMING_MANIFEST
from domain.operations.status import canonical_authority_digest
from domain.operations.table_attempt import ProviderTerminalState
from domain.shared_kernel.errors import ErrorCode, ValidationError

__all__ = [
    "JOB_SPAN",
    "MANAGED_JOB_TIMING_INVALID",
    "STAGE_SPAN",
    "ManagedJobTimingError",
    "ProviderTerminal",
    "SpanLink",
    "SynthesizedSpan",
    "TimingGap",
    "TimingOutcome",
    "TimingStage",
    "TimingVerification",
    "ValidatedTiming",
    "synthesize_spans",
    "validate_timing_manifest",
    "verify_after_terminal",
]

MANAGED_JOB_TIMING_INVALID: Final = "MANAGED_JOB_TIMING_INVALID"
_VERSION: Final = "InJobTimingManifest.v1"
_WALL_CLOCK: Final = "wall_clock"
_TERMINAL_STATES: Final = frozenset(state.value for state in ProviderTerminalState)


class ManagedJobTimingError(ValidationError):
    """The manifest cannot be trusted: no span; the timing gap is recorded."""

    def __init__(self, description: str) -> None:
        super().__init__(ErrorCode("MANAGED_JOB", 1), description)
        self.signal = MANAGED_JOB_TIMING_INVALID


@dataclass(frozen=True, slots=True)
class TimingStage:
    """One declared in-job stage."""

    stage: str
    started_at: datetime
    ended_at: datetime


@dataclass(frozen=True, slots=True)
class ValidatedTiming:
    """A manifest bound to its provider job, ready for one span set."""

    provider_job_id: str
    executor: str
    clock_source: str
    """Which clock produced every stage instant (explicit clock provenance)."""
    stages: tuple[TimingStage, ...]
    digest: str


def _moment(value: object) -> datetime:
    if not isinstance(value, str):
        raise ManagedJobTimingError("a stage instant is not a string.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ManagedJobTimingError("a stage instant is not ISO-8601.") from None
    if parsed.tzinfo is None:
        raise ManagedJobTimingError("a stage instant carries no offset.")
    return parsed


def validate_timing_manifest(
    manifest: Mapping[str, object], provider_job_id: str, declared_stages: Sequence[str]
) -> ValidatedTiming:
    """Bind the manifest to the exact provider job and its declared stages, or refuse."""
    required = TIMING_MANIFEST[_VERSION]["required"]
    if any(name not in manifest for name in required) or set(manifest) - set(required):
        raise ManagedJobTimingError("the manifest's fields are not exactly the contract's.")
    if manifest["manifest_version"] != _VERSION:
        raise ManagedJobTimingError("the manifest version is not InJobTimingManifest.v1.")
    if manifest["provider_job_id"] != provider_job_id:
        raise ManagedJobTimingError("the manifest is bound to another provider job.")
    executor = manifest["executor"]
    if executor not in MANAGED_JOB_EXECUTORS:
        raise ManagedJobTimingError("the executor is not declared.")
    clock_source = manifest["clock_source"]
    if clock_source not in TIMING_MANIFEST[_VERSION]["clock_sources"]:
        raise ManagedJobTimingError("the clock source is not declared.")
    raw = manifest["stages"]
    if not isinstance(raw, list) or not raw:
        raise ManagedJobTimingError("the manifest declares no stages.")
    stages: list[TimingStage] = []
    for item in raw:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(item, dict) or set(item) != set(  # pyright: ignore[reportUnknownArgumentType]
            TIMING_MANIFEST[_VERSION]["stage_fields"]
        ):
            raise ManagedJobTimingError("a stage's fields are not exactly the contract's.")
        name = item["stage"]  # pyright: ignore[reportUnknownVariableType]
        if name not in declared_stages:
            raise ManagedJobTimingError("the manifest names an undeclared stage.")
        stage = TimingStage(str(name), _moment(item["started_at"]), _moment(item["ended_at"]))  # pyright: ignore[reportUnknownArgumentType]
        if stage.ended_at < stage.started_at:
            raise ManagedJobTimingError("a stage ends before it starts.")
        stages.append(stage)
    if len({stage.stage for stage in stages}) != len(stages):
        raise ManagedJobTimingError("a stage is reported twice.")
    digest = canonical_authority_digest(
        [
            _VERSION,
            provider_job_id,
            str(executor),
            str(clock_source),
            [[s.stage, s.started_at.isoformat(), s.ended_at.isoformat()] for s in stages],
        ]
    )
    return ValidatedTiming(provider_job_id, str(executor), str(clock_source), tuple(stages), digest)


# S5 half (BF-11): the verifier and span synthesizer that run after provider terminal.


@dataclass(frozen=True, slots=True)
class ProviderTerminal:
    """The provider's own terminal record for one managed job, which the manifest binds to."""

    provider_job_id: str
    executor: str
    started_at: datetime
    terminal_at: datetime
    state: str
    """The provider's terminal state: `SUCCEEDED`, `FAILED` or `CANCELLED`."""


@dataclass(frozen=True, slots=True)
class SpanLink:
    """One declared link: `child_of` the job span, or `follows_from` the root flow."""

    kind: str
    target: str


@dataclass(frozen=True, slots=True)
class SynthesizedSpan:
    """One span the synthesizer emits, with deterministic identity and only declared links."""

    span_id: str
    name: str
    stage: str | None
    started_at: datetime
    ended_at: datetime
    links: tuple[SpanLink, ...]


@dataclass(frozen=True, slots=True)
class TimingGap:
    """The closed timing gap recorded instead of a span when the manifest cannot be trusted."""

    provider_job_id: str
    signal: str
    reason: str


class TimingOutcome(StrEnum):
    """What one verification run produced."""

    SPANS = "spans"
    """One span set: the job span and one span per declared stage."""
    GAP = "gap"
    """No span; the closed timing gap is recorded (`MANAGED_JOB_TIMING_INVALID`)."""
    ALREADY_EMITTED = "already_emitted"
    """An exact replay of an emitted manifest: no duplicate span."""


@dataclass(frozen=True, slots=True)
class TimingVerification:
    """The verifier's one result for one provider terminal."""

    outcome: TimingOutcome
    digest: str | None = None
    spans: tuple[SynthesizedSpan, ...] = ()
    gap: TimingGap | None = None


JOB_SPAN: Final = "managed_job.run"
STAGE_SPAN: Final = "managed_job.stage"


def _span_id(digest: str, name: str, stage: str | None) -> str:
    return canonical_authority_digest(["managed-job-span/v1", digest, name, stage or ""])[:16]


def synthesize_spans(
    timing: ValidatedTiming, terminal: ProviderTerminal, root_flow_id: str
) -> tuple[SynthesizedSpan, ...]:
    """Build the one span set: the job span (linked to the root flow) and its stage spans."""
    job_id = _span_id(timing.digest, JOB_SPAN, None)
    job = SynthesizedSpan(
        span_id=job_id,
        name=JOB_SPAN,
        stage=None,
        started_at=terminal.started_at,
        ended_at=terminal.terminal_at,
        links=(SpanLink("follows_from", root_flow_id),),
    )
    stages = tuple(
        SynthesizedSpan(
            span_id=_span_id(timing.digest, STAGE_SPAN, stage.stage),
            name=STAGE_SPAN,
            stage=stage.stage,
            started_at=stage.started_at,
            ended_at=stage.ended_at,
            links=(SpanLink("child_of", job_id),),
        )
        for stage in timing.stages
    )
    return (job, *stages)


def verify_after_terminal(  # noqa: PLR0913, PLR0917 -- one call joins its five authorities
    manifest: Mapping[str, object],
    terminal: ProviderTerminal,
    declared_stages: Sequence[str],
    root_flow_id: str,
    emitted_digests: frozenset[str],
) -> TimingVerification:
    """Verify one manifest against its provider terminal; synthesize spans once or record a gap.

    The provider record must be terminal (`SUCCEEDED`, `FAILED` or `CANCELLED`). The manifest
    must bind to the exact provider job and executor, name only declared stages, and place every
    stage inside the provider's own started-to-terminal interval. That interval is wall-clock
    time, so only a `wall_clock` manifest can be placed in it: a `monotonic` manifest has no
    anchor to it and is refused until a consumer defines one. A refusal emits no span and
    records the closed gap. A manifest whose digest was already emitted is an exact replay and
    emits nothing.
    """
    try:
        if terminal.state not in _TERMINAL_STATES:
            raise ManagedJobTimingError("the provider record is not terminal.")
        timing = validate_timing_manifest(manifest, terminal.provider_job_id, declared_stages)
        if timing.executor != terminal.executor:
            raise ManagedJobTimingError("the manifest's executor is not the provider's.")
        if timing.clock_source != _WALL_CLOCK:
            raise ManagedJobTimingError(
                "a monotonic manifest has no anchor to the provider's wall-clock interval."
            )
        for stage in timing.stages:
            if stage.started_at < terminal.started_at or stage.ended_at > terminal.terminal_at:
                raise ManagedJobTimingError("a stage lies outside the provider job's interval.")
    except ManagedJobTimingError as error:
        return TimingVerification(
            TimingOutcome.GAP,
            gap=TimingGap(terminal.provider_job_id, error.signal, error.description),
        )
    if timing.digest in emitted_digests:
        return TimingVerification(TimingOutcome.ALREADY_EMITTED, digest=timing.digest)
    return TimingVerification(
        TimingOutcome.SPANS,
        digest=timing.digest,
        spans=synthesize_spans(timing, terminal, root_flow_id),
    )
