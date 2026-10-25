# SPDX-License-Identifier: MIT-0
"""Merge reads that each arrive grouped and ordered by one key (ADR-152, entity-streamed reads).

A campaign-wide page reads several figures at one grain, each `ORDER BY` its grain, and renders
one campaign at a time. Each read is cut into groups by its leading key columns; this merges the
groups of every read by that key, so the render sees one key's groups from all reads together
and never holds another key's. Ordering is by code point, which is how Trino orders `varchar`,
so a key that arrives out of order is a defect, never a reorder.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Final

from domain.shared_kernel.errors import ErrorCode, InvariantError

__all__ = ["GROUPS_OUT_OF_ORDER", "KeyedGroup", "MergedGroup", "merge_ordered_groups"]

GROUPS_OUT_OF_ORDER: Final = ErrorCode("GROUPS", 1)
"""A grouped read delivered a key at or before one it already delivered: a defect."""

type GroupKey = tuple[str, ...]


@dataclass(frozen=True, slots=True)
class KeyedGroup[T]:
    """One read's members under one key, in the order the read returned them."""

    key: GroupKey
    members: tuple[T, ...]


@dataclass(frozen=True, slots=True)
class MergedGroup[T]:
    """One key's group from every read, in read order; an empty tuple where a read had none."""

    key: GroupKey
    groups: tuple[tuple[T, ...], ...]


def _ordered[T](stream: Iterable[KeyedGroup[T]], index: int) -> Iterator[KeyedGroup[T]]:
    last: GroupKey | None = None
    for group in stream:
        if last is not None and group.key <= last:
            raise InvariantError(
                GROUPS_OUT_OF_ORDER,
                "A grouped read delivered its keys out of code-point order.",
                context={"read": str(index)},
            )
        last = group.key
        yield group


def merge_ordered_groups[T](
    streams: tuple[Iterable[KeyedGroup[T]], ...],
) -> Iterator[MergedGroup[T]]:
    """Yield each key once, ascending, with every read's group for it.

    Each stream must deliver strictly ascending keys. At most one group per stream is held
    ahead of the key being yielded.
    """
    iterators = tuple(_ordered(stream, index) for index, stream in enumerate(streams))
    heads: list[KeyedGroup[T] | None] = [next(iterator, None) for iterator in iterators]
    while True:
        present = [head.key for head in heads if head is not None]
        if not present:
            return
        key = min(present)
        groups: list[tuple[T, ...]] = []
        for index, head in enumerate(heads):
            if head is not None and head.key == key:
                groups.append(head.members)
                heads[index] = next(iterators[index], None)
            else:
                groups.append(())
        yield MergedGroup(key, tuple(groups))
