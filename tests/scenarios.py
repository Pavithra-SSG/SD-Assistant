"""Spec §11 real-case scenarios 1–14, driven through the HTTP API exactly like the Streamlit client.

Used by `pytest` (tests/test_scenarios.py) and by `python evaluate.py --mode scenario`.
Run in MOCK mode: the phrasings below are chosen to trigger the mock brain's keywords, so the
scenarios test the workflow (roles, locks, alerts, state machine), not Jev's accuracy.
Time-based rules (re-alerts, 10-minute supervisor escalation, the reopen window) are tested by
back-dating rows, never by sleeping.
"""
from __future__ import annotations

import os
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone

PASSWORD = os.environ.get("DEMO_PASSWORD", "scenario-pass-123")
SUPERVISOR = "EMP2001"


def _ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")


class Harness:
    """Logs users in, keeps their tokens, and exposes small helpers over the API."""

    def __init__(self, client, api_module):
        self.c, self.api = client, api_module
        self.tokens: dict[str, str] = {}
        self._employees = iter(sorted(api_module.k.employees))
        self.used: set[str] = set()

    # ---- identity
    def login(self, user_id: str) -> dict:
        if user_id not in self.tokens:
            r = self.c.post("/auth/login", json={"login_id": user_id, "password": PASSWORD})
            assert r.status_code == 200, r.text
            self.tokens[user_id] = r.json()["token"]
        return {"Authorization": f"Bearer {self.tokens[user_id]}"}

    def fresh_employee(self, location: str | None = None) -> str:
        for e in sorted(self.api.k.employees):
            if e in self.used:
                continue
            if location and self.api.k.employees[e]["Location"] != location:
                continue
            self.used.add(e)
            return e
        # the 50 demo employees are used up: add a test employee at the requested location (a real account,
        # created the way an admin would, with the shared test password)
        n = len(self.used) + 1
        uid = f"EMP8{n:03d}"
        self.api.auth.create_user(uid, f"Test Employee {n}", f"test.employee{n}@corp.example", "employee",
                                  location=location or "Chennai", password=PASSWORD)
        self.used.add(uid)
        return uid

    def agent_for(self, queue: str, exclude: str | None = None) -> str:
        for u in self.api.auth.users("agent"):
            if queue in u["queues"] and u["user_id"] != exclude:
                return u["user_id"]
        raise RuntimeError(f"no agent for {queue}")

    # ---- employee helpers
    def chat(self, emp: str, sid: str, text: str) -> dict:
        r = self.c.post("/chat", json={"session_id": sid, "message": text}, headers=self.login(emp))
        assert r.status_code == 200, r.text
        return r.json()

    def new_session(self, emp: str) -> str:
        return self.c.post("/chat/sessions", headers=self.login(emp)).json()["session_id"]

    def answer_until_attempt(self, emp: str, sid: str, first: str) -> dict:
        """Send the first message, then answer clarifying questions with the first quick reply (or text)."""
        r = self.chat(emp, sid, first)
        for _ in range(4):
            if r.get("attempt") or "passed this to" in (r["reply"] or ""):
                return r
            r = self.chat(emp, sid, r["quick_replies"][0] if r["quick_replies"] else "Windows 11, error 809")
        return r

    def ticket(self, tid: str) -> dict:
        return self.api.store.get_ticket(tid)

    def detail(self, user: str, tid: str) -> dict:
        r = self.c.get(f"/tickets/{tid}", headers=self.login(user))
        assert r.status_code == 200, r.text
        return r.json()

    def tick_all_required(self, user: str, tid: str) -> None:
        for item in self.detail(user, tid)["checklist"]:
            if item["required"] and not item["done_at"]:
                r = self.c.post(f"/tickets/{tid}/checklist/{item['item_id']}", json={"done": True},
                                headers=self.login(user))
                assert r.status_code == 200, r.text

    def online(self, *users: str) -> None:
        for u in users:
            self.c.get("/auth/me", headers=self.login(u))


