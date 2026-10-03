"""User accounts (supervisor): find a person, reset a password, unlock, disable a leaver, change role or
queues, create accounts one by one or import them from an HR export. Nothing is deleted; all audited."""
import pandas as pd
import streamlit as st

from ui import client
from ui.client import ApiError

me = st.session_state.user
meta = client.meta()
st.markdown("## User accounts")

users = client.get("/admin/users")
c1, c2, c3 = st.columns([2, 1, 1])
q = c1.text_input("Find", placeholder="Name, employee ID or email", label_visibility="collapsed")
role_f = c2.selectbox("Role", ["All roles", "employee", "agent", "supervisor"], label_visibility="collapsed")
show_disabled = c3.toggle("Show disabled")
rows = [u for u in users
        if (not q or q.lower() in f"{u['name']} {u['user_id']} {u['email']}".lower())
        and (role_f == "All roles" or u["role"] == role_f) and (show_disabled or u["status"] == "active")]


def _status(u: dict) -> str:
    return ", ".join(s for s, on in (("Disabled", u["status"] != "active"), ("Locked", u["locked"]),
                                      ("Must set password", u["must_change_password"])) if on) or "Active"


st.caption(f"{len(rows)} of {len(users)} accounts")
st.dataframe(pd.DataFrame([{"Employee ID": u["user_id"], "Name": u["name"], "Role": u["role"].title(),
                            "Job title": u.get("job_title") or "", "Queues": ", ".join(u["queues"])
                            if u["role"] == "agent" else ("All" if u["role"] == "supervisor" else ""),
                            "Email": u["email"] or "", "Status": _status(u)} for u in rows]),
             hide_index=True, width="stretch", height=min(420, 38 + 35 * max(1, len(rows))))


def _show_temp(uid: str, temp: str) -> None:
    st.session_state.temp_pw = (uid, temp)


if st.session_state.get("temp_pw"):
    uid, temp = st.session_state.temp_pw
    st.warning(f"Temporary password for **{uid}** — shown once. Give it to the person through a verified "
               "channel (for example read it out on a call-back to the number on file). They must change it "
               "when they sign in.")
    st.code(temp, language=None)
    if st.button("I've handed it over"):
        del st.session_state.temp_pw
        st.rerun()

tab_manage, tab_new, tab_import = st.tabs(["Manage an account", "New account", "Import from HR (CSV)"])

with tab_manage:
    ids = [u["user_id"] for u in rows]
    pick = st.selectbox("Account", ids, index=None, placeholder="Choose from the filtered list",
                        format_func=lambda i: next(f"{u['name']} ({i})" for u in users if u["user_id"] == i))
    if pick:
        u = next(x for x in users if x["user_id"] == pick)
        st.markdown(f"**{u['name']}** · {u['role'].title()} · {u.get('job_title') or '—'} · {_status(u)}")
        b1, b2, b3 = st.columns(3)
        if b1.button("Reset password", help="Only after verifying who is asking. Signs them out everywhere."):
            try:
                _show_temp(pick, client.post(f"/admin/users/{pick}/reset-password")["temporary_password"])
                st.rerun()
            except ApiError as e:
                st.error(str(e.detail))
        if u["locked"] and b2.button("Unlock"):
            client.post(f"/admin/users/{pick}/unlock")
            st.rerun()
        if pick != me["user_id"]:
            label = "Disable (leaver)" if u["status"] == "active" else "Re-enable"
            if b3.button(label):
                client.post(f"/admin/users/{pick}/{'disable' if u['status'] == 'active' else 'enable'}")
                st.rerun()
        with st.form(f"edit-{pick}"):
            st.markdown("Role and queues")
            role = st.selectbox("Role", ["employee", "agent", "supervisor"],
                                index=["employee", "agent", "supervisor"].index(u["role"]))
            queues = st.multiselect("Queues (agents)", meta["queues"],
                                    default=[x for x in u["queues"] if x in meta["queues"]] if u["role"] == "agent"
                                    else [])
            title = st.text_input("Job title", u.get("job_title") or "")
            if st.form_submit_button("Save changes"):
                try:
                    client.patch(f"/admin/users/{pick}", {"role": role, "queues": queues, "job_title": title or None})
                    st.success("Saved. If the role or queues changed, they need to sign in again.")
                except ApiError as e:
                    st.error(str(e.detail))

with tab_new, st.form("new-user", clear_on_submit=True):
    a, b = st.columns(2)
    emp_id = a.text_input("Employee ID", placeholder="EMP1051")
    name = b.text_input("Full name")
    email = a.text_input("Work email")
    role = b.selectbox("Role", ["employee", "agent", "supervisor"])
    queues = st.multiselect("Queues (agents only)", meta["queues"])
    title = a.text_input("Job title")
    dept = b.text_input("Department")
    loc = a.text_input("Location")
    asset = b.text_input("Device asset tag")
    if st.form_submit_button("Create account", type="primary"):
        try:
            res = client.post("/admin/users", {"employee_id": emp_id, "name": name, "email": email, "role": role,
                                               "queues": queues, "job_title": title or None,
                                               "department": dept or None, "location": loc or None,
                                               "asset_tag": asset or None})
            _show_temp(res["user_id"], res["temporary_password"])
            st.rerun()
        except ApiError as e:
            st.error(str(e.detail))

with tab_import:
    st.caption("Columns: employee_id, name, email, role, queues, job_title, department, location, asset_tag, "
               "manager_id. Separate several queues with ; . Existing IDs are updated, never duplicated.")
    up = st.file_uploader("HR export (.csv)", type=["csv"])
    if up and st.button("Import", type="primary"):
        try:
            res = client.post("/admin/users/import", {"csv_text": up.getvalue().decode("utf-8-sig")})
        except ApiError as e:
            st.error(str(e.detail))
        else:
            st.success(f"Created {len(res['created'])} · updated {res['updated']}")
            for err in res["errors"]:
                st.error(err)
            if res["created"]:
                st.warning("Temporary passwords are in this file, shown once. Distribute securely, then delete it.")
                st.download_button("Download temporary passwords",
                                   pd.DataFrame(res["created"]).to_csv(index=False).encode(),
                                   "temporary-passwords.csv", "text/csv")
