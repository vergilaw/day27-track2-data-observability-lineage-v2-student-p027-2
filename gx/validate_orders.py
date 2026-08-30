#!/usr/bin/env python3
"""Great Expectations Core 1.21 validation flow for the orders contract.

The starter validated four standalone expectations against a batch. This builds
the full production shape instead:

    contract YAML -> ExpectationSuite -> ValidationDefinition -> Checkpoint
                                                                    |
                                                        severity-routing Action
                                                        (block / quarantine / warn)

The contract file stays the single source of truth: expectations are generated
from it, so a contract change cannot silently drift away from the GX suite.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import great_expectations as gx
    from great_expectations.checkpoint import Checkpoint
    from great_expectations.checkpoint.actions import ActionContext, ValidationAction
    from great_expectations.checkpoint.checkpoint import CheckpointResult
    from great_expectations.core.expectation_suite import ExpectationSuite
    from great_expectations.core.validation_definition import ValidationDefinition
except ImportError as exc:  # friendlier classroom failure
    raise SystemExit("great_expectations is not installed. Run: pip install -r requirements.txt") from exc

from src.contract_validator import SEVERITY_ACTION, contract_columns, load_contract

TYPE_LISTS = {
    "integer": ["int", "int8", "int16", "int32", "int64", "Int64"],
    "number": ["int", "int8", "int16", "int32", "int64", "Int64", "float", "float32", "float64", "Float64"],
    "string": ["str", "object", "string"],
}
DATETIME_TYPES = ["datetime64[ns]", "datetime64[us]", "datetime64", "Timestamp", "DATETIME"]
RESULT_PATH = ROOT / "reports" / "gx_validation_result.json"


# --------------------------------------------------------------------------- #
# custom Action: route failures by severity
# --------------------------------------------------------------------------- #
class SeverityRoutingAction(ValidationAction):
    """Turn a Checkpoint result into a pipeline decision.

    `critical` -> block the batch, `warning` -> quarantine and continue,
    `info` -> warn only. The decision plus the offending values are written to
    `reports/gx_validation_result.json` so the incident report can cite evidence
    instead of a screenshot.
    """

    type: Literal["severity_routing"] = "severity_routing"
    name: str = "severity_routing"
    output_path: str = str(RESULT_PATH)

    def run(self, checkpoint_result: CheckpointResult, action_context: ActionContext | None = None) -> dict:
        failures: list[dict[str, Any]] = []
        total = 0
        for validation_result in checkpoint_result.run_results.values():
            for result in validation_result.results:
                total += 1
                if result.success:
                    continue
                config = result.expectation_config
                failures.append(
                    {
                        "expectation": config.type,
                        "column": config.kwargs.get("column"),
                        "severity": (config.meta or {}).get("severity", "warning"),
                        "unexpected_count": result.result.get("unexpected_count"),
                        "unexpected_percent": result.result.get("unexpected_percent"),
                        "sample_unexpected_values": result.result.get("partial_unexpected_list"),
                    }
                )

        by_severity = {s: [f for f in failures if f["severity"] == s] for s in ("critical", "warning", "info")}
        if by_severity["critical"]:
            decision, reason = "block", "critical expectation(s) failed - do not publish this batch"
        elif by_severity["warning"]:
            decision, reason = "quarantine", "warning expectation(s) failed - isolate bad rows, continue"
        elif by_severity["info"]:
            decision, reason = "warn", "info expectation(s) failed - log only"
        else:
            decision, reason = "pass", "all expectations passed"

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "checkpoint_success": bool(checkpoint_result.success),
            "expectations_evaluated": total,
            "failed_expectations": len(failures),
            "decision": decision,
            "reason": reason,
            "severity_counts": {k: len(v) for k, v in by_severity.items()},
            "failures": failures,
        }
        out = Path(self.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return payload


# --------------------------------------------------------------------------- #
# contract -> suite
# --------------------------------------------------------------------------- #
def build_suite_from_contract(
    contract: dict[str, Any],
    df: pd.DataFrame | None = None,
    *,
    suite_name: str = "orders_contract_suite",
    min_rows: int | None = None,
    now: datetime | None = None,
) -> ExpectationSuite:
    """Generate the suite from the contract, adapted to the batch's dtypes.

    `df` should be the *prepared* frame: declared datetime columns that were
    already parsed get a dtype expectation, while columns still arriving as
    strings get `ExpectColumnValuesToBeDateutilParseable`.
    """
    suite = ExpectationSuite(name=suite_name)

    def add(expectation, severity: str) -> None:
        expectation.meta = {**(expectation.meta or {}), "severity": severity}
        expectation.severity = severity if severity in ("critical", "warning") else "warning"
        suite.add_expectation(expectation)

    if min_rows is not None:
        add(gx.expectations.ExpectTableRowCountToBeBetween(min_value=min_rows), "critical")

    for column, rules in contract_columns(contract).items():
        rules = rules or {}
        severity = rules.get("severity", "warning")
        if rules.get("required"):
            add(gx.expectations.ExpectColumnValuesToNotBeNull(column=column), severity)
        if rules.get("unique"):
            add(gx.expectations.ExpectColumnValuesToBeUnique(column=column), severity)
        if rules.get("accepted_values") is not None:
            add(
                gx.expectations.ExpectColumnValuesToBeInSet(column=column, value_set=list(rules["accepted_values"])),
                severity,
            )
        if "min" in rules or "max" in rules:
            add(
                gx.expectations.ExpectColumnValuesToBeBetween(
                    column=column, min_value=rules.get("min"), max_value=rules.get("max")
                ),
                severity,
            )
        dtype = rules.get("type")
        if dtype in TYPE_LISTS:
            add(gx.expectations.ExpectColumnValuesToBeInTypeList(column=column, type_list=TYPE_LISTS[dtype]), severity)
        elif dtype in ("datetime", "timestamp", "date"):
            already_parsed = df is not None and column in df.columns and pd.api.types.is_datetime64_any_dtype(df[column])
            if already_parsed:
                # Unparseable values already became NaT, which not_null catches.
                add(gx.expectations.ExpectColumnValuesToBeInTypeList(column=column, type_list=DATETIME_TYPES), severity)
            else:
                add(gx.expectations.ExpectColumnValuesToBeDateutilParseable(column=column), severity)
        if rules.get("min_length") is not None or rules.get("max_length") is not None:
            add(
                gx.expectations.ExpectColumnValueLengthsToBeBetween(
                    column=column, min_value=rules.get("min_length"), max_value=rules.get("max_length")
                ),
                severity,
            )

    freshness = contract.get("freshness") or {}
    if freshness.get("column"):
        # Freshness as a real expectation: the newest timestamp must sit inside
        # the SLA window. Requires the column to be parsed to datetime first
        # (see `prepare_dataframe`).
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=float(freshness.get("max_delay_minutes", 60)))
        add(
            gx.expectations.ExpectColumnMaxToBeBetween(
                column=freshness["column"],
                min_value=cutoff.replace(tzinfo=None),
            ),
            freshness.get("severity", "warning"),
        )
    return suite


def prepare_dataframe(df: pd.DataFrame, contract: dict[str, Any]) -> pd.DataFrame:
    """Parse declared datetime columns so datetime expectations can compare."""
    prepared = df.copy()
    freshness_column = (contract.get("freshness") or {}).get("column")
    if freshness_column and freshness_column in prepared.columns:
        prepared[freshness_column] = pd.to_datetime(
            prepared[freshness_column], errors="coerce", utc=True, format="mixed"
        ).dt.tz_localize(None)
    return prepared


# --------------------------------------------------------------------------- #
# checkpoint
# --------------------------------------------------------------------------- #
def run_orders_checkpoint(
    df: pd.DataFrame | None = None,
    *,
    contract_path: str | Path = ROOT / "contracts" / "orders_contract.yaml",
    min_rows: int | None = None,
    result_path: str | Path = RESULT_PATH,
) -> dict[str, Any]:
    """Run the full Suite -> ValidationDefinition -> Checkpoint -> Action flow."""
    contract = load_contract(contract_path)
    if df is None:
        df = pd.read_csv(ROOT / "data" / "incoming" / "orders.csv")

    context = gx.get_context(mode="ephemeral")
    data_source = context.data_sources.add_pandas("orders_pandas")
    asset = data_source.add_dataframe_asset(name="orders_dataframe")
    batch_definition = asset.add_batch_definition_whole_dataframe("whole_orders")

    prepared = prepare_dataframe(df, contract)
    suite = context.suites.add(build_suite_from_contract(contract, prepared, min_rows=min_rows))
    validation_definition = context.validation_definitions.add(
        ValidationDefinition(name="orders_contract_validation", data=batch_definition, suite=suite)
    )
    checkpoint = context.checkpoints.add(
        Checkpoint(
            name="orders_contract_checkpoint",
            validation_definitions=[validation_definition],
            actions=[SeverityRoutingAction(name="route_by_severity", output_path=str(result_path))],
            result_format="SUMMARY",
        )
    )
    checkpoint.run(batch_parameters={"dataframe": prepared})
    with open(result_path, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    orders = pd.read_csv(ROOT / "data" / "incoming" / "orders.csv")
    # Volume floor: half of the last known healthy batch is a deliberate, cheap
    # guard against partial ingestion; the statistical version lives in
    # observability/anomaly.py.
    baseline_rows = len(pd.read_csv(ROOT / "data" / "baseline" / "orders.csv"))
    payload = run_orders_checkpoint(orders, min_rows=int(baseline_rows * 0.5))

    print("=== GX CHECKPOINT (orders_contract_checkpoint) ===")
    print(f"expectations evaluated : {payload['expectations_evaluated']}")
    print(f"failed                 : {payload['failed_expectations']} {payload['severity_counts']}")
    for failure in payload["failures"]:
        print(
            f"  [{failure['severity']:<8}] {failure['expectation']:<45} "
            f"column={failure['column']} unexpected={failure['unexpected_count']} "
            f"sample={str(failure['sample_unexpected_values'])[:60]}"
        )
    print(f"decision               : {payload['decision'].upper()} ({SEVERITY_ACTION.get('critical')} on critical)")
    print(f"reason                 : {payload['reason']}")
    print(f"evidence               : {RESULT_PATH.relative_to(ROOT)}")
    # Non-zero exit lets an orchestrator (Airflow/CI) actually stop the pipeline.
    return 1 if payload["decision"] == "block" else 0


if __name__ == "__main__":
    raise SystemExit(main())
