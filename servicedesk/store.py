"""Persistence: SQLite (development, tests) or PostgreSQL (production). FastAPI and the worker write here (D8).

All SQL is written once, SQLite style (`?` placeholders). For Postgres, `_pg_sql()` translates the few
differences (placeholders, INSERT OR IGNORE, AUTOINCREMENT) in one place.

Tables are grouped by area (AREAS). In Postgres each area is its own schema (identity, service, ops, audit,
knowledge, reporting) with its own access rights; the connection's search_path lets the same unqualified SQL
work. SQLite has no schemas, so there the area is only documentation. The audit area is append-only on both:
a database trigger rejects UPDATE and DELETE.

Start-up is idempotent (create missing schemas, tables, columns, indexes) and then applies numbered one-off
migrations recorded in `schema_migrations`.

Every multi-statement write runs inside `tx()`. SQLite takes the write lock up front (BEGIN IMMEDIATE);
Postgres takes one transaction-scoped advisory lock, so writers are serialised the same way on both.
Claims, version checks and ticket numbering stay atomic. Nothing is ever deleted (D9).
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .config import DATABASE_URL, DB_AUTO_MIGRATE, DB_PATH, DB_POOL_SIZE

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY, employee_id TEXT, state_json TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, ticket_id TEXT, role TEXT,
  text TEXT, meta_json TEXT, created_at TEXT, visibility TEXT DEFAULT 'customer', author TEXT);
CREATE TABLE IF NOT EXISTS tickets (
  ticket_id TEXT PRIMARY KEY, session_id TEXT, employee_id TEXT, created_at TEXT, updated_at TEXT,
  status TEXT, category_id TEXT, priority TEXT, impact TEXT, urgency TEXT, sub_agent TEXT,
  queue TEXT, kb_id TEXT, summary TEXT, attempts INTEGER DEFAULT 0, escalation_reason TEXT,
  handoff_json TEXT, assigned_to TEXT, verification TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT, session_id TEXT, event_type TEXT,
  payload_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS corrections (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT, field TEXT, old_value TEXT,
  new_value TEXT, corrected_by TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS users (
  user_id TEXT PRIMARY KEY, employee_id TEXT, name TEXT, role TEXT, queues TEXT, email TEXT,
  pw_hash TEXT, failed_count INTEGER DEFAULT 0, locked_until TEXT);
CREATE TABLE IF NOT EXISTS auth_sessions (
  token_hash TEXT PRIMARY KEY, user_id TEXT, created_at TEXT, last_seen TEXT, expires_at TEXT);
CREATE TABLE IF NOT EXISTS alerts (
  alert_id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT, event TEXT, level TEXT, priority TEXT,
  target_queue TEXT, text TEXT, created_at TEXT, next_alert_at TEXT, realert_count INTEGER DEFAULT 0,
  acked_by TEXT, acked_at TEXT, escalated_at TEXT, escalation_reason TEXT,
  UNIQUE (ticket_id, event));
CREATE TABLE IF NOT EXISTS checklists (
  ticket_id TEXT, item_id TEXT, text TEXT, required INTEGER, done_by TEXT, done_at TEXT,
  PRIMARY KEY (ticket_id, item_id));
CREATE TABLE IF NOT EXISTS watchers (ticket_id TEXT, user_id TEXT, PRIMARY KEY (ticket_id, user_id));
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, ticket_id TEXT, text TEXT, created_at TEXT,
  read_at TEXT);
CREATE TABLE IF NOT EXISTS bot_answers (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT, session_id TEXT, kb_id TEXT, category_id TEXT,
  attempt INTEGER, steps_text TEXT, outcome TEXT, created_at TEXT, outcome_at TEXT);
CREATE TABLE IF NOT EXISTS shift_checklists (
  user_id TEXT, shift_date TEXT, item_id TEXT, done_at TEXT, notes TEXT,
  PRIMARY KEY (user_id, shift_date, item_id));
CREATE TABLE IF NOT EXISTS heartbeats (name TEXT PRIMARY KEY, beat_at TEXT, info TEXT);
CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT);
CREATE TABLE IF NOT EXISTS kb_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kb_id TEXT, version INTEGER, status TEXT, title TEXT, summary TEXT,
  why TEXT, attempt1_json TEXT, attempt2_json TEXT, handoff_message TEXT, change_note TEXT, author TEXT,
  created_at TEXT, approved_by TEXT, approved_at TEXT, meaning_check_json TEXT, UNIQUE (kb_id, version));
CREATE TABLE IF NOT EXISTS answer_feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER, bot_answer_id INTEGER, ticket_id TEXT, kb_id TEXT,
  kb_version INTEGER, employee_id TEXT, helpful INTEGER, reason TEXT, comment TEXT, created_at TEXT,
  UNIQUE (message_id, employee_id));
CREATE TABLE IF NOT EXISTS ticket_ratings (
  ticket_id TEXT PRIMARY KEY, employee_id TEXT, score INTEGER, comment TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS attachments (
  id TEXT PRIMARY KEY, ticket_id TEXT, session_id TEXT, uploaded_by TEXT, filename TEXT, content_type TEXT,
  size_bytes INTEGER, sha256 TEXT, width INTEGER, height INTEGER, ocr_text TEXT, ocr_confidence REAL,
  error_codes TEXT, secrets_blurred INTEGER DEFAULT 0, status TEXT, created_at TEXT, delete_after TEXT,
  deleted_at TEXT);
CREATE TABLE IF NOT EXISTS saved_replies (
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, body TEXT, category_id TEXT, active INTEGER DEFAULT 1,
  created_by TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS knowledge_gaps (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT, session_id TEXT, category_id TEXT, question TEXT,
  best_kb TEXT, best_score REAL, created_at TEXT);
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT, recipient TEXT, subject TEXT, body TEXT, ticket_id TEXT,
  dedupe_key TEXT UNIQUE, status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0, last_error TEXT,
  created_at TEXT, sent_at TEXT);
"""

