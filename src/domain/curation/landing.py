# SPDX-License-Identifier: MIT-0
"""Decide what a landed delivery is, before anything tries to read it.

This module owns the **raw-zone landing contract** — the agreement with whoever runs
extraction about where an object goes and what accompanies it. It is a negotiated interface
rather than an internal one (docs/concepts/provenance-chain.md section 2.3), so it is stated
in the domain and enforced on every landing rather than trusted.

**The manifest is authoritative; the prefix is navigational.** Nothing here parses identity
out of an S3 key. The moment a key is parsed there are two implementations of the identity
format that can disagree, and the one that drifts loads a delivery as something it is not —
the same reasoning that makes the run ledger take its partition key as an argument rather
than re-parsing its own stored identity string.

What the prefix is *for*, then, is the three things a manifest cannot do: scope an IAM grant,
filter an S3 event, and let a human find a delivery during an incident without opening
manifests. And because it is derived from the manifest deterministically, comparing where an
object actually landed against where its manifest says it belongs turns the path into a
checksum on the manifest. `misplacement` is that comparison.

**The prefix keys on `source_dataset`, never on a contract.** One vendor document is read by
*two* contracts — `GET_VENDOR_SALES_REPORT` becomes both `vendor-sales-by-asin` and
`vendor-sales-reported-total`, because it carries two sibling arrays at two grains. A prefix
naming a contract could not express that, and it would also force the extraction team to know
our internal contract ids. They name what they ran; `contracts_for` resolves the rest.

**Three failure classes, and they must not be confused.** A *rejected landing* is this
module's: the manifest is absent, malformed, misplaced, or names a dataset no contract reads,
so we cannot even tell which partition it belongs to. A *skip* is `watermarks.py`: we know
exactly what this is and already have it, or something newer. A *quarantine* is
`assertions.py`: we know what it is, it loaded, and it failed a check. Only the third one
concerns a partition, which is why a rejected landing raises rather than returning a
`LoadOutcome` — there is no partition to quarantine yet.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, cast

from domain.curation.mapping import options_hash
from domain.curation.watermarks import PartitionKey
from domain.semantics.identity import SourceIdentifier
from domain.shared_kernel.provenance import ProducerGovernance

if TYPE_CHECKING:  # pragma: no cover — type-only, keeps the import graph flat
    from collections.abc import Mapping

    from domain.data_contracts.contract import DataContract, SourceFormat

__all__ = [
    "GOVERNED_MANIFEST_LAYOUT",
    "LEGACY_MANIFEST_LAYOUT",
    "MANIFEST_NAME",
    "DeliveryContracts",
    "LandedObject",
    "LandingError",
    "Manifest",
    "ManifestPrefix",
    "contracts_for",
    "delivery_prefix_of",
    "manifest_prefix_for",
    "misplacement",
    "partition_for",
    "require_installed_governance",
]

MANIFEST_NAME = "manifest.json"
"""The object whose arrival means a delivery is complete.

**This is the trigger, and that is the whole point of naming it once.** A delivery is more
than one object, so an event on a *data* object fires before the manifest exists and fires
once per part. An event on the manifest fires exactly once per delivery, after everything it
describes is in place — which makes "write the manifest last" the single instruction the
landing contract has to give, and makes it verifiable rather than hoped for.

It also makes the S3 rule a suffix filter and nothing else, so no prefix gymnastics are
needed to subscribe, and a multipart upload of a large data object is simply not subscribed.
"""

SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")
"""What may appear in a path segment.

Deliberately narrow. A segment carrying a slash would silently create a directory level and
put the manifest somewhere its own prefix does not describe; whitespace survives a copy-paste
into a console and not into a script. Every real value already complies — `dsp_impressions`,
`GET_VENDOR_SALES_REPORT`, `analytics_vendorAnalytics_2024_09_30`, `US`.
"""

RETRIEVED_FORMAT = "%Y%m%dT%H%M%SZ"
LEGACY_MANIFEST_LAYOUT = 1
GOVERNED_MANIFEST_LAYOUT = 2
"""How a retrieval instant is spelled in a prefix.

