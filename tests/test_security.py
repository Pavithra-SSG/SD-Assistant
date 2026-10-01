"""Spec §14 Phase 2 acceptance + §15 security checklist."""
import pytest

from servicedesk.services.tickets import IllegalTransition

from .scenarios import PASSWORD, SUPERVISOR


def test_api_ignores_body_identity(h):
    emp, other = h.fresh_employee(), h.fresh_employee()
    sid = h.new_session(emp)
    r = h.c.post("/chat", json={"session_id": sid, "message": "My printer won't print, queue stuck",
                                "employee_id": other}, headers=h.login(emp))
    assert r.status_code == 200
    assert h.ticket(r.json()["ticket_id"])["employee_id"] == emp


def test_cannot_use_someone_elses_session(h):
    emp, other = h.fresh_employee(), h.fresh_employee()
    sid = h.new_session(emp)
    r = h.c.post("/chat", json={"session_id": sid, "message": "hello"}, headers=h.login(other))
    assert r.status_code == 404


def test_lockout_after_five_failures(h):
    emp = h.fresh_employee()
    codes = [h.c.post("/auth/login", json={"login_id": emp, "password": "wrong"}).status_code for _ in range(5)]
    assert codes == [401, 401, 401, 401, 423]
    assert h.c.post("/auth/login", json={"login_id": emp, "password": PASSWORD}).status_code == 423
    events = [e["event_type"] for e in h.api.store.query("SELECT event_type FROM events WHERE actor=?", (emp,))]
    assert events.count("login_failed") >= 4 and "locked" in events


def test_recovery_never_creates_a_session(h):
    emp = h.fresh_employee()
    before = h.api.store.one("SELECT COUNT(*) n FROM auth_sessions")["n"]
    r = h.c.post("/auth/recover", json={"login_id": emp, "problem": "password", "has_registered_factor": True})
    assert r.json()["outcome"] == "reset_done" and "token" not in r.json()
    r2 = h.c.post("/auth/recover", json={"login_id": emp, "problem": "mfa", "has_registered_factor": False})
    assert r2.json()["outcome"] == "human_v3" and h.ticket(r2.json()["ticket_id"])["queue"] == "Identity Security"
    assert h.api.store.one("SELECT COUNT(*) n FROM auth_sessions")["n"] == before


def test_work_notes_never_reach_employee(h):
    from .scenarios import _escalated_vpn

    tid = _escalated_vpn(h)
    emp = h.ticket(tid)["employee_id"]
    agent = h.agent_for("Network Remote Access")
    h.c.post(f"/tickets/{tid}/takeover", headers=h.login(agent))
    h.c.post(f"/tickets/{tid}/comment", json={"text": "SECRET-INTERNAL note", "visibility": "internal"},
             headers=h.login(agent))
    h.c.post(f"/tickets/{tid}/comment", json={"text": "Hi, looking into it", "visibility": "customer"},
             headers=h.login(agent))
    view = h.c.get(f"/me/tickets/{tid}", headers=h.login(emp)).json()
    sid = h.ticket(tid)["session_id"]
    chat = h.c.get(f"/chat/sessions/{sid}/messages", headers=h.login(emp)).json()
    blob = str(view) + str(chat)
    assert "SECRET-INTERNAL" not in blob and "Hi, looking into it" in blob
    assert "trace" not in blob and "flags" not in view and "impact" not in view
    notes = h.c.get("/me/notifications", headers=h.login(emp)).json()
    assert any("commented" in n["text"] for n in notes)


def test_illegal_transition_rejected(h):
    emp = h.fresh_employee()
    r = h.chat(emp, h.new_session(emp), "My printer won't print, queue stuck")
    with pytest.raises(IllegalTransition):
        h.api.tickets.transition(r["ticket_id"], "CLOSED", "test", "jump")


def test_agent_sees_own_queue_plus_p1(h):
    agent = h.agent_for("Workplace/Print Support")
    rows = h.c.get("/queue", headers=h.login(agent)).json()
    assert rows and all(r["queue"] == "Workplace/Print Support" or r["priority"] == "P1" or r["owner_id"] == agent
                        for r in rows)


def test_priority_override_supervisor_only(h):
    from .scenarios import _escalated_vpn

    tid = _escalated_vpn(h)
    agent = h.agent_for("Network Remote Access")
    v = h.detail(agent, tid)["ticket"]["version"]
    r = h.c.post(f"/tickets/{tid}/update", json={"version": v, "changes": {"priority": "P1"}, "reason": "x"},
                 headers=h.login(agent))
    assert r.status_code == 403
    r = h.c.post(f"/tickets/{tid}/update", json={"version": v, "changes": {"priority": "P1"},
                                                 "reason": "VIP demo tomorrow"}, headers=h.login(SUPERVISOR))
    assert r.status_code == 200 and r.json()["priority_source"] == "manual"


