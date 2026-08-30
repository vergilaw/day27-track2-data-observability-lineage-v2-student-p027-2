"""SLI/SLO math + multi-window burn-rate alerting policy.

`calculate_slo` keeps the simple normalized burn-rate math from the starter.
`evaluate_multiwindow_burn` implements the Google SRE Workbook
"Alerting on SLOs" multiwindow, multi-burn-rate policy so that a sustained fast
burn pages while a short transient spike does not.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

# 30-day rolling error-budget window, the SRE Workbook default.
BUDGET_WINDOW_HOURS = 30 * 24

# (burn_rate_threshold, budget consumed in the long window, long window, short
# window, action, severity). Ordered from fastest to slowest burn.
MULTIWINDOW_POLICY: tuple[dict[str, Any], ...] = (
    {"threshold": 14.4, "budget_consumed": 0.02, "long_window": "1h", "short_window": "5m",
     "action": "page", "severity": "critical"},
    {"threshold": 6.0, "budget_consumed": 0.05, "long_window": "6h", "short_window": "30m",
     "action": "page", "severity": "high"},
    {"threshold": 3.0, "budget_consumed": 0.10, "long_window": "1d", "short_window": "2h",
     "action": "ticket", "severity": "warning"},
    {"threshold": 1.0, "budget_consumed": 0.10, "long_window": "3d", "short_window": "6h",
     "action": "ticket", "severity": "warning"},
)


def calculate_slo(target: float, bad_events: int, total_events: int) -> dict[str, Any]:
    if not 0 < target < 1:
        raise ValueError("target must be between 0 and 1 (exclusive)")
    if bad_events < 0 or total_events < 0 or bad_events > total_events:
        raise ValueError("invalid event counts")
    allowed_bad_rate = 1.0 - target
    if total_events == 0:
        return {
            "target": target,
            "actual_bad_rate": 0.0,
            "allowed_bad_rate": allowed_bad_rate,
            "burn_rate": 0.0,
            "remaining_error_budget_fraction": 1.0,
            "error_budget_events": 0.0,
            "hours_to_exhaustion": float("inf"),
            "breached": False,
        }
    actual_bad_rate = bad_events / total_events
    burn_rate = actual_bad_rate / allowed_bad_rate
    consumed_fraction = min(1.0, burn_rate)
    return {
        "target": target,
        "actual_bad_rate": actual_bad_rate,
        "allowed_bad_rate": allowed_bad_rate,
        "burn_rate": burn_rate,
        "remaining_error_budget_fraction": max(0.0, 1.0 - consumed_fraction),
        "error_budget_events": allowed_bad_rate * total_events,
        "hours_to_exhaustion": (BUDGET_WINDOW_HOURS / burn_rate) if burn_rate > 0 else float("inf"),
        "breached": bool(actual_bad_rate > allowed_bad_rate),
    }


def _matched_tier(burn: float) -> dict[str, Any] | None:
    for tier in MULTIWINDOW_POLICY:
        if burn >= tier["threshold"]:
            return tier
    return None


def evaluate_multiwindow_burn(
    *,
    short_window_burn: float,
    long_window_burn: float,
    policy: str = "sre_multiwindow",
) -> dict[str, Any]:
    """Decide page / ticket / monitor from two burn-rate windows.

    The long window says "we are burning budget fast enough to care"; the short
    window confirms the problem is *still happening right now*. Both must clear
    the same threshold before we page - that is what suppresses a transient
    spike, and what makes the alert stop firing quickly after recovery.

    | short | long | outcome                                            |
    |-------|------|----------------------------------------------------|
    | high  | high | sustained fast burn -> page                         |
    | high  | low  | transient spike -> monitor, no page                 |
    | low   | high | burn already decaying -> ticket, no page            |
    | low   | low  | healthy                                             |
    """
    short = float(short_window_burn)
    long = float(long_window_burn)
    if short < 0 or long < 0:
        raise ValueError("burn rates must be non-negative")

    base = {
        "policy": policy,
        "short_window_burn": short,
        "long_window_burn": long,
        "thresholds": [t["threshold"] for t in MULTIWINDOW_POLICY],
    }

    confirmed = _matched_tier(min(short, long))
    if confirmed and confirmed["action"] == "page":
        return {
            **base,
            "page": True,
            "severity": confirmed["severity"],
            "action": "page",
            "burn_rate_threshold": confirmed["threshold"],
            "window": f"{confirmed['long_window']}/{confirmed['short_window']}",
            "hours_to_exhaustion": BUDGET_WINDOW_HOURS / max(short, long),
            "reason": (
                f"sustained_fast_burn: short={short:.2f} and long={long:.2f} both >= "
                f"{confirmed['threshold']} ({confirmed['budget_consumed']:.0%} of the 30d budget "
                f"in {confirmed['long_window']})"
            ),
        }

    if confirmed:  # both windows above a slow-burn threshold -> ticket, not a page
        return {
            **base,
            "page": False,
            "severity": confirmed["severity"],
            "action": "ticket",
            "burn_rate_threshold": confirmed["threshold"],
            "window": f"{confirmed['long_window']}/{confirmed['short_window']}",
            "hours_to_exhaustion": BUDGET_WINDOW_HOURS / max(short, long),
            "reason": (
                f"sustained_slow_burn: short={short:.2f}, long={long:.2f} >= "
                f"{confirmed['threshold']}; budget erodes without an immediate outage"
            ),
        }

    fastest = _matched_tier(max(short, long))
    if fastest and short > long:
        return {
            **base,
            "page": False,
            "severity": "warning",
            "action": "monitor",
            "burn_rate_threshold": fastest["threshold"],
            "reason": (
                f"transient_spike: short={short:.2f} exceeds {fastest['threshold']} but the long "
                f"window is only {long:.2f}; not yet enough budget burned to page"
            ),
        }
    if fastest:
        return {
            **base,
            "page": False,
            "severity": "warning",
            "action": "ticket",
            "burn_rate_threshold": fastest["threshold"],
            "reason": (
                f"burn_decaying: long={long:.2f} exceeds {fastest['threshold']} but short={short:.2f} "
                f"shows the incident is recovering; investigate the consumed budget instead of paging"
            ),
        }

    return {
        **base,
        "page": False,
        "severity": "info",
        "action": "none",
        "burn_rate_threshold": MULTIWINDOW_POLICY[-1]["threshold"],
        "reason": f"healthy: short={short:.2f} and long={long:.2f} below all burn-rate thresholds",
    }


def evaluate_burn_windows(window_burns: Mapping[str, float] | Iterable[tuple[str, float]]) -> dict[str, Any]:
    """Evaluate >2 windows at once, e.g. {'5m': 20, '1h': 15, '6h': 2, '3d': 0.4}.

    Each policy tier is checked against *its own* pair of windows when both are
    supplied (1h/5m, 6h/30m, ...). The first tier whose window pair clears the
    threshold wins; otherwise we fall back to the shortest/longest pair so the
    transient-spike rule still applies.
    """
    burns = dict(window_burns)
    if len(burns) < 2:
        raise ValueError("need at least two windows")

    for tier in MULTIWINDOW_POLICY:
        long_w, short_w = tier["long_window"], tier["short_window"]
        if long_w in burns and short_w in burns:
            result = evaluate_multiwindow_burn(
                short_window_burn=burns[short_w], long_window_burn=burns[long_w]
            )
            if result["action"] in {"page", "ticket"}:
                result["windows"] = burns
                result["matched_windows"] = f"{long_w}/{short_w}"
                return result

    order = sorted(burns, key=_window_minutes)
    result = evaluate_multiwindow_burn(
        short_window_burn=burns[order[0]], long_window_burn=burns[order[-1]]
    )
    result["windows"] = burns
    result["matched_windows"] = f"{order[-1]}/{order[0]}"
    return result


def _window_minutes(window: str) -> float:
    unit = window[-1].lower()
    value = float(window[:-1])
    return value * {"m": 1, "h": 60, "d": 1440}.get(unit, 1)