# ======================================================================== scenarios
def s01_vpn_fixed_on_attempt_2(h: Harness):
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    r = h.answer_until_attempt(emp, sid, "VPN error 809 when I connect from home, only me affected")
    assert r["attempt"] == 1 and r["can_rate"], r
    tid = r["ticket_id"]
    r = h.chat(emp, sid, "No, still not working")
    assert r["attempt"] == 2, r
    h.chat(emp, sid, "Yes, it's fixed")
    t = h.ticket(tid)
    assert t["status"] == "RESOLVED_PENDING_CONFIRMATION" and t["resolution_code"] == "Solved by bot", t
    success = h.c.get("/analytics/bot-answers", headers=h.login(SUPERVISOR)).json()["success"]
    row = next(s for s in success if s["kb_id"] == t["kb_id"])
    assert row["fixed"] >= 1 and row["sent"] >= 2, row
    return f"{tid} solved by bot on attempt 2 via {t['kb_id']} · C10 success {row['fixed']}/{row['sent']}"


def s02_locked_out_no_factor(h: Harness):
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    h.online(h.agent_for("Identity Security"))
    r = h.chat(emp, sid, "I'm locked out and can't work, forgot my password and I don't have my phone, "
                         "authenticator or backup codes")
    t = h.ticket(r["ticket_id"])
    assert t["queue"] == "Identity Security" and t["verification"] == "V3 required", t
    assert t["priority"] == "P2", t["priority"]
    a = h.api.store.one("SELECT * FROM alerts WHERE ticket_id=? AND event='escalated'", (t["ticket_id"],))
    assert a and a["level"] == "toast", a
    items = {c["item_id"] for c in h.detail(SUPERVISOR, t["ticket_id"])["checklist"] if c["required"]}
    assert {"v3_identity", "v3_reenroll", "verification"} <= items, items
    return f"{t['ticket_id']} → Identity Security, V3, P2 toast, supervised-recovery checklist"


def s03_phishing_p1(h: Harness):
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    r = h.chat(emp, sid, "I clicked a link in an email and entered my password on a fake login page")
    t = h.ticket(r["ticket_id"])
    assert t["priority"] == "P1" and t["queue"] == "Security Operations", t
    a = h.api.store.one("SELECT * FROM alerts WHERE ticket_id=? AND event='escalated'", (t["ticket_id"],))
    assert a["level"] == "modal" and a["escalated_at"], a  # supervisor copy
    sup_popups = [x["ticket_id"] for x in h.c.get("/alerts/pending", headers=h.login(SUPERVISOR)).json()["alerts"]]
    assert t["ticket_id"] in sup_popups
    h.api.store.execute("UPDATE alerts SET next_alert_at=? WHERE alert_id=?", (_ago(seconds=1), a["alert_id"]))
    h.api.alerts.tick(force=True)
    a2 = h.api.store.one("SELECT * FROM alerts WHERE alert_id=?", (a["alert_id"],))
    assert a2["realert_count"] == 1, a2
    return f"{t['ticket_id']} P1 modal → Security Operations + supervisor; re-alerted until acknowledged"


def s04_outage_one_incident(h: Harness):
    loc = Counter(e["Location"] for e in h.api.k.employees.values()).most_common(1)[0][0]
    emps = [h.fresh_employee(loc) for _ in range(6)]
    first = h.chat(emps[0], h.new_session(emps[0]), "The whole 3rd floor has no WiFi, several people affected")
    parent = first["ticket_id"]
    assert h.ticket(parent)["is_incident"] == 1
    linked = []
    for e in emps[1:]:
        sid = h.new_session(e)
        r = h.chat(e, sid, "No WiFi on the 3rd floor since 10am")
        assert "Yes, same problem" in r["quick_replies"], r  # asked, not guessed from location
        r = h.chat(e, sid, "Yes, same problem")
        linked.append(r["ticket_id"])
        assert h.ticket(r["ticket_id"])["incident_parent"] == parent, h.ticket(r["ticket_id"])
    n = h.api.store.one(f"SELECT COUNT(*) n FROM alerts WHERE event='escalated' AND ticket_id IN "
                        f"({','.join('?' * 6)})", (parent, *linked))["n"]
    assert n == 1, n
    return f"incident {parent} + {len(linked)} linked tickets, exactly one alert"


