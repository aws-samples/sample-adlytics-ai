# SPDX-License-Identifier: MIT-0
"""Decide what one landed delivery writes, once its objects have been read and mapped.

The step between mapping and merging, and it exists because that step stopped being trivial the
moment one delivery could feed two contracts. Three things have to happen in the right order and
none of them is I/O:

1. Every contract's own assertions run against its own rows.
2. The assertions that are *about two contracts* run once for the delivery.
3. The results are attributed back to partitions — and a delivery-level failure lands on **every**
   partition, not on whichever grain looks suspect.

**Step 3 is the whole reason this module is not a loop in an adapter.** A control-total failure
is symmetric evidence: either a detail row is wrong or the reported aggregate is, and the check
cannot tell which. Quarantining the half that looks broken encodes a guess. Quarantining both
fails towards an absence, which is visible and self-healing, rather than towards a plausible
number, which is neither. ADR-031 is the argument; this is where it is enforced.

Pure. Every input arrives as a field on the request — the parsed document, the mapped rows, the
dimension keys already known — the same shape `WatermarkRequest.previous` and
`DimensionResolutionRequest.known_keys` use, and for the same reason: every outcome is reachable
in a test with no bucket, no table and no clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from domain.curation.assertions import (
    AssertionName,
    ControlTotalRequest,
    CurrencyAgreementRequest,
    HeaderRequest,
    LoadOutcome,
    NonAdditiveSuppressionRequest,
    PackVersionColumnRequest,
    Violation,
    check_control_total,
    check_currency_agreement,
    check_header,
    check_non_additive_suppression,
    check_pack_version_columns,
    control_total_measures,
    detail_and_total,
)
from domain.curation.mapping import MappingError, options_hash, suppression_flag
from domain.data_contracts.schema import (
    CONFORMED_PROVENANCE_COLUMNS,
    conformed_provenance_values,
)
from domain.measurement.periodic import (
    AS_OF_MONTH_COLUMN,
    BUCKET_SCHEME_COLUMN,
    COHORT_GRAIN_COLUMN,
    COHORT_PATH_COLUMN,
    ROW_KIND_COLUMN,
)
from domain.producer.variants import (
    BASE_PACK,
    PACK_VARIANT_MISMATCH,
    DeclaredVariant,
    PreviousPeriodEvidence,
    VariantRequest,
    select_pack_variant,
)
from domain.semantics.publication import PACK_VARIANT_OPTION
from domain.shared_kernel.provenance import withheld_money_counts
from domain.shopper_aggregates.checks import (
    REPEATED_TOTAL_MISMATCH,
    BarRequest,
    BarVerdict,
    RepeatedTotal,
    RepeatedTotalRequest,
    check_bar,
    check_repeated_totals,
)

CURRENCY_DISAGREEMENT = "CURRENCY_DISAGREEMENT"
"""More than one currency reached one delivered row (design §5.5)."""
MARKETPLACE_FILTER_SUSPECTED = "MARKETPLACE_FILTER_SUSPECTED"
"""Every reached `account` row had no purchaser (design §5.5, review DS4-7)."""

if TYPE_CHECKING:  # pragma: no cover — type-only
    from collections.abc import Mapping, Sequence
    from datetime import date

    from domain.curation.landing import Manifest
    from domain.curation.mapping import SourceDocument
    from domain.curation.watermarks import PartitionKey
    from domain.data_contracts.contract import DataContract
    from domain.data_contracts.validation import ColumnRegistry

__all__ = [
    "OPTIONS_HASH_COLUMN",
    "AssessmentRequest",
    "ContractAssessment",
    "ContractLoad",
    "DeliveryAssessment",
    "DiscardFoldRequest",
    "DiscardPolicy",
    "DiscardSplit",
    "IngestionError",
    "SuppressionSplit",
    "WithheldResidual",
    "assess_source_delivery",
    "fold_discarded",
    "split_discarded",
    "split_withheld",
]


class IngestionError(Exception):
    """A delivery cannot be assessed or written as described.

    Every sibling module in `domain/curation` carries its own class, so a caller can tell a
    mapping failure from a watermark one without reading the message.
    """


OPTIONS_HASH_COLUMN = "report_options_hash"
"""The curated column carrying which option set produced a row.

