# SPDX-License-Identifier: MIT-0
"""Retail health product detail (ADR-125): baked children, closed custody and the one route."""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, field, replace
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from change_support import SEMANTICS
from preset_artifact_support import indexed_parent_custody
from test_presentation_handler import (
    CAMPAIGN_PLATFORM_LABELS,
    DIMENSION_LABELS,
    MARKETPLACE,
    METRIC_CATALOG,
    RETAIL_HEALTH_PERIOD,
    RETAIL_HEALTH_PRIOR,
    SCHEMAS,
    SCOPE,
    SESSION,
    RecordingCatalog,
    RecordingPages,
    _body,  # pyright: ignore[reportPrivateUsage]
    _event,  # pyright: ignore[reportPrivateUsage]
    _PresentationModule,  # pyright: ignore[reportPrivateUsage]
    _retail_health_artifact,  # pyright: ignore[reportPrivateUsage]
    _retail_health_event,  # pyright: ignore[reportPrivateUsage]
    _retail_units,  # pyright: ignore[reportPrivateUsage]
    presentation_module,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

from adapters.outbound.s3.preset_artifacts import PRESET_ARTIFACT_BODY_CONFLICT
from domain.narrative.shaped_response import UnitKind, Units
from domain.presentation.artifacts import (
    PageArtifactReadRequest,
    ParentCapabilityIssuanceRequest,
    ParentCapabilityIssuanceResult,
    ProductSelectionCapabilityReadRequest,
    ProductSelectionCapabilityRecord,
    ProductSelectionCapabilityValidationResult,
    validate_page_artifact,
    validate_product_selection_capability,
)
from domain.presentation.compared_metrics import presentation_labels
from domain.presentation.insights import (
    InsightNextStepKind,
    InsightOutputClass,
    InsightSlot,
    InsightSlotId,
    InsightSlotState,
    next_step_from_payload,
)
from domain.presentation.next_steps import ProductRefTarget, next_step_target_payload
from domain.presentation.preset_artifacts import (
    PresetArtifactBody,
    PresetArtifactCustodyManifest,
    PresetArtifactIndexShardIdentity,
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactPointerRosterRequest,
    PresetArtifactPointerRosterResult,
    PresetArtifactProductChildIdentity,
    PresetArtifactRevision,
    PresetArtifactSiblingBody,
    PresetArtifactSiblingCustody,
    PresetArtifactSiblingReadRequest,
    PresetArtifactVariant,
    canonical_preset_artifact_json,
    index_shard_descriptors,
    split_parent_custody,
)
from domain.presentation.retail_health import (
    RETAIL_HEALTH_ROW_GRAIN,
    RETAIL_HEALTH_TOTAL_METRICS,
    RetailDistributorView,
    RetailHealthArtifact,
    RetailMetricValue,
    retail_comparison,
    retail_health_artifact_payload,
    retail_health_evidence_status,
)
from domain.presentation.retail_health_custody import (
    RetailHealthCustodyManifest,
    RetailHealthCustodyProduct,
    RetailProductChildCustody,
    product_ref_for,
    retail_health_custody_manifest_from_payload,
    retail_health_custody_manifest_payload,
    retail_product_child_custody_payload,
)
from domain.presentation.retail_product_detail import (
    RETAIL_PRODUCT_INSIGHT_PROVENANCE,
    RETAIL_PRODUCT_SERIES_GRAIN,
    RetailAdAttributed,
    RetailAdChannelRow,
    RetailAdComparison,
    RetailAdValue,
    RetailDetailReason,
    RetailProductDetailArtifact,
    RetailProductDetailOutcome,
    RetailProductIdentity,
    RetailProductInsight,
    RetailSalesPoint,
    RetailTrafficPoint,
    product_insight_stats,
    retail_ad_comparison,
    retail_product_detail_from_payload,
    retail_product_detail_payload,
)
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.identity import ProductKey
from domain.shared_kernel.errors import DomainError, InvariantError, ValidationError
from domain.shared_kernel.values import DateRange

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]
PAGE = PresetArtifactPage("retail-health")
VARIANT = PresetArtifactVariant("7d")
LABELS = presentation_labels(METRIC_CATALOG)
_RATIOS = {
    "conversion_rate_retail",
    "return_rate",
    "fulfilment_rate",
}


def _served(response: dict[str, Any]) -> dict[str, Any]:
    """Read a served JSON body as the browser reads it."""
    return cast("dict[str, Any]", _body(response))


def _product(asin: str) -> ProductKey:
    return ProductKey(f"amazon_asin:{asin.lower()}", SCOPE, MARKETPLACE)


_AD_CHANNELS = (("amazon_dsp", "Amazon DSP"), ("amazon_sponsored_products", "Sponsored Products"))


