"""Runtime configuration. Secrets come from the environment / .env, never from code."""
import os
import secrets
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # python-dotenv is optional
    pass

ROOT = Path(__file__).resolve().parent.parent

# "production" turns on the strict rules: required secrets, no demo accounts, no simulated self-service reset
APP_ENV = os.getenv("APP_ENV", "development").lower()
PRODUCTION = APP_ENV == "production"
APP_VERSION = os.getenv("APP_VERSION", "2.1.0")
DOCS_ENABLED = os.getenv("DOCS_ENABLED", "0" if PRODUCTION else "1") == "1"
LOG_FORMAT = os.getenv("LOG_FORMAT", "json" if PRODUCTION else "text")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
# Sign-in throttle per client IP, on top of the per-account lockout (stops password spraying across accounts)
LOGIN_RATE_PER_MINUTE = int(os.getenv("LOGIN_RATE_PER_MINUTE", "20"))
# Behind the reverse proxy, trust its X-Forwarded-For header for the client address
TRUST_PROXY = os.getenv("TRUST_PROXY", "1" if PRODUCTION else "0") == "1"
# The background worker runs alert timers and ticket sweeps; the API only runs them itself if no worker is
# configured (development convenience).
WORKER_INTERVAL_SECONDS = int(os.getenv("WORKER_INTERVAL_SECONDS", "15"))
API_RUNS_TIMERS = os.getenv("API_RUNS_TIMERS", "0" if PRODUCTION else "1") == "1"


class ConfigError(RuntimeError):
    """A required production setting is missing or unsafe."""
DATA_DIR = ROOT / "datasets"
DB_PATH = Path(os.getenv("SERVICEDESK_DB", ROOT / "servicedesk.db"))
# Production: postgresql://user:pass@host:5432/servicedesk. Empty = the SQLite file above (development, tests).
DATABASE_URL = os.getenv("DATABASE_URL", "")
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "10"))
# Production: a one-shot `migrate` step (database owner) changes the schema; the app connects as a role that
# can't, and refuses to start if the schema is behind (DB_AUTO_MIGRATE=0).
DB_AUTO_MIGRATE = os.getenv("DB_AUTO_MIGRATE", "1") == "1"

TYPESAFE_API_KEY = os.getenv("TYPESAFE_API_KEY", "")
# Pin a version (e.g. "jev-1.13.0") once thresholds are tuned on your data;
# "jev-latest" moves when TypeSafe ships a new release.
TYPESAFE_MODEL = os.getenv("TYPESAFE_MODEL", "jev-latest")

# MOCK mode = keyword heuristics instead of Jev. Used automatically when no key is set.
JEV_MOCK = os.getenv("JEV_MOCK", "0") == "1" or not TYPESAFE_API_KEY

# Decision thresholds. These are starting points: re-tune them on your own tickets.
CATEGORY_MIN_CONFIDENCE = float(os.getenv("CATEGORY_MIN_CONFIDENCE", "0.50"))
RISK_FLAG_THRESHOLD = float(os.getenv("RISK_FLAG_THRESHOLD", "0.50"))
KB_MIN_RELEVANCE = float(os.getenv("KB_MIN_RELEVANCE", "0.50"))
FIELD_PROVIDED_THRESHOLD = float(os.getenv("FIELD_PROVIDED_THRESHOLD", "0.50"))
MAX_CLARIFYING_QUESTIONS = int(os.getenv("MAX_CLARIFYING_QUESTIONS", "2"))
MAX_ATTEMPTS = 2  # from agent_playbooks.json -> Max_Autonomous_Attempts

# ---- Phase 2A: auth
def _jwt_secret() -> str:
    """JWT signing key: JWT_SECRET from the environment, else (development only) a random key kept in a
    git-ignored file. Production refuses to start without a strong JWT_SECRET, because a file-based key would
    differ between containers and sign everyone out on every redeploy."""
    if os.getenv("JWT_SECRET"):
        if PRODUCTION and len(os.environ["JWT_SECRET"]) < 32:
            raise ConfigError("JWT_SECRET must be at least 32 characters in production")
        return os.environ["JWT_SECRET"]
    if PRODUCTION:
        raise ConfigError("JWT_SECRET is required in production (python -c \"import secrets; "
                          "print(secrets.token_urlsafe(48))\")")
    path = ROOT / ".jwt_secret"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()