Named here because this module compares it against the manifest's own `option_set_hash`, and
the two are computed from different places: the column is mapped out of the delivered document,
the manifest field is declared by the producer. Agreement between them is the only evidence
that a manifest describes the document it was written beside.
"""


@dataclass(frozen=True, slots=True)
class ContractLoad:
    """One contract's share of a delivery: its rows, and the partition they belong to."""

    contract: DataContract
    partition: PartitionKey
    rows: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class DiscardPolicy:
    """A governed pack that drops its suppressed rows before the fact write (ADR-099 point 7).

    Built by the adapter from the installed query packs declaring `discard_suppressed_rows`. The
    load applies it only to the delivery whose manifest names that exact pack, so every other
    pack, `reject_delivery` included, keeps today's checks.
    """

    query_pack_id: str
    contract_id: str
    expected_bar: int
    variants: tuple[DeclaredVariant, ...] = ()
    """The pack's declared variants (plan D9), so the load knows which pack versions are which."""


@dataclass(frozen=True, slots=True)
class AssessmentRequest:
    """Everything needed to assess landed source evidence, with no I/O left to do."""

    manifest: Manifest
    document: SourceDocument
    loads: tuple[ContractLoad, ...]
    registry: ColumnRegistry
    discard_policies: tuple[DiscardPolicy, ...] = ()
    """Installed `discard_suppressed_rows` packs; empty means no pack discards."""
    variant_evidence: Mapping[str, PreviousPeriodEvidence] = field(
        default_factory=dict[str, PreviousPeriodEvidence]
    )
    """The previous period's published evidence per contract, for a pack declaring an automatic
    variant (plan D10). Read by the adapter from the publication state the load already reads;
    absent means no previous period, so no automatic variant is due."""


@dataclass(frozen=True, slots=True)
class ContractAssessment:
    """What one contract's partition does, and why."""

    load: ContractLoad
    outcome: LoadOutcome

    @property
    def contract_id(self) -> str:
        """Name the contract this assessment is about."""
        return self.load.contract.contract_id

    @property
    def quarantines(self) -> bool:
        """Whether this partition must be quarantined rather than written."""
        return self.outcome.quarantines

    @property
    def writes(self) -> bool:
        """Whether this partition has rows to write and nothing preventing it."""
        return not self.outcome.quarantines and bool(self.load.rows)


@dataclass(frozen=True, slots=True)
class DeliveryAssessment:
    """What a whole delivery does, per partition."""

    manifest: Manifest
    assessments: tuple[ContractAssessment, ...]

    @property
    def quarantines(self) -> bool:
        """Whether any partition in this delivery must be quarantined."""
        return any(item.quarantines for item in self.assessments)

    @property
    def violations(self) -> tuple[Violation, ...]:
        """Give every distinct violation across the delivery, in the order found.

        Deduplicated, because a delivery-level failure is attributed to every partition and
        reporting it once per partition would make one defect in one document look like several.
        """
        seen: list[Violation] = []
        for item in self.assessments:
            for violation in item.outcome.violations:
                if violation not in seen:
                    seen.append(violation)
        return tuple(seen)


def _manifest_agreement(request: AssessmentRequest, load: ContractLoad) -> LoadOutcome:
    """Check that the document agrees with the manifest about which pull this is.

    Only meaningful for a contract that maps the option hash as a column; a Data Kiosk delivery
    has no report options at all, so there is nothing to disagree about and an empty outcome is
    the honest answer rather than a skipped check.
    """
    found = {
        str(row[OPTIONS_HASH_COLUMN])
        for row in load.rows
        if row.get(OPTIONS_HASH_COLUMN) is not None
    }
    disagreeing = sorted(found - {request.manifest.option_set_hash})
    if not disagreeing:
        return LoadOutcome()

    return LoadOutcome(
        violations=(
            Violation(
                assertion=AssertionName.MANIFEST_AGREEMENT,
                detail=(
                    f"{load.contract.contract_id}: the manifest declares option set "
                    f"{request.manifest.option_set_hash} but the document's rows carry "
                    f"{', '.join(disagreeing)}. The manifest describes a different pull than "
                    "the object it was written beside, so which lens these units were measured "
                    "under cannot be decided — and two lenses merged double-count every unit."
                ),
                context=(
                    ("contract_id", load.contract.contract_id),
                    ("manifest_option_set_hash", request.manifest.option_set_hash),
                    ("document_option_set_hashes", ",".join(disagreeing)),
                    ("expected_from_option_set", options_hash(dict(request.manifest.option_set))),
                ),
            ),
        )
    )


