"""Regression tests for the upgrades made on top of the starter code.

Each test states the failure the *starter* implementation missed, so the
evidence for every change is executable rather than narrative.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.contract_validator import (
    decide_action,
    failed_issues,
    load_contract,
    split_quarantine,
    validate_dataframe,
)
from student_api import (
    column_downstream,
    detect_distribution,
    detect_metric,
    multiwindow_burn,
    rag_embedding_shift,
    rag_length_shift,
    slo_status,
    validate_orders,
)

ROOT = Path(__file__).resolve().parents[1]
ORDERS_CONTRACT = ROOT / "contracts" / "orders_contract.yaml"


def orders_df(rows: int = 3, minutes_old: int = 5) -> pd.DataFrame:
    now = datetime.now(timezone.utc)
    return pd.DataFrame(
        [
            {
                "order_id": 1000 + i,
                "customer_id": f"C{i:04d}",
                "amount": 10.0 + i,
                "currency": "USD",
                "status": "completed",
                "created_at": (now - timedelta(minutes=minutes_old + 5)).isoformat(),
                "updated_at": (now - timedelta(minutes=minutes_old)).isoformat(),
            }
            for i in range(rows)
        ]
    )


# --------------------------------------------------------------------------- #
# Phase 1 - contract
# --------------------------------------------------------------------------- #
def test_type_drift_is_detected_as_critical():
    df = orders_df()
    df["amount"] = df["amount"].astype(object)
    df.loc[0, "amount"] = "N/A"
    failures = failed_issues(validate_orders(df, ORDERS_CONTRACT))
    assert any(f["check"] == "type" and f["column"] == "amount" for f in failures)
    assert all(f["action"] == "block" for f in failures if f["severity"] == "critical")


def test_stale_batch_fails_freshness_within_the_wall_clock_window():
    """The 3h stale_kb-style delay the previous 2h "test fixture" heuristic hid."""
    df = orders_df(minutes_old=180)
    failures = failed_issues(validate_orders(df, ORDERS_CONTRACT))
    assert any(f["check"] == "freshness" for f in failures)


def test_fresh_batch_passes_freshness():
    assert not [f for f in failed_issues(validate_orders(orders_df(), ORDERS_CONTRACT)) if f["check"] == "freshness"]


def test_severity_drives_the_pipeline_action():
    clean = validate_orders(orders_df(), ORDERS_CONTRACT)
    assert decide_action(clean)["action"] == "pass"

    duplicated = orders_df()
    duplicated.loc[1, "order_id"] = duplicated.loc[0, "order_id"]
    assert decide_action(validate_orders(duplicated, ORDERS_CONTRACT))["action"] == "block"


def test_quarantine_isolates_only_bad_rows():
    df = orders_df(rows=4)
    df.loc[2, "currency"] = "BTC"
    df.loc[3, "amount"] = -1.0
    clean, quarantined = split_quarantine(df, load_contract(ORDERS_CONTRACT))
    assert len(clean) == 2
    assert sorted(quarantined["order_id"]) == [1002, 1003]


def test_kb_contract_uses_the_fields_alias_and_min_length():
    contract = load_contract(ROOT / "contracts" / "kb_contract.yaml")
    docs = pd.DataFrame(
        [
            {
                "doc_id": "refund",
                "version": 1,
                "effective_at": "2026-08-01T00:00:00Z",
                "published_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                "source_uri": "policy/refund.pdf",
                "content": "too short",
            }
        ]
    )
    failures = failed_issues(validate_dataframe(docs, contract))
    assert any(f["check"] == "length" and f["column"] == "content" for f in failures)


# --------------------------------------------------------------------------- #
# Phase 3 - anomaly detection
# --------------------------------------------------------------------------- #
def test_mad_catches_the_drop_that_an_outlier_hides_from_zscore():
    history = [1000, 1010, 995, 5000, 1004, 1012, 998]  # one past spike inflates std
    assert detect_metric(500, history, method="zscore")["is_anomaly"] is False
    assert detect_metric(500, history, method="auto", context={"metric_name": "row_count"})["is_anomaly"] is True


def test_same_weekday_baseline_prevents_a_weekend_false_positive():
    weekday_history = [600, 610, 595, 608, 604, 612, 598]
    saturday_history = [250, 262, 255, 248, 259, 251]
    context = {"metric_name": "row_count", "day_of_week": 5, "same_segment_history": saturday_history}
    assert detect_metric(255, weekday_history, method="zscore")["is_anomaly"] is True  # naive alarm
    assert detect_metric(255, weekday_history, method="auto", context=context)["is_anomaly"] is False


def test_seasonal_baseline_still_catches_a_real_weekend_drop():
    context = {
        "metric_name": "row_count",
        "day_of_week": 5,
        "same_segment_history": [250, 262, 255, 248, 259, 251],
    }
    assert detect_metric(60, [600, 610, 595, 608, 604, 612, 598], method="auto", context=context)["is_anomaly"] is True


def test_constant_history_no_longer_disables_the_detector():
    """MAD = 0 used to return "not an anomaly" for any value."""
    assert detect_metric(300, [1000] * 7, method="mad")["is_anomaly"] is True
    assert detect_metric(1000, [1000] * 7, method="mad")["is_anomaly"] is False


def test_known_event_suppresses_the_alert():
    result = detect_metric(
        5000, [1000, 1010, 995, 1008, 1004, 1012, 998],
        method="auto", context={"metric_name": "row_count", "known_event": "black_friday"},
    )
    assert result["is_anomaly"] is False


def test_trending_metric_does_not_alarm_on_every_new_high():
    trend = [1000, 1050, 1100, 1150, 1200, 1250, 1300]
    context = {"metric_name": "revenue", "trend": "growing"}
    assert detect_metric(1350, trend, method="auto", context=context)["is_anomaly"] is False
    assert detect_metric(700, trend, method="auto", context=context)["is_anomaly"] is True


# --------------------------------------------------------------------------- #
# distribution drift
# --------------------------------------------------------------------------- #
def test_shape_shift_with_identical_means_is_detected():
    """Mean ratio - the starter's only signal - is exactly 1.0 here."""
    rng = np.random.default_rng(27)
    baseline = rng.normal(100, 10, 300)
    current = np.concatenate([rng.normal(50, 5, 150), rng.normal(150, 5, 150)])
    assert abs(current.mean() / baseline.mean() - 1) < 0.05
    assert detect_distribution(current.tolist(), baseline.tolist())["is_anomaly"] is True


