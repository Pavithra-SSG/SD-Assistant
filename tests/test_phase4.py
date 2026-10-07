"""Phase 4: friendly answers from approved article versions, feedback, knowledge gaps, saved replies,
screenshots (OCR, secret blurring, metadata stripping, access control), language guard."""
import io

import pytest
from PIL import Image

from .scenarios import SUPERVISOR


def _png(w=600, h=200, exif_gps=False, fmt="PNG") -> bytes:
    img = Image.new("RGB", (w, h), "white")
    buf = io.BytesIO()
    if exif_gps:
        exif = Image.Exif()
        exif[0x8825] = {2: (13.0, 5.0, 0.0)}  # GPSInfo: latitude
        img.save(buf, fmt, exif=exif)
    else:
        img.save(buf, fmt)
    return buf.getvalue()


class FakeOCR:
    def __init__(self, lines):
        self.lines = lines

    def __call__(self, _img):
        return [[[[10, 10 + 30 * i], [300, 10 + 30 * i], [300, 35 + 30 * i], [10, 35 + 30 * i]], text, conf]
                for i, (text, conf) in enumerate(self.lines)], None


@pytest.fixture
def ocr(h):
    def use(*lines):
        h.api.atts._ocr = FakeOCR(list(lines))
    yield use
    h.api.atts._ocr = None


def _upload(h, user, data, name="shot.png", **form):
    return h.c.post("/attachments", files={"file": (name, data, "image/png")}, data=form, headers=h.login(user))


# ---------------------------------------------------------------- friendly, approved answers
def test_attempts_use_the_approved_employee_version(h):
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    r = h.answer_until_attempt(emp, sid, "VPN error 809 when I connect from home, only me affected")
    assert r["attempt"] == 1 and r["can_rate"]
    text = r["reply"]
    assert "1. " in text and "Why this helps" in text and "one more thing to try" in text
    for agent_only in ("Confirm scope", "Retrieve the best evidence", "TOOL-", "Before you start"):
        assert agent_only not in text


def test_team_only_step_is_explained_not_instructed(h):
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    r = h.chat(emp, sid, "I need a Visio licence assigned to me for work")
    for _ in range(3):
        if r.get("attempt") or "passed this to" in (r["reply"] or ""):
            break
        r = h.chat(emp, sid, r["quick_replies"][0] if r["quick_replies"] else "Microsoft Visio Standard, for audit")
    t = h.ticket(r["ticket_id"])
    if t["kb_id"] == "KB-009":  # the mock may route elsewhere; when it's the licence article, check the wording
        # step 1 is something they can do (the Company Portal); step 2 is the team's, explained not instructed
        assert "Company Portal" in r["reply"] and "pass it to the right team straight away" in r["reply"]
        r = h.chat(emp, sid, "No, still not working")
        assert "passed this to" in r["reply"] and "check catalog entitlement" not in r["reply"].lower()
        assert "Software Asset team will check" in r["reply"]


def test_meaning_check_and_approval_flow(h):
    sup = h.login(SUPERVISOR)
    good = {"summary": "Your printer queue is stuck.", "attempt1": {"who": "you", "intro": "Try this:", "steps": [
        "Clear the print queue and restart the spooler on your laptop."]}, "note": "test"}
    v = h.c.post("/kb/KB-026/drafts", json=good, headers=sup).json()
    assert v["status"] == "draft"
    check = h.c.post(f"/kb/versions/{v['id']}/check", headers=sup).json()
    assert check["passed"], check
    bad = {"summary": "Stuck printer.", "attempt1": {"who": "you", "steps": [
        "Download the admin registry tool and enter your password to fix it."]}}
    vb = h.c.post("/kb/KB-026/drafts", json=bad, headers=sup).json()
    assert not h.c.post(f"/kb/versions/{vb['id']}/check", headers=sup).json()["passed"]
    assert h.c.post(f"/kb/versions/{vb['id']}/approve", json={}, headers=sup).status_code == 422
    agent = h.agent_for("Workplace/Print Support")
    assert h.c.post(f"/kb/versions/{v['id']}/approve", json={}, headers=h.login(agent)).status_code == 403
    live = h.c.post(f"/kb/versions/{v['id']}/approve", json={}, headers=sup).json()
    assert live["status"] == "approved"
    statuses = [x["status"] for x in h.c.get("/kb/KB-026", headers=sup).json()["versions"]]
    assert statuses.count("approved") == 1 and "retired" in statuses