def discard_policy_for(request: AssessmentRequest, contract: DataContract) -> DiscardPolicy | None:
    """Return the discard policy of the pack that produced this delivery, if it declares one."""
    governance = request.manifest.governance
    if governance is None:
        return None
    matches = tuple(
        policy
        for policy in request.discard_policies
        if policy.query_pack_id == governance.query_pack_id
        and policy.contract_id == contract.contract_id
    )
    return matches[0] if matches else None


def _violation(
    assertion: AssertionName, contract: DataContract, detail: str, **context: str
) -> LoadOutcome:
    return LoadOutcome(
        violations=(
            Violation(
                assertion=assertion,
                detail=f"{contract.contract_id}: {detail}",
                context=(("contract_id", contract.contract_id), *sorted(context.items())),
            ),
        )
    )


def _shopper_aggregate_assertions(
    request: AssessmentRequest, load: ContractLoad, policy: DiscardPolicy | None
) -> LoadOutcome:
    """Run the load checks a closed-enumeration contract's discard policy replaces.

    A `discard_suppressed_rows` delivery skips `check_non_additive_suppression`, because its
    withheld rows are dropped before the fact write rather than refusing it, and runs the bar
    check instead: every unsuppressed row reads `distinct_user_count` at most the pack's
    `expectedBar` (design §4.4). Every other contract keeps the non-additive refusal unchanged.
    """
    contract = load.contract
    if policy is None:
        if contract.records_cell_state:
            return _violation(
                AssertionName.CLOSED_ENUMERATION,
                contract,
                "records cell_state, so its delivery must come from a governed pack that "
                "declares discard_suppressed_rows; this one doesn't, so its withheld rows "
                "can't be discarded or its missing cells written not_returned.",
            )
        return check_non_additive_suppression(
            NonAdditiveSuppressionRequest(contract=contract, rows=load.rows)
        )
    if not contract.records_cell_state and not contract.discards_into_history:
        return _violation(
            AssertionName.CLOSED_ENUMERATION,
            contract,
            "its pack declares discard_suppressed_rows, but the contract records no "
            "cell_state, so a missing cell would read as nothing rather than not_returned.",
        )
    # An SCD2 dimension (packs 9 and 13) has no cells: its withheld rows are dropped and the
    # history keeps each omitted key's earlier version, so only the bar check applies to it.
    outcome = _variant_check(request, contract, policy)
    outcome = outcome.combined(_currency_minimum_check(request, contract))
    outcome = outcome.combined(_marketplace_filter_check(request, contract))
    outcome = outcome.combined(_repeated_total_check(request, contract))
    bar = check_bar(BarRequest(rows=request.document.rows, expected_bar=policy.expected_bar))
    if bar.verdict is BarVerdict.ACCEPTED or bar.finding is None:
        return outcome
    return outcome.combined(
        _violation(
            AssertionName.PRIVACY_BAR,
            contract,
            f"{bar.finding.code}: {bar.finding.detail}",
            bar_verdict=bar.verdict.value,
            finding_code=bar.finding.code,
            highest_user_count="unreadable" if bar.highest is None else str(bar.highest),
            expected_bar=str(policy.expected_bar),
        )
    )


def _variant_check(
    request: AssessmentRequest, contract: DataContract, policy: DiscardPolicy
) -> LoadOutcome:
    """Refuse a delivery of any pack variant but the one this period expects (plan D10)."""
    governance = request.manifest.governance
    named = request.manifest.option_set.get(PACK_VARIANT_OPTION)
    requested = None if named is None else str(named)
    if not policy.variants and requested is None:
        return LoadOutcome()
    version = None if governance is None else governance.query_pack_version
    delivered = next(
        (item.variant for item in policy.variants if item.pack_version == version), None
    )
    selection = select_pack_variant(
        VariantRequest(
            declared=policy.variants,
            requested=requested,
            delivered=delivered,
            previous=request.variant_evidence.get(contract.contract_id),
        )
    )
    if selection.accepted:
        return LoadOutcome()
    expected = BASE_PACK if selection.expected is None else selection.expected
    return _violation(
        AssertionName.PACK_VARIANT,
        contract,
        f"{PACK_VARIANT_MISMATCH}: {selection.detail}",
        finding_code=PACK_VARIANT_MISMATCH,
        expected_variant=expected,
        delivered_variant=BASE_PACK if delivered is None else delivered,
    )