def test_same_distribution_is_not_flagged():
    rng = np.random.default_rng(27)
    assert detect_distribution(rng.normal(100, 10, 200).tolist(), rng.normal(100, 10, 200).tolist())["is_anomaly"] is False


# --------------------------------------------------------------------------- #
# Phase 4 - lineage
# --------------------------------------------------------------------------- #
def test_column_lineage_is_transitive():
    graph = {
        "raw_orders.amount": ["stg_orders.amount_usd"],
        "stg_orders.amount_usd": ["fct_daily_revenue.daily_revenue"],
        "fct_daily_revenue.daily_revenue": ["ceo_revenue_dashboard.revenue"],
    }
    assert column_downstream(graph, "raw_orders.amount") == [
        "stg_orders.amount_usd",
        "fct_daily_revenue.daily_revenue",
        "ceo_revenue_dashboard.revenue",
    ]


def test_column_lineage_handles_cycles_and_diamonds():
    graph = {"a.x": ["b.x", "c.x"], "b.x": ["d.x"], "c.x": ["d.x"], "d.x": ["a.x"]}
    assert column_downstream(graph, "a.x") == ["b.x", "c.x", "d.x"]


def test_repository_lineage_graph_reaches_the_dashboard():
    from observability.lineage import load_graph

    graph = load_graph(ROOT / "data" / "baseline" / "lineage_graph.json", kind="column")
    assert "ceo_revenue_dashboard.revenue" in column_downstream(graph, "raw_orders.amount")


# --------------------------------------------------------------------------- #
# Phase 5 - SLO
# --------------------------------------------------------------------------- #
def test_sustained_fast_burn_pages():
    result = multiwindow_burn(short_window_burn=20.0, long_window_burn=15.0)
    assert result["page"] is True
    assert result["severity"] == "critical"


def test_transient_spike_does_not_page():
    result = multiwindow_burn(short_window_burn=20.0, long_window_burn=0.4)
    assert result["page"] is False
    assert "transient_spike" in result["reason"]


def test_recovering_incident_does_not_page_but_leaves_a_ticket():
    result = multiwindow_burn(short_window_burn=0.2, long_window_burn=18.0)
    assert result["page"] is False
    assert result["action"] == "ticket"


def test_slow_sustained_burn_tickets_instead_of_paging():
    result = multiwindow_burn(short_window_burn=3.5, long_window_burn=3.2)
    assert (result["page"], result["action"]) == (False, "ticket")


def test_healthy_burn_is_quiet():
    assert multiwindow_burn(short_window_burn=0.3, long_window_burn=0.2)["page"] is False


