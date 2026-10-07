"""My tickets: status, priority, owner, SLA target, customer-visible thread, reply, reopen (spec §7.4, §9)."""
import streamlit as st

from ui import client
from ui.client import ApiError
from ui.common import chip, local

ss = st.session_state
st.markdown("## My tickets")

rows = client.get("/me/tickets")
if not rows:
    st.info("You have no tickets yet. Use Chat or the Ticket form to report an issue.")
    st.stop()

show_closed = st.toggle("Show closed and cancelled", value=False)
view = [t for t in rows if show_closed or t["status"] not in ("CLOSED", "CANCELLED")]

left, right = st.columns([2, 3], gap="large")
with left:
    for t in view:
        active = ss.get("my_open") == t["ticket_id"]
        with st.container(border=True):
            st.markdown(f"{chip(t['priority'])} **{t['ticket_id']}** &nbsp; <span class='muted'>{t['state']}</span>",
                        unsafe_allow_html=True)
            st.markdown(t["summary"][:110])
            if st.button("Viewing" if active else "Open", key=f"open-{t['ticket_id']}", disabled=active,
                         width="stretch"):
                ss.my_open = t["ticket_id"]
                st.rerun()
if not ss.get("my_open") or ss.my_open not in {t["ticket_id"] for t in rows}:
    ss.my_open = view[0]["ticket_id"] if view else rows[0]["ticket_id"]

with right:
    t = client.get(f"/me/tickets/{ss.my_open}")
    st.markdown(f"### {t['ticket_id']} {chip(t['priority'])}", unsafe_allow_html=True)
    st.markdown(f"**{t['state']}**")
    c1, c2, c3 = st.columns(3)
    c1.markdown(f"<p class='field-label'>Category</p><p class='field-value'>{t['category']}"
                f"{' · ' + t['subcategory'] if t['subcategory'] else ''}</p>", unsafe_allow_html=True)
    c2.markdown(f"<p class='field-label'>Team</p><p class='field-value'>{t['queue']}</p>", unsafe_allow_html=True)
    c3.markdown(f"<p class='field-label'>Assigned to</p><p class='field-value'>{t['owner'] or '—'}</p>",
                unsafe_allow_html=True)
    c1.markdown(f"<p class='field-label'>Opened</p><p class='field-value'>{local(t['created_at'])} via "
                f"{t['channel']}</p>", unsafe_allow_html=True)
    c2.markdown(f"<p class='field-label'>Target fix by</p><p class='field-value'>{local(t['resolve_due'])}</p>",
                unsafe_allow_html=True)
    if t["resolved_at"]:
        c3.markdown(f"<p class='field-label'>Resolved</p><p class='field-value'>{local(t['resolved_at'])}</p>",
                    unsafe_allow_html=True)
    if t["incident_parent"]:
        st.info(f"Linked to incident {t['incident_parent']}: many people are affected, and the team is working on it.")
    st.markdown(f"**Short description:** {t['summary']}")

    if t["can_confirm"]:  # the same two answers the chat offers: the employee closes it, or sends it back
        st.info("IT says this is fixed. **Is it working for you now?**")
        y, n_, _ = st.columns([1, 1, 1])
        if y.button("Yes, it's fixed", type="primary", width="stretch"):
            client.post(f"/me/tickets/{t['ticket_id']}/confirm")
            st.toast("Thanks! Closed as resolved.")
            st.rerun()
        if n_.button("No, still not working", width="stretch"):
            client.post(f"/me/tickets/{t['ticket_id']}/reopen", {"text": "No, still not working"})
            st.toast("Reopened and sent back to the team.")
            st.rerun()
    b = st.columns(3)
    if t["bot_active"] and t["session_id"] and b[1].button("Continue in chat", width="stretch"):
        ss.chat_sid = t["session_id"]
        st.switch_page("views/employee_chat.py")
    if t["can_cancel"]:
        with b[2].popover("Cancel ticket", width="stretch"):
            why = st.text_input("Why cancel?", key=f"cancel-{t['ticket_id']}")
            if st.button("Cancel this ticket", disabled=not why.strip(), key=f"cbtn-{t['ticket_id']}"):
                client.post(f"/me/tickets/{t['ticket_id']}/cancel", {"text": why})
                st.rerun()

    if t.get("can_rate"):
        with st.container(border=True):
            if t.get("rating"):
                st.markdown(f"You rated this {'⭐' * t['rating']} — thank you.")
            else:
                st.markdown("**How did we do?** Your rating goes to the IT team and helps us improve.")
                score = st.feedback("stars", key=f"stars-{t['ticket_id']}")
                note = st.text_input("Anything we could do better? (optional)", key=f"note-{t['ticket_id']}")
                if score is not None and st.button("Send rating", key=f"rate-{t['ticket_id']}"):
                    client.post(f"/me/tickets/{t['ticket_id']}/rating", {"score": score + 1, "comment": note or None})
                    st.toast("Thanks for rating!")
                    st.rerun()

    st.markdown("#### Conversation")
    avatars = {"user": "🧑‍💻", "bot": "🤖", "agent": "👤", "system": "ℹ️"}
    for m in t["thread"]:
        with st.chat_message("user" if m["role"] == "user" else "assistant", avatar=avatars.get(m["role"])):
            if m["role"] == "agent":
                st.caption(m.get("agent") or "Support engineer")
            if m["text"] != "(sent a screenshot)":
                st.markdown(m["text"])
            for att_id in m.get("attachment_ids") or []:
                data = client.image(att_id)
                if data:
                    st.image(data, width=280)
                else:
                    st.caption("🖼️ Screenshot (removed after the retention period)")
            st.caption(local(m["created_at"]))

    if t["can_reopen"]:
        st.caption("Came back? Describe it here and the ticket reopens (up to 7 days after it was resolved).")
        label = "Describe what's happening again"
    elif t["status"] in ("CLOSED", "RESOLVED", "CANCELLED", "RESOLVED_PENDING_CONFIRMATION"):
        st.caption("This ticket is past its reopen window. Replying opens a new ticket linked to this one.")
        label = "Describe the problem"
    else:
        label = "Reply"
    with st.form(f"reply-{t['ticket_id']}", clear_on_submit=True):
        text = st.text_area(label, placeholder="Never include passwords or codes.")
        if st.form_submit_button("Reopen and send" if t["can_reopen"] else "Send", type="primary") and text.strip():
            try:
                res = client.post(f"/me/tickets/{t['ticket_id']}/reply", {"text": text})
            except ApiError as e:
                st.error(str(e.detail))
            else:
                if res.get("action") == "confirmed":
                    st.toast("Thanks! Marked as fixed.")
                if res.get("action") == "new_ticket":
                    ss.my_open = res["ticket_id"]
                st.rerun()