def _blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _currency_minimum_check(request: AssessmentRequest, contract: DataContract) -> LoadOutcome:
    """Refuse a delivery where a currency's MAX and MIN differ on any row (design §5.5).

    Read off every delivered row, withheld ones included: two currencies behind one row mean its
    amounts can't be added, whether or not the row survives. A row where either is NULL carries
    no currency to disagree about (a group with no DSP touch has no DSP currency).
    """
    disagreements: list[str] = []
    for index, row in enumerate(request.document.rows):
        for column, minimum in contract.currency_minimums:
            high, low = row.get(column), row.get(minimum)
            if _blank(high) or _blank(low):
                continue
            if str(high).strip() != str(low).strip():
                disagreements.append(f"row {index}: {column} {high!r} != {minimum} {low!r}")
    if not disagreements:
        return LoadOutcome()
    return _violation(
        AssertionName.CURRENCY_AGREEMENT,
        contract,
        f"{CURRENCY_DISAGREEMENT}: more than one currency reached a row, so its amounts can't be "
        f"added: {'; '.join(disagreements)}.",
        finding_code=CURRENCY_DISAGREEMENT,
    )


def _count(value: object) -> Decimal | None:
    if _blank(value):
        return None
    try:
        return Decimal(str(value).strip())
    except InvalidOperation:
        return None


def _marketplace_filter_check(request: AssessmentRequest, contract: DataContract) -> LoadOutcome:
    """Refuse a delivery whose reached rows on the declared slice all have no outcome (DS4-7).

    Over the unsuppressed rows on the slice (`account` IDs), when some row reached shoppers and
    every row carrying the outcome reads 0. A touch-only variant splits reach and outcome onto
    separate rows, so each is read where the row carries it.
    """
    check = contract.marketplace_filter_check
    suppression = contract.suppression
    if check is None or suppression is None:
        return LoadOutcome()

    def delivered(column: str) -> str:
        mapping = contract.field_for(column)
        return column if mapping is None else mapping.delivered_name

    slice_name, reached, outcome = (
        delivered(check.slice_column),
        delivered(check.reached),
        delivered(check.outcome),
    )
    reached_any = False
    outcomes: list[Decimal] = []
    for row in request.document.rows:
        try:
            if suppression_flag(row.get(suppression.discriminator_column)):
                continue
        except MappingError:
            continue
        if str(row.get(slice_name)) != check.slice_value:
            continue
        shoppers = _count(row.get(reached))
        reached_any = reached_any or (shoppers is not None and shoppers > 0)
        purchasers = _count(row.get(outcome))
        if purchasers is not None:
            outcomes.append(purchasers)
    if not reached_any or not outcomes or any(value > 0 for value in outcomes):
        return LoadOutcome()
    return _violation(
        AssertionName.MARKETPLACE_FILTER,
        contract,
        f"{MARKETPLACE_FILTER_SUSPECTED}: every returned {check.slice_value} row reached shoppers "
        f"with no {check.outcome}: possible marketplace-filter mismatch. A heuristic: a real zero "
        "period is refused too, and reads not available.",
        finding_code=MARKETPLACE_FILTER_SUSPECTED,
    )


def _repeated_total_check(request: AssessmentRequest, contract: DataContract) -> LoadOutcome:
    """Refuse a delivery whose repeated totals differ between copies (design §7a).

    Read over the unsuppressed delivered rows: a withheld row's counts aren't copies. Pack 14
    repeats each cohort's size on every follow-up month row, so two different values for one
    cohort and group mean the pack counted two populations.
    """
    suppression = contract.suppression
    if not contract.repeated_totals or suppression is None:
        return LoadOutcome()
    rows: list[dict[str, object]] = []
    for row in request.document.rows:
        try:
            if suppression_flag(row.get(suppression.discriminator_column)):
                continue
        except MappingError:
            continue
        rows.append(row)
    findings = check_repeated_totals(
        RepeatedTotalRequest(
            rows=tuple(rows),
            totals=tuple(
                RepeatedTotal(column, keyed_by) for column, keyed_by in contract.repeated_totals
            ),
        )
    )
    if not findings:
        return LoadOutcome()
    return _violation(
        AssertionName.REPEATED_TOTAL,
        contract,
        f"{REPEATED_TOTAL_MISMATCH}: " + "; ".join(finding.detail for finding in findings),
        finding_code=REPEATED_TOTAL_MISMATCH,
    )


