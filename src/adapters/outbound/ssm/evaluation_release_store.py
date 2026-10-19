# SPDX-License-Identifier: MIT-0
"""The evaluation gate's read, write, and delete of one release record (ADR-079).

The gate provider is the only writer of each family's record: a String parameter holding
canonical `EvaluationRelease.v1` JSON, overwritten in place. Reads go through the dispatcher's
own reader, so the gate and the dispatcher decode a record by one rule and a record the gate
reads back is exactly what the dispatcher will act on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from botocore.exceptions import BotoCoreError, ClientError

from adapters.inbound.conversation.contract_codec import contract_to_json
from adapters.outbound.aws_errors import aws_failure_context
from adapters.outbound.ssm.evaluation_release import SsmEvaluationReleaseReader
from domain.evaluation.release import EvaluationReleaseV1
from domain.shared_kernel.errors import DependencyError, ErrorCode
from ports.outbound.evaluation_gate import EvaluationReleaseStorePort

if TYPE_CHECKING:  # pragma: no cover - typing only
    from types_boto3_ssm.client import SSMClient

__all__ = ["EVALUATION_RELEASE_UNWRITABLE", "SsmEvaluationReleaseStore"]

EVALUATION_RELEASE_UNWRITABLE: Final = ErrorCode("EVALUATION", 11)
_NOT_FOUND: Final = "ParameterNotFound"


class SsmEvaluationReleaseStore(EvaluationReleaseStorePort):
    """One SSM client per process, shared by the reader it delegates to."""

    def __init__(self, client: SSMClient) -> None:
        self._client = client

    def read(self, parameter_name: str) -> EvaluationReleaseV1 | None:
        """Read the record through the dispatcher's reader; a missing parameter is None."""
        return SsmEvaluationReleaseReader((parameter_name,), self._client).read()[0]

    def write(self, parameter_name: str, release: EvaluationReleaseV1) -> None:
        """Overwrite the record as one String parameter."""
        try:
            self._client.put_parameter(
                Name=parameter_name,
                Value=contract_to_json(release),
                Type="String",
                Overwrite=True,
            )
        except (ClientError, BotoCoreError) as error:
            raise DependencyError(
                EVALUATION_RELEASE_UNWRITABLE,
                "PutParameter failed.",
                context={"operation": "PutParameter", **aws_failure_context(error)},
            ) from error

    def delete(self, parameter_name: str) -> None:
        """Delete the record; ParameterNotFound means it is already gone."""
        try:
            self._client.delete_parameter(Name=parameter_name)
        except (ClientError, BotoCoreError) as error:
            if (
                isinstance(error, ClientError)
                and error.response.get("Error", {}).get("Code") == _NOT_FOUND
            ):
                return
            raise DependencyError(
                EVALUATION_RELEASE_UNWRITABLE,
                "DeleteParameter failed.",
                context={"operation": "DeleteParameter", **aws_failure_context(error)},
            ) from error
