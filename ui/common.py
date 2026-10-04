"""Shared look and live widgets: CSS tokens, priority/SLA chips, alert polling, notifications."""
from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from ui import client
from ui.client import ApiError

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Sans+Condensed:wght@500;600&display=swap');
:root{
  --ink:#1E2A38; --muted:#5B6B7C; --paper:#F7F9FB; --line:#D9E0E7; --desk:#2C5E8C;
  --p1:#C0362C; --p2:#C9731C; --p3:#2F7A86; --p4:#6B7B86; --ok:#2E7D4F;
}
html, body, [class*="css"], .stMarkdown, .stButton button, input, textarea, select {
  font-family:'IBM Plex Sans', system-ui, sans-serif !important; }
h1,h2,h3 { font-weight:600 !important; letter-spacing:-0.01em; color:var(--ink); }
h1 { font-size:1.75rem !important; } h2 { font-size:1.35rem !important; } h3 { font-size:1.1rem !important; }
.block-container { padding-top:2.2rem; max-width:1320px; }
.chip { display:inline-block; padding:1px 9px; border-radius:3px; font-weight:600; font-size:.78rem;
  line-height:1.5; color:#fff; font-family:'IBM Plex Sans Condensed', sans-serif; font-variant-numeric:tabular-nums; }
.P1{background:var(--p1)} .P2{background:var(--p2)} .P3{background:var(--p3)} .P4{background:var(--p4)}
.state { display:inline-block; padding:1px 8px; border:1px solid var(--line); border-radius:3px;
  font-size:.78rem; color:var(--ink); background:#fff; }
.sla { display:inline-block; padding:2px 10px; border-radius:12px; font-size:.8rem; font-weight:500;
  font-variant-numeric:tabular-nums; border:1px solid transparent; }
.sla.ok{ background:#E7F2EC; color:var(--ok);} .sla.at_risk{ background:#FBF0E1; color:#9A560F;}
.sla.breached{ background:#F8E3E1; color:var(--p1);} .sla.met{ background:#EAEFF4; color:var(--muted);}
/* the ticket header strip: the one loud element */
.tk-head { display:flex; align-items:stretch; gap:0; border:1px solid var(--line); border-radius:6px;
  background:#fff; margin-bottom:.8rem; overflow:hidden; }
.tk-prio { display:flex; align-items:center; justify-content:center; min-width:74px; color:#fff;
  font:600 1.35rem 'IBM Plex Sans Condensed', sans-serif; }
.tk-body { padding:.55rem 1rem; flex:1; }
.tk-id { font:600 1.3rem 'IBM Plex Sans', sans-serif; color:var(--ink); margin-right:.6rem;
  font-variant-numeric:tabular-nums; }
.tk-sub { color:var(--muted); font-size:.86rem; margin-top:2px; }
.tk-timers { display:flex; flex-direction:column; justify-content:center; gap:4px; padding:.5rem 1rem;
  border-left:1px solid var(--line); min-width:220px; }
.banner { border-radius:5px; padding:.55rem .9rem; margin:.2rem 0 .6rem; font-size:.92rem; }
.banner.p1 { background:var(--p1); color:#fff; font-weight:500; }
.banner.sys { background:#FBF0E1; color:#7A4510; border:1px solid #EDD2AE; }
.field-label { color:var(--muted); font-size:.8rem; margin-bottom:0; }
.field-value { font-size:.95rem; margin-bottom:.45rem; }
.tl { border-left:2px solid var(--line); margin-left:.4rem; padding-left:.9rem; }
.tl-row { font-size:.86rem; margin:.1rem 0 .45rem; }
.tl-time { color:var(--muted); font-variant-numeric:tabular-nums; margin-right:.4rem; }
.tl-internal { background:#FFF8E6; border-radius:3px; padding:0 4px; }
.flag-bar { height:6px; background:#EAEFF4; border-radius:3px; overflow:hidden; margin:2px 0 6px; }
.flag-bar > div { height:6px; border-radius:3px; }
.muted { color:var(--muted); font-size:.85rem; }
div[data-testid="stMetricValue"] { font-variant-numeric:tabular-nums; }
</style>
"""

PRIO_COLOR = {"P1": "#C0362C", "P2": "#C9731C", "P3": "#2F7A86", "P4": "#6B7B86"}
STATE_LABEL = {"ESCALATION_QUEUED": "Escalation queued", "HUMAN_ASSIGNED": "Assigned",
               "HUMAN_IN_PROGRESS": "In progress", "RESOLVED_PENDING_CONFIRMATION": "Resolved, awaiting confirmation",
               "WAITING_FOR_USER": "Waiting for user", "WAITING_FOR_VALIDATION_1": "Bot: awaiting check (1)",
               "WAITING_FOR_VALIDATION_2": "Bot: awaiting check (2)", "VERIFICATION_PENDING": "Verification pending",
               "COLLECTING_INFORMATION": "Bot: collecting info", "READY_FOR_RESOLUTION": "Ready for resolution",
               "CLASSIFYING": "Classifying", "NEW": "New", "ATTEMPT_1": "Bot attempt 1", "ATTEMPT_2": "Bot attempt 2",
               "RESOLVED": "Resolved", "CLOSED": "Closed", "CANCELLED": "Cancelled", "REOPENED": "Reopened"}


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def chip(p: str | None) -> str:
    return f'<span class="chip {p}">{p}</span>' if p else ""


def state(s: str) -> str:
    return f'<span class="state">{STATE_LABEL.get(s, s)}</span>'


def local(ts: str | None, with_date: bool = True) -> str:
    if not ts:
        return "—"
    d = datetime.fromisoformat(ts).astimezone()
    return d.strftime("%d %b %H:%M" if with_date else "%H:%M")


def countdown(due: str | None) -> str:
    if not due:
        return "—"
    secs = (datetime.fromisoformat(due) - datetime.now(timezone.utc)).total_seconds()
    m = int(abs(secs) // 60)
    txt = f"{m // 1440}d {m % 1440 // 60}h" if m >= 1440 else f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"
    return f"{txt} left" if secs >= 0 else f"{txt} overdue"


def sla_pill(label: str, due: str | None, st_: str | None) -> str:
    if not st_:
        return ""
    txt = "met" if st_ == "met" else countdown(due)
    return f'<span class="sla {st_}">⏱ {label} · {txt}</span>'


def error(e: ApiError) -> None:
    if e.body.get("missing"):
        st.error(f"{e.detail.split(':')[0]}. Still needed:\n\n" + "\n".join(f"- {m}" for m in e.body["missing"]))
    else:
        st.error(str(e.detail))


def open_ticket(tid: str) -> None:
    st.session_state.open_ticket = tid
    st.session_state.pop("queue_table", None)  # forget the row selection, or the queue would reopen it
    st.switch_page("views/ticket.py")


# ------------------------------------------------------------------ support: live alerts (spec §6.5)
@st.dialog("P1 alert: a human is needed now", width="large")
def _p1_dialog(a: dict) -> None:
    st.markdown(f"{chip('P1')} **{a['ticket_id']}** → {a['target_queue']}", unsafe_allow_html=True)
    st.markdown(f"> {a.get('summary') or a['text']}")
    if a["realert_count"]:
        st.caption(f"Re-alert #{a['realert_count']}: nobody has acknowledged this yet.")
    if a.get("escalation_reason") and a["escalation_reason"].startswith("unacknowledged"):
        st.warning(f"Escalated to supervisor: {a['escalation_reason']}")
    others = [o for o in st.session_state.get("p1_fresh", []) if o["alert_id"] != a["alert_id"]]
    if others:
        st.caption(f"{len(others)} more P1 waiting: {', '.join(o['ticket_id'] for o in others[:5])}. They're in the "
                   "red banner and your queue; **Later** snoozes them all until their next re-alert.")
    c1, c2, c3 = st.columns(3)
    if c1.button("Acknowledge", type="primary", width="stretch"):
        client.post(f"/alerts/{a['alert_id']}/ack")
        st.session_state.p1_modal = None
        st.rerun()
    if c2.button("Take over", width="stretch"):
        try:
            client.post(f"/tickets/{a['ticket_id']}/takeover")
        except ApiError as e:
            st.error(str(e.detail))
            return
        st.session_state.p1_modal = None
        open_ticket(a["ticket_id"])
    if c3.button("Later", width="stretch"):
        # every P1 waiting right now, not just this one: with three P1s, Later showed the next dialog straight
        # away, so a supervisor had to click through them all (4 Oct)
        for o in [a, *st.session_state.get("p1_fresh", [])]:
            st.session_state.p1_seen[o["alert_id"]] = o["realert_count"]
        st.session_state.p1_modal = None
        st.rerun()


@st.fragment(run_every="10s")
def alert_center() -> None:
    """Polls /alerts/pending every 10 s: P1 → modal + red banner, P2 → toast, others → badges."""
    try:
        data = client.get("/alerts/pending")
    except ApiError:
        return
    ss = st.session_state
    ss.setdefault("p1_seen", {})
    ss.setdefault("toasted", set())
    ss.alert_counts = data["counts"]
    if data.get("degraded"):
        st.markdown(f'<div class="banner sys">⚠️ Model degraded: Jev failed at {local(data["degraded"]["since"])}. '
                    "New tickets are being routed to people.</div>", unsafe_allow_html=True)
    for a in data["alerts"]:
        if a["level"] == "system":
            st.markdown(f'<div class="banner sys">🛰️ {a["text"]}</div>', unsafe_allow_html=True)
    p1 = [a for a in data["alerts"] if a["priority"] == "P1"]
    if p1:
        ids = ", ".join(a["ticket_id"] for a in p1[:4])
        st.markdown(f'<div class="banner p1">🔴 {len(p1)} unacknowledged P1: {ids}</div>',
                    unsafe_allow_html=True)
    if data.get("backlog"):
        st.markdown(f'<div class="banner sys">Alert backlog: {data["total"]} unacknowledged alerts.</div>',
                    unsafe_allow_html=True)
    for a in data["alerts"]:
        key = (a["alert_id"], a["realert_count"])
        if a["level"] == "toast" and key not in ss.toasted:  # P2; P3/P4 are badge + queue list only
            ss.toasted.add(key)
            st.toast(f"{a['priority']} {a['ticket_id']} → {a['target_queue']}", icon="🔔")
        elif a["event"] == "sla_risk" and key not in ss.toasted:
            ss.toasted.add(key)
            st.toast(a["text"], icon="⏱️")
    fresh = [a for a in p1 if ss.p1_seen.get(a["alert_id"], -1) < a["realert_count"]]
    ss.p1_fresh = fresh
    if not ss.get("p1_modal"):
        if fresh:
            ss.p1_modal = fresh[0]["alert_id"]
    current = next((a for a in p1 if a["alert_id"] == ss.get("p1_modal")), None)
    if not current:
        ss.p1_modal = None
    # The dialog itself is drawn by p1_dialog() from the main page on each full run. Opening it from this
    # 10-second check stacked a new copy on every check and on every re-alert (4 Oct). Here we only notice a new
    # alert or re-alert (or one that's gone) and reload the page once so it's drawn fresh, exactly once.
    key = (current["alert_id"], current["realert_count"]) if current else None
    ss.p1_current = current
    if ss.get("p1_dialog_shown") != key:
        ss.p1_dialog_shown = key
        st.rerun(scope="app")


def p1_dialog() -> None:
    """Call from the main page (not a fragment) after alert_center(): shows the due P1 alert, if any. Drawn on
    every full run while it's due, so its buttons work and there's never more than one."""
    current = st.session_state.get("p1_current")
    if current:
        _p1_dialog(current)


@st.fragment(run_every="10s")
def sidebar_badges() -> None:
    counts = st.session_state.get("alert_counts") or {}
    parts = [f'{chip(p)} {counts[p]}' for p in ("P1", "P2", "P3", "P4") if counts.get(p)]
    st.markdown("**Unacknowledged alerts**<br>" + ("&nbsp; ".join(parts) if parts else
                                                  '<span class="muted">None</span>'), unsafe_allow_html=True)


# ------------------------------------------------------------------ notifications (both sides)
@st.fragment(run_every="10s")
def notifications() -> None:
    try:
        rows = client.get("/me/notifications", unread="true")
    except ApiError:
        return
    for n in reversed(rows):
        st.toast(n["text"], icon="💬")
    if rows:
        client.post("/me/notifications/read", {"ids": [n["id"] for n in rows]})
