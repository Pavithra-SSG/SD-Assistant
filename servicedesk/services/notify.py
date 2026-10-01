"""NotificationService: in-app notifications, plus e-mail and Microsoft Teams through the `outbox` table.

It never decides *who* is alerted about escalations; that is AlertService.

Nothing is sent from a web request. `notify()` writes the in-app notification and, when enabled, an outbox
row; the worker sends outbox rows (retrying failures with back-off), so a slow mail server never slows the
app and nothing is lost if it's down. Each outbox row has a dedupe key, so a retry never sends twice.

E-mails say what happened in one line and link to the app; the conversation itself stays in the app.
"""
from __future__ import annotations

import json
import smtplib
import ssl
from email.message import EmailMessage

from .. import config
from ..store import Store, now

MAX_ATTEMPTS = 5


class NotificationService:
    def __init__(self, store: Store):
        self.store = store

    # ---------------------------------------------------------------- in-app (+ e-mail)
    def notify(self, user_id: str | None, ticket_id: str | None, text: str) -> None:
        if not user_id:
            return
        nid = self.store.insert("INSERT INTO notifications (user_id,ticket_id,text,created_at) VALUES (?,?,?,?)",
                                (user_id, ticket_id, text, now()))
        u = self.store.one("SELECT name, email, role, notify_email, status FROM users WHERE user_id=?", (user_id,))
        wants = u and u["email"] and (u.get("notify_email") in (None, 1)) and (u.get("status") or "active") == "active"
        allowed = config.EMAIL_EMPLOYEES if (u or {}).get("role") == "employee" else config.EMAIL_STAFF
        if wants and allowed and config.SMTP_HOST:
            first = (u["name"] or "there").split()[0]
            subject = f"[{ticket_id}] Update on your IT request" if ticket_id else "IT Service Desk update"
            body = (f"Hi {first},\n\n{text}\n\nOpen the IT Service Desk to see the details or reply:\n"
                    f"{config.SITE_URL}\n\nThis is an automatic message; replies to this e-mail aren't read yet.\n")
            self.queue("email", u["email"], subject, body, ticket_id, f"notif:{nid}")

    def list(self, user_id: str, unread_only: bool = False, limit: int = 30) -> list[dict]:
        sql = "SELECT * FROM notifications WHERE user_id=?" + (" AND read_at IS NULL" if unread_only else "")
        return self.store.query(sql + " ORDER BY id DESC LIMIT ?", (user_id, limit))

    def mark_read(self, user_id: str, ids: list[int] | None = None) -> None:
        if ids:
            marks = ",".join("?" * len(ids))
            self.store.execute(f"UPDATE notifications SET read_at=? WHERE user_id=? AND id IN ({marks})",
                               (now(), user_id, *ids))
        else:
            self.store.execute("UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL",
                               (now(), user_id))

    # ---------------------------------------------------------------- team channel
    def team_alert(self, text: str, ticket_id: str | None, key: str) -> None:
        """A message to the IT team's Teams channel (P1/P2 alerts, supervisor escalations)."""
        if config.TEAMS_WEBHOOK_URL:
            self.queue("teams", "it-channel", None, text, ticket_id, f"teams:{key}")

    # ---------------------------------------------------------------- outbox
    def queue(self, channel: str, recipient: str, subject: str | None, body: str, ticket_id: str | None,
              dedupe_key: str) -> None:
        self.store.execute("INSERT OR IGNORE INTO outbox (channel,recipient,subject,body,ticket_id,dedupe_key,status,"
                           "attempts,created_at) VALUES (?,?,?,?,?,?,'pending',0,?)",
                           (channel, recipient, subject, body, ticket_id, dedupe_key, now()))

    def send_pending(self, limit: int = 20, sender=None) -> dict:
        """Worker: send due outbox rows. Failed rows are retried on later cycles, up to MAX_ATTEMPTS."""
        rows = self.store.query("SELECT * FROM outbox WHERE status='pending' AND attempts<? ORDER BY id LIMIT ?",
                                (MAX_ATTEMPTS, limit))
        sent = failed = 0
        for r in rows:
            try:
                (sender or _send)(r)
            except Exception as e:  # noqa: BLE001 - record and retry later; one bad row must not block others
                attempts = r["attempts"] + 1
                self.store.execute("UPDATE outbox SET attempts=?, last_error=?, status=? WHERE id=?",
                                   (attempts, str(e)[:300], "failed" if attempts >= MAX_ATTEMPTS else "pending",
                                    r["id"]))
                failed += 1
            else:
                self.store.execute("UPDATE outbox SET status='sent', sent_at=?, attempts=attempts+1 WHERE id=?",
                                   (now(), r["id"]))
                sent += 1
        return {"sent": sent, "failed": failed}


def _send(row: dict) -> None:
    if row["channel"] == "email":
        send_email(row["recipient"], row["subject"] or "IT Service Desk", row["body"])
    elif row["channel"] == "teams":
        send_teams(row["body"])
    else:
        raise ValueError(f"unknown channel {row['channel']}")


def send_email(to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = config.SMTP_FROM, to, subject
    msg["Auto-Submitted"] = "auto-generated"  # stops out-of-office loops
    msg.set_content(body)
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as s:
        if config.SMTP_STARTTLS:
            s.starttls(context=ssl.create_default_context())
        if config.SMTP_USER:
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
        s.send_message(msg)


def send_teams(text: str, url: str | None = None) -> None:
    """Teams incoming webhook (or any webhook that accepts {"text": ...}, e.g. Slack)."""
    import httpx
    r = httpx.post(url or config.TEAMS_WEBHOOK_URL, content=json.dumps({"text": text}),
                   headers={"Content-Type": "application/json"}, timeout=15)
    r.raise_for_status()