JWT_SECRET = _jwt_secret()
SESSION_ABSOLUTE_HOURS = 8
SESSION_IDLE_MINUTES = 30
LOCKOUT_FAILURES = 5
LOCKOUT_MINUTES = 15
PASSWORD_MIN_LENGTH = int(os.getenv("PASSWORD_MIN_LENGTH", "12"))
PASSWORD_BLOCKLIST_FILE = os.getenv("PASSWORD_BLOCKLIST_FILE", "")
# The "Can't sign in?" self-service reset calls a SIMULATED authenticator (tools.py). It stays off in
# production until a real identity provider is connected; recovery then always goes to Identity Support.
SELF_SERVICE_RESET = os.getenv("SELF_SERVICE_RESET", "0" if PRODUCTION else "1") == "1"
# Demo accounts (50 employees + 15 staff, one shared password) are for development only.
DEMO_PASSWORD = "" if PRODUCTION else os.getenv("DEMO_PASSWORD", "")
ONLINE_WINDOW_MINUTES = 5  # an agent counts as "online" if their session was used this recently

# ---- Phase 2C: alerts and ticket rules
P1_REALERT_MINUTES = 2
P1_SUPERVISOR_MINUTES = 10
SLA_RISK_FRACTION = 0.75
REOPEN_WINDOW_DAYS = 7
DUPLICATE_WINDOW_HOURS = 24
INCIDENT_LINK_HOURS = 24
ALERT_BACKLOG_WARNING = 5
JEV_OUTAGE_BANNER_MINUTES = 15

# ---- Phase 4: business-hours SLA clock (P1 runs 24x7)
TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata")
BUSINESS_HOURS = os.getenv("BUSINESS_HOURS", "09:00-18:00")
BUSINESS_DAYS = os.getenv("BUSINESS_DAYS", "Mon-Fri")
HOLIDAYS_FILE = os.getenv("HOLIDAYS_FILE", "")  # one YYYY-MM-DD per line, # comments allowed
SLA_24X7_PRIORITIES = tuple(p.strip() for p in os.getenv("SLA_24X7_PRIORITIES", "P1").split(",") if p.strip())
# A screenshot mid-ticket is treated as "a different problem" (and the employee is asked) only when Jev is at
# least this sure it belongs to another category and gives the ticket's own category under 20%.
SCREENSHOT_MISMATCH_CONFIDENCE = float(os.getenv("SCREENSHOT_MISMATCH_CONFIDENCE", "0.6"))
# routing_matrix After_Hours_Rule: "24x7 for P1/security": security incidents never wait for Monday either.
# Sign-in and MFA lockouts (CAT-01, CAT-05) are added too: someone locked out can't work at all, so
# "first reply on Monday" for a Saturday password problem isn't acceptable (decided 3 Oct 2026).
SLA_24X7_CATEGORIES = tuple(c.strip() for c in os.getenv("SLA_24X7_CATEGORIES", "CAT-01,CAT-05,CAT-09").split(",")
                            if c.strip())
# Statuses where the SLA clock stops because we're waiting on the employee (dataset Pause_Rule)
SLA_PAUSE_STATUSES = ("WAITING_FOR_USER",)

# ---- Go-live: notifications outside the app (sent by the worker from the outbox)
SITE_URL = os.getenv("SITE_URL", "http://localhost:8501")
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "IT Service Desk <itsupport@company.example>")
SMTP_STARTTLS = os.getenv("SMTP_STARTTLS", "1") == "1"
EMAIL_EMPLOYEES = os.getenv("EMAIL_EMPLOYEES", "1") == "1"
EMAIL_STAFF = os.getenv("EMAIL_STAFF", "0") == "1"  # staff work in the app and get P1/P2 in Teams
TEAMS_WEBHOOK_URL = os.getenv("TEAMS_WEBHOOK_URL", "")
# Watchdog + dead-man's switch
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "") or TEAMS_WEBHOOK_URL
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO", "")
HEALTHCHECK_PING_URL = os.getenv("HEALTHCHECK_PING_URL", "")
# ---- Go-live: privacy and retention (India DPDP Act: keep personal data no longer than needed)
RETENTION_MONTHS = int(os.getenv("RETENTION_MONTHS", "24"))

# ---- Phase 4: screenshots (OCR runs on this server; nothing leaves the company)
ATTACHMENT_DIR = Path(os.getenv("ATTACHMENT_DIR", ROOT / "attachments"))
ATTACHMENT_MAX_MB = int(os.getenv("ATTACHMENT_MAX_MB", "10"))
ATTACHMENT_RETENTION_DAYS = int(os.getenv("ATTACHMENT_RETENTION_DAYS", "90"))
OCR_ENABLED = os.getenv("OCR_ENABLED", "1") == "1"
OCR_MIN_CONFIDENCE = float(os.getenv("OCR_MIN_CONFIDENCE", "0.75"))

# Streamlit talks to FastAPI over HTTP (D8: FastAPI is the only writer).
API_URL = os.getenv("SERVICEDESK_API", "http://127.0.0.1:8000")
