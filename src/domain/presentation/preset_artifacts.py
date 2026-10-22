# SPDX-License-Identifier: MIT-0
"""Capability-free immutable preset-artifact values and pointer transitions."""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, cast

from domain.shared_kernel.errors import ErrorCode, InvariantError
from domain.shared_kernel.values import DateRange

__all__ = [
    "NO_SIBLINGS",
    "PRESET_BODY_CEILING",
    "RETAIL_BODY_CEILING",
    "SIBLING_DESCRIPTOR_MAX_BYTES",
    "SIBLING_INDEX_KEY",
    "SIBLING_INDEX_SHARD_ENTRIES",
    "SIBLING_INDEX_SHARD_KIND",
    "ArtifactBodyWriteResult",
    "CustodyManifestWriteResult",
    "PresetArtifactBody",
    "PresetArtifactBodyCeilingError",
    "PresetArtifactCustodyHeader",
    "PresetArtifactCustodyManifest",
    "PresetArtifactFilterIdentity",
    "PresetArtifactIndexShardIdentity",
    "PresetArtifactPage",
    "PresetArtifactPointer",
    "PresetArtifactPointerAdvanceOutcome",
    "PresetArtifactPointerAdvanceRequest",
    "PresetArtifactPointerAdvanceResult",
    "PresetArtifactPointerRosterRequest",
    "PresetArtifactPointerRosterResult",
    "PresetArtifactPointerTemplate",
    "PresetArtifactProductChildIdentity",
    "PresetArtifactPublicationRequest",
    "PresetArtifactPublicationResult",
    "PresetArtifactRevision",
    "PresetArtifactSiblingBody",
    "PresetArtifactSiblingCustody",
    "PresetArtifactSiblingCustodyWriteResult",
    "PresetArtifactSiblingDescriptor",
    "PresetArtifactSiblingIdentity",
    "PresetArtifactSiblingIndexEntry",
    "PresetArtifactSiblingPair",
    "PresetArtifactSiblingReadRequest",
    "PresetArtifactSiblingStream",
    "PresetArtifactSiblingWriteResult",
    "PresetArtifactStageResult",
    "PresetArtifactVariant",
    "SourceUnchanged",
    "canonical_preset_artifact_json",
    "child_identity_from_path",
    "classify_conditional_pointer_failure",
    "covering_index_shard",
    "index_shard_descriptors",
    "parent_custody_manifest",
    "pointer_written_at",
    "sibling_descriptor_bytes",
    "sibling_index_shards",
    "split_parent_custody",
]

_PAGE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,79}$")
_VARIANT_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
"""A declared preset id is the variant; calendar preset ids carry underscores (ADR-100)."""
_REVISION = re.compile(r"^[a-f0-9]{64}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_ORDERING_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,79}$")
_PRODUCT_REF = re.compile(r"^[a-f0-9]{32}$")
_FILTER_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_PRESET_SIBLING_SEGMENT_INDEX = 10_000
MAX_PRESET_ARTIFACT_BYTES = 256_000
MAX_PRESET_POINTER_ROSTER = 4
PRESET_ARTIFACT_POINTER_CONFLICT = ErrorCode("PRESET_ARTIFACT", 1)
RETAIL_BODY_CEILING = "retail_body_ceiling"
"""D18: a Retail Health parent body or custody manifest over the byte cap."""
PRESET_BODY_CEILING = "preset_body_ceiling"
"""D18 for any other page: the same typed final failure, named for its page family."""
_RETAIL_HEALTH_PAGE = "retail-health"
_WRITTEN_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_WRITTEN_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class PresetArtifactBodyCeilingError(ValueError):
    """D18: a parent body or custody manifest is over `MAX_PRESET_ARTIFACT_BYTES`.

    Typed and non-retryable: the same sources render the same bytes, so a retry cannot succeed.
    It is raised while the body is constructed, which every source does before its first S3
    write, so an over-cap body never reaches persistence. A GRAPH bake task records it as a
    final task failure; no content is dropped.
    """

    def __init__(self, reason: str, observed_bytes: int) -> None:
        super().__init__(
            f"preset artifact {reason}: {observed_bytes} bytes exceed the "
            f"{MAX_PRESET_ARTIFACT_BYTES}-byte cap."
        )
        self.reason = reason
        self.observed_bytes = observed_bytes


def _require_within_ceiling(page: PresetArtifactPage, payload_json: str) -> None:
    observed = len(payload_json.encode())
    if observed > MAX_PRESET_ARTIFACT_BYTES:
        reason = RETAIL_BODY_CEILING if page.value == _RETAIL_HEALTH_PAGE else PRESET_BODY_CEILING
        raise PresetArtifactBodyCeilingError(reason, observed)


def pointer_written_at(instant: datetime) -> str:
    """Render a graph bake's write instant as stored (`YYYY-MM-DDTHH:MM:SSZ`, UTC, truncated)."""
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("a pointer write instant must be timezone-aware.")
    return instant.astimezone(UTC).strftime(_WRITTEN_AT_FORMAT)


def canonical_preset_artifact_json(value: dict[str, object]) -> str:
    """Render the one stable JSON representation allowed for immutable bodies."""
    if not value:
        raise ValueError("preset artifact payload cannot be empty.")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True, slots=True)
class PresetArtifactPage:
    """One declared page namespace shared by all of its preset artifacts."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.value), str) or not _PAGE_IDENTIFIER.fullmatch(
            self.value
        ):
            raise ValueError("preset artifact page must be a lowercase identifier.")


@dataclass(frozen=True, slots=True)
class PresetArtifactVariant:
    """One declared, bakeable window variant within a page namespace."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.value), str) or not _VARIANT_IDENTIFIER.fullmatch(
            self.value
        ):
            raise ValueError("preset artifact variant must be a lowercase identifier.")


