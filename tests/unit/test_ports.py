# SPDX-License-Identifier: MIT-0
"""Tests for port contracts.

Ports are interfaces, so what is testable is the value objects they carry and the
structural guarantees the signatures make.
"""

import pathlib
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from publication_support import bound_plan

from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.metric_dictionary import load_catalog
from domain.measurement.planning import FactShape, PlanRequest, plan
from domain.measurement.publications import BoundQueryPlan
from domain.shared_kernel.provenance import Suppression
from domain.shared_kernel.values import DateRange
from ports.outbound.canonical import CanonicalDatasetWritePort, CanonicalMaterializeRequest
from ports.outbound.identity_provider import ExchangeCodeRequest, IdentityProviderPort
from ports.outbound.publication_selection import PublicationSelectionPort
from ports.outbound.query import (
    BAKE_LONGEST_WINDOW_DAYS,
    BAKE_MAX_ROWS,
    BAKE_MAX_STREAMED_ROWS,
    MAX_ROWS_CEILING,
    PeriodSeriesRequest,
    QueryPort,
    QueryRequest,
    QueryResult,
    RowCeiling,
    row_ceiling,
)
from ports.outbound.source_aligned import SourceAlignedReadPort, SourceAlignedWritePort
from ports.outbound.source_coverage import (
    SourceCoveragePort,
    SourceCoverageRequest,
    SourceCoverageRequirement,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-006")]

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


@pytest.fixture(scope="module")
def revenue_plan() -> BoundQueryPlan:
    """Plan and bind the real target question to immutable successful publications."""
    contracts = {c.contract_id: c for _, c in load_contracts(REPO_ROOT)}
    sales = contracts["vendor-sales-by-asin"]
    logical = plan(
        load_catalog(REPO_ROOT),
        PlanRequest(
            metric_name="retail_revenue_shipped",
            grains=("product",),
            window=DateRange(date(2026, 8, 10), date(2026, 8, 16)),
            filters={
                "marketplace_key": "ATVPDKIKX0DER",
                "distributor_view": "MANUFACTURING",
                "selling_program": "RETAIL",
            },
        ),
        FactShape.of(sales),
    )
    return bound_plan(logical, published_at=NOW)


class TestQueryPortShape:
    def test_the_port_accepts_no_sql(self) -> None:
        """The structural guarantee behind "never execute model-authored SQL".

        If a `sql` field ever appears on QueryRequest, this fails — which is the
        alarm we want, because it converts a compile-time guarantee back into a
        rule someone has to remember.
        """
        fields = QueryRequest.__dataclass_fields__.keys()
        assert set(fields) == {"plan", "max_rows", "ceiling", "ranked"}
        assert not any("sql" in f.lower() for f in fields)

    def test_what_crosses_is_a_validated_plan(self, revenue_plan: BoundQueryPlan) -> None:
        """Stronger than a template name, which is what this used to carry.

        A plan has already had its fact and measure resolved from the dictionary, its
        group-by drawn from a closed map, and every filter key checked against the fact's
        real column list. That last one is the security-relevant part: a filter key is an
        identifier, SQL cannot bind an identifier, so an adapter must interpolate it.
        """
        request = QueryRequest(plan=revenue_plan, max_rows=100)

        logical = request.plan.logical
        assert logical.fact == "fct_retail_sales"
        assert logical.measure_column == "revenue_shipped"
        assert logical.group_by == ("amazon_asin",)

    def test_max_rows_must_be_positive(self, revenue_plan: BoundQueryPlan) -> None:
        with pytest.raises(ValueError, match="max_rows"):
            QueryRequest(plan=revenue_plan, max_rows=0)

    def test_max_rows_is_capped(self, revenue_plan: BoundQueryPlan) -> None:
        """A result too large to narrate gets truncated, and truncation must be visible.

        Capping the request keeps that decision in the open rather than letting a caller ask
        for a million rows and quietly receive a page.
        """
        with pytest.raises(ValueError, match="cannot be narrated honestly"):
            QueryRequest(plan=revenue_plan, max_rows=MAX_ROWS_CEILING + 1)

    def test_protocol_is_satisfied_structurally(self) -> None:
        # No inheritance required — an adapter satisfies the port by shape.
        class Fake:
            def run(self, request: object) -> object:  # noqa: ARG002
                return None

        assert isinstance(Fake(), QueryPort)


class TestRowCeilingPerCallerKind:
    """ADR-143: one ceiling per caller kind; chat's narration bound is unchanged."""

    def test_the_default_kind_is_narration_with_chats_unchanged_bound(
        self, revenue_plan: BoundQueryPlan
    ) -> None:
        request = QueryRequest(plan=revenue_plan, max_rows=MAX_ROWS_CEILING)
        assert request.ceiling is RowCeiling.NARRATION
        assert (MAX_ROWS_CEILING, row_ceiling(RowCeiling.NARRATION)) == (10_000, 10_000)

    @pytest.mark.parametrize(
        ("kind", "bound"),
        [
            (RowCeiling.NARRATION, MAX_ROWS_CEILING),
            (RowCeiling.BAKE, BAKE_MAX_ROWS),
            (RowCeiling.BAKE_STREAMED, BAKE_MAX_STREAMED_ROWS),
        ],
    )
    def test_each_kind_admits_its_bound_and_refuses_one_more(
        self, revenue_plan: BoundQueryPlan, kind: RowCeiling, bound: int
    ) -> None:
        """Paired: exactly the bound constructs; one row more is the port's ValueError."""
        assert row_ceiling(kind) == bound
        assert QueryRequest(plan=revenue_plan, max_rows=bound, ceiling=kind).max_rows == bound
        with pytest.raises(ValueError, match="max_rows"):
            QueryRequest(plan=revenue_plan, max_rows=bound + 1, ceiling=kind)
        with pytest.raises(ValueError, match="max_rows"):
            PeriodSeriesRequest((revenue_plan,), bound + 1, kind)

    def test_a_bake_width_is_refused_under_narration(self, revenue_plan: BoundQueryPlan) -> None:
        with pytest.raises(ValueError, match="cannot be narrated honestly"):
            QueryRequest(plan=revenue_plan, max_rows=BAKE_MAX_ROWS)

    def test_the_streamed_ceiling_is_derived_from_the_held_one(self) -> None:
        assert BAKE_MAX_STREAMED_ROWS == BAKE_LONGEST_WINDOW_DAYS * BAKE_MAX_ROWS
        assert MAX_ROWS_CEILING < BAKE_MAX_ROWS <= 50_000  # finite, never above the proposal


class TestQueryResultCarriesProvenance:
    def test_suppression_has_no_default(self) -> None:
        """A default would read downstream as "nothing was withheld".

        That is a claim, not an absence, and it is the one claim the platform must
        never make by accident — so an adapter is required to state it.
        """
        with pytest.raises(TypeError, match="suppression"):
            QueryResult(rows=(), bytes_scanned=0, truncated=False)  # type: ignore[call-arg]

    def test_truncation_and_suppression_are_separate_facts(self) -> None:
        """Both shorten a result, and they mean entirely different things.

        Truncation is our max_rows cutting the tail off; suppression is Amazon
        withholding rows for privacy. Collapsing them would report a privacy
        withholding as a paging artefact.
        """
        result = QueryResult(
            rows=({"spend": Decimal("100.00")},),
            bytes_scanned=1024,
            truncated=True,
            suppression=Suppression(
                rows_suppressed=True,
                reason="97 'sponsored_ads_traffic' users < 100 required",
            ),
        )
        assert result.truncated is True
        assert result.suppression.rows_suppressed is True
        assert result.suppression.reason is not None

    def test_an_unsuppressed_result_still_states_so(self) -> None:
        result = QueryResult(
            rows=(),
            bytes_scanned=0,
            truncated=False,
            suppression=Suppression(rows_suppressed=False),
        )
        assert result.suppression.rows_suppressed is False


class TestIdentityProviderPortShape:
    def test_the_port_cannot_authenticate_a_password(self) -> None:
        """The structural guarantee behind keeping federated SSO possible.

        `InitiateAuth` cannot sign in a federated user, so a port that accepted a
        username and password would quietly foreclose corporate SSO — see ADR-007
        (docs/decisions/adr-007-auth-cookie-bff.md). If such a method appears here, this fails.
        """
        methods = {name for name in dir(IdentityProviderPort) if not name.startswith("_")}
        assert methods == {
            "exchange_code",
            "refresh",
            "revoke",
            "signing_keys",
            "refresh_signing_keys",
        }
        assert not any(
            word in name for name in methods for word in ("password", "login", "authenticate")
        )

    def test_a_code_exchange_requires_pkce(self) -> None:
        # An intercepted authorization code is only survivable when the code
        # alone is insufficient, which is exactly what the verifier provides.
        with pytest.raises(ValueError, match="code_verifier"):
            ExchangeCodeRequest(
                code="c", redirect_uri="https://app.customer.com/", code_verifier=""
            )

    def test_a_code_is_required(self) -> None:
        with pytest.raises(ValueError, match="code is required"):
            ExchangeCodeRequest(
                code="", redirect_uri="https://app.customer.com/", code_verifier="v"
            )

    def test_a_redirect_uri_is_required(self) -> None:
        with pytest.raises(ValueError, match="redirect_uri"):
            ExchangeCodeRequest(code="c", redirect_uri="", code_verifier="v")

    def test_protocol_is_satisfied_structurally(self) -> None:
        class Fake:
            def exchange_code(self, request: object) -> object:  # noqa: ARG002
                return None

            def refresh(self, refresh_token: str) -> object:  # noqa: ARG002
                return None

            def revoke(self, refresh_token: str) -> None: ...

            def signing_keys(self) -> object:
                return None

            def refresh_signing_keys(self) -> object:
                return None

        assert isinstance(Fake(), IdentityProviderPort)


class TestSemanticV2PublicationPorts:
    @pytest.mark.covers("ADR-044")
    def test_canonical_materialization_carries_rows_and_evidence_but_no_sql(self) -> None:
        fields = CanonicalMaterializeRequest.__dataclass_fields__.keys()
        assert set(fields) == {"destination", "publication", "rows", "rule_set_version"}
        assert not any("sql" in field.lower() for field in fields)

    @pytest.mark.covers("ADR-044")
    def test_source_read_write_and_canonical_write_are_separate_authorities(self) -> None:
        class SourceReader:
            def publication(self, reference: object) -> None:  # noqa: ARG002
                return None

        class SourceWriter:
            def materialize(self, publication: object) -> None:  # noqa: ARG002
                return None

        class CanonicalWriter:
            def materialize(self, request: object) -> None:  # noqa: ARG002
                return None

            def attest(self, request: object) -> None:  # noqa: ARG002
                return None

        assert isinstance(SourceReader(), SourceAlignedReadPort)
        assert isinstance(SourceWriter(), SourceAlignedWritePort)
        assert isinstance(CanonicalWriter(), CanonicalDatasetWritePort)
        assert not isinstance(SourceReader(), SourceAlignedWritePort)

    @pytest.mark.covers("ADR-047")
    def test_publication_selection_is_a_read_only_structural_port(self) -> None:
        class Selector:
            def select(self, request: object) -> object:  # noqa: ARG002
                return None

        assert isinstance(Selector(), PublicationSelectionPort)


class TestSourceCoveragePort:
    def test_request_rejects_duplicate_requirements(self) -> None:
        """Repeated requirements could obscure a caller's declared contributor set."""
        requirement = SourceCoverageRequirement((("source", "dataset"),))

        with pytest.raises(ValueError, match="cannot repeat"):
            SourceCoverageRequest((requirement, requirement))

    def test_protocol_is_satisfied_structurally(self) -> None:
        class Reader:
            def source_coverage(self, request: SourceCoverageRequest) -> object:  # noqa: ARG002
                return None

        assert isinstance(Reader(), SourceCoveragePort)
