"""Conversation quality found in the first hands-on test (3 Oct 2026): exact dates instead of "Monday",
security handled 24x7, answer buttons for error details, no questions about what we already know, and
replies that read in a clear order."""
from datetime import datetime, timezone

from servicedesk import business_hours as bh
from servicedesk.orchestrator import TYPE_OWN, field_question, profile_answer


def test_times_always_name_the_date():
    ist = bh._calendar()[0]
    sat = datetime(2026, 10, 3, 10, 30, tzinfo=ist).astimezone(timezone.utc)
    mon = datetime(2026, 10, 5, 10, 0, tzinfo=ist).astimezone(timezone.utc)
    assert bh.friendly(mon, sat) == "by 10:00 am on Monday, 5 Oct"
    assert bh.friendly(sat.replace(hour=sat.hour + 2), sat).endswith("today (Sat 3 Oct)")
    assert "Monday to Friday" in bh.working_hours_text()


def test_security_incidents_run_around_the_clock():
    ist = bh._calendar()[0]
    sat = datetime(2026, 10, 3, 10, 30, tzinfo=ist).astimezone(timezone.utc)
    # a P2 security report on Saturday is due on Saturday, not "10:00 am on Monday"
    assert bh.add_hours(sat, 1, "P2", "CAT-09").astimezone(ist) == datetime(2026, 10, 3, 11, 30, tzinfo=ist)
    assert bh.add_hours(sat, 1, "P2", "CAT-06").astimezone(ist).weekday() == 0  # email waits for Monday


def test_locked_out_of_sign_in_or_mfa_never_waits_for_monday():
    """Regression (3 Oct): a P3 password reset escalated on Saturday said 'first reply by 1:00 pm on Monday'."""
    ist = bh._calendar()[0]
    sat_3pm = datetime(2026, 10, 3, 15, 0, tzinfo=ist).astimezone(timezone.utc)
    for cat in ("CAT-01", "CAT-05"):
        assert bh.add_hours(sat_3pm, 4, "P3", cat).astimezone(ist) == datetime(2026, 10, 3, 19, 0, tzinfo=ist)


def test_error_details_offer_buttons_and_a_way_to_type():
    outlook = {"Category_ID": "CAT-06", "Field_Name": "error_or_symptom", "UI_Control": "textarea",
               "Options_or_Source": "Describe what happens", "Help_Text": "Exact behavior or error"}
    ask, opts = field_question(outlook)
    assert ask == "What exactly is going wrong?" and "Emails stuck in the Outbox" in opts and opts[-1] == TYPE_OWN
    vpn = {"Category_ID": "CAT-02", "Field_Name": "os_version", "UI_Control": "dropdown",
           "Options_or_Source": "Windows 11|Windows 10|Other", "Help_Text": "Your operating system"}
    assert field_question(vpn) == ("Which operating system is your laptop on?", ["Windows 11", "Windows 10", "Other"])


def test_known_details_are_not_asked():
    me = {"Employee_ID": "EMP1001", "Email": "user1@company.example", "Asset_Tag": "LT-70001"}
    assert profile_answer({"Category_ID": "CAT-01", "Field_Name": "employee_id"}, me) == "EMP1001"
    assert profile_answer({"Category_ID": "CAT-01", "Field_Name": "account_username"}, me) == "user1"
    assert profile_answer({"Category_ID": "CAT-04", "Field_Name": "asset_tag"}, me) == "LT-70001"
    assert profile_answer({"Category_ID": "CAT-06", "Field_Name": "client"}, me) is None


def test_type_own_keeps_the_question_open(h):
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    r = h.chat(emp, sid, "Outlook isn't sending email")
    for _ in range(4):
        if TYPE_OWN in r["quick_replies"] or r.get("attempt") or "passed this to" in (r["reply"] or ""):
            break
        r = h.chat(emp, sid, r["quick_replies"][0] if r["quick_replies"] else "Outlook Desktop")
    if TYPE_OWN not in r["quick_replies"]:
        return  # the model judged the detail already given; nothing to check on this path
    r = h.chat(emp, sid, TYPE_OWN)
    assert "type it" in r["reply"] and not r["quick_replies"]
    r = h.chat(emp, sid, "It says 0x800CCC0E when I press send")
    assert "type it" not in r["reply"]


def test_handoff_gives_reference_priority_and_exact_time(h):
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    r = h.chat(emp, sid, "I clicked a link and typed my password on a fake login page")
    text = r["reply"]
    assert "Until Security gets back to you" in text and "- **Ticket:**" in text
    assert "First reply expected" in text and "around the clock" in text


