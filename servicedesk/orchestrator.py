"""ConversationService (the main agent): chat / form turns, Jev calls, KB attempts.

Jev supplies judgments (brain.py). This file supplies policy: safety gates, verification levels,
the two-attempt limit, escalation and the handoff package. Every user-facing sentence is either a
fixed template or approved KB text, so the bot cannot invent a fix. Employees see the approved employee
version of an article (services/kb.py); agent-only notes (pre-checks, tool IDs) never reach them.

It never changes a ticket's status itself: it asks TicketService.transition(), which rejects
anything outside the lifecycle table (spec §5.3). One pipeline, many doors: chat and the ticket
form share the same gates and sub-agents.
"""
from __future__ import annotations

import copy
import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import business_hours as bh
from . import config, tools
from .brain import BrainError, FormCheck, form_text, make_brain
from .directory import employee_profile
from .knowledge import ATTEMPT_2_FALLBACK, INCIDENT_CATEGORIES, OTHER_CATEGORY, get_knowledge
from .services.alerts import AlertService
from .services.attachments import AttachmentService, problem_lines, summary_for_bot
from .services.kb import KnowledgeService
from .services.notify import NotificationService
from .services.tickets import TicketService, employee_label
from .store import Store, now

FIXED, NOT_FIXED, HELP, HUMAN = ("Yes, it's fixed", "No, still not working",
                                 "I need help with a step", "Talk to a human")
VALIDATION_REPLIES = [FIXED, NOT_FIXED, HELP, HUMAN]
SOMETHING_ELSE = "Something else"
# "Something else": how much it stops their work → (impact, urgency) for the priority matrix
WORK_IMPACT = {"I can't work at all": ("moderate", "high"),
               "Other people have it too": ("significant", "high"),
               "I can work, but it's slowing me down": ("limited", "medium"),
               "It's a minor annoyance": ("limited", "low")}
OPEN_TICKET = "Open a ticket for this"
SAME_ISSUE, NEW_ISSUE = "Yes, same issue", "No, it's something new"
SAME_OUTAGE, JUST_ME = "Yes, same problem", "No, it's just me"
PERSON_PLEASE = "Connect me with a person"
# answers (content/field_questions.json, CAT-09.actions_already_taken) that make a report urgent at once
_SECURITY_HARM_ANSWERS = {"I clicked a link", "I entered my password", "I opened an attachment"}
WRONG_SHOT, SEPARATE_PROBLEM, CARRY_ON =("Wrong screenshot, I'll send another", "It's a separate problem",
                                          "Carry on with my current issue")
# Scripts we can't read yet (Tamil, Devanagari, Telugu, Kannada, Malayalam, Bengali, Gujarati, Gurmukhi, Odia)
_NON_ENGLISH_SCRIPT = re.compile(r"[\u0900-\u0D7F]")
_NON_USER_PRESTEP = re.compile(r"TOOL-|^Collect|^Ask |^Retrieve|^Confirm scope|^Check known|^Confirm "
                               r"(OS|tool|client|app|printer|device|connection|software|exact|MFA)", re.I)
_SECRET_PATTERN = re.compile(r"(?i)\b(password|passcode|pwd|otp|one[- ]time code|code|pin|api[_ -]?key|token)"
                             r"(\s*(is|was|=|:)\s*)(\S{4,})")
HIDDEN = "[message hidden: it appeared to contain a password or code]"
CHASE = "Ask the team to prioritise this"
DONE_STATUSES = ("RESOLVED", "CLOSED", "CANCELLED", "RESOLVED_PENDING_CONFIRMATION")
_FRUSTRATION = re.compile(r"(?i)\b(frustrat\w*|annoy\w*|angry|upset|fed up|ridiculous|unacceptable|useless|"
                          r"disappointed|still waiting|waiting (so|too|very) long|no one|nobody|hurry|asap|"
                          r"urgent(ly)?|how much longer|taking (so|too) long)\b")
# "status update", "any update?", "progress?" (but not "Windows update failed": that's a new problem)
_STATUS_WORDS = re.compile(r"(?i)(\bstatus\b|\bany updates?\b|\bupdate on\b|^\s*updates?\W*$|\bprogress\b|\bany news\b)")
_TIMING_QUESTION = re.compile(r"(?i)\b(when|which (day|date|monday|tuesday|wednesday|thursday|friday|saturday|"
                              r"sunday)|what (time|day|date)|exact (date|time|day)|eta|how long)\b")


@dataclass
class Reply:
    text: str | None
    quick_replies: list[str] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    ticket_id: str | None = None
    meta: dict = field(default_factory=dict)  # stored with the message, e.g. which bot answer to rate
    message_id: int | None = None


def user_voice(action: str) -> str:
    """KB steps are written for agents ("guide user to ..."); present them to the employee."""
    a = re.sub(r"^(guide|direct|instruct|ask)( the)? user( to)? ", "", action.strip(), flags=re.I)
    a = re.sub(r"\s*\(TOOL-[^)]*\)|\s+via TOOL-\d+", "", a)  # tool IDs are internal
    return a[0].upper() + a[1:] if a else a


def redact_inline(text: str) -> str:
    """Hide just the secret values in free text (feedback comments, OCR text); keep the rest readable."""
    return _SECRET_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}[hidden]", text or "")


def mask_secrets(text: str) -> str:
    """Mask what looks like a secret value; if nothing matches, hide the whole text."""
    masked = _SECRET_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}[hidden]", text or "")
    return masked if masked != text else ("[hidden: appeared to contain a password or code]" if text else text)


def _reply_extras(r: Reply) -> dict:
    """What the client needs besides the text: the attempt number and whether the answer can be rated."""
    return {"attempt": r.meta.get("attempt"), "message_id": r.message_id, "can_rate": bool(r.meta.get("rate"))}


def _pct(p: float) -> str:
    return f"{p:.0%}"


def _numbered(steps: list[str]) -> str:
    return "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))


TYPE_OWN = "Something else, I'll type it"
PRIORITY_WORD = {"P1": "critical", "P2": "high", "P3": "medium", "P4": "low"}
_FIELD_QUESTIONS: dict = json.loads((Path(__file__).parent / "content" / "field_questions.json")
                                    .read_text(encoding="utf-8"))
# what the employee does after a verified tool action (the tool result says what was done, not what's next)
_AFTER_ACTION = {"KB-002": "Open that link, choose a new password, then sign in again.",
                 "KB-003": "Try signing in again now.",
                 "KB-016": "Open the MFA setup portal and scan the new QR code with your authenticator app."}


def _field_spec(f: dict) -> dict:
    return _FIELD_QUESTIONS.get(f"{f.get('Category_ID')}.{f['Field_Name']}", {})


def field_question(f: dict) -> tuple[str, list[str]]:
    """(question, answer buttons) for a detail the assistant needs. Wording and options come from
    content/field_questions.json; the dataset's dropdown options are the fallback."""
    spec = _field_spec(f)
    ask = spec.get("ask") or f["Help_Text"].rstrip(" .?") + "?"
    opts = list(spec.get("options") or ([o.strip() for o in f["Options_or_Source"].split("|")]
                                        if f["UI_Control"] in ("dropdown", "yes_no") else []))
    if spec.get("type_own"):
        opts.append(TYPE_OWN)
    if not opts and not spec.get("ask") and f.get("Options_or_Source"):
        ask += f"\n\n_For example: {f['Options_or_Source']}_"
    return ask, opts


def profile_answer(f: dict, employee: dict) -> str | None:
    """Details we already know about the signed-in employee are never asked for."""
    src = _field_spec(f).get("from_profile")
    if src == "username":
        return (employee.get("Email") or "").split("@")[0] or None
    return employee.get(src) if src else None


