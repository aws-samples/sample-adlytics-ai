# SPDX-License-Identifier: MIT-0
"""Service-neutral values for governed source production and registration."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import cast

from domain.semantics.identity import SourceIdentifier
from domain.semantics.publication import PACK_VARIANT_OPTION as LENS_PACK_VARIANT_OPTION
from domain.semantics.publication import PERIOD_GRAIN_OPTION as LENS_PERIOD_GRAIN_OPTION
from domain.shared_kernel.provenance import ProducerGovernance

SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._-]+$")
SHA256_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
REGISTRATION_SCHEMA_VERSION = "2"
MAX_REGISTRATION_BYTES = 1_000_000


def digest_bytes(body: bytes) -> str:
    """Return one algorithm-qualified digest for exact bytes."""
    return f"sha256:{hashlib.sha256(body).hexdigest()}"


def digest_json(value: object) -> str:
    """Return a stable digest for a JSON-compatible semantic value."""
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return digest_bytes(rendered.encode())


def _require_token(value: str, label: str) -> None:
    if not SAFE_TOKEN.fullmatch(value):
        raise ValueError(f"{label} must contain only letters, digits, dot, dash, or underscore.")


def _require_digest(value: str, label: str) -> None:
    if not SHA256_DIGEST.fullmatch(value):
        raise ValueError(f"{label} must be an algorithm-qualified lowercase SHA-256 digest.")


class AccessTier(StrEnum):
    """Customer-side source access required to execute a query pack."""

    STANDARD = "standard"
    PAID_FEATURE = "paid_feature"


class LiveValidationPolicy(StrEnum):
    """How live plausibility handles source capability."""

    REQUIRED = "required"
    CAPABILITY_CONDITIONAL = "capability_conditional"


class ScheduleKind(StrEnum):
    """How the source execution was initiated."""

    ADHOC = "adhoc"
    SCHEDULED = "scheduled"


class SuppressionPolicy(StrEnum):
    """What a delivery's privacy-withheld rows do (ADR-099 point 7, ADR-110).

    Replaces the boolean `rejectSuppressedRows`, which reads as `reject_delivery`. A pack that
    declares neither keeps today's behaviour: withheld rows fold into the delivery's `_withheld`
    residual (ADR-034).
    """

    REJECT_DELIVERY = "reject_delivery"
    """Any withheld row refuses the whole delivery (sponsored and DSP reach)."""

    DISCARD_SUPPRESSED_ROWS = "discard_suppressed_rows"
    """Withheld rows are dropped before the fact write; surviving rows publish (shopper packs)."""


class PeriodGrain(StrEnum):
    """A declared measurement period a periodic pack extracts (ADR-110 point 1).

    Each grain has exactly one alignment, all on the AMC (UTC) day: `week` is an ISO week,
    Monday to Sunday; `month` is a calendar month; `quarter` is a calendar quarter; `custom` is a
    cohort vintage, the 13 whole calendar months ending on the last day of its `as_of_month`
    (ADR-104, `domain.semantics.calendar_periods.VINTAGE_MONTHS`). So the alignment is a property
    of the grain rather than a second declaration that could disagree.
    """

    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    CUSTOM = "custom"


class ParameterDataType(StrEnum):
    """AMC scalar `inputParameters` types (`docs/sources/amc.md`, Parameterisation).

    `ARRAY` is left out on purpose: no list-valued parameter varies between runs, so the
    differencing guard never sees a changing filter (ADR-110 point 3).
    """

    BOOLEAN = "BOOLEAN"
    DATE = "DATE"
    DOUBLE = "DOUBLE"
    INTEGER = "INTEGER"
    LONG = "LONG"
    STRING = "STRING"
    TIMESTAMP = "TIMESTAMP"


class PeriodValue(StrEnum):
    """A parameter value derived from the registration's own period, never from a mapping."""

    AS_OF_MONTH = "as_of_month"
    """A cohort vintage's `as_of_month` as `YYYY-MM-01 00:00:00`, the text pack 14's SQL casts to
    a TIMESTAMP (ADR-104; the form the 2026-10-09 probes passed, `validation/RESULTS.md`)."""

    SNAPSHOT_START = "snapshot_start"
    """The day before the month's first Thursday, `YYYY-MM-DD 00:00:00` (packs 10-11, design
    §5.5): Amazon takes the ASI snapshot that Thursday, so `[start, end)` holds exactly one."""

    SNAPSHOT_END = "snapshot_end"
    """The day after the month's first Thursday, `YYYY-MM-DD 00:00:00`: the range's open end."""

    @property
    def grain(self) -> PeriodGrain:
        """The one period grain this value is derived from: a cohort vintage, or a month."""
        return PeriodGrain.CUSTOM if self is PeriodValue.AS_OF_MONTH else PeriodGrain.MONTH


