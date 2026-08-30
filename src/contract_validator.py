"""Data contract validator.

Covers the deterministic half of the reliability stack: schema presence, types,
null/unique/accepted/range rules, string length, freshness, plus severity-driven
actions (block / quarantine / warn) and row-level quarantine.

Statistical failures (volume drops, distribution drift) are deliberately *not*
here - they belong to `observability/anomaly.py` and `observability/distribution.py`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import yaml

# severity -> what the pipeline should do with the batch
SEVERITY_ACTION = {"critical": "block", "warning": "quarantine", "info": "warn"}
SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}

# A batch older than this is treated as a historical replay / static fixture:
# wall-clock freshness is meaningless there, so we fall back to the batch's own
# internal lag (newest declared timestamp vs the freshness column).
DEFAULT_HISTORICAL_BATCH_HOURS = 24


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
        "action": "none" if passed else SEVERITY_ACTION.get(severity, "warn"),
        "details": details,
    }


def load_contract(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def contract_columns(contract: dict[str, Any]) -> dict[str, Any]:
    """`columns:` is the lab's spelling; `fields:` is used by the KB contract."""
    return contract.get("columns") or contract.get("fields") or {}


# --------------------------------------------------------------------------- #
# per-column checks
# --------------------------------------------------------------------------- #
def _type_violations(series: pd.Series, dtype: str) -> pd.Series:
    """Boolean mask of values that do not match the declared contract type."""
    present = series.notna()
    if dtype in ("integer", "number", "float"):
        coerced = pd.to_numeric(series, errors="coerce")
        invalid = present & coerced.isna()
        if dtype == "integer":
            invalid |= coerced.notna() & (coerced % 1 != 0)
        return invalid
    if dtype in ("datetime", "timestamp", "date"):
        coerced = pd.to_datetime(series, errors="coerce", utc=True, format="mixed")
        return present & coerced.isna()
    if dtype in ("string", "varchar", "text"):
        return present & ~series.map(lambda v: isinstance(v, str))
    if dtype in ("boolean", "bool"):
        return present & ~series.map(lambda v: isinstance(v, (bool,)) or str(v).lower() in {"true", "false", "0", "1"})
    return pd.Series(False, index=series.index)


def _column_row_violations(df: pd.DataFrame, column: str, rules: dict[str, Any]) -> pd.Series:
    """Row mask of contract violations for one column (used for quarantine)."""
    series = df[column]
    mask = pd.Series(False, index=df.index)
    if rules.get("required"):
        mask |= series.isna()
    if rules.get("unique"):
        mask |= series.duplicated(keep=False) & series.notna()
    if rules.get("accepted_values") is not None:
        mask |= series.notna() & ~series.isin(rules["accepted_values"])
    if "min" in rules or "max" in rules:
        numeric = pd.to_numeric(series, errors="coerce")
        if "min" in rules:
            mask |= (numeric < rules["min"]).fillna(False)
        if "max" in rules:
            mask |= (numeric > rules["max"]).fillna(False)
    if rules.get("type"):
        mask |= _type_violations(series, rules["type"])
    if rules.get("min_length") is not None:
        lengths = series.map(lambda v: len(str(v)) if pd.notna(v) else 0)
        mask |= series.notna() & (lengths < rules["min_length"])
    if rules.get("max_length") is not None:
        lengths = series.map(lambda v: len(str(v)) if pd.notna(v) else 0)
        mask |= series.notna() & (lengths > rules["max_length"])
    return mask.fillna(False)


