# SPDX-License-Identifier: MIT-0
"""The AgentCore Gateway invocation contract: what arrives, and what may go back.

One module for the transport, so neither tool handler carries knowledge of how it was
invoked and the domain carries none at all. Everything here is adapter work — reading a Lambda
event, finding files on disk, encoding JSON — and none of it is a measurement decision.

**The Gateway Lambda target contract, verified 2026-08-19 rather than inferred.** The parts this
module exists for:

- The **event** is a flat map of the tool's own `inputSchema` properties. There is no envelope, so
  arguments are read straight off it.
- Everything else arrives in `context.client_context.custom`, and the tool name comes **prefixed**
  as `${target_name}___${tool_name}`. Comparing the raw value against a `MeasurementTool` member
  would never match, which is the kind of failure that looks like a routing bug.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import pathlib
from collections.abc import Iterable
from decimal import Decimal
from enum import Enum
from typing import Any, cast

from adapters.outbound.telemetry.reporting import GatewayChannelKeys
from domain.shared_kernel.errors import ErrorCode, InvariantError, ValidationError

__all__ = [
    "BAD_INVOCATION",
    "GatewayInvocation",
    "channel_keys",
    "contracts_root",
    "gateway_message_ref",
    "propagated_header_pairs",
    "to_json_safe",
]

BAD_INVOCATION = ErrorCode(area="GATEWAY", number=1)
"""An invocation this adapter could not read. A `ValidationError`, because the request is outside
its contract rather than the platform being broken."""

BUNDLE_INCOMPLETE = ErrorCode(area="GATEWAY", number=2)
"""The deployment artefact does not carry the contracts. An `InvariantError`: the bundle and the
code disagree, which is a packaging fault rather than anything a caller did."""

TOOL_NAME_DELIMITER = "___"
"""Three underscores, which is what Gateway puts between the target name and the tool name.

Not a guess and not configurable: it is the documented delimiter, and the prefix has to be
stripped by the target because Gateway does not do it.
"""

_MARKER = pathlib.Path("contracts") / "shared" / "metric-dictionary.yaml"


def contracts_root() -> pathlib.Path:
    """Find the directory `contracts/` sits under, in this process, whatever the layout.

    **Two layouts have to work and they are genuinely different.** In the repository the metric
    dictionary is at `<repo>/contracts/` while this module is under `<repo>/src/adapters/`. In a
    Lambda the source tree is unpacked at the task root, so `contracts/` is a sibling of
    `adapters/`. A fixed number of parent hops serves one and breaks the other.

    So it searches upward for the dictionary itself rather than counting directories. That also
    makes the failure informative: a bundle built without `contracts/` fails at import with the
    paths it looked in, rather than at the first request with a `FileNotFoundError` naming one.

    Read at module scope by each entry point, never per request — a container that cannot find its
    own contracts must fail before it is considered healthy.
    """
    here = pathlib.Path(__file__).resolve()
    searched: list[str] = []
    for candidate in here.parents:
        searched.append(str(candidate))
        if (candidate / _MARKER).is_file():
            return candidate
    raise InvariantError(
        BUNDLE_INCOMPLETE,
        "This deployment does not carry the data contracts.",
        context={"looked_for": str(_MARKER), "searched": searched},
    )


@dataclasses.dataclass(frozen=True, slots=True)
class GatewayInvocation:
    """One tool call, as the transport delivered it.

    Separated from the tool request objects on purpose. Those are what a *tool* needs and are
    validated in domain terms; this is what the *wire* said, and the two should not be one type —
    otherwise every tool request grows fields that only exist because of how it was invoked.
    """

    tool_name: str
    arguments: dict[str, Any]
    gateway_message_ref: str

    @classmethod
    def read(cls, event: dict[str, Any], context: object) -> GatewayInvocation:
        """Read the invocation, or refuse it naming what was missing.

        **`gateway_message_ref` comes from `bedrockAgentCoreMcpMessageId`.** A Gateway Lambda target
        receives no browser correlation id, session id, or supported distributed trace parent. The
        local request identifier can locate the managed Gateway call, but it must never be labelled
        as browser correlation or used to imply trace continuity.

        Deliberately **not** taken from the arguments. A locator the model supplies is one the model
        can fabricate or reuse, which is the same class of mistake as accepting identity as a
        parameter.
        """
        custom = _custom_context(context)
        raw_name = custom.get("bedrockAgentCoreToolName")
        if not isinstance(raw_name, str) or not raw_name:
            raise ValidationError(
                code=BAD_INVOCATION,
                description="This invocation did not say which tool was called.",
                context={"present": sorted(custom)},
            )
        tool_name = strip_target_prefix(raw_name)
        if not tool_name:
            raise ValidationError(
                code=BAD_INVOCATION,
                description="This invocation carried an empty tool name.",
                context={"present": sorted(custom)},
            )

        message_ref = gateway_message_ref(context)
        if not message_ref:
            # Both absent means the context is not a Gateway context at all. Refused rather than
            # defaulted, because an invented locator is worse than none: it looks like provenance
            # and is not.
            raise ValidationError(
                code=BAD_INVOCATION,
                description="This invocation carried no Gateway message reference.",
                context={"present": sorted(custom)},
            )

        return cls(
            tool_name=tool_name,
            arguments=dict(event),
            gateway_message_ref=message_ref,
        )


def channel_keys(event: object, context: object) -> GatewayChannelKeys:
    """Name the keys this invocation carried in its client context and its event, never values.

    ADR-077 probe P3: whether an `Authorization` an interceptor sets reaches a Lambda target, and
    through which channel. Read from the raw event and context rather than a parsed invocation, so
    an invocation this adapter refuses still reports what it carried.
    """
    event_keys: list[str] = []
    if isinstance(event, dict):
        event_keys = [str(key) for key in cast("dict[Any, Any]", event)]
    custom = _custom_context(context)
    return GatewayChannelKeys.from_names(
        custom, event_keys, _header_names(custom.get(PROPAGATED_HEADERS_KEY))
    )


PROPAGATED_HEADERS_KEY = "bedrockAgentCorePropagatedHeaders"
"""Client-context key through which Gateway delivers propagated headers to a Lambda target.

