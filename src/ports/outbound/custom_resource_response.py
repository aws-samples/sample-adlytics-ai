# SPDX-License-Identifier: MIT-0
"""Custom-resource response port: deliver one CloudFormation response document."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["CustomResourceResponderPort", "CustomResourceResponse"]


@dataclass(frozen=True, slots=True)
class CustomResourceResponse:
    """One serialized response for the callback's pre-signed ResponseURL."""

    response_url: str
    body: bytes

    def __post_init__(self) -> None:
        if not self.response_url.startswith("https://"):
            raise ValueError("A custom-resource response goes only to an https ResponseURL.")


@runtime_checkable
class CustomResourceResponderPort(Protocol):
    """Deliver a response; raises DependencyError when delivery fails or is refused."""

    def send(self, response: CustomResourceResponse) -> None:
        """PUT the response document to its ResponseURL."""
        ...
