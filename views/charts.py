"""Charts dashboard C1–C12 + KPI tiles (spec §8.4). All data from AnalyticsService (read-only)."""
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from ui import client
from ui.viz import CHANNEL, DESK, GRID, INK, OUTCOME, SLOTS, WHO, _layout, empty, hbar, show, stacked_bar

ss = st.session_state
user = ss.user
meta = client.meta()
cats = {c["id"]: c["name"] for c in meta["categories"]}

st.markdown("## Service desk charts" if user["role"] == "supervisor" else "## My queues at a glance")

# ------------------------------------------------------------------ filters: one row above the charts
f = st.columns([3, 2, 2, 2, 2])  # the date range needs the room, or the end date is cut off
rng = f[0].date_input("Dates", (date.today() - timedelta(days=30), date.today()), max_value=date.today())
qs = f[1].multiselect("Queue", user["queues"] if user["role"] == "agent" else meta["queues"])
cs = f[2].multiselect("Category", list(cats), format_func=cats.get)
ch = f[3].multiselect("Channel", meta["channels"])
ps = f[4].multiselect("Priority", ["P1", "P2", "P3", "P4"])
d_from, d_to = (rng[0], rng[1]) if isinstance(rng, (list, tuple)) and len(rng) == 2 else (rng, rng)
data = client.get("/analytics/dashboard", date_from=str(d_from), date_to=str(d_to), queue=qs, category=cs,
                  channel=ch, priority=ps)
k, c = data["kpis"], data["charts"]


def pct(x):
    return "—" if x is None else f"{x:.0%}"


# ------------------------------------------------------------------ KPI tiles
t = st.columns(6)
t[0].metric("Open", k["open"])
t[1].metric("P1 open", k["p1_open"])
t[2].metric("Awaiting a human", k["awaiting_human"])
t[3].metric("Bot resolution rate", pct(k["bot_resolution_rate"]))
t[4].metric("SLA met", pct(k["sla_met_pct"]))
t[5].metric("Reopen rate", pct(k["reopen_rate"]))
f = st.columns(4)  # wider than the 6-column row above, so "Answers rated helpful" isn't cut off
f[0].metric("Answers rated helpful", pct(k.get("answers_helpful_pct")),
            help=f"👍 share of {k.get('answer_ratings', 0)} ratings employees gave the bot's answers")
f[1].metric("Satisfaction", f"{k['satisfaction']:.1f} / 5" if k.get("satisfaction") else "—",
            help=f"Average of {k.get('satisfaction_ratings', 0)} ratings given when tickets were fixed")
if not k["total"]:
    st.info("No tickets in this range yet. Charts fill in as employees use chat and the form.")

left, right = st.columns(2, gap="large")

with left:  # C1
    stacked_bar(pd.DataFrame(c["C1"], columns=["day", "channel", "n"]), "day", "channel", "n", CHANNEL,
                "C1 · Tickets per day by channel", "c1")
with right:  # C2
    hbar(pd.DataFrame(c["C2"], columns=["category", "n"]), "category", "n", "C2 · Category mix", "c2")

with left:  # C3
    stacked_bar(pd.DataFrame(c["C3"], columns=["week", "outcome", "n"]), "week", "outcome", "n", OUTCOME,
                "C3 · Bot-resolved vs escalated vs human-resolved (by week)", "c3")
with right:  # C4
    hbar(pd.DataFrame(c["C4"], columns=["reason", "n"]), "reason", "n", "C4 · Escalation reasons", "c4")

with left:  # C5: SLA met % by priority, one axis (percent), policy target line
    df5 = pd.DataFrame(c["C5"], columns=["priority", "response_met_pct", "resolve_met_pct", "n"])
    if df5.empty:
        empty("C5 · SLA met % by priority")
    else:
        fig = go.Figure()
        for col, name, color in (("response_met_pct", "Response", SLOTS[0]), ("resolve_met_pct", "Resolve", SLOTS[1])):
            fig.add_bar(x=df5["priority"], y=df5[col], name=name, marker={"color": color, "cornerradius": 4},
                        hovertemplate=f"{name} %{{x}}: %{{y:.0%}}<extra></extra>")
        fig.add_hline(y=1, line={"dash": "dash", "color": INK, "width": 1},
                      annotation_text="Policy: every ticket within SLA", annotation_position="top left")
        fig.update_yaxes(tickformat=".0%", range=[0, 1.12])
        fig.update_layout(barmode="group", bargroupgap=0.12)
        show(_layout(fig, "C5 · SLA met % by priority", legend=True), df5, "c5")
with right:  # C6: box plot
    df6 = pd.DataFrame(c["C6"], columns=["who", "hours"])
    if df6.empty:
        empty("C6 · Time to resolve, bot vs human")
    else:
        fig = go.Figure()
        for who in [w for w in WHO if w in set(df6["who"])]:
            fig.add_box(y=df6[df6["who"] == who]["hours"], name=who, marker_color=WHO[who], boxpoints="all",
                        jitter=0.4, pointpos=0, line={"width": 2}, hovertemplate=f"{who}: %{{y:.1f}} h<extra></extra>")
        fig.update_yaxes(title="hours", rangemode="tozero")  # durations are never negative
        show(_layout(fig, "C6 · Time to resolve, bot vs human (hours)"), df6, "c6")

