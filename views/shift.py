"""Shift summary (fixed template, no LLM text) + shift checklist (spec §8.6–8.7)."""
import streamlit as st

from ui import client
from ui.common import open_ticket

data = client.get("/analytics/shift")
s, live = data["summary"], data["status"]

st.markdown("## Shift")
with st.container(border=True):
    st.markdown("**Summary**")
    st.markdown(s["text"])
    c = st.columns(6)
    c[0].metric("New", s["new"])
    c[1].metric("Resolved by bot", s["resolved_bot"])
    c[2].metric("Resolved by agents", s["resolved_human"])
    c[3].metric("Escalated", s["escalated"])
    c[4].metric("SLA breaches", s["sla_breaches"])
    c[5].metric("Jev outages", s["jev_outages"])
    st.code(s["text"], language=None)
    st.caption("Copy this into the handover channel.")

hints = {"p1_reviewed": f"{len(live['open_p1'])} open P1: {', '.join(live['open_p1']) or 'none'}",
         "alerts_cleared": f"{live['unacked_alerts']} unacknowledged alerts",
         "sla_owned": f"{len(live['at_risk_unowned'])} SLA-at-risk tickets with no owner: "
                      f"{', '.join(live['at_risk_unowned']) or 'none'}",
         "handover": "Write what the next shift needs to know"}

with st.container(border=True):
    st.markdown("**Shift checklist**")
    for item in data["checklist"]:
        done = bool(item["done_at"])
        cols = st.columns([3, 4])
        new = cols[0].checkbox(item["text"], value=done, key=f"shift-{item['item_id']}")
        cols[1].caption(hints[item["item_id"]])
        notes = item["notes"]
        if item["item_id"] == "handover":
            notes = st.text_area("Handover notes", value=item["notes"] or "", key="shift-notes")
        if new != done or (item["item_id"] == "handover" and notes != (item["notes"] or "") and
                           st.button("Save notes")):
            client.post(f"/analytics/shift/{item['item_id']}", {"done": new, "notes": notes})
            st.rerun()

if live["open_p1"]:
    st.markdown("**Open P1s**")
    for tid in live["open_p1"]:
        if st.button(tid, key=f"p1-{tid}"):
            open_ticket(tid)