@dataclass(frozen=True, slots=True)
class PresetArtifactRevision:
    """Stable revision derived solely from the exact publication identities used."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.value), str) or not _REVISION.fullmatch(self.value):
            raise ValueError("preset artifact revision must be a lowercase SHA-256 digest.")

    @classmethod
    def from_publication_identities(
        cls, publication_identities: tuple[str, ...], *, render_format: int = 1
    ) -> PresetArtifactRevision:
        """Derive an immutable identity from exact source publications and the render format.

        The render format names the page source's rendering generation: identical sources
        rendered by a corrected renderer must supersede, not collide with, the immutable body
        the wrong renderer produced (ADR-070, 2026-09-16 render-format amendment).
        """
        if not publication_identities or any(
            not identity.strip() for identity in publication_identities
        ):
            raise ValueError("preset artifact revision requires nonblank publication identities.")
        if len(set(publication_identities)) != len(publication_identities):
            raise ValueError("preset artifact publication identities cannot repeat.")
        if render_format < 1:
            raise ValueError("preset artifact render format must be a positive generation.")
        evidence = json.dumps(
            {"publications": sorted(publication_identities), "render_format": render_format},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return cls(hashlib.sha256(evidence).hexdigest())


@dataclass(frozen=True, slots=True)
class PresetArtifactBody:
    """Canonical, capability-free JSON rendered for one immutable revision."""

    page: PresetArtifactPage
    variant: PresetArtifactVariant
    revision: PresetArtifactRevision
    payload_json: str

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.page), PresetArtifactPage):
            raise TypeError("preset artifact body requires a PresetArtifactPage.")
        if not isinstance(cast("object", self.variant), PresetArtifactVariant):
            raise TypeError("preset artifact body requires a PresetArtifactVariant.")
        if not isinstance(cast("object", self.revision), PresetArtifactRevision):
            raise TypeError("preset artifact body requires a PresetArtifactRevision.")
        if not isinstance(cast("object", self.payload_json), str) or not self.payload_json:
            raise ValueError("preset artifact body cannot be blank.")
        _require_within_ceiling(self.page, self.payload_json)
        try:
            payload = json.loads(self.payload_json)
        except ValueError:
            raise ValueError("preset artifact body is not JSON.") from None
        if (
            not isinstance(payload, dict)
            or canonical_preset_artifact_json(cast("dict[str, object]", payload))
            != self.payload_json
        ):
            raise ValueError("preset artifact body must be a canonical JSON object.")

    @property
    def key(self) -> str:
        """Return the revision-keyed immutable S3 object key."""
        return f"pages/{self.page.value}/{self.variant.value}/{self.revision.value}.json"

    @property
    def digest(self) -> str:
        """Return the checksum that binds a pointer to the persisted body bytes."""
        return hashlib.sha256(self.payload_json.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class PresetArtifactCustodyManifest:
    """Private canonical JSON custody companion never referenced by the public payload.

    It shares the exact page/variant/revision identity of one public body while living in
    a distinct key namespace, so serve-time capability minting can recover canonical
    selectors without the browser-visible artifact ever carrying them.
    """

    page: PresetArtifactPage
    variant: PresetArtifactVariant
    revision: PresetArtifactRevision
    payload_json: str

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.page), PresetArtifactPage):
            raise TypeError("preset artifact custody manifest requires a PresetArtifactPage.")
        if not isinstance(cast("object", self.variant), PresetArtifactVariant):
            raise TypeError("preset artifact custody manifest requires a PresetArtifactVariant.")
        if not isinstance(cast("object", self.revision), PresetArtifactRevision):
            raise TypeError("preset artifact custody manifest requires a PresetArtifactRevision.")
        if not isinstance(cast("object", self.payload_json), str) or not self.payload_json:
            raise ValueError("preset artifact custody manifest cannot be blank.")
        _require_within_ceiling(self.page, self.payload_json)
        try:
            payload = json.loads(self.payload_json)
        except ValueError:
            raise ValueError("preset artifact custody manifest is not JSON.") from None
        if (
            not isinstance(payload, dict)
            or canonical_preset_artifact_json(cast("dict[str, object]", payload))
            != self.payload_json
        ):
            raise ValueError("preset artifact custody manifest must be a canonical JSON object.")

    @property
    def key(self) -> str:
        """Return the revision-keyed immutable custody object key."""
        return f"custody/{self.page.value}/{self.variant.value}/{self.revision.value}.json"

    @property
    def digest(self) -> str:
        """Return the checksum that binds a pointer to the persisted custody bytes."""
        return hashlib.sha256(self.payload_json.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class PresetArtifactSiblingIdentity:
    """One ordered immutable sibling beneath a parent revision.

    ``filter_id`` names the channel filter sibling the segment belongs to (ADR-127 decision 2),
    ``all`` for All channels. It is ``None`` only on a segment baked before render format 11,
    whose key has no filter dimension.
    """

    ordering: str
    segment_index: int
    filter_id: str | None = None

    def __post_init__(self) -> None:
        if not _ORDERING_IDENTIFIER.fullmatch(self.ordering):
            raise ValueError("preset artifact sibling ordering is invalid.")
        if not 0 <= self.segment_index <= MAX_PRESET_SIBLING_SEGMENT_INDEX:
            raise ValueError("preset artifact sibling segment index is invalid.")
        if self.filter_id is not None and not _FILTER_IDENTIFIER.fullmatch(self.filter_id):
            raise ValueError("preset artifact sibling filter is invalid.")

    @property
    def value(self) -> str:
        """Stable path suffix for this filter, ordering and segment."""
        segment = f"{self.ordering}/segment-{self.segment_index:04d}"
        return segment if self.filter_id is None else f"{self.filter_id}/{segment}"

    @property
    def path(self) -> str:
        """Key suffix beneath the parent revision."""
        return f"segments/{self.value}"


@dataclass(frozen=True, slots=True)
class PresetArtifactProductChildIdentity:
    """One baked product-detail child beneath a parent revision (ADR-125 decision 2).

    ``product_ref`` is a server-only digest of the canonical product key; it names the object
    key and the parent custody's closed child set, and never reaches a browser.
    """

    product_ref: str

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.product_ref), str) or not _PRODUCT_REF.fullmatch(
            self.product_ref
        ):
            raise ValueError("preset artifact product child reference is invalid.")

    @property
    def path(self) -> str:
        """Key suffix beneath the parent revision."""
        return f"products/{self.product_ref}"


@dataclass(frozen=True, slots=True)
class PresetArtifactFilterIdentity:
    """One channel filter sibling's parent body beneath the All-channels revision (ADR-127).

    Every filter of a preset is rendered from one measurement batch in one bake, so it shares
    the pointer's revision; All-channels custody binds each one by digest.
    """

    filter_id: str

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.filter_id), str) or not (
            _FILTER_IDENTIFIER.fullmatch(self.filter_id)
        ):
            raise ValueError("preset artifact filter sibling id is invalid.")

    @property
    def path(self) -> str:
        """Key suffix beneath the parent revision."""
        return f"filters/{self.filter_id}"


@dataclass(frozen=True, slots=True)
class PresetArtifactIndexShardIdentity:
    """One shard of a revision's sibling digest index (ADR-145): custody-only, no public body.

    Its key is `custody/{page}/{variant}/{revision}/index/segment-NNNN.json`; the parent custody
    binds it by digest, and it binds up to `SIBLING_INDEX_SHARD_ENTRIES` sibling descriptors.
    """

    shard_index: int

    def __post_init__(self) -> None:
        if (
            isinstance(cast("object", self.shard_index), bool)
            or not isinstance(cast("object", self.shard_index), int)
            or not 0 <= self.shard_index <= MAX_PRESET_SIBLING_SEGMENT_INDEX
        ):
            raise ValueError("preset artifact sibling index shard is invalid.")

    @property
    def path(self) -> str:
        """Key suffix beneath the parent revision's custody."""
        return f"index/segment-{self.shard_index:04d}"


