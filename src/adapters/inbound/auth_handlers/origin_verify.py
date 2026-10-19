# SPDX-License-Identifier: MIT-0
"""The origin-verify check both the authorizer and the auth handler make first (ADR-078).

Each container reads the secret at cold start and holds it for at most the maximum cache age. A
presented value that matches neither current nor previous triggers one re-read, rate-limited to the
re-read interval, which is shorter than that age. That is how a warm container meets a rotated
value without refusing edge traffic, and why a flood of wrong values costs at most one Secrets
Manager read per interval per container.

A read that fails refuses the request. Serving an expired value instead would keep accepting a
dropped previous value past the maximum age the rotation procedure relies on.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from adapters import config
from adapters.inbound.auth_handlers import events
from domain.identity.origin_verify import (
    ORIGIN_VERIFY_HEADER,
    OriginVerifyValues,
    origin_matches,
    parse_origin_verify_values,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from aws_lambda_powertools import Logger

REFUSED_CATEGORY = "origin_verify_refused"
"""The log category the `OriginVerifyRefused` metric filter in `ApiStack` counts."""


class OriginVerifyUnavailableError(Exception):
    """The held values expired and the last re-read failed within the interval.

    Raised rather than answered `False`, so a Secrets Manager fault reports as an error, never as
    an origin-verify refusal that the direct-caller alarm counts.
    """


class OriginVerifier:
    """Holds the origin-verify values under the cache rule above."""

    def __init__(
        self,
        settings: config.OriginVerifySettings,
        read: Callable[[str], str],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read = lambda: read(settings.secret_id)
        self._max_age = settings.max_cache_age_seconds
        self._interval = settings.reread_interval_seconds
        self._clock = clock
        now = clock()
        self._values: OriginVerifyValues = parse_origin_verify_values(self._read())
        self._loaded_at = now
        self._last_attempt = now

    def verify(self, presented: str) -> bool:
        """Whether `presented` is an accepted value, re-reading at most once per interval."""
        now = self._clock()
        expired = now - self._loaded_at >= self._max_age
        if not expired and origin_matches(presented, self._values):
            return True
        if now - self._last_attempt < self._interval:
            if expired:
                raise OriginVerifyUnavailableError
            return False
        self._last_attempt = now
        self._values = parse_origin_verify_values(self._read())
        self._loaded_at = now
        return origin_matches(presented, self._values)


def verifier_from_config() -> OriginVerifier:
    """Build the process-wide verifier, reading the secret now so a bad one fails the cold start."""
    return OriginVerifier(config.origin_verify_settings(), config.read_origin_verify_secret)


def presented_value(event: dict[str, Any]) -> str:
    """Return the header value the request carries, or empty."""
    return events.headers(event).get(ORIGIN_VERIFY_HEADER, "")


def report_refusal(logger: Logger, *, component: str, event: dict[str, Any]) -> None:
    """Record one refusal with the API request id, and never the presented value.

    The request id is what lets acceptance prove the authorizer never ran for a request without
    the header: every request the authorizer refuses for a missing or wrong value leaves this
    record, so a request id with no record was answered by API Gateway alone. A request refused
    because the secret could not be read is reported as an error instead, never here.
    """
    logger.warning(
        "origin verify refused",
        extra={
            "category": REFUSED_CATEGORY,
            "component": component,
            "api_request_id": events.api_request_id(event),
        },
    )
