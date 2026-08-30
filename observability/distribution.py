"""Distribution drift detection.

The starter compared means only, which misses shape changes (bimodality, tail
growth, a currency mix flip) whenever the mean happens to stay put. This version
combines three complementary signals and needs no SciPy:

- **PSI** (population stability index) on baseline quantile bins - the standard
  data/feature drift metric, sensitive to mass moving between buckets;
- **two-sample Kolmogorov-Smirnov** statistic + asymptotic p-value - sensitive to
  any CDF difference, and gives a significance level instead of a bare ratio;
- **robust central-tendency ratio** (median / MAD based) - keeps the intuitive
  "revenue halved" signal that the starter mean-ratio provided, without letting a
  single outlier drive it.
"""
from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np

# PSI convention: <0.1 stable, 0.1-0.25 moderate shift, >0.25 significant shift.
PSI_MODERATE = 0.10
PSI_SIGNIFICANT = 0.25


def population_stability_index(
    current: np.ndarray, baseline: np.ndarray, *, bins: int | None = None, epsilon: float = 1e-6
) -> float:
    """PSI over baseline quantile bins (equal-frequency, drift-robust).

    Bin count adapts to the smaller sample (~5 observations per bin) because PSI
    on 10 bins with a handful of points is mostly binning noise.
    """
    if current.size == 0 or baseline.size == 0:
        return 0.0
    if bins is None:
        bins = int(max(2, min(10, min(current.size, baseline.size) // 5)))
    quantiles = np.linspace(0, 100, bins + 1)
    edges = np.unique(np.percentile(baseline, quantiles))
    if edges.size < 2:  # constant baseline: fall back to "same value or not"
        same = float(np.mean(current == baseline[0]))
        return 0.0 if same == 1.0 else float(-math.log(max(same, epsilon)))
    edges = np.concatenate(([-np.inf], edges[1:-1], [np.inf]))
    base_counts, _ = np.histogram(baseline, bins=edges)
    cur_counts, _ = np.histogram(current, bins=edges)
    base_pct = np.maximum(base_counts / baseline.size, epsilon)
    cur_pct = np.maximum(cur_counts / current.size, epsilon)
    return float(np.sum((cur_pct - base_pct) * np.log(cur_pct / base_pct)))


def ks_two_sample(current: np.ndarray, baseline: np.ndarray) -> tuple[float, float]:
    """Two-sample KS statistic and asymptotic p-value (no SciPy needed)."""
    n, m = current.size, baseline.size
    if n == 0 or m == 0:
        return 0.0, 1.0
    grid = np.sort(np.concatenate([current, baseline]))
    cdf_cur = np.searchsorted(np.sort(current), grid, side="right") / n
    cdf_base = np.searchsorted(np.sort(baseline), grid, side="right") / m
    d = float(np.max(np.abs(cdf_cur - cdf_base)))
    ne = math.sqrt(n * m / (n + m))
    lam = (ne + 0.12 + 0.11 / ne) * d
    if lam <= 0:
        return d, 1.0
    p = 2.0 * sum((-1) ** (j - 1) * math.exp(-2.0 * (j * lam) ** 2) for j in range(1, 101))
    return d, float(min(1.0, max(0.0, p)))


def _robust_ratio(current: np.ndarray, baseline: np.ndarray) -> float:
    """Symmetric median ratio; 1.0 means identical, larger means further apart."""
    cur_med = float(np.median(current))
    base_med = float(np.median(baseline))
    if cur_med == 0 and base_med == 0:
        return 1.0
    if cur_med == 0 or base_med == 0:
        return float("inf")
    return float(max(abs(cur_med / base_med), abs(base_med / cur_med)))


def detect_distribution_shift(
    current_values: Iterable[float],
    baseline_values: Iterable[float],
    *,
    ratio_threshold: float = 3.0,
    psi_threshold: float = PSI_SIGNIFICANT,
    alpha: float = 0.01,
    min_samples: int = 5,
) -> dict[str, Any]:
    """Return a drift verdict combining PSI, KS and a robust median ratio.

    `score` is the PSI value (unbounded, comparable across runs); the KS
    statistic/p-value and the robust ratio are reported alongside so an on-call
    engineer can tell *how* the distribution moved, not just that it did.
    """
    cur = np.asarray(list(current_values), dtype=float)
    base = np.asarray(list(baseline_values), dtype=float)
    cur = cur[np.isfinite(cur)]
    base = base[np.isfinite(base)]
    if cur.size == 0 or base.size == 0:
        return {
            "is_anomaly": False,
            "score": 0.0,
            "method": "psi_ks_robust",
            "reason": "empty_input",
            "psi": 0.0,
            "ks_statistic": 0.0,
            "ks_pvalue": 1.0,
            "robust_ratio": 1.0,
        }

    psi = population_stability_index(cur, base)
    ks_stat, ks_p = ks_two_sample(cur, base)
    ratio = _robust_ratio(cur, base)

    # PSI and KS both need enough observations to be meaningful; below that we
    # only trust the robust ratio, otherwise every 3-row batch looks like drift.
    reliable_sample = cur.size >= min_samples and base.size >= min_samples
    triggers: list[str] = []
    if reliable_sample and psi >= psi_threshold:
        triggers.append(f"psi={psi:.3f}>={psi_threshold}")
    if reliable_sample and ks_p < alpha:
        triggers.append(f"ks_p={ks_p:.4f}<{alpha} (D={ks_stat:.3f})")
    if ratio >= ratio_threshold:
        triggers.append(f"robust_ratio={ratio:.2f}>={ratio_threshold}")

    if triggers:
        verdict = "significant_shift"
    elif psi >= PSI_MODERATE:
        verdict = "moderate_shift_watch"
    else:
        verdict = "stable"

    return {
        "is_anomaly": bool(triggers),
        "score": float(psi),
        "method": "psi_ks_robust",
        "reason": (
            f"{verdict}: " + ("; ".join(triggers) if triggers else
                              f"psi={psi:.3f}, ks_D={ks_stat:.3f}, ks_p={ks_p:.3f}, ratio={ratio:.2f}")
            + f" | n_current={cur.size}, n_baseline={base.size}"
            + ("" if reliable_sample else "; small_sample_psi_ks_ignored")
        ),
        "psi": float(psi),
        "ks_statistic": float(ks_stat),
        "ks_pvalue": float(ks_p),
        "robust_ratio": float(ratio),
        "current_median": float(np.median(cur)),
        "baseline_median": float(np.median(base)),
        "verdict": verdict,
    }
