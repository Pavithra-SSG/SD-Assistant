"""FastAPI backend: the ONLY process that writes to the database (D8). Streamlit is a client.

Run:  uvicorn api:app --reload      then open http://127.0.0.1:8000/docs
Identity comes from the bearer token only, never from a request body (spec §15).
Every route checks the role. There are no delete endpoints (D9).
The TypeSafe key stays on this server (never sent to a browser).
"""
from __future__ import annotations

import csv
import io
import json
import time
import uuid
from collections import defaultdict, deque
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi import Form, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from servicedesk import config
from servicedesk.directory import employee_exists, employee_profile
from servicedesk.logs import setup as setup_logging
from servicedesk.knowledge import FORM_CONTACT_OPTIONS, FORM_IMPACT_OPTIONS, FORM_WORKAROUND_OPTIONS
from servicedesk.orchestrator import ConversationService, _reply_extras, is_thanks, redact_inline
from servicedesk.schemas import (ERRORS, AlertOut, BotAnswersOut, ChatOut, ChecklistItem, ChoiceOut, CorrectionOut,
                                 DashboardOut, EmployeeTicket, EmployeeTicketDetail, HealthOut, LoginOut, MeOut,
                                 MessageOut, MetaOut, NotificationOut, Ok, PendingAlertsOut, PerformanceRow, QueueRow,
                                 RecoverOut, ReplyOut, ReviewOut, SessionOut, SessionRow, ShiftOut, StaffOut,
                                 SubmitOut, TicketDetailOut, TicketOut, ImportOut, TempPasswordOut, UserOut,
                                 AttachmentOut)
from servicedesk.services.analytics import AnalyticsService
from servicedesk.services.auth import AuthError, AuthService
from servicedesk.services.attachments import AttachmentError, problem_lines, summary_for_bot
from servicedesk.services.kb import KBError, fill_reply
from servicedesk.services.privacy import PrivacyService
from servicedesk.services.notify import NotificationService
from servicedesk.services.tickets import (AGENT_RESOLUTION_CODES, BOT_STATES, HUMAN_STATES, RESOLUTION_CODES,
                                          TicketError, employee_label)
from servicedesk.store import Store, VersionConflict, ago, now

T_SYS, T_AUTH = "0. System & reference data", "1. Sign-in (everyone)"
T_CHAT, T_FORM, T_MINE = "2. Employee: chat", "3. Employee: ticket form", "4. Employee: my tickets"
T_NOTE, T_TICK = "5. Notifications (everyone)", "6. Support: queue & ticket form"
T_ALERT, T_INS = "7. Support: alerts", "8. Support: dashboards & reports"
T_ADMIN = "9. Supervisor: user accounts"
T_KB = "10. Support: knowledge, gaps & saved replies"

TAGS = [
    {"name": T_SYS, "description": "Health check and the lists the UI needs (categories, queues, form fields)."},
    {"name": T_AUTH, "description": "**Start here.** `POST /auth/login` → copy `token` → click **Authorize** (top "
                                    "right) → paste it. Sign in with your employee ID or work email. After a temporary "
                                    "password, call `POST /auth/change-password` first: other endpoints answer 403 "
                                    "`password_change_required` until you do. Demo (development only): `EMP1001`–"
                                    "`EMP1050`, IT staff `EMP2001`–`EMP2015`, password `DEMO_PASSWORD` in `.env`."},
    {"name": T_CHAT, "description": "**Employee token.** Create a session, then send messages. The bot triages, "
                                    "answers from the KB (max 2 attempts) or escalates to a human."},
    {"name": T_FORM, "description": "**Employee token.** Three steps: review → submit → choose Fix now / Just log."},
    {"name": T_MINE, "description": "**Employee token.** Own tickets only: status, thread, reply, reopen, confirm, "
                                    "cancel. Work notes and Jev scores are never returned here."},
    {"name": T_NOTE, "description": "**Any token.** In-app notifications for the signed-in user."},
    {"name": T_TICK, "description": "**Agent or supervisor token.** The ServiceNow-style ticket: claim / take over, "
                                    "edit with version check, comments and work notes, checklist, resolve."},
    {"name": T_ALERT, "description": "**Agent or supervisor token.** P1 modal / P2 toast / badges. The UI polls "
                                     "every 10 seconds."},
    {"name": T_INS, "description": "**Agent or supervisor token** (corrections: supervisor only). Read-only numbers."},
    {"name": T_KB, "description": "**Agent or supervisor token** (approve and saved-reply edits: supervisor). "
                                  "Employee-friendly article versions: draft → meaning check → approve. Only "
                                  "approved text reaches employees."},
    {"name": T_ADMIN, "description": "**Supervisor token.** Create and import accounts, change roles and queues, "
                                     "reset passwords (after verifying the person out of band), unlock, disable "
                                     "leavers. Nothing is deleted; every action is audited."},
]

app = FastAPI(
    title="IT Service Desk Ticket Assistant (Jev)", openapi_tags=TAGS, version=config.APP_VERSION,
    # production hides the interactive docs unless DOCS_ENABLED=1 (they list every endpoint)
    docs_url="/docs" if config.DOCS_ENABLED else None, redoc_url=None,
    openapi_url="/openapi.json" if config.DOCS_ENABLED else None,
    description="Sections run in the order you'd use them. Each endpoint shows its request body and, under "
                "**Responses**, the exact shape it returns. Endpoints with a padlock need a token (see section 1).")

log = setup_logging("servicedesk.api")


# ---------------------------------------------------------------- request middleware
_login_hits: dict[str, deque] = defaultdict(deque)
_SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                     "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
                     "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'"}


