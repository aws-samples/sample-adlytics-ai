# SPDX-License-Identifier: MIT-0
"""Typed prepublication rule evidence gates canonical publication.

ADR-049. Evidence is complete only when every required rule family ran. A qualified result is
publishable only when its rule supplies the mandatory caveat; a free-form warning cannot enter this
model. The immutable result set is persisted with each successful publication.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "NO_CELL_RETURNED_RULE_ID",
    "WITHHELD_ONLY_RULE_ID",
    "LegacyPublicationRules",
    "PrepublicationRuleFamily",
    "PrepublicationRuleResult",
    "PublicationEvidence",
    "PublicationEvidenceSubject",
    "RuleOutcome",
]

WITHHELD_ONLY_RULE_ID = "suppression:withheld-only"
NO_CELL_RETURNED_RULE_ID = "suppression:no-cell-returned"
"""A roster-graded grain set's delivery returned no cell: zero rows, every rostered key absent.

Unlike `WITHHELD_ONLY_RULE_ID` it reads as an answer, not a refusal: the grain set has no closed
enumeration at load (pack 2, design §4.5), so its roster supplies "not available" at serve.
"""

_RULE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$")


class PrepublicationRuleFamily(StrEnum):
    """Closed validation families required before canonical data can become current."""

    PROVENANCE = "provenance"
    IDENTITY = "identity"
    TIME = "time"
    METRIC_SEMANTICS = "metric_semantics"
    CONTRACT_ASSERTIONS = "contract_assertions"
    TABLE_DQ = "table_dq"
    CONTROL_TOTALS = "control_totals"
    SUPPRESSION_COVERAGE = "suppression_coverage"


class RuleOutcome(StrEnum):
    """Whether one typed rule passed, qualified publication, or refused it."""

    PASSED = "pass"
    QUALIFIED = "qualified"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class PrepublicationRuleResult:
    """Immutable result of one versioned rule in one closed validation family."""

    family: PrepublicationRuleFamily
    rule_id: str
    outcome: RuleOutcome
    detail: str
    qualifier: str | None = None

    def __post_init__(self) -> None:
        if not _RULE_ID.fullmatch(self.rule_id.strip()):
            raise ValueError(f"prepublication rule id is invalid: {self.rule_id!r}.")
        if not self.detail.strip():
            raise ValueError("prepublication rule result requires diagnostic detail.")
        if self.outcome is RuleOutcome.QUALIFIED and not (
            self.qualifier and self.qualifier.strip()
        ):
            raise ValueError("qualified prepublication result requires its mandatory caveat.")
        if self.outcome is not RuleOutcome.QUALIFIED and self.qualifier is not None:
            raise ValueError("only a qualified prepublication result may carry a caveat.")


@dataclass(frozen=True, slots=True)
class PublicationEvidenceSubject:
    """Exact candidate semantics and bytes a modern rule set evaluated."""

    publisher: str
    partition: str
    source_publications: tuple[str, ...]
    source_revision: str
    content_checksum: str

    def __post_init__(self) -> None:
        values = {
            "publisher": self.publisher,
            "partition": self.partition,
            "source revision": self.source_revision,
            "content checksum": self.content_checksum,
        }
        missing = [label for label, value in values.items() if not value.strip()]
        if missing:
            raise ValueError(
                f"publication evidence subject is missing: {', '.join(sorted(missing))}."
            )
        if not self.source_publications or any(
            not reference.strip() for reference in self.source_publications
        ):
            raise ValueError("publication evidence subject requires source publications.")
        if len(set(self.source_publications)) != len(self.source_publications):
            raise ValueError("publication evidence subject repeats a source publication.")


@dataclass(frozen=True, slots=True)
class LegacyPublicationRules:
    """Readable pre-V2 evidence that cannot authorize a new publication candidate."""

    passed_rules: tuple[str, ...]
    qualified_warnings: tuple[str, ...] = ()
    failed_rules: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.passed_rules:
            raise ValueError("legacy publication evidence requires its passed rules.")


@dataclass(frozen=True, slots=True)
class PublicationEvidence:
    """Modern candidate-bound evidence or readable historical untyped evidence."""

    rule_set_version: str
    subject: PublicationEvidenceSubject | None = None
    results: tuple[PrepublicationRuleResult, ...] = ()
    legacy: LegacyPublicationRules | None = None

    def __post_init__(self) -> None:
        if not self.rule_set_version.strip():
            raise ValueError("publication evidence requires its rule-set version.")
        modern = self.subject is not None or bool(self.results)
        historical = self.legacy is not None
        if modern == historical:
            raise ValueError("publication evidence must be exactly one of modern or historical.")
        if historical:
            if self.subject is not None or self.results:
                raise ValueError("historical evidence cannot claim a modern candidate binding.")
            return
        if self.subject is None or not self.results:
            raise ValueError("modern publication evidence requires subject and typed results.")
        rule_ids = tuple(result.rule_id for result in self.results)
        if len(set(rule_ids)) != len(rule_ids):
            raise ValueError("publication evidence repeats a rule result.")
        present = {result.family for result in self.results}
        missing = set(PrepublicationRuleFamily) - present
        if missing:
            names = ", ".join(sorted(family.value for family in missing))
            raise ValueError(f"publication evidence is missing required families: {names}.")

    @property
    def publishable(self) -> bool:
        """Whether modern complete evidence may authorize its exact bound candidate."""
        return self.subject is not None and all(
            result.outcome is not RuleOutcome.FAIL for result in self.results
        )

    @property
    def withheld_only(self) -> bool:
        """Whether typed modern evidence proves the bucket has no attributable rows."""
        return any(
            result.family is PrepublicationRuleFamily.SUPPRESSION_COVERAGE
            and result.rule_id == WITHHELD_ONLY_RULE_ID
            and result.outcome is RuleOutcome.PASSED
            for result in self.results
        )

    @property
    def no_cell_returned(self) -> bool:
        """Whether typed modern evidence proves a roster-graded delivery returned no cell."""
        return any(
            result.family is PrepublicationRuleFamily.SUPPRESSION_COVERAGE
            and result.rule_id == NO_CELL_RETURNED_RULE_ID
            and result.outcome is RuleOutcome.PASSED
            for result in self.results
        )

    @property
    def readable_historical_success(self) -> bool:
        """Whether a prior immutable success remains readable but cannot be reused."""
        return self.legacy is not None and not self.legacy.failed_rules

    @property
    def passed_rules(self) -> tuple[str, ...]:
        """Stable rule ids that passed without qualification."""
        if self.legacy is not None:
            return self.legacy.passed_rules
        return tuple(
            result.rule_id for result in self.results if result.outcome is RuleOutcome.PASSED
        )

    @property
    def qualified_warnings(self) -> tuple[str, ...]:
        """Mandatory caveats, or historical warnings retained without reinterpretation."""
        if self.legacy is not None:
            return self.legacy.qualified_warnings
        return tuple(
            result.qualifier
            for result in self.results
            if result.outcome is RuleOutcome.QUALIFIED and result.qualifier is not None
        )

    @property
    def failed_rules(self) -> tuple[str, ...]:
        """Stable rule ids that refused publication."""
        if self.legacy is not None:
            return self.legacy.failed_rules
        return tuple(
            result.rule_id for result in self.results if result.outcome is RuleOutcome.FAIL
        )
