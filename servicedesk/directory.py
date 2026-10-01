"""Employee directory: one lookup for a person's profile.

The users table is the source of truth (accounts imported from HR or created by an admin). The demo dataset
(datasets/employees.json) is only a fallback, so development data keeps working. Returns the dataset's
field names, which the bot, tools and ticket code already use.
"""
from __future__ import annotations


def employee_profile(store, k, employee_id: str) -> dict:
    base = dict(k.employees.get(employee_id, {}))
    r = store.one("SELECT * FROM users WHERE user_id=?", (employee_id,))
    if r:
        from_users = {"Employee_ID": r["user_id"], "Full_Name": r["name"], "Email": r["email"],
                      "Department": r.get("department"), "Job_Title": r.get("job_title"),
                      "Location": r.get("location"), "Asset_Tag": r.get("asset_tag"),
                      "Manager_ID": r.get("manager_id"),
                      "Status": "Active" if (r.get("status") or "active") == "active" else "Inactive"}
        base.update({key: v for key, v in from_users.items() if v})
    return base or {"Employee_ID": employee_id, "Status": "Active"}


def employee_exists(store, k, employee_id: str) -> bool:
    return bool(store.one("SELECT 1 AS x FROM users WHERE user_id=? AND role='employee'", (employee_id,))) or \
        employee_id in k.employees
