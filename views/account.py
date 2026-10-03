"""My account: profile and password change. After a temporary password this is the only page available."""
import json

import streamlit as st

from ui import client
from ui.client import ApiError

user = st.session_state.user
forced = bool(user.get("must_change_password"))

st.markdown("## Set your password" if forced else "## My account")
if forced:
    st.info("You signed in with a temporary password. Choose your own password to continue.")
else:
    c1, c2, c3 = st.columns(3)
    c1.markdown(f"<p class='field-label'>Name</p><p class='field-value'>{user['name']}</p>", unsafe_allow_html=True)
    c2.markdown(f"<p class='field-label'>Employee ID</p><p class='field-value'>{user['user_id']}</p>",
                unsafe_allow_html=True)
    c3.markdown(f"<p class='field-label'>Work email</p><p class='field-value'>{user.get('email') or '—'}</p>",
                unsafe_allow_html=True)
    st.markdown("#### Change password")

with st.form("change_pw", clear_on_submit=True):
    current = st.text_input("Temporary password" if forced else "Current password", type="password")
    new = st.text_input("New password", type="password",
                        help="At least 12 characters. A short sentence is easy to remember and hard to guess. "
                             "Don't include your employee ID or email name.")
    again = st.text_input("New password again", type="password")
    if st.form_submit_button("Save password", type="primary"):
        if new != again:
            st.error("The two new passwords don't match.")
        else:
            try:
                client.post("/auth/change-password", {"current_password": current, "new_password": new})
            except ApiError as e:
                st.error(str(e.detail))
            else:
                st.session_state.user = client.get("/auth/me")
                st.session_state.flash_ok = "Password saved. Your other sessions were signed out."
                st.rerun()
if st.session_state.get("flash_ok"):
    st.success(st.session_state.pop("flash_ok"))
if not forced:
    st.markdown("#### Your data")
    st.caption("Download everything the service desk holds about you: profile, tickets, conversations, ratings "
               "and screenshot records.")
    if st.button("Prepare my data"):
        st.session_state.my_data = client.call("GET", "/me/data")
    if st.session_state.get("my_data"):
        st.download_button("Download (JSON)", json.dumps(st.session_state.my_data, indent=2),
                           f"my-it-data-{user['user_id']}.json", "application/json")
if forced and st.button("Sign out"):
    try:
        client.post("/auth/logout")
    except ApiError:
        pass
    st.session_state.clear()
    st.rerun()