PARAMETER_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
# The registration option naming a periodic delivery's grain, so week and month deliveries of
# one pack are separate option sets and the load's overlap check passes (ADR-110 point 1). Its
# one home is the publication lens, which hashes it (`domain.semantics.publication`).
PERIOD_GRAIN_OPTION = LENS_PERIOD_GRAIN_OPTION
PACK_VARIANT_OPTION = LENS_PACK_VARIANT_OPTION
"""The registration option naming a declared variant's delivery (plan D9); its one home is the
publication lens, which hashes it."""
DEFAULT_CADENCE_LAG_DAYS = 2
"""AMC's 48-hour availability SLA (`docs/sources/amc.md`), the lag a pack without one takes."""
DEFAULT_EXPECTED_BAR = 2
EXPECTED_BARS = frozenset({2, 100})
"""The two privacy bars AMC applies: 2 users for NONE and LOW columns, 100 for MEDIUM and HIGH."""


@dataclass(frozen=True, slots=True)
class InputParameter:
    """One governed AMC query parameter, resolved from a governed mapping, never templated.

    Exactly one source fills it. `commerce_identity` names the marketplace-alias namespace in
    `contracts/shared/commerce-identities.yaml` whose value for the registration's marketplace
    fills the parameter; `period_value` derives it from the registration's period (a cohort
    vintage's `as_of_month`, packs 14-15).
    """

    name: str
    data_type: ParameterDataType
    commerce_identity: str | None = None
    period_value: PeriodValue | None = None

    def __post_init__(self) -> None:
        if not PARAMETER_NAME.fullmatch(self.name):
            raise ValueError(
                f"input parameter {self.name!r} must be lowercase letters, digits and "
                "underscores, starting with a letter."
            )
        if self.name == PERIOD_GRAIN_OPTION:
            raise ValueError(f"input parameter cannot be named {PERIOD_GRAIN_OPTION!r}.")
        if (self.commerce_identity is None) == (self.period_value is None):
            raise ValueError(
                f"input parameter {self.name} takes exactly one of commerceIdentity and "
                "periodValue."
            )
        if self.commerce_identity is not None:
            _require_token(self.commerce_identity, f"input parameter {self.name} namespace")

    @property
    def declared(self) -> dict[str, str]:
        """Render the parameter as the pack's digest binds it."""
        if self.commerce_identity is not None:
            return {"commerceIdentity": self.commerce_identity, "dataType": self.data_type.value}
        assert self.period_value is not None  # noqa: S101 -- __post_init__ requires one source
        return {"dataType": self.data_type.value, "periodValue": self.period_value.value}


@dataclass(frozen=True, slots=True)
class PeriodPolicy:
    """The calendar periods a periodic pack extracts; absent means a daily pack."""

    grains: tuple[PeriodGrain, ...]

    def __post_init__(self) -> None:
        if not self.grains:
            raise ValueError("a period policy must declare at least one grain.")
        if len(set(self.grains)) != len(self.grains):
            raise ValueError("a period policy repeats a grain.")
        if self.grains != tuple(grain for grain in PeriodGrain if grain in self.grains):
            raise ValueError("period policy grains must be listed week, month, quarter, custom.")
        if PeriodGrain.CUSTOM in self.grains and len(self.grains) > 1:
            raise ValueError("a cohort vintage (custom) is a pack's only period grain.")


class VariantTrigger(StrEnum):
    """What switches a declared pack variant on (plan D9, D10)."""

    MANUAL = "manual"
    """Only a registration naming the variant runs it, and only while no automatic variant is
    due (pack 5's touch-only variant, design §8 "criterion 7 fails" row (1))."""

    DEGRADE_NEXT_PERIOD = "degrade_next_period"
    """Due from the next period once a delivery's graded slice passes ADR-019's degrade
    threshold, and sticky until Step B (packs 6-7 coarse buckets, 14-15 quarterly cohorts)."""