Compact rather than ISO-with-colons, because a colon in an S3 key is legal and miserable —
it needs escaping in a URL and quoting in every shell. The landing contract requires whole-second
precision, so formatting cannot collapse two accepted retrieval instants onto one key.
"""


class LandingError(Exception):
    """A landed delivery cannot be identified, so nothing may be loaded from it."""


@dataclass(frozen=True, slots=True)
class LandedObject:
    """One data object in a delivery, and what proves it arrived whole.

    `byte_count` and `checksum` are both required because they fail differently: a truncated
    upload is caught by the count without reading the body, and a corrupted one only by the
    digest. Neither is redundant.
    """

    name: str
    byte_count: int
    checksum: str
    version_id: str | None = None

    def __post_init__(self) -> None:
        """Refuse an object that cannot be verified on arrival."""
        if not SEGMENT.match(self.name):
            raise LandingError(
                f"{self.name!r} is not a usable object name. It must be a single path "
                "segment of letters, digits, dot, dash or underscore."
            )
        if self.byte_count <= 0:
            raise LandingError(
                f"{self.name}: byte_count is {self.byte_count}. An empty delivered object is "
                "a failed extraction, not a delivery with no rows — a report with no rows "
                "still has a header."
            )
        if not self.checksum.strip():
            raise LandingError(f"{self.name}: checksum is required.")
        if self.version_id is not None and not self.version_id.strip():
            raise LandingError(f"{self.name}: version_id cannot be blank when supplied.")


@dataclass(frozen=True, slots=True)
class ManifestPrefix:
    """The identity fields that deterministically place one canonical manifest."""

    source_system: str
    source_dataset: str
    source_scope: SourceIdentifier
    marketplace: str
    period_start: date
    period_end: date
    option_set_hash: str
    retrieved_at: datetime
    layout_version: int = LEGACY_MANIFEST_LAYOUT
    query_pack_id: str | None = None


def _source_scope_segment(source_scope: SourceIdentifier) -> str:
    """Hash a qualified scope into one unambiguous, path-safe identity segment."""
    qualified = json.dumps(
        {"namespace": source_scope.namespace, "value": source_scope.value},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(qualified).hexdigest()


def manifest_prefix_for(identity: ManifestPrefix) -> str:
    """Render one backward-compatible legacy or query-pack-qualified governed prefix."""
    if identity.layout_version not in {LEGACY_MANIFEST_LAYOUT, GOVERNED_MANIFEST_LAYOUT}:
        raise LandingError(f"unsupported manifest layout version {identity.layout_version}.")
    if identity.layout_version == GOVERNED_MANIFEST_LAYOUT:
        if identity.query_pack_id is None or not SEGMENT.fullmatch(identity.query_pack_id):
            raise LandingError("governed manifest layout requires one safe query pack identity.")
        query_pack = f"query_pack={identity.query_pack_id}/"
    else:
        if identity.query_pack_id is not None:
            raise LandingError("legacy manifest layout cannot carry a query pack path segment.")
        query_pack = ""
    retrieved = identity.retrieved_at.astimezone(UTC).strftime(RETRIEVED_FORMAT)
    return (
        f"{identity.source_system}/"
        f"{identity.source_dataset}/"
        f"{query_pack}"
        f"source_scope={_source_scope_segment(identity.source_scope)}/"
        f"marketplace={identity.marketplace}/"
        f"window={identity.period_start.isoformat()}_{identity.period_end.isoformat()}/"
        f"options={identity.option_set_hash}/"
        f"retrieved={retrieved}/"
    )


@dataclass(frozen=True, slots=True)
class Manifest:
    """The only authoritative statement of what arrived.

    Every field is required and none has a default, which is the point: an optional
    provenance field is a field that gets dropped, and its absence then reads downstream as a
    finding rather than as a gap. `docs/concepts/provenance-chain.md` section 2.5 is the
    specification this implements.

    Not hashable, because `option_set` is a mapping. Nothing needs to hash a manifest —
    `option_set_hash` is the identity-bearing value and it is a string.
    """

    source_system: str
    source_dataset: str
    source_scope: SourceIdentifier
    """The source-owned account or advertiser identity that qualified this extraction."""

    option_set: Mapping[str, object]
    """The full set of options that produced this, verbatim and unedited.

    Kept whole as well as hashed because the hash proves two pulls differ and only the set
    itself says *how*. A `MANUFACTURING` pull and a `SOURCING` pull of one window are
    different datasets that double-count if merged, and this is the evidence of which is
    which.
    """

    option_set_hash: str
    """A deterministic digest of `option_set`, checked against it rather than trusted."""

    marketplace: str
    """One marketplace. Never a list, because one object is one marketplace's data."""

    period_start: date
    period_end: date
    """The window the data covers. Not when it was fetched, and not when it last changed."""

    source_watermark: date | None
    """The source's own revision date when the source provides one.

    Explicit None is reserved for sources such as Data Kiosk snapshots that expose no business
    revision. Their ordering must come from separately typed system-first-observed evidence.
    """

    retrieved_at: datetime
    """When extraction fetched it. A third distinct time, and a real curated column."""

    declared_columns: tuple[str, ...]
    """The column names the objects should contain.

    What makes the layer 3 header assertion possible, and how a workflow other than the one we
    registered is detected: a delivered file whose columns differ from the registered
    extraction's is a different query's output wearing the right filename.
    """

    objects: tuple[LandedObject, ...]
    """Every data object this manifest describes, excluding itself.

    A tuple rather than one object because a large extraction may be delivered in parts.
    Splitting by *size* is fine; splitting by grain or merging two windows is not, and that
    prohibition lives in the landing contract rather than in a type.
    """

    governance: ProducerGovernance | None = None
    """Exact governed producer artifacts when Adlytics finalized a registration.

    Optional only for the exact-version trusted direct-manifest compatibility path. A
    Finalizer-generated AMC manifest always carries it in layout v2; historical governed layout-v1
    evidence remains readable because its manifest and data VersionIds are selected exactly.
    """

    layout_version: int = LEGACY_MANIFEST_LAYOUT
    """Raw-key layout version; v2 qualifies governed deliveries by query-pack identity."""

    def __post_init__(self) -> None:
        """Refuse a manifest that cannot identify or verify its own delivery."""
        for name in ("source_system", "source_dataset", "marketplace"):
            value = str(getattr(self, name))
            if not SEGMENT.match(value):
                raise LandingError(
                    f"{name} is {value!r}, which cannot be a path segment. It must be "
                    "letters, digits, dot, dash or underscore."
                )
        if not isinstance(cast("object", self.source_scope), SourceIdentifier):
            raise LandingError("source_scope must be a qualified SourceIdentifier.")
        if self.layout_version not in {LEGACY_MANIFEST_LAYOUT, GOVERNED_MANIFEST_LAYOUT}:
            raise LandingError(f"unsupported manifest layout version {self.layout_version}.")
        if self.layout_version == GOVERNED_MANIFEST_LAYOUT and self.governance is None:
            raise LandingError("governed manifest layout requires exact producer governance.")
        if not self.option_set_hash.strip():
            raise LandingError("option_set_hash is required.")
        expected = options_hash(dict(self.option_set))
        if self.option_set_hash != expected:
            raise LandingError(
                f"option_set_hash is {self.option_set_hash!r} but hashing option_set gives "
                f"{expected!r}. The two disagree, so which pull this is cannot be decided."
            )
        if self.period_end < self.period_start:
            raise LandingError(
                f"the window ends {self.period_end} before it begins {self.period_start}."
            )
        if self.retrieved_at.tzinfo is None:
            raise LandingError("retrieved_at must be timezone-aware.")
        if self.retrieved_at.astimezone(UTC).microsecond:
            raise LandingError(
                "retrieved_at must have whole-second precision because raw identity is "
                "second-qualified."
            )
        if not self.declared_columns:
            raise LandingError(
                "declared_columns is required and must be non-empty. A producer that cannot "
                "say what it wrote is a producer whose output cannot be validated."
            )
        if not self.objects:
            raise LandingError(
                "objects is required and must be non-empty. A manifest describing no data "
                "object is a completion marker for nothing."
            )

    @property
    def prefix(self) -> str:
        """Give the prefix this delivery belongs at, derived and never parsed back.

        Ordered general to specific, so every prefix is a meaningful narrower question:
        everything from this source, then this dataset, then this marketplace, then this
        window. The same ordering principle as `PartitionKey.identity`.

        `options=` sits above `retrieved=` deliberately. Two option sets for one window are
        different *datasets*; two retrievals of one option set are *versions of one thing*.
        Grouping the versions together is what makes a restatement legible when someone is
        looking at a listing rather than a table.
        """
        return manifest_prefix_for(
            ManifestPrefix(
                source_system=self.source_system,
                source_dataset=self.source_dataset,
                source_scope=self.source_scope,
                marketplace=self.marketplace,
                period_start=self.period_start,
                period_end=self.period_end,
                option_set_hash=self.option_set_hash,
                retrieved_at=self.retrieved_at,
                layout_version=self.layout_version,
                query_pack_id=(
                    self.governance.query_pack_id
                    if self.layout_version == GOVERNED_MANIFEST_LAYOUT
                    and self.governance is not None
                    else None
                ),
            )
        )

    @property
    def manifest_key(self) -> str:
        """Give the full key of this manifest, which is what an event reports."""
        return f"{self.prefix}{MANIFEST_NAME}"

    def object_keys(self) -> tuple[str, ...]:
        """Give the full key of every data object this manifest describes."""
        return tuple(f"{self.prefix}{landed.name}" for landed in self.objects)