type PresetArtifactChildIdentity = (
    PresetArtifactSiblingIdentity
    | PresetArtifactProductChildIdentity
    | PresetArtifactFilterIdentity
)
"""An ordered segment, a product-detail child or a filter sibling; each binds through custody."""
type PresetArtifactCustodyIdentity = PresetArtifactChildIdentity | PresetArtifactIndexShardIdentity
"""Any custody-namespace object beneath a revision: a child's custody or an index shard."""
_CHILD_IDENTITIES = (
    PresetArtifactSiblingIdentity,
    PresetArtifactProductChildIdentity,
    PresetArtifactFilterIdentity,
)
_CUSTODY_IDENTITIES = (*_CHILD_IDENTITIES, PresetArtifactIndexShardIdentity)
_SEGMENT_PART = re.compile(r"^segment-(\d{4,5})$")


def child_identity_from_path(path: str) -> PresetArtifactChildIdentity:
    """Decode one child's `path` exactly as `identity.path` renders it, or raise `ValueError`."""
    parts = path.split("/") if isinstance(cast("object", path), str) else []
    identity: PresetArtifactChildIdentity
    match parts:
        case ["segments", ordering, segment] if matched := _SEGMENT_PART.fullmatch(segment):
            identity = PresetArtifactSiblingIdentity(ordering, int(matched.group(1)))
        case ["segments", filter_id, ordering, segment] if matched := _SEGMENT_PART.fullmatch(
            segment
        ):
            identity = PresetArtifactSiblingIdentity(ordering, int(matched.group(1)), filter_id)
        case ["products", ref]:
            identity = PresetArtifactProductChildIdentity(ref)
        case ["filters", filter_id]:
            identity = PresetArtifactFilterIdentity(filter_id)
        case _:
            raise ValueError("preset artifact child path is invalid.")
    if identity.path != path:
        raise ValueError("preset artifact child path is not canonical.")
    return identity