def _client_ip(request: Request) -> str:
    # behind the reverse proxy the real address is the first X-Forwarded-For hop (the proxy sets it)
    fwd = request.headers.get("x-forwarded-for") if config.TRUST_PROXY else None
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Request ID, sign-in throttle per IP, security headers, one access-log line per request."""
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    start = time.perf_counter()
    if request.url.path in ("/auth/login", "/auth/recover") and request.method == "POST":
        hits, ip, t = _login_hits[_client_ip(request)], _client_ip(request), time.monotonic()
        while hits and t - hits[0] > 60:
            hits.popleft()
        if len(hits) >= config.LOGIN_RATE_PER_MINUTE:
            log.warning("sign-in throttled", extra={"ip": ip, "path": request.url.path, "request_id": rid})
            return JSONResponse({"detail": "Too many sign-in attempts from this network. Wait a minute."},
                                status_code=429, headers={"Retry-After": "60", "X-Request-ID": rid})
        hits.append(t)
    try:
        response = await call_next(request)
    except Exception:
        log.exception("unhandled error", extra={"path": request.url.path, "request_id": rid})
        response = JSONResponse({"detail": "Internal error", "request_id": rid}, status_code=500)
    docs = request.url.path in ("/docs", "/openapi.json") or request.url.path.startswith("/docs/")
    for k_, v in _SECURITY_HEADERS.items():
        if not (docs and k_ == "Content-Security-Policy"):  # Swagger UI needs its own scripts
            response.headers.setdefault(k_, v)
    response.headers["X-Request-ID"] = rid
    if request.url.path not in ("/health", "/ready"):
        log.info("request", extra={"method": request.method, "path": request.url.path,
                                   "status": response.status_code, "ms": round((time.perf_counter() - start) * 1000),
                                   "user": getattr(request.state, "user_id", None), "request_id": rid})
    return response


# ---------------------------------------------------------------- wiring (one instance of each service)
store = Store()
notify = NotificationService(store)
conv = ConversationService(store=store, notify=notify)
k = conv.k
auth = AuthService(store, k)
alerts = conv.alerts
alerts.auth = auth
tickets = conv.tickets
analytics = AnalyticsService(store, k, tickets)
kbs = conv.kbs
atts = conv.atts
privacy = PrivacyService(store)
if config.DEMO_PASSWORD:  # development only: demo accounts from the datasets, password from .env
    auth.seed(config.DEMO_PASSWORD)
# Employee-friendly article drafts: auto-approved only in development; production waits for a supervisor
kbs.seed_drafts(approve=bool(config.DEMO_PASSWORD), by="demo-seed" if config.DEMO_PASSWORD else "release")
kbs.seed_saved_replies()

bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------- errors
@app.exception_handler(TicketError)
def _ticket_error(_: Request, e: TicketError):
    body = {"detail": str(e)}
    if hasattr(e, "missing"):
        body["missing"] = e.missing
    return JSONResponse(body, status_code=e.status_code)


@app.exception_handler(VersionConflict)
def _conflict(_: Request, e: VersionConflict):
    return JSONResponse({"detail": str(e), "version": e.ticket.get("version")}, status_code=409)


@app.exception_handler(AuthError)
def _auth_error(_: Request, e: AuthError):
    return JSONResponse({"detail": str(e)}, status_code=e.status_code)


# ---------------------------------------------------------------- identity
def current_user(request: Request, cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> dict:
    if not cred:
        raise HTTPException(401, "Sign in first")
    user = auth.authenticate(cred.credentials)
    request.state.token = cred.credentials
    request.state.user_id = user["user_id"]
    if user["must_change_password"] and request.url.path not in PASSWORD_CHANGE_PATHS:
        raise HTTPException(403, "password_change_required")
    return user


PASSWORD_CHANGE_PATHS = {"/auth/me", "/auth/logout", "/auth/change-password"}


def require_role(*roles: str):
    def dep(request: Request, user: dict = Depends(current_user)) -> dict:
        if user["role"] not in roles:
            auth.audit("role_denied", user["user_id"], path=request.url.path, role=user["role"])
            raise HTTPException(403, "role_denied")
        return user
    return dep


employee_only = require_role("employee")
support_only = require_role("agent", "supervisor")
supervisor_only = require_role("supervisor")


def _ticket_for(tid: str, user: dict) -> dict:
    t = store.get_ticket(tid)
    if not t or not tickets.can_view(user, t):
        raise HTTPException(404, "Ticket not found")
    return t


# ================================================================ misc
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


@app.get("/health", tags=[T_SYS], response_model=HealthOut, summary="Is the API up, and is Jev working?")
def health():
    """Liveness: the process answers. Public, so it reveals nothing about users or data."""
    return {"brain": conv.brain.name, "model": None if config.JEV_MOCK else config.TYPESAFE_MODEL,
            "degraded": alerts.degraded(), "version": config.APP_VERSION, "environment": config.APP_ENV}


@app.get("/ready", tags=[T_SYS], summary="Can the API serve traffic? (database + worker heartbeat)",
         responses={503: {"description": "Not ready: database unreachable or worker silent"}})
def ready():
    """Readiness for the load balancer / Docker healthcheck. 503 if the database is unreachable, or (in
    production) if the background worker hasn't reported for 3 intervals, since alerts would stop."""
    checks = {"database": False, "worker": None}
    try:
        checks["database"] = store.ping()
    except Exception as e:  # noqa: BLE001 - any database error means not ready
        log.warning("readiness: database error %s", e)
    if not config.API_RUNS_TIMERS:
        beat = store.one("SELECT beat_at FROM heartbeats WHERE name='worker'") if checks["database"] else None
        checks["worker"] = bool(beat and beat["beat_at"] >= ago(seconds=3 * config.WORKER_INTERVAL_SECONDS))
    ok = checks["database"] and checks["worker"] is not False
    return JSONResponse({"ready": ok, **checks}, status_code=200 if ok else 503)


@app.get("/meta", tags=[T_SYS], response_model=MetaOut, responses=ERRORS,
         summary="Reference data for forms and filters")
def meta(user: dict = Depends(current_user)):
    """Categories, issue types, dynamic form fields, queues and scales. Any signed-in user; nothing per-user."""
    return {"categories": [{"id": c, "name": k.name(c)} for c in k.categories],
            "issue_types": {c: k.issue_types(c) for c in k.categories},
            "form_fields": {c: k.form_fields(c) for c in k.categories},
            "impact_options": FORM_IMPACT_OPTIONS, "workaround_options": FORM_WORKAROUND_OPTIONS,
            "contact_options": FORM_CONTACT_OPTIONS, "queues": k.all_queues(),
            "resolution_codes": RESOLUTION_CODES, "agent_resolution_codes": AGENT_RESOLUTION_CODES,
            "channels": ["Chat", "Form", "Email", "Phone", "Portal"],
            "impact_scale": {"extensive": "1 Extensive", "significant": "2 Significant", "moderate": "3 Moderate",
                             "limited": "4 Limited"},
            "urgency_scale": {"critical": "1 Critical", "high": "2 High", "medium": "3 Medium", "low": "4 Low"},
            "kb": {kid: a.title for kid, a in k.kb.items()}}


# ================================================================ auth (2A)
class LoginIn(BaseModel):
    login_id: str
    password: str


class RecoverIn(BaseModel):
    login_id: str
    problem: Literal["password", "mfa"]
    has_registered_factor: bool


@app.post("/auth/login", tags=[T_AUTH], response_model=LoginOut, summary="Sign in → get a token",
          responses={401: ERRORS[401], 423: {"description": "Locked after 5 failed attempts (15 minutes)"}})
def login(body: LoginIn):
    """Send your employee ID (`EMP1001`; IT staff `EMP2001`–`EMP2015`) or work email, plus the password. Copy `token` from the
    response, click **Authorize** at the top and paste it."""
    return auth.login(body.login_id.strip(), body.password)


@app.post("/auth/logout", tags=[T_AUTH], response_model=Ok, responses=ERRORS,
          summary="Sign out (revokes this token)")
def logout(request: Request, user: dict = Depends(current_user)):
    auth.logout(request.state.token, user["user_id"])
    return {"ok": True}


@app.get("/auth/me", tags=[T_AUTH], response_model=MeOut, responses=ERRORS, summary="Who am I?")
def me(user: dict = Depends(current_user)):
    """Your user, role, queues, location and asset tag, taken from the token."""
    emp = employee_profile(store, k, user["user_id"])
    return {**user, "location": emp.get("Location"), "department": emp.get("Department"),
            "asset_tag": emp.get("Asset_Tag")}


@app.post("/auth/recover", tags=[T_AUTH], response_model=RecoverOut,
          summary="Can't sign in? (password or MFA recovery)")
def recover(body: RecoverIn):
    """No token needed. With a registered factor: simulated V2 push → reset link (`reset_done`). Without one:
    a V3 supervised-recovery ticket for Identity Security (`human_v3`). Never creates a session."""
    return auth.recover(body.login_id.strip(), body.problem, body.has_registered_factor, tickets=tickets)


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=1, max_length=128)


@app.post("/auth/change-password", tags=[T_AUTH], response_model=Ok, responses=ERRORS,
          summary="Change my password (required after a temporary one)")
def change_password(body: ChangePasswordIn, request: Request, user: dict = Depends(current_user)):
    """At least 12 characters, not a common password, not containing your ID or email name. Your other
    sessions are signed out; this one stays signed in."""
    try:
        auth.change_password(user["user_id"], body.current_password, body.new_password,
                             keep_token=request.state.token)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return {"ok": True}


# ================================================================ supervisor: user accounts
class UserCreateIn(BaseModel):
    employee_id: str
    name: str = Field(min_length=1, max_length=120)
    email: str
    role: Literal["employee", "agent", "supervisor"] = "employee"
    queues: list[str] = []
    job_title: str | None = None
    department: str | None = None
    location: str | None = None
    asset_tag: str | None = None
    manager_id: str | None = None


class UserUpdateIn(BaseModel):
    name: str | None = None
    email: str | None = None
    role: Literal["employee", "agent", "supervisor"] | None = None
    queues: list[str] | None = None
    job_title: str | None = None
    department: str | None = None
    location: str | None = None
    asset_tag: str | None = None
    manager_id: str | None = None


class ImportIn(BaseModel):
    csv_text: str = Field(description="CSV with a header row: employee_id,name,email,role,queues,job_title,"
                                      "department,location,asset_tag,manager_id (queues separated by ;)")


def _admin_call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except KeyError as e:
        raise HTTPException(404, "User not found") from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@app.get("/admin/users", tags=[T_ADMIN], response_model=list[UserOut], responses=ERRORS,
         summary="List all accounts")
