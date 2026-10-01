"""KnowledgeService: employee-facing versions of KB articles, their approval, and saved replies.

The bot only ever says approved text. A version goes draft → (meaning check) → approved; approving retires
the previous approved version, so there is exactly one live version per article and a full history.
The meaning check asks Jev, for each attempt the employee performs, whether the rewrite asks for the same
thing as the original article and whether it adds an action. A supervisor may approve despite a failed check
only with a written reason, which is audited.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from ..knowledge import Knowledge
from ..store import Store, now

DRAFTS_FILE = Path(__file__).resolve().parent.parent / "content" / "kb_employee_drafts.json"
SAME_MIN, ADDS_MAX = 0.5, 0.5
_CACHE_SECONDS = 30  # api and worker are separate processes: a short cache picks up approvals quickly


class KBError(Exception):
    status_code = 422


def _step_text(step: dict | None) -> str:
    """Plain text of one attempt, for the meaning check and for 'last instructions'."""
    if not step:
        return ""
    if step.get("who") == "it":
        return step.get("note", "")
    return " ".join(filter(None, [step.get("intro", "")] + list(step.get("steps") or [])))


class KnowledgeService:
    def __init__(self, store: Store, k: Knowledge, brain=None):
        self.store, self.k, self.brain = store, k, brain
        self._live: dict[str, dict | None] = {}
        self._live_at = 0.0

    # ================================================================ versions
    def seed_drafts(self, approve: bool = False, by: str = "seed") -> int:
        """Load the shipped drafts as version 1 for articles that have no version yet. Development can
        auto-approve them so the demo shows the new wording; production leaves them for a supervisor."""
        drafts = {k: v for k, v in json.loads(DRAFTS_FILE.read_text(encoding="utf-8")).items() if k in self.k.kb}
        have = {r["kb_id"] for r in self.store.query("SELECT DISTINCT kb_id FROM kb_versions")}
        n = 0
        for kb_id, d in drafts.items():
            if kb_id in have:
                continue
            status = "approved" if approve else "draft"
            self.store.execute(
                "INSERT INTO kb_versions (kb_id,version,status,title,summary,why,attempt1_json,attempt2_json,"
                "handoff_message,change_note,author,created_at,approved_by,approved_at) "
                "VALUES (?,1,?,?,?,?,?,?,?,?,?,?,?,?)",
                (kb_id, status, d.get("title"), d.get("summary"), None, json.dumps(d.get("attempt1")),
                 json.dumps(d.get("attempt2")), d.get("handoff_message"), "Initial employee-friendly draft",
                 by, now(), by if approve else None, now() if approve else None))
            n += 1
        self._live_at = 0
        return n

    @staticmethod
    def _row(r: dict | None) -> dict | None:
        if not r:
            return None
        return {**{k: v for k, v in r.items() if not k.endswith("_json")},
                "attempt1": json.loads(r["attempt1_json"] or "null"),
                "attempt2": json.loads(r["attempt2_json"] or "null"),
                "meaning_check": json.loads(r["meaning_check_json"] or "null")}

    def live(self, kb_id: str) -> dict | None:
        """The approved employee version, or None (the bot then uses the plain article, minus agent notes)."""
        if time.monotonic() - self._live_at > _CACHE_SECONDS:
            rows = self.store.query("SELECT * FROM kb_versions WHERE status='approved'")
            self._live = {r["kb_id"]: self._row(r) for r in rows}
            self._live_at = time.monotonic()
        return self._live.get(kb_id)

    def version(self, version_id: int) -> dict:
        v = self._row(self.store.one("SELECT * FROM kb_versions WHERE id=?", (version_id,)))
        if not v:
            raise KBError("Version not found")
        return v

    def versions(self, kb_id: str) -> list[dict]:
        return [self._row(r) for r in self.store.query("SELECT * FROM kb_versions WHERE kb_id=? ORDER BY version "
                                                         "DESC", (kb_id,))]

    def articles(self) -> list[dict]:
        """One row per KB article: live version, pending draft, and how helpful employees found it."""
        latest = {}
        for r in self.store.query("SELECT kb_id, version, status FROM kb_versions ORDER BY version"):
            latest.setdefault(r["kb_id"], {})[r["status"]] = r["version"]
        fb = {r["kb_id"]: r for r in self.store.query(
            "SELECT kb_id, SUM(CASE WHEN helpful=1 THEN 1 ELSE 0 END) AS up, "
            "SUM(CASE WHEN helpful=0 THEN 1 ELSE 0 END) AS down FROM answer_feedback GROUP BY kb_id")}
        out = []
        for kb_id, a in self.k.kb.items():
            st = latest.get(kb_id, {})
            up, down = (fb.get(kb_id) or {}).get("up") or 0, (fb.get(kb_id) or {}).get("down") or 0
            out.append({"kb_id": kb_id, "title": a.title, "category": self.k.name(a.category_id),
                        "handling": a.handling_mode, "live_version": st.get("approved"),
                        "draft_version": st.get("draft"), "helpful": up, "not_helpful": down,
                        "helpful_rate": round(up / (up + down), 3) if up + down else None})
        return out

    def save_draft(self, kb_id: str, data: dict, author: str, note: str = "") -> dict:
        if kb_id not in self.k.kb:
            raise KBError("Unknown article")
        for key in ("attempt1", "attempt2"):
            step = data.get(key)
            if step is None:
                continue
            if step.get("who") not in ("you", "it"):
                raise KBError(f"{key}: who must be 'you' or 'it'")
            if step["who"] == "you" and not [s for s in step.get("steps") or [] if s.strip()]:
                raise KBError(f"{key}: add at least one step")
        if not (data.get("summary") or "").strip():
            raise KBError("Add a short summary: what's happening, in plain words")
        n = (self.store.one("SELECT MAX(version) AS v FROM kb_versions WHERE kb_id=?", (kb_id,)) or {}).get("v") or 0
        self.store.execute(
            "INSERT INTO kb_versions (kb_id,version,status,title,summary,why,attempt1_json,attempt2_json,"
            "handoff_message,change_note,author,created_at) VALUES (?,?,'draft',?,?,?,?,?,?,?,?,?)",
            (kb_id, n + 1, data.get("title") or self.k.kb[kb_id].title, data["summary"].strip(), None,
             json.dumps(data.get("attempt1")), json.dumps(data.get("attempt2")), data.get("handoff_message"),
             note or "Edited", author, now()))
        self.store.log_event("KB draft saved", {"detail": f"{kb_id} v{n + 1} by {author}"}, actor=author)
        return self.versions(kb_id)[0]

    def check(self, version_id: int) -> dict:
        """Meaning check: every employee-performed attempt against the original article's attempt."""
        v = self.version(version_id)
        art = self.k.kb[v["kb_id"]]
        pairs, labels = [], []
        for n, original in ((1, art.attempt_1), (2, art.attempt_2)):
            step = v[f"attempt{n}"]
            if step and step.get("who") == "you" and original:
                pairs.append((original, _step_text(step)))
                labels.append(n)
        results = self.brain.check_rewrite(pairs) if pairs and self.brain else []
        items = [{"attempt": n, "original": o, "rewrite": r, "same": round(res["same"], 3),
                  "adds": round(res["adds"], 3), "ok": res["same"] >= SAME_MIN and res["adds"] < ADDS_MAX}
                 for n, (o, r), res in zip(labels, pairs, results)]
        check = {"passed": all(i["ok"] for i in items), "items": items, "checked_at": now()}
        self.store.execute("UPDATE kb_versions SET meaning_check_json=? WHERE id=?", (json.dumps(check), version_id))
        return check

    def approve(self, version_id: int, user: dict, override_reason: str | None = None) -> dict:
        v = self.version(version_id)
        if v["status"] != "draft":
            raise KBError(f"Only drafts can be approved (this one is {v['status']})")
        check = v["meaning_check"] or self.check(version_id)
        if not check["passed"] and not (override_reason or "").strip():
            raise KBError("The meaning check flagged a step. Fix the draft, or approve with a reason if you've "
                          "checked it yourself.")
        with self.store.tx() as c:
            c.execute("UPDATE kb_versions SET status='retired' WHERE kb_id=? AND status='approved'", (v["kb_id"],))
            c.execute("UPDATE kb_versions SET status='approved', approved_by=?, approved_at=? WHERE id=?",
                      (user["user_id"], now(), version_id))
        self.store.log_event("KB approved", {"detail": f"{v['kb_id']} v{v['version']} approved by {user['name']}"
                                             + (f" (check overridden: {override_reason})" if not check["passed"]
                                                else ""), "meaning_check_passed": check["passed"]},
                             actor=user["user_id"])
        self._live_at = 0
        return self.version(version_id)

    # ================================================================ knowledge gaps
    def gaps(self, since: str) -> list[dict]:
        """Questions no article answered, grouped by category, most frequent first, with a few examples.
        The list of what to write next."""
        rows = self.store.query("SELECT category_id, question, best_kb, best_score, created_at FROM knowledge_gaps "
                                "WHERE created_at>=? ORDER BY created_at DESC", (since,))
        groups: dict[str, dict] = {}
        for r in rows:
            g = groups.setdefault(r["category_id"] or "CAT-OTHER", {
                "category_id": r["category_id"], "category": self.k.name(r["category_id"]) if r["category_id"] in
                self.k.categories else "Unclear / other", "count": 0, "examples": [], "nearest": {}})
            g["count"] += 1
            if len(g["examples"]) < 5 and r["question"]:
                g["examples"].append({"question": r["question"], "at": r["created_at"]})
            if r["best_kb"]:
                g["nearest"][r["best_kb"]] = g["nearest"].get(r["best_kb"], 0) + 1
        out = sorted(groups.values(), key=lambda g: -g["count"])
        for g in out:
            g["nearest"] = [{"kb_id": kid, "title": self.k.kb[kid].title, "times": n}
                            for kid, n in sorted(g["nearest"].items(), key=lambda x: -x[1])[:3] if kid in self.k.kb]
        return out

    # ================================================================ saved replies (agents)
    def saved_replies(self, category_id: str | None = None, include_inactive: bool = False) -> list[dict]:
        rows = self.store.query("SELECT * FROM saved_replies" + ("" if include_inactive else " WHERE active=1") +
                                " ORDER BY title")
        return [r for r in rows if not category_id or not r["category_id"] or r["category_id"] == category_id]

    def save_reply(self, user: dict, title: str, body: str, category_id: str | None = None,
                   reply_id: int | None = None, active: bool = True) -> dict:
        if not title.strip() or not body.strip():
            raise KBError("A saved reply needs a title and text")
        if reply_id:
            self.store.execute("UPDATE saved_replies SET title=?, body=?, category_id=?, active=?, updated_at=? "
                               "WHERE id=?", (title.strip(), body.strip(), category_id, int(active), now(), reply_id))
        else:
            self.store.execute("INSERT INTO saved_replies (title,body,category_id,active,created_by,created_at,"
                               "updated_at) VALUES (?,?,?,?,?,?,?)",
                               (title.strip(), body.strip(), category_id, int(active), user["user_id"], now(), now()))
        return next(r for r in self.saved_replies(include_inactive=True) if r["title"] == title.strip())

    def seed_saved_replies(self) -> None:
        if self.store.one("SELECT 1 AS x FROM saved_replies LIMIT 1"):
            return
        seed = [
            ("Taking a look", "Hi {first_name}, thanks for your patience. I'm looking into {ticket_id} now and "
                              "I'll update you here as soon as I know more."),
            ("Please try this", "Hi {first_name}, could you try the following and let me know what happens?\n\n1. "),
            ("Fixed: please confirm", "Hi {first_name}, I've made the change on our side. Could you try again and "
                                      "confirm it's working? If it isn't, just reply here and I'll pick it up."),
            ("Need more detail", "Hi {first_name}, to help me get this right, could you tell me the exact error "
                                 "message (a screenshot is fine) and when it started?"),
            ("Waiting on another team", "Hi {first_name}, I've passed this to the team that can make the change. "
                                        "I'll keep an eye on it and update you here."),
        ]
        for title, body in seed:
            self.store.execute("INSERT INTO saved_replies (title,body,active,created_by,created_at,updated_at) "
                               "VALUES (?,?,1,'seed',?,?)", (title, body, now(), now()))


def fill_reply(body: str, ticket: dict, employee_name: str | None, agent: dict) -> str:
    """Placeholders in saved replies: {first_name} {ticket_id} {agent_name}."""
    first = (employee_name or "there").split()[0]
    return (body.replace("{first_name}", first).replace("{ticket_id}", ticket["ticket_id"])
            .replace("{agent_name}", agent["name"]))
