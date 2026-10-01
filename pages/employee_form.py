"""Employee ticket form: stages 1–3 → Review (one Jev check) → Submit → Fix now / Just log (spec §6.3, §9)."""
import streamlit as st

from ui import client
from ui.client import ApiError
from ui.common import chip

ss = st.session_state
meta = client.meta()
me = client.get("/auth/me")
cats = {c["id"]: c["name"] for c in meta["categories"]}


def reset() -> None:
    for key in [k for k in ss if str(k).startswith("f_")] + ["form_review", "form_result"]:
        ss.pop(key, None)


st.markdown("## Report an issue")
st.caption("Fill in what you know. Priority is worked out for you from impact and urgency.")

if ss.get("form_result"):
    res = ss.form_result
    if res["outcome"] == "choose":
        st.success(res["message"])
        st.markdown(f"Priority {chip(res['priority'])}", unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        if c1.button("Fix it now with the assistant", type="primary", width="stretch"):
            try:
                out = client.post(f"/forms/{res['ticket_id']}/choice", {"choice": "fix_now"})
            except ApiError as e:
                st.error(str(e.detail))
            else:
                ss.chat_sid = out["session_id"]
                reset()
                st.switch_page("pages/employee_chat.py")
        if c2.button("Just log it for the team", width="stretch"):
            try:
                out = client.post(f"/forms/{res['ticket_id']}/choice", {"choice": "just_log"})
            except ApiError as e:
                st.error(str(e.detail))
            else:
                ss.form_result = {"outcome": "logged", "ticket_id": res["ticket_id"], "message": out["reply"]}
                st.rerun()
    else:
        (st.info if res["outcome"] == "added_to_existing" else st.success)(res["message"])
    b1, b2 = st.columns(2)
    if b1.button("Open My tickets", width="stretch"):
        reset()
        st.switch_page("pages/my_tickets.py")
    if b2.button("Report another issue", width="stretch"):
        reset()
        st.rerun()
    st.stop()

# ------------------------------------------------------------------ stage 1: requester (read-only)
with st.container(border=True):
    st.markdown("**1 · Requester**")
    c1, c2, c3 = st.columns(3)
    c1.text_input("Name", f"{me['name']} ({me['user_id']})", disabled=True)
    c2.text_input("Location", me.get("location") or "—", disabled=True)
    c3.text_input("Device (asset tag)", me.get("asset_tag") or "—", disabled=True)
    st.caption("Taken from your sign-in. Reporting for someone else? They need to raise it themselves.")

# ------------------------------------------------------------------ stage 2: what's wrong
with st.container(border=True):
    st.markdown("**2 · What's wrong**")
    c1, c2 = st.columns(2)
    cat = c1.selectbox("Category", ["NOT_SURE", *cats], key="f_cat",
                       format_func=lambda c: "Not sure" if c == "NOT_SURE" else cats[c])
    issue = c2.selectbox("Issue type", meta["issue_types"].get(cat, ["Other"]) if cat != "NOT_SURE" else ["Other"],
                         key="f_issue")
    desc = st.text_input("Short description", key="f_desc", max_chars=300,
                         placeholder="One sentence, e.g. VPN shows error 809 when I connect from home")
    c1, c2 = st.columns(2)
    impact = c1.radio("Who is affected?", meta["impact_options"], key="f_impact")
    workaround = c2.radio("Can you keep working?", meta["workaround_options"], key="f_work")

# ------------------------------------------------------------------ stage 3: details
with st.container(border=True):
    st.markdown("**3 · Details**")
    error = st.text_area("Exact error (optional)", key="f_error", max_chars=2000,
                         help="Paste the message you see. Never include passwords, codes or tokens.")
    details = {}
    fields = meta["form_fields"].get(cat, []) if cat != "NOT_SURE" else []
    cols = st.columns(2)
    for i, f in enumerate(fields):
        label = f["Help_Text"] + ("" if f["Required"] == "Yes" else " (optional)")
        col = cols[i % 2]
        key = f"f_d_{cat}_{f['Field_Name']}"
        opts = [o.strip() for o in f["Options_or_Source"].split("|")]
        if f["UI_Control"] in ("dropdown", "yes_no"):
            details[f["Field_Name"]] = col.selectbox(label, ["", *opts], key=key)
        elif f["UI_Control"] == "textarea":
            details[f["Field_Name"]] = col.text_area(label, key=key, placeholder=f["Options_or_Source"])
        elif f["UI_Control"] == "file":
            continue  # attachments are collected below
        else:
            details[f["Field_Name"]] = col.text_input(label, key=key, placeholder=f["Options_or_Source"])
    files = st.file_uploader("Screenshots (optional)", accept_multiple_files=True, key="f_files",
                             type=["png", "jpg", "jpeg", "webp"])
    st.caption("A screenshot of the error helps a lot. We read the text in it for you, and blur anything that "
               "looks like a password or code before saving it.")
    contact = st.radio("Preferred contact", meta["contact_options"], horizontal=True, key="f_contact")

missing = [f["Help_Text"] for f in fields if f["Required"] == "Yes" and f["UI_Control"] != "file"
           and not details.get(f["Field_Name"])]
form = {"category_id": None if cat == "NOT_SURE" else cat, "issue_type": issue, "short_description": desc.strip(),
        "impact_choice": impact, "workaround": workaround, "exact_error": error.strip() or None,
        "details": {k: v for k, v in details.items() if v},
        "attachment_ids": [], "preferred_contact": contact}

if st.button("Review", type="primary", disabled=len(desc.strip()) < 3):
    if missing:
        st.warning("Please fill in: " + "; ".join(missing))
    else:
        try:
            uploaded = ss.setdefault("f_uploaded", {})  # file name+size → attachment id: read each file once
            for f in (files or [])[:3]:
                key_ = f"{f.name}:{f.size}"
                if key_ not in uploaded:
                    with st.spinner(f"Reading your screenshot {f.name}…"):
                        uploaded[key_] = client.upload(f)["id"]
            form["attachment_ids"] = [uploaded[f"{f.name}:{f.size}"] for f in (files or [])[:3]]
            with st.spinner("Checking your ticket…"):
                ss.form_review = client.post("/forms/review", form)
        except ApiError as e:
            st.error(str(e.detail))

# ------------------------------------------------------------------ review
rv = ss.get("form_review")
if rv:
    with st.container(border=True):
        st.markdown("**Review**")
        for w in rv["warnings"]:
            st.warning(w)
        if rv.get("screenshot_note"):
            st.info("🖼️ " + rv["screenshot_note"])
        st.markdown(f"Priority {chip(rv['priority'])} &nbsp; <span class='muted'>worked out from who is affected "
                    "and whether you can keep working. You can't change it.</span>", unsafe_allow_html=True)
        st.markdown(f"**Category:** {rv['category_name']}")
        accept = False
        if rv.get("suggested_category_name"):
            accept = st.toggle(f"This sounds more like **{rv['suggested_category_name']}**. Use that instead?",
                               key="f_accept")
        dup_choice = None
        if rv.get("duplicate"):
            d = rv["duplicate"]
            dup_choice = st.radio(f"Is this about **{d['ticket_id']}** ({d['summary'][:70]}, {d['label']})?",
                                  ["No, it's a new issue", f"Yes, add this to {d['ticket_id']}"], key="f_dup")
        if rv.get("try_first"):
            tf = rv["try_first"]
            with st.expander(f"Try this first (optional): {tf['title']}"):
                st.markdown("\n".join(f"{i}. {x}" for i, x in enumerate(tf.get("steps") or [tf["step"]], 1)))
                st.caption("If it works, you don't need to submit anything.")
                if st.button("That fixed it", key="f_fixed"):
                    reset()
                    st.success("Great, nothing more to do.")
                    st.stop()
        if st.button("Submit ticket", type="primary"):
            dup = rv["duplicate"]["ticket_id"] if dup_choice and dup_choice.startswith("Yes") else None
            try:
                ss.form_result = client.post("/forms/submit", {"review_id": rv["review_id"],
                                                               "accept_suggestion": accept, "duplicate_of": dup})
            except ApiError as e:
                st.error(str(e.detail))
            else:
                st.rerun()
