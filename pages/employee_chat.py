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
if ss.get("chat_sid") is None:
    ss.chat_sid = (chat_sessions[0]["session_id"] if chat_sessions else client.post("/chat/sessions")["session_id"])

head, new = st.columns([5, 1])
head.markdown("## How can IT help?")
if new.button("New conversation", width="stretch"):
    ss.chat_sid = client.post("/chat/sessions")["session_id"]
    st.rerun()

all_sids = [s["session_id"] for s in sessions]
if ss.chat_sid not in all_sids:
    all_sids.insert(0, ss.chat_sid)
if len(all_sids) > 1:
    ss.chat_sid = st.selectbox("Conversation", all_sids, index=all_sids.index(ss.chat_sid),
                               format_func=lambda s: ("From the ticket form · " if s.startswith("form-") else "Chat · ")
                               + next((local(x["updated_at"]) for x in sessions if x["session_id"] == s), "new"),
                               label_visibility="collapsed")
sid = ss.chat_sid


def send(text: str, files=None) -> None:
    ids = []
    for f in files or []:
        with st.spinner(f"Reading your screenshot {f.name}…"):
            try:
                ids.append(client.upload(f, session_id=sid)["id"])
            except ApiError as e:
                st.error(str(e.detail))
                return
    with st.spinner("Checking…"):
        try:
            client.post("/chat", {"session_id": sid, "message": text or "", "attachment_ids": ids})
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
    for c, s in zip(st.columns(len(starters)), starters):
        if c.button(s, width="stretch"):
            send(s)
elif ss.get("last_bot") and ss.last_bot.get("quick_replies"):
    qr = ss.last_bot["quick_replies"]
    for i, (c, q) in enumerate(zip(st.columns(len(qr)), qr)):
        if c.button(q, key=f"qr-{ss.last_bot['id']}-{i}", width="stretch"):
            send(q)

entry = st.chat_input("Describe your issue, or attach a screenshot. Never share passwords or codes.",
                      accept_file="multiple", file_type=["png", "jpg", "jpeg", "webp"])
if entry:
    send(entry.text, entry.files)
