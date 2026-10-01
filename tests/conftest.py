"""Test setup: a throwaway database, MOCK brain, seeded users. Set before the API module is imported.

SQLite by default. To run the same tests on Postgres, point TEST_DATABASE_URL at an EMPTY, disposable
database; every table in its public schema is dropped first:
    TEST_DATABASE_URL=postgresql://postgres:testpw@localhost:55432/sdtest python -m pytest tests -q
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["APP_ENV"] = "development"
os.environ["SERVICEDESK_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "")
if os.environ["DATABASE_URL"]:
    import psycopg

    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as _c:
        for _schema in ("identity", "service", "ops", "audit", "knowledge", "reporting"):  # the store's areas
            _c.execute(f"DROP SCHEMA IF EXISTS {_schema} CASCADE")
        _c.execute("DROP SCHEMA public CASCADE")
        _c.execute("CREATE SCHEMA public")
os.environ["JEV_MOCK"] = "1"
os.environ["DEMO_PASSWORD"] = "scenario-pass-123"
os.environ["MOCK_DEGRADED"] = ""
os.environ["LOGIN_RATE_PER_MINUTE"] = "100000"  # many accounts sign in from one test client
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402

from .scenarios import Harness  # noqa: E402


@pytest.fixture(scope="session")
def client():
    return TestClient(api.app)


@pytest.fixture(scope="session")
def h(client):
    return Harness(client, api)