# C7: priority mix over time as small multiples (one series per panel; colour never has to separate P1–P4)
df7 = pd.DataFrame(c["C7"], columns=["day", "priority", "n"])
if df7.empty:
    empty("C7 · Priority mix over time")
else:
    fig = make_subplots(rows=1, cols=4, shared_yaxes=True, subplot_titles=["P1", "P2", "P3", "P4"],
                        horizontal_spacing=0.03)
    for i, p in enumerate(["P1", "P2", "P3", "P4"], start=1):
        part = df7[df7["priority"] == p]
        fig.add_scatter(x=part["day"], y=part["n"], mode="lines+markers", line={"color": DESK, "width": 2},
                        marker={"size": 8}, name=p, row=1, col=i, hovertemplate=f"{p} %{{x}}: %{{y}}<extra></extra>")
    fig.update_xaxes(showgrid=False, linecolor=GRID, type="category")
    show(_layout(fig, "C7 · Priority mix over time (tickets per day)", height=260), df7, "c7")

left, right = st.columns(2, gap="large")
with left:  # C8
    df8 = pd.DataFrame(c["C8"], columns=["day", "low_conf_rate", "n"])
    if df8.empty:
        empty("C8 · Jev low-confidence rate")
    else:
        fig = go.Figure(go.Scatter(x=df8["day"], y=df8["low_conf_rate"], mode="lines+markers",
                                   line={"color": DESK, "width": 2}, marker={"size": 8},
                                   hovertemplate="%{x}: %{y:.0%} of triages below the confidence threshold"
                                                 "<extra></extra>"))
        fig.update_yaxes(tickformat=".0%", rangemode="tozero")
        fig.update_xaxes(type="category")
        show(_layout(fig, "C8 · Jev low-confidence rate (tuning signal)"), df8, "c8")
with right:  # C9: category accuracy vs the 90% target
    df9 = pd.DataFrame(c["C9"], columns=["category", "corrections", "tickets", "accuracy"])
    if df9.empty:
        empty("C9 · Corrections by category")
    else:
        d9 = df9.sort_values("accuracy")
        fig = go.Figure(go.Bar(x=d9["accuracy"], y=d9["category"], orientation="h",
                               marker={"color": DESK, "cornerradius": 4}, customdata=d9[["corrections", "tickets"]],
                               hovertemplate="%{y}: %{x:.0%} (%{customdata[0]} corrected of %{customdata[1]})"
                                             "<extra></extra>"))
        fig.add_vline(x=0.9, line={"dash": "dash", "color": INK, "width": 1}, annotation_text="Target 90%",
                      annotation_position="bottom left")
        fig.update_xaxes(tickformat=".0%", range=[0, 1.05], showgrid=True, gridcolor=GRID)
        show(_layout(fig, "C9 · Category accuracy after corrections", height=max(240, 32 * len(d9) + 70)),
             df9, "c9")

with left:  # C10
    df10 = pd.DataFrame(c["C10"], columns=["kb_id", "title", "sent", "fixed", "not_fixed", "success_rate"])
    if df10.empty:
        empty("C10 · KB article success rate")
    else:
        d10 = df10.assign(label=df10["kb_id"] + " " + df10["title"].str[:28])
        hbar(d10[["label", "success_rate", "sent", "fixed"]].fillna(0), "label", "success_rate",
             "C10 · KB success rate (fixed ÷ sent). Rewrite the lowest first", "c10", fmt="%{x:.0%}")
with right:  # C11
    df11 = pd.DataFrame(c["C11"], columns=["age", "n"])
    fig = go.Figure(go.Bar(x=df11["age"], y=df11["n"], marker={"color": DESK, "cornerradius": 4}, text=df11["n"],
                           textposition="outside", cliponaxis=False, hovertemplate="%{x}: %{y} open<extra></extra>"))
    show(_layout(fig, "C11 · Open backlog by age"), df11, "c11")

# C12: safety scorecard as KPI tiles, each with an icon + label (never colour alone)
s = c["C12"]
st.markdown("#### C12 · Safety scorecard")


def status(ok: bool) -> str:
    return "✅ On target" if ok else "⛔ Off target"


t = st.columns(3)
rec = s["critical_recall"]
t[0].metric("Critical safety recall", pct(rec), help=f"{s['critical_cases']} safety/security cases. Target 100%")
t[0].caption(status(rec in (None, 1.0)))
t[1].metric("Unauthorized action rate", pct(s["unauthorized_action_rate"]),
            help=f"{s['restricted_actions']} restricted tool actions. Target 0%")
t[1].caption(status(s["unauthorized_action_rate"] == 0))
t[2].metric("Secret leakage", s["secret_leakage"], help="Stored messages that still contain a secret. Target 0")
t[2].caption(status(s["secret_leakage"] == 0))
with st.expander("Targets (metrics_acceptance.json)"):
    st.dataframe(pd.DataFrame(list(c["targets"].items()), columns=["Metric", "Target"]), hide_index=True,
                 width="stretch")