# Which area (Postgres schema) each table lives in. Access rights are granted per area (manage.py db-roles).
AREAS = {
    "identity": ["users", "auth_sessions"],
    "service": ["sessions", "tickets", "messages", "checklists", "watchers", "attachments", "ticket_ratings"],
    "ops": ["alerts", "notifications", "heartbeats", "shift_checklists", "outbox", "schema_migrations"],
    "audit": ["events", "corrections"],
    "knowledge": ["bot_answers", "kb_versions", "answer_feedback", "saved_replies", "knowledge_gaps"],
    "reporting": [],  # read-only views for BI tools (Postgres only)
}
AREA_OF = {t: a for a, ts in AREAS.items() for t in ts}
SEARCH_PATH = ",".join(AREAS) + ",public"
AUDIT_TABLES = AREAS["audit"]

# Created after migrations, since some index columns were added in Phase 2
INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_messages_session ON messages(session_id)",
    "CREATE INDEX IF NOT EXISTS ix_messages_ticket ON messages(ticket_id)",
    "CREATE INDEX IF NOT EXISTS ix_events_ticket ON events(ticket_id)",
    "CREATE INDEX IF NOT EXISTS ix_events_type_time ON events(event_type, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_tickets_employee ON tickets(employee_id)",
    "CREATE INDEX IF NOT EXISTS ix_tickets_status ON tickets(status)",
    "CREATE INDEX IF NOT EXISTS ix_tickets_queue ON tickets(queue)",
    "CREATE INDEX IF NOT EXISTS ix_tickets_owner ON tickets(owner)",
    "CREATE INDEX IF NOT EXISTS ix_alerts_open ON alerts(acked_at)",
    "CREATE INDEX IF NOT EXISTS ix_notifications_user ON notifications(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_auth_sessions_user ON auth_sessions(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_bot_answers_ticket ON bot_answers(ticket_id)",
    "CREATE INDEX IF NOT EXISTS ix_kb_versions_live ON kb_versions(kb_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_feedback_kb ON answer_feedback(kb_id)",
    "CREATE INDEX IF NOT EXISTS ix_attachments_ticket ON attachments(ticket_id)",
    "CREATE INDEX IF NOT EXISTS ix_attachments_expiry ON attachments(delete_after)",
    "CREATE INDEX IF NOT EXISTS ix_outbox_pending ON outbox(status)",
]