# --------------------------------------------------------------------------- #
# freshness
# --------------------------------------------------------------------------- #
def _parse_datetime(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce", utc=True, format="mixed")


def evaluate_freshness(df: pd.DataFrame, contract: dict[str, Any], now: pd.Timestamp | None = None) -> dict[str, Any] | None:
    """Freshness check with an explicit fallback for historical batches.

    Wall clock is the right reference for a live pipeline. For a replayed or
    fixture batch (everything older than `historical_batch_after_hours`) the wall
    clock only measures how long ago the file was written, so we switch to the
    batch's *internal* lag: how far the freshness column trails the newest
    timestamp the batch itself declares. That still catches a partially updated
    slice; it cannot catch a batch that is uniformly old, which is why volume and
    arrival-time monitoring live in the anomaly layer.
    """
    freshness = contract.get("freshness")
    if not freshness:
        return None
    column = freshness.get("column")
    if not column or column not in df.columns:
        return None

    severity = freshness.get("severity", "warning")
    max_delay = float(freshness.get("max_delay_minutes", 60))
    historical_after = float(freshness.get("historical_batch_after_hours", DEFAULT_HISTORICAL_BATCH_HOURS))

    series = _parse_datetime(df[column])
    if series.isna().all():
        return _issue(
            "freshness",
            column=column,
            severity=severity,
            passed=False,
            details=f"no parseable timestamp in {column}",
        )

    now = now or pd.Timestamp.now(tz="UTC")
    latest = series.max()
    wall_clock_delay = (now - latest).total_seconds() / 60.0

    if wall_clock_delay <= historical_after * 60:
        return _issue(
            "freshness",
            column=column,
            severity=severity,
            passed=(wall_clock_delay <= max_delay),
            details=(
                f"mode=wall_clock; delay_minutes={wall_clock_delay:.2f}; max={max_delay}"
            ),
        )

    # Historical batch: compare against the newest timestamp declared in the batch.
    reference = latest
    for other, rules in contract_columns(contract).items():
        if other == column or other not in df.columns:
            continue
        if rules.get("type") in ("datetime", "timestamp", "date"):
            candidate = _parse_datetime(df[other]).max()
            if pd.notna(candidate) and candidate > reference:
                reference = candidate
    batch_lag = (reference - latest).total_seconds() / 60.0
    return _issue(
        "freshness",
        column=column,
        severity=severity,
        passed=(batch_lag <= max_delay),
        details=(
            f"mode=batch_lag (batch is {wall_clock_delay / 60:.1f}h old, beyond the "
            f"{historical_after:.0f}h wall-clock window); lag_minutes={batch_lag:.2f}; max={max_delay}"
        ),
    )


# --------------------------------------------------------------------------- #
# main entry points
# --------------------------------------------------------------------------- #
def validate_dataframe(df: pd.DataFrame, contract: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    columns = contract_columns(contract)

    for column, rules in columns.items():
        rules = rules or {}
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
            invalid_count = int((series.notna() & ~series.isin(accepted)).sum())
            issues.append(
                _issue(
                    "accepted_values",
                    column=column,
                    severity=severity,
                    passed=(invalid_count == 0),
                    details=f"invalid_count={invalid_count}; accepted={accepted}",
                )
            )

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
                    details=f"invalid_count={invalid_count}; min={rules.get('min')}; max={rules.get('max')}",
                )
            )

        dtype = rules.get("type")
        if dtype:
            invalid_count = int(_type_violations(series, dtype).sum())
            issues.append(
                _issue(
                    "type",
                    column=column,
                    severity=severity,
                    passed=(invalid_count == 0),
                    details=f"invalid_count={invalid_count}; expected={dtype}; pandas_dtype={series.dtype}",
                )
            )

        if rules.get("min_length") is not None or rules.get("max_length") is not None:
            lengths = series.map(lambda v: len(str(v)) if pd.notna(v) else 0)
            invalid = pd.Series(False, index=series.index)
            if rules.get("min_length") is not None:
                invalid |= series.notna() & (lengths < rules["min_length"])
            if rules.get("max_length") is not None:
                invalid |= series.notna() & (lengths > rules["max_length"])
            issues.append(
                _issue(
                    "length",
                    column=column,
                    severity=severity,
                    passed=(int(invalid.sum()) == 0),
                    details=(
                        f"invalid_count={int(invalid.sum())}; "
                        f"min_length={rules.get('min_length')}; max_length={rules.get('max_length')}"
                    ),
                )
            )

    freshness_issue = evaluate_freshness(df, contract)
    if freshness_issue is not None:
        issues.append(freshness_issue)

    return issues


def failed_issues(issues: list[dict[str, Any]], min_severity: str | None = None) -> list[dict[str, Any]]:
    failed = [i for i in issues if not i.get("passed", False)]
    if min_severity is None:
        return failed
    threshold = SEVERITY_ORDER[min_severity]
    return [i for i in failed if SEVERITY_ORDER.get(i.get("severity", "warning"), 1) >= threshold]


def decide_action(issues: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Batch-level decision from severity: block > quarantine > warn > pass."""
    failed = [i for i in issues if not i.get("passed", False)]
    if any(i.get("severity") == "critical" for i in failed):
        action, reason = "block", "at least one critical contract check failed"
    elif any(i.get("severity") == "warning" for i in failed):
        action, reason = "quarantine", "warning-level checks failed; isolate bad rows and continue"
    elif failed:
        action, reason = "warn", "only info-level checks failed"
    else:
        action, reason = "pass", "all contract checks passed"
    return {
        "action": action,
        "reason": reason,
        "failed_checks": len(failed),
        "critical_failures": sum(1 for i in failed if i.get("severity") == "critical"),
        "failed_check_names": sorted({f"{i['check']}:{i.get('column')}" for i in failed}),
    }


def split_quarantine(df: pd.DataFrame, contract: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split a batch into (clean, quarantined) rows.

    Row-level rules only - a missing column is a batch-level failure and cannot
    be quarantined row by row.
    """
    bad = pd.Series(False, index=df.index)
    for column, rules in contract_columns(contract).items():
        if column in df.columns:
            bad |= _column_row_violations(df, column, rules or {})
    return df.loc[~bad].copy(), df.loc[bad].copy()
