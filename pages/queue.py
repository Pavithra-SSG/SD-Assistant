"""My queue: own queues + all P1 (agent) or everything (supervisor), sorted P1 → P4, then SLA due (spec §8.1)."""
import pandas as pd
import streamlit as st

from ui import client
from ui.client import ApiError
from ui.common import STATE_LABEL, countdown, error, open_ticket

ss = st.session_state
user = ss.user
meta = client.meta()

head, action = st.columns([4, 1])
head.markdown("## My queue" if user["role"] == "agent" else "## All queues")
with action.popover("Log a phone call", width="stretch"):
    with st.form("phone"):
        caller = st.text_input("Caller employee ID", placeholder="EMP1012")
        cat = st.selectbox("Category", [c["id"] for c in meta["categories"]],
                           format_func=lambda c: next(x["name"] for x in meta["categories"] if x["id"] == c))
        desc = st.text_input("Short description")
        c1, c2 = st.columns(2)
        imp = c1.selectbox("Impact", list(meta["impact_scale"]), index=3, format_func=meta["impact_scale"].get)
        urg = c2.selectbox("Urgency", list(meta["urgency_scale"]), index=2, format_func=meta["urgency_scale"].get)
        if st.form_submit_button("Create and take over", type="primary"):
            try:
                t = client.post("/tickets", {"caller_employee_id": caller.strip(), "category_id": cat,
                                             "short_description": desc, "impact": imp, "urgency": urg})
            except ApiError as e:
                error(e)
            else:
                open_ticket(t["ticket_id"])

f1, f2, f3, f4 = st.columns([2, 2, 2, 1])
queues = f1.multiselect("Queue", user["queues"] if user["role"] == "agent" else meta["queues"])
prios = f2.multiselect("Priority", ["P1", "P2", "P3", "P4"])
who = f3.segmented_control("Show", ["All", "Mine", "Unowned", "Waiting on a human"], default="All")
closed = f4.toggle("Include resolved")


@st.fragment(run_every="15s")
def table() -> None:
    rows = client.get("/queue", include_closed=str(closed).lower())
    if queues:
        rows = [r for r in rows if r["queue"] in queues]
    if prios:
        rows = [r for r in rows if r["priority"] in prios]
    if who == "Mine":
        rows = [r for r in rows if r["owner_id"] == user["user_id"]]
    elif who == "Unowned":
        rows = [r for r in rows if not r["owner_id"]]
    elif who == "Waiting on a human":
        rows = [r for r in rows if r["status"] == "ESCALATION_QUEUED"]
    if not rows:
        st.info("Nothing here right now. New escalations appear automatically.")
        return
    df = pd.DataFrame([{
        "Ticket": r["ticket_id"], "P": r["priority"], "State": STATE_LABEL.get(r["status"], r["status"]),
        "Short description": r["summary"][:90], "Queue": r["queue"], "Owner": r["owner"] or "—",
        "Response": "met" if r["sla"].get("response_state") == "met" else countdown(r["sla"].get("response_due")),
        "Resolve": "met" if r["sla"].get("resolve_state") == "met" else countdown(r["sla"].get("resolve_due")),
        "SLA": {"ok": "🟢", "at_risk": "🟠", "breached": "🔴", "met": "✔"}.get(r["sla"].get("resolve_state"), ""),
        "Channel": r["channel"], "Incident": "parent" if r["is_incident"] else (r["incident_parent"] or ""),
    } for r in rows])
    st.caption(f"{len(rows)} tickets · refreshes every 15 s · click a row to open it")
    ev = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                      key="queue_table", column_config={"P": st.column_config.TextColumn(width="small"),
                                                        "SLA": st.column_config.TextColumn(width="small")})
    if ev.selection.rows:
        open_ticket(df.iloc[ev.selection.rows[0]]["Ticket"])


table()