def test_after_handoff_old_buttons_and_when_questions_answer_about_this_ticket(h):
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    tid = h.chat(emp, sid, "I clicked a link and typed my password on a fake login page")["ticket_id"]
    for msg in ("No, still not working", "which monday?", "give exact date"):
        r = h.chat(emp, sid, msg)
        assert tid in r["reply"] and "first reply" in r["reply"].lower() and "Before I open a new ticket" not in r["reply"]
    assert len(h.api.store.tickets(employee_id=emp)) == 1  # no second ticket opened


def test_status_answers_when_with_a_date(h):
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    h.chat(emp, sid, "I clicked a link and typed my password on a fake login page")
    r = h.chat(emp, h.new_session(emp), "what is the status of my ticket")  # a new chat: the list view
    assert "Here's where your tickets stand" in r["reply"] and "First reply expected" in r["reply"]
    assert any(m in r["reply"] for m in (" Oct", " Nov", " Dec", " Jan"))


def test_phishing_report_asks_before_handing_off(h):
    """Regression (3 Oct): "I think I got a phishing email" went straight to Security with nothing to check."""
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    r = h.chat(emp, sid, "I think I got a phishing email")
    tid = r["ticket_id"]
    assert "Until Security looks at it" in r["reply"] and "passed this to" not in r["reply"]
    assert h.ticket(tid)["status"] != "ESCALATION_QUEUED"
    if "What kind of security concern" in r["reply"]:  # skipped when the message already says "phishing"
        r = h.chat(emp, sid, "Phishing Email")
    assert "Have you done anything with it so far?" in r["reply"] and "I clicked a link" in r["quick_replies"]
    r = h.chat(emp, sid, "Nothing yet")
    assert "attach a screenshot" in r["reply"] and r["quick_replies"] == ["Skip"]
    r = h.chat(emp, sid, "Skip")
    assert "that gives Security what they need" in r["reply"] and "passed this to the **Security Operations**" in r["reply"]
    t = h.ticket(tid)
    assert t["status"] == "ESCALATION_QUEUED" and t["priority"] == "P2"
    handoff = __import__("json").loads(t["handoff_json"])
    assert handoff["details_collected"]["actions_already_taken"] == "Nothing yet"


def test_phishing_report_turns_urgent_when_they_clicked(h):
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    r = h.chat(emp, sid, "I think I got a phishing email")
    tid = r["ticket_id"]
    if "What kind of security concern" in r["reply"]:
        h.chat(emp, sid, "Phishing Email")
    r = h.chat(emp, sid, "I entered my password")
    t = h.ticket(tid)
    assert t["priority"] == "P1" and t["status"] == "ESCALATION_QUEUED"
    assert "don't use that password anywhere else" in r["reply"]
    assert len(h.api.store.tickets(employee_id=emp)) == 1  # the same ticket, upgraded


def test_past_due_times_never_say_today():
    ist = bh._calendar()[0]
    now = datetime(2026, 10, 3, 12, 30, tzinfo=ist).astimezone(timezone.utc)
    past = datetime(2026, 9, 28, 13, 0, tzinfo=ist).astimezone(timezone.utc)
    assert bh.friendly(past, now) == "by 1:00 pm on Monday, 28 Sep"


def test_frustrated_follow_up_is_about_this_ticket_with_empathy_and_a_way_to_chase(h):
    from servicedesk.orchestrator import CHASE
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    tid = h.chat(emp, sid, "I clicked a link and typed my password on a fake login page")["ticket_id"]
    r = h.chat(emp, sid, "I really get frustrated when will I get solve of this ticket issue")
    assert "I'm sorry this is taking longer" in r["reply"] and tid in r["reply"]
    assert "Here's where your tickets stand" not in r["reply"] and r["quick_replies"] == [CHASE]
    r = h.chat(emp, sid, CHASE)
    assert "asked them to prioritise" in r["reply"]
    sup = h.api.store.one("SELECT COUNT(*) AS n FROM notifications WHERE user_id='EMP2001' AND ticket_id=? "
                          "AND text LIKE '%prioritised%'", (tid,))
    assert sup["n"] == 1
    r = h.chat(emp, sid, "status update please, this is urgent")
    assert "already asked the team lead" in r["reply"] and not r["quick_replies"]


def test_something_else_asks_for_details_before_a_person(h):
    """Regression (3 Oct): "os crash" → Something else went straight to the Duty Manager with one line and no time."""
    from servicedesk.orchestrator import SOMETHING_ELSE, WORK_IMPACT
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    r = h.chat(emp, sid, "something odd keeps happening")
    assert SOMETHING_ELSE in r["quick_replies"]
    r = h.chat(emp, sid, SOMETHING_ELSE)
    assert "What happens?" in r["reply"] and not r.get("ticket_id")
    assert h.api.store.tickets(employee_id=emp) == []  # nothing opened yet
    r = h.chat(emp, sid, "odd stuff, then it goes away")
    assert "How much is this affecting your work" in r["reply"] and r["quick_replies"] == list(WORK_IMPACT)
    r = h.chat(emp, sid, "I can't work at all")
    assert "passed this to the **Service Desk Duty Manager**" in r["reply"] and "First reply expected" in r["reply"]
    t = h.api.store.tickets(employee_id=emp)[0]
    assert t["priority"] == "P2" and "odd stuff" in t["summary"]
    details = __import__("json").loads(t["handoff_json"])["details_collected"]
    assert details == {"what_happens": "odd stuff, then it goes away", "effect_on_work": "I can't work at all"}