@dataclass(frozen=True, slots=True)
class PackVariant:
    """One declared pre-built alternative of a governed pack (plan D9).

    Its own SQL and pack version, over the same contract and source pack, so its deliveries are
    governed exactly as the base pack's are. The variant id goes into the registration's option
    set (`PACK_VARIANT_OPTION`), so its rows are a series of their own.
    """

    variant: str
    pack_version: int
    sql: str
    query_digest: str
    expected_columns: tuple[str, ...]
    trigger: VariantTrigger

    def __post_init__(self) -> None:
        if not PARAMETER_NAME.fullmatch(self.variant):
            raise ValueError(
                f"pack variant {self.variant!r} must be lowercase letters, digits and underscores."
            )
        if self.pack_version < 1:
            raise ValueError(f"pack variant {self.variant}: pack version starts at 1.")
        if digest_bytes(self.sql.encode()) != self.query_digest:
            raise ValueError(f"pack variant {self.variant}: query_digest doesn't match its SQL.")
        if not self.expected_columns or len(set(self.expected_columns)) != len(
            self.expected_columns
        ):
            raise ValueError(f"pack variant {self.variant}: expected_columns must be unique.")

    @property
    def declared(self) -> dict[str, object]:
        """Render the variant as the base pack's digest binds it."""
        return {
            "expectedColumns": list(self.expected_columns),
            "packVersion": self.pack_version,
            "queryDigest": self.query_digest,
            "trigger": self.trigger.value,
            "variant": self.variant,
        }


