"""Production features: account administration, password policy, forced password change, sign-in throttle,
readiness, the worker lease and the Postgres SQL translation."""
import uuid

from servicedesk import config
from servicedesk.services.auth import password_problems
from servicedesk.store import _pg_sql

from .scenarios import PASSWORD, SUPERVISOR

GOOD = "correct horse battery staple"


def _new_id() -> str:
    return "EMP9" + uuid.uuid4().hex[:3].upper()


def _create(h, role="employee", **extra) -> tuple[str, str]:
    uid = _new_id()
    r = h.c.post("/admin/users", json={"employee_id": uid, "name": f"Test {uid}", "email": f"{uid.lower()}@corp.example",
                                       "role": role, **extra}, headers=h.login(SUPERVISOR))
    assert r.status_code == 200, r.text
    return uid, r.json()["temporary_password"]


def _login(h, uid, pw):
    return h.c.post("/auth/login", json={"login_id": uid, "password": pw})


def _bearer(r) -> dict:
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_password_policy():
    assert password_problems("short") and password_problems("password1234")
    assert password_problems("my EMP1234 password is long", "EMP1234")
    assert password_problems("aaaaaaaaaaaaaaaa")
    assert not password_problems(GOOD, "EMP1234", "jo.bloggs@corp.example")


def test_temporary_password_forces_change(h):
    uid, temp = _create(h)
    r = _login(h, uid, temp)
    assert r.status_code == 200 and r.json()["user"]["must_change_password"]
    hdr = _bearer(r)
    assert h.c.get("/me/tickets", headers=hdr).json()["detail"] == "password_change_required"
    assert h.c.get("/auth/me", headers=hdr).status_code == 200
    weak = h.c.post("/auth/change-password", json={"current_password": temp, "new_password": "short"}, headers=hdr)
    assert weak.status_code == 422
    ok = h.c.post("/auth/change-password", json={"current_password": temp, "new_password": GOOD}, headers=hdr)
    assert ok.status_code == 200
    assert h.c.get("/me/tickets", headers=hdr).status_code == 200  # this session stays signed in
    assert _login(h, uid, temp).status_code == 401 and _login(h, uid, GOOD).status_code == 200


def test_disable_signs_out_and_blocks_login(h):
    uid, temp = _create(h)
    hdr = _bearer(_login(h, uid, temp))
    assert h.c.post(f"/admin/users/{uid}/disable", headers=h.login(SUPERVISOR)).status_code == 200
    assert h.c.get("/auth/me", headers=hdr).status_code == 401
    r = _login(h, uid, temp)
    assert r.status_code == 401 and r.json()["detail"] == "Invalid ID or password"  # no account disclosure
    h.c.post(f"/admin/users/{uid}/enable", headers=h.login(SUPERVISOR))
    assert _login(h, uid, temp).status_code == 200


def test_admin_reset_revokes_sessions(h):
    uid, temp = _create(h)
    hdr = _bearer(_login(h, uid, temp))
    new_temp = h.c.post(f"/admin/users/{uid}/reset-password", headers=h.login(SUPERVISOR)).json()["temporary_password"]
    assert h.c.get("/auth/me", headers=hdr).status_code == 401
    assert _login(h, uid, temp).status_code == 401
    assert _login(h, uid, new_temp).json()["user"]["must_change_password"]


def test_admin_endpoints_are_supervisor_only(h):
    agent = h.agent_for("Network Remote Access")
    assert h.c.get("/admin/users", headers=h.login(agent)).status_code == 403
    assert h.c.post("/admin/users/EMP1001/reset-password", headers=h.login(h.fresh_employee())).status_code == 403


def test_import_creates_updates_and_reports_errors(h):
    new, existing = _new_id(), _new_id()
    _ = h.c.post("/admin/users", json={"employee_id": existing, "name": "Old Name", "email": f"{existing}@corp.example"},
                 headers=h.login(SUPERVISOR))
    csv_text = ("employee_id,name,email,role,queues,job_title,department,location,asset_tag,manager_id\n"
                f"{new},New Person,{new}@corp.example,agent,Network Operations,Engineer,IT,Pune,LT-1,\n"
                f"{existing},New Name,{existing}@corp.example,employee,,,,,,\n"
                "EMPBAD,No Email,,employee,,,,,,\n"
                f"{_new_id()},Wrong Queue,x@corp.example,agent,Not A Queue,,,,,\n")
    res = h.c.post("/admin/users/import", json={"csv_text": csv_text}, headers=h.login(SUPERVISOR)).json()
    assert [c["employee_id"] for c in res["created"]] == [new] and res["updated"] == 1
    assert len(res["errors"]) == 2
    users = {u["user_id"]: u for u in h.c.get("/admin/users", headers=h.login(SUPERVISOR)).json()}
    assert users[existing]["name"] == "New Name" and users[new]["queues"] == ["Network Operations"]