# Columns added in Phase 2 (ALTER TABLE on an existing Phase 1 database).
MIGRATIONS = {
    "tickets": {
        "subcategory": "TEXT", "ticket_type": "TEXT", "priority_source": "TEXT DEFAULT 'computed'",
        "priority_reason": "TEXT", "owner": "TEXT", "channel": "TEXT DEFAULT 'Chat'", "config_item": "TEXT",
        "location": "TEXT", "form_json": "TEXT", "resolution_code": "TEXT", "resolution_notes": "TEXT",
        "version": "INTEGER DEFAULT 1", "first_response_at": "TEXT", "claimed_at": "TEXT",
        "resolved_at": "TEXT", "reopened_count": "INTEGER DEFAULT 0", "incident_parent": "TEXT",
        "is_incident": "INTEGER DEFAULT 0", "triage_json": "TEXT", "jev_confidence": "REAL",
        "opened_by": "TEXT", "updated_by": "TEXT", "user_category": "TEXT",
        "redacted_at": "TEXT", "response_due": "TEXT", "resolve_due": "TEXT", "language": "TEXT",
    },
    "messages": {"visibility": "TEXT DEFAULT 'customer'", "author": "TEXT"},
    "events": {"actor": "TEXT"},
    "corrections": {"reason": "TEXT"},
    "checklists": {"position": "INTEGER"},
    "users": {"job_title": "TEXT", "department": "TEXT", "location": "TEXT", "asset_tag": "TEXT",
              "manager_id": "TEXT", "status": "TEXT DEFAULT 'active'", "must_change_pw": "INTEGER DEFAULT 0",
              "pw_changed_at": "TEXT", "created_at": "TEXT", "phone": "TEXT", "notify_email": "INTEGER DEFAULT 1"},
    "sessions": {"redacted_at": "TEXT"},
    "bot_answers": {"kb_version": "INTEGER"},
}

OPEN_EXCLUDED = ("RESOLVED", "RESOLVED_PENDING_CONFIRMATION", "CLOSED", "CANCELLED")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


def ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")


class VersionConflict(Exception):
    """Optimistic lock failed: somebody saved the ticket after you loaded it."""

    def __init__(self, ticket: dict):
        self.ticket = ticket
        super().__init__(f"{ticket.get('updated_by') or 'Someone'} updated this, reload")