def test_form_flow_fix_now_and_secret_masking(h):
    emp = h.fresh_employee()
    hdr = h.login(emp)
    review = h.c.post("/forms/review", json={
        "category_id": "CAT-10", "issue_type": "Queue stuck", "short_description": "VPN error 809 from home",
        "impact_choice": "Only me", "workaround": "Partial workaround",
        "exact_error": "error 809 on the vpn client, code is Hunter2024!", "details": {}},
        headers=hdr).json()
    assert review["suggested_category"] == "CAT-02"  # D3: suggest, don't auto-correct
    assert "Hunter2024" not in str(review) and review["warnings"]
    sub = h.c.post("/forms/submit", json={"review_id": review["review_id"], "accept_suggestion": True},
                   headers=hdr).json()
    assert sub["outcome"] == "choose", sub
    t = h.ticket(sub["ticket_id"])
    assert t["channel"] == "Form" and t["category_id"] == "CAT-02" and t["user_category"] == "CAT-10"
    fix = h.c.post(f"/forms/{sub['ticket_id']}/choice", json={"choice": "fix_now"}, headers=hdr).json()
    assert fix["attempt"] == 1  # zero clarifying questions
    assert "Hunter2024" not in str(h.api.store.query("SELECT * FROM tickets WHERE ticket_id=?", (t["ticket_id"],)))


def test_form_safety_overrides_workaround(h):
    emp = h.fresh_employee()
    hdr = h.login(emp)
    review = h.c.post("/forms/review", json={
        "category_id": "CAT-04", "issue_type": "Laptop issue", "short_description": "Laptop battery is swelling",
        "impact_choice": "Only me", "workaround": "Work can continue"}, headers=hdr).json()
    assert review["priority"] == "P1"
    sub = h.c.post("/forms/submit", json={"review_id": review["review_id"]}, headers=hdr).json()
    assert sub["outcome"] == "escalated" and "stop using" in sub["message"].lower()


def test_form_locked_out_never_p4(h):
    emp = h.fresh_employee()
    review = h.c.post("/forms/review", json={
        "category_id": "CAT-01", "issue_type": "Account locked", "short_description": "Can't sign in to my laptop",
        "impact_choice": "Only me", "workaround": "No workaround"}, headers=h.login(emp)).json()
    assert review["priority"] in ("P2", "P3")


def test_no_delete_routes(client):
    methods = {m for r in client.app.routes for m in getattr(r, "methods", set())}
    assert "DELETE" not in methods



def test_staff_sign_in_like_any_employee(h):
    for login_id, uid in (("EMP2010", "EMP2010"), ("emp2010", "EMP2010"), ("ananya.iyer@company.example", "EMP2010"),
                          (SUPERVISOR, SUPERVISOR)):
        r = h.c.post("/auth/login", json={"login_id": login_id, "password": PASSWORD})
        assert r.status_code == 200 and r.json()["user"]["user_id"] == uid, login_id
    me = r.json()["user"]
    assert me["name"] == "Lakshmi Narayanan" and me["job_title"] == "Service Desk Supervisor"
    assert h.c.post("/auth/login", json={"login_id": "ananya", "password": PASSWORD}).status_code == 401


def test_old_staff_ids_are_renamed(h):
    s, auth = h.api.store, h.api.auth
    s.execute("UPDATE users SET user_id='AGT-09' WHERE user_id='EMP2010'")
    s.execute("INSERT INTO watchers (ticket_id,user_id) VALUES ('TKT-OLD','AGT-09')")
    s.log_event("Claimed", {"detail": "old"}, ticket_id="TKT-OLD", actor="AGT-09")
    auth.seed(PASSWORD)
    assert not s.query("SELECT 1 FROM users WHERE user_id='AGT-09'")
    assert s.one("SELECT user_id FROM watchers WHERE ticket_id='TKT-OLD'")["user_id"] == "EMP2010"
    assert s.one("SELECT actor FROM events WHERE ticket_id='TKT-OLD'")["actor"] == "EMP2010"


def test_supervisor_sees_everyone_who_worked_the_ticket(h):
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    tid = h.c.post("/chat", json={"session_id": sid, "message": "VPN keeps disconnecting and I cannot work, "
                                  "please connect me to a person"}, headers=h.login(emp)).json()["ticket_id"]
    agent = h.agent_for("Network Remote Access")
    h.c.post(f"/tickets/{tid}/takeover", headers=h.login(agent))
    h.c.post(f"/tickets/{tid}/comment", json={"text": "Checking the tunnel logs", "visibility": "internal"},
             headers=h.login(agent))
    people = h.detail(SUPERVISOR, tid)["people"]
    assert people[0]["role"] == "requester" and people[0]["user_id"] == emp
    worker = next(p for p in people if p["user_id"] == agent)
    assert worker["current_owner"] and "Added work notes" in worker["did"] and " " in worker["name"]