def delivery_prefix_of(manifest_key: str) -> str:
    """Take the prefix a manifest sits at, without reading any meaning from it.

    Splitting a key at its last separator is not parsing: it makes no claim about what the
    segments mean, which is the line this module holds. What the delivery *is* comes from the
    manifest's own fields.
    """
    if not manifest_key.endswith(f"/{MANIFEST_NAME}"):
        raise LandingError(
            f"{manifest_key!r} is not a manifest key. A delivery's completion marker is "
            f"{MANIFEST_NAME} inside the delivery's own prefix."
        )
    return manifest_key[: -len(MANIFEST_NAME)]


def misplacement(manifest: Manifest, manifest_key: str) -> str | None:
    """Say why a manifest does not sit where it says it belongs, or None when it does.

    This is the check that makes the path worth having. The prefix is derived from the
    manifest, so agreement is free and disagreement is evidence: either the producer did not
    follow the layout, or the manifest describes a different delivery than the one it was
    written beside. Both are landings we must refuse rather than guess at.

    Returns a reason rather than raising, so a caller can report it alongside anything else
    wrong with the same landing instead of stopping at the first finding.
    """
    actual = delivery_prefix_of(manifest_key)
    expected = manifest.prefix
    if actual == expected:
        return None
    return (
        f"this manifest describes a delivery belonging at {expected!r} but was found at "
        f"{actual!r}, so where it sits and what it claims to be disagree."
    )


