"""Anomaly detection.

`zscore_detector` stays as the naive baseline (it is what the lab compares
against). `mad_detector` is the robust alternative, and `auto` is the
context-aware policy that picks a baseline and combines signals:

- **same-weekday / same-segment baseline** when the caller supplies one, because
  a Saturday row count must be compared with other Saturdays;
- **median + MAD** so a single past outlier does not inflate the spread and hide
  today's real drop (the classic z-score masking failure);
- **mean-absolute-deviation fallback** when MAD is 0 - a flat history used to
  make every detector silently return "not an anomaly";
- **EWMA detrending** for metrics that legitimately trend;
- **relative-magnitude guard** for count-like metrics, so a 75% volume drop pages
  even when the historical spread is wide.
"""
from __future__ import annotations

from typing import Any, Iterable

import numpy as np

DEFAULT_ZSCORE_THRESHOLD = 3.0
DEFAULT_MAD_THRESHOLD = 3.5
# MAD -> sigma (0.6745) and MeanAD -> sigma (1.2533) consistency constants.
MAD_TO_SIGMA = 0.6745
MEANAD_TO_SIGMA = 1.2533
COUNT_METRIC_HINTS = ("count", "rows", "volume", "records", "orders", "events")


def zscore_detector(current: float, history: Iterable[float], threshold: float = 3.0) -> dict[str, Any]:
    values = np.asarray(list(history), dtype=float)
    if values.size < 3:
        return {"is_anomaly": False, "score": 0.0, "method": "zscore", "reason": "insufficient_history"}
    mean = float(np.mean(values))
    std = float(np.std(values))
    if std == 0:
        score = float("inf") if float(current) != mean else 0.0
    else:
        score = abs(float(current) - mean) / std
    return {
        "is_anomaly": bool(score > threshold),
        "score": float(score),
        "method": "zscore",
        "reason": f"mean={mean:.3f}, std={std:.3f}, threshold={threshold}",
    }


def mad_detector(current: float, history: Iterable[float], threshold: float = DEFAULT_MAD_THRESHOLD) -> dict[str, Any]:
    """Modified z-score around the median.

    Zero-MAD is the interesting edge case: >50% of the history being identical
    makes MAD 0, which the starter treated as "cannot decide" - that silently
    disables the detector exactly when the metric is most stable. We fall back to
    the mean absolute deviation, and finally to an exact-equality test.
    """
    values = np.asarray(list(history), dtype=float)
    values = values[np.isfinite(values)]
    if values.size < 5:
        return {"is_anomaly": False, "score": 0.0, "method": "mad", "reason": "insufficient_history"}
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    current = float(current)

    if mad > 0:
        scale, basis = mad / MAD_TO_SIGMA, f"mad={mad:.3f}"
    else:
        mean_ad = float(np.mean(np.abs(values - median)))
        if mean_ad > 0:
            scale, basis = mean_ad * MEANAD_TO_SIGMA, f"mad=0, mean_abs_dev={mean_ad:.3f}"
        else:  # perfectly constant history
            score = 0.0 if current == median else float("inf")
            return {
                "is_anomaly": bool(score > threshold),
                "score": score,
                "method": "mad",
                "reason": f"constant_history median={median:.3f}; any deviation is an anomaly",
            }

    score = abs(current - median) / scale
    return {
        "is_anomaly": bool(score > threshold),
        "score": float(score),
        "method": "mad",
        "reason": f"median={median:.3f}, {basis}, threshold={threshold}",
    }


def ewma_detector(
    current: float,
    history: Iterable[float],
    *,
    alpha: float = 0.3,
    threshold: float = DEFAULT_MAD_THRESHOLD,
) -> dict[str, Any]:
    """Compare against an EWMA forecast, scored on robust residual spread.

    Useful when the metric legitimately trends: a growing series makes a plain
    median baseline flag every new high as an anomaly.
    """
    values = np.asarray(list(history), dtype=float)
    values = values[np.isfinite(values)]
    if values.size < 5:
        return {"is_anomaly": False, "score": 0.0, "method": "ewma", "reason": "insufficient_history"}
    level = float(values[0])
    residuals: list[float] = []
    for value in values[1:]:
        residuals.append(float(value) - level)
        level = alpha * float(value) + (1 - alpha) * level
    residual_median = float(np.median(residuals))
    residual_mad = float(np.median(np.abs(np.asarray(residuals) - residual_median)))
    scale = residual_mad / MAD_TO_SIGMA if residual_mad > 0 else float(np.mean(np.abs(residuals))) or 1e-9
    # EWMA lags a linear trend, so the residuals carry a systematic bias; score
    # today's residual against that residual distribution, not against zero.
    score = abs((float(current) - level) - residual_median) / scale
    return {
        "is_anomaly": bool(score > threshold),
        "score": float(score),
        "method": "ewma",
        "reason": (
            f"forecast={level:.3f}, residual_bias={residual_median:.3f}, "
            f"residual_scale={scale:.3f}, threshold={threshold}"
        ),
    }


