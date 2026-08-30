"""Lineage utilities: dataset-level + column-level blast radius.

Phase 4 deliverables implemented here:
- transitive dataset traversal (BFS, cycle safe),
- transitive *column* traversal (the starter only returned direct children),
- upstream traversal for root-cause search,
- dbt `manifest.json` parsing for both dataset and column graphs,
- optional OpenLineage-compatible event emission (no server required).
"""
from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

Graph = dict[str, list[str]]


# --------------------------------------------------------------------------- #
# loading helpers
# --------------------------------------------------------------------------- #
def load_graph(path: str | Path, kind: str = "dataset") -> Graph:
    """Load a lineage graph from JSON.

    `kind` selects `dataset_lineage` or `column_lineage` when the payload holds
    both. A plain `{node: [children]}` file is returned as-is.
    """
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return _select_graph(payload, kind)


def _select_graph(payload: Mapping[str, Any], kind: str = "dataset") -> Graph:
    key = "column_lineage" if kind == "column" else "dataset_lineage"
    other = "dataset_lineage" if kind == "column" else "column_lineage"
    if isinstance(payload, Mapping) and key in payload:
        return dict(payload[key])
    if isinstance(payload, Mapping) and other in payload and len(payload) == 1:
        # Only the sibling graph is present; caller passed the wrong kind.
        return dict(payload[other])
    return dict(payload)


def _normalize(graph: Mapping[str, Any] | None, kind: str) -> Graph:
    """Accept either a raw adjacency map or the full lineage payload."""
    if not graph:
        return {}
    return _select_graph(graph, kind)


# --------------------------------------------------------------------------- #
# traversal
# --------------------------------------------------------------------------- #
def _bfs(graph: Graph, start: str) -> list[str]:
    """Transitive children in BFS order, excluding `start`, cycle safe."""
    seen = {start}
    q: deque[str] = deque([start])
    out: list[str] = []
    while q:
        node = q.popleft()
        for child in graph.get(node, []) or []:
            if child in seen:
                continue
            seen.add(child)
            out.append(child)
            q.append(child)
    return out


def reverse_graph(graph: Graph) -> Graph:
    reversed_: Graph = {}
    for parent, children in graph.items():
        for child in children or []:
            reversed_.setdefault(child, []).append(parent)
    return reversed_


def get_downstream_assets(graph: Mapping[str, Any], start: str) -> list[str]:
    """Return transitive downstream assets in BFS order, excluding start."""
    return _bfs(_normalize(graph, "dataset"), start)


def get_upstream_assets(graph: Mapping[str, Any], start: str) -> list[str]:
    """Return transitive upstream assets (root-cause candidates) in BFS order."""
    return _bfs(reverse_graph(_normalize(graph, "dataset")), start)


def get_column_downstream(column_graph: Mapping[str, Any], start_column: str) -> list[str]:
    """Transitive column-level downstream traversal.

    The starter returned only direct children, so `raw_orders.amount` stopped at
    `stg_orders.amount_usd` instead of reaching `ceo_revenue_dashboard.revenue`.
    """
    return _bfs(_normalize(column_graph, "column"), start_column)


def get_column_upstream(column_graph: Mapping[str, Any], start_column: str) -> list[str]:
    return _bfs(reverse_graph(_normalize(column_graph, "column")), start_column)


def dataset_of(column: str) -> str:
    """`fct_daily_revenue.daily_revenue` -> `fct_daily_revenue`."""
    return column.rsplit(".", 1)[0] if "." in column else column


def datasets_touched_by_column(column_graph: Mapping[str, Any], start_column: str) -> list[str]:
    """Datasets impacted by a single column change, de-duplicated, BFS order."""
    out: list[str] = []
    for col in get_column_downstream(column_graph, start_column):
        ds = dataset_of(col)
        if ds not in out:
            out.append(ds)
    return out


def blast_radius(
    payload: Mapping[str, Any],
    start: str,
    *,
    column: str | None = None,
) -> dict[str, Any]:
    """Combined dataset + column blast radius for incident triage."""
    dataset_graph = _normalize(payload, "dataset")
    column_graph = _normalize(payload, "column")
    downstream = get_downstream_assets(dataset_graph, start)
    result: dict[str, Any] = {
        "start": start,
        "downstream_assets": downstream,
        "upstream_assets": get_upstream_assets(dataset_graph, start),
        "impacted_count": len(downstream),
        "terminal_consumers": [a for a in downstream if not dataset_graph.get(a)],
    }
    if column:
        cols = get_column_downstream(column_graph, column)
        result["start_column"] = column
        result["downstream_columns"] = cols
        result["column_impacted_datasets"] = datasets_touched_by_column(column_graph, column)
    return result


# --------------------------------------------------------------------------- #
# dbt manifest
# --------------------------------------------------------------------------- #
def _load_manifest(manifest_path: str | Path) -> dict[str, Any]:
    path = Path(manifest_path)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_dbt_dataset_graph(
    manifest_path: str | Path, *, short_names: bool = False, models_only: bool = False
) -> Graph:
    """Map each dbt node to the nodes that depend on it.

    `short_names=True` rewrites `model.project.stg_orders` -> `stg_orders`;
    `models_only=True` drops tests/seed-less nodes so the graph stays readable.
    """
    manifest = _load_manifest(manifest_path)
    if not manifest:
        return {}

    def keep(uid: str) -> bool:
        return not models_only or uid.split(".")[0] in {"model", "seed", "source", "snapshot", "exposure"}

    def name(uid: str) -> str:
        return uid.split(".")[-1] if short_names else uid

    graph: Graph = {}
    for parent, children in manifest.get("child_map", {}).items():
        if not keep(parent):
            continue
        graph[name(parent)] = [name(c) for c in children if keep(c)]
    return graph