def admin_users(role: str | None = None, user: dict = Depends(supervisor_only)):
    return auth.users(role)


@app.post("/admin/users", tags=[T_ADMIN], response_model=TempPasswordOut, responses=ERRORS,
          summary="Create an account and get a one-time temporary password")
def admin_create_user(body: UserCreateIn, user: dict = Depends(supervisor_only)):
    """Give the temporary password to the person through a verified channel. They must change it at
    first sign-in."""
    temp = _admin_call(auth.create_user, body.employee_id, body.name, body.email, body.role, body.queues,
                       body.job_title, body.department, body.location, body.asset_tag, body.manager_id,
                       by=user["user_id"])
    return {"user_id": body.employee_id.strip().upper(), "temporary_password": temp}


@app.post("/admin/users/import", tags=[T_ADMIN], response_model=ImportOut, responses=ERRORS,
          summary="Import or update accounts from CSV (HR export)")
def admin_import(body: ImportIn, user: dict = Depends(supervisor_only)):
    rows = list(csv.DictReader(io.StringIO(body.csv_text.strip())))
    return auth.import_users(rows, by=user["user_id"])


@app.patch("/admin/users/{user_id}", tags=[T_ADMIN], response_model=UserOut, responses=ERRORS,
           summary="Change name, email, role, queues or profile")
def admin_update_user(user_id: str, body: UserUpdateIn, user: dict = Depends(supervisor_only)):
    """Role or queue changes sign the person out, because their token carries both."""
    return _admin_call(auth.update_user, user_id, by=user["user_id"], **body.model_dump())


@app.post("/admin/users/{user_id}/reset-password", tags=[T_ADMIN], response_model=TempPasswordOut,
          responses=ERRORS, summary="Reset a password after verifying the person (temporary password)")
def admin_reset_password(user_id: str, user: dict = Depends(supervisor_only)):
    """Only after verifying who is asking (e.g. call back the number on file). Signs them out everywhere."""
    temp = _admin_call(auth.reset_password, user_id, by=user["user_id"])
    return {"user_id": user_id.upper(), "temporary_password": temp}


@app.post("/admin/users/{user_id}/unlock", tags=[T_ADMIN], response_model=Ok, responses=ERRORS,
          summary="Unlock after too many failed sign-ins")
def admin_unlock(user_id: str, user: dict = Depends(supervisor_only)):
    _admin_call(auth.unlock, user_id, by=user["user_id"])
    return {"ok": True}


@app.post("/admin/users/{user_id}/disable", tags=[T_ADMIN], response_model=Ok, responses=ERRORS,
          summary="Disable a leaver (signs them out; nothing is deleted)")
def admin_disable(user_id: str, user: dict = Depends(supervisor_only)):
    if user_id.upper() == user["user_id"]:
        raise HTTPException(422, "You can't disable your own account")
    _admin_call(auth.set_status, user_id, False, by=user["user_id"])
    return {"ok": True}


@app.post("/admin/users/{user_id}/enable", tags=[T_ADMIN], response_model=Ok, responses=ERRORS,
          summary="Re-enable an account")
def admin_enable(user_id: str, user: dict = Depends(supervisor_only)):
    _admin_call(auth.set_status, user_id, True, by=user["user_id"])
    return {"ok": True}


# ================================================================ employee: chat (2B)
class ChatIn(BaseModel):
    session_id: str
    message: str = Field(default="", max_length=4000)
    attachment_ids: list[str] = Field(default=[], max_length=3,
                                      description="Screenshots uploaded first with POST /attachments")


def _own_session(session_id: str, user: dict) -> None:
    owner = store.session_owner(session_id)
    if owner and owner != user["employee_id"]:
        auth.audit("session_denied", user["user_id"], session_id=session_id)
        raise HTTPException(404, "Conversation not found")


@app.post("/chat/sessions", tags=[T_CHAT], response_model=SessionOut, responses=ERRORS,
          summary="Start a new conversation")
def new_session(user: dict = Depends(employee_only)):
    """Returns a `session_id` to use with `POST /chat`."""
    sid = f"{user['employee_id']}-{uuid.uuid4().hex[:8]}"
    store.save_session(sid, user["employee_id"], {"stage": "IDLE", "employee_id": user["employee_id"]})
    return {"session_id": sid}


@app.get("/chat/sessions", tags=[T_CHAT], response_model=list[SessionRow], responses=ERRORS,
         summary="List my conversations")
def list_sessions(user: dict = Depends(employee_only)):
    """Newest first. `in_progress` marks a chat still waiting on the employee (or with a person in it)."""
    rows = []
    for s in store.sessions_for(user["employee_id"]):
        state = json.loads(s.pop("state_json") or "{}")
        rows.append({**s, "in_progress": state.get("stage", "IDLE") not in ("IDLE", "ESCALATED", "ENDED"),
                     "ticket_id": state.get("ticket_id")})
    return rows


@app.post("/chat", tags=[T_CHAT], response_model=ChatOut, responses=ERRORS,
          summary="Send a chat message, get the bot's reply")
def chat(body: ChatIn, user: dict = Depends(employee_only)):
    """Send `session_id` + `message`. Show `reply` and `quick_replies` as buttons. `reply` is null once a human
    has taken over (the bot stays silent). Any `employee_id` in the body is ignored: identity is the token."""
    _own_session(body.session_id, user)
    atts = [_my_attachment(a, user) for a in body.attachment_ids]
    if not body.message.strip() and not atts:
        raise HTTPException(422, "Type a message or attach a screenshot")
    r = conv.handle(body.session_id, user["employee_id"], body.message.strip(), attachments=atts)
    return {"reply": r.text, "quick_replies": r.quick_replies, "ticket_id": r.ticket_id, **_reply_extras(r)}


def _employee_message(m: dict, ratings: dict | None = None) -> dict:
    """Customer-visible fields only: no trace, no Jev scores."""
    meta = m["meta"]
    return {"id": m["id"], "role": m["role"], "text": m["text"], "created_at": m["created_at"],
            "ticket_id": m["ticket_id"], "agent": meta.get("agent"), "quick_replies": meta.get("quick_replies", []),
            "attempt": meta.get("attempt"), "can_rate": bool(meta.get("rate")),
            "rating": (ratings or {}).get(m["id"]), "attachment_ids": meta.get("attachment_ids", []),
            "ended": bool(meta.get("ended")), "new_problem": meta.get("carry")}


def _my_ratings(employee_id: str) -> dict[int, bool]:
    return {r["message_id"]: bool(r["helpful"]) for r in
            store.query("SELECT message_id, helpful FROM answer_feedback WHERE employee_id=?", (employee_id,))}


@app.get("/chat/sessions/{session_id}/messages", tags=[T_CHAT], response_model=list[MessageOut],
         responses=ERRORS, summary="Read a conversation")
def session_messages(session_id: str, user: dict = Depends(employee_only)):
    """Customer-visible messages only: no bot trace, no Jev scores, no work notes."""
    _own_session(session_id, user)
    ratings = _my_ratings(user["employee_id"])
    return [_employee_message(m, ratings) for m in store.messages(session_id=session_id, customer_only=True)]


# ================================================================ screenshots (Phase 4)
def _my_attachment(att_id: str, user: dict) -> dict:
    a = atts.get(att_id)
    if not a or a["uploaded_by"] != user["user_id"]:
        raise HTTPException(404, "Attachment not found")
    return a


def _can_see_attachment(a: dict, user: dict) -> bool:
    if a["uploaded_by"] == user["user_id"]:
        return True
    if user["role"] == "employee":
        return False
    t = store.get_ticket(a["ticket_id"] or "")
    return bool(t and tickets.can_view(user, t))


@app.post("/attachments", tags=[T_CHAT], response_model=AttachmentOut, responses=ERRORS,
          summary="Upload a screenshot → text read from it (OCR), secrets blurred")
