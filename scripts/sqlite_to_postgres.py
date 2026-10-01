"""Copy an existing SQLite database (development / pilot) into Postgres. Run once, before go-live.

    python scripts/sqlite_to_postgres.py servicedesk.db postgresql://user:pass@host:5432/servicedesk

The SQLite file is only read. The target gets the current schema first (with the SQLite file brought up to
date as well), then every row is copied. Rows already in the target are skipped, so a re-run is safe.
Ends with row counts from both sides so you can compare them.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from servicedesk.store import Store  # noqa: E402

TABLES = ["users", "sessions", "tickets", "messages", "events", "corrections", "auth_sessions", "alerts",
          "checklists", "watchers", "notifications", "bot_answers", "shift_checklists"]
SERIAL = {"messages": "id", "events": "id", "corrections": "id", "alerts": "alert_id", "notifications": "id",
          "bot_answers": "id"}


def main(sqlite_path: str, pg_url: str) -> int:
    if not Path(sqlite_path).exists():
        print(f"no such file: {sqlite_path}")
        return 2
    Store(path=sqlite_path, url="")  # applies any pending migrations to the source copy's schema
    target = Store(url=pg_url)
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    print(f"{'table':<18}{'sqlite':>8}{'postgres':>10}")
    for table in TABLES:
        rows = src.execute(f"SELECT * FROM {table}").fetchall()
        cols = [c for c in (rows[0].keys() if rows else []) if c in target.columns(table)]
        if rows:
            sql = (f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})")
            with target.tx() as c:
                for r in rows:
                    c.execute(sql, tuple(r[k] for k in cols))
        if table in SERIAL:  # continue numbering after the copied ids
            col = SERIAL[table]
            target.query(f"SELECT setval(pg_get_serial_sequence('{table}','{col}'), "
                         f"GREATEST((SELECT COALESCE(MAX({col}),0) FROM {table}), 1))")
        n = target.one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
        print(f"{table:<18}{len(rows):>8}{n:>10}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
