"""Response models for the API. They document what each endpoint returns in /docs.

Every model allows extra fields (`extra="allow"`), so declaring a model never silently drops data an
endpoint returns. Request bodies live next to their routes in api.py.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Out(BaseModel):
    model_config = ConfigDict(extra="allow")


class Ok(Out):
    ok: bool = True


class ErrorOut(Out):
    detail: str = Field(description="What went wrong, e.g. 'role_denied' or 'Owned by Priya'")
    missing: list[str] | None = Field(None, description="Resolve only: the items still needed")


ERRORS = {401: {"model": ErrorOut, "description": "Not signed in, or the session expired"},
          403: {"model": ErrorOut, "description": "Signed in, but this role can't use this endpoint (role_denied)"},
          404: {"model": ErrorOut, "description": "Not found, or not visible to you"},
          409: {"model": ErrorOut, "description": "Conflict: illegal state change, already owned, stale version, "
                                                  "or resolve blocked"}}


# ---------------------------------------------------------------- system + sign-in
class HealthOut(Out):
    brain: str = Field(description="'Jev' or the offline mock")
    model: str | None = Field(description="TypeSafe model in use (None in mock mode)")
    degraded: dict | None = Field(description="Set when Jev failed recently: {since: timestamp}")
    version: str
    environment: str


class CategoryRef(Out):
    id: str = Field(examples=["CAT-02"])
    name: str = Field(examples=["VPN & Remote Access"])


class MetaOut(Out):
    categories: list[CategoryRef]
    issue_types: dict[str, list[str]] = Field(description="Category id → issue types (subcategories)")
    form_fields: dict[str, list[dict]] = Field(description="Category id → dynamic form fields")
    impact_options: list[str]
    workaround_options: list[str]
    contact_options: list[str]
    queues: list[str]
    resolution_codes: list[str]
    agent_resolution_codes: list[str]
    channels: list[str]
    impact_scale: dict[str, str]
    urgency_scale: dict[str, str]
    kb: dict[str, str] = Field(description="KB id → title")


class UserOut(Out):
    user_id: str = Field(examples=["EMP2010"], description="Employee ID (IT staff are employees too)")
    employee_id: str | None = Field(examples=["EMP2010"])
    name: str
    role: Literal["employee", "agent", "supervisor"]
    queues: list[str] = Field(description="Queues an agent works; empty for employees")
    email: str | None
    job_title: str | None = Field(None, examples=["Network Engineer"])
    department: str | None = None
    location: str | None = None
    status: Literal["active", "disabled"] = "active"
    must_change_password: bool = Field(False, description="True after a temporary password: only "
                                                          "/auth/change-password works until it is changed")
    locked: bool = False


class TempPasswordOut(Out):
    user_id: str
    temporary_password: str = Field(description="Shown once. Hand it over through a verified channel; the "
                                                "person must change it at first sign-in")


class ImportOut(Out):
    created: list[dict] = Field(description="New accounts with their one-time temporary passwords")
    updated: int
    errors: list[str]


class LoginOut(Out):
    token: str = Field(description="Bearer token: click Authorize and paste it")
    user: UserOut
    expires_at: str


class MeOut(UserOut):
    location: str | None
    department: str | None
    asset_tag: str | None


class RecoverOut(Out):
    outcome: Literal["reset_done", "human_v3", "submitted"]
    message: str
    ticket_id: str | None = None


class StaffOut(Out):
    user_id: str
    name: str
    role: str
    job_title: str | None = None
    queues: list[str]


# ---------------------------------------------------------------- employee: chat
class SessionOut(Out):
    session_id: str


class SessionRow(Out):
    session_id: str
    updated_at: str | None
    in_progress: bool = Field(False, description="True while the bot is waiting for an answer in this chat, or a "
                                                 "person from IT is chatting in it: reopen it rather than start a new one")
    n_messages: int = Field(0, description="0 for a conversation that was opened but never used")
    ticket_id: str | None = Field(None, description="The latest ticket this conversation is about")


class ChatOut(Out):
    reply: str | None = Field(description="Bot reply. None when a human owns the chat (the bot stays silent)")
    quick_replies: list[str] = Field(description="Buttons to show under the reply")
    ticket_id: str | None
    attempt: int | None = Field(None, description="1 or 2 when this reply is a troubleshooting attempt")
    message_id: int | None = Field(None, description="Id of the bot message (use it to rate the answer)")
    can_rate: bool = Field(False, description="Show 👍/👎: POST /me/feedback/answer with message_id")


class AttachmentOut(Out):
    id: str
    filename: str | None
    ocr_text: str | None = Field(description="Text read from the image; secret lines replaced by [hidden: …]")
    ocr_confidence: float | None
    readable: bool = Field(description="False: the bot will ask the employee to type the error instead")
    error_codes: list[str]
    secrets_blurred: int = Field(description="Lines blurred because they looked like a password or code")
    width: int | None
    height: int | None
    created_at: str
    delete_after: str | None
    available: bool = Field(description="False once deleted after the retention period")


class MessageOut(Out):
    id: int
    role: Literal["user", "bot", "agent", "system"]
    text: str
    created_at: str
    ticket_id: str | None
    agent: str | None = Field(None, description="Agent name on human replies")
    quick_replies: list[str] = []


# ---------------------------------------------------------------- employee: form
class ReviewOut(Out):
    review_id: str | None = Field(description="Pass to /forms/submit")
    priority: str = Field(description="Computed; the employee can't change it")
    priority_reason: str
    category_name: str
    final_category: str
    suggested_category: str | None = Field(description="Jev thinks another category fits (suggestion only)")
    suggested_category_name: str | None
    impact: str
    urgency: str
    warnings: list[str] = Field(description="Secret masked, stop-use, security notice…")
    masked: dict = Field(description="Fields whose secrets were hidden")
    try_first: dict | None = Field(description="Optional KB step to try before submitting")
    duplicate: dict | None = Field(description="Open ticket in the same category from the last 24h")
    degraded: str | None = Field(None, description="Jev unavailable: the ticket will go to a person")


class SubmitOut(Out):
    outcome: Literal["choose", "escalated", "added_to_existing"] = Field(
        description="choose → call /forms/{ticket_id}/choice · escalated → a human has it")
    ticket_id: str
    session_id: str | None = None
    priority: str | None = None
    queue: str | None = None
    message: str


class ChoiceOut(Out):
    session_id: str
    reply: str | None
    quick_replies: list[str]
    ticket_id: str
    attempt: int | None = Field(None, description="1 or 2 when this reply is a troubleshooting attempt")
    message_id: int | None = Field(None, description="Id of the bot message (use it to rate the answer)")
    can_rate: bool = Field(False, description="Show 👍/👎: POST /me/feedback/answer with message_id")


# ---------------------------------------------------------------- employee: my tickets
class EmployeeTicket(Out):
    ticket_id: str
    status: str
    state: str = Field(description="Friendly label, e.g. 'With Network team'")
    priority: str
    summary: str
    category: str
    subcategory: str | None
    queue: str
    owner: str | None = Field(description="Name of the engineer working on it")
    channel: str | None
    created_at: str
    resolved_at: str | None
    resolve_due: str | None
    incident_parent: str | None
    can_reopen: bool
    can_confirm: bool
    can_cancel: bool
    bot_active: bool


class EmployeeTicketDetail(EmployeeTicket):
    thread: list[MessageOut] = Field(description="Customer-visible messages only (never work notes)")


class ReplyOut(Out):
    action: Literal["chat", "comment", "reopened", "new_ticket"] = Field(
        description="chat: bot answered · comment: added for the engineer · reopened · new_ticket (past 7 days)")
    reply: str | None = None
    quick_replies: list[str] | None = None
    ticket_id: str | None = None


class NotificationOut(Out):
    id: int
    user_id: str
    ticket_id: str | None
    text: str
    created_at: str
    read_at: str | None


# ---------------------------------------------------------------- support: queue + ticket
class SlaOut(Out):
    response_due: str | None = None
    resolve_due: str | None = None
    response_state: Literal["ok", "at_risk", "breached", "met"] | None = None
    resolve_state: Literal["ok", "at_risk", "breached", "met"] | None = None
    ack_hours: float | None = None
    resolve_hours: float | None = None


class QueueRow(Out):
    ticket_id: str
    priority: str
    status: str
    category: str
    queue: str
    owner: str | None
    owner_id: str | None
    summary: str
    channel: str | None
    created_at: str
    sla: SlaOut
    escalation_reason: str | None
    incident_parent: str | None
    is_incident: int | None
    requester: str


class TicketOut(Out):
    """A full ticket row (support view)."""
    ticket_id: str
    status: str
    priority: str
    priority_source: str | None
    category_id: str
    subcategory: str | None
    impact: str | None
    urgency: str | None
    queue: str
    owner: str | None
    version: int = Field(description="Send back with /update (optimistic locking)")
    summary: str


class ChecklistItem(Out):
    ticket_id: str
    item_id: str
    text: str
    required: int
    done_by: str | None
    done_at: str | None


class AlertOut(Out):
    alert_id: int
    ticket_id: str
    event: str
    level: Literal["modal", "toast", "badge", "system"]
    priority: str | None
    target_queue: str | None
    text: str | None
    realert_count: int | None = 0
    acked_by: str | None
    acked_at: str | None
    escalated_at: str | None = Field(description="Set when copied / escalated to the supervisor")


class PersonOut(Out):
    user_id: str = Field(description="Employee ID")
    name: str
    role: Literal["requester", "agent", "supervisor"]
    job_title: str | None
    team: str | None
    did: list[str] = Field(description="What they did on this ticket, e.g. Claimed, Resolved, Added work notes")
    first_at: str | None
    last_at: str | None
    current_owner: bool


class TicketDetailOut(Out):
    ticket: TicketOut
    sla: SlaOut
    bot_panel: dict = Field(description="Risk flags, Jev confidence, verification, attempts, KB, reason")
    suggested_kb: list[dict] = Field(description="Top KB articles with Jev relevance")
    checklist: list[ChecklistItem]
    messages: list[dict] = Field(description="All messages incl. internal work notes")
    events: list[dict] = Field(description="Append-only timeline: bot decisions, status changes, alerts. "
                                           "`actor_name` names the staff member who did it")
    watchers: list[str] = Field(description="Names of people watching the ticket")
    people: list[PersonOut] = Field(description="The requester, then every IT staff member who worked on it")
    linked: list[str] = Field(description="Tickets linked to this incident")
    legal_next: list[dict] = Field(description="Allowed next states and the action that reaches each")
    alerts: list[AlertOut]


class PendingAlertsOut(Out):
    alerts: list[AlertOut] = Field(description="What to pop up now (P1 modal, P2 toast, system banner)")
    counts: dict[str, int] = Field(description="Unacknowledged alerts by priority, for badges")
    total: int
    backlog: bool = Field(description="Supervisor: too many unacknowledged alerts")
    degraded: dict | None = Field(description="Jev failed recently → show 'Model degraded'")


# ---------------------------------------------------------------- analytics
class KpisOut(Out):
    open: int
    p1_open: int
    awaiting_human: int
    bot_resolution_rate: float | None
    sla_met_pct: float | None
    reopen_rate: float | None
    total: int
    answers_helpful_pct: float | None = Field(None, description="Share of 👍 among employee ratings of bot answers")
    answer_ratings: int = 0
    satisfaction: float | None = Field(None, description="Average 1–5 rating given when tickets were fixed")
    satisfaction_ratings: int = 0


class DashboardOut(Out):
    kpis: KpisOut
    charts: dict[str, Any] = Field(description="C1–C12 datasets + metric targets")
    scope: dict = Field(description="Filters actually applied (agents are limited to their queues)")


class BotAnswersOut(Out):
    answers: list[dict] = Field(description="Every KB answer the bot sent, newest first")
    success: list[dict] = Field(description="Per article: sent, fixed, success_rate")


class PerformanceRow(Out):
    user_id: str
    name: str
    handled: int
    resolved: int
    median_first_response_h: float | None
    median_resolution_h: float | None
    sla_met_pct: float | None
    reopen_rate: float | None
    checklist_completion: float | None
    corrections_made: int


class ShiftOut(Out):
    summary: dict = Field(description="Today's numbers + a fixed-template text summary")
    status: dict = Field(description="Live hints: open P1s, unacknowledged alerts, unowned at-risk tickets")
    checklist: list[dict]


class CorrectionOut(Out):
    id: int
    ticket_id: str
    field: str
    old_value: str | None
    new_value: str | None
    corrected_by: str = Field(description="Employee ID of the person who fixed it")
    corrected_by_name: str | None = None
    reason: str | None
    created_at: str
