# SPDX-License-Identifier: MIT-0
"""The dispatcher's reader of the evaluation release records in Parameter Store (ADR-079).

Each family's gate provider is the only writer of its release record, a String parameter named
`.../release-<family>` holding canonical `EvaluationRelease.v1` JSON. A parameter that does not
exist reads as not enabled. A parameter that exists but cannot be read, decoded, or matched to
the family its name declares fails the read, so the dispatcher sends nothing that run.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, NoReturn, cast

import boto3

from adapters.inbound.conversation.contract_codec import decode_contract
from domain.evaluation.release import (
    CURRENT_EVALUATION_FAMILIES,
    EvaluationFamilyV1,
    EvaluationReleaseV1,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError
from ports.outbound.evaluation_release import EvaluationReleaseReaderPort

if TYPE_CHECKING:
    from types_boto3_ssm.client import SSMClient

EVALUATION_RELEASE_UNREADABLE = ErrorCode("EVALUATION", 6)
EVALUATION_RELEASE_INVALID = ErrorCode("EVALUATION", 7)
_RELEASE_PREFIX = "release-"
_RELEASE_SCHEMA = "EvaluationRelease.v1"
_MAX_PARAMETERS_PER_CALL = 10
"""GetParameters' own ceiling on names per request."""


def _family_of(name: str) -> EvaluationFamilyV1:
    """Read the current family a release parameter name declares in its last path segment."""
    segment = name.rpartition("/")[2]
    if not name.startswith("/") or not segment.startswith(_RELEASE_PREFIX):
        raise ValueError("evaluation release parameter name must end in release-<family>.")
    try:
        family = EvaluationFamilyV1(segment.removeprefix(_RELEASE_PREFIX))
    except ValueError:
        raise ValueError("evaluation release parameter names an unknown family.") from None
    if family not in CURRENT_EVALUATION_FAMILIES:
        raise ValueError(
            "evaluation release parameter names a retired family, which has no release record."
        )
    return family


class SsmEvaluationReleaseReader(EvaluationReleaseReaderPort):
    """Read every configured release record in one strongly typed GetParameters call."""

    def __init__(self, parameter_names: tuple[str, ...], client: SSMClient | None = None) -> None:
        if not parameter_names or len(parameter_names) > _MAX_PARAMETERS_PER_CALL:
            raise ValueError("evaluation release parameters must name one to ten parameters.")
        families = [_family_of(name) for name in parameter_names]
        if len(set(families)) != len(families) or len(set(parameter_names)) != len(families):
            raise ValueError("evaluation release parameters must name each family once.")
        self._names = parameter_names
        self._families = tuple(families)
        self._client = client

    @property
    def _ssm(self) -> SSMClient:
        if self._client is None:
            self._client = boto3.client("ssm")
        return self._client

    def read(self) -> tuple[EvaluationReleaseV1 | None, ...]:
        """Return each family's record in configured order; a missing parameter is `None`."""
        try:
            response = self._ssm.get_parameters(Names=list(self._names), WithDecryption=False)
        except Exception as error:
            raise DependencyError(
                EVALUATION_RELEASE_UNREADABLE,
                "Evaluation release records could not be read.",
                context={"cause": type(error).__name__},
            ) from None
        values = {
            parameter.get("Name"): parameter.get("Value")
            for parameter in response.get("Parameters", [])
        }
        missing = set(response.get("InvalidParameters", []))
        releases: list[EvaluationReleaseV1 | None] = []
        for name, family in zip(self._names, self._families, strict=True):
            if name in missing and name not in values:
                releases.append(None)
                continue
            value = values.get(name)
            if not isinstance(value, str):
                self._invalid("An evaluation release record was neither returned nor missing.")
            releases.append(self._decode(value, family))
        return tuple(releases)

    def _decode(self, value: str, family: EvaluationFamilyV1) -> EvaluationReleaseV1:
        try:
            document: object = json.loads(value)
            if (
                not isinstance(document, dict)
                or cast("dict[str, object]", document).get("schema_version") != _RELEASE_SCHEMA
            ):
                raise ValueError("evaluation release schema version is unsupported.")
            release = decode_contract(value, EvaluationReleaseV1)
        except TypeError, ValueError:
            self._invalid("An evaluation release record is malformed.")
        if release.family is not family:
            self._invalid("An evaluation release record names another family than its name.")
        return release

    @staticmethod
    def _invalid(description: str) -> NoReturn:
        raise InvariantError(EVALUATION_RELEASE_INVALID, description)
