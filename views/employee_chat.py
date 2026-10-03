"""Employee chat on the API (spec §9): screenshots, quick replies, and 👍/👎 on every bot answer."""
import streamlit as st

from ui import client
from ui.client import ApiError
from ui.common import local

user = st.session_state.user
ss = st.session_state
REASONS = {"unclear": "Unclear", "didnt_work": "Didn't work", "wrong_problem": "Wrong problem", "other": "Other"}

sessions = client.get("/chat/sessions")
chat_sessions = [s for s in sessions if not s["session_id"].startswith("form-")]


def fresh_chat() -> str:
    """A clean conversation for a new problem; an unused one is reused rather than piling up empty chats."""
    unused = next((s["session_id"] for s in chat_sessions if not s.get("n_messages")), None)
    return unused or client.post("/chat/sessions")["session_id"]


if ss.get("chat_sid") is None:
    # reopen the last chat only if it's mid-conversation (a question waiting, or a person from IT in it);
    # otherwise start clean, so a new problem never lands in an old ticket's thread
    latest = chat_sessions[0] if chat_sessions else None
    ss.chat_sid = latest["session_id"] if latest and latest.get("in_progress") else fresh_chat()

with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
    st.markdown("## How can IT help?")
    new_chat = st.button("New conversation", width="content")
if new_chat:
    ss.chat_sid = fresh_chat()
    st.rerun()

# past conversations stay one click away; never-used ones are left out of the list
all_sids = [s["session_id"] for s in sessions if s.get("n_messages") or s["session_id"] == ss.chat_sid]
if ss.chat_sid not in all_sids:
    all_sids.insert(0, ss.chat_sid)
def conversation_label(s: str) -> str:
    row = next((x for x in sessions if x["session_id"] == s), None)
    if not row or not row.get("n_messages"):
        return "New conversation"
    kind = "From the ticket form" if s.startswith("form-") else "Chat"
    return f"{kind} · {local(row['updated_at'])}" + (f" · {row['ticket_id']}" if row.get("ticket_id") else "")


if len(all_sids) > 1:
    ss.chat_sid = st.selectbox("Conversation", all_sids, index=all_sids.index(ss.chat_sid),
                               format_func=conversation_label, label_visibility="collapsed")
sid = ss.chat_sid


def send(text: str, files=None, new_chat: bool = False) -> None:
    """Send to this conversation, or to a fresh one when this one is closed ('thanks') or the employee chose a
    separate ticket for a different problem: one problem, one ticket, one conversation."""
    target = sid
    last = ss.get("last_bot") or {}
    if new_chat or last.get("ended"):
        target = ss.chat_sid = fresh_chat()
    ids = []
    for f in files or []:
        with st.spinner(f"Reading your screenshot {f.name}…"):
            try:
                ids.append(client.upload(f, session_id=target)["id"])
            except ApiError as e:
                st.error(str(e.detail))
                return
    with st.spinner("Checking…"):
        try:
            client.post("/chat", {"session_id": target, "message": text or "", "attachment_ids": ids})
        except ApiError as e:
            st.error(str(e.detail))
            return
    st.rerun()


def rate(m: dict) -> None:
    """👍 / 👎 under a bot answer; after 👎, one tap to say why."""
    why_key = f"why-{m['id']}"
    if m["rating"] is None:
        val = st.feedback("thumbs", key=f"fb-{m['id']}")
        if val is not None:
            client.post("/me/feedback/answer", {"message_id": m["id"], "helpful": bool(val)})
            ss[why_key] = not val
            st.rerun()
    elif m["rating"] is False and ss.get(why_key):
        pick = st.pills("What went wrong?", list(REASONS.values()), key=f"reason-{m['id']}")
        if pick:
            reason = next(k for k, v in REASONS.items() if v == pick)
            client.post("/me/feedback/answer", {"message_id": m["id"], "helpful": False, "reason": reason})
            ss[why_key] = False
            st.toast("Thanks, that helps us improve this answer.")
            st.rerun()
    else:
        st.caption("👍 Thanks for the feedback" if m["rating"] else "👎 Thanks, we'll use this to improve")


@st.fragment(run_every="8s")
def thread() -> None:
    """Refreshes on its own so agent replies and 'joined' notices appear without a click."""
    msgs = client.get(f"/chat/sessions/{sid}/messages")
    avatars = {"user": "🧑‍💻", "bot": "🤖", "agent": "👤", "system": "ℹ️"}
    for m in msgs:
        with st.chat_message("user" if m["role"] == "user" else "assistant", avatar=avatars.get(m["role"])):
            if m["role"] == "agent":
                st.caption(f"{m.get('agent') or 'Support engineer'} · IT support")
            if m["text"] and m["text"] != "(sent a screenshot)":
                st.markdown(m["text"])
            for att_id in m.get("attachment_ids") or []:
                data = client.image(att_id)
                if data:
                    st.image(data, width=320, caption="Your screenshot (passwords or codes are blurred)")
                else:
                    st.caption("🖼️ Screenshot (removed after the retention period)")
            if m["role"] == "bot" and m.get("can_rate"):
                rate(m)
    ss.last_bot = msgs[-1] if msgs and msgs[-1]["role"] == "bot" else None
    ss.empty_chat = not msgs


thread()

if ss.get("empty_chat"):
    st.caption("Describe the problem in your own words, or start with one of these. You can attach a screenshot "
               "of the error with the 📎 button.")
    starters = ["My VPN won't connect", "I forgot my password", "Outlook isn't sending email",
                "I think I got a phishing email"]
    # buttons size to their text and wrap to a new line, so a label is never cut off
    with st.container(horizontal=True, gap="small"):
        for s in starters:
            if st.button(s, width="content"):
                send(s)
elif ss.get("last_bot") and ss.last_bot.get("ended"):
    st.caption("✅ This conversation is closed. Type below to start a new one; it'll get its own ticket if needed.")
elif ss.get("last_bot") and ss.last_bot.get("quick_replies"):
    qr = ss.last_bot["quick_replies"]
    carry = ss.last_bot.get("new_problem")  # "open a separate ticket?": yes moves the problem to a new chat
    with st.container(horizontal=True, gap="small"):
        for i, q in enumerate(qr):
            if st.button(q, key=f"qr-{ss.last_bot['id']}-{i}", width="content"):
                if carry and i == 0:
                    send(carry, new_chat=True)
                else:
                    send(q)

entry = st.chat_input("Describe your issue, or attach a screenshot. Never share passwords or codes.",
                      accept_file="multiple", file_type=["png", "jpg", "jpeg", "webp"])
if entry:
    send(entry.text, entry.files)