Undocumented in the Lambda target contract; observed live 2026-09-26 (ADR-077 probe P3).
"""


def propagated_header_pairs(context: object) -> tuple[tuple[str, object], ...]:
    """Return every header Gateway propagated to this target, as (name, value) pairs.

    Duplicates are kept rather than collapsed: the value arrives as a map or as JSON of one, and
    `json.loads` would silently keep only the last of two same-named members. A caller deciding
    that exactly one `Authorization` arrived needs to see both. Anything that is not a map, or not
    JSON of one, carries no headers.
    """
    return _header_pairs(_custom_context(context).get(PROPAGATED_HEADERS_KEY))


@dataclasses.dataclass(frozen=True, slots=True)
class _JsonObject:
    """One decoded JSON object, its members in order and duplicates intact."""

    pairs: tuple[tuple[str, object], ...]


def _json_object(pairs: list[tuple[str, object]]) -> _JsonObject:
    return _JsonObject(tuple(pairs))


def _header_pairs(value: object) -> tuple[tuple[str, object], ...]:
    """Read a propagated-headers value: a map, or JSON of one."""
    if isinstance(value, str):
        try:
            decoded: object = json.loads(value, object_pairs_hook=_json_object)
        except ValueError:
            return ()
        return decoded.pairs if isinstance(decoded, _JsonObject) else ()
    if isinstance(value, dict):
        return tuple((str(name), item) for name, item in cast("dict[Any, Any]", value).items())
    return ()


def _header_names(value: object) -> list[str]:
    """Return the header names a propagated-headers value carries: a map, or JSON of one."""
    return [name for name, _ in _header_pairs(value)]


def gateway_message_ref(context: object) -> str:
    """Return the bounded target-local AgentCore request reference, if present."""
    custom = _custom_context(context)
    value = custom.get("bedrockAgentCoreMcpMessageId") or custom.get("bedrockAgentCoreAwsRequestId")
    return value[:256] if isinstance(value, str) and value else ""


def strip_target_prefix(tool_name: str) -> str:
    """Take the tool's own name out of the prefixed name Gateway sends.

    `measurement___measurement_metric_by_grain` becomes `measurement_metric_by_grain`. A name with
    no delimiter is returned unchanged, so a direct invocation in a test does not have to fake the
    prefix to be routed.

    Splits on the **last** occurrence rather than the first, because the delimiter is three
    underscores and a tool name is free to contain one.
    """
    _, delimiter, suffix = tool_name.rpartition(TOOL_NAME_DELIMITER)
    return suffix if delimiter else tool_name


def _custom_context(context: object) -> dict[str, Any]:
    """Read `context.client_context.custom`, or an empty mapping.

    Tolerant because the shape varies by caller rather than by correctness: Gateway populates the
    client context, a console test invocation does not, and a unit test supplies its own object.
    Absence is reported by the caller as a refusal naming what was present, which is more useful
    than an `AttributeError` three frames down.
    """
    client_context: object = getattr(context, "client_context", None)
    custom: object = getattr(client_context, "custom", None)
    if not isinstance(custom, dict):
        return {}
    pairs = cast("dict[Any, Any]", custom)
    return {str(key): item for key, item in pairs.items()}


def to_json_safe(value: Any) -> Any:
    """Convert the envelope into something `json.dumps` accepts, losing nothing.

    **Every `Decimal` becomes a string, and that is the point rather than a limitation.** The
    envelope's whole premise is that a tool has already rounded and scaled its figures, so the
    scale is part of the answer — `Decimal("0.9420")` through `float` is a value that no longer
    states four decimal places, and for money it is where a cent goes missing. A string preserves
    exactly what the tool computed, and the model is reading rather than calculating.

    Enums are unwrapped to their values so a consumer sees `fresh` rather than
    `FreshnessStatus.FRESH`, and tuples become lists because JSON has no tuple.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return to_json_safe(value.value)
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: to_json_safe(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    return _containers(value)


def _containers(value: Any) -> Any:
    """Recurse into a mapping or a sequence, and pass anything else through.

    Split from `to_json_safe` only because the branch count crossed a lint threshold. The two read
    as one function and the boundary carries no meaning of its own.
    """
    if isinstance(value, dict):
        mapping = cast("dict[Any, Any]", value)
        return {str(key): to_json_safe(item) for key, item in mapping.items()}
    if isinstance(value, list | tuple | set | frozenset):
        items = cast("Iterable[Any]", value)
        return [to_json_safe(item) for item in items]
    return value