def partition_for(manifest: Manifest, contract: DataContract) -> PartitionKey:
    """Give the partition one contract's share of this delivery writes.

    The bridge between a landing and the ledger, and it is one function because the two
    vocabularies nearly but not quite match: a landing is keyed on the *dataset* while a
    partition is keyed on the *contract*, precisely so that one delivery read by two contracts
    produces two partitions. That is what lets one grain quarantine while the other loads.

    `report_options_hash` comes from the manifest's `option_set_hash` rather than from the
    mapped rows, so the ledger's idea of which pull this is agrees with the landing's by
    construction. Whether the *document* agrees with either is a separate question, and
    `ingestion.py` asserts it.
    """
    return PartitionKey(
        source_system=manifest.source_system,
        contract_id=contract.contract_id,
        report_options_hash=manifest.option_set_hash,
        marketplace_key=manifest.marketplace,
        period_start=manifest.period_start,
        period_end=manifest.period_end,
    )


@dataclass(frozen=True, slots=True)
class DeliveryContracts:
    """The contracts that read one landed delivery, and the format they agree it is.

    A result object rather than a bare tuple because the format is a second thing the caller
    needs and deriving it again at the call site is how two callers come to disagree.
    """

    manifest: Manifest
    contracts: tuple[DataContract, ...]
    source_format: SourceFormat


