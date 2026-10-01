"""Background worker: alert timers and ticket sweeps, whether or not anyone has the app open.

Run:  python worker.py            (Docker: the `worker` service)

Every WORKER_INTERVAL_SECONDS it
  * advances alert timers: P1 re-alert every 2 min, supervisor escalation (P1 at 10 min, P2 at half the SLA
    acknowledgement time), SLA-at-risk warnings at 75%
  * sweeps tickets: auto-confirm after 3 days, close after the 7-day reopen window
  * sends queued e-mails and Teams messages from the outbox (retrying failures)
  * writes a heartbeat that the API's /ready endpoint checks, and pings HEALTHCHECK_PING_URL if set (an
    outside "dead man's switch": if the pings stop, that service alerts you, even if the whole server is down)
Once a day it removes expired sign-in sessions (the login audit trail stays in `events`), deletes screenshots
past their retention date, and removes personal text from tickets closed more than RETENTION_MONTHS ago.

Only one worker runs the timers at a time: it holds a lease row in `heartbeats`. A second copy waits and
takes over if the first stops beating for 3 intervals.
"""
from __future__ import annotations

import os
import signal
import socket
import time

import httpx

from servicedesk import config
from servicedesk.knowledge import get_knowledge
from servicedesk.logs import setup
from servicedesk.services.alerts import AlertService
from servicedesk.services.attachments import AttachmentService
from servicedesk.services.auth import AuthService
from servicedesk.services.notify import NotificationService
from servicedesk.services.privacy import PrivacyService
from servicedesk.services.tickets import TicketService
from servicedesk.store import Store, ago, now

log = setup("servicedesk.worker")
ME = f"{socket.gethostname()}:{os.getpid()}"
_stop = False


def _on_signal(signum, _frame):
    global _stop
    log.info("stopping", extra={"signal": signum})
    _stop = True


def take_lease(store: Store, ttl_seconds: int) -> bool:
    """True if this process may run the timers now. Atomic: tx() serialises writers."""
    with store.tx() as c:
        r = c.execute("SELECT beat_at, info FROM heartbeats WHERE name='worker'").fetchone()
        if r and r["info"] != ME and r["beat_at"] >= ago(seconds=ttl_seconds):
            return False  # another worker is alive
        c.execute("INSERT INTO heartbeats (name,beat_at,info) VALUES ('worker',?,?) ON CONFLICT(name) DO UPDATE SET "
                  "beat_at=excluded.beat_at, info=excluded.info", (now(), ME))
    return True


def run_once(alerts: AlertService, tickets: TicketService, notify: NotificationService) -> None:
    alerts.tick(force=True)
    tickets.sweep()
    sent = notify.send_pending()
    if sent["sent"] or sent["failed"]:
        log.info("outbox", extra=sent)


def daily(store: Store, atts: AttachmentService, privacy: PrivacyService) -> None:
    n = store.execute("DELETE FROM auth_sessions WHERE expires_at<?", (ago(days=1),))
    log.info("daily housekeeping", extra={"expired_sessions": n, "screenshots_deleted": atts.purge_expired(),
                                          **privacy.run_retention()})


def ping_healthcheck() -> None:
    if config.HEALTHCHECK_PING_URL:
        try:
            httpx.get(config.HEALTHCHECK_PING_URL, timeout=5)
        except httpx.HTTPError as e:
            log.warning("healthcheck ping failed", extra={"error": str(e)[:200]})


def main() -> None:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    store = Store()
    k = get_knowledge()
    notify = NotificationService(store)
    auth = AuthService(store, k)
    alerts = AlertService(store, k, auth=auth, notify=notify)
    tickets = TicketService(store, k, alerts, notify)
    atts, privacy = AttachmentService(store), PrivacyService(store)
    interval = config.WORKER_INTERVAL_SECONDS
    log.info("worker started", extra={"worker": ME, "interval_s": interval,
                                      "database": "postgres" if store.pg else "sqlite"})
    last_cleanup, leader = 0.0, None
    while not _stop:
        started = time.monotonic()
        try:
            is_leader = take_lease(store, ttl_seconds=3 * interval)
            if is_leader != leader:
                log.info("lease acquired" if is_leader else "standing by: another worker holds the lease",
                         extra={"worker": ME})
                leader = is_leader
            if is_leader:
                run_once(alerts, tickets, notify)
                if time.monotonic() - last_cleanup > 24 * 3600:
                    daily(store, atts, privacy)
                    last_cleanup = time.monotonic()
                ping_healthcheck()
        except Exception:  # noqa: BLE001 - keep the loop alive; the next cycle retries
            log.exception("worker cycle failed")
        # sleep in short steps so SIGTERM stops the worker promptly
        while not _stop and time.monotonic() - started < interval:
            time.sleep(0.5)
    log.info("worker stopped", extra={"worker": ME})


if __name__ == "__main__":
    main()