@dataclass(frozen=True, slots=True)
class PresetArtifactSiblingBody:
    """Canonical public JSON for one non-parent ordered segment."""

    page: PresetArtifactPage
    variant: PresetArtifactVariant
    revision: PresetArtifactRevision
    identity: PresetArtifactChildIdentity
    payload_json: str

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.identity), _CHILD_IDENTITIES):
            raise TypeError("preset artifact sibling body requires a sibling identity.")
        if len(self.payload_json.encode()) > MAX_PRESET_ARTIFACT_BYTES:
            raise ValueError("preset artifact sibling body exceeds the safe byte limit.")
        try:
            payload = json.loads(self.payload_json)
        except ValueError:
            raise ValueError("preset artifact sibling body is not JSON.") from None
        if (
            not isinstance(payload, dict)
            or canonical_preset_artifact_json(cast("dict[str, object]", payload))
            != self.payload_json
        ):
            raise ValueError("preset artifact sibling body must be a canonical JSON object.")

    @property
    def key(self) -> str:
        """Immutable revision-qualified public sibling key."""
        return (
            f"pages/{self.page.value}/{self.variant.value}/{self.revision.value}/"
            f"{self.identity.path}.json"
        )

    @property
    def digest(self) -> str:
        """SHA-256 digest binding the sibling's exact public bytes."""
        return hashlib.sha256(self.payload_json.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class PresetArtifactSiblingCustody:
    """Private custody JSON paired one-to-one with an ordered public sibling."""

    page: PresetArtifactPage
    variant: PresetArtifactVariant
    revision: PresetArtifactRevision
    identity: PresetArtifactCustodyIdentity
    payload_json: str

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.identity), _CUSTODY_IDENTITIES):
            raise TypeError("preset artifact sibling custody requires a sibling identity.")
        if len(self.payload_json.encode()) > MAX_PRESET_ARTIFACT_BYTES:
            raise ValueError("preset artifact sibling custody exceeds the safe byte limit.")
        try:
            payload = json.loads(self.payload_json)
        except ValueError:
            raise ValueError("preset artifact sibling custody is not JSON.") from None
        if (
            not isinstance(payload, dict)
            or canonical_preset_artifact_json(cast("dict[str, object]", payload))
            != self.payload_json
        ):
            raise ValueError("preset artifact sibling custody must be a canonical JSON object.")

    @property
    def key(self) -> str:
        """Immutable revision-qualified private sibling key."""
        return (
            f"custody/{self.page.value}/{self.variant.value}/{self.revision.value}/"
            f"{self.identity.path}.json"
        )

    @property
    def digest(self) -> str:
        """SHA-256 digest binding the sibling's exact private bytes."""
        return hashlib.sha256(self.payload_json.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class PresetArtifactSiblingDescriptor:
    """Index binding for one public/custody sibling pair, or for one custody-only index shard.

    `body_digest` is None exactly for an index shard, which has no public body.
    """

    identity: PresetArtifactCustodyIdentity
    body_digest: str | None
    custody_digest: str

    def __post_init__(self) -> None:
        shard = isinstance(cast("object", self.identity), PresetArtifactIndexShardIdentity)
        if not isinstance(cast("object", self.identity), _CUSTODY_IDENTITIES):
            raise TypeError("preset artifact sibling descriptor requires a sibling identity.")
        if (self.body_digest is None) != shard or not _DIGEST.fullmatch(self.custody_digest):
            raise ValueError("preset artifact sibling descriptor digests are invalid.")
        if self.body_digest is not None and not _DIGEST.fullmatch(self.body_digest):
            raise ValueError("preset artifact sibling descriptor digests are invalid.")


@dataclass(frozen=True, slots=True)
class PresetArtifactSiblingReadRequest:
    """Exact parent-attested sibling identity and bytes required by one read."""

    page: PresetArtifactPage
    variant: PresetArtifactVariant
    revision: PresetArtifactRevision
    descriptor: PresetArtifactSiblingDescriptor


@dataclass(frozen=True, slots=True)
class PresetArtifactSiblingWriteResult:
    """Attested result of writing one public sibling body."""

    page: PresetArtifactPage
    variant: PresetArtifactVariant
    revision: PresetArtifactRevision
    identity: PresetArtifactChildIdentity
    key: str
    digest: str
    version_id: str | None


@dataclass(frozen=True, slots=True)
class PresetArtifactSiblingCustodyWriteResult:
    """Attested result of writing one private sibling custody body or index shard."""

    page: PresetArtifactPage
    variant: PresetArtifactVariant
    revision: PresetArtifactRevision
    identity: PresetArtifactCustodyIdentity
    key: str
    digest: str
    version_id: str | None


@dataclass(frozen=True, slots=True)
class CustodyManifestWriteResult:
    """Attested result of a create-or-verify immutable custody manifest write."""

    page: PresetArtifactPage
    variant: PresetArtifactVariant
    revision: PresetArtifactRevision
    key: str
    digest: str
    version_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.page), PresetArtifactPage):
            raise TypeError("preset artifact custody result requires a PresetArtifactPage.")
        if not isinstance(cast("object", self.variant), PresetArtifactVariant):
            raise TypeError("preset artifact custody result requires a PresetArtifactVariant.")
        if not isinstance(cast("object", self.revision), PresetArtifactRevision):
            raise TypeError("preset artifact custody result requires a PresetArtifactRevision.")
        expected_key = f"custody/{self.page.value}/{self.variant.value}/{self.revision.value}.json"
        if self.key != expected_key:
            raise ValueError(
                "preset artifact custody result key does not match its immutable identity."
            )
        if not _DIGEST.fullmatch(self.digest):
            raise ValueError("preset artifact custody result digest is invalid.")
        if self.version_id is not None and not self.version_id:
            raise ValueError(
                "preset artifact custody result version must be nonblank when supplied."
            )


@dataclass(frozen=True, slots=True)
class ArtifactBodyWriteResult:
    """Attested result of a create-or-verify immutable body write."""

    page: PresetArtifactPage
    variant: PresetArtifactVariant
    revision: PresetArtifactRevision
    key: str
    digest: str
    version_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.page), PresetArtifactPage):
            raise TypeError("preset artifact body result requires a PresetArtifactPage.")
        if not isinstance(cast("object", self.variant), PresetArtifactVariant):
            raise TypeError("preset artifact body result requires a PresetArtifactVariant.")
        if not isinstance(cast("object", self.revision), PresetArtifactRevision):
            raise TypeError("preset artifact body result requires a PresetArtifactRevision.")
        expected_key = f"pages/{self.page.value}/{self.variant.value}/{self.revision.value}.json"
        if self.key != expected_key:
            raise ValueError(
                "preset artifact body result key does not match its immutable identity."
            )
        if not _DIGEST.fullmatch(self.digest):
            raise ValueError("preset artifact body result digest is invalid.")
        if self.version_id is not None and not self.version_id:
            raise ValueError("preset artifact body result version must be nonblank when supplied.")


