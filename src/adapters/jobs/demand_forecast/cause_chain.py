# SPDX-License-Identifier: MIT-0
"""Turn a caught error into the error envelope's cause chain (adapted from the source engine).

Class names only, outermost first, at most `MAX_CAUSES`, following `__cause__` and
`__context__`; never a message, which can carry values. Nothing here raises.
"""

from __future__ import annotations

import re

MAX_CAUSES = 5
_CLASS_NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$.]{0,127}")


def cause_chain(error: BaseException) -> tuple[str, ...]:
    """Return the class names of `error` and its causes, outermost first."""
    classes: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and len(classes) < MAX_CAUSES and id(current) not in seen:
        seen.add(id(current))
        name = type(current).__name__
        classes.append(name if _CLASS_NAME.fullmatch(name) else "Exception")
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return tuple(classes)
