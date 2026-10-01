"""Go-live: notifications outside the app (outbox), retention and data export, access logging, watchdog."""
from servicedesk import config

from .scenarios import SUPERVISOR


def test_email_and_teams_go_through_the_outbox(h, monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.test")
    monkeypatch.setattr(config, "TEAMS_WEBHOOK_URL", "https://teams.test/hook")
    store, notify = h.api.store, h.api.notify
    emp = h.fresh_employee()
    store.execute("DELETE FROM outbox")
    notify.notify(emp, "TKT-9999", "Your ticket was resolved. Please confirm.")
    row = store.one("SELECT * FROM outbox WHERE channel='email'")
    assert row and row["status"] == "pending" and "[TKT-9999]" in row["subject"] and config.SITE_URL in row["body"]
    notify.team_alert("P1 test", "TKT-9999", "k1")
    notify.team_alert("P1 test", "TKT-9999", "k1")  # same key: no duplicate
    assert store.one("SELECT COUNT(*) AS n FROM outbox WHERE channel='teams'")["n"] == 1
    calls = []

    def flaky(r):
        calls.append(r["channel"])
        if r["channel"] == "teams":
            raise ConnectionError("webhook down")
    res = notify.send_pending(sender=flaky)
    assert res == {"sent": 1, "failed": 1}
    teams = store.one("SELECT * FROM outbox WHERE channel='teams'")
    assert teams["status"] == "pending" and teams["attempts"] == 1 and "webhook down" in teams["last_error"]
    for _ in range(5):
        notify.send_pending(sender=flaky)
    assert store.one("SELECT status FROM outbox WHERE channel='teams'")["status"] == "failed"  # gave up after 5


def test_no_email_without_smtp_or_for_staff(h, monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "")
    store = h.api.store
    store.execute("DELETE FROM outbox")
    h.api.notify.notify(h.fresh_employee(), None, "x")
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.test")
    h.api.notify.notify(SUPERVISOR, None, "staff get the app + Teams, not e-mail")
    assert store.one("SELECT COUNT(*) AS n FROM outbox")["n"] == 0


def test_p1_alert_reaches_the_team_channel(h, monkeypatch):
    monkeypatch.setattr(config, "TEAMS_WEBHOOK_URL", "https://teams.test/hook")
    store = h.api.store
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    r = h.chat(emp, sid, "My laptop battery is swelling and the case is bulging")
    row = store.one("SELECT * FROM outbox WHERE channel='teams' AND ticket_id=?", (r["ticket_id"],))
    assert row and "P1" in row["body"] and "swelling" not in row["body"]  # no employee words in Teams


def test_retention_removes_personal_text_but_keeps_facts(h):
    from .scenarios import _escalated_vpn
    store = h.api.store
    tid = _escalated_vpn(h)
    t = h.ticket(tid)
    store.execute("UPDATE tickets SET status='CLOSED', updated_at='2020-01-01T00:00:00+00:00' WHERE ticket_id=?", (tid,))
    res = h.api.privacy.run_retention()
    assert res["tickets"] >= 1
    after = h.ticket(tid)
    assert after["summary"].startswith("[removed") and after["redacted_at"] and after["handoff_json"] is None
    assert after["category_id"] == t["category_id"] and after["priority"] == t["priority"]  # facts kept
    texts = {m["text"] for m in store.messages(ticket_id=tid)}
    assert texts == {"[removed after the retention period]"}
    assert store.one("SELECT 1 AS x FROM events WHERE event_type='Retention'")


def test_my_data_export_and_staff_views_logged(h):
    from .scenarios import _escalated_vpn
    tid = _escalated_vpn(h)
    emp = h.ticket(tid)["employee_id"]
    r = h.c.get("/me/data", headers=h.login(emp))
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    data = r.json()
    assert data["profile"]["user_id"] == emp and any(t["ticket_id"] == tid for t in data["tickets"])
    assert all(m.get("visibility", "customer") == "customer" for m in data["messages"])
    h.detail(SUPERVISOR, tid)
    h.detail(SUPERVISOR, tid)  # a second look within the hour isn't logged again
    n = h.api.store.one("SELECT COUNT(*) AS n FROM events WHERE event_type='Ticket viewed' AND ticket_id=? AND actor=?",
                        (tid, SUPERVISOR))["n"]
    assert n == 1


def test_watchdog_sends_one_down_and_one_recovered():
    import healthwatch
    w = healthwatch.Watch(failures_needed=2)
    assert w.observe(["worker stopped"]) is None           # one blip: no alert
    down = w.observe(["worker stopped"])
    assert down and "DOWN" in down[0] and "worker stopped" in down[1]
    assert w.observe(["worker stopped"]) is None           # still down: no repeat
    up = w.observe([])
    assert up and "RECOVERED" in up[0]
    assert w.observe([]) is None