def _source_assertions(request: AssessmentRequest, load: ContractLoad) -> LoadOutcome:
    """Run checks decidable from the landed bytes, manifest, contracts, and registry alone."""
    outcome = check_header(
        HeaderRequest(
            contract=load.contract,
            delivered_columns=frozenset(request.manifest.declared_columns),
        )
    )
    outcome = outcome.combined(
        _shopper_aggregate_assertions(request, load, discard_policy_for(request, load.contract))
    )
    outcome = outcome.combined(
        check_currency_agreement(
            CurrencyAgreementRequest(
                contract=load.contract, source=request.document, rows=load.rows
            )
        )
    )
    governance = request.manifest.governance
    outcome = outcome.combined(
        check_pack_version_columns(
            PackVersionColumnRequest(
                contract=load.contract,
                query_pack_version=None if governance is None else governance.query_pack_version,
                rows=load.rows,
            )
        )
    )
    return outcome.combined(_manifest_agreement(request, load))


def _control_totals(request: AssessmentRequest) -> LoadOutcome:
    """Run the assertion that is about two contracts, once for the delivery.

    Returns one outcome for the whole delivery rather than one per contract, because that is
    what the failure means: the two grains disagree, and neither is identifiable as the wrong
    one. The caller attributes it to every partition.
    """
    by_id = {load.contract.contract_id: load for load in request.loads}
    outcome = LoadOutcome()

    for detail, total in detail_and_total([load.contract for load in request.loads]):
        detail_load = by_id.get(detail.contract_id)
        total_load = by_id.get(total.contract_id)
        if detail_load is None or total_load is None:  # pragma: no cover — both come from loads
            continue

        # The intersection, because a measure only one contract carries cannot be compared.
        # `orderedRevenue` is absent under a SOURCING pull on one side and not the other.
        measures = control_total_measures(detail, request.registry) & control_total_measures(
            total, request.registry
        )
        outcome = outcome.combined(
            check_control_total(
                ControlTotalRequest(
                    detail_rows=detail_load.rows,
                    total_rows=total_load.rows,
                    measures=measures,
                    label=f"{detail.contract_id} against {total.contract_id}",
                )
            )
        )
    return outcome


def assess_source_delivery(request: AssessmentRequest) -> DeliveryAssessment:
    """Assess landed source evidence for Semantic V2 publication."""
    delivery_wide = _control_totals(request)
    return DeliveryAssessment(
        manifest=request.manifest,
        assessments=tuple(
            ContractAssessment(
                load=load,
                outcome=_source_assertions(request, load).combined(delivery_wide),
            )
            for load in request.loads
        ),
    )


@dataclass(frozen=True, slots=True)
class WithheldResidual:
    """What one delivery could not attribute, keyed on the delivery rather than on a row.

    Every key comes from the manifest or from the contract's own identity, so suppression cannot
    reach it — which is the whole of
    [ADR-034](../../../docs/decisions/adr-034-a-withheld-residual-is-keyed-on-its-delivery.md). AMC
    nulls a row's dimensions; we authored the delivery's, because we landed it.
    """

    contract_id: str
    marketplace: str
    period_start: date
    period_end: date
    option_set_hash: str
    provenance: Mapping[str, str]
    """`basis_key` and `source_timezone`, the conformed provenance every fact row carries.

    A residual is still a measurement, so it declares what it was measured on and which zone its
    day closed in. Taken from `conformed_provenance_values`, the one home for those literals —
    restating them here would be a second place for a contract's basis to be recorded.
    """

    measures: Mapping[str, object]
    """The withheld figures, summed across every fully-nulled row in the delivery.

    Summed rather than kept separate because they are indistinguishable: AMC merges rows that are
    identical after nulling, and for a contract mapping only NONE and LOW columns every fully
    nulled row *is* identical. Several arriving means the merge did not collapse them, and they
    still cannot be told apart.
    """

    row_count: int
    """How many delivered rows were folded in, so a load can report what it moved."""

    by_row_kind: tuple[tuple[str, Mapping[str, object]], ...] = ()
    """For a fact carrying a variant `row_kind` (plan D11): the withheld figures per row kind.

    Pack 5's touch-only variant withholds exposure and outcome rows in one delivery, and the two
    branches' figures are different measurements, so the residual keeps them apart under the
    `row_kind` the pack's SELECT stamped (a literal, which suppression leaves on the row). Each
    part is summed by the same rule as `measures`, and together they make `measures`. Empty for a
    fact with no `row_kind`, whose residual is the one delivery-keyed row.
    """

    bucket_scheme: str | None = None
    """For a frequency fact (packs 6 and 7, plan D11): the scheme the withheld buckets were
    counted under, the literal the pack's SELECT stamped on every withheld row. One delivery is
    one scheme, so it keys every residual row of the delivery. None for a fact with no
    `bucket_scheme`."""
    cohort_keys: tuple[tuple[str, object], ...] = ()
    """For a cohort fact (packs 14 and 15, plan D11): the vintage's `as_of_month`, the
    `cohort_path` the contract stamps and the `cohort_grain` the pack's SELECT stamps on every
    withheld row. One delivery is one vintage, path and grain, so they key every residual row of
    the delivery. Empty for a fact with no `cohort_grain`."""