@dataclass(frozen=True, slots=True)
class PresetArtifactPointer:
    """A capability-free page/variant pointer to one immutable artifact body."""

    page: PresetArtifactPage
    variant: PresetArtifactVariant
    revision: PresetArtifactRevision
    body_digest: str
    forward_token: int
    supersedes: PresetArtifactRevision | None = None
    custody_digest: str | None = None
    grant_id: str | None = None
    """S6 §2.8 (D28): the grant whose graph bake wrote this pointer; None on a legacy pointer."""
    serving_generation: str | None = None
    """The grant's serving generation; set exactly when `grant_id` is."""
    written_at: str | None = None
    """The graph bake's UTC write instant, RFC 3339 whole seconds; set exactly with `grant_id`."""
    resolved_window: DateRange | None = None
    """The window the body serves (ADR-116 decision 4), capability-free, so a drill link names
    exactly what this preset serves now. None on a pointer written before pointers carried it:
    a reader treats that as "no dates", never as an error."""
    comparison_window: DateRange | None = None
    """The equal prior the body compares with, or None when it compares nothing."""

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.page), PresetArtifactPage):
            raise TypeError("preset artifact pointer requires a PresetArtifactPage.")
        self._require_grant_fields()
        for window in (self.resolved_window, self.comparison_window):
            if window is not None and not isinstance(cast("object", window), DateRange):
                raise TypeError("preset artifact pointer windows must be date ranges.")
        if self.comparison_window is not None and (
            self.resolved_window is None or self.comparison_window.end >= self.resolved_window.start
        ):
            raise ValueError("a pointer's comparison window ends before its resolved window.")
        if not isinstance(cast("object", self.variant), PresetArtifactVariant):
            raise TypeError("preset artifact pointer requires a PresetArtifactVariant.")
        if not isinstance(cast("object", self.revision), PresetArtifactRevision):
            raise TypeError("preset artifact pointer requires a PresetArtifactRevision.")
        if not _DIGEST.fullmatch(self.body_digest):
            raise ValueError("preset artifact pointer body digest is invalid.")
        if self.custody_digest is not None and not _DIGEST.fullmatch(self.custody_digest):
            raise ValueError("preset artifact pointer custody digest is invalid.")
        if self.forward_token < 1:
            raise ValueError("preset artifact pointer forward token must be positive.")
        if self.supersedes is not None:
            if not isinstance(cast("object", self.supersedes), PresetArtifactRevision):
                raise TypeError("preset artifact pointer predecessor must be a revision.")
            if self.supersedes == self.revision:
                raise ValueError("preset artifact pointer cannot supersede its own revision.")

    def _require_grant_fields(self) -> None:
        """D28(a): only a grant-bearing pointer carries the graph fields, and it carries all three.

        A legacy (grant-less) pointer carries none, so its stored item stays exactly today's
        envelope, which the deployed reader rebuilds and compares.
        """
        fields = (self.grant_id, self.serving_generation, self.written_at)
        if self.grant_id is None:
            if any(value is not None for value in fields):
                raise ValueError("a grant-less preset artifact pointer carries no graph fields.")
            return
        if self.serving_generation is None or self.written_at is None:
            raise ValueError(
                "a grant-bearing preset artifact pointer carries its serving generation "
                "and its write instant."
            )
        if not _DIGEST.fullmatch(self.grant_id) or not _DIGEST.fullmatch(self.serving_generation):
            raise ValueError("preset artifact pointer grant fields must be SHA-256 digests.")
        if not _WRITTEN_AT.fullmatch(self.written_at):
            raise ValueError("preset artifact pointer write instant is not RFC 3339 whole seconds.")
        try:
            datetime.strptime(self.written_at, _WRITTEN_AT_FORMAT)  # noqa: DTZ007 -- Z-suffixed
        except ValueError:
            raise ValueError("preset artifact pointer write instant is not an instant.") from None

    @property
    def partition_key(self) -> str:
        """Return the shared DynamoDB partition key for the page/variant pair."""
        return f"{self.page.value}#{self.variant.value}"

    @property
    def body_key(self) -> str:
        """Return the only S3 object key allowed for this immutable pointer."""
        return f"pages/{self.page.value}/{self.variant.value}/{self.revision.value}.json"

    @property
    def custody_key(self) -> str:
        """Return the only custody object key allowed for this immutable pointer."""
        return f"custody/{self.page.value}/{self.variant.value}/{self.revision.value}.json"


@dataclass(frozen=True, slots=True)
class PresetArtifactPointerRosterRequest:
    """One bounded, ordered strong read of declared CURRENT preset pointers."""

    page: PresetArtifactPage
    variants: tuple[PresetArtifactVariant, ...]

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.page), PresetArtifactPage):
            raise TypeError("preset artifact pointer roster requires a page.")
        if (
            not self.variants
            or len(self.variants) > MAX_PRESET_POINTER_ROSTER
            or len(set(self.variants)) != len(self.variants)
        ):
            raise ValueError("preset artifact pointer roster variants are invalid.")


@dataclass(frozen=True, slots=True)
class PresetArtifactPointerRosterResult:
    """Pointers and stable absences in the request's exact declared order."""

    request: PresetArtifactPointerRosterRequest
    pointers: tuple[PresetArtifactPointer | None, ...]

    def __post_init__(self) -> None:
        if len(self.pointers) != len(self.request.variants):
            raise ValueError("preset artifact pointer roster result is incomplete.")
        for variant, pointer in zip(self.request.variants, self.pointers, strict=True):
            if pointer is not None and (
                pointer.page != self.request.page or pointer.variant != variant
            ):
                raise ValueError("preset artifact pointer roster result is misordered.")

    def pointer_for(self, variant: PresetArtifactVariant) -> PresetArtifactPointer | None:
        """Return the pointer or stable absence for one requested variant."""
        try:
            index = self.request.variants.index(variant)
        except ValueError:
            raise ValueError("preset artifact variant was not requested in this roster.") from None
        return self.pointers[index]


@dataclass(frozen=True, slots=True)
class PresetArtifactPointerAdvanceRequest:
    """Forward-only pointer transition after its body has been persisted."""

    pointer: PresetArtifactPointer
    superseded_history_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.pointer), PresetArtifactPointer):
            raise TypeError("preset artifact pointer advance requires a pointer.")
        if self.pointer.supersedes is None:
            if self.superseded_history_expires_at is not None:
                raise ValueError("an initial pointer cannot set a superseded history expiry.")
            return
        if self.superseded_history_expires_at is None:
            raise ValueError("a superseding pointer requires a history retention expiry.")
        if (
            self.superseded_history_expires_at.tzinfo is None
            or self.superseded_history_expires_at.utcoffset() is None
        ):
            raise ValueError("preset artifact history retention expiry must be timezone-aware.")


class PresetArtifactPointerAdvanceOutcome(StrEnum):
    """The only outcomes permitted by one conditional pointer transition."""

    ADVANCED = "advanced"
    NO_OP = "no_op"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class PresetArtifactPointerAdvanceResult:
    """Typed outcome of a conditional forward-only pointer write."""

    outcome: PresetArtifactPointerAdvanceOutcome


