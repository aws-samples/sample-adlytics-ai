# SPDX-License-Identifier: MIT-0
"""The execution explanation service: graph authority plus the status v2 projection (S2).

One request names one arm (`domain.operations.flow_explain`). The service resolves it to a root
flow, reads the authority mode, the root's graph records (projected candidates, each re-read from
the execution-graph store, plus the graphs the manifest's fingerprint names), and one page of
the root's receipts, each re-attested against the receipt item it copies. The root `received`
instant is the exact manifest version's `LastModified` (FR-2). Records of epoch 0 carry the
`shadow` label and never count as lifecycle progress (V-5).

Paging uses signed stateless cursors (`domain.operations.execution_cursor`). The oversize check
on an incoming cursor runs before any read; nothing here writes the graph store, the projection
or any cursor store (BF-5). Every port is read-only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, cast

from domain.operations.execution_cursor import (
    MAX_CURSOR_BYTES,
    CursorCapacityLimitedError,
    CursorMac,
    CursorState,
    admit_cursor,
    issue_cursor,
)
from domain.operations.execution_graph import graph_id as derive_graph_id
from domain.operations.flow_explain import (
    RECEIPTS_ROUTE,
    REQUIRED_STAGES,
    ExplanationArm,
    ExplanationRefusal,
    ExplanationRequest,
    ExplanationUnavailableError,
    StageEvidence,
    StageStatus,
    graph_stage_evidence,
    missing_required_stages,
    node_failures,
    require_document_bound,
    stage_rows,
)
from domain.operations.generated_execution_graphs import CONTRACT_REVISION, TEMPLATES
from domain.operations.graph_projection import (
    GraphProjectionRow,
    epoch_label,
    project_graph_item,
)
from domain.operations.ingestion_root import ManifestVersion, manifest_fingerprint, root_flow_id
from ports.outbound.execution_explanation import (
    GraphAuthorityReader,
    GraphProjectionReader,
    ManifestVersionReader,
)

__all__ = ["CURSOR_REVISION", "ExplanationDependencies", "explain"]

CURSOR_REVISION: Final = "status-v2/1"
_TEMPLATE: Final = next(iter(TEMPLATES))
_ROOT_AUTHORITY: Final = "raw_manifest_version_last_modified"
_GRAPH_AUTHORITY: Final = "execution_graph_store"


@dataclass(frozen=True, slots=True)
class ExplanationDependencies:
    """The read-only ports one explanation uses."""

    authority: GraphAuthorityReader
    projection: GraphProjectionReader
    manifests: ManifestVersionReader
    cursor_mac: CursorMac


@dataclass(frozen=True, slots=True)
class _Root:
    root_flow_id: str
    manifest: ManifestVersion | None
    received_at: datetime | None
    focus_graph_id: str | None


def _unavailable(refusal: ExplanationRefusal, description: str) -> ExplanationUnavailableError:
    return ExplanationUnavailableError(refusal, description)


def _manifest_root(key: str, version_id: str, deps: ExplanationDependencies) -> _Root:
    received = deps.manifests.last_modified(key, version_id)
    if received is None:
        raise _unavailable(ExplanationRefusal.MANIFEST_NOT_FOUND, "no such manifest version.")
    version = ManifestVersion(key, version_id)
    return _Root(root_flow_id(version), version, received, None)


def _resolve(request: ExplanationRequest, deps: ExplanationDependencies) -> _Root:
    if request.arm is ExplanationArm.MANIFEST_VERSION:
        return _manifest_root(str(request.manifest_key), str(request.version_id), deps)
    if request.arm is ExplanationArm.MANIFEST_KEY:
        versions = deps.manifests.version_ids(str(request.manifest_key))
        if not versions:
            raise _unavailable(ExplanationRefusal.MANIFEST_NOT_FOUND, "no such manifest key.")
        if len(versions) != 1:
            raise _unavailable(
                ExplanationRefusal.AMBIGUOUS_MANIFEST, "the manifest key has several versions."
            )
        return _manifest_root(str(request.manifest_key), next(iter(versions)), deps)
    if request.arm is ExplanationArm.FLOW:
        flow = str(request.flow_id)
        receipts, _ = deps.projection.receipts(flow, None, 1)
        if not receipts and not deps.projection.graphs(flow):
            raise _unavailable(ExplanationRefusal.FLOW_NOT_FOUND, "no such root flow.")
        return _Root(flow, None, None, None)
    graph = deps.authority.graph(str(request.graph_id))
    root = None if graph is None else graph.get("root_flow_id")
    nodes = () if graph is None else cast("Sequence[object]", graph.get("nodes") or ())
    if not isinstance(root, str) or (
        request.arm is ExplanationArm.NODE and request.node not in nodes
    ):
        raise _unavailable(ExplanationRefusal.FLOW_NOT_FOUND, "no such graph or node.")
    return _Root(root, None, None, request.graph_id)


def _graph_document(
    graph_name: str, item: Mapping[str, object], projected: bool
) -> dict[str, object]:
    epoch = item.get("authority_epoch")
    epoch_value = epoch if isinstance(epoch, int) else -1
    return {
        "graph_id": graph_name,
        "authority": _GRAPH_AUTHORITY,
        "authority_epoch": epoch_value,
        "label": epoch_label(epoch_value),
        "state": item.get("state"),
        "template": item.get("template"),
        "nodes": [str(node) for node in cast("Sequence[object]", item.get("nodes") or ())],
        "fingerprint": item.get("fingerprint"),
        "receipt_request_id": item.get("receipt_request_id"),
        "projected": projected,
    }


def _graphs(
    root: _Root, projected_rows: Sequence[GraphProjectionRow], deps: ExplanationDependencies
) -> tuple[list[dict[str, object]], list[str]]:
    projected = {str(row.document.get("graph_id")): row for row in projected_rows}
    candidates = list(projected)
    if root.manifest is not None:
        fingerprint = manifest_fingerprint(root.manifest, _TEMPLATE, int(CONTRACT_REVISION))
        candidates.append(derive_graph_id(fingerprint, 0, 0))
        epoch1 = deps.authority.epoch1_graph_id(fingerprint)
        if epoch1 is not None:
            candidates.append(epoch1)
    if root.focus_graph_id is not None:
        candidates.append(root.focus_graph_id)
    unique = sorted(set(candidates))
    found = deps.authority.items([(f"GRAPH#{name}", "GRAPH") for name in unique])
    documents: list[dict[str, object]] = []
    gaps: list[str] = []
    for name in unique:
        item = found.get((f"GRAPH#{name}", "GRAPH"))
        if item is None or item.get("root_flow_id") != root.root_flow_id:
            if name in projected:
                gaps.append(f"graph_projection_without_authority:{name}")
            continue
        documents.append(_graph_document(name, item, name in projected))
    documents.sort(
        key=lambda entry: (int(cast("int", entry["authority_epoch"])), str(entry["graph_id"]))
    )
    return documents, gaps


def _receipt_documents(
    rows: Sequence[GraphProjectionRow], deps: ExplanationDependencies
) -> tuple[list[dict[str, object]], list[str]]:
    found = deps.authority.items([(row.authority_pk, row.authority_sk) for row in rows])
    documents: list[dict[str, object]] = []
    gaps: list[str] = []
    for row in rows:
        item = found.get((row.authority_pk, row.authority_sk))
        current = None if item is None else project_graph_item(item)
        attested = current is not None and current.digest == row.digest
        if not attested:
            gaps.append(f"receipt_projection_unattested:{row.document.get('request_id')}")
        documents.append(
            {
                **dict(row.document),
                "authority": _GRAPH_AUTHORITY,
                "label": epoch_label(row.authority_epoch),
                "attested": attested,
            }
        )
    return documents, gaps


def _focus(root: _Root, graphs: Sequence[Mapping[str, object]], node: str | None) -> object:
    if root.focus_graph_id is None:
        return None
    template = next(iter(TEMPLATES.values()))
    declared = {str(entry["node"]): entry for entry in template["nodes"]}
    focus: dict[str, object] = {"graph_id": root.focus_graph_id}
    if node is not None:
        entry = declared[node]
        focus.update(
            {
                "node": node,
                "after": [str(name) for name in entry["after"]],
                "phase": entry.get("phase"),
                # S2 records no node items: a shadow graph never dispatches (V-5).
                "node_state": None,
            }
        )
    focus["graph_state"] = next(
        (graph["state"] for graph in graphs if graph["graph_id"] == root.focus_graph_id), None
    )
    return focus


def explain(request: ExplanationRequest, deps: ExplanationDependencies) -> dict[str, object]:
    """Return one bounded explanation tree, or raise its typed refusal."""
    if request.cursor is not None and len(request.cursor.encode("utf-8")) > MAX_CURSOR_BYTES:
        raise CursorCapacityLimitedError("the continuation exceeds the generated cursor bound.")
    root = _resolve(request, deps)
    filter_ = {"root_flow_id": root.root_flow_id}
    after = None
    if request.cursor is not None:
        state = admit_cursor(
            request.cursor,
            route=RECEIPTS_ROUTE,
            filter_values=filter_,
            revision=CURSOR_REVISION,
            signer=deps.cursor_mac,
        )
        after = state.last_key.get("sort_key")
    mode = deps.authority.read_mode()
    graph_rows = deps.projection.graphs(root.root_flow_id)
    graphs, graph_gaps = _graphs(root, graph_rows, deps)
    rows, more = deps.projection.receipts(root.root_flow_id, after, request.limit)
    # S6 part 2 (§2.10): the N1 stages of a graph run come from its NODE, CLAIM and SERVING rows.
    executions, executions_cut = deps.projection.executions(root.root_flow_id)
    receipts, receipt_gaps = _receipt_documents(rows, deps)
    next_cursor = None
    if more and rows:
        next_cursor = issue_cursor(
            CursorState(RECEIPTS_ROUTE, filter_, CURSOR_REVISION, {"sort_key": rows[-1].sort_key}),
            deps.cursor_mac,
        )
    label = "graph" if any(graph["authority_epoch"] == 1 for graph in graphs) else "shadow"
    evidence = [
        StageEvidence(
            stage=REQUIRED_STAGES[0],
            status=StageStatus.APPLIED,
            activity_id=root.root_flow_id,
            authority=_ROOT_AUTHORITY,
            occurred_at=root.received_at,
            label=label,
        ),
        *graph_stage_evidence([*graph_rows, *rows, *executions]),
    ]
    stages, stage_gaps = stage_rows(evidence, root.received_at)
    if executions_cut:
        stage_gaps.append("execution_projection_truncated")
    missing = missing_required_stages(evidence)
    gaps = sorted({*graph_gaps, *receipt_gaps, *stage_gaps})
    manifest = (
        None
        if root.manifest is None
        else {"key": root.manifest.key, "version_id": root.manifest.version_id}
    )
    document: dict[str, object] = {
        "schema_version": "2",
        "evaluation_mode": "observation_only",
        "reference": {"arm": request.arm.value},
        "root_flow_id": root.root_flow_id,
        "manifest": manifest,
        "root_clock": {
            "started_at": None if root.received_at is None else root.received_at.isoformat(),
            "authority": _ROOT_AUTHORITY,
        },
        "authority_mode": {"mode": mode.mode.value, "revision": mode.revision},
        "graphs": graphs,
        "focus": _focus(root, graphs, request.node),
        "stages": stages,
        "missing_required_stages": missing,
        "complete": not missing and not gaps,
        "gaps": gaps,
        "receipts": receipts,
        "next_cursor": next_cursor,
        "served_revision": None,
        # Slice 7 (D35): each FAILED node's reason, code, last batch and remedy.
        "node_failures": node_failures([*graph_rows, *rows, *executions]),
    }
    require_document_bound(document, len(graphs))
    return document