def _source_mapped_grain(contract: DataContract) -> tuple[str, ...]:
    """Name the grain columns whose values AMC supplies, and can therefore withhold.

    Excludes a literal: `channel_key` on a DSP contract is stamped by us during mapping, so it is
    never null however few users sit behind the row. Excludes an extraction option too —
    `rows_suppressed` is synthesised by AMC because the workflow asked, and is present on every
    row including a withheld one.
    """
    return tuple(
        mapping.target_column
        for mapping in contract.fields
        if mapping.target_column in contract.grain
        and not mapping.is_literal
        and not mapping.assigned_by_load
        and not mapping.from_extraction_option
    )


@dataclass(frozen=True, slots=True)
class SuppressionSplit:
    """Attributable mapped rows, their source ordinals, and exact delivery residual."""

    attributable_rows: tuple[dict[str, object], ...]
    attributable_ordinals: tuple[int, ...]
    residual: WithheldResidual | None

    def __post_init__(self) -> None:
        if len(self.attributable_rows) != len(self.attributable_ordinals):
            raise ValueError("suppression split rows and source ordinals differ in length.")
        if tuple(sorted(set(self.attributable_ordinals))) != self.attributable_ordinals:
            raise ValueError("suppression split source ordinals must be sorted and unique.")


def split_withheld(
    contract: DataContract,
    manifest: Manifest,
    rows: Sequence[Mapping[str, object]],
) -> SuppressionSplit:
    """Split mapped rows while preserving exact source ordinals for canonical compilation.

    A row whose every source-supplied grain column is null cannot be placed in its fact grain, so
    it is folded into delivery-keyed residual evidence. A partially nulled row is refused rather
    than guessed at because it belongs safely in neither the attributable fact nor the residual.
    """
    keys = _source_mapped_grain(contract)
    if not keys:  # pragma: no cover — every fact contract maps at least one grain column
        attributable = tuple(dict(row) for row in rows)
        return SuppressionSplit(attributable, tuple(range(len(attributable))), None)

    kept: list[dict[str, object]] = []
    kept_ordinals: list[int] = []
    withheld: list[Mapping[str, object]] = []
    for index, row in enumerate(rows):
        absent = tuple(key for key in keys if row.get(key) in (None, ""))
        if not absent:
            kept.append(dict(row))
            kept_ordinals.append(index)
            continue
        if len(absent) != len(keys):
            raise IngestionError(
                f"{contract.contract_id} row {index}: {', '.join(absent)} are absent but "
                f"{', '.join(k for k in keys if k not in absent)} are present. A privacy "
                "withholding nulls every grain column a row failed the threshold for, so a "
                "partial one means this contract maps a column with a stricter threshold than "
                "the rest. Such a row belongs in neither the fact nor its residual."
            )
        withheld.append(row)

    return SuppressionSplit(
        attributable_rows=tuple(kept),
        attributable_ordinals=tuple(kept_ordinals),
        residual=_folded(contract, manifest, withheld),
    )


@dataclass(frozen=True, slots=True)
class DiscardSplit:
    """The rows a `discard_suppressed_rows` delivery keeps, and the ones it dropped."""

    attributable_ordinals: tuple[int, ...]
    discarded: int
    withheld_ordinals: tuple[int, ...] = ()
    """The dropped rows, whose additive measures go to the `_withheld` residual (ADR-099 pt 7)."""