@dataclass(frozen=True, slots=True)
class PresetArtifactPointerTemplate:
    """Every field of one pointer advance except `custody_digest`, which the stage fills in.

    A source can't know its parent custody's digest: the stage builds that custody from the
    source's header and the sibling index it writes (ADR-145). `complete` returns the advance.
    """

    page: PresetArtifactPage
    variant: PresetArtifactVariant
    revision: PresetArtifactRevision
    body_digest: str
    forward_token: int
    supersedes: PresetArtifactRevision | None = None
    superseded_history_expires_at: datetime | None = None
    grant_id: str | None = None
    serving_generation: str | None = None
    written_at: str | None = None
    resolved_window: DateRange | None = None
    comparison_window: DateRange | None = None

    def __post_init__(self) -> None:
        # Every field is held to the pointer's and the advance's own rules now, not at the stage.
        self.complete(None)

    def complete(self, custody_digest: str | None) -> PresetArtifactPointerAdvanceRequest:
        """Return the pointer advance this template names, with the staged custody's digest."""
        return PresetArtifactPointerAdvanceRequest(
            PresetArtifactPointer(
                self.page,
                self.variant,
                self.revision,
                self.body_digest,
                self.forward_token,
                self.supersedes,
                custody_digest,
                self.grant_id,
                self.serving_generation,
                self.written_at,
                self.resolved_window,
                self.comparison_window,
            ),
            self.superseded_history_expires_at,
        )

    @property
    def partition_key(self) -> str:
        """Return the shared DynamoDB partition key for the page/variant pair."""
        return f"{self.page.value}#{self.variant.value}"


type PresetArtifactSiblingPair = tuple[PresetArtifactSiblingBody, PresetArtifactSiblingCustody]
"""One sibling's public body and its private custody, written body first."""


@dataclass(frozen=True, slots=True)
class _HeldSiblingPairs:
    """A finite tuple of pairs as a stream factory (`PresetArtifactSiblingStream.of`)."""

    pairs: tuple[PresetArtifactSiblingPair, ...]

    def __call__(self) -> Iterator[PresetArtifactSiblingPair]:
        return iter(self.pairs)


@dataclass(frozen=True, slots=True)
class PresetArtifactSiblingStream:
    """A re-iterable factory of one revision's sibling pairs, and their declared count (ADR-145).

    Each iteration calls `pairs()` afresh, so a source can render its siblings lazily. The stage
    consumes the stream once and keeps only each pair's descriptor, so a bake holds the pairs in
    flight, never every body. A stream that yields a different number of pairs than `count`,
    a repeated identity, or a pair of another revision is an invariant the stage refuses.
    """

    pairs: Callable[[], Iterator[PresetArtifactSiblingPair]]
    count: int

    def __post_init__(self) -> None:
        if not callable(self.pairs):
            raise TypeError("a preset artifact sibling stream requires a pair factory.")
        if (
            isinstance(cast("object", self.count), bool)
            or not isinstance(cast("object", self.count), int)
            or self.count < 0
        ):
            raise ValueError("a preset artifact sibling stream count is a nonnegative integer.")

    @classmethod
    def of(cls, pairs: Iterable[PresetArtifactSiblingPair]) -> PresetArtifactSiblingStream:
        """Wrap a finite set of pairs, for a source with few siblings (Retail's children)."""
        held = tuple(pairs)
        for pair in held:
            if (
                not isinstance(cast("object", pair), tuple)
                or len(pair) != 2  # noqa: PLR2004 -- a body and its custody
                or not isinstance(cast("object", pair[0]), PresetArtifactSiblingBody)
                or not isinstance(cast("object", pair[1]), PresetArtifactSiblingCustody)
            ):
                raise TypeError("a preset artifact sibling pair is a body and its custody.")
        return cls(_HeldSiblingPairs(held), len(held))

    def __iter__(self) -> Iterator[PresetArtifactSiblingPair]:
        return iter(self.pairs())


NO_SIBLINGS: Final = PresetArtifactSiblingStream.of(())
"""The stream of a publication with no siblings."""


@dataclass(frozen=True, slots=True)
class PresetArtifactCustodyHeader:
    """A page's own parent-custody fields: canonical JSON with no `sibling_index` key.

    The stage builds the parent custody from it and the sibling index it writes (ADR-145).
    """

    payload_json: str

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.payload_json), str) or not self.payload_json:
            raise ValueError("preset artifact custody header cannot be blank.")
        try:
            payload = json.loads(self.payload_json)
        except ValueError:
            raise ValueError("preset artifact custody header is not JSON.") from None
        if (
            not isinstance(payload, dict)
            or canonical_preset_artifact_json(cast("dict[str, object]", payload))
            != self.payload_json
        ):
            raise ValueError("preset artifact custody header must be a canonical JSON object.")
        if SIBLING_INDEX_KEY in payload:
            raise ValueError("a custody header never carries the stage's sibling index.")