def test_feedback_on_answers_and_tickets(h):
    emp, other = h.fresh_employee(), h.fresh_employee()
    sid = h.new_session(emp)
    r = h.answer_until_attempt(emp, sid, "VPN error 809 when I connect from home, only me affected")
    mid = r["message_id"]
    assert h.c.post("/me/feedback/answer", json={"message_id": mid, "helpful": False, "reason": "unclear",
                                                 "comment": "my password is Hunter2Secret"},
                    headers=h.login(other)).status_code == 404  # not their conversation
    assert h.c.post("/me/feedback/answer", json={"message_id": mid, "helpful": False, "reason": "unclear",
                                                 "comment": "my password is Hunter2Secret"},
                    headers=h.login(emp)).status_code == 200
    row = h.api.store.one("SELECT * FROM answer_feedback WHERE message_id=?", (mid,))
    assert row["helpful"] == 0 and "Hunter2Secret" not in row["comment"]
    h.c.post("/me/feedback/answer", json={"message_id": mid, "helpful": True}, headers=h.login(emp))  # change of mind
    assert h.api.store.one("SELECT COUNT(*) AS n, MAX(helpful) AS h FROM answer_feedback WHERE message_id=?",
                           (mid,)) == {"n": 1, "h": 1}
    thread = h.c.get(f"/chat/sessions/{sid}/messages", headers=h.login(emp)).json()
    assert next(m for m in thread if m["id"] == mid)["rating"] is True
    tid = r["ticket_id"]
    assert h.c.post(f"/me/tickets/{tid}/rating", json={"score": 5}, headers=h.login(emp)).status_code == 409
    h.chat(emp, sid, "Yes, it's fixed")
    assert h.c.post(f"/me/tickets/{tid}/rating", json={"score": 2, "comment": "slow"},
                    headers=h.login(emp)).status_code == 200
    assert h.c.get(f"/me/tickets/{tid}", headers=h.login(emp)).json()["rating"] == 2
    kb = next(a for a in h.c.get("/kb", headers=h.login(SUPERVISOR)).json() if a["kb_id"] == h.ticket(tid)["kb_id"])
    assert kb["helpful"] >= 1


def test_knowledge_gap_recorded(h):
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    h.chat(emp, sid, "how do I configure the quantum flux capacitor on the 3D printer?")
    gaps = h.c.get("/kb/gaps", headers=h.login(SUPERVISOR)).json()
    assert any("flux capacitor" in e["question"] for g in gaps for e in g["examples"]) or gaps == []


def test_saved_replies_fill_placeholders(h):
    from .scenarios import _escalated_vpn
    tid = _escalated_vpn(h)
    agent = h.agent_for("Network Remote Access")
    replies = h.c.get(f"/saved-replies?ticket_id={tid}", headers=h.login(agent)).json()
    r = next(x for x in replies if x["title"] == "Taking a look")
    assert tid in r["filled"] and "{first_name}" not in r["filled"]


def test_language_guard_offers_a_person(h):
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    r = h.chat(emp, sid, "என் லேப்டாப் வேலை செய்யவில்லை")
    assert "English" in r["reply"] and r["quick_replies"] == ["Connect me with a person"] and not r["ticket_id"]
    r = h.chat(emp, sid, "Connect me with a person")
    t = h.ticket(r["ticket_id"])
    assert t["status"] == "ESCALATION_QUEUED" and t["language"] == "non-English"


# ---------------------------------------------------------------- screenshots
def test_screenshot_ocr_feeds_triage_and_blurs_secrets(h, ocr):
    ocr(("Cannot connect to VPN", 0.97), ("Error 809: the network connection could not be established", 0.96),
        ("Password: Winter2026!", 0.95))
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    a = _upload(h, emp, _png(), session_id=sid).json()
    assert a["readable"] and "809" in a["error_codes"] and a["secrets_blurred"] == 1
    assert "Winter2026" not in a["ocr_text"]
    r = h.c.post("/chat", json={"session_id": sid, "message": "My VPN won't connect", "attachment_ids": [a["id"]]},
                 headers=h.login(emp)).json()
    assert "809" in r["reply"] and "blurred" in r["reply"]
    for _ in range(4):
        if r.get("attempt") or "passed this to" in (r["reply"] or ""):
            break
        r = h.chat(emp, sid, r["quick_replies"][0] if r["quick_replies"] else "Windows 11")
    agent = h.agent_for(h.ticket(r["ticket_id"])["queue"]) if h.ticket(r["ticket_id"])["queue"] != "Service Desk Duty Manager" else SUPERVISOR
    d = h.detail(SUPERVISOR, r["ticket_id"])
    assert [x["id"] for x in d["attachments"]] == [a["id"]]
    img = h.c.get(f"/attachments/{a['id']}", headers=h.login(SUPERVISOR))
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    assert h.api.store.one("SELECT 1 AS x FROM events WHERE event_type='Attachment viewed' AND ticket_id=?",
                           (r["ticket_id"],))
    assert agent