_LEGACY_DOCUMENT_FANOUTS: dict[tuple[str, str], frozenset[str]] = {
    ("vendor_central", "GET_VENDOR_SALES_REPORT"): frozenset(
        {"vendor-sales-by-asin", "vendor-sales-reported-total"}
    ),
    ("vendor_central", "GET_VENDOR_TRAFFIC_REPORT"): frozenset(
        {"vendor-traffic-by-asin", "vendor-traffic-reported-total"}
    ),
}


def require_installed_governance(
    manifest: Manifest,
    installed: tuple[ProducerGovernance, ...],
) -> None:
    """Require every governed manifest digest to match one installed immutable artifact set."""
    governance = manifest.governance
    if governance is None:
        return
    if governance not in installed:
        raise LandingError(
            "governed manifest artifacts differ from every installed query, contract, and "
            "source-pack binding."
        )


def contracts_for(manifest: Manifest, available: tuple[DataContract, ...]) -> DeliveryContracts:
    """Resolve the exact governed contract or one intentional legacy document fan-out.

    Governed producer manifests name the one contract whose query produced their output. Legacy
    manifests may still fan one document into several contracts only when every contract reads the
    same nested dual-array shape, as Vendor Central reports do. A flat ungoverned delivery matching
    several contracts is ambiguous and is refused rather than interpreted as every query shape over
    the same source table.
    """
    matched = tuple(
        contract
        for contract in available
        if contract.source_system == manifest.source_system
        and contract.source_dataset == manifest.source_dataset
    )
    if not matched:
        raise LandingError(
            f"no contract reads {manifest.source_dataset!r} from {manifest.source_system!r}. "
            "Either the extraction delivered a dataset this deployment does not model, or a "
            "contract names the dataset differently than the manifest does."
        )

    governance = manifest.governance
    if governance is None and manifest.source_system == "amc":
        raise LandingError(
            "AMC delivery requires exact producer governance before its query shape can be read."
        )
    if governance is not None:
        if governance.source_pack_id != governance.data_contract_id:
            raise LandingError(
                "governed manifest source pack and data contract identities disagree."
            )
        governed = tuple(
            contract for contract in matched if contract.contract_id == governance.data_contract_id
        )
        if len(governed) != 1:
            raise LandingError(
                f"governed contract {governance.data_contract_id!r} does not uniquely read "
                f"{manifest.source_dataset!r} from {manifest.source_system!r}."
            )
        matched = governed

    formats = {contract.source_format for contract in matched}
    if len(formats) != 1:
        spelled = ", ".join(sorted(fmt.value for fmt in formats))
        raise LandingError(
            f"{manifest.source_dataset}: its contracts declare {len(formats)} different "
            f"source formats ({spelled}), so one delivered object would have to be parsed "
            "two ways."
        )
    source_format = next(iter(formats))
    if governance is None and len(matched) > 1:
        expected = _LEGACY_DOCUMENT_FANOUTS.get((manifest.source_system, manifest.source_dataset))
        actual = frozenset(contract.contract_id for contract in matched)
        if source_format.value != "json_dual_array" or expected is None or actual != expected:
            raise LandingError(
                f"{manifest.source_dataset}: an ungoverned delivery matches several contracts "
                "outside an exact installed legacy document fan-out; exact producer governance "
                "is required to select its query shape."
            )

    return DeliveryContracts(
        manifest=manifest,
        contracts=matched,
        source_format=source_format,
    )


def dataset_prefix(contract: DataContract) -> str:
    """Return the non-empty raw-zone scope owned by one source contract.

    Reprocessing and administrative planning share this pure derivation so neither discovers raw
    data from an account-wide listing or drifts from the landing contract.
    """
    prefix = f"{contract.source_system}/{contract.source_dataset}/"
    if prefix.startswith("/") or prefix == "/" or ".." in prefix:
        raise LandingError(f"{contract.contract_id}: derived an unsafe source dataset prefix.")
    return prefix
