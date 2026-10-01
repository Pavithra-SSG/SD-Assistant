"""Thin HTTP client for the FastAPI backend. The bearer token lives in st.session_state only."""
from __future__ import annotations

import httpx
import streamlit as st

from servicedesk.config import API_URL


class ApiError(Exception):
    def __init__(self, status: int, detail, body: dict | None = None):
        self.status, self.detail, self.body = status, detail, body or {}
        super().__init__(f"{status}: {detail}")


@st.cache_resource
def _http() -> httpx.Client:
    return httpx.Client(base_url=API_URL, timeout=60)


def call(method: str, path: str, **kw):
    headers = {}
    if st.session_state.get("token"):
        headers["Authorization"] = f"Bearer {st.session_state.token}"
    try:
        r = _http().request(method, path, headers=headers, **kw)
    except httpx.ConnectError as e:
        raise ApiError(0, f"Can't reach the service desk API at {API_URL}. Start it with "
                          "`uvicorn api:app` and refresh.") from e
    if r.status_code == 401 and st.session_state.get("token"):
        st.session_state.clear()
        st.session_state.flash = r.json().get("detail", "Please sign in again")
        st.rerun()
    if r.status_code >= 400:
        try:
            body = r.json()
        except ValueError:
            body = {"detail": r.text}
        raise ApiError(r.status_code, body.get("detail", r.text), body)
    return r.json()


def get(path: str, **params):
    return call("GET", path, params={k: v for k, v in params.items() if v not in (None, [], "")})


def post(path: str, body: dict | None = None):
    return call("POST", path, json=body or {})


def upload(file, **form) -> dict:
    """POST /attachments with a Streamlit UploadedFile; returns the processed attachment (OCR text etc.)."""
    return call("POST", "/attachments", files={"file": (file.name, file.getvalue(), file.type or "image/png")},
                data={k: v for k, v in form.items() if v})


@st.cache_data(ttl=300, show_spinner=False, max_entries=200)
def _image(att_id: str, token: str) -> bytes | None:
    # the token is deliberately part of the cache key: Streamlit's cache is shared by every user of the app,
    # so a key of the attachment id alone would hand one person's screenshot to anyone who asks for that id
    r = _http().get(f"/attachments/{att_id}", headers={"Authorization": f"Bearer {token}"})
    return r.content if r.status_code == 200 else None


def image(att_id: str) -> bytes | None:
    """Screenshot bytes for the signed-in user (the API checks who may see it)."""
    return _image(att_id, st.session_state.get("token", ""))


def patch(path: str, body: dict):
    return call("PATCH", path, json=body)


@st.cache_data(ttl=600, show_spinner=False)
def meta() -> dict:
    """Reference data (categories, form fields, queues). Same for every user, so cached globally."""
    return get("/meta")
