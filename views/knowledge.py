"""Knowledge (support): the employee-friendly version of every article, the approval workflow, the list of
questions no article answered, and saved replies. Only approved text reaches employees."""
import pandas as pd
import streamlit as st

from ui import client
from ui.client import ApiError
from ui.common import local

user = st.session_state.user
is_sup = user["role"] == "supervisor"
meta = client.meta()
st.markdown("## Knowledge")

tab_a, tab_g, tab_r = st.tabs(["Articles", "Knowledge gaps", "Saved replies"])

# ------------------------------------------------------------------ articles
with tab_a:
    arts = client.get("/kb")
    waiting = [a for a in arts if a["draft_version"]]
    st.caption(f"{sum(1 for a in arts if a['live_version'])} of {len(arts)} articles have an approved employee "
               f"version · {len(waiting)} draft(s) waiting for approval. Employees see only approved text.")
    st.dataframe(pd.DataFrame([{
        "Article": a["kb_id"], "Title": a["title"], "Category": a["category"],
        "Live": f"v{a['live_version']}" if a["live_version"] else "— (plain article)",
        "Draft waiting": f"v{a['draft_version']}" if a["draft_version"] else "",
        "Helpful": None if a["helpful_rate"] is None else round(a["helpful_rate"] * 100),
        "Ratings": a["helpful"] + a["not_helpful"]} for a in arts]),
        hide_index=True, width="stretch", height=300,
        column_config={"Helpful": st.column_config.ProgressColumn("Helpful %", min_value=0, max_value=100,
                                                                  format="%d%%")})

    kb_id = st.selectbox("Open an article", [a["kb_id"] for a in arts], index=None, placeholder="Choose an article",
                         format_func=lambda i: f"{i} · {next(a['title'] for a in arts if a['kb_id'] == i)}")
    if kb_id:
        art = client.get(f"/kb/{kb_id}")
        versions = art["versions"]
        live = next((v for v in versions if v["status"] == "approved"), None)
        draft = next((v for v in versions if v["status"] == "draft"), None)
        left, right = st.columns(2)
        with left, st.container(border=True):
            st.markdown("**Original article (written for agents)**")
            st.caption(art["handling"])
            st.markdown("\n".join(f"{i}. {s}" for i, s in enumerate(art["original_steps"], 1)))
        with right, st.container(border=True):
            shown = draft or live
            st.markdown(f"**{'Draft' if draft else 'Live'} employee version v{shown['version']}**" if shown else
                        "**No employee version yet**")
            if shown:
                st.markdown(f"_{shown['summary']}_")
                for n in (1, 2):
                    step = shown.get(f"attempt{n}")
                    if not step:
                        continue
                    st.markdown(f"**Fix {n}** · {'the employee does it' if step['who'] == 'you' else 'IT does it'}")
                    if step["who"] == "you":
                        st.markdown((step.get("intro") or "") + "\n\n" +
                                    "\n".join(f"{i}. {s}" for i, s in enumerate(step["steps"], 1)))
                        if step.get("why"):
                            st.caption(f"Why this helps: {step['why']}")
                    else:
                        st.markdown(step.get("note") or "")
                if shown.get("handoff_message"):
                    st.markdown(f"**When a person must take over:** {shown['handoff_message']}")
                mc = shown.get("meaning_check")
                if mc:
                    st.markdown("**Meaning check:** " + ("✅ passed" if mc["passed"] else "⚠️ flagged"))
                    for item in mc["items"]:
                        st.caption(f"Fix {item['attempt']}: same request {item['same']:.0%} · adds an action "
                                   f"{item['adds']:.0%} {'✅' if item['ok'] else '⚠️'}")
        if draft:
            c1, c2, c3 = st.columns([1, 1, 2])
            if c1.button("Run meaning check", key=f"chk-{draft['id']}"):
                try:
                    client.post(f"/kb/versions/{draft['id']}/check")
                    st.rerun()
                except ApiError as e:
                    st.error(str(e.detail))
            if is_sup:
                override = c3.text_input("Reason, only if approving despite a flag", key=f"ovr-{draft['id']}",
                                         label_visibility="collapsed",
                                         placeholder="Reason, only if approving despite a flag")
                if c2.button("Approve and publish", type="primary", key=f"apr-{draft['id']}"):
                    try:
                        client.post(f"/kb/versions/{draft['id']}/approve", {"override_reason": override or None})
                        st.success("Published: employees now see this version.")
                        st.rerun()
                    except ApiError as e:
                        st.error(str(e.detail))
            else:
                c3.caption("A supervisor approves drafts before employees see them.")

        with st.expander("✏️ Write a new draft"):
            base = draft or live or {}
            with st.form(f"draft-{kb_id}"):
                title = st.text_input("Title (plain words)", base.get("title") or art["title"])
                summary = st.text_area("What's happening, in one or two friendly sentences", base.get("summary") or "")
                data = {}
                for n, orig in ((1, art["original_attempt1"]), (2, art["original_attempt2"])):
                    if not orig:
                        continue
                    step = base.get(f"attempt{n}") or {"who": "you", "steps": []}
                    st.markdown(f"**Fix {n}** · original: _{orig}_")
                    who = st.radio("Who does it?", ["you", "it"], index=0 if step["who"] == "you" else 1,
                                   horizontal=True, key=f"who{n}-{kb_id}",
                                   format_func=lambda w: "The employee" if w == "you" else "IT (explain it)")
                    intro = st.text_input("Intro", step.get("intro") or "", key=f"intro{n}-{kb_id}")
                    steps = st.text_area("Steps, one per line", "\n".join(step.get("steps") or []), key=f"steps{n}-{kb_id}")
                    why = st.text_input("Why this helps (one sentence)", step.get("why") or "", key=f"why{n}-{kb_id}")
                    note = st.text_input("If IT does it: what happens next", step.get("note") or "", key=f"note{n}-{kb_id}")
                    data[f"attempt{n}"] = {"who": who, "intro": intro or None, "why": why or None, "note": note or None,
                                           "steps": [s.strip() for s in steps.splitlines() if s.strip()]}
                handoff = st.text_area("When a person must take over: what the employee is told",
                                       base.get("handoff_message") or "")
                change = st.text_input("What changed and why", placeholder="e.g. Clearer step 2, real menu names")
                if st.form_submit_button("Save draft"):
                    try:
                        client.post(f"/kb/{kb_id}/drafts", {"title": title, "summary": summary, **data,
                                                            "handoff_message": handoff or None, "note": change})
                        st.success("Draft saved. Run the meaning check, then a supervisor approves it.")
                        st.rerun()
                    except ApiError as e:
                        st.error(str(e.detail))
        with st.expander(f"History ({len(versions)} versions)"):
            st.dataframe(pd.DataFrame([{"Version": v["version"], "Status": v["status"], "By": v["author"],
                                        "Created": local(v["created_at"]), "Approved by": v["approved_by"] or "",
                                        "Change": v["change_note"] or ""} for v in versions]),
                         hide_index=True, width="stretch")

