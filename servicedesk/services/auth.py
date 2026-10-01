"""AuthService: login, sessions, roles, lockout, the "Can't sign in?" recovery path.

Login gives V1 (session assurance) only. It never grants V2+: resets, MFA changes
and wipes still go through the identity tool (TOOL-03). Every attempt is audited.

Token = JWT (sub, role, queues, jti) signed with JWT_SECRET. Only its SHA-256 is
stored in `auth_sessions`, which enforces the 8h absolute and 30-minute idle expiry
and lets logout revoke it. Swap this class for an OIDC provider in production.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from .. import config, tools
from ..knowledge import Knowledge
from ..directory import employee_profile
from ..store import Store, now, parse_ts

# IT staff are employees too: same ID format (EMP2xxx range), full names, work emails, job titles.
# One agent per queue (routing_matrix.json + the duty manager queue), a second Network Remote Access agent so
# two people can race for one ticket (scenarios 6-7), and one supervisor.
# (employee ID, full name, job title, role, queues or None for all, Phase 2.0 ID it replaces)
STAFF = [
    ("EMP2001", "Lakshmi Narayanan", "Service Desk Supervisor", "supervisor", None, "SUP-01"),
    ("EMP2002", "Priya Sharma", "IT Support Engineer", "agent", ["Application Access"], "AGT-01"),
    ("EMP2003", "Arjun Menon", "IT Support Engineer", "agent", ["Collaboration Support"], "AGT-02"),
    ("EMP2004", "Meera Pillai", "Desktop Support Engineer", "agent", ["End User Hardware"], "AGT-03"),
    ("EMP2005", "Rahul Verma", "Identity Security Analyst", "agent", ["Identity Security"], "AGT-04"),
    ("EMP2006", "Kavya Reddy", "Identity Support Engineer", "agent", ["Identity Support"], "AGT-05"),
    ("EMP2007", "Vikram Singh", "Messaging Engineer", "agent", ["Messaging Support"], "AGT-06"),
    ("EMP2008", "Divya Nair", "Mobile Device Engineer", "agent", ["Mobile Device Management"], "AGT-07"),
    ("EMP2009", "Karthik Rao", "Network Engineer", "agent", ["Network Operations"], "AGT-08"),
    ("EMP2010", "Ananya Iyer", "Network Engineer", "agent", ["Network Remote Access"], "AGT-09"),
    ("EMP2011", "Rohan Gupta", "Security Operations Analyst", "agent", ["Security Operations"], "AGT-10"),
    ("EMP2012", "Sneha Kulkarni", "Service Desk Duty Manager", "agent", ["Service Desk Duty Manager"], "AGT-11"),
    ("EMP2013", "Aditya Joshi", "Software Asset Analyst", "agent", ["Software Asset Management"], "AGT-12"),
    ("EMP2014", "Nisha Kapoor", "Workplace Support Engineer", "agent", ["Workplace/Print Support"], "AGT-13"),
    ("EMP2015", "Samuel Thomas", "Network Engineer", "agent", ["Network Remote Access", "Network Operations"],
     "AGT-99"),
]

# every column that stores a user ID, for renaming old staff IDs on an existing database
_USER_ID_COLUMNS = [("tickets", "owner"), ("tickets", "assigned_to"), ("tickets", "updated_by"),
                    ("events", "actor"), ("messages", "author"), ("checklists", "done_by"),
                    ("alerts", "acked_by"), ("corrections", "corrected_by"), ("watchers", "user_id"),
                    ("notifications", "user_id"), ("shift_checklists", "user_id")]


class AuthError(Exception):
    status_code = 401


class Locked(AuthError):
    status_code = 423


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


ROLES = ("employee", "agent", "supervisor")

# The most-used passwords that still pass a length rule. Real deployments can extend this list via
# PASSWORD_BLOCKLIST_FILE (one password per line).
_COMMON = {"password1234", "password12345", "passw0rd1234", "welcome12345", "qwerty123456", "123456789012",
           "letmein12345", "changeme1234", "administrator", "iloveyou1234", "company12345", "servicedesk1"}


def _blocklist() -> set[str]:
    words = set(_COMMON)
    if config.PASSWORD_BLOCKLIST_FILE:
        try:
            with open(config.PASSWORD_BLOCKLIST_FILE, encoding="utf-8") as f:
                words |= {w.strip().lower() for w in f if w.strip()}
        except OSError:
            pass
    return words


def password_problems(pw: str, user_id: str = "", email: str = "") -> list[str]:
    """Length-first policy (NIST SP 800-63B): long enough, not common, not the user's own ID or email.
    No forced symbol/digit mix and no forced expiry; those push people to weaker, predictable passwords."""
    problems = []
    if len(pw) < config.PASSWORD_MIN_LENGTH:
        problems.append(f"must be at least {config.PASSWORD_MIN_LENGTH} characters")
    if len(pw) > 128:
        problems.append("must be at most 128 characters")
    low = pw.lower()
    if low in _blocklist():
        problems.append("is too common")
    if user_id and user_id.lower() in low or email and email.split("@")[0].lower() in low:
        problems.append("must not contain your employee ID or email name")
    if len(set(pw)) < 5:
        problems.append("is too repetitive")
    return problems


def temporary_password() -> str:
    """16 random characters. Valid only until the first sign-in, which forces a change."""
    return secrets.token_urlsafe(12)


_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt())


class AuthService:
    def __init__(self, store: Store, k: Knowledge):
        self.store, self.k = store, k

    # ================================================================ users
    def seed(self, password: str) -> dict:
        """Create users once: 50 employees plus the IT staff. Existing passwords are kept; staff profiles
        (name, title, email, queues) are refreshed, and Phase 2.0 staff IDs (AGT-09…) are renamed in place."""
        pw = hash_pw(password)
        created = 0
        rows = [(e["Employee_ID"], e["Full_Name"], None, "employee", "[]", e["Email"])
                for e in self.k.employees.values()]
        for uid, name, title, role, queues, _old in STAFF:
            email = name.lower().replace(" ", ".") + "@company.example"
            rows.append((uid, name, title, role, json.dumps(queues or self.k.all_queues()), email))
        emp_profiles = {e["Employee_ID"]: e for e in self.k.employees.values()}
        with self.store.tx() as c:
            for uid, *_rest, old in STAFF:
                if c.execute("SELECT 1 FROM users WHERE user_id=?", (old,)).fetchone() and \
                        not c.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone():
                    self._rename(c, old, uid)
            for uid, name, title, role, queues, email in rows:
                created += c.execute("INSERT OR IGNORE INTO users (user_id,employee_id,name,role,queues,email,"
                                     "job_title,pw_hash,failed_count,status,must_change_pw,created_at) "
                                     "VALUES (?,?,?,?,?,?,?,?,0,'active',0,?)",
                                     (uid, uid, name, role, queues, email, title, pw, now())).rowcount
                if role != "employee":
                    c.execute("UPDATE users SET employee_id=?,name=?,role=?,queues=?,email=?,job_title=? "
                              "WHERE user_id=?", (uid, name, role, queues, email, title, uid))
                elif uid in emp_profiles:  # profile fields live in the directory, not only in the dataset
                    e = emp_profiles[uid]
                    c.execute("UPDATE users SET department=COALESCE(department,?), location=COALESCE(location,?), "
                              "asset_tag=COALESCE(asset_tag,?), manager_id=COALESCE(manager_id,?), "
                              "job_title=COALESCE(job_title,?) WHERE user_id=?",
                              (e.get("Department"), e.get("Location"), e.get("Asset_Tag"), e.get("Manager_ID"),
                               e.get("Job_Title"), uid))
        return {"created": created, "total": len(rows)}

    def _rename(self, c, old: str, new: str) -> None:
        """Move an old staff ID to its employee ID everywhere it is stored. Old sessions are signed out."""
        # rows that would collide with a primary key after the rename are dropped first
        c.execute("DELETE FROM watchers WHERE user_id=? AND ticket_id IN "
                  "(SELECT ticket_id FROM watchers WHERE user_id=?)", (old, new))
        c.execute("DELETE FROM shift_checklists WHERE user_id=? AND EXISTS (SELECT 1 FROM shift_checklists s "
                  "WHERE s.user_id=? AND s.shift_date=shift_checklists.shift_date "
                  "AND s.item_id=shift_checklists.item_id)", (old, new))
        with self.store.audit_rewrite(c):  # audit rows keep their meaning: the same person, new ID
            for table, col in _USER_ID_COLUMNS:
                if col in self.store.columns(table, c):
                    c.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (new, old))
            c.execute("INSERT INTO events (ticket_id,session_id,event_type,payload_json,created_at,actor) "
                      "VALUES (NULL,NULL,'Audit rewrite',?,?,'system')",
                      (json.dumps({"detail": f"legacy staff ID {old} renamed to {new}"}), now()))
        c.execute("DELETE FROM auth_sessions WHERE user_id=?", (old,))
        c.execute("UPDATE users SET user_id=? WHERE user_id=?", (new, old))

    def user(self, user_id: str) -> dict | None:
        r = self._find(user_id)
        return self._public(r) if r else None

    def _find(self, login_id: str) -> dict | None:
        """Look a user up by employee ID (any case) or work email."""
        login_id = (login_id or "").strip()
        return self.store.one("SELECT * FROM users WHERE lower(user_id)=lower(?) OR lower(email)=lower(?)",
                              (login_id, login_id))

    @staticmethod
    def _public(r: dict) -> dict:
        return {"user_id": r["user_id"], "employee_id": r["employee_id"], "name": r["name"], "role": r["role"],
                "queues": json.loads(r["queues"] or "[]"), "email": r["email"],
                "job_title": r.get("job_title"), "department": r.get("department"), "location": r.get("location"),
                "status": r.get("status") or "active", "must_change_password": bool(r.get("must_change_pw")),
                "locked": bool(r.get("locked_until") and parse_ts(r["locked_until"]) > datetime.now(timezone.utc))}

    def users(self, role: str | None = None, include_disabled: bool = True) -> list[dict]:
        where = ["role=?"] * bool(role) + ["COALESCE(status,'active')='active'"] * (not include_disabled)
        rows = self.store.query("SELECT * FROM users" + (" WHERE " + " AND ".join(where) if where else "") +
                                " ORDER BY role DESC, name", (role,) if role else ())
        return [self._public(r) for r in rows]

    # ================================================================ account administration
    def create_user(self, user_id: str, name: str, email: str, role: str = "employee",
                    queues: list[str] | None = None, job_title: str | None = None, department: str | None = None,
                    location: str | None = None, asset_tag: str | None = None, manager_id: str | None = None,
                    by: str | None = None, password: str | None = None) -> str:
        """Create an account. Returns a one-time temporary password (the user must change it at first
        sign-in) unless `password` is given, which must meet the policy."""
        user_id, email = user_id.strip().upper(), email.strip().lower()
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{1,31}", user_id):
            raise ValueError("Employee ID: 2–32 letters, digits, - or _")
        if "@" not in email:
            raise ValueError("A work email is required")
        if self._find(user_id) or self._find(email):
            raise ValueError(f"{user_id} or {email} already exists")
        bad = [q for q in (queues or []) if q not in self.k.all_queues()]
        if bad:
            raise ValueError(f"Unknown queue(s): {', '.join(bad)}")
        if password:
            problems = password_problems(password, user_id, email)
            if problems:
                raise ValueError("; ".join(problems))
        temp = password or temporary_password()
        queues_json = json.dumps(self.k.all_queues() if role == "supervisor" else (queues or []))
        self.store.execute(
            "INSERT INTO users (user_id,employee_id,name,role,queues,email,pw_hash,failed_count,job_title,"
            "department,location,asset_tag,manager_id,status,must_change_pw,created_at) "
            "VALUES (?,?,?,?,?,?,?,0,?,?,?,?,?,'active',?,?)",
            (user_id, user_id, name.strip(), role, queues_json, email, hash_pw(temp), job_title, department,
             location, asset_tag, manager_id, int(not password), now()))
        self.audit("user_created", user_id, role=role, by=by)
        return temp

    def update_user(self, user_id: str, by: str | None = None, **fields) -> dict:
        """Change profile, role or queues. Signing-in fields (password, status) have their own methods."""
        r = self._find(user_id)
        if not r:
            raise KeyError(user_id)
        allowed = {"name", "email", "role", "queues", "job_title", "department", "location", "asset_tag",
                   "manager_id"}
        changes = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if "role" in changes and changes["role"] not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        if "queues" in changes:
            bad = [q for q in changes["queues"] if q not in self.k.all_queues()]
            if bad:
                raise ValueError(f"Unknown queue(s): {', '.join(bad)}")
            changes["queues"] = json.dumps(changes["queues"])
        if changes.get("role") == "supervisor":
            changes["queues"] = json.dumps(self.k.all_queues())
        if changes:
            self.store.execute(f"UPDATE users SET {','.join(f'{k}=?' for k in changes)} WHERE user_id=?",
                               (*changes.values(), r["user_id"]))
            if "role" in changes or "queues" in changes:
                self.revoke_sessions(r["user_id"])  # the token carries role + queues: sign in again
            self.audit("user_updated", r["user_id"], fields=sorted(changes), by=by)
        return self.user(r["user_id"]) or {}

    def import_users(self, rows: list[dict], by: str | None = None) -> dict:
        """Bulk create from HR data (CSV columns: employee_id, name, email, role, queues, job_title,
        department, location, asset_tag, manager_id). Existing IDs are updated, never duplicated.
        Returns the created accounts with their temporary passwords, to hand out securely."""
        created, updated, errors = [], 0, []
        for i, row in enumerate(rows, 2):  # row 1 is the CSV header
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            uid = row.get("employee_id", "").upper()
            queues = [q.strip() for q in row.get("queues", "").split(";") if q.strip()]
            profile = {k: row.get(k) or None for k in ("job_title", "department", "location", "asset_tag",
                                                       "manager_id")}
            try:
                if self._find(uid):
                    self.update_user(uid, by=by, name=row.get("name") or None, email=row.get("email") or None,
                                     role=row.get("role") or None, queues=queues or None, **profile)
                    updated += 1
                else:
                    temp = self.create_user(uid, row.get("name", ""), row.get("email", ""),
                                            row.get("role") or "employee", queues, by=by, **profile)
                    created.append({"employee_id": uid, "email": row.get("email"), "temporary_password": temp})
            except (ValueError, KeyError) as e:
                errors.append(f"line {i} ({uid or 'no ID'}): {e}")
        return {"created": created, "updated": updated, "errors": errors}

    def reset_password(self, user_id: str, by: str | None = None) -> str:
        """Admin reset after the person's identity was verified out of band (e.g. a call-back to the
        number on file). Returns a one-time temporary password; all sessions are signed out."""
        r = self._find(user_id)
        if not r:
            raise KeyError(user_id)
        temp = temporary_password()
        self.store.execute("UPDATE users SET pw_hash=?, must_change_pw=1, failed_count=0, locked_until=NULL "
                           "WHERE user_id=?", (hash_pw(temp), r["user_id"]))
        self.revoke_sessions(r["user_id"])
        self.audit("password_reset_by_admin", r["user_id"], by=by)
        return temp

    def set_status(self, user_id: str, active: bool, by: str | None = None) -> None:
        """Disable a leaver (or re-enable). Disabling signs them out everywhere. Nothing is deleted (D9)."""
        r = self._find(user_id)
        if not r:
            raise KeyError(user_id)
        self.store.execute("UPDATE users SET status=? WHERE user_id=?",
                           ("active" if active else "disabled", r["user_id"]))
        if not active:
            self.revoke_sessions(r["user_id"])
        self.audit("user_enabled" if active else "user_disabled", r["user_id"], by=by)

    def unlock(self, user_id: str, by: str | None = None) -> None:
        r = self._find(user_id)
        if not r:
            raise KeyError(user_id)
        self.store.execute("UPDATE users SET failed_count=0, locked_until=NULL WHERE user_id=?", (r["user_id"],))
        self.audit("user_unlocked", r["user_id"], by=by)

    def change_password(self, user_id: str, current: str, new: str, keep_token: str | None = None) -> None:
        """The user changes their own password (required after a temporary one). Other sessions end."""
        r = self._find(user_id)
        if not r or not bcrypt.checkpw(current.encode(), r["pw_hash"].encode()):
            self.audit("password_change_failed", user_id)
            raise AuthError("Current password is wrong")
        problems = password_problems(new, r["user_id"], r["email"])
        if bcrypt.checkpw(new.encode(), r["pw_hash"].encode()):
            problems.append("must differ from the current password")
        if problems:
            raise ValueError("New password " + "; ".join(problems))
        self.store.execute("UPDATE users SET pw_hash=?, must_change_pw=0, pw_changed_at=? WHERE user_id=?",
                           (hash_pw(new), now(), r["user_id"]))
        self.revoke_sessions(r["user_id"], keep=_sha(keep_token) if keep_token else None)
        self.audit("password_changed", r["user_id"])

    def revoke_sessions(self, user_id: str, keep: str | None = None) -> None:
        self.store.execute("UPDATE auth_sessions SET expires_at=? WHERE user_id=? AND expires_at>? AND "
                           "token_hash<>?", (now(), user_id, now(), keep or ""))

    def audit(self, event: str, user_id: str | None, **data) -> None:
        self.store.log_event(event, {"detail": f"{event} {user_id or ''}".strip(), "user_id": user_id, **data},
                             actor=user_id)

    # ================================================================ login / logout
    def login(self, login_id: str, password: str) -> dict:
        r = self._find(login_id)
        if not r:
            bcrypt.checkpw(b"x", _DUMMY_HASH)  # same cost either way: no user-enumeration timing
            self.audit("login_failed", login_id, reason="unknown user")
            raise AuthError("Invalid ID or password")
        uid = r["user_id"]
        locked = parse_ts(r["locked_until"])
        if locked and locked > datetime.now(timezone.utc):
            self.audit("login_failed", uid, reason="locked")
            raise Locked(f"Account locked until {locked.strftime('%H:%M')} UTC after "
                         f"{config.LOCKOUT_FAILURES} failed attempts")
        if not bcrypt.checkpw(password.encode(), r["pw_hash"].encode()):
            fails = (r["failed_count"] or 0) + 1
            if fails >= config.LOCKOUT_FAILURES:
                until = (datetime.now(timezone.utc) + timedelta(minutes=config.LOCKOUT_MINUTES)).isoformat(
                    timespec="seconds")
                self.store.execute("UPDATE users SET failed_count=0, locked_until=? WHERE user_id=?", (until, uid))
                self.audit("locked", uid, until=until)
                raise Locked(f"Too many failed attempts. Account locked for {config.LOCKOUT_MINUTES} minutes.")
            self.store.execute("UPDATE users SET failed_count=? WHERE user_id=?", (fails, uid))
            self.audit("login_failed", uid, failed_count=fails)
            raise AuthError("Invalid ID or password")
        if (r.get("status") or "active") != "active":
            self.audit("login_failed", uid, reason="disabled")
            raise AuthError("Invalid ID or password")  # same message: don't reveal that the account exists
        self.store.execute("UPDATE users SET failed_count=0, locked_until=NULL WHERE user_id=?", (uid,))
        user = self._public(r)
        exp = datetime.now(timezone.utc) + timedelta(hours=config.SESSION_ABSOLUTE_HOURS)
        token = jwt.encode({"sub": uid, "role": user["role"], "queues": user["queues"],
                            "jti": secrets.token_hex(16), "exp": exp}, config.JWT_SECRET, algorithm="HS256")
        self.store.execute("INSERT INTO auth_sessions (token_hash,user_id,created_at,last_seen,expires_at) VALUES (?,?,?,?,?)",
                           (_sha(token), uid, now(), now(), exp.isoformat(timespec="seconds")))
        self.audit("login_success", uid, role=user["role"])
        return {"token": token, "user": user, "expires_at": exp.isoformat(timespec="seconds")}

    def authenticate(self, token: str) -> dict:
        """Identity comes from here only: signature + expiry + live session row + idle timeout."""
        try:
            claims = jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
        except jwt.PyJWTError as e:
            raise AuthError(f"Invalid token: {e}") from e
        row = self.store.one("SELECT * FROM auth_sessions WHERE token_hash=?", (_sha(token),))
        nowdt = datetime.now(timezone.utc)
        if not row or parse_ts(row["expires_at"]) <= nowdt:
            raise AuthError("Session ended, please sign in again")
        if nowdt - parse_ts(row["last_seen"]) > timedelta(minutes=config.SESSION_IDLE_MINUTES):
            self.store.execute("UPDATE auth_sessions SET expires_at=? WHERE token_hash=?", (now(), row["token_hash"]))
            self.audit("session_idle_timeout", claims["sub"])
            raise AuthError("Signed out after 30 minutes of inactivity")
        if nowdt - parse_ts(row["last_seen"]) > timedelta(seconds=30):  # throttle writes
            self.store.execute("UPDATE auth_sessions SET last_seen=? WHERE token_hash=?", (now(), row["token_hash"]))
        user = self.user(claims["sub"])
        if not user or user["status"] != "active":
            raise AuthError("This account is not active")
        return user

    def logout(self, token: str, user_id: str) -> None:
        self.store.execute("UPDATE auth_sessions SET expires_at=? WHERE token_hash=?", (now(), _sha(token)))
        self.audit("logout", user_id)

    def online_agents(self, queue: str) -> list[str]:
        """Agents whose session was used in the last few minutes and who work this queue."""
        since = (datetime.now(timezone.utc) - timedelta(minutes=config.ONLINE_WINDOW_MINUTES)).isoformat(
            timespec="seconds")
        rows = self.store.query("SELECT DISTINCT u.user_id, u.queues FROM auth_sessions s JOIN users u ON "
                                "u.user_id=s.user_id WHERE u.role='agent' AND s.last_seen>=? AND s.expires_at>?",
                                (since, now()))
        return [r["user_id"] for r in rows if queue in json.loads(r["queues"] or "[]")]

    # ================================================================ "Can't sign in?" (diagram 05)
    def recover(self, login_id: str, problem: str, has_registered_factor: bool, tickets=None) -> dict:
        """Password / MFA only. V2 step-up on the registered authenticator → reset link, no session.
        Failed or no factor → human V3 supervised recovery. Replies are generic (no user enumeration)."""
        generic = {"outcome": "submitted",
                   "message": "If that ID exists, we've sent a sign-in prompt to its registered authenticator. "
                              "Follow the link it gives you. You still need to sign in normally afterwards."}
        r = self._find(login_id)
        if not r or r["role"] != "employee":
            self.audit("recovery_requested", login_id, known=False)
            return generic
        uid = r["user_id"]
        recent = self.store.one("SELECT COUNT(*) n FROM events WHERE event_type LIKE 'recovery_%' AND actor=? "
                                "AND created_at>=?", (uid, (datetime.now(timezone.utc) - timedelta(hours=1))
                                                     .isoformat(timespec="seconds")))
        if recent and recent["n"] >= 5:
            self.audit("recovery_rate_limited", uid)
            return generic
        emp = employee_profile(self.store, self.k, uid)
        if has_registered_factor and config.SELF_SERVICE_RESET:
            ver = tools.verify_identity(emp, f"self-service {problem}")
            if ver["status"] == "VERIFIED":
                fn = tools.reset_password if problem == "password" else tools.mfa_reset
                res = fn(emp, ver)
                self.store.execute("UPDATE users SET failed_count=0, locked_until=NULL WHERE user_id=?", (uid,))
                self.audit("recovery_reset_done", uid, problem=problem, verification=ver["verification_id"],
                           tool=res["tool"], correlation_id=res.get("correlation_id"))
                return {"outcome": "reset_done", "message": "Verified on your registered authenticator "
                        "(simulated). A one-time reset link was sent to your registered contact. "
                        "No session was created, so sign in normally once you've reset it."}
        # V3: supervised recovery by Identity Security (a ticket, never a session). Without a real identity
        # provider (production default) every recovery takes this path.
        no_factor = not has_registered_factor
        why = ("no registered factor available (V3)" if no_factor else
               "self-service reset is not connected; verify identity, then reset (V3)")
        tid = None
        if tickets is not None:
            tid = tickets.create(session_id=None, employee_id=uid, category_id="CAT-01" if
                                 problem == "password" else "CAT-05", impact="moderate", urgency="high", flags={},
                                 summary=f"Can't sign in: {problem}, {why}",
                                 channel="Portal", opened_by="Recovery page", actor=uid)
            tickets.store.update_ticket(tid, queue="Identity Security", verification="V3 required")
            tickets.escalate(tid, f"Sign-in recovery: {why}", {
                "requester": uid, "problem": problem, "verification": "V3 required"}, actor=uid)
        self.audit("recovery_v3", uid, problem=problem, ticket_id=tid)
        lead = "We couldn't verify you with a registered factor, so " if no_factor else ""
        return {"outcome": "human_v3", "ticket_id": tid,
                "message": f"{lead}{'i' if lead else 'I'}dentity Security will verify who you are and reset "
                           "your sign-in. They'll contact you through a verified channel (for example a call to "
                           "the number on file) and give you a temporary password."}