@dataclass(frozen=True, slots=True)
class PresetArtifactPublicationRequest:
    """One body-first persistence request for a rendered preset artifact (ADR-145).

    The stage writes the sibling stream, then the sibling index, then the parent body and the
    parent custody it builds from `custody_header` and the index; the pointer, completed from
    the template with that custody's digest, advances last. A publication with no header has no
    custody, and so no siblings.
    """

    body: PresetArtifactBody
    pointer_template: PresetArtifactPointerTemplate
    custody_header: PresetArtifactCustodyHeader | None = None
    siblings: PresetArtifactSiblingStream = NO_SIBLINGS
    closed_children: frozenset[PresetArtifactProductChildIdentity] | None = None
    """When set, the stream's identities are exactly this set (Retail's served rows), checked by
    the stage before any write."""

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.body), PresetArtifactBody):
            raise TypeError("preset artifact publication requires a body.")
        if not isinstance(cast("object", self.pointer_template), PresetArtifactPointerTemplate):
            raise TypeError("preset artifact publication requires a pointer template.")
        if not isinstance(cast("object", self.siblings), PresetArtifactSiblingStream):
            raise TypeError("preset artifact publication siblings must be a sibling stream.")
        template = self.pointer_template
        if (self.body.page, self.body.variant, self.body.revision, self.body.digest) != (
            template.page,
            template.variant,
            template.revision,
            template.body_digest,
        ):
            raise ValueError(
                "preset artifact pointer must exactly attest its body identity and bytes."
            )
        if self.custody_header is not None and not isinstance(
            cast("object", self.custody_header), PresetArtifactCustodyHeader
        ):
            raise TypeError("preset artifact publication custody must be a custody header.")
        if self.custody_header is None and (self.siblings.count or self.closed_children):
            raise ValueError("a sibling-bearing preset artifact publication carries custody.")


@dataclass(frozen=True, slots=True)
class PresetArtifactStageResult:
    """Plan 4.5: the attested parent of one staged revision, and what the stage wrote.

    A GRAPH bake task stages; only the finalizer constructs pointer members (DB7). `pairs`
    counts every write chain (each sibling pair, each index shard and the parent) and
    `written_bytes` their encoded bytes, as written.
    """

    body: ArtifactBodyWriteResult
    custody: CustodyManifestWriteResult | None
    pairs: int
    written_bytes: int

    @property
    def custody_digest(self) -> str | None:
        """The staged parent custody's digest, which completes the pointer template."""
        return None if self.custody is None else self.custody.digest


@dataclass(frozen=True, slots=True)
class PresetArtifactPublicationResult:
    """The staged revision and the resulting pointer state from one publication attempt."""

    body: ArtifactBodyWriteResult
    pointer: PresetArtifactPointerAdvanceResult
    custody: CustodyManifestWriteResult | None
    pairs: int
    written_bytes: int


# --- the sharded sibling digest index (ADR-145) ----------------------------------------------

SIBLING_INDEX_SHARD_ENTRIES: Final = 800
"""Descriptors per index shard (class A): 800 x `SIBLING_DESCRIPTOR_MAX_BYTES` fits the cap."""
SIBLING_DESCRIPTOR_MAX_BYTES: Final = 256
"""The most bytes one encoded descriptor may take (class A); a test holds the widest real one."""
SIBLING_INDEX_KEY: Final = "sibling_index"
"""The parent-custody key the stage owns: one `{first_identity, last_identity, digest}` a shard."""
SIBLING_INDEX_SHARD_KIND: Final = "preset_artifact_sibling_index_shard"


@dataclass(frozen=True, slots=True)
class PresetArtifactSiblingIndexEntry:
    """One shard's range in the parent custody: its first and last identity paths and digest."""

    first_identity: str
    last_identity: str
    digest: str

    def __post_init__(self) -> None:
        for path in (self.first_identity, self.last_identity):
            child_identity_from_path(path)
        if self.first_identity > self.last_identity or not _DIGEST.fullmatch(self.digest):
            raise ValueError("preset artifact sibling index entry is invalid.")

    def covers(self, path: str) -> bool:
        """Whether an identity path falls in this shard's range."""
        return self.first_identity <= path <= self.last_identity


def _descriptor_payload(descriptor: PresetArtifactSiblingDescriptor) -> dict[str, object]:
    return {
        "body_digest": descriptor.body_digest,
        "custody_digest": descriptor.custody_digest,
        "identity": descriptor.identity.path,
    }


def sibling_descriptor_bytes(descriptor: PresetArtifactSiblingDescriptor) -> int:
    """Return one descriptor's encoded size inside an index shard."""
    return len(canonical_preset_artifact_json(_descriptor_payload(descriptor)).encode())


def sibling_index_shards(
    parent: PresetArtifactBody, descriptors: Iterable[PresetArtifactSiblingDescriptor]
) -> tuple[tuple[PresetArtifactSiblingCustody, PresetArtifactSiblingIndexEntry], ...]:
    """Sort descriptors by identity path and cut them into shards of 800, each with its entry.

    A repeated identity, an index shard named as a sibling, or a descriptor over
    `SIBLING_DESCRIPTOR_MAX_BYTES` is a defect (`ValueError`).
    """
    ordered = sorted(descriptors, key=lambda item: item.identity.path)
    paths = [item.identity.path for item in ordered]
    if len(set(paths)) != len(paths):
        raise ValueError("preset artifact sibling identities must be unique.")
    for item in ordered:
        if isinstance(item.identity, PresetArtifactIndexShardIdentity):
            raise ValueError("an index shard is never a sibling of its own index.")
        if sibling_descriptor_bytes(item) > SIBLING_DESCRIPTOR_MAX_BYTES:
            raise ValueError("a preset artifact sibling descriptor exceeds its byte bound.")
    shards: list[tuple[PresetArtifactSiblingCustody, PresetArtifactSiblingIndexEntry]] = []
    for shard_index, start in enumerate(range(0, len(ordered), SIBLING_INDEX_SHARD_ENTRIES)):
        chunk = ordered[start : start + SIBLING_INDEX_SHARD_ENTRIES]
        shard = PresetArtifactSiblingCustody(
            parent.page,
            parent.variant,
            parent.revision,
            PresetArtifactIndexShardIdentity(shard_index),
            canonical_preset_artifact_json(
                {
                    "descriptors": [_descriptor_payload(item) for item in chunk],
                    "kind": SIBLING_INDEX_SHARD_KIND,
                    "shard": shard_index,
                }
            ),
        )
        shards.append(
            (
                shard,
                PresetArtifactSiblingIndexEntry(
                    chunk[0].identity.path, chunk[-1].identity.path, shard.digest
                ),
            )
        )
    return tuple(shards)