async def upload_attachment(file: UploadFile, session_id: str | None = Form(None), ticket_id: str | None = Form(None),
                            user: dict = Depends(current_user)):
    """PNG, JPG or WebP up to 10 MB. The image is re-saved without hidden metadata (like GPS), read on this
    server, and any visible password or code is blurred before it's stored. Pass the returned `id` in
    `attachment_ids` on /chat, or on the ticket form. Staff may attach to tickets they can see."""
    if session_id:
        _own_session(session_id, user)
    if ticket_id:
        _ticket_for(ticket_id, user)
    data = await file.read(config.ATTACHMENT_MAX_MB * 1024 * 1024 + 1)
    try:
        return await run_in_threadpool(atts.process, data, file.filename or "screenshot", user["user_id"],
                                       session_id, ticket_id)
    except AttachmentError as e:
        raise HTTPException(422, str(e)) from e


@app.get("/attachments/{att_id}", tags=[T_CHAT], responses=ERRORS, summary="View a screenshot (PNG)")
def get_attachment(att_id: str, user: dict = Depends(current_user)):
    """The uploader, and support staff who can see its ticket. Every staff view is recorded in the audit log."""
    a = atts.get(att_id)
    if not a or not _can_see_attachment(a, user):
        raise HTTPException(404, "Attachment not found")
    path = atts.path(a)
    if not path:
        raise HTTPException(410, "This screenshot was deleted after the retention period")
    if a["uploaded_by"] != user["user_id"]:
        store.log_event("Attachment viewed", {"detail": f"screenshot {att_id[:8]} viewed by {user['name']}"},
                        ticket_id=a["ticket_id"], actor=user["user_id"])
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, no-store"})


# ================================================================ employee: ticket form (2B)
class FormIn(BaseModel):
    category_id: str | None = None  # None = "Not sure"
    issue_type: str | None = None
    short_description: str = Field(min_length=3, max_length=300)
    impact_choice: str
    workaround: str
    exact_error: str | None = Field(default=None, max_length=2000)
    details: dict[str, str] = {}
    attachment_ids: list[str] = Field(default=[], max_length=3,
                                      description="Screenshots uploaded first with POST /attachments")
    preferred_contact: str = "In-app"


class SubmitIn(BaseModel):
    review_id: str
    accept_suggestion: bool = False
    duplicate_of: str | None = None


class ChoiceIn(BaseModel):
    choice: Literal["fix_now", "just_log"]


@app.post("/forms/review", tags=[T_FORM], response_model=ReviewOut, responses=ERRORS,
          summary="Step 1: review the form (one Jev check)")
def form_review(body: FormIn, user: dict = Depends(employee_only)):
    """Computes priority, checks the category, masks secrets, looks for a duplicate and a 'try this first' KB
    step. Creates nothing. `category_id: null` means "Not sure". Pass the returned `review_id` to step 2."""
    if body.category_id and body.category_id not in k.categories:
        raise HTTPException(400, "Unknown category")
    if body.impact_choice not in FORM_IMPACT_OPTIONS or body.workaround not in FORM_WORKAROUND_OPTIONS:
        raise HTTPException(400, "Impact and workaround must be one of the listed options")
    form = body.model_dump()
    shots = [_my_attachment(a, user) for a in body.attachment_ids]
    if shots:  # the screenshot text is read by the same review as the typed fields
        form["screenshot_text"], form["screenshot_note"] = summary_for_bot(shots)
        err = next((ln for s in shots if s["readable"] for ln in problem_lines(s["ocr_text"] or "")), "")
        other = conv.screenshot_mismatch(body.category_id, err) if err and body.category_id else None
        if other:  # 7 Oct: a screenshot of a different problem went in without a word
            form["screenshot_warning"] = (
                f"Your screenshot shows \"{err[:120]}\", which looks like a {k.name(other)} problem, not "
                f"{k.name(body.category_id)}. Did you attach the right one? If not, remove it and add the right "
                "screenshot, or change the category.")
    return conv.review_form(user["employee_id"], form)


@app.post("/forms/submit", tags=[T_FORM], response_model=SubmitOut, responses=ERRORS,
          summary="Step 2: submit the reviewed form")
def form_submit(body: SubmitIn, user: dict = Depends(employee_only)):
    """Creates the ticket and runs the same policy gates as chat. `outcome=escalated`: a human has it.
    `outcome=choose`: go to step 3. `duplicate_of` adds the details to that open ticket instead."""
    try:
        return conv.submit_form(user["employee_id"], body.review_id, body.accept_suggestion, body.duplicate_of)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@app.post("/forms/{ticket_id}/choice", tags=[T_FORM], response_model=ChoiceOut, responses=ERRORS,
          summary="Step 3: fix it now, or just log it")
def form_choice(ticket_id: str, body: ChoiceIn, user: dict = Depends(employee_only)):
    """`fix_now`: the bot starts at attempt 1 in the returned chat session. `just_log`: sent to the team."""
    try:
        return conv.form_choice(user["employee_id"], ticket_id, body.choice)
    except PermissionError as e:
        raise HTTPException(404, "Ticket not found") from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


# ================================================================ employee: My tickets (2B)
def _employee_ticket(t: dict, with_thread: bool = False) -> dict:
    owner = tickets.user_name(t["owner"])
    sla = tickets.sla_status(t)
    out = {"ticket_id": t["ticket_id"], "status": t["status"], "state": employee_label(t, owner),
           "priority": t["priority"], "summary": t["summary"], "category": k.name(t["category_id"]),
           "subcategory": t["subcategory"], "queue": t["queue"], "owner": owner, "channel": t["channel"],
           "location": t["location"], "config_item": t["config_item"], "created_at": t["created_at"],
           "updated_at": t["updated_at"], "resolved_at": t["resolved_at"], "resolution_code": t["resolution_code"],
           "resolve_due": sla.get("resolve_due"), "incident_parent": t["incident_parent"],
           "session_id": t["session_id"], "type": t["ticket_type"],
           "can_reopen": t["status"] in ("RESOLVED_PENDING_CONFIRMATION", "RESOLVED")
           and tickets.within_reopen_window(t),
           "can_confirm": t["status"] == "RESOLVED_PENDING_CONFIRMATION",
           "can_cancel": t["status"] in BOT_STATES + HUMAN_STATES,
           "bot_active": t["status"] in BOT_STATES,
           "can_rate": t["status"] in RATEABLE_STATES}
    rating = store.one("SELECT score, comment FROM ticket_ratings WHERE ticket_id=?", (t["ticket_id"],))
    out["rating"] = rating["score"] if rating else None
    if with_thread:
        msgs = store.messages(ticket_id=t["ticket_id"], customer_only=True)
        if t["session_id"]:  # chat turns before the ticket existed belong to the same conversation
            seen = {m["id"] for m in msgs}
            msgs += [m for m in store.messages(session_id=t["session_id"], customer_only=True)
                     if m["id"] not in seen and (m["ticket_id"] in (None, t["ticket_id"]))]
            msgs.sort(key=lambda m: m["id"])
        ratings = _my_ratings(t["employee_id"])
        out["thread"] = [_employee_message(m, ratings) for m in msgs]
    return out


RATEABLE_STATES = ("RESOLVED_PENDING_CONFIRMATION", "RESOLVED", "CLOSED")


@app.get("/me/tickets", tags=[T_MINE], response_model=list[EmployeeTicket], responses=ERRORS,
         summary="List my tickets")
def my_tickets(user: dict = Depends(employee_only)):
    """Own tickets only, with a friendly state label and which actions are allowed."""
    if config.API_RUNS_TIMERS:
        tickets.sweep()
    return [_employee_ticket(t) for t in store.tickets(employee_id=user["employee_id"])]


@app.get("/me/tickets/{ticket_id}", tags=[T_MINE], response_model=EmployeeTicketDetail, responses=ERRORS,
         summary="Open one of my tickets")
def my_ticket(ticket_id: str, user: dict = Depends(employee_only)):
    """Status, owner, target fix time and the customer-visible thread (never work notes)."""
    return _employee_ticket(_ticket_for(ticket_id, user), with_thread=True)


class TextIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@app.post("/me/tickets/{ticket_id}/reply", tags=[T_MINE], response_model=ReplyOut, responses=ERRORS,
          summary="Reply on my ticket")