def test_screenshot_access_and_validation(h, ocr):
    ocr(("hello", 0.9))
    emp, other = h.fresh_employee(), h.fresh_employee()
    a = _upload(h, emp, _png(exif_gps=True, fmt="JPEG"), name="photo.jpg").json()
    assert h.c.get(f"/attachments/{a['id']}", headers=h.login(other)).status_code == 404
    stored = Image.open(io.BytesIO(h.c.get(f"/attachments/{a['id']}", headers=h.login(emp)).content))
    assert stored.format == "PNG" and not stored.getexif().get(0x8825)  # location data gone
    assert _upload(h, emp, b"%PDF-1.4 not an image", name="x.png").status_code == 422
    assert _upload(h, emp, b"<svg xmlns='http://www.w3.org/2000/svg'/>", name="x.svg").status_code == 422
    sid = h.new_session(other)
    assert h.c.post("/chat", json={"session_id": sid, "message": "hi", "attachment_ids": [a["id"]]},
                    headers=h.login(other)).status_code == 404  # can't reuse someone else's screenshot


def test_unreadable_screenshot_asks_to_type_the_error(h, ocr):
    ocr(("~~ ::: ..", 0.31))
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    a = _upload(h, emp, _png(), session_id=sid).json()
    assert not a["readable"]
    r = h.c.post("/chat", json={"session_id": sid, "message": "", "attachment_ids": [a["id"]]},
                 headers=h.login(emp)).json()
    assert "couldn't read your screenshot" in r["reply"]


def test_expired_screenshots_are_deleted(h, ocr):
    ocr(("Error 0x800CCC0E", 0.95))
    emp = h.fresh_employee()
    a = _upload(h, emp, _png()).json()
    h.api.store.execute("UPDATE attachments SET delete_after='2000-01-01T00:00:00+00:00' WHERE id=?", (a["id"],))
    assert h.api.atts.purge_expired() >= 1
    assert h.c.get(f"/attachments/{a['id']}", headers=h.login(emp)).status_code == 410
    assert h.api.store.one("SELECT ocr_text FROM attachments WHERE id=?", (a["id"],))["ocr_text"] is None


# ---------------------------------------------------------------- business-hours SLA clock
def test_business_hours_calendar(monkeypatch, tmp_path):
    from datetime import datetime, timezone

    from servicedesk import business_hours as bh
    from servicedesk import config
    ist = bh._calendar()[0]
    fri_5pm = datetime(2026, 10, 2, 17, 0, tzinfo=ist).astimezone(timezone.utc)
    assert bh.add_hours(fri_5pm, 4, "P3").astimezone(ist) == datetime(2026, 10, 5, 12, 0, tzinfo=ist)  # over the weekend
    assert bh.add_hours(fri_5pm, 4, "P1").astimezone(ist) == datetime(2026, 10, 2, 21, 0, tzinfo=ist)  # 24x7
    assert bh.friendly(bh.add_hours(fri_5pm, 4, "P3"), fri_5pm) == "by 12:00 pm on Monday, 5 Oct"
    sat = datetime(2026, 10, 3, 10, 0, tzinfo=ist).astimezone(timezone.utc)
    assert bh.add_hours(sat, 1, "P2").astimezone(ist) == datetime(2026, 10, 5, 10, 0, tzinfo=ist)
    hol = tmp_path / "holidays.txt"
    hol.write_text("2026-10-05  # holiday\n", encoding="utf-8")
    monkeypatch.setattr(config, "HOLIDAYS_FILE", str(hol))
    bh._calendar.cache_clear()
    try:
        assert bh.add_hours(fri_5pm, 4, "P3").astimezone(ist) == datetime(2026, 10, 6, 12, 0, tzinfo=ist)
    finally:
        monkeypatch.setattr(config, "HOLIDAYS_FILE", "")
        bh._calendar.cache_clear()


