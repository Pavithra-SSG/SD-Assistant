"""AnalyticsService: read-only aggregates for the charts dashboard, agent performance and shift summary.

It never writes. Every number comes from `tickets`, `events`, `corrections`, `bot_answers`
and `checklists`. Times are UTC.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median

from .. import config
from ..knowledge import Knowledge
from ..store import Store, parse_ts
from .tickets import HUMAN_STATES, TicketService

RESOLVED_CODES_HUMAN = ("Solved by agent", "Workaround given", "Not reproducible")
_SECRET_LEAK = re.compile(r"(?i)\b(password|passcode|otp|one[- ]time code|api[_ -]?key)\s*(is|was|=|:)\s*"
                          r"(?!\[hidden\])\S{4,}")
REASON_GROUPS = [
    ("Security incident", ("security incident", "soc handoff")),
    ("Physical safety", ("physical safety",)),
    ("P1 critical", ("priority p1",)),
    ("Two attempts failed", ("attempts failed", "no second supported attempt")),
    ("No grounded KB", ("no kb article",)),
    ("User asked for a human", ("asked for a human", "needed help following")),
    ("Privileged / approval (V4)", ("privileged", "v4")),
    ("Identity recovery (V3)", ("v3",)),
    ("Another person's account", ("another person", "sec-06")),
    ("Multi-user incident", ("multiple users", "incident opened", "linked to incident", "known service incident")),
    ("Mandatory handoff", ("mandatory handoff", "handling mode")),
    ("Jev unavailable", ("model unavailable",)),
    ("Just log (form)", ("just log",)),
    ("Unclear category", ("other / unclear", "not sure")),
    ("Reopened", ("reopened",)),
    ("Taken over by agent", ("taken over",)),
    ("Verification / tool failure", ("verification failed", "failed:", "registry")),
]


def reason_group(reason: str | None) -> str:
    r = (reason or "").lower()
    for name, keys in REASON_GROUPS:
        if any(k in r for k in keys):
            return name
    return "Other"


def _hours(a: str | None, b: str | None) -> float | None:
    da, db = parse_ts(a), parse_ts(b)
    return round((db - da).total_seconds() / 3600, 2) if da and db else None


def _day(ts: str) -> str:
    return ts[:10]


def _week(ts: str) -> str:
    d = parse_ts(ts)
    return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d") if d else ""


class AnalyticsService:
    def __init__(self, store: Store, k: Knowledge, tickets: TicketService):
        self.store, self.k, self.tickets = store, k, tickets

    # ================================================================ filters
    def _tickets(self, f: dict | None = None) -> list[dict]:
        f = f or {}
        rows = self.store.query("SELECT * FROM tickets")
        out = []
        for t in rows:
            if f.get("date_from") and t["created_at"][:10] < f["date_from"]:
                continue
            if f.get("date_to") and t["created_at"][:10] > f["date_to"]:
                continue
            if f.get("queues") and t["queue"] not in f["queues"]:
                continue
            if f.get("categories") and t["category_id"] not in f["categories"]:
                continue
            if f.get("channels") and t["channel"] not in f["channels"]:
                continue
            if f.get("priorities") and t["priority"] not in f["priorities"]:
                continue
            out.append(t)
        return out

    @staticmethod
    def outcome(t: dict) -> str:
        code = t.get("resolution_code")
        if code == "Solved by bot":
            return "Bot-resolved"
        if code in RESOLVED_CODES_HUMAN:
            return "Human-resolved"
        if code in ("Duplicate", "Cancelled by user"):
            return "Cancelled / duplicate"
        if t["escalation_reason"] or t["status"] in HUMAN_STATES:
            return "Escalated (open)"
        return "Bot in progress"

    def _sla(self, t: dict) -> dict:
        return self.tickets.sla_status(t)

    # ================================================================ KPIs
    def kpis(self, f: dict | None = None) -> dict:
        ts = self._tickets(f)
        open_ = [t for t in ts if t["status"] not in ("RESOLVED", "RESOLVED_PENDING_CONFIRMATION", "CLOSED",
                                                     "CANCELLED")]
        resolved = [t for t in ts if t["resolution_code"] in ("Solved by bot",) + RESOLVED_CODES_HUMAN]
        sla_rows = [self._sla(t) for t in resolved]
        met = sum(s.get("resolve_state") == "met" for s in sla_rows)
        reopened = sum((t["reopened_count"] or 0) > 0 for t in ts)
        ever_resolved = len(resolved) + reopened
        ids = {t["ticket_id"] for t in ts}
        votes = [r["helpful"] for r in self.store.query("SELECT ticket_id, helpful FROM answer_feedback")
                 if r["ticket_id"] in ids]
        scores = [r["score"] for r in self.store.query("SELECT ticket_id, score FROM ticket_ratings")
                  if r["ticket_id"] in ids]
        return {
            "answers_helpful_pct": round(sum(votes) / len(votes), 3) if votes else None,
            "answer_ratings": len(votes),
            "satisfaction": round(sum(scores) / len(scores), 2) if scores else None,
            "satisfaction_ratings": len(scores),
            "open": len(open_),
            "p1_open": sum(t["priority"] == "P1" for t in open_),
            "awaiting_human": sum(t["status"] == "ESCALATION_QUEUED" for t in open_),
            "bot_resolution_rate": round(sum(t["resolution_code"] == "Solved by bot" for t in resolved)
                                         / len(resolved), 3) if resolved else None,
            "sla_met_pct": round(met / len(sla_rows), 3) if sla_rows else None,
            "reopen_rate": round(reopened / ever_resolved, 3) if ever_resolved else None,
            "total": len(ts),
        }

    # ================================================================ charts C1–C12
    def charts(self, f: dict | None = None) -> dict:
        ts = self._tickets(f)
        ids = {t["ticket_id"] for t in ts}
        out: dict = {}
        out["C1"] = [{"day": d, "channel": c, "n": n} for (d, c), n in
                     sorted(Counter((_day(t["created_at"]), t["channel"] or "Chat") for t in ts).items())]
        out["C2"] = [{"category": self.k.name(c), "n": n} for c, n in
                     Counter(t["category_id"] for t in ts).most_common()]
        out["C3"] = [{"week": w, "outcome": o, "n": n} for (w, o), n in
                     sorted(Counter((_week(t["created_at"]), self.outcome(t)) for t in ts).items())]
        out["C4"] = [{"reason": r, "n": n} for r, n in
                     Counter(reason_group(t["escalation_reason"]) for t in ts if t["escalation_reason"]).most_common()]
        c5 = defaultdict(lambda: {"resp_met": 0, "resp_n": 0, "res_met": 0, "res_n": 0})
        for t in ts:
            s = self._sla(t)
            if not s:
                continue
            row = c5[t["priority"]]
            if s["response_state"] in ("met", "breached"):
                row["resp_n"] += 1
                row["resp_met"] += s["response_state"] == "met"
            if s["resolve_state"] in ("met", "breached"):
                row["res_n"] += 1
                row["res_met"] += s["resolve_state"] == "met"
        out["C5"] = [{"priority": p, "response_met_pct": round(r["resp_met"] / r["resp_n"], 3) if r["resp_n"] else None,
                      "resolve_met_pct": round(r["res_met"] / r["res_n"], 3) if r["res_n"] else None,
                      "n": max(r["resp_n"], r["res_n"])} for p, r in sorted(c5.items())]
        out["C6"] = [{"who": "Bot" if t["resolution_code"] == "Solved by bot" else "Human",
                      "hours": _hours(t["created_at"], t["resolved_at"])} for t in ts
                     if t["resolved_at"] and t["resolution_code"] in ("Solved by bot",) + RESOLVED_CODES_HUMAN]
        out["C7"] = [{"day": d, "priority": p, "n": n} for (d, p), n in
                     sorted(Counter((_day(t["created_at"]), t["priority"]) for t in ts).items())]
        by_day = defaultdict(lambda: [0, 0])
        for t in ts:
            if t["jev_confidence"] is not None:
                by_day[_day(t["created_at"])][1] += 1
                by_day[_day(t["created_at"])][0] += t["jev_confidence"] < config.CATEGORY_MIN_CONFIDENCE
        out["C8"] = [{"day": d, "low_conf_rate": round(a / b, 3), "n": b} for d, (a, b) in sorted(by_day.items())]
        corr = [c for c in self.store.corrections() if c["field"] == "category_id" and c["ticket_id"] in ids]
        original = {}  # the category the bot (or form) first assigned, before any correction
        for c in sorted(corr, key=lambda c: c["id"]):
            original.setdefault(c["ticket_id"], c["old_value"])
        per_cat = Counter(original.get(t["ticket_id"], t["category_id"]) for t in ts)
        wrong = Counter(original.values())
        out["C9"] = [{"category": self.k.name(c), "corrections": wrong.get(c, 0), "tickets": n,
                      "accuracy": round(1 - wrong.get(c, 0) / n, 3)} for c, n in per_cat.most_common()
                     if c in self.k.categories]
        out["C10"] = self.kb_success(f)
        nowdt = datetime.now(timezone.utc)
        buckets = Counter()
        for t in ts:
            if t["status"] in ("RESOLVED", "RESOLVED_PENDING_CONFIRMATION", "CLOSED", "CANCELLED"):
                continue
            h = (nowdt - parse_ts(t["created_at"])).total_seconds() / 3600
            buckets["0–4h" if h < 4 else "4–24h" if h < 24 else "1–3d" if h < 72 else ">3d"] += 1
        out["C11"] = [{"age": b, "n": buckets.get(b, 0)} for b in ("0–4h", "4–24h", "1–3d", ">3d")]
        out["C12"] = self.safety_scorecard(ts)
        out["targets"] = {r["Metric"]: r["Target"] for r in _metrics()}
        return out

    def kb_success(self, f: dict | None = None) -> list[dict]:
        ids = {t["ticket_id"] for t in self._tickets(f)}
        rows = [r for r in self.store.query("SELECT * FROM bot_answers WHERE ticket_id IS NOT NULL AND attempt>0")
                if r["ticket_id"] in ids]
        agg = defaultdict(lambda: {"sent": 0, "fixed": 0, "not_fixed": 0})
        for r in rows:
            a = agg[r["kb_id"]]
            a["sent"] += 1
            a["fixed"] += r["outcome"] == "fixed"
            a["not_fixed"] += r["outcome"] == "not_fixed"
        return sorted(({"kb_id": kb, "title": self.k.kb[kb].title, **a,
                        "success_rate": round(a["fixed"] / a["sent"], 3) if a["sent"] else None}
                       for kb, a in agg.items()), key=lambda r: (r["success_rate"] is None, r["success_rate"] or 0))

    def safety_scorecard(self, ts: list[dict]) -> dict:
        thr = config.RISK_FLAG_THRESHOLD
        critical, routed = 0, 0
        for t in ts:
            flags = json.loads(t["triage_json"] or "{}").get("flags", {})
            if flags.get("security_incident", 0) >= thr or flags.get("physical_safety", 0) >= thr or \
                    t["category_id"] == "CAT-09":
                critical += 1
                routed += bool(t["escalation_reason"]) or t["status"] in HUMAN_STATES
        ids = {t["ticket_id"] for t in ts}
        restricted, unauthorized = 0, 0
        for tid in ids:
            verified = False
            for e in self.store.events(tid):
                p = e["payload"]
                if p.get("tool") == "TOOL-03":
                    verified = p.get("status") == "VERIFIED"
                if p.get("tool") in ("TOOL-04", "TOOL-05", "TOOL-06") and p.get("status") == "SUCCESS":
                    restricted += 1
                    unauthorized += not verified
        leaks = sum(bool(_SECRET_LEAK.search(m["text"] or "")) for m in self.store.query(
            "SELECT text FROM messages"))
        return {"critical_recall": round(routed / critical, 3) if critical else None, "critical_cases": critical,
                "unauthorized_action_rate": round(unauthorized / restricted, 3) if restricted else 0.0,
                "restricted_actions": restricted, "secret_leakage": leaks}

    # ================================================================ bot answers log
    def answer_team(self, ticket: dict | None, category_id: str | None) -> str | None:
        """The team an answer belongs to: its ticket's queue, or (a how-to answer with no ticket) the team that
        owns the category."""
        if ticket:
            return ticket["queue"]
        return (self.k.routing.get(category_id or "") or {}).get("Primary_Queue")

    def bot_answers(self, f: dict | None = None) -> dict:
        """Every set of fix steps the bot sent, newest first, and the success rate per article for the SAME rows,
        so the numbers always match the list. `f` (all optional): queues (the teams to show; an agent's own
        queues are forced by the API), agent (ticket owner), categories, kb_id, outcome, date_from, date_to,
        search (ticket number, article or words in the steps). 7 Oct: every agent saw every team's answers."""
        f = f or {}
        tickets = {t["ticket_id"]: t for t in self.store.query("SELECT ticket_id, queue, owner FROM tickets")}
        names = {u["user_id"]: u["name"] for u in self.store.query("SELECT user_id, name FROM users")}
        search = (f.get("search") or "").strip().lower()
        rows = []
        for r in self.store.query("SELECT * FROM bot_answers ORDER BY id DESC"):
            t = tickets.get(r["ticket_id"] or "")
            team = self.answer_team(t, r["category_id"])
            owner = t["owner"] if t else None
            outcome = r["outcome"] or "pending"
            kb = self.k.kb.get(r["kb_id"])
            title = kb.title if kb else r["kb_id"]
            if f.get("queues") is not None and team not in f["queues"]:
                continue
            if f.get("agent") and owner != f["agent"]:
                continue
            if f.get("categories") and r["category_id"] not in f["categories"]:
                continue
            if f.get("kb_id") and r["kb_id"] != f["kb_id"]:
                continue
            if f.get("outcome") and outcome != f["outcome"]:
                continue
            day = (r["created_at"] or "")[:10]
            if f.get("date_from") and day < f["date_from"] or f.get("date_to") and day > f["date_to"]:
                continue
            if search and search not in " ".join((r["ticket_id"] or "", r["kb_id"] or "", title,
                                                  r["steps_text"] or "")).lower():
                continue
            rows.append({**r, "outcome": outcome, "title": title, "category": self.k.name(r["category_id"]),
                         "team": team, "agent_id": owner, "agent": names.get(owner) if owner else None})
        agg = defaultdict(lambda: {"sent": 0, "fixed": 0, "not_fixed": 0, "escalated": 0})
        for r in rows:  # success: fix steps sent inside a ticket (attempt 1 or 2); how-to answers have no outcome
            if r["ticket_id"] and (r["attempt"] or 0) > 0:
                a = agg[r["kb_id"]]
                a["sent"] += 1
                if r["outcome"] in a:
                    a[r["outcome"]] += 1
        success = sorted(({"kb_id": kb, "title": self.k.kb[kb].title if kb in self.k.kb else kb,
                           "sent": a["sent"], "fixed": a["fixed"], "not_fixed": a["not_fixed"],
                           "escalated": a["escalated"],
                           "success_rate": round(a["fixed"] / a["sent"], 3) if a["sent"] else None}
                          for kb, a in agg.items()),
                         key=lambda r: (r["success_rate"] is None, r["success_rate"] or 0))
        return {"answers": rows[:1000], "success": success, "total": len(rows)}

    # ================================================================ agent performance (spec §8.5)
    def performance(self, date_from: str | None = None, date_to: str | None = None,
                    agent_id: str | None = None) -> list[dict]:
        def in_range(ts):
            return (not date_from or ts[:10] >= date_from) and (not date_to or ts[:10] <= date_to)

        users = {u["user_id"]: u for u in self.store.query("SELECT * FROM users WHERE role IN ('agent','supervisor')")}
        ev = self.store.query("SELECT * FROM events WHERE ticket_id IS NOT NULL ORDER BY id")
        by_ticket = defaultdict(list)
        for e in ev:
            e["payload"] = json.loads(e["payload_json"])
            by_ticket[e["ticket_id"]].append(e)
        stats = defaultdict(lambda: {"handled": 0, "first_resp": [], "resolution": [], "resolved": 0, "sla_met": 0,
                                     "reopened": 0, "checklist_ok": 0})
        for tid, events in by_ticket.items():
            t = self.store.get_ticket(tid)
            claim_at, awaiting_reply, last_resolver = {}, set(), None
            for e in events:
                a = e["actor"]
                if e["event_type"] == "Claimed" and a in users:
                    claim_at[a] = e["created_at"]
                    awaiting_reply.add(a)
                    if in_range(e["created_at"]):
                        stats[a]["handled"] += 1
                elif e["event_type"] == "Comment" and a in awaiting_reply and \
                        e["payload"].get("visibility") == "customer":
                    awaiting_reply.discard(a)  # only the first customer-visible reply after each claim
                    if in_range(e["created_at"]):
                        stats[a]["first_resp"].append(_hours(claim_at[a], e["created_at"]))
                elif e["event_type"] == "Status" and e["payload"].get("to") == "RESOLVED_PENDING_CONFIRMATION" \
                        and a in users:
                    last_resolver = a
                    if in_range(e["created_at"]):
                        s = stats[a]
                        s["resolved"] += 1
                        if a in claim_at:
                            s["resolution"].append(_hours(claim_at[a], e["created_at"]))
                        # the same verdict as the ticket timer and Charts C5 (business hours, paused while waiting
                        # on the employee); plain clock hours only if the ticket has since been reopened (7 Oct)
                        verdict = self._sla(t).get("resolve_state") if t else None
                        sla = self.k.sla_target(t["category_id"], t["priority"]) if t else None
                        if verdict in ("met", "breached"):
                            s["sla_met"] += verdict == "met"
                        elif sla and _hours(t["created_at"], e["created_at"]) <= sla["Resolution_Target_Hours"]:
                            s["sla_met"] += 1
                        req = [c for c in self.tickets.checklist(tid) if c["required"]]
                        s["checklist_ok"] += all(c["done_at"] and c["done_at"] <= e["created_at"] for c in req)
                elif e["event_type"] == "Status" and e["payload"].get("to") == "REOPENED" and last_resolver:
                    stats[last_resolver]["reopened"] += 1
        corr = Counter(c["corrected_by"] for c in self.store.corrections()
                       if in_range(c["created_at"]))
        out = []
        for uid, u in users.items():
            if agent_id and uid != agent_id:
                continue
            s = stats[uid]
            fr = [x for x in s["first_resp"] if x is not None]
            rs = [x for x in s["resolution"] if x is not None]
            out.append({"user_id": uid, "name": u["name"], "role": u["role"],
                        "queues": json.loads(u["queues"] or "[]"), "handled": s["handled"],
                        "median_first_response_h": round(median(fr), 2) if fr else None,
                        "median_resolution_h": round(median(rs), 2) if rs else None,
                        "resolved": s["resolved"],
                        "sla_met_pct": round(s["sla_met"] / s["resolved"], 3) if s["resolved"] else None,
                        "reopen_rate": round(s["reopened"] / s["resolved"], 3) if s["resolved"] else None,
                        "checklist_completion": round(s["checklist_ok"] / s["resolved"], 3) if s["resolved"] else None,
                        "corrections_made": corr.get(uid, 0)})
        return sorted(out, key=lambda r: (-r["handled"], r["name"]))

    # ================================================================ shift summary (spec §8.7)
    def shift_summary(self, day: str | None = None) -> dict:
        day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        ts = self.store.query("SELECT * FROM tickets")
        new = [t for t in ts if t["created_at"][:10] == day]
        resolved_today = [t for t in ts if (t["resolved_at"] or "")[:10] == day]
        bot = sum(t["resolution_code"] == "Solved by bot" for t in resolved_today)
        human = sum(t["resolution_code"] in RESOLVED_CODES_HUMAN for t in resolved_today)
        escalated = self.store.one("SELECT COUNT(DISTINCT ticket_id) n FROM events WHERE event_type='Status' AND "
                                   "payload_json LIKE '%\"to\": \"ESCALATION_QUEUED\"%' AND substr(created_at,1,10)=?",
                                   (day,))["n"]
        breaches = sum(1 for t in ts if (t["created_at"][:10] == day or (t["resolved_at"] or "")[:10] == day)
                       and "breached" in (self._sla(t).get("response_state"), self._sla(t).get("resolve_state")))
        open_p1 = [t["ticket_id"] for t in ts if t["priority"] == "P1" and t["status"] not in
                   ("RESOLVED", "RESOLVED_PENDING_CONFIRMATION", "CLOSED", "CANCELLED")]
        top3 = [(self.k.name(c), n) for c, n in Counter(t["category_id"] for t in new).most_common(3)]
        reasons = Counter(reason_group(t["escalation_reason"]) for t in new if t["escalation_reason"])
        top_reason = reasons.most_common(1)[0] if reasons else None
        outages = self.store.one("SELECT COUNT(*) n FROM events WHERE event_type='Fallback' AND "
                                 "substr(created_at,1,10)=?", (day,))["n"]
        text = (f"Shift summary for {day} (UTC): {len(new)} new tickets, {bot + human} resolved "
                f"({bot} by the bot, {human} by agents), {escalated} escalated, {breaches} SLA breach"
                f"{'es' if breaches != 1 else ''}. Open P1s: {', '.join(open_p1) if open_p1 else 'none'}. "
                f"Top issue types: {', '.join(f'{c} ({n})' for c, n in top3) if top3 else 'none'}. "
                f"Top escalation reason: {f'{top_reason[0]} ({top_reason[1]})' if top_reason else 'none'}. "
                f"Jev outages: {outages}.")
        return {"day": day, "new": len(new), "resolved_bot": bot, "resolved_human": human, "escalated": escalated,
                "sla_breaches": breaches, "open_p1": open_p1, "top_issue_types": top3,
                "top_escalation_reason": top_reason, "jev_outages": outages, "text": text}

    # ================================================================ shift checklist
    SHIFT_ITEMS = [("p1_reviewed", "Open P1s reviewed"), ("alerts_cleared", "Unacknowledged alerts cleared"),
                   ("sla_owned", "SLA-at-risk tickets owned"), ("handover", "Handover notes written")]

    def shift_status(self) -> dict:
        """Live hints next to each shift item, so the checklist reflects the real queue."""
        open_rows = self.store.query("SELECT * FROM tickets WHERE status IN ('ESCALATION_QUEUED','HUMAN_ASSIGNED',"
                                     "'HUMAN_IN_PROGRESS')")
        at_risk_unowned = [t["ticket_id"] for t in open_rows if not t["owner"] and
                           self._sla(t).get("resolve_state") in ("at_risk", "breached")]
        unacked = self.store.one("SELECT COUNT(*) n FROM alerts WHERE acked_at IS NULL")["n"]
        return {"open_p1": [t["ticket_id"] for t in open_rows if t["priority"] == "P1"],
                "unacked_alerts": unacked, "at_risk_unowned": at_risk_unowned}


def _metrics() -> list[dict]:
    with open(config.DATA_DIR / "metrics_acceptance.json", encoding="utf-8") as f:
        return json.load(f)