class ConversationService:
    def __init__(self, store: Store | None = None, tickets: TicketService | None = None,
                 alerts: AlertService | None = None, notify: NotificationService | None = None):
        self.k = get_knowledge()
        self.store = store or Store()
        self.notify = notify or NotificationService(self.store)
        self.alerts = alerts or AlertService(self.store, self.k, notify=self.notify)
        self.tickets = tickets or TicketService(self.store, self.k, self.alerts, self.notify)
        self.brain = make_brain(self.k)
        self.kbs = KnowledgeService(self.store, self.k, self.brain)
        self.atts = AttachmentService(self.store)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ================================================================ entry points
    def _session_lock(self, session_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(session_id, threading.Lock())

    def handle(self, session_id: str, employee_id: str, message: str,
               attachments: list[dict] | None = None) -> Reply:
        """One employee turn. Per-turn state lives on a shallow copy, so API worker threads never share
        it; a per-session lock stops a double-submit from running two turns on one conversation.
        `attachments` are already-processed screenshots: their text joins the message for triage."""
        with self._session_lock(session_id):
            turn = copy.copy(self)
            turn._attachments = attachments or []
            return turn._handle(session_id, employee_id, message)

    def _begin(self, session_id: str, employee_id: str) -> dict:
        st = self.store.get_session(session_id) or {"stage": "IDLE", "employee_id": employee_id}
        self._trace: list[dict] = []
        self._notes: list[str] = []
        self._st, self._sid = st, session_id
        self._employee = employee_profile(self.store, self.k, employee_id)
        self._channel = st.get("channel", "Chat")
        return st

    def _handle(self, session_id: str, employee_id: str, message: str) -> Reply:
        st = self._begin(session_id, employee_id)
        self._user_text = message or "(sent a screenshot)"  # may be replaced by a redaction
        atts = getattr(self, "_attachments", [])
        self._screenshot_error = next((ln for a in atts if a["readable"]
                                       for ln in problem_lines(a["ocr_text"] or "")), "")
        typed, context = message, ""
        if atts and st.get("stage") == "CONFIRM_SCREENSHOT":  # a new screenshot replaces the doubtful one
            st["stage"] = st.pop("shot_pending", {}).get("prev_stage", "IDLE")
        # (not for security reports: a phishing email's screenshot can look like any other topic, and it's evidence)
        if atts and self._screenshot_error and st.get("ticket_id") and st.get("stage") in (
                "COLLECTING", "VALIDATING", "ESCALATED") and not st.get("security_report") \
                and st.get("category_id") != "CAT-09":
            try:
                other = self._screenshot_other_problem()
            except BrainError:
                other = None
            if other:  # never act on a screenshot of a different problem without asking
                return self._finish(self._ask_about_screenshot(other, typed))
        if atts:  # what the screenshot says becomes part of what Jev reads; the note tells the employee
            context, note = summary_for_bot(atts)
            if note:
                self._notes.append(note)
            if context:
                # a screenshot-only message would otherwise become a ticket summary of raw OCR text
                lead = message.strip() or (f"Screenshot shows: {self._screenshot_error[:160]}"
                                           if self._screenshot_error else "Sent a screenshot")
                message = f"{lead}\n\nText from the attached screenshot:\n{context}"
            self._t("Screenshot", f"{len(atts)} attached · error codes: "
                                  f"{sorted({c for a in atts for c in a['error_codes']}) or 'none'}")
        if atts and not typed.strip() and not context:
            # an image with nothing usable and no words: don't guess (and don't greet them as if they'd said hi)
            return self._finish(self._reprompt())
        return self._finish(self._route(message))

    def _route(self, message: str) -> Reply:
        st = self._st
        try:
            stage = st.get("stage", "IDLE")
            if stage == "HUMAN":
                reply = self._to_human_owner(message)  # a human owns the conversation: the bot stays silent
            elif stage == "CONFIRM_SCREENSHOT":
                reply = self._on_confirm_screenshot(message)
            elif stage == "CONFIRM_CATEGORY":
                reply = self._on_confirm_category(message)
            elif stage == "DESCRIBE_OTHER":
                reply = self._on_describe_other(message)
            elif stage == "OTHER_IMPACT":
                reply = self._on_other_impact(message)
            elif stage == "CONFIRM_INCIDENT":
                reply = self._on_confirm_incident(message)
            elif stage == "CONFIRM_DUPLICATE":
                reply = self._on_confirm_duplicate(message)
            elif stage == "COLLECTING":
                reply = self._on_field_answer(message)
            elif stage == "VALIDATING":
                reply = self._on_validation(message)
            elif message == CHASE and st.get("ticket_id"):
                reply = self._chase()
            elif stage == "ESCALATED" and st.get("ticket_id") and (
                    message in VALIDATION_REPLIES or _TIMING_QUESTION.search(message)
                    or _STATUS_WORDS.search(message) or _FRUSTRATION.search(message)):
                # old buttons, "when / which Monday?", "status update", "I'm frustrated": about this ticket
                reply = self._after_handoff(message)
            elif message == OPEN_TICKET and st.get("pending_issue"):
                reply = self._triage(st["pending_issue"], force_ticket=True)
            elif message == PERSON_PLEASE and st.get("language_note"):
                reply = self._language_handoff()
            elif _NON_ENGLISH_SCRIPT.search(message):
                reply = self._language_guard(message)
            else:
                reply = self._triage(message)
        except BrainError as e:
            reply = self._fallback(str(e), message)
        return reply

    # ---------------------------------------------------------------- screenshots that don't fit
    def _screenshot_other_problem(self) -> str | None:
        """Category of the screenshot's error when it clearly belongs to a different problem than the open
        ticket (e.g. a printer jam sent during a VPN fix); None when it fits or we can't tell."""
        t = self.tickets.get(self._st["ticket_id"])
        tri = self.brain.triage(self._screenshot_error, [])
        if not tri.categories:
            return None
        probs = dict(tri.categories)
        top, p = tri.categories[0]
        self._t("Screenshot check", f"screenshot looks like {self.k.name(top)} {_pct(p)}; ticket is "
                                    f"{self.k.name(t['category_id'])} {_pct(probs.get(t['category_id'], 0))}")
        if top not in (t["category_id"], OTHER_CATEGORY) and p >= config.SCREENSHOT_MISMATCH_CONFIDENCE \
                and probs.get(t["category_id"], 0) < 0.2:
            return top
        return None

    def _ask_about_screenshot(self, other_cat: str, typed: str) -> Reply:
        st = self._st
        t = self.tickets.get(st["ticket_id"])
        st["shot_pending"] = {"prev_stage": st["stage"], "typed": typed, "category": other_cat,
                              "error": self._screenshot_error}
        st["stage"] = "CONFIRM_SCREENSHOT"
        self._t("Screenshot check", "different problem → asked before using it")
        return Reply(f"Thanks for the screenshot. It shows _\"{self._screenshot_error[:140]}\"_, which looks like "
                     f"a **{self.k.name(other_cat)}** problem, but we're working on your "
                     f"**{self.k.name(t['category_id'])}** issue (**{t['ticket_id']}**).\n\nWhat would you like to do?",
                     quick_replies=[WRONG_SHOT, SEPARATE_PROBLEM, CARRY_ON])

    def _on_confirm_screenshot(self, message: str) -> Reply:
        st = self._st
        p = st.pop("shot_pending", {})
        st["stage"] = p.get("prev_stage", "IDLE")
        tid = st.get("ticket_id")
        if message == SEPARATE_PROBLEM:
            st["stage"] = "IDLE"
            self._t("Screenshot check", "employee: separate problem → new triage")
            reply = self._triage(f"Screenshot shows: {p.get('error', '')}")
            reply.text = (f"Okay, I'll treat that as a new problem. Your **{tid}** ticket stays open.\n\n"
                          f"{reply.text or ''}").strip()
            return reply
        if message == WRONG_SHOT:
            return self._reprompt("No problem. Attach the right one with the **+** button whenever you're ready.")
        if message == CARRY_ON:
            if (p.get("typed") or "").strip():  # what they typed with the screenshot still counts
                self._user_text = p["typed"]
                return self._route(p["typed"])
            return self._reprompt("Okay, let's carry on.")
        return self._route(message)  # they answered in words instead: handle it as a normal reply

    def _reprompt(self, lead: str = "") -> Reply:
        """Ask again for whatever we were waiting for, with its buttons."""
        st = self._st
        stage = st.get("stage", "IDLE")
        if stage == "COLLECTING" and st.get("current_field"):
            ask, opts = field_question(st["current_field"])
            return Reply(f"{lead}\n\nMeanwhile: **{ask}**".strip(), quick_replies=opts)
        if stage == "VALIDATING":
            return Reply(f"{lead}\n\nDid the last steps fix it?".strip(), quick_replies=VALIDATION_REPLIES)
        if stage == "ESCALATED" and st.get("ticket_id"):
            r = self._after_handoff()
            r.text = f"{lead}\n\n{r.text}".strip()
            return r
        return Reply(f"{lead}\n\nWhat's going wrong? Tell me in a sentence and I'll take it from there.".strip())

    def _language_guard(self, message: str) -> Reply:
        """We can only triage English reliably. Say so kindly and offer a person, never guess."""
        st = self._st
        st["language_note"] = message
        self._t("Language", "non-English script detected → asked to rephrase or connect a person")
        return Reply("Sorry, I can only read English at the moment, so I don't want to guess what you need. "
                     "Could you describe the problem in English? If that's hard, I'll connect you with a person "
                     "from IT instead.", quick_replies=[PERSON_PLEASE])

    def _language_handoff(self) -> Reply:
        st = self._st
        st["pending_issue"] = st.pop("language_note")
        st.setdefault("triage", {"impact": "limited", "urgency": "medium", "flags": {}, "categories": []})
        st["issue_context"] = st["pending_issue"]
        self._open_ticket(OTHER_CATEGORY, language="non-English")
        return self._escalate("Employee wrote in a language the bot can't read — a person will help.",
                              "No problem. I've passed your message to the team exactly as you wrote it.")

    def _finish(self, reply: Reply, record_user: bool = True) -> Reply:
        st, sid = self._st, self._sid
        atts = getattr(self, "_attachments", [])
        if record_user and getattr(self, "_user_text", None) is not None:
            self.store.add_message(sid, "user", self._user_text, ticket_id=st.get("ticket_id"),
                                   author=self._employee["Employee_ID"],
                                   meta={"attachment_ids": [a["id"] for a in atts]} if atts else None)
        if st.get("ticket_id"):
            self.atts.link_session(sid, st["ticket_id"])
        if reply.text:
            if self._notes:
                reply.text = "\n\n".join(self._notes + [reply.text])
            reply.trace = self._trace
            reply.ticket_id = reply.ticket_id or st.get("ticket_id")
            reply.message_id = self.store.add_message(sid, "bot", reply.text, ticket_id=reply.ticket_id,
                                                      meta={"quick_replies": reply.quick_replies,
                                                            "trace": reply.trace, **reply.meta})
        self.store.save_session(sid, self._employee["Employee_ID"], st)
        return reply

    # ================================================================ helpers
    def _t(self, step: str, detail: str, data: dict | None = None):
        self._trace.append({"step": step, "detail": detail})
        self.store.log_event(step, {"detail": detail, **(data or {})},
                             ticket_id=self._st.get("ticket_id"), session_id=self._sid, actor="bot")

    def _to(self, status: str, reason: str = "", **fields) -> None:
        self.tickets.transition(self._st["ticket_id"], status, "bot", reason, **fields)

    def _history(self) -> list[dict]:
        return [{"role": m["role"], "text": m["text"]} for m in
                self.store.messages(self._sid, customer_only=True)[-8:]]

    def _apply_guard(self, flags: dict[str, float]) -> None:
        """SEC-02 / SEC-03: mask secrets, refuse manipulation, keep going on the real issue."""
        if flags.get("shared_secret", 0) >= config.RISK_FLAG_THRESHOLD:
            self._user_text = HIDDEN
            self._notes.append("🔒 It looks like your message contained a password or one-time code. I've hidden "
                               "it from the ticket. Please never share secrets in chat, and change that "
                               "password if it's a real one.")
            self._t("Safety", f"secret masked (p={flags['shared_secret']:.2f})")
        if flags.get("manipulation", 0) >= config.RISK_FLAG_THRESHOLD:
            self._notes.append("Just so you know: I can't skip verification or approvals for anyone. I'll "
                               "handle this the standard way, which keeps everyone's accounts safe.")
            self._t("Safety", f"manipulation attempt ignored (p={flags['manipulation']:.2f})")

    @staticmethod
    def _risky(flags, key) -> bool:
        return flags.get(key, 0) >= config.RISK_FLAG_THRESHOLD

    def _to_human_owner(self, message: str) -> Reply:
        """Spec §5.4: once a human takes over, the bot stays silent on that session."""
        t = self.store.get_ticket(self._st.get("ticket_id") or "")
        try:  # secrets are still masked before storage, even while the bot is silent
            if self._risky(self.brain.guard(message), "shared_secret"):
                self._user_text = HIDDEN
                self.store.add_message(self._sid, "system", "🔒 Your message looked like it contained a password or "
                                       "code, so it was hidden from the ticket.", ticket_id=self._st.get("ticket_id"))
        except BrainError:
            pass
        if t and t["owner"]:
            self.notify.notify(t["owner"], t["ticket_id"],
                               f"Employee replied on {t['ticket_id']}: {self._user_text[:80]}")
        return Reply(None)

    # ================================================================ triage
    def _triage(self, message: str, force_ticket: bool = False, unclear_ok: bool = False) -> Reply:
        st = self._st
        tri = self.brain.triage(message, self._history())
        top = ", ".join(f"{self.k.name(c)} {_pct(p)}" for c, p in tri.categories[:3])
        self._t("Intent router", f"{tri.intent} (confidence {_pct(tri.intent_confidence)})", tri.raw)
        self._t("Ticket triage", f"category → {top}; impact={tri.impact}, urgency={tri.urgency}")
        raised = {k: round(v, 2) for k, v in tri.flags.items() if v >= config.RISK_FLAG_THRESHOLD}
        if raised:
            self._t("Safety gate", f"flags raised: {raised}")
        self._apply_guard(tri.flags)

        st["triage"] = {"intent": tri.intent, "impact": tri.impact, "urgency": tri.urgency, "flags": tri.flags,
                        "categories": tri.categories[:4], "category_confidence": tri.category_confidence}
        st["pending_issue"] = message if self._user_text != HIDDEN else "(redacted)"
        st["issue_context"] = st["pending_issue"]
        st.pop("dup_declined", None)

        danger = self._risky(tri.flags, "security_incident") or self._risky(tri.flags, "physical_safety")
        if self._risky(tri.flags, "shared_secret") and not danger:
            st.pop("pending_issue", None)
            return Reply("Could you describe the problem again, without the password or code? "
                         "For example: \"my password isn't accepted on the VPN\".")
        intent = "report_it_problem" if (force_ticket or danger) else tri.intent

        if intent == "small_talk":
            open_t = [t for t in self.store.tickets(employee_id=self._employee["Employee_ID"])
                      if t["status"] not in ("RESOLVED", "CLOSED", "CANCELLED")]
            extra = f" Your ticket {open_t[0]['ticket_id']} is still open." if open_t else ""
            return Reply("Hi! 👋 I'm the IT Service Desk assistant. Tell me what's going wrong, in your own words, "
                         "and I'll help you fix it or get it to the right person. You can attach a screenshot too."
                         f"{extra}")
        if intent == "out_of_scope":
            return Reply("I'm only set up for IT problems, so I can't help with that one. For HR, payroll or "
                         "travel, the employee portal is the best place to go.")
        if intent == "ticket_status":  # in a chat that has a ticket, they mean that one
            return self._after_handoff(message) if st.get("ticket_id") else self._ticket_status()
        if intent == "how_to_question":
            return self._answer_how_to(message, tri)

        # --- it's a ticket
        cat = tri.category
        if self._risky(tri.flags, "security_incident"):
            # accuracy fix: a lost/stolen phone stays in Mobile & MDM (wipe policy), not generic SOC
            cat = "CAT-12" if tri.category == "CAT-12" else "CAT-09"
        elif self._risky(tri.flags, "physical_safety"):
            cat = "CAT-04"
        elif cat == OTHER_CATEGORY or tri.category_confidence < config.CATEGORY_MIN_CONFIDENCE:
            if unclear_ok:  # they've already said none of the teams fit: don't offer the same list again
                self._t("Confirm classification", "still unclear after more detail → asking how it affects work")
                return self._ask_other_impact()
            options = [self.k.name(c) for c, _ in tri.categories if c != OTHER_CATEGORY][:3]
            st["stage"] = "CONFIRM_CATEGORY"
            why = ("top category is Other / unclear" if cat == OTHER_CATEGORY else
                   f"confidence {_pct(tri.category_confidence)} < {_pct(config.CATEGORY_MIN_CONFIDENCE)}")
            self._t("Confirm classification", f"{why} → asking user (CHAT-02)")
            return Reply("Thanks. So I can send this to the right team, which of these is closest?",
                         quick_replies=options + [SOMETHING_ELSE])
        if not danger:
            dup = self._duplicate_prompt(cat)
            if dup:
                return dup
        return self._plan_ticket(cat)

    def _on_confirm_category(self, message: str) -> Reply:
        names = {self.k.name(c): c for c in self.k.categories}
        if message in names:
            self._t("Confirm classification", f"user chose {message}")
            self._st["stage"] = "IDLE"
            return self._duplicate_prompt(names[message]) or self._plan_ticket(names[message])
        if message == SOMETHING_ELSE:
            # never hand a person a one-line "os crash": ask what happens first, then how much it stops work
            self._t("Confirm classification", "user chose Something else → asking for details")
            self._st["stage"] = "DESCRIBE_OTHER"
            return Reply("No problem. Tell me a bit more so I can either fix it or get it to the right person:\n\n"
                         "- **What happens?** For example: _\"blue screen, then it restarts\"_ or _\"the screen "
                         "freezes and I have to hold the power button\"_\n"
                         "- **When does it happen?** At start-up, when you open a particular app, or at random?\n"
                         "- **Any error message?** Type it, or attach a screenshot.")
        # free text: re-triage with the extra detail
        self._st["stage"] = "IDLE"
        return self._triage(f"{self._st.get('pending_issue', '')}\n{message}".strip(), unclear_ok=True)

    def _on_describe_other(self, message: str) -> Reply:
        """Their fuller description: if it now matches a team, carry on there; if not, ask about impact."""
        st = self._st
        st["stage"] = "IDLE"
        st["other_detail"] = message
        # both messages together become the ticket summary, so the team sees the full picture
        return self._triage(f"{st.get('pending_issue', '')}\n{message}".strip(), unclear_ok=True)

    def _ask_other_impact(self) -> Reply:
        self._st["stage"] = "OTHER_IMPACT"
        return Reply("Thanks, that helps. **How much is this affecting your work right now?**",
                     quick_replies=list(WORK_IMPACT))

    def _on_other_impact(self, message: str) -> Reply:
        st = self._st
        if message not in WORK_IMPACT:  # typed instead of pressing a button: treat it as more detail
            st["other_detail"] = f"{st.get('other_detail', '')}\n{message}".strip()
            st["pending_issue"] = f"{st.get('pending_issue', '')}\n{message}".strip()
            return Reply("Got it, I've added that. **How much is this affecting your work right now?**",
                         quick_replies=list(WORK_IMPACT))
        st["stage"] = "IDLE"
        impact, urgency = WORK_IMPACT[message]
        st.setdefault("triage", {}).update(impact=impact, urgency=urgency)
        detail = st.get("other_detail", "")
        st["answers"] = {k: v for k, v in (("what_happens", detail), ("effect_on_work", message)) if v}
        self._open_ticket(OTHER_CATEGORY)
        self._t("Confirm classification", f"no team matched; effect on work: {message} → human")
        return self._escalate("No standard category matched after asking for details; goes to a person.",
                              "Thanks. This doesn't match one of the fixes I can run myself, so I'm getting a "
                              "person to look at it, with everything you've told me.")

    # ---------------------------------------------------------------- duplicate guard (spec §5.4, §9)
    def _duplicate_prompt(self, cat: str) -> Reply | None:
        st = self._st
        if st.get("dup_declined") == cat:
            return None
        dup = self.tickets.open_duplicate(self._employee["Employee_ID"], cat)
        if not dup:
            return None
        st.update(stage="CONFIRM_DUPLICATE", dup_ticket=dup["ticket_id"], dup_category=cat)
        self._t("Duplicate guard", f"open {self.k.name(cat)} ticket {dup['ticket_id']} in the last "
                                   f"{config.DUPLICATE_WINDOW_HOURS}h → asking user")
        label = employee_label(dup, self.tickets.user_name(dup["owner"]))
        return Reply(f"Before I open a new ticket: is this about **{dup['ticket_id']}**, _{dup['summary'][:90]}_ "
                     f"({label})?",
                     quick_replies=[SAME_ISSUE, NEW_ISSUE])

    def _on_confirm_duplicate(self, message: str) -> Reply:
        st = self._st
        dup_id, cat = st.pop("dup_ticket", None), st.pop("dup_category", None)
        st["stage"] = "IDLE"
        if message == SAME_ISSUE and dup_id:
            dup = self.tickets.get(dup_id)
            self.store.add_message(dup["session_id"] or self._sid, "user", st.get("pending_issue", ""),
                                   ticket_id=dup_id, author=self._employee["Employee_ID"])
            if dup["owner"]:
                self.notify.notify(dup["owner"], dup_id, f"Employee added more detail to {dup_id}.")
            self._t("Duplicate guard", f"user confirmed same issue → added to {dup_id}, no new ticket")
            st["ticket_id"] = dup_id
            label = employee_label(dup, self.tickets.user_name(dup["owner"]))
            return Reply(f"Got it. I've added this to **{dup_id}** instead of opening a new ticket, so everything "
                         f"stays in one place. It's currently **{label}**.", ticket_id=dup_id)
        st["dup_declined"] = cat
        self._t("Duplicate guard", "user said it's a new issue")
        if not cat:
            return self._triage(message)
        return self._plan_ticket(cat)

    # ================================================================ chat agent (no ticket)
    def _answer_how_to(self, message: str, tri) -> Reply:
        cats = [c for c, p in tri.categories[:2] if c != OTHER_CATEGORY]
        cands = self.k.kb_for_categories(cats)
        g = self.brain.ground(message, cands, [])
        self._t("Retriever", self._kb_trace(g.kb_scores), g.raw)
        best = max(g.kb_scores.items(), key=lambda x: x[1], default=(None, 0))
        if best[1] < config.KB_MIN_RELEVANCE:
            self._gap(cats[0] if cats else OTHER_CATEGORY, message, best)
            return Reply("I don't have an approved answer for that one, and I'd rather not guess. Shall I open "
                         "a ticket so someone from IT can help?", quick_replies=[OPEN_TICKET])
        kb = self.k.kb[best[0]]
        ev = self.kbs.live(kb.kb_id)
        who, body, plain = self._employee_steps(kb, 1)
        if ev and ev.get("attempt1") and who == "you":
            text = f"{ev['summary']}\n\n{body}"
            nxt = ev.get("attempt2")
            if nxt and nxt.get("who") == "you":
                text += f"\n\n**If that doesn't work:** {nxt.get('intro', '')}\n\n{_numbered(nxt['steps'])}"
        elif ev and ev.get("attempt1"):  # a team action: explain it
            text = f"{ev['summary']} {body}".strip()
        elif ev or not kb.attempt_1:
            return Reply("That one needs someone from IT rather than a quick fix. Shall I open a ticket for you?",
                         quick_replies=[OPEN_TICKET])
        elif kb.attempt_1:
            text = f"Here's what usually fixes it:\n\n1. {user_voice(kb.attempt_1)}."
            if kb.attempt_2:
                text += f"\n\nIf that doesn't work: {user_voice(kb.attempt_2)}."
        else:
            return Reply("That one needs someone from IT rather than a quick fix. Shall I open a ticket for you?",
                         quick_replies=[OPEN_TICKET])
        aid = self._record_answer(kb, 0, plain, ticket_id=None)
        return Reply(f"{text}\n\nIf this is happening to you right now, I can open a ticket and walk you "
                     "through it step by step.", quick_replies=[OPEN_TICKET, "Thanks!"], meta=self._rate(aid))

    def _when(self, t: dict) -> tuple[str, bool]:
        """(sentence about timing, overdue?) for an open ticket. Past times are called overdue, never "today"."""
        if t["status"] in DONE_STATUSES:
            return "", False
        sla = self.tickets.sla_status(t)
        if t["status"] == "ESCALATION_QUEUED" and sla.get("response_eta"):
            if sla.get("response_state") == "breached":
                return (f"⚠️ Their first reply is **overdue**: it was due "
                        f"{sla['response_eta'].removeprefix('by ')}."), True
            return f"First reply expected **{sla['response_eta']}**.", False
        if t["status"] in ("HUMAN_ASSIGNED", "HUMAN_IN_PROGRESS") and sla.get("resolve_eta"):
            if sla.get("resolve_state") == "breached":
                return f"⚠️ The fix is **overdue**: the target was {sla['resolve_eta'].removeprefix('by ')}.", True
            return f"Target to fix: **{sla['resolve_eta']}**.", False
        return "", False

    def _after_handoff(self, message: str = "") -> Reply:
        """The ticket from this chat is with a team: where it is, when to expect them, and (when they're upset or
        it's late) a way to ask for it to be prioritised."""
        st = self._st
        t = self.tickets.get(st["ticket_id"])
        upset = bool(_FRUSTRATION.search(message or ""))
        when, overdue = self._when(t)
        tz = datetime.now(timezone.utc).astimezone(bh._calendar()[0]).strftime("%Z")
        state = (f"is with the **{t['queue']}** team" if t["status"] == "ESCALATION_QUEUED"
                 else f"– {employee_label(t, self.tickets.user_name(t['owner']))}")
        parts = []
        if upset:
            parts.append("I'm sorry this is taking longer than you'd like. I understand it's frustrating, "
                         "especially when it's stopping you from working.")
        parts.append(f"**{t['ticket_id']}** {state}.")
        if when:
            parts.append(f"{when} _(Times are {tz}.)_")
        buttons = []
        if t["status"] not in DONE_STATUSES and (upset or overdue):
            if st.get("chased") == t["ticket_id"]:
                parts.append("I've already asked the team lead to prioritise it; they've been notified.")
            else:
                parts.append("If it's urgent for you, I can ask the team lead to prioritise it now.")
                buttons = [CHASE]
        others = [o for o in self.store.tickets(employee_id=self._employee["Employee_ID"])
                  if o["ticket_id"] != t["ticket_id"] and o["status"] not in DONE_STATUSES]
        if others:
            parts.append(f"You have {len(others)} other open ticket{'s' if len(others) > 1 else ''}; "
                         "you'll find them all under **My tickets**.")
        parts.append("If anything has changed, such as a new error message, just describe it here or attach a "
                     "screenshot.")
        return Reply("\n\n".join(parts), quick_replies=buttons)

    def _chase(self) -> Reply:
        """The employee asks for their waiting ticket to be prioritised: the team lead (supervisors) and the
        owner are notified, and it's on the ticket's timeline. Once per ticket per conversation."""
        st = self._st
        t = self.tickets.get(st["ticket_id"])
        if st.get("chased") != t["ticket_id"]:
            st["chased"] = t["ticket_id"]
            name = self._employee.get("Full_Name") or self._employee.get("Employee_ID")
            text = (f"{name} is waiting on {t['ticket_id']} ({t['queue']}, {t['priority']}) and asked for it to be "
                    "prioritised.")
            for r in self.store.query("SELECT user_id FROM users WHERE role='supervisor'"):
                self.notify.notify(r["user_id"], t["ticket_id"], text)
            if t.get("owner"):
                self.notify.notify(t["owner"], t["ticket_id"], text)
            self.store.log_event("Employee asked to prioritise", {"detail": "via chat"}, ticket_id=t["ticket_id"],
                                 session_id=self._sid, actor=self._employee.get("Employee_ID"))
            self._t("Chase", f"supervisors{' and owner' if t.get('owner') else ''} notified")
        return Reply(f"Done. I've let the **{t['queue']}** team lead know you're waiting and asked them to "
                     f"prioritise **{t['ticket_id']}**. Their reply will appear right here.")

    def _ticket_status(self) -> Reply:
        """Answers "where's my ticket?", "which Monday?", "give the exact date": every open ticket with an
        exact date and time, the one from this conversation first."""
        ts = sorted(self.store.tickets(employee_id=self._employee["Employee_ID"]),
                    key=lambda t: t["created_at"] or "", reverse=True)
        current = self._st.get("ticket_id")
        ts = sorted(ts, key=lambda t: t["ticket_id"] != current)[:6]
        if not ts:
            return Reply("You don't have any tickets yet. Tell me what's wrong and I'll help.")
        lines = []
        for t in ts:
            sentence, _late = self._when(t)
            when = f"\n  {sentence}" if sentence else ""
            mark = " _(this conversation)_" if t["ticket_id"] == current else ""
            lines.append(f"- **{t['ticket_id']}**{mark}: {self.k.name(t['category_id'])}, "
                         f"{t['priority']} ({PRIORITY_WORD.get(t['priority'], 'normal')}). "
                         f"{employee_label(t, self.tickets.user_name(t['owner']))}.{when}")
        tz = datetime.now(timezone.utc).astimezone(bh._calendar()[0]).strftime("%Z")
        return Reply(f"Here's where your tickets stand. Times are {tz}.\n\n" + "\n".join(lines)
                     + "\n\nYou'll find every detail under **My tickets**.")

    # ================================================================ ticket planning
    def _open_ticket(self, cat: str, **extra) -> str:
        tri = self._st.get("triage", {})
        flags = tri.get("flags", {})
        tid = self.tickets.create(
            session_id=self._sid, employee_id=self._employee["Employee_ID"], category_id=cat,
            impact=tri.get("impact"), urgency=tri.get("urgency"), flags=flags,
            summary=self._st.get("pending_issue", ""), channel=self._channel,
            opened_by="Bot (form)" if self._channel == "Form" else "Bot (chat)",
            triage_json=json.dumps(tri, default=str), jev_confidence=tri.get("category_confidence"), **extra)
        t = self.tickets.get(tid)
        self._st.update(ticket_id=tid, category_id=cat, attempt=0, steps_tried=[], tool_results=[],
                        help_resends=0)
        self._st.setdefault("answers", {})
        self._t("Ticket created", f"{tid} · {self.k.name(cat)} · {t['priority']} ({t['priority_reason']}) "
                                  f"→ {self.k.sub_agent(cat)}")
        return tid

    def _gates(self, cat: str, ask_incident: bool = True) -> Reply | None:
        """Decision rule 3: safety, security and scope gate. Policy beats model confidence."""
        st = self._st
        flags = st["triage"]["flags"]
        ticket = self.tickets.get(st["ticket_id"])
        if self._risky(flags, "physical_safety"):
            return self._escalate(
                "Physical safety hazard reported.",
                "⚠️ **Please stop using the device now**: unplug it, don't charge it, and keep it away "
                "from people and anything flammable. Don't try to open or repair it.")
        if cat == "CAT-09" or (self._risky(flags, "security_incident") and cat != "CAT-12"):
            return self._security_path()
        if ticket["priority"] == "P1":
            return self._escalate("Computed priority P1 (critical) — human alerted immediately.")
        if cat in ("CAT-01", "CAT-05", "CAT-07") and self._risky(flags, "acting_for_someone_else"):
            return self._escalate("Request targets another person's account (SEC-06) — needs an authorised "
                                  "delegated workflow.",
                                  "I can only make changes to your own account, to keep everyone's accounts safe. "
                                  "Your colleague can contact IT themselves, or the team can check whether a "
                                  "delegated request applies.")
        if cat in ("CAT-01", "CAT-05") and self._risky(flags, "all_factors_lost"):
            self.store.update_ticket(st["ticket_id"], verification="V3 required", queue="Identity Security")
            return self._escalate("No registered sign-in factor available → V3 supervised identity recovery.",
                                  "Because none of your usual sign-in methods work, I can't confirm it's you "
                                  "in chat. That's on purpose, to protect your account. Identity Security will "
                                  "verify you another way and get you back in.")
        if self._risky(flags, "privileged_or_irreversible"):
            self.store.update_ticket(st["ticket_id"], verification="V4 + approval")
            return self._escalate("Privileged or irreversible action requested → V4 + approval (never auto-granted).",
                                  "Admin access and actions that can't be undone (like wiping a device) always "
                                  "need approval, for everyone. I've raised the approval request for you.")
        if self.k.categories.get(cat, {}).get("Mandatory_Handoff") == "Yes":
            return self._escalate("Category requires mandatory handoff.")
        if cat in INCIDENT_CATEGORIES:
            multi = self._risky(flags, "multiple_users_affected")
            parent = self.tickets.open_incident(cat, ticket["location"])
            if parent and parent["ticket_id"] != ticket["ticket_id"]:
                if multi:  # they told us others are affected: same outage, link straight away
                    return self._link_incident(parent)
                if ask_incident:  # same category + site but maybe just them: ask, don't guess
                    st.update(stage="CONFIRM_INCIDENT", incident=parent["ticket_id"])
                    self._t("Incident", f"open incident {parent['ticket_id']} at {ticket['location']} → asking user")
                    return Reply(f"There's already a known **{self.k.name(cat)}** problem at {ticket['location']}: "
                                 f"_{parent['summary'][:90]}_ ({parent['ticket_id']}). Is yours the same?",
                                 quick_replies=[SAME_OUTAGE, JUST_ME])
            if multi:
                self.store.update_ticket(st["ticket_id"], is_incident=1)
                return self._escalate("Multiple users/site affected → incident opened; matching tickets will link "
                                      "to it (KB-022).")
        return None

    def _on_confirm_incident(self, message: str) -> Reply:
        st = self._st
        parent_id, st["stage"] = st.pop("incident", None), "IDLE"
        parent = self.store.get_ticket(parent_id or "")
        if message == SAME_OUTAGE and parent:
            self._t("Incident", f"user confirmed same outage as {parent_id}")
            return self._link_incident(parent)
        self._t("Incident", f"user said it's not the {parent_id} outage → normal troubleshooting")
        return self._ground_and_continue(st["category_id"])

    def _link_incident(self, parent: dict) -> Reply:
        """Scenario 4: later tickets for the same outage link to the first one, with no new alert."""
        me = self.tickets.get(self._st["ticket_id"])
        self.store.update_ticket(me["ticket_id"], incident_parent=parent["ticket_id"], priority=parent["priority"],
                                 priority_reason=f"inherits incident {parent['ticket_id']}")
        self._t("Incident", f"linked to open incident {parent['ticket_id']} ({me['location']}) — no new alert")
        return self._escalate(f"Linked to incident {parent['ticket_id']}.",
                              f"It looks like you're affected by the known problem **{parent['ticket_id']}** at "
                              f"{me['location']}. The team is already on it, and I've linked your ticket so you'll "
                              "get the same updates as everyone else.")

    def _plan_ticket(self, cat: str) -> Reply:
        self._open_ticket(cat)
        return self._gates(cat) or self._ground_and_continue(cat)

    def _security_urgent(self) -> bool:
        """Something already happened (link clicked, password typed, malware ran): no questions, P1 now."""
        flags = self._st["triage"]["flags"]
        return (self._risky(flags, "security_incident") or self._risky(flags, "active_harm")
                or self.tickets.get(self._st["ticket_id"])["priority"] == "P1")

    def _security_path(self) -> Reply:
        """A report of something suspicious (nothing clicked yet) gets a few quick questions so Security has
        something to investigate; anything already acted on goes to Security immediately."""
        st = self._st
        urgent = self._security_urgent()
        fields = [] if urgent else self._security_questions()
        cands = self.k.kb_for_categories(["CAT-09"])
        g = self.brain.ground(st["issue_context"], cands, fields)
        self._t("Retriever", self._kb_trace(g.kb_scores), g.raw)
        self._save_kb_scores(g.kb_scores)
        kb_id = max(g.kb_scores, key=lambda k: g.kb_scores[k]) if g.kb_scores else "KB-024"
        st["kb_id"] = kb_id
        self.store.update_ticket(st["ticket_id"], kb_id=kb_id)
        missing = [f for f in fields if g.field_provided.get(f["Field_Name"], 0) < config.FIELD_PROVIDED_THRESHOLD]
        if urgent or not missing:
            return self._security_handoff(kb_id, asked=False)
        self._t("Security", f"report, nothing acted on yet → asking {[f['Field_Name'] for f in missing]} first")
        st["security_report"] = True
        st["pending_fields"] = missing[:3]
        st["asked_on"] = st["ticket_id"]  # our own intro below replaces the generic one
        reply = self._next_field_or_act()
        reply.text = ("Thank you for reporting this. Flagging it quickly is the right call.\n\n"
                      "🛡️ **Until Security looks at it:** don't click any links or open attachments in that message, "
                      "and don't reply to it.\n\nA few quick questions so Security can check it properly.\n\n"
                      f"{(reply.text or '').removeprefix('Thanks. ')}")
        return reply

    def _security_questions(self) -> list[dict]:
        fields = list(self.k.fields_to_ask("CAT-09"))
        shot = next((f for f in self.k.fields.get("CAT-09", []) if f["Field_Name"] == "email_headers_or_screenshot"),
                    None)
        if shot and not getattr(self, "_attachments", []):  # evidence helps; they can skip it
            fields.append(shot)
        return fields

    def _security_handoff(self, kb_id: str, asked: bool, urgent_answer: str = "") -> Reply:
        st = self._st
        st.pop("security_report", None)
        steps = {
            "KB-024": ["Don't click any links or open attachments in that message.",
                       "Don't reply to it or forward it to colleagues.",
                       "If you've already clicked a link or entered your password, tell me now so we can treat "
                       "it as urgent."],
            "KB-025": ["Treat your account as exposed: don't use that password anywhere else.",
                       "Stay signed out of anything that asks for it again.",
                       "Security will reset your password and MFA after verifying it's you."],
        }.get(kb_id, ["Don't take any further action on the suspicious item.",
                      "Keep it as it is, so Security can look at it."])
        if asked:  # they've just answered our questions: the "tell me if you clicked" step is done
            steps = [s for s in steps if not s.startswith("If you've already clicked")] + [
                "Don't delete the message yet: Security may need to look at it."]
        extra, what = {
            "I clicked a link": ("Close the page that opened, and don't type anything into it.", "you clicked the link"),
            "I opened an attachment": ("Disconnect from Wi-Fi (or unplug the network cable) but leave the computer on, "
                                       "so Security can check it.", "you opened the attachment"),
            "I entered my password": ("", "you entered your password"),
        }.get(urgent_answer, ("", "of what happened"))
        if extra:
            steps = [extra] + steps
        self._record_answer(self.k.kb[kb_id], 0, " ".join(steps), ticket_id=st["ticket_id"], outcome="escalated")
        lead = (f"Thanks for telling me. Because {what}, I've marked this **urgent** and sent it to Security right "
                "away." if urgent_answer else
                "Thank you, that gives Security what they need." if asked
                else "Thank you for reporting this. Flagging it quickly is the right call.")
        return self._escalate(f"Security incident — no resolution attempt, SOC handoff ({kb_id}).",
                              f"{lead}\n\n🛡️ **Until Security gets back to you:**\n"
                              + "\n".join(f"- {s}" for s in steps))

    def _security_escalate_now(self, why: str) -> Reply:
        """Mid-report they tell us they clicked / typed their password: P1 and Security straight away."""
        st = self._st
        st["triage"]["flags"]["active_harm"] = max(st["triage"]["flags"].get("active_harm", 0), 0.95)
        self.store.update_ticket(st["ticket_id"], priority="P1", priority_reason=f"security/safety override: {why}")
        self._t("Security", f"upgraded to P1: {why}")
        st["pending_fields"] = []
        st.pop("current_field", None)
        return self._security_handoff("KB-025" if "password" in why.lower() else st.get("kb_id") or "KB-024",
                                      asked=True, urgent_answer=why or "urgent")

    def _gap(self, category_id: str, question: str, best: tuple) -> None:
        """Knowledge gap: a real question no article answered. Reviewed weekly on the Knowledge page."""
        if self._user_text == HIDDEN:
            return
        self.store.execute("INSERT INTO knowledge_gaps (ticket_id,session_id,category_id,question,best_kb,best_score,"
                           "created_at) VALUES (?,?,?,?,?,?,?)",
                           (self._st.get("ticket_id"), self._sid, category_id, redact_inline(question)[:500],
                            best[0], round(best[1] or 0, 3), now()))

    def _save_kb_scores(self, scores: dict[str, float]) -> None:
        """Jev relevance per article, shown as "Suggested KB" on the support form."""
        self._st["triage"] = {**self._st["triage"], "kb_scores": scores}
        self.store.update_ticket(self._st["ticket_id"], triage_json=json.dumps(self._st["triage"], default=str))

    def _kb_trace(self, scores: dict[str, float]) -> str:
        top = sorted(scores.items(), key=lambda x: -x[1])[:4]
        return "KB relevance → " + ", ".join(f"{k} {_pct(v)}" for k, v in top) if top else "no candidates"

    def _ground_and_continue(self, cat: str) -> Reply:
        st = self._st
        tri = st["triage"]
        second = [c for c, p in tri["categories"][1:2] if c not in (cat, OTHER_CATEGORY) and p >= 0.2]
        cands = self.k.kb_for_categories([cat] + second)
        fields = self.k.fields_to_ask(cat)
        g = self.brain.ground(st["issue_context"], cands, fields)
        self._t("Retriever", self._kb_trace(g.kb_scores), g.raw)
        self._save_kb_scores(g.kb_scores)
        best_id, best_p = max(g.kb_scores.items(), key=lambda x: x[1], default=(None, 0))
        if best_p < config.KB_MIN_RELEVANCE:
            self._gap(cat, st["issue_context"], (best_id, best_p))
            return self._escalate(f"No KB article judged relevant (best {_pct(best_p)}) — bot does not guess.")
        kb = self.k.kb[best_id]
        st["kb_id"] = kb.kb_id
        self.store.update_ticket(st["ticket_id"], kb_id=kb.kb_id)

        missing = [f for f in fields if g.field_provided.get(f["Field_Name"], 0) < config.FIELD_PROVIDED_THRESHOLD]
        shown = getattr(self, "_screenshot_error", "")
        if shown:  # never ask for the error message the screenshot already shows
            for f in [f for f in missing if "error" in f["Field_Name"].lower()]:
                missing.remove(f)
                st["answers"][f["Field_Name"]] = shown
                self._t("Understand", f"{f['Field_Name']} taken from the screenshot")
        for f in list(missing):  # never ask a signed-in employee for their own ID, username or asset tag
            known = profile_answer(f, self._employee)
            if known:
                missing.remove(f)
                st["answers"][f["Field_Name"]] = known
                self._t("Understand", f"{f['Field_Name']} taken from the employee profile")
        if g.field_provided:
            given = [f for f, p in g.field_provided.items() if p >= config.FIELD_PROVIDED_THRESHOLD]
            self._t("Understand", f"details already given: {given or 'none'}; will ask: "
                                  f"{[f['Field_Name'] for f in missing[:config.MAX_CLARIFYING_QUESTIONS]]}")
        st["pending_fields"] = missing[:config.MAX_CLARIFYING_QUESTIONS]
        if kb.tools and "TOOL-10" in kb.tools:
            res = tools.asset_lookup(self._employee)
            st["tool_results"].append(res)
            if res.get("found"):
                self.store.update_ticket(st["ticket_id"], config_item=res["asset_tag"])
            self._t("Tool", f"TOOL-10 asset lookup → {res.get('asset_tag', 'not found')}", res)
        return self._next_field_or_act()

    # ================================================================ collecting details
    def _next_field_or_act(self) -> Reply:
        st = self._st
        status = self.tickets.get(st["ticket_id"])["status"]
        if not st.get("pending_fields") and st.get("security_report"):
            return self._security_handoff(st.get("kb_id") or "KB-024", asked=True)
        if st.get("pending_fields"):
            f = st["pending_fields"].pop(0)
            st["current_field"] = f
            st["stage"] = "COLLECTING"
            if status == "CLASSIFYING":
                self._to("COLLECTING_INFORMATION", "Category/scope not complete")
            self._to("WAITING_FOR_USER", "Question sent")
            ask, opts = field_question(f)
            first = st.get("asked_on") != st["ticket_id"]
            st["asked_on"] = st["ticket_id"]
            lead = ("To point you to the right fix, I need a couple of details first.\n\n" if first
                    else "Thanks. ")
            return Reply(f"{lead}**{ask}**", quick_replies=opts)
        if status in ("CLASSIFYING", "COLLECTING_INFORMATION"):
            self._to("READY_FOR_RESOLUTION", "Required context complete")
        return self._act()

    def _on_field_answer(self, message: str) -> Reply:
        st = self._st
        flags = self.brain.guard(message)
        self._apply_guard(flags)
        if st.get("security_report") and (message in _SECURITY_HARM_ANSWERS or self._risky(flags, "security_incident")):
            return self._security_escalate_now(message)  # same ticket, now urgent: never a second ticket
        if self._risky(flags, "security_incident") or self._risky(flags, "physical_safety"):
            st["stage"] = "IDLE"
            return self._triage(message)
        if message == TYPE_OWN:  # same question stays open for their own words
            return Reply("Sure, please type it as close as you can to what you see on screen. A screenshot "
                         "works too: use the **+** button.")
        f = st.pop("current_field", None)
        self._to("COLLECTING_INFORMATION", "Answer received")
        if f:
            value = self._user_text
            st["answers"][f["Field_Name"]] = value
            st["issue_context"] += f"\n{f['Help_Text']} {value}"
            self._t("Understand", f"collected {f['Field_Name']}")
        return self._next_field_or_act()

    # ================================================================ solve
    def _act(self) -> Reply:
        kb = self.k.kb[self._st["kb_id"]]
        if kb.is_handoff:
            return self._handoff_article(kb)
        if kb.is_verified_tool_action:
            return self._verified_action(kb, attempt=1)
        return self._attempt(1)

    def _handoff_article(self, kb) -> Reply:
        """Articles a person must handle. Employees get the approved explanation of what happens next; the
        article's own steps are written for agents, so they are never shown."""
        ev = self.kbs.live(kb.kb_id)
        tip = "\n\n".join(filter(None, [ev.get("summary"), ev.get("handoff_message")])) if ev else ""
        return self._escalate(f"{kb.kb_id} handling mode is '{kb.handling_mode}'.", tip)

    def _employee_steps(self, kb, n: int) -> tuple[str, str, str]:
        """(who, text for the chat, plain text) for attempt n. From the approved employee version when there
        is one; otherwise the article's own attempt with tool IDs and agent wording removed."""
        ev = self.kbs.live(kb.kb_id)
        step = ev.get(f"attempt{n}") if ev else None
        if step:
            if step.get("who") == "it":
                return "it", step.get("note", ""), step.get("note", "")
            body = f"{step.get('intro') or 'Here is what to try:'}\n\n{_numbered(step['steps'])}"
            if step.get("why"):
                body += f"\n\n_Why this helps: {step['why']}_"
            return "you", body, " ".join(step["steps"])
        action = user_voice((kb.attempt_1 if n == 1 else kb.attempt_2) or "")
        return "you", f"Here is what to try:\n\n1. {action}.", action

    def _rate(self, bot_answer_id: int | None) -> dict:
        """Message meta that lets the employee rate this answer (👍 / 👎)."""
        return {"bot_answer_id": bot_answer_id, "rate": True} if bot_answer_id else {}

    def _attempt(self, n: int) -> Reply:
        st = self._st
        kb = self.k.kb[st["kb_id"]]
        action = kb.attempt_1 if n == 1 else kb.attempt_2
        if n == 2 and not action:
            fb = ATTEMPT_2_FALLBACK.get(kb.kb_id)
            if fb and self.k.kb[fb].is_verified_tool_action:
                self._t("Solve", f"{kb.kb_id} has no second path → playbook fallback {fb}")
                return self._verified_action(self.k.kb[fb], attempt=2)
            return self._escalate(f"{kb.kb_id} has no second supported attempt.")
        if not action:
            return self._handoff_article(kb)

        if n == 1 and st["category_id"] in tools._SERVICE_FOR_CATEGORY:
            health = tools.check_service_health(st["category_id"])
            st["tool_results"].append(health)
            self._t("Tool", f"TOOL-02 service health → {health['service']} {health['status']}", health)
            if health["status"] != "operational":
                return self._escalate(
                    f"Known service incident {health['incident_id']} — linked instead of troubleshooting.",
                    f"Good news, it's not you: there's a known **{health['service']}** problem right now "
                    f"({health['incident_id']}) and the team is already working on it. I've linked your ticket, "
                    "so you'll hear as soon as it's fixed.")

        # Pre-checks in the article ("Confirm scope…", "Check AD lockout…") are agent notes: never shown here
        who, body, plain = self._employee_steps(kb, n)
        ev = self.kbs.live(kb.kb_id)
        if who == "it":  # only IT can do this step: explain what happens next instead of instructing
            self._t("Solve", f"attempt {n} of {kb.kb_id} is a team action → hand to the team")
            lead = (ev["summary"] if ev and n == 1
                    else "Thanks for trying that. The next step is one for our IT team:" if n == 2 else "")
            return self._escalate(f"{kb.kb_id} attempt {n} needs a team action: {action}",
                                  f"{lead}\n\n{body}".strip())
        if n == 1:
            opener = ev["summary"] if ev else "Let's get this sorted."
            closing = ("**Did that fix it?**\n\n_If not, I have one more thing to try before bringing in someone "
                       "from IT._")
        else:
            opener = "Thanks for trying that. Here's one more thing to try."
            closing = ("**Did that fix it?**\n\n_If not, I'll pass this to the IT team with everything we've tried, "
                       "so you won't need to explain it again._")
        text = f"{opener}\n\n{body}\n\n{closing}"
        st["steps_tried"].append({"attempt": n, "kb": kb.kb_id, "action": action,
                                  "kb_version": ev["version"] if ev else None})
        st.update(attempt=n, stage="VALIDATING", last_instructions=plain, last_display=body)
        self._to(f"ATTEMPT_{n}", "Safe grounded action selected" if n == 1 else "Different supported path",
                 attempts=n)
        self._to(f"WAITING_FOR_VALIDATION_{n}", "Action delivered")
        aid = self._record_answer(kb, n, plain, ticket_id=st["ticket_id"], kb_version=ev["version"] if ev else None)
        self._t("Solve", f"attempt {n}: {action}" + (f" (employee version v{ev['version']})" if ev else ""))
        return Reply(text, quick_replies=VALIDATION_REPLIES, meta={**self._rate(aid), "attempt": n})

    def _verified_action(self, kb, attempt: int) -> Reply:
        st = self._st
        label, fn = tools.VERIFIED_ACTIONS.get(kb.kb_id, (None, None))
        if not fn:  # KB text mentions a tool the registry does not allow -> never execute (SEC-09)
            return self._escalate(f"{kb.kb_id} requires a tool action that isn't in the approved registry.")
        if attempt == 1:
            self._to("VERIFICATION_PENDING", "Sensitive action requires step-up", verification="V2 step-up")
        else:
            self._to("ATTEMPT_2", "Different supported path (verified tool)", attempts=2, verification="V2 step-up")
        ver = tools.verify_identity(self._employee, label)
        st["tool_results"].append(ver)
        self._t("Tool", f"TOOL-03 identity verification → {ver['status']}", ver)
        if ver["status"] != "VERIFIED":
            return self._escalate("Step-up verification failed or unavailable.")
        if attempt == 1:
            self._to("READY_FOR_RESOLUTION", "Verified and unexpired", verification="V2 VERIFIED")
            self._to("ATTEMPT_1", "Safe grounded action selected", attempts=1)
        res = fn(self._employee, ver)
        st["tool_results"].append(res)
        self._t("Tool", f"{res['tool']} {label} → {res['status']}", res)
        if res["status"] != "SUCCESS":
            return self._escalate(f"{res['tool']} failed: {res.get('reason')}")
        extra = res.get("delivery") or res.get("next") or ""
        action = f"{label} via {res['tool']} (ref {res['correlation_id']})"
        st["steps_tried"].append({"attempt": attempt, "kb": kb.kb_id, "action": action})
        st.update(attempt=attempt, stage="VALIDATING", last_instructions=f"{label} was completed. {extra}")
        self._to(f"WAITING_FOR_VALIDATION_{attempt}", "Action executed", verification="V2 VERIFIED")
        self._record_answer(kb, attempt, action, ticket_id=st["ticket_id"])
        # one clear sequence: what we checked, what was done, what to do next. (The article summary is written
        # *before* approval, "once you've approved…", so it isn't repeated here.)
        lead = "Thanks for trying that. " if attempt == 2 else ""
        demo = " _(In this demo the approval is simulated.)_" if ver.get("simulated", True) else ""
        nxt = _AFTER_ACTION.get(kb.kb_id, "Please try again now.")
        return Reply(f"{lead}To keep your account safe, I sent a sign-in request to your registered authenticator "
                     f"app, and it was approved ✅.{demo}\n\n**{label} is done.** {extra.capitalize()}"
                     f"{'.' if extra else ''} Reference: `{res['correlation_id']}`.\n\n**Next:** {nxt}\n\n"
                     "**Did that work?**", quick_replies=VALIDATION_REPLIES)

    # ================================================================ wrap up
    def _on_validation(self, message: str) -> Reply:
        st = self._st
        if message in VALIDATION_REPLIES:  # button press: no model call needed
            outcome = {FIXED: "fixed", NOT_FIXED: "not_fixed", HELP: "needs_help", HUMAN: "wants_human"}[message]
            self._t("Wrap up", f"button → {outcome}")
        else:
            wu = self.brain.wrap_up(st.get("last_instructions", ""), message)
            self._t("Wrap up", f"outcome={wu.outcome} (confidence {_pct(wu.confidence)})", wu.raw)
            self._apply_guard(wu.flags)
            if self._risky(wu.flags, "security_incident") or self._risky(wu.flags, "physical_safety"):
                st["stage"] = "IDLE"
                return self._triage(message)
            outcome = wu.outcome if wu.confidence >= 0.4 else "needs_help"
        if outcome in ("fixed", "not_fixed"):
            self._answer_outcome(outcome)

        if outcome == "fixed":
            self._to("RESOLVED_PENDING_CONFIRMATION", "Success confirmed", resolution_code="Solved by bot",
                     resolution_notes=f"Fixed on attempt {st.get('attempt')} ({st.get('kb_id')})", resolved_at=now())
            st["stage"] = "IDLE"
            self._t("Wrap up", "resolved; summary saved")
            return Reply(f"That's great news! 🎉 I've marked **{st['ticket_id']}** as fixed. If it comes back in "
                         "the next 7 days, just tell me here or press **Reopen** in My tickets.")
        if outcome == "not_fixed":
            if st.get("attempt", 0) < config.MAX_ATTEMPTS:
                return self._attempt(st["attempt"] + 1)
            return self._escalate(f"{config.MAX_ATTEMPTS} supported attempts failed.")
        if outcome == "needs_help":
            st["help_resends"] = st.get("help_resends", 0) + 1
            if st["help_resends"] > 1:
                return self._escalate("User needed help following the steps twice.")
            steps = st.get("last_display") or f"**{st.get('last_instructions', '')}**"
            return Reply(f"No problem, let's take it slowly. Here are the steps again:\n\n{steps}\n\nWhich step "
                         "are you stuck on? Tell me what you see on your screen (a screenshot helps), or I can "
                         "bring in a person.", quick_replies=VALIDATION_REPLIES)
        if outcome == "wants_human":
            return self._escalate("User asked for a human.")
        # new_issue: the current ticket stays waiting for validation; start fresh
        st["stage"] = "IDLE"
        return self._triage(message)

    # ---------------------------------------------------------------- bot answers log (spec §8.2)
    def _record_answer(self, kb, attempt: int, steps: str, ticket_id: str | None, outcome: str | None = None,
                       kb_version: int | None = None) -> int:
        return self.store.insert("INSERT INTO bot_answers (ticket_id,session_id,kb_id,category_id,attempt,steps_text,"
                                 "outcome,created_at,kb_version) VALUES (?,?,?,?,?,?,?,?,?)",
                                 (ticket_id, self._sid, kb.kb_id, kb.category_id, attempt, steps, outcome, now(),
                                  kb_version))

    def _answer_outcome(self, outcome: str) -> None:
        self.store.execute("UPDATE bot_answers SET outcome=?, outcome_at=? WHERE id=(SELECT MAX(id) FROM "
                           "bot_answers WHERE ticket_id=? AND outcome IS NULL)",
                           (outcome, now(), self._st.get("ticket_id")))

    # ================================================================ escalation
    def _escalate(self, reason: str, user_prefix: str = "") -> Reply:
        st = self._st
        if not st.get("ticket_id"):
            self._open_ticket(OTHER_CATEGORY)
        t = self.tickets.get(st["ticket_id"])
        handoff = {
            "ticket_id": t["ticket_id"], "requester": self._employee.get("Employee_ID"),
            "location": self._employee.get("Location"), "channel": t["channel"],
            "category": self.k.name(t["category_id"]), "sub_agent": t["sub_agent"], "priority": t["priority"],
            "priority_reason": t["priority_reason"], "impact": t["impact"], "urgency": t["urgency"],
            "original_statement": t["summary"], "kb_id": st.get("kb_id"),
            "details_collected": st.get("answers", {}), "form_answers": st.get("form"),
            "steps_tried": st.get("steps_tried", []), "tool_results": st.get("tool_results", []),
            "verification": t["verification"],
            "risk_flags": {k: round(v, 2) for k, v in st.get("triage", {}).get("flags", {}).items()
                           if v >= config.RISK_FLAG_THRESHOLD},
            "incident_parent": t["incident_parent"], "escalation_reason": reason,
        }
        self._answer_outcome("escalated")
        if t["status"] in ("ESCALATION_QUEUED", "HUMAN_ASSIGNED", "HUMAN_IN_PROGRESS"):
            self.store.update_ticket(t["ticket_id"], escalation_reason=reason, handoff_json=json.dumps(handoff))
        else:
            t = self.tickets.escalate(t["ticket_id"], reason, handoff)
        # the handoff itself (with the employee's words) is on the ticket; the audit log keeps what, not the text
        self._t("Escalation", f"{t['queue']} · {reason}", {"handoff_fields": sorted(k for k, v in handoff.items() if v)})
        st["stage"] = "ESCALATED"
        return Reply(f"{user_prefix}\n\n{self._handoff_summary(t)}".strip())

    def _handoff_summary(self, t: dict) -> str:
        """What the employee needs after a hand-off: who has it, the reference, and an exact time."""
        sla = self.tickets.sla_status(t)
        eta = sla.get("response_eta")
        lines = [f"I've passed this to the **{t['queue']}** team.", "",
                 f"- **Ticket:** {t['ticket_id']}",
                 f"- **Priority:** {t['priority']} ({PRIORITY_WORD.get(t['priority'], 'normal')})"]
        if eta:
            lines.append(f"- **First reply expected:** {eta}")
        note = ""
        if sla.get("clock") == "24x7":
            note = "_This is handled around the clock, including evenings and weekends._"
        elif eta and "today" not in eta:
            note = f"_The team works {bh.working_hours_text()}, so that's their next working time._"
        body = "\n".join(lines) + (f"\n\n{note}" if note else "")
        return (f"{body}\n\nThey'll see this whole conversation and everything we've tried, so you won't need to "
                "explain it again. Their reply will appear right here and under **My tickets**.")

    def _fallback(self, error: str, message: str) -> Reply:
        """Prompt_Model_Contracts 'Fallback': no fabricated result, deterministic safe route + supervisor alert."""
        self._t("Fallback", f"model unavailable: {error}")
        hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
        self.alerts.system_alert(f"jev_outage:{hour}", f"Jev unavailable ({error[:80]}) — tickets routed to humans")
        st = self._st
        st.setdefault("pending_issue", message)
        st.setdefault("triage", {"impact": "limited", "urgency": "medium", "flags": {}, "categories": []})
        t = self.store.get_ticket(st.get("ticket_id") or "")
        if not t or st.get("stage") in ("IDLE", "ESCALATED") or t["status"] not in (
                "CLASSIFYING", "COLLECTING_INFORMATION", "READY_FOR_RESOLUTION", "WAITING_FOR_USER",
                "WAITING_FOR_VALIDATION_1", "WAITING_FOR_VALIDATION_2"):
            self._open_ticket(OTHER_CATEGORY)
        return self._escalate("Decision model unavailable — safe route to human.",
                              "I'm having trouble on my side right now, so rather than keep you waiting, I'm "
                              "handing this straight to a person.")

    # ================================================================ ticket form (spec §6.3, §9)
    def review_form(self, employee_id: str, form: dict) -> dict:
        """Review step: code mapping for impact/urgency + one Jev check_form call. Nothing is created yet.
        The review is kept server-side in a fresh session, so Submit can't be tampered with."""
        sid = f"form-{employee_id}-{uuid.uuid4().hex[:8]}"
        self._begin(sid, employee_id)
        chosen = form.get("category_id") or "NOT_SURE"
        form = {**form, "category_name": self.k.name(chosen) if chosen in self.k.categories else "Not sure"}
        cands = self.k.kb_for_categories([chosen]) if chosen in self.k.categories else []
        impact, urgency = self.k.form_impact_urgency(form.get("impact_choice", ""), form.get("workaround", ""))
        degraded = None
        try:
            fc = self.brain.check_form(form, cands, ask_impact=impact is None)
        except BrainError as e:  # safe route: no model judgments, the ticket will go straight to a person
            degraded = str(e)
            self.store.log_event("Fallback", {"detail": f"model unavailable on form review: {e}"}, session_id=sid,
                                 actor="bot")
            self.alerts.system_alert(f"jev_outage:{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H')}",
                                     f"Jev unavailable ({degraded[:80]}) — tickets routed to humans")
            fc = FormCheck([(OTHER_CATEGORY, 0.0)], 0.0, {}, {}, None, raw={"error": degraded})
        kb_scores = dict(fc.kb_scores)
        jev_cat, jev_conf = fc.category, fc.category_confidence
        suggestion = None
        confident = not degraded and jev_cat != OTHER_CATEGORY and jev_conf >= config.CATEGORY_MIN_CONFIDENCE
        if confident and jev_cat != chosen:
            if chosen in self.k.categories:
                suggestion = jev_cat  # D3: suggest only, never auto-correct ("Not sure" just takes Jev's pick)
            extra = self.k.kb_for_categories([jev_cat])
            try:
                kb_scores.update(self.brain.ground(form_text(form), extra, []).kb_scores)
            except BrainError:
                pass
        flags = fc.flags
        impact = impact or fc.impact or "limited"
        harm = self._risky(flags, "active_harm") or self._risky(flags, "physical_safety") or \
            self._risky(flags, "security_incident")
        if harm:
            urgency = "critical"
        final_cat = chosen if chosen in self.k.categories else (jev_cat if jev_conf >= config.CATEGORY_MIN_CONFIDENCE
                                                                else OTHER_CATEGORY)
        if self._risky(flags, "security_incident"):
            final_cat = "CAT-12" if final_cat == "CAT-12" else "CAT-09"
        elif self._risky(flags, "physical_safety"):
            final_cat = "CAT-04"
        prio, why = self.k.compute_priority(impact, urgency, flags, final_cat)
        masked = {}
        if self._risky(flags, "shared_secret"):
            for key in ("short_description", "exact_error"):
                if form.get(key):
                    masked[key] = mask_secrets(form[key])
            form = {**form, **masked}
        try_first = None
        for kb_id, p in sorted(kb_scores.items(), key=lambda x: -x[1]):
            kb = self.k.kb[kb_id]
            if p >= config.KB_MIN_RELEVANCE and kb.attempt_1 and not kb.is_handoff and not harm and \
                    not kb.is_verified_tool_action:
                who, _body, plain = self._employee_steps(kb, 1)
                if who != "you":
                    continue
                ev = self.kbs.live(kb_id)
                try_first = {"kb_id": kb_id, "title": ev["title"] if ev else kb.title, "step": plain,
                             "steps": ev["attempt1"]["steps"] if ev else [plain], "relevance": p}
                break
        dup = self.tickets.open_duplicate(employee_id, final_cat) if final_cat != OTHER_CATEGORY else None
        review = {"review_id": sid, "form": form, "chosen_category": chosen, "final_category": final_cat,
                  "suggested_category": suggestion, "jev_category": jev_cat, "jev_confidence": jev_conf,
                  "impact": impact, "urgency": urgency, "priority": prio, "priority_reason": why,
                  "flags": flags, "kb_scores": kb_scores, "masked": masked, "try_first": try_first,
                  "duplicate": {"ticket_id": dup["ticket_id"], "summary": dup["summary"],
                                "label": employee_label(dup, self.tickets.user_name(dup["owner"]))} if dup else None,
                  "degraded": degraded, "raw": fc.raw}
        self.store.save_session(sid, employee_id, {"stage": "FORM_REVIEW", "channel": "Form", "review": review,
                                                   "employee_id": employee_id})
        self.store.log_event("form_preview", {"detail": f"{self.k.name(final_cat)} · {prio} ({why})"
                                              + (f" · suggests {self.k.name(suggestion)}" if suggestion else ""),
                                              "flags": {k: round(v, 2) for k, v in flags.items()}},
                             session_id=sid, actor=employee_id)
        public = {k: v for k, v in review.items() if k not in ("raw", "kb_scores", "flags")}
        public["screenshot_note"] = form.get("screenshot_note")
        public["warnings"] = self._form_warnings(flags, masked) + (
            ["The assistant is unavailable right now, so this ticket will go straight to a person."]
            if degraded else [])
        public["category_name"] = self.k.name(final_cat)
        public["suggested_category_name"] = self.k.name(suggestion) if suggestion else None
        return public

    def _form_warnings(self, flags: dict, masked: dict) -> list[str]:
        w = []
        if masked:
            w.append("🔒 Your description seemed to contain a password or code. We've hidden it. Please change "
                     "that password if it's real.")
        if self._risky(flags, "physical_safety"):
            w.append("⚠️ Stop using the device now: unplug it, don't charge it, keep it away from people.")
        if self._risky(flags, "security_incident"):
            w.append("🛡️ This looks like a security incident. Don't click anything else; Security Operations "
                     "will be alerted as soon as you submit.")
        if self._risky(flags, "manipulation"):
            w.append("Requests to skip verification or approval are ignored; the standard process applies.")
        return w

    def submit_form(self, employee_id: str, review_id: str, accept_suggestion: bool = False,
                    duplicate_of: str | None = None) -> dict:
        """Submit → policy gates (same as chat) → escalated, or the user chooses Fix now / Just log (D2)."""
        st = self.store.get_session(review_id) if review_id else None
        if not st or st.get("employee_id") != employee_id or st.get("stage") != "FORM_REVIEW":
            raise ValueError("Review expired or not yours — review the form again")
        with self._session_lock(review_id):
            turn = copy.copy(self)
            return turn._submit(review_id, employee_id, st, accept_suggestion, duplicate_of)

    def _submit(self, sid, employee_id, st, accept_suggestion, duplicate_of) -> dict:
        self._begin(sid, employee_id)
        self._st = st
        rv = st["review"]
        form = rv["form"]
        if duplicate_of and rv.get("duplicate") and rv["duplicate"]["ticket_id"] == duplicate_of:
            dup = self.tickets.get(duplicate_of)
            self.store.add_message(dup["session_id"] or sid, "user", f"(from the ticket form) "
                                   f"{form['short_description']}", ticket_id=duplicate_of, author=employee_id)
            if dup["owner"]:
                self.notify.notify(dup["owner"], duplicate_of, f"Employee added more detail to {duplicate_of}.")
            st["stage"] = "DONE"
            self.store.save_session(sid, employee_id, st)
            return {"outcome": "added_to_existing", "ticket_id": duplicate_of,
                    "message": f"Added to {duplicate_of} instead of opening a new ticket."}
        cat = rv["final_category"]
        if accept_suggestion and rv.get("suggested_category") and not self._risky(rv["flags"], "security_incident"):
            cat = rv["suggested_category"]
        st.update(stage="IDLE", form=form, answers=dict(form.get("details") or {}),
                  pending_issue=form["short_description"], issue_context=form_text(form),
                  triage={"intent": "form", "impact": rv["impact"], "urgency": rv["urgency"], "flags": rv["flags"],
                          "categories": [[rv["jev_category"], rv["jev_confidence"]]],
                          "category_confidence": rv["jev_confidence"], "kb_scores": rv["kb_scores"]})
        self._user_text = None
        extra = {"subcategory": form.get("issue_type") if form.get("issue_type") != "Other" else None,
                 "form_json": json.dumps(form), "user_category": rv["chosen_category"]}
        if form.get("details", {}).get("asset_tag"):
            extra["config_item"] = form["details"]["asset_tag"]
        self._open_ticket(cat, **extra)
        tid = st["ticket_id"]
        for att_id in form.get("attachment_ids") or []:  # screenshots from the form belong to the new ticket
            self.store.execute("UPDATE attachments SET ticket_id=? WHERE id=? AND uploaded_by=? AND ticket_id IS NULL",
                               (tid, att_id, employee_id))
        self.store.add_message(sid, "user", f"📝 Ticket form: **{form['short_description']}**"
                               + (f"\n\nError: {form['exact_error']}" if form.get("exact_error") else ""),
                               ticket_id=tid, author=employee_id)
        if rv.get("suggested_category") and cat != rv["suggested_category"]:
            self._t("Category check", f"user kept {self.k.name(cat)}; Jev suggested "
                                      f"{self.k.name(rv['suggested_category'])} (stored, not applied — D3)")
        reply = None
        if rv.get("degraded"):
            reply = self._escalate("Decision model unavailable — safe route to human.")
        elif cat == OTHER_CATEGORY:
            reply = self._escalate("Category Not sure / unclear on the form — goes to a human.")
        else:
            reply = self._gates(cat, ask_incident=False)  # the form can't ask mid-submit: link only on multi-user
        if reply is None:
            best = max(((k, p) for k, p in rv["kb_scores"].items() if self.k.kb[k].category_id == cat),
                       key=lambda x: x[1], default=(None, 0))
            if best[1] < config.KB_MIN_RELEVANCE:
                reply = self._escalate(f"No KB article judged relevant (best {_pct(best[1])}) — bot does not guess.")
            else:
                st["kb_id"] = best[0]
                self.store.update_ticket(tid, kb_id=best[0])
                kb = self.k.kb[best[0]]
                if kb.is_handoff:
                    self._to("READY_FOR_RESOLUTION", "Required context complete (form)")
                    reply = self._handoff_article(kb)
                else:
                    self._to("READY_FOR_RESOLUTION", "Required context complete (form)")
                    st["stage"] = "FORM_CHOICE"
                    self._t("Form", f"resolvable with {best[0]} ({_pct(best[1])}) → user chooses Fix now / Just log")
        if st["stage"] == "FORM_CHOICE":
            self.store.save_session(sid, employee_id, st)
            t = self.tickets.get(tid)
            return {"outcome": "choose", "ticket_id": tid, "session_id": sid, "priority": t["priority"],
                    "message": f"{tid} created. The assistant can walk you through a fix now "
                               f"({self.k.kb[st['kb_id']].title}), or just log it for the team."}
        self._finish(reply, record_user=False)
        t = self.tickets.get(tid)
        return {"outcome": "escalated", "ticket_id": tid, "session_id": sid, "priority": t["priority"],
                "queue": t["queue"], "message": reply.text}

    def form_choice(self, employee_id: str, ticket_id: str, choice: str) -> dict:
        t = self.tickets.get(ticket_id)
        if t["employee_id"] != employee_id:
            raise PermissionError("Not your ticket")
        sid = t["session_id"]
        with self._session_lock(sid):
            turn = copy.copy(self)
            st = turn._begin(sid, employee_id)
            if st.get("stage") != "FORM_CHOICE":
                raise ValueError("This ticket is no longer waiting for a choice")
            turn._user_text = None
            if choice == "fix_now":
                turn.store.add_message(sid, "system", "You chose **Fix it now**.", ticket_id=ticket_id)
                reply = turn._act()  # starts at attempt 1: the form already captured the details
            else:
                reply = turn._escalate("Employee chose Just log on the form.")
            turn._finish(reply, record_user=False)
            return {"session_id": sid, "reply": reply.text, "quick_replies": reply.quick_replies,
                    "ticket_id": ticket_id, **_reply_extras(reply)}


ServiceDesk = ConversationService  # Phase 1 name, kept for evaluate.py and older scripts
