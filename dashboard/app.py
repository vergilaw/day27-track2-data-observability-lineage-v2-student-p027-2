from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports" / "latest_metrics.json"
HISTORY = ROOT / "data" / "history" / "metrics_history.csv"

ACTION_COLOR = {"pass": "🟢", "warn": "🟡", "quarantine": "🟠", "block": "🔴", "none": "🟢",
                "monitor": "🟡", "ticket": "🟠", "page": "🔴"}

st.set_page_config(page_title="Data Reliability Lab", layout="wide")
st.title("Data Reliability Game Day")
st.caption("Signals an on-call data engineer needs to decide: is this batch safe to publish?")

if not REPORT.exists():
    st.warning("Run `make baseline` first to generate reports/latest_metrics.json")
    st.stop()

report = json.loads(REPORT.read_text(encoding="utf-8"))
decision = report["contract_decision"]
burn = report["multiwindow_burn"]
row_anomaly = report["row_count_anomaly"]

# --- headline decision ------------------------------------------------------
st.subheader("Batch decision")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Contract decision", f"{ACTION_COLOR.get(decision['action'], '')} {decision['action'].upper()}")
c2.metric("Alert", f"{ACTION_COLOR.get(burn['action'], '')} {burn['action'].upper()}",
          help=burn["reason"])
c3.metric("Orders rows", report["orders_rows"],
          delta="anomaly" if row_anomaly["is_anomaly"] else "in range",
          delta_color="inverse" if row_anomaly["is_anomaly"] else "normal")
c4.metric("Freshness (min)", f"{report['freshness_minutes']:.1f}")
st.caption(decision["reason"])

# --- contract ---------------------------------------------------------------
left, right = st.columns(2)
with left:
    st.subheader("Contract & quarantine")
    st.write(
        f"failed checks: **{report['failed_contract_checks']}** "
        f"(critical **{report['critical_contract_failures']}**)"
    )
    st.write(
        f"rows: **{report['quarantine']['clean_rows']}** clean, "
        f"**{report['quarantine']['quarantined_rows']}** quarantined"
    )
    if decision["failed_check_names"]:
        st.error("\n".join(f"- {name}" for name in decision["failed_check_names"]))
    if report["kb_failed_contract_checks"]:
        st.warning(
            "Knowledge base:\n"
            + "\n".join(f"- {i['check']}:{i['column']} — {i['details']}" for i in report["kb_failed_contract_checks"])
        )

with right:
    st.subheader("Error budget")
    slo = report["contract_slo"]
    st.write(
        f"burn rate **{slo['burn_rate']:.2f}**, remaining budget "
        f"**{slo['remaining_error_budget_fraction']:.0%}**"
    )
    st.write(
        f"multi-window: short **{burn['short_window_burn']:.2f}** / "
        f"long **{burn['long_window_burn']:.2f}** → page **{burn['page']}**"
    )
    st.caption(f"SLI: {burn['sli']} · {burn['reason']}")

# --- anomaly ----------------------------------------------------------------
st.subheader("Volume anomaly: robust vs naive")
naive = report["row_count_anomaly_naive_zscore"]
st.write(
    f"seasonal/robust detector → **{row_anomaly['is_anomaly']}** "
    f"({row_anomaly['method']}, score {row_anomaly['score']:.2f}) · "
    f"naive z-score → **{naive['is_anomaly']}** (score {naive['score']:.2f})"
)
st.caption(row_anomaly["reason"])

history = pd.read_csv(HISTORY)
history["weekend"] = history["day_of_week"] >= 5
chart = history.set_index("date")[["row_count"]].copy()
chart["today"] = report["orders_rows"]
st.line_chart(chart)
st.caption("Weekend traffic is ~43% of weekday traffic — the reason a same-weekday baseline matters.")

# --- drift ------------------------------------------------------------------
st.subheader("Drift signals")
d1, d2, d3 = st.columns(3)
shift = report["amount_distribution_shift"]
d1.metric("Amount distribution", "drift" if shift["is_anomaly"] else "stable", help=shift["reason"])
d2.metric("KB text length", "drift" if report["kb_text_length_signal"]["is_anomaly"] else "stable",
          help=report["kb_text_length_signal"]["reason"])
d3.metric("KB embedding norms", "drift" if report["kb_embedding_norm_signal"]["is_anomaly"] else "stable",
          help=report["kb_embedding_norm_signal"]["reason"])

# --- blast radius -----------------------------------------------------------
st.subheader("Blast radius")
impact = report["blast_radius"]
kb_impact = report["kb_blast_radius"]
st.write("**stg_orders** → " + " → ".join(impact["downstream_assets"]))
st.write("**raw_orders.amount** → " + " → ".join(impact["downstream_columns"]))
st.write("**kb_documents** → " + " → ".join(kb_impact["downstream_assets"]))
st.write("**kb_documents.content** → " + " → ".join(kb_impact["downstream_columns"]))
st.caption(f"OpenLineage events: {report['openlineage_events']}")

with st.expander("Raw evidence payload"):
    st.json(report)
