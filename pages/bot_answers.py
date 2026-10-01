"""Bot answers log (spec §8.2): every KB answer the bot gave, with a per-article success rate."""
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from ui import client
from ui.common import local, open_ticket

meta = client.meta()
cats = {c["id"]: c["name"] for c in meta["categories"]}
st.markdown("## Bot answers log")
st.caption("Every step the bot sent, straight from a KB article. Articles with a low success rate are the first to rewrite.")

f = st.columns(3)
kb = f[0].selectbox("Article", ["", *meta["kb"]], format_func=lambda k: "All articles" if not k else
                    f"{k} {meta['kb'][k]}")
cat = f[1].selectbox("Category", ["", *cats], format_func=lambda c: "All categories" if not c else cats[c])
rng = f[2].date_input("Dates", (date.today() - timedelta(days=30), date.today()), max_value=date.today())
d_from, d_to = (rng[0], rng[1]) if isinstance(rng, (list, tuple)) and len(rng) == 2 else (rng, rng)
data = client.get("/analytics/bot-answers", kb_id=kb or None, category_id=cat or None, date_from=str(d_from),
                  date_to=str(d_to))

succ = pd.DataFrame(data["success"], columns=["kb_id", "title", "sent", "fixed", "not_fixed", "success_rate"])
st.markdown("#### Success rate by article")
if succ.empty:
    st.info("No KB answers sent yet.")
else:
    succ["success_rate"] = (succ["success_rate"] * 100).round()
    st.dataframe(succ.rename(columns={"kb_id": "Article", "title": "Title", "sent": "Sent", "fixed": "Fixed",
                                      "not_fixed": "Not fixed", "success_rate": "Success %"}),
                 hide_index=True, width="stretch", column_config={
                     "Success %": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d%%")})

st.markdown("#### Answers")
rows = data["answers"]
if rows:
    df = pd.DataFrame([{"When": local(r["created_at"]), "Ticket": r["ticket_id"] or "(how-to, no ticket)",
                        "Article": r["kb_id"], "Category": r["category"],
                        "Attempt": r["attempt"] or "how-to", "Steps sent": r["steps_text"],
                        "Outcome": r["outcome"] or "pending"} for r in rows])
    ev = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                      key="answers_table")
    if ev.selection.rows and df.iloc[ev.selection.rows[0]]["Ticket"].startswith("TKT"):
        st.session_state.pop("answers_table", None)
        open_ticket(df.iloc[ev.selection.rows[0]]["Ticket"])
else:
    st.caption("Nothing matches these filters.")