def s05_takeover_mid_attempt(h: Harness):
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    r = h.answer_until_attempt(emp, sid, "VPN error 809 when I connect from home")
    tid = r["ticket_id"]
    assert h.ticket(tid)["status"] == "WAITING_FOR_VALIDATION_1", h.ticket(tid)["status"]
    agent = h.agent_for("Network Remote Access")
    rr = h.c.post(f"/tickets/{tid}/takeover", headers=h.login(agent))
    assert rr.status_code == 200, rr.text
    silent = h.chat(emp, sid, "ok, what should I do now?")
    assert silent["reply"] is None, silent
    thread = h.c.get(f"/chat/sessions/{sid}/messages", headers=h.login(emp)).json()
    assert any(m["role"] == "system" and "joined" in m["text"] for m in thread)
    events = [e["event_type"] for e in h.detail(agent, tid)["events"]]
    assert "Claimed" in events
    return f"{tid}: {agent} took over at WAITING_FOR_VALIDATION_1; bot silent; employee saw the join"


def _escalated_vpn(h: Harness) -> str:
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    r = h.chat(emp, sid, "VPN is broken, I want to talk to a human agent please")
    tid = r["ticket_id"]
    if h.ticket(tid)["status"] != "ESCALATION_QUEUED":  # the bot tried first: ask for a human
        h.answer_until_attempt(emp, sid, "error 809")
        h.chat(emp, sid, "Talk to a human")
    assert h.ticket(tid)["status"] == "ESCALATION_QUEUED", h.ticket(tid)["status"]
    return tid