@dataclass(frozen=True, slots=True)
class GovernedQueryPack:
    """One extraction query bound to exact downstream governed artifacts.

    The last five fields are optional with stated defaults (ADR-110), so an existing pack, or a
    daily pack another design adds, declares none of them and keeps its digest. Each field joins
    the digest only when it differs from its default.
    """

    pack_id: str
    pack_version: int
    contract_id: str
    contract_version: int
    contract_digest: str
    source_pack_id: str
    source_pack_version: int
    source_pack_digest: str
    source_system: str
    source_dataset: str
    sql: str
    query_digest: str
    expected_columns: tuple[str, ...]
    execution_options: tuple[tuple[str, str], ...]
    max_result_bytes: int
    access_tier: AccessTier
    live_validation: LiveValidationPolicy
    safety_blockers: tuple[str, ...] = ()
    suppression_policy: SuppressionPolicy | None = None
    period_policy: PeriodPolicy | None = None
    cadence_lag_days: int | None = None
    """Days after period end the delivery is expected; `None` takes the default. Required on a
    paid-feature pack, because its pending-versus-missing state reads it (ADR-107)."""
    input_parameters: tuple[InputParameter, ...] = ()
    expected_bar: int = DEFAULT_EXPECTED_BAR
    retained_governance: tuple[ProducerGovernance, ...] = ()
    """Exact earlier-version bindings whose landed deliveries still load (`retainedVersions`).

    A pack version bump changes every digest a landed manifest binds, so without these a
    delivery, re-land or reprocess of evidence the earlier version produced would be refused as
    ungoverned. Each is the earlier version's exact governance, recorded when the version was
    superseded; the producer only ever runs the current version. Not part of `digest`: it says
    what the load accepts, not what the producer executes.
    """
    variants: tuple[PackVariant, ...] = ()
    """Declared pre-built alternatives (plan D9), each its own pack version. Part of `digest`
    only when declared, so a pack without variants keeps its digest."""
    variant_id: str | None = None
    """Set on the binding `variant_binding` derives for one variant; None on the base pack."""

    def __post_init__(self) -> None:
        for label, value in (
            ("pack_id", self.pack_id),
            ("contract_id", self.contract_id),
            ("source_pack_id", self.source_pack_id),
            ("source_system", self.source_system),
            ("source_dataset", self.source_dataset),
        ):
            _require_token(value, label)
        if self.pack_version < 1 or self.contract_version < 1 or self.source_pack_version < 1:
            raise ValueError("pack, contract, and source-pack versions start at 1.")
        for label, value in (
            ("contract_digest", self.contract_digest),
            ("source_pack_digest", self.source_pack_digest),
            ("query_digest", self.query_digest),
        ):
            _require_digest(value, label)
        if not self.sql.strip():
            raise ValueError(f"{self.pack_id}: query SQL cannot be blank.")
        if digest_bytes(self.sql.encode()) != self.query_digest:
            raise ValueError(f"{self.pack_id}: query_digest does not match the execution SQL.")
        if not self.expected_columns:
            raise ValueError(f"{self.pack_id}: expected_columns cannot be empty.")
        if len(set(self.expected_columns)) != len(self.expected_columns):
            raise ValueError(f"{self.pack_id}: expected_columns contains a duplicate.")
        option_names = tuple(name for name, _value in self.execution_options)
        if not option_names or len(set(option_names)) != len(option_names):
            raise ValueError(f"{self.pack_id}: execution options must be nonempty and unique.")
        if any(not name.strip() or not value.strip() for name, value in self.execution_options):
            raise ValueError(f"{self.pack_id}: execution option names and values cannot be blank.")
        if self.max_result_bytes < 1:
            raise ValueError(f"{self.pack_id}: max_result_bytes must be positive.")
        for blocker in self.safety_blockers:
            _require_token(blocker, "safety blocker")
        self._check_declarations()
        self._check_retained()
        self._check_variants()

    def _check_variants(self) -> None:
        """Each variant has its own id and a pack version no other binding of this pack uses."""
        ids = tuple(item.variant for item in self.variants)
        versions = (
            self.pack_version,
            *(item.query_pack_version for item in self.retained_governance),
            *(item.pack_version for item in self.variants),
        )
        if len(set(ids)) != len(ids) or len(set(versions)) != len(versions):
            raise ValueError(
                f"{self.pack_id}: variants need distinct ids and pack versions distinct from the "
                "current and retained versions."
            )
        if self.variant_id is not None and self.variants:
            raise ValueError(f"{self.pack_id}: a variant binding declares no variants of its own.")

    def _check_retained(self) -> None:
        """Each retained binding is an earlier version of this pack, over the same identities."""
        versions = tuple(item.query_pack_version for item in self.retained_governance)
        if len(set(versions)) != len(versions) or any(
            version >= self.pack_version for version in versions
        ):
            raise ValueError(
                f"{self.pack_id}: retainedVersions name distinct versions earlier than "
                f"{self.pack_version}."
            )
        for item in self.retained_governance:
            if (item.query_pack_id, item.data_contract_id, item.source_pack_id) != (
                self.pack_id,
                self.contract_id,
                self.source_pack_id,
            ):
                raise ValueError(
                    f"{self.pack_id}: a retained version binds the same pack, contract and "
                    "source pack."
                )

    def _check_declarations(self) -> None:
        """Check the optional ADR-110 fields; each has a default, so absence is always valid."""
        if self.cadence_lag_days is None:
            if self.access_tier is AccessTier.PAID_FEATURE:
                raise ValueError(
                    f"{self.pack_id}: a paid-feature pack must declare cadenceLagDays."
                )
        elif isinstance(self.cadence_lag_days, bool) or self.cadence_lag_days < 0:
            raise ValueError(f"{self.pack_id}: cadenceLagDays must be a non-negative integer.")
        if self.expected_bar not in EXPECTED_BARS:
            raise ValueError(f"{self.pack_id}: expectedBar must be 2 or 100.")
        names = tuple(parameter.name for parameter in self.input_parameters)
        if len(set(names)) != len(names):
            raise ValueError(f"{self.pack_id}: inputParameters repeats a name.")
        if names != tuple(sorted(names)):
            raise ValueError(f"{self.pack_id}: inputParameters must be sorted by name.")
        clashing = sorted(set(names) & {name for name, _value in self.execution_options})
        if clashing:
            raise ValueError(
                f"{self.pack_id}: inputParameters reuse execution option names {clashing}."
            )
        grains = () if self.period_policy is None else self.period_policy.grains
        for item in self.input_parameters:
            if item.period_value is not None and grains != (item.period_value.grain,):
                raise ValueError(
                    f"{self.pack_id}: periodValue {item.period_value.value} needs periodPolicy "
                    f"grains [{item.period_value.grain.value}], the period it is derived from."
                )

    @property
    def reject_suppressed_rows(self) -> bool:
        """Whether any withheld row refuses the delivery (the pre-ADR-110 boolean)."""
        return self.suppression_policy is SuppressionPolicy.REJECT_DELIVERY

    @property
    def effective_cadence_lag_days(self) -> int:
        """The declared lag, or AMC's 48-hour default."""
        return DEFAULT_CADENCE_LAG_DAYS if self.cadence_lag_days is None else self.cadence_lag_days

    @property
    def declared_fields(self) -> dict[str, object]:
        """Render each ADR-110 field only where it differs from its default.

        So a pack that declares none, or declares a default (a paid pack's `cadenceLagDays: 2`),
        keeps the digest it had: the digest binds behaviour, and the behaviour didn't change.
        `reject_delivery` keeps its original `rejectSuppressedRows` rendering for the same reason.
        """
        rendered: dict[str, object] = {}
        if self.effective_cadence_lag_days != DEFAULT_CADENCE_LAG_DAYS:
            rendered["cadenceLagDays"] = self.effective_cadence_lag_days
        if self.expected_bar != DEFAULT_EXPECTED_BAR:
            rendered["expectedBar"] = self.expected_bar
        if self.input_parameters:
            rendered["inputParameters"] = {
                parameter.name: parameter.declared for parameter in self.input_parameters
            }
        if self.period_policy is not None:
            rendered["periodPolicy"] = {
                "grains": [grain.value for grain in self.period_policy.grains]
            }
        if self.suppression_policy is SuppressionPolicy.REJECT_DELIVERY:
            rendered["rejectSuppressedRows"] = True
        elif self.suppression_policy is not None:
            rendered["suppressionPolicy"] = self.suppression_policy.value
        if self.variants:
            rendered["variants"] = [item.declared for item in self.variants]
        if self.variant_id is not None:
            rendered["variant"] = self.variant_id
        return rendered

    def variant_binding(self, variant: str) -> GovernedQueryPack:
        """Return the exact binding a producer runs, and a Finalizer checks, for one variant.

        The variant's SQL, columns and pack version over this pack's contract and source pack, with
        `variant_id` set, so its governance and digest name the variant rather than the base pack.
        """
        found = next((item for item in self.variants if item.variant == variant), None)
        if found is None:
            raise ValueError(f"{self.pack_id} declares no variant {variant!r}.")
        return replace(
            self,
            pack_version=found.pack_version,
            sql=found.sql,
            query_digest=found.query_digest,
            expected_columns=found.expected_columns,
            retained_governance=(),
            variants=(),
            variant_id=found.variant,
        )

    def binding_for(self, version: int) -> GovernedQueryPack | None:
        """Resolve a registration's pack version to the current pack or one of its variants.

        A retained version resolves to nothing: it is kept so its landed deliveries still load,
        and the producer never runs it again.
        """
        if version == self.pack_version:
            return self
        found = next((item for item in self.variants if item.pack_version == version), None)
        return None if found is None else self.variant_binding(found.variant)

    @property
    def variant_governance(self) -> tuple[ProducerGovernance, ...]:
        """Every declared variant's exact governance, which a landed manifest may carry."""
        return tuple(self.variant_binding(item.variant).governance for item in self.variants)

    @property
    def digest(self) -> str:
        """Bind the pack definition and every governed downstream artifact."""
        return digest_json(
            {
                "accessTier": self.access_tier.value,
                "contract": {
                    "digest": self.contract_digest,
                    "id": self.contract_id,
                    "version": self.contract_version,
                },
                "expectedColumns": list(self.expected_columns),
                "executionOptions": dict(self.execution_options),
                "liveValidation": self.live_validation.value,
                "maxResultBytes": self.max_result_bytes,
                "packId": self.pack_id,
                "packVersion": self.pack_version,
                "queryDigest": self.query_digest,
                "safetyBlockers": list(self.safety_blockers),
                **self.declared_fields,
                "sourceDataset": self.source_dataset,
                "sourcePack": {
                    "digest": self.source_pack_digest,
                    "id": self.source_pack_id,
                    "version": self.source_pack_version,
                },
                "sourceSystem": self.source_system,
            }
        )

    @property
    def governance(self) -> ProducerGovernance:
        """Project the exact identity carried into a finalized manifest."""
        return ProducerGovernance(
            query_pack_id=self.pack_id,
            query_pack_version=self.pack_version,
            query_pack_digest=self.digest,
            query_digest=self.query_digest,
            data_contract_id=self.contract_id,
            data_contract_digest=self.contract_digest,
            source_pack_id=self.source_pack_id,
            source_pack_digest=self.source_pack_digest,
        )


