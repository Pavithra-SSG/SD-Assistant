"""Watchdog: tells IT when the service desk itself is broken. Run as its own container.

Every WATCHDOG_INTERVAL_SECONDS it checks
  * the API's /ready (database reachable, background worker alive; if the worker is silent, P1 re-alerts
    and supervisor escalations have stopped)
  * the web app's health endpoint
After WATCHDOG_FAILURES consecutive failures it sends one "DOWN" alert, and one "RECOVERED" when all is well
again: to ALERT_WEBHOOK_URL (Teams/Slack incoming webhook) and/or ALERT_EMAIL_TO (SMTP settings as for
notifications). It talks to those directly, not through the database, because the database may be what's down.

It can't report its own death or the whole server going down: for that, set HEALTHCHECK_PING_URL (e.g. a free
healthchecks.io check). The worker pings it every cycle, and that outside service alerts you when pings stop.
"""
from __future__ import annotations

import os
import signal
import time

import httpx

from servicedesk import config
from servicedesk.logs import setup
from servicedesk.services.notify import send_email, send_teams

log = setup("servicedesk.watchdog")
API = os.getenv("WATCHDOG_API_URL", "http://api:8000")
UI = os.getenv("WATCHDOG_UI_URL", "http://ui:8501")
INTERVAL = int(os.getenv("WATCHDOG_INTERVAL_SECONDS", "60"))
FAILURES = int(os.getenv("WATCHDOG_FAILURES", "2"))
_stop = False


def check() -> list[str]:
    """Problems found right now, in plain words (empty list = healthy)."""
    problems = []
    try:
        r = httpx.get(f"{API}/ready", timeout=10)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200:
            if body.get("database") is False:
                problems.append("the database is unreachable")
            if body.get("worker") is False:
                problems.append("the background worker has stopped (P1 re-alerts and escalations are paused)")
            if not problems:
                problems.append(f"the API is not ready (HTTP {r.status_code})")
    except httpx.HTTPError as e:
        problems.append(f"the API is not answering ({type(e).__name__})")
    try:
        httpx.get(f"{UI}/_stcore/health", timeout=10).raise_for_status()
    except httpx.HTTPError as e:
        problems.append(f"the web app is not answering ({type(e).__name__})")
    return problems


def send_alert(subject: str, text: str) -> None:
    sent = False
    if config.ALERT_WEBHOOK_URL:
        try:
            send_teams(f"{subject}\n\n{text}", config.ALERT_WEBHOOK_URL)
            sent = True
        except Exception as e:  # noqa: BLE001 - try the other channel
            log.error("alert webhook failed", extra={"error": str(e)[:200]})
    if config.ALERT_EMAIL_TO and config.SMTP_HOST:
        try:
            send_email(config.ALERT_EMAIL_TO, subject, text)
            sent = True
        except Exception as e:  # noqa: BLE001
            log.error("alert e-mail failed", extra={"error": str(e)[:200]})
    if not sent:
        log.error("no alert channel worked or none is configured", extra={"subject": subject})


class Watch:
    """Turns a stream of check results into one DOWN and one RECOVERED message (no alert storms)."""

    def __init__(self, failures_needed: int = FAILURES):
        self.needed, self.streak, self.down = failures_needed, 0, False

    def observe(self, problems: list[str]) -> tuple[str, str] | None:
        if problems:
            self.streak += 1
            if not self.down and self.streak >= self.needed:
                self.down = True
                return ("🚨 IT Service Desk is DOWN", "Problem: " + "; ".join(problems) +
                        f".\n\nChecked {self.streak} times in a row. Runbook: docs/DEPLOY.md → Monitoring.")
            return None
        self.streak = 0
        if self.down:
            self.down = False
            return "✅ IT Service Desk has RECOVERED", "All checks are passing again."
        return None


def main() -> None:
    def stop(*_):
        global _stop
        _stop = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    watch = Watch()
    log.info("watchdog started", extra={"api": API, "ui": UI, "interval_s": INTERVAL,
                                        "webhook": bool(config.ALERT_WEBHOOK_URL), "email": bool(config.ALERT_EMAIL_TO)})
    while not _stop:
        problems = check()
        if problems:
            log.warning("check failed", extra={"problems": problems})
        msg = watch.observe(problems)
        if msg:
            send_alert(*msg)
        for _ in range(INTERVAL * 2):
            if _stop:
                break
            time.sleep(0.5)


if __name__ == "__main__":
    main()