def parent_custody_manifest(
    body: PresetArtifactBody,
    header: PresetArtifactCustodyHeader,
    index: tuple[PresetArtifactSiblingIndexEntry, ...],
) -> PresetArtifactCustodyManifest:
    """Build a parent custody: the source's header plus one `sibling_index` entry per shard."""
    payload = cast("dict[str, object]", json.loads(header.payload_json))
    payload[SIBLING_INDEX_KEY] = [
        {
            "digest": entry.digest,
            "first_identity": entry.first_identity,
            "last_identity": entry.last_identity,
        }
        for entry in index
    ]
    return PresetArtifactCustodyManifest(
        body.page, body.variant, body.revision, canonical_preset_artifact_json(payload)
    )


def split_parent_custody(
    manifest: PresetArtifactCustodyManifest,
) -> tuple[dict[str, object], tuple[PresetArtifactSiblingIndexEntry, ...]]:
    """Return a parent custody's header payload and its sibling index, or raise `ValueError`.

    The index's shards are ordered and disjoint: each range starts after the last one ends.
    """
    payload = cast("dict[str, object]", json.loads(manifest.payload_json))
    raw = payload.pop(SIBLING_INDEX_KEY, None)
    if not isinstance(raw, list):
        raise ValueError("a parent custody carries its sibling index.")
    entries: list[PresetArtifactSiblingIndexEntry] = []
    for item in cast("list[object]", raw):
        if not isinstance(item, dict) or frozenset(cast("dict[str, object]", item)) != frozenset(
            {
                "digest",
                "first_identity",
                "last_identity",
            }
        ):
            raise ValueError("a sibling index entry's fields are not exact.")
        fields = cast("dict[str, object]", item)
        first, last, digest = fields["first_identity"], fields["last_identity"], fields["digest"]
        if not all(isinstance(value, str) for value in (first, last, digest)):
            raise ValueError("a sibling index entry's fields are text.")
        entries.append(
            PresetArtifactSiblingIndexEntry(
                cast("str", first), cast("str", last), cast("str", digest)
            )
        )
    for earlier, later in itertools.pairwise(entries):
        if earlier.last_identity >= later.first_identity:
            raise ValueError("a sibling index's shards are ordered and disjoint.")
    return payload, tuple(entries)


def index_shard_descriptors(
    shard: PresetArtifactSiblingCustody, entry: PresetArtifactSiblingIndexEntry
) -> tuple[PresetArtifactSiblingDescriptor, ...]:
    """Decode one index shard the parent entry binds, or raise `ValueError`.

    The shard's bytes must match the entry's digest, its descriptors must be strictly ordered by
    identity path, and its first and last must be the entry's range.
    """
    if not isinstance(shard.identity, PresetArtifactIndexShardIdentity) or (
        shard.digest != entry.digest
    ):
        raise ValueError("an index shard does not match its parent custody entry.")
    payload = json.loads(shard.payload_json)
    if (
        not isinstance(payload, dict)
        or frozenset(cast("dict[str, object]", payload))
        != frozenset({"descriptors", "kind", "shard"})
        or payload["kind"] != SIBLING_INDEX_SHARD_KIND
        or payload["shard"] != shard.identity.shard_index
        or not isinstance(payload["descriptors"], list)
        or not payload["descriptors"]
    ):
        raise ValueError("an index shard's fields are not exact.")
    descriptors: list[PresetArtifactSiblingDescriptor] = []
    for raw in cast("list[object]", payload["descriptors"]):
        if not isinstance(raw, dict) or frozenset(cast("dict[str, object]", raw)) != frozenset(
            {
                "body_digest",
                "custody_digest",
                "identity",
            }
        ):
            raise ValueError("an index descriptor's fields are not exact.")
        item = cast("dict[str, object]", raw)
        body_digest, custody_digest = item["body_digest"], item["custody_digest"]
        if not isinstance(body_digest, str) or not isinstance(custody_digest, str):
            raise ValueError("an index descriptor's digests are text.")
        descriptors.append(
            PresetArtifactSiblingDescriptor(
                child_identity_from_path(cast("str", item["identity"])),
                body_digest,
                custody_digest,
            )
        )
    paths = [item.identity.path for item in descriptors]
    if (
        any(earlier >= later for earlier, later in itertools.pairwise(paths))
        or (paths[0], paths[-1]) != (entry.first_identity, entry.last_identity)
        or len(paths) > SIBLING_INDEX_SHARD_ENTRIES
    ):
        raise ValueError("an index shard's descriptors are not its entry's ordered range.")
    return tuple(descriptors)


def covering_index_shard(
    index: tuple[PresetArtifactSiblingIndexEntry, ...], identity: PresetArtifactChildIdentity
) -> int | None:
    """Return the position of the one shard whose range covers an identity, or None."""
    path = identity.path
    return next((position for position, entry in enumerate(index) if entry.covers(path)), None)


@dataclass(frozen=True, slots=True)
class SourceUnchanged:
    """A source computed the revision the current pointer already carries (S6 §2.8, D26).

    Distinct from no artifact: nothing is written and the intent is done, never unavailable.
    """

    revision: PresetArtifactRevision


def classify_conditional_pointer_failure(
    request: PresetArtifactPointerAdvanceRequest,
    existing_history: PresetArtifactPointer | None,
    existing_current: PresetArtifactPointer | None,
) -> PresetArtifactPointerAdvanceResult:
    """Classify a failed conditional write after strongly attesting current/history records."""
    desired = request.pointer
    if existing_history is not None and existing_history != desired:
        raise InvariantError(
            PRESET_ARTIFACT_POINTER_CONFLICT,
            "An immutable preset artifact revision names different pointer evidence.",
            context={"partition": desired.partition_key, "revision": desired.revision.value},
        )
    if existing_history == desired and existing_current == desired:
        return PresetArtifactPointerAdvanceResult(PresetArtifactPointerAdvanceOutcome.NO_OP)
    return PresetArtifactPointerAdvanceResult(PresetArtifactPointerAdvanceOutcome.STALE)
