"""Bot answers log (spec §8.2): every set of fix steps the bot sent, and how often each article fixed the problem.
Agents see their own teams only; a supervisor sees every team and can narrow it down (7 Oct: every agent saw
every team's answers, and opening another team's ticket said "not found")."""
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from ui import client
from ui.common import local, open_ticket

user = st.session_state.user
is_sup = user["role"] == "supervisor"
meta = client.meta()
cats = {c["id"]: c["name"] for c in meta["categories"]}
OUTCOMES = {"": "All outcomes", "fixed": "Fixed", "not_fixed": "Not fixed", "escalated": "Escalated",
            "pending": "Waiting for the employee"}

st.markdown("## Bot answers log")
st.caption("Every set of fix steps the bot sent, straight from a KB article, and what happened next. "
           + ("You see every team; narrow it down with the filters." if is_sup else
              f"You see your team{'s' if len(user['queues']) > 1 else ''}: {', '.join(user['queues'])}."))

# ------------------------------------------------------------------ filters: one row, like User accounts
f1 = st.columns([3, 2, 2] if is_sup else [3, 2])
search = f1[0].text_input("Find", placeholder="Ticket number, article or words in the steps",
                          label_visibility="collapsed")
team_opts = meta["queues"] if is_sup else user["queues"]
team = f1[1].selectbox("Team", ["", *team_opts], format_func=lambda q: q or ("All teams" if is_sup else
                                                                           "All my teams"),
                       label_visibility="collapsed")
agent = ""
if is_sup:
    staff = {u["user_id"]: u["name"] for u in client.get("/users") if u["role"] == "agent"}
    agent = f1[2].selectbox("Agent", ["", *staff], format_func=lambda a: staff[a] if a else "All agents",
                            label_visibility="collapsed")
f2 = st.columns([2, 2, 2, 3])
cat = f2[0].selectbox("Category", ["", *cats], format_func=lambda c: cats[c] if c else "All categories",
                      label_visibility="collapsed")
kb = f2[1].selectbox("Article", ["", *meta["kb"]], format_func=lambda k: f"{k} {meta['kb'][k]}" if k else
                     "All articles", label_visibility="collapsed")
outcome = f2[2].selectbox("Outcome", list(OUTCOMES), format_func=OUTCOMES.get, label_visibility="collapsed")
rng = f2[3].date_input("Dates", (date.today() - timedelta(days=30), date.today()), max_value=date.today(),
                       label_visibility="collapsed")
d_from, d_to = (rng[0], rng[1]) if isinstance(rng, (list, tuple)) and len(rng) == 2 else (rng, rng)

data = client.get("/analytics/bot-answers", search=search or None, queue=[team] if team else None,
                  agent=agent or None, category_id=[cat] if cat else None, kb_id=kb or None,
                  outcome=outcome or None, date_from=str(d_from), date_to=str(d_to))

# ------------------------------------------------------------------ success rate: the same rows as the list below
st.markdown("#### Success rate by article")
st.caption("Success = fixed ÷ sent, counting only fix steps sent inside a ticket (attempt 1 or 2). "
           "The lowest articles are the first to rewrite.")
succ = pd.DataFrame(data["success"], columns=["kb_id", "title", "sent", "fixed", "not_fixed", "escalated",
                                              "success_rate"])
if succ.empty:
    st.info("No fix steps sent for these filters.")
else:
    succ["success_rate"] = (succ["success_rate"] * 100).round()
    st.dataframe(succ.rename(columns={"kb_id": "Article", "title": "Title", "sent": "Sent", "fixed": "Fixed",
                                      "not_fixed": "Not fixed", "escalated": "Escalated",
                                      "success_rate": "Success %"}),
                 hide_index=True, width="stretch", column_config={
                     "Success %": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d%%")})

# ------------------------------------------------------------------ the answers
st.markdown("#### Answers")
rows = data["answers"]
if rows:
    st.caption(f"{data['total']} answer{'s' if data['total'] != 1 else ''}"
               + (" (showing the newest 1000)" if data["total"] > len(rows) else "")
               + " · click a row with a ticket number to open the ticket")
    df = pd.DataFrame([{"When": local(r["created_at"]), "Ticket": r["ticket_id"] or "(how-to, no ticket)",
                        "Team": r["team"] or "—", "Agent": r["agent"] or "—", "Article": r["kb_id"],
                        "Category": r["category"], "Attempt": r["attempt"] or "how-to",
                        "Outcome": OUTCOMES.get(r["outcome"], r["outcome"]), "Steps sent": r["steps_text"]}
                       for r in rows])
    ev = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                      key="answers_table")
    if ev.selection.rows and df.iloc[ev.selection.rows[0]]["Ticket"].startswith("TKT"):
        st.session_state.pop("answers_table", None)
        open_ticket(df.iloc[ev.selection.rows[0]]["Ticket"])
else:
    st.caption("Nothing matches these filters.")
