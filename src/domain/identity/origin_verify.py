# SPDX-License-Identifier: MIT-0
"""The origin-verify comparison (ADR-078, docs/concepts/authentication.md edge entry).

CloudFront adds a secret header to every request it forwards to the HTTP API. The header proves a
request came through the edge, not who sent it: every authorized route still requires a verified
credential. The secret holds a current value and, during a rotation, the previous one; either is
accepted, and nothing else is.
"""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass, field
from typing import cast

from domain.identity.session import IdentityError

ORIGIN_VERIFY_HEADER = "x-adl-origin-verify"
"""Declared once more in `infra/lib/config` as `ORIGIN_VERIFY_HEADER`; a test holds them equal."""


@dataclass(frozen=True, slots=True)
class OriginVerifyValues:
    """The values an edge request may carry. Never rendered, so a log of this object is safe."""

    current: str = field(repr=False)
    previous: str = field(default="", repr=False)


def parse_origin_verify_values(secret_string: str) -> OriginVerifyValues:
    """Parse the secret's `{"current", "previous"}` document; `previous` is optional.

    Raises `IdentityError` without echoing the document, which is the secret itself.
    """
    try:
        document = json.loads(secret_string)
    except ValueError:
        raise IdentityError("The origin-verify secret is not a JSON document.") from None
    if not isinstance(document, dict):
        raise IdentityError("The origin-verify secret is not a JSON object.")
    fields = cast("dict[str, object]", document)
    current = fields.get("current")
    previous = fields.get("previous", "")
    if not isinstance(current, str) or not current or not isinstance(previous, str):
        raise IdentityError("The origin-verify secret has no usable current value.")
    return OriginVerifyValues(current=current, previous=previous)


def origin_matches(presented: str, values: OriginVerifyValues) -> bool:
    """Whether `presented` is the current or the previous value.

    Constant-time, and both comparisons always run, so the timing reveals neither how much of a
    value matched nor which of the two did. An empty presented value never matches, and an empty
    previous value is never a match target.
    """
    candidate = presented.encode()
    current = hmac.compare_digest(candidate, values.current.encode())
    previous = hmac.compare_digest(candidate, values.previous.encode()) and bool(values.previous)
    return bool(presented) and (current or previous)
