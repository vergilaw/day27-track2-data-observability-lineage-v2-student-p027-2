"""End-to-end check of the GX Suite -> ValidationDefinition -> Checkpoint -> Action flow."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
gx_validate = pytest.importorskip("gx.validate_orders")


def orders_df(rows: int = 5) -> pd.DataFrame:
    now = datetime.now(timezone.utc)
    return pd.DataFrame(
        [
            {
                "order_id": 5000 + i,
                "customer_id": f"C{i:04d}",
                "amount": 25.0 + i,
                "currency": "USD",
                "status": "completed",
                "created_at": (now - timedelta(minutes=12)).isoformat(),
                "updated_at": (now - timedelta(minutes=6)).isoformat(),
            }
            for i in range(rows)
        ]
    )


def run(df, tmp_path, **kwargs):
    return gx_validate.run_orders_checkpoint(df, result_path=tmp_path / "gx_result.json", **kwargs)


def test_healthy_batch_passes_the_checkpoint(tmp_path):
    payload = run(orders_df(), tmp_path)
    assert payload["decision"] == "pass"
    assert payload["failed_expectations"] == 0


def test_duplicate_primary_key_blocks_the_batch(tmp_path):
    df = orders_df()
    df.loc[1, "order_id"] = df.loc[0, "order_id"]
    payload = run(df, tmp_path)
    assert payload["decision"] == "block"
    failures = {f["expectation"] for f in payload["failures"]}
    assert "expect_column_values_to_be_unique" in failures


def test_partial_ingestion_trips_the_volume_floor(tmp_path):
    payload = run(orders_df(rows=2), tmp_path, min_rows=5)
    assert payload["decision"] == "block"
    assert any(f["expectation"] == "expect_table_row_count_to_be_between" for f in payload["failures"])


def test_severity_routing_writes_evidence_for_the_incident_report(tmp_path):
    df = orders_df()
    df.loc[0, "currency"] = "BTC"
    payload = run(df, tmp_path)
    assert (tmp_path / "gx_result.json").exists()
    invalid = next(f for f in payload["failures"] if f["expectation"] == "expect_column_values_to_be_in_set")
    assert invalid["severity"] == "critical"
    assert "BTC" in invalid["sample_unexpected_values"]