def extract_dbt_column_graph(manifest_path: str | Path, *, short_names: bool = True) -> Graph:
    """Column lineage from a dbt manifest.

    dbt-core does not ship column-level lineage, so two sources are combined:

    1. **Declared edges** - `meta.upstream_columns: ['stg_orders.amount_usd']` on a
       column in `schema.yml`. Explicit, survives renames and aggregations, and is
       reviewed like any other code.
    2. **Name matching** - a parent column flows into a child column of the same
       name. Free, but blind to renames (`amount` -> `amount_usd`).

    A SQL parser (sqlglot) or OpenLineage column facets would remove the manual
    annotation; the trade-off is an extra dependency for the lab.
    """
    manifest = _load_manifest(manifest_path)
    if not manifest:
        return {}
    nodes = {**manifest.get("nodes", {}), **manifest.get("sources", {})}

    def name(uid: str) -> str:
        return uid.split(".")[-1] if short_names else uid

    graph: Graph = {}

    def add_edge(source: str, target: str) -> None:
        children = graph.setdefault(source, [])
        if target not in children:
            children.append(target)

    # 1. explicit annotations
    for uid, node in nodes.items():
        for column, spec in (node.get("columns") or {}).items():
            for upstream in ((spec.get("meta") or {}).get("upstream_columns") or []):
                add_edge(str(upstream), f"{name(uid)}.{column}")

    # 2. name matching between a node and its declared children
    for parent, children in manifest.get("child_map", {}).items():
        parent_node = nodes.get(parent)
        if not parent_node:
            continue
        parent_cols = set(parent_node.get("columns") or {})
        for child in children:
            child_node = nodes.get(child)
            if not child_node:
                continue
            for column in (child_node.get("columns") or {}):
                if column in parent_cols:
                    add_edge(f"{name(parent)}.{column}", f"{name(child)}.{column}")
    return graph


def merge_graphs(*graphs: Mapping[str, Any]) -> Graph:
    """Union of several adjacency maps, preserving order and de-duplicating."""
    merged: Graph = {}
    for graph in graphs:
        for source, targets in (graph or {}).items():
            children = merged.setdefault(source, [])
            for target in targets or []:
                if target not in children:
                    children.append(target)
    return merged


# --------------------------------------------------------------------------- #
# OpenLineage (optional bonus) - emit events without running a Marquez server
# --------------------------------------------------------------------------- #
def to_openlineage_events(
    dataset_graph: Mapping[str, Any],
    *,
    namespace: str = "data-reliability-lab",
    job_prefix: str = "build",
    column_graph: Mapping[str, Any] | None = None,
    run_id: str = "00000000-0000-0000-0000-000000000027",
) -> list[dict[str, Any]]:
    """Build OpenLineage `RunEvent` payloads (spec 1-0-5 shape).

    One COMPLETE event per output dataset, with a `columnLineage` output facet
    when a column graph is supplied. The payload can be POSTed to a Marquez
    `/api/v1/lineage` endpoint or just archived as incident evidence.
    """
    dataset_graph = _normalize(dataset_graph, "dataset")
    column_graph = _normalize(column_graph, "column") if column_graph else {}
    parents_of = reverse_graph(dataset_graph)
    now = datetime.now(timezone.utc).isoformat()

    events: list[dict[str, Any]] = []
    for output in sorted({c for children in dataset_graph.values() for c in children or []}):
        inputs = parents_of.get(output, [])
        fields: dict[str, Any] = {}
        for src_col, targets in column_graph.items():
            for target in targets or []:
                if dataset_of(target) != output:
                    continue
                field = target.rsplit(".", 1)[-1]
                entry = fields.setdefault(field, {"inputFields": [], "transformationType": "DIRECT"})
                entry["inputFields"].append(
                    {
                        "namespace": namespace,
                        "name": dataset_of(src_col),
                        "field": src_col.rsplit(".", 1)[-1],
                    }
                )
        output_facets = {"columnLineage": {"fields": fields}} if fields else {}
        events.append(
            {
                "eventType": "COMPLETE",
                "eventTime": now,
                "producer": "https://github.com/data-reliability-lab/observability",
                "schemaURL": "https://openlineage.io/spec/1-0-5/OpenLineage.json#/definitions/RunEvent",
                "run": {"runId": run_id},
                "job": {"namespace": namespace, "name": f"{job_prefix}.{output}"},
                "inputs": [{"namespace": namespace, "name": i} for i in inputs],
                "outputs": [{"namespace": namespace, "name": output, "facets": output_facets}],
            }
        )
    return events


def write_openlineage_events(
    dataset_graph: Mapping[str, Any],
    path: str | Path,
    *,
    column_graph: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> Path:
    """Write newline-delimited OpenLineage events (one JSON object per line)."""
    events = to_openlineage_events(dataset_graph, column_graph=column_graph, **kwargs)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return out