def my_reply(ticket_id: str, body: TextIn, user: dict = Depends(employee_only)):
    """Reply routing: bot still working → the chat pipeline; human side → comment; resolved ≤ 7 days →
    reopen; older → new ticket linked to the old one (spec §5.4)."""
    t = _ticket_for(ticket_id, user)
    if t["status"] in BOT_STATES and t["session_id"]:
        r = conv.handle(t["session_id"], user["employee_id"], body.text)
        return {"action": "chat", "reply": r.text, "quick_replies": r.quick_replies}
    if t["status"] in HUMAN_STATES:
        tickets.comment(ticket_id, user, body.text, "customer")
        for uid in {t["owner"], *tickets.watchers(ticket_id)} - {None}:
            notify.notify(uid, ticket_id, f"Employee replied on {ticket_id}: {body.text[:80]}")
        return {"action": "comment"}
    if t["status"] == "RESOLVED_PENDING_CONFIRMATION" and is_thanks(body.text):
        # "thanks, works now" is a confirmation, not a reason to reopen (7 Oct: it reopened the ticket)
        tickets.confirm(ticket_id, user["employee_id"])
        return {"action": "confirmed"}
    if t["status"] in ("RESOLVED_PENDING_CONFIRMATION", "RESOLVED") and tickets.within_reopen_window(t):
        tickets.reopen(ticket_id, user["employee_id"], body.text)
        return {"action": "reopened"}
    if t["status"] in ("RESOLVED", "CLOSED", "RESOLVED_PENDING_CONFIRMATION", "CANCELLED"):
        new = tickets.create(session_id=t["session_id"], employee_id=user["employee_id"],
                             category_id=t["category_id"], impact=t["impact"], urgency=t["urgency"],
                             flags=json.loads(t["triage_json"] or "{}").get("flags", {}),
                             summary=f"Follow-up to {ticket_id}: {body.text}", channel=t["channel"],
                             opened_by="Employee (follow-up)", actor=user["employee_id"], incident_parent=None)
        store.add_message(t["session_id"], "user", body.text, ticket_id=new, author=user["employee_id"])
        tickets.escalate(new, f"Follow-up to {ticket_id} after the 7-day reopen window",
                         {"previous_ticket": ticket_id, "original_statement": body.text}, actor=user["employee_id"])
        store.log_event("Linked", {"detail": f"follow-up of {ticket_id}"}, ticket_id=new, actor=user["employee_id"])
        return {"action": "new_ticket", "ticket_id": new}
    raise HTTPException(409, f"Can't reply in state {t['status']}")


@app.post("/me/tickets/{ticket_id}/reopen", tags=[T_MINE], response_model=Ok, responses=ERRORS,
          summary="Reopen (within 7 days of resolution)")
def my_reopen(ticket_id: str, body: TextIn, user: dict = Depends(employee_only)):
    _ticket_for(ticket_id, user)
    tickets.reopen(ticket_id, user["employee_id"], body.text)
    return {"ok": True}


@app.post("/me/tickets/{ticket_id}/confirm", tags=[T_MINE], response_model=Ok, responses=ERRORS,
          summary="Confirm it's fixed")
def my_confirm(ticket_id: str, user: dict = Depends(employee_only)):
    _ticket_for(ticket_id, user)
    tickets.confirm(ticket_id, user["employee_id"])
    return {"ok": True}


@app.post("/me/tickets/{ticket_id}/cancel", tags=[T_MINE], response_model=Ok, responses=ERRORS,
          summary="Cancel my ticket (a reason is required)")
def my_cancel(ticket_id: str, body: TextIn, user: dict = Depends(employee_only)):
    _ticket_for(ticket_id, user)
    tickets.cancel(ticket_id, user, body.text, "Cancelled by user")
    return {"ok": True}


class AnswerFeedbackIn(BaseModel):
    message_id: int
    helpful: bool
    reason: Literal["unclear", "didnt_work", "wrong_problem", "other"] | None = None
    comment: str | None = Field(default=None, max_length=500)


@app.post("/me/feedback/answer", tags=[T_MINE], response_model=Ok, responses=ERRORS,
          summary="Rate a bot answer 👍 / 👎 (optionally say why)")
def rate_answer(body: AnswerFeedbackIn, user: dict = Depends(employee_only)):
    """Only bot answers marked `can_rate`, only in your own conversations. Rating again replaces your earlier
    rating. Feeds the helpfulness numbers per article on the Knowledge page and the dashboard."""
    m = store.one("SELECT * FROM messages WHERE id=?", (body.message_id,))
    meta = json.loads(m["meta_json"] or "{}") if m else {}
    if not m or m["role"] != "bot" or not meta.get("rate") or store.session_owner(m["session_id"]) != user["employee_id"]:
        raise HTTPException(404, "Message not found")
    ans = store.one("SELECT kb_id, kb_version, ticket_id FROM bot_answers WHERE id=?", (meta["bot_answer_id"],)) or {}
    comment = redact_inline(body.comment.strip()) if body.comment and body.comment.strip() else None
    store.execute("INSERT INTO answer_feedback (message_id,bot_answer_id,ticket_id,kb_id,kb_version,employee_id,"
                  "helpful,reason,comment,created_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(message_id, "
                  "employee_id) DO UPDATE SET helpful=excluded.helpful, reason=excluded.reason, "
                  "comment=excluded.comment, created_at=excluded.created_at",
                  (body.message_id, meta["bot_answer_id"], ans.get("ticket_id"), ans.get("kb_id"),
                   ans.get("kb_version"), user["employee_id"], int(body.helpful), body.reason, comment, now()))
    return {"ok": True}


class RatingIn(BaseModel):
    score: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=1000)


@app.post("/me/tickets/{ticket_id}/rating", tags=[T_MINE], response_model=Ok, responses=ERRORS,
          summary="Rate how it went (1–5), once the ticket is fixed")
def rate_ticket(ticket_id: str, body: RatingIn, user: dict = Depends(employee_only)):
    """Available once the ticket is resolved. A low score (1–2) notifies the person who resolved it and the
    supervisors, so someone can follow up."""
    t = _ticket_for(ticket_id, user)
    if t["status"] not in RATEABLE_STATES:
        raise HTTPException(409, "You can rate a ticket once it's fixed")
    comment = redact_inline(body.comment.strip()) if body.comment and body.comment.strip() else None
    store.execute("INSERT INTO ticket_ratings (ticket_id,employee_id,score,comment,created_at) VALUES (?,?,?,?,?) "
                  "ON CONFLICT(ticket_id) DO UPDATE SET score=excluded.score, comment=excluded.comment, "
                  "created_at=excluded.created_at", (ticket_id, user["employee_id"], body.score, comment, now()))
    store.log_event("Rated", {"detail": f"{body.score}/5"}, ticket_id=ticket_id, actor=user["employee_id"])
    if body.score <= 2:
        for uid in {t["owner"], *[u["user_id"] for u in auth.users("supervisor", include_disabled=False)]} - {None}:
            notify.notify(uid, ticket_id, f"{ticket_id} was rated {body.score}/5 by the employee. Worth a follow-up.")
    return {"ok": True}


@app.get("/me/data", tags=[T_AUTH], responses=ERRORS, summary="Download everything held about me (JSON)")
def my_data(user: dict = Depends(current_user)):
    """Your profile, tickets, conversations, ratings, feedback and screenshot records. Internal work notes and
    bot scores aren't included. The request is recorded in the audit log."""
    store.log_event("Data export", {"detail": f"{user['user_id']} downloaded their data"}, actor=user["user_id"])
    return JSONResponse(privacy.export(user["user_id"]),
                        headers={"Content-Disposition": f"attachment; filename=my-it-data-{user['user_id']}.json"})


@app.get("/me/notifications", tags=[T_NOTE], response_model=list[NotificationOut], responses=ERRORS,
         summary="My notifications")
def my_notifications(unread: bool = False, user: dict = Depends(current_user)):
    """Employees: agent comments, resolved, reassigned. Agents: employee replies on tickets they own."""
    return notify.list(user["user_id"], unread_only=unread)


class ReadIn(BaseModel):
    ids: list[int] = []


@app.post("/me/notifications/read", tags=[T_NOTE], response_model=Ok, responses=ERRORS,
          summary="Mark notifications read")