@dataclass(frozen=True, slots=True)
class QueryPackSelection:
    """Producer-declared pack and exact SQL identity."""

    pack_id: str
    version: int
    query_digest: str

    def __post_init__(self) -> None:
        _require_token(self.pack_id, "query pack id")
        if self.version < 1:
            raise ValueError("query pack version starts at 1.")
        _require_digest(self.query_digest, "query digest")


@dataclass(frozen=True, slots=True)
class ExecutionIdentity:
    """Source execution, workflow, and schedule identity."""

    execution_id: str
    workflow_id: str
    schedule_kind: ScheduleKind
    schedule_id: str | None

    def __post_init__(self) -> None:
        _require_token(self.execution_id, "execution id")
        _require_token(self.workflow_id, "workflow id")
        if self.schedule_kind is ScheduleKind.ADHOC and self.schedule_id is not None:
            raise ValueError("an ad hoc execution must carry schedule_id as null.")
        if self.schedule_kind is ScheduleKind.SCHEDULED:
            if self.schedule_id is None:
                raise ValueError("a scheduled execution requires schedule_id.")
            _require_token(self.schedule_id, "schedule id")


@dataclass(frozen=True, slots=True)
class IncomingObject:
    """One exact incoming result selected by a registration."""

    key: str
    version_id: str

    def __post_init__(self) -> None:
        if not self.key.strip() or self.key.startswith("/") or self.key.endswith("/"):
            raise ValueError("incoming object key must be a nonblank relative S3 object key.")
        if not self.version_id.strip():
            raise ValueError("incoming object VersionId cannot be blank.")


