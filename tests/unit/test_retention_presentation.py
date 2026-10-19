# SPDX-License-Identifier: MIT-0
"""Public Retention v2 route over one attested retained vintage body."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, cast

import pytest
from test_presentation_handler import (
    METRIC_CATALOG,
    SCHEMAS,
    SESSION,
    RecordingCatalog,
    RecordingPages,
    _body,  # pyright: ignore[reportPrivateUsage]
    _event,  # pyright: ignore[reportPrivateUsage]
    _PresentationModule,  # pyright: ignore[reportPrivateUsage]
    presentation_module,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)
from test_retention import SEPTEMBER, US, _market, _standard  # pyright: ignore[reportPrivateUsage]

from domain.presentation.absence_copy import AbsenceTrigger, absence_message
from domain.presentation.preset_artifacts import (
    PresetArtifactBody,
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactPointerRosterRequest,
    PresetArtifactPointerRosterResult,
    PresetArtifactRevision,
    PresetArtifactVariant,
    canonical_preset_artifact_json,
)
from domain.presentation.retention import (
    RetentionCohortArtifact,
    RetentionVintage,
    retention_artifact_payload,
)
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.identity import CommerceScopeKey
from domain.shared_kernel.errors import InvariantError
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit]
PATH = "/api/presentation/retention"
PAGE = PresetArtifactPage("retention")
VARIANT = PresetArtifactVariant("latest_with_current_publication")
NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)
TOKENS = tuple(letter * 43 for letter in "ABCDE")


def _payload(outcome: str = "complete") -> dict[str, object]:
    standard = (
        None if outcome == "empty" else _standard(size=0 if outcome == "insufficient" else 400)
    )
    vintage = RetentionVintage(SEPTEMBER, US, standard)
    return retention_artifact_payload(RetentionCohortArtifact(SEPTEMBER, (_market(vintage),)))


def _first_market(payload: dict[str, object]) -> dict[str, Any]:
    return cast("list[dict[str, Any]]", payload["markets"])[0]


def _first_standard_cell(payload: dict[str, object]) -> dict[str, Any]:
    market = _first_market(payload)
    return cast(
        "list[dict[str, Any]]",
        cast("list[dict[str, Any]]", market["triangle"]["standard"])[0]["rows"],
    )[0]["cells"][0]


@dataclass
class _Store:
    payload: dict[str, object]
    missing: bool = False
    malformed_pointer: bool = False
    reads: list[str] = field(default_factory=list[str])

    def __post_init__(self) -> None:
        revision = PresetArtifactRevision.from_publication_identities(
            ("retention-fictional",), render_format=1
        )
        self.body = PresetArtifactBody(
            PAGE, VARIANT, revision, canonical_preset_artifact_json(self.payload)
        )
        self.pointer = (
            None
            if self.missing
            else PresetArtifactPointer(
                PAGE,
                VARIANT,
                revision,
                self.body.digest,
                1,
                None,
                None,
                resolved_window=None
                if self.malformed_pointer
                else DateRange(date(2025, 9, 1), date(2026, 9, 30)),
            )
        )

    def roster(
        self, request: PresetArtifactPointerRosterRequest
    ) -> PresetArtifactPointerRosterResult:
        self.reads.append(request.page.value)
        return PresetArtifactPointerRosterResult(request, (self.pointer,))

    def read(self, pointer: PresetArtifactPointer) -> PresetArtifactBody:
        assert self.pointer is not None and pointer == self.pointer
        self.reads.append("body")
        return self.body


def _serve(  # noqa: PLR0913 -- route fixture plus injectable retained-store control
    module: _PresentationModule,
    payload: dict[str, object],
    request: dict[str, object] | None = None,
    *,
    store: _Store | None = None,
) -> tuple[dict[str, Any], _Store, RecordingPages]:
    store = store or _Store(payload)
    pages = RecordingPages(tokens=TOKENS, clock=lambda: NOW)
    runtime = module.runtime_factory(
        catalog=RecordingCatalog(),
        page_context=pages,
        page_artifacts=pages,
        parent_capability_issuance=pages,
        scope=AuthorizedAnalyticalScope(frozenset({CommerceScopeKey("amazon_retail_us")})),
        clock=pages.clock,
        token_minter=iter(TOKENS).__next__,
        schemas=SCHEMAS,
        metric_catalog=METRIC_CATALOG,
        preset_artifact_bodies=store,
        preset_artifact_pointers=store,
    )
    event = _event(PATH, {"session_id": SESSION, **(request or {})})
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}
    response = module.handle(event, runtime)
    return cast("dict[str, Any]", _body(response)), store, pages


def test_public_retention_route_binds_request_response_vintage_revision_and_all_context_copies(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    payload = _payload()
    baked_revision = cast(
        "dict[str, object]",
        cast(
            "list[object]",
            cast("dict[str, object]", cast("list[object]", payload["markets"])[0])["insights"],
        )[0],
    )["artifact_revision"]
    response, store, pages = _serve(presentation_module, payload)
    assert response["kind"] == "page"
    assert response["temporal"]["resolved"] == {
        "as_of_month": "2026-09",
        "label_text": "Cohorts through September 2026",
        "period_label": "September 2026",
        "window": {"start": "2025-09-01", "end": "2026-09-30"},
    }
    assert (response["temporal"]["mode"], "vintages" in response["temporal"]) == ("fixed", False)
    page = response["page"]
    assert (page["page_id"], page["route"], page["artifact_revision"]) == (
        "retention",
        "/retention/",
        pages.artifact_records[0].reference.value,
    )
    stored = cast("dict[str, Any]", pages.artifact_records[0].artifact.payload)
    served_insight = page["artifact"]["markets"][0]["insights"][0]
    assert served_insight["artifact_revision"] == page["artifact_revision"]
    assert served_insight["artifact_revision"] != baked_revision
    assert stored["markets"][0]["insights"][0] == served_insight
    assert (
        page["starter_questions"]
        == page["artifact"]["starter_questions"]
        == stored["starter_questions"]
    )
    assert (
        page["page_context_label"]
        == page["artifact"]["page_context_label"]
        == stored["page_context_label"]
    )
    assert store.reads == ["retention", "body"]


def test_missing_retention_pointer_serves_typed_calm_state_while_malformed_pointer_fails_closed(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    missing = _Store(_payload(), missing=True)
    response, store, pages = _serve(presentation_module, missing.payload, store=missing)
    assert response == {
        "envelope_version": 2,
        "expected_after": None,
        "kind": "window_unavailable",
        "message": absence_message(
            AbsenceTrigger.RETENTION_VINTAGE_NOT_PUBLISHED,
            period_label=None,
            other_window_ready=False,
        ),
        "reason_code": "vintage_unavailable",
        "recovery": {
            "action_id": None,
            "no_self_service_reason": "wait_for_source_coverage",
        },
        "session_id": SESSION,
        "temporal": {"mode": "fixed", "resolved": None, "version": 2},
    }
    assert store.reads == ["retention"]
    assert pages.artifact_records == []

    malformed = _Store(_payload(), malformed_pointer=True)
    with pytest.raises(InvariantError):
        _serve(presentation_module, malformed.payload, store=malformed)
    assert malformed.reads == ["retention"]


def test_malformed_retention_request_is_rejected_before_pointer_access(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    missing = _Store(_payload(), missing=True)
    with pytest.raises(Exception, match="presentation request fields are invalid"):
        _serve(
            presentation_module,
            missing.payload,
            {"unexpected": True},
            store=missing,
        )
    assert missing.reads == []


@pytest.mark.parametrize("outcome", ("empty", "insufficient"))
def test_retention_absence_bodies_serve_no_starter_fallback(
    presentation_module: _PresentationModule,  # noqa: F811
    outcome: str,
) -> None:
    response, _, pages = _serve(presentation_module, _payload(outcome))
    assert response["page"]["artifact"]["starter_questions"] == []
    assert pages.artifact_records[0].artifact.payload["starter_questions"] == []


@pytest.mark.parametrize("month", ("2026-09", "2026-08"))
def test_retention_refuses_every_vintage_selection_before_any_read(
    presentation_module: _PresentationModule,  # noqa: F811
    month: str,
) -> None:
    """Latest only: even the served month is refused when selected, before a pointer read."""
    store = _Store(_payload())
    with pytest.raises(Exception, match="temporal_selection is invalid"):
        _serve(
            presentation_module,
            store.payload,
            {"temporal_selection": {"kind": "vintage", "as_of_month": month}},
            store=store,
        )
    assert store.reads == []
    # Control: the same request without a selection serves the latest vintage.
    response, _, _ = _serve(presentation_module, _payload())
    assert response["kind"] == "page"


def test_retention_rejects_a_malformed_body_without_fallback(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    malformed = _payload()
    cast("dict[str, object]", malformed["vintage"])["label"] = ""
    with pytest.raises(InvariantError):
        _serve(presentation_module, malformed)


def test_retention_server_boundary_rejects_invalid_path_followup_outcome_and_evidence_matrices(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    valid, _, _ = _serve(presentation_module, _payload())
    assert valid["kind"] == "page"

    invalid: list[dict[str, object]] = []
    withheld = deepcopy(_payload())
    _first_standard_cell(withheld).update(
        cohort_size=None, rate=None, state="withheld", text="cohort sizes changed between runs"
    )
    invalid.append(withheld)

    missing_size = deepcopy(_payload())
    _first_standard_cell(missing_size)["cohort_size"] = None
    invalid.append(missing_size)

    mismatched_size_phrase = deepcopy(_payload())
    market = _first_market(mismatched_size_phrase)
    second = cast(
        "list[dict[str, Any]]",
        cast("list[dict[str, Any]]", market["triangle"]["standard"])[0]["rows"],
    )[0]["cells"][1]
    cast("dict[str, object]", second["cohort_size"])["text"] = "different customers"
    invalid.append(mismatched_size_phrase)

    wrong_followups = deepcopy(_payload())
    market = _first_market(wrong_followups)
    cast(
        "list[dict[str, Any]]",
        cast("list[dict[str, Any]]", market["triangle"]["standard"])[0]["rows"],
    )[0]["cells"].pop()
    invalid.append(wrong_followups)

    wrong_outcome = deepcopy(_payload())
    market = _first_market(wrong_outcome)
    market["outcome"] = "insufficient"
    market["absence_text"] = "There is not enough cohort evidence to show Retention safely."
    invalid.append(wrong_outcome)

    wrong_evidence = deepcopy(_payload())
    wrong_evidence["evidence_status"] = deepcopy(_payload("empty")["evidence_status"])
    invalid.append(wrong_evidence)

    missing_evidence = deepcopy(_payload())
    del missing_evidence["evidence_status"]
    invalid.append(missing_evidence)

    duplicate_evidence = deepcopy(_payload("empty"))
    evidence = cast("dict[str, object]", duplicate_evidence["evidence_status"])
    evidence["slots"] = [*cast("list[object]", evidence["slots"])] * 2
    invalid.append(duplicate_evidence)

    for candidate in invalid:
        with pytest.raises(InvariantError):
            _serve(presentation_module, candidate)