def _ad_rows() -> RetailAdAttributed:
    """Two governed channels, each its own row with its prior; nothing is totalled."""

    def side(metric: str, current: int, prior: int) -> RetailAdComparison:
        units = Units(UnitKind.CURRENCY, "USD") if metric != "purchases" else Units(UnitKind.COUNT)
        return retail_ad_comparison(
            metric,
            RetailAdValue(current, units, RETAIL_HEALTH_PERIOD),
            RetailAdValue(prior, units, RETAIL_HEALTH_PRIOR),
            SEMANTICS,
        )

    return RetailAdAttributed(
        tuple(
            RetailAdChannelRow(
                channel,
                label,
                side("purchases", 4 + index, 3),
                side("attributed_revenue_14d", 40 + 10 * index, 30),
            )
            for index, (channel, label) in enumerate(_AD_CHANNELS)
        )
    )


def _child(  # noqa: PLR0913 -- one product and its two fixture switches
    row_title: str, asin: str, *, ads: bool = False, movers: InsightSlot | None = None
) -> RetailProductDetailArtifact:
    """One fictional product's child: every tile at product grain, its series and absences.

    `ads` gives it per-channel ad rows; `movers` names it in the parent's filled movers card.
    """
    totals = tuple(
        retail_comparison(
            metric,
            RetailMetricValue(
                Decimal("0.02") if metric in _RATIOS else 20,
                _retail_units(metric),
                RETAIL_HEALTH_PERIOD,
                RETAIL_HEALTH_ROW_GRAIN,
                signed=metric == "amazon_margin_proxy",
            ),
            RetailMetricValue(
                Decimal("0.01") if metric in _RATIOS else 10,
                _retail_units(metric),
                RETAIL_HEALTH_PRIOR,
                RETAIL_HEALTH_ROW_GRAIN,
                signed=metric == "amazon_margin_proxy",
            ),
            SEMANTICS,
        )
        for metric in RETAIL_HEALTH_TOTAL_METRICS
    )

    def series(
        metric: str, point: type[RetailSalesPoint] | type[RetailTrafficPoint]
    ) -> tuple[Any, ...]:
        return tuple(
            point(
                RETAIL_HEALTH_PERIOD.start + timedelta(days=offset),
                RetailMetricValue(
                    3,
                    _retail_units(metric),
                    DateRange(
                        RETAIL_HEALTH_PERIOD.start + timedelta(days=offset),
                        RETAIL_HEALTH_PERIOD.start + timedelta(days=offset),
                    ),
                    RETAIL_PRODUCT_SERIES_GRAIN,
                ),
                RetailMetricValue(
                    2,
                    _retail_units(metric),
                    DateRange(
                        RETAIL_HEALTH_PRIOR.start + timedelta(days=offset),
                        RETAIL_HEALTH_PRIOR.start + timedelta(days=offset),
                    ),
                    RETAIL_PRODUCT_SERIES_GRAIN,
                ),
            )
            for offset in range(RETAIL_HEALTH_PERIOD.days)
        )

    revenue = next(item for item in totals if item.metric == "retail_revenue_shipped")
    insights = (
        ()
        if movers is None
        else (
            RetailProductInsight(
                movers,
                product_insight_stats(row_title, revenue, "+10.00 USD", 1, 2),
                RETAIL_PRODUCT_INSIGHT_PROVENANCE,
            ),
        )
    )
    return RetailProductDetailArtifact(
        outcome=RetailProductDetailOutcome.SELECTED,
        period=RETAIL_HEALTH_PERIOD,
        prior_period=RETAIL_HEALTH_PRIOR,
        product=RetailProductIdentity(row_title, asin, f"PARENT-{asin}"),
        distributor_view=RetailDistributorView.MANUFACTURING,
        totals=totals,
        absences=(),
        sales_daily=series("retail_revenue_shipped", RetailSalesPoint),
        traffic_daily=series("glance_views", RetailTrafficPoint),
        ad_attributed=_ad_rows()
        if ads
        else RetailAdAttributed(
            (),
            RetailDetailReason.MISSING_CURRENT_PUBLICATION,
            "Amazon Marketing Cloud hasn't sent purchases for this product for "
            "Aug 13\u201319, 2026.",
        ),
        insights=insights,
        no_insight_line=None
        if insights
        else "No Retail health insight names this product for Aug 13\u201319, 2026.",
        evidence_status=retail_health_evidence_status(
            RETAIL_HEALTH_PERIOD, tuple(item.metric for item in totals), LABELS
        ),
    )


@dataclass
class _Publication:
    """One bake: parent body, custody, one child per displayed row and the children's index.

    `child_custody` holds each child's custody and each index shard, by key.
    """

    pointer: PresetArtifactPointer
    body: PresetArtifactBody
    custody: PresetArtifactCustodyManifest
    child_bodies: dict[str, PresetArtifactSiblingBody]
    child_custody: dict[str, PresetArtifactSiblingCustody]


