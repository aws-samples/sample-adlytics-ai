# SPDX-License-Identifier: MIT-0
"""Remove fictional probe markers from the synthetic personas' long-term Memory.

P5 asks `probe-a` to label every report with a fresh `ZEPHYRINE-<hex>` codename, and long-term
Memory keeps it as a live preference. Left in place, every later `probe-a` turn labels its answer
with it: on 2026-09-30 the agent regression run scored those labels as unsupported claims. Only
records matching one fictional pattern are removed, and only under the cross-session namespaces
the Harness retrieves, read from its retrieval configuration rather than named here, so one code
path stays right on both sides of a strategy change.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, cast

from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from types_boto3_bedrock_agentcore import BedrockAgentCoreClient
    from types_boto3_bedrock_agentcore_control import BedrockAgentCoreControlClient

__all__ = [
    "CODENAME",
    "cross_session_namespaces",
    "semantic_namespace",
    "settle_codenames",
    "sweep_codenames",
]

CODENAME = re.compile(r"ZEPHYRINE-[0-9a-f]{6}", re.IGNORECASE)
_PAGE_LIMIT = 20
_POLL_INTERVAL_SECONDS = 30
# Long-term extraction is asynchronous: a statement or a recall that echoed the codename can
# produce a record after the last case returned. A clean window this long ends the teardown.
_SETTLE_SECONDS = 120
_MAX_SECONDS = 600
_SEMANTIC_PREFIXES = ("/semantics/", "/context/")


def cross_session_namespaces(
    control_client: BedrockAgentCoreControlClient, harness_id: str
) -> tuple[str, ...]:
    """Return the namespace templates the Harness retrieves across sessions, sorted.

    The retrieval configuration maps each namespace template to its strategy; a template keyed
    by `{sessionId}` is one conversation's summary and is never recalled in another session.
    Raises `ValueError` unless there is a preference namespace and exactly one semantic one.
    """
    harness = control_client.get_harness(harnessId=harness_id)["harness"]
    configuration = cast(
        "dict[str, object]",
        cast("dict[str, object]", harness.get("memory", {})).get(
            "agentCoreMemoryConfiguration", {}
        ),
    )
    retrieval = cast("dict[str, object] | None", configuration.get("retrievalConfig"))
    if not retrieval:
        raise ValueError("the Harness memory has no retrieval configuration")
    namespaces = tuple(sorted(key for key in retrieval if "{sessionId}" not in key))
    if not any(key.startswith("/preferences/") for key in namespaces):
        raise ValueError("the Harness retrieves no /preferences/ namespace")
    semantic = [key for key in namespaces if key.startswith(_SEMANTIC_PREFIXES)]
    if len(semantic) != 1:
        raise ValueError(
            "the Harness must retrieve exactly one /semantics/ or /context/ namespace, "
            f"found {len(semantic)}"
        )
    return namespaces


def semantic_namespace(namespaces: Sequence[str]) -> str:
    """Return the one cross-session semantic namespace among `cross_session_namespaces`."""
    semantic = [key for key in namespaces if key.startswith(_SEMANTIC_PREFIXES)]
    if len(semantic) != 1:
        raise ValueError(
            "the Harness must retrieve exactly one /semantics/ or /context/ namespace, "
            f"found {len(semantic)}"
        )
    return semantic[0]


def sweep_codenames(  # noqa: PLR0917 - the sweep target: memory, actors, namespaces
    client: BedrockAgentCoreClient,
    memory_id: str,
    actor_ids: Sequence[str],
    namespace_templates: Sequence[str],
    *,
    pattern: re.Pattern[str] = CODENAME,
) -> int:
    """Delete every record matching `pattern` under each actor's given namespaces.

    Lists by `namespacePath` with a trailing slash, the form the stored namespaces take; a
    `namespace` filter returned nothing for records that `namespacePath` listed.
    """
    deleted = 0
    seen: set[str] = set()
    for actor_id in actor_ids:
        for template in namespace_templates:
            namespace_path = f"{template.replace('{actorId}', actor_id)}/"
            next_token: str | None = None
            for _ in range(_PAGE_LIMIT):
                request: dict[str, object] = {
                    "memoryId": memory_id,
                    "namespacePath": namespace_path,
                    "maxResults": 100,
                }
                if next_token is not None:
                    request["nextToken"] = next_token
                response = client.list_memory_records(**request)  # type: ignore[arg-type]
                for record in response.get("memoryRecordSummaries", []):
                    record_id = record["memoryRecordId"]
                    text = record.get("content", {}).get("text", "")
                    if record_id in seen or not pattern.search(text):
                        continue
                    seen.add(record_id)
                    try:
                        client.delete_memory_record(memoryId=memory_id, memoryRecordId=record_id)
                    except ClientError as error:
                        # Listed under more than one namespace, or already consolidated away.
                        if (
                            error.response.get("Error", {}).get("Code")
                            != "ResourceNotFoundException"
                        ):
                            raise
                        continue
                    deleted += 1
                next_token = response.get("nextToken")
                if not next_token:
                    break
    return deleted


def settle_codenames(  # noqa: PLR0917 - the sweep target plus explicit settle bounds
    client: BedrockAgentCoreClient,
    memory_id: str,
    actor_ids: Sequence[str],
    namespace_templates: Sequence[str],
    *,
    pattern: re.Pattern[str] = CODENAME,
    settle_seconds: int = _SETTLE_SECONDS,
    poll_seconds: int = _POLL_INTERVAL_SECONDS,
    max_seconds: int = _MAX_SECONDS,
) -> int:
    """Sweep, then keep sweeping until one full settle window passes with nothing new to delete.

    Raises `TimeoutError` if records keep appearing past `max_seconds`, so a caller never reports
    a clean memory it did not observe.
    """
    started = time.monotonic()
    deleted = sweep_codenames(client, memory_id, actor_ids, namespace_templates, pattern=pattern)
    clean_since = started
    while time.monotonic() - clean_since < settle_seconds:
        if time.monotonic() - started > max_seconds:
            raise TimeoutError("codename records kept appearing past the settle bound")
        time.sleep(poll_seconds)
        found = sweep_codenames(client, memory_id, actor_ids, namespace_templates, pattern=pattern)
        if found:
            deleted += found
            clean_since = time.monotonic()
    return deleted