def read_notifications(body: ReadIn, user: dict = Depends(current_user)):
    """Send the `ids` to mark; an empty list marks everything read."""
    notify.mark_read(user["user_id"], body.ids or None)
    return {"ok": True}


# ================================================================ support: queue + ticket form (2C)
@app.get("/queue", tags=[T_TICK], response_model=list[QueueRow], responses=ERRORS,
         summary="My queue (sorted P1 → P4, then SLA due)")
def queue(include_closed: bool = False, user: dict = Depends(support_only)):
    """Agents: their own queues + every P1. Supervisors: everything."""
    if config.API_RUNS_TIMERS:
        tickets.sweep()
    rows = tickets.queue_for(user, include_closed)
    return [{"ticket_id": t["ticket_id"], "priority": t["priority"], "status": t["status"],
             "category": k.name(t["category_id"]), "queue": t["queue"], "owner": tickets.user_name(t["owner"]),
             "owner_id": t["owner"], "summary": t["summary"], "channel": t["channel"],
             "created_at": t["created_at"], "updated_at": t["updated_at"], "sla": t["sla"],
             "escalation_reason": t["escalation_reason"], "incident_parent": t["incident_parent"],
             "is_incident": t["is_incident"], "requester": t["employee_id"]} for t in rows]


def _bot_panel(t: dict) -> dict:
    tri = json.loads(t["triage_json"] or "{}")
    flags = tri.get("flags", {})
    return {"flags": {f: round(v, 2) for f, v in sorted(flags.items(), key=lambda x: -x[1]) if v >= 0.1},
            "raised": [f for f, v in flags.items() if v >= config.RISK_FLAG_THRESHOLD],
            "category_confidence": t["jev_confidence"], "intent": tri.get("intent"),
            "top_categories": [(k.name(c), p) for c, p in tri.get("categories", [])[:3]] if tri.get("categories")
            else [], "verification": t["verification"], "attempts": t["attempts"], "kb_id": t["kb_id"],
            "escalation_reason": t["escalation_reason"], "priority_reason": t["priority_reason"],
            "user_category": k.name(t["user_category"]) if t["user_category"] in k.categories else t["user_category"],
            "impact": t["impact"], "urgency": t["urgency"]}


ACTIONS = {"HUMAN_ASSIGNED": "claim", "HUMAN_IN_PROGRESS": "take_over", "ESCALATION_QUEUED": "reassign",
           "RESOLVED_PENDING_CONFIRMATION": "resolve", "CANCELLED": "cancel"}


_DID = {"Opened": "Logged the ticket", "Claimed": "Claimed", "Form updated": "Edited the form",
        "Comment": "Commented to the employee", "Work note": "Added work notes", "Checklist": "Worked the checklist",
        "Alert acknowledged": "Acknowledged the alert", "Corrected": "Corrected the bot"}
_DID_STATUS = {"HUMAN_IN_PROGRESS": "Worked on it", "RESOLVED_PENDING_CONFIRMATION": "Resolved",
               "CANCELLED": "Cancelled", "ESCALATION_QUEUED": "Reassigned", "HUMAN_ASSIGNED": "Claimed"}


def _people(t: dict, events: list[dict], watchers: list[str]) -> list[dict]:
    """Everyone who touched the ticket: the requester, then each staff member with what they did and when."""
    staff = {u["user_id"]: u for u in auth.users() if u["role"] != "employee"}
    seen: dict[str, dict] = {}

    def add(uid: str | None, did: str | None, at: str | None) -> None:
        if not uid or uid not in staff:
            return
        p = seen.setdefault(uid, {"user_id": uid, "name": staff[uid]["name"], "role": staff[uid]["role"],
                                  "job_title": staff[uid]["job_title"], "team": ", ".join(staff[uid]["queues"])
                                  if staff[uid]["role"] == "agent" else "All queues",
                                  "did": [], "first_at": at, "last_at": at, "current_owner": uid == t["owner"]})
        if did and did not in p["did"]:
            p["did"].append(did)
        if at:
            p["first_at"], p["last_at"] = min(filter(None, (p["first_at"], at))), max(filter(None, (p["last_at"], at)))

    for e in events:
        did = _DID.get(e["event_type"])
        if e["event_type"] == "Status":
            frm, _, to = str(e["payload"].get("detail", "")).partition("→")
            to = to.split("·")[0].strip()
            # a take-over passes through the queue state; only a move away from a human is a reassignment
            did = None if to == "ESCALATION_QUEUED" and not frm.strip().startswith("HUMAN_") else _DID_STATUS.get(to)
        add(e["actor"], did, e["created_at"])
    add(t["owner"], "Current owner", None)
    for w in watchers:
        add(w, "Watching", None)
    emp = employee_profile(store, k, t["employee_id"])
    requester = {"user_id": t["employee_id"], "name": emp.get("Full_Name") or t["employee_id"], "role": "requester",
                 "job_title": emp.get("Job_Title") or emp.get("Department"), "team": emp.get("Department"),
                 "did": ["Raised the ticket"], "first_at": t["created_at"], "last_at": None, "current_owner": False}
    return [requester] + sorted(seen.values(), key=lambda p: p["first_at"] or "9")


@app.get("/tickets/{ticket_id}", tags=[T_TICK], response_model=TicketDetailOut, responses=ERRORS,
         summary="Open a ticket (full support view)")
def ticket_detail(ticket_id: str, user: dict = Depends(support_only)):
    """Fields, bot panel, suggested KB, checklist, all messages incl. work notes, timeline, the legal next
    states and alerts. Keep `ticket.version` for `/update`. Each staff view is recorded (who looked at whose
    data), at most once per person per ticket per hour."""
    t = _ticket_for(ticket_id, user)
    if not store.one("SELECT 1 AS x FROM events WHERE ticket_id=? AND event_type='Ticket viewed' AND actor=? AND "
                     "created_at>=?", (ticket_id, user["user_id"], ago(hours=1))):
        store.log_event("Ticket viewed", {"detail": f"opened by {user['name']}"}, ticket_id=ticket_id,
                        actor=user["user_id"])
    tri = json.loads(t["triage_json"] or "{}")
    kb_scores = tri.get("kb_scores", {})
    legal = [s for s in tickets.legal_next(t["status"]) if s in ACTIONS]
    names = {u["user_id"]: u["name"] for u in auth.users() if u["role"] != "employee"}
    watchers = tickets.watchers(ticket_id)
    events = [{**e, "actor_name": names.get(e["actor"])} for e in store.events(ticket_id)]
    return {"ticket": {**t, "category": k.name(t["category_id"]), "owner_name": tickets.user_name(t["owner"]),
                       "caller_name": employee_profile(store, k, t["employee_id"]).get("Full_Name"),
                       "handoff": json.loads(t["handoff_json"] or "null"), "form": json.loads(t["form_json"] or
                                                                                              "null")},
            "sla": tickets.sla_status(t), "bot_panel": _bot_panel(t),
            "suggested_kb": [{"kb_id": kid, "title": k.kb[kid].title, "relevance": round(p, 3),
                              "steps": k.kb[kid].steps_raw} for kid, p in
                             sorted(kb_scores.items(), key=lambda x: -x[1])[:4]],
            "checklist": tickets.checklist(ticket_id),
            "messages": store.messages(ticket_id=ticket_id) if not t["session_id"] else
            [m for m in store.messages(session_id=t["session_id"]) if m["ticket_id"] in (None, ticket_id)],
            "events": events, "watchers": [names.get(w, w) for w in watchers],
            "people": _people(t, events, watchers),
            "linked": [x["ticket_id"] for x in store.query("SELECT ticket_id FROM tickets WHERE incident_parent=?",
                                                           (ticket_id,))],
            "legal_next": [{"state": s, "action": ACTIONS[s]} for s in legal],
            "alerts": store.query("SELECT * FROM alerts WHERE ticket_id=? ORDER BY alert_id", (ticket_id,)),
            "attachments": [{k_: v for k_, v in a.items() if k_ not in ("sha256",)}
                            for a in atts.for_ticket(ticket_id, t["session_id"])]}


@app.post("/tickets/{ticket_id}/claim", tags=[T_TICK], response_model=TicketOut, responses=ERRORS,
          summary="Claim a queued ticket")