# ------------------------------------------------------------------ knowledge gaps
with tab_g:
    days = st.segmented_control("Period", [7, 30, 90], default=7, format_func=lambda d: f"Last {d} days")
    gaps = client.get("/kb/gaps", days=days or 7)
    if not gaps:
        st.info("No unanswered questions in this period. 🎉")
    st.caption("Real questions no article could answer, most frequent first. Write or extend an article for the "
               "top ones.")
    for g in gaps:
        with st.container(border=True):
            st.markdown(f"**{g['category']}** · asked {g['count']} time{'s' if g['count'] != 1 else ''}")
            for e in g["examples"]:
                st.markdown(f"- _{e['question'][:200]}_ <span class='muted'>({local(e['at'])})</span>",
                            unsafe_allow_html=True)
            if g["nearest"]:
                st.caption("Closest existing articles: " + ", ".join(f"{n['kb_id']} {n['title']}" for n in g["nearest"]))

# ------------------------------------------------------------------ saved replies
with tab_r:
    st.caption("Agents pick these in the ticket's comment box. Placeholders: {first_name} {ticket_id} {agent_name}.")
    replies = client.get("/saved-replies")
    for r in replies:
        with st.expander(("" if r["active"] else "🚫 ") + r["title"]):
            if is_sup:
                with st.form(f"sr-{r['id']}"):
                    t_ = st.text_input("Title", r["title"])
                    b_ = st.text_area("Text", r["body"])
                    act_ = st.checkbox("Active", bool(r["active"]))
                    if st.form_submit_button("Save"):
                        client.call("POST", "/saved-replies", params={"reply_id": r["id"]},
                                    json={"title": t_, "body": b_, "active": act_})
                        st.rerun()
            else:
                st.markdown(r["body"])
    if is_sup:
        with st.form("sr-new", clear_on_submit=True):
            st.markdown("**New saved reply**")
            t_ = st.text_input("Title")
            b_ = st.text_area("Text", placeholder="Hi {first_name}, …")
            if st.form_submit_button("Add") and t_.strip() and b_.strip():
                client.post("/saved-replies", {"title": t_, "body": b_})
                st.rerun()
