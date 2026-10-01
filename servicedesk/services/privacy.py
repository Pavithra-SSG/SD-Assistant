"""PrivacyService: retention and "my data" (India's DPDP Act, 2023: keep personal data no longer than needed,
let people see what's held about them).

Retention (run daily by the worker): for tickets that finished more than RETENTION_MONTHS ago, the personal
text is removed — conversation messages, the ticket summary, form answers, handoff package, feedback and
rating comments, gap questions — while the facts stay (ticket number, dates, category, priority, SLA, who
worked on it), so reports keep working and nothing is deleted outright (D9). The audit log holds no message
text (migration 4), so it needs no redaction. Screenshots have their own, shorter retention.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .. import config
from ..store import Store, now

REMOVED = "[removed after the retention period]"
FINAL = ("RESOLVED", "CLOSED", "CANCELLED")


def _months_ago(months: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=30 * months)).isoformat(timespec="seconds")


class PrivacyService:
    def __init__(self, store: Store):
        self.store = store

    def run_retention(self, months: int | None = None) -> dict:
        cutoff = _months_ago(months or config.RETENTION_MONTHS)
        marks = ",".join("?" * len(FINAL))
        rows = self.store.query(f"SELECT ticket_id, session_id FROM tickets WHERE status IN ({marks}) AND "
                                f"updated_at<? AND redacted_at IS NULL", (*FINAL, cutoff))
        for t in rows:
            tid, sid = t["ticket_id"], t["session_id"]
            with self.store.tx() as c:
                c.execute("UPDATE messages SET text=?, meta_json='{}' WHERE ticket_id=?", (REMOVED, tid))
                if sid:  # chat turns from before the ticket existed, in the same conversation
                    c.execute("UPDATE messages SET text=?, meta_json='{}' WHERE session_id=? AND ticket_id IS NULL",
                              (REMOVED, sid))
                    c.execute("UPDATE sessions SET state_json='{}', redacted_at=? WHERE session_id=?", (now(), sid))
                c.execute("UPDATE tickets SET summary=?, handoff_json=NULL, form_json=NULL, triage_json=NULL, "
                          "resolution_notes=?, redacted_at=? WHERE ticket_id=?", (REMOVED, REMOVED, now(), tid))
                c.execute("UPDATE answer_feedback SET comment=NULL WHERE ticket_id=?", (tid,))
                c.execute("UPDATE ticket_ratings SET comment=NULL WHERE ticket_id=?", (tid,))
                c.execute("UPDATE knowledge_gaps SET question=? WHERE ticket_id=?", (REMOVED, tid))
                # bot_answers hold approved KB text, not personal text: kept for the success-rate reports
        # time-based text outside tickets
        n_notes = self.store.execute("UPDATE notifications SET text=? WHERE created_at<? AND text<>?",
                                     (REMOVED, cutoff, REMOVED))
        n_gaps = self.store.execute("UPDATE knowledge_gaps SET question=? WHERE created_at<? AND question<>?",
                                    (REMOVED, cutoff, REMOVED))
        n_outbox = self.store.execute("UPDATE outbox SET body=?, subject=NULL WHERE created_at<? AND body<>?",
                                      (REMOVED, cutoff, REMOVED))
        result = {"tickets": len(rows), "notifications": n_notes, "gap_questions": n_gaps, "emails": n_outbox,
                  "cutoff": cutoff}
        if any(v for k, v in result.items() if k != "cutoff"):
            self.store.log_event("Retention", {"detail": f"personal text removed from {len(rows)} ticket(s) "
                                                         f"closed before {cutoff[:10]}", **result}, actor="system")
        return result

    def export(self, employee_id: str) -> dict:
        """Everything held about one employee that they may see (no internal work notes or bot scores)."""
        u = self.store.one("SELECT user_id, name, email, role, job_title, department, location, asset_tag, "
                           "created_at, status FROM users WHERE user_id=?", (employee_id,))
        tickets = self.store.query("SELECT ticket_id, created_at, updated_at, status, category_id, priority, summary, "
                                   "channel, resolved_at, resolution_code FROM tickets WHERE employee_id=? "
                                   "ORDER BY created_at", (employee_id,))
        sessions = [r["session_id"] for r in self.store.query("SELECT session_id FROM sessions WHERE employee_id=?",
                                                              (employee_id,))]
        messages = []
        for sid in sessions:
            messages += self.store.query("SELECT created_at, role, text, ticket_id FROM messages WHERE session_id=? "
                                         "AND visibility='customer' ORDER BY id", (sid,))
        return {"exported_at": now(), "profile": u, "tickets": tickets, "messages": messages,
                "ratings": self.store.query("SELECT ticket_id, score, comment, created_at FROM ticket_ratings WHERE "
                                            "employee_id=?", (employee_id,)),
                "answer_feedback": self.store.query("SELECT kb_id, helpful, reason, comment, created_at FROM "
                                                    "answer_feedback WHERE employee_id=?", (employee_id,)),
                "screenshots": [dict(r, note="image files are deleted after the retention period")
                                for r in self.store.query("SELECT id, created_at, ticket_id, ocr_text, delete_after, "
                                                          "deleted_at FROM attachments WHERE uploaded_by=?",
                                                          (employee_id,))],
                "retention": {"conversations_months": config.RETENTION_MONTHS,
                              "screenshots_days": config.ATTACHMENT_RETENTION_DAYS},
                "note": "Questions about your data: contact the IT Service Desk or your data protection officer."}
