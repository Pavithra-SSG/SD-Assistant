"""Corrections log (supervisor): every bot decision a human fixed, with the reason. Improvement data."""
import pandas as pd
import streamlit as st

from ui import client
from ui.common import local

meta = client.meta()
cats = {c["id"]: c["name"] for c in meta["categories"]}
st.markdown("## Corrections log")
st.caption("Bot mistakes fixed by people. Category corrections feed C9, and `python evaluate.py --mode corrections` "
           "replays them as evaluation cases.")
rows = client.get("/corrections")
if not rows:
    st.info("No corrections yet.")
    st.stop()


def name(field, v):
    return cats.get(v, v) if field == "category_id" else v


st.dataframe(pd.DataFrame([{"When": local(r["created_at"]), "Ticket": r["ticket_id"], "Field": r["field"],
                            "Bot said": name(r["field"], r["old_value"]), "Corrected to": name(r["field"], r["new_value"]),
                            "By": f"{r.get('corrected_by_name') or r['corrected_by']} ({r['corrected_by']})",
                            "Reason": r["reason"] or ""} for r in rows]),
             hide_index=True, width="stretch")