_MOVERS_TEXT = "Fictional Product FICTA changed by 10.00 USD."


def _filled_movers(parent: RetailHealthArtifact) -> RetailHealthArtifact:
    """Fill the baked deterministic movers card, naming the first row first."""
    movers = InsightSlot(
        InsightSlotId.RETAIL_MOVERS,
        InsightOutputClass.OBSERVED,
        InsightSlotState.FILLED,
        _MOVERS_TEXT,
        "Server-ranked absolute shipped-revenue changes.",
        "publication-1",
        "deterministic-v1",
    )
    return replace(parent, insights=(*parent.insights[:-1], movers))


def _publication(  # noqa: PLR0913 -- one bake and its three fixture switches
    identity: str = "publication-1",
    *,
    movers: bool = False,
    ads: bool = False,
    first_title: str | None = None,
) -> _Publication:
    """Bake one fictional format-8 publication.

    `movers` fills the movers card naming the first row; `ads` gives that row's child ads;
    `first_title` is the first child's catalog title, as a long Vendor Central title would be.
    """
    parent: RetailHealthArtifact = _retail_health_artifact()
    if first_title is not None:
        parent = replace(
            parent, rows=(replace(parent.rows[0], title=first_title), *parent.rows[1:])
        )
    if movers:
        parent = _filled_movers(parent)
    movers_slot = parent.insights[-1] if movers else None
    revision = PresetArtifactRevision.from_publication_identities((identity,))
    body = PresetArtifactBody(
        PAGE,
        VARIANT,
        revision,
        canonical_preset_artifact_json(retail_health_artifact_payload(parent)),
    )
    child_bodies: dict[str, PresetArtifactSiblingBody] = {}
    child_custody: dict[str, PresetArtifactSiblingCustody] = {}
    products: list[RetailHealthCustodyProduct] = []
    pairs: list[tuple[PresetArtifactSiblingBody, PresetArtifactSiblingCustody]] = []
    for index, row in enumerate(parent.rows):
        first = index == 0
        product = _product(row.child_asin)
        ref = product_ref_for(product)
        identity_ = PresetArtifactProductChildIdentity(ref)
        public = PresetArtifactSiblingBody(
            PAGE,
            VARIANT,
            revision,
            identity_,
            canonical_preset_artifact_json(
                retail_product_detail_payload(
                    _child(
                        row.title,
                        row.child_asin,
                        ads=ads and first,
                        movers=movers_slot if first else None,
                    )
                )
            ),
        )
        private = PresetArtifactSiblingCustody(
            PAGE,
            VARIANT,
            revision,
            identity_,
            canonical_preset_artifact_json(
                retail_product_child_custody_payload(
                    RetailProductChildCustody(ref, product, public.digest)
                )
            ),
        )
        child_bodies[public.key] = public
        child_custody[private.key] = private
        products.append(RetailHealthCustodyProduct(ref, product))
        pairs.append((public, private))
    # ADR-145: the manifest is the header; the stage's index is the children's one digest home.
    custody, shards = indexed_parent_custody(
        body,
        retail_health_custody_manifest_payload(
            RetailHealthCustodyManifest(
                body.digest, tuple(products), products[0].product_ref if movers else None
            )
        ),
        tuple(pairs),
    )
    child_custody.update({shard.key: shard for shard in shards})
    pointer = PresetArtifactPointer(PAGE, VARIANT, revision, body.digest, 1, None, custody.digest)
    return _Publication(pointer, body, custody, child_bodies, child_custody)


def _key(request: PresetArtifactSiblingReadRequest, root: str) -> str:
    return (
        f"{root}/{request.page.value}/{request.variant.value}/{request.revision.value}/"
        f"{request.descriptor.identity.path}.json"
    )


@dataclass
class _Bodies:
    """Pointers, parent bodies and child bodies; a read attests bytes as the S3 adapter does."""

    publication: _Publication
    attest: bool = True

    def current(self, page: PresetArtifactPage, variant: PresetArtifactVariant):
        pointer = self.publication.pointer
        return pointer if (pointer.page, pointer.variant) == (page, variant) else None

    def roster(self, request: PresetArtifactPointerRosterRequest):
        return PresetArtifactPointerRosterResult(
            request, tuple(self.current(request.page, variant) for variant in request.variants)
        )

    def read(self, pointer: PresetArtifactPointer) -> PresetArtifactBody:
        del pointer
        return self.publication.body

    def read_sibling(self, request: PresetArtifactSiblingReadRequest) -> PresetArtifactSiblingBody:
        body = self.publication.child_bodies[_key(request, "pages")]
        if self.attest and body.digest != request.descriptor.body_digest:
            raise InvariantError(PRESET_ARTIFACT_BODY_CONFLICT, "digest mismatch")
        return body


