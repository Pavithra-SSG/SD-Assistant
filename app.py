"""Streamlit client: login gate, then pages by role. It never imports the engine (D8).

Run the API first:   uvicorn api:app
Then:                streamlit run app.py
"""
import streamlit as st

from servicedesk import config
from ui import client
from ui.client import ApiError
from ui.common import alert_center, inject_css, notifications, sidebar_badges

st.set_page_config(page_title="IT Service Desk", page_icon="🛠️", layout="wide")
inject_css()


def _sign_in(login_id: str, pw: str) -> None:
    try:
        res = client.post("/auth/login", {"login_id": login_id, "password": pw})
    except ApiError as e:
        st.error(str(e.detail))
    else:
        st.session_state.token, st.session_state.user = res["token"], res["user"]
        st.rerun()


def login_page() -> None:
    left, mid, right = st.columns([1, 1.25, 1])
    with mid:
        st.markdown("## IT Service Desk")
        if st.session_state.get("flash"):
            st.info(st.session_state.pop("flash"))
        st.caption("Sign in with your employee ID or work email. IT staff use their employee ID too.")
        with st.form("login"):
            login_id = st.text_input("Employee ID or work email", placeholder="EMP1001")
            pw = st.text_input("Password", type="password")
            if st.form_submit_button("Sign in", type="primary", width="stretch"):
                _sign_in(login_id, pw)
        with st.expander("How we use your information"):
            st.caption("The service desk keeps what you tell it, and your screenshots, to fix your IT problems. "
                       "Screenshots are read on company servers, anything that looks like a password is blurred, "
                       f"and images are deleted after {config.ATTACHMENT_RETENTION_DAYS} days. Conversations are "
                       f"cleared {config.RETENTION_MONTHS} months after a ticket closes. You can download "
                       "everything held about you from **My account**.")
        with st.expander("Can't sign in?"):
            st.caption("For a forgotten password or a lost MFA method. You'll approve a prompt on your registered "
                       "authenticator. This never signs you in.")
            rid = st.text_input("Your ID or work email", key="rec_id")
            problem = st.radio("What's wrong?", ["password", "mfa"], horizontal=True,
                               format_func=lambda p: "Forgot my password" if p == "password" else "Lost my MFA method")
            factor = st.radio("Do you still have a registered sign-in method (authenticator app, security key or "
                              "backup codes)?", ["Yes", "No"], horizontal=True)
            if st.button("Start recovery", width="stretch") and rid:
                try:
                    res = client.post("/auth/recover", {"login_id": rid, "problem": problem,
                                                        "has_registered_factor": factor == "Yes"})
                    (st.success if res["outcome"] == "reset_done" else st.info)(res["message"])
                except ApiError as e:
                    st.error(str(e.detail))
        if config.DEMO_PASSWORD:  # never shown in production
            st.caption("Demo accounts: employees EMP1001–EMP1050 · IT supervisor EMP2001 · IT agents "
                       "EMP2002–EMP2015 (see README). The password is DEMO_PASSWORD in .env.")


def _no_sidebar() -> None:
    """Signed-out screens have no menu. Streamlit keeps a sidebar that was open before sign-out, empty; hide it."""
    st.markdown("<style>[data-testid='stSidebar'], [data-testid='stSidebarCollapsedControl'], "
                "[data-testid='stExpandSidebarButton'] {display: none;}</style>", unsafe_allow_html=True)


if "token" not in st.session_state:
    _no_sidebar()
    st.navigation([st.Page(login_page, title="Sign in", icon="🔐")], position="hidden").run()
    st.stop()

user = st.session_state.user
role = user["role"]

if user.get("must_change_password"):  # after a temporary password nothing else works until it is changed
    _no_sidebar()
    st.navigation([st.Page("pages/account.py", title="Set your password", icon="🔑")], position="hidden").run()
    st.stop()

if role == "employee":
    pages = {"Get help": [st.Page("pages/employee_chat.py", title="Chat", icon="💬", default=True),
                          st.Page("pages/employee_form.py", title="Ticket form", icon="📝"),
                          st.Page("pages/my_tickets.py", title="My tickets", icon="🎫")]}
else:
    work = [st.Page("pages/queue.py", title="My queue", icon="📥", default=True),
            st.Page("pages/ticket.py", title="Ticket", icon="🎫"),
            st.Page("pages/bot_answers.py", title="Bot answers log", icon="🤖"),
            st.Page("pages/knowledge.py", title="Knowledge", icon="📚")]
    insight = [st.Page("pages/charts.py", title="Charts", icon="📊"),
               st.Page("pages/performance.py", title="Agent performance", icon="🏁"),
               st.Page("pages/shift.py", title="Shift", icon="🕘")]
    if role == "supervisor":
        insight.append(st.Page("pages/corrections.py", title="Corrections log", icon="✏️"))
    pages = {"Work": work, "Insight": insight}
account = [st.Page("pages/account.py", title="My account", icon="🔑")]
if role == "supervisor":
    account.append(st.Page("pages/users.py", title="User accounts", icon="👥"))
pages["Account"] = account

nav = st.navigation(pages)
if role != "employee":
    alert_center()  # before the sidebar, so the badges below show this poll's counts

with st.sidebar:
    st.markdown(f"**{user['name']}**  \n<span class='muted'>{user['user_id']} · "
                f"{user.get('job_title') or role.title()}</span>",
                unsafe_allow_html=True)
    if role != "employee":
        st.caption("Queues: " + ", ".join(user["queues"]) if role == "agent" else "All queues")
        sidebar_badges()
    if st.button("Sign out", width="stretch"):
        try:
            client.post("/auth/logout")
        except ApiError:
            pass
        st.session_state.clear()
        st.rerun()

notifications()
nav.run()