@dataclass(frozen=True, slots=True)
class SourceRegistration:
    """The complete producer statement written after one exact result exists."""

    schema_version: str
    source_system: str
    source_dataset: str
    source_scope: SourceIdentifier
    marketplace: str
    period_start: date
    period_end: date
    source_watermark: date | None
    retrieved_at: datetime
    option_set: Mapping[str, object]
    execution: ExecutionIdentity
    query_pack: QueryPackSelection
    object: IncomingObject

    def __post_init__(self) -> None:
        if self.schema_version != REGISTRATION_SCHEMA_VERSION:
            raise ValueError(
                f"registration schema_version must be {REGISTRATION_SCHEMA_VERSION!r}."
            )
        for label, value in (
            ("source_system", self.source_system),
            ("source_dataset", self.source_dataset),
            ("marketplace", self.marketplace),
        ):
            _require_token(value, label)
        if not isinstance(cast("object", self.source_scope), SourceIdentifier):
            raise TypeError("registration source_scope must be a SourceIdentifier.")
        if self.period_end < self.period_start:
            raise ValueError("registration period_end cannot precede period_start.")
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise ValueError("registration retrieved_at must be timezone-aware.")
        if self.retrieved_at.astimezone(UTC).microsecond:
            raise ValueError(
                "registration retrieved_at must have whole-second precision for canonical raw "
                "identity."
            )
        if not self.option_set:
            raise ValueError("registration option_set must contain the complete execution options.")

    @property
    def incoming_prefix(self) -> str:
        """Return the only incoming prefix this identity may describe."""
        return f"incoming/{self.source_system}/{self.source_dataset}/{self.execution.execution_id}/"

    @property
    def registration_key(self) -> str:
        """Return the completion object key for this registration."""
        return f"{self.incoming_prefix}registration.json"


@dataclass(frozen=True, slots=True)
class ResultSummary:
    """Facts deterministically measured from one local or landed CSV."""

    columns: tuple[str, ...]
    row_count: int
    suppressed_row_count: int
    time_window_starts: frozenset[str]
    time_window_ends: frozenset[str]

    def __post_init__(self) -> None:
        if not self.columns:
            raise ValueError("a CSV result must carry a header.")
        if self.row_count < 0:
            raise ValueError("result row_count cannot be negative.")
        if self.suppressed_row_count < 0 or self.suppressed_row_count > self.row_count:
            raise ValueError("suppressed row count must fall within the result row count.")


@dataclass(frozen=True, slots=True)
class ResultEvidence:
    """Manifest facts derived from exact result bytes."""

    byte_count: int
    checksum: str
    declared_columns: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.byte_count < 1:
            raise ValueError("result evidence requires nonempty bytes.")
        _require_digest(self.checksum, "result checksum")
        if not self.declared_columns:
            raise ValueError("result evidence requires declared columns.")