def claim(ticket_id: str, user: dict = Depends(support_only)):
    """Atomic: if two agents claim at once, one wins and the other gets 409 'Owned by …'."""
    _ticket_for(ticket_id, user)
    return tickets.claim(ticket_id, user)


@app.post("/tickets/{ticket_id}/takeover", tags=[T_TICK], response_model=TicketOut, responses=ERRORS,
          summary="Take over (claim + start in one step)")
def takeover(ticket_id: str, user: dict = Depends(support_only)):
    """Also works mid-conversation: the bot goes silent and the employee sees '… joined the chat'."""
    _ticket_for(ticket_id, user)
    return tickets.take_over(ticket_id, user)


@app.post("/tickets/{ticket_id}/start", tags=[T_TICK], response_model=TicketOut, responses=ERRORS,
          summary="Start work on a ticket you claimed")
def start(ticket_id: str, user: dict = Depends(support_only)):
    _ticket_for(ticket_id, user)
    return tickets.start(ticket_id, user)


class UpdateIn(BaseModel):
    version: int
    changes: dict[str, str | None]
    reason: str = ""


@app.post("/tickets/{ticket_id}/update", tags=[T_TICK], response_model=TicketOut, responses=ERRORS,
          summary="Save form changes (version-checked)")
def update(ticket_id: str, body: UpdateIn, user: dict = Depends(support_only)):
    """Send the `version` you loaded. If someone saved first: 409 'X updated this, reload'. Changing category,
    impact, urgency or priority needs a `reason` and is logged as a correction; only a supervisor can override
    priority. Example `changes`: `{"category_id": "CAT-02", "subcategory": "Cannot connect"}`."""
    _ticket_for(ticket_id, user)
    return tickets.update(ticket_id, user, body.changes, body.version, body.reason)


class CommentIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    visibility: Literal["customer", "internal"]


@app.post("/tickets/{ticket_id}/comment", tags=[T_TICK], response_model=Ok, responses=ERRORS,
          summary="Add a comment (customer) or work note (internal)")
def comment(ticket_id: str, body: CommentIn, user: dict = Depends(support_only)):
    _ticket_for(ticket_id, user)
    tickets.comment(ticket_id, user, body.text, body.visibility)
    return {"ok": True}


class TickIn(BaseModel):
    done: bool


@app.post("/tickets/{ticket_id}/checklist/{item_id}", tags=[T_TICK], response_model=list[ChecklistItem],
          responses=ERRORS, summary="Tick or untick a checklist item (owner only)")
def tick(ticket_id: str, item_id: str, body: TickIn, user: dict = Depends(support_only)):
    _ticket_for(ticket_id, user)
    return tickets.tick(ticket_id, item_id, user, body.done)


class ResolveIn(BaseModel):
    resolution_code: str
    notes: str


@app.post("/tickets/{ticket_id}/resolve", tags=[T_TICK], response_model=TicketOut, responses=ERRORS,
          summary="Resolve (blocked until the checklist is done)")
def resolve(ticket_id: str, body: ResolveIn, user: dict = Depends(support_only)):
    """Needs every required checklist item, a resolution code and notes; otherwise 409 with a `missing` list."""
    _ticket_for(ticket_id, user)
    return tickets.resolve(ticket_id, user, body.resolution_code, body.notes)


class CancelIn(BaseModel):
    reason: str
    duplicate_of: str | None = None


@app.post("/tickets/{ticket_id}/cancel", tags=[T_TICK], response_model=TicketOut, responses=ERRORS,
          summary="Cancel, or close as duplicate")
def cancel(ticket_id: str, body: CancelIn, user: dict = Depends(support_only)):
    """Nothing is ever deleted: the ticket is cancelled and audited. Set `duplicate_of` to close as duplicate."""
    _ticket_for(ticket_id, user)
    if body.duplicate_of and not store.get_ticket(body.duplicate_of):
        raise HTTPException(400, f"{body.duplicate_of} not found")
    return tickets.cancel(ticket_id, user, body.reason, "Duplicate" if body.duplicate_of else "Cancelled by user",
                          body.duplicate_of)


class ReassignIn(BaseModel):
    queue: str
    reason: str


@app.post("/tickets/{ticket_id}/reassign", tags=[T_TICK], response_model=TicketOut, responses=ERRORS,
          summary="Reassign to another queue")
def reassign(ticket_id: str, body: ReassignIn, user: dict = Depends(support_only)):
    _ticket_for(ticket_id, user)
    return tickets.reassign(ticket_id, user, body.queue, body.reason)


@app.post("/tickets/{ticket_id}/watch", tags=[T_TICK], response_model=Ok, responses=ERRORS,
          summary="Add me to the watch list")
def watch(ticket_id: str, user: dict = Depends(support_only)):
    _ticket_for(ticket_id, user)
    tickets.watch(ticket_id, user["user_id"])
    return {"ok": True}


class PhoneIn(BaseModel):
    caller_employee_id: str
    category_id: str
    short_description: str = Field(min_length=3, max_length=300)
    impact: Literal["extensive", "significant", "moderate", "limited"]
    urgency: Literal["critical", "high", "medium", "low"]


@app.post("/tickets", tags=[T_TICK], response_model=TicketOut, responses=ERRORS,
          summary="Log a phone call as a ticket")
def log_call(body: PhoneIn, user: dict = Depends(support_only)):
    """An agent logs a phone call: contact type Phone, opened by the agent, owned by them."""
    if not employee_exists(store, k, body.caller_employee_id) or body.category_id not in k.categories:
        raise HTTPException(400, "Unknown caller or category")
    tid = tickets.create(session_id=None, employee_id=body.caller_employee_id, category_id=body.category_id,
                         impact=body.impact, urgency=body.urgency, flags={}, summary=body.short_description,
                         channel="Phone", opened_by=user["name"], actor=user["user_id"])
    tickets.escalate(tid, f"Logged from a phone call by {user['name']}", {"caller": body.caller_employee_id},
                     actor=user["user_id"], alert=False)
    return tickets.take_over(tid, user)


# ================================================================ alerts (2C)
@app.get("/alerts/pending", tags=[T_ALERT], response_model=PendingAlertsOut, responses=ERRORS,
         summary="Alerts I should see now (poll every 10 s)")
def pending_alerts(user: dict = Depends(support_only)):
    """In production the worker advances the timers (P1 re-alert every 2 min, supervisor escalation, SLA
    risk); in development this poll does it."""
    if config.API_RUNS_TIMERS:
        alerts.tick()
    return alerts.pending(user)


@app.post("/alerts/{alert_id}/ack", tags=[T_ALERT], response_model=AlertOut, responses=ERRORS,
          summary="Acknowledge an alert")
def ack_alert(alert_id: int, user: dict = Depends(support_only)):
    try:
        return alerts.ack(alert_id, user)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e


# ================================================================ analytics (2C, read-only)
def _filters(date_from, date_to, queue_, category, channel, priority, user) -> dict:
    f = {"date_from": date_from, "date_to": date_to, "queues": queue_, "categories": category,
         "channels": channel, "priorities": priority}
    if user["role"] == "agent":  # agents see their queue view
        f["queues"] = [q for q in (queue_ or user["queues"]) if q in user["queues"]] or user["queues"]
    return f


@app.get("/analytics/dashboard", tags=[T_INS], response_model=DashboardOut, responses=ERRORS,
         summary="KPI tiles + charts C1–C12")
def dashboard(date_from: str | None = None, date_to: str | None = None,
              queue: list[str] | None = Query(None), category: list[str] | None = Query(None),
              channel: list[str] | None = Query(None), priority: list[str] | None = Query(None),
              user: dict = Depends(support_only)):
    f = _filters(date_from, date_to, queue, category, channel, priority, user)
    return {"kpis": analytics.kpis(f), "charts": analytics.charts(f), "scope": f}


@app.get("/analytics/bot-answers", tags=[T_INS], response_model=BotAnswersOut, responses=ERRORS,
         summary="Bot answers log + KB success rates")
