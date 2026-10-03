"""Agent performance (spec §8.5). Agents see their own row; supervisors see everyone."""
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from ui import client

user = st.session_state.user
st.markdown("## Agent performance" if user["role"] == "supervisor" else "## My performance")
rng = st.date_input("Period", (date.today() - timedelta(days=30), date.today()), max_value=date.today())
d_from, d_to = (rng[0], rng[1]) if isinstance(rng, (list, tuple)) and len(rng) == 2 else (rng, rng)
rows = client.get("/analytics/performance", date_from=str(d_from), date_to=str(d_to))


def pct(x):
    return None if x is None else round(x * 100)


def hours(x):
    return "—" if x is None else f"{x:.1f}"  # a dash reads better than "None" when nothing was resolved yet


df = pd.DataFrame([{
    "Agent": r["name"], "ID": r["user_id"], "Queues": ", ".join(r["queues"]) if r["role"] == "agent" else "All",
    "Handled": r["handled"], "Resolved": r["resolved"], "First response (median h)": hours(r["median_first_response_h"]),
    "Resolution (median h)": hours(r["median_resolution_h"]), "SLA met %": pct(r["sla_met_pct"]),
    "Reopen rate %": pct(r["reopen_rate"]), "Checklist completion %": pct(r["checklist_completion"]),
    "Corrections made": r["corrections_made"]} for r in rows])
if df.empty or not df["Handled"].sum():
    st.info("No tickets claimed in this period yet.")
st.dataframe(df, hide_index=True, width="stretch", column_config={
    "SLA met %": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d%%"),
    "Checklist completion %": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d%%"),
    "Reopen rate %": st.column_config.NumberColumn(format="%d%%")})
st.caption("Handled = tickets claimed in the period. First response and resolution are measured from the claim. "
           "Checklist completion should always be 100%: Resolve is blocked until required items are done. "
           "Corrections made counts bot mistakes fixed, which is a good thing.")