def test_imported_employee_profile_reaches_tickets(h):
    uid, temp = _create(h, location="Pune", asset_tag="LT-99999", department="Finance")
    hdr = _bearer(_login(h, uid, temp))
    h.c.post("/auth/change-password", json={"current_password": temp, "new_password": GOOD}, headers=hdr)
    sid = h.c.post("/chat/sessions", headers=hdr).json()["session_id"]
    tid = h.c.post("/chat", json={"session_id": sid, "message": "My laptop battery is swelling"},
                   headers=hdr).json()["ticket_id"]
    t = h.ticket(tid)
    assert t["location"] == "Pune" and t["config_item"] == "LT-99999"


def test_sign_in_throttle(h, monkeypatch):
    import api
    monkeypatch.setattr(config, "LOGIN_RATE_PER_MINUTE", 3)
    api._login_hits.clear()
    codes = [h.c.post("/auth/login", json={"login_id": "EMP1001", "password": "x"}).status_code for _ in range(4)]
    api._login_hits.clear()
    assert codes[-1] == 429 and 429 not in codes[:3]


def test_security_headers_and_readiness(h):
    r = h.c.get("/ready")
    assert r.status_code == 200 and r.json()["database"] is True
    assert r.headers["X-Content-Type-Options"] == "nosniff" and r.headers["X-Request-ID"]
    assert "users" not in h.c.get("/health").json()  # public endpoint reveals nothing about accounts


def test_worker_lease_is_exclusive(h):
    import worker
    store = h.api.store
    store.execute("DELETE FROM heartbeats WHERE name='worker'")
    assert worker.take_lease(store, 60)
    real = worker.ME
    worker.ME = "other-host:1"
    try:
        assert not worker.take_lease(store, 60)  # the first worker is still alive
    finally:
        worker.ME = real
    assert worker.take_lease(store, 60)


def test_pg_sql_translation():
    assert _pg_sql("INSERT OR IGNORE INTO w VALUES (?,?)", True) == "INSERT INTO w VALUES (%s,%s) ON CONFLICT DO NOTHING"
    assert _pg_sql("SELECT * FROM e WHERE p LIKE '%x%' AND a=?", True) == "SELECT * FROM e WHERE p LIKE '%%x%%' AND a=%s"
    assert _pg_sql("SELECT * FROM e WHERE p LIKE '%x%'", False) == "SELECT * FROM e WHERE p LIKE '%x%'"


def test_demo_login_still_works(h):
    assert _login(h, "EMP1001", PASSWORD).status_code == 200


def test_audit_log_is_append_only(h):
    import pytest
    store = h.api.store
    store.log_event("Test", {"detail": "append-only check"}, ticket_id="TKT-AUDIT", actor="system")
    with pytest.raises(Exception, match="append-only"):
        store.execute("UPDATE events SET event_type='tampered' WHERE ticket_id='TKT-AUDIT'")
    with pytest.raises(Exception, match="append-only"):
        store.execute("DELETE FROM events WHERE ticket_id='TKT-AUDIT'")
    with store.tx() as c, store.audit_rewrite(c):  # the controlled door still works
        c.execute("UPDATE events SET actor='system-2' WHERE ticket_id='TKT-AUDIT'")
    assert store.one("SELECT actor FROM events WHERE ticket_id='TKT-AUDIT'")["actor"] == "system-2"
    with pytest.raises(Exception, match="append-only"):  # and closes again afterwards
        store.execute("DELETE FROM events WHERE ticket_id='TKT-AUDIT'")


def test_migrations_recorded_and_areas(h):
    store = h.api.store
    assert store.pending_migrations() == []
    assert {r["version"] for r in store.query("SELECT version FROM schema_migrations")} >= {1, 2, 3}
    if store.pg:
        from servicedesk.store import AREA_OF
        rows = store.query("SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema "
                           "NOT IN ('pg_catalog','information_schema') AND table_type='BASE TABLE'")
        where = {r["table_name"]: r["table_schema"] for r in rows}
        assert all(where[t] == area for t, area in AREA_OF.items()), where
        assert "public" not in where.values()
        assert store.one("SELECT COUNT(*) AS n FROM reporting.ticket_facts")["n"] >= 0
