"""TicketService: create, transition(), claim, versioned update, checklists, resolve, reopen, cancel.

Owns every status change (spec §5.3). It never talks to Jev. Illegal jumps raise
IllegalTransition; every change is appended to `events`.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .. import business_hours as bh
from .. import config
from ..directory import employee_profile
from ..knowledge import OTHER_CATEGORY, Knowledge
from ..store import OPEN_EXCLUDED, Store, VersionConflict, now, parse_ts

# ---------------------------------------------------------------- lifecycle
BOT_STATES = ("NEW", "CLASSIFYING", "COLLECTING_INFORMATION", "WAITING_FOR_USER", "READY_FOR_RESOLUTION",
              "VERIFICATION_PENDING", "ATTEMPT_1", "WAITING_FOR_VALIDATION_1", "ATTEMPT_2",
              "WAITING_FOR_VALIDATION_2", "REOPENED")
HUMAN_STATES = ("ESCALATION_QUEUED", "HUMAN_ASSIGNED", "HUMAN_IN_PROGRESS")
DONE_STATES = ("RESOLVED_PENDING_CONFIRMATION", "RESOLVED", "CLOSED", "CANCELLED")
OPEN_STATES = BOT_STATES + HUMAN_STATES

# status_lifecycle.json (22 rows) + transitions drawn in diagram 07 that the JSON omits
_DATASET_EXTRA = {
    ("COLLECTING_INFORMATION", "READY_FOR_RESOLUTION"): "Context complete (diagram 07)",
    ("RESOLVED", "CLOSED"): "Reopen window elapsed (diagram 07)",
    ("RESOLVED", "REOPENED"): "Issue recurs (diagram 07 / EXT-4)",
}
# Spec §5.3 extensions. EXT-1 also covers the two ATTEMPT states: the playbooks escalate on
# "failed verification or tool failure", which happens while an attempt is executing.
_EXT1_FROM = ("CLASSIFYING", "COLLECTING_INFORMATION", "READY_FOR_RESOLUTION", "WAITING_FOR_USER",
              "WAITING_FOR_VALIDATION_1", "ATTEMPT_1", "ATTEMPT_2")
EXTENSIONS = {
    **{(s, "ESCALATION_QUEUED"): "EXT-1 safety/P1/handoff/wants human/no grounded KB" for s in _EXT1_FROM},
    ("ESCALATION_QUEUED", "HUMAN_IN_PROGRESS"): "EXT-2 direct take-over",
    **{(s, "CANCELLED"): "EXT-3 cancel (audited)" for s in OPEN_STATES + ("RESOLVED_PENDING_CONFIRMATION",)},
    **{(s, "ESCALATION_QUEUED"): "EXT-5 reassign" for s in HUMAN_STATES},
}


def load_transitions() -> dict[tuple[str, str], str]:
    t = {(r["From_Status"], r["To_Status"]): r["Trigger"] for r in k_lifecycle()}
    t.update(_DATASET_EXTRA)
    for key, why in EXTENSIONS.items():
        t.setdefault(key, why)
    return t


def k_lifecycle() -> list[dict]:
    with open(config.DATA_DIR / "status_lifecycle.json", encoding="utf-8") as f:
        return json.load(f)


RESOLUTION_CODES = ["Solved by bot", "Solved by agent", "Workaround given", "Duplicate", "Not reproducible",
                    "Cancelled by user"]
AGENT_RESOLUTION_CODES = ["Solved by agent", "Workaround given", "Not reproducible"]
ACCESS_SECURITY_CATS = ("CAT-01", "CAT-05", "CAT-07", "CAT-09")


class TicketError(Exception):
    status_code = 400


class IllegalTransition(TicketError):
    status_code = 409


class Forbidden(TicketError):
    status_code = 403


class AlreadyOwned(TicketError):
    status_code = 409


class ResolveBlocked(TicketError):
    status_code = 409

    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__("Resolve blocked: " + "; ".join(missing))


def employee_label(t: dict, owner_name: str | None = None) -> str:
    """Friendly state for the employee view (spec §7.4)."""
    s = t["status"]
    team = (t.get("queue") or "support").replace(" Support", "")
    if s in ("NEW", "CLASSIFYING", "COLLECTING_INFORMATION", "READY_FOR_RESOLUTION", "VERIFICATION_PENDING",
             "ATTEMPT_1", "ATTEMPT_2", "REOPENED"):
        return "Assistant is working on it"
    if s == "WAITING_FOR_USER":
        return "Waiting for your answer"
    if s.startswith("WAITING_FOR_VALIDATION"):
        return "Waiting for you to confirm the fix"
    if s == "ESCALATION_QUEUED":
        return f"With {team} team"
    if s in ("HUMAN_ASSIGNED", "HUMAN_IN_PROGRESS"):
        return f"{owner_name or 'An engineer'} ({team}) is working on it"
    return {"RESOLVED_PENDING_CONFIRMATION": "Resolved, please confirm", "RESOLVED": "Resolved",
            "CLOSED": "Closed", "CANCELLED": "Cancelled"}.get(s, s)


class TicketService:
    def __init__(self, store: Store, k: Knowledge, alerts=None, notify=None):
        self.store, self.k = store, k
        self.alerts, self.notify = alerts, notify
        self.transitions = load_transitions()

    # ================================================================ lookups
    def get(self, tid: str) -> dict:
        t = self.store.get_ticket(tid)
        if not t:
            raise TicketError(f"{tid} not found")
        return t

    def user_name(self, user_id: str | None) -> str | None:
        if not user_id:
            return None
        r = self.store.one("SELECT name FROM users WHERE user_id=?", (user_id,))
        return r["name"] if r else user_id

    def legal_next(self, status: str) -> list[str]:
        return sorted({b for (a, b) in self.transitions if a == status})

    # ================================================================ create + transition
    def create(self, *, session_id: str | None, employee_id: str, category_id: str, impact: str | None,
               urgency: str | None, flags: dict | None, summary: str, channel: str = "Chat",
               opened_by: str = "Bot (chat)", actor: str = "bot", **extra) -> str:
        emp = employee_profile(self.store, self.k, employee_id)
        prio, reason = self.k.compute_priority(impact, urgency, flags, category_id)
        tid = self.store.create_ticket(
            session_id=session_id, employee_id=employee_id, status="NEW", category_id=category_id,
            ticket_type=self.k.ticket_type(category_id) if category_id != OTHER_CATEGORY else "Incident",
            priority=prio, priority_source="computed", priority_reason=reason, impact=impact, urgency=urgency,
            sub_agent=self.k.sub_agent(category_id), queue=self.k.queue(category_id), channel=channel,
            location=emp.get("Location"), config_item=emp.get("Asset_Tag"), summary=summary[:300],
            attempts=0, verification="V1 (session)", opened_by=opened_by, updated_by=actor, **extra)
        self.store.log_event("Opened", {"detail": f"{tid} via {channel} · {prio} ({reason})",
                                                "priority": prio, "reason": reason},
                             ticket_id=tid, session_id=session_id, actor=actor)
        self.transition(tid, "CLASSIFYING", actor="system", reason="Request accepted")
        return tid

    def transition(self, tid: str, to: str, actor: str, reason: str = "", **fields) -> dict:
        """The only way a status changes. Rejects anything not in the lifecycle table."""
        with self.store.tx() as c:
            row = c.execute("SELECT status FROM tickets WHERE ticket_id=?", (tid,)).fetchone()
            if not row:
                raise TicketError(f"{tid} not found")
            frm = row["status"]
            if (frm, to) not in self.transitions:
                self.store.log_event("Illegal transition blocked", {"detail": f"{frm} → {to}", "reason": reason},
                                     ticket_id=tid, actor=actor, conn=c)
                raise IllegalTransition(f"{tid}: {frm} → {to} is not a legal transition")
            self.store.update_ticket(tid, conn=c, status=to, updated_by=actor, **fields)
            self.store.log_event("Status", {"detail": f"{frm} → {to}" + (f" · {reason}" if reason else ""),
                                            "from": frm, "to": to, "reason": reason, "rule":
                                            self.transitions[(frm, to)]}, ticket_id=tid, actor=actor, conn=c)
        return self.get(tid)

    def walk(self, tid: str, path: list[str], actor: str, reason: str = "") -> None:
        """Move through several legal states in order, skipping ones already reached."""
        for to in path:
            if self.get(tid)["status"] != to:
                self.transition(tid, to, actor, reason)

    # ================================================================ escalation
    def escalate(self, tid: str, reason: str, handoff: dict | None, actor: str = "bot",
                 alert: bool = True) -> dict:
        t = self.get(tid)
        fields = {"escalation_reason": reason}
        if handoff is not None:
            fields["handoff_json"] = json.dumps(handoff)
        if t["status"] == "REOPENED":
            self.transition(tid, "COLLECTING_INFORMATION", actor, "Re-triage")
        t = self.transition(tid, "ESCALATION_QUEUED", actor, reason, **fields)
        self.build_checklist(t)
        if alert and self.alerts:
            self.alerts.on_escalation(t)
        return t

    # ================================================================ ownership (spec §5.4)
    def _atomic_claim(self, tid: str, user_id: str) -> None:
        with self.store.tx() as c:
            n = c.execute("UPDATE tickets SET owner=?, claimed_at=?, updated_by=?, version=version+1, "
                          "updated_at=? WHERE ticket_id=? AND owner IS NULL",
                          (user_id, now(), user_id, now(), tid)).rowcount
            if n == 0:
                owner = c.execute("SELECT owner FROM tickets WHERE ticket_id=?", (tid,)).fetchone()
                if owner and owner["owner"] == user_id:
                    return
                raise AlreadyOwned(f"Owned by {self.user_name(owner['owner'] if owner else None)}")
            self.store.log_event("Claimed", {"detail": f"owner = {self.user_name(user_id)}"}, ticket_id=tid,
                                 actor=user_id, conn=c)

    def claim(self, tid: str, user: dict) -> dict:
        t = self.get(tid)
        if t["status"] != "ESCALATION_QUEUED":
            raise IllegalTransition(f"Only queued tickets can be claimed (state {t['status']})")
        self._atomic_claim(tid, user["user_id"])
        if self.alerts:
            self.alerts.ack_ticket(tid, user["user_id"])
        return self.transition(tid, "HUMAN_ASSIGNED", user["user_id"], "Human accepts")

    def take_over(self, tid: str, user: dict) -> dict:
        """Claim + start in one step (EXT-2). From a bot state it escalates first (scenario 5)."""
        t = self.get(tid)
        if t["owner"] and t["owner"] != user["user_id"]:
            raise AlreadyOwned(f"Owned by {self.user_name(t['owner'])}")
        if t["status"] in BOT_STATES:
            self.escalate(tid, f"Taken over by {user['name']} mid-conversation", None,
                          actor=user["user_id"], alert=False)
            t = self.get(tid)
        if t["status"] not in ("ESCALATION_QUEUED", "HUMAN_ASSIGNED"):
            raise IllegalTransition(f"Cannot take over from {t['status']}")
        self._atomic_claim(tid, user["user_id"])
        if self.alerts:
            self.alerts.ack_ticket(tid, user["user_id"])
        t = self.transition(tid, "HUMAN_IN_PROGRESS", user["user_id"], "Takeover")
        self.build_checklist(t)
        self._set_session_stage(t, "HUMAN")
        if t["session_id"]:
            self.store.add_message(t["session_id"], "system", f"👤 {user['name']} from {t['queue']} joined the "
                                                              "chat.", ticket_id=tid, author=user["user_id"])
        return t

    def start(self, tid: str, user: dict) -> dict:
        t = self.get(tid)
        self._require_owner(t, user)
        t = self.transition(tid, "HUMAN_IN_PROGRESS", user["user_id"], "Takeover")
        self._set_session_stage(t, "HUMAN")
        return t

    def _require_owner(self, t: dict, user: dict) -> None:
        if t["owner"] != user["user_id"] and user["role"] != "supervisor":
            raise Forbidden(f"Owned by {self.user_name(t['owner']) or 'nobody'}; take it over first")

    def _set_session_stage(self, t: dict, stage: str) -> None:
        if not t.get("session_id"):
            return
        st = self.store.get_session(t["session_id"]) or {}
        st["stage"] = stage
        if stage == "HUMAN":
            st["ticket_id"] = t["ticket_id"]
        self.store.save_session(t["session_id"], t["employee_id"], st)

    # ================================================================ form edits (optimistic locking)
    EDITABLE = ("category_id", "subcategory", "impact", "urgency", "config_item", "location", "channel",
                "summary", "priority")
    CORRECTED = ("category_id", "impact", "urgency", "priority")

    def update(self, tid: str, user: dict, changes: dict, expected_version: int, reason: str = "") -> dict:
        t = self.get(tid)
        changes = {f: v for f, v in changes.items() if f in self.EDITABLE and v is not None and v != t[f]}
        if not changes:
            return t
        if "priority" in changes and user["role"] != "supervisor":
            raise Forbidden("Priority is computed; only a supervisor can override it (D11)")
        corrected = [f for f in changes if f in self.CORRECTED]
        if corrected and not reason.strip():
            raise TicketError(f"A reason is required when correcting {', '.join(corrected)}")
        fields = dict(changes)
        if "category_id" in changes:
            cat = changes["category_id"]
            fields.update(sub_agent=self.k.sub_agent(cat), ticket_type=self.k.ticket_type(cat))
            if "subcategory" not in changes:
                fields["subcategory"] = None
        if "priority" in changes:
            fields.update(priority_source="manual", priority_reason=f"supervisor override: {reason}")
        elif t.get("priority_source") != "manual" and {"impact", "urgency", "category_id"} & set(changes):
            flags = json.loads(t["triage_json"] or "{}").get("flags", {})
            p, why = self.k.compute_priority(fields.get("impact", t["impact"]), fields.get("urgency", t["urgency"]),
                                             flags, fields.get("category_id", t["category_id"]))
            fields.update(priority=p, priority_reason=why)
        fields["updated_by"] = user["user_id"]
        try:
            self.store.update_ticket(tid, expected_version=expected_version, **fields)
        except VersionConflict as e:
            e.args = (f"{self.user_name(e.ticket.get('updated_by')) or 'Someone'} updated this, reload",)
            raise
        for f in corrected:
            self.store.add_correction(tid, f, t[f], changes[f], user["user_id"], reason)
        self.store.log_event("Form updated", {"detail": ", ".join(f"{f}: {t[f]} → {v}" for f, v in changes.items())
                                              + (f" · reason: {reason}" if reason else ""), "changes": changes,
                                              "before": {f: t[f] for f in changes}}, ticket_id=tid,
                             actor=user["user_id"])
        if "priority" in fields and fields["priority"] != t["priority"] and self.alerts and \
                t["status"] in HUMAN_STATES:
            self.alerts.on_priority_change(self.get(tid))
        return self.get(tid)

    # ================================================================ comments / work notes
    def comment(self, tid: str, user: dict, text: str, visibility: str) -> None:
        t = self.get(tid)
        if visibility not in ("customer", "internal"):
            raise TicketError("visibility must be customer or internal")
        role = "agent" if user["role"] in ("agent", "supervisor") else "user"
        self.store.add_message(t["session_id"], role, text, ticket_id=tid, visibility=visibility,
                               author=user["user_id"], meta={"agent": user["name"]} if role == "agent" else {})
        label = "Comment" if visibility == "customer" else "Work note"
        # the audit log records that it happened; the words live in the ticket thread (and follow retention)
        self.store.log_event(label, {"detail": f"{label.lower()} added ({len(text)} characters)",
                                     "visibility": visibility}, ticket_id=tid,
                             actor=user["user_id"])
        if role == "agent" and visibility == "customer":
            if not t["first_response_at"]:
                self.store.update_ticket(tid, first_response_at=now(), updated_by=user["user_id"])
            if self.notify:
                self.notify.notify(t["employee_id"], tid, f"{user['name']} commented on {tid}: {text[:80]}")

    def watch(self, tid: str, user_id: str) -> None:
        self.store.execute("INSERT OR IGNORE INTO watchers (ticket_id,user_id) VALUES (?,?)", (tid, user_id))

    def watchers(self, tid: str) -> list[str]:
        return [r["user_id"] for r in self.store.query("SELECT user_id FROM watchers WHERE ticket_id=?", (tid,))]

    # ================================================================ checklist (spec §8.6)
    def build_checklist(self, t: dict) -> None:
        """Base items + playbook items. Idempotent: existing ticks are kept."""
        cat, pb = t["category_id"], self.k.playbooks.get(t["category_id"], {})
        items = [("review_timeline", "Review the bot timeline and steps already tried", True)]
        if cat in ACCESS_SECURITY_CATS:
            items.append(("verification", "Confirm the verification level needed for the action", True))
        items.append(("scope", "Confirm scope and impact with the user", True))
        if pb:
            items.append(("pb_prechecks", f"Playbook pre-checks: {pb['Pre_Checks']}", True))
        kb = self.k.kb.get(t["kb_id"] or "")
        if kb:
            agent_steps = [s for s in kb.pre_steps if s.startswith(("Check", "Collect", "Confirm", "Verify"))][:2]
            for i, s in enumerate(agent_steps):
                items.append((f"kb_step_{i + 1}", f"{kb.kb_id}: {s.rstrip('.')}", True))
        if cat == "CAT-09":
            items.append(("contain", "Contain: reset credentials + revoke sessions (after verification)", True))
        if (t.get("verification") or "").startswith("V3"):
            items.append(("v3_identity", "Supervised recovery: verify identity via manager + photo ID (V3)", True))
            items.append(("v3_reenroll", "Re-enrol a registered factor before any reset", True))
        if (t.get("verification") or "").startswith("V4"):
            items.append(("v4_approval", "Approval recorded (app owner + manager + security) before acting", True))
        if t["priority"] in ("P1", "P2"):
            items.append(("eta", "Give the user an ETA", True))
        crit = pb.get("Success_Criteria", "the original function works again")
        items.append(("confirm_fix", f"Confirm the fix with the user ({crit})", True))
        items.append(("correction", "Log a correction if the bot misclassified", False))
        items.append(("kb_update", "Suggest a KB update if the article was wrong", False))
        with self.store.tx() as c:
            for pos, (item_id, text, req) in enumerate(items, 1):
                c.execute("INSERT OR IGNORE INTO checklists (ticket_id,item_id,text,required,position) "
                          "VALUES (?,?,?,?,?)", (t["ticket_id"], item_id, text, int(req), pos))

    def checklist(self, tid: str) -> list[dict]:
        return self.store.query("SELECT * FROM checklists WHERE ticket_id=? ORDER BY position, item_id", (tid,))

    def tick(self, tid: str, item_id: str, user: dict, done: bool) -> list[dict]:
        t = self.get(tid)
        self._require_owner(t, user)
        n = self.store.execute("UPDATE checklists SET done_by=?, done_at=? WHERE ticket_id=? AND item_id=?",
                               (user["user_id"] if done else None, now() if done else None, tid, item_id))
        if not n:
            raise TicketError(f"No checklist item {item_id}")
        self.store.log_event("Checklist", {"detail": f"{'✓' if done else '✗'} {item_id}"}, ticket_id=tid,
                             actor=user["user_id"])
        return self.checklist(tid)

    def missing_for_resolve(self, t: dict, code: str | None, notes: str | None) -> list[str]:
        missing = [f"Checklist: {c['text']}" for c in self.checklist(t["ticket_id"]) if c["required"]
                   and not c["done_at"]]
        if code not in RESOLUTION_CODES:
            missing.append("Resolution code")
        if not (notes or "").strip():
            missing.append("Resolution notes")
        return missing

    # ================================================================ resolve / confirm / reopen / cancel
    def resolve(self, tid: str, user: dict, code: str, notes: str) -> dict:
        """D10: blocked until required checklist items + resolution code + notes."""
        t = self.get(tid)
        self._require_owner(t, user)
        if t["status"] != "HUMAN_IN_PROGRESS":
            raise IllegalTransition(f"Take the ticket over before resolving (state {t['status']})")
        missing = self.missing_for_resolve(t, code, notes)
        if missing:
            raise ResolveBlocked(missing)
        t = self.transition(tid, "RESOLVED_PENDING_CONFIRMATION", user["user_id"], "Fix applied",
                            resolution_code=code, resolution_notes=notes, resolved_at=now())
        self._set_session_stage(t, "IDLE")
        if t["session_id"]:
            self.store.add_message(t["session_id"], "system", f"✅ {tid} was resolved by {user['name']}. "
                                   "Reply within 7 days if it comes back.", ticket_id=tid, author=user["user_id"])
        if self.notify:
            self.notify.notify(t["employee_id"], tid, f"{tid} was resolved by {user['name']}. Please confirm.")
        return t

    def confirm(self, tid: str, actor: str) -> dict:
        return self.transition(tid, "RESOLVED", actor, "Closure confirmed")

    def within_reopen_window(self, t: dict) -> bool:
        r = parse_ts(t["resolved_at"])
        return bool(r) and datetime.now(timezone.utc) - r <= timedelta(days=config.REOPEN_WINDOW_DAYS)

    def reopen(self, tid: str, actor: str, text: str) -> dict:
        """EXT-4 / dataset: ≤ 7 days after resolution, the same ticket comes back to a human."""
        t = self.get(tid)
        if t["status"] not in ("RESOLVED_PENDING_CONFIRMATION", "RESOLVED") or not self.within_reopen_window(t):
            raise IllegalTransition("Reopen is only possible within 7 days of resolution")
        prev_owner = t["owner"]
        t = self.transition(tid, "REOPENED", actor, "Issue recurs", reopened_count=(t["reopened_count"] or 0) + 1,
                            owner=None, claimed_at=None, resolution_code=None, resolution_notes=None,
                            resolved_at=None)
        if prev_owner:
            self.watch(tid, prev_owner)
            if self.notify:
                self.notify.notify(prev_owner, tid, f"{tid} was reopened by the employee.")
        # the fix didn't hold: the next owner works the checklist again (ticks are un-set, never deleted)
        self.store.execute("UPDATE checklists SET done_by=NULL, done_at=NULL WHERE ticket_id=?", (tid,))
        if t["session_id"]:
            self.store.add_message(t["session_id"], "user", text, ticket_id=tid, author=actor)
        t = self.escalate(tid, f"Reopened by employee: {text[:120]}", None, actor=actor)
        self._set_session_stage(t, "ESCALATED")
        return t

    def cancel(self, tid: str, user: dict, reason: str, code: str = "Cancelled by user",
               duplicate_of: str | None = None) -> dict:
        t = self.get(tid)
        if user["role"] == "employee" and t["employee_id"] != user["employee_id"]:
            raise Forbidden("Not your ticket")
        if not reason.strip():
            raise TicketError("A reason is required to cancel (D9: nothing is deleted)")
        fields = {"resolution_code": code, "resolution_notes": reason, "resolved_at": now()}
        if duplicate_of:
            fields["incident_parent"] = duplicate_of
        t = self.transition(tid, "CANCELLED", user["user_id"], reason, **fields)
        self._set_session_stage(t, "IDLE")
        if self.alerts:
            self.alerts.ack_ticket(tid, user["user_id"])
        return t

    def reassign(self, tid: str, user: dict, queue: str, reason: str) -> dict:
        t = self.get(tid)
        if queue not in self.k.all_queues():
            raise TicketError(f"Unknown queue {queue}")
        if not reason.strip():
            raise TicketError("A reason is required to reassign")
        prev_owner = t["owner"]
        t = self.transition(tid, "ESCALATION_QUEUED", user["user_id"], f"Reassigned to {queue}: {reason}",
                            queue=queue, owner=None, claimed_at=None)
        self._set_session_stage(t, "ESCALATED")
        if self.alerts:
            self.alerts.ack_ticket(tid, user["user_id"])
            self.alerts.on_escalation(t, event=f"reassigned:{queue}:{t['version']}")
        if self.notify:
            self.notify.notify(t["employee_id"], tid, f"{tid} was moved to the {queue} team.")
            if prev_owner and prev_owner != user["user_id"]:
                self.notify.notify(prev_owner, tid, f"{tid} was reassigned to {queue}.")
        return t

    def sweep(self) -> None:
        """Time-based transitions: auto-confirm after 3 days, close after the 7-day reopen window."""
        for t in self.store.query("SELECT * FROM tickets WHERE status IN ('RESOLVED_PENDING_CONFIRMATION',"
                                  "'RESOLVED') AND resolved_at IS NOT NULL"):
            age = datetime.now(timezone.utc) - parse_ts(t["resolved_at"])
            try:
                if t["status"] == "RESOLVED_PENDING_CONFIRMATION" and age > timedelta(days=3):
                    self.transition(t["ticket_id"], "RESOLVED", "system", "Auto-confirmed after 3 days")
                elif t["status"] == "RESOLVED" and age > timedelta(days=config.REOPEN_WINDOW_DAYS):
                    self.transition(t["ticket_id"], "CLOSED", "system", "Reopen window elapsed")
            except IllegalTransition:
                pass

    # ================================================================ queries
    def open_duplicate(self, employee_id: str, category_id: str, exclude: str | None = None) -> dict | None:
        """Spec §5.4: an open ticket in the same category within 24h → "Is this about TKT-…?"."""
        rows = self.store.query(
            f"SELECT * FROM tickets WHERE employee_id=? AND category_id=? AND created_at>=? "
            f"AND status NOT IN ({','.join('?' * len(OPEN_EXCLUDED))}) ORDER BY created_at DESC",
            (employee_id, category_id,
             (datetime.now(timezone.utc) - timedelta(hours=config.DUPLICATE_WINDOW_HOURS)).isoformat(
                 timespec="seconds"), *OPEN_EXCLUDED))
        rows = [r for r in rows if r["ticket_id"] != exclude]
        return rows[0] if rows else None

    def open_incident(self, category_id: str, location: str | None) -> dict | None:
        """Spec §5.4: first multi-user ticket becomes the incident; later matching tickets link to it."""
        if not location:
            return None
        since = (datetime.now(timezone.utc) - timedelta(hours=config.INCIDENT_LINK_HOURS)).isoformat(
            timespec="seconds")
        return self.store.one(
            f"SELECT * FROM tickets WHERE is_incident=1 AND category_id=? AND location=? AND created_at>=? "
            f"AND status NOT IN ({','.join('?' * len(OPEN_EXCLUDED))}) ORDER BY created_at LIMIT 1",
            (category_id, location, since, *OPEN_EXCLUDED))

    def queue_for(self, user: dict, include_closed: bool = False) -> list[dict]:
        """Agent: own queues + all P1. Supervisor: all. Sorted P1 → P4, then SLA due."""
        rows = self.store.tickets()
        if not include_closed:
            rows = [t for t in rows if t["status"] not in ("CLOSED", "CANCELLED", "RESOLVED")]
        if user["role"] == "agent":
            qs = set(user["queues"])
            rows = [t for t in rows if t["queue"] in qs or t["priority"] == "P1" or t["owner"] == user["user_id"]]
        for t in rows:
            t["sla"] = self.sla_status(t)
        rank = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}
        rows.sort(key=lambda t: (rank.get(t["priority"], 5), t["sla"].get("resolve_due") or "9999"))
        return rows

    def can_view(self, user: dict, t: dict) -> bool:
        if user["role"] == "supervisor":
            return True
        if user["role"] == "employee":
            return t["employee_id"] == user["employee_id"]
        return (t["queue"] in user["queues"] or t["priority"] == "P1" or t["owner"] == user["user_id"]
                or user["user_id"] in self.watchers(t["ticket_id"]))

    def sla_status(self, t: dict) -> dict:
        """Response/resolve due times and a colour (green / amber at 75% / red when breached).
        Counted in business hours (P1: 24x7). The resolve clock stops while we wait for the employee
        (dataset Pause_Rule), so that time is added back to the resolve target."""
        sla = self.k.sla_target(t["category_id"], t["priority"])
        created = parse_ts(t["created_at"])
        if not sla or not created:
            return {}
        prio, cat = t["priority"], t.get("category_id")  # security incidents run 24x7 like P1
        nowdt = datetime.now(timezone.utc)
        paused = self.paused_hours(t, nowdt)
        ack_h, res_h = sla["Acknowledgement_Hours"], sla["Resolution_Target_Hours"]
        resp_due = bh.add_hours(created, ack_h, prio, cat)
        res_due = bh.add_hours(created, res_h + paused, prio, cat)
        responded = parse_ts(t.get("claimed_at")) or (created if t["status"] in DONE_STATES
                                                      and not t.get("owner") else None)
        resolved = parse_ts(t.get("resolved_at"))

        def state(due, done, target, minus=0.0):
            end = done or nowdt
            if end > due:
                return "breached"
            frac = max(0.0, bh.hours_between(created, end, prio, cat) - minus) / target if target else 1
            return "met" if done else ("at_risk" if frac >= config.SLA_RISK_FRACTION else "ok")

        return {"response_due": resp_due.isoformat(timespec="seconds"),
                "resolve_due": res_due.isoformat(timespec="seconds"),
                "response_state": state(resp_due, responded, ack_h),
                "resolve_state": state(res_due, resolved, res_h, paused),
                "ack_hours": ack_h, "resolve_hours": res_h, "paused_hours": round(paused, 2),
                "clock": "24x7" if bh.is_24x7(prio, cat) else "business hours",
                "response_eta": bh.friendly(resp_due, nowdt), "resolve_eta": bh.friendly(res_due, nowdt)}

    def paused_hours(self, t: dict, until: datetime) -> float:
        """Counted hours the ticket spent in a pause status (waiting for the employee)."""
        rows = self.store.query("SELECT payload_json, created_at FROM events WHERE ticket_id=? AND event_type='Status' "
                                "ORDER BY id", (t["ticket_id"],))
        total, since = 0.0, None
        for r in rows:
            to = str(json.loads(r["payload_json"] or "{}").get("detail", "")).partition("→")[2].split("·")[0].strip()
            at = parse_ts(r["created_at"])
            if since is not None:
                total += bh.hours_between(since, at, t["priority"], t.get("category_id"))
                since = None
            if to in config.SLA_PAUSE_STATUSES:
                since = at
        if since is not None:
            total += bh.hours_between(since, until, t["priority"], t.get("category_id"))
        return total
