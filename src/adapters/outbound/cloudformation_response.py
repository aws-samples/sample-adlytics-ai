# SPDX-License-Identifier: MIT-0
"""HTTPS delivery of a CloudFormation custom-resource response.

The ResponseURL is a pre-signed S3 URL. CloudFormation's custom-resource contract requires a PUT
with an empty `Content-Type`, because the URL was signed without one.
"""

from __future__ import annotations

from typing import Final

import requests

from adapters.outbound.https import HttpsSession, InsecureUrlError
from domain.shared_kernel.errors import DependencyError, ErrorCode
from ports.outbound.custom_resource_response import (
    CustomResourceResponderPort,
    CustomResourceResponse,
)

__all__ = ["RESPONSE_DELIVERY_FAILED", "HttpsCustomResourceResponder"]

RESPONSE_DELIVERY_FAILED: Final = ErrorCode("HARNESSCR", 3)
_TIMEOUT_SECONDS: Final = 30
_SUCCESS_LIMIT: Final = 300


class HttpsCustomResourceResponder(CustomResourceResponderPort):
    """PUT one response document over HTTPS, refusing any non-HTTPS hop."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session or HttpsSession()

    def send(self, response: CustomResourceResponse) -> None:
        """Deliver the document; the pre-signed URL is never logged or placed in an error."""
        try:
            result = self._session.put(
                response.response_url,
                data=response.body,
                headers={"Content-Type": ""},
                timeout=_TIMEOUT_SECONDS,
            )
        except (requests.RequestException, InsecureUrlError) as error:
            raise DependencyError(
                RESPONSE_DELIVERY_FAILED,
                "The custom-resource response could not be delivered.",
                context={"cause": type(error).__name__},
            ) from None
        if result.status_code >= _SUCCESS_LIMIT:
            raise DependencyError(
                RESPONSE_DELIVERY_FAILED,
                "The custom-resource response was refused.",
                context={"status": result.status_code},
            )
