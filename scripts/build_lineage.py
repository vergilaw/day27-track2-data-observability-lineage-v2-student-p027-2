#!/usr/bin/env python3
"""Build the lineage graph from the dbt manifest and answer blast-radius questions.

dbt knows about models and seeds; it does not know about the CEO dashboard or the
RAG/support-agent chain. Those consumer edges come from the curated graph in
`data/baseline/lineage_graph.json`, and the two are merged into one graph.

    make dbt && make lineage
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from observability.lineage import (
    blast_radius,
    extract_dbt_column_graph,
    extract_dbt_dataset_graph,
    merge_graphs,
    write_openlineage_events,
)

MANIFEST = ROOT / "dbt_project" / "target" / "manifest.json"
CURATED = ROOT / "data" / "baseline" / "lineage_graph.json"
OUT = ROOT / "reports" / "lineage_from_dbt.json"

# dbt names the seeds `orders`/`customers`; the curated graph calls the same
# assets `raw_orders`/`raw_customers`.
SEED_ALIASES = {"orders": "raw_orders", "customers": "raw_customers"}


def rename(graph: dict[str, list[str]]) -> dict[str, list[str]]:
    return {
        SEED_ALIASES.get(source, source): [SEED_ALIASES.get(t, t) for t in targets]
        for source, targets in graph.items()
    }


def main() -> int:
    if not MANIFEST.exists():
        print(f"{MANIFEST.relative_to(ROOT)} not found - run `make dbt` first.")
        return 1

    curated = json.loads(CURATED.read_text(encoding="utf-8"))
    dataset_graph = merge_graphs(
        rename(extract_dbt_dataset_graph(MANIFEST, short_names=True, models_only=True)),
        curated["dataset_lineage"],
    )
    column_graph = merge_graphs(extract_dbt_column_graph(MANIFEST), curated["column_lineage"])

    payload = {"dataset_lineage": dataset_graph, "column_lineage": column_graph}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    events = write_openlineage_events(
        dataset_graph, ROOT / "reports" / "openlineage_events.jsonl", column_graph=column_graph
    )

    print("=== LINEAGE (dbt manifest + curated consumers) ===")
    print(f"datasets: {len(dataset_graph)} | column edges: {sum(len(v) for v in column_graph.values())}")
    for start, column in (("stg_orders", "raw_orders.amount"), ("kb_documents", "kb_documents.content")):
        impact = blast_radius(payload, start, column=column)
        print(f"\nif {start} breaks:")
        print(f"  datasets  : {' -> '.join([start] + impact['downstream_assets'])}")
        print(f"  consumers : {', '.join(impact['terminal_consumers']) or 'none'}")
        print(f"  {column}:")
        print(f"    columns : {' -> '.join([column] + impact['downstream_columns'])}")
    print(f"\nwritten: {OUT.relative_to(ROOT)}, {events.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
