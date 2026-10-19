# SPDX-License-Identifier: MIT-0
"""Canonical AWS SDK failure metadata for structured application telemetry."""

from __future__ import annotations

import re
from collections.abc import Mapping

from botocore.exceptions import BotoCoreError, ClientError

_MAX_CHAIN_DEPTH = 12
_AUTHORIZATION_FAILURE = re.compile(
    r"(?:User|Principal): (?P<principal>.+?) is not authorized to perform: "
    r"(?P<action>[A-Za-z0-9:_-]+) on resource: (?P<resource>\S+)"
)
_REDACTIONS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)((?:set-)?cookie\s*[:=]\s*)[^\r\n]+"),
    re.compile(r"(?i)(x-amz-(?:credential|security-token|signature)\s*[:=]\s*)[^&\s,;]+"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"(?i)\b\S*(?:secret|password|credential|token)\S*\b"),
)


def redact_diagnostic_text(value: str) -> str:
    """Remove credential-shaped values while preserving diagnostic meaning."""
    redacted = value.replace("\r", " ").replace("\n", " ")
    for pattern in _REDACTIONS:
        redacted = pattern.sub(
            lambda match: f"{match.group(1)}[REDACTED]" if match.lastindex else "[REDACTED]",
            redacted,
        )
    return redacted


def _exception_chain(error: BaseException) -> tuple[BaseException, ...]:
    chain: list[BaseException] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(chain) < _MAX_CHAIN_DEPTH:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return tuple(chain)


def aws_failure_context(error: BaseException) -> dict[str, object]:
    """Return canonical provider metadata from the first AWS SDK failure in a chain."""
    chain = _exception_chain(error)
    sdk_error = next((item for item in chain if isinstance(item, ClientError)), None)
    if isinstance(sdk_error, ClientError):
        response = sdk_error.response
        details = response.get("Error", {})
        metadata = response.get("ResponseMetadata", {})
        context: dict[str, object] = {
            "cause": type(sdk_error).__name__,
            "provider": "aws",
            "provider_operation": sdk_error.operation_name,
        }
        service_code = details.get("Code")
        message = details.get("Message")
        status = metadata.get("HTTPStatusCode")
        request_id = metadata.get("RequestId")
        if isinstance(service_code, str) and service_code:
            context["service_code"] = service_code
        if isinstance(message, str) and message:
            provider_message = redact_diagnostic_text(message)
            context["provider_message"] = provider_message
            authorization = _AUTHORIZATION_FAILURE.search(provider_message)
            if authorization is not None:
                context.update(
                    {
                        "principal": authorization.group("principal"),
                        "provider_action": authorization.group("action"),
                        "resource": authorization.group("resource"),
                    }
                )
        if type(status) is int:
            context["status"] = status
        if isinstance(request_id, str) and request_id:
            context["request_id"] = request_id
        return context

    core_error = next((item for item in chain if isinstance(item, BotoCoreError)), None)
    if isinstance(core_error, BotoCoreError):
        return {
            "cause": type(core_error).__name__,
            "provider": "aws",
            "provider_message": redact_diagnostic_text(str(core_error)),
        }
    return {}


def merged_aws_failure_context(
    error: BaseException,
    existing: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Merge adapter context with canonical AWS fields taking precedence."""
    return {**dict(existing or {}), **aws_failure_context(error)}