@dataclass
class _Custody:
    publication: _Publication

    def read(self, pointer: PresetArtifactPointer) -> PresetArtifactCustodyManifest:
        del pointer
        return self.publication.custody

    def read_sibling(
        self, request: PresetArtifactSiblingReadRequest
    ) -> PresetArtifactSiblingCustody:
        return self.publication.child_custody[_key(request, "custody")]


@dataclass
class _ProductPages(RecordingPages):
    """Page store that also keeps and redeems product capabilities, as DynamoDB does."""

    product_records: list[ProductSelectionCapabilityRecord] = field(
        default_factory=list[ProductSelectionCapabilityRecord]
    )
    minted: int = 0

    def mint(self) -> str:
        """One fresh opaque reference per mint across every request in a test."""
        self.minted += 1
        return self.tokens[self.minted - 1]

    def issue_parent_capabilities(
        self, request: ParentCapabilityIssuanceRequest
    ) -> ParentCapabilityIssuanceResult:
        result = super().issue_parent_capabilities(request)
        self.product_records.extend(
            record
            for record in request.capabilities
            if isinstance(record, ProductSelectionCapabilityRecord)
        )
        return result

    def read_product_capability(
        self, request: ProductSelectionCapabilityReadRequest
    ) -> ProductSelectionCapabilityValidationResult:
        record = next(
            (item for item in self.product_records if item.reference == request.reference), None
        )
        parent_record = (
            None
            if record is None
            else next(
                (
                    item
                    for item in self.artifact_records
                    if item.reference == record.parent_artifact_reference
                ),
                None,
            )
        )
        parent = (
            None
            if parent_record is None
            else validate_page_artifact(
                parent_record,
                PageArtifactReadRequest(
                    parent_record.reference,
                    request.verified_owner_id,
                    request.conversation_session_id,
                    request.at,
                ),
            ).artifact
        )
        return validate_product_selection_capability(record, request, parent)


_TOKENS = tuple(f"{letter * 43}" for letter in "ABCDEFGHIJKLMN")


def _runtime(  # noqa: PLR0913 -- the stores and one attest switch
    module: _PresentationModule,
    pages: _ProductPages,
    publication: _Publication,
    *,
    attest: bool = True,
) -> object:
    stores = _Bodies(publication, attest)
    return module.runtime_factory(
        catalog=RecordingCatalog(),
        page_context=pages,
        page_artifacts=pages,
        parent_capability_issuance=pages,
        product_capabilities=pages,
        scope=AuthorizedAnalyticalScope(frozenset({SCOPE})),
        clock=pages.clock,
        token_minter=pages.mint,
        schemas=SCHEMAS,
        metric_catalog=METRIC_CATALOG,
        preset_artifact_bodies=stores,
        preset_artifact_pointers=stores,
        preset_artifact_custody=_Custody(publication),
        dimension_labels=DIMENSION_LABELS,
        campaign_platform_labels=CAMPAIGN_PLATFORM_LABELS,
    )


def _serve_parent(
    module: _PresentationModule, pages: _ProductPages, publication: _Publication
) -> dict[str, Any]:
    response = module.handle(
        _retail_health_event({"session_id": SESSION}), _runtime(module, pages, publication)
    )
    assert response["statusCode"] == 200
    return _served(response)


def _product_event(reference: str, preset: str = "7d", **extra: object) -> dict[str, Any]:
    event = _event(
        "/api/presentation/retail-health-product",
        {
            "session_id": SESSION,
            "selection_reference": reference,
            "temporal_selection": {"kind": "preset", "preset_id": preset},
            **extra,
        },
    )
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}
    return event


# --- Custody: the closed child set ---------------------------------------------------------


def _header(publication: _Publication) -> dict[str, object]:
    header, _ = split_parent_custody(publication.custody)
    return header


@pytest.mark.covers("ADR-125", "ADR-145")
def test_the_parent_custody_names_the_rows_and_its_index_is_the_childrens_one_digest_home() -> None:
    """The manifest carries no child digests; the stage's index lists each child once."""
    publication = _publication()
    header = _header(publication)
    assert set(header) == {"kind", "body_digest", "products", "movers_product_ref"}
    manifest = retail_health_custody_manifest_from_payload(header)
    refs = tuple(item.product_ref for item in manifest.products)
    _, index = split_parent_custody(publication.custody)
    indexed = {
        item.identity: item
        for position, entry in enumerate(index)
        for item in index_shard_descriptors(
            publication.child_custody[
                f"custody/retail-health/7d/{publication.pointer.revision.value}/"
                f"{PresetArtifactIndexShardIdentity(position).path}.json"
            ],
            entry,
        )
    }
    assert set(indexed) == {PresetArtifactProductChildIdentity(ref) for ref in refs}
    for key, body in publication.child_bodies.items():
        descriptor = indexed[body.identity]
        assert descriptor.body_digest == body.digest
        private = publication.child_custody[key.replace("pages/", "custody/", 1)]
        assert descriptor.custody_digest == private.digest
    # The manifest's own codec and its refusals are held in test_retail_health_custody.py.


