# SPDX-License-Identifier: MIT-0
"""Properties every domain value must have, asserted once rather than per module.

Mutation review kept finding the same gap in module after module: `frozen=True` carried a
guarantee that nothing asserted, so a mutant flipping it survived. Three reviews found it three
times (the objectives, figures and watermarks modules; ADR-013) and the remaining fifty-odd domain
modules would each have found it again.

Asserting it here instead is the move ADR-013 asks for: when a property holds for every member of
a set, test the set. This kills the mutant in every domain module at once, including the ones no
mutation run has reached, and it fails on a new dataclass added without `frozen`.

`slots=True` is deliberately not asserted. It is one of ADR-013's two known equivalent classes —
it changes how attributes are stored, not what they are or what may be done to them — so a test
for it would pin an implementation detail rather than a guarantee.
"""

from __future__ import annotations

import dataclasses
import importlib
import pkgutil
from datetime import date

import pytest

import domain
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit, pytest.mark.covers("G-25", "ADR-013")]


def _domain_modules() -> list[str]:
    """Every importable module under `domain`, which is also a claim that they import cleanly.

    A domain module that needed credentials, a network call or a clock to import would fail here,
    which is the hexagonal rule stated as a test rather than only as a boundary check.
    """
    found = [
        info.name
        for info in pkgutil.walk_packages(domain.__path__, prefix="domain.")
        if not info.ispkg
    ]
    if not found:
        raise AssertionError("no domain modules were discovered, so this suite proves nothing")
    return sorted(found)


def _dataclasses_declared_in(module_name: str) -> list[type]:
    """Only the dataclasses this module defines, never ones it imported from elsewhere."""
    module = importlib.import_module(module_name)
    return [
        obj
        for obj in vars(module).values()
        if isinstance(obj, type) and dataclasses.is_dataclass(obj) and obj.__module__ == module_name
    ]


DOMAIN_MODULES = _domain_modules()


class TestEveryDomainValueIsImmutable:
    """A domain value records something already decided, so nothing may edit one afterwards."""

    @pytest.mark.parametrize("module_name", DOMAIN_MODULES)
    def test_every_dataclass_is_frozen(self, module_name: str) -> None:
        mutable = [
            cls.__qualname__
            for cls in _dataclasses_declared_in(module_name)
            if not cls.__dataclass_params__.frozen  # type: ignore[attr-defined]
        ]

        assert mutable == []

    def test_the_sweep_actually_finds_dataclasses(self) -> None:
        """Without this the parametrised test above passes on an empty collection, forever.

        The count is asserted as a floor rather than a literal so adding a value object does not
        edit this test, while removing the discovery mechanism still fails it.
        """
        total = sum(len(_dataclasses_declared_in(name)) for name in DOMAIN_MODULES)

        assert total >= 50

    def test_a_frozen_instance_actually_refuses_assignment(self) -> None:
        """`__dataclass_params__` is metadata; this is the behaviour it is supposed to describe.

        Asserted on one real value rather than all of them, because what could differ between
        classes is the flag, and the flag is what the parametrised test covers.
        """
        window = DateRange(date(2026, 8, 10), date(2026, 8, 16))

        with pytest.raises(dataclasses.FrozenInstanceError):
            window.start = date(2026, 1, 1)  # type: ignore[misc]
