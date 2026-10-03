"""AlertService: priority alert rules, re-alerts, supervisor escalation, acknowledgements (spec §8.3).

  P1     modal + red banner + supervisor copy · re-alert every 2 min · unacked 10 min → supervisor
  P2     toast + badge · unacked past ½ SLA acknowledgement time → supervisor
  P3/P4  badge + queue list · SLA-at-risk warning at 75% of the resolve target
  System Jev outage, alert backlog → supervisor banner

Routing: category → routing_matrix.Primary_Queue → logged-in agents in that queue; nobody online →
supervisor. `(ticket_id, event)` is unique, so a ticket can't alert twice for the same event.
`tick()` runs in the background worker (worker.py) in production; in development the API also runs it on
every poll of /alerts/pending (10 s), so the app works without a worker process.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from .. import business_hours as bh
from .. import config
from ..knowledge import Knowledge
from ..store import Store, now, parse_ts

LEVEL = {"P1": "modal", "P2": "toast", "P3": "badge", "P4": "badge"}
SYSTEM = "SYSTEM"


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class AlertService:
    def __init__(self, store: Store, k: Knowledge, auth=None, notify=None):
        self.store, self.k, self.auth, self.notify = store, k, auth, notify
        self._tick_lock = threading.Lock()
        self._last_tick = 0.0

    # ================================================================ raising
    def on_escalation(self, t: dict, event: str = "escalated") -> None:
        tid = t["ticket_id"]
        if t.get("incident_parent") and event == "escalated":
            self.store.log_event("Alert", {"detail": f"suppressed: linked to incident {t['incident_parent']} "
                                                     "(one alert per incident)"}, ticket_id=tid, actor="system")
            return
        prio, queue = t["priority"], t["queue"]
        online = self.auth.online_agents(queue) if self.auth else []
        if event == "sla_risk":  # a warning, not a page: badge only, no re-alert, no supervisor copy
            level, to_sup, why = "badge", False, ""
            text = f"SLA at risk: {t['ticket_id']} ({prio}) has used 75% of its resolve target"
            n = self.store.execute(
                "INSERT OR IGNORE INTO alerts (ticket_id,event,level,priority,target_queue,text,created_at) "
                "VALUES (?,?,?,?,?,?,?)", (tid, event, level, prio, queue, text, now()))
            if n:
                self.store.log_event("Alert", {"detail": text}, ticket_id=tid, actor="system")
            return
        level = LEVEL.get(prio, "badge")
        to_sup = prio == "P1" or not online
        why = ("P1: supervisor copy" if prio == "P1" else "") or ("" if online else f"nobody online in {queue}")
        text = f"{prio} {t['ticket_id']} → {queue}: {(t.get('summary') or '')[:90]}"
        n = self.store.execute(
            "INSERT OR IGNORE INTO alerts (ticket_id,event,level,priority,target_queue,text,created_at,next_alert_at,"
            "escalated_at,escalation_reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (tid, event, level, prio, queue, text, now(),
             _iso(datetime.now(timezone.utc) + timedelta(minutes=config.P1_REALERT_MINUTES)) if prio == "P1" else None,
             now() if to_sup else None, why or None))
        if n:
            target = queue + (" + supervisor" if to_sup else "")
            self.store.log_event("Alert", {"detail": f"{prio} alert ({level}) → {target}" +
                                           ("" if online else " · nobody online in queue"),
                                           "event": event, "online": online}, ticket_id=tid, actor="system")
            if prio in ("P1", "P2") and self.notify:  # the team channel, so it's seen even with the app closed
                # category and queue only: the employee's own words stay in the app
                icon = "🔴" if prio == "P1" else "🟠"
                self.notify.team_alert(f"{icon} {prio} {tid} · {self.k.name(t['category_id'])} → {queue}"
                                       + ("" if online else " (nobody online in this queue)")
                                       + f". Open the service desk: {config.SITE_URL}", tid, f"{tid}:{event}")

    def on_priority_change(self, t: dict) -> None:
        if t["priority"] in ("P1", "P2"):
            self.on_escalation(t, event=f"priority:{t['priority']}")

    def system_alert(self, key: str, text: str) -> None:
        """Jev outage etc. One row per key (e.g. per hour), shown as a supervisor banner."""
        n = self.store.execute("INSERT OR IGNORE INTO alerts (ticket_id,event,level,priority,target_queue,text,"
                               "created_at,escalated_at,escalation_reason) VALUES (?,?,?,?,?,?,?,?,?)",
                               (SYSTEM, key, "system", None, "supervisor", text, now(), now(), "system"))
        if n:
            self.store.log_event("System alert", {"detail": text, "key": key}, actor="system")

    # ================================================================ acknowledging
    def ack(self, alert_id: int, user: dict) -> dict:
        a = self.store.one("SELECT * FROM alerts WHERE alert_id=?", (alert_id,))
        if not a or not self._visible(a, user):
            raise LookupError("Alert not found")
        if not a["acked_at"]:
            self.store.execute("UPDATE alerts SET acked_by=?, acked_at=? WHERE alert_id=? AND acked_at IS NULL",
                               (user["user_id"], now(), alert_id))
            self.store.log_event("Alert acknowledged", {"detail": f"{a['event']} acked by {user['name']}"},
                                 ticket_id=None if a["ticket_id"] == SYSTEM else a["ticket_id"],
                                 actor=user["user_id"])
        return self.store.one("SELECT * FROM alerts WHERE alert_id=?", (alert_id,))

    def ack_ticket(self, tid: str, user_id: str) -> None:
        n = self.store.execute("UPDATE alerts SET acked_by=?, acked_at=? WHERE ticket_id=? AND acked_at IS NULL",
                               (user_id, now(), tid))
        if n:
            self.store.log_event("Alert acknowledged", {"detail": f"{n} alert(s) acked on take-over"},
                                 ticket_id=tid, actor=user_id)

    # ================================================================ timers
    def tick(self, force: bool = False) -> None:
        """Re-alerts, supervisor escalation and SLA-risk warnings. Throttled to once per 5 s."""
        with self._tick_lock:
            if not force and time.monotonic() - self._last_tick < 5:
                return
            self._last_tick = time.monotonic()
        nowdt = datetime.now(timezone.utc)
        for a in self.store.query("SELECT * FROM alerts WHERE acked_at IS NULL AND ticket_id!=?", (SYSTEM,)):
            age = nowdt - parse_ts(a["created_at"])
            if a["priority"] == "P1" and a["next_alert_at"] and parse_ts(a["next_alert_at"]) <= nowdt:
                self.store.execute("UPDATE alerts SET realert_count=realert_count+1, next_alert_at=? "
                                   "WHERE alert_id=?", (_iso(nowdt + timedelta(minutes=config.P1_REALERT_MINUTES)),
                                                        a["alert_id"]))
                self.store.log_event("Alert", {"detail": f"P1 re-alert #{a['realert_count'] + 1} (not acknowledged)"},
                                     ticket_id=a["ticket_id"], actor="system")
            if a["escalation_reason"] in (None, "P1: supervisor copy") and not self._sup_escalated(a):
                limit = None
                if a["priority"] == "P1":
                    limit = timedelta(minutes=config.P1_SUPERVISOR_MINUTES)
                elif a["priority"] == "P2":
                    t = self.store.get_ticket(a["ticket_id"])
                    sla = self.k.sla_target(t["category_id"], "P2") if t else None
                    half = (sla["Acknowledgement_Hours"] if sla else 1) / 2
                    # P2 counts business hours, like its SLA; the timedelta keeps the comparison below uniform
                    age = timedelta(hours=bh.hours_between(parse_ts(a["created_at"]), nowdt, "P2"))
                    limit = timedelta(hours=half)
                if limit and age >= limit:
                    self.store.execute("UPDATE alerts SET escalated_at=?, escalation_reason=? WHERE alert_id=?",
                                       (now(), f"unacknowledged {int(age.total_seconds() // 60)} min", a["alert_id"]))
                    self.store.log_event("Alert", {"detail": f"{a['priority']} not acknowledged in time → supervisor "
                                                             "(logged as SLA risk)"},
                                         ticket_id=a["ticket_id"], actor="system")
                    if self.notify:
                        self.notify.team_alert(f"⏰ {a['priority']} {a['ticket_id']} has not been acknowledged for "
                                               f"{int(age.total_seconds() // 60)} min. Supervisor, please check: "
                                               f"{config.SITE_URL}", a["ticket_id"], f"{a['alert_id']}:supervisor")
        self._sla_risk(nowdt)

    @staticmethod
    def _sup_escalated(a: dict) -> bool:
        return bool(a["escalation_reason"]) and a["escalation_reason"].startswith("unacknowledged")

    def _sla_risk(self, nowdt: datetime) -> None:
        rows = self.store.query("SELECT * FROM tickets WHERE status IN ('ESCALATION_QUEUED','HUMAN_ASSIGNED',"
                                "'HUMAN_IN_PROGRESS')")
        for t in rows:
            sla = self.k.sla_target(t["category_id"], t["priority"])
            created = parse_ts(t["created_at"])
            if not sla or not created:
                continue
            frac = bh.hours_between(created, nowdt, t["priority"], t.get("category_id"), t.get("kb_id")) / sla["Resolution_Target_Hours"]
            if frac >= config.SLA_RISK_FRACTION:
                self.on_escalation({**t, "incident_parent": None}, event="sla_risk")

    # ================================================================ reading
    def _visible(self, a: dict, user: dict) -> bool:
        if user["role"] == "supervisor":
            return True
        if user["role"] != "agent" or a["ticket_id"] == SYSTEM:
            return False
        return a["target_queue"] in user["queues"] or a["priority"] == "P1"

    def pending(self, user: dict) -> dict:
        """What this user should see right now. Supervisors get P1s, anything escalated to them,
        and system alerts as pop-ups; everything else counts toward badges."""
        rows = self.store.query("SELECT a.*, t.status, t.owner, t.summary, t.category_id FROM alerts a "
                                "LEFT JOIN tickets t ON t.ticket_id=a.ticket_id WHERE a.acked_at IS NULL "
                                "ORDER BY a.alert_id DESC")
        mine = [a for a in rows if self._visible(a, user)]
        if user["role"] == "supervisor":
            popup = [a for a in mine if a["priority"] == "P1" or a["escalated_at"] or a["level"] == "system"]
        else:
            popup = mine
        counts = {"P1": 0, "P2": 0, "P3": 0, "P4": 0, "system": 0}
        for a in mine:
            counts[a["priority"] or "system"] = counts.get(a["priority"] or "system", 0) + 1
        return {"alerts": popup, "counts": counts, "total": len(mine),
                "backlog": len(mine) >= config.ALERT_BACKLOG_WARNING and user["role"] == "supervisor",
                "degraded": self.degraded()}

    def degraded(self) -> dict | None:
        """"Model degraded" banner: a Jev failure in the last few minutes."""
        since = _iso(datetime.now(timezone.utc) - timedelta(minutes=config.JEV_OUTAGE_BANNER_MINUTES))
        r = self.store.one("SELECT created_at, payload_json FROM events WHERE event_type='Fallback' AND "
                           "created_at>=? ORDER BY id DESC LIMIT 1", (since,))
        return {"since": r["created_at"]} if r else None
