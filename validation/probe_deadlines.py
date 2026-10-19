# SPDX-License-Identifier: MIT-0
"""Hard wall-clock deadlines for long-running live probes, per network call and per run.

On 2026-10-06 two probes hung for more than 30 minutes on a dead connection after the host's
network changed, although every call carried a client timeout. A socket timeout bounds one
`recv`; it does not bound a call that keeps a half-open connection or retries quietly. These
deadlines are wall-clock alarms (`signal.setitimer`), so the probe fails fast with a named
`ProbeDeadlineError` whatever the socket does.

- `within(seconds, label)` nests: the earliest open deadline is the armed one, and leaving a
  block re-arms the next. Main thread only, as every probe here is single-threaded.
- `BOTO_CONFIG` gives every boto3 client explicit connect and read timeouts and a bounded
  standard retry, so one call is bounded even before its alarm.
- `bounded_client` runs every direct API call of a client inside its own deadline.
"""

from __future__ import annotations

import signal
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import FrameType
from typing import Any, Final

from botocore.config import Config

BOTO_CONFIG: Final = Config(
    connect_timeout=10, read_timeout=30, retries={"max_attempts": 3, "mode": "standard"}
)
CALL_DEADLINE_SECONDS: Final = 120
"""One API call: three attempts of at most 10 s connect plus 30 s read, then the alarm."""


class ProbeDeadlineError(TimeoutError):
    """A probe call or run passed its wall-clock deadline; the message names which."""


@dataclass
class _Deadlines:
    clock: Callable[[], float] = time.monotonic
    open: list[tuple[float, str, float]] = field(default_factory=list[tuple[float, str, float]])

    def _arm(self) -> None:
        if not self.open:
            signal.setitimer(signal.ITIMER_REAL, 0)
            return
        earliest = min(end for end, _, _ in self.open)
        signal.setitimer(signal.ITIMER_REAL, max(0.001, earliest - self.clock()))

    def _expired(self, _signum: int, _frame: FrameType | None) -> None:
        _, label, seconds = min(self.open)
        raise ProbeDeadlineError(f"{label} passed its {seconds:g} s deadline")

    @contextmanager
    def within(self, seconds: float, label: str) -> Generator[None]:
        if seconds <= 0:
            raise ValueError("a deadline must be positive")
        previous = signal.signal(signal.SIGALRM, self._expired)
        entry = (self.clock() + seconds, label, seconds)
        self.open.append(entry)
        self._arm()
        try:
            yield
        finally:
            self.open.remove(entry)
            self._arm()
            if not self.open:
                signal.signal(signal.SIGALRM, previous)


DEADLINES: Final = _Deadlines()
within: Final = DEADLINES.within


class _BoundedClient:
    """A boto3 client whose direct calls each run inside a deadline.

    Paginators are bounded by `BOTO_CONFIG` per page and by the run deadline.
    """

    def __init__(self, client: Any, service: str, seconds: float) -> None:
        self._client = client
        self._service = service
        self._seconds = seconds

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._client, name)
        if (
            not callable(attribute)
            or name.startswith("_")
            or name in {"get_paginator", "can_paginate"}
        ):
            return attribute

        def call(*args: Any, **kwargs: Any) -> Any:
            with within(self._seconds, f"{self._service}.{name}"):
                return attribute(*args, **kwargs)

        return call


def bounded_client(
    session: Any,
    service: str,
    *,
    region_name: str | None = None,
    seconds: float = CALL_DEADLINE_SECONDS,
) -> Any:
    """Create a client with `BOTO_CONFIG` and a per-call deadline."""
    options: dict[str, Any] = {"config": BOTO_CONFIG}
    if region_name is not None:
        options["region_name"] = region_name
    return _BoundedClient(session.client(service, **options), service, seconds)