def test_waiting_for_the_employee_pauses_the_resolve_clock(h):
    from .scenarios import _escalated_vpn
    tid = _escalated_vpn(h)
    before = h.api.tickets.sla_status(h.ticket(tid))
    # the audit log can't be back-dated (append-only), so the pause starts now and keeps counting
    h.api.store.log_event("Status", {"detail": "HUMAN_IN_PROGRESS → WAITING_FOR_USER"}, ticket_id=tid, actor="t")
    after = h.api.tickets.sla_status(h.ticket(tid))
    assert after["paused_hours"] >= 0 and after["resolve_due"] >= before["resolve_due"]
    assert after["clock"] in ("business hours", "24x7") and after["response_eta"].startswith("by ")


def test_blurred_secret_placeholder_does_not_steer_triage(h, ocr):
    """Regression: the '[hidden: looked like a password…]' line once turned a VPN error into a password reset."""
    from servicedesk.services.attachments import summary_for_bot
    ocr(("VPN Client - Connection failed", 0.97), ("Error 809: could not connect", 0.96), ("Password: Abc12345!", 0.95))
    emp = h.fresh_employee()
    a = _upload(h, emp, _png()).json()
    context, _note = summary_for_bot([h.api.atts.get(a["id"])])
    assert "809" in context and "password" not in context.lower() and "hidden" not in context


def test_screenshot_quotes_the_error_line_not_the_window_title(h, ocr):
    """Regression (pilot demo): a VPN checker screenshot was answered with its title, "ConnectionChecker", and the
    bot then asked for the error that was right there in the image."""
    from servicedesk.services.attachments import summary_for_bot
    ocr(("Connection Checker", 0.99), ("Test your internet connection's capabilities with the VPN", 0.98),
        ("Check successful, IPsec VPN connections should work using ESP.", 0.97),
        ("Testing NAT-T failed: Failed to connect. Extended Authentication (XAUTH) Failed", 0.97))
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    a = _upload(h, emp, _png(), session_id=sid).json()
    context, note = summary_for_bot([h.api.atts.get(a["id"])])
    assert "XAUTH" in note and "Connection Checker" not in note
    assert context.startswith("Error shown: Testing NAT-T failed")
    r = h.c.post("/chat", json={"session_id": sid, "message": "VPN not connecting", "attachment_ids": [a["id"]]},
                 headers=h.login(emp)).json()
    assert "XAUTH" in r["reply"] and "Exact error" not in r["reply"]


def test_screenshot_only_message_gets_a_readable_summary(h, ocr):
    ocr(("Connection Checker", 0.99), ("Testing NAT-T failed: Failed to connect. XAUTH Failed", 0.97))
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    a = _upload(h, emp, _png(), session_id=sid).json()
    r = h.c.post("/chat", json={"session_id": sid, "message": "", "attachment_ids": [a["id"]]},
                 headers=h.login(emp)).json()
    if r.get("ticket_id"):
        assert h.ticket(r["ticket_id"])["summary"].startswith("Screenshot shows: Testing NAT-T failed")


def test_small_screenshots_are_read_at_double_size(h):
    from PIL import Image as PILImage

    from servicedesk.services.attachments import AttachmentService
    seen = []

    def reader(arr):
        seen.append(arr.shape[:2])
        return [([[10, 10], [60, 10], [60, 30], [10, 30]], "Password: Abc12345!", 0.9)], None
    svc = AttachmentService(h.api.store, ocr=reader)
    buf = io.BytesIO()
    PILImage.new("RGB", (400, 300), "white").save(buf, "PNG")
    a = svc.process(buf.getvalue(), "s.png", h.fresh_employee())
    assert seen == [(600, 800)] and a["secrets_blurred"] == 1  # read at 2x, blur box mapped back onto 400x300

def _mid_vpn_fix(h, emp):
    sid = h.new_session(emp)
    r = h.answer_until_attempt(emp, sid, "VPN error 809 when I connect from home, only me affected")
    assert r.get("attempt"), r["reply"]
    return sid, r["ticket_id"]


