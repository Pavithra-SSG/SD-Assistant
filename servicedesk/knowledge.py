"""Loads the ServiceDesk JSON datasets and exposes deterministic lookups.

Everything here is plain code: category metadata, the priority matrix, routing,
SLA targets, KB parsing. Jev never computes these; it only supplies judgments
that this code turns into decisions.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache

from . import config
from .config import DATA_DIR


def _load(name: str):
    with open(DATA_DIR / f"{name}.json", encoding="utf-8") as f:
        return json.load(f)


# Sub-agent grouping from the IT Service Desk Ticket Assistant design doc (section 5).
SUB_AGENTS = {
    "A1 Devices": ["CAT-04", "CAT-10", "CAT-12"],
    "A2 Software": ["CAT-03"],
    "A3 Network & Comms": ["CAT-02", "CAT-06", "CAT-08", "CAT-11"],
    "A4 Access & Security": ["CAT-01", "CAT-05", "CAT-07", "CAT-09"],
}
OTHER_CATEGORY = "OTHER"

# Fields the authenticated session / tools already supply, so the bot never asks for them.
SESSION_SUPPLIED_FIELDS = {"employee_id", "account_username", "asset_tag"}

# If a KB article has no usable second attempt, fall back to another article's action.
ATTEMPT_2_FALLBACK = {"KB-001": "KB-002"}

# Categories where "several people affected" means a shared outage -> one incident, linked tickets.
INCIDENT_CATEGORIES = ("CAT-02", "CAT-06", "CAT-08", "CAT-11")
FORM_IMPACT_OPTIONS = ["Only me", "Several users", "Department/site", "Company-wide", "Unknown"]
FORM_WORKAROUND_OPTIONS = ["No workaround", "Partial workaround", "Work can continue"]
FORM_CONTACT_OPTIONS = ["In-app", "Corporate email", "Corporate phone"]

HANDOFF_MODES = ("Immediate Escalation", "Policy Check + Handoff", "Data Collection + Handoff")


@dataclass
class KBArticle:
    kb_id: str
    category_id: str
    title: str
    handling_mode: str
    verification: str
    steps_raw: str
    steps: list[str] = field(default_factory=list)
    attempt_1: str | None = None
    attempt_2: str | None = None
    tools: list[str] = field(default_factory=list)
    pre_steps: list[str] = field(default_factory=list)

    @property
    def is_handoff(self) -> bool:
        return any(m in self.handling_mode for m in HANDOFF_MODES)

    @property
    def is_verified_tool_action(self) -> bool:
        return "Verified Tool Action" in self.handling_mode

    def summary(self) -> dict:
        return {"id": self.kb_id, "title": self.title, "handling": self.handling_mode,
                "steps": self.steps_raw}


def _split_steps(text: str) -> list[str]:
    parts = re.split(r"\s*(?:^|\s)\d+\.\s", " " + text)
    return [p.strip() for p in parts if p.strip()]


def _parse_kb(row: dict) -> KBArticle:
    art = KBArticle(
        kb_id=row["KB_ID"], category_id=row["Category_ID"], title=row["Title"],
        handling_mode=row["Handling_Mode"], verification=row["Verification_Level"],
        steps_raw=row["Resolution_Steps"],
    )
    art.steps = _split_steps(art.steps_raw)
    art.tools = sorted(set(re.findall(r"TOOL-\d{2}", art.steps_raw)))
    seen_attempt = False
    for step in art.steps:
        m = re.match(r"Attempt ([12]):\s*(.*)", step, flags=re.I)
        if m:
            seen_attempt = True
            if m.group(1) == "1":
                art.attempt_1 = m.group(2).rstrip(".")
            else:
                art.attempt_2 = m.group(2).rstrip(".")
        elif not seen_attempt:
            art.pre_steps.append(step)
    if not art.attempt_1:  # inline form, e.g. KB-022 "Attempt 1 ... ; Attempt 2 ..."
        m = re.search(r"Attempt 1:?\s+(.+?);\s*Attempt 2:?\s+(.+?)\.(?:\s|$)", art.steps_raw)
        if m:
            art.attempt_1, art.attempt_2 = m.group(1).strip(), m.group(2).strip()
            art.pre_steps = [s for s in art.steps if "Attempt 1" not in s][:2]
    # "use a different supported path" is not an actionable step on its own.
    if art.attempt_2 and "different supported path" in art.attempt_2.lower():
        art.attempt_2 = None
    return art


class Knowledge:
    def __init__(self) -> None:
        self.categories = {c["Category_ID"]: c for c in _load("categories")}
        self.subcategories: dict[str, list[str]] = {}
        for s in _load("subcategories"):
            self.subcategories.setdefault(s["Category_ID"], []).append(s["Subcategory"])
        self.kb = {r["KB_ID"]: _parse_kb(r) for r in _load("kb_articles")}
        self.routing = {r["Category_ID"]: r for r in _load("routing_matrix")}
        self.priority_matrix = {(r["Impact"].lower(), r["Urgency"].lower()): r["Computed_Priority"]
                                for r in _load("priority_matrix")}
        self.sla = {(r["Category_ID"], r["Priority"]): r for r in _load("sla_policy")}
        self.fields: dict[str, list[dict]] = {}
        for f in _load("dynamic_form_fields"):
            self.fields.setdefault(f["Category_ID"], []).append(f)
        self.playbooks = {p["Category_ID"]: p for p in _load("agent_playbooks")}
        self.employees = {e["Employee_ID"]: e for e in _load("employees")}
        self.tools = {t["Tool_ID"]: t for t in _load("tool_catalog")}
        self.sample_tickets = _load("sample_tickets")
        # Slugs are what Jev sees as Choice labels; descriptions carry the meaning.
        self.slug_to_cat = {self.slug(cid): cid for cid in self.categories}
        self.slug_to_cat["other_or_unclear"] = OTHER_CATEGORY

    # ---------- categories ----------
    def slug(self, cat_id: str) -> str:
        if cat_id == OTHER_CATEGORY:
            return "other_or_unclear"
        name = self.categories[cat_id]["Category"].lower()
        return re.sub(r"[^a-z0-9]+", "_", name.replace("&", "and")).strip("_")

    def name(self, cat_id: str) -> str:
        return "Other / unclear" if cat_id == OTHER_CATEGORY else self.categories[cat_id]["Category"]

    def category_criteria(self) -> dict[str, str]:
        crit = {}
        for cid, c in self.categories.items():
            examples = ", ".join(self.subcategories.get(cid, []))
            crit[self.slug(cid)] = f"{c['Category']}. Typical issues: {examples}."
        crit["other_or_unclear"] = ("Not an IT issue covered by the categories above, "
                                    "or too vague to tell which category applies.")
        return crit

    def sub_agent(self, cat_id: str) -> str:
        for agent, cats in SUB_AGENTS.items():
            if cat_id in cats:
                return agent
        return "Human (Other / unclear)"

    # ---------- triage ----------
    def compute_priority(self, impact: str | None, urgency: str | None, flags: dict | None = None,
                         category_id: str | None = None) -> tuple[str, str]:
        """The ONE place priority is computed (spec §5.4). Returns (priority, reason).

        Matrix first, then policy overrides that only ever raise priority:
        safety/security/active harm -> P1; security category floor P2; multi-user incident floor P2.
        """
        flags = flags or {}
        risky = lambda k: flags.get(k, 0) >= config.RISK_FLAG_THRESHOLD  # noqa: E731
        prio = self.priority_matrix.get(((impact or "limited").lower(), (urgency or "medium").lower()), "P3")
        if risky("physical_safety") or risky("active_harm") or (
                risky("security_incident") and category_id != "CAT-12"):
            return "P1", "security/safety override"
        if risky("security_incident") and category_id == "CAT-12" and prio in ("P3", "P4"):
            return "P2", "lost/compromised device floor"
        if category_id == "CAT-09" and prio in ("P3", "P4"):
            return "P2", "security category floor"
        if risky("multiple_users_affected") and category_id in INCIDENT_CATEGORIES and prio in ("P3", "P4"):
            return "P2", "multi-user incident floor"
        return prio, "matrix"

    def ticket_type(self, cat_id: str) -> str:
        if cat_id == "CAT-09":
            return "Security incident"
        return "Service request" if cat_id in ("CAT-03", "CAT-07") else "Incident"

    def all_queues(self) -> list[str]:
        qs = {r["Primary_Queue"] for r in self.routing.values()}
        qs |= {r["Fallback_or_Approval_Queue"] for r in self.routing.values()}
        return sorted(qs)

    # ---------- ticket form (code mapping, no Jev) ----------
    def issue_types(self, cat_id: str) -> list[str]:
        return self.subcategories.get(cat_id, []) + ["Other"]

    def form_fields(self, cat_id: str) -> list[dict]:
        """Dynamic fields for the form; session/tool-supplied ones are filled automatically."""
        return [f for f in self.fields.get(cat_id, []) if f["Field_Name"] not in SESSION_SUPPLIED_FIELDS]

    @staticmethod
    def form_impact_urgency(impact_choice: str, workaround: str) -> tuple[str | None, str]:
        """Spec §9: Only me -> limited (moderate if no workaround) · Several users -> significant ·
        Dept/site or Company-wide -> extensive · Unknown -> None (Jev decides).
        Workaround No / Partial / Can continue -> urgency high / medium / low."""
        urgency = {"No workaround": "high", "Partial workaround": "medium",
                   "Work can continue": "low"}.get(workaround, "medium")
        impact = {"Only me": "moderate" if workaround == "No workaround" else "limited",
                  "Several users": "significant", "Department/site": "extensive",
                  "Company-wide": "extensive"}.get(impact_choice)
        return impact, urgency

    def queue(self, cat_id: str) -> str:
        if cat_id == OTHER_CATEGORY:
            return "Service Desk Duty Manager"
        return self.routing[cat_id]["Primary_Queue"]

    def sla_target(self, cat_id: str, priority: str) -> dict | None:
        if cat_id == OTHER_CATEGORY:  # no row of its own: the dataset's targets are the same per priority
            return next((v for (_c, p), v in sorted(self.sla.items()) if p == priority), None)
        return self.sla.get((cat_id, priority))

    # ---------- retrieval ----------
    def kb_for_categories(self, cat_ids: list[str]) -> list[KBArticle]:
        return [a for a in self.kb.values() if a.category_id in cat_ids]

    def fields_to_ask(self, cat_id: str) -> list[dict]:
        return [f for f in self.fields.get(cat_id, [])
                if f["Required"] == "Yes" and f["Field_Name"] not in SESSION_SUPPLIED_FIELDS]


@lru_cache(maxsize=1)
def get_knowledge() -> Knowledge:
    return Knowledge()