def split_discarded(
    contract: DataContract,
    document_rows: Sequence[Mapping[str, object]],
    measures: frozenset[str],
) -> DiscardSplit:
    """Drop every delivered row flagged `rows_suppressed = true` (ADR-099 point 7).

    Read off the delivered discriminator itself, not off which grain columns are null, so a
    withheld row is dropped whatever AMC left on it: under the per-path bar a row holding two or
    more shoppers is withheld when one source path has fewer (plan D19), and it is dropped the same
    way. Distinct counts are never folded (ADR-099 point 12). A contract that also maps an additive
    measure folds the dropped rows' additive measures into its residual (`fold_discarded`). The
    discarded count is reported as a diagnostic beside the cell loss.
    """
    del measures  # every measure is read from the rows `fold_discarded` folds
    if contract.suppression is None:
        raise IngestionError(
            f"{contract.contract_id}: only a suppression-bearing contract discards."
        )
    column = contract.suppression.discriminator_column
    kept: list[int] = []
    for index, row in enumerate(document_rows):
        try:
            suppressed = suppression_flag(row.get(column))
        except MappingError:
            raise IngestionError(
                f"{contract.contract_id} row {index}: {column} is neither true nor false."
            ) from None
        if not suppressed:
            kept.append(index)
    survivors = set(kept)
    withheld = tuple(index for index in range(len(document_rows)) if index not in survivors)
    return DiscardSplit(tuple(kept), len(withheld), withheld)


@dataclass(frozen=True, slots=True)
class DiscardFoldRequest:
    """A discard delivery's dropped rows, and the contract's measure columns."""

    contract: DataContract
    manifest: Manifest
    rows: tuple[Mapping[str, object], ...]
    measures: frozenset[str]


def fold_discarded(request: DiscardFoldRequest) -> WithheldResidual | None:
    """Fold a discard delivery's dropped rows' additive measures into its residual (ADR-099 pt 7).

    Only the additive `measures` and each money measure's own currency column are read: a dropped
    row's labels (a surviving `exposure_source_set`) aren't figures, and its distinct counts are
    never summed. Money on a row whose currency came back NULL isn't summed (AMC-V decision 5), so a
    residual whose every withheld amount lost its currency stores it NULL, which reads "withheld".
    None when nothing was dropped or nothing additive is mapped.
    """
    contract, rows = request.contract, request.rows
    distinct = {item.target_column for item in contract.fields if item.non_additive}
    additive = request.measures - distinct
    if not rows or not additive:
        return None
    currencies = {
        item.currency_column
        for item in contract.monetary_fields
        if item.target_column in additive and item.currency_column
    }
    kept = additive | currencies | {ROW_KIND_COLUMN, BUCKET_SCHEME_COLUMN, COHORT_GRAIN_COLUMN}
    return _folded(
        contract,
        request.manifest,
        tuple({k: v for k, v in row.items() if k in kept} for row in rows),
    )


def _folded(
    contract: DataContract, manifest: Manifest, rows: Sequence[Mapping[str, object]]
) -> WithheldResidual | None:
    """Sum the withheld rows into one residual, or None when nothing was withheld.

    `None` rather than a residual of zeroes, because "this delivery withheld nothing" and "this
    delivery withheld zero impressions" are different statements and only the first is true. The
    same argument as an empty monetary result carrying no currency.
    """
    if not rows:
        return None
    by_row_kind: tuple[tuple[str, Mapping[str, object]], ...] = ()
    if contract.field_for(ROW_KIND_COLUMN) is not None:
        kinds: dict[str, list[Mapping[str, object]]] = {}
        for index, row in enumerate(rows):
            kind = row.get(ROW_KIND_COLUMN)
            if kind is None or not str(kind).strip():
                raise IngestionError(
                    f"{contract.contract_id} withheld row {index}: {ROW_KIND_COLUMN} is absent, "
                    "so its figures can't be kept apart from another branch's. The pack's "
                    "SELECT stamps it on every row, a withheld one included."
                )
            kinds.setdefault(str(kind).strip(), []).append(row)
        by_row_kind = tuple(
            (kind, _summed(contract, tuple(group))) for kind, group in sorted(kinds.items())
        )
    return WithheldResidual(
        contract_id=contract.contract_id,
        provenance=conformed_provenance_values(contract),
        marketplace=manifest.marketplace,
        period_start=manifest.period_start,
        period_end=manifest.period_end,
        option_set_hash=manifest.option_set_hash,
        measures=_summed(contract, rows),
        row_count=len(rows),
        by_row_kind=by_row_kind,
        bucket_scheme=_withheld_literal(contract, rows, BUCKET_SCHEME_COLUMN),
        cohort_keys=_withheld_cohort_keys(contract, manifest, rows),
    )


