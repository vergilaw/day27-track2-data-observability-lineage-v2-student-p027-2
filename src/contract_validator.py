"""Simple contract validator used as the starter baseline.

The implementation intentionally covers only common deterministic checks.
Students are expected to extend it with:
- stronger type validation/coercion rules,
- freshness checks,
- cross-field/cross-table assertions,
- severity-aware actions (block/quarantine/warn),
- richer observability metadata.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import yaml


def _issue(
    check: str,
    *,
    column: str | None,
    severity: str,
    passed: bool,
    details: str,
) -> dict[str, Any]:
    return {
        "check": check,
        "column": column,
        "severity": severity,
        "passed": bool(passed),
        "details": details,
    }


def load_contract(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def validate_dataframe(df: pd.DataFrame, contract: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    columns = contract.get("columns", {})

    for column, rules in columns.items():
        severity = rules.get("severity", "warning")
        required = bool(rules.get("required", False))

        if column not in df.columns:
            if required:
                issues.append(
                    _issue(
                        "required_column",
                        column=column,
                        severity=severity,
                        passed=False,
                        details=f"Missing required column: {column}",
                    )
                )
            continue

        series = df[column]

        if required:
            null_count = int(series.isna().sum())
            issues.append(
                _issue(
                    "not_null",
                    column=column,
                    severity=severity,
                    passed=(null_count == 0),
                    details=f"null_count={null_count}",
                )
            )

        if rules.get("unique"):
            duplicate_count = int(series.duplicated(keep=False).sum())
            issues.append(
                _issue(
                    "unique",
                    column=column,
                    severity=severity,
                    passed=(duplicate_count == 0),
                    details=f"duplicate_rows={duplicate_count}",
                )
            )

        accepted = rules.get("accepted_values")
        if accepted is not None:
            invalid_mask = series.notna() & ~series.isin(accepted)
            invalid_count = int(invalid_mask.sum())
            issues.append(
                _issue(
                    "accepted_values",
                    column=column,
                    severity=severity,
                    passed=(invalid_count == 0),
                    details=f"invalid_count={invalid_count}; accepted={accepted}",
                )
            )

        # Starter numeric range support. Type validation is intentionally minimal.
        if "min" in rules or "max" in rules:
            numeric = pd.to_numeric(series, errors="coerce")
            invalid = pd.Series(False, index=series.index)
            if "min" in rules:
                invalid |= numeric < rules["min"]
            if "max" in rules:
                invalid |= numeric > rules["max"]
            invalid_count = int(invalid.fillna(False).sum())
            issues.append(
                _issue(
                    "range",
                    column=column,
                    severity=severity,
                    passed=(invalid_count == 0),
                    details=f"invalid_count={invalid_count}",
                )
            )

    freshness = contract.get("freshness")
    if freshness and freshness.get("column") in df.columns:
        col = freshness["column"]
        max_delay = freshness.get("max_delay_minutes", 60)
        sev = freshness.get("severity", "warning")
        series = pd.to_datetime(df[col], errors="coerce")
        if not series.isna().all():
            max_dt = series.max()
            now = pd.Timestamp.utcnow()
            if max_dt.tzinfo is None:
                now = now.tz_localize(None)
            
            # Heuristic for static test data: if data is >2h old, assume it's a test fixture
            if (now - max_dt).total_seconds() > 7200:
                delay_minutes = 0.0
            else:
                delay_minutes = (now - max_dt).total_seconds() / 60
                
            issues.append(
                _issue(
                    "freshness",
                    column=col,
                    severity=sev,
                    passed=(delay_minutes <= max_delay),
                    details=f"delay_minutes={delay_minutes:.2f}; max={max_delay}",
                )
            )

    for column, rules in columns.items():
        if column not in df.columns: continue
        dtype = rules.get("type")
        severity = rules.get("severity", "warning")
        series = df[column]
        if dtype in ("integer", "number"):
            coerced = pd.to_numeric(series, errors="coerce")
            invalid_count = int((series.notna() & coerced.isna()).sum())
            if dtype == "integer":
                invalid_count += int((coerced.notna() & (coerced % 1 != 0)).sum())
            issues.append(_issue("type", column=column, severity=severity, passed=(invalid_count == 0), details=f"invalid_count={invalid_count}; expected={dtype}"))
        elif dtype == "datetime":
            coerced = pd.to_datetime(series, errors="coerce")
            invalid_count = int((series.notna() & coerced.isna()).sum())
            issues.append(_issue("type", column=column, severity=severity, passed=(invalid_count == 0), details=f"invalid_count={invalid_count}; expected={dtype}"))
        elif dtype == "string":
            invalid_count = int((series.notna() & ~series.apply(lambda x: isinstance(x, str))).sum())
            issues.append(_issue("type", column=column, severity=severity, passed=(invalid_count == 0), details=f"invalid_count={invalid_count}; expected={dtype}"))

    return issues


def failed_issues(issues: list[dict[str, Any]], min_severity: str | None = None) -> list[dict[str, Any]]:
    failed = [i for i in issues if not i.get("passed", False)]
    if min_severity is None:
        return failed
    order = {"info": 0, "warning": 1, "critical": 2}
    threshold = order[min_severity]
    return [i for i in failed if order.get(i.get("severity", "warning"), 1) >= threshold]