def _relative_guard(current: float, values: np.ndarray, *, drop_ratio: float = 0.5) -> tuple[bool, float, str]:
    """Magnitude check for count-like metrics: is the level less than half /
    more than double the historical median?"""
    median = float(np.median(values))
    if median <= 0:
        return False, 1.0, ""
    ratio = float(current) / median
    if ratio <= drop_ratio:
        return True, ratio, f"volume_drop: {ratio:.0%} of median {median:.1f}"
    if ratio >= 1 / drop_ratio:
        return True, ratio, f"volume_spike: {ratio:.0%} of median {median:.1f}"
    return False, ratio, ""


def _looks_like_count(metric_name: str | None) -> bool:
    name = (metric_name or "").lower()
    return any(hint in name for hint in COUNT_METRIC_HINTS)


def auto_detector(
    current: float,
    history: Iterable[float],
    *,
    threshold: float | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Context-aware policy - see the module docstring for the signal list."""
    context = context or {}
    if context.get("known_event"):
        return {
            "is_anomaly": False,
            "score": 0.0,
            "method": "auto:known_event",
            "reason": f"suppressed by known_event={context['known_event']!r}",
        }

    full = np.asarray([v for v in history], dtype=float)
    full = full[np.isfinite(full)]
    segment = np.asarray([v for v in (context.get("same_segment_history") or [])], dtype=float)
    segment = segment[np.isfinite(segment)]

    # Seasonality: prefer the same-segment (e.g. same weekday) baseline when it
    # is long enough to be a baseline at all.
    if segment.size >= 5:
        values, baseline_kind = segment, "same_segment"
    elif full.size >= 5:
        values, baseline_kind = full, "full_history"
    elif segment.size >= 3:
        values, baseline_kind = segment, "same_segment_short"
    else:
        values, baseline_kind = full, "full_history_short"

    if values.size < 3:
        return {
            "is_anomaly": False,
            "score": 0.0,
            "method": "auto:insufficient_history",
            "reason": f"need >=3 observations, got {values.size}",
        }

    trending = bool(context.get("trend"))
    if trending and values.size >= 5:
        primary = ewma_detector(current, values, threshold=threshold or DEFAULT_MAD_THRESHOLD)
    elif values.size >= 5:
        primary = mad_detector(current, values, threshold=threshold or DEFAULT_MAD_THRESHOLD)
    else:
        primary = zscore_detector(current, values, threshold=threshold or DEFAULT_ZSCORE_THRESHOLD)

    reasons = [f"baseline={baseline_kind}(n={values.size})", primary["reason"]]
    is_anomaly = bool(primary["is_anomaly"])
    score = float(primary["score"])

    metric_name = context.get("metric_name")
    # Named count metrics get the sensitive guard; with no metric name at all we
    # only override on a 3x move, which no legitimate daily series survives.
    if not trending:
        if _looks_like_count(metric_name):
            drop_ratio = 0.5
        elif metric_name is None:
            drop_ratio = 1 / 3
        else:
            drop_ratio = 0.0
        guarded, ratio, guard_reason = (
            _relative_guard(current, values, drop_ratio=drop_ratio) if drop_ratio else (False, 1.0, "")
        )
        if guarded:
            reasons.append(guard_reason)
            is_anomaly = True
            if not np.isfinite(score) or score < DEFAULT_MAD_THRESHOLD:
                score = max(score if np.isfinite(score) else 0.0, abs(1.0 - ratio) * 10.0)

    return {
        "is_anomaly": is_anomaly,
        "score": score,
        "method": f"auto:{primary['method']}",
        "reason": "; ".join(r for r in reasons if r),
        "baseline": baseline_kind,
        "metric_name": metric_name,
    }


def detect_anomaly(
    current: float,
    history: Iterable[float],
    *,
    method: str = "auto",
    threshold: float | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stable lab API.

    - `zscore`: naive baseline (kept for comparison/teaching),
    - `mad`: robust modified z-score,
    - `ewma`: trend-aware forecast residual,
    - `auto`: context-aware policy combining the above.
    """
    if method == "mad":
        return mad_detector(current, history, threshold=threshold or DEFAULT_MAD_THRESHOLD)
    if method == "zscore":
        return zscore_detector(current, history, threshold=threshold or DEFAULT_ZSCORE_THRESHOLD)
    if method == "ewma":
        return ewma_detector(current, history, threshold=threshold or DEFAULT_MAD_THRESHOLD)
    if method == "auto":
        return auto_detector(current, history, threshold=threshold, context=context)
    raise ValueError(f"Unsupported method: {method}")