def bot_answers(kb_id: str | None = None, category_id: list[str] | None = Query(None), date_from: str | None = None,
                date_to: str | None = None, queue: list[str] | None = Query(None), agent: str | None = None,
                outcome: str | None = None, search: str | None = None, user: dict = Depends(support_only)):
    """Agents see their own teams' answers only (7 Oct: every agent saw every team's); a supervisor sees all,
    narrowed by team, agent, category, article, outcome, dates or a search. The success table is worked out from
    the same rows."""
    queues = queue or None
    if user["role"] == "agent":
        queues = [q for q in (queue or user["queues"]) if q in user["queues"]] or user["queues"]
    return analytics.bot_answers({"queues": queues, "agent": agent, "categories": category_id, "kb_id": kb_id,
                                  "outcome": outcome, "date_from": date_from, "date_to": date_to,
                                  "search": search})


@app.get("/analytics/performance", tags=[T_INS], response_model=list[PerformanceRow], responses=ERRORS,
         summary="Agent performance (agents: own row; supervisor: all)")
def performance(date_from: str | None = None, date_to: str | None = None, user: dict = Depends(support_only)):
    return analytics.performance(date_from, date_to, None if user["role"] == "supervisor" else user["user_id"])


@app.get("/analytics/shift", tags=[T_INS], response_model=ShiftOut, responses=ERRORS,
         summary="Shift summary + shift checklist")
def shift(day: str | None = None, user: dict = Depends(support_only)):
    items = {r["item_id"]: r for r in store.query(
        "SELECT * FROM shift_checklists WHERE user_id=? AND shift_date=?",
        (user["user_id"], day or analytics.shift_summary()["day"]))}
    return {"summary": analytics.shift_summary(day), "status": analytics.shift_status(),
            "checklist": [{"item_id": i, "text": txt, "done_at": items.get(i, {}).get("done_at"),
                           "notes": items.get(i, {}).get("notes")} for i, txt in analytics.SHIFT_ITEMS]}


class ShiftTickIn(BaseModel):
    done: bool
    notes: str | None = None


@app.post("/analytics/shift/{item_id}", tags=[T_INS], response_model=Ok, responses=ERRORS,
          summary="Tick a shift checklist item")
def shift_tick(item_id: str, body: ShiftTickIn, user: dict = Depends(support_only)):
    if item_id not in dict(analytics.SHIFT_ITEMS):
        raise HTTPException(404)
    day = analytics.shift_summary()["day"]
    store.execute("INSERT INTO shift_checklists (user_id,shift_date,item_id,done_at,notes) VALUES (?,?,?,?,?) ON CONFLICT(user_id,shift_date,item_id) DO "
                  "UPDATE SET done_at=excluded.done_at, notes=excluded.notes",
                  (user["user_id"], day, item_id, now() if body.done else None, body.notes))
    store.log_event("Shift checklist", {"detail": f"{'✓' if body.done else '✗'} {item_id}"}, actor=user["user_id"])
    return {"ok": True}


@app.get("/corrections", tags=[T_INS], response_model=list[CorrectionOut], responses=ERRORS,
         summary="Corrections log (supervisor only)")
def corrections(user: dict = Depends(supervisor_only)):
    return [{**r, "corrected_by_name": tickets.user_name(r["corrected_by"])} for r in store.corrections()]


# ================================================================ knowledge: articles, gaps, saved replies
class StepIn(BaseModel):
    who: Literal["you", "it"]
    intro: str | None = None
    steps: list[str] = []
    why: str | None = None
    note: str | None = None


class DraftIn(BaseModel):
    title: str | None = None
    summary: str = Field(min_length=1, max_length=600)
    attempt1: StepIn | None = None
    attempt2: StepIn | None = None
    handoff_message: str | None = Field(default=None, max_length=800)
    note: str | None = Field(default=None, max_length=200, description="What changed and why")


class ApproveIn(BaseModel):
    override_reason: str | None = Field(default=None, description="Required only if the meaning check flagged "
                                                                  "a step and you've checked it yourself")


def _kb_call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except KBError as e:
        raise HTTPException(422, str(e)) from e


@app.get("/kb", tags=[T_KB], responses=ERRORS, summary="All articles: live version, pending draft, helpfulness")
def kb_articles(user: dict = Depends(support_only)):
    """Each row carries the team that owns its category. Agents get their own teams' articles (7 Oct: like the
    bot answers log, every agent saw everything)."""
    return [a for a in kbs.articles() if _team_visible(user, a["team"])]


def _team_visible(user: dict, team: str | None) -> bool:
    return user["role"] == "supervisor" or team in user["queues"]


@app.get("/kb/gaps", tags=[T_KB], responses=ERRORS, summary="Questions no article answered (what to write next)")
def kb_gaps(days: int = Query(7, ge=1, le=90), user: dict = Depends(support_only)):
    """Grouped by category; agents see their own teams' categories (an unclear question goes to everyone)."""
    return [g for g in kbs.gaps(ago(days=days)) if g["team"] is None or _team_visible(user, g["team"])]


@app.get("/kb/{kb_id}", tags=[T_KB], responses=ERRORS, summary="One article: the original and every version")
def kb_article(kb_id: str, user: dict = Depends(support_only)):
    if kb_id not in k.kb:
        raise HTTPException(404, "Article not found")
    a = k.kb[kb_id]
    return {"kb_id": kb_id, "title": a.title, "category": k.name(a.category_id), "handling": a.handling_mode,
            "original_steps": a.steps, "original_attempt1": a.attempt_1, "original_attempt2": a.attempt_2,
            "versions": kbs.versions(kb_id)}


@app.post("/kb/{kb_id}/drafts", tags=[T_KB], responses=ERRORS, summary="Save a new employee-facing draft")
def kb_save_draft(kb_id: str, body: DraftIn, user: dict = Depends(support_only)):
    """Agents and supervisors can draft. Nothing reaches employees until a supervisor approves it."""
    data = body.model_dump()
    return _kb_call(kbs.save_draft, kb_id, data, user["user_id"], body.note or "")


@app.post("/kb/versions/{version_id}/check", tags=[T_KB], responses=ERRORS,
          summary="Meaning check: does the rewrite still ask for the same thing?")
def kb_check(version_id: int, user: dict = Depends(support_only)):
    return _kb_call(kbs.check, version_id)


@app.post("/kb/versions/{version_id}/approve", tags=[T_KB], responses=ERRORS,
          summary="Approve a draft: it goes live and replaces the previous version")
def kb_approve(version_id: int, body: ApproveIn, user: dict = Depends(supervisor_only)):
    return _kb_call(kbs.approve, version_id, user, body.override_reason)


class SavedReplyIn(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    body: str = Field(min_length=1, max_length=2000)
    category_id: str | None = None
    active: bool = True


@app.get("/saved-replies", tags=[T_KB], responses=ERRORS,
         summary="Saved replies, filled in for a ticket if `ticket_id` is given")
def saved_replies(ticket_id: str | None = None, user: dict = Depends(support_only)):
    """Placeholders `{first_name}`, `{ticket_id}` and `{agent_name}` are filled in for the given ticket."""
    if not ticket_id:
        return kbs.saved_replies(include_inactive=user["role"] == "supervisor")
    t = _ticket_for(ticket_id, user)
    name = employee_profile(store, k, t["employee_id"]).get("Full_Name")
    return [{**r, "filled": fill_reply(r["body"], t, name, user)} for r in kbs.saved_replies(t["category_id"])]


@app.post("/saved-replies", tags=[T_KB], responses=ERRORS, summary="Create or update a saved reply (supervisor)")
def save_saved_reply(body: SavedReplyIn, reply_id: int | None = None, user: dict = Depends(supervisor_only)):
    if body.category_id and body.category_id not in k.categories:
        raise HTTPException(400, "Unknown category")
    return _kb_call(kbs.save_reply, user, body.title, body.body, body.category_id, reply_id, body.active)


@app.get("/users", tags=[T_SYS], response_model=list[StaffOut], responses=ERRORS,
         summary="List agents and supervisors (support only)")
def users(user: dict = Depends(support_only)):
    return [{"user_id": u["user_id"], "name": u["name"], "role": u["role"], "queues": u["queues"]}
            for u in auth.users() if u["role"] != "employee"]