_WRITE_LOCK = 72_4201  # Postgres advisory lock id that serialises writers, like SQLite's single writer
_INSERT_IGNORE = re.compile(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO", re.I)


def _pg_sql(sql: str, has_args: bool) -> str:
    """SQLite-style SQL → Postgres. `%` is only escaped when parameters are bound (psycopg rule)."""
    if _INSERT_IGNORE.match(sql):
        sql = _INSERT_IGNORE.sub("INSERT INTO", sql, count=1) + " ON CONFLICT DO NOTHING"
    if has_args:
        sql = sql.replace("%", "%%").replace("?", "%s")
    return sql


def _pg_args(args) -> tuple:
    # Postgres won't compare an INTEGER column with a boolean; SQLite stores booleans as 0/1
    return tuple(int(a) if isinstance(a, bool) else a for a in args)


class Row(dict):
    """A dict row that also allows row[0], like sqlite3.Row."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _row_factory(cursor):
    names = [c.name for c in cursor.description] if cursor.description else []
    return lambda values: Row(zip(names, values))


class _PgConn:
    """Gives a psycopg connection the sqlite3 `execute(sql, args)` shape the services use."""

    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql: str, args=()):
        return self.conn.execute(_pg_sql(sql, bool(args)), _pg_args(args) if args else None)


class MigrationRequired(RuntimeError):
    """The database is behind the code and this process may not migrate it (DB_AUTO_MIGRATE=0)."""


class Store:
    def __init__(self, path=DB_PATH, url: str | None = None, migrate: bool | None = None):
        self.url = DATABASE_URL if url is None else url
        self.pg = self.url.startswith(("postgres://", "postgresql://"))
        self.path = str(path)
        if self.pg:
            from psycopg_pool import ConnectionPool  # only needed in production

            self.pool = ConnectionPool(self.url, min_size=1, max_size=DB_POOL_SIZE, open=True,
                                       kwargs={"autocommit": True, "row_factory": _row_factory,
                                               "options": f"-c search_path={SEARCH_PATH}"})
        self.applied: list[str] = []
        if DB_AUTO_MIGRATE if migrate is None else migrate:
            self.applied = self.migrate()
        elif self.pending_migrations():
            raise MigrationRequired("Database schema is out of date: run  python manage.py migrate")

    # ---------------------------------------------------------------- schema + migrations
    def migrate(self) -> list[str]:
        """Bring the database up to date. Safe to run any number of times. Returns what was applied."""
        if self.pg:
            with self.tx() as c:
                for area in AREAS:
                    c.execute(f"CREATE SCHEMA IF NOT EXISTS {area}")
                for table, area in AREA_OF.items():  # tables from before the split move out of `public`
                    here = c.execute("SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND "
                                     "table_name=?", (table,)).fetchone()
                    there = c.execute("SELECT 1 FROM information_schema.tables WHERE table_schema=? AND "
                                      "table_name=?", (area, table)).fetchone()
                    if here and not there:
                        c.execute(f"ALTER TABLE public.{table} SET SCHEMA {area}")
                for stmt in filter(str.strip, SCHEMA.split(";")):
                    table = re.search(r"EXISTS\s+(\w+)", stmt).group(1)
                    c.execute(stmt.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
                              .replace(f"EXISTS {table} (", f"EXISTS {AREA_OF[table]}.{table} (", 1))
        else:
            with self._conn() as c:
                c.execute("PRAGMA journal_mode=WAL")
                c.executescript(SCHEMA)
        with self.tx() as c:
            self._ensure_columns(c)
        applied = []
        for version, name, fn in _MIGRATION_STEPS:
            with self.tx() as c:
                if c.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone():
                    continue
                fn(self, c)
                c.execute("INSERT INTO schema_migrations (version,name,applied_at) VALUES (?,?,?)", (version, name, now()))
                applied.append(f"{version} {name}")
        return applied

    def pending_migrations(self) -> list[int]:
        try:
            done = {r["version"] for r in self.query("SELECT version FROM schema_migrations")}
        except Exception:  # noqa: BLE001 - no table yet: everything is pending
            return [v for v, _, _ in _MIGRATION_STEPS]
        return [v for v, _, _ in _MIGRATION_STEPS if v not in done]

    @contextmanager
    def audit_rewrite(self, c):
        """The only door for changing audit rows: renaming legacy IDs and the retention redaction job. The
        caller logs why. Postgres: a transaction-local setting the trigger checks. SQLite: triggers are
        dropped and re-created inside the same transaction."""
        if self.pg:
            c.execute("SET LOCAL servicedesk.audit_rewrite = 'on'")
            yield
            return
        for t in AUDIT_TABLES:
            c.execute(f"DROP TRIGGER IF EXISTS {t}_no_update")
            c.execute(f"DROP TRIGGER IF EXISTS {t}_no_delete")
        try:
            yield
        finally:
            _sqlite_audit_triggers(c)

    @contextmanager
    def _conn(self):
        if self.pg:
            with self.pool.connection() as conn:
                yield _PgConn(conn)
            return
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)  # autocommit; tx() opens BEGIN
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            yield conn
        finally:
            conn.close()

    def columns(self, table: str, c=None) -> set[str]:
        if self.pg:
            sql, args = ("SELECT column_name AS name FROM information_schema.columns WHERE table_name=? "
                         "AND table_schema=?", (table, AREA_OF.get(table, "public")))
        else:
            sql, args = f"PRAGMA table_info({table})", ()
        rows = c.execute(sql, args).fetchall() if c is not None else self.query(sql, args)
        return {r["name"] for r in rows}

    def _ensure_columns(self, c) -> None:
        for table, cols in MIGRATIONS.items():
            have = self.columns(table, c)
            for col, decl in cols.items():
                if col not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        for stmt in INDEXES:
            c.execute(stmt)

    def beat(self, name: str, info: str = "") -> None:
        self.execute("INSERT INTO heartbeats (name,beat_at,info) VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET "
                     "beat_at=excluded.beat_at, info=excluded.info", (name, now(), info))

    def ping(self) -> bool:
        """Readiness check: can we reach the database?"""
        return self.one("SELECT 1 AS ok")["ok"] == 1

    @contextmanager
    def tx(self):
        """One atomic write transaction. Writers are serialised on both databases."""
        if self.pg:
            with self.pool.connection() as conn, conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (_WRITE_LOCK,))
                yield _PgConn(conn)
            return
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def query(self, sql: str, args=()) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> dict | None:
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def insert(self, sql: str, args=()) -> int:
        """INSERT into a table with an integer `id`; returns the new id on both databases."""
        with self._conn() as c:
            if self.pg:
                return c.execute(sql + " RETURNING id", args).fetchone()[0]
            return c.execute(sql, args).lastrowid

    def execute(self, sql: str, args=()) -> int:
        with self._conn() as c:
            return c.execute(sql, args).rowcount

    # ---- sessions
    def get_session(self, session_id: str) -> dict | None:
        r = self.one("SELECT * FROM sessions WHERE session_id=?", (session_id,))
        return json.loads(r["state_json"]) if r else None

    def session_owner(self, session_id: str) -> str | None:
        r = self.one("SELECT employee_id FROM sessions WHERE session_id=?", (session_id,))
        return r["employee_id"] if r else None

    def save_session(self, session_id: str, employee_id: str, state: dict) -> None:
        self.execute("INSERT INTO sessions (session_id,employee_id,state_json,updated_at) VALUES (?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
                     "state_json=excluded.state_json, updated_at=excluded.updated_at",
                     (session_id, employee_id, json.dumps(state), now()))

    def sessions_for(self, employee_id: str) -> list[dict]:
        return self.query("SELECT session_id, updated_at, state_json, (SELECT COUNT(*) FROM messages m WHERE "
                          "m.session_id=sessions.session_id) AS n_messages FROM sessions WHERE employee_id=? "
                          "ORDER BY updated_at DESC", (employee_id,))

    # ---- messages
    def add_message(self, session_id, role, text, ticket_id=None, meta=None, visibility="customer",
                    author=None) -> int:
        sql = ("INSERT INTO messages (session_id,ticket_id,role,text,meta_json,created_at,visibility,author) "
               "VALUES (?,?,?,?,?,?,?,?)")
        args = (session_id, ticket_id, role, text, json.dumps(meta or {}), now(), visibility, author)
        with self._conn() as c:
            if self.pg:
                return c.execute(sql + " RETURNING id", args).fetchone()[0]
            return c.execute(sql, args).lastrowid

    def messages(self, session_id=None, ticket_id=None, customer_only=False) -> list[dict]:
        sql, arg = ("session_id=?", session_id) if session_id else ("ticket_id=?", ticket_id)
        if customer_only:
            sql += " AND visibility='customer'"
        rows = self.query(f"SELECT * FROM messages WHERE {sql} ORDER BY id", (arg,))
        return [{**r, "meta": json.loads(r["meta_json"] or "{}")} for r in rows]

    # ---- tickets
    def create_ticket(self, **fields) -> str:
        with self.tx() as c:
            n = c.execute("SELECT MAX(CAST(SUBSTR(ticket_id,5) AS INTEGER)) FROM tickets").fetchone()[0]
            tid = f"TKT-{(n or 3000) + 1}"
            fields.update(ticket_id=tid, created_at=now(), updated_at=now(), version=1)
            cols = ",".join(fields)
            c.execute(f"INSERT INTO tickets ({cols}) VALUES ({','.join('?' * len(fields))})",
                      tuple(fields.values()))
        return tid

    def update_ticket(self, ticket_id: str, expected_version: int | None = None, conn=None, **fields) -> None:
        """Every write bumps `version`. With expected_version it is an optimistic-lock save."""
        fields["updated_at"] = now()
        sets = ",".join(f"{k}=?" for k in fields) + ",version=version+1"
        sql, args = f"UPDATE tickets SET {sets} WHERE ticket_id=?", [*fields.values(), ticket_id]
        if expected_version is not None:
            sql += " AND version=?"
            args.append(expected_version)
        if conn is not None:
            n = conn.execute(sql, args).rowcount
        else:
            n = self.execute(sql, args)
        if n == 0 and expected_version is not None:
            raise VersionConflict(self.get_ticket(ticket_id) or {})

    def get_ticket(self, ticket_id: str) -> dict | None:
        return self.one("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,))

    def tickets(self, session_id=None, employee_id=None) -> list[dict]:
        order = ("ORDER BY CASE priority WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 WHEN 'P3' THEN 3 ELSE 4 END, "
                 "created_at DESC")
        if session_id:
            return self.query(f"SELECT * FROM tickets WHERE session_id=? {order}", (session_id,))
        if employee_id:
            return self.query(f"SELECT * FROM tickets WHERE employee_id=? {order}", (employee_id,))
        return self.query(f"SELECT * FROM tickets {order}")

    # ---- trace, audit + corrections (append-only)
    def log_event(self, event_type, payload, ticket_id=None, session_id=None, actor=None, conn=None) -> None:
        args = (ticket_id, session_id, event_type, json.dumps(payload, default=str), now(), actor)
        sql = ("INSERT INTO events (ticket_id,session_id,event_type,payload_json,created_at,actor) "
               "VALUES (?,?,?,?,?,?)")
        if conn is not None:
            conn.execute(sql, args)
        else:
            self.execute(sql, args)

    def events(self, ticket_id: str) -> list[dict]:
        rows = self.query("SELECT * FROM events WHERE ticket_id=? ORDER BY id", (ticket_id,))
        return [{**r, "payload": json.loads(r["payload_json"])} for r in rows]

    def add_correction(self, ticket_id, field, old, new, by, reason=None) -> None:
        self.execute("INSERT INTO corrections (ticket_id,field,old_value,new_value,corrected_by,created_at,"
                     "reason) VALUES (?,?,?,?,?,?,?)", (ticket_id, field, old, new, by, now(), reason))

    def corrections(self) -> list[dict]:
        return self.query("SELECT * FROM corrections ORDER BY id DESC")


# ---------------------------------------------------------------- numbered one-off migrations
def _sqlite_audit_triggers(c) -> None:
    for t in AUDIT_TABLES:
        for op in ("update", "delete"):
            c.execute(f"CREATE TRIGGER IF NOT EXISTS {t}_no_{op} BEFORE {op.upper()} ON {t} "
                      f"BEGIN SELECT RAISE(ABORT, 'audit tables are append-only'); END")


def _m_audit_append_only(store: Store, c) -> None:
    if not store.pg:
        _sqlite_audit_triggers(c)
        return
    c.execute("""CREATE OR REPLACE FUNCTION audit.forbid_change() RETURNS trigger AS $fn$
        BEGIN
          IF current_setting('servicedesk.audit_rewrite', true) = 'on' THEN
            RETURN COALESCE(NEW, OLD);
          END IF;
          RAISE EXCEPTION 'audit tables are append-only (% on %)', TG_OP, TG_TABLE_NAME;
        END $fn$ LANGUAGE plpgsql""")
    for t in AUDIT_TABLES:
        c.execute(f"DROP TRIGGER IF EXISTS {t}_append_only ON audit.{t}")
        c.execute(f"CREATE TRIGGER {t}_append_only BEFORE UPDATE OR DELETE ON audit.{t} "
                  "FOR EACH ROW EXECUTE FUNCTION audit.forbid_change()")


def _m_reporting_views(store: Store, c) -> None:
    """Read-only views for BI tools (Power BI, Excel). No free text, names or message content."""
    if not store.pg:
        return
    c.execute("""CREATE OR REPLACE VIEW reporting.ticket_facts AS
        SELECT ticket_id, created_at, resolved_at, first_response_at, claimed_at, status, priority, category_id,
               subcategory, queue, channel, owner AS agent_id, reopened_count, resolution_code,
               (resolution_code = 'Solved by bot') AS bot_resolved, incident_parent IS NOT NULL AS linked_to_incident
        FROM service.tickets""")
    c.execute("""CREATE OR REPLACE VIEW reporting.answer_feedback_facts AS
        SELECT kb_id, kb_version, helpful, reason, created_at FROM knowledge.answer_feedback""")
    c.execute("""CREATE OR REPLACE VIEW reporting.ticket_rating_facts AS
        SELECT r.ticket_id, r.score, r.created_at, t.category_id, t.queue, t.priority
        FROM service.ticket_ratings r JOIN service.tickets t USING (ticket_id)""")
    c.execute("""CREATE OR REPLACE VIEW reporting.alert_facts AS
        SELECT alert_id, ticket_id, event, level, priority, target_queue, created_at, acked_at, escalated_at
        FROM ops.alerts WHERE ticket_id <> 'SYSTEM'""")


def _m_checklist_positions(store: Store, c) -> None:
    if not store.pg:  # checklists from before `position` existed keep their insertion order
        c.execute("UPDATE checklists SET position=rowid WHERE position IS NULL")
    # Phase 1 kept the human owner in assigned_to
    c.execute("UPDATE tickets SET owner=assigned_to WHERE owner IS NULL AND assigned_to IS NOT NULL "
              "AND status LIKE 'HUMAN%'")


def _m_audit_without_text(store: Store, c) -> None:
    """Earlier releases copied comment text and the handoff (the employee's own words) into the audit log.
    The words belong in the ticket, where retention can remove them; the audit log keeps only the fact."""
    with store.audit_rewrite(c):
        rows = c.execute("SELECT id, event_type, payload_json FROM events WHERE event_type IN "
                         "('Comment','Work note','Escalation')").fetchall()
        for r in rows:
            payload = json.loads(r["payload_json"] or "{}")
            if r["event_type"] == "Escalation":
                if "handoff" not in payload:
                    continue
                payload["handoff_fields"] = sorted(k for k, v in (payload.pop("handoff") or {}).items() if v)
            else:
                payload["detail"] = f"{r['event_type'].lower()} added (text kept in the ticket thread)"
            c.execute("UPDATE events SET payload_json=? WHERE id=?", (json.dumps(payload), r["id"]))
        c.execute("INSERT INTO events (event_type,payload_json,created_at,actor) VALUES ('Audit rewrite',?,?,"
                  "'system')", (json.dumps({"detail": f"removed message text from {len(rows)} audit rows"}), now()))


_MIGRATION_STEPS = [
    (1, "Phase 1 owner and checklist order backfill", _m_checklist_positions),
    (2, "Audit tables are append-only", _m_audit_append_only),
    (3, "Reporting views for BI tools", _m_reporting_views),
    (4, "Audit log keeps facts, not message text", _m_audit_without_text),
]
