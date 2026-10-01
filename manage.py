"""Admin command line. Run inside the API container:  docker compose exec api python manage.py <command>

  migrate                             apply database changes + refresh database accounts (run as the owner)
  check                               validate production settings and database access
  create-user ID "Name" email ROLE [--queues "Q1;Q2"] [--title T] [--department D] [--location L]
                                      create an account; prints a one-time temporary password
  import-users FILE.csv | -          create/update accounts from an HR export (- = stdin; see docs/DEPLOY.md)
  reset-password ID                   new temporary password (verify the person first); signs them out
  unlock ID | disable ID | enable ID
  set-role ID ROLE [--queues "Q1;Q2"] change role / queues (signs them out)
  list-users [--role ROLE]

Temporary passwords are shown once. Hand them over through a verified channel; the person must change
theirs at first sign-in. Every command is recorded in the audit trail as actor "cli".
"""
from __future__ import annotations

import argparse
import csv
import os
import sys

from servicedesk import config
from servicedesk.dbroles import ensure_roles
from servicedesk.knowledge import get_knowledge
from servicedesk.services.auth import ROLES, AuthService
from servicedesk.store import Store

BY = "cli"


def _queues(text: str | None) -> list[str] | None:
    return [q.strip() for q in text.split(";") if q.strip()] if text else None


def cmd_check(_args, auth: AuthService, store: Store) -> int:
    problems = []
    if config.PRODUCTION:
        if config.JEV_MOCK:
            problems.append("TYPESAFE_API_KEY is missing: the bot would run on the offline mock brain")
        if not store.pg:
            problems.append("DATABASE_URL is not a Postgres URL: production should not run on SQLite")
        if config.DOCS_ENABLED:
            problems.append("DOCS_ENABLED=1: the API documentation is public (fine only on a private network)")
        if config.SELF_SERVICE_RESET:
            problems.append("SELF_SERVICE_RESET=1 but the identity tools are simulated")
    ok = store.ping()
    supervisors = [u for u in auth.users("supervisor") if u["status"] == "active"]
    print(f"environment   {config.APP_ENV} (version {config.APP_VERSION})")
    print(f"database      {'postgres' if store.pg else 'sqlite'}, reachable: {ok}")
    print(f"AI model      {'MOCK (offline)' if config.JEV_MOCK else config.TYPESAFE_MODEL}")
    print(f"accounts      {len(auth.users())} total, {len(supervisors)} active supervisor(s)")
    if not supervisors:
        problems.append("No active supervisor: create one with  python manage.py create-user ... supervisor")
    for p in problems:
        print(f"  ! {p}")
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 0 if not problems else 1


def cmd_migrate(_args, _auth, store: Store) -> int:
    applied = store.applied
    print("schema up to date" + (": applied " + "; ".join(applied) if applied else " (nothing new)"))
    for line in ensure_roles(store, os.getenv("DB_APP_PASSWORD"), os.getenv("DB_REPORTING_PASSWORD")):
        print(f"  {line}")
    return 0


def cmd_create(a, auth: AuthService, _store) -> int:
    temp = auth.create_user(a.employee_id, a.name, a.email, a.role, _queues(a.queues), a.title, a.department,
                            a.location, by=BY)
    print(f"Created {a.employee_id.upper()} ({a.role}). Temporary password (shown once):\n  {temp}")
    return 0


def cmd_import(a, auth: AuthService, _store) -> int:
    """`import-users -` reads the CSV from stdin and writes the new accounts' temporary passwords as CSV to
    stdout (the containers' file system is read-only). The summary goes to stderr."""
    if a.file == "-":
        rows = list(csv.DictReader(sys.stdin.read().lstrip("﻿").splitlines()))
    else:
        with open(a.file, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    res = auth.import_users(rows, by=BY)
    print(f"created {len(res['created'])}, updated {res['updated']}, errors {len(res['errors'])}", file=sys.stderr)
    for e in res["errors"]:
        print(f"  ! {e}", file=sys.stderr)
    if res["created"]:
        w = csv.DictWriter(sys.stdout, fieldnames=["employee_id", "email", "temporary_password"])
        w.writeheader()
        w.writerows(res["created"])
        print("Temporary passwords were written to standard output. Distribute securely, then delete them.",
              file=sys.stderr)
    return 1 if res["errors"] else 0


def cmd_reset(a, auth: AuthService, _store) -> int:
    print(f"Temporary password for {a.employee_id.upper()} (shown once):\n  {auth.reset_password(a.employee_id, by=BY)}")
    return 0


def cmd_simple(a, auth: AuthService, _store) -> int:
    {"unlock": lambda: auth.unlock(a.employee_id, by=BY),
     "disable": lambda: auth.set_status(a.employee_id, False, by=BY),
     "enable": lambda: auth.set_status(a.employee_id, True, by=BY)}[a.command]()
    print(f"{a.command}: {a.employee_id.upper()} done")
    return 0


def cmd_set_role(a, auth: AuthService, _store) -> int:
    u = auth.update_user(a.employee_id, by=BY, role=a.role, queues=_queues(a.queues))
    print(f"{u['user_id']}: {u['role']}, queues {', '.join(u['queues']) or '-'}")
    return 0


def cmd_list(a, auth: AuthService, _store) -> int:
    for u in auth.users(a.role):
        flags = " ".join(f for f, on in (("DISABLED", u["status"] != "active"), ("LOCKED", u["locked"]),
                                          ("MUST-CHANGE-PW", u["must_change_password"])) if on)
        print(f"{u['user_id']:<10} {u['role']:<11} {u['name']:<28} {u['email'] or '':<36} {flags}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("migrate")
    c = sub.add_parser("create-user")
    c.add_argument("employee_id")
    c.add_argument("name")
    c.add_argument("email")
    c.add_argument("role", choices=ROLES)
    for opt in ("--queues", "--title", "--department", "--location"):
        c.add_argument(opt)
    sub.add_parser("import-users").add_argument("file")
    for name in ("reset-password", "unlock", "disable", "enable"):
        sub.add_parser(name).add_argument("employee_id")
    r = sub.add_parser("set-role")
    r.add_argument("employee_id")
    r.add_argument("role", choices=ROLES)
    r.add_argument("--queues")
    sub.add_parser("list-users").add_argument("--role", choices=ROLES)
    a = ap.parse_args(argv)
    store = Store(migrate=True if a.command == "migrate" else None)
    auth = AuthService(store, get_knowledge())
    handlers = {"check": cmd_check, "migrate": cmd_migrate, "create-user": cmd_create, "import-users": cmd_import,
                "reset-password": cmd_reset, "unlock": cmd_simple, "disable": cmd_simple, "enable": cmd_simple,
                "set-role": cmd_set_role, "list-users": cmd_list}
    try:
        return handlers[a.command](a, auth, store)
    except (ValueError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