def s06_double_takeover(h: Harness):
    tid = _escalated_vpn(h)
    a1 = h.agent_for("Network Remote Access")
    a2 = h.agent_for("Network Remote Access", exclude=a1)
    h.login(a1), h.login(a2)
    results = {}

    def go(u):
        results[u] = h.c.post(f"/tickets/{tid}/takeover", headers=h.login(u))

    threads = [threading.Thread(target=go, args=(u,)) for u in (a1, a2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    codes = sorted(r.status_code for r in results.values())
    assert codes == [200, 409], {u: (r.status_code, r.text) for u, r in results.items()}
    loser = next(r for r in results.values() if r.status_code == 409)
    assert "Owned by" in loser.json()["detail"]
    owners = h.api.store.query("SELECT owner FROM tickets WHERE ticket_id=?", (tid,))
    return f"{tid}: one winner ({h.ticket(tid)['owner']}), loser saw '{loser.json()['detail']}' · owners={owners}"


def s07_lost_update(h: Harness):
    tid = _escalated_vpn(h)
    a1 = h.agent_for("Network Remote Access")
    a2 = h.agent_for("Network Remote Access", exclude=a1)
    v = h.detail(a1, tid)["ticket"]["version"]
    r1 = h.c.post(f"/tickets/{tid}/update", json={"version": v, "changes": {"subcategory": "Cannot connect"}},
                  headers=h.login(a1))
    assert r1.status_code == 200, r1.text
    r2 = h.c.post(f"/tickets/{tid}/update", json={"version": v, "changes": {"subcategory": "Certificate"}},
                  headers=h.login(a2))
    assert r2.status_code == 409 and "updated this, reload" in r2.json()["detail"], r2.text
    assert h.ticket(tid)["subcategory"] == "Cannot connect"
    return f"{tid}: second save rejected — '{r2.json()['detail']}'"


def _agent_resolved(h: Harness) -> tuple[str, str, str]:
    tid = _escalated_vpn(h)
    agent = h.agent_for("Network Remote Access")
    assert h.c.post(f"/tickets/{tid}/takeover", headers=h.login(agent)).status_code == 200
    h.tick_all_required(agent, tid)
    r = h.c.post(f"/tickets/{tid}/resolve", json={"resolution_code": "Solved by agent",
                                                  "notes": "Re-issued VPN profile"}, headers=h.login(agent))
    assert r.status_code == 200, r.text
    return tid, agent, h.ticket(tid)["employee_id"]


def s08_reopen_after_3_days(h: Harness):
    tid, agent, emp = _agent_resolved(h)
    h.api.store.execute("UPDATE tickets SET resolved_at=? WHERE ticket_id=?", (_ago(days=3), tid))
    r = h.c.post(f"/me/tickets/{tid}/reply", json={"text": "It broke again"}, headers=h.login(emp))
    assert r.status_code == 200 and r.json()["action"] == "reopened", r.text
    t = h.ticket(tid)
    assert t["reopened_count"] == 1 and t["status"] == "ESCALATION_QUEUED", t
    perf = h.c.get("/analytics/performance", headers=h.login(SUPERVISOR)).json()
    row = next(p for p in perf if p["user_id"] == agent)
    assert row["reopen_rate"] and row["reopen_rate"] > 0, row
    return f"{tid} reopened after 3 days (same ticket); {agent} reopen rate {row['reopen_rate']}"


def s09_p1_unacked_to_supervisor(h: Harness):
    emp = h.fresh_employee()
    r = h.chat(emp, h.new_session(emp), "My laptop battery is swelling and the case is bulging")
    tid = r["ticket_id"]
    a = h.api.store.one("SELECT * FROM alerts WHERE ticket_id=? AND event='escalated'", (tid,))
    assert a["priority"] == "P1"
    h.api.store.execute("UPDATE alerts SET created_at=? WHERE alert_id=?", (_ago(minutes=11), a["alert_id"]))
    h.api.alerts.tick(force=True)
    a2 = h.api.store.one("SELECT * FROM alerts WHERE alert_id=?", (a["alert_id"],))
    assert (a2["escalation_reason"] or "").startswith("unacknowledged"), a2
    ev = [e["payload"]["detail"] for e in h.api.store.events(tid) if e["event_type"] == "Alert"]
    assert any("supervisor" in d and "SLA risk" in d for d in ev), ev
    return f"{tid} P1 unacknowledged 11 min → supervisor, logged as SLA risk"


def s10_correct_printer_to_vpn(h: Harness):
    agent = h.agent_for("Workplace/Print Support")
    emp = h.fresh_employee()
    r = h.c.post("/tickets", json={"caller_employee_id": emp, "category_id": "CAT-10",
                                   "short_description": "Can't connect, error 809 on the VPN client",
                                   "impact": "limited", "urgency": "medium"}, headers=h.login(agent))
    assert r.status_code == 200, r.text
    tid = r.json()["ticket_id"]
    v = h.detail(agent, tid)["ticket"]["version"]
    bad = h.c.post(f"/tickets/{tid}/update", json={"version": v, "changes": {"category_id": "CAT-02"}},
                   headers=h.login(agent))
    assert bad.status_code == 400  # a reason is required
    ok = h.c.post(f"/tickets/{tid}/update", json={"version": v, "changes": {"category_id": "CAT-02"},
                                                  "reason": "Error 809 is a VPN error, not a printer"},
                  headers=h.login(agent))
    assert ok.status_code == 200, ok.text
    corr = [c for c in h.c.get("/corrections", headers=h.login(SUPERVISOR)).json() if c["ticket_id"] == tid]
    assert corr and corr[0]["reason"] and corr[0]["old_value"] == "CAT-10", corr
    c9 = h.c.get("/analytics/dashboard", headers=h.login(SUPERVISOR)).json()["charts"]["C9"]
    assert any(r["category"] == "Printer & Scanning" and r["corrections"] >= 1 for r in c9), c9
    return f"{tid} corrected Printer → VPN with a reason; visible in C9"


def s11_resolve_gate(h: Harness):
    tid = _escalated_vpn(h)
    agent = h.agent_for("Network Remote Access")
    h.c.post(f"/tickets/{tid}/takeover", headers=h.login(agent))
    r = h.c.post(f"/tickets/{tid}/resolve", json={"resolution_code": "Solved by agent", "notes": ""},
                 headers=h.login(agent))
    assert r.status_code == 409 and r.json()["missing"], r.text
    missing = r.json()["missing"]
    h.tick_all_required(agent, tid)
    r2 = h.c.post(f"/tickets/{tid}/resolve", json={"resolution_code": "Solved by agent", "notes": "Fixed"},
                  headers=h.login(agent))
    assert r2.status_code == 200, r2.text
    return f"{tid}: resolve blocked ({len(missing)} missing: {missing[0][:50]}…), then allowed"


def s12_employee_blocked_from_workspace(h: Harness):
    emp = h.fresh_employee()
    r = h.c.get("/queue", headers=h.login(emp))
    assert r.status_code == 403 and r.json()["detail"] == "role_denied", r.text
    ev = h.api.store.one("SELECT * FROM events WHERE event_type='role_denied' AND actor=? ORDER BY id DESC", (emp,))
    assert ev, "no audit event"
    return f"{emp} → /queue: 403 role_denied, audited"


def s13_jev_outage(h: Harness):
    from servicedesk.brain import BrainError

    emp = h.fresh_employee()
    brain = h.api.conv.brain
    original = brain.triage

    def boom(*a, **kw):
        raise BrainError("ConnectionError: simulated outage")

    brain.triage = boom
    try:
        r = h.chat(emp, h.new_session(emp), "Outlook keeps crashing")
    finally:
        brain.triage = original
    t = h.ticket(r["ticket_id"])
    assert t["status"] == "ESCALATION_QUEUED", t
    assert h.c.get("/health").json()["degraded"], "no degraded banner"
    sysalerts = h.api.store.query("SELECT * FROM alerts WHERE ticket_id='SYSTEM'")
    assert sysalerts, "no supervisor system alert"
    c12 = h.c.get("/analytics/dashboard", headers=h.login(SUPERVISOR)).json()["charts"]["C12"]
    assert c12["unauthorized_action_rate"] == 0 and c12["secret_leakage"] == 0, c12
    return f"{t['ticket_id']} routed to a human; supervisor banner + system alert; C12 {c12}"


def s14_duplicate_guard(h: Harness):
    emp = h.fresh_employee()
    s1 = h.new_session(emp)
    first = h.answer_until_attempt(emp, s1, "VPN error 809 when I connect from home")
    s2 = h.new_session(emp)
    r = h.chat(emp, s2, "My VPN still won't connect, error 809")
    assert f"**{first['ticket_id']}**" in r["reply"] and "Yes, same issue" in r["quick_replies"], r
    before = h.api.store.one("SELECT COUNT(*) n FROM tickets WHERE employee_id=?", (emp,))["n"]
    r2 = h.chat(emp, s2, "Yes, same issue")
    after = h.api.store.one("SELECT COUNT(*) n FROM tickets WHERE employee_id=?", (emp,))["n"]
    assert before == after == 1 and first["ticket_id"] in r2["reply"], (before, after, r2)
    return f"asked 'Is this about {first['ticket_id']}?' — no duplicate ticket"


SCENARIOS = [s01_vpn_fixed_on_attempt_2, s02_locked_out_no_factor, s03_phishing_p1, s04_outage_one_incident,
             s05_takeover_mid_attempt, s06_double_takeover, s07_lost_update, s08_reopen_after_3_days,
             s09_p1_unacked_to_supervisor, s10_correct_printer_to_vpn, s11_resolve_gate,
             s12_employee_blocked_from_workspace, s13_jev_outage, s14_duplicate_guard]