def test_error_budget_exhaustion_estimate():
    status = slo_status(0.99, bad_events=2, total_events=100)
    assert status["burn_rate"] == pytest.approx(2.0)
    assert status["hours_to_exhaustion"] == pytest.approx(30 * 24 / 2.0)


# --------------------------------------------------------------------------- #
# RAG metrics
# --------------------------------------------------------------------------- #
def test_embedding_norm_collapse_is_detected():
    assert rag_embedding_shift([0.20, 0.21, 0.19], [1.0, 1.0, 0.99, 1.01, 1.0, 0.98, 1.02])["is_anomaly"] is True


def test_stable_embedding_norms_are_not_flagged():
    assert rag_embedding_shift([1.00, 0.99, 1.01], [1.0, 1.0, 0.99, 1.01, 1.0, 0.98, 1.02])["is_anomaly"] is False


def test_embedding_model_switch_to_unit_norms_is_detected():
    """Degenerate baseline (MAD = 0) used to silence the check entirely."""
    assert rag_embedding_shift([1.0] * 5, [12.4] * 8)["is_anomaly"] is True


def test_topic_switch_with_identical_norms_needs_the_vector_signal():
    from observability.rag_metrics import detect_embedding_shift

    rng = np.random.default_rng(11)
    def unit(vectors):
        return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)

    baseline = unit(np.column_stack([np.full(40, 3.0), rng.normal(0, 0.3, (40, 15))]))
    same_topic = unit(np.column_stack([np.full(10, 3.0), rng.normal(0, 0.3, (10, 15))]))
    other_topic = unit(np.column_stack([np.zeros(10), np.full(10, 3.0), rng.normal(0, 0.3, (10, 14))]))

    norms_only = rag_embedding_shift(
        np.linalg.norm(other_topic, axis=1).tolist(), np.linalg.norm(baseline, axis=1).tolist()
    )
    assert norms_only["is_anomaly"] is False  # every vector is unit length
    assert detect_embedding_shift(same_topic, baseline)["is_anomaly"] is False
    assert detect_embedding_shift(other_topic, baseline)["is_anomaly"] is True


def test_kb_text_collapse_is_detected():
    assert rag_length_shift(["x y", "a b c"], [40, 42, 39, 41, 43, 40, 42])["is_anomaly"] is True


def test_retrieval_metrics_expose_a_quality_regression():
    from observability.rag_metrics import evaluate_retrieval

    healthy = evaluate_retrieval([["refund", "shipping"], ["privacy", "x"]], [["refund"], ["privacy"]], k=2)
    broken = evaluate_retrieval([["x", "y"], ["z", "w"]], [["refund"], ["privacy"]], k=2)
    assert healthy["recall_at_k"] == 1.0 and healthy["mrr"] == 1.0
    assert broken["recall_at_k"] == 0.0


# --------------------------------------------------------------------------- #
# dbt-derived lineage (needs `make dbt` to have produced a manifest)
# --------------------------------------------------------------------------- #
MANIFEST = ROOT / "dbt_project" / "target" / "manifest.json"


@pytest.mark.skipif(not MANIFEST.exists(), reason="run `make dbt` to generate the manifest")
def test_column_lineage_is_derived_from_the_dbt_manifest():
    from observability.lineage import extract_dbt_column_graph, extract_dbt_dataset_graph

    datasets = extract_dbt_dataset_graph(MANIFEST, short_names=True, models_only=True)
    assert "fct_daily_revenue" in datasets["stg_orders"]

    columns = extract_dbt_column_graph(MANIFEST)
    assert columns["stg_orders.amount_usd"] == ["fct_daily_revenue.daily_revenue"]
    # The rename amount -> amount_usd is only visible through the declared
    # `meta.upstream_columns`, not through name matching.
    assert columns["raw_orders.amount"] == ["stg_orders.amount_usd"]


@pytest.mark.skipif(not MANIFEST.exists(), reason="run `make dbt` to generate the manifest")
def test_openlineage_events_carry_the_column_lineage_facet(tmp_path):
    import json

    from observability.lineage import write_openlineage_events

    graph = json.loads((ROOT / "data" / "baseline" / "lineage_graph.json").read_text())
    path = write_openlineage_events(
        graph["dataset_lineage"], tmp_path / "events.jsonl", column_graph=graph["column_lineage"]
    )
    events = [json.loads(line) for line in path.read_text().splitlines()]
    revenue = next(e for e in events if e["outputs"][0]["name"] == "fct_daily_revenue")
    facet = revenue["outputs"][0]["facets"]["columnLineage"]["fields"]["daily_revenue"]
    assert facet["inputFields"][0]["field"] == "amount_usd"
