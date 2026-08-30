"""RAG / knowledge-base reliability signals.

A support agent can answer confidently from a stale or truncated index without
any pipeline error, so the KB needs its own SLIs:

- **text length drift** - chunking or extraction broke and documents collapsed;
- **embedding drift** - the embedding model or normalization changed, or the
  index was rebuilt from different content (detected via vector norms, or via
  cosine similarity to the baseline centroid when raw vectors are available);
- **retrieval quality** - recall@k / MRR against a small golden set.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

import numpy as np

from observability.anomaly import mad_detector, zscore_detector
from observability.distribution import detect_distribution_shift


def approximate_token_lengths(texts: Iterable[str]) -> list[int]:
    # Deliberately simple proxy; no tokenizer/model download needed.
    return [len(str(t).split()) for t in texts]


def _robust_point_check(current: float, baseline: Iterable[float], threshold: float) -> dict[str, Any]:
    """MAD when the history supports it, z-score otherwise."""
    values = list(baseline)
    result = mad_detector(current, values, threshold=threshold)
    if result["reason"].startswith("insufficient_history"):
        result = zscore_detector(current, values, threshold=threshold)
    return result


def detect_text_length_shift(
    current_texts: Iterable[str],
    baseline_batch_means: Iterable[float],
    *,
    threshold: float = 3.0,
    collapse_ratio: float = 0.5,
) -> dict[str, Any]:
    """Detect KB text-length collapse/inflation against historical batch means.

    Two independent triggers: a robust point anomaly on the batch mean, and a
    relative-magnitude guard so an all-identical history (MAD = 0) still fires
    when the content halves.
    """
    lengths = approximate_token_lengths(current_texts)
    current_mean = float(np.mean(lengths)) if lengths else 0.0
    baseline = [float(v) for v in baseline_batch_means]
    baseline_mean = float(np.median(baseline)) if baseline else 0.0

    result = _robust_point_check(current_mean, baseline, threshold)
    ratio = (current_mean / baseline_mean) if baseline_mean else 1.0
    collapsed = bool(baseline_mean and (ratio <= collapse_ratio or ratio >= 1 / max(collapse_ratio, 1e-9)))

    result["is_anomaly"] = bool(result["is_anomaly"] or collapsed)
    result["method"] = f"text_length:{result['method']}"
    result["metric"] = "mean_text_length"
    result["current_mean"] = current_mean
    result["baseline_median"] = baseline_mean
    result["ratio_vs_baseline"] = float(ratio)
    result["min_length"] = int(min(lengths)) if lengths else 0
    result["empty_doc_ratio"] = float(np.mean([l == 0 for l in lengths])) if lengths else 0.0
    if collapsed:
        result["reason"] = (
            f"length_collapse: current_mean={current_mean:.2f} is {ratio:.2f}x the baseline "
            f"median {baseline_mean:.2f}; " + result["reason"]
        )
    return result


def detect_embedding_norm_shift(
    current_norms: Iterable[float],
    baseline_norms: Iterable[float],
    *,
    threshold: float = 3.5,
    relative_threshold: float = 0.15,
) -> dict[str, Any]:
    """Embedding-space drift from precomputed vector norms.

    Triggers on any of:
    - robust modified z-score of the current median against the baseline norms,
    - a full distribution shift (PSI / KS) between the two norm samples,
    - a relative median move larger than `relative_threshold` (catches the
      degenerate case where every baseline norm is identical, so MAD = 0 -
      exactly what happens when a model switches to L2-normalized output).
    """
    cur = np.asarray([v for v in current_norms], dtype=float)
    base = np.asarray([v for v in baseline_norms], dtype=float)
    cur = cur[np.isfinite(cur)]
    base = base[np.isfinite(base)]
    if cur.size == 0 or base.size == 0:
        return {
            "is_anomaly": False,
            "score": 0.0,
            "method": "embedding_norm",
            "reason": "empty_input",
            "metric": "embedding_norm_mean",
        }

    cur_med = float(np.median(cur))
    base_med = float(np.median(base))
    point = _robust_point_check(cur_med, base.tolist(), threshold)
    dist = detect_distribution_shift(cur.tolist(), base.tolist())
    relative_shift = abs(cur_med - base_med) / abs(base_med) if base_med else (0.0 if cur_med == 0 else float("inf"))
    # Baseline's own relative spread: the shift only counts when it is bigger
    # than the noise the baseline already shows (and MAD = 0 means no noise).
    base_spread = float(np.median(np.abs(base - base_med)) / abs(base_med)) if base_med else 0.0

    triggers: list[str] = []
    if point["is_anomaly"]:
        triggers.append(f"robust_point_score={point['score']:.2f}>{threshold}")
    if dist["is_anomaly"]:
        triggers.append(f"distribution_shift(psi={dist['psi']:.3f}, ks_p={dist['ks_pvalue']:.4f})")
    if relative_shift >= relative_threshold and relative_shift > base_spread:
        triggers.append(f"relative_median_shift={relative_shift:.1%}>={relative_threshold:.0%}")

    score = point["score"] if math.isfinite(point["score"]) else float(dist["score"])
    return {
        "is_anomaly": bool(triggers),
        "score": float(score),
        "method": f"embedding_norm:{point['method']}+psi_ks",
        "reason": (
            ("embedding_drift: " + "; ".join(triggers)) if triggers
            else f"stable: median {cur_med:.4f} vs baseline {base_med:.4f} (shift={relative_shift:.1%})"
        ),
        "metric": "embedding_norm_median",
        "current_median": cur_med,
        "baseline_median": base_med,
        "current_mean": float(np.mean(cur)),
        "baseline_mean": float(np.mean(base)),
        "relative_shift": float(relative_shift),
        "baseline_relative_spread": base_spread,
        "psi": float(dist["psi"]),
        "ks_pvalue": float(dist["ks_pvalue"]),
    }


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    va, vb = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    return float(np.dot(va, vb) / denom) if denom else 0.0


def detect_embedding_shift(
    current_vectors: Iterable[Sequence[float]],
    baseline_vectors: Iterable[Sequence[float]],
    *,
    similarity_floor: float = 0.9,
    min_baseline_cohesion: float = 0.5,
    threshold: float = 3.5,
) -> dict[str, Any]:
    """Richer drift check when raw vectors are available (optional path).

    Compares the current batch against the baseline centroid: a rebuilt index
    with a different model shows up as a centroid-similarity drop even when the
    norms are unchanged (e.g. both models emit unit vectors).
    """
    cur = np.asarray([list(v) for v in current_vectors], dtype=float)
    base = np.asarray([list(v) for v in baseline_vectors], dtype=float)
    if cur.size == 0 or base.size == 0 or cur.shape[1:] != base.shape[1:]:
        return {
            "is_anomaly": bool(cur.size and base.size),
            "score": 0.0,
            "method": "embedding_centroid",
            "reason": "empty_input" if not (cur.size and base.size) else "dimension_mismatch",
        }
    centroid = base.mean(axis=0)
    current_sims = [cosine_similarity(v, centroid) for v in cur]
    baseline_sims = [cosine_similarity(v, centroid) for v in base]
    mean_sim = float(np.mean(current_sims))
    baseline_mean_sim = float(np.mean(baseline_sims))
    point = _robust_point_check(mean_sim, baseline_sims, threshold)
    # Only meaningful when the baseline actually clusters around its centroid;
    # for a diffuse cloud the mean cosine is ~0 and the ratio says nothing.
    below_floor = bool(
        baseline_mean_sim >= min_baseline_cohesion
        and mean_sim < similarity_floor * baseline_mean_sim
    )
    norms = detect_embedding_norm_shift(
        np.linalg.norm(cur, axis=1).tolist(), np.linalg.norm(base, axis=1).tolist()
    )
    return {
        "is_anomaly": bool(point["is_anomaly"] or below_floor or norms["is_anomaly"]),
        "score": float(point["score"] if math.isfinite(point["score"]) else 0.0),
        "method": "embedding_centroid_cosine",
        "reason": (
            f"mean_cosine_to_baseline_centroid={mean_sim:.4f} "
            f"(baseline={baseline_mean_sim:.4f}); norm_signal={norms['reason']}"
        ),
        "mean_cosine_similarity": mean_sim,
        "baseline_mean_cosine": baseline_mean_sim,
        "norm_signal": norms,
    }


def evaluate_retrieval(
    retrieved: Iterable[Sequence[str]],
    relevant: Iterable[Sequence[str]],
    *,
    k: int = 5,
) -> dict[str, Any]:
    """Golden-set retrieval quality: recall@k, precision@k and MRR.

    Lets the lab assert that a KB incident (stale docs, collapsed chunks) shows
    up as a *user-visible* regression, not just an index-side statistic.
    """
    retrieved_list = [list(r) for r in retrieved]
    relevant_list = [set(r) for r in relevant]
    if not retrieved_list or len(retrieved_list) != len(relevant_list):
        return {"recall_at_k": 0.0, "precision_at_k": 0.0, "mrr": 0.0, "k": k, "queries": 0}

    recalls, precisions, rr = [], [], []
    for got, want in zip(retrieved_list, relevant_list):
        top = got[:k]
        hits = len(set(top) & want)
        recalls.append(hits / len(want) if want else 0.0)
        precisions.append(hits / len(top) if top else 0.0)
        rank = next((i + 1 for i, doc in enumerate(top) if doc in want), 0)
        rr.append(1.0 / rank if rank else 0.0)
    return {
        "recall_at_k": float(np.mean(recalls)),
        "precision_at_k": float(np.mean(precisions)),
        "mrr": float(np.mean(rr)),
        "k": k,
        "queries": len(retrieved_list),
    }