def test_a_new_problem_in_the_same_chat_starts_clean(h):
    """Regression (3 Oct): a second problem in the same chat picked up the first ticket's answers and the
    model read the old conversation."""
    import json
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    first = h.chat(emp, sid, "I think I got a phishing email")["ticket_id"]
    if "What kind of security concern" in h.api.store.messages(sid)[-1]["text"]:
        h.chat(emp, sid, "Phishing Email")
    h.chat(emp, sid, "Nothing yet")
    h.chat(emp, sid, "Skip")  # handed to Security with actions_already_taken = Nothing yet
    seen = []
    real = h.api.conv.brain.triage
    h.api.conv.brain.triage = lambda msg, history: seen.append(history) or real(msg, history)
    try:
        r = h.chat(emp, sid, "Outlook isn't sending email")
    finally:
        h.api.conv.brain.triage = real
    assert not any("phishing" in m["text"].lower() for m in seen[0])  # the old problem isn't read again
    state = json.loads(h.api.store.one("SELECT state_json FROM sessions WHERE session_id=?", (sid,))["state_json"])
    assert "actions_already_taken" not in state.get("answers", {}) and not state.get("security_report")
    assert r.get("ticket_id") != first
    rows = {s["session_id"]: s for s in h.c.get("/chat/sessions", headers=h.login(emp)).json()}
    assert rows[sid]["n_messages"] > 0 and "in_progress" in rows[sid]


def test_a_lost_phone_is_handled_around_the_clock():
    """Regression (3 Oct): a lost phone reported on Saturday said 'first reply by 10:00 am on Monday'."""
    ist = bh._calendar()[0]
    sat_7pm = datetime(2026, 10, 3, 19, 30, tzinfo=ist).astimezone(timezone.utc)
    assert bh.add_hours(sat_7pm, 1, "P2", "CAT-12", "KB-031").astimezone(ist) == datetime(2026, 10, 3, 20, 30, tzinfo=ist)
    assert bh.add_hours(sat_7pm, 1, "P2", "CAT-12", "KB-030").astimezone(ist).weekday() == 0  # a sync problem waits


def test_a_confident_it_category_beats_not_it(h):
    """Regression (3 Oct): "I am unable to join a meeting" got "I'm only set up for IT problems"."""
    from servicedesk.brain import Triage
    from servicedesk.orchestrator import OPEN_TICKET
    emp = h.fresh_employee()
    brain, real = h.api.conv.brain, h.api.conv.brain.triage

    def says_not_it(msg, history, cats=None):
        t = real(msg, history)
        return Triage(intent="out_of_scope", intent_confidence=.88, categories=cats or t.categories,
                      category_confidence=(cats or t.categories)[0][1], impact=t.impact, urgency=t.urgency,
                      flags=t.flags, raw=t.raw)
    try:
        brain.triage = lambda m, hist: says_not_it(m, hist, [("CAT-11", .95), ("OTHER", .05)])
        r = h.chat(emp, h.new_session(emp), "i am unable to join a meeting")
        assert "doesn't look like an IT problem" not in r["reply"] and r.get("ticket_id")
        brain.triage = lambda m, hist: says_not_it(m, hist, [("OTHER", .9), ("CAT-11", .1)])
        sid = h.new_session(emp)
        r = h.chat(emp, sid, "what's the canteen menu today")
        assert "doesn't look like an IT problem" in r["reply"] and r["quick_replies"] == [OPEN_TICKET]
    finally:
        brain.triage = real
    r = h.chat(emp, sid, OPEN_TICKET)  # the way out still works if we've misread it
    assert r.get("ticket_id") or "which of these is closest" in r["reply"]


def test_overdue_ticket_says_overdue(h):
    emp = h.fresh_employee()
    sid = h.new_session(emp)
    tid = h.chat(emp, sid, "I clicked a link and typed my password on a fake login page")["ticket_id"]
    h.api.store.execute("UPDATE tickets SET created_at='2026-09-01T03:00:00+00:00' WHERE ticket_id=?", (tid,))
    r = h.chat(emp, sid, "status update")
    assert "**overdue**" in r["reply"] and "today" not in r["reply"]