def _withheld_cohort_keys(
    contract: DataContract, manifest: Manifest, rows: Sequence[Mapping[str, object]]
) -> tuple[tuple[str, object], ...]:
    """Key a cohort delivery's residual on its vintage, path and cohort grain (plan D11)."""
    grain = _withheld_literal(contract, rows, COHORT_GRAIN_COLUMN)
    path = contract.field_for(COHORT_PATH_COLUMN)
    if grain is None or path is None or path.literal_value is None:
        return ()
    return (
        (AS_OF_MONTH_COLUMN, manifest.period_end.replace(day=1)),
        (COHORT_GRAIN_COLUMN, grain),
        (COHORT_PATH_COLUMN, str(path.literal_value)),
    )


def _withheld_literal(
    contract: DataContract, rows: Sequence[Mapping[str, object]], column: str
) -> str | None:
    """Return the one SELECT-stamped literal a delivery's withheld rows carry, or fail closed.

    Packs 6-7 stamp `bucket_scheme` and packs 14-15 `cohort_grain` on every row. None for a
    contract without the column. Every withheld row must carry the literal, and all the same one:
    a row without it, or two values in one delivery, would put figures counted under different
    buckets or cohort grains on one residual key.
    """
    if contract.field_for(column) is None:
        return None
    values: set[str] = set()
    for index, row in enumerate(rows):
        value = row.get(column)
        if value is None or not str(value).strip():
            raise IngestionError(
                f"{contract.contract_id} withheld row {index}: {column} is absent, so the "
                "residual can't name what its figures were counted under. The pack's SELECT "
                "stamps it on every row, a withheld one included."
            )
        values.add(str(value).strip())
    if len(values) != 1:
        raise IngestionError(
            f"{contract.contract_id}: the withheld rows carry {column} values {sorted(values)}; "
            "one delivery is counted under one."
        )
    return values.pop()


def _summed(contract: DataContract, rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Sum withheld rows' measures by the residual's rules (see `_folded`)."""
    # A distinct count never joins the residual (ADR-099 point 12): no total is built by summing
    # it, and two withheld rows' counts may share shoppers. Today every pack whose contract maps
    # one refuses a delivery with any withheld row first, so nothing reaches here to drop yet.
    distinct = {field.target_column for field in contract.fields if field.non_additive}
    # A money amount on a withheld row whose currency came back NULL has no unit, so it is never
    # summed into the residual's money (AMC-V decision 5): the residual's money then covers only
    # the rows that kept a currency, and a residual with none stores the amount NULL, which reads
    # "withheld". Counts and other non-money measures on the same row are summed as before.
    money = {field.target_column: field.currency_column or "" for field in contract.monetary_fields}
    measures: dict[str, object] = {}
    for row in rows:
        for name, value in row.items():
            if name in contract.grain or name in CONFORMED_PROVENANCE_COLUMNS or name in distinct:
                continue
            if value is None or value == "":
                continue
            if name in money and not withheld_money_counts(row.get(money[name])):
                continue
            running = measures.get(name)
            measures[name] = value if running is None else _added(running, value)
    return measures


def _added(running: object, value: object) -> object:
    """Add two delivered measure values, refusing anything that is not a quantity.

    Values arrive as strings from a CSV, so this parses rather than assuming. A non-numeric measure
    is a mapping defect and fails here naming nothing it could have silently coerced.
    """
    try:
        return Decimal(str(running)) + Decimal(str(value))
    except InvalidOperation, ValueError:
        if running == value:
            return running
        raise IngestionError(
            f"a withheld row carries {value!r} where a quantity was expected, so the residual "
            "cannot be summed. Suppression withholds dimensions and keeps measures, so a "
            "non-numeric measure here is a mapping defect rather than a withholding."
        ) from None
