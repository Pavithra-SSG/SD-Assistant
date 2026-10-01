"""Least-privilege database accounts (Postgres). Run by the database owner through `manage.py migrate`.

  sd_app        what the api and worker connect as: read/write in identity, service, ops and knowledge;
                audit is INSERT + SELECT only (no UPDATE/DELETE even if a trigger were dropped); can't create or
                alter tables, so a bug or an injection can't change the schema.
  sd_reporting  what BI tools (Power BI, Excel) connect as: SELECT on the `reporting` views only. No names,
                message text or passwords are reachable.

The owner account (POSTGRES_USER) is used only by the one-shot `migrate` step and by backups.
"""
from __future__ import annotations

from psycopg import sql

WRITE_AREAS = ("identity", "service", "ops", "knowledge")


def _ensure_login_role(conn, name: str, password: str) -> None:
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (name,)).fetchone()
    stmt = "ALTER ROLE {} WITH LOGIN PASSWORD {}" if exists else "CREATE ROLE {} WITH LOGIN PASSWORD {}"
    conn.execute(sql.SQL(stmt).format(sql.Identifier(name), sql.Literal(password)))


def ensure_roles(store, app_password: str | None, reporting_password: str | None) -> list[str]:
    """Create/refresh the roles and their grants. Idempotent. Returns a description of what was set."""
    if not store.pg:
        return ["SQLite: database accounts don't apply"]
    done = []
    with store.pool.connection() as conn:
        db = conn.execute("SELECT current_database()").fetchone()
        dbname = list(db.values())[0] if isinstance(db, dict) else db[0]
        owner = sql.Identifier(conn.info.user)
        if app_password:
            _ensure_login_role(conn, "sd_app", app_password)
            app = sql.Identifier("sd_app")
            conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(dbname), app))
            for area in WRITE_AREAS + ("audit",):
                conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(area), app))
                conn.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}")
                             .format(sql.Identifier(area), app))
                conn.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} GRANT USAGE, SELECT ON "
                                     "SEQUENCES TO {}").format(owner, sql.Identifier(area), app))
            for area in WRITE_AREAS:
                conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}")
                             .format(sql.Identifier(area), app))
                conn.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} GRANT SELECT, INSERT, "
                                     "UPDATE, DELETE ON TABLES TO {}").format(owner, sql.Identifier(area), app))
            conn.execute(sql.SQL("GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA audit TO {}").format(app))
            conn.execute(sql.SQL("REVOKE UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA audit FROM {}").format(app))
            conn.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA audit GRANT SELECT, INSERT ON "
                                 "TABLES TO {}").format(owner, app))
            # only the owner records migrations
            conn.execute(sql.SQL("REVOKE INSERT, UPDATE, DELETE ON ops.schema_migrations FROM {}").format(app))
            done.append("sd_app: read/write identity, service, ops, knowledge; audit insert-only; no DDL")
        if reporting_password:
            _ensure_login_role(conn, "sd_reporting", reporting_password)
            rep = sql.Identifier("sd_reporting")
            conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(dbname), rep))
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA reporting TO {}").format(rep))
            conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA reporting TO {}").format(rep))
            conn.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA reporting GRANT SELECT ON TABLES "
                                 "TO {}").format(owner, rep))
            done.append("sd_reporting: SELECT on reporting views only")
    return done or ["no role passwords set (DB_APP_PASSWORD / DB_REPORTING_PASSWORD): accounts unchanged"]
