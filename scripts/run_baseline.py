#!/usr/bin/env python3
"""One-shot reliability sweep over the current incoming data.

Prints the observability signals a data on-call engineer would look at first,
and writes the full evidence payload to `reports/latest_metrics.json`.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from observability.anomaly import detect_anomaly
from observability.distribution import detect_distribution_shift
from observability.lineage import blast_radius, write_openlineage_events
from observability.rag_metrics import detect_embedding_norm_shift, detect_text_length_shift
from observability.slo import calculate_slo, evaluate_multiwindow_burn
from src.contract_validator import (
    decide_action,
    failed_issues,
    load_contract,
    split_quarantine,
    validate_dataframe,
)
from src.io_utils import load_jsonl, load_yaml


def json_safe(value):
    """`float('inf')` is valid Python but not valid JSON - keep the file strict."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    return value


def null_rate_burn(history: pd.DataFrame, slo: dict) -> dict:
    """Multi-window burn rate for the "daily null rate stays under budget" SLI."""
    target = float(slo.get("target", 0.99))
    max_null_rate = float(slo.get("max_null_rate", 0.008))
    short_days = int(slo.get("short_window_days", 3))
    long_days = int(slo.get("long_window_days", 14))

    def burn(days: int) -> float:
        window = history["null_rate"].tail(days)
        bad = int((window > max_null_rate).sum())
        return calculate_slo(target, bad_events=bad, total_events=len(window))["burn_rate"]

    result = evaluate_multiwindow_burn(
        short_window_burn=burn(short_days), long_window_burn=burn(long_days)
    )
    result["sli"] = f"daily null_rate <= {max_null_rate}"
    result["windows_days"] = {"short": short_days, "long": long_days}
    return result


