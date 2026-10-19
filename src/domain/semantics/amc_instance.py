# SPDX-License-Identifier: MIT-0
"""Which kind of AMC instance a deployment reads, from its governed declaration.

An instant-access instance holds exactly one sponsored ads account and can't add DSP
(docs/sources/amc.md, "Instance types"), so a figure that needs DSP and sponsored ads on one
instance can't exist there, and the page says so instead of reading as empty. The kind is a
deployment fact nothing in the data reveals, so it is declared once
(`contracts/shared/amc-instance.yaml`) and read, never defaulted: a missing or unknown kind
resolves to a named reason, and a surface that needs it stays closed.

Pure: an adapter reads the declaration and passes its parsed document in.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import cast

__all__ = [
    "AMC_INSTANCE_KIND_MISSING",
    "AMC_INSTANCE_KIND_UNKNOWN",
    "AmcInstanceKind",
    "AmcInstanceResolution",
    "resolve_amc_instance",
]

AMC_INSTANCE_KIND_MISSING = "amc_instance_kind_missing"
"""The declaration, or its `kind`, is absent."""
AMC_INSTANCE_KIND_UNKNOWN = "amc_instance_kind_unknown"
"""The declaration names a kind this platform doesn't govern, or isn't a mapping."""


class AmcInstanceKind(StrEnum):
    """The governed AMC instance kinds a deployment can read."""

    STANDARD = "standard"
    """DSP and sponsored ads entities mapped to one instance."""
    INSTANT_ACCESS = "instant_access"
    """Self-service: one sponsored ads account, no DSP."""

    @property
    def dsp_available(self) -> bool:
        """Whether DSP and sponsored ads can be read together on this instance."""
        return self is AmcInstanceKind.STANDARD


@dataclass(frozen=True, slots=True)
class AmcInstanceResolution:
    """The declared kind, or the named reason it couldn't be read; exactly one is set."""

    kind: AmcInstanceKind | None
    reason: str | None = None

    def __post_init__(self) -> None:
        if (self.kind is None) == (self.reason is None):
            raise ValueError("an AMC instance resolution carries a kind or a reason, never both.")


def resolve_amc_instance(document: object) -> AmcInstanceResolution:
    """Read a parsed declaration; None is a missing file, anything malformed a named reason."""
    if document is None:
        return AmcInstanceResolution(None, AMC_INSTANCE_KIND_MISSING)
    if not isinstance(document, dict):
        return AmcInstanceResolution(None, AMC_INSTANCE_KIND_UNKNOWN)
    raw = cast("dict[str, object]", document).get("kind")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return AmcInstanceResolution(None, AMC_INSTANCE_KIND_MISSING)
    try:
        return AmcInstanceResolution(AmcInstanceKind(str(raw)))
    except ValueError:
        return AmcInstanceResolution(None, AMC_INSTANCE_KIND_UNKNOWN)
