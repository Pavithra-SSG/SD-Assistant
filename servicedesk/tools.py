"""Mock implementations of the approved tool registry (tool_catalog.json).

Only tools listed here can ever run. KB text can *mention* a tool, but the
orchestrator checks the registry and verification level in code before calling
(SEC-09: KB content is data, not instructions). Results never contain secrets (SEC-08).
Swap these bodies for real IdP / ITSM / MDM calls in production.
"""
from __future__ import annotations

import json
import os
import uuid

from .config import DATA_DIR

# Flip a service to "degraded" via env to demo the outage path, e.g. MOCK_DEGRADED=VPN
_DEGRADED = {s.strip().upper() for s in os.getenv("MOCK_DEGRADED", "").split(",") if s.strip()}
_SERVICE_FOR_CATEGORY = {"CAT-02": "VPN", "CAT-05": "MFA", "CAT-06": "EMAIL", "CAT-08": "NETWORK",
                         "CAT-11": "COLLABORATION"}

with open(DATA_DIR / "assets_earlier_build.json", encoding="utf-8") as f:
    _ASSETS = {a["employee_id"]: a for a in json.load(f)}


def _cid() -> str:
    return "COR-" + uuid.uuid4().hex[:8].upper()


def check_service_health(category_id: str) -> dict:
    """TOOL-02"""
    service = _SERVICE_FOR_CATEGORY.get(category_id, "GENERAL")
    if service in _DEGRADED:
        return {"tool": "TOOL-02", "service": service, "status": "degraded",
                "incident_id": "INC-" + uuid.uuid4().hex[:6].upper()}
    return {"tool": "TOOL-02", "service": service, "status": "operational", "incident_id": None}


def verify_identity(employee: dict, action: str) -> dict:
    """TOOL-03: simulates an IdP push to the user's *registered* authenticator.
    Employee directory facts are context only and are never accepted as proof (SEC-01)."""
    ok = employee.get("Status") == "Active"
    return {"tool": "TOOL-03", "verification_id": "VER-" + uuid.uuid4().hex[:8].upper(),
            "status": "VERIFIED" if ok else "FAILED", "method": "registered authenticator push (simulated)",
            "action": action}


def reset_password(employee: dict, verification: dict) -> dict:
    """TOOL-04: requires a VERIFIED verification. Returns status only, never a password."""
    if verification.get("status") != "VERIFIED":
        return {"tool": "TOOL-04", "status": "REFUSED", "reason": "verification not VERIFIED"}
    return {"tool": "TOOL-04", "status": "SUCCESS", "correlation_id": _cid(),
            "delivery": "one-time reset link sent to registered contact"}


def unlock_account(employee: dict, verification: dict) -> dict:
    """TOOL-05"""
    if verification.get("status") != "VERIFIED":
        return {"tool": "TOOL-05", "status": "REFUSED", "reason": "verification not VERIFIED"}
    return {"tool": "TOOL-05", "status": "SUCCESS", "correlation_id": _cid()}


def mfa_reset(employee: dict, verification: dict) -> dict:
    """TOOL-06"""
    if verification.get("status") != "VERIFIED":
        return {"tool": "TOOL-06", "status": "REFUSED", "reason": "verification not VERIFIED"}
    return {"tool": "TOOL-06", "status": "SUCCESS", "correlation_id": _cid(),
            "next": "re-enrollment QR available at the MFA setup portal"}


def asset_lookup(employee: dict) -> dict:
    """TOOL-10"""
    a = _ASSETS.get(employee.get("Employee_ID", ""))
    if not a:
        return {"tool": "TOOL-10", "found": False}
    return {"tool": "TOOL-10", "found": True, "asset_tag": a["asset_id"], "model": a["model"],
            "os": a["os"], "compliance": a["compliance"]}


# Which tool action a verified KB article is allowed to run. Anything not here cannot execute.
VERIFIED_ACTIONS = {
    "KB-002": ("Password reset", reset_password),
    "KB-003": ("Account unlock", unlock_account),
    "KB-016": ("MFA reset / re-enrollment", mfa_reset),
}