def main() -> None:
    config = load_yaml(ROOT / "lab_config.yaml")
    orders = pd.read_csv(ROOT / "data" / "incoming" / "orders.csv")
    baseline_orders = pd.read_csv(ROOT / "data" / "baseline" / "orders.csv")
    history = pd.read_csv(ROOT / "data" / "history" / "metrics_history.csv")

    # --- 1. Deterministic contract validation --------------------------------
    contract = load_contract(ROOT / "contracts" / "orders_contract.yaml")
    issues = validate_dataframe(orders, contract)
    failed = failed_issues(issues)
    critical_failed = failed_issues(issues, min_severity="critical")
    decision = decide_action(issues)
    clean_rows, quarantined_rows = split_quarantine(orders, contract)

    # --- 2. Volume anomaly with a same-weekday (seasonal) baseline -----------
    current_dow = datetime.now().weekday()
    segment = history.loc[history["day_of_week"] == current_dow, "row_count"].tail(8).tolist()
    full_history = history["row_count"].tail(14).tolist()
    row_result = detect_anomaly(
        len(orders),
        full_history,
        method="auto",
        context={
            "metric_name": "row_count",
            "day_of_week": current_dow,
            "same_segment_history": segment,
        },
    )
    # Kept for comparison: the naive detector the lab starts from.
    naive_result = detect_anomaly(len(orders), full_history, method="zscore")

    # --- 3. Distribution drift on the money column ---------------------------
    amount_shift = detect_distribution_shift(
        orders["amount"].tolist(), baseline_orders["amount"].tolist()
    )

    # --- 4. Freshness --------------------------------------------------------
    updated = pd.to_datetime(orders["updated_at"], utc=True, errors="coerce")
    freshness_minutes = (
        pd.Timestamp(datetime.now(timezone.utc)) - updated.max()
    ).total_seconds() / 60.0

    # --- 5. RAG / knowledge-base signals -------------------------------------
    docs = load_jsonl(ROOT / "data" / "incoming" / "kb_documents.jsonl")
    kb_contract = load_contract(ROOT / "contracts" / "kb_contract.yaml")
    kb_issues = validate_dataframe(pd.DataFrame(docs), kb_contract)
    kb_decision = decide_action(kb_issues)
    text_result = detect_text_length_shift(
        [d["content"] for d in docs], history["mean_text_length"].tail(14).tolist()
    )
    kb_published = pd.to_datetime([d["published_at"] for d in docs], utc=True, errors="coerce")
    kb_age_minutes = (
        pd.Timestamp(datetime.now(timezone.utc)) - kb_published.max()
    ).total_seconds() / 60.0
    # No embedding model in the lab: the history column stands in for today's
    # index statistics so the interface is exercised end to end.
    norm_history = history["embedding_norm_mean"].tolist()
    embedding_result = detect_embedding_norm_shift(norm_history[-1:], norm_history[:-1])

    # --- 6. SLO / error budget ----------------------------------------------
    bad = 1 if critical_failed else 0
    contract_slo = calculate_slo(
        float(config["slo"]["critical_contract_pass"]["target"]), bad_events=bad, total_events=1
    )
    burn = null_rate_burn(history, config["slo"].get("order_null_rate", {}))

    # --- 7. Lineage / blast radius ------------------------------------------
    with open(ROOT / "data" / "baseline" / "lineage_graph.json", "r", encoding="utf-8") as f:
        lineage = json.load(f)
    dataset_lineage = lineage["dataset_lineage"]
    column_lineage = lineage["column_lineage"]
    impact = blast_radius(lineage, "stg_orders", column="raw_orders.amount")
    kb_impact = blast_radius(lineage, "kb_documents", column="kb_documents.content")
    events_path = write_openlineage_events(
        dataset_lineage, ROOT / "reports" / "openlineage_events.jsonl", column_graph=column_lineage
    )

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "orders_rows": int(len(orders)),
        "failed_contract_checks": len(failed),
        "critical_contract_failures": len(critical_failed),
        "contract_decision": decision,
        "quarantine": {"clean_rows": int(len(clean_rows)), "quarantined_rows": int(len(quarantined_rows))},
        "row_count_anomaly": row_result,
        "row_count_anomaly_naive_zscore": naive_result,
        "amount_distribution_shift": amount_shift,
        "freshness_minutes": freshness_minutes,
        "kb_age_minutes": kb_age_minutes,
        "kb_contract_decision": kb_decision,
        "kb_failed_contract_checks": [
            {k: i[k] for k in ("check", "column", "severity", "details")} for i in failed_issues(kb_issues)
        ],
        "kb_blast_radius": kb_impact,
        "kb_text_length_signal": text_result,
        "kb_embedding_norm_signal": embedding_result,
        "contract_slo": contract_slo,
        "multiwindow_burn": burn,
        "sample_blast_radius_from_stg_orders": impact["downstream_assets"],
        "blast_radius": impact,
        "openlineage_events": str(events_path.relative_to(ROOT)),
    }
    out = ROOT / "reports" / "latest_metrics.json"
    out.write_text(json.dumps(json_safe(report), indent=2, default=str), encoding="utf-8")

    print("=== DATA RELIABILITY BASELINE ===")
    print(f"orders rows              : {len(orders)}")
    print(f"contract failed checks   : {len(failed)} (critical={len(critical_failed)})")
    print(f"contract decision        : {decision['action'].upper()} - {decision['reason']}")
    print(f"quarantine               : {len(quarantined_rows)} row(s) isolated, {len(clean_rows)} clean")
    print(
        f"row-count anomaly        : {row_result['is_anomaly']} "
        f"({row_result['method']}, score={row_result['score']:.2f}, baseline={row_result['baseline']})"
    )
    print(f"  naive z-score would say: {naive_result['is_anomaly']} (score={naive_result['score']:.2f})")
    print(f"  {row_result['reason']}")
    if "volume_spike" in row_result["reason"] and current_dow >= 5:
        print(
            "  note: the shipped fixture always writes ~600 rows, while the synthetic history "
            "models a weekend dip (~43%). On Sat/Sun the seasonal baseline correctly reports a "
            "spike against that history - it is a fixture artifact, not a pipeline fault."
        )
    print(f"amount distribution      : {amount_shift['is_anomaly']} ({amount_shift['verdict']}, psi={amount_shift['psi']:.3f})")
    print(f"freshness minutes        : {freshness_minutes:.1f} (kb {kb_age_minutes:.1f})")
    print(f"KB contract decision     : {kb_decision['action'].upper()} - {kb_decision['reason']}")
    for issue in failed_issues(kb_issues):
        print(f"  [{issue['severity']:<8}] {issue['check']}:{issue['column']} - {issue['details']}")
    print(f"KB length anomaly        : {text_result['is_anomaly']}")
    print(f"KB embedding drift       : {embedding_result['is_anomaly']}")
    print(
        f"error budget             : burn={contract_slo['burn_rate']:.2f}, "
        f"remaining={contract_slo['remaining_error_budget_fraction']:.1%}"
    )
    print(
        f"multi-window burn        : page={burn['page']} severity={burn['severity']} "
        f"action={burn['action']} (short={burn['short_window_burn']:.2f}, long={burn['long_window_burn']:.2f})"
    )
    print(f"blast radius (dataset)   : {', '.join(impact['downstream_assets'])}")
    print(f"blast radius (column)    : raw_orders.amount -> {', '.join(impact['downstream_columns'])}")
    print(f"blast radius (kb)        : kb_documents -> {', '.join(kb_impact['downstream_assets'])}")
    print(f"report                   : {out.relative_to(ROOT)} | lineage events: {events_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