@pytest.mark.covers("ADR-125")
def test_a_child_body_round_trips_canonically_and_names_no_product_key() -> None:
    child = _child("Fictional Product FICTA", "FICTA")
    payload = retail_product_detail_payload(child)
    assert retail_product_detail_payload(retail_product_detail_from_payload(payload)) == payload
    totals = cast("list[dict[str, object]]", payload["totals"])
    assert tuple(item["metric"] for item in totals) == RETAIL_HEALTH_TOTAL_METRICS
    assert "amazon_asin:" not in json.dumps(payload)
    tampered = json.loads(json.dumps(payload))
    tampered["ad_attributed"]["rows"] = []
    tampered["totals"] = tampered["totals"][:-1]
    with pytest.raises(ValueError):
        retail_product_detail_from_payload(tampered)


# --- The parent mints one capability per row -----------------------------------------------


@pytest.mark.covers("ADR-125")
def test_each_row_gets_an_opaque_product_capability_bound_to_its_bake(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    publication = _publication()
    pages = _ProductPages(tokens=_TOKENS)
    payload = _serve_parent(presentation_module, pages, publication)

    rows = payload["page"]["artifact"]["rows"]
    references = [row["selection_reference"] for row in rows]
    assert len(set(references)) == len(rows) == 2
    assert [record.reference.value for record in pages.product_records] == references
    for row, record in zip(rows, pages.product_records, strict=True):
        assert record.product == _product(row["child_asin"])
        assert record.catalog_identity == "/".join(
            (
                "retail-health",
                "7d",
                publication.pointer.revision.value,
                product_ref_for(record.product),
            )
        )
    # Neither the canonical key nor the server-only reference reaches the browser.
    text = json.dumps(payload)
    assert "amazon_asin:" not in text
    assert all(product_ref_for(record.product) not in text for record in pages.product_records)


# --- The detail route ----------------------------------------------------------------------


@pytest.mark.covers("ADR-125")
def test_a_product_row_opens_its_baked_child_on_the_parents_window(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    publication = _publication()
    pages = _ProductPages(tokens=_TOKENS)
    parent = _serve_parent(presentation_module, pages, publication)
    reference = parent["page"]["artifact"]["rows"][0]["selection_reference"]

    response = presentation_module.handle(
        _product_event(reference), _runtime(presentation_module, pages, publication)
    )
    page = _served(response)["page"]

    assert response["statusCode"] == 200
    assert page["artifact_type"] == "RetailProductDetail"
    assert page["artifact_id"] == "retail_product_detail"
    assert page["parent_revision"] == parent["page"]["artifact_revision"]
    assert page["artifact"]["product"]["child_asin"] == "FICTA"
    assert page["artifact"]["period"] == {"start": "2026-08-13", "end": "2026-08-19"}
    assert page["page_context_label"] == (
        "Retail health · Fictional Product FICTA · Aug 13\u201319, 2026"
    )
    assert page["starter_questions"]
    assert all("Fictional Product FICTA" in question for question in page["starter_questions"])


@pytest.mark.covers("ADR-125")
def test_a_window_that_differs_from_the_parents_is_refused(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    publication = _publication()
    pages = _ProductPages(tokens=_TOKENS)
    parent = _serve_parent(presentation_module, pages, publication)
    reference = parent["page"]["artifact"]["rows"][0]["selection_reference"]

    for preset in ("1d", "14d"):
        with pytest.raises(ValidationError, match="Retail health window"):
            presentation_module.handle(
                _product_event(reference, preset), _runtime(presentation_module, pages, publication)
            )
    event = _product_event(reference)
    event["body"] = json.dumps(
        {
            "session_id": SESSION,
            "selection_reference": reference,
            "temporal_selection": {"kind": "custom", "start": "2026-08-13", "end": "2026-08-19"},
        }
    )
    with pytest.raises(ValidationError, match="Retail health window"):
        presentation_module.handle(event, _runtime(presentation_module, pages, publication))


@pytest.mark.covers("ADR-125")
def test_a_digest_mismatch_refuses_and_never_falls_back(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """Changed child bytes are the malformed refusal, whether the store or the handler sees it."""
    publication = _publication()
    pages = _ProductPages(tokens=_TOKENS)
    parent = _serve_parent(presentation_module, pages, publication)
    reference = parent["page"]["artifact"]["rows"][0]["selection_reference"]
    key = next(iter(publication.child_bodies))
    original = publication.child_bodies[key]
    other = retail_product_detail_payload(_child("Fictional Product FICTA", "FICTA"))
    other["no_insight_line"] = "A different, tampered line."
    publication.child_bodies[key] = PresetArtifactSiblingBody(
        original.page,
        original.variant,
        original.revision,
        original.identity,
        canonical_preset_artifact_json(other),
    )
    for attest in (True, False):
        with pytest.raises(InvariantError, match="malformed"):
            presentation_module.handle(
                _product_event(reference),
                _runtime(presentation_module, pages, publication, attest=attest),
            )
    assert pages.artifact_records[-1].artifact.artifact_type == "RetailHealthArtifact"


@pytest.mark.covers("ADR-125")
def test_a_child_missing_from_the_custody_refuses(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """Custody for the same revision that no longer names the row's child is malformed."""
    publication = _publication()
    pages = _ProductPages(tokens=_TOKENS)
    parent = _serve_parent(presentation_module, pages, publication)
    rows = parent["page"]["artifact"]["rows"]
    manifest = retail_health_custody_manifest_from_payload(_header(publication))
    # A manifest naming only the second product: the first row's product is not in it.
    kept = replace(manifest, products=manifest.products[1:])
    publication.custody, _ = indexed_parent_custody(
        publication.body,
        retail_health_custody_manifest_payload(kept),
        tuple(
            (body, publication.child_custody[key.replace("pages/", "custody/", 1)])
            for key, body in publication.child_bodies.items()
        ),
    )
    publication.pointer = replace(publication.pointer, custody_digest=publication.custody.digest)

    with pytest.raises(InvariantError, match="malformed"):
        presentation_module.handle(
            _product_event(rows[0]["selection_reference"]),
            _runtime(presentation_module, pages, publication),
        )


@pytest.mark.covers("ADR-125", "ADR-145")
def test_a_child_the_index_does_not_list_refuses_and_a_listed_one_opens(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """A served row whose child has no descriptor in the index is malformed, never a fallback."""
    publication = _publication()
    pages = _ProductPages(tokens=_TOKENS)
    rows = _serve_parent(presentation_module, pages, publication)["page"]["artifact"]["rows"]
    first, second = ((key, body) for key, body in publication.child_bodies.items())
    # The manifest still names both rows; the index lists only the second child.
    publication.custody, shards = indexed_parent_custody(
        publication.body,
        _header(publication),
        ((second[1], publication.child_custody[second[0].replace("pages/", "custody/", 1)]),),
    )
    publication.child_custody.update({shard.key: shard for shard in shards})
    publication.pointer = replace(publication.pointer, custody_digest=publication.custody.digest)
    assert first[1].identity == PresetArtifactProductChildIdentity(
        product_ref_for(_product(rows[0]["child_asin"]))
    )

    with pytest.raises(InvariantError, match="malformed"):
        presentation_module.handle(
            _product_event(rows[0]["selection_reference"]),
            _runtime(presentation_module, pages, publication),
        )
    # Paired: the row the index lists opens its child through it.
    response = presentation_module.handle(
        _product_event(rows[1]["selection_reference"]),
        _runtime(presentation_module, pages, publication),
    )
    assert response["statusCode"] == 200


@pytest.mark.covers("ADR-125", "ADR-145")
def test_a_tampered_index_shard_refuses_and_never_falls_back(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """Shard bytes that differ from the parent custody's entry are the malformed refusal."""
    publication = _publication()
    pages = _ProductPages(tokens=_TOKENS)
    rows = _serve_parent(presentation_module, pages, publication)["page"]["artifact"]["rows"]
    _, index = split_parent_custody(publication.custody)
    assert len(index) == 1
    key = (
        f"custody/retail-health/7d/{publication.pointer.revision.value}/"
        f"{PresetArtifactIndexShardIdentity(0).path}.json"
    )
    shard = publication.child_custody[key]
    payload = json.loads(shard.payload_json)
    # Swap the two children's descriptors' custody digests: well-formed, but not the bytes bound.
    first, second = payload["descriptors"]
    first["custody_digest"], second["custody_digest"] = (
        second["custody_digest"],
        first["custody_digest"],
    )
    publication.child_custody[key] = replace(
        shard, payload_json=canonical_preset_artifact_json(payload)
    )

    with pytest.raises(InvariantError, match="malformed"):
        presentation_module.handle(
            _product_event(rows[0]["selection_reference"]),
            _runtime(presentation_module, pages, publication),
        )
    # Paired: the shard as written opens the same row.
    publication.child_custody[key] = shard
    response = presentation_module.handle(
        _product_event(rows[0]["selection_reference"]),
        _runtime(presentation_module, pages, publication),
    )
    assert response["statusCode"] == 200


@pytest.mark.covers("ADR-125")
def test_a_bake_that_moved_on_asks_the_page_to_refresh(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    publication = _publication()
    pages = _ProductPages(tokens=_TOKENS)
    parent = _serve_parent(presentation_module, pages, publication)
    reference = parent["page"]["artifact"]["rows"][0]["selection_reference"]
    publication.pointer = _publication("publication-2").pointer

    with pytest.raises(DomainError, match="Refresh the parent page"):
        presentation_module.handle(
            _product_event(reference), _runtime(presentation_module, pages, publication)
        )


@pytest.mark.covers("ADR-125")
def test_a_format_8_body_with_rows_and_no_custody_refuses_and_never_falls_back(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """The bake always writes custody for rows, so a pointer without it is malformed."""
    publication = _publication()
    publication.pointer = replace(publication.pointer, custody_digest=None)
    pages = _ProductPages(tokens=_TOKENS)

    with pytest.raises(InvariantError, match="malformed"):
        presentation_module.handle(
            _retail_health_event({"session_id": SESSION}),
            _runtime(presentation_module, pages, publication),
        )
    assert pages.product_records == []
    assert pages.artifact_records == []


@pytest.mark.covers("ADR-125", "ADR-122")
def test_a_filled_movers_card_is_served_with_its_rows_open_product_detail_step(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """The step targets the capability minted for the row the movers card names first."""
    publication = _publication(movers=True)
    pages = _ProductPages(tokens=_TOKENS)
    payload = _serve_parent(presentation_module, pages, publication)

    artifact = payload["page"]["artifact"]
    by_asin = {row["child_asin"]: row["selection_reference"] for row in artifact["rows"]}
    movers = next(slot for slot in artifact["insights"] if slot["id"] == "retail-movers")
    assert movers["state"] == "filled"
    assert movers["next_step"] == {
        "kind": "open_product_detail",
        "label": f"Open {artifact['rows'][0]['title']}",
        "reason": "It changed shipped revenue more than any other product.",
        "target": {"selection_reference": by_asin["FICTA"]},
    }
    # The step's reference is one of this serve's row capabilities, never a new one.
    assert by_asin["FICTA"] in {record.reference.value for record in pages.product_records}
    # No other card carries the step, and a parent with no movers product carries none.
    assert all(slot.get("next_step") is None for slot in artifact["insights"] if slot is not movers)
    plain = _serve_parent(presentation_module, _ProductPages(tokens=_TOKENS), _publication())
    assert all(slot.get("next_step") is None for slot in plain["page"]["artifact"]["insights"])


@pytest.mark.covers("ADR-125")
def test_a_product_tile_opens_its_focus_through_the_same_capability(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    publication = _publication()
    pages = _ProductPages(tokens=_TOKENS)
    parent = _serve_parent(presentation_module, pages, publication)
    reference = parent["page"]["artifact"]["rows"][0]["selection_reference"]
    event = _event(
        "/api/presentation/retail-metric-focus",
        {
            "session_id": SESSION,
            "selection_reference": reference,
            "metric": "retail_revenue_shipped",
            "temporal_selection": {"kind": "preset", "preset_id": "7d"},
        },
    )

    response = presentation_module.handle(event, _runtime(presentation_module, pages, publication))

    assert response["statusCode"] == 200
    focus = _served(response)
    assert focus["metric"]["id"] == "retail_revenue_shipped"
    assert focus["period"] == {"start": "2026-08-13", "end": "2026-08-19"}


# --- The next step -------------------------------------------------------------------------


@pytest.mark.covers("ADR-125", "ADR-122")
def test_open_product_detail_is_a_served_step_never_a_cached_one() -> None:
    target = ProductRefTarget("v2_" + "A" * 40)
    assert next_step_target_payload(target, cached=False) == {
        "selection_reference": target.selection_reference
    }
    with pytest.raises(ValueError, match="never cached"):
        next_step_target_payload(target, cached=True)
    step = next_step_from_payload(
        {
            "kind": "open_product_detail",
            "label": "Open Fictional Product",
            "reason": "It changed shipped revenue more than any other product.",
            "target": {"selection_reference": target.selection_reference},
        }
    )
    assert step.kind is InsightNextStepKind.OPEN_PRODUCT_DETAIL
    with pytest.raises(ValueError, match="never cached"):
        next_step_from_payload(
            {
                "kind": "open_product_detail",
                "label": "Open",
                "reason": "Reason.",
                "target": {"selection_reference": target.selection_reference},
            },
            cached=True,
        )


# --- The browser decodes the server's own responses ----------------------------------------

BROWSER_FIXTURES = ROOT / "frontend/test/fixtures"
"""The served parent and its first row's child, as this handler writes them (fictional)."""


def browser_fixture_responses(module: _PresentationModule) -> dict[str, dict[str, Any]]:
    """Serve a format-8 parent with a filled movers card, then open its first row's child."""
    publication = _publication(movers=True, ads=True)
    pages = _ProductPages(tokens=_TOKENS)
    parent = _serve_parent(module, pages, publication)
    reference = parent["page"]["artifact"]["rows"][0]["selection_reference"]
    child = module.handle(_product_event(reference), _runtime(module, pages, publication))
    assert child["statusCode"] == 200
    return {"retail-health-v2.json": parent, "retail-product-v2.json": _served(child)}


@pytest.mark.covers("ADR-125")
def test_the_browser_fixtures_are_the_servers_own_parent_and_child_responses(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """`frontend/test` decodes these bytes, so a server change that the browser can't read fails."""
    for name, served in browser_fixture_responses(presentation_module).items():
        committed = json.loads((BROWSER_FIXTURES / name).read_text())
        assert committed == served, f"regenerate frontend/test/fixtures/{name}"


# --- Starter questions on product detail ---------------------------------------------------


@pytest.mark.covers("ADR-125", "ADR-120")
def test_a_product_title_too_long_for_a_starter_is_named_by_its_asin_and_never_cut(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """Name the product by its title while the question fits, then by its ASIN, never cut.

    At this 7-day window the shipped-revenue question fits a 65-character title and the
    glance-views one 68. A title one longer is replaced by the row's ASIN, whole.
    """
    shipped = "How did shipped revenue for {} change in Aug 13\u201319, 2026?"
    glance = "How did glance views for {} change in Aug 13\u201319, 2026?"
    # Each question at 120 characters with the title, then at 121 where the ASIN takes over.
    cases = (
        (65, ("title", "title")),
        (66, ("FICTA", "title")),
        (68, ("FICTA", "title")),
        (69, ("FICTA", "FICTA")),
    )
    for size, names in cases:
        title = "T" * size
        publication = _publication(first_title=title)
        pages = _ProductPages(tokens=_TOKENS)
        parent = _serve_parent(presentation_module, pages, publication)
        reference = parent["page"]["artifact"]["rows"][0]["selection_reference"]

        page = _served(
            presentation_module.handle(
                _product_event(reference), _runtime(presentation_module, pages, publication)
            )
        )["page"]

        expected = [
            template.format(title if name == "title" else name)
            for template, name in zip((shipped, glance), names, strict=True)
        ]
        assert page["starter_questions"] == expected
        assert all(len(question) <= 120 for question in expected)
        at_bound = {65: 0, 68: 1}.get(size)
        if at_bound is not None:
            assert len(expected[at_bound]) == 120
        # A title one past a question's bound would write 121: it names the ASIN instead.
        for template, name in zip((shipped, glance), names, strict=True):
            assert (name == "FICTA") == (len(template.format(title)) > 120)
        # The page still names the product by its full title everywhere else.
        assert title in page["page_context_label"]


# --- No references to the retired surfaces remain ------------------------------------------

_RETIRED = (
    re.compile(r"RETAIL_PRODUCT_PATH"),
    re.compile(r"presentation/retail-product(?![\w-])"),
    re.compile(r"retail-product-detail"),
    re.compile(r"retail-detail"),
    re.compile(r"domain\.presentation\.retail_product(?!_detail)\b"),
    re.compile(r"RetailProductArtifact"),
    re.compile(r"RETAIL_PRODUCT_ARTIFACT"),
    re.compile(r"retail_product_artifact"),
)
_SCANNED = (
    "src",
    "presentation",
    "contracts",
    "infra/lib",
    "frontend/lib",
    "frontend/components",
    "frontend/app",
    "frontend/generated",
)


@pytest.mark.covers("ADR-125", "ADR-117")
def test_no_reference_to_the_retired_product_evidence_surfaces_remains() -> None:
    """Retired with this slice: the route, handler path, page entries, bindings and module."""
    assert not (ROOT / "src/domain/presentation/retail_product.py").exists()
    found: list[str] = []
    for top in _SCANNED:
        for path in (ROOT / top).rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".ts", ".tsx", ".yaml", ".json"}:
                continue
            if "node_modules" in path.parts or "__pycache__" in path.parts:
                continue
            text = path.read_text(errors="ignore")
            found.extend(
                f"{path.relative_to(ROOT)}: {pattern.pattern}"
                for pattern in _RETIRED
                if pattern.search(text)
            )
    assert found == []