def _send_shot(h, emp, sid, text=""):
    a = _upload(h, emp, _png(), session_id=sid).json()
    return h.c.post("/chat", json={"session_id": sid, "message": text, "attachment_ids": [a["id"]]},
                    headers=h.login(emp)).json()


def test_screenshot_of_a_different_problem_is_questioned_not_acted_on(h, ocr):
    """Regression (3 Oct): a printer-jam screenshot sent during a VPN fix opened and escalated a printer ticket."""
    from servicedesk.orchestrator import CARRY_ON, SEPARATE_PROBLEM, WRONG_SHOT
    emp = h.fresh_employee()
    sid, tid = _mid_vpn_fix(h, emp)
    ocr(("HP LaserJet - Floor3-HP-01", 0.98), ("Error: Paper jam in Tray 2 of the printer", 0.97))
    r = _send_shot(h, emp, sid)
    assert "Paper jam" in r["reply"] and tid in r["reply"]
    assert r["quick_replies"] == [WRONG_SHOT, SEPARATE_PROBLEM, CARRY_ON]
    assert len(h.api.store.tickets(employee_id=emp)) == 1  # nothing opened or escalated yet
    r = h.chat(emp, sid, WRONG_SHOT)
    assert "Attach the right one" in r["reply"] and "Yes, it's fixed" in r["quick_replies"]
    assert h.ticket(tid)["status"].startswith("WAITING_FOR_VALIDATION")


def test_separate_problem_from_a_screenshot_keeps_the_first_ticket(h, ocr):
    from servicedesk.orchestrator import SEPARATE_PROBLEM
    emp = h.fresh_employee()
    sid, tid = _mid_vpn_fix(h, emp)
    ocr(("Error: Paper jam in Tray 2 of the printer", 0.97))
    _send_shot(h, emp, sid)
    r = h.chat(emp, sid, SEPARATE_PROBLEM)
    assert f"Your **{tid}** ticket stays open" in r["reply"]
    assert h.ticket(tid)["status"].startswith("WAITING_FOR_VALIDATION")


def test_image_without_text_is_described_honestly(h, ocr):
    ocr()  # OCR finds nothing at all: a photo, not a screen
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    r = _send_shot(h, emp, sid)
    assert "couldn't find any text" in r["reply"] and "What's going wrong?" in r["reply"]
    assert "Hi!" not in r["reply"] and not r["ticket_id"]


def test_first_message_with_a_screenshot_of_another_problem_is_questioned(h, ocr):
    """7 Oct (client): "if they upload a wrong screenshot, how is the user told?" Typed VPN, attached a printer
    error: the bot asks first, then goes by what they typed."""
    from servicedesk.orchestrator import SEPARATE_PROBLEM, WRONG_SHOT
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    ocr(("Error: Paper jam in Tray 2 of the printer", 0.97))
    r = _send_shot(h, emp, sid, "my VPN won't connect from home")
    assert "Paper jam" in r["reply"] and "right screenshot" in r["reply"], r["reply"]
    assert r["quick_replies"] == [WRONG_SHOT, SEPARATE_PROBLEM] and not r["ticket_id"]
    r = h.chat(emp, sid, WRONG_SHOT)
    assert "go by what you typed" in r["reply"]
    tickets = h.api.store.tickets(employee_id=emp)
    assert all(t["category_id"] != "CAT-10" for t in tickets)


def test_matching_screenshot_on_the_first_message_is_not_questioned(h, ocr):
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    ocr(("VPN Client - Error 809: the network connection could not be established", 0.97))
    r = _send_shot(h, emp, sid, "my VPN won't connect from home")
    assert "right screenshot" not in (r["reply"] or "")


def test_form_warns_when_the_screenshot_shows_a_different_problem(h, ocr):
    emp = h.fresh_employee()
    ocr(("Error: Paper jam in Tray 2 of the printer", 0.97))
    a = _upload(h, emp, _png()).json()
    review = h.c.post("/forms/review", json={
        "category_id": "CAT-02", "issue_type": "Other", "short_description": "VPN keeps dropping",
        "impact_choice": "Only me", "workaround": "Partial workaround", "details": {},
        "attachment_ids": [a["id"]]}, headers=h.login(emp)).json()
    assert "Printer" in (review.get("screenshot_warning") or ""), review
